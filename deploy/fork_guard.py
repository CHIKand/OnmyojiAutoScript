# This Python file uses the following encoding: utf-8
"""启动器更新前的保护：找出 HEAD 上不属于任何远端引用的本地提交。

deploy/git.py 在 reset --hard 之前调用。存在这类提交时跳过 reset/pull，
避免冲掉尚未推送的合并或补图。只用标准库：安装器运行时第三方依赖可能还没装好。
"""
import os
import subprocess

WARNING_TEMPLATE = ('发现 {count} 个未推送到任何远端的本地提交，跳过本次 reset/pull。'
                    '请先执行 git push origin HEAD:{branch} 后再启动')


def _git(git: str, repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([git, *args], cwd=repo, capture_output=True, text=True,
                          env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})


def local_only_commits(git: str, repo: str = '.') -> list[str]:
    """返回 `git rev-list HEAD --not --remotes` 的提交号；HEAD 尚无提交时返回空列表。"""
    if _git(git, repo, 'rev-parse', '--verify', '--quiet', 'HEAD').returncode != 0:
        return []
    result = _git(git, repo, 'rev-list', 'HEAD', '--not', '--remotes')
    if result.returncode != 0:
        raise RuntimeError(f'git rev-list 执行失败：{result.stderr.strip()}')
    return result.stdout.split()
