"""等搭档的数据层：搭档设置与保存校验、组队身份字段的映射、进度文件里的组队字段、GET 返回的搭档候选。

接口的用例同 test_router.py：处理函数用 asyncio.run 直接调，instance_dir、default_config 来自 conftest。
"""
import asyncio
import logging
from dataclasses import replace
from datetime import datetime

import pytest

from module.config.config_model import ConfigModel
from module.orchestra import mapping
from module.orchestra.mapping import TASK_SPECS, team_tasks
from module.orchestra.model import Orchestra, RowError, validate_settings
from module.orchestra.progress import (Progress, clear_team_wait, clear_team_wait_file, load_progress, progress_path,
                                       save_progress)
from module.server.orchestra_router import get_orchestra, put_orchestra
from tasks.BondlingFairyland.config import UserStatus as BondlingStatus
from tasks.Exploration.config import UserStatus as ExplorationStatus
from tasks.Orochi.config import UserStatus

T0 = datetime(2026, 11, 26, 10, 0)


def test_partner_defaults():
    o = Orchestra()
    assert (o.partner, o.partner_wait_minutes) == ('', 30)


@pytest.mark.parametrize('minutes, ok', [(0, False), (1, True), (1439, True), (1440, False)])
def test_partner_wait_minutes_range(minutes, ok):
    errors = validate_settings(Orchestra(partner_wait_minutes=minutes))
    assert (RowError(-1, 'partner_wait_minutes', '取值不合法') not in errors) is ok


def test_partner_must_be_another_existing_instance():
    assert validate_settings(Orchestra(partner='t'), {'t', 'u'}, 't') == [RowError(-1, 'partner', '搭档不能是自己')]
    assert validate_settings(Orchestra(partner='x'), {'t', 'u'}, 't') == [RowError(-1, 'partner', '没有这个实例')]
    assert validate_settings(Orchestra(partner='u'), {'t', 'u'}, 't') == []
    assert validate_settings(Orchestra(partner='x')) == []          # 不给实例列表就不查


def test_team_status_paths_resolve():
    assert {c: s.team_status_path for c, s in TASK_SPECS.items() if s.team_status_path} == {
        'Orochi': 'orochi_config.user_status', 'EternitySea': 'eternity_sea_config.user_status',
        'FallenSun': 'fallen_sun_config.user_status', 'EvoZone': 'evo_zone_config.user_status',
        'Exploration': 'exploration_config.user_status', 'BondlingFairyland': 'bondling_config.user_status',
        'OtherWorldTwilight': 'other_world_twilight_config.user_status'}


def test_team_tasks_follow_user_status():
    model = ConfigModel()
    assert team_tasks(model) == frozenset({'Orochi', 'EternitySea', 'FallenSun', 'EvoZone', 'OtherWorldTwilight'})
    model.orochi.orochi_config.user_status = UserStatus.ALONE                       # tasks.Orochi.config
    model.exploration.exploration_config.user_status = ExplorationStatus.MEMBER     # tasks.Exploration.config
    model.bondling_fairyland.bondling_config.user_status = BondlingStatus.handoff1  # 换手不算
    assert team_tasks(model) == frozenset({'EternitySea', 'FallenSun', 'EvoZone', 'OtherWorldTwilight', 'Exploration'})


def test_missing_team_status_path_keeps_the_task(caplog):
    orochi = next(s for s in mapping._DEFINITIONS if s.command == 'Orochi')
    resolved = mapping._resolve(replace(orochi, team_status_path='orochi_config.no_such_field'))
    assert resolved.command == 'Orochi' and resolved.team_status_path is None
    assert any('组队身份字段' in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_team_fields_roundtrip(tmp_path):
    p = Progress(team_wait_since=T0, team_wait_ordinal=2, team_wait_task='Orochi', team_alive_at=T0,
                 team_met_at=T0, team_met_ordinal=1, team_met_task='Orochi', team_partner='山川知异路',
                 team_row_ordinal=2)
    path = progress_path('t', tmp_path)
    save_progress(path, p)
    assert load_progress(path) == p


def test_old_progress_file_gets_team_defaults(tmp_path):
    path = progress_path('t', tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"phase": "resting", "row_index": 1}', encoding='utf-8')
    p = load_progress(path)
    assert (p.team_wait_since, p.team_met_at, p.team_partner, p.team_row_ordinal) == (None, None, '', None)


def test_clear_team_wait():
    p = Progress(team_wait_since=T0, team_wait_ordinal=1, team_wait_task='Orochi', team_alive_at=T0, team_met_at=T0)
    assert clear_team_wait(p) is True
    assert (p.team_wait_since, p.team_wait_ordinal, p.team_wait_task, p.team_alive_at, p.team_met_at) == \
        (None, None, None, None, T0)
    assert clear_team_wait(p) is False


def test_clear_team_wait_file(tmp_path):
    path = progress_path('t', tmp_path)
    assert clear_team_wait_file(path) is False and not path.exists()             # 没有文件：不新建
    save_progress(path, Progress(phase='ready'))
    before = path.read_bytes()
    assert clear_team_wait_file(path) is False and path.read_bytes() == before   # 没有等待记录：不写
    save_progress(path, Progress(phase='ready', row_index=2, team_wait_since=T0, team_wait_ordinal=1,
                                 team_wait_task='Orochi', team_alive_at=T0, team_met_at=T0))
    assert clear_team_wait_file(path) is True
    p = load_progress(path)
    assert (p.team_wait_since, p.team_wait_ordinal, p.team_wait_task, p.team_alive_at) == (None,) * 4
    assert (p.team_met_at, p.phase, p.row_index) == (T0, 'ready', 2)              # 会合记录和其余部分不动
    path.write_text('{坏了', encoding='utf-8')
    assert clear_team_wait_file(path) is False
    assert path.read_text(encoding='utf-8') == '{坏了'                            # 损坏的文件不拿默认进度覆盖


def test_put_saves_partner_settings(instance_dir, default_config):
    ConfigModel.write_json('u', default_config)
    result = asyncio.run(put_orchestra('t', {'partner': 'u', 'partner_wait_minutes': 45}))
    assert result['ok'] is True
    o = ConfigModel('t').orchestra
    assert (o.partner, o.partner_wait_minutes) == ('u', 45)


def test_put_rejects_unknown_partner(instance_dir):
    result = asyncio.run(put_orchestra('t', {'partner': 'gone'}))
    assert result['ok'] is False
    assert result['errors'] == [{'index': -1, 'field': 'partner', 'message': '没有这个实例'}]
    assert ConfigModel('t').orchestra.partner == ''


def test_get_lists_other_instances_as_partners(instance_dir, default_config):
    ConfigModel.write_json('山川知异路', default_config)
    ConfigModel.write_json('a', default_config)
    assert asyncio.run(get_orchestra('t'))['partners'] == ['a', '山川知异路']
