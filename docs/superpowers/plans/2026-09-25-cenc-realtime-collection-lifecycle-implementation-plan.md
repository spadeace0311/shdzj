# CENC 实时采集与事件生命周期实施计划

> **面向上级执行代理：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 建设独立 CENC 实时采集器，以 FAN Studio WebSocket 为主链路、Wolfx HTTP 常驻低频轮询为备用链路，完整记录事件生命周期转换，并在首次正式报到达后写入持久化评估触发。

**架构：** 新增独立 `collector` 进程，与 API 共用后端领域代码和 PostgreSQL 数据库，但运行生命周期完全独立。FAN 与 Wolfx 适配器统一输出 `CollectorEnvelope`；协调器负责归一化、按语义指纹去重、追加事件修订、记录 `T1`，并在同一事务中写入 Outbox。版本化 PostGIS 区域边界解析器为现有响应规则引擎提供上海范围上下文。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2、asyncpg、Alembic、PostgreSQL 16、PostGIS 3.4、`websockets`、`httpx`、pytest、React 19、TypeScript、Vitest、Playwright、Docker Compose。

**设计规格：** `docs/superpowers/specs/2026-09-25-cenc-realtime-collection-lifecycle-design.md`

## 全局约束

- FAN Studio WebSocket 是实时采集主链路。
- Wolfx HTTP 作为低频备用链路常驻运行，不能只在 FAN 故障后才启动。
- 两条链路只接收 CENC 自动速报和正式测定报文。
- 自动速报只建立待定事件，永不写入评估触发。
- 首次正式报记录不可变的 `T1`，并写入一条 `assessment.requested` Outbox。
- 后续内容发生变化的正式报生成 `correction` 修订，并写入新的 Outbox。
- `T1` 是首次正式修订成功提交时的采集器接收时刻。
- FAN 与 Wolfx 的重复报文不得生成重复修订或 Outbox。
- 平台内部来源仍为 `cenc`；实际提供方和传输链路只作为审计元数据。
- 上海本地响应范围包括上海市行政区域及其边界外 50 公里海域。
- 区域数据缺失时必须返回 `pending`，不得猜测制度响应等级。
- `event_lifecycle_outbox` 是未来评估编排器使用的稳定接口；本期不引入 Temporal。
- 采集器不得调用本平台自身的 HTTP 接入接口。
- 凭据从环境变量或受控密钥文件读取，禁止写入日志或提交到仓库。
- `FAN_APP_ID` 是规范配置名；`CENC_APP_ID` 仅作为兼容回退别名。
- 测试事件和演练事件不得被 CENC 实时采集器接收。
- 新文件统一使用 UTF-8；代码标识符和 API 字段保持 ASCII。
- 每个任务都必须以聚焦测试、本地自检和一次独立提交结束。

---

## 目标文件结构

```text
backend/
  app/
    collector/
      __init__.py
      domain.py
      fan.py
      wolfx.py
      coordinator.py
      spool.py
      supervisor.py
      service.py
      main.py
      models.py
      router.py
      schemas.py
      status_service.py
      replay.py
    events/
      lifecycle.py
      models.py
      repository.py
      service.py
    regions/
      __init__.py
      models.py
      repository.py
      service.py
      importer.py
      cli.py
  migrations/versions/
    0005_event_lifecycle_outbox.py
    0006_collector_runtime.py
    0007_region_boundaries.py
  tests/
    fixtures/
      fan_cenc.json
      wolfx_cenc_eqlist.json
      shanghai_boundary.geojson
    test_collector_config.py
    test_collector_domain.py
    test_event_lifecycle.py
    test_event_lifecycle_schema.py
    test_collector_schema.py
    test_region_resolver.py
    test_collected_event_service.py
    test_fan_collector.py
    test_wolfx_collector.py
    test_collector_supervisor.py
    test_collector_spool.py
    test_collector_api.py
    test_collector_status_service.py
frontend/
  src/
    pages/CollectorStatusPage.tsx
  tests/collector-status.test.tsx
  e2e/collector-flow.spec.ts
docs/runbooks/cenc-realtime-collection.md
```

## Task 1：采集器配置与共享契约

**文件：**
- 修改：`backend/app/config.py`
- 修改：`backend/tests/conftest.py`
- 修改：`.env.example`
- 新增：`backend/app/collector/__init__.py`
- 新增：`backend/app/collector/domain.py`
- 新增：`backend/tests/test_collector_config.py`
- 新增：`backend/tests/test_collector_domain.py`

**接口：**
- 使用：现有 `Settings`、`SecretStr`、`NormalizedEvent`。
- 产出：
  - `CollectorProvider(StrEnum)`：`FAN = "fan"`、`WOLFX = "wolfx"`。
  - `CollectorLane(StrEnum)`：`WEBSOCKET = "websocket"`、`HTTP = "http"`。
  - `CollectorEnvelope` 数据类：字段为 `provider`、`lane`、`received_at`、`payload`。
  - `ProviderHealthUpdate` 数据类：字段为 `provider`、`state`、`connected`、`last_http_status`、`last_connected_at`、`last_message_at`、`last_success_at`、`consecutive_failures`、`reconnect_count`、`last_error`、`updated_at`。
  - `Settings.fan_app_id`、`Settings.fan_api_key`、`Settings.resolved_fan_app_id` 以及下方列出的采集配置字段。

- [ ] **步骤 1：先编写会失败的配置测试**

```python
# backend/tests/test_collector_config.py
from pydantic import SecretStr

from app.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "jwt_secret": SecretStr("jwt-secret-at-least-16-characters"),
        "superadmin_initial_password": SecretStr("admin-secret-at-least-16-characters"),
        "cenc_collector_enabled": True,
        "fan_app_id": "",
        "fan_api_key": SecretStr("fan-key"),
        "cenc_app_id": "compat-app-id",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_collector_uses_cenc_app_id_as_compatibility_alias() -> None:
    settings = make_settings()

    assert settings.resolved_fan_app_id == "compat-app-id"


def test_collector_is_disabled_by_default_without_credentials() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        jwt_secret=SecretStr("jwt-secret-at-least-16-characters"),
        superadmin_initial_password=SecretStr("admin-secret-at-least-16-characters"),
    )

    assert settings.cenc_collector_enabled is False


def test_enabled_collector_requires_api_key() -> None:
    try:
        make_settings(fan_api_key=SecretStr(""))
    except ValueError as exc:
        assert "FAN_API_KEY" in str(exc)
    else:
        raise AssertionError("collector configuration must reject an empty API key")


def test_collector_can_be_disabled_without_credentials() -> None:
    settings = make_settings(
        cenc_collector_enabled=False,
        fan_api_key=SecretStr(""),
        cenc_app_id="",
    )

    assert settings.cenc_collector_enabled is False
```

- [ ] **步骤 2：运行配置测试并确认失败**

运行：

```powershell
Set-Location backend
python -m pytest tests/test_collector_config.py -v
```

预期：失败，因为采集器配置尚不存在。

- [ ] **步骤 3：加入精确的采集器配置**

向 `Settings` 加入：

```python
cenc_collector_enabled: bool = False
fan_app_id: str = ""
fan_api_key: SecretStr = SecretStr("")
cenc_app_id: str = ""
fan_ws_primary_url: str = "wss://ws.fanstudio.tech/all"
fan_ws_backup_url: str = "wss://ws.fanstudio.hk/all"
fan_query_interval_seconds: int = 10
wolfx_cenc_url: str = "https://api.wolfx.jp/cenc_eqlist.json"
wolfx_poll_interval_seconds: int = 10
cenc_bootstrap_lookback_hours: int = 24
collector_stale_after_seconds: int = 60
collector_spool_dir: str = "/var/lib/collector-spool"
collector_max_spool_bytes: int = 1_073_741_824
```

加入以下属性和校验逻辑：

```python
@property
def resolved_fan_app_id(self) -> str:
    return self.fan_app_id.strip() or self.cenc_app_id.strip()

@model_validator(mode="after")
def validate_collector_configuration(self) -> "Settings":
    if self.cenc_collector_enabled and not self.resolved_fan_app_id:
        raise ValueError("FAN_APP_ID or CENC_APP_ID is required when the collector is enabled")
    if self.cenc_collector_enabled and not self.fan_api_key.get_secret_value():
        raise ValueError("FAN_API_KEY is required when the collector is enabled")
    if self.fan_query_interval_seconds < 1 or self.wolfx_poll_interval_seconds < 1:
        raise ValueError("collector poll intervals must be positive")
    if self.cenc_bootstrap_lookback_hours < 0:
        raise ValueError("CENC_BOOTSTRAP_LOOKBACK_HOURS must not be negative")
    if self.collector_stale_after_seconds < 1:
        raise ValueError("COLLECTOR_STALE_AFTER_SECONDS must be positive")
    if not self.collector_spool_dir.strip():
        raise ValueError("COLLECTOR_SPOOL_DIR must not be empty")
    if self.collector_max_spool_bytes < 1:
        raise ValueError("COLLECTOR_MAX_SPOOL_BYTES must be positive")
    return self
```

共享 `Settings` 默认禁用采集器，因此 API、Alembic 和普通测试不要求 FAN 凭据。`collector` 服务在 Compose 中显式覆盖 `CENC_COLLECTOR_ENABLED=true`，届时再由同一个 `Settings` 校验凭据；缺少凭据时只阻止 collector 启动。将新增配置全部加入 `.env.example`，密钥字段保持为空，不得写入真实密钥。`backend/tests/conftest.py` 仍显式设置 `CENC_COLLECTOR_ENABLED=false`，避免开发机 `.env` 意外开启采集配置校验。

- [ ] **步骤 4：先编写会失败的共享契约测试**

```python
# backend/tests/test_collector_domain.py
from datetime import UTC, datetime

import pytest

from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider


def test_collector_envelope_normalizes_timestamp_and_copies_payload() -> None:
    source = {"type": "cenc_eqlist", "No1": {"EventID": "CENC-1"}}
    envelope = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
        payload=source,
    )

    source["type"] = "changed"

    assert envelope.provider is CollectorProvider.FAN
    assert envelope.lane is CollectorLane.WEBSOCKET
    assert envelope.payload["type"] == "cenc_eqlist"


def test_collector_envelope_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 1, 2),
            payload={"No1": {}},
        )
```

- [ ] **步骤 5：实现共享契约**

```python
# backend/app/collector/domain.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class CollectorProvider(StrEnum):
    FAN = "fan"
    WOLFX = "wolfx"


class CollectorLane(StrEnum):
    WEBSOCKET = "websocket"
    HTTP = "http"


@dataclass(frozen=True, slots=True)
class CollectorEnvelope:
    provider: CollectorProvider
    lane: CollectorLane
    received_at: datetime
    payload: dict[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must include timezone information")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class ProviderHealthUpdate:
    provider: CollectorProvider
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

    def __post_init__(self) -> None:
        if self.state not in {"starting", "healthy", "degraded", "critical", "stopped"}:
            raise ValueError("invalid collector state")
        if self.consecutive_failures < 0 or self.reconnect_count < 0:
            raise ValueError("collector counters must not be negative")
```

- [ ] **步骤 6：运行聚焦测试**

运行：

```powershell
Set-Location backend
python -m pytest tests/test_collector_config.py tests/test_collector_domain.py -v
python -m ruff check app/config.py app/collector tests/test_collector_config.py tests/test_collector_domain.py
```

预期：全部测试通过，Ruff 无错误。

- [ ] **步骤 7：提交**

```powershell
git add .env.example backend/app/config.py backend/app/collector backend/tests/conftest.py backend/tests/test_collector_config.py backend/tests/test_collector_domain.py
git commit -m "feat: define CENC collector contracts"
```

## Task 2：语义指纹与生命周期分类

**文件：**
- 新增：`backend/app/events/lifecycle.py`
- 新增：`backend/tests/test_event_lifecycle.py`

**接口：**
- 使用：`NormalizedEvent`、`EventKind`、`canonical_source_id()`。
- 产出：
  - `MessageFamily(StrEnum)`：`AUTO = "auto"`、`REVIEWED = "reviewed"`。
  - `semantic_fingerprint(event: NormalizedEvent, family: MessageFamily) -> str`。
  - `classify_reviewed_kind(existing_reviewed_kinds: tuple[EventKind, ...]) -> EventKind`。
  - `message_family(kind: EventKind) -> MessageFamily`。

- [ ] **步骤 1：先编写会失败的指纹测试**

