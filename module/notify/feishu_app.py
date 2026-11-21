"""飞书自建应用推送：HTTP 客户端（FeishuClient）、发件箱（Outbox）、同类错误合并器（Aggregator），以及接在现有
推送配置上的通知器（FeishuAppNotifier）、主进程补发（flush_outbox）和实例启动时的登记与自检（on_script_loop_start）。

导入本模块不联网，也不建文件和目录（module.logger 自己导入时会打开日志文件，与其他模块相同）。
只用已有依赖：requests、PyYAML、Pillow。

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
异常消息只含接口名、HTTP 状态码、code、msg 和网络异常的说明，不含 app secret 和 token。网络异常不串联 requests 的
原异常；本模块各帧的局部变量里也不留 app secret 和 token。项目日志会渲染 traceback 里的局部变量，记日志的规矩见 FeishuError。

通知器：推送配置（OASX 里实例的 脚本 → 出错设置 → 消息推送配置）写 provider: feishu_app 时，notify.Notifier 把 push
转交给 FeishuAppNotifier。
- 凭证只放 .env（FEISHU_APP_ID、FEISHU_APP_SECRET），用的时候才读，不存在对象上，不进日志、发件箱和 repr。
- 每条消息先写进发件箱 log/feishu_outbox/，再试着立即发送：报错的实例推送完马上 exit(1)，没发出去的由主进程每 60 秒
  调 flush_outbox 补发。发送时带条目的 id（文件名去掉 .json）作 uuid，响应丢了再发，飞书 1 小时内也只发出一条。
- 实例进程（on_script_loop_start 登记过截图来源的进程）合并同类错误：同一实例、标题和内容相同的报错在 aggregate_minutes
  分钟内只发第一条，窗口结束后由 flush_outbox 补发一条汇总，发给窗口里第一条的接收方。主进程的测试推送不合并。
- 一条消息：标题，然后每行一段：错误摘要、正在跑的编排行、日志最后 30 行（脱敏），最后是截图。富文本请求体不超过
  30 KB，超了从最早的日志行删起。
"""
import base64
import io
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import requests
import yaml
from PIL import Image

