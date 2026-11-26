"""等搭档的会合判断：组队行开打前，凭自己和搭档的进度，算出该开打、接着等、等超时，还是（续跑时）跳过这一行，
以及给面板和通知用的搭档情况。

纯，同 engine 模块：不读写文件、不读时钟（时间只来自 now 参数）、不写日志、不用随机。返回 Progress 的函数都在
copy.deepcopy 出来的副本上改，不动传进来的对象。读搭档的进度文件、写日志、发通知、复位卡死计时、退出实例都在运行时。

配对：两边的组队序号（本表里第几个可运行的组队行，从 1 起，每遍循环重新数）相同、任务也相同才配对，子项不比。
会合有两条路：
    a. 搭档此刻正在等同一行：等待记录 FRESH 内刷新过，阶段是 ready 或 interrupted，没有暂停，它设的搭档是我；
    b. 搭档已经先走了：它最近一次会合是同一行，会合时间不早于我开始等的时间。
按 a 会合时，会合时间取两边开始等的时间中较晚的那个，不早于任何一边开始等的时间，所以另一边随后总能按 b 认出来。
会合后各自记下 team_met_*，清掉等待记录。

运行时的用法：设了搭档、这一行是组队行（team_ordinal 不为 None）时，每 POLL 调一次 gate，按 Gate.kind 处理：
go 开打；wait 交一个 POLL 后到点的占位，接着等；timeout 先存盘清空后的进度，再读一次搭档，用 rescue_after_timeout
看它是不是刚按 a 先走了，不是就停下；skip 是续跑的组队行搭档已经离开，用 engine.finish_interrupted_row 按已打完记账。
面板和通知里的搭档情况用 partner_situation，等待中的日志按 LOG_EVERY 节流。
"""
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta

from module.orchestra.mapping import TASK_SPECS
from module.orchestra.model import OrchestraRow, validate_rows
from module.orchestra.progress import Progress, clear_team_wait

POLL = timedelta(seconds=30)        # 等搭档时多久看一次搭档的进度
FRESH = timedelta(seconds=90)       # 等待记录、心跳多久之内算新鲜；超过就当搭档那边没有动静
LOG_EVERY = timedelta(minutes=5)    # 等待中多久记一行日志

_SILENT = f'搭档 {int(FRESH.total_seconds())} 秒没有动静'


def _fresh(at: datetime | None, now: datetime) -> bool:
    """at 有值，且距 now 不超过 FRESH（含边界）。"""
    return at is not None and now - at <= FRESH


def _last_seen(p: Progress) -> datetime | None:
    """行运行中的最近动静：最近一次心跳；刚开打、还没有心跳时取这一行的开始时间。"""
    return p.heartbeat_at or p.row_started_at


def _title(command: str | None) -> str:
    """任务显示名：映射表里的 title，不在表里时用命令名本身。"""
    spec = TASK_SPECS.get(command)
    return spec.title if spec is not None else str(command)


def _is_team_row(row: OrchestraRow, tasks: frozenset[str]) -> bool:
    """可运行的组队行：任务在 tasks 里，且 validate_rows 对这一行不报错（可运行的判断同 engine.next_row_index）。"""
    return row.task in tasks and not validate_rows([row])


def team_ordinal(rows: list[OrchestraRow], index: int, tasks: frozenset[str]) -> int | None:
    """rows[index] 是可运行的组队行时，返回它是第几个（从 1 起，只数可运行的组队行）；否则返回 None，行号越界也是。

    tasks 是此刻算组队的任务命令名，即 mapping.team_tasks 读当时配置的结果。
    """
    if not 0 <= index < len(rows) or not _is_team_row(rows[index], tasks):
        return None
    return sum(1 for row in rows[:index + 1] if _is_team_row(row, tasks))


