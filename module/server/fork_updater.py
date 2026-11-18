"""检查更新和合并上游：把 fork_git 的 git 操作层接到部署配置、.env、素材重新生成、导入自检和实例重启上。

OASX 的"更新"页调用 /home/update_info 和 /home/execute_update。上游 home_router 里这两个处理函数的函数体各只剩两行，
分别转到这里的 check_update 和 run_update：git 和网络调用都放到工作线程里，不卡事件循环；两个函数无论 git、网络、
部署配置出什么问题都不抛异常（接口不返回 500），出错记日志并给出文案。

合并（execute）和检查更新（update_info）共用 MERGE_LOCK，同一时刻只有一个在动仓库：merge_upstream 不可重入，
后到的调用等前一个做完再开始。每日定时合并走 run_update_result（run_update 的核心，多返回合并结果），规则一样。
磁盘上的代码变了就重启在跑的实例，让它们换上新代码：合并并推送成功（merged）、补推了之前没推上去的提交（pushed）、
本地合并了但推送失败（push_failed 且 merged_locally；本地提交由启动器守卫保护，重试会补推）。上游新增了第三方依赖
（needs_install，或者它推送失败）不重启：新进程一启动就缺模块，要等用户在 OASX 重启服务、启动器装好依赖。
提示文案只放一行摘要，git 的原文整段写进日志。
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
from module.server.fork_git import (FORK_REMOTE, UPSTREAM_REMOTE, GitRunner, MergeResult, count_behind,
                                    ensure_remote, merge_upstream, upstream_commits)
from module.server.instance_control import restart_running_instances

DEFAULT_UPSTREAM_URL = 'https://gitcode.com/OnmyojiAutoScript/OnmyojiAutoScript.git'
DEFAULT_UPSTREAM_BRANCH = 'dev'
HISTORY_LIMIT = 15                     # 更新页列出的上游提交数，与上游 Updater.get_commit(n=15) 一致
ERROR_STATUS = 'error'                 # 不是 merge_upstream 给的状态：合并过程中出了意外，没有来得及得出结果
RESTART_STATUSES = frozenset({'merged', 'pushed'})     # 另外还有本地合并了、推送失败的 push_failed，见 _restarts
CHECKOUT_CHANGED = frozenset({'merged', 'pushed', 'needs_install'})   # 同上，见 _changed_checkout
DEPENDENCY_NOTE = '；依赖有变化，请在 OASX 重启服务以安装新依赖'
SERVER_CODE_NOTE = '；服务端代码有变化，请在 OASX 重启服务以完整生效'
RESTART_FAILED_NOTE = '；重启实例失败，请在总览页手动重启'
DIRTY_HINT = '需要保留的改动请到素材维护页提交；不需要的，在 OASX 重启服务会把它们还原为 fork 版本（会丢失这些改动）'
PUSH_REJECTED = 'fork 上有更新的提交，推送被拒'
PUSH_CREDENTIALS = '凭据无效或需要登录'
PUSH_NETWORK = '网络不通或超时'
LIST_LIMIT = 10                        # 提示里最多列出的条目数：未提交的改动、代码冲突的文件、git 列出的文件
SMOKE_CODE = ('import script; import module.server.custom_routes; import module.orchestra.runtime; '
              'import module.server.app')
SMOKE_TIMEOUT = 120                    # 秒
SMOKE_LINES = 20                       # 自检失败时带回的 stderr 末尾行数

# 推送失败原因的归类，按顺序判断，比较时不分大小写。都对不上就取 stderr 第一行有内容的话
_NON_FAST_FORWARD = ('(fetch first)', 'non-fast-forward')
_CREDENTIAL_SIGNS = ('authentication failed', 'could not read username', 'could not read password',
                     'terminal prompts disabled', 'user interactivity has been disabled',
                     'invalid username or password', 'password authentication was removed', 'permission denied',
                     ' denied to ', 'returned error: 401', 'returned error: 403', 'http basic: access denied')
_NETWORK_SIGNS = ('timed out', 'could not resolve host', 'could not resolve proxy', 'failed to connect',
                  "couldn't connect to server", 'connection refused', 'connection reset', 'connection was reset',
                  'network is unreachable', 'recv failure', 'send failure', 'empty reply from server',
                  'ssl_error_syscall', 'failed to receive handshake', 'unexpected disconnect',
                  'the remote end hung up unexpectedly', 'early eof', 'rpc failed')

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
    """合并结果提交之前的导入自检：在仓库里另起一个 Python，导入 script、module.server.custom_routes、
    module.orchestra.runtime 和 module.server.app。上游改名了什么东西、合并没有冲突却会让实例和服务一重启就起不来，
    这里先拦住，merge_upstream 据此把合并中止（只缺上游新增的第三方依赖时例外，见 merge_upstream）。
    导入 module.server.app 会在子进程里建 MainManager（读 config/ 下的实例配置，一个都没有时像服务启动那样从模板建
    oas1.json；起一个守护线程），不起服务、不占端口，子进程随即退出。
    返回 (是否通过, 原因)：失败时是 stderr 的最后 20 行，超时是 'timed out'。
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


