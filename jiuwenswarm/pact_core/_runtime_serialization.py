"""Strict local dataclass receipts for Runtime-only continuation facades."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T")


def encode(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    return value


def decode(cls: type[T], value: object) -> T:
    if not is_dataclass(cls) or not isinstance(value, dict):
        raise ValueError("receipt must be a dataclass object")
    declared = fields(cls)
    if set(value) != {field.name for field in declared}:
        raise ValueError("receipt fields do not match the current facade")
    hints = get_type_hints(cls)
    args: dict[str, Any] = {}
    for field in declared:
        if not field.init:
            if value[field.name] != field.default:
                raise ValueError("receipt schema does not match the current facade")
            continue
        args[field.name] = _decode_value(hints[field.name], value[field.name])
    return cls(**args)


def _decode_value(expected: Any, value: object) -> Any:
    if expected is object:
        return value
    origin, arguments = get_origin(expected), get_args(expected)
    if origin in (Union, UnionType):
        for item in arguments:
            try:
                return _decode_value(item, value)
            except ValueError:
                pass
        raise ValueError("receipt union value is invalid")
    if origin is Literal:
        if any(type(value) is type(item) and value == item for item in arguments):
            return value
        raise ValueError("receipt enum value is invalid")
    if expected is Path:
        if not isinstance(value, str):
            raise ValueError("receipt path must be a string")
        return Path(value)
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("receipt tuple must be an array")
        return tuple(_decode_value(arguments[0], item) for item in value)
    if origin is list:
        if not isinstance(value, list):
            raise ValueError("receipt list must be an array")
        return [_decode_value(arguments[0], item) for item in value]
    if origin is dict:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError("receipt mapping must have string keys")
        return {key: _decode_value(arguments[1], item) for key, item in value.items()}
    if isinstance(expected, type) and is_dataclass(expected):
        return decode(expected, value)
    if type(value) is expected:
        return value
    raise ValueError("receipt value has the wrong type")
