# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 11:22:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-PT01..PT17：PTrade 适配器——字段投影/状态矩阵/BarData/API 族/设置族/调度/detect

"""T-PT01..PT17（M2-L, 设计 4.7 / 附录C）。

探针机制: _ptrade_run 模板化生成策略（init_extra/body 两段拼装）, 策略把 g.probe
JSON 落盘, 测试读回断言——真实驱动链路（session+engine+BrokerSim）。
期望值手算依据: 平坦价 10.0、初始资金 1_000_000、NEXT_OPEN+half_spread=0.001、
佣金 max(5, 万1×额)（make_backtest_env 默认; 测试方案 §11 纪律）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtzquant.adapters.base import _default_registry
from mtzquant.adapters.ptrade.adapter import PTradeAdapter
from mtzquant.adapters.ptrade.objects import (
    PTRADE_STATUS,
    make_bar_data,
    ptrade_status_of,
)
from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.engine import UnifiedBacktestEngine
from mtzquant.engine.results import ResultStore
from mtzquant.engine.runner import _settings_fees, build_pipeline
from mtzquant.engine.session import BacktestSession
from tests.fixtures.backtest_env import make_backtest_env


def _ptrade_run(
    tmp_path: Path,
    strategy_body: str = "",
    *,
    init_extra: str = "",
    n: int = 30,
) -> SimpleNamespace:
    """手动装配端到端: ptrade 策略 → BacktestSession → engine.run()（真实 BrokerSim）。

    模板: initialize = [g.n/g.probe 初始化 + init_extra(多行)]; handle_data = [计数 + body]。
    """
    probe_path = tmp_path / "probe.json"
    init_lines = "".join(f"    {line}\n" for line in init_extra.strip().splitlines())
    body_lines = "".join(f"    {line}\n" for line in strategy_body.strip().splitlines())
    strategy = (
        f"PROBE_PATH = r'{probe_path}'\n"
        "import json as _json\n"
        "def initialize(context):\n"
        "    g.n = 0\n"
        "    g.probe = {}\n"
        f"{init_lines}"
        "def handle_data(context, data):\n"
        "    g.n += 1\n"
        f"{body_lines}"
        "def after_trading_end(context):\n"
        "    with open(PROBE_PATH, 'w', encoding='utf-8') as _f:\n"
        "        _json.dump(g.probe, _f, default=str)\n"
    )
    env = make_backtest_env(tmp_path, n=n, strategy_text=strategy)
    env.task.strategy.type = "ptrade"
    pipeline = build_pipeline(env.settings, env.task.universe)
    store = ResultStore(run_id="r_pt")
    session = BacktestSession(
        env.task,
        driver=pipeline.driver,
        provider=pipeline.provider,
        calendar=pipeline.calendar,
        run_id="r_pt",
        settings_fees=_settings_fees(env.settings),
        result_store=store,
    )
    engine = UnifiedBacktestEngine(session, broker=session.broker)
    snapshot = engine.run()
    probe = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else {}
    return SimpleNamespace(
        snapshot=snapshot, probe=probe, session=session, records=store.all_records()
    )


# ------------------------------------------------------------------
# T-PT01 官方字段投影（Context/Portfolio/Position, 4.7 字段表）
# ------------------------------------------------------------------
BODY_FIELDS = """\
if g.n == 5:
    o = order('510300.SS', 1000)
    g.probe['receipt_type'] = type(o).__name__
    g.probe['capital_base'] = context.capital_base
    g.probe['sim_freq'] = context.sim_params.data_frequency
    g.probe['blotter_dt'] = str(context.blotter.current_dt)
if g.n == 6:
    p = context.portfolio
    g.probe['portfolio_value'] = p.portfolio_value
    g.probe['available_cash'] = p.available_cash
    g.probe['starting_cash'] = p.starting_cash
    pos = get_position('510300.SS')
    g.probe['pos_sid'] = pos.sid
    g.probe['pos_amount'] = pos.amount
    g.probe['pos_enable'] = pos.enable_amount
    g.probe['pos_cost'] = pos.cost_basis
    g.probe['pos_today'] = pos.today_amount
    g.probe['pos_last_px'] = pos.last_sale_price
if g.n == 20:
    order_target_value('510300.SS', 0.0)
