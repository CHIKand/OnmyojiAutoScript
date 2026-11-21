"""飞书通知器（module.notify.feishu_app 的 FeishuAppNotifier、flush_outbox、on_script_loop_start），
以及 notify.py 委托和 script.py 钩子的测试。

不联网：请求全部走注入的 FakeSession，它按 URL 路径返回预设的响应，并记下每个请求。
文件只写 tmp_path，它同时是当前目录（.env 也从这里读）。凭证默认用环境变量给假值。
项目 logger（logging.getLogger('oas')）的 handler 换成空列表：不写真日志文件，也不往控制台打，记录经 caplog 读。
"""
import ast
import copy
import io
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import numpy as np
import pytest
import requests
import yaml
from PIL import Image
from rich.console import Console
from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from module.logger import logger
from module.notify import feishu_app
from module.notify.feishu_app import (FeishuAppNotifier, flush_outbox, on_script_loop_start,
                                      register_screenshot_provider)
from module.notify.notify import Notifier
from module.orchestra.progress import Progress, progress_path, save_progress

# 被测代码用到时才导入两个上游模块，它们导入时发的警告与本模块无关：编排行要用 TASK_SPECS，会导入上游的配置模型
# （pydantic 弃用警告）；脱敏用的 module/handler/sensitive_info.py 源码里有无效转义（编译时的 DeprecationWarning）
pytestmark = [pytest.mark.filterwarnings('ignore::pydantic.warnings.PydanticDeprecatedSince20'),
              pytest.mark.filterwarnings('ignore:invalid escape sequence:DeprecationWarning')]

SCRIPT_PY = Path(__file__).resolve().parents[2] / 'script.py'
TOKEN = '/open-apis/auth/v3/tenant_access_token/internal'
IMAGES = '/open-apis/im/v1/images'
MESSAGES = '/open-apis/im/v1/messages'
SECRET = 'S3CRET-fake-app-secret'
SENT = {'code': 0, 'msg': 'success', 'data': {'message_id': 'om_1'}}
UPLOADED = {'code': 0, 'msg': 'success', 'data': {'image_key': 'img_v2_abc'}}
CFG = {'provider': 'feishu_app', 'receive_id_type': 'open_id', 'receive_id': 'ou_x'}
FEISHU_YAML = 'provider: feishu_app\nreceive_id_type: open_id\nreceive_id: ou_x\n'
ROW_LINE = '编排：第 3 行 肝帝·御灵之境'
T0 = datetime(2026, 11, 21, 10, 0)
FRAME = np.zeros((4, 6, 3), dtype=np.uint8)
FRAME[..., 0], FRAME[..., 1], FRAME[..., 2] = 200, 100, 50     # 三个通道各不相同：通道顺序弄反就对不上


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return copy.deepcopy(self._body)


class FakeSession:
    """按 URL 路径返回预设的响应，并记下每个请求。

    on(path, *replies)：replies 依次用掉，最后一个一直重复；reply 是 dict（HTTP 200 的 JSON）、FakeResponse
    或异常实例（调用时抛出）。没预设的路径抛 KeyError，让测试失败。on_request(path) 在每个请求开头调用。
    """

    def __init__(self):
        self.replies: dict[str, list] = {}
        self.calls: list[dict] = []
        self.on_request = None

    def on(self, path: str, *replies) -> 'FakeSession':
        self.replies[path] = list(replies)
        return self

    def _handle(self, method: str, url: str, **kwargs):
        path = urlsplit(url).path
        self.calls.append({'method': method, 'path': path, **kwargs})
        if self.on_request is not None:
            self.on_request(path)
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


def token_reply(token: str = 't-1') -> dict:
    return {'code': 0, 'msg': 'ok', 'tenant_access_token': token, 'expire': 7200}


def ok_session() -> FakeSession:
    return FakeSession().on(TOKEN, token_reply()).on(IMAGES, UPLOADED).on(MESSAGES, SENT)


def down_session() -> FakeSession:
    return FakeSession().on(TOKEN, requests.ConnectionError('offline'))


class Clock:
    """feishu_app._now 的替身：返回 now，测试直接改它。"""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_fake')
    monkeypatch.setenv('FEISHU_APP_SECRET', SECRET)
    monkeypatch.setattr(logger, 'handlers', [])
    monkeypatch.setattr(logger, 'log_file', str(tmp_path / 'log' / 'instance.txt'), raising=False)
    for name, value in (('_screenshot_provider', None), ('_instance_name', None), ('_flush_quiet', set()),
                        ('_flush_missing_logged', False)):
        monkeypatch.setattr(feishu_app, name, value)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock(T0)
    monkeypatch.setattr(feishu_app, '_now', fake)
    return fake


