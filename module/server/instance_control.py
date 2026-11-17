"""实例重启，以及各个自定义接口共用的实例名核对。

重启先 await 进程的 stop()，再 await start()。上游做不到这一点：GET /{name}/start 调了 async 的 start() 却没 await，
ScriptProcess.start() 发现进程还活着时调 self.stop() 同样没 await，旧进程不一定停得掉。
同一个实例的重启按先后排队：两次重启交错的话，两次 stop 叠在一起，后一次 start 可能让前一次起的进程没人管。
M3a 合并上游代码后用 restart_running_instances 让在跑的实例换上新代码，单个实例用 POST /custom/instance/{name}/restart。

不在模块顶层导入 module.server.main_manager：它一导入就扫描 config/、给每个实例建进程对象并启动推送线程。
需要进程表时经 _manager() 现取，测试里把它换成假的。
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter

from module.logger import logger
from module.server.script_process import ScriptState

instance_app = APIRouter(prefix='/custom/instance')


def _manager():
    """上游的 MainManager 单例，用到时才导入。"""
    from module.server.main_manager import mm
    return mm


def instance_names() -> set[str]:
    """config/ 下实例配置的名字，不含 template。

    只读目录。不用上游的 all_script_files：它在没有任何实例配置时会顺手复制出一份 oas1.json。
    """
    return {path.stem for path in (Path.cwd() / 'config').glob('*.json') if path.stem != 'template'}


_restart_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _restart_lock(name: str) -> asyncio.Lock:
    """name 的重启锁，在当前运行的事件循环上第一次用到时才建。asyncio.Lock 一有争用就绑定到当时的循环，
    换了循环（比如测试里每个 asyncio.run 都是新的循环）就换一把新的。只在事件循环线程里调用，不用另加锁。"""
    loop = asyncio.get_running_loop()
    entry = _restart_locks.get(name)
    if entry is None or entry[0] is not loop:
        entry = _restart_locks[name] = (loop, asyncio.Lock())
    return entry[1]


async def restart_instance(name: str) -> bool:
    """重启一个实例：先 stop() 再 start()，都 await；同一个实例的两次重启按先后排队。进程表里没有这个实例返回 False。"""
    async with _restart_lock(name):
        process = _manager().script_process.get(name)
        if process is None:
            return False
        await process.stop()
        await process.start()
    logger.info(f'[{name}] 实例已重启')
    return True


async def restart_running_instances() -> list[str]:
    """重启所有没停的实例（状态不是 INACTIVE），返回重启成了的名字。停着的不动，不替用户启动。
    一个实例出错只记日志（带调用栈），不挡着其余的，也不列进返回值。"""
    restarted: list[str] = []
    for name, process in list(_manager().script_process.items()):   # 重启中途进程表可能被别的请求改动，先取快照
        try:
            if process.state != ScriptState.INACTIVE and await restart_instance(name):
                restarted.append(name)
        except Exception:
            logger.exception(f'[{name}] 实例重启失败')
    return restarted


@instance_app.post('/{script_name}/restart')
async def post_restart(script_name: str) -> dict:
    """重启实例，让它重新加载代码和配置。名字不是 config/ 下的实例时直接返回 ok 为 False。"""
    if script_name not in await asyncio.to_thread(instance_names):
        return {'ok': False}
    return {'ok': await restart_instance(script_name)}
