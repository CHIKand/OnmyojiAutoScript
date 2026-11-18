"""素材维护页的业务逻辑：任务清单、静态检查、标签文件。

这一层只读素材、做判断，不碰 git，也不改素材。root 是仓库根目录（任务在 root/tasks/ 下），不传就取进程的当前目录，
与标注工具的 PROJECT_ROOT 一致；测试传临时目录，不依赖全局状态。

清单和检查看到的 json，与 dev_tools/assets_extract.py 编进 assets.py 的完全一致：直接用 AssetsExtractor.all_json_file，
连它对路径里 temp 的过滤也照搬。所以清单里有的条目，assets.py 里就该有对应的属性。
assets.py 与 json 是否一致靠"试生成"判断：继承 AssetsExtractor，只把写盘的 write_file 换成空操作，其余全走 extract 自己的流程，
再和磁盘上的 assets.py 比对。AssetsExtractor 按当前目录算图片的相对路径，所以当前目录不是 root 时不比对，只记一条警告。
不在服务进程里 os.chdir：进程是各线程共用的。
dev_tools.assets_extract 依赖 numpy 和 tqdm，用到时才导入。文件类型的判断借用标注工具的 AnnotatorManager._detect_rule_type
（页面上的类型名和后面的单条打分都按它的口径），module.server.tool 同样用到时才导入：它导入时按当前目录定出 PROJECT_ROOT，
服务启动时早已导入过，别在换过当前目录之后第一次导入它。
标签文件随 fork 提交，结构是 {任务: {json 相对路径: {条目名: {"periodic": 布尔, "screen": 画面名}}}}。
没有标签的条目取默认值（periodic 为假，画面为"未分类"）；标签文件里已经不存在的条目、文件、任务，清单一概忽略。
"""
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from module.logger import logger

DEFAULT_TASKS = ('ActivityShikigami', 'MetaDemon')      # 页面默认只列这两个限时活动，可以切换成全部任务
CONTAINER_TASK = 'Component'                            # 组件的容器目录，素材在 Component/<组件>/ 里，它自己不是任务
UNCLASSIFIED = '未分类'
SCREENS = ('活动主页', '普通挑战页', 'Boss挑战页', '战斗中', '退出时', UNCLASSIFIED)
EXTRA_SCREENS = {'MetaDemon': ('超鬼王主页', '超鬼王战斗', '合成页')}     # 个别任务多出来的画面
FRAME_WIDTH, FRAME_HEIGHT = 1280, 720                   # 模拟器画面尺寸，ROI 以它为界
TAGS_RELPATH = Path('assets') / 'custom' / 'assets_tags.json'
_ROI_FIELDS = ('roiFront', 'roiBack')
_ATTRIBUTE = re.compile(r'^\t([A-Z]+_[A-Z0-9_]+) =', re.MULTILINE)      # assets.py 里的属性名
_NAMES_SHOWN = 10                                       # 说明里最多列出几个属性名


@dataclass(frozen=True)
class Issue:
    """静态检查发现的一个问题。

    kind：missing_image / json_error / duplicate_item / roi_out_of_bounds / assets_outdated。
    file：json 相对任务目录的路径（正斜杠）；assets_outdated 是 assets.py；不针对某个文件时为空（试生成失败的 json_error）。
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
    """可以做素材维护的任务名：tasks/ 下的一层子目录，不含 Component（容器目录）和以 . 或 _ 开头的（__pycache__ 之类）。

    all_tasks 为假时只留 DEFAULT_TASKS 里目录还在的，顺序同 DEFAULT_TASKS；为真时是全部，按名字排序。
    """
    tasks_dir = _repo_root(root) / 'tasks'
    if not tasks_dir.is_dir():
        return []
    names = sorted(entry.name for entry in tasks_dir.iterdir()
                   if entry.is_dir() and entry.name != CONTAINER_TASK and not entry.name.startswith(('.', '_')))
    return names if all_tasks else [name for name in DEFAULT_TASKS if name in names]


def task_root(task: str, root: Path | None = None) -> Path:
    """任务目录 root/tasks/<task>。

    task 必须是 list_tasks 列得出来的名字：路径穿越（../etc）、Component、带路径分隔符的名字、不存在的目录都抛 ValueError，
    HTTP 接口据此回 4xx。拿目录清单当白名单比对，而不是拼路径再检查，名字里的任何花样都进不来。
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


