"""ETF 自定义数据源 + 向前扩展历史 — 后端单元/回归测试。

不依赖真实 TickFlow API 或真实数据: 所有外部依赖通过 monkeypatch mock。
覆盖:
  1. run_extend_history(asset_type="stock") 走原有股票逻辑
  2. repository.earliest_etf_daily_date 空表返回 None
  3. GenericHTTPProvider.get_daily 配 asset_type_param 后请求参数含 asset_type=etf
"""
from __future__ import annotations

import types
from datetime import date, datetime
from unittest.mock import MagicMock

from app.services import preferences

# ──────────────────────────────────────────────────────────
# 1. run_extend_history(asset_type="stock") 走原有股票逻辑
# ──────────────────────────────────────────────────────────


def test_extend_history_stock_unchanged(monkeypatch, tmp_path):
    """asset_type='stock' (默认) → 走原有股票逻辑:
    调用 sync_and_persist_daily_batch, 不调 sync_and_persist_etf_daily。
    """
    from app.services import extend_history

    # —— mock repo ——
    # 股票路径调 earliest_daily_date (非 ETF 路径), 返回一个早日期
    earliest = date(2025, 6, 1)

    mock_db = MagicMock()
    mock_store = types.SimpleNamespace(data_dir=tmp_path)
    repo = types.SimpleNamespace(
        store=mock_store,
        db=mock_db,
        earliest_daily_date=lambda: earliest,
        refresh_index_views=lambda: None,
    )

    # —— mock capset ——
    capset = types.SimpleNamespace(has=lambda cap: False)

    # —— 跟踪哪些 sync 函数被调 ——
    calls: dict[str, bool] = {
        "sync_and_persist_daily_batch": False,
        "sync_and_persist_etf_daily": False,
        "sync_adj_factor": False,
        "run_pipeline": False,
    }

    def fake_sync_daily_batch(*args, **kwargs):
        calls["sync_and_persist_daily_batch"] = True
        return 100  # written rows

    def fake_sync_etf_daily(*args, **kwargs):
        calls["sync_and_persist_etf_daily"] = True
        return 0

    def fake_sync_adj_factor(*args, **kwargs):
        calls["sync_adj_factor"] = True
        return 0, []

    def fake_run_pipeline(*args, **kwargs):
        calls["run_pipeline"] = True
        return 100

    # —— mock _resolve_universe ——
    monkeypatch.setattr(extend_history, "_resolve_universe", lambda capset: ["600519.SH"])

    # —— mock kline_sync ——
    monkeypatch.setattr(
        "app.services.kline_sync.sync_and_persist_daily_batch",
        fake_sync_daily_batch,
    )
    monkeypatch.setattr("app.services.kline_sync.sync_adj_factor", fake_sync_adj_factor)

    # —— mock index_sync (确保 ETF 路径不被触发) ——
    monkeypatch.setattr(
        "app.services.index_sync.sync_and_persist_etf_daily",
        fake_sync_etf_daily,
    )

    # —— mock indicators.pipeline.run_pipeline ——
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", fake_run_pipeline)

    # —— mock _invalidate (避免触发真实缓存逻辑) ——
    monkeypatch.setattr(extend_history, "_invalidate", lambda table=None: None)

    # —— mock preferences (adj_factor_provider → same_as_daily → tickflow) ——
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "same_as_daily")
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")

    # —— 执行 ——
    result = extend_history.run_extend_history(
        repo, capset, value=1, unit="month", asset_type="stock",
    )

    # —— 断言 ——
    assert calls["sync_and_persist_daily_batch"] is True, "股票路径应调 sync_and_persist_daily_batch"
    assert calls["sync_and_persist_etf_daily"] is False, "股票路径不应调 sync_and_persist_etf_daily"
    assert "error" not in result, "正常路径不应返回 error"
    assert result["daily_rows"] == 100


# ──────────────────────────────────────────────────────────
# 3. repository.earliest_etf_daily_date 空表返回 None
# ──────────────────────────────────────────────────────────


