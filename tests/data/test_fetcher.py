# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 15:30:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-DF01..DF11：O 阶段 DataFetcher/覆盖/限流/驱动/主数据/导入（全 mock）

"""T-DF01..DF11（M2-O, 设计 3.9/3.10/3.11）——HTTP 全 mock。

覆盖: DuckDB 只读安全化/直读、覆盖 gaps、令牌桶+自动降档+熔断、tushare/akshare 驱动、
      六步管道（幂等/去重拒绝/续传/多源 fallback）、主数据 upsert、目录导入。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.data.coverage import CoverageChecker
from mtzquant.data.duckdb_query import DuckDBQuery
from mtzquant.data.fetcher import DataFetcher
from mtzquant.data.ratelimit import RateLimitController, RateSpec, TokenBucketLimiter

CODE = "510300.SH"
START = date(2024, 1, 1)
END = date(2024, 1, 31)


def _row(d: str) -> dict[str, str]:
    return {
        "ts_code": CODE,
        "trade_date": d,
        "open": "3.50",
        "high": "3.60",
        "low": "3.40",
        "close": "3.55",
        "vol": "1200000",
        "amount": "4200000",
    }


def _df(days: list[str]) -> pd.DataFrame:
    return pd.DataFrame([_row(d) for d in days])


def _biz(days: list[date]) -> list[str]:
    return [d.strftime("%Y%m%d") for d in days if d.weekday() < 5]


def _write_local(tmp_path: Path, days: list[str]) -> Path:
    p = tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    _df(days).to_csv(p, index=False)
    return p


class _FakeClock:
    """单调推进的假时钟（测试: 不真实 sleep, 令牌桶时间照常推进）。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def _fetcher(tmp_path: Path, fetch_fn, **kw) -> DataFetcher:  # type: ignore[no-untyped-def]
    return DataFetcher(
        tmp_path,
        sources=["akshare", "tushare"],
        fetch_fn=fetch_fn,
        checkpoint_dir=tmp_path / ".cache" / "fetch_checkpoint",
        cache_dir=tmp_path / ".cache" / "parquet",
        now=_FakeClock(),
        **kw,
    )


# ==================================================================
# T-DF01 DuckDB 只读安全化 + read_csv_auto（3.10）
# ==================================================================
def test_df01_readonly_guard(tmp_path: Path) -> None:
    q = DuckDBQuery()
    try:
        for bad in (
            "DELETE FROM x",
            "INSERT INTO x VALUES(1)",
            "DROP TABLE t",
            "SELECT * FROM x; UPDATE t SET a=1",
            "PRAGMA database_list",
        ):
            with pytest.raises(MtzQuantError):
                q.execute_select(bad)
        # 合法 SELECT 放行
        df = q.execute_select("SELECT 1 AS a")
        assert int(df["a"].iloc[0]) == 1
    finally:
        q.close()


