from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    # 只用于类型标注。config_model 在顶层导入本文件，mapping 又在顶层导入 config_model，
    # 本文件顶层导入 mapping 或 config_model 都会成环，所以校验函数用到时才在函数内导入。
    from module.orchestra.mapping import TaskSpec


class OrchestraRow(BaseModel):
    """编排表的一行，即一轮。task 之外的字段为 None 表示未填。"""
    task: str
    sub1: str | None = None
    sub2: str | None = None
    max_count: int | None = None
    max_minutes: int | None = None
    rest_minutes: int | None = None


class Orchestra(BaseModel):
    """一个实例的编排配置，存在实例配置的 orchestra 字段里。

    字段只用宽松的普通类型：取值非法（未知任务名、越界的数、不认识的 state）读取时不抛异常，挡不住实例启动，
    取值是否合法由保存时的 validate_settings 把关；类型不对（次数写成字符串、行里缺 task、rows 不是列表）
    仍会让加载失败，与上游其他字段一样。
    state、command、command_seq 是界面与引擎之间的运行控制字段，只由命令接口和引擎写入，不属于用户设置。
    """
    rows: list[OrchestraRow] = []
    after_finish: str = 'stop'                # stop / loop
    total_minutes_cap: int | None = None
    default_rest_minutes: int = 5
    rest_jitter: bool = False
    resume_policy: str = 'remaining'          # remaining / rerun
    cut_in_tasks: list[str] = ['MetaDemon', 'ActivityShikigami']
    state: str = 'idle'                       # idle / running / paused
    command: str = ''
    command_seq: int = 0


@dataclass(frozen=True)
class RowError:
    """保存校验的一条错误：index 是行号（从 0 起），设置项的错误为 -1；field 是出错的属性名。"""
    index: int
    field: str
    message: str


def _sub_in_options(spec: 'TaskSpec', position: int, raw: str) -> bool:
    return position < len(spec.subs) and raw in spec.subs[position].options


def _row_errors(index: int, row: OrchestraRow, spec: 'TaskSpec') -> list[RowError]:
    errors: list[RowError] = []
    if row.sub1 is None:
        if row.task == 'ActivityShikigami':  # 爬塔的次数字段按类型分开存放，不选类型就没有落点
            errors.append(RowError(index, 'sub1', '请选择爬塔类型'))
    elif not _sub_in_options(spec, 0, row.sub1):
        errors.append(RowError(index, 'sub1', '子项不在可选范围内'))
    if row.sub2 is not None and not _sub_in_options(spec, 1, row.sub2):
        errors.append(RowError(index, 'sub2', '子项不在可选范围内'))

    if row.max_count is not None:
        if spec.count_path is None:
            errors.append(RowError(index, 'max_count', '该任务没有次数上限'))
        elif spec.count_max is not None:
            if not 1 <= row.max_count <= spec.count_max:
                errors.append(RowError(index, 'max_count', f'次数需在 1–{spec.count_max} 之间'))
        elif row.max_count < 1:
            errors.append(RowError(index, 'max_count', '次数需不小于 1'))

    if row.max_minutes is not None:
        if spec.time_path is None:
            errors.append(RowError(index, 'max_minutes', '该任务没有时长上限'))
        elif not 1 <= row.max_minutes <= 1439:
            errors.append(RowError(index, 'max_minutes', '分钟需在 1–1439 之间'))

    if row.rest_minutes is not None and not 0 <= row.rest_minutes <= 1439:
        errors.append(RowError(index, 'rest_minutes', '休息需在 0–1439 之间'))
    return errors


def validate_rows(rows: list[OrchestraRow]) -> list[RowError]:
    """逐行校验，返回全部错误，空列表表示通过。任务不在映射表里的行只报这一条，不再查它的其他项。"""
    from module.orchestra.mapping import TASK_SPECS
    errors: list[RowError] = []
    for index, row in enumerate(rows):
        spec = TASK_SPECS.get(row.task)
        if spec is None:
            errors.append(RowError(index, 'task', '不支持编排的任务'))
        else:
            errors.extend(_row_errors(index, row, spec))
    return errors


def _scheduled_commands() -> set[str]:
    """上游调度器能排的任务命令名：ConfigModel 上类型带 scheduler 的字段，命令名取类型的类名（同上游 Function.command）。"""
    from module.config.config_model import ConfigModel   # 函数内导入，原因见文件顶部
    return {field.annotation.__name__ for field in ConfigModel.model_fields.values()
            if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)
            and 'scheduler' in field.annotation.model_fields}


def validate_settings(o: Orchestra) -> list[RowError]:
    """校验用户可改的设置项，再接上 validate_rows 的结果。

    设置项的错误 index 为 -1，field 是属性名。state、command、command_seq 不查，原因见 Orchestra 的说明。
    插队名单可以是任何上游调度器能排的任务（比如真蛇），不限于映射表里的 17 个；Restart、SoulsTidy 写不写都永远放行。
    """
    invalid: list[str] = []
    if o.after_finish not in ('stop', 'loop'):
        invalid.append('after_finish')
    if o.total_minutes_cap is not None and o.total_minutes_cap < 1:
        invalid.append('total_minutes_cap')
    if not 0 <= o.default_rest_minutes <= 1439:
        invalid.append('default_rest_minutes')
    if o.resume_policy not in ('remaining', 'rerun'):
        invalid.append('resume_policy')
    if o.cut_in_tasks and not set(o.cut_in_tasks) <= _scheduled_commands():
        invalid.append('cut_in_tasks')
    return [RowError(-1, name, '取值不合法') for name in invalid] + validate_rows(o.rows)
