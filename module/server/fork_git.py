"""定制分支合并社区上游的 git 操作层。

每个函数显式接收 GitRunner（git 可执行文件加仓库目录），不碰全局状态，所以能拿临时仓库测试。
上层 ForkUpdater 负责读部署配置、渲染提示文案、接入真实的 assets.py 生成和导入自检；
M3b 的素材页复用 commit_paths 和 push。只用标准库。

合并之前先拉取 fork（origin 的部署分支）：fork 上有运行目录还没有的提交（开发克隆部署了新代码、服务还没重启）就不合并，
返回 fork_ahead，两边各有对方没有的提交返回 diverged，免得在旧基点上做出推不上去的本地合并。
合并先做成"待提交"状态（merge --no-commit --no-ff）：在工作区里按顺序处理素材冲突、重新生成 assets.py、
跑调用方传入的自检，全部通过才提交并推送。代码冲突、自检不过、处理途中的任何异常，都会 merge --abort，
分支和工作区回到合并之前，不留合并到一半的仓库。唯一的例外是自检只因为上游新增的第三方依赖还没装而导入失败
（needs_install）：照常提交推送，由用户重启服务时启动器装依赖。每次 git 调用都不等待输入（GIT_TERMINAL_PROMPT=0、
GIT_EDITOR=true、GCM_INTERACTIVE=Never、stdin 接空设备），不弹控制台窗口（Windows 上加 CREATE_NO_WINDOW：
服务由 pythonw 起，没有控制台），也从不加 --force。
联网的命令（fetch、push、ls-remote）另有 NETWORK_TIMEOUT 秒的超时，超时就杀掉 git 并按该步骤失败处理，
免得网络卡死时整个更新流程（以及等着它的服务接口和后台线程）一直挂着。
上游没有新提交时，merge_upstream 还会把留在本地、没推到 fork 的提交补推上去（比如上次推送失败留下的合并提交），
结果是 pushed。进程在合并途中被杀留下的合并状态，下次调用开头会清理，但只清理上游那一侧改动的文件：别的已跟踪文件
被改过（暂存的或没暂存的），本次调用就停下来返回 dirty，一个文件都不动，合并状态原样留着；清理本身没成功也返回
dirty，从不抛异常。
"""
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

UPSTREAM_REMOTE = 'upstream'
FORK_REMOTE = 'origin'
ASSET_SUFFIXES = {'.png', '.jpg', '.jpeg', '.bmp', '.json'}
NETWORK_TIMEOUT = 300       # 秒，联网命令的超时
NETWORK_COMMANDS = frozenset({'fetch', 'push', 'ls-remote'})
_REAP_TIMEOUT = 10          # 秒，杀掉进程后等它收尾的上限
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0   # 子进程的 creationflags：不弹控制台窗口
_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")


