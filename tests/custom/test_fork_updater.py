"""更新检查与执行（module.server.fork_updater）的测试。

git 操作都在 tmp_path 下的本地仓库里跑（夹具 trio 取自 test_fork_git）：不联网，不读写本仓库的远端、config/ 和 log/。
ForkUpdater 一律显式传入 repo、git、branch：无参构造会读部署配置，而 DeployConfig 一构造就在当前目录建 config/deploy.yaml，
所以无参构造只用假的 DeployConfig 验，两个包装函数（check_update、run_update）用假的 ForkUpdater 验。
上游地址和分支用环境变量指到本地仓库（环境变量优先于 .env，本机有没有 .env 都不影响）。
本文件不导入 module.server.app，也不导入 main_manager（它一导入就扫 config/、起线程）。
用到真 AssetsExtractor 的用例，用例名里别带 temp：它会跳过路径里含 "temp" 的 json，而 pytest 的临时目录以用例名命名。
"""
import ast
import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.server import fork_updater
from module.server.fork_git import GitRunner, MergeResult
from module.server.fork_updater import (DEFAULT_UPSTREAM_BRANCH, DEFAULT_UPSTREAM_URL, MERGE_LOCK, ForkUpdater,
                                        check_update, default_verify, message_of, run_update)
from test_fork_git import is_clean, merging, sh, stall, trio, trio_template, write  # noqa: F401  后三个是夹具

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_NOTE = '；依赖有变化，请在 OASX 重启服务以安装新依赖'
VERIFY_OK = lambda repo: (True, '')  # noqa: E731   换掉默认自检：临时仓库里没有能导入的 script.py


class LogSpy:
    """换掉 fork_updater.logger：按级别记下消息，不往真日志里写。"""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _record(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message, *args, **kwargs):
        self._record('info', message)

    def warning(self, message, *args, **kwargs):
        self._record('warning', message)

    def error(self, message, *args, **kwargs):
        self._record('error', message)

    def exception(self, message, *args, **kwargs):
        self._record('exception', message)

    @property
    def levels(self) -> list[str]:
        return [level for level, _ in self.records]


@pytest.fixture
def log(monkeypatch) -> LogSpy:
    spy = LogSpy()
    monkeypatch.setattr(fork_updater, 'logger', spy)
    return spy


@pytest.fixture
def local_upstream(trio, monkeypatch):
    """ForkUpdater 读到的上游设置指向 trio 的本地上游仓库。"""
    monkeypatch.setenv('OAS_UPSTREAM_URL', str(trio.upstream))
    monkeypatch.setenv('OAS_UPSTREAM_BRANCH', 'dev')
    return trio


def make_updater(trio) -> ForkUpdater:
    return ForkUpdater(repo=trio.work, git='git', branch='custom')


def result(status, commits=0, conflicts=(), message='', requirements=False) -> MergeResult:
    return MergeResult(status, commits, [], list(conflicts), message, requirements)


class BlockedMerge:
    """假的 merge_upstream：进入后停在 release 上，让测试在这段时间里去试第二个调用。"""

    def __init__(self):
        self.entered, self.release, self.order = threading.Event(), threading.Event(), []

    def __call__(self, *args, **kwargs):
        self.order.append('enter')
        self.entered.set()
        assert self.release.wait(10)
        self.order.append('leave')
        return MergeResult('up_to_date', 0, [], [], '')


def in_thread(function, results: list) -> threading.Thread:
    thread = threading.Thread(target=lambda: results.append(function()), daemon=True)
    thread.start()
    return thread


# ---- update_info ----------------------------------------------------------------------------------------------

