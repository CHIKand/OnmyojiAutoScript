"""主进程这边的飞书功能：找群接口（module.server.feishu_router 的 GET /custom/feishu/chats）、测试推送附的测试图、
每日定时合并失败时的飞书通知（feishu_app.notify_server_event，由 custom_jobs 调用）、补发任务的登记，以及 .env.example
里取 open_id、chat_id 的步骤。

不联网：请求全部走注入的 FakeSession，它按 URL 路径返回预设的响应，并记下每个请求；接口用的会话（feishu_router._session）
默认换成什么都没预设的假会话，用例忘了指定也只会失败，不会真去联网。处理函数直接调，不起 FastAPI 的 TestClient（它依赖的
httpx 没装）。文件只写 tmp_path，它同时是当前目录（.env 也从这里读）。凭证默认用环境变量给假值。
项目 logger（logging.getLogger('oas')）的 handler 换成空列表，记录经 caplog 读；日志文件指到 tmp_path 里一个不存在的文件，
测试推送不会读到仓库真实日志的末尾。
不导入 module.server.home_router（它会带进 main_manager，一导入就扫 config/、起线程）：测试推送照 /home/notify_test 的写法
直接用 Notifier(setting, True).push(...)，那个处理函数的写法另用 ast 核对。
"""
import ast
import asyncio
import copy
import inspect
import io
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from fastapi import FastAPI
from fastapi.routing import APIRoute
from PIL import Image
from rich.console import Console
from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from module.config.dotenv import read_env
from module.logger import logger
from module.notify import feishu_app
from module.notify.feishu_app import FeishuClient, FeishuError, flush_outbox, notify_server_event
from module.notify.notify import Notifier
from module.server import custom_jobs, feishu_router, fork_updater
from module.server.custom_routes import register_custom
from module.server.feishu_router import get_chats
from module.server.fork_git import MergeResult

# 脱敏模块 module/handler/sensitive_info.py 的源码里有无效转义：测试推送拼日志尾时才导入它，编译时发的警告与本模块无关
pytestmark = pytest.mark.filterwarnings('ignore:invalid escape sequence:DeprecationWarning')

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = json.loads((REPO_ROOT / 'config' / 'template.json').read_text(encoding='utf-8'))
TOKEN = '/open-apis/auth/v3/tenant_access_token/internal'
IMAGES = '/open-apis/im/v1/images'
MESSAGES = '/open-apis/im/v1/messages'
CHATS = '/open-apis/im/v1/chats'
SECRET = 'S3CRET-fake-app-secret'
TOKEN_VALUE = 't-g1047cFAKEtoken0001'                              # 足够独特，在日志里搜不会误中
SENT = {'code': 0, 'msg': 'success', 'data': {'message_id': 'om_1'}}
UPLOADED = {'code': 0, 'msg': 'success', 'data': {'image_key': 'img_v2_abc'}}
CHATS_REPLY = {'code': 0, 'msg': 'success',
               'data': {'items': [{'chat_id': 'oc_1', 'name': '群一', 'owner_id': 'ou_owner'},
                                  {'chat_id': 'oc_2', 'name': '群二'}], 'has_more': False}}
NO_SCOPE = {'code': 99991672, 'msg': 'Access denied. One of the following scopes is required: [im:chat:readonly]'}
FEISHU_YAML = 'provider: feishu_app\nreceive_id_type: open_id\nreceive_id: ou_x\n'
MERGE_TITLE = 'OAS 合并上游'
MERGE_MESSAGE = '合并上游遇到代码冲突，已中止，分支未改动：module/a.py'
MERGE_TIME = datetime(2026, 11, 17, 4, 31)
BOOM = RuntimeError('boom')


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return copy.deepcopy(self._body)


