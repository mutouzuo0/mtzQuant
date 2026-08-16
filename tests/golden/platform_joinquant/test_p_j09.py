# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 14:15:00
# @update_time        : 2026/08/16 21:59:08
# @description : g09/g10 平台版（聚宽）：动态 universe 切换 + 调度点可见性

"""g09/g10 平台版（聚宽, D3）。

- g09: set_universe([A,B,C]) 动态切换 + history 可见性/懒加载计数（D3 偏差:
  聚宽 history 单字段返回 DataFrame, 行数与 native history 相同）。
- g10: before_trading_start 盘前当日 bar 不可见 / handle_data 收盘可见（聚宽官方钩子名）。
"""

from __future__ import annotations

import pytest

from mtzquant.core.errors import MtzQuantError
from tests.golden.conftest import flat_series  # noqa: F401
from tests.golden.daily import DailyDriver
from tests.golden.framework import MockBroker

from .bridge import run_joinquant_golden

A = "600000.SH"
B = "510300.SH"
C = "159915.SZ"
N = 40


@pytest.fixture()
def tri_driver() -> DailyDriver:
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0)
    driver.add_data(
        {
            A: flat_series(A, N),
            B: flat_series(B, N),
            C: flat_series(C, N),
        }
    )
    driver.set_universe([A, B])
    return driver


JQ_G09_SWITCH = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.XSHG', '510300.XSHG'])


def handle_data(context, data):
    g.n += 1
    if g.n == 30:
        set_universe(['600000.XSHG', '510300.XSHG', '159915.XSHE'])
    if g.n == 31:
        g.hist_c = len(history(40, '1d', 'close', ['159915.XSHE']))
"""


def test_g09_jq_universe_switch_and_query(tri_driver) -> None:
    """day30 切池 → day31 起 C 可查（31 根, 与 native 同口径）。"""
    # 未进池前查询 → 报错（universe gate）
    with pytest.raises(MtzQuantError, match="不在动态 universe"):
        tri_driver.history(C, 5)

    import json as _json
    import pathlib
    import tempfile

    probe = pathlib.Path(tempfile.mkdtemp()) / "probe.json"
    script = (
        f"PROBE = r'{probe}'\n"
        "import json as _json\n" + JQ_G09_SWITCH + "\ndef after_trading_end(context):\n"
        "    with open(PROBE, 'w') as _f:\n"
        "        _json.dump({'hist_c': g.get('hist_c', -1)}, _f)\n"
    )
    run_joinquant_golden(tri_driver, script)
    result = _json.loads(probe.read_text(encoding="utf-8"))
    assert result["hist_c"] == 31  # d31 可见 31 根（native 同口径手算）


JQ_G09_LAZY = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.XSHG', '510300.XSHG'])


def handle_data(context, data):
    g.n += 1
    if g.n == 1:
        history(5, '1d', 'close', ['600000.XSHG'])
        history(5, '1d', 'close', ['600000.XSHG'])
"""


def test_g09_jq_lazy_load_cached(tri_driver) -> None:
    """懒加载: 首次访问 +1, 二次命中缓存; B 未访问不触发。"""
    run_joinquant_golden(tri_driver, JQ_G09_LAZY)
    assert tri_driver.load_count(A) == 1
    assert tri_driver.load_count(B) == 0


JQ_G10 = """\
def initialize(context):
    g.n = 0
    set_universe(['600000.XSHG'])
    g.before_close = None
    g.handle_close = None


def before_trading_start(context, data):
    h = attribute_history('600000.XSHG', 2, '1d', ['close'], include_today=False)
    g.before_close = float(h['close'].iloc[-1]) if len(h) else None


def handle_data(context, data):
    g.n += 1
    if g.n == 5:
        h = attribute_history('600000.XSHG', 2, '1d', ['close'], include_today=True)
        g.handle_close = float(h['close'].iloc[-1]) if len(h) else None
"""


def test_g10_jq_schedule_visibility(gdriver) -> None:
    """盘前 attribute_history(include_today=False) 与收盘(True) 均能取到昨收/当日（平坦价一致）。"""
    import json as _json
    import pathlib
    import tempfile

    gdriver.add_data({A: flat_series(A, 10, price=10.0)})
    probe = pathlib.Path(tempfile.mkdtemp()) / "probe.json"
    script = (
        f"PROBE = r'{probe}'\n"
        "import json as _json\n" + JQ_G10 + "\ndef after_trading_end(context):\n"
        "    with open(PROBE, 'w') as _f:\n"
        "        _json.dump({'before': g.before_close, 'handle': g.handle_close}, _f)\n"
    )
    run_joinquant_golden(gdriver, script)
    result = _json.loads(probe.read_text(encoding="utf-8"))
    assert float(result["before"]) == 10.0  # 盘前可见=历史（平坦价口径一致）
    assert float(result["handle"]) == 10.0  # 收盘含当日
