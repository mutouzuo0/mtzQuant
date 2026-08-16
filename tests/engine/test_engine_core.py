# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 02:42:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-E01~E04/E10：orderbook/broker/engine/resultstore 组件测试（设计 5.3/5.1/5.6）

"""T-E01~E04 / T-E10：引擎内核组件测试。

- T-E01 OpenOrderBook: 受理冻结/eligible/收盘过期/gtc 跨日
- T-E02 BrokerSim: 一字板过期 + info_json / 停牌 no-op / 触板打开成交
- T-E03 BrokerSim: 容量截断部分成交
- T-E04 UnifiedBacktestEngine: 十阶段顺序
- T-E10 ResultStore: event_seq 单调 + 批量 flush
"""

from __future__ import annotations

from datetime import datetime

from mtzquant.core.types import InstrumentType
from mtzquant.engine.broker import BrokerSim, MatchingModels
from mtzquant.engine.instrument import Board, FeeParams, InstrumentProfile, LimitRule
from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.models.fee import FeeModel
from mtzquant.engine.models.fill_price import FillModel, PriceBasis
from mtzquant.engine.models.liquidity import LiquidityModel
from mtzquant.engine.models.slippage import SlippageModel
from mtzquant.engine.orderbook import OpenOrderBook
from mtzquant.engine.orders import (
    Order,
    OrderDirection,
    OrderEventType,
    OrderStatus,
    OrderStyle,
    TimeInForce,
)
from mtzquant.engine.results import FlushPolicy, ResultStore

RUN = "r-test"


def _order(
    oid: str,
    *,
    side: OrderDirection = OrderDirection.BUY,
    qty: float = 1000,
    eligible: datetime | None = None,
    tif: TimeInForce = TimeInForce.DAY,
) -> Order:
    return Order(
        order_id=oid,
        run_id=RUN,
        code="510300.SH",
        side=side,
        style=OrderStyle.QUANTITY,
        qty=qty,
        order_api="order",
        submitted_at=datetime(2026, 1, 2, 15, 0),
        eligible_fill_at=eligible or datetime(2026, 1, 3, 9, 30),
        time_in_force=tif,
    )


def _bar(
    *,
    open_: float = 10.0,
    high: float | None = None,
    low: float | None = None,
    close: float = 10.0,
    volume: float = 100_000,
    limit_up: bool = False,
    limit_down: bool = False,
    suspended: bool = False,
    dt: datetime | None = None,
) -> MinimalBar:
    return MinimalBar(
        dt=dt or datetime(2026, 1, 3, 9, 30),
        open=open_,
        high=high if high is not None else max(open_, close),
        low=low if low is not None else min(open_, close),
        close=close,
        volume=volume,
        pre_close=10.0,
        suspended=suspended,
        limit_up=limit_up,
        limit_down=limit_down,
    )


def _profile() -> InstrumentProfile:
    return InstrumentProfile(
        code="510300.SH",
        instrument_type=InstrumentType.ETF,
        limit_rule=LimitRule(board=Board.MAIN),
        lot_size=100,
        t_plus=1,
        fee=FeeParams(
            commission_rate=0.0001, commission_min=5.0, stamp_tax_rate=0.0, transfer_fee_rate=0.0
        ),
    )


def _sim(participation: float = 0.25) -> BrokerSim:
    return BrokerSim(
        models=MatchingModels(
            fill=FillModel(basis=PriceBasis.NEXT_OPEN, half_spread=0.0),
            slippage=SlippageModel(ratio=0.0),
            fee=FeeModel(),
            liquidity=LiquidityModel(max_participation=participation),
        )
    )


# ------------------------------------------------------------------
# T-E01 OrderBook
# ------------------------------------------------------------------
def test_accept_freezes_cash_and_rejects_insufficient() -> None:
    book = OpenOrderBook()
    ev = book.accept(_order("o1"), available_cash=1_000_000.0, ref_price=10.0)
    assert ev is not None and ev.event_type is OrderEventType.ACCEPTED
    assert book.frozen_of("o1") > 0  # 买入冻结（含佣预估）
    # 现金不足 → REJECTED, 不进账本
    big = _order("o2", qty=500_000)
    ev2 = book.accept(big, available_cash=1_000.0, ref_price=10.0)
    assert ev2 is not None and ev2.event_type is OrderEventType.REJECTED
    assert big.status is OrderStatus.REJECTED
    assert "o2" not in book.active_orders()


def test_eligible_filters_by_fill_time() -> None:
    book = OpenOrderBook()
    book.accept(
        _order("o1", eligible=datetime(2026, 1, 3, 9, 30)), available_cash=1e6, ref_price=10.0
    )
    book.accept(
        _order("o2", eligible=datetime(2026, 1, 5, 9, 30)), available_cash=1e6, ref_price=10.0
    )
    eligible = [o.order_id for o in book.eligible(datetime(2026, 1, 3, 9, 30))]
    assert eligible == ["o1"]  # 未到时可撮合时点的 o2 不入选（T-E01）


def test_day_expire_and_gtc_cross_day() -> None:
    book = OpenOrderBook()
    book.accept(
        _order("day1", eligible=datetime(2026, 1, 3, 9, 30)), available_cash=1e6, ref_price=10.0
    )
    book.accept(
        _order("gtc1", eligible=datetime(2026, 1, 3, 9, 30), tif=TimeInForce.GTC),
        available_cash=1e6,
        ref_price=10.0,
    )
    evs = book.expire_day_orders(when=datetime(2026, 1, 3, 15, 0))
    assert [e.order_id for e in evs] == ["day1"]
    assert [o.order_id for o in book.active_orders()] == ["gtc1"]  # gtc 跨日保留


