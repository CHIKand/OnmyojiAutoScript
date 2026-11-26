"""编排引擎的运行进度文件。

每个实例一份，放在 log/orchestra/<实例名>.json。里面记着当前第几行、处于哪个阶段、心跳、休息截止时间、
被行参数覆盖之前的用户原值，以及等搭档的记录：进程中断后凭它续跑或还原，接口也读它给运行面板，搭档实例读它判断会合。
不放在 log/ 根目录：上游每次启动会删掉 log/ 根目录下超过 14 天的文件（module/logger.py 的 cleanup_logs，
子目录里只清 log/error），停了半个月的实例会连同没还原的原值一起丢掉进度。也不放在 config/：
上游把那里的每个 json 都当作一个实例。早先版本放在 log/.orchestra_<实例名>.json，实例第一次读进度时
用 adopt_legacy_progress 搬过来。

只有本模块写这个文件，写入走 atomic_write，不会读到写了一半的内容。
文件不存在或内容损坏时读成默认进度，不抛异常。

Windows 上有读者开着文件时，写者的替换会被拒；读者赶上替换的瞬间，读也会被拒，都表现为 PermissionError。
双方占用文件的时间都很短，所以读写遇到 PermissionError 时有限次重试，见 _retry_on_permission_error。
"""
import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from module.config.atomicwrites import atomic_write
from module.logger import logger

HISTORY_LIMIT = 20  # 文件里最多留多少条历史
_RETRY_ATTEMPTS = 10  # 读写遇到 PermissionError 时，一次操作最多尝试的次数
_RETRY_DELAY = 0.05   # 两次尝试之间等待的秒数
_DATETIME_FIELDS = ('sequence_started_at', 'row_started_at', 'heartbeat_at', 'rest_until',
                    'team_wait_since', 'team_alive_at', 'team_met_at')


@dataclass
class Progress:
    """一个实例的编排运行进度。datetime 不带时区信息，用 isoformat 原样存取。"""
    phase: str = 'idle'                    # idle / ready / row_running / resting / interrupted / done
    row_index: int = 0
    row_task: str | None = None            # 正在跑或被打断的那一行的任务；表在运行中被改过时，凭它认出是不是同一行
    loop_count: int = 0
    sequence_started_at: datetime | None = None
    row_started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    done_count: int | None = None          # 本行已完成的次数；未知为 None
    row_elapsed_before: float = 0.0        # 本行此前各次中断累计的秒数
    rest_until: datetime | None = None
    rest_remaining: float | None = None    # 暂停时记下的剩余休息秒数
    total_elapsed: float = 0.0             # 秒
    saved_params: dict = field(default_factory=dict)   # 行参数写入前用户的原值（apply_row 的返回值），崩溃后据此还原
    last_command_seq: int = 0              # 已处理到的命令序号
    paused: bool = False
    history: list[dict] = field(default_factory=list)  # 写文件时只留最后 HISTORY_LIMIT 条
    status_note: str = ''                  # 给运行面板看的提示，如 防封禁休眠中，08:00 继续
    # 等搭档的等待记录：正在等时有值，会合或不再等后由 clear_team_wait 清空
    team_wait_since: datetime | None = None    # 开始等的时刻
    team_wait_ordinal: int | None = None       # 在等的是本表第几个组队行，从 1 起
    team_wait_task: str | None = None          # 在等的那一行的任务
    team_alive_at: datetime | None = None      # 最近一次刷新等待的时刻，搭档凭它判断这边还在不在等
    # 最近一次与搭档会合：搭档凭它判断这边已经先走了
    team_met_at: datetime | None = None
    team_met_ordinal: int | None = None
    team_met_task: str | None = None
    team_partner: str = ''                     # 本实例当前设的搭档
    team_row_ordinal: int | None = None        # 正在跑或被打断的那一行是第几个组队行；不是组队行为 None


def _to_json_dict(p: Progress) -> dict:
    """转成可直接写成 JSON 的字典：datetime 为 ISO 字符串，历史只留最后 HISTORY_LIMIT 条。返回的是副本，不动 p。"""
    data = asdict(p)
    for name in _DATETIME_FIELDS:
        if data[name] is not None:
            data[name] = data[name].isoformat()
    data['history'] = data['history'][-HISTORY_LIMIT:]
    return data


def _from_json(raw: Any) -> Progress:
    """_to_json_dict 的逆运算。顶层不是对象或日期无法解析时抛 ValueError / TypeError。"""
    if not isinstance(raw, dict):
        raise ValueError(f'顶层应为 JSON 对象，实际是 {type(raw).__name__}')
    names = {f.name for f in fields(Progress)}
    data = {key: value for key, value in raw.items() if key in names}   # 未知键忽略，缺的键取默认值
    for name in _DATETIME_FIELDS:
        if data.get(name) is not None:
            data[name] = datetime.fromisoformat(data[name])
    return Progress(**data)


def _retry_on_permission_error(operation: Callable[[], Any]) -> Any:
    """执行 operation 并返回它的结果；遇到 PermissionError 就隔 _RETRY_DELAY 秒再来，一共最多 _RETRY_ATTEMPTS 次。

    只重试 PermissionError，其他异常立刻抛出；最后一次仍失败，就把最后一次的 PermissionError 抛出去。
    """
    attempt = 1
    while True:
        try:
            return operation()
        except PermissionError:
            if attempt >= _RETRY_ATTEMPTS:
                raise
            attempt += 1
            time.sleep(_RETRY_DELAY)


