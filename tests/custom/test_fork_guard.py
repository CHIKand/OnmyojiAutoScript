import subprocess

from deploy.fork_guard import WARNING_TEMPLATE, local_only_commits


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def commit(work, message):
    sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-m', message, cwd=work)


def repo_with_origin(tmp_path):
    origin, work = tmp_path / 'origin.git', tmp_path / 'work'
    sh('git', 'init', '--bare', str(origin), cwd=tmp_path)
    sh('git', 'clone', str(origin), str(work), cwd=tmp_path)
    commit(work, 'c1')
    sh('git', 'push', 'origin', 'HEAD:master', cwd=work)
    return origin, work


def test_pushed_head_is_not_blocked(tmp_path):
    _, work = repo_with_origin(tmp_path)
    assert local_only_commits('git', str(work)) == []


def test_unpushed_commit_blocks(tmp_path):
    _, work = repo_with_origin(tmp_path)
    commit(work, 'local')
    assert len(local_only_commits('git', str(work))) == 1


def test_first_switch_to_fork_is_not_blocked(tmp_path):
    _, work = repo_with_origin(tmp_path)
    fork = tmp_path / 'fork.git'
    sh('git', 'init', '--bare', str(fork), cwd=tmp_path)
    # 旧 origin/master 远端引用仍在，HEAD 上的提交对远端是已知的
    sh('git', 'remote', 'set-url', 'origin', str(fork), cwd=work)
    assert local_only_commits('git', str(work)) == []


def test_unborn_head_returns_empty(tmp_path):
    # 首次安装时 git_repository_init 面对的是刚 init、还没有任何提交的仓库
    work = tmp_path / 'fresh'
    sh('git', 'init', str(work), cwd=tmp_path)
    assert local_only_commits('git', str(work)) == []


class _InstallerStub:
    """只提供 git_repository_init 用到的成员：git、execute、remove。"""
    git = 'git'

    def execute(self, command, allow_failure=False):
        ok = subprocess.run(command, shell=True, capture_output=True).returncode == 0
        if not ok and not allow_failure:
            raise RuntimeError(command)
        return ok

    def remove(self, file):
        pass


def test_installer_skips_reset_when_unpushed(tmp_path, monkeypatch):
    from deploy.git import GitManager
    origin, work = repo_with_origin(tmp_path)
    commit(work, 'local')
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=work, capture_output=True, text=True).stdout
    monkeypatch.chdir(work)
    GitManager.git_repository_init(_InstallerStub(), repo=str(origin), source='origin', branch='master',
                                   proxy='', ssl_verify=True, keep_changes=False)
    after = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=work, capture_output=True, text=True).stdout
    assert after == head


def test_warning_names_push_command():
    message = WARNING_TEMPLATE.format(count=2, branch='custom')
    assert 'git push origin HEAD:custom' in message
    assert '2' in message
