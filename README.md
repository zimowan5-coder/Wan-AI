# Wan's AI · Revenue OS v0.2（本地试点版）

**真实询盘 → 自动分类 → 待审回复 → 单封发送 → 商户验签收款 → 分账/复盘。**

> 核心原则：没有真实客户就没有真实收入。默认不会群发营销邮件、不会自动支出或假装已收款。
> 当前为可运行的试点系统，不是公开多租户 SaaS，也未完成真实商业验证。

## 不会写代码也能启动

1. 安装 Python 3.11 或 3.12（Windows 安装时勾选 Add to PATH）。
2. Windows 双击 `scripts\\start_windows.bat`；macOS/Linux 运行 `bash scripts/start.sh`。
3. 脚本首次启动自动生成强管理员密码，**只显示一次，请记下**。浏览器打开 http://127.0.0.1:8931。
4. 管理员用户名固定为 `admin`，密码为刚才生成的密码。
5. 点击导航栏“首次配置”，在页面表单填入你拥有或有授权的业务邮箱、可选 Stripe 付款链接。无需手工修改代码。

## 现在真正具备的能力

| 能力 | 真实状态 |
|---|---|
| 本地后台 / 任务 / 财务与审批 | 已实现；Python / FastAPI / SQLite |
| 市场研究 | 可抓取公开页面，但信息和商机评分未得到真人市场验证 |
| 真实询盘 | 手动导入 + IMAP 专用业务邮箱轮询 + 签名 webhook |
| 邮件回复 | 自动分类/模板草稿；人工审核并点击发送才单封 SMTP 发出 |
| 公开介绍页 | `/offer`（仅配置 Stripe 付款链接后出现付款按钮） |
| Stripe 收款 | 签名 `checkout.session.completed`，CNY 真/模拟分账；支持 CNY 部分/全部退款事件记账；不支持自动退款发起、自动订阅或争议对账 |
| 首次使用 | 自动生成后台密码、首次配置提示页、建议“今天只做一件事” |
| 安全与恢复 | 私有控制台鉴权、输入约束、防 SSRF 初步处理、异常任务隔离、本地每日备份 |
| AI 引擎 | 默认本地规则；付费 API **默认关闭**，手动粘贴回答可继续工作 |
| 自动获客、自动成交、自动续费、净利润保证 | **没有实现，不能声称有效** |

## 文档与验收

- **`docs/OPERATOR_PLAYBOOK.md`**：给非技术经营者的使用、邮箱、收款、故障、备份说明
- **`docs/GROWTH_EXPERIMENTS.md`**：14 天首次付费验证、试点定价、获客渠道与停损规则
- **`docs/ENGINEERING_DECISIONS.md`**：参考优秀开源项目、结构与未实现部分
- 旧版设计见 `docs/ARCHITECTURE.md`（v0.1 历史）、`docs/ROADMAP.md`（v0.1 历史），以本 README 和最新文档为准。

## 开发者

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

公开页面 `GET /offer`、商户 webhook `POST /api/billing/stripe`、签名自有表单 `POST /api/intake`；后台数据必须通过认证。
不要把 `.env`、`data/` 或密钥上传 GitHub。公网部署前必须完成 HTTPS、访问控制、隐私告知、邮件投递、收款实测、数据恢复演练。
