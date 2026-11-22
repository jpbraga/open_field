"""Validate a job's parameters against its service's declared schema."""
from __future__ import annotations

import json
import secrets
from typing import Any

from .config import ParamSpec, ServiceSpec
from .expr import ExprError, evaluate


class ParamError(ValueError):
    pass


def _num(p: ParamSpec, name: str, v: float) -> None:
    if p.min is not None and v < p.min:
        raise ParamError(f"{name} must be >= {p.min:g}")
    if p.max is not None and v > p.max:
        raise ParamError(f"{name} must be <= {p.max:g}")


def coerce(p: ParamSpec, name: str, v: Any) -> Any:
    if p.type == "string":
        if not isinstance(v, str):
            raise ParamError(f"{name} must be a string")
        v = v.strip()
        if p.max_length and len(v) > p.max_length:
            raise ParamError(f"{name} is longer than {p.max_length} characters")
        if p.required and not v:
            raise ParamError(f"{name} must not be empty")
        return v
    if p.type == "int":
        if isinstance(v, bool):
            raise ParamError(f"{name} must be an integer")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ParamError(f"{name} must be an integer") from None
        if f != int(f):
            raise ParamError(f"{name} must be an integer")
        v = int(f)
        _num(p, name, v)
        if p.multiple_of and v % p.multiple_of:
            raise ParamError(f"{name} must be a multiple of {p.multiple_of}")
        return v
    if p.type == "float":
        if isinstance(v, bool):
            raise ParamError(f"{name} must be a number")
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ParamError(f"{name} must be a number") from None
        _num(p, name, v)
        return v
    if p.type == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "yes", "no"):
            return v.lower() in ("true", "1", "yes")
        raise ParamError(f"{name} must be true or false")
    if p.type == "json":
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                raise ParamError(f"{name} must be JSON") from None
        if not isinstance(v, (list, dict)):
            raise ParamError(f"{name} must be a JSON list or object")
        if p.max_length and len(v) > p.max_length:
            raise ParamError(f"{name} has more than {p.max_length} items")
        return v
    if p.type == "enum":
        values = p.values or []
        for allowed in values:
            if v == allowed or str(v) == str(allowed):
                return allowed
        raise ParamError(f"{name} must be one of {values}")
    raise ParamError(f"{name}: unsupported type {p.type}")


def resolve(spec: ServiceSpec, raw: dict[str, Any], upload_counts: dict[str, int]) -> dict[str, Any]:
    """Coerce and check the parameters, fill defaults, apply `derive` and the `validate` rules.
    File parameters are only counted here (they are saved and checked by the job manager)."""
    if not isinstance(raw, dict):
        raise ParamError("params must be an object")
    known = set(spec.params)
    unknown = (set(raw) | set(upload_counts)) - known
    if unknown:
        raise ParamError(f"unknown parameter(s): {', '.join(sorted(unknown))}; "
                         f"this service takes: {', '.join(sorted(known))}")
    values: dict[str, Any] = {}
    for name, p in spec.params.items():
        if p.type in ("file", "files"):
            n = upload_counts.get(name, 0)
            if name in raw:
                raise ParamError(f"{name} is a file: send it as a multipart field named {name!r}")
            if p.required and not n:
                raise ParamError(f"{name} is required (multipart file field {name!r})")
            limit = 1 if p.type == "file" else p.max_files
            if n > limit:
                raise ParamError(f"{name} takes at most {limit} file(s)")
            # A truthy stand-in, so `derive` / `validate` can test presence; the saved paths replace it.
            values[name] = None if not n else (["<upload>"] * n if p.type == "files" else "<upload>")
            continue
        v = raw.get(name)
        if v is None:
            v = secrets.randbelow(2**31) if (p.default == "random" and p.type == "int") else p.default
        if v is None:
            if p.required:
                raise ParamError(f"{name} is required")
            values[name] = None
            continue
        values[name] = coerce(p, name, v)
    try:
        for name, expression in spec.derive.items():
            v = evaluate(expression, values)
            if v is not None and name in spec.params and spec.params[name].type not in ("file", "files"):
                v = coerce(spec.params[name], name, v)
            values[name] = v
        for rule in spec.validate_rules:
            if not evaluate(rule.expr, values):
                raise ParamError(rule.message)
    except ExprError as e:
        raise ParamError(f"service config error: {e}") from None
    except (TypeError, ZeroDivisionError) as e:
        raise ParamError(f"cannot check the parameters: {e}") from None
    return values


def schema(spec: ServiceSpec) -> dict[str, Any]:
    """The parameter schema, for clients (web forms, agents)."""
    out = {}
    for name, p in spec.params.items():
        item = {"type": p.type, "required": p.required, "description": p.description}
        for key in ("default", "min", "max", "multiple_of", "max_length", "values"):
            value = getattr(p, key)
            if value is not None:
                item[key] = value
        if p.type in ("file", "files"):
            item.update(accept=p.accept, max_files=1 if p.type == "file" else p.max_files, max_mb=p.max_mb)
        out[name] = item
    return out
