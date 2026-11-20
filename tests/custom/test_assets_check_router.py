"""素材维护页接口（module.server.assets_check_router）的测试：处理函数、错误映射、路由注册和静态目录。

业务接口都是普通 def（FastAPI 把它们放进线程池跑），用例直接调；页面是 async，用 asyncio.run。路由、静态目录、
错误响应体再按 ASGI 直接调一次 app 验证：不起服务器，也不用 TestClient（它依赖的 httpx 没装）。
接口按服务进程的当前目录找 tasks/ 和标签文件（root=None），所以用例先 monkeypatch.chdir 到临时目录。
module.server.tool 导入时按当前目录定出 PROJECT_ROOT，所以在收集阶段（当前目录是仓库根）就先导入它。
git 的用例在 tmp_path 下跑：裸仓库当 fork、它的工作克隆当运行目录（带本地提交身份），接口的 runner 和分支换成这一份，
不构造 DeployConfig（它会在当前目录建 config/deploy.yaml）。
AssetsExtractor 会跳过路径里含 "temp" 的 json，所以用例名和夹具里都不带这个子串，root 夹具也会检查。
"""
import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from starlette.routing import Mount

from module.server import assets_check, assets_check_router, fork_updater
from module.server import tool as annotator_tool
from module.server.assets_check_router import (WEB_DIR, CommitPushBody, NoCacheStaticFiles, ReplaceImageBody,
                                               ScreenCheckBody, assets_api, assets_check_page, get_export, get_rules,
                                               get_static_check, get_tags, get_tasks, post_commit_push,
                                               post_replace_image, post_screen_check, put_tags)
from module.server.custom_routes import register_custom
from module.server.fork_git import GitRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
TAGS_FILE = 'assets/custom/assets_tags.json'
SCREENS = ['活动主页', '普通挑战页', 'Boss挑战页', '战斗中', '退出时', '未分类']
EMPTY_SCREEN_HINT = '所选画面没有已标注的图片或 OCR 条目'
ANNOTATOR_STATIC = Path(annotator_tool.__file__).resolve().parent / 'web' / 'annotator' / 'static'
REAL_RUNNER_AND_BRANCH = assets_check_router._runner_and_branch


class LogSpy:
    """换掉 logger：按级别记下消息，不往真日志里写。"""

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
    monkeypatch.setattr(assets_check_router, 'logger', spy)
    return spy


@pytest.fixture(autouse=True)
def no_deploy_config(monkeypatch):
    """用例没换 git 时，接口一碰 runner 就失败：免得真去构造 DeployConfig。"""
    def refuse():
        raise AssertionError('用例没有指定运行目录的 git')

    monkeypatch.setattr(assets_check_router, '_runner_and_branch', refuse)


@pytest.fixture(autouse=True)
def lock_is_released():
    """每个用例结束时 MERGE_LOCK 必须是空闲的。"""
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


OLD_PNG = png_bytes((1, 2, 3), (2, 2))
NEW_PNG = png_bytes((200, 100, 50), (5, 4))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def pixels(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
    assert image is not None, f'读不出图片：{path}'
    return image


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def put_json(root: Path, relpath: str, data) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_text(data).encode('utf-8'))
    return path


def put_bytes(root: Path, relpath: str, data: bytes) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def image_rule(name: str, image: str | None = None, **extra) -> dict:
    return {'itemName': name, 'imageName': image or f'{name}.png', 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20',
            'method': 'Template matching', 'threshold': 0.8, 'description': f'{name} 的描述', **extra}


def ocr_rule(name: str, **extra) -> dict:
    return {'itemName': name, 'roiFront': '0,0,10,10', 'roiBack': '0,0,20,20', 'mode': 'Single', 'method': 'Default',
            'keyword': '', 'description': f'{name} 的描述', **extra}


def generate_assets(root: Path, task: str = 'Foo') -> Path:
    """用真的 AssetsExtractor 生成 tasks/<task>/assets.py。当前目录必须是 root。"""
    from dev_tools.assets_extract import AssetsExtractor
    AssetsExtractor(str(root / 'tasks' / task)).extract()
    return root / 'tasks' / task / 'assets.py'


def files_under(root: Path) -> dict[str, bytes]:
    """root 下全部文件的 {相对路径: 内容}，用来断言"什么都没变"（包括没留下临时文件）。"""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def http_error(call) -> HTTPException:
    """调 call，断言它抛 HTTPException，返回这个异常。"""
    with pytest.raises(HTTPException) as caught:
        call()
    return caught.value


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """空的仓库根，当前目录已切到这里。"""
    assert 'temp' not in str(tmp_path), f'临时目录路径里带 temp，AssetsExtractor 会跳过其中所有 json：{tmp_path}'
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def foo(root) -> Path:
    """Foo 任务：as/image.json 里 a、b 两个图片条目（a.png 在，b.png 缺），as/ocr.json 里一个 OCR 条目 c；assets.py 刚生成。"""
    put_json(root, 'tasks/Foo/as/image.json', [image_rule('a'), image_rule('b')])
    put_json(root, 'tasks/Foo/as/ocr.json', [ocr_rule('c')])
    put_bytes(root, 'tasks/Foo/as/a.png', OLD_PNG)
    generate_assets(root)
    return root


