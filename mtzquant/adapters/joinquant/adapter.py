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

    # ------------------------------------------------------------------
    # StrategyAdapter 协议
    # ------------------------------------------------------------------
    def load(self, strategy_path: Path, context: Any = None) -> None:
        """加载策略源码: 预注入 g/log/API/data → exec → 解析生命周期入口（4.6）。"""
        code = Path(strategy_path).read_text(encoding="utf-8")
        namespace: dict[str, Any] = dict(self._api_namespace())
        namespace["__name__"] = "joinquant_strategy"
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
        refresh_context(
            self._ctx,
            current_dt=dt,
            previous_date=previous if previous is not None else dt.date(),
            universe=universe,
            portfolio=jq_portfolio_view(pf) if pf is not None else None,
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

    def _jq_data(self, *, include_today: bool) -> dict[str, Any]:
        """handle_data 的 data 载荷: {security: 快照}（聚宽 data[code] 语义, 4.6）。"""
        universe_fn = getattr(self._ctx, "universe_fn", None)
        universe = list(universe_fn()) if universe_fn is not None else []
        dt = _self_now(self._ctx)
        provider = getattr(self._ctx, "provider", None)
        out: dict[str, Any] = {}
        for code in sorted(universe):
            sec = denormalize_code(code)
            if not include_today or provider is None:
                out[sec] = _jq_snapshot(sec, dt, paused=True)
                continue
            bar = provider.bar_at(code, dt)
            if bar is None:
                out[sec] = _jq_snapshot(sec, dt, paused=True)
                continue
            out[sec] = _jq_snapshot(
                sec,
                dt,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                paused=bool(bar.suspended),
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
        return receipt

    # ------------------------------------------------------------------
    # 调度族（4.6）
    # ------------------------------------------------------------------
    def run_daily(self, func: Callable[..., Any], time: str = "every_bar") -> None:
        if not self._in_initialize:
            raise MtzQuantError(
                "run_daily 仅可在 initialize 中注册（聚宽官方语义）", stage="adapter:joinquant"
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
        """每日执行已调度任务（run_daily 折叠后每日; run_weekly/monthly 首交易日）。"""
        for func, _t in list(self._daily_jobs):
            func(self._ctx)
        for func, weekday, _t in list(self._weekly_jobs):
            if dt.weekday() == (weekday - 1) % 7 or self._is_first_trade_weekday(dt):
                func(self._ctx)
        for func, monthday, _t in list(self._monthly_jobs):
            if dt.day == monthday or self._is_first_trade_monthday(dt):
                func(self._ctx)

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
        open_tax: float = 0.0,
        close_tax: float = 0.001,
        open_commission: float = 0.0003,
        close_commission: float = 0.0003,
        min_commission: float = 5.0,
    ) -> None:
        """聚宽 set_order_cost: 佣金/印花税（买卖侧统一, 4.6 已知近似）。"""
        fn = getattr(self._ctx, "set_fees_fn", None)
        if fn is not None:
            fn(
                commission_rate=open_commission,
                min_commission=min_commission,
                stamp_tax_rate=close_tax,
                transfer_fee_rate=0.0,
            )

    def set_slippage(self, value: float) -> None:
        fn = getattr(self._ctx, "set_slippage_fn", None)
        if fn is not None:
            fn(ratio=value)

    def set_commission(self, commission_ratio: float = 0.0003, min_commission: float = 5.0) -> None:
        """聚宽 set_commission（仅佣金, 4.6 变体）。"""
        fn = getattr(self._ctx, "set_fees_fn", None)
        if fn is not None:
            fn(commission_rate=commission_ratio, min_commission=min_commission)

    def set_option(self, key: str, value: Any) -> None:
        """聚宽 set_option（L2 子集: 能映射则映射, 否则结构化报错, 4.9）。"""
        if key in ("auto_handle_position", "use_real_price", "order_volume_ratio"):
            self._note_degradation(f"set_option({key!r}) 为聚宽 L2, 回测近似忽略")
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
            return frame if df else (frame[field].to_numpy() if field in frame else frame)
        return pd.concat([f[field].rename(c) for f, c in zip(frames, codes, strict=True)], axis=1)

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

    def get_price(
        self,
        security: str,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        frequency: str = "1d",
        fields: list[str] | None = None,
        skip_paused: bool = True,
        fq: str = "pre",
        count: int | None = None,
    ) -> Any:
        core = self._data_core()
        return core.get_price(
            normalize_code(security),
            start_date=_to_date(start_date),
            end_date=_to_date(end_date),
            count=count,
            unit="1d",
            fields=fields,
        )

    def get_current_data(self, security_list: list[str] | None = None) -> dict[str, Any]:
        """get_current_data: {security: CurrentData 快照}（4.6）。"""
        universe_fn = getattr(self._ctx, "universe_fn", None)
        if security_list:
            codes = [normalize_code(c) for c in security_list]
        else:
            codes = list(universe_fn()) if universe_fn is not None else []
        snap = self._jq_data(include_today=True)
        return {denormalize_code(c): snap.get(denormalize_code(c)) for c in codes}

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

    def get_all_securities(self, types: list[str] | None = None, date: str | None = None) -> Any:
        """全部证券（master 支撑; M2 近似: 由 universe 内已有标的给出, 4.6）。"""
        universe_fn = getattr(self._ctx, "universe_fn", None)
        codes = list(universe_fn()) if universe_fn is not None else []
        return pd.DataFrame(
            [{"code": c, "display_name": c, "name": c} for c in codes],
            columns=["code", "display_name", "name"],
        )

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

    # ------------------------------------------------------------------
    # 订单查询（聚宽 L0 子集: get_trades, 4.6）
    # ------------------------------------------------------------------
    def get_trades(self) -> list[Any]:
        fills = getattr(self._ctx, "fills", None)
        return list(fills) if fills is not None else []


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
    paused: bool = False,
) -> SimpleNamespace:
    """聚宽 data[security] / CurrentData 快照（字段子集, 4.6）。"""
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
        volume=volume,
        paused=paused,
        high_limit=0.0,
        low_limit=0.0,
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


def _register() -> None:
    from mtzquant.adapters.base import register_adapter

    register_adapter("joinquant", JoinQuantAdapter)


_register()
