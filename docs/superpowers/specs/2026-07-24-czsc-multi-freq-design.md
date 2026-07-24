# 缠论分析扩展设计：多频率 / 指数 / 全信号目录

- 日期：2026-07-24
- 分支：`feat/czsc-analysis-demo`（延续）
- 前置：`docs/superpowers/specs/2026-07-24-czsc-integration-design.md`（已落地的日K单级别 demo）
- 目标：在已有 czsc demo 基础上扩展三项能力——①多频率 K 线（1m/5m/15m/30m/60m + 周/月/季）；②指数标的支持；③信号目录 + 用户勾选（替代当前写死的 3 个信号）

## 1. 范围

### 做（In Scope）

- **多频率**：`/api/czsc/analyze` 新增 `freq` 参数，支持 9 档频率（日线/周线/月线/季线/1分钟/5分钟/15分钟/30分钟/60分钟）。周/月/季从日K resample；5/15/30/60 分钟从 1 分钟 resample（czsc `resample_bars`）。
- **指数支持**：复用 `resolve_asset_type` + `get_daily_asset`（日/周/月/季已通）；指数分钟级用逐日 `fetch_minute_single` 实时拉取拼接（A3 方案，不落库）。
- **全信号目录**：新增 `/api/czsc/signals` 端点，调 `czsc._native.list_all_signals()` 返回信号目录；`/analyze` 新增 `signals` 参数（逗号分隔信号名），用户勾选要跑的信号；`/status` 返回默认推荐信号名列表。
- 前端：频率选择器、信号勾选面板（按 category 分组 + 默认推荐集预勾选）、图表分钟级 datetime 支持、搜索放开 stock/etf/index。

### 不做（Out of Scope）

- 指数分钟K持久化存储（`kline_index_minute`）—— A3 先用实时拉取跑通，存储升级作为独立后续项
- 多级别联立分析（CzscTrader 跨周期）—— 仍单级别
- 信号参数自定义（di/ma_type/timeperiod 等仍用 czsc 默认值，`signals_config` 只传 `{name, freq}`）
- 信号结果缓存 / 性能优化（每次现算）
- 年线（Freq.Y）—— 需求未提，季线已够长周期

## 2. 架构

延续现有分层（`api/czsc.py` HTTP 薄壳 + `czsc_service.py` 唯一 import czsc 处）。新增频率配置表 + resample 层 + 信号目录。

```
前端 CzscAnalysis (频率选择器 + 信号勾选 + CzscKChart)
   │  GET /api/czsc/analyze?symbol=&freq=&days=&signals=
   │  GET /api/czsc/signals        信号目录
   │  GET /api/czsc/status         可用性 + 默认推荐信号
   ▼
后端 app/api/czsc.py            ← HTTP 薄壳
   ▼
后端 app/services/czsc_service.py
   │  ① FREQ_CONFIG[freq] 查频率配置（数据源族 / resample目标 / 默认窗口 / czsc Freq串 / init_n）
   │  ② resolve_asset_type → stock/etf/index
   │  ③ 按频率族取数：
   │       日线族(日/周/月/季): get_daily_asset → (周/月/季 polars resample) → bars
   │       分钟族(1m-60m): 取1分钟序列(stock/etf:get_minute_range; index:逐日fetch_minute_single)
   │                     → (非1m: czsc resample_bars) → bars
   │  ④ CZSC(bars) → fx_list/bi_list/zs_list
   │  ⑤ _build_signals_config(选中信号名, freq串) → generate_czsc_signals
   │  ⑥ _serialize（freq 影响日期格式：分钟族 %Y-%m-%d %H:%M）
   ▼
czsc._native (Rust/PyO3)
```

### 隔离原则

- 所有 `import czsc` 仍在 `czsc_service.py`；`api/czsc.py` 不直接 import czsc。
- `resample_bars` / `format_standard_kline` / `list_all_signals` 调用集中在 service 私有 helper。
- 前端不感知 czsc，只消费 JSON。

