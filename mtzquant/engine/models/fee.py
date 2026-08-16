# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : FeeModel 费用模型：佣金 max(min,率×额) + 印花税(卖出,仅股票) + 过户费(仅沪市)

"""FeeModel 费用模型（设计 5.3.3 / 8.3.3）。

费用口径全部来自 InstrumentProfile.fee（费率档案化，撮合层不硬编码）：
  佣金   = max(min_commission, commission_rate × amount)
  印花税 = 卖出时 stamp_tax_rate × amount（股票有值、ETF 档案 0）
  过户费 = transfer_fee_rate × amount（沪市股票；非沪市/ETF 档案 0）
四项费用分账输出 FeeBreakdown，供 Fill 明细与流水对账（design 8.3.3："分账正确"）。
"""

from __future__ import annotations

from dataclasses import dataclass

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.instrument import InstrumentProfile
from mtzquant.engine.orders import OrderDirection


@dataclass(frozen=True)
class FeeBreakdown:
    """单笔成交费用分账（design 8.3.3 四项，逐项可审计）。"""

    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0

    @property
    def total(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee


@dataclass(frozen=True)
class FeeModel:
    """v1 默认费用实现。传入不同 profile.fee 即获得 ETF/可转债/股票差异（全档案驱动）。"""

    def compute(
        self,
        qty: float,
        price: float,
        profile: InstrumentProfile,
        side: OrderDirection,
    ) -> FeeBreakdown:
        if qty < 0 or price <= 0:
            raise MtzQuantError(
                f"费用计算输入非法: qty={qty} price={price}",
                stage="fee",
                hint="qty 非负且 price 必须为正",
            )
        sell_sides = (OrderDirection.SELL, OrderDirection.CLOSE_LONG, OrderDirection.CLOSE_SHORT)
        is_sell = side in sell_sides
        amount = qty * price
        fee = profile.fee
        commission = max(fee.commission_min, fee.commission_rate * amount)
        stamp_tax = fee.stamp_tax_rate * amount if is_sell else 0.0
        transfer_fee = round(fee.transfer_fee_rate * amount, 4)
        commission = round(commission, 4)
        stamp_tax = round(stamp_tax, 4)
        return FeeBreakdown(commission=commission, stamp_tax=stamp_tax, transfer_fee=transfer_fee)
