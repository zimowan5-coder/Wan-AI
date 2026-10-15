# Wan's AI Business OS v0.1 架构说明

## 1. 设计目标

可扩展的 AI 商业经营平台，不是绑定单一商业模式的软件：

- **A. 经营核心（Business OS）**：任务调度、Agent/工具执行、审批闸口、财务台账、经营监控。
- **B. 可插拔业务模块**：每个模块是一门生意，复用统一的任务/支付/客户/分析基础设施。

## 2. 架构图（v0.1）

```
                    ┌─────────────────────────────┐
                    │  Web 控制台 (FastAPI+Jinja2)  │
                    │  dashboard/tasks/research/   │
                    │  finance/approvals/modules   │
                    └──────────────┬──────────────┘
                                   │ 读写
                    ┌──────────────▼──────────────┐
                    │  SQLite (WAL)               │
                    │  tasks/task_logs/research_  │
                    │  reports/ledger/orders/     │
                    │  approvals/settings/        │
                    │  artifacts/projects/modules │
                    └──────────────┬──────────────┘
                                   │ 轮询 claim
                    ┌──────────────▼──────────────┐
                    │  Worker（后台线程）          │
                    │  handlers: market_research / │
                    │  sample_fulfillment /        │
                    │  simulated_order /           │
                    │  inquiry_process /           │
                    │  inquiry_simulate / llm_probe│
                    └──────┬───────────────┬──────┘
                           │               │
                 ┌─────────▼──────┐  ┌──────▼──────────┐
                 │ research.py    │  │ llm.py providers│
                 │ 真抓取+确定性   │  │ local(免费)/     │
                 │ 评分+中文报告   │  │ openai/anthropic│
                 └────────────────┘  │ /gemini(需Key)  │
                                     └─────────────────┘
```

关键决策：

- **单进程双线程**（Web + Worker）：v0.1 阶段零运维，双击即用；SQLite WAL + 全局写锁保证并发安全。数据量上来后再拆。
- **任务状态机**：pending → running → succeeded/failed（含重试）；awaiting_approval（人审闸口）；awaiting_input（人肉 bridge）。claim 用锁内 SELECT+UPDATE，保证不重复执行。
- **审批分两种**：`pre`（执行前，如超预算任务）批准后回队列；`mid`（执行中，如询盘草稿待发）批准后直接成功——避免重跑死循环。
- **模型层可插拔**：`llm.py` Provider 接口。没 Key 就抛 `NotConfigured`，任务转 `awaiting_input`，用户把提示词贴到自己的 Plus/Pro 订阅里、把回答粘贴回来继续——诚实且零成本。
- **财务铁律**：`ledger.is_simulated` 区分真实/模拟；控制台真实盈利区永远看不到模拟收入。

## 3. 什么是真的 / 什么是桩（v0.1 验收对照表）

| 能力 | 状态 | 说明 |
|---|---|---|
| 任务创建/调度/重试/超时/日志 | ✅ 真 | Worker 真实轮询执行 |
| 市场研究：搜索+抓取+评分+报告 | ✅ 真 | 真发 HTTP 请求、真解析页面；搜索被反爬时降级并如实标注 |
| 样品制作 | ✅ 真 | 真写文件到 data/artifacts，任务关联可查 |
| 模拟订单→台账 | ✅ 真 | 全链路真跑，但打「模拟」标、不进真实口径 |
| 审批闸口 | ✅ 真 | 批准/拒绝联动任务状态，全程留痕 |
| 财务台账 | ✅ 真 | 真实记账；API 消耗只有真实调用才记 |
| 本地规则引擎（local provider） | ✅ 真 | 抽取式处理，零费用，明确标识非大模型 |
| OpenAI/Anthropic/Gemini 调用 | ⚠️ 桩 | 代码已实现，需 API Key；没 Key 时转人工，不扣费 |
| 询盘自动发送（邮件/IM） | ❌ 未实现 | 只产出草稿+审批单；发送需人工 |
| 真实询盘来源接入 | ❌ 未实现 | 见 INQUIRYPILOT_ASSESSMENT.md |
| 在线支付/收款 | ❌ 未实现 | v0.2 接入（Stripe/支付宝按经营地区定） |

## 4. 目录结构

```
app/            # 后端：db/tasks/worker/llm/finance/approvals/main
app/modules/    # 业务模块（base 注册表 + inquirypilot）
app/research.py # 市场研究引擎
web/templates   # Jinja2 页面（无 CDN，离线可用）
web/static      # 原生 CSS/JS
tests/          # pytest（14 用例）
scripts/        # start_windows.bat / start.sh / demo_e2e.py
docs/           # 本文档集
data/           # SQLite + artifacts（运行时生成，不入库）
```

## 5. 安全与成本

- 凭证只走环境变量/`.env`（不入库、不进日志）。
- 预算硬顶：`monthly_budget_cny` 默认 5000；超 `approval_threshold_cny`（默认 50）自动进审批。
- 对外发布/账号操作/不可逆动作一律走审批，默认全部拒绝自动执行。
- v0.1 真实花费：0（本地运行 + 免费搜索 + 本地规则引擎）。
