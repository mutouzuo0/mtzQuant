# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:30:00
# @update_time        : 2026/08/16 21:59:08
# @description : E 最小 ETF 下载器：限流/幂等/去重断言/原子写/熔断（3.9 裁剪版）

"""最小 ETF 日线下载器（设计 3.9 裁剪版, 阶段 E）——akshare 优先 / tushare 备选。

落盘布局与去重/原子纪律与 3.9 完全一致, M2 完整 DataFetcher 无缝接管:
  落盘: data/kline/etf/day/{code}.csv（tushare 源格式: ts_code/trade_date/OHLC/vol/amount,
        「归一前原始落盘」——读时经 D3 管道归一, 与 3.12 raw 精神一致）;
  master/instruments.csv 同步 upsert 种子行。

纪律（三道防线）:
  a. 幂等     —— 请求区间先裁剪到本地缺失段（pandas 扫 min/max dt, 无 DuckDB 覆盖检查）
  b. 去重合并 —— 按 dt 去重 keep='latest' 后与本地合并
  c. 落盘断言 —— 合并结果 count==distinct dt, 不满足拒绝写盘
  限流: 令牌桶（akshare 1 次/秒、tushare 120 次/分）+ 0.5~1.5×随机抖动
        + 指数退避（max_retry=3）+ 连续失败 5 次熔断报错（3.9 防封禁）
  原子性: 临时文件 + os.replace（中断不损坏已有 CSV）
"""

from __future__ import annotations

import os
import random
import tempfile
import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from mtzquant.config import get_tushare_token, load_secrets
from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import InstrumentType
from mtzquant.data.master import InstrumentRow, MasterStore
from mtzquant.data.ratelimit import TokenBucketLimiter  # D5: 防封禁件提炼自 ratelimit 共用

_TUSHARE_COLS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")


@dataclass
class DownloadReport:
    """单标的下载报告（CLI 输出, 3.9）。"""

    code: str
    status: str = "ok"  # ok | skipped(无缺段) | failed
    added_rows: int = 0
    dedup_removed: int = 0
    merged_start: str = ""
    merged_end: str = ""
    reason: str = ""