def test_update_info_shape_and_count(local_upstream):
    trio = local_upstream
    base = trio.head
    trio.upstream_commit({'new1.txt': '1\n'}, 'first upstream change')
    trio.upstream_commit({'new2.txt': '2\n'}, 'second upstream change')
    tip = sh(trio.upstream, 'rev-parse', 'refs/heads/dev')

    info = make_updater(trio).update_info()

    assert set(info) == {'is_update', 'branch', 'current_commit', 'latest_commit', 'commit'}
    assert info['is_update'] is True
    assert info['branch'] == 'custom'
    assert len(info['commit']) == 2
    assert [item[3] for item in info['commit']] == ['second upstream change', 'first upstream change']   # 新的在前
    # 提交信息是 [sha, 作者, ISO 日期, 标题]，三处格式一致；OASX 按这四项逐格显示
    assert info['current_commit'][0] == base and info['current_commit'][1:4:2] == ['tester', 'base']
    assert info['latest_commit'] == info['commit'][0]
    assert info['latest_commit'][0] == tip and info['latest_commit'][3] == 'second upstream change'
    assert all(isinstance(item, list) and len(item) == 4 for item in [*info['commit'], info['current_commit']])
    datetime.fromisoformat(info['latest_commit'][2])
    assert json.loads(json.dumps(info)) == info          # 能直接序列化成响应


def test_update_info_without_new_upstream_commits(local_upstream):
    info = make_updater(local_upstream).update_info()
    assert info['is_update'] is False
    assert info['commit'] == []
    assert info['current_commit'] == info['latest_commit']      # HEAD 就是上游最新，OASX 的两行显示同一个提交


def test_update_info_lists_the_latest_15_commits_newest_first(local_upstream):
    trio = local_upstream
    for number in range(17):
        sh(trio.seed, 'commit', '--allow-empty', '-q', '-m', f'c{number}')
    sh(trio.seed, 'push', '-q', str(trio.upstream), 'HEAD:refs/heads/dev')
    info = make_updater(trio).update_info()
    assert info['is_update'] is True
    assert [item[3] for item in info['commit']] == [f'c{number}' for number in range(16, 1, -1)]   # c16 到 c2，共 15 个


def test_update_info_is_none_when_the_fetch_fails(trio, monkeypatch, tmp_path, log):
    monkeypatch.setenv('OAS_UPSTREAM_URL', str(tmp_path / 'missing.git'))
    monkeypatch.setenv('OAS_UPSTREAM_BRANCH', 'dev')
    before = trio.head
    assert make_updater(trio).update_info() is None
    assert trio.head == before
    assert log.levels == ['error']                       # 取不到上游是预期内的失败：一行错误，不带调用栈


def test_update_info_is_none_when_the_fetch_times_out(local_upstream, stall, log):
    stall('fetch')
    assert make_updater(local_upstream).update_info() is None
    assert 'exception' in log.levels
    assert not MERGE_LOCK.locked()


