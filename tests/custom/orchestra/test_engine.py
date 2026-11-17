import copy
import json
import random
from datetime import datetime, timedelta

import pytest

from module.orchestra.engine import (Decision, TaskRef, decide, next_row_index, on_row_end, on_row_start, recover,
                                     row_limits, sync_controls)
from module.orchestra.model import Orchestra, OrchestraRow
from module.orchestra.progress import HISTORY_LIMIT, Progress

T0 = datetime(2026, 11, 17, 10, 0)

SIX = [OrchestraRow(task='Orochi', sub1='悲鸣', max_count=30, max_minutes=20, rest_minutes=5)] * 2 \
    + [OrchestraRow(task='GoryouRealm', max_count=50, max_minutes=15, rest_minutes=10)] * 3 \
    + [OrchestraRow(task='Orochi', sub1='悲鸣', max_minutes=30)]


def at(minutes: float) -> datetime:
    """T0 之后的第 minutes 分钟。"""
    return T0 + timedelta(minutes=minutes)


def running(rows, **kw) -> Orchestra:
    return Orchestra(rows=rows, state='running', **kw)


def started(s: Orchestra, now: datetime = T0) -> Progress:
    """刚开始的序列：阶段 ready，指向第 1 行。"""
    return sync_controls(s, Progress(), now)


def run_row(p, s, minutes, completed=True, start=T0, done_count=None, rng=None):
    """依次调用 on_row_start 与 on_row_end，返回新进度。行号取进度里的当前行。"""
    p = on_row_start(p, p.row_index, start, {})
    return on_row_end(p, s, start + timedelta(minutes=minutes), completed, done_count, rng or random.Random(0))


# ---------- 计划书规定的 16 个用例 ----------

def test_six_rows_run_in_order_then_stop():
    s, p, now, seen = Orchestra(rows=SIX, state='running'), Progress(), T0, []
    p = sync_controls(s, p, now)
    while True:
        d = decide(s, p, [], [], now)
        if d.kind == 'wait':
            now = d.at; continue
        if d.kind == 'passthrough':
            break
        seen.append(d.command)
        p = on_row_start(p, d.row_index, now, {})
        now += timedelta(minutes=1)
        p = on_row_end(p, s, now, True, None, random.Random(0))
    assert seen == ['Orochi', 'Orochi', 'GoryouRealm', 'GoryouRealm', 'GoryouRealm', 'Orochi'] and p.phase == 'done'


def test_loop_mode_wraps_and_counts():
    s = running(SIX, after_finish='loop')
    p = started(s)
    for _ in SIX[:-1]:
        p = run_row(p, s, 1)
    assert (p.row_index, p.loop_count) == (5, 0)   # 还没到第 6 行结束，不回绕
    p = run_row(p, s, 1)
    assert p.row_index == 0 and p.loop_count == 1


def test_total_cap_finishes_after_current_row():
    s = running(SIX, total_minutes_cap=2)
    p = run_row(started(s), s, 1)
    assert p.phase == 'resting'   # 才 1 分钟，没到上限
    p = run_row(p, s, 1, start=at(10))
    assert p.phase == 'done'


def test_restart_preempts_row():
    s = running(SIX)
    assert decide(s, started(s), [TaskRef('Restart', T0)], [], T0) == Decision('native', 'Restart')


def test_cut_in_pending_runs_natively():
    s = running(SIX)
    assert decide(s, started(s), [TaskRef('MetaDemon', T0)], [], T0) == Decision('native', 'MetaDemon')


def test_non_cut_in_pending_is_held():
    s = running(SIX)
    assert decide(s, started(s), [TaskRef('Nian', T0)], [], T0) == Decision('row', 'Orochi', row_index=0)


def test_rest_wait_targets_earlier_cut_in():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    assert decide(s, p, [], [TaskRef('MetaDemon', at(20))], T0) == Decision('wait', 'MetaDemon', at=at(20))


