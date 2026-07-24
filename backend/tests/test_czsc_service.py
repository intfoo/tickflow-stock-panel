"""缠论分析服务测试。

测试用例:
  - test_is_available: is_available 返回 bool
  - test_analyze_degradation: czsc 不可用时返回 {available:false}
  - test_analyze_with_mock: 用 czsc.mock 数据走通 analyze (importorskip 跳过未装环境)
  - test_serialize_field_mapping: 验证 顶分型→top, 向上→up 映射
  - test_signal_marker_extraction: 验证买卖标记提取正确
"""
from __future__ import annotations

import pytest

from app.services import czsc_service


# ---------------------------------------------------------------------------
# test_is_available
# ---------------------------------------------------------------------------
def test_is_available():
    """is_available() 应返回 bool 类型。"""
    result = czsc_service.is_available()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# test_analyze_degradation
# ---------------------------------------------------------------------------
def test_analyze_degradation(monkeypatch):
    """czsc 不可用时 analyze 返回 {available:false, message:...}。"""
    monkeypatch.setattr(czsc_service, "is_available", lambda: False)

    # repo 不需要真实实现, 因为降级路径不会调到 repo
    result = czsc_service.analyze(repo=None, symbol="000001.SZ", freq="日线")

    assert result["available"] is False
    assert "message" in result
    assert "czsc" in result["message"]


# ---------------------------------------------------------------------------
# test_analyze_with_mock
# ---------------------------------------------------------------------------
def test_analyze_with_mock():
    """用 czsc.mock.generate_symbol_kines 生成假数据走通 analyze。

    需要 czsc 已安装; 未装环境自动跳过。
    """
    pytest.importorskip("czsc")
    import polars as pl
    from czsc.mock import generate_symbol_kines

    # 生成模拟日K数据 (czsc 返回 pandas DataFrame)
    pdf = generate_symbol_kines("000001", "日线", "20240101", "20250601")

    # 转为 polars DataFrame, 列名对齐 tickflow 日K schema
    # czsc mock 列: dt, symbol, open, close, high, low, vol, amount
    # tickflow 列: date, symbol, open, high, low, close, volume, amount
    df = pl.DataFrame({
        "date": pdf["dt"].dt.date.tolist() if hasattr(pdf["dt"].dt, "date") else pdf["dt"].tolist(),
        "symbol": pdf["symbol"].tolist(),
        "open": pdf["open"].tolist(),
        "high": pdf["high"].tolist(),
        "low": pdf["low"].tolist(),
        "close": pdf["close"].tolist(),
        "volume": pdf["vol"].tolist(),
        "amount": pdf["amount"].tolist(),
    })

    # 构造一个 mock repo, resolve_asset_type + get_daily_asset 返回我们的 df
    class MockRepo:
        def resolve_asset_type(self, symbol):
            return "stock"

        def get_daily_asset(self, asset_type, symbol, start, end):
            return df

    repo = MockRepo()
    result = czsc_service.analyze(repo, "000001.SZ", freq="日线")

    assert result["available"] is True
    assert result["symbol"] == "000001.SZ"
    assert result["freq"] == "日线"
    assert isinstance(result["bars"], list)
    assert len(result["bars"]) > 0
    assert isinstance(result["fx_list"], list)
    assert isinstance(result["bi_list"], list)
    assert isinstance(result["zs_list"], list)
    assert isinstance(result["signals"], list)
    assert isinstance(result["signal_markers"], list)

    # 验证 bars 结构
    bar = result["bars"][0]
    assert "date" in bar
    assert "open" in bar
    assert "high" in bar
    assert "low" in bar
    assert "close" in bar
    assert "volume" in bar

    # 日期格式应为 YYYY-MM-DD
    assert len(bar["date"]) == 10

    # 如果有分型, 验证 mark 是 top/bottom
    for fx in result["fx_list"]:
        assert fx["mark"] in ("top", "bottom")

    # 如果有笔, 验证 direction 是 up/down
    for bi in result["bi_list"]:
        assert bi["direction"] in ("up", "down")


