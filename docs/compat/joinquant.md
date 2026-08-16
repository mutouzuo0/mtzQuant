# 聚宽（JoinQuant）兼容清单

> M2-N5 交付（设计 4.6/4.9）。目标：**原生聚宽策略零改动加载**（用户体验目标），
> 兼容性以**语义等级 S0–S4** 承诺，不承诺与官方回测结果一致。
> 验收纪律（4.9）：黄金用例逐项通过 = **S3 级**；禁止以「与官方回测同量级/收益接近」验收。

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
| `before_trading_start(context, data)` | L0 | 盘前（当日 bar 不可见，5.2 可见性） |
| `after_trading_end(context)` | L0 | 盘后（仅 context，聚宽官方签名） |
| `process_initialize(context)` | L2 | **跳过并记降级**（回测语义占位）；初始化逻辑请放 `initialize` |

### 下单族（L0，K5 归一，买正卖负）

| API | 级别 | 说明 |
|-----|------|------|
| `order(security, amount)` | L0 | 按股数（amount>0 买 / <0 卖）；返回 Order 模拟回执（is_filled/is_buy/entrust_no） |
| `order_target(security, target_amount)` | L0 | 目标股数 |
| `order_value(security, value)` | L0 | 按金额 |
| `order_target_value(security, target_value)` | L0 | 目标市值（整手/零差忽略/方向，g08） |
| `order_market(security, amount)` | L0 | 市价单（日线撮合语义同 order） |
| `order_shares(security, amount)` | L2 | 聚宽 L2，尽力实现（与 order 同义） |
| `get_trades()` | L0 | 成交列表（回测内可见成交） |

### 数据族（S2~S3）

| API | 级别 | 说明 |
|-----|------|------|
| `history(count, unit, field, security_list, df, skip_paused, include_now, fq)` | L0 | 批量 pivot 宽表；单标的返回 DataFrame；`include_now` 控制当日可见性 |
| `attribute_history(security, count, unit, fields, skip_paused, df, include_today, fq)` | L0 | 单标的历史；`include_today=False` 盘前防泄露（g10） |
| `get_price(security, start_date, end_date, frequency, fields, count, ...)` | L0 | 区间/根数行情（PIT: as_of=end_date） |
| `get_current_data(security_list)` | L0 | `{security: CurrentData 快照}`（close/volume/paused/…） |
| `data[security]` | L0 | handle_data 内当日快照对象（close/volume/paused/day） |
| `get_trade_days(start_date, end_date)` | L0 | 交易日历区间 |
| `get_all_securities(types, date)` | L2 | **M2 近似**：由 universe 内已有标的给出（master 支撑归 O6） |
| `get_index_stocks(index_symbol)` | L1 | 读本地成分快照（D6）；缺失 → 结构化报错，**不返回当前成分**（防幸存者偏差） |
| `get_extras(info, security_list, ...)` | L2 | **未实现**，结构化报错 + 替代建议（停牌标记用 `get_price(fields=['paused'])`） |

### 配置族（S2~S3）

| API | 级别 | 说明 |
|-----|------|------|
| `set_universe(securities)` | L0 | 动态 universe（g09：切换后新标的可查，懒加载） |
| `set_order_cost(open_tax, close_tax, open_commission, close_commission, min_commission)` | L2 | **买卖侧统一**佣金/印花税（4.6 已知近似） |
| `set_commission(commission_ratio, min_commission)` | L2 | 仅佣金变体 |
| `set_slippage(value)` | L0 | 滑点比率（映射引擎 SlippageModel） |
| `set_benchmark(code)` | L2 | **运行时设置不生效**（基准取任务配置，记降级） |
| `set_option(key, value)` | L2 | 仅 `auto_handle_position/use_real_price/order_volume_ratio` 可映射（记降级）；其余结构化报错 |

### 调度族（S3）

| API | 级别 | 说明 |
|-----|------|------|
| `run_daily(func, time)` | L0 | 无 context 参数（区别于 PTrade）；日线回测 time 折叠 15:00，盘中时刻记降级 |
| `run_weekly(func, weekday, time)` | L2 | 折叠到每周首交易日（记降级） |
| `run_monthly(func, monthday, time)` | L2 | 折叠到每月首交易日（记降级） |

## 已知近似清单（4.6 / 4.9 登记）

1. **日线回测时刻折叠**：`run_daily` 盘中时刻（9:30~14:50）一律折叠 15:00 执行并记
   `semantic_degradation`（`every_bar/open/close/15:00` 不记）。
2. **周/月调度折叠**：`run_weekly/run_monthly` 折叠到周/月首交易日（简化规则）。
3. **费用统一**：`set_order_cost` 买卖侧统一费率（不区分 open/close 侧）。
4. **`set_benchmark` 不生效**：基准始终取任务配置。
5. **`process_initialize` 跳过**：仅记降级不执行。
6. **成分股依赖本地快照**（`get_index_stocks`）：缺失报错，不返回当前成分（防幸存者偏差）。
7. **`get_all_securities` 近似**：由 universe 内标的给出，非全市场 master。
8. **`context.portfolio.market_cap`**：日线近似 = total_value（无盘口股本）。
9. **`context.portfolio.daily_returns`**：恒 0（日收益由引擎指标给出）。
10. **`data[security]` 字段子集**：`high_limit/low_limit` 恒 0（日线无盘口涨跌停价）。
11. **`get_extras` 未实现**：结构化报错 + 替代建议。
12. **`set_option` 仅三键**：未知键结构化报错。
13. **北交所 `.BJ`**：无平台别名，原样透传（登记为已知近似）。
14. **订单模拟回执**：为字段子集对象（`order_id/security/amount/is_buy/entrust_no/status`），
    状态经 `sync_orders` 绑定引擎订单后与撮合一致（S3 时序）。

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
# 一键装载聚宽策略（等价 mtzquant run --strategy-type joinquant）
mtzquant run --strategy strategies/joinquant/dual_ma.py --universe 510300.XSHG

# 兼容报告（COMPAT_REGISTRY 登记态，P3 定稿后可 dump）
python -c "from mtzquant.adapters.shared.compat import compat_report; print(compat_report('joinquant'))"
```
