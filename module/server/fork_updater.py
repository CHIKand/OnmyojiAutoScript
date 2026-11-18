"""检查更新和合并上游：把 fork_git 的 git 操作层接到部署配置、.env、素材重新生成、导入自检和实例重启上。

OASX 的"更新"页调用 /home/update_info 和 /home/execute_update。上游 home_router 里这两个处理函数的函数体各只剩两行，
分别转到这里的 check_update 和 run_update：git 和网络调用都放到工作线程里，不卡事件循环；两个函数无论 git、网络、
部署配置出什么问题都不抛异常（接口不返回 500），出错记日志并给出文案。

合并（execute）和检查更新（update_info）共用 MERGE_LOCK，同一时刻只有一个在动仓库：merge_upstream 不可重入，
后到的调用等前一个做完再开始。每日定时合并也走 run_update，规则一样。
合并成功并推送（merged），或者补推了之前没推上去的提交（pushed）之后才重启在跑的实例，让它们换上新代码；
推送失败时不重启，本地提交由启动器守卫保护，用户重试一次就会补推并重启。
"""
import asyncio
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from module.config.dotenv import read_env
from module.logger import logger
from module.server.fork_git import (UPSTREAM_REMOTE, GitRunner, MergeResult, count_behind, ensure_remote,
                                    merge_upstream, upstream_commits)
from module.server.instance_control import restart_running_instances

DEFAULT_UPSTREAM_URL = 'https://gitcode.com/OnmyojiAutoScript/OnmyojiAutoScript.git'
DEFAULT_UPSTREAM_BRANCH = 'dev'
HISTORY_LIMIT = 15                     # 更新页列出的上游提交数，与上游 Updater.get_commit(n=15) 一致
ERROR_STATUS = 'error'                 # 不是 merge_upstream 给的状态：合并过程中出了意外，没有来得及得出结果
RESTART_STATUSES = frozenset({'merged', 'pushed'})
DEPENDENCY_NOTE = '；依赖有变化，请在 OASX 重启服务以安装新依赖'
DIRTY_LINES = 10                       # 未提交改动的提示最多列出的行数
SMOKE_CODE = 'import script; import module.server.custom_routes; import module.orchestra.runtime'
SMOKE_TIMEOUT = 120                    # 秒
SMOKE_LINES = 20                       # 自检失败时带回的 stderr 末尾行数

MERGE_LOCK = threading.Lock()


def _upstream_settings() -> tuple[str, str]:
    """上游地址和分支。每次调用都重新读，改了 .env 不用重启；写成空值按没写处理。"""
    return (read_env('OAS_UPSTREAM_URL', DEFAULT_UPSTREAM_URL) or DEFAULT_UPSTREAM_URL,
            read_env('OAS_UPSTREAM_BRANCH', DEFAULT_UPSTREAM_BRANCH) or DEFAULT_UPSTREAM_BRANCH)


def _deploy_settings() -> tuple[str, str]:
    """部署配置里的 git 可执行文件（绝对路径）和分支。DeployConfig 一构造就会在当前目录建 config/deploy.yaml，
    所以用到时才导入、构造。"""
    from module.server.config import DeployConfig
    config = DeployConfig()
    return config.filepath('GitExecutable'), config.Branch


def _usable_git(configured: str) -> str:
    """部署配置里的 git 不存在时改用系统 PATH 里的 git（开发克隆里没有工具链），并在日志里写明用的是哪个。
    不带目录的命令名交给 PATH，不核对。"""
    if not os.path.dirname(configured) or os.path.isfile(configured):
        return configured
    system = shutil.which('git')
    if system is None:
        logger.warning(f'部署配置里的 git 不存在：{configured}，系统里也找不到 git，之后的 git 调用会失败')
        return configured
    logger.info(f'部署配置里的 git 不存在：{configured}，改用系统里的 git：{system}')
    return system


def _describe(exc: Exception) -> str:
    """异常的可读说明：git 命令失败时优先用它的 stderr。"""
    stderr = getattr(exc, 'stderr', None)
    return (stderr.strip() if isinstance(stderr, str) else '') or f'{type(exc).__name__}: {exc}'


def _failure(exc: Exception) -> MergeResult:
    return MergeResult(ERROR_STATUS, 0, [], [], _describe(exc))