# ---------------------------------------------------------------------------
# test_serialize_field_mapping
# ---------------------------------------------------------------------------
def test_serialize_field_mapping():
    """验证 顶分型→top, 底分型→bottom, 向上→up, 向下→down 映射。"""
    pytest.importorskip("czsc")

    # 直接测试映射常量
    assert czsc_service._MARK_MAP["顶分型"] == "top"
    assert czsc_service._MARK_MAP["底分型"] == "bottom"
    assert czsc_service._DIR_MAP["向上"] == "up"
    assert czsc_service._DIR_MAP["向下"] == "down"

    # 构造 mock CZSC 对象测试 _serialize
    from unittest.mock import MagicMock
    import pandas as pd

    # Mock FX
    fx_top = MagicMock()
    fx_top.dt = pd.Timestamp("2025-01-08")
    fx_top.fx = 10.5
    fx_top.mark.value = "顶分型"

    fx_bottom = MagicMock()
    fx_bottom.dt = pd.Timestamp("2025-01-03")
    fx_bottom.fx = 10.0
    fx_bottom.mark.value = "底分型"

    # Mock BI
    bi_up = MagicMock()
    bi_up.fx_a = fx_bottom
    bi_up.fx_b = fx_top
    bi_up.direction.value = "向上"

    # Mock bar
    bar = MagicMock()
    bar.dt = pd.Timestamp("2025-01-02")
    bar.open = 10.1
    bar.high = 10.3
    bar.low = 10.0
    bar.close = 10.2
    bar.vol = 123456

    # Mock CZSC
    c = MagicMock()
    c.bars_raw = [bar]
    c.fx_list = [fx_top, fx_bottom]
    c.bi_list = [bi_up]

    signals_result = [
        {
            "dt": "2025-01-02T00:00:00+00:00",
            "close": 10.2,
            "日线_D1B_BUY1": "其他_其他_任意_0",
            "日线_D1B_SELL1": "其他_其他_任意_0",
            "日线_D1_表里关系V230102": "向上_顶分_任意_0",
        }
    ]

    result = czsc_service._serialize(c, signals_result, "000001.SZ", "日线")

    # 验证分型映射
    assert result["fx_list"][0]["mark"] == "top"
    assert result["fx_list"][1]["mark"] == "bottom"

    # 验证笔方向映射
    assert result["bi_list"][0]["direction"] == "up"

    # 验证日期格式化 (naive Timestamp → YYYY-MM-DD)
    assert result["fx_list"][0]["dt"] == "2025-01-08"
    assert result["bi_list"][0]["a_dt"] == "2025-01-03"
    assert result["bi_list"][0]["b_dt"] == "2025-01-08"

    # 验证信号 dt 格式化 (ISO 带时区 → YYYY-MM-DD)
    assert result["signals"][0]["dt"] == "2025-01-02"
    # 信号 dict 的 bar 原始字段应被过滤掉
    assert "close" not in result["signals"][0]
    assert "日线_D1B_BUY1" in result["signals"][0]


