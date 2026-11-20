"""素材维护页业务逻辑（module.server.assets_check）的测试：任务、清单、静态检查、标签。

数据都造在 tmp_path/tasks/Foo/ 下，root 一律传 tmp_path，不碰真实仓库的 tasks/。只有两类用例读真实仓库：
爬塔缺图（规格 F18）、初始标签文件与真实 json 对得上。
AssetsExtractor 会跳过路径里含 "temp" 的 json（dev_tools/assets_extract.py:276），清单和检查沿用同一个过滤，
所以用例名里别带 temp，root 夹具也会检查 tmp_path。
assets.py 比对要求当前目录就是 root（AssetsExtractor 按当前目录算图片路径），用到它的用例先 monkeypatch.chdir(root)。
"""
import dataclasses
import json
from pathlib import Path

import pytest

from module.server import assets_check
from module.server.assets_check import (DEFAULT_TASKS, EXTRA_SCREENS, SCREENS, Issue, list_rules, list_tasks,
                                        load_tags, save_tags, screens_of, static_check, tags_path, task_root)

REPO_ROOT = Path(__file__).resolve().parents[2]


class LogSpy:
    """换掉 assets_check.logger：按级别记下消息，不往真日志里写。"""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _record(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message, *args, **kwargs):
        self._record('info', message)

    def warning(self, message, *args, **kwargs):
        self._record('warning', message)

    def error(self, message, *args, **kwargs):
        self._record('error', message)

    def exception(self, message, *args, **kwargs):
        self._record('exception', message)

    def messages(self, level: str) -> list[str]:
        return [message for item_level, message in self.records if item_level == level]


@pytest.fixture(autouse=True)
def log(monkeypatch) -> LogSpy:
    spy = LogSpy()
    monkeypatch.setattr(assets_check, 'logger', spy)
    return spy


@pytest.fixture
def root(tmp_path) -> Path:
    assert 'temp' not in str(tmp_path), f'临时目录路径里带 temp，AssetsExtractor 会跳过其中所有 json：{tmp_path}'
    return tmp_path


