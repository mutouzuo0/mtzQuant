# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:42:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-S06：导出物与 DB 同源一致（逐行比对）+ summary.json schema（设计 9.1）

"""T-S06：RunStore.export 导出物与输入 Bundle 逐行一致 + summary.json 可解析。"""

from __future__ import annotations

import json

import pandas as pd

from mtzquant.engine.export import ExportBundle, RunStore


def _bundle() -> ExportBundle:
    navs = [
        {
            "trade_date": "2026-01-02",
            "nav": 1.0,
            "cash": 1e6,
            "positions_value": 0.0,
            "total_value": 1e6,
            "drawdown": 0.0,
            "open_positions": 0,
        },
        {
            "trade_date": "2026-01-03",
            "nav": 1.01,
            "cash": 990_000.0,
            "positions_value": 30_000.0,
            "total_value": 1_020_000.0,
            "drawdown": 0.0,
            "open_positions": 1,
        },
    ]
    orders = [
        {"order_id": "o1", "code": "510300.SH", "side": "buy", "status": "filled", "qty": 3000}
    ]
    fills = [
        {
            "order_id": "o1",
            "code": "510300.SH",
            "side": "buy",
            "price": 10.01,
            "volume": 3000,
            "amount": 30030.0,
            "commission": 5.0,
        }
    ]
    events = [{"order_id": "o1", "event_type": "accepted", "event_time": "2026-01-02 15:00"}]
    return ExportBundle(
        run_id="r_export_1",
        navs=navs,
        orders=orders,
        fills=fills,
        events=events,
        fees={"commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.0},
        status="completed_exact",
        degradations=[],
        manifest={"manifest_hash": "abc123"},
        task={"task_name": "t"},
        strategy_code="# demo\n",
    )


def test_ts06_export_files_match_bundle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = RunStore(tmp_path / "results")
    out = store.export(_bundle())
    assert out.is_dir()

    # 逐行比对（9.1 同源: 导出 = 输入投影）
    nav_csv = pd.read_csv(out / "daily_stats.csv")
    assert len(nav_csv) == 2
    assert nav_csv["nav"].tolist() == [1.0, 1.01]
    orders_csv = pd.read_csv(out / "orders.csv")
    assert orders_csv["order_id"].tolist() == ["o1"]
    assert len(pd.read_csv(out / "fills.csv")) == 1
    assert len(pd.read_csv(out / "order_events.csv")) == 1

    # summary.json schema（8.4: 关键指标字段存在且可机读）
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    m = summary["metrics"]
    assert "total_return" in m and "sharpe" in m and "max_drawdown" in m
    assert summary["status"] == "completed_exact"
    assert summary["fees"]["commission"] == 5.0

    # manifest/task/策略快照
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_hash"] == "abc123"
    assert (out / "strategy_snapshot.py").read_text(encoding="utf-8") == "# demo\n"


def test_ts06_empty_nav_graceful(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = RunStore(tmp_path / "results")
    b = _bundle()
    b.navs = []
    out = store.export(b)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert "error" in summary  # 无净值时不炸（9.1 防御）
