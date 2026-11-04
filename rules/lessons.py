"""Fixed condition language for coach lessons; the coach writes conditions, never the evaluator.

A condition is one Python-like expression over the player's current observation, e.g.
    has_zombie("pole vaulting") and sun >= 50
Only the names below, literals, and/or/not, comparisons and + - * / are accepted. Nothing is
executed with eval(): a small tree walker computes the value. A comparison with an unknown
(None) side is false, so a lesson never shows on a guess.
"""
from __future__ import annotations

import ast

MAX_CONDITION = 240
FACTS = {
    "sun": "sun available now (observed sun minus commands issued since), or unknown",
    "zombies": "number of zombies seen on the lawn",
    "rows": "number of lawn rows",
}
FUNCTIONS = {
    "has_zombie": "has_zombie(type): a zombie of this type is seen",
    "zombie_count": "zombie_count(type): how many zombies of this type are seen",
    "zombies_in_row": "zombies_in_row(row): zombies seen in this row (0 = top)",
    "nearest_col": "nearest_col(row): column of the zombie closest to the house in this row, or unknown",
    "plant_count": "plant_count(plant): how many of this plant are on the lawn",
    "attackers_in_row": "attackers_in_row(row): attacking plants in this row",
    "has_card": "has_card(plant): this card is in the seed bank",
    "card_ready": "card_ready(plant): this card is bright (affordable and recharged) now",
}
_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
          ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
          ast.Div, ast.Constant, ast.Name, ast.Load, ast.Call)


def vocabulary() -> str:
    return "; ".join([f"{k}: {v}" for k, v in FACTS.items()] + list(FUNCTIONS.values()))


def parse_condition(text: str) -> ast.Expression:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CONDITION:
        raise ValueError(f"a lesson condition is 1-{MAX_CONDITION} characters")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"lesson condition is not an expression: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            raise ValueError(f"lesson conditions cannot use {type(node).__name__}")
        if isinstance(node, ast.Constant) and type(node.value) not in (int, float, str, bool, type(None)):
            raise ValueError("lesson constants are numbers, text, true/false or None")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords or len(node.args) != 1:
                raise ValueError(f"lesson functions take one argument: {', '.join(FUNCTIONS)}")
        elif isinstance(node, ast.Name) and node.id not in FACTS and node.id not in FUNCTIONS:
            raise ValueError(f"unknown name in lesson condition: {node.id}")
    return tree


def evaluate(text: str, facts: dict, functions: dict) -> bool:
    """facts: FACTS values; functions: FUNCTIONS implementations taking one JSON value."""
    return bool(_value(parse_condition(text).body, facts, functions))


def _value(node, facts, functions):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in facts:
            raise ValueError(f"{node.id} is a function; call it with one argument")
        return facts[node.id]
    if isinstance(node, ast.Call):
        return functions[node.func.id](_value(node.args[0], facts, functions))
    if isinstance(node, ast.BoolOp):
        result = None
        for value in node.values:
            result = _value(value, facts, functions)
            if isinstance(node.op, ast.And) and not result or isinstance(node.op, ast.Or) and result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        value = _value(node.operand, facts, functions)
        if isinstance(node.op, ast.Not):
            return not value
        return None if value is None else -value
    if isinstance(node, ast.BinOp):
        left, right = _value(node.left, facts, functions), _value(node.right, facts, functions)
        if left is None or right is None or isinstance(node.op, ast.Div) and right == 0:
            return None
        return {ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
                ast.Mult: lambda: left * right, ast.Div: lambda: left / right}[type(node.op)]()
    left = _value(node.left, facts, functions)
    for op, comparator in zip(node.ops, node.comparators):
        right = _value(comparator, facts, functions)
        if left is None or right is None:
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                return False
        ok = {ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right, ast.Lt: lambda: left < right,
              ast.LtE: lambda: left <= right, ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right}[type(op)]()
        if not ok:
            return False
        left = right
    return True