def _git_env() -> dict[str, str]:
    """GIT_TERMINAL_PROMPT 和 GCM_INTERACTIVE 分别关掉终端里和 Git Credential Manager 弹窗里的输入，
    取不到凭据时 git 直接失败，不会停下来等人。"""
    return {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_EDITOR': 'true', 'GCM_INTERACTIVE': 'Never'}


def _kill_tree(process: subprocess.Popen) -> None:
    """杀掉 git 和它起的帮手进程（git-remote-https 等）。Windows 上只杀 git 本身的话，帮手会留在网络等待里，
    所以带 /T 杀整棵树；其他系统只杀 git 本身。"""
    if os.name == 'nt' and process.poll() is None:      # 已经退出的进程，PID 可能已被别的进程占用，不能按 PID 去杀
        try:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=_REAP_TIMEOUT, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    process.kill()
    try:
        process.wait(timeout=_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        pass


def _timed_out(command: str) -> str:
    return f'git {command} timed out after {NETWORK_TIMEOUT}s'


@dataclass
class GitRunner:
    """某个仓库目录加上要用的 git 可执行文件。"""
    git: str
    repo: Path

    def _env(self) -> dict[str, str]:
        """子进程的环境变量：_git_env 加上 git 所在的目录。工具链里的 git 把 git-remote-https 这些帮手放在自己旁边
        （toolkit/Git/mingw64/bin），PATH 里没有这个目录时 fetch、push 报 "'remote-https' is not a git command"。
        git 是不带目录的命令名时交给 PATH，不动它。相对路径按服务进程的当前目录算成绝对路径，不依赖子进程的工作目录。"""
        env = _git_env()
        directory = os.path.dirname(self.git)
        if directory:
            env['PATH'] = os.pathsep.join(filter(None, [os.path.abspath(directory), env.get('PATH')]))
        return env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """在仓库目录里执行 git，输出按 UTF-8 解码。check 为真时非零退出码抛 CalledProcessError，stderr 在异常上。

        第一个参数是 NETWORK_COMMANDS 里的联网命令时带 NETWORK_TIMEOUT 的超时：超时后 git 连同帮手进程被杀掉，
        抛 subprocess.TimeoutExpired（check 为假也一样，调用方要自己处理）。其余命令没有超时。
        """
        command = [self.git, *args]
        if args[:1] and args[0] in NETWORK_COMMANDS:
            return self._run_with_timeout(command, check)
        return subprocess.run(command, cwd=self.repo, capture_output=True, encoding='utf-8', errors='replace',
                              stdin=subprocess.DEVNULL, check=check, env=self._env(), creationflags=NO_WINDOW)

    def _run_with_timeout(self, command: list[str], check: bool) -> subprocess.CompletedProcess:
        """输出写临时文件而不是管道：git-remote-https 这类帮手进程会继承管道，卡住时即使杀了 git，
        读管道的一端也要等到帮手退出才返回（在本机实测 subprocess.run(timeout=3) 对卡死的 git fetch 阻塞了 25 秒以上），
        超时就形同虚设。写文件没有这个依赖，wait 到点就返回。"""
        with tempfile.TemporaryFile('w+', encoding='utf-8', errors='replace') as out, \
                tempfile.TemporaryFile('w+', encoding='utf-8', errors='replace') as err:
            process = subprocess.Popen(command, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=self._env(), creationflags=NO_WINDOW)
            try:
                process.wait(timeout=NETWORK_TIMEOUT)
            except BaseException:       # 超时，或者被 Ctrl+C 打断
                _kill_tree(process)
                raise
            out.seek(0)
            err.seek(0)
            result = subprocess.CompletedProcess(command, process.returncode, out.read(), err.read())
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result


@dataclass
class MergeResult:
    """merge_upstream 的结果。

    status：up_to_date / merged / needs_install / pushed / conflict_aborted / push_failed / fetch_failed / dirty /
    smoke_failed / fork_ahead / diverged。
    merged 是并入了上游的新提交并且推送成功；needs_install 也一样提交推送了，只是自检因为上游新增的第三方依赖还没装
    而导入失败（缺的模块名在 missing_module）；pushed 是上游没有新提交，只是把之前没推上去的本地提交补推成功了；
    fork_ahead 是 fork 上有 HEAD 没有的提交、HEAD 没有 fork 之外的提交（等启动器对齐，不合并）；diverged 是两边各有
    对方没有的提交（不合并也不推送，要人处理）；fetch_failed 是拉取 fork 或上游失败（fetch_remote 说是哪个）；
    dirty 是已跟踪文件有未提交的改动，或者上次留下的合并没清理成（上游改动范围之外的文件被改过，或者放弃不了；
    message 里写明合并和有改动的文件）。
    commits：merged、needs_install 和紧跟在合并之后推送失败的 push_failed 里，是已并入 HEAD 的上游提交数；pushed 和
    补推失败的 push_failed 里，是 HEAD 领先 origin/<分支> 的本地提交数（origin/<分支> 不存在时记 0）；fork_ahead 里
    是 fork 多出的提交数；其余状态为 0。
    resolved 是素材冲突里取了上游版本的路径，conflicts 是让合并中止的代码冲突路径。
    message 是 git 或自检给出的原文，可能为空，面向用户的文案由上层生成。
    requirements_changed 表示 requirements.txt 有了变化，server_code_changed 表示服务进程启动时就加载的代码有了变化
    （见 is_server_code_path）：merged、needs_install 和合并之后推送失败的 push_failed 比的是合并前的 HEAD；
    pushed 和补推失败的 push_failed 比的是推送前的 origin/<分支>，该引用不存在时都记 False。
    merged_locally 表示这次调用造出了合并提交（merged、needs_install，以及它们推送失败变成的 push_failed）：
    磁盘上的代码变了，与"补推失败、这次什么都没合并"的 push_failed 区分开。
    """
    status: str
    commits: int
    resolved: list[str]
    conflicts: list[str]
    message: str
    requirements_changed: bool = False
    merged_locally: bool = False
    server_code_changed: bool = False
    missing_module: str = ''
    fetch_remote: str = ''          # fetch_failed 时拉取失败的远端：origin（fork）或 upstream（上游）


def ensure_remote(r: GitRunner, name: str, url: str) -> None:
    """让远端 name 存在并指向 url；已经是这个地址就不动。"""
    current = r.run('config', '--get', f'remote.{name}.url', check=False)
    if current.returncode != 0:
        r.run('remote', 'add', name, url)
    elif current.stdout.strip() != url:
        r.run('remote', 'set-url', name, url)


def count_behind(r: GitRunner, upstream_ref: str) -> int:
    """upstream_ref 比 HEAD 多出的提交数。"""
    return int(r.run('rev-list', '--count', f'HEAD..{upstream_ref}').stdout.strip())


def upstream_commits(r: GitRunner, upstream_ref: str, n: int = 15) -> list[tuple[str, str, str, str]]:
    """HEAD..upstream_ref 里最新的 n 个提交，新的在前，每项是 (sha, 作者, ISO 日期, 标题)。"""
    log = r.run('log', '--format=%H%x1f%an%x1f%aI%x1f%s', '-n', str(n), f'HEAD..{upstream_ref}').stdout
    return [tuple(line.split('\x1f', 3)) for line in log.split('\n') if line]


def is_asset_path(path: str) -> bool:
    """素材文件：tasks/ 下的图片和 json，以及从它们生成的 assets.py。"""
    if not path.startswith('tasks/'):
        return False
    pure = PurePosixPath(path)
    return pure.name == 'assets.py' or pure.suffix.lower() in ASSET_SUFFIXES


def is_server_code_path(path: str) -> bool:
    """服务进程启动时就加载、改了要重启服务才完整生效的文件：server.py、module/ 和 deploy/ 下的一切，
    以及 tasks/ 下（任意深度）的 config*.py（配置模型从它们组装）。"""
    if path == 'server.py' or path.startswith(('module/', 'deploy/')):
        return True
    return path.startswith('tasks/') and PurePosixPath(path).match('config*.py')


def commit_paths(r: GitRunner, paths: list[str], message: str) -> str | None:
    """只提交给定的路径（含删除），返回新提交的 sha；这些路径没有变化时返回 None。
    索引里别处已暂存的内容不会被带进这次提交。"""
    if not paths:
        return None          # 空列表不能交给 git add -A，那会暂存整个工作区
    r.run('add', '-A', '--', *paths)
    if r.run('diff', '--cached', '--quiet', 'HEAD', '--', *paths, check=False).returncode == 0:
        return None
    r.run('commit', '-m', message, '--', *paths)
    return r.run('rev-parse', 'HEAD').stdout.strip()


def push(r: GitRunner, branch: str) -> tuple[bool, str]:
    """git push origin HEAD:<branch>，不加 --force。返回 (是否成功, 失败时的 stderr；超时则是超时说明)。
    用 HEAD:<分支> 是因为运行目录的本地分支名可能和 fork 上的不一样。"""
    try:
        pushed = r.run('push', 'origin', f'HEAD:{branch}', check=False)
    except subprocess.TimeoutExpired:
        return False, _timed_out('push')
    ok = pushed.returncode == 0
    return ok, '' if ok else pushed.stderr.strip()


def merge_upstream(r: GitRunner, upstream: str, upstream_branch: str, push_branch: str,
                   regenerate: Callable[[str], None],
                   verify: Callable[[Path], tuple[bool, str]] | None = None) -> MergeResult:
    """把上游的 upstream_branch 合并进当前分支并推送到 origin 的 push_branch。

    顺序：清理上次留下的合并 → 查已跟踪文件的改动 → 拉取 fork 并比较 → 拉取上游 → 合并或补推。
    合并之前先 fetch origin <push_branch>（NETWORK_TIMEOUT 内）：失败返回 fetch_failed（fetch_remote 为 origin，
    仓库没动过）。fork 上有 HEAD 没有的提交时不合并：HEAD 没有 fork 之外的提交是 fork_ahead（commits 是 fork 多出的
    提交数，等用户重启服务、启动器把运行目录对齐到 fork），两边各有对方没有的提交是 diverged（不合并也不推送）。
    素材冲突取上游版本，之后对受影响的任务目录调用 regenerate(任务目录)：普通任务是 tasks/ 下的目录名，
    组件是 Component/<组件>，各取各的 assets.py。有代码冲突就整体中止。
    verify 在提交之前收到仓库路径，返回 (是否通过, 原因)，None 表示不做自检。自检不通过，或者 verify、regenerate
    抛异常，或者处理合并结果时 git 命令失败，都 merge --abort 并返回 smoke_failed，message 里是原因。例外：
    原因里有 ModuleNotFoundError: No module named 'X'、X 不是仓库自己的包或模块、这次合并又改了 requirements.txt，
    就当作上游新增了第三方依赖，照常提交推送，返回 needs_install（missing_module 为 X）。
    fetch 或 push 超过 NETWORK_TIMEOUT 秒没完成：fetch 返回 fetch_failed（仓库没动过），
    push 返回 push_failed（本地合并保留），message 都是 "git <命令> timed out after <秒数>s"。
    上游没有新提交时看刚拉取的 origin/<push_branch>：HEAD 领先它（或者它不存在）就补推，成功返回 pushed，失败返回
    push_failed，所以合并成功、推送失败之后再调用一次就能把本地合并推上去；不领先返回 up_to_date，不推送。
    开头清理上次留下的合并，只动上游那一侧改动的文件，别的文件有改动就返回 dirty，见 _abort_leftover_merge。

    不可重入，调用方要保证同一个仓库同一时刻只有一次调用（ForkUpdater 用 MERGE_LOCK）：开头把仓库里已有的合并
    当作上次被杀留下的残留来处理，同时进行的另一次调用的合并也会被当成残留——只在上游改动的文件上被放弃掉，
    别的文件有改动时本次返回 dirty。
    """
    stuck = _abort_leftover_merge(r)                      # 上次合并途中进程被杀留下的状态
    if stuck:
        return MergeResult('dirty', 0, [], [], stuck)
    # 运行目录里总有未跟踪的文件，只看已跟踪文件的改动；core.quotepath=off 让中文路径在说明里照原样显示
    dirty = r.run('-c', 'core.quotepath=off', 'status', '--porcelain', '--untracked-files=no').stdout.rstrip()
    if dirty:
        return MergeResult('dirty', 0, [], [], dirty)
    failed = _fetch(r, FORK_REMOTE, push_branch)
    if failed:
        return failed
    fork = _fork_state(r, push_branch)
    if fork:
        return fork
    ensure_remote(r, UPSTREAM_REMOTE, upstream)
    failed = _fetch(r, UPSTREAM_REMOTE, upstream_branch)
    if failed:
        return failed
    ref = f'{UPSTREAM_REMOTE}/{upstream_branch}'
    behind = count_behind(r, ref)
    if behind == 0:
        return _push_unpushed_commits(r, push_branch)
    result = _merge_and_commit(r, ref, upstream_branch, behind, regenerate, verify)
    if not result.merged_locally:
        return result
    ok, error = push(r, push_branch)
    return result if ok else replace(result, status='push_failed', message=error)


def _fetch(r: GitRunner, remote: str, branch: str) -> MergeResult | None:
    """git fetch <remote> <branch>（顺带更新 <remote>/<branch>）。成功返回 None，失败或超时返回 fetch_failed。"""
    try:
        fetched = r.run('fetch', remote, branch, check=False)
    except subprocess.TimeoutExpired:
        return MergeResult('fetch_failed', 0, [], [], _timed_out('fetch'), fetch_remote=remote)
    if fetched.returncode != 0:
        return MergeResult('fetch_failed', 0, [], [], fetched.stderr.strip(), fetch_remote=remote)
    return None


def _ref_exists(r: GitRunner, ref: str) -> bool:
    return r.run('rev-parse', '-q', '--verify', ref, check=False).returncode == 0


def _count(r: GitRunner, revisions: str) -> int:
    """rev-list --count 的结果，比如 _count(r, 'A..B') 是 B 有、A 没有的提交数。"""
    return int(r.run('rev-list', '--count', revisions).stdout.strip())


def _fork_state(r: GitRunner, push_branch: str) -> MergeResult | None:
    """刚拉取的 fork（origin/<push_branch>）有 HEAD 没有的提交时返回 fork_ahead 或 diverged，否则返回 None。

    fork 领先、HEAD 没有 fork 之外的提交（HEAD 是 origin/<push_branch> 的祖先）：fork_ahead，commits 是 fork 多出的
    提交数。这时合并会落在旧基点上、推送必然被拒；启动器守卫看不到只在本地的提交，用户重启服务就能对齐到 fork。
    两边各有对方没有的提交：diverged，要人把 fork 的分支合并进来再推送。
    origin/<push_branch> 不存在（远端的取回规则不覆盖这个分支，拉取只写了 FETCH_HEAD）时比不了，返回 None。
    """
    tracked = f'{FORK_REMOTE}/{push_branch}'
    if not _ref_exists(r, tracked):
        return None
    fork_only = _count(r, f'HEAD..{tracked}')
    if fork_only == 0:
        return None
    local_only = _count(r, f'{tracked}..HEAD')
    if local_only == 0:
        return MergeResult('fork_ahead', fork_only, [], [], f'{tracked} has {fork_only} commit(s) that HEAD does not')
    return MergeResult('diverged', 0, [], [], f'HEAD and {tracked} have diverged: {local_only} commit(s) only here, '
                                              f'{fork_only} only on the fork')


def _push_unpushed_commits(r: GitRunner, push_branch: str) -> MergeResult:
    """上游没有新提交时的收尾：把还没推到 fork 的本地提交推上去，比如上次合并成功、推送失败，合并提交一直留在本地。

    调用前刚拉取过 fork，并且 fork 不领先 HEAD（_fork_state）。HEAD 领先 origin/<push_branch>，或者这个引用不存在
    就推送：成功是 pushed，失败是 push_failed（commits 都是领先的提交数），不领先是 up_to_date，这时不推送。
    引用不存在时数不出提交、也比不了文件，commits 记 0、requirements_changed 和 server_code_changed 记 False。
    领先数和文件的比较都要在推送之前做：推送成功后 git 会把 origin/<push_branch> 挪到 HEAD，之后什么差别都比不出来。
    列不出有差别的文件时两个标志都记 False：它们只影响提示，不挡着推送。
    """
    tracked = f'{FORK_REMOTE}/{push_branch}'
    known = _ref_exists(r, tracked)
    ahead = _count(r, f'{tracked}..HEAD') if known else 0
    if known and ahead == 0:
        return MergeResult('up_to_date', 0, [], [], '')
    changed = (_changed_paths(r, tracked, 'HEAD') or []) if known else []
    flags = {'requirements_changed': 'requirements.txt' in changed,
             'server_code_changed': any(is_server_code_path(path) for path in changed)}
    ok, error = push(r, push_branch)
    if not ok:
        return MergeResult('push_failed', ahead, [], [], error, **flags)
    return MergeResult('pushed', ahead, [], [], '', **flags)


def _abort_merge(r: GitRunner, *, stage_edits: bool = False) -> None:
    """放弃进行中的合并；没有合并在进行就什么也不做。

    stage_edits 用于合并结果上又被改过文件的场合（assets.py 写到一半、自检动了文件）：merge --abort 遇到
    "合并涉及、又有未暂存改动"的文件会拒绝并留下合并状态，所以先把改动登记进索引。合并开始前工作区是干净的，
    这时登记的只会是合并和处理过程自己造成的改动，之后马上被 abort 丢弃。
    """
    if r.run('rev-parse', '-q', '--verify', 'MERGE_HEAD', check=False).returncode != 0:
        return
    if stage_edits:
        r.run('add', '-u')
    r.run('merge', '--abort')


def _abort_leftover_merge(r: GitRunner) -> str | None:
    """清理上次进程被杀时留下的合并。清理成功（或者本来就没有合并）返回 None；没清理成就返回一段说明，调用方据此
    返回 dirty。不抛异常。

    保证：中断的合并只在上游那一侧改动的文件上被清理——合并基点到 MERGE_HEAD 的差异，加上这些路径所属任务目录的
    assets.py（regenerate 生成的那份）。已跟踪文件里只要有别的文件被改过，不管暂存没暂存、这个合并能不能直接
    abort，本次调用都停下来返回 dirty：一次 abort 都不试，什么都不丢弃，合并状态原样留着，交给人处理。
    范围不能取 HEAD 与 MERGE_HEAD 的差异：那里面还有只有 fork 改过的文件，人在这些文件上的改动会被 abort
    悄悄还原成 HEAD 的内容。上游改动范围之内的文件，人手改的和本工具留下的分不开，清理时一并丢掉。

    典型的卡住：regenerate 写完 assets.py、还没 git add 进程就被杀，merge --abort 遇到"合并涉及、又有未暂存改动"
    的文件会拒绝（Entry ... not uptodate），之后每次调用都卡在这里。这时有改动的文件都在上述范围里，就把其中有
    未暂存改动的文件逐个 git add，重试一次 abort；重试还不行也返回 dirty。
    """
    merge_head = r.run('rev-parse', '-q', '--verify', 'MERGE_HEAD', check=False)
    if merge_head.returncode != 0:
        return None
    sha = merge_head.stdout.strip()[:7]

    def left(reason: str) -> str:
        return f'leftover merge {sha} was left as it is: {reason}'

    def failed(error: str, detail: str) -> str:
        return f'leftover merge {sha} could not be aborted: {_one_line(error)} ({detail})'

    merge_base = r.run('merge-base', 'HEAD', 'MERGE_HEAD', check=False)
    base = merge_base.stdout.strip() if merge_base.returncode == 0 else ''
    upstream_changes = _changed_paths(r, base, 'MERGE_HEAD') if base else None
    if upstream_changes is None:
        return left('could not list the files the merge touches')
    units = {_task_dir(path) for path in upstream_changes if is_asset_path(path)} - {None}
    allowed = set(upstream_changes) | {f'tasks/{unit}/assets.py' for unit in units}
    unstaged = _changed_paths(r)
    staged = _changed_paths(r, '--cached')
    if unstaged is None or staged is None:
        return left('could not list the modified files')
    outside = sorted((set(unstaged) | set(staged)) - allowed)
    if outside:
        return left(f'modified files outside the upstream changes: {", ".join(outside)}')

    aborted = r.run('merge', '--abort', check=False)
    if aborted.returncode == 0:
        return None
    if unstaged:
        added = r.run('add', '--', *sorted(unstaged), check=False)
        if added.returncode != 0:
            return failed(aborted.stderr, f'git add failed: {_one_line(added.stderr)}')
    retried = r.run('merge', '--abort', check=False)
    if retried.returncode != 0:
        return failed(retried.stderr, f'retried after staging: {", ".join(sorted(unstaged)) or "nothing to stage"}')
    return None


def _changed_paths(r: GitRunner, *diff_args: str) -> list[str] | None:
    """git diff 列出的有差异的路径，命令失败返回 None。-z 让中文路径不被转义，--no-renames 让改名的两端都列出来，
    这样几份清单之间才能直接按路径比较。"""
    listed = r.run('diff', '--no-renames', '--name-only', '-z', *diff_args, check=False)
    if listed.returncode != 0:
        return None
    return [path for path in listed.stdout.split('\0') if path]


def _one_line(text: str) -> str:
    """多行的 git 报错压成一行，好放进 message。"""
    return '; '.join(line.strip() for line in text.splitlines() if line.strip())


def _merge_and_commit(r: GitRunner, ref: str, branch: str, behind: int, regenerate: Callable[[str], None],
                      verify: Callable[[Path], tuple[bool, str]] | None) -> MergeResult:
    """合并、处理素材冲突、自检、提交，返回 merged、needs_install、conflict_aborted 或 smoke_failed。
    前两种已经提交（merged_locally 为真），其余都已 abort。requirements.txt 和服务端代码有没有变化在自检之前就比好：
    自检失败时要靠前者判断是不是上游新增了依赖。"""
    merge = r.run('merge', '--no-commit', '--no-ff', ref, check=False)
    try:
        unmerged = _unmerged_paths(r)
        code = sorted(path for path in unmerged if not is_asset_path(path))
        if code or (merge.returncode != 0 and not unmerged):
            # 有代码冲突，或合并根本没开始（比如有未跟踪文件会被覆盖）
            _abort_merge(r)
            return MergeResult('conflict_aborted', 0, [], code, merge.stderr.strip())
        resolved = _take_upstream(r, unmerged)
        _regenerate(r, resolved, regenerate)
        changed = _changed_paths(r, '--cached', 'HEAD')          # 这次合并相对合并前的 HEAD 改了哪些文件
        if changed is None:
            raise RuntimeError('git diff could not list the files the merge changes')
        requirements_changed = 'requirements.txt' in changed
        server_code_changed = any(is_server_code_path(path) for path in changed)
        ok, error = verify(r.repo) if verify else (True, '')
        missing = '' if ok or not requirements_changed else _missing_dependency(r.repo, error)
        if ok or missing:
            r.run('commit', '--no-edit', '-m', f'合并上游 {branch} 分支（{behind} 个提交）')
            return MergeResult('merged' if ok else 'needs_install', behind, resolved, [], error, requirements_changed,
                               merged_locally=True, server_code_changed=server_code_changed, missing_module=missing)
    except Exception as exc:
        error = _describe(exc)
    _abort_merge(r, stage_edits=True)
    return MergeResult('smoke_failed', 0, [], [], error)


def _missing_dependency(repo: Path, reason: str) -> str:
    """自检失败的原因如果是缺了第三方模块，返回模块名，否则返回空串。

    看原因里最后一处 ModuleNotFoundError: No module named 'X'：X 的顶层名是仓库根目录下的目录或 .py 文件时，缺的是
    仓库自己的东西（上游改名、删文件），装依赖解决不了，返回空串，合并照常中止。"""
    found = _MISSING_MODULE.findall(reason)
    if not found:
        return ''
    name = found[-1]
    top = name.split('.')[0]
    if not top or (repo / top).is_dir() or (repo / f'{top}.py').is_file():
        return ''
    return name


def _unmerged_paths(r: GitRunner) -> dict[str, set[str]]:
    """未合并的路径，对应它在索引里还有哪几个阶段：1 共同祖先、2 我们、3 上游。"""
    paths: dict[str, set[str]] = {}
    for entry in r.run('ls-files', '-u', '-z').stdout.split('\0'):
        if entry:
            meta, _, path = entry.partition('\t')
            paths.setdefault(path, set()).add(meta.split()[2])
    return paths


def _take_upstream(r: GitRunner, unmerged: dict[str, set[str]]) -> list[str]:
    """冲突的素材取上游：上游改过的取上游那份，上游删掉的跟着删。返回处理过的路径，按路径排序。"""
    for path, stages in unmerged.items():
        if '3' in stages:
            r.run('checkout', '--theirs', '--', path)
            r.run('add', '--', path)
        else:
            r.run('rm', '-q', '--', path)
    return sorted(unmerged)


def _task_dir(path: str) -> str | None:
    """素材文件所属的任务目录（相对 tasks/），也就是放它 assets.py 的目录：tasks/<任务>，组件是
    tasks/Component/<组件>。直接放在 tasks/ 或 tasks/Component/ 下的文件不属于任何任务，返回 None。
    path 必须是 is_asset_path 认定的素材路径。"""
    parts = path.split('/')
    depth = 3 if parts[1] == 'Component' else 2
    return '/'.join(parts[1:depth]) if len(parts) > depth else None


def _regenerate(r: GitRunner, resolved: list[str], regenerate: Callable[[str], None]) -> None:
    """每个受影响的任务目录重新生成一次 assets.py 并加进索引。
    合并结果里这个目录已经没有文件（上游把整个任务删了）就跳过，免得生成一个空壳 assets.py。"""
    for task in sorted({_task_dir(path) for path in resolved} - {None}):
        if not r.run('ls-files', '--', f'tasks/{task}').stdout.strip():
            continue
        regenerate(task)
        assets = f'tasks/{task}/assets.py'
        if (r.repo / assets).is_file():
            r.run('add', '--', assets)


def _describe(exc: Exception) -> str:
    """异常的可读说明：git 命令失败时优先用它的 stderr。"""
    stderr = getattr(exc, 'stderr', None)
    return (stderr.strip() if isinstance(stderr, str) else '') or f'{type(exc).__name__}: {exc}'
