"""人工授权闸口。

规则：
- 任务创建时若 needs_approval=1，或预估成本超过 settings.approval_threshold_cny，
  任务进入 awaiting_approval，必须经此模块批准后才能执行。
- kind: spend 支出 / publish 对外发布 / account_action 账号操作 /
         irreversible 不可逆操作 / task_run 任务执行。
- 批准 -> 关联任务回到 pending；拒绝 -> 关联任务 cancelled。全程留痕。
"""
from .db import now_iso, jdump, jload

KINDS = {
    "spend": "支出",
    "publish": "对外发布",
    "account_action": "账号操作",
    "irreversible": "不可逆操作",
    "task_run": "任务执行",
}

STATUSES = {
    "pending": "待审批",
    "approved": "已批准",
    "rejected": "已拒绝",
}


def create(db, kind, title, detail=None, amount_cny=0.0):
    if kind not in KINDS:
        raise ValueError(f"unknown approval kind: {kind}")
    cur = db.execute(
        "INSERT INTO approvals(ts,kind,title,detail_json,amount_cny,status)"
        " VALUES(?,?,?,?,?,?)",
        (now_iso(), kind, title, jdump(detail or {}), float(amount_cny), "pending"),
    )
    return cur.lastrowid


def get(db, approval_id):
    row = db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    return dict(row) if row else None


def list_approvals(db, status=None, limit=200):
    if status:
        rows = db.query("SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT ?",
                        (status, limit))
    else:
        rows = db.query("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["kind_label"] = KINDS.get(d["kind"], d["kind"])
        d["status_label"] = STATUSES.get(d["status"], d["status"])
        d["detail"] = jload(d.get("detail_json"))
        out.append(d)
    return out


def count_pending(db):
    row = db.query_one("SELECT COUNT(*) AS c FROM approvals WHERE status='pending'")
    return int(row["c"])


def decide(db, approval_id, approve: bool, by="owner", note=""):
    ap = get(db, approval_id)
    if not ap:
        raise ValueError(f"approval {approval_id} not found")
    if ap["status"] != "pending":
        raise ValueError(f"approval {approval_id} already decided")
    new_status = "approved" if approve else "rejected"
    db.execute(
        "UPDATE approvals SET status=?, decided_at=?, decided_by=?, note=? WHERE id=?",
        (new_status, now_iso(), by, note, approval_id),
    )
    # 联动任务：
    # - 拒绝 -> 任务 cancelled；
    # - 批准 + task_phase=pre（执行前审批）-> 任务回 pending 等待执行；
    # - 批准 + task_phase=mid（执行中审批，如询盘草稿）-> 任务直接 succeeded，
    #   避免 worker 重跑再次建审批单造成死循环。
    task = db.query_one("SELECT id FROM tasks WHERE approval_id=?", (approval_id,))
    if task:
        phase = (jload(ap.get("detail_json")) or {}).get("task_phase", "pre")
        if not approve:
            db.execute(
                "UPDATE tasks SET status='cancelled', updated_at=?, finished_at=?,"
                " error=? WHERE id=?",
                (now_iso(), now_iso(), "审批被拒绝", task["id"]),
            )
        elif phase == "mid":
            db.execute(
                "UPDATE tasks SET status='succeeded', finished_at=?, updated_at=?,"
                " result_summary=result_summary || ? WHERE id=?",
                (now_iso(), now_iso(), "（审批通过：草稿可用，请人工发送）", task["id"]),
            )
        else:
            db.execute(
                "UPDATE tasks SET status='pending', updated_at=? WHERE id=?",
                (now_iso(), task["id"]),
            )
    return new_status
