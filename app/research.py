# -*- coding: utf-8 -*-
"""
market research engine — 市场研究引擎（Wan's AI Business OS v0.1）

职责：
    给定一个研究主题(topic)，执行“免费公开信息收集 -> 确定性规则评分 ->
    中文 Markdown 报告”的全链路。本模块是 v0.1 的**研究侧 MVP**，只做
    需求信号的初步发现与排序，不做商业立项承诺。

诚实标记约定（全系统统一）：
    - "local-rules"        : 本模块实际的 AI 模式 —— 本地规则引擎，
                            未调用任何付费 AI API，不需要 API Key。
    - "demo"/"simulated"  : 演示或模拟数据，禁止写入正式经营台账。
    - "unverified"         : 未经验证的信号（本模块抓取到的需求信号全部
                            是 unverified，需真实用户访谈/询盘数据才能立项）。

硬约束：
    1. 仅使用 Python 标准库 + httpx；不调用付费 AI API。
    2. 所有网络操作 best-effort：超时/被墙/解析失败一律降级返回，
       不抛异常，并在 notes / 报告中诚实记录。
    3. 评分逻辑确定性（无 random、无时间随机），相同输入相同输出。
    4. 全中文输出（含代码中的注释与返回给用户看的文案）。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

try:
    import httpx  # noqa: F401
except ImportError:  # 允许无 httpx 的环境：网络函数降级为空结果
    httpx = None

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

AI_MODE_TEXT = "本地规则引擎（未调用付费 API）"
AI_MODE_CODE = "local-rules"

DDG_URL = "https://html.duckduckgo.com/html/"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 评分信号词（确定性、人工设定的关键词清单）
DEMAND_KEYWORDS = ["需求", "痛点", "求推荐", "哪里买", "多少钱", "价格", "怎么样", "靠谱"]
COMPETITION_KEYWORDS = ["竞品", "对比", "排行榜", "十大", "测评"]
AUTOMATION_KEYWORDS = ["自动化", "AI", "批量", "模板", "流程", "无人值守"]
RISK_KEYWORDS = ["骗局", "封号", "法律", "合规", "风险", "诈骗"]

PRICE_PATTERNS = [
    r"¥\s?\d[\d,]*",      # ¥500 / ¥ 1,200
    r"\$\s?\d[\d,]*",     # $29 / $ 99
    r"\d[\d,]*\s*元",     # 499元 / 1200 元
]

def _http_get(url: str, headers: dict, timeout: int):
    """发一个 GET 请求。显式处理代理：

    - 优先读 https_proxy/https_proxy/http_proxy 环境变量；
    - trust_env=False，绕开 httpx 对畸形 no_proxy 条目（如 [::1]）的解析 bug；
    - 目标机器无代理变量时即为直连。
    """
    import os
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1") or host.startswith("192.168.")
    proxy_url = (
        os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    kwargs = {
        "timeout": timeout,
        "headers": headers,
        "follow_redirects": True,
        "trust_env": False,
    }
    if proxy_url and not local:
        kwargs["proxy"] = proxy_url
    with httpx.Client(**kwargs) as client:
        return client.get(url)


_BEIJING = timezone(timedelta(hours=8))


def _now_str() -> str:
    """返回北京时间的当前时间字符串（确定格式，不影响评分逻辑）。"""
    return datetime.now(_BEIJING).strftime("%Y-%m-%d %H:%M")


def _clean_ws(text: str) -> str:
    """压缩空白：把连续空白统一成单个空格，并去掉首尾空白。"""
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------------------------------------------------------
# 1. 搜索
# ----------------------------------------------------------------------------

class _DDGParser(HTMLParser):
    """解析 DuckDuckGo html 版结果页，提取 a.result__a 链接的标题与地址。"""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self._in_link = False
        self._href = ""
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._in_snippet = False
        self._last_result_idx = -1

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "a" and "result__a" in cls.split():
            self._in_link = True
            self._href = attrs.get("href", "")
            self._title_parts = []
        if tag == "a" and "result__snippet" in cls.split():
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag):
        if tag == "a":
            if self._in_link:
                self.results.append({
                    "title": _clean_ws(unescape("".join(self._title_parts))),
                    "url": self._href.strip(),
                    "snippet": "",
                })
                self._last_result_idx = len(self.results) - 1
                self._in_link = False
                self._href = ""
            if self._in_snippet:
                if self._last_result_idx >= 0:
                    self.results[self._last_result_idx]["snippet"] = _clean_ws(
                        unescape("".join(self._snippet_parts)))
                self._in_snippet = False

    def handle_data(self, data):
        if self._in_link:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


def _is_redirect_url(url: str) -> bool:
    """判断是否为站内跳转链接（DDG 的 //duckduckgo.com/l/?uddg= 等形式）。"""
    u = url.strip().lower()
    if not u:
        return True
    if u.startswith("//duckduckgo.com/l/") or "duckduckgo.com/l/" in u:
        return True
    parsed = urlparse(u if "://" in u else "https://" + u)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        return True
    return False