```python
# backend/tests/test_event_lifecycle.py
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.events.domain import EventKind, NormalizedEvent
from app.events.lifecycle import (
    MessageFamily,
    classify_reviewed_kind,
    message_family,
    semantic_fingerprint,
)


BASE = NormalizedEvent(
    kind=EventKind.FORMAL,
    source="cenc",
    source_event_id="CENC-1",
    origin_time=datetime(2026, 9, 25, 1, 2, 3, tzinfo=UTC),
    longitude=Decimal("121.500000"),
    latitude=Decimal("31.200000"),
    depth_km=Decimal("10.00"),
    magnitude=Decimal("5.2"),
    place="上海测试位置",
    report_time=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
)


def test_fingerprint_ignores_provider_event_id() -> None:
    backup = replace(BASE, source_event_id="WOLFX-ALIAS")

    assert semantic_fingerprint(BASE, MessageFamily.REVIEWED) == semantic_fingerprint(
        backup,
        MessageFamily.REVIEWED,
    )


def test_fingerprint_changes_when_magnitude_changes() -> None:
    correction = replace(BASE, magnitude=Decimal("5.3"))

    assert semantic_fingerprint(BASE, MessageFamily.REVIEWED) != semantic_fingerprint(
        correction,
        MessageFamily.REVIEWED,
    )


def test_fingerprint_ignores_report_time_when_report_number_exists() -> None:
    numbered = replace(
        BASE,
        report_number=2,
        report_time=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
    )
    equivalent = replace(
        numbered,
        report_time=datetime(2026, 9, 25, 1, 5, 1, tzinfo=UTC),
    )

    assert semantic_fingerprint(numbered, MessageFamily.REVIEWED) == semantic_fingerprint(
        equivalent,
        MessageFamily.REVIEWED,
    )


def test_fingerprint_uses_report_time_when_report_number_is_missing() -> None:
    first = replace(BASE, report_number=None, report_time=datetime(2026, 9, 25, 1, 5, tzinfo=UTC))
    later = replace(first, report_time=datetime(2026, 9, 25, 1, 6, tzinfo=UTC))

    assert semantic_fingerprint(first, MessageFamily.REVIEWED) != semantic_fingerprint(
        later,
        MessageFamily.REVIEWED,
    )


def test_formal_and_correction_share_reviewed_family_but_auto_does_not() -> None:
    assert message_family(EventKind.FORMAL) is MessageFamily.REVIEWED
    assert message_family(EventKind.CORRECTION) is MessageFamily.REVIEWED
    assert message_family(EventKind.AUTO) is MessageFamily.AUTO


def test_reviewed_kind_is_formal_until_a_reviewed_revision_exists() -> None:
    assert classify_reviewed_kind(()) is EventKind.FORMAL
    assert classify_reviewed_kind((EventKind.AUTO,)) is EventKind.FORMAL
    assert classify_reviewed_kind((EventKind.FORMAL,)) is EventKind.CORRECTION
    assert classify_reviewed_kind((EventKind.FORMAL, EventKind.CORRECTION)) is EventKind.CORRECTION
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```powershell
Set-Location backend
python -m pytest tests/test_event_lifecycle.py -v
```

预期：失败，因为 `app.events.lifecycle` 尚不存在。

- [ ] **步骤 3：实现确定性指纹**

```python
# backend/app/events/lifecycle.py
import hashlib
import json
from decimal import Decimal
from enum import StrEnum

from app.events.domain import EventKind, NormalizedEvent


class MessageFamily(StrEnum):
    AUTO = "auto"
    REVIEWED = "reviewed"


def message_family(kind: EventKind) -> MessageFamily:
    if kind is EventKind.AUTO:
        return MessageFamily.AUTO
    if kind in {EventKind.FORMAL, EventKind.CORRECTION}:
        return MessageFamily.REVIEWED
    raise ValueError(f"unsupported CENC lifecycle kind: {kind}")


def semantic_fingerprint(event: NormalizedEvent, family: MessageFamily) -> str:
    material = {
        "source": "cenc",
        "family": family.value,
        "origin_time": event.origin_time.isoformat(),
        "longitude": _decimal_text(event.longitude, 6),
        "latitude": _decimal_text(event.latitude, 6),
        "depth_km": _decimal_text(event.depth_km, 2),
        "magnitude": _decimal_text(event.magnitude, 1),
    }
    if event.report_number is not None:
        material["report_number"] = event.report_number
    else:
        material["report_time"] = (
            event.report_time.isoformat() if event.report_time is not None else None
        )
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_reviewed_kind(
    existing_reviewed_kinds: tuple[EventKind, ...],
) -> EventKind:
    if any(
        kind in {EventKind.FORMAL, EventKind.CORRECTION}
        for kind in existing_reviewed_kinds
    ):
        return EventKind.CORRECTION
    return EventKind.FORMAL


def _decimal_text(value: Decimal, places: int) -> str:
    zero = Decimal(0).quantize(Decimal(1).scaleb(-places))
    return format(zero if value == 0 else value, f".{places}f")
```

有报次时只使用报次判断正式报文版本，正式报文时间不参与指纹；没有报次时才使用源正式报文时间。未来提供方若增加仅用于传输的附加时间戳，不得将其映射到 `NormalizedEvent.report_time`。

- [ ] **步骤 4：运行聚焦测试与静态检查**

运行：

```powershell
Set-Location backend
python -m pytest tests/test_event_lifecycle.py -v
python -m ruff check app/events/lifecycle.py tests/test_event_lifecycle.py
```

预期：全部测试通过，Ruff 无错误。

- [ ] **步骤 5：提交**

```powershell
git add backend/app/events/lifecycle.py backend/tests/test_event_lifecycle.py
git commit -m "feat: add CENC semantic lifecycle rules"
```

## Task 3：事件生命周期数据结构与 Outbox

**文件：**
- 修改：`backend/app/events/models.py`
- 修改：`backend/migrations/env.py`
- 新增：`backend/migrations/versions/0005_event_lifecycle_outbox.py`
- 新增：`backend/tests/test_event_lifecycle_schema.py`

**接口：**
- 使用：现有 `Base`、事件表和迁移头 `0004_users`。
- 产出：
  - `raw_messages.provider`
  - `raw_messages.ingest_lane`
  - `earthquake_events.t1_at`
  - `earthquake_events.lifecycle_state`
  - `earthquake_events.latest_trigger_revision_id`
  - `earthquake_revisions.semantic_fingerprint`
  - `earthquake_revisions.provider`
  - `earthquake_revisions.ingest_lane`
  - `earthquake_revisions.ingested_at`
  - `earthquake_revisions.inside_shanghai`
  - `earthquake_revisions.distance_to_boundary_km`
  - `earthquake_revisions.region_boundary_version`
  - `earthquake_revisions.region_computed_at`
  - `EventLifecycleOutbox` ORM 模型及 `event_lifecycle_outbox` 表。

- [ ] **步骤 1：先编写会失败的数据结构测试**

```python
# backend/tests/test_event_lifecycle_schema.py
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


async def test_event_lifecycle_columns_and_outbox_exist() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        schema = await connection.run_sync(
            lambda sync: {
                table: {column["name"] for column in inspect(sync).get_columns(table)}
                for table in (
                    "raw_messages",
                    "earthquake_events",
                    "earthquake_revisions",
                    "event_lifecycle_outbox",
                )
            }
        )
    await engine.dispose()

    assert {"provider", "ingest_lane"} <= schema["raw_messages"]
    assert {"t1_at", "lifecycle_state", "latest_trigger_revision_id"} <= schema[
        "earthquake_events"
    ]
    assert {
        "semantic_fingerprint",
        "provider",
        "ingest_lane",
        "ingested_at",
        "inside_shanghai",
        "distance_to_boundary_km",
        "region_boundary_version",
        "region_computed_at",
    } <= schema["earthquake_revisions"]
    assert {
        "event_id",
        "revision_id",
        "trigger_type",
        "trigger_reason",
        "status",
    } <= schema["event_lifecycle_outbox"]
```

- [ ] **步骤 2：运行数据结构测试并确认失败**

运行：

```powershell
Set-Location backend
python -m pytest tests/test_event_lifecycle_schema.py -v
```

预期：失败，因为新表或字段尚不存在。

- [ ] **步骤 3：加入 ORM 字段**

扩展 `RawMessage`：

```python
provider: Mapped[str | None] = mapped_column(String(32), index=True)
ingest_lane: Mapped[str | None] = mapped_column(String(32), index=True)
```

扩展 `EarthquakeEvent`：

```python
t1_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
lifecycle_state: Mapped[str] = mapped_column(
    String(32),
    default="auto_pending",
    server_default=text("'auto_pending'"),
    index=True,
)
latest_trigger_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
```

扩展 `EarthquakeRevision`：

```python
semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
provider: Mapped[str | None] = mapped_column(String(32), index=True)
ingest_lane: Mapped[str | None] = mapped_column(String(32), index=True)
ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
inside_shanghai: Mapped[bool | None] = mapped_column(Boolean)
distance_to_boundary_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
region_boundary_version: Mapped[str | None] = mapped_column(String(64), index=True)
region_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

加入：

```python
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
```

- [ ] **步骤 4：新增迁移 `0005_event_lifecycle_outbox.py`**

迁移必须：

- 设置 `revision = "0005_event_lifecycle"`。
- 设置 `down_revision = "0004_users"`。
- 加入步骤 3 的全部字段。
- 加入部分唯一索引：

```python
op.create_index(
    "uq_earthquake_revisions_semantic_fingerprint",
    "earthquake_revisions",
    ["event_id", "semantic_fingerprint"],
    unique=True,
    postgresql_where=sa.text("semantic_fingerprint IS NOT NULL"),
)
```

- 创建 `event_lifecycle_outbox`，包含两个外键、唯一约束和待处理索引。
- `downgrade()` 按逆序删除 Outbox 表、索引和字段。

- [ ] **步骤 5：运行迁移与数据结构测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_migrations.py tests/test_event_lifecycle_schema.py -v
```

预期：迁移到达 `0005_event_lifecycle`，两个测试均通过。

- [ ] **步骤 6：提交**

```powershell
git add backend/app/events/models.py backend/migrations/env.py backend/migrations/versions/0005_event_lifecycle_outbox.py backend/tests/test_event_lifecycle_schema.py
git commit -m "feat: persist CENC lifecycle and assessment outbox"
```

## Task 4：采集运行状态与死信数据结构

**文件：**
- 新增：`backend/app/collector/models.py`
- 修改：`backend/migrations/env.py`
- 新增：`backend/migrations/versions/0006_collector_runtime.py`
- 新增：`backend/tests/test_collector_schema.py`

**接口：**
- 使用：`Base`、`0005_event_lifecycle`。
- 产出：
  - `CollectorRuntimeState` ORM 模型。
  - `CollectorDeadLetter` ORM 模型。
  - `collector_runtime_state`、`collector_dead_letters` 表。

- [ ] **步骤 1：先编写会失败的数据结构测试**

```python
# backend/tests/test_collector_schema.py
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


async def test_collector_runtime_tables_exist() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        schema = await connection.run_sync(
            lambda sync: {
                "tables": set(inspect(sync).get_table_names()),
                "columns": {
                    column["name"]
                    for column in inspect(sync).get_columns("collector_runtime_state")
                },
            }
        )
    await engine.dispose()

    assert {"collector_runtime_state", "collector_dead_letters"} <= schema["tables"]
    assert "last_processed_source_time" in schema["columns"]
```

- [ ] **步骤 2：运行数据结构测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collector_schema.py -v
```

预期：失败，因为两个表尚不存在。

- [ ] **步骤 3：加入精确的 ORM 模型**

```python
# backend/app/collector/models.py
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CollectorRuntimeState(Base):
    __tablename__ = "collector_runtime_state"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_processed_source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reconnect_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CollectorDeadLetter(Base):
    __tablename__ = "collector_dead_letters"
    __table_args__ = (Index("ix_collector_dead_letters_open", "status", "last_failed_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    lane: Mapped[str] = mapped_column(String(32), index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_category: Mapped[str] = mapped_column(String(64), index=True)
    error_message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
```

- [ ] **步骤 4：新增迁移 `0006_collector_runtime.py`**

使用 `revision = "0006_collector_runtime"`、`down_revision = "0005_event_lifecycle"`，按模型精确定义创建两个表。`downgrade()` 先删除 `collector_dead_letters`，再删除 `collector_runtime_state`。

在 `backend/migrations/env.py` 导入 `app.collector.models`。

