"""编排命令接口启停实例的流程（规格 §8.3，2026-11-25 修订）。

开始、暂停、继续、停止先停实例，实例停下之后才写配置和进度，状态为 running 的写完再启动实例；跳过休息不启停实例。
进程表换成假的（不导入上游的 MainManager），进程替身的 stop、start 与配置、进度的每次写入记进同一本账 journal，
用例按先后断言。处理函数用 asyncio.run 直接调。每个用例先 chdir 到 tmp_path，里面有 config/template.json 与
config/t.json，不碰仓库自己的 config/ 和 log/。
"""
import asyncio
import logging
import shutil
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.config import config_model
from module.config.config_model import ConfigModel
from module.orchestra.engine import recover, row_limits, sync_controls
from module.orchestra.mapping import apply_row, serialize_value
from module.orchestra.model import OrchestraRow
from module.orchestra.progress import Progress, load_progress, progress_path, progress_to_dict, save_progress
from module.server import instance_control, orchestra_router
from module.server.instance_control import restart_instance
from module.server.orchestra_router import post_command
from module.server.script_process import ScriptState
from tasks.Orochi.config import Layer

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = Path('config') / 't.json'   # 相对当前目录，用例里是 tmp_path
NEXT_RUN = 'orochi.scheduler.next_run'


@pytest.fixture(scope='module')
def default_config() -> dict:
    """一份默认实例配置的字典。上游默认值里有写成字符串的整数字段，dump 时 pydantic 会警告，与本文件无关，只在这里放行。"""
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Pydantic serializer warnings', category=UserWarning)
        return ConfigModel().model_dump()