class FakeSession:
    """按 URL 路径返回预设的响应，并记下每个请求。

    on(path, *replies)：replies 依次用掉，最后一个一直重复；reply 是 dict（HTTP 200 的 JSON）、FakeResponse
    或异常实例（调用时抛出）。没预设的路径抛 KeyError，让测试失败。
    """

    def __init__(self):
        self.replies: dict[str, list] = {}
        self.calls: list[dict] = []

    def on(self, path: str, *replies) -> 'FakeSession':
        self.replies[path] = list(replies)
        return self

    def _handle(self, method: str, url: str, **kwargs):
        path = urlsplit(url).path
        self.calls.append({'method': method, 'path': path, **kwargs})
        queue = self.replies[path]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply.with_traceback(None)            # 同一个异常对象会抛多次：每次从头记 traceback
        return reply if isinstance(reply, FakeResponse) else FakeResponse(200, reply)

    def post(self, url, **kwargs):
        return self._handle('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self._handle('GET', url, **kwargs)

    def to(self, path: str) -> list[dict]:
        return [call for call in self.calls if call['path'] == path]


def token_reply(token: str = TOKEN_VALUE) -> dict:
    return {'code': 0, 'msg': 'ok', 'tenant_access_token': token, 'expire': 7200}


def ok_session() -> FakeSession:
    return FakeSession().on(TOKEN, token_reply()).on(IMAGES, UPLOADED).on(MESSAGES, SENT)


def down_session() -> FakeSession:
    return FakeSession().on(TOKEN, requests.ConnectionError('offline'))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_fake')
    monkeypatch.setenv('FEISHU_APP_SECRET', SECRET)
    monkeypatch.delenv('OAS_UPSTREAM_MERGE_TIME', raising=False)
    monkeypatch.setattr(logger, 'handlers', [])
    monkeypatch.setattr(logger, 'log_file', str(tmp_path / 'log' / 'server.txt'), raising=False)
    for name, value in (('_screenshot_provider', None), ('_instance_name', None), ('_flush_quiet', set()),
                        ('_flush_missing_logged', False)):
        monkeypatch.setattr(feishu_app, name, value)
    monkeypatch.setattr(feishu_router, '_session', lambda: FakeSession())


def no_credentials(monkeypatch, dotenv: str = '') -> None:
    monkeypatch.delenv('FEISHU_APP_ID')
    monkeypatch.delenv('FEISHU_APP_SECRET')
    Path('.env').write_text(dotenv, encoding='utf-8')


def use_session(monkeypatch, session: FakeSession) -> FakeSession:
    """找群接口改用这个假会话。"""
    monkeypatch.setattr(feishu_router, '_session', lambda: session)
    return session


def message(session: FakeSession, index: int = -1) -> dict:
    """第 index 条发出的消息的 zh_cn 层：title 和 content（段落数组）。"""
    return json.loads(session.to(MESSAGES)[index]['json']['content'])['zh_cn']


def texts(post: dict) -> list[str]:
    return [node['text'] for paragraph in post['content'] for node in paragraph if node['tag'] == 'text']


def images(post: dict) -> list[str]:
    return [node['image_key'] for paragraph in post['content'] for node in paragraph if node['tag'] == 'img']


def receivers(session: FakeSession) -> list[tuple[str, str]]:
    return [(call['params']['receive_id_type'], call['json']['receive_id']) for call in session.to(MESSAGES)]


def logged(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == 'oas' and r.levelno == level]


def entries(root: Path) -> list[Path]:
    return sorted((root / 'log' / 'feishu_outbox').glob('*.json'))


def feishu_yaml(id_type: str, receive_id: str) -> str:
    return f'provider: feishu_app\nreceive_id_type: {id_type}\nreceive_id: "{receive_id}"\n'


def write_instance(root: Path, name: str, *, enable: bool = True, notify_config: str = '', text: str | None = None):
    """config/<name>：仓库模板的副本，只改 script.error 的 notify_enable 和 notify_config；给了 text 就原样写它。"""
    path = root / 'config' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        data = copy.deepcopy(TEMPLATE)
        data['script']['error'].update(notify_enable=enable, notify_config=notify_config)
        text = json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding='utf-8')


def rich_log(monkeypatch) -> io.StringIO:
    """照 module/logger.py 的文件 handler 配一个 RichHandler：rich traceback 带每一帧的局部变量。
    控制台放宽到 500 列，长行不折断，搜字符串不会漏。返回它写入的流。"""
    stream = io.StringIO()
    handler = RichHandler(console=Console(file=stream, no_color=True, highlight=False, width=500), show_path=False,
                          show_time=False, show_level=False, rich_tracebacks=True, tracebacks_show_locals=True,
                          tracebacks_extra_lines=3, tracebacks_width=500, highlighter=NullHighlighter())
    monkeypatch.setattr(logger, 'handlers', [handler])
    return stream


