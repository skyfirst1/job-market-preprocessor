"""Idempotently accept useful large partial lists without claiming completeness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobprep.adapters.pool import accept_substantial_partial_list
from jobprep.store import Store, now


MIGRATION_KIND = "temporary_acceptance_substantial_job_list"


def _is_wechat(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".") == "mp.weixin.qq.com"
    except (TypeError, ValueError):
        return False


def migrate(data_dir: str | Path, *, apply: bool = False, min_jobs: int = 20) -> dict:
    if isinstance(min_jobs, bool) or not isinstance(min_jobs, int) or min_jobs < 1:
        raise ValueError("min_jobs must be a positive integer")
    store = Store(data_dir)
    candidates = []
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT id,url,result_json FROM tasks WHERE status='partial' ORDER BY id"
        ).fetchall()
        for row in rows:
            if _is_wechat(row["url"]):
                continue
            try:
                result = json.loads(row["result_json"] or "null")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict):
                continue
            promoted = accept_substantial_partial_list(result, min_jobs=min_jobs)
            if promoted.get("status") != "ok" or result.get("status") == "ok":
                continue
            candidates.append((row["id"], row["url"], promoted))

        if apply:
            timestamp = now()
            for task_id, url, promoted in candidates:
                acceptance = promoted["coverage"]["temporary_acceptance"]
                acceptance.update(migration=MIGRATION_KIND, task_id=task_id)
                db.execute(
                    """UPDATE tasks SET status='ok',result_json=?,error=NULL,
                       updated_at=?,available_at=NULL,last_error_kind=NULL
                       WHERE id=? AND status='partial'""",
                    (json.dumps(promoted, ensure_ascii=False), timestamp, task_id),
                )
                db.execute(
                    "INSERT INTO events(timestamp,task_id,kind,detail) VALUES(?,?,?,?)",
                    (timestamp, task_id, MIGRATION_KIND,
                     json.dumps({"url": url, **acceptance}, ensure_ascii=False)),
                )

    return {
        "mode": "apply" if apply else "preview",
        "min_jobs": min_jobs,
        "eligible": len(candidates),
        "updated": len(candidates) if apply else 0,
        "task_ids": [task_id for task_id, _, _ in candidates],
        "wechat_excluded": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--min-jobs", type=int, default=20)
    parser.add_argument("--apply", action="store_true",
                        help="persist changes; omission performs a read-only preview")
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_dir, apply=args.apply, min_jobs=args.min_jobs),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
