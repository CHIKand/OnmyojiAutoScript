"""素材维护页写入这一半的测试：按画面体检、替换图片、提交并推送、保存标签、导出补丁（module.server.assets_check）。

git 的用例都在 tmp_path 下的本地仓库里跑：裸仓库当 origin（fork），一份带本地身份的工作克隆当运行目录，不联网，
不碰本仓库的远端、config/ 和 log/。推送失败靠把 origin 的 pushurl 指到不存在的路径，拉取失败靠把 origin 的 url 指过去。
体检和换图不用 git，只在 tmp_path 下造 tasks/Foo/。
真的 AssetsExtractor 会跳过路径里含 "temp" 的 json（dev_tools/assets_extract.py），所以夹具和用例名里都不带这个子串，root 夹具也会检查。
module.server.tool 导入时按当前目录定出 PROJECT_ROOT，所以在收集阶段（当前目录是仓库根）就先导入它。
"""
import base64
import contextlib
import json
import os
import shutil
import subprocess
import textwrap
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from module.server import assets_check, fork_updater
from module.server import tool as annotator_tool
from module.server.fork_git import GitRunner

BUSY = '正在合并上游或保存素材，请稍后再试'
MERGING = '仓库里有未完成的上游合并，请先在 OASX 更新页点"执行更新"处理，再保存素材'
DIVERGED = '运行目录和 fork 已经分叉，需要人工处理，这次没有提交'
FETCH_TAIL = '。已提交的改动留在本地，启动器不会冲掉，联网后再点"提交并推送"'
PUSH_TAIL = '。改动已在本地提交，可以稍后再点"提交并推送"重试'
TAGS_FILE = 'assets/custom/assets_tags.json'
NOT_A_REPO = 'does not appear to be a git repository'        # 指到不存在的路径时，git 第一行报错里的这句


def fork_ahead(n: int) -> str:
    return (f'fork 上有 {n} 个运行目录还没有的提交，现在提交会和 fork 分叉：请先在 OASX 重启服务同步，'
            f'再重新替换并提交（重启会还原未提交的改动）')


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


@pytest.fixture(autouse=True)
def lock_is_released():
    """每个用例结束时 MERGE_LOCK 必须是空闲的：写入函数不管成功失败都要放锁。漏放了也先放掉，免得连累后面的用例。"""
    yield
    leaked = not fork_updater.MERGE_LOCK.acquire(blocking=False)
    fork_updater.MERGE_LOCK.release()
    assert not leaked, 'MERGE_LOCK 没有释放'


@contextlib.contextmanager
def lock_held():
    """假装别处的合并或保存正占着 MERGE_LOCK。"""
    assert fork_updater.MERGE_LOCK.acquire(blocking=False)
    try:
        yield
    finally:
        fork_updater.MERGE_LOCK.release()


# ------------------------------------------------------------------------------------------------------------------
# 造数据
# ------------------------------------------------------------------------------------------------------------------

def png_bytes(color=(10, 20, 30), size=(4, 3)) -> bytes:
    """纯色 PNG，size 是 (宽, 高)，color 是 BGR。"""
    image = np.zeros((size[1], size[0], 3), np.uint8)
    image[:] = color
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    return encoded.tobytes()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def pixels(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
    assert image is not None, f'读不出图片：{path}'
    return image


OLD_PNG = png_bytes((1, 2, 3), (2, 2))
BINARY = b'\x89PNG\r\n\x1a\n\x00\xff\x00\r\n\x1a'           # 含 \0、\r\n 的二进制，导出时一个字节都不能变


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def image_rule(name: str, image: str | None = None, **extra) -> dict:
    return {'itemName': name, 'imageName': image or f'{name}.png', 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20',
            'method': 'Template matching', 'threshold': 0.8, 'description': f'{name} 的描述', **extra}


def ocr_rule(name: str, **extra) -> dict:
    return {'itemName': name, 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20', 'mode': 'Single', 'method': 'Default',
            'keyword': '', 'description': f'{name} 的描述', **extra}


def click_rule(name: str, **extra) -> dict:
    return {'itemName': name, 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20', 'description': f'{name} 的描述', **extra}


def write_files(base: Path, files: dict) -> None:
    """内容为 None 表示删除该文件。一律按字节写，免得换行符被改。"""
    for name, content in files.items():
        path = base / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))


def put_json(root: Path, relpath: str, data) -> Path:
    write_files(root, {relpath: json_text(data)})
    return root / relpath


def base_files() -> dict:
    """运行目录里已提交的内容：Foo 任务（a、b 两张图在，gone 没有）、别的任务、组件 A（Component/A，图 x 在）、标签文件、
    仓库根的杂项。"""
    return {
        'tasks/Foo/as/image.json': json_text([image_rule('a'), image_rule('b'), image_rule('gone')]),
        'tasks/Foo/as/a.png': OLD_PNG,
        'tasks/Foo/as/b.png': OLD_PNG,
        'tasks/Foo/as/老图.png': OLD_PNG,
        'tasks/Foo/assets.py': '# generated\n',
        'tasks/Bar/x.png': OLD_PNG,
        'tasks/Component/A/image.json': json_text([image_rule('x')]),
        'tasks/Component/A/x.png': OLD_PNG,
        'tasks/Component/A/assets.py': '# generated\n',
        TAGS_FILE: '{}\n',
        'other.txt': 'base\n',
    }


def tree(root: Path) -> dict[str, bytes]:
    """root 下全部文件（含 .git 里的）的 {相对路径: 内容}，用来断言"什么都没变"：包括没留下临时文件。"""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


@pytest.fixture
def root(tmp_path) -> Path:
    assert 'temp' not in str(tmp_path), f'临时目录路径里带 temp，AssetsExtractor 会跳过其中所有 json：{tmp_path}'
    write_files(tmp_path, base_files())
    return tmp_path


class Regen:
    """假的 regenerate：记下被调用时的任务名。"""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, task):
        self.calls.append(task)


def replace(root, image_name='a.png', content=None, *, json_relpath='as/image.json', task='Foo', regenerate=None):
    """调 replace_image，默认换 as/image.json 里的 a.png；regenerate 不传就用记录器。返回 (结果, 记录器)。"""
    regen = regenerate if regenerate is not None else Regen()
    content = b64(png_bytes()) if content is None else content
    result = assets_check.replace_image(task, json_relpath, image_name, content, root=root, regenerate=regen)
    return result, regen


# ------------------------------------------------------------------------------------------------------------------
# git 夹具：裸 origin + 工作克隆
# ------------------------------------------------------------------------------------------------------------------

def sh(cwd, *args) -> str:
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, encoding='utf-8')
    assert done.returncode == 0, f'git {" ".join(args)} 失败：{done.stderr}'
    return done.stdout.strip()


