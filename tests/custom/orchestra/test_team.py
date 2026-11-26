"""等搭档的会合判断（module.orchestra.team）：组队行序号、会合规则 a 与 b、超时与最后一读、续跑时搭档在哪、面板上的搭档情况。

前半部分照简报原样，只有 test_partner_situation 按审查前的裁定改了一条、加了一条；补充用例放在文件末尾。
"""
import copy
import random
from datetime import datetime, timedelta

import pytest

from module.orchestra.engine import finish_interrupted_row
from module.orchestra.model import Orchestra, OrchestraRow
from module.orchestra.progress import Progress
from module.orchestra.team import (gate, partner_met, partner_situation, partner_waiting, rescue_after_timeout,
                                   resume_partner_state, team_ordinal)

T0 = datetime(2026, 11, 26, 10, 0)
ROWS = [OrchestraRow(task='Orochi'), OrchestraRow(task='RealmRaid'), OrchestraRow(task='RealmRaid'),
        OrchestraRow(task='Orochi'), OrchestraRow(task='RealmRaid')]


def waiting(**kw) -> Progress:
    """搭档 u 正在等第 1 个组队行（八岐大蛇），它的搭档是 t。"""
    return Progress(**{**dict(phase='ready', team_wait_since=T0, team_wait_ordinal=1, team_wait_task='Orochi',
                              team_alive_at=T0, team_partner='t'), **kw})


def test_team_ordinal_counts_runnable_team_rows():
    tasks = frozenset({'Orochi'})
    assert [team_ordinal(ROWS, i, tasks) for i in range(5)] == [1, None, None, 2, None]
    rows = [OrchestraRow(task='NoSuchTask'), *ROWS]
    assert team_ordinal(rows, 0, tasks) is None and team_ordinal(rows, 1, tasks) == 1


def test_partner_waiting_requires_every_condition():
    now = T0 + timedelta(seconds=90)
    assert partner_waiting('t', waiting(), 1, 'Orochi', now)
    assert partner_waiting('t', waiting(phase='interrupted'), 1, 'Orochi', now)
    for bad in (dict(team_alive_at=T0 - timedelta(seconds=1)), dict(paused=True), dict(phase='resting'),
                dict(team_wait_ordinal=2), dict(team_wait_task='EternitySea'), dict(team_partner='x'),
                dict(team_wait_since=None)):
        assert not partner_waiting('t', waiting(**bad), 1, 'Orochi', now)
    assert not partner_waiting('t', None, 1, 'Orochi', now)


def test_partner_met_only_counts_meetings_after_my_wait():
    partner = Progress(team_met_at=T0, team_met_ordinal=1, team_met_task='Orochi')
    assert partner_met(partner, 1, 'Orochi', since=T0) == T0
    assert partner_met(partner, 1, 'Orochi', since=T0 + timedelta(seconds=1)) is None
    assert partner_met(partner, 2, 'Orochi', since=T0) is None
    assert partner_met(partner, 1, 'EternitySea', since=T0) is None


def test_first_arrival_starts_waiting():
    p = Progress(phase='resting', row_index=3, rest_until=T0)
    g = gate(p, 't', None, 2, 'Orochi', T0, 30, resuming=False)
    q = g.progress
    assert g.kind == 'wait'
    assert (q.phase, q.rest_until, q.team_wait_since, q.team_wait_ordinal, q.team_wait_task, q.team_alive_at) == \
        ('ready', None, T0, 2, 'Orochi', T0)
    assert p.phase == 'resting'                                  # 不改传入的对象


def test_meets_a_waiting_partner():
    p = gate(Progress(phase='ready'), 't', None, 1, 'Orochi', T0, 30, False).progress
    now = T0 + timedelta(seconds=40)
    g = gate(p, 't', waiting(team_wait_since=T0 + timedelta(seconds=20), team_alive_at=now), 1, 'Orochi', now, 30, False)
    assert (g.kind, g.met_at) == ('go', T0 + timedelta(seconds=20))
    q = g.progress
    assert (q.team_met_at, q.team_met_ordinal, q.team_met_task) == (T0 + timedelta(seconds=20), 1, 'Orochi')
    assert (q.team_wait_since, q.team_alive_at) == (None, None)


def test_goes_when_partner_already_met_me():
    p = gate(Progress(phase='ready'), 't', None, 1, 'Orochi', T0, 30, False).progress
    partner = Progress(phase='row_running', team_met_at=T0 + timedelta(seconds=5), team_met_ordinal=1,
                       team_met_task='Orochi')
    g = gate(p, 't', partner, 1, 'Orochi', T0 + timedelta(seconds=30), 30, False)
    assert (g.kind, g.met_at) == ('go', T0 + timedelta(seconds=5))


