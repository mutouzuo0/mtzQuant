# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 15:05:00
# @update_time        : 2026/08/16 21:59:08
# @description : O5 DataFetcher 六步管道：覆盖→增量下载(切片/checkpoint/多源)→归一→去重→原子落盘

"""DataFetcher 完整数据获取管道（设计 3.9 全图）。

`fetch(codes, start, end)` 六步:
  ① 覆盖检查（O3 CoverageChecker）——请求区间先裁剪到缺失段;
  ② 增量下载——大区间按年/批切片, checkpoint 按（标的×区间片）落 `.cache/fetch_checkpoint/`,
     `--resume` 续传不重下; 多源顺序 fallback, 报告标注实际来源;
  ③ 归一校验——复用 DataNormalizer（下载与回测同一条归一链路, 3.3）校验数据可归一;
  ④ DuckDB 去重合并——`UNION ALL(旧CSV,新数据)` + `row_number() over (partition by dt
     order by 批次 desc)=1`, keep=latest/first 可配; **落盘前断言 count==distinct dt,
     不满足拒绝写盘**;
  ⑤ 原子落盘——临时文件 + os.replace（中断不损坏已有 CSV）;
  ⑥ 缓存失效——删该 [code,freq] L2 parquet。

落盘保持 **tushare 源格式原始列**（归一前原始落盘, 3.12 raw 精神; 读时经 DataNormalizer）。
主数据（O6）: `fetch_master()` 全量拉取 + code 主键 upsert + 快照留档; `import_dir()` 导入任意 CSV。
"""

from __future__ import annotations

import json
import os
import tempfile
import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from mtzquant.core.codes import normalize_code
from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import Frequency
from mtzquant.data.coverage import CoverageChecker, InstrumentCoverage
from mtzquant.data.duckdb_query import DuckDBQuery
from mtzquant.data.master import InstrumentRow, MasterStore
from mtzquant.data.normalizer import DataNormalizer
from mtzquant.data.ratelimit import RateLimitController, RetryPolicy

# 落盘统一源格式列（3.5; 与 fetch_etf 一致, 读时归一）
TUSHARE_KLINE_COLS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")


def instrument_type_of(code: str) -> str:
    """按代码推断品种类型（3.12 kline/{type} 布局）: 5/159/16 开头=ETF, 其余按股票。"""
    norm = normalize_code(code)
    body = norm.split(".")[0]
    if body.startswith("5") or body.startswith("159") or body.startswith("16"):
        return "etf"
    return "stock"


def _year_slices(start: date, end: date, batch_days: int) -> list[tuple[date, date]]:
    """把 [start,end] 按 ≤batch_days 的连续片段切分（大区间分片, 3.9 ②）。"""
    if start > end:
        return []
    out: list[tuple[date, date]] = []
    lo = start
    while lo <= end:
        hi = min(end, lo + timedelta(days=batch_days - 1))
        out.append((lo, hi))
        lo = hi + timedelta(days=1)
    return out


@dataclass
class FetchReport:
    """单标的下载报告（CLI 输出, 3.9）。"""

    code: str
    status: str = "ok"  # ok | skipped | dry_run | failed
    added_rows: int = 0
    dedup_removed: int = 0
    merged_start: str = ""
    merged_end: str = ""
    source: str = ""
    reason: str = ""


@dataclass
class MasterReport:
    """主数据刷新报告（3.11）。"""

    added: int = 0
    updated: int = 0
    total: int = 0
    source: str = ""
    reason: str = ""


@dataclass
class FundamentalsReport:
    """单标的基本面下载报告（M3-R2, 3.13）。"""

    code: str
    table: str = ""
    status: str = "ok"  # ok | skipped | failed
    added_rows: int = 0
    merged_rows: int = 0
    source: str = ""
    reason: str = ""


@dataclass
class ConstituentsReport:
    """单指数成分快照下载报告（M3-R2, 3.13）。"""

    index_code: str
    snapshot_date: str = ""
    status: str = "ok"  # ok | skipped | failed
    members: int = 0
    source: str = ""
    reason: str = ""