# ---- 找群接口 GET /custom/feishu/chats ------------------------------------------------------------------------

def test_chats_endpoint_uses_env_only(tmp_path, monkeypatch, caplog):
    no_credentials(monkeypatch, f'FEISHU_APP_ID=cli_from_dotenv\nFEISHU_APP_SECRET={SECRET}\n')
    session = use_session(monkeypatch, FakeSession().on(TOKEN, token_reply()).on(CHATS, CHATS_REPLY))
    assert not inspect.signature(get_chats).parameters                 # 请求里带不进任何参数：凭证只能来自 .env
    response = get_chats()
    assert response == {'ok': True, 'chats': [{'chat_id': 'oc_1', 'name': '群一'}, {'chat_id': 'oc_2', 'name': '群二'}],
                        'error': ''}
    assert session.to(TOKEN)[0]['json'] == {'app_id': 'cli_from_dotenv', 'app_secret': SECRET}   # 用的是 .env 里的凭证
    assert session.to(CHATS)[0]['headers'] == {'Authorization': f'Bearer {TOKEN_VALUE}'}
    body = json.dumps(response, ensure_ascii=False)
    assert SECRET not in body and TOKEN_VALUE not in body
    assert SECRET not in caplog.text and TOKEN_VALUE not in caplog.text


@pytest.mark.parametrize('dotenv', [
    '',
    'FEISHU_APP_ID=cli_fake\n',
    f'FEISHU_APP_SECRET={SECRET}\n',
    'FEISHU_APP_ID=\nFEISHU_APP_SECRET=\n',
], ids=['nothing', 'no-secret', 'no-app-id', 'empty-values'])
def test_chats_endpoint_without_credentials_names_the_two_keys(monkeypatch, caplog, dotenv):
    no_credentials(monkeypatch, dotenv)
    session = use_session(monkeypatch, FakeSession())
    response = get_chats()
    assert response['ok'] is False and response['chats'] == []
    assert 'FEISHU_APP_ID' in response['error'] and 'FEISHU_APP_SECRET' in response['error']
    assert session.calls == []
    assert SECRET not in json.dumps(response) and SECRET not in caplog.text


@pytest.mark.parametrize('token, chats', [
    ({'code': 10014, 'msg': 'app id not exists'}, CHATS_REPLY),
    (token_reply(), FakeResponse(400, NO_SCOPE)),
    (requests.ConnectionError('offline'), CHATS_REPLY),
], ids=['credentials', 'no-scope', 'network'])
def test_chats_endpoint_reports_a_feishu_error_as_its_text(monkeypatch, caplog, token, chats):
    use_session(monkeypatch, FakeSession().on(TOKEN, token).on(CHATS, chats))
    response = get_chats()
    with pytest.raises(FeishuError) as info:                             # 同样的应答，客户端抛的就是这个错
        FeishuClient('cli_fake', 'x', session=FakeSession().on(TOKEN, token).on(CHATS, chats)).list_chats()
    assert response == {'ok': False, 'chats': [], 'error': str(info.value)}
    assert SECRET not in json.dumps(response) and SECRET not in caplog.text
    assert not [r for r in caplog.records if r.exc_info]


def test_chats_endpoint_reports_an_unexpected_error_by_type_and_message(monkeypatch, caplog):
    use_session(monkeypatch, FakeSession().on(TOKEN, token_reply()).on(CHATS, BOOM))
    assert get_chats() == {'ok': False, 'chats': [], 'error': 'RuntimeError: boom'}
    error, = logged(caplog, logging.ERROR)
    assert 'RuntimeError: boom' in error
    assert not [r for r in caplog.records if r.exc_info]                # 没带 traceback（exc_info）


def test_chats_endpoint_is_a_plain_def_under_custom_feishu():
    app = FastAPI()
    register_custom(app)
    route, = [r for r in app.routes if isinstance(r, APIRoute) and r.path == '/custom/feishu/chats']
    assert route.methods == {'GET'} and route.endpoint is get_chats
    assert not inspect.iscoroutinefunction(get_chats)                   # 普通 def：FastAPI 放进线程池跑，联网不卡事件循环


