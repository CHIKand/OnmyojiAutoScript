# This Python file uses the following encoding: utf-8
"""编排表的任务映射：一行里的子项、次数、分钟，运行前写进任务自己的原生配置字段，运行后还原。

任务本身的逻辑不改：它读到的就是写进去的值，到次数或分钟照原样退出。

字段路径有两种写法。TaskSpec、SubField 里的路径相对 model.<model_key>；apply_row 返回字典的键是
以 ConfigModel 为根的完整路径，restore_params 只凭它就能还原，不必再查映射表。
读写走 ConfigModel.deep_get / deep_set，只改嵌套对象，不会触发 ConfigModel.__setattr__ 的自动保存。

本模块在 script.py 与服务端的导入链上，所以不按名字导入上游任务模块里的枚举：子项的枚举在建表时
按字段路径从 ConfigModel 的类型标注里取。上游改了字段或枚举，只是对不上的任务不进 TASK_SPECS
（记一条警告），表里它的行校验不过、运行时跳过，导入本身不会失败。
"""
from dataclasses import dataclass, replace
from datetime import datetime, time
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

from module.config.config_model import ConfigModel
from module.logger import logger

if TYPE_CHECKING:
    from module.orchestra.model import OrchestraRow   # 只用于类型标注


@dataclass(frozen=True)
class SubField:
    """子项：界面上的一个下拉框，对应任务自己的一个选项字段。"""
    path: str                       # 相对 model.<model_key> 的字段路径
    options: tuple[str, ...]        # 可选值，也是界面下拉框的取值
    labels: tuple[str, ...]         # 界面显示文案，与 options 一一对应
    to_value: Callable[[str], Any]  # 把选项转成该字段实际存放的值（枚举成员、布尔等）
    required: bool = False          # 必须选。只有爬塔的类型是：不选就没有次数字段可写，校验见 model._row_errors


@dataclass(frozen=True)
class _EnumSub:
    """枚举字段的子项，只在 _DEFINITIONS 里占位：建表时按路径查出字段的枚举类型，换成 SubField。"""
    path: str   # 相对 model.<model_key> 的字段路径


@dataclass(frozen=True)
class TaskSpec:
    command: str                # 任务命令名，即编排表里的 task
    model_key: str              # 该任务在 ConfigModel 上的字段名
    title: str                  # 界面显示名，按菜单分组加前缀
    count_path: str | None      # 次数字段；None 表示没有。爬塔是含 {type} 的模板，见 count_path_for
    count_max: int | None       # 次数字段声明的上限；没有声明则为 None
    time_path: str | None       # 时长字段（Time 类型）；None 表示没有
    subs: tuple[SubField, ...]  # 子项，依次对应 sub1、sub2
    counter_attr: str | None    # 任务对象上记录"已打次数"的属性名；None 表示没有可靠的属性


def _enum_sub(path: str, enum_cls: type[Enum]) -> SubField:
    """枚举字段的子项：选项取枚举的值，按枚举顺序；这些值本身就是界面文案。"""
    values = tuple(member.value for member in enum_cls)
    return SubField(path, values, values, enum_cls)


def _on_off(raw: str) -> bool:
    return {'on': True, 'off': False}[raw]


