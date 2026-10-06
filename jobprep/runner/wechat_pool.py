"""Durable, single-worker queue for bounded WeChat article acquisition."""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import time
from typing import Awaitable, Callable

from ..app.wechat_public import public_url


SUCCESS_STATUSES = frozenset({"ok", "partial", "pending_ocr"})
PACING_SAFETY_SECONDS = 0.1


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def pool_lock(path: Path):
    """Prevent two pool runners from starting articles concurrently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        raise RuntimeError("Another WeChat pool worker is running") from None
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class WechatPoolStore:
    """SQLite state whose successful rows are intentionally immutable."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(".lock")
        with closing(self.connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                  id TEXT PRIMARY KEY,
                  url TEXT UNIQUE NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
                  position INTEGER NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  result_json TEXT,
                  last_error TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS article_queue ON articles(status,position);
                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  started_at REAL NOT NULL,
                  finished_at REAL,
                  state TEXT NOT NULL,
                  processed INTEGER NOT NULL DEFAULT 0,
                  succeeded INTEGER NOT NULL DEFAULT 0,
                  failed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp REAL NOT NULL,
                  run_id INTEGER,
                  article_id TEXT,
                  kind TEXT NOT NULL,
                  detail TEXT
                );
                CREATE TABLE IF NOT EXISTS pacing (
                  id INTEGER PRIMARY KEY CHECK(id=1),
                  last_started_at REAL NOT NULL
                );
                """
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def enqueue(self, url: str, metadata=None, *, now_value=None):
        normalized = public_url(url)
        article_id = hashlib.sha256(normalized.encode()).hexdigest()[:24]
        timestamp = time.time() if now_value is None else now_value
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM articles").fetchone()[0]
            db.execute(
                """INSERT OR IGNORE INTO articles
                   (id,url,status,position,metadata_json,created_at,updated_at)
                   VALUES(?,?,'pending',?,?,?,?)""",
                (article_id, normalized, position, _json(metadata or {}), timestamp, timestamp),
            )
        return article_id

    def seed_success(self, url, result, *, now_value=None):
        """Persist a previously validated workstation result without running it."""
        article_id = self.enqueue(url, now_value=now_value)
        timestamp = time.time() if now_value is None else now_value
        with closing(self.connect()) as db, db:
            db.execute(
                """UPDATE articles SET status='succeeded',result_json=?,last_error=NULL,updated_at=?
                   WHERE id=? AND status<>'succeeded'""",
                (_json(result), timestamp, article_id),
            )
        return article_id

    def recover(self, now_value):
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                "UPDATE articles SET status='pending',updated_at=? WHERE status='running'",
                (now_value,),
            )
            return cursor.rowcount

    def retry_failed(self, now_value):
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                """UPDATE articles SET status='pending',last_error=NULL,updated_at=?
                   WHERE status='failed'""",
                (now_value,),
            )
            return cursor.rowcount

    def delay_until_next_start(self, now_value, interval):
        with closing(self.connect()) as db:
            row = db.execute("SELECT last_started_at FROM pacing WHERE id=1").fetchone()
        return max(0.0, row[0] + interval - now_value) if row else 0.0

    def has_pending(self):
        with closing(self.connect()) as db:
            return db.execute(
                "SELECT EXISTS(SELECT 1 FROM articles WHERE status='pending')"
            ).fetchone()[0] == 1

    def claim(self, now_value):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM articles WHERE status='pending' ORDER BY position,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE articles SET status='running',attempts=attempts+1,updated_at=?
                   WHERE id=?""",
                (now_value, row["id"]),
            )
            db.execute(
                """INSERT INTO pacing VALUES(1,?)
                   ON CONFLICT(id) DO UPDATE SET last_started_at=excluded.last_started_at""",
                (now_value,),
            )
            item = dict(row)
            item.update(status="running", attempts=item["attempts"] + 1)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            return item

    def finish(self, article_id, success, result, error, now_value):
        status = "succeeded" if success else "failed"
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status FROM articles WHERE id=?", (article_id,)).fetchone()
            if current is None:
                raise KeyError(article_id)
            # A successful URL can never be demoted, including by a stale worker.
            if current["status"] == "succeeded":
                return
            db.execute(
                """UPDATE articles SET status=?,result_json=?,last_error=?,updated_at=?
                   WHERE id=?""",
                (status, _json(result), error, now_value, article_id),
            )

    def abandon(self, article_id, now_value):
        with closing(self.connect()) as db, db:
            db.execute(
                """UPDATE articles SET status='pending',updated_at=?
                   WHERE id=? AND status='running'""",
                (now_value, article_id),
            )

    def start_run(self, now_value):
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO runs(started_at,state) VALUES(?,'running')", (now_value,)
            )
            return cursor.lastrowid

    def finish_run(self, run_id, state, processed, succeeded, failed, now_value):
        with closing(self.connect()) as db, db:
            db.execute(
                """UPDATE runs SET finished_at=?,state=?,processed=?,succeeded=?,failed=?
                   WHERE id=?""",
                (now_value, state, processed, succeeded, failed, run_id),
            )

    def event(self, run_id, article_id, kind, detail, now_value):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO events(timestamp,run_id,article_id,kind,detail) VALUES(?,?,?,?,?)",
                (now_value, run_id, article_id, kind, str(detail)[:2000]),
            )

    def stats(self):
        with closing(self.connect()) as db:
            statuses = {
                row["status"]: row["n"]
                for row in db.execute("SELECT status,COUNT(*) n FROM articles GROUP BY status")
            }
            last_run = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"statuses": statuses, "last_run": dict(last_run) if last_run else None}


