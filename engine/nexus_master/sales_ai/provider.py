"""The LLM connection point for module F (section 61's "AI sales assistant"): an
interface with a rule-based default that is always on, and an LLM-backed implementation
that is BLOCKED until a real API key exists -- same on/off-by-env-var pattern as
whatsapp.CloudAPI, discovery.LeadAdsConnector and enrich.ExternalProvider. No specific
summary/suggestion quality is claimed from the LLM branch since it has never run (no
key); the rule-based branch is what every other sales_ai function actually calls today.
"""
import os


class SalesAIProvider:
    def summarize(self, interactions):
        raise NotImplementedError


class RuleBasedProvider(SalesAIProvider):
    """Default, always enabled. Extractive, not generative: counts and picks real lines
    out of the real interaction list, invents nothing."""

    enabled = True

    def summarize(self, interactions):
        if not interactions:
            return "אין עדיין היסטוריית שיחה."
        customer_lines = [i["text"] for i in interactions if i.get("role") == "customer"]
        questions = [t for t in customer_lines if "?" in t]
        last = interactions[-1]
        parts = [f"{len(interactions)} הודעות בשיחה", f"{len(customer_lines)} מהלקוח"]
        if questions:
            parts.append(f"{len(questions)} שאלות מהלקוח")
        parts.append(f"אחרונה: \"{last['text'][:80]}\" ({last.get('role', '?')})")
        return " · ".join(parts)


class LLMProvider(SalesAIProvider):
    """BLOCKED unless SALES_AI_LLM_API_KEY is set. No vendor is named or invented here --
    wiring a real API call in belongs to whichever provider the user actually contracts
    with, once there is a key to test it against."""

    ENV_VAR = "SALES_AI_LLM_API_KEY"

    def __init__(self, key=None):
        self.key = key if key is not None else os.environ.get(self.ENV_VAR)

    @property
    def enabled(self):
        return bool(self.key)

    def summarize(self, interactions):  # pragma: no cover -- requires a real key/vendor
        if not self.enabled:
            raise RuntimeError(f"LLM provider כבוי — חסר {self.ENV_VAR} (BLOCKED)")
        raise NotImplementedError("no LLM vendor wired in yet -- set a key, then implement the call")


def default_provider():
    llm = LLMProvider()
    return llm if llm.enabled else RuleBasedProvider()
