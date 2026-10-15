"""无界面端到端演示（验收用）。

直接调用 worker 同步跑完：市场研究 -> 样品制作 -> 模拟订单，
打印每一步结果与台账。网络不可用时研究引擎会自动降级，不中断链路。

用法：python scripts/demo_e2e.py [--db data/demo.db] [--topic "xxx"]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import finance as fin_mod
from app import tasks as tasks_mod
from app import worker as worker_mod
from app.db import Database, now_iso


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/demo.db")
    ap.add_argument("--topic", default="AI 外贸询盘处理工具")
    args = ap.parse_args()

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    db = Database(db_path)
    for k, v in {"monthly_budget_cny": "5000", "approval_threshold_cny": "50",
                 "llm_provider": "local"}.items():
        db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))

    # 注册模块（触发 InquiryPilot 注册；demo 不需要它，但保证导入无副作用）
    import app.modules.inquirypilot  # noqa: F401

    steps = []
    t0 = time.time()

    def run_step(step_name, **kw):
        tid = tasks_mod.create_task(db, **kw)
        print(f"[演示] {step_name} -> 任务 #{tid} 执行中...", flush=True)
        ok, summary = worker_mod.run_task_once(db, tid)
        status = tasks_mod.get_task(db, tid)["status"]
        print(f"[演示] {step_name} -> {status}：{summary}", flush=True)
        steps.append((step_name, status))
        return ok

    ok1 = run_step("市场研究", type="market_research",
                   title=f"演示：市场研究（{args.topic}）",
                   payload={"topic": args.topic}, timeout_sec=600)
    ok2 = run_step("样品制作", type="sample_fulfillment", title="演示：样品制作",
                   payload={"brief": f"基于《{args.topic}》研究报告制作产品介绍单页",
                            "format": "html"})
    ok3 = run_step("模拟订单", type="simulated_order", title="演示：模拟订单",
                   payload={"item": "演示商品·AI 询盘跟进服务", "amount_cny": 199,
                            "customer": "模拟客户"}, module_key="demo")

    totals = fin_mod.totals(db)
    print("\n===== 演示结果 =====")
    for title, status in steps:
        print(f"  {title}: {status}")
    print(f"  真实收入: ¥{totals['revenue_real']} / 模拟收入: ¥{totals['revenue_sim']}")
    print(f"  真实成本: ¥{totals['cost_real']}（API 0 + 支出 0，应为 0）")
    print(f"  用时: {time.time()-t0:.1f}s  数据库: {db_path}")
    ok = ok1 and ok2 and ok3 and totals["revenue_real"] == 0 \
        and totals["revenue_sim"] == 199 and totals["cost_real"] == 0
    print("端到端演示：" + ("通过 ✓" if ok else "未完全通过 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