if g.n == 21:
    g.probe['pos_after_sell'] = get_position('510300.SS').amount
    g.probe['get_positions_n'] = len(get_positions())
"""


def test_pt01_official_fields_projection(tmp_path: Path) -> None:
    r = _ptrade_run(tmp_path, BODY_FIELDS, init_extra="set_universe(['510300.SS'])")
    p = r.probe
    assert r.snapshot["status"] == "completed_exact"
    assert p["receipt_type"] == "PTradeOrder"
    assert p["capital_base"] == 1_000_000.0  # initialize 可读（官方语义）
    assert p["sim_freq"] == "day"
    assert p["blotter_dt"].startswith("2020-")  # 每 bar 刷新
    # 手算: 第5根下单 1000 股, 次日开盘 10.0×1.001=10.01 成交, 佣金 max(5, 10010×1e-4)=5.01
    assert p["pos_sid"] == "510300.XSHG"  # 官方 sid（外部码, 4.7）
    assert p["pos_amount"] == 1000.0
    assert p["pos_enable"] == 0.0  # T+1: 当日买入不可卖
    assert abs(p["pos_cost"] - 10.011) < 0.01  # (10010 + 5.01 佣金)/1000 ≈ 10.015 稀释后
    assert p["pos_last_px"] == 10.0  # 平坦收盘价估值
    # 手算: 佣金 max(5, 10010×1e-4)=5.0; 现金 1e6−10010−5=989985 + 市值 1000×10=10000 → 999985
    assert abs(p["portfolio_value"] - 999_985.0) < 1.0
    assert p["starting_cash"] == 1_000_000.0
    assert p["pos_after_sell"] == 0.0  # 清仓
    assert p["get_positions_n"] == 0  # positions 移除


# ------------------------------------------------------------------
# T-PT02 状态映射矩阵（子集 {2,7,8,6,9} + 部撤 5; 0 未报不可达）
# ------------------------------------------------------------------
class _FakeOrder:
    def __init__(self, status: str, qty: float, filled: float) -> None:
        self.status = status
        self.qty = qty
        self.filled_qty = filled


def test_pt02_status_matrix() -> None:
    from mtzquant.engine.orders import OrderStatus

    m = [
        (_FakeOrder(OrderStatus.PENDING, 1000, 0), "2"),
        (_FakeOrder(OrderStatus.PARTIALLY_FILLED, 1000, 400), "7"),
        (_FakeOrder(OrderStatus.FILLED, 1000, 1000), "8"),
        (_FakeOrder(OrderStatus.CANCELLED, 1000, 0), "6"),
        (_FakeOrder(OrderStatus.EXPIRED, 1000, 0), "6"),
        (_FakeOrder(OrderStatus.EXPIRED, 1000, 300), "5"),  # 部撤
        (_FakeOrder(OrderStatus.REJECTED, 1000, 0), "9"),
    ]
    produced = set()
    for order, expected in m:
        got = ptrade_status_of(order)  # type: ignore[arg-type]
        assert got == expected, f"{order.status} → {got}（期望 {expected}）"
        produced.add(got)
    assert produced == {"2", "7", "8", "6", "5", "9"}  # 回测产出子集
    assert "0" not in produced  # 未报不可达（受理即已报）——已知近似
    assert set(PTRADE_STATUS) == {"0", "2", "5", "6", "7", "8", "9"}


# ------------------------------------------------------------------
# T-PT03 BarData 全字段（日线; 分钟字段补 0 归 M5 字段位先占）
# ------------------------------------------------------------------
def test_pt03_bar_data_factory_fields() -> None:
    bd = make_bar_data(
        symbol="510300.XSHG",
        dt=datetime(2026, 1, 2, 15, 0),
        open=10.0,
        close=10.5,
        high=10.8,
        low=9.9,
        volume=1_000_000,
        money=10_500_000,
        preclose=10.0,
        high_limit=11.0,
        low_limit=9.0,
        is_open=1,
    )
    for field in (
        "symbol",
        "name",
        "dt",
        "is_open",
        "open",
        "close",
        "price",
        "low",
        "high",
        "volume",
        "money",
        "preclose",
        "high_limit",
        "low_limit",
        "unlimited",
    ):
        assert hasattr(bd, field), f"BarData 缺字段 {field}"
    assert bd.price == bd.close  # price=现价（日线=收盘）
    assert bd.is_open == 1 and bd.unlimited is False


BODY_BAR = """\
if g.n == 3:
    bd = data['510300.XSHG']
    g.probe['bd'] = {k: str(getattr(bd, k)) for k in
                     ('symbol','open','close','price','volume','money','preclose',
                      'high_limit','low_limit','is_open','unlimited')}