- [ ] **步骤 5：运行迁移与数据结构测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_migrations.py tests/test_collector_schema.py -v
```

预期：迁移到达 `0006_collector_runtime`，全部测试通过。

- [ ] **步骤 6：提交**

```powershell
git add backend/app/collector/models.py backend/migrations/env.py backend/migrations/versions/0006_collector_runtime.py backend/tests/test_collector_schema.py
git commit -m "feat: persist collector runtime and dead letters"
```

## Task 5：版本化区域边界解析器

**文件：**
- 新增：`backend/app/regions/__init__.py`
- 新增：`backend/app/regions/domain.py`
- 新增：`backend/app/regions/models.py`
- 新增：`backend/app/regions/repository.py`
- 新增：`backend/app/regions/service.py`
- 新增：`backend/app/regions/importer.py`
- 新增：`backend/app/regions/cli.py`
- 修改：`backend/migrations/env.py`
- 新增：`backend/migrations/versions/0007_region_boundaries.py`
- 新增：`backend/tests/fixtures/shanghai_boundary.geojson`
- 新增：`backend/tests/test_region_resolver.py`

**接口：**
- 使用：PostgreSQL/PostGIS 和 `ResponseInput` 边界字段。
- 产出：
  - `app.regions.domain.RegionContext` 值对象。
  - `RegionBoundary` ORM 模型。
  - `RegionContext` 数据类：字段为 `inside_shanghai`、`distance_to_boundary_km`、`boundary_version`、`computed_at`。
  - `RegionRepository.get_active(session) -> RegionBoundary | None`。
  - `RegionContextResolver.resolve(longitude: Decimal, latitude: Decimal) -> RegionContext`。
  - CLI：`python -m app.regions.cli import --file <path> --version <version> --name <name> --activate`。

- [ ] **步骤 1：加入合成边界夹具**

```json
# backend/tests/fixtures/shanghai_boundary.geojson
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {"name": "test-shanghai"},
      "geometry": {
        "type": "Polygon",
        "coordinates": [[
          [120.8, 30.6],
          [122.2, 30.6],
          [122.2, 31.9],
          [120.8, 31.9],
          [120.8, 30.6]
        ]]
      }
    }
  ]
}
```

- [ ] **步骤 2：先编写会失败的解析器测试**

```python
# backend/tests/test_region_resolver.py
from decimal import Decimal

from sqlalchemy import delete

from app.db import SessionFactory
from app.regions.models import RegionBoundary
from app.regions.service import RegionContextResolver


class EmptyRepository:
    async def get_active(self, session):
        return None


class TestRepository:
    async def get_active(self, session):
        result = await session.execute(
            RegionBoundary.__table__.select().where(RegionBoundary.is_active)
        )
        return result.mappings().first()


async def test_resolver_resolves_inside_and_outside_with_real_postgis() -> None:
    repository = TestRepository()
    resolver = RegionContextResolver(SessionFactory, repository=repository)
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            from app.regions.importer import import_geojson

            await import_geojson(
                session,
                "/app/tests/fixtures/shanghai_boundary.geojson",
                version="test-2026.1",
                name="test-shanghai",
                activate=True,
            )

    inside = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))
    outside = await resolver.resolve(Decimal("122.9"), Decimal("31.2"))

    assert inside.inside_shanghai is True
    assert inside.boundary_version == "test-2026.1"
    assert outside.inside_shanghai is False
    assert outside.distance_to_boundary_km > 0


async def test_resolver_returns_pending_without_boundary() -> None:
    resolver = RegionContextResolver(
        SessionFactory,
        repository=EmptyRepository(),
    )

    result = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))

    assert result.inside_shanghai is None
    assert result.boundary_version is None
```

- [ ] **步骤 3：运行解析器测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_region_resolver.py -v
```

预期：失败，因为区域包和数据表尚不存在。

- [ ] **步骤 4：加入模型与迁移**

```python
# backend/app/regions/models.py
import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Numeric, String, UniqueConstraint, false, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RegionBoundary(Base):
    __tablename__ = "region_boundaries"
    __table_args__ = (UniqueConstraint("version", name="uq_region_boundaries_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    local_buffer_km: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("50"))
    geom = mapped_column(Geometry(geometry_type="MULTIPOLYGON", srid=4326), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(512))
    checksum: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
```

迁移 `0007_region_boundaries.py` 使用 `revision = "0007_region_boundaries"`、`down_revision = "0006_collector_runtime"`，创建数据表并为 `geom` 建立 GiST 索引。

- [ ] **步骤 5：实现仓储、解析器与导入器**

`RegionRepository` 必须暴露：

```python
async def get_active(self, session: AsyncSession) -> RegionBoundary | None: ...
async def activate(self, version: str) -> None: ...
```

`RegionContextResolver.resolve` 必须通过一次 PostGIS 查询完成：

```sql
SELECT
  version,
  ST_Covers(geom, point) AS inside_land,
  ST_DWithin(geom::geography, point::geography, local_buffer_km * 1000) AS in_local_buffer,
  ST_Distance(geom::geography, point::geography) / 1000.0 AS distance_km
FROM region_boundaries
WHERE is_active
```

其中 `point` 为 `ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)`。

返回：

```python
RegionContext(
    inside_shanghai=True if inside_land or in_local_buffer else False,
    distance_to_boundary_km=Decimal("0") if inside_land else Decimal(str(distance_km)),
    boundary_version=row.version,
    computed_at=datetime.now(UTC),
)
```

若不存在有效边界，返回 `inside_shanghai=None`、`distance_to_boundary_km=None`、`boundary_version=None`。

`importer.py` 必须：

- 使用 `json.loads` 读取 UTF-8 GeoJSON。
- 接受 Polygon 和 MultiPolygon 要素。
- 将所有多边形合并为一个 `MULTIPOLYGON` WKT。
- 拒绝空几何、超出 WGS84 范围的坐标以及多个同时启用的版本。
- 对原始文件字节计算 SHA-256 校验和。
- 通过 `ST_GeomFromText(:wkt, 4326)` 写入。

`cli.py` 必须支持导入和启用。输出只允许包含版本、要素数量、校验和和启用状态，不得输出文件中的敏感信息或原始几何。

- [ ] **步骤 6：运行迁移与聚焦测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_migrations.py tests/test_region_resolver.py -v
```

预期：迁移到达 `0007_region_boundaries`，解析器测试通过。

- [ ] **步骤 7：提交**

```powershell
git add backend/app/regions backend/migrations/env.py backend/migrations/versions/0007_region_boundaries.py backend/tests/fixtures/shanghai_boundary.geojson backend/tests/test_region_resolver.py
git commit -m "feat: add versioned region boundary resolver"
```

## Task 6：原子化生命周期入库与 Outbox 服务

**文件：**
- 修改：`backend/app/events/repository.py`
- 修改：`backend/app/events/service.py`
- 修改：`backend/app/events/router.py`
- 修改：`backend/app/events/schemas.py`
- 修改：`backend/tests/test_event_api.py`
- 修改：`backend/tests/test_event_service.py`
- 修改：`backend/tests/test_event_service_responses.py`
- 修改：`backend/tests/conftest.py`
- 新增：`backend/tests/test_collected_event_service.py`

**接口：**
- 使用：
  - `CollectorProvider`、`CollectorLane`
  - `semantic_fingerprint`、`message_family`、`classify_reviewed_kind`
  - `RegionContextResolver`
  - `app.regions.domain.RegionContext`
  - 现有响应规则引擎。
- 产出：
  - `EventIngestResult.is_new: bool`。
  - `LifecycleIngestOutcome`：字段为 `event_id`、`revision_id`、`revision_no`、`event_kind`、`lifecycle_state`、`is_current`、`is_new`、`triggered_assessment`、`institutional_level`、`service_level`、`t1_at`。
  - `EventService.ingest_collected(raw_payload, event, provider, lane, received_at, response_input, region_context, trigger_reason="live") -> LifecycleIngestOutcome`。

- [ ] **步骤 1：先编写会失败的服务测试**

```python
# backend/tests/test_collected_event_service.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.collector.domain import CollectorLane, CollectorProvider
from app.db import SessionFactory
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.events.sources.cenc import CencAdapter
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_lifecycle_data(session_factory):
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


def collected_event(
    *,
    kind: str = "formal",
    magnitude: str = "5.1",
    provider: str = "fan",
    lane: str = "websocket",
    report_time: str = "2026-09-25T01:04:00Z",
    report_number: int | None = None,
) -> dict[str, object]:
    event = NormalizedEvent(
        kind=EventKind(kind),
        source="cenc",
        source_event_id="CENC-2026-0001",
        origin_time=datetime(2026, 9, 25, 1, 2, 3, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="上海测试位置",
        report_time=datetime.fromisoformat(report_time.replace("Z", "+00:00")),
        report_number=report_number,
    )
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)
    region_context = RegionContext(
        inside_shanghai=True,
        distance_to_boundary_km=Decimal("0"),
        boundary_version="test-2026.1",
        computed_at=received_at,
    )
    return {
        "raw_payload": {
            "eventId": "CENC-2026-0001",
            "reportType": kind,
            "originTime": event.origin_time.isoformat(),
            "longitude": str(event.longitude),
            "latitude": str(event.latitude),
            "magnitude": str(event.magnitude),
            "depth": str(event.depth_km),
            "place": event.place,
        },
        "event": event,
        "provider": CollectorProvider(provider).value,
        "lane": CollectorLane(lane).value,
        "received_at": received_at,
        "response_input": ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        "region_context": region_context,
    }


def collected_service(session_factory=SessionFactory) -> EventService:
    return EventService(session_factory)


async def outbox_count(session_factory, event_id: str) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(EventLifecycleOutbox)
                .where(EventLifecycleOutbox.event_id == event_id)
            )
            or 0
        )


async def outbox_reason(session_factory, revision_id: str) -> str | None:
    async with session_factory() as session:
        return await session.scalar(
            select(EventLifecycleOutbox.trigger_reason).where(
                EventLifecycleOutbox.revision_id == revision_id
            )
        )


async def test_auto_then_formal_creates_one_event_and_one_outbox(session_factory) -> None:
    service = collected_service(session_factory)
    auto = collected_event(kind="auto", magnitude="4.8", report_time="2026-09-25T01:02:00Z")
    formal = collected_event(kind="formal", magnitude="5.1", report_time="2026-09-25T01:04:00Z")

    auto_result = await service.ingest_collected(**auto)
    formal_result = await service.ingest_collected(**formal)

    assert auto_result.triggered_assessment is False
    assert formal_result.triggered_assessment is True
    assert formal_result.event_id == auto_result.event_id
    assert formal_result.t1_at == formal["received_at"]
    assert await outbox_count(session_factory, auto_result.event_id) == 1


async def test_equivalent_fan_and_wolfx_reviewed_messages_do_not_duplicate(session_factory) -> None:
    service = collected_service(session_factory)
    fan_payload = {
        "id": "CENC-2026-0001",
        "infoTypeName": "正式(已核实)",
        "shockTime": "2026/09/25 09:02:03",
        "createTime": "2026/09/25 09:04:00",
        "placeName": "上海测试位置",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }
    wolfx_payload = {
        "type": "reviewed",
        "EventID": "CENC-2026-0001",
        "time": "2026/09/25 09:02:03",
        "ReportTime": "2026/09/25 09:04:00",
        "placeName": "上海测试位置",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }
    fan = {
        **collected_event(kind="formal", provider="fan", lane="websocket"),
        "event": CencAdapter().parse(fan_payload),
        "raw_payload": fan_payload,
    }
    wolfx = {
        **collected_event(kind="formal", provider="wolfx", lane="http"),
        "event": CencAdapter().parse(wolfx_payload),
        "raw_payload": wolfx_payload,
    }

    first = await service.ingest_collected(**fan)
    second = await service.ingest_collected(**wolfx)

    assert first.revision_id == second.revision_id
    assert first.is_new is True
    assert second.is_new is False
    assert await outbox_count(session_factory, first.event_id) == 1


async def test_changed_reviewed_message_creates_correction_and_second_outbox(session_factory) -> None:
    service = collected_service(session_factory)
    formal = collected_event(kind="formal", magnitude="5.1")
    correction = collected_event(
        kind="formal",
        magnitude="5.2",
        report_time="2026-09-25T01:06:00Z",
    )

    first = await service.ingest_collected(**formal)
    second = await service.ingest_collected(**correction)

    assert second.event_kind.value == "correction"
    assert second.t1_at == first.t1_at
    assert second.triggered_assessment is True
    assert await outbox_count(session_factory, first.event_id) == 2


