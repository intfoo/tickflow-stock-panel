# 开发笔记（fork 私有）

本文件记录 fork 私有的开发知识、踩坑结论与场景说明，补充 [CONTRIBUTING.md](../CONTRIBUTING.md) 与 [FORKING.md](../FORKING.md) 未覆盖的细节。上游同步零冲突（上游无此文件）。后续按主题追加小节即可。

---

## ETF 实时行情接入（none 档 + 自定义数据源）

适用场景：TickFlow 账号 none 档位，实时行情走自定义数据源（YAML 配置的 HTTP 源）。

### 核心结论

**ETF 实时行情复用股票的自定义源取数通道，无需额外配置 ETF 专用数据源。** 自定义源 `/realtime` 端点一次性返回全市场 records（含股票/ETF/指数），后端按 symbol 拆分到三个资产分支各自落盘 + 焐热 enriched 缓存。

### 触发链路

```
QuoteService._poll_loop (quote_service.py:511, 间隔 = 设置页「行情刷新间隔」)
  └─ _fetch_full_market_quotes (quote_service.py:552)
       provider = preferences.get_realtime_data_provider()  ← 自定义源名
       ├─ 559: custom_sources.provider_has_dataset(provider, "realtime")?
       │        └─ YAML datasets 必须配 "realtime" 才走自定义源, 否则回退 TickFlow
       ├─ 563: get_provider(provider).get_realtime()  ← 调自定义源 /realtime 端点
       │       ★ 返回 records 全市场混在一起 (含 ETF/指数)
       └─ 567: _process_full_market_records(records)  (quote_service.py:560)
            ├─ 677-680: 按 symbol 拆 stock_records / etf_records / index_records
            │            用 etf_set = repo.get_etf_symbol_set() 做匹配
            ├─ 707-722: ETF 分支
            │    ├─ flush_live_daily_asset("etf", etf_daily_df)  → 落 kline_etf_daily
            │    └─ _flush_live_enriched(..., asset_type="etf") → 算 ETF enriched,
            │                                                          落 kline_etf_enriched
            │                                                          + 焐热 _etf_enriched_cache
            └─ 743: _evaluate_monitors → ETF 轮读 _etf_enriched_cache 评估
```

### 与 TickFlow 源的区别

| 维度 | TickFlow 源 | 自定义源 |
|------|------------|---------|
| 请求构造 | 按 universes/symbols 主动请求，需 `realtime_pull_etf` 开关才会把 ETF symbol 加入请求列表 (quote_service.py:602) | 一次性返回全市场 records，拆分在拿到后做 |
| `realtime_pull_etf` 开关 | **必须开**，否则 ETF symbol 不进请求 | **无关紧要**，自定义源走 559-568 分支不经过该开关 |
| ETF 进 enriched 的条件 | 开关开 + ETF symbol 在请求列表里 | `/realtime` 返回的 records 里包含 ETF symbol |

### 接入清单

1. **自定义源 `/realtime` 端点返回数据必须包含 ETF 行情**。symbol 格式要和 `instruments_etf` 表里完全一致（如 `513100.SH`、`159934.SZ`），否则 `_split_records_by_asset` 的 `symbol in etf_set` 匹配不上，ETF records 会落到 stock 分支被当股票处理
2. **ETF instruments 表非空**：盘后管道必须先跑过 ETF instruments 同步（让 `get_etf_instruments()` / `get_etf_symbol_set()` 返回数据），否则 `etf_set` 为空 → 拆分时所有 records 都被当 stock
3. **YAML datasets 含 "realtime"**：否则 quote_service 559 行判否，回退 TickFlow 源（none 档会拉空）

### 潜在坑

- **自选 vs 全市场**：自定义源 `/realtime` 若只返回自选的几只 ETF（而非全市场 ETF），ETF enriched 缓存里只有这几只。监控侧没问题（只评估这几只），但选股页/回测页用全市场 ETF 时会缺数据。这是数据源设计取舍，不是 bug
- **symbol 后缀大小写/格式**：`513100.SH` 而非 `513100` 或 `sh513100`，严格匹配 `instruments_etf` 表的 symbol 列

### 验证方式

盘中开启自定义源后：
1. `GET /api/intraday/status` → `etf_symbol_count` 应 > 0
2. 策略监控页 → ETF 策略（如 `ai_20260807_etf_momentum`）应开始有评估结果
3. 日志应有 `enriched 增量: N 只, YYYY-MM-DD` 含 ETF 数量
