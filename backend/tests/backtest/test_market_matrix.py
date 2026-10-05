from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions
from app.backtest.matrix import build_market_matrix


def _row(symbol: str, day: int, price: float, **overrides) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "date": date(2024, 1, 1) + timedelta(days=day),
        "open": overrides.get("open", price),
        "high": overrides.get("high", price),
        "low": overrides.get("low", price),
        "close": overrides.get("close", price),
        "volume": overrides.get("volume", 100_000),
        "score": overrides.get("score", 0.0),
        "signal_limit_up": overrides.get("signal_limit_up", False),
        "signal_limit_down": overrides.get("signal_limit_down", False),
        "signal_entry": overrides.get("signal_entry", False),
        "signal_exit": overrides.get("signal_exit", False),
    }


def test_sparse_mapping_is_stable_read_only_and_not_forward_filled():
    panel = pl.DataFrame([
        _row("B", 1, 20, signal_entry=True),
        _row("A", 2, 12),
        _row("A", 0, 10, signal_entry=True),
    ])
    entries = panel["signal_entry"]
    matrix = build_market_matrix(panel, entries, None)

    assert matrix.symbols == ("A", "B")
    assert matrix.timestamp_labels == ("2024-01-01", "2024-01-02", "2024-01-03")
    assert matrix.shape == (3, 2)
    assert np.isnan(matrix.close[1, 0])
    assert matrix.tradable[1, 0] == 0
    assert matrix.entry[1, 0] == 0
    assert matrix.close.flags.writeable is False
    assert matrix.entry.flags.writeable is False

    reversed_panel = panel.reverse()
    reversed_matrix = build_market_matrix(reversed_panel, reversed_panel["signal_entry"], None)
    np.testing.assert_allclose(matrix.close, reversed_matrix.close, equal_nan=True)
    np.testing.assert_array_equal(matrix.entry, reversed_matrix.entry)


def test_duplicate_timestamp_symbol_is_rejected():
    panel = pl.DataFrame([_row("A", 0, 10), _row("A", 0, 11)])
    with pytest.raises(ValueError, match="unique timestamp/symbol"):
        build_market_matrix(panel, None, None)


def test_intraday_timestamps_share_daily_session_id():
    panel = pl.DataFrame({
        "symbol": ["A", "A", "A"],
        "datetime": [
            "2024-01-01 09:30:00",
            "2024-01-01 10:30:00",
            "2024-01-02 09:30:00",
        ],
        "open": [10.0, 10.1, 10.2],
        "high": [10.0, 10.1, 10.2],
        "low": [10.0, 10.1, 10.2],
        "close": [10.0, 10.1, 10.2],
        "volume": [100, 100, 100],
    }).with_columns(pl.col("datetime").str.to_datetime())

    matrix = build_market_matrix(panel, None, None)
    assert matrix.session_ids.tolist() == [0, 0, 1]


def test_tradable_matches_legacy_suspension_rules():
    panel = pl.DataFrame([
        _row("A", 0, 10, volume=100),
        _row("B", 0, 10, volume=0),
        _row("C", 0, 10, volume=0, high=11, low=9),
        _row("D", 0, 0, volume=100),
    ])
    matrix = build_market_matrix(panel, None, None)
    tradable = dict(zip(matrix.symbols, matrix.tradable[0].tolist()))
    assert tradable == {"A": 1, "B": 0, "C": 1, "D": 0}


def test_open_t_plus_one_keeps_legacy_next_asset_bar_semantics():
    panel = pl.DataFrame([
        _row("A", 0, 10, signal_entry=True),
        _row("B", 1, 20),
        _row("A", 2, 12),
    ])
    matrix = build_market_matrix(
        panel,
        panel["signal_entry"],
        None,
        entry_delay_bars=1,
        entry_signal_ids=["signal_entry"],
    )

    assert matrix.entry[:, 0].tolist() == [0, 0, 1]
    assert matrix.entry_signal_time[2, 0] == 0


