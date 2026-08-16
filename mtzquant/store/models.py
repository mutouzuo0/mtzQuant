# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:10:00
# @update_time        : 2026/08/16 21:59:08
# @description : G1 store/models.py：SQLAlchemy2 全表模型（物理FK 核心/逻辑FK 明细, 设计 8.3 定稿）

"""持久化 ORM 模型（设计 8.3，Schema 冻结于计划 §5）。

外键分层（8.1 v1.1 折中）:
  核心元数据表 → 物理外键（backtest_run / strategy_snapshot / run_manifest / backtest_metrics）
  海量事件明细 → 逻辑外键（orders / order_events / fills / backtest_daily_nav）
级联删除统一走 Repo（软删除优先, 物理删除需 --force）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now().astimezone()


# 运行状态（8.3.1 七态; 终态: completed_exact/completed_degraded/stopped/error）
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_COMPLETED_EXACT = "completed_exact"
RUN_COMPLETED_DEGRADED = "completed_degraded"
RUN_STOPPED = "stopped"
RUN_ERROR = "error"


class StrategySnapshot(Base):
    """策略代码快照（8.3.2; sha256 唯一, 同码复用）。"""

    __tablename__ = "strategy_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    code_text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BacktestRun(Base):
    """回测主表（8.3.1; id=r_<ts>_<hash>）。"""

    __tablename__ = "backtest_run"
    __table_args__ = (
        Index("ix_run_status_started", "status", "started_at"),
        Index("ix_run_platform", "platform"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_name: Mapped[str] = mapped_column(String(255), default="")
    platform: Mapped[str] = mapped_column(String(32), default="native")
    strategy_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_snapshot.id"), index=True
    )  # 物理 FK（核心表, 8.1）
    parent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)  # 逻辑自关联
    params_json: Mapped[str] = mapped_column(Text, default="{}")  # 已脱敏（3.6）
    status: Mapped[str] = mapped_column(String(32), default=RUN_RUNNING)
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_log: Mapped[str | None] = mapped_column(Text)
    eliminated_reason: Mapped[str | None] = mapped_column(Text)  # M3-U3 淘汰留痕（5.8.2）
    mtzquant_version: Mapped[str] = mapped_column(String(32), default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 软删除


class RunManifest(Base):
    """确定性重放清单（8.8; 与 run 1:1 物理 FK）。"""

    __tablename__ = "run_manifest"

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("backtest_run.id"), primary_key=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    manifest_hash: Mapped[str] = mapped_column(String(64))


class BacktestMetrics(Base):
    """绩效指标（8.4 全量, gross/net 双口径; 与 run 1:1 物理 FK）。"""

    __tablename__ = "backtest_metrics"

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("backtest_run.id"), primary_key=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")  # 8.4 全指标序列化
    metrics_version: Mapped[str] = mapped_column(String(16), default="")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Order(Base):
    """订单（8.3.3 明细表, 逻辑 FK）。"""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_run_submitted", "run_id", "submitted_at"),
        Index("ix_orders_run_code", "run_id", "code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)  # 逻辑 FK
    strategy_snapshot_id: Mapped[int | None] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(16))
    style: Mapped[str] = mapped_column(String(24))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    order_api: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    eligible_fill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_in_force: Mapped[str] = mapped_column(String(8), default="day")
    remaining_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OrderEvent(Base):
    """订单事件（8.3.3 审计/对账; 每次状态迁移一行）。"""

    __tablename__ = "order_events"
    __table_args__ = (
        Index("ix_events_run_time", "run_id", "event_time"),
        Index("ix_events_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(24))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float | None] = mapped_column(Float)
    info_json: Mapped[str | None] = mapped_column(Text)


class Fill(Base):
    """成交（8.3.3 明细表; 一订单可多笔部分成交）。"""

    __tablename__ = "fills"
    __table_args__ = (
        Index("ix_fills_run_time", "run_id", "fill_time"),
        Index("ix_fills_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_snapshot_id: Mapped[int | None] = mapped_column(Integer, index=True)
    fill_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 回测内
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    code: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    stamp_tax: Mapped[float] = mapped_column(Float, default=0.0)
    transfer_fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_fee: Mapped[float] = mapped_column(Float, default=0.0)
    order_api: Mapped[str] = mapped_column(String(64), default="")
    bar_volume: Mapped[float] = mapped_column(Float, default=0.0)  # 容量证据（8.4.4, M2-P4）
    participation_rate: Mapped[float] = mapped_column(Float, default=0.0)


class BacktestDailyNav(Base):
    """每日净值（8.3.4 明细表; (run_id, trade_date) 唯一）。"""

    __tablename__ = "backtest_daily_nav"
    __table_args__ = (
        UniqueConstraint("run_id", "trade_date", name="uq_nav_run_date"),
        Index("ix_nav_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    strategy_nav: Mapped[float] = mapped_column(Float)
    benchmark_nav: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    positions_value: Mapped[float] = mapped_column(Float)
    total_value: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)


class RunEventJournal(Base):
    """回测事件日志（append-only, 8.3.7, M4-W3）——WS 断线补帧/逐日回放的唯一数据源。

    与 ResultStore 信封同构（6.3: kind/run_id/event_seq/committed/ts/payload）;
    (run_id, event_seq) 唯一; 由 run_task 的 flush 钩子批量写入（8.7 写缓冲）。
    """

    __tablename__ = "run_event_journal"
    __table_args__ = (
        UniqueConstraint("run_id", "event_seq", name="uq_journal_run_seq"),
        Index("ix_journal_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    event_seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    committed: Mapped[bool] = mapped_column(default=False)
    ts: Mapped[int] = mapped_column(Integer, default=0)  # 发布毫秒时间戳（8.8）
    payload_json: Mapped[str] = mapped_column(Text)


# ------------------------------------------------------------------
# 初始化 / 连接
# ------------------------------------------------------------------
def init_db(url: str = "sqlite:///./mtzquant.db") -> Any:
    """建库（WAL + 单写连接, 8.1/8.7）；返回 engine。"""
    from sqlalchemy import create_engine, event

    engine = create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("sqlite"):
        from sqlalchemy import Engine

        @event.listens_for(Engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    Base.metadata.create_all(engine)
    return engine


# backtest_trades 聚合视图（8.3.3: fills 按订单聚合业务视图; 建库后注册）
_TRADES_VIEW_SQL = text(
    """
    CREATE VIEW IF NOT EXISTS backtest_trades AS
    SELECT order_id, code, side, SUM(volume) AS volume, SUM(amount) AS amount,
           SUM(commission + stamp_tax + transfer_fee + slippage_cost) AS total_fee,
           COUNT(*) AS fill_count
    FROM fills GROUP BY order_id, code, side
    """
)
