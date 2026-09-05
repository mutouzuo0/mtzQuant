# 聚宽（JoinQuant）兼容清单

> M2-N5 交付（设计 4.6/4.9）。目标：**原生聚宽策略零改动加载**（用户体验目标），
> 兼容性以**语义等级 S0–S4** 承诺，不承诺与官方回测结果一致。
> 验收纪律（4.9）：黄金用例逐项通过 = **S3 级**；禁止以「与官方回测同量级/收益接近」验收。
>
> 语义依据：`.zcode/doc/JoinQuantAPI.md`（官方 API 文档 PDF 的 Markdown 版，2026-09 生成）。
> 2026-09-05 更新：小市值类原版策略兼容面（FixedSlippage/PerTrade/OrderStatus/单参
> before_trading_start/day_open/paused 字段/query.limit/全市场基本面池/池外下单扩池）。

## 语义等级（4.9）

| 等级 | 承诺 | 验证手段 |
|------|------|---------|
| S0 | 可加载，生命周期可启动 | 冒烟测试 |
| S1 | API 签名兼容（参数/返回类型不报错） | 单元测试 |
| S2 | 返回数据结构与字段语义与目标平台一致 | 对照样例数据 |
| S3 | **时间可见性、调度、订单语义**与目标平台一致（无未来数据、时点正确、成交时序正确） | 黄金用例断言（g01-g13 聚宽版全绿） |
| S4 | 黄金用例上与目标平台（官方回测）结果误差受控 | 双平台对照运行 |

## API 兼容表（L0/L1/L2）

### 生命周期钩子（S3）

| API | 级别 | 说明 |
|-----|------|------|
| `initialize(context)` | L0 | 初始化入口；`set_universe/run_daily/set_order_cost` 等配置族仅限此内调用 |
| `handle_data(context, data)` | L0 | 主驱动（日线 15:00）；`data[security]` 快照含 close/volume/paused/day |
| `before_trading_start(context)` | L0 | 盘前；**官方单参签名**（按 `inspect` 兼容存量双参 `(context, data)` 写法） |
| `after_trading_end(context)` | L0 | 盘后（仅 context，聚宽官方签名） |
| `process_initialize(context)` | L2 | **跳过并记降级**（回测语义占位）；初始化逻辑请放 `initialize` |

### 下单族（L0，K5 归一，买正卖负）

| API | 级别 | 说明 |
|-----|------|------|
| `order(security, amount)` | L0 | 按股数（amount>0 买 / <0 卖）；返回 Order 模拟回执（官方口径：`amount/filled` 恒正、`status` 为 OrderStatus、属性访问） |
| `order_target(security, target_amount)` | L0 | 目标股数 |
| `order_value(security, value)` | L0 | 按金额 |
| `order_target_value(security, target_value)` | L0 | 目标市值（整手/零差忽略/方向，g08） |
| `order_market(security, amount)` | L0 | 市价单（日线撮合语义同 order） |
| `order_shares(security, amount)` | L2 | 聚宽 L2，尽力实现（与 order 同义） |
| `get_trades()` | L0 | 成交列表（回测内可见成交） |
| `OrderStatus`（枚举） | L0 | 官方枚举注入（`open/filled=部分成交/canceled/rejected/held=全部成交/new/pending_cancel`）；回执 `status` 动态映射引擎状态 |

### 数据族（S2~S3）