class ForkUpdater:
    """某个仓库目录里的"检查更新"和"合并上游"。

    不传参数时读部署配置（GitExecutable 和 Branch），仓库是当前工作目录；测试直接传三个参数，不碰 DeployConfig。
    branch 是 fork 上的分支名，推送一律 push origin HEAD:<branch>，运行目录的本地分支名可能不一样。
    """

    def __init__(self, repo: Path | None = None, git: str | None = None, branch: str | None = None):
        if git is None or branch is None:
            configured_git, configured_branch = _deploy_settings()
            git = configured_git if git is None else git
            branch = configured_branch if branch is None else branch
        self.repo = Path.cwd() if repo is None else Path(repo)
        self.git = _usable_git(git)
        self.branch = branch
        self.runner = GitRunner(self.git, self.repo)

    def update_info(self) -> dict | None:
        """OASX 更新页的数据，键与 UpdateInfoModel 一致：
        is_update（上游领先 HEAD 的提交数大于 0）、branch、current_commit（HEAD）、latest_commit（上游最新）、
        commit（上游领先的最新 15 个提交）。提交都是 [sha, 作者, ISO 日期, 标题]。

        先取一次上游。取不到（断网、超时）或者出任何别的错，都记日志并返回 None，与上游的 update_info 一样。
        和合并共用 MERGE_LOCK，合并进行时等它做完，所以几项数据来自同一个仓库状态。
        """
        try:
            with MERGE_LOCK:
                return self._update_info()
        except Exception:
            logger.exception('检查更新失败')
            return None

    def _update_info(self) -> dict | None:
        upstream, upstream_branch = _upstream_settings()
        ensure_remote(self.runner, UPSTREAM_REMOTE, upstream)
        fetched = self.runner.run('fetch', UPSTREAM_REMOTE, upstream_branch, check=False)
        if fetched.returncode != 0:
            logger.error(f'检查更新：拉取上游失败：{fetched.stderr.strip()}')
            return None
        ref = f'{UPSTREAM_REMOTE}/{upstream_branch}'
        return {
            'is_update': count_behind(self.runner, ref) > 0,
            'branch': self.branch,
            'current_commit': self._commit('HEAD'),
            'latest_commit': self._commit(ref),
            'commit': [list(item) for item in upstream_commits(self.runner, ref, HISTORY_LIMIT)],
        }

    def _commit(self, ref: str) -> list[str]:
        """ref 指向的提交：[sha, 作者, ISO 日期, 标题]，格式与 upstream_commits 一致。"""
        line = self.runner.run('log', '-1', '--format=%H%x1f%an%x1f%aI%x1f%s', ref, '--').stdout.strip('\n')
        return line.split('\x1f', 3)

    def execute(self) -> MergeResult:
        """把上游合并进当前分支并推送，结果的状态和文案见 merge_upstream 和 message_of。

        持有 MERGE_LOCK 做完整个合并，别的合并和检查更新排队等着。merge_upstream 报告的各种状态原样返回；
        它周围的意外异常（git 或临时目录不见了、git 处在意料之外的状态……）记日志，转成 status 为 error 的结果，不往外抛。
        """
        with MERGE_LOCK:
            try:
                upstream, upstream_branch = _upstream_settings()
                return merge_upstream(self.runner, upstream, upstream_branch, self.branch, self.regenerate,
                                      default_verify)
            except Exception as exc:
                logger.exception('合并上游时出现意外错误')
                return _failure(exc)

    def regenerate(self, name: str) -> None:
        """重新生成 tasks/<name>/assets.py：name 是任务目录名，组件是 Component/<组件>。

        AssetsExtractor 按当前工作目录算素材的相对路径（dev_tools/assets_extract.py 的 relative_to(Path.cwd())），
        所以当前目录必须就是仓库根，否则抛异常，merge_upstream 把合并中止。不在服务进程里 os.chdir：进程是各线程共用的。
        """
        cwd = Path.cwd().resolve()
        if cwd != self.repo.resolve():
            raise RuntimeError(f'重新生成 assets.py 需要在仓库根目录下进行：当前目录 {cwd}，仓库 {self.repo}')
        from dev_tools.assets_extract import AssetsExtractor      # 它依赖 numpy、tqdm，用到时才导入
        AssetsExtractor(str(self.repo / 'tasks' / name)).extract()


def _smoke_python() -> str:
    """跑自检的 Python。服务用 pythonw.exe 启动时它没有 stderr，抓不到报错，所以同目录有 python.exe 就用它。"""
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        console = executable.with_name('python.exe')
        if console.is_file():
            return str(console)
    return str(executable)


