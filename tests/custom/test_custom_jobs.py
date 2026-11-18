"""后台任务线程与每日定时合并（module.server.custom_jobs）的测试。

每个用例都先 chdir 到 tmp_path：状态文件 log/custom_jobs/state.json 和 .env 都落在那里，不碰仓库自己的 config/、log/ 和 .env。
fork_updater.run_update 一律换成假的（自动夹具 fake_update），没有哪个用例会真去合并，也不联网。
事件循环是真的，跑在辅助线程里；时钟是假的，用 tick() 一轮一轮驱动，不靠睡眠。起真线程的用例只做有限时长的等待。
本文件不导入 module.server.app，也不导入 main_manager（它一导入就扫 config/、起线程）；app.py 的接线用 ast 静态核对。
"""
import ast
import asyncio
import gc
import threading
import time
import warnings
from datetime import date, datetime
from pathlib import Path

import pytest

from module.server import custom_jobs, custom_routes, fork_updater
from module.server.custom_jobs import CustomJobs, merge_due, register_job, start_custom_jobs

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_TIME_KEY = 'OAS_UPSTREAM_MERGE_TIME'
UP_TO_DATE = '已是最新，无需合并'


# ---- merge_due：计划给的四个用例，原样 -------------------------------------------------------------------------

def test_disabled_when_empty():
    assert merge_due(datetime(2026, 11, 17, 5), None, None) is False


def test_due_after_time_once_per_day():
    assert merge_due(datetime(2026, 11, 17, 4, 31), '04:30', None) is True
    assert merge_due(datetime(2026, 11, 17, 4, 31), '04:30', date(2026, 11, 17)) is False


def test_missed_minute_runs_once_later_same_day():
    assert merge_due(datetime(2026, 11, 17, 9, 0), '04:30', date(2026, 11, 16)) is True


def test_not_due_before_time():
    assert merge_due(datetime(2026, 11, 17, 4, 29), '04:30', date(2026, 11, 16)) is False


# ---- merge_due：边界 -------------------------------------------------------------------------------------------

def test_due_from_the_minute_itself_but_not_a_second_before():
    assert merge_due(datetime(2026, 11, 17, 4, 30, 0), '04:30', None) is True
    assert merge_due(datetime(2026, 11, 17, 4, 29, 59), '04:30', None) is False


def test_the_edges_of_the_day():
    assert merge_due(datetime(2026, 11, 17, 0, 0), '00:00', None) is True
    assert merge_due(datetime(2026, 11, 17, 23, 58), '23:59', None) is False
    assert merge_due(datetime(2026, 11, 17, 23, 59), '23:59', None) is True


def test_a_new_day_runs_again():
    assert merge_due(datetime(2026, 11, 18, 4, 30), '04:30', date(2026, 11, 17)) is True


def test_a_remembered_date_after_today_does_not_switch_the_merge_off():
    # 时钟被往回拨过：宁可多补一次，也不让状态文件里的未来日期把定时合并关上几个月
    assert merge_due(datetime(2026, 11, 17, 5, 0), '04:30', date(2026, 11, 20)) is True


@pytest.mark.parametrize('hhmm', ['', '24:00', '25:00', '12:60', '99:99', '4:30', '04:5', '0430', '04:30:00', '04-30',
                                  'abc', '-1:00'])
def test_invalid_times_disable_the_merge(hhmm):
    assert merge_due(datetime(2026, 11, 17, 23, 59), hhmm, None) is False


@pytest.mark.parametrize('text, expected', [
    ('00:00', (0, 0)), ('04:30', (4, 30)), ('09:05', (9, 5)), ('19:59', (19, 59)), ('23:59', (23, 59)),
    ('24:00', None), ('29:00', None), ('30:00', None), ('12:60', None), ('4:30', None), ('04:5', None),
    ('04:30:00', None), ('0430', None), ('04.30', None), ('abc', None), ('', None), (None, None),
])
def test_hhmm_parsing_is_strict(text, expected):
    # 24:00 这类值用 merge_due 看不出区别（时钟永远走不到它，结果同样是 False），所以单独核对解析本身
    assert custom_jobs._parse_hhmm(text) == expected


# ---- 状态文件 log/custom_jobs/state.json -----------------------------------------------------------------------

def state_file() -> Path:
    return Path('log') / 'custom_jobs' / 'state.json'


def test_the_state_round_trips_through_a_file_in_a_subdirectory():
    custom_jobs._save_last_run(date(2026, 11, 17))
    assert state_file().is_file()                                    # 子目录：上游的 14 天清理够不着
    assert custom_jobs._load_last_run() == date(2026, 11, 17)
    assert list(state_file().parent.iterdir()) == [state_file()]     # atomic_write 的临时文件不留


