# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 21:59:08
# @description : J report.html 单测：自包含单文件/指标卡/交互净值图/成交分页表（9.2）

"""report.html 生成测试（9.2; T-I01 依赖的 report 命令内核）。"""

from __future__ import annotations

from pathlib import Path

from mtzquant.engine.report import render_report
from mtzquant.engine.runner import run_task
from tests.fixtures.backtest_env import make_backtest_env


def test_report_html_self_contained(tmp_path: Path) -> None:
    """自包含单文件: 指标卡/语义保真/交互净值图(canvas+内嵌JS)/成交分页表, 无外部依赖。"""
    env = make_backtest_env(tmp_path)
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)

    out = render_report(result.run_id, out_root=env.out_root)
    assert out.is_file()
    html = out.read_text(encoding="utf-8")

    # 指标卡（gross/net 双口径）
    assert "总收益" in html and "夏普" in html and "最大回撤" in html
    # 标题: mtzQuant 品牌 + 回测执行时间（run_id 毫秒时间戳, 含时分秒）
    assert "mtzQuant 回测报告 · 20" in html
    import re as _re

    assert _re.search(r"mtzQuant 回测报告 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", html)
    # 语义保真（completed_exact 无降级）
    assert "completed_exact" in html
    # 交互净值+回撤 canvas（内嵌 JS: hover 数值 / ↑↓ 缩放 / ←→ 平移）
    assert 'id="nav-chart"' in html
    assert "NAV_DATA" in html
    assert "ArrowUp" in html and "ArrowDown" in html
    # 成交分页表（滚动容器 + 每页 100 条 + 页码直达; 无 order_id 列）
    assert 'id="orders-body"' in html
    assert 'id="orders-prev"' in html and 'id="orders-next"' in html
    assert 'id="orders-pages"' in html  # 页码直达按钮容器
    assert "table-scroll" in html  # 滚动容器（表体滚动条）
    assert "<th>order_id</th>" not in html
    # 自包含: 无外部脚本/资源引用（内嵌 script 允许, SVG 命名空间 xmlns 不算 CDN 依赖）
    assert 'src="http' not in html and 'href="http' not in html


def test_report_capacity_section(tmp_path: Path) -> None:
    """T-C05（8.4.4）: 容量证据四表+图（参与率/截断/不可成交/延迟）+ fills 容量列。"""
    env = make_backtest_env(tmp_path)
    result = run_task(env.task, settings=env.settings, out_root=env.out_root, persist=False)
    # fills 含容量证据列（bar_volume / participation_rate, BrokerSim 成交时记录）
    fills = result.bundle.fills
    assert fills, "应有成交"
    assert all("bar_volume" in f and "participation_rate" in f for f in fills)
    assert fills[0]["bar_volume"] > 0  # 合成量充足

    out = render_report(result.run_id, out_root=env.out_root)
    html = out.read_text(encoding="utf-8")
    # 容量与摩擦证据节（四表）
    assert "容量与摩擦证据" in html
    assert "参与率分布" in html
    assert "不可成交 / 容量截断" in html
    assert "成交延迟分布" in html


def test_report_missing_run_raises(tmp_path: Path) -> None:
    """缺失 run 目录 → 结构化错误（hint 引导先 run）。"""
    import pytest

    from mtzquant.core.errors import MtzQuantError

    with pytest.raises(MtzQuantError):
        render_report("r_nope", out_root=tmp_path / "results")
