# mtzQuant

本地优先、事件驱动、多策略平台兼容的量化研究与回测框架。

- **本地 CSV 起步**：数据源地址全部配置化；日线 + 分钟线（分钟 M5）
- **平台兼容**：聚宽 / PTrade 原生策略力争零改动本地回测（M2；以语义等级 S0-S4 与黄金用例验收）
- **事件驱动撮合**：订单生命周期（订单 ≠ 成交 ≠ 拒单）、容量约束、涨跌停两态、T+1、公司行为三时点
- **点时正确**：所有数据查询带 `as_of` + `knowledge_time` 双重校验，杜绝未来数据
- **确定性复现**：RunManifest 记录代码/数据/环境全版本指纹，同 manifest 重放逐笔一致
- **全量入库**：参数/策略快照/逐单/逐笔/逐日净值/指标（SQLite，可切 PostgreSQL）

## 状态

`M0+M1 开发中`（语义规格 + 可信日线内核）。路线图：M0 规格 → M1 日线内核 → M2 平台适配 → M3 向量化研究 → M4 Web 实时可视 → M5 分钟级/实盘。

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
