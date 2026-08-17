# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 08:20:00
# @update_time        : 2026/08/17 08:20:00
# @description : 菜场大妈量化V1（聚宽原版）→ mtzQuant 受支持 API 的近似改写版；移除基本面族（M3 未实现），
#                涨跌停用日线近似；语义差异见文件头「近似项清单」（原版见同目录 菜场大妈量化V1_聚宽.py）

"""近似改写说明（对照原版 strategies/joinquant/菜场大妈量化V1_聚宽.py）：

[保留] 月度换仓 ≤10 只; 剔除科创板(68)/北交所(4,8 开头)/ST/退市(按名称)/停牌/
       涨跌停; 股价∈[2.0, 9.0]; 卖出不在新列表持仓 / 买入用可用现金等分;
       昨日涨停今日开板卖出; 费用(佣金万3+最低5 / 卖出印花千1); 滑点 1%。

[近似]（框架/数据限制所致, 逐项说明）
  - 删除 `from jqdata import *`（本地无 jqdata 包, API 由适配器预注入）;
  - `log.set_level('order','error')` 删除（注入 log 无 set_level, 仅 info/warn/error）;
  - `history(1, '1m', ...)` → `history(1, '1d', ...)`（日线回测无分钟线）;
  - 涨/跌停判定: 快照无 high_limit/low_limit 字段, 用「收盘≈最高(低)价 且 当日
    涨跌幅 ≥ +9.5% / ≤ -9.5%（创业板 300 开头按 ±19.5%）」近似;
  - 昨日涨停: attribute_history 近 3 根日线取「昨日」判定（策略 15:00 折叠点
    当日 bar 可见, 防未来: 只用 D-1 与 D-2 收盘）;
  - 名称过滤: 快照无 name, 从 data/master/instruments.csv 读 code→name;
  - 卖出后的可用现金未含当日卖出回款（框架 next_open 撮合, 当日卖出次日入账）。

[移除]（基本面族未实现, M3 遗留; 语义有损）
  - 股息率前 25% 过滤（finance.STK_XR_XD 分红数据无抓取器）;
  - PEG ∈ [-3, 3] 过滤（valuation/indicator 未实现）;
  - 市值从小到大排序 → 退化为 universe 顺序取前 10（缺 market_cap 数据）。

[注意] get_all_securities 返回任务配置 universe（当前成分快照, 存在幸存者偏差）;
       内部码 600000.SH ↔ 平台码 600000.XSHG 由 _jq/_norm 互转（positions 与
       get_current_data 键为平台码, history/get_all_securities 为内部码）。
"""

import pandas as pd

# ---- 本地名称表（data/master/instruments.csv, code→name; ST/退市过滤用）----
_NAME_MAP: dict[str, str] = {}
try:
    _mf = pd.read_csv("data/master/instruments.csv", dtype={"code": str})
    _NAME_MAP = dict(zip(_mf["code"], _mf["name"].fillna("")))
except Exception:  # noqa: BLE001  master 缺失时降级为空映射（ST 过滤失效）
    pass

# 内部码 ↔ 聚宽平台码（3.4）: 600000.SH → 600000.XSHG / 000001.SZ → 000001.XSHE
_SUFFIX_MAP = {".SH": ".XSHG", ".SZ": ".XSHE", ".BJ": ".BJ"}


def _jq(code: str) -> str:
    """内部码 → 聚宽平台码（positions/get_current_data 键）。"""
    for k, v in _SUFFIX_MAP.items():
        if code.endswith(k):
            return code[: -len(k)] + v
    return code


def _norm(code: str) -> str:
    """聚宽平台码 → 内部码（history/get_all_securities 用）。"""
    for k, v in _SUFFIX_MAP.items():
        if code.endswith(v):
            return code[: -len(v)] + k
    return code


def _close_panel(codes: list[str], count: int = 2) -> pd.DataFrame | None:
    """批量最近 count 根收盘价, 列=内部码; 兼容单标的（列名重命名）。"""
    if not codes:
        return None
    df = history(count, unit="1d", field="close", security_list=codes)
    if df is None or len(df) == 0:
        return None
    if len(codes) == 1 and codes[0] not in df.columns:
        df = pd.DataFrame({codes[0]: df["close"]})
    return df


def _limit_threshold(code: str) -> float:
    """涨跌停阈值: 创业板 20%, 其余主板 10%（科创板/北交所已在选股前剔除）。"""
    return 0.195 if code.startswith("300") else 0.095


# =====================================================================
# 初始化
# =====================================================================
def initialize(context):
    # -----------------------策略参数-----------------------
    g.stock_num = 10
    g.price_dn = 2.0
    g.price_up = 9.0
    g.benchmark = "000905.XSHG"

    # -----------------------回测设置-----------------------
    # 股票类费用: 买入佣金万3, 卖出佣金万3+印花千1, 单笔最低 5 元
    set_order_cost(
        open_tax=0.0,
        close_tax=0.001,
        open_commission=0.0003,
        close_commission=0.0003,
        min_commission=5,
    )
    # 滑点 1%（原版 PriceRelatedSlippage(0.01) → 比值近似）
    set_slippage(0.01)

    # -----------------------运行函数-----------------------
    run_daily(get_zt_stock_list, "9:05")
    run_monthly(select_stocks, 1, "9:30")
    run_monthly(trade_stocks, 1, "14:55")
    run_daily(check_limit_up, "14:00")

    # 月调度防重复（适配器把 run_monthly 折叠到月初首 5 日内可能多次触发）
    g.last_month = None
    g.select_list = []


