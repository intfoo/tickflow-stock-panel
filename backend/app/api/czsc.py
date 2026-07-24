"""缠论分析 API — HTTP 薄壳。

路由前缀: /api/czsc

端点:
  GET /analyze  缠论分析 (分型/笔/中枢/信号)
  GET /signals  信号目录 (全量, 按 namespace 分组)
  GET /status   czsc 是否可用 + 默认推荐信号 (供前端菜单显隐)
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services import czsc_service

router = APIRouter(prefix="/api/czsc", tags=["czsc"])


@router.get("/analyze")
def analyze(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    freq: str = Query("日线", description="日线/周线/月线/季线/1分钟/5分钟/15分钟/30分钟/60分钟"),
    days: int | None = Query(None, ge=1, description="取近 N 天; 不传则用该 freq 默认值"),
    signals: str | None = Query(None, description="逗号分隔信号名; 不传则用默认推荐集"),
):
    """缠论分析 — 分型/笔/中枢/买卖点信号。

    czsc 未安装时返回降级响应 {available:false}。
    """
    if not czsc_service.is_available():
        return {
            "available": False,
            "message": "缠论分析需要 czsc 扩展，请运行: uv sync --extra czsc",
        }

    repo = request.app.state.repo
    sig_list = [s.strip() for s in signals.split(",") if s.strip()] if signals else None
    return czsc_service.analyze(repo, symbol, freq, days, sig_list)


@router.get("/signals")
def signals_catalog():
    """返回 czsc 全信号目录 (按 namespace 分组)。"""
    return czsc_service.list_signals()


@router.get("/status")
def status():
    """返回 czsc 是否可用 + 默认推荐信号 (供前端菜单显隐与预勾选)。"""
    return {
        "available": czsc_service.is_available(),
        "default_signals": czsc_service.DEFAULT_SIGNALS,
    }