# ---------------------------------------------------------------------------
# test_signal_marker_extraction
# ---------------------------------------------------------------------------
def test_signal_marker_extraction():
    """验证买卖标记提取: value 以"一买"/"一卖"开头且不含「其他」才生成 marker。"""
    pytest.importorskip("czsc")

    from unittest.mock import MagicMock
    import pandas as pd

    # 构造信号 dict list — 模拟4个 bar:
    # bar1: 一买触发 (应生成 buy marker)
    # bar2: 一卖触发 (应生成 sell marker)
    # bar3: 未触发 (其他_其他_任意_0, 不生成)
    # bar4: 一买 + 一卖同时触发 (两个都生成)
    signals_result = [
        {
            "dt": "2025-02-20T00:00:00+00:00",
            "close": 10.3,
            "日线_D1B_BUY1": "一买_5笔_任意_0",
            "日线_D1B_SELL1": "其他_其他_任意_0",
        },
        {
            "dt": "2025-03-10T00:00:00+00:00",
            "close": 11.2,
            "日线_D1B_BUY1": "其他_其他_任意_0",
            "日线_D1B_SELL1": "一卖_11笔_任意_0",
        },
        {
            "dt": "2025-04-01T00:00:00+00:00",
            "close": 10.8,
            "日线_D1B_BUY1": "其他_其他_任意_0",
            "日线_D1B_SELL1": "其他_其他_任意_0",
        },
        {
            "dt": "2025-05-15T00:00:00+00:00",
            "close": 10.5,
            "日线_D1B_BUY1": "一买_7笔_任意_0",
            "日线_D1B_SELL1": "一卖_7笔_任意_0",
        },
    ]

    # 构造 bars (提供 close 价格)
    bars = []
    for sig in signals_result:
        bar = MagicMock()
        # _fmt_dt 对 ISO 字符串会 parse, 所以这里也用一致的方式
        dt_str = sig["dt"][:10]
        bar.dt = pd.Timestamp(dt_str)
        bar.close = sig["close"]
        bars.append(bar)

    markers = czsc_service._extract_signal_markers(signals_result, bars)

    # 应该有 4 个 marker: bar1 buy, bar2 sell, bar4 buy+sell
    assert len(markers) == 4

    # bar1: buy
    assert markers[0]["dt"] == "2025-02-20"
    assert markers[0]["kind"] == "buy"
    assert markers[0]["label"] == "一类买点"
    assert markers[0]["price"] == 10.3

    # bar2: sell
    assert markers[1]["dt"] == "2025-03-10"
    assert markers[1]["kind"] == "sell"
    assert markers[1]["label"] == "一类卖点"
    assert markers[1]["price"] == 11.2

    # bar4: buy + sell (两个 marker)
    bar4_markers = [m for m in markers if m["dt"] == "2025-05-15"]
    assert len(bar4_markers) == 2
    kinds = {m["kind"] for m in bar4_markers}
    assert kinds == {"buy", "sell"}


# ---------------------------------------------------------------------------
# Task 1: FREQ_CONFIG + analyze 新签名
# ---------------------------------------------------------------------------
def test_freq_config_complete():
    """9 档频率配置齐全, freq_str 正确。"""
    assert set(czsc_service.FREQ_CONFIG.keys()) == {
        "日线", "周线", "月线", "季线", "1分钟", "5分钟", "15分钟", "30分钟", "60分钟"
    }
    cfg = czsc_service.FREQ_CONFIG["季线"]
    assert cfg.freq_str == "季线"
    assert cfg.family == "daily"
    assert cfg.default_days == 40
    assert cfg.init_n == 8

    cfg_m = czsc_service.FREQ_CONFIG["60分钟"]
    assert cfg_m.family == "minute"
    assert cfg_m.default_days == 60
    assert cfg_m.max_days == 120
    assert cfg_m.init_n == 20


def test_daily_calendar_factor():
    """周/月/季线 default_days 是目标根数, 换算系数应使取到的日K足够 resample。
    回归: 修复前周线 default_days=100 用 days*2=200 日历日 → 仅 ~28 周。
    """
    f = czsc_service._DAILY_CALENDAR_FACTOR
    assert f["日线"] == 2
    assert f["周线"] == 7    # 100 周 → 700 日历日 ≈ 500 交易日 ≈ 100 周
    assert f["月线"] == 30   # 60 月 → 1800 日历日 ≈ 5 年
    assert f["季线"] == 90   # 40 季 → 3600 日历日 ≈ 10 年


def test_analyze_signature_daily():
    """analyze 新签名: freq 默认日线, czsc 未装时返回降级。"""
    from unittest.mock import patch
    with patch.object(czsc_service, "is_available", return_value=False):
        res = czsc_service.analyze(repo=None, symbol="000001.SZ", freq="日线")
    assert res["available"] is False


