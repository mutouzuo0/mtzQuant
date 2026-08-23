# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:16:00
# @update_time        : 2026/08/23 13:20:00
# @description : G3 store/repo.py：RunRepo（run 创建/快照复用/软删除/purge）+ DetailRepo（批量插入）

"""仓储层（设计 8.3/8.7）——SQL 访问的唯一入口。

RunRepo    核心元数据: run 创建、策略快照 sha256 复用、状态更新、软删除、
           purge --force 按序物理删除（8.1 级联走 Repo）。
DetailRepo 明细批量插入（executemany, 8.7）: orders/order_events/fills/navs。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from mtzquant.core.errors import MtzQuantError
from mtzquant.store.models import (
    BacktestDailyNav,
    BacktestMetrics,
    BacktestRun,
    Fill,
    Order,
    OrderEvent,
    RunEventJournal,
    RunManifest,
    StrategySnapshot,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sharpe_from_metrics(metrics_json: str | None) -> float | None:
    """从 backtest_metrics.metrics_json 提取 sharpe（list 排序用, 8.4）。"""
    if not metrics_json:
        return None
    try:
        data = json.loads(metrics_json)
        return data.get("metrics", {}).get("sharpe")
    except (json.JSONDecodeError, AttributeError):
        return None


def _metrics_cols(metrics_json: str | None) -> dict[str, Any]:
    """从 metrics_json 提取历史列表 KPI 列（M4: 总收益/年化/最大回撤/夏普; 缺失 → None）。"""
    cols: dict[str, Any] = {
        "sharpe": None,
        "total_return": None,
        "annual_return": None,
        "max_drawdown": None,
    }
    if not metrics_json:
        return cols
    try:
        metrics = json.loads(metrics_json).get("metrics", {})
        cols["sharpe"] = metrics.get("sharpe")
        cols["total_return"] = metrics.get("total_return")
        cols["annual_return"] = metrics.get("annual_return")
        dd = metrics.get("max_drawdown")
        cols["max_drawdown"] = dd.get("value") if isinstance(dd, dict) else dd
        return cols
    except (json.JSONDecodeError, AttributeError):
        return cols


def _metrics_note(status: str | None, cols: dict[str, Any]) -> str | None:
    """P0-2: 已完成但 KPI 全空 → 给出原因（不再是无声「—」; 前端 hover 展示）。"""
    kpi_keys = ("sharpe", "total_return", "annual_return", "max_drawdown")
    if any(cols.get(k) is not None for k in kpi_keys):
        return None
    st = status or ""
    if st.startswith("completed"):
        return "该 run 未写入绩效指标（旧版引擎或未落库），可点「报告」查看或重跑"
    if st in ("running", "paused"):
        return "运行中，完成后自动写入指标"
    return "该 run 未落库指标"


class RunRepo:
    """回测运行核心元数据仓储。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------------
    def get_or_create_snapshot(
        self, *, file_name: str, code_text: str, sha256: str
    ) -> tuple[StrategySnapshot, bool]:
        """策略快照: 同 sha256 复用（T-S04）, 否则新建。返回 (快照, 是否新建)。"""
        with Session(self.engine, expire_on_commit=False) as s:
            snap = s.execute(
                select(StrategySnapshot).where(StrategySnapshot.sha256 == sha256)
            ).scalar_one_or_none()
            if snap is not None:
                return snap, False
            snap = StrategySnapshot(
                file_name=file_name,
                code_text=code_text,
                sha256=sha256,
                line_count=code_text.count("\n") + 1,
            )
            s.add(snap)
            s.commit()
            s.refresh(snap)
            return snap, True

    def create_run(
        self,
        *,
        run_id: str,
        task_name: str,
        platform: str,
        snapshot_id: int,
        params_json: str,
        status: str = "running",
        manifest_hash: str | None = None,
        mtzquant_version: str = "",
        parent_run_id: str | None = None,
    ) -> BacktestRun:
        """创建 run（params_json 必须已脱敏, 3.6/8.3.1）。

        M4-W2: 已存在（serve 预建 running 行）→ 覆写终态字段（upsert 语义, 8.3.1）。
        """
        with Session(self.engine, expire_on_commit=False) as s:
            existing = s.get(BacktestRun, run_id)
            if existing is not None:
                existing.task_name = task_name
                existing.platform = platform
                existing.strategy_snapshot_id = snapshot_id
                existing.parent_run_id = parent_run_id
                existing.params_json = params_json
                existing.status = status
                existing.manifest_hash = manifest_hash
                existing.mtzquant_version = mtzquant_version
                s.commit()
                return existing
            run = BacktestRun(
                id=run_id,
                task_name=task_name,
                platform=platform,
                strategy_snapshot_id=snapshot_id,
                parent_run_id=parent_run_id,
                params_json=params_json,
                status=status,
                manifest_hash=manifest_hash,
                mtzquant_version=mtzquant_version,
            )
            s.add(run)
            s.commit()
            return run

    def update_status(self, run_id: str, status: str, *, error_log: str | None = None) -> None:
        with Session(self.engine, expire_on_commit=False) as s:
            vals: dict[str, Any] = {"status": status, "finished_at": datetime.now().astimezone()}
            if error_log is not None:
                vals["error_log"] = error_log
            s.execute(update(BacktestRun).where(BacktestRun.id == run_id).values(**vals))
            s.commit()

    def eliminate_run(self, run_id: str, reason: str) -> None:
        """淘汰留痕（5.8.2, M3-U3）: 候选 run 保留 + 淘汰原因（list 默认折叠）。"""
        if not reason:
            raise MtzQuantError(
                f"淘汰原因不能为空: {run_id}", stage="store", hint="5.8.2 淘汰须留痕"
            )
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(
                update(BacktestRun).where(BacktestRun.id == run_id).values(eliminated_reason=reason)
            )
            s.commit()

    def get(self, run_id: str) -> BacktestRun | None:
        with Session(self.engine, expire_on_commit=False) as s:
            return s.get(BacktestRun, run_id)

    def list_runs(
        self, *, sort_by: str = "started_at", limit: int = 50, include_eliminated: bool = False
    ) -> list[dict[str, Any]]:
        """列出 run（M3-U3: 默认折叠被淘汰候选, --include-eliminated 显式显示, 5.8.2）。

        M4: 一律 LEFT JOIN metrics 附加 KPI 列（sharpe/total_return/annual_return/max_drawdown）,
        Web 历史页与 CLI 同源（9.1）; sort_by=sharpe 时无指标行沉底（8.4）。
        """
        not_elim = BacktestRun.eliminated_reason.is_(None)
        with Session(self.engine, expire_on_commit=False) as s:
            rows = s.execute(
                select(BacktestRun, BacktestMetrics.metrics_json)
                .join(BacktestMetrics, BacktestRun.id == BacktestMetrics.run_id, isouter=True)
                .where(BacktestRun.deleted_at.is_(None))
                .where(not_elim if not include_eliminated else True)  # type: ignore[arg-type]
                .order_by(BacktestRun.started_at.desc())
                .limit(limit)
            ).all()
            runs: list[dict[str, Any]] = []
            for run, metrics_json in rows:
                d = self._run_dict(run)
                cols = _metrics_cols(metrics_json)
                d.update(cols)
                d["metrics_note"] = _metrics_note(run.status, cols)  # P0-2 指标缺失原因
                runs.append(d)
            if sort_by == "sharpe":
                runs.sort(key=lambda r: (r.get("sharpe") is None, -(r.get("sharpe") or 0.0)))
            return runs

    @staticmethod
    def _run_dict(run: BacktestRun) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "task_name": run.task_name,
            "platform": run.platform,
            "status": run.status,
            "manifest_hash": run.manifest_hash,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "mtzquant_version": run.mtzquant_version,
            "error_log": run.error_log,
            "eliminated_reason": run.eliminated_reason,
        }

    def set_manifest(self, run_id: str, manifest_json: str, manifest_hash: str) -> None:
        """写入确定性重放清单（8.8; run_manifest 1:1）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            existing = s.get(RunManifest, run_id)
            if existing is None:
                s.add(
                    RunManifest(
                        run_id=run_id, manifest_json=manifest_json, manifest_hash=manifest_hash
                    )
                )
            else:
                existing.manifest_json = manifest_json
                existing.manifest_hash = manifest_hash
            s.execute(
                update(BacktestRun)
                .where(BacktestRun.id == run_id)
                .values(manifest_hash=manifest_hash)
            )
            s.commit()

    def get_manifest(self, run_id: str) -> tuple[str, str] | None:
        """读取 (manifest_json, manifest_hash); 缺失 → None。"""
        with Session(self.engine, expire_on_commit=False) as s:
            row = s.get(RunManifest, run_id)
            if row is None:
                return None
            return row.manifest_json, row.manifest_hash

    def set_metrics(self, run_id: str, metrics_json: str, metrics_version: str) -> None:
        """写入绩效指标（8.4; backtest_metrics 1:1）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            existing = s.get(BacktestMetrics, run_id)
            if existing is None:
                s.add(
                    BacktestMetrics(
                        run_id=run_id,
                        metrics_json=metrics_json,
                        metrics_version=metrics_version,
                    )
                )
            else:
                existing.metrics_json = metrics_json
                existing.metrics_version = metrics_version
            s.commit()

    def get_metrics(self, run_id: str) -> dict[str, Any] | None:
        """读取指标 dict（metrics_json 解析; 缺失 → None）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            row = s.get(BacktestMetrics, run_id)
            if row is None:
                return None
            return json.loads(row.metrics_json)

    def get_params(self, run_id: str) -> dict[str, Any] | None:
        """读取 params_json（已脱敏, 3.6; 缺失 → None）。"""
        run = self.get(run_id)
        if run is None or not run.params_json:
            return None
        try:
            return json.loads(run.params_json)
        except json.JSONDecodeError:
            return None

    def get_navs(self, run_id: str) -> list[dict[str, Any]]:
        """读取每日净值明细（backtest_daily_nav, 8.3.4; compare/report 用）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            rows = s.execute(
                select(BacktestDailyNav)
                .where(BacktestDailyNav.run_id == run_id)
                .order_by(BacktestDailyNav.trade_date)
            ).scalars()
            return [
                {
                    "trade_date": r.trade_date,
                    "strategy_nav": r.strategy_nav,
                    "benchmark_nav": r.benchmark_nav,
                    "cash": r.cash,
                    "positions_value": r.positions_value,
                    "total_value": r.total_value,
                    "drawdown": r.drawdown,
                    "open_positions": r.open_positions,
                }
                for r in rows
            ]

    def get_snapshot_code(self, run_id: str) -> str | None:
        """读取该 run 策略源码快照（strategy_snapshot.code_text, P2 diff 用）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            run = s.get(BacktestRun, run_id)
            if run is None:
                return None
            snap = s.get(StrategySnapshot, run.strategy_snapshot_id)
            return snap.code_text if snap is not None else None

    def get_snapshot_info(self, run_id: str) -> dict[str, Any] | None:
        """读取该 run 策略快照元信息（源码/文件名/sha256/行数, Web 源码查看用）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            run = s.get(BacktestRun, run_id)
            if run is None:
                return None
            snap = s.get(StrategySnapshot, run.strategy_snapshot_id)
            if snap is None:
                return None
            return {
                "file_name": snap.file_name,
                "code": snap.code_text,
                "sha256": snap.sha256,
                "line_count": snap.line_count,
            }

    def lineage(self) -> list[dict[str, Any]]:
        """全量 run 谱系节点（含 parent_run_id/收益摘要, P2 lineage 用）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            rows = s.execute(
                select(BacktestRun, BacktestMetrics.metrics_json)
                .join(BacktestMetrics, BacktestRun.id == BacktestMetrics.run_id, isouter=True)
                .where(BacktestRun.deleted_at.is_(None))
                .order_by(BacktestRun.started_at)
            ).all()
            out: list[dict[str, Any]] = []
            for run, metrics_json in rows:
                node: dict[str, Any] = {
                    "run_id": run.id,
                    "task_name": run.task_name,
                    "platform": run.platform,
                    "parent_run_id": run.parent_run_id,
                    "status": run.status,
                    "params_json": run.params_json,
                }
                m = _sharpe_from_metrics(metrics_json)
                node["sharpe"] = m
                out.append(node)
            return out

    # ------------------------------------------------------------------
    def soft_delete(self, run_id: str) -> None:
        """软删除（deleted_at 标记, 8.1 级联优先软删）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(
                update(BacktestRun)
                .where(BacktestRun.id == run_id)
                .values(deleted_at=datetime.now().astimezone())
            )
            s.commit()

    def purge_run(self, run_id: str, *, force: bool = False) -> int:
        """物理删除该 run 全量记录（明细→元数据; 需 force, 8.1）。"""
        if not force:
            raise MtzQuantError(
                f"物理删除需 --force: {run_id}",
                stage="store",
                hint="默认软删除（soft_delete）; purge 会永久清除明细与指标",
            )
        with Session(self.engine, expire_on_commit=False) as s:
            order_ids = [
                r[0] for r in s.execute(select(Order.order_id).where(Order.run_id == run_id))
            ]
            s.execute(delete(OrderEvent).where(OrderEvent.run_id == run_id))
            s.execute(delete(RunEventJournal).where(RunEventJournal.run_id == run_id))
            s.execute(delete(Fill).where(Fill.run_id == run_id))
            s.execute(delete(Order).where(Order.run_id == run_id))
            s.execute(delete(BacktestDailyNav).where(BacktestDailyNav.run_id == run_id))
            s.execute(delete(BacktestMetrics).where(BacktestMetrics.run_id == run_id))
            s.execute(delete(RunManifest).where(RunManifest.run_id == run_id))
            run = s.get(BacktestRun, run_id)
            if run is not None:
                s.delete(run)
            s.commit()
        return len(order_ids)


