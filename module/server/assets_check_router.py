"""素材维护页的接口和页面：只做 HTTP 映射，业务逻辑都在 assets_check.py。

接口在 /custom/assets 下（assets_api）；页面 GET /tool/assets-check 写全路径挂在不带前缀的 assets_page 上，
上游的 tool_router.py 不动。页面的静态文件挂在 /custom/assets/static（custom_routes 里挂载），响应加 Cache-Control: no-cache，
理由同 app.py 的 NoCacheStaticFiles；不从 app.py 导入它，因为 app.py 导入了 custom_routes，会循环导入。

业务接口都是普通 def，FastAPI 把它们放进线程池跑：git、联网、cv2、读写素材都不卡事件循环。
清单、静态检查、体检一律用 root=None，即服务进程的当前目录（仓库根），同标注工具的 PROJECT_ROOT；
静态检查里 assets.py 的比对也只在当前目录就是仓库根时才做。
要 git 的接口（保存标签、上传替换、提交并推送、导出补丁）每次请求经 _runner_and_branch 现取运行目录的 git 和 fork 分支，
做法同 M3a 的 ForkUpdater()（部署配置里的 GitExecutable、Branch，仓库是当前目录）；测试换掉这个函数，不构造 DeployConfig。
上传替换换完图立刻提交并推送（assets_check.replace_and_commit），换下的图不留在运行目录里等启动器还原。

错误一律回 {"detail": {"code", "message"}}，同标注工具：ValueError 回 400；WriteRefused（正在合并或保存、仓库里有没做完的合并）
回 409，按类判断，别的 RuntimeError 不算"忙"；标注工具的 AnnotatorError 回它自己的状态码；其余异常回 500，消息是异常的说明，
调用栈记进日志。保存标签、上传替换、提交并推送的结果（含失败原因 error）照常以 200 返回，由页面显示。

module.server.tool 只在体检接口里用到时才导入：它一导入就起标注工具的会话清理线程，还会带进设备、配置等一串模块，
而 custom_routes 导入本模块时要保持轻量（见 test_importing_the_modules_does_not_load_the_manager）。服务启动时 app.py 早已
导入过它（tool_router），PROJECT_ROOT 按启动时的当前目录定好，这里取到的就是同一个对象。
"""
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.staticfiles import StaticFiles

from module.logger import logger
from module.server import assets_check
from module.server.assets_check import WriteRefused
from module.server.fork_git import GitRunner
from module.server.fork_updater import ForkUpdater

WEB_DIR = Path(__file__).resolve().parent / 'web' / 'assets_check'
STATIC_PREFIX = '/custom/assets/static'
EMPTY_SCREEN_HINT = '所选画面没有已标注的图片或 OCR 条目'
COMMIT_FIRST_TEXT = '先提交，再导出补丁包'
EXPORT_DIR_PREFIX = 'oas-assets-patch-'

assets_api = APIRouter(prefix='/custom/assets')
assets_page = APIRouter()


class ScreenCheckBody(BaseModel):
    session_id: str
    image_id: str
    screen: str


class ReplaceImageBody(BaseModel):
    json_relpath: str
    image_name: str
    content_base64: str


class CommitPushBody(BaseModel):
    message: str = ''


