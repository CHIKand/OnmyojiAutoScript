"""编排接口（module.server.orchestra_router）、实例重启（module.server.instance_control）与路由注册的测试。

处理函数用 asyncio.run 直接调，不起 FastAPI 的 TestClient（它依赖的 httpx 没装）。
每个用例都先 chdir 到 tmp_path，里面有 config/template.json 与 config/t.json，不碰仓库自己的 config/ 和 log/。
上游的 MainManager 在导入时就会扫 config/、建进程对象、起推送线程，所以本文件不导入 module.server.main_manager。
编排命令会停、启实例，每个用例默认用一张假的进程表（fake_instances），建真的进程对象会直接报错；
重启相关的用例再换上自己的假进程表。命令启停实例的流程见 test_router_commands.py。
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.routing import Mount

from module.config import config_model
from module.config.config_model import ConfigModel
from module.orchestra.engine import sync_controls
from module.orchestra.progress import Progress, progress_path, save_progress
from module.server import instance_control, orchestra_router
from module.server.custom_routes import register_custom
from module.server.instance_control import (instance_lock, instance_names, post_restart, restart_instance,
                                            restart_running_instances, start_instance)
from module.server.orchestra_router import get_orchestra, post_command, put_orchestra
from module.server.script_process import ScriptState

REPO_ROOT = Path(__file__).resolve().parents[3]
MESSAGE_OUT_OF_RANGE = '次数需在 1–30 之间'   # 破折号是 en dash，同 module.orchestra.model 里的文案


@pytest.fixture(scope='module')
def default_config() -> dict:
    """一份默认实例配置的字典。上游默认值里有写成字符串的整数字段，dump 时 pydantic 会警告，与本文件无关，只在这里放行。"""
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Pydantic serializer warnings', category=UserWarning)
        return ConfigModel().model_dump()


@pytest.fixture
def instance_dir(tmp_path, monkeypatch, default_config) -> Path:
    """cwd 切到 tmp_path，里面是 config/template.json（仓库模板的副本）和实例配置 config/t.json。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config').mkdir()
    shutil.copy(REPO_ROOT / 'config' / 'template.json', tmp_path / 'config' / 'template.json')
    ConfigModel.write_json('t', default_config)
    return tmp_path


def config_bytes(name: str = 't') -> bytes:
    return (Path('config') / f'{name}.json').read_bytes()


def stored(name: str = 't'):
    """从磁盘读回的 orchestra 配置。"""
    return ConfigModel(name).orchestra


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob('*'))


class FakeProcess:
    """ScriptProcess 的替身：stop、start 都是 async，按调用顺序记账。"""

    def __init__(self, state: ScriptState = ScriptState.RUNNING):
        self.state = state
        self.calls: list[str] = []

    async def stop(self) -> None:
        await asyncio.sleep(0)   # 让出一次事件循环：调用方不 await 的话，这行之后的记账会晚于 start
        self.calls.append('stop')

    async def start(self) -> None:
        self.calls.append('start')


class SlowProcess(FakeProcess):
    """stop 分两步、中间两次让出事件循环：两次重启若交错进行，记账里会出现两个 stop:begin 挨在一起。"""

    async def stop(self) -> None:
        self.calls.append('stop:begin')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.calls.append('stop:end')


def use_fake_manager(monkeypatch, **processes: FakeProcess) -> None:
    manager = SimpleNamespace(script_process=dict(processes))
    monkeypatch.setattr(instance_control, '_manager', lambda: manager)


@pytest.fixture(autouse=True)
def fake_instances(monkeypatch) -> None:
    """默认的假进程表：实例 t 停着。命令接口会停、启实例，真的进程表会导入上游 MainManager、真的启动实例进程。"""
    use_fake_manager(monkeypatch, t=FakeProcess(ScriptState.INACTIVE))

    def no_real_process(name: str):
        raise AssertionError(f'测试里不该建真的进程对象：{name}')
    monkeypatch.setattr(instance_control, 'ScriptProcess', no_real_process)


def resting(**kwargs) -> None:
    """实例的进度文件：正在休息。"""
    save_progress(progress_path('t'), Progress(phase='resting', row_index=1,
                                               rest_until=datetime.now() + timedelta(minutes=5), **kwargs))


# ---------- GET ----------

def test_get_returns_catalog_settings_progress(instance_dir):
    data = asyncio.run(get_orchestra('t'))
    assert set(data) == {'settings', 'progress', 'catalog'}
    assert len(data['catalog']) == 17
    assert data['progress']['phase'] == 'idle'
    assert data['settings']['rows'] == []
    assert data['settings']['state'] == 'idle'


