"""向前扩展历史数据 — 完全独立于 daily_pipeline 的盘后管道。

用户从日 K 卡片手动触发,指定往前补的时长 (x 天/月/年)。
流程:
  1. 获取当前最早日期
  2. 向前拉日 K batch (start = 最早日期 - offset, end = 最早日期)
  3. 向前拉除权因子 (同范围)
  4. 全量重算 enriched
  5. 刷新视图 + 缓存

⚠️ 本模块不导入 daily_pipeline 的任何函数,只复用基础设施:
  - kline_sync.sync_and_persist_daily_batch / sync_adj_factor
  - indicators.pipeline.run_pipeline
  - pipeline_jobs.JobStore
  - tickflow.repository.KlineRepository
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

from app.services import kline_sync
from app.services.pipeline_jobs import job_store
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet) -> list[str]:
    """解析标的池 — 与 daily_pipeline 独立的副本。"""
    if capset.has(Cap.KLINE_DAILY_BATCH):
        try:
            from app.tickflow.pools import get_pool
            all_a = get_pool("CN_Equity_A", refresh=True)
            if all_a:
                return sorted(all_a)
        except Exception as e:
            logger.warning("CN_Equity_A pool unavailable: %s", e)

    from app.tickflow.pools import DEMO_SYMBOLS, get_pool as _get_pool
    from app.config import settings
    from pathlib import Path
    import polars as pl
    base: set[str] = set(DEMO_SYMBOLS)
    base.update(_get_pool("watchlist"))
    d = Path(settings.data_dir)
    inst_path = d / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:
            logger.warning("instruments supplement failed: %s", e)
    return sorted(base)


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
    except Exception as e:
        logger.warning("refresh view %s failed: %s", name, e)


def compute_offset(value: int, unit: str) -> timedelta:
    """将用户输入的 value + unit 转成 timedelta。"""
    if unit == "day":
        return timedelta(days=value)
    elif unit == "month":
        return timedelta(days=value * 30)
    elif unit == "year":
        return timedelta(days=value * 365)
    else:
        raise ValueError(f"不支持的单位: {unit}")


def run_extend_history(
    repo: KlineRepository,
    capset: CapabilitySet,
    value: int,
    unit: str,
    on_progress: Callable | None = None,
    asset_type: str = "stock",
) -> dict:
    """向前扩展历史数据的主函数。

    完全独立于 daily_pipeline.run_now(),不调用其任何逻辑。
    返回结果 dict 供 job_store 记录。
    """
    emit = on_progress or _noop

    # 0. 计算时间偏移
    offset = compute_offset(value, unit)
    today = date.today()

    if asset_type == "etf":
        # 使用独立的 stage 名 'extend_history_etf', 与股票的 'extend_history' 区分,
        # 前端 STAGE_CARD 据此把进度路由到 ETF 卡片而非日K卡片。
        stage = "extend_history_etf"
        # 获取 ETF 最早日期
        emit(stage, 2, "检查当前 ETF 数据范围…")
        earliest = repo.earliest_etf_daily_date()

        if not earliest:
            return {"error": "本地无 ETF 日K数据,请先执行一次完整同步"}

        new_start = earliest - offset
        if new_start >= earliest:
            return {"error": "扩展范围无效,请增大时间跨度"}

        # 解析 ETF 标的池
        instruments = repo.get_etf_instruments()
        if instruments.is_empty():
            from app.services import index_sync as _idx_sync
            _idx_sync.sync_etf_instruments(repo)
            instruments = repo.get_etf_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return {"error": "ETF 标的池为空"}
        universe = sorted(set(instruments["symbol"].to_list()))

        start_str = new_start.strftime("%Y-%m-%d")
        end_str = earliest.strftime("%Y-%m-%d")

        # 先拉 ETF 复权因子: enriched 在日K同步内联计算, 需用最新因子保证复权口径
        # (对齐股票路径 adj_factor → run_pipeline 的时序)
        written_adj = 0
        adj_start = datetime.combine(new_start, datetime.min.time())
        adj_end = datetime.combine(today, datetime.min.time())
        from app.services import preferences as _prefs
        # adj_factor_provider 的 same_as_daily 已在 getter 层解析为真实源
        adj_provider = _prefs.get_adj_factor_provider()
        can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
        if can_sync_adj:
            emit(stage, 10, f"获取 ETF 除权因子 [{start_str} ~ {today.strftime('%Y-%m-%d')}]…")
            _adj_chunk_etf = lambda cur, tot: emit(
                stage, 10 + int(15 * cur / tot),
                f"ETF 除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True,
            )
            written_adj, affected_etf = kline_sync.sync_adj_factor(
                universe, repo, capset,
                start_time=adj_start, end_time=adj_end,
                asset_type="etf",
                on_chunk_done=_adj_chunk_etf,
            )
            emit(stage, 28, f"ETF 除权因子完成,{written_adj} 行")
            # 刷新 adj_factor_etf 视图
            _refresh_single_view(repo, "adj_factor_etf")
            _invalidate("etf_adj_factor")
        else:
            emit(stage, 28, "ETF 除权因子跳过(无权限)")

        # 拉 ETF 日 K（走自定义源分流; enriched 内部基于刚同步的因子逐 chunk 计算）
        emit(stage, 30, f"获取 ETF 日K [{start_str} ~ {end_str}]…")
        from app.services import index_sync
        _daily_chunk_etf = lambda cur, tot: emit(
            stage, 30 + int(50 * cur / tot),
            f"ETF 日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True,
        )
        _on_fallback_etf = lambda msg: emit(stage, 30, f"⚠ {msg}", skip_log=False)
        written_daily = index_sync.sync_and_persist_etf_daily(
            repo, capset,
            start_date=datetime.combine(new_start, datetime.min.time()),
            end_date=datetime.combine(earliest, datetime.min.time()),
            on_chunk_done=_daily_chunk_etf,
            on_fallback=_on_fallback_etf,
        )
        emit(stage, 82, f"ETF 日K完成,写入 {written_daily} 行")
        _invalidate("etf_daily")
        _invalidate("etf_enriched")

        # 因子晚到(含扩展区间新发现的拆分/分红) → 受影响 ETF 全日期重算 enriched,
        # 旧区间与新区间复权口径保持一致 (此前仅提示不重算: 拆分事件会让旧区间
        # 保持未复权价, 回测在拆分点出现收益假暴跌)
        if written_adj > 0 and affected_etf:
            try:
                index_sync.recompute_etf_enriched_for_symbols(repo, affected_etf)
                emit(stage, 85, f"ETF enriched 重算完成,{len(affected_etf)} 只")
            except Exception as e:
                logger.warning("ETF enriched 重算失败: %s", e)
                emit(stage, 85, f"提示: 新增 {written_adj} 条除权因子, enriched 重算失败({e}), "
                                "建议全量重同步 ETF 数据")

        # 刷新视图
        emit(stage, 95, "刷新视图…")
        repo.refresh_index_views()
        _invalidate(None)

        etf_dir = repo.store.data_dir / "kline_etf_daily"
        etf_days = len(list(etf_dir.glob("date=*"))) if etf_dir.exists() else 0

        emit(stage, 100, f"完成,已扩展至 {new_start}")

        return {
            "earliest_before": earliest.isoformat(),
            "earliest_after": new_start.isoformat(),
            "daily_rows": written_daily,
            "daily_days": etf_days,
            "adj_factor_rows": written_adj,
            "universe_size": len(universe),
        }

    # 1. 获取当前最早日期
    emit("extend_history", 2, "检查当前数据范围…")
    earliest = repo.earliest_daily_date()

    if not earliest:
        return {"error": "本地无日K数据,请先执行一次完整同步"}

    new_start = earliest - offset
    # 不能超过今天
    if new_start >= earliest:
        return {"error": "扩展范围无效,请增大时间跨度"}

    # 2. 解析标的池
    emit("extend_history", 5, "解析标的池…")
    universe = _resolve_universe(capset)
    if not universe:
        return {"error": "标的池为空"}
    emit("extend_history", 8, f"标的池: {len(universe)} 只")

    start_str = new_start.strftime("%Y-%m-%d")
    end_str = earliest.strftime("%Y-%m-%d")

    # 3. 拉日 K
    emit("extend_history", 10, f"获取日K [{start_str} ~ {end_str}]…")
    logger.info("extend_history: daily K [%s ~ %s], %d symbols", start_str, end_str, len(universe))

    def _daily_chunk(cur: int, tot: int) -> None:
        emit("extend_history", 10 + int(35 * cur / tot),
             f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    _on_fallback = lambda msg: emit("extend_history", 10, f"⚠ {msg}", skip_log=False)
    written_daily = kline_sync.sync_and_persist_daily_batch(
        universe, repo, capset,
        start_date=datetime.combine(new_start, datetime.min.time()),
        end_date=datetime.combine(earliest, datetime.min.time()),
        on_chunk_done=_daily_chunk,
        on_fallback=_on_fallback,
    )
    emit("extend_history", 45, f"日K 完成,写入 {written_daily} 行")
    logger.info("extend_history: daily K done, %d rows", written_daily)
    _refresh_single_view(repo, "kline_daily")
    _invalidate("daily")

    # 4. 拉除权因子 (新范围)
    written_adj = 0
    adj_start = datetime.combine(new_start, datetime.min.time())
    adj_end = datetime.combine(today, datetime.min.time())
    adj_start_str = new_start.strftime("%Y-%m-%d")
    adj_end_str = today.strftime("%Y-%m-%d")

    from app.services import preferences as _prefs
    adj_provider = _prefs.get_adj_factor_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
    if can_sync_adj:
        emit("extend_history", 48, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("extend_history: adj_factor [%s ~ %s]", adj_start_str, adj_end_str)

        def _adj_chunk(cur: int, tot: int) -> None:
            emit("extend_history", 48 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

        written_adj, _affected = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk,
        )
        emit("extend_history", 60, f"除权因子完成,{written_adj} 行")
        logger.info("extend_history: adj_factor done, %d rows", written_adj)
        _refresh_single_view(repo, "adj_factor")
        _invalidate("adj_factor")
    else:
        emit("extend_history", 60, "除权因子跳过(无权限)")
        logger.info("extend_history: adj_factor skipped, no ADJ_FACTOR capability")

    # 5. 全量重算 enriched
    emit("extend_history", 65, "全量计算 enriched…")
    logger.info("extend_history: full enriched rebuild start")

    from app.indicators.pipeline import run_pipeline
    written_enriched = run_pipeline()

    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0
    emit("extend_history", 92, f"enriched 完成,覆盖 {enriched_days} 天")
    logger.info("extend_history: enriched done, %d days", enriched_days)
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # 6. 刷新视图
    emit("extend_history", 95, "刷新视图…")
    _refresh_single_view(repo, "kline_daily")
    _refresh_single_view(repo, "kline_enriched")
    _refresh_single_view(repo, "adj_factor")
    _invalidate(None)

    # 7. 统计结果
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0

    emit("extend_history", 100, f"完成,已扩展至 {new_start}")

    return {
        "earliest_before": earliest.isoformat(),
        "earliest_after": new_start.isoformat(),
        "daily_rows": written_daily,
        "daily_days": daily_days,
        "adj_factor_rows": written_adj,
        "enriched_days": enriched_days,
        "universe_size": len(universe),
    }
