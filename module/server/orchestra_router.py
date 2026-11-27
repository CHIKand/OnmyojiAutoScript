"""编排的读写与控制接口，给 OASX 的编排页用：页面每 2 秒 GET 一次，保存用 PUT，按钮用 POST。

每个处理函数先核对实例名，只认 config/ 下已有的实例配置。进度文件的路径由名字直接拼出，
而 ConfigModel 读不到文件时会给默认值、一保存就新建配置文件，名字不核对就会碰到不该碰的路径。

配置只经 ConfigModel 读写，上游在 config/<名字>.json.lock 上加跨进程文件锁，实例进程与本接口不会同时写同一个文件。
本进程里各接口的读改写（PUT 写之前的重读到存盘、命令的读到存盘）再用 _CONFIG_LOCK 串行，互相不会冲掉对方刚写的值。
同步的文件读写（配置、进度、目录列表）都放到工作线程里做，不卡事件循环。

开始、暂停、继续、停止直接启停实例进程（规格 §8.3，2026-11-25 修订）：先停下实例，再写配置和进度，state 是 running 的
写完再启动实例。实例跑任务期间不重读配置文件，任务结束时的自动存盘（上游 ConfigModel.__setattr__）会把内存里的旧副本
整份写回，实例在跑时写进去的运行控制字段就被冲掉了；实例停下之后再写，没有第二个写者。进度文件平时只由实例写，
暂停、停止时实例已经停下，由本接口经 module.orchestra.progress 写。这四条命令都在实例停下之后、再次启动之前清掉进度里
等搭档的等待记录（会合记录不动，组队编排等搭档设计 §7.7）：留着的话，搭档在新进程第一次选任务之前读到它，
会当成这边还在等。继续、开始是尽力清：进度文件读写失败只记警告，照常启动实例，实例第一次选任务时自己会清。
同一实例的命令与重启共用 instance_lock，按先后排队。
跳过休息照旧只改配置、不启停实例：实例等待下一个任务时每 5 秒查一次配置文件的修改时间（上游 Script.wait_until），
发现变化就重新选任务。
"""
import asyncio
import threading
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from module.config.config_model import ConfigModel
from module.logger import logger
from module.orchestra.engine import sync_controls
from module.orchestra.mapping import catalog, restore_params
from module.orchestra.model import Orchestra, validate_settings
from module.orchestra.progress import (Progress, clear_team_wait, clear_team_wait_file, load_progress, progress_path,
                                       progress_to_dict, save_progress)
from module.server.instance_control import instance_lock, instance_names, start_instance, stop_instance

orchestra_app = APIRouter(prefix='/custom/orchestra')

_NOT_FOUND = '实例不存在'
_EDITABLE = ('rows', 'after_finish', 'total_minutes_cap', 'default_rest_minutes', 'rest_jitter', 'resume_policy',
             'cut_in_tasks', 'partner', 'partner_wait_minutes')   # PUT 能改的项；state、command、command_seq 只由命令接口和引擎写
_STATE_OF_ACTION = {'start': 'running', 'resume': 'running', 'pause': 'paused', 'stop': 'idle'}   # 启停实例的命令写的 state
_ACTIONS = (*_STATE_OF_ACTION, 'skip_rest')   # 用元组，action 是什么类型的值都能安全地比较
_CONFIG_LOCK = threading.Lock()   # 本进程里接口之间的配置读改写串行，见文件开头


def _validation_errors(exc: ValidationError) -> list[dict]:
    """pydantic 的类型错误转成接口的错误格式，同一处出的多条只留一条。

    位置形如 ('rows', 行号, 属性名, ...) 的落到那一行的那一项，其余落到设置项（行号 -1）。
    """
    errors: list[dict] = []
    for item in exc.errors():
        loc = item['loc']
        if len(loc) >= 3 and loc[0] == 'rows' and isinstance(loc[1], int):
            error = {'index': loc[1], 'field': str(loc[2]), 'message': '取值不合法'}
        else:
            error = {'index': -1, 'field': str(loc[0]) if loc else '', 'message': '取值不合法'}
        if error not in errors:
            errors.append(error)
    return errors


def _read_view(name: str) -> dict:
    names = instance_names()
    if name not in names:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    settings = ConfigModel(name).orchestra.model_dump(mode='json')
    try:
        progress = load_progress(progress_path(name))
    except OSError as exc:   # 进度文件被占用，重试用尽仍读不了：页面 2 秒后会再来读
        raise HTTPException(status_code=503, detail='进度文件暂时无法读取') from exc
    return {'settings': settings, 'progress': progress_to_dict(progress, datetime.now()), 'catalog': catalog(),
            'partners': sorted(names - {name})}   # 搭档下拉框的候选：其余实例