def _capped(items: list[str]) -> str:
    """用"、"连起来，最多 LIST_LIMIT 项，多出来的记成" 等 N 项"。"""
    more = len(items) - LIST_LIMIT
    return '、'.join(items[:LIST_LIMIT]) + (f' 等 {more} 项' if more > 0 else '')


def _summary(text: str) -> str:
    """git 报错的一行摘要：第一行有内容的话（跳过 hint: 提示、推送开头的 To <地址>），紧跟其后缩进列出的文件
    （比如会被合并覆盖的未跟踪文件）接在同一行。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('hint:') or stripped == 'remote:' or line.startswith('To '):
            continue
        items = []
        for following in lines[index + 1:]:
            if not following.strip() or not following[:1].isspace():
                break
            items.append(following.strip())
        return f'{stripped} {_capped(items)}' if items else stripped
    return ''


def push_reason(detail: str) -> str:
    """推送失败的原因，归成一句：fork 上有更新的提交（非快进被拒）、凭据问题、网络或超时；都不是就取 git 报错的
    第一行。原文由 run_update_result 整段写进日志。"""
    text = detail.lower()
    if any(sign in text for sign in _NON_FAST_FORWARD):
        return PUSH_REJECTED
    if any(sign in text for sign in _CREDENTIAL_SIGNS):
        return PUSH_CREDENTIALS
    if any(sign in text for sign in _NETWORK_SIGNS):
        return PUSH_NETWORK
    return _summary(detail)


def _restarts(result: MergeResult) -> bool:
    """合并之后要不要重启在跑的实例：磁盘上的代码变了就重启（merged、pushed、本地合并了但推送失败的 push_failed），
    缺上游新增的第三方依赖时不重启（新进程一启动就缺模块）。"""
    if result.missing_module:
        return False
    return result.status in RESTART_STATUSES or (result.status == 'push_failed' and result.merged_locally)


def _changed_checkout(result: MergeResult) -> bool:
    """这次调用之后，运行目录里的代码与服务进程启动时加载的可能不一样了。"""
    return result.status in CHECKOUT_CHANGED or (result.status == 'push_failed' and result.merged_locally)


def message_of(result: MergeResult, restarted: list[str] | None) -> str:
    """合并结果的提示文案，OASX 更新页和日志里都显示它。restarted 是实际重启了的实例名，None 表示重启时出错。

    几个固定文案（已是最新、合并成功、代码冲突、推送失败）出自计划，不要改字；其余状态的文案与它们同风格。
    推送失败分两种：这次合并了、没推上去（merged_locally）用计划的文案，末尾接重启结果；补推之前的本地提交又失败了，
    说还有几个提交没推上去，不重启。推送失败的原因归成一句（push_reason）。
    重启了实例的状态，末尾写"已重启实例：<名字>"（一个都没有写"无"），重启出错时改写"重启实例失败……"。
    缺上游新增的依赖时（needs_install，或者它推送失败）末尾改说依赖的事，实例没有重启。
    之后按顺序追加：服务端代码有变化的提示（只在这次改了运行目录代码的状态上）、依赖有变化的提示（requirements_changed
    为真时，不管什么状态，都在最末尾）。
    dirty 的说明只列前 LIST_LIMIT 行，多出来的行数记在末尾（"等 N 项"），再接一句怎么处理这些改动；代码冲突的文件
    最多列 LIST_LIMIT 个；fetch_failed 只留最后一个非空行（git 的最终报错），并说清拉取的是 fork 还是上游；
    conflict_aborted 没有冲突文件时合并根本没开始，不说成代码冲突。
    """
    status, commits, detail = result.status, result.commits, result.message.strip()
    if restarted is None:
        restart = RESTART_FAILED_NOTE
    else:
        restart = f'；已重启实例：{"、".join(restarted) or "无"}'
    dependency = f'；上游新增依赖 {result.missing_module}，请在 OASX 重启服务（启动器会先安装依赖），实例暂未重启'
    if status == 'up_to_date':
        text = '已是最新，无需合并'
    elif status == 'merged':
        text = f'已合并上游 {commits} 个提交并推送到 fork{restart}'
    elif status == 'needs_install':
        text = f'已合并上游 {commits} 个提交并推送到 fork{dependency}'
    elif status == 'pushed':
        pushed = f'已把之前未推送的 {commits} 个提交推送到 fork' if commits else '已把本地提交推送到 fork'
        text = f'{pushed}{restart}'
    elif status == 'conflict_aborted' and result.conflicts:
        text = f'合并上游遇到代码冲突，已中止，分支未改动：{_capped(result.conflicts)}'
    elif status == 'conflict_aborted':      # 合并根本没开始（有未跟踪的文件挡着、index.lock……）
        text = f'合并未能开始，分支未改动：{_summary(detail)}'
    elif status == 'push_failed' and result.merged_locally:
        text = (f'已在本地合并上游 {commits} 个提交，但推送失败：{push_reason(detail)}。启动器守卫会保护本地提交，'
                f'请检查网络或令牌后重试' + (dependency if result.missing_module else restart))
    elif status == 'push_failed':           # 上游没有新提交，补推之前留下的本地提交又失败了
        unpushed = f'仍有 {commits} 个本地提交没推送到 fork' if commits else '仍有本地提交没推送到 fork'
        text = f'{unpushed}：{push_reason(detail)}。启动器守卫会保护本地提交，请检查后重试'
    elif status == 'fork_ahead':
        text = (f'fork 上有运行目录还没有的 {commits} 个新提交，已暂停合并：请先在 OASX 重启服务（启动器会更新到 fork 最新），'
                f'再合并上游')
    elif status == 'diverged':
        text = '运行目录和 fork 各有对方没有的提交，已停止，需要人工处理：先把 fork 的 custom 合并进运行目录再推送'
    elif status == 'dirty':
        lines = _lines(detail)
        shown, more = '\n'.join(lines[:LIST_LIMIT]), len(lines) - LIST_LIMIT
        text = f'运行目录有未提交的改动，已跳过合并：{shown}' + (f'\n等 {more} 项' if more > 0 else '') + f'\n{DIRTY_HINT}'
    elif status == 'fetch_failed':
        last = _lines(detail)[-1] if detail else ''
        source = ' fork ' if result.fetch_remote == FORK_REMOTE else '上游'     # 合并前先拉取 fork，失败时说清是哪个
        text = f'拉取{source}失败，未改动任何文件：{last}'
    elif status == 'smoke_failed':
        text = f'合并上游后自检失败，已中止，分支未改动：{detail}'
    else:                       # ERROR_STATUS，以及 fork_git 以后新增、这里还不认识的状态
        text = f'合并上游时出现意外错误，请查看服务日志：{detail or status}'
    if result.server_code_changed and _changed_checkout(result):
        text += SERVER_CODE_NOTE
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


async def run_update_result() -> tuple[MergeResult | None, str]:
    """合并上游的核心，返回 (合并结果, 提示文案)。按钮的 run_update 和每日定时合并都走这里，M4 的通知也用它。

    在工作线程里合并；磁盘上的代码变了就重启在跑的实例（见 _restarts）。任何错误都转成文案，不往外抛：合并没能给出
    结果（比如读不了部署配置）时结果为 None。重启失败（restart_running_instances 自己已经逐个实例兜住错误）不能把已经
    成功的合并报成失败：记日志，文案里如实写重启失败。合并结果里 git 或自检的原文整段写进日志（文案里只有一行摘要）；
    文案本身不在这里记，由调用方各记一次。
    """
    try:
        result = await asyncio.to_thread(_merge)
    except Exception as exc:                    # 比如读不了部署配置；合并本身的意外已经在 execute 里转成结果
        logger.exception('合并上游失败')
        return None, message_of(_failure(exc), [])
    if result.message and result.status != ERROR_STATUS:     # error 的原文 execute 已经连同调用栈记过了
        logger.warning(f'合并上游（{result.status}）的详细说明（git 或自检的原文）：\n{result.message}')
    restarted: list[str] | None = []
    if _restarts(result):
        try:
            restarted = await restart_running_instances()
        except Exception:
            logger.exception('合并上游后重启实例失败')
            restarted = None
    return result, message_of(result, restarted)


async def run_update() -> str:
    """GET /home/execute_update 的实现：run_update_result 的文案，记一次日志后返回。不往外抛。"""
    _, message = await run_update_result()
    logger.info(message)
    return message
