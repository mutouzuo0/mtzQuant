{
    "对象": [
        {
            "名称": "g - 全局对象",
            "使用场景": "该对象仅支持回测、交易模块。",
            "对象说明": "全局对象g，用于存储用户的各类可被不同函数(包括自定义函数)调用的全局数据,如：g.security = None #股票池",
            "注意事项": "无",
            "示例": "def initialize(context):\n    g.security = \"600570.SS\"\n    g.count = 1\n    g.flag = 0\n    set_universe(g.security)\n\ndef handle_data(context, data):\n    log.info(g.security)\n    log.info(g.count)\n    log.info(g.flag)"
        },
        {
            "名称": "Context - 上下文对象",
            "使用场景": "该对象仅支持回测、交易模块。",
            "对象说明": "类型为业务上下文对象",
            "注意事项": "对象内的portfolio数据更新周期详见Portfolio对象对象注意事项说明。",
            "内容": {
                "capital_base": "起始资金",
                "previous_date": "前一个交易日",
                "sim_params": {
                    "capital_base": "起始资金",
                    "data_frequency": "数据频率"
                },
                "portfolio": "账户信息，可参考Portfolio对象",
                "initialized": "是否执行初始化",
                "slippage": {
                    "volume_limit": "成交限量",
                    "price_impact": "价格影响力"
                },
                "commission": {
                    "tax": "印花税费率",
                    "cost": "佣金费率",
                    "min_trade_cost": "最小佣金"
                },
                "blotter": {
                    "current_dt": "当前单位时间的开始时间，datetime.datetime对象(北京时间)"
                },
                "recorded_vars": "收益曲线值"
            },
            "示例": "def initialize(context):\n    g.security = ['600570.SS', '000001.SZ']\n    set_universe(g.security)\n\ndef handle_data(context, data):\n    # 获得当前回测相关时间\n    pre_date = context.previous_date\n    log.info(pre_date)\n    year = context.blotter.current_dt.year\n    log.info(year)\n    month = context.blotter.current_dt.month\n    log.info(month)\n    day = context.blotter.current_dt.day\n    log.info(day)\n    hour = context.blotter.current_dt.hour\n    log.info(hour)\n    minute = context.blotter.current_dt.minute\n    log.info(minute)\n    second = context.blotter.current_dt.second\n    log.info(second)\n    # 获取\"年-月-日\"格式\n    date = context.blotter.current_dt.strftime(\"%Y-%m-%d\")\n    log.info(date)\n    # 获取周几\n    weekday = context.blotter.current_dt.isoweekday()\n    log.info(weekday)"
        },
        {
            "名称": "BarData - K线数据对象",
            "使用场景": "该对象仅支持回测、交易模块。",
            "对象说明": "一个单位时间内代码的K线数据，是一个类对象。",
            "注意事项": "preclose、high_limit、low_limit、unlimited在分钟频率中均填充为0.0。当前周期内首次调用会在线获取该代码K线数据，当前周期重复调用时将会返回首次调用缓存的该代码K线数据。",
            "基本属性": [
                "symbol 标的代码",
                "name 代码名称",
                "dt 当前周期时间",
                "is_open 停牌标志，0-停牌，1-非停牌",
                "open 当前周期开盘价",
                "close 当前周期收盘价",
                "price 当前周期最新价",
                "low 当前周期最低价",
                "high 当前周期最高价",
                "volume 当前周期成交量",
                "money 当前周期成交额",
                "preclose 昨收盘价(仅日线返回)",
                "high_limit 涨停价(仅日线返回)",
                "low_limit 跌停价(仅日线返回)",
                "unlimited 是否无涨跌停限制(仅日线返回)",
                "datetime 当前周期时间"
            ],
            "示例": "def initialize(context):\n    g.security = \"600570.SS\"\n    set_universe(g.security)\n\n\ndef before_trading_start(context, data):\n    g.flag = False\n\n\ndef handle_data(context, data):\n    if not g.flag:\n        # 打印代码BarData对象\n        log.info(data[g.security])\n        # 打印标的代码\n        log.info(data[g.security].symbol)\n        # 打印代码名称\n        log.info(data[g.security].name)\n        # 打印当前周期时间\n        log.info(data[g.security].dt)\n        # 打印当前周期是否开盘\n        log.info(data[g.security].is_open)\n        # 打印当前周期开盘价\n        log.info(data[g.security].open)\n        # 打印当前周期收盘价\n        log.info(data[g.security].close)\n        # 打印当前周期最新价\n        log.info(data[g.security].price)\n        # 打印当前周期最低价\n        log.info(data[g.security].low)\n        # 打印当前周期最高价\n        log.info(data[g.security].high)\n        # 打印当前周期成交量\n        log.info(data[g.security].volume)\n        # 打印当前周期成交额\n        log.info(data[g.security].money)\n        # 打印昨收盘价(仅日线返回)\n        log.info(data[g.security].preclose)\n        # 打印涨停价(仅日线返回)\n        log.info(data[g.security].high_limit)\n        # 打印跌停价(仅日线返回)\n        log.info(data[g.security].low_limit)\n        # 打印是否无涨跌停限制(仅日线返回)\n        log.info(data[g.security].unlimited)\n        # 打印当前周期时间\n        log.info(data[g.security].datetime)\n        g.flag = True"
        },
        {
            "名称": "Portfolio - 资产对象",
            "使用场景": "该对象仅支持回测、交易模块。",
            "对象说明": "对象数据包含账户当前的资金，标的信息，即所有标的操作仓位的信息汇总",
            "注意事项": "交易中对象内的数据更新周期默认为6s(具体配置需咨询所在券商)，即上一次账户资金、委托、持仓查询并更新到对象中后，间隔6s发起下一次查询。数据更新时间范围为before_trading_start-after_trading_end。",
            "内容": {
                "股票账户返回": {
                    "cash": "当前可用资金(不包含冻结资金)",
                    "positions": "当前持有的标的(包含不可卖出的标的)，dict类型，key是标的代码，value是Position对象",
                    "portfolio_value": "当前持有的标的和现金的总价值",
                    "positions_value": "持仓价值",
                    "capital_used": "已使用的现金",
                    "returns": "当前的收益比例, 相对于初始资金",
                    "pnl": "当前账户总资产-初始账户总资产",
                    "start_date": "开始时间"
                },
                "期货账户返回": {
                    "cash": "当前可用资金(不包含冻结资金)",
                    "positions": "当前持有的标的(包含不可卖出的标的)，dict类型，key是标的代码，value是Position对象",
                    "portfolio_value": "当前持有的保证金和现金的总价值",
                    "positions_value": "持仓价值",
                    "returns": "当前的收益比例, 相对于初始资金",
                    "pnl": "当前账户总资产-初始账户总资产",
                    "start_date": "开始时间",
                    "margin": "保证金"
                }
            },
            "示例": "def initialize(context):\n    g.security = \"600570.SS\"\n    set_universe([g.security])\n\ndef handle_data(context, data):\n    log.info(context.portfolio.portfolio_value)"
        },
        {
            "名称": "Position - 持仓对象",
            "使用场景": "该对象仅支持回测、交易模块。",
            "对象说明": "持有的某个标的的信息。",
            "注意事项": "期货业务持仓把单个合约的持仓分为了多头仓(long)、空头仓(short)。交易中对象内的数据更新周期默认为6s(具体配置需咨询所在券商)，即上一次账户资金、委托、持仓查询并更新到对象中后，间隔6s发起下一次查询。数据更新时间范围为before_trading_start-after_trading_end。交易场景下，持仓信息是每6秒与柜台同步后更新的，update_time字段记录了最近的更新时间，格式为：\"%Y-%m-%d %H:%M:%S\"。回测场景返回None。",
            "内容": {
                "股票账户返回": {
                    "sid": "标的代码",
                    "enable_amount": "可用数量",
                    "amount": "总持仓数量",
                    "last_sale_price": "最新价格",
                    "cost_basis": "持仓成本价格",
                    "today_amount": "今日开仓数量",
                    "business_type": "持仓类型",
                    "update_time": "持仓更新时间"
                },
                "期货账户返回": {
                    "sid": "标的代码",
                    "short_enable_amount": "空头仓可用数量",
                    "long_enable_amount": "多头仓可用数量",
                    "today_short_amount": "空头仓今仓数量",
                    "today_long_amount": "多头仓今仓数量",
                    "long_cost_basis": "多头仓持仓成本",
                    "short_cost_basis": "空头仓持仓成本",
                    "long_amount": "多头仓总持仓量",
                    "short_amount": "空头仓总持仓量",
                    "long_pnl": "多头仓浮动盈亏",
                    "short_pnl": "空头仓浮动盈亏",
                    "amount": "总持仓数量",
                    "enable_amount": "可用数量",
                    "last_sale_price": "最新价格",
                    "business_type": "持仓类型",
                    "delivery_date": "交割日，期货使用",
                    "margin": "持仓保证金",
                    "contract_multiplier": "合约乘数",
                    "update_time": "持仓更新时间"
                }
            },
            "示例": "def initialize(context):\n    g.security = '600570.SS'\n    set_universe(g.security)\n\ndef handle_data(context, data):\n    order(g.security,1000)\n    position = get_position(g.security)\n    log.info(position)"
        },
        {
            "名称": "Order - 委托对象",
            "使用场景": "该对象仅支持回测、交易模块。",
            "对象说明": "买卖订单信息",
            "注意事项": "回测中entrust_no、cancel_entrust_no字段值为None。交易中对象内的数据更新分为两种同时进行：1.数据周期默认为6s(具体配置需咨询所在券商)，即上一次账户资金、委托、持仓查询并更新到对象中后，间隔6s发起下一次查询。数据更新时间范围为before_trading_start-after_trading_end。2.后台接收到主推数据时会更新对象内成交数量、委托状态、持仓成本价等信息。交易中对原委托进行撤单时，cancel_entrust_no字段值填充撤单委托编号。交易中期货(对接UFT柜台)对原委托进行撤单时，撤单委托编号等于原委托编号。",
            "内容": {
                "股票账户返回": {
                    "id": "订单号",
                    "dt": "订单产生时间，datetime.datetime类型",
                    "limit": "指定价格",
                    "symbol": "标的代码(备注：标的代码尾缀为四位，上证为XSHG，深圳为XSHE，如需对应到代码请做代码尾缀兼容)",
                    "amount": "下单数量，买入是正数，卖出是负数",
                    "created": "订单生成时间，datetime.datetime类型",
                    "filled": "成交数量，买入时为正数，卖出时为负数",
                    "entrust_no": "委托编号",
                    "cancel_entrust_no": "撤单委托编号",
                    "priceGear": "盘口档位",
                    "status": "委托状态"
                },
                "期货账户返回": {
                    "id": "订单号",
                    "dt": "订单产生时间，datetime.datetime类型",
                    "limit": "指定价格",
                    "symbol": "标的代码",
                    "amount": "下单数量，正数",
                    "created": "订单生成时间，datetime.datetime类型",
                    "side": "多空仓标志(str类型，long：多头仓，short：空头仓)",
                    "action": "开平仓方向(str类型，open：开仓，close：平仓)",
                    "entrust_direction": "买卖方向(str类型，buy：买入，sell：卖出)",
                    "filled": "成交数量，正数",
                    "entrust_no": "委托编号",
                    "cancel_entrust_no": "撤单委托编号",
                    "priceGear": "盘口档位",
                    "status": "委托状态"
                }
            },
            "示例": "def initialize(context):\n    g.security = \"600570.SS\"\n    set_universe(g.security)\n\ndef handle_data(context, data):\n    order(g.security, 100)\n    log.info(get_orders())"
        }
    ],
    "数据字典": {
        "status -- 委托状态": {
            "0": "未报",
            "1": "待报",
            "2": "已报",
            "3": "已报待撤",
            "4": "部成待撤",
            "5": "部撤",
            "6": "已撤",
            "7": "部成",
            "8": "已成",
            "9": "废单",
            "+": "已受理",
            "-": "已确认",
            "C": "正报",
            "V": "已确认"
        },
        "entrust_type -- 委托类别": {
            "0": "委托",
            "2": "撤单",
            "4": "确认",
            "6": "信用融资",
            "7": "信用融券",
            "9": "信用交易"
        },
        "entrust_prop -- 委托属性": {
            "0": "买卖",
            "1": "配股",
            "3": "申购",
            "4": "回购",
            "7": "转股",
            "9": "股息",
            "N": "ETF申赎",
            "Q": "对手方最优价格",
            "R": "最优五档即时成交剩余转限价",
            "S": "本方最优价格",
            "T": "即时成交剩余撤销",
            "U": "最优五档即时成交剩余撤销",
            "V": "全成交或撤销",
            "b": "定价委托",
            "c": "确认委托",
            "d": "限价委托",
            "HKN": "港股订单申报",
            "HKO": "零股订单申报"
        },
        "business_direction -- 成交方向": {
            "0": "卖",
            "1": "买",
            "2": "借入",
            "3": "出借"
        },
        "trans_kind -- 委托类型": {
            "深圳市场": {
                "1": "市价委托",
                "2": "限价委托",
                "3": "本方最优"
            },
            "上海市场": {
                "4": "增加订单",
                "5": "删除订单"
            }
        },
        "trade_status -- 交易状态": {
            "START": "市场启动(初始化之后，集合竞价前)",
            "PRETR": "盘前",
            "OCALL": "开始集合竞价",
            "TRADE": "交易(连续撮合)",
            "HALT": "暂停交易",
            "SUSP": "停盘",
            "BREAK": "休市",
            "POSTR": "盘后",
            "ENDTR": "交易结束",
            "STOPT": "长期停盘，停盘n天，n>=1",
            "DELISTED": "退市",
            "POSMT": "盘后交易",
            "PCALL": "盘后集合竞价",
            "INIT": "盘后固定价格启动前",
            "ENDPT": "盘后固定价格闭市阶段",
            "POSSP ": "盘后固定价格停牌"
        },
        "trans_flag -- 成交标记": {
            "0": "普通成交",
            "1": "撤单成交"
        },
        "trans_identify_am -- 盘后逐笔成交序号标识": {
            "0": "盘中",
            "1": "盘后"
        },
        "entrust_bs -- 委托方向": {
            "1": "买",
            "2": "卖"
        },
        "cash_replace_flag -- 现金替代标志": {
            "0": "禁止替代",
            "1": "允许替代",
            "2": "必须替代",
            "3": "非沪市退补现金替代",
            "4": "非沪市必须现金替代",
            "5": "非沪深退补现金替代",
            "6": "非沪深必须现金替代"
        },
        "exchange_type/futu_exch_type -- 交易类别": {
            "0": "资金",
            "1": "上海",
            "2": "深圳",
            "9": "特转A",
            "A": "特转B",
            "D": "沪Ｂ",
            "G": "沪港通",
            "H": "深Ｂ",
            "Q": "青岛产权",
            "S": "深港通",
            "T": "场外OTC市场",
            "U": "转融通",
            "J": "金华基金",
            "K": "香港市场",
            "X": "固定收益",
            "F1": "郑州交易所",
            "F2": "大连交易所",
            "F3": "上海交易所",
            "F4": "金融交易所",
            "F5": "能源交易所",
            "Z1": "业务受理",
            "R": "H股全流通"
        },
        "delist_flag -- 退市标志": {
            "0": "正常",
            "1": "退市"
        },
        "hedge_type -- 投机/套保类型": {
            "0": "投机",
            "1": "套保",
            "2": "套利",
            "3": "做市商",
            "4": "备兑",
            "0": "权利方",
            "1": "义务方",
            "2": "备兑方",
            "C": "看涨期权",
            "P": "看跌期权"
        },
        "market_type -- 市价委托类型": {
            "0": "对手方最优价格",
            "1": "最优五档即时成交剩余转限价",
            "2": "本方最优价格",
            "3": "即时成交剩余撤销",
            "4": "最优五档即时成交剩余撤销",
            "5": "全额成交或撤单"
        },
        "submarket_type -- 申购代码所属市场": {
            "0": "上证普通代码",
            "1": "上证科创板代码",
            "2": "深证普通代码",
            "3": "深证创业板代码",
            "4": "可转债代码"
        },
        "cash_group -- 两融头寸性质": {
            "0": "核心头寸",
            "1": "普通业务头寸",
            "2": "专项业务头寸"
        },
        "compact_type -- 合约类别": {
            "0": "融资",
            "1": "融券",
            "2": "其他负债"
        },
        "compact_status -- 合约状态": {
            "0": "开仓未归还",
            "1": "部分归还",
            "2": "合约已过期",
            "3": "客户自行归还",
            "4": "手工了结",
            "5": "未形成负债"
        },
        "underlying_type -- 关联类型": {
            "0": "A股",
            "1": "B股",
            "2": "H股",
            "3": "期货",
            "4": "期权",
            "5": "港股-认购",
            "6": "港股-认沽",
            "7": "港股-牛证",
            "8": "港股-熊证",
            "9": "港股-界内证",
            "10": "英股关联关系",
            "11": "美股关联代码",
            "12": "股本认股权证认购证",
            "13": "股本认股权证认沽证",
            "14": "可转债关联关系正向-正股关联可转债",
            "15": "可转债关联关系反向-可转债关联正股"
        },
        "real_type -- 成交类型": {
            "0": "买卖",
            "1": "查询",
            "2": "撤单",
            "6": "融资",
            "7": "融券",
            "8": "平仓",
            "9": "信用",
            "G": "期权强制平仓"
        },
        "real_status -- 成交状态": {
            "0": "成交",
            "2": "废单",
            "4": "确认"
        }
    }
}