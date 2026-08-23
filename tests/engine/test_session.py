# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/23 12:20:00
# @description : I1 BacktestSession 会话测试：买卖记账/T+1 拒单/零股清仓（设计 5.1/4.5）

"""BacktestSession 组件测试（阶段 I 生产路径）。

手算基准（平坦价 10.0, 5 号 bar 买 500k → 6 号 bar 开盘成交, 20 号 bar 清仓）:
  买 10.01×50000=500500 佣 50.05; 卖 9.99×50000=499500 佣 49.95
  末态现金 1e6-500500-50.05+499500-49.95 = 998900 → nav 0.9989, 佣金合计 100
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.runner import run_task
from mtzquant.engine.session import TaskConfig, normalize_universe
from tests.fixtures.backtest_env import make_backtest_env

CODE = "510300.SH"
BUY_AMOUNT = round(10.01 * 50_000, 2)  # 500500.0
SELL_AMOUNT = round(9.99 * 50_000, 2)  # 499500.0
FINAL_CASH = round(1_000_000 - BUY_AMOUNT - 50.05 + SELL_AMOUNT - 49.95, 2)  # 998900.0


def test_session_buy_sell_accounting(tmp_path: Path) -> None:
    """买卖全链路: 2 单 2 成交、费用 100、末态 nav 0.9989（手算, 4.9.2 六要素）。"""
    env = make_backtest_env(tmp_path)
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    b = result.bundle
    assert b.status == "completed_exact"
    assert len(b.orders) == 2 and len(b.fills) == 2
    # 成交逐笔
    buy, sell = b.fills
    assert buy["side"] == "buy" and abs(buy["price"] - 10.01) <= 1e-10
    assert buy["volume"] == 50_000 and abs(buy["amount"] - BUY_AMOUNT) <= 1e-6
    assert sell["side"] == "sell" and abs(sell["price"] - 9.99) <= 1e-10
    # 费用
    assert b.fees["commission"] == pytest.approx(100.0, abs=1e-6)
    assert b.fees["stamp_tax"] == 0.0
    # 末态净值（清仓）
    last = b.navs[-1]
    assert last["nav"] == pytest.approx(FINAL_CASH / 1_000_000, abs=1e-10)
    assert last["positions_value"] == 0.0
    # 现金恒等式
    assert last["cash"] == pytest.approx(FINAL_CASH, abs=1e-6)


def test_session_initial_positions(tmp_path: Path) -> None:
    """初始持仓: 首日估值价 = 首个交易日 close（5.5）。"""
    env = make_backtest_env(tmp_path)
    env.task.backtest.initial_positions = {CODE: 10_000.0}
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    first = result.bundle.navs[0]
    assert first["nav"] == pytest.approx((1_000_000 + 10_000 * 10.0) / 1_000_000, abs=1e-10)
    assert first["open_positions"] == 1


T1_SELL_STRATEGY = """\
def initialize(context):
    context.g["code"] = "510300.SH"
    context.g["bars"] = 0


def on_bar(context, bar):
    context.g["bars"] += 1
    n = context.g["bars"]
    if n == 5:
        context.adapter.order_target_value(context.g["code"], 500_000)
    elif n == 6:
        # 买入当日（成交于 6 号开盘）立即挂卖 → T+1 拒单（字符串方向 "sell" 亦可）
        context.adapter.order(context.g["code"], "sell", 100)
"""


def test_session_t1_sell_rejected_same_day(tmp_path: Path) -> None:
    """T+1: 当日买入不可卖 → 卖单 REJECTED（4.5/g03 语义, 字符串方向兼容）。"""
    env = make_backtest_env(tmp_path, strategy_text=T1_SELL_STRATEGY)
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    b = result.bundle
    sell = [o for o in b.orders if o["side"] == "sell"]
    assert sell and sell[0]["status"] == "rejected"
    assert sell[0]["reject_reason"] == "t_plus_sell_unavailable"
    # 买入本身成功
    assert any(o["status"] == "filled" for o in b.orders)


ODD_LOT_CLEAR_STRATEGY = """\
def initialize(context):
    context.g["code"] = "510300.SH"
    context.g["bars"] = 0


def on_bar(context, bar):
    context.g["bars"] += 1
    if context.g["bars"] == 1:
        context.adapter.order_target_value(context.g["code"], 0.0)
"""


def test_session_odd_lot_residual_can_be_cleared(tmp_path: Path) -> None:
    """零股尾仓可一次性清空（回归: ETF轮动V16.1 近似版 2025-06-11 后停摆根因）。

    旧逻辑 order_target_value(0) 对 83 股取 lot_round(83)=0 → 被 g08 静默忽略,
    尾仓永卖不掉、买入门 current_hold_size < target_num 恒 False → 全区间停摆。
    """
    env = make_backtest_env(tmp_path, strategy_text=ODD_LOT_CLEAR_STRATEGY)
    env.task.backtest.initial_positions = {CODE: 83.0}
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    b = result.bundle
    # 清仓卖单按 83 股全额成单（不再被取整吞掉）
    sells = [o for o in b.orders if o["side"] == "sell"]
    assert sells and sells[0]["qty"] == 83.0
    assert sells[0]["status"] == "filled"
    fills = [f for f in b.fills if f["side"] == "sell"]
    assert fills and fills[0]["volume"] == 83.0
    # 末态无持仓
    last = b.navs[-1]
    assert last["open_positions"] == 0
    assert last["positions_value"] == pytest.approx(0.0, abs=1e-6)


BUY_83_STRATEGY = """\
def initialize(context):
    context.g["code"] = "510300.SH"
    context.g["bars"] = 0


def on_bar(context, bar):
    context.g["bars"] += 1
    if context.g["bars"] == 1:
        # 目标金额 = 83 股 × 10.0 元 —— 不足一手, 买入侧仍强制整手 → 空单忽略
        context.adapter.order_target_value(context.g["code"], 830.0)
"""


def test_session_buy_side_stays_round_lot(tmp_path: Path) -> None:
    """买入侧仍强制整手: 目标不足一手（83 股）→ 空单 g08 忽略, 不产生持仓。

    与卖出零股语义对照 —— 只放行"一次性清空", 不允许买入零股（设计 4.5）。
    """
    env = make_backtest_env(tmp_path, strategy_text=BUY_83_STRATEGY)
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    b = result.bundle
    assert not any(o["side"] == "buy" for o in b.orders)  # 无买入订单
    last = b.navs[-1]
    assert last["open_positions"] == 0
    assert last["positions_value"] == pytest.approx(0.0, abs=1e-6)


def test_task_config_validation(tmp_path: Path) -> None:
    """TaskConfig pydantic: 缺必填字段 → ValidationError。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskConfig(task_name="x", strategy={"file": "s.py"}, backtest={}, universe=[])


def test_normalize_universe_dedup_order() -> None:
    """universe 代码归一去重且保持顺序（设计 3.4/8.8 确定性）。"""
    assert normalize_universe(["600000.SH", "600000.XSHG", "159915.SZ"]) == [
        "600000.SH",
        "159915.SZ",
    ]


def test_session_missing_strategy_file(tmp_path: Path) -> None:
    """策略文件缺失 → 结构化错误（runner 装配前校验）。"""
    env = make_backtest_env(tmp_path)
    env.task.strategy.file = str(tmp_path / "nope.py")
    with pytest.raises(MtzQuantError):
        run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
