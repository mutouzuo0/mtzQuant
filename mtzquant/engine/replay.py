# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : I4 `mtzquant replay`：按 RunManifest 重放 + 逐笔/逐点 diff（设计 8.8/T-X01/X02）

"""replay（设计 8.8）——确定性重放与差异定位。

流程: 读取 results/<run_id>/{task.json, manifest.json, 明细 CSV} → 以同一 task 重跑
     （新 run）→ 新旧逐笔比对（orders/fills 逐笔、nav 逐点、manifest_hash）→ 差异报告。

差异定位（T-X02）: 数据被修订时, nav 差异可关联到 (标的×交易日)；orders/fills
     差异关联到 order_id/code。未变化部分零差异。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from mtzquant.config import Settings
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.runner import run_task
from mtzquant.engine.session import TaskConfig

_TOL = 1e-10


@dataclass
class ReplayDiff:
    """单条差异（9.1 机读: 段落/标的/交易日/旧值/新值/说明）。"""

    section: str  # orders | fills | navs | manifest
    key: str = ""  # order_id / trade_date
    code: str | None = None
    trade_date: str | None = None
    old: Any = None
    new: Any = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "key": self.key,
            "code": self.code,
            "trade_date": self.trade_date,
            "old": self.old,
            "new": self.new,
            "detail": self.detail,
        }


@dataclass
class ReplayReport:
    """重放结果报告（identical=True 即 T-X01 通过）。"""

    run_id: str
    identical: bool
    manifest_identical: bool
    diffs: list[ReplayDiff] = field(default_factory=list)
    old_manifest_hash: str | None = None
    new_manifest_hash: str | None = None
    new_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "identical": self.identical,
            "manifest_identical": self.manifest_identical,
            "old_manifest_hash": self.old_manifest_hash,
            "new_manifest_hash": self.new_manifest_hash,
            "new_run_id": self.new_run_id,
            "diff_count": len(self.diffs),
            "diffs": [d.to_dict() for d in self.diffs],
        }


def replay_run(
    run_id: str,
    *,
    settings: Settings,
    out_root: Path | str = "results",
    persist: bool = False,
) -> ReplayReport:
    """按 task 重放并 diff; 返回 ReplayReport（数据修订 → 差异定位）。"""
    run_dir = Path(out_root) / run_id
    if not run_dir.is_dir():
        raise MtzQuantError(
            f"重放源目录不存在: {run_dir}",
            stage="replay",
            hint="先 `mtzquant run` 生成 results/<run_id>；或检查 --out 路径",
        )
    task_path = run_dir / "task.json"
    manifest_path = run_dir / "manifest.json"
    if not task_path.is_file():
        raise MtzQuantError(
            f"缺少 {task_path.name}（导出物不完整）", stage="replay", hint="重跑该任务以补全导出"
        )
    task = TaskConfig.model_validate(json.loads(task_path.read_text(encoding="utf-8")))

    old_manifest: dict[str, Any] = {}
    old_manifest_hash: str | None = None
    if manifest_path.is_file():
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_manifest_hash = old_manifest.get("manifest_hash")

    # 重放（同 task, 新 run_id; 不覆盖旧导出）
    result = run_task(task, settings=settings, out_root=out_root, persist=persist)
    new = result.bundle
    new_manifest_hash = result.manifest_hash

    diffs: list[ReplayDiff] = []
    _diff_orders(run_dir, new, diffs)
    _diff_fills(run_dir, new, diffs)
    _diff_navs(run_dir, new, diffs)

    manifest_identical = old_manifest_hash == new_manifest_hash
    if old_manifest and not manifest_identical:
        diffs.append(
            ReplayDiff(
                section="manifest",
                key="manifest_hash",
                old=old_manifest_hash,
                new=new_manifest_hash,
                detail="数据/配置被修订 → 清单哈希变化",
            )
        )
    return ReplayReport(
        run_id=run_id,
        identical=not diffs and manifest_identical,
        manifest_identical=manifest_identical,
        diffs=diffs,
        old_manifest_hash=old_manifest_hash,
        new_manifest_hash=new_manifest_hash,
        new_run_id=new.run_id,
    )


# ------------------------------------------------------------------
# 逐笔/逐点比对
# ------------------------------------------------------------------
def _diff_orders(run_dir: Path, new: Any, diffs: list[ReplayDiff]) -> None:
    """订单逐单比对（按受理序号位置; order_id 内嵌 run_id, 不可作 join 键, 8.8）。"""
    path = run_dir / "orders.csv"
    if not path.is_file():
        return
    old = pd.read_csv(path, dtype={"order_id": str}).to_dict("records")
    new_records = list(new.orders)
    for i in range(max(len(old), len(new_records))):
        o = old[i] if i < len(old) else {}
        n = new_records[i] if i < len(new_records) else {}
        for key in ("code", "side", "status", "qty"):
            ov, nv = o.get(key), n.get(key)
            if not _same(ov, nv):
                diffs.append(
                    ReplayDiff(
                        section="orders",
                        key=f"#{i}",
                        code=n.get("code") or o.get("code"),
                        old=ov,
                        new=nv,
                        detail=f"订单字段 {key} 不一致",
                    )
                )


def _diff_fills(run_dir: Path, new: Any, diffs: list[ReplayDiff]) -> None:
    """成交逐笔比对（按成交顺序位置; order_id 内嵌 run_id, 不作 join 键, 8.8）。"""
    path = run_dir / "fills.csv"
    if not path.is_file():
        return
    old = pd.read_csv(path, dtype={"order_id": str}).to_dict("records")
    new_records = list(new.fills)
    for i in range(max(len(old), len(new_records))):
        o = old[i] if i < len(old) else {}
        n = new_records[i] if i < len(new_records) else {}
        for key in ("code", "side", "price", "volume", "amount"):
            ov, nv = o.get(key), n.get(key)
            if not _same(ov, nv):
                diffs.append(
                    ReplayDiff(
                        section="fills",
                        key=f"#{i}",
                        code=n.get("code") or o.get("code"),
                        old=ov,
                        new=nv,
                        detail=f"成交字段 {key} 不一致",
                    )
                )


def _diff_navs(run_dir: Path, new: Any, diffs: list[ReplayDiff]) -> None:
    path = run_dir / "daily_stats.csv"
    if not path.is_file():
        return
    old = pd.read_csv(path, dtype={"trade_date": str}).set_index("trade_date")
    new_map = {n["trade_date"]: n for n in new.navs}
    for day in sorted(set(old.index) | set(new_map)):
        o = old.loc[day].to_dict() if day in old.index else {}
        n = new_map.get(day, {})
        for key in ("nav", "cash", "positions_value", "total_value"):
            ov = o.get(key)
            nv = n.get(key)
            if not _same(ov, nv):
                diffs.append(
                    ReplayDiff(
                        section="navs",
                        key=day,
                        trade_date=day,
                        old=ov,
                        new=nv,
                        detail=f"净值字段 {key} 不一致",
                    )
                )


def _same(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= _TOL * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError):
            return a == b
    return a == b


def manifest_diff_summary(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """数据清单字段级差异说明（T-X02: 定位到标的×文件）。"""
    notes: list[str] = []
    old_data = old.get("data_manifest", {})
    new_data = new.get("data_manifest", {})
    for code in sorted(set(old_data) | set(new_data)):
        o = old_data.get(code, {})
        n = new_data.get(code, {})
        if o.get("sha256") != n.get("sha256"):
            notes.append(
                f"{code}: 数据文件哈希变化（{o.get('sha256', '-')[:8]}→{n.get('sha256', '-')[:8]}）"
            )
    return notes