def test_update_info_never_raises(local_upstream, monkeypatch, log):
    def broken(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(fork_updater, 'count_behind', broken)
    assert make_updater(local_upstream).update_info() is None
    assert 'exception' in log.levels
    assert not MERGE_LOCK.locked()                       # 出错也要放开锁，否则之后的合并永远等不到


def test_upstream_settings_come_from_the_env_file_and_are_read_at_every_call(trio, tmp_path, monkeypatch):
    monkeypatch.delenv('OAS_UPSTREAM_URL', raising=False)
    monkeypatch.delenv('OAS_UPSTREAM_BRANCH', raising=False)
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    env_file = cwd / '.env'
    env_file.write_text(f'OAS_UPSTREAM_URL={trio.upstream}\nOAS_UPSTREAM_BRANCH=dev\n', encoding='utf-8')
    updater = make_updater(trio)

    assert updater.update_info()['is_update'] is False
    env_file.write_text(f'OAS_UPSTREAM_URL={tmp_path / "missing.git"}\n', encoding='utf-8')
    assert updater.update_info() is None                 # 同一个对象，改了 .env 下一次调用就用新值


def test_execute_hands_the_settings_to_merge_upstream(trio, tmp_path, monkeypatch):
    calls = []

    def fake_merge(runner, upstream, upstream_branch, push_branch, regenerate, verify=None):
        calls.append((runner, upstream, upstream_branch, push_branch, regenerate, verify))
        return MergeResult('up_to_date', 0, [], [], '')

    monkeypatch.setattr(fork_updater, 'merge_upstream', fake_merge)
    monkeypatch.delenv('OAS_UPSTREAM_URL', raising=False)
    monkeypatch.delenv('OAS_UPSTREAM_BRANCH', raising=False)
    monkeypatch.chdir(tmp_path)                          # 这里没有 .env，读到的是默认值
    updater = make_updater(trio)

    assert updater.execute().status == 'up_to_date'
    runner, upstream, upstream_branch, push_branch, regenerate, verify = calls[0]
    assert (upstream, upstream_branch, push_branch) == (DEFAULT_UPSTREAM_URL, DEFAULT_UPSTREAM_BRANCH, 'custom')
    assert (DEFAULT_UPSTREAM_URL, DEFAULT_UPSTREAM_BRANCH) == (
        'https://gitcode.com/OnmyojiAutoScript/OnmyojiAutoScript.git', 'dev')
    assert (runner.git, runner.repo) == ('git', trio.work)
    assert regenerate == updater.regenerate and verify is fork_updater.default_verify

    monkeypatch.setenv('OAS_UPSTREAM_URL', 'https://example.invalid/fork.git')
    monkeypatch.setenv('OAS_UPSTREAM_BRANCH', 'main')
    updater.execute()
    assert calls[1][1:4] == ('https://example.invalid/fork.git', 'main', 'custom')


def test_env_example_documents_the_upstream_keys():
    lines = (REPO_ROOT / '.env.example').read_text(encoding='utf-8').splitlines()
    assert f'OAS_UPSTREAM_URL={DEFAULT_UPSTREAM_URL}' in lines              # 示例里的值和代码默认值是同一份
    assert f'OAS_UPSTREAM_BRANCH={DEFAULT_UPSTREAM_BRANCH}' in lines
    assert any(line.lstrip('# ').startswith('OAS_UPSTREAM_MERGE_TIME=') for line in lines)


# ---- execute ---------------------------------------------------------------------------------------------------

def test_execute_merges_upstream_and_pushes_to_the_deploy_branch(local_upstream, monkeypatch):
    trio = local_upstream
    monkeypatch.setattr(fork_updater, 'default_verify', VERIFY_OK)
    trio.upstream_commit({'new.txt': 'hello\n'})
    outcome = make_updater(trio).execute()
    assert (outcome.status, outcome.commits) == ('merged', 1)
    assert (trio.work / 'new.txt').is_file()
    assert trio.fork_tip == trio.head                    # push origin HEAD:custom


def test_execute_regenerates_assets_with_the_real_extractor(local_upstream, monkeypatch):
    trio = local_upstream
    monkeypatch.chdir(trio.work)                         # AssetsExtractor 按当前目录算相对路径
    monkeypatch.setattr(fork_updater, 'default_verify', VERIFY_OK)
    rule = ('[{"description": "旗", "itemName": "flag", "imageName": "a.png", "roiFront": "1,2,3,4", '
            '"roiBack": "5,6,7,8", "method": "Template matching", "threshold": 0.8}]\n')
    trio.upstream_commit({'tasks/Foo/as/image.json': rule})
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": 1}]\n'})            # 两边都改了同一份素材

    outcome = make_updater(trio).execute()

    assert (outcome.status, outcome.resolved) == ('merged', ['tasks/Foo/as/image.json'])
    assets = (trio.work / 'tasks' / 'Foo' / 'assets.py').read_text(encoding='utf-8')
    assert 'class FooAssets' in assets and 'I_FLAG = RuleImage(' in assets
    assert 'file="./tasks/Foo/as/a.png"' in assets
    assert trio.fork_tip == trio.head


def test_regenerate_refuses_to_run_outside_the_repository_root(local_upstream, monkeypatch, tmp_path):
    trio = local_upstream
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(fork_updater, 'default_verify', VERIFY_OK)
    trio.upstream_commit({'tasks/Foo/as/image.json': '[{"v": "upstream"}]\n'})
    trio.fork_commit({'tasks/Foo/as/image.json': '[{"v": "fork"}]\n'})
    before = trio.head

    outcome = make_updater(trio).execute()

    assert outcome.status == 'smoke_failed'              # 目录不对就生成不出可靠的 assets.py：整个合并中止
    assert str(trio.work) in outcome.message
    assert trio.head == before and not merging(trio.work) and is_clean(trio.work)
    assert message_of(outcome, []).startswith('合并上游后自检失败，已中止，分支未改动：')


@pytest.mark.parametrize('name', ['GuildBanquet', 'Component/Buy'])
def test_regenerate_reproduces_the_committed_assets_file(tmp_path, monkeypatch, name):
    repo = tmp_path / 'repo'
    shutil.copytree(REPO_ROOT / 'tasks' / name, repo / 'tasks' / name)
    assets = repo / 'tasks' / name / 'assets.py'
    committed = assets.read_bytes()
    assets.unlink()
    monkeypatch.chdir(repo)

    ForkUpdater(repo=repo, git='git', branch='custom').regenerate(name)    # 普通任务和 Component/<组件> 各取各的目录

    assert assets.read_bytes() == committed


def test_execute_turns_an_unexpected_exception_into_an_error_result(trio, monkeypatch, log):
    def broken(*args, **kwargs):
        raise OSError('git is gone')

    monkeypatch.setattr(fork_updater, 'merge_upstream', broken)
    outcome = make_updater(trio).execute()
    assert outcome.status == 'error' and 'git is gone' in outcome.message
    assert message_of(outcome, []) == '合并上游时出现意外错误，请查看服务日志：OSError: git is gone'
    assert 'exception' in log.levels
    assert not MERGE_LOCK.locked()


def test_a_second_execute_waits_for_the_first(trio, monkeypatch):
    blocked = BlockedMerge()
    monkeypatch.setattr(fork_updater, 'merge_upstream', blocked)
    updater, results = make_updater(trio), []
    first = in_thread(updater.execute, results)
    assert blocked.entered.wait(10)
    second = in_thread(updater.execute, results)
    time.sleep(0.3)                                      # 锁不起作用的话，第二个调用会在这段时间里闯进去
    assert blocked.order == ['enter'] and second.is_alive()
    blocked.release.set()
    first.join(10)
    second.join(10)
    assert blocked.order == ['enter', 'leave', 'enter', 'leave']
    assert [item.status for item in results] == ['up_to_date', 'up_to_date']


def test_update_info_waits_for_a_running_merge(local_upstream, monkeypatch):
    blocked = BlockedMerge()
    monkeypatch.setattr(fork_updater, 'merge_upstream', blocked)
    started, real_ensure_remote = threading.Event(), fork_updater.ensure_remote

    def watching_ensure_remote(*args, **kwargs):          # update_info 动仓库的第一步：没有锁的话它马上就会走到这里
        started.set()
        return real_ensure_remote(*args, **kwargs)

    monkeypatch.setattr(fork_updater, 'ensure_remote', watching_ensure_remote)
    updater, merged, infos = make_updater(local_upstream), [], []
    first = in_thread(updater.execute, merged)
    assert blocked.entered.wait(10)
    second = in_thread(updater.update_info, infos)
    assert not started.wait(0.3)                         # 检查更新不和合并同时动同一个仓库
    assert second.is_alive()
    blocked.release.set()
    first.join(10)
    second.join(10)
    assert started.is_set() and infos[0]['is_update'] is False


# ---- check_update / run_update ---------------------------------------------------------------------------------

def use_updater(monkeypatch, *, info=None, outcome=None, constructor_error=None, delay=0.0):
    """换掉 fork_updater.ForkUpdater，免得构造出真的 DeployConfig。"""
    class FakeUpdater:
        def __init__(self):
            if constructor_error:
                raise constructor_error

        def update_info(self):
            time.sleep(delay)
            return info

        def execute(self):
            return outcome

    monkeypatch.setattr(fork_updater, 'ForkUpdater', FakeUpdater)


def use_restart(monkeypatch, names=('oas1', 'oas2'), error=None) -> list[str]:
    calls: list[str] = []

    async def restart():
        calls.append('restart')
        if error:
            raise error
        return list(names)

    monkeypatch.setattr(fork_updater, 'restart_running_instances', restart)
    return calls


def test_check_update_returns_the_update_info(monkeypatch):
    use_updater(monkeypatch, info={'is_update': True})
    assert asyncio.run(check_update()) == {'is_update': True}
    use_updater(monkeypatch, info=None)
    assert asyncio.run(check_update()) is None


def test_check_update_runs_off_the_event_loop(monkeypatch):
    use_updater(monkeypatch, info={'is_update': False}, delay=0.3)      # 在事件循环线程里 sleep 的话，计时协程一次都跑不到
    ticks = []

    async def scenario():
        async def ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks.append(1)

        task = asyncio.create_task(ticker())
        value = await check_update()
        task.cancel()
        return value

    assert asyncio.run(scenario()) == {'is_update': False}
    assert len(ticks) >= 5


def test_check_update_never_raises(monkeypatch, log):
    use_updater(monkeypatch, constructor_error=RuntimeError('no deploy config'))
    assert asyncio.run(check_update()) is None
    assert 'exception' in log.levels


@pytest.mark.parametrize('status, restarts', [
    ('merged', True), ('pushed', True),
    ('up_to_date', False), ('push_failed', False), ('conflict_aborted', False), ('dirty', False),
    ('fetch_failed', False), ('smoke_failed', False), ('error', False),
])
def test_run_update_restarts_instances_only_after_a_successful_push(monkeypatch, status, restarts):
    use_updater(monkeypatch, outcome=result(status, 2, message='detail'))
    calls = use_restart(monkeypatch)
    message = asyncio.run(run_update())
    assert calls == (['restart'] if restarts else [])
    assert ('已重启实例：oas1、oas2' in message) is restarts
    assert ('已重启实例' in message) is restarts


def test_run_update_reports_the_names_that_were_restarted(monkeypatch):
    use_updater(monkeypatch, outcome=result('merged', 3))
    use_restart(monkeypatch, names=())
    assert asyncio.run(run_update()) == '已合并上游 3 个提交并推送到 fork；已重启实例：无'


def test_run_update_runs_the_merge_off_the_event_loop(monkeypatch):
    loop_thread, merge_thread = [], []

    class Updater:
        def execute(self):
            merge_thread.append(threading.get_ident())
            return result('up_to_date')

    monkeypatch.setattr(fork_updater, 'ForkUpdater', Updater)

    async def scenario():
        loop_thread.append(threading.get_ident())
        return await run_update()

    assert asyncio.run(scenario()) == '已是最新，无需合并'
    assert merge_thread and merge_thread != loop_thread


def test_run_update_never_raises(monkeypatch, log):
    use_updater(monkeypatch, constructor_error=RuntimeError('no deploy config'))
    calls = use_restart(monkeypatch)
    message = asyncio.run(run_update())
    assert message == '合并上游时出现意外错误，请查看服务日志：RuntimeError: no deploy config'
    assert calls == [] and 'exception' in log.levels


def test_run_update_keeps_the_merge_message_when_the_restart_fails(monkeypatch, log):
    use_updater(monkeypatch, outcome=result('merged', 3))
    use_restart(monkeypatch, error=RuntimeError('no manager'))
    assert asyncio.run(run_update()) == '已合并上游 3 个提交并推送到 fork；已重启实例：无'   # 合并和推送已经成功，不能报成失败
    assert 'exception' in log.levels


def test_home_router_handlers_delegate_to_fork_updater(monkeypatch):
    """上游的 home_router 一导入就会带进 main_manager 等重模块，换成空壳再导入；导入完立刻从 sys.modules 里撤掉。"""
    for name, stub in {'module.server.main_manager': SimpleNamespace(MainManager=object),
                       'module.server.updater': SimpleNamespace(Updater=object),
                       'module.ocr.rpc': SimpleNamespace(shutdown_ocr_server=lambda: None)}.items():
        monkeypatch.setitem(sys.modules, name, stub)
    sys.modules.pop('module.server.home_router', None)
    try:
        home_router = importlib.import_module('module.server.home_router')
        tree = ast.parse(Path(home_router.__file__).read_text(encoding='utf-8'))
    finally:
        sys.modules.pop('module.server.home_router', None)

    handlers = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    assert len(handlers['update_info'].body) == 2 and len(handlers['execute_update'].body) == 2   # 上游文件只动这两个函数体
    assert not [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                and 'fork_updater' in ast.dump(node)]                                           # 不新增模块级导入

    async def fake_check():
        return {'is_update': False}

    async def fake_run():
        return '已是最新，无需合并'

    monkeypatch.setattr(fork_updater, 'check_update', fake_check)
    monkeypatch.setattr(fork_updater, 'run_update', fake_run)
    assert asyncio.run(home_router.update_info()) == {'is_update': False}
    assert asyncio.run(home_router.execute_update()) == '已是最新，无需合并'


# ---- message_of ------------------------------------------------------------------------------------------------

def test_message_texts():
    assert message_of(result('up_to_date'), []) == '已是最新，无需合并'
    assert message_of(result('merged', 3), ['oas1', 'oas2']) == '已合并上游 3 个提交并推送到 fork；已重启实例：oas1、oas2'
    assert message_of(result('merged', 3), []) == '已合并上游 3 个提交并推送到 fork；已重启实例：无'
    assert message_of(result('conflict_aborted', conflicts=['module/a.py', 'b.py']), []) == \
        '合并上游遇到代码冲突，已中止，分支未改动：module/a.py、b.py'
    assert message_of(result('push_failed', 2, message='fatal: denied'), []) == \
        '已在本地合并上游 2 个提交，但推送失败：fatal: denied。启动器守卫会保护本地提交，请检查网络或令牌后重试'
    assert message_of(result('merged', 1, requirements=True), ['oas1']) == \
        '已合并上游 1 个提交并推送到 fork；已重启实例：oas1' + DEPENDENCY_NOTE


@pytest.mark.parametrize('status', ['up_to_date', 'merged', 'pushed', 'conflict_aborted', 'push_failed', 'dirty',
                                    'fetch_failed', 'smoke_failed', 'error'])
def test_the_dependency_note_follows_requirements_changed_for_every_status(status):
    plain = message_of(result(status, 1, message='x\ny'), ['oas1'])
    flagged = message_of(result(status, 1, message='x\ny', requirements=True), ['oas1'])
    assert not plain.endswith(DEPENDENCY_NOTE)
    assert flagged == plain + DEPENDENCY_NOTE


def test_message_texts_for_pushed():
    assert message_of(result('pushed', 4), ['oas1']) == '已把之前未推送的 4 个提交推送到 fork；已重启实例：oas1'
    assert message_of(result('pushed', 0), []) == '已把本地提交推送到 fork；已重启实例：无'


def test_message_texts_for_dirty():
    assert message_of(result('dirty', message=' M a.py\nA  b.py'), []) == '运行目录有未提交的改动，已跳过合并：M a.py\nA  b.py'
    lines = [f' M tasks/file{number}.py' for number in range(12)]
    shown = '\n'.join(f'M tasks/file{number}.py' for number in range(10))
    assert message_of(result('dirty', message='\n'.join(lines)), []) == \
        f'运行目录有未提交的改动，已跳过合并：{shown}\n等 2 项'                      # 只留前 10 行，多出来的行数记在末尾
    exactly_ten = message_of(result('dirty', message='\n'.join(lines[:10])), [])
    assert exactly_ten == f'运行目录有未提交的改动，已跳过合并：{shown}'


def test_message_texts_for_fetch_and_smoke_failures():
    assert message_of(result('fetch_failed', message='fatal: first\n\nfatal: unable to access x\n'), []) == \
        '拉取上游失败，未改动任何文件：fatal: unable to access x'                      # 只留最后一个非空行
    assert message_of(result('smoke_failed', message='ModuleNotFoundError: No module named x'), []) == \
        '合并上游后自检失败，已中止，分支未改动：ModuleNotFoundError: No module named x'


def test_conflict_text_falls_back_to_the_git_message_without_conflict_files():
    # 合并没能开始（比如有未跟踪文件会被覆盖）时 conflicts 为空，{files} 处改放 git 的说明，免得句子在冒号处断掉
    text = message_of(result('conflict_aborted', message='error: untracked working tree files would be overwritten'), [])
    assert text == '合并上游遇到代码冲突，已中止，分支未改动：error: untracked working tree files would be overwritten'


def test_message_for_an_unexpected_error():
    assert message_of(result('error', message='OSError: boom'), []) == '合并上游时出现意外错误，请查看服务日志：OSError: boom'


# ---- git 可执行文件 --------------------------------------------------------------------------------------------

def test_no_argument_construction_reads_the_deploy_config(tmp_path, monkeypatch):
    from module.server import config as server_config
    git_exe = tmp_path / 'toolkit' / 'git.exe'
    git_exe.parent.mkdir()
    git_exe.write_bytes(b'')

    class FakeDeployConfig:
        Branch = 'custom'

        def filepath(self, key):
            assert key == 'GitExecutable'
            return str(git_exe)

    monkeypatch.setattr(server_config, 'DeployConfig', FakeDeployConfig)
    monkeypatch.chdir(tmp_path)
    updater = ForkUpdater()
    assert (updater.git, updater.branch, updater.repo) == (str(git_exe), 'custom', Path.cwd())


def test_a_missing_configured_git_falls_back_to_the_system_git(tmp_path, log):
    system_git = shutil.which('git')
    updater = ForkUpdater(repo=tmp_path, git=str(tmp_path / 'toolkit' / 'git.exe'), branch='custom')
    assert updater.git == system_git and updater.runner.git == system_git
    assert any(system_git in text for _, text in log.records)       # 日志里写明用的是哪个 git


def test_an_existing_configured_git_and_a_bare_command_are_kept(tmp_path, log):
    git_exe = tmp_path / 'bin' / 'git.exe'
    git_exe.parent.mkdir()
    git_exe.write_bytes(b'')
    assert ForkUpdater(repo=tmp_path, git=str(git_exe), branch='custom').git == str(git_exe)
    assert ForkUpdater(repo=tmp_path, git='git', branch='custom').git == 'git'     # 不带路径的命令名交给 PATH，不核对存在
    assert log.records == []


def test_without_any_git_the_configured_path_is_kept_and_a_warning_logged(tmp_path, monkeypatch, log):
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    configured = str(tmp_path / 'toolkit' / 'git.exe')
    assert ForkUpdater(repo=tmp_path, git=configured, branch='custom').git == configured
    assert 'warning' in log.levels


def test_runner_puts_the_git_directory_first_on_path(tmp_path, monkeypatch):
    git = tmp_path / 'toolkit' / 'Git' / 'mingw64' / 'bin' / 'git.exe'      # 不必真存在：子进程被换成了记录器
    seen = []

    def fake_run(command, **kwargs):
        seen.append(kwargs['env']['PATH'])
        return subprocess.CompletedProcess(command, 0, '', '')

    class FakePopen:
        returncode = 0

        def __init__(self, command, **kwargs):
            seen.append(kwargs['env']['PATH'])

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(subprocess, 'Popen', FakePopen)
    runner = GitRunner(str(git), tmp_path)
    runner.run('status')                                 # 本地命令
    runner.run('fetch', 'upstream', 'dev')               # 联网命令走另一条路径
    expected = os.pathsep.join([str(git.parent), os.environ['PATH']])
    assert seen == [expected, expected]                  # 工具链的 git 要靠同目录的 git-remote-https 才能联网


def test_runner_leaves_path_alone_for_a_bare_git_command(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: seen.append(kwargs['env']['PATH']) or
                        subprocess.CompletedProcess(command, 0, '', ''))
    GitRunner('git', tmp_path).run('status')
    assert seen == [os.environ['PATH']]


def test_runner_resolves_a_relative_git_path_against_the_server_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: seen.append(kwargs['env']['PATH']) or
                        subprocess.CompletedProcess(command, 0, '', ''))
    elsewhere = tmp_path / 'repo'
    GitRunner('./toolkit/Git/bin/git.exe', elsewhere).run('status')
    first = seen[0].split(os.pathsep)[0]
    assert os.path.isabs(first) and Path(first).parts[-3:] == ('toolkit', 'Git', 'bin')    # 不能依赖子进程的工作目录


