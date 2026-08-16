# AGENTS.md — mtzQuant AI 项目上下文

> AI 编码助手进入本工程**必读**。架构全文见本地 `.zcode/mtzQuant回测框架设计方案.md`（不入库），
> 执行依据见 `.zcode/plans/mtzQuant-M0-M1-实施计划.md` 与 `mtzQuant-测试方案.md`（不入库）。

## 项目一句话

本地优先、事件驱动、多策略平台兼容的量化回测框架：本地 CSV 日线起步，聚宽/PTrade 原生策略零改动回测（M2），
回测过程实时可视（M4），记录全量入库可复现。**当前进度：M0+M1（可信日线内核）**。

## 架构一页图

```
L5 web      (M4)  浏览器 ECharts
L4 server   (M4)  FastAPI REST + WebSocket(event_seq)
L3 engine         UnifiedBacktestEngine(十阶段主循环) → BrokerSim(事件驱动撮合)
                  ├─ Scheduler / OpenOrderBook / Account / ResultStore / Metrics
                  └─ StrategyAdapter 协议 ← AdapterRegistry（native/v1, joinquant+ptrade/M2, qmt/M5）
L2 data           SourceDriver(csv) → DataNormalizer → MarketDataProvider(PIT: as_of+knowledge_time)
                  └─ DataCache(L1 内存 numpy + L2 parquet)
L1 store          SQLite + SQLAlchemy2 + Alembic（run/snapshot/manifest/metrics 物理FK；orders/fills 明细逻辑FK）
```

## 模块地图

| 路径 | 职责 | 阶段 |
|------|------|------|
| `mtzquant/core/` | 代码归一/类型/四时间模型/PIT/结构化异常 | B |
| `mtzquant/engine/` | 引擎主循环/订单状态机/撮合/账户/公司行为/指标 | B+F |
| `mtzquant/engine/models/` | 撮合五模型（Fill/Slippage/Fee/Liquidity/Latency） | B6 |
| `mtzquant/adapters/` | 平台适配（`native` 本轮；joinquant/ptrade M2） | F6 |
| `mtzquant/data/` | CSV 驱动/归一/日历/缓存/Provider/ETF下载器 | D+E |
| `mtzquant/store/` | ORM 模型/Repo/批量写缓冲 | G |
| `mtzquant/cli.py` | CLI 三调用面之一 | I |
| `mtzquant/worker/` | subprocess 隔离 | I5 |
| `research/ optimize/ ml/ server/` | 占位（M3/M4） | — |

## 依赖规则（import-linter 强制，违反即构建失败）

1. `mtzquant.adapters` **禁止** import `mtzquant.engine.broker/account/metrics`（撮合/记账/绩效只有一份实现——适配器只翻译，禁止膨胀成第二引擎）；
2. `mtzquant.data` 禁止 import `mtzquant.engine`；
3. `mtzquant.research/ml` 禁止 import `mtzquant.server`。

## 常用命令

```bash
.venv/Scripts/python -m mtzquant --version        # CLI 入口
.venv/Scripts/python -m mtzquant config          # 配置检查（config 即命令，无 check 子命令）
pytest                                          # 快速门禁（排除 slow/network）
pytest -m slow                                  # 性能/真实数据
ruff check mtzquant tests && ruff format --check mtzquant tests
mypy mtzquant
lint-imports                                    # 依赖契约
```

## 硬性纪律

1. **测试通过才能 commit**；提交前跑完上方门禁全套。
2. **密钥纪律**：任何 token/key 只进 `config/secrets.json`（已 gitignore）或 `MTZQUANT_*` 环境变量；代码/测试/文档/示例一律占位符。仓库只有 `*.example.json` 空模板。
3. **不入库清单**：`config/secrets.json`、`config/settings.json`、`data/`（本地CSV）、`results/`、`.cache/`、`mtzquant.db*`、`.zcode/`（设计/计划文档）。
4. **价格语义**（设计 3.14）：raw_price 是唯一记账/撮合基准；复权价只用于指标研究——不许把复权价喂给撮合。
5. **确定性**（设计 8.8）：禁止未播种随机（seed=42）；dict 遍历先排序；时间戳整数毫秒。
6. **接口变更**必须回写设计文档并升版本号，同时更新 `.zcode/plans/` 下计划进度。
7. 黄金用例断言**六要素**（订单/成交/现金/持仓/费用/净值）逐笔比对，禁止只看最终收益（4.9.2）。
8. 全量 `from __future__ import annotations` + 类型注解；异常带 `run_id/stage/hint`。
9. **文件头注释**：每个 `.py` 文件必须带块注释（模板见用户级 `~/.zcode/AGENTS.md`）：`# coding:utf-8` + `@author: 木头左` + `@create_time` + `@update_time` + `@description`；新建文件两时间同值，修改仅更新 `@update_time`。头注释不准确视为缺陷。

## 数据目录约定（设计 3.12）

```
data/
├── master/instruments.csv              # 标的主数据
├── kline/{stock,etf}/day/{code}.csv    # 日线（归一代码命名，如 510300.SH.csv）
├── corporate_actions/{type}/{code}.csv # 公司行为（announce/ex/pay 三日期）
├── calendars/trade_days.csv            # 交易日历
└── factor/adj_factor/{type}/{code}.csv # 复权因子
```