def configure(repo: Path) -> None:
    for key, value in (('user.name', 'tester'), ('user.email', 'tester@example.com'),
                       ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')):
        sh(repo, 'config', key, value)


def commit_files(repo: Path, files: dict, message: str) -> None:
    write_files(repo, files)
    sh(repo, 'add', '-A')
    sh(repo, 'commit', '-q', '-m', message)


@pytest.fixture(scope='module')
def seed_repo(tmp_path_factory) -> Path:
    """建好 origin.git（裸仓库，分支 custom）和从它克隆出来的 work，里面是 base_files。每个用例拷一份，比重建快得多。
    种子仓库放在另一个目录里，免得被一起拷走。"""
    source, base = tmp_path_factory.mktemp('seed-source'), tmp_path_factory.mktemp('seed-repo')
    origin, seed, work = base / 'origin.git', source / 'seed', base / 'work'
    sh(base, 'init', '-q', '--bare', '--template=', str(origin))
    sh(origin, 'symbolic-ref', 'HEAD', 'refs/heads/custom')
    sh(source, 'init', '-q', '--template=', str(seed))
    configure(seed)
    sh(seed, 'checkout', '-q', '-b', 'custom')
    commit_files(seed, base_files(), 'base')
    sh(seed, 'push', '-q', str(origin), 'HEAD:refs/heads/custom')
    sh(base, 'clone', '-q', '--template=', '-c', 'core.autocrlf=false', str(origin), str(work))
    configure(work)
    return base


class Repo:
    """seed_repo 的一份拷贝：origin.git（fork）、work（运行目录）和被测的 GitRunner。"""

    def __init__(self, base: Path):
        self.base, self.origin, self.work = base, base / 'origin.git', base / 'work'
        self.runner = GitRunner('git', self.work)

    @property
    def head(self) -> str:
        return sh(self.work, 'rev-parse', 'HEAD')

    @property
    def fork_tip(self) -> str:
        return sh(self.origin, 'rev-parse', 'refs/heads/custom')

    def write(self, files: dict) -> None:
        """只改工作区，不提交。"""
        write_files(self.work, files)

    def commit(self, files: dict, message: str = 'local change') -> None:
        commit_files(self.work, files, message)

    def rival_commit(self, files: dict, message: str = 'rival change') -> None:
        """另一份克隆往 fork 推一个提交：fork 上多出运行目录没有的提交。"""
        rival = self.base / 'rival'
        if not rival.exists():
            sh(self.base, 'clone', '-q', '--template=', str(self.origin), str(rival))
            configure(rival)
        commit_files(rival, files, message)
        sh(rival, 'push', '-q', 'origin', 'HEAD:refs/heads/custom')

    def files_of(self, sha: str) -> set[str]:
        """一个提交改动的文件（-z：中文路径不转义）。"""
        out = subprocess.run(['git', 'diff-tree', '-r', '--root', '--no-commit-id', '--name-only', '-z', sha], cwd=self.work,
                             capture_output=True, encoding='utf-8').stdout
        return {name for name in out.split('\0') if name}

    def message(self, sha: str) -> str:
        return sh(self.work, 'log', '-1', '--format=%B', sha)

    def status(self) -> list[str]:
        out = subprocess.run(['git', '-c', 'core.quotepath=off', 'status', '--porcelain', '--untracked-files=all'],
                             cwd=self.work, capture_output=True, encoding='utf-8').stdout
        return sorted(out.splitlines())

    def point_origin_at(self, url) -> None:
        sh(self.work, 'remote', 'set-url', 'origin', str(url))

    def break_push(self, missing) -> None:
        """推送失败、拉取照常：只把 origin 的推送地址指到不存在的仓库。"""
        sh(self.work, 'remote', 'set-url', '--push', 'origin', str(missing))

    def fix_push(self) -> None:
        sh(self.work, 'config', '--unset', 'remote.origin.pushurl')


@pytest.fixture
def repo(seed_repo, tmp_path) -> Repo:
    assert 'temp' not in str(tmp_path), f'临时目录路径里带 temp，AssetsExtractor 会跳过其中所有 json：{tmp_path}'
    base = tmp_path / 'repo'
    shutil.copytree(seed_repo, base)
    copy = Repo(base)
    copy.point_origin_at(copy.origin)
    return copy


@pytest.fixture
def git_calls(monkeypatch) -> list[tuple]:
    """GitRunner.run 之后每次调用的参数（元组），调用照常真跑。"""
    calls, real = [], GitRunner.run

    def spy(self, *args, check=True):
        calls.append(args)
        return real(self, *args, check=check)

    monkeypatch.setattr(GitRunner, 'run', spy)
    return calls


def break_git(monkeypatch, *prefix, stderr='fatal: simulated failure'):
    """以这串参数开头的 git 命令一律失败（退出码 128），其余照常真跑。"""
    real = GitRunner.run

    def run(self, *args, check=True):
        if args[:len(prefix)] != prefix:
            return real(self, *args, check=check)
        if check:
            raise subprocess.CalledProcessError(128, [self.git, *args], '', stderr)
        return subprocess.CompletedProcess([self.git, *args], 128, '', stderr)

    monkeypatch.setattr(GitRunner, 'run', run)


# ------------------------------------------------------------------------------------------------------------------
# 按画面体检
# ------------------------------------------------------------------------------------------------------------------

SCREEN_TAGS = {'Foo': {
    'as/image.json': {'a': {'periodic': True, 'screen': '普通挑战页'}, 'b': {'periodic': False, 'screen': '战斗中'}},
    'as/ocr.json': {'c': {'periodic': False, 'screen': '普通挑战页'}},
    'as/click.json': {'d': {'periodic': False, 'screen': '普通挑战页'}},
    'as/list.json': {'e': {'periodic': False, 'screen': '普通挑战页'}},
}}


@pytest.fixture
def screen_root(tmp_path) -> Path:
    """Foo 任务：两个图片条目、一个 OCR 条目，加上不打分的点击条目、列表文件和一个解析不了的 json。"""
    assert 'temp' not in str(tmp_path), f'临时目录路径里带 temp，AssetsExtractor 会跳过其中所有 json：{tmp_path}'
    put_json(tmp_path, 'tasks/Foo/as/image.json', [image_rule('a'), image_rule('b', threshold=0.9)])
    put_json(tmp_path, 'tasks/Foo/as/ocr.json', [ocr_rule('c', keyword='战斗')])
    put_json(tmp_path, 'tasks/Foo/as/click.json', [click_rule('d')])
    put_json(tmp_path, 'tasks/Foo/as/list.json', {
        'name': 'lst', 'direction': 'vertical', 'type': 'image', 'roiBack': '0,0,100,100', 'description': '列表',
        'list': [{'itemName': 'e', 'roiFront': '0,0,50,50'}]})
    write_files(tmp_path, {'tasks/Foo/as/broken.json': '{'})
    return tmp_path


class FakeTester:
    """假的 annotator_manager.test_rule：记下每次调用，按条目名给定结果；fail 里的条目抛异常。"""

    def __init__(self, results=None, fail=None):
        self.calls: list[tuple] = []
        self.results = results or {}
        self.fail = fail or {}

    def __call__(self, session_id, image_id, task_name, json_relpath, rule_type, rule, list_meta=None):
        name = rule['itemName']
        self.calls.append((session_id, image_id, task_name, json_relpath, rule_type, name))
        if name in self.fail:
            raise self.fail[name]
        return self.results.get(name, {'matched': True, 'similarity': 0.95})


def test_screen_check_marks_expected(screen_root):
    tester = FakeTester()
    results = assets_check.screen_check('Foo', '普通挑战页', 'session-1', 'image-1', tester=tester, tags=SCREEN_TAGS,
                                        root=screen_root)
    assert results == [
        {'file': 'as/image.json', 'item': 'a', 'matched': True, 'similarity': 0.95, 'threshold': 0.8, 'expected': True},
        {'file': 'as/image.json', 'item': 'b', 'matched': True, 'similarity': 0.95, 'threshold': 0.9, 'expected': False},
        {'file': 'as/ocr.json', 'item': 'c', 'matched': True, 'similarity': 0.95, 'threshold': None, 'expected': True},
    ]       # 只给图片和 OCR 条目打分，标在别的画面上的也打，只是 expected 为假；点击、列表条目不打分；OCR 没有阈值
    assert tester.calls == [('session-1', 'image-1', 'Foo', 'as/image.json', 'image', 'a'),
                            ('session-1', 'image-1', 'Foo', 'as/image.json', 'image', 'b'),
                            ('session-1', 'image-1', 'Foo', 'as/ocr.json', 'ocr', 'c')]


def test_screen_check_passes_the_json_rule_to_the_tester(screen_root):
    """tester 收到的是 json 里的条目原样（标注工具测试单条规则用的就是这些字段），不夹带标签的字段。"""
    rules = []

    def tester(session_id, image_id, task_name, json_relpath, rule_type, rule, list_meta=None):
        rules.append(rule)
        return {'matched': True, 'similarity': 1.0}

    assets_check.screen_check('Foo', '普通挑战页', 's', 'i', tester=tester, tags=SCREEN_TAGS, root=screen_root)
    assert rules == [image_rule('a'), image_rule('b', threshold=0.9), ocr_rule('c', keyword='战斗')]


def test_screen_check_with_tags_given_does_not_read_the_tags_file(screen_root):
    """传了 tags 就只用它：磁盘上的标签文件哪怕坏了，也不读、不报错。"""
    write_files(screen_root, {TAGS_FILE: '{broken'})
    results = assets_check.screen_check('Foo', '普通挑战页', 's', 'i', tester=FakeTester(), tags=SCREEN_TAGS,
                                        root=screen_root)
    assert [(item['item'], item['expected']) for item in results] == [('a', True), ('b', False), ('c', True)]


def test_screen_check_reports_a_broken_tags_file(screen_root):
    """不传 tags 时读 root 下的标签文件，文件坏了抛 ValueError（同 load_tags），一条都不打分。"""
    write_files(screen_root, {TAGS_FILE: '{broken'})
    tester = FakeTester()
    with pytest.raises(ValueError, match='标签文件损坏'):
        assets_check.screen_check('Foo', '普通挑战页', 's', 'i', tester=tester, root=screen_root)
    assert tester.calls == []


def test_screen_check_reports_what_the_tester_says(screen_root):
    tester = FakeTester({'a': {'matched': False, 'similarity': 0.12}, 'c': {'matched': True, 'similarity': 1.0}})
    results = assets_check.screen_check('Foo', '普通挑战页', 's', 'i', tester=tester, tags=SCREEN_TAGS, root=screen_root)
    assert [(item['item'], item['matched'], item['similarity']) for item in results] == [
        ('a', False, 0.12), ('b', True, 0.95), ('c', True, 1.0)]


def test_screen_with_no_items_returns_empty(screen_root):
    tester = FakeTester()
    assert assets_check.screen_check('Foo', 'Boss挑战页', 's', 'i', tester=tester, tags=SCREEN_TAGS, root=screen_root) == []
    assert tester.calls == []        # 没有条目标在这个画面上：一条都不打分


def test_screen_with_only_unscored_items_returns_empty(screen_root):
    """标在这个画面上的只有点击条目和列表条目（不打分）：同样视为没有。"""
    tags = {'Foo': {'as/click.json': {'d': {'periodic': False, 'screen': '活动主页'}},
                    'as/list.json': {'e': {'periodic': False, 'screen': '活动主页'}}}}
    tester = FakeTester()
    assert assets_check.screen_check('Foo', '活动主页', 's', 'i', tester=tester, tags=tags, root=screen_root) == []
    assert tester.calls == []


def test_screen_check_reports_a_failing_item_and_goes_on(screen_root, log):
    tester = FakeTester(fail={'a': RuntimeError('图片不存在: a.png')})
    results = assets_check.screen_check('Foo', '普通挑战页', 's', 'i', tester=tester, tags=SCREEN_TAGS, root=screen_root)
    assert [item['item'] for item in results] == ['a', 'b', 'c']        # 一条出错不中断整个体检
    assert results[0] == {'file': 'as/image.json', 'item': 'a', 'matched': False, 'similarity': None, 'threshold': 0.8,
                          'expected': True, 'error': '图片不存在: a.png'}
    assert all('error' not in item for item in results[1:])
    assert (results[1]['matched'], results[2]['matched']) == (True, True)
    assert any('as/image.json 的 a 打分失败' in message for message in log.messages('warning'))


def test_screen_check_error_without_a_message_names_the_exception_type(screen_root):
    tester = FakeTester(fail={'c': KeyError()})
    results = assets_check.screen_check('Foo', '普通挑战页', 's', 'i', tester=tester, tags=SCREEN_TAGS, root=screen_root)
    assert results[2]['error'] == 'KeyError'


def test_screen_check_reads_the_tags_file_under_root_by_default(screen_root):
    assets_check.save_tags(SCREEN_TAGS, assets_check.tags_path(screen_root))
    results = assets_check.screen_check('Foo', '战斗中', 's', 'i', tester=FakeTester(), root=screen_root)
    assert [(item['item'], item['expected']) for item in results] == [('a', False), ('b', True), ('c', False)]


def test_screen_check_without_tags_treats_everything_as_unclassified(screen_root):
    results = assets_check.screen_check('Foo', '未分类', 's', 'i', tester=FakeTester(), root=screen_root)
    assert [item['expected'] for item in results] == [True, True, True]
    assert assets_check.screen_check('Foo', '战斗中', 's', 'i', tester=FakeTester(), root=screen_root) == []


def test_screen_check_defaults_to_the_annotator_test_rule(screen_root, monkeypatch):
    tester = FakeTester()
    monkeypatch.setattr(annotator_tool.annotator_manager, 'test_rule', tester)
    results = assets_check.screen_check('Foo', '普通挑战页', 'session-9', 'image-9', tags=SCREEN_TAGS, root=screen_root)
    assert len(results) == 3
    assert tester.calls[0] == ('session-9', 'image-9', 'Foo', 'as/image.json', 'image', 'a')


def test_screen_check_rejects_an_unknown_task(screen_root):
    with pytest.raises(ValueError):
        assets_check.screen_check('../etc', '活动主页', 's', 'i', tester=FakeTester(), tags={}, root=screen_root)


def test_screen_check_passes_component_paths_the_annotator_way(screen_root):
    """组件同标注工具：任务名 Component，json 相对路径 <组件>/x.json（标注工具的 test_rule、缩略图都按这个找文件）。"""
    put_json(screen_root, 'tasks/Component/A/image.json', [image_rule('x')])
    put_json(screen_root, 'tasks/Component/B/ocr.json', [ocr_rule('o')])
    tags = {'Component': {'A/image.json': {'x': {'periodic': False, 'screen': '战斗中'}}}}
    tester = FakeTester()
    results = assets_check.screen_check('Component', '战斗中', 's', 'i', tester=tester, tags=tags, root=screen_root)
    assert [(item['file'], item['expected']) for item in results] == [('A/image.json', True), ('B/ocr.json', False)]
    assert tester.calls == [('s', 'i', 'Component', 'A/image.json', 'image', 'x'),
                            ('s', 'i', 'Component', 'B/ocr.json', 'ocr', 'o')]


# ------------------------------------------------------------------------------------------------------------------
# 替换图片
# ------------------------------------------------------------------------------------------------------------------

def test_write_refused_is_a_runtime_error_not_a_value_error():
    """HTTP 接口按类型分流：ValueError 回 400，WriteRefused 回 409。"""
    assert issubclass(assets_check.WriteRefused, RuntimeError)
    assert not issubclass(assets_check.WriteRefused, ValueError)


def test_replace_writes_and_regenerates(root):
    target = root / 'tasks/Foo/as/a.png'
    new = png_bytes((9, 8, 7), (1, 1))
    seen = []

    def regenerate(task):
        seen.append((task, pixels(target).tolist()))        # 调用时新图已经写好

    result, _ = replace(root, content=b64(new), regenerate=regenerate)

    assert result == {'target': 'tasks/Foo/as/a.png'}
    assert seen == [('Foo', [[[9, 8, 7]]])]
    assert pixels(target).shape == (1, 1, 3)
    assert set(tree(root)) == set(base_files())                 # 没多出临时文件


def test_replace_creates_a_missing_image(root):
    """规格 F18：json 里有、磁盘上没有的图（爬塔的 pass_8）要能补上，不是拒绝。"""
    assert not (root / 'tasks/Foo/as/gone.png').exists()
    result, regen = replace(root, 'gone.png', b64(png_bytes((5, 5, 5), (3, 2))))
    assert result == {'target': 'tasks/Foo/as/gone.png'}
    assert pixels(root / 'tasks/Foo/as/gone.png').shape == (2, 3, 3)
    assert regen.calls == ['Foo']


def test_replace_creates_a_missing_subdirectory(root):
    put_json(root, 'tasks/Foo/deep/image.json', [image_rule('d', 'sub/d.png')])
    result, _ = replace(root, 'sub/d.png', json_relpath='deep/image.json')
    assert result == {'target': 'tasks/Foo/deep/sub/d.png'}
    assert (root / 'tasks/Foo/deep/sub/d.png').is_file()


def test_replace_accepts_a_data_url(root):
    data = png_bytes((4, 5, 6), (2, 1))
    replace(root, content='data:image/png;base64,' + b64(data))
    assert pixels(root / 'tasks/Foo/as/a.png').tolist() == [[[4, 5, 6], [4, 5, 6]]]


def test_replace_accepts_wrapped_base64(root):
    data = png_bytes((4, 5, 6), (2, 1))
    replace(root, content='\n'.join(textwrap.wrap(b64(data), 20)))
    assert pixels(root / 'tasks/Foo/as/a.png').tolist() == [[[4, 5, 6], [4, 5, 6]]]


@pytest.mark.parametrize('name, magic', [('s.png', b'\x89PNG'), ('s.jpg', b'\xff\xd8\xff'), ('s.jpeg', b'\xff\xd8\xff'),
                                         ('s.bmp', b'BM'), ('S.PNG', b'\x89PNG')])
def test_replace_reencodes_to_the_extension_of_the_target(root, name, magic):
    """上传的是 PNG，目标是 .jpg 就得写成真的 JPEG：按目标后缀重新编码，而不是把上传的字节原样落盘。"""
    put_json(root, 'tasks/Foo/fmt/image.json', [image_rule('s', name)])
    result, _ = replace(root, name, b64(png_bytes((30, 60, 90), (8, 6))), json_relpath='fmt/image.json')
    written = (root / 'tasks/Foo/fmt' / name).read_bytes()
    assert written.startswith(magic)
    assert pixels(root / 'tasks/Foo/fmt' / name).shape == (6, 8, 3)
    assert result == {'target': f'tasks/Foo/fmt/{name}'}


def list_file(list_type: str, entry: dict) -> dict:
    """真实的列表文件：顶层 name、direction、type、roiBack、description，条目只有 itemName 和 roiFront
    （同 GameUi 的 page/list_image_act_list.json）。"""
    return {'name': 'lst', 'direction': 'vertical', 'type': list_type, 'roiBack': '0,0,100,100', 'description': '列表',
            'list': [entry]}


def test_replace_an_image_list_entry_uses_the_item_name(root):
    """RuleList 按 <json 所在目录>/<itemName>.png 加载列表里的图（ListExtractor 把 json 所在目录编成 folder），替换的就是这张。"""
    put_json(root, 'tasks/Foo/lists/list.json', list_file('image', {'itemName': 'e1', 'roiFront': '0,0,50,50'}))
    result, regen = replace(root, 'e1.png', json_relpath='lists/list.json')
    assert result == {'target': 'tasks/Foo/lists/e1.png'}
    assert pixels(root / 'tasks/Foo/lists/e1.png').shape == (3, 4, 3) and regen.calls == ['Foo']


@pytest.mark.parametrize('list_type, entry, image_name', [
    ('ocr', {'itemName': 'e1', 'roiFront': '0,0,50,50'}, 'e1.png'),                                 # 文字列表没有图
    ('image', {'itemName': 'e1', 'roiFront': '0,0,50,50', 'imageName': 'other.png'}, 'other.png'),  # RuleList 不看 imageName
], ids=['ocr-list', 'image-name-ignored'])
def test_replace_refuses_list_images_the_runtime_never_loads(root, list_type, entry, image_name):
    put_json(root, 'tasks/Foo/lists/list.json', list_file(list_type, entry))
    before = tree(root)
    with pytest.raises(ValueError):
        replace(root, image_name, json_relpath='lists/list.json')
    assert tree(root) == before


STRAY_CLICKS = [click_rule('c1'), click_rule('c2', imageName='c2.png')]      # 同真实的 SwitchSoul/ss/click.json


def test_replace_refuses_a_stray_image_name_in_a_click_file(root):
    """点击文件编成 RuleClick，条目上零星带着的 imageName 运行时不加载：不能替换。"""
    put_json(root, 'tasks/Foo/as/click.json', STRAY_CLICKS)
    before = tree(root)
    regen = Regen()
    with pytest.raises(ValueError, match='没有用到图片'):
        replace(root, 'c2.png', json_relpath='as/click.json', regenerate=regen)
    assert tree(root) == before and regen.calls == []


def test_replace_removes_a_stale_scratch_file(root):
    """上次写到一半进程被杀留下的临时文件（固定名 <图片名>.assets-check.tmp）：这次写入时先删掉，写完不留任何临时文件。"""
    write_files(root, {'tasks/Foo/as/a.png.assets-check.tmp': b'half-written'})
    replace(root)
    assert set(tree(root)) == set(base_files())
    assert pixels(root / 'tasks/Foo/as/a.png').shape == (3, 4, 3)


@pytest.mark.parametrize('image_name', ['../x.png', 'a.exe', 'other.png', 'as/a.png', '', 'a.PNG ', 'A.png'])
def test_replace_rejects_traversal_and_ext(root, image_name):
    """image_name 必须是这个 json 里某个条目的 imageName，所以路径穿越、乱七八糟的后缀、没登记的名字都进不来。"""
    before = tree(root)
    regen = Regen()
    with pytest.raises(ValueError):
        replace(root, image_name, regenerate=regen)
    assert tree(root) == before and regen.calls == []


@pytest.mark.parametrize('image_name', ['../../outside.png', '../../../outside.png', 'evil.exe', 'noext', 'a.png.exe', 'x.webp',
                                        'x.gif'])
def test_replace_checks_the_path_even_when_the_json_lists_the_name(root, image_name):
    """json 本身被改坏、条目指到任务目录外或不允许的后缀：登记过也不放行（防线是两层，不只靠名单）。"""
    put_json(root, 'tasks/Foo/as/odd.json', [image_rule('odd', image_name)])
    before = tree(root)
    regen = Regen()
    with pytest.raises(ValueError):
        replace(root, image_name, json_relpath='as/odd.json', regenerate=regen)
    assert tree(root) == before and regen.calls == []
    assert not (root / 'outside.png').exists() and not (root / 'tasks/outside.png').exists()


def test_replace_rejects_an_absolute_image_name_that_the_json_lists(root):
    outside = root / 'outside-abs.png'
    put_json(root, 'tasks/Foo/as/odd.json', [image_rule('odd', outside.as_posix())])
    before = tree(root)
    with pytest.raises(ValueError):
        replace(root, outside.as_posix(), json_relpath='as/odd.json')
    assert tree(root) == before and not outside.exists()


def test_replace_rejects_a_directory_target(root):
    (root / 'tasks/Foo/as/dir.png').mkdir()
    put_json(root, 'tasks/Foo/as/odd.json', [image_rule('odd', 'dir.png')])
    with pytest.raises(ValueError):
        replace(root, 'dir.png', json_relpath='as/odd.json')
    assert (root / 'tasks/Foo/as/dir.png').is_dir()


@pytest.mark.parametrize('json_relpath', ['nope.json', 'as/missing.json', '../Bar/image.json', '../../etc.json',
                                          'as/../as/image.json', '/as/image.json', 'as\\image.json', '', 'as/a.png'])
def test_replace_rejects_a_json_that_is_not_one_of_the_task(root, json_relpath):
    before = tree(root)
    with pytest.raises(ValueError):
        replace(root, json_relpath=json_relpath)
    assert tree(root) == before


@pytest.mark.parametrize('task', ['Missing', '../Foo', 'Foo/..', 'Component/A', '', None])
def test_replace_rejects_an_unknown_task(root, task):
    before = tree(root)
    with pytest.raises(ValueError):
        replace(root, task=task)
    assert tree(root) == before


def test_replace_rejects_a_broken_json(root):
    (root / 'tasks/Foo/as/bad.json').write_text('{', encoding='utf-8')
    with pytest.raises(ValueError):
        replace(root, json_relpath='as/bad.json')


@pytest.mark.parametrize('content', [
    'not base64 !!!',
    b64(b'this is not an image at all'),
    '',
    '   ',
    'data:image/png;base64,',
    'data:image/png,abc',                         # 不是 base64 编码的 data URL
    'data:image/png,' + b64(png_bytes()),         # 同上，哪怕逗号后面碰巧是一张合法图片的 base64
    b64(b'<html></html>'),
    None,
    12345,
])
def test_replace_rejects_non_image_and_keeps_original(root, content):
    original = (root / 'tasks/Foo/as/a.png').read_bytes()
    before = tree(root)
    regen = Regen()
    with pytest.raises(ValueError):
        assets_check.replace_image('Foo', 'as/image.json', 'a.png', content, root=root, regenerate=regen)
    assert (root / 'tasks/Foo/as/a.png').read_bytes() == original
    assert tree(root) == before and regen.calls == []        # 没写临时文件，也没去重新生成


def test_replace_keeps_the_original_when_the_write_fails(root, monkeypatch):
    original = (root / 'tasks/Foo/as/a.png').read_bytes()
    before = tree(root)
    regen = Regen()

    def boom(src, dst):
        raise OSError('disk gone')

    with monkeypatch.context() as patch:
        patch.setattr(assets_check.os, 'replace', boom)
        with pytest.raises(OSError):
            replace(root, regenerate=regen)
    assert (root / 'tasks/Foo/as/a.png').read_bytes() == original
    assert tree(root) == before and regen.calls == []          # 失败后临时文件也清掉了


def test_replace_refuses_while_another_write_holds_the_lock(root):
    before = tree(root)
    regen = Regen()
    with lock_held():
        with pytest.raises(assets_check.WriteRefused) as caught:
            replace(root, regenerate=regen)
    assert str(caught.value) == BUSY
    assert tree(root) == before and regen.calls == []


def test_replace_holds_the_lock_while_writing_and_regenerating(root, monkeypatch):
    held, real_replace = [], os.replace

    def spy_replace(src, dst):
        held.append(('write', fork_updater.MERGE_LOCK.locked()))
        real_replace(src, dst)

    def regenerate(task):
        held.append(('regenerate', fork_updater.MERGE_LOCK.locked()))

    monkeypatch.setattr(assets_check.os, 'replace', spy_replace)
    replace(root, regenerate=regenerate)
    assert held == [('write', True), ('regenerate', True)]


def test_replace_refuses_while_a_merge_is_in_progress(root):
    write_files(root, {'.git/MERGE_HEAD': '0' * 40 + '\n'})
    before = tree(root)
    regen = Regen()
    with pytest.raises(assets_check.WriteRefused) as caught:
        replace(root, regenerate=regen)
    assert str(caught.value) == MERGING
    assert tree(root) == before and regen.calls == []


def test_replace_releases_the_lock_when_regenerate_fails(root):
    def broken(task):
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        replace(root, regenerate=broken)
    assert fork_updater.MERGE_LOCK.acquire(blocking=False)
    fork_updater.MERGE_LOCK.release()
    assert pixels(root / 'tasks/Foo/as/a.png').shape == (3, 4, 3)       # 图已经换上了，重新生成的失败不回滚它


def test_replace_default_regenerate_rebuilds_assets_py(root, monkeypatch):
    monkeypatch.chdir(root)
    result = assets_check.replace_image('Foo', 'as/image.json', 'a.png', b64(png_bytes()), root=root)
    assert result == {'target': 'tasks/Foo/as/a.png'}
    assets = (root / 'tasks/Foo/assets.py').read_text(encoding='utf-8')
    assert 'class FooAssets' in assets and 'I_A = RuleImage(' in assets and 'I_GONE = RuleImage(' in assets
    assert 'file="./tasks/Foo/as/a.png"' in assets             # 图片路径按仓库根算，所以当前目录必须是 root


def test_replace_a_component_image_regenerates_only_that_component(root):
    """组件的图换完，重新生成的是那个组件自己的 assets.py：regenerate 收到 Component/<组件>（同 ForkUpdater.regenerate）。"""
    result, regen = replace(root, 'x.png', json_relpath='A/image.json', task='Component')
    assert result == {'target': 'tasks/Component/A/x.png'}
    assert regen.calls == ['Component/A']
    assert pixels(root / 'tasks/Component/A/x.png').shape == (3, 4, 3)


def test_replace_a_component_image_with_the_default_regenerate(root, monkeypatch):
    monkeypatch.chdir(root)
    assets_check.replace_image('Component', 'A/image.json', 'x.png', b64(png_bytes()), root=root)
    assets = (root / 'tasks/Component/A/assets.py').read_text(encoding='utf-8')
    assert 'class AAssets' in assets and 'file="./tasks/Component/A/x.png"' in assets
    assert not (root / 'tasks/Component/assets.py').exists()          # 容器目录不生成 assets.py


def test_replace_refuses_a_json_directly_under_the_component_container(root):
    """直接放在 tasks/Component 下的 json 不属于任何组件（标注工具也拒绝保存它）：不在清单里，不能替换。"""
    put_json(root, 'tasks/Component/loose.json', [image_rule('loose')])
    before = tree(root)
    regen = Regen()
    with pytest.raises(ValueError):
        replace(root, 'loose.png', json_relpath='loose.json', task='Component', regenerate=regen)
    assert tree(root) == before and regen.calls == []


def test_replace_default_regenerate_refuses_outside_the_repo_before_writing(root):
    """默认的重新生成按当前目录算图片路径：当前目录不是 root 就在写盘之前拒绝，不能图换了、assets.py 没生成。"""
    assert Path.cwd().resolve() != root.resolve()
    before = tree(root)
    with pytest.raises(RuntimeError, match='当前目录'):
        assets_check.replace_image('Foo', 'as/image.json', 'a.png', b64(png_bytes()), root=root)
    assert tree(root) == before


# ------------------------------------------------------------------------------------------------------------------
# 提交并推送
# ------------------------------------------------------------------------------------------------------------------

def test_commit_and_push_roundtrip(repo):
    repo.write({'tasks/Foo/as/a.png': b'changed', 'tasks/Foo/as/new.png': b'new', 'tasks/Foo/as/b.png': None,
                'tasks/Bar/x.png': b'bar changed', 'other.txt': 'dirty\n', 'untracked.txt': 'new\n'})

    result = assets_check.commit_and_push('Foo', '换了几张图', repo.runner, 'custom')

    assert result == {'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.fork_tip == result['commit']                    # fork 上能看到该提交
    assert repo.files_of(result['commit']) == {'tasks/Foo/as/a.png', 'tasks/Foo/as/new.png', 'tasks/Foo/as/b.png'}
    assert repo.message(result['commit']) == '换了几张图'
    assert repo.status() == [' M other.txt', ' M tasks/Bar/x.png', '?? untracked.txt']       # 别处的改动没被带进提交


def test_commit_without_changes_returns_none(repo, git_calls):
    before = repo.head
    result = assets_check.commit_and_push('Foo', '没有改动', repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': ''}
    assert repo.head == before == repo.fork_tip
    assert not any(call[0] == 'push' for call in git_calls)        # 没有改动，也没有没推上去的提交：不推送


@pytest.mark.parametrize('message', ['', '   ', '\n'])
def test_blank_message_gets_the_default(repo, message):
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    result = assets_check.commit_and_push('Foo', message, repo.runner, 'custom')
    assert repo.message(result['commit']) == '素材维护：替换 Foo 素材'


@pytest.mark.parametrize('task', ['../..', '..', 'Missing', 'Foo/..', 'Component/A', '', None])
def test_commit_and_push_rejects_an_unknown_task(repo, task):
    repo.write({'tasks/Foo/as/a.png': b'changed', 'other.txt': 'dirty\n'})
    before = repo.head
    with pytest.raises(ValueError):
        assets_check.commit_and_push(task, 'm', repo.runner, 'custom')
    assert repo.head == before and repo.status() == [' M other.txt', ' M tasks/Foo/as/a.png']


def test_a_failed_push_can_be_retried_without_a_new_change(repo, tmp_path):
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    repo.break_push(tmp_path / 'missing.git')

    first = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')

    assert first['commit'] == repo.head and first['pushed'] is False
    assert first['error'].startswith('推送失败：') and first['error'].endswith(PUSH_TAIL)
    assert NOT_A_REPO in first['error']
    assert repo.fork_tip != repo.head                           # 提交留在本地

    repo.fix_push()
    second = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')

    assert second == {'commit': None, 'pushed': True, 'error': ''}        # 这次没有新提交，但补推上去了
    assert repo.fork_tip == repo.head


def test_a_missing_tracking_ref_still_pushes(repo):
    """origin/<分支> 这个引用不存在（拉取只写了 FETCH_HEAD）时比不了领先数，照样推送。"""
    sh(repo.work, 'config', 'remote.origin.fetch', '+refs/heads/none:refs/remotes/origin/none')
    sh(repo.work, 'update-ref', '-d', 'refs/remotes/origin/custom')
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    result = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')
    assert result == {'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head


def test_fork_ahead_commits_nothing(repo):
    repo.rival_commit({'tasks/Foo/as/a.png': b'rival'})
    repo.write({'tasks/Foo/as/b.png': b'mine'})
    before = repo.head

    result = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')

    assert result == {'commit': None, 'pushed': False, 'error': fork_ahead(1)}
    assert repo.head == before
    assert repo.status() == [' M tasks/Foo/as/b.png']           # 改动还在工作区，没被提交


def test_fork_ahead_counts_the_commits_only_the_fork_has(repo):
    repo.rival_commit({'a.txt': '1'})
    repo.rival_commit({'b.txt': '2'})
    repo.write({'tasks/Foo/as/b.png': b'mine'})
    assert assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')['error'] == fork_ahead(2)


def test_diverged_commits_nothing(repo):
    repo.commit({'other.txt': 'local\n'})
    repo.rival_commit({'tasks/Foo/as/a.png': b'rival'})
    repo.write({'tasks/Foo/as/b.png': b'mine'})
    before = repo.head

    result = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')

    assert result == {'commit': None, 'pushed': False, 'error': DIVERGED}
    assert repo.head == before and repo.status() == [' M tasks/Foo/as/b.png']


def test_fetch_failure_commits_but_does_not_push(repo, tmp_path):
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    repo.point_origin_at(tmp_path / 'missing.git')
    fork_before = repo.fork_tip

    first = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')

    assert first['commit'] == repo.head and first['pushed'] is False        # 提交照做（启动器守卫不会冲掉它），不推送
    assert first['error'].startswith('拉取 fork 失败，没有推送：') and first['error'].endswith(FETCH_TAIL)
    assert NOT_A_REPO in first['error']
    assert repo.fork_tip == fork_before

    repo.point_origin_at(repo.origin)
    second = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')
    assert second == {'commit': None, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head


def test_busy_lock_is_refused_without_touching_git(repo, git_calls):
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    before = repo.head
    git_calls.clear()
    with lock_held():
        result = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': BUSY}
    assert git_calls == []
    assert repo.head == before and repo.status() == [' M tasks/Foo/as/a.png']


def test_commit_holds_the_lock_for_every_git_call(repo, monkeypatch):
    """从查 MERGE_HEAD、拉取、提交到推送，每一条 git 命令都在持有 MERGE_LOCK 时执行。"""
    held, real = [], GitRunner.run

    def spy(self, *args, check=True):
        held.append((args[0], fork_updater.MERGE_LOCK.locked()))
        return real(self, *args, check=check)

    monkeypatch.setattr(GitRunner, 'run', spy)
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    assert assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')['pushed'] is True
    assert {'rev-parse', 'fetch', 'add', 'commit', 'push'} <= {command for command, _ in held}
    assert [command for command, locked in held if not locked] == []


def test_merge_in_progress_is_refused(repo, git_calls):
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    write_files(repo.work, {'.git/MERGE_HEAD': repo.head + '\n'})
    before = repo.head
    result = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': MERGING}
    assert repo.head == before and repo.status() == [' M tasks/Foo/as/a.png']
    assert not {call[0] for call in git_calls} & {'fetch', 'add', 'commit', 'push'}


def test_a_git_failure_is_returned_not_raised(repo, monkeypatch):
    repo.write({'tasks/Foo/as/a.png': b'changed'})
    break_git(monkeypatch, 'commit', stderr='Author identity unknown\n\nfatal: unable to auto-detect email address')
    result = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': 'git 命令失败：Author identity unknown'}


def test_replace_then_commit_and_push(repo):
    """整条链路：换图（写盘 + 重新生成 assets.py）之后，提交并推送把图和 assets.py 一起带上去。"""
    def regenerate(task):
        write_files(repo.work, {f'tasks/{task}/assets.py': '# regenerated\n'})

    new = png_bytes((9, 9, 9), (2, 2))
    replace(repo.work, 'gone.png', b64(new), regenerate=regenerate)
    result = assets_check.commit_and_push('Foo', '', repo.runner, 'custom')
    assert result['pushed'] is True
    assert repo.files_of(result['commit']) == {'tasks/Foo/as/gone.png', 'tasks/Foo/assets.py'}
    assert sh(repo.origin, 'show', f'{repo.fork_tip}:tasks/Foo/assets.py') == '# regenerated'


# ------------------------------------------------------------------------------------------------------------------
# 替换并提交（页面上的"上传替换"：换图、重新生成 assets.py、提交、推送一步做完）
# ------------------------------------------------------------------------------------------------------------------

def worktree(root: Path) -> dict[str, bytes]:
    """tree 去掉 .git 里的文件：拉取会更新 .git，只看工作区有没有被改。"""
    return {name: data for name, data in tree(root).items() if not name.startswith('.git/')}


def replace_commit(repo: Repo, image_name='a.png', content=None, *, json_relpath='as/image.json', task='Foo',
                   regenerate=None):
    """调 replace_and_commit，默认换 as/image.json 里的 a.png；regenerate 不传就用记录器。返回 (结果, 记录器)。"""
    regen = regenerate if regenerate is not None else Regen()
    content = b64(png_bytes()) if content is None else content
    result = assets_check.replace_and_commit(task, json_relpath, image_name, content, repo.runner, 'custom',
                                             regenerate=regen)
    return result, regen


def test_replace_and_commit_roundtrip(repo, monkeypatch):
    """换图和重新生成的 assets.py 在同一个提交里推到 fork，提交说明写明换的是哪张图；工作区不留未提交的改动。"""
    monkeypatch.chdir(repo.work)                    # 默认的重新生成按当前目录算图片路径
    new = png_bytes((9, 8, 7), (3, 2))

    result = assets_check.replace_and_commit('Foo', 'as/image.json', 'a.png', b64(new), repo.runner, 'custom')

    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': True, 'commit': repo.head, 'pushed': True, 'error': '',
                      'uncommitted': False}
    assert repo.fork_tip == result['commit']                                    # fork 上能看到该提交
    assert repo.files_of(result['commit']) == {'tasks/Foo/as/a.png', 'tasks/Foo/assets.py'}
    assert repo.message(result['commit']) == '素材维护：替换 tasks/Foo/as/a.png'
    on_fork = subprocess.run(['git', 'show', 'custom:tasks/Foo/as/a.png'], cwd=repo.origin, capture_output=True).stdout
    assert on_fork == (repo.work / 'tasks/Foo/as/a.png').read_bytes()
    assert pixels(repo.work / 'tasks/Foo/as/a.png').shape == (2, 3, 3)
    assert 'I_A = RuleImage(' in (repo.work / 'tasks/Foo/assets.py').read_text(encoding='utf-8')
    assert repo.status() == []


def test_replace_and_commit_creates_a_missing_image(repo):
    """规格 F18：json 里有、磁盘上没有的图，补上之后同样立刻提交推送。"""
    result, regen = replace_commit(repo, 'gone.png')
    assert (result['target'], result['replaced'], result['pushed']) == ('tasks/Foo/as/gone.png', True, True)
    assert repo.files_of(result['commit']) == {'tasks/Foo/as/gone.png'}
    assert regen.calls == ['Foo'] and repo.fork_tip == repo.head


def test_replace_and_commit_refuses_before_writing_when_the_fork_is_ahead(repo):
    repo.rival_commit({'other.txt': 'rival\n'})
    before, files = repo.head, worktree(repo.work)

    result, regen = replace_commit(repo)

    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': False, 'commit': None, 'pushed': False,
                      'error': fork_ahead(1), 'uncommitted': False}
    assert worktree(repo.work) == files and regen.calls == []                 # 图没写，assets.py 也没重新生成
    assert repo.head == before and repo.status() == []


def test_replace_and_commit_refuses_when_busy(repo, git_calls):
    before = tree(repo.work)
    with lock_held():
        result, regen = replace_commit(repo)
    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': False, 'commit': None, 'pushed': False, 'error': BUSY,
                      'uncommitted': False}
    assert git_calls == [] and regen.calls == []
    assert tree(repo.work) == before


def test_replace_and_commit_refuses_while_a_merge_is_in_progress(repo, git_calls):
    write_files(repo.work, {'.git/MERGE_HEAD': repo.head + '\n'})
    before = tree(repo.work)
    result, regen = replace_commit(repo)
    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': False, 'commit': None, 'pushed': False, 'error': MERGING,
                      'uncommitted': False}
    assert tree(repo.work) == before and regen.calls == []
    assert not {call[0] for call in git_calls} & {'fetch', 'add', 'commit', 'push', 'status'}


def test_replace_and_commit_keeps_the_local_commit_when_the_push_fails(repo, tmp_path):
    repo.break_push(tmp_path / 'missing.git')

    result, _ = replace_commit(repo)

    assert (result['replaced'], result['commit'], result['pushed'], result['uncommitted']) == (True, repo.head, False, False)
    assert result['error'].startswith('推送失败：') and result['error'].endswith(PUSH_TAIL)
    assert repo.fork_tip != repo.head and repo.files_of(repo.head) == {'tasks/Foo/as/a.png'}
    repo.fix_push()
    assert assets_check.commit_and_push('Foo', '', repo.runner, 'custom') == {'commit': None, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head                                           # 补推上去了


def test_replace_and_commit_reports_a_git_failure_after_the_write(repo, monkeypatch):
    """图已经写下、提交失败：replaced 为真、commit 为空、uncommitted 为真，error 是原因（页面据此提示点"提交并推送"重试）。"""
    break_git(monkeypatch, 'commit', stderr='Author identity unknown\n\nfatal: unable to auto-detect email address')
    result, regen = replace_commit(repo)
    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': True, 'commit': None, 'pushed': False,
                      'error': 'git 命令失败：Author identity unknown', 'uncommitted': True}
    assert regen.calls == ['Foo'] and repo.status() == ['M  tasks/Foo/as/a.png']      # 已暂存、没提交成


def test_replace_and_commit_same_image_after_a_failed_push(repo, tmp_path):
    """推送失败之后再传一张一样的图（cv2 重新编码是确定的，写下去的字节相同）：没有新的提交，uncommitted 为假，
    error 是补推之前那个提交又失败的原因，不是"没有提交成功"；推送修好之后再传一次，补推上去，仍然没有新的提交。"""
    repo.break_push(tmp_path / 'missing.git')
    content = b64(png_bytes((9, 8, 7), (3, 2)))
    first, _ = replace_commit(repo, content=content)
    committed = repo.head
    assert (first['commit'], first['pushed'], first['uncommitted']) == (committed, False, False)

    again, regen = replace_commit(repo, content=content)

    assert {key: again[key] for key in ('replaced', 'commit', 'pushed', 'uncommitted')} == {
        'replaced': True, 'commit': None, 'pushed': False, 'uncommitted': False}
    assert again['error'].startswith('推送失败：') and again['error'].endswith(PUSH_TAIL)
    assert regen.calls == ['Foo'] and repo.head == committed and repo.status() == []

    repo.fix_push()
    pushed, _ = replace_commit(repo, content=content)
    assert pushed == {'target': 'tasks/Foo/as/a.png', 'replaced': True, 'commit': None, 'pushed': True, 'error': '',
                      'uncommitted': False}
    assert repo.fork_tip == repo.head == committed


def test_replace_and_commit_same_image_as_the_fork(repo):
    """传上来的图和已提交、已推送的一样：图照样重写一遍，没有提交、不推送，也没有错误。"""
    result, _ = replace_commit(repo, content=b64(OLD_PNG))
    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': True, 'commit': None, 'pushed': False, 'error': '',
                      'uncommitted': False}
    assert repo.status() == [] and repo.fork_tip == repo.head


