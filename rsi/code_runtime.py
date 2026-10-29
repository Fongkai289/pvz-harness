"""Restricted, argument-only Python programs for code evolution.

This is a language restriction layer, NOT a generic Python security sandbox.
Run candidates in an isolated worker with external time and memory limits: even
accepted arithmetic, recursion, or nested loops can exhaust resources. The host
must supply only the image, JSON observations/memory, and deliberately trusted
helpers. Helpers must not expose files, networking, judge state, or metrics.
No image libraries are imported here; the host owns the pixels API.

A source contains only top-level functions (and optional docstrings). It must
define perceive(image) and decide(observation, memory). Extra functions, literal
scalar defaults, inert type annotations, and bounded for/comprehension iteration
are supported. Module state, imports, classes, while, decorators, dynamic calls,
and attribute access other than checked data-method calls are forbidden.
"""

from __future__ import annotations

import ast
import json
import keyword
import math
from collections.abc import Callable, Iterable, Mapping
from types import CodeType
from typing import Any


MAX_SOURCE_BYTES = 64 * 1024
MAX_AST_NODES = 10_000
MAX_JSON_BYTES = 64 * 1024
MAX_MEMORY_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_ITERATIONS = 100_000


class CandidateValidationError(ValueError):
    """The candidate source is outside the accepted Python subset."""


class CandidateOutputError(ValueError):
    """A candidate input, result, or memory violates the JSON contract."""


def _bounded_range(*args: int) -> range:
    result = range(*args)
    try:
        if len(result) <= MAX_ITERATIONS:
            return result
    except OverflowError:
        pass
    raise ValueError("range exceeds the iteration limit")


def _bounded_iter(values: Iterable[Any]) -> Iterable[Any]:
    for index, value in enumerate(values):
        if index >= MAX_ITERATIONS:
            raise ValueError("loop exceeds the iteration limit")
        yield value


_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min, "range": _bounded_range,
    "reversed": reversed, "round": round, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip,
}
_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "print", "breakpoint",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "hasattr", "type", "object", "super", "help", "memoryview",
    "classmethod", "staticmethod", "property", "exit", "quit",
})
_METHODS = {
    dict: frozenset({"get", "items", "values", "keys", "pop", "copy",
                     "setdefault", "update", "clear", "popitem"}),
    list: frozenset({"append", "extend", "pop", "copy", "sort", "count",
                     "index", "insert", "remove", "clear", "reverse"}),
    tuple: frozenset({"count", "index"}),
    str: frozenset({"count", "index", "find", "rfind", "startswith",
                    "endswith", "split", "rsplit", "strip", "lstrip",
                    "rstrip", "lower", "upper", "replace", "join"}),
}
_METHOD_NAMES = frozenset().union(*_METHODS.values())


def _call_method(receiver: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    # Exact types avoid dispatching an ostensibly safe name on an image, helper
    # object, or a subclass with a privileged/malicious method implementation.
    if method not in _METHODS.get(type(receiver), ()):
        raise CandidateValidationError("data method requires a plain data receiver")
    return getattr(receiver, method)(*args, **kwargs)


def _check_name(name: str) -> None:
    if (not isinstance(name, str) or not name.isidentifier()
            or keyword.iskeyword(name) or name.startswith("_")
            or name in _FORBIDDEN_NAMES):
        raise CandidateValidationError(f"forbidden name: {name!r}")


def _check_json(value: Any, label: str, max_bytes: int = MAX_JSON_BYTES) -> None:
    """Check exact JSON types, cycles, depth, finite numbers, and encoded size."""
    remaining = MAX_JSON_NODES
    active: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise CandidateOutputError(f"{label} exceeds JSON complexity limits")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is int:
            # Avoid Python's integer-to-string ceiling and enormous conversions.
            if item.bit_length() > 4096:
                raise CandidateOutputError(f"{label} contains an oversized integer")
            return
        if kind is float:
            if not math.isfinite(item):
                raise CandidateOutputError(f"{label} contains a non-finite number")
            return
        if kind is str:
            if len(item) > max_bytes:
                raise CandidateOutputError(f"{label} exceeds {max_bytes} bytes")
            return
        if kind not in (list, dict):
            raise CandidateOutputError(f"{label} must contain only JSON values")
        identity = id(item)
        if identity in active:
            raise CandidateOutputError(f"{label} contains a cycle")
        if len(item) > MAX_JSON_NODES:
            raise CandidateOutputError(f"{label} exceeds JSON complexity limits")
        active.add(identity)
        try:
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise CandidateOutputError(f"{label} requires string keys")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)
    try:
        # Stream encoding so many individually small strings cannot cause a
        # large temporary JSON allocation before the size check.
        size = 0
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                   separators=(",", ":"))
        for chunk in encoder.iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > max_bytes:
                raise CandidateOutputError(f"{label} exceeds {max_bytes} bytes")
    except (UnicodeError, OverflowError) as exc:
        raise CandidateOutputError(f"{label} cannot be encoded as JSON") from exc


