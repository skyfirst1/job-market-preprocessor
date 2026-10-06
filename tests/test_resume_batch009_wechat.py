from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from jobprep.runner.wechat_pool import WechatPoolRunner, WechatPoolStore
from scripts import resume_batch009_wechat as resume


def queue_file(root: Path, count: int = 16) -> Path:
    path = root / resume.QUEUE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"company": f"company-{index}",
         "acquisition_url": f"https://mp.weixin.qq.com/s/article_{index:02d}"}
        for index in range(count)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_checkpoint_is_exactly_sixteen_unique_wechat_urls(tmp_path):
    assert len(resume.load_checkpoint(queue_file(tmp_path))) == 16
    with pytest.raises(ValueError, match="exactly 16"):
        resume.load_checkpoint(queue_file(tmp_path, 15))


def test_budget_preflight_is_read_only_and_reports_next_window(tmp_path):
    db_path = tmp_path / "data/artifacts/.wechat_public/captures.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE captures (key TEXT, url TEXT, state TEXT, requests INTEGER, scheduled REAL, result_path TEXT)")
        db.executemany("INSERT INTO captures VALUES (?,?,?,?,?,?)", [
            (str(i), str(i), "partial", 1, 1000 + i, None) for i in range(20)
        ])
    before = db_path.read_bytes()
    report = resume.capture_budget(tmp_path, now_value=2000)
    assert report["used"] == 20 and report["available"] == 0
    assert report["next_available_at"] is not None
    assert db_path.read_bytes() == before


def test_dry_run_and_exhausted_execute_never_build_processor(tmp_path, monkeypatch):
    queue_file(tmp_path)
    monkeypatch.setattr(resume, "seed_successes", lambda root, pool, items: 0)
    called = []
    monkeypatch.setattr(resume, "checkpoint_processor", lambda *a, **k: called.append(True))
    dry = resume.main(["--root", str(tmp_path)])
    assert dry["state"] == "dry_run"
    monkeypatch.setattr(resume, "capture_budget", lambda root: {
        "used": 20, "limit": 20, "available": 0, "next_available_at": "later"
    })
    waiting = resume.main(["--root", str(tmp_path), "--execute"])
    assert waiting["state"] == "budget_wait"
    assert waiting["network_requests_started"] == 0
    assert called == []


def test_success_is_seeded_and_never_reset(tmp_path):
    pool = WechatPoolStore(tmp_path / "resume.sqlite3")
    url = "https://mp.weixin.qq.com/s/already_done"
    pool.seed_success(url, {"status": "partial"})
    pool.enqueue("https://mp.weixin.qq.com/s/not_done")
    reset = resume.reset_local_failures(pool, {url}, 1234)
    assert reset == 0
    assert pool.stats()["statuses"] == {"pending": 1, "succeeded": 1}


class FakeTime:
    def __init__(self):
        self.value = 1000.0
        self.sleeps = []

    def clock(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def test_resume_policy_is_single_worker_ten_seconds_and_eleventh_failure(tmp_path):
    pool = WechatPoolStore(tmp_path / "resume.sqlite3")
    for index in range(16):
        pool.enqueue(f"https://mp.weixin.qq.com/s/failure_{index:02d}")
    fake = FakeTime()
    active = 0
    max_active = 0
    starts = []

    async def processor(url, metadata):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        starts.append(fake.clock())
        active -= 1
        return {"status": "error", "requests_this_run": 1, "error": "fixture"}

    report = asyncio.run(WechatPoolRunner(
        pool, processor, clock=fake.clock, sleep=fake.sleep
    ).run(limit=16))
    assert max_active == 1
    assert all(b - a >= 10 for a, b in zip(starts, starts[1:]))
    assert report["state"] == "circuit_open"
    assert report["failed"] == report["processed"] == 11
    assert report["statuses"] == {"failed": 11, "pending": 5}


def test_budget_result_is_deferred_without_becoming_failure(tmp_path):
    pool = WechatPoolStore(tmp_path / "resume.sqlite3")
    pool.enqueue("https://mp.weixin.qq.com/s/deferred")
    report = asyncio.run(WechatPoolRunner(pool, lambda url, metadata: {
        "status": "error", "error_kind": "new_article_24h_budget_exhausted",
        "requests_this_run": 0,
    }).run(limit=16))
    assert report["state"] == "circuit_open"
    assert report["processed"] == report["failed"] == 0
    assert report["statuses"] == {"pending": 1}
