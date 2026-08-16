# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 11:00:00
# @update_time        : 2026/08/16 15:32:24
# @description : L4-L7 PTradeAdapter：L0 API 注入 + 设置族 + run_daily 调度 + 注册（4.7）

"""PTradeAdapter（设计 4.7 / 附录C）——PTrade 官方策略零改动回测。

注入方式: 执行策略源码前把 g/log + 全部 L0 API 预注入 module 命名空间
（官方「全局函数」语义; context 经 initialize(context) 参数传入）。

调度语义（对齐官方而非猜测, 4.7/5.2）:
- `run_daily(context, func, time)`: 仅 initialize 内可注册（他处结构化报错）;
  日线回测 time 统一折叠 15:00 执行并记 semantic_degradation（strict 拒绝走 B9）;
- `run_interval`: 官方仅实盘, 回测中结构化报错;
- `handle_data(context, data)`: 主入口, 每 bar（日线=每日 15:00 收盘后, data 含当日）。

盘前可见性（5.2）: before_trading 的 data 为停牌占位（is_open=0, 无价格）——当日
bar 盘前不可见, 防前视。

回执: 下单族返回 PTradeOrder（模拟回执）; sync_orders 绑定引擎订单后状态随内部
状态机动态映射（objects.ptrade_status_of, 回测子集 {2,7,8,6,9}）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from zquant.adapters.shared.code_style import denormalize_code
from zquant.adapters.shared.context_factory import refresh_context
from zquant.adapters.shared.data_apis import DataApiCore
from zquant.adapters.shared.g_container import GContainer
from zquant.adapters.shared.log_api import make_log
from zquant.adapters.shared.order_apis import make_order_api
from zquant.adapters.shared.portfolio_view import UniformPosition, uniform_portfolio
from zquant.core.codes import normalize_code
from zquant.core.errors import ZQuantError
from zquant.engine.orders import OrderRequest, OrderStatus

from .objects import (
    PTradeOrder,
    make_bar_data,
    make_ptrade_context,
    ptrade_portfolio,
    ptrade_position,
    ptrade_symbol,
)


class PTradeAdapter:
    """PTrade 平台适配器（L0 全量 + L1 子集, 4.7）。"""

    platform = "ptrade"

    def __init__(self) -> None:
        self._g = GContainer()
        self._orders: list[OrderRequest] = []
        self._receipt_seq = 0
        self._receipts: dict[str, PTradeOrder] = {}  # entrust_no → 回执
        self._receipt_by_req: dict[int, PTradeOrder] = {}  # id(req) → 回执（sync 对齐）
        self._daily_jobs: list[tuple[Callable[..., Any], str]] = []  # (func, 原始 time)
        self._pending_cancels: set[str] = set()  # 同 bar 撤单暂存（sync 后执行）
        self._in_initialize = False
        self.degradations: list[str] = []  # semantic_degradation（run 记录, 5.2）
        self.pending_initial_positions: dict[str, tuple[float, float | None]] = {}
        self._initialize: Callable[[Any], None] | None = None
        self._handle_data: Callable[[Any, Any], None] | None = None
        self._before_trading: Callable[[Any, Any], None] | None = None
        self._after_trading: Callable[[Any], None] | None = None
        self._ctx = make_ptrade_context(capital_base=0.0, previous_date=None)
        self._ctx.platform = "ptrade"
        self._ctx.g = self._g
        self._gateway = _InnerGateway(self)
        self._emit: Callable[[str, dict[str, Any]], None] | None = None

    # ------------------------------------------------------------------
    # StrategyAdapter 协议
    # ------------------------------------------------------------------
    def load(self, strategy_path: Path, context: Any = None) -> None:
        """加载策略源码: 预注入 g/log/API → exec → 解析生命周期入口（4.7）。"""
        code = Path(strategy_path).read_text(encoding="utf-8")
        namespace: dict[str, Any] = dict(self._api_namespace())
        namespace["__name__"] = "ptrade_strategy"
        exec(compile(code, str(strategy_path), "exec"), namespace)  # noqa: S102
        self._initialize = namespace.get("initialize")
        self._handle_data = namespace.get("handle_data")
        self._before_trading = namespace.get("before_trading")
        self._after_trading = namespace.get("after_trading_end")
        if self._initialize is None:
            raise ZQuantError(
                "PTrade 策略必须定义 initialize(context)",
                stage="adapter:ptrade",
                hint="PTrade 官方语义: initialize 是唯一必选入口",
            )

    def setup(self, account_view: Any = None) -> None:
        self._ctx.account = account_view

    def on_before_trading(self, ev: Any = None) -> None:
        """盘前回调（before_trading; 当日 bar 不可见, data 为停牌占位, 5.2）。"""
        if self._before_trading is not None:
            self._refresh(_self_now(self._ctx))
            self._before_trading(self._ctx, self._bar_data_map(include_today=False))

    def on_bar(self, ev: Any = None) -> None:
        """主驱动（15:00）: 刷新 context → run_daily 任务 → handle_data（4.7 顺序）。"""
        dt = _self_now(self._ctx)
        self._refresh(dt)
        for func, _original_time in list(self._daily_jobs):
            func(self._ctx)
        if self._handle_data is not None:
            self._handle_data(self._ctx, self._bar_data_map(include_today=True))

    def on_after_trading(self, ev: Any = None) -> None:
        if self._after_trading is not None:
            self._refresh(_self_now(self._ctx))
            self._after_trading(self._ctx)

    def take_orders(self) -> list[OrderRequest]:
        out, self._orders = self._orders, []
        return out

    def sync_orders(self, pairs: list[tuple[OrderRequest, Any]]) -> None:
        """回执 ↔ 引擎订单对齐（id(req) 匹配; 同 bar 撤单在绑定后立即执行, 5.3.1）。"""
        for req, order in pairs:
            receipt = self._receipt_by_req.get(id(req))
            if receipt is None or order is None:
                continue
            receipt.bind(order)
            if receipt.entrust_no in self._pending_cancels:
                self._try_cancel(receipt, order)

    def _try_cancel(self, receipt: PTradeOrder, eo: Any) -> None:
        """执行撤单（账本 cancel + 事件入流水; 已终态订单跳过）。"""
        from zquant.engine.orders import OrderStatus

        if eo.status not in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            return
        book = getattr(self._ctx, "order_book", None)
        if book is None:
            return
        ev = book.cancel(eo.order_id, when=_self_now(self._ctx))
        if ev is not None:
            record = getattr(self._ctx, "record_event_fn", None)
            if record is not None:
                record(ev)

    def finalize(self) -> None:
        self._in_initialize = False

    # ------------------------------------------------------------------
    # initialize 驱动（session 构造期调用, 4.2; ctx=session 注入面=本适配器 _ctx）
    # ------------------------------------------------------------------
    def run_initialize(self, ctx: Any) -> None:
        emit = getattr(ctx, "emit", None)
        if emit is not None:
            self._emit = emit  # log 事件接线（load 阶段暂存的 stub 升级为直发）
        task = getattr(ctx, "task", None)
        if task is not None and not self._ctx.capital_base:
            base = float(task.backtest.initial_capital)
            self._ctx.capital_base = base
            self._ctx.sim_params.capital_base = base
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
        """每 bar 刷新 context（内核字段 + PTrade portfolio 投影 + blotter）。

        策略侧 current_dt/blotter.current_dt 暴露为 naive 本地时间（对齐 PTrade 官方
        语义, 官方 current_dt 为 naive datetime; 内部 PIT 仍走 aware now_fn, 8.8）。
        """
        account = getattr(self._ctx, "account", None)
        cal = getattr(self._ctx, "calendar", None)
        previous = cal.before(dt.date()) if cal is not None else None
        universe_fn = getattr(self._ctx, "universe_fn", None)
        universe = list(universe_fn()) if universe_fn is not None else []
        pf = uniform_portfolio(account) if account is not None else None
        naive_dt = dt.replace(tzinfo=None)
        refresh_context(
            self._ctx,
            current_dt=naive_dt,
            previous_date=previous if previous is not None else dt.date(),
            universe=universe,
            portfolio=ptrade_portfolio(pf) if pf is not None else None,
        )
        self._ctx.blotter.current_dt = naive_dt

    def _phase(self) -> str:
        phase = getattr(self._ctx, "phase", None)
        return phase() if callable(phase) else "on_daily_close"

    def _bar_data_map(self, *, include_today: bool) -> dict[str, Any]:
        """handle_data/before_trading 的 data 载荷: {symbol: PTradeBarData}（4.7）。

        include_today=False（盘前）: 全部 is_open=0 占位无价格（防前视, 5.2）。
        """
        universe_fn = getattr(self._ctx, "universe_fn", None)
        universe = list(universe_fn()) if universe_fn is not None else []
        dt = _self_now(self._ctx)
        provider = getattr(self._ctx, "provider", None)
        out: dict[str, Any] = {}
        for code in sorted(universe):
            symbol = ptrade_symbol(code)
            if not include_today or provider is None:
                out[symbol] = make_bar_data(symbol=symbol, dt=dt, is_open=0)
                continue
            bar = provider.bar_at(code, dt)
            if bar is None:
                out[symbol] = make_bar_data(symbol=symbol, dt=dt, is_open=0)
                continue
            out[symbol] = make_bar_data(
                symbol=symbol,
                dt=dt,
                open=bar.open,
                close=bar.close,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
                money=bar.amount,
                preclose=bar.pre_close,
                high_limit=bar.limit_up,
                low_limit=bar.limit_down,
                is_open=0 if bar.suspended else 1,
            )
        return out

    def _data_core(self) -> DataApiCore:
        provider = getattr(self._ctx, "provider", None)
        if provider is None:
            raise ZQuantError("数据 API 需要引擎装配（provider 未注入）", stage="adapter:ptrade")
        return DataApiCore(
            provider,
            # PIT 时点保持 aware（now_fn 动态源, 8.8）; 不用策略侧 naive current_dt
            current_dt=lambda: _self_now(self._ctx),
            phase=self._phase,
        )

    # ------------------------------------------------------------------
    # L4 API 命名空间（module.__dict__ 预注入, 官方全局函数语义）
    # ------------------------------------------------------------------
    def _api_namespace(self) -> dict[str, Any]:
        ns: dict[str, Any] = {
            "g": self._g,
            "log": make_log(lambda k, p: self._emit_event(k, p)),
        }
        # 下单族（4.7 五函数; 回执 wrap 为 PTradeOrder）
        order_ns = make_order_api(
            "ptrade", self._gateway, lambda: _self_now(self._ctx), wrap=self._make_receipt
        )
        for key, value in vars(order_ns).items():
            if callable(value):
                ns[key] = value
        # 订单查询族（4.7）
        ns["cancel_order"] = self.cancel_order
        ns["get_orders"] = self.get_orders
        ns["get_order"] = self.get_order
        ns["get_open_orders"] = self.get_open_orders
        ns["get_trades"] = self.get_trades
        # 持仓族
        ns["get_position"] = self.get_position
        ns["get_positions"] = self.get_positions
        ns["get_all_positions"] = self.get_positions  # 官方两者近似（附录C L1）
        # 数据族
        ns["get_history"] = self.get_history
        ns["get_price"] = self.get_price
        ns["get_snapshot"] = self.get_snapshot
        # 交易日历族
        ns["get_trading_day"] = self.get_trading_day
        ns["get_trade_days"] = self.get_trade_days
        ns["get_all_trades_days"] = self.get_all_trades_days
        # 工具
        ns["check_limit"] = self.check_limit
        ns["is_trade"] = self.is_trade
        ns["get_frequency"] = self.get_frequency
        # 设置族 8 个（4.7）
        ns["set_universe"] = self.set_universe
        ns["set_benchmark"] = self.set_benchmark
        ns["set_commission"] = self.set_commission
        ns["set_slippage"] = self.set_slippage
        ns["set_fixed_slippage"] = self.set_fixed_slippage
        ns["set_volume_ratio"] = self.set_volume_ratio
        ns["set_limit_mode"] = self.set_limit_mode
        ns["set_yesterday_position"] = self.set_yesterday_position
        # 调度族
        ns["run_daily"] = self.run_daily
        ns["run_interval"] = self.run_interval
        return ns

    def _make_receipt(self, order_id: str, req: OrderRequest) -> PTradeOrder:
        """wrap 工厂: 平台 Order 模拟回执（4.7 amount 买正卖负; 数量未定风格记值, 近似）。"""
        style = req.style.value
        if style in ("quantity", "market"):
            raw = req.quantity or 0.0
        elif style == "target_quantity":
            raw = req.target_quantity or 0.0
        elif style == "value":
            raw = req.value or 0.0
        else:  # target_value
            raw = req.target_value or 0.0
        signed = raw if req.direction.value == "buy" else -raw
        receipt = PTradeOrder(
            entrust_no=order_id,
            symbol=denormalize_code(req.code),
            amount=signed,
            dt=_self_now(self._ctx),
        )
        self._receipts[order_id] = receipt
        self._receipt_by_req[id(req)] = receipt
        return receipt

    # ------------------------------------------------------------------
    # 调度族（4.7/5.2）
    # ------------------------------------------------------------------
    def run_daily(self, context: Any, func: Callable[..., Any], time: str = "14:50") -> None:
        """官方签名 run_daily(context, func, time); 仅 initialize 内可注册。"""
        if not self._in_initialize:
            raise ZQuantError(
                "run_daily 仅可在 initialize 中注册（PTrade 官方语义）",
                stage="adapter:ptrade",
                hint=f"当前试图注册 {getattr(func, '__name__', func)!r}; 移入 initialize",
            )
        t = str(time)
        if t not in ("09:30", "11:30", "13:00", "14:00", "14:50", "15:00", "open", "after_close"):
            self._note_degradation(f"run_daily time={t!r} 非 PTrade 标准时刻, 折叠 15:00 执行")
        elif t != "15:00":
            self._note_degradation(f"run_daily time={t!r} 日线回测折叠 15:00 执行（收盘后）")
        self._daily_jobs.append((func, t))

    def run_interval(self, context: Any, func: Callable[..., Any], seconds: int = 60) -> None:
        """官方 run_interval 仅实盘可用; 回测中结构化报错（4.7）。"""
        raise ZQuantError(
            "run_interval 仅实盘可用, 回测不支持",
            stage="adapter:ptrade",
            hint="日线回测请用 run_daily(context, func, time) 或 handle_data",
        )

    # ------------------------------------------------------------------
    # 设置族 8 个（4.7）
    # ------------------------------------------------------------------
    def set_universe(self, universe: list[str] | str) -> None:
        """股票池（归一 + 引擎生效; 懒加载由 provider 承担, T-E07 通道）。"""
        codes = [universe] if isinstance(universe, str) else list(universe)
        fn = getattr(self._ctx, "set_universe_fn", None)
        if fn is not None:
            fn(codes)
        self._ctx.universe = [normalize_code(c) for c in codes]

    def set_benchmark(self, code: str) -> None:
        """基准（M2 已知近似: 基准以 task.backtest.benchmark 为准, 运行时变更记降级）。"""
        self._note_degradation(f"set_benchmark({code!r}) 运行时设置不生效（基准取任务配置）")

    def set_commission(
        self,
        tradetype: str = "STOCK",
        commission_ratio: float = 0.0003,
        min_commission: float = 5.0,
        tax_ratio: float = 0.001,
        transfer_fee_ratio: float = 0.0,
    ) -> None:
        """按品种费率（STOCK/ETF/LOF; M2 统一套用全品种, 品种档案差异归 M3）。"""
        fn = getattr(self._ctx, "set_fees_fn", None)
        if fn is not None:
            fn(
                commission_rate=commission_ratio,
                min_commission=min_commission,
                stamp_tax_rate=tax_ratio if tradetype == "STOCK" else 0.0,
                transfer_fee_rate=transfer_fee_ratio,
            )
        self._ctx.commission.cost = commission_ratio
        self._ctx.commission.tax = tax_ratio if tradetype == "STOCK" else 0.0
        self._ctx.commission.min_trade_cost = min_commission
        if tradetype != "STOCK":
            self._note_degradation(
                f"set_commission(type={tradetype!r}) M2 统一费率套用全品种（品种档案归 M3）"
            )

    def set_slippage(self, value: float) -> None:
        """比例滑点（5.3.3）。"""
        fn = getattr(self._ctx, "set_slippage_fn", None)
        if fn is not None:
            fn(ratio=value)
        self._ctx.slippage.price_impact = value

    def set_fixed_slippage(self, value: float) -> None:
        """固定滑点（元）。"""
        fn = getattr(self._ctx, "set_slippage_fn", None)
        if fn is not None:
            fn(fixed=value)

    def set_volume_ratio(self, ratio: float) -> None:
        """参与率 → LiquidityModel（默认 0.25 对齐官方, 5.3.3）。"""
        fn = getattr(self._ctx, "set_liquidity_fn", None)
        if fn is not None:
            fn(ratio)

    def set_limit_mode(self, mode: str) -> None:
        """'UNLIMITED' → 关闭容量约束（一字板撮合约束保留, 已知近似）。"""
        if mode != "UNLIMITED":
            raise ZQuantError(
                f"set_limit_mode 仅支持 'UNLIMITED', 得到 {mode!r}", stage="adapter:ptrade"
            )
        fn = getattr(self._ctx, "set_liquidity_fn", None)
        if fn is not None:
            fn(1.0)
        self._note_degradation("set_limit_mode('UNLIMITED'): 关闭容量约束（一字板约束保留）")

    def set_yesterday_position(self, positions: dict[str, Any]) -> None:
        """底仓注入（官方 {sid: Position}; 暂存待 session 装配, 3.6/L5）。"""
        for sid, pos in positions.items():
            code = normalize_code(sid)
            if isinstance(pos, dict):
                amount = float(pos.get("amount", 0.0))
                cost = pos.get("cost_basis")
            elif isinstance(pos, (int, float)):
                amount, cost = float(pos), None
            else:
                amount = float(getattr(pos, "amount", 0.0))
                cost = getattr(pos, "cost_basis", None)
            self.pending_initial_positions[code] = (amount, float(cost) if cost else None)

    # ------------------------------------------------------------------
    # 订单查询族（读引擎订单/账本, 5.3.1）
    # ------------------------------------------------------------------
    def get_orders(self) -> dict[str, PTradeOrder]:
        """全部订单回执（entrust_no → PTradeOrder, 状态实时映射）。"""
        return dict(self._receipts)

    def get_order(self, entrust_no: str) -> PTradeOrder | None:
        return self._receipts.get(str(entrust_no))

    def get_open_orders(self) -> dict[str, PTradeOrder]:
        """未终态订单（内部 pending/partially_filled）。"""
        out: dict[str, PTradeOrder] = {}
        for no, r in self._receipts.items():
            eo = r._engine_order
            if eo is None or eo.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
                out[no] = r
        return out

    def get_trades(self) -> list[Any]:
        """成交明细（引擎 fills 只读视图）。"""
        fills = getattr(self._ctx, "fills", None)
        return list(fills) if fills is not None else []

    def cancel_order(self, entrust_no: str | PTradeOrder) -> None:
        """撤单（同 bar 撤单经 pending 暂存, sync_orders 绑定后立即执行; 5.3.1）。"""
        no = entrust_no.entrust_no if isinstance(entrust_no, PTradeOrder) else str(entrust_no)
        receipt = self._receipts.get(no)
        if receipt is None:
            return
        receipt.cancel_entrust_no = f"{no}-c"  # 官方撤单号（受理即记）
        self._pending_cancels.add(no)
        eo = receipt._engine_order
        if eo is not None:
            self._try_cancel(receipt, eo)

    # ------------------------------------------------------------------
    # 持仓族（4.7）
    # ------------------------------------------------------------------
    def get_position(self, security: str) -> Any:
        """单标的持仓（PTrade Position 投影; 无持仓 → amount=0 占位; sid=外部码）。"""
        code = normalize_code(security)
        account = getattr(self._ctx, "account", None)
        pf = uniform_portfolio(account) if account is not None else None
        pos = pf.positions.get(code) if pf is not None else None
        if pos is None:
            pos = UniformPosition(
                code=code,
                total_qty=0.0,
                today_qty=0.0,
                avg_cost=0.0,
                last_price=0.0,
                market_value=0.0,
            )
        view = ptrade_position(pos)
        view.sid = ptrade_symbol(code)  # 官方 sid（外部码风格, 4.7）
        return view

    def get_positions(self) -> dict[str, Any]:
        """全部持仓（symbol → PTrade Position）。"""
        account = getattr(self._ctx, "account", None)
        if account is None:
            return {}
        pf = uniform_portfolio(account)
        return {ptrade_symbol(c): ptrade_position(p) for c, p in sorted(pf.positions.items())}

    # ------------------------------------------------------------------
    # 数据族（4.7 / 3.13）
    # ------------------------------------------------------------------
    def get_history(
        self,
        count: int,
        frequency: str = "1d",
        field: str = "close",
        security_list: list[str] | str | None = None,
        fq: str = "pre",
        include: bool = False,
    ) -> Any:
        """官方 get_history: 历史数据（count 根 × 单字段; 单标的 np.array, 多标的 DataFrame）。"""
        core = self._data_core()
        if isinstance(security_list, str):
            codes = [normalize_code(security_list)]
        elif security_list:
            codes = [normalize_code(c) for c in security_list]
        else:
            universe_fn = getattr(self._ctx, "universe_fn", None)
            codes = list(universe_fn()) if universe_fn is not None else []
        frames = [
            core.history(c, count, unit="1d", fields=[field], include_today=include) for c in codes
        ]
        if len(codes) <= 1:
            frame = frames[0] if frames else pd.DataFrame()
            return frame[field].to_numpy() if field in getattr(frame, "columns", []) else frame
        return pd.concat([f[field].rename(c) for f, c in zip(frames, codes, strict=True)], axis=1)

    def get_price(
        self,
        security: str,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        frequency: str = "1d",
        fields: list[str] | None = None,
        fq: str = "pre",
    ) -> Any:
        """区间行情（日线; raw 价口径——复权价只用于研究, 3.14 纪律不破）。"""
        core = self._data_core()
        return core.get_price(
            normalize_code(security),
            start_date=_to_date(start_date),
            end_date=_to_date(end_date),
            unit="1d",
            fields=fields,
        )

    def get_snapshot(self, security_list: list[str] | None = None) -> dict[str, Any]:
        """实时快照; 回测日线=当日 bar 快照并计入降级清单（5.2-⑤）。"""
        self._note_degradation("get_snapshot 回测日线返回当日 bar 快照（非实时盘口, 5.2-⑤）")
        universe_fn = getattr(self._ctx, "universe_fn", None)
        if security_list:
            codes = [normalize_code(c) for c in security_list]
        else:
            codes = list(universe_fn()) if universe_fn is not None else []
        core = self._data_core()
        out: dict[str, Any] = {}
        dt = _self_now(self._ctx)
        for code in codes:
            bar = core.bar(code)
            sym = ptrade_symbol(code)
            if bar is None:
                out[sym] = make_bar_data(symbol=sym, dt=dt, is_open=0)
                continue
            out[sym] = make_bar_data(
                symbol=sym,
                dt=dt,
                open=bar.open,
                close=bar.close,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
                money=bar.amount,
                preclose=bar.pre_close,
                high_limit=bar.limit_up,
                low_limit=bar.limit_down,
                is_open=0 if bar.suspended else 1,
            )
        return out

    # ------------------------------------------------------------------
    # 交易日历族（4.7）
    # ------------------------------------------------------------------
    def get_trading_day(self, offset: int = 0) -> date | None:
        """当前交易日（offset 位移; 日历由数据日并集推导, 3.12-④）。"""
        cal = getattr(self._ctx, "calendar", None)
        today = _self_now(self._ctx).date()
        if cal is None:
            return today
        if offset == 0:
            return cal.before(today) or today
        d: date | None = today
        step = cal.after if offset > 0 else cal.before
        for _ in range(abs(offset)):
            d = step(d)  # type: ignore[operator]
            if d is None:
                return None
        return d

    def get_trade_days(
        self, start: str | date | None = None, end: str | date | None = None
    ) -> list[date]:
        """区间交易日列表（None → 全区间, 3.12-④）。"""
        cal = getattr(self._ctx, "calendar", None)
        if cal is None:
            return []
        first, last = cal.first_day, cal.last_day
        if first is None or last is None:
            return []
        s = _to_date(start) or first
        e = _to_date(end) or last
        return cal.trading_days(s, e)

    def get_all_trades_days(self) -> list[date]:
        """全部交易日（3.12-④）。"""
        return self.get_trade_days(None, None)

    # ------------------------------------------------------------------
    # 工具（4.7）
    # ------------------------------------------------------------------
    def check_limit(self, security: str) -> dict[str, Any]:
        """涨跌停检查（当日 close 与 limit_up/limit_down 比较, 5.2）。"""
        code = normalize_code(security)
        bar = self._data_core().bar(code)
        if bar is None:
            return {"is_limit_up": False, "is_limit_down": False, "paused": True}
        return {
            "is_limit_up": bar.high > 0 and bar.close >= bar.limit_up - 1e-9,
            "is_limit_down": bar.low > 0 and bar.close <= bar.limit_down + 1e-9,
            "paused": bool(bar.suspended),
        }

    def is_trade(self) -> bool:
        """是否交易时段（回测恒 False, 4.7 官方语义——回测无实时时段）。"""
        return False

    def get_frequency(self) -> str:
        """回测频率标识（日线 '1d'）。"""
        task = getattr(self._ctx, "task", None)
        return getattr(task.backtest, "frequency", "1d") if task is not None else "1d"


class _InnerGateway:
    """下单落点（K5 BrokerGateway: 收集 OrderRequest + 生成模拟回执 id）。"""

    def __init__(self, adapter: PTradeAdapter) -> None:
        self._adapter = adapter

    def submit_request(self, req: OrderRequest) -> str:
        self._adapter._receipt_seq += 1
        oid = f"pt{self._adapter._receipt_seq}"
        self._adapter._orders.append(req)
        return oid


def _self_now(ctx: Any) -> datetime:
    """当前回测时刻（now_fn 动态源优先; current_dt 静态值兜底, 8.8）。

    current_dt 由 refresh_context 写为静态值（策略读属性语义）, 不可作刷新源
    （会定格首日）——refresh 必须走 now_fn。
    """
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


def _register() -> None:
    from zquant.adapters.base import register_adapter

    register_adapter("ptrade", PTradeAdapter)


_register()
