# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:05:00
# @update_time        : 2026/08/16 21:59:08
# @description : F2 BrokerSim：事件驱动撮合（一字板/容量截断/滑点/费用/部分成交, 设计 5.3.2/5.3.3）

"""BrokerSim（设计 5.3.2/5.3.3）——真实撮合内核, 替代阶段 C 的 MockBroker。

只被行情事件调用（process_orders(order_book, bar, profile)），绝不被策略直接驱动:
  停牌      → 当日不撮合（gtc 顺延, day 收盘过期）
  一字板    → 整日不成交（open==high==low==close 且触停价）→ day 过期 + 记 degradation
  触板打开  → 正常撮合（info_json 记 touched_limit 提示）
  容量      → 单笔 ≤ bar 成量 × 参与率（LiquidityModel, 默认 0.25）→ 部分成交
  价格      → FillModel 基准（next_open, 买卖侧代理）+ SlippageModel 滑点
  费用      → FeeModel（B6, 档案驱动）

产出: 每个订单的 MatchOutcome（Fill + OrderEvent 迁移 + 冻结释放额）, 由引擎入账。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from mtzquant.engine.instrument import InstrumentProfile
from mtzquant.engine.models import (
    FeeModel,
    FillModel,
    LatencyModel,
    LiquidityModel,
    PriceBasis,
    SlippageModel,
)
from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.orderbook import OpenOrderBook
from mtzquant.engine.orders import (
    Fill,
    Order,
    OrderEvent,
    OrderEventType,
    TimeInForce,
    transition_order,
)


@dataclass(frozen=True)
class MatchingModels:
    """撮合五模型组合快照（任务默认; M5+ 可替换增强实现）。"""

    fill: FillModel = FillModel(basis=PriceBasis.NEXT_OPEN, half_spread=0.001)
    slippage: SlippageModel = SlippageModel(ratio=0.0)  # 滑点默认已由 fill 买卖侧代理承担
    fee: FeeModel = FeeModel()
    liquidity: LiquidityModel = LiquidityModel(max_participation=0.25)
    latency: LatencyModel = LatencyModel(bars_delay=0)


@dataclass(frozen=True)
class MatchOutcome:
    """单个订单在某一 bar 的撮合结果（引擎据此入账/释放/记降级）。"""

    order: Order
    events: list[OrderEvent]
    fill: Fill | None
    release_frozen: float = 0.0
    one_word_board: bool = False
    touched_limit: bool = False
    capacity_capped: bool = False


@runtime_checkable
class ExecutionGateway(Protocol):
    """执行通道协议（设计 5.3.2: 适配器/测试替换 BrokerSim 的边界）。"""

    def submit(self, order: Order) -> None: ...
    def cancel(self, ref: str) -> bool: ...
    def events(self) -> Iterator[OrderEvent]: ...


class BrokerSim:
    """事件驱动撮合器（真实成交语义）。"""

    def __init__(self, models: MatchingModels | None = None) -> None:
        self.models = models or MatchingModels()

    # ------------------------------------------------------------------
    def process_orders(
        self,
        order_book: OpenOrderBook,
        bar: MinimalBar,
        profile: InstrumentProfile,
        code: str | None = None,
    ) -> list[MatchOutcome]:
        """撮合该 bar 下全部可成交订单（顺序=受理顺序, 确定性 8.8）。

        code 非空时仅撮合该标的的订单（阶段⑤按标的逐 bar 驱动, 5.3.2）——
        多标的池下订单必须只对自身标的的 bar 撮合, 防串号成交。
        """
        outcomes: list[MatchOutcome] = []
        for order in order_book.eligible(bar.dt, code=code):
            outcomes.append(self._match(order, bar, profile, order_book))
        return outcomes

    def _match(
        self, order: Order, bar: MinimalBar, profile: InstrumentProfile, order_book: OpenOrderBook
    ) -> MatchOutcome:
        if bar.suspended:
            # 停牌: 当日不撮合（gtc 顺延; day 由引擎在收盘过期）
            return MatchOutcome(order=order, events=[], fill=None)

        # 一字板（open==high==low==close 且触停价）→ 整日不成交
        if bar.is_one_word_limit:
            ev = self._one_word_expire(order, bar.dt)
            return MatchOutcome(
                order=order,
                events=[] if ev is None else [ev],
                fill=None,
                release_frozen=order_book.release(order.order_id),
                one_word_board=True,
            )

        # 触板但盘中打开（收盘=涨停价但 OHLC 不全等）→ 正常撮合 + 提示
        touched = (bar.limit_up or bar.limit_down) and not bar.is_one_word_limit

        # 容量约束（LiquidityModel: 单笔 ≤ bar 成量 × 参与率）
        max_qty = self.models.liquidity.max_qty(order.remaining_qty, bar.volume)
        if max_qty <= 0 or bar.volume <= 0:
            # 无量 bar: 不成交（day 单收盘过期）
            return MatchOutcome(order=order, events=[], fill=None, touched_limit=touched)

        fill_qty = min(order.remaining_qty, max_qty)
        capped = fill_qty < order.remaining_qty

        base = self.models.fill.fill_price(bar, order.side)
        price = self.models.slippage.apply(base, order.side)
        fee = self.models.fee.compute(fill_qty, price, profile, order.side)

        # 成交时刻 = 订单最早可撮合时刻（next_open 09:30, g02/g13 时间链）, 兜底 bar.dt
        fill_time = order.eligible_fill_at or bar.dt
        fill = Fill(
            order_id=order.order_id,
            code=order.code,
            side=order.side,
            price=price,
            volume=fill_qty,
            fill_time=fill_time,
            commission=fee.commission,
            stamp_tax=fee.stamp_tax,
            transfer_fee=fee.transfer_fee,
            bar_volume=bar.volume,  # 容量证据（8.4.4）
            participation_rate=round(fill_qty / bar.volume, 6) if bar.volume else 0.0,
        )
        ev_type = OrderEventType.PARTIAL_FILL if capped else OrderEventType.FILL
        info: dict[str, Any] = {}
        if touched:
            info["touched_limit"] = True
        if capped:
            info["capacity_capped"] = True
            info["fill_ratio"] = round(fill_qty / order.remaining_qty, 6)
        ev = transition_order(
            order, ev_type, event_time=fill_time, qty=fill_qty, price=price, info_json=info or None
        )
        return MatchOutcome(
            order=order,
            events=[ev],
            fill=fill,
            release_frozen=order_book.release(order.order_id),
            touched_limit=touched,
            capacity_capped=capped,
        )

    @staticmethod
    def _one_word_expire(order: Order, when: datetime) -> OrderEvent | None:
        """一字板整日不成交 → day 单过期（gtc 顺延）; 记 one_word_limit 标记。"""
        if order.time_in_force is not TimeInForce.DAY:
            return None  # gtc: 顺延, 不产生事件
        return transition_order(
            order,
            OrderEventType.EXPIRE,
            event_time=when,
            info_json={"one_word_limit": True, "board": True},
        )
