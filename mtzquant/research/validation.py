# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:15:00
# @update_time        : 2026/08/17 01:15:00
# @description : M3-U1/U2 时间隔离 + embargo：三段切分 / 边界隔离带 / test 决策拒绝（5.8.2）

"""防过拟合时间隔离（设计 5.8.2, M3-U1/U2）——只按时间边界切分。

- `split_time_range`: train/valid/test 三段切分（只按时间边界; 非随机, T-F01）;
- `apply_embargo`: 滚动验证边界隔离带（防 horizon 前瞻标签跨越泄露, 配合 R4
  `label_forward_return`; embargo_periods 可配, 5.8.2/3.8）;
- `DecisionValidator`: test 段参与调优决策 → 静态检查拒绝（调优循环只读 train/valid 指标）。

OOS 指标单独存储: 由调用方以 `run_manifest.parent_lineage.split` 标记（U1, manifest 已支持）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from mtzquant.core.errors import MtzQuantError

VALID_SPLITS = ("train", "valid", "test")


@dataclass(frozen=True)
class TimeSplit:
    """三段时间边界（日期级; 只按时间切分, 8.8 确定性）。"""

    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    test_start: date
    test_end: date
    embargo_bars: int = 0

    def split_of(self, d: date) -> str | None:
        """某日属于哪个段（train/valid/test; 边界含; 隔离带内 → None 不入任何训练/评估）。"""
        if self.train_start <= d <= self.train_end:
            return "train"
        if self.valid_start <= d <= self.valid_end:
            return "valid"
        if self.test_start <= d <= self.test_end:
            return "test"
        return None


def split_time_range(
    start: date,
    end: date,
    days: list[date],
    *,
    train_ratio: float = 0.6,
    valid_ratio: float = 0.2,
    embargo_bars: int = 0,
) -> TimeSplit:
    """按交易日序列 time 三段切分（train/valid/test; embargo 拉回 train 上界）。

    只按时间边界（T-F01: 越界样本不入 train/valid）; 序列须升序且含 [start,end]。
    """
    if not start <= end:
        raise MtzQuantError(f"区间非法: {start} > {end}", stage="validation")
    if not 0 < train_ratio < 1 or not 0 <= valid_ratio < 1 or train_ratio + valid_ratio >= 1:
        raise MtzQuantError(
            f"比例非法: train={train_ratio}, valid={valid_ratio}（须 train+valid<1）",
            stage="validation",
        )
    if embargo_bars < 0:
        raise MtzQuantError(f"embargo_bars 不能为负: {embargo_bars}", stage="validation")
    seq = sorted({d for d in days if start <= d <= end})
    if not seq:
        raise MtzQuantError("切分区间无交易日", stage="validation", hint="检查 days/start/end")
    n = len(seq)
    n_train = max(1, int(round(n * train_ratio)))
    n_valid = max(0, int(round(n * valid_ratio)))
    n_test = n - n_train - n_valid
    if n_test < 1 or n_valid < 0:
        raise MtzQuantError("切分后 test 段为空（区间太短）", stage="validation")
    train_end = seq[n_train - 1]
    valid_start = seq[n_train]
    valid_end = seq[min(n_train + n_valid - 1, n - 1)]
    test_start = seq[min(n_train + n_valid, n - 1)]
    test_end = seq[-1]
    # embargo: 边界隔离带——train 上界前移 embargo_bars（防前瞻标签跨越泄露, U2）
    if embargo_bars > 0 and n_train > embargo_bars:
        train_end = seq[n_train - 1 - embargo_bars]
    return TimeSplit(
        train_start=seq[0],
        train_end=train_end,
        valid_start=valid_start,
        valid_end=valid_end,
        test_start=test_start,
        test_end=test_end,
        embargo_bars=embargo_bars,
    )


def apply_embargo(split: TimeSplit, embargo_bars: int) -> TimeSplit:
    """对既有切分应用/更新 embargo（train 上界拉回, 防 horizon 标签跨越, U2）。"""
    return split_time_range(
        split.train_start,
        split.test_end,
        _range_days(split, split.train_start, split.test_end),
        train_ratio=0.6,
        valid_ratio=0.2,
        embargo_bars=embargo_bars,
    )


def _range_days(split: TimeSplit, start: date, end: date) -> list[date]:
    """从既有切分还原区间内全部交易日（保守重建, 边界含）。"""
    out: list[date] = []
    cur = start
    while cur <= end:
        if split.split_of(cur) is not None:
            out.append(cur)
        cur = date(cur.year, cur.month, cur.day) + __import__("datetime").timedelta(days=1)
    return out


class DecisionValidator:
    """test 段决策拒绝（5.8.2, U1）——调优循环只读 train/valid 指标。"""

    def __init__(self, *, allowed_for_decision: tuple[str, ...] = ("train", "valid")) -> None:
        self._allowed = tuple(allowed_for_decision)

    def check(self, split_name: str, *, for_decision: bool = True) -> None:
        """校验某段指标是否可用于决策; test 段参与决策 → 拒绝（T-F01 越界拒绝）。"""
        if split_name not in VALID_SPLITS:
            raise MtzQuantError(
                f"未知段名 {split_name!r}", stage="validation", hint=f"可选: {VALID_SPLITS}"
            )
        if for_decision and split_name not in self._allowed:
            raise MtzQuantError(
                f"段 {split_name!r} 不可用于调优决策（test 段仅 OOS 评估, 5.8.2）",
                stage="validation",
                hint=f"调优循环只读 {self._allowed}; OOS 指标独立存储（parent_lineage.split）",
            )