# 映射表的定义，subs 里的 _EnumSub 由 build_task_specs 换成 SubField。
# counter_attr 取值依据：current_count 由 BaseTask 初始化，run_general_battle 每进一场加 1；
# 探索另有自己的 minions_cnt。为 None 的任务没有可靠的实例属性：
#   SixRealms、Hyakkiyakou：计数只是方法内的局部变量；
#   ActivityShikigami：计数在字典 count_map[爬塔类型] 里，不会累加 current_count；
#   MetaDemon：计数在类级的 battle_wait runtime 里，不会累加 current_count；
#   FindJade、MemoryScrolls：不计战斗次数。
# BondlingFairyland 在 handoff1 / handoff2 身份下，换手时会把 current_count 清零。
_DEFINITIONS = (
    TaskSpec('Orochi', 'orochi', '御魂·八岐大蛇',
             count_path='orochi_config.limit_count', count_max=None, time_path='orochi_config.limit_time',
             subs=(_EnumSub('orochi_config.layer'),), counter_attr='current_count'),
    TaskSpec('Sougenbi', 'sougenbi', '御魂·业原火',
             count_path='sougenbi_config.limit_count', count_max=None, time_path='sougenbi_config.limit_time',
             subs=(_EnumSub('sougenbi_config.sougenbi_class'),), counter_attr='current_count'),
    TaskSpec('FallenSun', 'fallen_sun', '御魂·日轮之陨',
             count_path='fallen_sun_config.limit_count', count_max=None, time_path='fallen_sun_config.limit_time',
             subs=(_EnumSub('fallen_sun_config.layer'),), counter_attr='current_count'),
    TaskSpec('EternitySea', 'eternity_sea', '御魂·永生之海',
             count_path='eternity_sea_config.limit_count', count_max=None, time_path='eternity_sea_config.limit_time',
             subs=(_EnumSub('eternity_sea_config.layer'),), counter_attr='current_count'),
    TaskSpec('SixRealms', 'six_realms', '御魂·六道之门',
             count_path='six_realms_gate.limit_count', count_max=None, time_path='six_realms_gate.limit_time',
             subs=(), counter_attr=None),
    TaskSpec('OtherWorldTwilight', 'other_world_twilight', '御魂·彼世逢魔',
             count_path='other_world_twilight_config.limit_count', count_max=None,
             time_path='other_world_twilight_config.limit_time',
             subs=(), counter_attr='current_count'),
    TaskSpec('BondlingFairyland', 'bondling_fairyland', '肝帝·契灵之境',
             count_path='bondling_config.limit_count', count_max=None, time_path='bondling_config.limit_time',
             subs=(_EnumSub('bondling_config.bondling_mode'), _EnumSub('bondling_config.bondling_stone_class')),
             counter_attr='current_count'),
    TaskSpec('EvoZone', 'evo_zone', '肝帝·觉醒副本',
             count_path='evo_zone_config.limit_count', count_max=None, time_path='evo_zone_config.limit_time',
             subs=(_EnumSub('evo_zone_config.kirin_type'), _EnumSub('evo_zone_config.layer')),
             counter_attr='current_count'),
    TaskSpec('GoryouRealm', 'goryou_realm', '肝帝·御灵之境',
             count_path='goryou_config.limit_count', count_max=None, time_path='goryou_config.limit_time',
             subs=(_EnumSub('goryou_config.goryou_class'),), counter_attr='current_count'),
    TaskSpec('Exploration', 'exploration', '肝帝·探索',
             count_path='exploration_config.minions_cnt', count_max=None, time_path='exploration_config.limit_time',
             subs=(_EnumSub('exploration_config.exploration_level'),),
             counter_attr='minions_cnt'),
    TaskSpec('Hyakkiyakou', 'hyakkiyakou', '肝帝·百鬼夜行',
             count_path='hyakkiyakou_config.hya_limit_count', count_max=None,
             time_path='hyakkiyakou_config.hya_limit_time',
             subs=(), counter_attr=None),
    TaskSpec('HeroTest', 'hero_test', '肝帝·英杰试炼',
             count_path='herotest.limit_count', count_max=None, time_path='herotest.limit_time',
             subs=(_EnumSub('herotest.layer'),), counter_attr='current_count'),
    TaskSpec('FindJade', 'find_jade', '肝帝·寻找协作',
             count_path=None, count_max=None, time_path=None,
             subs=(), counter_attr=None),
    TaskSpec('MemoryScrolls', 'memory_scrolls', '肝帝·绘卷',
             count_path=None, count_max=None, time_path=None,
             subs=(_EnumSub('memory_scrolls_config.scroll_number'),), counter_attr=None),
    TaskSpec('RealmRaid', 'realm_raid', '个人突破',
             count_path='raid_config.number_attack', count_max=30, time_path=None,
             subs=(), counter_attr='current_count'),
    # 爬塔的次数按类型分开存放（pass_limit、ap_limit……），所以次数路径是模板，由子项决定
    TaskSpec('ActivityShikigami', 'activity_shikigami', '限时·爬塔',
             count_path='general_climb.{type}_limit', count_max=None, time_path='general_climb.limit_time',
             subs=(SubField('general_climb.run_sequence',
                            ('pass', 'ap', 'boss', 'ap100'), ('门票', '体力', 'Boss', '100体'), str, required=True),),
             counter_attr=None),
    TaskSpec('MetaDemon', 'meta_demon', '限时·超鬼王',
             count_path=None, count_max=None, time_path=None,
             subs=(SubField('meta_demon_config.md_use_strategy', ('on', 'off'), ('开', '关'), _on_off),),
             counter_attr=None),
)

