# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : LiquidityModel 容量约束：单笔 ≤ 基准量(prev_adv)×max_participation，basis 可选

"""LiquidityModel 容量约束（设计 5.3.3）。

单笔成交量 ≤ 基准量 × max_participation（默认 0.10，压力上限 0.25）。
日线容量时点模型（M0 冻结项⑤）——基准量时点由任务配置 basis 决定：
  prev_adv（默认）: 前一日 ADV，下单时刻已知、无前视，S3 语义安全；
  intraday_vwap / open_auction_vol / stress_only: M5 预留（配合 VWAP 执行口径）。
基准量的**取数时点**由 BrokerSim 按 basis 解析后传入，本模型只做纯缩放计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mtzquant.core.errors import MtzQuantError


class LiquidityBasis(StrEnum):
    PREV_ADV = "prev_adv"  # 默认：前一日 ADV（下单时刻已知，无前视）
    INTRADAY_VWAP = "intraday_vwap"  # 预留：当日 VWAP/J 执行口径
    OPEN_AUCTION_VOL = "open_auction_vol"  # 预留：开盘集合竞价成交量
    STRESS_ONLY = "stress_only"  # 预留：仅事后容量压力测试（不参与 S3 精确语义）


@dataclass(frozen=True)
class LiquidityModel:
    """v1 默认容量实现：单笔 ≤ reference_volume × max_participation。"""

    max_participation: float = 0.10  # 良好流动性取 5%~10%；0.25 为压力上限
    basis: LiquidityBasis = LiquidityBasis.PREV_ADV

    def __post_init__(self) -> None:
        if not (0.0 < self.max_participation <= 1.0):
            raise MtzQuantError(
                f"max_participation 必须在 (0, 1] 区间，得到 {self.max_participation}",
                stage="liquidity",
                hint="PTrade set_volume_ratio 语义：0.10 默认、0.25 压力上限",
            )

    def max_qty(self, order_qty: float, reference_volume: float) -> float:
        """可成交数量 = min(订单量, 基准量 × 参与率)；负数输入视为符号错误。"""
        if order_qty < 0 or reference_volume < 0:
            raise MtzQuantError(
                f"容量计算输入不能为负: order_qty={order_qty} reference_volume={reference_volume}",
                stage="liquidity",
            )
        cap = reference_volume * self.max_participation
        return min(order_qty, cap)

    def with_participation(self, ratio: float) -> LiquidityModel:
        """派生副本（任务多档敏感性，报告输出 1%/5%/10%/20%，design 8.4.4）。"""
        return LiquidityModel(max_participation=ratio, basis=self.basis)
