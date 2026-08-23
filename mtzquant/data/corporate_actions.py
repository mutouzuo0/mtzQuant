# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/23 11:30:00
# @update_time        : 2026/08/23 11:30:00
# @description : 共享公司行为日历加载器（设计 3.12）: data/corporate_actions/{type}/{code}.csv → 行字典

"""共享公司行为日历（独立于价格数据、独立于策略的通用市场数据表）。

目录布局（设计 3.12 / AGENTS.md 数据目录约定）:
    data/corporate_actions/{type}/{code}.csv   # type ∈ split/cash_div/bonus

每个文件列（列名大小写不敏感, 缺失列忽略）:
    announce_date, ex_date, pay_date, per_share_cash, ratio
（code/type 从文件路径推导: {code}.csv 文件名 + 上级目录 {type}）

本模块只做「读文件 → 行字典」, 不 import engine（依赖纪律: data 层禁止依赖 engine）;
行字典由引擎层（BacktestSession._parse_corp_action）转 CorporateAction 生效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# 列别名: 规范名 → 兼容别名集合（trade_days 同款"列名不敏感"约定）
_COL_ALIASES: dict[str, tuple[str, ...]] = {
    "announce_date": ("announce_date", "announce", "ann_dt", "公告日"),
    "ex_date": ("ex_date", "ex_dt", "exdate", "除权日"),
    "pay_date": ("pay_date", "pay_dt", "paydate", "到账日"),
    "per_share_cash": ("per_share_cash", "cash", "cash_per_share", "每股现金"),
    "ratio": ("ratio", "split_ratio", "送转比例"),
}


def _pick(row: pd.Series, key: str) -> Any:
    """按别名取单元格; 空串 → None。"""
    for alias in _COL_ALIASES[key]:
        if alias in row.index:
            v = row[alias]
            if pd.notna(v) and str(v).strip() != "":
                return str(v).strip()
    return None


def load_corporate_actions(
    root: Path, dir_template: str = "corporate_actions/{type}"
) -> list[dict[str, Any]]:
    """读取共享公司行为日历, 返回行字典列表（code/type/三日期/现金/比例）。

    `dir_template` 形如 `corporate_actions/{type}`（settings.local_csv.corporate_actions_dir）;
    `{type}` 占位剥离后 rglob 全量 .csv——目录缺失/空 → []。
    """
    base = Path(str(dir_template).split("{type}")[0].strip("/\\"))
    d = root / base
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(d.rglob("*.csv")):
        act_type = path.parent.name  # .../corporate_actions/{type}/{code}.csv
        code = path.stem
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, ValueError, KeyError):
            continue  # 单文件坏行不阻断整表（best-effort, 3.12）
        for _, row in df.iterrows():
            item: dict[str, Any] = {
                "code": code,
                "type": act_type,
                "announce_date": _pick(row, "announce_date"),
                "ex_date": _pick(row, "ex_date"),
                "pay_date": _pick(row, "pay_date"),
                "per_share_cash": _pick(row, "per_share_cash"),
                "ratio": _pick(row, "ratio"),
            }
            out.append(item)
    return out
