"""本分支的后台任务线程：可选的每日定时合并上游，以及别的里程碑注册进来的周期任务。

app.py 的 on_startup 经 custom_routes 调 start_custom_jobs()，起一个名为 custom-jobs 的守护线程。线程每 TICK_SECONDS
秒醒来一次：先看每日定时合并该不该跑，再按间隔依次跑已注册的任务（register_job）。start_custom_jobs 起线程之前登记
飞书补发（feishu_flush，每 60 秒调一次 feishu_app.flush_outbox）。

每日定时合并：.env 的 OAS_UPSTREAM_MERGE_TIME（HH:MM；缺省或留空就是关闭，不合法的值也按关闭处理，并给每个取值记一次日志），
每次检查都重新读，改了不用重启。当天已过这个时刻、当天的合并还没用掉，就让服务的事件循环跑 fork_updater.run_update_result：
与更新页的按钮同一条路径（同一把锁，代码变了同样重启实例，同样的文案），线程在这里等它做完，把文案记进日志（只在这里记一次）。
时刻错过了（电脑休眠，或服务当时没开）不要紧：之后第一次检查发现"过了时刻、今天的还没用掉"就补跑，线程一启动就先查一次。
结果是代码冲突中止（conflict_aborted）或推送失败（push_failed）时，再把这条文案发一条飞书通知（feishu_app.notify_server_event，
发给各实例配置里开着飞书推送的接收方）；更新页的按钮不发。

一天只合并一次：每次试之前先把今天记成用掉了，不论结果好坏。唯一的例外是拉取失败（fetch_failed，比如电脑刚醒、网络还没通）：
拿到这个结果后把今天退回去，RETRY_DELAY 之后再试，一天最多试 MAX_ATTEMPTS 次，最后一次也失败就用掉今天。出错、等不到结果、
进程被杀都不退回，所以不会反复合并。用掉的日期、今天试了几次、上一次的时刻记在 log/custom_jobs/state.json，同时留在内存里：
状态文件写不进去，也不会每分钟合并一次，最坏是服务重启后按文件里的旧记录再试。想再合并就用更新页的按钮。状态文件放在子目录里：
上游每次启动会删掉 log/ 根目录下超过 14 天的文件，子目录里只清 log/error。

线程不会死：每日合并的检查、每个任务、每一轮检查各有自己的 try/except，出错记日志（带调用栈）后照常进入下一轮。
线程用 Event.wait 等下一轮，不用 time.sleep，stop() 能立刻叫醒它。模块导入时什么都不做（不起线程、不建文件）：
fork_updater 的导入自检会导入 custom_routes，进而导入本模块。
"""
import asyncio
import concurrent.futures
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from module.config.atomicwrites import atomic_write
from module.config.dotenv import read_env
from module.logger import logger
from module.server import fork_updater
from module.server.fork_git import MergeResult

THREAD_NAME = 'custom-jobs'
TICK_SECONDS = 60                      # 线程每隔这么久醒来检查一次；间隔再短的任务，实际也是每轮最多跑一次
MERGE_TIMEOUT = 1800                   # 秒；定时合并等结果的上限（拉取和推送各有 300 秒超时，导入自检 120 秒，正常远用不到）
DUE_SLACK = 1.0                        # 秒；任务的间隔差不超过它就算到期。线程的等待有计时器误差（Windows 上 monotonic
                                       # 的分辨率约 15 毫秒，Event.wait 可能早醒），间隔恰好等于 TICK_SECONDS 的任务
                                       # （M4 的飞书补发是 60 秒）不留余量，就会隔一轮才跑
RETRY_DELAY = timedelta(minutes=10)    # 拉取失败后隔多久再试
MAX_ATTEMPTS = 6                       # 每天最多试几次（只有拉取失败才会再试）
MERGE_TIME_KEY = 'OAS_UPSTREAM_MERGE_TIME'
STATE_KEY = 'upstream_merge_last_run'
ATTEMPTS_KEY = 'upstream_merge_attempts'
LAST_ATTEMPT_KEY = 'upstream_merge_last_attempt'
FEISHU_MERGE_TITLE = 'OAS 合并上游'                  # 每日定时合并失败时飞书通知的标题
FEISHU_MERGE_STATUSES = frozenset({'conflict_aborted', 'push_failed'})   # 每日定时合并的这两种结果发飞书通知
_HHMM = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')


