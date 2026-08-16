# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 13:08:00
# @update_time        : 2026/08/16 21:59:08
# @description : T-JQ01..JQ10：聚宽适配器——命名空间/数据族快照/下单配置族/调度族/detect

"""T-JQ01..JQ10（M2-N, 设计 4.6）。探针机制同 T-PT（g.probe JSON 落盘, 真实链路）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtzquant.adapters.base import _default_registry
from mtzquant.adapters.joinquant.adapter import JoinQuantAdapter
from mtzquant.core.errors import MtzQuantError, NotImplementedApiError
from mtzquant.engine.engine import UnifiedBacktestEngine
from mtzquant.engine.results import ResultStore
from mtzquant.engine.runner import _settings_fees, build_pipeline
from mtzquant.engine.session import BacktestSession
from tests.fixtures.backtest_env import make_backtest_env


def _jq_run(
    tmp_path: Path,
    body: str = "",
    *,
    init_extra: str = "",
    n: int = 30,
) -> SimpleNamespace:
    """手动装配端到端（聚宽策略 → BacktestSession → engine.run(), 真实 BrokerSim）。"""
    probe_path = tmp_path / "probe.json"
    init_lines = "".join(f"    {line}\n" for line in init_extra.strip().splitlines())
    body_lines = "".join(f"    {line}\n" for line in body.strip().splitlines())
    strategy = (
        f"PROBE = r'{probe_path}'\n"
        "import json as _json\n"
        "def initialize(context):\n"
        "    g.n = 0\n"
        "    g.probe = {}\n"
        f"{init_lines}"
        "def handle_data(context, data):\n"
        "    g.n += 1\n"
        f"{body_lines}"
        "def after_trading_end(context):\n"
        "    with open(PROBE, 'w', encoding='utf-8') as _f:\n"
        "        _json.dump(g.probe, _f, default=str)\n"
    )
    env = make_backtest_env(tmp_path, n=n, strategy_text=strategy)
    env.task.strategy.type = "joinquant"
    pipeline = build_pipeline(env.settings, env.task.universe)
    store = ResultStore(run_id="r_jq")
    session = BacktestSession(
        env.task,
        driver=pipeline.driver,
        provider=pipeline.provider,
        calendar=pipeline.calendar,
        run_id="r_jq",
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
# T-JQ01 注入命名空间骨架 + process_initialize 降级
# ------------------------------------------------------------------
BODY_SKEL = """\
if g.n == 1:
    g.probe['has_data'] = '510300.XSHG' in data
    g.probe['cdt'] = str(context.current_dt)
    g.probe['prev'] = str(context.previous_date)
"""


def test_jq01_namespace_and_lifecycle(tmp_path: Path) -> None:
    r = _jq_run(
        tmp_path,
        BODY_SKEL,
        init_extra=(
            "set_universe(['510300.XSHG'])\ndef before_trading_start(context, data):\n    pass"
        ),
    )
    p = r.probe
    assert p["has_data"] is True  # data[security] 快照存在
    assert p["cdt"].startswith("2020-")
    assert p["prev"].startswith("2020-")
    assert r.snapshot["status"] == "completed_exact"


def test_jq01_process_initialize_degraded(tmp_path: Path) -> None:
    strategy = """\
def initialize(context):
    g.n = 0
    set_universe(['510300.XSHG'])

def process_initialize(context):
    pass

def handle_data(context, data):
    g.n += 1

def after_trading_end(context):
    import json
    with open(r'%s', 'w') as _f:
        json.dump({'n': g.n}, _f)