def test_earliest_etf_daily_date_empty(monkeypatch):
    """空表 / 无视图时 earliest_etf_daily_date 返回 None。"""
    from app.tickflow import repository as repo_mod

    # 上游 v0.2.2 起该方法走 execute_one (cursor+close, 防 Windows 句柄钉住),
    # mock execute_one 返回 (None,) 模拟空表
    mock_execute_one = MagicMock(return_value=(None,))

    # 直接绑到类上测试 (不实例化完整 repo, 避免 DataStore 初始化)
    fake_self = types.SimpleNamespace(execute_one=mock_execute_one)

    result = repo_mod.KlineRepository.earliest_etf_daily_date(fake_self)

    assert result is None

    # 验证 SQL 查的是 kline_etf_daily
    executed_sql = mock_execute_one.call_args[0][0]
    assert "kline_etf_daily" in executed_sql


# ──────────────────────────────────────────────────────────
# 4. GenericHTTPProvider.get_daily 配 asset_type_param 后请求参数含 asset_type=etf
# ──────────────────────────────────────────────────────────


def test_generic_provider_get_daily_asset_type(monkeypatch):
    """配 asset_type_param 后, get_daily(asset_type='etf') 的请求参数含 asset_type=etf。"""
    from app.data_providers.custom.config import AuthConfig, CustomSourceConfig, DatasetConfig
    from app.data_providers.custom.provider import GenericHTTPProvider

    # 构造一个带 asset_type_param 的 daily dataset 配置
    daily_cfg = DatasetConfig(
        url="http://fake.example.com/daily",
        method="GET",
        batch=100,
        rpm=None,
        field_map={
            "symbol": "symbol",
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
        },
        symbols_param="symbols",
        start_param="start_time",
        end_param="end_time",
        asset_type_param="asset_type",  # 关键: 配置了 asset_type_param
    )

    config = CustomSourceConfig(
        name="fake",
        display_name="Fake Source",
        auth=AuthConfig(type="none"),
        datasets={"daily": daily_cfg},
    )

    provider = GenericHTTPProvider(config)

    # —— mock _request_rows 捕获 override_params ——
    captured: dict = {}

    def fake_request_rows(cfg, *, symbols=None, start_time=None, end_time=None,
                          override_params=None, override_body=None):
        captured["override_params"] = override_params
        captured["override_body"] = override_body
        # 返回空 list → _mapped_frame → empty df → normalize_daily → empty
        return []

    monkeypatch.setattr(provider, "_request_rows", fake_request_rows)

    # 调用 get_daily with asset_type="etf"
    provider.get_daily(
        symbols=["510300.SH"],
        start_time=datetime(2025, 1, 1),
        end_time=datetime(2025, 6, 1),
        asset_type="etf",
    )

    # —— 断言: override_params 含 asset_type=etf ——
    assert captured["override_params"] is not None, "override_params 不应为 None (asset_type_param 已配置)"
    assert captured["override_params"].get("asset_type") == "etf", \
        f"override_params 应含 asset_type=etf, 实际: {captured['override_params']}"

    # override_body 也应含 (GET 请求 body 不发但 _request_rows 仍接收)
    assert captured["override_body"] is not None
    assert captured["override_body"].get("asset_type") == "etf"

    provider.close()


