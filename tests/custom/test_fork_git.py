"""合并上游的 git 操作层。

全部在 tmp_path 下的本地裸仓库里跑：不联网，不读写本仓库的远端、config/ 和 log/。
每个临时仓库都设了本地身份并关掉 autocrlf 和提交签名，结果不依赖本机的全局 git 配置。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from module.server import fork_git
from module.server.fork_git import (NETWORK_TIMEOUT, GitRunner, commit_paths, count_behind, ensure_remote,
                                    is_asset_path, is_server_code_path, merge_upstream, push, upstream_commits)
from module.server.upstream_hooks import Hook, OverriddenBody

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
STATUS_CALL = ('-c', 'core.quotepath=off', 'status', '--porcelain', '--untracked-files=no')

BASE = {
    'script.py': 'base\n',
    'other.py': 'base-other\n',          # 之后两边都不改的一个文件
    'requirements.txt': 'pkg==1\n',
    'tasks/Foo/as/a.png': b'\x89PNG\x00base',
    'tasks/Foo/as/image.json': '[{"v": 0}]\n',
    'tasks/Foo/as/斗町.png': b'\x89PNG\x00base-cn',
    'tasks/Foo/assets.py': '# generated v0\n',
    'tasks/Component/Buy/buy/image.json': '[{"v": 0}]\n',
    'tasks/Component/Buy/assets.py': '# generated v0\n',
}


def sh(cwd, *args):
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, encoding='utf-8')
    assert done.returncode == 0, f'git {" ".join(args)} 失败：{done.stderr}'
    return done.stdout.strip()


def write(repo, files):
    """内容为 None 表示删除该文件。一律按字节写，免得换行符被改。"""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))


def commit(repo, files, message):
    write(repo, files)
    sh(repo, 'add', '-A')
    sh(repo, 'commit', '-q', '-m', message)


def configure(repo):
    for key, value in (('user.name', 'tester'), ('user.email', 'tester@example.com'),
                       ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')):
        sh(repo, 'config', key, value)


def merging(repo):
    return (repo / '.git' / 'MERGE_HEAD').exists()


def is_clean(repo):
    return sh(repo, 'status', '--porcelain', '--untracked-files=no') == ''


def parents(repo):
    """HEAD 的父提交（合并提交有两个）。"""
    return sh(repo, 'rev-list', '--parents', '-n', '1', 'HEAD').split()[1:]


def wait_until(predicate, timeout=5.0):
    """有限时长地等 predicate 成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def build_trio(root):
    """建好三个仓库：upstream.git（dev）和 fork.git（custom）两个裸仓库，从 fork 克隆、挂了 upstream 远端的 work
    （被测的就是它），以及 upstream.git 的另一份克隆 seed（专门用来往上游推新提交）。
    --template= 不拷示例钩子，仓库里文件少，整份拷贝快。"""
    upstream, fork, seed, work = (root / name for name in ('upstream.git', 'fork.git', 'seed', 'work'))
    for bare, branch in ((upstream, 'dev'), (fork, 'custom')):
        sh(root, 'init', '-q', '--bare', '--template=', str(bare))
        sh(bare, 'symbolic-ref', 'HEAD', f'refs/heads/{branch}')
    sh(root, 'init', '-q', '--template=', str(seed))
    configure(seed)
    sh(seed, 'checkout', '-q', '-b', 'dev')
    commit(seed, BASE, 'base')
    sh(seed, 'push', '-q', str(upstream), 'HEAD:refs/heads/dev')
    sh(seed, 'push', '-q', str(fork), 'HEAD:refs/heads/custom')
    sh(root, 'clone', '-q', '--template=', '-c', 'core.autocrlf=false', str(fork), str(work))
    configure(work)
    sh(work, 'remote', 'add', 'upstream', str(upstream))


class Trio:
    """build_trio 建出的四个仓库，加上测试常用的操作。"""

    def __init__(self, root):
        self.root = root
        self.upstream, self.fork, self.seed, self.work = (root / name for name in
                                                          ('upstream.git', 'fork.git', 'seed', 'work'))
        self.runner = GitRunner('git', self.work)

    @property
    def head(self):
        return sh(self.work, 'rev-parse', 'HEAD')

    @property
    def fork_tip(self):
        return sh(self.fork, 'rev-parse', 'refs/heads/custom')

    def upstream_commit(self, files, message='upstream change'):
        commit(self.seed, files, message)
        sh(self.seed, 'push', '-q', str(self.upstream), 'HEAD:refs/heads/dev')

    def fork_commit(self, files, message='fork change'):
        commit(self.work, files, message)

    def merge(self, regenerate=None, verify=None, upstream=None, hooks=None):
        return merge_upstream(self.runner, upstream or str(self.upstream), 'dev', 'custom',
                              regenerate or Regen(self.work), verify, hooks)


class Regen:
    """假的 regenerate：记下被调用的任务名，并像真的一样重写 tasks/<任务>/assets.py。"""

    def __init__(self, repo):
        self.repo, self.calls = repo, []

    def __call__(self, task):
        self.calls.append(task)
        write(self.repo, {f'tasks/{task}/assets.py': f'# regenerated {task}\n'})


class Killed(BaseException):
    """模拟进程在这一步被杀。BaseException 不会被 merge_upstream 里的 except Exception 接住，合并状态原样留在仓库里。"""


def abort_fails(repo):
    """裸 git merge --abort 此刻会失败，并且失败后合并状态原封不动：残留状态的前提。"""
    aborted = subprocess.run(['git', 'merge', '--abort'], cwd=repo, capture_output=True, encoding='utf-8')
    return aborted.returncode != 0 and merging(repo)


def leave_half_regenerated_merge(trio, fork_files=None):
    """评审重现的状态：fork 和上游都改了 image.json（素材冲突），上游还改了 assets.py 和一张中文名的图（都干净地
    并进了索引）。素材冲突取上游之后 regenerate 写完 assets.py，进程在 git add 之前被杀：assets.py 在索引里是
    上游的版本，工作区里是写出来的版本，merge --abort 因此拒绝。fork_files 是 fork 顺手在同一个提交里改的别的文件，
    上游没改它们。"""
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 2}]\n', **(fork_files or {})})
    trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n', 'tasks/Foo/assets.py': '# upstream assets\n',
                          'tasks/Foo/as/斗町.png': b'\x89PNG\x00up-cn'})

    def killed(task):
        write(trio.work, {'tasks/Foo/assets.py': '# half written'})
        raise Killed

    with pytest.raises(Killed):
        trio.merge(killed)
    assert abort_fails(trio.work)


def leave_plain_merge(trio, fork_files=None):
    """进程在 merge --no-commit 之后、提交之前被杀：干净的合并，裸 git merge --abort 此刻不会被拒绝。
    fork_files 同上：fork 改了、上游没改的文件。"""
    if fork_files:
        trio.fork_commit(fork_files)
    trio.upstream_commit({'new.txt': 'hello\n'})
    sh(trio.work, 'fetch', '-q', 'upstream', 'dev')
    subprocess.run(['git', 'merge', '--no-commit', '--no-ff', 'upstream/dev'], cwd=trio.work, capture_output=True)
    assert merging(trio.work)


class StalledGit:
    """假的卡死的 git 进程：wait 一调用就按超时处理，不真等。"""
    pid = 0           # 测试里 _kill_tree 被换成了记录器，不会真去杀进程

    def __init__(self, command, **kwargs):
        self.args, self.kwargs, self.wait_timeouts, self.killed = command, kwargs, [], False

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(self.args, timeout)

    def poll(self):
        return None   # 还活着

    def kill(self):
        self.killed = True


@pytest.fixture
def stall(monkeypatch):
    """stall('fetch', 'push')：这些联网命令换成卡死的假进程，其余命令照常真跑。给了 remote 就只换发往这个远端的
    （命令的第一个参数，比如 stall('fetch', remote='upstream') 只卡拉取上游）。杀进程树的清理换成记录。"""
    def install(*commands, remote=None):
        processes, killed = [], []
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command[1] in commands and (remote is None or command[2:3] == [remote]):
                processes.append(StalledGit(command, **kwargs))
                return processes[-1]
            return real_popen(command, **kwargs)

        monkeypatch.setattr(subprocess, 'Popen', popen)
        monkeypatch.setattr(fork_git, '_kill_tree', killed.append)
        return SimpleNamespace(processes=processes, killed=killed)

    return install


@pytest.fixture
def git_calls(monkeypatch):
    """GitRunner.run 之后每次调用的参数（元组），调用照常真跑。"""
    calls, real = [], GitRunner.run

    def spy(self, *args, check=True):
        calls.append(args)
        return real(self, *args, check=check)

    monkeypatch.setattr(GitRunner, 'run', spy)
    return calls


