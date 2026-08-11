# 开发笔记（fork 私有）

后续按主题追加小节。

---

## 当前配置

- TickFlow 账号：none 档
- 实时行情：自定义数据源（HTTP 源，YAML 配置）
- 监控目标策略：`ai_20260807_etf_momentum`（ETF 动量轮动，asset_type=etf）

### ETF 实时行情接入要点

- 自定义源 `/realtime` 返回的 records 须包含 ETF 行情，symbol 格式与 `instruments_etf` 表一致（如 `513100.SH`）
- `realtime_pull_etf` 开关对自定义源无关（那是 TickFlow 源专属的请求构造逻辑）
- 前提：ETF instruments 表非空（盘后管道跑过 ETF instruments 同步）
