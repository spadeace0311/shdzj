from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.qa.domain import AnswerDraft, ExecutionPlan, KnowledgeQuery, MapIntent, ToolCallPlan
from app.qa.planner import PlanPayload
from app.qa.prompts import (
    build_answer_messages,
    build_plan_messages,
    build_stream_answer_messages,
)


class DeepSeekError(RuntimeError):
    pass


class DeepSeekUnavailableError(DeepSeekError):
    pass


class DeepSeekRequestError(DeepSeekError):
    pass


class DeepSeekMalformedResponseError(DeepSeekError):
    pass


class AnswerDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    structured: dict[str, Any] = Field(default_factory=dict)
    citation_keys: list[str] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)


_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)


class DeepSeekAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._base_url = settings.deepseek_base_url.rstrip("/")

    async def plan(
        self,
        question: str,
        context: Any,
        tool_catalog: list[dict[str, Any]],
    ) -> ExecutionPlan:
        self._require_api_key()
        messages = build_plan_messages(question, context, tool_catalog)
        response = await self._post_json(messages)
        payload = await self._read_json_payload(response, "plan")
        return self._coerce_execution_plan(payload)

    async def answer(
        self,
        question: str,
        context: Any,
        evidence: Any,
        tool_results: Any,
    ) -> AnswerDraft:
        self._require_api_key()
        messages = build_answer_messages(question, context, evidence, tool_results)
        response = await self._post_json(messages)
        payload = await self._read_json_payload(response, "answer")
        try:
            parsed = AnswerDraftPayload.model_validate(payload)
        except ValidationError:
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned malformed answer JSON"
            ) from None
        return AnswerDraft(
            text=parsed.text,
            structured=parsed.structured,
            citation_keys=parsed.citation_keys,
            degraded_reasons=parsed.degraded_reasons,
        )

    async def stream_answer(
        self,
        question: str,
        context: Any,
        evidence: Any,
        tool_results: Any,
    ) -> AsyncIterator[str]:
        self._require_api_key()
        messages = build_stream_answer_messages(
            question,
            context,
            evidence,
            tool_results,
        )
        payload = self._request_payload(messages, response_format=None)
        payload["stream"] = True
        headers = self._headers()

        if self._client is not None:
            client = self._client
            stream_context = None
        else:
            client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._settings.deepseek_timeout_seconds,
            )
            stream_context = client

        try:
            try:
                async with client.stream(
                    "POST",
                    self._endpoint(),
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        raise DeepSeekRequestError(
                            f"DeepSeek request failed with status {response.status_code}"
                        ) from None
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            raise DeepSeekMalformedResponseError(
                                "DeepSeek returned malformed stream JSON"
                            ) from None
                        content = self._extract_stream_content(event)
                        if content:
                            yield content
            except _RETRYABLE_TRANSPORT_ERRORS:
                raise DeepSeekRequestError(
                    "DeepSeek stream request failed because the connection could not be established"
                ) from None
        finally:
            if stream_context is not None:
                await stream_context.aclose()

    async def _post_json(
        self,
        messages: list[dict[str, str]],
    ) -> httpx.Response:
        payload = self._request_payload(
            messages,
            response_format={"type": "json_object"},
        )
        headers = self._headers()

        if self._client is not None:
            client = self._client
            should_close = False
        else:
            client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._settings.deepseek_timeout_seconds,
            )
            should_close = True

        try:
            return await self._post_with_retries(client, payload, headers)
        finally:
            if should_close:
                await client.aclose()

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        max_retries = self._settings.deepseek_max_retries
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(
                    self._endpoint(),
                    headers=headers,
                    json=payload,
                )
            except _RETRYABLE_TRANSPORT_ERRORS:
                if attempt >= max_retries:
                    raise DeepSeekRequestError(
                        "DeepSeek request failed because the connection could not be established"
                    ) from None
                await asyncio.sleep(0.5 * 2**attempt)
                continue

            retryable_status = response.status_code == 429 or response.status_code >= 500
            if retryable_status and attempt < max_retries:
                await response.aclose()
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code != 200:
                await response.aclose()
                raise DeepSeekRequestError(
                    f"DeepSeek request failed with status {response.status_code}"
                ) from None
            return response

        raise DeepSeekRequestError("DeepSeek request failed after configured retries") from None

    async def _read_json_payload(
        self,
        response: httpx.Response,
        kind: str,
    ) -> Any:
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned malformed JSON"
            ) from None

        if not isinstance(body, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a non-object response"
            ) from None
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a response without choices"
            ) from None
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a malformed choice"
            ) from None
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a malformed message"
            ) from None
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekMalformedResponseError(
                f"DeepSeek returned an empty {kind} response"
            ) from None
        if kind == "plan" and len(content) > 20000:
            raise DeepSeekMalformedResponseError(
                "DeepSeek plan exceeds 20000 characters"
            ) from None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise DeepSeekMalformedResponseError(
                f"DeepSeek returned malformed {kind} JSON"
            ) from None

    def _extract_stream_content(self, event: Any) -> str | None:
        if not isinstance(event, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a non-object stream event"
            ) from None
        if "choices" not in event:
            return None
        choices = event["choices"]
        if choices == []:
            return None
        if not isinstance(choices, list):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned malformed stream choices"
            ) from None
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a malformed stream choice"
            ) from None
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a malformed stream delta"
            ) from None
        content = delta.get("content")
        if content is None or content == "":
            return None
        if not isinstance(content, str):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned non-text stream content"
            ) from None
        return content

    def _coerce_execution_plan(self, payload: Any) -> ExecutionPlan:
        if not isinstance(payload, dict):
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned a non-object plan"
            ) from None
        try:
            parsed = PlanPayload.model_validate(payload)
        except ValidationError:
            raise DeepSeekMalformedResponseError(
                "DeepSeek returned malformed plan JSON"
            ) from None
        return ExecutionPlan(
            intent=parsed.intent,
            tool_calls=[
                ToolCallPlan(name=call.name, arguments=call.arguments)
                for call in parsed.tool_calls
            ],
            knowledge_queries=[
                KnowledgeQuery(text=query.text, top_k=query.top_k)
                for query in parsed.knowledge_queries
            ],
            map_intents=[
                MapIntent(
                    action_type=action.action_type,
                    target_ref=action.target_ref,
                    reason=action.reason,
                    bounds=action.bounds,
                    radius_km=action.radius_km,
                    layer_id=action.layer_id,
                    layers=action.layers,
                )
                for action in parsed.map_intents
            ],
            clarification=parsed.clarification,
        )

    def _require_api_key(self) -> None:
        if not self._settings.deepseek_api_key.get_secret_value():
            raise DeepSeekUnavailableError("DeepSeek API key is not configured")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                f"Bearer {self._settings.deepseek_api_key.get_secret_value()}"
            ),
            "Content-Type": "application/json",
        }

    def _request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._settings.deepseek_model,
            "messages": messages,
            "temperature": 0,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        return payload

    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"
