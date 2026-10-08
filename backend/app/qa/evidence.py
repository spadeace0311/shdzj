from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.knowledge.index import RetrievedEvidence
from app.qa.access import is_model_exportable_access_level

_LAYER_RANK = {
    "local_authority": 0,
    "structured_live": 1,
    "public_reference": 2,
}
_MAX_CITATIONS = 3
_MAX_PER_VERSION = 3
_NUMERIC_CLAIM_PATTERN = re.compile(r"[0-9]")
_METRIC_NAMES = frozenset(
    {
        "count",
        "depth_km",
        "distance_km",
        "floating_population",
        "latitude",
        "longitude",
        "magnitude",
        "rate",
        "ratio",
        "resident_population",
        "total_population",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    citation_key: str
    chunk_id: UUID
    version_id: UUID
    source_title: str
    version_label: str
    locator: str | None
    excerpt: str
    source_uri: str | None
    checksum: str
    layer: str
    access_level: str

    def to_model_dict(self) -> dict[str, Any]:
        return {
            "kind": "document",
            "citation_key": self.citation_key,
            "chunk_id": str(self.chunk_id),
            "source_title": self.source_title,
            "version": self.version_label,
            "locator": self.locator,
            "text": self.excerpt,
            "source_uri": self.source_uri,
            "checksum": self.checksum,
            "layer": self.layer,
        }

    def to_persistence_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "citation_key": self.citation_key,
            "source_title": self.source_title,
            "version_label": self.version_label,
            "locator": self.locator,
            "excerpt": self.excerpt,
            "source_uri": self.source_uri,
            "checksum": self.checksum,
        }

    def to_event_dict(self) -> dict[str, Any]:
        return {
            "citation_key": self.citation_key,
            "chunk_id": str(self.chunk_id),
            "source_title": self.source_title,
            "version_label": self.version_label,
            "locator": self.locator,
            "excerpt": self.excerpt,
            "source_uri": self.source_uri,
            "checksum": self.checksum,
            "access_level": self.access_level,
        }


@dataclass(frozen=True, slots=True)
class EvidencePack:
    primary: tuple[dict[str, Any], ...]
    citations: tuple[EvidenceCitation, ...]
    restricted_count: int
    conflict_notes: tuple[str, ...]
    authority_notes: tuple[str, ...]

    @property
    def model_evidence(self) -> list[dict[str, Any]]:
        return list(self.primary)


@dataclass(frozen=True, slots=True)
class _StructuredFact:
    tool_name: str
    metric_key: str
    scope_key: str
    scope: dict[str, Any]
    value: Any
    unit: str | None
    source: str | None
    version: str | None
    parameters: dict[str, Any]

    def to_primary_dict(self) -> dict[str, Any]:
        return {
            "kind": "structured",
            "authority": "structured",
            "tool_name": self.tool_name,
            "metric_key": self.metric_key,
            "scope": self.scope,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "version": self.version,
            "parameters": self.parameters,
        }


class EvidenceBuilder:
    def build(
        self,
        evidence: Iterable[RetrievedEvidence] = (),
        tool_results: Iterable[Any] = (),
    ) -> EvidencePack:
        facts, conflict_notes = _structured_facts(tool_results)
        citations, restricted_count = _document_citations(
            evidence,
            prioritize_nonnumeric=bool(facts),
        )
        authority_notes: list[str] = []
        if facts:
            suppressed = [
                citation.citation_key
                for citation in citations
                if _contains_numeric_claim(citation.excerpt)
            ]
            authority_notes.append(
                "structured_authority:"
                f"{','.join(fact.to_primary_dict()['tool_name'] for fact in facts)}; "
                "documentary_citation_keys:"
                f"{','.join(citation.citation_key for citation in citations)}; "
                "numeric_document_citations_suppressed:"
                f"{','.join(suppressed) if suppressed else 'none'}"
            )
        primary = tuple(
            [
                *(fact.to_primary_dict() for fact in facts),
                *(
                    {
                        **citation.to_model_dict(),
                        "authority": "documentary_nonnumeric",
                    }
                    for citation in citations
                    if is_model_exportable_access_level(citation.access_level)
                    and not (
                        facts and _contains_numeric_claim(citation.excerpt)
                    )
                ),
            ]
        )
        return EvidencePack(
            primary=primary,
            citations=citations,
            restricted_count=restricted_count,
            conflict_notes=tuple(conflict_notes),
            authority_notes=tuple(authority_notes),
        )


def build_evidence_pack(
    evidence: Iterable[RetrievedEvidence] = (),
    tool_results: Iterable[Any] = (),
) -> EvidencePack:
    return EvidenceBuilder().build(evidence, tool_results)


def _document_citations(
    evidence: Iterable[RetrievedEvidence],
    *,
    prioritize_nonnumeric: bool = False,
) -> tuple[tuple[EvidenceCitation, ...], int]:
    unique: dict[UUID, RetrievedEvidence] = {}
    for item in evidence:
        unique.setdefault(item.chunk_id, item)

    ordered = sorted(
        unique.values(),
        key=lambda item: (
            int(prioritize_nonnumeric and _contains_numeric_claim(item.text)),
            _LAYER_RANK.get(item.layer, 99),
            -max(item.scores.values(), default=0.0),
            str(item.version_id),
            str(item.chunk_id),
        ),
    )
    per_version: dict[UUID, int] = defaultdict(int)
    selected: list[RetrievedEvidence] = []
    for item in ordered:
        if per_version[item.version_id] >= _MAX_PER_VERSION:
            continue
        selected.append(item)
        per_version[item.version_id] += 1
        if len(selected) == _MAX_CITATIONS:
            break

    citations = tuple(
        EvidenceCitation(
            citation_key=f"C{index}",
            chunk_id=item.chunk_id,
            version_id=item.version_id,
            source_title=item.source_title,
            version_label=str(item.version_id),
            locator=_locator(item),
            excerpt=item.text,
            source_uri=item.source_uri,
            checksum=item.checksum,
            layer=item.layer,
            access_level=item.access_level,
        )
        for index, item in enumerate(selected, start=1)
    )
    restricted_count = sum(
        not is_model_exportable_access_level(item.access_level)
        for item in unique.values()
    )
    return citations, restricted_count


def _structured_facts(
    tool_results: Iterable[Any],
) -> tuple[list[_StructuredFact], list[str]]:
    facts: list[_StructuredFact] = []
    for execution in tool_results:
        name = getattr(execution, "name", None) or getattr(
            execution,
            "tool_name",
            None,
        )
        result = getattr(execution, "result", getattr(execution, "tool_result", execution))
        status = str(getattr(result, "status", "ok"))
        if status != "ok":
            continue
        value = getattr(result, "value", None)
        if not isinstance(value, Mapping):
            continue
        parameters = _mapping(getattr(result, "parameters", {}))
        scope_digest = _scope_key(parameters)
        scope = dict(parameters)
        source = getattr(result, "source", None)
        version = getattr(result, "version", None)
        unit = getattr(result, "unit", None)
        for metric_key, metric_value, metric_unit in _iter_metrics(value):
            facts.append(
                _StructuredFact(
                    tool_name=str(name or "unknown"),
                    metric_key=metric_key,
                    scope_key=scope_digest,
                    scope=scope,
                    value=_json_safe(metric_value),
                    unit=metric_unit or unit,
                    source=source,
                    version=version,
                    parameters=parameters,
                )
            )

    grouped: dict[tuple[str, str], list[_StructuredFact]] = defaultdict(list)
    for fact in facts:
        grouped[(fact.metric_key, fact.scope_key)].append(fact)

    conflict_notes: list[str] = []
    for (_conflict_metric, _conflict_scope), group in grouped.items():
        by_value: dict[str, list[_StructuredFact]] = defaultdict(list)
        for fact in group:
            by_value[_canonical(fact.value)].append(fact)
        if len(by_value) <= 1:
            continue
        first = group[0]
        values = [
            f"{_canonical(item[0].value)} {item[0].unit or ''}".strip()
            for item in by_value.values()
        ]
        versions = [
            f"{item[0].source or item[0].tool_name}@{item[0].version or 'unknown'}"
            for item in by_value.values()
        ]
        conflict_notes.append(
            f"structured_conflict:{first.metric_key} "
            f"scope={first.scope_key}: "
            + " vs ".join(
                f"{value} ({version})" for value, version in zip(values, versions, strict=True)
            )
        )
    return facts, conflict_notes


def _iter_metrics(value: Mapping[str, Any]) -> Iterable[tuple[str, Any, str | None]]:
    metrics = value.get("metrics")
    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            metric_key = metric.get("metric_key")
            metric_value = metric.get("numeric_value")
            if isinstance(metric_key, str) and _is_metric_value(metric_value):
                yield metric_key, metric_value, _unit(metric.get("unit"))

    statistics = value.get("statistics")
    if isinstance(statistics, Mapping):
        for key, metric_value in statistics.items():
            if isinstance(key, str) and _is_metric_value(metric_value):
                yield key, metric_value, None

    for key, metric_value in value.items():
        if key in {"metrics", "statistics"}:
            continue
        if _is_metric_key(key) and _is_metric_value(metric_value):
            yield key, metric_value, _unit(value.get("unit"))


def _is_metric_key(key: Any) -> bool:
    return isinstance(key, str) and (
        key in _METRIC_NAMES
        or key.endswith("_km")
        or key.endswith("_population")
        or key.endswith("_rate")
        or key.endswith("_ratio")
    )


def _is_metric_value(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _unit(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _scope_key(parameters: Mapping[str, Any]) -> str:
    scope_names = (
        "event_id",
        "radius_km",
        "magnitude_min",
        "area_scope",
        "area_code",
        "value_type",
        "product_type",
        "historical_event_id",
    )
    return _canonical({name: parameters[name] for name in scope_names if name in parameters})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _locator(item: RetrievedEvidence) -> str | None:
    parts: list[str] = []
    if item.page_from is not None:
        if item.page_to is not None and item.page_to != item.page_from:
            parts.append(f"p.{item.page_from}-{item.page_to}")
        else:
            parts.append(f"p.{item.page_from}")
    if item.section_path:
        parts.append(" > ".join(item.section_path))
    return " / ".join(parts) if parts else None


def _canonical(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _contains_numeric_claim(text: str) -> bool:
    return bool(_NUMERIC_CLAIM_PATTERN.search(text))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "EvidenceBuilder",
    "EvidenceCitation",
    "EvidencePack",
    "build_evidence_pack",
]
