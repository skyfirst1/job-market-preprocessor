import json

from jobprep.store import Store
from scripts.accept_substantial_partials import MIGRATION_KIND, migrate


def _partial(count):
    return {
        "status": "partial",
        "jobs": [{"title": f"Role {index}"} for index in range(count)],
        "coverage": {"complete": False, "list_complete": False,
                     "stop_reason": "unknown_pagination"},
    }


def test_migration_is_previewed_bounded_idempotent_and_excludes_wechat(tmp_path):
    store = Store(tmp_path)
    accepted = store.add_task("https://careers.example/jobs")
    small = store.add_task("https://small.example/jobs")
    wechat = store.add_task("https://mp.weixin.qq.com/s?id=public")
    store.finish(accepted, "partial", _partial(20))
    store.finish(small, "partial", _partial(19))
    store.finish(wechat, "partial", _partial(25))

    assert migrate(tmp_path) == {
        "mode": "preview", "min_jobs": 20, "eligible": 1, "updated": 0,
        "task_ids": [accepted], "wechat_excluded": True,
    }
    report = migrate(tmp_path, apply=True)
    assert report["updated"] == 1
    updated = store.task(accepted)
    result = json.loads(updated["result_json"])
    assert updated["status"] == "ok"
    assert result["coverage"]["list_complete"] is False
    assert result["coverage"]["stop_reason"] == "unknown_pagination"
    acceptance = result["coverage"]["temporary_acceptance"]
    assert acceptance["migration"] == MIGRATION_KIND
    assert store.task(small)["status"] == "partial"
    assert store.task(wechat)["status"] == "partial"
    assert migrate(tmp_path, apply=True)["updated"] == 0

    with store.connect() as db:
        events = db.execute("SELECT kind FROM events WHERE task_id=?", (accepted,)).fetchall()
    assert [row["kind"] for row in events] == [MIGRATION_KIND]
