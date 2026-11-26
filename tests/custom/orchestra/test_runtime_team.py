"""编排运行时等搭档的测试：module.orchestra.runtime 在选任务时接 module.orchestra.team 的会合判断。

替身、假时钟和自动生效的夹具 isolated 都取自 test_runtime：每个用例先 chdir 到 tmp_path，进度文件都写在
tmp_path/log/orchestra 下，用例结束时收回心跳线程。本实例名是 t，搭档默认是 u，搭档的进度文件由用例按需写好。
表是一行八岐大蛇，FakeConfig 默认的八岐大蛇身份是队长，所以这一行是第 1 个组队行。
"""
import logging
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pytest

from module.orchestra import runtime as runtime_module
from module.orchestra.model import OrchestraRow
from module.orchestra.progress import Progress, progress_path, save_progress
# isolated 是自动生效的夹具，导入进来才对本文件的用例生效
from test_runtime import T0, fn, isolated, make, saved  # noqa: F401


class FakeNotifier:
    def __init__(self, error: Exception | None = None):
        self.pushes, self.error = [], error
    def push(self, title: str, content: str) -> bool:
        if self.error: raise self.error
        self.pushes.append((title, content)); return True

class FakeDevice:
    clears = 0
    def stuck_record_clear(self): self.clears += 1

def partner_file(name='u', **fields): save_progress(progress_path(name), Progress(**fields))
def waiting_for_t(name='u', at=T0, **kw):
    partner_file(name, **{**dict(phase='ready', team_wait_since=at, team_wait_ordinal=1, team_wait_task='Orochi',
                                  team_alive_at=at, team_partner='t'), **kw})

def test_no_partner_behaves_as_before():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')])
    assert runtime.pick().next_run == T0 and saved().team_wait_since is None

def test_partner_equal_to_self_is_ignored(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='t')
    assert runtime.pick().next_run == T0
    runtime.pick()
    assert sum('搭档设成了自己' in r.getMessage() for r in caplog.records) == 1

def test_first_arrival_waits_polls_and_resets_stuck_timer():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    script.device = device = FakeDevice()                      # 进 script.__dict__，同 cached_property 建好之后
    assert runtime.pick().next_run == T0 + timedelta(seconds=31)   # POLL 加 1 秒占位余量
    p = saved()
    assert (p.phase, p.team_wait_since, p.team_wait_ordinal, p.team_wait_task, p.team_partner) == \
        ('ready', T0, 1, 'Orochi', 'u')
    assert p.status_note == '等搭档 u：已等 0 分钟，最多 30 分钟；读不到搭档的进度'
    assert device.clears == 1

def test_no_device_is_created_for_the_reset():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    runtime.pick()
    assert 'device' not in vars(script)

def test_meets_a_waiting_partner():
    waiting_for_t()
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    assert runtime.pick().next_run == T0
    assert (saved().team_met_at, saved().team_wait_since) == (T0, None)

def test_goes_when_partner_already_met_me():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    runtime.pick()
    partner_file(team_met_at=T0 + timedelta(seconds=10), team_met_ordinal=1, team_met_task='Orochi', team_partner='t')
    clock.advance(seconds=30)
    assert runtime.pick().next_run == clock.now

def test_unreadable_partner_progress_keeps_waiting(monkeypatch):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    runtime.pick()
    real = runtime_module.load_progress
    def locked(path):
        if path == progress_path('u'): raise PermissionError('占用中')
        return real(path)
    monkeypatch.setattr(runtime_module, 'load_progress', locked)
    waiting_for_t()                                               # 文件在，但读不了
    clock.advance(seconds=30)
    assert runtime.pick().next_run == clock.now + timedelta(seconds=31)
    assert saved().status_note.endswith('；读不到搭档的进度')

def test_unicode_partner_name():
    waiting_for_t('山川知异路')
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='山川知异路')
    assert runtime.pick().next_run == T0

def test_timeout_pauses_notifies_and_exits():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    config.notifier = notifier = FakeNotifier()
    runtime.pick()
    clock.advance(minutes=30)
    with pytest.raises(SystemExit, match='等搭档 u 超时，已暂停'):
        runtime.pick()
    assert config.model.orchestra.state == 'paused' and config.saves >= 1
    assert notifier.pushes == [('等搭档超时', '等搭档 u 超过 30 分钟，已暂停在第 1 行（御魂·八岐大蛇）\n'
                                             '读不到搭档的进度\n'
                                             '处理好之后，在编排页点继续接着等；要两边都从头来，先把两个号都点停止，再都点开始')]
    assert (saved().status_note, saved().team_wait_since) == ('等搭档 u 超时，已暂停', None)

def test_timeout_rescued_by_the_last_read():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    config.notifier = FakeNotifier()
    runtime.pick()
    clock.advance(minutes=30)
    reads = iter([None, Progress(team_met_at=T0 + timedelta(minutes=29), team_met_ordinal=1, team_met_task='Orochi')])
    runtime._read_partner = lambda name: next(reads)              # 超时那一刻读不到，再读一次时搭档已按 a 先走
    assert runtime.pick().next_run == clock.now
    assert config.model.orchestra.state == 'running'