def test_get_reports_the_progress_file(instance_dir):
    rest_until = datetime.now() + timedelta(minutes=2)
    save_progress(progress_path('t'), Progress(phase='resting', row_index=1, rest_until=rest_until))
    progress = asyncio.run(get_orchestra('t'))['progress']
    assert progress['phase'] == 'resting' and progress['row_index'] == 1
    assert 0 < progress['rest_remaining_seconds'] <= 120


def test_get_answers_503_while_the_progress_file_stays_locked(instance_dir, monkeypatch):
    def locked(path):
        raise PermissionError('占用中')

    monkeypatch.setattr(orchestra_router, 'load_progress', locked)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(get_orchestra('t'))
    assert caught.value.status_code == 503


# ---------- PUT ----------

def test_put_rejects_invalid_rows(instance_dir):
    before = config_bytes()
    result = asyncio.run(put_orchestra('t', {'rows': [{'task': 'RealmRaid', 'max_count': 31}]}))
    assert result == {'ok': False, 'errors': [{'index': 0, 'field': 'max_count', 'message': MESSAGE_OUT_OF_RANGE}]}
    assert config_bytes() == before


@pytest.mark.parametrize('body, error', [
    ({'rows': [{'task': 'Orochi', 'max_count': 'abc'}]}, {'index': 0, 'field': 'max_count'}),
    ({'rows': [{'task': 'Orochi'}, {'max_count': 3}]}, {'index': 1, 'field': 'task'}),
    ({'rows': [5]}, {'index': -1, 'field': 'rows'}),
    ({'rows': 'zzz'}, {'index': -1, 'field': 'rows'}),
    ({'default_rest_minutes': 'abc'}, {'index': -1, 'field': 'default_rest_minutes'}),
    ({'cut_in_tasks': [1, 2]}, {'index': -1, 'field': 'cut_in_tasks'}),   # 两项出错，只报一条
])
def test_put_maps_type_errors_and_keeps_the_file(instance_dir, body, error):
    before = config_bytes()
    result = asyncio.run(put_orchestra('t', body))
    assert result == {'ok': False, 'errors': [{**error, 'message': '取值不合法'}]}
    assert config_bytes() == before


def test_put_rejects_invalid_settings(instance_dir):
    before = config_bytes()
    result = asyncio.run(put_orchestra('t', {'after_finish': 'forever'}))
    assert result == {'ok': False, 'errors': [{'index': -1, 'field': 'after_finish', 'message': '取值不合法'}]}
    assert config_bytes() == before


def test_put_saves_rows_and_settings(instance_dir):
    rows = [{'task': 'Orochi', 'max_count': 3, 'max_minutes': 20, 'rest_minutes': 2},
            {'task': 'ActivityShikigami', 'sub1': 'boss'}]
    settings = {'after_finish': 'loop', 'total_minutes_cap': 600, 'default_rest_minutes': 8, 'rest_jitter': True,
                'resume_policy': 'rerun', 'cut_in_tasks': ['MetaDemon']}
    first = asyncio.run(put_orchestra('t', {'rows': rows, **settings}))
    assert first == {'ok': True, 'errors': []}
    saved = stored()
    assert [(r.task, r.sub1, r.max_count, r.max_minutes, r.rest_minutes) for r in saved.rows] == [
        ('Orochi', None, 3, 20, 2), ('ActivityShikigami', 'boss', None, None, None)]
    assert {key: getattr(saved, key) for key in settings} == settings

    second = asyncio.run(put_orchestra('t', {'rows': [{'task': 'Orochi'}]}))   # 没带的设置项保持原样
    assert second == {'ok': True, 'errors': []}
    saved = stored()
    assert [r.task for r in saved.rows] == ['Orochi']
    assert {key: getattr(saved, key) for key in settings} == settings


def test_put_keeps_state(instance_dir):
    assert asyncio.run(post_command('t', {'action': 'start'})) == {'ok': True}
    result = asyncio.run(put_orchestra('t', {'rows': [{'task': 'Orochi', 'max_count': 3}]}))
    assert result == {'ok': True, 'errors': []}
    saved = stored()
    assert saved.state == 'running'
    assert [r.task for r in saved.rows] == ['Orochi']


