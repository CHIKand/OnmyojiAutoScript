import logging

from module.config.dotenv import read_env

def test_missing_env_file_returns_default(tmp_path, monkeypatch):
    monkeypatch.delenv('OAS_X', raising=False)
    assert read_env('OAS_X', 'd', env_file=tmp_path / '.env') == 'd'

def test_parse_quotes_spaces_comments(tmp_path, monkeypatch):
    for k in ('A', 'B', 'C'):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / '.env'
    f.write_text('# 注释\n\nA = 1 \nB="x y"\nC=\'z\'\n', encoding='utf-8')
    assert (read_env('A', env_file=f), read_env('B', env_file=f), read_env('C', env_file=f)) == ('1', 'x y', 'z')

def test_environment_overrides_file(tmp_path, monkeypatch):
    f = tmp_path / '.env'
    f.write_text('A=file\n', encoding='utf-8')
    monkeypatch.setenv('A', 'env')
    assert read_env('A', env_file=f) == 'env'

def test_non_utf8_bytes_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.delenv('A', raising=False)
    f = tmp_path / '.env'
    f.write_bytes(b'# \xd6\xd0\xce\xc4\nA=1\n')
    assert read_env('A', env_file=f) == '1'

def test_default_env_file_is_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv('A', raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('A=cwd\n', encoding='utf-8')
    assert read_env('A') == 'cwd'

def test_unreadable_env_file_returns_default(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('A', raising=False)
    f = tmp_path / '.env'
    f.mkdir()  # .env 是目录：读取必抛 OSError（Windows 上为 PermissionError）
    assert read_env('A', 'd', env_file=f) == 'd'
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'Error' in warnings[0].getMessage()
    assert str(f) in warnings[0].getMessage()
