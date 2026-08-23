# data/ — 本地数据目录（内容不入库）

目录骨架随仓库分发（`.gitkeep` 占位），**所有数据文件一律不入库**（见根目录 AGENTS.md 不入库清单）。

| 子目录 | 内容 |
|--------|------|
| `master/` | 标的主数据 `instruments.csv` + `snapshots/` 快照 |
| `kline/{stock,etf,index}/day/` | 日线 CSV（归一代码命名，如 `510300.SH.csv`） |
| `corporate_actions/{split,cash_div}/` | 公司行为 CSV（announce/ex/pay 三日期） |
| `fundamentals/{daily_basic,dividend,fina_indicator}/` | 基本面数据 |
| `calendars/` | `trade_days.csv` 交易日历 |

数据落盘方式：`mtzquant fetch-etf`（ETF 日线）、`mtzquant fetch-corp-actions`（公司行为日历）等下载命令。
设计依据：3.12 数据目录约定（详见 AGENTS.md）。