# ---- 默认自检 --------------------------------------------------------------------------------------------------

def smoke_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """三个被自检导入的模块的最小替身；files 里给出的文件换成那份内容。"""
    base = {'script.py': '', 'module/__init__.py': '', 'module/server/__init__.py': '',
            'module/server/custom_routes.py': '', 'module/orchestra/__init__.py': '', 'module/orchestra/runtime.py': ''}
    write(root, {**base, **(files or {})})
    return root


def test_default_verify_passes_when_the_modules_import(tmp_path):
    assert default_verify(smoke_repo(tmp_path)) == (True, '')


@pytest.mark.parametrize('broken', ['script.py', 'module/server/custom_routes.py', 'module/orchestra/runtime.py'])
def test_default_verify_fails_when_any_of_the_three_imports_raises(tmp_path, broken):
    ok, reason = default_verify(smoke_repo(tmp_path, {broken: 'raise RuntimeError("boom")\n'}))
    assert ok is False
    assert 'RuntimeError: boom' in reason


def test_default_verify_keeps_chinese_error_text_readable(tmp_path):
    ok, reason = default_verify(smoke_repo(tmp_path, {'script.py': 'raise RuntimeError("自检用的中文报错")\n'}))
    assert ok is False
    assert 'RuntimeError: 自检用的中文报错' in reason      # 子进程在中文 Windows 上默认写 GBK，没有定成 UTF-8 就会乱码


