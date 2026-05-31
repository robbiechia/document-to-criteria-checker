"""Schema interpreter — maps RuleSet field_required names to user-provided schema fields.

Accepts schema in two ways:
  1. Free text (typed by user): "age: int, citizenship: str (SC/SPR), monthly_income: float"
  2. Schema file (uploaded): parsed structurally before LLM mapping

Supported file formats for structural parsing:
  .json   — JSON Schema (extracts properties + types)
  .py     — Python TypedDict / dataclass / Pydantic model (AST-parsed field annotations)
  .sql    — CREATE TABLE DDL (regex-extracted column names + types)
  .yaml / .yml — flat key: type mapping or OpenAPI-style schema
  .ts     — TypeScript interface (regex-extracted field names)

For large schemas (>50 fields), a warning is returned alongside the parsed text.
The LLM mapping step receives the parsed text in the same way as free text.

Output: a dict mapping extracted field names to user schema names.
  {"buyer_age": "age", "buyer_citizenship": "citizenship", ...}
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Optional

import app.config as _cfg
from app.utils.llm_client import complete

LARGE_SCHEMA_FIELD_LIMIT = 50  # warn if schema has more fields than this


# ---------------------------------------------------------------------------
# Schema file parsers — convert file bytes to structured text
# ---------------------------------------------------------------------------

def _parse_json_schema(content: str) -> tuple[str, str]:
    """Extract fields from a JSON Schema file.
    Returns (structured_text, warning).
    """
    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        return content, f"Could not parse JSON Schema: {e}"

    # Handle both root-level properties and nested definitions
    properties = schema.get("properties") or {}
    # Also look inside $defs / definitions
    for defs in (schema.get("$defs") or schema.get("definitions") or {}).values():
        properties.update(defs.get("properties") or {})

    if not properties:
        return content, ""

    lines = []
    for name, prop in properties.items():
        ptype = prop.get("type") or prop.get("$ref", "").split("/")[-1] or "any"
        desc = prop.get("description", "")
        enum_vals = prop.get("enum")
        line = f"{name}: {ptype}"
        if enum_vals:
            line += f" (one of: {', '.join(str(v) for v in enum_vals)})"
        if desc:
            line += f" — {desc}"
        lines.append(line)

    warn = (
        f"Schema has {len(lines)} fields — only the most relevant will be mapped."
        if len(lines) > LARGE_SCHEMA_FIELD_LIMIT else ""
    )
    return "\n".join(lines), warn


def _parse_python_schema(content: str) -> tuple[str, str]:
    """Extract fields from a Python TypedDict, dataclass, or Pydantic model.
    Returns (structured_text, warning).
    """
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return content, f"Could not parse Python schema: {e}"

    lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef,)):
            for item in node.body:
                # TypedDict / dataclass field: name: type  or  name: type = default
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_name = item.target.id
                    try:
                        type_str = ast.unparse(item.annotation)
                    except Exception:
                        type_str = "any"
                    lines.append(f"{field_name}: {type_str}")

    if not lines:
        # Fallback: extract variable annotations at module level
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                try:
                    type_str = ast.unparse(node.annotation)
                except Exception:
                    type_str = "any"
                lines.append(f"{node.target.id}: {type_str}")

    warn = (
        f"Schema has {len(lines)} fields — only the most relevant will be mapped."
        if len(lines) > LARGE_SCHEMA_FIELD_LIMIT else ""
    )
    return "\n".join(lines) if lines else content, warn


def _parse_sql_schema(content: str) -> tuple[str, str]:
    """Extract columns from a CREATE TABLE SQL statement.
    Returns (structured_text, warning).
    """
    # Match column definitions: column_name  TYPE [constraints]
    col_pattern = re.compile(
        r"^\s*[`\"']?(\w+)[`\"']?\s+(\w+(?:\([^)]*\))?)",
        re.MULTILINE | re.IGNORECASE,
    )
    # Skip keywords that appear at column position
    _SKIP = {
        "primary", "unique", "index", "key", "constraint", "foreign", "check",
        "create", "table", "if", "not", "exists",
    }
    lines = []
    for m in col_pattern.finditer(content):
        name, col_type = m.group(1), m.group(2)
        if name.lower() not in _SKIP:
            lines.append(f"{name}: {col_type}")

    warn = (
        f"DDL has {len(lines)} columns — only the most relevant will be mapped."
        if len(lines) > LARGE_SCHEMA_FIELD_LIMIT else ""
    )
    return "\n".join(lines) if lines else content, warn


def _parse_yaml_schema(content: str) -> tuple[str, str]:
    """Extract fields from a YAML schema (flat key: type or OpenAPI-style).
    Returns (structured_text, warning).
    """
    try:
        import yaml  # optional dependency
        data = yaml.safe_load(content)
    except Exception:
        # Fallback: regex parse "  field_name:" lines
        lines = re.findall(r"^\s{0,4}(\w+)\s*:", content, re.MULTILINE)
        return "\n".join(f"{l}: any" for l in lines if l not in ("type", "properties", "required")), ""

    lines = []

    def extract_props(obj, prefix=""):
        if isinstance(obj, dict):
            props = obj.get("properties") or {}
            for name, prop in props.items():
                full = f"{prefix}{name}" if prefix else name
                ptype = (prop or {}).get("type", "any") if isinstance(prop, dict) else "any"
                desc = (prop or {}).get("description", "") if isinstance(prop, dict) else ""
                line = f"{full}: {ptype}"
                if desc:
                    line += f" — {desc}"
                lines.append(line)
                # recurse into nested objects
                if isinstance(prop, dict) and prop.get("type") == "object":
                    extract_props(prop, prefix=full + ".")
            # flat key: type mapping
            if not props:
                for k, v in obj.items():
                    if isinstance(v, str) and k not in ("type", "$schema", "title", "description"):
                        lines.append(f"{k}: {v}")

    extract_props(data)
    warn = (
        f"Schema has {len(lines)} fields — only the most relevant will be mapped."
        if len(lines) > LARGE_SCHEMA_FIELD_LIMIT else ""
    )
    return "\n".join(lines) if lines else content, warn


def _parse_typescript_schema(content: str) -> tuple[str, str]:
    """Extract fields from a TypeScript interface or type definition.
    Returns (structured_text, warning).
    """
    # Match:  fieldName: type;   or   fieldName?: type;
    field_pattern = re.compile(r"^\s+(\w+)\??\s*:\s*(.+?)\s*;", re.MULTILINE)
    lines = []
    for m in field_pattern.finditer(content):
        name = m.group(1)
        ts_type = m.group(2).strip()
        lines.append(f"{name}: {ts_type}")

    warn = (
        f"Interface has {len(lines)} fields — only the most relevant will be mapped."
        if len(lines) > LARGE_SCHEMA_FIELD_LIMIT else ""
    )
    return "\n".join(lines) if lines else content, warn


def parse_schema_file(filename: str, content: bytes) -> tuple[str, str]:
    """Parse a schema file into structured text for the LLM interpreter.

    Args:
        filename: original filename (used to detect format by extension)
        content:  raw file bytes

    Returns:
        (schema_text, warning)  — schema_text is passed to interpret(),
        warning is shown in the UI if non-empty.
    """
    suffix = Path(filename).suffix.lower()
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return "", "Could not decode file as UTF-8."

    if suffix == ".json":
        return _parse_json_schema(text)
    elif suffix == ".py":
        return _parse_python_schema(text)
    elif suffix in (".sql",):
        return _parse_sql_schema(text)
    elif suffix in (".yaml", ".yml"):
        return _parse_yaml_schema(text)
    elif suffix in (".ts", ".tsx"):
        return _parse_typescript_schema(text)
    else:
        # Unknown format — pass raw text to LLM
        return text, f"Unknown format '{suffix}' — passing raw text to interpreter."

_SYSTEM = """
You are a schema mapping assistant. You will be given:
1. A list of field names extracted from a policy document analysis (the "extracted names")
2. A user-provided schema description (in any format)

