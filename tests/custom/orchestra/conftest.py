"""编排测试共用的护栏：默认拦住真的进程表和进程对象。

instance_control 经 _manager() 现取进程表，真的那张要导入上游 MainManager：它一导入就扫描 config/、给每个实例建进程对象
并启动推送线程；真的 ScriptProcess.start() 会拉起实例进程。每个用例开始前把两者换成一调用就抛 AssertionError 的函数，
忘了装假进程表的用例直接失败。test_router.py 的 fake_instances 与 test_router_commands.py 的 table() 在它之后执行，
照旧换上各自的假进程表。
"""
import pytest

from module.server import instance_control


@pytest.fixture(autouse=True)
def no_real_instance_processes(monkeypatch) -> None:
    """instance_control 的 _manager 与 ScriptProcess 换成一调用就抛 AssertionError 的函数。"""
    def no_real_manager():
        raise AssertionError('测试里不该用真的进程表：先用 use_fake_manager 或 table() 换上假的')

    def no_real_process(name: str):
        raise AssertionError(f'测试里不该建真的进程对象：{name}')

    monkeypatch.setattr(instance_control, '_manager', no_real_manager)
    monkeypatch.setattr(instance_control, 'ScriptProcess', no_real_process)
