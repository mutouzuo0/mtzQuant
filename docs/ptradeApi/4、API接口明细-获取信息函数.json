{
  "functions": [
    {
      "name": "get_trading_day",
      "description": "获取交易日期",
      "usage": "研究、回测、交易模块可用",
      "parameters": {
        "day": {
          "type": "int",
          "description": "表示天数，正的为数天后，负的为数天前，day取0表示获取当前交易日，如果当前日期为非交易日则返回上一交易日的日期。day默认取值为0，不建议获取交易所还未公布的交易日期"
        }
      },
      "returns": {
        "type": "datetime.date",
        "description": "交易日期"
      },
      "example": {
        "initialize": "g.security = ['600670.SS', '000001.SZ']; set_universe(g.security)",
        "handle_data": "next_trading_date = get_trading_day(1); log.info(next_trading_date); previous_trading_date = get_trading_day(-1); log.info(previous_trading_date)"
      },
      "notes": [
        "默认情况下，回测中当前时间为策略中调用该接口的回测日日期(context.blotter.current_dt)",
        "默认情况下，研究中当前时间为调用当天日期",
        "默认情况下，交易中当前时间为调用当天日期"
      ]
    },
    {
      "name": "get_all_trades_days",
      "description": "获取全部交易日期",
      "usage": "研究、回测、交易模块可用",
      "parameters": {
        "date": {
          "type": "str",
          "description": "如'2016-02-13'或'20160213'"
        }
      },
      "returns": {
        "type": "numpy.ndarray",
        "description": "包含所有交易日的数组"
      },
      "example": {
        "initialize": "all_trades_days = get_all_trades_days(); log.info(all_trades_days); all_trades_days_date = get_all_trades_days('20150312'); log.info(all_trades_days_date); g.security = ['600570.SS', '000001.SZ']; set_universe(g.security)",
        "handle_data": "pass"
      },
      "notes": [
        "默认情况下，回测中date为策略中调用该接口的回测日日期(context.blotter.current_dt)",
        "默认情况下，研究中date为调用当天日期",
        "默认情况下，交易中date为调用当天日期"
      ]
    },
    {
      "name": "get_trade_days",
      "description": "获取指定范围交易日期",
      "usage": "研究、回测、交易模块可用",
      "parameters": {
        "start_date": {
          "type": "str",
          "description": "开始日期，与count二选一，不可同时使用。如'2016-02-13'或'20160213',开始日期最早不超过1990年"
        },
        "end_date": {
          "type": "str",
          "description": "结束日期，如'2016-02-13'或'20160213'。如果输入的结束日期大于今年则至多返回截止到今年的数据"
        },
        "count": {
          "type": "int",
          "description": "数量，与start_date二选一，不可同时使用，必须大于0。表示获取end_date往前的count个交易日，包含end_date当天。count建议不大于3000，即返回数据的开始日期不早于1990年"
        }
      },
      "returns": {
        "type": "numpy.ndarray",
        "description": "包含指定范围交易日的数组"
      },
      "example": {
        "initialize": "trade_days = get_trade_days('2016-01-01', '2016-02-01'); log.info(trade_days); g.security = ['600570.SS', '000001.SZ']; set_universe(g.security)",
        "handle_data": "trading_days = get_trade_days(count=10); log.info(trading_days)"
      },
      "notes": [
        "默认情况下，回测中end_date为策略中调用该接口的回测日日期(context.blotter.current_dt)",
        "默认情况下，研究中end_date为调用当天日期",
        "默认情况下，交易中end_date为调用当天日期"
      ]
    },
    {
      "name": "get_trading_day_by_date",
      "description": "按日期获取指定交易日",
      "usage": "研究、回测、交易模块可用",
      "parameters": {
        "query_date": {
          "type": "str",
          "description": "查询日期,如'20230213'"
        },
        "day": {
          "type": "int",
          "description": "表示天数，正的为数天后，负的为数天前，day取0表示获取当前交易日，如果当前日期为非交易日则返回下一交易日的日期。day默认取值为0"
        }
      },
      "returns": {
        "type": "str",
        "description": "交易日日期"
      },
      "example": {
        "initialize": "g.security = ['600570.SS', '000001.SZ']; set_universe(g.security)",
        "handle_data": "current_date = context.blotter.current_dt.strftime('%Y-%m-%d'); trading_date = get_trading_day_by_date('20230501', 0); if trading_date == current_date: log.info('今日是5月1日之后首个交易日')"
      },
      "notes": [
        "query_date为必传入参",
        "该函数主要使用场景：按固定自然日调仓"
      ]
    },
    {
      "name": "get_market_list",
      "description": "获取市场列表",
      "usage": "研究、回测、交易模块可用",
      "parameters": {},
      "returns": {
        "type": "pandas.DataFrame",
        "description": "市场列表目录"
      },
      "example": {
        "initialize": "get_market_list()",
        "returns": "finance_mic, finance_name; 0, A, 美国证券交易所; 1, CBJC, 北京邮票; 2, CBOT, 芝加哥商品期货; 3, CCFX, 中国金融期货交易所; 4, CCGG, 中国国际文交所; 5, CCJC, 卡巴拉长江交易所; ...; 66, XFUND, 基金; 67, XHKG-SS, 沪港通; 68, XHKG-SZ, 深港通; 69, XSGE, 上海期货交易所; 70, XZCE, 郑州商品交易所; 71, YCME, 渝川玉石"
      },
      "notes": [
        "回测和交易中仅限before_trading_start和after_trading_end中使用"
      ]
    },
    {
      "name": "get_market_detail",
      "description": "获取市场详细信息",
      "usage": "研究、回测、交易模块可用",
      "parameters": {
        "finance_mic": {
          "type": "str",
          "description": "市场代码，相关市场编码参考get_market_list返回信息"
        }
      },
      "returns": {
        "type": "pandas.DataFrame",
        "description": "市场详细信息"
      },
      "example": {
        "initialize": "get_market_detail('XSHG')",
        "returns": "hq_type_code, prod_code, prod_name, trade_time_rule; MRI, 000001, 上证指数, 0; MRI, 000002, Ａ股指数, 0; MRI, 000003, Ｂ股指数, 0; MRI, 000004, 工业指数, 0; MRI, 000005, 商业指数, 0; MRI, 000006, 地产指数, 0; MRI, 000007, 公用指数, 0; MRI, 000008, 综合指数, 0"
      },
      "notes": [
        "回测和交易中仅限before_trading_start和after_trading_end中使用"
      ]
    },
    {
      "name": "get_history",
      "description": "获取历史行情",
      "usage": "研究、回测、交易模块可用",
      "parameters": {
        "count": {
          "type": "int",
          "description": "K线数量，大于0，返回指定数量的K线行情；必填参数"
        },
        "frequency": {
          "type": "str",
          "description": "K线周期，现有支持1分钟线(1m)、5分钟线(5m)、15分钟线(15m)、30分钟线(30m)、60分钟线(60m)、120分钟线(120m)、日线(1d)、周线(1w/weekly)、月线(mo/monthly)、季度线(1q/quarter)和年线(1y/yearly)频率的数据；选填参数，默认为'1d'"
        },
        "field": {
          "type": "list[str]/str",
          "description": "指明数据结果集中所支持输出的行情字段；选填参数，默认为['open','high','low','close','volume','money','price']；输出字段包括：open -- 开盘价，high -- 最高价，low --最低价，close -- 收盘价，volume -- 交易量，money -- 交易金额，price -- 最新价，is_open -- 是否开盘(仅日线返回)，preclose -- 昨收盘价(仅日线返回)，high_limit -- 涨停价(仅日线返回)，low_limit -- 跌停价(仅日线返回)，unlimited -- 判断查询日是否是无涨跌停限制(1:该日无涨跌停限制;0:该日不是无涨跌停限制)(仅日线返回)"
        },
        "security_list": {
          "type": "list[str]/str",
          "description": "要获取数据的股票列表；选填参数，None表示在上下文中的universe中选中的所有股票"
        },
        "fq": {
          "type": "str",
          "description": "数据复权选项，支持包括，pre-前复权，post-后复权，dypre-动态前复权，None-不复权；选填参数，默认为None"
        },
        "include": {
          "type": "bool",
          "description": "是否包含当前周期，True - 包含，False - 不包含；选填参数，默认为False"
        },
        "fill": {
          "type": "str",
          "description": "行情获取不到某一时刻的分钟数据时，是否用上一分钟的数据进行填充该时刻数据，'pre'-用上一分钟数据填充，'nan'-NaN进行填充(仅交易有效)；选填参数，默认为'nan'"
        },
        "is_dict": {
          "type": "bool",
          "description": "返回是否是字典(dict)格式{str: array()}，True - 是，False - 不是；选填参数，默认为False；返回为字典格式取数速度相对较快"
        }
      },
      "returns": {
        "type": "dict/pandas.DataFrame/pandas.Panel",
        "description": "历史行情数据，具体返回类型取决于参数设置",
        "details": {
          "is_dict: True": {
            "type": "dict",
            "description": "返回字典类型数据，键为股票代码，值为数组，包含日期时间、开盘价、最高价、最低价、收盘价、成交量、成交额、最新价等字段"
          },
          "is_dict: False": {
            "type": "pandas.DataFrame/pandas.Panel",
            "description": "返回DataFrame或Panel类型数据，具体取决于Python版本和参数设置",
            "details": {
              "python3.5、python3.11版本（单股票）": {
                "type": "pandas.DataFrame",
                "description": "行索引是datetime.datetime对象，列索引是行情字段。例如，获取单支股票的单个或多个字段时，返回的DataFrame对象，行索引是datetime.datetime对象，列索引是行情字段，为str类型。"
              },
              "python3.11版本（多股票）": {
                "type": "pandas.DataFrame",
                "description": "行索引是datetime.datetime对象，列索引是股票代码code和取的字段，为str类型。例如，获取多支股票的单个或多个字段时，返回的DataFrame对象，行索引是datetime.datetime对象，列索引是股票代码code和取的字段，为str类型。"
              },
              "python3.5版本（多股票，单字段）": {
                "type": "pandas.DataFrame",
                "description": "行索引是datetime.datetime对象，列索引是股票代码的编号，为str类型。例如，获取多支股票的单个字段时，返回的DataFrame对象，行索引是datetime.datetime对象，列索引是股票代码的编号，为str类型。"
              },
              "python3.5版本（多股票，多字段）": {
                "type": "pandas.Panel",
                "description": "items索引是行情字段，每个item是一个DataFrame，行索引是datetime.datetime对象，列索引是股票代码，为str类型。例如，获取多支股票的多个字段时，返回的Panel对象，items索引是行情字段，每个item是一个DataFrame，行索引是datetime.datetime对象，列索引是股票代码，为str类型。"
              }
            }
          }
        }
      },
      "example": {
        "initialize": "g.security = ['600570.SS', '000001.SZ']; set_universe(g.security)",
        "handle_data": [
          "his = get_history(5, '1d', 'close', security_list=g.security); log.info('股票池中全部股票过去5天的每日收盘价'); log.info(his)",
          "his_ss = his.query('code in [\"600570.SS\"]')['close']; log.info('获取600570(恒生电子)过去5天的每天收盘价'); log.info(his_ss)",
          "log.info('获取600570(恒生电子)昨天的收盘价'); log.info(his_ss[-1])",
          "log.info('获取600570(恒生电子)每一列的平均值'); log.info(his_ss.mean())",
          "his1 = get_history(10, '1m', 'volume'); log.info('获取股票池中全部股票的过去10分钟的成交量'); log.info(his1)",
          "his2 = get_history(5, '1d', 'close', security_list='600570.SS'); log.info('获取恒生电子的过去5天的每天的收盘价'); log.info(his2)",
          "his3 = get_history(5, '1d', 'close', security_list='600570.SS', fq='post'); log.info('获取恒生电子的过去5天的每天的后复权收盘价'); log.info(his3)",
          "his4 = get_history(5, '1w', 'close', security_list='600570.SS'); log.info('获取恒生电子的过去5周的每周的收盘价'); log.info(his4)",
          "dataframe_info = get_history(2, frequency='1d', field=['open','close'], security_list=g.security); open_df = dataframe_info[['code', 'open']]; log.info('获所有股票的取开盘价数据'); log.info(open_df); df = open_df.query('code in [\"600570.SS\"]')['open']; log.info('仅获取恒生电子的开盘价数据'); log.info(df)"
        ]
      },
      "notes": [
        "该接口只能获取2005年后的数据",
        "针对停牌场景，没有跳过停牌的日期，无论对单只股票还是多只股票进行调用，时间轴均为二级市场交易日日历，停牌时使用停牌前的数据填充，成交量为0，日K线可使用成交量为0的逻辑进行停牌日过滤",
        "证监会行业、聚源行业、概念板块、地域板块所对应标的的行情数据为非标准的交易所下发数据，是由数据源自行按照成分股分类规则进行计算的，存在与三方数据源不一致的情况。如用户需要在策略中使用，应自行评估该数据的合理性",
        "该接口与get_price接口不支持多线程同时调用，即在run_daily或run_interval等函数中不要与handle_data等框架模块同一时刻调用get_history或get_price接口，否则会偶现获取数据为空的现象"
      ]
    },

    {
      "name": "get_individual_entrust",
      "description": "获取当日逐笔委托行情数据",
      "usage": "在交易模块可用",
      "parameters": {
        "stocks": {
          "type": "list[str]",
          "description": "默认为当前股票池中代码列表"
        },
        "data_count": {
          "type": "int",
          "description": "数据条数，默认为50，最大为200"
        },
        "start_pos": {
          "type": "int",
          "description": "起始位置，默认为0"
        },
        "search_direction": {
          "type": "int",
          "description": "搜索方向(1向前，2向后)，默认为1"
        },
        "is_dict": {
          "type": "bool",
          "description": "返回类型，False-DataFrame/Panel，True-dict，默认为False"
        }
      },
      "returns": "dict类型数据，异常时返回None",
      "example": {
        "initialize": "g.security = '000001.SZ'; set_universe(g.security)",
        "handle_data": "entrust = get_individual_entrust(); log.info(entrust)"
      },
      "notes": "需开通level2行情才能获取数据；返回dict类型数据速度更快"
    },
    {
      "name": "get_individual_transaction",
      "description": "获取当日逐笔成交行情数据",
      "usage": "在交易模块可用",
      "parameters": {
        "stocks": {
          "type": "list[str]",
          "description": "默认为当前股票池中代码列表"
        },
        "data_count": {
          "type": "int",
          "description": "数据条数，默认为50，最大为200"
        },
        "start_pos": {
          "type": "int",
          "description": "起始位置，默认为0"
        },
        "search_direction": {
          "type": "int",
          "description": "搜索方向(1向前，2向后)，默认为1"
        },
        "is_dict": {
          "type": "bool",
          "description": "返回类型，False-DataFrame/Panel，True-dict，默认为False"
        }
      },
      "returns": "dict类型数据，异常时返回None",
      "example": {
        "initialize": "g.security = '000001.SZ'; set_universe(g.security)",
        "handle_data": "transaction = get_individual_transaction(); log.info(transaction)"
      },
      "notes": "需开通level2行情才能获取数据；返回dict类型数据速度更快"
    },
    {
      "name": "get_tick_direction",
      "description": "获取当日分时成交行情数据",
      "usage": "在交易模块可用",
      "parameters": {
        "symbols": {
          "type": "str/list[str]",
          "description": "单只标的代码或代码列表"
        },
        "query_date": {
          "type": "int",
          "description": "查询日期，默认为0，返回当日日期数据，格式为YYYYMMDD"
        },
        "start_pos": {
          "type": "int",
          "description": "起始位置，默认为0"
        },
        "search_direction": {
          "type": "int",
          "description": "搜索方向(1向前，2向后)，默认为1"
        },
        "data_count": {
          "type": "int",
          "description": "数据条数，默认为50，最大为200"
        },
        "is_dict": {
          "type": "bool",
          "description": "返回类型，False-OrderedDict，True-dict，默认为False"
        }
      },
      "returns": "dict或OrderedDict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "direction_data = get_tick_direction([g.security]); log.info(direction_data)"
      },
      "notes": "返回dict类型数据速度更快"
    },
    {
      "name": "get_sort_msg",
      "description": "获取板块、行业的快照信息",
      "usage": "在交易模块可用",
      "parameters": {
        "sort_type_grp": {
          "type": "str/list[str]",
          "description": "板块或行业的代码，暂时只支持XBHS.DY地域、XBHS.GN概念、XBHS.ZJHHY证监会行业、XBHS.ZS指数、XBHS.HY行业等"
        },
        "sort_field_name": {
          "type": "str",
          "description": "需要排序的字段，如preclose_px、open_px、last_px等"
        },
        "sort_type": {
          "type": "int",
          "description": "排序方式，默认降序(0:升序，1:降序)"
        },
        "data_count": {
          "type": "int",
          "description": "数据条数，默认为100，最大为10000"
        }
      },
      "returns": "list[dict]类型数据",
      "example": {
        "initialize": "g.security = '000001.SZ'; set_universe(g.security)",
        "handle_data": "sort_data = get_sort_msg(sort_type_grp='XBHS.DY', sort_field_name='preclose_px', sort_type=1, data_count=100); log.info(sort_data)"
      },
      "notes": "证监会行业、聚源行业等数据为非标准数据，可能存在与三方数据源不一致的情况"
    },
    {
      "name": "get_gear_price",
      "description": "获取指定代码的档位行情价格",
      "usage": "仅在交易模块可用",
      "parameters": {
        "sids": {
          "type": "str/list[str]",
          "description": "股票代码"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "gear_price = get_gear_price(g.security); log.info(gear_price)"
      },
      "notes": "获取实时行情快照失败时返回档位内容为空dict；若无L2行情时，委托笔数字段返回0"
    },
    {
      "name": "get_snapshot",
      "description": "获取实时行情快照",
      "usage": "仅在交易模块可用",
      "parameters": {
        "security": {
          "type": "str/list[str]",
          "description": "单只股票代码或者多只股票代码组成的列表"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "snapshot = get_snapshot(g.security); log.info(snapshot)"
      },
      "notes": "证监会行业、聚源行业等数据为非标准数据，可能存在与三方数据源不一致的情况"
    },
    {
      "name": "get_trend_data",
      "description": "获取集中竞价期间代码数据",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "date": {
          "type": "str",
          "description": "日期，格式为YYYYmmdd"
        },
        "stocks": {
          "type": "str/list[str]",
          "description": "股票代码"
        },
        "market": {
          "type": "str/list[str]",
          "description": "市场"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "trend_data = get_trend_data(stocks=g.security); log.info(trend_data)"
      },
      "notes": "不传参数时，默认返回当日XSHE,XSHG市场所有代码的数据；stocks和market不能同时入参"
    },
    {
      "name": "get_stock_name",
      "description": "获取证券名称",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "stocks": {
          "type": "str/list[str]",
          "description": "证券代码"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "stock_name = get_stock_name(g.security); log.info(stock_name)"
      },
      "notes": "交易场景下，默认每个交易日的09:07分~09:09之间完成当天数据的更新"
    },
    {
      "name": "get_stock_info",
      "description": "获取指定证券基础信息",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "stocks": {
          "type": "str/list[str]",
          "description": "证券代码"
        },
        "field": {
          "type": "str/list[str]",
          "description": "指明数据结果集中所支持输出字段，如stock_name、listed_date、de_listed_date等"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "stock_info = get_stock_info(g.security); log.info(stock_info)"
      },
      "notes": "field不做入参时默认只返回stock_name字段"
    },
    {
      "name": "get_stock_status",
      "description": "获取证券的ST、停牌、退市等属性",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "stocks": {
          "type": "str/list[str]",
          "description": "证券代码"
        },
        "query_type": {
          "type": "str",
          "description": "支持'ST'、'HALT'、'DELISTING'、'DELISTING_SORTING'等类型属性的查询，默认为'ST'"
        },
        "query_date": {
          "type": "str",
          "description": "查询日期，格式为YYYYmmdd，默认为None，表示当前日期"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "st_status = get_stock_status(g.security, 'ST'); log.info(st_status)"
      },
      "notes": "无"
    },
    {
      "name": "get_underlying_code",
      "description": "获取证券的关联代码",
      "usage": "在交易模块可用",
      "parameters": {
        "symbols": {
          "type": "str/list[str]",
          "description": "需要查询的代码"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '000001.SZ'; set_universe(g.security)",
        "handle_data": "underlying_code_info = get_underlying_code(g.security); log.info(underlying_code_info)"
      },
      "notes": "无"
    },
    {
      "name": "get_stock_exrights",
      "description": "获取证券除权除息信息",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "stock_code": {
          "type": "str",
          "description": "证券代码"
        },
        "date": {
          "type": "str/int/datetime.date",
          "description": "查询该日期的除权除息信息，默认获取该证券历史上所有除权除息信息"
        }
      },
      "returns": "pandas.DataFrame类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "stock_exrights = get_stock_exrights(g.security); log.info(stock_exrights)"
      },
      "notes": "无"
    },
    {
      "name": "get_stock_blocks",
      "description": "获取证券所属板块信息",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "stock_code": {
          "type": "str",
          "description": "证券代码"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "blocks = get_stock_blocks(g.security); log.info(blocks)"
      },
      "notes": "该函数获取的是当下的数据，回测中注意未来函数"
    },
    {
      "name": "get_index_stocks",
      "description": "获取一个指数在平台可交易的成分股列表",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "index_code": {
          "type": "str",
          "description": "指数代码"
        },
        "date": {
          "type": "str",
          "description": "日期，格式为YYYYMMDD，默认为当前日期"
        }
      },
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "stocks = get_index_stocks('000300.XBHS', '20160620'); set_universe(stocks); log.info(stocks)"
      },
      "notes": "在回测中，date不入参默认取当前回测周期所属历史日期；在研究和交易中，date不入参默认取当前日期"
    },
    {
      "name": "get_industry_stocks",
      "description": "获取一个行业的所有股票",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "industry_code": {
          "type": "str",
          "description": "行业编码，尾缀必须是.XBHS"
        }
      },
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "stocks = get_industry_stocks('A01000.XBHS'); set_universe(stocks); log.info(stocks)"
      },
      "notes": "该函数获取的是当下的数据，回测中注意未来函数"
    },
    {
      "name": "get_fundamentals",
      "description": "获取财务三大报表数据、日频估值数据、各项财务能力指标数据",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "security": {
          "type": "str/list[str]",
          "description": "一支股票代码或者多只股票代码组成的list"
        },
        "table": {
          "type": "str",
          "description": "财务数据表名，如valuation、balance_statement、income_statement等"
        },
        "fields": {
          "type": "str/list[str]",
          "description": "指明数据结果集中所需输出业务字段"
        },
        "date": {
          "type": "str/datetime.date",
          "description": "按日期查询模式，返回查询日期之前对应的财务数据"
        },
        "start_year": {
          "type": "str",
          "description": "查询开始年份，按年份查询模式，返回输入年份范围内对应的财务数据"
        },
        "end_year": {
          "type": "str",
          "description": "查询截止年份，按年份查询模式，返回输入年份范围内对应的财务数据"
        },
        "report_types": {
          "type": "str",
          "description": "财报类型，如'1'表示获取一季度财报，'2'表示获取半年报等"
        },
        "merge_type": {
          "type": "int",
          "description": "数据更新设置，获取原始发布或最新发布数据信息"
        },
        "is_dataframe": {
          "type": "bool",
          "description": "True-返回DataFrame格式；False-返回pandas.Panel格式，默认为False"
        }
      },
      "returns": "pandas.DataFrame或pandas.Panel类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "funda_data = get_fundamentals(g.security, 'balance_statement', fields='total_assets', start_year='2011', end_year='2020', report_types='1'); log.info(funda_data)"
      },
      "notes": "该接口为http在线获取，存在流量限制，每秒不得调用超过100次，单次最大调用量是500条数据"
    },
    {
      "name": "get_Ashares",
      "description": "获取指定日期沪深市场的所有A股代码列表",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "date": {
          "type": "str",
          "description": "日期，格式为YYYYmmdd，默认为当前日期"
        }
      },
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "ashares = get_Ashares(); log.info(ashares)"
      },
      "notes": "在回测中，date不入参默认取回测日期；在研究和交易中，date不入参默认取当前日期"
    },
    {
      "name": "get_etf_list",
      "description": "获取柜台返回的ETF代码列表",
      "usage": "仅支持PTrade客户端可用、仅在股票交易模块可用",
      "parameters": {},
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "etf_code_list = get_etf_list(); log.info(etf_code_list)"
      },
      "notes": "无"
    },
    {
      "name": "get_etf_info",
      "description": "获取单支或者多支ETF的信息",
      "usage": "仅支持PTrade客户端可用、仅在股票交易模块可用",
      "parameters": {
        "etf_code": {
          "type": "str/list[str]",
          "description": "单支ETF代码或者一个ETF代码的list"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "etf_info = get_etf_info('510020.SS'); log.info(etf_info)"
      },
      "notes": "无"
    },
    {
      "name": "get_etf_stock_list",
      "description": "获取目标ETF的成分券列表",
      "usage": "仅支持PTrade客户端可用、仅在股票交易模块可用",
      "parameters": {
        "etf_code": {
          "type": "str",
          "description": "单支ETF代码"
        }
      },
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "stock_list = get_etf_stock_list('510020.SS'); log.info(stock_list)"
      },
      "notes": "无"
    },
    {
      "name": "get_etf_stock_info",
      "description": "获取ETF成分券信息",
      "usage": "仅支持PTrade客户端可用、仅在股票交易模块可用",
      "parameters": {
        "etf_code": {
          "type": "str",
          "description": "单支ETF代码"
        },
        "security": {
          "type": "str/list[str]",
          "description": "单只股票代码或者一个由多只股票代码组成的列表"
        }
      },
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "stock_info = get_etf_stock_info('510050.SS', '600000.SS'); log.info(stock_info)"
      },
      "notes": "无"
    },
    {
      "name": "get_ipo_stocks",
      "description": "获取当日IPO申购标的信息",
      "usage": "仅支持Ptrade客户端可用、仅在股票交易模块可用",
      "parameters": {},
      "returns": "dict类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "ipo_stocks = get_ipo_stocks().get('可转债代码'); log.info(ipo_stocks)"
      },
      "notes": "无"
    },
    {
      "name": "get_cb_list",
      "description": "返回当前可转债市场的所有代码列表(包含停牌代码)",
      "usage": "仅在交易模块可用",
      "parameters": {},
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "cb_list = get_cb_list(); log.info(cb_list)"
      },
      "notes": "同一分钟内多次调用返回当前分钟首次查询的缓存数据"
    },
    {
      "name": "get_cb_info",
      "description": "获取可转债基础信息",
      "usage": "在研究、交易模块可用",
      "parameters": {},
      "returns": "pandas.DataFrame类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "df = get_cb_info(); log.info(df)"
      },
      "notes": "获取失败时返回空DataFrame；使用前需确认是否有此权限"
    },
    {
      "name": "get_reits_list",
      "description": "获取指定日期沪深市场的所有公募REITs基金代码列表",
      "usage": "在研究、回测、交易模块可用",
      "parameters": {
        "date": {
          "type": "str",
          "description": "日期，格式为YYYYmmdd，默认为当前日期"
        }
      },
      "returns": "list[str]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "reits_list = get_reits_list(); log.info(reits_list)"
      },
      "notes": "在回测中，date不入参默认取回测日期；在研究和交易中，date不入参默认取当前日期"
    },
    {
      "name": "get_position",
      "description": "获取某个标的持仓信息详情",
      "usage": "仅在回测、交易模块可用",
      "parameters": {
        "security": {
          "type": "str",
          "description": "标的代码"
        }
      },
      "returns": "Position对象",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "position = get_position(g.security); log.info(position)"
      },
      "notes": "无"
    },
    {
      "name": "get_positions",
      "description": "获取多个标的的持仓信息详情",
      "usage": "仅在回测、交易模块可用",
      "parameters": {
        "security": {
          "type": "str/list[str]",
          "description": "标的代码，可以是一个列表，不传时默认为获取所有持仓"
        }
      },
      "returns": "dict[str:Position]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "positions = get_positions(g.security); log.info(positions)"
      },
      "notes": "无"
    },
    {
      "name": "get_all_positions",
      "description": "获取当前账户的持仓信息详情",
      "usage": "仅在交易模块可用",
      "parameters": {},
      "returns": "list[dict]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "all_positions = get_all_positions(); log.info(all_positions)"
      },
      "notes": "返回的是缓存的账户定时同步持仓查询数据"
    },
    {
      "name": "get_trades_file",
      "description": "获取对账数据文件",
      "usage": "仅在回测模块可用",
      "parameters": {
        "save_path": {
          "type": "str",
          "description": "导出对账数据存储的路径，默认在notebook的根目录下"
        }
      },
      "returns": "str类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "after_trading_end": "data_path = get_trades_file('user_data/data'); log.info(data_path)"
      },
      "notes": "文件目录的命名需要遵守规则，长度不能超过256个字符，名称中不能包含特殊字符"
    },
    {
      "name": "convert_position_from_csv",
      "description": "从csv文件中获取设置底仓的参数列表",
      "usage": "仅在回测模块可用",
      "parameters": {
        "path": {
          "type": "str",
          "description": "csv文件对应路径及文件名"
        }
      },
      "returns": "list[dict]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "initialize": "poslist = convert_position_from_csv('Poslist.csv'); set_yesterday_position(poslist)"
      },
      "notes": "文件目录的命名需要遵守规则，长度不能超过256个字符，名称中不能包含特殊字符"
    },
    {
      "name": "get_user_name",
      "description": "获取登录终端的资金账号",
      "usage": "仅在回测、交易模块可用",
      "parameters": {
        "login_account": {
          "type": "bool",
          "description": "默认返回登录终端的资金账号，交易中传入False时返回当前策略绑定账号"
        }
      },
      "returns": "str类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "current_id = get_user_name(False); log.info(current_id)"
      },
      "notes": "回测中无论是否传入login_account参数均返回登录终端的资金账号"
    },
    {
      "name": "get_deliver",
      "description": "获取账户历史交割单信息",
      "usage": "仅在交易模块可用",
      "parameters": {
        "start_date": {
          "type": "str",
          "description": "开始日期，格式为YYYYmmdd"
        },
        "end_date": {
          "type": "str",
          "description": "结束日期，格式为YYYYmmdd"
        }
      },
      "returns": "list[dict]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "deliver_info = get_deliver('20210101', '20211117'); log.info(deliver_info)"
      },
      "notes": "仅支持查询上一个交易日(包含)之前的交割单信息；返回的是柜台原数据"
    },
    {
      "name": "get_fundjour",
      "description": "获取账户历史资金流水信息",
      "usage": "仅在交易模块可用",
      "parameters": {
        "start_date": {
          "type": "str",
          "description": "开始日期，格式为YYYYmmdd"
        },
        "end_date": {
          "type": "str",
          "description": "结束日期，格式为YYYYmmdd"
        }
      },
      "returns": "list[dict]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "fundjour_info = get_fundjour('20210101', '20211117'); log.info(fundjour_info)"
      },
      "notes": "仅支持查询上一个交易日(包含)之前的资金流水信息；返回的是柜台原数据"
    },
    {
      "name": "get_research_path",
      "description": "获取研究界面根目录路径",
      "usage": "在回测、交易模块可用",
      "parameters": {},
      "returns": "str类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "research_path = get_research_path(); log.info(research_path)"
      },
      "notes": "无"
    },
    {
      "name": "get_trade_name",
      "description": "获取当前交易的名称",
      "usage": "仅在交易模块可用",
      "parameters": {},
      "returns": "str类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "handle_data": "trade_name = get_trade_name(); log.info(trade_name)"
      },
      "notes": "无"
    },
    {
      "name": "get_lucky_info",
      "description": "获取指定时间范围内的中签信息",
      "usage": "仅在交易模块可用",
      "parameters": {
        "start_date": {
          "type": "str",
          "description": "开始日期，格式为YYYYmmdd"
        },
        "end_date": {
          "type": "str",
          "description": "结束日期，格式为YYYYmmdd"
        }
      },
      "returns": "list[dict]类型数据",
      "example": {
        "initialize": "g.security = '600570.SS'; set_universe(g.security)",
        "before_trading_start": "lucky_info = get_lucky_info('20220928', '20220929'); log.info(lucky_info)"
      },
      "notes": "同一分钟内多次调用返回当前分钟首次查询的缓存数据"
    }
  ]
}