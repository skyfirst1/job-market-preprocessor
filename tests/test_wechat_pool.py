import asyncio
from pathlib import Path

import pytest

from jobprep.runner.wechat_pool import PoolPolicy, WechatPoolRunner, WechatPoolStore


def urls(count):
    return [f"https://mp.weixin.qq.com/s/article_{index:02d}" for index in range(count)]


class FakeTime:
    def __init__(self):
        self.value = 1000.0
        self.sleeps = []

    def clock(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class EarlyWakeTime(FakeTime):
    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += min(seconds, 3.0)


def run(value):
    return asyncio.run(value)


def test_single_worker_starts_at_least_ten_seconds_apart(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    for url in urls(3):
        store.enqueue(url, now_value=999)
    fake = FakeTime()
    starts = []

    async def processor(url, metadata):
        starts.append(fake.clock())
        return {"status": "ok", "evidence_validated": True}

    report = run(WechatPoolRunner(store, processor, clock=fake.clock, sleep=fake.sleep).run())
    assert report["succeeded"] == 3
    assert all(b - a >= 10.09 for a, b in zip(starts, starts[1:]))
    assert fake.sleeps == pytest.approx([10.1, 10.1])


def test_monotonic_deadline_rechecks_after_early_wakeup(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    for url in urls(2):
        store.enqueue(url, now_value=999)
    fake = EarlyWakeTime()
    starts = []

    def processor(url, metadata):
        starts.append(fake.clock())
        return {"status": "ok", "evidence_validated": True}

    report = run(WechatPoolRunner(store, processor, clock=fake.clock, sleep=fake.sleep).run())
    assert report["succeeded"] == 2
    assert starts[1] - starts[0] >= 10.09
    assert len(fake.sleeps) >= 4


def test_eleventh_failure_opens_circuit_and_leaves_remainder_pending(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    for url in urls(13):
        store.enqueue(url)
    fake = FakeTime()

    report = run(
        WechatPoolRunner(
            store,
            lambda url, metadata: {"status": "error", "error": "fixture"},
            clock=fake.clock,
            sleep=fake.sleep,
        ).run()
    )
    assert report["state"] == "circuit_open"
    assert report["processed"] == report["failed"] == 11
    assert report["statuses"] == {"failed": 11, "pending": 2}


def test_budget_exhaustion_opens_circuit_without_counting_a_no_request_failure(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    for url in urls(3):
        store.enqueue(url)
    fake = FakeTime()

    report = run(
        WechatPoolRunner(
            store,
            lambda url, metadata: {
                "status": "error",
                "error_kind": "new_article_24h_budget_exhausted",
                "requests_this_run": 0,
            },
            clock=fake.clock,
            sleep=fake.sleep,
        ).run()
    )

    assert report["state"] == "circuit_open"
    assert report["processed"] == report["failed"] == 0
    assert report["statuses"] == {"pending": 3}


def test_success_is_never_reenqueued_or_retried(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    url = urls(1)[0]
    store.enqueue(url)
    fake = FakeTime()
    calls = []

    def processor(value, metadata):
        calls.append(value)
        return {"status": "partial", "evidence_validated": True}

    runner = WechatPoolRunner(store, processor, clock=fake.clock, sleep=fake.sleep)
    assert run(runner.run())["succeeded"] == 1
    store.enqueue(url, {"new": "metadata"})
    report = run(runner.run(retry_failed=True))
    assert report["processed"] == 0
    assert report["statuses"] == {"succeeded": 1}
    assert calls == [url]


def test_failed_item_can_be_explicitly_retried_after_repair(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    store.enqueue(urls(1)[0])
    fake = FakeTime()
    healthy = False

    def processor(url, metadata):
        return {"status": "ok" if healthy else "error"}

    runner = WechatPoolRunner(store, processor, clock=fake.clock, sleep=fake.sleep)
    assert run(runner.run())["statuses"] == {"failed": 1}
    healthy = True
    report = run(runner.run(retry_failed=True))
    assert report["retried"] == 1
    assert report["statuses"] == {"succeeded": 1}


def test_running_item_is_recovered_after_interruption(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    store.enqueue(urls(1)[0])
    claimed = store.claim(1000)
    assert claimed["status"] == "running"
    fake = FakeTime()
    report = run(
        WechatPoolRunner(
            store,
            lambda url, metadata: {"status": "ok"},
            clock=fake.clock,
            sleep=fake.sleep,
        ).run()
    )
    assert report["recovered"] == 1
    assert report["statuses"] == {"succeeded": 1}


def test_policy_rejects_short_interval_or_changed_breaker():
    with pytest.raises(ValueError, match="at least 10"):
        PoolPolicy(min_interval_seconds=9.99)
    with pytest.raises(ValueError, match="fixed at 10"):
        PoolPolicy(max_failures=11)


def test_default_batch_is_bounded_to_twenty(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    for url in urls(25):
        store.enqueue(url)
    fake = FakeTime()
    report = run(
        WechatPoolRunner(
            store,
            lambda url, metadata: {"status": "ok"},
            clock=fake.clock,
            sleep=fake.sleep,
        ).run()
    )
    assert report["processed"] == 20
    assert report["statuses"] == {"pending": 5, "succeeded": 20}


def test_seeded_success_is_never_claimed(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    url = urls(1)[0]
    store.seed_success(url, {"status": "partial", "existing_result_json": True})
    fake = FakeTime()
    report = run(
        WechatPoolRunner(
            store,
            lambda value, metadata: pytest.fail("seeded success was claimed"),
            clock=fake.clock,
            sleep=fake.sleep,
        ).run()
    )
    assert report["processed"] == 0
    assert report["statuses"] == {"succeeded": 1}


def test_seed_cannot_replace_an_existing_success_result(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    url = urls(1)[0]
    store.seed_success(url, {"status": "partial", "marker": "original"})
    store.seed_success(url, {"status": "ok", "marker": "replacement"})
    with store.connect() as db:
        saved = db.execute("SELECT result_json FROM articles").fetchone()[0]
    assert "original" in saved
    assert "replacement" not in saved


def test_progress_reports_each_durable_transition(tmp_path):
    store = WechatPoolStore(tmp_path / "pool.sqlite3")
    store.enqueue(urls(1)[0])
    fake = FakeTime()
    events = []
    runner = WechatPoolRunner(
        store,
        lambda url, metadata: {"status": "ok"},
        clock=fake.clock,
        sleep=fake.sleep,
        progress=lambda event, details: events.append(event),
    )
    run(runner.run())
    assert events == ["run_started", "article_started", "article_finished", "run_finished"]