async def test_recovery_trigger_reason_is_persisted(session_factory) -> None:
    service = collected_service(session_factory)
    result = await service.ingest_collected(
        **collected_event(kind="formal"),
        trigger_reason="recovery",
    )

    assert await outbox_reason(session_factory, result.revision_id) == "recovery"


async def test_late_older_reviewed_revision_is_stored_without_new_outbox(
    session_factory,
) -> None:
    service = collected_service(session_factory)
    current = collected_event(report_number=3)
    stale = collected_event(
        magnitude="5.0",
        report_number=2,
        report_time="2026-09-25T01:03:00Z",
    )

    first = await service.ingest_collected(**current)
    second = await service.ingest_collected(**stale)

    assert second.is_new is True
    assert second.is_current is False
    assert second.triggered_assessment is False
    assert await outbox_count(session_factory, first.event_id) == 1
```

该测试必须通过 `CencAdapter` 读取真实 FAN/Wolfx 正式报字段，而不是直接构造两个相同的 `NormalizedEvent`。它用于证明 `createTime` 与 `ReportTime` 映射后得到相同语义指纹；若真实协议未来字段变化，测试必须按上游报文夹具更新，不能用人工复制标准事件掩盖差异。

使用现有真实数据库会话工厂；若本机不能直连数据库，则通过已建立的 `postgres` Compose 服务运行测试。

在 `backend/tests/conftest.py` 加入：

```python
import pytest

from app.db import SessionFactory


@pytest.fixture
def session_factory():
    return SessionFactory
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collected_event_service.py -v
```

预期：失败，因为 `ingest_collected` 尚不存在。

- [ ] **步骤 3：扩展仓储追加语义**

修改 `EventIngestResult`：

```python
@dataclass(frozen=True, slots=True)
class EventIngestResult:
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: EventKind
    is_current: bool
    is_new: bool = True
```

修改 `get_or_create_raw_message` 签名：

```python
async def get_or_create_raw_message(
    self,
    session: AsyncSession,
    payload: dict[str, object],
    event: NormalizedEvent,
    received_at: datetime,
    *,
    provider: str,
    ingest_lane: str,
) -> RawMessage:
```

创建新 `RawMessage` 时必须写入 `provider` 和 `ingest_lane`。若相同校验和报文已存在，则直接复用现有记录，不覆盖第一次实际接收的提供方和链路元数据。

修改 `append_revision` 签名：

```python
async def append_revision(
    self,
    session: AsyncSession,
    raw: RawMessage,
    event: NormalizedEvent,
    *,
    semantic_fingerprint: str | None,
    provider: str,
    ingest_lane: str,
    ingested_at: datetime,
    region_context: RegionContext | None,
) -> EventIngestResult:
```

规则：

1. 已有原始修订直接返回 `is_new=False`。
2. 在现有按来源加锁范围内定位或创建规范事件。
3. 查询规范事件已有修订。
4. 若已存在相同 `semantic_fingerprint` 的修订，返回该修订并设置 `is_new=False`。
5. 正式输入且尚无正式修订时，使用 `EventKind.FORMAL`。
6. 正式输入且已有正式修订时，使用 `EventKind.CORRECTION`。
7. 自动速报始终保留为 `EventKind.AUTO`。
8. 持久化 `semantic_fingerprint`、`provider`、`ingest_lane`、`ingested_at`。CENC 的自动、正式、修订报文必须提供非空指纹；人工、测试、演练事件传 `None`。
9. 将区域上下文保存到修订的 `inside_shanghai`、`distance_to_boundary_km`、`region_boundary_version`、`region_computed_at` 字段。
10. 首次正式修订提交时设置 `event.t1_at = ingested_at`。
11. 修订不得重置 `t1_at`。
12. 仅当新修订为当前修订时，将生命周期状态设置为 `auto_pending`、`formal_triggered` 或 `correction_triggered`；非当前旧修订不得覆盖当前生命周期。
13. 仅在调用方为新且当前的正式修订写入 Outbox 时设置 `latest_trigger_revision_id`。

现有 `_becomes_current` 逻辑仍是当前修订选择规则的唯一依据。

- [ ] **步骤 4：加入原子化 Outbox 写入**

实现：

```python
async def enqueue_assessment(
    self,
    session: AsyncSession,
    *,
    event_id: object,
    revision_id: object,
    revision_no: int,
    trigger_reason: str,
    created_at: datetime,
) -> bool:
    existing = await session.scalar(
        select(EventLifecycleOutbox.id).where(
            EventLifecycleOutbox.event_id == event_id,
            EventLifecycleOutbox.revision_id == revision_id,
            EventLifecycleOutbox.trigger_type == "assessment.requested",
        )
    )
    if existing is not None:
        return False

    event = await session.get(EarthquakeEvent, event_id, with_for_update=True)
    if event is None:
        raise LookupError(f"event not found: {event_id}")

    session.add(
        EventLifecycleOutbox(
            event_id=event_id,
            revision_id=revision_id,
            trigger_type="assessment.requested",
            trigger_reason=trigger_reason,
            payload={
                "event_id": str(event_id),
                "revision_id": str(revision_id),
                "revision_no": revision_no,
            },
            status="pending",
            attempt_count=0,
            created_at=created_at,
            available_at=created_at,
        )
    )
    event.latest_trigger_revision_id = revision_id
    await session.flush()
    return True
```

加入必要的 `EventLifecycleOutbox` 和 `EarthquakeEvent` 导入。

- [ ] **步骤 5：加入 `EventService.ingest_collected`**

在同一个事务中实现：

```python
async def ingest_collected(
    self,
    raw_payload: dict[str, object],
    event: NormalizedEvent,
    provider: str,
    lane: str,
    received_at: datetime,
    response_input: ResponseInput | None,
    region_context: RegionContext | None,
    trigger_reason: str = "live",
) -> LifecycleIngestOutcome:
```

必须满足：

- 校验 `raw_payload` 和 `received_at`。
- 允许 provider 为 `fan`、`wolfx`、`api`，lane 为 `websocket`、`http`；`fan` 只配 `websocket`，`wolfx` 和 `api` 只配 `http`。
- 获取现有按来源粒度的入库锁。
- 调用 `get_or_create_raw_message` 时传入 provider 和 lane，确保新原始报文保存实际审计元数据。
- 追加修订前计算报文族和语义指纹。
- 调用 `append_revision`。
- 新且当前的正式报或修订，才计算响应建议，并使用现有仓储方法写入修订和事件。
- 仅当 `result.is_new and result.is_current` 且类型为 `FORMAL` 或 `CORRECTION` 时，精确调用一次 `enqueue_assessment`；乱序旧修订只保存审计记录，不触发评估。
- 在一个事务中提交全部变更。
- 返回已持久化的当前响应等级、`lifecycle_state`、`T1`、`is_new`、`triggered_assessment`。

`trigger_reason` 仅允许 `live` 或 `recovery`，其他值必须拒绝。

- [ ] **步骤 6：保持现有公开方法兼容**

`EventService.ingest` 和 `EventService.ingest_with_response_suggestion` 继续服务于现有兼容 API，可委托给共享私有方法。自动报兼容接口不得写 Outbox；正式报和修订兼容接口必须调用 `ingest_collected`，provider 使用 `"api"`、lane 使用 `"http"`、`trigger_reason="recovery"`，因此受控补录和故障恢复会按正式生命周期触发评估。兼容接口若有 `regionContext`，路由器将其转换为 `app.regions.domain.RegionContext`，`boundary_version` 为 `None`、`computed_at` 为当前 UTC 时刻；没有上下文时传 `None`。人工入口 `/api/v1/events/manual` 继续调用不写 Outbox 的兼容路径；小震正式报的手工评估触发在后续评估编排子系统实现时接入统一触发接口。兼容 API 调用可写路径时，`ingested_at` 使用规范化后的接收时间；仅当事件类型为 `AUTO`、`FORMAL`、`CORRECTION` 时计算 `message_family` 和语义指纹，`MANUAL`、`TEST`、`DRILL` 的 `semantic_fingerprint` 必须为 `None`。

扩展 `backend/app/events/schemas.py.EventIngestResponse` 与 `_ingest_outcome_response`，使正式报和修订兼容接口返回 `lifecycle_state` 和 `t1_at`。`lifecycle_state` 必须与落库后的当前状态一致，`t1_at` 返回 ISO 时间字符串或 `null`；该契约由 Task 12 的真实 API E2E 测试验证。

更新现有仓储测试调用点，显式传入 `provider`、`ingest_lane`、`ingested_at`、`semantic_fingerprint`；兼容 API 测试中 provider 使用 `"api"`，lane 使用 `"http"`。

- [ ] **步骤 7：运行聚焦测试与回归测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collected_event_service.py tests/test_event_service.py tests/test_event_service_responses.py tests/test_event_api.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

预期：全部测试通过，现有 API 行为保持不变。

- [ ] **步骤 8：提交**

```powershell
git add backend/app/events/repository.py backend/app/events/service.py backend/app/events/router.py backend/tests/test_collected_event_service.py backend/tests/test_event_service.py backend/tests/test_event_service_responses.py backend/tests/test_event_api.py backend/tests/conftest.py
git commit -m "feat: atomically ingest collected events and triggers"
```

## Task 7：FAN Studio WebSocket 采集器

**文件：**
- 修改：`backend/pyproject.toml`
- 新增：`backend/app/collector/fan.py`
- 新增：`backend/tests/fixtures/fan_cenc.json`
- 新增：`backend/tests/test_fan_collector.py`

**接口：**
- 使用：
  - `CollectorEnvelope`
  - `CollectorProvider.FAN`
  - `CollectorLane.WEBSOCKET`
  - 任务 1 的 FAN 配置。
- 产出：
  - `FanMessageParser.parse(message: dict[str, object], received_at: datetime) -> FanParseResult`.
  - `FanParseResult.envelope`、`FanParseResult.auth_state`、`FanParseResult.heartbeat`。
  - `FanCollector.run(on_envelope, on_health, stop_event=None)`；`stop_event` 为 `asyncio.Event | None`，为空时在内部创建。

- [ ] **步骤 1：加入 `websockets` 依赖**

向 `backend/pyproject.toml` 加入：

```toml
"websockets==15.0.1",
```

- [ ] **步骤 2：先编写会失败的解析测试**

```python
# backend/tests/test_fan_collector.py
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.collector.domain import CollectorLane, CollectorProvider
from app.collector.fan import FanMessageParser


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "fan_cenc.json").read_text(encoding="utf-8")
)