def test_rest_jitter_within_20_percent():
    s = running(SIX, rest_jitter=True)
    p = run_row(started(s), s, 1, rng=random.Random(1))
    seconds = (p.rest_until - at(1)).total_seconds()
    assert 240 <= seconds <= 360
    assert seconds != 300   # 区间包含 300，不排除它就验不出随机有没有生效


def test_rest_zero_goes_ready():
    s = running([OrchestraRow(task='Orochi', rest_minutes=0), OrchestraRow(task='Orochi')])
    p = run_row(started(s), s, 1)
    assert p.phase == 'ready' and p.rest_until is None and p.row_index == 1


def test_remaining_limits_after_interrupt():
    p = Progress(phase='interrupted', row_elapsed_before=720, done_count=18)
    assert row_limits(SIX[0], p, 'remaining') == (12, 8)


def test_rerun_policy_returns_original():
    p = Progress(phase='interrupted', row_elapsed_before=720, done_count=18)
    assert row_limits(SIX[0], p, 'rerun') == (30, 20)


def test_recover_uses_heartbeat_not_now():
    p = Progress(phase='row_running', row_started_at=T0, heartbeat_at=at(7))
    out = recover(p, at(300))   # 15:00，停机的几个小时不能算进已用时间
    assert out.row_elapsed_before == 420 and out.phase == 'interrupted'


def test_pause_keeps_rest_and_resume_restores():
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    p = sync_controls(Orchestra(rows=SIX, state='paused'), p, at(20))
    assert p.paused and p.rest_remaining == 600
    p = sync_controls(Orchestra(rows=SIX, state='running'), p, at(60))
    assert not p.paused and p.rest_until == at(70)


def test_skip_rest_command_ends_rest():
    s = running(SIX, command='skip_rest', command_seq=1)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    assert sync_controls(s, p, at(10)).rest_until == at(10)


def test_rows_shrunk_while_running_finishes():
    s = running(SIX[:3])
    assert decide(s, Progress(phase='ready', row_index=5), [], [], T0) == Decision('passthrough')


def test_invalid_row_is_skipped():
    s = running([OrchestraRow(task='Nian'), OrchestraRow(task='Orochi')])
    assert decide(s, Progress(phase='ready'), [], [], T0) == Decision('row', 'Orochi', row_index=1)


# ---------- 控制员裁定的补充用例 ----------

# 裁定 3：无效的行跳过

def test_next_row_index_skips_invalid_rows():
    rows = [OrchestraRow(task='Nian'), OrchestraRow(task='Orochi'), OrchestraRow(task='Nian'), OrchestraRow(task='Orochi')]
    assert [next_row_index(rows, i) for i in range(5)] == [1, 1, 3, 3, None]
    assert next_row_index(rows, -3) == 1
    assert next_row_index([], 0) is None


def test_row_end_skips_invalid_rows_when_advancing():
    s = running([OrchestraRow(task='Orochi'), OrchestraRow(task='Nian'), OrchestraRow(task='GoryouRealm')])
    assert run_row(started(s), s, 1).row_index == 2


def test_no_runnable_row_left_ends_or_wraps_by_after_finish():
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='Nian')]
    stop, loop = running(rows), running(rows, after_finish='loop')
    assert run_row(started(stop), stop, 1).phase == 'done'
    wrapped = run_row(started(loop), loop, 1)
    assert (wrapped.phase, wrapped.row_index, wrapped.loop_count) == ('resting', 0, 1)


@pytest.mark.parametrize('rows', [[], [OrchestraRow(task='Nian')]], ids=['empty', 'all-invalid'])
def test_start_without_runnable_row_finishes_immediately(rows):
    assert started(running(rows)).phase == 'done'


# 裁定 2：未知取值的回退

def test_unknown_state_behaves_as_idle():
    s = Orchestra(rows=SIX, state='weird')
    assert sync_controls(s, Progress(phase='resting', rest_until=at(30)), T0).phase == 'idle'
    assert sync_controls(s, Progress(), T0).phase == 'idle'   # 也不会被当成开始
    assert decide(s, Progress(phase='ready'), [TaskRef('Restart', T0)], [], T0) == Decision('passthrough')