def _parse_hhmm(text: str | None) -> tuple[int, int] | None:
    """'HH:MM'（各两位数字，00:00 到 23:59）解析成 (时, 分)；空值，格式或范围不对返回 None。"""
    match = _HHMM.fullmatch(text) if text else None
    return (int(match[1]), int(match[2])) if match else None


def merge_due(now: datetime, hhmm: str | None, last_run: date | None) -> bool:
    """每日定时合并现在该不该跑：设了合法的 hhmm，今天已过这个时刻（整分就算到点），今天还没试过。

    last_run 是上一次试的日期，只要不是今天就算今天还没试过：状态文件里的日期哪怕比今天晚（时钟被往回拨过），
    也只是多补一次，不会把定时合并无声无息地关上几个月。纯函数：hhmm 不合法按关闭处理，日志由调用方记。
    """
    due = _parse_hhmm(hhmm)
    if due is None:
        return False
    return (now.hour, now.minute) >= due and last_run != now.date()


@dataclass(frozen=True)
class MergeState:
    """每日定时合并的记录。last_run：当天的合并已经用掉的日期（试过且不是拉取失败，或者拉取失败到了次数上限）；
    attempts：last_attempt 那一天试过的次数；last_attempt：上一次试的时刻。"""
    last_run: date | None = None
    attempts: int = 0
    last_attempt: datetime | None = None


def attempt_number(now: datetime, state: MergeState) -> int:
    """merge_due 为真时，现在这次是今天的第几次尝试；还在等（离上一次不到 RETRY_DELAY）或今天已经试满 MAX_ATTEMPTS
    次时返回 0。上一次不是今天的，从 1 算起。时钟被往回拨过（上一次在现在之后）不等，免得卡到下一次时刻。纯函数。"""
    last = state.last_attempt
    if last is None or last.date() != now.date():
        return 1
    if state.attempts >= MAX_ATTEMPTS or timedelta(0) <= now - last < RETRY_DELAY:
        return 0
    return state.attempts + 1


def _state_path() -> Path:
    return Path.cwd() / 'log' / 'custom_jobs' / 'state.json'


def _load_state() -> MergeState:
    """状态文件里的记录。文件不存在是首次运行的常态，静默返回空记录；只有日期的旧格式照常读（没试过）。
    读不了或内容不对（不是 JSON、不是对象、没有日期这个键、日期时刻格式不对、次数不是非负整数）记一条 WARNING，
    同样返回空记录（按没跑过处理）。"""
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        last_run, attempts, last_attempt = data[STATE_KEY], data.get(ATTEMPTS_KEY, 0), data.get(LAST_ATTEMPT_KEY)
        if type(attempts) is not int or attempts < 0:
            raise ValueError(f'{ATTEMPTS_KEY} is not a non-negative integer: {attempts!r}')
        return MergeState(None if last_run is None else date.fromisoformat(last_run), attempts,
                          None if last_attempt is None else datetime.fromisoformat(last_attempt))
    except FileNotFoundError:
        return MergeState()
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning(f'后台任务状态文件损坏或读不了（{path}）：{exc!r}，按没跑过处理')
        return MergeState()


def _save_state(state: MergeState) -> None:
    """原子写状态文件，目录不存在时先建。写不进去时抛 OSError，由调用方决定怎么办。"""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {STATE_KEY: None if state.last_run is None else state.last_run.isoformat(),
            ATTEMPTS_KEY: state.attempts,
            LAST_ATTEMPT_KEY: None if state.last_attempt is None else state.last_attempt.isoformat(timespec='seconds')}
    with atomic_write(path, overwrite=True, encoding='utf-8', newline='') as f:
        f.write(json.dumps(data, indent=2))


