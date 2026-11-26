"""编排引擎接入调度循环的运行时：在上游 Script 选任务、运行任务前、运行任务后三处各挂一个钩子。

没有编排在进行时（state 是 idle，阶段是 idle 或 done），pick 原样返回上游 config.get_next() 的结果，
其余两个钩子什么都不做，不改配置、不写进度文件，实例的行为与上游一致。
调用顺序与 engine 模块说明一致：选任务前 recover、sync_controls、decide，选中要等搭档的组队行时再过 team.gate；
行运行前 row_limits、on_row_start；行运行后先还原行参数，再 on_row_end。

进度（Progress）只由本进程写：首次 pick 时读一次放在内存，之后每次变化都存盘。文件写不进去只记警告，
以内存里的为准，下一次存盘时补写。心跳线程与主线程共用 _lock，进度的替换、就地修改和存盘都在锁里。
配置每次现取 script.config：上游会删掉缓存重建 config（ConfigModel 也会被 reload 换掉），不能跨调用持有。

等搭档（会合规则见 team 模块说明）：设了搭档（不是本实例自己）、decide 选中的是组队行时，交给上游之前先读搭档的进度文件
（只读）过一次 team.gate。接着等：交一个 team.POLL 后到点的占位，面板提示写已等多久、搭档在哪，并复位上游设备的卡死计时，
否则上游空等时只截图不点击，连着等下去会被当成卡死而重启游戏。续跑的组队行搭档已离开：这一行按已打完记账，再 decide 一次。
等满了：先存盘清空后的等待记录，再读一次搭档，它刚好先走了就照常开打，否则暂停编排、发通知、抛 SystemExit 让实例退出。
插队放行前撤下等待刷新时间，搭档不再把这边算作在等，开始等的时间不变。本进程第一次读进度时清掉上一个进程留下的等待记录：
实例重启后（点继续也会重启实例）重新计时。没设搭档时这些都不发生；进度里的搭档名只在编排进行中才跟着设置刷新。

钩子不让异常漏进调度循环：pick 出错退回上游的选法，before_run、after_run 出错只记日志。唯一的例外是等搭档超时时
主动抛的 SystemExit：它不是 Exception，pick 的兜底拦不住，交给上游的进程外壳把实例记为警告状态后退出。
行参数是用户配置的原值被临时覆盖，所以出错时宁可不跑编排，也不能让覆盖后的值留在配置里：
before_run 先把原值记进进度并存盘，再把行参数存进配置文件，两次落盘之间被杀，恢复时凭进度把原值写回即可；
启动一行失败就撤回行参数并暂停编排，免得 decide 一直选中它、让它的任务一次次按原生设置跑；
after_run 没走完，进度里仍是 row_running 且带着 saved_params，下一次 pick 补还原。
任务自己的 scheduler.next_run 也算被覆盖的原值：before_run 把它设为现在（凭它判完成），原值一并记进 saved_params。
行完成时任务已把它推到之后，留着任务写的值，其余还原；没完成、启动失败、崩溃或停止遗留时连它一起还原，
否则它停在过去，上游会把它当成到点的待办（在插队名单里的还会被编排放行），按用户的原生设置再跑一轮。
编排进行中（state 是 running 或 paused 且阶段不是 idle、done）上游没有任务可跑，不能让实例因此退出：
改交一个 5 分钟后到点的占位 Function，上游睡到那时再选，配置一变（比如点了继续）就提前醒来。编排停了或跑完了，照上游重抛。
编排进行中每次选任务记一行 INFO（选中哪一行、休息到几点、放行了谁），没有编排的实例一行都不记。
等搭档时不逐次记：开始等记一行，之后每 team.LOG_EVERY 记一行；会合、续跑时怎么处理、超时，各记一行。
"""
import random
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, NoReturn

from module.config.anti_ban import AntiBanGuard
from module.config.config import Function
from module.config.config_model import ConfigModel
from module.config.utils import convert_to_underscore
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.orchestra import team
from module.orchestra.engine import (Decision, TaskRef, decide, finish_interrupted_row, next_row_index, on_row_end,
                                     on_row_start, recover, row_limits, sync_controls)
from module.orchestra.mapping import TASK_SPECS, TaskSpec, apply_row, restore_params, serialize_value, team_tasks
from module.orchestra.model import Orchestra, validate_rows
from module.orchestra.progress import (Progress, adopt_legacy_progress, clear_team_wait, legacy_progress_path,
                                       load_progress, progress_path, save_progress)

