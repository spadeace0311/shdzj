from typing import Protocol

from app.events.domain import NormalizedEvent


class SourceAdapter(Protocol):
    def parse(self, payload: dict[str, object]) -> NormalizedEvent: ...