def save_tags(tags: dict, path: Path | None = None) -> None:
    """整份写回标签文件：UTF-8、不转义中文、缩进 2、末尾换行、LF 换行，键序照传入的。

    先写同目录的临时文件再 os.replace 换上去，写到一半出错或断电，旧文件原样还在；出错时临时文件也清掉。
    """
    path = Path(path) if path is not None else tags_path()
    text = json.dumps(tags, ensure_ascii=False, indent=2) + '\n'     # 先序列化：内容不合法时不留下临时文件
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, scratch = tempfile.mkstemp(dir=path.parent, prefix=f'{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(scratch, path)
    except BaseException:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        raise


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


def _json_files(task_dir: Path) -> list[Path]:
    """AssetsExtractor 会编进 assets.py 的 json 文件，按相对路径排序。"""
    files = [Path(file) for file in _extractor().AssetsExtractor(str(task_dir)).all_json_file()]
    return sorted(files, key=lambda path: _relpath(path, task_dir))


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

def _missing_images(file: str, path: Path, entries: list[dict]) -> list[Issue]:
    """imageName 指向的图片不在磁盘上。图片路径按 json 所在目录算，和 assets.py 里的 file="./<json 目录>/<imageName>" 一致。"""
    issues = []
    for entry in entries:
        if 'imageName' not in entry:
            continue
        image_name = str(entry['imageName'])
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


def _duplicate_issues(places: dict[str, list[tuple[str, str]]]) -> list[Issue]:
    """同一个属性名出现在两处以上（同一个文件里的两条也算）：编进同一个 assets.py 后互相覆盖。每一处各报一条，说明里列出全部位置。"""
    issues = []
    for attribute, found in places.items():
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


def _outdated_issues(task_dir: Path, root: Path | None) -> list[Issue]:
    """assets.py 与 json 试生成的结果不一致：属性名对不上的列出缺哪些、多哪些，属性名都对得上就说内容有差异；assets.py 不存在也算。"""
    repo = _repo_root(root)
    if repo != Path.cwd().resolve():
        logger.warning(f'素材检查：当前目录不是 {repo}，AssetsExtractor 按当前目录算图片路径，跳过 assets.py 与 json 的比对')
        return []
    try:
        generated, assets_file = _trial_assets(task_dir)
    except Exception as exc:        # json 缺字段、结构不对时，AssetsExtractor 会抛各种异常，让页面把它当成问题列出来
        logger.warning(f'素材检查：试生成 {task_dir.name} 的 assets.py 失败：{exc!r}')
        return [Issue('json_error', '', '', f'试生成 assets.py 失败，某个 json 的字段不全或结构不对：{exc!r}')]
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
    任务里有 json 解析不了时不做 assets.py 比对（试生成会因此失败）；当前目录不是 root 时也不做，见模块说明。
    """
    task_dir = task_root(task, root)
    issues: list[Issue] = []
    places: dict[str, list[tuple[str, str]]] = {}       # 属性名 -> [(json 相对路径, 条目名)]
    for path in _json_files(task_dir):
        file = _relpath(path, task_dir)
        data, error = _read_rules(path)
        if error:
            issues.append(Issue('json_error', file, '', error))
            continue
        entries = _entries(data)
        issues += _missing_images(file, path, entries)
        issues += _roi_issues(file, data, entries)
        for attribute, name in _attributes(data):
            places.setdefault(attribute, []).append((file, name))
    issues += _duplicate_issues(places)
    if not any(issue.kind == 'json_error' for issue in issues):
        issues += _outdated_issues(task_dir, root)
    return issues
