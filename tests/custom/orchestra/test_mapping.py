from datetime import time
from module.config.config_model import ConfigModel
from module.orchestra.mapping import TASK_SPECS, apply_row, restore_params, minutes_to_time
from module.orchestra.model import OrchestraRow

ORDER = ['Orochi', 'Sougenbi', 'FallenSun', 'EternitySea', 'SixRealms', 'OtherWorldTwilight',
         'BondlingFairyland', 'EvoZone', 'GoryouRealm', 'Exploration', 'Hyakkiyakou', 'HeroTest',
         'FindJade', 'MemoryScrolls', 'RealmRaid', 'ActivityShikigami', 'MetaDemon']
WITH_SUB = {'Orochi', 'Sougenbi', 'FallenSun', 'EternitySea', 'BondlingFairyland', 'EvoZone', 'GoryouRealm',
            'Exploration', 'HeroTest', 'MemoryScrolls', 'ActivityShikigami', 'MetaDemon'}

def test_catalog_order_and_subitems():
    assert list(TASK_SPECS) == ORDER
    assert {c for c, s in TASK_SPECS.items() if s.subs} == WITH_SUB
    assert len(TASK_SPECS['EvoZone'].subs) == 2 and len(TASK_SPECS['BondlingFairyland'].subs) == 2

def test_minutes_to_time():
    assert minutes_to_time(20) == time(0, 20) and minutes_to_time(90) == time(1, 30) and minutes_to_time(1439) == time(23, 59)

def test_apply_orochi_row_sets_native_fields():
    m = ConfigModel()
    apply_row(m, OrchestraRow(task='Orochi', sub1='悲鸣', max_count=30, max_minutes=20))
    c = m.orochi.orochi_config
    assert (c.layer.value, c.limit_count, c.limit_time) == ('悲鸣', 30, time(0, 20))

def test_apply_activity_row_writes_type_limit_and_sequence():
    m = ConfigModel()
    apply_row(m, OrchestraRow(task='ActivityShikigami', sub1='ap', max_count=120, max_minutes=45))
    g = m.activity_shikigami.general_climb
    assert (g.run_sequence, g.ap_limit, g.limit_time) == ('ap', 120, time(0, 45))

def test_apply_overrides_for_remaining_limits():
    m = ConfigModel()
    apply_row(m, OrchestraRow(task='GoryouRealm', max_count=50, max_minutes=15), count=12, minutes=8)
    assert (m.goryou_realm.goryou_config.limit_count, m.goryou_realm.goryou_config.limit_time) == (12, time(0, 8))

def test_apply_then_restore_roundtrip_for_every_task():
    m = ConfigModel()
    before = m.model_dump()
    for spec in TASK_SPECS.values():
        row = OrchestraRow(task=spec.command,
                           sub1=spec.subs[0].options[0] if spec.subs else None,
                           sub2=spec.subs[1].options[0] if len(spec.subs) > 1 else None,
                           max_count=1 if spec.count_path else None,
                           max_minutes=7 if spec.time_path else None)
        restore_params(m, apply_row(m, row))
    assert m.model_dump() == before
