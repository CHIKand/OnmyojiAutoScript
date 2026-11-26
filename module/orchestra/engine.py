"""编排引擎的纯决策层：凭配置、进度、待办和当前时间，算出下一步做什么，以及一行结束后进度变成什么样。

纯，是指：不读写文件、不读时钟（时间只来自 now 参数）、不用全局随机（随机只来自 rng 参数）、不写日志。
返回 Progress 的函数都在 copy.deepcopy 出来的副本上改，不动传进来的对象，调用方拿返回值和旧进度比一下，
就知道有没有变化、要不要存盘。decide 每次选任务都会被调用，被跳过的无效行由运行时去记日志，免得刷屏。

运行时在调度循环里的调用顺序：
    选任务前：本进程第一次先 recover，再 sync_controls，最后 decide；选中的是组队行、又设了搭档时先过 team.gate，
        续跑时搭档已离开（skip）就用 finish_interrupted_row 把这一行按已打完记账，再 decide 一次；
    行运行前：row_limits 求本次上限，on_row_start 记下开始、这一行的任务和组队序号，清掉等待记录；
    行运行后：先还原行参数，再 on_row_end。
row_limits 按剩余量算的前提是接着跑同一个中断行：只有 decide 选中的行号等于进度里的 row_index、
该行的任务也等于进度里的 row_task 时，运行时才用它，否则用该行自己的上限。on_row_start 判断续跑用的是同一个条件。
saved_params（被行参数覆盖前的用户原值）由 on_row_start 写入，on_row_end 清空；recover、sync_controls 的
开始与停止原样保留它，运行时还原参数之后要自己清掉。阶段变为 done 时，运行时要把配置里的 state 写回 idle，
否则下一次 sync_controls 会把还在 running 的 state 当成新的开始。

开始有两个来源：配置里来了一条新的 start 命令（command 为 start，command_seq 大于已处理的），不论处在哪个阶段
都从头开始；或者 state 是 running 而阶段是 idle、done。前者让"停止之后马上开始"在实例读到配置之前也不会被吞掉：
光看 state，两次点击之后它还是 running。
Restart 和 SoulsTidy（上游的重启与御魂溢出自愈，ALWAYS_THROUGH，按此优先级）编排进行中永远放行，
插队名单里的任务次之。
status_note 只由运行时写，引擎在序列跑完（done）和停止时把它清空，面板上不会留着上一次的提示。
这两处也清掉等待记录（会合记录不动）：停了或跑完的实例，不会被搭档当成还在等。

done_count 的约定：它是"本行累计已打次数"，跨中断累加。on_row_start 接着同一个中断行续跑时保留此前的累计，
所以运行时在 on_row_start 之后读到的 (p.done_count or 0) 就是这一段的底数；心跳写进进度的值、传给 on_row_end
的值，都应是 底数 + 任务这一段自己的计数（读不到计数就传 None，on_row_end 会保留此前的累计）。
不这样做，第二次中断会把次数清零，补跑时就会超出次数上限。
"""
import copy
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from module.orchestra.model import Orchestra, OrchestraRow, validate_rows
from module.orchestra.progress import HISTORY_LIMIT, Progress, clear_team_wait

ALWAYS_THROUGH = ('Restart', 'SoulsTidy')   # 上游的重启与御魂溢出自愈，永远放行，按此优先级
_LIVE_PHASES = ('ready', 'resting', 'interrupted')   # 序列还在进行、此刻没有行在跑的阶段
_REST_MINUTES_MAX = 1439   # 休息分钟的上限，与保存校验一致；手改的配置也不会让 timedelta 溢出


@dataclass(frozen=True)
class TaskRef:
    """调度器里一个任务的快照：命令名和下次运行时间。"""
    command: str
    next_run: datetime


@dataclass(frozen=True)
class Decision:
    """下一步做什么。kind 取值：
    passthrough：编排不插手，按上游原逻辑选任务；
    native：放行 command（ALWAYS_THROUGH 或插队名单里的任务），按它原生的参数运行，不写行参数；
    row：运行表里第 row_index 行，command 是该行的任务；
    wait：休息中，等到 at 再选；command 是 at 那一刻到点的插队任务或 ALWAYS_THROUGH 里的任务，at 就是休息截止时为 None。
    """
    kind: str
    command: str | None = None
    at: datetime | None = None
    row_index: int | None = None


def _state(settings: Orchestra) -> str:
    """running / paused / idle。orchestra 是宽松配置，别的取值一律按 idle，手改坏了也不会误启动。"""
    return settings.state if settings.state in ('running', 'paused') else 'idle'


