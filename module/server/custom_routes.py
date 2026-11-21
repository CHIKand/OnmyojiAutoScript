"""本分支新增路由的注册点。上游的 app.py 只在 include_router(tool_app) 之后调用 register_custom 一次，
之后各里程碑要加路由，只改这里。后台任务线程也经这里接入：app.py 在 on_startup 里调 start_custom_jobs，
它定义在 custom_jobs.py，这里只是转出，上游的 app.py 因此只多一个导入名。
"""
from fastapi import FastAPI

from module.server.assets_check_router import STATIC_PREFIX, WEB_DIR, NoCacheStaticFiles, assets_api, assets_page
from module.server.custom_jobs import start_custom_jobs  # noqa: F401  转出给 app.py
from module.server.feishu_router import feishu_app_router
from module.server.instance_control import instance_app
from module.server.orchestra_router import orchestra_app


def register_custom(app: FastAPI) -> None:
    app.include_router(orchestra_app)
    app.include_router(instance_app)
    app.include_router(assets_api)
    app.include_router(assets_page)
    app.include_router(feishu_app_router)
    if WEB_DIR.is_dir():        # 同 app.py 挂标注工具静态目录的做法：目录缺了不挡着服务启动，页面回 404
        app.mount(STATIC_PREFIX, NoCacheStaticFiles(directory=str(WEB_DIR)), name='assets_check_static')