class DetailRepo:
    """明细批量写入（8.7 executemany; 与 WriteBuffer 配合）。

    空列表直接返回（防 SQLAlchemy 空 list 生成「仅默认列」INSERT 触发 NOT NULL 违规）。
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def insert_orders(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(Order), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_events(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(OrderEvent), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_fills(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(Fill), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_navs(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with Session(self.engine, expire_on_commit=False) as s:
            s.execute(insert(BacktestDailyNav), [dict(r) for r in rows])
            s.commit()
        return len(rows)

    def insert_journal(self, rows: Sequence[dict[str, Any]]) -> int:
        """事件日志批量写入（M4-W3, 8.3.7; (run_id,event_seq) 幂等 upsert 语义）。"""
        if not rows:
            return 0
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        with Session(self.engine, expire_on_commit=False) as s:
            for r in rows:
                s.execute(
                    sqlite_insert(RunEventJournal)
                    .values(**r)
                    .on_conflict_do_nothing(index_elements=["run_id", "event_seq"])
                )
            s.commit()
        return len(rows)

    def journal(
        self, run_id: str, after_seq: int = 0, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        """读取事件日志（按 event_seq 升序; after_seq 断点续传, M4-W3 resume 补帧）。"""
        with Session(self.engine, expire_on_commit=False) as s:
            rows = s.execute(
                select(RunEventJournal)
                .where(RunEventJournal.run_id == run_id)
                .where(RunEventJournal.event_seq > after_seq)
                .order_by(RunEventJournal.event_seq)
                .limit(limit)
            ).scalars()
            return [
                {
                    "type": r.kind,
                    "run_id": r.run_id,
                    "ts": r.ts,
                    "event_seq": r.event_seq,
                    "committed": r.committed,
                    "data": json.loads(r.payload_json),
                }
                for r in rows
            ]