def _write_settings(name: str, body: dict) -> dict:
    names = instance_names()
    if name not in names:
        return {'ok': False, 'errors': [{'index': -1, 'field': 'script_name', 'message': _NOT_FOUND}]}
    current = ConfigModel(name).orchestra
    accepted = {key: body[key] for key in _EDITABLE if key in body}
    try:
        candidate = Orchestra.model_validate({**current.model_dump(), **accepted})
    except ValidationError as exc:
        return {'ok': False, 'errors': _validation_errors(exc)}
    errors = validate_settings(candidate, names, name)   # 搭档要是另一个已有的实例
    if errors:
        return {'ok': False, 'errors': [asdict(error) for error in errors]}

    # 校验与写入之间实例可能改过 state、command_seq（引擎跑完一遍会把 state 置回 idle），命令接口也可能刚写过，
    # 所以写之前重读一次，只覆盖用户可改的项，运行控制字段保持读到的样子。重读到存盘持 _CONFIG_LOCK，
    # 本进程的接口之间不会互相冲掉；实例进程的写入不经过这把锁，读到写之间（构造 ConfigModel 约几十毫秒）
    # 仍有空隙，上游别的写配置的接口也是这样。
    with _CONFIG_LOCK:
        cfg = ConfigModel(name)
        live = cfg.orchestra
        for key in _EDITABLE:
            setattr(live, key, getattr(candidate, key))
        cfg.save()
    logger.info(f'[{name}] 编排设置已保存，共 {len(live.rows)} 行')
    return {'ok': True, 'errors': []}


def _start_pending(live: Orchestra, progress: Progress) -> bool:
    """配置里有一条实例还没处理的开始命令。"""
    return live.command == 'start' and live.command_seq > progress.last_command_seq


def _skip_rest(name: str) -> bool:
    """记一条跳过休息命令，不启停实例。规格 §8.3 跳过休息仅在休息中可用：一行在跑时也记下的话，它会在这一行之后的休息里生效。"""
    try:
        progress = load_progress(progress_path(name))
    except OSError:
        return False   # 进度文件暂时读不了，不知道是不是在休息
    if progress.phase != 'resting':
        return False
    with _CONFIG_LOCK:
        cfg = ConfigModel(name)
        live = cfg.orchestra
        if _start_pending(live, progress):
            # 实例还没读到上一次的开始：新序列从第 1 行起，没有休息可跳，而且命令只有一格，会把 start 顶掉
            return False
        live.command = 'skip_rest'
        live.command_seq += 1
        cfg.save()
    return True


def _halt(name: str, state: str) -> bool:
    """暂停（state 为 paused）、停止（idle）在实例停下之后做的事：配置写入 state，并还原本行临时写入的参数；
    进度按 sync_controls 同步（暂停时记下剩余休息，停止时阶段回到 idle），清掉 saved_params 和等搭档的等待记录
    （会合记录不动）；暂停在本行第一次心跳之前的，心跳记为行开始时刻。

    先存配置、后存进度，同 OrchestraRuntime._restore_originals；个别字段还原不了只记错误，其余照常还原，
    同 OrchestraRuntime._restore。进度读不了（裁定 R2）或配置存不进去返回 False，配置与进度都不动，
    原值留在进度里，实例下次启动时引擎还原。配置存成了、进度没存成：暂停或停止已经生效，只记错误，返回 True，
    进度里留下的原值实例下次启动时引擎再还原一次，写回的是同样的值。
    """
    path = progress_path(name)
    try:
        p = load_progress(path)
    except OSError as exc:
        logger.error(f'[{name}] 编排进度文件读取失败（{path}）：{exc!r}，实例已停下，配置未改')
        return False
    with _CONFIG_LOCK:
        try:
            cfg = ConfigModel(name)
            live = cfg.orchestra
            live.state = state
            p = sync_controls(live, p, datetime.now())
            clear_team_wait(p)   # 实例停下了，不再等搭档；停止时 sync_controls 已清过，再清一次无妨
            if p.phase == 'row_running' and p.heartbeat_at is None:
                # 本行还没有心跳：已用截止到行开始，同引擎的 recover；不补的话面板按当前时刻算，暂停多久就涨多久
                p.heartbeat_at = p.row_started_at
            if p.saved_params:
                try:
                    restore_params(cfg, p.saved_params)
                except Exception as exc:
                    logger.error(f'[{name}] 编排：行参数有字段没能还原，其余已还原：{exc}')
            cfg.save()
        except OSError as exc:
            logger.error(f'[{name}] 编排配置读写失败：{exc!r}，实例已停下，进度未改，行参数的原值留待实例下次启动时还原')
            return False
    p.saved_params = {}   # p 是 sync_controls 返回的副本
    try:
        save_progress(path, p)
    except OSError as exc:
        logger.error(f'[{name}] 编排进度写入失败（{path}）：{exc!r}，配置已改，进度里的原值留待实例下次启动时再还原一次')
    return True


