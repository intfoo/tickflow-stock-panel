"""缠论分析服务 — 唯一 import czsc 的地方。

数据流:
  polars DF → pandas → rename → format_standard_kline → CZSC(bars)
  → fx_list / bi_list / signals → 序列化为 JSON

czsc 未安装时降级: is_available() 返回 False, analyze() 返回 {available:false}。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# czsc 可用性检测 (模块级缓存)
# ---------------------------------------------------------------------------
_CZSC_AVAILABLE: bool | None = None


def is_available() -> bool:
    """检测 czsc 是否可导入, 结果缓存到模块级变量。"""
    global _CZSC_AVAILABLE
    if _CZSC_AVAILABLE is not None:
        return _CZSC_AVAILABLE
    try:
        import czsc  # noqa: F401
        _CZSC_AVAILABLE = True
    except ImportError:
        _CZSC_AVAILABLE = False
    return _CZSC_AVAILABLE


# ---------------------------------------------------------------------------
# 频率配置表 (集中常量, 便于调整)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FreqConfig:
    """单档频率配置。

    Attributes:
        freq_str: czsc 中文字符串 (日线/周线/月线/季线/1分钟/5分钟/15分钟/30分钟/60分钟)
        family: "daily" (日K族) | "minute" (分钟族)
        default_days: days 不传时的默认取数窗口 (目标频率的根数; 日线族周/月/季会换算成日K日历日)
        max_days: days 上限 (clamp)
        init_n: CZSC 信号预热 bar 数 (分钟族调大)
    """
    freq_str: str
    family: str  # "daily" | "minute"
    default_days: int
    max_days: int
    init_n: int


FREQ_CONFIG: dict[str, FreqConfig] = {
    "日线":  FreqConfig("日线",  "daily",  300, 500, 50),
    "周线":  FreqConfig("周线",  "daily",  100, 300, 20),
    "月线":  FreqConfig("月线",  "daily",  60,  200, 12),
    "季线":  FreqConfig("季线",  "daily",  40,  100, 8),
    "1分钟": FreqConfig("1分钟", "minute", 3,   10,  200),
    "5分钟": FreqConfig("5分钟", "minute", 10,  30,  100),
    "15分钟": FreqConfig("15分钟", "minute", 20, 60,  50),
    "30分钟": FreqConfig("30分钟", "minute", 40, 90,  30),
    "60分钟": FreqConfig("60分钟", "minute", 60, 120, 20),
}

# 日线族: default_days 是「目标频率根数」, 取日K时需换算成日历日范围。
# 周/月/季 resample 后根数大幅减少, 不能用 days*2 (那是日线口径)。
# 系数 = 1 根目标 bar 对应的日历日 (含富余, 确保取到足够日K):
#   日线 2 日历日/根, 周线 7 (≈5交易日), 月线 30 (≈21交易日), 季线 90 (≈63交易日)
_DAILY_CALENDAR_FACTOR: dict[str, int] = {
    "日线": 2,
    "周线": 7,
    "月线": 30,
    "季线": 90,
}


# ---------------------------------------------------------------------------
# 默认推荐信号 + namespace 中文映射
# ---------------------------------------------------------------------------
# 注意: 一/二/三类买卖点由 _detect_chanlun_bs 结构法自包含产生 (不依赖 czsc 信号,
# 买卖对称且强制结构验证, 解决 czsc 一买过严0触发/二买不验证一买/三买依赖均线等问题)。
# 此处默认信号只保留结构状态/背驰信号 (供 tooltip 展示结构信息, 不画买卖点 marker)。
DEFAULT_SIGNALS: list[str] = [
    "cxt_bi_status_V230102",       # 笔表里关系 (笔状态核心)
    "cxt_bi_end_V230618",          # 笔结束辅助
    "cxt_double_zs_V230311",       # 中枢背驰 (双中枢一类买卖点)
    "cxt_three_bi_V230618",        # 三笔背驰
    "cxt_five_bi_V230619",         # 五笔背驰
    "cxt_seven_bi_V230620",        # 七笔背驰
]

# 可勾选信号白名单: czsc 共 222 个信号, 多为冷门辅助; 此处只留知名缠论结构 + 经典 TA,
# 过滤掉杂项 (jcc 形态 / zdy 自定义 / pressure / 大量 bar/tas 变体)。
# DEFAULT_SIGNALS 必须全部包含在内 (否则默认信号不在可选列表)。
SIGNAL_WHITELIST: set[str] = {
    # — 缠论结构: 笔状态 —
    "cxt_bi_status_V230102", "cxt_bi_end_V230618", "cxt_bi_base_V230228",
    "cxt_bi_trend_V230913", "cxt_bi_zdf_V230601", "cxt_bi_stop_V230815",
    # — 缠论结构: 买卖点 —
    "cxt_first_buy_V221126", "cxt_first_sell_V221126",
    "cxt_second_bs_V230320", "cxt_second_bs_V240524",
    "cxt_third_bs_V230318", "cxt_third_bs_V230319", "cxt_third_buy_V230228",
    # — 缠论结构: 中枢 / 背驰 / 形态 —
    "cxt_double_zs_V230311", "cxt_range_oscillation_V230620", "cxt_overlap_V240612",
    "cxt_three_bi_V230618", "cxt_five_bi_V230619", "cxt_seven_bi_V230620",
    "cxt_nine_bi_V230621", "cxt_eleven_bi_V230622",
    # — 缠论结构: 分型 / 决策 / 辅助 —
    "cxt_fx_power_V221107", "cxt_decision_V240526", "cxt_bs_V240526", "cxt_ubi_end_V230816",
    # — 经典 TA 指标 —
    "tas_macd_bc_V230803", "tas_macd_base_V230320", "tas_ma_base_V230313",
    "tas_boll_bc_V221118", "tas_kdj_base_V221101", "tas_rsi_base_V230227",
    # — 经典 K 线特征 —
    "bar_zdt_V230331", "bar_amount_acc_V230214",
}

NAMESPACE_LABEL: dict[str, str] = {
    "cxt": "缠论结构",
    "bar": "K线形态",
    "tas": "TA指标",
    "vol": "成交量",
    "obv": "OBV",
    "jcc": "经典K线形态",
    "zdy": "自定义指标",
    # 其余 namespace 直接用原值作分组名
}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def analyze(repo, symbol: str, freq: str = "日线", days: int | None = None,
             signals: list[str] | None = None,
             signal_params: dict[str, dict] | None = None) -> dict:
    """缠论分析主入口。

    Args:
        repo: KlineRepository 实例
        symbol: 标的代码, 如 "000001.SZ"
        freq: 频率 (日线/周线/月线/季线/1分钟/5分钟/15分钟/30分钟/60分钟)
        days: 取近 N 天; 不传则用 FREQ_CONFIG[freq].default_days, clamp 到 max_days
        signals: 信号名列表; 不传则用 DEFAULT_SIGNALS

    Returns:
        分析结果 dict (见设计文档第 3 节 API 响应结构)。
        czsc 不可用时返回降级响应。
    """
    if not is_available():
        return {
            "available": False,
            "message": "缠论分析需要 czsc 扩展，请运行: uv sync --extra czsc",
        }

    # 校验频率
    if freq not in FREQ_CONFIG:
        raise ValueError(f"不支持的频率: {freq}")

    cfg = FREQ_CONFIG[freq]
    days = days or cfg.default_days
    days = min(days, cfg.max_days)

    from czsc import CZSC, generate_czsc_signals

    asset_type = repo.resolve_asset_type(symbol)

    # --- 日线族: get_daily_asset → (周/月/季 resample) → bars ---
    if cfg.family == "daily":
        end = date.today()
        # days 是目标频率根数, 换算成日K日历日范围 (周/月/季需更大窗口)
        start = end - timedelta(days=days * _DAILY_CALENDAR_FACTOR[freq])
        df = repo.get_daily_asset(asset_type, symbol, start, end)

        if df.is_empty():
            return _empty_result(symbol, freq)

        if freq != "日线":
            df = _resample_daily(df, freq)

        bars = _df_to_bars(df, cfg.freq_str)

    # --- 分钟族: _fetch_minute_series → (非1m: resample_bars) → bars ---
    elif cfg.family == "minute":
        df_1m = _fetch_minute_series(repo, asset_type, symbol, days)
        if df_1m.is_empty():
            return _empty_result(symbol, freq, "分钟K数据不足（未同步或非交易日）")

        # 防御: format_standard_kline / resample_bars 要求 8 列含 amount;
        # 不同数据源 (fetch_minute_single index 实时拉取) 可能缺 amount 列 → 补 0
        import polars as pl
        if "amount" not in df_1m.columns:
            df_1m = df_1m.with_columns(pl.lit(0.0).alias("amount"))

        if freq != "1分钟":
            import pandas as pd
            from czsc import resample_bars
            pdf = df_1m.rename({"datetime": "dt", "volume": "vol"}).to_pandas()
            pdf["dt"] = pd.to_datetime(pdf["dt"])
            bars = resample_bars(pdf, target_freq=freq, base_freq="1分钟", raw_bars=True)
        else:
            bars = _df_to_bars(df_1m, "1分钟")
    else:
        raise ValueError(f"未知频率族: {cfg.family}")

    if not bars:
        return _empty_result(symbol, freq)

    # CZSC 计算
    c = CZSC(bars)

    # 信号生成
    sig_names = signals if signals else DEFAULT_SIGNALS
    signals_config = _build_signals_config(sig_names, cfg.freq_str, signal_params)
    signals_result = generate_czsc_signals(bars, signals_config, init_n=cfg.init_n)

    # 序列化 (传原始 bars 给图表, czsc 的 c.bars_raw 是包含处理后的会丢首尾 bars)
    return _serialize(c, signals_result, symbol, freq, bars)


def _empty_result(symbol: str, freq: str, message: str = "") -> dict:
    """构造空结果 (数据不足时的降级响应)。"""
    result = {
        "available": True,
        "symbol": symbol,
        "freq": freq,
        "bars": [],
        "fx_list": [],
        "bi_list": [],
        "zs_list": [],
        "signals": [],
        "signal_markers": [],
    }
    if message:
        result["message"] = message
    return result


# ---------------------------------------------------------------------------
# polars 日K → 周/月/季 K (group_by_dynamic)
# ---------------------------------------------------------------------------
def _resample_daily(df, freq_str: str):
    """polars 日K → 周/月/季 K。

    聚合: open=first, close=last, high=max, low=min, volume=sum, amount=sum
    date 取每桶首日。

    周线用 group_by_dynamic(every="1w", start_by="monday")；
    月线/季线用 dt.year/dt.month/dt.quarter 分组 (group_by_dynamic 的 "1mo" 不按自然月分桶)。
    """
    import polars as pl

    df = df.sort("date")

    if freq_str == "周线":
        # group_by_dynamic 的分桶边界列即 date, 自动取每桶起始日, 不需在 agg 中再 alias date
        out = (
            df.group_by_dynamic("date", every="1w", start_by="monday")
            .agg(
                pl.col("symbol").first(),
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                pl.col("volume").sum(),
                pl.col("amount").sum(),
            )
            .drop_nulls("open")
        )
        return out

    # 月线/季线: group_by_dynamic 的 "1mo" 不按自然月分桶, 改用 year/month/quarter 分组
    agg_exprs = [
        pl.col("date").min().alias("date"),
        pl.col("symbol").first(),
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
        pl.col("amount").sum(),
    ]

    if freq_str == "月线":
        df2 = df.with_columns(
            _year=pl.col("date").dt.year(),
            _month=pl.col("date").dt.month(),
        )
        out = (
            df2.group_by(["_year", "_month"])
            .agg(agg_exprs)
            .sort("date")
            .drop(["_year", "_month"])
        )
        return out

    if freq_str == "季线":
        df2 = df.with_columns(
            _year=pl.col("date").dt.year(),
            _quarter=pl.col("date").dt.quarter(),
        )
        out = (
            df2.group_by(["_year", "_quarter"])
            .agg(agg_exprs)
            .sort("date")
            .drop(["_year", "_quarter"])
        )
        return out

    raise ValueError(f"_resample_daily 不支持频率: {freq_str}")


# ---------------------------------------------------------------------------
# 分钟族取数
# ---------------------------------------------------------------------------
def _fetch_minute_series(repo, asset_type: str, symbol: str, days: int):
    """取 N 天 1 分钟 K。

    stock/etf: 本地优先 (get_minute_range 读持久化 parquet) → 缺失交易日实时补拉
               (fetch_minute_single, 不落库), 与 /api/kline/minute 降级模式对齐。
    index:     无持久化, 逐日 fetch_minute_single 实时拉取拼接。
    """
    import polars as pl
    from app.services import kline_sync
    end = date.today()
    start = end - timedelta(days=days * 2)

    # 从日K推导预期交易日列表 (stock/etf/index 都有日K)
    daily_df = repo.get_daily_asset(asset_type, symbol, start, end)
    if daily_df.is_empty() or "date" not in daily_df.columns:
        # 无日K无法推导交易日 → stock/etf 回退纯本地读, index 返回空
        if asset_type in ("stock", "etf"):
            df = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
            return df.filter(pl.col("symbol") == symbol).sort("datetime") if not df.is_empty() else df
        return pl.DataFrame()
    trade_days = [d if isinstance(d, date) else date.fromisoformat(str(d))
                  for d in daily_df["date"].to_list()][-days:]

    if asset_type in ("stock", "etf"):
        # 本地优先: 读持久化 parquet
        df_local = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
        if not df_local.is_empty():
            df_local = df_local.filter(pl.col("symbol") == symbol).sort("datetime")
        # 找出本地缺失的交易日 → 实时补拉 (不落库)
        local_days = (set(df_local["datetime"].dt.date().unique().to_list())
                      if not df_local.is_empty() else set())
        missing_days = [d for d in trade_days if d not in local_days]
        if not missing_days:
            return df_local
        parts = [df_local] if not df_local.is_empty() else []
        for d in missing_days:
            try:
                sub = kline_sync.fetch_minute_single(symbol, d, asset_type=asset_type)
                if not sub.is_empty():
                    parts.append(sub)
            except Exception:  # noqa: BLE001
                logger.warning("minute fetch failed %s %s", symbol, d, exc_info=True)
        if not parts:
            return pl.DataFrame()
        return pl.concat(parts, how="diagonal_relaxed").sort("datetime")

    # index: 逐日实时拉取 (无持久化)
    parts = []
    for d in trade_days:
        try:
            sub = kline_sync.fetch_minute_single(symbol, d, asset_type="index")
            if not sub.is_empty():
                parts.append(sub)
        except Exception:  # noqa: BLE001
            logger.warning("index minute fetch failed %s %s", symbol, d, exc_info=True)
    return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()


# ---------------------------------------------------------------------------
# polars → czsc bars 转换
# ---------------------------------------------------------------------------
def _df_to_bars(df, freq_str: str) -> list:
    """tickflow polars DF → czsc RawBar 列表。

    转换步骤: select 8 列 → to_pandas → rename {date→dt, volume→vol}
    → pd.to_datetime(dt) → format_standard_kline(freq=freq_str)
    """
    import pandas as pd
    from czsc import format_standard_kline

    pdf = df.select(["date", "symbol", "open", "high", "low", "close", "volume", "amount"]).to_pandas()
    pdf = pdf.rename(columns={"date": "dt", "volume": "vol"})
    pdf["dt"] = pd.to_datetime(pdf["dt"])
    bars = format_standard_kline(pdf, freq=freq_str)
    return bars


# ---------------------------------------------------------------------------
# 信号配置构建
# ---------------------------------------------------------------------------
def _build_signals_config(
    signal_names: list[str],
    freq_str: str,
    signal_params: dict[str, dict] | None = None,
) -> list[dict]:
    """信号名列表 + freq_str → czsc signals_config 格式 [{name, freq, ...params}]。

    signal_params: 可选 {信号名: {参数名: 值}}, 合并到对应信号 config (参数透传)。
    不传则只含 name+freq, czsc 信号函数内部用默认参数 (如 di=1)。
    """
    signal_params = signal_params or {}
    return [{"name": n, "freq": freq_str, **signal_params.get(n, {})} for n in signal_names]


# ---------------------------------------------------------------------------
# 信号目录
# ---------------------------------------------------------------------------
def list_signals() -> dict:
    """返回 czsc 全信号目录 (按 namespace 分组)。

    调 czsc._native.list_all_signals(include_kline=True, include_trader=False)，
    只取 kline 类信号 (trader 类不能跑)。
    czsc 未装时返回 {available: false, groups: {}, total: 0}。
    """
    if not is_available():
        return {"available": False, "groups": {}, "total": 0}

    import czsc._native as native
    items = native.list_all_signals(include_kline=True, include_trader=False)

    groups: dict[str, list] = {}
    for it in items:
        if it["name"] not in SIGNAL_WHITELIST:
            continue
        ns = it.get("namespace", "other")
        label = NAMESPACE_LABEL.get(ns, ns)
        param_template = it.get("param_template", "")
        groups.setdefault(label, []).append({
            "name": it["name"],
            "category": it.get("category", ""),
            "namespace": ns,
            "param_template": param_template,
            "desc": _parse_signal_desc(it["name"], param_template),
            "is_bs": _is_buy_sell_signal(it["name"]),
            "default_params": _parse_default_params(param_template),
        })

    total = sum(len(v) for v in groups.values())
    return {"available": True, "groups": groups, "total": total}


def _parse_default_params(param_template: str) -> dict:
    """从 param_template 提取占位符默认值 (供前端展示/参数透传)。

    模板形如 "{freq}_D{di}B_BUY1V230102": {freq} 由 freq_str 提供 (不入 params),
    {di}→1。常见占位符默认: di=1, n=20, timeperiod=5, ma_type="SMA"。
    """
    import re
    defaults = {"di": 1, "n": 20, "timeperiod": 5, "ma_type": "SMA"}
    params: dict = {}
    for m in re.finditer(r"\{(\w+)\}", param_template):
        key = m.group(1)
        if key == "freq":
            continue
        if key in defaults:
            params[key] = defaults[key]
    return params


def _parse_signal_desc(name: str, param_template: str) -> str:
    """从信号名 + param_template 解析中文可读描述。

    list_all_signals 无 desc 字段。中文描述来源优先级:
      1. _SIGNAL_CN_PREFIX 按信号名前缀映射 (买卖点等关键信号, 模板为纯英文/混合)
      2. 从 param_template 提取中文字符序列拼接 (多数 cxt 模板含中文如"表里关系"/"分型强弱")
      3. 提取末尾 V 前的英文片段 (兜底)
      4. 空字符串
    """
    # 1. 前缀映射
    for prefix, cn in _SIGNAL_CN_PREFIX:
        if name.startswith(prefix):
            return cn
    # 2. 提取中文片段
    import re
    cn = "".join(re.findall(r"[\u4e00-\u9fff]+", param_template))
    if cn:
        return cn
    # 3. 末尾 V 前的英文 token (如 ADTM)
    m = re.search(r"([A-Za-z][A-Za-z0-9]*?)V?\d*$", param_template)
    return m.group(1) if m else ""


# 关键信号中文名 (按信号名前缀匹配; 这些模板为纯英文或"辅助"类, 中文提取不够清晰)
# 顺序敏感: 更长/更具体的前缀放前面。
_SIGNAL_CN_PREFIX: list[tuple[str, str]] = [
    ("cxt_first_buy", "一买"),
    ("cxt_first_sell", "一卖"),
    ("cxt_second_bs", "二类买卖点"),
    ("cxt_third_bs", "三类买卖点"),
    ("cxt_third_buy", "三类买点"),
    ("cxt_double_zs", "双中枢一类买卖点"),
    ("cxt_bi_status", "笔表里关系"),
    ("cxt_bi_base", "笔基础状态"),
    ("cxt_bi_end", "笔结束辅助"),
    ("cxt_bi_stop", "笔止损距离"),
    ("cxt_bi_trend", "笔趋势形态"),
    ("cxt_bi_zdf", "笔涨跌幅分层"),
    ("cxt_bs", "买卖点辅助"),
    ("cxt_decision", "决策区域"),
    ("cxt_fx_power", "分型强弱"),
    ("cxt_overlap", "支撑压力"),
    ("cxt_range_oscillation", "区间震荡"),
    ("cxt_three_bi", "三笔形态"),
    ("cxt_five_bi", "五笔形态"),
    ("cxt_seven_bi", "七笔形态"),
    ("cxt_nine_bi", "九笔形态"),
    ("cxt_eleven_bi", "十一笔形态"),
    ("cxt_ubi_end", "UBI笔结束辅助"),
]


# 买卖点信号名前缀 (这些信号触发时会在 K 线上显示买卖标记)。
# 供 list_signals 标注 is_bs, 让前端区分"会产生K线标记"与"仅结构状态"的信号。
_BS_SIGNAL_PREFIXES = (
    "cxt_first_buy", "cxt_first_sell",
    "cxt_second_bs", "cxt_third_bs", "cxt_third_buy",
    "cxt_double_zs",
)


def _is_buy_sell_signal(name: str) -> bool:
    """是否为买卖点信号 (触发时 value 以 一买/二买/三买/一卖/二卖/三卖 开头 → 生成 K 线标记)。"""
    return name.startswith(_BS_SIGNAL_PREFIXES)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
_MARK_MAP = {"顶分型": "top", "底分型": "bottom"}
_DIR_MAP = {"向上": "up", "向下": "down"}


def _fmt_dt(ts, minute: bool = False) -> str:
    """将 pandas Timestamp / datetime / ISO 字符串统一转为日期字符串。

    minute=False → 'YYYY-MM-DD' (日线族)
    minute=True  → 'YYYY-MM-DD HH:MM' (分钟族)
    """
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts[:16] if minute else ts[:10]
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M") if minute else ts.strftime("%Y-%m-%d")
    s = str(ts)
    return s[:16] if minute else s[:10]


def _serialize(c, signals_result: list[dict], symbol: str, freq: str = "日线",
               bars: list | None = None) -> dict:
    """CZSC 对象 + 信号结果 → JSON 可序列化 dict。

    字段名映射: 顶分型→top, 底分型→bottom, 向上→up, 向下→down
    日期格式由 freq 决定: 分钟族 %Y-%m-%d %H:%M, 日线族 %Y-%m-%d。

    bars: 原始输入 K 线 (用于图表 K 线序列)。czsc 的 c.bars_raw 是 K 线包含处理
    (include processing) 后的 NewBars, 会合并被包含的 K 线 → 根数少于输入,
    首尾可能丢 bars。图表 K 线应用原始 bars, fx/bi/zs 仍用 czsc 处理结果。
    """
    minute = FREQ_CONFIG.get(freq, FREQ_CONFIG["日线"]).family == "minute"
    chart_bars = bars if bars is not None else c.bars_raw

    # bars 序列化 (用原始输入 bars, 非 czsc 处理后的 bars_raw)
    bars_out = []
    for bar in chart_bars:
        bars_out.append({
            "date": _fmt_dt(bar.dt, minute),
            "open": round(float(bar.open), 4),
            "high": round(float(bar.high), 4),
            "low": round(float(bar.low), 4),
            "close": round(float(bar.close), 4),
            "volume": float(bar.vol) if bar.vol is not None else 0,
        })

    # 分型序列化
    fx_out = []
    for fx in c.fx_list:
        # 分型确认时间 = 构成分型的第 3 根 K 线 (new_bars[2]) 的 dt
        # 分型有滞后: 极值点 fx.dt 是中间 K 线, 但分型在第 3 根 K 线才确认成立
        confirm_dt = _fmt_dt(fx.dt, minute)
        try:
            nb = fx.new_bars
            if nb and len(nb) >= 3:
                confirm_dt = _fmt_dt(nb[2].dt, minute)
        except Exception:  # noqa: BLE001
            logger.debug("fx new_bars unavailable, fallback confirm_dt=fx.dt", exc_info=True)
        try:
            power = fx.power_str  # 分型强度: 强/中/弱 (czsc FX 内置判定)
        except Exception:  # noqa: BLE001
            power = ""
        fx_out.append({
            "dt": _fmt_dt(fx.dt, minute),
            "confirm_dt": confirm_dt,
            "price": round(float(fx.fx), 4),
            "mark": _MARK_MAP.get(fx.mark.value, fx.mark.value),
            "power": power,
        })

    # 笔序列化
    bi_out = []
    for bi in c.bi_list:
        bi_out.append({
            "a_dt": _fmt_dt(bi.fx_a.dt, minute),
            "a_price": round(float(bi.fx_a.fx), 4),
            "b_dt": _fmt_dt(bi.fx_b.dt, minute),
            "b_price": round(float(bi.fx_b.fx), 4),
            "direction": _DIR_MAP.get(bi.direction.value, bi.direction.value),
        })

    # 中枢: 用 czsc 官方 ZS 对象 (前3笔重叠算法, 含 gg/dd/zz/is_valid), 从 bi_list 识别
    zs_out = _extract_zs_from_bis(c.bi_list, minute)

    # 信号序列化 (透传 list[dict], dt 格式化, 过滤掉 bar 原始数据字段)
    signals_out = []
    bar_keys = {"dt", "close", "open", "high", "low", "vol", "amount", "symbol", "freq", "id"}
    for sig in signals_result:
        entry: dict[str, Any] = {"dt": _fmt_dt(sig.get("dt"), minute)}
        for k, v in sig.items():
            if k in bar_keys:
                continue
            entry[k] = v
        signals_out.append(entry)

    # 买卖标记 (用原始 bars 建 dt→close 映射, 非 czsc 处理后的 bars_raw)
    signal_markers = _extract_signal_markers(signals_result, chart_bars, minute)
    # 追加缠论买卖点 (结构法: 一买/一卖/二买/二卖, 独立于信号体系)
    signal_markers = sorted(
        signal_markers + _detect_chanlun_bs(c, chart_bars, minute),
        key=lambda m: m["dt"],
    )

    return {
        "available": True,
        "symbol": symbol,
        "freq": freq,
        "bars": bars_out,
        "fx_list": fx_out,
        "bi_list": bi_out,
        "zs_list": zs_out,
        "signals": signals_out,
        "signal_markers": signal_markers,
    }


# ---------------------------------------------------------------------------
# 中枢提取 (用 czsc 官方 ZS 对象, 从 bi_list 识别)
# ---------------------------------------------------------------------------
def _extract_zs_from_bis(bi_list, minute: bool = False) -> list:
    """从笔列表识别中枢, 用 czsc 官方 ZS 对象计算区间。

    CZSC 对象只算分型/笔, 不提供 zs_list; 中枢需从 bi_list 识别:
      遍历连续3笔, 用官方 ZS(bis) 构造 → zg=min(前3笔高), zd=max(前3笔低),
      zz=中轴, gg/dd=全段极值; is_valid() 判定有效性。有效则记录并跳过3笔。

    相比旧版 (第2/3笔重叠自算), 改用官方 ZS::new 算法 + is_valid, 区间更准。
    """
    if not bi_list or len(bi_list) < 3:
        return []
    from czsc import ZS
    out = []
    try:
        i = 0
        while i <= len(bi_list) - 3:
            tri = list(bi_list[i:i + 3])
            zs = ZS(tri)
            if zs.is_valid():
                out.append({
                    "sdt": _fmt_dt(zs.sdt, minute),
                    "edt": _fmt_dt(zs.edt, minute),
                    "zd": round(float(zs.zd), 4),
                    "zg": round(float(zs.zg), 4),
                    "zz": round(float(zs.zz), 4),
                    "gg": round(float(zs.gg), 4),
                    "dd": round(float(zs.dd), 4),
                })
                i += 3
            else:
                i += 1
    except Exception:  # noqa: BLE001
        logger.debug("ZS extraction failed, returning empty list", exc_info=True)
    return out


# ---------------------------------------------------------------------------
# 信号 → 买卖标记提取 (value 驱动)
# ---------------------------------------------------------------------------
_BS_VALUE_PREFIX = {
    "一买": ("buy", "一类买点"), "一卖": ("sell", "一类卖点"),
    "二买": ("buy", "二类买点"), "二卖": ("sell", "二类卖点"),
    "三买": ("buy", "三类买点"), "三卖": ("sell", "三类卖点"),
    # 类二买/类二卖: cxt_nine_bi_V230621 形态 value 以"类"开头, 需单独映射才画 marker
    "类二买": ("buy", "二类买点"), "类二卖": ("sell", "二类卖点"),
}

# 买卖点优先级 (数值越小越高): 同日同向多 marker 取最高优先级 label
_BS_PRIORITY = {
    "一类买点": 1, "二类买点": 2, "三类买点": 3,
    "一类卖点": 1, "二类卖点": 2, "三类卖点": 3,
}


def _detect_raw_markers(signals_result: list[dict], bars, minute: bool = False) -> list[dict]:
    """per-key 状态切换检测 → raw markers (未做全局去重)。

    czsc 的买卖点信号 (cxt_first_buy/second_bs/third_bs 等) 是**状态式**信号:
    只要当前笔结构满足条件, 该 bar 的 signal value 就一直是 "二买" / "三卖" 等,
    直到结构变化才转为 "其他"。因此同一状态会连续出现在多根 K 线上, 不去重会刷屏。

    标记规则:
      - 遍历每 bar 的信号 dict 每个 (sig_key, value) 键值对
      - value 不以「其他」开头且以 一买/二买/三买/一卖/二卖/三卖 开头 → 判定为「活跃」
      - 仅在 (sig_key) 上一个 bar 的状态为非活跃 (或不同前缀) 时输出 marker
        → 同一 sig_key 下连续 N 根「二买」只输出第一个 bar, 避免刷屏
      - kind/label 由前缀映射 (_BS_VALUE_PREFIX)
      - marker 取该 bar 的 close 作 price

    注意: 本函数仅做 per-key 去重; 多 sig_key 同日触发 / 连续同向由 _dedupe_markers 处理。
    """
    markers = []

    # 构建 dt → close 映射 (bars 的 dt 可能是 naive Timestamp)
    dt_close: dict[str, float] = {}
    for bar in bars:
        dt_key = _fmt_dt(bar.dt, minute)
        dt_close[dt_key] = float(bar.close)

    # 每根 bar 的信号 dict 中, dt/close/open 等是 bar 原始字段, 不是信号 key
    bar_keys = {"dt", "close", "open", "high", "low", "vol", "amount", "symbol", "freq", "id"}

    # 每个信号 key 的上一个 bar 状态前缀 (None = 非活跃/其他)
    prev_prefix: dict[str, str | None] = {}

    for sig in signals_result:
        sig_dt = _fmt_dt(sig.get("dt"), minute)
        price = dt_close.get(sig_dt)

        for sig_key, value in sig.items():
            if sig_key in bar_keys or not isinstance(value, str):
                continue

            # 当前 bar 该 sig_key 的状态前缀
            current_prefix: str | None = None
            for prefix in _BS_VALUE_PREFIX:
                if value.startswith(prefix):
                    current_prefix = prefix
                    break

            # 仅在状态从 非活跃/其他 或 不同前缀 切换到 当前活跃前缀 时输出 marker
            if current_prefix is not None and prev_prefix.get(sig_key) != current_prefix:
                kind, label = _BS_VALUE_PREFIX[current_prefix]
                markers.append({
                    "dt": sig_dt,
                    "kind": kind,
                    "label": label,
                    "price": round(price, 4) if price is not None else None,
                })

            # 始终更新 prev (包括切回 None) → 下次 "其他→二买" 转换才能被识别
            prev_prefix[sig_key] = current_prefix

    return markers


def _extract_signal_markers(signals_result: list[dict], bars, minute: bool = False) -> list[dict]:
    """提取买卖标记 = per-key 状态检测 + 全局去重 (同日合并 + 买卖交替)。

    缠论同级别买卖点不连续: per-key 去重仅保证单信号不刷屏; 多 sig_key 同日触发 /
    连续同向由 _dedupe_markers 全局后处理, 使最终序列买卖严格交替、同日至多一个。
    """
    raw = _detect_raw_markers(signals_result, bars, minute)
    return _dedupe_markers(raw)


def _dedupe_markers(markers: list[dict]) -> list[dict]:
    """对 per-key 去重后的买卖标记做全局后处理, 使其符合缠论「同级别买卖点不连续」语义。

    1. 按 dt 排序
    2. 同日合并: 同向取优先级最高 (一>二>三); 异向冲突 (同日又买又卖) 视为噪声丢弃该日全部
    3. 全局买卖交替: 连续同向段内取优先级最高 (一>二>三); 段间方向必交替 (买后必先卖再买)
    """
    if not markers:
        return []

    # 1. 排序 (稳定, 保留同日原始顺序)
    markers.sort(key=lambda m: m["dt"])

    # 2. 同日合并
    merged: list[dict] = []
    i = 0
    n = len(markers)
    while i < n:
        dt = markers[i]["dt"]
        same_day = [markers[i]]
        j = i + 1
        while j < n and markers[j]["dt"] == dt:
            same_day.append(markers[j])
            j += 1
        i = j
        # 同日又买又卖 → 矛盾, 丢弃
        if len({m["kind"] for m in same_day}) > 1:
            continue
        # 同向多个 → 取优先级最高 (priority 最小)
        merged.append(min(same_day, key=lambda m: _BS_PRIORITY.get(m["label"], 99)))

    # 3. 全局买卖交替: 连续同向段内取优先级最高 (一>二>三), 段间方向必交替
    #    缠论: 同级别买后必先卖再买; 段内若混入更高优先级买点 (如一买), 覆盖较早的二买/三买
    result: list[dict] = []
    seg: list[dict] = []
    seg_kind: str | None = None
    for m in merged:
        if m["kind"] != seg_kind:
            if seg:
                result.append(min(seg, key=lambda x: _BS_PRIORITY.get(x["label"], 99)))
            seg = [m]
            seg_kind = m["kind"]
        else:
            seg.append(m)
    if seg:
        result.append(min(seg, key=lambda x: _BS_PRIORITY.get(x["label"], 99)))
    return result


# ---------------------------------------------------------------------------
# 缠论买卖点检测 (结构法, 自包含): 一买/一卖/二买/二卖
# ---------------------------------------------------------------------------
# 设计依据 (缠中说禅):
#   一买: 下跌趋势末端, 创阶段新低且动能衰竭(背驰) → 趋势底背驰点
#   一卖: 上涨趋势末端, 创阶段新高且动能衰竭(背驰) → 趋势顶背驰点
#   二买: 一买后第一次回调(向下笔)低点不破一买低点 (确定性高于一买)
#   二卖: 一卖后第一次反弹(向上笔)高点不破一卖高点
#
# 与 czsc check_first_buy 的关键区别:
#   czsc 要求"窗口首笔是全段最高/最低"(标准单边趋势), 震荡市一买几乎不触发
#   (000001.SH 一类买点 0 次 / 一类卖点 6 次, 明显不对称失真);
#   本实现改为"相对前一同向笔创新高/新低 + 背驰", 买卖对称且贴合实际市场。
# ---------------------------------------------------------------------------

def _fx_confirm_dt(fx, minute: bool) -> str:
    """分型确认时间 = 构成分型的第3根K线 (new_bars[2]) 的 dt。

    分型有滞后: 极值点 fx.dt 是中间K线, 分型在第3根K线才确认成立。
    与 _serialize 分型 confirm_dt 同逻辑; new_bars 不可用时 fallback fx.dt。
    """
    try:
        nb = fx.new_bars
        if nb and len(nb) >= 3:
            return _fmt_dt(nb[2].dt, minute)
    except Exception:  # noqa: BLE001
        pass
    return _fmt_dt(fx.dt, minute)


def _bc_fade(last, prev) -> bool:
    """背驰判定: 末笔相对前一同向笔价格力度衰竭 (power_price 减小)。

    缠论背驰以"价格动能衰竭"为准 (czsc 要求 量/长 至少一项同步, 会漏掉
    阴跌背驰/放量V反等合理底, 如 000001.SH bi[16] 价缩放量); 量/长仅辅助, 不作门槛。
    """
    return last.power_price < prev.power_price


def _is_first_buy(bis, j: int) -> bool:
    """一买: 下跌趋势末端背驰点 (创新低 + 价格力度衰竭)。

    缠论一买 = 趋势底背驰。笔级别判定 (与一卖对称):
      1. 创新低显著: low 跌破前一同向下跌笔低点 (双底锚定容忍 1% 区间)
      2. 价格背驰: power_price 相对前一同向笔衰竭
    不要求"全图最低"(czsc check_first_buy 过严, 震荡市0触发)。
    """
    from czsc import Direction
    if bis[j].direction != Direction.Down or j < 2:
        return False
    # 新低显著性: 跌破前一同向笔低点 (趋势延续) 或 双底锚定 (低点不破前低 +3% 区间)
    if bis[j].low > bis[j - 2].low * 1.03:
        return False
    return _bc_fade(bis[j], bis[j - 2])


def _is_first_sell(bis, j: int) -> bool:
    """一卖: 上涨趋势末端背驰点 (创新高 + 价格力度衰竭)。与一买对称。"""
    from czsc import Direction
    if bis[j].direction != Direction.Up or j < 2:
        return False
    # 新高显著性: 突破前一同向笔高点 (趋势延续) 或 双顶锚定 (高点不破前高 -3% 区间)
    if bis[j].high < bis[j - 2].high * 0.97:
        return False
    return _bc_fade(bis[j], bis[j - 2])


def _find_zhongshu(bis) -> list[dict]:
    """识别缠论中枢 (滑动窗口: 连续3笔重叠区间)。

    中枢: 连续3笔的价格重叠区, ZG=min(3笔high)/ZD=max(3笔low) (ZG>ZD 才有效)。
    滑动窗口识别所有候选中枢; 重叠中枢会对同一三买卖重复触发, 由调用方去重。
    不处理中枢延伸 (延伸使边界模糊, 三买卖判定反而错位), 用固定3笔窗口边界清晰。

    Returns:
        [{a, b, zg, zd}] (b=a+2)
    """
    zs = []
    for i in range(len(bis) - 2):
        zg = min(bis[i].high, bis[i + 1].high, bis[i + 2].high)
        zd = max(bis[i].low, bis[i + 1].low, bis[i + 2].low)
        if zg > zd:
            zs.append({"a": i, "b": i + 2, "zg": zg, "zd": zd})
    return zs


def _detect_chanlun_bs(c, chart_bars, minute: bool) -> list[dict]:
    """缠论买卖点检测 (结构法, 自包含, 不依赖信号勾选): 一买/一卖/二买/二卖/三买/三卖。

    Returns:
        标记 [{dt, confirm_dt, kind, label, price}], label ∈ 一/二/三类买点|卖点。
        confirm_dt = 锚定分型 fx_b 第3根K线确认时刻 (分型滞后2根K线), 与分型 confirm_dt 同逻辑。
        回看批量判定, 历史与当前统一; 与 czsc 信号体系解耦。
    """
    from czsc import Direction
    bis = c.bi_list
    if len(bis) < 5:
        return []
    dt_close = {_fmt_dt(b.dt, minute): float(b.close) for b in chart_bars}
    out = []
    n = len(bis)

    def mk(j: int, kind: str, label: str, price: float) -> dict:
        fx_b = bis[j].fx_b
        dt = _fmt_dt(fx_b.dt, minute)
        # 确认时间: 该买卖点锚定的分型 fx_b 第3根K线确认时刻 (分型滞后2根K线)
        return {"dt": dt, "confirm_dt": _fx_confirm_dt(fx_b, minute),
                "kind": kind, "label": label,
                "price": round(dt_close.get(dt, price), 4)}

    for j in range(n):
        if _is_first_buy(bis, j):
            out.append(mk(j, "buy", "一类买点", bis[j].low))
            # 二买: 一买后第一次回调(向下笔)低点不破一买低点
            k = j + 2
            if k < n and bis[k].direction == Direction.Down and bis[k].low >= bis[j].low:
                out.append(mk(k, "buy", "二类买点", bis[k].low))
        elif _is_first_sell(bis, j):
            out.append(mk(j, "sell", "一类卖点", bis[j].high))
            # 二卖: 一卖后第一次反弹(向上笔)高点不破一卖高点
            k = j + 2
            if k < n and bis[k].direction == Direction.Up and bis[k].high <= bis[j].high:
                out.append(mk(k, "sell", "二类卖点", bis[k].high))

    # 三买/三卖: 中枢突破后回抽不破 (缠中说禅: 离开段突破中枢, 回抽不入中枢)
    for zs in _find_zhongshu(bis):
        b, zg, zd = zs["b"], zs["zg"], zs["zd"]
        # 离开段可以是中枢最后一笔(本身突破) 或 中枢后第一笔, 取第一个有效
        for leave_idx in (b, b + 1):
            if leave_idx >= n:
                break
            back_idx = leave_idx + 1
            if back_idx >= n:
                break
            leave, back = bis[leave_idx], bis[back_idx]
            # 三买: 向上突破 zg + 回抽(向下笔)低点不破 zg
            if (leave.direction == Direction.Up and leave.high > zg
                    and back.direction == Direction.Down and back.low >= zg):
                out.append(mk(back_idx, "buy", "三类买点", back.low))
                break
            # 三卖: 向下突破 zd + 回抽(向上笔)高点不破 zd
            if (leave.direction == Direction.Down and leave.low < zd
                    and back.direction == Direction.Up and back.high <= zd):
                out.append(mk(back_idx, "sell", "三类卖点", back.high))
                break

    # 去重: 同 dt 同 kind 取优先级最高 (一类>二类>三类), 按 dt 排序
    best: dict = {}
    for m in out:
        key = (m["dt"], m["kind"])
        if key not in best or _BS_PRIORITY.get(m["label"], 99) < _BS_PRIORITY.get(best[key]["label"], 99):
            best[key] = m
    return sorted(best.values(), key=lambda m: m["dt"])
