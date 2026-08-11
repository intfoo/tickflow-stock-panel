"""标的池(Universe)定义(§6.3)。

实现:
  - 各池成份股用 universes.get(uid) 的 symbols 字段获取(标的列表接口,轻量)
  - CN_Equity_A universe 不可用时回退到 exchanges.get_instruments 聚合沪深京三市
  - 自选池 = 用户的 watchlist
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl

from app.config import settings
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

PoolId = Literal["CSI300", "CSI500", "SSE50", "CN_Equity_A", "CN_Index", "watchlist"]

# TickFlow universe id 是它内部命名(见 tf.universes.list())。
# 没有官方对照表,启动时按名称模糊匹配从 universes.list() 里找。
# 常见名:沪深300 / 中证500 / 上证50 / 全 A
_POOL_NAME_HINTS = {
    "CSI300": ["沪深300", "HS300", "CSI300"],
    "CSI500": ["中证500", "ZZ500", "CSI500"],
    "SSE50":  ["上证50",  "SH50", "SSE50"],
}


def _find_universe_id(hints: list[str]) -> str | None:
    """从 universes.list() 里按 name/id 子串匹配找一个 universe id。"""
    try:
        tf = get_client()
        unis = tf.universes.list()
    except Exception as e:  # noqa: BLE001
        logger.warning("universes.list failed: %s", e)
        return None
    for u in unis or []:
        item = u if isinstance(u, dict) else {"id": getattr(u, "id", ""), "name": getattr(u, "name", "")}
        haystack = (item.get("id", "") + " " + item.get("name", "")).lower()
        for h in hints:
            if h.lower() in haystack:
                return item["id"]
    return None


def _pool_cache_path(pool_id: str) -> Path:
    return settings.data_dir / "pools" / f"{pool_id}.parquet"


def get_pool(pool_id: PoolId, refresh: bool = False) -> list[str]:
    """返回标的池里的 symbol 列表。"""
    if pool_id == "watchlist":
        return _load_watchlist()

    cache = _pool_cache_path(pool_id)
    if cache.exists() and not refresh:
        df = pl.read_parquet(cache)
        return df["symbol"].to_list()

    symbols = _fetch_pool(pool_id)
    if symbols:
        cache.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": symbols, "as_of": [date.today()] * len(symbols)}).write_parquet(cache)
    return symbols


def _fetch_universe_members(uid: str) -> list[str] | None:
    """用 universes.get(uid) 拉取标的池成份股 symbol 列表。

    universes.get 返回 {id, name, description, region, category, symbol_count, symbols},
    其中 symbols 是 symbol 字符串列表。这是获取成份股的正确接口,
    不要用 quotes.get_by_universes(那是实时行情接口,只为取 symbol 调它会拉一堆
    不需要的 OHLCV 数据,且限速更严、free 档可能无权限)。
    """
    try:
        tf = get_client()
        info = tf.universes.get(uid)
    except Exception as e:  # noqa: BLE001
        logger.warning("universes.get(%s) failed: %s", uid, e)
        return None
    if not isinstance(info, dict):
        return None
    symbols = info.get("symbols") or []
    return [str(s) for s in symbols if s]


def _fetch_pool(pool_id: PoolId) -> list[str]:
    """从 TickFlow 拉取池成份。

    优先用 universes.get(uid) 的 symbols 字段(标的列表接口,轻量);
    universes.list 里找不到对应 universe 时(如 free 档无沪深300),
    CN_Equity_A 回退到 exchanges.get_instruments 聚合沪深京三市。
    """
    # 指数成份池(CSI300/CSI500/SSE50):用 _POOL_NAME_HINTS 匹配 universe id
    if pool_id in _POOL_NAME_HINTS:
        uid = _find_universe_id(_POOL_NAME_HINTS[pool_id])
        if not uid:
            logger.warning("无法在 TickFlow universes 列表里匹配到 %s", pool_id)
            return []
        members = _fetch_universe_members(uid)
        if members:
            return members
        logger.warning("universes.get(%s) 返回空, %s 池拉取失败", uid, pool_id)
        return []

    # 全 A:优先用 CN_Equity_A universe
    if pool_id == "CN_Equity_A":
        uid = _find_universe_id(["CN_Equity_A", "沪深京A股", "全A"])
        if uid:
            members = _fetch_universe_members(uid)
            if members:
                return sorted(set(members))
            logger.warning("universes.get(%s) 返回空, 回退 exchanges.get_instruments", uid)

        # 回退: exchanges.get_instruments 聚合沪深京三市
        return _fetch_all_a_via_exchanges()

    # 指数池
    if pool_id == "CN_Index":
        uid = _find_universe_id(["CN_Index", "沪深指数", "指数"])
        uid = uid or "CN_Index"
        members = _fetch_universe_members(uid)
        if members:
            return sorted(set(members))
        logger.warning("universes.get(%s) 返回空, CN_Index 池拉取失败", uid)
        return []

    return []


def _fetch_all_a_via_exchanges() -> list[str]:
    """用 exchanges.get_instruments 聚合沪深京三市股票 symbol 列表(兜底)。

    exchanges.get_instruments 返回标的元数据(symbol/name/code/exchange/type),
    不含实时行情,适合用于获取标的列表。CN_Equity_A universe 不可用时回退到此。
    """
    tf = get_client()
    all_symbols: set[str] = set()
    for ex in ("SH", "SZ", "BJ"):
        try:
            items = tf.exchanges.get_instruments(ex, instrument_type="stock")
        except Exception as e:  # noqa: BLE001
            logger.warning("exchanges.get_instruments(%s) failed: %s", ex, e)
            continue
        for item in items or []:
            if isinstance(item, dict):
                sym = item.get("symbol")
                if sym:
                    all_symbols.add(str(sym))
    if all_symbols:
        logger.info("CN_Equity_A via exchanges.get_instruments: %d symbols", len(all_symbols))
    return sorted(all_symbols)


def _load_watchlist() -> list[str]:
    """读取用户自选(由 watchlist service 维护)。"""
    path = settings.data_dir / "user_data" / "watchlist.parquet"
    if not path.exists():
        return []
    df = pl.read_parquet(path)
    if df.is_empty() or "symbol" not in df.columns:
        return []
    return df["symbol"].to_list()


# 兜底:Free 用户/无 API 时给一个小型可用集合,让 UI 不至于空白
DEMO_SYMBOLS = [
    "600000.SH",  # 浦发银行
    "600036.SH",  # 招商银行
    "600519.SH",  # 贵州茅台
    "601318.SH",  # 中国平安
    "601398.SH",  # 工商银行
    "000001.SZ",  # 平安银行
    "000333.SZ",  # 美的集团
    "000651.SZ",  # 格力电器
    "000858.SZ",  # 五粮液
    "002594.SZ",  # 比亚迪
]
