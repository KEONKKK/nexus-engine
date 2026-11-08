"""Section 64: conversation summaries, via the pluggable provider (provider.py) --
RuleBasedProvider by default, LLMProvider once a key exists. `interactions` is a plain
list of {"role": "customer"|"business", "text": ..., "ts": ...} dicts supplied by the
caller: this module does not read any CRM table itself, since the interactions log
(section 54) belongs to module E (owned elsewhere) and may not exist yet -- see
design/nigun/STATUS.md row 64's note."""
from .provider import default_provider


def summarize_conversation(interactions, provider=None):
    provider = provider or default_provider()
    return provider.summarize(interactions or [])