def test_unknown_after_finish_behaves_as_stop():
    s = running([OrchestraRow(task='Orochi')], after_finish='forever')
    p = run_row(started(s), s, 1)
    assert p.phase == 'done' and p.loop_count == 0


def test_unknown_resume_policy_behaves_as_remaining():
    s = running(SIX, resume_policy='whatever')
    p = Progress(phase='interrupted', row_elapsed_before=720, done_count=18)
    assert row_limits(SIX[0], p, 'whatever') == (12, 8)
    p = run_row(Progress(phase='ready'), s, 1, completed=False, done_count=18)
    assert p.row_elapsed_before == 60 and p.done_count == 18   # 没有按 rerun 清零


# decide：顺序与等待目标

@pytest.mark.parametrize('s, p', [
    (Orchestra(rows=SIX, state='idle'), Progress(phase='ready')),
    (Orchestra(rows=SIX, state='paused'), Progress(phase='ready')),
    (running(SIX), Progress(phase='ready', paused=True)),
    (running(SIX), Progress(phase='idle')),
    (running(SIX), Progress(phase='done')),
    (running([]), Progress(phase='ready')),
], ids=['state-idle', 'state-paused', 'progress-paused', 'phase-idle', 'phase-done', 'empty-table'])
def test_decide_passes_through_unless_sequence_is_active(s, p):
    pending = [TaskRef('Restart', T0), TaskRef('MetaDemon', T0)]   # 编排没在跑时，连 Restart 也交给上游
    assert decide(s, p, pending, [], T0) == Decision('passthrough')


def test_decide_priority_restart_then_first_cut_in_then_rest():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    pending = [TaskRef('Nian', T0), TaskRef('ActivityShikigami', T0), TaskRef('MetaDemon', T0)]
    assert decide(s, p, pending, [], T0) == Decision('native', 'ActivityShikigami')   # 按待办顺序的第一个插队任务
    assert decide(s, p, pending + [TaskRef('Restart', T0)], [], T0) == Decision('native', 'Restart')
    assert decide(s, p, [TaskRef('Nian', T0)], [], T0) == Decision('wait', None, at=at(30))   # 休息中，非插队任务不放行


def test_decide_runs_next_row_once_rest_is_over():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    assert decide(s, p, [], [], at(30)) == Decision('row', 'Orochi', row_index=1)
    assert decide(s, p, [], [], at(31)) == Decision('row', 'Orochi', row_index=1)
    # 进度文件被手改、休息截止缺失：不等待，直接跑下一行
    assert decide(s, Progress(phase='resting', row_index=1), [], [], T0) == Decision('row', 'Orochi', row_index=1)


def test_wait_target_picks_the_earliest_candidate():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    waiting = [TaskRef('MetaDemon', at(20)), TaskRef('ActivityShikigami', at(15)), TaskRef('Restart', at(25))]
    assert decide(s, p, [], waiting, T0) == Decision('wait', 'ActivityShikigami', at=at(15))


def test_wait_target_honours_restart_in_waiting():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    assert decide(s, p, [], [TaskRef('Restart', at(10))], T0) == Decision('wait', 'Restart', at=at(10))


def test_wait_target_tie_goes_to_rest_and_ignores_non_cut_in():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    waiting = [TaskRef('MetaDemon', at(30)), TaskRef('Nian', at(5))]   # 平手取休息截止；Nian 不在名单里
    assert decide(s, p, [], waiting, T0) == Decision('wait', None, at=at(30))


def test_cut_in_time_counts_toward_rest():
    s = running(SIX)
    p = run_row(started(s), s, 1)   # 10:01 起休息 5 分钟，到 10:06
    assert decide(s, p, [], [TaskRef('MetaDemon', at(3))], at(1)) == Decision('wait', 'MetaDemon', at=at(3))
    assert decide(s, p, [TaskRef('MetaDemon', at(3))], [], at(3)) == Decision('native', 'MetaDemon')
    assert decide(s, p, [], [], at(4)) == Decision('wait', None, at=at(6))   # 插队花掉的时间不顺延休息截止
    assert decide(s, p, [], [], at(6)) == Decision('row', 'Orochi', row_index=1)


