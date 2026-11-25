"""编排测试共用的夹具：默认拦住真的进程表和进程对象的护栏，以及实例配置目录。

instance_control 经 _manager() 现取进程表，真的那张要导入上游 MainManager：它一导入就扫描 config/、给每个实例建进程对象
并启动推送线程；真的 ScriptProcess.start() 会拉起实例进程。每个用例开始前把两者换成一调用就抛 AssertionError 的函数，
忘了装假进程表的用例直接失败。test_router.py 的 fake_instances 与 test_router_commands.py 的 table() 在它之后执行，
照旧换上各自的假进程表；fake_instances 只换进程表，建真的进程对象仍由护栏拦住。

instance_dir 把 cwd 切到 tmp_path，建好 config/template.json 与实例 t 的配置 config/t.json，不碰仓库自己的 config/
和 log/。test_router.py 的用例直接用它，test_router_commands.py 的 journal 建在它上面。
"""
import shutil
import warnings
from pathlib import Path

import pytest

from module.config.config_model import ConfigModel
from module.server import instance_control

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def no_real_instance_processes(monkeypatch) -> None:
    """instance_control 的 _manager 与 ScriptProcess 换成一调用就抛 AssertionError 的函数。"""
    def no_real_manager():
        raise AssertionError('测试里不该用真的进程表：先用 use_fake_manager 或 table() 换上假的')

    def no_real_process(name: str):
        raise AssertionError(f'测试里不该建真的进程对象：{name}')

    monkeypatch.setattr(instance_control, '_manager', no_real_manager)
    monkeypatch.setattr(instance_control, 'ScriptProcess', no_real_process)


@pytest.fixture(scope='module')
def default_config() -> dict:
    """一份默认实例配置的字典。上游默认值里有写成字符串的整数字段，dump 时 pydantic 会警告，与本文件无关，只在这里放行。"""
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Pydantic serializer warnings', category=UserWarning)
        return ConfigModel().model_dump()


@pytest.fixture
def instance_dir(tmp_path, monkeypatch, default_config) -> Path:
    """cwd 切到 tmp_path，里面是 config/template.json（仓库模板的副本）和实例配置 config/t.json。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config').mkdir()
    shutil.copy(REPO_ROOT / 'config' / 'template.json', tmp_path / 'config' / 'template.json')
    ConfigModel.write_json('t', default_config)
    return tmp_path