# ---- 测试推送带图 ---------------------------------------------------------------------------------------------

def test_test_push_attaches_image(tmp_path):
    notifier = Notifier(FEISHU_YAML, True)          # /home/notify_test 的写法：不设 config_name；主进程也没登记截图来源
    session = ok_session()
    notifier.delegate.session = session
    assert notifier.push(title='测试', content='OAS 推送测试') is True
    assert [call['path'] for call in session.calls] == [TOKEN, IMAGES, MESSAGES]
    png = session.to(IMAGES)[0]['files']['image'][1]
    image = Image.open(io.BytesIO(png))
    assert (image.format, image.size) == ('PNG', (320, 80))
    assert len(png) < 4096                                              # 很小
    pixels = image.convert('RGB')
    assert pixels.getpixel((0, 0)) == pixels.getpixel((319, 79))        # 纯色底
    assert len(set(pixels.getdata())) > 1                               # 上面写了字
    post = message(session)
    assert post['title'] == '测试'
    assert texts(post) == ['OAS 推送测试']
    assert images(post) == ['img_v2_abc']
    assert notifier.push(title='测试', content='OAS 推送测试') is True   # 再点一次照样发（主进程不合并），图一模一样
    assert len(session.to(MESSAGES)) == 2
    assert session.to(IMAGES)[1]['files']['image'][1] == png
    assert not (tmp_path / 'log' / 'feishu_aggregate.json').exists()
    assert entries(tmp_path) == []


def test_the_notify_test_endpoint_still_pushes_through_notifier():
    # 上面的用例照这个写法走 /home/notify_test 的路径；home_router.py 不改（R14）
    tree = ast.parse((REPO_ROOT / 'module' / 'server' / 'home_router.py').read_text(encoding='utf-8'))
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'notify_test')
    source = ast.unparse(handler)
    assert 'Notifier(setting, True)' in source
    assert 'notifier.push(title=title, content=content)' in source


# ---- 每日定时合并失败的飞书通知 ---------------------------------------------------------------------------------

def test_merge_failure_goes_once_to_every_distinct_feishu_target(tmp_path, caplog):
    write_instance(tmp_path, 'a.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    write_instance(tmp_path, 'b.json', notify_config=feishu_yaml('open_id', ' ou_1 '))     # 同一个人：只发一次
    write_instance(tmp_path, 'c.json', notify_config=feishu_yaml('chat_id', 'oc_1'))
    write_instance(tmp_path, 'd.json', enable=False, notify_config=feishu_yaml('open_id', 'ou_disabled'))
    write_instance(tmp_path, 'e.json', notify_config='provider: bark\nkey: k')
    write_instance(tmp_path, 'template.json', notify_config=feishu_yaml('open_id', 'ou_template'))
    write_instance(tmp_path, 'findjade/f.json', notify_config=feishu_yaml('open_id', 'ou_subfolder'))  # 子目录不是实例
    session = ok_session()
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=session)
    assert receivers(session) == [('open_id', 'ou_1'), ('chat_id', 'oc_1')]
    for index in range(2):                                              # 一行文字，不附图
        assert message(session, index) == {'title': MERGE_TITLE, 'content': [[{'tag': 'text', 'text': MERGE_MESSAGE}]]}
    assert session.to(IMAGES) == []
    assert entries(tmp_path) == []
    assert not (tmp_path / 'log' / 'feishu_aggregate.json').exists()   # 不合并
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_unreadable_configs_and_bad_targets_are_skipped_with_a_warning(tmp_path, caplog):
    write_instance(tmp_path, 'a.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    write_instance(tmp_path, 'broken.json', text='{')
    write_instance(tmp_path, 'listed.json', text='[]')
    write_instance(tmp_path, 'typo.json',
                   notify_config='provider: feishu_app\nreceive_id_type: user_id\nreceive_id: u\n')
    session = ok_session()
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=session)
    assert receivers(session) == [('open_id', 'ou_1')]
    warnings = logged(caplog, logging.WARNING)
    assert len(warnings) == 3
    for name in ('broken', 'listed', 'typo'):
        assert len([line for line in warnings if name in line]) == 1, name


@pytest.mark.parametrize('enabled', [False, True], ids=['no-config-folder', 'nothing-uses-feishu'])
def test_no_feishu_target_sends_nothing(tmp_path, caplog, enabled):
    if enabled:
        write_instance(tmp_path, 'a.json', enable=False, notify_config=feishu_yaml('open_id', 'ou_1'))
        write_instance(tmp_path, 'b.json', notify_config='provider: null')
    session = FakeSession()
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=session)
    assert session.calls == []
    assert not (tmp_path / 'log').exists()
    line, = [r for r in caplog.records if r.name == 'oas']
    assert line.levelno == logging.INFO and MERGE_TITLE in line.getMessage()


