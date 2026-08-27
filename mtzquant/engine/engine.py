# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:10:00
# @update_time        : 2026/08/25 21:10:00
# @description : F3 UnifiedBacktestEngine：日内十阶段主循环（5.1/6.4）+ 收盘撮合趟（5.3.3）+ W0

"""UnifiedBacktestEngine（设计 5.1）——统一回测主循环。

日内十阶段严格顺序（每交易日）:
  ① SessionStart        账户/日状态就绪, ControlSignal 检查（pause/stop）
  ② 公司行为开盘前生效    送转/拆股改数量稀释成本; 现金分红计 receivable（3.14）
  ③ T+1 释放             昨日买入转可卖（settle_day）
  ④ before_open 调度     盘前回调（当日 bar 不可见）
  ⑤ 开盘撮合             处理各标的当日 bar: BrokerSim + 成交入账（阶段⑥回调已含⑤）
  ⑥ 策略回调             日线 15:00（handle_data 语义; 账户已含⑤成交, 防重复下单）
  ⑦ 盘中                （日线无）
  ⑧ on_daily_close 调度  日线降级折叠点（strict_schedule 校验在 Scheduler）
  ⑨ 估值 mark_to_market  raw_close 估值 + DailyNav（停牌/退市 stale_price 标记）
  ⑩ 分红到账             pay_date: receivable → available

终态: completed_exact / completed_degraded / stopped / error。
控制: 每 bar 边界检查 ControlSignal（pause gate 挂起、stop 终止）。
"""

from __future__ import annotations

import time as _time  # 别名: 下方 from datetime import time 会遮蔽 stdlib time
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from typing import Any, Protocol

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.broker import BrokerSim, MatchOutcome
from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.orderbook import OpenOrderBook
from mtzquant.engine.orders import (
    Fill,
    OrderDirection,
    OrderEvent,
    OrderEventType,
    OrderStatus,
)

_OPEN: time = time(9, 30)
_CLOSE: time = time(15, 0)


def _at15(dt: datetime) -> datetime:
    """当日 15:00 收盘时刻（撮合趟 bar 时戳, 5.3.3）。"""
    return dt.replace(hour=15, minute=0, second=0, microsecond=0)


@dataclass
class StageTrace:
    """阶段顺序追踪（T-E04 断言: 每交易日应完整命中 ①-⑩ 且顺序正确）。"""

    order: list[str] = field(default_factory=list)

    def hit(self, stage: str) -> None:
        self.order.append(stage)


@dataclass
class ControlSignal:
    """回测控制（6.4: 暂停门 / 停止旗）。"""

    pause_requested: bool = False
    stop_requested: bool = False
    gate_open: bool = True  # pause 挂起时 False, 主循环原地等待（M4 语义占位）


class SessionPort(Protocol):
    """引擎 → 会话的依赖口（数据/账户/回调, F5 BacktestSession 实现）。"""

    def trading_days(self) -> list[datetime]: ...
    def bar_at(self, code: str, dt: datetime) -> MinimalBar | None: ...
    def apply_open_actions(self, dt: datetime) -> list[str]: ...  # 阶段②: 返回公司行为说明
    def release_t1(self) -> None: ...  # 阶段③
    def run_before_open(self, dt: datetime) -> None: ...  # 阶段④
    def run_strategy(self, dt: datetime) -> list[Any]: ...  # 阶段⑥: 返回 Order 列表
    def run_on_close(self, dt: datetime) -> None: ...  # 阶段⑧
    def mark_to_market(self, dt: datetime) -> Any: ...  # 阶段⑨: 返回 NavPoint
    def settle_dividends(self) -> None: ...  # 阶段⑩
    def orders_to_book(self, orders: list[Any]) -> None: ...  # 策略产出 → 账本受理
    def profile_of(self, code: str) -> Any: ...  # InstrumentProfile
    def universe(self) -> list[str]: ...
    def available_cash(self) -> float: ...
    def frozen_of_order(self, order_id: str) -> float: ...  # 该单账户冻结额（5.3.4 终检）
    def release_order_freeze(self, order_id: str) -> None: ...  # 释放该单账户冻结
    def closeable_qty(self, code: str) -> float: ...  # T+1 可卖
    def is_pending_sell(self, order_id: str) -> bool: ...  # 挂单卖出已同步入账（跳过 T+1 终检）
    def record_event(self, ev: Any) -> None: ...  # 订单事件入流水
    def account_apply_fill(self, fill: Any) -> None: ...  # 成交入账（含费用）
    def finalize(self) -> Any: ...  # 返回结果快照
    def emit(self, kind: str, payload: dict[str, Any]) -> None: ...  # 事件发布（6.3 信封源, W0）