@pytest.fixture
def broken_git(monkeypatch):
    """broken_git('merge', '--abort')：以这串参数开头的 git 命令一律失败（退出码 128），其余照常真跑。
    返回被拦下的调用。"""
    def install(*prefix, stderr='fatal: simulated failure'):
        blocked, real = [], GitRunner.run

        def run(self, *args, check=True):
            if args[:len(prefix)] != prefix:
                return real(self, *args, check=check)
            blocked.append(args)
            if check:
                raise subprocess.CalledProcessError(128, [self.git, *args], '', stderr)
            return subprocess.CompletedProcess([self.git, *args], 128, '', stderr)

        monkeypatch.setattr(GitRunner, 'run', run)
        return blocked

    return install


def push_fails(trio, tmp_path):
    """推送失败、拉取照常：只把 origin 的推送地址（pushurl）指到不存在的仓库。合并前要先拉取 fork，
    所以不能像以前那样把整个 origin 地址改坏。"""
    sh(trio.work, 'remote', 'set-url', '--push', 'origin', str(tmp_path / 'missing.git'))


def push_recovers(trio):
    sh(trio.work, 'config', '--unset', 'remote.origin.pushurl')


def missing_module(name):
    """假的自检：导入时缺模块 name，输出与默认自检带回的 stderr 末尾同样格式。"""
    return lambda repo: (False, 'Traceback (most recent call last):\n  File "<string>", line 1, in <module>\n'
                                f"ModuleNotFoundError: No module named '{name}'")


@pytest.fixture(scope='module')
def trio_template(tmp_path_factory):
    root = tmp_path_factory.mktemp('trio-template')
    build_trio(root)
    return root


@pytest.fixture
def trio(trio_template, tmp_path):
    """每个用例一份拷贝：新建这几个仓库要十几个 git 进程，拷目录快得多。仓库配置里的远端地址要改到拷贝上。"""
    root = tmp_path / 'trio'
    shutil.copytree(trio_template, root)
    copy = Trio(root)
    sh(copy.work, 'remote', 'set-url', 'origin', str(copy.fork))
    sh(copy.work, 'remote', 'set-url', 'upstream', str(copy.upstream))
    return copy


def test_up_to_date(trio):
    before = trio.head
    result = trio.merge()
    assert (result.status, result.commits, result.merged_locally) == ('up_to_date', 0, False)
    assert trio.head == before


def test_clean_merge_pushes(trio):
    trio.upstream_commit({'new.txt': 'hello\n'})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert (result.status, result.commits, result.merged_locally) == ('merged', 1, True)
    assert (result.resolved, result.conflicts, result.requirements_changed) == ([], [], False)
    assert (result.server_code_changed, result.missing_module, result.fetch_remote) == (False, '', '')
    assert regen.calls == []
    assert trio.fork_tip == trio.head
    assert (trio.work / 'new.txt').read_bytes() == b'hello\n'
    # 即使能快进也留一个合并提交（--no-ff）：提交号加两个父提交
    assert len(sh(trio.work, 'rev-list', '--parents', '-n', '1', 'HEAD').split()) == 3
    subject = sh(trio.work, 'log', '-1', '--format=%s')
    assert 'dev' in subject and '1' in subject


