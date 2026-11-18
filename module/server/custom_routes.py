"""本分支新增路由的注册点。上游的 app.py 只在 include_router(tool_app) 之后调用 register_custom 一次，
之后各里程碑要加路由，只改这里。后台任务线程也经这里接入：app.py 在 on_startup 里调 start_custom_jobs，
它定义在 custom_jobs.py，这里只是转出，上游的 app.py 因此只多一个导入名。
"""
from fastapi import FastAPI

from module.server.custom_jobs import start_custom_jobs  # noqa: F401  转出给 app.py
from module.server.instance_control import instance_app
from module.server.orchestra_router import orchestra_app


def register_custom(app: FastAPI) -> None:
    app.include_router(orchestra_app)
    app.include_router(instance_app)