def test_put_ignores_control_fields_in_the_body(instance_dir):
    body = {'rows': [{'task': 'Orochi'}], 'state': 'paused', 'command': 'skip_rest', 'command_seq': 9}
    assert asyncio.run(put_orchestra('t', body)) == {'ok': True, 'errors': []}
    saved = stored()
    assert (saved.state, saved.command, saved.command_seq) == ('idle', '', 0)


def test_put_keeps_what_the_instance_changed_during_validation(instance_dir, monkeypatch):
    real_validate = orchestra_router.validate_settings

    def validate_while_instance_moves_on(candidate):
        # 校验这一刻，实例进程改了运行控制字段（比如引擎把编排暂停了，用户又点了跳过休息）
        cfg = ConfigModel('t')
        cfg.orchestra.state = 'paused'
        cfg.orchestra.command = 'skip_rest'
        cfg.orchestra.command_seq = 5
        cfg.save()
        return real_validate(candidate)

    monkeypatch.setattr(orchestra_router, 'validate_settings', validate_while_instance_moves_on)
    assert asyncio.run(put_orchestra('t', {'rows': [{'task': 'Orochi'}]})) == {'ok': True, 'errors': []}
    saved = stored()
    assert (saved.state, saved.command, saved.command_seq) == ('paused', 'skip_rest', 5)
    assert [r.task for r in saved.rows] == ['Orochi']


# ---------- POST command ----------

def test_state_commands_walk_through_the_states(instance_dir):
    # 开始另外记一条 start 命令（序号加 1），暂停、继续、停止只改 state
    for action, state in [('start', 'running'), ('pause', 'paused'), ('resume', 'running'), ('stop', 'idle')]:
        assert asyncio.run(post_command('t', {'action': action})) == {'ok': True}
        saved = stored()
        assert saved.state == state, action
        assert (saved.command, saved.command_seq) == ('start', 1), action


def test_skip_rest_increments_seq(instance_dir):
    resting()   # 只有休息中才接受跳过休息
    for _ in range(2):
        assert asyncio.run(post_command('t', {'action': 'skip_rest'})) == {'ok': True}
    saved = stored()
    assert saved.command_seq == 2
    assert saved.command == 'skip_rest'
    assert saved.state == 'idle'


def test_every_start_is_a_new_command(instance_dir):
    for seq in (1, 2, 3):
        assert asyncio.run(post_command('t', {'action': 'start'})) == {'ok': True}
        saved = stored()
        assert (saved.state, saved.command, saved.command_seq) == ('running', 'start', seq)


def test_stop_then_start_before_the_instance_reads_still_restarts_the_sequence(instance_dir):
    """实例在两次点击之间没读到配置：光看 state 还是 running，序列会接着跑；start 命令让它从头开始（规格 §8.3 停止即清零）。"""
    assert asyncio.run(put_orchestra('t', {'rows': [{'task': 'Orochi'}] * 3}))['ok']
    assert asyncio.run(post_command('t', {'action': 'start'})) == {'ok': True}
    mid_sequence = Progress(phase='resting', row_index=2, sequence_started_at=datetime(2026, 11, 17, 8, 0),
                            rest_until=datetime(2026, 11, 17, 9, 0), last_command_seq=stored().command_seq)
    for action in ('stop', 'start'):   # 实例已处理过第一次开始，正在第 3 行之前休息
        assert asyncio.run(post_command('t', {'action': action})) == {'ok': True}
    now = datetime(2026, 11, 17, 8, 30)
    p = sync_controls(stored(), mid_sequence, now)
    assert (p.phase, p.row_index, p.sequence_started_at) == ('ready', 0, now)


@pytest.mark.parametrize('phase', [None, 'idle', 'ready', 'row_running', 'interrupted', 'done'])
def test_skip_rest_is_refused_unless_resting(instance_dir, phase):
    if phase is not None:   # None：还没有进度文件
        save_progress(progress_path('t'), Progress(phase=phase))
    before = config_bytes()
    assert asyncio.run(post_command('t', {'action': 'skip_rest'})) == {'ok': False}
    assert config_bytes() == before   # 一行在跑时点的跳过，不能留到下一次休息才生效


def test_skip_rest_while_paused_in_a_rest_is_accepted(instance_dir):
    save_progress(progress_path('t'), Progress(phase='resting', paused=True, rest_remaining=120.0))
    assert asyncio.run(post_command('t', {'action': 'skip_rest'})) == {'ok': True}   # 继续之后生效