# ------------------------------------------------------------------
# T-E02 BrokerSim limit board / suspended / touched
# ------------------------------------------------------------------
def test_one_word_board_expires_day_order() -> None:
    book = OpenOrderBook()
    o = _order("o1")
    book.accept(o, available_cash=1e6, ref_price=10.0)
    # 一字涨停: open==high==low==close 且触停价
    bar = _bar(open_=10.0, close=10.0, limit_up=True)
    outcomes = _sim().process_orders(book, bar, _profile())
    assert len(outcomes) == 1
    oc = outcomes[0]
    assert oc.one_word_board is True
    assert oc.events[0].event_type is OrderEventType.EXPIRE
    assert oc.events[0].info_json.get("one_word_limit") is True  # T-E02 标记
    assert o.status is OrderStatus.EXPIRED
    assert book.frozen_of("o1") == 0.0  # 冻结已释放


def test_suspended_noop_keeps_pending() -> None:
    book = OpenOrderBook()
    o = _order("o1")
    book.accept(o, available_cash=1e6, ref_price=10.0)
    oc = _sim().process_orders(book, _bar(suspended=True), _profile())[0]
    assert oc.fill is None and oc.events == []  # 停牌 no-op
    assert o.status is OrderStatus.PENDING  # 顺延（收盘由 orderbook 过期）


def test_touched_limit_open_still_fills() -> None:
    book = OpenOrderBook()
    o = _order("o1")
    book.accept(o, available_cash=1e6, ref_price=10.0)
    # 触板但盘中打开（OHLC 不全等, close==涨停价）
    oc = _sim().process_orders(
        book, _bar(open_=10.0, high=11.0, low=9.9, close=11.0, limit_up=True), _profile()
    )[0]
    assert oc.fill is not None
    assert oc.touched_limit is True
    assert o.status is OrderStatus.FILLED


# ------------------------------------------------------------------
# T-E03 liquidity capacity partial fill
# ------------------------------------------------------------------
def test_capacity_caps_partial_fill() -> None:
    book = OpenOrderBook()
    o = _order("o1", qty=50_000)
    book.accept(o, available_cash=1e6, ref_price=10.0)
    sim = _sim(participation=0.25)  # 25% × volume 100_000 = 25_000
    oc = sim.process_orders(book, _bar(volume=100_000), _profile())[0]
    assert oc.capacity_capped is True
    assert oc.fill is not None and oc.fill.volume == 25_000
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert oc.events[0].info_json.get("capacity_capped") is True  # T-E03


# ------------------------------------------------------------------
# T-E04 ten-stage engine order
# ------------------------------------------------------------------
def test_ten_stage_sequence() -> None:
    from mtzquant.engine.engine import UnifiedBacktestEngine

    engine = UnifiedBacktestEngine(_DummySession())
    engine.run()
    order = engine.trace.order
    # 每交易日完整命中 ①-⑩
    assert order.count("session_start") >= 2
    assert order[:10] == [
        "session_start",
        "corp_open",
        "t1_release",
        "before_open",
        "open_match",
        "strategy",
        "intraday_none",
        "on_close",
        "mark_to_market",
        "dividend_settle",
    ]


class _DummySession:
    """T-E04 桩: 驱动引擎跑 3 个交易日（无数据, 只验证阶段顺序）。"""

    def __init__(self) -> None:
        self._days = 3

    def trading_days(self) -> list[datetime]:
        return [datetime(2026, 1, 2 + i, 15, 0) for i in range(self._days)]

    def bar_at(self, code, dt):  # type: ignore[no-untyped-def]
        return None

    def apply_open_actions(self, dt) -> list[str]:  # type: ignore[no-untyped-def]
        return []

    def release_t1(self) -> None:
        pass

    def run_before_open(self, dt) -> None:  # type: ignore[no-untyped-def]
        pass

    def run_strategy(self, dt):  # type: ignore[no-untyped-def]
        return []

    def run_on_close(self, dt) -> None:  # type: ignore[no-untyped-def]
        pass

    def mark_to_market(self, dt):  # type: ignore[no-untyped-def]
        return None  # T-E04 桩: 估值占位（引擎仅追加/计数）

    def settle_dividends(self) -> None:
        pass

    def orders_to_book(self, orders) -> None:  # type: ignore[no-untyped-def]
        pass

    def profile_of(self, code):  # type: ignore[no-untyped-def]
        return _profile()

    def universe(self) -> list[str]:
        return []

    def available_cash(self) -> float:
        return 1e6

    def record_event(self, ev) -> None:  # type: ignore[no-untyped-def]
        pass

    def account_apply_fill(self, fill) -> None:  # type: ignore[no-untyped-def]
        pass

    def finalize(self):  # type: ignore[no-untyped-def]
        return None


# ------------------------------------------------------------------
# T-E10 ResultStore event_seq + buffering
# ------------------------------------------------------------------
def test_resultstore_event_seq_and_flush() -> None:
    flushed: list = []
    store = ResultStore(
        FlushPolicy(batch_size=2, flush_interval_ms=500, buffer_max_rows=50_000),
        flush_hook=flushed.append,
    )
    assert store.emit("log", {"m": "a"}) == 1
    assert store.emit("log", {"m": "b"}) == 2  # 单调递增（8.8）
    assert store.buffered_count == 0  # 达 batch_size=2 自动 flush
    assert len(flushed) == 1 and len(flushed[0]) == 2  # 钩子收到整批
    assert all(r.committed for r in flushed[0])
    assert store.next_seq() == 3
    store.emit("fill", {"x": 1})
    recs = store.finalize()
    assert len(recs) == 3
    assert recs[-1].kind == "fill"
