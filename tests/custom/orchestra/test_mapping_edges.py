"""任务映射（module.orchestra.mapping）的补充用例：钉住 Task 1 的控制员裁定、目录的完整形状，
以及枚举改从 ConfigModel 的类型标注解析之后，字段对不上时的退路。

test_mapping.py 照简报原样保留，这些用例单独放在这里。
"""
import ast
import logging
from datetime import datetime, time
from pathlib import Path

import pytest

from module.config.config_model import ConfigModel
from module.orchestra import mapping
from module.orchestra.engine import next_row_index
from module.orchestra.mapping import (TASK_SPECS, apply_row, catalog, count_path_for, restore_params,
                                      serialize_value)
from module.orchestra.model import OrchestraRow, RowError, validate_rows
from tasks.BondlingFairyland.config import BondlingClass, BondlingMode
from tasks.EternitySea.config import Layer as EternitySeaLayer
from tasks.EvoZone.config import KirinType, Layer as EvoZoneLayer
from tasks.Exploration.config import ExplorationLevel
from tasks.FallenSun.config import Layer as FallenSunLayer
from tasks.GoryouRealm.config import GoryouClass
from tasks.HeroTest.config import Layer as HeroTestLayer
from tasks.MemoryScrolls.config import ScrollNumber
from tasks.Orochi.config import Layer as OrochiLayer
from tasks.Sougenbi.config import SougenbiClass

MAPPING_PY = Path(mapping.__file__)
ORDER = list(TASK_SPECS)   # 17 个任务，按菜单顺序；test_mapping.py 钉住了具体的顺序
ENUM_SUBS = {   # 测试里可以按名字导入上游枚举，映射模块自己不行
    'Orochi': (OrochiLayer,), 'Sougenbi': (SougenbiClass,), 'FallenSun': (FallenSunLayer,),
    'EternitySea': (EternitySeaLayer,), 'BondlingFairyland': (BondlingMode, BondlingClass),
    'EvoZone': (KirinType, EvoZoneLayer), 'GoryouRealm': (GoryouClass,), 'Exploration': (ExplorationLevel,),
    'HeroTest': (HeroTestLayer,), 'MemoryScrolls': (ScrollNumber,),
}


def oas_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == 'oas' and r.levelno == logging.WARNING]


def failing_at(monkeypatch, bad_path: str, result=mapping._MISSING) -> None:
    """让 _annotation 在 bad_path 上返回 result（缺省为查不到），其余路径照常解析：模拟上游改了某个字段。"""
    real = mapping._annotation
    monkeypatch.setattr(mapping, '_annotation', lambda path: result if path == bad_path else real(path))


# ---------- 枚举从类型标注解析（I5） ----------

def test_mapping_does_not_import_upstream_task_modules():
    tree = ast.parse(MAPPING_PY.read_text(encoding='utf-8'))
    imported = [n.module for n in tree.body if isinstance(n, ast.ImportFrom)]
    imported += [alias.name for n in tree.body if isinstance(n, ast.Import) for alias in n.names]
    assert not [name for name in imported if name and name.startswith('tasks')]   # 上游改名不能让本模块导入失败


def test_enum_subs_resolve_to_the_upstream_enums():
    for command, enums in ENUM_SUBS.items():
        subs = TASK_SPECS[command].subs
        assert [sub.to_value for sub in subs] == list(enums), command
        assert [sub.options for sub in subs] == [tuple(m.value for m in enum) for enum in enums], command
        assert [sub.labels for sub in subs] == [sub.options for sub in subs], command


def test_every_task_resolves_today():
    assert mapping.build_task_specs() == TASK_SPECS and len(TASK_SPECS) == 17


@pytest.mark.parametrize('bad_path, result', [
    ('orochi.orochi_config.layer', mapping._MISSING),         # 枚举字段改名或删了
    ('orochi.orochi_config.layer', str),                      # 字段还在，但不再是枚举
    ('orochi.orochi_config.limit_count', mapping._MISSING),   # 次数字段对不上
    ('orochi.orochi_config.limit_time', mapping._MISSING),    # 时长字段对不上
    ('orochi.scheduler.next_run', mapping._MISSING),          # 运行时要写的 next_run 对不上
], ids=['enum-missing', 'not-an-enum', 'count', 'time', 'next-run'])
def test_a_task_that_no_longer_resolves_is_left_out_with_one_warning(monkeypatch, caplog, bad_path, result):
    failing_at(monkeypatch, bad_path, result)
    with caplog.at_level(logging.WARNING):
        specs = mapping.build_task_specs()
    assert list(specs) == [command for command in ORDER if command != 'Orochi']   # 其余照旧，顺序不变
    warnings = oas_warnings(caplog)
    assert len(warnings) == 1 and 'Orochi' in warnings[0].getMessage() and bad_path in warnings[0].getMessage()


def test_every_climb_type_count_field_is_checked(monkeypatch):
    failing_at(monkeypatch, 'activity_shikigami.general_climb.ap100_limit')   # 只有最后一种类型对不上
    assert 'ActivityShikigami' not in mapping.build_task_specs()


def test_an_unexpected_error_while_resolving_never_escapes(monkeypatch, caplog):
    def broken(path):
        raise TypeError('标注长得不认识')
    monkeypatch.setattr(mapping, '_annotation', broken)
    with caplog.at_level(logging.WARNING):
        assert mapping.build_task_specs() == {}   # 导入时运行：一个都对不上，也只是空表，不抛
    assert len(oas_warnings(caplog)) == len(ORDER)