def test_skip_rest_is_refused_while_a_start_is_pending(instance_dir):
    resting()   # 上一个序列的休息
    assert asyncio.run(post_command('t', {'action': 'start'})) == {'ok': True}   # 实例还没读到这次开始
    before = config_bytes()
    assert asyncio.run(post_command('t', {'action': 'skip_rest'})) == {'ok': False}   # 否则会把 start 命令顶掉
    assert config_bytes() == before
    resting(last_command_seq=1)   # 实例处理了开始，又进入了新序列的休息
    assert asyncio.run(post_command('t', {'action': 'skip_rest'})) == {'ok': True}
    assert (stored().command, stored().command_seq) == ('skip_rest', 2)


def test_skip_rest_is_refused_when_the_progress_cannot_be_read(instance_dir, monkeypatch):
    resting()

    def locked(path):
        raise PermissionError('占用中')
    monkeypatch.setattr(orchestra_router, 'load_progress', locked)
    before = config_bytes()
    assert asyncio.run(post_command('t', {'action': 'skip_rest'})) == {'ok': False}
    assert config_bytes() == before


def test_a_command_waits_for_a_put_that_is_writing(instance_dir, monkeypatch):
    """PUT 重读配置之后、存盘之前来了开始：两个接口之间不串行的话，PUT 存盘会把开始冲掉。"""
    real_read = config_model.read_file
    reread, release = threading.Event(), threading.Event()
    put_reads = []

    def read(path):
        data = real_read(path)
        if threading.current_thread().name == 'put':
            put_reads.append(path)
            if len(put_reads) == 2:   # 第一次读是校验用的，第二次是写之前的重读
                reread.set()
                release.wait(5)
        return data
    monkeypatch.setattr(config_model, 'read_file', read)
    results = {}
    put = threading.Thread(name='put', target=lambda: results.update(
        put=orchestra_router._write_settings('t', {'rows': [{'task': 'Orochi'}]})))
    command = threading.Thread(name='command', target=lambda: results.update(
        command=asyncio.run(post_command('t', {'action': 'start'}))))
    put.start()
    try:
        assert reread.wait(5)
        command.start()
        command.join(0.3)
        waited = command.is_alive()   # PUT 还在读改写：开始要等它存完
    finally:
        release.set()
        put.join(5)
        if command.is_alive() or command.ident is not None:
            command.join(5)
    assert waited
    assert results == {'put': {'ok': True, 'errors': []}, 'command': {'ok': True}}
    saved = stored()
    assert [r.task for r in saved.rows] == ['Orochi']
    assert (saved.state, saved.command, saved.command_seq) == ('running', 'start', 1)


@pytest.mark.parametrize('body', [{'action': 'explode'}, {'action': 'START'}, {'action': None}, {'action': ['start']}, {}])
def test_unknown_action_is_rejected(instance_dir, body):
    before = config_bytes()
    assert asyncio.run(post_command('t', body)) == {'ok': False}
    assert config_bytes() == before


# ---------- 不认识的实例名 ----------

@pytest.mark.parametrize('name', ['nope', 'template', '../t', '..\\t'])
def test_unknown_name_is_rejected_without_touching_any_path(instance_dir, monkeypatch, name):
    def manager_must_not_be_asked():
        raise AssertionError('名字没通过核对，不该去问进程表')

    monkeypatch.setattr(instance_control, '_manager', manager_must_not_be_asked)
    files_before = tree(instance_dir)
    t_before, template_before = config_bytes('t'), config_bytes('template')

    with pytest.raises(HTTPException) as caught:
        asyncio.run(get_orchestra(name))
    assert caught.value.status_code == 404
    put = asyncio.run(put_orchestra(name, {'rows': [{'task': 'Orochi'}]}))
    assert put['ok'] is False and put['errors']
    assert asyncio.run(post_command(name, {'action': 'start'})) == {'ok': False}
    assert asyncio.run(post_restart(name)) == {'ok': False}

    assert tree(instance_dir) == files_before   # 没有新建配置、进度文件或 log 目录
    assert (config_bytes('t'), config_bytes('template')) == (t_before, template_before)


def test_instance_names_lists_configs_without_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert instance_names() == set()   # 没有 config/ 目录：既不报错，也不像上游的 all_script_files 那样造 oas1
    assert tree(tmp_path) == []
    (tmp_path / 'config').mkdir()
    for file_name in ('template.json', 'a.json', 'b.c.json', 'a.json.lock', 'deploy.yaml'):
        (tmp_path / 'config' / file_name).write_text('{}', encoding='utf-8')
    assert instance_names() == {'a', 'b.c'}