def _merge_on_loop(loop: asyncio.AbstractEventLoop) -> tuple[MergeResult | None, str]:
    """让服务的事件循环跑一次 run_update_result，在这里（后台任务线程）等它的 (合并结果, 文案)。循环已经关了的话，
    把还没人 await 的协程关掉再抛，不留一个"从未被 await"的警告。超时抛 concurrent.futures.TimeoutError，
    不取消：合并本身还在循环上继续，做完照常重启实例。"""
    coroutine = fork_updater.run_update_result()
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception:
        coroutine.close()
        raise
    return future.result(timeout=MERGE_TIMEOUT)


@dataclass
class _Job:
    fn: Callable[[], None]
    interval: float                    # 秒
    last_start: float | None = None    # 上一次开始跑的单调时间；None = 还没跑过


class CustomJobs:
    """后台任务线程和它管的任务。生产里只有模块末尾的 _default 一个实例；测试自己造实例，传入假时钟。

    now 是墙上时间（每日合并按它算几点、哪一天），monotonic 是单调时间（周期任务按它算间隔），tick_seconds 是两轮检查的间隔。
    """

    def __init__(self, now: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic, tick_seconds: float = TICK_SECONDS):
        self._now = now
        self._monotonic = monotonic
        self._tick_seconds = tick_seconds
        self._lock = threading.Lock()                      # 护着任务表和线程句柄；跑任务时不持锁
        self._jobs: dict[str, _Job] = {}
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._state = MergeState()                         # 每日合并的记录；第一次用到时才从状态文件读
        self._state_loaded = False
        self._warned: set[str] = set()                     # 已经记过"不合法"的 OAS_UPSTREAM_MERGE_TIME 取值

    def register(self, name: str, interval_seconds: float, fn: Callable[[], None]) -> None:
        """登记一个周期任务：每 interval_seconds 秒跑一次（线程每轮最多跑它一次），登记后的第一轮就跑。
        同名再登记就换掉原来的。线程启动前后都可以调。"""
        with self._lock:
            self._jobs[name] = _Job(fn, float(interval_seconds))

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """启动线程；线程活着时再调是空操作。loop 是每日合并要交给 run_update_result 的事件循环，缺省取当前正在运行的那个
        （没有就抛 RuntimeError），所以要在事件循环里调，app.py 的 on_startup 就是。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if loop is None:
                loop = asyncio.get_running_loop()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(loop, self._stop), name=THREAD_NAME, daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """叫停线程并最多等 timeout 秒。服务从不调它，测试用。停下之后可以再 start。"""
        with self._lock:
            thread, stop = self._thread, self._stop
        if thread is not None and stop is not None:
            stop.set()
            thread.join(timeout)

    def tick(self, loop: asyncio.AbstractEventLoop) -> None:
        """一轮检查：先看每日定时合并，再跑到期的任务。两部分各自兜住异常，合并的检查出错不会让任务饿着。
        线程每轮调它，测试也直接调；不能在 loop 所在的线程里调，它会在那里等 run_update_result 做完。"""
        try:
            self._check_daily_merge(loop)
        except Exception:
            logger.exception('每日定时合并检查出错')
        self._run_due_jobs()

    def _run(self, loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
        logger.info(f'后台任务线程 {THREAD_NAME} 已启动（每 {self._tick_seconds:g} 秒检查一次）')
        while True:
            try:
                self.tick(loop)
            except Exception:                              # tick 自己已经兜住了各部分，这是最后一道网
                logger.exception('后台任务线程这一轮检查出错，下一轮继续')
            if stop.wait(self._tick_seconds):
                return

    def _merge_time(self) -> str | None:
        """OAS_UPSTREAM_MERGE_TIME 的值；没设、留空、不合法都是 None（关闭）。不合法的取值每个只记一次日志。"""
        value = read_env(MERGE_TIME_KEY)
        if not value:
            return None
        if _parse_hhmm(value) is None:
            if value not in self._warned:
                self._warned.add(value)
                logger.warning(f'{MERGE_TIME_KEY}={value!r} 不是合法的 HH:MM（00:00 到 23:59），每日定时合并保持关闭')
            return None
        return value

    def _merge_state(self) -> MergeState:
        if not self._state_loaded:
            self._state, self._state_loaded = _load_state(), True
        return self._state

    def _remember(self, state: MergeState) -> None:
        self._state, self._state_loaded = state, True
        try:
            _save_state(state)
        except Exception as exc:
            logger.warning(f'记录每日定时合并的状态失败：{exc!r}；本次运行按内存里的记录办，服务重启后按状态文件里的旧记录')

    def _check_daily_merge(self, loop: asyncio.AbstractEventLoop) -> None:
        hhmm = self._merge_time()
        if hhmm is None:
            return
        now = self._now()
        state = self._merge_state()
        if not merge_due(now, hhmm, state.last_run):
            return
        attempt = attempt_number(now, state)
        if not attempt:
            return
        # 先记再试：今天先算用掉，拿到拉取失败的结果才退回去，所以出错、等不到结果、进程被杀都不会让它反复合并
        self._remember(MergeState(now.date(), attempt, now))
        nth = f'（今天第 {attempt} 次）' if attempt > 1 else ''
        logger.info(f'每日定时合并上游：设定 {hhmm}，现在 {now:%H:%M}，开始合并{nth}')
        try:
            result, message = _merge_on_loop(loop)
        except concurrent.futures.TimeoutError:
            logger.error(f'每日定时合并上游：{MERGE_TIMEOUT} 秒内没有等到结果，不再等待（合并还在后台继续，做完照常重启实例）')
            return
        except Exception:
            logger.exception('每日定时合并上游失败')
            return
        if result is None or result.status != 'fetch_failed':
            logger.info(f'每日定时合并上游：{message}')
            if result is not None and result.status in FEISHU_MERGE_STATUSES:
                from module.notify import feishu_app           # 用到时才导入，同 start_custom_jobs
                feishu_app.notify_server_event(FEISHU_MERGE_TITLE, message)     # 不抛异常
        elif attempt < MAX_ATTEMPTS:
            self._remember(MergeState(state.last_run, attempt, now))      # 拉取失败不占用今天
            minutes = int(RETRY_DELAY.total_seconds() // 60)
            logger.info(f'每日定时合并上游：{message}（拉取失败不算今天的合并，{minutes} 分钟后重试，'
                        f'今天已试 {attempt}/{MAX_ATTEMPTS} 次）')
        else:
            logger.info(f'每日定时合并上游：{message}（今天已试 {MAX_ATTEMPTS} 次都没拉取成功，明天再合并）')

    def _run_due_jobs(self) -> None:
        with self._lock:
            jobs = list(self._jobs.items())
        for name, job in jobs:
            started = self._monotonic()
            if job.last_start is not None and started - job.last_start < job.interval - DUE_SLACK:
                continue
            job.last_start = started
            try:
                job.fn()
            except Exception:
                logger.exception(f'后台任务 {name} 出错')


_default = CustomJobs()


def register_job(name: str, interval_seconds: float, fn: Callable[[], None]) -> None:
    """登记一个周期任务，由后台任务线程每 interval_seconds 秒跑一次。M4 用它注册飞书补发。见 CustomJobs.register。"""
    _default.register(name, interval_seconds, fn)


def start_custom_jobs(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """启动后台任务线程（每 60 秒检查一次每日定时合并并跑已登记的任务）；线程活着时再调是空操作。见 CustomJobs.start。

    起线程之前先登记飞书补发（每 60 秒一次 flush_outbox）。在这里登记、在这里才导入飞书模块，导入本模块就没有副作用；
    同名再登记只是换掉原来的，再调一次也无妨。
    """
    from module.notify.feishu_app import flush_outbox
    register_job('feishu_flush', 60, flush_outbox)
    _default.start(loop)
