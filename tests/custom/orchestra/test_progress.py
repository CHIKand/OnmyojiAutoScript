import errno
import json
import logging
import os
import sys
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.config import atomicwrites
from module.orchestra import progress as progress_module
from module.logger import cleanup_logs
from module.orchestra.progress import (Progress, adopt_legacy_progress, legacy_progress_path, load_progress,
                                       progress_path, progress_to_dict, save_progress)

T0 = datetime(2026, 11, 17, 10, 0, 0, 123456)   # 带微秒：往返之后必须原样相等
COMPUTED = {'row_elapsed_seconds', 'rest_remaining_seconds'}


def full_progress() -> Progress:
    """每个字段都取非默认值。"""
    return Progress(
        phase='resting',
        row_index=3,
        row_task='Orochi',
        loop_count=2,
        sequence_started_at=T0,
        row_started_at=T0 + timedelta(minutes=5, microseconds=7),
        heartbeat_at=T0 + timedelta(minutes=9),
        done_count=12,
        row_elapsed_before=125.5,
        rest_until=T0 + timedelta(hours=1),
        rest_remaining=300.25,
        total_elapsed=3600.75,
        saved_params={'orochi.orochi_config.limit_count': 30, 'orochi.orochi_config.limit_time': '00:20:00'},
        last_command_seq=4,
        paused=True,
        history=[{'row_index': 0, 'task': 'Orochi', 'completed': True}],
        status_note='防封禁休眠中，08:00 继续',
        team_wait_since=T0 + timedelta(hours=2),
        team_wait_ordinal=2,
        team_wait_task='Orochi',
        team_alive_at=T0 + timedelta(hours=2, seconds=30),
        team_met_at=T0 + timedelta(hours=1, minutes=30),
        team_met_ordinal=1,
        team_met_task='EternitySea',
        team_partner='山川知异路',
        team_row_ordinal=1,
    )


