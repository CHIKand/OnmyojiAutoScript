import copy
import json
import random
from dataclasses import fields, replace
from datetime import datetime, timedelta

import pytest

from module.orchestra.engine import (Decision, TaskRef, decide, finish_interrupted_row, next_row_index, on_row_end,
                                     on_row_start, recover, row_limits, sync_controls)
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
    """依次调用 on_row_start 与 on_row_end，返回新进度。行号取进度里的当前行，任务取表里那一行的，同运行时。"""
    task = s.rows[p.row_index].task if 0 <= p.row_index < len(s.rows) else None
    p = on_row_start(p, p.row_index, start, {}, task)
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
    p = on_row_start(started(s), 0, T0, {'k': 1}, 'Orochi')
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
        (resting, lambda p: sync_controls(running(SIX, command='start', command_seq=9), p, at(10))),   # 开始命令
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


# ---------- 最终评审修正 ----------

# I2：上游的自愈任务 SoulsTidy 与 Restart 一样永远放行，Restart 在前

def test_souls_tidy_is_always_let_through():
    s = running(SIX, cut_in_tasks=[])
    assert decide(s, started(s), [TaskRef('SoulsTidy', T0)], [], T0) == Decision('native', 'SoulsTidy')


def test_restart_then_souls_tidy_then_the_cut_in_list():
    s = running(SIX)
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    pending = [TaskRef('MetaDemon', T0), TaskRef('SoulsTidy', T0)]
    assert decide(s, p, pending, [], T0) == Decision('native', 'SoulsTidy')   # 自愈先于插队名单，不看待办顺序
    pending = [TaskRef('SoulsTidy', T0), TaskRef('Restart', T0)]
    assert decide(s, p, pending, [], T0) == Decision('native', 'Restart')


def test_wait_target_honours_souls_tidy_in_waiting():
    s = running(SIX, cut_in_tasks=[])
    p = Progress(phase='resting', row_index=1, rest_until=at(30))
    assert decide(s, p, [], [TaskRef('SoulsTidy', at(10))], T0) == Decision('wait', 'SoulsTidy', at=at(10))


def test_a_cut_in_task_beyond_the_catalog_runs_natively():
    s = running(SIX, cut_in_tasks=['TrueOrochi'])
    assert decide(s, started(s), [TaskRef('TrueOrochi', T0)], [], T0) == Decision('native', 'TrueOrochi')
    assert decide(s, started(s), [TaskRef('MetaDemon', T0)], [], T0) == Decision('row', 'Orochi', row_index=0)


# I3：开始命令，"停止后马上开始"在实例读到配置之前也不会丢

def test_start_command_restarts_a_sequence_in_progress():
    old = Progress(phase='resting', row_index=3, loop_count=1, sequence_started_at=T0, rest_until=at(30),
                   total_elapsed=600.0, history=[{'row_index': 2}], saved_params={'k': 1}, last_command_seq=4,
                   status_note='防封禁休眠中，08:00 继续')
    p = sync_controls(running(SIX, command='start', command_seq=5), old, at(10))
    assert p == Progress(phase='ready', sequence_started_at=at(10), saved_params={'k': 1}, last_command_seq=5)


@pytest.mark.parametrize('phase', ['ready', 'resting', 'interrupted', 'idle', 'done'])
def test_start_command_resets_whatever_the_phase(phase):
    old = Progress(phase=phase, row_index=4, row_task='GoryouRealm', row_elapsed_before=720.0, done_count=18,
                   sequence_started_at=T0, rest_until=at(30), paused=True, rest_remaining=60.0)
    p = sync_controls(running(SIX, command='start', command_seq=1), old, at(10))
    assert (p.phase, p.row_index, p.sequence_started_at, p.last_command_seq) == ('ready', 0, at(10), 1)
    assert (p.row_task, p.row_elapsed_before, p.done_count, p.paused, p.rest_until) == (None, 0.0, None, False, None)


def test_start_command_with_the_instance_off_does_not_resume_yesterdays_row():
    crashed = Progress(phase='row_running', row_index=2, row_task='GoryouRealm', sequence_started_at=T0,
                       row_started_at=T0, heartbeat_at=at(5), saved_params={'k': 1})
    s = running(SIX, command='start', command_seq=1)
    p = sync_controls(s, recover(crashed, at(600)), at(600))   # 进程起来后的第一次选任务：先 recover 再同步
    assert (p.phase, p.row_index, p.sequence_started_at, p.saved_params) == ('ready', 0, at(600), {'k': 1})
    assert decide(s, p, [], [], at(600)) == Decision('row', 'Orochi', row_index=0)


