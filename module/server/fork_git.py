"""定制分支合并社区上游的 git 操作层。

每个函数显式接收 GitRunner（git 可执行文件加仓库目录），不碰全局状态，所以能拿临时仓库测试。
上层 ForkUpdater 负责读部署配置、渲染提示文案、接入真实的 assets.py 生成和导入自检；
M3b 的素材页复用 commit_paths 和 push。只用标准库。

合并先做成"待提交"状态（merge --no-commit --no-ff）：在工作区里按顺序处理素材冲突、重新生成 assets.py、
跑调用方传入的自检，全部通过才提交并推送。代码冲突、自检不过、处理途中的任何异常，都会 merge --abort，
分支和工作区回到合并之前，不留合并到一半的仓库。每次 git 调用都不等待输入（GIT_TERMINAL_PROMPT=0、
GIT_EDITOR=true、stdin 接空设备），也从不加 --force。
"""
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

UPSTREAM_REMOTE = 'upstream'
ASSET_SUFFIXES = {'.png', '.jpg', '.jpeg', '.bmp', '.json'}


@dataclass
class GitRunner:
    """某个仓库目录加上要用的 git 可执行文件。"""
    git: str
    repo: Path

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """在仓库目录里执行 git，输出按 UTF-8 解码。check 为真时非零退出码抛 CalledProcessError，stderr 在异常上。"""
        return subprocess.run([self.git, *args], cwd=self.repo, capture_output=True, encoding='utf-8',
                              errors='replace', stdin=subprocess.DEVNULL, check=check,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_EDITOR': 'true'})


@dataclass
class MergeResult:
    """merge_upstream 的结果。

    status：up_to_date / merged / conflict_aborted / push_failed / fetch_failed / dirty / smoke_failed。
    commits 是已并入 HEAD 的上游提交数，只有 merged 和 push_failed 非零。resolved 是素材冲突里取了上游版本的路径，
    conflicts 是让合并中止的代码冲突路径。message 是 git 或自检给出的原文，可能为空，面向用户的固定文案由上层生成。
    requirements_changed 表示合并让 requirements.txt 相对合并前的 HEAD 有了变化。
    """
    status: str
    commits: int
    resolved: list[str]
    conflicts: list[str]
    message: str
    requirements_changed: bool = False


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
    """git push origin HEAD:<branch>，不加 --force。返回 (是否成功, 失败时的 stderr)。
    用 HEAD:<分支> 是因为运行目录的本地分支名可能和 fork 上的不一样。"""
    pushed = r.run('push', 'origin', f'HEAD:{branch}', check=False)
    ok = pushed.returncode == 0
    return ok, '' if ok else pushed.stderr.strip()


def merge_upstream(r: GitRunner, upstream: str, upstream_branch: str, push_branch: str,
                   regenerate: Callable[[str], None],
                   verify: Callable[[Path], tuple[bool, str]] | None = None) -> MergeResult:
    """把上游的 upstream_branch 合并进当前分支并推送到 origin 的 push_branch。

    素材冲突取上游版本，之后对受影响的任务目录调用 regenerate(任务目录)：普通任务是 tasks/ 下的目录名，
    组件是 Component/<组件>，各取各的 assets.py。有代码冲突就整体中止。
    verify 在提交之前收到仓库路径，返回 (是否通过, 原因)，None 表示不做自检。自检不通过，或者 verify、regenerate
    抛异常，或者处理合并结果时 git 命令失败，都 merge --abort 并返回 smoke_failed，message 里是原因。

    不可重入：开头会放弃仓库里已有的合并，同一个仓库同时有两次调用时，后一次会把前一次进行中的合并放弃掉，
    调用方要自己保证同一时刻只有一次。
    """
    _abort_merge(r)                                       # 上次合并途中进程被杀留下的状态
    dirty = r.run('status', '--porcelain', '--untracked-files=no').stdout.rstrip()
    if dirty:                                             # 运行目录里总有未跟踪的文件，只看已跟踪文件的改动
        return MergeResult('dirty', 0, [], [], dirty)
    ensure_remote(r, UPSTREAM_REMOTE, upstream)
    fetched = r.run('fetch', UPSTREAM_REMOTE, upstream_branch, check=False)
    if fetched.returncode != 0:
        return MergeResult('fetch_failed', 0, [], [], fetched.stderr.strip())
    ref = f'{UPSTREAM_REMOTE}/{upstream_branch}'
    behind = count_behind(r, ref)
    if behind == 0:
        return MergeResult('up_to_date', 0, [], [], '')
    result = _merge_and_commit(r, ref, upstream_branch, behind, regenerate, verify)
    if result.status != 'merged':
        return result
    ok, error = push(r, push_branch)
    return result if ok else replace(result, status='push_failed', message=error)


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


def _merge_and_commit(r: GitRunner, ref: str, branch: str, behind: int, regenerate: Callable[[str], None],
                      verify: Callable[[Path], tuple[bool, str]] | None) -> MergeResult:
    """合并、处理素材冲突、自检、提交，返回 merged、conflict_aborted 或 smoke_failed；除 merged 外都已 abort。"""
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
        requirements_changed = r.run('diff', '--cached', '--quiet', 'HEAD', '--', 'requirements.txt',
                                     check=False).returncode == 1
        ok, error = verify(r.repo) if verify else (True, '')
        if ok:
            r.run('commit', '--no-edit', '-m', f'合并上游 {branch} 分支（{behind} 个提交）')
            return MergeResult('merged', behind, resolved, [], '', requirements_changed)
    except Exception as exc:
        error = _describe(exc)
    _abort_merge(r, stage_edits=True)
    return MergeResult('smoke_failed', 0, [], [], error)


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
