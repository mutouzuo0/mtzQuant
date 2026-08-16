# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : 公司行为模型（设计 3.14）：cash_div/bonus/split 三时点语义 + PIT 可见性

"""公司行为模型（设计 3.14）。

三时点语义（announce_date 公告 / ex_date 除权除息 / pay_date 到账）：
  现金分红 ex_date：receivable_cash += 持仓数×每股现金（价格已除息但现金不可用，4.4）；
  pay_date：receivable→available（结算阶段⑩，5.1）；
  送转/拆并股：开盘前生效 total_qty×=倍数、avg_cost 相应稀释——当日策略回调即可见新持仓。
PIT 可见性：announce_date ≤ as_of 的行为才可见（3.13，防未来公司行为泄漏）。
事件记录：CorpActionRecord → 事件流 + DB（审计，含三日期，报告列出全部公司行为）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.account import Account


class CorpActionType(StrEnum):
    CASH_DIV = "cash_div"  # 现金分红
    BONUS = "bonus"  # 送转股（10 送 5 → ratio=0.5，qty ×= 1+ratio）
    SPLIT = "split"  # 拆并股（1 拆 2 → ratio=2；2 并 1 → ratio=0.5）


@dataclass(frozen=True)
class CorporateAction:
    """一次公司行为事件（存储主键 (code, ex_date, action_type)，设计 3.14）。"""

    code: str
    action_type: CorpActionType
    announce_date: date
    ex_date: date
    pay_date: date | None = None  # 现金分红到账日；送转/拆并通常无现金流
    per_share_cash: float | None = None  # CASH_DIV：每股现金分红
    ratio: float = 0.0  # BONUS：送转比例；SPLIT：拆并股乘数（>0）

    def __post_init__(self) -> None:
        if self.announce_date > self.ex_date:
            raise MtzQuantError(
                f"公告日不能晚于除权日: {self.announce_date} > {self.ex_date}",
                stage="corp_action",
                hint="三时点必须满足 announce ≤ ex ≤ pay（设计 3.14）",
            )
        if self.pay_date is not None and self.ex_date > self.pay_date:
            raise MtzQuantError(
                f"除权日不能晚于到账日: {self.ex_date} > {self.pay_date}",
                stage="corp_action",
            )
        if self.action_type is CorpActionType.CASH_DIV:
            if self.per_share_cash is None or self.per_share_cash < 0:
                raise MtzQuantError("现金分红缺少 per_share_cash 或为负", stage="corp_action")
        elif self.ratio <= 0:
            raise MtzQuantError(f"送转/拆并 ratio 必须为正，得到 {self.ratio}", stage="corp_action")

    def visible_as_of(self, as_of: date) -> bool:
        """PIT 可见性：公告日晚于 as_of 的行为不可见（3.13/3.14）。"""
        return self.announce_date <= as_of

    def applies_on(self, session_date: date, as_of: date) -> bool:
        """当日（session_date）为 ex_date 且此时已公告，才在开盘前生效。"""
        return self.ex_date == session_date and self.visible_as_of(as_of)

    def applied_pay_date(self, session_date: date, as_of: date) -> bool:
        return self.pay_date == session_date and self.visible_as_of(as_of)

    def apply_on_ex_date(self, account: Account, session_start: datetime) -> CorpActionRecord:
        """ex_date 开盘前生效：现金分红计应收 / 送转拆并改数量并稀释成本。"""
        pos = account.positions.get(self.code)
        if pos is None:
            raise MtzQuantError(
                f"公司行为 {self.code} 生效时无持仓（确权于 record_date，引擎顺序保证）",
                stage="corp_action",
                hint="v1 持仓快照确权；本账户未持有该标的时由引擎跳过",
            )
        if self.action_type is CorpActionType.CASH_DIV:
            qty = pos.total_qty
            amount = qty * (self.per_share_cash or 0.0)
            account.credit_dividend(amount)  # 现金不可用，仅计应收
            detail = {"amount": amount, "per_share": self.per_share_cash}
        else:
            factor = 1.0 + self.ratio if self.action_type is CorpActionType.BONUS else self.ratio
            pos.apply_share_change(factor)  # 数量 ×factor、avg_cost 稀释
            detail = {"ratio": self.ratio, "factor": factor}
        return CorpActionRecord(
            code=self.code,
            action_type=self.action_type,
            ex_date=self.ex_date,
            pay_date=self.pay_date,
            apply_time=session_start,
            detail=detail,
        )


@dataclass(frozen=True)
class CorpActionRecord:
    """公司行为已生效记录（事件流 + DB 审计，报告列出全部，3.14）。"""

    code: str
    action_type: CorpActionType
    ex_date: date
    pay_date: date | None
    apply_time: datetime
    detail: dict[str, Any]  # JSON 化明细：{amount, per_share} 或 {ratio, factor}
