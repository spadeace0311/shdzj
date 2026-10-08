import json

import httpx
import pytest
from pydantic import BaseModel, Field, SecretStr

from app.config import Settings
from app.qa.deepseek import (
    DeepSeekAdapter,
    DeepSeekMalformedResponseError,
    DeepSeekRequestError,
    DeepSeekUnavailableError,
)


VALID_DATABASE_URL = "postgresql+asyncpg://earthquake:earthquake@localhost:5432/earthquake"
VALID_JWT_SECRET = "test-jwt-secret-at-least-16-characters"
VALID_SUPERADMIN_PASSWORD = "test-superadmin-password-at-least-16-characters"


def make_settings(
    *,
    api_key: str = "test-deepseek-key",
    base_url: str = "https://deepseek.test",
    model: str = "deepseek-flash",
    timeout_seconds: float = 0.1,
    max_retries: int = 2,
) -> Settings:
    return Settings(
        _env_file=None,
        database_url=VALID_DATABASE_URL,
        jwt_secret=VALID_JWT_SECRET,
        superadmin_initial_password=VALID_SUPERADMIN_PASSWORD,
        deepseek_api_key=SecretStr(api_key),
        deepseek_base_url=base_url,
        deepseek_model=model,
        deepseek_timeout_seconds=timeout_seconds,
        deepseek_max_retries=max_retries,
    )


class FaultInput(BaseModel):
    event_id: str = Field(min_length=1)


class FaultTool:
    name = "fault.nearest"
    description = "查询最近断裂带"
    input_model = FaultInput


class FakeRegistry:
    def get(self, name: str) -> FaultTool | None:
        return FaultTool() if name == "fault.nearest" else None

    def catalog(self) -> list[dict]:
        return [
            {
                "name": FaultTool.name,
                "description": FaultTool.description,
                "parameters": {
                    "type": "object",
                    "properties": {"event_id": {"type": "string"}},
                    "required": ["event_id"],
                },
            }
        ]


def plan_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


def chat_body(body: object) -> httpx.Response:
    return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        {"choices": "not-a-list"},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_plan_rejects_malformed_response_containers(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_body(body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(max_retries=0), client=client)

    async def run() -> None:
        with pytest.raises(DeepSeekMalformedResponseError):
            await adapter.plan("问题", {}, [])
        await client.aclose()

    import asyncio

    asyncio.run(run())


@pytest.mark.parametrize(
    "event",
    [
        [],
        None,
        {"choices": "not-a-list"},
        {"choices": [None]},
        {"choices": [{"delta": None}]},
        {"choices": [{"delta": "not-an-object"}]},
        {"choices": [{"delta": {"content": 123}}]},
    ],
)
def test_stream_rejects_malformed_response_containers(event: object) -> None:
    content = f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content.encode(),
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(), client=client)

    async def run() -> None:
        with pytest.raises(DeepSeekMalformedResponseError):
            async for _chunk in adapter.stream_answer("问题", {}, [], []):
                pass
        await client.aclose()

    import asyncio

    asyncio.run(run())