def test_parse_fan_cenc_initial_list() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["initial"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert parsed.envelope.provider is CollectorProvider.FAN
    assert parsed.envelope.lane is CollectorLane.WEBSOCKET
    first = parsed.envelope.payload["No1"]
    assert first["eventId"] == "CENC-2026-0001"
    assert first["infoTypeName"] == "正式(已核实)"


def test_parse_fan_cenc_query_response() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["query_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert parsed.envelope.payload["No1"]["eventId"] == "CENC-2026-0002"


def test_parse_fan_cenclist_response_normalizes_history_keys() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["cenclist_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert list(parsed.envelope.payload) == ["No1", "No2"]
    assert {
        item["eventId"] for item in parsed.envelope.payload.values()
    } == {"CENC-2026-0001", "CENC-2026-0002"}


def test_parse_fan_auth_success() -> None:
    parsed = FanMessageParser().parse(
        {"type": "auth_success"},
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is None
    assert parsed.auth_state == "success"


def test_parse_fan_auth_failure_does_not_include_key() -> None:
    parsed = FanMessageParser().parse(
        {"type": "auth_fail", "message": "invalid key"},
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is None
    assert parsed.auth_state == "failed"
    assert "key" not in parsed.error_text.lower()
```

夹具必须同时包含 `initial_all`、`query_response`、`cenclist_response` 和 `update` 消息，且不得包含任何凭据字段。`initial_all` 与 `query_response` 的业务数据位于顶层来源键 `cenc.Data`；`cenclist_response.Data` 按公开 Kanameishi 实现保存对象映射，计划夹具使用两个非 `NoN` 原始键，用于验证解析器按 `shockTime`、`id` 稳定排序后规范化为 `No1`、`No2`。

- [ ] **步骤 3：运行解析测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_fan_collector.py -v
```

预期：失败，因为 FAN 采集器模块尚不存在。

- [ ] **步骤 4：实现解析器**

`FanMessageParser.parse` 必须：

- 接受 `initial_all`、`query_response`、`cenclist_response`、`update`、`auth_success`、`auth_fail`。
- 对 `initial_all` 和 `query_response`，从顶层来源键提取 `message["cenc"]["Data"]` 并返回 `CollectorEnvelope`；不得按 `message["Data"]["cenc"]` 解析。
- 对 `cenclist_response`，提取 `Data` 对象映射，按 `shockTime` 后 `id` 稳定排序，规范化为 `{"No1": ..., "No2": ...}` 后返回单个 `CollectorEnvelope`，供协调器统一展开。
- 对 `update`，仅当 `source == "cenc"` 时提取 `Data`，并返回包含 `{"No1": Data}` 的 `CollectorEnvelope`。
- 对 `auth_success`，返回 `FanParseResult(auth_state="success")`。
- 对 `auth_fail`，返回 `FanParseResult(auth_state="failed", error_text="authentication failed")`。
- 忽略无关消息类型和来源，不得将其记录为错误。
- `appId`、`key`、原始认证 JSON 不得进入 envelope 或错误文本。

- [ ] **步骤 5：先编写会失败的重连测试**

```python
async def fake_sleep(delay: float) -> None:
    return None


async def test_fan_collector_rotates_urls_after_connection_failure() -> None:
    attempts: list[str] = []

    async def fake_connect(url: str):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("primary unavailable")
        raise asyncio.CancelledError

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary", "wss://backup"),
        query_interval_seconds=1,
        connect=fake_connect,
        sleep=fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await collector.run(on_envelope=AsyncMock(), on_health=AsyncMock())

    assert attempts == ["wss://primary", "wss://backup"]
```

使用可控注入的 `connect` 和 `sleep`，不得真实等待。

- [ ] **步骤 6：实现 `FanCollector`**

`run` 必须：

1. 发出 `starting`。
2. 连接当前 URL。
3. 发送：

```json
{"type":"auth","appId":"<resolved app id>","key":"<secret>"}
```

4. 发送 `cenclist`，并处理 `cenclist_response` 历史补漏结果。
5. 每隔 `fan_query_interval_seconds` 发送 `query`。
6. 通过 `FanMessageParser` 解析消息。
7. 使用真实接收时间发出每个 envelope。
8. 认证失败时发出 `degraded`，按配置退避后轮换到备用地址，且不得记录凭据。
9. 断线时发出 `degraded`，使用封顶指数退避，轮换 URL 后重连。
10. 认证成功且收到业务 envelope 时发出 `healthy`，更新 `last_http_status=None`、`last_connected_at`、`last_message_at`、`last_success_at`。
11. `stop_event` 置位时干净停止并发出 `stopped`。

实现带抖动且最大 30 秒的延迟辅助函数。

- [ ] **步骤 7：运行 FAN 测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_fan_collector.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/collector/fan.py tests/test_fan_collector.py
```

预期：全部测试通过，Ruff 无错误。

- [ ] **步骤 8：提交**

```powershell
git add backend/pyproject.toml backend/app/collector/fan.py backend/tests/fixtures/fan_cenc.json backend/tests/test_fan_collector.py
git commit -m "feat: collect CENC data from FAN websocket"
```

## Task 8：Wolfx HTTP 备用采集器

**文件：**
- 新增：`backend/app/collector/wolfx.py`
- 新增：`backend/tests/fixtures/wolfx_cenc_eqlist.json`
- 新增：`backend/tests/test_wolfx_collector.py`

**接口：**
- 使用：
  - `CollectorEnvelope`
  - `CollectorProvider.WOLFX`
  - `CollectorLane.HTTP`
  - 注入的 `httpx.AsyncClient`。
- 产出：
  - `WolfxMessageParser.parse(payload: dict[str, object], received_at: datetime) -> list[CollectorEnvelope]`.
  - `WolfxCollector.poll_once() -> list[CollectorEnvelope]`.
  - `WolfxCollector.run(on_envelope, on_health, stop_event=None)`；`stop_event` 为 `asyncio.Event | None`，为空时在内部创建。

- [ ] **步骤 1：加入 Wolfx 夹具**

夹具必须包含 `No1` 自动报和 `No2` 正式报对象，且每个对象包含 `CencAdapter` 所需全部字段：

- `EventID`
- `type`
- `time`
- `ReportTime`
- `placeName`
- `magnitude`
- `depth`
- `latitude`
- `longitude`

- [ ] **步骤 2：先编写会失败的解析与轮询测试**

```python
# backend/tests/test_wolfx_collector.py
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.collector.domain import CollectorLane, CollectorProvider
from app.collector.wolfx import WolfxCollector, WolfxMessageParser


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "wolfx_cenc_eqlist.json").read_text(
        encoding="utf-8"
    )
)


def test_wolfx_parser_emits_only_cenc_automatic_and_reviewed() -> None:
    payload = {
        **FIXTURE,
        "No3": {**FIXTURE["No2"], "type": "cancelled"},
    }

    result = WolfxMessageParser().parse(
        payload,
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert [item.provider for item in result] == [
        CollectorProvider.WOLFX,
        CollectorProvider.WOLFX,
    ]
    assert [item.lane for item in result] == [CollectorLane.HTTP, CollectorLane.HTTP]


async def test_poll_once_returns_502_as_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await collector.poll_once()

    await client.aclose()
```

- [ ] **步骤 3：运行测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_wolfx_collector.py -v
```

预期：失败，因为 Wolfx 采集器模块尚不存在。

- [ ] **步骤 4：实现解析器与轮询器**

`WolfxMessageParser.parse` 必须：

- 按数值顺序遍历匹配 `^No\d+$` 的键。
- 跳过非映射值。
- 只接受 `type` 为 `automatic` 或 `reviewed` 的值。
- 按数值顺序，每个有效事件返回一个 envelope。
- 使用原键保留事件对象，例如 `{"No1": item}`，以便 `CencAdapter` 解析。

`WolfxCollector.poll_once` 必须：

- 以 10 秒超时 GET 配置 URL。
- 调用 `raise_for_status()`。
- JSON 响应不是对象时抛出 `ValueError`。
- 将响应接收时间传给解析器。

`run` 必须：

- 立即轮询，之后按配置间隔持续轮询。
- 分别处理 `httpx.HTTPError`、超时、JSON 格式错误和非对象响应。
- 失败时发出 `degraded` 并累计连续失败次数。
- 失败后使用带抖动、最大 30 秒的指数退避，避免故障期间高频请求。
- HTTP 成功响应即使没有新事件，也发出 `healthy`，更新 `last_http_status`、`last_connected_at` 并清零失败计数。
- 每次健康更新都必须提供 `last_http_status` 和 `last_connected_at`；WebSocket 链路前者固定为 `None`。
- `stop_event` 置位时干净停止。

- [ ] **步骤 5：运行 Wolfx 测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_wolfx_collector.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/collector/wolfx.py tests/test_wolfx_collector.py
```

预期：全部测试通过，Ruff 无错误。

- [ ] **步骤 6：提交**

```powershell
git add backend/app/collector/wolfx.py backend/tests/fixtures/wolfx_cenc_eqlist.json backend/tests/test_wolfx_collector.py
git commit -m "feat: add Wolfx CENC backup collector"
```

## Task 9：采集协调器、监督器与容器

**文件：**
- 新增：`backend/app/collector/coordinator.py`
- 新增：`backend/app/collector/service.py`
- 新增：`backend/app/collector/spool.py`
- 新增：`backend/app/collector/supervisor.py`
- 新增：`backend/app/collector/main.py`
- 新增：`backend/tests/test_collector_supervisor.py`
- 新增：`backend/tests/test_collector_spool.py`
- 修改：`infra/compose.yaml`
- 修改：`backend/Dockerfile`

**接口：**
- 使用：
  - `FanCollector`
  - `WolfxCollector`
  - `EventService.ingest_collected`
  - `RegionContextResolver`
  - `CollectorRuntimeState`、`CollectorDeadLetter`。
- 产出：
  - `CollectorCoordinator.expand(envelope) -> list[CollectorEnvelope]`。
  - `CollectorCoordinator.ingest(envelope, trigger_reason="live") -> LifecycleIngestOutcome | None`。
  - `CollectorService.persist_health(update) -> None`.
  - `CollectorService.record_dead_letter(...) -> None`.
  - `CollectorService.get_last_processed_source_time(provider) -> datetime | None`。
  - `CollectorService.update_after_success(provider, ingested_at, source_time) -> None`。
  - `CollectorService.get_last_ingested_event_id() -> str | None`。
  - `CollectorService.load_dead_letter(dead_letter_id) -> dict[str, object] | None`。
  - `CollectorService.mark_dead_letter(dead_letter_id, status, error=None) -> None`。
  - `CollectorSpool.append(envelope) -> Path`、`CollectorSpool.iter_pending() -> Iterator[tuple[Path, CollectorEnvelope]]`、`CollectorSpool.remove(path) -> None`。
  - `CollectorSupervisor.run(stop_event=None) -> None`。
  - 可执行模块 `python -m app.collector.main`。

- [ ] **步骤 1：先编写会失败的协调器测试**

```python
# backend/tests/test_collector_supervisor.py
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.collector.supervisor import CollectorSupervisor
from app.events.domain import EventKind
from app.events.repository import LifecycleIngestOutcome
from app.regions.domain import RegionContext


class RecordingRegionResolver:
    def __init__(self, inside: bool | None = True, distance: str | None = "0") -> None:
        self._context = RegionContext(
            inside_shanghai=inside,
            distance_to_boundary_km=Decimal(distance) if distance is not None else None,
            boundary_version="test-2026.1",
            computed_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        )

    async def resolve(self, longitude, latitude) -> RegionContext:
        return self._context


class RecordingEventService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest_collected(self, **kwargs) -> LifecycleIngestOutcome:
        self.calls.append(kwargs)
        event = kwargs["event"]
        return LifecycleIngestOutcome(
            event_id="event-1",
            revision_id="revision-1",
            revision_no=1,
            event_kind=event.kind,
            lifecycle_state=(
                "auto_pending"
                if event.kind is EventKind.AUTO
                else "formal_triggered"
            ),
            is_current=True,
            is_new=True,
            triggered_assessment=event.kind is not EventKind.AUTO,
            institutional_level="较大响应",
            service_level=2,
            t1_at=kwargs["received_at"],
        )


class RecordingCoordinator:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[CollectorEnvelope] = []
        self.fail_with = fail_with

    def expand(self, envelope: CollectorEnvelope) -> list[CollectorEnvelope]:
        return [envelope]

    async def ingest(self, envelope: CollectorEnvelope, trigger_reason: str = "live"):
        self.calls.append(envelope)
        if self.fail_with is not None:
            raise self.fail_with
        return None


class RecordingCollectorService:
    def __init__(self) -> None:
        self.dead_letters: list[dict[str, object]] = []
        self.last_processed_source_time = None

    async def get_last_processed_source_time(self, provider: str):
        return self.last_processed_source_time

    async def update_after_success(self, provider: str, ingested_at, source_time) -> None:
        self.last_processed_source_time = source_time

    async def record_dead_letter(self, **kwargs) -> None:
        self.dead_letters.append(kwargs)

    async def record_spool_overflow(self, **kwargs) -> None:
        self.dead_letters.append({**kwargs, "error_category": "internal_error"})


class RecordingSpool:
    def __init__(self) -> None:
        self.pending: list[tuple[Path, CollectorEnvelope]] = []

    def append(self, envelope: CollectorEnvelope) -> Path:
        path = Path(f"spool-{len(self.pending) + 1}")
        self.pending.append((path, envelope))
        return path

    def iter_pending(self):
        yield from self.pending

    def remove(self, path: Path) -> None:
        self.pending = [
            item for item in self.pending if item[0] != path
        ]


class NoopFanCollector:
    async def run(self, **kwargs) -> None:
        return None


class NoopWolfxCollector:
    async def run(self, **kwargs) -> None:
        return None


def collector_settings():
    from app.config import Settings
    from pydantic import SecretStr

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        jwt_secret=SecretStr("jwt-secret-at-least-16-characters"),
        superadmin_initial_password=SecretStr("admin-secret-at-least-16-characters"),
        cenc_collector_enabled=False,
    )


def reviewed_wolfx_event() -> dict[str, object]:
    return {
        "EventID": "CENC-1",
        "type": "reviewed",
        "time": "2026-09-25T01:02:03Z",
        "placeName": "上海测试位置",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }


def envelope(report_time: str = "2026-09-25T01:04:00Z") -> CollectorEnvelope:
    return CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {**reviewed_wolfx_event(), "ReportTime": report_time}},
    )

async def test_coordinator_uses_region_context_and_trigger_reason() -> None:
    event_service = RecordingEventService()
    regions = RecordingRegionResolver(inside=True, distance="0")
    coordinator = CollectorCoordinator(event_service, regions)
    envelope = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": reviewed_wolfx_event()},
    )

    result = await coordinator.ingest(envelope, trigger_reason="recovery")

    assert result is not None
    call = event_service.calls[0]
    assert call["provider"] == "fan"
    assert call["lane"] == "websocket"
    assert call["response_input"].inside_shanghai is True
    assert call["region_context"].boundary_version == "test-2026.1"
    assert call["trigger_reason"] == "recovery"