def test_replace_and_commit_asks_git_about_the_image_under_the_lock(repo, monkeypatch):
    """uncommitted 取自 git status 对这张图的看法，在提交推送之后、放锁之前查，只查这一个路径。"""
    checks, real = [], GitRunner.run

    def spy(self, *args, check=True):
        if args[0] == 'status':
            checks.append((args[-2:], fork_updater.MERGE_LOCK.locked()))
        return real(self, *args, check=check)

    monkeypatch.setattr(GitRunner, 'run', spy)
    result, _ = replace_commit(repo, 'gone.png')
    assert (result['pushed'], result['uncommitted']) == (True, False)
    assert checks == [(('--', 'tasks/Foo/as/gone.png'), True)]


def test_replace_and_commit_counts_an_unknown_git_status_as_uncommitted(repo, monkeypatch, log):
    """git status 自己失败时看不出图提交了没有：按没提交算（页面会提示点"提交并推送"，真没有改动时只是一次空操作），记警告。"""
    break_git(monkeypatch, 'status', stderr='fatal: index file corrupt')
    result, _ = replace_commit(repo, content=b64(OLD_PNG))
    assert (result['replaced'], result['commit'], result['error'], result['uncommitted']) == (True, None, '', True)
    assert any('index file corrupt' in message for message in log.messages('warning'))


