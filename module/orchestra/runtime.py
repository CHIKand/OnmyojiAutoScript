"""编排引擎接入调度循环的运行时：在上游 Script 选任务、运行任务前、运行任务后三处各挂一个钩子。

没有编排在进行时（state 是 idle，阶段是 idle 或 done），pick 原样返回上游 config.get_next() 的结果，
其余两个钩子什么都不做，不改配置、不写进度文件，实例的行为与上游一致。
调用顺序与 engine 模块说明一致：选任务前 recover、sync_controls、decide；行运行前 row_limits、on_row_start；
行运行后先还原行参数，再 on_row_end。

进度（Progress）只由本进程写：首次 pick 时读一次放在内存，之后每次变化都存盘。文件写不进去只记警告，
以内存里的为准，下一次存盘时补写。心跳线程与主线程共用 _lock，进度的替换、就地修改和存盘都在锁里。
配置每次现取 script.config：上游会删掉缓存重建 config（ConfigModel 也会被 reload 换掉），不能跨调用持有。

钩子不让异常漏进调度循环：pick 出错退回上游的选法，before_run、after_run 出错只记日志。
行参数是用户配置的原值被临时覆盖，所以出错时宁可不跑编排，也不能让覆盖后的值留在配置里：
before_run 先把原值记进进度并存盘，再把行参数存进配置文件，两次落盘之间被杀，恢复时凭进度把原值写回即可；
启动一行失败就撤回行参数并暂停编排，免得 decide 一直选中它、让它的任务一次次按原生设置跑；
after_run 没走完，进度里仍是 row_running 且带着 saved_params，下一次 pick 补还原。
编排进行中（state 是 running 或 paused 且阶段不是 idle、done）上游没有任务可跑，不能让实例因此退出：
改交一个 5 分钟后到点的占位 Function，上游睡到那时再选，配置一变（比如点了继续）就提前醒来。编排停了或跑完了，照上游重抛。
"""
import random
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from module.config.anti_ban import AntiBanGuard
from module.config.config import Function
from module.config.config_model import ConfigModel
from module.config.utils import convert_to_underscore
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.orchestra.engine import (Decision, TaskRef, decide, next_row_index, on_row_end, on_row_start, recover,
                                     row_limits, sync_controls)
from module.orchestra.mapping import TASK_SPECS, TaskSpec, apply_row, restore_params
from module.orchestra.model import Orchestra, validate_rows
from module.orchestra.progress import Progress, load_progress, progress_path, save_progress

_HEARTBEAT_THREAD = 'OrchestraHeartbeat'
_JOIN_TIMEOUT = 5.0   # 等心跳线程退出的秒数上限：它在 Event 上等，被唤醒后至多还剩一次存盘（重试最多约 0.5 秒）
_EMPTY_WAIT = timedelta(minutes=5)   # 编排进行中却没有任务可跑时，占位等待的时长


def _function(command: str, when: datetime) -> Function:
    """交给调度循环的 Function。上游只看 command 和 next_run，其余字段取常规值。"""
    return Function(convert_to_underscore(command),
                    {'scheduler': {'enable': True, 'next_run': when, 'priority': 5}})


