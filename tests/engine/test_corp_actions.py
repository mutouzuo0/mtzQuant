# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : T-U08 公司行为测试（设计 3.14）：三时点语义/送转稀释/PIT 可见性

"""T-U08：公司行为（设计 3.14）：ex_date 计应收（现金不可用）、pay_date 转可用、
送转开盘前生效（qty×倍数、avg_cost 稀释、当日即见新持仓）、announce_date 晚于
as_of 的行为不可见（PIT）。
"""

from __future__ import annotations

from datetime import date
from datetime import datetime as dt
from pathlib import Path

import pytest

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.account import Account
from mtzquant.engine.corp_actions import CorpActionRecord, CorpActionType, CorporateAction
from mtzquant.engine.orders import Fill, OrderDirection

SESSION_START = dt(2026, 6, 15, 9, 15)


def _account(cash: float = 100_000.0) -> Account:
    return Account(run_id="run-1", initial_cash=cash, available_cash=cash)


def _hold(
    acct: Account, code: str = "600000.SH", qty: float = 1000.0, price: float = 10.0
) -> Account:
    acct.apply_fill(
        Fill(
            order_id="o1",
            code=code,
            side=OrderDirection.BUY,
            price=price,
            volume=qty,
            fill_time=SESSION_START,
        )
    )
    acct.settle_day()  # 模拟昨日建仓，避免 T+1 干扰
    return acct


def _div(
    code: str = "600000.SH", *, announce: date, ex: date, pay: date, per_share: float
) -> CorporateAction:
    return CorporateAction(
        code=code,
        action_type=CorpActionType.CASH_DIV,
        announce_date=announce,
        ex_date=ex,
        pay_date=pay,
        per_share_cash=per_share,
    )


def _bonus(code: str = "600000.SH", *, announce: date, ex: date, ratio: float) -> CorporateAction:
    return CorporateAction(
        code=code, action_type=CorpActionType.BONUS, announce_date=announce, ex_date=ex, ratio=ratio
    )


def test_cash_div_ex_date_credits_receivable_not_available() -> None:
    """ex_date：receivable += qty×per_share（价格已除息但现金不可用，4.4）。"""
    acct = _hold(_account(), qty=1000.0)
    act = _div(
        ex=date(2026, 6, 15), announce=date(2026, 6, 1), pay=date(2026, 6, 20), per_share=0.5
    )
    rec = act.apply_on_ex_date(acct, SESSION_START)
    assert isinstance(rec, CorpActionRecord)
    assert rec.detail["amount"] == pytest.approx(500.0)
    assert acct.receivable_cash == pytest.approx(500.0)
    assert acct.available_cash == pytest.approx(90_000.0)  # 现金不可用
    acct.assert_invariant()


def test_cash_div_pay_date_transfers_to_available() -> None:
    """pay_date：receivable → available（结算阶段⑩）。"""
    acct = _hold(_account(), qty=1000.0)
    act = _div(
        ex=date(2026, 6, 15), announce=date(2026, 6, 1), pay=date(2026, 6, 20), per_share=0.5
    )
    act.apply_on_ex_date(acct, SESSION_START)
    assert act.applied_pay_date(date(2026, 6, 20), as_of=date(2026, 6, 20))
    settled = acct.settle_dividend()
    assert settled == pytest.approx(500.0)
    assert acct.receivable_cash == 0.0
    assert acct.available_cash == pytest.approx(90_500.0)


def test_bonus_share_change_before_open_visible_same_day() -> None:
    """送转开盘前生效：qty ×(1+ratio)、avg_cost 稀释，当日策略回调即见新持仓。"""
    acct = _hold(_account(), qty=1000.0, price=10.0)
    pos = acct.positions["600000.SH"]
    assert pos.avg_cost == pytest.approx(10.0)

    act = _bonus(ex=date(2026, 6, 15), announce=date(2026, 6, 1), ratio=0.5)  # 10 送 5
    act.apply_on_ex_date(acct, SESSION_START)
    assert pos.total_qty == pytest.approx(1500.0)
    assert pos.avg_cost == pytest.approx(10.0 / 1.5)  # 成本稀释
    # 当日可见新持仓：可卖含新增股（总成本不变）
    assert pos.closeable_qty == pytest.approx(1500.0)
    acct.assert_invariant()