def test_merge_failure_with_missing_credentials_writes_nothing(tmp_path, monkeypatch, caplog):
    no_credentials(monkeypatch)
    write_instance(tmp_path, 'a.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    session = FakeSession()
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=session)
    assert session.calls == []
    assert not (tmp_path / 'log').exists()
    error, = logged(caplog, logging.ERROR)
    assert '飞书凭证缺失' in error


def test_a_merge_failure_that_cannot_go_out_now_waits_for_the_next_flush(tmp_path):
    write_instance(tmp_path, 'a.json', notify_config=feishu_yaml('chat_id', 'oc_1'))
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=down_session())
    path, = entries(tmp_path)
    entry = json.loads(path.read_text(encoding='utf-8'))
    assert {key: entry[key] for key in ('receive_id_type', 'receive_id', 'title', 'lines', 'png_base64')} == {
        'receive_id_type': 'chat_id', 'receive_id': 'oc_1', 'title': MERGE_TITLE, 'lines': [MERGE_MESSAGE],
        'png_base64': None}
    session = ok_session()
    assert flush_outbox(root=tmp_path, session=session) == 1
    assert receivers(session) == [('chat_id', 'oc_1')]
    assert texts(message(session)) == [MERGE_MESSAGE]
    assert entries(tmp_path) == []


def test_the_immediate_send_is_a_flush_round(tmp_path):
    # 与补发同一条路径：发件箱里早先留下的条目先发，这条通知跟在后面
    write_instance(tmp_path, 'a.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    feishu_app.Outbox(tmp_path / 'log' / 'feishu_outbox').put(
        {'created_at': '2026-11-17T04:00:00', 'receive_id_type': 'open_id', 'receive_id': 'ou_earlier',
         'title': 'OAS1 t', 'lines': ['c'], 'png_base64': None})
    session = ok_session()
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=session)
    assert receivers(session) == [('open_id', 'ou_earlier'), ('open_id', 'ou_1')]
    assert entries(tmp_path) == []


# ---- custom_jobs 的接线：哪些合并结果发通知、补发任务什么时候登记 --------------------------------------------------

CONFLICT_HEADLINE = '合并上游有代码冲突，需人工处理'                      # 计划书 §6.3 的原话
RESTARTED = '已合并上游 3 个提交并推送到 fork；已重启实例：oas1'
RESTART_FAILED = '已合并上游 3 个提交并推送到 fork' + fork_updater.RESTART_FAILED_NOTE


def run_daily_merge(monkeypatch, status: str | None, *, message: str = MERGE_MESSAGE, conflicts: tuple = (),
                    attempts_before: int = 0) -> None:
    """跑一轮每日定时合并的检查，合并结果的状态为 status（None 表示合并没给出结果），文案为 message。
    attempts_before：今天已经试过几次（都是拉取失败，上一次在 RETRY_DELAY 之前），这一次就是第 attempts_before + 1 次。"""
    monkeypatch.setenv('OAS_UPSTREAM_MERGE_TIME', '04:30')
    result = None if status is None else MergeResult(status, 0, [], list(conflicts), '')
    monkeypatch.setattr(custom_jobs, '_merge_on_loop', lambda loop: (result, message))
    jobs = custom_jobs.CustomJobs(now=lambda: MERGE_TIME)
    if attempts_before:
        jobs._state = custom_jobs.MergeState(None, attempts_before, MERGE_TIME - custom_jobs.RETRY_DELAY)
        jobs._state_loaded = True
    jobs.tick(None)


