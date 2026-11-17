from module.config.config_model import ConfigModel
from module.orchestra.model import Orchestra, OrchestraRow, RowError, validate_rows, validate_settings

def test_realm_raid_count_over_30_rejected():
    assert validate_rows([OrchestraRow(task='RealmRaid', max_count=31)]) == [RowError(0, 'max_count', '次数需在 1–30 之间')]

def test_minutes_on_task_without_time_field_rejected():
    assert validate_rows([OrchestraRow(task='FindJade', max_minutes=10)]) == [RowError(0, 'max_minutes', '该任务没有时长上限')]

def test_activity_requires_type():
    assert validate_rows([OrchestraRow(task='ActivityShikigami')]) == [RowError(0, 'sub1', '请选择爬塔类型')]

def test_unknown_task_and_bad_ranges():
    errs = validate_rows([OrchestraRow(task='Nian'), OrchestraRow(task='Orochi', max_minutes=1440, rest_minutes=-1)])
    assert RowError(0, 'task', '不支持编排的任务') in errs
    assert RowError(1, 'max_minutes', '分钟需在 1–1439 之间') in errs
    assert RowError(1, 'rest_minutes', '休息需在 0–1439 之间') in errs

def test_settings_values_validated():
    assert RowError(-1, 'after_finish', '取值不合法') in validate_settings(Orchestra(after_finish='forever'))

def test_bad_rows_do_not_break_config_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config').mkdir()
    data = ConfigModel().model_dump()
    data['orchestra'] = {'rows': [{'task': 'Nian', 'max_count': -5}], 'state': 'weird'}
    ConfigModel.write_json('t', data)
    assert ConfigModel('t').orchestra.rows[0].task == 'Nian'   # ConfigModel.__init__ 只接受 config_name

def test_orchestra_is_ignored_by_scheduler():
    data = ConfigModel().model_dump()
    assert 'scheduler' not in data['orchestra']
