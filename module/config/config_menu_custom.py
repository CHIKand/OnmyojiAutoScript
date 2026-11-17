from module.config.dotenv import read_env

# 编排页的菜单键，直接用中文、不另加翻译；显示效果待 M2b 目检确认。
ORCHESTRA_MENU_KEY = '编排'

# 开关关闭时 OASX 看到的菜单，顺序与内容固定；其余键名沿用上游已有的翻译。
_CUSTOM_MENU: dict[str, list[str]] = {
    'Overview': [],
    ORCHESTRA_MENU_KEY: [],
    'Soul Zones': ['Orochi', 'Sougenbi', 'FallenSun', 'EternitySea', 'SixRealms', 'OtherWorldTwilight'],
    'Liver Emperor Exclusive': ['BondlingFairyland', 'EvoZone', 'GoryouRealm', 'Exploration',
                                'Hyakkiyakou', 'HeroTest', 'FindJade', 'MemoryScrolls'],
    'RealmRaid': [],
    'Activity Task': ['ActivityShikigami', 'MetaDemon'],
    'Script': ['Script', 'Restart', 'GlobalGame'],
}

# ConfigMenu.gui_menu_list 会 del 这两个键，缺了就抛 KeyError，收敛后也要保留。
_KEPT_KEYS = ('TaskList', 'Tools')


def _show_all_enabled() -> bool:
    # 不缓存：每次构造 ConfigMenu 都重新读 .env。/script_menu 每个请求新建一个 Config，
    # 而 Config.__init__ 经上游的继承链会跑两次 ConfigMenu.__init__，所以每个请求读两遍。
    # .env 读不了时 read_env 记一条警告并返回默认值，开关随之按关闭处理，这里无需再兜底。
    return read_env('OAS_MENU_SHOW_ALL') == '1'


def custom_menu(upstream: dict[str, list[str]], show_all: bool | None = None) -> dict[str, list[str]]:
    """把上游菜单收敛为定制菜单，返回新字典，不改动 upstream。

    show_all 为 None 时读 OAS_MENU_SHOW_ALL，值为 1 才开启，其余或缺省均为关闭。
    关闭：固定的 7 项，外加上游里有的 TaskList、Tools（原样带过来，上游没有就不生成）。
    开启：保持上游的键和顺序，在 Overview 之后插入编排。
    """
    if show_all is None:
        show_all = _show_all_enabled()
    if show_all:
        menu = {}
        for key, items in upstream.items():
            menu[key] = list(items)
            if key == 'Overview':
                menu[ORCHESTRA_MENU_KEY] = []
        return menu
    menu = {key: list(items) for key, items in _CUSTOM_MENU.items()}
    for key in _KEPT_KEYS:
        if key in upstream:
            menu[key] = list(upstream[key])
    return menu