def search_web(query: str, max_results: int = 8, timeout: int = 20) -> list[dict]:
    """用 DuckDuckGo html 版抓取搜索结果。

    返回 [{"title", "url", "snippet"}]，失败返回 []（不抛异常）。
    注意：结果经过去重，并过滤掉站内跳转链接。
    """
    if httpx is None or not query.strip():
        return []
    try:
        resp = _http_get(
            DDG_URL + "?" + urlencode({"q": query}),
            headers={"User-Agent": BROWSER_UA},
            timeout=timeout,
        )
        if resp.status_code != 200 or not resp.text:
            return []
        parser = _DDGParser()
        parser.feed(resp.text)
        seen: set[str] = set()
        out: list[dict] = []
        for item in parser.results:
            url = item.get("url", "").strip()
            if not url or _is_redirect_url(url):
                continue
            key = url.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            if not item.get("title"):
                continue
            out.append({
                "title": item["title"],
                "url": url,
                "snippet": item.get("snippet", ""),
            })
            if len(out) >= max_results:
                break
        return out
    except Exception:
        # best-effort：超时、被墙、解析失败都降级为 []
        return []


# ----------------------------------------------------------------------------
# 2. 抓取页面
# ----------------------------------------------------------------------------

class _PageParser(HTMLParser):
    """提取 <title> 与正文文本，丢弃 script/style/nav/aside/header/footer 内容。"""

    _SKIP = {"script", "style", "nav", "aside", "header", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            self.text_parts.append(data)


def fetch_page(url: str, timeout: int = 15, max_chars: int = 20000) -> dict:
    """抓取单个页面并提取标题与正文。

    返回 {"url", "title", "text", "ok": bool, "error": str|None}。
    失败时 ok=False 且 error 说明原因，不抛异常。
    """
    result = {"url": url, "title": "", "text": "", "ok": False, "error": None}
    if httpx is None:
        result["error"] = "httpx 未安装"
        return result
    if not url or not url.strip():
        result["error"] = "空 URL"
        return result
    try:
        resp = _http_get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}"
            return result
        html = resp.text or ""
        parser = _PageParser()
        try:
            parser.feed(html[:200000])  # 防止超大页面卡死解析
        except Exception:
            pass
        title = _clean_ws(unescape("".join(parser.title_parts)))
        text = _clean_ws(unescape("".join(parser.text_parts)))
        if max_chars > 0:
            text = text[:max_chars]
        result["title"] = title
        result["text"] = text
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001 —— best-effort 约定，记录不抛出
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


# ----------------------------------------------------------------------------
# 3. 确定性评分
# ----------------------------------------------------------------------------

def _count_hits(text: str, keywords: list[str]) -> int:
    """统计文本中各关键词的出现次数之和（关键词本身互斥，无需去重）。"""
    return sum(text.count(kw) for kw in keywords)