def next_row_index(rows: list[OrchestraRow], start: int) -> int | None:
    """从 start 起第一个可运行行的行号，没有则 None。validate_rows 对这一行不报错才算可运行，手改坏的行因此被跳过。"""
    for index in range(max(start, 0), len(rows)):
        if not validate_rows([rows[index]]):
            return index
    return None


def _finish(p: Progress) -> None:
    """就地把序列标为跑完：没有休息，也没有等着的行，不再暂停，提示和等待记录清空（会合记录不动）。"""
    p.phase = 'done'
    p.rest_until = None
    p.rest_remaining = None
    p.paused = False
    p.status_note = ''
    clear_team_wait(p)


def sync_controls(settings: Orchestra, p: Progress, now: datetime) -> Progress:
    """把配置里的运行控制字段（state、command、command_seq）同步到进度，依次处理开始、暂停、继续、停止、命令、改表。

    开始：新的 start 命令不论阶段，或 state 为 running 而阶段为 idle、done。进度全部重置，只留下 saved_params
    （可能是崩溃遗留的、还没还原的用户原值）；命令序号对齐配置，开始之前发出的跳过休息不会在这次编排里重放。
    开始先于暂停、停止处理：点了开始又点暂停（或停止），实例读到配置时，结果是从头开始并暂停（或停下）。
    停止：阶段回到 idle，休息作废，提示和等待记录清空（会合记录不动），saved_params 留给运行时还原。
    命令：序号大于已处理的才生效，不管认不认识都记为已处理；跳过休息只在休息中生效，暂停时改写剩余秒数，继续后才起作用。
    改表：运行中把表改短或改坏，当前序号之后已没有可运行的行时，视为这一遍跑完，之后按"跑完后"设置处理：
    循环则回到第一个可运行行并计一遍，否则结束。那一行已不在表里，它累计的用时、次数和任务名一并丢弃。
    """
    p = copy.deepcopy(p)
    state = _state(settings)

    start_command = settings.command == 'start' and settings.command_seq > p.last_command_seq
    if start_command or (state == 'running' and p.phase in ('idle', 'done')):
        p = Progress(phase='ready', sequence_started_at=now, row_index=0,
                     last_command_seq=settings.command_seq, saved_params=p.saved_params)

    if state == 'paused' and not p.paused:
        if p.phase == 'resting' and p.rest_until is not None:
            p.rest_remaining = max(0.0, (p.rest_until - now).total_seconds())
        p.paused = True
    elif state == 'running' and p.paused:
        if p.rest_remaining is not None:
            p.rest_until = now + timedelta(seconds=p.rest_remaining)
        p.rest_remaining = None
        p.paused = False

    if state == 'idle' and p.phase not in ('idle', 'done'):
        p.phase = 'idle'
        p.paused = False
        p.rest_until = None
        p.rest_remaining = None
        p.status_note = ''
        clear_team_wait(p)

    if settings.command_seq > p.last_command_seq:
        if settings.command == 'skip_rest' and p.phase == 'resting':
            p.rest_until = now
            if p.paused:
                p.rest_remaining = 0.0
        p.last_command_seq = settings.command_seq

    if (state == 'running' and not p.paused and p.phase in _LIVE_PHASES
            and next_row_index(settings.rows, p.row_index) is None):
        p.row_elapsed_before = 0.0
        p.done_count = None
        p.row_task = None
        first = next_row_index(settings.rows, 0) if settings.after_finish == 'loop' else None
        if first is None:
            _finish(p)
        else:
            p.row_index = first
            p.loop_count += 1
            if p.phase == 'interrupted':
                p.phase = 'ready'   # 被打断的那一行没了，没有可续跑的
    return p


def _wait_decision(settings: Orchestra, p: Progress, waiting: list[TaskRef], now: datetime) -> Decision | None:
    """休息中就返回 wait，不在休息中（或休息已到点）返回 None。

    目标取休息截止和等待任务里最早的 next_run。等待任务只算插队名单里的和 ALWAYS_THROUGH 里的，后者永远放行，
    不能睡过它；平手取休息截止（command 为 None）。
    """
    if p.phase != 'resting' or p.rest_until is None or p.rest_until <= now:
        return None
    target, command = p.rest_until, None
    for task in waiting:
        if (task.command in settings.cut_in_tasks or task.command in ALWAYS_THROUGH) and task.next_run < target:
            target, command = task.next_run, task.command
    return Decision('wait', command, at=target)


