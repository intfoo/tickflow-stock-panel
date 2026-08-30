"""ETF 复权因子链路修复 — 回归测试。

背景: ETF 拆分(如 159967 创成长 2020-11-06)时回测收益假暴跌。根因:
  1. sync_adj_factor 写盘前未过滤响应到请求 symbols → 上游兜底返回的
     全市场股票事件污染 adj_factor_etf (线上实证: 请求 2 只 ETF 返回 5328 只股票)。
  2. daily_pipeline ETF 因子首次同步只拉最近 30 天 → 历史拆分事件永远缺失。
  3. 因子晚到时 ETF enriched 不做受影响标的全日期重算 → 历史分区保持未复权价。

不依赖真实数据源/网络: 全部 monkeypatch mock。
"""
from __future__ import annotations

import itertools
import types
from datetime import date, datetime

import polars as pl

from app.services import preferences


def _repo(tmp_path, earliest=None):
    return types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        earliest_etf_daily_date=lambda: earliest,
    )


def _factor_df(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "trade_date": [r[1] for r in rows],
            "ex_factor": [r[2] for r in rows],
        }
    )


# ──────────────────────────────────────────────────────────
# Fix 1: sync_adj_factor 响应过滤到请求 symbols
# ──────────────────────────────────────────────────────────


def test_sync_adj_factor_filters_unrequested_symbols_custom(monkeypatch, tmp_path):
    """自定义源路径: 上游返回非请求 symbol 的行被丢弃, 不落盘不进 affected。"""
    from app.services import kline_sync

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "amazingdata")
    resp = _factor_df([
        ("159967.SZ", date(2020, 11, 5), 2.941176),
        ("000001.SZ", date(2026, 8, 1), 1.1),  # 上游兜底混入的股票事件
    ])
    fake_provider = types.SimpleNamespace(
        get_adj_factors=lambda symbols, start_time, end_time, asset_type, on_chunk_done: resp,
    )
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: fake_provider)

    capset = types.SimpleNamespace(has=lambda cap: False)
    written, affected = kline_sync.sync_adj_factor(
        ["159967.SZ"], _repo(tmp_path), capset, asset_type="etf",
    )

    df = pl.read_parquet(tmp_path / "adj_factor_etf" / "all.parquet")
    assert set(df["symbol"].to_list()) == {"159967.SZ"}
    assert affected == ["159967.SZ"]
    assert written == 1


def test_sync_adj_factor_filters_unrequested_symbols_tickflow(monkeypatch, tmp_path):
    """TickFlow 路径: SDK 返回 dict 中非请求 symbol 同样被过滤。"""
    from app.services import kline_sync

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tickflow")
    resp = {
        "159967.SZ": [{"trade_date": "2020-11-05", "ex_factor": 2.941176}],
        "000001.SZ": [{"trade_date": "2026-08-01", "ex_factor": 1.1}],
    }
    fake_tf = types.SimpleNamespace(
        klines=types.SimpleNamespace(ex_factors=lambda symbols, **kwargs: resp),
    )
    monkeypatch.setattr(kline_sync, "get_client", lambda: fake_tf)
    capset = types.SimpleNamespace(
        has=lambda cap: True,
        limits=lambda cap: types.SimpleNamespace(batch=50, rpm=30),
    )

    written, affected = kline_sync.sync_adj_factor(
        ["159967.SZ"], _repo(tmp_path), capset, asset_type="etf",
    )

    df = pl.read_parquet(tmp_path / "adj_factor_etf" / "all.parquet")
    assert set(df["symbol"].to_list()) == {"159967.SZ"}
    assert affected == ["159967.SZ"]
    assert written == 1


def test_sync_adj_factor_prunes_polluted_etf_existing(monkeypatch, tmp_path):
    """已被污染(全是股票 symbol)的 adj_factor_etf 在 merge 时自 prune。"""
    from app.services import kline_sync

    fdir = tmp_path / "adj_factor_etf"
    fdir.mkdir(parents=True)
    _factor_df([
        ("000001.SZ", date(2026, 8, 1), 1.1),
        ("000002.SZ", date(2026, 8, 1), 1.2),
    ]).write_parquet(fdir / "all.parquet")

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "amazingdata")
    resp = _factor_df([("159967.SZ", date(2020, 11, 5), 2.941176)])
    fake_provider = types.SimpleNamespace(
        get_adj_factors=lambda symbols, start_time, end_time, asset_type, on_chunk_done: resp,
    )
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: fake_provider)

    capset = types.SimpleNamespace(has=lambda cap: False)
    kline_sync.sync_adj_factor(["159967.SZ"], _repo(tmp_path), capset, asset_type="etf")

    df = pl.read_parquet(fdir / "all.parquet")
    assert set(df["symbol"].to_list()) == {"159967.SZ"}, "污染的股票行应被 prune"