def no_credentials(monkeypatch) -> None:
    monkeypatch.delenv('FEISHU_APP_ID')
    monkeypatch.delenv('FEISHU_APP_SECRET')


def make_notifier(session, cfg: dict | None = None, name: str = 'OAS1') -> FeishuAppNotifier:
    notifier = FeishuAppNotifier(dict(CFG if cfg is None else cfg), True, root=Path.cwd(), session=session)
    notifier.config_name = name
    return notifier


class StandIn:
    """Script 的替身：on_script_loop_start 只用 config_name、config 和 __dict__ 里已建好的 device。"""

    def __init__(self, name: str, notify_enable: bool, notify_config: str):
        self.config_name = name
        error = SimpleNamespace(notify_enable=notify_enable, notify_config=notify_config)
        self.config = SimpleNamespace(model=SimpleNamespace(script=SimpleNamespace(error=error)))

    @property
    def device(self):
        raise AssertionError('不该触发 Script.device 的懒加载')


def start_instance(name: str = 'oas1', *, frame=None, notify_enable: bool = False,
                   notify_config: str = FEISHU_YAML, session: FakeSession | None = None) -> StandIn:
    """模拟实例进程的调度循环开始：登记截图来源和实例名；frame 给了就当作设备最新的一帧。"""
    script = StandIn(name, notify_enable, notify_config)
    if frame is not None:
        script.__dict__['device'] = SimpleNamespace(image=frame)
    on_script_loop_start(script, session=session if session is not None else FakeSession())
    return script


def row_running(name: str = 'oas1', row_index: int = 2, row_task: str = 'GoryouRealm', phase: str = 'row_running'):
    save_progress(progress_path(name, Path.cwd()), Progress(phase=phase, row_index=row_index, row_task=row_task))


def write_log(lines: list[str]) -> None:
    path = Path(logger.log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(f'{line}\n' for line in lines), encoding='utf-8')


def entries(root: Path) -> list[Path]:
    return sorted((root / 'log' / 'feishu_outbox').glob('*.json'))


def message(session: FakeSession, index: int = -1) -> dict:
    """第 index 条发出的消息的 zh_cn 层：title 和 content（段落数组）。"""
    return json.loads(session.to(MESSAGES)[index]['json']['content'])['zh_cn']


def texts(post: dict) -> list[str]:
    return [node['text'] for paragraph in post['content'] for node in paragraph if node['tag'] == 'text']


def images(post: dict) -> list[str]:
    return [node['image_key'] for paragraph in post['content'] for node in paragraph if node['tag'] == 'img']


def logged(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == 'oas' and r.levelno == level]


def snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob('*')) if p.is_file()}


# ---- 计划里的七个用例 ----------------------------------------------------------------------------------------

def test_message_persisted_before_send_attempt(tmp_path):
    start_instance()
    session = down_session()
    seen = []
    session.on_request = lambda path: seen.append(len(entries(tmp_path)))
    assert make_notifier(session).push('御灵GoryouRealm', '<oas1> ScriptError') is False
    assert seen == [1]                                              # 发第一个请求时，消息已经在发件箱里
    path, = entries(tmp_path)
    entry = json.loads(path.read_text(encoding='utf-8'))
    assert set(entry) == {'created_at', 'receive_id_type', 'receive_id', 'title', 'lines', 'png_base64'}
    assert (entry['receive_id_type'], entry['receive_id']) == ('open_id', 'ou_x')
    assert entry['title'] == 'OAS1 御灵GoryouRealm'
    assert entry['lines'] == ['<oas1> ScriptError']
    assert entry['png_base64'] is None


def test_flush_resends_and_clears(tmp_path):
    make_notifier(down_session()).push('御灵GoryouRealm', '<oas1> ScriptError')
    stem = entries(tmp_path)[0].stem
    session = ok_session()
    assert flush_outbox(root=tmp_path, session=session) == 1
    assert entries(tmp_path) == []
    call, = session.to(MESSAGES)
    assert call['json']['uuid'] == stem                             # 补发带条目的 id：响应丢了再发也不会重复
    assert call['params'] == {'receive_id_type': 'open_id'}
    assert call['json']['receive_id'] == 'ou_x'
    assert message(session)['title'] == 'OAS1 御灵GoryouRealm'
    assert texts(message(session)) == ['<oas1> ScriptError']


def _raising_source():
    raise RuntimeError('device gone')


@pytest.mark.parametrize('source', [_raising_source, lambda: None], ids=['raises', 'returns-none'])
def test_screenshot_failure_sends_text_only(tmp_path, source):
    row_running()
    write_log(['line 1', '', 'line 2'])
    start_instance()
    register_screenshot_provider(source)
    session = ok_session()
    assert make_notifier(session).push('t', 'c') is True
    assert session.to(IMAGES) == []
    post = message(session)
    assert images(post) == []
    assert texts(post) == ['c', ROW_LINE, '日志最后 2 行：', 'line 1', 'line 2']


