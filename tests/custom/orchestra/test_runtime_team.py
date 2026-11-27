"""编排运行时等搭档的测试：module.orchestra.runtime 在选任务时接 module.orchestra.team 的会合判断。

替身、假时钟和自动生效的夹具 isolated 都取自 test_runtime：每个用例先 chdir 到 tmp_path，进度文件都写在
tmp_path/log/orchestra 下，用例结束时收回心跳线程。本实例名是 t，搭档默认是 u，搭档的进度文件由用例按需写好。
表是一行八岐大蛇，FakeConfig 默认的八岐大蛇身份是队长，所以这一行是第 1 个组队行。
文件末尾的用例用 runtime_for 建 t、u 两个真的运行时，共用一个假时钟，互读对方真写下的进度文件。
"""
import logging
import random
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.config.anti_ban import AntiBanGuard
from module.orchestra import runtime as runtime_module
from module.orchestra.model import OrchestraRow
from module.orchestra.progress import Progress, load_progress, progress_path, save_progress
from tasks.Script.config_optimization import WhenTaskQueueEmpty
# isolated 是自动生效的夹具，导入进来才对本文件的用例生效
from test_runtime import T0, Clock, FakeConfig, fn, isolated, make, saved  # noqa: F401


class FakeNotifier:
    def __init__(self, error: Exception | None = None):
        self.pushes, self.error = [], error
    def push(self, title: str, content: str) -> bool:
        if self.error: raise self.error
        self.pushes.append((title, content)); return True

class FakeDevice:
    clears = click_clears = 0
    def stuck_record_clear(self): self.clears += 1
    def click_record_clear(self): self.click_clears += 1

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


# ---------- 审查后的修正（第 1 轮） ----------

def partner_playing():
    """搭档已按会合开打第 1 行：在跑，心跳新鲜，会合记在 T0。这边按规则 b 只认不早于自己开始等的会合。"""
    partner_file(phase='row_running', row_task='Orochi', team_row_ordinal=1, heartbeat_at=T0, team_met_at=T0,
                 team_met_ordinal=1, team_met_task='Orochi', team_partner='t')


@pytest.mark.parametrize('between', [None, 'Restart'])
def test_a_meeting_is_kept_until_the_row_really_starts(between, caplog):
    waiting_for_t()
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    assert runtime.pick().next_run == T0                          # 会合
    partner_playing()                                             # 搭档随即开打，这边的行却没起来
    if between is not None:
        runtime.before_run(between)                               # 桌面客户端没登录：上游把任务改写成了 Restart
    clock.advance(minutes=3)
    assert runtime.pick().next_run == clock.now                   # 不重新等，接着开打
    clock.advance(seconds=30)
    assert runtime.pick().next_run == clock.now
    assert saved().team_wait_since is None                        # 没有新的等待记录
    lines = [m for m in oas_messages(caplog, logging.INFO) if m.startswith('编排：')]
    assert not [m for m in lines if '等搭档 u（' in m]               # 没有"开始等"那一行
    assert lines.count('编排：已与搭档会合，第 1 行 Orochi 还没开打，接着开打') == 1   # 同一次会合只记一行


def test_the_meeting_is_used_up_once_the_row_starts():
    waiting_for_t()
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    runtime.pick()                                                # 会合
    runtime.before_run('Orochi')                                  # 行真的起来了
    clock.advance(minutes=1)
    runtime.after_run('Orochi', False)                            # 没打完，中断在这一行
    partner_file(phase='interrupted', row_task='Orochi', team_row_ordinal=1, team_partner='t')   # 搭档也停在这一行
    assert runtime.pick().next_run == clock.now + timedelta(seconds=31)   # 续跑照常先看搭档：等它


def test_a_new_process_starts_without_the_meeting():
    waiting_for_t()
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    runtime.pick()                                                # 会合，行没起来，进程随后退出
    partner_playing()
    restarted, _, _, clock = make([OrchestraRow(task='Orochi')], partner='u')   # 同一份进度，新进程
    clock.advance(minutes=3)
    assert restarted.pick().next_run == clock.now + timedelta(seconds=31)   # 会合只记在内存里：照常等