def _is_rate_limited(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    for kw in ("限流", "frequence", "too many", "429", "rate limit", "每分钟", "被限制"):
        if kw.lower() in text:
            return True
    return False


class DataFetcher:
    """六步管道数据获取（设计 3.9）。"""

    def __init__(
        self,
        root_path: Path | str,
        *,
        sources: list[str] | tuple[str, ...] = ("akshare", "tushare"),
        checkpoint_dir: Path | str = ".cache/fetch_checkpoint",
        cache_dir: Path | str = ".cache/parquet",
        controller: RateLimitController | None = None,
        retry_policy: RetryPolicy | None = None,
        fetch_fn: Callable[..., pd.DataFrame] | None = None,  # 测试注入（绕过网络）
        fetch_fund_fn: Callable[..., pd.DataFrame] | None = None,  # 测试注入（基本面, M3-R2）
        fetch_constituents_fn: Callable[..., pd.DataFrame] | None = None,  # 测试注入（成分, M3-R2）
        now: Callable[[], float] | None = None,
        batch_days: int = 365,
        dedup_keep: str = "latest",
    ) -> None:
        self._root = Path(root_path)
        self._sources = list(sources)
        self._checkpoint_dir = Path(checkpoint_dir)
        self._cache_dir = Path(cache_dir)
        self._controller = controller or RateLimitController(now=now)
        self._retry = retry_policy or RetryPolicy(max_retries=3)
        self._fetch_fn = fetch_fn
        self._fetch_fund_fn = fetch_fund_fn
        self._fetch_constituents_fn = fetch_constituents_fn
        self._now = now or time_mod.monotonic
        self._batch_days = batch_days
        self._dedup_keep = dedup_keep
        self._master = MasterStore(self._root / "master" / "instruments.csv")

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def kline_path(self, code: str, instrument_type: str) -> Path:
        return self._root / "kline" / instrument_type / "day" / f"{code}.csv"

    def _checkpoint_path(self, code: str, start: date, end: date) -> Path:
        return self._checkpoint_dir / code / f"{start:%Y%m%d}_{end:%Y%m%d}.json"

    # ------------------------------------------------------------------
    # ① 覆盖检查（O3）
    # ------------------------------------------------------------------
    def coverage(self, codes: list[str]) -> list[InstrumentCoverage]:
        return [
            CoverageChecker(self._root, instrument_type=instrument_type_of(c)).coverage(c)
            for c in sorted({normalize_code(x) for x in codes})
        ]

    def gaps(self, code: str, start: date, end: date) -> tuple[str, list[tuple[date, date]]]:
        typ = instrument_type_of(code)
        return typ, CoverageChecker(self._root, instrument_type=typ).gaps(code, start, end)

    # ------------------------------------------------------------------
    # ② 增量下载（切片 + checkpoint + 多源 fallback）
    # ------------------------------------------------------------------
    def _fetch_source(
        self, source: str, code: str, start: date, end: date, typ: str
    ) -> pd.DataFrame:
        """单源拉取（含限流等待 + 指数退避重试）; 结构化错误/封禁类不重试。"""
        attempt = 0
        while True:
            wait = self._controller.wait(source)
            if wait > 0 and self._now is time_mod.monotonic:  # 真实时钟才 sleep（测试注入不阻塞）
                time_mod.sleep(wait)
            try:
                df = self._fetch_once(source, code, start, end, typ)
                self._controller.record_success(source)
                return df
            except MtzQuantError:
                self._controller.record_failure(source)  # 结构化错误（token 缺失等）→ 换源
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_rate_limited(exc):
                    self._controller.record_rate_limited(source)
                else:
                    self._controller.record_failure(source)
                attempt += 1
                if not self._retry.allow_retry(attempt):
                    raise
                if self._now is time_mod.monotonic:  # 测试友好: 注入 now 时不真实 sleep
                    time_mod.sleep(self._retry.next_delay(attempt))

    def _fetch_once(self, source: str, code: str, start: date, end: date, typ: str) -> pd.DataFrame:
        if self._fetch_fn is not None:
            return self._fetch_fn(code, start, end, source=source, instrument_type=typ)
        from mtzquant.data.drivers.remote import get_source

        return get_source(source).fetch_kline(code, start, end, instrument_type=typ)

    # ------------------------------------------------------------------
    # ③ 归一校验（复用 DataNormalizer, 3.3）
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_raw(raw: pd.DataFrame, code: str) -> pd.DataFrame:
        """下载数据归一校验 + 清洗（日期可解析、非空、无重复、排序; 不可归一则拒绝）。"""
        if raw is None or raw.empty:
            return raw
        if "trade_date" not in raw.columns:
            raise MtzQuantError(
                f"下载数据缺 trade_date 列: {code}",
                stage="fetcher",
                hint="源需返回 tushare 源格式列（3.5）",
            )
        dts = pd.to_datetime(raw["trade_date"], format="%Y%m%d", errors="coerce")
        if dts.isna().any():
            raise MtzQuantError(
                f"下载数据含不可解析日期: {code}",
                stage="fetcher",
                hint="源返回了非法 trade_date（3.3）",
            )
        # 重复 dt 拒绝写盘（3.9 c: 源数据不应有重复日; 不静默吞掉）
        dup = int(dts.duplicated().sum())
        if dup:
            raise MtzQuantError(
                f"下载数据含重复 dt {dup} 行: {code}, 拒绝写盘（3.9 c）", stage="fetcher"
            )
        # 复用 DataNormalizer 校验（下载与回测同一条归一链路, 3.3）——数据不可归一则视为坏数据
        try:
            DataNormalizer(Frequency.D1).normalize(raw, code)
        except MtzQuantError as exc:
            raise MtzQuantError(
                f"下载数据归一校验失败: {code}: {exc.message}", stage="fetcher"
            ) from exc
        clean = raw[dts.notna()].copy()
        return clean.sort_values("trade_date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # ④ DuckDB 去重合并（3.10） + ⑤ 原子落盘 + ⑥ 缓存失效
    # ------------------------------------------------------------------
    def _merge_new(self, code: str, path: Path, new_raw: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """旧 CSV + 新数据 → DuckDB UNION ALL + ROW_NUMBER 去重（keep=latest/first, 3.10）。"""
        order = "DESC" if self._dedup_keep == "latest" else "ASC"
        if not path.is_file():
            merged = new_raw[list(TUSHARE_KLINE_COLS)]
            before = len(merged)
            merged = merged.drop_duplicates(subset=["trade_date"], keep="last").sort_values(
                "trade_date"
            )
            return merged, before - len(merged)
        old = str(path).replace("\\", "/")
        new = _df_to_temp_csv(new_raw, code, self._root)  # 新数据暂存临时 CSV（DuckDB 直读）
        try:
            sql = (
                "WITH merged AS ("
                "  SELECT ts_code, CAST(trade_date AS VARCHAR) AS trade_date,"
                "         CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high,"
                "         CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close,"
                "         CAST(vol AS DOUBLE) AS vol, CAST(amount AS DOUBLE) AS amount,"
                "         ROW_NUMBER() OVER (PARTITION BY CAST(trade_date AS VARCHAR)"
                f"          ORDER BY batch_seq {order}) AS rn"
                "  FROM ("
                f"    SELECT *, 0 AS batch_seq FROM read_csv_auto('{old}', header=true,"
                "            sample_size=100000)"
                "    UNION ALL"
                f"    SELECT *, 1 AS batch_seq FROM read_csv_auto('{new}', header=true,"
                "            sample_size=100000)"
                "  )"
                ")"
                " SELECT ts_code, trade_date, open, high, low, close, vol, amount"
                " FROM merged WHERE rn = 1 ORDER BY trade_date"
            )
            q = DuckDBQuery()
            try:
                merged = q.execute_select(sql)
            finally:
                q.close()
            return merged, 0
        finally:
            os.remove(new)

    def _assert_unique(self, df: pd.DataFrame, code: str) -> None:
        """落盘前断言 count == distinct dt（3.9 c; 不满足拒绝写盘）。"""
        if len(df) != df["trade_date"].nunique():
            n, u = len(df), df["trade_date"].nunique()
            raise MtzQuantError(
                f"去重后仍不唯一: {code}（{n} 行 / {u} 唯一日）, 拒绝写盘（3.9 c）",
                stage="fetcher",
                hint="构造数据含重复 dt, 检查源返回或人工改动; 原文件完好未动",
            )

    def _atomic_write(self, path: Path, df: pd.DataFrame) -> None:
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

    def _invalidate_cache(self, code: str) -> None:
        """⑥ 缓存失效: 删该 [code,freq] L2 parquet（3.7）。"""
        for p in self._cache_dir.glob(f"*{code}*"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 主入口: 六步管道
    # ------------------------------------------------------------------
    def fetch(
        self,
        codes: list[str],
        start: date,
        end: date,
        *,
        frequency: str = "1d",
        dry_run: bool = False,
        resume: bool = False,
    ) -> list[FetchReport]:
        if frequency != "1d":
            raise MtzQuantError(
                f"DataFetcher 暂只支持日线（1d）, 收到 {frequency!r}",
                stage="fetcher",
                hint="分钟线归 M5（pyproject marker m5_deferred）",
            )
        reports: list[FetchReport] = []
        for code in sorted({normalize_code(c) for c in codes}):
            reports.append(self._fetch_one(code, start, end, dry_run=dry_run, resume=resume))
        return reports

    def _fetch_one(
        self, code: str, start: date, end: date, *, dry_run: bool, resume: bool
    ) -> FetchReport:
        report = FetchReport(code=code)
        typ = instrument_type_of(code)
        path = self.kline_path(code, typ)
        try:
            checker = CoverageChecker(self._root, instrument_type=typ)
            gaps = checker.gaps(code, start, end)
            if dry_run:
                report.status = "dry_run"
                report.reason = f"缺失段 {len(gaps)} 个" + (
                    f"（{_fmt_range(gaps[0])} …）" if gaps else "（已全覆盖）"
                )
                return report
            if not gaps:
                report.status = "skipped"
                report.reason = "本地已覆盖请求区间"
                return report

            total_added = 0
            total_dedup = 0
            source_used = ""
            mlo = mhi = None
            for glo, ghi in gaps:
                for slo, shi in _year_slices(glo, ghi, self._batch_days):
                    done = self._checkpoint_done(code, slo, shi)
                    if resume and done:
                        continue  # --resume 续传: 已下区间不重下（不重复请求）
                    raw, source = self._download_slice(code, typ, slo, shi)
                    source_used = source or source_used
                    if raw is None or raw.empty:
                        self._mark_checkpoint(code, slo, shi, source, 0)
                        continue
                    clean = self._validate_raw(raw, code)
                    old_rows = _local_rows(path)
                    merged, dedup = self._merge_new(code, path, clean)
                    self._assert_unique(merged, code)
                    added = max(len(merged) - old_rows, 0)
                    self._atomic_write(path, merged)
                    total_added += added
                    total_dedup += dedup
                    mlo = merged["trade_date"].iloc[0]
                    mhi = merged["trade_date"].iloc[-1]
                    self._mark_checkpoint(code, slo, shi, source, len(clean))
            report.added_rows = total_added
            report.dedup_removed = total_dedup
            report.source = source_used
            report.merged_start = mlo or ""
            report.merged_end = mhi or ""
            self._invalidate_cache(code)  # ⑥
            self._master.upsert(
                [InstrumentRow(code=code, instrument_type=typ, exchange=code.rsplit(".", 1)[-1])]
            )
            if total_added == 0 and source_used:
                report.status = "skipped"
                report.reason = "拉取后无新增（全部已存在）"
            return report
        except MtzQuantError as exc:
            report.status = "failed"
            report.reason = exc.message
            return report
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.reason = f"{type(exc).__name__}: {exc}"
            return report

    def _download_slice(
        self, code: str, typ: str, start: date, end: date
    ) -> tuple[pd.DataFrame, str]:
        """多源顺序 fallback; 全部失败抛结构化错误。"""
        last_err: Exception | None = None
        for source in self._sources:
            try:
                df = self._fetch_source(source, code, start, end, typ)
                return df, source
            except MtzQuantError as exc:
                last_err = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        raise MtzQuantError(
            f"全部源拉取失败: {code} {start}~{end}",
            stage="fetcher",
            hint=f"末次原因: {last_err}; 检查网络/限流/源可用性（3.9）",
        ) from last_err

    # ------------------------------------------------------------------
    # checkpoint（3.9 ②: 按 标的×区间片 落 JSON, --resume 续传不重下）
    # ------------------------------------------------------------------
    def _checkpoint_done(self, code: str, start: date, end: date) -> bool:
        p = self._checkpoint_path(code, start, end)
        if not p.is_file():
            return False
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("status") == "done"
        except (OSError, ValueError):
            return False

    def _mark_checkpoint(self, code: str, start: date, end: date, source: str, rows: int) -> None:
        p = self._checkpoint_path(code, start, end)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"status": "done", "source": source, "rows": rows, "at": datetime.now().isoformat()}
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # O6 主数据与导入
    # ------------------------------------------------------------------
    def fetch_master(
        self, sources: list[str] | None = None, instrument_type: str | None = None
    ) -> MasterReport:
        """全量主数据拉取 → 源字段映射 → 代码归一 → code 主键 upsert（3.11）。"""
        rep = MasterReport()
        srcs = list(sources or self._sources)
        last_err: Exception | None = None
        df = None
        for source in srcs:
            try:
                self._controller.wait(source)
                df = self._fetch_master_once(source, instrument_type)
                self._controller.record_success(source)
                rep.source = source
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._controller.record_failure(source)
                continue
        if df is None:
            rep.reason = f"主数据拉取失败: {last_err}"
            return rep
        rows = self._map_master_rows(df, instrument_type)
        rep.added, rep.updated = self._master.upsert(rows)
        rep.total = len(rows)
        return rep

    @staticmethod
    def _fetch_master_once(source: str, instrument_type: str | None) -> pd.DataFrame:
        from mtzquant.data.drivers.remote import get_source

        return get_source(source).fetch_master(instrument_type)

    @staticmethod
    def _map_master_rows(df: pd.DataFrame, instrument_type: str | None) -> list[InstrumentRow]:
        """源原始列 → InstrumentRow（ts_code → 归一 code, name, list_date, 3.11）。"""
        rows: list[InstrumentRow] = []
        for _, r in df.iterrows():
            raw_code = str(r.get("ts_code") or r.get("code") or r.get("symbol") or "").strip()
            if not raw_code:
                continue
            code = normalize_code(raw_code)
            rows.append(
                InstrumentRow(
                    code=code,
                    name=str(r.get("name") or "").strip(),
                    instrument_type=instrument_type or instrument_type_of(code),
                    exchange=code.rsplit(".", 1)[-1],
                    list_date=str(r.get("list_date") or "").strip(),
                    delist_date=str(r.get("delist_date") or "").strip(),
                )
            )
        return rows

    # ------------------------------------------------------------------
    # M3-R2 基本面通道（3.13）: 财务最小集 + 每日估值, 落 fundamentals/{table}/{code}.csv
    # ------------------------------------------------------------------
    def fundamentals_path(self, table: str, code: str) -> Path:
        return self._root / "fundamentals" / table / f"{code}.csv"

    def fetch_fundamentals(
        self,
        codes: list[str],
        table: str,
        start: date,
        end: date,
        *,
        sources: list[str] | None = None,
        dry_run: bool = False,
    ) -> list[FundamentalsReport]:
        """拉取基本面最小集（fina_indicator 主键 code+报告期+公告日, 修订版本行保留）。

        落盘: `data/fundamentals/{table}/{code}.csv`（源原始列, 3.12 raw 精神）;
        幂等: 已存在行按主键去重（重复公告日/交易日行覆盖, 修订版本行保留）。
        """
        from mtzquant.data.drivers.tushare_fundamental import (
            DAILY_BASIC_COLS,
            FINA_INDICATOR_COLS,
        )

        known = {"fina_indicator": FINA_INDICATOR_COLS, "daily_basic": DAILY_BASIC_COLS}
        if table not in known:
            raise MtzQuantError(
                f"未知财务表 {table!r}", stage="fetcher", hint=f"可选: {sorted(known)}"
            )
        srcs = list(sources or self._sources)
        reports: list[FundamentalsReport] = []
        for code in sorted({normalize_code(c) for c in codes}):
            reports.append(
                self._fetch_fund_one(code, table, start, end, known[table], srcs, dry_run=dry_run)
            )
        return reports

    def _fetch_fund_one(
        self,
        code: str,
        table: str,
        start: date,
        end: date,
        cols: tuple[str, ...],
        sources: list[str],
        *,
        dry_run: bool,
    ) -> FundamentalsReport:
        rep = FundamentalsReport(code=code, table=table)
        path = self.fundamentals_path(table, code)
        # 幂等: 请求区间已被本地覆盖 → skipped（按日期列粗判）
        if not dry_run and path.is_file():
            local_days = self._fund_local_days(path, table)
            if local_days and min(local_days) <= start and max(local_days) >= end:
                rep.status = "skipped"
                rep.reason = "本地已覆盖请求区间"
                return rep
        try:
            last_err: Exception | None = None
            df = None
            used = ""
            for source in sources:
                wait = self._controller.wait(source)
                if wait > 0 and self._now is time_mod.monotonic:
                    time_mod.sleep(wait)
                try:
                    df = self._fetch_fund_once(source, code, table, start, end)
                    self._controller.record_success(source)
                    used = source
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    self._controller.record_failure(source)
                    continue
            if df is None or df.empty:
                rep.status = "failed" if last_err else "skipped"
                rep.reason = f"无数据: {last_err}" if last_err else "源返回空"
                return rep
            clean = self._clean_fund(df, code, table, cols)
            merged, added = self._merge_fund(path, clean, table)
            self._atomic_write(path, merged)
            rep.added_rows = added
            rep.merged_rows = len(merged)
            rep.source = used
            self._master.upsert(
                [
                    InstrumentRow(
                        code=code,
                        instrument_type=instrument_type_of(code),
                        exchange=code.rsplit(".", 1)[-1],
                    )
                ]
            )
            return rep
        except MtzQuantError as exc:
            rep.status = "failed"
            rep.reason = exc.message
            return rep
        except Exception as exc:  # noqa: BLE001
            rep.status = "failed"
            rep.reason = f"{type(exc).__name__}: {exc}"
            return rep

    def _fetch_fund_once(
        self, source: str, code: str, table: str, start: date, end: date
    ) -> pd.DataFrame:
        """单源基本面拉取（测试注入优先, 否则走 tushare_fundamental 驱动）。"""
        if self._fetch_fund_fn is not None:
            return self._fetch_fund_fn(code, table, start, end, source=source)
        from mtzquant.data.drivers.remote import get_fundamental_source

        src = get_fundamental_source(source)
        if table == "fina_indicator":
            return src.fetch_fina_indicator(code, start, end)
        if table == "daily_basic":
            return src.fetch_daily_basic(code, start, end)
        raise MtzQuantError(f"未知财务表 {table!r}", stage="fetcher")

    @staticmethod
    def _clean_fund(df: pd.DataFrame, code: str, table: str, cols: tuple[str, ...]) -> pd.DataFrame:
        """基本面下载校验: 列齐全/日期可解析/无重复主键; 不可归一则拒绝。"""
        if df is None or df.empty:
            return df
        missing = [c for c in ("ts_code",) + cols[1:] if c not in df.columns]
        if "ts_code" not in df.columns:
            raise MtzQuantError(
                f"基本面下载缺 ts_code 列: {code}/{table}",
                stage="fetcher",
                hint="源需返回 ts_code + 日期列（3.13 源格式）",
            )
        if missing:
            raise MtzQuantError(
                f"基本面下载缺列: {code}/{table}: {missing}",
                stage="fetcher",
                hint=f"期望列: {cols}",
            )
        date_col = "ann_date" if table == "fina_indicator" else "trade_date"
        dts = pd.to_datetime(df[date_col], format="%Y%m%d", errors="coerce")
        if dts.isna().any():
            raise MtzQuantError(
                f"基本面下载含不可解析 {date_col}: {code}/{table}",
                stage="fetcher",
                hint="源返回了非法日期（3.13）",
            )
        key = ["end_date", "ann_date"] if table == "fina_indicator" else ["trade_date"]
        if df.duplicated(subset=key).any():
            raise MtzQuantError(
                f"基本面下载含重复主键 {key}: {code}/{table}, 拒绝写盘",
                stage="fetcher",
                hint="单次拉取不应重复（3.9 c）; 修订版本行指不同 ann_date, 不属重复",
            )
        return df.sort_values(date_col).reset_index(drop=True)

    def _merge_fund(self, path: Path, new_df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, int]:
        """旧文件 + 新数据按主键去重合并（修订版本行保留: fina_indicator 主键含 ann_date）。"""
        if table == "fina_indicator":
            key = ["ts_code", "end_date", "ann_date"]
        else:
            key = ["ts_code", "trade_date"]
        if not path.is_file():
            merged = new_df.copy()
            merged = merged.drop_duplicates(subset=key, keep="last").sort_values(key[-1])
            return merged, len(merged)  # 无旧文件 → 全部为新增
        try:
            old = pd.read_csv(path, dtype=str, keep_default_na=False)
        except OSError:
            old = pd.DataFrame()
        merged = (
            pd.concat([old, new_df.astype(str)], ignore_index=True)
            .drop_duplicates(subset=key, keep="last")
            .sort_values(key[-1])
            .reset_index(drop=True)
        )
        return merged, max(len(merged) - len(old), 0)

    @staticmethod
    def _fund_local_days(path: Path, table: str) -> set[date]:
        """本地基本面文件已有日期（幂等粗判; 读取失败 → 空集）。"""
        try:
            col = "ann_date" if table == "fina_indicator" else "trade_date"
            df = pd.read_csv(path, usecols=[col], dtype=str, keep_default_na=False)
            dts = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce").dropna()
            return {d.date() for d in dts}
        except Exception:  # noqa: BLE001
            return set()

    # ------------------------------------------------------------------
    # M3-R2 成分快照通道（3.13）: 落 index_constituents/{index}_{date}.csv
    # ------------------------------------------------------------------
    def constituents_path(self, index_code: str, snapshot_date: date) -> Path:
        return self._root / "index_constituents" / f"{index_code}_{snapshot_date:%Y%m%d}.csv"

    def fetch_constituents(
        self,
        index_codes: list[str],
        snapshot_date: date,
        *,
        sources: list[str] | None = None,
        dry_run: bool = False,
    ) -> list[ConstituentsReport]:
        """拉取指数成分快照（某交易日成分+权重+入/出日期区间; 幂等: 已存在则 skipped）。"""
        srcs = list(sources or self._sources)
        reports: list[ConstituentsReport] = []
        for index in sorted({normalize_code(c) for c in index_codes}):
            reports.append(
                self._fetch_constituents_one(index, snapshot_date, srcs, dry_run=dry_run)
            )
        return reports

    def _fetch_constituents_one(
        self, index: str, snapshot_date: date, sources: list[str], *, dry_run: bool
    ) -> ConstituentsReport:
        rep = ConstituentsReport(index_code=index, snapshot_date=snapshot_date.isoformat())
        path = self.constituents_path(index, snapshot_date)
        if not dry_run and path.is_file():
            rep.status = "skipped"
            rep.reason = "快照已存在"
            return rep
        try:
            last_err: Exception | None = None
            df = None
            used = ""
            for source in sources:
                wait = self._controller.wait(source)
                if wait > 0 and self._now is time_mod.monotonic:
                    time_mod.sleep(wait)
                try:
                    df = self._fetch_constituents_once(source, index, snapshot_date)
                    self._controller.record_success(source)
                    used = source
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    self._controller.record_failure(source)
                    continue
            if df is None or df.empty:
                rep.status = "failed" if last_err else "skipped"
                rep.reason = f"无数据: {last_err}" if last_err else "源返回空"
                return rep
            clean = self._clean_constituents(df, index)
            self._atomic_write(path, clean)
            rep.members = len(clean)
            rep.source = used
            return rep
        except MtzQuantError as exc:
            rep.status = "failed"
            rep.reason = exc.message
            return rep
        except Exception as exc:  # noqa: BLE001
            rep.status = "failed"
            rep.reason = f"{type(exc).__name__}: {exc}"
            return rep

    def _fetch_constituents_once(
        self, source: str, index: str, snapshot_date: date
    ) -> pd.DataFrame:
        if self._fetch_constituents_fn is not None:
            return self._fetch_constituents_fn(index, snapshot_date, source=source)
        from mtzquant.data.drivers.remote import get_fundamental_source

        return get_fundamental_source(source).fetch_index_constituents(index, snapshot_date)

    @staticmethod
    def _clean_constituents(df: pd.DataFrame, index: str) -> pd.DataFrame:
        """成分快照校验: con_code 列齐全/非空/唯一; 不可归一则拒绝。"""
        if df is None or df.empty:
            return df
        if "con_code" not in df.columns:
            raise MtzQuantError(
                f"成分快照缺 con_code 列: {index}",
                stage="fetcher",
                hint="期望列: index_code/con_code/in_date/out_date/weight（R2 布局）",
            )
        clean = df.copy()
        clean["con_code"] = clean["con_code"].astype(str).str.strip()
        clean = clean[clean["con_code"] != ""]
        if clean.empty:
            raise MtzQuantError(f"成分快照全空: {index}", stage="fetcher")
        clean["index_code"] = index
        for col in ("in_date", "out_date"):
            if col not in clean.columns:
                clean[col] = ""
        if "weight" not in clean.columns:
            clean["weight"] = float("nan")
        return clean.drop_duplicates(subset=["con_code"]).reset_index(drop=True)

    def import_dir(self, src_dir: Path | str, *, resume: bool = True) -> list[FetchReport]:
        """导入目录任意 CSV（3.5 嗅探 → 归一校验 → 去重合并入库, 3.11）。"""
        from mtzquant.data.drivers.csv_driver import CsvSourceDriver

        src = Path(src_dir)
        reports: list[FetchReport] = []
        for f in sorted(src.glob("*.csv")):
            raw = pd.read_csv(f, dtype=str, keep_default_na=False)
            if raw.empty:
                continue
            fmt = CsvSourceDriver.sniff_format([c.lower() for c in raw.columns])
            code = _code_of_file(f, raw, fmt)
            if code is None:
                reports.append(
                    FetchReport(code=f.stem, status="failed", reason="无法从文件/列推断代码")
                )
                continue
            path = self.kline_path(code, instrument_type_of(code))
            try:
                clean = self._validate_raw(raw, code)
                merged, dedup = self._merge_new(code, path, clean)
                self._assert_unique(merged, code)
                self._atomic_write(path, merged)
                self._invalidate_cache(code)
                reports.append(
                    FetchReport(
                        code=code,
                        status="ok",
                        dedup_removed=dedup,
                        merged_start=merged["trade_date"].iloc[0],
                        merged_end=merged["trade_date"].iloc[-1],
                    )
                )
            except MtzQuantError as exc:
                reports.append(FetchReport(code=code, status="failed", reason=exc.message))
        return reports


def _code_of_file(f: Path, raw: pd.DataFrame, fmt: str) -> str | None:
    """导入文件代码: 源列 ts_code / 文件名（去掉平台前缀, 3.5）。"""

    for col in ("ts_code", "symbol", "code"):
        if col in raw.columns and len(raw) > 0 and str(raw[col].iloc[0]).strip():
            try:
                return normalize_code(str(raw[col].iloc[0]))
            except MtzQuantError:
                pass
    stem = f.stem
    for prefix in ("tushare_", "joinquant_", "generic_", "normalized_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
    try:
        return normalize_code(stem)
    except MtzQuantError:
        return None


def _local_rows(path: Path) -> int:
    """返回旧文件行数（文件不存在/读取失败 → 0）。"""
    if not path.is_file():
        return 0
    try:
        return len(pd.read_csv(path, dtype=str, keep_default_na=False))
    except Exception:  # noqa: BLE001
        return 0


def _df_to_temp_csv(df: pd.DataFrame, code: str, root: Path) -> str:
    """新数据落临时 CSV（DuckDB read_csv_auto 直读; 用后即删）。"""
    tmp_dir = root / ".cache" / "fetch_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".csv", dir=tmp_dir)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        df.to_csv(fh, index=False)
    return tmp


def _fmt_range(r: tuple[date, date]) -> str:
    return f"{r[0]}~{r[1]}"
