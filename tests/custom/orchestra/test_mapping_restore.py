"""restore_params 的出错分支：一项还原不了不挡住其余项，最后统一抛一次，列出所有没还原的路径。

test_mapping.py 照简报原样保留，这些用例单独放在这里。
"""
from datetime import time

import pytest

from module.config.config_model import ConfigModel
from module.orchestra.mapping import restore_params
from tasks.Orochi.config import Layer

LAYER = 'orochi.orochi_config.layer'
COUNT = 'orochi.orochi_config.limit_count'
LIMIT_TIME = 'orochi.orochi_config.limit_time'


def dirty_model() -> ConfigModel:
    """三个字段都是和待还原的原值不同的值。"""
    model = ConfigModel()
    config = model.orochi.orochi_config
    config.layer, config.limit_count, config.limit_time = Layer.TWELVE, 99, time(0, 5)
    return model


def test_an_undeserializable_value_does_not_stop_the_rest():
    model = dirty_model()
    saved = {LAYER: '不存在的层', COUNT: 30, LIMIT_TIME: '00:20:00'}   # 转不回去的排在最前面
    with pytest.raises(ValueError) as info:
        restore_params(model, saved)
    config = model.orochi.orochi_config
    assert (config.limit_count, config.limit_time) == (30, time(0, 20))   # 排在后面的两项照样还原了
    assert config.layer == Layer.TWELVE   # 转不回去的那一项保持原样，不写成别的
    assert LAYER in str(info.value) and '不存在的层' in str(info.value)   # 消息里有路径，也有原值
    assert COUNT not in str(info.value) and LIMIT_TIME not in str(info.value)   # 还原成功的不会列进去


@pytest.mark.parametrize('raw', ['abc', '25:99:99', 123, None])
def test_a_corrupt_time_is_a_value_level_failure_whatever_its_type(raw):
    model = dirty_model()
    with pytest.raises(ValueError) as info:   # fromisoformat 对坏字符串抛 ValueError，对非字符串抛 TypeError
        restore_params(model, {LIMIT_TIME: raw, COUNT: 30})
    config = model.orochi.orochi_config
    assert config.limit_count == 30 and config.limit_time == time(0, 5)
    assert LIMIT_TIME in str(info.value)


def test_the_message_lists_every_path_that_failed_both_kinds():
    model = dirty_model()
    saved = {'orochi.no_such_group.limit_count': 1, LAYER: '不存在的层', COUNT: 30,
             'orochi.orochi_config.no_such_field': 2, LIMIT_TIME: 'abc'}
    with pytest.raises(ValueError) as info:
        restore_params(model, saved)
    message = str(info.value)
    for path in ('orochi.no_such_group.limit_count', 'orochi.orochi_config.no_such_field', LAYER, LIMIT_TIME):
        assert path in message
    assert COUNT not in message
    assert model.orochi.orochi_config.limit_count == 30


def test_only_unresolvable_paths_keep_the_original_message():
    model = dirty_model()
    saved = {'orochi.no_such_group.limit_count': 1, COUNT: 30, 'orochi.orochi_config.no_such_field': 2}
    with pytest.raises(ValueError) as info:
        restore_params(model, saved)
    assert str(info.value) == ('编排映射的字段路径无法解析，未还原：'
                               'orochi.no_such_group.limit_count, orochi.orochi_config.no_such_field')
    assert model.orochi.orochi_config.limit_count == 30


def test_a_field_that_cannot_be_written_is_reported_and_skipped(monkeypatch):
    model = dirty_model()
    real_deep_set = ConfigModel.deep_set

    def refuse_layer(obj, keys, value):   # 只有这一个字段写不进去
        return False if str(keys) == LAYER else real_deep_set(obj, keys, value)
    monkeypatch.setattr(ConfigModel, 'deep_set', staticmethod(refuse_layer))
    with pytest.raises(ValueError) as info:
        restore_params(model, {LAYER: '悲鸣', COUNT: 30})
    assert LAYER in str(info.value)
    assert model.orochi.orochi_config.limit_count == 30


def test_the_message_carries_each_underlying_exception():
    model = dirty_model()
    with pytest.raises(ValueError) as info:
        restore_params(model, {LAYER: '不存在的层', LIMIT_TIME: 123})
    message = str(info.value)
    assert 'is not a valid Layer' in message   # 枚举转不回去：ValueError 的原文
    assert 'TypeError' in message              # 时间不是字符串：fromisoformat 的 TypeError


def test_nothing_to_report_when_every_field_restores():
    model = dirty_model()
    restore_params(model, {LAYER: '悲鸣', COUNT: 30, LIMIT_TIME: '00:20:00'})   # 不抛异常
    config = model.orochi.orochi_config
    assert (config.layer, config.limit_count, config.limit_time) == (Layer.ELEVEN, 30, time(0, 20))