@pytest.fixture
def merge_notices(monkeypatch) -> list[tuple]:
    """custom_jobs 调 notify_server_event 的参数，一次一项。"""
    sent = []
    monkeypatch.setattr(feishu_app, 'notify_server_event', lambda *args, **kwargs: sent.append((args, kwargs)))
    return sent


@pytest.mark.parametrize('status, message, attempts_before', [
    ('up_to_date', '已是最新，无需合并', 0),
    ('merged', RESTARTED, 0),
    ('pushed', '已把之前未推送的 1 个提交推送到 fork；已重启实例：无', 0),
    ('fetch_failed', MERGE_MESSAGE, 0),                                     # 第 1 次：10 分钟后重试
    ('fetch_failed', MERGE_MESSAGE, custom_jobs.MAX_ATTEMPTS - 2),           # 倒数第 2 次：还会重试
], ids=['up-to-date', 'merged', 'pushed', 'fetch-failed-first', 'fetch-failed-before-last'])
def test_a_daily_merge_that_needs_nobody_sends_nothing(monkeypatch, merge_notices, status, message, attempts_before):
    run_daily_merge(monkeypatch, status, message=message, attempts_before=attempts_before)
    assert merge_notices == []


@pytest.mark.parametrize('status, conflicts, message, attempts_before', [
    ('conflict_aborted', ('module/a.py',), MERGE_MESSAGE, 0),
    ('conflict_aborted', (), '合并未能开始，分支未改动：error: Your local changes would be overwritten', 0),
    ('push_failed', (), '已在本地合并上游 2 个提交，但推送失败：网络不通或超时。启动器守卫会保护本地提交，请检查网络或令牌后重试', 0),
    ('needs_install', (), '已合并上游 2 个提交并推送到 fork；上游新增依赖 foo，请在 OASX 重启服务（启动器会先安装依赖），实例暂未重启', 0),
    ('merged', (), RESTART_FAILED, 0),
    ('pushed', (), '已把之前未推送的 1 个提交推送到 fork' + fork_updater.RESTART_FAILED_NOTE, 0),
    ('fork_ahead', (), 'fork 上有运行目录还没有的 1 个新提交，已暂停合并', 0),
    ('diverged', (), '运行目录和 fork 各有对方没有的提交，已停止，需要人工处理', 0),
    ('dirty', (), '运行目录有未提交的改动，已跳过合并：M tasks/a.py', 0),
    ('smoke_failed', (), '合并上游后自检失败，已中止，分支未改动：ImportError: x', 0),
    ('error', (), '合并上游时出现意外错误，请查看服务日志：boom', 0),
    (None, (), '合并上游时出现意外错误，请查看服务日志：RuntimeError: no deploy.yaml', 0),
    ('fetch_failed', (), '拉取上游失败，未改动任何文件：fatal: unable to access', custom_jobs.MAX_ATTEMPTS - 1),
    ('a_status_fork_git_adds_later', (), '合并上游时出现意外错误，请查看服务日志：a_status_fork_git_adds_later', 0),
], ids=['conflict', 'conflict-not-started', 'push-failed', 'needs-install', 'merged-restart-failed',
        'pushed-restart-failed', 'fork-ahead', 'diverged', 'dirty', 'smoke-failed', 'error', 'no-result',
        'fetch-failed-last-attempt', 'unknown-status'])
def test_a_daily_merge_that_needs_a_person_notifies_and_says_so_first(monkeypatch, merge_notices, status, conflicts,
                                                                      message, attempts_before):
    run_daily_merge(monkeypatch, status, message=message, conflicts=conflicts, attempts_before=attempts_before)
    ((title, text), kwargs), = merge_notices
    assert (title, kwargs) == (MERGE_TITLE, {})
    headline, rest = text.split('\n', 1)
    assert headline.startswith('合并上游') and headline.endswith('，需人工处理')     # 第一行点明结果，说要人处理
    assert rest == message                                                  # 后面是更新页的同一句文案，一字不改
    if status == 'a_status_fork_git_adds_later':
        assert status in headline


def test_a_daily_merge_conflict_carries_the_spec_line(monkeypatch, merge_notices):
    run_daily_merge(monkeypatch, 'conflict_aborted', conflicts=('module/a.py',))
    assert merge_notices == [((MERGE_TITLE, f'{CONFLICT_HEADLINE}\n{MERGE_MESSAGE}'), {})]


