# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:02:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D03：归一黄金比对 + 停牌/缺失值/涨跌停（设计 3.3）

"""T-D03：三格式同源数据归一后与手算期望表逐列全等（TOL=1e-10）；含停牌与缺失值规则。"""

from __future__ import annotations

import pandas as pd
import pytest

from mtzquant.core.types import Frequency
from mtzquant.data.normalizer import DataNormalizer, dt_index_to_ms

from .conftest import FILL_TOL, load_expected_normalized, write_day_csv

FORMATS = ("tushare", "joinquant", "generic")


def _normalize(drv, code: str, *, limit=None) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    raw = drv.load_kline(
        code,
        Frequency.D1,
        pd.Timestamp("2024-01-01", tz="Asia/Shanghai"),
        pd.Timestamp("2024-12-31", tz="Asia/Shanghai"),
    )
    return DataNormalizer().normalize(raw, code, fmt="auto", limit=limit)


def _frames_almost_equal(actual: pd.DataFrame, expected: pd.DataFrame) -> None:
    assert list(actual.columns) == list(expected.columns)
    assert (dt_index_to_ms(actual.index) == dt_index_to_ms(expected.index)).all()
    for col in actual.columns:
        a, e = actual[col].to_numpy(dtype="float64"), expected[col].to_numpy(dtype="float64")
        mask = ~pd.isna(e)
        assert (pd.isna(a) == pd.isna(e)).all(), f"NaN 位置不一致: {col}"
        assert pd.Series(a[mask]).sub(pd.Series(e[mask])).abs().max() <= FILL_TOL, (
            f"数值超差: {col}"
        )


def test_golden_normalized_per_format(make_driver, tmp_path, etf_limit) -> None:  # type: ignore[no-untyped-def]
    """三格式各自归一结果与手算期望表逐列全等（T-D03）。"""
    expected = load_expected_normalized()
    for fmt in FORMATS:
        write_day_csv(tmp_path, "510300.SH", fmt)
        drv = make_driver(tmp_path)
        actual = _normalize(drv, "510300.SH", limit=etf_limit)
        _frames_almost_equal(actual, expected)


def test_three_formats_identical(make_driver, tmp_path, etf_limit) -> None:  # type: ignore[no-untyped-def]
    """同源数据三格式归一输出彼此全等（跨格式一致性, 3.5/3.3）。"""
    results = []
    for fmt in FORMATS:
        write_day_csv(tmp_path, "510300.SH", fmt)
        results.append(_normalize(make_driver(tmp_path), "510300.SH", limit=etf_limit))
    for other in results[1:]:
        _frames_almost_equal(results[0], other)


def test_daily_bar_timestamp_is_15_00(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """日线时间戳 = 交易日 15:00 收盘时刻（设计 3.3 防未来数据）。"""
    write_day_csv(tmp_path, "510300.SH", "generic")
    norm = _normalize(make_driver(tmp_path), "510300.SH")
    assert norm.index[0].hour == 15 and norm.index[0].minute == 0
    assert norm.index[0].tz is not None  # tz-aware（Asia/Shanghai）


def test_no_limit_provider_yields_nan_columns(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "generic")
    norm = _normalize(make_driver(tmp_path), "510300.SH")
    assert norm["limit_up"].isna().all()
    assert norm["limit_down"].isna().all()


def test_limit_prices_from_profile(make_driver, tmp_path, etf_limit) -> None:  # type: ignore[no-untyped-def]
    """涨跌停价由注入档案 ±10% 计算（设计 5.4）。"""
    write_day_csv(tmp_path, "510300.SH", "generic")
    norm = _normalize(make_driver(tmp_path), "510300.SH", limit=etf_limit)
    # 第二日: 昨收 10.2 → 涨停 11.22 / 跌停 9.18
    assert abs(norm["limit_up"].iloc[1] - 11.22) <= FILL_TOL
    assert abs(norm["limit_down"].iloc[1] - 9.18) <= FILL_TOL
    # 首日无昨收 → 涨跌停 NaN
    assert pd.isna(norm["limit_up"].iloc[0])


def test_zero_volume_marks_suspended(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """volume==0 → suspended=1（设计 3.3 缺失值规则）。"""
    day_dir = tmp_path / "kline" / "etf" / "day"
    day_dir.mkdir(parents=True)
    (day_dir / "510300.SH.csv").write_text(
        "date,open,high,low,close,volume,amount\n"
        "2024-01-02,10.0,10.5,9.9,10.2,1000000,10200000\n"
        "2024-01-03,0,0,0,0,0,0\n"
        "2024-01-04,10.2,10.6,10.1,10.4,1100000,11300000\n",
        encoding="utf-8",
    )
    norm = _normalize(make_driver(tmp_path), "510300.SH")
    assert norm["suspended"].iloc[1] == 1
    assert norm["suspended"].iloc[[0, 2]].eq(0).all()
    # 停牌日价格沿用前收（缺失规则）
    assert abs(norm["close"].iloc[1] - 10.2) <= FILL_TOL


def test_paused_flag_from_joinquant(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """聚宽 paused=1 显式标记停牌（即使有量）。"""
    day_dir = tmp_path / "kline" / "etf" / "day"
    day_dir.mkdir(parents=True)
    (day_dir / "510300.SH.csv").write_text(
        "time,open,high,low,close,volume,money,paused,factor\n"
        "2024-01-02,10.0,10.5,9.9,10.2,1000000,10200000,0,1.0\n"
        "2024-01-03,10.2,10.6,10.1,10.4,999999,11300000,1,1.0\n",
        encoding="utf-8",
    )
    norm = _normalize(make_driver(tmp_path), "510300.SH")
    assert norm["suspended"].iloc[1] == 1


def test_duplicate_dt_keeps_latest(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """重复日期行保留最新一条（去重纪律 3.9/8.8）。"""
    day_dir = tmp_path / "kline" / "etf" / "day"
    day_dir.mkdir(parents=True)
    (day_dir / "510300.SH.csv").write_text(
        "date,open,high,low,close,volume,amount\n"
        "2024-01-02,10.0,10.5,9.9,10.2,1000000,10200000\n"
        "2024-01-02,10.1,10.6,10.0,10.3,2000000,20500000\n",
        encoding="utf-8",
    )
    norm = _normalize(make_driver(tmp_path), "510300.SH")
    assert len(norm) == 1
    assert abs(norm["close"].iloc[0] - 10.3) <= FILL_TOL


def test_empty_input_raises(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from mtzquant.core.errors import MtzQuantError

    with pytest.raises(MtzQuantError):
        DataNormalizer().normalize(None, "510300.SH")  # type: ignore[arg-type]