def test_matrix_matcher_matches_legacy_trade_records_and_equity():
    rows = []
    for symbol, score in (("A", 90), ("B", 80), ("C", 70)):
        for day in range(5):
            overrides = {"score": score}
            if symbol == "A" and day == 2:
                overrides.update(open=9, high=9, low=9, close=9, signal_limit_down=True)
            if symbol == "B" and day == 3:
                overrides.update(open=8.5, high=9, low=8, close=8.5)
            rows.append(_row(symbol, day, 10 + day * 0.1, **overrides))
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    entries = pl.Series([
        row["date"] == date(2024, 1, 1)
        for row in panel.select("date").iter_rows(named=True)
    ])
    exits = pl.Series([
        row["symbol"] == "A" and row["date"] == date(2024, 1, 2)
        for row in panel.select(["symbol", "date"]).iter_rows(named=True)
    ])
    config = MatcherConfig(
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        max_positions=2,
        max_exposure_pct=0.8,
        stop_loss_pct=0.1,
        initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]

    matrix_result = engine.simulate_portfolio(panel, entries, exits, config)
    legacy_result = engine.simulate_portfolio_legacy(panel, entries, exits, config)

    assert [asdict(trade) for trade in matrix_result.trades] == [
        asdict(trade) for trade in legacy_result.trades
    ]
    assert matrix_result.equity_curve == legacy_result.equity_curve
    assert matrix_result.drawdown_curve == legacy_result.drawdown_curve
    assert matrix_result.stats["execution"] == legacy_result.stats["execution"]
    assert matrix_result.stats["pending_exit_positions"] == legacy_result.stats["pending_exit_positions"]


def test_independent_matrix_matches_legacy_candidates():
    panel = pl.DataFrame([
        _row("A", 0, 10, signal_entry=True),
        _row("A", 1, 11, signal_entry=True),
        _row("A", 2, 12),
        _row("A", 3, 9, low=8.5),
        _row("A", 4, 10),
    ]).sort(["symbol", "date"])
    entries = panel["signal_entry"]
    exits = panel["signal_exit"]
    config = MatcherConfig(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_hold_days=2,
        stop_loss_pct=0.1,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]

    matrix_result = engine.simulate_independent_candidates(panel, entries, exits, config)
    legacy_result = engine.simulate_independent_candidates_legacy(panel, entries, exits, config)

    assert [asdict(trade) for trade in matrix_result.trades] == [
        asdict(trade) for trade in legacy_result.trades
    ]
    assert matrix_result.stats["execution"] == legacy_result.stats["execution"]


def _crow(symbol: str, d: date, price: float, **overrides) -> dict:
    """_row 的显式日期版 (跨年场景)。"""
    base = _row(symbol, 0, price, **overrides)
    base["date"] = d
    return base