def test_a_merge_that_never_started_is_not_called_a_code_conflict(monkeypatch, merge_notices):
    # 与 message_of 的规矩一致：conflict_aborted 却没有冲突文件，是合并根本没开始
    run_daily_merge(monkeypatch, 'conflict_aborted', message='合并未能开始，分支未改动：x')
    (_, text), _ = merge_notices[0]
    assert text.split('\n')[0] == '合并上游未能开始，需人工处理'


def test_a_daily_merge_conflict_reaches_feishu(tmp_path, monkeypatch):
    write_instance(tmp_path, 'oas1.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    session = ok_session()
    real = feishu_app.notify_server_event
    monkeypatch.setattr(feishu_app, 'notify_server_event', lambda title, text: real(title, text, session=session))
    run_daily_merge(monkeypatch, 'conflict_aborted', conflicts=('module/a.py',))
    assert receivers(session) == [('open_id', 'ou_1')]
    assert message(session) == {'title': MERGE_TITLE, 'content': [[{'tag': 'text', 'text': CONFLICT_HEADLINE}],
                                                                  [{'tag': 'text', 'text': MERGE_MESSAGE}]]}


@pytest.mark.parametrize('body', ['x' * 1_000, '御' * 1_000], ids=['ascii', 'cjk'])
def test_a_server_event_caps_each_line_and_the_whole_message(tmp_path, body):
    # 自检失败、意外错误的文案带着最多 20 行 stderr：每行最多 300 个字符，整条照 requests 的 json= 算不超过 28,000 字节
    write_instance(tmp_path, 'a.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    stderr = [f'{i:02d} {body}' for i in range(20)]
    text = '\n'.join([CONFLICT_HEADLINE, '合并上游后自检失败，已中止，分支未改动：' + body, *stderr])
    session = ok_session()
    notify_server_event(MERGE_TITLE, text, root=tmp_path, session=session)
    call, = session.to(MESSAGES)
    assert len(json.dumps(call['json']['content'])) <= 28_000
    lines = texts(message(session))
    assert lines[0] == CONFLICT_HEADLINE                                    # 第一行短，不截
    assert [line[:2] for line in lines[2:]] == [f'{i:02d}' for i in range(20)]   # 一行不删，只截长的
    assert all(0 < len(line) <= 300 for line in lines)


def test_the_update_button_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(feishu_app, 'notify_server_event', lambda *args, **kwargs: sent.append(args))

    async def conflict():
        return MergeResult('conflict_aborted', 0, [], ['module/a.py'], ''), MERGE_MESSAGE

    monkeypatch.setattr(fork_updater, 'run_update_result', conflict)
    assert asyncio.run(fork_updater.run_update()) == MERGE_MESSAGE     # OASX 更新页"执行更新"走的就是它
    assert sent == []


def test_the_flush_job_is_registered_when_the_jobs_start(monkeypatch):
    fresh = custom_jobs.CustomJobs()
    started = []
    monkeypatch.setattr(fresh, 'start', lambda loop=None: started.append((loop, dict(fresh._jobs))))
    monkeypatch.setattr(custom_jobs, '_default', fresh)
    custom_jobs.start_custom_jobs('the loop')
    (loop, jobs), = started                                             # 起线程的那一刻已经登记好了
    assert loop == 'the loop'
    assert list(jobs) == ['feishu_flush']
    assert jobs['feishu_flush'].fn is flush_outbox and jobs['feishu_flush'].interval == 60


def test_importing_the_server_modules_registers_no_job(tmp_path):
    """fork_updater 的导入自检会导入 custom_routes（进而导入 custom_jobs 和找群接口）：导入时不登记任务、不起线程。
    换个干净的进程验：本进程里别的用例可能已经动过 custom_jobs。"""
    code = '\n'.join([
        'import threading',
        'import module.server.custom_routes',
        'from module.server import custom_jobs',
        'assert custom_jobs._default._jobs == {}, sorted(custom_jobs._default._jobs)',
        'assert threading.active_count() == 1',
    ])
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)},
                            capture_output=True, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stderr[-2000:]


