# v0.2 架构与真实能力边界

## 为什么不直接复制 20 个热门 Agent 项目

以可运营结果优先：已有 FastAPI、SQLite、Jinja2、Worker 够用；多 Agent 框架和分布式微服务会先带来成本与故障点。业务找到付费客户后再换栈。

借鉴而非依赖：
- FastAPI 官方 full-stack-fastapi-template：测试、鉴权、后台安全、CI/CD；https://github.com/fastapi/full-stack-fastapi-template
- LangGraph：任务持久化、人工审批、不可重复执行的副作用；https://github.com/langchain-ai/langgraph
- n8n：成熟的系统间自动化与多连接器设计（许可证为 fair-code，商用前核查）；https://github.com/n8n-io/n8n
- LiteLLM：模型成本计算、预算预留、分级限额的成熟思路；https://github.com/BerriAI/litellm
- Stripe：服务端 webhook 验签、事件幂等、不要信任浏览器成功页；https://docs.stripe.com/webhooks
- OWASP CSRF Cheat Sheet：Origin/Referer、Fetch Metadata、操作保护；https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
- Paul Graham, Do Things That Don't Scale：先人工争取首批客户，深度理解需求再规模化；https://paulgraham.com/ds.html

## 本版新增

- leads：入站线索去重、任务关联、状态、优先级、待审草稿及人工结果标记。
- IMAP：TLS、只读 UNSEEN+UID 读取，自动每 5 分钟轮询；不发送、不修改已读状态。
- SMTP：管理员确认才调用 STARTTLS 发出单封邮件；用 CAS 防止并发重发；发送结果不确定时进入 failed 隔离。
- HMAC 入站：独立站需由可信后端对原始请求体签名，不能把共享密钥暴露在前端 JavaScript。
- payments：仅签名校验后接收 Stripe checkout.session.completed，session_id/event_id 去重；真实 CNY 付款单独记账，测试模式为模拟，其他币种暂不折算。付款并不自动履约。
- owner 控制台：Basic Auth（设置密码后）、跨站 POST 检查、仅本地无密码可访问。
- 用户看板：真实线索数量、草稿、回复、手工转化、财务分真实/模拟。
- 启动保护：系统重启发现残留 running 任务时标失败隔离，不自动再次执行可能有外部副作用的任务。
- 安全抓取：只允许 HTTP(S) 公网解析地址，禁用重定向跟随；仍需生产部署的网络出口限制抵御 DNS rebinding。

## 尚未实现 / 不得谎称完成

- 完全免配置云托管、商户账户代开、营销网络和自动获取客户。
- Stripe 订阅自动续费、完整退款/争议事件、各币种结算、税务发票和银行对账。
- 多租户客户隔离、公开注册与登录、客户自助开通、订阅配额、自动交付客户使用权限。
- 自动调价、自我训练、可执行商业实验的政策引擎。
- 强分布式持久队列、进程级任务硬超时：当前 Python 线程池的超时不足以杀死运行中的线程。
- OAuth 企业邮箱连接器和海量邮件历史重算。
- 客服升级/敏感内容过滤、保留期删除 UI 与企业级合规审计。

## 发布前验收规则

1. pytest 全绿，Windows 真实环境完成启动、登录、导入、审批、SMTP 发送和重启测试。
2. 试验邮箱实收、实发至少一封；失联后恢复以及断网故障均测试，无重复发送。
3. Stripe 测试事件/真实小额付款各一次，并核对外部订单及 CNY 真实口径。
4. 业务数据每日自动/手工备份，恢复演练能成功。
5. 明确谁负责客户协议、数据处理告知、退款与税务；未完成时不对公网销售。
