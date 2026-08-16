# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 01:34:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-D08：下载器 mock（幂等/去重/原子/限流/熔断, 3.9）

"""T-D08：ETF 下载器（HTTP 全 mock, 设计 3.9）——幂等/去重断言/原子性/限流/退避/熔断。"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from mtzquant.config import get_tushare_token
from mtzquant.data.fetch_etf import EtfDownloader, TokenBucketLimiter

START = date(2024, 1, 1)
END = date(2024, 1, 31)


def _row(d: str) -> dict[str, str]:
    return {
        "ts_code": "510300.SH",
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


def _make(tmp_path: Path, fetch_fn) -> EtfDownloader:  # type: ignore[no-untyped-def]
    return EtfDownloader(
        tmp_path, source="akshare", fetch_fn=fetch_fn, rate_limit_per_min=10_000_000
    )


def _write_local(tmp_path: Path, code: str = "510300.SH", days: list[str] | None = None) -> None:  # type: ignore[no-untyped-def]
    days = days or ["20240102", "20240103"]
    p = tmp_path / "kline" / "etf" / "day" / f"{code}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    _df(days).to_csv(p, index=False)
    return p


def test_range_clip_to_missing_segment(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """本地已有 01-02/03 → 请求 01-02..01-31 只拉缺失段 01-04 起（3.9 a 幂等）。"""
    _write_local(tmp_path, days=["20240102", "20240103"])
    calls: list[tuple[str, date, date]] = []

    def fake(code, start, end):  # type: ignore[no-untyped-def]
        calls.append((code, start, end))
        return _df(["20240104", "20240105"])

    dl = _make(tmp_path, fake)
    report = dl.download(["510300.SH"], date(2024, 1, 2), date(2024, 1, 31))[0]
    assert calls == [("510300.SH", date(2024, 1, 4), date(2024, 1, 31))]
    assert report.status == "ok"
    assert report.added_rows == 2
    assert report.merged_start == "20240102"
    assert report.merged_end == "20240105"


def test_covered_range_skips_fetch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _write_local(tmp_path, days=["20240102", "20240103", "20240104"])
    calls: list = []

    def fake(code, start, end):  # type: ignore[no-untyped-def]
        calls.append((code, start, end))
        return _df([])

    dl = _make(tmp_path, fake)
    report = dl.download(["510300.SH"], date(2024, 1, 2), date(2024, 1, 4))[0]
    assert report.status == "skipped"
    assert calls == []


def test_dedup_keep_latest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _write_local(tmp_path, days=["20240102"])
    # 拉取含重复日期（01-02 新值 + 01-03）→ 去重 keep=latest
    dup = pd.concat([_df(["20240102"]), _df(["20240102", "20240103"])], ignore_index=True)

    def fake(code, start, end):  # type: ignore[no-untyped-def]
        return dup

    dl = _make(tmp_path, fake)
    report = dl.download(["510300.SH"], date(2024, 1, 2), date(2024, 1, 31))[0]
    assert report.status == "ok"
    assert report.dedup_removed == 2  # 本地 1 行 + 拉取 3 行(含 2 次重复) → 唯一 2 日
    merged = pd.read_csv(tmp_path / "kline" / "etf" / "day" / "510300.SH.csv")
    assert merged["trade_date"].nunique() == len(merged)


def test_unparseable_date_refuses_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """三道防线 c: 坏日期行 → 拒绝写盘, 原文件完好。"""
    p = _write_local(tmp_path, days=["20240102"])

    def fake(code, start, end):  # type: ignore[no-untyped-def]
        return _df(["20240103", "????"])

    dl = _make(tmp_path, fake)
    report = dl.download(["510300.SH"], date(2024, 1, 2), date(2024, 1, 31))[0]
    assert report.status == "failed"
    assert "拒绝写盘" in report.reason
    # 原文件未被改动
    assert [str(r["trade_date"]) for r in pd.read_csv(p, dtype=str).to_dict("records")] == [
        "20240102"
    ]


def test_atomic_write_preserves_original_on_failure(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """写盘中抛异常 → 原文件完好（原子性）。"""
    p = _write_local(tmp_path, days=["20240102"])
    original = p.read_text(encoding="utf-8")

    def boom(code, start, end):  # type: ignore[no-untyped-def]
        return _df(["20240103"])

    dl = _make(tmp_path, boom)
    real_replace = os.replace

    def bad_replace(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("模拟写盘失败")

    monkeypatch.setattr("mtzquant.data.fetch_etf.os.replace", bad_replace)
    report = dl.download(["510300.SH"], date(2024, 1, 2), date(2024, 1, 31))[0]
    monkeypatch.setattr("mtzquant.data.fetch_etf.os.replace", real_replace)
    assert report.status == "failed"
    assert p.read_text(encoding="utf-8") == original
    assert not list(p.parent.glob("*.tmp"))  # 无残留临时文件


def test_master_seed_upserted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    def fake(code, start, end):  # type: ignore[no-untyped-def]
        return _df(["20240102"])

    dl = _make(tmp_path, fake)
    dl.download(["510300.SH"], date(2024, 1, 1), date(2024, 1, 31))
    master = pd.read_csv(tmp_path / "master" / "instruments.csv")
    assert "510300.SH" in master["code"].tolist()
    assert master.loc[master["code"] == "510300.SH", "instrument_type"].iloc[0] == "etf"


def test_token_bucket_intervals() -> None:
    """令牌桶: 相邻两次许可启动时刻间隔 ≈ interval（含 0.5~1.5× 抖动, 3.9）。"""
    fake_now = [0.0]

    def now() -> float:
        return fake_now[0]

    limiter = TokenBucketLimiter(rate_per_min=60, now=now)  # interval=1.0s, 抖动 ∈ [0.5,1.5]
    assert limiter.wait() == 0.0  # 首次立即放行
    fake_now[0] += 0.2  # 仅过 0.2s 再次请求
    second = limiter.wait()
    # interval-0.2 ∈ [0.3, 1.3]（seed=42 固定抖动, 确定性）
    assert 0.3 <= second <= 1.3


def test_retry_with_backoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """重试 max_retry 次后成功（指数退避, 3.9）。"""
    attempts: list[str] = []

    def flaky(code, start, end):  # type: ignore[no-untyped-def]
        attempts.append(code)
        if len(attempts) <= 2:
            raise RuntimeError("网络抖动")
        return _df(["20240102"])

    dl = EtfDownloader(
        tmp_path,
        source="akshare",
        fetch_fn=flaky,
        max_retry=3,
        rate_limit_per_min=10_000_000,
        now=lambda: 0.0,
    )
    report = dl.download(["510300.SH"], date(2024, 1, 1), date(2024, 1, 31))[0]
    assert report.status == "ok"
    assert len(attempts) == 3


def test_circuit_breaker_after_threshold(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """连续 5 个标的失败 → 熔断中止（3.9 防封禁）。"""
    calls: list[str] = []

    def always_fail(code, start, end):  # type: ignore[no-untyped-def]
        calls.append(code)
        raise RuntimeError("boom")

    dl = EtfDownloader(
        tmp_path,
        source="akshare",
        fetch_fn=always_fail,
        max_retry=0,
        breaker_threshold=5,
        rate_limit_per_min=10_000_000,
    )
    with pytest.raises(Exception) as exc:
        dl.download([f"51030{i}.SH" for i in range(6)], date(2024, 1, 1), date(2024, 1, 31))
    assert "熔断" in str(exc.value)
    assert len(calls) == 5  # 第 5 个失败后即熔断, 第 6 个不再尝试


def test_token_priority_env_over_secrets(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MTZQUANT_TUSHARE_TOKEN", "env_token")
    secrets = {"tushare": {"token": "file_token"}}
    assert get_tushare_token(secrets) == "env_token"  # 环境变量 > secrets.json（3.6）


def test_tushare_missing_token_structured_error(tmp_path) -> None:  # type: ignore[no-untyped-def]

    dl = EtfDownloader(
        tmp_path, source="tushare", secrets={"tushare": {}}, rate_limit_per_min=10_000_000
    )
    report = dl.download(["510300.SH"], date(2024, 1, 1), date(2024, 1, 31))[0]
    assert report.status == "failed"
    assert "token" in report.reason