# ---- 新路径上的日志不带局部变量 ----------------------------------------------------------------------------------
# 项目日志的 handler 渲染 traceback 时带每一帧的局部变量。下面每条路径都在"密钥或 token 正在请求参数里"的那一刻抛意外异常，
# 照项目日志的方式渲染记下的日志：里面不能有 app secret，也不能有 token。

def via_chats(monkeypatch, tmp_path, session: FakeSession) -> str:
    use_session(monkeypatch, session)
    return json.dumps(get_chats(), ensure_ascii=False)


def via_server_event(monkeypatch, tmp_path, session: FakeSession) -> str:
    write_instance(tmp_path, 'oas1.json', notify_config=feishu_yaml('open_id', 'ou_1'))
    notify_server_event(MERGE_TITLE, MERGE_MESSAGE, root=tmp_path, session=session)
    return ''


def via_test_push(monkeypatch, tmp_path, session: FakeSession) -> str:
    notifier = Notifier(FEISHU_YAML, True)
    notifier.delegate.session = session
    return str(notifier.push(title='测试', content='OAS 推送测试'))


NEW_PATHS = {'chats': (via_chats, CHATS), 'server-event': (via_server_event, MESSAGES),
             'test-push': (via_test_push, IMAGES)}


@pytest.mark.parametrize('step', ['token', 'api'])
@pytest.mark.parametrize('path', list(NEW_PATHS))
def test_the_new_paths_log_no_secret_even_with_traceback_locals(tmp_path, monkeypatch, path, step):
    stream = rich_log(monkeypatch)
    run, api = NEW_PATHS[path]
    session = FakeSession().on(TOKEN, BOOM if step == 'token' else token_reply()).on(api, BOOM)
    response = run(monkeypatch, tmp_path, session)
    raised = session.calls[-1]                                          # 抛出的那一刻，密钥或 token 就在请求参数里
    if step == 'token':
        assert raised['json']['app_secret'] == SECRET
    else:
        assert raised['headers']['Authorization'] == f'Bearer {TOKEN_VALUE}'
    text = stream.getvalue()
    assert 'RuntimeError: boom' in text                                 # 这条日志确实渲染了
    assert SECRET not in text and TOKEN_VALUE not in text
    assert SECRET not in response and TOKEN_VALUE not in response


# ---- .env.example ------------------------------------------------------------------------------------------------

def test_env_example_explains_how_to_find_the_receive_id(monkeypatch):
    lines = (REPO_ROOT / '.env.example').read_text(encoding='utf-8').splitlines()
    start = next(index for index, line in enumerate(lines) if 'aggregate_minutes:' in line) + 1
    end = lines.index('FEISHU_APP_ID=')
    assert all(line.startswith('#') for line in lines[start:end])       # 注释都单独占一行
    comment = '\n'.join(lines[start:end])
    for words in ('https://open.feishu.cn/api-explorer', '发送消息', 'receive_id_type 设为 open_id', '快速复制 open_id',
                  '复制成员 ID', 'ou_', 'receive_id_type 写 open_id', '可用范围',                  # 个人的 open_id
                  '群机器人', '添加机器人', '/custom/feishu/chats', 'WebuiPort', 'oc_',
                  'receive_id_type 写 chat_id'):                                                    # 群的 chat_id
        assert words in comment, words
    monkeypatch.delenv('FEISHU_APP_ID')
    monkeypatch.delenv('FEISHU_APP_SECRET')
    assert read_env('FEISHU_APP_ID', env_file=REPO_ROOT / '.env.example') == ''      # 示例里的两个值仍是空的
    assert read_env('FEISHU_APP_SECRET', env_file=REPO_ROOT / '.env.example') == ''


def test_env_example_says_who_gets_the_daily_merge_message():
    lines = (REPO_ROOT / '.env.example').read_text(encoding='utf-8').splitlines()
    start = lines.index(f'OAS_UPSTREAM_BRANCH={fork_updater.DEFAULT_UPSTREAM_BRANCH}') + 1
    sample = next(index for index, line in enumerate(lines) if line.lstrip('# ').startswith('OAS_UPSTREAM_MERGE_TIME='))
    line, = [line for line in lines[start:sample] if '飞书' in line]       # 定时合并那一节里的一行注释
    assert line.startswith('#')
    for words in ('需要人处理', '各实例', '接收方'):
        assert words in line, words
