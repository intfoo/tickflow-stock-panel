"""指数 / ETF 数据同步服务。

标的列表优先用免费的 exchanges.get_instruments(type=index/etf) 拉取
(None/Free 档均可用,无需 quote.pool 权限);付费档可额外用
quotes.get_by_universes 作为补充来源。日K统一走 klines.batch。
"""
from __future__ import annotations

import logging
import gc
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.services import kline_sync, preferences
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, min_batch, resolve_limit, sleep_between_batches
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# exchanges.get_instruments 查询的交易所(沪深京)
_EXCHANGES = ["SH", "SZ", "BJ"]


def _quotes_to_index_instruments(resp) -> pl.DataFrame:
    """将 TickFlow quotes 响应(get_by_universes)规范为指数 instruments。

    付费档(Starter+)的补充来源,免费档用不到。
    """
    if resp is None:
        return pl.DataFrame()

    if isinstance(resp, pl.DataFrame):
        df = resp
    elif hasattr(resp, "columns"):
        df = pl.from_pandas(resp.reset_index() if hasattr(resp, "reset_index") else resp)
    else:
        rows: list[dict] = []
        for q in resp or []:
            item = q if isinstance(q, dict) else {}
            ext = item.get("ext") or {}
            symbol = item.get("symbol")
            if not symbol:
                continue
            rows.append({
                "symbol": str(symbol),
                "name": ext.get("name") or item.get("name") or str(symbol),
            })
        df = pl.DataFrame(rows)

    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()

    rename = {"ts_code": "symbol"}
    df = df.rename({k: v for k, v in rename.items() if k in df.columns})

    if "name" not in df.columns:
        if "ext" in df.columns:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))
        else:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))

    result = df.select([
        pl.col("symbol").cast(pl.Utf8),
        pl.col("name").cast(pl.Utf8),
    ]).with_columns([
        pl.col("symbol").str.split(".").list.first().alias("code"),
        pl.lit("index").alias("asset_type"),
    ])
    return result.unique(subset=["symbol"], keep="last").sort("symbol")