def partner_waiting(me: str, partner: Progress | None, ordinal: int, task: str, now: datetime) -> bool:
    """会合规则 a：搭档此刻正在等同一行（组队序号 ordinal、任务 task），等的是我（me 是本实例名）。

    要求全部成立：有等待记录，FRESH 内刷新过，没有暂停，阶段是 ready（续跑时的等待是 interrupted），
    序号和任务都相同，它设的搭档是我。
    """
    return (partner is not None and partner.team_wait_since is not None and _fresh(partner.team_alive_at, now)
            and not partner.paused and partner.phase in ('ready', 'interrupted')
            and partner.team_wait_ordinal == ordinal and partner.team_wait_task == task
            and partner.team_partner == me)


def partner_met(partner: Progress | None, ordinal: int, task: str, since: datetime) -> datetime | None:
    """会合规则 b：搭档最近一次会合是同一行，且不早于 since（我开始等的时间），说明它已按 a 先走了：返回它记下的
    会合时间；否则返回 None。早于 since 的会合是上一遍循环或更早的，不算。"""
    if (partner is None or partner.team_met_at is None or partner.team_met_ordinal != ordinal
            or partner.team_met_task != task or partner.team_met_at < since):
        return None
    return partner.team_met_at


def resume_partner_state(partner: Progress | None, ordinal: int, task: str, now: datetime) -> str:
    """续跑被打断的组队行之前，看搭档在哪，按顺序取第一条成立的：
    1. 读不到搭档的进度：stopped；
    2. 它在跑这一行：最近动静（心跳，还没有心跳时取开始时间）在 FRESH 内为 here，直接续跑；否则为 stopped；
    3. 它也中断在这一行：stopped；
    4. 它正在等这一行（等待记录的序号和任务都相同）：stopped；
    5. 其余：left，它已经离开这一行（在休息、在跑别的行、在等别的组队行……），这一行按已打完记账。
    stopped 时照常等它。"同一行"指任务相同，组队序号也相同；搭档进度里没有序号（升级前写的）时只比任务。
    """
    if partner is None:
        return 'stopped'
    same_row = partner.row_task == task and partner.team_row_ordinal in (ordinal, None)
    if partner.phase == 'row_running' and same_row:
        return 'here' if _fresh(_last_seen(partner), now) else 'stopped'
    if partner.phase == 'interrupted' and same_row:
        return 'stopped'
    if (partner.team_wait_since is not None and partner.team_wait_ordinal == ordinal
            and partner.team_wait_task == task):
        return 'stopped'
    return 'left'


def partner_situation(me: str, partner: Progress | None, ordinal: int, task: str, now: datetime) -> str:
    """面板与通知里的搭档情况，按顺序取第一条成立的。ordinal、task 是我这一行的组队序号和任务，任务写显示名。

    暂停、编排没在跑（idle、done）先于"搭档设的搭档不是我"判断：team_partner 只在搭档的编排进行中才刷新，
    没开始的搭档进度里是旧值或空的。搭档没开始是等不到最常见的原因，不能在面板和通知里说成配错了搭档。
    """
    if partner is None:
        return '读不到搭档的进度'
    if partner.paused:
        return '搭档已暂停'
    if partner.phase in ('idle', 'done'):
        return '搭档的编排没在跑'
    if partner.team_partner != me:
        return '搭档设的搭档不是我'
    if partner.team_wait_since is not None:
        if not _fresh(partner.team_alive_at, now):
            return _SILENT
        if partner.team_wait_ordinal != ordinal:
            return f'搭档在等第 {partner.team_wait_ordinal} 个组队行，我在第 {ordinal} 个'
        if partner.team_wait_task != task:
            return f'搭档这一行是 {_title(partner.team_wait_task)}，我是 {_title(task)}'
        return '搭档也在等'
    if partner.phase == 'row_running':
        if _fresh(_last_seen(partner), now):
            return f'搭档在跑第 {partner.row_index + 1} 行 {_title(partner.row_task)}'
        return _SILENT
    if partner.phase == 'resting':
        return '搭档在休息'
    if partner.phase == 'interrupted':
        return f'搭档第 {partner.row_index + 1} 行中断了'
    return '搭档还没到'