def test_a_later_save_replaces_the_date():
    custom_jobs._save_last_run(date(2026, 11, 17))
    custom_jobs._save_last_run(date(2026, 11, 18))
    assert custom_jobs._load_last_run() == date(2026, 11, 18)


def test_a_missing_state_file_means_never_ran_and_is_not_logged(log):
    assert custom_jobs._load_last_run() is None
    assert log.records == []                                         # 首次运行的常态


@pytest.mark.parametrize('content', [
    b'', b'{', b'\xff\xfe\x00garbage', b'[]', b'"2026-11-17"', b'null', b'{}',
    b'{"upstream_merge_last_run": 20261117}', b'{"upstream_merge_last_run": "yesterday"}',
    b'{"upstream_merge_last_run": "2026-13-45"}',
])
def test_a_corrupt_state_file_means_never_ran(content, log):
    state_file().parent.mkdir(parents=True)
    state_file().write_bytes(content)
    assert custom_jobs._load_last_run() is None
    assert log.levels == ['warning']


def test_an_unreadable_state_file_means_never_ran(log):
    state_file().mkdir(parents=True)                                 # 路径是个目录：读它抛 OSError
    assert custom_jobs._load_last_run() is None
    assert log.levels == ['warning']


# ---- 夹具与替身 ------------------------------------------------------------------------------------------------

class LogSpy:
    """换掉 custom_jobs.logger：按级别记下消息，不往真日志里写。"""

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

    def messages(self, level: str) -> list[str]:
        return [message for kind, message in self.records if kind == level]

    def mentions(self, text: str) -> bool:
        return any(text in message for _, message in self.records)


class FakeUpdate:
    """换掉 fork_updater.run_update：记下被调用的次数和跑在哪个事件循环上。error 非空就抛它，delay 大于 0 就先等一会儿。"""

    def __init__(self):
        self.message = UP_TO_DATE
        self.error: Exception | None = None
        self.delay = 0.0
        self.calls = 0
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.finished = threading.Event()

    async def __call__(self) -> str:
        self.calls += 1
        self.loops.append(asyncio.get_running_loop())
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return self.message
        finally:
            self.finished.set()


class Clock:
    """假时钟：墙上时间给每日合并用，单调时间给周期任务用，由用例手动拨。"""

    def __init__(self, now: datetime):
        self.now = now
        self.mono = 1000.0

    def wall(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """有限时长地等 predicate 成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def job_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == custom_jobs.THREAD_NAME]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """cwd 切到 tmp_path；环境变量里的合并时间清掉，.env 在 tmp_path 里本来就没有。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(MERGE_TIME_KEY, raising=False)


@pytest.fixture(autouse=True)
def log(monkeypatch) -> LogSpy:
    spy = LogSpy()
    monkeypatch.setattr(custom_jobs, 'logger', spy)
    return spy


@pytest.fixture(autouse=True)
def fake_update(monkeypatch) -> FakeUpdate:
    fake = FakeUpdate()
    monkeypatch.setattr(fork_updater, 'run_update', fake)
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 17, 4, 31))


@pytest.fixture
def loop():
    """真的事件循环，跑在辅助线程里：后台任务线程要把合并交给它。"""
    event_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=event_loop.run_forever, daemon=True)
    thread.start()
    yield event_loop
    event_loop.call_soon_threadsafe(event_loop.stop)
    thread.join(timeout=5)
    event_loop.close()                                               # 循环没停下的话这里会抛，用例就此失败


@pytest.fixture
def new_jobs(clock, loop):
    """造 CustomJobs（默认接假时钟），用例结束时把它们的线程都停掉。依赖 loop：线程先停，循环后停。"""
    created: list[CustomJobs] = []

    def make(**kwargs) -> CustomJobs:
        kwargs.setdefault('now', clock.wall)
        kwargs.setdefault('monotonic', clock.monotonic)
        jobs = CustomJobs(**kwargs)
        created.append(jobs)
        return jobs

    yield make
    for jobs in created:
        jobs.stop()
    assert wait_until(lambda: not job_threads()), '后台任务线程没有停下来'


# ---- 每日定时合并：一轮检查 ------------------------------------------------------------------------------------

