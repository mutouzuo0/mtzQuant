# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:42:00
# @update_time        : 2026/08/16 00:42:00
# @description : D7 主数据：instruments.csv 读写/upsert/快照（设计 3.11）

"""证券基础信息主数据（设计 3.11）——「基本信息」与行情的唯一对应关系。

存储: data/master/instruments.csv（单文件, 主键=code, 常驻增量更新）
快照: data/master/snapshots/instruments_{YYYYMMDD}.csv（每次刷新留档, 可回溯）
upsert 语义（3.11）:
  已有行 → 更新可变字段（name/delist_date/industry…）, 保留首次 list_date
  新行   → append（新上市标的）
幂等: 同日重复刷新零变化; 刷新后调用方失效重建内存缓存。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# 字段全集（设计 3.11; 顺序即 CSV 列序，跨版本稳定）
MASTER_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "instrument_type",
    "exchange",
    "list_date",
    "delist_date",
    "underlying",
    "exercise_price",
    "contract_type",
    "maturity_date",
    "industry",
    "updated_at",
)


@dataclass(frozen=True)
class InstrumentRow:
    """主数据单行（可空字段默认''或 None, 构造后不可变）。"""

    code: str
    name: str = ""
    instrument_type: str = "stock"
    exchange: str = ""
    list_date: str = ""  # YYYY-MM-DD（保留首次入档值, upsert 不覆盖）
    delist_date: str = ""  # 空=在市; 源 99999999/00000000 洗成空（3.11）
    underlying: str = ""
    exercise_price: str = ""
    contract_type: str = ""
    maturity_date: str = ""
    industry: str = ""
    updated_at: str = ""  # ISO 时间戳（维护方填充）

    def to_row(self) -> dict[str, str]:
        return {
            f: (getattr(self, f) if f != "exercise_price" else getattr(self, f))
            for f in MASTER_FIELDS
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MasterStore:
    """master/instruments.csv 读写（设计 3.11 最小版, 下游 D/E 阶段共用）。"""

    def __init__(self, path: Path | str, snapshot_dir: Path | str | None = None) -> None:
        self._path = Path(path)
        self._snapshot_dir = (
            Path(snapshot_dir) if snapshot_dir is not None else self._path.parent / "snapshots"
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def read(self) -> pd.DataFrame:
        """读取全表（code 为索引; 文件缺失 → 空表）。"""
        if not self._path.is_file():
            return pd.DataFrame(columns=list(MASTER_FIELDS)).set_index("code")
        df = pd.read_csv(self._path, dtype=str, keep_default_na=False)
        for col in MASTER_FIELDS:
            if col not in df.columns:
                df[col] = ""
        df = df[list(MASTER_FIELDS)].replace("nan", "")
        return df.set_index("code")

    def get(self, code: str) -> InstrumentRow | None:
        df = self.read()
        if code not in df.index:
            return None
        row = df.loc[code]
        vals: dict[str, str] = {"code": code}
        for f in MASTER_FIELDS:
            if f == "code":
                continue
            vals[f] = "" if pd.isna(row[f]) else str(row[f])
        return InstrumentRow(**vals)

    def get_name(self, code: str) -> str:
        row = self.get(code)
        return row.name if row else ""

    # ------------------------------------------------------------------
    # 写入（upsert + 快照, 3.11）
    # ------------------------------------------------------------------
    def upsert(self, rows: list[InstrumentRow]) -> tuple[int, int]:
        """按 code 主键 upsert; 返回 (新增行数, 更新行数)（3.11 语义）。"""
        if not rows:
            return (0, 0)
        existing = self.read()
        cols = [f for f in MASTER_FIELDS if f != "code"]  # code 为索引, 不在列中
        added = updated = 0
        for r in rows:
            if r.code in existing.index:
                old = existing.loc[r.code]
                for f in cols:
                    if f == "list_date":
                        # 保留首次 list_date; 新值仅在原值为空时采用
                        existing.loc[r.code, f] = old.get("list_date") or getattr(r, f) or ""
                    elif f == "updated_at":
                        existing.loc[r.code, f] = _now_iso()
                    else:
                        new_val = getattr(r, f)
                        if new_val:
                            existing.loc[r.code, f] = str(new_val)
                updated += 1
            else:
                values = {f: str(getattr(r, f)) for f in cols}
                values["list_date"] = values["list_date"] or ""
                values["updated_at"] = _now_iso()
                existing.loc[r.code] = [values[f] for f in cols]
                added += 1
        existing = existing.loc[~existing.index.duplicated(keep="last")]
        self._write(existing)
        return (added, updated)

    def clear(self) -> None:
        """清空主数据（谨慎: 恢复性操作, 测试用）。"""
        if self._path.is_file():
            self._path.unlink()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _write(self, df: pd.DataFrame) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".csv.tmp")
        try:
            df.reset_index().rename(columns={"index": "code"}).to_csv(
                tmp, index=False, encoding="utf-8"
            )
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)
        self.snapshot()

    def snapshot(self) -> Path:
        """全量另存 snapshots/instruments_{YYYYMMDD}.csv（每次写入留档, 3.11）。"""
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        snap = self._snapshot_dir / f"instruments_{date.today():%Y%m%d}.csv"
        if self._path.is_file() and not snap.exists():
            import shutil

            shutil.copy2(self._path, snap)
        return snap


def clean_field(value: str) -> str:
    """源脏值归一: 99999999/00000000 → ''（退市日期占位, 设计 3.11）；nan → ''。"""
    v = str(value).strip()
    if v in ("99999999", "00000000", "nan", "", "N/A", "NaT"):
        return ""
    return v