def test_plan_uses_chat_completions_json_response_format() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return chat_response(
            plan_json(
                {
                    "intent": "distance",
                    "tool_calls": [],
                    "knowledge_queries": [],
                    "map_intents": [],
                    "clarification": None,
                }
            )
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(), client=client)

    async def run() -> None:
        await adapter.plan("震中距最近断层多远？", {"event_id": "e1"}, FakeRegistry().catalog())
        await client.aclose()

    import asyncio

    asyncio.run(run())

    assert captured["path"] == "/chat/completions"
    assert captured["payload"]["model"] == "deepseek-flash"
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_plan_retries_connection_errors_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("connection failed", request=request)
        return chat_response(
            plan_json(
                {
                    "intent": "distance",
                    "tool_calls": [],
                    "knowledge_queries": [],
                    "map_intents": [],
                    "clarification": None,
                }
            )
        )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.qa.deepseek.asyncio.sleep", fake_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(max_retries=2), client=client)

    async def run() -> None:
        plan = await adapter.plan("问题", {}, [])
        assert plan.intent == "distance"
        await client.aclose()

    import asyncio

    asyncio.run(run())
    monkeypatch.undo()
    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_plan_retries_429_and_5xx_but_not_other_4xx() -> None:
    sequence = [429, 503]

    def handler(request: httpx.Request) -> httpx.Response:
        if sequence:
            return httpx.Response(sequence.pop(0), text="temporary")
        return chat_response(
            plan_json(
                {
                    "intent": "distance",
                    "tool_calls": [],
                    "knowledge_queries": [],
                    "map_intents": [],
                    "clarification": None,
                }
            )
        )

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.qa.deepseek.asyncio.sleep", fake_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(max_retries=2), client=client)

    async def run() -> None:
        await adapter.plan("问题", {}, [])
        await client.aclose()

    import asyncio

    asyncio.run(run())
    monkeypatch.undo()
    assert sequence == []


def test_plan_does_not_retry_non_retryable_4xx() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    async def fake_sleep(seconds: float) -> None:
        raise AssertionError("should not sleep")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.qa.deepseek.asyncio.sleep", fake_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(max_retries=3), client=client)

    async def run() -> None:
        with pytest.raises(DeepSeekRequestError):
            await adapter.plan("问题", {}, [])
        await client.aclose()

    import asyncio

    asyncio.run(run())
    monkeypatch.undo()
    assert calls == 1


def test_plan_rejects_malformed_json_without_retrying() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="{not-json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(max_retries=3), client=client)

    async def run() -> None:
        with pytest.raises(DeepSeekMalformedResponseError):
            await adapter.plan("问题", {}, [])
        await client.aclose()

    import asyncio

    asyncio.run(run())
    assert calls == 1


def test_missing_key_raises_unavailable_without_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response("{}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(api_key=""), client=client)

    async def run() -> None:
        with pytest.raises(DeepSeekUnavailableError):
            await adapter.plan("问题", {}, [])
        await client.aclose()

    import asyncio

    asyncio.run(run())
    assert calls == 0


def test_error_text_does_not_contain_api_key() -> None:
    api_key = "super-secret-deepseek-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=api_key)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(api_key=api_key, max_retries=0), client=client)

    async def run() -> None:
        with pytest.raises(DeepSeekRequestError) as captured:
            await adapter.plan("问题", {}, [])
        assert api_key not in str(captured.value)
        assert api_key not in repr(captured.value)
        await client.aclose()

    import asyncio

    asyncio.run(run())


def test_answer_parses_structured_draft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(
            json.dumps(
                {
                    "text": "无法确认",
                    "structured": {"missing": ["knowledge_evidence"]},
                    "citation_keys": [],
                    "degraded_reasons": ["knowledge_evidence"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(), client=client)

    async def run() -> None:
        draft = await adapter.answer("2030年上海发生了什么？", {}, [], [])
        assert draft.text == "无法确认"
        assert draft.structured == {"missing": ["knowledge_evidence"]}
        assert draft.degraded_reasons == ["knowledge_evidence"]
        await client.aclose()

    import asyncio

    asyncio.run(run())


def test_stream_answer_yields_delta_content_and_preserves_emitted_text() -> None:
    content = (
        'data: {"choices":[{"delta":{"content":"第一段"}}]}\n\n'
        "data: not-json\n\n"
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=content.encode(),
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekAdapter(make_settings(), client=client)
    chunks: list[str] = []

    async def run() -> None:
        with pytest.raises(DeepSeekMalformedResponseError):
            async for chunk in adapter.stream_answer("问题", {}, [], []):
                chunks.append(chunk)
        await client.aclose()

    import asyncio

    asyncio.run(run())
    assert chunks == ["第一段"]
    assert captured["payload"]["stream"] is True