# 裁定 6：sync_controls

def test_start_resets_progress_but_keeps_saved_params():
    leftover = {'orochi.orochi_config.limit_count': 30}
    old = Progress(phase='done', row_index=4, loop_count=3, total_elapsed=999.0, history=[{'row_index': 4}],
                   saved_params=leftover, last_command_seq=0)
    p = sync_controls(running(SIX, command_seq=3), old, at(5))
    assert p == Progress(phase='ready', sequence_started_at=at(5), saved_params=leftover, last_command_seq=3)
    assert p.saved_params is not leftover   # 是副本


def test_start_syncs_command_seq_so_stale_skip_rest_never_fires():
    s = running(SIX, command='skip_rest', command_seq=3)   # 开始之前就发出的命令
    p = sync_controls(s, Progress(phase='done'), T0)
    assert p.last_command_seq == 3
    p = run_row(p, s, 1)   # 第 1 行打完，休息到 10:06
    p = sync_controls(s, p, at(2))
    assert p.phase == 'resting' and p.rest_until == at(6)


def test_stop_goes_idle_and_keeps_saved_params():
    p = Progress(phase='resting', row_index=1, rest_until=at(30), rest_remaining=600.0, paused=True,
                 saved_params={'k': 1})
    out = sync_controls(Orchestra(rows=SIX, state='idle'), p, T0)
    assert (out.phase, out.paused, out.rest_until, out.rest_remaining) == ('idle', False, None, None)
    assert out.saved_params == {'k': 1}   # 留给运行时还原
    assert sync_controls(Orchestra(rows=SIX, state='idle'), out, T0) == out   # 已经停下，再同步不变


def test_skip_rest_while_paused_takes_effect_on_resume():
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    p = sync_controls(Orchestra(rows=SIX, state='paused'), p, at(20))
    p = sync_controls(Orchestra(rows=SIX, state='paused', command='skip_rest', command_seq=1), p, at(25))
    assert p.rest_remaining == 0.0 and p.paused
    p = sync_controls(running(SIX, command='skip_rest', command_seq=1), p, at(40))
    assert p.rest_until == at(40) and not p.paused and p.rest_remaining is None


def test_skip_rest_outside_rest_is_consumed_without_effect():
    s = running(SIX, command='skip_rest', command_seq=2)
    p = sync_controls(s, Progress(phase='ready', last_command_seq=1), T0)
    assert p.last_command_seq == 2
    p = run_row(p, s, 1)
    assert sync_controls(s, p, at(2)).rest_until == at(6)   # 同一条命令不会重放


def test_unknown_command_is_consumed_without_effect():
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    out = sync_controls(running(SIX, command='dance', command_seq=1), p, at(10))
    assert out.rest_until == at(30) and out.last_command_seq == 1


# 裁定 6f：运行中改表

def test_table_shrunk_in_loop_mode_wraps_to_first_row():
    s = running(SIX[:3], after_finish='loop')
    p = Progress(phase='resting', row_index=5, rest_until=at(30))
    out = sync_controls(s, p, T0)
    assert (out.phase, out.row_index, out.loop_count, out.rest_until) == ('resting', 0, 1, at(30))   # 休息照常


@pytest.mark.parametrize('after_finish', ['stop', 'forever'], ids=['stop', 'unknown-value'])
def test_table_shrunk_in_stop_mode_finishes(after_finish):
    p = Progress(phase='resting', row_index=5, rest_until=at(30))
    out = sync_controls(running(SIX[:3], after_finish=after_finish), p, T0)
    assert out.phase == 'done' and out.rest_until is None