@pytest.mark.parametrize('task, json_relpath, image_name, content', [
    ('Missing', 'as/image.json', 'a.png', b64(png_bytes())),
    ('Foo', 'as/nope.json', 'a.png', b64(png_bytes())),
    ('Foo', 'as/image.json', '../x.png', b64(png_bytes())),
    ('Foo', 'as/image.json', 'a.png', 'not base64 !!!'),
    ('Foo', 'as/image.json', 'a.png', b64(b'this is not an image at all')),
], ids=['task', 'json', 'image-name', 'base64', 'not-an-image'])
def test_replace_and_commit_validates_before_touching_git(repo, git_calls, task, json_relpath, image_name, content):
    """校验同 replace_image，一律在动 git 和磁盘之前做完：不通过就抛 ValueError，一条 git 命令都不跑。"""
    before = tree(repo.work)
    regen = Regen()
    with pytest.raises(ValueError):
        assets_check.replace_and_commit(task, json_relpath, image_name, content, repo.runner, 'custom', regenerate=regen)
    assert git_calls == [] and regen.calls == [] and tree(repo.work) == before


def test_replace_and_commit_refuses_a_stray_image_name_in_a_click_file(repo, git_calls):
    put_json(repo.work, 'tasks/Foo/as/click.json', STRAY_CLICKS)
    before = tree(repo.work)
    regen = Regen()
    with pytest.raises(ValueError, match='没有用到图片'):
        replace_commit(repo, 'c2.png', json_relpath='as/click.json', regenerate=regen)
    assert git_calls == [] and regen.calls == [] and tree(repo.work) == before


