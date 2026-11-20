"""素材维护页的业务逻辑：任务清单、静态检查、标签文件、按画面体检，以及换图、提交推送、导出补丁。

清单、静态检查、体检只读素材、做判断，不碰 git，也不改素材。root 是仓库根目录（任务在 root/tasks/ 下），不传就取进程的当前目录，
与标注工具的 PROJECT_ROOT 一致；测试传临时目录，不依赖全局状态。

清单和检查看到的 json，与 dev_tools/assets_extract.py 编进 assets.py 的完全一致：直接用 AssetsExtractor.all_json_file，
连它对路径里 temp 的过滤也照搬。所以清单里有的条目，assets.py 里就该有对应的属性。
tasks/Component 同标注工具整个算一个任务（json 相对路径是 <组件>/x.json），但每个组件各编各的 assets.py：重名、assets.py 比对、
换图后的重新生成都按组件目录分开做（见 _extract_root）。
assets.py 与 json 是否一致靠"试生成"判断：继承 AssetsExtractor，只把写盘的 write_file 换成空操作，其余全走 extract 自己的流程，
再和磁盘上的 assets.py 比对。AssetsExtractor 按当前目录算图片的相对路径，所以当前目录不是 root 时不比对，只记一条警告。
不在服务进程里 os.chdir：进程是各线程共用的。
dev_tools.assets_extract 依赖 numpy 和 tqdm，用到时才导入。文件类型的判断借用标注工具的 AnnotatorManager._detect_rule_type
（页面上的类型名和后面的单条打分都按它的口径），module.server.tool 同样用到时才导入：它导入时按当前目录定出 PROJECT_ROOT，
服务启动时早已导入过，别在换过当前目录之后第一次导入它。
标签文件随 fork 提交，结构是 {任务: {json 相对路径: {条目名: {"periodic": 布尔, "screen": 画面名}}}}。
没有标签的条目取默认值（periodic 为假，画面为"未分类"）；标签文件里已经不存在的条目、文件、任务，清单一概忽略。
写入这一半（replace_image、replace_and_commit、commit_and_push、commit_tags）与合并上游共用 fork_updater.MERGE_LOCK：
merge_upstream 不可重入，它的中止流程还会把合并窗口里的改动登记进索引再丢掉，所以写入要等合并让出锁。拿锁不阻塞，拿不到立刻
拒绝，免得占住线程池里的工作线程等上几分钟；仓库里留着没做完的合并（MERGE_HEAD）时同样拒绝。replace_image 不碰 git，只写文件、
重新生成 assets.py；页面上的上传替换走 replace_and_commit，换完立刻提交并推送：没提交的图会被启动器的 git reset --hard 还原。
提交推送只 add 任务目录（保存标签只 add 标签文件），先拉取 fork：fork 上有运行目录没有的提交就一个提交都不做，免得造出
推不上去、又被启动器守卫钉死在运行目录里的提交。导出补丁用 git archive，二进制内容不经过 GitRunner.run 的 UTF-8 解码。
"""
import base64
import json
import math
import os
import re
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from module.logger import logger
from module.server import fork_updater
from module.server.fork_git import (FORK_REMOTE, GitRunner, _count, _fetch, _fork_state, _ref_exists, commit_paths,
                                    push)

DEFAULT_TASKS = ('ActivityShikigami', 'MetaDemon')      # 页面默认只列这两个限时活动，可以切换成全部任务
CONTAINER_TASK = 'Component'                            # 组件的容器目录：整个算一个任务，每个组件 Component/<组件> 各编各的 assets.py
UNCLASSIFIED = '未分类'
SCREENS = ('活动主页', '普通挑战页', 'Boss挑战页', '战斗中', '退出时', UNCLASSIFIED)
EXTRA_SCREENS = {'MetaDemon': ('超鬼王主页', '超鬼王战斗', '合成页')}     # 个别任务多出来的画面
FRAME_WIDTH, FRAME_HEIGHT = 1280, 720                   # 模拟器画面尺寸，ROI 以它为界
TAGS_RELPATH = Path('assets') / 'custom' / 'assets_tags.json'
SCRATCH_SUFFIX = '.assets-check.tmp'                    # 原子写入的临时文件：<文件名>.assets-check.tmp
SCRATCH_PATHSPEC = f':(exclude)*{SCRATCH_SUFFIX}'       # 提交时排除临时文件（git 路径规格里 * 也匹配 /，哪一层都排除）
_ROI_FIELDS = ('roiFront', 'roiBack')
_ATTRIBUTE = re.compile(r'^\t([A-Z]+_[A-Z0-9_]+) =', re.MULTILINE)      # assets.py 里的属性名
_NAMES_SHOWN = 10                                       # 说明里最多列出几个属性名
SCORED_TYPES = ('image', 'ocr')                         # 体检只给这两类条目打分，别的类型没有"在画面上匹配"一说
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.bmp')      # 允许替换的图片后缀（标注工具额外放行的 .webp 这里不收）
TAGS_COMMIT_MESSAGE = '素材维护：更新标签'
BUSY_TEXT = '正在合并上游或保存素材，请稍后再试'
MERGING_TEXT = '仓库里有未完成的上游合并，请先在 OASX 更新页点"执行更新"处理，再保存素材'
FORK_AHEAD_TEXT = ('fork 上有 {n} 个运行目录还没有的提交，现在提交会和 fork 分叉：请先在 OASX 重启服务同步，'
                   '再重新替换并提交（重启会还原未提交的改动）')
DIVERGED_TEXT = '运行目录和 fork 已经分叉，需要人工处理，这次没有提交'
FETCH_FAILED_TEXT = '拉取 fork 失败，没有推送：{reason}。已提交的改动留在本地，启动器不会冲掉，联网后再点"提交并推送"'
PUSH_FAILED_TEXT = '推送失败：{reason}。改动已在本地提交，可以稍后再点"提交并推送"重试'
GIT_FAILED_TEXT = 'git 命令失败：{reason}'
UNKNOWN_REASON = '原因不明'