def write_raw(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_roundtrip_preserves_all_fields(tmp_path):
    p = full_progress()
    default = Progress()
    # 新增字段时若忘了在 full_progress 里赋值，这里先报错，往返测试才不会漏掉它
    assert all(getattr(p, f.name) != getattr(default, f.name) for f in fields(Progress))
    path = progress_path('oas1', tmp_path)
    save_progress(path, p)   # log/ 目录此时还不存在，由 save_progress 创建
    assert load_progress(path) == p


def test_missing_or_corrupt_file_gives_default(tmp_path):
    path = progress_path('oas1', tmp_path)
    assert load_progress(path) == Progress()   # 文件不存在
    write_raw(path, '{')
    assert load_progress(path) == Progress()   # JSON 残缺


def test_history_capped_at_20(tmp_path):
    entries = [{'row_index': i} for i in range(25)]
    p = Progress(history=list(entries))
    path = progress_path('oas1', tmp_path)
    save_progress(path, p)
    loaded = load_progress(path)
    assert len(loaded.history) == 20
    assert loaded.history == entries[-20:]
    assert p.history == entries   # 只裁剪写进文件的内容，不动调用方手里的对象


def test_progress_path_layout(tmp_path, monkeypatch):
    assert progress_path('oas1', tmp_path) == tmp_path / 'log' / 'orchestra' / 'oas1.json'
    assert legacy_progress_path('oas1', tmp_path) == tmp_path / 'log' / '.orchestra_oas1.json'
    monkeypatch.chdir(tmp_path)
    assert progress_path('oas1') == Path.cwd() / 'log' / 'orchestra' / 'oas1.json'   # 缺省取当前目录
    assert legacy_progress_path('oas1') == Path.cwd() / 'log' / '.orchestra_oas1.json'


def test_upstream_log_cleanup_leaves_the_progress_file_alone(tmp_path):
    """上游每次启动删 log/ 根目录下超过 14 天的文件，子目录里只进 log/error：进度文件放在 log/orchestra/ 才留得住。"""
    path = progress_path('oas1', tmp_path)
    save_progress(path, full_progress())
    stale_root_file = tmp_path / 'log' / '2026-01-01_oas1.txt'
    stale_root_file.write_text('old', encoding='utf-8')
    month_ago = (datetime.now() - timedelta(days=30)).timestamp()
    for old in (path, path.parent, stale_root_file):
        os.utime(old, (month_ago, month_ago))
    cleanup_logs(str(tmp_path / 'log'))
    assert not stale_root_file.exists()   # 清理确实跑了
    assert load_progress(path) == full_progress()


# ---- 旧位置 log/.orchestra_<实例名>.json 的一次性迁移 ----

def test_adopt_moves_the_legacy_file_when_the_new_one_is_missing(tmp_path):
    legacy = legacy_progress_path('oas1', tmp_path)
    save_progress(legacy, full_progress())   # 旧版本写下的、还在进行中的进度
    raw = legacy.read_bytes()
    assert adopt_legacy_progress('oas1', tmp_path) is True
    assert not legacy.exists()
    assert progress_path('oas1', tmp_path).read_bytes() == raw   # 原样搬过来，saved_params 一个不少
    assert adopt_legacy_progress('oas1', tmp_path) is False      # 只搬一次


def test_adopt_leaves_both_alone_when_the_new_file_exists(tmp_path):
    legacy, path = legacy_progress_path('oas1', tmp_path), progress_path('oas1', tmp_path)
    save_progress(legacy, Progress(row_index=1))
    save_progress(path, Progress(row_index=2))
    assert adopt_legacy_progress('oas1', tmp_path) is False
    assert (load_progress(legacy).row_index, load_progress(path).row_index) == (1, 2)   # 新的为准，旧的不动


def test_adopt_without_a_legacy_file_creates_nothing(tmp_path):
    assert adopt_legacy_progress('oas1', tmp_path) is False
    assert list(tmp_path.iterdir()) == []   # 连 log/ 都不建：没编排过的实例不留痕迹


@pytest.mark.parametrize('content', [
    '[]',                                               # 顶层不是对象
    '{"row_index": 3, "heartbeat_at": "not-a-time"}',   # 日期无法解析：整份作废，不留下 row_index
    '{"rest_until": 123}',                              # 日期不是字符串
])
def test_unusable_content_gives_default(tmp_path, content):
    path = progress_path('oas1', tmp_path)
    write_raw(path, content)
    assert load_progress(path) == Progress()


def test_unknown_keys_ignored_and_missing_keys_default(tmp_path):
    path = progress_path('oas1', tmp_path)
    write_raw(path, '{"phase": "resting", "row_index": 2, "added_by_a_newer_version": [1, 2]}')
    assert load_progress(path) == Progress(phase='resting', row_index=2)


def test_corrupt_file_warns_but_missing_file_is_silent(tmp_path, caplog):
    path = progress_path('oas1', tmp_path)

    def warnings() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name == 'oas' and r.levelno == logging.WARNING]

    with caplog.at_level(logging.WARNING):
        load_progress(path)   # 文件不存在是首次运行的常态，不该刷日志
        assert warnings() == []
        write_raw(path, '{')
        load_progress(path)
    assert len(warnings()) == 1


def test_row_elapsed_seconds_uses_heartbeat():
    p = Progress(phase='row_running', row_started_at=T0, heartbeat_at=T0 + timedelta(minutes=7),
                 row_elapsed_before=120.0)
    # now 远晚于心跳：截止时刻取心跳，不取 now
    assert progress_to_dict(p, T0 + timedelta(hours=5))['row_elapsed_seconds'] == 420.0 + 120.0
    # 还没有心跳时用 now
    p = Progress(phase='row_running', row_started_at=T0)
    assert progress_to_dict(p, T0 + timedelta(seconds=90))['row_elapsed_seconds'] == 90.0
    # 不在 row_running 阶段则不计算
    p = Progress(phase='interrupted', row_started_at=T0, heartbeat_at=T0 + timedelta(minutes=7))
    assert progress_to_dict(p, T0 + timedelta(hours=5))['row_elapsed_seconds'] is None


def test_rest_remaining_seconds_while_resting():
    p = Progress(phase='resting', rest_until=T0 + timedelta(minutes=10))
    assert progress_to_dict(p, T0 + timedelta(minutes=8, seconds=30))['rest_remaining_seconds'] == 90.0
    assert progress_to_dict(p, T0 + timedelta(minutes=11))['rest_remaining_seconds'] == 0.0   # 已过期不出负数
    assert progress_to_dict(Progress(phase='row_running', rest_until=T0), T0)['rest_remaining_seconds'] is None


