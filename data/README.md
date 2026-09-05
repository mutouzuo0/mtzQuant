# data/ — 本地数据目录（内容不入库）

目录骨架随仓库分发（`.gitkeep` 占位），**所有数据文件一律不入库**（见根目录 AGENTS.md 不入库清单）。

| 子目录 | 内容 |
|--------|------|
| `master/` | 标的主数据 `instruments.csv` + `snapshots/` 快照 |
| `kline/{stock,etf,index}/day/` | 日线 CSV（归一代码命名，如 `510300.SH.csv`） |
| `kline/{etf,lof,stock}/minute/1m/{code}/` | 1分钟线按月 parquet 分片（`{YYYY-MM}.parquet`，见下） |
| `corporate_actions/{split,cash_div}/` | 公司行为 CSV（announce/ex/pay 三日期） |
| `fundamentals/{daily_basic,dividend,fina_indicator}/` | 基本面数据 |
| `factor/adj_factor/{etf,lof,stock}/` | 复权因子 CSV（基金为前/后复权合并；股票为 tushare 单因子） |
| `calendars/` | `trade_days.csv` 交易日历 |
| `scripts/` | 本地数据整理脚本（基金/股票分钟导入器与核验器，不入库） |

数据落盘方式：`mtzquant fetch-etf`（ETF 日线）、`mtzquant fetch-corp-actions`（公司行为日历）等下载命令。
设计依据：3.12 数据目录约定（详见 AGENTS.md）。

## 基金分钟线（`kline/{etf,lof}/minute/1m/`，设计 3.12 / M5 布局）

由 `scripts/import_fund_minute.py` 从本地「基金_分钟数据」压缩包导入（百度网盘源；ETF 2005-02 起、LOF 2010-08 起，1 分钟频率）。

- 布局：`kline/{etf|lof}/minute/1m/{归一代码}/{YYYY-MM}.parquet`（zstd 压缩，按月分片支持 M5 惰性加载）
- 字段：`time,open,high,low,close,volume,amount,pct_chg,amplitude`
  - `time` = bar 结束时刻（datetime64，北京时间）；`volume`/成交量为份、`amount`/成交额为元
  - 源表头（时间/代码/名称/开盘价/收盘价/最高价/最低价/成交量/成交额/涨幅/振幅）已重命名并重排为 OHLC 标准列序
  - 注意：当前框架读路径仅支持日线（M5 将接入分钟）；分钟数据为 M5 储备，勿手工改为日线目录
  - 品种族 `lof`（`instrument_type=lof`，代码如 `169201.SZ`）与 `etf` 分目录存放；M5 落地时品种推断需补 LOF 分支
- 复权因子：`factor/adj_factor/{etf|lof}/{code}.csv`，字段 `code,trade_date,factor_qfq,factor_hfq`
  （数值原样入库，LOF 前复权因子存在负值属源数据特性；框架纪律——复权价只用于指标研究，不进撮合）
- 主数据：源 `ETF基础信息列表.csv`（含上市日期/跟踪指数）与 `LOF基金列表.csv`（仅代码/名称，前缀风格
  `sz169201` 已归一）已增量并入 `master/instruments.csv`（已有行只补空不覆盖）

## A股 1分钟线（`kline/stock/minute/1m/`，设计 3.12 / M5 布局）

由 `scripts/import_stock_minute.py` 从本地「A股分钟数据」导入（百度网盘源；沪深 2000-06 起、北交所 2020 起、
含 259 只退市股全史；1 分钟频率），`scripts/verify_stock_minute.py` 提供删源包前的值级核验。

- 布局与字段同基金分钟线：`kline/stock/minute/1m/{归一代码}/{YYYY-MM}.parquet`
  - 沪深 `600000.SH`/`000001.SZ`、北交所 `920010.BJ`（源包前缀风格 `sh600000`/`bj920010` 已归一；
    北交所历史旧码已被源包统一映射为 920xxx）
  - 退市股与在市股同目录共存（退市判断看主数据；股票名不带「(退)」后缀）
- 源 CSV 表头同基金（时间/代码/名称/开盘价/收盘价/最高价/最低价/成交量/成交额/涨幅/振幅）；
  时间列**双格式并存**已统一归一：`YYYY-MM-DD HH:MM:SS`（2026-08-25 前）与 `YYYY/MM/DD HH:MM`（此后及退市包）
- 覆盖：2000-06-09 ~ 2026-08-28，5,827 只 / 84.7 万个月分片 / 约 74.5 GB（1 分钟；5/15/30/60 分钟源包未导入）
- 复权因子：`factor/adj_factor/stock/{code}.csv`，源为 tushare 单因子包（`复权因子.zip`，5890 只）——
  **单因子=后复权乘子 → 存入 `factor_hfq`，`factor_qfq` 列留空**；T00018.SH 等非股票特殊代码不入库
- 主数据：`股票列表_沪深.csv`（5,201）与 `股票列表_京市.csv`（311）经 upsert 并入
  `master/instruments.csv`（只补空不覆盖；补 `industry`/`list_date`/`exchange`；退市股如已在册不覆盖）
- 数据说明（重要）：
  - 按月归档链与按年汇总链在个别 bar 存在卖方数据差异（例：`002231.SZ` 2011-09-27 09:30 开仓 bar 按年链为空量），
    已用**按月链定稿**（`--monthly` 修正趟，逐月与分片合并 keep='last'）后删除按月归档源目录
  - 复权价只用于指标研究，不进撮合（框架纪律）；分钟读路径 M5 落地前框架仅支持日线