def test_sync_adj_factor_stock_does_not_prune(monkeypatch, tmp_path):
    """股票因子表不 prune: 已调出当前标的池的个股历史因子必须保留。"""
    from app.services import kline_sync

    fdir = tmp_path / "adj_factor"
    fdir.mkdir(parents=True)
    _factor_df([("600000.SH", date(2020, 5, 1), 1.3)]).write_parquet(fdir / "all.parquet")

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "amazingdata")
    resp = _factor_df([("600519.SH", date(2026, 6, 1), 1.02)])
    fake_provider = types.SimpleNamespace(
        get_adj_factors=lambda symbols, start_time, end_time, asset_type, on_chunk_done: resp,
    )
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: fake_provider)

    capset = types.SimpleNamespace(has=lambda cap: False)
    kline_sync.sync_adj_factor(["600519.SH"], _repo(tmp_path), capset, asset_type="stock")

    df = pl.read_parquet(fdir / "all.parquet")
    assert set(df["symbol"].to_list()) == {"600000.SH", "600519.SH"}


# ──────────────────────────────────────────────────────────
# Fix 2: ETF 因子同步起点 (etf_adj_sync_start)
# ──────────────────────────────────────────────────────────


def test_etf_adj_sync_start_missing_file_full_history(tmp_path):
    """因子表不存在 → 从 ETF 日K最早日期全历史回补。"""
    from app.services import index_sync

    repo = _repo(tmp_path, earliest=date(2018, 6, 1))
    start = index_sync.etf_adj_sync_start(repo, ["159967.SZ"], datetime(2026, 8, 30))
    assert start == datetime(2018, 6, 1)


def test_etf_adj_sync_start_missing_file_no_daily(tmp_path):
    """因子表不存在且无 ETF 日K → 兜底最近 365 天。"""
    from app.services import index_sync

    repo = _repo(tmp_path, earliest=None)
    start = index_sync.etf_adj_sync_start(repo, ["159967.SZ"], datetime(2026, 8, 30))
    assert start == datetime(2025, 8, 30)


def test_etf_adj_sync_start_incremental_with_lookback(tmp_path):
    """增量: max(trade_date)=2026-08-20 晚于 15 天回看线 → 用回看线 2026-08-15。"""
    from app.services import index_sync

    fdir = tmp_path / "adj_factor_etf"
    fdir.mkdir(parents=True)
    _factor_df([("159967.SZ", date(2026, 8, 20), 1.05)]).write_parquet(fdir / "all.parquet")

    repo = _repo(tmp_path, earliest=date(2018, 6, 1))
    start = index_sync.etf_adj_sync_start(repo, ["159967.SZ"], datetime(2026, 8, 30))
    assert start == datetime(2026, 8, 15)


def test_etf_adj_sync_start_incremental_older_max(tmp_path):
    """增量: max(trade_date)=2026-08-01 早于 15 天回看线 → 用 max(trade_date)。"""
    from app.services import index_sync

    fdir = tmp_path / "adj_factor_etf"
    fdir.mkdir(parents=True)
    _factor_df([("159967.SZ", date(2026, 8, 1), 1.05)]).write_parquet(fdir / "all.parquet")

    repo = _repo(tmp_path, earliest=date(2018, 6, 1))
    start = index_sync.etf_adj_sync_start(repo, ["159967.SZ"], datetime(2026, 8, 30))
    assert start == datetime(2026, 8, 1)


def test_etf_adj_sync_start_polluted_file_full_history(tmp_path):
    """因子表被污染(过滤到 ETF symbols 后为空) → 视同缺失, 全历史回补。"""
    from app.services import index_sync

    fdir = tmp_path / "adj_factor_etf"
    fdir.mkdir(parents=True)
    _factor_df([("000001.SZ", date(2026, 8, 27), 1.1)]).write_parquet(fdir / "all.parquet")

    repo = _repo(tmp_path, earliest=date(2018, 6, 1))
    start = index_sync.etf_adj_sync_start(repo, ["159967.SZ"], datetime(2026, 8, 30))
    assert start == datetime(2018, 6, 1), "污染行的日期不得挡住全历史回补"


# ──────────────────────────────────────────────────────────
# Fix 3: recompute_etf_enriched_for_symbols
# ──────────────────────────────────────────────────────────


