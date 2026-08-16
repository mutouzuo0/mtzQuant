# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:12:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D05：两级缓存 命中/失效/adjust 入键/clean（设计 3.7）

"""T-D05：两级缓存（设计 3.7）。

关键: L2 命中/失效判定只对「新 DataCache 实例」有效（同实例有 L1, 不触发 L2 分支）。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from mtzquant.core.types import AdjustMode, Frequency
from mtzquant.data.cache import DataCache, cache_key
from mtzquant.data.drivers.csv_driver import CsvSourceDriver
from mtzquant.data.normalizer import DataNormalizer

from .conftest import write_day_csv


def _loader(drv: CsvSourceDriver, code: str = "510300.SH"):  # type: ignore[no-untyped-def]
    def loader() -> pd.DataFrame:
        raw = drv.load_kline(code, Frequency.D1, datetime(2024, 1, 1), datetime(2024, 12, 31))
        return DataNormalizer().normalize(raw, code, fmt="auto", limit=None)

    return loader


def _cache_dir(tmp_path: Path) -> Path:
    return tmp_path / ".cache" / "parquet"


def _fresh(tmp_path: Path) -> DataCache:
    return DataCache(_cache_dir(tmp_path))


def _drv(tmp_path: Path) -> CsvSourceDriver:
    return CsvSourceDriver(root_path=str(tmp_path), kline_day_dir="kline/{type}/day")


def _seed_once(tmp_path: Path) -> Path:
    """写源 CSV 并完成首次加载（落盘 parquet+src sig），返回源文件路径。"""
    src = write_day_csv(tmp_path, "510300.SH", "generic")
    cache = _fresh(tmp_path)
    df = cache.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert len(df) == 5
    assert cache.stats.source_loads == 1  # 首写走源解析
    assert cache.stats.writes == 1
    assert (
        _cache_dir(tmp_path)
        .joinpath(cache_key("510300.SH", Frequency.D1, AdjustMode.NONE) + ".parquet")
        .is_file()
    )
    return src


def test_first_load_writes_parquet(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _seed_once(tmp_path)


def test_l1_hit_avoids_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    src = _seed_once(tmp_path)
    cache = _fresh(tmp_path)
    cache.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert cache.stats.source_loads == 0  # 新实例首次走 L2 命中（免源解析）
    cache.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert cache.stats.l1_hits == 1
    assert cache.stats.source_loads == 0  # L1 命中不新增源解析


def test_l2_hit_avoids_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """新实例（模拟二次启动）→ L2 命中, 免源解析（3.7 省 60%+）。"""
    src = _seed_once(tmp_path)
    cache2 = _fresh(tmp_path)
    df = cache2.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert len(df) == 5
    assert cache2.stats.source_loads == 0
    assert cache2.stats.l2_hits == 1


def test_mtime_change_invalidates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """源 CSV mtime 变化 → 对应 parquet 失效重建（3.7）。"""
    src = _seed_once(tmp_path)
    os.utime(src, (src.stat().st_atime + 100, src.stat().st_mtime + 100))
    cache = _fresh(tmp_path)
    cache.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert cache.stats.source_loads == 1  # 重建（新实例无 L1, L2 已失效）


def test_size_change_invalidates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    src = _seed_once(tmp_path)
    with src.open("a", encoding="utf-8") as fh:  # 追加空行 → size 变化
        fh.write("\n")
    cache = _fresh(tmp_path)
    cache.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert cache.stats.source_loads == 1


def test_unchanged_hits_without_parse(tmp_path) -> None:  # type: ignore[no-untyped-def]
    src = _seed_once(tmp_path)
    cache = _fresh(tmp_path)
    cache.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert cache.stats.source_loads == 0  # 未变 → 命中免解析


def test_adjust_in_cache_key_isolated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """adjust 进入缓存键, 不同复权口径互不污染（3.7）。"""
    src = _seed_once(tmp_path)
    cache = _fresh(tmp_path)
    cache.get(
        "510300.SH",
        Frequency.D1,
        AdjustMode.FORWARD,
        loader=_loader(_drv(tmp_path)),
        source_ref=src,
    )
    assert cache.stats.source_loads == 1  # 新 adjust 键 → 独立加载
    k1 = cache_key("510300.SH", Frequency.D1, AdjustMode.NONE)
    k2 = cache_key("510300.SH", Frequency.D1, AdjustMode.FORWARD)
    assert k1 != k2
    assert _cache_dir(tmp_path).joinpath(f"{k1}.parquet").is_file()
    assert _cache_dir(tmp_path).joinpath(f"{k2}.parquet").is_file()


def test_clean_removes_matching(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _seed_once(tmp_path)
    cache = _fresh(tmp_path)
    n = cache.clean(code="510300.SH")
    assert n == 1  # 只计 parquet, 不算 .sig 旁车
    assert list(_cache_dir(tmp_path).glob("*.parquet*")) == []


def test_clean_all_and_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _seed_once(tmp_path)
    cache = _fresh(tmp_path)
    assert cache.clean() == 1
    # 禁用缓存 → 恒走源解析
    src = write_day_csv(tmp_path, "510300.SH", "generic")
    off = DataCache(_cache_dir(tmp_path), enabled=False)
    off.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    off.get(
        "510300.SH", Frequency.D1, AdjustMode.NONE, loader=_loader(_drv(tmp_path)), source_ref=src
    )
    assert off.stats.source_loads == 2
