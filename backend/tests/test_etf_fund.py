"""ETF 资金/份额模块测试。"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import etf_fund as etf_api
from app.config import settings
from app.data_providers.custom.config import AuthConfig, CustomSourceConfig, DatasetConfig
from app.services import etf_fund
from app.services import etf_fund_store as store
from app.services import etf_fund_sync as sync


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yield


def _share_df(rows):
    return pl.DataFrame(
        rows,
        schema={"code": pl.Utf8, "trade_date": pl.Date, "share": pl.Float64, "ann_date": pl.Date},
        orient="row",
    )


def _nav(rows):
    return pl.DataFrame(rows, schema={"code": pl.Utf8, "trade_date": pl.Date, "nav": pl.Float64}, orient="row")


CAL = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
       date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)]


class TestStore:
    def test_read_missing_returns_empty(self):
        assert store.read_share().is_empty()
        assert store.read_nav().is_empty()
        assert store.read_inflow().is_empty()

    def test_merge_share_dedup_overwrite(self):
        store.merge_share(_share_df([
            ("510300.SH", date(2026, 8, 1), 100.0, date(2026, 8, 2)),
            ("510300.SH", date(2026, 8, 4), 110.0, date(2026, 8, 5)),
        ]))
        # 同 (code, trade_date) 再 merge 应覆盖旧值
        store.merge_share(_share_df([
            ("510300.SH", date(2026, 8, 4), 115.0, date(2026, 8, 5)),
            ("510500.SH", date(2026, 8, 4), 50.0, date(2026, 8, 5)),
        ]))
        df = store.read_share().sort(["code", "trade_date"])
        assert df.height == 3
        row = df.filter(
            (pl.col("code") == "510300.SH") & (pl.col("trade_date") == date(2026, 8, 4))
        )
        assert row["share"][0] == 115.0

    def test_merge_nav_dedup(self):
        store.merge_nav(pl.DataFrame(
            [("510300.SH", date(2026, 8, 1), 4.0)],
            schema={"code": pl.Utf8, "trade_date": pl.Date, "nav": pl.Float64},
            orient="row"))
        store.merge_nav(pl.DataFrame(
            [("510300.SH", date(2026, 8, 1), 4.1)],
            schema={"code": pl.Utf8, "trade_date": pl.Date, "nav": pl.Float64},
            orient="row"))
        assert store.read_nav().height == 1
        assert store.read_nav()["nav"][0] == 4.1

    def test_write_inflow_full_replace(self):
        schema = {"code": pl.Utf8, "trade_date": pl.Date,
                  "inflow_share": pl.Float64, "inflow_amount": pl.Float64}
        store.write_inflow(pl.DataFrame([("A", date(2026, 8, 1), 1.0, 4.0)], schema=schema, orient="row"))
        store.write_inflow(pl.DataFrame([("B", date(2026, 8, 2), 2.0, 8.0)], schema=schema, orient="row"))
        df = store.read_inflow()
        assert df.height == 1 and df["code"][0] == "B"

    def test_config_roundtrip_and_defaults(self):
        cfg = store.load_config()
        assert cfg["data_source"] is None and cfg["overlay_index"] == "000001.SH"
        store.save_config({"data_source": "amaz", "source_fingerprint": "abc",
                           "overlay_index": "000300.SH"})
        assert store.load_config()["data_source"] == "amaz"

    def test_broad_roundtrip(self):
        assert store.load_broad() == []
        store.save_broad(["510300.SH", "510500.SH"])
        assert store.load_broad() == ["510300.SH", "510500.SH"]

    def test_state_roundtrip(self):
        st = store.load_state()
        assert st["backfill"]["running"] is False and st["completed_months"] == []
        st["completed_months"] = ["2026-07"]
        store.save_state(st)
        assert store.load_state()["completed_months"] == ["2026-07"]


class TestCalc:
    def test_sparse_diff_times_nav(self):
        share = _share_df([
            ("A", date(2026, 8, 3), 100.0, date(2026, 8, 4)),
            ("A", date(2026, 8, 5), 130.0, date(2026, 8, 6)),  # +30万份 x nav 2.0 = 60万
            ("A", date(2026, 8, 10), 90.0, date(2026, 8, 11)),  # -40万份 x nav 2.2 = -88万
        ])
        nav = _nav([(c, d, v) for c, d, v in [
            ("A", date(2026, 8, 3), 1.0), ("A", date(2026, 8, 4), 1.5),
            ("A", date(2026, 8, 5), 2.0), ("A", date(2026, 8, 6), 2.1),
            ("A", date(2026, 8, 7), 2.1), ("A", date(2026, 8, 10), 2.2)]])
        out = etf_fund.compute_inflow(share, nav, CAL).sort("trade_date")
        assert out["inflow_share"].to_list() == [None, 30.0, -40.0]
        assert out["inflow_amount"].to_list()[1:] == [60.0, -88.0]

    def test_first_row_diff_is_null(self):
        share = _share_df([("A", date(2026, 8, 3), 100.0, date(2026, 8, 4))])
        nav = _nav([("A", date(2026, 8, 3), 1.0)])
        out = etf_fund.compute_inflow(share, nav, CAL)
        assert out["inflow_amount"][0] is None  # 首行 diff=null, 不丢也不造 0

    def test_nav_ffill_on_calendar_before_join(self):
        # share 变动日 8-5 无 nav 行, 但 8-4 有更新 nav —— 必须用 8-4 的值(2.0),
        # 而不是上一个 share 变动日 8-3 的 nav(1.0)
        share = _share_df([
            ("A", date(2026, 8, 3), 100.0, date(2026, 8, 4)),
            ("A", date(2026, 8, 5), 150.0, date(2026, 8, 6)),
        ])
        nav = _nav([("A", date(2026, 8, 3), 1.0), ("A", date(2026, 8, 4), 2.0)])
        out = etf_fund.compute_inflow(share, nav, CAL)
        row = out.filter(pl.col("trade_date") == date(2026, 8, 5))
        assert row["inflow_amount"][0] == 50.0 * 2.0

    def test_null_nav_stays_null(self):
        share = _share_df([
            ("A", date(2026, 8, 3), 100.0, date(2026, 8, 4)),
            ("A", date(2026, 8, 5), 150.0, date(2026, 8, 6)),
        ])
        out = etf_fund.compute_inflow(share, _nav([]), CAL)  # 无任何 nav
        assert out["inflow_amount"][1] is None


class TestFlow:
    def _write_inflow(self):
        store.write_inflow(pl.DataFrame(
            [("B1", date(2026, 8, 5), 10.0, 20.0),
             ("B1", date(2026, 8, 6), 5.0, 10.0),
             ("B2", date(2026, 8, 6), 1.0, 2.0),
             ("X", date(2026, 8, 6), 99.0, 990.0)],  # 非宽基不计入
            schema={"code": pl.Utf8, "trade_date": pl.Date,
                    "inflow_share": pl.Float64, "inflow_amount": pl.Float64},
            orient="row"))

    def test_flow_sum_zero_fill_and_tail(self, monkeypatch):
        self._write_inflow()
        store.save_broad(["B1", "B2"])
        cal = CAL  # 8-3 .. 8-10
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: cal)
        out = etf_fund.fund_flow(repo=None, days=60)
        series = {r["trade_date"]: r["amount"] for r in out["series"]}
        # 8-5: B1 20万=0.002亿; 8-6: B1+B2=12万=0.0012亿; X 不计
        assert abs(series["2026-08-05"] - 0.002) < 1e-9
        assert abs(series["2026-08-06"] - 0.0012) < 1e-9
        assert series["2026-08-04"] == 0.0  # 历史缺失日补 0
        assert out["stats"]["data_end_date"] == "2026-08-06"
        assert out["series"][-1]["trade_date"] == "2026-08-06"  # 尾部不补 0, 截到数据日

    def test_flow_empty(self, monkeypatch):
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.fund_flow(repo=None, days=60)
        assert out["series"] == [] and out["stats"]["data_end_date"] is None


class TestLeaderboard:
    def _write_enriched(self):
        # 3 只 ETF x 6 天 close 序列, 验证涨幅窗口
        d = settings.data_dir / "kline_etf_enriched"
        closes = {"A": [10, 11, 12, 13, 14, 15], "B": [20, 19, 18, 17, 16, 15],
                  "C": [5, 5, 5, 5, 5, 5]}
        for dt, i in zip(CAL, range(6), strict=False):
            day = d / f"date={dt.isoformat()}"
            day.mkdir(parents=True, exist_ok=True)
            pl.DataFrame({
                "symbol": list(closes),
                "date": [dt] * 3,
                "close": [closes[s][i] for s in closes],
                "amount": [1e8, 2e8, 3e8],
            }).write_parquet(day / "part.parquet")

    def test_change_windows_and_market_cap(self, monkeypatch):
        self._write_enriched()
        store.save_broad(["A"])
        store.merge_share(_share_df([("A", date(2026, 8, 10), 20000.0, date(2026, 8, 11))]))
        store.merge_nav(_nav([("A", date(2026, 8, 10), 2.0)]))
        store.write_inflow(pl.DataFrame(
            [("A", date(2026, 8, 10), 100.0, 200.0)],
            schema={"code": pl.Utf8, "trade_date": pl.Date,
                    "inflow_share": pl.Float64, "inflow_amount": pl.Float64},
            orient="row"))
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.leaderboard_rows(repo=None, broad={"A"}, sort="change_pct",
                                        order="desc", page=1, size=20, broad_only=False)
        rows = {r["symbol"]: r for r in out["rows"]}
        assert out["total"] == 3 and out["data_date"] == "2026-08-10"
        a = rows["A"]
        assert a["change_pct"] == pytest.approx(15 / 14 - 1)        # 最新 vs 前一日
        assert a["change_pct_5d"] == pytest.approx(15 / 10 - 1)     # 最新 vs 5 个交易日前
        assert a["is_broad"] is True
        assert a["inflow_1d"] == pytest.approx(0.02)                # 200万 = 0.02亿
        assert a["share"] == pytest.approx(2.0)                     # 20000万份 = 2亿份
        assert a["market_cap"] == pytest.approx(4.0)                # 2亿份 x 2元 = 4亿
        assert rows["B"]["change_pct"] == pytest.approx(15 / 16 - 1)
        assert rows["C"]["inflow_1d"] is None                        # 无资金数据 → None

    def test_market_cap_aligned_ffill(self, monkeypatch):
        # share 最后变动日 8-10, nav 最后公布日 8-8 -> market_cap = share(8-10) x nav(8-8)
        self._write_enriched()
        store.save_broad(["A"])
        store.merge_share(_share_df([
            ("A", date(2026, 8, 3), 10000.0, date(2026, 8, 4)),   # 1万份
            ("A", date(2026, 8, 10), 20000.0, date(2026, 8, 11)),  # 2万份
        ]))
        store.merge_nav(_nav([
            ("A", date(2026, 8, 3), 1.0),
            ("A", date(2026, 8, 8), 2.0),  # nav 最后公布日早于 share 最后变动日
        ]))
        store.write_inflow(pl.DataFrame(
            [("A", date(2026, 8, 10), 100.0, 200.0)],
            schema={"code": pl.Utf8, "trade_date": pl.Date,
                    "inflow_share": pl.Float64, "inflow_amount": pl.Float64},
            orient="row"))
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.leaderboard_rows(repo=None, broad={"A"}, sort="amount",
                                        order="desc", page=1, size=20, broad_only=False)
        rows = {r["symbol"]: r for r in out["rows"]}
        a = rows["A"]
        assert a["share"] == pytest.approx(2.0)          # 20000万份 = 2亿份
        assert a["market_cap"] is not None
        assert a["market_cap"] == pytest.approx(4.0)      # 2亿份 x nav 2.0 = 4亿

    def test_sort_and_broad_only_and_pagination(self, monkeypatch):
        self._write_enriched()
        store.save_broad(["A"])
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.leaderboard_rows(repo=None, broad={"A"}, sort="change_pct",
                                        order="asc", page=1, size=2, broad_only=True)
        assert out["total"] == 1 and out["rows"][0]["symbol"] == "A"
        out2 = etf_fund.leaderboard_rows(repo=None, broad={"A"}, sort="change_pct",
                                         order="asc", page=2, size=2, broad_only=False)
        assert out2["total"] == 3 and len(out2["rows"]) == 1  # 第二页剩 1 行


# ===== Task 3: Sync 模块测试 =====


class _FakeProvider:
    def __init__(self, config):
        self.config = config


def _fake_provider_config(url, token_env):
    return CustomSourceConfig(
        name="amaz", display_name="Amaz",
        auth=AuthConfig(type="bearer", token_env=token_env),
        datasets={"daily": DatasetConfig(url=url)},
    )


def _fake_provider_config_multi():
    return CustomSourceConfig(
        name="amaz", display_name="Amaz",
        auth=AuthConfig(type="bearer", token_env="TK"),
        datasets={
            "daily": DatasetConfig(url="http://daily-host:3021/x/daily"),
            "etf": DatasetConfig(url="http://etf-host:3021/y/etf"),
        },
    )


class TestSyncHelpers:
    def test_extract_base_url(self):
        assert sync.extract_base_url("http://127.0.0.1:3021/api/daily") == "http://127.0.0.1:3021"
        assert sync.extract_base_url("http://127.0.0.1:3021/") == "http://127.0.0.1:3021"
        assert sync.extract_base_url("https://x.com:8443") == "https://x.com:8443"

    def test_fingerprint_stable_and_sensitive(self):
        a = sync.source_fingerprint("http://h:3021", "TOKEN_A")
        assert a == sync.source_fingerprint("http://h:3021", "TOKEN_A")
        assert a != sync.source_fingerprint("http://h:3021", "TOKEN_B")
        assert a != sync.source_fingerprint("http://h:3022", "TOKEN_A")
        assert len(a) == 16

    def test_month_ranges(self):
        r = sync._month_ranges(date(2026, 1, 15), date(2026, 3, 10))
        assert r == [(date(2026, 1, 15), date(2026, 1, 31)),
                     (date(2026, 2, 1), date(2026, 2, 28)),
                     (date(2026, 3, 1), date(2026, 3, 10))]

    def test_resolve_source_unconfigured(self):
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 409

    def test_resolve_source_deleted(self, monkeypatch):
        store.save_config({"data_source": "ghost", "source_fingerprint": "x"})
        monkeypatch.setattr(sync.loader, "get_provider",
                            lambda name: (_ for _ in ()).throw(ValueError("nf")))
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 409 and "已删除" in ei.value.args[0]

    def test_resolve_source_fingerprint_changed(self, monkeypatch):
        cfg = _fake_provider_config(url="http://h:3021/api/daily", token_env="TK")
        monkeypatch.setattr(sync.loader, "get_provider", lambda name: _FakeProvider(cfg))
        store.save_config({"data_source": "amaz",
                           "source_fingerprint": sync.source_fingerprint("http://h:9999", "TK")})
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 409 and "已变化" in ei.value.args[0]

    def test_resolve_source_token_missing(self, monkeypatch):
        cfg = _fake_provider_config(url="http://h:3021/api/daily", token_env="TK_MISSING")
        monkeypatch.setattr(sync.loader, "get_provider", lambda name: _FakeProvider(cfg))
        monkeypatch.setattr(sync, "_token_from_env", lambda name: None)
        store.save_config({"data_source": "amaz",
                           "source_fingerprint": sync.source_fingerprint("http://h:3021", "TK_MISSING")})
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 401 and "TK_MISSING" in ei.value.args[0]

    def test_resolve_source_ok_prefers_etf_dataset(self, monkeypatch):
        cfg = _fake_provider_config_multi()
        monkeypatch.setattr(sync.loader, "get_provider", lambda name: _FakeProvider(cfg))
        monkeypatch.setattr(sync, "_token_from_env", lambda name: "tok")
        fp = sync.source_fingerprint("http://etf-host:3021", "TK")
        store.save_config({"data_source": "amaz", "source_fingerprint": fp})
        out = sync.resolve_source()
        assert out["base_url"] == "http://etf-host:3021"
        assert out["headers"] == {"Authorization": "Bearer tok"}
        assert out["token_env"] == "TK"


class TestSyncRun:
    @pytest.mark.asyncio
    async def test_incremental_merges_and_recomputes(self, monkeypatch):
        monkeypatch.setattr(sync, "resolve_source", lambda: {
            "name": "amaz", "base_url": "http://h:3021",
            "headers": {}, "fingerprint": "fp", "warning": None,
            "token_env": "TK"})
        calls = []

        async def fake_fetch(src, path, start, end):
            calls.append((path, start, end))
            if path == "/etf/share":
                return _share_df([("A", end, 100.0, end)])
            return _nav([("A", end, 2.0)])

        monkeypatch.setattr(sync, "_fetch_range", fake_fetch)
        monkeypatch.setattr(sync.etf_fund, "recompute_inflow", lambda repo: None)
        out = await sync.run_incremental(repo=None)
        assert out["ok"] is True and len(calls) == 2
        assert store.read_share().height == 1
        assert store.load_state()["last_sync"] is not None

    @pytest.mark.asyncio
    async def test_backfill_months_resume(self, monkeypatch):
        monkeypatch.setattr(sync, "resolve_source", lambda: {
            "name": "amaz", "base_url": "http://h:3021",
            "headers": {}, "fingerprint": "fp", "warning": None,
            "token_env": "TK"})
        st = store.load_state()
        st["completed_months"] = ["2026-01"]
        store.save_state(st)
        done = []

        async def fake_fetch(src, path, start, end):
            done.append((start, end))
            return _share_df([]) if path == "/etf/share" else _nav([])

        monkeypatch.setattr(sync, "_fetch_range", fake_fetch)
        monkeypatch.setattr(sync.etf_fund, "recompute_inflow", lambda repo: None)
        await sync.run_backfill(None, date(2026, 1, 1), date(2026, 2, 28))
        # 2026-01 已完成跳过, 只拉 2026-02 (share+nav 各一次)
        assert done == [(date(2026, 2, 1), date(2026, 2, 28)),
                        (date(2026, 2, 1), date(2026, 2, 28))]
        assert "2026-02" in store.load_state()["completed_months"]
        assert store.load_state()["backfill"]["running"] is False


# ===== Task 4: API 模块测试 =====


def _req():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))


class TestApi:
    def test_put_broad_rejects_non_etf(self, monkeypatch):
        monkeypatch.setattr(
            etf_api, "_etf_symbols",
            lambda repo: {"510300.SH", "510500.SH"})
        with pytest.raises(Exception) as ei:
            etf_api.put_broad(_req(), etf_api.BroadIn(symbols=["510300.SH", "600000.SH"]))
        assert "600000.SH" in str(ei.value)

    def test_put_broad_ok(self, monkeypatch):
        monkeypatch.setattr(etf_api, "_etf_symbols", lambda repo: {"510300.SH"})
        out = etf_api.put_broad(_req(), etf_api.BroadIn(symbols=["510300.SH"]))
        assert out["symbols"] == ["510300.SH"]
        assert store.load_broad() == ["510300.SH"]

    def test_leaderboard_size_clamped(self, monkeypatch):
        captured = {}

        def fake_rows(repo, broad, sort, order, page, size, broad_only):
            captured["size"] = size
            return {"rows": [], "total": 0, "data_date": None}

        monkeypatch.setattr(etf_api.etf_fund, "leaderboard_rows", fake_rows)
        etf_api.leaderboard(_req(), sort="amount", order="desc", page=1,
                            size=9999, broad_only=False)
        assert captured["size"] == 100

    def test_sync_unconfigured_409(self, monkeypatch):
        async def fake_trigger(mode, repo, start, end):
            raise sync.SyncError("未配置数据源", 409)
        monkeypatch.setattr(etf_api.etf_fund_sync, "trigger", fake_trigger)
        with pytest.raises(Exception) as ei:
            asyncio.run(etf_api.post_sync(_req(), etf_api.SyncIn(mode="incremental")))
        assert getattr(ei.value, "status_code", None) == 409
