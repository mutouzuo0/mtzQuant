# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : T-U10 MetricsCalculator 测试（设计 8.4）：逐公式手算向量+反例场景

"""T-U10：MetricsCalculator（设计 8.4）：逐公式手算向量：年化(ANN=250)/波动(ddof=1)/
最大回撤含峰谷日/夏普(rf 可配)/索提诺 TDD(全样本 min 平方)/卡玛/Beta/Alpha/TE/IR/
简单与几何超额/FIFO 回合配对胜率（分批建仓分批卖出）/单边换手率(取 max 非求和)。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from mtzquant.engine.metrics import (
    TradeRecord,
    compute_metrics,
    pair_fifo_rounds,
    turnover_single_sided,
)

D = date


def test_annual_return_and_volatility_hand_math() -> None:
    """nav=[1,1.1,1.21]，r=[0.1,0.1]：年化=1.21^(250/2)-1；波动为 0（收益恒同）。"""
    m = compute_metrics([1.0, 1.1, 1.21], ann=250)
    assert m.risk.total_return == pytest.approx(0.21)
    assert m.risk.annual_return == pytest.approx(1.21**125 - 1)
    assert m.risk.annual_volatility == pytest.approx(0.0)  # ddof=1 + 恒定收益
    assert m.risk.daily_win_rate == pytest.approx(1.0)


def test_annual_volatility_ddof_one() -> None:
    """r=[0,0.01,0.0198] 型收益 → std(ddof=1)×√250 手算。"""
    nav = [1.0, 1.0, 1.01, 1.03]
    r = np.array([0.0, 0.01, 1.03 / 1.01 - 1])
    expected = float(np.std(r, ddof=1)) * np.sqrt(250)
    m = compute_metrics(nav, ann=250)
    assert m.risk.annual_volatility == pytest.approx(expected)
    assert m.risk.total_return == pytest.approx(0.03)


def test_max_drawdown_with_peak_trough_dates() -> None:
    """nav=[1,1.2,0.9,1.1,0.88]：峰 1→谷 5，回撤 1-0.88/1.2=0.2667。"""
    dates = [D(2026, 1, 1), D(2026, 1, 2), D(2026, 1, 3), D(2026, 1, 4), D(2026, 1, 5)]
    m = compute_metrics([1.0, 1.2, 0.9, 1.1, 0.88], dates=dates)
    info = m.risk.max_drawdown
    assert info.value == pytest.approx(1 - 0.88 / 1.2)
    assert info.peak_date == D(2026, 1, 2) and info.peak_index == 1
    assert info.trough_date == D(2026, 1, 5) and info.trough_index == 4


def test_sharpe_with_configurable_rf() -> None:
    """r=[0.05]*2 均值 0.05、std=0？取非恒模：r=[0.1,0,0.05,-0.05]。"""
    nav = np.array([1.0])
    for r in (0.1, 0.0, 0.05, -0.05):
        nav = np.append(nav, nav[-1] * (1 + r))
    r_series = nav[1:] / nav[:-1] - 1
    mean = float(np.mean(r_series))
    std = float(np.std(r_series, ddof=1))
    # rf=0
    m0 = compute_metrics(nav, ann=250)
    assert m0.risk.sharpe == pytest.approx(mean / std * np.sqrt(250))
    # rf_d = 0.04/250
    rf_d = 0.04 / 250
    m1 = compute_metrics(nav, ann=250, risk_free_annual=0.04)
    assert m1.risk.sharpe == pytest.approx((mean - rf_d) / std * np.sqrt(250))


def test_sortino_tdd_uses_full_sample_min_square() -> None:
    """反例向量：负收益子样本 std ≠ TDD（专门构造，8.4.1 明确公式）。

    r=[0.5,-0.3]：负收益子样本只有 -0.3 → std=0（或 NaN）；而
    TDD=√(mean(min(r,0)²))=√((0+0.09)/2)=0.21213——两者不等价，公式用 TDD。
    """
    nav = np.array([1.0, 1.5, 1.05])
    m = compute_metrics(nav, ann=250)
    tdd = float(np.sqrt((0.0**2 + 0.3**2) / 2))
    assert tdd == pytest.approx(0.21213203435596428)
    mean_r = float(np.mean(nav[1:] / nav[:-1] - 1))
    assert m.risk.sortino == pytest.approx(mean_r / tdd * np.sqrt(250))
    # 反例核心断言：TDD ≠ 负收益子样本 std
    assert tdd != pytest.approx(0.0)


def test_calmar_hand_math() -> None:
    """nav=[1,1.1,1.21,1.21,0.968]：年化与回撤比值。"""
    nav = np.array([1.0, 1.1, 1.21, 1.21, 0.968])
    m = compute_metrics(nav, ann=250)
    ann_return = 0.968 ** (250 / 4) - 1
    mdd = 1 - 0.968 / 1.21
    assert m.risk.calmar == pytest.approx(ann_return / mdd)
    assert m.risk.max_drawdown.value == pytest.approx(mdd)


def test_beta_alpha_te_ir_hand_math() -> None:
    """构造 r 与 rb 线性相关：beta=定价、te=0、ir=NaN?（diff 恒 0 → std=0 → IR 未定义）"""
    nav = np.array([1.0, 1.1, 1.21, 1.331])  # 每日 +10%
    bnav = np.array([1.0, 1.05, 1.1025, 1.157625])  # 每日 +5%
    m = compute_metrics(nav, benchmark_nav=bnav, ann=250, risk_free_annual=0.02)
    rb = bnav[1:] / bnav[:-1] - 1
    r = nav[1:] / nav[:-1] - 1
    rf_d = 0.02 / 250
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / np.var(rb, ddof=1))
    alpha = float((np.mean(r - rf_d) - beta * np.mean(rb - rf_d)) * 250)
    te = float(np.std(r - rb, ddof=1) * np.sqrt(250))
    assert m.benchmark is not None
    assert m.benchmark.beta == pytest.approx(beta)
    assert m.benchmark.annual_alpha == pytest.approx(alpha)
    assert m.benchmark.tracking_error == pytest.approx(te)
    assert m.benchmark.excess_simple == pytest.approx(1.331 - 1.157625)
    assert m.benchmark.excess_geometric == pytest.approx((1.331 + 1) / (1.157625 + 1) - 1)


def test_information_ratio_hand_math() -> None:
    """构造 diff=r-rb=[0.01,0.02,-0.01]：IR=mean/std(ddof=1)×√250、TE 一致。"""
    r = np.array([0.03, 0.04, -0.02])
    rb = np.array([0.02, 0.02, -0.01])
    nav = np.concatenate(([1.0], np.cumprod(1 + r)))
    bnav = np.concatenate(([1.0], np.cumprod(1 + rb)))
    m = compute_metrics(nav, benchmark_nav=bnav, ann=250)
    diff = r - rb
    assert m.benchmark is not None
    assert m.benchmark.tracking_error == pytest.approx(float(np.std(diff, ddof=1) * np.sqrt(250)))
    assert m.benchmark.information_ratio == pytest.approx(
        float(np.mean(diff) / np.std(diff, ddof=1) * np.sqrt(250))
    )


def test_fifo_rounds_batch_split_pairs() -> None:
    """分批建仓分批卖出 FIFO 配对（8.4.3）：卖出跨多批买入按数量拆分为配对回合。

    买入 100@10、200@12 → 卖出 150@15（对上 100@10 与 50@12 两批次 = 两个配对回合），
    再卖出 150@16（对上 150@12）。
    回合 500+150=650、600 → 净利 1250，全部盈利。
    """
    trades = [
        TradeRecord("600000.SH", +100.0, 10.0, D(2026, 1, 1)),
        TradeRecord("600000.SH", +200.0, 12.0, D(2026, 1, 2)),
        TradeRecord("600000.SH", -150.0, 15.0, D(2026, 1, 5)),
        TradeRecord("600000.SH", -150.0, 16.0, D(2026, 1, 6)),
    ]
    stats = pair_fifo_rounds(trades)
    assert stats.total_rounds == 3  # 跨批买入拆分 → 3 个配对回合
    assert stats.win_rounds == 3
    assert stats.win_rate == pytest.approx(1.0)
    assert stats.net_pnl == pytest.approx(500.0 + 150.0 + 600.0)
    assert stats.avg_win == pytest.approx((500.0 + 150.0 + 600.0) / 3)
    assert np.isnan(stats.profit_factor)  # 无亏损回合 → NaN


def test_fifo_rounds_with_losses() -> None:
    """混合胜负：胜率按回合计、盈亏比=平均盈利/平均亏损。"""
    trades = [
        TradeRecord("a.SH", +100.0, 10.0, D(2026, 1, 1)),
        TradeRecord("a.SH", -100.0, 11.0, D(2026, 1, 2)),  # 盈利 +100
        TradeRecord("b.SH", +100.0, 10.0, D(2026, 1, 3)),
        TradeRecord("b.SH", -100.0, 9.0, D(2026, 1, 4)),  # 亏损 -100
    ]
    stats = pair_fifo_rounds(trades)
    assert stats.total_rounds == 2 and stats.win_rounds == 1
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.avg_win == pytest.approx(100.0)
    assert stats.avg_loss == pytest.approx(-100.0)
    assert stats.profit_factor == pytest.approx(1.0)


def test_fifo_rejects_unmatched_sell() -> None:
    with pytest.raises(ValueError, match="无对应买入"):
        pair_fifo_rounds([TradeRecord("x.SH", -100.0, 10.0, D(2026, 1, 1))])


def test_turnover_takes_daily_max_not_sum() -> None:
    """单日先买后卖：换手取 max(买入额, 卖出额) 而非求和（8.4.3 口径反例）。

    8/15 买 10000 卖 8000 → 当日 max=10000；8/16 仅买 5000 → 5000。
    总换手额=15000（而非 23000）；年化=15000/100000/0.5=0.3。
    """
    turnover = turnover_single_sided(
        buy_amounts={D(2026, 8, 15): 10_000.0, D(2026, 8, 16): 5_000.0},
        sell_amounts={D(2026, 8, 15): 8_000.0},
        mean_total_asset=100_000.0,
        years=0.5,
    )
    assert turnover.total_turnover_amount == pytest.approx(15_000.0)
    assert turnover.annualized_turnover == pytest.approx(0.3)


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="长度不一致"):
        compute_metrics([1.0, 1.1], benchmark_nav=[1.0])