@dataclass(frozen=True)
class Issue:
    """静态检查发现的一个问题。

    kind：missing_image / json_error / duplicate_item / roi_out_of_bounds / assets_outdated。
    file：json 相对任务目录的路径（正斜杠），Component 的是 <组件>/...；assets_outdated 是 assets.py（组件是 <组件>/assets.py）；
    试生成失败的 json_error 不针对某个文件，普通任务为空，组件是组件目录名。
    item：条目名；不针对某个条目时为空，列表文件整体的问题用它顶层的 name。
    detail：给人看的说明。
    """
    kind: str
    file: str
    item: str
    detail: str


# ------------------------------------------------------------------------------------------------------------------
# 任务
# ------------------------------------------------------------------------------------------------------------------

def _repo_root(root: Path | None) -> Path:
    return (Path(root) if root is not None else Path.cwd()).resolve()


def list_tasks(all_tasks: bool = False, root: Path | None = None) -> list[str]:
    """可以做素材维护的任务名：tasks/ 下的一层子目录，不含以 . 或 _ 开头的（__pycache__ 之类）。

    Component 同标注工具，整个算一个任务（json 的相对路径是 <组件>/x.json），见 _extract_root。
    all_tasks 为假时只留 DEFAULT_TASKS 里目录还在的，顺序同 DEFAULT_TASKS；为真时是全部，按名字排序。
    """
    tasks_dir = _repo_root(root) / 'tasks'
    if not tasks_dir.is_dir():
        return []
    names = sorted(entry.name for entry in tasks_dir.iterdir()
                   if entry.is_dir() and not entry.name.startswith(('.', '_')))
    return names if all_tasks else [name for name in DEFAULT_TASKS if name in names]


def task_root(task: str, root: Path | None = None) -> Path:
    """任务目录 root/tasks/<task>。

    task 必须是 list_tasks 列得出来的名字：路径穿越（../etc）、带路径分隔符的名字（Component/<组件> 也不行）、不存在的目录
    都抛 ValueError，HTTP 接口据此回 4xx。拿目录清单当白名单比对，而不是拼路径再检查，名字里的任何花样都进不来。
    """
    if task not in list_tasks(all_tasks=True, root=root):
        raise ValueError(f'未知任务：{task!r}')
    return _repo_root(root) / 'tasks' / task


def screens_of(task: str) -> tuple[str, ...]:
    """任务可选的画面：通用的六个，加上该任务独有的（超鬼王多三个）。"""
    return SCREENS + EXTRA_SCREENS.get(task, ())


# ------------------------------------------------------------------------------------------------------------------
# 标签
# ------------------------------------------------------------------------------------------------------------------

def tags_path(root: Path | None = None) -> Path:
    """标签文件的位置：<root>/assets/custom/assets_tags.json。"""
    return _repo_root(root) / TAGS_RELPATH


def load_tags(path: Path | None = None) -> dict:
    """读标签文件，不传 path 就读当前目录下的。文件不存在返回 {}。

    文件损坏（不是 JSON、顶层不是对象）抛 ValueError，不当成空表：否则下一次保存会悄悄把它覆盖掉。
    """
    path = Path(path) if path is not None else tags_path()
    try:
        tags = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except ValueError as exc:       # JSONDecodeError 和 UnicodeDecodeError 都是它的子类
        raise ValueError(f'标签文件损坏：{path}（{exc}）') from exc
    if not isinstance(tags, dict):
        raise ValueError(f'标签文件损坏：{path}（顶层应当是对象）')
    return tags


def _write_atomically(path: Path, data: bytes) -> None:
    """把 data 写成 path：先写同目录的临时文件 <文件名>.assets-check.tmp，再 os.replace 换上去。写到一半出错或断电，原文件
    原样还在；出错时临时文件也清掉。目录不存在就先建。保存标签和换图共用。

    临时文件名固定（同一时刻只有一个写入者，见 _write_guard）：上次写到一半进程被杀留下的同名残渣，这次先删掉再新建；
    别的文件留下的残渣提交时也带不进去，_commit_and_push 用 SCRATCH_PATHSPEC 把这类文件排除在外。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + SCRATCH_SUFFIX)
    scratch.unlink(missing_ok=True)
    try:
        with open(scratch, 'xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(scratch, path)
    except BaseException:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        raise


def save_tags(tags: dict, path: Path | None = None) -> None:
    """整份写回标签文件：UTF-8、不转义中文、缩进 2、末尾换行、LF 换行，键序照传入的。原子写入，见 _write_atomically。"""
    path = Path(path) if path is not None else tags_path()
    text = json.dumps(tags, ensure_ascii=False, indent=2) + '\n'     # 先序列化：内容不合法时不留下临时文件
    _write_atomically(path, text.encode('utf-8'))


def _tag_of(tags: dict, task: str, file: str, name) -> dict:
    """标签文件里某个条目的标签；没有，或者格式不对（不是对象、periodic 不是布尔、screen 为空），对应字段用默认值。"""
    files = tags.get(task)
    items = files.get(file) if isinstance(files, dict) else None
    tag = items.get(name) if isinstance(items, dict) and isinstance(name, str) else None
    tag = tag if isinstance(tag, dict) else {}
    screen = tag.get('screen')
    return {'periodic': tag.get('periodic') is True, 'screen': screen if isinstance(screen, str) and screen else UNCLASSIFIED}


# ------------------------------------------------------------------------------------------------------------------
# 清单
# ------------------------------------------------------------------------------------------------------------------

def _extractor():
    """dev_tools.assets_extract 模块，用到时才导入（它依赖 numpy 和 tqdm）。"""
    from dev_tools import assets_extract
    return assets_extract


def _relpath(path: Path, task_dir: Path) -> str:
    return path.relative_to(task_dir).as_posix()


def _extract_root(task_dir: Path, json_file: Path) -> Path | None:
    """json_file 编进哪个目录的 assets.py（AssetsExtractor 的 task_path）。

    普通任务是任务目录本身。Component 是 json 所在的组件目录 tasks/Component/<组件>（同标注工具的
    _resolve_assets_extract_target，上游的 AllAssetsExtractor 也是一个组件一个 assets.py）；直接放在 tasks/Component 下的 json
    不属于任何组件，不会编进哪个 assets.py，标注工具也拒绝保存它，返回 None。
    """
    if task_dir.name != CONTAINER_TASK:
        return task_dir
    parts = json_file.relative_to(task_dir).parts
    return task_dir / parts[0] if len(parts) > 1 else None


def _json_files(task_dir: Path) -> list[Path]:
    """AssetsExtractor 会编进 assets.py 的 json 文件，按相对路径排序。不属于任何组件的 json（见 _extract_root）不收。"""
    files = [Path(file) for file in _extractor().AssetsExtractor(str(task_dir)).all_json_file()]
    return sorted((file for file in files if _extract_root(task_dir, file) is not None),
                  key=lambda path: _relpath(path, task_dir))


def _read_rules(path: Path) -> tuple[list | dict | None, str]:
    """读一个素材 json，返回 (数据, 错误说明)，出错时数据为 None、说明非空。

    编码与 AssetsExtractor.read_file 一致：UTF-8，不吞 BOM。顶层不是列表或对象的文件，AssetsExtractor 也会丢掉，这里同样算错。
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:        # JSONDecodeError 和 UnicodeDecodeError 都是 ValueError 的子类
        return None, f'{type(exc).__name__}: {exc}'
    if not isinstance(data, (list, dict)):
        return None, f'顶层应当是列表或对象，实际是 {type(data).__name__}'
    return data, ''


