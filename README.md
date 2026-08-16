# mtzQuant

本地优先、事件驱动、多策略平台兼容的量化研究与回测框架。

- **本地 CSV 起步**：数据源地址全部配置化；日线 + 分钟线（分钟 M5）
- **平台兼容**：聚宽 / PTrade 原生策略力争零改动本地回测（M2；以语义等级 S0-S4 与黄金用例验收）
- **事件驱动撮合**：订单生命周期（订单 ≠ 成交 ≠ 拒单）、容量约束、涨跌停两态、T+1、公司行为三时点
- **点时正确**：所有数据查询带 `as_of` + `knowledge_time` 双重校验，杜绝未来数据
- **确定性复现**：RunManifest 记录代码/数据/环境全版本指纹，同 manifest 重放逐笔一致
- **全量入库**：参数/策略快照/逐单/逐笔/逐日净值/指标（SQLite，可切 PostgreSQL）

## 状态

`M0-M4 已完成`（可信日线内核 + 平台适配 + 向量化研究 + Web 实时可视）。路线图：M5 分钟级/实盘。

## 里程碑

- **M1** 可信日线内核：事件驱动撮合 / PIT 时点 / 确定性重放 / CLI / 报告
- **M2** 平台适配：聚宽/PTrade 原生策略零改动回测（黄金用例）+ 完整 DataFetcher
- **M3** 向量化研究：因子引擎 / 股票池 / 组合构造 / 目标权重交接 / 摩擦归因 / 参数扫描 + 防过拟合套件
- **M4** Web 实时可视：`mtzquant serve` 多页面 SPA + WS 事件流（断线补帧）+ 报告 + 数据体检 + 远程访问

## 安装

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # Windows
# source .venv/bin/activate && pip install -e ".[dev]"   # Linux/macOS
```

## 快速开始

```bash
cp config/settings.example.json config/settings.json   # 本地配置（不入 Git）
cp config/secrets.example.json   config/secrets.json   # 密钥（不入 Git；tushare token 填这里）
mtzquant config
```

> 📖 **完整使用说明见 [`docs/使用说明.md`](docs/使用说明.md)**：怎么运行、怎么准备数据、怎么用 AI。

## 常用命令

```bash
mtzquant run -c configs/demo_dual_ma.json              # 执行回测
mtzquant report <run_id>                                # 生成自包含 report.html（--open 浏览器）
mtzquant list / compare / lineage / diff / rerun        # 历史与谱系
mtzquant optimize -c task.json --space '{"fast":[5,10,20],"slow":[40,60]}' --top 5   # 参数扫描
mtzquant fetch --codes 510300.SH --start 2020-01-01 --end 2025-12-31   # 数据下载
mtzquant fetch --fundamentals fina_indicator --codes 600000.SH ...      # 基本面（PIT）
mtzquant health                                          # 数据体检（DuckDB 全库扫描）
mtzquant serve                                           # Web 控制台（监控/新建/历史/扫描/数据）
mtzquant remote --provider tailscale                     # 远程访问配套（tailnet 手机可看）
```

## 开发

```bash
pytest                    # 快速门禁（排除 slow/network 用例）
pytest -m slow            # 性能预算 / 真实数据用例
ruff check mtzquant tests   # lint
mypy mtzquant               # 类型检查（宽松档）
lint-imports              # 模块依赖契约（适配器不得依赖引擎内部）
```

## 安全纪律

- 密钥只进 `config/secrets.json`（已 gitignore）或 `MTZQUANT_*` 环境变量，永不入库；
- 本地行情数据（`data/`）、回测产物（`results/`）、业务库（`mtzquant.db`）不入库；
- 入库前的任务参数自动脱敏（token/api_key/secret/webhook/password 模式匹配）。

## License

Proprietary — 仅供个人研究使用。
