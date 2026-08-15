"""ETF 行情(份额/资金) API — fork 私有独立模块。"""
from __future__ import annotations

import logging
from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import etf_fund, etf_fund_sync
from app.services import etf_fund_store as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/etf-fund", tags=["etf-fund"])


class ConfigIn(BaseModel):
    data_source: str
    overlay_index: str | None = None


class BroadIn(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=2000)


class SyncIn(BaseModel):
    mode: str = "incremental"
    start: date | None = None
    end: date | None = None


def _etf_symbols(repo) -> set[str]:
    if repo is None:
        return set()
    df = repo.get_etf_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return set()
    return set(df["symbol"].cast(pl.Utf8).to_list())


def _etf_name_map(repo) -> dict[str, str]:
    if repo is None:
        return {}
    df = repo.get_etf_instruments()
    if df.is_empty():
        return {}
    cols = [c for c in ["symbol", "name"] if c in df.columns]
    if len(cols) < 2:
        return {}
    return {r["symbol"]: r["name"] for r in df.select(cols).to_dicts()}


def _config_payload(request: Request) -> dict:
    from app.data_providers.custom import loader
    cfg = store.load_config()
    sources = [{"name": s["name"], "display_name": s["display_name"]}
               for s in loader.list_sources()]
    changed, warning = False, None
    if cfg["data_source"]:
        try:
            src = etf_fund_sync.resolve_source()
            changed = src["fingerprint"] != (cfg["source_fingerprint"] or "")
            warning = src["warning"]
        except etf_fund_sync.SyncError as e:
            changed = e.status == 409 and "已变化" in str(e)
            warning = str(e)
    return {"sources": sources, "data_source": cfg["data_source"],
            "source_changed": changed, "overlay_index": cfg["overlay_index"],
            "warning": warning}


@router.get("/config")
def get_config(request: Request) -> dict:
    return _config_payload(request)


@router.put("/config")
def put_config(request: Request, body: ConfigIn) -> dict:
    from app.data_providers.custom import loader
    try:
        provider = loader.get_provider(body.data_source)
    except ValueError:
        raise HTTPException(404, f"数据源不存在: {body.data_source}") from None
    base = etf_fund_sync.extract_base_url(etf_fund_sync.pick_dataset_url(provider.config))
    token_env = provider.config.auth.token_env
    store.save_config({
        "data_source": body.data_source,
        "source_fingerprint": etf_fund_sync.source_fingerprint(base, token_env),
        **({"overlay_index": body.overlay_index} if body.overlay_index else {}),
    })
    return _config_payload(request)


@router.get("/broad")
def get_broad(request: Request) -> dict:
    symbols = store.load_broad()
    names = _etf_name_map(request.app.state.repo)
    return {"symbols": symbols,
            "items": [{"symbol": s, "name": names.get(s, s)} for s in symbols]}


@router.put("/broad")
def put_broad(request: Request, body: BroadIn) -> dict:
    valid = _etf_symbols(request.app.state.repo)
    if valid:
        bad = [s for s in body.symbols if s not in valid]
        if bad:
            raise HTTPException(422, f"非 ETF 代码: {', '.join(bad[:5])}")
    store.save_broad(body.symbols)
    return get_broad(request)


@router.get("/instruments")
def get_instruments(request: Request) -> dict:
    repo = request.app.state.repo
    df = repo.get_etf_instruments() if repo is not None else None
    if df is None or df.is_empty():
        return {"items": []}
    cols = [c for c in ["symbol", "name"] if c in df.columns]
    return {"items": df.select(cols).sort("symbol").to_dicts()}


@router.post("/sync")
async def post_sync(request: Request, body: SyncIn) -> dict:
    if body.mode not in ("incremental", "backfill"):
        raise HTTPException(422, "mode 必须是 incremental 或 backfill")
    if body.mode == "backfill" and (body.start is None or body.end is None):
        raise HTTPException(422, "backfill 需要 start 和 end")
    try:
        return await etf_fund_sync.trigger(
            body.mode, request.app.state.repo, body.start, body.end)
    except etf_fund_sync.SyncError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/status")
def get_status(request: Request) -> dict:
    cfg = store.load_config()
    out = etf_fund_sync.sync_status()
    out["configured"] = bool(cfg["data_source"])
    try:
        src = etf_fund_sync.resolve_source()
        out["source_changed"] = src["fingerprint"] != (cfg["source_fingerprint"] or "")
    except etf_fund_sync.SyncError:
        out["source_changed"] = False
    return out


@router.get("/leaderboard")
def leaderboard(
    request: Request,
    sort: str = Query("amount"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    broad_only: bool = Query(False),
) -> dict:
    size = min(size, 100)
    return etf_fund.leaderboard_rows(
        request.app.state.repo, set(store.load_broad()),
        sort, order, page, size, broad_only)


@router.get("/flow")
def flow(request: Request, days: int = Query(120, ge=5, le=750)) -> dict:
    return etf_fund.fund_flow(request.app.state.repo, days)


def register(app) -> None:
    """lifespan 内调用: 注册路由 + 每日增量同步 cron (须在 start_scheduler 之后)。"""
    app.include_router(router)
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        logger.warning("etf_fund: scheduler 不可用, 跳过每日同步 cron")
        return
    from apscheduler.triggers.cron import CronTrigger

    async def _job():
        try:
            await etf_fund_sync.trigger("incremental", app.state.repo)
        except etf_fund_sync.SyncError as e:
            logger.info("etf_fund 每日同步跳过: 已有同步进行中 (%s)", e)
        except Exception as e:
            logger.warning("etf_fund 每日同步失败: %s", e)

    scheduler.add_job(
        _job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=17, minute=0,
                            timezone="Asia/Shanghai"),
        id="etf_fund_sync", replace_existing=True, misfire_grace_time=3600,
    )
