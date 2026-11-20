"""飞书客户端、发件箱、同类合并器（module.notify.feishu_app）的测试。

不联网：FeishuClient 的请求全部走注入的 FakeSession，它按 URL 路径返回预设的响应，并记下每个请求。
文件只写 tmp_path。logger 换成 LogSpy（自动夹具），不往真日志里写。token 缓存用假时钟，合并器直接传 now。
"""
import copy
import json
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from module.notify import feishu_app
from module.notify.feishu_app import Aggregator, FeishuClient, FeishuError, Outbox

BASE = 'https://open.feishu.cn'
TOKEN = '/open-apis/auth/v3/tenant_access_token/internal'
IMAGES = '/open-apis/im/v1/images'
MESSAGES = '/open-apis/im/v1/messages'
CHATS = '/open-apis/im/v1/chats'
SECRET = 'S3CRET-fake-app-secret'
PNG = b'\x89PNG\r\n\x1a\nfake-png-bytes'
SENT = {'code': 0, 'msg': 'success', 'data': {'message_id': 'om_1'}}
UPLOADED = {'code': 0, 'msg': 'success', 'data': {'image_key': 'img_v2_abc'}}
NO_CHATS = {'code': 0, 'msg': 'success', 'data': {'items': [], 'has_more': False}}
TOKEN_ERROR_CODES = [99991661, 99991663, 99991664, 99991665, 99991668, 99991671]


class FakeResponse:
    """够 FeishuClient 用的响应：status_code 和 json()。body 为 str 时按原文解析，不是 JSON 就抛 ValueError（同 requests）。"""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return copy.deepcopy(self._body)