def test_df01_read_csv_auto_with_filename(tmp_path: Path) -> None:
    _write_local(tmp_path, ["20240102", "20240103"])
    q = DuckDBQuery()
    try:
        df = q.read_csv_auto(tmp_path / "kline" / "etf" / "day" / "*.csv")
        assert len(df) == 2
        assert "filename" in df.columns  # 跨品种扫描带 _file 列（3.10）
        rep = q.quality_report(tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv")
        assert set(rep) == {
            "missing_weekday",
            "zero_price_volume",
            "ohlc_out_of_bounds",
            "duplicate_dates",
            "parse_fail",
        }
    finally:
        q.close()


# ==================================================================
# T-DF02 覆盖 gaps（3.9-①）
# ==================================================================
def test_df02_gaps_holes_and_weekend(tmp_path: Path) -> None:
    _write_local(tmp_path, ["20240102", "20240103", "20240104", "20240105"])  # 周二~周五
    c = CoverageChecker(tmp_path, instrument_type="etf")
    cov = c.coverage(CODE)
    assert cov.min_dt == date(2024, 1, 2)
    assert cov.max_dt == date(2024, 1, 5)
    assert cov.distinct_dt == 4 and cov.dup_dt == 0
    # START=2024-01-01(周一) 未覆盖 → 缺口 [01-01] + [01-08..31]（周末 06/07 不构成缺失, 幂等）
    gaps = c.gaps(CODE, START, END)
    assert gaps == [(date(2024, 1, 1), date(2024, 1, 1)), (date(2024, 1, 8), END)]
    # 空洞切分: 覆盖 01-02..05 与 01-15..19 → 缺口三段（含 01-01）
    _write_local(
        tmp_path,
        [
            "20240102",
            "20240103",
            "20240104",
            "20240105",
            "20240115",
            "20240116",
            "20240117",
            "20240118",
            "20240119",
        ],
    )
    gaps2 = c.gaps(CODE, START, END)
    assert gaps2 == [
        (date(2024, 1, 1), date(2024, 1, 1)),
        (date(2024, 1, 8), date(2024, 1, 12)),
        (date(2024, 1, 22), END),
    ]


def test_df02_gaps_missing_file_full(tmp_path: Path) -> None:
    c = CoverageChecker(tmp_path, instrument_type="etf")
    assert c.gaps(CODE, START, END) == [(START, END)]  # 无文件 = 全缺失


# ==================================================================
# T-DF03 令牌桶 + 自动降档 + 熔断（3.9 防封禁）
# ==================================================================
def test_df03_token_bucket_wait() -> None:
    lt = TokenBucketLimiter(rate_per_min=60, now=lambda: 0.0)
    assert lt.wait() == 0.0  # 首次立即放行
    w = lt.wait()
    assert w >= 0.0 and w < 2.0  # 1 次/秒 × 抖动


def test_df03_auto_downshift_and_recover() -> None:
    ctl = RateLimitController(specs={"ak": RateSpec(rate_per_min=60)})
    base = ctl.current_rate("ak")
    ctl.record_rate_limited("ak")  # 命中限流 → 速率减半
    assert ctl.current_rate("ak") == base / 2
    ctl.record_success("ak")  # 成功 → 缓慢恢复
    assert base / 2 < ctl.current_rate("ak") <= base


def test_df03_circuit_breaker_pause() -> None:
    ctl = RateLimitController(
        specs={"ak": RateSpec(rate_per_min=60)},
        breaker_threshold=3,
        breaker_pause_seconds=60,
        now=lambda: 100.0,
    )
    for _ in range(3):
        ctl.record_failure("ak")
    assert ctl.is_breached("ak")
    ctl.record_success("ak")  # 成功重置计数
    assert not ctl.is_breached("ak")


# ==================================================================
# T-DF04/05 源驱动（tushare mock pro / akshare fetch_fn）
# ==================================================================
class _FakePro:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames
        self.calls: list[tuple[str, str, str]] = []

    def daily(self, ts_code=None, start_date=None, end_date=None):
        self.calls.append(("daily", start_date, end_date))
        return self._frames.get("daily", pd.DataFrame())

    def fund_daily(self, ts_code=None, start_date=None, end_date=None):
        self.calls.append(("fund_daily", start_date, end_date))
        return self._frames.get("fund_daily", pd.DataFrame())

    def fund_basic(self, market=None):
        return pd.DataFrame([{"ts_code": "510300.SH", "name": "沪深300ETF"}])

    def stock_basic(self, **kw):
        return pd.DataFrame([{"ts_code": "600000.SH", "name": "浦发银行", "list_date": "19991110"}])


def test_df04_tushare_driver_stock_and_etf() -> None:
    from mtzquant.data.drivers.tushare_driver import TushareSource

    pro = _FakePro(
        {
            "daily": _df(["20240102"]),
            "fund_daily": _df(["20240103"]),
        }
    )
    src = TushareSource(pro=pro)
    stock = src.fetch_kline("600000.SH", START, END, instrument_type="stock")
    assert list(stock.columns)[:2] == ["ts_code", "trade_date"]
    assert pro.calls[0][0] == "daily"
    etf = src.fetch_kline(CODE, START, END, instrument_type="etf")
    assert pro.calls[1][0] == "fund_daily"
    assert etf["trade_date"].iloc[0] == "20240103"
    m = src.fetch_master("etf")
    assert m["ts_code"].iloc[0] == "510300.SH"


def test_df04_tushare_token_missing() -> None:
    from mtzquant.data.drivers.tushare_driver import TushareSource

    with pytest.raises(MtzQuantError, match="token"):
        TushareSource(secrets={}).fetch_kline(CODE, START, END, instrument_type="etf")


def test_df05_akshare_driver_delegates_fetch_fn(tmp_path: Path) -> None:
    from mtzquant.data.drivers.akshare_driver import AkshareSource

    src = AkshareSource(fetch_fn=lambda code, s, e: _df(["20240102"]))
    df = src.fetch_kline(CODE, START, END, instrument_type="etf")
    assert df["trade_date"].iloc[0] == "20240102"


# ==================================================================
# T-DF06 幂等（同参数重复 fetch → 零下载零写入）
# ==================================================================
def test_df06_idempotent_repeat_fetch(tmp_path: Path) -> None:
    calls: list = []

    def fake(code, start, end, *, source, instrument_type):  # type: ignore[no-untyped-def]
        calls.append((start, end))
        return _df(_biz(pd.date_range(start, end).date))

    f = _fetcher(tmp_path, fake)
    r1 = f.fetch([CODE], START, END)[0]
    assert r1.status == "ok" and r1.added_rows > 0
    calls.clear()
    r2 = f.fetch([CODE], START, END)[0]
    assert r2.status == "skipped" and calls == []  # 零下载零写入


def test_df06_dry_run_no_write(tmp_path: Path) -> None:
    calls: list = []

    def fake(code, start, end, *, source, instrument_type):  # type: ignore[no-untyped-def]
        calls.append(1)
        return _df(["20240102"])

    f = _fetcher(tmp_path, fake)
    r = f.fetch([CODE], START, END, dry_run=True)[0]
    assert r.status == "dry_run" and calls == []
    assert not (tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv").exists()


# ==================================================================
# T-DF07 去重拒绝写盘（人为构造重复 dt → 原文件完好）
# ==================================================================
def test_df07_dup_dt_rejected_original_intact(tmp_path: Path) -> None:
    _write_local(tmp_path, ["20240102", "20240103"])
    original = (tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv").read_bytes()

    def fake(code, start, end, *, source, instrument_type):  # type: ignore[no-untyped-def]
        # 拉取含重复 dt（同一日期两行不同价）
        return pd.DataFrame(
            [
                _row("20240104"),
                _row("20240104"),
            ]
        )

    f = _fetcher(tmp_path, fake)
    r = f.fetch([CODE], date(2024, 1, 4), date(2024, 1, 5))[0]
    assert r.status == "failed"
    assert "拒绝写盘" in r.reason
    assert (tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv").read_bytes() == original


# ==================================================================
# T-DF08 中断后续传不重复请求已下区间
# ==================================================================
def test_df08_checkpoint_resume(tmp_path: Path) -> None:
    call_ranges: list[tuple[date, date]] = []
    fail_after = 2  # 第 3 次调用起抛错（模拟中断）

    def fake(code, start, end, *, source, instrument_type):  # type: ignore[no-untyped-def]
        call_ranges.append((start, end))
        if len(call_ranges) > fail_after:
            raise ConnectionError("network down")
        return _df(_biz(pd.date_range(start, end).date))

    f = _fetcher(tmp_path, fake, batch_days=20)
    r1 = f.fetch([CODE], date(2024, 1, 1), date(2024, 2, 29))[0]
    assert r1.status == "failed"
    cps = list((tmp_path / ".cache" / "fetch_checkpoint" / CODE).glob("*.json"))
    assert len(cps) >= 2  # 前两片已落 checkpoint
    # resume 续传: 只请求仍未覆盖的片
    call_ranges.clear()
    r2 = f.fetch([CODE], date(2024, 1, 1), date(2024, 2, 29), resume=True)[0]
    assert r2.status == "ok"
    # 不重复请求已下区间（r2 首个请求起点 > 1 月覆盖边缘, 且无 1 月早段请求）
    assert all(s >= date(2024, 2, 1) for s, _ in call_ranges)
    full = pd.read_csv(tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv")
    assert len(full) == len(_df(_biz(pd.date_range(date(2024, 1, 1), date(2024, 2, 29)).date)))


# ==================================================================
# T-DF09 多源顺序 fallback（akshare 失败 → tushare 成功, 报告标注实际来源）
# ==================================================================
def test_df09_multi_source_fallback(tmp_path: Path) -> None:
    def fake(code, start, end, *, source, instrument_type):  # type: ignore[no-untyped-def]
        if source == "akshare":
            raise ConnectionError("akshare down")
        return _df(_biz(pd.date_range(start, end).date))

    f = _fetcher(tmp_path, fake)
    r = f.fetch([CODE], START, END)[0]
    assert r.status == "ok"
    assert r.source == "tushare"  # 实际来源标注


def test_df09_all_sources_fail(tmp_path: Path) -> None:
    def fake(code, start, end, *, source, instrument_type):  # type: ignore[no-untyped-def]
        raise ConnectionError("all down")

    f = _fetcher(tmp_path, fake)
    r = f.fetch([CODE], START, END)[0]
    assert r.status == "failed"
    assert "全部源" in r.reason


# ==================================================================
# T-DF10 主数据 upsert + 快照留档（3.11）
# ==================================================================
def test_df10_master_upsert_and_snapshot(tmp_path: Path) -> None:
    f = _fetcher(tmp_path, fetch_fn=lambda *a, **k: _df([]))
    # 直接验证 _map_master_rows + MasterStore.upsert（网络源由 T-DF04 mock pro 覆盖）
    rows = DataFetcher._map_master_rows(
        pd.DataFrame([{"ts_code": "600000.SH", "name": "浦发银行", "list_date": "19991110"}]),
        "stock",
    )
    assert rows[0].code == "600000.SH" and rows[0].list_date == "19991110"
    added, updated = f._master.upsert(rows)
    assert added == 1
    snap = tmp_path / "master" / "snapshots"
    assert list(snap.glob("instruments_*.csv"))  # 快照留档
    # 重复刷新: 更新而非新增（幂等）
    added2, updated2 = f._master.upsert(rows)
    assert added2 == 0 and updated2 == 1


# ==================================================================
# T-DF11 目录导入（3.5 嗅探 → 归一 → 去重合并入库）
# ==================================================================
def test_df11_import_dir(tmp_path: Path) -> None:
    src = tmp_path / "import"
    src.mkdir()
    _df(["20240102", "20240103"]).to_csv(src / f"{CODE}.csv", index=False)
    f = _fetcher(tmp_path, fetch_fn=lambda *a, **k: _df([]))
    reports = f.import_dir(src)
    assert reports[0].status == "ok"
    p = tmp_path / "kline" / "etf" / "day" / f"{CODE}.csv"
    assert p.is_file()
    assert len(pd.read_csv(p)) == 2
