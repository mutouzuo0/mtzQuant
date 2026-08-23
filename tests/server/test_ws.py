# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 09:20:00
# @update_time        : 2026/08/18 23:25:00
# @description : T-W01a..d：W0 最小可视版——app 装配/WS 信封一致+多客户端/桥接 fan-out/页面覆盖
"""T-W01（M2-W0 Web 最小可视版）——fastapi TestClient + 真实 BacktestRuntime。

覆盖:
  T-W01a  app 装配: GET / 单页 / /api/runtime / /static/vendor/echarts.min.js
  T-W01b  WS 信封与 ResultStore 逐字段一致（6.3 冻结字段名） + 多客户端 + seq 单调 + committed 快照
  T-W01c  会话桥接: 事件实时 fan-out 到 WS 订阅者（跨线程 call_soon_threadsafe 路径）
  T-W01d  页面逻辑: 各事件类型消费分支齐全（progress/daily_nav/fill/log/status）
        + 净值增量 setOption（appendData 对 line 系列无效, 已弃用）
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mtzquant.server.app import _STATIC_DIR, create_app
from mtzquant.server.run_local import BacktestRuntime
from mtzquant.server.ws import ENVELOPE_KEYS, envelope
from tests.fixtures.backtest_env import make_backtest_env


def _read_all(ws: TestClient, n: int) -> list[dict]:
    """从 WS 读 n 条（回放已入队, 同步返回）。"""
    return [json.loads(ws.receive_text()) for _ in range(n)]


# ------------------------------------------------------------------
# T-W01a app 装配
# ------------------------------------------------------------------
def test_create_app_assembly() -> None:
    app = create_app(None)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        for marker in (
            'id="bar"',
            'id="navChart"',
            'id="statusBadge"',
            "echarts.min.js",
            "onDailyNav",
        ):
            assert marker in r.text, f"页面缺标记: {marker}"
        r2 = client.get("/api/runtime")
        assert r2.status_code == 200
        assert r2.json()["status"] == "idle"
        r3 = client.get("/static/vendor/echarts.min.js")
        assert r3.status_code == 200
        assert len(r3.content) > 100_000  # 本地化 ECharts（禁 CDN, 离线可用）


# ------------------------------------------------------------------
# T-W01b 信封一致 + 多客户端一致 + 回放快照
# ------------------------------------------------------------------
def test_envelope_consistency_and_multi_client(tmp_path) -> None:
    env = make_backtest_env(tmp_path, n=40)
    runtime = BacktestRuntime(env.task, settings=env.settings)
    runtime.start()
    runtime.join(timeout=30)
    assert runtime.status == "completed_exact"
    records = runtime.store.all_records()
    assert records, "事件流不应为空"
    kinds = {r.kind for r in records}
    assert {"progress", "daily_nav", "status"} <= kinds  # 进度/日净/终态全覆盖

    expected = [envelope(r) for r in records]
    app = create_app(runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as ws1:
            with client.websocket_connect("/api/ws") as ws2:
                got1 = _read_all(ws1, len(expected))
                got2 = _read_all(ws2, len(expected))

    # 信封字段与 6.3 完全一致
    for msg in got1:
        assert set(msg) == set(ENVELOPE_KEYS)
        assert isinstance(msg["event_seq"], int)
        assert msg["run_id"] == runtime.run_id
        assert isinstance(msg["ts"], int) and msg["ts"] > 0
        assert isinstance(msg["data"], dict)
    # 与 ResultStore 逐字段一致（直接转发不新造）
    assert got1 == expected
    # 多客户端同看事件序列一致
    assert got2 == expected
    # 事件序严格单调（确定性, 8.8）
    seqs = [m["event_seq"] for m in got1]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # 回放已落库事件 committed:true 快照（_run finally flush, 6.3）
    assert all(m["committed"] is True for m in got1)


# ------------------------------------------------------------------
# T-W01c 会话桥接: 实时 fan-out（跨线程投递路径）
# ------------------------------------------------------------------
def test_session_bridge_live_fanout(tmp_path) -> None:
    env = make_backtest_env(tmp_path, n=20)
    runtime = BacktestRuntime(env.task, settings=env.settings)  # 不 start: 测纯实时路径
    app = create_app(runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as ws:
            # 模拟回测线程实时推事件（emit → publish_hook → call_soon_threadsafe）
            runtime.store.emit("log", {"kind": "ping", "amount": 1.0})
            runtime.store.emit(
                "progress",
                {
                    "trade_date": "2020-01-03",
                    "day_index": 0,
                    "total_days": 20,
                    "percent": 0.05,
                    "elapsed_seconds": 0.0,
                    "eta_seconds": 0,
                },
            )
            m1 = json.loads(ws.receive_text())
            m2 = json.loads(ws.receive_text())
            assert m1["type"] == "log" and m1["data"]["kind"] == "ping"
            assert m2["type"] == "progress"
            assert m2["event_seq"] == m1["event_seq"] + 1  # 单调续接
            assert m1["committed"] is False  # 实时事件未落库
            assert m2["run_id"] == runtime.run_id


# ------------------------------------------------------------------
# T-W01d 页面消费各事件类型（单页逻辑覆盖）
# ------------------------------------------------------------------
def test_page_event_handlers() -> None:
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for t in ("progress", "daily_nav", "fill", "log", "status"):
        assert f'case "{t}"' in html, f"页面缺 {t} 消费分支"
    # 净值节流全量刷新（appendData 对 line 无效, 已弃用）
    assert "flushChart" in html and "setOption({series:" in html
    assert "new WebSocket" in html  # WS 订阅入口
    assert "mtzquant report" in html  # 终态提示