@dataclass(frozen=True)
class Gate:
    """gate 的结果，progress 总是副本。kind 取值：
    go：开打。按 a、b 会合时 met_at 是会合时间，已记进 team_met_*；续跑时搭档还在这一行，met_at 为 None；
    wait：接着等，等待记录已刷新；
    timeout：等满了，等待记录已清空；
    skip：续跑时搭档已离开这一行，进度原样，交给 engine.finish_interrupted_row 记账。
    """
    kind: str
    progress: Progress
    met_at: datetime | None = None


def _record_meeting(p: Progress, met_at: datetime, ordinal: int, task: str) -> None:
    """就地记下与搭档会合（搭档凭它按规则 b 认出我已先走），清掉等待记录。"""
    p.team_met_at, p.team_met_ordinal, p.team_met_task = met_at, ordinal, task
    clear_team_wait(p)


def gate(p: Progress, me: str, partner: Progress | None, ordinal: int, task: str, now: datetime,
         wait_minutes: int, resuming: bool) -> Gate:
    """组队行开打前的一次判断。me 是本实例名；partner 是读到的搭档进度，读不到为 None；ordinal、task 是这一行的
    组队序号和任务；resuming 表示接着同一个中断行续跑（判断同运行时调 row_limits 的前提）。按顺序：
    1. 续跑时先看搭档在哪（resume_partner_state）：还在这一行就直接续跑（go，等待记录清空），已经离开就 skip，
       也停着就往下走，照常等它；
    2. 没有等待记录，或记录的不是这一行（序号或任务不同）：从现在开始等，记下序号和任务。不是续跑时阶段改为 ready，
       休息作废；续跑时阶段留在 interrupted，开打时凭它接着算剩余量；
    3. 刷新 team_alive_at，搭档凭它判断我还在等；
    4. 规则 a 成立：会合，会合时间取两边开始等的时间中较晚的那个；
    5. 规则 b 成立（since 取自己开始等的时间）：会合，会合时间取搭档记下的；
    6. 从开始等算起满 wait_minutes 分钟：timeout，等待记录清空；
    7. 其余：wait。
    插队任务跑完回来时，等待记录还在、开始等的时间不变，插队花掉的时间也算在等待里。
    """
    p = copy.deepcopy(p)
    if resuming:
        state = resume_partner_state(partner, ordinal, task, now)
        if state == 'here':
            clear_team_wait(p)
            return Gate('go', p)
        if state == 'left':
            return Gate('skip', p)
    if p.team_wait_since is None or p.team_wait_ordinal != ordinal or p.team_wait_task != task:
        p.team_wait_since, p.team_wait_ordinal, p.team_wait_task = now, ordinal, task
        if not resuming:
            p.phase = 'ready'
            p.rest_until = None
            p.rest_remaining = None
    p.team_alive_at = now
    if partner_waiting(me, partner, ordinal, task, now):
        met_at = max(p.team_wait_since, partner.team_wait_since)
    else:
        met_at = partner_met(partner, ordinal, task, p.team_wait_since)
    if met_at is not None:
        _record_meeting(p, met_at, ordinal, task)
        return Gate('go', p, met_at)
    # 按秒比，不造 timedelta：手改成极大的分钟数也不会溢出
    if (now - p.team_wait_since).total_seconds() >= wait_minutes * 60:
        clear_team_wait(p)
        return Gate('timeout', p)
    return Gate('wait', p)


def rescue_after_timeout(p: Progress, partner: Progress | None, ordinal: int, task: str,
                         since: datetime) -> Progress | None:
    """等满之后、停下之前再读一次搭档时用。since 是超时前自己开始等的时间（p 里的等待记录在超时时已清空）。

    只看规则 b：搭档在最后一刻已按 a 先走了，就返回记下会合的副本，照常开打；否则返回 None，运行时随后停下。
    规则 a 在这里不算：自己已经不再等了。
    """
    met_at = partner_met(partner, ordinal, task, since)
    if met_at is None:
        return None
    p = copy.deepcopy(p)
    _record_meeting(p, met_at, ordinal, task)
    return p
