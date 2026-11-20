"""飞书自建应用推送：HTTP 客户端（FeishuClient）、发件箱（Outbox）、同类错误合并器（Aggregator）。

导入本模块不联网，也不建文件和目录（module.logger 自己导入时会打开日志文件，与其他模块相同）。只用已有依赖 requests。

飞书开放平台接口，2026-11-21 按 open.feishu.cn 官方文档核对，错误响应用假凭证实测过：
- 取 token：POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal，JSON {app_id, app_secret}，
  返回 {code, msg, tenant_access_token, expire}。expire 是剩余秒数，最长 7200；剩余不足 30 分钟时再取，才会拿到新 token。
  凭证错误回 HTTP 200 加非 0 code（实测 10003、10014；文档另列 10013、10015）。本接口不需要权限。
  文档已把它标为"历史版本，不推荐使用"，新版是 POST https://accounts.feishu.cn/oauth/v3/token（OAuth client
  credentials，返回 JWT）。官方 Python、Go SDK 取 tenant token 仍用旧接口，这里沿用；改用新版时，请求、响应字段和
  刷新触发码都要重写。
- 上传图片：POST /open-apis/im/v1/images，multipart 两个字段 image_type=message、image=<文件>，取 data.image_key；
  不超过 10 MB，不能是 0 字节。
- 发消息：POST /open-apis/im/v1/messages?receive_id_type=open_id|chat_id，JSON {receive_id, msg_type: "post",
  content: <JSON 字符串>}，可带 uuid（不超过 50 字符，同一 uuid 1 小时内只发出一条）。content 为
  {"zh_cn": {"title": ..., "content": [[{"tag": "text", "text": ...}], ..., [{"tag": "img", "image_key": ...}]]}}：
  语言层必需，图片独占一段，富文本请求体不超过 30 KB（超出回 230025）。请求体经 requests 的 json= 序列化（非 ASCII
  转成 \\uXXXX），算长度时按序列化后的字节数（推断：文档没写按哪种编码算）。
- 机器人所在的群：GET /open-apis/im/v1/chats?page_size=<最大 100>&page_token=...，不含单聊；
  返回 data.items[].chat_id、data.items[].name、data.has_more、data.page_token。
- 出错：响应体为 {code, msg, error: {...}}，以 code 判断。token 错误回 HTTP 400：无效或过期 99991663（实测），
  没带 token 99991661（实测），不以 t- 开头 99991668（实测），另有 99991664、99991665、99991671（文档）。
  频控回 HTTP 429 加 code 99991400（部分旧接口回 400），发消息接口另有 230020。缺权限是 99991672，换 token 没用。
- 权限（每个接口任选一项）：发消息 im:message:send_as_bot；上传图片 im:resource 或 im:resource:upload；
  群列表 im:chat:readonly。另外要开启机器人能力，并发布版本。

失败一律抛 FeishuError，kind 分四类。先看响应体的 code，再看 HTTP 状态码：
- network：requests 抛了异常（连接失败、超时等）。
- transient：HTTP 5xx、HTTP 429、频控码 99991400 / 230020、换过 token 重发后仍是 token 错误、code 为 0 却缺必要字段，
  以及 HTTP 2xx 却没有 code 的响应（不是飞书的应答）。
- credentials：取 token 的接口回了其余非 0 code，或回了没有 code 的 4xx。
- rejected：其他接口回了其余非 0 code，或回了没有 code 的 4xx。
带 token 的请求遇到 token 错误码时，作废缓存的 token，重新取一个，原请求重发一次。每个请求都带超时 (5, 10) 秒。
异常消息只含接口名、HTTP 状态码、code、msg 和网络异常的说明，不含 app secret 和 token。
"""
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

import requests

from module.config.atomicwrites import atomic_write
from module.logger import logger

TOKEN_PATH = '/open-apis/auth/v3/tenant_access_token/internal'
IMAGES_PATH = '/open-apis/im/v1/images'
MESSAGES_PATH = '/open-apis/im/v1/messages'
CHATS_PATH = '/open-apis/im/v1/chats'