async def test_coordinator_skips_cancellation_without_dead_letter() -> None:
    coordinator = CollectorCoordinator(RecordingEventService(), RecordingRegionResolver())

    result = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {**reviewed_wolfx_event(), "infoTypeName": "取消"}},
        )
    )

    assert result is None


def test_coordinator_expands_fan_matrix_in_numeric_order() -> None:
    coordinator = CollectorCoordinator(RecordingEventService(), RecordingRegionResolver())
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)
    envelope = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=received_at,
        payload={
            "No2": {**reviewed_wolfx_event(), "EventID": "CENC-2"},
            "No1": {**reviewed_wolfx_event(), "EventID": "CENC-1"},
        },
    )

    expanded = coordinator.expand(envelope)

    assert [next(iter(item.payload)) for item in expanded] == ["No1", "No2"]
    assert [item.received_at for item in expanded] == [received_at, received_at]
    assert [item.provider for item in expanded] == [
        CollectorProvider.FAN,
        CollectorProvider.FAN,
    ]
```

- [ ] **步骤 2：先编写会失败的监督器与恢复测试**

```python
async def test_supervisor_orders_batch_by_source_report_time() -> None:
    coordinator = RecordingCoordinator()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=RecordingCollectorService(),
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
    )

    await supervisor.process_envelopes(
        [
            envelope(report_time="2026-09-25T01:06:00Z"),
            envelope(report_time="2026-09-25T01:04:00Z"),
        ],
        trigger_reason="recovery",
    )

    assert [
        call.payload["No1"]["ReportTime"] for call in coordinator.calls
    ] == [
        "2026-09-25T01:04:00Z",
        "2026-09-25T01:06:00Z",
    ]


async def test_malformed_message_becomes_dead_letter() -> None:
    service = RecordingCollectorService()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(fail_with=ValueError("invalid CENC depth")),
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
    )

    outcome = await supervisor.process_one(envelope())

    assert outcome == "dead_letter"
    assert service.dead_letters[0]["error_category"] == "parse_error"


async def test_recovery_spool_is_not_filtered_by_newer_watermark() -> None:
    service = RecordingCollectorService()
    service.last_processed_source_time = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
    coordinator = RecordingCoordinator()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
    )
    older = envelope(report_time="2026-09-25T01:04:00Z")

    await supervisor.process_envelopes([older], trigger_reason="recovery")

    assert coordinator.calls == [older]
```

- [ ] **步骤 3：运行监督器测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collector_supervisor.py tests/test_collector_spool.py -v
```

预期：失败，因为协调器和监督器尚不存在。

- [ ] **步骤 4：实现协调器**

`CollectorCoordinator.expand` 必须：

1. 校验 provider/lane 组合：
   - FAN 只能使用 `websocket`。
   - Wolfx 只能使用 `http`。
2. FAN 初始全量或查询响应包含 `cenc` 矩阵时，按 `NoN` 键数值顺序为每个事件生成独立、复制原接收时间和 provider/lane 的单事件 envelope。
3. Wolfx payload 已经是 `{"NoN": item}` 时，原样返回一个 envelope。
4. 空矩阵返回空列表。

`CollectorCoordinator.ingest` 必须只接收 `expand` 返回的单事件 envelope，并：

1. 使用 `CancellationIgnored` 拒绝取消报文。
2. 使用 `CencAdapter` 解析。
3. 要求 `event.source == "cenc"`。
4. 使用事件坐标解析区域上下文。
5. 构造 `ResponseInput`：

```python
ResponseInput(
    magnitude=event.magnitude,
    depth_km=event.depth_km,
    inside_shanghai=region.inside_shanghai,
    distance_to_boundary_km=region.distance_to_boundary_km,
    deaths=None,
    max_intensity=None,
)
```

6. 使用原始 payload、事件、`provider.value`、`lane.value`、接收时间、区域输入、`region_context` 和触发原因调用 `ingest_collected`。

- [ ] **步骤 5：实现持久化服务**

`CollectorService` 封装数据库会话并：

- 使用 PostgreSQL `insert(...).on_conflict_do_update(...)` 更新健康状态。
- 只更新对应 provider 的行。
- 首次失败时写入死信并设置 `first_failed_at == last_failed_at`。
- 提供 `get_last_processed_source_time`、`update_after_success`，分别读写数据库中的来源时间水位和入库时刻；两个时间域不得混用。
- 提供 `get_last_ingested_event_id`，查询 `earthquake_revisions.ingested_at IS NOT NULL` 的记录，按 `ingested_at DESC, revision_no DESC` 取最近成功入库修订并返回 `event_id`。
- 提供 `load_dead_letter(dead_letter_id) -> dict[str, object] | None`，返回原始 payload、provider、lane、received_at 和 status；不存在时抛出 `LookupError`。
- 提供 `mark_dead_letter(dead_letter_id, status, error=None) -> None`，只允许状态 `open`、`retried`、`resolved`。
- `error_category` 规则：
  - `TypeError`、`ValueError`、`CancellationIgnored` 记为 `parse_error`。
  - `SQLAlchemyError` 记为 `storage_error`。
  - 其他未预期异常记为 `internal_error`。
- `error_message` 截断到 2,000 字符，且不得序列化配置对象或认证消息。

- [ ] **步骤 6：实现监督器**

`CollectorSupervisor.run` 必须：

1. 将两个 provider 状态初始化为 `starting`。
2. 使用一个有界 `asyncio.Queue(maxsize=2_000)`。
3. 启动 FAN 和 Wolfx，回调只将 envelope 放入队列，不得长期阻塞网络任务。
4. 每次处理一个队列项。
5. 每个 envelope 先经 `CollectorCoordinator.expand` 展开；显式恢复批次展开后按 `report_time`、`origin_time`、provider 优先级（FAN 在 Wolfx 前）排序。
6. 正常队列项使用 `trigger_reason="live"`。
7. 启动补录用 `trigger_reason="recovery"`。
8. 首次启动设置 cutoff 为 `now - CENC_BOOTSTRAP_LOOKBACK_HOURS`，跳过更早消息。
9. 后续启动读取 `collector_runtime_state.last_processed_source_time`，仅把它作为 FAN/Wolfx 上游恢复查询和补漏窗口的下界；不得用它过滤 live envelope、spool envelope 或死信重放。来源时间等于水位的消息仍需经语义指纹去重，来源时间早于水位的 spool/死信报文仍必须进入生命周期服务，由“新且当前”规则决定是否保存和触发。
10. 入库成功后同时更新 `last_success_at` 为当前入库时刻，并将 `last_processed_source_time` 推进到已成功处理消息的最大来源时间。
11. 解析失败写入死信后继续。
12. 存储失败按 1、2、4 秒重试三次；仍失败时先把原始 envelope 原子写入本地 spool，再继续处理后续消息。
13. 队列溢出时先同步写 spool；spool 写入成功则继续，写入失败或达到容量上限时把状态标记为 `critical` 并停止 supervisor，禁止静默丢弃或假报成功。
14. 启动时先按 `received_at` 升序重放 spool 中的报文，全部排空后才启动 live 接收；成功一条删除一条，数据库仍不可用时停止重放并保留全部文件。
15. 存储失败写入 spool 后，数据库恢复后的下一轮处理必须先排空 spool，再处理新的 live 队列项，确保 A 写 spool、C 已成功入库时，A 仍会作为历史修订保存且不会因水位被过滤。
16. 关闭时取消子任务、关闭连接或客户端，并将两个 provider 标记为 `stopped`。

检查点比较仅用于上游恢复查询，优先使用 `source_report_time`，缺失时回退到 `origin_time`；不得作为 `CollectorSupervisor.process_one` 的业务过滤条件。

`CollectorSpool` 使用每文件一个 envelope 的 JSON 格式，文件名包含接收时间和 UUID；写入流程为同目录临时文件、`flush()`、`os.fsync()`、`os.replace()`、父目录 `fsync()`。spool 只保存业务报文，不保存认证消息或凭据。

- [ ] **步骤 6.1：先编写会失败的 spool 测试**

```python
# backend/tests/test_collector_spool.py
from datetime import UTC, datetime

from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.collector.spool import CollectorSpool


def test_spool_round_trip_preserves_receipt_time(tmp_path) -> None:
    envelope = CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {"EventID": "CENC-1"}},
    )
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)

    spool.append(envelope)
    path, restored = next(spool.iter_pending())

    assert restored == envelope
    spool.remove(path)
    assert list(spool.iter_pending()) == []


def test_spool_rejects_write_over_capacity(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=1)

    try:
        spool.append(
            CollectorEnvelope(
                provider=CollectorProvider.WOLFX,
                lane=CollectorLane.HTTP,
                received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
                payload={"No1": {"EventID": "CENC-1"}},
            )
        )
    except OverflowError:
        pass
    else:
        raise AssertionError("spool must reject writes beyond capacity")
```

步骤 6.1 的测试必须先失败，再实现 `CollectorSpool` 并通过。

- [ ] **步骤 7：加入可执行入口**

`backend/app/collector/main.py` 必须：

- 配置结构化日志。
- 创建 `stop_event`。
- 在支持的平台安装 SIGINT、SIGTERM 信号处理。
- 构造 settings、`CollectorService`、`CollectorSpool`、`CollectorCoordinator`、`FanCollector`、`WolfxCollector`、`CollectorSupervisor`。
- 若 `settings.cenc_collector_enabled` 为 false，则带明确错误信息非零退出，避免 collector 容器静默空转。
- 持续运行直到 stop event 置位。
- 启动配置失败时非零退出。
- 在 `0.0.0.0:8100` 暴露仅容器内可访问的健康服务。
- 仅当至少一个 provider 为 `healthy` 或 `degraded` 时，`/healthz` 返回 HTTP 200；两者均为 `critical` 时返回 503。

本地健康响应只包含状态名称和时间戳，不得包含凭据。

- [ ] **步骤 8：加入 Compose 服务**

加入：

```yaml
  collector:
    build:
      context: ../backend
    env_file:
      - ../.env
    environment:
      CENC_COLLECTOR_ENABLED: "true"
      FAN_API_KEY: "${FAN_API_KEY:-}"
      FAN_APP_ID: "${FAN_APP_ID:-}"
      CENC_APP_ID: "${CENC_APP_ID:-}"
      COLLECTOR_SPOOL_DIR: "/var/lib/collector-spool"
    command: ["python", "-m", "app.collector.main"]
    volumes:
      - ../backend:/app
      - ../config:/config
      - collector-spool:/var/lib/collector-spool
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=3)"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s

volumes:
  collector-spool:
```

Compose 只注入原始环境值，`FAN_APP_ID` 与兼容别名 `CENC_APP_ID` 的必填关系和校验统一由后端 `Settings` 负责。不得在 Compose 中使用 `:?` 必填插值，否则即使只启动 API 也会全局失败。collector 不得向宿主机发布 8100 端口。