def test_start_command_is_handled_once():
    s = running(SIX, command='start', command_seq=1)
    p = run_row(sync_controls(s, Progress(phase='resting', row_index=3), T0), s, 1)
    assert (p.phase, p.row_index) == ('resting', 1)
    assert sync_controls(s, p, at(2)) == p   # 同一条命令处理过了，之后每次选任务都不再重来


def test_start_command_then_pause_starts_paused():
    s = Orchestra(rows=SIX, state='paused', command='start', command_seq=1)
    p = sync_controls(s, Progress(phase='resting', row_index=3, rest_until=at(30)), at(10))
    assert (p.phase, p.row_index, p.paused) == ('ready', 0, True)
    assert decide(s, p, [], [], at(10)) == Decision('passthrough')


def test_start_command_then_stop_ends_idle_and_keeps_the_originals():
    s = Orchestra(rows=SIX, state='idle', command='start', command_seq=1)
    p = sync_controls(s, Progress(phase='resting', row_index=3, rest_until=at(30), saved_params={'k': 1}), at(10))
    assert (p.phase, p.row_index, p.rest_until, p.saved_params, p.last_command_seq) == ('idle', 0, None, {'k': 1}, 1)


# N1：表在运行中被改过，凭 row_task 认行

def test_row_start_resumes_only_the_same_task_at_the_same_index():
    p = Progress(phase='interrupted', row_index=2, row_task='GoryouRealm', row_elapsed_before=720.0, done_count=18)
    same = on_row_start(p, 2, at(60), {}, 'GoryouRealm')
    assert (same.row_elapsed_before, same.done_count, same.row_task) == (720.0, 18, 'GoryouRealm')
    other = on_row_start(p, 2, at(60), {}, 'Orochi')   # 同一个行号，表里已经换成别的任务
    assert (other.row_elapsed_before, other.done_count, other.row_task) == (0.0, None, 'Orochi')


def test_history_records_the_task_that_ran_not_the_edited_table():
    s = running(SIX)
    p = on_row_start(started(s), 0, T0, {}, 'Orochi')
    edited = running([OrchestraRow(task='GoryouRealm')] + SIX[1:])   # 第 1 行运行中被改成了御灵
    assert on_row_end(p, edited, at(3), True, 30, random.Random(0)).history[-1]['task'] == 'Orochi'


def test_row_task_stays_while_interrupted_and_clears_once_the_row_completes():
    s = running(SIX)
    p = on_row_start(started(s), 0, T0, {}, 'Orochi')
    assert on_row_end(p, s, at(3), False, 5, random.Random(0)).row_task == 'Orochi'
    assert on_row_end(p, s, at(3), True, 5, random.Random(0)).row_task is None


def test_table_edit_drops_the_task_of_the_vanished_row_too():
    p = Progress(phase='interrupted', row_index=5, row_task='Orochi', row_elapsed_before=720, done_count=18)
    out = sync_controls(running(SIX[:3], after_finish='loop'), p, T0)
    assert (out.phase, out.row_index, out.row_task) == ('ready', 0, None)


# N5：跑完与停止时清掉防封禁提示

def test_done_and_stop_clear_the_status_note():
    note = '防封禁休眠中，08:00 继续'
    s = running([OrchestraRow(task='Orochi')])
    p = on_row_start(started(s), 0, T0, {}, 'Orochi')
    p.status_note = note
    assert on_row_end(p, s, at(1), True, None, random.Random(0)).status_note == ''   # 跑完
    resting = Progress(phase='resting', row_index=1, rest_until=at(30), status_note=note)
    assert sync_controls(Orchestra(rows=SIX, state='idle'), resting, T0).status_note == ''   # 停止
    assert sync_controls(running(SIX[:3]), replace(resting, row_index=5), T0).status_note == ''   # 改表改到跑完
    assert sync_controls(running(SIX), resting, T0).status_note == note   # 其余时候不动


# N12：行运行中落下的暂停（运行时只在选任务时同步，今天走不到，引擎先兜住）

def test_rest_after_a_row_that_ended_while_paused_keeps_its_full_length():
    s = running(SIX)
    p = on_row_start(started(s), 0, T0, {}, 'Orochi')
    p.paused = True
    out = on_row_end(p, s, at(1), True, None, random.Random(0))
    assert (out.phase, out.paused, out.rest_remaining) == ('resting', True, 300.0)
    resumed = sync_controls(running(SIX), out, at(60))
    assert (resumed.paused, resumed.rest_until, resumed.rest_remaining) == (False, at(65), None)   # 继续时休满 5 分钟


def test_finish_clears_paused():
    s = running([OrchestraRow(task='Orochi')])
    p = on_row_start(started(s), 0, T0, {}, 'Orochi')
    p.paused = True
    out = on_row_end(p, s, at(1), True, None, random.Random(0))
    assert (out.phase, out.paused, out.rest_remaining) == ('done', False, None)


