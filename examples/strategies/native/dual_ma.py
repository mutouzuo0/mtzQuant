# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 06:48:31
# @update_time        : 2026/08/16 06:48:31
# @description : demo 双均线策略（native）: fast/slow 双均线 + 仓位切换, 供 configs/demo_dual_ma.json 端到端

"""demo 双均线策略（native 平台）。

金叉买入至目标仓位（position_ratio × 总资产）、死叉清仓。仅做六要素口径演示——
不做收益回测准确性评判（4.9.2 纪律）。
"""


def initialize(context):
    context.g["fast"] = 5
    context.g["slow"] = 20
    context.g["code"] = "510300.SH"
    context.g["position_ratio"] = 0.9


def on_bar(context, bar):
    code = context.g["code"]
    df = context.history(code, context.g["slow"] + 1)
    if len(df) < context.g["slow"]:
        return
    fast = float(df["close"].tail(context.g["fast"]).mean())
    slow = float(df["close"].tail(context.g["slow"]).mean())
    pos = context.account.positions.get(code)
    held = pos.total_qty if pos is not None else 0.0
    if fast > slow and held == 0:
        context.adapter.order_target_value(
            code, context.g["position_ratio"] * context.account.total_value
        )
    elif fast <= slow and held > 0:
        context.adapter.order_target_value(code, 0.0)
