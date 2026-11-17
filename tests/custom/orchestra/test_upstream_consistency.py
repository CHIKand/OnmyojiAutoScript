"""没有编排在跑时，实例的行为必须与上游一致：同样的配置，选出同样的任务，也不多写任何文件。

用真实的 Config 读 tmp_path/config/t.json，脚本用替身（真正的 Script 要拉起设备）。
另有一条用 ast 检查 script.py 里的钩子都在：之后合并上游改了 script.py，钩子被冲掉时这里先报错。
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.config.anti_ban import AntiBanGuard
from module.config.config import Config
from module.config.config_model import ConfigModel
from module.exception import RequestHumanTakeover
from module.orchestra.progress import progress_path
from module.orchestra.runtime import orchestra_pick
from tasks.Script.config_optimization import ScheduleRule

PAST = datetime(2024, 1, 1)
SCRIPT_PY = Path(__file__).resolve().parents[3] / 'script.py'

# 上游的 Config.update_scheduler 用了 pydantic 已弃用的 .dict()，这里要调真实的 get_next，免得每个用例都刷这条警告
pytestmark = pytest.mark.filterwarnings(
    'ignore:The `dict` method is deprecated:pydantic.warnings.PydanticDeprecatedSince20')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def write_config(enabled: dict[str, tuple[datetime, int]], rule: str) -> None:
    """把一份实例配置写进 config/t.json。enabled：模型字段名 -> (next_run, priority)，其余任务一律不启用（含默认启用的 Restart）。"""
    model = ConfigModel()
    model.restart.scheduler.enable = False
    for key, (next_run, priority) in enabled.items():
        scheduler = getattr(model, key).scheduler
        scheduler.enable, scheduler.next_run, scheduler.priority = True, next_run, priority
    model.script.optimization.schedule_rule = ScheduleRule(rule)
    ConfigModel.write_json('t', model.model_dump())


def stand_in() -> SimpleNamespace:
    return SimpleNamespace(config=Config('t'), config_name='t', anti_ban_guard=AntiBanGuard(), running_task_obj=None)


def same_as_upstream(script: SimpleNamespace) -> None:
    picked = orchestra_pick(script)
    expected = Config('t').get_next()   # 另起一个 Config 读同一份文件：上游自己的选法
    assert (picked.command, picked.next_run) == (expected.command, expected.next_run)


@pytest.mark.parametrize('rule', ['Filter', 'FIFO', 'Priority'])
def test_idle_matches_upstream(rule):
    write_config({'orochi': (PAST, 5), 'goryou_realm': (PAST + timedelta(hours=1), 1),
                  'restart': (PAST + timedelta(hours=2), 5)}, rule)
    same_as_upstream(stand_in())


@pytest.mark.parametrize('rule', ['Filter', 'FIFO', 'Priority'])
def test_idle_matches_upstream_without_restart(rule):
    # 去掉 Restart 后三种规则选出的任务各不相同：Filter 看固定顺序，FIFO 看时间，Priority 看优先级数字
    write_config({'orochi': (PAST + timedelta(hours=1), 5), 'goryou_realm': (PAST, 1)}, rule)
    same_as_upstream(stand_in())


def test_idle_with_only_waiting_tasks_matches_upstream():
    write_config({'orochi': (datetime.now() + timedelta(days=1), 5)}, 'Filter')
    same_as_upstream(stand_in())


def test_idle_with_nothing_enabled_raises_like_upstream():
    write_config({}, 'Filter')
    with pytest.raises(RequestHumanTakeover):
        orchestra_pick(stand_in())
    with pytest.raises(RequestHumanTakeover):
        Config('t').get_next()


def test_idle_leaves_no_trace():
    write_config({'orochi': (PAST, 5)}, 'Filter')
    config_file = Path.cwd() / 'config' / 't.json'
    before = config_file.read_bytes()
    script = stand_in()
    orchestra_pick(script)
    orchestra_pick(script)
    assert config_file.read_bytes() == before   # 没有改写配置
    assert not progress_path('t').exists()   # 也没有建进度文件


def _hook_calls(method: ast.FunctionDef, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(method)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def test_script_py_keeps_the_hooks():
    tree = ast.parse(SCRIPT_PY.read_text(encoding='utf-8'))
    script_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Script')
    methods = {n.name: n for n in script_class.body if isinstance(n, ast.FunctionDef)}

    pick, = _hook_calls(methods['get_next_task'], 'orchestra_pick')
    assert not [n for n in ast.walk(methods['get_next_task'])
                if isinstance(n, ast.Attribute) and n.attr == 'get_next']   # 上游直接调 config.get_next() 的那行已换掉
    before, = _hook_calls(methods['loop'], 'orchestra_before_run')
    after, = _hook_calls(methods['loop'], 'orchestra_after_run')
    run, = [n for n in ast.walk(methods['loop'])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'run'
            and isinstance(n.func.value, ast.Name) and n.func.value.id == 'self']
    assert before.lineno < run.lineno < after.lineno   # 运行前、运行、运行后，顺序不能乱
    stored = [n for n in ast.walk(methods['run'])
              if isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == 'running_task_obj'
                                                   for t in n.targets)]
    assert len(stored) == 1   # 任务对象存进 self.running_task_obj，心跳线程才读得到它的计数
    imported = {alias.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'module.orchestra.runtime'
                for alias in n.names}
    assert imported == {'orchestra_pick', 'orchestra_before_run', 'orchestra_after_run'}