- [ ] **步骤 9：运行聚焦测试与 Compose 校验**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collector_supervisor.py -v
docker compose --env-file .env -f infra/compose.yaml config
docker compose --env-file .env -f infra/compose.yaml build collector
```

预期：测试通过，Compose 配置可解析，collector 镜像构建成功。

- [ ] **步骤 10：提交**

```powershell
git add backend/app/collector/coordinator.py backend/app/collector/service.py backend/app/collector/spool.py backend/app/collector/supervisor.py backend/app/collector/main.py backend/tests/test_collector_supervisor.py backend/tests/test_collector_spool.py backend/Dockerfile infra/compose.yaml
git commit -m "feat: run CENC collector supervisor"
```

## Task 10：采集状态 API 与死信重放命令

**文件：**
- 新增：`backend/app/collector/router.py`
- 新增：`backend/app/collector/schemas.py`
- 新增：`backend/app/collector/status_service.py`
- 新增：`backend/app/collector/replay.py`
- 修改：`backend/app/main.py`
- 新增：`backend/tests/test_collector_api.py`
- 新增：`backend/tests/test_collector_replay.py`
- 新增：`backend/tests/test_collector_status_service.py`

**接口：**
- 使用：
  - `CollectorRuntimeState`
  - `CollectorDeadLetter`
  - `RegionBoundary`
  - 现有 `require_role`。
- 产出：
  - `GET /api/v1/collector/status`.
  - `CollectorStatusResponse` Pydantic schema.
  - `CollectorStatusService(session_factory, region_repository).get_status() -> CollectorStatusResponse`。
  - `get_collector_status_service()` FastAPI 依赖；测试可覆盖该依赖。
  - `replay_dead_letter(dead_letter_id, service, coordinator) -> None`。
  - `python -m app.collector.replay --dead-letter <uuid>`.

- [ ] **步骤 1：先编写会失败的 API 测试**

```python
# backend/tests/test_collector_api.py
from fastapi.testclient import TestClient

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collector.router import get_collector_status_service
from app.main import app


class FakeCollectorStatusService:
    async def get_status(self) -> dict[str, object]:
        return {
            "overall_state": "healthy",
            "providers": [
                {
                    "provider": "fan",
                    "state": "healthy",
                    "connected": True,
                    "last_http_status": None,
                    "last_connected_at": "2026-09-25T01:05:00Z",
                    "last_message_at": "2026-09-25T01:05:10Z",
                    "last_success_at": "2026-09-25T01:05:10Z",
                    "consecutive_failures": 0,
                    "reconnect_count": 0,
                    "last_error": None,
                    "updated_at": "2026-09-25T01:05:10Z",
                },
                {
                    "provider": "wolfx",
                    "state": "healthy",
                    "connected": True,
                    "last_http_status": 200,
                    "last_connected_at": "2026-09-25T01:05:00Z",
                    "last_message_at": "2026-09-25T01:05:10Z",
                    "last_success_at": "2026-09-25T01:05:10Z",
                    "consecutive_failures": 0,
                    "reconnect_count": 0,
                    "last_error": None,
                    "updated_at": "2026-09-25T01:05:10Z",
                },
            ],
            "open_dead_letter_count": 0,
            "boundary_version": "test-2026.1",
            "last_ingested_event_id": "event-1",
        }


def test_collector_status_requires_authentication() -> None:
    response = TestClient(app).get("/api/v1/collector/status")

    assert response.status_code == 401


def test_collector_status_returns_both_providers() -> None:
    fake_service = FakeCollectorStatusService()
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="superadmin",
        role="superadmin",
        workgroup=None,
    )
    app.dependency_overrides[get_collector_status_service] = lambda: fake_service
    try:
        response = TestClient(app).get("/api/v1/collector/status")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_collector_status_service, None)

    assert response.status_code == 200
    assert response.json()["overall_state"] == "healthy"
    assert {item["provider"] for item in response.json()["providers"]} == {"fan", "wolfx"}
```

若现有认证测试已经有依赖覆盖工具，则复用该工具，但不得引入未定义 fixture。

- [ ] **步骤 2：运行 API 测试并确认失败**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collector_api.py -v
```

预期：失败，因为路由尚不存在。

- [ ] **步骤 3：实现响应模型与路由**

响应结构：

```json
{
  "overall_state": "healthy",
  "providers": [
    {
      "provider": "fan",
      "state": "healthy",
      "connected": true,
      "last_http_status": null,
      "last_connected_at": "2026-09-25T01:05:00Z",
      "last_message_at": "2026-09-25T01:05:10Z",
      "last_success_at": "2026-09-25T01:05:10Z",
      "consecutive_failures": 0,
      "reconnect_count": 0,
      "last_error": null,
      "updated_at": "2026-09-25T01:05:10Z"
    }
  ],
  "open_dead_letter_count": 0,
  "boundary_version": "shanghai-public-2026.1",
  "last_ingested_event_id": "uuid-or-null"
}
```

总体状态规则：

- FAN 为 `healthy` 时总体为 `healthy`，无论 Wolfx 是健康还是降级。
- FAN 为 `degraded`、`starting`、`stopped` 或缺失，但 Wolfx 为 `healthy` 时总体为 `degraded`。
- 两条链路均为 `critical`、`stopped`、缺失或不存在健康状态行时，总体为 `critical`。

仅允许 `superadmin`、`group_leader`、`group_deputy` 访问。

`CollectorStatusService` 必须通过同一数据库会话读取：

- `collector_runtime_state` 中 FAN、Wolfx 两行；缺失行按不存在处理并参与总体状态计算。
- `collector_dead_letters.status == "open"` 的计数。
- `RegionRepository.get_active(session)` 返回的当前边界版本。
- `earthquake_revisions.ingested_at IS NOT NULL` 中按 `ingested_at DESC, revision_no DESC` 得到的最近事件 ID；没有记录时返回 `null`，不得根据运行状态表中的其他时间字段猜测。

`get_collector_status_service()` 必须构造并返回该服务，路由器不得在请求处理函数中直接拼装数据库查询。

`backend/tests/test_collector_status_service.py` 使用真实数据库构造 FAN/Wolfx 运行状态、开放死信、有效区域边界和两条不同 `ingested_at` 的修订，断言聚合字段、开放死信数量、边界版本和最近事件 ID；同时断言 `ingested_at=NULL` 的旧修订不会覆盖最近结果。

- [ ] **步骤 4：实现死信重放命令**

核心函数签名固定为：

```python
async def replay_dead_letter(
    dead_letter_id: str,
    service: CollectorService,
    coordinator: CollectorCoordinator,
) -> None:
```

`replay.py` 必须：

- 必填参数 `--dead-letter <uuid>`。
- 读取原始 payload、provider、lane。
- 通过 `CollectorCoordinator` 重新处理。
- 成功后标记 `retried`，后续再次成功重放后标记 `resolved`。
- 再次失败时标记 `open` 并追加新错误。
- 使用原始 `CollectorDeadLetter.received_at` 作为 envelope 接收时间，使历史报文永远不会被当成实时报文。
- 使用 `trigger_reason="recovery"`。
- 不得输出凭据。

先编写：

```python
# backend/tests/test_collector_replay.py
from datetime import UTC, datetime

from app.collector.replay import replay_dead_letter


class FakeReplayService:
    def __init__(self) -> None:
        self.record = {
            "id": "dead-1",
            "raw_payload": {"No1": {"EventID": "CENC-1"}},
            "provider": "wolfx",
            "lane": "http",
            "received_at": datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
            "status": "open",
        }
        self.statuses: list[str] = []

    async def load_dead_letter(self, dead_letter_id: str):
        return self.record

    async def mark_dead_letter(self, dead_letter_id: str, status: str, error=None) -> None:
        self.statuses.append(status)


class FakeReplayCoordinator:
    def __init__(self) -> None:
        self.envelopes = []
        self.trigger_reasons: list[str] = []

    async def ingest(self, envelope, trigger_reason: str = "live"):
        self.envelopes.append(envelope)
        self.trigger_reasons.append(trigger_reason)
        return None


async def test_replay_uses_original_received_at_and_recovery_reason() -> None:
    service = FakeReplayService()
    coordinator = FakeReplayCoordinator()

    await replay_dead_letter("dead-1", service, coordinator)

    assert coordinator.envelopes[0].received_at == service.record["received_at"]
    assert coordinator.trigger_reasons == ["recovery"]
    assert service.statuses == ["retried"]
```

- [ ] **步骤 5：向 FastAPI 注册路由**

在 `backend/app/main.py` 导入并注册 `collector_router`。重放命令仅提供 CLI，不提供 HTTP 端点。

- [ ] **步骤 6：运行 API 与重放测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_collector_api.py tests/test_collector_replay.py tests/test_auth.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app
```

预期：测试通过，只有已认证且有权限的用户能查看采集状态。

- [ ] **步骤 7：提交**

```powershell
git add backend/app/collector/router.py backend/app/collector/schemas.py backend/app/collector/status_service.py backend/app/collector/replay.py backend/app/main.py backend/tests/test_collector_api.py backend/tests/test_collector_replay.py backend/tests/test_collector_status_service.py
git commit -m "feat: expose collector status and replay"
```

## Task 11：采集状态与事件生命周期界面

**文件：**
- 修改：`frontend/src/types.ts`
- 修改：`frontend/src/api/client.ts`
- 修改：`frontend/src/App.tsx`
- 修改：`frontend/src/pages/EventListPage.tsx`
- 修改：`frontend/src/pages/EventDetailPage.tsx`
- 修改：`frontend/src/styles.css`
- 新增：`frontend/src/pages/CollectorStatusPage.tsx`
- 新增：`frontend/tests/collector-status.test.tsx`
- 修改：`frontend/tests/event-list.test.tsx`

**接口：**
- 使用：`GET /api/v1/collector/status`。
- 产出：
  - `CollectorStatus` TypeScript 接口。
  - `getCollectorStatus()` API 函数。
  - `/collector` 管理路由。
  - 事件列表与详情中的生命周期标签。

- [ ] **步骤 1：先编写会失败的前端测试**

```tsx
// frontend/tests/collector-status.test.tsx
import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { CollectorStatusPage } from "../src/pages/CollectorStatusPage";

vi.mock("../src/api/client", () => ({
  getCollectorStatus: vi.fn().mockResolvedValue({
    overall_state: "degraded",
    providers: [
      { provider: "fan", state: "degraded", connected: false, consecutive_failures: 3 },
      { provider: "wolfx", state: "healthy", connected: true, consecutive_failures: 0 },
    ],
    open_dead_letter_count: 2,
    boundary_version: "shanghai-public-2026.1",
    last_ingested_event_id: "event-1",
  }),
}));

test("renders primary degraded and backup healthy states", async () => {
  render(<CollectorStatusPage />);

  expect(await screen.findByText("总体状态：降级")).toBeInTheDocument();
  expect(screen.getByText("FAN 主链路")).toBeInTheDocument();
  expect(screen.getByText("Wolfx 备用链路")).toBeInTheDocument();
  expect(screen.getByText("待处理死信 2 条")).toBeInTheDocument();
});
```

- [ ] **步骤 2：运行前端测试并确认失败**

运行：

```powershell
Set-Location frontend
npm test -- --run tests/collector-status.test.tsx
```

预期：失败，因为页面和 API 函数尚不存在。

- [ ] **步骤 3：加入类型与 API 客户端**

```ts
export type CollectorState = "starting" | "healthy" | "degraded" | "critical" | "stopped";

export interface CollectorProviderStatus {
  provider: "fan" | "wolfx";
  state: CollectorState;
  connected: boolean;
  last_http_status: number | null;
  last_connected_at: string | null;
  last_message_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  reconnect_count: number;
  last_error: string | null;
  updated_at: string;
}

export interface CollectorStatus {
  overall_state: CollectorState;
  providers: CollectorProviderStatus[];
  open_dead_letter_count: number;
  boundary_version: string | null;
  last_ingested_event_id: string | null;
}

export type EventLifecycleState =
  | "auto_pending"
  | "formal_triggered"
  | "correction_triggered";