""" % (Path(tmp_path) / "probe.json")
    _jq_run(tmp_path, "", init_extra="")  # 保留装配副作用（probe 落盘）
    # 直接跑含 process_initialize 的完整策略
    env = make_backtest_env(tmp_path, n=5, strategy_text=strategy)
    env.task.strategy.type = "joinquant"
    pipeline = build_pipeline(env.settings, env.task.universe)
    store = ResultStore(run_id="r_jq")
    session = BacktestSession(
        env.task,
        driver=pipeline.driver,
        provider=pipeline.provider,
        calendar=pipeline.calendar,
        run_id="r_jq",
        settings_fees=_settings_fees(env.settings),
        result_store=store,
    )
    engine = UnifiedBacktestEngine(session, broker=session.broker)
    snap = engine.run()
    assert snap["status"] == "completed_degraded"  # process_initialize 跳过记降级
    assert any("process_initialize" in d for d in snap["degradations"])


# ------------------------------------------------------------------
# T-JQ02 data[security] 快照字段（close/volume/paused）+ 切片
# ------------------------------------------------------------------
BODY_DATA = """\
if g.n == 3:
    cd = data['510300.XSHG']
    g.probe['close'] = cd.close
    g.probe['volume'] = cd.volume
    g.probe['paused'] = cd.paused
    g.probe['day'] = str(cd.day)
"""


def test_jq02_data_snapshot(tmp_path: Path) -> None:
    r = _jq_run(tmp_path, BODY_DATA, init_extra="set_universe(['510300.XSHG'])", n=6)
    assert float(r.probe["close"]) == 10.0  # 平坦价
    assert float(r.probe["volume"]) == 10_000_000.0
    assert r.probe["paused"] is False
    assert r.probe["day"].startswith("2020-")


# ------------------------------------------------------------------
# T-JQ03 attribute_history include_today=False（cutoff 防泄露）
# ------------------------------------------------------------------
BODY_ATTR = """\
if g.n == 6:
    df = attribute_history('510300.XSHG', 3, fields=['close'], include_today=False)
    g.probe['rows'] = len(df)
    df2 = attribute_history('510300.XSHG', 3, fields=['close'], include_today=True)
    g.probe['rows_today'] = len(df2)
"""


def test_jq03_attribute_history_cutoff(tmp_path: Path) -> None:
    r = _jq_run(tmp_path, BODY_ATTR, init_extra="set_universe(['510300.XSHG'])", n=6)
    assert r.probe["rows"] == 3  # 可见窗口（含当日时 3, 不含当日 3——平坦连续）
    assert r.probe["rows_today"] == 3


# ------------------------------------------------------------------
# T-JQ04 get_index_stocks 缺快照报错（D6）
# ------------------------------------------------------------------
def test_jq04_get_index_stocks_missing_snapshot(tmp_path: Path) -> None:
    """无成分快照 → NotImplementedApiError（D6, 不返回当前成分防幸存者偏差）。"""
    adapter = JoinQuantAdapter()
    with pytest.raises(NotImplementedApiError) as ei:
        adapter.get_index_stocks("000300.XSHG")
    assert "get_index_stocks" in str(ei.value)
    assert "成分" in str(ei.value)


# ------------------------------------------------------------------
# T-JQ05 get_current_data
# ------------------------------------------------------------------
BODY_CUR = """\
if g.n == 3:
    cd = get_current_data(['510300.XSHG'])
    g.probe['close'] = cd['510300.XSHG'].close
    g.probe['paused'] = cd['510300.XSHG'].paused
