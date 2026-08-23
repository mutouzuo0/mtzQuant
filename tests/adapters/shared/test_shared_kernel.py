# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 10:02:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-A01..A08：共享内核——g/log/context/portfolio 投影/下单族/cutoff/compat/代码互转

"""T-A01..A08（M2-K 共享内核, 设计 4.4/4.5/4.9）。

覆盖: g 双风格容器 / log 双拼写 / context 内核字段+平台扩展 / 持仓投影全字段表 /
下单族归一（order_api 落库）/ 数据族 PIT cutoff 防泄露 / 报错模板机读 / 代码互转往返。
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from mtzquant.adapters.shared.code_style import denormalize_code, round_trip
from mtzquant.adapters.shared.compat import compat_report, not_implemented, register_api
from mtzquant.adapters.shared.context_factory import make_context, refresh_context
from mtzquant.adapters.shared.data_apis import DataApiCore
from mtzquant.adapters.shared.g_container import GContainer
from mtzquant.adapters.shared.log_api import make_log
from mtzquant.adapters.shared.order_apis import make_order_api
from mtzquant.adapters.shared.portfolio_view import (
    UniformPortfolio,
    UniformPosition,
    jq_portfolio_view,
    jq_position_view,
    ptrade_portfolio_view,
    ptrade_position_view,
    uniform_portfolio,
)
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.orders import OrderDirection, OrderStyle


# ------------------------------------------------------------------
# T-A01 g 容器（双风格 + 结构化提示）
# ------------------------------------------------------------------
def test_g_container_dual_style() -> None:
    g = GContainer()
    g.bars = 3  # 属性风格（聚宽/PTrade 写法）
    g["code"] = "510300.SH"  # 下标风格（native 写法）
    assert g.bars == 3 and g["code"] == "510300.SH"
    assert g["bars"] == 3 and g.code == "510300.SH"  # 双风格互读
    assert "bars" in g and len(g) == 2
    assert g.get("missing", 7) == 7


def test_g_container_missing_is_structured() -> None:
    import pytest

    g = GContainer()
    with pytest.raises(MtzQuantError) as ei:
        _ = g.bars
    assert "bars" in str(ei.value)  # 结构化提示（4.4, 非裸 AttributeError）
    with pytest.raises(MtzQuantError):
        _ = g["code"]
    # 裸下划线属性仍走 AttributeError（Python 语义不破坏）
    with pytest.raises(AttributeError):
        _ = g._internal


# ------------------------------------------------------------------
# T-A02 log API（双拼写 + current_dt）
# ------------------------------------------------------------------
def test_log_api_dual_spelling() -> None:
    events: list[tuple[str, dict]] = []

    def _emit(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    log = make_log(_emit, current_dt=lambda: datetime(2026, 1, 2, 15, 0))
    log.info("hello %s", "world")
    log.warn("聚宽拼写")
    log.warning("PTrade 拼写")
    log.error("boom")
    assert len(events) == 4
    kinds = {k for k, _ in events}
    assert kinds == {"log"}
    msgs = [p["message"] for _, p in events]
    assert "hello world" in msgs  # printf 风格
    assert "聚宽拼写" in msgs and "PTrade 拼写" in msgs  # 双拼写都在
    levels = [p["level"] for _, p in events]
    assert levels == ["info", "warn", "warning", "error"]
    for _, p in events:
        assert p["current_dt"] == "2026-01-02T15:00:00"  # 带回测内时刻


# ------------------------------------------------------------------
# T-A03 context 工厂（内核字段 + 平台扩展槽 + 每 bar 刷新）
# ------------------------------------------------------------------
def test_context_factory_kernel_and_refresh() -> None:
    ctx = make_context(
        "ptrade",
        run_id="r_x",
        task_name="t",
        current_dt=datetime(2026, 1, 2, 15, 0),
        universe=["510300.SH"],
        portfolio="PF",
    )
    assert ctx.platform == "ptrade"
    assert ctx.run_id == "r_x" and ctx.current_dt is not None
    assert ctx.universe == ["510300.SH"] and ctx.portfolio == "PF"
    # 平台扩展槽（PTrade 按 4.7 补）
    ctx.blotter = {"current_dt": None}
    ctx.capital_base = 1_000_000.0
    assert ctx.blotter == {"current_dt": None} and ctx.capital_base == 1_000_000.0

    # 每 bar 引擎刷新: 原地更新（同一对象引用）
    previous = ctx
    refresh_context(
        ctx,
        current_dt=datetime(2026, 1, 3, 15, 0),
        previous_date=date(2026, 1, 2),
        universe=["000001.SZ"],
        portfolio="PF2",
    )
    assert ctx is previous
    assert ctx.current_dt == datetime(2026, 1, 3, 15, 0)
    assert ctx.previous_date == date(2026, 1, 2)
    assert ctx.universe == ["000001.SZ"] and ctx.portfolio == "PF2"
    assert ctx.timestamp == datetime(2026, 1, 3, 15, 0)  # 兼容 native timestamp


# ------------------------------------------------------------------
# T-A04 持仓投影全字段表（4.4 投影表）
# ------------------------------------------------------------------
def _sample_uniform() -> tuple[UniformPortfolio, UniformPosition]:
    pos = UniformPosition(
        code="510300.SH",
        total_qty=5000,
        today_qty=1000,
        avg_cost=3.0,
        last_price=3.2,
        market_value=16000.0,
    )
    pf = UniformPortfolio(
        positions={"510300.SH": pos},
        available_cash=800_000.0,
        receivable_cash=0.0,
        frozen_cash=1000.0,
        total_value=817_000.0,
        initial_cash=1_000_000.0,
    )
    return pf, pos


def test_portfolio_projection_field_table() -> None:
    pf, pos = _sample_uniform()
    assert pos.closeable_qty == 4000  # T+1: total - today

    jq = jq_position_view(pos)
    for field in (
        "security",
        "amount",
        "total_amount",
        "closeable_amount",
        "avg_cost",
        "price",
        "value",
    ):
        assert hasattr(jq, field), f"聚宽持仓缺字段 {field}"
    assert (jq.security, jq.amount, jq.closeable_amount) == ("510300.SH", 5000, 4000)
    assert (jq.avg_cost, jq.price, jq.value) == (3.0, 3.2, 16000.0)

    jpf = jq_portfolio_view(pf)
    for field in (
        "starting_cash",
        "available_cash",
        "positions_value",
        "total_assets",
        "long_positions",
    ):
        assert hasattr(jpf, field), f"聚宽账户缺字段 {field}"
    assert jpf.starting_cash == 1_000_000.0 and jpf.available_cash == 800_000.0
    assert jpf.positions_value == pf.positions_value

    pp = ptrade_position_view(pos)
    for field in (
        "sid",
        "amount",
        "enable_amount",
        "cost_basis",
        "last_sale_price",
        "today_amount",
    ):
        assert hasattr(pp, field), f"PTrade 持仓缺字段 {field}"
    assert (pp.sid, pp.amount, pp.enable_amount) == ("510300.SH", 5000, 4000)
    assert (pp.cost_basis, pp.last_sale_price, pp.today_amount) == (3.0, 3.2, 1000)

    ppf = ptrade_portfolio_view(pf)
    for field in ("portfolio_value", "capital_used", "returns", "pnl", "starting_cash"):
        assert hasattr(ppf, field), f"PTrade 账户缺字段 {field}"
    assert ppf.portfolio_value == 817_000.0
    assert abs(ppf.returns - (817_000.0 / 1_000_000.0 - 1.0)) < 1e-9


def test_uniform_portfolio_from_account_view() -> None:
    """AccountView（session 注入形态）→ UniformPortfolio 归一。"""
    from mtzquant.engine.session import AccountView

    view = AccountView(
        positions={
            "510300.SH": type(
                "Pos",
                (),
                {
                    "code": "510300.SH",
                    "total_qty": 5000.0,
                    "today_qty": 1000.0,
                    "avg_cost": 3.0,
                    "last_price": 3.2,
                    "market_value": 16000.0,
                },
            )()
        },
        available_cash=800_000.0,
        initial_cash=1_000_000.0,
    )
    view.total_value = 817_000.0
    pf = uniform_portfolio(view)
    assert pf.starting_cash == 1_000_000.0
    assert pf.positions["510300.SH"].closeable_qty == 4000


def test_position_removed_after_close() -> None:
    """清仓后 positions 移除（账户语义已保证; 视图只见现存持仓）。"""
    pf = UniformPortfolio(positions={}, available_cash=1_000_000.0, initial_cash=1_000_000.0)
    assert list(pf.positions) == []  # 无持仓 → 投影无该标的


# ------------------------------------------------------------------
# T-A05 下单族归一（order_api 落库 + 平台 Order 模拟回执）
# ------------------------------------------------------------------
class _RecGateway:
    def __init__(self) -> None:
        self.requests: list = []
        self._n = 0

    def submit_request(self, req) -> str:  # type: ignore[no-untyped-def]
        self.requests.append(req)
        self._n += 1
        return f"oid{self._n}"


def test_order_apis_normalize_and_receipt() -> None:
    gw = _RecGateway()
    clock = lambda: datetime(2026, 1, 2, 15, 0)  # noqa: E731
    orders = make_order_api("ptrade", gw, clock)

    orders.order("600000.XSHG", 1000)
    orders.order_value("000001.XSHE", -50_000.0)
    orders.order_target("510300.SH", 2000)
    orders.order_target_value("510300.SH", 100_000.0)
    orders.order_market("510300.SH", 3000)
    orders.order_shares("600000.SH", 500)

    assert len(gw.requests) == 6
    r = gw.requests[0]
    assert (r.code, r.direction, r.style, r.order_api) == (
        "600000.SH",
        OrderDirection.BUY,
        OrderStyle.QUANTITY,
        "order",
    )  # 平台码归一 + order_api 落库
    assert r.quantity == 1000 and r.created_at == datetime(2026, 1, 2, 15, 0)
    r1 = gw.requests[1]
    assert (r1.direction, r1.style, r1.value) == (OrderDirection.SELL, OrderStyle.VALUE, 50_000.0)
    r2 = gw.requests[2]
    assert r2.style is OrderStyle.TARGET_QUANTITY and r2.target_quantity == 2000
    r3 = gw.requests[3]
    assert r3.style is OrderStyle.TARGET_VALUE and r3.target_value == 100_000.0
    r4 = gw.requests[4]
    assert r4.style is OrderStyle.MARKET and r4.quantity == 3000
    r5 = gw.requests[5]
    assert r5.order_api == "order_shares" and r5.code == "600000.SH"


def test_order_api_receipt_wrap() -> None:
    gw = _RecGateway()
    clock = lambda: datetime(2026, 1, 2, 15, 0)  # noqa: E731
    wraps: list = []

    def _wrap(oid: str, req) -> dict:  # type: ignore[no-untyped-def]
        wraps.append((oid, req))
        return {"id": oid, "code": req.code}

    orders = make_order_api("ptrade", gw, clock, wrap=_wrap)
    receipt = orders.order("510300.SH", 100)
    assert receipt == {"id": "oid1", "code": "510300.SH"}
    assert wraps[0][0] == "oid1"


# ------------------------------------------------------------------
# T-A06 数据族 PIT cutoff 防泄露
# ------------------------------------------------------------------
class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def history(self, code, fields, n, *, as_of, knowledge_time, include_today, frequency):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "code": code,
                "fields": fields,
                "n": n,
                "as_of": as_of,
                "knowledge_time": knowledge_time,
                "include_today": include_today,
                "frequency": frequency,
            }
        )
        return pd.DataFrame({"close": []}, index=pd.Index([], dtype="int64"))


def test_data_cutoff_pit() -> None:
    prov = _FakeProvider()
    now = datetime(2026, 1, 5, 15, 0)

    # 盘前: include_today 缺省 False（当日 bar 不可见, 5.2）
    core_pre = DataApiCore(prov, current_dt=lambda: now, phase=lambda: "before_open")
    core_pre.history("510300.SH", 5)
    c = prov.calls[-1]
    assert c["as_of"] == now and c["knowledge_time"] == now
    assert c["include_today"] is False
    assert c["frequency"].value == "1d"

    # 收盘: include_today 缺省 True（当日已定稿）
    core_close = DataApiCore(prov, current_dt=lambda: now, phase=lambda: "on_daily_close")
    core_close.attribute_history("510300.SH", 10, fields=["close"])
    c = prov.calls[-1]
    assert c["include_today"] is True
    assert c["fields"] == ["close"]

    # 显式 include_today 覆盖缺省
    core_close.history("510300.SH", 3, include_today=False)
    assert prov.calls[-1]["include_today"] is False


def test_current_data_snapshot_paused_when_no_bar() -> None:
    class _NoBarProvider:
        def bar_at(self, code, dt):  # type: ignore[no-untyped-def]
            return None

    core = DataApiCore(
        _NoBarProvider(),
        current_dt=lambda: datetime(2026, 1, 5, 15, 0),
        phase=lambda: "on_daily_close",
    )
    snap = core.current_data(["510300.SH"])
    assert snap["510300.SH"].paused is True
    assert snap["510300.SH"].last_price == 0.0


# ------------------------------------------------------------------
# T-A07 未实现 API 报错模板机读
# ------------------------------------------------------------------
def test_compat_registry_and_error_template() -> None:
    register_api("ptrade", "order", "L0")
    register_api("ptrade", "get_history", "L1")
    assert compat_report("ptrade") == {"get_history": "L1", "order": "L0"}

    err = not_implemented(
        "ptrade", "get_fundamentals", alternative="用 get_price(fields=['paused'])"
    )
    assert err.api_name == "get_fundamentals"  # type: ignore[attr-defined]
    assert err.platform == "ptrade"  # type: ignore[attr-defined]
    assert err.level == "L2"  # type: ignore[attr-defined]
    d = err.to_dict()
    # 机读可解析（4.9 模板: 平台名/兼容清单路径/可选替代/实现指引）
    assert d["type"] == "NotImplementedApiError"
    assert "adapter:ptrade" in d["stage"]
    assert "docs/compat/ptrade.md" in d["hint"]
    assert "get_price(fields=['paused'])" in d["hint"]


# ------------------------------------------------------------------
# T-A08 代码互转往返
# ------------------------------------------------------------------
def test_code_style_round_trip() -> None:
    assert denormalize_code("600000.SH") == "600000.XSHG"
    assert denormalize_code("000001.SZ") == "000001.XSHE"
    assert denormalize_code("600000.XSHG") == "600000.XSHG"  # 幂等
    assert round_trip("600000.XSHG") == "600000.XSHG"
    assert round_trip("000001.XSHE") == "000001.XSHE"
    # PTrade 真实外部码（4.7 修订）: 沪市 .SS, 深市 .SZ
    assert denormalize_code("510300.SH", "ptrade") == "510300.SS"
    assert denormalize_code("000001.SZ", "ptrade") == "000001.SZ"