def test_times_out_at_the_limit():
    p = gate(Progress(phase='ready'), 't', None, 1, 'Orochi', T0, 30, False).progress
    assert gate(p, 't', None, 1, 'Orochi', T0 + timedelta(minutes=30, seconds=-1), 30, False).kind == 'wait'
    g = gate(p, 't', None, 1, 'Orochi', T0 + timedelta(minutes=30), 30, False)
    assert g.kind == 'timeout' and g.progress.team_wait_since is None


def test_moving_to_another_team_row_restarts_the_clock():
    p = gate(Progress(phase='ready'), 't', None, 1, 'Orochi', T0, 30, False).progress
    later = T0 + timedelta(minutes=10)
    assert gate(p, 't', None, 2, 'Orochi', later, 30, False).progress.team_wait_since == later


def test_rescue_after_timeout_only_by_rule_b():
    met = Progress(team_met_at=T0 + timedelta(minutes=29), team_met_ordinal=1, team_met_task='Orochi')
    rescued = rescue_after_timeout(Progress(phase='ready'), met, 1, 'Orochi', since=T0)
    assert rescued is not None and rescued.team_met_at == T0 + timedelta(minutes=29)
    assert rescue_after_timeout(Progress(phase='ready'), waiting(), 1, 'Orochi', since=T0) is None


@pytest.mark.parametrize('partner, expected', [
    (Progress(phase='row_running', row_task='Orochi', team_row_ordinal=2, heartbeat_at=T0), 'here'),
    (Progress(phase='row_running', row_task='Orochi', team_row_ordinal=2,
              heartbeat_at=T0 - timedelta(seconds=91)), 'stopped'),
    (Progress(phase='interrupted', row_task='Orochi', team_row_ordinal=2), 'stopped'),
    (Progress(phase='ready', team_wait_since=T0, team_wait_ordinal=2, team_wait_task='Orochi'), 'stopped'),
    (None, 'stopped'),
    (Progress(phase='resting', row_index=4), 'left'),
    (Progress(phase='row_running', row_task='RealmRaid', heartbeat_at=T0), 'left'),
    (Progress(phase='ready', team_wait_since=T0, team_wait_ordinal=1, team_wait_task='Orochi'), 'left'),
])
def test_resume_partner_state(partner, expected):
    assert resume_partner_state(partner, 2, 'Orochi', T0) == expected


def test_resume_with_old_partner_progress_compares_task_only():
    old = Progress(phase='row_running', row_task='Orochi', heartbeat_at=T0)   # 升级前的进度没有 team_row_ordinal
    assert resume_partner_state(old, 2, 'Orochi', T0) == 'here'


def test_resuming_gate_branches():
    p = Progress(phase='interrupted', row_index=3, row_task='Orochi')
    here = Progress(phase='row_running', row_task='Orochi', team_row_ordinal=2, heartbeat_at=T0)
    assert gate(p, 't', here, 2, 'Orochi', T0, 30, True).kind == 'go'
    assert gate(p, 't', Progress(phase='resting'), 2, 'Orochi', T0, 30, True).kind == 'skip'
    g = gate(p, 't', Progress(phase='interrupted', row_task='Orochi', team_row_ordinal=2), 2, 'Orochi', T0, 30, True)
    assert g.kind == 'wait' and g.progress.phase == 'interrupted' and g.progress.team_wait_since == T0


@pytest.mark.parametrize('partner, text', [
    (None, '读不到搭档的进度'),
    (Progress(team_partner='x', phase='resting'), '搭档设的搭档不是我'),
    (Progress(phase='idle'), '搭档的编排没在跑'),
    (Progress(team_partner='t', paused=True), '搭档已暂停'),
    (waiting(team_alive_at=T0 - timedelta(seconds=91)), '搭档 90 秒没有动静'),
    (waiting(), '搭档在等第 1 个组队行，我在第 2 个'),
    (waiting(team_wait_ordinal=2, team_wait_task='EternitySea'), '搭档这一行是 御魂·永生之海，我是 御魂·八岐大蛇'),
    (waiting(team_wait_ordinal=2), '搭档也在等'),
    (Progress(team_partner='t', phase='row_running', row_index=2, row_task='RealmRaid', heartbeat_at=T0),
     '搭档在跑第 3 行 个人突破'),
    (Progress(team_partner='t', phase='row_running', row_index=2, row_task='RealmRaid',
              heartbeat_at=T0 - timedelta(seconds=91)), '搭档 90 秒没有动静'),
    (Progress(team_partner='t', phase='resting'), '搭档在休息'),
    (Progress(team_partner='t', phase='interrupted', row_index=1), '搭档第 2 行中断了'),
    (Progress(team_partner='t', phase='idle'), '搭档的编排没在跑'),
    (Progress(team_partner='t', phase='ready'), '搭档还没到'),
])
def test_partner_situation(partner, text):
    assert partner_situation('t', partner, 2, 'Orochi', T0) == text


# ---------- 补充用例 ----------