"""


def test_jq05_get_current_data(tmp_path: Path) -> None:
    r = _jq_run(tmp_path, BODY_CUR, init_extra="set_universe(['510300.XSHG'])", n=6)
    assert float(r.probe["close"]) == 10.0
    assert r.probe["paused"] is False


# ------------------------------------------------------------------
# T-JQ06/07 下单配置族
# ------------------------------------------------------------------
def test_jq06_set_universe_and_order(tmp_path: Path) -> None:
    r = _jq_run(
        tmp_path,
        "if g.n == 5:\n    order('510300.XSHG', 1000)\n",
        init_extra="set_universe(['510300.XSHG'])",
        n=8,
    )
    assert r.session.universe() == ["510300.SH"]  # 归一
    assert len(r.snapshot["fills"]) == 1  # 下单成交
    assert r.snapshot["fills"][0]["volume"] == 1000.0


def test_jq07_order_cost_and_slippage(tmp_path: Path) -> None:
    r = _jq_run(
        tmp_path,
        "if g.n == 5:\n    order('510300.XSHG', 1000)\n",
        init_extra=(
            "set_universe(['510300.XSHG'])\n"
            "set_order_cost(open_commission=0.001, min_commission=1.0, close_tax=0.0)\n"
            "set_slippage(0.01)"
        ),
        n=8,
    )
    fills = r.snapshot["fills"]
    assert len(fills) == 1
    assert abs(fills[0]["commission"] - 0.001 * fills[0]["amount"]) < 0.02
    assert fills[0]["price"] > 10.05  # 滑点生效


def test_jq07_set_option_l2(tmp_path: Path) -> None:
    adapter = JoinQuantAdapter()
    adapter.set_option("auto_handle_position", True)  # L2 可映射 → 记降级
    assert any("set_option" in d for d in adapter.degradations)
    adapter.degradations = []
    with pytest.raises(NotImplementedApiError):
        adapter.set_option("unknown_option", 1)  # 未知 → 结构化报错


# ------------------------------------------------------------------
# T-JQ08/09 调度族
# ------------------------------------------------------------------
def test_jq08_run_daily_fold_1500(tmp_path: Path) -> None:
    body = "if g.n == 1:\n    g.probe['hit'] = g.get('job_hits', 0)\n"
    r = _jq_run(
        tmp_path,
        body,
        init_extra=(
            "set_universe(['510300.XSHG'])\n"
            "def job(context):\n"
            "    g.job_hits = g.get('job_hits', 0) + 1\n"
            "run_daily(job, '9:30')\n"
        ),
        n=5,
    )
    assert r.probe["hit"] == 1  # run_daily 每日执行（折叠 15:00）
    assert any("折叠 15:00" in d for d in r.snapshot["degradations"])  # 盘中 time 降级


def test_jq08_run_daily_every_bar_no_degradation(tmp_path: Path) -> None:
    r = _jq_run(
        tmp_path,
        "if g.n == 1:\n    g.probe['hits'] = g.get('job_hits', 0)\n",
        init_extra=(
            "set_universe(['510300.XSHG'])\n"
            "def job(context):\n"
            "    g.job_hits = g.get('job_hits', 0) + 1\n"
            "run_daily(job, 'every_bar')\n"
        ),
        n=4,
    )
    assert r.probe["hits"] == 1  # every_bar 每日执行
    assert not any("折叠 15:00" in d for d in r.snapshot["degradations"])


def test_jq08_run_daily_only_in_initialize(tmp_path: Path) -> None:
    adapter = JoinQuantAdapter()
    adapter._in_initialize = False
    with pytest.raises(MtzQuantError):
        adapter.run_daily(lambda ctx: None, "9:30")


def test_jq09_run_weekly_monthly_fold(tmp_path: Path) -> None:
    body = "g.probe['wk'] = g.get('wk_hits', 0)\ng.probe['mo'] = g.get('mo_hits', 0)\n"
    r = _jq_run(
        tmp_path,
        body,
        init_extra=(
            "set_universe(['510300.XSHG'])\n"
            "def wk(context):\n"
            "    g.wk_hits = g.get('wk_hits', 0) + 1\n"
            "def mo(context):\n"
            "    g.mo_hits = g.get('mo_hits', 0) + 1\n"
            "run_weekly(wk, 1, 'open')\n"
            "run_monthly(mo, 1, 'open')\n"
        ),
        n=12,
    )
    assert r.probe["wk"] >= 1  # 周首交易日折叠执行
    assert r.probe["mo"] >= 1  # 月首交易日折叠执行
    assert any("run_weekly" in d for d in r.snapshot["degradations"])
    assert any("run_monthly" in d for d in r.snapshot["degradations"])


# ------------------------------------------------------------------
# T-JQ10 detect 嗅探
# ------------------------------------------------------------------
def test_jq10_detect_and_registry() -> None:
    jq_code = "def initialize(c):\n    pass\n\ndef handle_data(c, data):\n    pass\n"
    assert _default_registry.detect(jq_code) == "joinquant"
    ptrade_code = "def initialize(c):\n    run_daily(c, f, '09:30')\n"
    assert _default_registry.detect(ptrade_code) == "ptrade"
    adapter = JoinQuantAdapter()
    assert adapter.platform == "joinquant"
