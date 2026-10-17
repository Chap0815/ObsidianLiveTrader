"""Offline model-picker suggestions, not an account-availability allowlist.

Model IDs checked against official provider documentation on 2026-10-17:
https://platform.claude.com/docs/en/models/overview
https://docs.x.ai/developers/models
https://developers.openai.com/api/docs/models
https://ollama.com/library/llama3.1
https://ollama.com/library/qwen3
https://ollama.com/library/gemma3

Keep custom IDs supported and never change a configured model automatically.
Ollama examples require an already installed local model; no downloads or
authenticated discovery requests are performed by this catalogue.
"""

MODEL_CATALOG: dict[str, tuple[tuple[str, str], ...]] = {
    "claude": (
        ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
        ("claude-opus-5-5", "Claude Opus 5.5"),
        ("claude-haiku-5-5", "Claude Haiku 5.5"),
        ("claude-fable-5-1", "Claude Fable 5.1"),
    ),
    "xai": (("grok-4.7", "Grok 4.7"),),
    "openai": (
        ("gpt-6.1-sol", "GPT-6.1 Sol"),
        ("gpt-6-astra", "GPT-6 Astra"),
        ("gpt-6-sol", "GPT-6 Sol"),
        ("gpt-6-luna", "GPT-6 Luna"),
    ),
    "ollama": (
        ("llama3.1", "Llama 3.1 (default local tag)"),
        ("qwen3:8b", "Qwen 3 · 8B"),
        ("qwen3:14b", "Qwen 3 · 14B"),
        ("qwen3:32b", "Qwen 3 · 32B"),
        ("gemma3:4b", "Gemma 3 · 4B"),
        ("gemma3:12b", "Gemma 3 · 12B"),
        ("gemma3:27b", "Gemma 3 · 27B"),
    ),
}


def model_options(provider: str) -> list[dict[str, str]]:
    """Return fresh public metadata; callers cannot mutate the catalogue."""
    return [
        {"id": model_id, "label": label}
        for model_id, label in MODEL_CATALOG.get(provider, ())
    ]
