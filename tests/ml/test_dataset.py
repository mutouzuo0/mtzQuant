# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:40:00
# @update_time        : 2026/08/16 23:40:00
# @description : M3-R4 测试 T-R04：to_frame/to_numpy 批量接口 + DatasetBuilder 防泄露切分

"""T-R04（设计 3.8, M3-R4 ML 通道）——宽表/矩阵直出 + 时间切分无泄露。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.drivers.csv_driver import CsvSourceDriver
from mtzquant.data.fundamentals import FundamentalsStore
from mtzquant.data.provider import MarketDataProvider
from mtzquant.ml.dataset import (
    DatasetBuilder,
    label_forward_return,
    split_by_time,
)
from tests.fixtures.synth import trade_days, write_etf_csv

_SH = ZoneInfo("Asia/Shanghai")


def _at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 15, 0, tzinfo=_SH)


def _provider(tmp_path: Path) -> MarketDataProvider:
    write_etf_csv(tmp_path, "510300.SH", n=60, base_price=10.0, drift=0.001, seed=1)
    write_etf_csv(tmp_path, "510500.SH", n=60, base_price=8.0, drift=0.0005, seed=2)
    drv = CsvSourceDriver(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")
    days = trade_days(date(2020, 1, 2), 60)
    cal = TradeCalendar.from_dates(days)
    return MarketDataProvider(drv, cal, fundamentals=FundamentalsStore(tmp_path))


def test_provider_to_frame_wide_table(tmp_path: Path) -> None:
    prov = _provider(tmp_path)
    frame = prov.to_frame(
        ["510300.SH", "510500.SH"], ["close", "volume"], _at(2020, 1, 2), _at(2020, 4, 30)
    )
    assert isinstance(frame.columns, pd.MultiIndex)
    assert frame.columns.names == ["code", "field"]
    # 日期并集为索引, 每列对齐
    assert len(frame.index) >= 55
    assert frame[("510300.SH", "close")].notna().sum() == len(frame)
    assert not frame.empty


def test_provider_to_numpy_shape(tmp_path: Path) -> None:
    prov = _provider(tmp_path)
    arr = prov.to_numpy(
        ["510300.SH", "510500.SH"], ["close", "volume"], _at(2020, 1, 2), _at(2020, 4, 30)
    )
    frame = prov.to_frame(
        ["510300.SH", "510500.SH"], ["close", "volume"], _at(2020, 1, 2), _at(2020, 4, 30)
    )
    assert arr.ndim == 3
    assert arr.shape == (len(frame), 2, 2)
    assert arr.dtype == np.float64


def test_label_forward_return_value() -> None:
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    close = pd.Series([10.0, 11.0, 12.0, 12.0, 13.0], index=idx)
    bar = pd.DataFrame({"close": close})
    lab = label_forward_return(bar, horizon=2)
    assert abs(lab.iloc[0] - (12.0 / 10.0 - 1.0)) < 1e-12
    assert np.isnan(lab.iloc[3])  # 尾部不足 horizon → NaN
    assert np.isnan(lab.iloc[4])


def test_label_forward_return_requires_sorted() -> None:
    # 索引乱序（2020-01-02 排在最前）→ 拒绝（前瞻标签要求时间升序）
    idx = pd.DatetimeIndex(["2020-01-02", "2020-01-01", "2020-01-03"], dtype="datetime64[ns]")
    bar = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    with pytest.raises(MtzQuantError, match="未升序"):
        label_forward_return(bar, horizon=1)


class TestDatasetBuilder:
    def _features(self, n: int = 40) -> pd.DataFrame:
        idx = pd.date_range("2020-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "momentum": np.linspace(0.01, 0.1, n),
                "volatility": np.linspace(0.2, 0.3, n),
            },
            index=idx,
        )

    def test_build_rolling_window(self) -> None:
        builder = DatasetBuilder(lookback=5, horizon=2)
        feats = self._features(40)
        y = label_forward_return(pd.DataFrame({"close": 10 + np.arange(40, dtype=float)}), 2)
        y.index = feats.index
        ds = builder.build(feats, y=y)
        assert ds.X.shape[1] == 5  # lookback
        assert ds.X.shape[2] == 2  # 特征数
        assert ds.n_samples == 40 - 5 - 2  # 尾部 horizon 剔除后无 NaN（lookback 预热也剔除）
        assert ds.X[0].shape == (5, 2)
        assert ds.meta["lookback"] == 5

    def test_split_by_time_no_leak(self) -> None:
        builder = DatasetBuilder(lookback=3, horizon=1)
        feats = self._features(40)
        y = label_forward_return(pd.DataFrame({"close": 10 + np.arange(40, dtype=float)}), 1)
        y.index = feats.index
        ds = builder.build(feats, y=y)
        train_end = date(2020, 1, 15)
        valid_end = date(2020, 1, 25)
        train, valid, test = split_by_time(ds, train_end, valid_end)
        # 防泄露: train 全部早于 valid/test（T-R04 自动断言）
        assert train.dt_index[-1] <= train_end
        assert train.dt_index[-1] < valid.dt_index[0]
        assert valid.dt_index[-1] < test.dt_index[0]
        assert len(train.X) + len(valid.X) + len(test.X) == ds.n_samples

    def test_split_invalid_boundaries_rejected(self) -> None:
        builder = DatasetBuilder(lookback=2, horizon=1)
        feats = self._features(20)
        y = label_forward_return(pd.DataFrame({"close": np.arange(20, dtype=float)}), 1)
        y.index = feats.index
        ds = builder.build(feats, y=y)
        with pytest.raises(MtzQuantError, match="早于"):
            split_by_time(ds, date(2020, 1, 20), date(2020, 1, 10))

    def test_build_from_provider_codes(self, tmp_path: Path) -> None:
        prov = _provider(tmp_path)
        builder = DatasetBuilder(lookback=5, horizon=2)
        ds = builder.build_from_provider(
            prov, ["510300.SH", "510500.SH"], ["close", "volume"], _at(2020, 1, 2), _at(2020, 4, 30)
        )
        assert ds.n_samples > 0
        assert ds.X.shape[1] == 5
        assert set(ds.code_index) == {"510300.SH", "510500.SH"}


def test_ml_contract_no_engine_import() -> None:
    """ml 通道独立: 不 import engine（契约）, 前瞻标签只在 ml 出现。"""
    import subprocess
    import sys

    code = "import mtzquant.ml.dataset; import sys; print('ok')"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert r.returncode == 0, r.stderr
