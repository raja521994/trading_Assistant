"""Optional OpenAI-compatible chat-completions connector (any provider exposing /chat/completions).

The model receives ONLY the deterministic facts and is instructed to interpret them. Output containing
numbers that are not present in the facts is rejected (falls back to the template interpretation)."""

from __future__ import annotations

import logging

import httpx

from .provider import Explanation, LLMProvider, unsupported_numbers

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a cautious equity research assistant for Indian markets. You will receive FACTS, CALCULATIONS and "
    "ASSUMPTIONS produced by deterministic code. Answer the user's question using only that material. Do not "
    "introduce any new number, price, indicator value, probability, news item or fundamental. Do not recommend "
    "placing real orders. If the material does not answer the question, say so. Reply in at most 6 short sentences."
)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout

    def interpret(self, question: str, base: Explanation) -> Explanation:
        material = (
            "FACTS:\n- " + "\n- ".join(base.facts) + "\n\nCALCULATIONS:\n- " + "\n- ".join(base.calculations)
            + "\n\nASSUMPTIONS:\n- " + "\n- ".join(base.assumptions)
            + "\n\nDETERMINISTIC INTERPRETATION:\n- " + "\n- ".join(base.interpretation)
        )
        out = Explanation(base.facts, base.calculations, base.assumptions, list(base.interpretation), "template",
                          list(base.warnings), list(base.sources))
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "temperature": 0.1,
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": f"{material}\n\nQUESTION: {question}"}]},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM call failed (%s); using deterministic interpretation", type(exc).__name__)
            out.warnings.append(f"LLM unavailable ({type(exc).__name__}); showing deterministic interpretation.")
            return out
        bad = unsupported_numbers(text, material + " " + question)
        if bad:
            out.warnings.append(f"LLM output rejected: it contained numbers not present in the computed facts {bad[:5]}.")
            return out
        out.interpretation = [f"[LLM interpretation - {self.model}] {text}"] + base.interpretation
        out.provider = self.name
        return out
