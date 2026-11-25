"""合并上游用的入口清单与接入函数（module.server.upstream_hooks）。

接入函数的用例都用造出来的 m.py 文本，不碰仓库。test_hooks_reproduce_the_seven_files 只读本仓库的 git 历史：
最近一次"合并上游"合进来的官方版本接上清单，必须逐字等于当前 HEAD 的版本（换行符差异除外）。
"""
import subprocess
from pathlib import Path

import pytest

from module.server.upstream_hooks import Hook, HookError, apply_hooks, hook_files, overridden_bodies

REPO_ROOT = Path(__file__).resolve().parents[2]
SEVEN = ['script.py', 'module/config/config_model.py', 'module/config/config_menu.py', 'module/notify/notify.py',
         'module/server/app.py', 'module/server/home_router.py', 'deploy/git.py']


def git(*args):
    return subprocess.run(['git', *args], cwd=REPO_ROOT, capture_output=True, encoding='utf-8', check=True).stdout


SAMPLE = 'import a\n\ndef f():\n    x = 1\n    return x\n\n\ndef g():\n    pass\n'


def hk(action, anchor, *lines):
    return Hook('m.py', anchor, action, tuple(lines))


def test_after_before_and_replace():
    hooks = (hk('after', 'import a', 'import b'), hk('replace', '    x = 1', '    x = 2'), hk('before', 'def g():', '# g'))
    assert apply_hooks('m.py', SAMPLE, hooks) == (
        'import a\nimport b\n\ndef f():\n    x = 2\n    return x\n\n\n# g\ndef g():\n    pass\n')


def test_replace_body_keeps_the_def_line_and_the_trailing_blank_lines():
    assert apply_hooks('m.py', SAMPLE, (hk('replace_body', 'def f():', '    return 0'),)) == (
        'import a\n\ndef f():\n    return 0\n\n\ndef g():\n    pass\n')


@pytest.mark.parametrize('text, needle', [
    (SAMPLE.replace('import a', 'import z'), '找不到这一行：import a'),
    (SAMPLE + 'import a\n', '这一行出现了 2 次：import a'),
], ids=['missing', 'twice'])
def test_an_anchor_must_appear_exactly_once(text, needle):
    with pytest.raises(HookError) as caught:
        apply_hooks('m.py', text, (hk('after', 'import a', 'import b'),))
    assert str(caught.value) == f'm.py 的第 1 条入口{needle}'


def test_indentation_must_match():
    with pytest.raises(HookError):
        apply_hooks('m.py', SAMPLE, (hk('replace', 'x = 1', 'x = 2'),))


@pytest.mark.parametrize('hooks', [
    (hk('replace_body', 'def f():', '    return 0'), hk('after', '    x = 1', '    y = 1')),
    (hk('later', 'import a', 'import b'),),
], ids=['overlapping', 'unknown-action'])
def test_a_broken_list_raises_value_error(hooks):
    # 清单自身的错（两条入口改到同一处、动作写错）不是官方改动造成的：抛 ValueError，不混进 HookError 的原因文案
    with pytest.raises(ValueError):
        apply_hooks('m.py', SAMPLE, hooks)


def test_crlf_text_stays_crlf():
    text = SAMPLE.replace('\n', '\r\n')
    assert apply_hooks('m.py', text, (hk('after', 'import a', 'import b'),)) == (
        'import a\r\nimport b\r\n' + text[len('import a\r\n'):])


def test_a_file_without_hooks_is_returned_unchanged():
    assert apply_hooks('other.py', SAMPLE, (hk('after', 'import a', 'import b'),)) == SAMPLE


def test_overridden_bodies_reports_only_a_changed_body():
    hooks = (hk('replace_body', 'def f():', '    return 0'),)
    (found,) = overridden_bodies('m.py', SAMPLE, SAMPLE.replace('    x = 1', '    x = 3'), hooks)
    assert (found.path, found.function) == ('m.py', 'f')
    assert '-    x = 1' in found.diff and '+    x = 3' in found.diff
    assert overridden_bodies('m.py', SAMPLE, SAMPLE, hooks) == []


def test_hooks_reproduce_the_seven_files():
    # 最近一次"合并上游"合并提交的第二个父提交就是当时合进来的上游版本；我们的版本必须正好是它加上清单
    upstream = git('log', '--merges', '--grep=^合并上游', '-1', '--format=%P').split()[1]
    assert hook_files() == sorted(SEVEN)
    for path in hook_files():
        assert apply_hooks(path, git('show', f'{upstream}:{path}')) == git('show', f'HEAD:{path}'), path