# ---------- 重启 ----------

def test_restart_awaits_stop_then_start(monkeypatch):
    process = FakeProcess()
    use_fake_manager(monkeypatch, t=process)
    assert asyncio.run(restart_instance('t')) is True
    assert process.calls == ['stop', 'start']


def test_restart_of_unknown_process_is_false(monkeypatch):
    use_fake_manager(monkeypatch)
    assert asyncio.run(restart_instance('ghost')) is False


def test_restart_running_instances_skips_inactive(monkeypatch):
    processes = {'a': FakeProcess(ScriptState.RUNNING), 'b': FakeProcess(ScriptState.INACTIVE),
                 'c': FakeProcess(ScriptState.WARNING), 'd': FakeProcess(ScriptState.UPDATING)}
    use_fake_manager(monkeypatch, **processes)
    assert asyncio.run(restart_running_instances()) == ['a', 'c', 'd']
    assert processes['b'].calls == []
    assert [processes[name].calls for name in 'acd'] == [['stop', 'start']] * 3


def test_overlapping_restarts_of_one_instance_take_turns(monkeypatch):
    process = SlowProcess()
    use_fake_manager(monkeypatch, t=process)

    async def twice():
        return await asyncio.gather(restart_instance('t'), restart_instance('t'))
    assert asyncio.run(twice()) == [True, True]
    assert process.calls == ['stop:begin', 'stop:end', 'start'] * 2   # 第二次等第一次 start 完才 stop


def test_restart_locks_follow_the_running_event_loop(monkeypatch):
    process = SlowProcess()
    use_fake_manager(monkeypatch, t=process)

    async def twice():
        return await asyncio.gather(restart_instance('t'), restart_instance('t'))
    for _ in range(2):   # 每次 asyncio.run 是新的事件循环；一把跨循环共用的锁在第二次争用时会报"绑在别的事件循环上"
        assert asyncio.run(twice()) == [True, True]
    assert process.calls == ['stop:begin', 'stop:end', 'start'] * 4


def test_restart_running_instances_survives_a_failing_instance(monkeypatch, caplog):
    class Broken(FakeProcess):
        async def stop(self) -> None:
            raise RuntimeError('进程停不下来')

    processes = {'a': FakeProcess(), 'b': Broken(), 'c': FakeProcess()}
    use_fake_manager(monkeypatch, **processes)
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(restart_running_instances()) == ['a', 'c']   # 只列出重启成了的
    assert [processes[name].calls for name in 'ac'] == [['stop', 'start']] * 2
    assert any('[b]' in r.getMessage() and r.exc_info for r in caplog.records if r.levelno >= logging.ERROR)


def test_restart_endpoint(instance_dir, monkeypatch):
    process = FakeProcess()
    use_fake_manager(monkeypatch, t=process)
    assert asyncio.run(post_restart('t')) == {'ok': True}
    assert process.calls == ['stop', 'start']

    use_fake_manager(monkeypatch)   # 配置在，进程表里没有（比如服务启动后才新建、还没启动过的实例）
    assert asyncio.run(post_restart('t')) == {'ok': False}


def test_restart_running_instances_skips_an_instance_stopped_while_it_waited(monkeypatch):
    # 另一项操作持着 instance_lock('t')；重启在 t 为 RUNNING 时排上锁；那项操作把 t 改成 INACTIVE 后放锁
    process = FakeProcess(ScriptState.RUNNING)
    use_fake_manager(monkeypatch, t=process)

    async def queued_behind_a_stop() -> list[str]:
        async with instance_lock('t'):   # 比如一条暂停命令
            restart = asyncio.create_task(restart_running_instances())
            for _ in range(3):
                await asyncio.sleep(0)
            assert not restart.done()   # 重启看到 t 在跑，排在锁上
            process.state = ScriptState.INACTIVE   # 暂停把实例停下了
        return await restart

    restarted = asyncio.run(queued_behind_a_stop())
    assert restarted == []
    assert process.calls == []


@pytest.mark.parametrize('present', [True, False])
def test_restart_instance_running_only_skips_a_stopped_or_missing_instance(monkeypatch, present):
    # present 时表里有 INACTIVE 的 t，否则表是空的
    processes = {'t': FakeProcess(ScriptState.INACTIVE)} if present else {}
    use_fake_manager(monkeypatch, **processes)
    assert asyncio.run(restart_instance('t', running_only=True)) is False
    if present:
        assert processes['t'].calls == []   # 不停、不启