_ALLOWED_NODES = frozenset({
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AugAssign, ast.Expr, ast.If, ast.For, ast.Break, ast.Continue, ast.Pass,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.Subscript, ast.Slice, ast.Call, ast.keyword, ast.Attribute,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.ListComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
    ast.UAdd, ast.USub, ast.Not, ast.Invert, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
})


def _check_annotation(node: ast.AST | None) -> None:
    if node is None:
        return
    allowed_types = {"int", "float", "bool", "str", "list", "dict", "tuple"}
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in allowed_types:
            continue
        if isinstance(child, ast.Constant) and child.value is None:
            continue
        if type(child) in (ast.Subscript, ast.Tuple, ast.Load, ast.BinOp, ast.BitOr):
            continue
        raise CandidateValidationError("only inert JSON type annotations are allowed")


class _Validator(ast.NodeVisitor):
    def __init__(self, functions: set[str], helpers: set[str]):
        self.call_names = functions | helpers | _SAFE_BUILTINS.keys()
        self.locals: set[str] = set()

    def generic_visit(self, node: ast.AST) -> None:
        if type(node) not in _ALLOWED_NODES:
            raise CandidateValidationError(f"forbidden syntax: {type(node).__name__}")
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        raise CandidateValidationError("nested functions are forbidden")

    def visit_Name(self, node: ast.Name) -> None:
        _check_name(node.id)
        if isinstance(node.ctx, ast.Store) and node.id in self.call_names:
            raise CandidateValidationError(f"cannot rebind callable: {node.id}")
        if isinstance(node.ctx, ast.Load) and node.id not in self.locals | self.call_names:
            raise CandidateValidationError(f"unknown name: {node.id}")

    def visit_Constant(self, node: ast.Constant) -> None:
        if type(node.value) not in (type(None), bool, int, float, str):
            raise CandidateValidationError("only JSON scalar literals are allowed")
        if type(node.value) is float and not math.isfinite(node.value):
            raise CandidateValidationError("non-finite literals are forbidden")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        raise CandidateValidationError("attributes are only allowed as data-method calls")

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            _check_name(node.func.id)
            if node.func.id not in self.call_names:
                raise CandidateValidationError(f"unknown callable: {node.func.id}")
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr not in _METHOD_NAMES:
                raise CandidateValidationError(f"forbidden method: {node.func.attr}")
            self.visit(node.func.value)
        else:
            raise CandidateValidationError("indirect calls are forbidden")
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            if kw.arg is None:
                raise CandidateValidationError("keyword unpacking is forbidden")
            _check_name(kw.arg)
            self.visit(kw.value)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        if node.is_async:
            raise CandidateValidationError("async iteration is forbidden")
        self.generic_visit(node)


class _Instrument(ast.NodeTransformer):
    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute):
            return ast.copy_location(ast.Call(
                func=ast.Name(id="_call_method", ctx=ast.Load()),
                args=[node.func.value, ast.Constant(node.func.attr), *node.args],
                keywords=node.keywords,
            ), node)
        return node

    def _wrap_iter(self, node: Any) -> ast.AST:
        self.generic_visit(node)
        node.iter = ast.copy_location(ast.Call(
            func=ast.Name(id="_bounded_iter", ctx=ast.Load()),
            args=[node.iter], keywords=[],
        ), node.iter)
        return node

    visit_For = _wrap_iter
    visit_comprehension = _wrap_iter


