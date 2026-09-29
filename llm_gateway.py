from __future__ import annotations


class LLMGateway:
    """Minimal public shim for the private LLM gateway used in production.

    Replace this class with your own provider adapter. It only needs to expose a
    `chat(messages, timeout=...) -> str` method returning JSON text for the
    personalization jobs.
    """

    def __init__(self, profile: str = "local") -> None:
        self.profile = profile

    def chat(self, messages: list[dict], timeout: int = 45) -> str:
        raise RuntimeError(
            "No public LLM provider is configured. Install or implement an "
            "LLMGateway adapter before running generation jobs."
        )