def _find_prices(text: str) -> list[str]:
    """用价格正则提取价格信号，去重并保留原样，最多 20 个。"""
    found: list[str] = []
    for pat in PRICE_PATTERNS:
        for m in re.findall(pat, text):
            m = _clean_ws(m)
            if m and m not in found:
                found.append(m)
                if len(found) >= 20:
                    return found
    return found


def _find_risk_flags(text: str) -> list[str]:
    """找出文本中出现的风险词（去重、保持固定顺序）。"""
    return [kw for kw in RISK_KEYWORDS if kw in text]


def score_opportunities(topic: str, pages: list[dict]) -> dict:
    """对候选机会做确定性规则评分。

    输入：topic（研究主题）、pages（fetch_page 的返回列表）。
    输出：{"opportunities": [按 score_0_100 降序], "notes": [...]}。

    每个机会含：name, demand_score(0-10), competition_score(0-10),
    price_signals[], automation_fit(0-10), risk_flags[], evidence[](url),
    score_0_100 = 需求*40% + 自动化*30% + (10-竞争)*20% - 风险词数*5（最低 0）。

    无有效页面时返回空机会列表，并在 notes 里诚实说明。
    """
    notes: list[str] = []
    valid_pages = [p for p in (pages or []) if p.get("ok") and p.get("text")]
    if not valid_pages:
        notes.append("未能抓取到任何有效页面文本，本次无法基于真实数据生成机会候选；"
                     "返回的报告为框架性报告，缺失数据已如实标注。")
        return {"opportunities": [], "notes": notes}

    candidates: list[dict] = []
    for idx, page in enumerate(valid_pages):
        text = page.get("text", "")
        url = page.get("url", "")
        title = page.get("title", "") or f"来源页面{idx + 1}"

        demand_hits = _count_hits(text, DEMAND_KEYWORDS)
        comp_hits = _count_hits(text, COMPETITION_KEYWORDS)
        auto_hits = _count_hits(text, AUTOMATION_KEYWORDS)
        risk_flags = _find_risk_flags(text)
        price_signals = _find_prices(text)

        # 确定性打分：关键词命中数映射到 0-10（分段函数，无随机性）
        demand_score = min(10, max(1, demand_hits // 3 + (1 if demand_hits else 0)))
        competition_score = min(10, comp_hits // 2)
        automation_fit = min(10, auto_hits // 2 + (1 if auto_hits else 0))

        score = (
            demand_score * 0.4
            + automation_fit * 0.3
            + (10 - competition_score) * 0.2
            - len(risk_flags) * 5
        )
        score_0_100 = round(max(0, score) * 10, 1)

        name_hint = title[:24] if title else topic
        candidates.append({
            "name": f"{topic}机会#{idx + 1}（源自「{name_hint}」）",
            "demand_score": demand_score,
            "competition_score": competition_score,
            "price_signals": price_signals,
            "automation_fit": automation_fit,
            "risk_flags": risk_flags,
            "evidence": [url] if url else [],
            "score_0_100": score_0_100,
        })

    # 取 top 3-5 个（不足 3 个则全取；多余则取前 5）
    candidates.sort(key=lambda c: (-c["score_0_100"], c["name"]))
    top = candidates[:5] if len(candidates) >= 5 else candidates

    notes.append(f"评分基于 {len(valid_pages)} 个抓取成功的页面文本，全部为确定性规则打分，"
                 f"未调用付费 AI API。")
    notes.append("需求信号仅为公开页面的关键词统计，属于 unverified（未验证）信号，"
                 "不能作为需求真实性的证据；立项前需要真实用户访谈或询盘数据。")
    notes.append(f"抓取失败的页面未纳入评分（本次共输入 {len(pages or [])} 页，"
                 f"有效 {len(valid_pages)} 页）。")
    return {"opportunities": top, "notes": notes}


# ----------------------------------------------------------------------------
# 4. 报告渲染
# ----------------------------------------------------------------------------

def _fmt_price_signals(analysis: dict) -> str:
    """汇总所有机会的价格信号，去重。"""
    all_prices: list[str] = []
    for opp in analysis.get("opportunities", []):
        for p in opp.get("price_signals", []):
            if p not in all_prices:
                all_prices.append(p)
    if not all_prices:
        return "本次抓取中未发现明确的价格数字信号。"
    return "、".join(all_prices[:30])


def render_report(topic: str, pages: list[dict], analysis: dict, meta: dict) -> str:
    """生成中文 Markdown 研究报告。"""
    created_at = meta.get("created_at", _now_str())
    sources_n = meta.get("sources_count", len(pages or []))
    opps = analysis.get("opportunities", [])
    notes = analysis.get("notes", [])

    ok_pages = [p for p in (pages or []) if p.get("ok")]
    has_data = bool(ok_pages and opps)

    lines: list[str] = []
    lines.append(f"# 市场研究报告：{topic}")
    lines.append("")
    lines.append("## 元信息")
    lines.append("")
    lines.append(f"- 研究时间：{created_at}")
    lines.append(f"- AI 模式：{AI_MODE_TEXT}")
    lines.append(f"- 来源数：{sources_n}（抓取成功 {len(ok_pages)}）")
    lines.append("")

    lines.append("## 执行摘要")
    lines.append("")
    if has_data:
        best = opps[0]
        lines.append(
            f"本次围绕「{topic}」共抓取 {sources_n} 个来源、成功解析 {len(ok_pages)} 页，"
            f"基于本地规则引擎生成 {len(opps)} 个机会候选。得分最高的是"
            f"「{best['name']}」（{best['score_0_100']} 分），其需求信号强度 "
            f"{best['demand_score']}/10，自动化适配度 {best['automation_fit']}/10。"
            f"以下分数均为关键词统计的确定性打分，尚未经过真实用户验证。"
        )
    else:
        lines.append(
            f"本次围绕「{topic}」未能获取到可用的公开页面数据（搜索或抓取均未成功），"
            f"因此无法给出基于真实数据的机会判断。本报告为框架性报告：保留了研究方法、"
            f"评分标准与局限说明，机会评分表为空。请检查网络环境后重新运行。"
        )
    lines.append("")

    lines.append("## 机会评分表")
    lines.append("")
    if opps:
        lines.append("| 机会 | 需求(0-10) | 竞争(0-10) | 自动化适配(0-10) | 风险词数 | 综合得分(0-100) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for o in opps:
            lines.append(
                f"| {o['name']} | {o['demand_score']} | {o['competition_score']} | "
                f"{o['automation_fit']} | {len(o['risk_flags'])} | {o['score_0_100']} |"
            )
    else:
        lines.append("（无数据：未能基于真实抓取生成机会候选。）")
    lines.append("")

    lines.append("## 机会详情")
    lines.append("")
    if opps:
        for o in opps:
            lines.append(f"### {o['name']}")
            lines.append("")
            ev = o.get("evidence") or []
            lines.append("- 证据来源：")
            for u in ev:
                lines.append(f"  - {u}")
            risks = o.get("risk_flags") or []
            if risks:
                lines.append(f"- ⚠️ 风险提示：页面文本中出现风险词「{'、'.join(risks)}」，"
                             f"需在立项前逐项核查。")
            else:
                lines.append("- ⚠️ 风险提示：未在抓取文本中发现风险关键词，"
                             "但这不代表没有风险——免费搜索覆盖有限。")
            ps = o.get("price_signals") or []
            lines.append(f"- 价格信号：{('、'.join(ps[:10]) if ps else '未发现明确价格数字')}")
            lines.append("")
    else:
        lines.append("（无数据，本节留空。）")
        lines.append("")

    lines.append("## 价格信号汇总")
    lines.append("")
    lines.append(_fmt_price_signals(analysis))
    lines.append("")

    lines.append("## 方法与局限")
    lines.append("")
    lines.append(f"- 方法：{AI_MODE_TEXT}；信号词命中统计 + 价格正则 + 确定性加权打分，"
                 "全程无随机因素。")
    lines.append("- 局限 1：免费搜索（DuckDuckGo html 版）覆盖有限，且可能被反爬/超时阻断；"
                 "本次未成功的抓取已在报告中如实标注。")
    lines.append("- 局限 2：所谓“需求信号”只是公开页面关键词出现次数的统计，"
                 "不能证明需求真实存在或有人愿意付费。")
    lines.append("- 局限 3：竞争、风险判断同样基于公开文本，未做深度尽调。")
    lines.append("- 下一步：如需立项，必须补充真实用户访谈、询盘数据或小规模付费测试；"
                 "本报告仅供研究起点，不构成商业决策依据。")
    if notes:
        lines.append("")
        lines.append("## 引擎备注")
        lines.append("")
        for n in notes:
            lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 5. 编排全流程
# ----------------------------------------------------------------------------

def _safe_log(log, level: str, msg: str) -> None:
    """调用可选的 log 回调，回调出错也不影响主流程。"""
    if log is None:
        return
    try:
        log(level, msg)
    except Exception:
        pass


def run_research(topic: str, log=None, max_results: int = 8,
                 seed_urls: list = None) -> dict:
    """编排市场研究全流程：search -> fetch -> score -> render。

    seed_urls：调用方可直接提供一批已知 URL（每行一个），跳过搜索直接抓取。
    适用于搜索被反爬拦截、或用户已有目标来源的场景。
    log 是可选回调 log(level: str, msg: str)。
    返回 {"topic", "report_md", "sources", "analysis", "stats", "ai_mode", "created_at"}。
    即使搜索/抓取全失败也会返回一份注明数据缺失的框架性报告，绝不抛异常。
    """
    started_at = _now_str()
    stats = {"results_found": 0, "pages_fetched": 0, "pages_ok": 0}
    results: list[dict] = []
    pages: list[dict] = []
    analysis = {"opportunities": [], "notes": []}

    try:
        _safe_log(log, "info", f"开始研究主题：{topic}")
        seeds = [u.strip() for u in (seed_urls or []) if u and u.strip()]
        if seeds:
            results = [{"title": u, "url": u, "snippet": "用户提供的种子 URL"} for u in seeds[:max_results]]
            _safe_log(log, "info", f"使用 {len(results)} 个种子 URL，跳过搜索")
        else:
            results = search_web(topic, max_results=max_results) or []
            if not results:
                _safe_log(log, "warning",
                          "搜索未返回结果（可能被反爬/网络受限），"
                          "可改用种子 URL 方式：在任务 payload 里传 seed_urls")
        stats["results_found"] = len(results)
        _safe_log(log, "info", f"搜索完成，找到 {len(results)} 个结果")

        for r in results:
            stats["pages_fetched"] += 1
            page = fetch_page(r.get("url", ""))
            if not page.get("title") and r.get("title"):
                page["title"] = r["title"]
            pages.append(page)
            if page.get("ok"):
                stats["pages_ok"] += 1
                _safe_log(log, "info", f"抓取成功：{page['url'][:80]}")
            else:
                _safe_log(log, "warning", f"抓取失败：{page.get('url','')[:80]}（{page.get('error')}）")

        analysis = score_opportunities(topic, pages)
        _safe_log(log, "info", f"评分完成，生成 {len(analysis.get('opportunities', []))} 个机会候选")
    except Exception as exc:  # noqa: BLE001 —— 编排层兜底，保证绝不抛异常
        note = f"研究流程中出现未预期异常，已降级为框架性报告：{type(exc).__name__}: {exc}"
        analysis.setdefault("notes", []).append(note)
        _safe_log(log, "error", note)

    meta = {"created_at": started_at, "sources_count": len(results)}
    report_md = render_report(topic, pages, analysis, meta)

    sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]

    _safe_log(log, "info", "研究报告已生成")
    return {
        "topic": topic,
        "report_md": report_md,
        "sources": sources,
        "analysis": analysis,
        "stats": stats,
        "ai_mode": AI_MODE_CODE,
        "created_at": started_at,
    }