"""


def test_pt03_bar_data_in_handle_data(tmp_path: Path) -> None:
    r = _ptrade_run(tmp_path, BODY_BAR, init_extra="set_universe(['510300.SS'])", n=6)
    bd = r.probe["bd"]
    assert bd["symbol"] == "510300.XSHG"
    assert float(bd["open"]) == 10.0 and float(bd["close"]) == 10.0  # 平坦价
    assert float(bd["price"]) == 10.0
    assert float(bd["volume"]) == 10_000_000.0
    assert float(bd["money"]) == 100_000_000.0  # 10.0 × 1e7（make_bars 口径）
    assert bd["is_open"] == "1" and bd["unlimited"] == "False"


# ------------------------------------------------------------------
# T-PT04~PT10 API 族注入（每族 ≥1）
# ------------------------------------------------------------------
BODY_APIS = """\
if g.n == 5:
    order('510300.SS', 1000)
if g.n == 6:
    g.probe['history_n'] = int(len(get_history(3, '1d', 'close')))
    px = get_price('510300.SS', start_date='2020-01-01', end_date='2020-01-10')
    g.probe['price_rows'] = int(len(px))
    g.probe['trading_day'] = str(get_trading_day())
    g.probe['trade_days_n'] = len(get_trade_days(None, None))
    g.probe['all_days_n'] = len(get_all_trades_days())
    g.probe['is_trade'] = is_trade()
    g.probe['freq'] = get_frequency()
    g.probe['check_limit'] = check_limit('510300.SS')
    g.probe['orders_n'] = len(get_orders())
    g.probe['open_n'] = len(get_open_orders())
    g.probe['trades_n'] = len(get_trades())
    g.probe['order_obj'] = get_order(list(get_orders().keys())[0]).entrust_no
if g.n == 20:
    order_target_value('510300.SS', 0.0)
"""


def test_pt04_to_pt10_api_families(tmp_path: Path) -> None:
    r = _ptrade_run(tmp_path, BODY_APIS, init_extra="set_universe(['510300.SS'])")
    p = r.probe
    assert p["history_n"] == 3  # get_history(3) → 3 根
    assert 1 <= p["price_rows"] <= 10  # get_price 区间过滤
    assert p["trade_days_n"] == 30 and p["all_days_n"] == 30  # 数据日并集（n=30）
    assert p["trading_day"].startswith("2020-")
    assert p["is_trade"] is False  # 恒 False（4.7）
    assert p["freq"] == "1d"
    cl = p["check_limit"]
    assert set(cl) == {"is_limit_up", "is_limit_down", "paused"}
    assert cl["is_limit_up"] is False and cl["paused"] is False
    assert p["orders_n"] == 1
    assert p["open_n"] == 0  # 已成交 → 无未终态
    assert p["trades_n"] == 1
    assert p["order_obj"].startswith("pt")


BODY_SNAPSHOT = """\
if g.n == 2:
    snap = get_snapshot(['510300.SS'])
    g.probe['snap_open'] = snap['510300.XSHG'].is_open
    g.probe['snap_close'] = snap['510300.XSHG'].close
"""


def test_pt07_get_snapshot_degradation(tmp_path: Path) -> None:
    r = _ptrade_run(tmp_path, BODY_SNAPSHOT, init_extra="set_universe(['510300.SS'])", n=4)
    assert r.probe["snap_open"] == 1
    assert float(r.probe["snap_close"]) == 10.0
    assert any("get_snapshot" in d for d in r.snapshot["degradations"])  # 5.2-⑤


BODY_CANCEL = """\
if g.n == 5:
    o = order('510300.SS', 1000)
    g.probe['entrust'] = o.entrust_no
    cancel_order(o)
    g.probe['cancel_no'] = o.cancel_entrust_no
