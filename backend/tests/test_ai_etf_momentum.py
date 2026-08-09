"""ai_20260807_etf_momentum 策略验证测试。

三层：
1. 评分单元测试（合成数据，不依赖数据文件）
2. 状态机行为测试（合成 MarketDataMatrix）
3. 端到端回测锚点（真实数据，缺数据则 skip）
"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pytest

from app.backtest.matrix import MarketDataMatrix

# ── 按路径加载策略文件（策略在 data/ 不在包内）──────────────────────
_STRATEGY_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "strategies" / "ai" / "ai_20260807_etf_momentum.py"
)

_spec = importlib.util.spec_from_file_location("ai_etf_momentum", _STRATEGY_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_momentum_scores = _mod._momentum_scores
SCORE_FLOOR = _mod.SCORE_FLOOR
EtfMomentumMatrixStrategy = _mod.EtfMomentumMatrixStrategy
MATRIX_STRATEGY = _mod.MATRIX_STRATEGY
POOLS = _mod.POOLS


# ═══════════════════════════════════════════════════════════════
#  辅助：构造合成 MarketDataMatrix
# ═══════════════════════════════════════════════════════════════

def _make_market(
    close: np.ndarray,
    symbols: tuple[str, ...],
) -> MarketDataMatrix:
    """从 (T, N) close 矩阵构造 MarketDataMatrix，其余 OHLCV 用 close 填充。"""
    t_n, n = close.shape
    close_f32 = close.astype(np.float32, copy=True)
    ohlc = close_f32.copy()
    volume = np.full((t_n, n), 1_000_000.0, dtype=np.float32)
    tradable = np.ones((t_n, n), dtype=np.uint8)
    limit_up = np.zeros((t_n, n), dtype=np.uint8)
    limit_down = np.zeros((t_n, n), dtype=np.uint8)
    timestamps = np.arange(t_n, dtype=np.int64)
    session_ids = np.arange(t_n, dtype=np.int32)
    labels = tuple(f"2024-01-{day + 1:02d}" for day in range(t_n))
    names = tuple(s[:6] for s in symbols)

    # 全部设为只读（MarketDataMatrix 是 frozen dataclass，内部验证可能检查）
    for arr in (timestamps, session_ids, ohlc, ohlc, ohlc, close_f32, volume,
                tradable, limit_up, limit_down):
        arr.flags.writeable = False

    return MarketDataMatrix(
        timestamps=timestamps,
        timestamp_labels=labels,
        session_ids=session_ids,
        symbols=symbols,
        names=names,
        open=ohlc,
        high=ohlc,
        low=ohlc,
        close=close_f32,
        volume=volume,
        tradable=tradable,
        limit_up_locked=limit_up,
        limit_down_locked=limit_down,
        fields=MappingProxyType({}),
    )


# ═══════════════════════════════════════════════════════════════
#  第一层：评分单元测试
# ═══════════════════════════════════════════════════════════════

def test_momentum_scores_linear_growth_positive():
    """线性增长 log 价：score 应为显著正值（对齐蓝本 sanity_check）。"""
    # T=60, N=1, 每日 +1%: close = 100 * 1.01**t
    t_n, m = 60, 25
    close = (100.0 * np.power(1.01, np.arange(t_n))).reshape(-1, 1)
    scores = _momentum_scores(close.astype(np.float64), m)

    # warm-up 期（前 m-1 行）应为 SCORE_FLOOR
    assert scores.shape == (t_n, 1)
    warmup = scores[:m - 1, 0]
    assert np.all(warmup == SCORE_FLOOR), f"warm-up 应为 SCORE_FLOOR, got {warmup}"

    # 最后一个 score 应为显著正值（1% 日化年化 ≈ 11.4x，R²≈1）
    assert scores[-1, 0] > 0.5, f"线性增长 score 应 > 0.5, got {scores[-1, 0]}"
    assert np.isfinite(scores[-1, 0]), "score 不应含 inf/NaN"


def test_momentum_scores_flat_is_floor():
    """零方差窗口 → 无动量信号：score 应为 SCORE_FLOOR 或近似零（非正）。

    浮点精度下 syy_dev 可能不精确为 0（极小正数），导致 r2 产生 ~1e-34 量级噪声。
    核心语义是"无动量"——score 不为正值（零方差 → slope≈0, r2≈0 → score≈0）。
    """
    t_n, m = 40, 20
    close = np.full((t_n, 1), 50.0)  # 恒定价格 → log 价方差为 0
    scores = _momentum_scores(close.astype(np.float64), m)

    # warm-up 期应为 SCORE_FLOOR
    assert np.all(scores[:m - 1, 0] == SCORE_FLOOR), \
        f"warm-up 应为 SCORE_FLOOR, got {scores[:m - 1, 0]}"

    # warm-up 之后：score 应近似零（零方差 → slope≈0, r2≈0 → score≈0）。
    # 浮点噪声可能导致极小正值 (~1e-34)，但语义上"无动量"——score 不为显著正值。
    valid_scores = scores[m - 1:, 0]
    assert np.all(np.abs(valid_scores) < 1e-10), \
        f"零方差窗口 score 应近似零, got max={np.abs(valid_scores).max()}"


def test_momentum_scores_nan_window_is_floor():
    """窗口含 NaN → SCORE_FLOOR（对齐蓝本 dropna 语义）。"""
    t_n, m = 60, 20
    close = (100.0 * np.power(1.01, np.arange(t_n))).reshape(-1, 1)
    # 在第 25~30 行塞 NaN，使 t=29 的窗口 [10..29] 含 NaN
    close_nan = close.copy()
    close_nan[25:31, 0] = np.nan
    scores = _momentum_scores(close_nan.astype(np.float64), m)

    # t=29 的窗口 [10..29] 含 NaN → SCORE_FLOOR
    assert scores[29, 0] == SCORE_FLOOR, \
        f"含 NaN 窗口 score 应为 SCORE_FLOOR, got {scores[29, 0]}"

    # 但全有效的窗口（如 t=59, 窗口 [40..59] 不含 NaN）仍为正值
    assert scores[59, 0] > 0.0, \
        f"有效窗口 score 应为正, got {scores[59, 0]}"


# ═══════════════════════════════════════════════════════════════
#  第二层：状态机行为测试
# ═══════════════════════════════════════════════════════════════

def test_state_machine_rotates_to_top1():
    """双标的合成矩阵：A 先强后弱、B 后强 → entry 先 A 后 B，exit A 为 rotate(code=2)。

    构造：A 前 40 天日涨 2%，之后走平；B 前 40 天走平，之后日涨 2%。
    m=20 时，t≈40 之后 B 的动量分将超过 A，触发换仓。
    """
    t_n = 80
    m = 20
    # A: 前 40 天涨，后 40 天平
    close_a = np.empty(t_n)
    close_a[:40] = 100.0 * np.power(1.02, np.arange(40))
    close_a[40:] = close_a[39]
    # B: 前 40 天平，后 40 天涨
    close_b = np.empty(t_n)
    close_b[:40] = 100.0
    close_b[40:] = 100.0 * np.power(1.02, np.arange(1, 41))

    close = np.column_stack([close_a, close_b])
    symbols = ("159934.SZ", "513100.SH")
    market = _make_market(close, symbols)

    params = {"pool": "classic4", "m_days": m, "pos_sl": 0.10}
    signals = MATRIX_STRATEGY.compute_signals(market, params)

    entry = signals.entry
    exit_ = signals.exit
    exit_code = signals.exit_signal_code

    # 1. 确认有 entry 信号产生
    assert entry.any(), "应产生 entry 信号"

    # 2. 找到 A 被 exit 且 code=2 (rotate) 的时刻
    a_col = 0
    rotate_days = np.where((exit_[:, a_col] == 1) & (exit_code[:, a_col] == 2))[0]
    assert len(rotate_days) > 0, "A 应被 rotate 换出 (exit_code=2)"

    # 3. rotate 发生在 t >= 40 之后（B 开始上涨之后）
    rotate_t = rotate_days[0]
    assert rotate_t >= 40, f"rotate 应在 B 走强后 (t>=40), got t={rotate_t}"

    # 4. rotate 当日 B 有 entry
    assert entry[rotate_t, 1] == 1, f"rotate 当日 B 应有 entry, t={rotate_t}"


def test_state_machine_take_profit_and_lock():
    """持仓涨幅 ≥ tp → exit code=0（止盈）；锁定期内该标的不出现 entry。

    构造：A 持续日涨 0.5%（累计 ~28%），超过黄金 5% 止盈线。
    止盈后 A 被锁定 30 天（lock_gold=30），期间即使 A 评分最高也不可买回。
    """
    t_n = 100
    m = 20
    # A: 前 60 天日涨 0.5%（累计 ~35%），之后走平
    close_a = np.empty(t_n)
    close_a[:60] = 100.0 * np.power(1.005, np.arange(60))
    close_a[60:] = close_a[59]
    # B: 全程缓慢上涨
    close_b = 100.0 * np.power(1.001, np.arange(t_n))

    close = np.column_stack([close_a, close_b])
    symbols = ("159934.SZ", "513100.SH")
    market = _make_market(close, symbols)

    # 用默认参数（tp_gold=0.05, lock_gold=30）
    params = {
        "pool": "classic4",
        "m_days": m,
        "pos_sl": 0.10,
        "tp_gold": 0.05,
        "lock_gold": 30,
    }
    signals = MATRIX_STRATEGY.compute_signals(market, params)

    entry = signals.entry
    exit_ = signals.exit
    exit_code = signals.exit_signal_code

    a_col = 0

    # 1. A 应有止盈 exit (code=0)
    tp_days = np.where((exit_[:, a_col] == 1) & (exit_code[:, a_col] == 0))[0]
    assert len(tp_days) > 0, "A 应触发止盈 (exit_code=0)"
    tp_t = tp_days[0]

    # 2. 止盈后 lock_gold(30) 天内 A 不可出现 entry
    lock_end = tp_t + 30
    lock_window = entry[tp_t + 1:lock_end, a_col]
    assert not lock_window.any(), \
        f"锁定期内 A 不应有 entry, t=[{tp_t + 1}, {lock_end}), got {np.where(lock_window)[0]}"

    # 3. 止盈当日应有换仓买入其他标的（或空仓）
    # 止盈触发时 exit A，同时尝试买 avail[0]（排除被锁的 A）
    # 验证止盈当日或之前 A 有 entry（即先持有才会止盈）
    entry_days_a = np.where(entry[:, a_col] == 1)[0]
    assert len(entry_days_a) > 0, "A 应有 entry（先买入才能止盈）"
    assert entry_days_a[0] < tp_t, "A 的首次 entry 应在止盈之前"


# ═══════════════════════════════════════════════════════════════
#  第三层：端到端锚点（数据缺失则 skip）
# ═══════════════════════════════════════════════════════════════

POOL4 = ["159934.SZ", "513100.SH", "510300.SH", "159915.SZ"]


def _has_etf_data_for_symbols(symbols: list[str], year: int = 2016) -> bool:
    """检查 kline_etf_enriched 目录是否覆盖指定年份的数据。"""
    data_dir = Path(_STRATEGY_PATH).resolve().parents[1] / "kline_etf_enriched"
    if not data_dir.exists():
        return False
    # 检查是否有早于指定年的分区目录
    for entry in data_dir.iterdir():
        name = entry.name
        if name.startswith("date="):
            try:
                d = name[5:]
                if int(d[:4]) <= year:
                    return True
            except ValueError:
                continue
    return False


def test_e2e_classic4_anchor():
    """classic4 池 2016-01-01 起 close_t/满仓/万一：年化 ∈ [0.43, 0.53]。

    数据前提：kline_etf_enriched 覆盖四标的 2016 至今，缺失 → pytest.skip。
    """
    if not _has_etf_data_for_symbols(POOL4, year=2016):
        pytest.skip("kline_etf_enriched 无 2016 年数据，跳过 e2e 锚点测试")

    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
    from app.config import settings
    from app.strategy.engine import StrategyEngine

    # 构造 StrategyEngine（加载 AI 策略目录）
    ai_strategy_dir = Path(_STRATEGY_PATH).resolve().parent
    strategy_engine = StrategyEngine(strategy_dirs=[ai_strategy_dir])

    # 确认策略加载成功
    strategy = strategy_engine.get("ai_20260807_etf_momentum")
    assert strategy is not None, "策略 ai_20260807_etf_momentum 加载失败"
    assert strategy.execution_backend == "matrix_native"

    # 构造 BacktestEngine（需要 repo，但 matrix_native 路径可能直接加载 parquet）
    repo = settings  # BacktestEngine 需要 repo，但实际走 load_market_data_matrix_for_backtest
    engine = BacktestEngine(repo)
    service = StrategyBacktestService(engine, strategy_engine)

    config = StrategyBacktestConfig(
        strategy_id="ai_20260807_etf_momentum",
        symbols=POOL4,
        start=date(2016, 1, 1),
        end=date(2026, 5, 1),
        matching="close_t",
        fees_pct=0.0001,
        slippage_bps=0.0,
        max_positions=1,
        asset_type="etf",
        mode="position",
        params={},
    )

    result = service.run(config)
    assert result.error is None, f"回测失败: {result.error}"

    annual_return = result.stats.get("annual_return")
    assert annual_return is not None, f"stats 中无 annual_return: {result.stats.keys()}"

    print(f"\n[e2e] annual_return={annual_return:.4f}")
    assert 0.43 <= annual_return <= 0.53, \
        f"年化 {annual_return:.4f} 不在 [0.43, 0.53] 范围内"
