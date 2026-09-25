from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class RegionContext(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    inside_shanghai: bool | None = None
    distance_to_boundary_km: Decimal | None = Field(default=None, ge=0)
    deaths: int | None = Field(default=None, ge=0)
    max_intensity: Decimal | None = Field(default=None, ge=0, le=12)


class ManualEventRequest(BaseModel):
    origin_time: datetime
    longitude: Decimal = Field(ge=-180, le=180)
    latitude: Decimal = Field(ge=-90, le=90)
    magnitude: Decimal = Field(ge=0, le=10)
    depth_km: Decimal = Field(ge=0, le=1000)
    source: str = Field(min_length=1, max_length=32)
    source_event_id: str | None = Field(default=None, max_length=128)
    place: str = Field(default="", max_length=256)
    event_kind: str = Field(default="manual", pattern="^(manual|test|drill)$")

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.strip().lower()


class EventIngestResponse(BaseModel):
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: str
    institutional_level: str | None = None
    service_level: int | None = None
    lifecycle_state: str | None = None
    t1_at: datetime | None = None


class EventSummaryResponse(BaseModel):
    id: str
    source: str
    event_kind: str
    place: str
    magnitude: Decimal
    depth_km: Decimal
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    institutional_level: str | None
    service_level: int | None
    revision_no: int
    lifecycle_state: str
    t1_at: datetime | None


class EventDetailResponse(BaseModel):
    id: str
    event_kind: str
    source: str
    place: str
    magnitude: Decimal
    depth_km: Decimal
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    institutional_level: str | None
    service_level: int | None
    response_suggestion: dict | None
    response_rule_version: str | None
    revision_no: int
    lifecycle_state: str
    t1_at: datetime | None
