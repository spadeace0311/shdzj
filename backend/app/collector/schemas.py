from datetime import datetime

from pydantic import BaseModel


class CollectorProviderStatusResponse(BaseModel):
    provider: str
    state: str
    connected: bool
    last_http_status: int | None
    last_connected_at: datetime | None
    last_message_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    reconnect_count: int
    last_error: str | None
    updated_at: datetime


class CollectorStatusResponse(BaseModel):
    overall_state: str
    providers: list[CollectorProviderStatusResponse]
    open_dead_letter_count: int
    boundary_version: str | None
    last_ingested_event_id: str | None
