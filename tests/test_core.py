"""Wan's AI v0.1 核心测试：任务状态机 / 财务口径 / 审批闸口 / 模块 / LLM。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import approvals as ap
from app import finance as fin
from app import tasks as t
from app import worker as w
from app.db import Database
from app.llm import get_provider, NotConfigured


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    for k, v in {"monthly_budget_cny": "5000", "approval_threshold_cny": "50",
                 "llm_provider": "local"}.items():
        d.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
    return d


def test_task_lifecycle(db):
    tid = t.create_task(db, type="sample_fulfillment", title="t1",
                        payload={"brief": "x"})
    assert t.get_task(db, tid)["status"] == "pending"
    claimed = t.claim_next(db)
    assert claimed == tid
    assert t.get_task(db, tid)["status"] == "running"
    assert t.claim_next(db) is None  # 原子领取：不会重复领
    t.finish(db, tid, True, result_summary="ok")
    assert t.get_task(db, tid)["status"] == "succeeded"


def test_task_retry_exhaustion(db):
    tid = t.create_task(db, type="llm_probe", title="t2", payload={"prompt": "x"},
                        max_attempts=2)
    # 模拟两次失败
    t.claim_next(db)
    assert t.schedule_retry(db, tid, "boom1") is True
    assert t.get_task(db, tid)["status"] == "pending"
    t.claim_next(db)
    assert t.schedule_retry(db, tid, "boom2") is False
    assert t.get_task(db, tid)["status"] == "failed"


def test_finance_simulated_separation(db):
    fin.create_order(db, "demo", "c1", "商品A", 100, is_simulated=True)
    fin.record(db, "revenue", 500, description="真实收入", is_simulated=False)
    fin.record(db, "expense", 30, description="服务器", is_simulated=False)
    totals = fin.totals(db)
    assert totals["revenue_sim"] == 100
    assert totals["revenue_real"] == 500
    assert totals["cost_real"] == 30
    # 模拟收入绝不混入真实口径
    assert totals["revenue_real"] != 600


def test_approval_gate_blocks_task(db):
    tid = t.create_task(db, type="sample_fulfillment", title="贵任务",
                        payload={}, estimated_cost_cny=9999)
    task = t.get_task(db, tid)
    assert task["status"] == "awaiting_approval"
    assert task["approval_id"]
    assert t.claim_next(db) is None  # 待审批任务不能被领取
    ap.decide(db, task["approval_id"], True)
    assert t.get_task(db, tid)["status"] == "pending"
    assert t.claim_next(db) == tid


def test_approval_reject_cancels_task(db):
    tid = t.create_task(db, type="sample_fulfillment", title="拒任务",
                        payload={}, needs_approval=True,
                        approval_kind="publish", approval_title="发布")
    aid = t.get_task(db, tid)["approval_id"]
    ap.decide(db, aid, False, note="不同意")
    assert t.get_task(db, tid)["status"] == "cancelled"


def test_llm_local_is_free_and_honest(db):
    p = get_provider("local")
    r = p.complete("需求：降本。价格 100 元。风险：封号。")
    assert r["cost_cny"] == 0
    assert "未调用付费 API" in r["text"]


def test_llm_missing_key_goes_notconfigured():
    p = get_provider("openai")
    with pytest.raises(NotConfigured):
        p.complete("hi")


def test_inquiry_classify():
    from app.modules.inquirypilot import classify_inquiry, draft_reply
    a = classify_inquiry("Hi, need 5000 pcs steel lunch boxes. Quote FOB price please. - buyer@x.example")
    assert a["intent"] == "询价"
    assert a["lang"] == "en"
    assert a["quantities"]
    reply = draft_reply(a)
    assert "human review required" in reply


def test_sample_fulfillment_writes_real_file(db, tmp_path):
    tid = t.create_task(db, type="sample_fulfillment", title="样品",
                        payload={"brief": "测试brief", "format": "html"})
    ok, summary = w.run_task_once(db, tid)
    assert ok
    assert t.get_task(db, tid)["status"] == "succeeded"
    arts = t.list_artifacts(db, tid)
    assert len(arts) == 1
    assert Path(arts[0]["path"]).exists()


def test_simulated_order_marked(db):
    tid = t.create_task(db, type="simulated_order", title="模拟单",
                        payload={"item": "X", "amount_cny": 199, "customer": "Y"})
    ok, _ = w.run_task_once(db, tid)
    assert ok
    totals = fin.totals(db)
    assert totals["revenue_sim"] == 199
    assert totals["revenue_real"] == 0


def test_mid_execution_approval_completes_task(db):
    """执行中审批（询盘草稿）：批准后任务直接成功，不会重跑死循环。"""
    tid = t.create_task(db, type="inquiry_process", title="询盘",
                        payload={"inquiry_text": "Hi, need 100 pcs. Quote please."},
                        module_key="inquirypilot")
    ok, _ = w.run_task_once(db, tid)
    task = t.get_task(db, tid)
    assert task["status"] == "awaiting_approval"
    aid = task["approval_id"]
    ap.decide(db, aid, True, note="草稿可用")
    task = t.get_task(db, tid)
    assert task["status"] == "succeeded"
    assert "人工发送" in task["result_summary"]


def test_pre_execution_approval_returns_to_queue(db):
    """执行前审批：批准后任务回队列等待执行。"""
    tid = t.create_task(db, type="sample_fulfillment", title="预审批",
                        payload={"brief": "x"}, needs_approval=True,
                        approval_kind="spend",
                        approval_title="花钱测试", estimated_cost_cny=100)
    assert t.get_task(db, tid)["status"] == "awaiting_approval"
    ap.decide(db, t.get_task(db, tid)["approval_id"], True)
    assert t.get_task(db, tid)["status"] == "pending"


def test_research_degrades_gracefully(monkeypatch):
    from app import research
    monkeypatch.setattr(research, "search_web", lambda *a, **k: [])
    out = research.run_research("测试主题", log=lambda l, m: None)
    assert out["report_md"]
    assert "测试主题" in out["report_md"]
    assert out["stats"]["pages_ok"] == 0


def test_research_scoring_deterministic():
    from app import research
    pages = [
        {"url": "http://a", "title": "t", "text": "用户需求很大，求推荐靠谱方案，价格多少钱？支持自动化批量处理。", "ok": True},
        {"url": "http://b", "title": "t", "text": "竞品对比排行榜，小心骗局和封号风险。", "ok": True},
    ]
    r1 = research.score_opportunities("主题", pages)
    r2 = research.score_opportunities("主题", pages)
    assert r1 == r2
    assert r1["opportunities"]