def decide(settings: Orchestra, p: Progress, pending: list[TaskRef], waiting: list[TaskRef],
           now: datetime) -> Decision:
    """按顺序判断，命中即返回：
    1. 编排没在跑（状态不是 running、已暂停、序列未开始或已结束、表是空的）：passthrough；
    2. 待办里有 ALWAYS_THROUGH 的任务：native，Restart 先于 SoulsTidy，与待办里的顺序无关；
    3. 待办里按顺序第一个插队名单里的任务：native；
    4. 休息中：wait；
    5. 当前序号起已没有可运行的行：passthrough，由 sync_controls 把阶段置为 done；
    6. 其余：当前序号起第一个可运行的行。
    """
    if _state(settings) != 'running' or p.paused or p.phase in ('idle', 'done') or not settings.rows:
        return Decision('passthrough')
    commands = {task.command for task in pending}
    for command in ALWAYS_THROUGH:
        if command in commands:
            return Decision('native', command)
    for task in pending:
        if task.command in settings.cut_in_tasks:
            return Decision('native', task.command)
    wait = _wait_decision(settings, p, waiting, now)
    if wait is not None:
        return wait
    index = next_row_index(settings.rows, p.row_index)
    if index is None:
        return Decision('passthrough')
    return Decision('row', settings.rows[index].task, row_index=index)


def row_limits(row: OrchestraRow, p: Progress, policy: str) -> tuple[int | None, int | None]:
    """这一行本次该写进任务的 (次数上限, 分钟上限)，None 表示该行没填，沿用任务自己的设置。

    只有行在中断后续跑（阶段为 interrupted）且策略不是 rerun 时才按剩余量算，否则原样返回：
    分钟 = 上限减已用，向上取整，至少 1；次数 = 上限减已打，至少 1，已打次数未知（None）时按原上限。
    取值不是 rerun 的策略一律按 remaining，同 _state 的道理。
    它只看阶段，认不出是不是同一行：调用方要先核对行号和任务都与进度里的一致，见文件开头。
    """
    count, minutes = row.max_count, row.max_minutes
    if p.phase != 'interrupted' or policy == 'rerun':
        return count, minutes
    if minutes is not None:
        minutes = max(1, math.ceil(minutes - p.row_elapsed_before / 60))
    if count is not None and p.done_count is not None:
        count = max(1, count - p.done_count)
    return count, minutes


def on_row_start(p: Progress, row_index: int, now: datetime, saved_params: dict,
                 task: str | None = None, team_ordinal: int | None = None) -> Progress:
    """记下第 row_index 行开始运行。saved_params 是写入行参数前的用户原值，task 是这一行的任务，
    team_ordinal 是这一行的组队序号（见 team.team_ordinal，不是组队行为 None），记进 team_row_ordinal，
    搭档续跑时凭它认出我是不是还在同一个组队行。

    接着同一个中断行续跑（阶段为 interrupted，行号相同，任务也相同）时，保留累计的用时和次数；
    其余情况都是新的一行，从零算：表在运行中被改过、同一个行号换成了别的任务，也是新的一行。
    开打就不再等：等待记录清掉，会合记录不动。组队行在会合时已经清过；等着的时候表被改了、改跑别的行时靠这里清，
    否则之后同序号同任务的组队行会接着旧的开始时间算，一到就超时。
    """
    p = copy.deepcopy(p)
    if not (p.phase == 'interrupted' and p.row_index == row_index and p.row_task == task):
        p.row_elapsed_before = 0.0
        p.done_count = None
    p.row_index = row_index
    p.row_task = task
    p.team_row_ordinal = team_ordinal
    clear_team_wait(p)
    p.phase = 'row_running'
    p.row_started_at = now
    p.heartbeat_at = None
    p.rest_until = None
    p.rest_remaining = None
    p.saved_params = copy.deepcopy(saved_params)
    return p


def _rest_seconds(settings: Orchestra, index: int, rng: random.Random) -> float:
    """index 行之后休息多少秒：该行自己的休息分钟，没填用默认，夹在 0–1439 之间；开了随机就乘以 0.8–1.2。"""
    row = settings.rows[index] if 0 <= index < len(settings.rows) else None   # 行在运行中被改表删掉了
    minutes = row.rest_minutes if row is not None and row.rest_minutes is not None else settings.default_rest_minutes
    seconds = min(max(minutes, 0), _REST_MINUTES_MAX) * 60.0
    if settings.rest_jitter:
        seconds *= rng.uniform(0.8, 1.2)
    return seconds


