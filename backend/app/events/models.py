import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RawMessage(Base):
    __tablename__ = "raw_messages"
    __table_args__ = (UniqueConstraint("checksum", name="uq_raw_messages_checksum"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    message_kind: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str | None] = mapped_column(String(32), index=True)
    ingest_lane: Mapped[str | None] = mapped_column(String(32), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        server_default=text("now()"),
    )
    checksum: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class EarthquakeEvent(Base):
    __tablename__ = "earthquake_events"
    __table_args__ = (Index("ix_earthquake_events_geom", "geom", postgresql_using="gist"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32), index=True)
    canonical_source_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    origin_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    depth_km: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    magnitude: Mapped[Decimal] = mapped_column(Numeric(4, 1), index=True)
    place: Mapped[str] = mapped_column(String(256))
    geom = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    institutional_level: Mapped[str | None] = mapped_column(String(32), index=True)
    service_level: Mapped[int | None] = mapped_column(Integer, index=True)
    response_suggestion: Mapped[dict | None] = mapped_column(JSONB)
    response_rule_version: Mapped[str | None] = mapped_column(String(32))
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    t1_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lifecycle_state: Mapped[str] = mapped_column(
        String(32),
        default="auto_pending",
        server_default=text("'auto_pending'"),
        index=True,
    )
    latest_trigger_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    revisions: Mapped[list["EarthquakeRevision"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
    )


class EarthquakeRevision(Base):
    __tablename__ = "earthquake_revisions"
    __table_args__ = (
        UniqueConstraint("event_id", "revision_no", name="uq_earthquake_revisions_event_revision"),
        Index(
            "uq_earthquake_revisions_event_current",
            "event_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "uq_earthquake_revisions_semantic_fingerprint",
            "event_id",
            "semantic_fingerprint",
            unique=True,
            postgresql_where=text("semantic_fingerprint IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    raw_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_messages.id"),
        unique=True,
        index=True,
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    revision_kind: Mapped[str] = mapped_column(String(32), index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), index=True)
    source_report_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_report_number: Mapped[int | None] = mapped_column(Integer)
    origin_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    depth_km: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    magnitude: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    place: Mapped[str] = mapped_column(String(256))
    institutional_level: Mapped[str | None] = mapped_column(String(32))
    service_level: Mapped[int | None] = mapped_column(Integer)
    response_suggestion: Mapped[dict | None] = mapped_column(JSONB)
    response_rule_version: Mapped[str | None] = mapped_column(String(32))
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    provider: Mapped[str | None] = mapped_column(String(32), index=True)
    ingest_lane: Mapped[str | None] = mapped_column(String(32), index=True)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    inside_shanghai: Mapped[bool | None] = mapped_column(Boolean)
    distance_to_boundary_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    region_boundary_version: Mapped[str | None] = mapped_column(String(64), index=True)
    region_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        index=True,
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    event: Mapped[EarthquakeEvent] = relationship(back_populates="revisions")


class EventLifecycleOutbox(Base):
    __tablename__ = "event_lifecycle_outbox"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "revision_id",
            "trigger_type",
            name="uq_event_lifecycle_outbox_event_revision_type",
        ),
        Index("ix_event_lifecycle_outbox_pending", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(64))
    trigger_reason: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
