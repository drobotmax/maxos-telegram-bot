"""Model router: select Claude model based on request kind."""

from .config import CLAUDE_MODEL_DEEP, CLAUDE_MODEL_FAST


def route(kind: str) -> str:
    """Return model ID for the given request kind.

    scheduled → fast (Haiku): predictable structured output, ~10x cheaper
    interactive → deep (Opus): open-ended, complex, user-facing quality
    """
    if kind == "scheduled":
        return CLAUDE_MODEL_FAST
    return CLAUDE_MODEL_DEEP
