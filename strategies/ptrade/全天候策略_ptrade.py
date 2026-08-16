# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 15:15:35
# @update_time        : 2026/08/16 15:15:35
# @description : 全天候策略（PTrade 用户策略）: 股票/中债/长债/黄金/商品五类资产 ETF 组合, 每 22 日或资产涨幅>30% 触发再平衡, 基准沪深300

"""全天候策略（PTrade 平台, 用户策略）。

五类资产固定比例 ETF 组合(股30%/中债15%/长债40%/金7.5%/商品7.5%), run_daily 9:31 调度;
每 g.period(22) 日定期再平衡或单类资产涨幅>g.raise_rate(30%) 触发(先清仓后按目标市值买入)。
仅作平台兼容演示, 不做收益评判(4.9.2)。
"""

# 导入函数库
import numpy as np
import pandas as pd
import datetime
from scipy import stats

# 初始化函数，设定基准等等
def initialize(context):
    # 设定沪深300作为基准
    set_benchmark('000300.XSHG')

    # 输出内容到日志 log.info()
    log.info('初始函数开始运行且全局只运行一次')

    g.capital = 100000
    g.rebalanced_asset_values = {}
    g.rebalanced_asset_alloc = {}
    g.rebalanced_stock_values = {}
    g.raise_rate = 0.3 #0.3 #触发rebalance的上涨比例, <=0不触发
    g.period = 22
    g.run_count = 0
    g.pool = {
        'stock': {'rate':0.3, 'codes':[
                    { #'159915.XSHE':datetime.datetime(2011,12,9),
                      '510310.XSHG':datetime.datetime(2013,3,25),
                      '513100.XSHG':datetime.datetime(2013,5,15),
                      '513500.XSHG':datetime.datetime(2014,1,15),
                    },
                    {
                        '510210.XSHG':datetime.datetime(2011,3,25),
                        '159903.XSHE':datetime.datetime(2010,2,2),
                        '159919.XSHE':datetime.datetime(2012,5,28),
                    },
                    {
                        '159901.XSHE':datetime.datetime(2006,4,24),
                        '510180.XSHG':datetime.datetime(2006,5,18),
                    },
                    {
                        '510050.XSHG':datetime.datetime(2005,2,23)
                    }
                ]},
        'mid_bond':{'rate':0.15, 'codes':[
                    {
                     '511010.XSHG':datetime.datetime(2013,3,25)
                    }
                ]},
        'long_bond':{'rate':0.4, 'codes':[
                    {'511260.XSHG':datetime.datetime(2017,8,24) #10years
                    },
                    
                ]},
        'gold':{'rate':0.075, 'codes':[
                    {'518880.XSHG':datetime.datetime(2013,7,29)
                    },
                    {'160719.XSHE':datetime.datetime(2012,2,10)
                    }
                ]},
        'goods':{'rate':0.075, 'codes':[
                    {'165513.XSHE':datetime.datetime(2012,1,13)
                    }
                ]},
    }
    
    g.stock_map_asset = init_stock_map_asset(g.pool)
    
    ## 运行函数（reference_security为运行时间的参考标的；传入的标的只做种类区分，因此传入'000300.XSHG'或'510300.XSHG'是一样的）
      # 开盘前运行
    #run_daily(before_market_open, time='before_open', reference_security='000001.XSHG')
      # 开盘时运行
    #run_monthly(market_open, monthday=1, time='open', reference_security='000001.XSHG')
    run_daily(context, func=market_open, time='9:31')
      # 收盘后运行
    #run_daily(after_market_close, time='after_close', reference_security='000001.XSHG')
    # 提取所有ETF代码到C.trade_code_list
    g.trade_code_list = []
    for asset in g.pool:
        for codes_dict in g.pool[asset]['codes']:
            g.trade_code_list.extend(codes_dict.keys())
    g.trade_code_list = list(set(g.trade_code_list))  # 去重
    
    
def init_stock_map_asset(pool):
    stock_map_asset = {}
    for asset in pool:
        for stocks in pool[asset]['codes']:
            for code in stocks:
                stock_map_asset[code] = asset
    return stock_map_asset
    
## 开盘前运行函数
def before_market_open(context):
    # 输出运行时间
    log.info('函数运行时间(before_market_open)：'+str(context.current_dt.time()))