def test_generic_provider_get_daily_no_asset_type_param(monkeypatch):
    """未配 asset_type_param 时, get_daily 不注入 asset_type 参数 (向后兼容)。"""
    from app.data_providers.custom.config import AuthConfig, CustomSourceConfig, DatasetConfig
    from app.data_providers.custom.provider import GenericHTTPProvider

    daily_cfg = DatasetConfig(
        url="http://fake.example.com/daily",
        method="GET",
        batch=100,
        rpm=None,
        field_map={
            "symbol": "symbol",
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
        },
        symbols_param="symbols",
        start_param="start_time",
        end_param="end_time",
        asset_type_param=None,  # 未配
    )

    config = CustomSourceConfig(
        name="fake",
        display_name="Fake Source",
        auth=AuthConfig(type="none"),
        datasets={"daily": daily_cfg},
    )

    provider = GenericHTTPProvider(config)

    captured: dict = {}

    def fake_request_rows(cfg, *, symbols=None, start_time=None, end_time=None,
                          override_params=None, override_body=None):
        captured["override_params"] = override_params
        captured["override_body"] = override_body
        return []

    monkeypatch.setattr(provider, "_request_rows", fake_request_rows)

    provider.get_daily(
        symbols=["510300.SH"],
        start_time=datetime(2025, 1, 1),
        end_time=datetime(2025, 6, 1),
        asset_type="etf",  # 即使传了 etf, 未配 asset_type_param 也不注入
    )

    # 未配 asset_type_param → override 为空 → 传 None
    assert captured["override_params"] is None, \
        "未配 asset_type_param 时 override_params 应为 None"
    assert captured["override_body"] is None, \
        "未配 asset_type_param 时 override_body 应为 None"

    provider.close()


# ──────────────────────────────────────────────────────────
# 5. _try_custom_daily: provider.get_daily 抛异常 → fall through 到 TickFlow
# ──────────────────────────────────────────────────────────


def test_try_custom_daily_fallback_on_exception(monkeypatch):
    """自定义源 provider.get_daily 抛异常时, _try_custom_daily 返回 (None, True)。"""
    from app.services import kline_sync

    symbols = ["600519.SH"]

    # —— mock _resolve_daily_provider 返回 (provider, False, None) → 表示 resolver 成功 ——
    mock_provider = MagicMock()
    mock_provider.get_daily.side_effect = RuntimeError("network timeout")
    monkeypatch.setattr(
        kline_sync, "_resolve_daily_provider",
        lambda name, asset_type="stock": (mock_provider, False, None),
    )

    # —— 执行 ——
    result = kline_sync._try_custom_daily(symbols, None, None, asset_type="stock")

    # —— 断言 ——
    assert result == (None, True), \
        f"provider.get_daily 抛异常时应返回 (None, True), 实际: {result}"


# ──────────────────────────────────────────────────────────
# 6. _try_custom_daily: resolver 异常 → fall through 到 TickFlow
# ──────────────────────────────────────────────────────────


def test_try_custom_daily_resolution_failed(monkeypatch):
    """provider_has_dataset 抛异常时, _try_custom_daily 返回 (None, True)。"""
    from app.data_providers import custom as custom_sources
    from app.services import kline_sync
    from app.services import preferences as prefs_mod

    symbols = ["600519.SH"]

    # —— mock preferences.get_daily_data_provider 返回自定义源名 ——
    monkeypatch.setattr(prefs_mod, "get_daily_data_provider", lambda: "mock_src")

    # —— mock custom_sources.provider_has_dataset 抛异常 ——
    def boom(*args, **kwargs):
        raise Exception("registry broken")

    monkeypatch.setattr(custom_sources, "provider_has_dataset", boom)

    # —— 执行 ——
    result = kline_sync._try_custom_daily(symbols, None, None)

    # —— 断言 ——
    assert result == (None, True), \
        f"resolver 异常时应返回 (None, True), 实际: {result}"


# ──────────────────────────────────────────────────────────
# 6b. sync_and_persist_etf_daily: 自定义源大 df → enriched 分 chunk 计算
# ──────────────────────────────────────────────────────────