def test_analyze_invalid_freq():
    """未知 freq 应抛 ValueError。"""
    # is_available 为 True 时才会校验 freq；用 monkeypatch 模拟已装
    from unittest.mock import patch
    with patch.object(czsc_service, "is_available", return_value=True):
        with pytest.raises(ValueError, match="不支持的频率"):
            czsc_service.analyze(repo=None, symbol="000001.SZ", freq="2分钟")


def test_fmt_dt_minute():
    """_fmt_dt minute=True 时格式为 YYYY-MM-DD HH:MM。"""
    import pandas as pd
    ts = pd.Timestamp("2025-01-06 09:35:00")
    assert czsc_service._fmt_dt(ts, minute=True) == "2025-01-06 09:35"
    # 默认 minute=False → 日期
    assert czsc_service._fmt_dt(ts) == "2025-01-06"


# ---------------------------------------------------------------------------
# Task 2: 周/月/季 polars resample
# ---------------------------------------------------------------------------
def test_resample_daily_weekly():
    """周线聚合: first/last/max/min/sum 正确。"""
    import polars as pl
    from datetime import date

    # 10 个交易日跨 2 周 (2025-01-06~10 为第1周, 01-13~17 为第2周)
    df = pl.DataFrame({
        "date": [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9), date(2025, 1, 10),
                 date(2025, 1, 13), date(2025, 1, 14), date(2025, 1, 15), date(2025, 1, 16), date(2025, 1, 17)],
        "symbol": ["000001.SZ"] * 10,
        "open":   [1.0, 1.1, 1.2, 1.3, 1.4,  2.0, 2.1, 2.2, 2.3, 2.4],
        "high":   [2.0, 2.0, 2.0, 2.0, 2.0,  3.0, 3.0, 3.0, 3.0, 3.0],
        "low":    [0.5, 0.5, 0.5, 0.5, 0.5,  1.5, 1.5, 1.5, 1.5, 1.5],
        "close":  [1.5, 1.5, 1.5, 1.5, 1.5,  2.5, 2.5, 2.5, 2.5, 2.5],
        "volume": [100] * 10,
        "amount": [1000] * 10,
    })
    out = czsc_service._resample_daily(df, "周线")
    assert out.height == 2  # 2 周
    row0 = out.row(0, named=True)
    assert row0["open"] == 1.0    # first
    assert row0["close"] == 1.5   # last
    assert row0["high"] == 2.0    # max
    assert row0["low"] == 0.5     # min
    assert row0["volume"] == 500  # sum(5)
    assert row0["amount"] == 5000

    row1 = out.row(1, named=True)
    assert row1["open"] == 2.0
    assert row1["close"] == 2.5
    assert row1["volume"] == 500


def test_resample_daily_monthly():
    """月线聚合: 跨月正确分组。"""
    import polars as pl
    from datetime import date

    df = pl.DataFrame({
        "date": [date(2025, 1, 6), date(2025, 1, 20), date(2025, 2, 3), date(2025, 2, 14)],
        "symbol": ["000001.SZ"] * 4,
        "open":   [1.0, 1.5, 2.0, 2.5],
        "high":   [2.0, 2.5, 3.0, 3.5],
        "low":    [0.5, 1.0, 1.5, 2.0],
        "close":  [1.5, 2.0, 2.5, 3.0],
        "volume": [100, 200, 300, 400],
        "amount": [1000, 2000, 3000, 4000],
    })
    out = czsc_service._resample_daily(df, "月线")
    assert out.height == 2  # 1月 + 2月
    row0 = out.row(0, named=True)
    assert row0["open"] == 1.0    # first of Jan
    assert row0["close"] == 2.0   # last of Jan
    assert row0["volume"] == 300  # 100+200


