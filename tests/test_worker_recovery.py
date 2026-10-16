"""Fail-closed task handling for ambiguous timeout and approval states."""
import threading
import time
import pytest
from app import worker, tasks
from app.db import Database


def test_timeout_is_quarantined_not_retried(tmp_path, monkeypatch):
    db = Database(tmp_path / "time.db")
    go = threading.Event()
    started = threading.Event()
    def blocked(db, task, log):
        started.set()
        go.wait(1)
        return "late"
    monkeypatch.setitem(worker.HANDLERS, "sample_fulfillment", blocked)
    tid = tasks.create_task(db, type="sample_fulfillment", title="test",
                            payload={"brief":"none"}, timeout_sec=0.02, max_attempts=3)
    try:
        t0 = time.monotonic()
        ok, summary = worker.run_task_once(db, tid)
        assert not ok
        assert "soft timeout" in summary
        assert time.monotonic() - t0 < 0.8
        assert tasks.get_task(db, tid)["status"] == "failed"
        assert tasks.get_task(db, tid)["attempts"] == 1
    finally:
        go.set()
        time.sleep(0.03)
        db.close()


def test_approval_cannot_be_skipped_by_direct_execution(tmp_path):
    db = Database(tmp_path / "approval.db")
    tid = tasks.create_task(db, type="sample_fulfillment", title="blocked",
                            payload={"brief": "dont"}, needs_approval=True)
    with pytest.raises(ValueError):
        worker.run_task_once(db, tid)
    assert tasks.get_task(db, tid)["status"] == "awaiting_approval"
    db.close()
