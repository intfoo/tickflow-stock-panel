"""ETF 份额/资金 HTTP 拉取与同步编排。

- base URL 解析 (§4.2): etf→daily→首个 dataset, 截取 scheme://host:port
- auth 复用 (§4.3): _token_from_env 预检 + 401 区分
- 数据源动态解析: 每次从 yaml 实时取 URL/auth (yaml 由用户显式维护, 不做指纹设卡)
- 单飞 (§4.5): 模块级 asyncio.Lock, 增量/回填互斥
- 回填按 batch_months 自然月分批 (页面可配, 持久化在 config.json), 可续跑跳过已完成批
"""
from __future__ import annotations

import asyncio
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


def _add_months(d: date, months: int) -> date:
    """日期加 N 个自然月, 日溢出时收敛到当月最后一天 (如 01-31 + 1月 → 02-28)。"""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    last_day = 31 if month == 12 else (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def _chunk_ranges(start: date, end: date, months: int) -> list[tuple[date, date]]:
    """按自然月数切分 [start, end] 区间 (回填批次)。"""
    ranges: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        seg_end = min(_add_months(cur, months) - timedelta(days=1), end)
        ranges.append((cur, seg_end))
        cur = seg_end + timedelta(days=1)
    return ranges


def pick_dataset_url(config: CustomSourceConfig) -> str:
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
    """解析当前配置的数据源, 返回 {name, base_url, headers, warning, token_env}。

    每次从 yaml 动态解析, yaml 变更即时生效。
    未配置/已删除/无 dataset URL → SyncError(409); token 缺失 → SyncError(401)。
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
    selected_url = pick_dataset_url(config)
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
        "warning": warning,
        "token_env": token_env,
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
            # 全量 ETF 大区间上游计算 35~72s + 30万行 JSON 传输, 60s 会 ReadTimeout
            # 引发重试 (同一区间重复请求, 上游靠结果缓存兜底)。300s 与项目 MAX_TIMEOUT 对齐。
            async with httpx.AsyncClient(timeout=300) as client:
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
    """增量同步: 拉最近 10 天 share+nav → merge → recompute_inflow → 更新 state。

    须持 _lock 调用(经 trigger)。
    """
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


async def run_backfill(repo, start: date, end: date, batch_months: int = 1) -> None:
    """分批回填: 按 batch_months 自然月一批, 续跑跳过 completed_chunks, 完成后 recompute + 更新 data_range。

    须持 _lock 调用(经 trigger)。批次键为批起始日 ISO, 改批次大小后重叠区间会重拉
    (merge 去重, 无害)。
    """
    src = resolve_source()
    batch_months = max(1, batch_months)
    chunks = _chunk_ranges(start, end, batch_months)

    state = store.load_state()
    completed = set(state.get("completed_chunks") or [])
    pending = [(s, e) for s, e in chunks if s.isoformat() not in completed]

    state["backfill"] = {
        "running": True,
        "total": len(chunks),
        "done": len(chunks) - len(pending),
        "current": pending[0][0].isoformat() if pending else None,
        "error": None,
    }
    store.save_state(state)

    try:
        for c_start, c_end in pending:
            chunk_key = c_start.isoformat()
            state = store.load_state()
            state["backfill"]["current"] = f"{c_start.isoformat()} ~ {c_end.isoformat()}"
            store.save_state(state)

            share_df = await _fetch_range(src, "/etf/share", c_start, c_end)
            if not share_df.is_empty():
                store.merge_share(share_df)
            nav_df = await _fetch_range(src, "/etf/nav", c_start, c_end)
            if not nav_df.is_empty():
                store.merge_nav(nav_df)

            state = store.load_state()
            completed.add(chunk_key)
            state["completed_chunks"] = sorted(completed)
            state["backfill"]["done"] += 1
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
    mode: str, repo, start: date | None = None, end: date | None = None,
    batch_months: int = 1,
) -> dict:
    """单飞触发同步: 运行中 → SyncError(409); 否则后台 create_task。

    锁用法: locked() 检查 + acquire 合为原子 (空闲时 acquire 不 yield);
    create_task 失败时 release 防泄漏; 任务 finally release。
    """
    if _lock.locked():
        raise SyncError("同步进行中, 请稍后再试", 409)
    # 空闲时 acquire 不 yield, 与上面 locked() 检查合起来原子
    await _lock.acquire()

    async def _run():
        try:
            if mode == "incremental":
                await run_incremental(repo)
            elif mode == "backfill":
                if start is None or end is None:
                    raise SyncError("backfill 需要 start 和 end", 422)
                await run_backfill(repo, start, end, batch_months)
        except SyncError:
            raise
        except Exception as e:
            logger.warning("etf_fund sync (%s) failed: %s", mode, e)
            raise
        finally:
            _lock.release()

    loop = asyncio.get_running_loop()
    try:
        task = loop.create_task(_run())
    except Exception:
        _lock.release()
        raise
    _ = task  # 防止 lint 警告未使用; 任务后台运行
    return {"ok": True}