def test_missing_credentials_returns_false(tmp_path, monkeypatch, caplog):
    no_credentials(monkeypatch)
    (tmp_path / '.env').write_text('', encoding='utf-8')
    session = FakeSession()
    assert make_notifier(session).push('t', 'c') is False
    assert session.calls == []
    assert not (tmp_path / 'log').exists()                          # 发件箱、合并状态都没写
    error, = logged(caplog, logging.ERROR)
    assert '飞书凭证缺失' in error


def test_secrets_never_written(tmp_path, monkeypatch, caplog):
    no_credentials(monkeypatch)
    (tmp_path / '.env').write_text(f'FEISHU_APP_ID=cli_fake\nFEISHU_APP_SECRET={SECRET}\n', encoding='utf-8')
    notifier = Notifier(FEISHU_YAML, True)
    notifier.config_name = 'OAS1'
    sessions = [
        down_session(),
        FakeSession().on(TOKEN, {'code': 10014, 'msg': 'app id not exists'}),
        FakeSession().on(TOKEN, RuntimeError('boom')),
        ok_session().on(MESSAGES, FakeResponse(400, {'code': 230002, 'msg': 'Bot is not in the chat'})),
        ok_session(),
    ]
    for index, session in enumerate(sessions):
        notifier.delegate.session = session
        notifier.push(title='t', content=f'c{index}')
        flush_outbox(root=tmp_path, session=session)
    assert sessions[-1].calls[0]['json']['app_secret'] == SECRET   # 密钥确实用上了
    files = {path: data for path, data in snapshot(tmp_path).items() if path != '.env'}
    assert files                                                    # 发件箱留下了条目（.failed）
    assert not [path for path, data in files.items() if b'S3CRET' in data]
    assert 'S3CRET' not in caplog.text
    echoes = [yaml.safe_dump(notifier.config, allow_unicode=True), repr(vars(notifier)), repr(vars(notifier.delegate))]
    assert not [echo for echo in echoes if 'S3CRET' in echo]


def test_orchestra_row_line(tmp_path):
    row_running(row_index=2, row_task='GoryouRealm')
    start_instance('oas1')
    session = ok_session()
    assert make_notifier(session).push('御灵GoryouRealm', '<oas1> ScriptError') is True
    assert texts(message(session))[:2] == ['<oas1> ScriptError', '编排：第 3 行 肝帝·御灵之境']


def test_notifier_delegation(tmp_path):
    notifier = Notifier("provider: feishu_app\nreceive_id_type: open_id\nreceive_id: ou_x", True)
    assert isinstance(notifier.delegate, FeishuAppNotifier)
    assert not hasattr(notifier, 'notifier')                        # 没交给 onepush（它不认识 feishu_app）
    assert notifier.delegate.session is requests                    # 线上用 requests；这里换成假的
    session = ok_session()
    notifier.delegate.session = session
    notifier.config_name = 'OAS1'
    assert notifier.push(title='t', content='c') is True
    assert notifier.delegate.config_name == 'OAS1'
    assert message(session)['title'] == 'OAS1 t'
    assert texts(message(session)) == ['c']
    assert entries(tmp_path) == []                                  # 发件箱在当前目录下，发出后已删


# ---- notify.py 与 script.py 的接入 -----------------------------------------------------------------------------

def delegating_notifier(session: FakeSession) -> Notifier:
    notifier = Notifier(FEISHU_YAML, True)
    notifier.delegate.session = session
    notifier.config_name = 'OAS1'
    return notifier


def test_a_dye_trials_push_with_message_instead_of_content(tmp_path):
    # tasks/DyeTrials/script_task.py 的写法：push(title=..., message=...)，没有 content
    session = ok_session()
    assert delegating_notifier(session).push(title='超鬼王', message='识别超时退出') is True
    assert message(session)['title'] == 'OAS1 超鬼王'
    assert texts(message(session)) == ['识别超时退出']


@pytest.mark.parametrize('kwargs, title', [
    ({'title': '超鬼王'}, 'OAS1 超鬼王'),
    ({}, 'OAS1'),
    ({'title': None, 'content': None}, 'OAS1'),
], ids=['title-only', 'nothing', 'none-values'])
def test_a_push_with_neither_content_nor_message(tmp_path, kwargs, title):
    session = ok_session()
    assert delegating_notifier(session).push(**kwargs) is True
    post = message(session)
    assert post['title'] == title
    assert texts(post) == ['（没有说明文字）']                     # 正文不留空：飞书怎么处理空的 content 没核实