def put_json(root: Path, relpath: str, data) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def put_text(root: Path, relpath: str, text: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def put_image(root: Path, relpath: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'not-really-a-png')       # 检查只看文件在不在，不解码
    return path


# 下面几个造出的条目，字段和 AssetsExtractor 各个 Extractor 要的一样（也是它们区分文件类型的依据）
def image_rule(name: str, image: str | None = None, **extra) -> dict:
    return {'itemName': name, 'imageName': image or f'{name}.png', 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20',
            'method': 'Template matching', 'threshold': 0.8, 'description': f'{name} 的描述', **extra}


def ocr_rule(name: str, **extra) -> dict:
    return {'itemName': name, 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20', 'mode': 'Single', 'method': 'Default',
            'keyword': '', 'description': f'{name} 的描述', **extra}


def click_rule(name: str, **extra) -> dict:
    return {'itemName': name, 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20', 'description': f'{name} 的描述', **extra}


def long_click_rule(name: str, **extra) -> dict:
    return {**click_rule(name), 'duration': 1000, **extra}


def swipe_rule(name: str, **extra) -> dict:
    return {**click_rule(name), 'mode': 'default', **extra}


def list_rule(name: str, entries: list[str], **extra) -> dict:
    return {'name': name, 'direction': 'vertical', 'type': 'image', 'roiBack': '0,0,100,100', 'description': f'{name} 的描述',
            'list': [{'itemName': entry, 'roiFront': '0,0,50,50'} for entry in entries], **extra}


def issues_of(issues: list[Issue], kind: str) -> list[tuple[str, str]]:
    """某一类问题的 (file, item) 列表。"""
    return [(issue.file, issue.item) for issue in issues if issue.kind == kind]


def generate_assets(root: Path, task: str = 'Foo') -> Path:
    """用真的 AssetsExtractor 生成 tasks/<task>/assets.py。当前目录必须是 root。"""
    from dev_tools.assets_extract import AssetsExtractor
    AssetsExtractor(str(root / 'tasks' / task)).extract()
    return root / 'tasks' / task / 'assets.py'


# ------------------------------------------------------------------------------------------------------------------
# 任务
# ------------------------------------------------------------------------------------------------------------------

def test_issue_is_frozen_dataclass():
    issue = Issue('missing_image', 'as/image.json', 'b', '图片不存在')
    assert (issue.kind, issue.file, issue.item, issue.detail) == ('missing_image', 'as/image.json', 'b', '图片不存在')
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.kind = 'json_error'


@pytest.mark.parametrize('name', ['../etc', 'Component/Sub', 'a/b', 'a\\b', '', '.', '..', 'Missing', 'foo', '__pycache__',
                                  '.hidden', 'Foo/..', None])
def test_unknown_task_rejected(root, name):
    (root / 'tasks' / 'Foo').mkdir(parents=True)
    (root / 'tasks' / 'Component' / 'Sub').mkdir(parents=True)
    (root / 'tasks' / '__pycache__').mkdir()
    (root / 'tasks' / '.hidden').mkdir()
    (root / 'etc').mkdir()
    with pytest.raises(ValueError):
        task_root(name, root=root)


def test_task_root_returns_the_task_directory(root):
    (root / 'tasks' / 'Foo').mkdir(parents=True)
    assert task_root('Foo', root=root) == (root / 'tasks' / 'Foo').resolve()


def test_task_root_defaults_to_cwd(root, monkeypatch):
    (root / 'tasks' / 'Foo').mkdir(parents=True)
    monkeypatch.chdir(root)
    assert task_root('Foo') == (root / 'tasks' / 'Foo').resolve()


def test_list_tasks_default_pair_and_all(root):
    assert DEFAULT_TASKS == ('ActivityShikigami', 'MetaDemon')
    for name in ('Foo', 'ActivityShikigami', 'Component', '__pycache__'):
        (root / 'tasks' / name).mkdir(parents=True)
    (root / 'tasks' / 'base_task.py').write_text('', encoding='utf-8')       # 文件不是任务
    assert list_tasks(all_tasks=True, root=root) == ['ActivityShikigami', 'Component', 'Foo']
    assert list_tasks(root=root) == ['ActivityShikigami']        # 默认只列还存在的默认任务（MetaDemon 目录不在）
    (root / 'tasks' / 'MetaDemon').mkdir()
    assert list_tasks(root=root) == ['ActivityShikigami', 'MetaDemon']


def test_component_is_one_task(root):
    """同标注工具：组件的容器目录 tasks/Component 整个算一个任务，json 的相对路径是 <组件>/x.json。"""
    (root / 'tasks' / 'Component' / 'Sub').mkdir(parents=True)
    assert task_root('Component', root=root) == (root / 'tasks' / 'Component').resolve()
    assert 'Component' in list_tasks(all_tasks=True, root=root)
    assert 'Component' not in list_tasks(root=root)


def test_list_tasks_without_tasks_directory(root):
    assert list_tasks(all_tasks=True, root=root) == []
    assert list_tasks(root=root) == []


def test_screens_per_task():
    assert SCREENS == ('活动主页', '普通挑战页', 'Boss挑战页', '战斗中', '退出时', '未分类')
    assert screens_of('ActivityShikigami') == SCREENS
    assert screens_of('MetaDemon') == SCREENS + ('超鬼王主页', '超鬼王战斗', '合成页')
    assert set(EXTRA_SCREENS) == {'MetaDemon'}


# ------------------------------------------------------------------------------------------------------------------
# 清单
# ------------------------------------------------------------------------------------------------------------------

def test_list_rules_types_items_and_order(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    put_json(root, 'tasks/Foo/as/ocr.json', [ocr_rule('o')])
    put_json(root, 'tasks/Foo/as/click.json', [click_rule('c')])
    put_json(root, 'tasks/Foo/as/long.json', [long_click_rule('l')])
    put_json(root, 'tasks/Foo/as/swipe.json', [swipe_rule('s')])
    put_json(root, 'tasks/Foo/list.json', list_rule('lst', ['p', 'q']))
    put_json(root, 'tasks/Foo/empty.json', [])
    put_text(root, 'tasks/Foo/broken.json', '{')

    rules = list_rules('Foo', root=root)

    assert [rule['file'] for rule in rules] == sorted(rule['file'] for rule in rules)      # 按相对路径排序，结果稳定
    by_file = {rule['file']: rule for rule in rules}
    assert {file: rule['rule_type'] for file, rule in by_file.items()} == {
        'as/image.json': 'image', 'as/ocr.json': 'ocr', 'as/click.json': 'click', 'as/long.json': 'long_click',
        'as/swipe.json': 'swipe', 'list.json': 'list', 'empty.json': 'unknown', 'broken.json': 'unknown'}
    assert [item['itemName'] for item in by_file['list.json']['items']] == ['p', 'q']       # 列表文件取 list 里的条目
    assert by_file['as/image.json']['items'][0]['imageName'] == 'a.png'                     # 条目带 json 里的全部字段
    assert by_file['as/image.json']['items'][0]['threshold'] == 0.8
    assert by_file['broken.json']['items'] == []                                            # 解析不了的文件照样列出
    assert by_file['empty.json']['items'] == []


def test_list_rules_skips_entries_that_are_not_objects(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a'), 7, 'text', None])
    rules = list_rules('Foo', root=root)
    assert [item['itemName'] for item in rules[0]['items']] == ['a']


def test_list_rules_merges_tags_and_defaults(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a'), image_rule('b'), image_rule('c')])
    save_tags({'Foo': {'as/image.json': {
        'a': {'periodic': True, 'screen': '活动主页'},
        'c': {'periodic': 'yes', 'screen': ''},          # 坏数据：periodic 不是布尔、screen 为空，退回默认值
    }}}, tags_path(root))
    items = {item['itemName']: item for item in list_rules('Foo', root=root)[0]['items']}
    assert (items['a']['periodic'], items['a']['screen']) == (True, '活动主页')
    assert (items['b']['periodic'], items['b']['screen']) == (False, '未分类')            # 没有标签：默认值
    assert (items['c']['periodic'], items['c']['screen']) == (False, '未分类')


def test_stale_tags_are_ignored(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    save_tags({
        'Foo': {'as/image.json': {'a': {'periodic': True, 'screen': '活动主页'}, 'gone': {'periodic': True, 'screen': '战斗中'}},
                'as/removed.json': {'z': {'periodic': True, 'screen': '战斗中'}}},
        'Gone': {'x.json': {'y': {'periodic': True, 'screen': '战斗中'}}},
    }, tags_path(root))
    rules = list_rules('Foo', root=root)
    assert [rule['file'] for rule in rules] == ['as/image.json']
    assert [item['itemName'] for item in rules[0]['items']] == ['a']            # 清单只按 json 实际条目输出
    assert rules[0]['items'][0]['screen'] == '活动主页'


def test_list_rules_ignores_malformed_tag_entries(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    save_tags({'Foo': ['not', 'a', 'dict'], 'Bar': None}, tags_path(root))
    assert list_rules('Foo', root=root)[0]['items'][0]['screen'] == '未分类'
    save_tags({'Foo': {'as/image.json': 'oops'}}, tags_path(root))
    assert list_rules('Foo', root=root)[0]['items'][0]['screen'] == '未分类'
    save_tags({'Foo': {'as/image.json': {'a': 'oops'}}}, tags_path(root))
    assert list_rules('Foo', root=root)[0]['items'][0]['screen'] == '未分类'


def test_json_under_ignored_dir_is_skipped(root):
    """清单和检查看到的 json 与 AssetsExtractor 编进 assets.py 的完全一致：路径里带 temp 的它不编，这里也不列。"""
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    put_text(root, 'tasks/Foo/temp_old/image.json', '{')
    assert [rule['file'] for rule in list_rules('Foo', root=root)] == ['as/image.json']
    assert issues_of(static_check('Foo', root=root), 'json_error') == []


# ------------------------------------------------------------------------------------------------------------------
# 静态检查
# ------------------------------------------------------------------------------------------------------------------

def test_missing_image_detected(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a'), image_rule('b')])
    put_image(root, 'tasks/Foo/as/a.png')

    missing = [issue for issue in static_check('Foo', root=root) if issue.kind == 'missing_image']

    assert [(issue.file, issue.item) for issue in missing] == [('as/image.json', 'b')]
    assert 'as/b.png' in missing[0].detail


def test_missing_image_resolves_relative_to_json(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a', image='sub/a.png')])
    put_image(root, 'tasks/Foo/as/sub/a.png')
    assert issues_of(static_check('Foo', root=root), 'missing_image') == []

    # 同名图片只在任务目录根下的 sub/ 里：不是按 json 所在目录找到的，要报
    (root / 'tasks/Foo/as/sub/a.png').unlink()
    put_image(root, 'tasks/Foo/sub/a.png')
    assert issues_of(static_check('Foo', root=root), 'missing_image') == [('as/image.json', 'a')]


def test_missing_image_ignores_rules_without_image(root):
    put_json(root, 'tasks/Foo/as/ocr.json', [ocr_rule('o')])
    put_json(root, 'tasks/Foo/as/click.json', [click_rule('c')])
    assert issues_of(static_check('Foo', root=root), 'missing_image') == []


def test_missing_image_of_a_list_file_is_the_one_rule_list_loads(root):
    """列表文件的图：type 为 image 时 RuleList 加载 <json 所在目录>/<itemName>.png（ListExtractor 把 json 所在目录编成 folder），
    条目里的 imageName 它不看；type 为 ocr 的列表没有图。"""
    pictures = list_rule('pics', ['p', 'q'])
    pictures['list'].append({'itemName': 'r', 'roiFront': '0,0,50,50', 'imageName': 'other.png'})
    put_json(root, 'tasks/Foo/lists/image_list.json', pictures)
    put_json(root, 'tasks/Foo/lists/ocr_list.json', list_rule('words', ['w'], type='ocr'))
    put_image(root, 'tasks/Foo/lists/p.png')
    put_image(root, 'tasks/Foo/lists/other.png')

    missing = [issue for issue in static_check('Foo', root=root) if issue.kind == 'missing_image']

    assert [(issue.file, issue.item) for issue in missing] == [('lists/image_list.json', 'q'), ('lists/image_list.json', 'r')]
    assert 'lists/q.png' in missing[0].detail and 'lists/r.png' in missing[1].detail


def test_json_error_detected(root):
    put_text(root, 'tasks/Foo/as/broken.json', '{')
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('b')])

    issues = static_check('Foo', root=root)

    errors = [issue for issue in issues if issue.kind == 'json_error']
    assert [(issue.file, issue.item) for issue in errors] == [('as/broken.json', '')]
    assert errors[0].detail                                                         # 带着解析器的说明
    assert issues_of(issues, 'missing_image') == [('as/image.json', 'b')]           # 别的文件照常检查


def test_json_error_for_invalid_utf8_and_wrong_top_level(root):
    path = root / 'tasks/Foo/as/gbk.json'
    path.parent.mkdir(parents=True)
    path.write_bytes('[{"itemName": "测试"}]'.encode('gbk'))                          # 不是 UTF-8
    put_text(root, 'tasks/Foo/as/number.json', '123')                               # 能解析，但顶层不是列表或对象
    issues = static_check('Foo', root=root)
    assert sorted(issues_of(issues, 'json_error')) == [('as/gbk.json', ''), ('as/number.json', '')]


def test_duplicate_item_across_files(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('x')])
    put_json(root, 'tasks/Foo/fire/image.json', [image_rule('x'), image_rule('y')])
    put_image(root, 'tasks/Foo/as/x.png')
    put_image(root, 'tasks/Foo/fire/x.png')
    put_image(root, 'tasks/Foo/fire/y.png')

    duplicates = [issue for issue in static_check('Foo', root=root) if issue.kind == 'duplicate_item']

    assert sorted((issue.file, issue.item) for issue in duplicates) == [('as/image.json', 'x'), ('fire/image.json', 'x')]
    assert all('I_X' in issue.detail for issue in duplicates)
    by_file = {issue.file: issue for issue in duplicates}
    assert 'fire/image.json' in by_file['as/image.json'].detail         # 每一条都指出另一处在哪
    assert 'as/image.json' in by_file['fire/image.json'].detail


def test_duplicate_item_within_one_file_and_ignoring_case(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('x'), image_rule('X'), image_rule('y')])
    assert issues_of(static_check('Foo', root=root), 'duplicate_item') == [('as/image.json', 'x'), ('as/image.json', 'X')]


def test_same_name_under_different_types_is_not_duplicate(root):
    """真实的超鬼王就有 boss_empty_1 同时在 image 和 ocr 里：编出来是 I_ 和 O_ 两个属性，不冲突。"""
    put_json(root, 'tasks/Foo/md/image.json', [image_rule('boss_empty_1')])
    put_json(root, 'tasks/Foo/md/ocr.json', [ocr_rule('boss_empty_1')])
    assert issues_of(static_check('Foo', root=root), 'duplicate_item') == []


def test_long_click_and_list_share_a_prefix(root):
    """长按和列表的属性都以 L_ 开头，同名会撞在一起；列表文件整个是一个属性，名字取 json 顶层的 name。"""
    put_json(root, 'tasks/Foo/a/long.json', [long_click_rule('foo')])
    put_json(root, 'tasks/Foo/b/list.json', list_rule('foo', ['p', 'q']))
    duplicates = [issue for issue in static_check('Foo', root=root) if issue.kind == 'duplicate_item']
    assert sorted((issue.file, issue.item) for issue in duplicates) == [('a/long.json', 'foo'), ('b/list.json', 'foo')]
    assert all('L_FOO' in issue.detail for issue in duplicates)


def test_list_entries_are_not_duplicates_of_other_rules(root):
    put_json(root, 'tasks/Foo/a/image.json', [image_rule('p')])
    put_json(root, 'tasks/Foo/b/list.json', list_rule('lst', ['p']))        # 列表里的 p 不单独生成属性
    assert issues_of(static_check('Foo', root=root), 'duplicate_item') == []


def test_roi_out_of_bounds(root):
    put_json(root, 'tasks/Foo/as/image.json', [
        image_rule('edge', roiFront='1260,700,20,20', roiBack='0,0,1280,720'),         # 贴着边不算越界
        image_rule('wide', roiFront='1270,700,20,30'),
        image_rule('negative', roiBack='-1,0,10,10'),
        image_rule('both', roiFront='0,0,1281,10', roiBack='0,0,10,721'),
    ])
    for name in ('edge', 'wide', 'negative', 'both'):
        put_image(root, f'tasks/Foo/as/{name}.png')

    issues = [issue for issue in static_check('Foo', root=root) if issue.kind == 'roi_out_of_bounds']

    assert [(issue.file, issue.item) for issue in issues] == [
        ('as/image.json', 'wide'), ('as/image.json', 'negative'), ('as/image.json', 'both'), ('as/image.json', 'both')]
    assert 'roiFront' in issues[0].detail and '1270,700,20,30' in issues[0].detail
    assert 'roiBack' in issues[1].detail
    assert 'roiFront' in issues[2].detail and 'roiBack' in issues[3].detail         # 两个字段都越界就报两条


def test_roi_check_covers_every_rule_type(root):
    put_json(root, 'tasks/Foo/a/ocr.json', [ocr_rule('o', roiFront='1279,0,2,2')])
    put_json(root, 'tasks/Foo/a/click.json', [click_rule('c', roiBack='0,719,5,5')])
    assert sorted(issues_of(static_check('Foo', root=root), 'roi_out_of_bounds')) == [('a/click.json', 'c'), ('a/ocr.json', 'o')]


def test_malformed_roi_is_reported_under_the_same_kind(root):
    put_json(root, 'tasks/Foo/as/image.json', [
        image_rule('three', roiFront='1,2,3'),
        image_rule('words', roiFront='a,b,c,d'),
        image_rule('number', roiBack=12),
        image_rule('five', roiBack='1,2,3,4,5'),
        image_rule('nan', roiFront='nan,0,10,10'),
        image_rule('empty', roiFront=''),
    ])
    for name in ('three', 'words', 'number', 'five', 'nan', 'empty'):
        put_image(root, f'tasks/Foo/as/{name}.png')
    issues = [issue for issue in static_check('Foo', root=root) if issue.kind == 'roi_out_of_bounds']
    assert [issue.item for issue in issues] == ['three', 'words', 'number', 'five', 'nan', 'empty']
    assert all('格式' in issue.detail for issue in issues)


def test_decimal_roi_is_valid_but_still_bounded(root):
    """标注工具保存裁剪框时会写出带小数的 ROI（真实素材里有 232.5,196,55,151），不是格式错误；越界照报。"""
    put_json(root, 'tasks/Foo/as/image.json', [
        image_rule('half', roiFront='232.5,196,55,151', roiBack='0,0,1279.5,719.5'),
        image_rule('over', roiFront='1279.5,0,1,10'),
    ])
    put_image(root, 'tasks/Foo/as/half.png')
    put_image(root, 'tasks/Foo/as/over.png')
    issues = [issue for issue in static_check('Foo', root=root) if issue.kind == 'roi_out_of_bounds']
    assert [(issue.item, '超出' in issue.detail) for issue in issues] == [('over', True)]


def test_list_file_roi_is_checked_on_top_level_and_entries(root):
    put_json(root, 'tasks/Foo/list.json', list_rule('lst', ['p', 'q'], roiBack='1200,0,200,100'))
    data = json.loads((root / 'tasks/Foo/list.json').read_text(encoding='utf-8'))
    data['list'][1]['roiFront'] = '0,700,10,30'
    put_json(root, 'tasks/Foo/list.json', data)
    issues = [issue for issue in static_check('Foo', root=root) if issue.kind == 'roi_out_of_bounds']
    assert [(issue.file, issue.item) for issue in issues] == [('list.json', 'lst'), ('list.json', 'q')]
    assert 'roiBack' in issues[0].detail and 'roiFront' in issues[1].detail


def test_clean_task_has_no_issues(root):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    put_image(root, 'tasks/Foo/as/a.png')
    assert static_check('Foo', root=root) == []


# ------------------------------------------------------------------------------------------------------------------
# assets.py 与 json 是否一致（试生成后比对）
# ------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def generated_task(root, monkeypatch):
    """Foo 任务有 a、b 两个图片条目，assets.py 刚用真的 AssetsExtractor 生成；当前目录已切到 root。"""
    monkeypatch.chdir(root)
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a'), image_rule('b')])
    put_image(root, 'tasks/Foo/as/a.png')
    put_image(root, 'tasks/Foo/as/b.png')
    return generate_assets(root)


def outdated(root: Path) -> list[Issue]:
    return [issue for issue in static_check('Foo', root=root) if issue.kind == 'assets_outdated']


def test_fresh_assets_py_is_up_to_date(root, generated_task):
    assert static_check('Foo', root=root) == []


def test_assets_outdated(root, generated_task):
    lines = generated_task.read_text(encoding='utf-8').splitlines(keepends=True)
    generated_task.write_bytes(''.join(line for line in lines if not line.startswith('\tI_B =')).encode('utf-8'))

    issues = outdated(root)

    assert [(issue.file, issue.item) for issue in issues] == [('assets.py', '')]
    assert 'I_B' in issues[0].detail and 'I_A' not in issues[0].detail          # 指出缺的是哪个属性


def test_assets_outdated_names_extra_attributes(root, generated_task):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])               # 删掉 b：assets.py 里的 I_B 成了多余的
    issues = outdated(root)
    assert len(issues) == 1 and 'I_B' in issues[0].detail and '多出' in issues[0].detail


def test_assets_outdated_when_content_differs_with_same_names(root, generated_task):
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a', threshold=0.9), image_rule('b')])
    issues = outdated(root)
    assert len(issues) == 1
    assert '内容' in issues[0].detail and 'I_A' not in issues[0].detail          # 属性名都对得上，只是内容有差异


def test_missing_assets_py_is_outdated_and_is_not_created(root, generated_task):
    generated_task.unlink()
    issues = outdated(root)
    assert [(issue.file, issue.item) for issue in issues] == [('assets.py', '')]
    assert not generated_task.exists()                                           # 试生成不能写盘


def test_trial_generation_never_writes(root, generated_task):
    generated_task.write_bytes(b'# stale\n')
    outdated(root)
    assert generated_task.read_bytes() == b'# stale\n'


def test_assets_check_skipped_when_root_is_not_cwd(root, log):
    """AssetsExtractor 按当前目录算图片路径：root 不是当前目录时试生成的文本对不上，不比对，记一条警告。"""
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    put_image(root, 'tasks/Foo/as/a.png')
    assert Path.cwd().resolve() != root.resolve()
    assert static_check('Foo', root=root) == []                     # 没有 assets.py，但不报
    assert any('assets.py' in message for message in log.messages('warning'))


def test_assets_check_skipped_when_json_is_broken(root, monkeypatch):
    monkeypatch.chdir(root)
    put_text(root, 'tasks/Foo/as/broken.json', '{')
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a')])
    put_image(root, 'tasks/Foo/as/a.png')
    issues = static_check('Foo', root=root)
    assert [issue.kind for issue in issues] == ['json_error']        # 没有 assets.py 也不报 assets_outdated


def test_trial_generation_failure_is_reported_not_raised(root, monkeypatch):
    monkeypatch.chdir(root)
    broken = image_rule('a')
    del broken['roiBack']                                            # AssetsExtractor 取 item["roiBack"] 会抛 KeyError
    put_json(root, 'tasks/Foo/as/image.json', [broken])
    put_image(root, 'tasks/Foo/as/a.png')
    issues = static_check('Foo', root=root)
    assert [issue.kind for issue in issues] == ['json_error']
    assert '试生成' in issues[0].detail and 'roiBack' in issues[0].detail


def test_empty_task_is_compared_with_the_pass_placeholder(root, monkeypatch):
    monkeypatch.chdir(root)
    (root / 'tasks' / 'Foo').mkdir(parents=True)
    assert generate_assets(root).read_text(encoding='utf-8').endswith('\tpass\n\n')
    assert static_check('Foo', root=root) == []


# ------------------------------------------------------------------------------------------------------------------
# 组件（tasks/Component）：一个任务，每个组件各编各的 assets.py
# ------------------------------------------------------------------------------------------------------------------

def test_component_rules_are_listed_relative_to_the_container(root):
    put_json(root, 'tasks/Component/A/image.json', [image_rule('x')])
    put_json(root, 'tasks/Component/B/sub/ocr.json', [ocr_rule('o')])
    rules = list_rules('Component', root=root)
    assert [(rule['file'], rule['rule_type']) for rule in rules] == [('A/image.json', 'image'), ('B/sub/ocr.json', 'ocr')]


def test_component_duplicates_are_scoped_per_component(root):
    """同名属性在不同组件里不冲突（各编进自己的 assets.py）；同一个组件里的两个文件才冲突。"""
    put_json(root, 'tasks/Component/A/image.json', [image_rule('x'), image_rule('y')])
    put_json(root, 'tasks/Component/A/sub/image.json', [image_rule('y')])
    put_json(root, 'tasks/Component/B/image.json', [image_rule('x')])
    for relpath in ('A/x.png', 'A/y.png', 'A/sub/y.png', 'B/x.png'):
        put_image(root, f'tasks/Component/{relpath}')
    duplicates = [issue for issue in static_check('Component', root=root) if issue.kind == 'duplicate_item']
    assert sorted((issue.file, issue.item) for issue in duplicates) == [('A/image.json', 'y'), ('A/sub/image.json', 'y')]


@pytest.fixture
def components(root, monkeypatch):
    """四个组件，当前目录已切到 root：A、B 有 json、图和刚生成的 assets.py；C 是空目录（没有 json，也没有 assets.py）；
    E 只有 assets.py（没有 json 时的 pass 占位，同真实的 BaseActivity）。"""
    monkeypatch.chdir(root)
    for name in ('A', 'B'):
        put_json(root, f'tasks/Component/{name}/image.json', [image_rule(f'{name.lower()}1')])
        put_image(root, f'tasks/Component/{name}/{name.lower()}1.png')
    (root / 'tasks/Component/C').mkdir()
    (root / 'tasks/Component/E').mkdir()
    for name in ('A', 'B', 'E'):
        generate_assets(root, f'Component/{name}')
    return root


def test_component_assets_are_checked_per_component(components):
    root = components
    assert static_check('Component', root=root) == []                  # 各组件的 assets.py 都对得上，空目录 C 也不报
    assert not (root / 'tasks/Component/assets.py').exists()           # 容器目录自己没有、也不该有 assets.py

    put_json(root, 'tasks/Component/B/image.json', [image_rule('b1'), image_rule('b2')])
    put_image(root, 'tasks/Component/B/b2.png')
    put_json(root, 'tasks/Component/D/image.json', [image_rule('d1')])     # 新组件，还没生成 assets.py
    put_image(root, 'tasks/Component/D/d1.png')

    issues = static_check('Component', root=root)

    assert [(issue.kind, issue.file) for issue in issues] == [('assets_outdated', 'B/assets.py'),
                                                              ('assets_outdated', 'D/assets.py')]
    assert 'I_B2' in issues[0].detail and '不存在' in issues[1].detail


def test_component_json_error_skips_only_that_component(components):
    root = components
    put_text(root, 'tasks/Component/A/broken.json', '{')
    (root / 'tasks/Component/B/assets.py').write_bytes(b'# stale\n')
    issues = static_check('Component', root=root)
    assert [(issue.kind, issue.file) for issue in issues] == [('json_error', 'A/broken.json'),
                                                              ('assets_outdated', 'B/assets.py')]


def test_component_trial_generation_failure_names_the_component(components):
    root = components
    broken = image_rule('a2')
    del broken['roiBack']                                            # AssetsExtractor 取 item["roiBack"] 会抛 KeyError
    put_json(root, 'tasks/Component/A/more.json', [broken])
    put_image(root, 'tasks/Component/A/a2.png')
    issues = static_check('Component', root=root)
    assert [(issue.kind, issue.file) for issue in issues] == [('json_error', 'A')]
    assert '试生成' in issues[0].detail and 'roiBack' in issues[0].detail


def test_component_json_directly_under_the_container_is_left_out(components):
    """直接放在 tasks/Component 下的 json 不属于任何组件，不会编进哪个 assets.py（标注工具也拒绝保存它）：清单、检查都不收。"""
    root = components
    put_json(root, 'tasks/Component/loose.json', [image_rule('loose')])        # 图也不存在
    assert 'loose.json' not in [rule['file'] for rule in list_rules('Component', root=root)]
    assert static_check('Component', root=root) == []


# ------------------------------------------------------------------------------------------------------------------
# 真实仓库
# ------------------------------------------------------------------------------------------------------------------

def test_real_activity_missing_images(monkeypatch):
    """规格 F18：fire/image.json 引用的 fire_pass_8、10、11 在目录里不存在。"""
    monkeypatch.chdir(REPO_ROOT)
    missing = [issue for issue in static_check('ActivityShikigami') if issue.kind == 'missing_image']
    assert {issue.item for issue in missing} == {'pass_8', 'pass_10', 'pass_11'}
    assert {issue.file for issue in missing} == {'fire/image.json'}


def test_real_component_assets_match_their_json(monkeypatch):
    """真实的 tasks/Component：json 都能解析，每个组件的 assets.py 都与试生成的结果一致（各组件分开比对）。"""
    monkeypatch.chdir(REPO_ROOT)
    issues = static_check('Component')
    assert [issue for issue in issues if issue.kind in ('json_error', 'assets_outdated')] == []
    assert all('/' in issue.file for issue in issues)              # 文件都写成 <组件>/...，同标注工具

# 计划（M3b Task 1 Step 3）里的爬塔标签表：(json 文件, 画面, periodic, 条目；None 表示该文件全部条目)
TOWER_TAGS = [
    ('as/pages.json', '活动主页', True, ['shi', 'to_battle_main', 'to_battle_boss']),
    ('as/pages.json', '普通挑战页', True, ['check_battle_main', 'battle_main_to_records']),
    ('as/pages.json', 'Boss挑战页', True, ['check_battle_boss']),
    ('as/pages.json', '退出时', True, ['back_green', 'red_exit', 'skip_button', 'confirm_skip']),
    ('as/pages.json', '未分类', True, ['to_battle_main_2']),
    ('as/image.json', '普通挑战页', True, ['climb_mode_pass', 'climb_mode_ap', 'climb_mode_switch']),
    ('as/image.json', '普通挑战页', False, ['lock', 'unlock']),
    ('fire/ocr.json', '普通挑战页', True, ['fire', 'remain_ap', 'remain_pass', 'remain_pass2', 'remain_buy', 'remain_ap100']),
    ('fire/ocr.json', 'Boss挑战页', True, ['remain_boss']),
    ('fire/ocr.json', '未分类', True, ['enter_ap100']),
    ('fire/image.json', '战斗中', False, None),
]


def test_initial_tags_for_activity_tower_match_the_plan():
    rules = {rule['file']: rule for rule in list_rules('ActivityShikigami', root=REPO_ROOT)}
    expected: dict[tuple[str, str], tuple[str, bool]] = {}
    for file, screen, periodic, names in TOWER_TAGS:
        for name in names if names is not None else [item['itemName'] for item in rules[file]['items']]:
            assert (file, name) not in expected, f'计划表里重复了 {file} {name}'
            expected[(file, name)] = (screen, periodic)

    actual = {(file, item['itemName']): (item['screen'], item['periodic'])
              for file, rule in rules.items() for item in rule['items'] if file in {f for f, *_ in TOWER_TAGS}}
    assert actual == expected                       # 表里每个条目都存在、标签一致，这四个文件里也没有漏标的条目


def test_initial_tags_refer_to_real_items_and_allowed_screens():
    tags = load_tags(tags_path(REPO_ROOT))
    assert tags, '初始标签文件不存在或为空'
    for task, files in tags.items():
        rules = {rule['file']: {item['itemName'] for item in rule['items']} for rule in list_rules(task, root=REPO_ROOT)}
        for file, items in files.items():
            assert file in rules, f'{task} 没有 {file}'
            for name, tag in items.items():
                assert name in rules[file], f'{task} {file} 没有条目 {name}'
                assert isinstance(tag['periodic'], bool), f'{task} {file} {name}'
                assert tag['screen'] in screens_of(task), f'{task} {file} {name}: {tag["screen"]}'
                assert set(tag) == {'periodic', 'screen'}


def test_initial_tags_for_meta_demon():
    rules = {rule['file']: rule for rule in list_rules('MetaDemon', root=REPO_ROOT)}
    tags = load_tags(tags_path(REPO_ROOT))['MetaDemon']
    for file, rule in rules.items():                # 超鬼王的每个条目都显式标过（拿不准的标"未分类"），不靠默认值
        assert {item['itemName'] for item in rule['items']} == set(tags[file]), file
    items = {(file, item['itemName']): item for file, rule in rules.items() for item in rule['items']}
    assert items[('md/image_2025.json', 'find_demon')]['screen'] == '超鬼王主页'
    assert items[('md/image.json', 'md_win')]['screen'] == '超鬼王战斗'
    assert items[('md/image_2025.json', 'crafting_start')]['screen'] == '合成页'
    assert {item['screen'] for item in items.values()} <= set(screens_of('MetaDemon'))
    assert not any(item['periodic'] for item in items.values())          # 超鬼王没有按期更换的素材，一律 false


# ------------------------------------------------------------------------------------------------------------------
# 标签文件
# ------------------------------------------------------------------------------------------------------------------

def test_tags_roundtrip(tmp_path):
    tags = {'Foo': {'as/image.json': {'x': {'periodic': True, 'screen': '活动主页'},
                                       'y': {'periodic': False, 'screen': '未分类'}}}}
    path = tmp_path / 'custom' / 'assets_tags.json'              # 目录还不存在也能写
    save_tags(tags, path)
    assert load_tags(path) == tags
    raw = path.read_bytes()
    assert raw.endswith(b'\n') and b'\r' not in raw               # 末尾换行，LF 换行
    assert '活动主页' in raw.decode('utf-8')                       # 不转义成 \uXXXX
    assert raw.decode('utf-8').startswith('{\n  "Foo": {\n    "as/image.json"')     # 缩进 2，保持传入的键序
    assert list(path.parent.iterdir()) == [path]                  # 没留下临时文件


def test_save_tags_replaces_the_whole_file(tmp_path):
    path = tmp_path / 'assets_tags.json'
    save_tags({'Foo': {'a.json': {'x': {'periodic': True, 'screen': '战斗中'}}}}, path)
    save_tags({'Bar': {}}, path)
    assert load_tags(path) == {'Bar': {}}


def test_save_tags_removes_a_stale_scratch_file(tmp_path):
    """临时文件名固定（<文件名>.assets-check.tmp）：上次写到一半进程被杀留下的残渣，这次写入时先删掉，不会一直留着。"""
    path = tmp_path / 'assets_tags.json'
    (tmp_path / 'assets_tags.json.assets-check.tmp').write_bytes(b'half-written')
    save_tags({'Foo': {}}, path)
    assert load_tags(path) == {'Foo': {}}
    assert list(tmp_path.iterdir()) == [path]


def test_save_tags_keeps_the_old_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / 'assets_tags.json'
    save_tags({'old': {}}, path)

    def boom(src, dst):
        raise OSError('disk gone')

    with monkeypatch.context() as patch:
        patch.setattr(assets_check.os, 'replace', boom)
        with pytest.raises(OSError):
            save_tags({'new': {}}, path)
    assert load_tags(path) == {'old': {}}
    assert list(tmp_path.iterdir()) == [path]                     # 失败后临时文件也清掉了


def test_load_tags_missing_file_is_empty(tmp_path):
    assert load_tags(tmp_path / 'nope' / 'assets_tags.json') == {}


@pytest.mark.parametrize('content', ['{', '[]', '"text"', '123'])
def test_load_tags_rejects_a_broken_file(tmp_path, content):
    """坏文件要让调用方看到错误，不能当成空表：否则下一次保存会悄悄覆盖掉它。"""
    path = tmp_path / 'assets_tags.json'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError):
        load_tags(path)


def test_tags_default_path_is_under_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tags_path() == (tmp_path / 'assets' / 'custom' / 'assets_tags.json').resolve()
    assert tags_path(tmp_path) == tags_path()
    assert load_tags() == {}
    save_tags({'Foo': {}})
    assert (tmp_path / 'assets' / 'custom' / 'assets_tags.json').is_file()
    assert load_tags() == {'Foo': {}}
