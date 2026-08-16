# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 23:20:00
# @update_time        : 2026/08/16 23:20:00
# @description : M3-R4 ML 数据集：DatasetBuilder / 防泄露时间切分 / 前瞻标签（设计 3.8）

"""ML 数据集构建（设计 3.8, M3-R4）——研究侧通道, **禁止进入回测主链路**。

- `DatasetBuilder`: 行情/因子宽表 → (X, y, dt_index) 监督样本（滚动窗口展开 lookback）;
- `split_by_time`: 只按时间边界切分 train/valid/test——训练样本时刻全部早于
  验证/测试样本时刻（**防泄露自动断言**, T-R04）;
- `label_forward_return`: 前瞻收益标签（horizon 后收盘/今收 - 1）——**仅本模块可见**,
  回测主链路禁止 import（import-linter 契约, pyproject）;

纪律: X/y 全 float64; dt_index 升序; 确定性（无随机）; 允许 NaN 标签样本由调用方裁剪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from mtzquant.core.errors import MtzQuantError


@dataclass
class MLDataset:
    """监督数据集（时间/标的全对齐）。"""

    X: np.ndarray  # [n_samples, lookback, n_features]
    y: np.ndarray  # [n_samples]
    dt_index: list[date]  # 每个样本的 bar 日期（标签时刻, 升序）
    code_index: list[str]  # 每个样本所属标的
    feature_names: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # lookback/horizon/构建信息

    # ------------------------------------------------------------------
    @property
    def n_samples(self) -> int:
        return len(self.dt_index)

    def validate(self) -> None:
        """结构自检（T-R04 时间隔离断言前置）: 维度对齐 + dt 单调升序。"""
        if self.X.ndim != 3:
            raise MtzQuantError(
                f"X 须为 3 维 [样本, lookback, 特征], 得到 {self.X.ndim}",
                stage="ml_dataset",
            )
        if not (len(self.X) == len(self.y) == len(self.dt_index) == len(self.code_index)):
            raise MtzQuantError(
                "X/y/dt_index/code_index 长度不一致", stage="ml_dataset", hint="数据集对齐失败"
            )
        # 检查相邻下降（b < a）→ 非单调; 等值/严格升序均通过
        if any(b < a for a, b in zip(self.dt_index, self.dt_index[1:], strict=False)):
            raise MtzQuantError(
                "dt_index 未升序", stage="ml_dataset", hint="按时间排序后再构建/切分"
            )


# ============================================================
# 前瞻标签（仅 ML 通道可见; 回测主链路禁止 import 本模块, 契约见 pyproject）
# ============================================================
def label_forward_return(bar_df: pd.DataFrame, horizon: int, close_col: str = "close") -> pd.Series:
    """前瞻收益标签: y_t = close_{t+horizon} / close_t - 1（指数对齐, 按 index 排序）。

    输入 bar_df 的 index 须为升序（日期/时间）; 尾部不足 horizon 的样本标签为 NaN。
    """
    if horizon < 1:
        raise MtzQuantError(
            f"horizon 必须 >= 1, 得到 {horizon}", stage="ml_dataset", hint="前瞻期数"
        )
    if close_col not in bar_df.columns:
        raise MtzQuantError(
            f"缺 {close_col} 列", stage="ml_dataset", hint="需要 close 计算前瞻收益"
        )
    close = bar_df[close_col]
    if not close.index.is_monotonic_increasing:
        raise MtzQuantError("bar_df 索引未升序", stage="ml_dataset", hint="前瞻标签要求按时间升序")
    return close.shift(-horizon) / close - 1.0


# ============================================================
# 时间切分（只按时间边界; 防泄露自动断言）
# ============================================================
def split_by_time(
    dataset: MLDataset, train_end: date, valid_end: date | None = None
) -> tuple[MLDataset, MLDataset, MLDataset]:
    """按时间边界切分 train/valid/test（T-R04: train 全部早于 valid/test）。

    train_end:   训练段最后一天（含）;
    valid_end:   验证段最后一天（含）; None → valid 空（只用 train/test）。
    输出断言: train.dt 最大值 < valid.dt 最小值; valid 最大值 < test 最小值。
    """
    dataset.validate()
    if valid_end is not None and valid_end < train_end:
        raise MtzQuantError(
            f"valid_end({valid_end}) 早于 train_end({train_end})",
            stage="ml_dataset",
            hint="时间三段须依次递增",
        )
    dts = np.array(dataset.dt_index)

    def _mask(lo: date | None, hi: date | None) -> np.ndarray:
        m = np.ones(len(dts), dtype=bool)
        if lo is not None:
            m &= dts > np.datetime64(lo)
        if hi is not None:
            m &= dts <= np.datetime64(hi)
        return m

    train_m = dts <= np.datetime64(train_end)
    if valid_end is None:
        valid_m = np.zeros(len(dts), dtype=bool)
        test_m = ~train_m
    else:
        valid_m = (dts > np.datetime64(train_end)) & (dts <= np.datetime64(valid_end))
        test_m = dts > np.datetime64(valid_end)

    def _slice(mask: np.ndarray) -> MLDataset:
        return MLDataset(
            X=dataset.X[mask],
            y=dataset.y[mask],
            dt_index=[d for d, keep in zip(dataset.dt_index, mask.tolist(), strict=True) if keep],
            code_index=[
                c for c, keep in zip(dataset.code_index, mask.tolist(), strict=True) if keep
            ],
            feature_names=list(dataset.feature_names),
            meta=dict(dataset.meta),
        )

    train, valid, test = _slice(train_m), _slice(valid_m), _slice(test_m)
    # 防泄露自动断言（T-R04 核心）
    if valid.n_samples and train.dt_index[-1] >= valid.dt_index[0]:
        raise MtzQuantError(
            "train 与 valid 时间边界重叠（防泄露失败）", stage="ml_dataset", hint="检查 train_end"
        )
    if test.n_samples and (
        valid.n_samples
        and valid.dt_index[-1] >= test.dt_index[0]
        or not valid.n_samples
        and train.dt_index[-1] >= test.dt_index[0]
    ):  # noqa: E501
        raise MtzQuantError(
            "训练/验证与 test 时间边界重叠（防泄露失败）", stage="ml_dataset", hint="检查 valid_end"
        )
    return train, valid, test


# ============================================================
# DatasetBuilder（滚动窗口展开 lookback）
# ============================================================
class DatasetBuilder:
    """宽表 → 监督数据集（M3-R4）: 每个样本 = 最近 lookback 根特征窗口 + 前瞻标签。

    用法:
      builder = DatasetBuilder(lookback=20, horizon=5)
      ds = builder.build(features_wide, y=label_series)          # 显式给标签
      ds = builder.build_from_provider(provider, codes, fields, start, end)
    """

    def __init__(self, *, lookback: int = 20, horizon: int = 5) -> None:
        if lookback < 1:
            raise MtzQuantError(f"lookback 必须 >= 1, 得到 {lookback}", stage="ml_dataset")
        self.lookback = lookback
        self.horizon = horizon

    def build(
        self,
        features: pd.DataFrame,
        *,
        y: pd.Series | None = None,
        horizon: int | None = None,
    ) -> MLDataset:
        """features: index=升序时间, columns=特征名（数值）; y: 对齐 index 的前瞻标签。"""
        horizon = horizon or self.horizon
        if features.empty:
            raise MtzQuantError("features 为空", stage="ml_dataset", hint="无特征可构建")
        if not features.index.is_monotonic_increasing:
            raise MtzQuantError("features 索引未升序", stage="ml_dataset", hint="按时间升序传入")
        feat_names = [str(c) for c in features.columns]
        vals = features.to_numpy(dtype=float)
        n, nf = vals.shape
        if y is None:
            y = (
                label_forward_return(features.assign(close=vals[:, 0]), horizon, close_col="close")
                if nf
                else pd.Series(index=features.index, dtype=float)
            )
        else:
            y = y.reindex(features.index)
        # 滚动窗口展开（样本 t 的特征 = vals[t-lookback:t], 标签 = y_t）
        X_list: list[np.ndarray] = []
        y_list: list[float] = []
        dt_list: list[date] = []
        code_list: list[str] = []
        for t in range(self.lookback, n):
            lab = y.iloc[t]
            if lab is None or (isinstance(lab, float) and np.isnan(lab)):
                continue  # 尾窗无前瞻标签 → 剔除（防 NaN 进监督集）
            X_list.append(vals[t - self.lookback : t])
            y_list.append(float(lab))
            dt_list.append(features.index[t].date())
            code_list.append(str(getattr(features.index[t], "code", "")) or "")
        if not X_list:
            raise MtzQuantError(
                f"无有效样本（lookback={self.lookback}, horizon={horizon}）",
                stage="ml_dataset",
                hint="数据窗口太短或标签全 NaN",
            )
        return MLDataset(
            X=np.asarray(X_list, dtype=float),
            y=np.asarray(y_list, dtype=float),
            dt_index=dt_list,
            code_index=code_list,
            feature_names=feat_names,
            meta={"lookback": self.lookback, "horizon": horizon, "n_features": nf},
        )

    def build_from_provider(
        self,
        provider: Any,
        codes: list[str],
        fields: list[str],
        start: datetime,
        end: datetime,
    ) -> MLDataset:
        """便捷: provider.to_frame → 逐标的样本（code_index 标记标的, 3.8）。"""
        frame = provider.to_frame(codes, fields, start, end)
        if frame.empty:
            raise MtzQuantError("provider.to_frame 返回空", stage="ml_dataset")
        codes_in = sorted({c for (c, _f) in frame.columns})
        X_list: list[np.ndarray] = []
        y_list: list[float] = []
        dt_list: list[date] = []
        code_list: list[str] = []
        feat_names = list(fields)
        for code in codes_in:
            sub = frame[code].dropna(subset=fields[0])
            if len(sub) < self.lookback + self.horizon:
                continue
            ds = self.build(sub, horizon=self.horizon)
            X_list.append(ds.X)
            y_list.extend(ds.y.tolist())
            dt_list.extend(ds.dt_index)
            code_list.extend([code] * ds.n_samples)
        if not X_list:
            raise MtzQuantError("无标的满足 lookback+horizon 长度", stage="ml_dataset")
        return MLDataset(
            X=np.concatenate(X_list, axis=0),
            y=np.asarray(y_list, dtype=float),
            dt_index=dt_list,
            code_index=code_list,
            feature_names=feat_names,
            meta={"lookback": self.lookback, "horizon": self.horizon, "codes": codes_in},
        )
