# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:56:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D02/T-D02c：三格式嗅探/显式格式/GBK/平铺挂载（3.5/3.12）

"""T-D02/T-D02c：三格式嗅探 + 显式 format 跳过 + 未知格式报错列期望列 + GBK + khQuant 平铺挂载。"""

from __future__ import annotations

from datetime import datetime

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import Frequency, InstrumentType
from mtzquant.data.drivers.csv_driver import CsvSourceDriver

from .conftest import write_day_csv


def test_sniff_three_formats(golden_dir) -> None:  # type: ignore[no-untyped-def]
    assert CsvSourceDriver.sniff_format(["ts_code", "trade_date", "close"]) == "tushare"
    assert CsvSourceDriver.sniff_format(["time", "open", "paused", "factor"]) == "joinquant"
    assert CsvSourceDriver.sniff_format(["date", "open", "close", "volume"]) == "generic"
    # 无 time+paused 只有 time → 通用格式
    assert CsvSourceDriver.sniff_format(["time", "open", "close"]) == "generic"


def test_unknown_format_lists_expected_columns() -> None:
    with pytest.raises(MtzQuantError) as exc:
        CsvSourceDriver.sniff_format(["foo", "bar", "baz"])
    msg = str(exc.value)
    assert "无法识别" in msg
    assert "trade_date" in msg  # 期望列名列出（AI 友好）
    assert "paused" in msg


def test_explicit_format_skips_sniffing(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """format 显式指定 → 即使列名不可识别也不抛错（跳过嗅探, 3.5）。"""
    drv = make_driver(tmp_path, format="generic")
    assert drv._resolve_format(["foo", "bar"]) == "generic"  # 不嗅探直接返回


def test_load_tushare_and_range_cut(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "tushare")
    drv = make_driver(tmp_path)
    df = drv.load_kline("510300.SH", Frequency.D1, datetime(2024, 1, 3), datetime(2024, 1, 5))
    assert list(df.columns) == [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
    ]
    assert len(df) == 3  # 01-03/04/05（01-02、01-08 被区间裁剪）
    assert str(df["trade_date"].iloc[0]) == "2024-01-03 00:00:00+08:00"


def test_joinquant_parsed_date_column(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "joinquant")
    drv = make_driver(tmp_path)
    df = drv.load_kline("510300.SH", Frequency.D1, datetime(2024, 1, 2), datetime(2024, 1, 8))
    assert len(df) == 5
    assert "paused" in df.columns  # 原始列保留, 交归一


def test_gbk_encoding_readable(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """GBK 编码样本可读（3.5 编码可配, 默认 utf-8-sig）。"""
    src = write_day_csv(tmp_path, "510300.SH", "generic")
    gbk_text = src.read_text(encoding="utf-8")
    src.write_text(gbk_text, encoding="gbk")
    drv = make_driver(tmp_path, encoding="gbk")
    df = drv.load_kline("510300.SH", Frequency.D1, datetime(2024, 1, 2), datetime(2024, 1, 8))
    assert len(df) == 5


def test_missing_file_reports_hint(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    drv = make_driver(tmp_path)
    with pytest.raises(MtzQuantError) as exc:
        drv.load_kline("510300.SH", Frequency.D1, datetime(2024, 1, 2), datetime(2024, 1, 8))
    assert "不存在" in str(exc.value)


def test_minute_frequency_not_implemented(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "tushare")
    drv = make_driver(tmp_path)
    with pytest.raises(MtzQuantError):
        drv.load_kline("510300.SH", Frequency.M1, datetime(2024, 1, 2), datetime(2024, 1, 8))


def test_khquant_flat_mount_group_by_type(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """T-D02c: khQuant 目录 data/etf/day/{code}.csv 直接挂载（kline_day_dir={type}/day）。"""
    kh_root = tmp_path / "khquant"
    dst = kh_root / "etf" / "day"
    dst.mkdir(parents=True)
    src = write_day_csv(tmp_path, "510300.SH", "tushare")
    (dst / "510300.SH.csv").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    drv = make_driver(kh_root, kline_day_dir="{type}/day")
    df = drv.load_kline("510300.SH", Frequency.D1, datetime(2024, 1, 2), datetime(2024, 1, 8))
    assert len(df) == 5


def test_flat_layout_group_by_type_false(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """group_by_type=false → 平铺 kline/day/{code}.csv（设计 3.12 退化兼容）。"""
    flat = tmp_path / "kline" / "day"
    flat.mkdir(parents=True)
    src = write_day_csv(tmp_path, "510300.SH", "generic")
    (flat / "510300.SH.csv").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    drv = make_driver(tmp_path, group_by_type=False)
    assert drv.kline_dir(InstrumentType.ETF, Frequency.D1) == tmp_path / "kline" / "day"
    df = drv.load_kline("510300.SH", Frequency.D1, datetime(2024, 1, 2), datetime(2024, 1, 8))
    assert len(df) == 5


def test_list_instruments_scans_dir(make_driver, tmp_path) -> None:  # type: ignore[no-untyped-def]
    write_day_csv(tmp_path, "510300.SH", "tushare")
    stock_day = tmp_path / "kline" / "stock" / "day"
    stock_day.mkdir(parents=True)
    (stock_day / "600000.SH.csv").write_text(
        "date,open,high,low,close,volume,amount\n2024-01-02,10.0,10.5,9.9,10.2,1000000,10200000\n",
        encoding="utf-8",
    )
    drv = make_driver(tmp_path)
    refs = drv.list_instruments()
    codes = sorted(r.code for r in refs)
    assert "510300.SH" in codes and "600000.SH" in codes