# N13：休息分钟夹在 0–1439（手改的配置不会让 timedelta 溢出）

@pytest.mark.parametrize('rest_minutes, default, seconds', [
    (None, 10 ** 11, 1439 * 60), (None, -5, 0), (5000, 5, 1439 * 60), (-3, 5, 0),
], ids=['default-huge', 'default-negative', 'row-huge', 'row-negative'])
def test_rest_minutes_are_clamped_to_a_day(rest_minutes, default, seconds):
    s = Orchestra(rows=[OrchestraRow(task='Orochi', rest_minutes=rest_minutes), OrchestraRow(task='Orochi')],
                  state='running', default_rest_minutes=default)   # 第 1 行开跑之后被手改成这样
    p = on_row_start(Progress(phase='ready'), 0, T0, {}, 'Orochi')
    out = on_row_end(p, s, at(1), True, None, random.Random(0))
    if seconds:
        assert out.phase == 'resting' and out.rest_until == at(1) + timedelta(seconds=seconds)
    else:
        assert out.phase == 'ready' and out.rest_until is None


# N14：引擎用例的空白

@pytest.mark.parametrize('bad', [
    OrchestraRow(task='Orochi', max_minutes=2000),
    OrchestraRow(task='Orochi', rest_minutes=-1),
    OrchestraRow(task='Orochi', sub1='十三层'),
], ids=['minutes-2000', 'rest-negative', 'sub1-out-of-range'])
def test_rows_with_bad_values_are_skipped(bad):
    s = running([bad, OrchestraRow(task='GoryouRealm')])
    assert next_row_index(s.rows, 0) == 1
    assert decide(s, Progress(phase='ready'), [], [], T0) == Decision('row', 'GoryouRealm', row_index=1)


def test_recover_without_a_start_time_adds_nothing():
    p = Progress(phase='row_running', row_index=1, row_started_at=None, heartbeat_at=at(5), row_elapsed_before=60.0)
    out = recover(p, at(300))
    assert (out.phase, out.row_elapsed_before) == ('interrupted', 60.0)


def test_resume_without_a_recorded_rest_keeps_the_deadline():
    p = Progress(phase='resting', row_index=1, rest_until=at(30), paused=True, rest_remaining=None)
    out = sync_controls(running(SIX), p, at(20))
    assert (out.paused, out.rest_until, out.rest_remaining) == (False, at(30), None)


# ---------- 等搭档：组队序号与续跑时按已打完记账 ----------

def test_on_row_start_records_team_ordinal():
    assert on_row_start(Progress(phase='ready'), 3, T0, {}, 'Orochi', team_ordinal=2).team_row_ordinal == 2


def test_on_row_end_clears_team_ordinal_only_when_completed():
    started = on_row_start(Progress(phase='ready'), 0, T0, {}, 'Orochi', team_ordinal=1)
    settings = Orchestra(rows=[OrchestraRow(task='Orochi'), OrchestraRow(task='RealmRaid')])
    assert on_row_end(started, settings, T0, False, None, random.Random(0)).team_row_ordinal == 1
    assert on_row_end(started, settings, T0, True, None, random.Random(0)).team_row_ordinal is None


def test_finish_interrupted_row_books_it_as_done():
    settings = Orchestra(rows=[OrchestraRow(task='Orochi', rest_minutes=2), OrchestraRow(task='RealmRaid')])
    p = Progress(phase='interrupted', row_index=0, row_task='Orochi', row_started_at=T0, row_elapsed_before=600.0,
                 done_count=7, total_elapsed=100.0, team_row_ordinal=1)
    end = T0 + timedelta(hours=1)
    q = finish_interrupted_row(p, settings, end, random.Random(0))
    assert q.total_elapsed == 700.0
    assert q.history[-1] == {'row_index': 0, 'task': 'Orochi', 'started_at': T0.isoformat(),
                             'ended_at': end.isoformat(), 'done_count': 7, 'loop_count': 0}
    assert (q.phase, q.row_index, q.rest_until) == ('resting', 1, end + timedelta(minutes=2))
    assert (q.row_elapsed_before, q.done_count, q.row_task, q.team_row_ordinal) == (0.0, None, None, None)