def _is_list_file(data) -> bool:
    """列表文件：带 name 的对象，条目在它的 list 里（判法同 AssetsExtractor.is_list_file）。"""
    return isinstance(data, dict) and 'name' in data


def _entries(data) -> list[dict]:
    """json 里的条目：列表文件取 data['list']，其余文件顶层就是条目列表；不是对象的元素跳过（标注工具也这么处理）。"""
    entries = data.get('list') if _is_list_file(data) else data
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _name_of(entry: dict) -> str:
    name = entry.get('itemName')
    return name if isinstance(name, str) else ''


def _annotator_rule_type(data) -> str:
    """标注工具对文件类型的叫法：image / ocr / click / long_click / swipe / list，认不出来是 unknown。"""
    from module.server.tool import AnnotatorManager
    return AnnotatorManager._detect_rule_type(data)


def list_rules(task: str, root: Path | None = None) -> list[dict]:
    """任务的素材清单：每个 json 文件一项 {'file', 'rule_type', 'items'}，按 file 排序。

    file 是相对任务目录的路径（正斜杠）。items 是文件里的条目（列表文件取 list 里的），每条带上 json 里的全部字段，
    再加 periodic（每期必换）和 screen（所在画面）；没有标签的是 False 和"未分类"。标签文件里已经不存在的条目一概忽略，
    清单只按 json 的实际内容输出。解析不了的文件照样列出：类型 unknown，没有条目（静态检查会把它报成 json_error）。
    标签文件损坏时抛 ValueError（见 load_tags）。
    """
    task_dir = task_root(task, root)
    tags = load_tags(tags_path(root))
    rules = []
    for path in _json_files(task_dir):
        file = _relpath(path, task_dir)
        data, _ = _read_rules(path)
        rule_type = _annotator_rule_type(data)
        entries = [] if rule_type == 'unknown' else _entries(data)
        rules.append({'file': file, 'rule_type': rule_type,
                      'items': [{**entry, **_tag_of(tags, task, file, entry.get('itemName'))} for entry in entries]})
    return rules


# ------------------------------------------------------------------------------------------------------------------
# 静态检查
# ------------------------------------------------------------------------------------------------------------------

def _entry_images(data) -> list[tuple[dict, object]]:
    """json 里每个条目运行时加载的图片：[(条目, 图片名)]，图片名相对 json 所在目录。

    普通文件是条目的 imageName，原样返回（json 写坏时不一定是字符串）：ImageExtractor 把它编成 file="./<json 目录>/<imageName>"。
    列表文件只有 type 为 image 时有图：ListExtractor 把 json 所在目录编成 RuleList 的 folder，RuleList 按 <folder>/<itemName>.png
    加载，条目里就算有 imageName 它也不看；没有 itemName 的条目跳过。
    """
    entries = _entries(data)
    if _is_list_file(data):
        if data.get('type') != 'image':
            return []
        return [(entry, f'{name}.png') for entry in entries if (name := _name_of(entry))]
    return [(entry, entry['imageName']) for entry in entries if 'imageName' in entry]


def _missing_images(file: str, path: Path, data) -> list[Issue]:
    """条目运行时要加载的图片（见 _entry_images）不在磁盘上。图片路径按 json 所在目录算，和 assets.py 里的一致。"""
    issues = []
    for entry, image in _entry_images(data):
        image_name = str(image)
        if not (path.parent / image_name).is_file():
            shown = (PurePosixPath(file).parent / image_name).as_posix()
            issues.append(Issue('missing_image', file, _name_of(entry), f'图片不存在：{shown}'))
    return issues


def _roi_problem(value) -> str | None:
    """ROI 字符串 x,y,w,h 的问题；没有问题返回 None。

    四个数按小数算：标注工具保存裁剪框时会写出 232.5 这样的值（它自己的 _parse_roi 按 float 解析），真实素材里也有，
    运行时 expand_roi 会取整，不是错误。
    """
    try:
        x, y, w, h = [float(part) for part in value.split(',')]
    except (AttributeError, ValueError):        # 不是字符串，不是数字，或者不是四个数（解包个数不对也抛 ValueError）
        return f'格式不对，应为 x,y,w,h 四个数字：{value!r}'
    if not all(math.isfinite(number) for number in (x, y, w, h)):       # nan 和 inf 与任何边界比较都没有意义
        return f'格式不对，应为 x,y,w,h 四个数字：{value!r}'
    if x < 0 or y < 0 or x + w > FRAME_WIDTH or y + h > FRAME_HEIGHT:
        return f'{value} 超出 {FRAME_WIDTH}×{FRAME_HEIGHT}'
    return None


def _roi_issues(file: str, data, entries: list[dict]) -> list[Issue]:
    """条目上出现的 roiFront、roiBack 都要是 x,y,w,h 四个数字，且落在画面以内；列表文件顶层的 roiBack 同样检查。
    格式不对的也算在 roi_out_of_bounds 下，说明里写明是格式问题。"""
    owners = [(_name_of(entry), entry) for entry in entries]
    if _is_list_file(data):
        name = data['name']
        owners.insert(0, (name if isinstance(name, str) else '', data))
    issues = []
    for name, owner in owners:
        for field in _ROI_FIELDS:
            if field in owner and (problem := _roi_problem(owner[field])):
                issues.append(Issue('roi_out_of_bounds', file, name, f'{field} {problem}'))
    return issues