```

使用现有认证请求辅助函数加入 `getCollectorStatus()`。若 `requestJson` 未导出，则在不改变行为的前提下导出。

- [ ] **步骤 4：实现采集状态页**

页面必须：

- 加载时自动请求，并提供手动刷新按钮。
- 显示 `总体状态：正常/降级/严重/启动中/已停止`。
- 分别展示 FAN 与 Wolfx 的状态、连接、最后消息、最后成功、失败次数、重连次数和错误摘要。
- 显示死信数量、边界版本、最近入库事件 ID。
- 使用表格或定义列表，不得使用嵌套卡片。
- API 请求失败时显示可重试状态。
- 不得渲染凭据。

- [ ] **步骤 5：加入导航与生命周期标签**

在 `App.tsx` 中加入 `/collector`，标签为 `采集状态`。

加入：

```ts
export function formatLifecycleState(value: string | null | undefined): string {
  if (value === "auto_pending") return "自动待定";
  if (value === "formal_triggered") return "正式报已触发评估";
  if (value === "correction_triggered") return "修订已触发评估";
  return "未进入生命周期";
}
```

向事件摘要、事件详情类型和 API 映射加入 `lifecycle_state`、`t1_at`。列表渲染 `formatLifecycleState`，详情事实区显示 `T1`。

- [ ] **步骤 6：更新后端事件响应模型**

向 `EventSummaryRecord`、`EventDetailRecord`、Pydantic 响应和前端 payload 加入 `lifecycle_state`、`t1_at`。`t1_at` 返回 ISO 时间戳或 `null`。

- [ ] **步骤 7：运行前端与后端检查**

运行：

```powershell
Set-Location frontend
npm test
npm run typecheck
npm run build
Set-Location ../backend
docker compose --env-file ../.env -f ../infra/compose.yaml run --rm api pytest tests/test_collector_api.py tests/test_event_api.py -v
```

预期：前端测试、类型检查、生产构建和后端事件 API 测试全部通过。

- [ ] **步骤 8：提交**

```powershell
git add frontend/src frontend/tests backend/app/events/router.py backend/app/events/schemas.py backend/app/events/repository.py
git commit -m "feat: show collector health and event lifecycle"
```

## Task 12：端到端验证与运行手册

**文件：**
- 新增：`frontend/e2e/collector-flow.spec.ts`
- 新增：`backend/tests/test_collector_integration.py`
- 新增：`docs/runbooks/cenc-realtime-collection.md`
- 修改：`README.md`
- 修改：`.env.example`

**接口：**
- 使用：此前全部任务和现有 Compose 环境。
- 产出：可复现启动、健康验证、恢复验证和用户文档。

- [ ] **步骤 1：加入端到端测试**

先增加真实数据库集成测试：

```python
# backend/tests/test_collector_integration.py
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import (
    CollectorEnvelope,
    CollectorLane,
    CollectorProvider,
)
from app.events.models import EventLifecycleOutbox
from app.events.service import EventService
from app.regions.domain import RegionContext


class FixedRegionResolver:
    async def resolve(self, longitude, latitude) -> RegionContext:
        return RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=0,
            boundary_version="test-2026.1",
            computed_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        )


async def test_fan_auto_then_wolfx_formal_creates_one_trigger(session_factory) -> None:
    service = EventService(session_factory)
    coordinator = CollectorCoordinator(service, FixedRegionResolver())
    auto = {
        "EventID": "CENC-INT-1",
        "type": "automatic",
        "time": "2026-09-25T01:02:03Z",
        "placeName": "上海集成测试",
        "magnitude": 4.8,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }
    formal = {
        **auto,
        "type": "reviewed",
        "ReportTime": "2026-09-25T01:04:00Z",
        "magnitude": 5.1,
    }

    auto_outcome = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 3, tzinfo=UTC),
            payload={"No1": auto},
        )
    )
    formal_outcome = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": formal},
        ),
        trigger_reason="recovery",
    )

    assert auto_outcome is not None
    assert formal_outcome is not None
    assert auto_outcome.event_id == formal_outcome.event_id
    assert auto_outcome.triggered_assessment is False
    assert formal_outcome.triggered_assessment is True
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EventLifecycleOutbox)
            .where(EventLifecycleOutbox.event_id == formal_outcome.event_id)
        )
    assert count == 1


async def test_wolfx_formal_reaches_lifecycle_when_fan_is_unavailable(
    session_factory,
) -> None:
    service = EventService(session_factory)
    coordinator = CollectorCoordinator(service, FixedRegionResolver())
    formal = {
        "type": "reviewed",
        "EventID": "CENC-BACKUP-ONLY-1",
        "time": "2026-09-25T02:02:03Z",
        "ReportTime": "2026-09-25T02:04:00Z",
        "placeName": "上海备用链路集成测试",
        "magnitude": 5.0,
        "depth": 10,
        "latitude": 31.25,
        "longitude": 121.55,
    }

    outcome = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 2, 5, tzinfo=UTC),
            payload={"No1": formal},
        )
    )

    assert outcome is not None
    assert outcome.triggered_assessment is True
    assert outcome.lifecycle_state == "formal_triggered"
```

这些测试使用真实 PostgreSQL/PostGIS 和真实生命周期服务。第一条覆盖自动报不触发、跨链路正式报触发唯一 Outbox；第二条在 FAN 不参与时仅通过 Wolfx 正式报完成生命周期触发，覆盖备用链路兜底验收。FAN/Wolfx 断线重连和 HTTP 失败仍由任务 7、8 的假连接/假 HTTP 测试覆盖，然后由步骤 7 的真实上游冒烟测试验证线上可用性。

```ts
// frontend/e2e/collector-flow.spec.ts
import { expect, test } from "@playwright/test";

test("collector status remains visible when backup is serving events", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill(process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin");
  await page.getByLabel("密码").fill(process.env.E2E_SUPERADMIN_PASSWORD ?? "");
  await page.getByRole("button", { name: "登录" }).click();

  await page.getByRole("link", { name: "采集状态" }).click();

  await expect(page.getByText(/总体状态：/)).toBeVisible();
  await expect(page.getByText("FAN 主链路")).toBeVisible();
  await expect(page.getByText("Wolfx 备用链路")).toBeVisible();
});
```

增加第二个测试。正式兼容接口现在按 `recovery` 生命周期处理正式报，因此该测试验证真实 API 入库、Outbox 触发和页面展示闭环：

```ts
test("formal recovery ingest reaches the lifecycle list", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill(process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin");
  await page.getByLabel("密码").fill(process.env.E2E_SUPERADMIN_PASSWORD ?? "");
  await page.getByRole("button", { name: "登录" }).click();

  const runId = crypto.randomUUID();
  const seed = Number.parseInt(runId.replaceAll("-", "").slice(0, 8), 16);
  const originTime = new Date(Date.UTC(2030, 0, 1) + seed * 1_000).toISOString();
  const place = `上海正式报 E2E ${runId}`;
  const response = await page.request.post("/api/v1/ingest/formal", {
    data: {
      eventId: `CENC-FORMAL-${runId}`,
      reportType: "formal",
      originTime,
      longitude: 121.4 + (seed % 400) / 1_000,
      latitude: 31 + ((seed >> 8) % 600) / 1_000,
      magnitude: 5.2,
      depth: 10,
      place,
      regionContext: {
        insideShanghai: true,
        distanceToBoundaryKm: 0,
        deaths: null,
        maxIntensity: null,
      },
    },
  });
  const responsePayload = (await response.json()) as {
    event_kind: string;
    lifecycle_state: string;
  };
  expect(response.status(), JSON.stringify(responsePayload)).toBe(201);
  expect(responsePayload).toMatchObject({
    event_kind: "formal",
    lifecycle_state: "formal_triggered",
  });

  await page.getByRole("link", { name: "人工触发" }).click();
  await page.getByRole("link", { name: "事件列表", exact: true }).click();

  const row = page.getByRole("row").filter({ hasText: place });
  await expect(row).toContainText("正式报已触发评估");
});
```

- [ ] **步骤 2：编写运行手册**

运行手册必须包含以下精确命令：

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f infra/compose.yaml up -d postgres
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api collector frontend
docker compose --env-file .env -f infra/compose.yaml ps
docker compose --env-file .env -f infra/compose.yaml logs -f collector
```

必须说明：

- `FAN_APP_ID`、`CENC_APP_ID` 兼容关系和 `FAN_API_KEY`。
- 如何使用 `python -m app.regions.cli import` 导入边界。
- 如何检查 `collector_runtime_state`。
- 如何检查最近一条 Outbox。
- 如何仅停止 collector 而不停止 API。
- 如何使用 `python -m app.collector.replay` 重放死信。
- 如何检查 `/var/lib/collector-spool`、确认数据库故障期间的待恢复报文，并在数据库恢复后自动排空。
- 为什么 spool 写入失败或容量耗尽时 supervisor 会进入 `critical` 并停止，而不是继续接收后静默丢弃。
- 如何在不修改源码的情况下轮换 FAN API Key。
- 如何诊断 `auth_fail`、WebSocket 消息停滞、Wolfx HTTP 故障、数据库故障、双链路严重状态。
- 已知边界：真实 FAN/Wolfx 上游验证需要用户提供的密钥和外网连接。

- [ ] **步骤 3：更新 README**

新增简短章节并链接运行手册，说明：

- collector 是独立服务。
- FAN 是主链路，Wolfx 是常驻备用链路。
- 缺少凭据会阻止 collector 启动，但不阻止 API 测试。

- [ ] **步骤 4：运行完整后端验证**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

预期：全部后端测试通过，Ruff 无错误。

- [ ] **步骤 5：运行完整前端验证**

运行：

```powershell
Set-Location frontend
npm test
npm run typecheck
npm run build
```

预期：全部前端测试通过，类型检查通过，生产构建成功。

- [ ] **步骤 6：运行 Compose 与浏览器验证**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d --build
docker compose --env-file .env -f infra/compose.yaml ps
Set-Location frontend
npm run test:e2e
```

预期：

- PostgreSQL、API、collector、frontend 均健康或正常运行。
- collector 健康端点报告至少一个 provider 可用。
- 现有事件流程测试和新增 collector 流程测试通过。

若网络阻断 Docker 镜像下载，则使用宿主机 Vite 服务和本地 Chromium 完成浏览器验证，并明确记录容器浏览器路径未验证，不得声称已验证。

- [ ] **步骤 7：凭据和网络可用时运行真实上游冒烟测试**

运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml restart collector
docker compose --env-file .env -f infra/compose.yaml logs --since=2m collector
```

预期：

- FAN 认证成功，或记录明确的 `auth_fail`。
- 即使 FAN 认证失败，Wolfx 仍保持健康。
- 日志中不出现密钥。
- 两个 provider 的 `collector_runtime_state` 均已更新。

若密钥或网络不可用，将真实上游测试记为未执行，不得根据单元测试推断真实链路成功。

- [ ] **步骤 8：提交**

```powershell
git add frontend/e2e/collector-flow.spec.ts docs/runbooks/cenc-realtime-collection.md README.md .env.example
git commit -m "test: verify CENC collector operations"
```

## 计划自检

### 规格覆盖

| 设计规格章节 | 实施任务 |
| --- | --- |
| 独立采集器及 FAN/Wolfx 架构 | 任务 1、7、8、9 |
| FAN 主链路与 Wolfx 常驻备用链路 | 任务 7、8、9 |
| 跨提供方去重 | 任务 2、6 |
| 自动速报不触发评估 | 任务 2、6 |
| 首次正式报设置 `T1` 并生成唯一 Outbox | 任务 3、6 |
| 后续正式修订生成 correction | 任务 2、6 |
| 恢复处理与启动时间窗口 | 任务 9 |
| 区域边界与 `pending` 行为 | 任务 5 |
| 运行健康与死信 | 任务 4、9、10 |
| 数据库故障本地 spool 与恢复 | 任务 1、9 |
| 正式报兼容补录触发 | 任务 6、12 |
| 状态 API 与管理页面 | 任务 10、11 |
| 安全与密钥脱敏 | 任务 1、7、9、10 |
| 单元、服务、集成和端到端测试 | 任务 2 至 12 |
| 通过 Outbox 向未来 Temporal 交接 | 任务 3、6 |

### 类型一致性

- `CollectorProvider`、`CollectorLane` 在任务 1 定义，后续不变。
- `semantic_fingerprint`、`message_family` 在任务 2 定义，任务 6 使用。
- `LifecycleIngestOutcome` 在任务 6 定义，任务 9、10 使用。
- `CollectorEnvelope` 在任务 1 定义，任务 7、8 产生。
- `RegionContextResolver.resolve` 在任务 5 定义，任务 9 使用。
- `CollectorRuntimeState`、`CollectorDeadLetter` 在任务 4 定义，任务 9、10 使用。
- `RegionContext` 与 `CollectorSpool` 的接口在任务 5、9 定义，并由任务 6、9 消费。
- `CollectorStatusService` 在任务 10 定义，聚合任务 4 的运行状态/死信、任务 5 的边界版本和任务 6 的最近入库修订。
- API `lifecycle_state`、`t1_at` 与数据库及前端命名完全一致。
- `CollectorDeadLetter.received_at` 是重放时唯一允许使用的原始接收时间来源。
- `last_processed_source_time` 仅作为上游恢复查询下界，不得用于过滤 live、spool 或死信 envelope。

### 占位符扫描

计划不包含 `TBD`、`TODO`、未定义错误处理或未定义接口引用。每个任务均明确文件、聚焦测试、实现行为、验证命令和提交边界。
