import json
import logging
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from module.orchestra.progress import Progress, load_progress, progress_path, progress_to_dict, save_progress

T0 = datetime(2026, 11, 17, 10, 0, 0, 123456)   # 带微秒：往返之后必须原样相等
COMPUTED = {'row_elapsed_seconds', 'rest_remaining_seconds'}


def full_progress() -> Progress:
    """每个字段都取非默认值。"""
    return Progress(
        phase='resting',
        row_index=3,
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
    assert progress_path('oas1', tmp_path) == tmp_path / 'log' / '.orchestra_oas1.json'
    monkeypatch.chdir(tmp_path)
    assert progress_path('oas1') == Path.cwd() / 'log' / '.orchestra_oas1.json'   # 缺省取当前目录


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