def _attributes(data) -> list[tuple[str, str]]:
    """一个 json 文件编进 assets.py 后产生的属性：[(属性名, 条目名)]。

    类型前缀和判断顺序沿用 AssetsExtractor.extract：先判列表文件（整个文件只有一个 L_ 属性，名字取顶层的 name），
    再依次是 image、click、long_click、swipe、ocr；名字过一遍它的 name_transform。长按和列表的前缀都是 L_，会撞在一起；
    同名条目在不同类型的文件里（I_ 和 O_）不算冲突。认不出类型的文件不产生属性。
    """
    extract = _extractor()
    extractor = extract.AssetsExtractor
    if not data:
        return []
    if _is_list_file(data):
        name = data['name']
        return [(f'L_{extract.name_transform(name)}', name)] if data.get('list') and isinstance(name, str) else []
    kinds = ((extractor.is_image_file, 'I'), (extractor.is_click_file, 'C'), (extractor.is_long_click_file, 'L'),
             (extractor.is_swipe_file, 'S'), (extractor.is_ocr_file, 'O'))
    try:
        prefix = next((prefix for is_kind, prefix in kinds if is_kind(data)), None)
    except (KeyError, TypeError):       # 顶层是没有 name 的对象，或第一个元素不是对象：AssetsExtractor 自己也会在这里抛异常
        return []
    if prefix is None:
        return []
    return [(f'{prefix}_{extract.name_transform(name)}', name) for name in map(_name_of, _entries(data)) if name]


def _duplicate_issues(places: dict[tuple[Path, str], list[tuple[str, str]]]) -> list[Issue]:
    """同一个 assets.py 里同一个属性名出现在两处以上（同一个文件里的两条也算）：互相覆盖。places 的键是
    (编进哪个目录的 assets.py, 属性名)，所以不同组件里的同名属性不算。每一处各报一条，说明里列出全部位置。"""
    issues = []
    for (_, attribute), found in places.items():
        if len(found) < 2:
            continue
        where = '、'.join(f'{file} 的 {name}' for file, name in found)
        detail = f'属性 {attribute} 出现 {len(found)} 次（{where}），编进 assets.py 后互相覆盖'
        issues += [Issue('duplicate_item', file, name, detail) for file, name in found]
    return issues


def _listed(names: list[str]) -> str:
    shown = '、'.join(names[:_NAMES_SHOWN])
    return shown + (f' 等 {len(names)} 项' if len(names) > _NAMES_SHOWN else '')


def _trial_assets(task_dir: Path) -> tuple[str, Path]:
    """按 AssetsExtractor.extract 的流程在内存里生成 assets.py 的内容，返回 (内容, assets.py 的路径)，不写盘。

    继承 AssetsExtractor，只把写盘的 write_file 换成空操作：文件清单、判断顺序、各个 Extractor、没有素材时的 pass 兜底
    都由 extract 自己决定，上游改了生成规则，试生成跟着变，不会因为这里另抄一份逻辑而对不上。
    extract 在调用 write_file 时，要写的内容就在 _result 里。
    """
    class DryRun(_extractor().AssetsExtractor):
        def write_file(self) -> None:
            pass

    extractor = DryRun(str(task_dir))
    extractor.extract()
    return extractor._result, extractor.assets_file


def _extract_dirs(task_dir: Path, json_files: list[Path]) -> list[Path]:
    """要比对 assets.py 的目录，按相对路径排序：普通任务是任务目录本身；Component 是有 json 或有 assets.py 的组件目录，
    两样都没有的目录（比如 __pycache__）没有可比的，视为一致。"""
    if task_dir.name != CONTAINER_TASK:
        return [task_dir]
    dirs = {_extract_root(task_dir, path) for path in json_files}
    dirs |= {entry for entry in task_dir.iterdir() if entry.is_dir() and (entry / _extractor().ASSETS_FILE).is_file()}
    return sorted(dirs, key=lambda path: _relpath(path, task_dir))


def _outdated_issues(task_dir: Path, extract_dirs: list[Path], root: Path | None) -> list[Issue]:
    """extract_dirs 里每个目录的 assets.py 与 json 试生成的结果是否一致（见 _outdated_issue）。当前目录不是 root 时一个都不比，
    记一条警告。"""
    repo = _repo_root(root)
    if repo != Path.cwd().resolve():
        logger.warning(f'素材检查：当前目录不是 {repo}，AssetsExtractor 按当前目录算图片路径，跳过 assets.py 与 json 的比对')
        return []
    return [issue for extract_dir in extract_dirs for issue in _outdated_issue(task_dir, extract_dir)]


def _outdated_issue(task_dir: Path, extract_dir: Path) -> list[Issue]:
    """extract_dir 的 assets.py 与 json 试生成的结果不一致：属性名对不上的列出缺哪些、多哪些，属性名都对得上就说内容有差异；
    assets.py 不存在也算。试生成失败报 json_error，file 是组件目录名（普通任务为空）。"""
    try:
        generated, assets_file = _trial_assets(extract_dir)
    except Exception as exc:        # json 缺字段、结构不对时，AssetsExtractor 会抛各种异常，让页面把它当成问题列出来
        logger.warning(f'素材检查：试生成 {extract_dir.name} 的 assets.py 失败：{exc!r}')
        where = '' if extract_dir == task_dir else _relpath(extract_dir, task_dir)
        return [Issue('json_error', where, '', f'试生成 assets.py 失败，某个 json 的字段不全或结构不对：{exc!r}')]
    file = _relpath(assets_file, task_dir)
    if not assets_file.is_file():
        return [Issue('assets_outdated', file, '', 'assets.py 不存在，需要重新生成')]
    actual = assets_file.read_text(encoding='utf-8', errors='replace')
    if actual == generated:
        return []
    expected_names, actual_names = set(_ATTRIBUTE.findall(generated)), set(_ATTRIBUTE.findall(actual))
    parts = []
    if expected_names - actual_names:
        parts.append(f'缺少属性 {_listed(sorted(expected_names - actual_names))}')
    if actual_names - expected_names:
        parts.append(f'多出属性 {_listed(sorted(actual_names - expected_names))}')
    detail = ('assets.py 与 json 不一致：' + '；'.join(parts) if parts
              else 'assets.py 的内容与 json 试生成的结果不一致（属性名都对得上，ROI、阈值、图片路径等有差异）')
    return [Issue('assets_outdated', file, '', detail)]