from module.config.atomicwrites import atomic_write
from module.config.dotenv import read_env
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

    记日志只记 str(e)，不带 traceback（不用 logger.exception，也不传 exc_info）：项目日志的 handler 开着
    tracebacks_show_locals，会把 traceback 里每一帧的局部变量写进日志。本模块的帧已保证不含 app secret 和 token，
    调用方的帧未必（比如读进局部变量的 FEISHU_APP_SECRET）。本模块抛出的其他意外异常也照此处理。
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
        """返回 tenant_access_token。缓存到过期前 TOKEN_MARGIN 秒（expire 是取到时的剩余秒数），之后重新取。

        本帧不留 token 局部变量：重新取的时候抛异常，这一帧也在 traceback 里。
        """
        return self._cached_token() or self._fetch_token()

    def _cached_token(self) -> str | None:
        """缓存里还没到期的 token，没有就是 None。self._token 只读一次：别的线程可能正把它作废成 None。"""
        token = self._token
        return token if token is not None and self._clock() < self._token_deadline else None

    def _fetch_token(self) -> str:
        self._token = None
        resp = self._send('取 token', 'post', TOKEN_PATH,
                          json={'app_id': self.app_id, 'app_secret': self._app_secret}, headers=JSON_HEADERS)
        body, code, msg = _parse(resp)
        if code != 0:
            kind = _failure_kind(resp.status_code, code, token_endpoint=True)
            raise FeishuError(kind, _describe('取 token', kind, resp.status_code, code, msg), code, msg)
        token, expire = body.get('tenant_access_token'), body.get('expire')
        if not isinstance(token, str) or not token or not _is_int(expire):
            body = token = None        # 响应里可能有 token：清掉再抛，不留在 traceback 的局部变量里
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
        """带上 token 发一次请求，返回 (响应, 响应体, code, msg)。带 token 的请求头直接写在参数里，不绑定到局部变量：
        _send 抛异常时，这一帧也在 traceback 里。"""
        resp = self._send(action, method, path,
                          headers={**(headers or {}), 'Authorization': f'Bearer {self.tenant_token()}'}, **kwargs)
        return (resp, *_parse(resp))

    def _send(self, action: str, method: str, path: str, **kwargs):
        """发一个请求（带超时），返回响应。requests 抛的异常（连接失败、超时等）换成 kind 为 network 的 FeishuError。

        kwargs 里有取 token 请求体里的 app secret，或请求头里的 token。所以先清空 kwargs，在 except 外面抛出，
        不串联原异常：requests、urllib3 那几帧的局部变量里同样带着请求体和请求头。原异常的类型和说明写进消息。
        """
        try:
            return getattr(self.session, method)(self.base + path, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            failure = f'飞书{action}失败（network）：{type(exc).__name__}: {exc}'
        kwargs.clear()
        raise FeishuError('network', failure)


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


# ---- 通知器：notify.Notifier 遇到 provider: feishu_app 时转交到这里 -------------------------------------------

OUTBOX_DIR = Path('log') / 'feishu_outbox'             # 相对根目录（缺省为当前目录：实例和服务都是仓库根目录）
AGGREGATE_STATE = Path('log') / 'feishu_aggregate.json'
RECEIVE_ID_TYPES = ('open_id', 'chat_id')
DEFAULT_AGGREGATE_MINUTES = 10
MAX_AGGREGATE_MINUTES = 1440                           # 合并窗口最长一天，更大的值按无效处理
LOG_TAIL_LINES = 30
LOG_TAIL_BYTES = 64 * 1024                             # 日志可能很大，只读末尾这么多字节
LOG_LINE_MAX = 300                                     # 每行日志最多保留的字符数
POST_CONTENT_LIMIT = 28_000                            # content 在请求体里占的字节数上限（富文本请求体不超过 30 KB）
IMAGE_KEY_ALLOWANCE = 'img_v2_' + 'x' * 93             # 算长度时图片段的 image_key 按 100 个字符预留（实际约 45 个）
ERROR_SHOT_MAX_AGE_MS = 60_000                         # log/error/<毫秒时间戳>/ 在这么久以内建的，才算这次报错存的截图

_MISSING_CREDENTIALS = '飞书凭证缺失：.env 里没有 FEISHU_APP_ID 或 FEISHU_APP_SECRET'

_screenshot_provider: Callable[[], Any | None] | None = None   # 本进程登记的截图来源，只有实例进程登记
_instance_name: str | None = None                      # 本进程的实例名（原样，不是大写），找编排进度文件用
_flush_quiet: set[str] = set()                         # flush 已记过的失败类别：下一次发送成功之前不再记
_flush_missing_logged = False                          # flush 是否已记过凭证缺失：每个进程最多记一次


def _now() -> datetime:
    """本机的 naive 当前时间，合并窗口和条目的 created_at 都用它。单独成函数，测试可以换掉。"""
    return datetime.now()


def _unexpected(exc: BaseException) -> str:
    """意外异常的日志文字：类型、说明和标准库格式的调用栈。只能在 except 块里调用。

    不用 logger.exception，也不传 exc_info：项目日志的 handler 会把 traceback 里每一帧的局部变量写进日志，其中可能有
    app secret、token 和请求体（见 FeishuError）。标准库格式的调用栈只有代码行，没有变量的值。
    """
    return f'{type(exc).__name__}: {exc}\n{traceback.format_exc().rstrip()}'


def register_screenshot_provider(fn: Callable[[], Any | None]) -> None:
    """登记本进程的截图来源：调用时返回设备最新的一帧（RGB 的 numpy 数组），没有时返回 None。

    登记过的进程（实例进程）推送时才合并同类错误、才附截图；主进程从不登记。
    """
    global _screenshot_provider
    _screenshot_provider = fn


def _credentials_present() -> bool:
    """FEISHU_APP_ID、FEISHU_APP_SECRET 都有值（进程环境变量优先，其次当前目录的 .env）。值不留在局部变量里。"""
    return bool(read_env('FEISHU_APP_ID')) and bool(read_env('FEISHU_APP_SECRET'))


def _client(session) -> FeishuClient:
    """按 .env 里的凭证建客户端，调用方先用 _credentials_present 确认过。

    app secret 直接传进构造函数，不绑定到这一帧的局部变量：traceback 带局部变量渲染时，这一帧也可能在里面。
    """
    return FeishuClient(read_env('FEISHU_APP_ID') or '', read_env('FEISHU_APP_SECRET') or '', session=session)


def _parse_target(cfg: dict) -> tuple[str, str] | None:
    """接收方 (receive_id_type, receive_id)：类型只能是 open_id 或 chat_id，receive_id 是非空字符串（去掉两边空格）。
    缺了或不合法时为 None。推送配置、发件箱条目和合并的 meta 都用它检查。"""
    id_type, receive_id = cfg.get('receive_id_type'), cfg.get('receive_id')
    if id_type not in RECEIVE_ID_TYPES or not isinstance(receive_id, str) or not receive_id.strip():
        return None
    return id_type, receive_id.strip()


def _target_error(cfg: dict) -> str:
    return (f'飞书推送配置有误（OASX 里实例的 脚本 → 出错设置 → 消息推送配置）：receive_id_type 要写 open_id 或 chat_id，'
            f'receive_id 不能为空；现在 receive_id_type 是 {cfg.get("receive_id_type")!r}')


def _aggregate_minutes(cfg: dict) -> int:
    """合并窗口的分钟数：缺省 10，0 表示不合并。不是 0 到 MAX_AGGREGATE_MINUTES 的整数时记一条警告，按缺省处理。"""
    value = cfg.get('aggregate_minutes')
    if value is None:
        return DEFAULT_AGGREGATE_MINUTES
    if _is_int(value) and 0 <= value <= MAX_AGGREGATE_MINUTES:
        return value
    logger.warning(f'飞书推送配置的 aggregate_minutes 无效（{value!r}），按 {DEFAULT_AGGREGATE_MINUTES} 分钟处理')
    return DEFAULT_AGGREGATE_MINUTES


class FeishuAppNotifier:
    """推送配置写 provider: feishu_app 时，notify.Notifier 把 push 转交给它。

    cfg 是解析好的推送配置：receive_id_type（open_id 或 chat_id）、receive_id、aggregate_minutes（可省略，缺省 10，
    0 表示不合并）。config_name 由 Notifier 在每次 push 前同步：实例名的大写，测试推送时为空。
    root（缺省当前目录）和 session（缺省 requests）只给测试用。
    """

    def __init__(self, cfg: dict, enable: bool, *, root: Path | None = None, session=requests):
        self.cfg = dict(cfg) if isinstance(cfg, dict) else {}
        self.enable = enable
        self.config_name = ''
        self.root = Path(root) if root is not None else Path.cwd()
        self.session = session

    def push(self, title: str, content: str) -> bool:
        """先写发件箱，再试着立即发送。返回 True 表示已发出或已合并，False 表示没发出。不抛异常。

        1. 没开就返回 False。接收方配置不对、凭证缺失：记一条错误，返回 False，不写发件箱。
        2. 登记过截图来源的进程先过合并：窗口内的同类错误只计数，记一行 INFO，返回 True。
        3. 拼消息、选截图，写进发件箱，再发送：成功删掉条目；飞书拒收改名为 .failed；网络错误、暂时性错误、凭证错误
           和意外异常留着条目，由主进程补发。写不进发件箱就不发，返回 False。
        """
        try:
            return self._push(title, content)
        except Exception as exc:
            logger.error(f'飞书推送出错：{_unexpected(exc)}')
            return False

    def _push(self, title: str, content: str) -> bool:
        if not self.enable:
            return False
        target = _parse_target(self.cfg)
        if target is None:
            logger.error(f'{_target_error(self.cfg)}。这条推送没有发出')
            return False
        if not _credentials_present():
            logger.error(f'{_MISSING_CREDENTIALS}，这条推送没有发出')
            return False
        full_title = f'{self.config_name} {title}'.strip()
        key = f'{self.config_name}|{title}|{content}'
        if _screenshot_provider is not None and not self._admit(target, full_title, key):
            return True
        lines = _compose_lines(full_title, content, self.root)
        png = _pick_image(self.root)
        outbox = Outbox(self.root / OUTBOX_DIR)
        try:
            path = outbox.put(_entry(target, full_title, lines, png))
        except OSError as exc:
            logger.error(f'飞书推送没有发出：写不进发件箱（{exc!r}）')
            return False
        try:
            _deliver(_client(self.session), path, target, full_title, lines, png)
        except FeishuError as exc:
            if exc.kind == 'rejected':
                _fail(outbox, path)
                logger.error(f'飞书拒收了这条推送，已改名为 .failed 留着备查：{exc}')
            elif exc.kind == 'credentials':
                logger.error(f'飞书凭证错误，推送留在发件箱，凭证改好后由主进程补发：{exc}')
            else:
                logger.warning(f'飞书推送暂时没发出，留在发件箱，由主进程补发：{exc}')
            return False
        _sent_ok()
        _done(outbox, path)
        logger.info(f'飞书推送已发出：{full_title}')
        return True

    def _admit(self, target: tuple[str, str], full_title: str, key: str) -> bool:
        """同类合并：窗口内的第一条放行（True），之后的只计数，记一行 INFO，返回 False。
        meta 记下接收方、完整标题和窗口分钟数，汇总照它发。"""
        window = _aggregate_minutes(self.cfg)
        meta = {'receive_id_type': target[0], 'receive_id': target[1], 'title': full_title, 'window': window}
        if Aggregator(self.root / AGGREGATE_STATE, window).admit(key, _now(), meta):
            return True
        logger.info(f'同类错误合并中：{full_title}，{window} 分钟内只发第一条，窗口结束后由主进程补发一条汇总')
        return False


def _entry(target: tuple[str, str], title: str, lines: list[str], png: bytes | None) -> dict:
    """发件箱条目：created_at、接收方、标题、各行文字和 PNG（base64，没有图为 None）。

    条目的 id 就是文件名去掉 .json（Outbox.put 起的名字），不另存。不存 image_key（有效期没写明，每次发送都重新上传），
    也不存任何凭证。
    """
    return {'created_at': _now().isoformat(timespec='seconds'), 'receive_id_type': target[0], 'receive_id': target[1],
            'title': title, 'lines': lines, 'png_base64': base64.b64encode(png).decode('ascii') if png else None}


def _entry_fields(entry: dict) -> tuple | None:
    """发件箱条目里发送要用的 (接收方, 标题, 各行, PNG)。字段缺了、类型不对或 base64 解不开时为 None。"""
    target, title, lines, png = _parse_target(entry), entry.get('title'), entry.get('lines'), entry.get('png_base64')
    if (target is None or not isinstance(title, str) or not isinstance(lines, list)
            or not all(isinstance(line, str) for line in lines) or not (png is None or isinstance(png, str))):
        return None
    try:
        image = base64.b64decode(png, validate=True) if png else None
    except ValueError:                 # binascii.Error 是 ValueError 的子类
        return None
    return target, title, lines, image


def _deliver(client: FeishuClient, path: Path, target: tuple[str, str], title: str, lines: list[str],
             png: bytes | None) -> None:
    """发一条发件箱条目：有图先上传，再发富文本。条目的 id（文件名去掉 .json）作 uuid，重发不会重复。"""
    image_key = client.upload_image(png) if png else None
    client.send_post(target[0], target[1], title, lines, image_key, uuid=path.stem[:UUID_MAX_LENGTH])


def _sent_ok() -> None:
    """发送成功了一次：flush 记过的失败类别清空，之后再失败重新记。"""
    _flush_quiet.clear()


def _done(outbox: Outbox, path: Path) -> None:
    try:
        outbox.done(path)
    except OSError as exc:
        logger.warning(f'飞书发件箱：{path.name} 已发出，但删不掉（{exc!r}）；主进程会再发一次，'
                       f'同一 uuid 飞书 1 小时内只发出一条')


def _fail(outbox: Outbox, path: Path) -> None:
    try:
        outbox.fail(path)
    except OSError as exc:
        logger.warning(f'飞书发件箱：{path.name} 改名为 .failed 失败（{exc!r}）')


def _compose_lines(title: str, content: str, root: Path) -> list[str]:
    """正文的各行，每行一段：错误摘要（有多行就逐行）、正在跑的编排行、日志尾部（前面加一行小标题）。空行不要。"""
    fixed = [line for line in str(content).splitlines() if line.strip()]
    row = _orchestra_row_line(root)
    if row:
        fixed.append(row)
    return _fit(title, fixed, _log_tail())


def _orchestra_row_line(root: Path) -> str | None:
    """本实例编排正在跑的那一行：编排：第 N 行 <任务显示名>（不在映射表里的任务用原名）。

    只在进度文件的阶段是 row_running 时才有；本进程没登记实例名（主进程）或进度文件读不了时没有。
    编排模块在这里才导入：建任务映射表要导入上游的配置模型。实例进程里 script.py 早已导入过它们。
    """
    name = _instance_name
    if not name:
        return None
    try:
        from module.orchestra.mapping import TASK_SPECS
        from module.orchestra.progress import load_progress, progress_path
        p = load_progress(progress_path(name, root))
        if p.phase != 'row_running':
            return None
        spec = TASK_SPECS.get(p.row_task)
        label = spec.title if spec is not None else (p.row_task or '')
        return f'编排：第 {p.row_index + 1} 行 {label}'.rstrip()
    except Exception as exc:
        logger.warning(f'飞书推送：读不了编排进度（{type(exc).__name__}: {exc}），这条不写编排行')
        return None


def _log_tail() -> list[str]:
    """项目日志（logger.log_file，实例进程里就是本实例的日志）的最后 LOG_TAIL_LINES 行，经 handle_sensitive_logs 脱敏，
    去掉空行。只读文件末尾 LOG_TAIL_BYTES 字节；读不了时为空列表，消息里就没有日志段。

    脱敏模块与 script.py 的 save_error_log 一样用到时才导入：它的源码里有无效转义，编译时发 DeprecationWarning。
    """
    from module.handler.sensitive_info import handle_sensitive_logs
    path = getattr(logger, 'log_file', None)
    if not path:
        return []
    try:
        with open(path, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', errors='replace').splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]              # 第一行多半是从中间截断的
    return [line for line in handle_sensitive_logs(lines[-LOG_TAIL_LINES:]) if line.strip()]


def _fit(title: str, fixed: list[str], log_lines: list[str]) -> list[str]:
    """拼出全部行，把富文本请求体控制在 30 KB 以内：日志每行截到 LOG_LINE_MAX 个字符，content 仍超过 POST_CONTENT_LIMIT
    就从最早的日志行删起。标题、摘要、编排行不删；日志一行不剩时，小标题也不要。"""
    log_lines = [line[:LOG_LINE_MAX] for line in log_lines]
    while True:
        lines = fixed + ([f'日志最后 {len(log_lines)} 行：', *log_lines] if log_lines else [])
        if not log_lines or _content_cost(title, lines) <= POST_CONTENT_LIMIT:
            return lines
        log_lines = log_lines[1:]


def _content_cost(title: str, lines: list[str]) -> int:
    """content 在发消息的请求体里实际占的字节数。send_post 用 json.dumps(ensure_ascii=False) 把它转成字符串，requests
    再用 json= 序列化整个请求体（非 ASCII 字符转成 \\uXXXX，一个汉字占 6 个字节），所以按两层 json.dumps 算。
    拼法与 send_post 相同；有没有图都按预留的 image_key 算上图片段。"""
    paragraphs = [[{'tag': 'text', 'text': line}] for line in lines]
    paragraphs.append([{'tag': 'img', 'image_key': IMAGE_KEY_ALLOWANCE}])
    return len(json.dumps(json.dumps({'zh_cn': {'title': title, 'content': paragraphs}}, ensure_ascii=False)))


def _pick_image(root: Path) -> bytes | None:
    """这条消息附的截图（PNG）。没登记截图来源的进程（主进程）不附图。

    先找 60 秒内 save_error_log 存的出错截图（已脱敏，原样发）；没有就取设备最新的一帧，经 handle_sensitive_image
    脱敏后编码成 PNG。截图来源抛异常或返回 None 时不附图，文字照发。
    """
    provider = _screenshot_provider
    if provider is None:
        return None
    png = _recent_error_png(root)
    return png if png is not None else _frame_png(provider)


def _recent_error_png(root: Path) -> bytes | None:
    """log/error/ 下最新的 <毫秒时间戳> 目录（60 秒内建的）里最新的 PNG。文件名是截图时刻，按名字排序就是先后。"""
    try:
        stamped = [(int(p.name), p) for p in (root / 'log' / 'error').iterdir()
                   if p.name.isascii() and p.name.isdigit() and p.is_dir()]
        now_ms = time.time() * 1000
        recent = [(stamp, p) for stamp, p in stamped if 0 <= now_ms - stamp <= ERROR_SHOT_MAX_AGE_MS]
        if not recent:
            return None
        shots = sorted(p for p in max(recent)[1].glob('*.png') if p.is_file())
        return shots[-1].read_bytes() if shots else None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(f'飞书推送：读不了出错截图（{exc!r}），改用设备最新的一帧')
        return None


def _frame_png(provider: Callable[[], Any | None]) -> bytes | None:
    """设备最新的一帧，脱敏后按 RGB 编码成 PNG（与 module/base/utils/utils.py 的 save_image 存图方式相同）。
    脱敏模块用到时才导入，原因见 _log_tail。"""
    from module.handler.sensitive_info import handle_sensitive_image
    try:
        frame = provider()
    except Exception as exc:
        logger.warning(f'飞书推送：取设备截图失败（{type(exc).__name__}: {exc}），这条只发文字')
        return None
    if frame is None:
        return None
    try:
        buffer = io.BytesIO()
        Image.fromarray(handle_sensitive_image(frame)).save(buffer, format='PNG')
        return buffer.getvalue()
    except Exception as exc:
        logger.warning(f'飞书推送：截图转成 PNG 失败（{type(exc).__name__}: {exc}），这条只发文字')
        return None


def _load_notify_config(text) -> dict | None:
    """照 notify.Notifier 的方式解析推送配置：YAML 的各个文档依次合并成一个 dict。解析不了时为 None。"""
    cfg: dict = {}
    try:
        for item in yaml.safe_load_all(text):
            cfg.update(item)
    except Exception:
        return None
    return cfg


def on_script_loop_start(script, *, session=requests) -> None:
    """实例进程的调度循环开始时调用（script.py 的 loop）。不抛异常。session 只给测试用。

    - 登记截图来源和实例名。截图来源只读 script.__dict__ 里已经建好的设备，不会触发 Script.device 的懒加载；
      实例名（script.config_name，原样）用来找编排进度文件。
    - 本实例开着推送、provider 是 feishu_app 时自检一次，结果记一行日志：接收方配置不对、凭证缺失、凭证错误、
      网络错误、暂时性错误各记一条错误，取到 token 记一条 INFO。
    """
    global _instance_name
    try:
        _instance_name = script.config_name
        register_screenshot_provider(lambda: getattr(script.__dict__.get('device'), 'image', None))
        _self_check(script.config.model.script.error, session)
    except Exception as exc:
        logger.error(f'飞书自检出错：{_unexpected(exc)}')


def _self_check(error, session) -> None:
    if not error.notify_enable:
        return
    cfg = _load_notify_config(error.notify_config)
    if cfg is None or cfg.get('provider') != 'feishu_app':
        return
    if _parse_target(cfg) is None:
        logger.error(_target_error(cfg))
        return
    if not _credentials_present():
        logger.error(f'{_MISSING_CREDENTIALS}，报错时推送不出去')
        return
    try:
        _client(session).tenant_token()
    except FeishuError as exc:
        if exc.kind == 'credentials':
            logger.error(f'飞书凭证错误：code={exc.code}，msg={exc.msg}，请核对 .env 里的 FEISHU_APP_ID 和 FEISHU_APP_SECRET')
        elif exc.kind == 'network':
            logger.error(f'飞书网络错误：{exc}')
        else:
            logger.error(f'飞书自检没通过（暂时性错误，发送时会重试）：{exc}')
        return
    logger.info('飞书自检通过：凭证有效')


def flush_outbox(*, root: Path | None = None, session=requests) -> int:
    """补发：主进程每 60 秒调一次。先把到期的合并汇总写进发件箱，再按写入先后发发件箱里的消息，返回这一轮发出的条数。
    不抛异常。root（缺省当前目录）和 session（缺省 requests）只给测试用。

    - 发件箱是空的、也没有合并状态文件：返回 0，不联网，也不读 .env。
    - 凭证缺失：每个进程只记一次错误，返回 0，发件箱和合并状态都不动。
    - 到期的汇总：发给窗口里第一条消息的接收方，标题相同，只有一行"{标题}：过去 {窗口} 分钟同类错误又出现 {n} 次"，不附图。
    - 逐条发送：成功删掉条目；飞书拒收改名为 .failed，记一条错误，接着发下一条；字段不全的条目改名为 .bad；
      网络错误、暂时性错误、凭证错误和意外异常留着这一条和后面的，这一轮到此为止。
      同一类失败在下一次发送成功之前只记一次，免得每 60 秒记一遍。
    """
    try:
        return _flush(Path(root) if root is not None else Path.cwd(), session)
    except Exception as exc:
        _flush_log_once('unexpected', f'飞书补发出错，下一轮再试：{_unexpected(exc)}', error=True)
        return 0


def _flush(root: Path, session) -> int:
    global _flush_missing_logged
    outbox, state = Outbox(root / OUTBOX_DIR), root / AGGREGATE_STATE
    if not outbox.pending() and not state.exists():
        return 0
    if not _credentials_present():
        if not _flush_missing_logged:
            _flush_missing_logged = True
            logger.error(f'{_MISSING_CREDENTIALS}，发件箱里的消息先留着，补上凭证后再发')
        return 0
    _queue_summaries(outbox, Aggregator(state, DEFAULT_AGGREGATE_MINUTES))
    pending = outbox.pending()
    if not pending:
        return 0
    client, sent = _client(session), 0
    for path in pending:
        entry = outbox.load(path)
        if entry is None:
            continue
        fields = _entry_fields(entry)
        if fields is None:
            outbox._quarantine(path, '字段不全或类型不对')
            continue
        try:
            _deliver(client, path, *fields)
        except FeishuError as exc:
            if exc.kind == 'rejected':
                _fail(outbox, path)
                logger.error(f'飞书拒收了发件箱里的 {path.name}，已改名为 .failed 留着备查：{exc}')
                continue
            if exc.kind == 'credentials':
                _flush_log_once(exc.kind, f'飞书补发暂停：凭证错误，消息留在发件箱，凭证改好后再发：{exc}', error=True)
            else:
                _flush_log_once(exc.kind, f'飞书补发暂停，消息留在发件箱，下一轮再试：{exc}', error=False)
            break
        except Exception as exc:
            _flush_log_once('unexpected', f'飞书补发出错，消息留在发件箱，下一轮再试：{_unexpected(exc)}', error=True)
            break
        sent += 1
        _sent_ok()
        _done(outbox, path)
    return sent


def _queue_summaries(outbox: Outbox, aggregator: Aggregator) -> None:
    """把到期的合并汇总写进发件箱：接收方和标题取窗口里第一条消息的（meta），只有一行文字，不附图。"""
    for key, count in aggregator.due_summaries(_now()):
        meta = aggregator.meta(key) or {}
        target, title = _parse_target(meta), meta.get('title')
        if target is None or not isinstance(title, str):
            logger.warning(f'飞书合并汇总缺少接收方或标题，没法补发：同类错误又出现 {count} 次')
            continue
        line = f'{title}：过去 {meta.get("window")} 分钟同类错误又出现 {count} 次'
        try:
            outbox.put(_entry(target, title, [line], None))
        except OSError as exc:
            logger.error(f'飞书合并汇总写不进发件箱（{exc!r}），这条汇总没发：{line}')


def _flush_log_once(kind: str, message: str, *, error: bool) -> None:
    """flush 的失败日志：同一类失败在下一次发送成功之前只记一次。"""
    if kind in _flush_quiet:
        return
    _flush_quiet.add(kind)
    if error:
        logger.error(message)
    else:
        logger.warning(message)