def test_table_edit_drops_progress_of_the_vanished_row():
    s = running(SIX[:3], after_finish='loop')
    p = Progress(phase='interrupted', row_index=5, row_elapsed_before=720, done_count=18)
    out = sync_controls(s, p, T0)
    assert (out.phase, out.row_index, out.row_elapsed_before, out.done_count) == ('ready', 0, 0.0, None)


def test_table_edit_waits_while_paused_and_applies_after_resume():
    p = Progress(phase='resting', row_index=5, rest_until=at(30))
    p = sync_controls(Orchestra(rows=SIX[:3], state='paused'), p, at(10))
    assert p.phase == 'resting' and p.row_index == 5
    assert sync_controls(running(SIX[:3]), p, at(20)).phase == 'done'


def test_table_edit_leaves_a_valid_current_row_alone():
    p = Progress(phase='interrupted', row_index=2, row_elapsed_before=720, done_count=18)
    assert sync_controls(running(SIX), p, T0) == p


# 裁定 7：row_limits 的边角

@pytest.mark.parametrize('phase, elapsed, done, expected', [
    ('interrupted', 721, 18, (12, 8)),    # 剩 7.98 分钟，向上取整
    ('interrupted', 1190, 29, (1, 1)),    # 剩不到 1 分钟按 1 分钟，剩 1 次
    ('interrupted', 1500, 35, (1, 1)),    # 已超上限，也至少给 1
    ('interrupted', 720, None, (30, 8)),  # 已打次数未知，次数按原上限
    ('resting', 720, 18, (30, 20)),       # 不是续跑，原样
    ('ready', 720, 18, (30, 20)),
], ids=['ceil', 'under-a-minute', 'over-cap', 'count-unknown', 'resting', 'ready'])
def test_row_limits_edges(phase, elapsed, done, expected):
    p = Progress(phase=phase, row_elapsed_before=elapsed, done_count=done)
    assert row_limits(SIX[0], p, 'remaining') == expected


def test_row_limits_leaves_unfilled_fields_unfilled():
    p = Progress(phase='interrupted', row_elapsed_before=720, done_count=18)
    assert row_limits(OrchestraRow(task='Orochi'), p, 'remaining') == (None, None)
    assert row_limits(SIX[5], p, 'remaining') == (None, 18)   # 只填了 30 分钟：30 - 12


# 裁定 8、9、10：行的开始、结束与恢复

def test_row_start_resumes_the_same_interrupted_row():
    p = Progress(phase='interrupted', row_index=2, row_elapsed_before=720, done_count=18, heartbeat_at=at(5),
                 rest_until=at(30), rest_remaining=60.0)
    out = on_row_start(p, 2, at(60), {'k': 1})
    assert (out.phase, out.row_index, out.row_started_at, out.heartbeat_at) == ('row_running', 2, at(60), None)
    assert out.row_elapsed_before == 720 and out.done_count == 18   # 续跑，累计的时间和次数保留
    assert (out.rest_until, out.rest_remaining, out.saved_params) == (None, None, {'k': 1})


@pytest.mark.parametrize('phase, row_index', [('interrupted', 3), ('resting', 2), ('ready', 2)],
                         ids=['other-row', 'not-interrupted-resting', 'not-interrupted-ready'])
def test_row_start_otherwise_starts_from_zero(phase, row_index):
    p = Progress(phase=phase, row_index=2, row_elapsed_before=720, done_count=18)
    out = on_row_start(p, row_index, at(60), {})
    assert (out.phase, out.row_index) == ('row_running', row_index)
    assert out.row_elapsed_before == 0.0 and out.done_count is None


def test_row_start_copies_saved_params():
    saved = {'orochi.orochi_config.limit_count': 30}
    out = on_row_start(Progress(), 0, T0, saved)
    saved['orochi.orochi_config.limit_count'] = 99
    assert out.saved_params == {'orochi.orochi_config.limit_count': 30}