class EtfDownloader:
    """ETF 日线下载器（设计 3.9 裁剪版）。"""

    def __init__(
        self,
        root_path: Path | str,
        *,
        source: str = "akshare",  # akshare | tushare
        max_retry: int = 3,
        breaker_threshold: int = 5,
        rate_limit_per_min: int = 60,
        akshare_min_interval_sec: float = 1.0,
        secrets: dict[str, Any] | None = None,
        rng: random.Random | None = None,
        now: Callable[[], float] | None = None,
        fetch_fn: Callable[..., pd.DataFrame] | None = None,  # 测试注入用
    ) -> None:
        self._root = Path(root_path)
        self._source = source
        self._max_retry = max_retry
        self._breaker_threshold = breaker_threshold
        self._secrets = secrets if secrets is not None else load_secrets()
        self._rng = rng or random.Random(42)
        self._now = now or time_mod.monotonic
        self._limiter = TokenBucketLimiter(rate_limit_per_min, rng=self._rng, now=self._now)
        self._akshare_min_interval = akshare_min_interval_sec
        self._fetch_fn = fetch_fn  # 测试注入（绕过真实网络）
        self._master = MasterStore(self._root / "master" / "instruments.csv")

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def kline_path(self, code: str) -> Path:
        return self._root / "kline" / "etf" / "day" / f"{code}.csv"

    # ------------------------------------------------------------------
    # 幂等: 本地缺失段裁剪（3.9 a）
    # ------------------------------------------------------------------
    def _local_range(self, code: str) -> tuple[date, date] | None:
        """返回本地已有数据 [min_dt, max_dt]（无文件/空 → None）。"""
        path = self.kline_path(code)
        if not path.is_file():
            return None
        df = pd.read_csv(path, usecols=["trade_date"], dtype=str, keep_default_na=False)
        if df.empty:
            return None
        dts = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce").dropna()
        if dts.empty:
            return None
        return dts.min().date(), dts.max().date()

    # ------------------------------------------------------------------
    # 拉取（akshare 优先 / tushare 备选; fetch_fn 可注入）
    # ------------------------------------------------------------------
    def _fetch_once(self, code: str, start: date, end: date) -> pd.DataFrame:
        """单次拉取（含来源选择与列归一 → tushare 源格式）。"""
        if self._fetch_fn is not None:
            return self._fetch_fn(code, start, end)
        if self._source == "tushare":
            return self._fetch_tushare(code, start, end)
        try:
            return self._fetch_akshare(code, start, end)
        except ImportError:
            if self._source == "akshare":
                return self._fetch_tushare(code, start, end)  # 备选降级
            raise

    def _fetch_akshare(self, code: str, start: date, end: date) -> pd.DataFrame:
        import akshare as ak  # 可选依赖（extras=[download]）

        df = ak.fund_etf_hist_em(
            symbol=code.split(".")[0],
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
        )
        # 中文列 → tushare 源格式
        rename = {
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "vol",
            "成交额": "amount",
        }
        df = df.rename(columns=rename)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        df.insert(0, "ts_code", code)
        return df[list(_TUSHARE_COLS)]

    def _fetch_tushare(self, code: str, start: date, end: date) -> pd.DataFrame:
        token = get_tushare_token(self._secrets)
        if not token:
            raise MtzQuantError(
                "tushare token 缺失",
                stage="fetch_etf",
                hint="MTZQUANT_TUSHARE_TOKEN 环境变量 或 secrets.json 的 tushare.token（3.6）",
            )
        import tushare as ts  # 可选依赖（extras=[download]）

        pro = ts.pro_api(token)
        df = pro.fund_daily(
            ts_code=code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )
        return df[list(_TUSHARE_COLS)]

    # ------------------------------------------------------------------
    # 去重合并 + 落盘断言 + 原子写（3.9 b/c）
    # ------------------------------------------------------------------
    def _merge(self, code: str, fetched: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """本地已有 + 新拉取 → 按 trade_date 去重（keep='latest'）; 返回 (合并df, 去重剔除行数)。"""
        path = self.kline_path(code)
        frames: list[pd.DataFrame] = []
        if path.is_file():
            local = pd.read_csv(path, dtype=str, keep_default_na=False)
            frames.append(
                local[list(_TUSHARE_COLS)] if set(_TUSHARE_COLS).issubset(local.columns) else local
            )
        frames.append(fetched)
        merged = pd.concat(frames, ignore_index=True)
        before = len(merged)
        # 配置 dedup_keep='latest'（3.9）→ pandas keep='last'（保留最后一条）
        merged = merged.drop_duplicates(subset=["trade_date"], keep="last")
        dedup = before - len(merged)
        merged = merged.sort_values("trade_date").reset_index(drop=True)
        return merged, dedup

    def _atomic_write(self, code: str, df: pd.DataFrame) -> None:
        path = self.kline_path(code)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".csv.tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                df.to_csv(fh, index=False)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def download(self, codes: list[str], start: date, end: date) -> list[DownloadReport]:
        """逐个标的下载; 连续失败达熔断阈值即中止（3.9）。"""
        reports: list[DownloadReport] = []
        consecutive_failures = 0
        for code in sorted({normalize_code(c) for c in codes}):
            report = self._download_one(code, start, end)
            reports.append(report)
            consecutive_failures = consecutive_failures + 1 if report.status == "failed" else 0
            if consecutive_failures >= self._breaker_threshold:
                raise MtzQuantError(
                    f"连续 {self._breaker_threshold} 个标的失败，触发熔断（3.9 防封禁）",
                    stage="fetch_etf",
                    hint=f"最近失败: {report.code}（{report.reason}）; 检查网络/接口限流, 稍后重试",
                )
        return reports

    def _download_one(self, code: str, start: date, end: date) -> DownloadReport:
        report = DownloadReport(code=code)
        try:
            local = self._local_range(code)
            fetch_start, fetch_end = start, end
            if local is not None:
                if local[0] <= start and local[1] >= end:
                    report.status = "skipped"
                    report.reason = "本地已覆盖请求区间"
                    return report
                # 只裁剪到缺失段（3.9 a）
                if local[0] <= start:
                    fetch_start = local[1] + timedelta(days=1)
            if fetch_start > fetch_end:
                report.status = "skipped"
                report.reason = "无缺失段"
                return report

            fetched = self._fetch_with_retry(code, fetch_start, fetch_end)
            merged, dedup = self._merge(code, fetched)

            # 落盘断言: 日期可解析且 count == distinct dt（三道防线 c）
            dt_vals = pd.to_datetime(merged["trade_date"], errors="coerce")
            if dt_vals.isna().any() or dt_vals.nunique() != len(merged):
                report.status = "failed"
                n, u = len(merged), dt_vals.nunique()
                report.reason = f"日期不可解析或去重后仍不唯一（{n} 行/{u} 唯一日），拒绝写盘"
                return report

            added = len(merged) - (0 if local is None else self._local_rows(code))
            self._atomic_write(code, merged)
            self._upsert_master(code)
            report.added_rows = max(added, 0)
            report.dedup_removed = dedup
            report.merged_start = merged["trade_date"].iloc[0]
            report.merged_end = merged["trade_date"].iloc[-1]
            return report
        except MtzQuantError as exc:
            report.status = "failed"
            report.reason = str(exc.message)
            return report
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.reason = f"{type(exc).__name__}: {exc}"
            return report

    def _local_rows(self, code: str) -> int:
        path = self.kline_path(code)
        if not path.is_file():
            return 0
        return len(pd.read_csv(path, dtype=str, keep_default_na=False))

    def _fetch_with_retry(self, code: str, start: date, end: date) -> pd.DataFrame:
        """指数退避重试（max_retry=3, 3.9）; 结构化错误直接透传（保 hint 可读）。"""
        last_exc: Exception | None = None
        for attempt in range(1 + self._max_retry):
            try:
                self._limiter.wait()
                return self._fetch_once(code, start, end)
            except MtzQuantError:
                raise  # 结构化错误（如 token 缺失）不重试, 原样透传
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self._max_retry:
                    break
                delay = (2**attempt) * self._rng.uniform(0.5, 1.5)  # 指数退避 + 抖动
                # 测试友好: now 注入时真实 sleep 可跳过, 但生产仍按 delay
                if self._now is time_mod.monotonic:
                    time_mod.sleep(delay)
        raise MtzQuantError(
            f"拉取失败（重试 {self._max_retry} 次）: {code}",
            stage="fetch_etf",
            hint=f"末次原因: {last_exc}; 检查网络与接口限流（3.9）",
        ) from last_exc

    def _upsert_master(self, code: str) -> None:
        """同步 master/instruments.csv 种子行（3.11/3.12）。"""
        self._master.upsert(
            [
                InstrumentRow(
                    code=code,
                    name="",
                    instrument_type=InstrumentType.ETF.value,
                    exchange=code.rsplit(".", 1)[-1],
                )
            ]
        )


def make_downloader(root_path: Path | str, **kwargs: Any) -> EtfDownloader:
    """快捷构造（CLI/脚本用）。"""
    return EtfDownloader(root_path, **kwargs)
