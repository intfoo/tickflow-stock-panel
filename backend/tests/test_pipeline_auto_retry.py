"""自动同步失败重试机制测试(仅限调度路径 _run_tracked)。

策略: 硬失败后 3 分钟重试 1 次; PipelineStageError 软失败不重试; 重试不套重试。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import PipelineStageError


class FakeJobStore:
    """内存版 JobStore, 避免写磁盘 job_store/ 目录。"""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self._counter = 0

    def create(self) -> tuple[str, bool]:
        self._counter += 1
        jid = f"job{self._counter}"
        self.jobs[jid] = {"id": jid, "status": "pending", "log": [], "error": None}
        return jid, True

    def start(self, jid: str) -> None:
        self.jobs[jid]["status"] = "running"

    def succeed(self, jid: str, result: dict) -> None:
        self.jobs[jid]["status"] = "succeeded"
        self.jobs[jid]["result"] = result

    def fail(self, jid: str, error: str) -> None:
        self.jobs[jid]["status"] = "failed"
        self.jobs[jid]["error"] = error

    def progress(self, jid: str, stage: str, pct: int, msg: str,
                 stage_pct: int | None = None, skip_log: bool = False) -> None:
        if not skip_log:
            self.jobs[jid]["log"].append(msg)


class FakeScheduler:
    timezone = ZoneInfo("Asia/Shanghai")

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, fn, trigger=None, id=None, replace_existing=False) -> None:
        self.jobs.append({"fn": fn, "trigger": trigger, "id": id})


@pytest.fixture()
def fakes(monkeypatch):
    store = FakeJobStore()
    sched = FakeScheduler()
    monkeypatch.setattr("app.services.pipeline_jobs.job_store", store)
    monkeypatch.setattr(daily_pipeline, "_scheduler", sched)
    return store, sched


def _boom(on_progress=None) -> dict:
    raise ConnectionError("tickflow api timeout")


def _ok(on_progress=None) -> dict:
    return {"universe_size": 1}


def test_hard_failure_schedules_one_retry_and_keeps_error(fakes):
    store, sched = fakes
    daily_pipeline._run_tracked(_boom, "daily_pipeline", retry=True)

    job = store.jobs["job1"]
    assert job["status"] == "failed"
    # error 保留实际异常(修复前是固定字符串, 排障看不到根因)
    assert "tickflow api timeout" in job["error"]

    # 排了一个 3 分钟后的一次性重试
    assert len(sched.jobs) == 1
    run_date = sched.jobs[0]["trigger"].run_date
    delta = (run_date - datetime.now(ZoneInfo("Asia/Shanghai"))).total_seconds()
    assert 170 < delta <= 180
    assert sched.jobs[0]["id"].startswith("retry_daily_pipeline_")


def test_no_retry_when_flag_off(fakes):
    store, sched = fakes
    daily_pipeline._run_tracked(_boom, "daily_pipeline")  # retry 默认 False
    assert store.jobs["job1"]["status"] == "failed"
    assert sched.jobs == []


def test_soft_failure_pipeline_stage_error_not_retried(fakes):
    store, sched = fakes

    def _soft(on_progress=None) -> dict:
        raise PipelineStageError(["index/etf sync: boom"])

    daily_pipeline._run_tracked(_soft, "daily_pipeline", retry=True)
    assert store.jobs["job1"]["status"] == "failed"
    assert "index/etf sync" in store.jobs["job1"]["error"]
    assert sched.jobs == []  # 软失败不重试


def test_retry_does_not_recursively_retry(fakes):
    store, sched = fakes
    daily_pipeline._run_tracked(_boom, "daily_pipeline", retry=True)
    assert len(sched.jobs) == 1

    # 模拟 3 分钟后重试触发, 再次硬失败 → 不得再排重试
    sched.jobs[0]["fn"]()
    assert store.jobs["job2"]["status"] == "failed"
    assert len(sched.jobs) == 1


def test_retry_job_logs_retry_reason(fakes):
    store, sched = fakes
    daily_pipeline._run_tracked(_boom, "daily_pipeline", retry=True)

    # 重试执行(_is_retry=True), 新 job 首条 log 写明是自动重试及上次失败原因
    jobs_before = len(store.jobs)
    daily_pipeline._run_tracked(_ok, "daily_pipeline", retry=True,
                                _is_retry=True, _last_error="tickflow api timeout")
    assert len(store.jobs) == jobs_before + 1
    retry_job = store.jobs[f"job{store._counter}"]
    assert retry_job["status"] == "succeeded"
    assert any("自动重试" in m and "tickflow api timeout" in m for m in retry_job["log"])


def test_success_path_unaffected(fakes):
    store, sched = fakes
    daily_pipeline._run_tracked(_ok, "instruments_sync", retry=True)
    assert store.jobs["job1"]["status"] == "succeeded"
    assert store.jobs["job1"]["result"] == {"universe_size": 1}
    assert sched.jobs == []