@dataclass
class _Row:
    """before_run 启动的一行，after_run 和心跳线程共用。"""
    spec: TaskSpec
    base: int                                  # 本行此前累计的次数，engine 模块说明里 done_count 的约定
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class OrchestraRuntime:
    """一个实例（Script）的编排运行时。script 只需要 config、anti_ban_guard、running_task_obj 这三样属性。"""

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

    # ---------- 选任务 ----------

    def pick(self) -> Function:
        """上游 get_next_task 里 config.get_next() 的替身，返回下一个要跑（或要等）的任务。

        passthrough 原样返回上游的结果；上游没有任务可跑（抛 RequestHumanTakeover）时，编排没在进行就照样重抛，
        在进行（暂停也算）就改交一个 5 分钟后到点的占位 Function，不让实例退出。native 返回待办里同名的 Function；
        row 与 wait 另造一个：row 的 next_run 是现在，wait 的 next_run 是休息截止（或更早到点的插队任务），
        上游发现它没到点就睡到那一刻再重选，占位的任务不会被运行。
        """
        try:
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

        if decision.kind == 'row':
            self._log_skipped(settings, p, decision.row_index)
        if p.phase == 'done' and settings.state != 'idle':
            self._set_idle(config)
        if settings.state in ('running', 'paused') or p.phase not in ('idle', 'done'):
            p = replace(p, status_note=self._anti_ban_note(config, now))   # 提示只在编排进行中才更新，否则原样不动
        self._commit(p)
        self._decision = decision

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
        return _function(decision.command or self._placeholder(settings, p), decision.at)

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
        """首次调用时读进度文件。文件缺失或损坏时 load_progress 给默认进度，不算失败；
        读文件本身出错（如被占用）返回 False，这一次按上游选，下一次再读。"""
        if self._p is not None:
            return True
        path = progress_path(config.config_name)
        try:
            p = load_progress(path)
        except OSError as exc:
            logger.warning(f'编排进度文件读取失败（{path}）：{exc!r}，本次按上游原逻辑选任务，下次再试')
            return False
        self._path, self._p = path, p
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
            logger.error(f'编排：行参数有字段没能还原，其余已还原：{exc}')
        config.save()

    @staticmethod
    def _set_idle(config) -> None:
        """序列跑完：把 state 写回 idle 并存配置。否则下一次 sync_controls 会把还停在 running 的 state 当成新的开始。"""
        config.model.orchestra.state = 'idle'
        config.save()

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
        saved = None
        try:
            spec = TASK_SPECS[command]
            # 剩余量只属于被中断的那一行；decide 跳过无效行选了别的行，它不沿用
            count, minutes = (row_limits(row, p, settings.resume_policy) if index == p.row_index
                              else (row.max_count, row.max_minutes))
            saved = apply_row(config.model, row, count, minutes)
            now = self._clock()
            # 先把原值记进进度并存盘，再把行参数存进配置文件：两次落盘之间被杀，恢复时凭进度把原值写回即可（幂等）
            started = on_row_start(p, index, now, saved)
            self._commit(started)
            if self._dirty:   # 进度没存成：原值只在内存里，不能把行参数写进配置文件，当作启动失败
                raise OSError('进度文件没能存盘，不写行参数')
            # next_run 先设为现在（同 Config.task_call，截到秒）：任务正常结束会把它推到之后，
            # 推不动就说明没走到结束，after_run 凭它判定完成
            next_run_path = f'{spec.model_key}.scheduler.next_run'
            if not ConfigModel.deep_set(config.model, next_run_path, now.replace(microsecond=0)):
                raise ValueError(f'无法写入 {next_run_path}')
            config.save()   # 任务内部 task_delay 会 reload，没存盘的行参数会丢
        except Exception as exc:
            self._abort_start(config, p, saved, index, command, exc)
            return
        script.running_task_obj = None   # 心跳不能读到上一个任务的计数
        row_run = _Row(spec, base=started.done_count or 0)
        self._row = row_run
        self._start_heartbeat(row_run)
        logger.info(f'编排：第 {index + 1} 行 {command} 开始（次数上限 {count}，分钟上限 {minutes}）')

    def _abort_start(self, config, p: Progress, saved: dict | None, index: int, command: str,
                     exc: Exception) -> None:
        """启动一行失败（写参数、存进度、存配置，哪一步都算）：撤回行参数、记错误、暂停编排。

        不暂停的话，decide 会一直选中这一行，它的任务就一次次按自己的设置原生运行。当前这个任务仍照常跑一次。
        配置文件在最后一步 config.save() 之前没有写过行参数，所以撤回只改内存，暂停时的 config.save() 把撤回后的值一并存下；
        撤回成功且存成了，进度才退回开始之前。否则保持原样：进度里若已有 row_running 与原值，下一次 pick 当作中断补还原。
        撤回与暂停各自出错都只记日志，互不挡着。
        """
        logger.error(f'编排：第 {index + 1} 行 {command} 启动失败：{exc!r}，撤回行参数并暂停编排')
        reverted = True
        try:
            if saved:
                restore_params(config.model, saved)
        except Exception as err:
            reverted = False
            logger.error(f'编排：撤回第 {index + 1} 行的行参数时出错：{err!r}')
        try:
            self._pause(config)
            if reverted:
                self._commit(p)
        except Exception as err:
            logger.error(f'编排：暂停编排时出错：{err!r}')

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
        next_run = ConfigModel.deep_get(config.model, f'{row.spec.model_key}.scheduler.next_run')
        # 规格 §8.4：run 返回成功，且任务把 next_run 推到了开始之后，才算这一行完成
        completed = (success and isinstance(next_run, datetime) and p.row_started_at is not None
                     and next_run > p.row_started_at)
        counter = self._counter(row.spec)
        done_count = None if counter is None else row.base + counter   # 读不到就传 None，engine 保留此前的累计
        self._restore(config, p.saved_params)
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