def _seed_etf_daily_with_split(tmp_path) -> None:
    """构造 159967.SZ 含拆分的 ETF 日K: 2.0/2.0 → 0.68/0.67 (事件日 2020-11-05)。"""
    rows = [
        (date(2020, 11, 3), 2.0),
        (date(2020, 11, 4), 2.0),
        (date(2020, 11, 5), 0.68),
        (date(2020, 11, 6), 0.67),
    ]
    for d, c in rows:
        part = tmp_path / "kline_etf_daily" / f"date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["159967.SZ"],
                "date": [d],
                "open": [c],
                "high": [c],
                "low": [c],
                "close": [c],
                "volume": [1000.0],
                "amount": [10000.0],
            }
        ).write_parquet(part / "part.parquet")


def _load_enriched(tmp_path) -> pl.DataFrame:
    files = sorted((tmp_path / "kline_etf_enriched").glob("date=*/part.parquet"))
    assert files, "enriched 应有输出分区"
    return pl.concat([pl.read_parquet(f) for f in files]).sort("date")


def test_recompute_etf_enriched_split_continuity(tmp_path):
    """拆分场景: 重算后 enriched close 连续 (无假暴跌), raw_close 保持原始价。"""
    from app.services import index_sync
    from app.tickflow.repository import DataStore, KlineRepository

    _seed_etf_daily_with_split(tmp_path)
    fdir = tmp_path / "adj_factor_etf"
    fdir.mkdir(parents=True, exist_ok=True)
    # 事件日 2020-11-05, pre/post = 2.0/0.68
    _factor_df([("159967.SZ", date(2020, 11, 5), 2.0 / 0.68)]).write_parquet(fdir / "all.parquet")

    repo = KlineRepository(DataStore(tmp_path))
    written = index_sync.recompute_etf_enriched_for_symbols(repo, ["159967.SZ"])
    assert written == 4

    df = _load_enriched(tmp_path)
    closes = df["close"].to_list()
    raws = df["raw_close"].to_list()
    # raw_close 保持原始不复权价 (含拆分跳变)
    assert raws == [2.0, 2.0, 0.68, 0.67]
    # 前复权 close: 最新价不变, 历史向下调整 → 序列连续
    assert closes[-1] == 0.67
    for prev, cur in itertools.pairwise(closes):
        assert abs(cur / prev - 1) < 0.05, f"复权后仍有跳变: {closes}"
    # 拆分前两日价格应被调整到 0.68 附近
    assert abs(closes[0] - 0.68) < 0.01
    assert abs(closes[1] - 0.68) < 0.01


def test_recompute_etf_enriched_skips_when_factors_missing(tmp_path):
    """因子表缺失时不重算 — 避免把已复权的 enriched 退回未复权价。"""
    from app.services import index_sync
    from app.tickflow.repository import DataStore, KlineRepository

    _seed_etf_daily_with_split(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    written = index_sync.recompute_etf_enriched_for_symbols(repo, ["159967.SZ"])
    assert written == 0
    assert not list((tmp_path / "kline_etf_enriched").glob("date=*/part.parquet"))


# ──────────────────────────────────────────────────────────
# Fix 3b: extend_history ETF 分支因子晚到 → 触发实际重算
# ──────────────────────────────────────────────────────────


def test_extend_history_etf_recomputes_on_new_factors(monkeypatch, tmp_path):
    """extend_history(asset_type='etf'): sync_adj_factor 有新因子 →
    调 recompute_etf_enriched_for_symbols(受影响标的), 而非仅提示。"""
    from app.services import extend_history

    mock_db = types.SimpleNamespace(execute=lambda *a, **k: None)
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        db=mock_db,
        earliest_etf_daily_date=lambda: date(2025, 6, 1),
        get_etf_instruments=lambda: pl.DataFrame({"symbol": ["159967.SZ"]}),
        refresh_index_views=lambda: None,
    )
    capset = types.SimpleNamespace(has=lambda cap: False)

    calls: dict[str, object] = {"recompute": None}

    monkeypatch.setattr(
        "app.services.kline_sync.sync_adj_factor",
        lambda *a, **k: (5, ["159967.SZ"]),
    )
    monkeypatch.setattr(
        "app.services.index_sync.sync_and_persist_etf_daily",
        lambda *a, **k: 100,
    )
    monkeypatch.setattr(
        "app.services.index_sync.recompute_etf_enriched_for_symbols",
        lambda repo, symbols, **k: calls.__setitem__("recompute", list(symbols)) or 0,
    )
    monkeypatch.setattr(extend_history, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(extend_history, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "same_as_daily")
    monkeypatch.setattr(preferences, "get_etf_data_provider_resolved", lambda: "amazingdata")

    result = extend_history.run_extend_history(
        repo, capset, value=1, unit="month", asset_type="etf",
    )

    assert "error" not in result
    assert calls["recompute"] == ["159967.SZ"], "因子晚到应触发受影响 ETF 的全日期重算"
