# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/23 11:50:00
# @update_time        : 2026/08/23 11:50:00
# @description : 共享公司行为日历加载器测试（设计 3.12）: data/corporate_actions/{type}/{code}.csv

"""共享公司行为日历加载器（T 新增）: split/cash_div 多文件读取、列别名、缺失目录 → []。"""

from __future__ import annotations

from pathlib import Path

from mtzquant.data.corporate_actions import load_corporate_actions


def _seed(root: Path) -> None:
    (root / "corporate_actions" / "split").mkdir(parents=True, exist_ok=True)
    (root / "corporate_actions" / "cash_div").mkdir(parents=True, exist_ok=True)
    (root / "corporate_actions" / "split" / "510300.SH.csv").write_text(
        "announce_date,ex_date,ratio\n2026-07-03,2026-07-06,3.0\n", encoding="utf-8"
    )
    (root / "corporate_actions" / "cash_div" / "510880.SH.csv").write_text(
        "announce_date,ex_date,pay_date,per_share_cash\n2025-01-16,2025-01-21,2025-01-24,0.142\n",
        encoding="utf-8",
    )


def test_load_corporate_actions_split_and_div(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _seed(root)
    items = load_corporate_actions(root, "corporate_actions/{type}")
    assert len(items) == 2
    by_type = {it["type"]: it for it in items}
    split = by_type["split"]
    assert split["code"] == "510300.SH"
    assert split["ex_date"] == "2026-07-06"
    assert split["ratio"] == "3.0"
    assert split["per_share_cash"] is None
    div = by_type["cash_div"]
    assert div["code"] == "510880.SH"
    assert div["per_share_cash"] == "0.142"
    assert div["pay_date"] == "2025-01-24"


def test_load_corporate_actions_column_alias(tmp_path: Path) -> None:
    """列名不敏感: ex_dt/公告日 等别名同样可读（trade_days 同款约定）。"""
    root = tmp_path / "data"
    (root / "corporate_actions" / "split").mkdir(parents=True)
    (root / "corporate_actions" / "split" / "512480.SH.csv").write_text(
        "公告日,ex_dt,ratio\n2026-07-02,2026-07-03,2.0\n", encoding="utf-8"
    )
    items = load_corporate_actions(root)
    assert len(items) == 1
    assert items[0]["announce_date"] == "2026-07-02"
    assert items[0]["ex_date"] == "2026-07-03"


def test_load_corporate_actions_missing_dir(tmp_path: Path) -> None:
    assert load_corporate_actions(tmp_path / "data") == []
    assert load_corporate_actions(tmp_path / "no_such") == []
