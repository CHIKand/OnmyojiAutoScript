import logging

from module.config.config_menu import ConfigMenu
from module.config.config_menu_custom import custom_menu, ORCHESTRA_MENU_KEY

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

def test_show_all_keeps_new_upstream_groups():
    upstream = {'Overview': [], 'TaskList': [], 'Script': ['Script'], 'New Group': ['X'], 'Tools': ['Image Rule']}
    menu = custom_menu(upstream, show_all=True)
    assert list(menu) == ['Overview', ORCHESTRA_MENU_KEY, 'TaskList', 'Script', 'New Group', 'Tools']
    assert 'New Group' not in custom_menu(upstream, show_all=False)

def test_custom_menu_keeps_tasklist_and_tools():
    menu = custom_menu({'Overview': [], 'TaskList': [], 'Tools': ['Image Rule']}, show_all=False)
    assert menu['TaskList'] == [] and menu['Tools'] == ['Image Rule']

def test_unreadable_env_file_defaults_to_off(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('OAS_MENU_SHOW_ALL', raising=False)
    (tmp_path / '.env').mkdir()  # .env 是目录：读取必抛 OSError（Windows 上为 PermissionError）
    menu = custom_menu({'Overview': [], 'TaskList': [], 'Tools': []})
    assert set(menu) == set(SEVEN) | {'TaskList', 'Tools'}
    assert any(r.levelno == logging.WARNING and '.env' in r.getMessage() for r in caplog.records)
