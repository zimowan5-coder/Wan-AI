"""任务与 Agent 执行系统。

状态机：
    pending -> running -> succeeded
                     |-> failed (attempts < max -> 回 pending 重试)
    pending -> awaiting_approval -> pending / cancelled
    running -> awaiting_input -> pending（人工粘贴 AI 输出等场景）
    任何未终态 -> cancelled

- claim_next 用 UPDATE...RETURNING 原子领取，避免多线程重复执行。
- 超时/重试/异常上报由 worker.py 驱动，这里只管状态流转与记录。
- 任务类型（v0.1）：market_research / sample_fulfillment / simulated_order /
  inquiry_process / inquiry_simulate / llm_probe
"""
from .db import now_iso, jdump, jload, row_to_dict
from . import approvals as approvals_mod

STATUS_LABELS = {
    "pending": "待执行",
    "awaiting_approval": "待审批",
    "running": "执行中",
    "awaiting_input": "待输入",
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
}

TASK_TYPES = {
    "market_research": "市场研究",
    "sample_fulfillment": "样品制作",
    "simulated_order": "模拟订单",
    "inquiry_process": "询盘处理",
    "inquiry_simulate": "模拟询盘",
    "llm_probe": "AI调用测试",
}


def get_setting(db, key, default=""):
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def create_task(db, *, type, title, payload=None, project_id=None, module_key=None,
                priority=0, max_attempts=3, timeout_sec=300,
                needs_approval=False, approval_kind=None, approval_title=None,
                approval_detail=None, estimated_cost_cny=0.0):
    """创建任务。需要审批时自动建审批单并进入 awaiting_approval。"""
    if type not in TASK_TYPES:
        raise ValueError(f"unknown task type: {type}")
    payload = payload or {}
    threshold = float(get_setting(db, "approval_threshold_cny", "50") or 50)
    must_approve = bool(needs_approval) or float(estimated_cost_cny or 0) > threshold

    approval_id = None
    status = "pending"
    if must_approve:
        kind = approval_kind or ("spend" if estimated_cost_cny else "task_run")
        approval_id = approvals_mod.create(
            db, kind,
            approval_title or f"审批任务：{title}",
            {"task_type": type, "task_title": title, "payload": payload,
             "task_phase": "pre"},
            amount_cny=estimated_cost_cny,
        )
        status = "awaiting_approval"

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO tasks(project_id,module_key,type,title,payload_json,status,priority,"
        " attempts,max_attempts,timeout_sec,needs_approval,approval_id,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, module_key, type, title, jdump(payload), status, priority,
         0, max_attempts, timeout_sec, 1 if must_approve else 0, approval_id, ts, ts),
    )
    return cur.lastrowid


def get_task(db, task_id):
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        return None
    d = row_to_dict(row)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    d["type_label"] = TASK_TYPES.get(d["type"], d["type"])
    d["payload"] = jload(d.get("payload_json"))
    return d


def list_tasks(db, status=None, limit=200):
    if status:
        rows = db.query("SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                        (status, limit))
    else:
        rows = db.query("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
        d["type_label"] = TASK_TYPES.get(d["type"], d["type"])
        out.append(d)
    return out


def count_by_status(db):
    rows = db.query("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status")
    return {r["status"]: r["c"] for r in rows}


def claim_next(db):
    """领取一个待执行任务（优先级高、id 小优先）。

    注：靠 db 全局写锁保证单进程内原子（SELECT + UPDATE 在同一把锁内完成），
    不用 UPDATE...RETURNING 以避开 sqlite commit/游标冲突。
    """
    from .db import _lock
    ts = now_iso()
    with _lock:
        row = db._conn.execute(
            "SELECT id FROM tasks WHERE status='pending'"
            " ORDER BY priority DESC, id ASC LIMIT 1").fetchone()
        if not row:
            return None
        cur = db._conn.execute(
            "UPDATE tasks SET status='running', started_at=?, attempts=attempts+1,"
            " updated_at=? WHERE id=? AND status='pending'",
            (ts, ts, row["id"]))
        db._conn.commit()
        return row["id"] if cur.rowcount else None


def finish(db, task_id, ok: bool, result_summary="", error=""):
    ts = now_iso()
    status = "succeeded" if ok else "failed"
    db.execute(
        "UPDATE tasks SET status=?, finished_at=?, updated_at=?,"
        " result_summary=?, error=? WHERE id=?",
        (status, ts, ts, result_summary or "", error or "", task_id),
    )


def schedule_retry(db, task_id, error=""):
    """失败但还有重试次数：回 pending。返回 True=将重试，False=次数用尽。"""
    t = get_task(db, task_id)
    if t and t["attempts"] < t["max_attempts"]:
        db.execute(
            "UPDATE tasks SET status='pending', updated_at=?, error=? WHERE id=?",
            (now_iso(), f"第{t['attempts']}次尝试失败，将重试：{error}", task_id),
        )
        return True
    finish(db, task_id, False, error=f"重试{ t['max_attempts'] if t else '?'}次后仍失败：{error}")
    return False


def log(db, task_id, level, message):
    db.execute("INSERT INTO task_logs(task_id,ts,level,message) VALUES(?,?,?,?)",
               (task_id, now_iso(), level, str(message)[:4000]))


def get_logs(db, task_id, limit=500):
    rows = db.query("SELECT ts,level,message FROM task_logs WHERE task_id=? ORDER BY id ASC LIMIT ?",
                    (task_id, limit))
    return [dict(r) for r in rows]


def request_input(db, task_id, prompt_text):
    """任务需要人工输入（如粘贴 AI 输出）：转 awaiting_input，提示词存 error 字段展示。"""
    db.execute("UPDATE tasks SET status='awaiting_input', updated_at=?, error=? WHERE id=?",
               (now_iso(), prompt_text, task_id))


def submit_input(db, task_id, text):
    """人工提交输入：把输入并入 payload.input_text，任务回 pending 继续。"""
    t = get_task(db, task_id)
    if not t or t["status"] != "awaiting_input":
        raise ValueError("task is not awaiting input")
    payload = t["payload"] or {}
    payload["input_text"] = text
    db.execute("UPDATE tasks SET status='pending', updated_at=?, payload_json=?, error=''"
               " WHERE id=?", (now_iso(), jdump(payload), task_id))


def cancel(db, task_id):
    t = get_task(db, task_id)
    if not t or t["status"] in ("succeeded", "failed", "cancelled"):
        raise ValueError("task already finished")
    db.execute("UPDATE tasks SET status='cancelled', updated_at=?, finished_at=? WHERE id=?",
               (now_iso(), now_iso(), task_id))


def retry_task(db, task_id):
    t = get_task(db, task_id)
    if not t or t["status"] not in ("failed", "cancelled"):
        raise ValueError("only failed/cancelled tasks can be retried")
    db.execute("UPDATE tasks SET status='pending', attempts=0, updated_at=?, error='' WHERE id=?",
               (now_iso(), task_id))


def save_artifact(db, task_id, name, path, mime=""):
    cur = db.execute(
        "INSERT INTO artifacts(task_id,name,path,mime,created_at) VALUES(?,?,?,?,?)",
        (task_id, name, str(path), mime, now_iso()),
    )
    return cur.lastrowid


def list_artifacts(db, task_id):
    rows = db.query("SELECT * FROM artifacts WHERE task_id=? ORDER BY id ASC", (task_id,))
    return [dict(r) for r in rows]