| API | 级别 | 说明 |
|-----|------|------|
| `history(count, unit, field, security_list, df, skip_paused, include_now, fq)` | L0 | 批量 pivot 宽表；单标的返回 DataFrame；**`include_now=False`（默认）不含当前 bar**（官方语义：即使 15:00 也如此，2026-09-05 修正）；字段含 `paused/money`（别名映射） |
| `attribute_history(security, count, unit, fields, skip_paused, df, include_today, fq)` | L0 | 单标的历史；**`include_today=False`（默认）不含当前 bar**（官方 JoinQuantAPI.md: 取日线不含当天, 即使 15:00/after_close 也如此——2026-09-05 修正: 此前版本在 15:00 折叠执行时会吸入当日 bar, 选股/回看语义错位）；字段含 `paused/money`；`skip_paused=True`（官方默认）剔除停牌行并保 count 行数；返回帧支持旧式 `df['close'][-1]` 位置访问；`df=False` 返回 `{field: ndarray}` |
| `get_price(security, start_date, end_date, frequency, fields, count, panel, fill_paused, ...)` | L0 | 区间/根数行情（PIT: as_of=end_date）；`panel=False` 长表含 `code` 列；`fill_paused` 宽容接收（本地停牌行即原始数据，语义等价，记降级）；未知参数宽容忽略（记降级） |
| `get_current_data(security_list)` | L0 | `{security: CurrentData 快照}`；官方**按需惰性取数**（`[security]` 时才获取，含 universe 外标的的真实 bar）；字段含 `day_open/last_price/high_limit/low_limit/paused/is_st/name` |
| `data[security]` | L0 | handle_data 内当日快照对象（close/volume/paused/day/day_open） |
| `get_trade_days(start_date, end_date)` | L0 | 交易日历区间 |
| `get_all_securities(types, date)` | L0 | 主数据 PIT 过滤（list_date<=date 且未退市；master 支撑，tushare stock_basic L+D） |
| `get_security_info(code)` | L0 | 主数据查询（`display_name/name/start_date/end_date/type`；`name` 官方为拼音缩写，本地以中文名代用——近似） |
| `get_index_stocks(index_symbol)` | L1 | 读本地成分快照（D6）；缺失 → 结构化报错，**不返回当前成分**（防幸存者偏差） |
| `get_extras(info, security_list, ...)` | L2 | **未实现**，结构化报错 + 替代建议（停牌标记用 `get_price(fields=['paused'])`） |
| `query(...).filter().order_by().limit()` | L0 | 基本面 DSL；`limit` 在排序后截断（官方语义）；`get_fundamentals` 不带 `code.in_()` 过滤时查询**主数据 PIT 全市场股票**（官方全市场语义） |

### 配置族（S2~S3）

| API | 级别 | 说明 |
|-----|------|------|
| `set_universe(securities)` | L0 | 动态 universe（g09：切换后新标的可查，懒加载）；**池外标的下单时自动扩池**（聚宽"任意标的可交易"语义，记降级） |
| `set_order_cost(open_tax, close_tax, open_commission, close_commission, min_commission)` | L2 | **买卖侧统一**佣金/印花税（4.6 已知近似） |
| `set_commission(PerTrade(buy_cost, sell_cost, min_cost))` | L0 | 官方已废弃但老策略大量在用；`sell_cost` 含印花税 → 拆分映射 `commission_rate=buy_cost + stamp_tax_rate=sell_cost−buy_cost`（如官方默认 0.0003/0.0013/5 = 买万3、卖万3+千1，精确等价） |
| `set_slippage(FixedSlippage(x) / PriceRelatedSlippage(x))` | L0 | 官方**总价差**口径：买卖各加减一半（`FixedSlippage(0.02)` → 成交价±0.01）→ 引擎 `fixed=x/2` / `ratio=x/2`；裸浮点仍按单边比例（mtzQuant 存量用法） |
| `set_benchmark(code)` | L2 | **运行时设置不生效**（基准取任务配置，记降级） |
| `set_option(key, value)` | L2 | `auto_handle_position/use_real_price/order_volume_ratio/avoid_future_data` 可映射（记降级）；其余结构化报错 |

### 调度族（S3）

| API | 级别 | 说明 |
|-----|------|------|
| `run_daily(func, time)` | L0 | 无 context 参数（区别于 PTrade）；日线回测 time 折叠 15:00，盘中时刻记降级 |
| `run_weekly(func, weekday, time)` | L2 | 折叠到每周首交易日（记降级） |
| `run_monthly(func, monthday, time)` | L2 | 折叠到每月首交易日（记降级） |

## 已知近似清单（4.6 / 4.9 登记）

1. **日线回测时刻折叠**：`run_daily` 盘中时刻（9:30~14:50）一律折叠 15:00 执行并记
   `semantic_degradation`（`every_bar/open/close/15:00` 不记）；同 bar 内按注册顺序执行。
2. **周/月调度折叠**：`run_weekly/run_monthly` 折叠到周/月首交易日（简化规则）。
3. **费用统一**：`set_order_cost` 买卖侧统一费率（不区分 open/close 侧）。
4. **`set_benchmark` 不生效**：基准始终取任务配置。
5. **`process_initialize` 跳过**：仅记降级不执行。
6. **成分股依赖本地快照**（`get_index_stocks`）：缺失报错，不返回当前成分（防幸存者偏差）。
7. **`get_all_securities`/`get_security_info` 依主数据**：名称/类型来自 tushare master 当前
   值（历史 ST 状态无法还原）；`get_security_info().name` 官方为拼音缩写，本地以中文名代用。
