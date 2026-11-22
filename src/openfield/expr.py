"""Small, safe expressions and templates for service configs.

Expressions (memory_gb, estimate_s, validate rules, derived params, `when` conditions) are Python
syntax restricted to arithmetic, comparisons, boolean logic, `x if c else y`, literals and a few
whitelisted functions. Names resolve to the job's parameters plus context values such as `work`.

Templates replace `{name}` with a value; `{{` and `}}` give literal braces. A template made of a single
placeholder keeps the value's type (useful for JSON bodies).
"""
from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Mapping


class ExprError(ValueError):
    pass


def snap_up(value: float, start: int, step: int, maximum: int | None = None) -> int | None:
    """The first value of the grid start, start+step, ... that is >= value (None above maximum)."""
    if value <= start:
        return start
    k = math.ceil((value - start) / step)
    snapped = start + k * step
    return None if maximum is not None and snapped > maximum else snapped


FUNCTIONS = {
    "min": min, "max": max, "abs": abs, "round": round, "int": int, "float": float, "bool": bool,
    "str": str, "len": len, "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt,
    "snap_up": snap_up,
}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}
_cache: dict[str, ast.Expression] = {}


def _compile(src: str) -> ast.Expression:
    tree = _cache.get(src)
    if tree is None:
        try:
            tree = ast.parse(src, mode="eval")
        except SyntaxError as e:
            raise ExprError(f"invalid expression {src!r}: {e.msg}") from None
        _cache[src] = tree
    return tree


def evaluate(src: Any, names: Mapping[str, Any]) -> Any:
    """Evaluate an expression string; numbers and booleans pass through unchanged."""
    if not isinstance(src, str):
        return src
    return _eval(_compile(src).body, names, src)


def _eval(node: ast.AST, names: Mapping[str, Any], src: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        if node.id in ("None", "True", "False"):
            return {"None": None, "True": True, "False": False}[node.id]
        raise ExprError(f"unknown name {node.id!r} in {src!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_eval(node.left, names, src), _eval(node.right, names, src))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand, names, src))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval(v, names, src)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = _eval(v, names, src)
            if result:
                return result
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names, src)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, names, src)
            if type(op) not in _CMP or not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, names, src) else node.orelse, names, src)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, names, src) for e in node.elts]
    if isinstance(node, ast.Subscript):
        container, key = _eval(node.value, names, src), _eval(node.slice, names, src)
        if isinstance(container, Mapping):
            return container.get(key)
        if isinstance(container, (list, tuple, str)) and isinstance(key, int):
            return container[key] if -len(container) <= key < len(container) else None
        raise ExprError(f"cannot index {type(container).__name__} in {src!r}")
    if isinstance(node, ast.Attribute) and not node.attr.startswith("_"):
        container = _eval(node.value, names, src)
        if isinstance(container, Mapping):
            return container.get(node.attr)
        raise ExprError(f"cannot read .{node.attr} of {type(container).__name__} in {src!r}")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
        return FUNCTIONS[node.func.id](*[_eval(a, names, src) for a in node.args])
    raise ExprError(f"unsupported syntax in {src!r}: {ast.dump(node)[:80]}")


_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([A-Za-z_][\w.]*)\}")


def render(template: Any, names: Mapping[str, Any]) -> Any:
    """Fill {name} placeholders. A lone placeholder returns the raw value; dicts and lists recurse."""
    if isinstance(template, dict):
        return {k: render(v, names) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, names) for v in template]
    if not isinstance(template, str):
        return template
    whole = re.fullmatch(r"\{([A-Za-z_][\w.]*)\}", template)
    if whole:
        return _lookup(whole.group(1), names, template)

    def sub(m: re.Match) -> str:
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        value = _lookup(m.group(1), names, template)
        return _fmt(value)

    return _PLACEHOLDER.sub(sub, template)


def render_str(template: Any, names: Mapping[str, Any]) -> str:
    """render() for command arguments and paths: always a string, never an empty placeholder."""
    value = render(template, names)
    return value if isinstance(value, str) else _fmt(value)


def _lookup(name: str, names: Mapping[str, Any], template: str) -> Any:
    value: Any = names
    for part in name.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            raise ExprError(f"unknown placeholder {{{name}}} in {template!r}")
    return value


def _fmt(value: Any) -> str:
    if value is None:
        raise ExprError("a placeholder has no value (guard the argument with `when`)")
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