def test_no_placeholder_when_the_log_tail_has_lines(tmp_path):
    write_log(['line 1'])
    session = ok_session()
    assert delegating_notifier(session).push(title='超鬼王') is True
    assert texts(message(session)) == ['日志最后 1 行：', 'line 1']


def test_disabled_notifier_does_nothing(tmp_path):
    notifier = Notifier(FEISHU_YAML, False)
    assert getattr(notifier, 'delegate', None) is None
    assert notifier.push(title='t', content='c') is False
    session = FakeSession()
    assert FeishuAppNotifier(dict(CFG), False, root=tmp_path, session=session).push('t', 'c') is False
    assert session.calls == []
    assert not (tmp_path / 'log').exists()


def test_other_providers_are_left_to_onepush():
    notifier = Notifier('provider: bark\nkey: k', True)
    assert getattr(notifier, 'delegate', None) is None
    assert notifier.notifier.name == 'bark'


def test_script_py_calls_the_hook_right_after_the_loop_start_log():
    tree = ast.parse(SCRIPT_PY.read_text(encoding='utf-8'))
    script_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Script')
    loop = next(n for n in script_class.body if isinstance(n, ast.FunctionDef) and n.name == 'loop')
    start = next(i for i, stmt in enumerate(loop.body) if 'Start scheduler loop' in ast.unparse(stmt))
    assert ast.unparse(loop.body[start + 1]) == 'on_script_loop_start(self)'
    imported = {alias.name for n in tree.body
                if isinstance(n, ast.ImportFrom) and n.module == 'module.notify.feishu_app' for alias in n.names}
    assert imported == {'on_script_loop_start'}


# ---- 配置与凭证 -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('cfg', [
    {'provider': 'feishu_app', 'receive_id_type': 'open_id'},
    {'provider': 'feishu_app', 'receive_id_type': 'open_id', 'receive_id': '  '},
    {'provider': 'feishu_app', 'receive_id_type': 'open_id', 'receive_id': 123},
    {'provider': 'feishu_app', 'receive_id_type': 'user_id', 'receive_id': 'ou_x'},
    {'provider': 'feishu_app', 'receive_id': 'ou_x'},
], ids=['no-receive-id', 'blank-receive-id', 'numeric-receive-id', 'unsupported-type', 'no-type'])
def test_missing_or_invalid_receive_target_writes_nothing(tmp_path, caplog, cfg):
    start_instance()
    session = FakeSession()
    assert make_notifier(session, cfg).push('t', 'c') is False
    assert session.calls == []
    assert not (tmp_path / 'log').exists()
    error, = logged(caplog, logging.ERROR)
    assert '飞书推送配置有误' in error


@pytest.mark.parametrize('minutes', ['abc', -1, 1441, True, 2.5])
def test_an_invalid_aggregate_minutes_falls_back_to_ten(tmp_path, caplog, minutes):
    start_instance()
    assert make_notifier(ok_session(), {**CFG, 'aggregate_minutes': minutes}).push('t', 'c') is True
    warning, = logged(caplog, logging.WARNING)
    assert 'aggregate_minutes' in warning
    state = json.loads((tmp_path / 'log' / 'feishu_aggregate.json').read_text(encoding='utf-8'))
    window, = state.values()
    assert window['meta']['window'] == 10


# ---- 发送失败的几种情形 ----------------------------------------------------------------------------------------

def test_rejected_message_goes_to_failed(tmp_path, caplog):
    session = ok_session().on(MESSAGES, FakeResponse(400, {'code': 230002, 'msg': 'Bot is not in the chat'}))
    assert make_notifier(session).push('t', 'c') is False
    assert entries(tmp_path) == []
    failed, = (tmp_path / 'log' / 'feishu_outbox').glob('*.failed')
    error, = logged(caplog, logging.ERROR)
    assert 'code=230002' in error and 'Bot is not in the chat' in error


@pytest.mark.parametrize('reply, level', [
    (requests.ConnectionError('offline'), logging.WARNING),
    (FakeResponse(503, {'code': 1, 'msg': 'busy'}), logging.WARNING),
    ({'code': 10014, 'msg': 'app id not exists'}, logging.ERROR),
], ids=['network', 'transient', 'credentials'])
def test_a_send_that_may_work_later_keeps_the_entry(tmp_path, caplog, reply, level):
    session = FakeSession().on(TOKEN, reply)
    assert make_notifier(session).push('t', 'c') is False
    assert len(entries(tmp_path)) == 1
    line, = [r.getMessage() for r in caplog.records if r.name == 'oas' and r.levelno >= logging.WARNING]
    assert line in logged(caplog, level)