TIMEOUT = (5, 10)                      # 秒：(连接, 读)。报错的实例推送完马上退出，不能被一个请求拖住太久
TOKEN_MARGIN = 60                      # 秒：token 离过期不到这么久就不再用，重新取
TOKEN_ERROR_CODES = frozenset({99991661, 99991663, 99991664, 99991665, 99991668, 99991671})
RATE_LIMIT_CODES = frozenset({99991400, 230020})
UUID_MAX_LENGTH = 50
CHAT_PAGE_SIZE = 100
CHAT_PAGE_LIMIT = 20                   # 群列表最多翻这么多页
JSON_HEADERS = {'Content-Type': 'application/json; charset=utf-8'}


class FeishuError(Exception):
    """飞书接口调用失败。

    kind 是失败的类别：'network'、'transient'、'credentials'、'rejected'，含义见模块文档。
    code、msg 是飞书响应里的原值，响应里没有就是 None、''。
    """

    def __init__(self, kind: str, message: str, code: int | None = None, msg: str = ''):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.msg = msg


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse(resp) -> tuple[dict, int | None, str]:
    """取响应体（不是 JSON 对象时为空 dict）、code（不是整数时为 None）和 msg（不是字符串时为 ''）。"""
    try:
        body = resp.json()
    except ValueError:                 # requests 的 JSONDecodeError 也是 ValueError 的子类
        body = None
    if not isinstance(body, dict):
        return {}, None, ''
    code, msg = body.get('code'), body.get('msg')
    return body, (code if _is_int(code) else None), (msg if isinstance(msg, str) else '')


def _data(body: dict) -> dict:
    data = body.get('data')
    return data if isinstance(data, dict) else {}


def _failure_kind(status: int, code: int | None, *, token_endpoint: bool) -> str:
    """code 不为 0 的响应属于哪类失败（token 错误码由调用方先处理）。"""
    if code in RATE_LIMIT_CODES or status == 429 or status >= 500:
        return 'transient'
    if code is None and not 400 <= status < 500:
        return 'transient'             # 2xx 却没有 code：不是飞书的应答，留着下次再试
    return 'credentials' if token_endpoint else 'rejected'


def _describe(action: str, kind: str, status: int, code: int | None, msg: str) -> str:
    detail = f'code={code}，msg={msg}' if code is not None else '响应里没有 code'
    return f'飞书{action}失败（{kind}）：HTTP {status}，{detail}'