def test_split_factor_multiples_qty() -> None:
    """1 拆 2：数量 ×2、成本 ÷2。"""
    acct = _hold(_account(), qty=1000.0, price=10.0)
    act = CorporateAction(
        code="600000.SH",
        action_type=CorpActionType.SPLIT,
        announce_date=date(2026, 6, 1),
        ex_date=date(2026, 6, 15),
        ratio=2.0,
    )
    act.apply_on_ex_date(acct, SESSION_START)
    pos = acct.positions["600000.SH"]
    assert pos.total_qty == pytest.approx(2000.0)
    assert pos.avg_cost == pytest.approx(5.0)


def test_no_position_raises_structured_error() -> None:
    """生效时无持仓：结构化 MtzQuantError（确权于 record_date，引擎顺序保证）。"""
    acct = _account()
    act = _div(
        ex=date(2026, 6, 15), announce=date(2026, 6, 1), pay=date(2026, 6, 20), per_share=0.5
    )
    with pytest.raises(MtzQuantError, match="无持仓"):
        act.apply_on_ex_date(acct, SESSION_START)


def test_pit_visibility_announce_after_as_of() -> None:
    """announce_date 晚于 as_of 的行为不可见（3.13/3.14）：ex 当日不生效。"""
    act = _div(
        ex=date(2026, 6, 15), announce=date(2026, 6, 10), pay=date(2026, 6, 20), per_share=0.5
    )
    as_of = date(2026, 6, 9)  # 公告在 6/10 才出来
    assert not act.visible_as_of(as_of)
    assert not act.applies_on(date(2026, 6, 15), as_of=as_of)
    # 公告后可见、ex 当日生效
    assert act.visible_as_of(date(2026, 6, 11))
    assert act.applies_on(date(2026, 6, 15), as_of=date(2026, 6, 11))


def test_three_time_point_order_validation() -> None:
    """三时点约束：announce ≤ ex ≤ pay；缺少关键字段报错。"""
    with pytest.raises(MtzQuantError, match="公告日不能晚于除权日"):
        _div(ex=date(2026, 6, 15), announce=date(2026, 6, 16), pay=date(2026, 6, 20), per_share=0.5)
    with pytest.raises(MtzQuantError, match="除权日不能晚于到账日"):
        _div(ex=date(2026, 6, 15), announce=date(2026, 6, 1), pay=date(2026, 6, 10), per_share=0.5)
    with pytest.raises(MtzQuantError, match="缺少 per_share_cash"):
        CorporateAction(
            code="x",
            action_type=CorpActionType.CASH_DIV,
            announce_date=date(2026, 6, 1),
            ex_date=date(2026, 6, 15),
        )
    with pytest.raises(MtzQuantError, match="ratio 必须为正"):
        _bonus(ex=date(2026, 6, 15), announce=date(2026, 6, 1), ratio=0.0)


# ------------------------------------------------------------------
# 共享公司行为日历（设计 3.12）: data/corporate_actions/{type}/{code}.csv 驱动 session 调仓
# ------------------------------------------------------------------
def test_shared_corp_calendar_split_applies(tmp_path: Path) -> None:
    """共享日历拆股在 ex_date 开盘前生效: 持仓 ×2、成本稀释（不依赖任务配置 corp_actions）。"""
    from mtzquant.engine.engine import UnifiedBacktestEngine
    from mtzquant.engine.results import ResultStore
    from mtzquant.engine.runner import _settings_fees, build_pipeline
    from mtzquant.engine.session import BacktestSession
    from tests.fixtures.backtest_env import make_backtest_env

    env = make_backtest_env(
        tmp_path,
        code="510300.SH",
        n=30,
        price=10.0,
        strategy_text="def initialize(context):\n    pass\n\ndef on_bar(context, bar):\n    pass\n",
    )
    split_dir = env.data_root / "corporate_actions" / "split"
    split_dir.mkdir(parents=True)
    (split_dir / "510300.SH.csv").write_text(
        "announce_date,ex_date,ratio\n2020-01-08,2020-01-09,2.0\n", encoding="utf-8"
    )
    # 任务配置 corp_actions 为空 → 拆股来源只能是共享日历
    env.task.backtest.initial_positions = {"510300.SH": 1000}

    pipeline = build_pipeline(env.settings, env.task.universe)
    store = ResultStore(run_id="r_cal")
    session = BacktestSession(
        env.task,
        driver=pipeline.driver,
        provider=pipeline.provider,
        calendar=pipeline.calendar,
        run_id="r_cal",
        settings_fees=_settings_fees(env.settings),
        result_store=store,
    )
    UnifiedBacktestEngine(session, broker=session.broker).run()

    pos = session.account.positions["510300.SH"]
    assert pos.total_qty == 2000.0  # 2:1 拆股 → 数量翻倍
    assert abs(pos.avg_cost - 5.0) < 1e-9  # 成本 10 → 5 稀释