@dataclass
class PoolPolicy:
    min_interval_seconds: float = 180.0
    max_interval_seconds: float = 300.0
    max_failures: int = 10
    pacing_safety_seconds: float = PACING_SAFETY_SECONDS

    def __post_init__(self):
        if (
            isinstance(self.min_interval_seconds, bool)
            or not isinstance(self.min_interval_seconds, (int, float))
            or not math.isfinite(self.min_interval_seconds)
            or self.min_interval_seconds < 10
        ):
            raise ValueError("WeChat article start interval must be at least 10 seconds")
        if (
            isinstance(self.max_interval_seconds, bool)
            or not isinstance(self.max_interval_seconds, (int, float))
            or not math.isfinite(self.max_interval_seconds)
            or self.max_interval_seconds < self.min_interval_seconds
        ):
            raise ValueError("WeChat maximum interval must be finite and at least the minimum")
        if self.max_failures != 10:
            raise ValueError("WeChat circuit breaker threshold is fixed at 10 failures")
        if self.pacing_safety_seconds < PACING_SAFETY_SECONDS:
            raise ValueError("WeChat pacing safety margin must be at least 0.1 seconds")


class WechatPoolRunner:
    def __init__(
        self,
        store: WechatPoolStore,
        processor: Callable[[str, dict], dict | Awaitable[dict]],
        policy=None,
        *,
        clock=None,
        monotonic_clock=None,
        sleep=asyncio.sleep,
        random_uniform=random.uniform,
        progress=None,
    ):
        self.store = store
        self.processor = processor
        self.policy = policy or PoolPolicy()
        self.clock = clock or time.time
        self.monotonic_clock = monotonic_clock or (time.monotonic if clock is None else self.clock)
        self.sleep = sleep
        self.random_uniform = random_uniform
        self.progress = progress

    def _next_interval(self) -> float:
        return self.random_uniform(
            self.policy.min_interval_seconds,
            self.policy.max_interval_seconds,
        ) + self.policy.pacing_safety_seconds

    async def _sleep_until(self, deadline: float) -> None:
        """Sleep to an absolute monotonic deadline, tolerating early wakeups."""
        while True:
            remaining = deadline - self.monotonic_clock()
            if remaining <= 0:
                return
            await self.sleep(remaining)

    async def _report(self, event, **details):
        if self.progress is None:
            return
        try:
            value = self.progress(event, details)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            # Reporting is secondary; SQLite remains the source of truth.
            self.store.event(None, None, "reporting_error", type(exc).__name__, self.clock())

    @staticmethod
    def successful(result):
        return (
            isinstance(result, dict)
            and result.get("status") in SUCCESS_STATUSES
            and result.get("evidence_validated", True) is True
        )

    async def run(self, *, limit=20, retry_failed=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with pool_lock(self.store.lock_path):
            now_value = self.clock()
            recovered = self.store.recover(now_value)
            retried = self.store.retry_failed(now_value) if retry_failed else 0
            run_id = self.store.start_run(now_value)
            processed = succeeded = failed = 0
            state = "completed"
            active = None
            next_start_deadline = self.monotonic_clock()
            await self._report("run_started", run_id=run_id, recovered=recovered, retried=retried)
            try:
                while processed < limit:
                    if not self.store.has_pending():
                        break
                    effective_interval = self._next_interval()
                    persisted_delay = self.store.delay_until_next_start(
                        self.clock(), effective_interval
                    )
                    next_start_deadline = max(
                        next_start_deadline,
                        self.monotonic_clock() + persisted_delay,
                    )
                    await self._sleep_until(next_start_deadline)
                    active = self.store.claim(self.clock())
                    if active is None:
                        break
                    self.store.event(run_id, active["id"], "started", "single_worker", self.clock())
                    await self._report("article_started", run_id=run_id, article=active,
                                       processed=processed, succeeded=succeeded, failed=failed)
                    try:
                        result = self.processor(active["url"], active["metadata"])
                        if inspect.isawaitable(result):
                            result = await result
                        success = self.successful(result)
                        error = None if success else str(result.get("error", "acquisition_failed"))[:2000]
                    except asyncio.CancelledError:
                        self.store.abandon(active["id"], self.clock())
                        state = "interrupted"
                        raise
                    except Exception as exc:
                        result = {"status": "error", "error": type(exc).__name__}
                        success = False
                        error = f"{type(exc).__name__}: processor failed"
                    budget_exhausted = (
                        isinstance(result, dict)
                        and result.get("error_kind") == "new_article_24h_budget_exhausted"
                        and result.get("requests_this_run") == 0
                    )
                    if budget_exhausted:
                        self.store.abandon(active["id"], self.clock())
                        state = "circuit_open"
                        self.store.event(
                            run_id,
                            active["id"],
                            "deferred",
                            "new_article_24h_budget_exhausted",
                            self.clock(),
                        )
                        await self._report(
                            "circuit_open",
                            run_id=run_id,
                            result=result,
                            reason="new_article_24h_budget_exhausted",
                            processed=processed,
                            succeeded=succeeded,
                            failed=failed,
                        )
                        active = None
                        break
                    self.store.finish(active["id"], success, result, error, self.clock())
                    processed += 1
                    succeeded += int(success)
                    failed += int(not success)
                    self.store.event(
                        run_id,
                        active["id"],
                        "succeeded" if success else "failed",
                        result.get("status", "invalid_result") if isinstance(result, dict) else "invalid_result",
                        self.clock(),
                    )
                    await self._report("article_finished", run_id=run_id, article=active,
                                       result=result, success=success, processed=processed,
                                       succeeded=succeeded, failed=failed)
                    active = None
                    if failed > self.policy.max_failures:
                        state = "circuit_open"
                        self.store.event(run_id, None, "circuit_open", "failure_count=11", self.clock())
                        await self._report("circuit_open", run_id=run_id, processed=processed,
                                           succeeded=succeeded, failed=failed)
                        break
            except BaseException:
                if state != "interrupted":
                    state = "error"
                raise
            finally:
                self.store.finish_run(run_id, state, processed, succeeded, failed, self.clock())
                await self._report("run_finished", run_id=run_id, state=state,
                                   processed=processed, succeeded=succeeded, failed=failed)
            return {
                "run_id": run_id,
                "state": state,
                "processed": processed,
                "succeeded": succeeded,
                "failed": failed,
                "recovered": recovered,
                "retried": retried,
                **self.store.stats(),
            }