def test_symbol_contributions_reconcile_with_equity_by_year():
    # 跨年持仓: 2023 买入后浮盈, 2024 续涨并卖出 — 贡献必须按市值变动落在对应年份,
    # 且年度贡献合计 = 年度权益变动 (与净值曲线严格对账)。
    panel = pl.DataFrame([
        _crow("A", date(2023, 12, 28), 10.0, signal_entry=True),
        _crow("A", date(2023, 12, 29), 10.5),
        _crow("A", date(2024, 1, 2), 11.0),
        _crow("A", date(2024, 1, 3), 12.0, signal_exit=True),
    ]).sort(["symbol", "date"])
    matrix = build_market_matrix(panel, panel["signal_entry"], panel["signal_exit"])
    config = MatcherConfig(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
        max_exposure_pct=1.0,
        initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    result = engine.simulate_market_matrix(matrix, config)

    contribs = {(c["symbol"], c["year"]): c["pnl"] for c in result.symbol_contributions}
    # 10000 股 @10: 2023 浮盈 (10.5-10)*10000 落 2023
    assert contribs[("A", 2023)] == pytest.approx(5_000.0, abs=0.01)
    # 2024: (11-10.5)*10000 浮盈 + 卖出日 (12-11)*10000
    assert contribs[("A", 2024)] == pytest.approx(15_000.0, abs=0.01)
    # 年度对账: Σ当年贡献 = 当年权益变动 (首年对初始资金)
    by_year: dict[int, float] = {}
    for c in result.symbol_contributions:
        by_year[c["year"]] = by_year.get(c["year"], 0.0) + c["pnl"]
    year_end_equity = {int(p["date"][:4]): p["value"] for p in result.equity_curve}
    assert by_year[2023] == pytest.approx(year_end_equity[2023] - 100_000, abs=0.01)
    assert by_year[2024] == pytest.approx(year_end_equity[2024] - year_end_equity[2023], abs=0.01)
    # 总量对账: Σ贡献 = 最终权益 - 初始资金 = 该标的交易已实现盈亏
    total = sum(c["pnl"] for c in result.symbol_contributions)
    assert total == pytest.approx(year_end_equity[2024] - 100_000, abs=0.01)
    assert total == pytest.approx(result.trades[0].pnl_amount, abs=0.01)


def test_symbol_contributions_include_fees():
    # 无价格波动时贡献全部为费用: 买入日 -买入费, 卖出日 -卖出费, 总量仍对账。
    panel = pl.DataFrame([
        _crow("A", date(2024, 1, 1), 10.0, signal_entry=True),
        _crow("A", date(2024, 1, 2), 10.0, signal_exit=True),
    ]).sort(["symbol", "date"])
    matrix = build_market_matrix(panel, panel["signal_entry"], panel["signal_exit"])
    config = MatcherConfig(
        matching="close_t",
        fees_pct=0.002,
        slippage_bps=0,
        max_positions=1,
        max_exposure_pct=1.0,
        initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    result = engine.simulate_market_matrix(matrix, config)

    # 买卖同日历年 → 聚合为一行, 数值 = -(买入费 + 卖出费) (价格无波动, 贡献全为费用)
    assert len(result.symbol_contributions) == 1
    entry = result.symbol_contributions[0]
    assert entry["pnl"] < 0
    trade = result.trades[0]
    expected_fees = -(trade.entry_value - trade.shares * trade.entry_price) - (
        trade.shares * trade.exit_price - trade.exit_value
    )
    assert entry["pnl"] == pytest.approx(expected_fees, abs=0.01)
    total = entry["pnl"]
    final_equity = result.equity_curve[-1]["value"]
    assert total == pytest.approx(final_equity - 100_000, abs=0.01)
    assert total == pytest.approx(result.trades[0].pnl_amount, abs=0.01)


def test_lightweight_portfolio_keeps_stats_without_curves_or_monte_carlo(monkeypatch):
    panel = pl.DataFrame([
        _row("A", 0, 10, signal_entry=True),
        _row("A", 1, 11),
        _row("A", 2, 12, signal_exit=True),
        _row("A", 3, 11),
    ]).sort(["symbol", "date"])
    matrix = build_market_matrix(
        panel,
        panel["signal_entry"],
        panel["signal_exit"],
    )
    config = MatcherConfig(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
        initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    full = engine.simulate_market_matrix(matrix, config)

    def unexpected(_pnls):
        raise AssertionError("Monte Carlo should not run")

    monkeypatch.setattr(BacktestEngine, "_mc_drawdown_percentiles", unexpected)
    light = engine.simulate_market_matrix(
        matrix,
        config,
        options=SimulationOptions(
            include_monte_carlo=False,
            include_curves=False,
            include_trades=False,
            include_per_symbol_stats=False,
            include_return_distribution=False,
        ),
    )

    assert light.equity_curve == []
    assert light.drawdown_curve == []
    assert light.trades == []
    assert light.per_symbol_stats == []
    assert "mc_maxdd_p50" not in light.stats
    for name in ("total_return", "annual_return", "max_drawdown", "sharpe", "sortino"):
        assert light.stats[name] == full.stats[name]
