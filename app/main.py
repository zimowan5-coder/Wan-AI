"""Wan's AI Business OS v0.1 —— Web 控制台入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8931
Windows 一键启动见 scripts/start_windows.bat
"""
import json
import threading
import os
from dotenv import load_dotenv
from pathlib import Path

import markdown
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import revenue as rev
from . import billing
from . import mailer
from .security import owner_guard, check_signature
from . import approvals as ap
from . import finance as fin
from . import tasks as tasks_mod
from . import worker as worker_mod
from .db import Database, now_iso, jdump, jload
from .modules import base as modules_base
from .modules import inquirypilot  # noqa: F401 触发注册

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "wanai.db"
load_dotenv(BASE_DIR / ".env", override=False)

DEFAULT_SETTINGS = {
    "monthly_budget_cny": "5000",
    "approval_threshold_cny": "50",
    "llm_provider": "local",
}


def create_app(db_path=None):
    db = Database(db_path or DEFAULT_DB)
    # 默认配置
    for k, v in DEFAULT_SETTINGS.items():
        if db.query_one("SELECT value FROM settings WHERE key=?", (k,)) is None:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
    # 模块注册进表（幂等）
    for m in modules_base.list_modules():
        if db.query_one("SELECT id FROM modules WHERE key=?", (m["key"],)) is None:
            db.execute(
                "INSERT INTO modules(key,name,description,status,created_at)"
                " VALUES(?,?,?,?,?)",
                (m["key"], m["name"], m["description"], m["status"], now_iso()),
            )

    app = FastAPI(title="Wan's AI Business OS v0.1")
    app.state.db = db
    tasks_mod.quarantine_interrupted(db)
    app.middleware("http")(owner_guard)
    templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")),
              name="static")

    # ---------- 公开服务介绍：与私有管理控制台分离 ----------
    @app.get("/offer", response_class=HTMLResponse)
    def offer(request: Request):
        checkout = os.environ.get("WANAI_CHECKOUT_URL", "").strip()
        # Only HTTPS provider-hosted checkout links, no javascript: or unsafe redirects.
        if not checkout.startswith(("https://buy.stripe.com/", "https://checkout.stripe.com/")):
            checkout = ""
        contact = rev.clean_email(os.environ.get("WANAI_FROM_EMAIL", ""))
        return templates.TemplateResponse(request, "offer.html", {
            "checkout_url": checkout, "contact_email": contact,
        })

    # ---------- 实际经营：询盘、交付与收款 ----------
    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": "0.2"}

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        return templates.TemplateResponse(request, "setup.html", {
            "readiness": rev.readiness(), "pending_approvals_count": ap.count_pending(db),
        })

    @app.get("/leads", response_class=HTMLResponse)
    def leads_page(request: Request):
        return templates.TemplateResponse(request, "leads.html", {
            "leads": rev.list_leads(db), "metrics": rev.metrics(db),
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/leads")
    def capture_manually(sender_email: str = Form(...), text: str = Form(...),
                         subject: str = Form("")):
        try:
            lead, _ = rev.capture(db, source="manual", external_id="",
                                  sender_email=sender_email, text=text, subject=subject)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return RedirectResponse(f"/leads/{lead['id']}", status_code=303)

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(request: Request, lead_id: int):
        lead = rev.get_lead(db, lead_id)
        if not lead:
            raise HTTPException(404, "Lead not found")
        linked = tasks_mod.get_task(db, lead["task_id"]) if lead["task_id"] else None
        return templates.TemplateResponse(request, "lead_detail.html", {
            "lead": lead, "task": linked, "pending_approvals_count": ap.count_pending(db),
            "smtp_ready": rev.readiness()["smtp"],
        })

    @app.post("/leads/{lead_id}/send")
    def send_lead(lead_id: int):
        try:
            mailer.send_approved(db, lead_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    @app.post("/leads/{lead_id}/outcome")
    def lead_outcome(lead_id: int, status: str = Form(...)):
        try:
            rev.mark_outcome(db, lead_id, status)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    @app.post("/inbox/sync")
    def sync_inbox():
        try:
            mailer.poll_inbox(db)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"收件箱连接失败：{type(e).__name__}")
        return RedirectResponse("/leads", status_code=303)

    @app.post("/api/intake")
    async def intake(request: Request):
        raw = await request.body()
        if len(raw) > 24_000:
            raise HTTPException(413, "payload too large")
        secret = os.environ.get("WANAI_INTAKE_SECRET", "")
        if not secret:
            raise HTTPException(503, "intake disabled")
        if not check_signature(secret, raw, request.headers.get("x-wanai-signature", "")):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            lead, created = rev.capture(
                db, source="webhook", external_id=str(payload.get("event_id") or ""),
                sender_email=payload.get("email", ""), text=payload.get("message", ""),
                subject=str(payload.get("subject") or ""))
        except (ValueError, TypeError, UnicodeError) as e:
            raise HTTPException(422, str(e))
        return {"accepted": True, "created": created, "lead_id": lead["id"]}

    @app.post("/api/billing/stripe")
    async def stripe_webhook(request: Request):
        raw = await request.body()
        if len(raw) > 100_000:
            raise HTTPException(413, "event too large")
        if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
            raise HTTPException(503, "billing webhook disabled")
        try:
            event = billing.verify_stripe_event(raw, request.headers.get("stripe-signature", ""))
            return billing.handle_event(db, event)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, "unverified or invalid payment event")

    # ---------- 控制台 ----------
    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        counts = tasks_mod.count_by_status(db)
        totals = fin.totals(db)
        budget_total = float(tasks_mod.get_setting(db, "monthly_budget_cny", "5000") or 5000)
        stats = {
            "approvals_pending": ap.count_pending(db),
            "tasks_pending": counts.get("pending", 0),
            "tasks_running": counts.get("running", 0),
            "tasks_succeeded": counts.get("succeeded", 0),
            "tasks_failed": counts.get("failed", 0),
            "budget_total_cny": budget_total,
            "budget_used_cny": totals["cost_real"],
            "revenue_real_cny": totals["revenue_real"],
            "revenue_sim_cny": totals["revenue_sim"],
            "cost_real_cny": totals["cost_real"],
        }
        return templates.TemplateResponse(request, "dashboard.html", {
            "request": request,
            "stats": stats,
            "recent_tasks": tasks_mod.list_tasks(db, limit=10),
            "pending_approvals": ap.list_approvals(db, status="pending", limit=10),
            "modules": modules_base.list_modules(),
            "lead_metrics": rev.metrics(db),
            "next_action": rev.next_action(db),
            "readiness": rev.readiness(),
            "pending_approvals_count": stats["approvals_pending"],
        })

    # ---------- 任务 ----------
    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request, status: str = ""):
        return templates.TemplateResponse(request, "tasks.html", {
            "request": request,
            "tasks": tasks_mod.list_tasks(db, status=status or None),
            "status_filter": status,
            "task_types": tasks_mod.TASK_TYPES,
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/tasks")
    def create_task_ep(type: str = Form(...), title: str = Form(...),
                       payload: str = Form(""), priority: int = Form(0)):
        try:
            data = json.loads(payload) if payload.strip() else {}
        except Exception:
            data = {"_raw": payload}
        tid = tasks_mod.create_task(db, type=type, title=title, payload=data,
                                    priority=priority)
        return RedirectResponse(f"/tasks/{tid}", status_code=303)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: int):
        task = tasks_mod.get_task(db, task_id)
        approval = ap.get(db, task["approval_id"]) if task and task.get("approval_id") else None
        return templates.TemplateResponse(request, "task_detail.html", {
            "request": request, "task": task,
            "logs": tasks_mod.get_logs(db, task_id),
            "artifacts": tasks_mod.list_artifacts(db, task_id),
            "approval": approval,
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: int):
        tasks_mod.cancel(db, task_id)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.post("/tasks/{task_id}/retry")
    def retry_task_ep(task_id: int):
        tasks_mod.retry_task(db, task_id)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.post("/tasks/{task_id}/input")
    def submit_input_ep(task_id: int, input_text: str = Form(...)):
        tasks_mod.submit_input(db, task_id, input_text)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    # ---------- 市场研究 ----------
    @app.get("/research", response_class=HTMLResponse)
    def research_page(request: Request):
        rows = db.query("SELECT id, topic, created_at, ai_mode, stats_json FROM research_reports"
                        " ORDER BY id DESC LIMIT 100")
        reports = []
        for r in rows:
            d = dict(r)
            st = jload(d.get("stats_json"))
            d["sources_count"] = st.get("pages_ok", 0)
            reports.append(d)
        return templates.TemplateResponse(request, "research.html", {
            "request": request, "reports": reports,
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/research")
    def create_research(topic: str = Form(...), seed_urls: str = Form("")):
        seeds = [u.strip() for u in seed_urls.splitlines() if u.strip()]
        payload = {"topic": topic}
        if seeds:
            payload["seed_urls"] = seeds
        tid = tasks_mod.create_task(db, type="market_research",
                                    title=f"市场研究：{topic}",
                                    payload=payload, timeout_sec=600)
        return RedirectResponse(f"/tasks/{tid}", status_code=303)

    @app.get("/research/{report_id}", response_class=HTMLResponse)
    def report_detail(request: Request, report_id: int):
        row = db.query_one("SELECT * FROM research_reports WHERE id=?", (report_id,))
        report = dict(row) if row else None
        html, sources, stats = "", [], {}
        if report:
            html = markdown.markdown(report["report_md"] or "",
                                     extensions=["tables", "fenced_code"])
            sources = jload(report.get("sources_json"), [])
            stats = jload(report.get("stats_json"), {})
        return templates.TemplateResponse(request, "report_detail.html", {
            "request": request, "report": report, "report_html": html,
            "sources": sources, "stats": stats,
            "ai_mode_label": "本地规则引擎（未调用付费 API）",
            "pending_approvals_count": ap.count_pending(db),
        })

    # ---------- 财务 ----------
    @app.get("/finance", response_class=HTMLResponse)
    def finance_page(request: Request):
        entries = fin.list_entries(db)
        for e in entries:
            e["kind_label"] = fin.KINDS.get(e["kind"], e["kind"])
        return templates.TemplateResponse(request, "finance.html", {
            "request": request, "ledger": entries,
            "orders": fin.list_orders(db), "totals": fin.totals(db),
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/finance/expense")
    def add_expense(amount_cny: float = Form(...), category: str = Form(""),
                   description: str = Form("")):
        fin.record(db, "expense", amount_cny, category=category,
                   description=description or "手工记账", is_simulated=False)
        return RedirectResponse("/finance", status_code=303)

    # ---------- 审批 ----------
    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request):
        return templates.TemplateResponse(request, "approvals.html", {
            "request": request, "approvals": ap.list_approvals(db),
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/approvals/{approval_id}/approve")
    def approve_ep(approval_id: int, note: str = Form("")):
        ap.decide(db, approval_id, True, note=note)
        return RedirectResponse("/approvals", status_code=303)

    @app.post("/approvals/{approval_id}/reject")
    def reject_ep(approval_id: int, note: str = Form("")):
        ap.decide(db, approval_id, False, note=note)
        return RedirectResponse("/approvals", status_code=303)

    # ---------- 业务模块 ----------
    @app.get("/modules", response_class=HTMLResponse)
    def modules_page(request: Request):
        return templates.TemplateResponse(request, "modules.html", {
            "request": request, "modules": modules_base.list_modules(),
            "pending_approvals_count": ap.count_pending(db),
        })

    @app.post("/modules/{key}/run")
    def run_module(key: str, action: str = Form(...), params: str = Form("{}")):
        mod = modules_base.get_module(key)
        if not mod:
            return RedirectResponse("/modules", status_code=303)
        act = next((a for a in mod["actions"] if a["action"] == action), None)
        if not act:
            return RedirectResponse("/modules", status_code=303)
        try:
            data = json.loads(params) if params.strip() else {}
        except Exception:
            data = {}
        tid = tasks_mod.create_task(
            db, type=act["task_type"], title=f"{mod['name']}：{act['label']}",
            payload=data, module_key=key,
            needs_approval=act.get("needs_approval", False))
        return RedirectResponse(f"/tasks/{tid}", status_code=303)

    # ---------- 端到端演示 ----------
    @app.post("/demo/run")
    def demo_run():
        t = threading.Thread(target=_run_demo, args=(db,), daemon=True)
        t.start()
        return RedirectResponse("/tasks", status_code=303)

    @app.on_event("startup")
    def _startup():
        app.state.worker_stop, app.state.worker_thread = worker_mod.start_worker(db)

    @app.on_event("shutdown")
    def _shutdown():
        try:
            app.state.worker_stop.set()
        except Exception:
            pass

    return app


def _wait_task(db, task_id, timeout=600):
    import time
    start = time.time()
    while time.time() - start < timeout:
        t = tasks_mod.get_task(db, task_id)
        if t["status"] in ("succeeded", "failed", "cancelled"):
            return t
        time.sleep(3)
    return tasks_mod.get_task(db, task_id)


def _run_demo(db):
    """端到端演示链路（后台线程跑，进度在任务列表可见）：
    市场研究 -> 样品制作 -> 模拟订单 -> 控制台查看。"""
    proj = db.query_one("SELECT id FROM projects WHERE name='演示项目'")
    if proj:
        pid = proj["id"]
    else:
        cur = db.execute("INSERT INTO projects(name,description,status,created_at)"
                         " VALUES(?,?,?,?)",
                         ("演示项目", "v0.1 端到端演示", "active", now_iso()))
        pid = cur.lastrowid
    topic = "AI 外贸询盘处理工具"
    t1 = tasks_mod.create_task(db, type="market_research", title=f"演示：市场研究（{topic}）",
                               payload={"topic": topic}, project_id=pid,
                               timeout_sec=600, priority=10)
    r1 = _wait_task(db, t1)
    brief = f"基于市场研究报告（{topic}），制作一份产品介绍单页样品。"
    t2 = tasks_mod.create_task(db, type="sample_fulfillment", title="演示：样品制作",
                               payload={"brief": brief, "format": "html"},
                               project_id=pid, priority=10)
    r2 = _wait_task(db, t2)
    t3 = tasks_mod.create_task(db, type="simulated_order", title="演示：模拟订单",
                               payload={"item": "演示商品·AI 询盘跟进服务",
                                        "amount_cny": 199, "customer": "模拟客户"},
                               project_id=pid, module_key="demo", priority=10)
    r3 = _wait_task(db, t3)
    ok = all(r and r["status"] == "succeeded" for r in (r1, r2, r3))
    tasks_mod.log(db, 0, "info",
                  f"端到端演示{'成功' if ok else '未完全成功'}："
                  f"研究#{t1}={r1['status'] if r1 else '?'}，"
                  f"样品#{t2}={r2['status'] if r2 else '?'}，"
                  f"订单#{t3}={r3['status'] if r3 else '?'}。"
                  "模拟订单已明确标记，不计入真实收入。")


app = create_app()
