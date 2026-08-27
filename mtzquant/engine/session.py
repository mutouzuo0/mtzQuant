# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/25 22:50:00
# @description : F5/I1 BacktestSession：生产化会话 + 任务配置解析（3.6/5.1）; W0 emit + K4 视图

"""BacktestSession（设计 5.1 SessionPort 的生产实现，阶段 I 提炼自 golden DailyDriver）。

装配: CsvSourceDriver → DataNormalizer → MarketDataProvider(PIT) + TradeCalendar +
     NativeAdapter + Account + OpenOrderBook + BrokerSim + ResultStore。

阶段⑥ 策略回调语义（与 golden 六要素口径一致，4.9.2）:
  订单 15:00 收盘受理、eligible_fill_at 按 fill_price 成交价基准分发（默认
  same_close=当日 15:00 收盘, 5.3.3 前视警示; next_open=次日 09:30, 4.5）;
  target_value 归一与整手取整由本会话按 InstrumentProfile.lot_size 完成;
  撮合=收盘/开盘按基准（FillModel 三值 + 买卖侧代理滑点, 5.3.3）;
  T+1 可卖校验、现金不足拒单、停牌/退市冻结估值（g03/g04/g06/g12 同构）。

时间纪律（8.8 确定性）: 会话内一律 Asia/Shanghai tz-aware 毫秒时间戳;
  dict 遍历先排序; 禁止未播种随机。

任务配置（设计 3.6）: task.json 是唯一事实来源, 本模块 pydantic 校验;
  敏感字段入库前经 config.sanitize_params 脱敏（由 runner 负责）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from mtzquant.adapters.base import create_adapter
from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import KLINE_COLUMNS
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.corporate_actions import load_corporate_actions
from mtzquant.data.drivers.base import SourceDriver
from mtzquant.data.provider import MarketDataProvider
from mtzquant.engine.account import Account, Position
from mtzquant.engine.broker import BrokerSim, MatchingModels
from mtzquant.engine.corp_actions import CorpActionType, CorporateAction
from mtzquant.engine.instrument import FeeParams, InstrumentProfile, etf_profile, stock_profile
from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.models.fee import FeeModel
from mtzquant.engine.models.fill_price import FillModel, PriceBasis
from mtzquant.engine.models.liquidity import LiquidityModel
from mtzquant.engine.models.slippage import SlippageModel
from mtzquant.engine.orders import (
    Fill,
    Order,
    OrderDirection,
    OrderEvent,
    OrderEventType,
    OrderRequest,
    OrderStatus,
    OrderStyle,
    TimeInForce,
)
from mtzquant.engine.results import ResultStore

_SH = ZoneInfo("Asia/Shanghai")

# 未配置基准时的默认基准: 沪深300ETF（净值曲线基准列, 起点归一=1, 8.4）
DEFAULT_BENCHMARK = "510300.SH"

# 买入侧方向（冻结可用资金; 卖出不冻结, 5.3.4）
_BUY_SIDES = frozenset({OrderDirection.BUY, OrderDirection.OPEN_LONG, OrderDirection.CLOSE_SHORT})
_SELL_SIDES = frozenset(
    {OrderDirection.SELL, OrderDirection.CLOSE_LONG, OrderDirection.CLOSE_SHORT}
)


# ============================================================
# 任务配置（设计 3.6：task.json 唯一事实来源, pydantic 校验）
# ============================================================
class StrategySpec(BaseModel):
    """策略规格: 源码路径 + 平台适配器 + 入口。"""

    file: str
    type: str = "native"  # 平台: native(本轮) / joinquant / ptrade(M2)
    entry: str = "on_bar"  # 入口函数（native: on_bar）


class BacktestSpec(BaseModel):
    """回测规格（3.6: 区间/资金/基准/频率/初始持仓/严格调度）。"""

    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD
    initial_capital: float = 1_000_000.0
    benchmark: str | None = None  # 基准代码（如 000300.SH, 生成 benchmark_nav）
    frequency: str = "1d"  # v1 仅日线
    initial_positions: dict[str, float] = Field(default_factory=dict)  # code → qty
    strict_schedule: bool = False
    delist_dates: dict[str, str] = Field(default_factory=dict)  # code → YYYY-MM-DD 最后交易日
    corp_actions: list[dict[str, Any]] = Field(default_factory=list)  # 3.14 三日期事件


class FeeSpec(BaseModel):
    """费率覆盖项（缺省用 settings.engine.default_fees, 3.6）。"""

    commission_rate: float | None = None
    min_commission: float | None = None
    stamp_tax_rate: float | None = None
    transfer_fee_rate: float | None = None


class TaskConfig(BaseModel):
    """回测任务（task.json 全字段; 3.6 / docs/schema/task.schema.json 一致）。"""

    task_name: str
    strategy: StrategySpec
    backtest: BacktestSpec
    universe: list[str] = Field(default_factory=list)  # 可空: runner 从策略源码自动提取
    fees: FeeSpec = Field(default_factory=FeeSpec)
    engine: dict[str, Any] = Field(default_factory=dict)  # 撮合覆盖项（M5 增强, 预留）


# ============================================================
# 策略可见的账户视图（4.2 setup(account_view), 轻量只读）
# ============================================================
@dataclass(frozen=True)
class _PosView:
    """策略可见的持仓（total_qty/市值/成本/最新价; M2-K4 增 today_qty 支撑 closeable/enable）。"""

    code: str
    total_qty: float
    market_value: float
    avg_cost: float
    last_price: float
    today_qty: float = 0.0  # 今日买入量（T+1: closeable = total - today）


@dataclass
class AccountView:
    """策略可见的账户视图（不可写; 撮合/记账仍在引擎侧唯一实现）。"""

    positions: dict[str, _PosView] = field(default_factory=dict)
    available_cash: float = 0.0
    receivable_cash: float = 0.0
    frozen_cash: float = 0.0
    total_value: float = 0.0  # 现金四分类 + 持仓市值
    initial_cash: float = 0.0  # 期初资金（平台 portfolio.starting_cash, 4.4 投影）

    @property
    def total_cash(self) -> float:
        return self.available_cash + self.receivable_cash + self.frozen_cash


# ============================================================
# 生产化会话（SessionPort 实现）
# ============================================================
class BacktestSession:
    """生产化回测会话（设计 5.1 SessionPort, F5 装配 + I 阶段完整驱动）。"""

    def __init__(
        self,
        task: TaskConfig,
        *,
        driver: SourceDriver,
        provider: MarketDataProvider,
        calendar: TradeCalendar,
        run_id: str | None = None,
        settings_fees: FeeParams | None = None,
        max_participation: float = 0.25,
        result_store: ResultStore | None = None,
        fill_price: str | None = None,
    ) -> None:
        self.task = task
        self.run_id = run_id or f"r_{task.task_name}"
        self._driver = driver
        self._provider = provider
        self._calendar = calendar
        self._result_store = result_store
        # M2-W0: 信封 run_id 归属（store 独立构造时可在此补齐, 6.3）
        if result_store is not None and not result_store.run_id:
            result_store.run_id = self.run_id

        # 运行态（先于适配器装配: ctx 注入引用以下列表/状态, M2 平台适配器依赖）
        self._universe: list[str] = normalize_universe(task.universe)
        self._profiles: dict[str, InstrumentProfile] = {}
        self._last_close_px: dict[str, float] = {}
        self._orders: list[Order] = []
        self._fills: list[Fill] = []
        self._events: list[OrderEvent] = []
        self._navs: list[dict[str, Any]] = []
        self._fees: dict[str, float] = {
            "commission": 0.0,
            "stamp_tax": 0.0,
            "transfer_fee": 0.0,
        }
        self._cum_fee = 0.0
        self._degradations: list[str] = []
        self._current_dt: datetime | None = None
        self._phase = "before_open"
        self._order_seq = 0
        self.status = "completed_exact"
        self._frozen_by_order: dict[str, float] = {}  # order_id → 账户冻结额（买入, 5.3.4）
        # order_id → (code, qty, est 回款): 挂单卖出同步入账待确认
        self._pending_sell_by_order: dict[str, tuple[str, float, float]] = {}
        self._pending_sync: list[tuple[OrderRequest, Order | None]] = []  # M2: 受理后回执对齐
        self._benchmark_close: dict[str, float] = {}
        self._order_book: Any = None  # 惰性（order_book 属性首次访问创建, 引擎装配后可用）

        # 撮合三件套（5.3.3 五模型; 先于适配器——initialize 期 set_* 族即可生效, 4.7）
        fee_params = settings_fees or FeeParams()
        self._fee_params = fee_params
        # 成交价基准解析: 显式参数 > task.engine.fill_price（task 唯一事实来源, 3.6）
        # > 引擎默认 same_close（5.3.3 前视警示）; runner 以 settings 兜底传参。
        basis_src = fill_price or (task.engine or {}).get("fill_price") or "same_close"
        try:
            basis = PriceBasis(basis_src)
        except ValueError:
            raise MtzQuantError(
                f"fill_price 非法: {basis_src!r}（可选 next_open/same_close/next_close）",
                stage="session",
                hint="task.engine.fill_price 须为 PriceBasis 三值之一",
            ) from None
        self._fill_price = basis  # 成交价基准（5.3.3: 决策当日收盘/次日开盘/次日收盘, 前视警示）
        self._broker = BrokerSim(
            models=MatchingModels(
                fill=FillModel(basis=basis, half_spread=0.0),
                slippage=SlippageModel(ratio=0.0),
                fee=FeeModel(),
                liquidity=LiquidityModel(max_participation=max_participation),
            )
        )
        self._account = Account(
            run_id=self.run_id,
            initial_cash=task.backtest.initial_capital,
            available_cash=task.backtest.initial_capital,
        )

        # 平台适配器（native; M2 joinquant/ptrade 复用骨架）
        self._adapter: Any = cast(Any, create_adapter(task.strategy.type))
        self._adapter.load(str(task.strategy.file))
        self._adapter.setup(AccountView())
        # 注入策略侧 API（native v1: history / timestamp / account）
        self._adapter._ctx.history = self._strategy_history  # type: ignore[attr-defined]
        self._adapter._ctx.timestamp = datetime(2000, 1, 1, 15, 0, tzinfo=_SH)
        # M2-K/L/N: 平台适配器注入面（provider PIT/事件/任务/日历/账户刷新/撮合设置/订单视图）
        ctx = self._adapter._ctx  # type: ignore[attr-defined]
        ctx.provider = self._provider
        ctx.emit = self.emit
        ctx.task = self.task
        ctx.calendar = self._calendar
        # M3: 主数据路径（get_all_securities PIT / 名称 / ST 判定; 经 driver settings 解析）
        _driver = getattr(self._provider, "_driver", None)
        _ds = getattr(_driver, "settings", None)
        ctx.master_path = (
            str(Path(_ds.root_path) / "master" / "instruments.csv")
            if _ds is not None
            else "data/master/instruments.csv"
        )
        ctx.current_dt = lambda: self._current_dt
        ctx.now_fn = lambda: self._current_dt  # M2: 适配器取当前回测时刻（动态, 每 bar 更新）
        ctx.phase = lambda: self._phase
        ctx.universe_fn = self.universe
        ctx.available_cash_fn = self.available_cash
        ctx.profile_of = self._profile
        ctx.order_book = self.order_book  # 待撮合账本（订单查询族只读视图, 5.3.1）
        ctx.orders = self._orders  # 引擎订单列表（live 状态, 4.7 get_order 族）
        ctx.fills = self._fills  # 成交列表（get_trades, 4.7）
        ctx.set_universe_fn = self._set_universe
        ctx.set_liquidity_fn = self._set_liquidity_participation
        ctx.set_fees_fn = self._set_fees
        ctx.set_slippage_fn = self._set_slippage
        ctx.record_event_fn = self._record_event
        ctx.run_id = self.run_id
        ctx.task_name = task.task_name
        # 生命周期: 首次驱动前调用策略 initialize（4.2 注入语义;
        # M2 平台适配器优先走 run_initialize（带调度注册窗口/接线）, native 走 _initialize）
        runner = getattr(self._adapter, "run_initialize", None)
        if runner is not None:
            runner(ctx)
        else:
            init = getattr(self._adapter, "_initialize", None)
            if init is not None:
                init(self._adapter._ctx)

        # M3-T1 参数注入（扫描任务: task.engine.params 覆盖策略默认 g 值, 4.7 参数族）:
        # 策略 initialize 先设默认, 此处框架参数覆盖——双均线 fast/slow 等扫描参数生效。
        params = (task.engine or {}).get("params") or {}
        if params:
            g = getattr(self._adapter, "_ctx", None)
            g = getattr(g, "g", None)
            if g is not None:
                for k, v in sorted(params.items()):
                    g[k] = v

        # 公司行为: 共享日历 data/corporate_actions/{type}/{code}.csv（设计 3.12, 通用市场数据,
        # 独立于价格与策略）自动加载 + 任务配置 corp_actions 覆盖（向后兼容, 后者优先）。
        self._corp_actions: list[CorporateAction] = []
        if self._driver is not None:
            lcs = getattr(self._driver, "settings", None)
            root = Path(getattr(lcs, "root_path", "data"))
            tmpl = getattr(lcs, "corporate_actions_dir", "corporate_actions/{type}")
            self._corp_actions = [
                self._parse_corp_action(item) for item in load_corporate_actions(root, tmpl)
            ]
        self._corp_actions += [self._parse_corp_action(item) for item in task.backtest.corp_actions]

        self._init_initial_positions()
        self._init_benchmark()

    # ------------------------------------------------------------------
    # 装配辅助
    # ------------------------------------------------------------------
    @property
    def order_book(self) -> Any:
        """待撮合队列（惰性创建, 5.3.1）。"""
        if self._order_book is None:
            from mtzquant.engine.orderbook import OpenOrderBook

            self._order_book = OpenOrderBook()
        return self._order_book

    @property
    def account(self) -> Account:
        return self._account

    @property
    def broker(self) -> BrokerSim:
        return self._broker

    def _parse_corp_action(self, item: dict[str, Any]) -> CorporateAction:
        act_type = CorpActionType(item.get("type", "cash_div"))
        return CorporateAction(
            code=item["code"],
            action_type=act_type,
            announce_date=date.fromisoformat(str(item.get("announce_date", item["ex_date"]))),
            ex_date=date.fromisoformat(str(item["ex_date"])),
            pay_date=date.fromisoformat(str(item["pay_date"])) if item.get("pay_date") else None,
            per_share_cash=float(item["per_share_cash"]) if item.get("per_share_cash") else None,
            ratio=float(item["ratio"]) if item.get("ratio") else 0.0,
        )

    def _init_initial_positions(self) -> None:
        """初始持仓: 首日估值价 = 首个交易日的 raw close（5.5）。

        M2-L5: 适配器 initialize 中 set_yesterday_position 暂存的底仓在此融合
        （pending 格式 {code: (qty, cost_basis|None)}, 复用官方三字段语义）。
        """
        pending = getattr(self._adapter, "pending_initial_positions", None)
        merged: dict[str, tuple[float, float | None]] = {
            code: (float(qty), None) for code, qty in self.task.backtest.initial_positions.items()
        }
        for code, (qty, cost) in (pending or {}).items():
            merged[code] = (qty, cost)  # 适配器底仓优先（策略显式设置）
        days = self.trading_days()
        day0 = days[0] if days else datetime(2000, 1, 1, 15, 0, tzinfo=_SH)
        for code, (qty, cost) in sorted(merged.items()):
            if not qty:
                continue
            bar = self._provider.bar_at(code, _at(day0, 15, 0))
            px = cost if cost and cost > 0 else (bar.close if bar is not None else 0.0)
            self._account.positions[code] = Position(
                code=code, total_qty=float(qty), avg_cost=px, last_price=px
            )

    def _init_benchmark(self) -> None:
        """基准净值: close_t / 回测起点前最近收盘（benchmark_nav, 8.4; 起点归一=1）。

        未配置基准时默认沪深300ETF（510300.SH）; 基准本地数据缺失时安静降级为无基准。
        """
        bench = self.task.backtest.benchmark or DEFAULT_BENCHMARK
        try:
            arr = self._provider.bar_array(bench)
        except Exception:  # noqa: BLE001 — 基准缺数据不阻断回测, 仅无基准曲线
            return
        if not arr.size:
            return
        # 归一基数: 回测起点当日/之前最近一个收盘（保证基准曲线起点=1, 而非 CSV 全history首日）
        start = self.task.backtest.start  # "YYYY-MM-DD"（ISO 字典序 = 日期序）
        base = None
        for ms, c in zip(arr["dt"], arr["close"], strict=True):
            if _day_str(int(ms)) <= start:
                base = float(c)
            else:
                break
        if base is None or base <= 0:
            base = float(arr["close"][0])  # 基准数据晚于回测起点 → 以首个可得收盘为基
        if base <= 0:
            return
        self._benchmark_close = {
            _day_str(int(ms)): float(c) / base
            for ms, c in zip(arr["dt"], arr["close"], strict=True)
        }

    # ------------------------------------------------------------------
    # 品种档案（5.4：stock/etf 内置; 撮合零硬编码）
    # ------------------------------------------------------------------
    def _profile(self, code: str) -> InstrumentProfile:
        prof = self._profiles.get(code)
        if prof is not None:
            return prof
        if _is_etf_code(code):
            base = etf_profile(code)
        else:
            base = stock_profile(code)
        bf = base.fee
        # 费率优先级: 任务 fees 显式覆盖 > 品种档案默认（ETF 免印花税等, 5.4）
        prof = InstrumentProfile(
            code=code,
            instrument_type=base.instrument_type,
            name=base.name,
            lot_size=base.lot_size,
            t_plus=base.t_plus,
            limit_rule=base.limit_rule,
            fee=FeeParams(
                commission_rate=(
                    self.task.fees.commission_rate
                    if self.task.fees.commission_rate is not None
                    else bf.commission_rate
                ),
                commission_min=(
                    self.task.fees.min_commission
                    if self.task.fees.min_commission is not None
                    else bf.commission_min
                ),
                stamp_tax_rate=(
                    self.task.fees.stamp_tax_rate
                    if self.task.fees.stamp_tax_rate is not None
                    else bf.stamp_tax_rate
                ),
                transfer_fee_rate=(
                    self.task.fees.transfer_fee_rate
                    if self.task.fees.transfer_fee_rate is not None
                    else bf.transfer_fee_rate
                ),
            ),
        )
        self._profiles[code] = prof
        return prof

    # ------------------------------------------------------------------
    # SessionPort 协议实现
    # ------------------------------------------------------------------
    def trading_days(self) -> list[datetime]:
        """回测区间内交易日（15:00 tz-aware; 由数据日并集推导的日历给出）。"""
        days = self._calendar.trading_days(
            date.fromisoformat(self.task.backtest.start),
            date.fromisoformat(self.task.backtest.end),
        )
        return [_at(d, 15, 0) for d in days]

    def bar_at(self, code: str, dt: datetime) -> MinimalBar | None:
        """当日 bar（撮合时点=09:30; 数据取自归一化 15:00 bar, 3.3）。"""
        bar = self._provider.bar_at(code, _at(dt, 15, 0))
        if bar is None:
            return None
        profile = self._profile(code)
        return MinimalBar(
            dt=_at(dt, 9, 30),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            pre_close=bar.pre_close,
            suspended=bool(bar.suspended),
            limit_up=self._touched(bar, profile, up=True),
            limit_down=self._touched(bar, profile, up=False),
        )

    @staticmethod
    def _touched(bar: Any, profile: InstrumentProfile, *, up: bool) -> bool:
        """是否触及涨跌停价（raw 价, 5.4 limit_map; NaN 守卫）。"""
        limit = bar.limit_up if up else bar.limit_down
        if limit != limit or limit <= 0:  # NaN 守卫
            return False
        return bool(bar.close >= limit) if up else bool(bar.close <= limit)

    def apply_open_actions(self, dt: datetime) -> list[str]:
        """阶段② 公司行为开盘前生效（3.14: ex_date 生效, 当日策略即见新持仓）。"""
        notes: list[str] = []
        day = dt.date()
        for action in sorted(self._corp_actions, key=lambda a: (a.code, a.ex_date)):
            if action.applies_on(day, day) and action.code in self._account.positions:
                rec = action.apply_on_ex_date(self._account, dt)
                self._emit("corp_action", self._corp_payload(rec))
                notes.append(f"{action.code} {action.action_type.value} @ {action.ex_date}")
        return notes

    def release_t1(self) -> None:
        """阶段③ T+1 释放：昨日买入量清零 → 今日可卖（5.4）。"""
        for pos in self._account.positions.values():
            pos.roll_to_new_day()

    def run_before_open(self, dt: datetime) -> None:
        """阶段④ 盘前调度（native v1 无钩子; bar 对策略不可见）。"""
        self._current_dt = dt
        self._phase = "before_open"
        self._adapter.on_before_trading(None)

    def run_strategy(self, dt: datetime) -> list[Order]:
        """阶段⑥ 策略回调（15:00; 账户已含⑤开盘成交, 防重复下单）。"""
        self._current_dt = dt
        self._phase = "on_daily_close"
        self._refresh_closes(dt)
        self._adapter._ctx.timestamp = dt  # type: ignore[attr-defined]
        self._adapter._ctx.account = self._build_account_view()  # type: ignore[attr-defined]
        self._adapter.on_bar(self._strategy_bar(dt))
        requests = self._adapter.take_orders()

        def _dir_key(d: Any) -> str:
            return d.value if isinstance(d, OrderDirection) else str(d)

        # M2-L/N: 平台回执 id ↔ 引擎 order_id 对齐（受理完成后由 orders_to_book 触发,
        # 保证同 bar 撤单在账本受理之后执行, 5.3.1）
        pairs: list[tuple[OrderRequest, Order | None]] = []
        orders: list[Order] = []
        for req in sorted(requests, key=lambda r: (r.created_at, r.code, _dir_key(r.direction))):
            order = self._translate(req)
            pairs.append((req, order))
            if order is not None:
                orders.append(order)
        self._pending_sync = pairs
        return orders

    def run_on_close(self, dt: datetime) -> None:
        """阶段⑧ on_daily_close: 当日已 eligible 未成交的 day 单过期（5.3.4）。"""
        self._adapter.on_after_trading(None)
        events = self.order_book.expire_day_orders(when=_at(dt, 15, 0))
        for ev in events:
            # 走公开 record_event: EXPIRE 须释放账户冻结（5.3.4）, 防现金泄漏
            self.record_event(ev)
            self._mark_degraded(f"{ev.order_id} @{dt.date()}: expired(day) 当日未成交")

    def mark_to_market(self, dt: datetime) -> dict[str, Any]:
        """阶段⑨ 收盘估值（raw_close）+ DailyNav（停牌/退市 stale 标记, 5.5）。"""
        day = dt.date()
        closes = self._last_close_px
        stale: list[str] = []
        open_pos = 0
        positions_value = 0.0
        for code in sorted(self._account.positions):
            pos = self._account.positions[code]
            bar = self._provider.bar_at(code, _at(dt, 15, 0))
            today = bar is not None and not bar.suspended and not self._is_delisted(code, day)
            if not today:
                stale.append(code)
            elif pos.total_qty > 0:
                open_pos += 1  # 仅计真实持仓（挂单卖出 0 量占位不计, 5.3.4）
            pos.last_price = closes.get(code, pos.last_price)
            positions_value += pos.market_value
        total_cash = self._account.total_cash
        equity = total_cash + positions_value
        nav = equity / self._account.initial_cash
        gross_nav = (equity + self._cum_fee) / self._account.initial_cash
        running_max = max((p["nav"] for p in self._navs), default=1.0)
        drawdown = 1.0 - nav / max(running_max, 1e-12) if running_max > 0 else 0.0
        point = {
            "trade_date": day.isoformat(),
            "nav": round(nav, 12),
            "gross_nav": round(gross_nav, 12),
            "cash": round(total_cash, 6),
            "positions_value": round(positions_value, 6),
            "total_value": round(equity, 6),
            "drawdown": round(drawdown, 12),
            "open_positions": open_pos,
            "stale_codes": list(stale),
            "cumulative_fee": round(self._cum_fee, 6),
            "benchmark_nav": (
                round(self._benchmark_close.get(day.isoformat(), float("nan")), 12)
                if self._benchmark_close
                else None
            ),
        }
        self._navs.append(point)
        self._emit("daily_nav", point)
        return point

    def settle_dividends(self) -> None:
        """阶段⑩ 分红到账（pay_date: receivable → available, 4.4）。"""
        day = self._current_dt.date() if self._current_dt else None
        if day is None:
            return
        for action in sorted(self._corp_actions, key=lambda a: (a.code, a.ex_date)):
            if action.applied_pay_date(day, day):
                amount = self._account.settle_dividend()
                if amount:
                    self._emit("log", {"kind": "dividend_settled", "amount": amount})

    def orders_to_book(self, orders: list[Order]) -> None:
        """账本受理（5.3.1 accept: 买入冻结可用资金; 现金不足 → REJECTED）。

        聚宽同步卖出语义（M3-N6）: **卖单先受理并即时入账**——持仓减量 + 回款计入
        可用现金, 使同批买入可等分"卖出回款+现金"（原版等分现金策略依赖此语义）;
        成交时补差（实际-预估）, 过期/撤销恢复。
        """
        live = [o for o in orders if o.status is not OrderStatus.REJECTED]
        for order in live:
            if order.side in _BUY_SIDES:
                continue  # 买单在卖单之后受理（回款已入账）
            ev = self.order_book.accept(
                order,
                available_cash=self._account.available_cash,
                ref_price=self._px(order.code),
                commission_rate=self._fee_params.commission_rate,
                min_commission=self._fee_params.commission_min,
                slippage_ratio=0.0,
            )
            if ev is not None:
                self._record_event(ev)
                if ev.event_type is OrderEventType.ACCEPTED:
                    pos = self._account.positions.get(order.code)
                    est = order.qty * (
                        pos.last_price if pos and pos.last_price else self._px(order.code)
                    )
                    self._account.pending_sell(order.code, order.qty, est)
                    self._pending_sell_by_order[order.order_id] = (order.code, order.qty, est)
        for order in live:
            if order.side not in _BUY_SIDES:
                continue
            ev = self.order_book.accept(
                order,
                available_cash=self._account.available_cash,
                ref_price=self._px(order.code),
                commission_rate=self._fee_params.commission_rate,
                min_commission=self._fee_params.commission_min,
                # 按现价冻结（不含滑点）——对齐聚宽: 策略等分现金时最后一单冻结≈可用;
                # 滑点/跳空导致的超现金由引擎成交侧缩量部分成交兜底（5.3.4）。
                slippage_ratio=0.0,
            )
            if ev is not None:
                self._record_event(ev)
                if ev.event_type is OrderEventType.ACCEPTED:
                    est = self.order_book.frozen_of(order.order_id)
                    self._account.freeze_cash(est)
                    self._frozen_by_order[order.order_id] = est
        # 受理完成后对齐平台回执（同 bar 撤单此刻可执行, 5.3.1/4.7）
        pairs = getattr(self, "_pending_sync", None)
        if pairs:
            sync = getattr(self._adapter, "sync_orders", None)
            if sync is not None:
                sync(pairs)
            self._pending_sync = []

    def universe(self) -> list[str]:
        return list(self._universe)

    def available_cash(self) -> float:
        return self._account.available_cash

    def release_order_freeze(self, order_id: str) -> None:
        """释放该订单在账户侧的冻结（公开入口; 拒单/过期/撤销后调用, 5.3.4）。"""
        self._release_order_freeze(order_id)

    def frozen_of_order(self, order_id: str) -> float:
        """该订单在账户侧的冻结额（防御性终检用: 成交前可用=available+frozen, 5.3.4）。"""
        return self._frozen_by_order.get(order_id, 0.0)

    def closeable_qty(self, code: str) -> float:
        """T+1 可卖数量（防御性终检用: 卖出成交不得超可卖, 5.3.4）。"""
        pos = self._account.positions.get(code)
        return pos.closeable_qty if pos is not None else 0.0

    def is_pending_sell(self, order_id: str) -> bool:
        """该订单是否为已同步入账的挂单卖出（受理时已减仓, 成交终检跳过 T+1）。"""
        return order_id in self._pending_sell_by_order

    def profile_of(self, code: str) -> Any:
        return self._profile(code)

    # ------------------------------------------------------------------
    # M2-L/N: 平台 set 族落点（set_universe / set_volume_ratio, 4.6/4.7）
    # ------------------------------------------------------------------
    def _set_universe(self, codes: list[str]) -> None:
        """set_universe 动态股票池: 归一 + 去重; 引擎逐日按新池撮合（懒加载由 provider 承担）。"""
        self._universe = normalize_universe(codes)

    def _set_liquidity_participation(self, ratio: float) -> None:
        """set_volume_ratio → LiquidityModel 参与率（5.3.3; 默认 0.25 对齐官方）。"""
        if not 0.0 < ratio <= 1.0:
            raise MtzQuantError(f"参与率必须 (0,1]，得到 {ratio}", stage="session")
        self._broker.models = replace(
            self._broker.models,
            liquidity=self._broker.models.liquidity.with_participation(ratio),
        )

    # ------------------------------------------------------------------
    # M2-L5: 平台 set 族运行时设置（费率/滑点/底仓, 4.7 设置族）
    # ------------------------------------------------------------------
    def _set_fees(
        self,
        *,
        commission_rate: float | None = None,
        min_commission: float | None = None,
        stamp_tax_rate: float | None = None,
        transfer_fee_rate: float | None = None,
    ) -> None:
        """set_commission 落点: 更新任务费率 + 清空品种档案缓存（新 profile 按新费率生成）。"""
        if commission_rate is not None:
            self.task.fees.commission_rate = commission_rate
        if min_commission is not None:
            self.task.fees.min_commission = min_commission
        if stamp_tax_rate is not None:
            self.task.fees.stamp_tax_rate = stamp_tax_rate
        if transfer_fee_rate is not None:
            self.task.fees.transfer_fee_rate = transfer_fee_rate
        self._profiles.clear()  # 缓存失效, _profile 按新 task.fees 重建

    def _set_slippage(self, *, ratio: float | None = None, fixed: float | None = None) -> None:
        """set_slippage/set_fixed_slippage 落点: 替换 SlippageModel（5.3.3）。"""
        cur = self._broker.models.slippage
        self._broker.models = replace(
            self._broker.models,
            slippage=SlippageModel(
                ratio=cur.ratio if ratio is None else ratio,
                fixed=cur.fixed if fixed is None else fixed,
            ),
        )

    def record_event(self, ev: Any) -> None:
        """订单事件入流水; 成交/部分成交/过期/撤销释放账户冻结（5.3.4）。

        PARTIAL_FILL 即释放该单全额预留（v1 日线: 部分成交后剩余量为 day 单,
        当日收盘必过期, 不会再成交——预留全额归还可用, 成交额再扣回, 5.3.4）。
        挂单卖出（同步入账）过期/撤销 → 恢复持仓并退回预估回款（M3-N6）。
        """
        self._events.append(ev)
        self._emit("order_event", self._event_payload(ev))
        if ev.event_type in (
            OrderEventType.FILL,
            OrderEventType.PARTIAL_FILL,
            OrderEventType.EXPIRE,
            OrderEventType.CANCEL,
        ):
            self._release_order_freeze(ev.order_id)
        if ev.event_type in (OrderEventType.EXPIRE, OrderEventType.CANCEL, OrderEventType.REJECTED):
            pending = self._pending_sell_by_order.pop(ev.order_id, None)
            if pending is not None:
                code, qty, est = pending
                self._account.restore_pending_sell(code, qty, est)

    def account_apply_fill(self, fill: Any) -> None:
        # 挂单卖出（受理时已入账）→ 成交确认补差, 不重复走普通卖出记账
        self._fees["commission"] += fill.commission
        self._fees["stamp_tax"] += fill.stamp_tax
        self._fees["transfer_fee"] += fill.transfer_fee
        self._cum_fee += fill.total_fee
        if fill.side in _SELL_SIDES and fill.order_id in self._pending_sell_by_order:
            code, qty, est = self._pending_sell_by_order.pop(fill.order_id)
            self._account.apply_sell_credited(fill, qty, est)
        else:
            self._account.apply_fill(fill)
        self._fills.append(fill)
        self._emit("fill", self._fill_payload(fill))

    def finalize(self) -> dict[str, Any]:
        """结束回测: 返回结果字典（runner 导出/入库用, 9.1）。"""
        if self._result_store is not None:
            self._result_store.finalize()
        # M2-L/N: 适配器语义降级并入 run 记录（4.9/5.2: run 记录降级项）
        adapter_degr = list(getattr(self._adapter, "degradations", None) or [])
        if adapter_degr and self.status == "completed_exact":
            self.status = "completed_degraded"
        return {
            "run_id": self.run_id,
            "navs": list(self._navs),
            "orders": [self._order_payload(o) for o in self._orders],
            "fills": [self._fill_payload(f) for f in self._fills],
            "events": [self._event_payload(e) for e in self._events],
            "fees": dict(self._fees),
            "status": self.status,
            "degradations": list(self._degradations) + adapter_degr,
        }

    # ------------------------------------------------------------------
    # 订单归一与翻译（4.5：四种风格 → 整手 qty + 方向）
    # ------------------------------------------------------------------
    def _normalize(self, req: OrderRequest) -> tuple[float, OrderDirection] | None:
        """归一为 (带符号 qty, 方向); 差=0/无效 → None（g08 忽略）。"""
        code = req.code
        profile = self._profile(code)
        style = req.style
        # 策略侧可能传字符串枚举值（如 "sell"）→ 归一为 OrderDirection 成员（4.5）
        direction = (
            req.direction
            if isinstance(req.direction, OrderDirection)
            else OrderDirection(req.direction)
        )
        held = self._held(code)
        if style in (OrderStyle.QUANTITY, OrderStyle.MARKET):
            qty = req.quantity or 0.0
            if qty <= 0:
                return None
            qty = (
                profile.sellable_lot_round(qty, held)
                if direction is OrderDirection.SELL
                else profile.lot_round(qty)
            )
            return (qty, direction) if qty > 0 else None
        if style is OrderStyle.VALUE:
            px = self._px(code)
            if px <= 0:
                return None
            qty = (req.value or 0.0) / px
            if qty <= 0:
                return None
            qty = (
                profile.sellable_lot_round(qty, held)
                if direction is OrderDirection.SELL
                else profile.lot_round(qty)
            )
            return (qty, direction) if qty > 0 else None
        if style is OrderStyle.TARGET_QUANTITY:
            diff = (req.target_quantity or 0.0) - held
            qty = self._signed(diff, profile, held=held)
            if qty == 0:
                return None  # 目标=当前持仓 → 忽略（g08）
            return qty, (OrderDirection.BUY if diff > 0 else OrderDirection.SELL)
        if style is OrderStyle.TARGET_VALUE:
            px = self._px(code)
            if px <= 0:
                return None
            mv = held * px
            diff = (req.target_value or 0.0) - mv
            qty = self._signed(diff / px, profile, held=held)
            if qty == 0:
                return None  # 目标=当前市值 → 忽略（g08）
            return qty, (OrderDirection.BUY if diff > 0 else OrderDirection.SELL)
        raise MtzQuantError(f"未支持下单风格 {style}", stage="session")

    @staticmethod
    def _signed(x: float, profile: InstrumentProfile, *, held: float = 0.0) -> float:
        if x > 0:
            return profile.lot_round(x)
        return -profile.sellable_lot_round(abs(x), held)

    def _held(self, code: str) -> float:
        pos = self._account.positions.get(code)
        return pos.total_qty if pos else 0.0

    def _px(self, code: str) -> float:
        """最近结算收盘价基准（4.5 归一基准; 停牌沿用最近有效收盘）。"""
        return self._last_close_px.get(code, 0.0)

    def _translate(self, req: OrderRequest) -> Order | None:
        """OrderRequest → Order（前置校验: 空量/退市/T+1 可卖, 4.5）。"""
        norm = self._normalize(req)
        if norm is None:
            return None  # g08: 目标=当前 → 忽略, 无订单无事件
        qty, direction = norm
        order = self._new_order(req, direction, abs(qty))
        if direction is OrderDirection.SELL:
            pos = self._account.positions.get(req.code)
            closeable = pos.closeable_qty if pos else 0.0
            if abs(qty) > closeable:
                return self._reject(order, "t_plus_sell_unavailable")
        return order

    def _new_order(self, req: OrderRequest, direction: OrderDirection, qty: float) -> Order:
        self._order_seq += 1
        order = Order(
            order_id=f"{self.run_id}-o{self._order_seq}",
            run_id=self.run_id,
            code=req.code,
            side=direction,
            style=req.style,
            qty=qty,
            order_api=req.order_api,
            submitted_at=req.created_at,
            eligible_fill_at=self._eligible_fill_at(),
            time_in_force=TimeInForce.DAY,
        )
        self._orders.append(order)
        return order

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        ev = OrderEvent(
            order_id=order.order_id,
            event_type=OrderEventType.REJECTED,
            event_time=order.submitted_at,
            info_json={"reason": reason},
        )
        self._record_event(ev)
        return order

    def _eligible_fill_at(self) -> datetime:
        """订单可撮合时点（按 fill_price 成交价基准分发, 5.3.3）。

        - same_close（默认）: 当日 15:00 收盘——策略 15:00 决策后由引擎
          阶段⑥.5 收盘撮合趟以当日收盘价成交（决策含当日收盘数据, same-bar
          口径, 前视警示见设计 5.3.3）;
        - next_open: 次日 09:30 开盘（4.5 原默认, g13 时刻级）;
        - next_close: 次日 15:00 收盘。
        """
        if self._fill_price is PriceBasis.SAME_CLOSE:
            return (
                _at(self._current_dt, 15, 0)
                if self._current_dt is not None
                else datetime(2000, 1, 1, 15, 0, tzinfo=_SH)
            )
        if self._fill_price is PriceBasis.NEXT_CLOSE:
            return self._next_at(15, 0)
        return self._next_open()

    def _next_open(self) -> datetime:
        """下一交易日 09:30（4.5: 订单预约次日开盘撮合, g13 时刻级）。"""
        return self._next_at(9, 30)

    def _next_at(self, hour: int, minute: int) -> datetime:
        """下一交易日 hour:minute（tz-aware; 无交易日/会话未开 → 纪元回退）。"""
        if self._current_dt is None:
            return datetime(2000, 1, 1, hour, minute, tzinfo=_SH)
        nxt = self._calendar.after(self._current_dt.date())
        if nxt is None:
            return datetime(2000, 1, 1, hour, minute, tzinfo=_SH)
        return _at(nxt, hour, minute)

    # ------------------------------------------------------------------
    # 成交入账（5.5: raw 价记账 + 现金四分类恒等式）
    # ------------------------------------------------------------------
    def _account_apply_fill(self, fill: Fill) -> None:
        self._fees["commission"] += fill.commission
        self._fees["stamp_tax"] += fill.stamp_tax
        self._fees["transfer_fee"] += fill.transfer_fee
        self._cum_fee += fill.total_fee
        self._account.apply_fill(fill)
        self._fills.append(fill)
        self._emit("fill", self._fill_payload(fill))

    def _release_order_freeze(self, order_id: str) -> None:
        """释放该订单在账户侧的冻结（成交/过期/撤销后调用, 5.3.4）。"""
        est = self._frozen_by_order.pop(order_id, 0.0)
        if est:
            self._account.release_frozen_cash(est)

    def _record_event(self, ev: OrderEvent) -> None:
        self._events.append(ev)
        self._emit("order_event", self._event_payload(ev))

    def _mark_degraded(self, note: str) -> None:
        if self.status == "completed_exact":
            self.status = "completed_degraded"
        self._degradations.append(note)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._result_store is not None:
            self._result_store.emit(kind, payload)

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        """事件发布入口（6.3 信封源; 引擎进度/终态与 M2-W0 WS 直发共用）。"""
        self._emit(kind, payload)

    # ------------------------------------------------------------------
    # 收盘估值辅助
    # ------------------------------------------------------------------
    def _refresh_closes(self, dt: datetime) -> None:
        """收盘后刷新各 code 基准价（4.5 目标价基准; 停牌/退市沿用最近有效收盘）。"""
        day = dt.date()
        closes = dict(self._last_close_px)
        for code in sorted(self._universe):
            if self._is_delisted(code, day):
                continue  # 退市（delist_date 后无 bar）: 冻结
            bar = self._provider.bar_at(code, _at(dt, 15, 0))
            if bar is None:
                continue  # 数据缺失: 沿用
            if bar.suspended and code in closes:
                continue  # 停牌: 沿用
            closes[code] = bar.close
        self._last_close_px = closes
        for code, pos in self._account.positions.items():
            if code in closes:
                pos.last_price = closes[code]

    def _is_delisted(self, code: str, day: date) -> bool:
        delist = self.task.backtest.delist_dates.get(code)
        if not delist:
            return False
        return day > date.fromisoformat(delist)

    # ------------------------------------------------------------------
    # 策略侧 API
    # ------------------------------------------------------------------
    def _strategy_history(self, code: str, n: int, *, as_of: Any = None) -> Any:
        """策略可见历史（PIT: as_of 默认当前回调时点; 盘前不含当日, 收盘含当日）。"""
        if self._current_dt is None:
            raise MtzQuantError("history 只能在回测运行期内调用（策略回调中）", stage="session")
        if isinstance(as_of, datetime):
            as_of_dt = as_of
        elif isinstance(as_of, date):
            as_of_dt = _at(as_of, 15, 0)
        elif isinstance(as_of, str):
            as_of_dt = _at(date.fromisoformat(as_of), 15, 0)
        else:
            as_of_dt = self._current_dt
        include_today = self._phase == "on_daily_close"
        return self._provider.history(
            code, list(KLINE_COLUMNS), n, as_of=as_of_dt, include_today=include_today
        )

    def _strategy_bar(self, dt: datetime) -> Any:
        """on_bar 的 bar 载荷（native v1: 首个 universe 代码的当日 bar; 多代码走 history）。"""
        if not self._universe:
            return SimpleNamespace(dt=dt, code="", open=0.0, high=0.0, low=0.0, close=0.0)
        code = self._universe[0]
        bar = self._provider.bar_at(code, _at(dt, 15, 0))
        return SimpleNamespace(
            dt=dt,
            code=code,
            open=bar.open if bar else 0.0,
            high=bar.high if bar else 0.0,
            low=bar.low if bar else 0.0,
            close=bar.close if bar else 0.0,
            volume=bar.volume if bar else 0.0,
        )

    def _build_account_view(self) -> AccountView:
        view = AccountView(
            positions={
                code: _PosView(
                    code=code,
                    total_qty=pos.total_qty,
                    market_value=pos.market_value,
                    avg_cost=pos.avg_cost,
                    last_price=pos.last_price,
                    today_qty=pos.today_qty,
                )
                # 排除 0 量占位（挂单卖出待确认）: 策略视图只应看到真实持仓, 5.3.4
                for code, pos in self._account.positions.items()
                if pos.total_qty > 0
            },
            available_cash=self._account.available_cash,
            receivable_cash=self._account.receivable_cash,
            frozen_cash=self._account.frozen_cash,
        )
        view.total_value = view.total_cash + sum(p.market_value for p in view.positions.values())
        view.initial_cash = self._account.initial_cash
        return view

    # ------------------------------------------------------------------
    # 载荷序列化（机读/落库/导出, 8.8 时间 ISO）
    # ------------------------------------------------------------------
    @staticmethod
    def _order_payload(o: Order) -> dict[str, Any]:
        return {
            "order_id": o.order_id,
            "code": o.code,
            "side": o.side.value,
            "style": o.style.value,
            "qty": o.qty,
            "order_api": o.order_api,
            "status": o.status.value,
            "submitted_at": _iso(o.submitted_at),
            "eligible_fill_at": _iso(o.eligible_fill_at) if o.eligible_fill_at else None,
            "time_in_force": o.time_in_force.value,
            "remaining_qty": o.remaining_qty,
            "filled_qty": o.filled_qty,
            "avg_fill_price": o.avg_fill_price,
            "reject_reason": o.reject_reason,
        }

    @staticmethod
    def _fill_payload(f: Fill) -> dict[str, Any]:
        return {
            "order_id": f.order_id,
            "code": f.code,
            "side": f.side.value,
            "price": f.price,
            "volume": f.volume,
            "amount": f.amount,
            "fill_time": _iso(f.fill_time),
            "commission": f.commission,
            "stamp_tax": f.stamp_tax,
            "transfer_fee": f.transfer_fee,
            "slippage_cost": f.slippage_cost,
            "total_fee": f.total_fee,
            "bar_volume": f.bar_volume,  # 容量证据（8.4.4）
            "participation_rate": f.participation_rate,
        }

    @staticmethod
    def _event_payload(e: OrderEvent) -> dict[str, Any]:
        return {
            "order_id": e.order_id,
            "event_type": e.event_type.value,
            "event_time": _iso(e.event_time),
            "qty": e.qty,
            "price": e.price,
            "info_json": e.info_json,
        }

    @staticmethod
    def _corp_payload(rec: Any) -> dict[str, Any]:
        return {
            "code": rec.code,
            "action_type": rec.action_type.value,
            "ex_date": rec.ex_date.isoformat(),
            "pay_date": rec.pay_date.isoformat() if rec.pay_date else None,
            "apply_time": _iso(rec.apply_time),
            "detail": rec.detail,
        }


# ============================================================
# 工具
# ============================================================
def normalize_universe(codes: list[str]) -> list[str]:
    """universe 代码归一（设计 3.4），保持任务给定顺序去重。"""
    from mtzquant.core.codes import normalize_code

    seen: dict[str, None] = {}
    for c in codes:
        seen.setdefault(normalize_code(c), None)
    return list(seen)


def _is_etf_code(code: str) -> bool:
    """ETF 前缀启发（510300.SH / 159915.SZ; v1 目标池, 5.4）。"""
    return code.startswith(("5", "15", "16"))


def _at(day: date | datetime, hour: int, minute: int) -> datetime:
    d = day.date() if isinstance(day, datetime) else day
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_SH)


def _iso(dt: datetime) -> str:
    return dt.astimezone(_SH).isoformat(timespec="seconds")


def _day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=_SH).date().isoformat()
