# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : MetricsCalculator 绩效指标（设计 8.4）：收益/风险/基准相关/交易统计纯计算

"""MetricsCalculator 绩效指标（设计 8.4 计算公式，全量精确、纯 numpy、无 I/O）。

口径要点：
  净值 nav[0..n]（nav[0]=1.0 基期），日收益 r[t]=nav[t]/nav[t-1]-1；
  ANN 年化天数默认 250（A股惯例，按 calendar profile 可配）；
  波动/方差 ddof=1；索提诺 TDD=√((1/n)·Σmin(r-MAR,0)²) 对**全样本**取 min 平方；
  最大回撤含峰→谷日期；Beta/Alpha/TE/IR 以基准序列为参照；
  FIFO 回合配对与胜率、单边换手率（按日 max 后求和，非全区间一次 max）。
gross/net 双口径：费用归集在引擎侧（费前/费后净值各算一遍），本模块保持纯计算。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np


@dataclass(frozen=True)
class MaxDrawdownInfo:
    """最大回撤幅度与峰→谷日期（报告输出起止日，8.4.1）。"""

    value: float
    peak_date: date | None = None
    trough_date: date | None = None
    peak_index: int = -1
    trough_index: int = -1


@dataclass(frozen=True)
class RiskMetrics:
    """收益与风险组（8.4.1）。"""

    total_return: float
    annual_return: float
    annual_volatility: float
    max_drawdown: MaxDrawdownInfo
    sharpe: float
    sortino: float
    calmar: float
    daily_win_rate: float


@dataclass(frozen=True)
class BenchmarkMetrics:
    """基准相关组（8.4.2）。"""

    beta: float
    annual_alpha: float
    tracking_error: float
    information_ratio: float
    excess_simple: float
    excess_geometric: float


@dataclass(frozen=True)
class Metrics:
    """一次净值口径的完整指标集合（v1：gross/net 各算一个实例）。"""

    risk: RiskMetrics
    benchmark: BenchmarkMetrics | None = None
    metrics_version: str = "8.4-v1"

    @staticmethod
    def compute(
        nav: np.ndarray,
        *,
        dates: list[date | datetime] | None = None,
        benchmark_nav: np.ndarray | None = None,
        ann: int = 250,
        risk_free_annual: float = 0.0,
        mar: float | None = None,
    ) -> Metrics:
        """§8.4 全量精确计算（纯函数；qt 输入由引擎按净值口径准备）。"""
        return compute_metrics(
            nav=nav,
            dates=dates,
            benchmark_nav=benchmark_nav,
            ann=ann,
            risk_free_annual=risk_free_annual,
            mar=mar,
        )


def _returns(nav: np.ndarray) -> np.ndarray:
    nav = np.asarray(nav, dtype=float)
    if nav.size < 2 or nav[0] <= 0:
        raise ValueError(f"净值序列非法: size={nav.size} nav[0]={nav[0]}")
    return nav[1:] / nav[:-1] - 1.0


def _std_ddof(x: np.ndarray, ddof: int = 1) -> float:
    """ddof 窗口不足时返回 NaN（而非 numpy 的 RuntimeWarning）：口径上不可估计。"""
    if x.size <= ddof:
        return float("nan")
    return float(np.std(x, ddof=ddof))


def _var_ddof(x: np.ndarray, ddof: int = 1) -> float:
    if x.size <= ddof:
        return float("nan")
    return float(np.var(x, ddof=ddof))


def compute_metrics(
    nav: np.ndarray | list[float],
    *,
    dates: list[date | datetime] | None = None,
    benchmark_nav: np.ndarray | list[float] | None = None,
    ann: int = 250,
    risk_free_annual: float = 0.0,
    mar: float | None = None,
) -> Metrics:
    """§8.4 全量精确计算（纯函数；gross/net 口径由引擎分别传入 nav）。"""
    nav = np.asarray(nav, dtype=float)
    r = _returns(nav)
    n = r.size  # 收益数（交易日数）
    rf_d = risk_free_annual / ann
    mar_d = rf_d if mar is None else mar
    if n == 0:
        raise ValueError("净值序列过短，无法计算收益")

    # ---- 8.4.1 收益与风险 ----
    ann_return = nav[-1] ** (ann / n) - 1.0
    ann_vol = _std_ddof(r) * np.sqrt(ann)
    running_max = np.maximum.accumulate(nav)
    dd = 1.0 - nav / running_max
    trough_i = int(np.argmax(dd))
    peak_i = int(np.argmax(nav[: trough_i + 1]))
    max_dd_info = MaxDrawdownInfo(
        value=float(dd[trough_i]),
        peak_date=_date_at(dates, peak_i),
        trough_date=_date_at(dates, trough_i),
        peak_index=peak_i,
        trough_index=trough_i,
    )
    denom = _std_ddof(r)
    sharpe = _annualized_ratio(float(np.mean(r - rf_d)), denom, ann)
    tdd = float(np.sqrt(np.mean(np.minimum(r - mar_d, 0.0) ** 2)))
    sortino = _annualized_ratio(float(np.mean(r - rf_d)), tdd, ann)
    calmar = ann_return / max_dd_info.value if max_dd_info.value > 0 else float("nan")
    daily_win_rate = float(np.mean(r > 0.0))

    risk = RiskMetrics(
        total_return=float(nav[-1] - 1.0),
        annual_return=ann_return,
        annual_volatility=ann_vol,
        max_drawdown=max_dd_info,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        daily_win_rate=daily_win_rate,
    )

    # ---- 8.4.2 基准相关 ----
    if benchmark_nav is None:
        return Metrics(risk=risk)
    bnav = np.asarray(benchmark_nav, dtype=float)
    if bnav.size != nav.size:
        raise ValueError(f"基准净值与策略净值长度不一致: {bnav.size} != {nav.size}")
    rb = _returns(bnav)
    rb_var = _var_ddof(rb)
    beta = float("nan")
    if rb_var == rb_var and rb_var > 0:
        beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb_var)
    alpha = float((np.mean(r - rf_d) - beta * np.mean(rb - rf_d)) * ann)
    diff = r - rb
    te = _std_ddof(diff) * np.sqrt(ann)
    diff_std = _std_ddof(diff)
    ir = _annualized_ratio(float(np.mean(diff)), diff_std, ann)
    excess_simple = float(nav[-1] - bnav[-1])
    excess_geometric = float((nav[-1] + 1.0) / (bnav[-1] + 1.0) - 1.0)
    return Metrics(
        risk=risk,
        benchmark=BenchmarkMetrics(
            beta=beta,
            annual_alpha=alpha,
            tracking_error=te,
            information_ratio=ir,
            excess_simple=excess_simple,
            excess_geometric=excess_geometric,
        ),
    )


def _annualized_ratio(num: float, denom: float, ann: int) -> float:
    """年化比值 num/denom×√ann（夏普/IR 共用）；分母为 NaN 或不正 → NaN。"""
    if denom != denom or denom <= 0:
        return float("nan")
    return float(num / denom * np.sqrt(ann))


def _date_at(dates: list[date | datetime] | None, i: int) -> date | None:
    if not dates or i < 0 or i >= len(dates):
        return None
    d = dates[i]
    return d.date() if isinstance(d, datetime) else d


# ============================================================
# 8.4.3 交易统计
# ============================================================


@dataclass
class TradeRecord:
    """一笔成交（回合配对的输入：分批建仓/分批卖出在此拆分）。"""

    code: str
    qty: float  # 买入 +、卖出 -（按数量拆分后的单位成交）
    price: float
    traded_at: date


@dataclass(frozen=True)
class RoundStats:
    """FIFO 平仓回合统计（8.4.3，按回合非按卖出笔）。"""

    total_rounds: int
    win_rounds: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    net_pnl: float


def pair_fifo_rounds(trades: list[TradeRecord]) -> RoundStats:
    """FIFO 默认配对：同一标的买入队列先进先出与卖出配对成回合。

    分批建仓/分批卖出按数量拆分（如卖出 300 对上前两批各 100/200 的成本）；
    跨批次成本按配对批次计算——"卖出一笔 = 一个平仓"的旧口径废弃（8.4.3）。
    """
    buys: dict[str, deque[tuple[float, float]]] = {}  # code → [(剩余qty, 买入价)]
    pnls: list[float] = []
    for t in sorted(trades, key=lambda x: (x.traded_at, x.code)):
        if t.qty >= 0:
            buys.setdefault(t.code, deque()).append((t.qty, t.price))
            continue
        remaining = -t.qty
        queue = buys.get(t.code)
        if not queue:
            raise ValueError(f"卖出 {t.code} {remaining} 无对应买入（FIFO 无法配对）")
        while remaining > 1e-9:
            if not queue:
                raise ValueError(f"卖出 {t.code} 超过买入存量（FIFO 配对失败）")
            qty, buy_price = queue[0]
            take = min(qty, remaining)
            queue_rest = qty - take
            if queue_rest <= 1e-9:
                queue.popleft()
            else:
                queue[0] = (queue_rest, buy_price)
            pnls.append((t.price - buy_price) * take)
            remaining -= take
    wins = [p for p in pnls if p > 0.0]
    losses = [p for p in pnls if p <= 0.0]
    total = len(pnls)
    win = len(wins)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("nan")
    return RoundStats(
        total_rounds=total,
        win_rounds=win,
        win_rate=win / total if total else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        net_pnl=sum(pnls),
    )


@dataclass(frozen=True)
class TurnoverStats:
    """单边换手率（8.4.3：按日取 max 后对日求和，再除以平均资产与年数）。"""

    annualized_turnover: float
    total_turnover_amount: float


def turnover_single_sided(
    buy_amounts: dict[date, float],
    sell_amounts: dict[date, float],
    *,
    mean_total_asset: float,
    years: float,
) -> TurnoverStats:
    """单边换手率 = Σ_d max(当日买入额, 当日卖出额) / mean(总资产) / 年数。

    构造"单日先买后卖"场景验证取 max 而非求和（8.4.3 口径明确）。
    """
    if mean_total_asset <= 0 or years <= 0:
        raise ValueError(f"平均资产与年数必须为正: asset={mean_total_asset} years={years}")
    days = set(buy_amounts) | set(sell_amounts)
    total = sum(max(buy_amounts.get(d, 0.0), sell_amounts.get(d, 0.0)) for d in days)
    return TurnoverStats(
        annualized_turnover=total / mean_total_asset / years,
        total_turnover_amount=total,
    )
