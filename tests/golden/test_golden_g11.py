# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/16 00:10:00
# @description : g11 分红送转（账户事件）：ex 应收+送转稀释 + pay 到账 + NAV 连续性

"""黄金用例 g11：分红送转账户事件（测试方案 §5 g11，防自证第二严用例）。

场景：持有 20,000 股（成本 10.0，价格平坦 10.0）；除权除息价按设计 3.14 同步修正：
- 现金分红：ex=day20、pay=day25、每股 0.5 → 应收 20,000×0.5=10,000；
- 送转 10送10：ex=day20 → 数量 ×2=40,000，avg_cost 稀释 → 10/2=5.0；
- 除息除权价：10.0 − 0.5 = 9.5（除息），再 ÷2（拆股）→ 4.75（day20 起 close）。

恒等式（三时点 NAV 连续，手算 1e-10）：
- day19（除息前）：cash 1,000,000 + 20,000×10.0 = 1,200,000 → nav=1.2
- day20（除息除权日）：市值 40,000×4.75=190,000 + 应收 10,000 + cash 1,000,000
  equity=1,200,000 → nav=1.2（市值与应收之和保持，无漏损益）
- day25（pay 到账）：cash 1,010,000 + 市值 190,000 = 1,200,000 → nav=1.2（应收转现金）
"""

from __future__ import annotations

from typing import Any

from .conftest import load_expected, make_bars
from .daily import DailyDriver
from .framework import MockBroker, assert_six

EXP = load_expected("g11")  # §2.4 oracle：expected/g11.json 手算期望值

CODE = "600156.SH"
N = 30
QTY = 20_000
PX = 10.0
DIV_PER = 0.5
BONUS = 2.0  # 10送10 → ×2
EX_PX = (PX - DIV_PER) / BONUS  # (10.0−0.5)/2 = 4.75（除息+除权两步修正）
EX_NUM = 19  # day19(0-based) 起除权除息


def _adr() -> tuple[DailyDriver, dict[str, Any]]:
    """构造除权前价格 10.0、除权后 4.75 的双段价格路径 + 关键日期索引。"""
    closes = [PX] * EX_NUM + [EX_PX] * (N - EX_NUM)
    broker = MockBroker()
    driver = DailyDriver(broker, initial_cash=1_000_000.0, initial_positions={CODE: QTY})
    bars = make_bars(CODE, closes, start="2026-01-05")
    driver.add_data({CODE: bars})
    dates = {"d19": bars[18].date, "d20": bars[19].date, "d25": bars[24].date}
    return driver, dates


def test_g11_cash_div_and_bonus() -> None:
    """ex 日应收+送转、pay 日到账：账户要素逐项断言。"""
    driver, dates = _adr()
    d19, d20, d25 = dates["d19"], dates["d20"], dates["d25"]

    # 公司行为：day20 开盘前 送转×2 + 应收股息 10,000；day25 pay 到账
    def _corp_ex() -> None:
        pos = driver.account.positions[CODE]
        pos.apply_share_change(BONUS)  # 20,000→40,000，avg_cost 10→5.0
        driver.account.credit_dividend(EXP["dividend_amount"])  # 应收 EXP 手算

    def _on_d20() -> None:
        # day20 策略回调已见新数量 40,000（阶段②先于⑥）
        assert driver.account.positions[CODE].total_qty == EXP["shares_after"]
        assert driver.account.positions[CODE].avg_cost == EXP["avg_cost_after"]

    driver.on_day_open(d20, _corp_ex)
    driver.on(d20, _on_d20)
    driver.on_dividend_pay(d25)
    snap = driver.run()

    # ---- 账户要素（oracle：expected/g11.json 手算末态） ----
    end = EXP["end_state"]
    pos_end = end["position"]
    assert_six(
        snap,
        positions={
            CODE: {
                "total_qty": pos_end["total_qty"],
                "avg_cost": pos_end["avg_cost"],
                "last_price": pos_end["last_price"],
                "market_value": pos_end["market_value"],
            },
        },
        status=end["status"],
    )

    # day25 现金分红到账：available=EXP 手算 1,010,000；应收清零
    assert abs(driver.account.available_cash - end["available_cash"]) <= 1e-6
    assert driver.account.receivable_cash == end["receivable_cash"]

    # ---- NAV 连续性（逐点，oracle：day19/20/25 全 =1.2） ----
    chk_map = {cp["day"]: cp["nav"] for cp in EXP["checkpoints"]}
    nav_map = {p.dt.date().isoformat(): p.nav for p in snap.nav_series}
    assert abs(nav_map[d19] - chk_map[19]) <= 1e-10  # 除息前
    assert abs(nav_map[d20] - chk_map[20]) <= 1e-10  # 除息除权日（市值+应收）
    assert abs(nav_map[d25] - chk_map[25]) <= 1e-10  # 到账日（应收已转现金）


def test_g11_receivable_unavailable_at_ex() -> None:
    """ex 日应收 10,000 但 available 不变（未到账不可用）。"""
    _ = _adr()
    # 语义在 account 层已由 credit_dividend/settle_dividend 保证，此处做隔离确认：
    from mtzquant.engine.account import Account

    acct = Account(run_id="x", initial_cash=1_000_000.0, available_cash=1_000_000.0)
    acct.credit_dividend(10_000.0)
    assert acct.receivable_cash == 10_000.0
    assert acct.available_cash == 1_000_000.0
    assert acct.total_cash == 1_010_000.0
    settled = acct.settle_dividend()
    assert settled == 10_000.0
    assert acct.available_cash == 1_010_000.0
    acct.assert_invariant()
