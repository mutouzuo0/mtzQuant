# PTrade 兼容清单

> M2-L7 交付（设计 4.7/4.9）。目标：**原生 PTrade 策略零改动加载**（用户体验目标），
> 兼容性以**语义等级 S0–S4** 承诺，不承诺与官方回测结果一致。
> 验收纪律（4.9）：黄金用例逐项通过 = **S3 级**；禁止以「与官方回测同量级/收益接近」验收。

## 语义等级（4.9）

| 等级 | 承诺 | 验证手段 |
|------|------|---------|
| S0 | 可加载，生命周期可启动 | 冒烟测试 |
| S1 | API 签名兼容（参数/返回类型不报错） | 单元测试 |
| S2 | 返回数据结构与字段语义与目标平台一致 | 对照样例数据 |
| S3 | **时间可见性、调度、订单语义**与目标平台一致（无未来数据、时点正确、成交时序正确） | 黄金用例断言（g01-g13 PTrade 版全绿） |
| S4 | 黄金用例上与目标平台（官方回测）结果误差受控 | 双平台对照运行 |

## API 兼容表（L0/L1/L2）

### 生命周期钩子（S3）

| API | 级别 | 说明 |
|-----|------|------|
| `initialize(context)` | L0 | 初始化入口；`set_*`/`run_daily` 仅限此内调用 |
| `handle_data(context, data)` | L0 | 主驱动（日线 15:00）；`data[symbol]` → PTradeBarData（官方字段） |
| `before_trading(context, data)` | L0 | 盘前（当日 bar 不可见, 5.2） |
| `after_trading_end(context)` | L0 | 盘后 |

### 下单族（L0，K5 归一，买正卖负）

| API | 级别 | 说明 |
|-----|------|------|
| `order(symbol, amount)` | L0 | 按股数（amount>0 买 / <0 卖）；返回 PTradeOrder 模拟回执 |
| `order_target(symbol, target_amount)` | L0 | 目标股数 |
| `order_value(symbol, value)` | L0 | 按金额 |
| `order_target_value(symbol, target_value)` | L0 | 目标市值（整手/零差忽略/方向, g08） |
| `order_market(symbol, amount)` | L1 | 市价单（日线撮合语义同 order） |
| `cancel_order(symbol, entrust_no)` | L0 | 撤单（受理后执行, 5.3.1） |
| `get_orders()/get_order(ref)/get_open_orders()` | L0 | 订单查询族（5.3 订单生命周期视图） |
| `get_trades()` | L1 | 当日成交回查 |

### 数据族（S2~S3）

| API | 级别 | 说明 |
|-----|------|------|
| `get_history(count, unit, field, symbol_list, ...)` | L0 | 批量历史（PIT: as_of/knowledge_time, 3.13） |
| `get_price(symbol, start_date, end_date, frequency, fields, ...)` | L0 | 区间/根数行情 |
| `get_snapshot(symbol_list)` | L1 | 当日 bar 快照（计降级清单） |
| `get_position(symbol)` / `get_positions()` | L0 | 持仓（PTrade Position 投影: amount/enable_amount/cost_basis/last_sale_price/today_amount） |

### 日历/工具（L0）

| API | 级别 | 说明 |
|-----|------|------|
| `get_trading_day()` | L0 | 当前交易日 |
| `get_trade_days(start, end)` | L0 | 交易日历区间 |
| `get_all_trades_days()` | L0 | 全量交易日历 |
| `check_limit(symbol, price)` | L0 | 涨跌停状态判定 |
| `is_trade(symbol)` | L0 | 是否可交易（恒 False 已知近似） |
| `get_frequency()` | L0 | 回测频率（日线=1d） |

### 配置族（L0）

| API | 级别 | 说明 |
|-----|------|------|
| `set_universe(symbols)` | L0 | 动态 universe（g09: 切换后新标的可查, 懒加载） |
| `set_benchmark(symbol)` | L0 | 基准（运行时记降级, 不生效——基准取任务配置） |
| `set_commission(type, ...)` | L0 | 按品种费率（映射 FeeModel） |
| `set_slippage(ratio)` | L0 | 滑点比率 |
| `set_fixed_slippage(price)` | L0 | 固定滑点 |
| `set_volume_ratio(ratio)` | L0 | → LiquidityModel 参与率（默认 0.25） |
| `set_limit_mode('UNLIMITED')` | L0 | 关闭容量约束 |
| `set_yesterday_position(pos_dict)` | L0 | → 任务 initial_positions（底仓, 3.6） |

### 调度族（S3）

| API | 级别 | 说明 |
|-----|------|------|
| `run_daily(context, func, time)` | L0 | 仅 initialize 可注册；日线 time 折叠 15:00（5.2 官方对齐） |
| `run_interval(seconds)` | L2 | **仅实盘**（PTrade 官方语义）；回测结构化报错 |

## 状态映射（PTrade 15 态 → 引擎, 4.7）

PTrade 官方 15 种委托状态映射到引擎 `OrderStatus`（回测可达子集）:

| PTrade | 引擎 | 说明 |
|--------|------|------|
| 0 未报 / 1 待报 | PENDING | 已受理未撮合 |
| 2 已报 | SUBMITTED | 挂单中 |
| 5 部撤 | PARTIALLY_CANCELLED | 部分成交后撤余 |
| 6 已撤 | CANCELLED | 撤单成功 |
| 7 部成 | PARTIALLY_FILLED | 部分成交（容量截断） |
| 8 已成 | FILLED | 全量成交 |
| 9 废单 | REJECTED | 拒单（拒因见 info_json） |
| 其余（3/4/10..14） | — | 回测不可达（无真实通道, 4.7 说明） |

## 已知近似清单（4.7 / 4.9 登记）

1. **日线时刻折叠**：`run_daily` 盘中时刻折叠 15:00 执行并记 `semantic_degradation`。
2. **`get_frequency`**：日线回测恒返回 `1d`。
3. **`is_trade`**：恒 False（日线无实时可交易性判定）。
4. **`set_benchmark` 运行时设置不生效**：基准始终取任务配置（记降级）。
5. **`get_snapshot`** 计降级清单（当日快照近似, 无盘口）。
6. **`run_interval`** 回测结构化报错（官方仅实盘, L2）。
7. **北交所 `.BJ`**：无平台别名，原样透传（登记为已知近似）。
8. **订单模拟回执**：`PTradeOrder` 字段子集（status/filled/avg_price/…），状态经
   `sync_orders` 绑定引擎订单后与撮合一致（S3 时序）。
9. **`data[symbol]` 涨跌停价**：日线 bar 由数据侧 limit 档案给出；缺失为 0（官方分钟语义近似）。

## 黄金用例覆盖（S3，tests/golden/platform_ptrade/）

| 用例 | 语义 | PTrade 验证点 |
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
| g10 | 调度可见性 | before_trading 盘前 / handle_data 收盘可见性 |
| g11 | 分红送转 | get_position 投影（ex 日新数量/成本） |
| g12 | 退市 | 估值冻结 + stale 标记 + 退市后拒单 |
| g13 | 时序 | 收盘挂单次日开盘成交（时点戳） |

## 执行方式

```bash
mtzquant run --strategy examples/strategies/ptrade/demo_all_weather.py --universe 510300.SS
```