def test_a_pause_that_fails_in_any_way_still_notifies_and_exits(caplog):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    config.notifier = notifier = FakeNotifier()
    runtime.pick()
    clock.advance(minutes=30)

    def broken():
        raise RuntimeError('配置写坏了')
    config.save = broken
    with pytest.raises(SystemExit, match='等搭档 u 超时，已暂停'):
        runtime.pick()
    assert len(notifier.pushes) == 1 and saved().status_note == '等搭档 u 超时，已暂停'
    errors = [r for r in caplog.records if r.name == 'oas' and r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == ["编排：等搭档超时，暂停没能存进配置：RuntimeError('配置写坏了')"]
    assert errors[0].exc_info is None


# ---------- 终审后的修正 ----------

def test_a_timeout_marks_the_progress_paused():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    config.notifier = FakeNotifier()
    runtime.pick()
    clock.advance(minutes=30)
    with pytest.raises(SystemExit):
        runtime.pick()
    assert saved().paused is True                                 # 搭档读到的是已暂停，不是还没到
    resumed, _, _, clock = make([OrchestraRow(task='Orochi')], partner='u')   # 点继续：state 写回 running，新进程
    clock.advance(minutes=31)
    assert resumed.pick().next_run == clock.now + timedelta(seconds=31)       # 继续照常处理暂停中的进度：重新等
    assert (saved().paused, saved().team_wait_since) == (False, clock.now)


IDLE_WARNING = '编排：任务队列为空时的设置会让每次等搭档的轮询都关掉游戏或模拟器，建议改成回庭院'
IDLE_NOTE = '；任务队列为空时的设置会让每次轮询都关掉游戏或模拟器，建议改成回庭院'


@pytest.mark.parametrize('method, close_game, close_emulator, closes', [
    (WhenTaskQueueEmpty.CLOSE_GAME, dtime(0, 0), dtime(0, 0), True),        # 关游戏、等待时长 0：每次空等都关游戏
    (WhenTaskQueueEmpty.GOTO_MAIN, dtime(0, 0), dtime(0, 0, 10), True),     # 关模拟器的等待时长短于一次占位
    (WhenTaskQueueEmpty.GOTO_MAIN, dtime(0, 0), dtime(0, 0), False),        # 用户的设置：回庭院，不关模拟器
    (WhenTaskQueueEmpty.CLOSE_GAME, dtime(0, 0, 30), dtime(0, 0), True),    # 占位 31 秒后到点：等 30 秒就关游戏
    (WhenTaskQueueEmpty.CLOSE_GAME, dtime(0, 0, 31), dtime(0, 0), False),   # 不短于占位：上游不关
    (WhenTaskQueueEmpty.CLOSE_GAME, dtime(0, 10), dtime(0, 0, 10), True),   # 关游戏等得够久，关模拟器的先到
    (WhenTaskQueueEmpty.GOTO_MAIN, dtime(0, 0), dtime(0, 0, 31), False),
])
def test_idle_settings_that_close_the_game_warn_once_and_tell_the_panel(caplog, method, close_game,
                                                                         close_emulator, closes):
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    optimization = config.model.script.optimization
    optimization.when_task_queue_empty = method
    optimization.close_game_wait_duration, optimization.close_emulator_wait_duration = close_game, close_emulator
    runtime.pick()
    clock.advance(seconds=30)
    runtime.pick()                                                # 两次轮询
    assert oas_messages(caplog, logging.WARNING) == ([IDLE_WARNING] if closes else [])   # 每个进程只记一次
    assert saved().status_note == '等搭档 u：已等 0 分钟，最多 30 分钟；读不到搭档的进度' + (IDLE_NOTE if closes else '')


def test_each_poll_also_clears_the_click_record():
    runtime, script, config, clock = make([OrchestraRow(task='Orochi')], partner='u')
    script.device = device = FakeDevice()
    for _ in range(3):
        runtime.pick()
        clock.advance(seconds=30)
    assert (device.clears, device.click_clears) == (3, 3)        # 同上游每个任务开始前：卡死计时、点击记录都复位


# 两个真的运行时：t、u 互设搭档，共用一个假时钟，各写各的进度文件，读的是对方真写下的

def runtime_for(name: str, partner: str, clock: Clock, progress: Progress | None = None):
    """实例 name 的运行时，搭档是 partner，时钟由用例传入。同 make，只是实例名可选（make 固定为 t）；
    表是一行八岐大蛇，通知用 FakeNotifier。progress 预先写进 name 的进度文件。返回 (运行时, 配置替身)。"""
    config = FakeConfig()
    config.config_name = name
    orchestra = config.model.orchestra   # 嵌套赋值不会触发 ConfigModel 的自动保存
    orchestra.rows, orchestra.state, orchestra.partner = [OrchestraRow(task='Orochi')], 'running', partner
    config.notifier = FakeNotifier()
    script = SimpleNamespace(config=config, config_name=name, anti_ban_guard=AntiBanGuard(), running_task_obj=None)
    if progress is not None:
        save_progress(progress_path(name), progress)
    return runtime_module.OrchestraRuntime(script, clock=clock, rng=random.Random(0)), config


def test_two_runtimes_meet_and_record_the_same_meeting():
    clock = Clock()
    t, _ = runtime_for('t', 'u', clock)
    u, _ = runtime_for('u', 't', clock)
    for _ in range(6):                                            # t 从 T0 起每 30 秒看一次，u 还没到
        assert t.pick().next_run == clock.now + timedelta(seconds=31)
        clock.advance(seconds=30)
    arrived = u.pick()                                            # T0 + 3 分钟 u 到了：t 正在等，按规则 a 直接开打
    assert (arrived.command, arrived.next_run) == ('Orochi', clock.now)
    clock.advance(seconds=30)
    follows = t.pick()                                            # t 的下一次轮询：u 已先走，按规则 b 开打
    assert (follows.command, follows.next_run) == ('Orochi', clock.now)
    met = T0 + timedelta(minutes=3)
    assert load_progress(progress_path('t')).team_met_at == load_progress(progress_path('u')).team_met_at == met


def test_two_runtimes_resuming_the_same_row_go_together():
    clock = Clock()
    t, _ = runtime_for('t', 'u', clock, progress=INTERRUPTED)    # 两边都中断在第 1 个组队行
    u, _ = runtime_for('u', 't', clock, progress=INTERRUPTED)
    assert t.pick().next_run == clock.now + timedelta(seconds=31)   # u 也停在这一行：等它
    assert load_progress(progress_path('t')).phase == 'interrupted'  # 续跑时的等待，阶段留在 interrupted
    clock.advance(seconds=10)
    assert u.pick().next_run == clock.now                         # t 正在等这一行：按规则 a 直接续跑
    clock.advance(seconds=20)
    assert t.pick().next_run == clock.now                         # t 的下一次轮询：u 已先走，按规则 b 续跑


def test_after_a_timeout_the_partner_sees_it_paused():
    clock = Clock()
    t, _ = runtime_for('t', 'u', clock)
    u, u_config = runtime_for('u', 't', clock)
    t.pick()
    clock.advance(minutes=30)
    with pytest.raises(SystemExit):                               # t 一个人等满 30 分钟，暂停并退出
        t.pick()
    clock.advance(minutes=1)
    assert u.pick().next_run == clock.now + timedelta(seconds=31)   # u 这才到：t 已经不等了
    assert load_progress(progress_path('u')).status_note.endswith('；搭档已暂停')
    clock.advance(minutes=30)
    with pytest.raises(SystemExit):
        u.pick()
    assert u_config.notifier.pushes[0][1].split('\n')[1] == '搭档已暂停'   # u 自己的超时通知，第二行同样写已暂停