def validate_source(source: str, helper_names: Iterable[str] = ()) -> CodeType:
    """Return validated, instrumented code; raise CandidateValidationError on failure.

    The result is for CandidateProgram's restricted namespace, not bare exec().
    Helper names authorize calls only; the constructor supplies their functions.
    """
    if not isinstance(source, str):
        raise CandidateValidationError("source must be a string")
    try:
        if len(source) > MAX_SOURCE_BYTES or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise CandidateValidationError("source exceeds byte limit")
        tree = ast.parse(source, filename="<candidate>", mode="exec")
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as exc:
        raise CandidateValidationError(f"invalid source: {exc}") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise CandidateValidationError("source exceeds AST node limit")
    helpers = set(helper_names)
    for name in helpers:
        _check_name(name)
        if name in _SAFE_BUILTINS or name in {"perceive", "decide"}:
            raise CandidateValidationError(f"reserved helper name: {name}")
    functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str:
            continue
        if not isinstance(node, ast.FunctionDef):
            raise CandidateValidationError("module must contain only functions and docstrings")
        _check_name(node.name)
        if node.name in functions or node.name in helpers or node.name in _SAFE_BUILTINS:
            raise CandidateValidationError(f"duplicate or reserved function: {node.name}")
        functions[node.name] = node
    if not {"perceive", "decide"} <= functions.keys():
        raise CandidateValidationError("source must define perceive and decide")
    validator = _Validator(set(functions), helpers)
    try:
        for name, node in functions.items():
            if node.decorator_list or getattr(node, "type_params", ()):
                raise CandidateValidationError("decorators and type parameters are forbidden")
            args = node.args
            positional = args.posonlyargs + args.args
            if args.vararg or args.kwarg or args.kwonlyargs:
                raise CandidateValidationError("functions require fixed positional parameters")
            required = {"perceive": 1, "decide": 2}.get(name)
            if required is not None and (len(positional) != required or args.defaults):
                raise CandidateValidationError(f"{name} requires exactly {required} parameters")
            for arg in positional:
                _check_name(arg.arg)
                if arg.arg in validator.call_names:
                    raise CandidateValidationError(f"parameter shadows callable: {arg.arg}")
                _check_annotation(arg.annotation)
                arg.annotation = None
            _check_annotation(node.returns)
            node.returns = None
            for default in args.defaults:
                try:
                    value = ast.literal_eval(default)
                except (ValueError, TypeError, SyntaxError) as exc:
                    raise CandidateValidationError("defaults must be literal scalars") from exc
                if type(value) not in (type(None), bool, int, float, str):
                    raise CandidateValidationError("defaults must be literal scalars")
                validator.visit(default)
            validator.locals = {arg.arg for arg in positional} | {
                child.id for statement in node.body for child in ast.walk(statement)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
            }
            for statement in node.body:
                validator.visit(statement)
        tree = _Instrument().visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, "<candidate>", "exec", dont_inherit=True)
    except (SyntaxError, RecursionError) as exc:
        raise CandidateValidationError(f"invalid candidate structure: {exc}") from exc


class CandidateProgram:
    """Compile once, then call perception/decisions with explicit episode memory.

    Memory is mutated in place and checked before and after each decision, even
    on failure. A failed call may leave mutations behind; the worker should
    discard a failed episode. Helpers are trusted capabilities supplied by the
    host, not implemented or discovered by candidate code.
    """

    def __init__(self, source: str, helpers: Mapping[str, Callable[..., Any]] | None = None):
        supplied = dict(helpers) if helpers is not None else {}
        code = validate_source(source, supplied)
        if any(not callable(helper) for helper in supplied.values()):
            raise CandidateValidationError("helpers must be callable")
        namespace = {
            "__builtins__": dict(_SAFE_BUILTINS),
            "_call_method": _call_method,
            "_bounded_iter": _bounded_iter,
            **supplied,
        }
        exec(code, namespace, namespace)
        self._perceive = namespace["perceive"]
        self._decide = namespace["decide"]

    def perceive(self, image: Any) -> dict:
        observation = self._perceive(image)
        if type(observation) is not dict:
            raise CandidateOutputError("perceive must return a dict")
        _check_json(observation, "observation")
        return observation

    def decide(self, observation: dict, memory: dict) -> dict | None:
        if type(observation) is not dict or type(memory) is not dict:
            raise CandidateOutputError("observation and memory must be plain dicts")
        _check_json(observation, "observation")
        _check_json(memory, "memory", MAX_MEMORY_BYTES)
        try:
            action = self._decide(observation, memory)
        finally:
            _check_json(memory, "memory", MAX_MEMORY_BYTES)
        if action is not None and type(action) is not dict:
            raise CandidateOutputError("decide must return a dict or None")
        _check_json(action, "action")
        return action