def test_rest_remaining_seconds_when_paused():
    # 暂停期间倒计时冻结：取暂停时记下的剩余秒数，不看已经过时的 rest_until
    p = Progress(phase='resting', paused=True, rest_until=T0, rest_remaining=600.0)
    assert progress_to_dict(p, T0 + timedelta(hours=2))['rest_remaining_seconds'] == 600.0
    # 暂停但没有休息中的记录：不凭空造出倒计时
    p = Progress(phase='row_running', paused=True)
    assert progress_to_dict(p, T0)['rest_remaining_seconds'] is None


def test_dict_view_is_file_form_plus_computed_fields(tmp_path):
    p = full_progress()
    path = progress_path('oas1', tmp_path)
    save_progress(path, p)
    text = path.read_text(encoding='utf-8')
    assert '防封禁休眠中' in text   # 中文不转义，打开文件就能看
    on_disk = json.loads(text)
    d = progress_to_dict(p, T0)
    assert set(d) - set(on_disk) == COMPUTED
    assert {k: v for k, v in d.items() if k not in COMPUTED} == on_disk
    assert d['sequence_started_at'] == '2026-11-17T10:00:00.123456'   # datetime 存 ISO 字符串，微秒保留
    assert progress_to_dict(Progress(), T0)['heartbeat_at'] is None
    json.dumps(d)   # 能直接交给 HTTP 响应序列化


# ---- Windows 上读者开着文件时的重试 ----
# 读者开着文件时，atomic_write 的替换会被拒（PermissionError）；读者赶上替换的瞬间，读也会被拒。
ATTEMPTS = progress_module._RETRY_ATTEMPTS


@pytest.fixture
def sleeps(monkeypatch) -> list:
    """重试间隔改成 0，并记下每次 sleep 的秒数：测试不用真等，也能核对只在两次尝试之间等待。"""
    recorded = []
    monkeypatch.setattr(progress_module, '_RETRY_DELAY', 0)
    monkeypatch.setattr(progress_module, 'time', SimpleNamespace(sleep=recorded.append))
    return recorded


def flaky(real, failures, calls, code):
    """包一层 real：前 failures 次（None 表示每次）抛 OSError(code, 'attempt N')，之后转调 real；calls 记下每次尝试。

    code 为 EACCES 时抛出的就是 PermissionError，与读者开着文件时 Windows 的拒绝同类。
    """
    def wrapper(*args, **kwargs):
        calls.append(args)
        if failures is None or len(calls) <= failures:
            raise OSError(code, f'attempt {len(calls)}')
        return real(*args, **kwargs)
    return wrapper


def deny_replace(monkeypatch, failures, code=errno.EACCES) -> list:
    """让 atomic_write 的替换步骤（读者开着文件时被拒的那一步）按 flaky 的规则失败，返回每次尝试的记录。"""
    calls = []
    monkeypatch.setattr(atomicwrites, 'replace_atomic', flaky(atomicwrites.replace_atomic, failures, calls, code))
    return calls


def deny_read(monkeypatch, failures, code=errno.EACCES) -> list:
    """让 Path.read_text 按 flaky 的规则失败，返回每次尝试的记录。"""
    calls = []
    monkeypatch.setattr(Path, 'read_text', flaky(Path.read_text, failures, calls, code))
    return calls


@pytest.mark.parametrize('failures', [1, 3, ATTEMPTS - 1])
def test_save_retries_when_replace_is_denied(tmp_path, monkeypatch, sleeps, failures):
    path = progress_path('oas1', tmp_path)
    calls = deny_replace(monkeypatch, failures)
    save_progress(path, full_progress())
    assert len(calls) == failures + 1   # 失败几次就多试几次，成功的那次是最后一次
    assert sleeps == [0] * failures     # 只在两次尝试之间等待
    assert [f.name for f in path.parent.iterdir()] == [path.name]   # 失败的几次没有留下临时文件
    assert load_progress(path) == full_progress()