# =====================================================================
# 月度选股
# =====================================================================
def select_stocks(context):
    month = context.current_dt.month
    if g.last_month == month:
        return
    g.last_month = month

    stock_list = get_all_securities("stock", context.previous_date)["code"].tolist()
    stock_list = filter_kcbj_stock(stock_list)
    stock_list = filter_st_stock(stock_list)
    stock_list = filter_paused_stock(context, stock_list)
    stock_list = filter_limit_stock(context, stock_list)
    stock_list = get_price_filter_list(context, stock_list, g.price_dn, g.price_up)
    # 原版按市值从小到大取前 g.stock_num; 无市值数据 → universe 顺序取前 N（近似）
    g.select_list = stock_list[: g.stock_num]
    log.info("月度选股 %d 只: %s" % (len(g.select_list), ",".join(g.select_list[:10])))


# 月度交易（卖出不在列表持仓, 买入新入选）
def trade_stocks(context):
    if g.last_month is None or g.last_month != context.current_dt.month:
        return
    select_list = g.select_list
    held = set(context.portfolio.positions)  # 平台码键

    # 卖出持仓中不在新列表的股票
    for s in held:
        if _norm(s) not in select_list:
            log.info("卖出 %s" % s)
            order_target(s, 0)

    # 买入: 等分可用现金, 填满到 g.stock_num 只
    kept = len([s for s in held if _norm(s) in select_list])
    buy_slots = g.stock_num - kept
    if buy_slots > 0:
        # 0.96 缓冲: 框架下单即按(价+滑点+费)冻结预估价(5.3.4), 10 单等分会超现金 1.8%
        psize = context.portfolio.available_cash / buy_slots * 0.96
        bought = 0
        for s in select_list:
            if _jq(s) not in held:
                log.info("买入 %s" % _jq(s))
                order_value(_jq(s), psize)
                bought += 1
                if kept + bought >= g.stock_num:
                    break


# =====================================================================
# 涨停打开处理（每日）
# =====================================================================
def get_zt_stock_list(context):
    """记录持仓中「昨日涨停」的股票（attribute_history 近 3 根, 取 D-1 判定）。"""
    g.high_limit_list = []
    for s in context.portfolio.positions:
        code = _norm(s)
        df = attribute_history(code, 3, "1d", ["close", "high", "low"])
        if df is None or len(df) < 3:
            continue
        c_prev = df["close"].iloc[-3]
        c_y = df["close"].iloc[-2]
        h_y = df["high"].iloc[-2]
        if c_prev <= 0:
            continue
        pct = (c_y - c_prev) / c_prev
        if c_y >= h_y * 0.999 and pct >= _limit_threshold(code):
            g.high_limit_list.append(s)


def check_limit_up(context):
    """昨日涨停股: 今日开板（收盘未封涨停）→ 卖出; 仍封板 → 持有。"""
    if not g.high_limit_list:
        return
    cd = get_current_data(g.high_limit_list)
    for s in g.high_limit_list:
        snap = cd.get(s)
        if snap is None or snap.close <= 0:
            continue
        if snap.close < snap.high * 0.999:  # 开板近似: 收盘未触及最高价
            log.info("[%s]涨停打开,卖出" % s)
            order_target(s, 0)
        else:
            log.info("[%s]涨停,继续持有" % s)


# =====================================================================
# 过滤族
# =====================================================================
def filter_kcbj_stock(stock_list):
    """剔除科创板(68 开头)与北交所(4/8 开头)股票。"""
    return [
        s for s in stock_list if not (s[0] in ("4", "8") or s[:2] == "68")
    ]


def filter_st_stock(stock_list):
    """剔除 ST/退市标签（按本地 master 名称; 名称缺失时保留）。"""
    out = []
    for s in stock_list:
        name = _NAME_MAP.get(s, "")
        if name and ("ST" in name or "*" in name or "退" in name):
            continue
        out.append(s)
    return out


def filter_paused_stock(context, stock_list):
    """剔除停牌股票（快照 paused 标记）。"""
    if not stock_list:
        return []
    cd = get_current_data(stock_list)
    return [s for s in stock_list if not cd.get(_jq(s), None) or not cd[_jq(s)].paused]


def filter_limit_stock(context, stock_list):
    """剔除当日涨停/跌停股票（已持仓不剔除, 避免被迫换股）。

    近似: 收盘≈最高且涨跌幅≥+9.5%(创19.5%) → 涨停; 收盘≈最低且≤-9.5% → 跌停。
    """
    if not stock_list:
        return []
    held_jq = set(context.portfolio.positions)
    cd = get_current_data(stock_list)
    panel = _close_panel(stock_list, count=2)
    if panel is None or len(panel) < 2:
        return []
    prev = panel.iloc[-2]
    out = []
    for s in stock_list:
        if _jq(s) in held_jq:
            out.append(s)
            continue
        snap = cd.get(_jq(s))
        pc = prev.get(s, 0.0)
        if snap is None or snap.close <= 0 or pc <= 0:
            continue
        pct = (snap.close - pc) / pc
        thr = _limit_threshold(s)
        is_up = snap.close >= snap.high * 0.999 and pct >= thr
        is_dn = snap.close <= snap.low * 1.001 and pct <= -thr
        if not is_up and not is_dn:
            out.append(s)
    return out


def get_price_filter_list(context, stock_list, thre1, thre2):
    """保留股价 ∈ [thre1, thre2] 的股票（已持仓不受价格限制）。"""
    if not stock_list:
        return []
    held_jq = set(context.portfolio.positions)
    panel = _close_panel(stock_list, count=1)
    if panel is None:
        return []
    last = panel.iloc[-1]
    return [
        s for s in stock_list if _jq(s) in held_jq or (thre1 <= last[s] <= thre2)
    ]
