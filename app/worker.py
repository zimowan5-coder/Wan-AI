"""后台 Worker：单线程轮询，顺序执行任务。

- 每 2 秒 claim 一个 pending 任务，用线程池 + timeout 跑 handler（防卡死）。
- handler 异常 -> 记录日志 -> 按 max_attempts 重试 -> 上报失败。
- 所有 handler 必须：写日志、产出可查证的结果（报告/文件/账）、诚实标记模拟与真实。
"""
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import tasks as tasks_mod
from . import finance as finance_mod
from .db import jdump, now_iso


def _log(db, task_id, level, msg):
    tasks_mod.log(db, task_id, level, msg)


# ---------------- handlers ----------------

def handle_market_research(db, task, log):
    from .research import run_research
    payload = task["payload"] or {}
    topic = payload.get("topic", "").strip()
    if not topic:
        raise ValueError("payload.topic 为空")
    log("info", f"开始市场研究：{topic}")
    result = run_research(topic, log=log,
                          max_results=int(payload.get("max_results", 8)),
                          seed_urls=payload.get("seed_urls"))
    db.execute(
        "INSERT INTO research_reports(task_id,topic,report_md,sources_json,stats_json,ai_mode,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (task["id"], topic, result["report_md"], jdump(result["sources"]),
         jdump(result["stats"]), result["ai_mode"], result["created_at"]),
    )
    n_ok = result["stats"].get("pages_ok", 0)
    summary = (f"研究报告已生成：{topic}；抓取成功 {n_ok} 页，"
               f"候选机会 {len(result['analysis'].get('opportunities', []))} 个"
               f"（AI模式：本地规则引擎，未产生费用）")
    log("info", summary)
    return summary


def handle_sample_fulfillment(db, task, log):
    payload = task["payload"] or {}
    brief = payload.get("brief", "").strip() or "未提供 brief"
    fmt = payload.get("format", "md")
    task_id = task["id"]
    art_dir = Path(db.path).parent / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    log("info", f"制作样品：brief={brief[:60]}")
    if fmt == "html":
        fname = f"sample-{task_id}.html"
        content = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>样品 - 任务 #{task_id}</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:40px auto;padding:0 16px}}</style>
