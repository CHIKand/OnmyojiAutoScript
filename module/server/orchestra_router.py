"""编排的读写与控制接口，给 OASX 的编排页用：页面每 2 秒 GET 一次，保存用 PUT，按钮用 POST。

每个处理函数先核对实例名，只认 config/ 下已有的实例配置。进度文件的路径由名字直接拼出，
而 ConfigModel 读不到文件时会给默认值、一保存就新建配置文件，名字不核对就会碰到不该碰的路径。

配置只经 ConfigModel 读写，上游在 config/<名字>.json.lock 上加跨进程文件锁，实例进程与本接口不会同时写同一个文件。
同步的文件读写（配置、进度、目录列表）都放到工作线程里做，不卡事件循环。
本接口只改配置文件，不通知实例：实例等待下一个任务时每 5 秒查一次配置文件的修改时间（上游 Script.wait_until），
发现变化就重新选任务。
"""
import asyncio
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from module.config.config_model import ConfigModel
from module.logger import logger
from module.orchestra.mapping import catalog
from module.orchestra.model import Orchestra, validate_settings
from module.orchestra.progress import load_progress, progress_path, progress_to_dict
from module.server.instance_control import instance_names

orchestra_app = APIRouter(prefix='/custom/orchestra')

_NOT_FOUND = '实例不存在'
_EDITABLE = ('rows', 'after_finish', 'total_minutes_cap', 'default_rest_minutes', 'rest_jitter', 'resume_policy',
             'cut_in_tasks')   # PUT 能改的项；state、command、command_seq 只由命令接口和引擎写
_STATE_OF_ACTION = {'start': 'running', 'resume': 'running', 'pause': 'paused', 'stop': 'idle'}
_ACTIONS = (*_STATE_OF_ACTION, 'skip_rest')   # 用元组，action 是什么类型的值都能安全地比较


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
    if name not in instance_names():
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    settings = ConfigModel(name).orchestra.model_dump(mode='json')
    try:
        progress = load_progress(progress_path(name))
    except OSError as exc:   # 进度文件被占用，重试用尽仍读不了：页面 2 秒后会再来读
        raise HTTPException(status_code=503, detail='进度文件暂时无法读取') from exc
    return {'settings': settings, 'progress': progress_to_dict(progress, datetime.now()), 'catalog': catalog()}


def _write_settings(name: str, body: dict) -> dict:
    if name not in instance_names():
        return {'ok': False, 'errors': [{'index': -1, 'field': 'script_name', 'message': _NOT_FOUND}]}
    current = ConfigModel(name).orchestra
    accepted = {key: body[key] for key in _EDITABLE if key in body}
    try:
        candidate = Orchestra.model_validate({**current.model_dump(), **accepted})
    except ValidationError as exc:
        return {'ok': False, 'errors': _validation_errors(exc)}
    errors = validate_settings(candidate)
    if errors:
        return {'ok': False, 'errors': [asdict(error) for error in errors]}

    # 校验与写入之间实例可能改过 state、command_seq（引擎跑完一遍会把 state 置回 idle），
    # 所以写之前重读一次，只覆盖用户可改的项，运行控制字段保持读到的样子。
    # 配置的读与写各自加锁，读到写之间（构造 ConfigModel 约几十毫秒）仍有空隙，上游别的写配置的接口也是这样。
    cfg = ConfigModel(name)
    live = cfg.orchestra
    for key in _EDITABLE:
        setattr(live, key, getattr(candidate, key))
    cfg.save()
    logger.info(f'[{name}] 编排设置已保存，共 {len(live.rows)} 行')
    return {'ok': True, 'errors': []}


def _apply_command(name: str, action: object) -> dict:
    if name not in instance_names() or action not in _ACTIONS:
        return {'ok': False}
    cfg = ConfigModel(name)
    live = cfg.orchestra
    if action == 'skip_rest':
        live.command = 'skip_rest'
        live.command_seq += 1
    else:
        live.state = _STATE_OF_ACTION[action]
    cfg.save()
    logger.info(f'[{name}] 编排命令：{action}')
    return {'ok': True}


@orchestra_app.get('/{script_name}')
async def get_orchestra(script_name: str) -> dict:
    """编排设置、运行进度与任务目录。名字不是 config/ 下的实例返回 404，进度文件暂时读不了返回 503。"""
    return await asyncio.to_thread(_read_view, script_name)


@orchestra_app.put('/{script_name}')
async def put_orchestra(script_name: str, body: dict) -> dict:
    """保存编排表与设置项，校验不过就一个字都不写。body 里的 state、command、command_seq 一律忽略。"""
    return await asyncio.to_thread(_write_settings, script_name, body)


@orchestra_app.post('/{script_name}/command')
async def post_command(script_name: str, body: dict) -> dict:
    """控制命令。action 取 start、pause、resume、stop 改 state，取 skip_rest 则令 command_seq 加 1。"""
    return await asyncio.to_thread(_apply_command, script_name, body.get('action'))
