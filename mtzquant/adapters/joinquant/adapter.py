# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 13:00:00
# @update_time        : 2026/08/16 21:59:08
# @description : N1-N5 JoinQuantAdapter：注入命名空间/数据族/下单配置族/调度族/detect（4.6）

"""JoinQuantAdapter（设计 4.6）——聚宽官方策略零改动回测。

注入: 执行策略源码前把 g/log + 全部 L0 API + data 快照对象预注入 module 命名空间;
生命周期: initialize / handle_data(context,data) / before_trading_start(context,data) /
after_trading_end(context); process_initialize 跳过并告警记降级（L2, 4.6）。

调度（对齐官方, 4.6/5.2）:
- `run_daily(func, time)`: 无 context 参数（区别于 PTrade）; 日线回测 time 映射
  bar 事件槽位——'every_bar'/'open'/'close' 每日执行, 盘中时刻折叠 15:00 并记
  semantic_degradation（strict_schedule 拒绝走 B9）;
- `run_weekly/run_monthly`: 折叠到周/月首交易日（B9 规则已有）。

数据族: data[security] 快照（close/volume/paused, 支持切片）/ history 批量 pivot /
attribute_history / get_price / get_current_data / get_index_stocks（本地成分快照, D6）/
get_all_securities（master 支撑）/ get_trade_days / get_extras（L2 报错+替代建议）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from mtzquant.adapters.joinquant import jq_query
from mtzquant.adapters.joinquant.jqdata_shim import OrderCost, PriceRelatedSlippage, install_jqdata
from mtzquant.adapters.shared.code_style import denormalize_code
from mtzquant.adapters.shared.context_factory import make_context, refresh_context
from mtzquant.adapters.shared.data_apis import DataApiCore
from mtzquant.adapters.shared.g_container import GContainer
from mtzquant.adapters.shared.log_api import make_log
from mtzquant.adapters.shared.order_apis import make_order_api
from mtzquant.adapters.shared.portfolio_view import jq_portfolio_view, uniform_portfolio
from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError, NotImplementedApiError
from mtzquant.engine.orders import OrderRequest

# 聚宽可调度时刻（日线回测: 盘中时刻折叠 15:00, 4.6 已知近似）
_JQ_TIMES = {
    "9:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "13:00",
    "13:30",
    "14:00",
    "14:30",
    "14:50",
    "15:00",
    "every_bar",
    "open",
    "close",
}


class JoinQuantAdapter:
    """聚宽平台适配器（L0 全量 + L1 子集, 4.6）。"""

    platform = "joinquant"

    def __init__(self) -> None:
        self._g = GContainer()
        self._orders: list[OrderRequest] = []
        self._receipt_seq = 0
        self._receipts: dict[str, Any] = {}  # entrust_no → 平台 Order（模拟回执）
        self._receipt_by_req: dict[int, Any] = {}
        self._daily_jobs: list[tuple[Callable[..., Any], str]] = []
        self._weekly_jobs: list[tuple[Callable[..., Any], int, str]] = []
        self._monthly_jobs: list[tuple[Callable[..., Any], int, str]] = []
        self._last_weekly_fire: tuple[int, int] | None = None  # 周折叠每周期一次
        self._last_monthly_fire: tuple[int, int] | None = None  # 月折叠每月一次
        self._in_initialize = False
        self.degradations: list[str] = []
        self.pending_initial_positions: dict[str, tuple[float, float | None]] = {}
        self._initialize: Callable[[Any], None] | None = None
        self._handle_data: Callable[[Any, Any], None] | None = None
        self._before_trading: Callable[[Any, Any], None] | None = None
        self._after_trading: Callable[[Any], None] | None = None
        self._process_initialize: Callable[[Any], None] | None = None
        self._ctx = make_context("joinquant")
        self._ctx.g = self._g
        self._gateway = _InnerGateway(self)
        self._emit: Callable[[str, dict[str, Any]], None] | None = None
        self._master_frame: pd.DataFrame | None = None  # M3 主数据缓存
        self._names: dict[str, str] | None = None  # M3 code→name 缓存

    # ------------------------------------------------------------------
    # StrategyAdapter 协议
    # ------------------------------------------------------------------
    def load(self, strategy_path: Path, context: Any = None) -> None:
        """加载策略源码: 预注入 g/log/API/data → exec → 解析生命周期入口（4.6）。

        M3: exec 前安装 `jqdata` 兼容模块（sys.modules["jqdata"]）, 使策略顶部的
        `from jqdata import *` 直接生效且与注入命名空间一致。
        """
        code = Path(strategy_path).read_text(encoding="utf-8")
        namespace: dict[str, Any] = dict(self._api_namespace())
        namespace["__name__"] = "joinquant_strategy"
        install_jqdata(namespace)
        exec(compile(code, str(strategy_path), "exec"), namespace)  # noqa: S102
        self._initialize = namespace.get("initialize")
        self._handle_data = namespace.get("handle_data")
        self._before_trading = namespace.get("before_trading_start")
        self._after_trading = namespace.get("after_trading_end")
        self._process_initialize = namespace.get("process_initialize")
        if self._initialize is None:
            raise MtzQuantError("聚宽策略必须定义 initialize(context)", stage="adapter:joinquant")

    def setup(self, account_view: Any = None) -> None:
        self._ctx.account = account_view

    def on_before_trading(self, ev: Any = None) -> None:
        """盘前回调（before_trading_start; 当日 bar 不可见, 5.2）。"""
        if self._before_trading is not None:
            self._refresh(_self_now(self._ctx))
            self._before_trading(self._ctx, self._jq_data(include_today=False))

    def on_bar(self, ev: Any = None) -> None:
        """主驱动（15:00）: 刷新 context → 调度任务 → handle_data（4.6 顺序）。"""
        dt = _self_now(self._ctx)
        self._refresh(dt)
        self._run_scheduled(dt)
        if self._handle_data is not None:
            self._handle_data(self._ctx, self._jq_data(include_today=True))

    def on_after_trading(self, ev: Any = None) -> None:
        if self._after_trading is not None:
            self._refresh(_self_now(self._ctx))
            self._after_trading(self._ctx)

    def take_orders(self) -> list[OrderRequest]:
        out, self._orders = self._orders, []
        return out

    def sync_orders(self, pairs: list[tuple[OrderRequest, Any]]) -> None:
        """回执 ↔ 引擎订单对齐（id(req) 匹配; 同 bar 撤单在绑定后执行, 5.3.1）。"""
        for req, order in pairs:
            receipt = self._receipt_by_req.get(id(req))
            if receipt is None or order is None:
                continue
            receipt["_engine_order"] = order

    def finalize(self) -> None:
        self._in_initialize = False

    # ------------------------------------------------------------------
    # initialize 驱动（session 构造期; ctx=session 注入面=本适配器 _ctx）
    # ------------------------------------------------------------------
    def run_initialize(self, ctx: Any) -> None:
        emit = getattr(ctx, "emit", None)
        if emit is not None:
            self._emit = emit
        task = getattr(ctx, "task", None)
        if task is not None:
            self._ctx.initial_capital = float(task.backtest.initial_capital)
        if self._process_initialize is not None:
            # process_initialize 是聚宽 L2（回测语义占位）——记降级不执行, 4.6
            self._note_degradation(
                "process_initialize 为聚宽 L2 API（回测语义占位）, 已跳过——"
                "初始化逻辑请放 initialize"
            )
        self._in_initialize = True
        try:
            if self._initialize is not None:
                self._initialize(self._ctx)
        finally:
            self._in_initialize = False

    # ------------------------------------------------------------------
    # 内部装配
    # ------------------------------------------------------------------
    def _emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self._emit is not None:
            self._emit(kind, payload)

    def _note_degradation(self, note: str) -> None:
        self.degradations.append(note)
        self._emit_event("log", {"kind": "semantic_degradation", "message": note})

    def _refresh(self, dt: datetime) -> None:
        account = getattr(self._ctx, "account", None)
        cal = getattr(self._ctx, "calendar", None)
        previous = cal.before(dt.date()) if cal is not None else None
        universe_fn = getattr(self._ctx, "universe_fn", None)
        universe = list(universe_fn()) if universe_fn is not None else []
        pf = uniform_portfolio(account) if account is not None else None
        view = jq_portfolio_view(pf) if pf is not None else None
        if view is not None:
            # 聚宽同步持仓语义: 视图 positions 支持迭代中增删（挂单即时入列/卖出即时移除）
            view.positions = _ViewPositions(view.positions)
        refresh_context(
            self._ctx,
            current_dt=dt,
            previous_date=previous if previous is not None else dt.date(),
            universe=universe,
            portfolio=view,
        )

    def _phase(self) -> str:
        phase = getattr(self._ctx, "phase", None)
        return phase() if callable(phase) else "on_daily_close"

    def _data_core(self) -> DataApiCore:
        provider = getattr(self._ctx, "provider", None)
        if provider is None:
            raise MtzQuantError(
                "数据 API 需要引擎装配（provider 未注入）", stage="adapter:joinquant"
            )
        return DataApiCore(
            provider,
            current_dt=lambda: getattr(self._ctx, "current_dt", None),
            phase=self._phase,
        )

    def _jq_data(self, *, include_today: bool, codes: list[str] | None = None) -> dict[str, Any]:
        """handle_data 的 data 载荷: {security: 快照}（聚宽 data[code] 语义, 4.6）。

        M3 增补: pre_close、high_limit/low_limit（由 pre_close×板块因子计算）、
        name（主数据）与 is_st（名称判定）; `codes` 限定构造范围——get_current_data
        局部请求时只建请求标的, 避免大池全 universe 快照（热路径, 5442×1604 天）。
        """
        if codes is None:
            universe_fn = getattr(self._ctx, "universe_fn", None)
            universe = list(universe_fn()) if universe_fn is not None else []
        else:
            universe = codes
        dt = _self_now(self._ctx)
        provider = getattr(self._ctx, "provider", None)
        names = self._name_map()
        out: dict[str, Any] = {}
        for code in sorted(universe):
            sec = denormalize_code(code)
            name = names.get(code, "")
            is_st = bool(name and ("ST" in name or "*" in name or "退" in name))
            if not include_today or provider is None:
                out[sec] = _jq_snapshot(sec, dt, paused=True, name=name, is_st=is_st)
                continue
            bar = provider.bar_at(code, dt)
            if bar is None:
                out[sec] = _jq_snapshot(sec, dt, paused=True, name=name, is_st=is_st)
                continue
            up_arr, dn_arr = self._limit_prices_series(code, bar.pre_close)
            up = float(up_arr[-1]) if up_arr.size and up_arr[-1] == up_arr[-1] else 0.0
            dn = float(dn_arr[-1]) if dn_arr.size and dn_arr[-1] == dn_arr[-1] else 0.0
            out[sec] = _jq_snapshot(
                sec,
                dt,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                pre_close=bar.pre_close,
                high_limit=up,
                low_limit=dn,
                paused=bool(bar.suspended),
                name=name,
                is_st=is_st,
            )
        return out

    # ------------------------------------------------------------------
    # N2 数据族 + N3 下单配置族 + N4 调度族（module.__dict__ 注入）
    # ------------------------------------------------------------------
    def _api_namespace(self) -> dict[str, Any]:
        ns: dict[str, Any] = {
            "g": self._g,
            "log": make_log(lambda k, p: self._emit_event(k, p)),
            "data": self._data_view,  # data[security] 快照（策略内动态取）
        }
        # 下单族（K5 归一; 聚宽签名 order(security, amount), amount 买正卖负）
        order_ns = make_order_api(
            "joinquant",
            self._gateway,
            lambda: _self_now(self._ctx),
            wrap=self._make_receipt,
        )
        for key, value in vars(order_ns).items():
            if callable(value):
                ns[key] = value
        ns["get_trades"] = self.get_trades
        # 数据族（4.6）
        ns["history"] = self.history
        ns["attribute_history"] = self.attribute_history
        ns["get_price"] = self.get_price
        ns["get_current_data"] = self.get_current_data
        ns["get_index_stocks"] = self.get_index_stocks
        ns["get_all_securities"] = self.get_all_securities
        ns["get_trade_days"] = self.get_trade_days
        ns["get_extras"] = self.get_extras
        # M3 基本面族（query DSL, 4.6 附录C）: query/valuation/indicator/finance/get_fundamentals
        ns["query"] = jq_query.query
        ns["valuation"] = jq_query.valuation
        ns["indicator"] = jq_query.indicator
        ns["finance"] = SimpleNamespace(
            STK_XR_XD=jq_query.finance.STK_XR_XD, run_query=self.run_query
        )
        ns["get_fundamentals"] = self.get_fundamentals
        # 配置族（4.6）
        ns["set_universe"] = self.set_universe
        ns["set_benchmark"] = self.set_benchmark
        ns["set_order_cost"] = self.set_order_cost
        ns["set_slippage"] = self.set_slippage
        ns["set_commission"] = self.set_commission
        ns["set_option"] = self.set_option
        # 调度族（4.6）
        ns["run_daily"] = self.run_daily
        ns["run_weekly"] = self.run_weekly
        ns["run_monthly"] = self.run_monthly
        return ns

    def _data_view(self, security: str) -> Any:
        """data[security]: 当日快照（handle_data 外访问, 4.6）。"""
        return self._jq_data(include_today=True).get(denormalize_code(security))

    def _make_receipt(self, order_id: str, req: OrderRequest) -> dict[str, Any]:
        """wrap 工厂: 聚宽 Order 模拟回执（dict, 官方 Order 对象字段子集, 4.6）。"""
        style = req.style.value
        if style in ("quantity", "market"):
            raw = req.quantity or 0.0
        elif style == "target_quantity":
            raw = req.target_quantity or 0.0
        elif style == "value":
            raw = req.value or 0.0
        else:
            raw = req.target_value or 0.0
        signed = raw if req.direction.value == "buy" else -raw
        receipt: dict[str, Any] = {
            "order_id": order_id,
            "security": denormalize_code(req.code),
            "amount": signed,
            "is_buy": req.direction.value == "buy",
            "entrust_no": order_id,
            "status": "open",  # 聚宽 Order.is_filled/is_buy 等, 绑定后转引擎状态
            "_engine_order": None,
        }
        self._receipts[order_id] = receipt
        self._receipt_by_req[id(req)] = receipt
        # 聚宽同步建仓语义: 挂单即时计入策略持仓视图——买入加列/卖出移除并回款,
        # 使 `len(context.portfolio.positions)` 与 available_cash 在下单循环内实时变化
        # （等分现金策略靠它 break/算槽位; 次日视图重建自愈）。M3-N6。
        # 注意 `order_target(s, 0)` 的 direction 为 buy（0>=0）, 须按 style/target 判定清仓卖。
        style = req.style.value
        target_zero = (style == "target_quantity" and (req.target_quantity or 0) == 0) or (
            style == "target_value" and (req.target_value or 0) == 0
        )
        if req.direction.value == "sell" or target_zero:
            self._book_pending_sell(req.code)
        else:
            self._book_pending_buy(req.code)
        return receipt

    def _book_pending_buy(self, code: str) -> None:
        """把买入挂单计入当前组合视图（仅视图层; 真实账户仍由引擎撮合侧记账）。"""
        portfolio = getattr(self._ctx, "portfolio", None)
        positions = getattr(portfolio, "positions", None)
        if positions is None:
            return
        sec = denormalize_code(code)
        if sec in positions:
            return
        positions[sec] = SimpleNamespace(
            security=sec,
            amount=0.0,  # 挂单量未知（次日成交才定）, 视图仅需 code 在列（len/in 判断）
            total_amount=0.0,
            closeable_amount=0.0,
            avg_cost=0.0,
            price=0.0,
            value=0.0,
            sid=sec,
            last_price=0.0,
        )

    def _book_pending_sell(self, code: str) -> None:
        """卖出挂单即时移除视图持仓, 并把预估回款计入视图 available_cash（聚宽同步语义）。"""
        portfolio = getattr(self._ctx, "portfolio", None)
        positions = getattr(portfolio, "positions", None)
        if portfolio is None or positions is None:
            return
        sec = denormalize_code(code)
        pos = positions.pop(sec, None)
        if pos is not None:
            try:
                proceeds = float(getattr(pos, "value", 0.0))
                portfolio.available_cash = float(portfolio.available_cash) + proceeds
            except (TypeError, ValueError):
                pass

    # ------------------------------------------------------------------
    # 调度族（4.6）
    # ------------------------------------------------------------------
    def run_daily(self, func: Callable[..., Any], time: str = "every_bar", **kw: Any) -> None:
        if not self._in_initialize:
            raise MtzQuantError(
                "run_daily 仅可在 initialize 中注册（聚宽官方语义）", stage="adapter:joinquant"
            )
        if kw.get("reference_security") is not None:
            self._note_degradation(
                "run_daily(reference_security=...) 为聚宽 L2 调度参考, 日线回测忽略"
            )
        t = str(time)
        if t not in _JQ_TIMES:
            self._note_degradation(f"run_daily time={t!r} 非聚宽标准时刻, 折叠 15:00 执行")
        elif t not in ("every_bar", "open", "close", "15:00"):
            self._note_degradation(f"run_daily time={t!r} 日线回测折叠 15:00 执行")
        self._daily_jobs.append((func, t))

    def run_weekly(self, func: Callable[..., Any], weekday: int = 1, time: str = "open") -> None:
        """周调度: 折叠到每周首个交易日（B9 规则已有, 4.6 已知近似）。"""
        if not self._in_initialize:
            raise MtzQuantError("run_weekly 仅可在 initialize 中注册", stage="adapter:joinquant")
        self._weekly_jobs.append((func, weekday, str(time)))
        self._note_degradation(f"run_weekly(weekday={weekday}) 折叠到每周首交易日")

    def run_monthly(self, func: Callable[..., Any], monthday: int = 1, time: str = "open") -> None:
        """月调度: 折叠到每月首个交易日（4.6 已知近似）。"""
        if not self._in_initialize:
            raise MtzQuantError("run_monthly 仅可在 initialize 中注册", stage="adapter:joinquant")
        self._monthly_jobs.append((func, monthday, str(time)))
        self._note_degradation(f"run_monthly(monthday={monthday}) 折叠到每月首交易日")

    def _run_scheduled(self, dt: datetime) -> None:
        """每日执行已调度任务（run_daily 折叠后每日; run_weekly/monthly 每周期一次）。

        周/月折叠只在本周期**首个触发日**执行一次（`_last_*_fire` 周期键守卫）——
        否则 `dt.day<=5` 的近似会在月初 5 日内每日触发, 导致月度策略每月多次交易。
        """
        for func, _t in list(self._daily_jobs):
            func(self._ctx)
        week_key = (dt.isocalendar().year, dt.isocalendar().week)
        if week_key != self._last_weekly_fire:
            fired = False
            for func, weekday, _t in list(self._weekly_jobs):
                if dt.weekday() == (weekday - 1) % 7 or self._is_first_trade_weekday(dt):
                    func(self._ctx)
                    fired = True
            if fired:
                self._last_weekly_fire = week_key
        month_key = (dt.year, dt.month)
        if month_key != self._last_monthly_fire:
            fired = False
            for func, monthday, _t in list(self._monthly_jobs):
                if dt.day == monthday or self._is_first_trade_monthday(dt):
                    func(self._ctx)
                    fired = True
            if fired:
                self._last_monthly_fire = month_key

    @staticmethod
    def _is_first_trade_weekday(dt: datetime) -> bool:
        return dt.day <= 7 and dt.weekday() == 0  # 简近似: 每周首交易日≈周一且周内前段

    @staticmethod
    def _is_first_trade_monthday(dt: datetime) -> bool:
        return dt.day <= 5  # 简近似: 每月首交易日≈月初前 5 日首根

    # ------------------------------------------------------------------
    # 配置族（4.6）
    # ------------------------------------------------------------------
    def set_universe(self, universe: list[str] | str) -> None:
        codes = [universe] if isinstance(universe, str) else list(universe)
        fn = getattr(self._ctx, "set_universe_fn", None)
        if fn is not None:
            fn(codes)
        self._ctx.universe = [normalize_code(c) for c in codes]

    def set_benchmark(self, code: str) -> None:
        self._note_degradation(f"set_benchmark({code!r}) 运行时设置不生效（基准取任务配置）")

    def set_order_cost(
        self,
        order_cost: OrderCost | None = None,
        open_tax: float = 0.0,
        close_tax: float = 0.001,
        open_commission: float = 0.0003,
        close_commission: float = 0.0003,
        min_commission: float = 5.0,
        **kw: Any,
    ) -> None:
        """聚宽 set_order_cost: 接受 `OrderCost` 对象或关键字参数（佣金/印花, 4.6 已知近似）。"""
        if isinstance(order_cost, OrderCost):
            # 仅取撮合侧用到的字段（open_tax/close_commission 为聚宽口径, 本引擎按买卖侧统一）
            close_tax = order_cost.close_tax
            open_commission = order_cost.open_commission
            min_commission = order_cost.min_commission
        fn = getattr(self._ctx, "set_fees_fn", None)
        if fn is not None:
            fn(
                commission_rate=open_commission,
                min_commission=min_commission,
                stamp_tax_rate=close_tax,
                transfer_fee_rate=0.0,
            )

    def set_slippage(self, value: float | PriceRelatedSlippage) -> None:
        """聚宽 set_slippage: 接受 `PriceRelatedSlippage(value)` 或浮点比值。"""
        if isinstance(value, PriceRelatedSlippage):
            value = value.value
        fn = getattr(self._ctx, "set_slippage_fn", None)
        if fn is not None:
            fn(ratio=float(value))

    def set_commission(self, commission_ratio: float = 0.0003, min_commission: float = 5.0) -> None:
        """聚宽 set_commission（仅佣金, 4.6 变体）。"""
        fn = getattr(self._ctx, "set_fees_fn", None)
        if fn is not None:
            fn(commission_rate=commission_ratio, min_commission=min_commission)

    def set_option(self, key: str, value: Any) -> None:
        """聚宽 set_option（L2 子集: 能映射则映射, 否则结构化报错, 4.9）。"""
        if key in (
            "auto_handle_position",
            "use_real_price",
            "order_volume_ratio",
            "avoid_future_data",
        ):
            note = (
                "为聚宽 L2, 回测近似忽略（PIT 已保证无未来函数）"
                if key == "avoid_future_data"
                else "为聚宽 L2, 回测近似忽略"
            )
            self._note_degradation(f"set_option({key!r}) {note}")
            return
        raise NotImplementedApiError(
            f"set_option({key!r})",
            self.platform,
            level="L2",
            alternative=(
                "聚宽 set_option 仅支持 auto_handle_position/use_real_price/order_volume_ratio"
            ),
        )

    # ------------------------------------------------------------------
    # 数据族（4.6 / 3.13）
    # ------------------------------------------------------------------
    def history(
        self,
        count: int,
        unit: str = "1d",
        field: str = "close",
        security_list: list[str] | None = None,
        df: bool = True,
        skip_paused: bool = True,
        include_now: bool = False,
        fq: str = "pre",
    ) -> Any:
        """聚宽 history: 批量 pivot 宽表（多标的多字段, 4.6）。"""
        if unit == "1m":
            self._note_degradation("history(unit='1m') 日线回测折叠为 1d（M3 已知近似）")
            unit = "1d"
        core = self._data_core()
        universe_fn = getattr(self._ctx, "universe_fn", None)
        codes = (
            [normalize_code(c) for c in security_list]
            if security_list
            else list(universe_fn() if universe_fn is not None else [])
        )
        frames = [
            core.history(c, count, unit=unit, fields=[field], include_today=include_now)
            for c in codes
        ]
        if len(codes) <= 1:
            frame = frames[0] if frames else pd.DataFrame()
            if not df and field in frame:
                return frame[field].to_numpy()
            if len(codes) == 1 and field in frame.columns:
                frame = frame.rename(columns={field: denormalize_code(codes[0])})
            return _PosFrame(frame.reset_index(drop=True))
        # 平台码统一: 列=聚宽码（与 get_all_securities/get_current_data/positions 一致）
        out = pd.concat(
            [f[field].rename(denormalize_code(c)) for f, c in zip(frames, codes, strict=True)],
            axis=1,
        )
        return _PosFrame(out.reset_index(drop=True))  # 兼容 `df[stock][-1]` 位置访问

    def attribute_history(
        self,
        security: str,
        count: int,
        unit: str = "1d",
        fields: list[str] | None = None,
        skip_paused: bool = True,
        df: bool = True,
        include_today: bool = False,
        fq: str = "pre",
    ) -> Any:
        return self._data_core().attribute_history(
            normalize_code(security), count, unit=unit, fields=fields, include_today=include_today
        )

    _VIRTUAL_FIELDS = {"high_limit", "low_limit", "limit_up", "limit_down"}

    def get_price(
        self,
        security: str | list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        frequency: str = "1d",
        fields: list[str] | None = None,
        skip_paused: bool = True,
        fq: str = "pre",
        count: int | None = None,
        panel: bool = True,
        include_now: bool = True,
    ) -> Any:
        """聚宽 get_price: 单/多标的, panel True/False, 虚拟字段 high_limit/low_limit（M3）。

        - 多标的 panel=True → {field: DataFrame(dt × codes)};
        - 多标的 panel=False → 每标的一行（末根）, 列 = [code] + fields
          （策略 get_zt_stock_list 用法）;
        - 单标的 → DataFrame(dt × fields)。
        涨跌停价（high_limit/low_limit）为虚拟字段: 本地 bar 无 limit 列,
          由 pre_close×板块因子计算。
        """
        is_list = isinstance(security, (list, tuple))
        codes = [normalize_code(c) for c in (security if is_list else [security])]  # type: ignore[arg-type]
        flds = list(fields) if fields else ["open", "close", "high", "low", "volume", "money"]
        want_limits = any(f in self._VIRTUAL_FIELDS for f in flds)
        base_fields = [f for f in flds if f not in self._VIRTUAL_FIELDS]
        if want_limits:
            base_fields = list(dict.fromkeys([*base_fields, "pre_close"]))
        if not codes:
            # 空列表输入: 返回带预期列的窄表（对齐聚宽 get_price([]) 语义）
            if is_list and not security:
                if panel:
                    return {f: pd.DataFrame() for f in flds}
                return pd.DataFrame(columns=["code"] + [f for f in flds])
            return pd.DataFrame()
        core = self._data_core()
        frames: list[pd.DataFrame | None] = []
        for c in codes:
            fr = core.get_price(
                c,
                start_date=_to_date(start_date),
                end_date=_to_date(end_date),
                count=count,
                unit="1d",
                fields=base_fields,
            )
            if want_limits and fr is not None and not fr.empty and "pre_close" in fr.columns:
                up, dn = self._limit_prices_series(c, fr["pre_close"])
                fr = fr.copy()
                fr["high_limit"] = up
                fr["low_limit"] = dn
            frames.append(fr)
        if not is_list:
            # 单标的（security 为字符串）: DataFrame(dt × fields)
            frame = frames[0]
            if frame is None or frame.empty:
                return frame
            keep = [f for f in flds if f in frame.columns]
            return frame[keep]
        if panel:
            out: dict[str, pd.DataFrame] = {}
            for f in flds:
                cols = {}
                for c, fr in zip(codes, frames, strict=True):
                    if fr is not None and f in fr.columns:
                        cols[denormalize_code(c)] = fr[f]  # 平台码统一
                if cols:
                    out[f] = pd.DataFrame(cols)
            return out
        # panel=False 多标的: 每标的一行（末根）, 列 = [code] + fields（平台码统一）
        rows: list[dict[str, Any]] = []
        for c, fr in zip(codes, frames, strict=True):
            if fr is None or fr.empty:
                continue
            last = fr.iloc[-1]
            row: dict[str, Any] = {"code": denormalize_code(c)}
            for f in flds:
                row[f] = last.get(f) if f in last.index else None
            rows.append(row)
        return pd.DataFrame(rows)

    def get_current_data(self, security_list: list[str] | None = None) -> dict[str, Any]:
        """get_current_data: {security: CurrentData 快照}（4.6）。

        M3: 返回 `_CurrentDataDict`——对**非 universe** 标的（get_all_securities 全市场但本地
        无行情）自动补暂停快照（paused=True）, 供 filter_st/paused 等安全索引, 对齐聚宽语义
        （无数据标的视为停牌, 不参与选股）。
        """
        universe_fn = getattr(self._ctx, "universe_fn", None)
        if security_list:
            codes = [normalize_code(c) for c in security_list]
        else:
            codes = list(universe_fn()) if universe_fn is not None else []
        # 只构造请求标的的快照（大池下 O(请求) 而非 O(universe), 热路径）
        snap = self._jq_data(include_today=True, codes=codes)
        base = {denormalize_code(c): snap.get(denormalize_code(c)) for c in codes}
        return _CurrentDataDict(self, base, now=_self_now(self._ctx))

    def get_index_stocks(self, index_symbol: str) -> list[str]:
        """成分股: 读本地成分快照（3.12-⑤, D6）; 缺失结构化报错不返回当前成分。"""
        code = normalize_code(index_symbol)
        root = getattr(self._ctx, "constituents_dir", None)
        if root is None:
            raise NotImplementedApiError(
                f"get_index_stocks({index_symbol!r})",
                self.platform,
                level="L1",
                alternative="成分快照下载器归 M3; 可先用 mtzquant fetch --master 或手动放置",
            )
        path = Path(str(root)) / f"{code}.csv"
        if not path.is_file():
            raise NotImplementedApiError(
                f"get_index_stocks({index_symbol!r})",
                self.platform,
                level="L1",
                alternative=(
                    f"成分快照缺失: {path}; 下载途径见 docs/数据源开发指南.md"
                    "（防幸存者偏差: 不返回当前成分, 3.13）"
                ),
            )
        import csv

        out: list[str] = []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                raw = row.get("code") or row.get("symbol") or row.get("ts_code") or ""
                if raw:
                    out.append(normalize_code(raw))
        return out

    def get_all_securities(
        self, types: list[str] | str | None = None, date: str | None = None
    ) -> Any:
        """全部证券（M3: 读主数据 PIT 过滤 list_date<=date 且未退市, 4.6/3.11）。

        返回 DataFrame（index=归一 code, 列 code/display_name/name/start_date/end_date）;
        date=None → 最近交易日（previous_date）。主数据来自 tushare stock_basic L+D。
        """
        want = set(types) if isinstance(types, (list, tuple)) else ({types} if types else {"stock"})
        asof = _to_date(date) or self._previous_date()
        df = self._master_df()
        out: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            if r["instrument_type"] not in want:
                continue
            listed = _parse_yyyymmdd(r.get("list_date"))
            delisted = _parse_yyyymmdd(r.get("delist_date"))
            if listed is not None and listed > asof:
                continue
            if delisted is not None and delisted <= asof:
                continue
            # 平台码统一: 输出聚宽码（600000.XSHG）, 与 get_current_data/positions/history 一致
            code = denormalize_code(normalize_code(r["code"]))
            name = r.get("name") or ""
            out.append(
                {
                    "code": code,
                    "display_name": name or code,
                    "name": name,
                    "start_date": r.get("list_date") or "",
                    "end_date": r.get("delist_date") or "",
                }
            )
        if not out:
            return pd.DataFrame(columns=["code", "display_name", "name", "start_date", "end_date"])
        return pd.DataFrame(out).set_index("code")

    # ------------------------------------------------------------------
    # M3 基本面族（query DSL, 4.6 附录C / 3.13）
    # ------------------------------------------------------------------
    def get_fundamentals(self, query_obj: Any, date: str | date | None = None) -> Any:
        """聚宽 get_fundamentals(query, date=None): 求值 query → DataFrame（PIT as_of=date）。"""
        asof = _to_date(date) or self._previous_date()
        try:
            return jq_query.eval_query(self._ctx, query_obj, asof)
        finally:
            self._maybe_trim_fund_cache()

    def run_query(self, query_obj: Any) -> Any:
        """聚宽 finance.run_query(query): 求值 finance 表查询（如 STK_XR_XD）, as_of=最近交易日。"""
        try:
            return jq_query.eval_query(self._ctx, query_obj, self._previous_date())
        finally:
            self._maybe_trim_fund_cache()

    def _maybe_trim_fund_cache(self) -> None:
        """基本面读取缓存上限（约 1500 文件 ≈ 1.5-2GB）: 超限清空, 防大池全载 OOM。"""
        provider = getattr(self._ctx, "provider", None)
        store = getattr(provider, "_fundamentals", None)
        cache = getattr(store, "_cache", None)
        if cache is not None and len(cache) > 20000:
            cache.clear()

    # ------------------------------------------------------------------
    # M3 辅助: 主数据 / 名称 / 涨跌停价
    # ------------------------------------------------------------------
    def _previous_date(self) -> date:
        prev = getattr(self._ctx, "previous_date", None)
        if prev is not None:
            return prev if isinstance(prev, date) else date.fromisoformat(str(prev))
        return date.today()

    def _master_path(self) -> Path:
        return Path(str(getattr(self._ctx, "master_path", None) or "data/master/instruments.csv"))

    def _master_df(self) -> pd.DataFrame:
        """主数据 DataFrame（缓存）: code/name/instrument_type/list_date/delist_date。"""
        if self._master_frame is None:
            self._master_frame = pd.DataFrame()
            try:
                df = pd.read_csv(self._master_path(), dtype=str, keep_default_na=False)
                for c in ("code", "name", "instrument_type", "list_date", "delist_date"):
                    if c not in df.columns:
                        df[c] = ""
                self._master_frame = df[
                    ["code", "name", "instrument_type", "list_date", "delist_date"]
                ]
            except OSError:
                self._master_frame = pd.DataFrame(
                    columns=["code", "name", "instrument_type", "list_date", "delist_date"]
                )
        return self._master_frame

    def _name_map(self) -> dict[str, str]:
        if self._names is None:
            try:
                df = self._master_df()
                self._names = dict(
                    zip((normalize_code(c) for c in df["code"]), df["name"], strict=True)
                )
            except Exception:  # noqa: BLE001
                self._names = {}
        return self._names

    def _limit_factor(self, code: str) -> float:
        """A股涨跌停幅度: ST 5% / 创业+科创 20% / 北交所 30% / 主板 10%。"""
        body = code.split(".")[0]
        name = self._name_map().get(code, "")
        if "ST" in name or "*" in name or "退" in name:
            return 0.05
        if body.startswith("300") or body.startswith("301") or body.startswith("688"):
            return 0.20
        if body.startswith("4") or body.startswith("8") or body.startswith("920"):
            return 0.30
        return 0.10

    def _limit_prices_series(self, code: str, pre_close: Any) -> tuple[Any, Any]:
        """按 pre_close 算 high_limit/low_limit（四舍五入到分; 返回 1-d 数组, 标量也兼容）。"""
        import numpy as np

        pc = np.atleast_1d(np.asarray(pre_close, dtype="float64"))
        factor = self._limit_factor(code)
        up = np.where(np.isfinite(pc) & (pc > 0), np.round(pc * (1 + factor), 2), np.nan)
        dn = np.where(np.isfinite(pc) & (pc > 0), np.round(pc * (1 - factor), 2), np.nan)
        return up, dn

    def get_trades(self) -> list[Any]:
        fills = getattr(self._ctx, "fills", None)
        return list(fills) if fills is not None else []

    def get_trade_days(
        self, start_date: str | date | None = None, end_date: str | date | None = None
    ) -> list[date]:
        cal = getattr(self._ctx, "calendar", None)
        if cal is None:
            return []
        first, last = cal.first_day, cal.last_day
        if first is None or last is None:
            return []
        return cal.trading_days(_to_date(start_date) or first, _to_date(end_date) or last)

    def get_extras(
        self, info: str, security_list: list[str], start_date: str, end_date: str, df: bool = True
    ) -> Any:
        """L2 报错 + 替代建议（停牌等非 K 线数据, 4.6/4.9）。"""
        raise NotImplementedApiError(
            f"get_extras({info!r})",
            self.platform,
            level="L2",
            alternative="停牌标记可用 get_price(fields=['paused']); 完整基本面归 M3",
        )


class _ViewPositions(dict[str, Any]):
    """聚宽持仓视图字典: 迭代/keys/items/values 返回快照——策略循环内下卖单即时移除
    持仓时不触发 "dictionary changed size during iteration"（同步建仓语义, M3-N6）。
    `len()` 仍实时反映移除后的持仓数（等分现金策略算槽位用）。"""

    def __iter__(self) -> Any:
        return iter(list(super().__iter__()))

    def keys(self) -> Any:  # type: ignore[override]
        return list(super().keys())

    def items(self) -> Any:  # type: ignore[override]
        return list(super().items())

    def values(self) -> Any:  # type: ignore[override]
        return list(super().values())


class _PosSeries(pd.Series):
    """支持旧式 `s[-1]` 位置访问（聚宽 history 的 `df[stock][-1]` 写法,
    现代 pandas 标签语义不兼容）。"""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return super().iloc[key]
        return super().__getitem__(key)


class _PosFrame(pd.DataFrame):
    """history 返回帧: 列访问（str）返回 _PosSeries, 兼容 `df[stock][-1]`。"""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return _PosSeries(super().__getitem__(key))
        return super().__getitem__(key)


class _CurrentDataDict(dict[str, Any]):
    """get_current_data 返回: 缺失标的自动补暂停快照（无行情→视为停牌, M3）。"""

    def __init__(self, adapter: JoinQuantAdapter, snapshots: dict[str, Any], now: datetime) -> None:
        super().__init__(snapshots)
        self._adapter = adapter
        self._now = now

    def __missing__(self, key: str) -> Any:
        code = normalize_code(key)
        sec = denormalize_code(code)
        name = self._adapter._name_map().get(code, "")
        is_st = bool(name and ("ST" in name or "*" in name or "退" in name))
        snap = _jq_snapshot(sec, self._now, paused=True, name=name, is_st=is_st)
        self[key] = snap
        return snap


class _InnerGateway:
    def __init__(self, adapter: JoinQuantAdapter) -> None:
        self._adapter = adapter

    def submit_request(self, req: OrderRequest) -> str:
        self._adapter._receipt_seq += 1
        oid = f"jq{self._adapter._receipt_seq}"
        self._adapter._orders.append(req)
        return oid


def _jq_snapshot(
    security: str,
    dt: datetime,
    *,
    open: float = 0.0,
    high: float = 0.0,
    low: float = 0.0,
    close: float = 0.0,
    volume: float = 0.0,
    pre_close: float = 0.0,
    high_limit: float = 0.0,
    low_limit: float = 0.0,
    name: str = "",
    is_st: bool = False,
    paused: bool = False,
) -> SimpleNamespace:
    """聚宽 data[security] / CurrentData 快照（字段子集, 4.6;
    M3 增补 pre_close/涨跌停/名称/ST）。"""
    return SimpleNamespace(
        code=security,
        security=security,
        day=dt.date(),
        open=open,
        high=high,
        low=low,
        close=close,
        price=close,
        last_price=close,
        pre_close=pre_close,
        volume=volume,
        paused=paused,
        high_limit=high_limit,
        low_limit=low_limit,
        name=name,
        is_st=is_st,
    )


def _self_now(ctx: Any) -> datetime:
    now_fn = getattr(ctx, "now_fn", None)
    if now_fn is not None:
        value = now_fn()
        if isinstance(value, datetime):
            return value
    now = getattr(ctx, "current_dt", None)
    if callable(now):
        now = now()
    if isinstance(now, datetime):
        return now
    ts = getattr(ctx, "timestamp", None)
    return ts if isinstance(ts, datetime) else datetime(2000, 1, 1, 15, 0)


def _to_date(d: str | date | None) -> date | None:
    if d is None:
        return None
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def _parse_yyyymmdd(value: Any) -> date | None:
    """YYYYMMDD/YYYY-MM-DD/空 → date 或 None（主数据 list/delist 日期）。"""
    if value is None:
        return None
    text = str(value).strip().split(".")[0]  # 去 CSV 浮点尾 ".0"
    if not text or text.lower() in ("nan", "none"):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def _register() -> None:
    from mtzquant.adapters.base import register_adapter

    register_adapter("joinquant", JoinQuantAdapter)


_register()