class FakeSession:
    """按 URL 路径返回预设的响应，并记下每个请求（方法、URL、路径和全部关键字参数）。

    on(path, *replies)：replies 依次用掉，最后一个一直重复。reply 可以是 dict（HTTP 200 的 JSON）、FakeResponse，
    或异常实例（调用时抛出）。请求没预设的路径会抛 KeyError，让测试失败。
    """

    def __init__(self):
        self.replies: dict[str, list] = {}
        self.calls: list[dict] = []

    def on(self, path: str, *replies) -> 'FakeSession':
        self.replies[path] = list(replies)
        return self

    def _handle(self, method: str, url: str, **kwargs):
        path = urlsplit(url).path
        self.calls.append({'method': method, 'url': url, 'path': path, **kwargs})
        queue = self.replies[path]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, FakeResponse) else FakeResponse(200, reply)

    def post(self, url, **kwargs):
        return self._handle('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self._handle('GET', url, **kwargs)

    def to(self, path: str) -> list[dict]:
        return [call for call in self.calls if call['path'] == path]


class FakeClock:
    """单调时钟的替身：返回 now，测试直接改它。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class LogSpy:
    """换掉 feishu_app.logger：按级别记下消息，不往真日志里写。"""

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
        return [message for lv, message in self.records if lv == level]


@pytest.fixture(autouse=True)
def log(monkeypatch) -> LogSpy:
    spy = LogSpy()
    monkeypatch.setattr(feishu_app, 'logger', spy)
    return spy


def token_reply(token: str = 't-1', expire: int = 7200) -> dict:
    return {'code': 0, 'msg': 'ok', 'tenant_access_token': token, 'expire': expire}


def make_client(session: FakeSession, **kwargs) -> FeishuClient:
    return FeishuClient('cli_fake', SECRET, session=session, **kwargs)


def send(client: FeishuClient) -> None:
    client.send_post('open_id', 'ou_x', 't', ['a'], None)


def send_error(*message_replies) -> tuple[FeishuError, FakeSession]:
    """token 正常（依次给 t-1、t-2），发消息接口依次回 message_replies；返回抛出的 FeishuError 和会话。"""
    session = FakeSession().on(TOKEN, token_reply('t-1'), token_reply('t-2')).on(MESSAGES, *message_replies)
    with pytest.raises(FeishuError) as info:
        send(make_client(session))
    return info.value, session


def token_error(*token_replies) -> tuple[FeishuError, FakeSession]:
    """取 token 的接口依次回 token_replies，发消息接口正常；返回抛出的 FeishuError 和会话。"""
    session = FakeSession().on(TOKEN, *token_replies).on(MESSAGES, SENT)
    with pytest.raises(FeishuError) as info:
        send(make_client(session))
    return info.value, session


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 21, hour, minute)


# ---- 计划给的六个用例 ------------------------------------------------------------------------------------------

def test_token_cached():
    session = FakeSession().on(TOKEN, token_reply('t-1')).on(MESSAGES, SENT)
    client = make_client(session)
    client.send_post('open_id', 'ou_x', 't', ['第一条'], None)
    client.send_post('open_id', 'ou_x', 't', ['第二条'], None)
    assert len(session.to(TOKEN)) == 1
    assert [call['headers']['Authorization'] for call in session.to(MESSAGES)] == ['Bearer t-1', 'Bearer t-1']


def test_expired_token_is_refreshed_once():
    expired = FakeResponse(400, {'code': 99991663, 'msg': 'Invalid access token for authorization.'})
    session = FakeSession().on(TOKEN, token_reply('t-1'), token_reply('t-2')).on(MESSAGES, expired, SENT)
    send(make_client(session))                                      # 不抛异常：换 token 重发成功
    assert len(session.to(TOKEN)) == 2
    assert [call['headers']['Authorization'] for call in session.to(MESSAGES)] == ['Bearer t-1', 'Bearer t-2']


def test_upload_then_post_uses_image_key():
    session = FakeSession().on(TOKEN, token_reply()).on(IMAGES, UPLOADED).on(MESSAGES, SENT)
    client = make_client(session)
    image_key = client.upload_image(PNG)
    client.send_post('open_id', 'ou_x', 'OAS1 探索', ['第一行'], image_key)
    assert image_key == 'img_v2_abc'
    content = json.loads(session.to(MESSAGES)[0]['json']['content'])
    assert content['zh_cn']['content'][-1] == [{'tag': 'img', 'image_key': 'img_v2_abc'}]
    upload = session.to(IMAGES)[0]
    assert upload['data'] == {'image_type': 'message'}
    _, blob, mime = upload['files']['image']
    assert (blob, mime) == (PNG, 'image/png')


def test_outbox_roundtrip_and_done(tmp_path, log):
    box = tmp_path / 'feishu_outbox'
    outbox = Outbox(box)
    assert not box.exists()                                         # 构造时不建目录
    msg = {'title': 'OAS1 探索', 'lines': ['第一行', 'line 2'], 'attempts': 0}
    p = outbox.put(msg)
    assert p.parent == box and p.suffix == '.json'
    assert outbox.pending() == [p]
    assert outbox.load(p) == msg
    assert '探索' in p.read_text(encoding='utf-8')                  # UTF-8 原文，不转义
    outbox.done(p)
    assert not p.exists()
    assert outbox.pending() == []
    assert log.records == []


def test_corrupt_outbox_entry_is_quarantined(tmp_path, log):
    outbox = Outbox(tmp_path)
    good = outbox.put({'n': 1})
    bad = tmp_path / '0000000000001-1-corrupt.json'
    bad.write_bytes(b'{"title": "half')
    assert outbox.pending() == [bad, good]                          # 名字小的在前
    assert outbox.load(bad) is None
    assert not bad.exists()
    assert (tmp_path / (bad.name + '.bad')).read_bytes() == b'{"title": "half'
    assert outbox.pending() == [good]
    assert outbox.load(good) == {'n': 1}                            # 坏文件不卡住后面的
    assert len(log.messages('warning')) == 1


def test_aggregator_window(tmp_path):
    agg = Aggregator(tmp_path / 'feishu_aggregate.json', 10)
    key = 'OAS1|探索|游戏卡死'
    assert agg.admit(key, at(10, 0)) is True
    assert agg.admit(key, at(10, 5)) is False
    assert agg.due_summaries(at(10, 11)) == [(key, 1)]


# ---- 客户端：请求的样子 --------------------------------------------------------------------------------------

def test_token_request_matches_the_documented_shape():
    session = FakeSession().on(TOKEN, token_reply('t-abc'))
    assert make_client(session).tenant_token() == 't-abc'
    call = session.calls[0]
    assert (call['method'], call['url']) == ('POST', BASE + TOKEN)
    assert call['json'] == {'app_id': 'cli_fake', 'app_secret': SECRET}
    assert call['headers'] == {'Content-Type': 'application/json; charset=utf-8'}


def test_send_post_body_matches_the_documented_shape():
    session = FakeSession().on(TOKEN, token_reply()).on(MESSAGES, SENT)
    client = make_client(session)
    client.send_post('chat_id', 'oc_1', 'OAS1 探索', ['第一行', 'line 2'], 'img_v2_x', uuid='0000000000001-2-ab')
    client.send_post('open_id', 'ou_x', '标题', ['只有一行'], None)
    first, second = session.to(MESSAGES)
    assert (first['method'], first['url']) == ('POST', BASE + MESSAGES)
    assert first['params'] == {'receive_id_type': 'chat_id'}
    assert first['headers']['Content-Type'] == 'application/json; charset=utf-8'
    body = first['json']
    assert set(body) == {'receive_id', 'msg_type', 'content', 'uuid'}
    assert (body['receive_id'], body['msg_type'], body['uuid']) == ('oc_1', 'post', '0000000000001-2-ab')
    assert isinstance(body['content'], str)                         # content 是 JSON 字符串
    assert json.loads(body['content']) == {'zh_cn': {'title': 'OAS1 探索', 'content': [
        [{'tag': 'text', 'text': '第一行'}],
        [{'tag': 'text', 'text': 'line 2'}],
        [{'tag': 'img', 'image_key': 'img_v2_x'}],                  # 图片独占最后一段
    ]}}
    assert second['params'] == {'receive_id_type': 'open_id'}
    assert 'uuid' not in second['json']
    assert json.loads(second['json']['content']) == {'zh_cn': {'title': '标题', 'content': [
        [{'tag': 'text', 'text': '只有一行'}]]}}


def test_send_post_refuses_a_uuid_longer_than_50_characters():
    session = FakeSession().on(TOKEN, token_reply()).on(MESSAGES, SENT)
    client = make_client(session)
    with pytest.raises(ValueError):
        client.send_post('open_id', 'ou_x', 't', ['a'], None, uuid='x' * 51)
    assert session.calls == []                                      # 一个请求都没发
    client.send_post('open_id', 'ou_x', 't', ['a'], None, uuid='x' * 50)
    assert session.to(MESSAGES)[0]['json']['uuid'] == 'x' * 50


def test_every_request_has_a_timeout():
    session = FakeSession().on(TOKEN, token_reply()).on(IMAGES, UPLOADED).on(MESSAGES, SENT).on(CHATS, NO_CHATS)
    client = make_client(session)
    client.send_post('open_id', 'ou_x', 't', ['a'], client.upload_image(PNG))
    client.list_chats()
    assert {call['path'] for call in session.calls} == {TOKEN, IMAGES, MESSAGES, CHATS}
    assert all(call['timeout'] == (5, 10) for call in session.calls)


def test_base_url_can_point_elsewhere():
    session = FakeSession().on(TOKEN, token_reply())
    FeishuClient('cli_fake', SECRET, session=session, base='http://127.0.0.1:9/').tenant_token()
    assert session.calls[0]['url'] == 'http://127.0.0.1:9' + TOKEN


# ---- 客户端：token 缓存与刷新 -------------------------------------------------------------------------------------

def test_token_is_reused_until_60_seconds_before_it_expires():
    clock = FakeClock(1000.0)
    session = FakeSession().on(TOKEN, token_reply('t-1', expire=7200), token_reply('t-2', expire=7200))
    client = make_client(session, clock=clock)
    assert client.tenant_token() == 't-1'
    clock.now = 1000.0 + 7200 - 60 - 0.001
    assert client.tenant_token() == 't-1'
    clock.now = 1000.0 + 7200 - 60
    assert client.tenant_token() == 't-2'
    assert len(session.to(TOKEN)) == 2


def test_a_failed_token_request_is_not_cached():
    session = FakeSession().on(TOKEN, requests.ConnectionError('connection refused'), token_reply('t-1'))
    client = make_client(session)
    with pytest.raises(FeishuError):
        client.tenant_token()
    assert client.tenant_token() == 't-1'


@pytest.mark.parametrize('code', TOKEN_ERROR_CODES)
def test_every_token_error_code_refreshes_and_retries_once(code):
    stale = FakeResponse(400, {'code': code, 'msg': 'token'})
    session = FakeSession().on(TOKEN, token_reply('t-1'), token_reply('t-2')).on(MESSAGES, stale, SENT)
    send(make_client(session))
    assert len(session.to(TOKEN)) == 2
    assert [call['headers']['Authorization'] for call in session.to(MESSAGES)] == ['Bearer t-1', 'Bearer t-2']


@pytest.mark.parametrize('path, call, ok', [
    (MESSAGES, send, SENT),
    (IMAGES, lambda client: client.upload_image(PNG), UPLOADED),
    (CHATS, lambda client: client.list_chats(), NO_CHATS),
])
def test_every_api_refreshes_the_token_once(path, call, ok):
    stale = FakeResponse(400, {'code': 99991663, 'msg': 'Invalid access token'})
    session = FakeSession().on(TOKEN, token_reply('t-1'), token_reply('t-2')).on(path, stale, ok)
    call(make_client(session))
    assert len(session.to(TOKEN)) == 2
    assert [c['headers']['Authorization'] for c in session.to(path)] == ['Bearer t-1', 'Bearer t-2']


def test_a_token_error_that_survives_the_refresh_is_transient():
    err, session = send_error(FakeResponse(400, {'code': 99991663, 'msg': 'Invalid access token'}))
    assert (err.kind, err.code, err.msg) == ('transient', 99991663, 'Invalid access token')
    assert len(session.to(TOKEN)) == 2
    assert len(session.to(MESSAGES)) == 2


# ---- 客户端：失败分类 ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('exc', [
    requests.ConnectionError('connection refused'),
    requests.ConnectTimeout('connect timed out'),
    requests.ReadTimeout('read timed out'),
])
@pytest.mark.parametrize('where', [TOKEN, MESSAGES])
def test_connection_errors_and_timeouts_are_network_errors(exc, where):
    if where == TOKEN:
        err, session = token_error(exc)
    else:
        err, session = send_error(exc)
    assert (err.kind, err.code, err.msg) == ('network', None, '')
    assert err.__cause__ is exc


@pytest.mark.parametrize('reply', [
    {'code': 10003, 'data': {}, 'msg': 'invalid param'},           # 实测（P1、P4）
    {'code': 10014, 'msg': 'app id not exists'},                   # 实测（P2、P3）
    {'code': 10013, 'msg': 'get tenant access token fail'},        # 文档
    {'code': 10015, 'msg': 'wrong app secret'},                    # 文档
])
def test_a_token_endpoint_refusal_is_a_credentials_error(reply):
    err, session = token_error(reply)                               # HTTP 200 加非 0 code
    assert (err.kind, err.code, err.msg) == ('credentials', reply['code'], reply['msg'])
    assert session.to(MESSAGES) == []


def test_a_token_endpoint_4xx_without_code_is_a_credentials_error():
    err, _ = token_error(FakeResponse(404, '404 page not found'))
    assert (err.kind, err.code) == ('credentials', None)
    assert 'HTTP 404' in str(err)


@pytest.mark.parametrize('reply', [
    FakeResponse(429, {'code': 99991400, 'msg': 'request trigger frequency limit'}),
    FakeResponse(500, 'Internal Server Error'),
    {'code': 0, 'msg': 'ok', 'expire': 7200},                       # 缺 tenant_access_token
    {'code': 0, 'msg': 'ok', 'tenant_access_token': 't-1'},         # 缺 expire
    FakeResponse(200, 'not json'),
])
def test_token_endpoint_overload_or_an_odd_success_is_transient(reply):
    err, session = token_error(reply)
    assert err.kind == 'transient'
    assert session.to(MESSAGES) == []


@pytest.mark.parametrize('reply', [
    FakeResponse(429, {'code': 99991400, 'msg': 'request trigger frequency limit'}),
    FakeResponse(400, {'code': 99991400, 'msg': 'request trigger frequency limit'}),   # 部分旧接口频控回 400
    FakeResponse(400, {'code': 230020, 'msg': 'rate limit'}),
    FakeResponse(429, 'Too Many Requests'),
    FakeResponse(500, {'code': 1, 'msg': 'internal error'}),
    FakeResponse(502, '<html>Bad Gateway</html>'),
    FakeResponse(503, {}),
    FakeResponse(200, 'not json'),                                  # 2xx 却没有 code：不是飞书的应答
    FakeResponse(200, {'msg': 'no code'}),
    FakeResponse(200, [1, 2]),
])
def test_rate_limits_server_errors_and_bodies_without_code_are_transient(reply):
    err, session = send_error(reply)
    assert err.kind == 'transient'
    assert len(session.to(TOKEN)) == 1                              # 不是 token 问题，不刷新
    assert len(session.to(MESSAGES)) == 1


@pytest.mark.parametrize('status, body, code', [
    (400, {'code': 230002, 'msg': 'Bot is not in the chat'}, 230002),
    (400, {'code': 230013, 'msg': 'Bot has NO availability to this user.'}, 230013),
    (400, {'code': 99991672, 'msg': 'Access denied. One of the following scopes is required'}, 99991672),
    (200, {'code': 230001, 'msg': 'invalid param'}, 230001),          # 以 code 为准，不看 HTTP 200
    (404, '404 page not found', None),                               # 没有 code 的 4xx
])
def test_other_failures_are_rejected_without_a_refresh(status, body, code):
    err, session = send_error(FakeResponse(status, body))
    assert (err.kind, err.code) == ('rejected', code)
    assert err.msg == (body['msg'] if isinstance(body, dict) else '')
    assert len(session.to(TOKEN)) == 1
    assert len(session.to(MESSAGES)) == 1
    assert f'HTTP {status}' in str(err)
    assert 't-1' not in str(err)                                    # token 不进异常消息


def test_an_upload_success_without_image_key_is_transient():
    session = FakeSession().on(TOKEN, token_reply()).on(IMAGES, {'code': 0, 'msg': 'success', 'data': {}})
    with pytest.raises(FeishuError) as info:
        make_client(session).upload_image(PNG)
    assert info.value.kind == 'transient'


@pytest.mark.parametrize('reply', [
    {'code': 10014, 'msg': 'app id not exists'},
    FakeResponse(502, 'Bad Gateway'),
    requests.ConnectionError('connection refused'),
])
def test_the_app_secret_never_shows_in_an_error_a_log_or_a_repr(reply, log):
    session = FakeSession().on(TOKEN, reply)
    client = make_client(session)
    with pytest.raises(FeishuError) as info:
        client.tenant_token()
    texts = [str(info.value), repr(info.value), repr(client)] + [message for _, message in log.records]
    assert not any(SECRET in text for text in texts)
    assert session.calls[0]['json']['app_secret'] == SECRET        # 只进取 token 的请求体


# ---- 客户端：群列表 ------------------------------------------------------------------------------------------

def test_list_chats_follows_page_token_until_has_more_is_false():
    page1 = {'code': 0, 'data': {'items': [{'chat_id': 'oc_1', 'name': '群一', 'avatar': 'x', 'owner_id': 'ou_o'}],
                                 'has_more': True, 'page_token': 'p2'}}
    page2 = {'code': 0, 'data': {'items': [{'chat_id': 'oc_2', 'name': '群二'}], 'has_more': False}}
    session = FakeSession().on(TOKEN, token_reply()).on(CHATS, page1, page2)
    assert make_client(session).list_chats() == [{'chat_id': 'oc_1', 'name': '群一'}, {'chat_id': 'oc_2', 'name': '群二'}]
    first, second = session.to(CHATS)
    assert (first['method'], first['url']) == ('GET', BASE + CHATS)
    assert first['params'] == {'page_size': 100}
    assert second['params'] == {'page_size': 100, 'page_token': 'p2'}
    assert first['headers'] == {'Authorization': 'Bearer t-1'}


def test_list_chats_with_no_groups_is_empty():
    session = FakeSession().on(TOKEN, token_reply()).on(CHATS, {'code': 0, 'msg': 'success', 'data': {'has_more': False}})
    assert make_client(session).list_chats() == []


def test_list_chats_stops_after_20_pages(log):
    endless = {'code': 0, 'data': {'items': [{'chat_id': 'oc_n', 'name': 'n'}], 'has_more': True, 'page_token': 'next'}}
    session = FakeSession().on(TOKEN, token_reply()).on(CHATS, endless)
    chats = make_client(session).list_chats()
    assert len(session.to(CHATS)) == 20
    assert len(chats) == 20
    assert len(log.messages('warning')) == 1


def test_list_chats_stops_when_has_more_comes_without_a_page_token(log):
    odd = {'code': 0, 'data': {'items': [{'chat_id': 'oc_1', 'name': '群一'}], 'has_more': True}}
    session = FakeSession().on(TOKEN, token_reply()).on(CHATS, odd)
    assert make_client(session).list_chats() == [{'chat_id': 'oc_1', 'name': '群一'}]
    assert len(session.to(CHATS)) == 1
    assert len(log.messages('warning')) == 1


# ---- 发件箱 ------------------------------------------------------------------------------------------------

def test_outbox_fail_sets_the_entry_aside(tmp_path):
    outbox = Outbox(tmp_path)
    p = outbox.put({'n': 1})
    outbox.fail(p)
    assert not p.exists()
    assert json.loads((tmp_path / (p.name + '.failed')).read_text(encoding='utf-8')) == {'n': 1}
    assert outbox.pending() == []


def test_outbox_pending_skips_bad_failed_and_temporary_files_oldest_first(tmp_path):
    outbox = Outbox(tmp_path)
    entries = [outbox.put({'n': i}) for i in range(3)]
    for name in ['x.json.bad', 'y.json.failed', 'tmpk3j2h1ab', 'notes.txt']:
        (tmp_path / name).write_text('{}', encoding='utf-8')
    (tmp_path / 'folder.json').mkdir()
    assert outbox.pending() == entries


def test_outbox_names_are_unique_ordered_and_fit_a_feishu_uuid(tmp_path):
    outbox = Outbox(tmp_path)
    paths = [outbox.put({'n': i}) for i in range(50)]
    assert len(set(paths)) == 50
    assert sorted(paths, key=lambda p: p.name) == paths            # 同一毫秒里连写也按先后排
    assert max(len(p.stem) for p in paths) <= 50
    assert outbox.pending() == paths


def test_outbox_without_a_directory_is_empty_and_creates_nothing(tmp_path):
    outbox = Outbox(tmp_path / 'missing')
    assert outbox.pending() == []
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('raw', [b'', b'{"a": 1', b'[1, 2]', b'"text"', b'null', b'\xff\xfe\x00garbage'])
def test_outbox_quarantines_anything_that_is_not_a_json_object(tmp_path, raw, log):
    p = tmp_path / '0000000000001-1-x.json'
    p.write_bytes(raw)
    assert Outbox(tmp_path).load(p) is None
    assert [q.name for q in tmp_path.iterdir()] == [p.name + '.bad']
    assert len(log.messages('warning')) == 1


def test_outbox_tolerates_an_entry_another_process_already_handled(tmp_path, log):
    outbox = Outbox(tmp_path)
    p = outbox.put({'n': 1})
    p.unlink()                                                      # 另一个进程已经发完删掉了
    assert outbox.load(p) is None
    outbox.done(p)
    outbox.fail(p)
    assert list(tmp_path.iterdir()) == []                           # 没生出 .bad 或 .failed
    assert log.records == []


def test_outbox_skips_an_entry_it_cannot_read_right_now(tmp_path, log):
    outbox = Outbox(tmp_path)
    p = tmp_path / '0000000000001-1-x.json'
    p.mkdir()                                                       # 读目录必抛 OSError（Windows 上为 PermissionError）
    assert outbox.load(p) is None
    assert p.is_dir()                                               # 不是损坏，不改名
    assert len(log.messages('warning')) == 1


def test_outbox_put_refuses_what_json_cannot_hold(tmp_path):
    outbox = Outbox(tmp_path / 'box')
    with pytest.raises(TypeError):
        outbox.put(['not', 'a', 'dict'])
    with pytest.raises(TypeError):
        outbox.put({'png': b'raw bytes'})                           # 图片要先编码成文本
    assert not (tmp_path / 'box').exists()                          # 先序列化，出错时不碰磁盘


# ---- 合并器 ------------------------------------------------------------------------------------------------

def test_aggregator_expired_window_is_still_summarised(tmp_path):
    agg = Aggregator(tmp_path / 'agg.json', 10)
    assert agg.admit('k', at(10, 0)) is True
    assert agg.admit('k', at(10, 5)) is False
    assert agg.admit('k', at(10, 30)) is True                       # 旧窗口已结束，还没汇总：新开窗口
    assert agg.due_summaries(at(10, 31)) == [('k', 1)]              # 旧窗口的计数照样交出来
    assert agg.admit('k', at(10, 32)) is False
    assert agg.due_summaries(at(10, 41)) == [('k', 1)]
    assert agg.due_summaries(at(10, 42)) == []


def test_aggregator_meta_is_kept_after_the_summary_until_the_next_one(tmp_path):
    agg = Aggregator(tmp_path / 'agg.json', 10)
    first = {'title': 'OAS1 探索', 'receive_id_type': 'open_id', 'receive_id': 'ou_x', 'window': 10}
    assert agg.admit('k', at(10, 0), first) is True
    assert agg.admit('k', at(10, 5), {'title': 'later'}) is False
    assert agg.meta('k') == first                                   # 窗口里存的是第一条的
    assert agg.due_summaries(at(10, 11)) == [('k', 1)]
    assert agg.meta('k') == first                                   # 清零后仍在，供补发汇总用
    assert agg.due_summaries(at(10, 12)) == []
    assert agg.meta('k') is None                                    # 下一次 due_summaries 才丢掉
    assert agg.meta('unknown') is None


def test_aggregator_judges_each_window_by_its_stored_end(tmp_path):
    path = tmp_path / 'agg.json'
    instance, server = Aggregator(path, 10), Aggregator(path, 1)    # 主进程不知道实例的窗口多长
    assert instance.admit('k', at(10, 0)) is True
    assert instance.admit('k', at(10, 5)) is False
    assert server.due_summaries(at(10, 6)) == []
    assert server.due_summaries(at(10, 10)) == [('k', 1)]          # 到 end 那一刻窗口就结束了


def test_aggregator_drops_a_window_without_repeats_silently(tmp_path):
    path = tmp_path / 'agg.json'
    agg = Aggregator(path, 10)
    assert agg.admit('k', at(10, 0)) is True
    assert agg.due_summaries(at(10, 11)) == []
    assert agg.meta('k') is None
    assert json.loads(path.read_text(encoding='utf-8')) == {}


def test_aggregator_creates_no_file_until_something_is_admitted(tmp_path):
    path = tmp_path / 'log' / 'feishu_aggregate.json'
    agg = Aggregator(path, 10)
    assert agg.due_summaries(at(10, 0)) == []
    assert agg.meta('k') is None
    assert not (tmp_path / 'log').exists()
    assert agg.admit('k', at(10, 0)) is True
    assert path.is_file()                                           # 父目录按需建


def test_a_clock_moved_back_closes_the_window(tmp_path):
    agg = Aggregator(tmp_path / 'agg.json', 10)
    assert agg.admit('k', at(10, 0)) is True
    assert agg.admit('k', at(10, 1)) is False
    assert agg.admit('k', at(9, 0)) is True                         # 时钟往回拨了一小时：不在窗口里，重开
    assert agg.due_summaries(at(9, 1)) == [('k', 1)]


@pytest.mark.parametrize('raw', [
    '{oops',
    '[]',
    '{"k": {"start": "not a time"}}',
    '{"k": {"start": "2026-11-21T10:00:00", "end": "2026-11-21T10:10:00", "count": "1", "pending": 0, "meta": null}}',
])
def test_a_corrupt_aggregate_state_counts_as_empty(tmp_path, raw, log):
    path = tmp_path / 'agg.json'
    path.write_text(raw, encoding='utf-8')
    agg = Aggregator(path, 10)
    assert agg.admit('k', at(10, 5)) is True
    assert len(log.messages('warning')) == 1
    assert agg.admit('k', at(10, 6)) is False                       # 状态已经重写成好的
    assert len(log.messages('warning')) == 1


def test_an_unreadable_aggregate_state_lets_the_message_through_and_is_not_overwritten(tmp_path, monkeypatch, log):
    path = tmp_path / 'agg.json'
    agg = Aggregator(path, 10)
    assert agg.admit('a', at(10, 0)) is True
    assert agg.admit('a', at(10, 1)) is False
    before = path.read_text(encoding='utf-8')
    real_read_text = Path.read_text

    def locked(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, '另一个程序正在使用此文件')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', locked)
    assert agg.admit('b', at(10, 2)) is True                        # 读不了：放行
    assert agg.due_summaries(at(10, 11)) == []
    assert agg.meta('a') is None
    monkeypatch.setattr(Path, 'read_text', real_read_text)
    assert path.read_text(encoding='utf-8') == before               # 没拿空状态盖掉 a 的窗口
    assert agg.due_summaries(at(10, 11)) == [('a', 1)]
    assert len(log.messages('warning')) == 3


def test_when_the_state_cannot_be_written_messages_go_through_and_summaries_wait(tmp_path, monkeypatch, log):
    agg = Aggregator(tmp_path / 'agg.json', 10)
    assert agg.admit('k', at(10, 0)) is True
    assert agg.admit('k', at(10, 1)) is False                       # 计数 1，已写进文件
    broken = {'on': True}
    real_atomic_write = feishu_app.atomic_write

    def flaky(*args, **kwargs):
        if broken['on']:
            raise OSError(28, 'No space left on device')
        return real_atomic_write(*args, **kwargs)

    monkeypatch.setattr(feishu_app, 'atomic_write', flaky)
    assert agg.admit('k', at(10, 2)) is True                        # 记不下这次计数：宁可多发
    assert agg.due_summaries(at(10, 11)) == []                      # 清不了零：这次先不交，免得下次重复
    broken['on'] = False
    assert agg.due_summaries(at(10, 12)) == [('k', 1)]
    assert len(log.messages('warning')) == 2


def test_aggregator_counts_every_concurrent_admit_in_one_process(tmp_path):
    path = tmp_path / 'agg.json'
    aggregators = [Aggregator(path, 10), Aggregator(path, 10)]     # 两个对象共用一个文件，也共用一把锁
    results = []

    def worker(agg):
        for _ in range(10):
            results.append(agg.admit('k', at(10, 0)))

    threads = [threading.Thread(target=worker, args=(aggregators[i % 2],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(results) == 40
    assert results.count(True) == 1
    assert aggregators[0].due_summaries(at(10, 10)) == [('k', 39)]