def static_check(task: str, root: Path | None = None) -> list[Issue]:
    """对一个任务做不需要截图的检查，返回问题列表，空表示没发现问题。

    顺序固定：先按文件逐个查（json_error、missing_image、roi_out_of_bounds），再报 duplicate_item，最后是 assets_outdated。
    重名和 assets.py 比对都按"编进哪个 assets.py"分开算（见 _extract_root）：普通任务就是整个任务，Component 是每个组件各自一份。
    某个 assets.py 的 json 里有解析不了的，就不比对这一份（试生成会因此失败）；当前目录不是 root 时一份都不比，见模块说明。
    """
    task_dir = task_root(task, root)
    json_files = _json_files(task_dir)
    issues: list[Issue] = []
    places: dict[tuple[Path, str], list[tuple[str, str]]] = {}      # (编进哪个目录的 assets.py, 属性名) -> [(json, 条目名)]
    broken: set[Path] = set()                                        # json 解析不了的目录，不比对它的 assets.py
    for path in json_files:
        file, extract_dir = _relpath(path, task_dir), _extract_root(task_dir, path)
        data, error = _read_rules(path)
        if error:
            issues.append(Issue('json_error', file, '', error))
            broken.add(extract_dir)
            continue
        issues += _missing_images(file, path, data)
        issues += _roi_issues(file, data, _entries(data))
        for attribute, name in _attributes(data):
            places.setdefault((extract_dir, attribute), []).append((file, name))
    issues += _duplicate_issues(places)
    extract_dirs = [extract_dir for extract_dir in _extract_dirs(task_dir, json_files) if extract_dir not in broken]
    if extract_dirs:
        issues += _outdated_issues(task_dir, extract_dirs, root)
    return issues


# ------------------------------------------------------------------------------------------------------------------
# 按画面体检
# ------------------------------------------------------------------------------------------------------------------

def _scored_entries(task_dir: Path) -> list[tuple[str, str, dict]]:
    """要打分的条目：[(json 相对任务目录的路径, 标注工具的类型名, json 里的条目原样)]，按文件、条目的顺序。

    文件清单同 list_rules（AssetsExtractor 编进 assets.py 的那些），只取标注工具认作 image 和 ocr 的文件；
    解析不了的文件类型是 unknown，跳过。
    """
    scored = []
    for path in _json_files(task_dir):
        data, _ = _read_rules(path)
        rule_type = _annotator_rule_type(data)
        if rule_type in SCORED_TYPES:
            scored += [(_relpath(path, task_dir), rule_type, entry) for entry in _entries(data)]
    return scored


def screen_check(task: str, screen: str, session_id: str, image_id: str, tester: Callable | None = None,
                 tags: dict | None = None, root: Path | None = None) -> list[dict]:
    """用标注工具会话里的一张截图，给任务的图片和 OCR 条目逐条打分，返回 [{'file', 'item', 'matched', 'similarity', 'threshold', 'expected'}]。

    标签画面等于 screen 的条目 expected 为真，其余为假：截图是 screen 这个画面，该匹配上的是 expected 的条目；别的条目照样打分，
    由页面置灰。没有任何图片或 OCR 条目标在这个画面上就返回 []，一条都不打分。
    threshold 是图片条目 json 里的阈值，OCR 条目没有，为 None。某一条打分出错（模板图不存在、ROI 写坏……）不中断整个体检：
    这一条 matched 为假、similarity 为 None，另有一个 error 键写原因。
    tester 的参数和返回值同 annotator_manager.test_rule，不传就用它，rule 参数给的是 json 里的条目原样；
    tags 不传就读 root 下的标签文件（损坏时抛 ValueError，见 load_tags），传了就只用它。task 不在清单里抛 ValueError。
    """
    task_dir = task_root(task, root)
    tags = load_tags(tags_path(root)) if tags is None else tags
    scored = _scored_entries(task_dir)
    expected = [_tag_of(tags, task, file, entry.get('itemName'))['screen'] == screen for file, _, entry in scored]
    if not any(expected):
        return []
    if tester is None:
        from module.server.tool import annotator_manager
        tester = annotator_manager.test_rule
    results = []
    for (file, rule_type, entry), is_expected in zip(scored, expected):
        result = {'file': file, 'item': _name_of(entry), 'matched': False, 'similarity': None,
                  'threshold': entry.get('threshold') if rule_type == 'image' else None, 'expected': is_expected}
        try:
            detail = tester(session_id, image_id, task, file, rule_type, entry)
        except Exception as exc:
            logger.warning(f'素材体检：{file} 的 {result["item"]} 打分失败：{exc!r}')
            result['error'] = str(exc) or type(exc).__name__
        else:
            result.update(matched=bool(detail.get('matched')), similarity=detail.get('similarity'))
        results.append(result)
    return results


# ------------------------------------------------------------------------------------------------------------------
# 写入的守卫
# ------------------------------------------------------------------------------------------------------------------

class WriteRefused(RuntimeError):
    """素材写入被拒绝：MERGE_LOCK 被占着（别处正在合并或保存），或者仓库里留着没做完的合并（MERGE_HEAD）。

    str(异常) 就是给用户看的原因，HTTP 接口据此回 409。replace_image 直接抛它，什么都没改；replace_and_commit、commit_and_push
    和 commit_tags 把它放进返回值的 error。
    """


