# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : 黄金用例断言框架（4.9.2）：六要素断言工具 + GoldenCase + MockBroker

"""黄金用例断言框架（设计 4.9.2，S3 级验收资产）。

六要素断言（订单/成交/现金/持仓/费用/净值）以引擎无关的 ``RunSnapshot`` 为载体：
阶段 C 由 MockBroker 驱动出快照，阶段 F 切换真实 BrokerSim 后同一套断言**不改**全绿。

断言工具：assert_orders / assert_fills / assert_cash_ledger / assert_positions /
          assert_fees / assert_nav_series —— 逐笔/逐点比对，浮点容差 1e-10。

MockBroker：可编程执行通道——按预程序化的规则返回 成交/拒单/部分成交/一字板
            等响应（阶段 F 由 BrokerSim 的真实撮合语义接管，断言层不变）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mtzquant.engine.orders import (
    Fill,
    Order,
    OrderEvent,
    OrderEventType,
    OrderStatus,
    transition_order,
)

__all__ = [
    "FILL_TOL",
    "PositionSnapshot",
    "CashLedger",
    "NavPoint",
    "RunSnapshot",
    "assert_orders",
    "assert_fills",
    "assert_cash_ledger",
    "assert_positions",
    "assert_fees",
    "assert_nav_series",
    "assert_six",
    "MockBroker",
    "GoldenCase",
]

FILL_TOL = 1e-10  # 黄金用例断言浮点容差（测试方案 §5 要求，绝不放宽）


# ============================================================
# 运行快照：六要素断言的对象（引擎无关）
# ============================================================


@dataclass(frozen=True)
class PositionSnapshot:
    """持仓末态快照（4.9.2 持仓要素：数量/成本/市价/市值）。"""

    code: str
    total_qty: float
    avg_cost: float
    last_price: float
    market_value: float


@dataclass
class CashLedger:
    """现金流水：逐事件分类账（断言逐行比对时刻/金额/备注）。"""

    available: list[tuple[datetime, float, str]] = field(default_factory=list)

    def post(self, when: datetime, amount: float, note: str) -> None:
        self.available.append((when, amount, note))


@dataclass(frozen=True)
class NavPoint:
    """净值序列单点（日线逐点断言：时刻/净值/总权益）。

    - ``stale_codes``: 当日采用"非最新收盘价"估值的标的清单（停牌沿用/退市冻结，
      设计 5.5）——g05/g12 断言用；
    - ``open_positions``: 当日持仓标的数（退市后终止计数，g12 断言用）。
    """

    dt: datetime
    nav: float
    equity: float
    stale_codes: tuple[str, ...] = ()
    open_positions: int | None = None


@dataclass
class RunSnapshot:
    """一次黄金用例运行的全部结果载体（六要素）。"""

    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    order_events: list[OrderEvent] = field(default_factory=list)
    cash: CashLedger = field(default_factory=CashLedger)
    positions: dict[str, PositionSnapshot] = field(default_factory=dict)
    nav_series: list[NavPoint] = field(default_factory=list)
    fees: dict[str, float] = field(default_factory=dict)
    status: str = "completed_exact"
    degradations: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (kind, detail)

    @property
    def fee_total(self) -> float:
        return sum(self.fees.values())

    @property
    def daily_nav_rows(self) -> int:
        return len(self.nav_series)


# ============================================================
# 断言工具（逐笔/逐点比对，容差 1e-10）
# ============================================================


def _almost(a: float, b: float) -> bool:
    if a == b:
        return True
    return abs(a - b) <= FILL_TOL * max(1.0, abs(a), abs(b))


def assert_orders(snap: RunSnapshot, expected: list[dict[str, Any]]) -> None:
    """逐单断言：code/side/status/qty/limit_price 关键字段比对。"""
    assert len(snap.orders) == len(expected), f"订单行数 {len(snap.orders)} != {len(expected)}"
    for actual, exp in zip(snap.orders, expected, strict=True):
        assert actual.code == exp["code"], f"{actual.order_id}: code {actual.code} != {exp['code']}"
        if "side" in exp:
            assert actual.side is exp["side"], (
                f"{actual.order_id}: side {actual.side} != {exp['side']}"
            )
        if "status" in exp:
            assert actual.status is exp["status"], (
                f"{actual.order_id}: status {actual.status} != {exp['status']}"
            )
        if "qty" in exp:
            assert _almost(actual.qty, exp["qty"]), (
                f"{actual.order_id}: qty {actual.qty} != {exp['qty']}"
            )


def assert_fills(snap: RunSnapshot, expected: list[dict[str, Any]]) -> None:
    """逐笔成交断言：code/side/price/volume/amount（金额=price×volume，容差 1e-10）。"""
    assert len(snap.fills) == len(expected), f"成交行数 {len(snap.fills)} != {len(expected)}"
    for actual, exp in zip(snap.fills, expected, strict=True):
        assert actual.code == exp["code"]
        assert actual.side is exp["side"]
        assert _almost(actual.price, exp["price"])
        assert _almost(actual.volume, exp["volume"])
        assert _almost(actual.amount, exp["amount"])
        if "fill_time" in exp:
            assert actual.fill_time == exp["fill_time"]


def assert_cash_ledger(snap: RunSnapshot, expected: list[tuple[Any, float, str]]) -> None:
    """现金流水逐行比对（（时刻, 金额, 备注），金额为入账的增减额）。"""
    actual = snap.cash.available
    assert len(actual) == len(expected), f"现金流水 {len(actual)} != {len(expected)}"
    for (a_when, a_amount, a_note), (e_when, e_amount, e_note) in zip(
        actual, expected, strict=True
    ):
        assert a_when == e_when, f"现金流水时刻 {a_when} != {e_when}"
        assert _almost(a_amount, e_amount), f"现金流水金额 {a_amount} != {e_amount}"
        assert a_note == e_note, f"现金流水备注 {a_note} != {e_note}"


def assert_positions(snap: RunSnapshot, expected: dict[str, dict[str, float]]) -> None:
    """持仓末态断言：{code: {total_qty/avg_cost/last_price/market_value}} 逐码比对。"""
    assert set(snap.positions) == set(expected), (
        f"持仓代码集 {set(snap.positions)} != {set(expected)}"
    )
    for code, exp in expected.items():
        pos = snap.positions[code]
        for key, value in exp.items():
            assert hasattr(pos, key), f"{code}: 无属性 {key}"
            assert _almost(getattr(pos, key), value), (
                f"{code}.{key}: {getattr(pos, key)} != {value}"
            )


def assert_fees(snap: RunSnapshot, expected: dict[str, float]) -> None:
    """费用断言：分项费用（commission/stamp_tax/transfer_fee）+ 总额 total。"""
    for key, value in expected.items():
        if key == "total":
            assert _almost(snap.fee_total, value), f"费用总额 {snap.fee_total} != {value}"
        else:
            assert key in snap.fees, f"缺少费用分项 {key}"
            assert _almost(snap.fees[key], value), f"{key}: {snap.fees[key]} != {value}"


def assert_nav_series(snap: RunSnapshot, expected: list[dict[str, Any]]) -> None:
    """净值逐点断言：每点 {dt, nav}（可含 equity），行数先行校验。"""
    assert len(snap.nav_series) == len(expected), (
        f"净值点数 {len(snap.nav_series)} != {len(expected)}"
    )
    for point, exp in zip(snap.nav_series, expected, strict=True):
        assert point.dt == exp["dt"]
        assert _almost(point.nav, exp["nav"]), f"{point.dt}: nav {point.nav} != {exp['nav']}"
        if "equity" in exp:
            assert _almost(point.equity, exp["equity"]), (
                f"{point.dt}: equity {point.equity} != {exp['equity']}"
            )


def assert_six(
    snap: RunSnapshot,
    *,
    orders: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
    cash: list[tuple[Any, float, str]] | None = None,
    positions: dict[str, dict[str, float]] | None = None,
    fees: dict[str, float] | None = None,
    nav: list[dict[str, Any]] | None = None,
    status: str | None = None,
) -> None:
    """六要素联合断言入口（黄金用例标准调用：逐项给定则逐项校验）。"""
    if orders is not None:
        assert_orders(snap, orders)
    if fills is not None:
        assert_fills(snap, fills)
    if cash is not None:
        assert_cash_ledger(snap, cash)
    if positions is not None:
        assert_positions(snap, positions)
    if fees is not None:
        assert_fees(snap, fees)
    if nav is not None:
        assert_nav_series(snap, nav)
    if status is not None:
        assert snap.status == status, f"运行状态 {snap.status} != {status}"


# ============================================================
# MockBroker：可编程执行通道（阶段 F 由 BrokerSim 替换，断言不变）
# ============================================================


class MockBroker:
    """可编程撮合通道：预约订单按预先指定的规则立即产生响应。

    规则键为 order 的 order_id，响应 dict：
      {"type": "fill",    "price": 10.0, "qty": 100}              → 完全成交
      {"type": "partial", "price": 10.0, "qty": 40}               → 部分成交 40
      {"type": "reject",  "reason": "insufficient_cash"}          → 拒单
      {"type": "expire",  "info": "one_word_limit"}               → 过期
      {"type": "one_word_board"}                                  → 一字板过期
    未预约的订单默认按市价全量成交（price=10.0）——空仓策略等用例天然不使用它。
    """

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.submitted: list[Order] = []
        self.accepted: list[Order] = []
        self.rejected: list[tuple[Order, str]] = []
        self.expired: list[tuple[Order, str]] = []
        self.fills: list[Fill] = []
        self.events: list[OrderEvent] = []
        self._seq = 0

    # ---- 编程接口 ----

    def program(self, order_id: str, response: dict[str, Any]) -> None:
        self.responses[order_id] = response

    # ---- 执行通道 ----

    def submit(self, order: Order) -> None:
        """受理订单（accepted→pending）并立刻按规则产生对应响应。"""
        self.submitted.append(order)
        self.accepted.append(order)
        self.events.append(
            OrderEvent(
                order_id=order.order_id,
                event_type=OrderEventType.ACCEPTED,
                event_time=order.submitted_at,
            )
        )
        rule = self.responses.get(order.order_id) or {
            "type": "fill",
            "price": 10.0,  # 未预约订单默认市价 10.0 全量成交
            "qty": order.qty,
        }
        kind = rule["type"]
        if kind == "reject":
            self._reject(order, rule)
        elif kind in ("expire", "one_word_board"):
            self._expire(order, rule, one_word=(kind == "one_word_board"))
        elif kind == "partial":
            self._fill(order, price=rule["price"], qty=rule["qty"], partial=True)
        else:
            self._fill(order, price=rule["price"], qty=rule.get("qty", order.qty), partial=False)

    # ---- 内部 ----

    def _reject(self, order: Order, rule: dict[str, Any]) -> None:
        reason = rule.get("reason", "rejected")
        # 受理前拒单（5.3.1）：不产生迁移事件，订单直接置终态 REJECTED
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.rejected.append((order, reason))
        self.events.append(
            OrderEvent(
                order_id=order.order_id,
                event_type=OrderEventType.REJECTED,
                event_time=order.submitted_at,
                info_json={"reason": reason},
            )
        )

    def _expire(self, order: Order, rule: dict[str, Any], *, one_word: bool) -> None:
        info = rule.get("info", "expired")
        info_json: dict[str, Any] = {"info": info}
        if one_word:
            info_json["one_word_limit"] = True  # 一字板标记：4.9.2 g04 要素
        self.events.append(
            transition_order(
                order,
                OrderEventType.EXPIRE,
                event_time=order.submitted_at,
                info_json=info_json,
            )
        )
        self.expired.append((order, info))

    def _fill(self, order: Order, *, price: float, qty: float, partial: bool) -> None:
        qty = min(qty, order.qty)
        event_type = OrderEventType.PARTIAL_FILL if partial else OrderEventType.FILL
        fill_time = order.eligible_fill_at or order.submitted_at
        self.events.append(
            transition_order(order, event_type, event_time=fill_time, qty=qty, price=price)
        )
        self.fills.append(
            Fill(
                order_id=order.order_id,
                code=order.code,
                side=order.side,
                price=price,
                volume=qty,
                fill_time=fill_time,
            )
        )


# ============================================================
# GoldenCase 声明式结构
# ============================================================


@dataclass(frozen=True)
class GoldenCase:
    """黄金用例声明（4.9.2）：场景描述/数据构造器/策略执行/六要素预期。

    - ``data_builder``: 传入种子返回市场数据 dict（code → {hist/last_price/suspended/limit_up…}）
    - ``run``: 传（数据, MockBroker）返回 RunSnapshot——用例自身的驱动逻辑
    - ``markers``: pytest 标记，如 g13 分钟侧 ``("m5_deferred",)``（阶段 C 跳过、阶段 F 启用）
    """

    case_id: str  # g01..g13
    name: str
    description: str
    data_builder: Callable[[dict[str, Any]], dict[str, Any]]
    run: Callable[[dict[str, Any], MockBroker], RunSnapshot]
    markers: tuple[str, ...] = ()

    def pytest_id(self) -> str:
        return self.case_id
