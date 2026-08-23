# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 22:30:00
# @update_time        : 2026/08/16 21:59:08
# @description : Account 账户：现金四分类(available/receivable/frozen/total)+持仓(avg_cost 轨迹)

"""Account 账户与估值（设计 5.5 / 4.4 / 3.14）。

- 记账与估值一律使用 raw_price（成交价唯一记账基准，3.14 价格语义约束）；
- 持仓 UniformPosition：total_qty/closeable_qty/today_qty/avg_cost/last_price/market_value，
  预留 direction/avg_margin（期货期权字段位，v1 恒多头全款语义）；
- 现金四分类恒等式：total = available + receivable + frozen；
- 买入加权平均成本、卖出 avg_cost 不变、清仓移除（T-U07 成本轨迹）。

公司行为（分红 receivable→available、送转稀释 avg_cost）由 B8 CorpActions 应用本模块接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.orders import Fill, OrderDirection


@dataclass
class Position:
    """单一持仓（设计 5.5）。avg_cost 为 raw 加权平均成本（不含费用）。"""

    code: str
    total_qty: float = 0.0
    today_qty: float = 0.0  # 今日买入量（T+1 冻结解算用）
    avg_cost: float = 0.0
    last_price: float = 0.0  # 最近估值价（raw，停牌沿用最近有效收盘）
    _frost: float = 0.0  # 冻结可卖（卖出订单已接受未成交，预留）
    direction: str = "long"  # 期货期权预留位（v1 恒多头）
    avg_margin: float = 0.0  # 期货期权预留位

    @property
    def closeable_qty(self) -> float:
        """T+1 可卖 = 总持仓 - 今日买入量（不含冻结；卖出冻结在引擎层总账）。"""
        return max(0.0, self.total_qty - self.today_qty)

    @property
    def market_value(self) -> float:
        """持仓市值 = 估值价 × 总数量（raw）。"""
        return self.last_price * self.total_qty

    def buy(self, qty: float, price: float) -> None:
        if qty <= 0 or price < 0:
            raise MtzQuantError(f"买入参数非法: qty={qty} price={price}", stage="account")
        new_total = self.total_qty + qty
        self.avg_cost = (self.avg_cost * self.total_qty + price * qty) / new_total
        self.total_qty = new_total
        self.today_qty += qty

    def sell(self, qty: float, price: float) -> None:
        if qty <= 0 or price < 0:
            raise MtzQuantError(f"卖出参数非法: qty={qty} price={price}", stage="account")
        if qty > self.closeable_qty:
            raise MtzQuantError(
                f"卖出 {qty} 超过可卖 {self.closeable_qty}（T+1 约束）",
                stage="account",
                hint=f"今日买入 {self.today_qty} 不可卖（{self.code}）",
            )
        self.total_qty -= qty
        if self.total_qty == 0:
            self.avg_cost = 0.0  # 清仓：成本复位
        self.today_qty = min(self.today_qty, self.total_qty)

    def roll_to_new_day(self) -> None:
        """结算后转入新交易日：今日买入量清零（T+1 解冻）。"""
        self.today_qty = 0.0

    def apply_share_change(self, factor: float) -> None:
        """送转/拆并股（开盘前生效，3.14）：数量 ×factor、avg_cost 同比例稀释。

        总成本不变、每股成本稀释到 1/factor；今日买入量与可卖同比例缩放。
        """
        if factor <= 0:
            raise MtzQuantError(f"送转/拆并倍数必须为正，得到 {factor}", stage="account")
        if self.total_qty == 0:
            return
        self.total_qty *= factor
        self.today_qty *= factor
        self.avg_cost /= factor


@dataclass
class Account:
    """统一账户（设计 5.5）。现金四分类恒等式 total = available + receivable + frozen。"""

    run_id: str
    initial_cash: float
    available_cash: float
    receivable_cash: float = 0.0  # 应收现金分红（ex_date 计、pay_date 转可用，3.14）
    frozen_cash: float = 0.0  # 买单冻结（Accepted 后，5.3.4）
    positions: dict[str, Position] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_cash < 0 or self.available_cash < 0:
            raise MtzQuantError(f"初始资金不能为负: initial={self.initial_cash}", stage="account")

    @property
    def total_cash(self) -> float:
        return self.available_cash + self.receivable_cash + self.frozen_cash

    def assert_invariant(self) -> None:
        """恒等式不变量：现金四分类 total = available + receivable + frozen。"""
        parts = (self.available_cash, self.receivable_cash, self.frozen_cash)
        if abs(self.total_cash - sum(parts)) > 1e-9:
            raise MtzQuantError(
                f"现金四分类恒等式被破坏: {self.total_cash} != "
                f"{self.available_cash}+{self.receivable_cash}+{self.frozen_cash}",
                stage="account",
                hint="total_cash = available + receivable + frozen",
            )

    # ---------------- 冻结（5.3.4 ④①）----------------

    def freeze_cash(self, amount: float) -> None:
        if amount < 0 or amount > self.available_cash:
            raise MtzQuantError(
                f"冻结金额非法: {amount}（可用 {self.available_cash}）", stage="account"
            )
        self.available_cash -= amount
        self.frozen_cash += amount
        self.assert_invariant()

    def release_frozen_cash(self, amount: float) -> None:
        # 容差 1e-9: 多单并发冻结时单笔 est 与累计 frozen_cash 的浮点舍入差（宁钳位不误炸）
        if amount < 0 or amount > self.frozen_cash + 1e-9:
            raise MtzQuantError(
                f"释放冻结金额非法: {amount}（已冻结 {self.frozen_cash}）", stage="account"
            )
        released = min(amount, self.frozen_cash)
        self.frozen_cash -= released
        self.available_cash += released
        if self.frozen_cash < 1e-9:
            self.frozen_cash = 0.0  # 钳位: 避免 -1e-10 级负冻结（恒等式容差内）
        self.assert_invariant()

    # ---------------- 成交记账（3.14：一律 raw_price）----------------

    def apply_fill(self, fill: Fill) -> None:
        """买入扣可用资金+费用、加权成本；卖出加回笼资金-费用、成本不变。"""
        pos = self.positions.get(fill.code)
        if pos is None:
            pos = Position(code=fill.code)
            self.positions[fill.code] = pos
        if fill.side in (OrderDirection.BUY, OrderDirection.OPEN_LONG, OrderDirection.CLOSE_SHORT):
            self._apply_buy(fill, pos)
        else:
            self._apply_sell(fill, pos)
        pos.last_price = fill.price  # 成交价即最新估值价
        self.assert_invariant()

    def _apply_buy(self, fill: Fill, pos: Position) -> None:
        need = fill.amount + fill.total_fee
        if need > self.available_cash:
            raise MtzQuantError(
                f"买入资金不足: 需 {need} 可用 {self.available_cash}（{fill.code}）",
                stage="account",
                hint="下单接受时已冻结预估价（5.3.4），此处为防御性终检",
            )
        self.available_cash -= need
        pos.buy(fill.volume, fill.price)

    def _apply_sell(self, fill: Fill, pos: Position) -> None:
        pos.sell(fill.volume, fill.price)
        self.available_cash += fill.amount - fill.total_fee
        if pos.total_qty == 0:
            del self.positions[fill.code]  # 清仓移除

    # ---------------- 挂单卖出同步入账（聚宽语义, 5.3.4 增强）----------------
    def pending_sell(self, code: str, qty: float, proceeds: float) -> None:
        """卖出挂单受理即入账: 持仓减量 + 回款计入可用（保持 NAV 不变式; 成交/过期再确认）。"""
        pos = self.positions.get(code)
        if pos is None or qty <= 0:
            return
        self.available_cash += proceeds
        pos.total_qty = max(0.0, pos.total_qty - qty)
        if pos.total_qty == 0:
            pos.avg_cost = 0.0  # 清仓待确认: 保留 Position 占位（成本/估值价供过期恢复）

    def apply_sell_credited(self, fill: Fill, order_qty: float, est: float) -> None:
        """挂单卖出的成交确认: 成交回款补差（实际-预估）, 恢复未成交部分。"""
        un_filled = max(0.0, order_qty - fill.volume)
        if un_filled > 0:
            pos = self.positions.get(fill.code)
            if pos is not None:
                pos.total_qty += un_filled  # 受理时按全量预减, 恢复未成交部分
        self.available_cash += (fill.amount - fill.total_fee) - est
        pos = self.positions.get(fill.code)
        if pos is not None and pos.total_qty <= 0:
            del self.positions[fill.code]

    def restore_pending_sell(self, code: str, qty: float, est: float) -> None:
        """卖出挂单过期/撤销: 恢复持仓、退回预估回款。"""
        pos = self.positions.get(code)
        if pos is None:
            pos = Position(code=code)
            self.positions[code] = pos
        pos.total_qty += qty
        self.available_cash -= est
        if pos.total_qty <= 0:
            del self.positions[code]

    # ---------------- 公司行为接口（B8 调用）----------------

    def credit_dividend(self, amount: float) -> None:
        """ex_date 记应收（价格已除息、现金不可用，4.4 三日期语义）。"""
        if amount < 0:
            raise MtzQuantError(f"分红金额不能为负: {amount}", stage="account")
        self.receivable_cash += amount
        self.assert_invariant()

    def settle_dividend(self) -> float:
        """pay_date 到账：应收 → 可用（结算阶段⑩，5.1），返回本次到账额。"""
        amount = self.receivable_cash
        self.receivable_cash = 0.0
        self.available_cash += amount
        self.assert_invariant()
        return amount

    def settle_day(self) -> None:
        """日终结算：T+1 今日买入量清零、释放全部冻结（当日单尾盘失效）。"""
        for pos in self.positions.values():
            pos.roll_to_new_day()
        if self.frozen_cash:
            self.release_frozen_cash(self.frozen_cash)
