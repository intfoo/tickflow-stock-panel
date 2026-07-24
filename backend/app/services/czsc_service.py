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
        default_days: days 不传时的默认取数窗口
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


# ---------------------------------------------------------------------------
# 默认推荐信号 + namespace 中文映射
# ---------------------------------------------------------------------------
DEFAULT_SIGNALS: list[str] = [
    "cxt_bi_status_V230102",       # 笔状态
    "cxt_first_buy_V221126",       # 一买
    "cxt_first_sell_V221126",      # 一卖
    "cxt_second_bs_V230320",       # 二类买卖点
    "cxt_third_bs_V230318",        # 三类买卖点
    "cxt_bi_base_V230228",         # 笔基础状态
]

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
             signals: list[str] | None = None) -> dict:
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
        start = end - timedelta(days=days * 2)
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
    signals_config = _build_signals_config(sig_names, cfg.freq_str)
    signals_result = generate_czsc_signals(bars, signals_config, init_n=cfg.init_n)

    # 序列化
    return _serialize(c, signals_result, symbol, freq)


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
def _build_signals_config(signal_names: list[str], freq_str: str) -> list[dict]:
    """信号名列表 + freq_str → czsc signals_config 格式 [{name, freq}]。"""
    return [{"name": n, "freq": freq_str} for n in signal_names]


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
        ns = it.get("namespace", "other")
        label = NAMESPACE_LABEL.get(ns, ns)
        groups.setdefault(label, []).append({
            "name": it["name"],
            "category": it.get("category", ""),
            "namespace": ns,
            "param_template": it.get("param_template", ""),
            "desc": _parse_signal_desc(it.get("param_template", "")),
        })

    return {"available": True, "groups": groups, "total": len(items)}


def _parse_signal_desc(param_template: str) -> str:
    """从 param_template 解析可读描述 (如 BUY1/SELL1/BS2辅助)。

    list_all_signals 无 desc 字段, 从 param_template 末尾可读片段解析。
    解析不到则返回空字符串。
    """
    import re
    m = re.search(r"(BUY\d|SELL\d|BS\d辅助|第[二三四]买卖点|[一二三]买辅助)", param_template)
    return m.group(1) if m else ""


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


def _serialize(c, signals_result: list[dict], symbol: str, freq: str = "日线") -> dict:
    """CZSC 对象 + 信号结果 → JSON 可序列化 dict。

    字段名映射: 顶分型→top, 底分型→bottom, 向上→up, 向下→down
    日期格式由 freq 决定: 分钟族 %Y-%m-%d %H:%M, 日线族 %Y-%m-%d。
    """
    minute = FREQ_CONFIG.get(freq, FREQ_CONFIG["日线"]).family == "minute"

    # bars 序列化
    bars_out = []
    for bar in c.bars_raw:
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
        fx_out.append({
            "dt": _fmt_dt(fx.dt, minute),
            "price": round(float(fx.fx), 4),
            "mark": _MARK_MAP.get(fx.mark.value, fx.mark.value),
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

    # 中枢 (best-effort)
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

    # 买卖标记
    signal_markers = _extract_signal_markers(signals_result, c.bars_raw, minute)

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
# 中枢提取 (best-effort)
# ---------------------------------------------------------------------------
def _extract_zs_from_bis(bi_list, minute: bool = False) -> list:
    """从笔列表中提取中枢 (best-effort)。

    算法: 遍历连续3笔, 第2、3笔价格区间重叠即为中枢。
    中枢区间 [zd, zg] = [max(第2笔低点, 第3笔低点), min(第2笔高点, 第3笔高点)]
    若 zd < zg 则重叠区有效。

    若提取复杂或失败, 返回 [] (设计文档允许降级)。
    """
    if not bi_list or len(bi_list) < 3:
        return []

    zs_list = []
    try:
        i = 0
        while i < len(bi_list) - 2:
            bi1 = bi_list[i + 1]  # 第2笔
            bi2 = bi_list[i + 2]  # 第3笔

            # 每笔的端点: fx_a 和 fx_b, 取价格区间
            low1 = min(bi1.fx_a.fx, bi1.fx_b.fx)
            high1 = max(bi1.fx_a.fx, bi1.fx_b.fx)
            low2 = min(bi2.fx_a.fx, bi2.fx_b.fx)
            high2 = max(bi2.fx_a.fx, bi2.fx_b.fx)

            zd = max(low1, low2)  # 中枢下沿
            zg = min(high1, high2)  # 中枢上沿

            if zd < zg:
                # 有效重叠 → 形成中枢
                zs_list.append({
                    "sdt": _fmt_dt(bi1.fx_a.dt, minute),
                    "edt": _fmt_dt(bi2.fx_b.dt, minute),
                    "zd": round(float(zd), 4),
                    "zg": round(float(zg), 4),
                })
                # 跳到中枢之后继续找
                i += 3
            else:
                i += 1
    except Exception:  # noqa: BLE001
        logger.debug("ZS extraction failed, returning empty list", exc_info=True)
        return []

    return zs_list


# ---------------------------------------------------------------------------
# 信号 → 买卖标记提取 (value 驱动)
# ---------------------------------------------------------------------------
_BS_VALUE_PREFIX = {
    "一买": ("buy", "一类买点"), "一卖": ("sell", "一类卖点"),
    "二买": ("buy", "二类买点"), "二卖": ("sell", "二类卖点"),
    "三买": ("buy", "三类买点"), "三卖": ("sell", "三类卖点"),
}


def _extract_signal_markers(signals_result: list[dict], bars, minute: bool = False) -> list:
    """从信号 dict list 提取买卖标记 (value 驱动)。

    规则:
      - 遍历每 bar 信号 dict 的所有 string value
      - value 不含「其他」且以 一买/二买/三买/一卖/二卖/三卖 开头 → 生成 marker
      - kind/label 由前缀映射 (_BS_VALUE_PREFIX)
      - marker 取该 bar 的 close 作 price, dt 格式化
      - 一个 bar 可能产生多个 marker (不同信号同时触发), 不去重
    """
    markers = []

    # 构建 dt → close 映射 (bars 的 dt 可能是 naive Timestamp)
    dt_close: dict[str, float] = {}
    for bar in bars:
        dt_key = _fmt_dt(bar.dt, minute)
        dt_close[dt_key] = float(bar.close)

    for sig in signals_result:
        sig_dt = _fmt_dt(sig.get("dt"), minute)
        price = dt_close.get(sig_dt)

        for value in sig.values():
            if not isinstance(value, str) or "其他" in value:
                continue
            for prefix, (kind, label) in _BS_VALUE_PREFIX.items():
                if value.startswith(prefix):
                    markers.append({
                        "dt": sig_dt,
                        "kind": kind,
                        "label": label,
                        "price": round(price, 4) if price is not None else None,
                    })
                    break  # 一个 value 只匹配一个前缀

    return markers