def get_trade_target(context):
    dt=context.current_dt
    ret = {}
    for asset in g.pool:
        stock_pool = g.pool[asset]['codes']
        target_stocks = []
        for stocks in stock_pool:
            cur_stocks_isOK = False
            for start_dt in stocks.values():
                if dt >= start_dt:
                    cur_stocks_isOK = True
                    break
            if cur_stocks_isOK:
                target_stocks = [k for k in stocks.keys()]
                break
        ret[asset] = {'rate':g.pool[asset]['rate'],
                    'codes':target_stocks,
                    }    
    return ret

def calc_stock_max_raise(context):
    max_raise_ratio = 0
    for code in context.portfolio.positions:
        print(code,context.portfolio.positions[code].value)
        ratio = context.portfolio.positions[code].value / g.rebalanced_stock_values[code] - 1
        if ratio > max_raise_ratio:
                max_raise_ratio = ratio 
    print(g.rebalanced_stock_values)
    
    return max_raise_ratio

def calc_asset_max_raise(context):
    asset_values = {}#g.rebalanced_asset_values.copy()
   
    for asset in g.rebalanced_asset_alloc:
        asset_values[asset] = 0
        codes = g.rebalanced_asset_alloc[asset]['codes']
        num = len(codes)
        per_value = g.rebalanced_asset_values[asset]
        if num > 0:
            per_value /= num
            for code in codes:
                if code in context.portfolio.positions:
                    asset_values[asset] += context.portfolio.positions[code].value
                else:
                    asset_values[asset] += per_value
        else:
            asset_values[asset] = per_value
    
    max_raise_ratio = 0
    for asset in g.rebalanced_asset_values:
        if asset in asset_values:
            ratio = asset_values[asset] / g.rebalanced_asset_values[asset] - 1
            if ratio > max_raise_ratio:
                max_raise_ratio = ratio 
    
    return max_raise_ratio
    
## 开盘时运行函数
def market_open(context):
    log.info('函数运行时间(market_open):'+str(context.current_dt.time()))
    #order_target('000001.SZ', 100)
    if g.run_count % g.period == 0:
        print('rebalance: period',g.period)
        asset_alloc = get_trade_target(context)
        #print(asset_alloc)
        rebalance(context, asset_alloc)
    elif g.raise_rate > 0 and calc_asset_max_raise(context) > g.raise_rate:
    #elif g.raise_rate > 0 and calc_stock_max_raise(context) > g.raise_rate:    
            #基于资产的reblaance效果更好
            #增加 g.raise_rate > 0 是为了测试的时候
            #可以去掉涨幅触发rebalance的逻辑
            print('rebalance: >', g.raise_rate)
            asset_alloc = get_trade_target(context)
            #print(asset_alloc)
            rebalance(context, asset_alloc)
            
    g.run_count += 1

def stat_cur_asset(context):
    ret = {}
    print(f'context.portfolio.positions:{context.portfolio.positions}')
    for code in context.portfolio.positions:
        if code not in g.stock_map_asset:  continue
        asset = g.stock_map_asset[code]
        if asset in ret :
            ret[asset].append(code)
        else:
            ret[asset] = [code]
    return ret
    
def rebalance(context, asset_alloc):    
    g.rebalanced_stock_values = {}
    new_asset_values = {}
    cur_asset_codes = stat_cur_asset(context)
    print(cur_asset_codes)
    #total_value = context.portfolio.total_value
    total_value = g.capital
    #计算新的资产价值
    for asset in asset_alloc:
        new_asset_values[asset] =  total_value * asset_alloc[asset]['rate']
    print(new_asset_values)
    #调整每个资产
    for asset in asset_alloc:
        new_asset_codes = asset_alloc[asset]['codes']
        #切换掉的标的
        if asset in cur_asset_codes:
            sell_all_list = set(cur_asset_codes[asset]) - set(new_asset_codes)
        else:
            sell_all_list = ()
        for code in sell_all_list:
            order_target_value(code, 0)
        #reblance 当前资产，如果当前资产不存，持有现金
        num = len(new_asset_codes)
        if num > 0:
            per_value = new_asset_values[asset] / num
            for code in new_asset_codes:
                order_target_value(code, per_value)
                g.rebalanced_stock_values[code] = per_value
    g.rebalanced_asset_values = new_asset_values
    g.rebalanced_asset_alloc = asset_alloc

## 收盘后运行函数
def after_market_close(context):
    log.info(str('函数运行时间(after_market_close):'+str(context.current_dt.time())))
    #得到当天所有成交记录
    #trades = get_trades()
    #for _trade in trades.values():
    #    log.info('成交记录：'+str(_trade))
    #log.info('一天结束')
    #log.info('##############################################################')