@contextmanager
def _write_guard(merging: Callable[[], bool]):
    """写素材的守卫：非阻塞地拿 MERGE_LOCK，拿不到抛 WriteRefused（BUSY_TEXT）；拿到后用 merging() 看仓库里有没有没做完的合并，
    有就抛 WriteRefused（MERGING_TEXT）。with 块里一直持有锁，不管怎么退出都会放锁。"""
    if not fork_updater.MERGE_LOCK.acquire(blocking=False):
        raise WriteRefused(BUSY_TEXT)
    try:
        if merging():
            raise WriteRefused(MERGING_TEXT)
        yield
    finally:
        fork_updater.MERGE_LOCK.release()


# ------------------------------------------------------------------------------------------------------------------
# 替换图片
# ------------------------------------------------------------------------------------------------------------------

def _replace_target(task_dir: Path, json_relpath: str, image_name: str) -> tuple[Path, Path]:
    """替换的目标：json_relpath 这个素材文件里某个条目运行时加载的图片（见 _entry_images：imageName，图片列表是
    <itemName>.png；按 json 所在目录算，同 assets.py 里的路径）。返回 (json 文件, 图片)，都是绝对路径。

    先过名单：json 必须是 AssetsExtractor 编进 assets.py 的那些文件之一，image_name 必须是其中某个条目运行时加载的图片名，
    所以 ../x.png、a.exe 这类名字根本进不来；再核对路径落在任务目录内、后缀是图片，json 被改坏时这两条是第二道防线。
    不符合的一律抛 ValueError。图片文件本身可以不存在：json 里有、磁盘上缺的图正是要补的。task_dir 要是已经 resolve 过的。
    """
    files = {_relpath(path, task_dir): path for path in _json_files(task_dir)}
    path = files.get(json_relpath) if isinstance(json_relpath, str) else None
    if path is None:
        raise ValueError(f'任务里没有这个素材文件：{json_relpath!r}')
    data, error = _read_rules(path)
    if error:
        raise ValueError(f'素材文件 {json_relpath} 读不了：{error}')
    image_names = {image for _, image in _entry_images(data) if isinstance(image, str)}
    if not isinstance(image_name, str) or image_name not in image_names:
        raise ValueError(f'{json_relpath} 里没有用到图片 {image_name!r} 的条目')
    target = (path.parent / image_name).resolve()
    if task_dir not in target.parents:
        raise ValueError(f'图片路径落在任务目录之外：{image_name}')
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f'不支持的图片后缀 {target.suffix!r}，只收 {"、".join(IMAGE_SUFFIXES)}')
    if target.is_dir():
        raise ValueError(f'目标是目录，不是图片：{image_name}')
    return path, target


def _decode_base64(content) -> bytes:
    """上传内容的 base64 解码：裸的 base64，或浏览器 FileReader 给的 data URL（data:image/png;base64, 开头）都行，空白和换行忽略。"""
    if not isinstance(content, str):
        raise ValueError('图片内容应当是 base64 字符串')
    text = content.strip()
    if text.startswith('data:'):
        header, _, text = text.partition(',')
        if not header.endswith(';base64'):
            raise ValueError('data URL 不是 base64 编码')
    try:
        return base64.b64decode(''.join(text.split()), validate=True)
    except ValueError as exc:       # binascii.Error 是 ValueError 的子类，含非 ASCII 字符的字符串也是
        raise ValueError('图片内容不是合法的 base64') from exc


def _reencode(raw: bytes, suffix: str) -> bytes:
    """校验 raw 是 cv2 读得出的图片，再按目标后缀重新编码：上传的是 PNG、目标是 .jpg，写下去的就是真的 JPEG，
    图片后面夹带的别的内容也不会落盘。读不出、编码不了都抛 ValueError。
    cv2 对空数据抛 cv2.error（断言失败）而不是返回 None，所以空数据先挡掉。cv2 和 numpy 用到时才导入。"""
    import cv2
    import numpy as np
    if not raw:
        raise ValueError('图片内容为空')
    try:
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error:
        image = None
    if image is None:
        raise ValueError('上传的内容不是可以读取的图片')
    try:
        ok, encoded = cv2.imencode(suffix, image)
    except cv2.error:
        ok = False
    if not ok:
        raise ValueError(f'图片无法编码成 {suffix}')
    return encoded.tobytes()


def _default_regenerate(repo: Path) -> Callable[[str], None]:
    """默认的重新生成 assets.py：AssetsExtractor 按当前目录算素材的相对路径，所以当前目录必须就是仓库根，否则抛 RuntimeError
    （同 ForkUpdater.regenerate；不在服务进程里 os.chdir，进程是各线程共用的）。检查在这里做、调用方在写盘之前调它，
    免得图换了、assets.py 没生成。"""
    cwd = Path.cwd().resolve()
    if cwd != repo:
        raise RuntimeError(f'重新生成 assets.py 需要在仓库根目录下进行：当前目录 {cwd}，仓库 {repo}')

    def regenerate(extract_dir: str) -> None:
        _extractor().AssetsExtractor(str(repo / 'tasks' / extract_dir)).extract()

    return regenerate


@dataclass(frozen=True)
class _Replacement:
    """一次换图要写的东西，落盘之前的检查都已通过（见 _prepare_replacement）。"""
    target: Path                            # 要写的图片，绝对路径
    shown: str                              # 仓库相对路径，如 tasks/ActivityShikigami/fire/fire_pass_8.png
    data: bytes                             # 按目标后缀重新编码好的图片
    extract_dir: str                        # 交给 regenerate 的目录（相对 tasks/）：任务名，组件是 Component/<组件>
    regenerate: Callable[[str], None]


def _prepare_replacement(task: str, json_relpath: str, image_name: str, content_base64: str, root: Path | None,
                         regenerate: Callable[[str], None] | None) -> _Replacement:
    """换图在落盘之前的全部检查，不碰 git 和磁盘，不通过就抛异常（规则见 replace_image）：任务、素材文件、图片名、路径、
    后缀、base64、图片内容（解码再按目标后缀重新编码），regenerate 不传时还有默认重新生成对当前目录的检查（RuntimeError）。"""
    task_dir = task_root(task, root).resolve()
    json_file, target = _replace_target(task_dir, json_relpath, image_name)
    data = _reencode(_decode_base64(content_base64), target.suffix.lower())
    if regenerate is None:
        regenerate = _default_regenerate(_repo_root(root))
    shown = f'tasks/{task}/{target.relative_to(task_dir).as_posix()}'
    extract_dir = _extract_root(task_dir, json_file).relative_to(task_dir.parent).as_posix()
    return _Replacement(target, shown, data, extract_dir, regenerate)