def replace_body(image_name='a.png', content=None, json_relpath='as/image.json') -> ReplaceImageBody:
    content = b64(NEW_PNG) if content is None else content
    return ReplaceImageBody(json_relpath=json_relpath, image_name=image_name, content_base64=content)


# ------------------------------------------------------------------------------------------------------------------
# git 夹具：裸仓库当 fork，工作克隆当运行目录
# ------------------------------------------------------------------------------------------------------------------

def sh(cwd, *args) -> str:
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, encoding='utf-8')
    assert done.returncode == 0, f'git {" ".join(args)} 失败：{done.stderr}'
    return done.stdout.strip()


def configure(repo: Path) -> None:
    for key, value in (('user.name', 'tester'), ('user.email', 'tester@example.com'),
                       ('core.autocrlf', 'false'), ('commit.gpgsign', 'false')):
        sh(repo, 'config', key, value)


@pytest.fixture(scope='module')
def seed_repo(tmp_path_factory) -> Path:
    """origin.git（裸仓库，分支 custom）和从它克隆出来的 work：Foo 任务（as/image.json 里 a.png 在、b.png 缺，as/ocr.json 里
    一个 OCR 条目 c，assets.py 已生成）和空标签文件。每个用例拷一份，比重建快。种子仓库放在另一个目录里，免得被一起拷走。"""
    source, base = tmp_path_factory.mktemp('router-seed-source'), tmp_path_factory.mktemp('router-seed-repo')
    seed, origin, work = source / 'seed', base / 'origin.git', base / 'work'
    sh(base, 'init', '-q', '--bare', '--template=', str(origin))
    sh(origin, 'symbolic-ref', 'HEAD', 'refs/heads/custom')
    sh(source, 'init', '-q', '--template=', str(seed))
    configure(seed)
    sh(seed, 'checkout', '-q', '-b', 'custom')
    put_json(seed, 'tasks/Foo/as/image.json', [image_rule('a'), image_rule('b')])
    put_json(seed, 'tasks/Foo/as/ocr.json', [ocr_rule('c')])
    put_bytes(seed, 'tasks/Foo/as/a.png', OLD_PNG)
    put_bytes(seed, TAGS_FILE, b'{}\n')
    previous = os.getcwd()
    os.chdir(seed)                  # AssetsExtractor 按当前目录算图片路径
    try:
        generate_assets(seed)
    finally:
        os.chdir(previous)
    sh(seed, 'add', '-A')
    sh(seed, 'commit', '-q', '-m', 'base')
    sh(seed, 'push', '-q', str(origin), 'HEAD:refs/heads/custom')
    sh(base, 'clone', '-q', '--template=', '-c', 'core.autocrlf=false', str(origin), str(work))
    configure(work)
    return base


class Repo:
    """seed_repo 的一份拷贝：origin.git（fork）、work（运行目录）和接口用的 GitRunner。"""

    def __init__(self, base: Path):
        self.base, self.origin, self.work = base, base / 'origin.git', base / 'work'
        self.runner = GitRunner('git', self.work)

    @property
    def head(self) -> str:
        return sh(self.work, 'rev-parse', 'HEAD')

    @property
    def fork_tip(self) -> str:
        return sh(self.origin, 'rev-parse', 'refs/heads/custom')

    def message(self, sha: str) -> str:
        return sh(self.work, 'log', '-1', '--format=%B', sha)

    def files_of(self, sha: str) -> set[str]:
        return set(filter(None, sh(self.work, 'diff-tree', '-r', '--root', '--no-commit-id', '--name-only', '-z',
                                   sha).split('\0')))

    def status(self) -> list[str]:
        return sorted(sh(self.work, 'status', '--porcelain', '--untracked-files=all').splitlines())

    def rival_commit(self) -> None:
        """另一份克隆往 fork 推一个提交：fork 上多出运行目录没有的提交。"""
        rival = self.base / 'rival'
        sh(self.base, 'clone', '-q', '--template=', str(self.origin), str(rival))
        configure(rival)
        put_bytes(rival, 'rival.txt', b'rival\n')
        sh(rival, 'add', '-A')
        sh(rival, 'commit', '-q', '-m', 'rival')
        sh(rival, 'push', '-q', 'origin', 'HEAD:refs/heads/custom')