def test_push_returns_false_when_the_outbox_cannot_be_written(tmp_path, monkeypatch, caplog):
    def refuse(self, msg):
        raise PermissionError('disk says no')

    monkeypatch.setattr(feishu_app.Outbox, 'put', refuse)
    session = FakeSession()
    assert make_notifier(session).push('t', 'c') is False
    assert session.calls == []                                     # 没落盘就不发
    error, = logged(caplog, logging.ERROR)
    assert 'disk says no' in error


def test_an_unexpected_error_keeps_the_entry_and_is_logged_by_type_and_message(tmp_path, caplog):
    session = FakeSession().on(TOKEN, RuntimeError('session blew up'))
    assert make_notifier(session).push('t', 'c') is False
    assert len(entries(tmp_path)) == 1
    error, = logged(caplog, logging.ERROR)
    assert 'RuntimeError: session blew up' in error
    assert not [r for r in caplog.records if r.exc_info]           # 没带 traceback（exc_info）


def test_an_unexpected_error_is_logged_without_traceback_locals(tmp_path, monkeypatch):
    # 照 module/logger.py 的文件 handler 配一个 RichHandler：rich traceback 带每一帧的局部变量。
    # 控制台放宽到 500 列，长行不折断，搜字符串不会漏
    stream = io.StringIO()
    handler = RichHandler(console=Console(file=stream, no_color=True, highlight=False, width=500), show_path=False,
                          show_time=False, show_level=False, rich_tracebacks=True, tracebacks_show_locals=True,
                          tracebacks_extra_lines=3, tracebacks_width=500, highlighter=NullHighlighter())
    monkeypatch.setattr(logger, 'handlers', [handler])
    session = FakeSession().on(TOKEN, RuntimeError('session blew up'))
    assert make_notifier(session).push('t', 'c') is False
    assert session.calls[0]['json']['app_secret'] == SECRET        # 抛出的那一刻，密钥就在取 token 请求的参数里
    text = stream.getvalue()
    assert 'RuntimeError: session blew up' in text
    assert SECRET not in text


# ---- 同类合并 -------------------------------------------------------------------------------------------------

def test_second_identical_push_in_window_is_aggregated(tmp_path, clock, caplog):
    start_instance()
    session = ok_session()
    notifier = make_notifier(session)
    assert notifier.push('t', 'c') is True
    clock.now = T0 + timedelta(minutes=5)
    assert notifier.push('t', 'c') is True
    assert len(session.to(MESSAGES)) == 1
    assert entries(tmp_path) == []
    assert [line for line in logged(caplog, logging.INFO) if '同类错误合并中' in line]
    assert notifier.push('t', 'other content') is True              # 内容不同就不算同类
    assert len(session.to(MESSAGES)) == 2


@pytest.mark.parametrize('instance, cfg', [(False, CFG), (True, {**CFG, 'aggregate_minutes': 0})],
                         ids=['no-screenshot-source', 'zero-minutes'])
def test_no_aggregation_without_screenshot_source_or_window(tmp_path, clock, instance, cfg):
    if instance:
        start_instance()
    session = ok_session()
    notifier = make_notifier(session, cfg)
    assert notifier.push('t', 'c') is True
    assert notifier.push('t', 'c') is True
    assert len(session.to(MESSAGES)) == 2
    assert (tmp_path / 'log' / 'feishu_aggregate.json').exists() == instance


def test_due_summary_goes_out_through_flush_to_first_target(tmp_path, clock):
    start_instance()
    first = {**CFG, 'receive_id_type': 'chat_id', 'receive_id': 'oc_first', 'aggregate_minutes': 5}
    assert make_notifier(ok_session(), first).push('t', 'c') is True
    clock.now = T0 + timedelta(minutes=1)
    assert make_notifier(FakeSession(), first).push('t', 'c') is True
    clock.now = T0 + timedelta(minutes=2)                           # 中途改了接收方：汇总仍跟窗口里第一条走
    assert make_notifier(FakeSession(), {**CFG, 'receive_id': 'ou_second'}).push('t', 'c') is True
    session = ok_session()
    clock.now = T0 + timedelta(minutes=3)
    assert flush_outbox(root=tmp_path, session=session) == 0       # 窗口还没结束
    assert session.calls == []
    clock.now = T0 + timedelta(minutes=6)
    assert flush_outbox(root=tmp_path, session=session) == 1
    call, = session.to(MESSAGES)
    assert call['params'] == {'receive_id_type': 'chat_id'}
    assert call['json']['receive_id'] == 'oc_first'
    assert message(session) == {'title': 'OAS1 t',
                                'content': [[{'tag': 'text', 'text': 'OAS1 t：过去 5 分钟同类错误又出现 2 次'}]]}
    assert session.to(IMAGES) == []
    assert entries(tmp_path) == []


