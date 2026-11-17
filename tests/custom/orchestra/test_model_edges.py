"""编排配置的保存校验（module.orchestra.model）补充用例：Task 2 没覆盖到的分支，以及插队名单的取值范围。

test_model.py 照简报原样保留，这些用例单独放在这里。
"""
import pytest

from module.orchestra.model import Orchestra, OrchestraRow, RowError, validate_rows, validate_settings
from tasks.EvoZone.config import KirinType, Layer as EvoZoneLayer


# ---------- 行的校验分支 ----------

@pytest.mark.parametrize('row', [
    OrchestraRow(task='Orochi', sub1='十三层'),          # 不在该任务的选项里
    OrchestraRow(task='SixRealms', sub1='壹层'),         # 这个任务没有子项
    OrchestraRow(task='ActivityShikigami', sub1='Tower'),
], ids=['not-an-option', 'task-without-subs', 'climb-unknown-type'])
def test_sub1_out_of_range(row):
    assert validate_rows([row]) == [RowError(0, 'sub1', '子项不在可选范围内')]


@pytest.mark.parametrize('row', [
    OrchestraRow(task='EvoZone', sub1=KirinType.FIREKIRIN.value, sub2='十一层'),   # 第二格不在选项里
    OrchestraRow(task='Orochi', sub2='悲鸣'),                                       # 这个任务只有一格
], ids=['not-an-option', 'only-one-dropdown'])
def test_sub2_out_of_range(row):
    assert validate_rows([row]) == [RowError(0, 'sub2', '子项不在可选范围内')]


def test_both_dropdowns_valid():
    assert validate_rows([OrchestraRow(task='EvoZone', sub1=KirinType.FIREKIRIN.value,
                                       sub2=EvoZoneLayer.TEN.value)]) == []


@pytest.mark.parametrize('task', ['FindJade', 'MemoryScrolls', 'MetaDemon'])
def test_count_on_a_task_without_a_count_field(task):
    assert RowError(0, 'max_count', '该任务没有次数上限') in validate_rows([OrchestraRow(task=task, max_count=3)])


@pytest.mark.parametrize('count', [0, -5])
def test_count_must_be_at_least_one(count):
    assert validate_rows([OrchestraRow(task='Orochi', max_count=count)]) == [
        RowError(0, 'max_count', '次数需不小于 1')]


def test_count_without_a_declared_cap_has_no_upper_bound():
    assert validate_rows([OrchestraRow(task='Orochi', max_count=100000)]) == []


@pytest.mark.parametrize('minutes, ok', [(0, False), (1, True), (1439, True), (2000, False)])
def test_minutes_range(minutes, ok):
    errors = validate_rows([OrchestraRow(task='Orochi', max_minutes=minutes)])
    assert errors == ([] if ok else [RowError(0, 'max_minutes', '分钟需在 1–1439 之间')])


@pytest.mark.parametrize('rest, ok', [(0, True), (1439, True), (1440, False), (-1, False)])
def test_rest_range(rest, ok):
    errors = validate_rows([OrchestraRow(task='Orochi', rest_minutes=rest)])
    assert errors == ([] if ok else [RowError(0, 'rest_minutes', '休息需在 0–1439 之间')])


def test_an_unknown_task_skips_the_other_checks():
    row = OrchestraRow(task='Nian', sub1='x', sub2='y', max_count=-5, max_minutes=5000, rest_minutes=-1)
    assert validate_rows([row]) == [RowError(0, 'task', '不支持编排的任务')]   # 只报这一条


def test_errors_carry_the_row_index():
    rows = [OrchestraRow(task='Orochi'), OrchestraRow(task='Orochi', max_count=0), OrchestraRow(task='Nian')]
    assert validate_rows(rows) == [RowError(1, 'max_count', '次数需不小于 1'), RowError(2, 'task', '不支持编排的任务')]


# ---------- 设置项 ----------

def test_default_settings_are_valid():
    assert validate_settings(Orchestra()) == []


@pytest.mark.parametrize('field, value', [
    ('after_finish', 'forever'),
    ('total_minutes_cap', 0),
    ('default_rest_minutes', -1),
    ('default_rest_minutes', 1440),
    ('resume_policy', 'whatever'),
    ('cut_in_tasks', ['NoSuchTask']),
])
def test_each_invalid_setting_is_reported(field, value):
    assert validate_settings(Orchestra(**{field: value})) == [RowError(-1, field, '取值不合法')]


@pytest.mark.parametrize('field, value', [
    ('after_finish', 'loop'), ('total_minutes_cap', None), ('total_minutes_cap', 1), ('default_rest_minutes', 0),
    ('default_rest_minutes', 1439), ('resume_policy', 'rerun'), ('cut_in_tasks', []),
])
def test_boundary_settings_are_accepted(field, value):
    assert validate_settings(Orchestra(**{field: value})) == []


def test_control_fields_are_not_validated():
    assert validate_settings(Orchestra(state='weird', command='dance', command_seq=-3)) == []


def test_settings_errors_come_before_row_errors():
    o = Orchestra(after_finish='forever', rows=[OrchestraRow(task='Nian')])
    assert validate_settings(o) == [RowError(-1, 'after_finish', '取值不合法'), RowError(0, 'task', '不支持编排的任务')]


# ---------- 插队名单：任何上游调度器能排的任务（I2） ----------

@pytest.mark.parametrize('task', ['SoulsTidy', 'TrueOrochi', 'Restart', 'Nian', 'RealmRaid', 'MetaDemon'])
def test_cut_in_accepts_any_scheduled_task(task):
    assert validate_settings(Orchestra(cut_in_tasks=['MetaDemon', task])) == []


@pytest.mark.parametrize('task', ['Script', 'GlobalGame', 'Orchestra', 'NoSuchTask', '', 'souls_tidy'])
def test_cut_in_rejects_what_the_scheduler_never_runs(task):
    # Script、GlobalGame、Orchestra 是 ConfigModel 上没有 scheduler 的字段；任务名按命令名（大驼峰）比对
    assert validate_settings(Orchestra(cut_in_tasks=[task])) == [RowError(-1, 'cut_in_tasks', '取值不合法')]