## 3. 数据流与数据结构

### 频率配置表 `FREQ_CONFIG`（集中常量，便于调整）

放在 `czsc_service.py` 顶部，dict 形式。每条含：`freq_str`（czsc 中文字符串）、`family`（daily/minute）、`default_days`、`max_days`、`init_n`。

| key (API freq) | freq_str | family | default_days | max_days | init_n | 说明 |
|----------------|----------|--------|--------------|----------|--------|------|
| 日线 | 日线 | daily | 300 | 500 | 50 | 现有 |
| 周线 | 周线 | daily | 100 | 300 | 20 | polars 日→周 |
| 月线 | 月线 | daily | 60 | 200 | 12 | polars 日→月 |
| 季线 | 季线 | daily | 40 | 100 | 8 | polars 日→季 |
| 1分钟 | 1分钟 | minute | 3 | 10 | 200 | 取1m原始 |
| 5分钟 | 5分钟 | minute | 10 | 30 | 100 | resample_bars 1m→5m |
| 15分钟 | 15分钟 | minute | 20 | 60 | 50 | resample_bars 1m→15m |
| 30分钟 | 30分钟 | minute | 40 | 90 | 30 | resample_bars 1m→30m |
| 60分钟 | 60分钟 | minute | 60 | 120 | 20 | resample_bars 1m→60m |

> `init_n` 分钟族调大（分钟 bar 多，需更多预热才稳定出信号）。这些数字集中于此，后续可一行调整。

### 数据获取

**日线族**（日/周/月/季）：
```python
end = date.today()
# days 是目标频率根数, 换算成日K日历日范围 (周/月/季需更大窗口, 否则 resample 后根数不足)
start = end - timedelta(days=days * _DAILY_CALENDAR_FACTOR[freq])  # 日线2/周线7/月线30/季线90
asset_type = repo.resolve_asset_type(symbol)
df = repo.get_daily_asset(asset_type, symbol, start, end)
if freq != "日线":
    df = _resample_daily(df, freq)        # polars group_by_dynamic
bars = _df_to_bars(df, freq_str)          # format_standard_kline(freq=freq_str)
```

**分钟族**（1m-60m）：
```python
df_1m = _fetch_minute_series(repo, asset_type, symbol, days)   # 返回 polars DF, 列含 datetime/volume
if freq != "1分钟":
    # resample_bars 只接受 pandas DataFrame (会拒绝 polars)，需先转
    pdf = df_1m.rename({"datetime": "dt", "volume": "vol"}).to_pandas()
    pdf["dt"] = pd.to_datetime(pdf["dt"])            # resample_bars 要求 dt datetime64[ns] 且 tz-naive
    bars = resample_bars(pdf, target_freq=freq_str, base_freq="1分钟", raw_bars=True)
else:
    bars = _df_to_bars(df_1m, "1分钟")                # _df_to_bars 内部做 polars→pandas+rename
```

> tz-naive 已确认安全：`get_minute_range` / `fetch_minute_single`（经 `_normalize_minute`）返回的 `datetime` 均为 naive（`pl.Datetime("us")` 无时区），无需额外 `tz_localize`。

`_fetch_minute_series(repo, asset_type, symbol, days)`：
- stock/etf：**本地优先 + 缺失交易日实时补拉**（对齐 `/api/kline/minute` 降级模式）。先 `repo.get_minute_range([symbol], start, end, asset_type)` 读持久化 parquet；从日K推导预期交易日列表，对本地缺失的交易日逐日 `kline_sync.fetch_minute_single(symbol, day, asset_type)` 实时拉取（不落库），polars concat。本地已有数据不重复拉。
- index：取窗口内交易日列表（从 `repo.get_daily_asset("index", symbol, ...)` 的 date 列推导），逐日 `kline_sync.fetch_minute_single(symbol, day, asset_type="index")`，polars concat。单日失败跳过（warning 日志），不阻断；拼接后 bar 数过少返回空。