8. **`context.portfolio.market_cap`**：日线近似 = total_value（无盘口股本）。
9. **`context.portfolio.daily_returns`**：恒 0（日收益由引擎指标给出）。
10. **涨跌停价近似**：本地 bar 无 limit 列，由 pre_close×板块因子（主板 10%/创业+科创 20%/
    北交所 30%/ST 5%）计算（`data[security]` 与 `get_price` 的 `high_limit/low_limit`）。
11. **`get_extras` 未实现**：结构化报错 + 替代建议。
12. **`set_option` 四键可映射**（auto_handle_position/use_real_price/order_volume_ratio/
    avoid_future_data）：未知键结构化报错。
13. **北交所 `.BJ`**：无平台别名，原样透传（登记为已知近似）。
14. **订单模拟回执**：官方 Order 字段子集 + 属性访问（`amount/filled` 恒正、`status` 动态
    映射引擎状态）；**未绑定引擎订单时按聚宽"下单即成交"可见口径乐观返回** `held/
    filled=amount`（引擎当日收盘确定性撮合；拒单/缩量在 `sync_orders` 绑定后诚实反映）。
15. **全市场基本面池**：`get_fundamentals` 无 `code.in_()` 过滤时按主数据 PIT 存续股票
    查询（官方全市场语义）；数据覆盖以本地 daily_basic 落盘为准（缺数据代码无值被过滤）。
16. **`PriceRelatedSlippage` 半价差修正**：2026-09-05 起按官方总价差口径映射（此前版本
    直接把 value 当单边比例，滑点放大 2 倍——老 run 结果复现需注意此差异）。
17. **等分现金末单**：受理端按**本金**冻结（2026-09-05 起），费用余量溢出由成交侧缩量
    部分成交兜底（对齐聚宽"按可用资金调整下单量"）；本金超现金仍拒单（g06 语义不变）。
18. **`attribute_history/history` 不含当日语义**：2026-09-05 起按官方 JoinQuantAPI.md
    语义对齐（"取日线不含当天, 即使 15:00/after_close 中也如此"）——`include_today=False`
    （默认）/`include_now=False`（默认）时把可见窗口拨到前一交易日，避免 15:00 折叠执行时
    吸入当日 bar（此前版本会误用当日 close 作选股条件，引入未来数据）。显式 `include_today=True`
    /`include_now=True` 仍按引擎"盘后当日可见"语义（对齐聚宽盘后场景）。

## 黄金用例覆盖（S3，tests/golden/platform_joinquant/）

| 用例 | 语义 | 聚宽验证点 |
|------|------|-----------|
| g01 | 空仓 | 生命周期启动、六要素全零 |
| g02 | 单次买卖 | order_target_value 整手/费用/NAV oracle |
| g03 | T+1 | 当日卖拒（t_plus_sell_unavailable） |
| g04 | 一字板 | 涨停单过期 + 降级 |
| g05 | 停牌 | stale 估值 + 挂单过期 |
| g06 | 现金不足 | insufficient_cash 拒单 |
| g07 | 费用 | 佣金下限/比例档 |
| g08 | target 边界 | 整手归一/零差忽略/方向 |
| g09 | 动态 universe | 切换后新标的可查 + 懒加载计数 |
| g10 | 调度可见性 | before_trading_start 盘前 / handle_data 收盘可见性 |
| g11 | 分红送转 | `context.portfolio.positions[sec]` 投影（ex 日新数量/成本） |
| g12 | 退市 | 估值冻结 + stale 标记 + 退市后拒单 |
| g13 | 时序 | 收盘挂单次日开盘成交（时点戳） |

## 执行方式

```bash
# 任务 JSON 样例见 configs/smallcap_lowopen_joinquant.json（strategy.type = "joinquant"）
.venv/Scripts/python -m mtzquant run -c configs/smallcap_lowopen_joinquant.json

# universe 留空时由策略源码自动嗅探（指数/基准代码）; 动态选股标的下单时自动扩池
```

```bash
# 兼容报告（COMPAT_REGISTRY 登记态，P3 定稿后可 dump）
python -c "from mtzquant.adapters.shared.compat import compat_report; print(compat_report('joinquant'))"
```