def test_restart_instance_still_restarts_a_stopped_instance_by_default(monkeypatch):
    process = FakeProcess(ScriptState.INACTIVE)
    use_fake_manager(monkeypatch, t=process)
    assert asyncio.run(restart_instance('t')) is True
    assert process.calls == ['stop', 'start']


@pytest.mark.parametrize('state', [ScriptState.RUNNING, ScriptState.WARNING, ScriptState.UPDATING])
def test_start_instance_stops_a_live_process_first(monkeypatch, state):
    process = FakeProcess(state)
    use_fake_manager(monkeypatch, t=process)
    asyncio.run(start_instance('t'))
    assert process.calls == ['stop', 'start']


def test_start_instance_starts_a_stopped_process_directly(monkeypatch):
    process = FakeProcess(ScriptState.INACTIVE)
    use_fake_manager(monkeypatch, t=process)
    asyncio.run(start_instance('t'))
    assert process.calls == ['start']


# ---------- 注册与导入 ----------

def test_register_custom_mounts_the_routes():
    """register_custom 加的全部 /custom 接口在这里一次列全，多出、少了都会发现；静态目录是 Mount，没有方法，单独核对。"""
    app = FastAPI()
    register_custom(app)
    mounted = {(route.path, method) for route in app.routes
               if isinstance(route, APIRoute) and route.path.startswith('/custom') for method in route.methods}
    assert mounted == {
        ('/custom/orchestra/{script_name}', 'GET'),
        ('/custom/orchestra/{script_name}', 'PUT'),
        ('/custom/orchestra/{script_name}/command', 'POST'),
        ('/custom/instance/{script_name}/restart', 'POST'),
        ('/custom/assets/tasks', 'GET'),
        ('/custom/assets/{task}/rules', 'GET'),
        ('/custom/assets/{task}/static-check', 'GET'),
        ('/custom/assets/tags', 'GET'),
        ('/custom/assets/tags', 'PUT'),
        ('/custom/assets/{task}/screen-check', 'POST'),
        ('/custom/assets/{task}/replace-image', 'POST'),
        ('/custom/assets/{task}/commit-push', 'POST'),
        ('/custom/assets/{task}/export', 'GET'),
        ('/custom/feishu/chats', 'GET'),
    }
    assert [route.path for route in app.routes if isinstance(route, Mount)] == ['/custom/assets/static']


def test_blocking_io_runs_off_the_event_loop(instance_dir, monkeypatch):
    seen: dict[str, set[int]] = {}

    def spy(owner, name: str) -> None:
        original = getattr(owner, name)

        def recording(*args, **kwargs):
            seen.setdefault(name, set()).add(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, recording)

    spy(config_model, 'read_file')
    spy(config_model, 'write_file')
    spy(orchestra_router, 'load_progress')
    spy(orchestra_router, 'save_progress')
    spy(orchestra_router, 'instance_names')
    spy(instance_control, 'instance_names')

    resting()

    async def scenario() -> int:
        await get_orchestra('t')
        await put_orchestra('t', {'rows': []})
        await post_command('t', {'action': 'skip_rest'})   # 先读进度文件看是不是在休息
        await post_command('t', {'action': 'start'})
        await post_command('t', {'action': 'pause'})   # 读进度、改配置、写进度
        await post_restart('t')   # 进程表里的 t 是替身：碰文件的只有名字核对
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert set(seen) == {'read_file', 'write_file', 'load_progress', 'save_progress', 'instance_names'}
    assert all(loop_thread not in threads for threads in seen.values())


def test_importing_the_modules_does_not_load_the_manager(tmp_path):
    """上游 MainManager 在导入时就扫 config/（没有实例配置时还会复制出 oas1.json）并起推送线程，
    本分支的三个模块不能把它带进来。换个干净的进程验：别的用例若导入了它，本进程的 sys.modules 就不干净了。
    导入 module.logger 会把进程的当前目录切到仓库根（上游行为），所以这里验的是没导入 main_manager 与没起线程，
    不看当前目录下有没有 config/。
    """
    code = '\n'.join([
        'import sys, threading',
        'import module.server.custom_routes, module.server.orchestra_router, module.server.instance_control',
        'assert "module.server.main_manager" not in sys.modules',
        'assert "module.server.app" not in sys.modules',
        'assert threading.active_count() == 1',
    ])
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)},
                            capture_output=True, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stderr[-2000:]