### polars 日→周/月/季 resample（`_resample_daily`）

用 `group_by_dynamic` 按自然周（周一起始）/月/季聚合 OHLCV：
- open=first, close=last, high=max, low=min, volume=sum, amount=sum
- dt 取每桶首日
- A股周末非交易日，自然周桶天然只在交易日有数据，无需特殊处理

### czsc Freq 枚举对应（已核实 `_format_standard_kline._FREQ_MAP`）

日线→Freq.D，周线→Freq.W，月线→Freq.M，**季线→Freq.S**（注意非 Q），1分钟→Freq.F1，5分钟→Freq.F5，15分钟→Freq.F15，30分钟→Freq.F30，60分钟→Freq.F60。`format_standard_kline(df, freq)` 接受中文字符串直接解析。

### 信号目录（`/api/czsc/signals`）

调 `czsc._native.list_all_signals(include_kline=True, include_trader=False)`。**只取 kline 类信号**——`generate_czsc_signals` 仅支持 kline 类信号（`signals_dispatcher.rs` 限制），trader 类信号无法通过本接口运行，展示会误导用户，故过滤掉。

`list_all_signals` 每条返回 `{name, category, namespace, param_template}`（**无 `desc` 字段**）。`list_signals()` 在 service 层后处理补 `desc`：从 `param_template` 末尾的可读片段解析（如 `{freq}_D{di}B_BUY1V221126` → "BUY1"），解析不到则 `desc=""`。

分组按 **`namespace`**（信号名首段下划线前缀，如 `cxt`/`bar`/`tas`/`zdy`）——这是 `list_all_signals` 运行时可得的字段。**不依赖** `dump_signal_catalog.py` 的 `FILE_METADATA`（那是按 `.rs` 源文件名分组的，运行时不可得）。用显式 `namespace→中文显示名` 映射表（集中在 `czsc_service.NAMESPACE_LABEL`），未登记的 namespace 回退为原值：

```python
NAMESPACE_LABEL = {
    "cxt": "缠论结构",
    "bar": "K线形态",
    "tas": "TA指标",
    "vol": "成交量",
    "obv": "OBV",
    "jcc": "经典K线形态",
    "zdy": "自定义指标",
    # 其余 namespace 直接用原值作分组名
}
```

返回结构：
```jsonc
{
  "available": true,
  "groups": {
    "缠论结构": [ { "name": "cxt_first_buy_V221126", "category": "kline", "namespace": "cxt", "param_template": "{freq}_D{di}B_BUY1V221126", "desc": "BUY1" } ],
    "TA指标": [ ... ],
    "成交量": [ ... ]
  },
  "total": 123
}
```

### 默认推荐信号（`DEFAULT_SIGNALS`，集中在 `czsc_service.py`）

```python
DEFAULT_SIGNALS = [
    "cxt_bi_status_V230102",       # 笔状态
    "cxt_first_buy_V221126",       # 一买
    "cxt_first_sell_V221126",      # 一卖
    "cxt_second_bs_V230320",       # 二类买卖点
    "cxt_third_bs_V230318",        # 三类买卖点
    "cxt_bi_base_V230228",         # 笔基础状态
]
```
`/status` 返回 `{"available": bool, "default_signals": [...]}`。

### `signals_config` 构建（`_build_signals_config`）

```python
def _build_signals_config(signal_names: list[str], freq_str: str) -> list[dict]:
    return [{"name": n, "freq": freq_str} for n in signal_names]
```
只传 name+freq，参数用 czsc 默认值（现有 demo 已验证可行）。`generate_czsc_signals(bars, signals_config, init_n=cfg.init_n)`。

### API 响应结构（`/analyze`，增量 freq）

