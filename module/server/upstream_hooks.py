"""合并上游时重接定制入口用的入口清单，以及把入口接到官方文本上的纯文本函数。

定制分支改过 7 个官方文件：script.py、module/config/config_model.py、module/config/config_menu.py、
module/notify/notify.py、module/server/app.py、module/server/home_router.py、deploy/git.py。这些改动全部写成 HOOKS 里的
入口：官方版本按清单接上入口，就逐字等于定制分支里的版本（tests/custom/test_upstream_hooks.py 用最近一次合进来的官方
版本把关）。用途：合并上游时代码冲突只落在这些文件里，就取官方版本，再按清单把入口重新接上，不用人手动处理。

规矩：这 7 个文件里的定制只能通过本清单来加。先在 HOOKS 里加入口，再让文件等于"官方版本 + 清单"。绕开清单直接改文件，
上面的测试会失败；重接前核对"共同祖先 + 清单"是否等于我们的版本时也对不上，自动处理只能整体中止。入口文件的范围从
清单里推出来（hook_files），不另外维护名单。

每条入口是一行定位行加一个动作。定位行是官方代码里的一整行原文，在官方版本里必须恰好出现一次；比较时只忽略行尾空白，
缩进必须相同（缩进变了说明官方改了代码结构，按找不到处理）。选定位行时还要让它在我们的版本里也只出现一次：插入类入口
看定位行本身，替换类入口看替换后的行。

只做文本处理，不碰 git，也不读写文件。只用标准库。
"""
import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass

_DEF_NAME = re.compile(r'\bdef\s+(\w+)')


@dataclass(frozen=True)
class Hook:
    """一条入口。

    path：相对仓库根，正斜杠。anchor：定位行整行原文，含缩进，不含换行符。
    action：after / before 在定位行之后 / 之前插入 lines；replace 用 lines 换掉定位行；replace_body 保留定位行
    （顶格的 def 行），把它下面的函数体换成 lines。函数体到下一处顶格、且不是 # 注释的非空行之前为止，末尾的空行和
    顶格注释不算，细节见 _body。
    lines：要放进去的行，每项一行，含缩进，不含换行符。
    """
    path: str
    anchor: str
    action: str
    lines: tuple[str, ...]


class HookError(Exception):
    """定位行在要接入的文本里不是恰好出现一次。str(e) 就是面向用户的原因，例如
    "script.py 的第 2 条入口找不到这一行：task = self.config.get_next()"。"""


@dataclass(frozen=True)
class OverriddenBody:
    """官方改过、接入时会被我们的版本整段覆盖的函数体。function 是 def 行里的函数名，
    diff 是共同祖先到官方的函数体差异（unified diff 文本）。"""
    path: str
    function: str
    diff: str


