"""Back-compat re-exports — implementation lives in app.llm.client."""

from app.llm.client import (  # noqa: F401
    GrokError,
    LlmError,
    analyze_with_grok,
    analyze_with_llm,
    annotate_proposal,
    build_llm_context,
    compact_daily_for_llm,
    compact_tf_for_llm,
    compute_simple_rrr,
    parse_proposal,
    strip_markdown_fences,
)

__all__ = [
    "GrokError",
    "LlmError",
    "analyze_with_grok",
    "analyze_with_llm",
    "annotate_proposal",
    "build_llm_context",
    "compact_daily_for_llm",
    "compact_tf_for_llm",
    "compute_simple_rrr",
    "parse_proposal",
    "strip_markdown_fences",
]