def test_default_verify_returns_only_the_last_lines_of_stderr(tmp_path):
    noisy = 'import sys\nsys.stderr.write("\\n".join(f"line{n}" for n in range(50)) + "\\n")\nraise SystemExit(1)\n'
    ok, reason = default_verify(smoke_repo(tmp_path, {'script.py': noisy}))
    lines = reason.splitlines()
    assert ok is False and len(lines) == 20
    assert lines[-1] == 'line49' and lines[0] == 'line30'


def test_default_verify_runs_in_the_repository_with_a_bounded_wait(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert default_verify(tmp_path) == (False, 'timed out')
    assert seen['cwd'] == tmp_path and seen['timeout'] == 120
    assert seen['env']['GIT_TERMINAL_PROMPT'] == '0'
    assert seen['stdin'] == subprocess.DEVNULL
    assert seen['command'][1] == '-c'
    code = seen['command'][2]
    assert 'import script' in code and 'import module.server.custom_routes' in code
    assert 'import module.orchestra.runtime' in code


def test_default_verify_prefers_python_exe_over_pythonw_exe(tmp_path, monkeypatch):
    pythonw, python = tmp_path / 'pythonw.exe', tmp_path / 'python.exe'
    pythonw.write_bytes(b'')
    python.write_bytes(b'')
    monkeypatch.setattr(sys, 'executable', str(pythonw))
    commands = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: commands.append(command[0]) or
                        subprocess.CompletedProcess(command, 0, '', ''))
    assert default_verify(tmp_path) == (True, '')
    python.unlink()
    assert default_verify(tmp_path) == (True, '')
    assert commands == [str(python), str(pythonw)]       # pythonw 没有 stderr，抓不到报错；只在没有 python.exe 时才退回它


def test_the_repository_defaults_to_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ForkUpdater(git='git', branch='custom').repo == Path.cwd()