class FeishuClient:
    """飞书开放平台的最小客户端：取 token（缓存）、上传图片、发富文本消息、列机器人所在的群。

    session 是 requests 模块，或任何有同样 post / get 的对象（测试注入假的）；只用到 json、data、files、params、
    headers、timeout 这几个参数。clock 返回单调时钟的秒数，只用来判断 token 是否快过期。
    app secret 只放进取 token 的请求体，不进异常消息，也不进 repr。
    """

    def __init__(self, app_id: str, app_secret: str, session=requests, base: str = 'https://open.feishu.cn', *,
                 clock: Callable[[], float] = time.monotonic):
        self.app_id = app_id
        self._app_secret = app_secret
        self.session = session
        self.base = base.rstrip('/')
        self._clock = clock
        self._token: str | None = None
        self._token_deadline = 0.0

    def tenant_token(self) -> str:
        """返回 tenant_access_token。缓存到过期前 TOKEN_MARGIN 秒（expire 是取到时的剩余秒数），之后重新取。"""
        token = self._token            # 只读一次：别的线程可能正把它作废成 None
        if token is not None and self._clock() < self._token_deadline:
            return token
        self._token = None
        resp = self._send('取 token', 'post', TOKEN_PATH,
                          json={'app_id': self.app_id, 'app_secret': self._app_secret}, headers=JSON_HEADERS)
        body, code, msg = _parse(resp)
        if code != 0:
            kind = _failure_kind(resp.status_code, code, token_endpoint=True)
            raise FeishuError(kind, _describe('取 token', kind, resp.status_code, code, msg), code, msg)
        token, expire = body.get('tenant_access_token'), body.get('expire')
        if not isinstance(token, str) or not token or not _is_int(expire):
            raise FeishuError('transient', '飞书取 token 失败（transient）：响应缺少 tenant_access_token 或 expire',
                              code, msg)
        self._token_deadline = self._clock() + expire - TOKEN_MARGIN
        self._token = token
        return token

    def upload_image(self, png: bytes) -> str:
        """上传一张发消息用的图片，返回 image_key。"""
        body = self._call('上传图片', 'post', IMAGES_PATH, data={'image_type': 'message'},
                          files={'image': ('image.png', png, 'image/png')})
        image_key = _data(body).get('image_key')
        if not isinstance(image_key, str) or not image_key:
            raise FeishuError('transient', '飞书上传图片失败（transient）：响应缺少 data.image_key', 0, '')
        return image_key

    def send_post(self, receive_id_type: str, receive_id: str, title: str, lines: list[str],
                  image_key: str | None = None, uuid: str | None = None) -> None:
        """发一条富文本消息：lines 每行一段，image_key 有值时图片独占最后一段。

        uuid 用于去重（同一 uuid 1 小时内只发出一条），超过 50 个字符抛 ValueError，不发请求。
        """
        if uuid is not None and len(uuid) > UUID_MAX_LENGTH:
            raise ValueError(f'uuid 不能超过 {UUID_MAX_LENGTH} 个字符，收到 {len(uuid)} 个')
        paragraphs = [[{'tag': 'text', 'text': line}] for line in lines]
        if image_key:
            paragraphs.append([{'tag': 'img', 'image_key': image_key}])
        content = {'zh_cn': {'title': title, 'content': paragraphs}}
        payload = {'receive_id': receive_id, 'msg_type': 'post', 'content': json.dumps(content, ensure_ascii=False)}
        if uuid:
            payload['uuid'] = uuid
        self._call('发消息', 'post', MESSAGES_PATH, params={'receive_id_type': receive_id_type}, json=payload,
                   headers=JSON_HEADERS)

    def list_chats(self) -> list[dict]:
        """机器人所在的群，[{'chat_id', 'name'}]。按 page_token 翻页到 has_more 为假，最多 CHAT_PAGE_LIMIT 页。"""
        chats: list[dict] = []
        params: dict = {'page_size': CHAT_PAGE_SIZE}
        for _ in range(CHAT_PAGE_LIMIT):
            data = _data(self._call('列群', 'get', CHATS_PATH, params=dict(params)))
            items = data.get('items')
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get('chat_id'):
                    chats.append({'chat_id': item['chat_id'], 'name': item.get('name') or ''})
            if not data.get('has_more'):
                return chats
            if not data.get('page_token'):
                logger.warning(f'飞书群列表说还有下一页，却没给 page_token，只返回前 {len(chats)} 个群')
                return chats
            params['page_token'] = data['page_token']
        logger.warning(f'飞书群列表超过 {CHAT_PAGE_LIMIT} 页，只返回前 {len(chats)} 个群')
        return chats

    def _call(self, action: str, method: str, path: str, *, headers: dict | None = None, **kwargs) -> dict:
        """带 token 调一次接口，返回 code 为 0 的响应体；失败抛 FeishuError。

        遇到 token 错误码时作废缓存的 token，重新取一个，原请求重发一次；重发后仍是 token 错误，按 transient 抛出。
        """
        resp, body, code, msg = self._send_with_token(action, method, path, headers, kwargs)
        if code in TOKEN_ERROR_CODES:
            self._token = None
            resp, body, code, msg = self._send_with_token(action, method, path, headers, kwargs)
        if code == 0:
            return body
        if code in TOKEN_ERROR_CODES:
            self._token = None
            kind = 'transient'
        else:
            kind = _failure_kind(resp.status_code, code, token_endpoint=False)
        raise FeishuError(kind, _describe(action, kind, resp.status_code, code, msg), code, msg)

    def _send_with_token(self, action: str, method: str, path: str, headers: dict | None, kwargs: dict):
        headers = {**(headers or {}), 'Authorization': f'Bearer {self.tenant_token()}'}
        resp = self._send(action, method, path, headers=headers, **kwargs)
        return (resp, *_parse(resp))

    def _send(self, action: str, method: str, path: str, **kwargs):
        """发一个请求（带超时），返回响应；requests 抛的异常转成 kind 为 network 的 FeishuError。"""
        try:
            return getattr(self.session, method)(self.base + path, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise FeishuError('network', f'飞书{action}失败（network）：{type(exc).__name__}: {exc}') from exc


_stamp_lock = threading.Lock()
_last_stamp = 0


def _next_stamp() -> int:
    """当前的毫秒时间戳。同一进程里严格递增：同一毫秒里连写也不重名、不乱序。"""
    global _last_stamp
    with _stamp_lock:
        _last_stamp = max(time.time_ns() // 1_000_000, _last_stamp + 1)
        return _last_stamp


class Outbox:
    """发件箱：每条消息一个 JSON 文件，发送成功后删掉。进程写下就退出的消息，由别的进程补发。

    消息的字段由调用方定，这里只要求是能存成 JSON 的 dict（bytes 之类要先编码成文本）。
    文件名为 <13 位毫秒时间戳>-<进程号>-<12 位随机十六进制>.json：跨进程不重名；按名字排序就是写入的先后，
    同一进程里时间戳严格递增；去掉 .json 后不超过 50 个字符，可以直接当 send_post 的 uuid。
    写入走 atomic_write：先写同目录的临时文件（tmp 开头，没有 .json 后缀），再改名，读的一方看不到写了一半的文件。

    - put：目录不存在时先建。先序列化，存不成 JSON 时抛 TypeError，不碰磁盘；写盘出错照常抛 OSError。
    - pending：只列 *.json 文件，跳过 .bad、.failed 和临时文件；目录不存在时返回空列表，也不建目录。
    - load：内容损坏（不是 UTF-8、不是 JSON、不是对象）时改名为 <原名>.bad，记一条 WARNING，返回 None。
      文件已被别的进程处理掉（不存在）时返回 None；暂时读不了时记 WARNING、返回 None，文件留着下次再读。
    - done：删掉文件，文件已不在不算错。fail：改名为 <原名>.failed 留着备查，文件已不在不算错。
      其他 OSError 照常抛出。
    """

    def __init__(self, dir: Path):
        self.dir = Path(dir)

    def put(self, msg: dict) -> Path:
        if not isinstance(msg, dict):
            raise TypeError(f'发件箱只收 dict，收到的是 {type(msg).__name__}')
        text = json.dumps(msg, ensure_ascii=False)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f'{_next_stamp():013d}-{os.getpid()}-{uuid4().hex[:12]}.json'
        with atomic_write(path, overwrite=True, encoding='utf-8', newline='') as f:
            f.write(text)
        return path

    def pending(self) -> list[Path]:
        return sorted((p for p in self.dir.glob('*.json') if p.is_file()), key=lambda p: p.name)

    def load(self, p: Path) -> dict | None:
        p = Path(p)
        try:
            msg = json.loads(p.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning(f'飞书发件箱：{p.name} 暂时读不了（{exc!r}），这次跳过')
            return None
        except ValueError as exc:      # UnicodeDecodeError、JSONDecodeError 都是 ValueError 的子类
            self._quarantine(p, repr(exc))
            return None
        if not isinstance(msg, dict):
            self._quarantine(p, f'内容是 {type(msg).__name__}，不是对象')
            return None
        return msg

    def done(self, p: Path) -> None:
        Path(p).unlink(missing_ok=True)

    def fail(self, p: Path) -> None:
        p = Path(p)
        try:
            os.replace(p, p.with_name(p.name + '.failed'))
        except FileNotFoundError:
            pass

    @staticmethod
    def _quarantine(p: Path, why: str) -> None:
        target = p.with_name(p.name + '.bad')
        try:
            os.replace(p, target)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(f'飞书发件箱：{p.name} 已损坏（{why}），改名为 {target.name} 也失败了（{exc!r}）')
            return
        logger.warning(f'飞书发件箱：{p.name} 已损坏（{why}），改名为 {target.name}')


@dataclass
class _Window:
    """一个 key 的合并窗口 [start, end)。count：放行第一条之后又来了几条；pending：之前已结束、还没交出去的计数；
    meta：开窗那一条带的附加信息。"""
    start: datetime
    end: datetime
    count: int = 0
    pending: int = 0
    meta: dict | None = None

    def is_open(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def to_json(self) -> dict:
        return {'start': self.start.isoformat(), 'end': self.end.isoformat(), 'count': self.count,
                'pending': self.pending, 'meta': self.meta}

    @classmethod
    def from_json(cls, item: dict) -> '_Window':
        count, pending, meta = item['count'], item['pending'], item['meta']
        if not (_is_int(count) and count >= 0 and _is_int(pending) and pending >= 0):
            raise ValueError('count、pending 不是非负整数')
        if meta is not None and not isinstance(meta, dict):
            raise ValueError('meta 不是对象')
        return cls(datetime.fromisoformat(item['start']), datetime.fromisoformat(item['end']), count, pending, meta)


class Aggregator:
    """同类错误合并：同一个 key 在窗口内只放行第一条，之后只计数；窗口结束后由 due_summaries 交出计数，用来补发汇总。

    状态文件由实例进程和主进程共用。每个 key 存窗口的起止时刻、计数、待交的计数和开窗那一条的 meta（要能存成 JSON）。
    每次读改写都在进程内的锁里做（本类的所有对象共用一把），写入用 atomic_write 整个替换。不加跨进程锁：两个进程
    同一瞬间改同一个文件时后写的覆盖先写的，最多让汇总少算一次，或多放行一条。

    - 文件不存在按空处理，什么都不交时也不建文件；内容损坏记一条 WARNING，按空处理，下次写入时覆盖。
    - 暂时读不了（比如 Windows 上正赶上别的进程替换文件）记 WARNING，这次不写，免得拿空状态盖掉别的 key：
      admit 放行，due_summaries 返回空，meta 返回 None。
    - 写不进去记 WARNING：admit 放行（宁可多发，不漏发），due_summaries 这次返回空（计数还在文件里，下次再交，
      不会重复汇总）。

    窗口是 [start, end)，now 落在里面才算窗口内；时钟往回拨到 start 之前，也算窗口已结束。
    window_minutes 不大于 0 时窗口为空，等于不合并。now 一律用本机的 naive 时间（datetime.now()）。
    - admit(key, now, meta)：窗口内计数加一，返回 False；否则新开窗口（end = now + window_minutes），存下 meta，
      返回 True。旧窗口结束时还有计数，就记进待交的计数，下一次 due_summaries 照样交出来。
    - due_summaries(now)：每个 key 交出待交的计数，窗口已结束的再加上窗口内的计数；合计大于 0 的按 (key, 合计)
      列出并清零，已结束而合计为 0 的窗口直接删掉。窗口是否结束看状态里存的 end，不看本对象的 window_minutes：
      主进程不知道各实例的窗口多长。
    - meta(key)：开窗那一条的 meta。清零后仍保留，到下一次 due_summaries 删掉这个已结束、计数为 0 的窗口为止。
    """

    _lock = threading.Lock()

    def __init__(self, state_path: Path, window_minutes: int):
        self.state_path = Path(state_path)
        self.window = timedelta(minutes=window_minutes)

    def admit(self, key: str, now: datetime, meta: dict | None = None) -> bool:
        with self._lock:
            windows = self._read()
            if windows is None:
                return True
            current = windows.get(key)
            if current is not None and current.is_open(now):
                current.count += 1
                admitted = False
            else:
                carried = 0 if current is None else current.pending + current.count
                windows[key] = _Window(now, now + self.window, 0, carried, meta)
                admitted = True
            if not self._write(windows):
                return True
            return admitted

    def due_summaries(self, now: datetime) -> list[tuple[str, int]]:
        with self._lock:
            windows = self._read()
            if windows is None:
                return []
            due: list[tuple[str, int]] = []
            changed = False
            for key, window in list(windows.items()):
                ended = not window.is_open(now)
                total = window.pending + (window.count if ended else 0)
                if total > 0:
                    due.append((key, total))
                    window.pending = 0
                    if ended:
                        window.count = 0       # 清零但留着 meta，下一次 due_summaries 再删
                    changed = True
                elif ended:
                    del windows[key]
                    changed = True
            if changed and not self._write(windows):
                return []
            return due

    def meta(self, key: str) -> dict | None:
        with self._lock:
            windows = self._read()
        window = None if windows is None else windows.get(key)
        return None if window is None else window.meta

    def _read(self) -> dict[str, _Window] | None:
        """读状态：不存在或损坏时为空 dict，暂时读不了时为 None。"""
        try:
            raw = json.loads(self.state_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning(f'飞书合并状态暂时读不了（{self.state_path}）：{exc!r}，这次不合并')
            return None
        except ValueError as exc:
            logger.warning(f'飞书合并状态已损坏（{self.state_path}）：{exc!r}，按空处理')
            return {}
        try:
            return {key: _Window.from_json(item) for key, item in raw.items()}
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            logger.warning(f'飞书合并状态已损坏（{self.state_path}）：{exc!r}，按空处理')
            return {}

    def _write(self, windows: dict[str, _Window]) -> bool:
        text = json.dumps({key: window.to_json() for key, window in windows.items()}, ensure_ascii=False, indent=2)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic_write(self.state_path, overwrite=True, encoding='utf-8', newline='') as f:
                f.write(text)
        except OSError as exc:
            logger.warning(f'飞书合并状态写不进去（{self.state_path}）：{exc!r}')
            return False
        return True