# ---- 消息内容：编排行、日志尾、截图、长度上限 --------------------------------------------------------------------

@pytest.mark.parametrize('phase, row_task, expected', [
    ('resting', 'GoryouRealm', None),
    ('row_running', 'NotInTheTable', '编排：第 3 行 NotInTheTable'),
], ids=['not-running', 'unknown-task'])
def test_orchestra_row_line_follows_the_progress_file(tmp_path, phase, row_task, expected):
    row_running(row_task=row_task, phase=phase)
    start_instance()
    session = ok_session()
    make_notifier(session).push('t', 'c')
    assert texts(message(session)) == ['c'] + ([expected] if expected else [])


def test_no_row_line_without_a_registered_instance(tmp_path):
    row_running()
    session = ok_session()
    make_notifier(session).push('t', 'c')
    assert texts(message(session)) == ['c']


def test_log_tail_is_the_last_30_lines_masked(tmp_path, monkeypatch):
    filler = ['x' * 79] * 3000                                      # 约 240 KB：只读文件末尾
    write_log(filler + [f'line {i:02d} private' for i in range(1, 41)])
    monkeypatch.setattr('module.handler.sensitive_info.handle_sensitive_logs',
                        lambda lines: [line.replace('private', '***') for line in lines])
    session = ok_session()
    make_notifier(session).push('t', 'c')
    assert texts(message(session)) == ['c', '日志最后 30 行：'] + [f'line {i:02d} ***' for i in range(11, 41)]


def test_post_body_stays_under_size_cap(tmp_path):
    row_running()
    write_log([f'{i:02d} ' + '御' * 400 for i in range(40)])
    start_instance(frame=FRAME)
    session = ok_session()
    assert make_notifier(session).push('t', 'summary') is True
    call, = session.to(MESSAGES)
    assert len(json.dumps(call['json']['content'])) <= 28_000      # 照 requests 的 json= 算：非 ASCII 转成 \uXXXX
    post = message(session)
    assert images(post) == ['img_v2_abc']
    lines = texts(post)
    log_lines = lines[3:]
    assert lines[:3] == ['summary', ROW_LINE, f'日志最后 {len(log_lines)} 行：']
    assert 0 < len(log_lines) < 30                                  # 从最早的日志行删起
    assert all(len(line) <= 300 for line in log_lines)
    assert log_lines[-1].startswith('39 ')                          # 最新的一行留着


def test_recent_error_screenshot_is_preferred_and_sent_as_saved(tmp_path):
    start_instance(frame=FRAME)
    now_ms = int(time.time() * 1000)
    older = tmp_path / 'log' / 'error' / str(now_ms - 5000)
    newest = tmp_path / 'log' / 'error' / str(now_ms)
    for folder in (older, newest):
        folder.mkdir(parents=True)
    (older / '2026-11-21_10-00-09-000000.png').write_bytes(b'older folder')
    (newest / '2026-11-21_10-00-00-000000.png').write_bytes(b'earlier shot')
    (newest / '2026-11-21_10-00-01-000000.png').write_bytes(b'latest shot')
    session = ok_session()
    assert make_notifier(session).push('t', 'c') is True
    upload, = session.to(IMAGES)
    assert upload['files']['image'][1] == b'latest shot'
    assert images(message(session)) == ['img_v2_abc']


def test_stale_error_screenshot_falls_back_to_the_masked_frame(tmp_path, monkeypatch):
    stale = tmp_path / 'log' / 'error' / str(int(time.time() * 1000) - 61_000)
    stale.mkdir(parents=True)
    (stale / 'shot.png').write_bytes(b'stale')
    masked = []
    monkeypatch.setattr('module.handler.sensitive_info.handle_sensitive_image',
                        lambda image: masked.append(image) or image)
    start_instance(frame=FRAME)
    session = ok_session()
    assert make_notifier(session).push('t', 'c') is True
    assert len(masked) == 1 and masked[0] is FRAME
    upload, = session.to(IMAGES)
    decoded = np.asarray(Image.open(io.BytesIO(upload['files']['image'][1])))
    assert decoded.shape == FRAME.shape and (decoded == FRAME).all()   # 按 RGB 原样编码成 PNG


# ---- 传图失败 ------------------------------------------------------------------------------------------------

UPLOAD_REJECTED = FakeResponse(400, {'code': 99991672,
                                     'msg': 'Access denied. One of the following scopes is required: [im:resource]'})


def send_with_screenshot(how: str, root: Path, session: FakeSession):
    """带截图发一条：how 为 push 时立即发；为 flush 时先断网写进发件箱，再由 flush_outbox 补发。两处走同一条发送路径。"""
    start_instance(frame=FRAME)
    if how == 'push':
        return make_notifier(session).push('t', 'c')
    make_notifier(down_session()).push('t', 'c')
    return flush_outbox(root=root, session=session)


