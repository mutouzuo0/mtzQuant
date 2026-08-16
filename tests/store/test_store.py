# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:20:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-S01/S03/S04/S05：store 模型约束/崩溃半程数据/快照 sha256 复用/参数脱敏

"""T-S01/S03/S04/S05：持久化（设计 8.3/8.7）。"""

from __future__ import annotations

import pytest

from mtzquant.config import sanitize_params
from mtzquant.store.models import BacktestDailyNav, init_db
from mtzquant.store.repo import DetailRepo, RunRepo


@pytest.fixture()
def engine(tmp_path):  # type: ignore[no-untyped-def]
    return init_db(f"sqlite:///{tmp_path / 'zq.db'}")


def test_ts01_create_run_with_snapshot_fk(engine) -> None:  # type: ignore[no-untyped-def]
    """T-S01: 核心元数据物理 FK 成立; 明细表可写可查。"""
    repo = RunRepo(engine)
    snap, created = repo.get_or_create_snapshot(
        file_name="s.py", code_text="def init(): pass", sha256="a" * 64
    )
    assert created is True
    run = repo.create_run(
        run_id="r_1_abc",
        task_name="t",
        platform="native",
        snapshot_id=snap.id,
        params_json="{}",
    )
    assert run.id == "r_1_abc"
    assert repo.get("r_1_abc") is not None
    # 明细批量写（逻辑 FK）
    det = DetailRepo(engine)
    det.insert_navs(
        [
            {
                "run_id": "r_1_abc",
                "trade_date": "2026-01-02",
                "strategy_nav": 1.0,
                "cash": 1e6,
                "positions_value": 0.0,
                "total_value": 1e6,
                "drawdown": 0.0,
                "open_positions": 0,
            }
        ]
    )
    with engine.connect() as conn:
        n = conn.execute(BacktestDailyNav.__table__.select()).fetchall()
    assert len(n) == 1


def test_ts04_snapshot_sha256_reuse(engine) -> None:  # type: ignore[no-untyped-def]
    """T-S04: 同 sha256 复用同一快照（不重复建行）。"""
    repo = RunRepo(engine)
    code = "def f(): return 1\n"
    sha = "b" * 64
    snap1, c1 = repo.get_or_create_snapshot(file_name="a.py", code_text=code, sha256=sha)
    snap2, c2 = repo.get_or_create_snapshot(file_name="a.py", code_text=code, sha256=sha)
    assert c1 is True and c2 is False
    assert snap1.id == snap2.id


def test_ts03_crash_keeps_flushed_rows(engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """T-S03: 崩溃半程——已 flush 入库的数据保留（缓冲未刷的丢弃, 宁慢不丢）。"""
    from mtzquant.store.write_buffer import BufferConfig, WriteBuffer

    repo = RunRepo(engine)
    snap, _ = repo.get_or_create_snapshot(file_name="s.py", code_text="x", sha256="c" * 64)
    repo.create_run(
        run_id="r_crash", task_name="t", platform="native", snapshot_id=snap.id, params_json="{}"
    )
    det = DetailRepo(engine)
    flushed_rows: list = []
    buf = WriteBuffer(
        BufferConfig(batch_size=2, flush_interval_ms=10_000, buffer_max_rows=100),
        flush_callback=lambda rows: (det.insert_navs(rows), flushed_rows.extend(rows))[0],
    )
    for i in range(3):
        buf.add(
            {
                "run_id": "r_crash",
                "trade_date": f"2026-01-0{i + 1}",
                "strategy_nav": 1.0,
                "cash": 1e6,
                "positions_value": 0.0,
                "total_value": 1e6,
                "drawdown": 0.0,
                "open_positions": 0,
            }
        )
    # 只 add 不 flush → 首 2 条达 batch_size 自动落库, 第 3 条留在缓冲（模拟崩溃丢失）
    with engine.connect() as conn:
        n = conn.execute(BacktestDailyNav.__table__.select()).fetchall()
    assert len(n) == 2
    buf.flush()  # 结束强制 flush → 第 3 条也入库
    with engine.connect() as conn:
        n = conn.execute(BacktestDailyNav.__table__.select()).fetchall()
    assert len(n) == 3


def test_ts05_params_sanitize() -> None:
    """T-S05: params_json 入库前脱敏（token/api_key/secret/webhook/password, 3.6）。"""
    cleaned = sanitize_params(
        {
            "strategy": {"file": "s.py"},
            "tushare": {"token": "real-token"},
            "api_key": "k",
            "nested": {"secret": "s", "safe": 1},
            "list": [{"password": "p"}],
        }
    )
    assert cleaned["tushare"]["token"] == ""
    assert cleaned["api_key"] == ""
    assert cleaned["nested"]["secret"] == ""
    assert cleaned["nested"]["safe"] == 1
    assert cleaned["list"][0]["password"] == ""
    assert cleaned["strategy"]["file"] == "s.py"  # 非敏感保留