def progress_path(config_name: str, root: Path | None = None) -> Path:
    """某个实例的进度文件路径：<root，缺省为当前目录>/log/orchestra/<实例名>.json。"""
    return (root if root is not None else Path.cwd()) / 'log' / 'orchestra' / f'{config_name}.json'


def legacy_progress_path(config_name: str, root: Path | None = None) -> Path:
    """早先版本的进度文件路径：<root，缺省为当前目录>/log/.orchestra_<实例名>.json。"""
    return (root if root is not None else Path.cwd()) / 'log' / f'.orchestra_{config_name}.json'


def adopt_legacy_progress(config_name: str, root: Path | None = None) -> bool:
    """新位置还没有进度文件、旧位置有时，把旧文件原样搬到新位置并返回 True，进行中的序列和没还原的原值都跟着过来；
    否则什么都不动，返回 False。

    只该由实例进程调用，它是进度文件唯一的写者，所以"新文件不存在"在搬之前不会变。搬遇到 PermissionError
    同样有限次重试，仍然失败就抛出，旧文件原样留着，下次再搬。
    """
    path, legacy = progress_path(config_name, root), legacy_progress_path(config_name, root)
    if path.exists() or not legacy.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _retry_on_permission_error(lambda: os.replace(legacy, path))
    return True


def load_progress(path: Path) -> Progress:
    """读进度文件。文件不存在时静默返回默认进度，那是首次运行的常态；内容损坏时记一条 WARNING，同样返回默认进度。

    损坏指：不是合法 JSON、顶层不是对象、日期字段无法解析。文件里的未知键忽略，缺的键取默认值，
    其余字段的类型不检查，因为文件只由本模块原子写入。
    读文件遇到 PermissionError（Windows 上正赶上写者替换文件）会先有限次重试。读文件时的其他错误，
    以及重试后仍然失败的 PermissionError，既不是不存在也不是损坏，照常抛出：拿默认值顶替的话，
    调用方接着保存就会把真实进度连同 saved_params 一起覆盖掉。
    """
    try:
        text = _retry_on_permission_error(lambda: path.read_text(encoding='utf-8'))
        return _from_json(json.loads(text))
    except FileNotFoundError:
        return Progress()
    except (TypeError, ValueError) as exc:   # JSONDecodeError、UnicodeDecodeError 都是 ValueError 的子类
        logger.warning(f'编排进度文件损坏（{path}）：{exc!r}，按空进度处理')
        return Progress()


def save_progress(path: Path, p: Progress) -> None:
    """原子写进度文件，目录不存在时先建。历史只写最后 HISTORY_LIMIT 条，p 本身不动。

    Windows 上有读者开着文件时替换会被拒，遇到 PermissionError 就把整个原子写重做一遍，
    次数与间隔见 _RETRY_ATTEMPTS、_RETRY_DELAY；仍然失败则抛出最后一次的 PermissionError。
    失败的那次写下的临时文件，由 atomic_write 回滚时删掉，旧文件原样保留。
    """
    text = json.dumps(_to_json_dict(p), ensure_ascii=False, indent=2)   # 序列化放在写入之前，出错时不碰磁盘
    path.parent.mkdir(parents=True, exist_ok=True)

    def write() -> None:
        with atomic_write(path, overwrite=True, encoding='utf-8', newline='') as f:
            f.write(text)

    _retry_on_permission_error(write)


def progress_to_dict(p: Progress, now: datetime) -> dict:
    """进度的字典视图，给 HTTP 接口用。各字段与文件里的形式一致，另附两个按 now 现算的字段，它们不写进文件。

    row_elapsed_seconds：row_running 阶段本行已跑的秒数（含此前各次中断累计的），截止时刻取最近一次心跳，
    还没有心跳时取 now；其他阶段为 None。
    rest_remaining_seconds：暂停且记着剩余休息秒数时，原样返回它；否则 resting 阶段为距 rest_until 的秒数，
    最小为 0；其他情况为 None。
    """
    data = _to_json_dict(p)
    data['row_elapsed_seconds'] = None
    if p.phase == 'row_running' and p.row_started_at is not None:
        end = p.heartbeat_at if p.heartbeat_at is not None else now
        data['row_elapsed_seconds'] = (end - p.row_started_at).total_seconds() + p.row_elapsed_before
    data['rest_remaining_seconds'] = None
    if p.paused and p.rest_remaining is not None:
        data['rest_remaining_seconds'] = p.rest_remaining
    elif p.phase == 'resting' and p.rest_until is not None:
        data['rest_remaining_seconds'] = max(0.0, (p.rest_until - now).total_seconds())
    return data


def clear_team_wait(p: Progress) -> bool:
    """就地清掉 p 的等待记录：team_wait_since、team_wait_ordinal、team_wait_task、team_alive_at；会合记录不动。

    原来有一项有值就返回 True，调用方据此决定要不要存盘；本来就是空的返回 False。
    """
    had_any = any(value is not None
                  for value in (p.team_wait_since, p.team_wait_ordinal, p.team_wait_task, p.team_alive_at))
    p.team_wait_since = p.team_wait_ordinal = p.team_wait_task = p.team_alive_at = None
    return had_any
