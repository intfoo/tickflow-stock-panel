"""宽基 ETF 推荐清单与有效集合计算 (fork 私有模块)。

设计见 docs/superpowers/specs/2026-08-16-etf-broad-presets-design.md §3。

- PRESET_BROAD_ETFS: 静态推荐清单 (宁缺毋滥, 不含 QDII/增强型)
- preset_symbols(instruments): 清单 ∩ instruments.symbol (防代码漂移)
- effective_broad(instruments): 综合 store 配置返回 (有效集合, 是否默认态)
"""
from __future__ import annotations

import polars as pl

PRESET_BROAD_ETFS: list[str] = [
    # 沪深300
    "510300.SH",  # 沪深300ETF华泰柏瑞
    "159919.SZ",  # 沪深300ETF嘉实
    "510330.SH",  # 沪深300ETF华夏
    "510310.SH",  # 沪深300ETF易方达
    # 中证500
    "510500.SH",  # 中证500ETF南方
    "159922.SZ",  # 中证500ETF嘉实
    "512500.SH",  # 中证500ETF华夏
    # 中证1000
    "512100.SH",  # 中证1000ETF南方
    "159845.SZ",  # 中证1000ETF汇添富
    # 上证50 / 上证180
    "510050.SH",  # 上证50ETF华夏
    "510180.SH",  # 上证180ETF华安
    # 创业板指 / 创业板50
    "159915.SZ",  # 创业板ETF易方达
    "159948.SZ",  # 创业板ETF天弘
    "159949.SZ",  # 创业板50ETF华安
    # 科创50
    "588000.SH",  # 科创50ETF华夏
    "588080.SH",  # 科创50ETF华泰柏瑞
    # 深证100
    "159901.SZ",  # 深证100ETF易方达
    # 上证综指
    "510210.SH",  # 上证综指ETF富国
    # 中证2000
    "563300.SH",  # 中证2000ETF华泰柏瑞
]


def preset_symbols(instruments: pl.DataFrame | None) -> list[str]:
    """推荐清单 ∩ instruments.symbol, 防代码漂移。

    instruments 为 None 或空 DataFrame 时返回 []。
    """
    if instruments is None or instruments.is_empty():
        return []
    if "symbol" not in instruments.columns:
        return []
    valid = set(instruments["symbol"].cast(pl.Utf8).to_list())
    return [s for s in PRESET_BROAD_ETFS if s in valid]


def effective_broad(instruments: pl.DataFrame | None) -> tuple[set[str], bool]:
    """返回 (有效宽基集合, 是否默认态)。

    - customized=True (用户保存过, 含空清单): 返回 (用户清单, False)
    - customized=False (默认/未配置): 返回 (preset∩instruments, True)

    instruments 为 None/空时不抛异常: 默认态返回 (set(), True),
    自定义态返回 (用户清单, False)。
    """
    from app.services import etf_fund_store as store

    cfg = store.load_broad()
    if cfg.get("customized"):
        return set(cfg.get("symbols", [])), False
    return set(preset_symbols(instruments)), True
