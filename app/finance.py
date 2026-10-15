"""成本与经营台账。

铁律：
- 每一笔账必须标 is_simulated（0 真实 / 1 模拟）。模拟订单的收入只能进模拟口径，
  控制台真实盈利区永远看不到它。
- kind: api_cost（模型 API 消耗）, expense（外部支出）, revenue（收入）, refund（退款）。
- 金额统一 CNY 元；外币发生时按记录时汇率折算并备注。
"""
from .db import now_iso

KINDS = {
    "api_cost": "API成本",
    "expense": "支出",
    "revenue": "收入",
    "refund": "退款",
}


def record(db, kind, amount_cny, category="", description="", ref_type=None,
           ref_id=None, is_simulated=False, currency="CNY"):
    """记一笔账。amount_cny 为正数；refund 用 kind 区分，不用负数。"""
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind: {kind}")
    cur = db.execute(
        "INSERT INTO ledger(ts,kind,amount_cny,currency,category,description,ref_type,ref_id,is_simulated)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (now_iso(), kind, float(amount_cny), currency, category, description,
         ref_type, ref_id, 1 if is_simulated else 0),
    )
    return cur.lastrowid


def _sum(db, kind, simulated):
    row = db.query_one(
        "SELECT COALESCE(SUM(amount_cny),0) AS s FROM ledger WHERE kind=? AND is_simulated=?",
        (kind, 1 if simulated else 0),
    )
    return float(row["s"])


def totals(db):
    """返回真实/模拟分开的汇总。调用方展示时必须分开展示。"""
    return {
        "revenue_real": _sum(db, "revenue", False),
        "revenue_sim": _sum(db, "revenue", True),
        "cost_real": _sum(db, "api_cost", False) + _sum(db, "expense", False),
        "cost_sim": _sum(db, "api_cost", True) + _sum(db, "expense", True),
        "expense_real": _sum(db, "expense", False),
        "api_cost_real": _sum(db, "api_cost", False),
        "refund_real": _sum(db, "refund", False),
    }


def list_entries(db, limit=200):
    rows = db.query("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def create_order(db, module_key, customer, item, amount_cny, is_simulated=False,
                 status="paid", notes=""):
    cur = db.execute(
        "INSERT INTO orders(module_key,customer,item,amount_cny,status,is_simulated,created_at,notes)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (module_key, customer, item, float(amount_cny), status,
         1 if is_simulated else 0, now_iso(), notes),
    )
    order_id = cur.lastrowid
    # 订单自动记一笔收入账（模拟订单 -> 模拟口径）
    record(db, "revenue", amount_cny, category=module_key or "order",
           description=f"订单 #{order_id} {item}",
           ref_type="order", ref_id=order_id, is_simulated=is_simulated)
    return order_id


def list_orders(db, limit=200):
    rows = db.query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