def test_commits_leave_scratch_files_out(repo):
    """别的图写到一半留下的临时文件留在任务目录里：上传替换和提交并推送都不把它带进提交，它只是没跟踪的残渣。"""
    repo.write({'tasks/Foo/as/b.png.assets-check.tmp': b'half-written'})

    replaced, _ = replace_commit(repo)
    repo.write({'tasks/Foo/as/老图.png': b'changed'})
    committed = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')

    assert repo.files_of(replaced['commit']) == {'tasks/Foo/as/a.png'}
    assert repo.files_of(committed['commit']) == {'tasks/Foo/as/老图.png'}
    assert repo.status() == ['?? tasks/Foo/as/b.png.assets-check.tmp']


def test_replace_and_commit_a_component_image(repo):
    """组件的提交路径是 tasks/Component（整个容器算一个任务）：别的组件留下的改动一起提交，别的任务的不带。"""
    repo.write({'tasks/Component/B/new.png': b'from the annotator', 'tasks/Foo/as/a.png': b'elsewhere'})

    result, regen = replace_commit(repo, 'x.png', json_relpath='A/image.json', task='Component')

    assert result == {'target': 'tasks/Component/A/x.png', 'replaced': True, 'commit': repo.head, 'pushed': True,
                      'error': '', 'uncommitted': False}
    assert regen.calls == ['Component/A']
    assert repo.files_of(result['commit']) == {'tasks/Component/A/x.png', 'tasks/Component/B/new.png'}
    assert repo.message(result['commit']) == '素材维护：替换 tasks/Component/A/x.png'
    assert repo.fork_tip == repo.head and repo.status() == [' M tasks/Foo/as/a.png']


