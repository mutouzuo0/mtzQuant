# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:20:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D06：Provider PIT + bar_at 二分交叉验证（3.7/3.13）

"""T-D06：Provider PIT 强制时点 + numpy 快路径一致性 + 预加载三模式（设计 3.7/3.8/3.13）。"""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import Frequency
from mtzquant.data.cache import DataCache
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.drivers.csv_driver import CsvSourceDriver
from mtzquant.data.normalizer import DataNormalizer
from mtzquant.data.provider import MarketDataProvider

from .conftest import asof, day_bar_ts, write_day_csv


def _make_provider(tmp_path, preload_mode: str = "window") -> MarketDataProvider:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "generic")  # 写源文件（副作用）
    drv = CsvSourceDriver(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")
    raw = drv.load_kline(
        "510300.SH",
        Frequency.D1,
        pd.Timestamp("2024-01-01", tz="Asia/Shanghai"),
        pd.Timestamp("2024-12-31", tz="Asia/Shanghai"),
    )
    norm = DataNormalizer().normalize(raw, "510300.SH")
    cal = TradeCalendar.from_dates(pd.to_datetime(norm.index.date).tolist())
    return MarketDataProvider(drv, cal, preload_mode=preload_mode)


def test_history_as_of_excludes_future_bar(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    # as_of=01-04 09:30（盘前）→ 当日 15:00 bar 不可见（防未来数据, 3.13）
    h = prov.history("510300.SH", ["close"], 5, as_of=asof(2024, 1, 4, 9, 30))
    assert len(h) == 2  # 01-02, 01-03
    assert list(h.index.date.astype(str)) == ["2024-01-02", "2024-01-03"]


def test_include_today_default_false(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    default = prov.history("510300.SH", ["close"], 5, as_of=asof(2024, 1, 4, 9, 30))
    assert len(default) == 2  # 不含当日
    with_today = prov.history(
        "510300.SH", ["close"], 5, as_of=asof(2024, 1, 4, 9, 30), include_today=True
    )
    assert len(with_today) == 3  # 含当日 01-04


def test_history_at_close_sees_today(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    h = prov.history("510300.SH", ["close"], 5, as_of=asof(2024, 1, 4, 15, 0))
    assert len(h) == 3  # event_time<=as_of: 01-04 15:00 bar 可见


def test_history_last_n_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    h = prov.history("510300.SH", ["close"], 2, as_of=asof(2024, 1, 8, 15, 0))
    assert len(h) == 2
    assert h.index[-1].date().isoformat() == "2024-01-08"
    assert h.index[0].date().isoformat() == "2024-01-05"


def test_knowledge_time_narrows_visibility(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    # as_of 到 01-08, 但 knowledge_time=01-04 15:00 → 只有 ≤01-04 可见
    h = prov.history(
        "510300.SH",
        ["close"],
        10,
        as_of=asof(2024, 1, 8, 15, 0),
        knowledge_time=asof(2024, 1, 4, 15, 0),
    )
    assert len(h) == 3


def test_bar_at_exact_binary_search(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    b = prov.bar_at("510300.SH", day_bar_ts(2024, 1, 3))
    assert b is not None
    assert b.dt == int(day_bar_ts(2024, 1, 3).timestamp() * 1000)
    assert abs(b.close - 10.4) < 1e-10
    assert b.suspended == 0


def test_bar_at_missing_returns_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    assert prov.bar_at("510300.SH", day_bar_ts(2024, 1, 6)) is None  # 周六无 bar
    assert prov.bar_at("510300.SH", day_bar_ts(2024, 1, 1)) is None  # 更早无 bar


def test_numpy_fastpath_matches_dataframe(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """bar_at 二分结果与 DataFrame 逐行查询一致（T-D06 随机交叉验证）。"""
    rng = random.Random(42)  # 确定性纪律 8.8
    prov = _make_provider(tmp_path)
    arr = prov.bar_array("510300.SH")
    # 全部 bar 精确比对
    for row in arr:
        b = prov.bar_at(
            "510300.SH",
            pd.Timestamp(int(row["dt"]), unit="ms", tz="UTC").tz_convert("Asia/Shanghai"),
        )
        assert b is not None
        assert abs(b.close - float(row["close"])) < 1e-10
        assert abs(b.open - float(row["open"])) < 1e-10
        assert int(b.dt) == int(row["dt"])
    # 随机 1000 个候选时点（含存在/不存在）→ 与 searchsorted 手算一致
    base = int(arr["dt"].min())
    top = int(arr["dt"].max())
    for _ in range(1000):
        target_ms = rng.randint(base - 86_400_000, top + 86_400_000)
        dt = pd.Timestamp(target_ms, unit="ms", tz="UTC").tz_convert("Asia/Shanghai")
        got = prov.bar_at("510300.SH", dt)
        idx = int(np.searchsorted(arr["dt"], target_ms, side="left"))
        expect = (
            int(arr["dt"][idx]) if (idx < arr.size and int(arr["dt"][idx]) == target_ms) else None
        )
        assert (got.dt if got else None) == expect


def test_preload_window_loads_all(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path, preload_mode="window")
    prov.preload(["510300.SH"], asof(2024, 1, 2), asof(2024, 1, 8))
    assert "510300.SH" in prov._arrays  # 已入内存


def test_preload_lazy_noop_until_access(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path, preload_mode="lazy")
    prov.preload(["510300.SH"], asof(2024, 1, 2), asof(2024, 1, 8))
    assert "510300.SH" not in prov._arrays  # lazy 不预载
    prov.history("510300.SH", ["close"], 1, as_of=asof(2024, 1, 8, 15, 0))  # 首次访问加载
    assert "510300.SH" in prov._arrays


def test_provider_with_cache(tmp_path) -> None:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "generic")
    drv = CsvSourceDriver(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")
    cal = TradeCalendar.from_dates(
        [
            pd.Timestamp("2024-01-02").date(),
            pd.Timestamp("2024-01-03").date(),
            pd.Timestamp("2024-01-04").date(),
            pd.Timestamp("2024-01-05").date(),
            pd.Timestamp("2024-01-08").date(),
        ]
    )
    cache = DataCache(tmp_path / ".cache", enabled=True)
    prov = MarketDataProvider(drv, cal, cache=cache)
    h = prov.history("510300.SH", ["close"], 3, as_of=asof(2024, 1, 8, 15, 0))
    assert len(h) == 3
    assert cache.stats.source_loads == 1  # 经缓存加载


def test_to_frame_to_numpy_m3(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """M3-R4 转正: to_frame/to_numpy 批量接口（设计 3.8）——宽表与矩阵直出。"""
    prov = _make_provider(tmp_path)
    frame = prov.to_frame(["510300.SH"], ["close", "volume"], asof(2024, 1, 2), asof(2024, 1, 8))
    assert isinstance(frame.columns, pd.MultiIndex)
    assert list(frame.columns.names) == ["code", "field"]
    assert len(frame) >= 5  # 多标的日期并集
    arr = prov.to_numpy(["510300.SH"], ["close"], asof(2024, 1, 2), asof(2024, 1, 8))
    assert arr.ndim == 3 and arr.shape[1:] == (1, 1)
    # fields 为空 → 结构化报错
    with pytest.raises(MtzQuantError):
        prov.to_frame(["510300.SH"], [], asof(2024, 1, 2), asof(2024, 1, 8))


def test_history_n_must_be_positive(tmp_path) -> None:  # type: ignore[no-untyped-def]
    prov = _make_provider(tmp_path)
    with pytest.raises(MtzQuantError):
        prov.history("510300.SH", ["close"], 0, as_of=asof(2024, 1, 8, 15, 0))