def test_resample_daily_quarterly():
    """季线聚合: 3 个月一桶。"""
    import polars as pl
    from datetime import date

    df = pl.DataFrame({
        "date": [date(2025, 1, 6), date(2025, 2, 3), date(2025, 4, 1), date(2025, 5, 1)],
        "symbol": ["000001.SZ"] * 4,
        "open":   [1.0, 2.0, 3.0, 4.0],
        "high":   [5.0, 6.0, 7.0, 8.0],
        "low":    [0.5, 1.5, 2.5, 3.5],
        "close":  [2.0, 3.0, 4.0, 5.0],
        "volume": [100, 200, 300, 400],
        "amount": [1000, 2000, 3000, 4000],
    })
    out = czsc_service._resample_daily(df, "季线")
    assert out.height == 2  # Q1(1-3月) + Q2(4-6月)
    row0 = out.row(0, named=True)
    assert row0["open"] == 1.0     # first of Q1
    assert row0["close"] == 3.0    # last of Q1
    assert row0["volume"] == 300   # 100+200


# ---------------------------------------------------------------------------
# Task 3: 分钟族取数
# ---------------------------------------------------------------------------
def test_fetch_minute_series_stock():
    """_fetch_minute_series stock 路径: 本地有数据 → 返回, 不触发实时补拉。"""
    import polars as pl
    from datetime import datetime, date

    class FakeRepo:
        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            # 交易日列表与本地分钟数据一致 → missing_days 为空, 不触发 fetch_minute_single
            return pl.DataFrame({"date": [date(2025, 1, 6)]})
        def get_minute_range(self, syms, start, end, asset_type="stock"):
            return pl.DataFrame({
                "symbol": ["000001.SZ"],
                "datetime": [datetime(2025, 1, 6, 9, 31)],
                "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05],
                "volume": [100], "amount": [1000],
            })

    df = czsc_service._fetch_minute_series(FakeRepo(), "stock", "000001.SZ", 3)
    assert not df.is_empty()
    assert "datetime" in df.columns


def test_fetch_minute_series_empty():
    """_fetch_minute_series stock 路径空数据: 本地+日K均空 → 返回空 DF。"""
    import polars as pl

    class FakeRepo:
        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            return pl.DataFrame()
        def get_minute_range(self, syms, start, end, asset_type="stock"):
            return pl.DataFrame()

    df = czsc_service._fetch_minute_series(FakeRepo(), "stock", "000001.SZ", 3)
    assert df.is_empty()


def test_fetch_minute_series_live_fallback():
    """本地无分钟K但有日K → 逐日 fetch_minute_single 实时补拉拼接 (不落库)。
    回归: 修复前 stock/etf 本地空直接返回空, 不会实时拉。
    """
    import polars as pl
    from datetime import datetime, date
    from unittest.mock import patch

    class FakeRepo:
        def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
            return pl.DataFrame({"date": [date(2025, 1, 6), date(2025, 1, 7)]})
        def get_minute_range(self, syms, start, end, asset_type="stock"):
            return pl.DataFrame()  # 本地空

    def fake_fetch(symbol, day, asset_type="stock"):
        return pl.DataFrame({
            "symbol": [symbol],
            "datetime": [datetime(day.year, day.month, day.day, 9, 31)],
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05],
            "volume": [100], "amount": [1000],
        })

    with patch("app.services.kline_sync.fetch_minute_single", side_effect=fake_fetch):
        df = czsc_service._fetch_minute_series(FakeRepo(), "stock", "000001.SZ", 3)
    assert df.height == 2  # 2 个交易日各补拉 1 根
    assert "datetime" in df.columns