class NoCacheStaticFiles(StaticFiles):
    """静态文件响应加 Cache-Control: no-cache：浏览器仍可缓存，但每次回源校验，页面更新后不会继续跑缓存里的旧脚本。
    同 module.server.app.NoCacheStaticFiles，那边写了原因。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers['Cache-Control'] = 'no-cache'
        return response


def _runner_and_branch() -> tuple[GitRunner, str]:
    """运行目录的 git 和 fork 分支：每次现读部署配置，同 M3a 的 ForkUpdater()。测试换掉这个函数。"""
    updater = ForkUpdater()
    return updater.runner, updater.branch


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={'code': code, 'message': message})


@contextmanager
def _mapped_errors(action: str):
    """把业务层的异常换成 HTTP 错误（规则见模块说明）。action 只用于日志。"""
    try:
        yield
    except HTTPException:
        raise
    except WriteRefused as exc:
        raise _http_error(409, 'write_refused', str(exc)) from exc
    except ValueError as exc:
        raise _http_error(400, 'invalid_request', str(exc)) from exc
    except Exception as exc:
        logger.exception(f'素材维护：{action}失败：{exc!r}')
        raise _http_error(500, 'internal_error', str(exc) or type(exc).__name__) from exc


@assets_api.get('/tasks')
def get_tasks(all_tasks: Annotated[bool, Query(alias='all')] = False) -> dict:
    """可以维护的任务：默认只有两个限时活动，all=1 时是全部任务。"""
    with _mapped_errors('读取任务列表'):
        return {'tasks': assets_check.list_tasks(all_tasks)}


@assets_api.get('/{task}/rules')
def get_rules(task: str) -> dict:
    """任务的素材清单，条目带上标签（periodic、screen）；screens 是这个任务可选的画面，页面的画面下拉框用它。"""
    with _mapped_errors('读取清单'):
        rules = assets_check.list_rules(task)
        return {'task': task, 'screens': list(assets_check.screens_of(task)), 'rules': rules}


@assets_api.get('/{task}/static-check')
def get_static_check(task: str) -> dict:
    """不需要截图的检查：缺图、json 写坏、重名、ROI 越界、assets.py 过期。"""
    with _mapped_errors('静态检查'):
        return {'issues': [asdict(issue) for issue in assets_check.static_check(task)]}


@assets_api.get('/tags')
def get_tags() -> dict:
    """整份标签文件，没有文件时是 {}。"""
    with _mapped_errors('读取标签'):
        return assets_check.load_tags()


@assets_api.put('/tags')
def put_tags(tags: dict) -> dict:
    """整份写回标签文件，立刻提交并推送到 fork。返回 {'commit', 'pushed', 'error'}，失败原因在 error 里。"""
    with _mapped_errors('保存标签'):
        runner, branch = _runner_and_branch()
        return assets_check.commit_tags(tags, runner, branch)


@assets_api.post('/{task}/screen-check')
def post_screen_check(task: str, body: ScreenCheckBody) -> dict:
    """用标注工具会话里的截图给任务的图片和 OCR 条目逐条打分。先核对会话和截图：会话过期时整体回一个错误，
    而不是每一行都带同一个错误。所选画面上没有任何条目时 results 为空，hint 给出提示。"""
    from module.server.tool import AnnotatorError, annotator_manager
    with _mapped_errors('体检'):
        try:
            annotator_manager.get_image_file(body.session_id, body.image_id)
        except AnnotatorError as exc:
            raise _http_error(exc.status_code, exc.code, exc.message) from exc
        results = assets_check.screen_check(task, body.screen, body.session_id, body.image_id)
    return {'results': results, 'hint': '' if results else EMPTY_SCREEN_HINT}


@assets_api.post('/{task}/replace-image')
def post_replace_image(task: str, body: ReplaceImageBody) -> dict:
    """用上传的图片替换素材图、重新生成 assets.py，立刻提交并推送到 fork。返回
    {'target', 'replaced', 'commit', 'pushed', 'error'}，没替换成或没推上去的原因在 error 里；参数不对回 400。"""
    with _mapped_errors('替换图片'):
        runner, branch = _runner_and_branch()
        return assets_check.replace_and_commit(task, body.json_relpath, body.image_name, body.content_base64, runner,
                                               branch)


@assets_api.post('/{task}/commit-push')
def post_commit_push(task: str, body: CommitPushBody) -> dict:
    """提交 tasks/<task> 下的改动并推送到 fork。返回 {'commit', 'pushed', 'error'}，失败原因在 error 里。"""
    with _mapped_errors('提交并推送'):
        runner, branch = _runner_and_branch()
        return assets_check.commit_and_push(task, body.message, runner, branch)


@assets_api.get('/{task}/export')
def get_export(task: str, commit: str = '') -> FileResponse:
    """把 commit 新增或修改的文件打成 zip 下载。zip 写在新建的临时目录里，发完由后台任务删掉；导出失败当场删掉。"""
    with _mapped_errors('导出补丁包'):
        assets_check.task_root(task)
        if not commit.strip():
            raise _http_error(400, 'missing_commit', COMMIT_FIRST_TEXT)
        runner, _ = _runner_and_branch()
        scratch = Path(tempfile.mkdtemp(prefix=EXPORT_DIR_PREFIX))
        try:
            patch = assets_check.export_patch(commit.strip(), runner, scratch)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
    return FileResponse(patch, media_type='application/zip', filename=patch.name,
                        background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True))


@assets_page.get('/tool/assets-check')
async def assets_check_page():
    """素材维护页。"""
    page = WEB_DIR / 'index.html'
    if not page.is_file():
        raise _http_error(404, 'page_not_found', '素材维护页面不存在')
    return FileResponse(page, headers={'Cache-Control': 'no-cache'})