# 同一文件的入口按在文件里的位置从上到下排列：HookError 里的"第 n 条"按这个顺序数
HOOKS: tuple[Hook, ...] = (
    # script.py：编排挑任务和任务前后的钩子，飞书截图登记
    Hook('script.py', 'from module.ocr.rpc import ensure_ocr_server_started', 'after', (
        'from module.orchestra.runtime import orchestra_pick, orchestra_before_run, orchestra_after_run',
        'from module.notify.feishu_app import on_script_loop_start',
    )),
    Hook('script.py', '            task = self.config.get_next()', 'replace', (
        '            task = orchestra_pick(self)',
    )),
    # 留住任务对象，编排的心跳要读它的次数
    Hook('script.py', '            task_module.ScriptTask(config=self.config, device=self.device).run()', 'replace', (
        '            self.running_task_obj = task_module.ScriptTask(config=self.config, device=self.device)',
        '            self.running_task_obj.run()',
    )),
    Hook('script.py', "        logger.info(f'Start scheduler loop: {self.config_name}')", 'after', (
        '        on_script_loop_start(self)',
    )),
    # 下面两条共用一行定位行，紧贴着包住 self.run
    Hook('script.py', '            success = self.run(inflection.camelize(task))', 'before', (
        '            orchestra_before_run(self, inflection.camelize(task))',
    )),
    Hook('script.py', '            success = self.run(inflection.camelize(task))', 'after', (
        '            orchestra_after_run(self, inflection.camelize(task), success)',
    )),

    # module/config/config_model.py：orchestra 字段，接在官方字段列表的最后一个字段后面
    Hook('module/config/config_model.py', 'from module.logger import logger', 'after', (
        'from module.orchestra.model import Orchestra',
    )),
    Hook('module/config/config_model.py',
         '    budokai_tournament: BudokaiTournament = Field(default_factory=BudokaiTournament)', 'after', (
             '    orchestra: Orchestra = Field(default_factory=Orchestra)',
         )),

    # module/config/config_menu.py：菜单收敛，接在 __init__ 的最后一行后面
    Hook('module/config/config_menu.py',
         """        self.menu["Tools"] = ['Image Rule', 'Ocr Rule', 'Click Rule', 'Long Click Rule', 'Swipe Rule', 'List Rule']""",
         'after', (
             '        from module.config.config_menu_custom import custom_menu',
             '        self.menu = custom_menu(self.menu)',
         )),

    # module/notify/notify.py：provider 为 feishu_app 时委托给 FeishuAppNotifier
    Hook('module/notify/notify.py', '        self.config = config', 'after', (
        "        if config.get('provider') == 'feishu_app':",
        '            from module.notify.feishu_app import FeishuAppNotifier',
        '            self.delegate = FeishuAppNotifier(config, enable)',
        '            return',
    )),
    Hook('module/notify/notify.py', '        # 更新配置', 'before', (
        "        if getattr(self, 'delegate', None) is not None:",
        '            self.delegate.config_name = self.config_name',
        "            return self.delegate.push(kwargs.get('title', ''), kwargs.get('content') or kwargs.get('message', ''))",
    )),

    # module/server/app.py：注册定制路由，启动后台线程
    Hook('module/server/app.py', 'from module.server.tool_router import tool_app', 'after', (
        'from module.server.custom_routes import register_custom, start_custom_jobs',
    )),
    Hook('module/server/app.py', 'app.include_router(tool_app)', 'after', (
        'register_custom(app)',
    )),
    Hook('module/server/app.py', '        await mm.restart_processes(app.state.script_instances)', 'after', (
        '    start_custom_jobs()',
    )),

    # module/server/home_router.py：检查更新、执行更新两个函数体整段归我们，改调 fork_updater
    Hook('module/server/home_router.py', 'async def update_info():', 'replace_body', (
        '    from module.server.fork_updater import check_update',
        '    return await check_update()',
    )),
    Hook('module/server/home_router.py', 'async def execute_update():', 'replace_body', (
        '    from module.server.fork_updater import run_update',
        '    return await run_update()',
    )),

    # deploy/git.py：启动器守卫，HEAD 上有没推到任何远端的提交时跳过 reset/pull
    Hook('deploy/git.py', """        self.execute(f'"{self.git}" fetch {source} {branch}')""", 'after', (
        '        from deploy.fork_guard import WARNING_TEMPLATE, local_only_commits',
        '        unpushed = local_only_commits(self.git)',
        '        if unpushed:',
        '            logger.warning(WARNING_TEMPLATE.format(count=len(unpushed), branch=branch))',
        '            return',
    )),
)


def hook_files(hooks: Sequence[Hook] = HOOKS) -> list[str]:
    """清单里的文件路径，去重、排序。合并时按这份名单认"入口文件"。"""
    return sorted({hook.path for hook in hooks})


def apply_hooks(path: str, text: str, hooks: Sequence[Hook] = HOOKS) -> str:
    """把清单里 path 的全部入口接到 text（官方版本）上，返回接好的文本。清单里没有 path 的入口时原样返回 text。

    先在原始 text 上按清单顺序逐条找定位行，不是恰好出现 1 次就抛 HookError（报第一条不合格的）；全部找到才动手，
    所以每条入口的位置都按原始 text 算，互不影响。共用一行定位行的 before 和 after 分别落在这一行的前后。
    换行符：text 里有 \\r\\n 就用 \\r\\n，否则用 \\n；末尾有没有换行，保持原样。
    两条入口换掉的行有重叠，或者动作不认识，是清单本身写错了，不是官方改动造成的：抛 ValueError。
    """
    mine = [hook for hook in hooks if hook.path == path]
    if not mine:
        return text
    newline, lines = _split(text)
    edits = []
    for number, hook in enumerate(mine, 1):
        found = _find(lines, hook.anchor)
        if len(found) != 1:
            raise HookError(_reason(path, number, hook.anchor, len(found)))
        edits.append((*_span(hook, lines, found[0]), number, hook.lines))
    result, done = [], 0
    for start, end, _, new in sorted(edits):
        if start < done:
            raise ValueError(f'{path} 的入口清单里有两条入口改到了同一处')
        result += lines[done:start]
        result += new
        done = end
    result += lines[done:]
    return newline.join(result)


