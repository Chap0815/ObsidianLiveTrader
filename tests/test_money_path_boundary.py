"""Static contract for the sole exchange-mutation path."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    Path("app/hyperliquid/client.py"),
    Path("app/mexc/client.py"),
}
ALLOWED_CALLERS = ADAPTERS | {Path("app/orders/service.py")}
MUTATION_PREFIXES = (
    "cancel_",
    "close_",
    "modify_",
    "place_",
    "set_",
    "transfer_",
    "update_",
    "withdraw_",
)
REQUIRED_MUTATIONS = {
    "cancel_order",
    "close_position_market",
    "place_order",
    "set_leverage",
}


def _tree(relative: Path) -> ast.AST:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=str(relative))


def _adapter_mutation_methods() -> set[str]:
    methods: set[str] = set()
    for relative in ADAPTERS:
        for node in ast.walk(_tree(relative)):
            if (
                isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and not node.name.startswith("_")
                and node.name.startswith(MUTATION_PREFIXES)
            ):
                methods.add(node.name)
    return methods


def test_exchange_mutations_are_called_only_by_order_service_or_adapter_internals():
    mutation_methods = _adapter_mutation_methods()
    assert REQUIRED_MUTATIONS <= mutation_methods

    violations: list[str] = []
    sources = sorted((ROOT / "app").rglob("*.py")) + sorted(
        (ROOT / "scripts").glob("*.py")
    )
    for source in sources:
        relative = source.relative_to(ROOT)
        if relative in ALLOWED_CALLERS:
            continue
        for node in ast.walk(_tree(relative)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in mutation_methods
            ):
                violations.append(f"{relative}:{node.lineno} calls {node.func.attr}")

    assert violations == [], (
        "Exchange mutations must flow through app/orders/service.py: "
        + "; ".join(violations)
    )
