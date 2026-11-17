"""编排运行时（module.orchestra.runtime）的测试。

配置用替身 FakeConfig：只带 runtime 用到的那部分属性，save() 只计数，不写 config/ 目录。
进度文件写在 tmp_path/log 下，每个用例都先 chdir 到 tmp_path，不碰仓库自己的 config/ 和 log/。
时间用假时钟；只有心跳线程的用例用真实的短间隔，并设了截止时间，线程在 after_run 里结束。
"""
import copy
import logging
import random
import threading
import time
from datetime import datetime, time as dtime, timedelta
from types import SimpleNamespace

import pytest

from module.config.anti_ban import AntiBanGuard
from module.config.config import Config, Function
from module.config.config_model import ConfigModel
from module.exception import RequestHumanTakeover
from module.orchestra import runtime as runtime_module
from module.orchestra.mapping import TASK_SPECS
from module.orchestra.model import OrchestraRow
from module.orchestra.progress import Progress, legacy_progress_path, load_progress, progress_path, save_progress
from module.orchestra.runtime import (OrchestraRuntime, orchestra_after_run, orchestra_before_run, orchestra_pick,
                                      runtime_of)
from tasks.Orochi.config import Layer

T0 = datetime(2026, 11, 17, 10, 0)
LIMIT_COUNT = 'orochi.orochi_config.limit_count'
LAYER = 'orochi.orochi_config.layer'
NEXT_RUN = 'orochi.scheduler.next_run'
DEFAULT_NEXT_RUN = ConfigModel().orochi.scheduler.next_run   # 上游默认值 2023-01-01，一直在过去
HEARTBEAT = 'OrchestraHeartbeat'
# 真实的 Config.save / update_scheduler 用了 pydantic 已弃用的 .dict()，用到真实 Config 的用例不刷这条上游警告
REAL_CONFIG = pytest.mark.filterwarnings(
    'ignore:The `dict` method is deprecated:pydantic.warnings.PydanticDeprecatedSince20')


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeConfig:
    """Config 的替身。get_next 的行为同上游：有待办取第一个，没有待办取等待里最早的，两表皆空则抛 RequestHumanTakeover。"""

    def __init__(self):
        self.model = ConfigModel()
        self.config_name = 't'
        self.pending_task: list[Function] = []
        self.waiting_task: list[Function] = []
        self.saves = 0
        self.reloads = 0

    def get_next(self) -> Function:
        if self.pending_task:
            return self.pending_task[0]
        if self.waiting_task:
            return copy.deepcopy(self.waiting_task[0])
        raise RequestHumanTakeover

    def save(self) -> None:
        self.saves += 1

    def reload(self) -> None:
        self.reloads += 1   # 真实的 Config.reload 会换一个新的 ConfigModel；这里不换，换了的情形见 test_after_run_works_on_the_reloaded_model


def fn(key: str, next_run: datetime) -> Function:
    """调度器里的一个任务，key 是模型字段名（orochi、restart……）。"""
    return Function(key, {'scheduler': {'enable': True, 'next_run': next_run, 'priority': 5}})


def make(rows=(), state='running', progress: Progress | None = None, interval: float = 30.0, **settings):
    """返回 (runtime, script 替身, config 替身, 假时钟)。progress 预先写进进度文件，settings 写进 orchestra 配置。"""
    config = FakeConfig()
    orchestra = config.model.orchestra   # 嵌套赋值不会触发 ConfigModel 的自动保存
    orchestra.rows = list(rows)
    orchestra.state = state
    for name, value in settings.items():
        setattr(orchestra, name, value)
    script = SimpleNamespace(config=config, config_name='t', anti_ban_guard=AntiBanGuard(), running_task_obj=None)
    if progress is not None:
        save_progress(progress_path('t'), progress)
    clock = Clock()
    runtime = OrchestraRuntime(script, clock=clock, rng=random.Random(0), heartbeat_interval=interval)
    return runtime, script, config, clock


def saved() -> Progress:
    return load_progress(progress_path('t'))


def heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == HEARTBEAT]


