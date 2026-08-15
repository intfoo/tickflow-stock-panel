"""ETF 资金/份额业务编排: 净流入计算、排行榜聚合、资金流序列。

口径基准: docs/superpowers/specs/2026-08-15-etf-market-page-design.md §5.2
- 涨幅四个窗口共用同一 close 序列 (kline_etf_enriched, 前复权, 含 live bar)
- market_cap = share_ffill x nav_ffill (同日期对齐), 万元→亿元
- 资金流尾部 2 个数据日不补 0 (深市 T+1 尾部缺失)
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import polars as pl

from app.config import settings
from app.services import etf_fund_store as store

logger = logging.getLogger(__name__)

_SORTABLE = {
    "change_pct", "change_pct_5d", "change_pct_20d", "change_pct_60d",
    "share", "inflow_1d", "inflow_5d", "inflow_20d", "inflow_60d",
    "amount", "market_cap",
}
_LOOKBACKS = {"change_pct_5d": 5, "change_pct_20d": 20, "change_pct_60d": 60}
_INFLOW_WINDOWS = {"inflow_1d": 1, "inflow_5d": 5, "inflow_20d": 20, "inflow_60d": 60}
_ENRICHED_SCAN_DAYS = 130  # 分区数, 覆盖 60 交易日窗口 + 余量


def trading_calendar(repo, start: date, end: date) -> list[date]:
    """交易日历: 指数日K(上证指数)优先 → etf enriched scan → 份额/净值自身日期并集。"""
    if repo is not None:
        try:
            df = repo.get_index_daily("000001.SH", start, end, ["date"])
            if not df.is_empty() and "date" in df.columns:
                return sorted(df["date"].to_list())
        except Exception as e:
            logger.debug("index calendar fallback: %s", e)
    try:
        df = _scan_etf_enriched(start, end, ["date"])
        if not df.is_empty():
            return sorted(df["date"].unique().to_list())
    except Exception:
        pass
    dates: set[date] = set()
    for df in (store.read_share(), store.read_nav()):
        if not df.is_empty():
            dates.update(df["trade_date"].to_list())
    return sorted(d for d in dates if start <= d <= end)


def _scan_etf_enriched(start: date, end: date, columns: list[str]) -> pl.DataFrame:
    base = settings.data_dir / "kline_etf_enriched"
    if not base.exists():
        return pl.DataFrame()
    dates = sorted(
        (p.name[5:] for p in base.glob("date=*") if p.is_dir()),
        reverse=True,
    )[:_ENRICHED_SCAN_DAYS]
    files = [base / f"date={d}" / "part.parquet" for d in dates]
    files = [f for f in files if f.exists()]
    if not files:
        return pl.DataFrame()
    lf = pl.scan_parquet(files)
    existing = [c for c in columns if c in lf.collect_schema().names()]
    if not existing:
        return pl.DataFrame()
    return lf.select(existing).collect()


def compute_inflow(share: pl.DataFrame, nav: pl.DataFrame, calendar: list[date]) -> pl.DataFrame:
    """净流入 = 稀疏份额 diff x nav(日历级 ffill 后 join)。单位: 万元。null 不转 0。"""
    if share.is_empty():
        return store.read_inflow()  # 空 schema 表
    cal = pl.DataFrame({"trade_date": calendar})
    codes = share.select("code").unique()
    if nav.is_empty():
        nav_full = codes.join(cal, how="cross").with_columns(
            pl.lit(None, dtype=pl.Float64).alias("nav"))
    else:
        nav_full = (
            codes.join(cal, how="cross")
            .join(nav, on=["code", "trade_date"], how="left")
            .sort(["code", "trade_date"])
            .with_columns(pl.col("nav").forward_fill().over("code"))
        )
    s = (
        share.sort(["code", "trade_date"])
        .with_columns(pl.col("share").diff().over("code").alias("inflow_share"))
    )
    return (
        s.join(nav_full, on=["code", "trade_date"], how="left")
        .with_columns((pl.col("inflow_share") * pl.col("nav")).alias("inflow_amount"))
        .select(["code", "trade_date", "inflow_share", "inflow_amount"])
    )


def recompute_inflow(repo) -> None:
    """全量重算 inflow 并原子替换 (先 diff 后截断由调用方保证 calendar 前扩)。"""
    share, nav = store.read_share(), store.read_nav()
    if share.is_empty():
        return
    end = date.today()
    start = share["trade_date"].min() - timedelta(days=20)  # 前扩覆盖首个 diff
    cal = trading_calendar(repo, start, end)
    df = compute_inflow(share, nav, cal)
    store.write_inflow(df)


def _load_quotes() -> pl.DataFrame:
    """scan 最近分区 ETF enriched: symbol, date, close, amount。"""
    end = date.today()
    start = end - timedelta(days=_ENRICHED_SCAN_DAYS * 2)
    df = _scan_etf_enriched(start, end, ["symbol", "date", "close", "amount"])
    return df.sort(["symbol", "date"]) if not df.is_empty() else df


def _window_agg(inflow: pl.DataFrame, cal: list[date], days: int) -> pl.DataFrame:
    """每 code 最近 days 个 calendar 交易日的 inflow_amount 合计 (万元)。"""
    if inflow.is_empty() or not cal:
        return pl.DataFrame(schema={"code": pl.Utf8, "total": pl.Float64})
    end = inflow["trade_date"].max()
    upto = [d for d in cal if d <= end]
    start = upto[-days] if len(upto) >= days else (upto[0] if upto else end)
    return (
        inflow.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") <= end))
        .group_by("code")
        .agg(pl.col("inflow_amount").sum().alias("total"))
    )


def leaderboard_rows(repo, broad: set[str], sort: str, order: str,
                     page: int, size: int, broad_only: bool) -> dict:
    quotes = _load_quotes()
    if quotes.is_empty():
        return {"rows": [], "total": 0, "data_date": None}
    data_date = quotes["date"].max()
    # 每 symbol: 最新 bar + 前 1/5/20/60 个交易日前 close (同一序列, 口径自洽)
    agg = quotes.group_by("symbol").agg([
        pl.col("close").last().alias("price"),
        pl.col("amount").last().alias("amount"),
        pl.col("close").slice(-2, 1).first().alias("close_prev"),
        pl.col("close").slice(-6, 1).first().alias("close_5d"),
        pl.col("close").slice(-21, 1).first().alias("close_20d"),
        pl.col("close").slice(-61, 1).first().alias("close_60d"),
    ])
    agg = agg.with_columns([
        (pl.col("price") / pl.col("close_prev") - 1).alias("change_pct"),
        (pl.col("price") / pl.col("close_5d") - 1).alias("change_pct_5d"),
        (pl.col("price") / pl.col("close_20d") - 1).alias("change_pct_20d"),
        (pl.col("price") / pl.col("close_60d") - 1).alias("change_pct_60d"),
    ])
    # 资金侧
    end = date.today()
    cal = trading_calendar(repo, end - timedelta(days=400), end)
    inflow = store.read_inflow()
    share, nav = store.read_share(), store.read_nav()
    fund = agg.select("symbol")
    for col, days in _INFLOW_WINDOWS.items():
        w = _window_agg(inflow, cal, days)
        fund = fund.join(w, left_on="symbol", right_on="code", how="left")
        fund = fund.rename({"total": col})
    # 份额/市值: share/nav 按 (code, trade_date) union 后排序,
    # 两列各自 forward_fill().over("code"), 再 group_by 取最后非 null 的 share 与 nav
    # (两列 ffill 到同一日期=两序列最大日期), 相乘得 market_cap
    if share.is_empty() and nav.is_empty():
        fund = fund.with_columns([
            pl.lit(None, dtype=pl.Float64).alias("share"),
            pl.lit(None, dtype=pl.Float64).alias("nav"),
        ])
    else:
        # union share+nav 的 (code, trade_date)
        share_sel = share.select(["code", "trade_date", "share"]) if not share.is_empty() else None
        nav_sel = nav.select(["code", "trade_date", "nav"]) if not nav.is_empty() else None
        if share_sel is not None and nav_sel is not None:
            combined = share_sel.join(nav_sel, on=["code", "trade_date"], how="full", coalesce=True)
        elif share_sel is not None:
            combined = share_sel.with_columns(pl.lit(None, dtype=pl.Float64).alias("nav"))
        else:
            combined = nav_sel.with_columns(pl.lit(None, dtype=pl.Float64).alias("share"))
        combined = (
            combined.sort(["code", "trade_date"])
            .with_columns([
                pl.col("share").forward_fill().over("code"),
                pl.col("nav").forward_fill().over("code"),
            ])
            .group_by("code", maintain_order=True)
            .agg([
                pl.col("share").last().alias("share"),
                pl.col("nav").last().alias("nav"),
            ])
        )
        fund = fund.join(combined, left_on="symbol", right_on="code", how="left")
    out = agg.join(fund, on="symbol", how="left").with_columns([
        (pl.col("share") / 1e4).alias("share"),          # 万份→亿份
        (pl.col("share") * pl.col("nav") / 1e4).alias("market_cap"),  # 万元→亿元
        (pl.col("amount") / 1e8).alias("amount"),        # 元→亿元
        pl.col("symbol").is_in(list(broad)).alias("is_broad"),
    ])
    for col in _INFLOW_WINDOWS:
        out = out.with_columns(pl.col(col) / 1e4)        # 万元→亿元
    if broad_only:
        out = out.filter(pl.col("is_broad"))
    sort_col = sort if sort in _SORTABLE else "amount"
    out = out.sort(sort_col, descending=(order != "asc"), nulls_last=True)
    total = out.height
    out = out.slice((page - 1) * size, size)
    keep = ["symbol", "price", "change_pct", "change_pct_5d", "change_pct_20d",
            "change_pct_60d", "share", "inflow_1d", "inflow_5d", "inflow_20d",
            "inflow_60d", "amount", "market_cap", "is_broad"]
    return {"rows": out.select(keep).to_dicts(), "total": total,
            "data_date": data_date.isoformat()}


def fund_flow(repo, days: int) -> dict:
    broad = set(store.load_broad())
    inflow = store.read_inflow()
    empty_stats = {"yesterday": None, "d5": None, "d20": None, "d60": None,
                   "data_end_date": None}
    if inflow.is_empty() or not broad:
        return {"series": [], "stats": empty_stats}
    inflow = inflow.filter(pl.col("code").is_in(list(broad)))
    if inflow.is_empty():
        return {"series": [], "stats": empty_stats}
    end = inflow["trade_date"].max()
    start = end - timedelta(days=max(days, 60) * 2 + 30)
    cal = [d for d in trading_calendar(repo, start, end) if d <= end]
    daily = (
        inflow.group_by("trade_date")
        .agg(pl.col("inflow_amount").sum() / 1e4)  # 万元→亿元
    )
    series_df = (
        pl.DataFrame({"trade_date": cal})
        .join(daily, on="trade_date", how="left")
        .with_columns(pl.col("inflow_amount").fill_null(0.0))
        .sort("trade_date")
    )
    if days and series_df.height > days:
        series_df = series_df.tail(days)
    series = [
        {"trade_date": r["trade_date"].isoformat(),
         "amount": round(r["inflow_amount"], 4)}
        for r in series_df.to_dicts()
    ]
    vals = series_df["inflow_amount"].to_list()

    def _tail(n: int) -> float | None:
        return round(sum(vals[-n:]), 4) if len(vals) >= 1 else None

    stats = {"yesterday": round(vals[-1], 4) if vals else None,
             "d5": _tail(5), "d20": _tail(20), "d60": _tail(60),
             "data_end_date": end.isoformat()}
    return {"series": series, "stats": stats}