@pytest.mark.parametrize('how, sent', [('push', True), ('flush', 1)])
def test_a_rejected_screenshot_upload_still_sends_the_text(tmp_path, caplog, how, sent):
    session = ok_session().on(IMAGES, UPLOAD_REJECTED)
    assert send_with_screenshot(how, tmp_path, session) == sent
    assert len(session.to(IMAGES)) == 1
    assert len(session.to(MESSAGES)) == 1
    post = message(session)
    assert images(post) == []
    assert texts(post) == ['c', '截图没能附上：飞书拒收了这张图（code=99991672）']
    assert entries(tmp_path) == []                                  # 文字发出去了，条目删掉
    assert not list((tmp_path / 'log' / 'feishu_outbox').glob('*.failed'))
    rejected = [line for line in logged(caplog, logging.ERROR) if '99991672' in line]
    assert len(rejected) == 1 and 'im:resource' in rejected[0]     # 拒收的 code 和 msg 记一行
    assert not [r for r in caplog.records if r.exc_info]


@pytest.mark.parametrize('reply', [FakeResponse(503, {'code': 1, 'msg': 'busy'}), requests.ConnectionError('offline')],
                         ids=['transient', 'network'])
@pytest.mark.parametrize('how, sent', [('push', False), ('flush', 0)])
def test_a_screenshot_upload_that_may_work_later_keeps_the_entry(tmp_path, how, sent, reply):
    session = ok_session().on(IMAGES, reply)
    assert send_with_screenshot(how, tmp_path, session) == sent
    assert len(session.to(IMAGES)) == 1
    assert session.to(MESSAGES) == []
    entry, = [json.loads(p.read_text(encoding='utf-8')) for p in entries(tmp_path)]
    assert entry['png_base64'] and entry['lines'] == ['c']          # 条目原样留着，截图也在，下次连图一起发


# ---- 补发 ----------------------------------------------------------------------------------------------------

def test_flush_with_nothing_to_do_makes_no_request(tmp_path, monkeypatch, caplog):
    session = FakeSession()
    assert flush_outbox(root=tmp_path, session=session) == 0
    no_credentials(monkeypatch)
    assert flush_outbox(root=tmp_path, session=session) == 0       # 没配飞书的用户：凭证缺失也不报
    assert session.calls == []
    assert not (tmp_path / 'log').exists()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_flush_missing_credentials_logs_once_and_touches_nothing(tmp_path, monkeypatch, caplog, clock):
    start_instance()
    make_notifier(down_session()).push('t', 'c')
    clock.now = T0 + timedelta(minutes=1)
    make_notifier(FakeSession()).push('t', 'c')                    # 合并掉一条，汇总在 10 分钟后到期
    no_credentials(monkeypatch)
    clock.now = T0 + timedelta(minutes=30)
    before = snapshot(tmp_path / 'log')
    session = FakeSession()
    assert flush_outbox(root=tmp_path, session=session) == 0
    assert flush_outbox(root=tmp_path, session=session) == 0
    assert session.calls == []
    assert snapshot(tmp_path / 'log') == before                     # 发件箱和合并状态都没动
    assert len([line for line in logged(caplog, logging.ERROR) if '飞书凭证缺失' in line]) == 1


def test_flush_stops_at_a_network_error_and_keeps_the_rest(tmp_path, caplog):
    start_instance()
    for content in ('c1', 'c2', 'c3'):
        make_notifier(down_session()).push('t', content)
    session = FakeSession().on(TOKEN, token_reply()).on(MESSAGES, SENT, requests.ConnectionError('offline'))
    assert flush_outbox(root=tmp_path, session=session) == 1
    assert len(session.to(MESSAGES)) == 2                           # 第二条断网，这一轮到此为止
    assert [json.loads(p.read_text(encoding='utf-8'))['lines'] for p in entries(tmp_path)] == [['c2'], ['c3']]
    assert flush_outbox(root=tmp_path, session=session) == 0
    assert len(entries(tmp_path)) == 2
    assert len([line for line in logged(caplog, logging.WARNING) if '飞书补发' in line]) == 1


def test_flush_fails_a_rejected_entry_and_goes_on(tmp_path, caplog):
    for content in ('c1', 'c2'):
        make_notifier(down_session()).push('t', content)
    session = ok_session().on(MESSAGES, FakeResponse(400, {'code': 230002, 'msg': 'Bot is not in the chat'}), SENT)
    assert flush_outbox(root=tmp_path, session=session) == 1
    assert entries(tmp_path) == []
    failed, = (tmp_path / 'log' / 'feishu_outbox').glob('*.failed')
    assert json.loads(failed.read_text(encoding='utf-8'))['lines'] == ['c1']
    assert [line for line in logged(caplog, logging.ERROR) if 'code=230002' in line]