def wait_for(condition, timeout: float = 3.0) -> bool:
    """轮询到条件成立或超时。3 秒只是失败时的上限，通过的用例几十毫秒就返回；
    Windows 上进度文件偶尔被占用，存盘要重试（最多约 0.5 秒），所以留足余量。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except OSError:
            pass   # 恰好赶上写者替换文件，下一轮再读
        time.sleep(0.01)
    return False


def warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == 'oas' and r.levelno == logging.WARNING]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    created: list[OrchestraRuntime] = []
    real_init = OrchestraRuntime.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        created.append(self)
    monkeypatch.setattr(OrchestraRuntime, '__init__', tracking_init)
    yield
    left_running = heartbeat_threads()
    for runtime in created:   # 断言失败时 after_run 没走到：停掉它的心跳，免得线程拖进后面的用例
        runtime._abandon_row()
    assert heartbeat_threads() == []
    assert left_running == []   # 每个用例自己也该把心跳线程停掉并回收


# ---------- 简报规定的 8 个用例 ----------

def test_pick_passthrough_when_idle():
    runtime, _, config, _ = make(state='idle')
    due = fn('orochi', T0 - timedelta(hours=1))
    config.pending_task = [due]
    assert runtime.pick() is due
    assert not progress_path('t').exists() and config.saves == 0   # 没有编排时和上游一样，不留任何痕迹


def test_pick_survives_no_enabled_tasks():
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi')], state='running')
    picked = runtime.pick()   # 两表皆空，get_next 抛 RequestHumanTakeover，编排运行中当作空待办
    assert picked.command == 'Orochi' and picked.next_run == T0


def test_row_cycle_applies_and_restores_params():
    rows = [OrchestraRow(task='Orochi', sub1='神罚', max_count=12), OrchestraRow(task='Orochi')]
    runtime, _, config, clock = make(rows=rows)
    cfg = config.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)

    assert runtime.pick().command == 'Orochi'
    runtime.before_run('Orochi')
    assert (cfg.limit_count, cfg.layer) == (12, Layer.TWELVE)
    p = saved()
    assert p.phase == 'row_running' and p.row_started_at == T0
    assert p.saved_params == {LIMIT_COUNT: 30, LAYER: '悲鸣', NEXT_RUN: DEFAULT_NEXT_RUN.isoformat()}

    config.model.orochi.scheduler.next_run = T0 + timedelta(days=1)   # 任务正常结束，会把 next_run 推到明天
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)
    assert config.model.orochi.scheduler.next_run == T0 + timedelta(days=1)   # 完成了：任务自己推后的 next_run 留着
    p = saved()
    assert p.phase == 'resting' and p.row_index == 1 and p.saved_params == {}
    assert p.rest_until == T0 + timedelta(minutes=8)   # 结束于 3 分钟时，默认休息 5 分钟
    assert config.reloads == 1


def test_row_without_next_run_advance_is_interrupted():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    runtime.before_run('Orochi')
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)   # run 返回了，但任务没走到 task_delay：next_run 没往后推
    p = saved()
    assert p.phase == 'interrupted' and p.row_index == 0
    assert p.row_elapsed_before == 180.0
    assert config.model.orochi.orochi_config.limit_count == 30   # 中断也要还原


def test_crash_restores_saved_params_on_next_pick():
    crashed = Progress(phase='row_running', row_index=0, row_started_at=T0, saved_params={LIMIT_COUNT: 30})
    runtime, _, config, _ = make(state='idle', progress=crashed)
    config.model.orochi.orochi_config.limit_count = 99   # 上次崩溃时留在配置里的行参数
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    assert config.model.orochi.orochi_config.limit_count == 30
    assert config.saves == 1   # 还原后存了配置
    p = saved()
    assert p.saved_params == {} and p.phase == 'idle'


def test_native_cut_in_does_not_apply_row_params():
    # 爬塔既在表里（还是当前行），又在插队名单里：原生插队的那一轮，不能把这一行的参数写进去
    rows = [OrchestraRow(task='ActivityShikigami', sub1='ap', max_count=100, max_minutes=45), OrchestraRow(task='Orochi')]
    runtime, _, config, _ = make(rows=rows)
    climb = config.model.activity_shikigami.general_climb
    before = climb.model_dump()
    config.pending_task = [fn('activity_shikigami', T0 - timedelta(minutes=1))]
    assert runtime.pick() is config.pending_task[0]   # 插队名单优先于当前行：待办里的爬塔原样放行
    runtime.before_run('ActivityShikigami')
    assert climb.model_dump() == before   # general_climb 一个字段没动
    assert config.saves == 0 and saved().phase == 'ready' and saved().saved_params == {}
    runtime.after_run('ActivityShikigami', True)   # 没启动行，什么都不做
    assert saved().phase == 'ready' and config.saves == 0 and climb.model_dump() == before

    # 对照：待办里没有爬塔了，同一个任务名作为表里的行运行，参数才写进去
    config.pending_task = []
    assert runtime.pick().command == 'ActivityShikigami'
    runtime.before_run('ActivityShikigami')
    assert (climb.run_sequence, climb.ap_limit, climb.limit_time) == ('ap', 100, dtime(0, 45))
    assert saved().phase == 'row_running'
    runtime.after_run('ActivityShikigami', False)


def test_heartbeat_writes_done_count():
    runtime, script, _, clock = make(rows=[OrchestraRow(task='Orochi', max_count=30)], interval=0.05)
    runtime.pick()
    runtime.before_run('Orochi')
    clock.advance(minutes=1)
    script.running_task_obj = SimpleNamespace(current_count=7)   # 任务对象在 run 里才创建；先拨钟再放对象，读到计数的心跳时间必是新的
    try:
        assert wait_for(lambda: saved().done_count == 7)
        assert saved().heartbeat_at == clock.now and saved().phase == 'row_running'
    finally:
        runtime.after_run('Orochi', False)
    assert saved().phase == 'interrupted' and saved().done_count == 7


def test_status_note_for_antiban_window():
    runtime, script, config, clock = make(rows=[OrchestraRow(task='Orochi')])
    anti_ban = config.model.script.anti_ban
    anti_ban.enable, anti_ban.sleep_start, anti_ban.sleep_end = True, dtime(2, 0), dtime(8, 0)
    clock.now = datetime(2026, 11, 17, 3, 0)
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，08:00 继续'


# ---------- 防封禁提示的其余分支（裁定 12） ----------

def test_status_note_empty_when_disabled_or_outside_window():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi')])
    anti_ban = config.model.script.anti_ban
    anti_ban.sleep_start, anti_ban.sleep_end = dtime(2, 0), dtime(8, 0)
    clock.now = datetime(2026, 11, 17, 3, 0)
    runtime.pick()
    assert saved().status_note == ''   # 没开防封禁
    anti_ban.enable = True
    clock.now = datetime(2026, 11, 17, 12, 0)
    runtime.pick()
    assert saved().status_note == ''   # 开了，但不在休眠窗口


def test_status_note_follows_the_window_and_the_long_rest():
    runtime, script, config, clock = make(rows=[OrchestraRow(task='Orochi')])
    anti_ban = config.model.script.anti_ban
    anti_ban.enable, anti_ban.sleep_start, anti_ban.sleep_end = True, dtime(2, 0), dtime(8, 0)
    clock.now = datetime(2026, 11, 17, 3, 0)
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，08:00 继续'
    script.anti_ban_guard._rest_until = datetime(2026, 11, 17, 9, 30)   # 窗口和长休息同时在，继续的时间取更晚的
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，09:30 继续'
    script.anti_ban_guard._rest_until = datetime(2026, 11, 17, 5, 0)
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，08:00 继续'
    script.anti_ban_guard._rest_until = None
    clock.now = datetime(2026, 11, 17, 9, 0)
    runtime.pick()
    assert saved().status_note == ''   # 出了窗口就清掉
    script.anti_ban_guard._rest_until = datetime(2026, 11, 17, 11, 30)   # 每日活跃上限触发的长休息
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，11:30 继续'
    script.anti_ban_guard._rest_until = datetime(2026, 11, 17, 8, 30)    # 已经过去的不算
    runtime.pick()
    assert saved().status_note == ''


def test_status_note_does_not_call_wake_time():
    runtime, script, config, clock = make(rows=[OrchestraRow(task='Orochi')])
    config.model.script.anti_ban.enable = True
    clock.now = datetime(2026, 11, 17, 3, 0)

    def forbidden(*args):
        raise AssertionError('wake_time 有副作用，只读状态，不能调用')
    script.anti_ban_guard.wake_time = forbidden
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，08:00 继续'


# ---------- 没有编排在进行时：不更新提示，不写进度文件 ----------

def sleeping_antiban(config, clock) -> None:
    """防封禁开着，假时钟落在 02:00–08:00 的休眠窗口里。"""
    anti_ban = config.model.script.anti_ban
    anti_ban.enable, anti_ban.sleep_start, anti_ban.sleep_end = True, dtime(2, 0), dtime(8, 0)
    clock.now = datetime(2026, 11, 17, 3, 0)


def test_idle_instance_with_antiban_in_the_window_writes_no_progress_file():
    runtime, _, config, clock = make(state='idle')
    sleeping_antiban(config, clock)
    due = fn('orochi', T0 - timedelta(hours=1))
    config.pending_task = [due]
    assert runtime.pick() is due
    assert not progress_path('t').exists()   # 没有编排：提示不更新，进度与加载时的一样，就不存盘
    assert config.saves == 0


def test_status_note_is_updated_while_paused():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi')], state='paused')
    sleeping_antiban(config, clock)
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    p = saved()
    assert p.paused and p.status_note == '防封禁休眠中，08:00 继续'   # 暂停也算编排在进行


def test_status_note_is_left_alone_when_nothing_is_orchestrated():
    finished = Progress(phase='done', sequence_started_at=T0, status_note='防封禁休眠中，08:00 继续')   # 上一轮留下的
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi')], state='idle', progress=finished)
    config.model.script.anti_ban.enable = True
    clock.now = datetime(2026, 11, 17, 12, 0)   # 不在窗口里：要是还在更新，提示会被清成空
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    assert saved() == finished   # 提示原样不动，文件也没有被改写
    assert config.saves == 0


# ---------- 选任务 ----------

def test_pick_reraises_human_takeover_when_not_orchestrating():
    runtime, _, _, _ = make(state='idle')
    with pytest.raises(RequestHumanTakeover):   # 同上游：没有任何任务可跑时请求人工接管
        runtime.pick()


def test_paused_with_nothing_to_run_waits_instead_of_exiting():
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='GoryouRealm')]
    resting = Progress(phase='resting', row_index=1, sequence_started_at=T0, rest_until=T0 + timedelta(minutes=10))
    runtime, _, config, clock = make(rows=rows, state='paused', progress=resting)
    picked = runtime.pick()   # 两张表都空：上游会请求人工接管、实例退出，暂停中不能这样
    assert picked.next_run == T0 + timedelta(minutes=5) and picked.next_run > clock.now   # 5 分钟后到点，上游睡到那时再选
    assert picked.command == 'GoryouRealm'   # 占位的任务名借自继续后要跑的那一行（第 2 行），不是表里的第一行
    p = saved()
    assert p.paused and p.phase == 'resting'   # 暂停照常记进进度
    runtime.before_run('GoryouRealm')   # 占位不是编排的行
    assert config.saves == 0 and saved().phase == 'resting'


@pytest.mark.parametrize('state, progress', [
    ('idle', None),                                                 # 停着
    ('idle', Progress(phase='done', sequence_started_at=T0)),       # 跑完了
    ('paused', None),                                               # 暂停了，但序列根本没开始：不算进行中
])
def test_nothing_to_run_still_raises_when_stopped_finished_or_never_started(state, progress):
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi')], state=state, progress=progress)
    with pytest.raises(RequestHumanTakeover):   # 同上游：请求人工接管
        runtime.pick()


def test_no_placeholder_can_be_built_without_a_runnable_row_so_the_upstream_exit_stands(caplog):
    started = Progress(phase='ready', row_index=0, sequence_started_at=T0)
    runtime, _, _, _ = make(rows=[OrchestraRow(task='NoSuchTask')], state='paused', progress=started)
    with caplog.at_level(logging.WARNING), pytest.raises(RequestHumanTakeover):   # 表里没有可运行的行，借不到占位的任务名：照上游重抛
        runtime.pick()
    assert [r for r in warnings(caplog) if '占位' in r.getMessage()]   # 预期内的情形：记警告
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]   # 不是内部错误，不走 pick 的兜底


@pytest.mark.parametrize('state, waits', [('running', True), ('paused', True), ('idle', False)])
def test_progress_load_failure_fallback_waits_only_while_orchestrating(state, waits, monkeypatch):
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi')], state=state)   # 两张表都空

    def unreadable(path):
        raise PermissionError('locked')
    monkeypatch.setattr(runtime_module, 'load_progress', unreadable)
    if waits:
        picked = runtime.pick()   # 进度读不到，只能凭 state 判断编排在不在进行
        assert (picked.command, picked.next_run) == ('Orochi', T0 + timedelta(minutes=5))
    else:
        with pytest.raises(RequestHumanTakeover):
            runtime.pick()


def test_pick_native_returns_the_pending_function_itself():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')])
    restart = fn('restart', T0 - timedelta(minutes=1))
    config.pending_task = [fn('orochi', T0 - timedelta(hours=1)), restart]
    assert runtime.pick() is restart   # Restart 永远放行，返回待办里那个对象，不重造


def test_row_function_matches_every_cataloged_task():
    for spec in TASK_SPECS.values():
        row = OrchestraRow(task=spec.command, sub1=spec.subs[0].options[0] if spec.subs else None)
        runtime, _, _, _ = make(rows=[row])
        picked = runtime.pick()
        assert (picked.command, picked.next_run, picked.enable) == (spec.command, T0, True)


def test_wait_placeholder_is_the_current_row_task():
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='GoryouRealm')]
    resting = Progress(phase='resting', row_index=1, sequence_started_at=T0, rest_until=T0 + timedelta(minutes=5))
    runtime, _, config, _ = make(rows=rows, progress=resting)
    picked = runtime.pick()   # 休息中、没有插队任务：占位 Function 的时间是休息截止再加 1 秒
    assert (picked.command, picked.next_run) == ('GoryouRealm', T0 + timedelta(minutes=5, seconds=1))
    runtime.before_run('GoryouRealm')   # 万一上游在休息截止的一瞬间把它当成到点的任务交了上来，也不当作编排的行
    assert saved().phase == 'resting' and config.saves == 0
    runtime.after_run('GoryouRealm', True)
    assert saved().phase == 'resting'


def test_wait_placeholder_skips_an_invalid_current_row():
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='NoSuchTask'), OrchestraRow(task='GoryouRealm')]
    resting = Progress(phase='resting', row_index=1, sequence_started_at=T0, rest_until=T0 + timedelta(minutes=5))
    runtime, _, _, _ = make(rows=rows, progress=resting)
    assert runtime.pick().command == 'GoryouRealm'   # 手改坏的行造不出 Function，顺延到下一个可运行的行


def test_wait_targets_the_earlier_cut_in_task():
    resting = Progress(phase='resting', row_index=0, sequence_started_at=T0, rest_until=T0 + timedelta(minutes=5))
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')], progress=resting)
    config.waiting_task = [fn('meta_demon', T0 + timedelta(minutes=2))]
    picked = runtime.pick()
    assert (picked.command, picked.next_run) == ('MetaDemon', T0 + timedelta(minutes=2, seconds=1))
    assert saved().phase == 'resting'


def test_skipped_invalid_row_is_logged_once_per_row(caplog):
    rows = [OrchestraRow(task='NoSuchTask'), OrchestraRow(task='NoSuchTask'),
            OrchestraRow(task='Orochi', max_count=0), OrchestraRow(task='Orochi')]
    progress = Progress(phase='ready', row_index=1, sequence_started_at=T0)   # 第 1 行已经过去了，不再提
    runtime, _, _, _ = make(rows=rows, progress=progress)
    with caplog.at_level(logging.WARNING):
        assert runtime.pick().command == 'Orochi'
    messages = [r.getMessage() for r in warnings(caplog)]
    assert len(messages) == 2
    assert '第 2 行' in messages[0] and '不支持编排的任务' in messages[0]
    assert '第 3 行' in messages[1] and 'max_count' in messages[1]
    assert saved().row_index == 1   # 记日志不改进度：行号要到 before_run 才推进


# ---------- 进度的读写（裁定 2） ----------

def test_progress_load_failure_falls_back_to_native_then_retries(monkeypatch, caplog):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')])
    due = fn('restart', T0 - timedelta(minutes=1))
    config.pending_task = [due]
    real, calls = runtime_module.load_progress, []

    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError('locked')
        return real(path)
    monkeypatch.setattr(runtime_module, 'load_progress', flaky)

    with caplog.at_level(logging.WARNING):
        assert runtime.pick() is due   # 读不到进度：这一次按上游的选，编排不插手
    assert [r for r in warnings(caplog) if '读取失败' in r.getMessage()]   # 是预期内的情形，记警告，不当作内部错误
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not progress_path('t').exists()
    config.pending_task = []
    assert runtime.pick().command == 'Orochi'   # 下一次再读，读到了就接上
    assert len(calls) == 2


def test_progress_save_failure_keeps_memory_and_is_retried(monkeypatch):
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi')])
    real, failures = runtime_module.save_progress, [1]

    def flaky(path, p):
        if failures:
            failures.pop()
            raise PermissionError('locked')
        real(path, p)
    monkeypatch.setattr(runtime_module, 'save_progress', flaky)

    assert runtime.pick().command == 'Orochi'   # 存盘失败不影响选任务
    assert not progress_path('t').exists()
    assert runtime.pick().command == 'Orochi'   # 进度没有变化，但上次没存成，这次补存
    assert saved().phase == 'ready'


# ---------- 崩溃恢复、停止、续跑 ----------

def test_crash_while_running_resumes_the_interrupted_row():
    crashed = Progress(phase='row_running', row_index=0, sequence_started_at=T0, row_started_at=T0,
                       heartbeat_at=T0 + timedelta(minutes=5), done_count=8, saved_params={LIMIT_COUNT: 30})
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=30)], progress=crashed)
    config.model.orochi.orochi_config.limit_count = 99
    assert runtime.pick().command == 'Orochi'
    assert config.model.orochi.orochi_config.limit_count == 30
    p = saved()
    assert p.phase == 'interrupted' and p.row_elapsed_before == 300.0 and p.done_count == 8 and p.saved_params == {}


@pytest.mark.parametrize('policy, elapsed, count', [('remaining', 600.0, 5), ('rerun', 0.0, None)])
def test_crash_resume_policy(policy, elapsed, count):
    crashed = Progress(phase='row_running', row_index=0, sequence_started_at=T0, row_started_at=T0,
                       heartbeat_at=T0 + timedelta(minutes=10), done_count=5)
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi', max_count=30)], progress=crashed, resume_policy=policy)
    runtime.pick()
    p = saved()
    assert (p.phase, p.row_elapsed_before, p.done_count) == ('interrupted', elapsed, count)


def test_stop_restores_saved_params_and_clears_them():
    leftover = Progress(phase='resting', row_index=1, sequence_started_at=T0, rest_until=T0 + timedelta(hours=1),
                        saved_params={LIMIT_COUNT: 30})
    runtime, _, config, _ = make(state='idle', progress=leftover)
    config.model.orochi.orochi_config.limit_count = 99
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    assert config.model.orochi.orochi_config.limit_count == 30 and config.saves == 1
    p = saved()
    assert p.saved_params == {} and p.phase == 'idle'


def test_restore_error_is_logged_and_saved_params_still_cleared(caplog):
    stale = Progress(phase='interrupted', row_index=0, sequence_started_at=T0,
                     saved_params={'orochi.no_such_group.limit_count': 30, LIMIT_COUNT: 30})
    runtime, _, config, _ = make(state='idle', progress=stale)
    config.model.orochi.orochi_config.limit_count = 99
    config.pending_task = [fn('restart', T0)]
    with caplog.at_level(logging.ERROR):
        runtime.pick()
    assert config.model.orochi.orochi_config.limit_count == 30   # 能还原的照常还原
    assert saved().saved_params == {}   # 清掉，免得每次选任务都重试
    assert any('no_such_group' in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)   # 对不上的字段记在日志里


def test_restore_goes_on_past_an_undeserializable_original(caplog):
    stale = Progress(phase='interrupted', row_index=0, sequence_started_at=T0,
                     saved_params={'orochi.orochi_config.layer': '不存在的层', LIMIT_COUNT: 30})   # 转不回去的排在前面
    runtime, _, config, _ = make(state='idle', progress=stale)
    config.model.orochi.orochi_config.limit_count = 99
    config.pending_task = [fn('restart', T0)]
    with caplog.at_level(logging.ERROR):
        runtime.pick()
    assert config.model.orochi.orochi_config.limit_count == 30   # 排在后面的原值照样还原了
    assert saved().saved_params == {}
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any('orochi.orochi_config.layer' in m and '不存在的层' in m for m in errors)   # 路径和原值都在日志里，人能手工改回


def test_config_save_failure_keeps_saved_params_for_the_next_pick(monkeypatch):
    stale = Progress(phase='interrupted', row_index=0, sequence_started_at=T0, saved_params={LIMIT_COUNT: 30})
    runtime, _, config, _ = make(state='idle', progress=stale)
    config.model.orochi.orochi_config.limit_count = 99
    due = fn('restart', T0)
    config.pending_task = [due]
    real_save = config.save

    def failing():
        raise OSError('disk full')
    config.save = failing
    assert runtime.pick() is due   # 还原没存成：这次按上游的选
    assert saved().saved_params == {LIMIT_COUNT: 30}   # 原值留着，下次再还原
    config.save = real_save
    runtime.pick()
    assert saved().saved_params == {} and config.saves == 1


def test_resume_same_interrupted_row_uses_remaining_limits_and_accumulates_count():
    row = OrchestraRow(task='Orochi', max_count=30, max_minutes=20)
    interrupted = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0, done_count=10,
                           row_elapsed_before=600.0)
    runtime, script, config, clock = make(rows=[row], progress=interrupted)
    cfg = config.model.orochi.orochi_config
    runtime.pick()
    runtime.before_run('Orochi')
    assert (cfg.limit_count, cfg.limit_time) == (20, dtime(0, 10))   # 30 次打了 10 次，20 分钟用了 10 分钟
    script.running_task_obj = SimpleNamespace(current_count=4)
    clock.advance(minutes=2)
    runtime.after_run('Orochi', False)
    p = saved()
    assert p.phase == 'interrupted' and p.done_count == 14   # 累计 = 底数 10 + 这一段 4
    assert p.row_elapsed_before == 600.0 + 120.0


def test_different_row_than_the_interrupted_one_gets_its_own_limits():
    rows = [OrchestraRow(task='NoSuchTask'), OrchestraRow(task='Orochi', max_count=25, max_minutes=20)]
    interrupted = Progress(phase='interrupted', row_index=0, sequence_started_at=T0, done_count=10,
                           row_elapsed_before=600.0)
    runtime, _, config, _ = make(rows=rows, progress=interrupted)
    cfg = config.model.orochi.orochi_config
    runtime.pick()   # 第 0 行无效，被跳过，选中第 1 行
    runtime.before_run('Orochi')
    assert (cfg.limit_count, cfg.limit_time) == (25, dtime(0, 20))   # 不沿用被中断那一行的剩余量（那会是 15 次、10 分钟）
    p = saved()
    assert p.row_index == 1 and p.row_elapsed_before == 0.0 and p.done_count is None
    runtime.after_run('Orochi', False)


# ---------- 行运行前后 ----------

def test_before_run_ignores_other_commands_and_vanished_rows():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    runtime.before_run('Restart')   # 桌面模式会把任务改写成 Restart：命令对不上，不动
    assert config.model.orochi.orochi_config.limit_count == 30 and saved().phase == 'ready'
    runtime.after_run('Restart', True)
    assert saved().phase == 'ready'
    config.model.orchestra.rows = []   # pick 之后，表被改空
    runtime.before_run('Orochi')
    assert config.model.orochi.orochi_config.limit_count == 30 and saved().phase == 'ready' and config.saves == 0


def test_apply_error_pauses_the_orchestration_and_the_next_pick_is_native(monkeypatch, caplog):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    assert runtime.pick().command == 'Orochi'

    def refuse(*args, **kwargs):
        raise ValueError('字段路径无法解析')
    monkeypatch.setattr(runtime_module, 'apply_row', refuse)
    with caplog.at_level(logging.ERROR):
        runtime.before_run('Orochi')   # 当前这个任务可以照常原生跑一次；before_run 本身不抛异常
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any('第 1 行' in m and 'Orochi' in m and '字段路径无法解析' in m for m in errors)   # 记错误：第几行、什么异常
    assert config.model.orchestra.state == 'paused' and config.saves == 1   # 暂停，并把暂停存进了配置
    assert config.model.orochi.orochi_config.limit_count == 30   # 参数一个没写
    assert saved().phase == 'ready' and saved().saved_params == {}   # 没起行
    runtime.after_run('Orochi', True)   # 行没起，after_run 什么都不做
    assert saved().phase == 'ready'

    due = fn('orochi', T0 - timedelta(minutes=1))
    config.pending_task = [due]
    assert runtime.pick() is due   # 已暂停：交还上游的选法，不再选这一行，它的任务不会一次次原生地跑下去
    assert saved().paused is True


@pytest.mark.parametrize('step', ['on_row_start', 'deep_set', 'config_save'])
def test_start_failure_reverts_pauses_and_hands_back_to_native(step, monkeypatch, caplog):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    assert runtime.pick().command == 'Orochi'
    if step == 'on_row_start':
        def boom(*args, **kwargs):
            raise RuntimeError('引擎出错')
        monkeypatch.setattr(runtime_module, 'on_row_start', boom)
    elif step == 'deep_set':
        real_deep_set = ConfigModel.deep_set

        def refuse_next_run(obj, keys, value):   # 只拒绝写 next_run：apply_row、restore_params 也走 deep_set，不能一起拒
            return False if str(keys).endswith('scheduler.next_run') else real_deep_set(obj, keys, value)
        monkeypatch.setattr(runtime_module.ConfigModel, 'deep_set', staticmethod(refuse_next_run))
    else:
        real_save, calls = config.save, []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('disk full')   # 写行参数的那次存配置失败，之后暂停时的那次成功
            real_save()
        config.save = flaky

    with caplog.at_level(logging.ERROR):
        runtime.before_run('Orochi')
    cfg = config.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)   # 已写的行参数撤回
    assert config.model.orchestra.state == 'paused' and config.saves == 1   # 暂停并存了配置（失败的那次不计）
    assert saved().phase == 'ready' and saved().saved_params == {}   # 进度退回开始之前
    assert heartbeat_threads() == []
    assert any('第 1 行' in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    runtime.after_run('Orochi', True)
    due = fn('orochi', T0 - timedelta(minutes=1))
    config.pending_task = [due]
    assert runtime.pick() is due   # 暂停了：下一次选任务交还上游


def test_a_failing_revert_does_not_stop_the_pause_and_keeps_the_originals_recorded(monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    runtime.pick()
    real_save, calls = config.save, []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('disk full')   # 写行参数的那次存配置失败，之后暂停时的那次成功
        real_save()
    config.save = flaky

    def boom(*args, **kwargs):
        raise RuntimeError('撤回出错')
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, 'restore_params', boom)   # 撤回本身也出错
        runtime.before_run('Orochi')   # 不抛异常
    assert config.model.orchestra.state == 'paused' and config.saves == 1   # 撤回出错不挡着暂停
    cfg = config.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (12, Layer.TWELVE)   # 行参数没撤回成，随暂停存进了配置
    p = saved()
    assert p.phase == 'row_running' and p.saved_params == {LIMIT_COUNT: 30, LAYER: '悲鸣',
                                                            NEXT_RUN: DEFAULT_NEXT_RUN.isoformat()}   # 原值不能丢

    config.pending_task = [fn('restart', T0)]
    runtime.pick()   # 下一次选任务：当作中断，用进度里的原值补还原
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN) and config.saves == 2
    p = saved()
    assert p.phase == 'interrupted' and p.saved_params == {}


def test_row_params_are_not_written_when_the_progress_could_not_be_saved(monkeypatch, caplog):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    assert runtime.pick().command == 'Orochi'
    real = runtime_module.save_progress

    def refuse_row_running(path, p):
        if p.phase == 'row_running':
            raise PermissionError('locked')   # 记着原值的那次存盘失败（重试用尽）
        real(path, p)
    monkeypatch.setattr(runtime_module, 'save_progress', refuse_row_running)
    cfg = config.model.orochi.orochi_config
    seen = []
    config.save = lambda: seen.append((cfg.limit_count, cfg.layer))   # 每次存配置的那一刻，行参数在不在
    with caplog.at_level(logging.ERROR):
        runtime.before_run('Orochi')   # 不抛异常
    assert seen == [(30, Layer.ELEVEN)]   # 只存过一次（暂停），存的时候是原值：配置文件没见过行参数
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)
    assert config.model.orchestra.state == 'paused'   # 当作启动失败：撤回、记错误、暂停
    assert any('第 1 行' in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert saved().phase == 'ready' and saved().saved_params == {}   # 进度文件里没有这一行的记录
    assert heartbeat_threads() == []
    runtime.after_run('Orochi', True)   # 行没起，什么都不做


def test_progress_is_written_before_the_row_params_reach_the_config_file():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    runtime.pick()
    seen = []
    config.save = lambda: seen.append(saved())   # 配置文件被写的那一刻，进度文件里应该已经记着原值了
    runtime.before_run('Orochi')
    assert len(seen) == 1
    assert seen[0].phase == 'row_running' and seen[0].saved_params == {LIMIT_COUNT: 30, LAYER: '悲鸣',
                                                                        NEXT_RUN: DEFAULT_NEXT_RUN.isoformat()}
    runtime.after_run('Orochi', False)


class Killed(BaseException):
    """模拟进程在两次落盘之间被杀。不是 Exception，钩子里的 except Exception 拦不住它。"""


def test_a_kill_between_the_two_writes_leaves_originals_that_recovery_restores():
    rows = [OrchestraRow(task='Orochi', sub1='神罚', max_count=12)]
    runtime, _, config, _ = make(rows=rows)
    runtime.pick()

    def die():
        raise Killed
    config.save = die   # 进度已经落盘，配置文件还没写成
    with pytest.raises(Killed):
        runtime.before_run('Orochi')
    p = saved()
    assert p.phase == 'row_running' and p.saved_params == {LIMIT_COUNT: 30, LAYER: '悲鸣',
                                                            NEXT_RUN: DEFAULT_NEXT_RUN.isoformat()}
    assert heartbeat_threads() == []   # 心跳在两次落盘之后才起

    # 重启：配置文件里还是原值，进度里是 row_running 与原值
    restarted, _, config2, _ = make(rows=rows)
    cfg = config2.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)
    restarted.pick()
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)   # 写回的是同样的原值：幂等，没写坏
    assert config2.saves == 1
    p = saved()
    assert p.phase == 'interrupted' and p.saved_params == {}


def test_double_failure_keeps_the_originals_in_the_progress():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    runtime.pick()

    def failing():
        raise OSError('disk full')
    real_save, config.save = config.save, failing   # 存配置一直失败：行参数写不进去，暂停也存不下来
    runtime.before_run('Orochi')   # 不抛异常
    cfg = config.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)   # 内存里的行参数撤回了
    assert config.model.orchestra.state == 'paused'   # 内存里已暂停，存不存得下来另说
    p = saved()
    assert p.phase == 'row_running' and p.saved_params == {LIMIT_COUNT: 30, LAYER: '悲鸣',
                                                            NEXT_RUN: DEFAULT_NEXT_RUN.isoformat()}
    runtime.after_run('Orochi', True)   # 行没起，什么都不做
    assert saved().phase == 'row_running'

    config.save = real_save   # 磁盘恢复后，下一次选任务把这当作中断，补还原
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    assert config.saves == 1
    p = saved()
    assert p.phase == 'interrupted' and p.saved_params == {}


def test_before_run_sets_next_run_to_now_and_clears_the_previous_task_object():
    runtime, script, config, _ = make(rows=[OrchestraRow(task='Orochi')])
    config.model.orochi.scheduler.next_run = T0 + timedelta(days=1)
    runtime.pick()
    script.running_task_obj = SimpleNamespace(current_count=99)   # 上一个任务的对象（pick 已清过一次，这里再放回去）
    runtime.before_run('Orochi')
    assert config.model.orochi.scheduler.next_run == T0   # 否则任务把 next_run 推回原值，就分不出完成与否
    assert script.running_task_obj is None
    assert saved().done_count is None   # 不会读到上一个任务的计数
    runtime.after_run('Orochi', False)


def test_failed_run_is_interrupted_even_if_next_run_advanced():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi')])
    runtime.pick()
    runtime.before_run('Orochi')
    config.model.orochi.scheduler.next_run = T0 + timedelta(days=1)
    clock.advance(minutes=1)
    runtime.after_run('Orochi', False)
    assert saved().phase == 'interrupted'


def test_last_row_completing_sets_state_back_to_idle():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    runtime.before_run('Orochi')
    config.model.orochi.scheduler.next_run = T0 + timedelta(days=1)
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)
    assert saved().phase == 'done' and saved().history[-1]['row_index'] == 0
    assert config.model.orchestra.state == 'idle'   # 不写回的话，下一次 sync_controls 会把 running + done 当成新的开始
    assert config.model.orochi.orochi_config.limit_count == 30
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    assert saved().phase == 'done'   # 没有重新开始


def test_table_edited_away_finishes_and_sets_state_back_to_idle():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')],
                                 progress=Progress(phase='ready', row_index=3, sequence_started_at=T0))
    config.pending_task = [fn('restart', T0)]
    assert runtime.pick() is config.pending_task[0]   # 当前序号之后没有可运行的行：跑完了，交还上游
    assert saved().phase == 'done'
    assert config.model.orchestra.state == 'idle' and config.saves == 1


def test_after_run_error_leaves_progress_recoverable(monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    runtime.before_run('Orochi')

    def boom(*args, **kwargs):
        raise RuntimeError('引擎出错')
    with monkeypatch.context() as patch:   # 只撤销这里的补丁，不动 isolated 里的 chdir
        patch.setattr(runtime_module, 'on_row_end', boom)
        runtime.after_run('Orochi', True)   # 不抛异常，线程也停了
    assert heartbeat_threads() == []
    p = saved()
    assert p.phase == 'row_running' and p.saved_params == {LIMIT_COUNT: 30, NEXT_RUN: DEFAULT_NEXT_RUN.isoformat()}   # 进度没推进，原值还记着
    runtime.after_run('Orochi', True)   # 那一行已经收过尾（虽然出错了）：再调用什么都不做，不会补推进度
    assert saved().phase == 'row_running'
    config.model.orochi.orochi_config.limit_count = 12   # 假设这次的还原没能落到配置里
    config.pending_task = [fn('restart', T0)]
    runtime.pick()   # 下一次选任务时，row_running 没有对应的运行中的行：当作中断处理并补还原
    assert config.model.orochi.orochi_config.limit_count == 30
    p = saved()
    assert p.phase == 'interrupted' and p.saved_params == {}


# ---------- 心跳线程（裁定 8） ----------

def test_heartbeat_survives_failing_saves(monkeypatch):
    runtime, script, _, _ = make(rows=[OrchestraRow(task='Orochi', max_count=30)], interval=0.02)
    runtime.pick()
    runtime.before_run('Orochi')
    real, calls = runtime_module.save_progress, []

    def flaky(path, p):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError('locked')   # OSError：只记警告
        if len(calls) == 2:
            raise RuntimeError('boom')   # 其他异常：线程里记日志，线程不死
        real(path, p)
    monkeypatch.setattr(runtime_module, 'save_progress', flaky)   # 心跳线程每次存盘时才查这个名字

    script.running_task_obj = SimpleNamespace(current_count=3)
    try:
        assert wait_for(lambda: len(calls) >= 3 and saved().done_count == 3)
        assert len(heartbeat_threads()) == 1   # 前两次存盘失败之后，线程还活着
    finally:
        runtime.after_run('Orochi', False)
    assert heartbeat_threads() == []


def test_heartbeat_adds_the_previous_count_of_a_resumed_row():
    interrupted = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0, done_count=10,
                           row_elapsed_before=60.0)
    runtime, script, _, _ = make(rows=[OrchestraRow(task='Orochi', max_count=30)], progress=interrupted, interval=0.02)
    runtime.pick()
    runtime.before_run('Orochi')
    script.running_task_obj = SimpleNamespace(current_count=4)   # 这一段自己打了 4 次
    try:
        assert wait_for(lambda: saved().done_count == 14)   # 进度里是累计：此前的 10 次加这一段的 4 次
    finally:
        runtime.after_run('Orochi', False)
    assert saved().done_count == 14


def test_heartbeat_interval_has_a_floor(monkeypatch):
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi')], interval=0)   # 间隔 0 会让 Event.wait 立刻返回，线程空转
    runtime.pick()
    runtime.before_run('Orochi')
    calls = []
    monkeypatch.setattr(runtime_module, 'save_progress', lambda path, p: calls.append(1))
    time.sleep(0.15)
    runtime.after_run('Orochi', False)
    assert len(calls) < 100   # 下限 0.01 秒：0.15 秒里最多十几次；没有下限会是几万次


def test_non_integer_counter_is_ignored():
    runtime, script, _, clock = make(rows=[OrchestraRow(task='Orochi', max_count=30)])
    runtime.pick()
    runtime.before_run('Orochi')
    script.running_task_obj = SimpleNamespace(current_count='7')   # 不是整数：不当作次数
    clock.advance(minutes=1)
    runtime.after_run('Orochi', False)
    p = saved()
    assert p.phase == 'interrupted' and p.done_count is None


def test_heartbeat_thread_that_cannot_start_does_not_break_the_row(monkeypatch):
    rows = [OrchestraRow(task='Orochi', sub1='神罚', max_count=12), OrchestraRow(task='Orochi')]
    runtime, _, config, clock = make(rows=rows)
    runtime.pick()

    def no_thread(self):
        raise RuntimeError("can't start new thread")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, 'start', no_thread)
        runtime.before_run('Orochi')   # 不抛异常；行已经开始，只是没有心跳
    cfg = config.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (12, Layer.TWELVE) and saved().phase == 'row_running'
    config.model.orochi.scheduler.next_run = T0 + timedelta(days=1)
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)   # 照常收尾：还原、判完成、推进，不会去 join 没启动的线程
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)
    p = saved()
    assert p.phase == 'resting' and p.row_index == 1 and p.saved_params == {}


def test_heartbeat_leaves_done_count_alone_without_a_reliable_counter():
    # SixRealms 没有可靠的实例计数属性（counter_attr 为 None）：只写心跳时间，次数保持原样
    runtime, script, _, clock = make(rows=[OrchestraRow(task='SixRealms')], interval=0.02)
    runtime.pick()
    runtime.before_run('SixRealms')
    clock.advance(minutes=1)
    script.running_task_obj = SimpleNamespace(current_count=7)
    try:
        assert wait_for(lambda: saved().heartbeat_at == clock.now)
        assert saved().done_count is None
    finally:
        runtime.after_run('SixRealms', False)
    assert saved().done_count is None


def test_after_run_stops_the_heartbeat_before_touching_progress():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi')], interval=0.01)
    runtime.pick()
    runtime.before_run('Orochi')
    assert len(heartbeat_threads()) == 1
    config.model.orochi.scheduler.next_run = T0 + timedelta(days=1)
    clock.advance(minutes=1)
    runtime.after_run('Orochi', True)
    assert heartbeat_threads() == []
    time.sleep(0.05)   # 间隔只有 0.01 秒：线程若没停，这段时间里会有心跳把 heartbeat_at 又写上
    p = saved()
    assert p.phase == 'done' and p.heartbeat_at is None   # on_row_end 清掉了心跳，之后没有人再写


# ---------- 兜底（裁定 11） ----------

def test_internal_error_in_pick_falls_back_to_native(monkeypatch, caplog):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    assert runtime.pick().command == 'Orochi'   # 先有一个 row 决定

    def boom(*args, **kwargs):
        raise RuntimeError('引擎出错')
    monkeypatch.setattr(runtime_module, 'sync_controls', boom)
    due = fn('restart', T0)
    config.pending_task = [due]
    with caplog.at_level(logging.ERROR):
        assert runtime.pick() is due   # 退回上游的选法
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
    runtime.before_run('Orochi')   # 上一轮的 row 决定已作废，不会因此写参数
    assert config.model.orochi.orochi_config.limit_count == 30 and config.saves == 0


def test_error_after_the_decision_is_made_still_voids_it(monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])

    def boom(*args, **kwargs):
        raise RuntimeError('造不出 Function')
    monkeypatch.setattr(runtime_module, '_function', boom)
    config.waiting_task = [fn('restart', T0 + timedelta(hours=1))]   # 上游的选法：两表里只有它
    assert runtime.pick().command == 'Restart'   # decide 已经定了 row，造 Function 时出错：退回上游的选法
    runtime.before_run('Orochi')   # 那个 row 决定不能留下来
    assert config.model.orochi.orochi_config.limit_count == 30 and config.saves == 0


def test_internal_error_in_pick_still_raises_upstreams_human_takeover_when_stopped(monkeypatch):
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi')], state='idle')

    def boom(*args, **kwargs):
        raise RuntimeError('引擎出错')
    monkeypatch.setattr(runtime_module, 'sync_controls', boom)
    with pytest.raises(RequestHumanTakeover):   # 编排没在进行：兜底就是上游自己的 get_next，它抛什么就抛什么
        runtime.pick()


@pytest.mark.parametrize('state', ['running', 'paused'])
def test_internal_error_fallback_waits_instead_of_exiting_while_orchestrating(state, monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')], state=state)   # 两张表都空

    def boom(*args, **kwargs):
        raise RuntimeError('引擎出错')
    monkeypatch.setattr(runtime_module, 'sync_controls', boom)
    picked = runtime.pick()   # 上游会请求人工接管、实例退出；编排在进行，不能这样
    assert (picked.command, picked.next_run) == ('Orochi', T0 + timedelta(minutes=5))
    runtime.before_run('Orochi')   # 占位不是编排的行
    runtime.after_run('Orochi', True)
    assert config.saves == 0


def test_fallback_reraises_when_the_orchestra_state_cannot_be_read(monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')])

    def break_the_model(*args, **kwargs):
        config.model = None   # 连 orchestra.state 都读不到
        raise RuntimeError('引擎出错')
    monkeypatch.setattr(runtime_module, 'sync_controls', break_the_model)
    with pytest.raises(RequestHumanTakeover):   # 读不到就照上游：原样重抛
        runtime.pick()


def test_internal_error_in_before_run_is_swallowed(monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    runtime.pick()

    def boom(*args, **kwargs):
        raise RuntimeError('引擎出错')
    monkeypatch.setattr(runtime_module, 'on_row_start', boom)
    runtime.before_run('Orochi')   # 不抛异常
    cfg = config.model.orochi.orochi_config
    assert (cfg.limit_count, cfg.layer) == (30, Layer.ELEVEN)   # 已写的参数撤回，没有进度记着原值就不能留
    assert config.model.orchestra.state == 'paused'   # 失败的一行不再被反复选中
    assert heartbeat_threads() == []
    runtime.after_run('Orochi', True)


def test_pick_during_a_row_treats_it_as_interrupted():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    runtime.before_run('Orochi')   # 之后 after_run 没有被调用
    clock.advance(minutes=2)
    config.pending_task = [fn('restart', T0)]
    runtime.pick()   # 选任务时不该有行在跑：停心跳，按中断处理，补还原
    assert heartbeat_threads() == []
    assert config.model.orochi.orochi_config.limit_count == 30
    p = saved()
    assert p.phase == 'interrupted' and p.saved_params == {}


# ---------- 配置对象不跨调用持有（裁定 1） ----------

def test_pick_uses_the_config_current_at_each_call():
    runtime, script, old, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    assert runtime.pick().command == 'Orochi'
    new = FakeConfig()   # 上游 del_cached_property 之后重建出来的 config，里面有个待办的 Restart
    new.model.orchestra.rows = list(old.model.orchestra.rows)
    new.model.orchestra.state = 'running'
    new.pending_task = [fn('restart', T0)]
    script.config = new
    assert runtime.pick() is new.pending_task[0]   # 选的是新 config 里的待办，不是旧的


def test_runtime_follows_the_config_across_rows():
    # 上游每个任务成功之后都 del_cached_property(config)，下一轮用的是重新读出来的新 config
    rows = [OrchestraRow(task='Orochi', max_count=12), OrchestraRow(task='Orochi', max_count=15)]
    runtime, script, first, clock = make(rows=rows, default_rest_minutes=0)
    second = FakeConfig()
    second.model.orchestra.rows, second.model.orchestra.state = list(rows), 'running'
    second.model.orchestra.default_rest_minutes = 0
    for config, expected in ((first, 12), (second, 15)):
        script.config = config
        assert runtime.pick().command == 'Orochi'
        runtime.before_run('Orochi')
        assert config.model.orochi.orochi_config.limit_count == expected
        config.model.orochi.scheduler.next_run = clock.now + timedelta(days=1)
        clock.advance(minutes=10)
        runtime.after_run('Orochi', True)
        assert config.model.orochi.orochi_config.limit_count == 30
    assert first.saves == 2   # 第二行没有再碰旧 config：第一行里存了两次（写参数、还原）
    assert second.saves == 3   # 写参数、还原，加上跑完把 state 写回 idle


def test_runtime_always_works_on_the_current_config():
    runtime, script, old, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    new = FakeConfig()   # 上游 del_cached_property 之后重建出来的 config
    new.model.orchestra.rows = list(old.model.orchestra.rows)
    new.model.orchestra.state = 'running'
    script.config = new
    runtime.before_run('Orochi')
    assert new.model.orochi.orochi_config.limit_count == 12 and old.model.orochi.orochi_config.limit_count == 30
    runtime.after_run('Orochi', False)
    assert new.model.orochi.orochi_config.limit_count == 30 and old.saves == 0


def test_after_run_works_on_the_reloaded_model():
    class ReloadingConfig(FakeConfig):
        """reload 换一个新的模型，像真实的 Config 那样；新模型里任务已经把 next_run 推到了明天。"""
        def reload(self):
            super().reload()
            self.stale = self.model
            self.model = copy.deepcopy(self.stale)
            self.model.orochi.scheduler.next_run = T0 + timedelta(days=1)

    runtime, script, _, clock = make()
    config = script.config = ReloadingConfig()
    config.model.orchestra.rows = [OrchestraRow(task='Orochi', max_count=12), OrchestraRow(task='Orochi')]
    config.model.orchestra.state = 'running'
    runtime.pick()
    runtime.before_run('Orochi')
    assert config.model.orochi.orochi_config.limit_count == 12
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)
    assert config.model.orochi.orochi_config.limit_count == 30      # 还原写在重载后的新模型上
    assert config.stale.orochi.orochi_config.limit_count == 12      # 旧模型没人碰
    assert saved().phase == 'resting'                                # 完成与否也凭新模型里的 next_run 判定


# ---------- 真实的 Config：行参数经过配置文件往返 ----------

def real_config_script(rows) -> tuple[SimpleNamespace, Config]:
    """tmp_path/config/t.json 里写好编排配置，脚本替身用真实的 Config 读它：reload、save 都是上游的真实现。"""
    model = ConfigModel()
    model.restart.scheduler.enable = False   # 默认启用的 Restart 会成为待办；这里要两表皆空，由编排的行来顶
    model.orchestra.rows, model.orchestra.state = list(rows), 'running'
    ConfigModel.write_json('t', model.model_dump())
    config = Config('t')
    return SimpleNamespace(config=config, config_name='t', anti_ban_guard=AntiBanGuard(), running_task_obj=None), config


@REAL_CONFIG
def test_row_cycle_through_the_real_config_file():
    script, config = real_config_script([OrchestraRow(task='Orochi', sub1='神罚', max_count=12),
                                         OrchestraRow(task='Orochi')])
    runtime = OrchestraRuntime(script, rng=random.Random(0))   # 真实时钟
    assert runtime.pick().command == 'Orochi'
    runtime.before_run('Orochi')
    on_disk = ConfigModel('t').orochi   # 任务内部的 task_delay 会先从文件重载：行参数这时必须已经在文件里
    assert (on_disk.orochi_config.limit_count, on_disk.orochi_config.layer) == (12, Layer.TWELVE)

    config.task_delay('Orochi', success=True, server=False)   # 任务正常结束时做的事：重载、把 next_run 推到明天、存盘
    runtime.after_run('Orochi', True)
    restored = ConfigModel('t').orochi
    assert (restored.orochi_config.limit_count, restored.orochi_config.layer) == (30, Layer.ELEVEN)
    assert restored.scheduler.next_run > datetime.now()   # 还原没有冲掉任务自己写的 next_run
    p = saved()
    assert p.phase == 'resting' and p.row_index == 1 and p.saved_params == {}


@REAL_CONFIG
def test_interrupted_row_through_the_real_config_file():
    script, _ = real_config_script([OrchestraRow(task='Orochi', sub1='神罚', max_count=12)])
    runtime = OrchestraRuntime(script, rng=random.Random(0))
    runtime.pick()
    runtime.before_run('Orochi')
    runtime.after_run('Orochi', True)   # run 返回了，但任务没走到 task_delay：文件里的 next_run 还是 before_run 写的那个
    restored = ConfigModel('t').orochi
    assert (restored.orochi_config.limit_count, restored.orochi_config.layer) == (30, Layer.ELEVEN)
    assert restored.scheduler.next_run == DEFAULT_NEXT_RUN   # 没完成：next_run 也回到行开始之前
    assert saved().phase == 'interrupted'


# ---------- 懒建与转调 ----------

def test_runtime_of_is_lazy_and_cached_on_the_script():
    script = SimpleNamespace(config=FakeConfig())
    assert '_orchestra' not in script.__dict__
    first = runtime_of(script)
    assert isinstance(first, OrchestraRuntime) and script.__dict__['_orchestra'] is first
    assert runtime_of(script) is first


def test_module_level_hooks_delegate_to_the_runtime():
    calls = []
    stub = SimpleNamespace(pick=lambda: calls.append('pick') or 'function',
                           before_run=lambda command: calls.append(('before', command)),
                           after_run=lambda command, success: calls.append(('after', command, success)))
    script = SimpleNamespace()
    script.__dict__['_orchestra'] = stub
    assert orchestra_pick(script) == 'function'
    orchestra_before_run(script, 'Orochi')
    orchestra_after_run(script, 'Orochi', True)
    assert calls == ['pick', ('before', 'Orochi'), ('after', 'Orochi', True)]


# ---------- 最终评审修正 ----------

# I1：行没完成时，任务自己的 next_run 也还原；否则它停在"现在"，被上游当成到点任务按原生设置再跑一轮

def test_interrupted_row_puts_the_native_next_run_back():
    runtime, _, config, clock = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    tomorrow = T0 + timedelta(days=1)
    config.model.orochi.scheduler.next_run = tomorrow   # 用户原本排在明天
    runtime.pick()
    runtime.before_run('Orochi')
    assert config.model.orochi.scheduler.next_run == T0   # 行开始：设为现在，用来判完成
    assert saved().saved_params[NEXT_RUN] == tomorrow.isoformat()
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)   # 任务没走到 task_delay：没完成
    assert saved().phase == 'interrupted'
    assert config.model.orochi.scheduler.next_run == tomorrow   # 回到明天，不会成为到点的待办


def test_completed_row_keeps_the_next_run_the_task_wrote():
    rows = [OrchestraRow(task='Orochi', max_count=12), OrchestraRow(task='Orochi')]
    runtime, _, config, clock = make(rows=rows)
    config.model.orochi.scheduler.next_run = T0 + timedelta(days=3)
    runtime.pick()
    runtime.before_run('Orochi')
    config.model.orochi.scheduler.next_run = T0 + timedelta(hours=2)   # 任务正常结束时自己推的时间
    clock.advance(minutes=3)
    runtime.after_run('Orochi', True)
    assert saved().phase == 'resting'
    assert config.model.orochi.scheduler.next_run == T0 + timedelta(hours=2)   # 不还原成三天后
    assert config.model.orochi.orochi_config.limit_count == 30   # 其余原值照常还原


def test_crash_recovery_puts_the_native_next_run_back_too():
    tomorrow = T0 + timedelta(days=1)
    crashed = Progress(phase='row_running', row_index=0, row_task='Orochi', sequence_started_at=T0, row_started_at=T0,
                       saved_params={LIMIT_COUNT: 30, NEXT_RUN: tomorrow.isoformat()})
    runtime, _, config, _ = make(state='idle', progress=crashed)   # 停止之后才重启
    config.model.orochi.orochi_config.limit_count = 12
    config.model.orochi.scheduler.next_run = T0   # 崩溃时留在配置里的"现在"
    config.pending_task = [fn('restart', T0)]
    runtime.pick()
    assert (config.model.orochi.orochi_config.limit_count, config.model.orochi.scheduler.next_run) == (30, tomorrow)
    assert saved().saved_params == {}


def test_a_failed_start_puts_the_native_next_run_back():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    tomorrow = T0 + timedelta(days=1)
    config.model.orochi.scheduler.next_run = tomorrow
    runtime.pick()
    real_save, calls = config.save, []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('disk full')   # 写行参数的那次存配置失败：next_run 这时在内存里已是现在
        real_save()
    config.save = flaky
    runtime.before_run('Orochi')
    assert config.model.orchestra.state == 'paused'
    assert (config.model.orochi.orochi_config.limit_count, config.model.orochi.scheduler.next_run) == (30, tomorrow)


def test_an_unreadable_native_next_run_stops_the_row_before_anything_is_written(monkeypatch):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    real_get = ConfigModel.deep_get

    def hide_next_run(obj, keys, default=None):
        return default if str(keys).endswith('scheduler.next_run') else real_get(obj, keys, default)
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module.ConfigModel, 'deep_get', staticmethod(hide_next_run))
        runtime.before_run('Orochi')
    assert config.model.orchestra.state == 'paused' and config.model.orochi.orochi_config.limit_count == 30
    assert saved().phase == 'ready' and saved().saved_params == {}


@REAL_CONFIG
@pytest.mark.filterwarnings('ignore:Pydantic serializer warnings:UserWarning')   # 上游默认值里写成字符串的整数字段
def test_an_interrupted_climb_row_is_not_rerun_natively_as_a_cut_in():
    # 爬塔既在表里又在插队名单里，用户自己把它排在明天。行没完成时 next_run 若停在"现在"，
    # 下一次选任务它就成了到点的插队任务，按用户自己的原生设置跑最多 30 分钟
    model = ConfigModel()
    model.restart.scheduler.enable = False
    tomorrow = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
    model.activity_shikigami.scheduler.enable, model.activity_shikigami.scheduler.next_run = True, tomorrow
    model.orchestra.rows = [OrchestraRow(task='ActivityShikigami', sub1='ap', max_count=50)]
    model.orchestra.state = 'running'
    ConfigModel.write_json('t', model.model_dump())
    script = SimpleNamespace(config=Config('t'), config_name='t', anti_ban_guard=AntiBanGuard(), running_task_obj=None)
    runtime = OrchestraRuntime(script, rng=random.Random(0))
    assert runtime.pick().command == 'ActivityShikigami'
    runtime.before_run('ActivityShikigami')
    runtime.after_run('ActivityShikigami', True)   # 没走到 task_delay：没完成
    assert ConfigModel('t').activity_shikigami.scheduler.next_run == tomorrow
    script.config = Config('t')   # 上游每轮之后重建 config
    assert runtime.pick().command == 'ActivityShikigami'
    assert runtime._decision.kind == 'row'   # 续跑表里的这一行，不是按原生设置插队


# I2：上游的御魂溢出自愈 SoulsTidy 在编排进行中照样放行

def test_souls_tidy_is_let_through_during_the_sequence():
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')], cut_in_tasks=[])
    tidy = fn('souls_tidy', T0 - timedelta(minutes=1))
    config.pending_task = [fn('orochi', T0 - timedelta(hours=1)), tidy]
    assert runtime.pick() is tidy


# I4：进度文件搬到 log/orchestra/，旧位置的文件在第一次读进度时搬过来

def test_a_legacy_progress_file_is_adopted_with_its_originals():
    crashed = Progress(phase='row_running', row_index=0, row_task='Orochi', sequence_started_at=T0, row_started_at=T0,
                       heartbeat_at=T0 + timedelta(minutes=5), done_count=8, saved_params={LIMIT_COUNT: 30})
    save_progress(legacy_progress_path('t'), crashed)   # 旧版本写在 log/ 根目录下的、进行中的进度
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=30)])
    config.model.orochi.orochi_config.limit_count = 99
    assert runtime.pick().command == 'Orochi'
    assert not legacy_progress_path('t').exists()
    assert config.model.orochi.orochi_config.limit_count == 30   # 原值随文件搬了过来，照样还原
    p = saved()
    assert (p.phase, p.row_elapsed_before, p.done_count, p.saved_params) == ('interrupted', 300.0, 8, {})


def test_a_legacy_file_that_cannot_be_moved_yet_is_retried_on_the_next_pick(monkeypatch, caplog):
    resting = Progress(phase='resting', row_index=0, sequence_started_at=T0, rest_until=T0 + timedelta(minutes=5))
    save_progress(legacy_progress_path('t'), resting)
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi')])
    real, calls = runtime_module.adopt_legacy_progress, []

    def flaky(name):
        calls.append(name)
        if len(calls) == 1:
            raise PermissionError('locked')
        return real(name)
    monkeypatch.setattr(runtime_module, 'adopt_legacy_progress', flaky)
    due = fn('restart', T0)
    config.pending_task = [due]
    with caplog.at_level(logging.WARNING):
        assert runtime.pick() is due   # 这一次按上游的选，编排不插手
    assert [r for r in warnings(caplog) if '读取失败' in r.getMessage()]
    assert legacy_progress_path('t').exists() and not progress_path('t').exists()
    config.pending_task = []
    picked = runtime.pick()   # 下一次搬成了，接着旧文件里的休息
    assert (picked.command, picked.next_run) == ('Orochi', T0 + timedelta(minutes=5, seconds=1))
    assert not legacy_progress_path('t').exists() and saved().phase == 'resting'


# N1：表在运行中被改过、同一个行号换了任务：不沿用被打断那一行的剩余量

def test_a_row_edited_into_another_task_gets_its_own_limits():
    interrupted = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0, done_count=10,
                           row_elapsed_before=600.0)
    runtime, _, config, _ = make(rows=[OrchestraRow(task='GoryouRealm', max_count=25, max_minutes=20)],
                                 progress=interrupted)
    assert runtime.pick().command == 'GoryouRealm'
    runtime.before_run('GoryouRealm')
    cfg = config.model.goryou_realm.goryou_config
    assert (cfg.limit_count, cfg.limit_time) == (25, dtime(0, 20))   # 不是 15 次、10 分钟
    p = saved()
    assert (p.row_index, p.row_task, p.row_elapsed_before, p.done_count) == (0, 'GoryouRealm', 0.0, None)
    runtime.after_run('GoryouRealm', False)


def test_the_progress_file_names_the_task_of_the_running_row():
    runtime, _, _, _ = make(rows=[OrchestraRow(task='NoSuchTask'), OrchestraRow(task='GoryouRealm')])
    runtime.pick()
    runtime.before_run('GoryouRealm')
    assert (saved().row_index, saved().row_task) == (1, 'GoryouRealm')
    runtime.after_run('GoryouRealm', False)
    assert (saved().phase, saved().row_task) == ('interrupted', 'GoryouRealm')


# N3：选任务时放下上一个任务的对象

@pytest.mark.parametrize('state', ['idle', 'running'])
def test_pick_lets_go_of_the_previous_task_object(state):
    runtime, script, config, _ = make(rows=[OrchestraRow(task='Orochi')], state=state)
    config.pending_task = [fn('restart', T0)]
    script.running_task_obj = SimpleNamespace(current_count=99)   # 上一个任务的对象，空闲等待期间不必还留着
    runtime.pick()
    assert script.running_task_obj is None


# N6：编排进行中每次选任务记一行 INFO

def orchestra_info(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == 'oas' and r.levelno == logging.INFO and r.getMessage().startswith('编排：')]


def test_each_pick_logs_its_decision_while_orchestrating(caplog):
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='GoryouRealm')]
    resting = Progress(phase='resting', row_index=1, sequence_started_at=T0, rest_until=T0 + timedelta(minutes=5))
    runtime, _, config, clock = make(rows=rows, progress=resting)
    with caplog.at_level(logging.INFO):
        runtime.pick()                                  # 休息中
        config.pending_task = [fn('meta_demon', T0)]
        runtime.pick()                                  # 插队
        config.pending_task = []
        clock.advance(minutes=6)
        runtime.pick()                                  # 休息完，下一行
        config.model.orchestra.state = 'paused'
        config.pending_task = [fn('restart', T0)]
        runtime.pick()                                  # 暂停
    lines = orchestra_info(caplog)
    assert len(lines) == 4   # 每次选任务一行
    assert '休息' in lines[0] and '10:05:00' in lines[0]
    assert 'MetaDemon' in lines[1]
    assert '第 2 行' in lines[2] and 'GoryouRealm' in lines[2]
    assert '暂停' in lines[3]


def test_an_idle_instance_logs_nothing_about_the_orchestration(caplog):
    runtime, _, config, _ = make(state='idle')
    config.pending_task = [fn('orochi', T0)]
    with caplog.at_level(logging.INFO):
        runtime.pick()
    assert not [r for r in caplog.records if '编排' in r.getMessage()]


# N7：休息等待的占位多等 1 秒

def test_the_rest_placeholder_is_due_a_second_after_the_rest_ends():
    # 上游拿到占位时若已到点（休息截止与上游取 now 只差几毫秒），会把占位的任务名当成到点任务直接跑，
    # before_run 认不出是行，任务就按原生设置跑了一轮。多等 1 秒，上游睡过这一刻再来选
    rest_until = T0 + timedelta(minutes=5)
    resting = Progress(phase='resting', row_index=0, sequence_started_at=T0, rest_until=rest_until)
    runtime, _, _, clock = make(rows=[OrchestraRow(task='Orochi')], progress=resting)
    clock.now = rest_until - timedelta(milliseconds=1)
    assert runtime.pick().next_run == rest_until + timedelta(seconds=1)


# N8：错误日志带上异常的调用栈

def test_a_start_failure_is_logged_with_its_traceback(monkeypatch, caplog):
    runtime, _, _, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()

    def refuse(*args, **kwargs):
        raise ValueError('字段路径无法解析')
    monkeypatch.setattr(runtime_module, 'apply_row', refuse)
    with caplog.at_level(logging.ERROR):
        runtime.before_run('Orochi')
    records = [r for r in caplog.records if r.levelno >= logging.ERROR and '启动失败' in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is not None and records[0].exc_info[0] is ValueError


def test_a_failing_revert_is_logged_with_its_traceback(monkeypatch, caplog):
    runtime, _, config, _ = make(rows=[OrchestraRow(task='Orochi', max_count=12)])
    runtime.pick()
    real_save, calls = config.save, []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('disk full')
        real_save()
    config.save = flaky

    def boom(*args, **kwargs):
        raise RuntimeError('撤回出错')
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(runtime_module, 'restore_params', boom)
        runtime.before_run('Orochi')
    records = [r for r in caplog.records if r.levelno >= logging.ERROR and r.getMessage().startswith('编排：撤回第')]
    assert len(records) == 1 and records[0].exc_info is not None


def test_a_restore_error_is_logged_with_its_traceback(caplog):
    stale = Progress(phase='interrupted', row_index=0, sequence_started_at=T0,
                     saved_params={'orochi.no_such_group.limit_count': 30})
    runtime, _, config, _ = make(state='idle', progress=stale)
    config.pending_task = [fn('restart', T0)]
    with caplog.at_level(logging.ERROR):
        runtime.pick()
    records = [r for r in caplog.records if r.levelno >= logging.ERROR and '没能还原' in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is not None