def test_asset_conflict_takes_upstream_and_regenerates(trio):
    trio.fork_commit({'tasks/Foo/as/a.png': b'\x89PNG\x00fork', 'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'tasks/Foo/as/a.png': b'\x89PNG\x00up', 'tasks/Foo/as/image.json': '[{"v": 1}]\n'})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert result.status == 'merged'
    assert result.resolved == ['tasks/Foo/as/a.png', 'tasks/Foo/as/image.json']
    assert (trio.work / 'tasks/Foo/as/a.png').read_bytes() == b'\x89PNG\x00up'
    assert (trio.work / 'tasks/Foo/as/image.json').read_bytes() == b'[{"v": 1}]\n'
    assert regen.calls == ['Foo']
    # 重新生成的 assets.py 进了合并提交，不是留在工作区里
    assert sh(trio.work, 'show', 'HEAD:tasks/Foo/assets.py') == '# regenerated Foo'
    assert is_clean(trio.work)
    assert trio.fork_tip == trio.head


def test_non_ascii_asset_paths_are_resolved(trio):
    # 上游仓库里有 tasks/Duel/ban/斗町.png 这类中文文件名，git 默认会转义输出，要按原始路径处理
    name = 'tasks/Foo/as/斗町.png'
    trio.fork_commit({name: b'\x89PNG\x00fork-cn'})
    trio.upstream_commit({name: b'\x89PNG\x00up-cn'})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert result.status == 'merged'
    assert result.resolved == [name]
    assert (trio.work / name).read_bytes() == b'\x89PNG\x00up-cn'
    assert regen.calls == ['Foo']
    assert is_clean(trio.work)


def test_code_conflict_aborts_and_keeps_head(trio):
    trio.fork_commit({'script.py': 'fork\n'})
    trio.upstream_commit({'script.py': 'upstream\n'})
    before = trio.head
    result = trio.merge()
    assert result.status == 'conflict_aborted'
    assert result.conflicts == ['script.py']
    assert trio.head == before
    assert not merging(trio.work)
    assert is_clean(trio.work)
    assert (trio.work / 'script.py').read_bytes() == b'fork\n'


def test_mixed_conflicts_abort(trio):
    trio.fork_commit({'script.py': 'fork\n', 'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'script.py': 'upstream\n', 'tasks/Foo/as/image.json': '[{"v": 1}]\n'})
    before = trio.head
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert result.status == 'conflict_aborted'
    assert result.conflicts == ['script.py']      # 只列代码冲突
    assert result.resolved == []
    assert regen.calls == []
    assert trio.head == before
    assert not merging(trio.work)
    assert is_clean(trio.work)
    # 素材那一半没有被单独取用
    assert (trio.work / 'tasks/Foo/as/image.json').read_bytes() == b'[{"v": 2}]\n'


# ---- 代码冲突只落在入口文件里：备份、核对前提、取官方版本重接入口 ----------------------------------------------------
# 用假清单：BASE 的 script.py 是 'base\n'，fork 一侧提交"共同祖先 + 清单"，官方在同一处加了别的行，两边就冲突。
# 上面两个用例用的是真清单：合成的 script.py 里找不到真清单的定位行，核对前提不过，所以仍然整体中止。

HOOKED = (Hook('script.py', 'base', 'after', ('hooked',)),)
TWO_HOOK_FILES = HOOKED + (Hook('new.py', 'base', 'after', ('hooked',)),)      # new.py 不在 BASE 里
STAMP = re.compile(r'log/merge_backup/\d{4}-\d{2}-\d{2}_\d{6}')


def raw_show(repo, spec):
    """git show 的原始字节。sh 走文本模式，\\r\\n 会被换成 \\n，看不出提交进去的换行。"""
    return subprocess.run(['git', 'show', spec], cwd=repo, capture_output=True, check=True).stdout


def backup_note(trio, result):
    return (trio.work / result.backup / '说明.txt').read_bytes().decode('utf-8')


def test_a_hook_file_conflict_is_rehooked_and_keeps_upstream_changes(trio):
    trio.fork_commit({'script.py': 'base\nhooked\n', 'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n', 'tasks/Foo/as/image.json': '[{"v": 1}]\n'})
    before, upstream = trio.head, sh(trio.seed, 'rev-parse', 'HEAD')
    regen = Regen(trio.work)
    result = trio.merge(regen, hooks=HOOKED)
    assert (result.status, result.rehooked, result.overridden, result.rehook_error) == (
        'merged', ['script.py'], [], '')
    assert (result.conflicts, result.resolved, regen.calls) == ([], ['tasks/Foo/as/image.json'], ['Foo'])   # 素材照常
    assert raw_show(trio.work, 'HEAD:script.py') == b'base\nhooked\nupstream\n'     # 我们的入口和官方的改动都在
    assert (trio.work / 'script.py').read_bytes() == b'base\nhooked\nupstream\n'
    assert before in parents(trio.work) and trio.fork_tip == trio.head
    assert is_clean(trio.work) and not merging(trio.work)
    # 备份：合并前 HEAD 版本的入口文件，加一份说明
    assert STAMP.fullmatch(result.backup)
    backup = trio.work / result.backup
    assert sorted(path.name for path in backup.iterdir()) == ['script.py', '说明.txt']
    assert (backup / 'script.py').read_bytes() == b'base\nhooked\n'
    note = backup_note(trio, result)
    assert f'合并前的 HEAD：{before}\n' in note and f'上游提交：{upstream}\n' in note
    assert '冲突的入口文件：script.py\n' in note
    assert note.endswith('官方改过、已用我们的版本覆盖的函数体：无\n结果：成功，合并已提交\n')


def test_a_replaced_body_is_overridden_and_recorded(trio):
    def router(body):
        return f'import x\n\n\ndef update_info():\n    {body}\n'

    name = 'module/server/router.py'
    trio.upstream_commit({name: router('return 1')})
    assert trio.merge().status == 'merged'                  # 官方新加的文件，干净地合进来
    hooks = (Hook(name, 'def update_info():', 'replace_body', ('    return ours()',)),)
    trio.fork_commit({name: router('return ours()')})      # 共同祖先 + 清单
    trio.upstream_commit({name: router('return 2')})       # 官方也改了这个函数体
    result = trio.merge(hooks=hooks)
    assert (result.status, result.rehooked, result.rehook_error) == ('merged', [name], '')
    assert result.overridden == [f'{name} 的 update_info']
    assert raw_show(trio.work, f'HEAD:{name}') == router('return ours()').encode()       # 用我们的版本覆盖
    backup = trio.work / result.backup
    assert (backup / name).read_bytes() == router('return ours()').encode()              # 保持原来的目录结构
    assert (backup / '官方改动-update_info.diff').read_bytes().decode('utf-8') == (
        '--- 共同祖先\n+++ 官方\n@@ -1 +1 @@\n-    return 1\n+    return 2\n')
    assert (f'官方改过、已用我们的版本覆盖的函数体：{name} 的 update_info（官方的改动见 官方改动-update_info.diff）\n'
            in backup_note(trio, result))


def fail_backup(*args, **kwargs):
    raise OSError('disk full')


# (fork 改的入口文件, 官方的改动, 换掉写备份的函数, 原因)
REHOOK_FAILURES = [
    pytest.param({'script.py': 'base\nhooked\n'}, {'script.py': 'rebased\n'}, None,
                 'script.py 的第 1 条入口找不到这一行：base', id='anchor-gone'),
    pytest.param({'script.py': 'base\nhooked\n'}, {'script.py': 'base\nupstream\nbase\n'}, None,
                 'script.py 的第 1 条入口这一行出现了 2 次：base', id='anchor-twice'),
    pytest.param({'script.py': 'base\nhooked\n'}, {'script.py': None}, None,
                 '官方删掉了 script.py', id='deleted-upstream'),
    pytest.param({'script.py': 'base\nhooked\nmine\n'}, {'script.py': 'base\nupstream\n'}, None,
                 'script.py 里有入口清单以外的改动，自动处理会把它弄丢', id='edited-beyond-the-list'),
    pytest.param({'new.py': 'base\nhooked\n'}, {'new.py': 'base\nupstream\n'}, None,
                 'new.py 没有共同祖先版本', id='added-on-both-sides'),
    pytest.param({'script.py': 'base\nhooked\n'}, {'script.py': 'base\nupstream\n'}, fail_backup,
                 '备份没写成（OSError: disk full）', id='backup-fails'),
]


@pytest.mark.parametrize('fork_files, upstream_files, backup_writer, reason', REHOOK_FAILURES)
def test_rehook_failures_abort_cleanly(trio, monkeypatch, fork_files, upstream_files, backup_writer, reason):
    # 都带一处素材冲突：合并要是往下走了，regenerate 就会被调用
    trio.fork_commit({**fork_files, 'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({**upstream_files, 'tasks/Foo/as/image.json': '[{"v": 1}]\n'})
    if backup_writer:
        monkeypatch.setattr(fork_git, '_write_backup', backup_writer)
    before, fork_before = trio.head, trio.fork_tip
    regen = Regen(trio.work)
    result = trio.merge(regen, hooks=TWO_HOOK_FILES)
    assert (result.status, result.conflicts, result.rehook_error) == ('conflict_aborted', sorted(fork_files), reason)
    assert (result.rehooked, result.overridden, result.resolved, regen.calls) == ([], [], [], [])
    assert trio.head == before and trio.fork_tip == fork_before
    assert is_clean(trio.work) and not merging(trio.work)
    for name, content in fork_files.items():
        assert (trio.work / name).read_bytes() == content.encode()                       # fork 的版本原样留着
    if backup_writer:
        assert result.backup == ''
        return
    assert STAMP.fullmatch(result.backup)
    backup = trio.work / result.backup
    for name in ('new.py', 'script.py'):                   # 清单里的入口文件都备份，HEAD 里没有的跳过
        in_head = sh(trio.work, 'ls-tree', '--name-only', 'HEAD', name) == name
        assert (backup / name).exists() is in_head
        if in_head:
            assert (backup / name).read_bytes() == raw_show(trio.work, f'HEAD:{name}')
    assert backup_note(trio, result).endswith(f'结果：失败：{reason}。合并已中止，分支未改动\n')


def test_a_conflict_beyond_the_hook_files_aborts_without_backup(trio):
    trio.fork_commit({'script.py': 'base\nhooked\n', 'other.py': 'fork\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n', 'other.py': 'upstream\n'})
    before = trio.head
    result = trio.merge(hooks=HOOKED)
    assert (result.status, result.conflicts) == ('conflict_aborted', ['other.py', 'script.py'])
    assert (result.rehook_error, result.backup, result.rehooked) == ('', '', [])
    assert not (trio.work / 'log' / 'merge_backup').exists()
    assert trio.head == before and is_clean(trio.work) and not merging(trio.work)
    assert (trio.work / 'script.py').read_bytes() == b'base\nhooked\n'


def test_rehook_in_a_crlf_worktree(trio):
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n'})
    sh(trio.work, 'config', 'core.autocrlf', 'true')       # Windows 上运行目录的样子：仓库里是 LF，工作区里是 CRLF
    sh(trio.work, 'rm', '-q', '-r', '--cached', '.')       # 重新检出，工作区换成 CRLF
    sh(trio.work, 'reset', '-q', '--hard')
    assert (trio.work / 'script.py').read_bytes() == b'base\r\nhooked\r\n'
    result = trio.merge(hooks=HOOKED)
    assert (result.status, result.rehooked) == ('merged', ['script.py'])
    assert is_clean(trio.work) and not merging(trio.work)
    assert raw_show(trio.work, 'HEAD:script.py') == b'base\nhooked\nupstream\n'          # 提交进去的是 LF
    assert (trio.work / 'script.py').read_bytes() == b'base\r\nhooked\r\nupstream\r\n'   # 工作区保持 CRLF


def test_a_rehooked_merge_that_fails_the_self_check_aborts_and_keeps_the_backup(trio):
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n'})
    before = trio.head
    seen = []

    def verify(repo):
        seen.append((repo / 'script.py').read_bytes())
        return False, 'import boom'

    result = trio.merge(verify=verify, hooks=HOOKED)
    assert (result.status, result.message, result.rehook_error) == ('smoke_failed', 'import boom', '')
    assert seen == [b'base\nhooked\nupstream\n']            # 自检看到的是重接之后的文件
    assert result.rehooked == ['script.py'] and STAMP.fullmatch(result.backup)      # 不管什么状态都带着
    assert trio.head == before and is_clean(trio.work) and not merging(trio.work)
    assert (trio.work / 'script.py').read_bytes() == b'base\nhooked\n'
    assert backup_note(trio, result).endswith(
        '结果：合并没有完成，已中止，分支未改动：处理途中出错或自检没通过，原因见服务日志\n')


def test_a_rehook_that_fails_during_the_write_back_aborts_cleanly(trio, broken_git):
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n'})
    before = trio.head
    broken_git('add', '--', 'script.py')                   # 重接好的文件已经写回工作区，登记进索引时失败
    result = trio.merge(hooks=HOOKED)
    assert (result.status, result.message, result.rehooked) == ('smoke_failed', 'fatal: simulated failure', [])
    assert STAMP.fullmatch(result.backup)
    assert trio.head == before and is_clean(trio.work) and not merging(trio.work)
    assert (trio.work / 'script.py').read_bytes() == b'base\nhooked\n'                # 写回的文件也还原了
    assert backup_note(trio, result).endswith(
        '结果：合并没有完成，已中止，分支未改动：处理途中出错或自检没通过，原因见服务日志\n')


def test_a_rehooked_merge_that_needs_a_new_dependency_is_still_a_success(trio):
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n', 'requirements.txt': 'pkg==1\nbrandnew==1.0\n'})
    result = trio.merge(verify=missing_module('brandnew'), hooks=HOOKED)
    assert (result.status, result.rehooked, result.missing_module) == ('needs_install', ['script.py'], 'brandnew')
    assert trio.fork_tip == trio.head
    assert backup_note(trio, result).endswith(
        '结果：成功，合并已提交；上游新增了第三方依赖 brandnew，要在 OASX 重启服务安装\n')


def test_a_rehooked_merge_whose_push_fails_keeps_the_rehook_fields(trio, tmp_path):
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n'})
    push_fails(trio, tmp_path)
    result = trio.merge(hooks=HOOKED)
    assert (result.status, result.merged_locally, result.rehooked) == ('push_failed', True, ['script.py'])
    assert STAMP.fullmatch(result.backup)
    assert backup_note(trio, result).endswith('结果：成功，合并已提交\n')         # 合并本身已经提交，推送另说


def test_a_rehook_backup_that_fails_midway_is_removed(trio, broken_git):
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n'})
    before = trio.head
    broken_git('show')                                     # 备份目录建好之后，读 HEAD 版本失败
    result = trio.merge(hooks=HOOKED)
    assert (result.status, result.backup) == ('conflict_aborted', '')
    assert result.rehook_error == ("备份没写成（CalledProcessError: Command '['git', 'show', 'HEAD:script.py']' "
                                   "returned non-zero exit status 128.）")
    assert list((trio.work / 'log' / 'merge_backup').iterdir()) == []                 # 写了一半的目录删掉了
    assert trio.head == before and is_clean(trio.work) and not merging(trio.work)


def test_rehook_backups_in_the_same_second_get_a_suffix(trio, monkeypatch):
    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 11, 26, 10, 15, 0)

    monkeypatch.setattr(fork_git, 'datetime', Clock)
    for taken in ('2026-11-26_101500', '2026-11-26_101500_2'):
        (trio.work / 'log' / 'merge_backup' / taken).mkdir(parents=True)
    trio.fork_commit({'script.py': 'base\nhooked\n'})
    trio.upstream_commit({'script.py': 'base\nupstream\n'})
    result = trio.merge(hooks=HOOKED)
    assert (result.status, result.backup) == ('merged', 'log/merge_backup/2026-11-26_101500_3')
    assert '时间：2026-11-26 10:15:00\n' in backup_note(trio, result)


def test_rehook_diff_names_carry_the_file_name_when_functions_share_a_name():
    found = [OverriddenBody('module/a/x.py', 'f', ''), OverriddenBody('module/b/y.py', 'f', ''),
             OverriddenBody('module/b/y.py', 'g', '')]
    assert fork_git._diff_names(found) == ['官方改动-x-f.diff', '官方改动-y-f.diff', '官方改动-g.diff']


def test_push_failure_keeps_local_merge(trio, tmp_path):
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    push_fails(trio, tmp_path)
    result = trio.merge()
    assert (result.status, result.commits) == ('push_failed', 1)
    assert result.merged_locally is True                 # 这次调用造出了合并提交：磁盘上的代码已经变了
    assert 'missing.git' in result.message
    assert trio.head != before
    assert before in parents(trio.work)
    assert not merging(trio.work)


def test_fetch_failure_reports_and_keeps_head(trio, tmp_path):
    before = trio.head
    result = trio.merge(upstream=str(tmp_path / 'missing.git'))
    assert (result.status, result.commits, result.fetch_remote) == ('fetch_failed', 0, 'upstream')
    assert 'missing.git' in result.message
    assert trio.head == before
    assert not merging(trio.work)


def test_a_fork_fetch_failure_names_the_fork_and_changes_nothing(trio, tmp_path, git_calls):
    trio.upstream_commit({'new.txt': 'hello\n'})
    sh(trio.work, 'remote', 'set-url', 'origin', str(tmp_path / 'missing.git'))
    before = trio.head
    git_calls.clear()
    result = trio.merge()
    assert (result.status, result.commits, result.fetch_remote) == ('fetch_failed', 0, 'origin')
    assert 'missing.git' in result.message
    assert trio.head == before and not merging(trio.work) and is_clean(trio.work)
    assert not [call for call in git_calls if call[0] in ('merge', 'commit', 'push')]


# ---- fork 领先运行目录（评审 I-1）---------------------------------------------------------------------------------
# 开发克隆往 fork 部署了新的 custom 提交，运行目录还没在 OASX 重启服务、没被启动器对齐到 fork。修复前，合并在旧基点上
# 做出本地合并，推送因为非快进被拒，之后每次重试都一样；启动器守卫看到本地提交又跳过对齐，运行目录永远拿不到 fork 的新代码。

@pytest.mark.parametrize('upstream_moved', [True, False], ids=['upstream-new', 'upstream-same'])
@pytest.mark.parametrize('fetched', [False, True], ids=['not-fetched-yet', 'already-fetched'])
def test_fork_ahead_pauses_without_merging_or_pushing(trio, tmp_path, git_calls, upstream_moved, fetched):
    fork_tip = rival_pushes(trio, tmp_path)
    if fetched:
        sh(trio.work, 'fetch', '-q', 'origin', 'custom')
    if upstream_moved:
        trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    git_calls.clear()
    result = trio.merge()
    assert (result.status, result.commits, result.merged_locally) == ('fork_ahead', 1, False)
    assert trio.head == before and trio.fork_tip == fork_tip
    assert not merging(trio.work) and is_clean(trio.work)
    assert not [call for call in git_calls if call[0] in ('merge', 'commit', 'push')]


def test_diverged_stops_without_merging_or_pushing(trio, tmp_path, git_calls):
    # 修复之前留下的局面：运行目录在旧基点上已经有本地提交（比如当时推送失败的合并），fork 又有了新提交
    trio.fork_commit({'local.txt': 'made on the old base\n'}, 'local merge on the old base')
    fork_tip = rival_pushes(trio, tmp_path)
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    git_calls.clear()
    for _ in range(2):                                     # 每次都停在 diverged，不再变成一次次推送失败
        result = trio.merge()
        assert (result.status, result.commits, result.merged_locally) == ('diverged', 0, False)
        assert 'origin/custom' in result.message
    assert trio.head == before and trio.fork_tip == fork_tip
    assert not merging(trio.work) and is_clean(trio.work)
    assert not [call for call in git_calls if call[0] in ('merge', 'commit', 'push')]


def test_review_p3_sequence_no_longer_loops_on_push_failed(trio, tmp_path):
    fork_tip = rival_pushes(trio, tmp_path)                # 开发克隆部署了新提交
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    assert [trio.merge().status for _ in range(2)] == ['fork_ahead', 'fork_ahead']     # 按钮或定时合并，重试也一样
    assert trio.head == before
    assert sh(trio.work, 'rev-list', 'HEAD', '--not', '--remotes') == ''     # 启动器守卫看的清单是空的，不会挡着对齐
    sh(trio.work, 'merge', '-q', '--ff-only', 'origin/custom')                 # 用户在 OASX 重启服务，启动器对齐到 fork
    result = trio.merge()
    assert (result.status, result.commits, result.merged_locally) == ('merged', 1, True)
    assert trio.fork_tip == trio.head
    sh(trio.work, 'merge-base', '--is-ancestor', fork_tip, 'HEAD')            # fork 的新提交在合并结果里


def test_a_push_rejected_by_a_racing_fork_update_is_not_forced_and_the_retry_reads_diverged(trio, tmp_path):
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    raced = []

    def verify(repo):                                      # 拉取 fork 之后、推送之前，开发克隆往 fork 推了新提交
        raced.append(rival_pushes(trio, tmp_path))
        return True, ''

    result = trio.merge(verify=verify)
    assert (result.status, result.commits, result.merged_locally) == ('push_failed', 1, True)
    assert '[rejected]' in result.message                  # 原文进日志；界面上归类成"fork 上有更新的提交，推送被拒"
    assert trio.fork_tip == raced[0]                       # 没有强推
    assert before in parents(trio.work)                    # 本地合并保留
    retried = trio.merge()
    assert retried.status == 'diverged'                    # 重试先拉取 fork，看出两边分叉，不再去推
    assert trio.fork_tip == raced[0]


def test_dirty_worktree_refuses(trio):
    trio.upstream_commit({'new.txt': 'hello\n'})
    write(trio.work, {'script.py': 'hand edited\n'})
    before = trio.head
    result = trio.merge()
    assert result.status == 'dirty'
    assert 'script.py' in result.message
    assert trio.head == before
    assert (trio.work / 'script.py').read_bytes() == b'hand edited\n'      # 手改的内容原样保留


def test_dirty_detail_keeps_chinese_paths_readable(trio):
    sh(trio.work, 'config', 'core.quotepath', 'true')     # git 的默认值；本机的全局配置怎样都不影响这个用例
    write(trio.work, {'tasks/Foo/as/斗町.png': b'\x89PNG\x00hand edited'})
    result = trio.merge()
    assert result.status == 'dirty'
    assert 'tasks/Foo/as/斗町.png' in result.message       # 不是 "tasks/Foo/as/\346\226\227..." 这样的转义


def test_untracked_files_do_not_count_as_dirty(trio):
    trio.upstream_commit({'new.txt': 'hello\n'})
    write(trio.work, {'log/run.log': 'x', 'scratch.tmp': 'y'})
    assert trio.merge().status == 'merged'
    assert (trio.work / 'scratch.tmp').read_bytes() == b'y'


@pytest.mark.parametrize('conflicting', [False, True], ids=['clean-leftover', 'conflicted-leftover'])
def test_leftover_merge_state_is_aborted_first(trio, conflicting):
    if conflicting:
        trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
        trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n'})
    else:
        trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    sh(trio.work, 'fetch', '-q', 'upstream', 'dev')
    subprocess.run(['git', 'merge', '--no-commit', '--no-ff', 'upstream/dev'], cwd=trio.work, capture_output=True)
    assert merging(trio.work)

    result = trio.merge()
    assert (result.status, result.commits) == ('merged', 1)
    assert not merging(trio.work)
    assert is_clean(trio.work)
    assert len(sh(trio.work, 'rev-list', '--parents', '-n', '1', 'HEAD').split()) == 3
    assert before in sh(trio.work, 'rev-list', '--parents', '-n', '1', 'HEAD').split()


def test_leftover_merge_with_half_written_assets_heals_then_merges(trio, git_calls):
    leave_half_regenerated_merge(trio)
    before = trio.head
    git_calls.clear()
    result = trio.merge()
    assert (result.status, result.commits) == ('merged', 1)
    assert not merging(trio.work) and is_clean(trio.work)
    assert before in sh(trio.work, 'rev-list', '--parents', '-n', '1', 'HEAD').split()
    assert sh(trio.work, 'show', 'HEAD:tasks/Foo/assets.py') == '# regenerated Foo'       # 半截的文件没混进合并提交
    assert trio.fork_tip == trio.head
    # 只动了该动的：恰好登记那一个文件，abort 只重试了一次
    healing = git_calls[:git_calls.index(STATUS_CALL)]
    assert [call for call in healing if call[0] == 'add'] == [('add', '--', 'tasks/Foo/assets.py')]
    assert healing.count(('merge', '--abort')) == 2
    # 第一次 abort 之前只看清单：合并基点、上游改了哪些文件、哪些文件有改动
    before_first_abort = healing[:healing.index(('merge', '--abort'))]
    assert {call[0] for call in before_first_abort} == {'rev-parse', 'merge-base', 'diff'}


def test_leftover_merge_heals_a_generated_assets_file_outside_the_merge_diff(trio):
    # 上游和 fork 都只改了 image.json，assets.py 不在上游那一侧改动的文件里（合并基点到 MERGE_HEAD 的差异）：它是
    # regenerate 生成后登记进索引的，自检又动了它，进程在这时被杀。所属任务目录被上游改过，所以它的 assets.py
    # 也算合并自己留下的
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n'})

    def killed(repo):
        write(repo, {'tasks/Foo/assets.py': '# touched by verify'})
        raise Killed

    with pytest.raises(Killed):
        trio.merge(verify=killed)
    assert sh(trio.work, 'diff', '--name-only', 'HEAD...MERGE_HEAD').split() == ['tasks/Foo/as/image.json']
    assert abort_fails(trio.work)

    result = trio.merge()
    assert (result.status, result.commits) == ('merged', 1)
    assert not merging(trio.work) and is_clean(trio.work)
    assert sh(trio.work, 'show', 'HEAD:tasks/Foo/assets.py') == '# regenerated Foo'


# 清理残留的合并只动上游那一侧改动的文件（合并基点到 MERGE_HEAD 的差异，加所属任务目录的 assets.py）。人改了别的文件，
# 不管暂存没暂存、残留的合并能不能直接 abort，都停下来返回 dirty：不 abort、不 add、不丢弃。
# (残留合并的种类, 人改的文件, fork 也改了它, 改完又暂存)
HUMAN_EDITS = [
    pytest.param('blocked', 'script.py', True, False, id='blocked-fork-only-unstaged'),          # 评审 S1
    pytest.param('blocked', 'script.py', True, True, id='blocked-fork-only-staged'),             # S1s
    pytest.param('plain', 'script.py', True, False, id='plain-fork-only-unstaged'),              # S1v
    pytest.param('plain', 'script.py', True, True, id='plain-fork-only-staged'),
    pytest.param('plain', 'other.py', False, False, id='plain-untouched-unstaged'),
    pytest.param('plain', 'other.py', False, True, id='plain-untouched-staged'),                 # S7
    pytest.param('blocked', 'other.py', False, False, id='blocked-untouched-unstaged'),
    pytest.param('blocked', 'other.py', False, True, id='blocked-untouched-staged'),
    pytest.param('blocked', 'tasks/Component/Buy/assets.py', False, False, id='blocked-untouched-task-assets'),
]


@pytest.mark.parametrize('leftover, edited, fork_changes_it, staged', HUMAN_EDITS)
def test_leftover_merge_stops_with_dirty_on_a_human_edit_outside_the_upstream_changes(
        trio, git_calls, leftover, edited, fork_changes_it, staged):
    fork_files = {edited: 'fork\n'} if fork_changes_it else None
    (leave_half_regenerated_merge if leftover == 'blocked' else leave_plain_merge)(trio, fork_files)
    write(trio.work, {edited: '# human edit\n'})
    if staged:
        sh(trio.work, 'add', edited)
    head = trio.head
    git_calls.clear()
    result = trio.merge()                                  # 不抛异常
    assert result.status == 'dirty'
    assert 'leftover merge' in result.message and edited in result.message
    assert sh(trio.work, 'rev-parse', '--short=7', 'MERGE_HEAD') in result.message
    assert merging(trio.work) and trio.head == head       # 合并状态原样留着
    assert (trio.work / edited).read_bytes() == b'# human edit\n'
    assert (edited in sh(trio.work, 'diff', '--cached', '--name-only').split()) is staged      # 暂存与否也没变
    if leftover == 'blocked':
        assert (trio.work / 'tasks/Foo/assets.py').read_bytes() == b'# half written'
    # abort 一次都没试，也没有任何会改动东西的命令：只看了清单
    assert {call[0] for call in git_calls} <= {'rev-parse', 'merge-base', 'diff'}

    # 人把自己的改动处理掉以后，下一次调用清理残留的合并并正常合并
    sh(trio.work, 'checkout', 'HEAD', '--', edited)
    assert trio.merge().status == 'merged'


def test_leftover_merge_that_still_cannot_be_aborted_after_staging_is_dirty(trio, broken_git):
    leave_half_regenerated_merge(trio)
    blocked = broken_git('merge', '--abort', stderr='fatal: abort exploded')
    result = trio.merge()
    assert result.status == 'dirty'
    assert 'leftover merge' in result.message and 'abort exploded' in result.message
    assert 'tasks/Foo/assets.py' in result.message         # 点名登记过的文件
    assert len(blocked) == 2                                # 第一次加一次重试，不再多
    assert merging(trio.work)
    assert (trio.work / 'tasks/Foo/assets.py').read_bytes() == b'# half written'    # 内容还在（登记进了索引，没丢）


@pytest.mark.parametrize('command, expected, aborts', [
    (('merge-base',), 'could not list the files the merge touches', 0),
    (('diff',), 'could not list the files the merge touches', 0),            # 第一个 diff 就是上游改动的清单
    (('diff', '--no-renames', '--name-only', '-z', '--cached'), 'could not list the modified files', 0),
    (('add',), 'git add failed: fatal: simulated failure', 1),
], ids=['merge-base', 'upstream-listing', 'staged-listing', 'add'])
def test_leftover_merge_heal_failing_midway_is_dirty_not_an_exception(trio, git_calls, broken_git, command, expected,
                                                                      aborts):
    leave_half_regenerated_merge(trio)
    broken_git(*command)
    git_calls.clear()
    result = trio.merge()
    assert result.status == 'dirty'
    assert 'leftover merge' in result.message and expected in result.message
    assert git_calls.count(('merge', '--abort')) == aborts   # 清单拿不到就一个 abort 都不试；add 失败了没走到重试
    assert merging(trio.work)
    assert (trio.work / 'tasks/Foo/assets.py').read_bytes() == b'# half written'


def test_is_asset_path_table():
    table = {
        'tasks/X/as/a.png': True,
        'tasks/X/as/a.jpg': True,
        'tasks/X/as/a.jpeg': True,
        'tasks/X/as/a.bmp': True,
        'tasks/X/as/image.json': True,
        'tasks/X/as/A.PNG': True,
        'tasks/X/assets.py': True,
        'tasks/Component/Buy/buy/image.json': True,
        'tasks/Component/Buy/assets.py': True,
        'tasks/X/config.py': False,
        'tasks/X/README.md': False,
        'tasks/X/as/a.png.bak': False,
        'script.py': False,
        'assets.py': False,
        'module/x/assets.py': False,
        'module/x/image.json': False,
        'config/oas1.json': False,
        'docs/tasks/a.png': False,
    }
    assert {path: is_asset_path(path) for path in table} == table


@pytest.mark.parametrize('side, expected', [('upstream', True), ('fork', False)])
def test_requirements_change_flag(trio, side, expected):
    # 上游改了 requirements.txt 才算；上游没改时，fork 自己改的不算（它在合并前就已经在 HEAD 里）
    trio.fork_commit({'requirements.txt': 'pkg==2\n'} if side == 'fork' else {'local.txt': 'x\n'})
    trio.upstream_commit({'requirements.txt': 'pkg==3\n'} if side == 'upstream' else {'new.txt': 'hello\n'})
    result = trio.merge()
    assert result.status == 'merged'
    assert result.requirements_changed is expected


def test_is_server_code_path_table():
    # 服务进程启动时就加载的代码：改了要在 OASX 重启服务才完整生效
    table = {
        'server.py': True,
        'module/server/app.py': True,
        'module/new_package/x.py': True,
        'module/config/argument/args.json': True,
        'deploy/git.py': True,
        'tasks/Foo/config.py': True,
        'tasks/Component/Buy/config_buy.py': True,
        'tasks/config.py': True,
        'tasks/Foo/script_task.py': False,
        'tasks/Foo/assets.py': False,
        'tasks/Foo/as/image.json': False,
        'tasks/Foo/config.json': False,
        'script.py': False,
        'dev_tools/assets_extract.py': False,
        'modules/x.py': False,
        'deploy.yaml': False,
        'README.md': False,
    }
    assert {path: is_server_code_path(path) for path in table} == table


@pytest.mark.parametrize('files, expected', [
    ({'module/server/new.py': 'x = 1\n'}, True),
    ({'server.py': 'print(1)\n'}, True),
    ({'deploy/new.py': 'x = 1\n'}, True),
    ({'tasks/Foo/config.py': 'x = 1\n'}, True),
    ({'new.txt': 'hello\n', 'tasks/Foo/as/image.json': '[{"v": 9}]\n'}, False),
], ids=['module', 'server', 'deploy', 'task-config', 'no-server-code'])
def test_server_code_changed_flag(trio, files, expected):
    trio.upstream_commit(files)
    result = trio.merge()
    assert result.status == 'merged'
    assert result.server_code_changed is expected


def test_server_code_changed_on_the_retry_path_compares_with_the_fork(trio, tmp_path):
    trio.upstream_commit({'module/server/new.py': 'x = 1\n'})
    push_fails(trio, tmp_path)
    failed = trio.merge()
    assert (failed.status, failed.server_code_changed) == ('push_failed', True)
    push_recovers(trio)
    retried = trio.merge()
    assert (retried.status, retried.server_code_changed) == ('pushed', True)    # 推送前拿 origin/custom 比的


# ---- 上游新增了第三方依赖（E1）---------------------------------------------------------------------------------

def test_a_new_third_party_dependency_is_merged_and_pushed_as_needs_install(trio):
    trio.upstream_commit({'requirements.txt': 'pkg==1\nbrandnew==1.0\n', 'script.py': 'import brandnew\n'})
    before = trio.head
    result = trio.merge(verify=missing_module('brandnew'))
    assert (result.status, result.commits, result.missing_module) == ('needs_install', 1, 'brandnew')
    assert result.merged_locally is True and result.requirements_changed is True
    assert "No module named 'brandnew'" in result.message           # 自检的原文留给日志
    assert before in parents(trio.work)
    assert trio.fork_tip == trio.head                                # 像普通合并一样提交并推送了
    assert not merging(trio.work) and is_clean(trio.work)


@pytest.mark.parametrize('verify, files', [
    (missing_module('brandnew'), {'script.py': 'import brandnew\n'}),              # requirements.txt 没变
    (missing_module('tasks.Foo.gone'), {'requirements.txt': 'pkg==2\n'}),          # 仓库自己的包
    (missing_module('script'), {'requirements.txt': 'pkg==2\n'}),                  # 仓库根目录下的模块
    (lambda repo: (False, 'ImportError: cannot import name x'), {'requirements.txt': 'pkg==2\n'}),
    (lambda repo: (False, 'timed out'), {'requirements.txt': 'pkg==2\n'}),
], ids=['requirements-same', 'repo-package', 'repo-module', 'other-import-error', 'timeout'])
def test_other_gate_failures_still_abort_as_smoke_failed(trio, verify, files):
    trio.upstream_commit(files)
    before, fork_before = trio.head, trio.fork_tip
    result = trio.merge(verify=verify)
    assert (result.status, result.missing_module, result.merged_locally) == ('smoke_failed', '', False)
    assert trio.head == before and trio.fork_tip == fork_before
    assert not merging(trio.work) and is_clean(trio.work)


def test_a_needs_install_merge_whose_push_fails_keeps_the_merge_and_the_module_name(trio, tmp_path):
    trio.upstream_commit({'requirements.txt': 'pkg==1\nbrandnew==1.0\n'})
    push_fails(trio, tmp_path)
    before = trio.head
    result = trio.merge(verify=missing_module('brandnew'))
    assert (result.status, result.missing_module, result.merged_locally) == ('push_failed', 'brandnew', True)
    assert result.requirements_changed is True and 'missing.git' in result.message
    assert before in parents(trio.work)


def test_smoke_failure_aborts_cleanly(trio):
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n', 'new.txt': 'hello\n'})
    before, fork_before = trio.head, trio.fork_tip
    result = trio.merge(verify=lambda repo: (False, 'import boom'))
    assert (result.status, result.message, result.commits) == ('smoke_failed', 'import boom', 0)
    assert (result.resolved, result.conflicts) == ([], [])
    assert trio.head == before
    assert not merging(trio.work)
    assert is_clean(trio.work)
    assert not (trio.work / 'new.txt').exists()
    assert (trio.work / 'tasks/Foo/as/image.json').read_bytes() == b'[{"v": 2}]\n'
    assert trio.fork_tip == fork_before                  # 没有推送


def test_verify_runs_on_the_resolved_merge_before_commit(trio):
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n', 'new.txt': 'hello\n'})
    seen = []

    def verify(repo):
        seen.append({
            'repo': repo,
            'merging': merging(repo),
            'json': (repo / 'tasks/Foo/as/image.json').read_bytes(),
            'assets': (repo / 'tasks/Foo/assets.py').read_bytes(),
            'new': (repo / 'new.txt').exists(),
        })
        return True, ''

    result = trio.merge(verify=verify)
    assert result.status == 'merged'
    assert seen == [{'repo': trio.work, 'merging': True, 'json': b'[{"v": 1}]\n',
                     'assets': b'# regenerated Foo\n', 'new': True}]
    assert not merging(trio.work)


def test_verify_exception_aborts_cleanly(trio):
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head

    def verify(repo):
        raise RuntimeError('verify blew up')

    result = trio.merge(verify=verify)
    assert result.status == 'smoke_failed'
    assert 'verify blew up' in result.message
    assert trio.head == before and not merging(trio.work) and is_clean(trio.work)


def test_git_failure_after_the_merge_started_still_aborts(trio):
    class Flaky(GitRunner):
        def run(self, *args, check=True):
            if args[:2] == ('ls-files', '-u'):
                raise subprocess.CalledProcessError(128, ['git', *args], stderr='fatal: Unable to create index.lock')
            return super().run(*args, check=check)

    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    result = merge_upstream(Flaky('git', trio.work), str(trio.upstream), 'dev', 'custom', Regen(trio.work))
    assert result.status == 'smoke_failed'
    assert 'index.lock' in result.message
    assert trio.head == before and not merging(trio.work) and is_clean(trio.work)
    assert not (trio.work / 'new.txt').exists()


def test_regenerate_failure_aborts_cleanly_after_partial_write(trio):
    # assets.py 上游也改了，干净地并进了索引；regenerate 写了半截文件就抛错
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 2}]\n'})
    trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n', 'tasks/Foo/assets.py': '# upstream assets\n'})
    before = trio.head

    def broken(task):
        write(trio.work, {'tasks/Foo/assets.py': '# partial'})
        raise RuntimeError('extract boom')

    result = trio.merge(broken)
    assert result.status == 'smoke_failed'
    assert 'extract boom' in result.message
    assert trio.head == before
    assert not merging(trio.work)
    assert is_clean(trio.work)
    assert (trio.work / 'tasks/Foo/assets.py').read_bytes() == b'# generated v0\n'


def test_upstream_deleted_asset_conflict_is_resolved_by_deletion(trio):
    trio.fork_commit({'tasks/Foo/as/a.png': b'\x89PNG\x00fork'})
    trio.upstream_commit({'tasks/Foo/as/a.png': None})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert result.status == 'merged'
    assert result.resolved == ['tasks/Foo/as/a.png']
    assert not (trio.work / 'tasks/Foo/as/a.png').exists()
    assert sh(trio.work, 'ls-tree', '--name-only', 'HEAD', 'tasks/Foo/as/a.png') == ''
    assert regen.calls == ['Foo']
    assert is_clean(trio.work)


def test_task_removed_upstream_is_not_regenerated(trio):
    trio.upstream_commit({name: None for name in BASE if name.startswith('tasks/Foo/')})
    trio.fork_commit({'tasks/Foo/as/a.png': b'\x89PNG\x00fork'})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert result.status == 'merged'
    assert result.resolved == ['tasks/Foo/as/a.png']
    assert regen.calls == []                                # 目录里已经没有文件，不去生成一个空壳 assets.py
    assert not (trio.work / 'tasks/Foo/assets.py').exists()
    assert sh(trio.work, 'ls-tree', '-r', '--name-only', 'HEAD', 'tasks/Foo') == ''


def test_component_assets_are_regenerated_per_subtask(trio):
    # 组件的 assets.py 在 tasks/Component/<组件>/ 下，任务名要带上组件这一级
    files = {'tasks/Component/Buy/buy/image.json': '[{"v": %d}]\n', 'tasks/Foo/as/image.json': '[{"v": %d}]\n'}
    trio.fork_commit({name: text % 2 for name, text in files.items()})
    trio.upstream_commit({name: text % 1 for name, text in files.items()})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert result.status == 'merged'
    assert regen.calls == ['Component/Buy', 'Foo']
    assert sh(trio.work, 'show', 'HEAD:tasks/Component/Buy/assets.py') == '# regenerated Component/Buy'
    assert not (trio.work / 'tasks/Component/assets.py').exists()


def test_untracked_file_in_the_way_aborts_without_merge_state(trio):
    trio.upstream_commit({'new.txt': 'upstream\n'})
    write(trio.work, {'new.txt': 'mine\n'})
    before = trio.head
    result = trio.merge()
    assert result.status == 'conflict_aborted'
    assert result.conflicts == []
    assert 'new.txt' in result.message
    assert trio.head == before
    assert not merging(trio.work)
    assert (trio.work / 'new.txt').read_bytes() == b'mine\n'


def test_push_goes_to_the_configured_branch_from_any_local_branch(trio):
    sh(trio.work, 'branch', '-m', 'master')              # 运行目录的本地分支名可能还是 master
    trio.upstream_commit({'new.txt': 'hello\n'})
    assert trio.merge().status == 'merged'
    assert trio.fork_tip == trio.head


def rival_pushes(trio, tmp_path):
    """别处往 fork 的 custom 推了一个提交，返回推完之后 custom 的提交号。work 里的 origin/custom 还是旧的。"""
    rival = tmp_path / 'rival'
    sh(tmp_path, 'clone', '-q', '-c', 'core.autocrlf=false', str(trio.fork), str(rival))
    configure(rival)
    commit(rival, {'rival.txt': 'someone else\n'}, 'rival')
    sh(rival, 'push', '-q', 'origin', 'HEAD:custom')
    return trio.fork_tip


@pytest.mark.parametrize('requirements', [True, False], ids=['requirements-changed', 'requirements-same'])
def test_retry_after_push_failure_pushes_the_kept_merge(trio, tmp_path, requirements):
    trio.upstream_commit({'new.txt': 'hello\n', **({'requirements.txt': 'pkg==3\n'} if requirements else {})})
    before = trio.head
    push_fails(trio, tmp_path)
    failed = trio.merge()
    assert (failed.status, failed.commits, failed.requirements_changed) == ('push_failed', 1, requirements)
    merged_head = trio.head
    assert trio.fork_tip == before                         # 合并只在本地

    push_recovers(trio)                                    # 网络恢复
    retried = trio.merge()
    assert (retried.status, retried.commits, retried.message) == ('pushed', 2, '')      # 合并提交加上游的那 1 个
    assert retried.requirements_changed is requirements   # 推送前拿 origin/custom 比的，推完它就挪到 HEAD 了
    assert retried.merged_locally is False                 # 这次没有造新的合并
    assert (retried.resolved, retried.conflicts) == ([], [])
    assert trio.fork_tip == trio.head == merged_head       # 补推的就是原来那个合并，没有再造提交
    assert not merging(trio.work) and is_clean(trio.work)

    again = trio.merge()
    assert (again.status, again.commits, again.requirements_changed) == ('up_to_date', 0, False)


def test_retry_that_fails_again_stays_retryable(trio, tmp_path):
    trio.upstream_commit({'new.txt': 'hello\n', 'requirements.txt': 'pkg==3\n'})
    before = trio.head
    push_fails(trio, tmp_path)
    assert trio.merge().status == 'push_failed'
    again = trio.merge()
    assert (again.status, again.commits, again.requirements_changed) == ('push_failed', 2, True)    # 这次数的是待推送的提交
    assert again.merged_locally is False                   # 补推失败：这次什么都没合并，文案和重启都跟着不同
    assert 'missing.git' in again.message
    assert trio.fork_tip == before
    assert not merging(trio.work) and is_clean(trio.work)

    push_recovers(trio)
    assert trio.merge().status == 'pushed'
    assert trio.fork_tip == trio.head


def test_local_commits_that_never_reached_the_fork_are_pushed(trio):
    trio.fork_commit({'tasks/Foo/as/a.png': b'\x89PNG\x00fork'}, 'asset page save')        # 比如素材页保存后推送失败留下的
    result = trio.merge()
    assert (result.status, result.commits, result.requirements_changed) == ('pushed', 1, False)
    assert (result.server_code_changed, result.merged_locally) == (False, False)
    assert trio.fork_tip == trio.head


def test_nothing_ahead_is_up_to_date_and_does_not_push(trio, tmp_path):
    push_fails(trio, tmp_path)                             # 真去推的话会失败，就不是 up_to_date
    result = trio.merge()
    assert (result.status, result.commits, result.message) == ('up_to_date', 0, '')


def test_missing_tracking_ref_pushes_and_reports_zero(trio):
    # 远端的取回规则不覆盖 custom 时，拉取 fork 只写 FETCH_HEAD，不会有 origin/custom：比不了 fork 领先没有，
    # 照旧补推（不强推，fork 真有新提交的话 git 会拒绝）。启动器自己 reset 到 origin/<分支>，运行目录里不会是这样
    trio.fork_commit({'requirements.txt': 'pkg==2\n'}, 'local change')
    sh(trio.work, 'config', 'remote.origin.fetch', '+refs/heads/other:refs/remotes/origin/other')
    sh(trio.work, 'update-ref', '-d', 'refs/remotes/origin/custom')
    result = trio.merge()
    assert (result.status, result.commits, result.requirements_changed) == ('pushed', 0, False)    # 没有参照，数不出也比不了
    assert trio.fork_tip == trio.head                      # 但推上去了


def test_runner_check_semantics(trio):
    assert trio.runner.run('rev-parse', 'HEAD').stdout.strip() == trio.head
    assert trio.runner.run('rev-parse', '--verify', '-q', 'no-such-ref', check=False).returncode != 0
    with pytest.raises(subprocess.CalledProcessError) as raised:
        trio.runner.run('rev-parse', '--verify', 'no-such-ref')
    assert raised.value.stderr


@pytest.fixture
def run_calls(monkeypatch):
    """subprocess.run 每次调用的关键字参数，调用照常真跑。"""
    calls, real_run = [], subprocess.run

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', spy)
    return calls


def test_runner_never_waits_for_input(trio, run_calls, stall):
    net = stall('fetch')                                  # 联网命令走另一条路径，两条都要检查
    trio.runner.run('status')
    with pytest.raises(subprocess.TimeoutExpired):
        trio.runner.run('fetch', 'upstream')
    for kwargs in (run_calls[-1], net.processes[0].kwargs):
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert kwargs['env']['GIT_EDITOR'] == 'true'
        assert kwargs['env']['GCM_INTERACTIVE'] == 'Never'    # Git Credential Manager 取不到凭据时直接失败，不弹窗
        assert kwargs['stdin'] == subprocess.DEVNULL
    assert trio.runner.run('var', 'GIT_EDITOR').stdout.strip() == 'true'


def test_git_never_opens_a_console_window(trio, run_calls, stall):
    # 服务由 pythonw 起，没有控制台：不加 CREATE_NO_WINDOW 的话，每个 git 进程都会闪一个黑窗口
    net = stall('fetch')
    trio.runner.run('status')
    with pytest.raises(subprocess.TimeoutExpired):
        trio.runner.run('fetch', 'upstream')
    assert run_calls[-1]['creationflags'] == NO_WINDOW
    assert net.processes[0].kwargs['creationflags'] == NO_WINDOW


def test_only_network_commands_have_a_timeout(trio, run_calls):
    trio.runner.run('status')
    trio.runner.run('rev-parse', 'HEAD')
    assert run_calls and all('timeout' not in kwargs for kwargs in run_calls)      # 本地命令不设超时
    assert NETWORK_TIMEOUT == 300 and fork_git.NETWORK_COMMANDS == {'fetch', 'push', 'ls-remote'}


@pytest.mark.parametrize('command', ['fetch', 'push', 'ls-remote'])
def test_network_command_times_out_and_the_process_is_killed(trio, stall, command):
    net = stall(command)
    with pytest.raises(subprocess.TimeoutExpired):
        trio.runner.run(command, 'origin', check=False)      # check 为假时超时也照样抛出
    [process] = net.processes
    assert process.wait_timeouts == [NETWORK_TIMEOUT]
    assert net.killed == [process]
    # 输出必须落到文件而不是管道：帮手进程会抓着管道不放，杀掉 git 之后读管道的一端也不会返回，超时就失效了
    assert subprocess.PIPE not in (process.kwargs['stdout'], process.kwargs['stderr'])


def test_kill_tree_takes_down_the_whole_tree_then_the_process(monkeypatch):
    calls, flags = [], []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: calls.append(command) or
                        flags.append(kwargs.get('creationflags')))
    process = StalledGit(['git', 'fetch'])
    process.wait = lambda timeout=None: 0
    fork_git._kill_tree(process)
    # Windows 上先 taskkill /T 杀整棵树，因为 git-remote-https 这类帮手不会随 git 一起退出
    assert calls == ([['taskkill', '/F', '/T', '/PID', '0']] if os.name == 'nt' else [])
    assert flags == ([NO_WINDOW] if os.name == 'nt' else [])     # taskkill 也不闪窗口
    assert process.killed

    # git 在超时的瞬间已经自己退出了：它的 PID 可能已被别的进程占用，不能再按 PID 去杀
    calls.clear()
    process.poll = lambda: 0
    fork_git._kill_tree(process)
    assert calls == []


def test_network_command_result_and_errors_match_the_local_path(trio, tmp_path):
    # 联网命令的输出走临时文件，这一条路径的解码也要是 UTF-8（否则中文路径的报错会乱码），check 的语义也要和本地命令一致
    sh(trio.work, 'remote', 'set-url', 'origin', str(tmp_path / '缺失仓库.git'))
    pushed = trio.runner.run('push', 'origin', 'HEAD:custom', check=False)
    assert pushed.returncode != 0
    assert '缺失仓库.git' in pushed.stderr
    with pytest.raises(subprocess.CalledProcessError) as raised:
        trio.runner.run('push', 'origin', 'HEAD:custom')
    assert raised.value.returncode == pushed.returncode
    assert '缺失仓库.git' in raised.value.stderr


@pytest.mark.parametrize('remote', ['origin', 'upstream'])
def test_fetch_timeout_reports_and_keeps_head(trio, stall, remote):
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    net = stall('fetch', remote=remote)
    result = trio.merge()
    assert (result.status, result.commits, result.fetch_remote) == ('fetch_failed', 0, remote)
    assert result.message == 'git fetch timed out after 300s'
    assert len(net.killed) == 1
    assert trio.head == before
    assert not merging(trio.work)
    assert is_clean(trio.work)


FAKE_FETCH = '''\
import os, subprocess, sys, time
# 假的 git fetch：像 git 起 git-remote-https 那样起一个孙进程，孙进程继承输出文件，卡在"网络等待"里
grandchild = ("import os, time; open('grandchild.tmp', 'w').write(str(os.getpid())); "
              "os.replace('grandchild.tmp', 'grandchild.pid'); time.sleep(120)")
subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', grandchild], stdout=sys.stdout,
                 stderr=sys.stderr)
deadline = time.monotonic() + 60
while not os.path.exists('grandchild.pid') and time.monotonic() < deadline:
    time.sleep(0.05)
print('fake fetch is stuck', flush=True)
open('ready', 'w').close()
time.sleep(120)
'''


@pytest.mark.skipif(os.name != 'nt', reason='taskkill /T 杀整棵进程树只在 Windows 上')
def test_a_real_timeout_kills_the_whole_process_tree_and_leaves_no_temp_files(tmp_path, monkeypatch):
    import psutil
    repo, temp = tmp_path / 'repo', tmp_path / 'temp'
    repo.mkdir()
    temp.mkdir()
    (repo / 'fetch').write_text(FAKE_FETCH, encoding='utf-8')   # [python, 'fetch', ...]：python 执行当前目录下的 fetch 脚本
    monkeypatch.setattr(fork_git, 'NETWORK_TIMEOUT', 2)
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))        # 输出临时文件落在这里，事后好查有没有留下
    python = getattr(sys, '_base_executable', sys.executable)  # 不经 venv 的启动器，起得快，2 秒内一定起好孙进程
    grandchild = None
    try:
        began = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            GitRunner(python, repo).run('fetch', 'origin', check=False)
        assert time.monotonic() - began < 10                   # 到点就返回，不被孙进程攥着的输出文件拖住
        assert (repo / 'ready').exists(), '假 git 没能在超时之前起好孙进程'
        grandchild = int((repo / 'grandchild.pid').read_text())
        assert wait_until(lambda: not psutil.pid_exists(grandchild)), '孙进程还活着：只杀了 git 本身，没杀整棵树'
        assert wait_until(lambda: not any(temp.iterdir())), f'临时文件没删掉：{list(temp.iterdir())}'
    finally:
        if grandchild is not None and psutil.pid_exists(grandchild):      # 用例失败时也不留卡住的进程
            psutil.Process(grandchild).kill()


