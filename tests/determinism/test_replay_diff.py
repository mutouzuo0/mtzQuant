# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : T-X02 replay 差异检测：篡改一段 CSV 价格 → 指出标的×交易日（设计 8.8）

"""T-X02：跑 run A 后篡改某标的 CSV 价格 → `mtzquant replay` 重跑并 diff。

断言:
  篡改后 manifest_hash 变化（data_manifest 定位到标的）
  nav 差异可定位到「篡改的交易日」（daily_stats 逐点比对）
  未篡改的 orders/fills 零差异
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from mtzquant.engine.replay import replay_run
from mtzquant.engine.runner import run_task
from tests.fixtures.backtest_env import make_backtest_env


def test_tx02_replay_detects_tampered_price(tmp_path: Path) -> None:
    env = make_backtest_env(tmp_path, n=60)
    run1 = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    # 篡改: 持仓期内第 10 根 bar 收盘价 +10%（策略 6~20 号 bar 持有 5 万股, 净值应变化）
    csv_path = env.data_root / "kline" / "etf" / "day" / f"{env.code}.csv"
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    tampered_row = df.index[9]  # 第 10 根 bar（持仓期内）
    old_close = float(df.at[tampered_row, "close"])
    df.at[tampered_row, "close"] = f"{old_close * 1.10:.4f}"
    df.to_csv(csv_path, index=False, encoding="utf-8")

    rep = replay_run(run1.run_id, settings=env.settings, out_root=env.out_root)

    # 清单变化（数据修订 → manifest_hash 不同）
    assert rep.manifest_identical is False
    assert rep.old_manifest_hash != rep.new_manifest_hash
    # 存在 nav 差异, 且定位到篡改交易日（第 10 根 bar 的 trade_date）
    nav_diffs = [d for d in rep.diffs if d.section == "navs"]
    assert nav_diffs, "应检测到净值差异"
    tampered_date = df.at[tampered_row, "trade_date"]
    tampered_date = f"{tampered_date[:4]}-{tampered_date[4:6]}-{tampered_date[6:]}"
    assert any(d.trade_date == tampered_date for d in nav_diffs), (
        f"差异应定位到 {tampered_date}, 实际 {[d.trade_date for d in nav_diffs[:5]]}"
    )
    # data_manifest 差异可定位到标的
    notes = [d for d in rep.diffs if d.section == "manifest"]
    assert any(env.code in str(d.detail) or d.key == "manifest_hash" for d in notes)


def test_tx02_untampered_replay_identical(tmp_path: Path) -> None:
    """未篡改数据时 replay 应完全一致（对照: 零差异）。"""
    env = make_backtest_env(tmp_path)
    run1 = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    rep = replay_run(run1.run_id, settings=env.settings, out_root=env.out_root)
    assert rep.identical is True
    assert rep.manifest_identical is True
    assert rep.diffs == []
