# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 16:20:00
# @update_time        : 2026/08/16 21:59:08
# @description : P1/P2 compare/lineage/diff/rerun 核心（设计 10.3）——纯函数, CLI 薄包装

"""compare/lineage/diff/rerun（设计 10.3）——纯逻辑, 可测试。

- `build_compare_table`: 8.4 指标对照表（rows=指标, cols=run）;
- `build_nav_frame`: 多 run 净值序列按交易日对齐（--csv/--json 输出源）;
- `build_lineage_tree`: parent_run_id 谱系树文本;
- `strategy_diff` / `params_diff`: 策略源码 / 参数（sort_keys 归一）diff;
- `rerun_from_params`: params_json 原样重跑, 新 run parent_run_id 指向原 run。

纪律: 只读 DB 组装 + 纯计算; 不写库（rerun 重跑走 run_task 生产路径）。
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from mtzquant.core.errors import MtzQuantError

# 8.4 对照表字段（label → metrics_json 取值路径; max_drawdown 为 dict）
_METRIC_LABELS: tuple[tuple[str, str], ...] = (
    ("总收益 total_return", "total_return"),
    ("年化收益 annual_return", "annual_return"),
    ("年化波动 annual_volatility", "annual_volatility"),
    ("最大回撤 max_drawdown", "max_drawdown"),
    ("夏普 sharpe", "sharpe"),
    ("索提诺 sortino", "sortino"),
    ("卡玛 calmar", "calmar"),
    ("日胜率 daily_win_rate", "daily_win_rate"),
)


def _metric_value(metrics: dict[str, Any] | None, field: str) -> Any:
    """从 8.4 metrics dict 取值（max_drawdown 是 dict, 取 value 字段）。"""
    if not metrics:
        return None
    m = metrics.get("metrics", metrics)
    v = m.get(field)
    if isinstance(v, dict):
        v = v.get("value")
    return v


def build_compare_table(
    run_metrics: list[tuple[str, dict[str, Any] | None]],
) -> dict[str, Any]:
    """指标对照表: {metric: {run_id: 值}} + 每行 best（最小回撤/最大收益等）。"""
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for run_id, metrics in run_metrics:
        for label, field in _METRIC_LABELS:
            row = rows.setdefault(label, {})
            row[run_id] = _metric_value(metrics, field)
        order.append(run_id)
    # 每行标注最优列（收益/比率越大越好, 回撤越小越好）
    best: dict[str, str] = {}
    for label, _f in _METRIC_LABELS:
        vals = {rid: rows[label].get(rid) for rid in order}
        numerics = {rid: v for rid, v in vals.items() if isinstance(v, (int, float))}
        if not numerics:
            continue
        if label.startswith("最大回撤"):
            best[label] = min(numerics, key=numerics.get)  # type: ignore[arg-type]
        else:
            best[label] = max(numerics, key=numerics.get)  # type: ignore[arg-type]
    return {"runs": order, "rows": rows, "best": best}


def build_nav_frame(navs_by_run: list[tuple[str, list[dict[str, Any]]]]) -> pd.DataFrame:
    """多 run 净值序列按交易日对齐（strategy_nav 列, 索引=trade_date）。"""
    frame = pd.DataFrame(index=pd.Index([], name="trade_date"))
    for run_id, navs in navs_by_run:
        if not navs:
            continue
        s = pd.Series({r["trade_date"]: r.get("strategy_nav") for r in navs}, name=run_id)
        frame = frame.join(s, how="outer")
    return frame.sort_index()


def build_lineage_tree(nodes: list[dict[str, Any]], root: str | None = None) -> str:
    """谱系树文本（10.3）: 从根（或全部无父节点）起, parent_run_id 关系缩进。"""
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_run_id"), []).append(n)
    lines: list[str] = []
    started: set[str] = set()

    def _walk(pid: str | None, depth: int) -> None:
        for node in sorted(by_parent.get(pid, []), key=lambda x: x["run_id"]):
            rid = node["run_id"]
            if rid in started:
                continue
            started.add(rid)
            sharpe = node.get("sharpe")
            status = node.get("status", "?")
            task = node.get("task_name", "")
            sharpe_txt = f" sharpe={sharpe:.3f}" if isinstance(sharpe, (int, float)) else ""
            lines.append(f"{'  ' * depth}├─ {rid}  [{status}]{sharpe_txt}  {task}")
            _walk(rid, depth + 1)

    if root is not None:
        root_node = next((n for n in nodes if n.get("run_id") == root), None)
        if root_node is None:
            raise MtzQuantError(f"run 不存在: {root}", stage="compare", hint="检查 run_id")
        _walk(root, 0)
    else:
        # 全量: 无父节点（或父节点已删）为根
        parents = {n.get("parent_run_id") for n in nodes}
        roots = sorted({n["run_id"] for n in nodes if n.get("parent_run_id") not in parents})
        if not roots:
            roots = sorted(n["run_id"] for n in nodes)
        for r in roots:
            _walk(r, 0)
    return "\n".join(lines) or "（无谱系记录）"


def strategy_diff(code1: str | None, code2: str | None) -> str:
    """策略源码 unified diff（- 旧 / + 新）。"""
    c1 = (code1 or "").splitlines(keepends=True)
    c2 = (code2 or "").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            c1,
            c2,
            fromfile="run1/strategy_snapshot",
            tofile="run2/strategy_snapshot",
            lineterm="",
        )
    )


def params_diff(p1: dict[str, Any] | None, p2: dict[str, Any] | None) -> str:
    """params_json 归一（sort_keys）后的 pretty diff（- 旧 / + 新）。"""
    d1 = json.dumps(p1 or {}, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    d2 = json.dumps(p2 or {}, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(d1, d2, fromfile="run1/params", tofile="run2/params", lineterm="")
    )


def rerun_from_params(
    params: dict[str, Any],
    *,
    parent_run_id: str,
    settings: Any,
    out_root: Path | str = "results",
    db_url: str | None = None,
) -> Any:
    """params_json 原样重跑; 新 run 的 parent_run_id 指向原 run（10.3）。"""
    from mtzquant.engine.runner import run_task
    from mtzquant.engine.session import TaskConfig

    try:
        task = TaskConfig.model_validate(params)
    except Exception as exc:  # noqa: BLE001
        raise MtzQuantError(
            f"params_json 无法还原任务: {parent_run_id}: {type(exc).__name__}: {exc}",
            stage="compare",
            hint="params_json 为脱敏任务配置（3.6）; 检查是否被手工改动",
        ) from exc
    return run_task(
        task,
        settings=settings,
        out_root=out_root,
        db_url=db_url,
        parent_run_id=parent_run_id,
    )