def test_notify_failure_still_pauses_and_exits(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    config.notifier = FakeNotifier(error=RuntimeError('飞书挂了'))
    runtime.pick()
    clock.advance(minutes=30)
    with pytest.raises(SystemExit):
        runtime.pick()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and '等搭档超时的通知没发出' in r.getMessage()]
    assert len(errors) == 1 and errors[0].exc_info is None

def test_cut_in_withdraws_alive_but_keeps_the_clock():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u', cut_in_tasks=['MetaDemon'])
    runtime.pick()
    clock.advance(seconds=30)
    config.pending_task = [fn('meta_demon', clock.now)]
    assert runtime.pick() is config.pending_task[0]
    assert (saved().team_alive_at, saved().team_wait_since) == (None, T0)

def test_startup_clears_a_stale_wait_record():
    stale = Progress(phase='ready', sequence_started_at=T0, team_wait_since=T0 - timedelta(hours=1),
                     team_wait_ordinal=1, team_wait_task='Orochi', team_alive_at=T0 - timedelta(hours=1))
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u', progress=stale)
    assert runtime.pick().next_run == T0 + timedelta(seconds=31)  # 重新计时，没有立刻超时
    assert saved().team_wait_since == T0

def test_resume_here_goes_immediately():
    partner_file(phase='row_running', row_task='Orochi', team_row_ordinal=1, heartbeat_at=T0, team_partner='t')
    interrupted = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0,
                           row_started_at=T0, team_row_ordinal=1)
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u', progress=interrupted)
    assert runtime.pick().next_run == T0

def test_resume_left_books_row_and_moves_on():
    partner_file(phase='resting', team_partner='t')
    interrupted = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0,
                           row_started_at=T0, done_count=5, team_row_ordinal=1)
    runtime, script, config, clock = make([OrchestraRow(task='Orochi', rest_minutes=2), OrchestraRow(task='RealmRaid')],
                                          partner='u', progress=interrupted)
    assert runtime.pick().next_run == T0 + timedelta(minutes=2, seconds=1)   # 进了第 1 行之后的休息
    assert (saved().phase, saved().history[-1]['task'], saved().history[-1]['done_count']) == ('resting', 'Orochi', 5)

def test_resume_stopped_waits_keeping_interrupted():
    partner_file(phase='interrupted', row_task='Orochi', team_row_ordinal=1, team_partner='t')
    interrupted = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0,
                           row_started_at=T0, team_row_ordinal=1)
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u', progress=interrupted)
    assert runtime.pick().next_run == T0 + timedelta(seconds=31)
    assert saved().phase == 'interrupted'

@pytest.mark.parametrize('task, ordinal', [('Orochi', 1), ('RealmRaid', None)])
def test_team_row_ordinal_recorded_on_row_start(task, ordinal):
    runtime, script, config, clock = make([OrchestraRow(task=task)], interval=3600)   # 没设搭档也记
    runtime.pick()
    runtime.before_run(task)
    assert saved().team_row_ordinal == ordinal
    runtime.after_run(task, False)                                # 收尾，停掉心跳

def test_wait_logging_is_throttled(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    for _ in range(11):                                           # 5 分钟内轮询 11 次
        runtime.pick(); clock.advance(seconds=30)
    lines = [r for r in caplog.records if r.levelno == logging.INFO and '等搭档 u' in r.getMessage()]
    assert len(lines) == 2                                        # 开始等一行，满 5 分钟一行


# ---------- 简报之外的补充 ----------

INTERRUPTED = Progress(phase='interrupted', row_index=0, row_task='Orochi', sequence_started_at=T0,
                       row_started_at=T0, team_row_ordinal=1)   # 第 1 个组队行打到一半被打断


def oas_messages(caplog, level: int) -> list[str]:
    """本项目日志（logger oas）里恰好是这一级别的消息，按记录顺序。"""
    return [r.getMessage() for r in caplog.records if r.name == 'oas' and r.levelno == level]


def test_isolated_is_in_effect(tmp_path):
    assert Path.cwd() == tmp_path   # 夹具从 test_runtime 导入后照样自动生效：进度文件不会写进仓库自己的 log/


def test_a_partner_on_an_idle_instance_writes_nothing():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], state='idle', partner='u')
    config.pending_task = [fn('orochi', T0)]
    assert runtime.pick() is config.pending_task[0]
    assert not progress_path('t').exists() and config.saves == 0   # 编排没在进行：搭档也不写进进度


def test_corrupted_partner_progress_counts_as_unreadable():
    progress_path('u').parent.mkdir(parents=True, exist_ok=True)
    progress_path('u').write_text('{坏了', encoding='utf-8')        # load_progress 读成默认进度
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    assert runtime.pick().next_run == T0 + timedelta(seconds=31)
    assert saved().status_note.endswith('；读不到搭档的进度')


