"""Structural validation for tool call responses against OpenAI-format tool schemas."""

from __future__ import annotations

import json
from typing import Any

# Maps JSON Schema type strings to Python types for top-level param checking.
_SCHEMA_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_tool_calls(
    tool_calls: list[dict[str, Any]],
    available_tools: list[dict[str, Any]],
    *,
    forced_name: str | None = None,
) -> list[str]:
    """Validates a list of tool calls against available tool definitions.

    Returns a list of error strings.  Empty list means all checks passed.

    Checks per tool call:
      1. Function name exists in available_tools (or matches *forced_name*).
      2. Arguments field parses as valid JSON.
      3. All required parameters are present.
      4. Top-level parameter types match schema declarations.
    """
    tool_index = _build_tool_index(available_tools)
    errors: list[str] = []

    for i, tc in enumerate(tool_calls):
        name = tc.get("name") or ""
        raw_args = tc.get("arguments") or ""
        prefix = f"tool_call[{i}] ({name})"

        # 1. Name check
        if forced_name and name != forced_name:
            errors.append(f"{prefix}: expected forced tool '{forced_name}', got '{name}'")
        elif name not in tool_index:
            errors.append(f"{prefix}: function name '{name}' not in available tools")
            continue  # cannot validate params without schema

        # 2. JSON parse
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, TypeError) as exc:
            errors.append(f"{prefix}: arguments are not valid JSON: {exc}")
            continue

        if not isinstance(args, dict):
            errors.append(f"{prefix}: arguments should be an object, got {type(args).__name__}")
            continue

        schema = tool_index.get(name)
        if schema is None:
            continue  # name already flagged above

        params_schema = schema.get("parameters", {})
        properties = params_schema.get("properties", {})
        required = set(params_schema.get("required", []))

        # 3. Required params
        for param_name in required:
            if param_name not in args:
                errors.append(f"{prefix}: missing required parameter '{param_name}'")

        # 4. Type checks (top-level only)
        for param_name, param_value in args.items():
            if param_name not in properties:
                continue  # extra params are tolerated
            declared_type = properties[param_name].get("type")
            if not declared_type:
                continue
            expected_types = _SCHEMA_TYPE_MAP.get(declared_type)
            if expected_types and not isinstance(param_value, expected_types):
                errors.append(
                    f"{prefix}: parameter '{param_name}' expected type "
                    f"'{declared_type}', got {type(param_value).__name__}"
                )

    return errors


def _build_tool_index(
    tools: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Builds a {name: function_schema} lookup from OpenAI-format tool defs."""
    index: dict[str, dict[str, Any]] = {}
    for tool in tools:
        func = tool.get("function", {})
        name = func.get("name")
        if name:
            index[name] = func
    return index
