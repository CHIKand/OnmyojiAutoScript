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
