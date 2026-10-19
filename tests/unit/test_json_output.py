import asyncio

import pytest
from pydantic import BaseModel

from orchestrator.utils.json_output import JSONExtractionError, extract_json, parse_model


class Thing(BaseModel):
    name: str
    count: int


def test_extract_plain_fenced_and_embedded():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('text\n```json\n{"a": 2}\n```\nmore') == {"a": 2}
    assert extract_json('Sure! Here it is: {"a": {"b": "}"}} thanks') == {"a": {"b": "}"}}


def test_extract_failure():
    with pytest.raises(JSONExtractionError):
        extract_json("no json here")


def test_parse_model_validates():
    assert parse_model('{"name": "x", "count": 3}', Thing).count == 3
    with pytest.raises(JSONExtractionError):
        parse_model('{"name": "x"}', Thing)


def test_complete_json_retries_once_then_fails(session, monkeypatch):
    """Invalid JSON -> one retry with the error -> success; twice invalid -> clear error."""
    from orchestrator.providers.base import BuiltPrompt, CompletionResult, ProviderError
    from orchestrator.services import provider_service

    replies = iter(["not json", '{"name": "ok", "count": 1}'])

    async def fake_complete(session, profile, prompt, metadata=None):
        return CompletionResult(text=next(replies), model="m")

    monkeypatch.setattr(provider_service, "complete", fake_complete)
    profile = provider_service.get_profile(session, "mock-worker")
    result, _ = asyncio.run(
        provider_service.complete_json(
            session, profile, BuiltPrompt(role="worker", sections=[]), Thing
        )
    )
    assert result.name == "ok"

    async def always_bad(session, profile, prompt, metadata=None):
        return CompletionResult(text="nope", model="m")

    monkeypatch.setattr(provider_service, "complete", always_bad)
    with pytest.raises(ProviderError, match="invalid JSON twice"):
        asyncio.run(
            provider_service.complete_json(
                session, profile, BuiltPrompt(role="worker", sections=[]), Thing
            )
        )