def test_interrupted_row_end_accumulates_and_keeps_count_when_unknown():
    s = running(SIX)
    p = Progress(phase='interrupted', row_index=0, row_elapsed_before=720, done_count=18)
    p = on_row_start(p, 0, T0, {'k': 1})
    p.heartbeat_at = at(1)
    out = on_row_end(p, s, at(2), False, None, random.Random(0))
    assert out.phase == 'interrupted' and out.row_elapsed_before == 840
    assert out.done_count == 18   # 这次没读到次数，保留此前的累计
    assert out.saved_params == {} and out.heartbeat_at is None
    assert on_row_end(p, s, at(2), False, 25, random.Random(0)).done_count == 25


def test_interrupted_row_end_under_rerun_policy_resets_counters():
    s = running(SIX, resume_policy='rerun')
    p = run_row(Progress(phase='ready'), s, 2, completed=False, done_count=7)
    assert (p.phase, p.row_elapsed_before, p.done_count) == ('interrupted', 0.0, None)


def test_interrupted_row_resumes_with_remaining_limits_and_total_counts_every_run():
    s = running(SIX)
    p = run_row(started(s), s, 12, completed=False, done_count=18)   # 打了 12 分钟、18 次被打断
    assert row_limits(SIX[0], p, s.resume_policy) == (12, 8)
    p = run_row(p, s, 8, start=at(60), done_count=30)               # 续跑同一行，补完剩下的
    assert (p.phase, p.row_index, p.total_elapsed) == ('resting', 1, 1200)   # 12 + 8 分钟都计入
    assert p.row_elapsed_before == 0.0 and p.done_count is None


def test_completed_row_end_writes_history_and_clears_run_state():
    s = running(SIX)
    p = on_row_start(started(s), 0, T0, {'k': 1})
    p.heartbeat_at = at(1)
    out = on_row_end(p, s, at(3), True, 30, random.Random(0))
    assert out.total_elapsed == 180 and out.saved_params == {} and out.heartbeat_at is None
    assert out.history == [{'row_index': 0, 'task': 'Orochi', 'started_at': T0.isoformat(),
                            'ended_at': at(3).isoformat(), 'done_count': 30, 'loop_count': 0}]
    json.dumps(out.history)   # 只含 JSON 原生值


def test_history_keeps_only_the_last_entries_in_memory():
    s = running([OrchestraRow(task='Orochi', rest_minutes=0)], after_finish='loop')
    p = started(s)
    for i in range(HISTORY_LIMIT + 5):
        p = run_row(p, s, 1, start=at(2 * i))
    assert [e['loop_count'] for e in p.history] == list(range(5, HISTORY_LIMIT + 5))


def test_rest_comes_from_the_row_else_the_default():
    rows = [OrchestraRow(task='Orochi', rest_minutes=7), OrchestraRow(task='Orochi'), OrchestraRow(task='Orochi')]
    s = running(rows, default_rest_minutes=3)
    p = run_row(started(s), s, 1)
    assert p.rest_until == at(1 + 7)   # 本行自己的休息
    p = run_row(p, s, 1, start=at(20))
    assert p.rest_until == at(21 + 3)  # 本行没填，用默认


def test_rest_jitter_is_one_uniform_draw_on_the_rest():
    s = running(SIX, rest_jitter=True)
    p = run_row(started(s), s, 1, rng=random.Random(1))
    expected = 300 * random.Random(1).uniform(0.8, 1.2)
    assert (p.rest_until - at(1)).total_seconds() == pytest.approx(expected)


def test_cap_ends_a_loop_too():
    s = running(SIX, total_minutes_cap=1, after_finish='loop')
    assert run_row(started(s), s, 1).phase == 'done'


def test_row_end_survives_the_row_vanishing_during_the_run():
    p = on_row_start(Progress(phase='ready', row_index=5), 5, T0, {})   # 第 6 行开跑后，表被改成只剩 3 行
    out = on_row_end(p, running(SIX[:3]), at(1), True, None, random.Random(0))
    assert out.phase == 'done' and out.history[-1]['task'] is None
    loop = running(SIX[:3], after_finish='loop', default_rest_minutes=3)
    out = on_row_end(p, loop, at(1), True, None, random.Random(0))
    assert (out.phase, out.row_index, out.loop_count, out.rest_until) == ('resting', 0, 1, at(1 + 3))   # 行没了，休息取默认


