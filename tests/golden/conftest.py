# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 23:10:00
# @description : 阶段 C golden 用例共享 fixture：交易日历/合成 K 线/公司行为数据构造

"""阶段 C golden 用例共享 fixture（测试方案 §2 数据构造器精简版）。

- `trade_calendar(start, n)`：交易日序列（跳过周六日，供 DayBar 按序生成）；
- `bar(code, dt, close, ...)`：单日 DayBar（open/high/low 默认=close，prev_close 由相邻日递推）；
- `series(code, closes)`：给定收盘价序列 → 逐日 DayBar 列表（连续交易日）；
- `corp_cash_div`: 现金分红 '{code}@{ex}@{pay}@{per_share}' 注册到 driver（g11）。

期望值一律由测试用例**手算字面量**断言（防自证，测试方案 §1.4/§2.4），
本 conftest 不参与任何业务计算。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mtzquant.core.errors import MtzQuantError

from .daily import DailyDriver, DayBar, LotFloor
from .framework import MockBroker

__all__ = ["trade_calendar", "make_bars", "flat_series", "corp_cash_div", "gdriver"]


def load_expected(case: str) -> dict[str, Any]:
    """读取 expected/{case}.json 手算期望值（防自证 §2.4：测试不反推引擎）。

    期望值文件由人工按场景算术独立计算（评审人可读），测试逐点 import 断言；
    生成/运行引擎不得改写该文件（不可回写）。
    """
    p = Path(__file__).parent / "expected" / f"{case}.json"
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def trade_calendar(start: str, n: int) -> list[datetime]:
    """连续交易日（自然日滤掉周末；节假日不处理——合成数据用）。

    日线 bar 的 dt 统一戳 15:00（4.7/g13：日线 bar 时间戳=收盘 15:00）。
    """
    d = datetime.strptime(start, "%Y-%m-%d").replace(hour=15, minute=0)
    out: list[datetime] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        # 日期轴推进保持在自然日步长（365 日轴；d 递增后时刻仍为 15:00）
        nxt = d + timedelta(days=1)
        d = nxt.replace(hour=15, minute=0)
    return out


def _prev_close(series: list[DayBar], i: int, default: float) -> float:
    return series[i - 1].close if i > 0 else default


def make_bars(
    code: str,
    closes: list[float],
    *,
    start: str = "2026-01-05",
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    suspended: set[int] | None = None,
) -> list[DayBar]:
    """由收盘价序列构造连续交易日 DayBar 列表（open/high/low 默认=close）。"""
    suspended = suspended or set()
    dts = trade_calendar(start, len(closes))
    bars: list[DayBar] = []

    # open 未给时按 close 递推 prev_close 前先收集 open
    opens_seq = opens if opens is not None else closes
    highs_seq = highs if highs is not None else closes
    lows_seq = lows if lows is not None else closes
    prev_close = closes[0]  # 首日 prev_close=首日 close（无前一日数据）
    for i, dt in enumerate(dts):
        close = closes[i] if i not in suspended else (bars[i - 1].close if i > 0 else closes[i])
        bars.append(
            DayBar(
                dt=dt,
                date=dt.strftime("%Y-%m-%d"),
                open=opens_seq[i],
                high=highs_seq[i],
                low=lows_seq[i],
                close=close,
                prev_close=prev_close,
                suspended=i in suspended,
                volume=1_000_000_000.0,  # 合成量充足: BrokerSim 容量(25%)不触发, 全量成交
            )
        )
        prev_close = bars[i].close
    return bars


def flat_series(
    code: str, n: int, *, price: float = 10.0, start: str = "2026-01-05"
) -> list[DayBar]:
    """平坦价格序列（g01/nav 计算用：价格恒定，波动=0）。"""
    return make_bars(code, [price] * n, start=start)


def corp_cash_div(
    driver: DailyDriver,
    code: str,
    *,
    ex: str,
    pay: str,
    per_share: float,
) -> None:
    """登记现金分红（g11）：ex 日开盘前记应收 + 除息调价，pay 日结算到账。

    除息日开盘价按 每股股利 下调（3.14 除息除权语义），driver 侧只做登记：
    实际扣减走 MockBroker/数据构造（测试里同步给出除息后的收盘序列）。
    """
    driver.on_dividend_pay(pay)

    def _on_ex() -> None:
        pos = driver.account.positions.get(code)
        if pos is None:
            raise MtzQuantError("分红登记时无持仓", stage="golden_conftest")
        dividend = pos.total_qty * per_share
        driver.account.credit_dividend(dividend)

    driver.on_day_open(ex, _on_ex)


@pytest.fixture()
def gdriver() -> DailyDriver:
    """默认黄金驱动（1e6 初始资金 + 默认费率），每用例独立。"""
    return DailyDriver(MockBroker(), initial_cash=1_000_000.0)


@pytest.fixture(scope="session")
def gold_lot() -> LotFloor:
    """整手语义对象（g08 边界断言用，lot=100）。"""
    return LotFloor(lot=100, price_step=0.01)