@pytest.fixture
def repo(seed_repo, tmp_path, monkeypatch) -> Repo:
    """运行目录：当前目录切到 work，接口的 runner 换成它，fork 分支是 custom。"""
    assert 'temp' not in str(tmp_path), f'临时目录路径里带 temp，AssetsExtractor 会跳过其中所有 json：{tmp_path}'
    base = tmp_path / 'repo'
    shutil.copytree(seed_repo, base)
    copy = Repo(base)
    sh(copy.work, 'remote', 'set-url', 'origin', str(copy.origin))
    monkeypatch.chdir(copy.work)
    monkeypatch.setattr(assets_check_router, '_runner_and_branch', lambda: (copy.runner, 'custom'))
    return copy


# ------------------------------------------------------------------------------------------------------------------
# ASGI：不起服务器，直接调一次 app
# ------------------------------------------------------------------------------------------------------------------

def http_request(app, method: str, path: str, query: str = '', body: bytes = b'',
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    """按 ASGI 直接调一次 app，返回 (状态码, 响应头（键小写）, 响应体)。"""
    messages = []

    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async def send(message):
        messages.append(message)

    raw_headers = [(key.lower().encode('latin-1'), value.encode('latin-1')) for key, value in (headers or {}).items()]
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method, 'scheme': 'http',
             'path': path, 'raw_path': path.encode('utf-8'), 'root_path': '', 'query_string': query.encode('ascii'),
             'headers': raw_headers, 'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 80)}
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message['type'] == 'http.response.start')
    payload = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    response_headers = {key.decode('latin-1').lower(): value.decode('latin-1') for key, value in start['headers']}
    return start['status'], response_headers, payload


def http_get(app, path: str, query: str = '') -> tuple[int, dict[str, str], bytes]:
    return http_request(app, 'GET', path, query)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    register_custom(app)
    return app


# ------------------------------------------------------------------------------------------------------------------
# 任务、清单、静态检查
# ------------------------------------------------------------------------------------------------------------------

def test_tasks_default_pair_or_all(root):
    for name in ('Foo', 'MetaDemon', 'ActivityShikigami', 'Component', '__pycache__'):
        (root / 'tasks' / name).mkdir(parents=True)
    assert get_tasks(all_tasks=False) == {'tasks': ['ActivityShikigami', 'MetaDemon']}
    assert get_tasks(all_tasks=True) == {'tasks': ['ActivityShikigami', 'Component', 'Foo', 'MetaDemon']}


def test_component_paths_match_the_annotator():
    """页面的缩略图（标注工具的 image-preview）和体检（test_rule）都把 task_name=Component、json_relpath=<组件>/x.json 交给
    标注工具。拿真实仓库核对：标注工具按这两个参数找到的 json 就是清单里的那个文件，它认定的 assets.py 目录与这里的一致，
    缩略图找得到图。"""
    manager = annotator_tool.AnnotatorManager
    task_dir = (REPO_ROOT / 'tasks' / 'Component').resolve()
    rules = assets_check.list_rules('Component', root=REPO_ROOT)
    assert len(rules) > 50
    for rule in rules:
        root_dir, json_file = manager._resolve_json_path('Component', rule['file'])
        assert (root_dir, json_file) == (task_dir, task_dir / rule['file'])
        assert manager._resolve_assets_extract_target(root_dir, json_file)[0] == assets_check._extract_root(task_dir,
                                                                                                           json_file)
    reward = annotator_tool.annotator_manager.get_rule_image_file('Component', 'GeneralBattle/gb/image.json',
                                                                  'gb_reward.png')
    assert reward == task_dir / 'GeneralBattle' / 'gb' / 'gb_reward.png'


def test_rules_include_tags(foo):
    put_json(foo, TAGS_FILE, {'Foo': {'as/image.json': {'a': {'periodic': True, 'screen': '战斗中'}},
                                      'as/ocr.json': {'c': {'periodic': False, 'screen': '退出时'}}}})

    result = get_rules('Foo')

    assert result['task'] == 'Foo'
    assert result['screens'] == SCREENS
    assert [(rule['file'], rule['rule_type']) for rule in result['rules']] == [('as/image.json', 'image'),
                                                                                ('as/ocr.json', 'ocr')]
    tags = {(rule['file'], item['itemName']): (item['periodic'], item['screen'])
            for rule in result['rules'] for item in rule['items']}
    assert tags == {('as/image.json', 'a'): (True, '战斗中'), ('as/image.json', 'b'): (False, '未分类'),
                    ('as/ocr.json', 'c'): (False, '退出时')}
    first = result['rules'][0]['items'][0]
    assert (first['imageName'], first['roiFront'], first['threshold']) == ('a.png', '0,0,10,10', 0.8)   # json 字段原样带着