def test_save_gives_up_after_all_attempts(tmp_path, monkeypatch, sleeps):
    path = progress_path('oas1', tmp_path)
    save_progress(path, Progress(row_index=1))   # 已有的旧文件
    calls = deny_replace(monkeypatch, None)
    with pytest.raises(PermissionError) as excinfo:
        save_progress(path, Progress(row_index=2))
    assert len(calls) == ATTEMPTS
    assert len(sleeps) == ATTEMPTS - 1                       # 最后一次失败后不再等
    assert excinfo.value.strerror == f'attempt {ATTEMPTS}'   # 抛出的是最后一次的异常
    assert [f.name for f in path.parent.iterdir()] == [path.name]   # 每次失败写下的临时文件都清掉了
    assert load_progress(path).row_index == 1                       # 旧文件原封不动


def test_save_does_not_retry_other_os_errors(tmp_path, monkeypatch, sleeps):
    path = progress_path('oas1', tmp_path)
    calls = deny_replace(monkeypatch, None, code=errno.EIO)
    with pytest.raises(OSError) as excinfo:
        save_progress(path, Progress())
    assert type(excinfo.value) is OSError   # 不是 PermissionError
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize('failures', [1, 3, ATTEMPTS - 1])
def test_load_retries_when_read_is_denied(tmp_path, monkeypatch, sleeps, failures):
    path = progress_path('oas1', tmp_path)
    save_progress(path, full_progress())
    calls = deny_read(monkeypatch, failures)
    assert load_progress(path) == full_progress()
    assert len(calls) == failures + 1
    assert sleeps == [0] * failures


def test_load_gives_up_after_all_attempts_and_raises(tmp_path, monkeypatch, sleeps):
    path = progress_path('oas1', tmp_path)
    save_progress(path, full_progress())
    calls = deny_read(monkeypatch, None)
    with pytest.raises(PermissionError) as excinfo:   # 不能拿默认进度顶替：调用方接着保存会覆盖真实的 saved_params
        load_progress(path)
    assert len(calls) == ATTEMPTS
    assert len(sleeps) == ATTEMPTS - 1
    assert excinfo.value.strerror == f'attempt {ATTEMPTS}'


def test_load_does_not_retry_other_os_errors(tmp_path, monkeypatch, sleeps):
    path = progress_path('oas1', tmp_path)
    save_progress(path, full_progress())
    calls = deny_read(monkeypatch, None, code=errno.EIO)
    with pytest.raises(OSError) as excinfo:
        load_progress(path)
    assert type(excinfo.value) is OSError
    assert len(calls) == 1 and sleeps == []


def test_adopt_retries_a_denied_move_and_keeps_the_legacy_file_when_it_gives_up(tmp_path, monkeypatch, sleeps):
    legacy = legacy_progress_path('oas1', tmp_path)
    save_progress(legacy, full_progress())
    calls = []
    monkeypatch.setattr(progress_module.os, 'replace', flaky(os.replace, None, calls, errno.EACCES))
    with pytest.raises(PermissionError):
        adopt_legacy_progress('oas1', tmp_path)
    assert len(calls) == ATTEMPTS and len(sleeps) == ATTEMPTS - 1
    assert load_progress(legacy) == full_progress() and not progress_path('oas1', tmp_path).exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='只有 Windows 会因为读者开着文件而拒绝替换')
def test_save_waits_for_a_reader_that_has_the_file_open(tmp_path, monkeypatch):
    path = progress_path('oas1', tmp_path)
    save_progress(path, Progress(row_index=1))
    waits = []
    with open(path, encoding='utf-8') as reader:   # 真的有一个读者开着文件，不是模拟

        def let_go(seconds):   # 第一次被拒、进入等待时，读者放手
            waits.append(seconds)
            reader.close()

        monkeypatch.setattr(progress_module, 'time', SimpleNamespace(sleep=let_go))
        save_progress(path, Progress(row_index=2))
    assert waits[:1] == [progress_module._RETRY_DELAY]   # 第一次尝试被真实的 PermissionError 拒绝，等过之后成功（杀毒等扫描器占着临时文件时可能多等几次）
    assert load_progress(path).row_index == 2
    assert [f.name for f in path.parent.iterdir()] == [path.name]
