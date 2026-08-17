'''
木头左
Date: 2025-11-24 22:01:34
LastEditTime: 2025-12-09 20:57:24
'''
# 风险提示：所有策略仅做为学习使用，年化收益率均是基于历史数据回测，回测收益不代表未来，所有标的代码仅做为示例，不做为投资建议，盈亏自负。
import pandas as pd
from jqdata import *

def initialize(context):
    # -----------------------策略参数-----------------------
    # 选股数目
    g.stock_num = 10
    # 价格阈值下限，不能低于这个价格
    g.price_dn = 2.0
    # 价格阈值上限，不能高于这个价格
    g.price_up = 9.0
    # PEG阈值下限，不能低于
    g.peg_dn = -3.0
    # PEG阈值上限，不能高于
    g.peg_up = 3.0
    # 策略基准：中证500指数
    g.benchmark = '000905.XSHG' 
    
    # -----------------------回测设置-----------------------
    # 设定基准
    set_benchmark(g.benchmark)
    # 开启动态复权模式(真实价格)
    set_option('use_real_price', True)
    # 打开防未来函数
    set_option("avoid_future_data", True)
    # 股票类每笔交易时的手续费是：买入时佣金万分之三，卖出时佣金万分之三加千分之一印花税, 每笔交易佣金最低扣5块钱
    set_order_cost(OrderCost(close_tax=0.001, open_commission=0.0003, close_commission=0.0003, min_commission=5), type='stock')
    # ***将设置滑点设置为1%，去掉滑点回测收益会高很多***
    set_slippage(PriceRelatedSlippage(0.01))
    # 过滤掉order系列API产生的比error级别低的log
    log.set_level('order', 'error')
    
    # -----------------------运行函数-----------------------
    # 获取持仓中昨日涨停的股票列表
    run_daily(get_zt_stock_list, time='9:05', reference_security=g.benchmark)
    # 选择满足条件的股票
    run_monthly(select_stocks, 1 ,time='9:30')
    # 交易股票
    run_monthly(trade_stocks, 1 ,time='14:55')
    # 卖出持仓中昨日涨停但今日开板的股票
    run_daily(check_limit_up, time='14:00')

# 选择满足条件的股票
def select_stocks(context):
    # -------------------过滤部分-------------------
    # 获取全部的股票列表
    stock_list = get_all_securities('stock', context.previous_date).index.tolist()
    # 剔除科创版和北交所股票
    stock_list = filter_kcbj_stock(stock_list)
    # 剔除ST及其他具有退市标签的股票
    stock_list = filter_st_stock(stock_list)
    # 剔除停牌股票
    stock_list = filter_paused_stock(stock_list)
    # 剔除涨停和跌停的股票
    stock_list = filter_limitup_stock(context, stock_list)
    stock_list = filter_limitdown_stock(context, stock_list)
    # -------------------选股部分-------------------
    # 选择高股息股票(全市场前25%)
    stock_list = get_dividend_ratio_filter_list(context, stock_list, False, 0, 0.25)    
    # 选择PEG在指定范围的股票，已经按照股票市值从小到大进行排序
    stock_list = get_peg_filter_list(context, stock_list, g.peg_dn, g.peg_up)
    # 选择股价在指定范围内的股票
    stock_list = get_price_filter_list(context, stock_list, g.price_dn, g.price_up)
    # 选择最终的前g.stock_num支股票
    g.select_list = stock_list[:g.stock_num]

# 交易股票    
def trade_stocks(context):
    cdata = get_current_data()
    select_list = g.select_list
    # 卖出股票
    for s in context.portfolio.positions:
        if (s  not in select_list) :
            log.info('卖出', s, cdata[s].name)
            order_target(s, 0)
    # 买入股票
    position_count = len(context.portfolio.positions)
    if g.stock_num > position_count:
        psize = context.portfolio.available_cash/(g.stock_num - position_count)
        for s in select_list:
            if s not in context.portfolio.positions:
                log.info('买入', s, cdata[s].name)
                order_value(s, psize)
                if len(context.portfolio.positions) == g.stock_num:
                    break