def _fetch_instruments_by_type(instrument_type: str, asset_type_label: str) -> pl.DataFrame:
    """用免费的 exchanges.get_instruments 拉取指定类型的标的列表。

    None/Free 档均可使用(标的信息查询免费开放)。
    instrument_type: 'index' / 'etf'
    asset_type_label: 写入 instruments 表的 asset_type 标记('index' / 'etf')
    """
    tf = get_client()
    rows: list[dict] = []
    for ex in _EXCHANGES:
        try:
            items = tf.exchanges.get_instruments(ex, instrument_type=instrument_type)
            for it in items or []:
                item = it if isinstance(it, dict) else {}
                symbol = item.get("symbol")
                if not symbol:
                    continue
                rows.append({
                    "symbol": str(symbol),
                    "name": item.get("name") or str(symbol),
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("get_instruments(%s, type=%s) failed: %s", ex, instrument_type, e)

    if not rows:
        return pl.DataFrame()

    return (
        pl.DataFrame(rows)
        .with_columns([
            pl.col("symbol").str.split(".").list.first().alias("code"),
            pl.lit(asset_type_label).alias("asset_type"),
        ])
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def sync_index_instruments(
    repo: KlineRepository,
    pull_index: bool = True,
    pull_etf: bool = True,
) -> int:
    """同步指数 / ETF 标的维表,返回标的总数。

    新版物理分开保存: 指数写 instruments_index, ETF 写 instruments_etf。
    读取层仍兼容旧版 instruments_index 中 asset_type='etf' 的历史数据。
    """
    index_parts: list[pl.DataFrame] = []
    etf_parts: list[pl.DataFrame] = []

    # 1) 免费通道:按开关分别拉 index / etf
    if pull_index:
        index_df = _fetch_instruments_by_type("index", "index")
        if not index_df.is_empty():
            index_parts.append(index_df)
    if pull_etf:
        etf_df = _fetch_instruments_by_type("etf", "etf")
        if not etf_df.is_empty():
            etf_parts.append(etf_df)

    # 2) 付费补充:Starter+ 用 get_by_universes 补指数(仅当开启指数拉取)
    if pull_index:
        capset = None
        try:
            from app.tickflow import policy
            capset = policy.detect_capabilities(force=False)
        except Exception:  # noqa: BLE001
            pass
        if capset is not None and capset.has(Cap.QUOTE_POOL):
            tf = get_client()
            for kwargs in (
                {"universes": ["CN_Index"]},
                {"universes": ["CN_Index"], "as_dataframe": False},
            ):
                try:
                    resp = tf.quotes.get_by_universes(**kwargs)
                    if resp is not None and len(resp) > 0:
                        sup = _quotes_to_index_instruments(resp)
                        if not sup.is_empty():
                            index_parts.append(sup)
                        break
                except Exception as e:  # noqa: BLE001
                    logger.debug("CN_Index universe supplement failed: %s", e)

    total = 0
    if index_parts:
        index_inst = pl.concat(index_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not index_inst.is_empty():
            repo.save_index_instruments(index_inst)
            total += index_inst.height
    if etf_parts:
        etf_inst = pl.concat(etf_parts, how="diagonal_relaxed").unique(subset=["symbol"], keep="last").sort("symbol")
        if not etf_inst.is_empty():
            repo.save_etf_instruments(etf_inst)
            total += etf_inst.height

    if total == 0:
        logger.warning("指数/ETF 标的列表为空(pull_index=%s, pull_etf=%s)", pull_index, pull_etf)
        return 0
    repo.refresh_index_views()
    logger.info("指数/ETF 标的同步完成: %d 只", total)
    return total


def sync_etf_instruments(repo: KlineRepository) -> int:
    """单独同步 ETF 标的维表(返回 ETF 数量)。"""
    etf_df = _fetch_instruments_by_type("etf", "etf")
    if etf_df.is_empty():
        return 0
    repo.save_etf_instruments(etf_df)
    repo.refresh_index_views()
    return etf_df.height


def sync_and_persist_index_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步指数/ETF 日K到独立 parquet,并计算 enriched。

    symbols_override 非空时,只拉这些代码(跳过 instruments 表),用于自定义范围。
    否则取 index_instruments 表全量(指数+ETF 合并存储)。
    on_chunk_done(current, total) 每个批次完成后回调。
    """
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
        if not symbols:
            return 0
    else:
        instruments = repo.get_index_instruments()
        if instruments.is_empty():
            sync_index_instruments(repo, pull_index=True, pull_etf=False)
            instruments = repo.get_index_instruments()
        if not instruments.is_empty() and "asset_type" in instruments.columns:
            instruments = instruments.filter(pl.col("asset_type") != "etf")
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    chunks = chunked(symbols, batch_size)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
        )
        if raw.is_empty():
            continue

        repo.append_index_daily(raw)
        enriched = compute_enriched(raw, factors=None, instruments=None)
        repo.append_index_enriched(enriched)
        total_rows += raw.height
        logger.info("index/etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched
        gc.collect()
    repo.refresh_index_views()
    return total_rows


def _load_etf_factors(repo: KlineRepository) -> pl.DataFrame:
    factor_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
    if not factor_path.exists():
        logger.warning("ETF 复权因子表不存在, ETF enriched 将不做前复权 (%s)", factor_path)
        return pl.DataFrame()
    try:
        return pl.read_parquet(factor_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("ETF 复权因子读取失败: %s", e)
        return pl.DataFrame()


def etf_adj_sync_start(
    repo: KlineRepository,
    etf_symbols: list[str],
    adj_end: datetime,
    lookback_days: int = 15,
) -> datetime:
    """ETF 除权因子同步起点 — 对齐股票语义 (daily_pipeline Step 1.5)。

    - 因子表缺失/为空/被污染(过滤到 ETF symbols 后无行) → ETF 日K最早日期,
      全历史回补 (旧实现固定 30 天窗口, 历史拆分事件永远进不了因子表,
      回测在拆分点出现收益假暴跌);
    - 否则增量: min(max(trade_date), adj_end - lookback_days)。
      回看兜底覆盖停机期间的新事件; sync_adj_factor merge+unique 幂等, 多拉无副作用。
    """
    adj_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
    max_date = None
    if adj_path.exists():
        try:
            max_date = (
                pl.scan_parquet(adj_path)
                .filter(pl.col("symbol").is_in(etf_symbols))
                .select(pl.col("trade_date").max())
                .collect()
                .item()
            )
        except Exception as e:
            logger.warning("ETF adj_factor 读取失败, 按全历史回补: %s", e)
            max_date = None
    if max_date is None:
        earliest = repo.earliest_etf_daily_date()
        base = earliest or (adj_end.date() - timedelta(days=365))
        return datetime.combine(base, datetime.min.time())
    if isinstance(max_date, str):
        max_d = date.fromisoformat(max_date)
    elif isinstance(max_date, datetime):
        max_d = max_date.date()
    else:
        max_d = max_date
    lookback_floor = adj_end - timedelta(days=lookback_days)
    return min(datetime.combine(max_d, datetime.min.time()), lookback_floor)


def recompute_etf_enriched_for_symbols(
    repo: KlineRepository,
    symbols: list[str],
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """用最新 ETF 复权因子对指定 ETF 全日期重算 enriched (merge-upsert 覆写)。

    前复权语义: 任一新除权事件都改变该标的**全部历史**的复权价, 而增量日K
    同步只重算新写入的日期分区 → 历史分区保持旧(可能未复权的)价格。对齐股票
    路径 run_pipeline(symbols=affected) 的"受影响个股全日期重算"。

    数据源为本地 kline_etf_daily (不重新拉网络)。返回写入行数。
    """
    if not symbols:
        return 0
    factors = _load_etf_factors(repo)
    if factors.is_empty():
        logger.warning("ETF 复权因子为空, 跳过 enriched 重算 (避免覆写为未复权价)")
        return 0
    daily_dir = repo.store.data_dir / "kline_etf_daily"
    if not daily_dir.exists():
        return 0
    daily_glob = str(daily_dir / "**" / "*.parquet")
    total = 0
    chunks = chunked(sorted(set(symbols)), 100)
    for i, chunk in enumerate(chunks):
        try:
            raw = (
                pl.scan_parquet(
                    daily_glob,
                    cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
                    extra_columns="ignore",
                )
                .filter(pl.col("symbol").is_in(chunk))
                .collect()
            )
        except Exception as e:
            logger.warning("ETF 日K读取失败, 跳过该批 (chunk %d/%d): %s", i + 1, len(chunks), e)
            continue
        if raw.is_empty():
            continue
        keep = [
            c for c in ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
            if c in raw.columns
        ]
        raw = raw.select(keep).sort(["symbol", "date"])
        batch_factors = factors.filter(pl.col("symbol").is_in(chunk))
        enriched = compute_enriched(raw, factors=batch_factors, instruments=None)
        repo.append_etf_enriched(enriched)
        total += raw.height
        del raw, batch_factors, enriched
        gc.collect()
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
    repo.refresh_index_views()
    logger.info("ETF enriched 重算完成: %d 只标的, %d 行", len(symbols), total)
    return total


def sync_etf_adj_factor(
    symbols: list[str],
    repo: KlineRepository,
    capset: CapabilitySet,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    on_chunk_done=None,
) -> tuple[int, list[str]]:
    """同步 ETF 复权因子；失败由调用方降级为 warning。"""
    return kline_sync.sync_adj_factor(
        symbols,
        repo,
        capset,
        start_time=start_time,
        end_time=end_time,
        on_chunk_done=on_chunk_done,
        asset_type="etf",
    )


def sync_and_persist_etf_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
    on_fallback: Callable[[str], None] | None = None,
) -> int:
    """同步 ETF 日K到独立 kline_etf_* parquet,并计算 ETF enriched。
    on_chunk_done(current, total) 每个批次完成后回调。
    """
    if symbols_override:
        symbols = sorted(set(s for s in symbols_override if s))
    else:
        instruments = repo.get_etf_instruments()
        if instruments.is_empty():
            sync_etf_instruments(repo)
            instruments = repo.get_etf_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    if not symbols:
        return 0

    # 自定义源分流（统一路由）
    end_time_custom = end_date or datetime.now()
    start_time_custom = start_date or (end_time_custom - timedelta(days=365))
    df_custom, fallback = kline_sync._try_custom_daily(
        symbols, start_time=start_time_custom, end_time=end_time_custom,
        asset_type="etf", on_chunk_done=on_chunk_done, on_fallback=on_fallback,
    )
    if not fallback:
        if df_custom is None or df_custom.is_empty():
            return 0
        repo.append_etf_daily(df_custom)
        factors = _load_etf_factors(repo)
        # 分 chunk 计算 enriched, 对齐 TickFlow 路径的逐 chunk 模式 (避免全量 OOM)
        enriched_parts: list[pl.DataFrame] = []
        for chunk_symbols in chunked(symbols, 100):
            chunk_df = df_custom.filter(pl.col("symbol").is_in(chunk_symbols))
            if chunk_df.is_empty():
                continue
            batch_factors = factors.filter(pl.col("symbol").is_in(chunk_symbols)) if not factors.is_empty() else factors
            enriched_parts.append(compute_enriched(chunk_df, factors=batch_factors, instruments=None))
            del chunk_df, batch_factors
            gc.collect()
        if enriched_parts:
            enriched = pl.concat(enriched_parts, how="diagonal_relaxed")
            repo.append_etf_enriched(enriched)
        repo.refresh_index_views()
        return df_custom.height
    # 自定义源未配 / 异常 → fall through 到 TickFlow

    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0

    limit = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)

    end_time = end_date or datetime.now()
    start_time = start_date or (end_time - timedelta(days=365))

    total_rows = 0
    chunks = chunked(symbols, batch_size)
    factors = _load_etf_factors(repo)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk,
            count=count,
            batch_size=None,
            start_time=start_time,
            end_time=end_time,
        )
        if raw.is_empty():
            continue

        repo.append_etf_daily(raw)
        batch_factors = factors.filter(pl.col("symbol").is_in(chunk)) if not factors.is_empty() else factors
        # ETF 使用复权和通用技术指标；不传 instruments，避免套用 A股涨跌停/连板逻辑。
        enriched = compute_enriched(raw, factors=batch_factors, instruments=None)
        repo.append_etf_enriched(enriched)
        total_rows += raw.height
        logger.info("etf daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched
        gc.collect()
    repo.refresh_index_views()
    return total_rows