```jsonc
{
  "available": true,
  "symbol": "000001.SZ",
  "freq": "5分钟",                    // 新增：回显请求 freq
  "bars": [ { "date": "2025-07-24 09:35", "open":.., "high":.., "low":.., "close":.., "volume":.. } ],
  // 分钟族 date 为 "YYYY-MM-DD HH:MM"；日线族仍为 "YYYY-MM-DD"
  "fx_list": [ { "dt": "2025-07-24 10:00", "price":.., "mark":"top" } ],
  "bi_list": [ { "a_dt":.., "a_price":.., "b_dt":.., "b_price":.., "direction":"up" } ],
  "zs_list": [ { "sdt":.., "edt":.., "zd":.., "zg":.. } ],
  "signals": [ { "dt":.., "5分钟_D1B_BUY1":"其他_其他_任意_0", ... } ],  // key 前缀随 freq 变
  "signal_markers": [ { "dt":.., "kind":"buy", "label":"一类买点", "price":.. } ]
}
```

降级响应不变（`{available:false, message:...}`）。

### 信号→买卖标记提取（泛化，value 驱动）

当前只提取 BUY1/SELL1。泛化为覆盖一二三类买卖点。**关键：czsc 各类买卖点信号的 key 命名不统一**（经审查 `crates/czsc-signals/src/cxt.rs` 确认）：

| 信号名 | 输出 key 片段 | 触发 value |
|--------|--------------|-----------|
| `cxt_first_buy_V221126` | 含 `BUY1` | `一买...` |
| `cxt_first_sell_V221126` | 含 `SELL1` | `一卖...` |
| `cxt_second_bs_V230320` | 含 `BS2辅助` | `二买...`/`二卖...` |
| `cxt_second_bs_V240524` | 含 `第二买卖点` | `二买...`/`二卖...` |
| `cxt_third_bs_V230318` | 含 `BS3辅助` | `三买...`/`三卖...` |
| `cxt_third_buy_V230228` | 含 `三买辅助` | `三买...`（仅买） |

**因此不能假设统一 `BUY[123]/SELL[123]` key 模式**（BS2/BS3 的 key 不含 BUY2/SELL2）。采用 **value 驱动**提取：value 前缀已编码级别+方向，与 key 命名无关，最稳健。

```python
# value 前缀 → (方向, 标签)；value 不含「其他」即视为触发
_BS_VALUE_PREFIX = {
    "一买": ("buy", "一类买点"), "一卖": ("sell", "一类卖点"),
    "二买": ("buy", "二类买点"), "二卖": ("sell", "二类卖点"),
    "三买": ("buy", "三类买点"), "三卖": ("sell", "三类卖点"),
}
```

提取规则：遍历每 bar 信号 dict 的所有 string value，若 value 不含「其他」且以 `_BS_VALUE_PREFIX` 某 key 开头 → 生成 marker（kind/label 取自前缀映射，price 取该 bar close，dt 格式化）。一个 bar 可能产生多个 marker（不同信号同时触发），不去重。

> 注：仅对 value 做前缀匹配即可，无需匹配 key。这些「一买/二买/三买/一卖/二卖/三卖」是缠论买卖点专属术语，非买卖点信号不会产生此前缀的 value，误报风险极低。

## 4. 后端组件

### `backend/app/api/czsc.py`（改）

```python
@router.get("/analyze")
def analyze(
    request: Request,
    symbol: str = Query(...),
    freq: str = Query("日线", description="日线/周线/月线/季线/1分钟/5分钟/15分钟/30分钟/60分钟"),
    days: int | None = Query(None, ge=1, description="取近 N 根; 不传则用该 freq 默认值"),
    signals: str | None = Query(None, description="逗号分隔信号名; 不传则用默认推荐集"),
):
    if not czsc_service.is_available():
        return {"available": False, "message": "缠论分析需要 czsc 扩展，请运行: uv sync --extra czsc"}
    repo = request.app.state.repo
    sig_list = [s.strip() for s in signals.split(",") if s.strip()] if signals else None
    return czsc_service.analyze(repo, symbol, freq, days, sig_list)

@router.get("/signals")
def signals_catalog():
    return czsc_service.list_signals()

@router.get("/status")
def status():
    return {"available": czsc_service.is_available(), "default_signals": czsc_service.DEFAULT_SIGNALS}
```

