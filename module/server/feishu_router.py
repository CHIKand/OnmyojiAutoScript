"""飞书的接口：GET /custom/feishu/chats 列出机器人所在的群，填推送配置时用它查群的 chat_id（步骤见 .env.example）。

凭证只从 read_env 读（FEISHU_APP_ID、FEISHU_APP_SECRET：进程环境变量优先，其次当前目录的 .env），请求里带不进来。
只接受同源请求（素材维护页的 SAME_ORIGIN_ONLY）：带 Origin 头而 host[:port] 与 Host 头不同的回 403，不联网。服务对所有来源
开放 CORS，别的网页读到 chat_id 后，再配上上游不查来源的 /home/notify_test，就能借机器人往这些群里发消息。OASX、curl 和
浏览器地址栏直接打开都不带 Origin，照常放行。
处理函数是普通 def，FastAPI 把它放进线程池跑，联网不卡事件循环。
出什么错都不往外抛，按 {'ok': False, 'chats': [], 'error': 原因} 返回：抛出去会被 app.py 的全局异常处理带着 traceback
记进日志，项目日志会渲染每一帧的局部变量，app secret 和 token 可能就在某一帧里。日志同样只记原因的文字，不带
traceback 的局部变量（规矩见 module.notify.feishu_app.FeishuError）。
"""
import traceback

import requests
from fastapi import APIRouter

from module.config.dotenv import read_env
from module.logger import logger
from module.notify.feishu_app import FeishuClient, FeishuError
from module.server.assets_check_router import SAME_ORIGIN_ONLY

MISSING_CREDENTIALS = '飞书凭证缺失：.env 里没有 FEISHU_APP_ID 或 FEISHU_APP_SECRET'

feishu_app_router = APIRouter(prefix='/custom/feishu')


def _session():
    """发请求用的 HTTP 会话：线上就是 requests 模块，测试换成假的。"""
    return requests


@feishu_app_router.get('/chats', dependencies=SAME_ORIGIN_ONLY)
def get_chats() -> dict:
    """机器人所在的群：{'ok': True, 'chats': [{'chat_id', 'name'}], 'error': ''}。

    失败时 ok 为 False、chats 为空，error 是原因：凭证缺失时点出 .env 的两个键名；飞书接口的错误是 FeishuError 的说明
    （含 HTTP 状态码、code 和 msg，不含密钥和 token）；别的意外是"类型: 说明"。
    """
    if not (read_env('FEISHU_APP_ID') and read_env('FEISHU_APP_SECRET')):
        return _failed(MISSING_CREDENTIALS)
    try:
        # app secret 直接传进构造函数，不绑定到这一帧的局部变量
        chats = FeishuClient(read_env('FEISHU_APP_ID') or '', read_env('FEISHU_APP_SECRET') or '',
                             session=_session()).list_chats()
    except FeishuError as exc:
        return _failed(str(exc))
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        logger.error(f'飞书群列表出错：{error}\n{traceback.format_exc().rstrip()}')   # 标准库的调用栈不带变量的值
        return {'ok': False, 'chats': [], 'error': error}
    return {'ok': True, 'chats': chats, 'error': ''}


def _failed(error: str) -> dict:
    logger.warning(f'飞书群列表没取到：{error}')
    return {'ok': False, 'chats': [], 'error': error}
