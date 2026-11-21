"""飞书自建应用推送：HTTP 客户端（FeishuClient）、发件箱（Outbox）、同类错误合并器（Aggregator），以及接在现有
推送配置上的通知器（FeishuAppNotifier）、主进程补发（flush_outbox）、主进程的事件通知（notify_server_event）和实例启动时的
登记与自检（on_script_loop_start）。

导入本模块不联网，也不建文件和目录（module.logger 自己导入时会打开日志文件，与其他模块相同）。
只用已有依赖：requests、PyYAML、Pillow；测试图用 OpenCV（cv2），用到时才导入。

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
  写下超过 2 分钟才发出去的，文字最后加一行"（补发：原定 <时刻>）"。
- 实例进程（on_script_loop_start 登记过截图来源的进程）合并同类错误：同一实例、标题和内容相同的报错在 aggregate_minutes
  分钟内只发第一条，窗口结束后由 flush_outbox 补发一条汇总，发给窗口里第一条的接收方。主进程的测试推送不合并。
- 一条消息：标题，然后每行一段：错误摘要、正在跑的编排行、日志最后 30 行（脱敏），最后是截图。富文本请求体不超过
  30 KB：标题和固定的行各截到 1,000 个字符，日志每行截到 300 个字符；超了从最早的日志行删起，还超就截短最长的固定行。
  主进程的测试推送（/home/notify_test）没有截图，附一张生成的测试图。
- 主进程自己的事件（每日定时合并上游需要人处理的结果）没有对应的推送配置：notify_server_event 发给各实例配置里
  开着飞书推送的接收方，只有文字（每行最多 300 个字符），不附图，不合并。
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
        start, end = datetime.fromisoformat(item['start']), datetime.fromisoformat(item['end'])
        if start.tzinfo is not None or end.tzinfo is not None:
            raise ValueError('start、end 带着时区：窗口一律用本机的 naive 时间，和它比较会抛 TypeError')
        return cls(start, end, count, pending, meta)


class Aggregator:
    """同类错误合并：同一个 key 在窗口内只放行第一条，之后只计数；窗口结束后由 due_summaries 交出计数，用来补发汇总。

    状态文件由实例进程和主进程共用。每个 key 存窗口的起止时刻、计数、待交的计数和开窗那一条的 meta（要能存成 JSON）。
    每次读改写都在进程内的锁里做（本类的所有对象共用一把），写入用 atomic_write 整个替换。不加跨进程锁：两个进程
    同一瞬间改同一个文件时后写的覆盖先写的，最多让汇总少算一次，或多放行一条。

    - 文件不存在按空处理，什么都不交时也不建文件；内容损坏（包括带时区的时刻）记一条 WARNING，按空处理，下次写入时覆盖。
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
FIXED_LINE_MAX = 1_000                                 # 标题和固定的行（错误摘要、编排行、汇总行、主进程事件的各行）最多保留的字符数
SUMMARY_CONTENT_MAX = 200                              # 汇总行里点明是哪条错误时，内容第一行最多保留的字符数
POST_CONTENT_LIMIT = 28_000                            # content 在请求体里占的字节数上限（富文本请求体不超过 30 KB）
IMAGE_KEY_ALLOWANCE = 'img_v2_' + 'x' * 93             # 算长度时图片段的 image_key 按 100 个字符预留（实际约 45 个）
ERROR_SHOT_MAX_AGE_MS = 60_000                         # log/error/<毫秒时间戳>/ 在这么久以内建的，才算这次报错存的截图
LATE_AFTER = timedelta(seconds=120)                    # 条目写下超过这么久才发出去，末尾加一行注明原定的时刻
NO_TEXT_LINE = '（没有说明文字）'                          # 正文一行都没有时放的那一行
TEST_IMAGE_SIZE = (320, 80)                            # 测试图的宽、高
TEST_IMAGE_TEXT = 'OAS TEST'                           # cv2 的字体没有中文字形，测试图上只写英文

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
        title、content 为 None 时按空字符串处理。

        1. 没开就返回 False。接收方配置不对、凭证缺失：记一条错误，返回 False，不写发件箱。
        2. 登记过截图来源的进程先过合并：窗口内的同类错误只计数，记一行 INFO，返回 True。合并出错按放行处理。
        3. 拼消息、选截图，写进发件箱，再发送：成功删掉条目；飞书拒收这条消息改名为 .failed（只拒收截图时文字照发，
           见 _deliver）；网络错误、暂时性错误、凭证错误和意外异常留着条目，由主进程补发。写不进发件箱就不发，返回 False。
        """
        try:
            return self._push(title, content)
        except Exception as exc:
            logger.error(f'飞书推送出错：{_unexpected(exc)}')
            return False

    def _push(self, title: str, content: str) -> bool:
        if not self.enable:
            return False
        title, content = ('' if value is None else str(value) for value in (title, content))
        target = _parse_target(self.cfg)
        if target is None:
            logger.error(f'{_target_error(self.cfg)}。这条推送没有发出')
            return False
        if not _credentials_present():
            logger.error(f'{_MISSING_CREDENTIALS}，这条推送没有发出')
            return False
        full_title = f'{self.config_name} {title}'.strip()[:FIXED_LINE_MAX]
        key = f'{self.config_name}|{title}|{content}'
        if _screenshot_provider is not None and not self._admit(target, full_title, key, content):
            return True
        lines = _compose_lines(full_title, content, self.root)
        png = _pick_image(self.root)
        outbox, entry = Outbox(self.root / OUTBOX_DIR), _entry(target, full_title, lines, png)
        try:
            path = outbox.put(entry)
        except OSError as exc:
            logger.error(f'飞书推送没有发出：写不进发件箱（{exc!r}）')
            return False
        try:
            _deliver(_client(self.session), path, target, full_title, lines, png, _created_at(entry))
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

    def _admit(self, target: tuple[str, str], full_title: str, key: str, content: str) -> bool:
        """同类合并：窗口内的第一条放行（True），之后的只计数，记一行 INFO，返回 False。
        meta 记下接收方、完整标题、窗口分钟数和内容的第一行（first_line，汇总行用它点明是哪条错误），汇总照它发。
        合并本身出错（比如状态文件里有意外的值）按放行处理，记一行警告：发送不能依赖合并。"""
        window = _aggregate_minutes(self.cfg)
        meta = {'receive_id_type': target[0], 'receive_id': target[1], 'title': full_title, 'window': window,
                'first_line': _first_line(content)}
        try:
            admitted = Aggregator(self.root / AGGREGATE_STATE, window).admit(key, _now(), meta)
        except Exception as exc:
            logger.warning(f'飞书同类合并出错，这条照常发送：{type(exc).__name__}: {exc}')
            return True
        if admitted:
            return True
        logger.info(f'同类错误合并中：{full_title}，{window} 分钟内只发第一条，窗口结束后由主进程补发一条汇总')
        return False


def _first_line(content: str) -> str:
    """内容里第一个有字的行，去掉两边空白，截到 SUMMARY_CONTENT_MAX 个字符；没有时为空字符串。"""
    return next((line.strip() for line in content.splitlines() if line.strip()), '')[:SUMMARY_CONTENT_MAX]


def _summary_line(title: str, meta: dict, count: int) -> str:
    """到期汇总的那一行：{标题}：过去 {窗口} 分钟同类错误又出现 {n} 次（{内容的第一行}）。
    meta 里没有内容的第一行（旧版本写下的合并状态）时不带括号那一段。"""
    line = f'{title}：过去 {meta.get("window")} 分钟同类错误又出现 {count} 次'
    first = meta.get('first_line')
    return f'{line}（{first.strip()[:SUMMARY_CONTENT_MAX]}）' if isinstance(first, str) and first.strip() else line


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
             png: bytes | None, created: datetime | None = None) -> None:
    """发一条发件箱条目，push 的立即发送和 flush_outbox 的补发都走这里：有图先上传，再发富文本。
    条目的 id（文件名去掉 .json）作 uuid，重发不会重复。

    飞书拒收这张图（rejected，比如缺 im:resource 权限、图超过 10 MB）时，不附图，末尾加一行说明（带 code），文字照发，
    拒收的 code 和 msg 记一行错误。传图的其他失败（网络错误、暂时性错误、凭证错误）照常抛出，条目原样留着。
    created 是条目的 created_at（见 _created_at）：写下已超过 LATE_AFTER 才发出去时，文字的最后加一行"（补发：原定 <时刻>）"。
    """
    image_key = None
    if png:
        try:
            image_key = client.upload_image(png)
        except FeishuError as exc:
            if exc.kind != 'rejected':
                raise
            logger.error(f'飞书拒收了截图，这条只发文字：{exc}')
            lines = [*lines, _image_rejected_line(exc.code)]
    if created is not None and _now() - created > LATE_AFTER:
        lines = [*lines, _late_line(created)]
    client.send_post(target[0], target[1], title, lines, image_key, uuid=path.stem[:UUID_MAX_LENGTH])


def _image_rejected_line(code: int | None) -> str:
    """飞书拒收截图时，代替图片段的那一行。"""
    return '截图没能附上：飞书拒收了这张图' + (f'（code={code}）' if code is not None else '')


def _late_line(created: datetime) -> str:
    """补发时文字最后加的那一行：原定的时刻，本机时间，精确到秒。"""
    return f'（补发：原定 {created.isoformat(sep=" ", timespec="seconds")}）'


def _created_at(entry: dict) -> datetime | None:
    """条目的 created_at，换成本机的 naive 时间（带时区的换算过来）；没有、不是字符串或读不懂时为 None。"""
    value = entry.get('created_at')
    if not isinstance(value, str):
        return None
    try:
        created = datetime.fromisoformat(value)
        return created if created.tzinfo is None else created.astimezone().replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):       # 时刻超出范围时 astimezone 可能抛后两种
        return None


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
    """正文的各行，每行一段：错误摘要（有多行就逐行）、正在跑的编排行、日志尾部（前面加一行小标题）。空行不要。
    一行都没有时（没给内容，也没有编排行和日志）放一行 NO_TEXT_LINE：飞书对空的 content 怎么处理没核实过。"""
    fixed = [line for line in str(content).splitlines() if line.strip()]
    row = _orchestra_row_line(root)
    if row:
        fixed.append(row)
    return _fit(title, fixed, _log_tail()) or [NO_TEXT_LINE]


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
    """拼出全部行，把富文本请求体控制在 30 KB 以内：固定的行（摘要、编排行、汇总行、主进程事件的各行）每行截到
    FIXED_LINE_MAX 个字符，日志每行截到 LOG_LINE_MAX 个字符；content 仍超过 POST_CONTENT_LIMIT 就从最早的日志行删起，
    日志一行不剩时小标题也不要；还超就截短最长的固定行（_shrink）。标题由调用方先截到 FIXED_LINE_MAX 个字符。"""
    fixed = [line[:FIXED_LINE_MAX] for line in fixed]
    log_lines = [line[:LOG_LINE_MAX] for line in log_lines]
    while True:
        lines = fixed + ([f'日志最后 {len(log_lines)} 行：', *log_lines] if log_lines else [])
        if _content_cost(title, lines) <= POST_CONTENT_LIMIT:
            return lines
        if not log_lines:
            return _shrink(title, fixed)
        log_lines = log_lines[1:]


def _shrink(title: str, lines: list[str]) -> list[str]:
    """日志行删光后 content 仍超过上限：截短最长的行，直到放得下。找出最大的 n，使每行最多留 n 个字符时放得下，
    只截比 n 长的行，所以最长的行先截，其余不动。每行只留一个字符也放不下（行数太多）时，先从最后一行删起，至少留一行。
    截完只剩空白的行不要；一行都不剩时放一行 NO_TEXT_LINE。标题不超过 FIXED_LINE_MAX 个字符时，一行总放得下。"""
    def cut(width: int, count: int) -> list[str]:
        return [line[:width] for line in lines[:count]]

    def fits(candidate: list[str]) -> bool:
        return _content_cost(title, candidate) <= POST_CONTENT_LIMIT

    def largest(low: int, high: int, ok: Callable[[int], bool]) -> int:
        """[low, high] 里让 ok 成立的最大值（ok 随取值增大由真变假）；都不成立时为 low。"""
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if ok(middle) else (low, middle - 1)
        return low

    count = largest(1, len(lines), lambda n: fits(cut(1, n))) if lines else 0
    width = largest(1, max((len(line) for line in lines[:count]), default=1), lambda n: fits(cut(n, count)))
    return [line for line in cut(width, count) if line.strip()] or [NO_TEXT_LINE]


def _content_cost(title: str, lines: list[str]) -> int:
    """content 在发消息的请求体里实际占的字节数。send_post 用 json.dumps(ensure_ascii=False) 把它转成字符串，requests
    再用 json= 序列化整个请求体（非 ASCII 字符转成 \\uXXXX，一个汉字占 6 个字节），所以按两层 json.dumps 算。
    拼法与 send_post 相同。末尾有没有图都预留三段：按预留 image_key 算的图片段、截图被拒收时换上的那行说明（按 10 位的
    code 算）和补发时加的那一行；图片段和拒收的说明实际只会有其中一段，所以算出来的不会比真发的小。"""
    paragraphs = [[{'tag': 'text', 'text': line}] for line in lines]
    paragraphs.append([{'tag': 'img', 'image_key': IMAGE_KEY_ALLOWANCE}])
    paragraphs.append([{'tag': 'text', 'text': _image_rejected_line(9_999_999_999)}])
    paragraphs.append([{'tag': 'text', 'text': _late_line(datetime.max)}])
    return len(json.dumps(json.dumps({'zh_cn': {'title': title, 'content': paragraphs}}, ensure_ascii=False)))


def _pick_image(root: Path) -> bytes | None:
    """这条消息附的图（PNG）。没登记截图来源的进程（主进程，推送只有 /home/notify_test 这类测试）附一张测试图。

    实例进程先找 60 秒内 save_error_log 存的出错截图（已脱敏，原样发）；没有就取设备最新的一帧，经 handle_sensitive_image
    脱敏后编码成 PNG。截图来源抛异常或返回 None 时不附图，文字照发。
    """
    provider = _screenshot_provider
    if provider is None:
        return _test_png()
    png = _recent_error_png(root)
    return png if png is not None else _frame_png(provider)


def _test_png() -> bytes | None:
    """测试图：TEST_IMAGE_SIZE 大小的浅灰底，正中写 TEST_IMAGE_TEXT，用 cv2 画、cv2.imencode 编码成 PNG。
    每次生成的字节都一样，不到 2 KB；它只用来验证上传图片的权限（im:resource）。生成失败记一条警告，不附图，文字照发。
    cv2 用到时才导入：只有主进程的测试推送用它。"""
    try:
        import cv2
        import numpy as np
        width, height = TEST_IMAGE_SIZE
        font, scale, thickness = cv2.FONT_HERSHEY_SIMPLEX, 1.5, 3
        (text_width, text_height), _ = cv2.getTextSize(TEST_IMAGE_TEXT, font, scale, thickness)
        image = np.full((height, width, 3), 240, np.uint8)
        cv2.putText(image, TEST_IMAGE_TEXT, ((width - text_width) // 2, (height + text_height) // 2), font, scale,
                    (0, 0, 0), thickness, cv2.LINE_8)
        ok, encoded = cv2.imencode('.png', image)
        if not ok:
            raise ValueError('cv2.imencode 没能编码')
        return encoded.tobytes()
    except Exception as exc:
        logger.warning(f'飞书推送：生成测试图失败（{type(exc).__name__}: {exc}），这条只发文字')
        return None


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
    - 到期的汇总：发给窗口里第一条消息的接收方，标题相同，只有一行"{标题}：过去 {窗口} 分钟同类错误又出现 {n} 次（{内容的
      第一行}）"（旧版本的合并状态没存内容，不带括号那一段），不附图。
      汇总出错记一次（到下一次发送成功为止），发件箱里的消息照常发。
    - 逐条发送（与 push 同一条路径 _deliver，只拒收截图时文字照发）：成功删掉条目；飞书拒收这条消息改名为 .failed，
      记一条错误，接着发下一条；字段不全的条目改名为 .bad；
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
    try:
        _queue_summaries(outbox, Aggregator(state, DEFAULT_AGGREGATE_MINUTES))
    except Exception as exc:                           # 汇总出错不能拦着发件箱里的消息
        _flush_log_once('summaries', f'飞书合并汇总出错，这一轮照常补发发件箱里的消息：{type(exc).__name__}: {exc}',
                        error=False)
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
            _deliver(client, path, *fields, _created_at(entry))
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
        line = _summary_line(title, meta, count)
        title = title[:FIXED_LINE_MAX]                 # 旧版本存下的标题没截过
        try:
            outbox.put(_entry(target, title, _fit(title, [line], []), None))
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


def notify_server_event(title: str, text: str, *, root: Path | None = None, session=requests) -> None:
    """主进程自己的事件（每日定时合并上游需要人处理的结果）发飞书通知。不抛异常。
    root（缺省当前目录）和 session（缺省 requests）只给测试用。

    主进程没有自己的推送配置，接收方取自 config/ 下的各实例配置（只看这一层，不含 template.json）：开着推送
    （script.error.notify_enable）、推送配置写 provider: feishu_app 的，取 (receive_id_type, receive_id)，去掉重复的。
    - 一个接收方都没有：不发，记一行 INFO。凭证缺失：记一条错误，什么都不写。
    - 每个接收方写一条发件箱条目：标题 title，正文是 text 的各行（空行不要），不附图，不合并。每行最多 LOG_LINE_MAX 个
      字符（自检失败、意外错误带着最多 20 行 stderr），整条超过上限时截短最长的行（见 _fit）。写完立即补发一轮
      （flush_outbox，与补发同一条路径，发件箱里早先的条目先发），没发出去的留给下一轮补发。
    """
    try:
        _notify_server_event(Path(root) if root is not None else Path.cwd(), title, text, session)
    except Exception as exc:
        logger.error(f'飞书通知出错：{_unexpected(exc)}')


def _notify_server_event(root: Path, title: str, text: str, session) -> None:
    targets = _event_targets(root)
    if not targets:
        logger.info(f'没有实例开着飞书推送，不发飞书通知：{title}')
        return
    if not _credentials_present():
        logger.error(f'{_MISSING_CREDENTIALS}，飞书通知没有发出：{title}')
        return
    title = str(title)[:FIXED_LINE_MAX]
    lines = [line[:LOG_LINE_MAX] for line in str(text).splitlines() if line.strip()] or [NO_TEXT_LINE]
    lines = _fit(title, lines, [])
    outbox, written = Outbox(root / OUTBOX_DIR), 0
    for target in targets:
        try:
            outbox.put(_entry(target, title, lines, None))
            written += 1
        except OSError as exc:
            logger.error(f'飞书通知写不进发件箱（{exc!r}），发给 {target[1]} 的没发：{title}')
    if written:
        logger.info(f'飞书通知已写进发件箱，发给 {written} 个接收方：{title}')
        flush_outbox(root=root, session=session)


def _event_targets(root: Path) -> list[tuple[str, str]]:
    """config/ 下各实例配置里开着飞书推送的接收方，按配置文件名的顺序，去掉重复的。

    配置用上游的 read_file 读：它与实例写配置时加同一把文件锁，读的时候不会撞上实例正在替换文件。读不了的配置、
    接收方写得不对的推送配置各记一条警告，跳过。
    """
    from module.config.utils import read_file
    targets: list[tuple[str, str]] = []
    for path in sorted((root / 'config').glob('*.json')):
        if path.name == 'template.json' or not path.is_file():
            continue
        try:
            error = read_file(str(path))['script']['error']
            enabled, notify_config = error.get('notify_enable'), error.get('notify_config')
        except Exception as exc:
            logger.warning(f'飞书通知：读不了实例配置 {path.name}（{type(exc).__name__}: {exc}），跳过')
            continue
        cfg = _load_notify_config(notify_config) if enabled is True else None
        if cfg is None or cfg.get('provider') != 'feishu_app':
            continue
        target = _parse_target(cfg)
        if target is None:
            logger.warning(f'飞书通知：实例 {path.stem} 的{_target_error(cfg)}，跳过')
            continue
        if target not in targets:
            targets.append(target)
    return targets