def test_team_ordinal_skips_invalid_team_rows_and_out_of_range_indexes():
    tasks = frozenset({'Orochi'})
    rows = [OrchestraRow(task='Orochi', max_minutes=2000), OrchestraRow(task='Orochi')]   # 第 1 行校验不过
    assert [team_ordinal(rows, i, tasks) for i in range(2)] == [None, 1]
    assert team_ordinal(ROWS, 5, tasks) is None and team_ordinal(ROWS, -2, tasks) is None   # 负数不从末尾倒数


def test_a_row_just_started_counts_from_its_start_until_the_first_heartbeat():
    started = dict(phase='row_running', row_index=0, row_task='Orochi', team_row_ordinal=2, team_partner='t')
    fresh = Progress(row_started_at=T0 - timedelta(seconds=10), **started)
    stale = Progress(row_started_at=T0 - timedelta(seconds=91), **started)
    assert resume_partner_state(fresh, 2, 'Orochi', T0) == 'here'
    assert resume_partner_state(stale, 2, 'Orochi', T0) == 'stopped'
    assert resume_partner_state(Progress(**started), 2, 'Orochi', T0) == 'stopped'   # 开始时间也没有
    assert partner_situation('t', fresh, 2, 'Orochi', T0) == '搭档在跑第 1 行 御魂·八岐大蛇'
    assert partner_situation('t', stale, 2, 'Orochi', T0) == '搭档 90 秒没有动静'


def test_later_polls_keep_the_clock_and_refresh_alive():
    p = gate(Progress(phase='ready'), 't', None, 1, 'Orochi', T0, 30, False).progress
    later = T0 + timedelta(seconds=30)
    q = gate(p, 't', None, 1, 'Orochi', later, 30, False).progress
    assert (q.team_wait_since, q.team_alive_at, q.phase) == (T0, later, 'ready')


def test_huge_wait_minutes_do_not_overflow():
    p = gate(Progress(phase='ready'), 't', None, 1, 'Orochi', T0, 30, False).progress
    assert gate(p, 't', None, 1, 'Orochi', T0 + timedelta(days=1), 10 ** 15, False).kind == 'wait'


def test_gate_and_rescue_do_not_touch_their_inputs():
    p = Progress(phase='interrupted', row_index=3, row_task='Orochi', history=[{'row_index': 0}],
                 saved_params={'k': 1}, team_wait_since=T0, team_wait_ordinal=2, team_wait_task='Orochi',
                 team_alive_at=T0)
    partners = [None, waiting(team_wait_ordinal=2, team_alive_at=T0 + timedelta(seconds=30)),
                Progress(phase='resting'), Progress(phase='interrupted', row_task='Orochi', team_row_ordinal=2),
                Progress(team_met_at=T0, team_met_ordinal=2, team_met_task='Orochi')]
    for resuming in (False, True):
        for partner in partners:
            before, partner_before = copy.deepcopy(p), copy.deepcopy(partner)
            for now in (T0 + timedelta(seconds=30), T0 + timedelta(hours=1)):   # 等着、会合，以及超时
                out = gate(p, 't', partner, 2, 'Orochi', now, 30, resuming).progress
                assert out is not p and out.history is not p.history and out.saved_params is not p.saved_params
            assert (p, partner) == (before, partner_before)
    met = Progress(team_met_at=T0, team_met_ordinal=2, team_met_task='Orochi')
    rescued = rescue_after_timeout(p, met, 2, 'Orochi', since=T0)
    assert rescued is not p and p.team_met_at is None


# ---------- 审查前的裁定 ----------

def test_a_wait_left_by_a_skipped_row_does_not_carry_over_to_the_next_same_team_row():
    """续跑时等过、搭档离开后按已打完记账，旧的等待记录不能留给下一个同序号同任务的组队行，否则一到就超时。"""
    p = Progress(phase='interrupted', row_index=0, row_task='Orochi', row_started_at=T0, team_row_ordinal=1,
                 team_wait_since=T0, team_wait_ordinal=1, team_wait_task='Orochi', team_alive_at=T0)
    settings = Orchestra(rows=[OrchestraRow(task='Orochi')], state='running', after_finish='loop')
    booked = finish_interrupted_row(p, settings, T0 + timedelta(seconds=30), random.Random(0))
    later = T0 + timedelta(minutes=40)
    g = gate(booked, 't', None, 1, 'Orochi', later, 30, resuming=False)
    assert (g.kind, g.progress.team_wait_since) == ('wait', later)


def test_paused_or_stopped_partner_is_reported_before_the_pairing_check():
    assert partner_situation('t', Progress(team_partner='x', paused=True), 2, 'Orochi', T0) == '搭档已暂停'
    assert partner_situation('t', Progress(team_partner='x', phase='done'), 2, 'Orochi', T0) == '搭档的编排没在跑'
