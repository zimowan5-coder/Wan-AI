# Wan's AI · 自主经营型 AI 商业操作系统（Business OS）v0.1

> 目标：长期运行、逐步自主经营、以真实利润为导向的 AI 商业操作系统。
> v0.1 交付一条**真实可运行**的最小闭环：市场研究 → 样品制作 → 模拟订单 → 经营台账 → 控制台，全部环节可验收、可审计。

## 一分钟启动（Windows）

1. 安装 [Python 3.11+](https://www.python.org/downloads/)（勾选 Add to PATH）
2. 双击 `scripts\start_windows.bat`
3. 浏览器自动打开 http://127.0.0.1:8931

Linux/macOS：`bash scripts/start.sh`

## 控制台里有什么

| 页面 | 能力 |
|---|---|
| 控制台 | 任务状态、待审批数、预算进度（5000 元首轮上限）、真实/模拟收支 |
| 任务 | 新建/查看/重试/取消任务；任务日志；`awaiting_input` 可粘贴 AI 输出继续 |
| 市场研究 | 输入主题（可附种子 URL）→ 真抓取页面 → 确定性评分 → 中文研究报告 |
| 财务 | 台账（API 成本/支出/收入/退款），**真实与模拟永远分开**；手工记账 |
| 审批 | 对外发布/支出/账号操作等人审单：批准/拒绝，全程留痕 |
| 业务模块 | 可插拔生意模块；v0.1 首个模块：InquiryPilot（外贸询盘处理，实验中） |

**端到端演示**：控制台首页点「运行端到端演示」，或 `python scripts/demo_e2e.py`。

## 诚实标记（v0.1 的铁律）

- 没有 API Key 就**不许假装能调大模型**：默认走本地规则引擎（零费用），缺 Key 的任务转人工粘贴流程（用你的 ChatGPT/Claude 订阅当 bridge）。
- 模拟订单/模拟数据永远打「模拟」标，**不计入真实收入**。
- 未实现的功能（自动发送邮件、在线支付、真实询盘来源接入）在文档与界面中明确标注，不画饼。

详见 `docs/ARCHITECTURE.md`（架构与"什么是真的/什么是桩"清单）、`docs/ROADMAP.md`（v0.2 任务）、`docs/INQUIRYPILOT_ASSESSMENT.md`（首个业务模块的证据评估）。

## 技术栈

Python 3.12 · FastAPI · SQLite（WAL）· Jinja2 服务端渲染（无构建步骤、无外部 CDN）· httpx · pytest。
单进程 = Web + 后台 Worker 线程，Windows 双击即用；第一阶段刻意不用 Docker/K8s/微服务。

## 测试

```
.venv\Scripts\activate && pytest tests/ -q
```

## 配置

复制 `.env.example` 为 `.env`。API Key 留空不影响启动，只会让对应模型不可用（任务转人工）。
预算与审批阈值：控制台所用 `settings` 表，默认月预算 5000 元、超 50 元支出自动进审批。