_MISSING = object()


def count_path_for(spec: TaskSpec, sub1: str | None) -> str | None:
    """该行实际要写的次数字段，相对 model.<model_key>。

    只有爬塔的路径含 {type}，要用 sub1 补全；没选子项时没有落点，返回 None。其余任务原样返回 spec.count_path。
    """
    path = spec.count_path
    if path is None or '{type}' not in path:
        return path
    return None if sub1 is None else path.format(type=sub1)


def _annotation(path: str) -> Any:
    """以 ConfigModel 为根的字段路径上，最后一级字段的类型标注。某一级不是 pydantic 模型或没有这个字段时返回 _MISSING。"""
    annotation: Any = ConfigModel
    for name in path.split('.'):
        fields = getattr(annotation, 'model_fields', None)
        if not isinstance(fields, dict) or name not in fields:
            return _MISSING
        annotation = fields[name].annotation
    return annotation


def _resolve(spec: TaskSpec) -> TaskSpec:
    """把 spec 里的 _EnumSub 换成按字段的枚举类型建出的 SubField，并核对它用到的字段路径都在 ConfigModel 上：
    子项、次数（爬塔按每种类型展开）、时长，以及运行时要写的 scheduler.next_run。对不上抛 LookupError。"""
    def annotation_of(path: str) -> Any:
        found = _annotation(f'{spec.model_key}.{path}')
        if found is _MISSING:
            raise LookupError(f'字段路径无法解析：{spec.model_key}.{path}')
        return found

    subs = []
    for sub in spec.subs:
        found = annotation_of(sub.path)
        if isinstance(sub, _EnumSub):
            if not (isinstance(found, type) and issubclass(found, Enum)):
                raise LookupError(f'{spec.model_key}.{sub.path} 不是枚举字段')
            sub = _enum_sub(sub.path, found)
        subs.append(sub)
    resolved = replace(spec, subs=tuple(subs))

    count_paths = []
    if spec.count_path is not None:
        count_paths = ([count_path_for(resolved, option) for option in resolved.subs[0].options]
                       if '{type}' in spec.count_path else [spec.count_path])
    for path in ('scheduler.next_run', spec.time_path, *count_paths):
        if path is not None:
            annotation_of(path)
    return resolved


def build_task_specs() -> dict[str, TaskSpec]:
    """按 _DEFINITIONS 建映射表，顺序不变。字段对不上的任务不放进去，每个记一条警告。

    在导入时运行，所以任何异常都不往外抛：本模块在 script.py 与服务端的导入链上，抛了实例和服务都起不来。
    """
    specs: dict[str, TaskSpec] = {}
    for spec in _DEFINITIONS:
        try:
            specs[spec.command] = _resolve(spec)
        except Exception as exc:
            logger.warning(f'编排映射：{spec.command} 的字段与上游配置对不上（{exc}），暂不支持编排，表里它的行会被跳过')
    return specs


# 按菜单顺序
TASK_SPECS: dict[str, TaskSpec] = build_task_specs()