if g.n == 6:
    o = get_order(g.probe['entrust'])
    g.probe['status_after_cancel'] = o.status
    g.probe['filled_after_cancel'] = o.filled
"""


def test_pt07_cancel_order_flow(tmp_path: Path) -> None:
    r = _ptrade_run(tmp_path, BODY_CANCEL, init_extra="set_universe(['510300.SS'])", n=8)
    assert r.probe["cancel_no"] == r.probe["entrust"] + "-c"  # 撤单号（受理即记）
    # 同 bar 下单即撤: sync 绑定后立即撤 → 下一 bar 可见已撤（PTrade 撤单异步语义近似）
    assert r.probe["status_after_cancel"] == "6"  # 已撤
    assert float(r.probe["filled_after_cancel"]) == 0.0
    assert len(r.snapshot["fills"]) == 0  # 撤单后无成交
    events = [e for e in r.snapshot["events"] if e["event_type"] == "cancel"]
    assert len(events) == 1


# ------------------------------------------------------------------
# T-PT11~PT13 设置族
# ------------------------------------------------------------------
def test_pt11_set_volume_ratio(tmp_path: Path) -> None:
    r = _ptrade_run(
        tmp_path, "", init_extra="set_universe(['510300.SS'])\nset_volume_ratio(0.5)", n=4
    )
    assert r.session.broker.models.liquidity.max_participation == 0.5


BODY_YPOS = """\
if g.n == 1:
    p = get_position('510300.SS')
    g.probe['init_amount'] = p.amount
    g.probe['init_cost'] = p.cost_basis
    g.probe['init_enable'] = p.enable_amount
"""


def test_pt12_set_yesterday_position(tmp_path: Path) -> None:
    r = _ptrade_run(
        tmp_path,
        BODY_YPOS,
        init_extra=(
            "set_universe(['510300.SS'])\n"
            "set_yesterday_position({'510300.SS': {'amount': 2000, 'cost_basis': 9.5}})"
        ),
        n=4,
    )
    assert r.probe["init_amount"] == 2000.0
    assert abs(r.probe["init_cost"] - 9.5) < 1e-9  # cost_basis 优先（三字段语义）
    assert r.probe["init_enable"] == 2000.0  # 昨仓 T+1 可卖


def test_pt13_set_universe_effective(tmp_path: Path) -> None:
    r = _ptrade_run(tmp_path, "", init_extra="set_universe(['510300.SS'])", n=4)
    assert r.session.universe() == ["510300.SH"]  # 归一生效


def test_pt13_set_limit_mode(tmp_path: Path) -> None:
    r = _ptrade_run(
        tmp_path,
        "",
        init_extra="set_universe(['510300.SS'])\nset_limit_mode('UNLIMITED')",
        n=4,
    )
    assert r.session.broker.models.liquidity.max_participation == 1.0
    assert any("UNLIMITED" in d for d in r.snapshot["degradations"])


BODY_BUY5 = """\
if g.n == 5:
    order('510300.SS', 1000)