`days` 改为可选：不传用 `FREQ_CONFIG[freq].default_days`，传了按 `max_days` clamp。

### `backend/app/services/czsc_service.py`（改）

新增/改动的符号：
```python
FREQ_CONFIG: dict[str, FreqConfig]            # 频率配置表（常量）
DEFAULT_SIGNALS: list[str]                    # 默认推荐信号（常量）

def analyze(repo, symbol, freq="日线", days=None, signals=None) -> dict   # 改签名
def list_signals() -> dict                    # 新增：信号目录
def _df_to_bars(df, freq_str) -> list         # 改：freq 参数化（原硬编码 Freq.D）
def _resample_daily(df, freq_str) -> pl.DataFrame   # 新增：日→周/月/季 polars
def _fetch_minute_series(repo, asset_type, symbol, days) -> pl.DataFrame  # 新增
def _build_signals_config(names, freq_str) -> list[dict]   # 新增
def _serialize(c, signals_result, symbol, freq) -> dict    # 改：freq 影响日期格式
def _fmt_dt(ts, minute=False) -> str          # 改：minute 族用 %Y-%m-%d %H:%M
def _extract_signal_markers(signals_result, bars) -> list  # 改：泛化 BUY[123]/SELL[123]
```

`FreqConfig` 用 `dataclass`：`freq_str, family, default_days, max_days, init_n`。

`is_available()` 不变。

### 测试 `backend/tests/test_czsc_service.py`（改/增）

- 既有用例适配新 `analyze` 签名（freq/days/signals）
- `test_freq_config` — 9 档频率配置齐全，freq_str 正确
- `test_resample_daily` — 给定日K，验证周/月/季聚合 OHLCV 正确（first/last/max/min/sum）
- `test_build_signals_config` — 信号名+freq → `[{name, freq}]`
- `test_signal_marker_extraction_generalized` — value 驱动：value 以 一买/二买/三买/一卖/二卖/三卖 开头且不含「其他」→ 提取对应 marker；含「其他」不触发；模拟 BS2 信号（key 含 `BS2辅助`、value `二买...`）验证能提取（回归 B1 场景）
- `test_fmt_dt_minute` — 分钟 Timestamp → "YYYY-MM-DD HH:MM"
- `test_list_signals` — `list_all_signals` 返回结构（`pytest.importorskip("czsc")`）
- `test_analyze_minute_mock` — 用 czsc.mock 生成分钟数据走通分钟族 analyze

## 5. 前端组件

### `frontend/src/lib/api.ts`（改）

```typescript
export type CzscFreq = '日线'|'周线'|'月线'|'季线'|'1分钟'|'5分钟'|'15分钟'|'30分钟'|'60分钟'

export interface CzscSignalEntry {
  name: string; category: string; namespace: string; param_template: string; desc: string
}
export interface CzscSignalsCatalog {
  available: boolean; groups: Record<string, CzscSignalEntry[]>; total: number
}
export interface CzscStatus { available: boolean; default_signals: string[] }

// 改签名
czscAnalyze: (symbol: string, freq: CzscFreq = '日线', days?: number, signals?: string[]) =>
  request<CzscAnalyzeResponse>(
    `/api/czsc/analyze?symbol=${encodeURIComponent(symbol)}&freq=${freq}`
    + (days ? `&days=${days}` : '')
    + (signals?.length ? `&signals=${encodeURIComponent(signals.join(','))}` : '')
  ),
czscSignals: () => request<CzscSignalsCatalog>('/api/czsc/signals'),
czscStatus: () => request<CzscStatus>('/api/czsc/status'),
```

`CzscBar.date` 注释说明：分钟族为 "YYYY-MM-DD HH:MM"。

### `frontend/src/pages/CzscAnalysis.tsx`（改）

