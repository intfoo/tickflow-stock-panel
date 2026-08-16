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
        store.save_config({"data_source": "amaz", "overlay_index": "000300.SH"})
        assert store.load_config()["data_source"] == "amaz"

    def test_broad_roundtrip(self):
        assert store.load_broad() == {"symbols": [], "customized": False}
        store.save_broad(["510300.SH", "510500.SH"])
        assert store.load_broad() == {"symbols": ["510300.SH", "510500.SH"],
                                       "customized": True}

    def test_state_roundtrip(self):
        st = store.load_state()
        assert st["backfill"]["running"] is False and st["completed_chunks"] == []
        st["completed_chunks"] = ["2026-07-01"]
        store.save_state(st)
        assert store.load_state()["completed_chunks"] == ["2026-07-01"]


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
        assert out["broad_count"] == 2
        series = {r["trade_date"]: r["amount"] for r in out["series"]}
        # 8-5: B1 20万=0.002亿; 8-6: B1+B2=12万=0.0012亿; X 不计
        assert abs(series["2026-08-05"] - 0.002) < 1e-9
        assert abs(series["2026-08-06"] - 0.0012) < 1e-9
        # 起点 = 最早数据日 (8-5), 之前的日历日 (8-3/8-4) 属未知区间不补 0
        assert out["series"][0]["trade_date"] == "2026-08-05"
        assert "2026-08-04" not in series
        assert out["stats"]["data_end_date"] == "2026-08-06"
        assert out["series"][-1]["trade_date"] == "2026-08-06"  # 尾部不补 0, 截到数据日

    def test_flow_empty(self, monkeypatch):
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.fund_flow(repo=None, days=60)
        assert out["series"] == [] and out["stats"]["data_end_date"] is None
        assert out["broad_count"] == 0


