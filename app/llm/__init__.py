"""LLM client for advisory trade proposals (Claude default).

Never bypasses risk gates — proposals are suggestions only.
"""

from app.llm.client import analyze_with_grok, analyze_with_llm, parse_proposal

__all__ = ["analyze_with_grok", "analyze_with_llm", "parse_proposal"]