def overridden_bodies(path: str, base: str, theirs: str, hooks: Sequence[Hook] = HOOKS) -> list[OverriddenBody]:
    """接入时会被我们的版本覆盖、而官方改过的函数体：path 的每条 replace_body 入口，base（共同祖先）和
    theirs（官方）里的函数体不同就记一项，按清单顺序。定位行在任一边不是恰好出现一次时跳过，不报错（apply_hooks 会报）。"""
    found = []
    for hook in hooks:
        if hook.path != path or hook.action != 'replace_body':
            continue
        old, new = _body_lines(base, hook.anchor), _body_lines(theirs, hook.anchor)
        if old is None or new is None or old == new:
            continue
        diff = ''.join(difflib.unified_diff(old, new, fromfile='共同祖先', tofile='官方'))
        found.append(OverriddenBody(path, _function_name(hook.anchor), diff))
    return found


def _split(text: str) -> tuple[str, list[str]]:
    """按 text 自己的换行符拆行：有 \\r\\n 就按 \\r\\n 拆，否则按 \\n。返回 (换行符, 各行)；末尾有换行时最后一项是空串，
    用同一个换行符拼回去就是原文。"""
    newline = '\r\n' if '\r\n' in text else '\n'
    return newline, text.split(newline)


def _find(lines: list[str], anchor: str) -> list[int]:
    """定位行出现在哪几行：逐行比较，只忽略行尾空白，缩进必须相同。"""
    wanted = anchor.rstrip()
    return [index for index, line in enumerate(lines) if line.rstrip() == wanted]


def _reason(path: str, number: int, anchor: str, count: int) -> str:
    """定位行不是恰好出现一次时的原因文案。number 是该文件内从 1 起的入口序号；定位行去掉首尾空白后截到 60 个字符。"""
    shown = anchor.strip()[:60]
    if count == 0:
        return f'{path} 的第 {number} 条入口找不到这一行：{shown}'
    return f'{path} 的第 {number} 条入口这一行出现了 {count} 次：{shown}'


def _span(hook: Hook, lines: list[str], at: int) -> tuple[int, int]:
    """定位行在第 at 行时，这条入口要换掉的行的范围 [start, end)；start == end 表示在 start 处插入。"""
    if hook.action == 'after':
        return at + 1, at + 1
    if hook.action == 'before':
        return at, at
    if hook.action == 'replace':
        return at, at + 1
    if hook.action == 'replace_body':
        return _body(lines, at)
    raise ValueError(f'{hook.path} 的入口动作不认识：{hook.action}')


def _body(lines: list[str], at: int) -> tuple[int, int]:
    """第 at 行（def 行）下面的函数体范围 [start, end)。只看文本，不解析语法：
    从下一行开始往下，越过空行、以空白开头的行和顶格的 # 注释行，停在第一处非空、既不以空白开头也不以 # 开头的行之前；
    再从末尾往回，末尾的空行（含只有空白的行）和顶格的 # 注释行不算函数体，它们属于下一段代码，原样留着。
    所以函数体中间按第一列注释掉的代码算函数体，随函数体一起换掉。函数体里的多行字符串或括号续行如果有顶格、
    不以 # 开头的行，范围会在那里提前结束。只适用于顶格的函数：方法的 def 行有缩进，这样算会越过函数末尾。"""
    start = end = at + 1
    while end < len(lines) and (not lines[end] or lines[end][0].isspace() or lines[end][0] == '#'):
        end += 1
    while end > start and (not lines[end - 1].strip() or lines[end - 1][0] == '#'):
        end -= 1
    return start, end


def _body_lines(text: str, anchor: str) -> list[str] | None:
    """text 里定位行那个函数的函数体，每项一行、以 \\n 结尾（给 difflib 用）；定位行不是恰好出现一次时返回 None。"""
    _, lines = _split(text)
    found = _find(lines, anchor)
    if len(found) != 1:
        return None
    start, end = _body(lines, found[0])
    return [line + '\n' for line in lines[start:end]]


def _function_name(anchor: str) -> str:
    """def 行里的函数名；不是 def 行时退回定位行本身（去掉首尾空白）。"""
    matched = _DEF_NAME.search(anchor)
    return matched.group(1) if matched else anchor.strip()