class TestCalendar:
    def test_calendar_union_covers_stale_index(self):
        """指数日K stale (停在 07-28) 时, share/nav 自身日期仍补齐日历尾部。

        回归: 日历截断曾导致尾部 share 变动 join 不到 nav, inflow_amount 全 null。
        """

        class _Repo:
            def get_index_daily(self, symbol, start, end, columns=None):
                return pl.DataFrame({"date": [date(2026, 7, 27), date(2026, 7, 28)]})

        store.merge_share(_share_df([("A", date(2026, 8, 14), 100.0, date(2026, 8, 15))]))
        cal = etf_fund.trading_calendar(_Repo(), date(2026, 7, 1), date(2026, 8, 16))
        assert cal[0] == date(2026, 7, 27)
        assert cal[-1] == date(2026, 8, 14)  # 不被指数 stale 截断


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
        out = etf_fund.leaderboard_rows(repo=None, sort="change_pct",
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
        out = etf_fund.leaderboard_rows(repo=None, sort="amount",
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
        out = etf_fund.leaderboard_rows(repo=None, sort="change_pct",
                                        order="asc", page=1, size=2, broad_only=True)
        assert out["total"] == 1 and out["rows"][0]["symbol"] == "A"
        out2 = etf_fund.leaderboard_rows(repo=None, sort="change_pct",
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

    def test_chunk_ranges(self):
        r = sync._chunk_ranges(date(2026, 1, 15), date(2026, 3, 10), 30)
        assert r == [(date(2026, 1, 15), date(2026, 2, 13)),
                     (date(2026, 2, 14), date(2026, 3, 10))]
        # 批次大于区间 → 单批
        assert sync._chunk_ranges(date(2026, 1, 1), date(2026, 1, 10), 30) == [
            (date(2026, 1, 1), date(2026, 1, 10))]

    def test_resolve_source_unconfigured(self):
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 409

    def test_resolve_source_deleted(self, monkeypatch):
        store.save_config({"data_source": "ghost"})
        monkeypatch.setattr(sync.loader, "get_provider",
                            lambda name: (_ for _ in ()).throw(ValueError("nf")))
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 409 and "已删除" in ei.value.args[0]

    def test_resolve_source_token_missing(self, monkeypatch):
        cfg = _fake_provider_config(url="http://h:3021/api/daily", token_env="TK_MISSING")
        monkeypatch.setattr(sync.loader, "get_provider", lambda name: _FakeProvider(cfg))
        monkeypatch.setattr(sync, "_token_from_env", lambda name: None)
        store.save_config({"data_source": "amaz"})
        with pytest.raises(sync.SyncError) as ei:
            sync.resolve_source()
        assert ei.value.status == 401 and "TK_MISSING" in ei.value.args[0]

    def test_resolve_source_ok_prefers_etf_dataset(self, monkeypatch):
        cfg = _fake_provider_config_multi()
        monkeypatch.setattr(sync.loader, "get_provider", lambda name: _FakeProvider(cfg))
        monkeypatch.setattr(sync, "_token_from_env", lambda name: "tok")
        store.save_config({"data_source": "amaz"})
        out = sync.resolve_source()
        assert out["base_url"] == "http://etf-host:3021"
        assert out["headers"] == {"Authorization": "Bearer tok"}
        assert out["token_env"] == "TK"


class TestSyncRun:
    @pytest.mark.asyncio
    async def test_incremental_merges_and_recomputes(self, monkeypatch):
        monkeypatch.setattr(sync, "resolve_source", lambda: {
            "name": "amaz", "base_url": "http://h:3021",
            "headers": {}, "warning": None, "token_env": "TK"})
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
    async def test_backfill_chunks_resume(self, monkeypatch):
        monkeypatch.setattr(sync, "resolve_source", lambda: {
            "name": "amaz", "base_url": "http://h:3021",
            "headers": {}, "warning": None, "token_env": "TK"})
        st = store.load_state()
        st["completed_chunks"] = ["2026-01-01"]
        store.save_state(st)
        done = []

        async def fake_fetch(src, path, start, end):
            done.append((start, end))
            return _share_df([]) if path == "/etf/share" else _nav([])

        monkeypatch.setattr(sync, "_fetch_range", fake_fetch)
        monkeypatch.setattr(sync.etf_fund, "recompute_inflow", lambda repo: None)
        # batch_days=31 → 两批: (01-01~01-31) 已完成跳过, (02-01~02-28) 拉取
        await sync.run_backfill(None, date(2026, 1, 1), date(2026, 2, 28), batch_days=31)
        assert done == [(date(2026, 2, 1), date(2026, 2, 28)),
                        (date(2026, 2, 1), date(2026, 2, 28))]
        assert "2026-02-01" in store.load_state()["completed_chunks"]
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
        assert out["is_default"] is False
        assert store.load_broad() == {"symbols": ["510300.SH"], "customized": True}

    def test_leaderboard_size_clamped(self, monkeypatch):
        captured = {}

        def fake_rows(repo, sort, order, page, size, broad_only):
            captured["size"] = size
            return {"rows": [], "total": 0, "data_date": None}

        monkeypatch.setattr(etf_api.etf_fund, "leaderboard_rows", fake_rows)
        etf_api.leaderboard(_req(), sort="amount", order="desc", page=1,
                            size=9999, broad_only=False)
        assert captured["size"] == 100

    def test_sync_unconfigured_409(self, monkeypatch):
        async def fake_trigger(mode, repo, start, end, batch_days=30):
            raise sync.SyncError("未配置数据源", 409)
        monkeypatch.setattr(etf_api.etf_fund_sync, "trigger", fake_trigger)
        with pytest.raises(Exception) as ei:
            asyncio.run(etf_api.post_sync(_req(), etf_api.SyncIn(mode="incremental")))
        assert getattr(ei.value, "status_code", None) == 409


class TestBroadPresets:
    """宽基推荐清单与四态 effective_broad 测试 (spec §6)。"""

    def _instruments(self, symbols):
        return pl.DataFrame({"symbol": symbols, "name": [f"ETF{s}" for s in symbols]})

    def test_preset_list_has_19(self):
        from app.services.etf_broad_presets import PRESET_BROAD_ETFS
        assert len(PRESET_BROAD_ETFS) == 19
        assert len(set(PRESET_BROAD_ETFS)) == 19  # 无重复

    def test_preset_symbols_intersect(self):
        from app.services.etf_broad_presets import PRESET_BROAD_ETFS, preset_symbols
        # 清单中存在的全部保留, 不存在的静默剔除
        inst = self._instruments([PRESET_BROAD_ETFS[0], "999999.XX", PRESET_BROAD_ETFS[5]])
        out = preset_symbols(inst)
        assert PRESET_BROAD_ETFS[0] in out
        assert PRESET_BROAD_ETFS[5] in out
        assert "999999.XX" not in out

    def test_preset_symbols_none_empty(self):
        from app.services.etf_broad_presets import preset_symbols
        assert preset_symbols(None) == []
        assert preset_symbols(pl.DataFrame()) == []

    def test_effective_broad_default(self):
        """默认态 (未配置): 返回 preset∩instruments, is_default=True。"""
        from app.services.etf_broad_presets import PRESET_BROAD_ETFS, effective_broad
        inst = self._instruments([*PRESET_BROAD_ETFS[:3], "999999.XX"])
        syms, is_default = effective_broad(inst)
        assert is_default is True
        assert syms == set(PRESET_BROAD_ETFS[:3])

    def test_effective_broad_customized(self):
        """自定义态: 返回用户清单, is_default=False。"""
        from app.services.etf_broad_presets import effective_broad
        store.save_broad(["510300.SH", "159919.SZ"])
        syms, is_default = effective_broad(self._instruments(["510300.SH", "159919.SZ"]))
        assert is_default is False
        assert syms == {"510300.SH", "159919.SZ"}

    def test_effective_broad_empty_customized(self):
        """用户保存空清单 (customized=True): 返回空, is_default=False。"""
        from app.services.etf_broad_presets import effective_broad
        store.save_broad([])  # save_broad([]) → {"symbols": [], "customized": True}
        syms, is_default = effective_broad(self._instruments(["510300.SH"]))
        assert is_default is False
        assert syms == set()

    def test_effective_broad_after_reset(self):
        """reset 后: 回默认态, is_default=True。"""
        from app.services.etf_broad_presets import PRESET_BROAD_ETFS, effective_broad
        store.save_broad(["510300.SH"])
        store.reset_broad()
        inst = self._instruments(PRESET_BROAD_ETFS[:2])
        syms, is_default = effective_broad(inst)
        assert is_default is True
        assert syms == set(PRESET_BROAD_ETFS[:2])

    def test_effective_broad_none_instruments(self):
        """instruments=None 不抛异常。"""
        from app.services.etf_broad_presets import effective_broad
        # 默认态 + None → (set(), True)
        syms, is_default = effective_broad(None)
        assert is_default is True
        assert syms == set()
        # 自定义态 + None → (用户清单, False)
        store.save_broad(["510300.SH"])
        syms, is_default = effective_broad(None)
        assert is_default is False
        assert syms == {"510300.SH"}

    def test_effective_broad_empty_dataframe(self):
        """instruments 为空 DataFrame 不抛异常。"""
        from app.services.etf_broad_presets import effective_broad
        syms, is_default = effective_broad(pl.DataFrame({"symbol": [], "name": []}))
        assert is_default is True
        assert syms == set()

    def test_load_broad_legacy_list_format(self):
        """旧格式 (纯 JSON list) 兼容 → customized=True。"""
        import json
        path = store.data_dir() / "broad_etf.json"
        path.write_text(json.dumps(["510300.SH", "510500.SH"]), encoding="utf-8")
        out = store.load_broad()
        assert out == {"symbols": ["510300.SH", "510500.SH"], "customized": True}

    def test_api_get_broad_default(self, monkeypatch):
        """GET /broad 默认态: symbols=preset∩instruments, is_default=True。"""
        # repo=None 时 instruments=None → 默认态 symbols=[]
        out = etf_api.get_broad(_req())
        assert out["is_default"] is True
        assert out["symbols"] == []
        assert out["presets"] == []  # instruments=None → preset_symbols 返回 []

    def test_api_get_broad_customized(self, monkeypatch):
        """GET /broad 自定义态: symbols=用户清单, is_default=False。"""
        store.save_broad(["510300.SH"])
        out = etf_api.get_broad(_req())
        assert out["is_default"] is False
        assert out["symbols"] == ["510300.SH"]

    def test_api_reset_broad(self):
        """POST /broad/reset: 回默认态。"""
        store.save_broad(["510300.SH"])
        out = etf_api.reset_broad(_req())
        assert out["is_default"] is True
        assert out["symbols"] == []
        assert store.load_broad() == {"symbols": [], "customized": False}

    def test_api_get_instruments_with_market_cap(self, monkeypatch):
        """GET /instruments: items 含 market_cap (有/无资金数据两态)。"""
        # 设置 share+nav → _latest_share_nav 有值
        store.merge_share(_share_df([("A", date(2026, 8, 10), 20000.0, date(2026, 8, 11))]))
        store.merge_nav(_nav([("A", date(2026, 8, 10), 2.0)]))

        class _Repo:
            def get_etf_instruments(self):
                return pl.DataFrame({"symbol": ["A", "B"], "name": ["ETF_A", "ETF_B"]})

        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_Repo())))
        out = etf_api.get_instruments(req)
        items = {r["symbol"]: r for r in out["items"]}
        # A: 20000万份 x 2元 / 1e4 = 4亿
        assert items["A"]["market_cap"] is not None
        assert items["A"]["market_cap"] == pytest.approx(4.0)
        # B: 无资金数据 → None
        assert items["B"]["market_cap"] is None

    def test_api_get_instruments_no_fund_data(self, monkeypatch):
        """GET /instruments: 无 share/nav 时 market_cap 全 None。"""
        class _Repo:
            def get_etf_instruments(self):
                return pl.DataFrame({"symbol": ["A"], "name": ["ETF_A"]})

        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_Repo())))
        out = etf_api.get_instruments(req)
        assert len(out["items"]) == 1
        assert out["items"][0]["market_cap"] is None

    def test_fund_flow_default_uses_preset(self, monkeypatch):
        """fund_flow 默认态: 使用推荐清单, is_default=True, broad_count=有效集合大小。"""
        from app.services.etf_broad_presets import PRESET_BROAD_ETFS
        # 写入 inflow 数据, code 命中 preset
        code = PRESET_BROAD_ETFS[0]
        store.write_inflow(pl.DataFrame(
            [(code, date(2026, 8, 5), 10.0, 20.0),
             (code, date(2026, 8, 6), 5.0, 10.0)],
            schema={"code": pl.Utf8, "trade_date": pl.Date,
                    "inflow_share": pl.Float64, "inflow_amount": pl.Float64},
            orient="row"))
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)

        class _Repo:
            def get_etf_instruments(self):
                return pl.DataFrame({"symbol": [code], "name": [f"ETF{code}"]})

        out = etf_fund.fund_flow(repo=_Repo(), days=60)
        assert out["is_default"] is True
        assert out["broad_count"] == 1  # preset∩instruments = {code}
        assert len(out["series"]) > 0  # 有数据

    def test_fund_flow_empty_default(self, monkeypatch):
        """fund_flow 默认态 + repo=None: broad 为空, is_default=True。"""
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.fund_flow(repo=None, days=60)
        assert out["is_default"] is True
        assert out["broad_count"] == 0
        assert out["series"] == []

    def test_fund_flow_customized_empty(self, monkeypatch):
        """fund_flow 自定义空清单: is_default=False, broad_count=0, series 空。"""
        store.save_broad([])  # customized=True, symbols=[]
        monkeypatch.setattr(etf_fund, "trading_calendar", lambda repo, s, e: CAL)
        out = etf_fund.fund_flow(repo=None, days=60)
        assert out["is_default"] is False
        assert out["broad_count"] == 0
        assert out["series"] == []