def test_push_timeout_keeps_local_merge(trio, stall):
    trio.upstream_commit({'new.txt': 'hello\n'})
    before, fork_before = trio.head, trio.fork_tip
    net = stall('push')
    result = trio.merge()
    assert (result.status, result.commits, result.merged_locally) == ('push_failed', 1, True)
    assert result.message == 'git push timed out after 300s'
    assert len(net.killed) == 1
    assert trio.head != before
    assert before in sh(trio.work, 'rev-list', '--parents', '-n', '1', 'HEAD').split()
    assert not merging(trio.work)
    assert trio.fork_tip == fork_before                  # 超时的推送没有到达 fork


def test_push_returns_the_timeout_text(trio, stall):
    stall('push')
    assert push(trio.runner, 'custom') == (False, 'git push timed out after 300s')


def test_count_behind_and_upstream_commits(trio):
    trio.upstream_commit({'a.txt': '1\n'}, '第一个提交')
    trio.upstream_commit({'b.txt': '2\n'}, 'second change')
    sh(trio.work, 'fetch', '-q', 'upstream', 'dev')
    assert count_behind(trio.runner, 'upstream/dev') == 2
    commits = upstream_commits(trio.runner, 'upstream/dev')
    assert [subject for _, _, _, subject in commits] == ['second change', '第一个提交']      # 新的在前，中文不乱码
    sha, author, date, _ = commits[0]
    assert sha == sh(trio.seed, 'rev-parse', 'HEAD')
    assert author == 'tester'
    datetime.fromisoformat(date)                                                          # ISO 8601
    assert len(upstream_commits(trio.runner, 'upstream/dev', n=1)) == 1
    assert upstream_commits(trio.runner, 'origin/custom') == []                           # 没有领先时为空


