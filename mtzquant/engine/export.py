# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:40:00
# @update_time        : 2026/08/19 10:30:00
# @description : H RunStore.export：结果导出 CSV/JSON + 指标汇总（设计 9.1/8.4）

"""RunStore.export（设计 9.1）——结果导出与 DB 同源。

导出物: results/<run_id>/
  orders.csv / order_events.csv / fills.csv / daily_stats.csv
  summary.json     （gross/net 双口径指标, 8.4）
  manifest.json    （确定性重放清单, 8.8）
  task.json        （任务配置原文）
  strategy_snapshot.py（策略源码快照）

一致性: 导出数据与 DB 同源（同一事件流投影, 9.1 纪律）——T-S06 逐行比对。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mtzquant.engine.metrics import Metrics

_METRICS_VERSION = "8.4-v1"


@dataclass
class ExportBundle:
    """一次回测的导出素材（引擎/会话产出, DB 投影同源）。"""

    run_id: str
    navs: list[
        dict[str, Any]
    ]  # [{trade_date, nav, cash, positions_value, total_value, drawdown, open_positions}]
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    events: list[dict[str, Any]]
    fees: dict[str, float]
    status: str
    degradations: list[str]
    manifest: dict[str, Any] | None = None
    task: dict[str, Any] | None = None
    strategy_code: str | None = None
    benchmark_nav: list[float] | None = None
    frictions: list[str] = field(default_factory=list)  # 撮合摩擦（N1; 带默认放末尾兼容旧构造）


class RunStore:
    """结果导出（9.1）与指标汇总（8.4）。"""

    def __init__(self, out_root: Path | str = "results") -> None:
        self.out_root = Path(out_root)

    # ------------------------------------------------------------------
    def compute_summary(self, bundle: ExportBundle) -> dict[str, Any]:
        """8.4 全指标（gross 口径; 本版以策略 nav 计算）→ summary dict。"""
        nav = np.asarray([r["nav"] for r in bundle.navs], dtype=float)
        if nav.size == 0:
            return {"error": "no nav"}
        from datetime import date
        from datetime import datetime as _dt

        dates: list[date | _dt] | None = [
            r["trade_date"] for r in bundle.navs if r.get("trade_date")
        ]
        bench = np.asarray(bundle.benchmark_nav, dtype=float) if bundle.benchmark_nav else None
        m = Metrics.compute(nav, dates=dates, benchmark_nav=bench)
        risk = m.risk
        return {
            "run_id": bundle.run_id,
            "status": bundle.status,
            "metrics_version": m.metrics_version,
            "metrics": {
                "total_return": risk.total_return,
                "annual_return": risk.annual_return,
                "annual_volatility": risk.annual_volatility,
                "max_drawdown": asdict(risk.max_drawdown),
                "sharpe": risk.sharpe,
                "sortino": risk.sortino,
                "calmar": risk.calmar,
                "daily_win_rate": risk.daily_win_rate,
            },
            "fees": bundle.fees,
            "degradations": bundle.degradations,
            "frictions": bundle.frictions,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    # ------------------------------------------------------------------
    def export(self, bundle: ExportBundle) -> Path:
        """导出全部产物; 返回 results/<run_id>/ 目录。"""
        out = self.out_root / bundle.run_id
        out.mkdir(parents=True, exist_ok=True)

        pd.DataFrame(bundle.navs).to_csv(out / "daily_stats.csv", index=False)
        pd.DataFrame(bundle.orders).to_csv(out / "orders.csv", index=False)
        pd.DataFrame(bundle.fills).to_csv(out / "fills.csv", index=False)
        pd.DataFrame(bundle.events).to_csv(out / "order_events.csv", index=False)

        summary = self.compute_summary(bundle)
        (out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if bundle.manifest is not None:
            (out / "manifest.json").write_text(
                json.dumps(bundle.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if bundle.task is not None:
            (out / "task.json").write_text(
                json.dumps(bundle.task, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if bundle.strategy_code is not None:
            (out / "strategy_snapshot.py").write_text(bundle.strategy_code, encoding="utf-8")
        return out