def test_flush_logs_a_credentials_error_once_until_a_send_succeeds(tmp_path, caplog):
    bad = FakeSession().on(TOKEN, {'code': 10014, 'msg': 'app id not exists'})
    make_notifier(down_session()).push('t', 'c1')
    assert flush_outbox(root=tmp_path, session=bad) == 0
    assert flush_outbox(root=tmp_path, session=bad) == 0
    assert flush_outbox(root=tmp_path, session=ok_session()) == 1
    make_notifier(down_session()).push('t', 'c2')
    assert flush_outbox(root=tmp_path, session=bad) == 0
    assert len([line for line in logged(caplog, logging.ERROR) if '飞书补发' in line]) == 2


def test_flush_quarantines_an_entry_with_missing_fields(tmp_path, caplog):
    make_notifier(down_session()).push('t', 'c1')
    broken = feishu_app.Outbox(tmp_path / 'log' / 'feishu_outbox').put({'title': 't', 'receive_id': 'ou_x'})
    make_notifier(down_session()).push('t', 'c3')
    session = ok_session()
    assert flush_outbox(root=tmp_path, session=session) == 2
    assert entries(tmp_path) == []
    assert broken.with_name(broken.name + '.bad').exists()
    assert len(session.to(MESSAGES)) == 2


def test_flush_counts_a_send_whose_entry_cannot_be_deleted(tmp_path, monkeypatch, caplog):
    make_notifier(down_session()).push('t', 'c')

    def locked(self, path):
        raise PermissionError('locked')

    monkeypatch.setattr(feishu_app.Outbox, 'done', locked)
    session = ok_session()
    assert flush_outbox(root=tmp_path, session=session) == 1
    assert len(session.to(MESSAGES)) == 1                           # 这一轮不重发
    assert len(entries(tmp_path)) == 1                              # 下一轮再发，同一 uuid 飞书 1 小时内只发一条
    assert [line for line in logged(caplog, logging.WARNING) if 'locked' in line]


# ---- 实例启动：登记与自检 --------------------------------------------------------------------------------------

def test_loop_start_registers_without_touching_the_lazy_device(tmp_path, caplog):
    script = start_instance('oas1')
    assert feishu_app._screenshot_provider() is None                # 设备还没建：不去建它
    script.__dict__['device'] = SimpleNamespace(image=FRAME)
    assert feishu_app._screenshot_provider() is FRAME
    assert feishu_app._instance_name == 'oas1'
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize('reply, level, expected', [
    (requests.ConnectionError('offline'), logging.ERROR, '飞书网络错误'),
    ({'code': 10014, 'msg': 'app id not exists'}, logging.ERROR, '飞书凭证错误：code=10014，msg=app id not exists'),
    (FakeResponse(503, {'code': 1, 'msg': 'busy'}), logging.ERROR, '飞书自检没通过（暂时性错误'),
    (token_reply(), logging.INFO, '飞书自检通过'),
], ids=['network', 'credentials', 'transient', 'ok'])
def test_loop_start_self_check_reports_one_line(tmp_path, caplog, reply, level, expected):
    session = FakeSession().on(TOKEN, reply)
    start_instance(notify_enable=True, session=session)
    assert len(session.to(TOKEN)) == 1
    line, = [r for r in caplog.records if r.name == 'oas' and '飞书' in r.getMessage()]
    assert line.levelno == level and expected in line.getMessage()


@pytest.mark.parametrize('notify_config, expected', [
    (FEISHU_YAML, '飞书凭证缺失'),
    ('provider: feishu_app\nreceive_id_type: open_id\n', '飞书推送配置有误'),
], ids=['no-credentials', 'no-receive-id'])
def test_loop_start_self_check_reports_configuration_errors_without_a_request(tmp_path, monkeypatch, caplog,
                                                                              notify_config, expected):
    no_credentials(monkeypatch)
    session = FakeSession()
    start_instance(notify_enable=True, notify_config=notify_config, session=session)
    assert session.calls == []
    error, = logged(caplog, logging.ERROR)
    assert expected in error


@pytest.mark.parametrize('enable, notify_config', [
    (False, FEISHU_YAML),
    (True, 'provider: bark\nkey: k'),
    (True, 'provider: [not closed'),
], ids=['disabled', 'other-provider', 'broken-yaml'])
def test_loop_start_self_check_is_skipped_unless_feishu_is_on(tmp_path, caplog, enable, notify_config):
    session = FakeSession()
    start_instance(notify_enable=enable, notify_config=notify_config, session=session)
    assert session.calls == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