"""


def test_pt_set_commission_applies(tmp_path: Path) -> None:
    r = _ptrade_run(
        tmp_path,
        BODY_BUY5,
        init_extra="set_universe(['510300.SS'])\nset_commission('STOCK', 0.001, 1.0, 0.0, 0.0)",
        n=8,
    )
    fills = r.snapshot["fills"]
    assert len(fills) == 1
    # 千1佣金: 1000×10.01=10010 → 10.01（>min=1）; 手算
    assert abs(fills[0]["commission"] - 0.001 * fills[0]["amount"]) < 0.01


def test_pt_set_slippage_applies(tmp_path: Path) -> None:
    r = _ptrade_run(
        tmp_path,
        BODY_BUY5,
        init_extra="set_universe(['510300.SS'])\nset_slippage(0.01)",
        n=8,
    )
    fills = r.snapshot["fills"]
    assert len(fills) == 1
    # 买: 10.0×1.001×1.01 ≈ 10.12（vs 无滑点 10.01）; 手算下界
    assert fills[0]["price"] > 10.05


# ------------------------------------------------------------------
# T-PT14~PT16 调度语义
# ------------------------------------------------------------------
BODY_RUN_DAILY_LATE = """\
run_daily(context, lambda ctx: None, '09:30')
"""


def test_pt14_run_daily_only_in_initialize(tmp_path: Path) -> None:
    with pytest.raises(MtzQuantError) as ei:
        _ptrade_run(tmp_path, BODY_RUN_DAILY_LATE, init_extra="set_universe(['510300.SS'])", n=3)
    assert "initialize" in str(ei.value)


def _job_fn() -> str:
    return (
        "def _job(context):\n"
        "    g.probe.setdefault('job_hits', 0)\n"
        "    g.probe['job_hits'] += 1\n"
        "    g.probe['job_hour'] = context.blotter.current_dt.hour\n"
    )


def test_pt15_run_daily_folds_and_runs(tmp_path: Path) -> None:
    # _job 定义在模块层（模板尾部附加）; run_daily 注册于 initialize
    probe_path = tmp_path / "probe.json"
    r = _ptrade_run_with_extra_fn(
        tmp_path,
        body="",
        init_extra="set_universe(['510300.SS'])\nrun_daily(context, _job, '09:30')",
        extra_fn=_job_fn(),
        n=5,
        probe_path=probe_path,
    )
    assert r.probe["job_hits"] == 5  # 每日一次（折叠）
    assert r.probe["job_hour"] == 15  # 统一 15:00
    assert any("折叠 15:00" in d for d in r.snapshot["degradations"])


def _ptrade_run_with_extra_fn(
    tmp_path: Path, *, body: str, init_extra: str, extra_fn: str, n: int, probe_path: Path
) -> SimpleNamespace:
    """带模块级附加函数的端到端（job 函数定义在策略层, 供 run_daily 注册）。"""
    init_lines = "".join(f"    {line}\n" for line in init_extra.strip().splitlines())
    body_lines = "".join(f"    {line}\n" for line in body.strip().splitlines())
    strategy = (
        f"PROBE_PATH = r'{probe_path}'\n"
        "import json as _json\n"
        "def initialize(context):\n"
        "    g.n = 0\n"
        "    g.probe = {}\n"
        f"{init_lines}"
        f"{extra_fn}"
        "def handle_data(context, data):\n"
        "    g.n += 1\n"
        f"{body_lines}"
        "def after_trading_end(context):\n"
        "    with open(PROBE_PATH, 'w', encoding='utf-8') as _f:\n"
        "        _json.dump(g.probe, _f, default=str)\n"
    )
    env = make_backtest_env(tmp_path, n=n, strategy_text=strategy)
    env.task.strategy.type = "ptrade"
    pipeline = build_pipeline(env.settings, env.task.universe)
    store = ResultStore(run_id="r_pt")
    session = BacktestSession(
        env.task,
        driver=pipeline.driver,
        provider=pipeline.provider,
        calendar=pipeline.calendar,
        run_id="r_pt",
        settings_fees=_settings_fees(env.settings),
        result_store=store,
    )
    engine = UnifiedBacktestEngine(session, broker=session.broker)
    snapshot = engine.run()
    probe = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else {}
    return SimpleNamespace(
        snapshot=snapshot, probe=probe, session=session, records=store.all_records()
    )


def test_pt16_run_interval_rejected(tmp_path: Path) -> None:
    # run_interval 在 initialize 中调用 → 立即抛错（无需引擎驱动）
    adapter = PTradeAdapter()

    adapter._api_namespace()  # 确保 gateway 就绪
    adapter._in_initialize = True
    with pytest.raises(MtzQuantError) as ei:
        adapter.run_interval(None, lambda ctx: None, 60)
    assert "run_interval" in str(ei.value)


# ------------------------------------------------------------------
# T-PT17 detect 嗅探 + 注册表
# ------------------------------------------------------------------
def test_pt17_detect_and_registry() -> None:
    ptrade_code = "def initialize(context):\n    run_daily(context, f, '09:30')\n"
    assert _default_registry.detect(ptrade_code) == "ptrade"
    jq_code = "def initialize(c):\n    pass\n\ndef handle_data(c, data):\n    pass\n"
    assert _default_registry.detect(jq_code) == "joinquant"
    native_code = "def initialize(c):\n    pass\n\ndef on_bar(c, bar):\n    pass\n"
    assert _default_registry.detect(native_code) == "native"
    adapter = PTradeAdapter()
    assert adapter.platform == "ptrade"