def default_verify(repo: Path) -> tuple[bool, str]:
    """合并结果提交之前的导入自检：在仓库里另起一个 Python，导入 script、module.server.custom_routes 和
    module.orchestra.runtime。上游改名了什么东西、合并没有冲突却会让实例和服务一重启就起不来，这里先拦住，
    merge_upstream 据此把合并中止。返回 (是否通过, 原因)：失败时是 stderr 的最后 20 行，超时是 'timed out'。
    子进程的 stderr 用 PYTHONIOENCODING 定成 UTF-8：管道里的 Python 在中文 Windows 上默认写 GBK，按 UTF-8 解码会乱码。
    """
    try:
        done = subprocess.run([_smoke_python(), '-c', SMOKE_CODE], cwd=repo, capture_output=True, encoding='utf-8',
                              errors='replace', stdin=subprocess.DEVNULL, timeout=SMOKE_TIMEOUT,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'PYTHONIOENCODING': 'utf-8'},
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))   # 服务没有控制台，不让它弹黑窗口
    except subprocess.TimeoutExpired:
        return False, 'timed out'
    if done.returncode == 0:
        return True, ''
    lines = [line for line in done.stderr.splitlines() if line.strip()]
    return False, '\n'.join(lines[-SMOKE_LINES:]) or f'exit code {done.returncode}'


def _lines(text: str) -> list[str]:
    """非空行，每行去掉首尾空白。"""
    return [line.strip() for line in text.splitlines() if line.strip()]


def message_of(result: MergeResult, restarted: list[str]) -> str:
    """合并结果的提示文案，OASX 更新页和日志里都显示它。restarted 是实际重启了的实例名。

    几个固定文案（已是最新、合并成功、代码冲突、推送失败）出自计划，不要改字；其余状态的文案与它们同风格。
    requirements_changed 为真时，不管什么状态，末尾都追加依赖有变化的提示。
    dirty 的说明只列前 DIRTY_LINES 行，多出来的行数记在末尾（"等 N 项"）；fetch_failed 只留最后一个非空行（git 的
    最终报错）；conflict_aborted 没有冲突文件（合并根本没开始）时，{files} 处改放 git 的说明。
    """
    status, commits, detail = result.status, result.commits, result.message.strip()
    names = '、'.join(restarted) or '无'
    if status == 'up_to_date':
        text = '已是最新，无需合并'
    elif status == 'merged':
        text = f'已合并上游 {commits} 个提交并推送到 fork；已重启实例：{names}'
    elif status == 'pushed':
        pushed = f'已把之前未推送的 {commits} 个提交推送到 fork' if commits else '已把本地提交推送到 fork'
        text = f'{pushed}；已重启实例：{names}'
    elif status == 'conflict_aborted':
        files = '、'.join(result.conflicts) or detail
        text = f'合并上游遇到代码冲突，已中止，分支未改动：{files}'
    elif status == 'push_failed':
        text = f'已在本地合并上游 {commits} 个提交，但推送失败：{detail}。启动器守卫会保护本地提交，请检查网络或令牌后重试'
    elif status == 'dirty':
        lines = _lines(detail)
        shown, more = '\n'.join(lines[:DIRTY_LINES]), len(lines) - DIRTY_LINES
        text = f'运行目录有未提交的改动，已跳过合并：{shown}' + (f'\n等 {more} 项' if more > 0 else '')
    elif status == 'fetch_failed':
        last = _lines(detail)[-1] if detail else ''
        text = f'拉取上游失败，未改动任何文件：{last}'
    elif status == 'smoke_failed':
        text = f'合并上游后自检失败，已中止，分支未改动：{detail}'
    else:                       # ERROR_STATUS，以及 fork_git 以后新增、这里还不认识的状态
        text = f'合并上游时出现意外错误，请查看服务日志：{detail or status}'
    return text + (DEPENDENCY_NOTE if result.requirements_changed else '')


def _check() -> dict | None:
    return ForkUpdater().update_info()


def _merge() -> MergeResult:
    return ForkUpdater().execute()


async def check_update() -> dict | None:
    """GET /home/update_info 的实现。git 在工作线程里跑；任何错误都记日志并返回 None，不往外抛。"""
    try:
        return await asyncio.to_thread(_check)
    except Exception:
        logger.exception('检查更新失败')
        return None


async def run_update() -> str:
    """GET /home/execute_update 的实现，每日定时合并也调它：在工作线程里合并，merged 或 pushed 之后重启在跑的
    实例，返回提示文案。任何错误都转成文案，不往外抛。重启失败（restart_running_instances 自己已经逐个实例兜住错误）
    不能把已经成功的合并报成失败：记日志，文案里的重启名单按空算。"""
    try:
        result = await asyncio.to_thread(_merge)
    except Exception as exc:                    # 比如读不了部署配置；合并本身的意外已经在 execute 里转成结果
        logger.exception('合并上游失败')
        result = _failure(exc)
    restarted: list[str] = []
    if result.status in RESTART_STATUSES:
        try:
            restarted = await restart_running_instances()
        except Exception:
            logger.exception('合并上游后重启实例失败')
    message = message_of(result, restarted)
    logger.info(message)
    return message