def test_the_anti_ban_note_comes_first_while_waiting():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    anti_ban = config.model.script.anti_ban
    anti_ban.enable, anti_ban.sleep_start, anti_ban.sleep_end = True, dtime(2, 0), dtime(8, 0)
    clock.now = datetime(2026, 11, 17, 3, 0)
    runtime.pick()
    assert saved().status_note == '防封禁休眠中，08:00 继续；等搭档 u：已等 0 分钟，最多 30 分钟；读不到搭档的进度'


def test_a_failing_stuck_reset_only_warns(caplog):
    class BrokenDevice:
        def stuck_record_clear(self):
            raise RuntimeError('设备断开')
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    script.device = BrokenDevice()
    assert runtime.pick().next_run == T0 + timedelta(seconds=31)   # 照常接着等
    assert oas_messages(caplog, logging.WARNING) == ["编排：复位卡死计时出错：RuntimeError('设备断开')"]


def test_a_skip_into_the_next_team_row_waits_there():
    partner_file(phase='resting', team_partner='t')
    rows = [OrchestraRow(task='Orochi', rest_minutes=0), OrchestraRow(task='Orochi')]
    runtime, script, config, clock = make(rows, partner='u', progress=INTERRUPTED)
    assert runtime.pick().next_run == T0 + timedelta(seconds=31)   # 第 1 行记账、不休息，第 2 行不是续跑：从头等
    p = saved()
    assert (p.row_index, p.phase, p.team_wait_ordinal, p.history[-1]['task']) == (1, 'ready', 2, 'Orochi')
    assert p.status_note == '等搭档 u：已等 0 分钟，最多 30 分钟；搭档在休息'


def test_a_skip_that_ends_the_sequence_sets_state_back_to_idle():
    partner_file(phase='resting', team_partner='t')
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u', progress=INTERRUPTED)
    config.waiting_task = [fn('restart', T0 + timedelta(hours=1))]
    assert runtime.pick().command == 'Restart'   # 唯一的一行记了账，序列跑完，交还上游
    assert saved().phase == 'done' and config.model.orchestra.state == 'idle' and config.saves == 1


@pytest.mark.parametrize('partner, line', [
    (dict(phase='row_running', row_task='Orochi', team_row_ordinal=1, heartbeat_at=T0),
     '编排：搭档还在第 1 个组队行，第 1 行直接续跑'),
    (dict(phase='resting'), '编排：搭档已离开第 1 个组队行，第 1 行按已打完记账'),
])
def test_resuming_logs_what_it_did(partner, line, caplog):
    partner_file(team_partner='t', **partner)
    runtime, script, config, clock = make([OrchestraRow(task='Orochi'), OrchestraRow(task='RealmRaid')],
                                          partner='u', progress=INTERRUPTED)
    runtime.pick()
    assert line in oas_messages(caplog, logging.INFO)


def test_waiting_logs_use_the_exact_copy_and_skip_the_per_pick_line(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    runtime.pick()                       # 开始等
    clock.advance(seconds=30)
    runtime.pick()                       # 接着等：不像休息那样每次选任务都记一行
    clock.advance(minutes=5)
    runtime.pick()                       # 距上一行满 5 分钟
    waiting_for_t(at=clock.now)
    clock.advance(seconds=30)
    runtime.pick()                       # 会合
    assert [m for m in oas_messages(caplog, logging.INFO) if m.startswith('编排：')] == [
        '编排：第 1 行 Orochi 等搭档 u（第 1 个组队行，最多 30 分钟）',
        '编排：等搭档 u 已 5 分钟；读不到搭档的进度',
        '编排：与搭档 u 会合，第 1 行 Orochi 开打',
        '编排：选中第 1 行 Orochi',
    ]


def test_self_partner_warning_names_the_instance(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='t')
    runtime.pick()
    assert oas_messages(caplog, logging.WARNING) == ['编排：搭档设成了自己（t），不等搭档']


def test_stopping_goes_on_past_a_failed_pause_and_a_failed_notice(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    config.notifier = FakeNotifier(error=RuntimeError('飞书挂了'))
    runtime.pick()
    clock.advance(minutes=30)

    def full():
        raise OSError('disk full')
    config.save = full
    with pytest.raises(SystemExit, match='等搭档 u 超时，已暂停'):
        runtime.pick()
    assert config.model.orchestra.state == 'paused'   # 内存里已暂停，存不存得下来另说
    assert saved().status_note == '等搭档 u 超时，已暂停'
    assert oas_messages(caplog, logging.ERROR) == ["编排：等搭档超时，暂停没能存进配置：OSError('disk full')",
                                                   "编排：等搭档超时的通知没发出：RuntimeError('飞书挂了')"]
    assert oas_messages(caplog, logging.WARNING) == ['编排：等搭档 u 超过 30 分钟，暂停编排并退出实例']


def test_a_hand_edited_zero_wait_stops_on_the_first_poll():
    partner_file(phase='resting', team_partner='t', team_met_at=T0 - timedelta(hours=1), team_met_ordinal=1,
                 team_met_task='Orochi')   # 上一遍循环的会合：救不回来，但超时后要拿它和开始等的时间比
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u', partner_wait_minutes=0)
    config.notifier = notifier = FakeNotifier()
    with pytest.raises(SystemExit):
        runtime.pick()
    assert len(notifier.pushes) == 1
