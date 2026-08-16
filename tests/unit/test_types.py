# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:09:30
# @description : T-U02：核心类型/枚举/四时间模型（设计 3.3 / 3.13）

"""T-U02：核心类型/枚举/四时间模型（设计 3.3 / 3.13）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import (
    DAILY_BAR_TIME,
    KLINE_COLUMNS,
    AdjustMode,
    DataType,
    Frequency,
    InstrumentType,
    TimeModel,
)


def test_frequency_v1_values() -> None:
    assert Frequency.D1.value == "1d"
    assert Frequency.M1.value == "1m"
    assert Frequency.M5.value == "5m"


def test_instrument_type_v1_and_reserved() -> None:
    assert InstrumentType.STOCK.value == "stock"
    assert InstrumentType.ETF.value == "etf"
    # 预留品种档案位（设计 3.3/5.4）
    assert InstrumentType.FUTURES.value == "futures"
    assert InstrumentType.OPTION.value == "option"


def test_data_type_orthogonal_dimensions() -> None:
    assert DataType.KLINE.value == "kline"
    # tick/trade/order/depth 预留（设计 3.3）
    reserved = {
        DataType.TICK.value,
        DataType.TRADE.value,
        DataType.ORDER.value,
        DataType.DEPTH.value,
    }
    assert reserved == {"tick", "trade", "order", "depth"}


def test_adjust_mode() -> None:
    assert AdjustMode.NONE.value == "none"
    assert AdjustMode.FORWARD.value == "forward"
    assert AdjustMode.BACKWARD.value == "backward"


def test_kline_column_contract_exact() -> None:
    """内部 K 线列契约（设计 3.3）——列序不允许漂移。"""
    assert KLINE_COLUMNS == (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pre_close",
        "suspended",
        "limit_up",
        "limit_down",
    )


def test_daily_bar_time() -> None:
    """日线时间戳 = 交易日 15:00 收盘时刻（设计 3.3 防未来数据约定）。"""
    assert (DAILY_BAR_TIME.hour, DAILY_BAR_TIME.minute) == (15, 0)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_time_model_valid_and_defaults() -> None:
    tm = TimeModel(event_time=_dt("2023-09-30T00:00"), published_at=_dt("2023-10-28T00:00"))
    assert tm.available_at == tm.published_at  # 缺省 = published_at
    assert tm.ingested_at is None


def test_time_model_market_data_event_equals_published() -> None:
    """行情类: event_time == published_at（盘中即时产生，设计 3.13）。"""
    t = _dt("2024-01-02T15:00")
    tm = TimeModel(event_time=t, published_at=t)
    assert tm.available_at == t


def test_time_model_rejects_published_before_event() -> None:
    with pytest.raises(MtzQuantError):
        TimeModel(event_time=_dt("2023-09-30"), published_at=_dt("2023-08-01"))


def test_time_model_rejects_available_before_published() -> None:
    with pytest.raises(MtzQuantError):
        TimeModel(
            event_time=_dt("2023-09-30"),
            published_at=_dt("2023-10-28"),
            available_at=_dt("2023-10-01"),
        )