def test_the_daily_merge_runs_once_on_the_server_loop_and_is_remembered(new_jobs, clock, loop, monkeypatch,
                                                                         fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    jobs = new_jobs()
    jobs.tick(loop)
    assert fake_update.calls == 1
    assert fake_update.loops == [loop]                               # 在服务的事件循环上跑，与更新页按钮同一条路径
    assert custom_jobs._load_last_run() == date(2026, 11, 17)
    assert log.mentions(UP_TO_DATE)                                  # run_update 返回的文案进日志
    jobs.tick(loop)
    clock.now = datetime(2026, 11, 17, 23, 0)
    jobs.tick(loop)
    assert fake_update.calls == 1                                    # 同一天不再跑
    clock.now = datetime(2026, 11, 18, 4, 30)
    jobs.tick(loop)
    assert fake_update.calls == 2                                    # 第二天再跑一次
    assert custom_jobs._load_last_run() == date(2026, 11, 18)


def test_nothing_runs_before_the_time(new_jobs, clock, loop, monkeypatch, fake_update):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    clock.now = datetime(2026, 11, 17, 4, 29)
    new_jobs().tick(loop)
    assert fake_update.calls == 0
    assert not state_file().exists()


def test_nothing_runs_and_nothing_is_written_when_the_merge_is_off(new_jobs, loop, fake_update, log):
    new_jobs().tick(loop)                                            # 没设：缺省就是关闭
    assert fake_update.calls == 0
    assert not Path('log').exists()
    assert log.records == []


def test_an_empty_value_means_off_without_a_log(new_jobs, loop, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '')
    new_jobs().tick(loop)
    assert fake_update.calls == 0
    assert log.records == []


def test_a_missed_minute_is_caught_up_once_later_the_same_day(new_jobs, clock, loop, monkeypatch, fake_update):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    custom_jobs._save_last_run(date(2026, 11, 16))                   # 昨天跑过
    clock.now = datetime(2026, 11, 17, 9, 0)                         # 4:30 电脑在休眠，现在才醒
    jobs = new_jobs()
    jobs.tick(loop)
    assert fake_update.calls == 1
    jobs.tick(loop)
    clock.now = datetime(2026, 11, 17, 21, 0)
    jobs.tick(loop)
    assert fake_update.calls == 1                                    # 补跑一次就够，一天只跑一次


def test_a_server_restart_the_same_day_does_not_merge_again(new_jobs, loop, monkeypatch, fake_update):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    new_jobs().tick(loop)
    assert fake_update.calls == 1
    new_jobs().tick(loop)                                            # 另一个实例：服务重启了，靠状态文件记得今天试过
    assert fake_update.calls == 1


def test_the_merge_time_is_read_at_every_check(new_jobs, loop, fake_update):
    jobs = new_jobs()
    jobs.tick(loop)
    assert fake_update.calls == 0
    Path('.env').write_text(f'{MERGE_TIME_KEY}=04:30\n', encoding='utf-8')     # 改 .env 不用重启
    jobs.tick(loop)
    assert fake_update.calls == 1


def test_an_invalid_time_is_off_and_logged_once_per_distinct_value(new_jobs, loop, monkeypatch, fake_update, log):
    jobs = new_jobs()
    monkeypatch.setenv(MERGE_TIME_KEY, '25:99')
    for _ in range(3):
        jobs.tick(loop)
    assert len(log.messages('warning')) == 1 and "'25:99'" in log.messages('warning')[0]
    monkeypatch.setenv(MERGE_TIME_KEY, 'abc')
    jobs.tick(loop)
    jobs.tick(loop)
    assert len(log.messages('warning')) == 2
    monkeypatch.setenv(MERGE_TIME_KEY, '25:99')                      # 见过的值不再记
    jobs.tick(loop)
    assert len(log.messages('warning')) == 2
    assert fake_update.calls == 0 and not Path('log').exists()
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')                      # 改成合法值之后恢复正常
    jobs.tick(loop)
    assert fake_update.calls == 1


# ---- 每日定时合并：无论结果如何，一天只试一次 -------------------------------------------------------------------

def test_a_failing_attempt_still_counts_and_never_raises(new_jobs, loop, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    fake_update.error = RuntimeError('boom')
    jobs = new_jobs()
    jobs.tick(loop)
    jobs.tick(loop)
    assert fake_update.calls == 1
    assert custom_jobs._load_last_run() == date(2026, 11, 17)
    assert 'exception' in log.levels                                 # 带调用栈记下


def test_an_unhappy_outcome_message_still_counts(new_jobs, loop, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    fake_update.message = '合并上游遇到代码冲突，已中止，分支未改动：module/a.py'
    jobs = new_jobs()
    jobs.tick(loop)
    jobs.tick(loop)
    assert fake_update.calls == 1
    assert log.mentions('代码冲突')


def test_a_closed_loop_still_counts_and_leaves_no_orphan_coroutine(new_jobs, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    dead = asyncio.new_event_loop()
    dead.close()
    jobs = new_jobs()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        jobs.tick(dead)
        jobs.tick(dead)
        gc.collect()
    assert not [w for w in caught if 'never awaited' in str(w.message)]
    assert fake_update.calls == 0
    assert custom_jobs._load_last_run() == date(2026, 11, 17)
    assert 'exception' in log.levels


def test_a_merge_that_takes_too_long_is_given_up_on_but_still_counts(new_jobs, loop, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')
    monkeypatch.setattr(custom_jobs, 'MERGE_TIMEOUT', 0.05)
    fake_update.delay = 0.3
    jobs = new_jobs()
    jobs.tick(loop)                                                  # 等了 0.05 秒就放弃，不抛
    assert log.levels.count('error') == 1
    assert custom_jobs._load_last_run() == date(2026, 11, 17)
    assert fake_update.finished.wait(5)                              # 合并本身还在循环上继续，让它做完再收场
    jobs.tick(loop)
    assert fake_update.calls == 1


def test_a_failed_state_write_does_not_cause_repeated_merges(new_jobs, loop, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '04:30')

    def broken(day):
        raise PermissionError('state.json is locked')

    monkeypatch.setattr(custom_jobs, '_save_last_run', broken)
    jobs = new_jobs()
    for _ in range(3):
        jobs.tick(loop)
    assert fake_update.calls == 1                                    # 内存里记着，不会每分钟合并一次
    assert len(log.messages('warning')) == 1


# ---- 注册的周期任务 --------------------------------------------------------------------------------------------

def test_a_due_job_runs_and_then_waits_out_its_interval(new_jobs, clock, loop):
    ran = []
    jobs = new_jobs()
    jobs.register('retry', 120, lambda: ran.append(clock.mono))
    jobs.tick(loop)
    assert ran == [1000.0]                                           # 从没跑过的任务第一轮就跑
    clock.mono += 60
    jobs.tick(loop)
    assert len(ran) == 1                                             # 间隔没到
    clock.mono += 60
    jobs.tick(loop)
    assert ran == [1000.0, 1120.0]


def test_an_interval_equal_to_the_tick_runs_on_every_tick_despite_timer_jitter(new_jobs, clock, loop):
    ran = []
    jobs = new_jobs()
    jobs.register('flush', 60, lambda: ran.append(1))                # M4 的飞书补发就是 60 秒
    jobs.tick(loop)
    for expected in (2, 3):
        clock.mono += 59.98                                          # 计时器早醒了 20 毫秒
        jobs.tick(loop)
        assert len(ran) == expected
    clock.mono += 58                                                 # 早了好几秒就不算到期
    jobs.tick(loop)
    assert len(ran) == 3


def test_a_raising_job_is_logged_and_does_not_stop_the_next_one(new_jobs, clock, loop, log):
    ran = []

    def broken():
        raise RuntimeError('boom')

    jobs = new_jobs()
    jobs.register('broken', 60, broken)
    jobs.register('good', 60, lambda: ran.append(1))
    jobs.tick(loop)
    assert ran == [1]
    assert log.levels == ['exception'] and 'broken' in log.messages('exception')[0]
    clock.mono += 60
    jobs.tick(loop)
    assert ran == [1, 1]                                             # 坏任务下一轮照样再试，别的任务照样跑
    assert log.levels == ['exception', 'exception']


def test_registering_a_name_again_replaces_the_job(new_jobs, loop):
    ran = []
    jobs = new_jobs()
    jobs.register('x', 60, lambda: ran.append('first'))
    jobs.register('x', 60, lambda: ran.append('second'))
    jobs.tick(loop)
    assert ran == ['second']


def test_jobs_run_one_after_another_in_registration_order(new_jobs, loop):
    order = []
    jobs = new_jobs()
    for name in ('a', 'b', 'c'):
        jobs.register(name, 60, lambda name=name: order.append(name))
    jobs.tick(loop)
    assert order == ['a', 'b', 'c']


def test_a_failing_merge_check_does_not_starve_the_jobs(new_jobs, loop, monkeypatch, log):
    def broken_read_env(key, default=None):
        raise RuntimeError('boom')

    monkeypatch.setattr(custom_jobs, 'read_env', broken_read_env)
    ran = []
    jobs = new_jobs()
    jobs.register('flush', 60, lambda: ran.append(1))
    jobs.tick(loop)
    assert ran == [1]
    assert 'exception' in log.levels


# ---- 线程 ------------------------------------------------------------------------------------------------------

def test_the_thread_is_a_named_daemon_and_the_first_check_runs_right_after_start(new_jobs, loop, monkeypatch,
                                                                                  fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '00:00')                      # 00:00 之后都算到点：一启动就补跑
    where = []
    jobs = new_jobs()                                                # 默认 60 秒一轮：第一轮之后线程在 Event 上等
    jobs.register('where', 60, lambda: where.append(threading.current_thread().name))
    jobs.start(loop)
    assert wait_until(lambda: fake_update.calls == 1 and where and log.mentions(UP_TO_DATE))
    assert len(job_threads()) == 1 and job_threads()[0].daemon
    assert where == [custom_jobs.THREAD_NAME]                        # 任务在 custom-jobs 线程里跑
    assert fake_update.loops == [loop]                               # 合并却是在服务循环上跑的
    assert any(custom_jobs.THREAD_NAME in message for message in log.messages('info'))   # 实机核对就看这行日志


def test_start_is_idempotent_while_the_thread_is_alive(new_jobs, loop):
    jobs = new_jobs()
    jobs.start(loop)
    first = job_threads()
    assert len(first) == 1
    jobs.start(loop)
    jobs.start(loop)
    assert job_threads() == first                                    # 还是同一个线程，没有多出来


def test_start_needs_a_running_loop_when_none_is_given(new_jobs):
    jobs = new_jobs()
    with pytest.raises(RuntimeError):
        jobs.start()
    assert job_threads() == []


def test_start_without_a_loop_uses_the_running_one(new_jobs, monkeypatch, fake_update, log):
    monkeypatch.setenv(MERGE_TIME_KEY, '00:00')
    jobs = new_jobs()

    async def scenario():
        jobs.start()                                                 # app.py 的 on_startup 就是这样调的：不传循环
        deadline = time.monotonic() + 5
        while not log.mentions(UP_TO_DATE) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)                                # 让出控制权，后台线程交来的合并才跑得起来
        return asyncio.get_running_loop()

    running = asyncio.run(scenario())
    assert fake_update.calls == 1
    assert fake_update.loops == [running]


def test_stop_wakes_the_thread_at_once_and_a_stopped_instance_can_start_again(new_jobs, loop):
    jobs = new_jobs()                                                # 60 秒一轮：线程睡在 Event 上，不是 time.sleep
    jobs.start(loop)
    assert wait_until(lambda: len(job_threads()) == 1)
    began = time.monotonic()
    jobs.stop()
    assert time.monotonic() - began < 5
    assert job_threads() == []
    jobs.start(loop)
    assert len(job_threads()) == 1


def test_the_thread_survives_a_crashing_check(new_jobs, loop, monkeypatch, log):
    jobs = new_jobs(tick_seconds=0.01)
    calls = []

    def crash(passed_loop):
        calls.append(passed_loop)
        raise RuntimeError('boom')

    monkeypatch.setattr(jobs, 'tick', crash)
    jobs.start(loop)
    assert wait_until(lambda: len(calls) >= 3)                       # 出错之后下一轮照样来
    assert len(job_threads()) == 1
    assert 'exception' in log.levels


def test_a_job_registered_after_the_thread_started_is_picked_up(new_jobs, loop):
    jobs = new_jobs(tick_seconds=0.01)
    jobs.start(loop)
    ran = threading.Event()
    jobs.register('late', 60, ran.set)                               # M4 在服务启动之后才注册也行
    assert ran.wait(5)


def test_the_module_functions_drive_the_default_instance(new_jobs, loop, monkeypatch):
    fresh = new_jobs(tick_seconds=0.01)
    monkeypatch.setattr(custom_jobs, '_default', fresh)
    ran = threading.Event()
    register_job('m4', 60, ran.set)
    start_custom_jobs(loop)
    start_custom_jobs(loop)
    assert ran.wait(5)
    assert len(job_threads()) == 1


# ---- 接线 ------------------------------------------------------------------------------------------------------

def test_custom_routes_re_exports_start_custom_jobs():
    assert custom_routes.start_custom_jobs is custom_jobs.start_custom_jobs


def test_app_py_starts_the_jobs_once_from_on_startup():
    tree = ast.parse((REPO_ROOT / 'module' / 'server' / 'app.py').read_text(encoding='utf-8'))
    imports = [node for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module == 'module.server.custom_routes']
    assert [[alias.name for alias in node.names] for node in imports] == [['register_custom', 'start_custom_jobs']]
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'start_custom_jobs']
    on_startup = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == 'on_startup')
    inside = [node for node in ast.walk(on_startup) if node in calls]
    assert len(calls) == 1 and len(inside) == 1                      # 只有 on_startup 里调了一次
    assert calls[0].args == [] and calls[0].keywords == []           # 不传循环：on_startup 里就是正在运行的那个