def minutes_to_time(minutes: int) -> time:
    """分钟数转成任务时长字段用的 time。时长字段是时:分:秒，所以只接受 0–1439，超出抛 ValueError。"""
    if not 0 <= minutes <= 1439:
        raise ValueError(f'分钟数需在 0–1439 之间：{minutes}')
    return time(minutes // 60, minutes % 60)


def serialize_value(v: Any) -> Any:
    """转成可以存进 JSON 的值：datetime 转 ISO 字符串，time 转 'HH:MM:SS'，Enum 取 .value，其他原样。"""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime('%H:%M:%S')
    if isinstance(v, Enum):
        return v.value
    return v


def _deserialize(raw: Any, current: Any) -> Any:
    """serialize_value 的逆运算，类型取字段当前值的类型。"""
    if isinstance(current, datetime):
        return datetime.fromisoformat(raw)
    if isinstance(current, time):
        return time.fromisoformat(raw)
    if isinstance(current, Enum):
        return type(current)(raw)
    return raw


def _get(model: ConfigModel, path: str) -> Any:
    """读字段当前值，路径无法解析时返回 _MISSING。"""
    return ConfigModel.deep_get(model, path, _MISSING)


def _set(model: ConfigModel, path: str, value: Any) -> None:
    if not ConfigModel.deep_set(model, path, value):
        raise ValueError(f'编排映射的字段路径无法写入：{path}')


def apply_row(model: ConfigModel, row: 'OrchestraRow',
              count: int | None = None, minutes: int | None = None) -> dict[str, Any]:
    """把一行的子项、次数、分钟写进任务的原生字段，返回 {完整字段路径: 原值序列化}，交给 restore_params 还原。

    count、minutes 不传时取 row 自己的 max_count、max_minutes。值为 None 的项沿用任务自己的设置：
    不写入，也不出现在返回值里。row.task 不在映射表里时抛 KeyError。

    有值却没有落点（任务没有这个字段，或爬塔没选类型）、子项不在可选范围内、路径无法解析，都抛 ValueError。
    先读后写，抛出时配置一个字段都没动。
    """
    spec = TASK_SPECS[row.task]
    writes: list[tuple[str, Any]] = []  # (以 ConfigModel 为根的路径, 新值)
    for position, raw in enumerate((row.sub1, row.sub2)):
        if raw is None:
            continue
        if position >= len(spec.subs):
            raise ValueError(f'{spec.command} 没有第 {position + 1} 个子项')
        sub = spec.subs[position]
        if raw not in sub.options:
            raise ValueError(f'{spec.command} 的子项取值不在可选范围内：{raw!r}')
        writes.append((f'{spec.model_key}.{sub.path}', sub.to_value(raw)))
    count = row.max_count if count is None else count
    if count is not None:
        path = count_path_for(spec, row.sub1)
        if path is None:
            raise ValueError(f'{spec.command} 没有可写入次数的字段')
        writes.append((f'{spec.model_key}.{path}', count))
    minutes = row.max_minutes if minutes is None else minutes
    if minutes is not None:
        if spec.time_path is None:
            raise ValueError(f'{spec.command} 没有可写入时长的字段')
        writes.append((f'{spec.model_key}.{spec.time_path}', minutes_to_time(minutes)))

    saved: dict[str, Any] = {}
    for path, _ in writes:
        current = _get(model, path)
        if current is _MISSING:
            raise ValueError(f'编排映射的字段路径无法解析：{path}')
        saved[path] = serialize_value(current)
    for path, value in writes:
        _set(model, path, value)
    return saved


def restore_params(model: ConfigModel, saved: dict[str, Any]) -> None:
    """按 apply_row 返回的字典还原字段。

    一项还原不了不挡住其余项：路径无法解析（映射或进度文件已过期），或原值转不回字段的类型、字段写不进去
    （枚举值改了名、时间串损坏等），都先记下，其余照常还原，处理完统一抛一次 ValueError，列出所有没还原的路径。
    原值转不回去的那一项，消息里带上原值和当时的异常，调用方清掉 saved_params 之后，人还能照着手工改回。
    """
    unresolved, failed = [], []
    for path, raw in saved.items():
        current = _get(model, path)
        if current is _MISSING:
            unresolved.append(path)
            continue
        try:
            _set(model, path, _deserialize(raw, current))
        except Exception as exc:
            failed.append(f'{path}（原值 {raw!r}，{exc!r}）')
    problems = []
    if unresolved:
        problems.append(f'编排映射的字段路径无法解析，未还原：{", ".join(unresolved)}')
    if failed:
        problems.append(f'原值无法写回字段，未还原：{", ".join(failed)}')
    if problems:
        raise ValueError('；'.join(problems))


def catalog() -> list[dict]:
    """给界面用的任务目录，顺序同 TASK_SPECS。子项的 required 为 True 表示这一格必须选。"""
    return [{
        'command': spec.command,
        'title': spec.title,
        'count_max': spec.count_max,
        'has_count': spec.count_path is not None,
        'has_time': spec.time_path is not None,
        'subs': [{'options': list(sub.options), 'labels': list(sub.labels), 'required': sub.required}
                 for sub in spec.subs],
    } for spec in TASK_SPECS.values()]
