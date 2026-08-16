# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 13:22:00
# @update_time        : 2026/08/16 21:59:08
# @description : N6 平台版黄金桥接：joinquant 策略脚本 → DailyDriver（D3: 数据/oracle 复用）

"""聚宽平台版黄金桥接（M2-N6, 计划 D3 纪律）。

`run_joinquant_golden(driver, script)`:
- 聚宽策略脚本 exec 进适配器注入命名空间（g/log/data + 下单族 + 调度族）;
- OrderRequest → DailyDriver 下单 API; 每交易日 15:00 驱动 adapter.on_bar;
- handle_data 的 data[security] 快照由 driver 当日 bar 构造（close/volume/paused）。

数据构造与六要素 oracle 与 native 版完全同源（复用 conftest/framework）;
本桥接只替换「策略动作的表达方式」（python 闭包 → 聚宽官方写法）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from mtzquant.adapters.joinquant.adapter import JoinQuantAdapter
from mtzquant.core.codes import normalize_code
from mtzquant.engine.orders import OrderDirection, OrderRequest, OrderStyle


class _DriverGateway:
    """OrderRequest → DailyDriver 下单 API（回执绑定 driver Order, 4.5）。"""

    def __init__(self, bridge: GoldenJoinQuantBridge) -> None:
        self._bridge = bridge

    def submit_request(self, req: OrderRequest) -> str:
        driver = self._bridge.driver
        adapter = self._bridge.adapter
        receipt_seq = adapter._receipt_seq + 1
        if req.style is OrderStyle.QUANTITY or req.style is OrderStyle.MARKET:
            order = driver.order(
                req.code,
                OrderDirection.BUY if req.direction.value == "buy" else OrderDirection.SELL,
                req.quantity or 0.0,
            )
        elif req.style is OrderStyle.VALUE:
            signed = req.value if req.direction.value == "buy" else -(req.value or 0.0)
            order = driver.order_value(req.code, signed)
        elif req.style is OrderStyle.TARGET_VALUE:
            order = driver.order_target_value(req.code, req.target_value or 0.0)
        else:  # TARGET_QUANTITY
            order = driver.order_target_quantity(req.code, req.target_quantity or 0.0)
        adapter._receipt_seq = receipt_seq
        oid = f"jq{receipt_seq}"
        adapter._orders.append(req)
        receipt = adapter._make_receipt(oid, req)
        if order is not None:
            receipt["_engine_order"] = order
        return oid


class _DriverProvider:
    """DailyDriver → DataApiCore 所需 provider 接口（PIT 语义由 driver.history 承担）。"""

    def __init__(self, bridge: GoldenJoinQuantBridge) -> None:
        self._bridge = bridge

    def history(
        self,
        code: str,
        fields: list[str] | None,
        n: int,
        *,
        as_of: Any,
        knowledge_time: Any = None,
        include_today: bool = False,
        frequency: Any = None,
    ) -> Any:
        import pandas as pd

        driver = self._bridge.driver
        date = as_of.date().isoformat() if hasattr(as_of, "date") else str(as_of)
        bars = driver.history(code, n, as_of=date)
        cols = fields or ["close"]
        index = pd.DatetimeIndex([b.dt for b in bars], tz=None if not bars else bars[0].dt.tzinfo)
        data: dict[str, list[float]] = {}
        for col in cols:
            data[col] = [float(getattr(b, col, 0.0)) for b in bars]
        return pd.DataFrame(data, index=index, columns=cols)

    def bar_at(self, code: str, dt: Any) -> Any:
        date = dt.strftime("%Y-%m-%d")
        return self._bridge.driver.data.get(code, {}).get(date)


class GoldenJoinQuantBridge:
    """JoinQuantAdapter ↔ DailyDriver 桥（平台版黄金用例专用）。"""

    def __init__(self, driver: Any) -> None:
        self.driver = driver
        self.adapter = JoinQuantAdapter()
        self._now: datetime = datetime(2000, 1, 1, 15, 0)
        ctx = self.adapter._ctx
        ctx.now_fn = lambda: self._now
        ctx.current_dt = self._now
        ctx.timestamp = self._now
        ctx.universe_fn = self._universe_codes
        ctx.set_universe_fn = lambda codes: driver.set_universe([normalize_code(c) for c in codes])
        ctx.emit = lambda kind, payload: None
        ctx.provider = _DriverProvider(self)
        ctx.phase = lambda: "on_daily_close"
        self.adapter._gateway = _DriverGateway(self)

    # ------------------------------------------------------------------
    def _universe_codes(self) -> list[str]:
        uni = getattr(self.driver, "_universe", None)
        if uni:
            return list(uni)
        return list(self.driver.data.keys())

    def _account_view(self) -> Any:
        acct = self.driver.account
        positions = {}
        for code, pos in acct.positions.items():
            positions[code] = SimpleNamespace(
                code=code,
                total_qty=pos.total_qty,
                today_qty=pos.today_qty,
                avg_cost=pos.avg_cost,
                last_price=pos.last_price,
                market_value=pos.market_value,
            )
        view = SimpleNamespace(
            positions=positions,
            available_cash=acct.available_cash,
            receivable_cash=acct.receivable_cash,
            frozen_cash=acct.frozen_cash,
            initial_cash=acct.initial_cash,
        )
        view.total_cash = acct.available_cash + acct.receivable_cash + acct.frozen_cash
        view.total_value = view.total_cash + sum(p.market_value for p in positions.values())
        return view

    @staticmethod
    def _jq_sec(code: str) -> str:
        """内部码 → 聚宽外部码（XSHG/XSHE, 4.6）。"""
        norm = normalize_code(code)
        return (
            f"{norm[:-3]}.XSHG"
            if norm.endswith(".SH")
            else (f"{norm[:-3]}.XSHE" if norm.endswith(".SZ") else norm)
        )

    def _data_map(self) -> dict[str, Any]:
        """当日 data 载荷: {security: 快照}（聚宽 data[code].close 语义, 4.6）。"""
        date = self._now.strftime("%Y-%m-%d")
        out: dict[str, Any] = {}
        for code in sorted(self._universe_codes()):
            bar = self.driver.data.get(code, {}).get(date)
            sec = self._jq_sec(code)
            if bar is None:
                out[sec] = SimpleNamespace(close=0.0, volume=0.0, paused=True, code=sec)
                continue
            out[sec] = SimpleNamespace(
                code=sec,
                close=bar.close,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
                paused=bar.suspended,
                last_price=bar.close,
            )
        return out

    # ------------------------------------------------------------------
    def _current_bar_dt(self) -> datetime:
        date = getattr(self.driver, "_current_date", None)
        if date:
            session = self.driver.sessions_by_date.get(date)
            if session is not None:
                return session.bar.dt
        return self._now

    def _drive_bar(self) -> None:
        """每交易日 15:00: 刷新账户视图 → 调度任务 → handle_data（4.6 顺序）。"""
        self._now = self._current_bar_dt()
        self.adapter._ctx.account = self._account_view()
        self.adapter._refresh(self._now)  # 刷新 context.portfolio（4.4, g11 持仓可见）
        self.adapter._run_scheduled(self._now)
        if self.adapter._handle_data is not None:
            self.adapter._handle_data(self.adapter._ctx, self._data_map())

    def _drive_before(self) -> None:
        self._now = self._current_bar_dt()
        if self.adapter._before_trading is not None:
            self.adapter._ctx.account = self._account_view()
            self.adapter._refresh(self._now)
            self.adapter._before_trading(self.adapter._ctx, self._data_map())

    def _drive_after(self) -> None:
        self._now = self._current_bar_dt()
        if self.adapter._after_trading is not None:
            self.adapter._ctx.account = self._account_view()
            self.adapter._refresh(self._now)
            self.adapter._after_trading(self.adapter._ctx)


def run_joinquant_golden(driver: Any, script: str) -> Any:
    """装载聚宽策略并桥接 DailyDriver; 返回 driver.run() 快照。"""
    bridge = GoldenJoinQuantBridge(driver)
    adapter = bridge.adapter
    namespace: dict[str, Any] = dict(adapter._api_namespace())
    namespace["__name__"] = "joinquant_golden"
    exec(compile(script, "<joinquant_golden>", "exec"), namespace)  # noqa: S102
    adapter._initialize = namespace.get("initialize")
    adapter._handle_data = namespace.get("handle_data")
    adapter._before_trading = namespace.get("before_trading_start")
    adapter._after_trading = namespace.get("after_trading_end")

    sessions = driver.sessions
    if sessions:
        adapter._in_initialize = True
        try:
            if adapter._initialize is not None:
                adapter._initialize(adapter._ctx)
        finally:
            adapter._in_initialize = False
        for s in sessions:
            bridge._now = s.bar.dt
            driver.on(s.bar.date, bridge._drive_bar)
            if adapter._before_trading is not None:
                driver.on_before_open(s.bar.date, bridge._drive_before)
            if adapter._after_trading is not None:
                driver.on_after_close(s.bar.date, bridge._drive_after)
    return driver.run()