def test_sync_etf_daily_chunked_enriched(monkeypatch):
    """自定义源返回 250 只标的大 df → compute_enriched 按 100 只/批分 3 次调用 (防全量 OOM)。"""
    import polars as pl

    from app.services import index_sync, kline_sync

    symbols = [f"51{i:04d}.SH" for i in range(250)]
    dates = [date(2025, 1, 2), date(2025, 1, 3)]
    df_custom = pl.DataFrame({
        "symbol": [s for s in symbols for _ in dates],
        "date": dates * len(symbols),
        "close": [1.0] * (len(symbols) * len(dates)),
    })

    appended: dict[str, int] = {"daily": 0, "enriched": 0}
    repo = types.SimpleNamespace(
        get_etf_instruments=lambda: pl.DataFrame({"symbol": symbols}),
        append_etf_daily=lambda df: appended.__setitem__("daily", df.height),
        append_etf_enriched=lambda df: appended.__setitem__("enriched", df.height),
        refresh_index_views=lambda: None,
    )
    capset = types.SimpleNamespace(has=lambda cap: False)

    # 自定义源成功返回全量 df
    monkeypatch.setattr(kline_sync, "_try_custom_daily", lambda *a, **k: (df_custom, False))
    monkeypatch.setattr(index_sync, "_load_etf_factors", lambda repo: pl.DataFrame())

    enrich_calls: list[int] = []

    def fake_compute_enriched(chunk_df, factors=None, instruments=None):
        enrich_calls.append(chunk_df["symbol"].n_unique())
        return chunk_df

    monkeypatch.setattr(index_sync, "compute_enriched", fake_compute_enriched)

    written = index_sync.sync_and_persist_etf_daily(repo, capset)

    assert written == df_custom.height
    assert enrich_calls == [100, 100, 50], \
        f"250 只标的应分 3 批 (100/100/50) 计算 enriched, 实际: {enrich_calls}"
    assert appended["daily"] == df_custom.height
    assert appended["enriched"] == df_custom.height


# ──────────────────────────────────────────────────────────
# 7. invalidate_data_cache("etf_adj_factor") 精确失效
# ──────────────────────────────────────────────────────────


def test_invalidate_etf_adj_factor():
    """invalidate_data_cache("etf_adj_factor") 后 _table_cache 中对应 key 被设为 None。"""
    from app.api import data as data_api

    # —— 先 set 一个非 None 值 ——
    fake_stats = {"rows": 100, "symbols_covered": 50, "trading_days": 200}
    data_api._table_cache["etf_adj_factor"] = fake_stats
    data_api._table_cache_ts["etf_adj_factor"] = 12345.0

    # 确认 set 成功
    assert data_api._table_cache["etf_adj_factor"] is not None

    # —— 调 invalidate ——
    data_api.invalidate_data_cache("etf_adj_factor")

    # —— 断言: 被设为 None ——
    assert data_api._table_cache["etf_adj_factor"] is None, \
        "invalidate_data_cache('etf_adj_factor') 后 _table_cache['etf_adj_factor'] 应为 None"


# ──────────────────────────────────────────────────────────
# 8. _safe_aggregate_etf_adj_factor 统计正确
# ──────────────────────────────────────────────────────────


def test_safe_aggregate_etf_adj_factor():
    """_safe_aggregate_etf_adj_factor 正确聚合 ETF 复权因子统计。"""
    from app.api import data as data_api

    # —— mock repo.execute_one 两次调用返回不同值 ——
    # 第一次: 取日期范围 (min, max)
    # 第二次: 统计 (rows, symbols, trading_days)
    call_count = [0]

    def fake_execute_one(sql, params=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # 日期范围查询
            return (date(2016, 1, 4), date(2026, 8, 7))
        else:
            # 统计查询
            return (100, 500, 2500)

    repo = types.SimpleNamespace(execute_one=fake_execute_one)

    # —— 执行 ——
    result = data_api._safe_aggregate_etf_adj_factor(repo)

    # —— 断言 ——
    assert result is not None, "结果不应为 None"
    assert result["rows"] == 100
    assert result["symbols_covered"] == 500
    assert result["trading_days"] == 2500
    assert result["earliest_date"] == "2016-01-04"
    assert result["latest_date"] == "2026-08-07"
    assert call_count[0] == 2, f"execute_one 应被调 2 次, 实际 {call_count[0]} 次"
