"""ETF 份额/资金 HTTP 拉取与同步编排。

- base URL 解析 (§4.2): etf→daily→首个 dataset, 截取 scheme://host:port
- auth 复用 (§4.3): _token_from_env 预检 + 401 区分
- 指纹防静默切换 (§4.2): source_fingerprint = sha256(base_url + token_env)[:16]
- 单飞 (§4.5): 模块级 asyncio.Lock, 增量/回填互斥
- 回填按自然月分批, 可续跑跳过已完成月
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx
import polars as pl

from app.data_providers.custom import loader
from app.data_providers.custom.config import CustomSourceConfig
from app.data_providers.custom.provider import _token_from_env
from app.services import etf_fund
from app.services import etf_fund_store as store

logger = logging.getLogger(__name__)

# 单飞锁: 增量/回填互斥
_lock = asyncio.Lock()

# 503 退避重试间隔 (秒)
_RETRY_DELAYS = [1, 2, 4]
# 增量拉取窗口 (天)
_INCREMENTAL_WINDOW = 10


class SyncError(Exception):
    """同步错误, 携带 HTTP status code。"""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def extract_base_url(url: str) -> str:
    """截取 scheme://host:port, 丢弃路径/查询/尾斜杠。"""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base.rstrip("/")


def source_fingerprint(base_url: str, token_env: str | None) -> str:
    """sha256(base_url + token_env) 前 16 位 hex。"""
    raw = f"{base_url}|{token_env or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """按自然月切分 [start, end] 区间。"""
    ranges: list[tuple[date, date]] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        # 月末
        if cur.month == 12:
            next_month = date(cur.year + 1, 1, 1)
        else:
            next_month = date(cur.year, cur.month + 1, 1)
        month_end = next_month - timedelta(days=1)

        seg_start = max(start, cur)
        seg_end = min(end, month_end)
        ranges.append((seg_start, seg_end))

        cur = next_month
    return ranges


def _pick_dataset_url(config: CustomSourceConfig) -> str:
    """优先级: etf → daily → 首个 dataset 的 url。"""
    datasets = config.datasets
    for key in ("etf", "daily"):
        ds = datasets.get(key)
        if ds and ds.url:
            return ds.url
    for ds in datasets.values():
        if ds and ds.url:
            return ds.url
    return ""


def resolve_source() -> dict:
    """解析当前配置的数据源, 返回 {name, base_url, headers, fingerprint, warning}。

    未配置/已删除/指纹变化 → SyncError(409); token 缺失 → SyncError(401)。
    """
    cfg = store.load_config()
    name = cfg.get("data_source")
    if not name:
        raise SyncError("未配置数据源", 409)

    try:
        provider = loader.get_provider(name)
    except ValueError:
        raise SyncError("配置的数据源已删除, 请重新选择", 409) from None

    config = provider.config
    selected_url = _pick_dataset_url(config)
    if not selected_url:
        raise SyncError("数据源未配置任何 dataset URL", 409)

    base_url = extract_base_url(selected_url)
    token_env = config.auth.token_env

    # host 不一致 warning
    warning: str | None = None
    hosts = set()
    for ds in config.datasets.values():
        if ds.url:
            hosts.add(extract_base_url(ds.url))
    if len(hosts) > 1:
        warning = f"各 dataset host 不一致 ({', '.join(sorted(hosts))}), 使用优先级最高的"

    # 指纹校验
    fp = source_fingerprint(base_url, token_env)
    saved_fp = cfg.get("source_fingerprint")
    if saved_fp and fp != saved_fp:
        raise SyncError("数据源配置已变化, 请在页面重新保存", 409)

    # token 预检
    token = _token_from_env(token_env)
    if config.auth.type != "none" and not token:
        raise SyncError(
            f"环境变量 {token_env} 未设置, 无法获取认证 token", 401
        )

    headers: dict[str, str] = {}
    if token and config.auth.type != "none":
        header_name = config.auth.header
        headers[header_name] = f"Bearer {token}"

    return {
        "name": name,
        "base_url": base_url,
        "headers": headers,
        "fingerprint": fp,
        "warning": warning,
    }


async def _fetch_range(
    src: dict, path: str, start: date, end: date
) -> pl.DataFrame:
    """HTTP POST {base_url}{path} 拉取 [start, end] 区间数据, 解析为 DataFrame。

    503 指数退避重试 3 次 (1s/2s/4s); 401 → SyncError(401); 其他 HTTP 错误 → SyncError(502)。
    """
    base_url = src["base_url"]
    headers = src.get("headers") or {}
    body = {"start_time": start.isoformat(), "end_time": end.isoformat()}
    url = f"{base_url}{path}"

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0, *_RETRY_DELAYS]):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < len(_RETRY_DELAYS):
                continue
            raise SyncError(f"HTTP 请求失败: {e}", 502) from e

        if resp.status_code == 503:
            last_exc = SyncError("上游 503 未就绪", 503)
            if attempt < len(_RETRY_DELAYS):
                continue
            raise SyncError("上游 503, 重试 3 次后仍失败", 503) from last_exc

        if resp.status_code == 401:
            token_env = (src.get("token_env") or "")
            raise SyncError(
                f"token 可能失效, 请检查环境变量 {token_env} 的值", 401
            )

        if resp.status_code >= 400:
            raise SyncError(
                f"上游 HTTP {resp.status_code}: {resp.text[:200]}", 502
            )

        data = resp.json().get("data", [])
        return _parse_response(data, path)

    # 不应到达
    raise SyncError(f"拉取失败: {last_exc}", 502) from last_exc


def _parse_response(data: list[dict], path: str) -> pl.DataFrame:
    """解析 resp.json()['data'] 为 DataFrame, 列名映射 + 类型转换。"""
    if not data:
        if path == "/etf/share":
            return pl.DataFrame(schema=store.SHARE_SCHEMA)
        return pl.DataFrame(schema=store.NAV_SCHEMA)

    raw = pl.DataFrame(data)

    if path == "/etf/share":
        # code/trade_date/share/ann_date
        cols = {}
        for col in ("code", "trade_date", "share", "ann_date"):
            if col in raw.columns:
                cols[col] = raw[col]
        df = pl.DataFrame(cols)
        if "trade_date" in df.columns:
            df = df.with_columns(
                pl.col("trade_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
            )
        if "ann_date" in df.columns:
            df = df.with_columns(
                pl.col("ann_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
            )
        if "share" in df.columns:
            df = df.with_columns(pl.col("share").cast(pl.Float64, strict=False))
        return df.select([c for c in ["code", "trade_date", "share", "ann_date"] if c in df.columns])

    # nav: code/trade_date/nav
    cols = {}
    for col in ("code", "trade_date", "nav"):
        if col in raw.columns:
            cols[col] = raw[col]
    df = pl.DataFrame(cols)
    if "trade_date" in df.columns:
        df = df.with_columns(
            pl.col("trade_date").str.strptime(pl.Date, "%Y-%m-%d", strict=False)
        )
    if "nav" in df.columns:
        df = df.with_columns(pl.col("nav").cast(pl.Float64, strict=False))
    return df.select([c for c in ["code", "trade_date", "nav"] if c in df.columns])


def _update_data_range() -> None:
    """从 share/nav 读 min/max trade_date 更新 state.data_range。"""
    share = store.read_share()
    nav = store.read_nav()
    dates: list[date] = []
    if not share.is_empty() and "trade_date" in share.columns:
        dates.extend(share["trade_date"].to_list())
    if not nav.is_empty() and "trade_date" in nav.columns:
        dates.extend(nav["trade_date"].to_list())
    if dates:
        state = store.load_state()
        state["data_range"] = {"min": min(dates).isoformat(), "max": max(dates).isoformat()}
        store.save_state(state)


async def run_incremental(repo) -> dict:
    """增量同步: 拉最近 10 天 share+nav → merge → recompute_inflow → 更新 state。"""
    src = resolve_source()
    end = date.today()
    start = end - timedelta(days=_INCREMENTAL_WINDOW)

    share_df = await _fetch_range(src, "/etf/share", start, end)
    if not share_df.is_empty():
        store.merge_share(share_df)
    nav_df = await _fetch_range(src, "/etf/nav", start, end)
    if not nav_df.is_empty():
        store.merge_nav(nav_df)

    etf_fund.recompute_inflow(repo)

    state = store.load_state()
    state["last_sync"] = end.isoformat()
    store.save_state(state)
    _update_data_range()

    return {"ok": True}


async def run_backfill(repo, start: date, end: date) -> None:
    """分批回填: 按自然月分批, 续跑跳过 completed_months, 完成后 recompute + 更新 data_range。"""
    src = resolve_source()
    months = _month_ranges(start, end)

    state = store.load_state()
    completed = set(state.get("completed_months") or [])
    pending = [(s, e) for s, e in months if s.strftime("%Y-%m") not in completed]

    state["backfill"] = {
        "running": True,
        "total": len(months),
        "done": len(completed),
        "current": pending[0][0].strftime("%Y-%m") if pending else None,
        "error": None,
    }
    store.save_state(state)

    try:
        for m_start, m_end in pending:
            month_key = m_start.strftime("%Y-%m")
            state = store.load_state()
            state["backfill"]["current"] = month_key
            store.save_state(state)

            share_df = await _fetch_range(src, "/etf/share", m_start, m_end)
            if not share_df.is_empty():
                store.merge_share(share_df)
            nav_df = await _fetch_range(src, "/etf/nav", m_start, m_end)
            if not nav_df.is_empty():
                store.merge_nav(nav_df)

            state = store.load_state()
            completed.add(month_key)
            state["completed_months"] = sorted(completed)
            state["backfill"]["done"] = len(completed)
            store.save_state(state)

        etf_fund.recompute_inflow(repo)
        _update_data_range()

    except Exception as e:
        state = store.load_state()
        state["backfill"]["error"] = str(e)
        store.save_state(state)
        raise
    finally:
        state = store.load_state()
        state["backfill"]["running"] = False
        state["backfill"]["current"] = None
        store.save_state(state)


def sync_status() -> dict:
    """返回 state + 当前是否运行中。"""
    state = store.load_state()
    state["running"] = _lock.locked()
    return state


async def trigger(
    mode: str, repo, start: date | None = None, end: date | None = None
) -> dict:
    """单飞触发同步: 运行中 → SyncError(409); 否则后台 create_task。

    锁用法: if _lock.locked() 检查 + async with _lock 包住后台任务协程本体。
    """
    if _lock.locked():
        raise SyncError("同步进行中, 请稍后再试", 409)

    async def _run():
        async with _lock:
            try:
                if mode == "incremental":
                    await run_incremental(repo)
                elif mode == "backfill":
                    if start is None or end is None:
                        raise SyncError("backfill 需要 start 和 end", 422)
                    await run_backfill(repo, start, end)
            except SyncError:
                raise
            except Exception as e:
                logger.warning("etf_fund sync (%s) failed: %s", mode, e)
                raise

    loop = asyncio.get_running_loop()
    task = loop.create_task(_run())
    # 不 await task, 后台运行
    _ = task  # 防止 lint 警告未使用
    return {"ok": True}


async def daily_incremental_job(repo) -> None:
    """cron 入口: 增量同步 (锁占用则跳过)。"""
    if _lock.locked():
        logger.info("etf_fund daily incremental skipped: sync in progress")
        return
    try:
        await run_incremental(repo)
    except Exception as e:
        logger.warning("etf_fund 每日同步失败: %s", e)
