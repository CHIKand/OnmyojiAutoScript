import os
from pathlib import Path


def read_env(key: str, default: str | None = None, env_file: Path | None = None) -> str | None:
    """读取配置项：进程环境变量优先，其次 .env 文件，都没有则返回 default。

    env_file 缺省为 Path.cwd() / '.env'。文件逐行按 KEY=VALUE 解析：
    跳过空行和 # 开头的行，键和值两边去空格，再去掉成对的单引号或双引号；
    同名键以后出现的为准。仅用标准库，每次调用都重新读文件，不做缓存。
    """
    from_env = os.environ.get(key)
    if from_env is not None:
        return from_env
    path = Path.cwd() / '.env' if env_file is None else env_file
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return default
    result = default
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, sep, raw = line.partition('=')
        if not sep or name.strip() != key:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
            value = value[1:-1]
        result = value
    return result