Your task: for each extracted field name, find the best matching field in the user schema.
If a field has no reasonable match, map it to itself.

Output ONLY a valid JSON object: {"extracted_name": "schema_name", ...}
No prose, no markdown fences.

Rules:
- Match semantically, not just lexically. "buyer_age" → "age" if the schema has an "age" field.
- If the schema is empty or None, map every field to itself.
- Never invent field names that don't appear in the user schema.
- Never omit an extracted field — every input key must appear in the output.
"""


def interpret(
    field_required_names: list[str],
    user_schema: Optional[str],
    model: Optional[str] = None,
) -> dict[str, str]:
    """Map extracted field_required names to user schema field names.

    Returns identity mapping if user_schema is None or empty.
    """
    if not field_required_names:
        return {}

    identity = {f: f for f in field_required_names}

    if not user_schema or not user_schema.strip():
        return identity

    model_name = model or _cfg.get("MODEL_STAGE2", "openai/gpt-5.4")
    user_msg = f"""Extracted field names:
{json.dumps(field_required_names, indent=2)}

User schema:
{user_schema.strip()}

Map each extracted name to the best matching field in the user schema.
Output only valid JSON."""

    result = complete(
        system=_SYSTEM,
        user=user_msg,
        model=model_name,
        max_tokens=1024,
        temperature=0.0,
    )

    try:
        mapping = json.loads(result.text)
        if not isinstance(mapping, dict):
            return identity
        # Ensure all input keys are present; fall back to identity for any missing
        return {f: mapping.get(f, f) for f in field_required_names}
    except (json.JSONDecodeError, ValueError):
        return identity


def apply_mapping(code: str, mapping: dict[str, str]) -> str:
    """Substitute field names in generated code using the mapping dict.

    Only substitutes profile.get("old_name") patterns to avoid replacing
    variable names or comments unintentionally.
    """
    import re
    result = code
    # Sort by length descending to avoid partial replacements
    for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        if old == new:
            continue
        # Replace profile.get("old_name") and profile.get('old_name')
        result = re.sub(
            rf'profile\.get\({re.escape(repr(old))}\)',
            f'profile.get({repr(new)})',
            result,
        )
        # Also replace bare string literals used as field references in comments/reasons
        result = re.sub(
            rf'\bfield {re.escape(repr(old))}\b',
            f'field {repr(new)}',
            result,
        )
    return result