def test_ensure_remote_adds_then_repoints(trio, tmp_path):
    first, second = str(tmp_path / 'a.git'), str(tmp_path / 'b.git')
    ensure_remote(trio.runner, 'extra', first)
    assert sh(trio.work, 'config', '--get', 'remote.extra.url') == first
    ensure_remote(trio.runner, 'extra', first)
    assert sh(trio.work, 'config', '--get-all', 'remote.extra.url') == first
    ensure_remote(trio.runner, 'extra', second)
    assert sh(trio.work, 'config', '--get-all', 'remote.extra.url') == second


def test_commit_paths_commits_only_the_given_paths(trio):
    write(trio.work, {'tasks/Foo/as/a.png': b'\x89PNG\x00new', 'tasks/Foo/as/新图.png': b'\x89PNG\x00add',
                      'other.txt': 'staged elsewhere\n'})
    sh(trio.work, 'add', 'other.txt')
    sha = commit_paths(trio.runner, ['tasks/Foo/as/a.png', 'tasks/Foo/as/新图.png'], '补图：Foo')
    assert sha == trio.head
    assert sh(trio.work, 'log', '-1', '--format=%s') == '补图：Foo'
    assert sh(trio.work, '-c', 'core.quotepath=off', 'diff-tree', '--no-commit-id', '--name-only', '-r',
              'HEAD').split() == ['tasks/Foo/as/a.png', 'tasks/Foo/as/新图.png']
    assert sh(trio.work, 'status', '--porcelain') == 'A  other.txt'        # 别的暂存内容原样留着

    write(trio.work, {'tasks/Foo/as/新图.png': None})
    deleted = commit_paths(trio.runner, ['tasks/Foo/as/新图.png'], '删图')
    assert deleted == trio.head and deleted != sha
    assert sh(trio.work, 'ls-tree', '--name-only', 'HEAD', 'tasks/Foo/as/新图.png') == ''
    assert sh(trio.work, 'status', '--porcelain') == 'A  other.txt'


def test_commit_paths_returns_none_when_nothing_changed(trio):
    head = trio.head
    write(trio.work, {'other.txt': 'unrelated\n'})
    assert commit_paths(trio.runner, ['script.py'], 'no change') is None
    assert commit_paths(trio.runner, [], 'no paths') is None          # 空列表不能退化成"全部暂存"
    assert trio.head == head
    assert sh(trio.work, 'status', '--porcelain') == '?? other.txt'


def test_push_reports_success_and_failure(trio, tmp_path):
    trio.fork_commit({'new.txt': 'x\n'})
    assert push(trio.runner, 'custom') == (True, '')
    assert trio.fork_tip == trio.head
    sh(trio.work, 'remote', 'set-url', 'origin', str(tmp_path / 'missing.git'))
    ok, error = push(trio.runner, 'custom')
    assert ok is False and 'missing.git' in error