def _apply_replacement(replacement: _Replacement) -> None:
    """写图（原子写入，见 _write_atomically），再重新生成 assets.py。调用方持有写入守卫。"""
    _write_atomically(replacement.target, replacement.data)
    replacement.regenerate(replacement.extract_dir)
    logger.info(f'素材替换：{replacement.shown}')


def replace_image(task: str, json_relpath: str, image_name: str, content_base64: str, root: Path | None = None,
                  regenerate: Callable[[str], None] | None = None) -> dict:
    """用上传的图片替换 json_relpath 里某个条目运行时加载的图 image_name，再重新生成 assets.py：regenerate 收到相对 tasks/
    的目录，普通任务是任务名，组件是 Component/<组件>（同 ForkUpdater.regenerate）。
    返回 {'target': 仓库相对路径}，比如 tasks/ActivityShikigami/fire/fire_pass_8.png。不提交：页面上的上传替换走
    replace_and_commit，换完立刻提交推送。

    content_base64 是裸 base64 或 data URL。落盘之前的检查都抛 ValueError、什么都不改：任务和素材文件得在清单里，image_name 得是
    那个文件里某个条目运行时加载的图（imageName；图片列表是 <itemName>.png，见 _entry_images），路径落在任务目录内、后缀是
    .png .jpg .jpeg .bmp，内容得是 cv2 读得出的图片。
    写入先把解码后的图片按目标后缀重新编码（不是原样落盘），写进同目录的临时文件再 os.replace，原图要么整张换掉要么不动；
    目标文件原来不存在（json 里有、磁盘上缺的图）就是新建。
    regenerate 不传就用默认的（见 _default_regenerate，当前目录不是仓库根时在写盘之前抛 RuntimeError）。regenerate 自己出错时
    图已经换上了，异常照常往外抛，不回滚。
    持有 MERGE_LOCK 做完整个替换；别处正在合并或保存、或仓库里有没做完的合并时抛 WriteRefused，什么都不改。
    """
    repo = _repo_root(root)
    with _write_guard(lambda: (repo / '.git' / 'MERGE_HEAD').exists()):
        replacement = _prepare_replacement(task, json_relpath, image_name, content_base64, root, regenerate)
        _apply_replacement(replacement)
    return {'target': replacement.shown}


# ------------------------------------------------------------------------------------------------------------------
# 提交并推送
# ------------------------------------------------------------------------------------------------------------------

def _outcome(commit: str | None, pushed: bool, error: str = '') -> dict:
    return {'commit': commit, 'pushed': pushed, 'error': error}


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), '')


def _fork_refusal(runner: GitRunner, branch: str) -> str:
    """刚拉取的 fork 比运行目录多出提交时，不能再往本地提交：fork 领先（fork_ahead）或两边分叉（diverged）都会让这个提交
    推不上去。返回给用户看的拒绝原因，没有这种情况返回 ''。"""
    state = _fork_state(runner, branch)
    if state is None:
        return ''
    return FORK_AHEAD_TEXT.format(n=state.commits) if state.status == 'fork_ahead' else DIVERGED_TEXT


def _push_if_ahead(runner: GitRunner, branch: str) -> tuple[bool, str]:
    """HEAD 领先 origin/<分支>，或者这个引用不存在（拉取没写它，比不了）就推送，返回 (是否推上去了, error)；
    没有领先的提交什么也不做。所以上次推送失败留下的提交，下一次调用（哪怕这次没有新改动）会补推。"""
    tracked = f'{FORK_REMOTE}/{branch}'
    if _ref_exists(runner, tracked) and _count(runner, f'{tracked}..HEAD') == 0:
        return False, ''
    pushed, stderr = push(runner, branch)
    if pushed:
        return True, ''
    logger.warning(f'素材提交：推送失败：{stderr}')
    return False, PUSH_FAILED_TEXT.format(reason=fork_updater.push_reason(stderr) or UNKNOWN_REASON)


def _commit_and_push(runner: GitRunner, branch: str, paths: list[str], message: str,
                     write: Callable[[], None] | None = None) -> dict:
    """commit_and_push、replace_and_commit 和 commit_tags 共用的核心，整个过程持有写入守卫（见 _write_guard）。

    顺序：拉取 fork → fork 有运行目录没有的提交就拒绝（什么都没提交，write 也没调）→ write()（保存标签靠它在拒绝之后才落盘，
    拒绝时检出保持干净）→ 只提交 paths（原子写入的临时文件除外，见 _write_atomically）→ 拉取失败就不推送 → 推送。
    拉取失败不挡着本地提交（启动器守卫不会冲掉没推上去的提交），只是不推送并提示重试；推送失败同理，提交留在本地，下次补推。
    git 命令跑了但退出码非零（没配提交身份、index.lock……）转成 error，不往外抛；写文件失败等别的异常照常往外抛。
    """
    commit = None
    try:
        with _write_guard(lambda: _ref_exists(runner, 'MERGE_HEAD')):
            fetch_failure = _fetch(runner, FORK_REMOTE, branch)
            refusal = '' if fetch_failure else _fork_refusal(runner, branch)
            if refusal:
                return _outcome(None, False, refusal)
            if write is not None:
                write()
            commit = commit_paths(runner, [*paths, SCRATCH_PATHSPEC], message)
            if fetch_failure:
                logger.warning(f'素材提交：拉取 fork 失败：{fetch_failure.message}')
                reason = _first_line(fetch_failure.message) or UNKNOWN_REASON
                return _outcome(commit, False, FETCH_FAILED_TEXT.format(reason=reason))
            pushed, error = _push_if_ahead(runner, branch)
            return _outcome(commit, pushed, error)
    except WriteRefused as exc:
        return _outcome(None, False, str(exc))
    except subprocess.CalledProcessError as exc:
        logger.warning(f'素材提交：git 命令失败：{exc.cmd}\n{exc.stderr}')
        return _outcome(commit, False, GIT_FAILED_TEXT.format(reason=_first_line(exc.stderr or '') or UNKNOWN_REASON))