def test_rules_screens_follow_the_task(root):
    put_json(root, 'tasks/MetaDemon/md/image.json', [image_rule('boss')])
    assert get_rules('MetaDemon')['screens'] == SCREENS + ['超鬼王主页', '超鬼王战斗', '合成页']


def test_static_check_endpoint(foo):
    expected = {'kind': 'missing_image', 'file': 'as/image.json', 'item': 'b', 'detail': '图片不存在：as/b.png'}
    assert get_static_check('Foo') == {'issues': [expected]}

    (foo / 'tasks/Foo/assets.py').write_bytes(b'# stale\n')         # 默认 root 就是当前目录：assets.py 比对照常进行
    assert [issue['kind'] for issue in get_static_check('Foo')['issues']] == ['missing_image', 'assets_outdated']


# ------------------------------------------------------------------------------------------------------------------
# 标签
# ------------------------------------------------------------------------------------------------------------------

def test_get_tags_returns_the_whole_file(root):
    tags = {'Foo': {'as/image.json': {'a': {'periodic': True, 'screen': '战斗中'}}}}
    put_json(root, TAGS_FILE, tags)
    assert get_tags() == tags


def test_get_tags_without_a_file_is_empty(root):
    assert get_tags() == {}


def test_put_tags_commits_with_the_resolved_runner_and_branch(repo, monkeypatch):
    calls, real = [], assets_check.commit_tags

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(assets_check, 'commit_tags', spy)
    tags = {'Foo': {'as/image.json': {'a': {'periodic': True, 'screen': '战斗中'}}}}

    result = put_tags(tags)

    assert calls == [((tags, repo.runner, 'custom'), {})]
    assert result == {'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head
    assert repo.files_of(repo.head) == {TAGS_FILE}
    assert get_tags() == tags                                       # 刷新后读回来的就是刚保存的


def test_put_tags_refusal_comes_back_in_the_result(repo):
    """提交类接口的失败原因在返回值的 error 里（HTTP 200），由页面显示，不转成 HTTP 错误。"""
    repo.rival_commit()
    before = repo.head

    result = put_tags({'Foo': {}})

    assert result['commit'] is None and result['pushed'] is False
    assert result['error'].startswith('fork 上有 1 个运行目录还没有的提交')
    assert repo.head == before
    assert (repo.work / TAGS_FILE).read_bytes() == b'{}\n'           # 被拒绝时标签文件一个字都没写


def test_runner_and_branch_come_from_fork_updater(root, monkeypatch):
    """每次请求都按部署配置现取（同 ForkUpdater()）：仓库是当前目录。这里把部署配置换掉，不构造 DeployConfig。"""
    monkeypatch.setattr(fork_updater, '_deploy_settings', lambda: ('git', 'custom'))

    runner, branch = REAL_RUNNER_AND_BRANCH()

    assert (runner.git, Path(runner.repo).resolve(), branch) == ('git', root.resolve(), 'custom')
    assert not (root / 'config').exists()


# ------------------------------------------------------------------------------------------------------------------
# 按画面体检
# ------------------------------------------------------------------------------------------------------------------

class FakeAnnotator:
    """标注工具里体检用到的两个方法：get_image_file 核对会话和截图，test_rule 给单条打分（a 匹配上，别的都没有）。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.session_error: Exception | None = None

    def get_image_file(self, session_id, image_id):
        self.calls.append(('get_image_file', session_id, image_id))
        if self.session_error is not None:
            raise self.session_error
        return Path('capture.png')

    def test_rule(self, session_id, image_id, task_name, json_relpath, rule_type, rule, list_meta=None):
        self.calls.append(('test_rule', session_id, image_id, task_name, json_relpath, rule_type, rule['itemName']))
        matched = rule['itemName'] == 'a'
        return {'matched': matched, 'similarity': 0.91 if matched else 0.12}


@pytest.fixture
def annotator(monkeypatch) -> FakeAnnotator:
    fake = FakeAnnotator()
    monkeypatch.setattr(annotator_tool.annotator_manager, 'get_image_file', fake.get_image_file)
    monkeypatch.setattr(annotator_tool.annotator_manager, 'test_rule', fake.test_rule)
    return fake


@pytest.fixture
def tagged_foo(foo) -> Path:
    """foo 加上标签：a 和 c 在"战斗中"，b 在"退出时"。"""
    put_json(foo, TAGS_FILE, {'Foo': {'as/image.json': {'a': {'periodic': False, 'screen': '战斗中'},
                                                        'b': {'periodic': False, 'screen': '退出时'}},
                                      'as/ocr.json': {'c': {'periodic': True, 'screen': '战斗中'}}}})
    return foo


def screen_body(screen: str) -> ScreenCheckBody:
    return ScreenCheckBody(session_id='s1', image_id='i1', screen=screen)


def test_screen_check_scores_with_the_annotator(tagged_foo, annotator):
    result = post_screen_check('Foo', screen_body('战斗中'))

    assert result == {'hint': '', 'results': [
        {'file': 'as/image.json', 'item': 'a', 'matched': True, 'similarity': 0.91, 'threshold': 0.8, 'expected': True},
        {'file': 'as/image.json', 'item': 'b', 'matched': False, 'similarity': 0.12, 'threshold': 0.8, 'expected': False},
        {'file': 'as/ocr.json', 'item': 'c', 'matched': False, 'similarity': 0.12, 'threshold': None, 'expected': True},
    ]}
    assert annotator.calls[0] == ('get_image_file', 's1', 'i1')                     # 先核对会话和截图
    assert annotator.calls[1] == ('test_rule', 's1', 'i1', 'Foo', 'as/image.json', 'image', 'a')


def test_screen_without_items_returns_empty_results_and_a_hint(tagged_foo, annotator):
    """所选画面上没有任何条目：返回空结果和提示，不报错，也一条都不打分。"""
    result = post_screen_check('Foo', screen_body('活动主页'))

    assert result == {'results': [], 'hint': EMPTY_SCREEN_HINT}
    assert [call[0] for call in annotator.calls] == ['get_image_file']


def test_expired_session_is_one_error_not_one_per_row(tagged_foo, annotator):
    annotator.session_error = annotator_tool.AnnotatorError('invalid_session', '会话不存在: s1', 404)

    error = http_error(lambda: post_screen_check('Foo', screen_body('战斗中')))

    assert (error.status_code, error.detail) == (404, {'code': 'invalid_session', 'message': '会话不存在: s1'})
    assert [call[0] for call in annotator.calls] == ['get_image_file']


# ------------------------------------------------------------------------------------------------------------------
# 替换图片
# ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('body', [
    replace_body(image_name='../x.png'),                                # 不是这个文件里任何条目的 imageName
    replace_body(json_relpath='as/nope.json'),                          # 任务里没有这个素材文件
    replace_body(json_relpath='as/ocr.json', image_name='c.png'),      # OCR 条目没有图片
    replace_body(content='不是 base64'),
    replace_body(content=b64(b'not an image')),
], ids=['traversal', 'unknown-json', 'no-image-entry', 'bad-base64', 'not-an-image'])
def test_replace_endpoint_validates(repo, body):
    before = files_under(repo.work)

    error = http_error(lambda: post_replace_image('Foo', body))

    assert error.status_code == 400
    assert error.detail['code'] == 'invalid_request' and error.detail['message']
    assert files_under(repo.work) == before                         # .git 也没动：校验在碰 git 之前


def test_replace_image_commits_with_the_resolved_runner_and_branch(repo, monkeypatch):
    """上传替换一步做完：换图、重新生成 assets.py、提交并推送，用的是现取的运行目录 git 和 fork 分支。"""
    calls, real = [], assets_check.replace_and_commit

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(assets_check, 'replace_and_commit', spy)
    content = 'data:image/png;base64,' + b64(NEW_PNG)

    result = post_replace_image('Foo', replace_body(content=content))

    assert calls == [(('Foo', 'as/image.json', 'a.png', content, repo.runner, 'custom'), {})]
    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': True, 'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head and repo.files_of(repo.head) == {'tasks/Foo/as/a.png'}
    assert repo.message(repo.head) == '素材维护：替换 tasks/Foo/as/a.png'
    assert np.array_equal(pixels(repo.work / 'tasks/Foo/as/a.png'), cv2.imdecode(np.frombuffer(NEW_PNG, np.uint8),
                                                                               cv2.IMREAD_UNCHANGED))
    assert repo.status() == []


def test_replace_endpoint_regenerates_assets_py(repo):
    """默认的重新生成按当前目录（仓库根）跑：删掉的 assets.py 重新生成出来，和提交里的一字不差，所以工作区是干净的。"""
    (repo.work / 'tasks/Foo/assets.py').unlink()

    result = post_replace_image('Foo', replace_body())

    assert result['pushed'] is True and repo.files_of(result['commit']) == {'tasks/Foo/as/a.png'}
    assert (repo.work / 'tasks/Foo/assets.py').is_file() and repo.status() == []


def test_busy_replace_comes_back_in_the_result(repo):
    """正在合并或保存时不替换：和提交类接口一样以 200 返回，原因在 error 里，什么都没写。"""
    before = files_under(repo.work)

    with lock_held():
        result = post_replace_image('Foo', replace_body())

    assert result == {'target': 'tasks/Foo/as/a.png', 'replaced': False, 'commit': None, 'pushed': False,
                      'error': '正在合并上游或保存素材，请稍后再试'}
    assert files_under(repo.work) == before


def test_write_refused_maps_to_409():
    with pytest.raises(HTTPException) as caught:
        with assets_check_router._mapped_errors('测试'):
            raise assets_check.WriteRefused('正在合并上游或保存素材，请稍后再试')
    assert (caught.value.status_code, caught.value.detail) == (409, {'code': 'write_refused',
                                                                     'message': '正在合并上游或保存素材，请稍后再试'})


def test_other_errors_are_500_with_the_reason(foo, monkeypatch, log):
    """WriteRefused 以外的 RuntimeError 不算"忙"：回 500，消息是异常的说明，调用栈记进日志。"""
    def boom(*args, **kwargs):
        raise RuntimeError('重新生成 assets.py 需要在仓库根目录下进行')

    monkeypatch.setattr(assets_check_router, '_runner_and_branch', lambda: (GitRunner('git', foo), 'custom'))
    monkeypatch.setattr(assets_check, 'replace_and_commit', boom)

    error = http_error(lambda: post_replace_image('Foo', replace_body()))

    assert (error.status_code, error.detail) == (500, {'code': 'internal_error',
                                                       'message': '重新生成 assets.py 需要在仓库根目录下进行'})
    assert len(log.messages('exception')) == 1


@pytest.mark.parametrize('call', [
    lambda: get_rules('NoSuch'),
    lambda: get_static_check('NoSuch'),
    lambda: post_screen_check('NoSuch', screen_body('战斗中')),
    lambda: post_replace_image('NoSuch', replace_body()),
    lambda: get_export('NoSuch', commit='HEAD'),
], ids=['rules', 'static-check', 'screen-check', 'replace-image', 'export'])
def test_unknown_task_is_400(foo, annotator, monkeypatch, call):
    monkeypatch.setattr(assets_check_router, '_runner_and_branch', lambda: (GitRunner('git', foo), 'custom'))
    error = http_error(call)
    assert (error.status_code, error.detail) == (400, {'code': 'invalid_request', 'message': "未知任务：'NoSuch'"})


def test_unknown_task_in_commit_push_is_400(repo):
    error = http_error(lambda: post_commit_push('NoSuch', CommitPushBody(message='')))
    assert (error.status_code, error.detail['code']) == (400, 'invalid_request')


def test_corrupt_tags_file_is_400(foo):
    put_bytes(foo, TAGS_FILE, b'{')
    for call in (get_tags, lambda: get_rules('Foo')):
        error = http_error(call)
        assert error.status_code == 400 and error.detail['message'].startswith('标签文件损坏')


# ------------------------------------------------------------------------------------------------------------------
# 提交并推送、导出补丁包
# ------------------------------------------------------------------------------------------------------------------

def test_commit_push_endpoint_commits_and_pushes(repo):
    """提交并推送收的是别处留下的改动（比如标注工具改的图）：上传替换自己已经提交过了。"""
    put_bytes(repo.work, 'tasks/Foo/as/b.png', NEW_PNG)

    result = post_commit_push('Foo', CommitPushBody(message='补上 b 的图'))

    assert result == {'commit': repo.head, 'pushed': True, 'error': ''}
    assert repo.fork_tip == repo.head
    assert repo.message(repo.head) == '补上 b 的图'
    assert repo.files_of(repo.head) == {'tasks/Foo/as/b.png'}


def test_commit_push_refusal_comes_back_in_the_result(repo):
    repo.rival_commit()
    put_bytes(repo.work, 'tasks/Foo/as/b.png', NEW_PNG)

    result = post_commit_push('Foo', CommitPushBody())

    assert result['commit'] is None and result['pushed'] is False
    assert result['error'].startswith('fork 上有 1 个运行目录还没有的提交')


@pytest.mark.parametrize('commit', ['', '   '])
def test_export_needs_a_commit(foo, commit):
    error = http_error(lambda: get_export('Foo', commit=commit))
    assert (error.status_code, error.detail) == (400, {'code': 'missing_commit', 'message': '先提交，再导出补丁包'})


def test_export_sends_the_zip_and_removes_it_afterwards(repo):
    commit = post_replace_image('Foo', replace_body())['commit']

    response = get_export('Foo', commit=commit)

    assert isinstance(response, FileResponse)
    patch = Path(response.path)
    assert patch.name == f'assets-patch-{commit[:7]}.zip'
    assert response.media_type == 'application/zip'
    assert response.headers['content-disposition'] == f'attachment; filename="{patch.name}"'
    with zipfile.ZipFile(patch) as archive:
        assert [name for name in archive.namelist() if not name.endswith('/')] == ['tasks/Foo/as/a.png']
        assert archive.read('tasks/Foo/as/a.png') == (repo.work / 'tasks/Foo/as/a.png').read_bytes()
    asyncio.run(response.background())                                   # 发完之后的清理
    assert not patch.parent.exists()


def test_failed_export_is_400_and_leaves_nothing_behind(repo, monkeypatch, tmp_path):
    made, real = [], tempfile.mkdtemp
    scratch = tmp_path / 'scratch'
    scratch.mkdir()

    def recording(*args, **kwargs):
        made.append(Path(real(*args, **{**kwargs, 'dir': scratch})))
        return str(made[-1])

    monkeypatch.setattr(tempfile, 'mkdtemp', recording)

    error = http_error(lambda: get_export('Foo', commit='0' * 40))

    assert (error.status_code, error.detail['code']) == (400, 'invalid_request')
    assert '仓库里没有这个提交' in error.detail['message']
    assert len(made) <= 1 and not any(path.exists() for path in made)


# ------------------------------------------------------------------------------------------------------------------
# 保存类接口只接受同源请求
# ------------------------------------------------------------------------------------------------------------------

HOST = '127.0.0.1:22267'
CROSS_ORIGIN = {'detail': {'code': 'cross_origin', 'message': '只接受素材维护页发出的保存请求'}}
WRITE_ENDPOINTS = [
    ('PUT', '/custom/assets/tags', {'Foo': {}}, 'commit_tags'),
    ('POST', '/custom/assets/Foo/replace-image', {'json_relpath': 'as/image.json', 'image_name': 'a.png',
                                                  'content_base64': 'x'}, 'replace_and_commit'),
    ('POST', '/custom/assets/Foo/commit-push', {'message': ''}, 'commit_and_push'),
]
WRITE_IDS = ['tags', 'replace-image', 'commit-push']


@pytest.fixture
def recorded_writes(monkeypatch) -> list[str]:
    """三个保存类接口背后的业务函数和 _runner_and_branch 都换成记录器：只记下被调了哪个，不碰 git 和磁盘。"""
    calls = []

    def runner_and_branch():
        calls.append('_runner_and_branch')
        return GitRunner('git', Path('.')), 'custom'

    def recorder(name):
        def record(*args, **kwargs):
            calls.append(name)
            return {'commit': None, 'pushed': False, 'error': ''}
        return record

    monkeypatch.setattr(assets_check_router, '_runner_and_branch', runner_and_branch)
    for name in ('commit_tags', 'replace_and_commit', 'commit_and_push'):
        monkeypatch.setattr(assets_check, name, recorder(name))
    return calls


def send_write(app, method: str, path: str, body: dict, origin: str | None) -> tuple[int, dict]:
    headers = {'host': HOST, 'content-type': 'application/json'}
    if origin is not None:
        headers['origin'] = origin
    status, _, payload = http_request(app, method, path, body=json.dumps(body).encode('utf-8'), headers=headers)
    return status, json.loads(payload)


@pytest.mark.parametrize('method, path, body, business', WRITE_ENDPOINTS, ids=WRITE_IDS)
@pytest.mark.parametrize('origin', [None, f'http://{HOST}'], ids=['no-origin', 'same-origin'])
def test_writes_from_the_page_or_without_origin_are_accepted(app, recorded_writes, method, path, body, business,
                                                             origin):
    """本页（同源）的请求带着 Origin，OASX、curl 不带：都放行。"""
    status, payload = send_write(app, method, path, body, origin)
    assert (status, payload) == (200, {'commit': None, 'pushed': False, 'error': ''})
    assert recorded_writes == ['_runner_and_branch', business]


@pytest.mark.parametrize('method, path, body, business', WRITE_ENDPOINTS, ids=WRITE_IDS)
@pytest.mark.parametrize('origin', ['http://evil.example', 'http://127.0.0.1:8080', 'null'],
                         ids=['other-host', 'other-port', 'opaque'])
def test_cross_origin_writes_are_refused_before_anything_runs(app, recorded_writes, method, path, body, business,
                                                              origin):
    """别的网页借浏览器发来的保存请求（Origin 的 host[:port] 与 Host 不同）：403，一个业务函数都不调。"""
    assert send_write(app, method, path, body, origin) == (403, CROSS_ORIGIN)
    assert recorded_writes == []


def test_reads_do_not_check_the_origin(app, root):
    status, _, payload = http_request(app, 'GET', '/custom/assets/tags',
                                      headers={'host': HOST, 'origin': 'http://evil.example'})
    assert (status, json.loads(payload)) == (200, {})


# ------------------------------------------------------------------------------------------------------------------
# 页面、路由注册与静态目录
# ------------------------------------------------------------------------------------------------------------------

def test_page_handler_returns_the_index_without_cache():
    response = asyncio.run(assets_check_page())
    assert Path(response.path).resolve() == (WEB_DIR / 'index.html').resolve()
    assert response.headers['cache-control'] == 'no-cache'


def test_register_custom_mounts_the_assets_routes(app):
    routes = {(route.path, method) for route in app.routes if isinstance(route, APIRoute)
              and route.path.startswith(('/custom/assets', '/tool/assets-check')) for method in route.methods}
    assert routes == {
        ('/custom/assets/tasks', 'GET'),
        ('/custom/assets/{task}/rules', 'GET'),
        ('/custom/assets/{task}/static-check', 'GET'),
        ('/custom/assets/tags', 'GET'),
        ('/custom/assets/tags', 'PUT'),
        ('/custom/assets/{task}/screen-check', 'POST'),
        ('/custom/assets/{task}/replace-image', 'POST'),
        ('/custom/assets/{task}/commit-push', 'POST'),
        ('/custom/assets/{task}/export', 'GET'),
        ('/tool/assets-check', 'GET'),
    }
    mounts = [route for route in app.routes if isinstance(route, Mount)]
    assert [route.path for route in mounts] == ['/custom/assets/static']
    assert isinstance(mounts[0].app, NoCacheStaticFiles)
    assert Path(mounts[0].app.directory).resolve() == WEB_DIR.resolve()


def test_api_handlers_run_off_the_event_loop():
    """业务接口都是普通 def：FastAPI 把它们放进线程池跑，git、cv2、读写文件不卡事件循环。"""
    assert assets_api.routes
    for route in assets_api.routes:
        assert not asyncio.iscoroutinefunction(route.endpoint), route.path


def test_page_and_static_files_are_served_without_cache(app):
    status, headers, body = http_get(app, '/tool/assets-check')
    assert (status, headers['cache-control']) == (200, 'no-cache')
    assert headers['content-type'].startswith('text/html')
    assert b'/custom/assets/static/app.js' in body

    for name in ('app.js', 'style.css', 'index.html'):
        status, headers, body = http_get(app, f'/custom/assets/static/{name}')
        assert (status, headers['cache-control']) == (200, 'no-cache'), name
        assert body == (WEB_DIR / name).read_bytes()


def test_query_flag_and_error_body_over_http(app, foo):
    for name in ('MetaDemon', 'ActivityShikigami'):
        (foo / 'tasks' / name).mkdir()

    status, _, body = http_get(app, '/custom/assets/tasks', 'all=1')
    assert (status, json.loads(body)) == (200, {'tasks': ['ActivityShikigami', 'Foo', 'MetaDemon']})
    status, _, body = http_get(app, '/custom/assets/tasks', 'all=0')
    assert (status, json.loads(body)) == (200, {'tasks': ['ActivityShikigami', 'MetaDemon']})

    status, _, body = http_get(app, '/custom/assets/NoSuch/rules')
    assert (status, json.loads(body)) == (400, {'detail': {'code': 'invalid_request', 'message': "未知任务：'NoSuch'"}})


def test_rules_table_shows_method_and_description():
    """清单按规格 §9 列出方法和描述；app.js 里的列数（占位行、空文件行要铺满整行）与表头一致。"""
    html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
    head = re.search(r'<thead>\s*<tr>(.*?)</tr>\s*</thead>\s*<tbody id="rulesBody">', html, re.S).group(1)
    headers = re.findall(r'<th>([^<]*)</th>', head)
    assert headers == ['缩略图', '条目', '类型', '文件', '每期必换', '画面', 'ROI', '阈值', '方法', '描述', '替换']
    columns = re.search(r'const RULE_COLUMNS = (\d+);', (WEB_DIR / 'app.js').read_text(encoding='utf-8'))
    assert columns and int(columns.group(1)) == len(headers)


def test_page_references_resolve_to_local_files():
    """页面只引用本服务上的文件（标注工具的 base.css 和本页的静态目录），不用外部 CDN，也不需要构建。"""
    html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
    references = re.findall(r'(?:href|src)="([^"]+)"', html)
    assert '/tool/annotator/static/css/base.css' in references
    served = {'/tool/annotator/static/': ANNOTATOR_STATIC, '/custom/assets/static/': WEB_DIR}
    for reference in references:
        prefix = next((prefix for prefix in served if reference.startswith(prefix)), None)
        assert prefix is not None, reference
        assert (served[prefix] / reference[len(prefix):]).is_file(), reference
    for name in ('index.html', 'app.js', 'style.css'):
        text = (WEB_DIR / name).read_text(encoding='utf-8')
        assert 'http://' not in text and 'https://' not in text, name
