"""本分支新增路由的注册点。上游的 app.py 只在 include_router(tool_app) 之后调用 register_custom 一次，
之后各里程碑要加路由，只改这里。
"""
from fastapi import FastAPI

from module.server.instance_control import instance_app
from module.server.orchestra_router import orchestra_app


def register_custom(app: FastAPI) -> None:
    app.include_router(orchestra_app)
    app.include_router(instance_app)