def on_row_end(p: Progress, settings: Orchestra, now: datetime, completed: bool, done_count: int | None,
               rng: random.Random) -> Progress:
    """记下当前行的结果，并定下一步。运行时要先还原行参数再调用，所以 saved_params 在这里清空。

    没完成：阶段为 interrupted，本次用时累加进 row_elapsed_before，done_count 不为 None 就记下，None 则保留此前的
    累计；策略为 rerun 时两项清零，下次整行重打。row_task、team_row_ordinal 保留，续跑时凭它们认行。
    完成：用时（含此前各次中断累计的）计入总时长，写一条历史（任务取 row_task，即实际跑的那个；done_count 取传入值，
    None 表示未知），然后找下一个可运行的行：已无下一行时，循环则回到第一个可运行行并计一遍，否则结束；
    总时长到达上限也结束，不会打断行，只在行结束时判断。没结束就按该行的设置休息；休息为 0 则直接 ready。
    行运行期间被暂停时，休息的全长记进 rest_remaining，继续时从头休满。
    """
    p = copy.deepcopy(p)
    run = 0.0 if p.row_started_at is None else max(0.0, (now - p.row_started_at).total_seconds())
    p.saved_params = {}
    p.heartbeat_at = None

    if not completed:
        p.phase = 'interrupted'
        p.row_elapsed_before += run
        if done_count is not None:
            p.done_count = done_count
        if settings.resume_policy == 'rerun':
            p.row_elapsed_before = 0.0
            p.done_count = None
        return p

    _complete_row(p, settings, now, run, done_count, rng)
    return p


def finish_interrupted_row(p: Progress, settings: Orchestra, now: datetime, rng: random.Random) -> Progress:
    """把阶段为 interrupted 的当前行按已打完记账：续跑的组队行发现搭档已经离开时用（team.gate 返回 skip）。

    不再加新的运行时间：总时长只加此前各次中断累计的 row_elapsed_before。历史的开始时间取原来的 row_started_at
    （没有时为 None），次数取进度里的累计 done_count。之后找下一行、循环、总时长上限、休息，以及清空这一行的
    运行状态（saved_params、heartbeat_at、累计用时与次数、row_task、team_row_ordinal），都与 on_row_end 的完成分支相同。
    续跑时可能已经等过几轮，等待记录也清掉，会合记录不动，理由同 on_row_start。
    """
    p = copy.deepcopy(p)
    p.saved_params = {}
    p.heartbeat_at = None
    clear_team_wait(p)
    _complete_row(p, settings, now, 0.0, p.done_count, rng)
    return p


def _complete_row(p: Progress, settings: Orchestra, now: datetime, run: float, done_count: int | None,
                  rng: random.Random) -> None:
    """就地把当前行记为完成：本次用时 run 连同此前累计的计入总时长，写一条历史，清掉这一行的运行状态，
    再定下一步。规则见 on_row_end 的完成分支，finish_interrupted_row 共用。"""
    index = p.row_index
    p.total_elapsed += p.row_elapsed_before + run
    p.history = (p.history + [{
        'row_index': index,
        'task': p.row_task,
        'started_at': None if p.row_started_at is None else p.row_started_at.isoformat(),
        'ended_at': now.isoformat(),
        'done_count': done_count,
        'loop_count': p.loop_count,
    }])[-HISTORY_LIMIT:]
    p.row_elapsed_before = 0.0
    p.done_count = None
    p.row_task = None
    p.team_row_ordinal = None

    cap = settings.total_minutes_cap
    following = None
    if cap is None or p.total_elapsed < cap * 60:
        following = next_row_index(settings.rows, index + 1)
        if following is None and settings.after_finish == 'loop':
            following = next_row_index(settings.rows, 0)
            if following is not None:
                p.loop_count += 1
    if following is None:
        _finish(p)
        return

    p.row_index = following
    seconds = _rest_seconds(settings, index, rng)
    if seconds <= 0:
        p.phase = 'ready'
        p.rest_until = None
    else:
        p.phase = 'resting'
        p.rest_until = now + timedelta(seconds=seconds)
        if p.paused:
            p.rest_remaining = seconds


def recover(p: Progress, now: datetime) -> Progress:
    """进程重启后的第一件事：阶段还停在 row_running，说明上次是中断的，改为 interrupted。

    已用时间按最后一次心跳算，没有心跳就算 0，不用 now：停机的那几个小时不是这一行的用时。
    saved_params、done_count、row_task 原样保留：第一个留给运行时还原，第二个是心跳最后写下的累计，
    第三个用来认出续跑的是不是同一行。其他阶段原样返回。now 只为与其他函数保持一致的签名，不参与计算。
    """
    p = copy.deepcopy(p)
    if p.phase == 'row_running':
        if p.row_started_at is not None:
            last_seen = p.heartbeat_at or p.row_started_at
            p.row_elapsed_before += max(0.0, (last_seen - p.row_started_at).total_seconds())
        p.phase = 'interrupted'
    return p