def commit_and_push(task: str, message: str, runner: GitRunner, branch: str) -> dict:
    """提交 tasks/<task> 下的改动并推送到 fork 的 branch 分支，返回 {'commit': sha | None, 'pushed': 是否推上去了, 'error': 原因}。

    只 add tasks/<task>：别的任务、未提交的代码和索引里别处已暂存的内容都不带进提交。message 为空白时用
    "素材维护：替换 <task> 素材"。task 必须是 list_tasks 列得出来的名字，否则抛 ValueError（免得 ../ 混进 git 路径）。
    没有改动时不建空提交，commit 为 None；本地还有没推上去的提交（上次推送失败留下的）照样推，所以失败之后再调一次就能补推。
    error 为空表示没有问题，非空是给用户看的原因（拉取失败、推送失败、fork 领先或分叉、正在合并或保存），见 _commit_and_push。
    """
    task_root(task, runner.repo)
    return _commit_and_push(runner, branch, [f'tasks/{task}'], (message or '').strip() or f'素材维护：替换 {task} 素材')


def replace_and_commit(task: str, json_relpath: str, image_name: str, content_base64: str, runner: GitRunner,
                       branch: str, regenerate: Callable[[str], None] | None = None) -> dict:
    """页面上的"上传替换"：换图、重新生成 assets.py、提交 tasks/<task>、推送到 fork，一步做完。返回
    {'target': 仓库相对路径, 'replaced': 图写下了没有, 'commit': sha | None, 'pushed': 推上去了没有, 'error': 原因}。

    换下的图不能留在运行目录里不提交：OASX 下次启动服务时启动器 git reset --hard 会把它还原，没提交的改动还会挡住每一次合并上游。
    落盘之前的检查同 replace_image（仓库是 runner.repo），全部在碰 git 和磁盘之前做完，不通过照样抛异常（ValueError，默认重新生成
    对当前目录的检查是 RuntimeError）。之后走 commit_and_push 的流程（见 _commit_and_push）：拉取 fork → fork 领先或分叉就拒绝，
    图不写 → 写图并重新生成 assets.py → 提交 → 推送。replaced 只在写图和重新生成都做完时为真：正在合并或保存、仓库里有没做完的
    合并、fork 领先或分叉时 replaced 为假，什么都没写，原因在 error 里。提交说明是"素材维护：替换 <target>"，历史里能看出换的是哪张图。
    写下之后 git 失败（commit 为 None、error 非空）时图留在工作区没提交，"提交并推送"可以重试；推送失败时提交留在本地，同样可以重试。
    """
    replacement = _prepare_replacement(task, json_relpath, image_name, content_base64, runner.repo, regenerate)
    written = []

    def write() -> None:
        _apply_replacement(replacement)
        written.append(replacement.shown)

    outcome = _commit_and_push(runner, branch, [f'tasks/{task}'], f'素材维护：替换 {replacement.shown}', write=write)
    return {'target': replacement.shown, 'replaced': bool(written), **outcome}


def commit_tags(tags: dict, runner: GitRunner, branch: str, path: Path | None = None) -> dict:
    """把 tags 存成标签文件，立刻提交并推送，返回值同 commit_and_push，提交说明固定为"素材维护：更新标签"。

    tags 必须是对象：顶层不是对象的标签文件，load_tags 会当成损坏，存进去就读不回来。path 不传就是 runner.repo 下的
    标签文件；传了也必须是仓库里的文件（仓库根目录本身不算：提交路径会变成 "."，把整个工作区带进去），以仓库相对路径提交，
    只提交这一个文件。这两条不符合都在动仓库之前抛 ValueError。
    先拉取并核对 fork，被拒绝（fork 领先、分叉、正在合并或保存）时标签文件一个字都没写，检出保持干净。保存后马上提交，
    因为未提交的标签文件会让运行目录一直"有未提交的改动"，挡住之后每一次合并上游。
    """
    if not isinstance(tags, dict):
        raise ValueError(f'标签应当是对象，实际是 {type(tags).__name__}')
    repo = _repo_root(runner.repo)
    path = (tags_path(repo) if path is None else Path(path)).resolve()
    if repo not in path.parents:
        raise ValueError(f'标签文件必须是仓库里的文件：{path}')
    relative = path.relative_to(repo).as_posix()
    return _commit_and_push(runner, branch, [relative], TAGS_COMMIT_MESSAGE, write=lambda: save_tags(tags, path))


# ------------------------------------------------------------------------------------------------------------------
# 导出补丁
# ------------------------------------------------------------------------------------------------------------------

def export_patch(commit: str, runner: GitRunner, out_dir: Path) -> Path:
    """把 commit 新增或修改的文件打成 zip，返回 out_dir/assets-patch-<短 sha>.zip。zip 里是仓库相对路径，解压到仓库根就是覆盖。

    commit 得是仓库里的提交（完整或短 sha、分支名、HEAD 都行），否则抛 ValueError；提交没有新增或修改的文件（比如只删了文件，
    删除的不进补丁包）也抛 ValueError。校验都在建 out_dir 之前。
    zip 由 git archive 直接写成文件，二进制内容不经过 GitRunner.run 的 UTF-8 解码；--literal-pathspecs 让文件名里的 [ * ?
    不被当成通配符。git archive 会顺手加上目录项，它们不是文件。
    """
    if not isinstance(commit, str) or not commit.strip() or commit.startswith('-'):        # 开头是 - 的会被 git 当成选项
        raise ValueError(f'不是有效的提交：{commit!r}')
    verified = runner.run('rev-parse', '--verify', f'{commit}^{{commit}}', check=False)
    if verified.returncode != 0:
        raise ValueError(f'仓库里没有这个提交：{commit!r}')
    sha = verified.stdout.strip()
    listed = runner.run('diff-tree', '-r', '--root', '--no-commit-id', '--name-only', '--diff-filter=AM', '-z', sha)
    paths = [path for path in listed.stdout.split('\0') if path]
    if not paths:
        raise ValueError(f'提交 {sha[:7]} 没有新增或修改的文件，没有可导出的内容')
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f'assets-patch-{sha[:7]}.zip'
    runner.run('--literal-pathspecs', 'archive', '--format=zip', '-o', str(target), sha, '--', *paths)
    return target