- 顶部搜索栏旁加 **频率选择器**（9 档下拉，默认日线）。切换频率时按 `FREQ_DEFAULTS`（前端镜像后端默认 days，或直接不传 days 让后端定）重置 days。
- 搜索组件放开 stock/etf/index：`StockFinancialSearch` 增加可选 `assetTypes` prop（默认 `'stock,etf,index'`），透传给 `api.instrumentSearch(q, limit, assetTypes)`。**后端 `/api/kline/instruments/search` 已支持 `asset_types=index`**（`search_instruments` 遍历 `get_instruments_asset(t)`，已确认 `get_instruments_asset("index")` 返回指数 instruments）——无需改后端。CzscAnalysis 页传入 `assetTypes="stock,etf,index"`。
- `CzscAnalysisBoard` query key 加 freq + signals：`['czsc-analyze', symbol, freq, signalsKey]`。
- 右侧侧栏加 **信号勾选面板**：`useQuery` 拉 `/api/czsc/signals` 目录，按 group 渲染 checkbox 列表，默认勾选 `/status` 返回的 `default_signals`。勾选状态变化触发 re-fetch。提供「全选/清空/恢复默认」按钮。选中信号数过多（>30）时提示性能警告。
- 摘要侧栏统计数字保持（分型/笔/中枢/买卖点）。
- 空状态文案适配：分钟级无数据提示「该标的未同步分钟K（Pro+ 功能），或非交易日」。

### `frontend/src/components/czsc/CzscKChart.tsx`（改）

- x 轴 date 匹配逻辑：`dateIndex` 用完整字符串（已含分钟族 "YYYY-MM-DD HH:MM"）。fx/bi/zs/marker 的 dt 直接按字符串匹配 `dateIndex.has(dt)`。
- dataZoom / axisLabel：分钟族用 `%Y-%m-%d %H:%M` 格式化，日线族用 `%Y-%m-%d`。可依据 dt 字符串长度判断（含空格→分钟族）。
- 其余渲染（candlestick/markPoint/markArea/line series）不变。

### 菜单/路由

已有「缠论分析」菜单项与路由，无需新增。`/status` 返回 `default_signals` 后菜单逻辑不变（仍按 available 显隐）。

## 6. 降级策略

| 场景 | 行为 |
|------|------|
| czsc 未装 | `/analyze`、`/signals`、`/status` 均返回 `{available:false}`；前端安装提示 |
| 未知 freq | `analyze` 校验 freq ∈ FREQ_CONFIG，否则 400「不支持的频率」 |
| 股票无对应频率数据 | 返回空 bars + 空 fx/bi，前端空状态（文案区分日K/分钟K） |
| 股票/ETF 分钟本地无数据 | `_fetch_minute_series` 对缺失交易日逐日 `fetch_minute_single` 实时补拉（不落库）；若日K也无 → 返回空 bars |
| 指数分钟逐日拉取全部失败 | `_fetch_minute_series` 返回空 df → analyze 返回空 bars + `message:"分钟K数据不足"` |
| 选中信号名不存在 | `generate_czsc_signals` 对未知信号忽略（不报错）；日志 warning |
| 信号数过多致超时 | 分钟族 × 大量信号可能数秒；前端设 `staleTime` + loading；后端无硬超时（同步计算），靠 days/信号数自律 |
| days 超过 max_days | API 层 clamp 到 max_days |

## 7. 渲染映射关键约束

### 日期格式对齐

- 日线族：dt 统一 `%Y-%m-%d`（现有）
- 分钟族：dt 统一 `%Y-%m-%d %H:%M`（24h，无秒）。`_fmt_dt(ts, minute=True)` 用 `strftime("%Y-%m-%d %H:%M")`
- fx/bi/zs/marker 的 dt 同上。前端按完整字符串匹配 dateIndex，不过滤时间部分。

### 分钟 resample 时区

