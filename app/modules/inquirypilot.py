"""InquiryPilot：外贸询盘 AI 处理与跟进（v0.1）。

证据评估结论（写在代码里，防画饼）：
- 真实询盘来源（Alibaba/独立站表单/邮箱）在 v0.1 均未接入：没有账号授权、
  没有历史询盘数据，无法验证"自动获客"链路。
- 因此 v0.1 只做诚实可运行的部分：「来料加工」模式——用户粘贴询盘原文，
  系统做意图分类、关键信息抽取、回复草稿生成；对外发送走人工审批，
  v0.1 不实现自动发送（避免失控）。
- 转正条件（v0.2）：接入 1 个真实询盘来源 + 连续 2 周人工确认草稿可用率 >80%
  + 有第一笔真实跟进回复记录，才开放半自动发送。

状态：experimental（实验中）
"""
import re

from .base import register

ASSESSMENT_SHORT = (
    "证据评估：真实询盘来源未接入，v0.1 仅支持粘贴询盘原文的「来料加工」模式"
    "（分类→抽取→草稿→审批）。自动发送未实现，需要真实来源+人工验证后才开放。"
)

INTENT_RULES = [
    ("询价", ["price", "quotation", "quote", "报价", "多少钱", "价格", "cost"]),
    ("打样", ["sample", "打样", "样品", "prototype"]),
    ("下单", ["order", "下单", "purchase order", "PI", "proforma"]),
    ("售后", ["complaint", "defect", "refund", "售后", "投诉", "退货", "broken"]),
    ("合作", ["distributor", "agency", "代理", "合作", "partnership"]),
]

COUNTRY_HINTS = ["USA", "United States", "UK", "Germany", "France", "Australia",
                 "Canada", "Japan", "UAE", "Saudi", "Brazil", "Mexico", "India",
                 "美国", "英国", "德国", "法国", "澳大利亚", "加拿大", "日本",
                 "阿联酋", "沙特", "巴西", "墨西哥", "印度"]

SAMPLE_INQUIRIES = [
    ("Hi, we are a kitchenware distributor in Germany. We need 5000 pcs of "
     "stainless steel lunch boxes, food grade. Please quote your best FOB price "
     "and lead time. - Thomas, buyer@kueche-de.example"),
    ("你好，我们是做户外用品的贸易公司，想找一款便携折叠桌椅套装，首单2000套，"
     "请报含税出厂价和打样周期。"),
    ("Hello, our order #A-2210 arrived with 3% broken items (ceramic mugs). "
     "We need replacement or refund. Please advise urgently. - Maria, Spain"),
]


def classify_inquiry(text: str) -> dict:
    t = text.lower()
    intent = "一般咨询"
    for label, kws in INTENT_RULES:
        if any(k.lower() in t for k in kws):
            intent = label
            break
    lang = "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    qty = re.findall(r"(\d[\d,]*)\s*(pcs|pieces|sets|units|套|件|个)", text, re.I)
    countries = [c for c in COUNTRY_HINTS if c.lower() in t]
    # 产品关键词：取长度>3 的英文词 / 中文词，去停用词
    words = re.findall(r"[A-Za-z]{4,}|[\u4e00-\u9fff]{2,}", text)
    stop = {"this", "that", "with", "from", "please", "your", "need", "hello",
            "thanks", "我们", "你们", "可以", "一个", "没有"}
    product_keywords = [w for w in words if w.lower() not in stop][:8]
    return {
        "intent": intent,
        "lang": lang,
        "emails": emails[:3],
        "quantities": [f"{q[0]} {q[1]}" for q in qty][:3],
        "countries": countries[:3],
        "product_keywords": product_keywords,
        "urgency": "high" if any(k in t for k in ["urgent", "urgently", "asap", "加急", "紧急"]) else "normal",
    }


INTENT_EN = {"询价": "pricing inquiry", "打样": "sampling", "下单": "order placement",
               "售后": "after-sales issue", "合作": "partnership", "一般咨询": "general inquiry"}


def draft_reply(analysis: dict) -> str:
    intent = analysis["intent"]
    zh = analysis["lang"] == "zh"
    if zh:
        body = (f"您好，感谢您的询盘！我们已收到您关于{intent}的需求，"
                f"{'数量约 ' + '、'.join(analysis['quantities']) + '，' if analysis['quantities'] else ''}"
                f"将在 24 小时内给您详细回复。\n\n"
                "为更快给您准确报价，能否补充：1）目标单价区间 2）交货地/贸易条款 "
                "3）是否需要打样？\n\n顺祝商祺！\nWan's AI · InquiryPilot（草稿，需人工确认后发送）")
    else:
        body = (f"Hello, thanks for your inquiry regarding "
                f"{INTENT_EN.get(intent, 'your inquiry')}! "
                f"We've received your request"
                f"{' for ' + ', '.join(analysis['quantities']) if analysis['quantities'] else ''} "
                f"and will reply in detail within 24 hours.\n\n"
                "To quote accurately, could you share: 1) target price range "
                "2) delivery terms 3) sample needed?\n\nBest regards,\n"
                "Wan's AI · InquiryPilot (draft, human review required before sending)")
    return body


register(
    key="inquirypilot",
    name="InquiryPilot · 外贸询盘处理",
    description="外贸询盘 AI 处理与跟进。" + ASSESSMENT_SHORT,
    status="experimental",
    actions=[
        {"action": "process_inquiry", "label": "处理一条询盘",
         "params_hint": '{"inquiry_text": "粘贴询盘原文"}',
         "task_type": "inquiry_process", "needs_approval": False},
        {"action": "simulate_batch", "label": "模拟批量询盘（测试链路）",
         "params_hint": '{"count": 3}',
         "task_type": "inquiry_simulate", "needs_approval": False},
    ],
)
