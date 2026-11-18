"""本分支的后台任务线程：可选的每日定时合并上游，以及别的里程碑注册进来的周期任务。

app.py 的 on_startup 经 custom_routes 调 start_custom_jobs()，起一个名为 custom-jobs 的守护线程。线程每 TICK_SECONDS
秒醒来一次：先看每日定时合并该不该跑，再按间隔依次跑已注册的任务（register_job，M4 用它注册飞书补发）。

每日定时合并：.env 的 OAS_UPSTREAM_MERGE_TIME（HH:MM；缺省或留空就是关闭，不合法的值也按关闭处理，并给每个取值记一次日志），
每次检查都重新读，改了不用重启。当天已过这个时刻、当天还没试过，就让服务的事件循环跑 fork_updater.run_update：与更新页的
按钮同一条路径（同一把锁，合并成功后同样重启实例，同样的文案），线程在这里等它做完，把文案记进日志。
时刻错过了（电脑休眠，或服务当时没开）不要紧：之后第一次检查发现"过了时刻、今天没试过"就补跑，线程一启动就先查一次。

一天只试一次：每次试之前先记下今天，不论结果好坏。记在 log/custom_jobs/state.json，同时留在内存里：状态文件写不进去，
也不会每分钟合并一次，最坏是服务重启后当天再试一次。想再合并就用更新页的按钮。状态文件放在子目录里：上游每次启动会删掉
log/ 根目录下超过 14 天的文件，子目录里只清 log/error。

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
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from module.config.atomicwrites import atomic_write
from module.config.dotenv import read_env
from module.logger import logger
from module.server import fork_updater

THREAD_NAME = 'custom-jobs'
TICK_SECONDS = 60                      # 线程每隔这么久醒来检查一次；间隔再短的任务，实际也是每轮最多跑一次
MERGE_TIMEOUT = 1800                   # 秒；定时合并等结果的上限（拉取和推送各有 300 秒超时，导入自检 120 秒，正常远用不到）
DUE_SLACK = 1.0                        # 秒；任务的间隔差不超过它就算到期。线程的等待有计时器误差（Windows 上 monotonic
                                       # 的分辨率约 15 毫秒，Event.wait 可能早醒），间隔恰好等于 TICK_SECONDS 的任务
                                       # （M4 的飞书补发是 60 秒）不留余量，就会隔一轮才跑
MERGE_TIME_KEY = 'OAS_UPSTREAM_MERGE_TIME'
STATE_KEY = 'upstream_merge_last_run'
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


def _state_path() -> Path:
    return Path.cwd() / 'log' / 'custom_jobs' / 'state.json'


def _load_last_run() -> date | None:
    """状态文件里记的、上一次试每日合并的日期。文件不存在是首次运行的常态，静默返回 None；
    读不了或内容不对（不是 JSON、不是对象、没有这个键、日期格式不对）记一条 WARNING，同样返回 None。"""
    path = _state_path()
    try:
        return date.fromisoformat(json.loads(path.read_text(encoding='utf-8'))[STATE_KEY])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning(f'后台任务状态文件损坏或读不了（{path}）：{exc!r}，按没跑过处理')
        return None


def _save_last_run(day: date) -> None:
    """原子写状态文件，目录不存在时先建。写不进去时抛 OSError，由调用方决定怎么办。"""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_write(path, overwrite=True, encoding='utf-8', newline='') as f:
        f.write(json.dumps({STATE_KEY: day.isoformat()}, indent=2))


def _merge_on_loop(loop: asyncio.AbstractEventLoop) -> str:
    """让服务的事件循环跑一次 run_update，在这里（后台任务线程）等它的结果。循环已经关了的话，
    把还没人 await 的协程关掉再抛，不留一个"从未被 await"的警告。超时抛 concurrent.futures.TimeoutError，
    不取消：合并本身还在循环上继续，做完照常重启实例。"""
    coroutine = fork_updater.run_update()
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
        self._last_run: date | None = None                 # 上一次试每日合并的日期；第一次用到时才从状态文件读
        self._last_run_loaded = False
        self._warned: set[str] = set()                     # 已经记过"不合法"的 OAS_UPSTREAM_MERGE_TIME 取值

    def register(self, name: str, interval_seconds: float, fn: Callable[[], None]) -> None:
        """登记一个周期任务：每 interval_seconds 秒跑一次（线程每轮最多跑它一次），登记后的第一轮就跑。
        同名再登记就换掉原来的。线程启动前后都可以调。"""
        with self._lock:
            self._jobs[name] = _Job(fn, float(interval_seconds))

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """启动线程；线程活着时再调是空操作。loop 是每日合并要交给 run_update 的事件循环，缺省取当前正在运行的那个
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
        线程每轮调它，测试也直接调；不能在 loop 所在的线程里调，它会在那里等 run_update 做完。"""
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

    def _last_merge_date(self) -> date | None:
        if not self._last_run_loaded:
            self._last_run, self._last_run_loaded = _load_last_run(), True
        return self._last_run

    def _remember_attempt(self, day: date) -> None:
        self._last_run, self._last_run_loaded = day, True
        try:
            _save_last_run(day)
        except Exception as exc:
            logger.warning(f'记录每日定时合并的日期失败：{exc!r}；服务不重启的话，当天不会再合并')

    def _check_daily_merge(self, loop: asyncio.AbstractEventLoop) -> None:
        hhmm = self._merge_time()
        if hhmm is None:
            return
        now = self._now()
        if not merge_due(now, hhmm, self._last_merge_date()):
            return
        self._remember_attempt(now.date())                 # 先记再试：不论结果如何，一天只试一次
        logger.info(f'每日定时合并上游：设定 {hhmm}，现在 {now:%H:%M}，开始合并')
        try:
            message = _merge_on_loop(loop)
        except concurrent.futures.TimeoutError:
            logger.error(f'每日定时合并上游：{MERGE_TIMEOUT} 秒内没有等到结果，不再等待（合并还在后台继续，做完照常重启实例）')
        except Exception:
            logger.exception('每日定时合并上游失败')
        else:
            logger.info(f'每日定时合并上游：{message}')

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
    """启动后台任务线程（每 60 秒检查一次每日定时合并并跑已登记的任务）；线程活着时再调是空操作。见 CustomJobs.start。"""
    _default.start(loop)