# 根据最近一年分红除以当前总市值计算股息率并筛选    
def get_dividend_ratio_filter_list(context, stock_list, sort, p1, p2):
    time1 = context.previous_date
    time0 = time1 - datetime.timedelta(days=365)
    #获取分红数据，由于finance.run_query最多返回4000行，以防未来数据超限，最好把stock_list拆分后查询再组合
    #某只股票可能一年内多次分红，导致其所占行数大于1，所以interval不要取满4000
    interval = 1000 
    list_len = len(stock_list)
    #截取不超过interval的列表并查询
    q = query(finance.STK_XR_XD.code, finance.STK_XR_XD.a_registration_date, finance.STK_XR_XD.bonus_amount_rmb
    ).filter(
        finance.STK_XR_XD.a_registration_date >= time0,
        finance.STK_XR_XD.a_registration_date <= time1,
        finance.STK_XR_XD.code.in_(stock_list[:min(list_len, interval)]))
    df = finance.run_query(q)
    #对interval的部分分别查询并拼接
    if list_len > interval:
        df_num = list_len // interval
        for i in range(df_num):
            q = query(finance.STK_XR_XD.code, finance.STK_XR_XD.a_registration_date, finance.STK_XR_XD.bonus_amount_rmb
            ).filter(
                finance.STK_XR_XD.a_registration_date >= time0,
                finance.STK_XR_XD.a_registration_date <= time1,
                finance.STK_XR_XD.code.in_(stock_list[interval*(i+1):min(list_len,interval*(i+2))]))
            temp_df = finance.run_query(q)
            df = df.append(temp_df)
    dividend = df.fillna(0)
    dividend = dividend.set_index('code')
    dividend = dividend.groupby('code').sum()
    temp_list = list(dividend.index) #query查询不到无分红信息的股票，所以temp_list长度会小于stock_list
    #获取股票的总市值数据
    q = query(valuation.code,valuation.market_cap).filter(valuation.code.in_(temp_list))
    cap = get_fundamentals(q, date=time1)
    cap = cap.set_index('code')
    #计算股息率
    DR = pd.concat([dividend, cap], axis=1, sort=False)
    DR['dividend_ratio'] = (DR['bonus_amount_rmb']/10000) / DR['market_cap']
    #排序并筛选
    DR = DR.sort_values(by=['dividend_ratio'], ascending=sort)
    final_list = list(DR.index)[int(p1*len(DR)):int(p2*len(DR))]
    return final_list

# 选择PEG在指定范围的股票                
def get_peg_filter_list(context, stocks, thre1, thre2):
    # 获取PEG数据
    q = query(valuation.code,
                valuation.pe_ratio / indicator.inc_net_profit_year_on_year,
                ).filter(
                    valuation.pe_ratio / indicator.inc_net_profit_year_on_year>thre1,
                    valuation.pe_ratio / indicator.inc_net_profit_year_on_year<thre2,
                    valuation.code.in_(stocks))
    df_fundamentals = get_fundamentals(q, date = None)       
    stocks = list(df_fundamentals.code)
    # 按市值从小到大进行
    df = get_fundamentals(query(valuation.code).filter(valuation.code.in_(stocks)).order_by(valuation.market_cap.asc()))
    return list(df.code)

# 选择股价在指定范围内的股票
def get_price_filter_list(context, stock_list, thre1, thre2):
	last_prices = history(1, unit='1m', field='close', security_list=stock_list)
	return [stock for stock in stock_list if stock in context.portfolio.positions.keys()
			or (last_prices[stock][-1]>=thre1 and last_prices[stock][-1]<=thre2)]
    
# 获取持仓中昨日涨停的股票列表（若有）
def get_zt_stock_list(context):
    #获取已持有列表
    g.high_limit_list = []
    hold_list = list(context.portfolio.positions)
    if hold_list:
        df = get_price(hold_list, end_date=context.previous_date, frequency='daily',
                       fields=['close', 'high_limit'],
                       count=1, panel=False)
        g.high_limit_list = df[df['close'] == df['high_limit']]['code'].tolist()
        
#  处理昨日涨停股票
def check_limit_up(context):
    # 获取持仓的昨日涨停列表
    current_data = get_current_data()
    if g.high_limit_list:
        for stock in g.high_limit_list:
            if current_data[stock].last_price < current_data[stock].high_limit:
                log.info("[%s]涨停打开，卖出" %stock)
                order_target(stock, 0)
            else:
                log.info("[%s]涨停，继续持有" %stock)
 
# 过滤科创板和北交所股票
def filter_kcbj_stock(stock_list):
    for stock in stock_list[:]:
        if stock[0] == '4' or stock[0] == '8' or stock[:2] == '68':
            stock_list.remove(stock)
    return stock_list

# 过滤停牌股票
def filter_paused_stock(stock_list):
	current_data = get_current_data()
	return [stock for stock in stock_list if not current_data[stock].paused]

# 过滤ST及其他具有退市标签的股票
def filter_st_stock(stock_list):
	current_data = get_current_data()
	return [stock for stock in stock_list
			if not current_data[stock].is_st
			and 'ST' not in current_data[stock].name
			and '*' not in current_data[stock].name
			and '退' not in current_data[stock].name]

# 过滤涨停的股票
def filter_limitup_stock(context, stock_list):
	last_prices = history(1, unit='1m', field='close', security_list=stock_list)
	current_data = get_current_data()
	# 已存在于持仓的股票即使涨停也不过滤，避免此股票再次可买，但因被过滤而导致选择别的股票
	return [stock for stock in stock_list if stock in context.portfolio.positions.keys()
			or last_prices[stock][-1] < current_data[stock].high_limit]

# 过滤跌停的股票
def filter_limitdown_stock(context, stock_list):
	last_prices = history(1, unit='1m', field='close', security_list=stock_list)
	current_data = get_current_data()
	return [stock for stock in stock_list if stock in context.portfolio.positions.keys()
			or last_prices[stock][-1] > current_data[stock].low_limit]