@pytest.fixture
def journal(tmp_path, monkeypatch, default_config) -> list:
    """cwd 切到 tmp_path，建好实例 t 的配置。返回账本：配置每写一次记 ('config', state)，进度每写一次记
    ('progress', phase)，进程替身的 stop、start 也记在这里。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config').mkdir()
    shutil.copy(REPO_ROOT / 'config' / 'template.json', tmp_path / 'config' / 'template.json')
    ConfigModel.write_json('t', default_config)
    entries: list = []
    real_write, real_save = config_model.write_file, orchestra_router.save_progress

    def write_file(path, data):
        real_write(path, data)
        entries.append(('config', data['orchestra']['state']))

    def save(path, p):
        real_save(path, p)
        entries.append(('progress', p.phase))

    monkeypatch.setattr(config_model, 'write_file', write_file)
    monkeypatch.setattr(orchestra_router, 'save_progress', save)
    return entries


class Instance:
    """ScriptProcess 的替身：stop、start 都是 async，状态随之改变，同真的一样；调用记进账本。"""

    def __init__(self, journal: list, state: ScriptState = ScriptState.RUNNING):
        self.journal = journal
        self.state = state

    async def stop(self) -> None:
        await asyncio.sleep(0)   # 让出一次事件循环：调用方不 await 的话，这行之后的记账会晚于写配置
        self.state = ScriptState.INACTIVE
        self.journal.append('stop')

    async def start(self) -> None:
        self.state = ScriptState.RUNNING
        self.journal.append('start')


class SlowInstance(Instance):
    """stop 分两步、中间睡 20 毫秒：两个操作若交错进行，账本里会出现两个 stop:begin 挨在一起，或别的记账夹在中间。"""

    async def stop(self) -> None:
        self.journal.append('stop:begin')
        await asyncio.sleep(0.02)
        self.state = ScriptState.INACTIVE
        self.journal.append('stop:end')


def table(monkeypatch, journal: list, **processes: Instance) -> dict:
    """进程表换成假的并返回它。表里没有的实例，start_instance 新建的进程对象是停着的替身，新建记 ('create', 名字)。"""
    processes = dict(processes)
    monkeypatch.setattr(instance_control, '_manager', lambda: SimpleNamespace(script_process=processes))

    def create(name: str) -> Instance:
        journal.append(('create', name))
        return Instance(journal, ScriptState.INACTIVE)

    monkeypatch.setattr(instance_control, 'ScriptProcess', create)
    return processes


def send(journal: list, action, name: str = 't') -> dict:
    """清空账本后发一条命令：账本只记这一条命令期间的事。"""
    journal.clear()
    return asyncio.run(post_command(name, {'action': action}))


def stored():
    return ConfigModel('t').orchestra


def progress() -> Progress:
    return load_progress(progress_path('t'))


def set_orchestra(**fields) -> None:
    cfg = ConfigModel('t')
    for key, value in fields.items():
        setattr(cfg.orchestra, key, value)   # 嵌套赋值不会触发 ConfigModel 的自动保存
    cfg.save()


def mid_row(extra: dict | None = None) -> dict:
    """实例正在跑第 1 行（御魂·神罚 12 次）：配置里是行参数，next_run 改成了开始时刻，state 为 running；
    进度为 row_running，记着用户原值（悲鸣、30 次、原来的 next_run），extra 一并记进去。返回记下的原值。"""
    cfg = ConfigModel('t')
    orchestra = cfg.orchestra
    orchestra.rows = [OrchestraRow(task='Orochi', sub1='神罚', max_count=12)]
    orchestra.state, orchestra.command, orchestra.command_seq = 'running', 'start', 1
    started = datetime.now().replace(microsecond=0) - timedelta(minutes=3)
    original_next_run = cfg.orochi.scheduler.next_run
    saved = {**apply_row(cfg, orchestra.rows[0]), NEXT_RUN: serialize_value(original_next_run), **(extra or {})}
    cfg.orochi.scheduler.next_run = started
    cfg.save()
    save_progress(progress_path('t'), Progress(
        phase='row_running', row_index=0, row_task='Orochi', sequence_started_at=started, row_started_at=started,
        heartbeat_at=started + timedelta(minutes=2), done_count=4, saved_params=saved, last_command_seq=1))
    return saved


def assert_originals_restored(original_next_run: datetime) -> None:
    orochi = ConfigModel('t').orochi
    assert (orochi.orochi_config.layer, orochi.orochi_config.limit_count) == (Layer.ELEVEN, 30)   # 悲鸣、30 次
    assert orochi.scheduler.next_run == original_next_run


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


# ---------- 护栏 ----------

def test_the_real_process_table_is_guarded_by_default():
    with pytest.raises(AssertionError):
        instance_control._manager()
    with pytest.raises(AssertionError):
        instance_control.ScriptProcess('t')


# ---------- 暂停 ----------

@pytest.mark.parametrize('present, state, stops', [
    (True, ScriptState.RUNNING, True),
    (True, ScriptState.WARNING, True),     # 上游在连续失败后退出实例时标 WARNING：不确定进程是否还在，照样停一次
    (True, ScriptState.INACTIVE, False),   # 已停着：没有要停的
    (False, None, False),                  # 进程表里没有：没有要停的，也不新建进程对象
])
def test_pause_stops_instance_before_writing(journal, monkeypatch, present, state, stops):
    processes = table(monkeypatch, journal, **({'t': Instance(journal, state)} if present else {}))
    mid_row()
    assert send(journal, 'pause') == {'ok': True}
    # 先停，再写配置、进度；不再启动
    assert journal == ['stop'] * stops + [('config', 'paused'), ('progress', 'row_running')]
    assert stored().state == 'paused'
    assert progress().paused
    assert ('t' in processes) == present


def test_pause_restores_row_params(journal, monkeypatch):
    table(monkeypatch, journal, t=Instance(journal))
    original_next_run = ConfigModel('t').orochi.scheduler.next_run
    mid_row()
    assert ConfigModel('t').orochi.orochi_config.layer == Layer.TWELVE   # 运行中：配置里是行参数 神罚
    assert send(journal, 'pause') == {'ok': True}
    assert_originals_restored(original_next_run)   # next_run 一并还原：行没打完，不能让它停在过去被当成到点的待办
    p = progress()
    assert p.saved_params == {}
    assert (p.phase, p.row_index, p.row_task, p.done_count) == ('row_running', 0, 'Orochi', 4)   # 中断的行留给继续后续跑


def test_pause_during_rest_keeps_remaining(journal, monkeypatch):
    table(monkeypatch, journal, t=Instance(journal))
    set_orchestra(rows=[OrchestraRow(task='Orochi')] * 2, state='running', command='start', command_seq=1)
    rest_until = datetime.now() + timedelta(minutes=5)
    save_progress(progress_path('t'), Progress(phase='resting', row_index=1, sequence_started_at=datetime.now(),
                                               rest_until=rest_until, last_command_seq=1))
    assert send(journal, 'pause') == {'ok': True}
    p = progress()
    assert p.phase == 'resting' and p.paused
    assert abs(p.rest_remaining - (rest_until - datetime.now()).total_seconds()) <= 1

    # 继续只写 state、不动进度：实例启动后引擎读到 running，休息截止 = 那一刻 + 暂停时记下的剩余
    assert send(journal, 'resume') == {'ok': True}
    later = datetime.now() + timedelta(hours=1)
    resumed = sync_controls(stored(), progress(), later)
    assert not resumed.paused and resumed.rest_until == later + timedelta(seconds=p.rest_remaining)


def test_pause_before_first_heartbeat_freezes_row_elapsed(journal, monkeypatch):
    table(monkeypatch, journal, t=Instance(journal))
    mid_row()
    # 本行刚开始 10 秒，第一次心跳还没来（on_row_start 清掉了 heartbeat_at）；这一行此前中断过，累计 90 秒
    before = progress()
    before.row_started_at = datetime.now().replace(microsecond=0) - timedelta(seconds=10)
    before.heartbeat_at, before.row_elapsed_before = None, 90.0
    save_progress(progress_path('t'), before)
    assert send(journal, 'pause') == {'ok': True}
    paused_at, paused = datetime.now(), progress()
    later = paused_at + timedelta(hours=1)
    # 暂停期间面板上本行的已用（全程也加上它）不再增长
    frozen = progress_to_dict(paused, paused_at)['row_elapsed_seconds']
    assert progress_to_dict(paused, later)['row_elapsed_seconds'] == frozen
    # 继续后实例的 recover 截止到最近一次心跳，没有心跳截止到行开始：记下的已用与按暂停前的进度算的一样
    # （即补心跳之前的结果），剩余也就不变；面板冻结的正是这个已用
    recovered, baseline = recover(paused, later), recover(before, later)
    assert recovered.row_elapsed_before == baseline.row_elapsed_before == frozen
    row = OrchestraRow(task='Orochi', max_count=12, max_minutes=20)
    assert row_limits(row, recovered, 'remaining') == row_limits(row, baseline, 'remaining')


# ---------- 继续 ----------

@pytest.mark.parametrize('state, expected', [
    (ScriptState.INACTIVE, [('config', 'running'), 'start']),
    (ScriptState.RUNNING, ['stop', ('config', 'running'), 'start']),   # 在跑：先停下再写
])
def test_resume_starts_instance_after_writing(journal, monkeypatch, state, expected):
    process = Instance(journal, state)
    table(monkeypatch, journal, t=process)
    set_orchestra(state='paused', command='start', command_seq=1)
    save_progress(progress_path('t'), Progress(phase='row_running', paused=True, last_command_seq=1))
    before = progress_path('t').read_bytes()
    assert send(journal, 'resume') == {'ok': True}
    assert journal == expected
    assert (stored().state, stored().command, stored().command_seq) == ('running', 'start', 1)   # 命令字段不动
    assert progress_path('t').read_bytes() == before   # 进度不动，留给实例的引擎
    assert process.state == ScriptState.RUNNING


# ---------- 停止 ----------

def test_stop_resets_progress_and_restores(journal, monkeypatch):
    process = Instance(journal)
    table(monkeypatch, journal, t=process)
    original_next_run = ConfigModel('t').orochi.scheduler.next_run
    mid_row()
    assert send(journal, 'stop') == {'ok': True}
    assert journal == ['stop', ('config', 'idle'), ('progress', 'idle')]   # 不再启动
    assert process.state == ScriptState.INACTIVE
    assert_originals_restored(original_next_run)
    p = progress()
    assert (p.phase, p.paused, p.saved_params, p.rest_until) == ('idle', False, {}, None)
    assert stored().state == 'idle'


# ---------- 开始 ----------

def test_start_restarts_running_instance(journal, monkeypatch):
    process = Instance(journal)
    table(monkeypatch, journal, t=process)
    mid_row()
    before = progress_path('t').read_bytes()
    assert send(journal, 'start') == {'ok': True}
    assert journal == ['stop', ('config', 'running'), 'start']
    assert (stored().state, stored().command, stored().command_seq) == ('running', 'start', 2)
    # 进度与行参数都不动：实例启动后引擎先把中断的行还原，再凭新命令从第 1 行开始
    assert progress_path('t').read_bytes() == before
    assert ConfigModel('t').orochi.orochi_config.layer == Layer.TWELVE
    assert process.state == ScriptState.RUNNING


@pytest.mark.parametrize('present', [True, False])
def test_start_when_not_running(journal, monkeypatch, present):
    processes = table(monkeypatch, journal, **({'t': Instance(journal, ScriptState.INACTIVE)} if present else {}))
    assert send(journal, 'start') == {'ok': True}
    # 没在跑：不停；写好配置再启动，进程表里没有时先建进程对象（比如服务启动后才新建的实例）
    assert journal == [('config', 'running')] + [('create', 't')] * (not present) + ['start']
    assert (stored().state, stored().command, stored().command_seq) == ('running', 'start', 1)
    assert processes['t'].state == ScriptState.RUNNING


# ---------- 跳过休息 ----------

def _resting_progress(**kwargs) -> None:
    save_progress(progress_path('t'), Progress(phase='resting', row_index=1,
                                               rest_until=datetime.now() + timedelta(minutes=5), **kwargs))


def _locked(path):
    raise PermissionError('占用中')


@pytest.mark.parametrize('case, ok', [
    ('resting', True),
    ('not_resting', False),
    ('start_pending', False),   # 实例还没处理开始命令：会把 start 顶掉
    ('progress_locked', False),
])
def test_skip_rest_unchanged(journal, monkeypatch, case, ok):
    process = Instance(journal)
    table(monkeypatch, journal, t=process)
    set_orchestra(state='running', command='start', command_seq=1)
    if case == 'not_resting':
        save_progress(progress_path('t'), Progress(phase='row_running', last_command_seq=1))
    else:
        _resting_progress(last_command_seq=0 if case == 'start_pending' else 1)
    if case == 'progress_locked':
        monkeypatch.setattr(orchestra_router, 'load_progress', _locked)
    before = CONFIG_FILE.read_bytes()
    assert send(journal, 'skip_rest') == {'ok': ok}
    assert 'stop' not in journal and 'start' not in journal and process.state == ScriptState.RUNNING   # 不启停实例
    if ok:
        assert journal == [('config', 'running')]   # 只记一条命令，不写进度
        assert (stored().command, stored().command_seq) == ('skip_rest', 2)
    else:
        assert journal == [] and CONFIG_FILE.read_bytes() == before


# ---------- 不接受的命令 ----------

@pytest.mark.parametrize('name, action', [
    ('nope', 'pause'), ('nope', 'start'), ('template', 'stop'),
    ('t', 'explode'), ('t', 'START'), ('t', None), ('t', ['start']),
])
def test_unknown_instance_or_action(journal, monkeypatch, name, action):
    process = Instance(journal)
    processes = table(monkeypatch, journal, t=process)
    before = CONFIG_FILE.read_bytes()
    assert send(journal, action, name) == {'ok': False}
    assert journal == []   # 不停、不启、不建进程对象，也不写配置和进度
    assert process.state == ScriptState.RUNNING and set(processes) == {'t'}
    assert CONFIG_FILE.read_bytes() == before
    assert not Path('config', 'nope.json').exists() and not progress_path(name).exists()


# ---------- 排队 ----------

def test_commands_serialized_per_instance(journal, monkeypatch):
    table(monkeypatch, journal, t=SlowInstance(journal))

    async def twice():
        return await asyncio.gather(post_command('t', {'action': 'start'}), post_command('t', {'action': 'start'}))
    journal.clear()
    assert asyncio.run(twice()) == [{'ok': True}, {'ok': True}]
    # 第二条等第一条把实例启动完，才开始停实例
    assert journal == ['stop:begin', 'stop:end', ('config', 'running'), 'start'] * 2
    assert stored().command_seq == 2


def test_a_command_waits_for_a_restart_of_the_same_instance(journal, monkeypatch):
    table(monkeypatch, journal, t=SlowInstance(journal))
    mid_row()

    async def both():
        return await asyncio.gather(restart_instance('t'), post_command('t', {'action': 'pause'}))
    journal.clear()
    assert asyncio.run(both()) == [True, {'ok': True}]
    assert journal == ['stop:begin', 'stop:end', 'start',   # 重启先拿到锁
                       'stop:begin', 'stop:end', ('config', 'paused'), ('progress', 'row_running')]


# ---------- 出错 ----------

@pytest.mark.parametrize('action', ['pause', 'stop'])
def test_unreadable_progress_after_stop_leaves_config_untouched(journal, monkeypatch, caplog, action):
    process = Instance(journal)
    table(monkeypatch, journal, t=process)
    mid_row()
    before = CONFIG_FILE.read_bytes()
    monkeypatch.setattr(orchestra_router, 'load_progress', _locked)
    with caplog.at_level(logging.ERROR):
        assert send(journal, action) == {'ok': False}
    assert journal == ['stop']   # 实例保持停下，不再启动；配置、进度都没写
    assert process.state == ScriptState.INACTIVE
    assert CONFIG_FILE.read_bytes() == before
    assert any(message.startswith('[t] ') and 'PermissionError' in message for message in errors(caplog))


def test_a_field_that_cannot_be_restored_does_not_stop_the_rest(journal, monkeypatch, caplog):
    table(monkeypatch, journal, t=Instance(journal))
    original_next_run = ConfigModel('t').orochi.scheduler.next_run
    mid_row(extra={'orochi.no_such_group.limit_count': 5})   # 映射或进度文件过期：这一项对不上
    with caplog.at_level(logging.ERROR):
        assert send(journal, 'pause') == {'ok': True}
    assert_originals_restored(original_next_run)   # 其余照常还原
    assert stored().state == 'paused'
    assert progress().saved_params == {}   # 配置存成了就清掉，免得每次都重试
    assert any('no_such_group' in message for message in errors(caplog))


@pytest.mark.parametrize('action', ['pause', 'stop'])
def test_config_save_failure_keeps_saved_params(journal, monkeypatch, caplog, action):
    table(monkeypatch, journal, t=Instance(journal))
    mid_row()
    before = progress_path('t').read_bytes()

    def failing(path, data):
        raise OSError('disk full')
    monkeypatch.setattr(config_model, 'write_file', failing)
    with caplog.at_level(logging.ERROR):
        assert send(journal, action) == {'ok': False}
    assert journal == ['stop']
    assert progress_path('t').read_bytes() == before   # 原值留在进度里，实例下次启动时引擎补还原
    assert any('disk full' in message for message in errors(caplog))


@pytest.mark.parametrize('action', ['start', 'resume'])
def test_a_failed_write_leaves_the_instance_stopped(journal, monkeypatch, caplog, action):
    process = Instance(journal)
    table(monkeypatch, journal, t=process)
    set_orchestra(state='paused')

    def failing(path, data):
        raise OSError('disk full')
    monkeypatch.setattr(config_model, 'write_file', failing)
    with caplog.at_level(logging.ERROR):
        assert send(journal, action) == {'ok': False}
    assert journal == ['stop']   # state 没写成 running：不启动，免得实例按暂停前的状态跑别的任务
    assert process.state == ScriptState.INACTIVE
    assert stored().state == 'paused'
    assert any('disk full' in message for message in errors(caplog))


def test_progress_write_failure_after_the_config_is_logged(journal, monkeypatch, caplog):
    table(monkeypatch, journal, t=Instance(journal))
    original_next_run = ConfigModel('t').orochi.scheduler.next_run
    saved = mid_row()

    def failing(path, p):
        raise PermissionError('占用中')
    monkeypatch.setattr(orchestra_router, 'save_progress', failing)
    with caplog.at_level(logging.ERROR):
        assert send(journal, 'pause') == {'ok': True}   # 实例已停下、配置已改：暂停生效了
    assert stored().state == 'paused'
    assert_originals_restored(original_next_run)
    assert progress().saved_params == saved   # 原值留着，实例下次启动时引擎再还原一次，同样的值
    assert any(message.startswith('[t] ') and 'PermissionError' in message for message in errors(caplog))
