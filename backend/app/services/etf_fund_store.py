"""ETF 份额/资金数据本地缓存 (data/etf_fund/, 运行时数据不入 Git)。

全部为「读-改-写 tmp + rename 原子替换」; 读侧永远读已提交文件。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import polars as pl

from app.config import settings

logger = logging.getLogger(__name__)

SHARE_SCHEMA = {"code": pl.Utf8, "trade_date": pl.Date, "share": pl.Float64, "ann_date": pl.Date}
NAV_SCHEMA = {"code": pl.Utf8, "trade_date": pl.Date, "nav": pl.Float64}
INFLOW_SCHEMA = {"code": pl.Utf8, "trade_date": pl.Date,
                 "inflow_share": pl.Float64, "inflow_amount": pl.Float64}

_DEFAULT_CONFIG = {"data_source": None, "overlay_index": "000001.SH", "batch_months": 1}
_DEFAULT_STATE = {
    "last_sync": None,
    "completed_chunks": [],
    "backfill": {"running": False, "total": 0, "done": 0, "current": None, "error": None},
    "data_range": {"min": None, "max": None},
}


def data_dir() -> Path:
    d = settings.data_dir / "etf_fund"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _empty(schema: dict) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _read(path: Path, schema: dict) -> pl.DataFrame:
    if not path.exists():
        return _empty(schema)
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("etf_fund read %s failed: %s", path, e)
        return _empty(schema)


def _write_atomic(df: pl.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def _merge(path: Path, schema: dict, new: pl.DataFrame) -> None:
    """按 (code, trade_date) 去重覆盖式合并。"""
    if new.is_empty():
        return
    new = new.unique(subset=["code", "trade_date"], keep="last")
    old = _read(path, schema)
    if not old.is_empty():
        old = old.join(new.select(["code", "trade_date"]), on=["code", "trade_date"], how="anti")
    out = pl.concat([old, new], how="diagonal_relaxed").sort(["code", "trade_date"])
    _write_atomic(out, path)


def merge_share(df: pl.DataFrame) -> None:
    _merge(data_dir() / "share.parquet", SHARE_SCHEMA, df)


def merge_nav(df: pl.DataFrame) -> None:
    _merge(data_dir() / "nav.parquet", NAV_SCHEMA, df)


def read_share() -> pl.DataFrame:
    return _read(data_dir() / "share.parquet", SHARE_SCHEMA)


def read_nav() -> pl.DataFrame:
    return _read(data_dir() / "nav.parquet", NAV_SCHEMA)


def read_inflow() -> pl.DataFrame:
    return _read(data_dir() / "inflow.parquet", INFLOW_SCHEMA)


def write_inflow(df: pl.DataFrame) -> None:
    _write_atomic(df.sort(["code", "trade_date"]) if not df.is_empty() else df,
                  data_dir() / "inflow.parquet")


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(default))
    merged = json.loads(json.dumps(default))
    merged.update(data)
    for k, v in default.items():
        if isinstance(v, dict):
            sub = dict(v)
            sub.update(data.get(k) or {})
            merged[k] = sub
    return merged


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    return _read_json(data_dir() / "config.json", _DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    merged = load_config()
    merged.update(cfg)
    merged.pop("source_fingerprint", None)  # 清理历史遗留字段 (指纹机制已移除)
    _write_json(data_dir() / "config.json", merged)


_DEFAULT_BROAD = {"symbols": [], "customized": False}


def load_broad() -> dict:
    """返回 {"symbols": list[str], "customized": bool}。

    - 文件不存在 → 默认态 {"symbols": [], "customized": False}
    - 旧格式 (纯 list) → {"symbols": list, "customized": True}
    - dict → 原样 (补默认键)
    """
    path = data_dir() / "broad_etf.json"
    if not path.exists():
        return json.loads(json.dumps(_DEFAULT_BROAD))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(_DEFAULT_BROAD))
    if isinstance(data, list):
        return {"symbols": list(data), "customized": True}
    if not isinstance(data, dict):
        return json.loads(json.dumps(_DEFAULT_BROAD))
    out = json.loads(json.dumps(_DEFAULT_BROAD))
    out.update(data)
    if not isinstance(out.get("symbols"), list):  # 手动编辑出 null/脏值时兜底
        out["symbols"] = []
    return out


def save_broad(symbols: list[str]) -> None:
    _write_json(data_dir() / "broad_etf.json",
                {"symbols": sorted(set(symbols)), "customized": True})


def reset_broad() -> None:
    _write_json(data_dir() / "broad_etf.json",
                {"symbols": [], "customized": False})


def load_state() -> dict:
    return _read_json(data_dir() / "sync_state.json", _DEFAULT_STATE)


def save_state(state: dict) -> None:
    _write_json(data_dir() / "sync_state.json", state)