def test_commit_and_push_a_component_change(repo):
    repo.write({'tasks/Component/A/x.png': b'changed'})
    result = assets_check.commit_and_push('Component', '', repo.runner, 'custom')
    assert result == {'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.files_of(result['commit']) == {'tasks/Component/A/x.png'}
    assert repo.message(result['commit']) == '素材维护：替换 Component 素材'


def test_replace_and_commit_default_regenerate_refuses_outside_the_repo_before_git(repo, git_calls):
    assert Path.cwd().resolve() != repo.work.resolve()
    before = tree(repo.work)
    with pytest.raises(RuntimeError, match='当前目录'):
        assets_check.replace_and_commit('Foo', 'as/image.json', 'a.png', b64(png_bytes()), repo.runner, 'custom')
    assert git_calls == [] and tree(repo.work) == before


# ------------------------------------------------------------------------------------------------------------------
# 保存标签（提交并推送标签文件）
# ------------------------------------------------------------------------------------------------------------------

TAGS = {'Foo': {'as/image.json': {'a': {'periodic': True, 'screen': '普通挑战页'}}}}


def test_commit_tags_commits_and_pushes_only_the_tags_file(repo):
    repo.write({'tasks/Foo/as/a.png': b'dirty', 'untracked.txt': 'x\n'})

    result = assets_check.commit_tags(TAGS, repo.runner, 'custom')

    assert result == {'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.fork_tip == result['commit']
    assert repo.files_of(result['commit']) == {TAGS_FILE}
    assert repo.message(result['commit']) == '素材维护：更新标签'
    assert assets_check.load_tags(repo.work / TAGS_FILE) == TAGS
    assert (repo.work / TAGS_FILE).read_text(encoding='utf-8') == json.dumps(TAGS, ensure_ascii=False, indent=2) + '\n'
    assert repo.status() == [' M tasks/Foo/as/a.png', '?? untracked.txt']        # 别处的改动不受影响


def test_commit_tags_writes_the_file_while_holding_the_lock(repo, monkeypatch):
    held, real = [], assets_check.save_tags

    def spy(tags, path=None):
        held.append(fork_updater.MERGE_LOCK.locked())
        real(tags, path)

    monkeypatch.setattr(assets_check, 'save_tags', spy)
    assert assets_check.commit_tags(TAGS, repo.runner, 'custom')['pushed'] is True
    assert held == [True]


def test_commit_tags_with_an_explicit_path_inside_the_repo(repo):
    path = repo.work / 'assets' / 'custom' / 'more_tags.json'
    result = assets_check.commit_tags(TAGS, repo.runner, 'custom', path=path)
    assert result['pushed'] is True
    assert repo.files_of(result['commit']) == {'assets/custom/more_tags.json'}      # 以仓库相对路径提交
    assert assets_check.load_tags(path) == TAGS


def test_commit_tags_rejects_a_path_outside_the_repo(repo, tmp_path, git_calls):
    outside = tmp_path / 'elsewhere' / 'tags.json'
    before = repo.head
    with pytest.raises(ValueError):
        assets_check.commit_tags(TAGS, repo.runner, 'custom', path=outside)
    assert not outside.exists() and repo.head == before
    assert git_calls == []                      # 先核对参数，不联网、不碰仓库


def test_commit_tags_rejects_the_repo_root_as_the_path(repo, git_calls):
    """仓库根目录本身不是"仓库里的文件"：拿它当标签文件，提交的路径会变成 "."，把整个工作区都带进去。"""
    with pytest.raises(ValueError):
        assets_check.commit_tags(TAGS, repo.runner, 'custom', path=repo.work)
    assert git_calls == [] and repo.status() == []


@pytest.mark.parametrize('tags', [[], [{'Foo': {}}], 'Foo', None])
def test_commit_tags_rejects_tags_that_are_not_an_object(repo, git_calls, tags):
    """标签文件顶层必须是对象（load_tags 把别的当成文件损坏）：不是对象就在动仓库之前拒绝，免得提交一份读不回来的文件。"""
    with pytest.raises(ValueError):
        assets_check.commit_tags(tags, repo.runner, 'custom')
    assert git_calls == []
    assert (repo.work / TAGS_FILE).read_text(encoding='utf-8') == '{}\n' and repo.status() == []


def test_commit_tags_without_changes_returns_none(repo):
    first = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert first['pushed'] is True
    second = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert second == {'commit': None, 'pushed': False, 'error': ''}


def test_commit_tags_refuses_before_writing_when_the_fork_is_ahead(repo):
    repo.rival_commit({'other.txt': 'rival\n'})
    before = repo.head
    result = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': fork_ahead(1)}
    assert (repo.work / TAGS_FILE).read_text(encoding='utf-8') == '{}\n'       # 被拒绝时一个字都没写，检出保持干净
    assert repo.head == before and repo.status() == []


def test_commit_tags_refuses_before_writing_when_diverged(repo):
    repo.commit({'other.txt': 'local\n'})
    repo.rival_commit({'tasks/Foo/as/a.png': b'rival'})
    result = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': DIVERGED}
    assert (repo.work / TAGS_FILE).read_text(encoding='utf-8') == '{}\n' and repo.status() == []


def test_commit_tags_refuses_when_busy(repo):
    with lock_held():
        result = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': BUSY}
    assert (repo.work / TAGS_FILE).read_text(encoding='utf-8') == '{}\n' and repo.status() == []


def test_commit_tags_refuses_while_a_merge_is_in_progress(repo):
    write_files(repo.work, {'.git/MERGE_HEAD': repo.head + '\n'})
    result = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert result == {'commit': None, 'pushed': False, 'error': MERGING}
    assert (repo.work / TAGS_FILE).read_text(encoding='utf-8') == '{}\n' and repo.status() == []


def test_commit_tags_failed_push_keeps_the_commit_and_can_be_retried(repo, tmp_path):
    repo.break_push(tmp_path / 'missing.git')
    first = assets_check.commit_tags(TAGS, repo.runner, 'custom')
    assert first['commit'] == repo.head and first['pushed'] is False and first['error'].startswith('推送失败：')
    repo.fix_push()
    assert assets_check.commit_tags(TAGS, repo.runner, 'custom') == {'commit': None, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head


# ------------------------------------------------------------------------------------------------------------------
# 导出补丁
# ------------------------------------------------------------------------------------------------------------------

def names_in(zip_path: Path) -> set[str]:
    """zip 里的文件名，不含 git archive 顺手加的目录项。"""
    with zipfile.ZipFile(zip_path) as archive:
        return {name for name in archive.namelist() if not name.endswith('/')}


def test_export_patch_contains_changed_files(repo, tmp_path):
    repo.write({'tasks/Foo/as/a.png': BINARY, 'tasks/Foo/as/新图.png': BINARY + b'2', 'tasks/Foo/assets.py': '# new\n'})
    sha = assets_check.commit_and_push('Foo', 'm', repo.runner, 'custom')['commit']
    out_dir = tmp_path / 'out'

    zip_path = assets_check.export_patch(sha, repo.runner, out_dir)

    assert zip_path == out_dir.resolve() / f'assets-patch-{sha[:7]}.zip'
    assert names_in(zip_path) == repo.files_of(sha)
    assert repo.files_of(sha) == {'tasks/Foo/as/a.png', 'tasks/Foo/as/新图.png', 'tasks/Foo/assets.py'}
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.read('tasks/Foo/as/a.png') == BINARY            # 二进制内容一个字节都不变
        assert archive.read('tasks/Foo/as/新图.png') == BINARY + b'2'


def test_export_patch_leaves_out_deleted_files(repo, tmp_path):
    """补丁包是解压覆盖用的，删掉的文件没有内容可放：只收新增和修改的。"""
    repo.commit({'tasks/Foo/as/a.png': BINARY, 'tasks/Foo/as/b.png': None})
    zip_path = assets_check.export_patch(repo.head, repo.runner, tmp_path / 'out')
    assert repo.files_of(repo.head) == {'tasks/Foo/as/a.png', 'tasks/Foo/as/b.png'}
    assert names_in(zip_path) == {'tasks/Foo/as/a.png'}


def test_export_patch_of_the_root_commit(repo, tmp_path):
    root_sha = sh(repo.work, 'rev-list', '--max-parents=0', 'HEAD')
    zip_path = assets_check.export_patch(root_sha, repo.runner, tmp_path / 'out')
    assert names_in(zip_path) == set(base_files())


def test_export_patch_accepts_short_sha_and_refs(repo, tmp_path):
    repo.commit({'tasks/Foo/as/a.png': BINARY})
    sha = repo.head
    for ref in (sha[:8], 'HEAD'):
        zip_path = assets_check.export_patch(ref, repo.runner, tmp_path / ref)
        assert zip_path.name == f'assets-patch-{sha[:7]}.zip'
        assert names_in(zip_path) == {'tasks/Foo/as/a.png'}


def test_export_patch_treats_file_names_literally(repo, tmp_path):
    """文件名里的 [ ] 不当通配符：a[1].png 不能把同目录没改动的 a1.png 也带进补丁包。"""
    repo.commit({'tasks/Foo/as/a1.png': BINARY}, 'plain name')
    repo.commit({'tasks/Foo/as/a[1].png': BINARY + b'2'}, 'bracket name')
    zip_path = assets_check.export_patch(repo.head, repo.runner, tmp_path / 'out')
    assert names_in(zip_path) == {'tasks/Foo/as/a[1].png'}


def test_export_patch_does_not_touch_the_checkout(repo, tmp_path):
    repo.commit({'tasks/Foo/as/a.png': BINARY})
    repo.write({'other.txt': 'dirty\n'})
    assets_check.export_patch(repo.head, repo.runner, tmp_path / 'out')
    assert repo.status() == [' M other.txt']


@pytest.mark.parametrize('commit', ['', ' ', 'nope', '0' * 40, 'HEAD~99', '--help', '-x', '--output=x', None])
def test_export_patch_rejects_an_unknown_commit(repo, tmp_path, commit):
    with pytest.raises(ValueError):
        assets_check.export_patch(commit, repo.runner, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_export_patch_without_changed_files_raises(repo, tmp_path):
    repo.commit({'tasks/Foo/as/b.png': None}, 'only a deletion')
    with pytest.raises(ValueError):
        assets_check.export_patch(repo.head, repo.runner, tmp_path / 'out')
    assert not list(tmp_path.rglob('*.zip'))
