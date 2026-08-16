# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:52:00
# @update_time        : 2026/08/16 21:59:08
# @description : tests/data 共享 fixture：golden 目录/驱动工厂/ETF 档案

"""tests/data 共享 fixture（T-D01..T-D07 共用）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from mtzquant.core.types import InstrumentType
from mtzquant.data.drivers.csv_driver import CsvSourceDriver
from mtzquant.engine.instrument import etf_profile

GOLDEN_DIR = Path(__file__).resolve().parent / "golden_files"
FILL_TOL = 1e-10


@pytest.fixture()
def golden_dir() -> Path:
    return GOLDEN_DIR


@pytest.fixture()
def etf_limit() -> object:
    """涨跌停价提供者（ETF 档案 ±10%, 设计 5.4）——测试侧注入, 数据层不 import engine。"""
    return etf_profile("510300.SH")


@pytest.fixture()
def make_driver(tmp_path: Path):
    """构造 CsvSourceDriver 的工厂（默认 root=tmp, 可覆盖路径/编码/格式等）。"""

    def _make(root: Path | str | None = None, **overrides) -> CsvSourceDriver:
        base = {"root_path": str(root or tmp_path), "kline_day_dir": "kline/{type}/day"}
        base.update(overrides)
        return CsvSourceDriver(**base)

    return _make


def write_day_csv(
    root: Path, code: str, fmt: str, *, instrument_type: InstrumentType = InstrumentType.ETF
) -> Path:
    """把 golden 样本按指定目录布局写入 root（返回文件路径）。

    布局: group_by_type=true → {root}/kline/{type}/day/{code}.csv
    golden 样本文件名不含交易所后缀（tushare_510300.csv）。
    """
    stem = code.split(".")[0]
    src = GOLDEN_DIR / f"{fmt}_{stem}.csv"
    dst = root / "kline" / instrument_type.value / "day" / f"{code}.csv"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def load_expected_normalized() -> pd.DataFrame:
    """读取手算期望归一表（dt 毫秒列 → UTC+8 索引）。

    dt 为 UTC 绝对毫秒 → utc=True 解析后再 tz_convert 到上海墙钟（8.8 确定性）。
    """
    df = pd.read_csv(GOLDEN_DIR / "normalized_510300.csv")
    df.index = pd.to_datetime(df.pop("dt"), unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
    return df


def asof(y: int, m: int, d: int, hh: int = 15, mm: int = 0) -> datetime:
    """构造 UTC+8 时点（15:00 默认收盘时刻）。"""
    return datetime(y, m, d, hh, mm, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))


def day_bar_ts(y: int, m: int, d: int) -> pd.Timestamp:
    """某交易日 15:00 的 bar 时间戳（与归一输出索引同构）。"""
    return pd.Timestamp(f"{y:04d}-{m:02d}-{d:02d} 15:00", tz="Asia/Shanghai")