czsc `resample_bars` 要求 dt **tz-naive**（Rust 拒绝 tz-aware）。`get_minute_range` 返回的 `datetime` 是 naive（本地时间），可直接用。`fetch_minute_single` 返回的也需确保 naive（实现时核对 `_normalize_minute` 输出）。

### 信号 key 前缀随 freq 变

信号 key 格式 `"{freq}_{信号key}"`，freq 变化时 key 前缀变（如 `5分钟_D1B_BUY1` vs `日线_D1B_BUY1`）。`_extract_signal_markers` 按 key 内容匹配 `BUY[123]/SELL[123]`，不依赖前缀，天然兼容。

## 8. 成功标准

1. `uv sync --extra czsc` 后，`GET /api/czsc/analyze?symbol=000001.SZ&freq=5分钟&days=10` 返回非空 bars（带时分）+ fx/bi + signals
2. `GET /api/czsc/analyze?symbol=000001.SH&freq=日线`（指数）返回非空结果
3. `GET /api/czsc/analyze?symbol=000001.SH&freq=1分钟&days=3`（指数分钟）返回非空 bars 或明确失败 message
4. `GET /api/czsc/analyze?symbol=000001.SZ&freq=周线` 返回周线聚合 bars（条数 ≈ 日线/5）
5. `GET /api/czsc/signals` 返回分组目录，total > 40
6. `GET /api/czsc/analyze?...&signals=cxt_first_buy_V221126,cxt_second_bs_V230320` 只跑指定信号
7. 前端切换频率/勾选信号后图表正确刷新；分钟级图表 x 轴显示时分
8. 不装 czsc 时页面显示安装提示，不报错，其他功能不受影响
9. 后端测试 `test_czsc_service.py` 在装了 czsc 的环境全绿
10. 改动隔离：不改动现有业务逻辑，只扩展 czsc 相关文件 + api.ts/菜单少量增量

## 9. 风险与待确认项

| 项 | 风险 | 处理 | 状态 |
|----|------|------|------|
| 指数分钟逐日拉取慢 | N 天 = N 次 API 调用 | 窗口短（1m×3d=3次）；单日失败跳过；A3 明确为过渡方案 | 已接受 |
| 信号全选性能 | 100+ 信号 × 分钟 bar 数秒 | 前端 >30 提示；用户自律 | 已记录 |
| 前端搜索是否支持 index | StockFinancialSearch 只搜 stock | 已确认后端 `/api/kline/instruments/search` 支持 `asset_types=index`；前端加 assetTypes prop 透传 | ✅ 已解决 |
| `_normalize_minute` 输出 tz | resample_bars 要求 naive | 已确认 `get_minute_range`/`fetch_minute_single` 返回 naive datetime，无需 tz_localize | ✅ 已解决 |
| 信号 key 命名不统一 | BS2/BS3 key 不含 BUY2/SELL2 | marker 提取改为 value 驱动（前缀 一买/二买/三买/一卖/二卖/三卖），与 key 无关 | ✅ 已解决 |
| list_all_signals 无 desc 字段 | 前端类型期望 desc | `list_signals()` 后处理补 desc（从 param_template 解析，解析不到留空） | ✅ 已解决 |
| 信号分组依赖文件名不可得 | FILE_METADATA 运行时不可用 | 改按 namespace 分组 + NAMESPACE_LABEL 显式映射；过滤 trader 类信号 | ✅ 已解决 |
| 周/月/季 resample 末根 | polars group_by_dynamic 含未完成周 | 可接受（缠论对末根不敏感，fx/bi 会自修正） | 已接受 |
| czsc import 首次耗时 | 首请求慢 | 现有设计已记录启动预热建议 | 已记录 |

## 10. 后续（不在本期）

- 指数分钟K持久化（A2：`kline_index_minute` + sync 路径）
- 信号参数自定义 UI（di/ma_type/timeperiod）
- 信号结果缓存（同 symbol+freq+signals 短时缓存）
- 多级别联立（CzscTrader 跨周期共振）
