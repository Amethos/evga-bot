"""One place that talks to Claude, so model settings and safety fallbacks stay consistent."""

import anthropic

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def parse(model: str, effort: str, system: str, user: str, schema):
    """Ask for a structured answer that is validated against a Pydantic model."""
    response = client().beta.messages.parse(
        model=model,
        max_tokens=16000,
        # The system prompt holds the instructions and your rules; caching it makes
        # every ticket after the first cheaper and faster.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"No usable answer from {model} (stop_reason={response.stop_reason})")
    return response.parsed_output
