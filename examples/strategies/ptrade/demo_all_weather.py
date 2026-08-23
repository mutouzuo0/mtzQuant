# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 12:44:00
# @update_time        : 2026/08/16 12:44:00
# @description : demo 双均线策略（PTrade 官方写法）: run_daily 调度 + get_history + order_target_value

"""demo 双均线策略（PTrade 平台, M2-L7 样例）。

PTrade 官方语义零改动风格: initialize + set_universe + run_daily 注册 +
get_history + order_target_value; 金叉建仓至目标市值、死叉清仓。
仅做六要素口径演示——不做收益回测准确性评判（4.9.2 纪律）。
"""


def initialize(context):
    g.fast = 5
    g.slow = 20
    g.code = '510300.SS'
    g.position_ratio = 0.9
    set_universe([g.code])
    run_daily(context, trade, '14:50')


def trade(context):
    code = g.code
    closes = get_history(g.slow + 1, '1d', 'close', [code])
    if len(closes) < g.slow:
        return
    fast = float(closes[-g.fast:].mean())
    slow = float(closes[-g.slow:].mean())
    pos = get_position(code)
    held = pos.amount
    portfolio = context.portfolio
    if fast > slow and held == 0:
        order_target_value(code, portfolio.portfolio_value * g.position_ratio)
    elif fast < slow and held > 0:
        order_target_value(code, 0.0)


def handle_data(context, data):
    """主入口（日线每日 15:00）; 交易逻辑已在 run_daily 注册, 此处仅日志。"""
    pass