class UnifiedBacktestEngine:
    """统一回测引擎（设计 5.1 十阶段）。"""

    def __init__(
        self,
        session: SessionPort,
        *,
        broker: BrokerSim | None = None,
        control_refresh: Any | None = None,
    ) -> None:
        self.session = session
        self.broker = broker or BrokerSim()
        # 待撮合账本与会话共享同一实例（orders_to_book 受理 → 此处撮合, 5.3.1）;
        # 无 order_book 的桩会话（T-E04）回退到引擎自有账本。
        self.order_book: Any = getattr(session, "order_book", None)
        if self.order_book is None:
            self.order_book = OpenOrderBook()
        self.control = ControlSignal()
        self._control_refresh = control_refresh  # M4-W2: 每日外部控制刷新（pause/stop 文件, 6.4）
        self.trace = StageTrace()
        self.degradations: list[str] = []  # 语义降级（一字板/挂单过期/适配器折叠, 4.9.2）
        self.frictions: list[str] = []  # 撮合摩擦（cash_capped 缩量; 不触发 degraded）
        self.status = "completed_exact"
        self._navs: list[Any] = []

    # ------------------------------------------------------------------
    def run(self) -> Any:
        """跑完整回测; 返回会话的最终快照。"""
        days = list(self.session.trading_days())
        self._total_days = len(days)
        self._t0 = _time.monotonic()
        try:
            for idx, dt in enumerate(days):
                if self._control_refresh is not None:
                    # M4: 外部控制（Web/CLI pause/stop 同权, 6.4）——pause 原地阻塞等待
                    self._control_refresh(self.control)
                if self.control.stop_requested:
                    self.status = "stopped"
                    break
                self._run_day(dt, day_index=idx)
        except MtzQuantError:
            self.status = "error"
            self._emit("status", {"status": "error"})
            raise
        if self.status == "completed_exact" and self.degradations:
            self.status = "completed_degraded"
        # 终态事件（W0 监控页状态条; committed 由 store.flush 在回测收尾时落定）
        self._emit(
            "status",
            {
                "status": self.status,
                "degradations": list(self.degradations),
                "frictions": list(self.frictions),
            },
        )
        return self.session.finalize()

    def _run_day(self, dt: datetime, day_index: int = 0) -> None:
        self.trace.hit("session_start")  # ①
        if not self.control.gate_open:
            self.trace.hit("paused_gate")  # M4 挂起占位
            return
        # ② 公司行为开盘前生效（送转/拆股改数量, 分红计应收, 3.14）
        self.trace.hit("corp_open")
        self.session.apply_open_actions(dt)
        # ③ T+1 释放（昨日买入转可卖）
        self.trace.hit("t1_release")
        self.session.release_t1()
        # ④ before_open 调度（当日 bar 不可见）
        self.trace.hit("before_open")
        self.session.run_before_open(dt)
        # ⑤ 开盘撮合: 各标的当日 bar → BrokerSim → 成交入账
        self.trace.hit("open_match")
        self._match_open(dt)
        # ⑥ 策略回调（日线 15:00, 账户已含⑤成交）
        self.trace.hit("strategy")
        orders = self.session.run_strategy(dt)
        if orders:
            self.session.orders_to_book(orders)
        # ⑥.5 收盘撮合（5.3.3: same_close 当日 15:00 / next_close 次日 15:00 单;
        # next_open 单未到可撮合时点 → 天然 no-op, 不改变 next_open 语义）
        self.trace.hit("close_match")
        self._match_close(dt)
        # ⑦ 盘中（日线无, 占位）
        self.trace.hit("intraday_none")
        # ⑧ on_daily_close 调度（日线降级折叠点）
        self.trace.hit("on_close")
        self.session.run_on_close(dt)
        # ⑨ 估值 mark_to_market（raw_close）+ DailyNav
        self.trace.hit("mark_to_market")
        nav = self.session.mark_to_market(dt)
        self._navs.append(nav)
        self._emit_progress(dt, day_index)
        # ⑩ 分红到账（pay_date: receivable → available）
        self.trace.hit("dividend_settle")
        self.session.settle_dividends()

    # ------------------------------------------------------------------
    def _match_open(self, dt: datetime) -> None:
        for code in sorted(self.session.universe()):
            bar = self.session.bar_at(code, dt)
            if bar is None:
                continue
            profile = self.session.profile_of(code)
            # 只撮合本标的订单（多标的池防串号成交, 5.3.2）
            outcomes = self.broker.process_orders(self.order_book, bar, profile, code=code)
            for oc in outcomes:
                self._apply_outcome(oc)

    def _match_close(self, dt: datetime) -> None:
        """收盘撮合趟（5.3.3 same_close/next_close 口径, 设计 5.3.3 前视警示）。

        与 _match_open 同管线（5.3.2 按 code 过滤）; bar 取回后时戳重标为当日
        15:00, 使 eligible_fill_at<=15:00 的订单（same_close 当日单）入选、次日单
        （next_open/next_close）不入选——SAME_CLOSE/NEXT_CLOSE 定价取 bar.close。
        """
        for code in sorted(self.session.universe()):
            bar = self.session.bar_at(code, dt)
            if bar is None:
                continue
            bar = replace(bar, dt=_at15(dt))
            profile = self.session.profile_of(code)
            outcomes = self.broker.process_orders(self.order_book, bar, profile, code=code)
            for oc in outcomes:
                self._apply_outcome(oc)

    def _apply_outcome(self, oc: MatchOutcome) -> None:
        if oc.one_word_board:
            self.degradations.append(f"{oc.order.order_id} @one_word: {oc.order.code} 一字板未成交")
        if oc.fill is not None:
            fill = oc.fill
            # 防御性终检（5.3.4）: 卖出超 T+1 可卖 → 拒单不中断; 买入超现金 → 缩量部分成交。
            if fill.side in (
                OrderDirection.BUY,
                OrderDirection.OPEN_LONG,
                OrderDirection.CLOSE_SHORT,
            ):
                oc = self._cap_buy_to_cash(oc)
                if oc.order.status is OrderStatus.REJECTED:
                    return  # 拒单已处理（释放冻结/记事件/终态）, 不再应用成交
            elif not self.session.is_pending_sell(oc.order.order_id) and (
                fill.volume > self.session.closeable_qty(fill.code) + 1e-9
            ):
                self._reject_fill(oc, "t_plus_sell_unavailable")
                return
        for ev in oc.events:
            self.session.record_event(ev)
        if oc.fill is not None:
            self.session.account_apply_fill(oc.fill)
        self.order_book.drop_order(oc.order.order_id)

    def _cap_buy_to_cash(self, oc: MatchOutcome) -> MatchOutcome:
        """买入成交超可支付现金（冻结按现价, 成交含滑点/跳空）→ 缩量部分成交到可支付。

        对齐聚宽: 等分现金策略末单按现价冻结≈可用, 成交价略高时缩量而非拒单（否则
        永远补不满最后一仓）。缩量后重算费用并用 PARTIAL_FILL 事件替换原 FILL 事件。
        返回（可能替换后的）MatchOutcome。
        """
        assert oc.fill is not None
        fill = oc.fill
        frozen = self.session.frozen_of_order(oc.order.order_id)
        need = fill.amount + fill.total_fee
        afford = self.session.available_cash() + frozen
        if need <= afford:
            return oc
        if fill.price <= 0 or afford <= 0:
            self._reject_fill(oc, "insufficient_cash")
            return oc
        # 可支付股数（取整 100 股, 留 0.5% 费用余量）
        capped_qty = max(0.0, int(afford * 0.995 / fill.price / 100.0) * 100.0)
        capped_qty = min(capped_qty, fill.volume)
        if capped_qty <= 0:
            self._reject_fill(oc, "insufficient_cash")
            return oc
        # 重算费用（复用 profile.fee）
        profile = self.session.profile_of(fill.code)
        fee = profile.fee
        new_amount = capped_qty * fill.price
        commission = max(fee.commission_min, fee.commission_rate * new_amount)
        is_sell = fill.side in (
            OrderDirection.SELL,
            OrderDirection.CLOSE_LONG,
            OrderDirection.CLOSE_SHORT,
        )
        stamp_tax = fee.stamp_tax_rate * new_amount if is_sell else 0.0
        transfer_fee = fee.transfer_fee_rate * new_amount
        # 用 PARTIAL_FILL 事件替换原 FILL 事件（成交缩量, 事件量一致）
        new_fill = Fill(
            order_id=fill.order_id,
            code=fill.code,
            side=fill.side,
            price=fill.price,
            volume=capped_qty,
            fill_time=fill.fill_time,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage_cost=fill.slippage_cost,
            bar_volume=fill.bar_volume,
            participation_rate=round(capped_qty / fill.bar_volume, 6) if fill.bar_volume else 0.0,
        )
        new_event = OrderEvent(
            order_id=fill.order_id,
            event_type=OrderEventType.PARTIAL_FILL,
            event_time=fill.fill_time,
            qty=capped_qty,
            price=fill.price,
            info_json={"reason": "cash_capped", "fill_ratio": round(capped_qty / fill.volume, 6)},
        )
        oc = replace(oc, fill=new_fill, events=[new_event])
        # 摩擦与降级分级（N1, 2026-08-19）: 缩量是常规撮合摩擦（事件流已留
        # PARTIAL_FILL.reason=cash_capped 逐笔证据）, 不再计入语义降级清单,
        # 否则降级常态化会让 completed_degraded 失去区分度。
        self.frictions.append(
            f"{oc.order.order_id} @cash_capped: {fill.code} 缩量成交 "
            f"{fill.volume:.0f}→{capped_qty:.0f}"
        )
        return oc

    def _reject_fill(self, oc: MatchOutcome, reason: str) -> None:
        """成交防御性终检不通过 → 拒单（释放冻结/记事件/终态 REJECTED）, 不中断回测。"""
        assert oc.fill is not None
        self.degradations.append(
            f"{oc.order.order_id} @{reason}: {oc.order.code} 拒单（{oc.fill.code} {reason}）"
        )
        self.order_book.drop_order(oc.order.order_id)
        self.session.release_order_freeze(oc.order.order_id)
        self.session.record_event(
            OrderEvent(
                order_id=oc.order.order_id,
                event_type=OrderEventType.REJECTED,
                event_time=oc.fill.fill_time,
                info_json={"reason": reason},
            )
        )
        oc.order.status = OrderStatus.REJECTED
        oc.order.reject_reason = reason

    @property
    def daily_nav_rows(self) -> int:
        return len(self._navs)

    # ------------------------------------------------------------------
    # 进度/终态事件（W0 监控页: 进度条 + 状态条; 6.3 信封, 直发不经 WriteBuffer）
    # ------------------------------------------------------------------
    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        emit = getattr(self.session, "emit", None)
        if emit is not None:
            emit(kind, payload)

    def _emit_progress(self, dt: datetime, day_index: int) -> None:
        done = day_index + 1
        total = max(1, self._total_days)
        elapsed = _time.monotonic() - self._t0
        eta_sec = int(elapsed / done * (total - done)) if done else 0
        self._emit(
            "progress",
            {
                "trade_date": dt.date().isoformat(),
                "day_index": day_index,
                "total_days": total,
                "percent": round(done / total, 4),
                "elapsed_seconds": round(elapsed, 3),
                "eta_seconds": eta_sec,
            },
        )
