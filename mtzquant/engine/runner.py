# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : I1 runner.run_task：会话装配 → 引擎驱动 → 导出 → DB 入库（设计 5.1/8.8/9.1）

"""run_task（阶段 I 生产路径）——CLI `mtzquant run` 的核心编排。

流程:
  1. 数据管道装配（CsvSourceDriver → DataCache → MarketDataProvider + TradeCalendar 推导）;
  2. BacktestSession + UnifiedBacktestEngine 驱动（十阶段主循环）;
  3. RunManifest 采集（8.8: strategy/data/config 哈希聚合）;
  4. RunStore.export 结果导出（9.1: CSV/JSON + 指标汇总）;
  5. RunRepo/DetailRepo 入库（快照 sha256 复用/脱敏/明细, 8.3/3.6）。

确定性（8.8）: run_id = r_<毫秒时间戳>_<任务哈希8>; 明细一律源自会话事件流同源投影。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from mtzquant import __version__
from mtzquant.adapters import native  # noqa: F401  # 注册 native 适配器（4.3 一行注册）
from mtzquant.config import Settings, sanitize_params
from mtzquant.core.errors import MtzQuantError
from mtzquant.data.cache import DataCache
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.drivers.csv_driver import CsvSourceDriver
from mtzquant.data.fundamentals import FundamentalsStore
from mtzquant.data.normalizer import DataNormalizer
from mtzquant.data.provider import MarketDataProvider
from mtzquant.engine.engine import UnifiedBacktestEngine
from mtzquant.engine.export import ExportBundle, RunStore
from mtzquant.engine.manifest import build_manifest, config_hash
from mtzquant.engine.results import FlushPolicy, ResultStore
from mtzquant.engine.session import BacktestSession, TaskConfig, normalize_universe
from mtzquant.store.models import init_db
from mtzquant.store.repo import DetailRepo, RunRepo


@dataclass
class RunResult:
    """一次 `mtzquant run` 的完整产出。"""

    run_id: str
    bundle: ExportBundle
    manifest: dict[str, Any]
    manifest_hash: str
    out_dir: Path
    db: Engine | None = None
    status: str = "completed_exact"
    error_log: str | None = None
    timing: dict[str, float] = field(default_factory=dict)  # 性能分解（T-P01/T-P02, 秒）


@dataclass
class Pipeline:
    """数据管道装配（CLI/测试共享; 单标的池 → Provider + 日历）。"""

    driver: CsvSourceDriver
    provider: MarketDataProvider
    calendar: TradeCalendar
    settings: Settings


def build_pipeline(settings: Settings, codes: list[str]) -> Pipeline:
    """装配三段式管道并预加载 universe（3.7 window 预载, 3.12-④ 日历推导）。"""
    lcs = settings.data.local_csv
    driver = CsvSourceDriver(lcs)
    cache = DataCache(settings.data.cache.parquet_dir, enabled=settings.data.cache.enabled)
    provider = MarketDataProvider(
        driver,
        TradeCalendar([]),
        normalizer=DataNormalizer(),
        cache=cache,
        preload_mode=settings.data.cache.preload_mode,
        warmup_bars=settings.data.cache.warmup_bars_default,
        fundamentals=FundamentalsStore(lcs.root_path),  # M3-R3: 基本面/成分 PIT 读取
    )
    norm = normalize_universe(codes)
    start = datetime.fromisoformat("1970-01-01")
    end = datetime.fromisoformat("2100-01-01")
    provider.preload(norm, start, end)
    calendar = _derive_calendar(provider, norm)
    return Pipeline(driver=driver, provider=provider, calendar=calendar, settings=settings)


def _derive_calendar(provider: MarketDataProvider, codes: list[str]) -> TradeCalendar:
    """由已加载标的的数据日并集推导交易日历（3.12-④）。"""
    from zoneinfo import ZoneInfo

    _sh = ZoneInfo("Asia/Shanghai")
    dates: set[Any] = set()
    for code in codes:
        arr = provider.bar_array(code)
        for ms in arr["dt"]:
            dates.add(datetime.fromtimestamp(int(ms) / 1000, tz=_sh).date())
    if not dates:
        raise MtzQuantError(
            "universe 无任何行情数据，无法推导交易日历",
            stage="runner",
            hint="检查 data/ 目录与 settings.local_csv.root_path；或先运行 fetch-etf",
        )
    return TradeCalendar.from_dates(sorted(dates))


def make_run_id(task: dict[str, Any]) -> str:
    """run_id = r_<毫秒>_<任务哈希8>（8.8 确定性 + 可读）。"""
    return f"r_{int(time.time() * 1000)}_{config_hash(task)[:8]}"


def run_task(
    task: TaskConfig,
    *,
    settings: Settings,
    out_root: Path | str = "results",
    db_url: str | None = None,
    run_id: str | None = None,
    persist: bool = True,
    parent_run_id: str | None = None,
    publish_hook: Any | None = None,
    control_path: Path | str | None = None,
) -> RunResult:
    """执行一次回测（装配 → 驱动 → 导出 → 入库）。

    parent_run_id（P2 rerun 谱系）: 新 run 记录指向原 run（10.3 lineage 用）。
    publish_hook（M4-W1, 6.3）: ResultStore 实时事件钩子（WS 流式, committed:false 即时发）;
    control_path（M4-W2, 6.4）: 控制文件路径（Web/CLI pause/stop 同权）。
    """
    t0 = time.perf_counter()
    pipeline = build_pipeline(settings, task.universe)
    t_load = time.perf_counter()
    strategy_path = Path(task.strategy.file)
    if not strategy_path.is_file():
        raise MtzQuantError(
            f"策略文件不存在: {strategy_path}",
            stage="runner",
            hint="task.json 的 strategy.file 为相对仓库根或绝对路径",
        )
    strategy_code = strategy_path.read_text(encoding="utf-8")

    task_dict = json.loads(task.model_dump_json())
    if run_id is None:
        run_id = make_run_id(task_dict)
    # M3-T3: 谱系（扫描/重跑: task.engine.parent_run_id 优先级最高, 否则用显式参数）
    if parent_run_id is None:
        parent_run_id = (task_dict.get("engine") or {}).get("parent_run_id")

    # ResultStore 事件流（journal-first 5.6; flush=明细+事件日志 8.7; publish=实时流 6.3）
    result_store = ResultStore(
        FlushPolicy(
            batch_size=settings.database.batch_size,
            flush_interval_ms=settings.database.batch_flush_interval_ms,
            buffer_max_rows=settings.database.buffer_max_rows,
        ),
        run_id=run_id,
        publish_hook=publish_hook,
    )
    # M4-W3 事件日志（run_event_journal, 8.3.7）: persist 时提前开库, flush 钩子批量写
    db: Engine | None = None
    detail: DetailRepo | None = None
    if persist:
        try:
            db = init_db(db_url or settings.database.url)
            detail = DetailRepo(db)

            def _journal_flush(batch: Any) -> None:
                assert detail is not None
                detail.insert_journal(
                    [
                        {
                            "run_id": r.run_id,
                            "event_seq": r.event_seq,
                            "kind": r.kind,
                            "committed": r.committed,
                            "ts": r.ts,
                            "payload_json": json.dumps(r.payload, ensure_ascii=False),
                        }
                        for r in batch
                    ]
                )

            result_store._flush_hook = _journal_flush  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 日志落库失败不阻断回测（journal 为增强, 8.7）
            db = None
            detail = None

    session = BacktestSession(
        task,
        driver=pipeline.driver,
        provider=pipeline.provider,
        calendar=pipeline.calendar,
        run_id=run_id,
        settings_fees=_settings_fees(settings),
        max_participation=settings.engine.max_participation,
        result_store=result_store,
    )
    engine = UnifiedBacktestEngine(
        session,
        broker=session.broker,
        control_refresh=_make_control_refresh(control_path),
    )
    try:
        snapshot = engine.run()
    except MtzQuantError:
        session.status = "error"
        raise
    t_engine = time.perf_counter()

    # 状态聚合（引擎 one_word + 会话 day 过期; 8.8 语义保真）
    degradations = list(engine.degradations) + list(snapshot["degradations"])
    status = engine.status
    if status == "completed_exact" and degradations:
        status = "completed_degraded"

    # 清单（8.8: 数据修订可定位到标的×文件）
    manifest, manifest_hash = build_manifest(
        task_dict,
        strategy_code,
        driver=pipeline.driver,
        universe=task.universe,
        settings=settings,
    )
    manifest["manifest_hash"] = manifest_hash  # 落盘可读（replay 比对用, 8.8）

    bundle = ExportBundle(
        run_id=run_id,
        navs=snapshot["navs"],
        orders=snapshot["orders"],
        fills=snapshot["fills"],
        events=snapshot["events"],
        fees=snapshot["fees"],
        status=status,
        degradations=degradations,
        manifest=manifest,
        task=task_dict,
        strategy_code=strategy_code,
        benchmark_nav=([r.get("benchmark_nav") for r in snapshot["navs"]] or None),
    )
    store = RunStore(out_root)
    out_dir = store.export(bundle)
    t_export = time.perf_counter()

    db2: Engine | None = None
    error_log: str | None = None
    if persist:
        try:
            db2 = db if db is not None else init_db(db_url or settings.database.url)
            persist_run(db2, bundle, manifest, manifest_hash, parent_run_id=parent_run_id)
        except Exception as exc:  # noqa: BLE001 - 入库失败不阻断导出（结果已落盘, 9.1）
            error_log = f"{type(exc).__name__}: {exc}"
    t_persist = time.perf_counter()
    return RunResult(
        run_id=run_id,
        bundle=bundle,
        manifest=manifest,
        manifest_hash=manifest_hash,
        out_dir=out_dir,
        db=db,
        status=status,
        error_log=error_log,
        timing={
            "load": t_load - t0,
            "engine": t_engine - t_load,
            "export": t_export - t_engine,
            "persist": t_persist - t_export,
            "total": t_persist - t0,
        },
    )


def persist_run(
    db: Engine,
    bundle: ExportBundle,
    manifest: dict[str, Any],
    manifest_hash: str,
    *,
    parent_run_id: str | None = None,
) -> None:
    """Bundle → DB（快照 sha256 复用 / 脱敏 / 明细 executemany, 8.3/3.6/8.7）。

    parent_run_id（P2 rerun）: 谱系指向原 run（10.3）。
    """
    repo = RunRepo(db)
    detail = DetailRepo(db)
    strategy_code = bundle.strategy_code or ""
    snap, _ = repo.get_or_create_snapshot(
        file_name=_task_strategy_file(bundle.task),
        code_text=strategy_code,
        sha256=manifest.get("strategy_sha256", ""),
    )
    params = sanitize_params(bundle.task or {})
    run = repo.create_run(
        run_id=bundle.run_id,
        task_name=(bundle.task or {}).get("task_name", bundle.run_id),
        platform="native",
        snapshot_id=snap.id,
        params_json=json.dumps(params, ensure_ascii=False, sort_keys=True),
        status=bundle.status,
        manifest_hash=manifest_hash,
        mtzquant_version=__version__,
        parent_run_id=parent_run_id,
    )
    _ = run  # run 对象仅供状态机使用（明细走 DetailRepo）

    detail.insert_orders(_order_rows(bundle, snap.id))
    detail.insert_events(_event_rows(bundle))
    detail.insert_fills(_fill_rows(bundle, snap.id))
    detail.insert_navs(_nav_rows(bundle))

    repo.set_manifest(
        bundle.run_id, json.dumps(manifest, ensure_ascii=False, indent=2), manifest_hash
    )
    repo.set_metrics(bundle.run_id, _metrics_json(bundle), "8.4-v1")
    repo.update_status(bundle.run_id, bundle.status)


def _task_strategy_file(task: dict[str, Any] | None) -> str:
    if not task:
        return "strategy.py"
    return str(task.get("strategy", {}).get("file", "strategy.py"))


def _metrics_json(bundle: ExportBundle) -> str:
    """summary 的 metrics 部分序列化（8.4; list --sort sharpe 读取）。"""
    store = RunStore()
    summary = store.compute_summary(bundle)
    return json.dumps(
        {"metrics": summary.get("metrics", {}), "status": bundle.status},
        ensure_ascii=False,
        sort_keys=True,
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _order_rows(bundle: ExportBundle, snapshot_id: int) -> list[dict[str, Any]]:
    rows = []
    for o in bundle.orders:
        rows.append(
            {
                "order_id": o["order_id"],
                "run_id": bundle.run_id,
                "strategy_snapshot_id": snapshot_id,
                "code": o["code"],
                "side": o["side"],
                "style": o["style"],
                "qty": o["qty"],
                "order_api": o["order_api"],
                "status": o["status"],
                "submitted_at": _parse_dt(o["submitted_at"]),
                "eligible_fill_at": _parse_dt(o["eligible_fill_at"]),
                "time_in_force": o["time_in_force"],
                "remaining_qty": o["remaining_qty"],
                "filled_qty": o["filled_qty"],
                "avg_fill_price": o.get("avg_fill_price"),
                "reject_reason": o.get("reject_reason"),
            }
        )
    return rows


def _event_rows(bundle: ExportBundle) -> list[dict[str, Any]]:
    rows = []
    for e in bundle.events:
        rows.append(
            {
                "run_id": bundle.run_id,
                "order_id": e["order_id"],
                "event_type": e["event_type"],
                "event_time": _parse_dt(e["event_time"]),
                "qty": e.get("qty", 0.0),
                "price": e.get("price"),
                "info_json": (
                    json.dumps(e["info_json"], ensure_ascii=False) if e.get("info_json") else None
                ),
            }
        )
    return rows


def _fill_rows(bundle: ExportBundle, snapshot_id: int) -> list[dict[str, Any]]:
    rows = []
    for f in bundle.fills:
        rows.append(
            {
                "run_id": bundle.run_id,
                "order_id": f["order_id"],
                "strategy_snapshot_id": snapshot_id,
                "fill_time": _parse_dt(f["fill_time"]),
                "code": f["code"],
                "side": f["side"],
                "price": f["price"],
                "volume": f["volume"],
                "amount": f["amount"],
                "commission": f["commission"],
                "stamp_tax": f["stamp_tax"],
                "transfer_fee": f["transfer_fee"],
                "slippage_cost": f.get("slippage_cost", 0.0),
                "total_fee": f["total_fee"],
                "order_api": "",
                "bar_volume": f.get("bar_volume", 0.0),  # 容量证据（8.4.4）
                "participation_rate": f.get("participation_rate", 0.0),
            }
        )
    return rows


def _nav_rows(bundle: ExportBundle) -> list[dict[str, Any]]:
    rows = []
    for n in bundle.navs:
        rows.append(
            {
                "run_id": bundle.run_id,
                "trade_date": n["trade_date"],
                "strategy_nav": n["nav"],
                "benchmark_nav": n.get("benchmark_nav"),
                "cash": n["cash"],
                "positions_value": n["positions_value"],
                "total_value": n["total_value"],
                "drawdown": n["drawdown"],
                "open_positions": n["open_positions"],
            }
        )
    return rows


def _settings_fees(settings: Settings):
    """settings.engine.default_fees → engine.instrument.FeeParams（会话费用基准）。"""
    from mtzquant.engine.instrument import FeeParams

    f = settings.engine.default_fees
    return FeeParams(
        commission_rate=f.commission_rate,
        commission_min=f.min_commission,
        stamp_tax_rate=f.stamp_tax_rate,
        transfer_fee_rate=f.transfer_fee_rate,
    )


def _make_control_refresh(control_path: Path | str | None) -> Any | None:
    """M4-W2 控制文件刷新（6.4）: 每 bar 边界读取 pause/stop 控制。

    pause → 原地阻塞轮询直到恢复（保持回测位置不跳步）; stop → 置停旗, 引擎收尾。
    文件缺失/损坏 → 视为无控制（可恢复, 不阻断回测）。
    """

    if control_path is None:
        return None
    path = Path(control_path)

    def _read() -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _refresh(control: Any) -> None:
        data = _read()
        control.pause_requested = bool(data.get("pause"))
        control.stop_requested = bool(data.get("stop"))
        while control.pause_requested and not control.stop_requested:
            time.sleep(0.2)
            data = _read()
            control.pause_requested = bool(data.get("pause"))
            control.stop_requested = bool(data.get("stop"))

    return _refresh
