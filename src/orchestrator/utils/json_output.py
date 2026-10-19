"""Extract and validate JSON from LLM output (raw, fenced, or embedded in prose)."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n?```", re.S)


class JSONExtractionError(ValueError):
    pass


def _balanced_objects(text: str) -> list[str]:
    """Find top-level {...} spans, respecting strings."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : i + 1])
    return spans


def extract_json(text: str) -> Any:
    """Return the first parseable JSON value. Tries: whole text, fenced blocks, brace spans."""
    if text is None:
        raise JSONExtractionError("no text")
    stripped = text.strip()
    candidates: list[str] = [stripped]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    candidates += _balanced_objects(text)
    for c in candidates:
        if not c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise JSONExtractionError("no valid JSON object found in output")


def parse_model(text: str, model: type[M]) -> M:
    """Extract JSON and validate against `model`. Raises JSONExtractionError on failure."""
    data = extract_json(text)
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise JSONExtractionError(f"JSON did not match {model.__name__}: {e}") from e


def json_instruction(model: type[BaseModel]) -> str:
    schema = json.dumps(model.model_json_schema(), sort_keys=True)
    return f"Respond with a single JSON object only, no prose, matching this JSON Schema:\n{schema}"