def test_row_end_without_start_time_counts_zero():
    out = on_row_end(Progress(phase='ready'), running(SIX), at(1), True, None, random.Random(0))
    assert out.total_elapsed == 0.0 and out.history[-1]['started_at'] is None


def test_clock_moving_backwards_never_gives_negative_time():
    p = Progress(phase='row_running', row_started_at=at(10), heartbeat_at=at(5))
    assert recover(p, at(20)).row_elapsed_before == 0.0
    s = running(SIX)
    p = on_row_start(started(s), 0, at(10), {})
    assert on_row_end(p, s, at(5), True, None, random.Random(0)).total_elapsed == 0.0
    assert on_row_end(p, s, at(5), False, None, random.Random(0)).row_elapsed_before == 0.0


def test_pause_after_the_rest_deadline_keeps_zero_remaining():
    p = Progress(phase='resting', row_index=1, rest_until=at(10))
    assert sync_controls(Orchestra(rows=SIX, state='paused'), p, at(20)).rest_remaining == 0.0


def test_recover_without_heartbeat_adds_nothing_and_keeps_state():
    p = Progress(phase='row_running', row_started_at=T0, done_count=18, row_elapsed_before=60.0,
                 saved_params={'k': 1})
    out = recover(p, at(300))
    assert (out.phase, out.row_elapsed_before, out.done_count, out.saved_params) == ('interrupted', 60.0, 18, {'k': 1})


@pytest.mark.parametrize('phase', ['idle', 'ready', 'resting', 'interrupted', 'done'])
def test_recover_is_a_noop_outside_row_running(phase):
    p = Progress(phase=phase, row_started_at=T0, heartbeat_at=at(7), row_elapsed_before=60.0)
    out = recover(p, at(300))
    assert out == p and out is not p


def test_recover_twice_does_not_double_count():
    p = Progress(phase='row_running', row_started_at=T0, heartbeat_at=at(7))
    assert recover(recover(p, at(300)), at(600)).row_elapsed_before == 420


# 裁定 1：纯函数

def test_functions_do_not_mutate_their_input():
    s = running(SIX, command='skip_rest', command_seq=1)
    s_before = s.model_dump()
    resting = Progress(phase='resting', row_index=5, rest_until=at(30), saved_params={'k': 1},
                       history=[{'row_index': 0}])   # 第 6 行：SIX[:3] 下已越界
    in_row = Progress(phase='row_running', row_started_at=T0, heartbeat_at=at(7), saved_params={'k': 1},
                      history=[{'row_index': 0}])
    done = Progress(phase='done', saved_params={'k': 1}, history=[{'row_index': 0}])
    calls = [
        (resting, lambda p: sync_controls(s, p, at(10))),   # 跳过休息
        (resting, lambda p: sync_controls(Orchestra(rows=SIX, state='paused'), p, at(10))),
        (resting, lambda p: sync_controls(Orchestra(rows=SIX, state='idle'), p, at(10))),
        (resting, lambda p: sync_controls(running(SIX[:3]), p, at(10))),   # 表被改短
        (done, lambda p: sync_controls(running(SIX), p, at(10))),   # 重新开始
        (in_row, lambda p: recover(p, at(300))),
        (resting, lambda p: on_row_start(p, 1, at(10), {'a': 1})),
        (in_row, lambda p: on_row_end(p, s, at(10), True, 3, random.Random(0))),
        (in_row, lambda p: on_row_end(p, s, at(10), False, 3, random.Random(0))),
    ]
    for p, call in calls:
        before = copy.deepcopy(p)
        out = call(p)
        assert p == before
        assert out is not p and out.saved_params is not p.saved_params and out.history is not p.history
    decide(s, resting, [TaskRef('MetaDemon', T0)], [TaskRef('Restart', at(5))], at(1))
    assert resting.rest_until == at(30) and s.model_dump() == s_before
