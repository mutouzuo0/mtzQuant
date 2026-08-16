# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:32:00
# @update_time        : 2026/08/16 21:59:08
# @description : D5 两级缓存：L1 内存 + L2 parquet（mtime/size 失效重建，设计 3.7）

"""两级缓存（设计 3.7）——供给层性能核心；数据正确性不依赖它（只是加速）。

  L1 内存: 会话级 {cache_key: DataFrame}（预热后主循环读内存零解析）
  L2 磁盘: parquet 二级缓存（.cache/parquet/{code}_{freq}_{adjust}.parquet）,
           首次由源 CSV 解析归一后落盘, 二次启动免 CSV 解析（省 60%+ 加载时间）;
           adjust 进入缓存键（不同复权口径各自缓存, 互不污染, 3.7）。

失效规则（设计 3.7）:
  源 CSV 文件 mtime 或 size 变化 → 对应 parquet 失效重建（旁车 .sig 文件记录基线）
  未变 → L2 命中（T-D05 用 stats.source_loads 断言免源解析）

并发纪律: 单写进程；临时文件 + os.replace 原子落盘（中断不产生半截缓存）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import AdjustMode, Frequency
from mtzquant.data.normalizer import dt_index_to_ms


@dataclass
class CacheStats:
    """缓存统计（确定性测试断言: T-D05 免解析 / 失效重建次数）。"""

    l1_hits: int = 0  # 内存命中
    l2_hits: int = 0  # parquet 命中（免源解析）
    source_loads: int = 0  # 走源解析次数（CSV 读取/归一）
    writes: int = 0  # 落盘次数

    def snapshot(self) -> dict[str, int]:
        return {
            "l1_hits": self.l1_hits,
            "l2_hits": self.l2_hits,
            "source_loads": self.source_loads,
            "writes": self.writes,
        }


class DataCache:
    """两级缓存（设计 3.7）。线程非安全（主循环单线程使用）。"""

    def __init__(self, parquet_dir: Path | str, enabled: bool = True) -> None:
        self._dir = Path(parquet_dir)
        self._enabled = enabled
        self._l1: dict[str, pd.DataFrame] = {}
        self.stats = CacheStats()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def get(
        self,
        code: str,
        frequency: Frequency,
        adjust: AdjustMode,
        *,
        loader: Callable[[], pd.DataFrame],
        source_ref: Path | None = None,
    ) -> pd.DataFrame:
        """三级查询: L1 内存 → L2 parquet（失效校验）→ 源加载回调。

        参数:
            loader      源解析回调（driver.load_kline + normalizer.normalize 的合成）
            输出即内部统一 K 线（索引=UTC+8 dt, 列=KLINE_COLUMNS）。
            source_ref  源 CSV 路径（失效判据 mtime/size）；None → 永不失效。
        """
        if not self._enabled:
            self.stats.source_loads += 1
            return loader()

        key = cache_key(code, frequency, adjust)
        if key in self._l1:
            self.stats.l1_hits += 1
            return self._l1[key]

        cached = self._parquet_load(key, source_ref)
        if cached is not None:
            self.stats.l2_hits += 1
            self._l1[key] = cached
            return cached

        self.stats.source_loads += 1
        df = loader()
        self._parquet_save(key, df, source_ref)
        self._l1[key] = df
        return df

    # ------------------------------------------------------------------
    # L2 parquet
    # ------------------------------------------------------------------
    def _parquet_path(self, key: str) -> Path:
        return self._dir / f"{key}.parquet"

    def _parquet_load(self, key: str, source_ref: Path | None) -> pd.DataFrame | None:
        p = self._parquet_path(key)
        if not p.is_file():
            return None
        # 失效校验（设计 3.7: 源 mtime/size 变化 → 视同不存在, 走重建）
        if source_ref is not None and _read_sig(p) != _sig(source_ref):
            return None
        try:
            raw = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001
            # 半截/损坏缓存 → 丢弃重建（宁重算不硬错）
            p.unlink(missing_ok=True)
            _sig_path(p).unlink(missing_ok=True)
            raise MtzQuantError(
                f"parquet 缓存解析失败: {p}（已丢弃, 下次重建）",
                stage="cache",
                hint="缓存损坏多来自写盘中断；重建即自愈",
            ) from exc
        return _restore_index(raw)

    def _parquet_save(self, key: str, df: pd.DataFrame, source_ref: Path | None) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        out = df.copy()
        out.insert(0, "dt", dt_index_to_ms(df.index))
        tmp = self._dir / f"{key}.tmp{os.getpid()}.parquet"
        try:
            out.to_parquet(tmp, index=False)
            os.replace(tmp, self._parquet_path(key))
        finally:
            tmp.unlink(missing_ok=True)
        if source_ref is not None:
            _write_sig(self._parquet_path(key), _sig(source_ref))
        self.stats.writes += 1

    # ------------------------------------------------------------------
    # 清理（mtzquant cache clean 语义, 3.7）
    # ------------------------------------------------------------------
    def clean(self, code: str | None = None, frequency: Frequency | None = None) -> int:
        """删除匹配的 parquet 缓存（含 .sig 旁车）; 返回删除的 parquet 数。缺省=全量。"""
        removed = 0
        if not self._dir.is_dir():
            return 0
        for f in sorted(self._dir.iterdir()):
            if not f.name.endswith(".parquet"):
                continue
            stem = f.name[: -len(".parquet")]
            if code is not None and not stem.startswith(code + "_"):
                continue
            if frequency is not None and f"_{frequency.value}_" not in stem:
                continue
            f.unlink(missing_ok=True)
            _sig_path(f).unlink(missing_ok=True)
            removed += 1
        return removed

    def clear_l1(self) -> None:
        self._l1.clear()


def cache_key(code: str, frequency: Frequency, adjust: AdjustMode) -> str:
    """缓存键（adjust 进键, 不同复权口径互不污染, 设计 3.7）。"""
    return f"{code}_{frequency.value}_{adjust.value}"


# ------------------------------------------------------------------
# 失效签名（mtime/size 基线, 设计 3.7）
# ------------------------------------------------------------------
def _sig(source: Path) -> tuple[int, int]:
    st = source.stat()
    return (int(st.st_mtime_ns), int(st.st_size))


def _sig_path(p: Path) -> Path:
    return p.with_suffix(p.suffix + ".sig")


def _write_sig(p: Path, sig: tuple[int, int]) -> None:
    _sig_path(p).write_text(json.dumps({"mtime_ns": sig[0], "size": sig[1]}), encoding="utf-8")


def _read_sig(p: Path) -> tuple[int, int] | None:
    sp = _sig_path(p)
    if not sp.is_file():
        return None
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
        return (int(data["mtime_ns"]), int(data["size"]))
    except Exception:  # noqa: BLE001
        return None


def _restore_index(raw: pd.DataFrame) -> pd.DataFrame:
    """把落盘的毫秒 dt 列还原为 UTC+8 DatetimeIndex（确定性往返，设计 8.8）。"""
    if "dt" in raw.columns:
        dt = pd.to_datetime(raw.pop("dt"), unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        raw.index = dt
    return raw.sort_index()
