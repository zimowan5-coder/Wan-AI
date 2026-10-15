"""模型调用接口：可插拔 Provider。

诚实标记是第一公民：
- "local"：本地规则引擎（抽取式摘要/模板），真实可用、零费用，不许冒充 AI 大模型。
- "openai"/"anthropic"/"gemini"：只有当环境变量里配了对应 API Key 才可用；
  没配 Key 时 complete() 抛 NotConfigured，任务转 awaiting_input 走人工粘贴
  （用户已有 ChatGPT Plus / Claude Pro 订阅，可人肉当 bridge）。
- 每次真实调用都按估算单价记一笔 api_cost 账（is_simulated=0）。

注意：消费者订阅（Plus/Pro）不等于 API 额度，不要混为一谈。
"""
import os
import re

import httpx

CNY_PER_USD = 7.1  # 估算汇率，记账时备注


class NotConfigured(Exception):
    """Provider 不可用（缺 Key）。调用方应转人工流程，而非假装成功。"""


class Provider:
    name = ""
    label = ""

    def complete(self, prompt: str, max_tokens: int = 800) -> dict:
        raise NotImplementedError


# ---------------- 本地规则引擎 ----------------

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")


class LocalProvider(Provider):
    """确定性抽取式处理：取关键句 + 关键词。零成本，真实现、假装不了大模型。"""
    name = "local"
    label = "本地规则引擎（未调用付费 API）"

    def complete(self, prompt: str, max_tokens: int = 800) -> dict:
        text = prompt.strip()
        sents = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
        # 简单打分：含数字/关键词的句子优先
        keywords = ["需求", "价格", "成本", "风险", "机会", "客户", "转化",
                    "price", "cost", "risk", "customer"]
        scored = []
        for s in sents:
            score = sum(1 for k in keywords if k.lower() in s.lower())
            score += len(re.findall(r"\d+", s)) * 0.5
            scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        picked = [s for _, s in scored[:5]] or sents[:3]
        out = "\n".join(f"- {s}" for s in picked)
        out = (f"[本地规则引擎输出，未调用付费 API]\n{out}"
               f"\n\n说明：如需大模型级综合，请配置 API Key 或走人工粘贴流程。")
        return {"text": out[: max_tokens * 4], "input_tokens": 0,
                "output_tokens": 0, "cost_cny": 0.0, "provider": "local"}


# ---------------- 付费 API（需 Key，未配置即不可用） ----------------

class _ApiProvider(Provider):
    env_key = ""
    price_in_usd_per_1k = 0.0
    price_out_usd_per_1k = 0.0

    def _key(self):
        key = os.environ.get(self.env_key, "").strip()
        if not key:
            raise NotConfigured(
                f"未配置 {self.env_key}，{self.label}不可用。"
                "任务将转人工：请把提示词贴到你的 ChatGPT/Claude 订阅里，把结果粘贴回来继续。"
            )
        return key

    def _cost_cny(self, inp, outp):
        usd = inp / 1000 * self.price_in_usd_per_1k + outp / 1000 * self.price_out_usd_per_1k
        return round(usd * CNY_PER_USD, 4)


class OpenAIProvider(_ApiProvider):
    name = "openai"
    label = "OpenAI API"
    env_key = "OPENAI_API_KEY"
    price_in_usd_per_1k = 0.0025
    price_out_usd_per_1k = 0.01

    def complete(self, prompt: str, max_tokens: int = 800) -> dict:
        key = self._key()
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        u = data.get("usage", {})
        inp, outp = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        return {"text": text, "input_tokens": inp, "output_tokens": outp,
                "cost_cny": self._cost_cny(inp, outp), "provider": "openai"}


class AnthropicProvider(_ApiProvider):
    name = "anthropic"
    label = "Anthropic API"
    env_key = "ANTHROPIC_API_KEY"
    price_in_usd_per_1k = 0.003
    price_out_usd_per_1k = 0.015

    def complete(self, prompt: str, max_tokens: int = 800) -> dict:
        key = self._key()
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-3-5-haiku-latest", "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        u = data.get("usage", {})
        inp, outp = u.get("input_tokens", 0), u.get("output_tokens", 0)
        return {"text": text, "input_tokens": inp, "output_tokens": outp,
                "cost_cny": self._cost_cny(inp, outp), "provider": "anthropic"}


class GeminiProvider(_ApiProvider):
    name = "gemini"
    label = "Gemini API"
    env_key = "GEMINI_API_KEY"
    price_in_usd_per_1k = 0.001
    price_out_usd_per_1k = 0.003

    def complete(self, prompt: str, max_tokens: int = 800) -> dict:
        key = self._key()
        r = httpx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={key}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": max_tokens}},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        u = data.get("usageMetadata", {})
        inp, outp = u.get("promptTokens", 0), u.get("candidatesTokenCount", 0)
        return {"text": text, "input_tokens": inp, "output_tokens": outp,
                "cost_cny": self._cost_cny(inp, outp), "provider": "gemini"}


_PROVIDERS = {
    "local": LocalProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def get_provider(name=None) -> Provider:
    name = (name or os.environ.get("WANAI_LLM_PROVIDER", "local")).strip().lower()
    cls = _PROVIDERS.get(name)
    if not cls:
        raise ValueError(f"unknown provider: {name}")
    return cls()


def provider_labels():
    return {k: v.label for k, v in _PROVIDERS.items()}
