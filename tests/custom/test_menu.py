import logging

from module.config.config_menu import ConfigMenu
from module.config.config_menu_custom import custom_menu, ORCHESTRA_MENU_KEY
from module.config.config_model import ConfigModel
from module.config.utils import convert_to_underscore

SEVEN = ['Overview', '编排', 'Soul Zones', 'Liver Emperor Exclusive', 'RealmRaid', 'Activity Task', 'Script']

def test_gui_menu_list_has_seven_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('OAS_MENU_SHOW_ALL', raising=False)
    menu = ConfigMenu().gui_menu_list
    assert list(menu) == SEVEN
    assert menu['Soul Zones'] == ['Orochi', 'Sougenbi', 'FallenSun', 'EternitySea', 'SixRealms', 'OtherWorldTwilight']
    assert menu['Liver Emperor Exclusive'] == ['BondlingFairyland', 'EvoZone', 'GoryouRealm', 'Exploration', 'Hyakkiyakou', 'HeroTest', 'FindJade', 'MemoryScrolls']
    assert menu['RealmRaid'] == [] and menu['编排'] == [] and menu['Overview'] == []
    assert menu['Activity Task'] == ['ActivityShikigami', 'MetaDemon']
    assert menu['Script'] == ['Script', 'Restart', 'GlobalGame']

def _assert_full_menu(menu):
    # 开关打开：上游分组全部回来，编排紧跟 Overview，TaskList/Tools 照旧被 gui_menu_list 去掉
    assert menu[0] == 'Overview'
    assert menu[1] == '编排'
    for group in ('Daily Task', 'Guild', 'Weekly Task'):
        assert group in menu
    for group in ('TaskList', 'Tools'):
        assert group not in menu

def test_show_all_via_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('OAS_MENU_SHOW_ALL', '1')
    _assert_full_menu(list(ConfigMenu().gui_menu_list))

def test_show_all_via_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('OAS_MENU_SHOW_ALL', raising=False)
    (tmp_path / '.env').write_text('OAS_MENU_SHOW_ALL=1\n', encoding='utf-8')
    _assert_full_menu(list(ConfigMenu().gui_menu_list))

def test_show_all_keeps_new_upstream_groups():
    upstream = {'Overview': [], 'TaskList': [], 'Script': ['Script'], 'New Group': ['X'], 'Tools': ['Image Rule']}
    menu = custom_menu(upstream, show_all=True)
    assert list(menu) == ['Overview', ORCHESTRA_MENU_KEY, 'TaskList', 'Script', 'New Group', 'Tools']
    assert 'New Group' not in custom_menu(upstream, show_all=False)

def test_custom_menu_keeps_tasklist_and_tools():
    menu = custom_menu({'Overview': [], 'TaskList': [], 'Tools': ['Image Rule']}, show_all=False)
    assert menu['TaskList'] == [] and menu['Tools'] == ['Image Rule']

def test_closed_menu_does_not_invent_missing_keys():
    # 上游没有的键不凭空生成，保持与上游一致（缺了就让上游自己的 del 去报）
    menu = custom_menu({'Overview': []}, show_all=False)
    assert 'TaskList' not in menu and 'Tools' not in menu
    menu = custom_menu({'Overview': [], 'Tools': ['Image Rule']}, show_all=False)
    assert 'TaskList' not in menu and menu['Tools'] == ['Image Rule']

def _leaves(menu):
    # 叶子：值为空列表的顶层键，或分组列表里的每一项
    return {leaf for key, items in menu.items() for leaf in (items or [key])}

def test_closed_menu_leaves_exist_upstream(tmp_path, monkeypatch):
    # 上游改名或删任务时，收敛菜单不能悄悄留下点不开的死条目
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('OAS_MENU_SHOW_ALL', raising=False)
    closed = _leaves(ConfigMenu().gui_menu_list) - {'Overview', ORCHESTRA_MENU_KEY}
    monkeypatch.setenv('OAS_MENU_SHOW_ALL', '1')  # 开关打开时就是上游菜单，只多一个编排
    upstream = _leaves(ConfigMenu().gui_menu_list) - {ORCHESTRA_MENU_KEY}
    assert closed
    no_field = sorted(leaf for leaf in closed if convert_to_underscore(leaf) not in ConfigModel.model_fields)
    assert not no_field, f'ConfigModel 里没有对应字段：{no_field}'
    assert not closed - upstream, f'上游菜单里已没有：{sorted(closed - upstream)}'

def test_unreadable_env_file_defaults_to_off(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('OAS_MENU_SHOW_ALL', raising=False)
    (tmp_path / '.env').mkdir()  # .env 是目录：读取必抛 OSError（Windows 上为 PermissionError）
    menu = custom_menu({'Overview': [], 'TaskList': [], 'Tools': []})
    assert set(menu) == set(SEVEN) | {'TaskList', 'Tools'}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert '.env' in warnings[0].getMessage() and 'Error' in warnings[0].getMessage()