def _mark_running(name: str, start: bool) -> bool:
    """继续、开始在实例停下之后、启动之前做的事：state 写成 running；开始另记一条 start 命令（command_seq 加 1）。
    配置读写失败返回 False，调用方不启动实例。

    配置存成之后，尽力清掉进度里等搭档的等待记录（会合记录不动），进度的其余部分不动：继续时实例的引擎接着原来的进度跑，
    开始时凭新命令重置。进度文件读写失败只记警告，照常返回 True：实例启动后第一次选任务时自己会清。"""
    with _CONFIG_LOCK:
        try:
            cfg = ConfigModel(name)
            live = cfg.orchestra
            live.state = 'running'
            if start:
                # 进度停在序列中途（暂停、中断）时，光看 state 是 running，引擎会接着跑；凭这条新命令才从第 1 行重来
                live.command = 'start'
                live.command_seq += 1
            cfg.save()
        except OSError as exc:
            logger.error(f'[{name}] 编排配置读写失败：{exc!r}，实例已停下，没有启动')
            return False
    try:
        clear_team_wait_file(progress_path(name))   # 没有等待记录就不写，进度文件原样留着
    except OSError as exc:
        logger.warning(f'[{name}] 编排：清等待记录时进度文件读写失败：{exc!r}，实例启动后自己会清')
    return True


async def _control(name: str, action: str) -> bool:
    """开始、暂停、继续、停止：先停实例，停下之后在工作线程里写配置和进度，state 为 running 的写完再启动实例。
    调用方持 instance_lock。返回 False（配置读写失败，暂停、停止时还有进度读不了，见 _halt、_mark_running）时
    实例保持停下；继续、开始时进度里的等待记录清不掉，只记警告，实例照常启动。"""
    await stop_instance(name)   # 已停着就什么都不做：暂停、停止总要停下，开始、继续在跑才停
    state = _STATE_OF_ACTION[action]
    if state != 'running':
        return await asyncio.to_thread(_halt, name, state)
    if not await asyncio.to_thread(_mark_running, name, action == 'start'):
        return False
    await start_instance(name)
    return True


@orchestra_app.get('/{script_name}')
async def get_orchestra(script_name: str) -> dict:
    """编排设置、运行进度、任务目录，以及搭档的候选（其余实例名，排好序）。
    名字不是 config/ 下的实例返回 404，进度文件暂时读不了返回 503。"""
    return await asyncio.to_thread(_read_view, script_name)


@orchestra_app.put('/{script_name}')
async def put_orchestra(script_name: str, body: dict) -> dict:
    """保存编排表与设置项，校验不过就一个字都不写。body 里的 state、command、command_seq 一律忽略。"""
    return await asyncio.to_thread(_write_settings, script_name, body)


@orchestra_app.post('/{script_name}/command')
async def post_command(script_name: str, body: dict) -> dict:
    """控制命令，不接受或没做成返回 ok 为 False。实例名或动作不认识时什么都不碰。
    start、pause、resume、stop 先停实例，都清掉进度里等搭档的等待记录（会合记录不动）：pause 写 state=paused、
    stop 写 state=idle，都还原本行参数、同步进度，不再启动实例；resume 写 state=running，start 另记一条 start 命令
    （command_seq 加 1），进度的其余部分不动，写完启动实例。resume、start 清等待记录是尽力的：进度文件读写失败只记警告，
    照常启动实例，ok 仍为 True，实例第一次选任务时自己会清。
    skip_rest 不启停实例，只在实例的进度处于休息中、且没有还没处理的开始命令时接受，记一条 skip_rest 命令。"""
    action = body.get('action')
    if action not in _ACTIONS or script_name not in await asyncio.to_thread(instance_names):
        return {'ok': False}
    async with instance_lock(script_name):
        if action == 'skip_rest':
            ok = await asyncio.to_thread(_skip_rest, script_name)
        else:
            ok = await _control(script_name, action)
    if ok:
        logger.info(f'[{script_name}] 编排命令：{action}')
    return {'ok': ok}
