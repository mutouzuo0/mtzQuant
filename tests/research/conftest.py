# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:20:00
# @update_time        : 2026/08/17 00:20:00
# @description : tests/research 共享 fixture：合成行情 → Provider + Settings（研究层共用）

"""研究层测试 fixture（T-V01..V06 共用）——合成行情 Provider 与事件引擎 settings。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from mtzquant.config import (
    DatabaseSettings,
    DataSettings,
    EngineSettings,
    FeeSettings,
    LocalCsvSettings,
    Settings,
)
from mtzquant.data.cache import DataCache
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.drivers.csv_driver import CsvSourceDriver
from mtzquant.data.fundamentals import FundamentalsStore
from mtzquant.data.provider import MarketDataProvider
from tests.fixtures.synth import trade_days, write_etf_csv

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def make_env(tmp_path: Path):
    """构造 (provider, settings, tmp_path); provider 与事件引擎 settings 共用同一数据根。"""

    def _make(codes: tuple[str, ...] = ("510300.SH", "510500.SH"), n: int = 120, **kw) -> tuple:
        for code in codes:
            write_etf_csv(tmp_path, code, n=n, **kw)
        days = trade_days(date(2020, 1, 2), n)
        lcs = LocalCsvSettings(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")
        drv = CsvSourceDriver(lcs)
        cache = DataCache(tmp_path / ".cache", enabled=True)
        provider = MarketDataProvider(
            drv,
            TradeCalendar.from_dates(days),
            cache=cache,
            fundamentals=FundamentalsStore(tmp_path),
        )
        provider.preload(list(codes), datetime(1970, 1, 1), datetime(2100, 1, 1))
        settings = Settings(
            data=DataSettings(local_csv=lcs),
            database=DatabaseSettings(url=f"sqlite:///{tmp_path}/t.db"),
            engine=EngineSettings(
                default_fees=FeeSettings(
                    commission_rate=0.0001,
                    min_commission=5.0,
                    stamp_tax_rate=0.0,
                    transfer_fee_rate=0.0,
                ),
                max_participation=0.25,
            ),
            research={"warmup_default": 60, "bps_default": 10.0},
            optimizer={"max_workers": 2, "mode_defaults": ("grid", "random")},
        )
        return provider, settings, tmp_path

    return _make