def test_rows_of_a_task_left_out_fail_validation_and_are_skipped(monkeypatch):
    failing_at(monkeypatch, 'orochi.orochi_config.layer')
    monkeypatch.setattr(mapping, 'TASK_SPECS', mapping.build_task_specs())   # 模拟导入时就没解析出来
    rows = [OrchestraRow(task='Orochi', max_count=3), OrchestraRow(task='GoryouRealm')]
    assert validate_rows(rows) == [RowError(0, 'task', '不支持编排的任务')]
    assert next_row_index(rows, 0) == 1


# ---------- 任务目录的形状（N4） ----------

def test_catalog_entry_shape():
    entries = {entry['command']: entry for entry in catalog()}
    assert list(entries) == ORDER
    assert entries['Orochi'] == {
        'command': 'Orochi', 'title': '御魂·八岐大蛇', 'count_max': None, 'has_count': True, 'has_time': True,
        'subs': [{'options': [m.value for m in OrochiLayer], 'labels': [m.value for m in OrochiLayer],
                  'required': False}],
    }
    assert entries['ActivityShikigami'] == {
        'command': 'ActivityShikigami', 'title': '限时·爬塔', 'count_max': None, 'has_count': True, 'has_time': True,
        'subs': [{'options': ['pass', 'ap', 'boss', 'ap100'], 'labels': ['门票', '体力', 'Boss', '100体'],
                  'required': True}],
    }
    assert entries['RealmRaid'] == {'command': 'RealmRaid', 'title': '个人突破', 'count_max': 30,
                                    'has_count': True, 'has_time': False, 'subs': []}
    assert entries['MetaDemon'] == {
        'command': 'MetaDemon', 'title': '限时·超鬼王', 'count_max': None, 'has_count': False, 'has_time': False,
        'subs': [{'options': ['on', 'off'], 'labels': ['开', '关'], 'required': False}],
    }
    for entry in entries.values():
        assert set(entry) == {'command', 'title', 'count_max', 'has_count', 'has_time', 'subs'}
        assert all(set(sub) == {'options', 'labels', 'required'} for sub in entry['subs'])


def test_only_the_climb_type_is_required_and_validation_agrees():
    required = [(entry['command'], i) for entry in catalog() for i, sub in enumerate(entry['subs']) if sub['required']]
    assert required == [('ActivityShikigami', 0)]
    for command in ORDER:   # 不选子项、其余也不填：必选的才报错
        errors = validate_rows([OrchestraRow(task=command)])
        assert bool(errors) == ((command, 0) in required), command


# ---------- Task 1 的控制员裁定 ----------

def test_none_leaves_the_task_fields_alone():
    # 用 == 比整个模型，不用 model_dump：上游默认值里有写成字符串的整数字段，dump 会刷 pydantic 的序列化警告
    model = ConfigModel()
    assert apply_row(model, OrchestraRow(task='Orochi')) == {}   # 一项都没填：不写、不记原值
    assert model == ConfigModel()
    saved = apply_row(model, OrchestraRow(task='Orochi', max_minutes=9))   # 只填了分钟：只动时长
    assert list(saved) == ['orochi.orochi_config.limit_time']
    config = model.orochi.orochi_config
    assert (config.layer, config.limit_count, config.limit_time) == (OrochiLayer.ELEVEN, 30, time(0, 9))


def test_realm_raid_count_cap_is_30():
    assert TASK_SPECS['RealmRaid'].count_max == 30
    assert validate_rows([OrchestraRow(task='RealmRaid', max_count=30)]) == []
    assert all(spec.count_max is None for command, spec in TASK_SPECS.items() if command != 'RealmRaid')


def test_two_dropdown_tasks_keep_their_sub_order():
    assert [sub.path for sub in TASK_SPECS['BondlingFairyland'].subs] == [
        'bondling_config.bondling_mode', 'bondling_config.bondling_stone_class']
    assert [sub.path for sub in TASK_SPECS['EvoZone'].subs] == ['evo_zone_config.kirin_type', 'evo_zone_config.layer']
    model = ConfigModel()
    apply_row(model, OrchestraRow(task='EvoZone', sub1=KirinType.WINDKIRIN.value, sub2=EvoZoneLayer.TEN.value))
    assert (model.evo_zone.evo_zone_config.kirin_type, model.evo_zone.evo_zone_config.layer) == (
        KirinType.WINDKIRIN, EvoZoneLayer.TEN)


def test_climb_count_path_needs_a_type():
    spec = TASK_SPECS['ActivityShikigami']
    assert count_path_for(spec, None) is None
    assert count_path_for(TASK_SPECS['Orochi'], None) == 'orochi_config.limit_count'   # 不是模板：与子项无关


@pytest.mark.parametrize('climb_type, field', [('boss', 'boss_limit'), ('ap100', 'ap100_limit')])
def test_boss_and_ap100_expand_to_their_own_count_fields(climb_type, field):
    spec = TASK_SPECS['ActivityShikigami']
    assert count_path_for(spec, climb_type) == f'general_climb.{field}'
    model = ConfigModel()
    saved = apply_row(model, OrchestraRow(task='ActivityShikigami', sub1=climb_type, max_count=7))
    climb = model.activity_shikigami.general_climb
    assert (climb.run_sequence, getattr(climb, field)) == (climb_type, 7)
    restore_params(model, saved)
    assert model == ConfigModel()


# ---------- datetime 的序列化（I1：行开始前记下 next_run 的原值） ----------

def test_datetime_original_round_trips_through_json_values():
    original = datetime(2026, 11, 17, 10, 0, 5, 123456)
    raw = serialize_value(original)
    assert raw == '2026-11-17T10:00:05.123456' and isinstance(raw, str)
    model = ConfigModel()
    model.orochi.scheduler.next_run = datetime(2026, 11, 18, 9, 0)   # 行运行期间被改成别的
    restore_params(model, {'orochi.scheduler.next_run': raw})
    assert model.orochi.scheduler.next_run == original