</head><body><h1>样品交付物</h1><p><b>任务</b> #{task_id} · {task['title']}</p>
<h2>需求简述</h2><p>{brief}</p>
<h2>说明</h2><p>此为 Wan's AI v0.1 样品制作任务生成的占位交付物（本地模板生成，零成本）。
真实业务中此处应为按客户需求定制的内容。</p></body></html>"""
        mime = "text/html"
    else:
        fname = f"sample-{task_id}.md"
        content = (f"# 样品交付物\n\n- 任务 #{task_id} · {task['title']}\n\n"
                   f"## 需求简述\n\n{brief}\n\n"
                   "## 说明\n\n此为 Wan's AI v0.1 样品制作任务生成的占位交付物"
                   "（本地模板生成，零成本）。\n")
        mime = "text/markdown"
    path = art_dir / fname
    path.write_text(content, encoding="utf-8")
    tasks_mod.save_artifact(db, task_id, fname, str(path), mime)
    log("info", f"样品已写入 {path}")
    return f"样品已生成：{fname}（本地模板，零成本）"


def handle_simulated_order(db, task, log):
    payload = task["payload"] or {}
    item = payload.get("item", "演示商品")
    amount = float(payload.get("amount_cny", 99))
    customer = payload.get("customer", "模拟客户")
    module_key = task.get("module_key") or "demo"
    oid = finance_mod.create_order(db, module_key, customer, item, amount,
                                   is_simulated=True, notes="v0.1 端到端演示：模拟订单")
    log("warning", f"创建模拟订单 #{oid}：{item} ¥{amount}（已标记为模拟，不计入真实收入）")
    return f"模拟订单 #{oid}：{item} ¥{amount}（模拟口径）"


def handle_inquiry_process(db, task, log):
    from .modules.inquirypilot import classify_inquiry, draft_reply, ASSESSMENT_SHORT
    payload = task["payload"] or {}
    text = (payload.get("inquiry_text") or "").strip()
    if not text and not payload.get("input_text"):
        raise ValueError("payload.inquiry_text 为空")
    text = text or payload["input_text"]
    log("info", "InquiryPilot：开始处理询盘（来料加工模式）")
    analysis = classify_inquiry(text)
    log("info", f"分类结果：意图={analysis['intent']} 语言={analysis['lang']} "
                f"产品关键词={analysis['product_keywords'][:5]}")
    reply = draft_reply(analysis)
    art_dir = Path(db.path).parent / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    fname = f"inquiry-{task['id']}-reply.md"
    path = art_dir / fname
    path.write_text(f"# 询盘跟进草稿（待审批，未发送）\n\n## 询盘原文\n\n{text[:2000]}\n\n"
                    f"## 分析\n\n{jdump(analysis, )}\n\n## 回复草稿\n\n{reply}\n",
                    encoding="utf-8")
    tasks_mod.save_artifact(db, task["id"], fname, str(path), "text/markdown")
    # 对外发送必须审批：建审批单，任务等待
    from . import approvals as ap
    aid = ap.create(db, "publish", f"审批：发送询盘回复（任务 #{task['id']}）",
                    {"task_id": task["id"], "draft_file": str(path),
                     "task_phase": "mid",
                     "note": "v0.1 未实现自动发送；批准仅表示草稿可用，需人工发送"},
                    amount_cny=0)
    db.execute("UPDATE tasks SET needs_approval=1, approval_id=?, status='awaiting_approval',"
               " updated_at=? WHERE id=?", (aid, now_iso(), task["id"]))
    log("warning", f"回复草稿已生成，发送前需人工审批（审批单 #{aid}）；v0.1 不实现自动发送")
    return f"询盘分析完成，草稿 {fname} 待审批发送（审批单 #{aid}）"


def handle_inquiry_simulate(db, task, log):
    from .modules.inquirypilot import SAMPLE_INQUIRIES
    payload = task["payload"] or {}
    count = min(int(payload.get("count", 3)), 10)
    log("info", f"生成 {count} 条模拟询盘，逐条创建处理任务（全部标记模拟）")
    for i,inq in enumerate(SAMPLE_INQUIRIES[:count]):
        tid = tasks_mod.create_task(
            db, type="inquiry_process", title=f"处理模拟询盘 #{i+1}",
            payload={"inquiry_text": inq, "simulated": True},
            module_key="inquirypilot", priority=1)
        log("info", f"  -> 子任务 #{tid}（模拟）")
    return f"已创建 {count} 个模拟询盘处理任务"


def handle_llm_probe(db, task, log):
    from .llm import get_provider, NotConfigured
    payload = task["payload"] or {}
    provider_name = payload.get("provider") or tasks_mod.get_setting(db, "llm_provider", "local")
    prompt = payload.get("prompt") or payload.get("input_text") or ""
    if not prompt:
        raise ValueError("payload.prompt 为空")
    provider = get_provider(provider_name)
    log("info", f"调用模型：{provider.label}")
    try:
        result = provider.complete(prompt)
    except NotConfigured as e:
        # 转人工：把提示词展示给用户去订阅里跑，粘贴回来继续
        tasks_mod.request_input(
            db, task["id"],
            f"[需人工] {provider.label}不可用：{e}\n\n请将以下提示词粘贴到你的 ChatGPT/Claude 订阅中，"
            f"把回答粘贴回本任务继续：\n\n{prompt[:2000]}")
        log("warning", "Provider 未配置，转人工粘贴流程（awaiting_input）")
        return "等待人工输入"
    cost = float(result.get("cost_cny", 0) or 0)
    if cost > 0:
        finance_mod.record(db, "api_cost", cost, category=f"llm/{provider.name}",
                           description=f"任务 #{task['id']} 模型调用 "
                           f"({result.get('input_tokens',0)}+{result.get('output_tokens',0)} tokens)",
                           ref_type="task", ref_id=task["id"], is_simulated=False)
        log("info", f"API 消耗已记账：¥{cost}")
    else:
        log("info", "本地规则引擎：零费用")
    art_dir = Path(db.path).parent / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    fname = f"llm-{task['id']}.md"
    (art_dir / fname).write_text(result["text"], encoding="utf-8")
    tasks_mod.save_artifact(db, task["id"], fname, str(art_dir / fname), "text/markdown")
    return f"模型调用完成（{provider.label}，费用 ¥{cost}）"


HANDLERS = {
    "market_research": handle_market_research,
    "sample_fulfillment": handle_sample_fulfillment,
    "simulated_order": handle_simulated_order,
    "inquiry_process": handle_inquiry_process,
    "inquiry_simulate": handle_inquiry_simulate,
    "llm_probe": handle_llm_probe,
}


# ---------------- 驱动 ----------------

def run_task_once(db, task_id):
    """同步执行单个任务（供演示脚本/测试用）。返回 (ok, summary)。"""
    task = tasks_mod.get_task(db, task_id)
    if not task:
        raise ValueError(f"task {task_id} not found")
    from .db import now_iso
    db.execute("UPDATE tasks SET status='running', started_at=?, attempts=attempts+1,"
               " updated_at=? WHERE id=?", (now_iso(), now_iso(), task_id))
    task = tasks_mod.get_task(db, task_id)

    def log(level, msg):
        _log(db, task_id, level, msg)

    handler = HANDLERS.get(task["type"])
    if not handler:
        tasks_mod.finish(db, task_id, False, error=f"未知任务类型 {task['type']}")
        return False, "unknown type"
    timeout = task["timeout_sec"] or 300
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_run_handler, db, handler, task, log)
        try:
            ok, summary = fut.result(timeout=timeout)
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            log("error", f"handler 异常：{e}\n{tb}")
            ok, summary = False, ""
            err = str(e)
        else:
            err = ""
    # awaiting_input / awaiting_approval 是 handler 主动设置的终态之一，不覆盖
    cur_status = tasks_mod.get_task(db, task_id)["status"]
    if cur_status == "running":
        if ok:
            tasks_mod.finish(db, task_id, True, result_summary=summary)
        else:
            retried = tasks_mod.schedule_retry(db, task_id, err or summary)
            log("warning", "将重试" if retried else "重试次数用尽，任务失败")
    return ok, summary


def _run_handler(db, handler, task, log):
    try:
        summary = handler(db, task, log)
        return True, summary or ""
    except Exception as e:
        return False, str(e)


def worker_loop(db, stop_event, poll_interval=2.0):
    from .db import now_iso
    _log_system(db, "worker 启动")
    while not stop_event.is_set():
        try:
            task_id = tasks_mod.claim_next(db)
            if task_id:
                run_task_once(db, task_id)
            else:
                time.sleep(poll_interval)
        except Exception as e:
            try:
                db.execute("INSERT INTO task_logs(task_id,ts,level,message) VALUES(?,?,?,?)",
                           (0, now_iso(), "error", f"worker loop 异常：{e}"))
            except Exception:
                pass
            time.sleep(poll_interval)


def _log_system(db, msg):
    from .db import now_iso
    try:
        db.execute("INSERT INTO task_logs(task_id,ts,level,message) VALUES(?,?,?,?)",
                   (0, now_iso(), "info", msg))
    except Exception:
        pass


def start_worker(db):
    stop = threading.Event()
    t = threading.Thread(target=worker_loop, args=(db, stop), daemon=True,
                         name="wanai-worker")
    t.start()
    return stop, t