_HEARTBEAT_THREAD = 'OrchestraHeartbeat'
_JOIN_TIMEOUT = 5.0   # 等心跳线程退出的秒数上限：它在 Event 上等，被唤醒后至多还剩一次存盘（重试最多约 0.5 秒）
_EMPTY_WAIT = timedelta(minutes=5)   # 编排进行中却没有任务可跑时，占位等待的时长
_WAIT_MARGIN = timedelta(seconds=1)  # 休息等待的占位比等待目标晚这么久到点，见 _pick


def _function(command: str, when: datetime) -> Function:
    """交给调度循环的 Function。上游只看 command 和 next_run，其余字段取常规值。"""
    return Function(convert_to_underscore(command),
                    {'scheduler': {'enable': True, 'next_run': when, 'priority': 5}})


def _next_run_path(spec: TaskSpec) -> str:
    """任务调度时间的字段路径，以 ConfigModel 为根，与 saved_params 的键同一写法。"""
    return f'{spec.model_key}.scheduler.next_run'


@dataclass
class _Row:
    """before_run 启动的一行，after_run 和心跳线程共用。"""
    spec: TaskSpec
    base: int                                  # 本行此前累计的次数，engine 模块说明里 done_count 的约定
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class OrchestraRuntime:
    """一个实例（Script）的编排运行时。script 只需要 config、anti_ban_guard、running_task_obj 这三样属性；
    设备对象 device 已经建好就拿来复位卡死计时，没建不新建。等搭档超时发通知用 config.notifier。"""

    def __init__(self, script, clock: Callable[[], datetime] = datetime.now, rng: random.Random | None = None,
                 heartbeat_interval: float = 30.0) -> None:
        self._script = script
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._interval = max(float(heartbeat_interval), 0.01)
        self._lock = threading.Lock()
        self._p: Progress | None = None       # 进度的权威副本，首次 pick 时从文件读入
        self._path: Path | None = None
        self._dirty = False                   # 上一次存盘失败了，下一次不管有没有变化都补存
        self._decision: Decision | None = None   # 最近一次 pick 的决定，before_run 凭它判断这一轮是不是编排的行
        self._row: _Row | None = None         # before_run 已启动、after_run 还没收尾的行
        self._warned_self_partner = False     # 搭档设成了自己的警告，每个进程只记一次
        self._team_logged_at: datetime | None = None   # 等搭档的日志上一次记在什么时候，按 team.LOG_EVERY 节流

    # ---------- 选任务 ----------

    def pick(self) -> Function:
        """上游 get_next_task 里 config.get_next() 的替身，返回下一个要跑（或要等）的任务。

        passthrough 原样返回上游的结果；上游没有任务可跑（抛 RequestHumanTakeover）时，编排没在进行就照样重抛，
        在进行（暂停也算）就改交一个 5 分钟后到点的占位 Function，不让实例退出。native 返回待办里同名的 Function；
        row 与 wait 另造一个：row 的 next_run 是现在，wait 的 next_run 是休息截止（或更早到点的插队任务）再加 1 秒，
        等搭档时是 team.POLL 之后再加 1 秒，上游发现它没到点就睡到那一刻再重选，占位的任务不会被运行。
        等搭档超时抛出的 SystemExit 不拦，让实例退出（见 _stop_for_partner）。
        上一个任务的对象（script.running_task_obj）在这里放下：心跳只在行运行时读它，空闲等待期间不必再引用着。
        """
        try:
            self._script.running_task_obj = None
            return self._pick()
        except RequestHumanTakeover:
            raise   # 上游没有任务可跑，交还给调用方处理，同没有编排时一样
        except Exception:
            logger.exception('编排选任务出错，本次按上游原逻辑选')
            self._decision = None   # 出错前也许已定下 row，这一轮不是编排的行
            return self._upstream_next()

    def _pick(self) -> Function:
        config = self._script.config
        if not self._load(config):
            return self._upstream_next()
        now = self._clock()
        settings = config.model.orchestra
        self._abandon_row()
        p = self._p

        if p.phase == 'row_running':
            # 进程重启后的第一次选任务，或上一行的 after_run 没走完：此刻没有行在跑，这一行按中断处理
            p = self._restore_originals(config, self._recover(p, settings, now))
        p = self._restore_originals(config, sync_controls(settings, p, now))

        error = None
        try:
            native = config.get_next()
        except RequestHumanTakeover as exc:
            native, error = None, exc   # 没有任务可跑：编排运行中当作空待办，否则交还上游
        pending = [] if error is not None else [TaskRef(f.command, f.next_run) for f in config.pending_task]
        waiting = [] if error is not None else [TaskRef(f.command, f.next_run) for f in config.waiting_task]
        decision = decide(settings, p, pending, waiting, now)
        wait_note = None   # 接着等搭档时面板上的等待提示，见 _team_gate
        ordinal = self._team_ordinal(config, settings, decision)
        if ordinal is not None:   # 放在置 idle 之前：续跑时搭档已离开、记完账序列跑完了，state 照样要写回 idle
            p, decision, wait_note = self._team_gate(config, settings, p, decision, ordinal, pending, waiting, now)

        if decision.kind == 'row':
            self._log_skipped(settings, p, decision.row_index)
        if p.phase == 'done' and settings.state != 'idle':
            self._set_idle(config)
        active = settings.state in ('running', 'paused') or p.phase not in ('idle', 'done')
        if active:
            # 提示和搭档名只在编排进行中才更新，否则原样不动；等搭档时等待提示接在防封禁提示后面
            note = '；'.join(text for text in (self._anti_ban_note(config, now), wait_note) if text)
            p = replace(p, status_note=note, team_partner=settings.partner)
        if decision.kind == 'native' and p.team_wait_since is not None:
            p = replace(p, team_alive_at=None)   # 插队放行：搭档不再把这边算作在等，开始等的时间不动
        self._commit(p)
        self._decision = decision
        if active and wait_note is None:   # 等搭档时由 _team_gate 按 team.LOG_EVERY 节流记日志，不逐次记
            self._log_decision(decision, p)

        if decision.kind == 'passthrough':
            if error is None:
                return native
            if settings.state in ('running', 'paused') and p.phase not in ('idle', 'done'):
                wait = self._wait_function(settings, p, now)   # 编排进行中（比如暂停了）：上游没任务可跑，也不让实例退出
                if wait is not None:
                    return wait
            raise error
        if decision.kind == 'native':
            return self._pending_function(config, decision.command)
        if decision.kind == 'row':
            return _function(decision.command, now)
        # 晚 1 秒到点：上游拿到占位时若已到点（等待目标与上游取的 now 只差几毫秒），会把占位的任务名当成
        # 到点的任务直接运行，before_run 认不出是行，那个任务就按原生设置跑了一轮
        return _function(decision.command or self._placeholder(settings, p), decision.at + _WAIT_MARGIN)

    def _wait_function(self, settings: Orchestra, p: Progress, now: datetime) -> Function | None:
        """上游没有任务可跑、编排又在进行时用：交一个 5 分钟后到点的占位 Function，上游睡到那时再选。
        占位的任务名借 _placeholder 的，它不会被运行。造不出来返回 None，调用方照上游的来。"""
        try:
            return _function(self._placeholder(settings, p), now + _EMPTY_WAIT)
        except Exception as exc:
            logger.warning(f'编排：上游没有任务可跑，也造不出占位等待：{exc!r}，按上游原逻辑处理')
            return None

    def _upstream_next(self) -> Function:
        """退回上游的选法（读进度失败、内部出错时用）：config.get_next() 的结果原样返回。
        它抛 RequestHumanTakeover（没有任务可跑）而编排在进行时，不让实例退出，改交占位等待。
        这里没有可靠的进度可看，只凭 orchestra.state（running 或 paused）判断；读不到 state、造不出占位，就照上游重抛。"""
        config = self._script.config
        try:
            return config.get_next()
        except RequestHumanTakeover:
            wait = None
            try:
                settings = config.model.orchestra
                if settings.state in ('running', 'paused'):
                    wait = self._wait_function(settings, self._p if self._p is not None else Progress(), self._clock())
            except Exception as exc:
                logger.warning(f'编排：读不到 orchestra.state：{exc!r}，按上游原逻辑处理')
            if wait is None:
                raise
            return wait

    def _load(self, config) -> bool:
        """首次调用时读进度文件。新位置还没有、旧位置（log/.orchestra_<实例名>.json）有时，先把旧文件搬过来，
        进行中的序列与没还原的原值都接得上。文件缺失或损坏时 load_progress 给默认进度，不算失败；
        搬或读本身出错（如被占用）返回 False，这一次按上游选，下一次再来。
        读到的等待记录是上一个进程留下的：清掉并存盘，再到组队行时重新计时。否则接着旧的开始时间算，一到就超时；
        暂停后点继续也会重启实例进程，同样从头计时。"""
        if self._p is not None:
            return True
        path = progress_path(config.config_name)
        try:
            if adopt_legacy_progress(config.config_name):
                logger.info(f'编排进度文件已从 {legacy_progress_path(config.config_name)} 搬到 {path}')
            p = load_progress(path)
        except OSError as exc:
            logger.warning(f'编排进度文件读取失败（{path}）：{exc!r}，本次按上游原逻辑选任务，下次再试')
            return False
        stale = clear_team_wait(p)
        self._path, self._p = path, p
        if stale:
            with self._lock:
                self._save_locked()
        return True

    def _abandon_row(self) -> None:
        """选任务时不该有行在跑。有，说明它的 after_run 没走到：停掉心跳，进度里的 row_running 留给调用方按中断处理。"""
        if self._row is not None:
            self._stop_heartbeat(self._row)
            self._row = None

    @staticmethod
    def _recover(p: Progress, settings: Orchestra, now: datetime) -> Progress:
        logger.warning(f'编排：第 {p.row_index + 1} 行没有正常结束（进程中断，或收尾时出错），按中断处理')
        p = recover(p, now)
        if settings.resume_policy == 'rerun':
            # recover 看不到策略。没崩溃时 on_row_end 会清零，崩溃走这里：整行重打，不接着算
            p.row_elapsed_before, p.done_count = 0.0, None
        return p

    def _restore_originals(self, config, p: Progress) -> Progress:
        """进度里留着用户原值、又没有行在跑（崩溃、停止遗留）：写回配置并存盘，再清掉 saved_params。
        存配置失败会抛出，此时 saved_params 保留，下一次选任务重试。"""
        if not p.saved_params or p.phase == 'row_running':
            return p
        self._restore(config, p.saved_params)
        p = replace(p, saved_params={})
        self._commit(p)
        return p

    @staticmethod
    def _restore(config, saved: dict) -> None:
        """把行参数的原值写回配置并存盘。个别字段对不上（映射或进度文件过期）只记错误，其余照常还原。"""
        if not saved:
            return
        try:
            restore_params(config.model, saved)
        except Exception as exc:
            logger.error(f'编排：行参数有字段没能还原，其余已还原：{exc}', exc_info=exc)
        config.save()

    @staticmethod
    def _set_idle(config) -> None:
        """序列跑完：把 state 写回 idle 并存配置。否则下一次 sync_controls 会把还停在 running 的 state 当成新的开始。"""
        config.model.orchestra.state = 'idle'
        config.save()

    @staticmethod
    def _log_decision(decision: Decision, p: Progress) -> None:
        """编排进行中每次选任务记一行：选中哪一行、休息到几点、放行了谁，或者为什么交还上游。"""
        if decision.kind == 'row':
            message = f'选中第 {decision.row_index + 1} 行 {decision.command}'
        elif decision.kind == 'wait':
            message = f'休息中，等到 {decision.at:%H:%M:%S}'
            if decision.command is not None:
                message += f'，届时放行 {decision.command}'
        elif decision.kind == 'native':
            message = f'放行 {decision.command}，按它自己的设置运行'
        else:
            message = '已暂停，按上游原逻辑选任务' if p.paused else '本次不插手，按上游原逻辑选任务'
        logger.info(f'编排：{message}')

    @staticmethod
    def _log_skipped(settings: Orchestra, p: Progress, index: int) -> None:
        """decide 跳过的无效行逐行记一条警告：engine 不记日志。"""
        for skipped in range(p.row_index, index):
            errors = validate_rows([settings.rows[skipped]])
            detail = '；'.join(f'{e.field}：{e.message}' for e in errors)
            logger.warning(f'编排表第 {skipped + 1} 行无效，已跳过（{detail}）')

    def _anti_ban_note(self, config, now: datetime) -> str:
        """防封禁休眠的提示，给运行面板看，只在编排进行中才算（见 _pick）。
        只读休眠窗口与 AntiBanGuard._rest_until，不调用 wake_time（它会改状态）。"""
        anti_ban = config.model.script.anti_ban
        if not anti_ban.enable:
            return ''
        wake = None
        if AntiBanGuard._in_sleep_window(now.time(), anti_ban.sleep_start, anti_ban.sleep_end):
            wake = AntiBanGuard._next_time_point(now, anti_ban.sleep_end)
        rest_until = getattr(getattr(self._script, 'anti_ban_guard', None), '_rest_until', None)
        if rest_until is not None and rest_until > now:
            wake = rest_until if wake is None else max(wake, rest_until)
        return '' if wake is None else f'防封禁休眠中，{wake:%H:%M} 继续'

    @staticmethod
    def _placeholder(settings: Orchestra, p: Progress) -> str:
        """休息截止最先到、没有插队任务可等时，占位 Function 借用的任务名：休息后要跑的那一行的任务。
        通常就是当前行；当前行被手改坏造不出 Function 时，顺延到下一个可运行的行。"""
        index = next_row_index(settings.rows, p.row_index)
        if index is None:
            index = next_row_index(settings.rows, 0)
        if index is None:
            raise ValueError('编排表里没有可运行的行')   # decide 只在表里有可运行行时才会让它休息
        return settings.rows[index].task

    @staticmethod
    def _pending_function(config, command: str) -> Function:
        for task in config.pending_task:
            if task.command == command:
                return task
        raise LookupError(f'待办里没有 {command}')

    # ---------- 等搭档 ----------

    def _team_ordinal(self, config, settings: Orchestra, decision: Decision) -> int | None:
        """decision 选中的行开打前要等搭档时，返回它的组队序号（见 team.team_ordinal）。不用等就返回 None：
        不是行、没设搭档、搭档设成了自己（手改的配置，按没设处理，每个进程只记一次警告），或这一行不是组队行。
        是不是组队行，凭这一刻配置里任务的队长、队员身份判断（mapping.team_tasks）。"""
        if decision.kind != 'row' or not settings.partner:
            return None
        if settings.partner == config.config_name:
            if not self._warned_self_partner:
                self._warned_self_partner = True
                logger.warning(f'编排：搭档设成了自己（{config.config_name}），不等搭档')
            return None
        return team.team_ordinal(settings.rows, decision.row_index, team_tasks(config.model))

    def _team_gate(self, config, settings: Orchestra, p: Progress, decision: Decision, ordinal: int,
                   pending: list[TaskRef], waiting: list[TaskRef],
                   now: datetime) -> tuple[Progress, Decision, str | None]:
        """decision 选中的组队行（组队序号 ordinal）开打前，读一次搭档的进度交给 team.gate，按结果处理。
        返回 (进度, 决定, 等待提示)。等待提示只在接着等搭档时有值，_pick 把它接在防封禁提示后面写进 status_note。

        go：开打，照常返回这一行。
        skip：续跑的组队行搭档已离开，这一行按已打完记账，再 decide 一次。又是组队行就再过一次，这次不是续跑，不会再 skip。
        wait：复位卡死计时，交一个 team.POLL 后到点的 wait。日志按 team.LOG_EVERY 节流。
        timeout：先存盘清空后的进度，再读一次搭档：它在最后一刻按规则 a 先走了就照常开打，否则 _stop_for_partner，不返回。
        """
        partner_name, me, minutes = settings.partner, config.config_name, settings.partner_wait_minutes
        index, command = decision.row_index, decision.command
        resuming = p.phase == 'interrupted' and index == p.row_index and p.row_task == command   # 同 _before_run
        partner = self._read_partner(partner_name)
        result = team.gate(p, me, partner, ordinal, command, now, minutes, resuming)
        q = result.progress

        if result.kind == 'skip':
            q = finish_interrupted_row(q, settings, now, self._rng)
            logger.info(f'编排：搭档已离开第 {ordinal} 个组队行，第 {index + 1} 行按已打完记账')
            again = decide(settings, q, pending, waiting, now)
            again_ordinal = self._team_ordinal(config, settings, again)
            if again_ordinal is None:
                return q, again, None
            return self._team_gate(config, settings, q, again, again_ordinal, pending, waiting, now)

        if result.kind == 'wait':
            waited = (now - q.team_wait_since) // timedelta(minutes=1)
            situation = team.partner_situation(me, partner, ordinal, command, now)
            self._reset_stuck_timer()
            if q.team_wait_since != p.team_wait_since:   # 这一轮才开始等（或改等另一行）
                logger.info(f'编排：第 {index + 1} 行 {command} 等搭档 {partner_name}'
                            f'（第 {ordinal} 个组队行，最多 {minutes} 分钟）')
                self._team_logged_at = now
            elif self._team_logged_at is None or now - self._team_logged_at >= team.LOG_EVERY:
                logger.info(f'编排：等搭档 {partner_name} 已 {waited} 分钟；{situation}')
                self._team_logged_at = now
            note = f'等搭档 {partner_name}：已等 {waited} 分钟，最多 {minutes} 分钟；{situation}'
            return q, Decision('wait', None, at=now + team.POLL), note

        met_at = result.met_at
        if result.kind == 'timeout':
            self._commit(q)   # 先把清空后的等待记录存下：从这一刻起，搭档不会再把这边算作在等
            # 超时前开始等的时间。这一轮才开始等就超时（只有手改成 0 分钟的配置会这样）时，就是现在
            since = p.team_wait_since if p.team_wait_since is not None else now
            partner = self._read_partner(partner_name)
            rescued = team.rescue_after_timeout(q, partner, ordinal, command, since)
            if rescued is None:
                self._stop_for_partner(config, q, team.partner_situation(me, partner, ordinal, command, now),
                                       index, command)
            q, met_at = rescued, rescued.team_met_at
        if met_at is None:   # 续跑时搭档还在这一行
            logger.info(f'编排：搭档还在第 {ordinal} 个组队行，第 {index + 1} 行直接续跑')
        else:
            logger.info(f'编排：与搭档 {partner_name} 会合，第 {index + 1} 行 {command} 开打')
        return q, decision, None

    @staticmethod
    def _read_partner(name: str) -> Progress | None:
        """读搭档的进度文件，只读，那是搭档实例自己写的。文件不存在、读时出 OSError（比如被占用、重试用尽），
        或读出来是默认进度（load_progress 遇到损坏时给的就是它），都返回 None，当作读不到：接着等，不报错。"""
        try:
            partner = load_progress(progress_path(name))
        except OSError:
            return None
        return None if partner == Progress() else partner

    def _reset_stuck_timer(self) -> None:
        """复位上游设备的卡死计时。等搭档时上游每次空等都按"任务队列为空时"的设置处理，回庭院的 GotoMain 只截图不点击，
        连着等下去会被判为卡死，接着报错、重启游戏。设备对象已经建好（Script.device 是 cached_property，建好后在 __dict__
        里）才复位，不为此新建：建设备要连模拟器或游戏窗口。出错只记警告，照常接着等。"""
        try:
            device = self._script.__dict__.get('device')
            if device is not None:
                device.stuck_record_clear()
        except Exception as exc:
            logger.warning(f'编排：复位卡死计时出错：{exc!r}')

    def _stop_for_partner(self, config, p: Progress, situation: str, index: int, command: str) -> NoReturn:
        """等满了搭档也没来：按顺序暂停编排并存配置、面板提示改成超时、发通知、记警告，最后抛 SystemExit 让实例退出。

        SystemExit 不是 Exception，pick 的兜底拦不住，交给上游的进程外壳把实例记为警告状态后退出。
        暂停存不进配置（OSError）、通知发不出，都只记错误，不带调用栈，照样往下做：停下比通知要紧。
        p 是等待记录已清空、已存过盘的进度，其余原样保留，点继续时重新开始等、重新计时。
        situation 是最后一次读到的搭档情况（team.partner_situation），写进通知的第二行。
        """
        settings = config.model.orchestra
        partner_name, minutes = settings.partner, settings.partner_wait_minutes
        try:
            self._pause(config)
        except OSError as exc:
            logger.error(f'编排：等搭档超时，暂停没能存进配置：{exc!r}')
        stopped = f'等搭档 {partner_name} 超时，已暂停'
        self._commit(replace(p, status_note=stopped))
        lines = (f'等搭档 {partner_name} 超过 {minutes} 分钟，已暂停在第 {index + 1} 行（{team.task_title(command)}）',
                 situation,
                 '处理好之后，在编排页点继续接着等；要两边都从头来，先把两个号都点停止，再都点开始')
        try:
            config.notifier.push(title='等搭档超时', content='\n'.join(lines))
        except Exception as exc:
            logger.error(f'编排：等搭档超时的通知没发出：{exc!r}')
        logger.warning(f'编排：等搭档 {partner_name} 超过 {minutes} 分钟，暂停编排并退出实例')
        raise SystemExit(stopped)

    # ---------- 行运行前 ----------

    def before_run(self, command: str) -> None:
        """任务运行前：若这一轮是编排的行，先在进度里记下开始和原值，再把行参数写进任务配置，启动心跳。
        启动失败则撤回并暂停编排（见 _abort_start）。其他情况什么都不做。"""
        try:
            self._before_run(command)
        except Exception:
            logger.exception(f'编排：{command} 运行前处理出错，任务按自己的设置运行')

    def _before_run(self, command: str) -> None:
        decision = self._decision
        if decision is None or decision.kind != 'row' or decision.command != command:
            return   # 编排没插手这一轮，或桌面模式把任务改写成了 Restart
        script = self._script
        config = script.config
        settings = config.model.orchestra
        index = decision.row_index
        if self._p is None or not 0 <= index < len(settings.rows) or settings.rows[index].task != command:
            return   # pick 之后表被改了，那一行已不是当时选中的
        row, p = settings.rows[index], self._p
        written: dict = {}   # 已在内存里改过的字段的原值，启动失败时只撤回这些
        try:
            spec = TASK_SPECS[command]
            # 剩余量只属于被中断的那一行，行号和任务都对得上才沿用：decide 跳过无效行选了别的行，
            # 或表在运行中被改过、同一个行号换了任务，都按该行自己的上限
            resuming = index == p.row_index and p.row_task == command
            count, minutes = (row_limits(row, p, settings.resume_policy) if resuming
                              else (row.max_count, row.max_minutes))
            next_run_path = _next_run_path(spec)
            original_next_run = ConfigModel.deep_get(config.model, next_run_path)
            if not isinstance(original_next_run, datetime):
                raise ValueError(f'无法读取 {next_run_path}')
            written = apply_row(config.model, row, count, minutes)
            saved = {**written, next_run_path: serialize_value(original_next_run)}   # 下面要把 next_run 改成现在
            now = self._clock()
            # 先把原值记进进度并存盘，再把行参数存进配置文件：两次落盘之间被杀，恢复时凭进度把原值写回即可（幂等）。
            # 组队序号不管设没设搭档都记，搭档续跑时凭它认出这边是不是还在同一个组队行
            ordinal = team.team_ordinal(settings.rows, index, team_tasks(config.model))
            started = on_row_start(p, index, now, saved, command, team_ordinal=ordinal)
            self._commit(started)
            if self._dirty:   # 进度没存成：原值只在内存里，不能把行参数写进配置文件，当作启动失败
                raise OSError('进度文件没能存盘，不写行参数')
            # next_run 先设为现在（同 Config.task_call，截到秒）：任务正常结束会把它推到之后，
            # 推不动就说明没走到结束，after_run 凭它判定完成
            if not ConfigModel.deep_set(config.model, next_run_path, now.replace(microsecond=0)):
                raise ValueError(f'无法写入 {next_run_path}')
            written = saved
            config.save()   # 任务内部 task_delay 会 reload，没存盘的行参数会丢
        except Exception as exc:
            self._abort_start(config, p, written, index, command, exc)
            return
        script.running_task_obj = None   # 心跳不能读到上一个任务的计数
        row_run = _Row(spec, base=started.done_count or 0)
        self._row = row_run
        self._start_heartbeat(row_run)
        logger.info(f'编排：第 {index + 1} 行 {command} 开始（次数上限 {count}，分钟上限 {minutes}）')

    def _abort_start(self, config, p: Progress, written: dict, index: int, command: str,
                     exc: Exception) -> None:
        """启动一行失败（写参数、存进度、存配置，哪一步都算）：撤回行参数、记错误、暂停编排。

        不暂停的话，decide 会一直选中这一行，它的任务就一次次按自己的设置原生运行。当前这个任务仍照常跑一次。
        written 是已在内存里改过的字段的原值：行参数，以及已改成现在的 next_run。没改过的不碰，免得撤回一个写不进去的字段时
        白白报错。配置文件在最后一步 config.save() 之前没有写过行参数，所以撤回只改内存，暂停时的 config.save()
        把撤回后的值一并存下。撤回成功且存成了，进度才退回开始之前。否则保持原样：进度里若已有 row_running 与原值，
        下一次 pick 当作中断补还原。撤回与暂停各自出错都只记日志（带调用栈），互不挡着。
        """
        logger.error(f'编排：第 {index + 1} 行 {command} 启动失败：{exc!r}，撤回行参数并暂停编排', exc_info=exc)
        reverted = True
        try:
            if written:
                restore_params(config.model, written)
        except Exception as err:
            reverted = False
            logger.error(f'编排：撤回第 {index + 1} 行的行参数时出错：{err!r}', exc_info=err)
        try:
            self._pause(config)
            if reverted:
                self._commit(p)
        except Exception as err:
            logger.error(f'编排：暂停编排时出错：{err!r}', exc_info=err)

    @staticmethod
    def _pause(config) -> None:
        config.model.orchestra.state = 'paused'
        config.save()

    # ---------- 行运行后 ----------

    def after_run(self, command: str, success: bool) -> None:
        """任务运行后：若 before_run 启动了一行，停心跳、还原行参数、判定完成并推进进度。其他情况什么都不做。"""
        row = self._row
        if row is None:
            return
        try:
            self._after_run(row, bool(success))
        except Exception:
            logger.exception(f'编排：{command} 运行后处理出错，进度留给下一次选任务时恢复')
        finally:
            self._stop_heartbeat(row)
            self._row = None

    def _after_run(self, row: _Row, success: bool) -> None:
        self._stop_heartbeat(row)   # 先停心跳，之后进度只有本线程在动
        config = self._script.config
        config.reload()   # 任务内部 task_delay 改过 next_run 并存了盘，以文件为准
        settings = config.model.orchestra
        with self._lock:
            p = self._p
        next_run_path = _next_run_path(row.spec)
        next_run = ConfigModel.deep_get(config.model, next_run_path)
        # 规格 §8.4：run 返回成功，且任务把 next_run 推到了开始之后，才算这一行完成
        completed = (success and isinstance(next_run, datetime) and p.row_started_at is not None
                     and next_run > p.row_started_at)
        counter = self._counter(row.spec)
        done_count = None if counter is None else row.base + counter   # 读不到就传 None，engine 保留此前的累计
        # 完成了：任务自己推后的 next_run 留着，其余原值还原；没完成：连 next_run 一起还原，免得它停在过去
        originals = p.saved_params if not completed else {
            path: raw for path, raw in p.saved_params.items() if path != next_run_path}
        self._restore(config, originals)
        ended = on_row_end(p, settings, self._clock(), completed, done_count, self._rng)
        if ended.phase == 'done' and settings.state != 'idle':
            self._set_idle(config)
        self._commit(ended)
        logger.info(f'编排：第 {p.row_index + 1} 行 {row.spec.command} {"完成" if completed else "未完成，等待续跑"}')

    # ---------- 进度与心跳 ----------

    def _commit(self, p: Progress) -> None:
        """用 p 顶替内存里的进度；有变化，或上次没存成，就存盘。"""
        with self._lock:
            changed = p != self._p
            self._p = p
            if changed or self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        """存进度文件，调用时要持有 _lock。写不进去只记警告，内存里的进度照用。"""
        try:
            save_progress(self._path, self._p)
            self._dirty = False
        except OSError as exc:
            self._dirty = True
            logger.warning(f'编排进度写入失败（{self._path}）：{exc!r}，沿用内存中的进度，下次再写')

    def _counter(self, spec: TaskSpec) -> int | None:
        """任务对象上"已打次数"的当前值；任务没有可靠的计数属性、对象还没建或值不是整数，返回 None。"""
        if spec.counter_attr is None:
            return None
        value = getattr(getattr(self._script, 'running_task_obj', None), spec.counter_attr, None)
        return value if type(value) is int else None

    def _start_heartbeat(self, row: _Row) -> None:
        thread = threading.Thread(target=self._heartbeat, args=(row,), name=_HEARTBEAT_THREAD, daemon=True)
        thread.start()
        row.thread = thread   # 线程没起来（start 抛异常）就不挂上去，_stop_heartbeat 不会去 join 一个没启动的线程

    def _heartbeat(self, row: _Row) -> None:
        """每个心跳间隔写一次 heartbeat_at 和累计次数，直到 row.stop 被设置。任何异常都只记日志，线程不退出。"""
        while not row.stop.wait(self._interval):
            try:
                self._beat(row)
            except Exception:
                logger.exception('编排心跳出错，下个周期再写')

    def _beat(self, row: _Row) -> None:
        with self._lock:
            if row.stop.is_set():   # after_run 已在收尾，进度归它改，不能再碰
                return
            p = self._p
            p.heartbeat_at = self._clock()
            counter = self._counter(row.spec)
            if counter is not None:
                p.done_count = row.base + counter
            self._save_locked()

    @staticmethod
    def _stop_heartbeat(row: _Row) -> None:
        row.stop.set()
        thread = row.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT)


def runtime_of(script) -> OrchestraRuntime:
    """script 上的运行时，第一次用到时才建。挂在 __dict__ 里，不经过 Script 的属性查找。"""
    runtime = script.__dict__.get('_orchestra')
    if runtime is None:
        runtime = script.__dict__['_orchestra'] = OrchestraRuntime(script)
    return runtime


def orchestra_pick(script) -> Function:
    return runtime_of(script).pick()


def orchestra_before_run(script, command: str) -> None:
    runtime_of(script).before_run(command)


def orchestra_after_run(script, command: str, success: bool) -> None:
    runtime_of(script).after_run(command, success)
