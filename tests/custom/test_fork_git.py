"""合并上游的 git 操作层。

全部在 tmp_path 下的本地裸仓库里跑：不联网，不读写本仓库的远端、config/ 和 log/。
每个临时仓库都设了本地身份并关掉 autocrlf 和提交签名，结果不依赖本机的全局 git 配置。
"""
import shutil
import subprocess
from datetime import datetime

import pytest

from module.server.fork_git import (GitRunner, commit_paths, count_behind, ensure_remote, is_asset_path,
                                    merge_upstream, push, upstream_commits)

BASE = {
    'script.py': 'base\n',
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

    def merge(self, regenerate=None, verify=None, upstream=None):
        return merge_upstream(self.runner, upstream or str(self.upstream), 'dev', 'custom',
                              regenerate or Regen(self.work), verify)


class Regen:
    """假的 regenerate：记下被调用的任务名，并像真的一样重写 tasks/<任务>/assets.py。"""

    def __init__(self, repo):
        self.repo, self.calls = repo, []

    def __call__(self, task):
        self.calls.append(task)
        write(self.repo, {f'tasks/{task}/assets.py': f'# regenerated {task}\n'})


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
    assert (result.status, result.commits) == ('up_to_date', 0)
    assert trio.head == before


def test_clean_merge_pushes(trio):
    trio.upstream_commit({'new.txt': 'hello\n'})
    regen = Regen(trio.work)
    result = trio.merge(regen)
    assert (result.status, result.commits) == ('merged', 1)
    assert (result.resolved, result.conflicts, result.requirements_changed) == ([], [], False)
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


def test_push_failure_keeps_local_merge(trio, tmp_path):
    trio.upstream_commit({'new.txt': 'hello\n'})
    before = trio.head
    sh(trio.work, 'remote', 'set-url', 'origin', str(tmp_path / 'missing.git'))
    result = trio.merge()
    assert (result.status, result.commits) == ('push_failed', 1)
    assert 'missing.git' in result.message
    assert trio.head != before
    assert before in sh(trio.work, 'rev-list', '--parents', '-n', '1', 'HEAD').split()
    assert not merging(trio.work)


def test_fetch_failure_reports_and_keeps_head(trio, tmp_path):
    before = trio.head
    result = trio.merge(upstream=str(tmp_path / 'missing.git'))
    assert (result.status, result.commits) == ('fetch_failed', 0)
    assert 'missing.git' in result.message
    assert trio.head == before
    assert not merging(trio.work)


def test_dirty_worktree_refuses(trio):
    trio.upstream_commit({'new.txt': 'hello\n'})
    write(trio.work, {'script.py': 'hand edited\n'})
    before = trio.head
    result = trio.merge()
    assert result.status == 'dirty'
    assert 'script.py' in result.message
    assert trio.head == before
    assert (trio.work / 'script.py').read_bytes() == b'hand edited\n'      # 手改的内容原样保留


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


def test_push_is_never_forced(trio, tmp_path):
    rival = tmp_path / 'rival'
    sh(tmp_path, 'clone', '-q', '-c', 'core.autocrlf=false', str(trio.fork), str(rival))
    configure(rival)
    commit(rival, {'rival.txt': 'someone else\n'}, 'rival')
    sh(rival, 'push', '-q', 'origin', 'HEAD:custom')
    rival_tip = trio.fork_tip

    trio.upstream_commit({'new.txt': 'hello\n'})
    result = trio.merge()
    assert result.status == 'push_failed'
    assert trio.fork_tip == rival_tip                    # 被拒绝的推送没有改动 fork


def test_runner_check_semantics(trio):
    assert trio.runner.run('rev-parse', 'HEAD').stdout.strip() == trio.head
    assert trio.runner.run('rev-parse', '--verify', '-q', 'no-such-ref', check=False).returncode != 0
    with pytest.raises(subprocess.CalledProcessError) as raised:
        trio.runner.run('rev-parse', '--verify', 'no-such-ref')
    assert raised.value.stderr


def test_runner_never_waits_for_input(trio, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', spy)
    trio.runner.run('status')
    env = seen[-1]['env']
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert env['GIT_EDITOR'] == 'true'
    assert seen[-1]['stdin'] == subprocess.DEVNULL
    assert trio.runner.run('var', 'GIT_EDITOR').stdout.strip() == 'true'


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