def test_finish_interrupted_row_ends_like_a_completed_row():
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='RealmRaid')]
    p = Progress(phase='interrupted', row_index=1, row_task='RealmRaid', row_started_at=T0, heartbeat_at=at(5),
                 row_elapsed_before=300.0, saved_params={'k': 1}, history=[{'row_index': 0}])
    before = copy.deepcopy(p)
    done = finish_interrupted_row(p, running(rows), at(60), random.Random(0))   # 最后一行，跑完就停
    assert (done.phase, done.saved_params, done.heartbeat_at) == ('done', {}, None)
    assert done.history[-1]['task'] == 'RealmRaid'
    looped = finish_interrupted_row(p, running(rows, after_finish='loop'), at(60), random.Random(0))
    assert (looped.phase, looped.row_index, looped.loop_count) == ('resting', 0, 1)
    capped = finish_interrupted_row(p, running(rows, after_finish='loop', total_minutes_cap=5), at(60),
                                    random.Random(0))
    assert capped.phase == 'done'   # 此前累计的 5 分钟计入总时长，到了上限
    assert p == before and done.history is not p.history


# 审查前的裁定：离开等待时清掉等待记录，会合记录不动

def test_on_row_start_clears_the_wait_record():
    met = T0 - timedelta(minutes=1)
    p = Progress(phase='ready', team_wait_since=T0, team_wait_ordinal=1, team_wait_task='Orochi', team_alive_at=T0,
                 team_met_at=met, team_met_ordinal=1, team_met_task='Orochi')
    out = on_row_start(p, 0, at(1), {}, 'RealmRaid')   # 等着的时候表被改了，改跑一个单刷行
    assert (out.team_wait_since, out.team_wait_ordinal, out.team_wait_task, out.team_alive_at) == (None,) * 4
    assert (out.team_met_at, out.team_met_ordinal, out.team_met_task) == (met, 1, 'Orochi')
    assert p.team_wait_since == T0   # 不改传入的对象


def test_finish_interrupted_row_clears_the_wait_record():
    p = Progress(phase='interrupted', row_index=0, row_task='Orochi', row_started_at=T0, team_row_ordinal=1,
                 team_wait_since=T0, team_wait_ordinal=1, team_wait_task='Orochi', team_alive_at=at(1),
                 team_met_at=T0, team_met_ordinal=1, team_met_task='Orochi')
    out = finish_interrupted_row(p, running(SIX), at(2), random.Random(0))
    assert (out.team_wait_since, out.team_wait_ordinal, out.team_wait_task, out.team_alive_at) == (None,) * 4
    assert (out.team_met_at, out.team_met_ordinal, out.team_met_task) == (T0, 1, 'Orochi')


# 运行时审查后的裁定：编排结束时（跑完、停止）也清掉等待记录，会合记录不动

MET = T0 - timedelta(hours=1)


def waiting_at_second_row() -> Progress:
    """在第 2 行等搭档（第 2 个组队行）；上一次会合在第 1 个组队行。"""
    return Progress(phase='ready', row_index=1, sequence_started_at=T0, team_wait_since=T0, team_wait_ordinal=2,
                    team_wait_task='Orochi', team_alive_at=at(1), team_met_at=MET, team_met_ordinal=1,
                    team_met_task='Orochi')


def test_finish_clears_the_wait_record_and_keeps_the_meeting():
    p = waiting_at_second_row()
    out = sync_controls(running([OrchestraRow(task='Orochi')]), p, at(2))   # 等着的第 2 行被改表删掉：序列跑完
    assert out.phase == 'done'
    assert (out.team_wait_since, out.team_wait_ordinal, out.team_wait_task, out.team_alive_at) == (None,) * 4
    assert (out.team_met_at, out.team_met_ordinal, out.team_met_task) == (MET, 1, 'Orochi')
    assert p.team_wait_since == T0   # 不改传入的对象


def test_stop_clears_the_wait_record_and_keeps_the_meeting():
    p = waiting_at_second_row()
    out = sync_controls(Orchestra(rows=SIX, state='idle'), p, at(2))
    assert out.phase == 'idle'
    assert (out.team_wait_since, out.team_wait_ordinal, out.team_wait_task, out.team_alive_at) == (None,) * 4
    assert (out.team_met_at, out.team_met_ordinal, out.team_met_task) == (MET, 1, 'Orochi')
    assert p.team_wait_since == T0


# 终审补的用例：开始命令连会合记录在内清空全部组队字段（停止、跑完只清等待记录）

def test_start_command_clears_every_team_field():
    old = replace(waiting_at_second_row(), last_command_seq=3, team_partner='u', team_row_ordinal=2)
    team = [f.name for f in fields(Progress) if f.name.startswith('team_')]
    assert all(getattr(old, name) != getattr(Progress(), name) for name in team)   # 开始之前每一项都有值
    p = sync_controls(running(SIX, command='start', command_seq=4), old, at(2))
    assert (p.phase, p.sequence_started_at, p.last_command_seq) == ('ready', at(2), 4)
    assert {name: getattr(p, name) for name in team} == {name: getattr(Progress(), name) for name in team}