# ---------------------------------------------------------------------------
# Task 4: 信号目录 + 默认信号 + value 驱动标记提取
# ---------------------------------------------------------------------------
def test_signal_marker_extraction_value_driven():
    """value 驱动: BS2 的 key 不含 BUY2 但 value 含「二买」→ 能提取。"""
    from datetime import datetime
    from unittest.mock import MagicMock

    class FakeBar:
        def __init__(self, dt, close):
            self.dt = dt
            self.close = close

    bars = [FakeBar(datetime(2025, 1, 6), 10.0), FakeBar(datetime(2025, 1, 7), 11.0)]
    sigs = [
        {"dt": datetime(2025, 1, 6), "日线_D1B_BUY1": "一买_5笔_任意_0"},
        {"dt": datetime(2025, 1, 7), "日线_D1BS2辅助": "其他_其他_任意_0"},          # 不触发
        {"dt": datetime(2025, 1, 7), "日线_D1BS2辅助V230320": "二买_任意_任意_0"},  # 触发 buy 二类
        {"dt": datetime(2025, 1, 7), "日线_D1BS3辅助": "三卖_任意_任意_0"},         # 触发 sell 三类
    ]
    markers = czsc_service._extract_signal_markers(sigs, bars)
    kinds = {(m["kind"], m["label"]) for m in markers}
    assert ("buy", "一类买点") in kinds
    assert ("buy", "二类买点") in kinds
    assert ("sell", "三类卖点") in kinds
    # 不含「其他」的不触发
    assert len(markers) == 3


def test_build_signals_config():
    """信号名 + freq → [{name, freq}]。"""
    cfg = czsc_service._build_signals_config(["cxt_first_buy_V221126"], "5分钟")
    assert cfg == [{"name": "cxt_first_buy_V221126", "freq": "5分钟"}]


def test_default_signals_exists():
    """DEFAULT_SIGNALS 包含核心信号。"""
    assert "cxt_first_buy_V221126" in czsc_service.DEFAULT_SIGNALS
    assert "cxt_first_sell_V221126" in czsc_service.DEFAULT_SIGNALS
    assert len(czsc_service.DEFAULT_SIGNALS) == 6


def test_list_signals():
    """list_signals 返回分组目录 (需 czsc 已装)。"""
    pytest.importorskip("czsc")
    result = czsc_service.list_signals()
    assert result["available"] is True
    assert isinstance(result["groups"], dict)
    assert result["total"] > 40
    # 每组元素有必要字段
    for group_name, items in result["groups"].items():
        for item in items:
            assert "name" in item
            assert "category" in item
            assert "namespace" in item
            assert "param_template" in item
            assert "desc" in item


def test_list_signals_degradation(monkeypatch):
    """czsc 未装时 list_signals 返回 {available: false}。"""
    monkeypatch.setattr(czsc_service, "is_available", lambda: False)
    result = czsc_service.list_signals()
    assert result["available"] is False
    assert result["groups"] == {}
    assert result["total"] == 0


def test_parse_signal_desc():
    """_parse_signal_desc: 前缀映射优先, 再从模板提取中文, 兜底英文片段。"""
    # 1. 前缀映射 → 中文 (买卖点等关键信号)
    assert czsc_service._parse_signal_desc("cxt_first_buy_V221126", "{freq}_D{di}B_BUY1V221126") == "一买"
    assert czsc_service._parse_signal_desc("cxt_first_sell_V221126", "{freq}_D{di}B_SELL1V221126") == "一卖"
    assert czsc_service._parse_signal_desc("cxt_second_bs_V230320", "{freq}_D{di}#{ma_type}_BS2辅助V230320") == "二类买卖点"
    # 2. 前缀命中 → 中文 (cxt_bi_status 在前缀映射中)
    assert czsc_service._parse_signal_desc("cxt_bi_status_V230102", "{freq}_D1_表里关系V230102") == "笔表里关系"
    assert czsc_service._parse_signal_desc("cxt_fx_power_V221107", "{freq}_D{di}F_分型强弱V221107") == "分型强弱"
    assert czsc_service._parse_signal_desc("cxt_three_bi_V230618", "{freq}_D{di}三笔_形态V230618") == "三笔形态"
    # 2b. 无前缀命中但模板含中文 → 提取中文片段
    assert czsc_service._parse_signal_desc("bar_some_V230101", "{freq}_D1_涨停V230101") == "涨停"
    # 3. 纯英文模板无前缀命中 → 兜底英文 token
    assert czsc_service._parse_signal_desc("tas_adtm_V230603", "{freq}_D{di}N{n}_ADTMV230603") == "ADTM"
    # 4. 解析不到 → 空
    assert czsc_service._parse_signal_desc("xxx", "") == ""
