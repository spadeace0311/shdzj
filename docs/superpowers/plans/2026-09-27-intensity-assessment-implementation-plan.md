# Intensity Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the model intensity, instrument-intensity adapter, fused intensity, persistence, Temporal orchestration, versioning, correction handling, API, and verification required by the approved intensity assessment specification.

**Architecture:** Keep the modular monolith and PostgreSQL/Temporal boundaries. Add a pure `app/intensity` domain package for deterministic algorithms, a repository/service layer for GIS and persistence, and Temporal activities that orchestrate the three intensity tasks without embedding formulas.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, PostgreSQL 16/PostGIS 3.4 with PostGIS Raster, Temporal Python 1.33, NumPy, PyProj, Rasterio, PyYAML, Pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-27-intensity-assessment-design.md`

## Global Constraints

- Implement only `intensity.model`, `intensity.instrument`, and `intensity.fusion`; loss, maps, reports, AI, and collaboration remain out of scope.
- Use the approved Shanghai model coefficients and `axis-ratio-v1` exactly as written in the spec.
- Use EPSG:32651 for the default Shanghai 1 km grid and WGS 84 geodesic distances.
- Continuous values use double precision; presentation rounds to 0.1 intensity degrees.
- The model task and fusion task are required; the instrument task is optional and degrades to `model_only`.
- The five-minute deadline starts at the latest valid report ingestion; it is an alert threshold, not a cancellation threshold.
- The technical workflow safety timeout is 1800 seconds.
- Every result must retain algorithm, parameter, grid, region, source, checksum, quality, and timing metadata.
- Do not read, print, commit, or expose `.env`, API keys, passwords, tokens, or credentials.
- All behavior changes use red-green TDD and end in a focused commit.

---

### Task 1: Domain Contracts and Versioned Parameters

**Files:**
- Create: `backend/app/intensity/__init__.py`
- Create: `backend/app/intensity/domain.py`
- Create: `backend/app/intensity/parameters.py`
- Create: `config/intensity/shanghai-2019.yaml`
- Create: `backend/tests/test_intensity_domain.py`
- Create: `backend/tests/test_intensity_parameters.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/config.py`
- Modify: `.env.example`
- Modify: `backend/tests/test_assessment_domain.py`

**Interfaces:**
- Consumes: existing `Settings` configuration pattern.
- Produces:
  - `ProductType`, `ProductStatus`, `InstrumentQuality`, `FusionMode`, `FusionQuality`, `DirectionStatus`
  - `IntensityEventSnapshot`
  - `GridDefinition`
  - `GridSamples`
  - `DirectionDecision`
  - `ModelField`
  - `InstrumentProduct`
  - `FusionField`
  - `ModelParameters`, `FusionParameters`, `ParameterBundle`
  - `load_parameter_bundle(path: str | Path) -> ParameterBundle`
  - `parameter_bundle_checksum(path: str | Path) -> str`

- [ ] **Step 1: Add failing dependency, configuration, and domain tests**

Add to `backend/pyproject.toml`:

```toml
dependencies = [
  "alembic==1.14.0",
  "asyncpg==0.30.0",
  "fastapi==0.115.6",
  "geoalchemy2==0.16.0",
  "httpx==0.28.1",
  "numpy==2.2.2",
  "pyjwt==2.10.1",
  "pwdlib[argon2]==0.2.1",
  "pydantic-settings==2.7.0",
  "pyproj==3.7.1",
  "python-multipart==0.0.20",
  "pyyaml==6.0.2",
  "rasterio==1.4.3",
  "sqlalchemy[asyncio]==2.0.36",
  "temporalio==1.33.0",
  "uvicorn[standard]==0.34.0",
  "websockets==15.0.1",
]
```

Add to `backend/app/config.py`:

```python
intensity_parameters_path: str = "/config/intensity/shanghai-2019.yaml"
intensity_region_profile_path: str = "/config/intensity/shanghai-region.yaml"
assessment_workflow_safety_timeout_seconds: int = 1800
```

Add validation in `validate_collector_configuration`:

```python
if not self.intensity_parameters_path.strip():
    raise ValueError("INTENSITY_PARAMETERS_PATH must not be empty")
if not self.intensity_region_profile_path.strip():
    raise ValueError("INTENSITY_REGION_PROFILE_PATH must not be empty")
if self.assessment_workflow_safety_timeout_seconds <= 0:
    raise ValueError("ASSESSMENT_WORKFLOW_SAFETY_TIMEOUT_SECONDS must be positive")
```

Update `backend/tests/test_assessment_domain.py` to assert:

```python
assert configured.intensity_parameters_path.endswith("shanghai-2019.yaml")
assert configured.intensity_region_profile_path.endswith("shanghai-region.yaml")
assert configured.assessment_workflow_safety_timeout_seconds == 1800
```

Add these invalid overrides to `test_assessment_settings_validate_bounded_values`:

```python
{"intensity_parameters_path": " "},
{"intensity_region_profile_path": ""},
{"assessment_workflow_safety_timeout_seconds": 0},
```

Add to `.env.example` without secrets:

```dotenv
INTENSITY_PARAMETERS_PATH=/config/intensity/shanghai-2019.yaml
INTENSITY_REGION_PROFILE_PATH=/config/intensity/shanghai-region.yaml
ASSESSMENT_WORKFLOW_SAFETY_TIMEOUT_SECONDS=1800
```

Create `config/intensity/shanghai-2019.yaml`:

```yaml
version: "shanghai-2019.1"

model:
  reference_doi: "10.3969/j.issn.1005-586X.2019.03.002"
  long_axis:
    intercept: 3.4142
    magnitude_coefficient: 1.0353
    decay_coefficient: 0.96272
    distance_offset_km: 8.0209
    sigma: 0.6310
  short_axis:
    intercept: 3.1626
    magnitude_coefficient: 1.0379
    decay_coefficient: 0.97451
    distance_offset_km: 7.6851
    sigma: 0.6638
  valid_magnitude:
    min: 4.0
    max: 6.2
  solver:
    intensity_min: -20.0
    intensity_max: 12.0
    tolerance: 0.000001
    max_iterations: 64

fusion:
  model_quality_weight: 1.0
  quality_weights:
    Q1: 1.0
    Q2: 0.5
    Q3: 0.25
    Q0: 0.0
  epsilon: 0.000001
  interval_z: 1.2816
  f1:
    min_coverage: 0.90
    max_sigma_p95: 0.75
  f2:
    min_coverage: 0.50
    max_sigma_p95: 1.25
```

Create `backend/tests/test_intensity_domain.py`:

```python
from app.intensity.domain import (
    DirectionStatus,
    FusionMode,
    FusionQuality,
    InstrumentQuality,
    ProductStatus,
    ProductType,
)


def test_intensity_enums_have_stable_external_values() -> None:
    assert ProductType.MODEL == "model"
    assert ProductType.INSTRUMENT == "instrument"
    assert ProductType.FUSION == "fusion"
    assert ProductStatus.UNAVAILABLE == "unavailable"
    assert InstrumentQuality.Q1 == "Q1"
    assert FusionMode.MODEL_ONLY == "model_only"
    assert FusionQuality.F3 == "F3"
    assert DirectionStatus.UNCERTAIN == "uncertain"
```

Create `backend/tests/test_intensity_parameters.py`:

```python
from pathlib import Path

import pytest

from app.intensity.domain import InstrumentQuality
from app.intensity.parameters import load_parameter_bundle, parameter_bundle_checksum


PARAMETERS = Path("/config/intensity/shanghai-2019.yaml")


def test_loads_approved_model_and_fusion_parameters() -> None:
    bundle = load_parameter_bundle(PARAMETERS)

    assert bundle.version == "shanghai-2019.1"
    assert bundle.checksum == parameter_bundle_checksum(PARAMETERS)
    assert bundle.model.long_axis.intercept == pytest.approx(3.4142)
    assert bundle.model.long_axis.decay_coefficient == pytest.approx(0.96272)
    assert bundle.model.short_axis.sigma == pytest.approx(0.6638)
    assert bundle.model.valid_magnitude_min == pytest.approx(4.0)
    assert bundle.model.valid_magnitude_max == pytest.approx(6.2)
    assert bundle.fusion.model_quality_weight == pytest.approx(1.0)
    assert bundle.fusion.quality_weights[InstrumentQuality.Q2] == pytest.approx(0.5)
    assert bundle.fusion.f1_min_coverage == pytest.approx(0.90)


def test_parameter_checksum_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    source = PARAMETERS.read_text(encoding="utf-8")
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(source, encoding="utf-8")
    second.write_text(source.replace("0.6310", "0.6311"), encoding="utf-8")

    assert parameter_bundle_checksum(first) == parameter_bundle_checksum(PARAMETERS)
    assert parameter_bundle_checksum(second) != parameter_bundle_checksum(PARAMETERS)
```

- [ ] **Step 2: Rebuild the API image and verify the tests fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml build api
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_domain.py tests/test_intensity_parameters.py -v
```

Expected: FAIL because `app.intensity` does not exist and `INTENSITY_PARAMETERS_PATH` is not mounted into the test command's environment.

- [ ] **Step 3: Implement immutable domain contracts and parameters**

Create `backend/app/intensity/domain.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import numpy as np


class ProductType(StrEnum):
    MODEL = "model"
    INSTRUMENT = "instrument"
    FUSION = "fusion"


class ProductStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"


class InstrumentQuality(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q0 = "Q0"


class FusionMode(StrEnum):
    FULL = "full"
    MODEL_ONLY = "model_only"


class FusionQuality(StrEnum):
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F0 = "F0"


class DirectionStatus(StrEnum):
    RESOLVED = "resolved"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class IntensityEventSnapshot:
    event_id: str
    revision_id: str
    magnitude: float
    longitude: float
    latitude: float
    report_ingested_at: datetime


@dataclass(frozen=True, slots=True)
class GridDefinition:
    version: str
    crs: str
    resolution_m: int
    origin_x: float
    origin_y: float
    width: int
    height: int

    @property
    def cell_count(self) -> int:
        return self.width * self.height

    def center_xy(self, row: int, column: int) -> tuple[float, float]:
        if not 0 <= row < self.height or not 0 <= column < self.width:
            raise IndexError("grid coordinate outside definition")
        return (
            self.origin_x + (column + 0.5) * self.resolution_m,
            self.origin_y - (row + 0.5) * self.resolution_m,
        )


@dataclass(frozen=True, slots=True)
class GridSamples:
    definition: GridDefinition
    rows: np.ndarray
    columns: np.ndarray
    x_km: np.ndarray
    y_km: np.ndarray
    longitude: np.ndarray
    latitude: np.ndarray
    distance_km: np.ndarray
    azimuth_deg: np.ndarray


@dataclass(frozen=True, slots=True)
class DirectionDecision:
    status: DirectionStatus
    source: str | None
    strike_deg: float | None
    candidate_fault_id: str | None = None
    candidate_distance_km: float | None = None


@dataclass(frozen=True, slots=True)
class ModelField:
    values: np.ndarray
    sigma: np.ndarray
    extrapolated: bool
    direction: DirectionDecision


@dataclass(frozen=True, slots=True)
class InstrumentProduct:
    status: ProductStatus
    product_id: str | None
    product_version: str | None
    observed_at: datetime | None
    source: str
    grid_version: str | None
    values: np.ndarray | None
    sigma: np.ndarray | None
    quality_codes: np.ndarray | None
    coverage_ratio: float
    reason: str | None = None
    generated_at: datetime | None = None
    crs: str | None = None
    resolution_m: float | None = None
    raw_checksum: str | None = None
    normalized_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class FusionField:
    values: np.ndarray
    sigma: np.ndarray
    p10: np.ndarray
    p90: np.ndarray
    model_weight: np.ndarray
    instrument_weight: np.ndarray
    quality_codes: np.ndarray
    mode: FusionMode
    quality: FusionQuality
    coverage_ratio: float
```

Create `backend/app/intensity/parameters.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.intensity.domain import InstrumentQuality


@dataclass(frozen=True, slots=True)
class AxisParameters:
    intercept: float
    magnitude_coefficient: float
    decay_coefficient: float
    distance_offset_km: float
    sigma: float


@dataclass(frozen=True, slots=True)
class ModelParameters:
    reference_doi: str
    long_axis: AxisParameters
    short_axis: AxisParameters
    valid_magnitude_min: float
    valid_magnitude_max: float
    solver_intensity_min: float
    solver_intensity_max: float
    solver_tolerance: float
    solver_max_iterations: int


@dataclass(frozen=True, slots=True)
class FusionParameters:
    model_quality_weight: float
    quality_weights: dict[InstrumentQuality, float]
    epsilon: float
    interval_z: float
    f1_min_coverage: float
    f1_max_sigma_p95: float
    f2_min_coverage: float
    f2_max_sigma_p95: float


@dataclass(frozen=True, slots=True)
class ParameterBundle:
    version: str
    model: ModelParameters
    fusion: FusionParameters
    checksum: str


def load_parameter_bundle(path: str | Path) -> ParameterBundle:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    model = payload["model"]
    fusion = payload["fusion"]

    def axis(name: str) -> AxisParameters:
        item = model[name]
        return AxisParameters(
            intercept=float(item["intercept"]),
            magnitude_coefficient=float(item["magnitude_coefficient"]),
            decay_coefficient=float(item["decay_coefficient"]),
            distance_offset_km=float(item["distance_offset_km"]),
            sigma=float(item["sigma"]),
        )

    weights = {
        InstrumentQuality(code): float(value)
        for code, value in fusion["quality_weights"].items()
    }
    if set(weights) != set(InstrumentQuality):
        raise ValueError("fusion quality weights must define Q1, Q2, Q3 and Q0")

    return ParameterBundle(
        version=str(payload["version"]),
        model=ModelParameters(
            reference_doi=str(model["reference_doi"]),
            long_axis=axis("long_axis"),
            short_axis=axis("short_axis"),
            valid_magnitude_min=float(model["valid_magnitude"]["min"]),
            valid_magnitude_max=float(model["valid_magnitude"]["max"]),
            solver_intensity_min=float(model["solver"]["intensity_min"]),
            solver_intensity_max=float(model["solver"]["intensity_max"]),
            solver_tolerance=float(model["solver"]["tolerance"]),
            solver_max_iterations=int(model["solver"]["max_iterations"]),
        ),
        fusion=FusionParameters(
            model_quality_weight=float(fusion["model_quality_weight"]),
            quality_weights=weights,
            epsilon=float(fusion["epsilon"]),
            interval_z=float(fusion["interval_z"]),
            f1_min_coverage=float(fusion["f1"]["min_coverage"]),
            f1_max_sigma_p95=float(fusion["f1"]["max_sigma_p95"]),
            f2_min_coverage=float(fusion["f2"]["min_coverage"]),
            f2_max_sigma_p95=float(fusion["f2"]["max_sigma_p95"]),
        ),
        checksum=parameter_bundle_checksum(path),
    )


def parameter_bundle_checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
```

Create `backend/app/intensity/__init__.py`:

```python
"""Deterministic earthquake intensity assessment domain."""
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_domain.py tests/test_intensity_parameters.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity tests/test_intensity_domain.py tests/test_intensity_parameters.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/app/config.py backend/app/intensity config/intensity backend/tests/test_intensity_domain.py backend/tests/test_intensity_parameters.py backend/tests/test_assessment_domain.py .env.example
git commit -m "feat: add intensity domain and parameters"
```

### Task 2: Region Profile, Stable Grid Geometry, and Geodesic Sampling

**Files:**
- Create: `backend/app/intensity/region.py`
- Create: `backend/app/intensity/grid.py`
- Create: `config/intensity/shanghai-region.yaml`
- Create: `backend/tests/test_intensity_region.py`
- Create: `backend/tests/test_intensity_grid.py`
- Modify: `backend/pyproject.toml` only if Task 1 has not already added NumPy and PyProj.

**Interfaces:**
- Consumes: `GridDefinition`, `GridSamples`.
- Produces:
  - `RegionProfile`
  - `load_region_profile(path: str | Path) -> RegionProfile`
  - `region_profile_checksum(path: str | Path) -> str`
  - `grid_definition_from_bounds(*, min_x, min_y, max_x, max_y, resolution_m, crs, version) -> GridDefinition`
  - `sample_grid(definition: GridDefinition, *, epicenter_longitude: float, epicenter_latitude: float) -> GridSamples`
  - `grid_definition_checksum(definition: GridDefinition) -> str`

- [ ] **Step 1: Write failing region-profile and grid tests**

Create `backend/tests/test_intensity_grid.py`:

```python
import numpy as np
import pytest

from app.intensity.domain import GridDefinition
from app.intensity.grid import (
    grid_definition_checksum,
    grid_definition_from_bounds,
    sample_grid,
)


def test_grid_definition_aligns_to_whole_cells() -> None:
    definition = grid_definition_from_bounds(
        min_x=999.0,
        min_y=1999.0,
        max_x=3001.0,
        max_y=5001.0,
        resolution_m=1000,
        crs="EPSG:32651",
        version="grid-test-1",
    )

    assert definition.origin_x == 0.0
    assert definition.origin_y == 6000.0
    assert definition.width == 4
    assert definition.height == 5
    assert definition.center_xy(0, 0) == (500.0, 5500.0)
    assert len(grid_definition_checksum(definition)) == 64


def test_sampling_returns_stable_rows_and_distances() -> None:
    definition = GridDefinition(
        version="grid-test-1",
        crs="EPSG:32651",
        resolution_m=1000,
        origin_x=0.0,
        origin_y=2000.0,
        width=2,
        height=2,
    )
    epicenter_lon = 121.5
    epicenter_lat = 31.2
    first = sample_grid(
        definition,
        epicenter_longitude=epicenter_lon,
        epicenter_latitude=epicenter_lat,
    )
    second = sample_grid(
        definition,
        epicenter_longitude=epicenter_lon,
        epicenter_latitude=epicenter_lat,
    )

    assert first.rows.tolist() == [0, 0, 1, 1]
    assert first.columns.tolist() == [0, 1, 0, 1]
    assert first.distance_km.shape == (4,)
    assert first.azimuth_deg.shape == (4,)
    assert first.x_km.shape == (4,)
    assert first.y_km.shape == (4,)
    assert np.allclose(first.longitude, second.longitude)
    assert np.allclose(first.distance_km, second.distance_km)
    assert np.all(first.distance_km >= 0)


def test_definition_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError):
        GridDefinition(
            version="bad",
            crs="EPSG:32651",
            resolution_m=0,
            origin_x=0,
            origin_y=0,
            width=1,
            height=1,
        )
```

Create `backend/tests/test_intensity_region.py`:

```python
from pathlib import Path

from app.intensity.region import load_region_profile, region_profile_checksum


REGION_PROFILE = Path("/config/intensity/shanghai-region.yaml")


def test_loads_versioned_shanghai_region_profile() -> None:
    profile = load_region_profile(REGION_PROFILE)

    assert profile.version == "shanghai-region-v1"
    assert profile.region_id == "shanghai"
    assert profile.administrative_level == "province"
    assert profile.grid_buffer_km == 100
    assert profile.grid_resolution_m == 1000
    assert profile.grid_crs == "EPSG:32651"
    assert profile.model_parameter_version == "shanghai-2019.1"
    assert profile.fusion_strategy_version == "fusion-inverse-variance-v1"
    assert profile.instrument_provider == "unavailable"
    assert profile.checksum == region_profile_checksum(REGION_PROFILE)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_grid.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `app.intensity.region` or
`app.intensity.grid`.

- [ ] **Step 3: Implement grid generation and geodesic sampling**

Create `config/intensity/shanghai-region.yaml`:

```yaml
version: "shanghai-region-v1"
region_id: "shanghai"
name: "上海市"
administrative_level: "province"
boundary_version: null
grid_buffer_km: 100
grid_resolution_m: 1000
grid_crs: "EPSG:32651"
model_parameter_version: "shanghai-2019.1"
fusion_strategy_version: "fusion-inverse-variance-v1"
instrument_provider: "unavailable"
fault_data_version: null
```

Create `backend/app/intensity/region.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RegionProfile:
    version: str
    region_id: str
    name: str
    administrative_level: str
    boundary_version: str | None
    grid_buffer_km: int
    grid_resolution_m: int
    grid_crs: str
    model_parameter_version: str
    fusion_strategy_version: str
    instrument_provider: str
    fault_data_version: str | None
    checksum: str

    def __post_init__(self) -> None:
        required = (
            self.version,
            self.region_id,
            self.name,
            self.administrative_level,
            self.grid_crs,
            self.model_parameter_version,
            self.fusion_strategy_version,
            self.instrument_provider,
        )
        if any(not value.strip() for value in required):
            raise ValueError("region profile text fields must not be empty")
        if self.grid_buffer_km < 0:
            raise ValueError("grid_buffer_km must not be negative")
        if self.grid_resolution_m <= 0:
            raise ValueError("grid_resolution_m must be positive")
        if len(self.checksum) != 64:
            raise ValueError("checksum must be a SHA-256 hex digest")


def load_region_profile(path: str | Path) -> RegionProfile:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RegionProfile(
        version=str(payload["version"]),
        region_id=str(payload["region_id"]),
        name=str(payload["name"]),
        administrative_level=str(payload["administrative_level"]),
        boundary_version=(
            str(payload["boundary_version"])
            if payload.get("boundary_version") is not None
            else None
        ),
        grid_buffer_km=int(payload["grid_buffer_km"]),
        grid_resolution_m=int(payload["grid_resolution_m"]),
        grid_crs=str(payload["grid_crs"]),
        model_parameter_version=str(payload["model_parameter_version"]),
        fusion_strategy_version=str(payload["fusion_strategy_version"]),
        instrument_provider=str(payload["instrument_provider"]),
        fault_data_version=(
            str(payload["fault_data_version"])
            if payload.get("fault_data_version") is not None
            else None
        ),
        checksum=region_profile_checksum(path),
    )


def region_profile_checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
```

Create `backend/app/intensity/grid.py`:

```python
from __future__ import annotations

import hashlib
import json
import math

import numpy as np
from pyproj import CRS, Geod, Transformer

from app.intensity.domain import GridDefinition, GridSamples


def grid_definition_from_bounds(
    *,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    resolution_m: int,
    crs: str,
    version: str,
) -> GridDefinition:
    if resolution_m <= 0:
        raise ValueError("resolution_m must be positive")
    if min_x > max_x or min_y > max_y:
        raise ValueError("grid bounds must be ordered")
    origin_x = math.floor(min_x / resolution_m) * resolution_m
    aligned_min_y = math.floor(min_y / resolution_m) * resolution_m
    origin_y = math.ceil(max_y / resolution_m) * resolution_m
    width = math.ceil((max_x - origin_x) / resolution_m)
    height = math.ceil((origin_y - aligned_min_y) / resolution_m)
    return GridDefinition(
        version=version,
        crs=crs,
        resolution_m=resolution_m,
        origin_x=float(origin_x),
        origin_y=float(origin_y),
        width=width,
        height=height,
    )


def grid_definition_checksum(definition: GridDefinition) -> str:
    payload = {
        "version": definition.version,
        "crs": definition.crs,
        "resolution_m": definition.resolution_m,
        "origin_x": definition.origin_x,
        "origin_y": definition.origin_y,
        "width": definition.width,
        "height": definition.height,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def sample_grid(
    definition: GridDefinition,
    *,
    epicenter_longitude: float,
    epicenter_latitude: float,
) -> GridSamples:
    rows, columns = np.indices((definition.height, definition.width))
    rows = rows.reshape(-1)
    columns = columns.reshape(-1)
    projected_x = definition.origin_x + (columns + 0.5) * definition.resolution_m
    projected_y = definition.origin_y - (rows + 0.5) * definition.resolution_m

    project_to_wgs84 = Transformer.from_crs(
        CRS.from_user_input(definition.crs),
        CRS.from_epsg(4326),
        always_xy=True,
    )
    longitude, latitude = project_to_wgs84.transform(projected_x, projected_y)
    longitude = np.asarray(longitude, dtype=np.float64)
    latitude = np.asarray(latitude, dtype=np.float64)

    geod = Geod(ellps="WGS84")
    distance_m, azimuth_deg, _ = geod.inv(
        np.full(longitude.shape, epicenter_longitude, dtype=np.float64),
        np.full(latitude.shape, epicenter_latitude, dtype=np.float64),
        longitude,
        latitude,
    )
    project_epicenter = Transformer.from_crs(
        CRS.from_epsg(4326),
        CRS.from_user_input(definition.crs),
        always_xy=True,
    )
    epicenter_x, epicenter_y = project_epicenter.transform(
        epicenter_longitude,
        epicenter_latitude,
    )
    return GridSamples(
        definition=definition,
        rows=rows,
        columns=columns,
        x_km=(projected_x - epicenter_x) / 1000.0,
        y_km=(projected_y - epicenter_y) / 1000.0,
        longitude=longitude,
        latitude=latitude,
        distance_km=np.asarray(distance_m, dtype=np.float64) / 1000.0,
        azimuth_deg=np.asarray(azimuth_deg, dtype=np.float64),
    )
```

Add validation to `GridDefinition.__post_init__` in `domain.py`:

```python
    def __post_init__(self) -> None:
        if self.resolution_m <= 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("grid dimensions and resolution must be positive")
        if not self.version.strip() or not self.crs.strip():
            raise ValueError("grid version and crs must not be empty")
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_grid.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_region.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity/grid.py app/intensity/region.py tests/test_intensity_grid.py tests/test_intensity_region.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intensity/domain.py backend/app/intensity/grid.py backend/app/intensity/region.py config/intensity/shanghai-region.yaml backend/tests/test_intensity_grid.py backend/tests/test_intensity_region.py
git commit -m "feat: add stable intensity grid geometry"
```

### Task 3: Direction Selection and Axis-Ratio Model Field

**Files:**
- Create: `backend/app/intensity/model.py`
- Create: `backend/tests/test_intensity_model.py`

**Interfaces:**
- Consumes: `GridSamples`, `ModelParameters`, `DirectionDecision`.
- Produces:
  - `FaultCandidate`
  - `resolve_direction(*, override_deg, focal_mechanism_deg, finite_fault_deg, candidates) -> DirectionDecision`
  - `axis_intensity(magnitude: float, distance_km: np.ndarray, axis: AxisParameters) -> np.ndarray`
  - `evaluate_model(snapshot, samples, direction, parameters) -> ModelField`
- Raises: `ModelFieldConvergenceError` when bounded bisection cannot bracket a root.

- [ ] **Step 1: Write failing model and direction tests**

Create `backend/tests/test_intensity_model.py`:

```python
import math
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    GridDefinition,
    GridSamples,
    IntensityEventSnapshot,
)
from app.intensity.model import (
    FaultCandidate,
    ModelFieldConvergenceError,
    axis_intensity,
    evaluate_model,
    resolve_direction,
)
from app.intensity.parameters import load_parameter_bundle


PARAMETERS = load_parameter_bundle("/config/intensity/shanghai-2019.yaml")


def test_direction_priority_and_stable_fault_tie_break() -> None:
    candidates = (
        FaultCandidate(fault_id="b", strike_deg=30.0, distance_km=5.0),
        FaultCandidate(fault_id="a", strike_deg=20.0, distance_km=5.0),
    )

    assert resolve_direction(
        override_deg=10.0,
        focal_mechanism_deg=20.0,
        finite_fault_deg=30.0,
        candidates=candidates,
    ).source == "manual_override"
    assert resolve_direction(
        override_deg=None,
        focal_mechanism_deg=20.0,
        finite_fault_deg=30.0,
        candidates=candidates,
    ).strike_deg == 20.0
    assert resolve_direction(
        override_deg=None,
        focal_mechanism_deg=None,
        finite_fault_deg=30.0,
        candidates=candidates,
    ).strike_deg == 30.0
    decision = resolve_direction(
        override_deg=None,
        focal_mechanism_deg=None,
        finite_fault_deg=None,
        candidates=candidates,
    )
    assert decision.candidate_fault_id == "a"
    assert decision.strike_deg == 20.0


def test_no_direction_uses_axis_average() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([10.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([10.0]),
        azimuth_deg=np.array([0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.UNCERTAIN, None, None),
        PARAMETERS.model,
    )
    expected = (5.806964124806861 + 5.552603218791649) / 2
    assert result.values[0] == pytest.approx(expected, abs=1e-6)
    assert not result.extrapolated


def test_axis_ratio_field_matches_axes_and_marks_extrapolation() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 2, 1),
        rows=np.array([0, 0]),
        columns=np.array([0, 1]),
        x_km=np.array([10.0, 0.0]),
        y_km=np.array([0.0, 10.0]),
        longitude=np.array([121.5, 121.5]),
        latitude=np.array([31.2, 31.2]),
        distance_km=np.array([10.0, 10.0]),
        azimuth_deg=np.array([90.0, 0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
        PARAMETERS.model,
    )

    assert result.values[0] == pytest.approx(5.806964124806861, abs=1e-5)
    assert result.values[1] == pytest.approx(5.552603218791649, abs=1e-5)
    assert result.sigma[0] == pytest.approx(0.6310)
    assert result.sigma[1] == pytest.approx(0.6638)


def test_model_marks_out_of_range_magnitude() -> None:
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=3.5,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([10.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([10.0]),
        azimuth_deg=np.array([0.0]),
    )

    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
        PARAMETERS.model,
    )

    assert result.extrapolated is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_model.py -v
```

Expected: FAIL with `ModuleNotFoundError: app.intensity.model`.

- [ ] **Step 3: Implement direction resolution and axis-ratio bisection**

Create `backend/app/intensity/model.py`:

```python
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    GridSamples,
    IntensityEventSnapshot,
    ModelField,
)
from app.intensity.parameters import AxisParameters, ModelParameters


class ModelFieldConvergenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FaultCandidate:
    fault_id: str
    strike_deg: float
    distance_km: float


def resolve_direction(
    *,
    override_deg: float | None,
    focal_mechanism_deg: float | None,
    finite_fault_deg: float | None,
    candidates: tuple[FaultCandidate, ...],
) -> DirectionDecision:
    for value, source in (
        (override_deg, "manual_override"),
        (focal_mechanism_deg, "focal_mechanism"),
        (finite_fault_deg, "finite_fault"),
    ):
        if value is not None:
            return DirectionDecision(
                status=DirectionStatus.RESOLVED,
                source=source,
                strike_deg=_normalize_strike(value),
            )
    eligible = tuple(
        candidate for candidate in candidates if candidate.distance_km <= 20.0
    )
    if eligible:
        selected = min(
            eligible,
            key=lambda item: (item.distance_km, item.fault_id),
        )
        return DirectionDecision(
            status=DirectionStatus.RESOLVED,
            source="nearest_fault",
            strike_deg=_normalize_strike(selected.strike_deg),
            candidate_fault_id=selected.fault_id,
            candidate_distance_km=selected.distance_km,
        )
    return DirectionDecision(DirectionStatus.UNCERTAIN, None, None)


def axis_intensity(
    magnitude: float,
    distance_km: np.ndarray,
    axis: AxisParameters,
) -> np.ndarray:
    distance = np.asarray(distance_km, dtype=np.float64)
    if np.any(distance < 0):
        raise ValueError("distance_km must not be negative")
    return (
        axis.intercept
        + axis.magnitude_coefficient * magnitude
        - axis.decay_coefficient * np.log(distance + axis.distance_offset_km)
    )
```

In the same file, implement the inverse radius and field:

```python
def _axis_radius(
    intensity: float,
    magnitude: float,
    axis: AxisParameters,
) -> float:
    exponent = (
        axis.intercept + axis.magnitude_coefficient * magnitude - intensity
    ) / axis.decay_coefficient
    radius = math.exp(exponent) - axis.distance_offset_km
    if not math.isfinite(radius) or radius <= 0:
        raise ModelFieldConvergenceError("axis radius is outside the valid range")
    return radius


def evaluate_model(
    snapshot: IntensityEventSnapshot,
    samples: GridSamples,
    direction: DirectionDecision,
    parameters: ModelParameters,
) -> ModelField:
    magnitude = float(snapshot.magnitude)
    if not math.isfinite(magnitude):
        raise ValueError("magnitude must be finite")
    if direction.status is DirectionStatus.UNCERTAIN:
        long_values = axis_intensity(
            magnitude,
            samples.distance_km,
            parameters.long_axis,
        )
        short_values = axis_intensity(
            magnitude,
            samples.distance_km,
            parameters.short_axis,
        )
        values = (long_values + short_values) / 2.0
        sigma = np.full(
            values.shape,
            math.sqrt(
                (parameters.long_axis.sigma**2 + parameters.short_axis.sigma**2) / 2.0
            ),
            dtype=np.float64,
        )
    else:
        values, sigma = _axis_ratio_field(
            magnitude,
            samples,
            float(direction.strike_deg),
            parameters,
        )
    return ModelField(
        values=values,
        sigma=sigma,
        extrapolated=not (
            parameters.valid_magnitude_min
            <= magnitude
            <= parameters.valid_magnitude_max
        ),
        direction=direction,
    )


def _axis_ratio_field(
    magnitude: float,
    samples: GridSamples,
    strike_deg: float,
    parameters: ModelParameters,
) -> tuple[np.ndarray, np.ndarray]:
    delta = np.deg2rad(samples.azimuth_deg - strike_deg)
    x = samples.distance_km * np.cos(delta)
    y = samples.distance_km * np.sin(delta)
    values = np.empty_like(x)
    sigma = np.empty_like(x)

    center_intensity = float(
        axis_intensity(
            magnitude,
            np.asarray([0.0], dtype=np.float64),
            parameters.long_axis,
        )[0]
    )
    high_bound = min(parameters.solver_intensity_max, center_intensity - 1e-6)
    if high_bound <= parameters.solver_intensity_min:
        raise ModelFieldConvergenceError("model field has no valid intensity bracket")

    for index, (x_value, y_value, delta_value) in enumerate(zip(x, y, delta, strict=True)):
        low = parameters.solver_intensity_min
        high = high_bound
        low_value = _field_residual(
            low,
            magnitude,
            x_value,
            y_value,
            parameters,
        )
        high_value = _field_residual(
            high,
            magnitude,
            x_value,
            y_value,
            parameters,
        )
        if low_value * high_value > 0:
            raise ModelFieldConvergenceError("model field root is not bracketed")
        for _ in range(parameters.solver_max_iterations):
            midpoint = (low + high) / 2.0
            residual = _field_residual(
                midpoint,
                magnitude,
                x_value,
                y_value,
                parameters,
            )
            if (
                abs(residual) <= parameters.solver_tolerance
                or high - low <= parameters.solver_tolerance
            ):
                low = high = midpoint
                break
            if low_value * residual <= 0:
                high = midpoint
            else:
                low = midpoint
                low_value = residual
        else:
            raise ModelFieldConvergenceError("model field bisection exceeded max iterations")

        values[index] = (low + high) / 2.0
        wa = math.cos(delta_value) ** 2
        wb = math.sin(delta_value) ** 2
        sigma[index] = math.sqrt(
            wa * parameters.long_axis.sigma**2
            + wb * parameters.short_axis.sigma**2
        )
    return values, sigma


def _field_residual(
    intensity: float,
    magnitude: float,
    x_km: float,
    y_km: float,
    parameters: ModelParameters,
) -> float:
    long_radius = _axis_radius(intensity, magnitude, parameters.long_axis)
    short_radius = _axis_radius(intensity, magnitude, parameters.short_axis)
    equivalent_distance = math.sqrt(
        x_km**2 + (long_radius / short_radius * y_km) ** 2
    )
    return float(
        axis_intensity(
            magnitude,
            np.asarray([equivalent_distance], dtype=np.float64),
            parameters.long_axis,
        )[0]
        - intensity
    )


def _normalize_strike(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("strike must be finite")
    return value % 360.0
```

- [ ] **Step 4: Run focused tests, including failure mode**

Add this test to `backend/tests/test_intensity_model.py`:

```python
def test_non_convergent_field_raises() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([1.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([1.0]),
        azimuth_deg=np.array([0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    broken = replace(
        PARAMETERS.model,
        solver_intensity_min=100.0,
        solver_intensity_max=101.0,
    )

    with pytest.raises(ModelFieldConvergenceError):
        evaluate_model(
            snapshot,
            samples,
            DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
            broken,
        )
```

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_model.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity/model.py tests/test_intensity_model.py
```

Expected: PASS, including the non-convergence test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intensity/model.py backend/tests/test_intensity_model.py
git commit -m "feat: implement axis-ratio model intensity"
```

### Task 4: Instrument Product Adapter and Validation

**Files:**
- Create: `backend/app/intensity/instrument.py`
- Create: `backend/tests/test_intensity_instrument.py`

**Interfaces:**
- Consumes: `GridDefinition`, `InstrumentProduct`, `ProductStatus`, `InstrumentQuality`.
- Produces:
  - `InstrumentRequest`
  - `InstrumentIntensityProvider` protocol
  - `UnavailableInstrumentProvider`
  - `validate_instrument_product(product, definition) -> InstrumentProduct`

- [ ] **Step 1: Write failing adapter tests**

Create `backend/tests/test_intensity_instrument.py`:

```python
from datetime import UTC, datetime

import numpy as np
import pytest

from app.intensity.domain import (
    GridDefinition,
    InstrumentProduct,
    InstrumentQuality,
    ProductStatus,
)
from app.intensity.instrument import (
    InstrumentRequest,
    UnavailableInstrumentProvider,
    validate_instrument_product,
)


DEFINITION = GridDefinition("grid-1", "EPSG:32651", 1000, 0, 2000, 2, 1)


async def test_default_provider_returns_unavailable_without_fake_grid() -> None:
    product = await UnavailableInstrumentProvider().fetch(
        InstrumentRequest(
            event_id="event-1",
            revision_id="revision-1",
            definition=DEFINITION,
            observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        )
    )

    assert product.status is ProductStatus.UNAVAILABLE
    assert product.values is None
    assert product.sigma is None
    assert product.quality_codes is None
    assert product.coverage_ratio == 0.0


def test_valid_product_is_normalized_on_grid() -> None:
    product = InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="instrument-1",
        product_version="v1",
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        source="eqim-fixture",
        grid_version="grid-1",
        values=np.array([[3.0, 4.0]], dtype=np.float64),
        sigma=np.array([[0.2, 0.3]], dtype=np.float64),
        quality_codes=np.array(
            [[InstrumentQuality.Q1, InstrumentQuality.Q2]],
            dtype=object,
        ),
        coverage_ratio=1.0,
    )

    normalized = validate_instrument_product(product, DEFINITION)

    assert normalized.status is ProductStatus.AVAILABLE
    assert normalized.values.shape == (2,)
    assert normalized.quality_codes.tolist() == ["Q1", "Q2"]
    assert normalized.coverage_ratio == pytest.approx(1.0)
    assert normalized.normalized_checksum is not None
    assert len(normalized.normalized_checksum) == 64


def test_invalid_shape_is_rejected() -> None:
    product = InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="bad",
        product_version="v1",
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        source="fixture",
        grid_version="grid-1",
        values=np.zeros((2, 2)),
        sigma=np.zeros((2, 2)),
        quality_codes=np.full((2, 2), InstrumentQuality.Q1),
        coverage_ratio=1.0,
    )

    with pytest.raises(ValueError, match="shape"):
        validate_instrument_product(product, DEFINITION)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_instrument.py -v
```

Expected: FAIL with `ModuleNotFoundError: app.intensity.instrument`.

- [ ] **Step 3: Implement the provider protocol and normalization**

Create `backend/app/intensity/instrument.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import numpy as np

from app.intensity.domain import (
    GridDefinition,
    InstrumentProduct,
    InstrumentQuality,
    ProductStatus,
)


@dataclass(frozen=True, slots=True)
class InstrumentRequest:
    event_id: str
    revision_id: str
    definition: GridDefinition
    observed_at: datetime


class InstrumentIntensityProvider(Protocol):
    async def fetch(self, request: InstrumentRequest) -> InstrumentProduct: ...


class UnavailableInstrumentProvider:
    async def fetch(self, request: InstrumentRequest) -> InstrumentProduct:
        return InstrumentProduct(
            status=ProductStatus.UNAVAILABLE,
            product_id=None,
            product_version=None,
            observed_at=None,
            source="not_connected",
            grid_version=request.definition.version,
            values=None,
            sigma=None,
            quality_codes=None,
            coverage_ratio=0.0,
            reason="formal instrument intensity product is not connected",
        )


def validate_instrument_product(
    product: InstrumentProduct,
    definition: GridDefinition,
) -> InstrumentProduct:
    if product.status is not ProductStatus.AVAILABLE and product.status is not ProductStatus.PARTIAL:
        return product
    expected_shape = (definition.height, definition.width)
    if product.grid_version != definition.version:
        raise ValueError("instrument product grid version does not match")
    if product.values is None or product.values.shape != expected_shape:
        raise ValueError("instrument product values shape does not match grid")
    if product.sigma is None or product.sigma.shape != expected_shape:
        raise ValueError("instrument product sigma shape does not match grid")
    if product.quality_codes is None or product.quality_codes.shape != expected_shape:
        raise ValueError("instrument product quality shape does not match grid")
    if not np.all(np.isfinite(product.values)):
        raise ValueError("instrument product values must be finite")
    if np.any(product.sigma < 0) or not np.all(np.isfinite(product.sigma)):
        raise ValueError("instrument product sigma must be finite and non-negative")

    normalized_codes = np.asarray(
        [
            InstrumentQuality(str(code)).value
            for code in product.quality_codes.reshape(-1)
        ],
        dtype=object,
    ).reshape(expected_shape)
    valid = np.asarray(
        [code in {InstrumentQuality.Q1.value, InstrumentQuality.Q2.value, InstrumentQuality.Q3.value}
         for code in normalized_codes.reshape(-1)],
        dtype=bool,
    ).reshape(expected_shape)
    coverage = float(valid.sum() / definition.cell_count)
    status = ProductStatus.AVAILABLE if coverage == 1.0 else ProductStatus.PARTIAL
    normalized_checksum = hashlib.sha256()
    normalized_checksum.update(np.asarray(product.values, dtype=np.float64).tobytes())
    normalized_checksum.update(np.asarray(product.sigma, dtype=np.float64).tobytes())
    normalized_checksum.update(normalized_codes.tobytes())
    return InstrumentProduct(
        status=status,
        product_id=product.product_id,
        product_version=product.product_version,
        observed_at=product.observed_at,
        source=product.source,
        grid_version=product.grid_version,
        values=np.asarray(product.values, dtype=np.float64),
        sigma=np.asarray(product.sigma, dtype=np.float64),
        quality_codes=normalized_codes,
        coverage_ratio=coverage,
        reason=product.reason,
        generated_at=product.generated_at,
        crs=product.crs,
        resolution_m=product.resolution_m,
        raw_checksum=product.raw_checksum,
        normalized_checksum=normalized_checksum.hexdigest(),
    )
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_instrument.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity/instrument.py tests/test_intensity_instrument.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intensity/instrument.py backend/tests/test_intensity_instrument.py
git commit -m "feat: add instrument intensity adapter"
```

### Task 5: Inverse-Variance Fusion and Quality

**Files:**
- Create: `backend/app/intensity/fusion.py`
- Create: `backend/tests/test_intensity_fusion.py`

**Interfaces:**
- Consumes: `ModelField`, `InstrumentProduct`, `FusionParameters`.
- Produces:
  - `fuse(model: ModelField, instrument: InstrumentProduct | None, parameters: FusionParameters) -> FusionField`
  - Internal deterministic helpers `_quality_codes`, `_p95`.

- [ ] **Step 1: Write failing fusion tests**

Create `backend/tests/test_intensity_fusion.py`:

```python
from datetime import UTC, datetime

import numpy as np
import pytest

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    FusionMode,
    FusionQuality,
    InstrumentProduct,
    InstrumentQuality,
    ModelField,
    ProductStatus,
)
from app.intensity.fusion import fuse
from app.intensity.parameters import load_parameter_bundle


PARAMETERS = load_parameter_bundle("/config/intensity/shanghai-2019.yaml")


def _model(values: list[float], sigma: list[float]) -> ModelField:
    return ModelField(
        values=np.asarray(values, dtype=np.float64),
        sigma=np.asarray(sigma, dtype=np.float64),
        extrapolated=False,
        direction=DirectionDecision(DirectionStatus.UNCERTAIN, None, None),
    )


def test_missing_instrument_returns_model_only() -> None:
    result = fuse(
        _model([4.0, 5.0], [0.6, 0.7]),
        None,
        PARAMETERS.fusion,
    )

    assert result.mode is FusionMode.MODEL_ONLY
    assert result.quality is FusionQuality.F3
    assert result.coverage_ratio == 0.0
    assert result.values.tolist() == [4.0, 5.0]
    assert result.instrument_weight.tolist() == [0.0, 0.0]


def test_inverse_variance_weighting_matches_expected() -> None:
    instrument = InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="i",
        product_version="v1",
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        source="fixture",
        grid_version="g",
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.2], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q1], dtype=object),
        coverage_ratio=1.0,
    )
    result = fuse(_model([4.0], [1.0]), instrument, PARAMETERS.fusion)
    model_weight = 1.0 / (1.0**2 + PARAMETERS.fusion.epsilon)
    instrument_weight = 1.0 / (0.2**2 + PARAMETERS.fusion.epsilon)
    expected = (model_weight * 4.0 + instrument_weight * 6.0) / (
        model_weight + instrument_weight
    )

    assert result.values[0] == pytest.approx(expected, abs=1e-6)
    assert result.p10[0] <= result.values[0] <= result.p90[0]
    assert result.mode is FusionMode.FULL


def test_quality_thresholds_follow_coverage_and_sigma() -> None:
    instrument = InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="i",
        product_version="v1",
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        source="fixture",
        grid_version="g",
        values=np.zeros(100, dtype=np.float64),
        sigma=np.full(100, 0.1, dtype=np.float64),
        quality_codes=np.full(100, InstrumentQuality.Q1, dtype=object),
        coverage_ratio=1.0,
    )
    result = fuse(
        _model([5.0] * 100, [0.5] * 100),
        instrument,
        PARAMETERS.fusion,
    )

    assert result.quality is FusionQuality.F1


def test_all_zero_quality_codes_fall_back_to_model_only() -> None:
    instrument = InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="i",
        product_version="v1",
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        source="fixture",
        grid_version="g",
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.1], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q0], dtype=object),
        coverage_ratio=0.0,
    )

    result = fuse(_model([4.0], [0.5]), instrument, PARAMETERS.fusion)

    assert result.mode is FusionMode.MODEL_ONLY
    assert result.values.tolist() == [4.0]
    assert result.instrument_weight.tolist() == [0.0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_fusion.py -v
```

Expected: FAIL with `ModuleNotFoundError: app.intensity.fusion`.

- [ ] **Step 3: Implement fusion and quality**

Create `backend/app/intensity/fusion.py`:

```python
from __future__ import annotations

import math

import numpy as np

from app.intensity.domain import (
    FusionField,
    FusionMode,
    FusionQuality,
    InstrumentProduct,
    InstrumentQuality,
    ModelField,
    ProductStatus,
)
from app.intensity.parameters import FusionParameters


def fuse(
    model: ModelField,
    instrument: InstrumentProduct | None,
    parameters: FusionParameters,
) -> FusionField:
    model_values = np.asarray(model.values, dtype=np.float64)
    model_sigma = np.asarray(model.sigma, dtype=np.float64)
    quality_codes = np.full(model_values.shape, InstrumentQuality.Q0.value, dtype=object)
    model_weight = parameters.model_quality_weight / (
        model_sigma**2 + parameters.epsilon
    )
    instrument_weight = np.zeros_like(model_values)
    fused_values = model_values.copy()
    fused_sigma = model_sigma.copy()
    mode = FusionMode.MODEL_ONLY
    coverage = 0.0

    if (
        instrument is not None
        and instrument.status in {ProductStatus.AVAILABLE, ProductStatus.PARTIAL}
        and instrument.values is not None
        and instrument.sigma is not None
        and instrument.quality_codes is not None
    ):
        valid = np.asarray(
            [
                str(code) in {
                    InstrumentQuality.Q1.value,
                    InstrumentQuality.Q2.value,
                    InstrumentQuality.Q3.value,
                }
                for code in instrument.quality_codes.reshape(-1)
            ],
            dtype=bool,
        ).reshape(model_values.shape)
        quality_codes[valid] = np.asarray(instrument.quality_codes, dtype=object)[valid]
        q = np.zeros(model_values.shape, dtype=np.float64)
        for quality, weight in parameters.quality_weights.items():
            q[quality_codes == quality.value] = weight
        candidate_weights = q / (instrument.sigma**2 + parameters.epsilon)
        candidate_weights[~valid] = 0.0
        instrument_weight = np.where(valid & (candidate_weights > 0), candidate_weights, 0.0)
        total_weight = model_weight + instrument_weight
        fused_values = (
            model_weight * model_values + instrument_weight * instrument.values
        ) / total_weight
        fused_sigma = np.sqrt(1.0 / total_weight)
        coverage = float(valid.sum() / model_values.size)
        if bool(instrument_weight.any()):
            mode = FusionMode.FULL

    p10 = fused_values - parameters.interval_z * fused_sigma
    p90 = fused_values + parameters.interval_z * fused_sigma
    quality_sigma = fused_sigma[instrument_weight > 0]
    quality = _fusion_quality(coverage, quality_sigma, mode, parameters)
    return FusionField(
        values=fused_values,
        sigma=fused_sigma,
        p10=p10,
        p90=p90,
        model_weight=model_weight,
        instrument_weight=instrument_weight,
        quality_codes=quality_codes,
        mode=mode,
        quality=quality,
        coverage_ratio=coverage,
    )


def _fusion_quality(
    coverage: float,
    sigma: np.ndarray,
    mode: FusionMode,
    parameters: FusionParameters,
) -> FusionQuality:
    if mode is FusionMode.MODEL_ONLY:
        return FusionQuality.F3
    sigma_p95 = _p95(sigma)
    if (
        coverage >= parameters.f1_min_coverage
        and sigma_p95 <= parameters.f1_max_sigma_p95
    ):
        return FusionQuality.F1
    if (
        coverage >= parameters.f2_min_coverage
        and sigma_p95 <= parameters.f2_max_sigma_p95
    ):
        return FusionQuality.F2
    return FusionQuality.F3


def _p95(values: np.ndarray) -> float:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return math.inf
    return float(np.percentile(finite, 95, method="linear"))
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_fusion.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity/fusion.py tests/test_intensity_fusion.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intensity/fusion.py backend/tests/test_intensity_fusion.py
git commit -m "feat: implement intensity fusion"
```

### Task 6: Assessment and Intensity Database Schema

**Files:**
- Create: `backend/app/intensity/models.py`
- Create: `backend/migrations/versions/0011_intensity_assessment.py`
- Create: `backend/tests/test_intensity_schema.py`
- Modify: `backend/app/assessment/models.py`
- Modify: `backend/app/events/models.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/test_assessment_schema.py`
- Modify: `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: existing assessment and event ORM models.
- Produces:
  - `AssessmentRun.report_ingested_at`, `deadline_basis_at`, `deadline_exceeded_at`, `duration_ms`, `algorithm_bundle_version`, `superseded_by_run_id`, `superseded_at`
  - `AssessmentTask.input_fingerprint`, `output_checksum`, `algorithm_version`
  - `EarthquakeEvent.latest_assessment_run_id`, `effective_assessment_run_id`
  - `AssessmentTaskAttempt`
  - `IntensityFieldProduct`
  - `IntensityRaster`

- [ ] **Step 1: Write failing schema tests**

Create `backend/tests/test_intensity_schema.py`:

```python
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from geoalchemy2 import Raster
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.intensity.models import IntensityFieldProduct, IntensityRaster


BACKEND_DIR = Path(__file__).parents[1]


def test_intensity_orm_metadata_contract() -> None:
    product = IntensityFieldProduct.__table__
    raster = IntensityRaster.__table__

    assert {
        "run_id",
        "task_id",
        "product_type",
        "status",
        "algorithm_version",
        "parameter_version",
        "strategy_version",
        "grid_definition_version",
        "region_profile_version",
        "input_fingerprint",
        "input_checksum",
        "output_checksum",
        "quality_grade",
        "coverage_ratio",
        "spatial_extent",
        "statistics",
        "source_product_id",
        "observed_at",
        "completed_at",
        "published_at",
    } <= set(product.c.keys())
    assert {
        "product_id",
        "rast",
        "band_manifest",
        "checksum",
        "width",
        "height",
        "srid",
    } <= set(raster.c.keys())
    assert isinstance(raster.c.rast.type, Raster)


async def test_intensity_schema_exists_at_migration_head() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    expected = ScriptDirectory.from_config(config).get_current_head()

    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        schema: dict[str, Any] = await connection.run_sync(
            lambda sync: {
                "tables": set(inspect(sync).get_table_names()),
                "columns": {
                    table: {column["name"] for column in inspect(sync).get_columns(table)}
                    for table in (
                        "assessment_runs",
                        "assessment_tasks",
                        "assessment_task_attempts",
                        "intensity_field_products",
                        "intensity_rasters",
                        "earthquake_events",
                    )
                    if table in inspect(sync).get_table_names()
                },
            }
        )
        migration = await connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        raster_extension = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis_raster')")
        )
    await engine.dispose()

    assert migration == expected
    assert raster_extension is True
    assert {
        "assessment_task_attempts",
        "intensity_field_products",
        "intensity_rasters",
    } <= schema["tables"]
    assert {
        "report_ingested_at",
        "deadline_basis_at",
        "deadline_exceeded_at",
        "duration_ms",
        "algorithm_bundle_version",
        "superseded_by_run_id",
        "superseded_at",
    } <= schema["columns"]["assessment_runs"]
    assert {
        "input_fingerprint",
        "output_checksum",
        "algorithm_version",
    } <= schema["columns"]["assessment_tasks"]
    assert {
        "latest_assessment_run_id",
        "effective_assessment_run_id",
    } <= schema["columns"]["earthquake_events"]
```

- [ ] **Step 2: Run the schema tests to verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_schema.py -v
```

Expected: FAIL because `app.intensity.models` and migration `0011_intensity_assessment` do not exist.

- [ ] **Step 3: Add ORM fields and tables**

In `backend/app/assessment/models.py`, add:

```python
report_ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
deadline_basis_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
deadline_exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
duration_ms: Mapped[int | None] = mapped_column(Integer)
algorithm_bundle_version: Mapped[str | None] = mapped_column(String(128))
superseded_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
    ForeignKey("assessment_runs.id", ondelete="SET NULL", use_alter=True),
    index=True,
)
superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Add to `AssessmentTask`:

```python
input_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
output_checksum: Mapped[str | None] = mapped_column(String(64))
algorithm_version: Mapped[str | None] = mapped_column(String(128))
```

In `backend/app/events/models.py`, add:

```python
latest_assessment_run_id: Mapped[uuid.UUID | None] = mapped_column(
    ForeignKey("assessment_runs.id", ondelete="SET NULL", use_alter=True),
    index=True,
)
effective_assessment_run_id: Mapped[uuid.UUID | None] = mapped_column(
    ForeignKey("assessment_runs.id", ondelete="SET NULL", use_alter=True),
    index=True,
)
```

Create `backend/app/intensity/models.py`:

```python
import uuid
from datetime import datetime

from geoalchemy2 import Geometry, Raster
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AssessmentTaskAttempt(Base):
    __tablename__ = "assessment_task_attempts"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_assessment_task_attempts_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    output_checksum: Mapped[str | None] = mapped_column(String(64))
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class IntensityFieldProduct(Base):
    __tablename__ = "intensity_field_products"
    __table_args__ = (
        UniqueConstraint("run_id", "product_type", name="uq_intensity_product_run_type"),
        Index("ix_intensity_products_run_type", "run_id", "product_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="CASCADE"),
        index=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    product_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    algorithm_version: Mapped[str] = mapped_column(String(128))
    parameter_version: Mapped[str] = mapped_column(String(128))
    strategy_version: Mapped[str | None] = mapped_column(String(128))
    grid_definition_version: Mapped[str] = mapped_column(String(128))
    region_profile_version: Mapped[str] = mapped_column(String(128))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    input_checksum: Mapped[str] = mapped_column(String(64))
    output_checksum: Mapped[str | None] = mapped_column(String(64))
    quality_grade: Mapped[str | None] = mapped_column(String(16), index=True)
    coverage_ratio: Mapped[float] = mapped_column(Numeric(8, 6), default=0)
    spatial_extent = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326),
        nullable=True,
    )
    statistics: Mapped[dict] = mapped_column(JSONB)
    source_product_id: Mapped[str | None] = mapped_column(String(160), index=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntensityRaster(Base):
    __tablename__ = "intensity_rasters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intensity_field_products.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    rast: Mapped[object] = mapped_column(Raster)
    band_manifest: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    srid: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
```

Create `backend/migrations/versions/0011_intensity_assessment.py` with revision:

```python
revision: str = "0011_intensity_assessment"
down_revision: str | None = "0010_assessment_orchestration"
```

Implement the migration with these exact operations:

```python
from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Geometry, Raster
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011_intensity_assessment"
down_revision: str | None = "0010_assessment_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis_raster")

    op.add_column(
        "assessment_runs",
        sa.Column("report_ingested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("deadline_basis_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("deadline_exceeded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("assessment_runs", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "assessment_runs",
        sa.Column("algorithm_bundle_version", sa.String(128), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("superseded_by_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE assessment_runs
        SET report_ingested_at = t1_at,
            deadline_basis_at = t1_at
        WHERE report_ingested_at IS NULL OR deadline_basis_at IS NULL
        """
    )
    op.alter_column("assessment_runs", "report_ingested_at", nullable=False)
    op.alter_column("assessment_runs", "deadline_basis_at", nullable=False)
    op.create_foreign_key(
        "fk_assessment_runs_superseded_by_run",
        "assessment_runs",
        "assessment_runs",
        ["superseded_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_assessment_runs_report_ingested_at",
        "assessment_runs",
        ["report_ingested_at"],
    )
    op.create_index(
        "ix_assessment_runs_deadline_basis_at",
        "assessment_runs",
        ["deadline_basis_at"],
    )
    op.create_index(
        "ix_assessment_runs_superseded_by_run_id",
        "assessment_runs",
        ["superseded_by_run_id"],
    )

    op.add_column(
        "assessment_tasks",
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "assessment_tasks",
        sa.Column("output_checksum", sa.String(64), nullable=True),
    )
    op.add_column(
        "assessment_tasks",
        sa.Column("algorithm_version", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_assessment_tasks_input_fingerprint",
        "assessment_tasks",
        ["input_fingerprint"],
    )

    op.create_table(
        "assessment_task_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        sa.Column("output_checksum", sa.String(64), nullable=True),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["assessment_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_assessment_task_attempts_number",
        ),
    )
    op.create_index(
        "ix_assessment_task_attempts_task_id",
        "assessment_task_attempts",
        ["task_id"],
    )
    op.create_index(
        "ix_assessment_task_attempts_status",
        "assessment_task_attempts",
        ["status"],
    )

    op.create_table(
        "intensity_field_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("algorithm_version", sa.String(128), nullable=False),
        sa.Column("parameter_version", sa.String(128), nullable=False),
        sa.Column("strategy_version", sa.String(128), nullable=True),
        sa.Column("grid_definition_version", sa.String(128), nullable=False),
        sa.Column("region_profile_version", sa.String(128), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_checksum", sa.String(64), nullable=False),
        sa.Column("output_checksum", sa.String(64), nullable=True),
        sa.Column("quality_grade", sa.String(16), nullable=True),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "spatial_extent",
            Geometry(geometry_type="POLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column("statistics", postgresql.JSONB(), nullable=False),
        sa.Column("source_product_id", sa.String(160), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["assessment_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["assessment_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "product_type",
            name="uq_intensity_product_run_type",
        ),
    )
    for name, columns in (
        ("ix_intensity_products_run_id", ["run_id"]),
        ("ix_intensity_products_task_id", ["task_id"]),
        ("ix_intensity_products_product_type", ["product_type"]),
        ("ix_intensity_products_status", ["status"]),
        ("ix_intensity_products_quality_grade", ["quality_grade"]),
        ("ix_intensity_products_source_product_id", ["source_product_id"]),
        ("ix_intensity_products_run_type", ["run_id", "product_type"]),
    ):
        op.create_index(name, "intensity_field_products", columns)

    op.create_table(
        "intensity_rasters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rast", Raster(), nullable=False),
        sa.Column("band_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("srid", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["intensity_field_products.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id"),
    )
    op.create_index(
        "ix_intensity_rasters_product_id",
        "intensity_rasters",
        ["product_id"],
    )

    op.add_column(
        "earthquake_events",
        sa.Column(
            "latest_assessment_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "earthquake_events",
        sa.Column(
            "effective_assessment_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_earthquake_events_latest_assessment_run",
        "earthquake_events",
        "assessment_runs",
        ["latest_assessment_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_earthquake_events_effective_assessment_run",
        "earthquake_events",
        "assessment_runs",
        ["effective_assessment_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_earthquake_events_latest_assessment_run_id",
        "earthquake_events",
        ["latest_assessment_run_id"],
    )
    op.create_index(
        "ix_earthquake_events_effective_assessment_run_id",
        "earthquake_events",
        ["effective_assessment_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_earthquake_events_effective_assessment_run_id",
        table_name="earthquake_events",
    )
    op.drop_index(
        "ix_earthquake_events_latest_assessment_run_id",
        table_name="earthquake_events",
    )
    op.drop_constraint(
        "fk_earthquake_events_effective_assessment_run",
        "earthquake_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_earthquake_events_latest_assessment_run",
        "earthquake_events",
        type_="foreignkey",
    )
    op.drop_column("earthquake_events", "effective_assessment_run_id")
    op.drop_column("earthquake_events", "latest_assessment_run_id")

    op.drop_index("ix_intensity_rasters_product_id", table_name="intensity_rasters")
    op.drop_table("intensity_rasters")
    for name in (
        "ix_intensity_products_run_type",
        "ix_intensity_products_source_product_id",
        "ix_intensity_products_quality_grade",
        "ix_intensity_products_status",
        "ix_intensity_products_product_type",
        "ix_intensity_products_task_id",
        "ix_intensity_products_run_id",
    ):
        op.drop_index(name, table_name="intensity_field_products")
    op.drop_table("intensity_field_products")

    op.drop_index(
        "ix_assessment_task_attempts_status",
        table_name="assessment_task_attempts",
    )
    op.drop_index(
        "ix_assessment_task_attempts_task_id",
        table_name="assessment_task_attempts",
    )
    op.drop_table("assessment_task_attempts")

    op.drop_index(
        "ix_assessment_tasks_input_fingerprint",
        table_name="assessment_tasks",
    )
    op.drop_column("assessment_tasks", "algorithm_version")
    op.drop_column("assessment_tasks", "output_checksum")
    op.drop_column("assessment_tasks", "input_fingerprint")

    op.drop_index(
        "ix_assessment_runs_superseded_by_run_id",
        table_name="assessment_runs",
    )
    op.drop_index(
        "ix_assessment_runs_deadline_basis_at",
        table_name="assessment_runs",
    )
    op.drop_index(
        "ix_assessment_runs_report_ingested_at",
        table_name="assessment_runs",
    )
    op.drop_constraint(
        "fk_assessment_runs_superseded_by_run",
        "assessment_runs",
        type_="foreignkey",
    )
    op.drop_column("assessment_runs", "superseded_at")
    op.drop_column("assessment_runs", "superseded_by_run_id")
    op.drop_column("assessment_runs", "algorithm_bundle_version")
    op.drop_column("assessment_runs", "duration_ms")
    op.drop_column("assessment_runs", "deadline_exceeded_at")
    op.drop_column("assessment_runs", "deadline_basis_at")
    op.drop_column("assessment_runs", "report_ingested_at")
```

Leave `postgis_raster` installed during downgrade because it may be shared by
other features.

Modify `backend/migrations/env.py`:

```python
from app.intensity import models as intensity_models  # noqa: F401
```

Update `backend/tests/test_migrations.py`:

```python
LATEST_REVISION = "0011_intensity_assessment"
INTENSITY_PREVIOUS_REVISION = "0010_assessment_orchestration"
```

Add a reversible migration test:

```python
async def _intensity_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
    await engine.dispose()
    return {
        "assessment_task_attempts",
        "intensity_field_products",
        "intensity_rasters",
    } <= names


async def test_0011_intensity_assessment_is_reversible() -> None:
    _set_revision(INTENSITY_PREVIOUS_REVISION)
    assert await _intensity_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _intensity_tables_exist() is True
        _set_revision(INTENSITY_PREVIOUS_REVISION)
        assert await _intensity_tables_exist() is False
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)
```

Update existing assessment schema assertions to include the new columns while
retaining the original uniqueness and index checks.

- [ ] **Step 4: Apply the migration and run schema tests**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_schema.py tests/test_assessment_schema.py tests/test_migrations.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app migrations tests/test_intensity_schema.py tests/test_assessment_schema.py tests/test_migrations.py
```

Expected: PASS and migration head `0011_intensity_assessment`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/assessment/models.py backend/app/events/models.py backend/app/intensity/models.py backend/migrations/env.py backend/migrations/versions/0011_intensity_assessment.py backend/tests/test_intensity_schema.py backend/tests/test_assessment_schema.py backend/tests/test_migrations.py
git commit -m "feat: add intensity assessment schema"
```

### Task 7: Raster Codec and Intensity Repository

**Files:**
- Create: `backend/app/intensity/artifacts.py`
- Create: `backend/app/intensity/repository.py`
- Create: `backend/tests/test_intensity_artifacts.py`
- Create: `backend/tests/test_intensity_repository.py`

**Interfaces:**
- Consumes: `GridDefinition`, `FusionField`, `ModelField`, `IntensityProductWrite`.
- Produces:
  - `RasterCodec.encode(definition, bands, band_manifest) -> bytes`
  - `RasterCodec.decode(payload) -> tuple[list[NDArray], dict]`
  - `IntensityProductWrite`
  - `IntensityRepository.save_product(session, write) -> UUID`
  - `IntensityRepository.get_product(session, run_id, product_type) -> ProductRecord | None`
  - `IntensityRepository.load_raster(session, product_id) -> tuple[list[NDArray], dict]`

- [ ] **Step 1: Write failing codec and repository tests**

Create `backend/tests/test_intensity_artifacts.py`:

```python
import numpy as np

from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition


def test_raster_codec_round_trip_preserves_values_and_georeference() -> None:
    definition = GridDefinition(
        version="grid-1",
        crs="EPSG:32651",
        resolution_m=1000,
        origin_x=500000.0,
        origin_y=3500000.0,
        width=2,
        height=2,
    )
    bands = [
        np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64),
        np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float64),
    ]
    payload = RasterCodec.encode(
        definition,
        bands,
        {"bands": [{"number": 1, "name": "value"}, {"number": 2, "name": "sigma"}]},
    )

    decoded_bands, metadata = RasterCodec.decode(payload)

    assert np.allclose(decoded_bands[0], bands[0])
    assert np.allclose(decoded_bands[1], bands[1])
    assert metadata["crs"] == "EPSG:32651"
    assert metadata["width"] == 2
    assert metadata["height"] == 2
```

Create `backend/tests/test_intensity_repository.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_intensity_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    await engine.dispose()


async def _seed_run(session_factory) -> tuple[UUID, UUID]:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="INTENSITY-REPO-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="test-grid",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            from app.assessment.repository import AssessmentRepository

            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            return run.id, task.id


async def test_product_and_raster_round_trip(session_factory) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    definition = GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1)
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=definition,
        region_profile_version="shanghai-v1",
        input_fingerprint="f" * 64,
        input_checksum="i" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )

    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        product = await repository.get_product(
            session,
            run_id=run_id,
            product_type=ProductType.MODEL,
        )
        bands, metadata = await repository.load_raster(session, product_id)

    assert product is not None
    assert product["product_type"] == "model"
    assert bands[0][0, 0] == pytest.approx(4.0)
    assert metadata["grid_definition_version"] == "grid-1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_artifacts.py tests/test_intensity_repository.py -v
```

Expected: FAIL because `artifacts.py` and `repository.py` do not exist.

- [ ] **Step 3: Implement Rasterio encoding and repository persistence**

Create `backend/app/intensity/artifacts.py`:

```python
from __future__ import annotations

import hashlib
import json
from io import BytesIO

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app.intensity.domain import GridDefinition


class RasterCodec:
    @staticmethod
    def encode(
        definition: GridDefinition,
        bands: list[tuple[str, np.ndarray]],
        band_manifest: dict,
    ) -> bytes:
        if not bands:
            raise ValueError("at least one raster band is required")
        expected = (definition.height, definition.width)
        for _, values in bands:
            if values.shape != expected:
                raise ValueError("raster band shape does not match grid definition")
        transform = from_origin(
            definition.origin_x,
            definition.origin_y,
            definition.resolution_m,
            definition.resolution_m,
        )
        profile = {
            "driver": "GTiff",
            "height": definition.height,
            "width": definition.width,
            "count": len(bands),
            "dtype": "float64",
            "crs": definition.crs,
            "transform": transform,
            "compress": "deflate",
        }
        with MemoryFile() as memory:
            with memory.open(**profile) as dataset:
                for index, (_, values) in enumerate(bands, start=1):
                    dataset.write(np.asarray(values, dtype=np.float64), index)
                dataset.update_tags(
                    grid_definition_version=definition.version,
                    band_manifest=json.dumps(
                        band_manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            return memory.read()

    @staticmethod
    def decode(payload: bytes) -> tuple[list[np.ndarray], dict]:
        with rasterio.io.MemoryFile(payload) as memory:
            with memory.open() as dataset:
                bands = [
                    dataset.read(index)
                    for index in range(1, dataset.count + 1)
                ]
                metadata = {
                    "crs": dataset.crs.to_string(),
                    "width": dataset.width,
                    "height": dataset.height,
                    "grid_definition_version": dataset.tags().get(
                        "grid_definition_version"
                    ),
                }
        return bands, metadata

    @staticmethod
    def checksum(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()
```

Create `backend/app/intensity/repository.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.intensity.artifacts import RasterCodec
from app.intensity.domain import (
    GridDefinition,
    ProductStatus,
    ProductType,
)
from app.intensity.models import IntensityFieldProduct, IntensityRaster


@dataclass(frozen=True, slots=True)
class IntensityProductWrite:
    run_id: UUID
    task_id: UUID
    product_type: ProductType
    status: ProductStatus
    algorithm_version: str
    parameter_version: str
    strategy_version: str | None
    grid_definition: GridDefinition
    region_profile_version: str
    input_fingerprint: str
    input_checksum: str
    quality_grade: str | None
    coverage_ratio: float
    statistics: dict
    source_product_id: str | None
    observed_at: datetime | None
    bands: list[tuple[str, np.ndarray]]


class IntensityRepository:
    async def save_product(
        self,
        session: AsyncSession,
        write: IntensityProductWrite,
    ) -> UUID:
        existing = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == write.run_id,
                IntensityFieldProduct.product_type == write.product_type.value,
            )
        )
        now = datetime.now(UTC)
        if existing is not None:
            if existing.input_fingerprint != write.input_fingerprint:
                raise ValueError("published product fingerprint cannot be overwritten")
            return existing.id

        product_id = uuid4()
        product = IntensityFieldProduct(
            id=product_id,
            run_id=write.run_id,
            task_id=write.task_id,
            product_type=write.product_type.value,
            status=write.status.value,
            algorithm_version=write.algorithm_version,
            parameter_version=write.parameter_version,
            strategy_version=write.strategy_version,
            grid_definition_version=write.grid_definition.version,
            region_profile_version=write.region_profile_version,
            input_fingerprint=write.input_fingerprint,
            input_checksum=write.input_checksum,
            output_checksum=None,
            quality_grade=write.quality_grade,
            coverage_ratio=write.coverage_ratio,
            spatial_extent=None,
            statistics=write.statistics,
            source_product_id=write.source_product_id,
            observed_at=write.observed_at,
            created_at=now,
            completed_at=None,
            published_at=None,
        )
        session.add(product)

        if write.bands:
            manifest = {
                "bands": [
                    {"number": index, "name": name}
                    for index, (name, _) in enumerate(write.bands, start=1)
                ]
            }
            payload = RasterCodec.encode(
                write.grid_definition,
                write.bands,
                manifest,
            )
            checksum = RasterCodec.checksum(payload)
            srid = CRS.from_user_input(write.grid_definition.crs).to_epsg()
            if srid is None:
                raise ValueError("grid CRS must map to an EPSG code")
            await session.execute(
                text(
                    """
                    INSERT INTO intensity_rasters (
                        id,
                        product_id,
                        rast,
                        band_manifest,
                        checksum,
                        width,
                        height,
                        srid,
                        created_at
                    )
                    VALUES (
                        :id,
                        :product_id,
                        ST_FromGDALRaster(:payload),
                        CAST(:manifest AS jsonb),
                        :checksum,
                        :width,
                        :height,
                        :srid,
                        :created_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "product_id": product_id,
                    "payload": payload,
                    "manifest": json.dumps(manifest),
                    "checksum": checksum,
                    "width": write.grid_definition.width,
                    "height": write.grid_definition.height,
                    "srid": srid,
                    "created_at": now,
                },
            )
            await session.execute(
                text(
                    """
                    UPDATE intensity_field_products
                    SET output_checksum = :checksum,
                        completed_at = :now,
                        spatial_extent = (
                            SELECT ST_Transform(ST_Envelope(r.rast), 4326)
                            FROM intensity_rasters r
                            WHERE r.product_id = :product_id
                        )
                    WHERE id = :product_id
                    """
                ),
                {
                    "checksum": checksum,
                    "now": now,
                    "product_id": product_id,
                },
            )
            product.output_checksum = checksum
            product.completed_at = now
        else:
            product.completed_at = now
        return product_id

    async def get_product(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        product_type: ProductType,
    ) -> dict | None:
        row = (
            await session.execute(
                select(IntensityFieldProduct).where(
                    IntensityFieldProduct.run_id == run_id,
                    IntensityFieldProduct.product_type == product_type.value,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "product_type": row.product_type,
            "status": row.status,
            "quality_grade": row.quality_grade,
            "coverage_ratio": float(row.coverage_ratio),
            "output_checksum": row.output_checksum,
            "statistics": dict(row.statistics),
        }

    async def load_raster(
        self,
        session: AsyncSession,
        product_id: UUID,
    ) -> tuple[list[np.ndarray], dict]:
        payload = await session.scalar(
            text(
                """
                SELECT ST_AsGDALRaster(rast, 'GTiff')
                FROM intensity_rasters
                WHERE product_id = :product_id
                """
            ),
            {"product_id": product_id},
        )
        if payload is None:
            raise LookupError("intensity raster not found")
        bands, metadata = RasterCodec.decode(bytes(payload))
        product = await session.get(IntensityFieldProduct, product_id)
        if product is None:
            raise LookupError("intensity product not found")
        metadata["grid_definition_version"] = product.grid_definition_version
        metadata["bands"] = (
            await session.scalar(
                text(
                    """
                    SELECT band_manifest
                    FROM intensity_rasters
                    WHERE product_id = :product_id
                    """
                ),
                {"product_id": product_id},
            )
        )["bands"]
        return bands, metadata
```

`spatial_extent` is nullable in the ORM for unavailable products. Set it for
raster products in a later task by computing `ST_Envelope` from the raster and
updating the product in the same transaction.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_artifacts.py tests/test_intensity_repository.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity/artifacts.py app/intensity/repository.py tests/test_intensity_artifacts.py tests/test_intensity_repository.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intensity/artifacts.py backend/app/intensity/repository.py backend/tests/test_intensity_artifacts.py backend/tests/test_intensity_repository.py
git commit -m "feat: persist intensity raster products"
```

### Task 8: Assessment Task Lifecycle, Idempotency, and Effective Versions

**Files:**
- Modify: `backend/app/assessment/repository.py`
- Create: `backend/app/intensity/service.py`
- Create: `backend/tests/test_assessment_task_lifecycle.py`
- Create: `backend/tests/test_intensity_service.py`

**Interfaces:**
- Consumes: `AssessmentRepository`, `IntensityRepository`, `ParameterBundle`, `GridDefinition`, `GridSamples`, `InstrumentIntensityProvider`.
- Produces:
  - `AssessmentRepository.start_task(session, run_id, task_key, input_fingerprint) -> AssessmentTask`
  - `AssessmentRepository.complete_task(session, task_id, output_checksum: str | None, result) -> AssessmentTask`
  - `AssessmentRepository.fail_task(session, task_id, error_category, error_summary) -> AssessmentTask`
  - `AssessmentRepository.complete_run(session, run_id, algorithm_bundle_version) -> AssessmentRun`
  - `AssessmentRepository.fail_run(session, run_id, error_summary) -> AssessmentRun`
  - `IntensityService.run_model(run_id: str) -> IntensityTaskOutcome`
  - `IntensityService.run_instrument(run_id: str) -> IntensityTaskOutcome`
  - `IntensityService.run_fusion(run_id: str) -> IntensityTaskOutcome`
  - `IntensityTaskOutcome`
  - `DirectionInputs`
  - `DirectionInputProvider` protocol
  - `NoDirectionInputProvider`

- [ ] **Step 1: Write failing lifecycle tests**

Create `backend/tests/test_assessment_task_lifecycle.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.models import AssessmentTaskAttempt
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_lifecycle_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTaskAttempt))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _seed_run(session_factory) -> AssessmentRun:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="LIFECYCLE-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=121.5,
        latitude=31.2,
        depth_km=10,
        magnitude=5.2,
        place="test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "LIFECYCLE-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=0,
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            return await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )


async def test_task_attempts_are_idempotent(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            first = await repository.start_task(
                session,
                run.id,
                "intensity.model",
                "f" * 64,
            )
            second = await repository.start_task(
                session,
                run.id,
                "intensity.model",
                "f" * 64,
            )
            assert first.id == second.id
            assert first.attempt_count == 1

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(AssessmentTaskAttempt).where(
                    AssessmentTaskAttempt.task_id == first.id
                )
            )
        ).all()

    assert len(attempts) == 1


async def test_completed_run_sets_deadline_and_effective_pointer(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    assert run.deadline_basis_at == datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    assert run.deadline_at == datetime(2026, 9, 27, 1, 8, tzinfo=UTC)

    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            await repository.complete_task(
                session,
                task.id,
                "a" * 64,
                {"product_id": "p"},
            )
            fusion = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.fusion",
                )
            )
            await repository.complete_task(
                session,
                fusion.id,
                "f" * 64,
                {"product_id": "fusion"},
            )
            completed = await repository.complete_run(
                session,
                run.id,
                "bundle-1",
            )
            event = await session.get(EarthquakeEvent, run.event_id)

    assert completed.status == "completed"
    assert event.effective_assessment_run_id == run.id


```

Create `backend/tests/test_intensity_service.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import numpy as np

from app.intensity.domain import (
    GridDefinition,
    InstrumentProduct,
    InstrumentQuality,
    ProductStatus,
)
from app.intensity.service import IntensityService
from app.intensity.parameters import load_parameter_bundle


class FakeInstrumentProvider:
    async def fetch(self, request):
        return InstrumentProduct(
            status=ProductStatus.AVAILABLE,
            product_id="instrument-fixture",
            product_version="1",
            observed_at=datetime(2026, 9, 27, tzinfo=UTC),
            source="fixture",
            grid_version=request.definition.version,
            values=np.full((request.definition.height, request.definition.width), 5.0),
            sigma=np.full((request.definition.height, request.definition.width), 0.2),
            quality_codes=np.full(
                (request.definition.height, request.definition.width),
                InstrumentQuality.Q1,
                dtype=object,
            ),
            coverage_ratio=1.0,
        )


def test_service_exposes_injected_grid_and_provider() -> None:
    definition = GridDefinition("grid-test", "EPSG:32651", 1000, 0, 1000, 1, 1)
    service = IntensityService(
        session_factory=object(),
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=definition,
        instrument_provider=FakeInstrumentProvider(),
    )

    assert service.grid_definition.version == "grid-test"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_task_lifecycle.py tests/test_intensity_service.py -v
```

Expected: FAIL because lifecycle methods and `IntensityService` are missing.

- [ ] **Step 3: Implement lifecycle methods and service foundation**

Modify `backend/app/assessment/repository.py`:

- Remove the existing `deadline_seconds = max(...)` run-level calculation.
- Change the datetime import to
  `from datetime import UTC, datetime, timedelta`.
- In `ensure_run_and_tasks`, derive:

```python
basis_at = revision.ingested_at or canonical.t1_at
deadline_at = basis_at + timedelta(seconds=300)
```

- Set `report_ingested_at=basis_at`, `deadline_basis_at=basis_at`,
  `deadline_at=deadline_at`, and `started_at=outbox.created_at`.
- Create each `AssessmentTask.deadline_at` from
  `basis_at + timedelta(seconds=task.deadline_offset_seconds)`, not from the
  original `canonical.t1_at`.
- Add and flush the new run first so its `id` exists, then set
  `canonical.latest_assessment_run_id = run.id` and mark older runs as
  superseded. Do not use `run.id` before the flush:

```python
session.add(run)
await session.flush()
canonical.latest_assessment_run_id = run.id
await session.execute(
    update(AssessmentRun)
    .where(
        AssessmentRun.event_id == canonical.id,
        AssessmentRun.id != run.id,
        AssessmentRun.superseded_at.is_(None),
    )
    .values(superseded_by_run_id=run.id, superseded_at=outbox.created_at)
)
```

Add methods with these exact signatures:

```python
async def start_task(
    self,
    session: AsyncSession,
    run_id: UUID,
    task_key: str,
    input_fingerprint: str,
) -> AssessmentTask:
    task = await session.scalar(
        select(AssessmentTask)
        .where(
            AssessmentTask.run_id == run_id,
            AssessmentTask.task_key == task_key,
        )
        .with_for_update()
    )
    if task is None:
        raise LookupError("assessment task not found")
    if task.status == "succeeded":
        return task
    if task.input_fingerprint not in {None, input_fingerprint}:
        raise ValueError("task input fingerprint changed")
    task.status = "running"
    task.input_fingerprint = input_fingerprint
    task.started_at = task.started_at or datetime.now(UTC)
    task.attempt_count += 1
    session.add(
        AssessmentTaskAttempt(
            task_id=task.id,
            attempt_number=task.attempt_count,
            status="running",
            started_at=datetime.now(UTC),
            input_fingerprint=input_fingerprint,
        )
    )
    return task


async def complete_task(
    self,
    session: AsyncSession,
    task_id: UUID,
    output_checksum: str | None,
    result: dict,
) -> AssessmentTask:
    task = await session.get(AssessmentTask, task_id, with_for_update=True)
    if task is None:
        raise LookupError("assessment task not found")
    now = datetime.now(UTC)
    task.status = "succeeded"
    task.output_checksum = output_checksum
    task.result = result
    task.completed_at = now
    task.last_error = None
    attempt = await session.scalar(
        select(AssessmentTaskAttempt).where(
            AssessmentTaskAttempt.task_id == task.id,
            AssessmentTaskAttempt.attempt_number == task.attempt_count,
        )
    )
    if attempt is not None:
        attempt.status = "succeeded"
        attempt.output_checksum = output_checksum
        attempt.completed_at = now
    return task


async def fail_task(
    self,
    session: AsyncSession,
    task_id: UUID,
    error_category: str,
    error_summary: str,
) -> AssessmentTask:
    task = await session.get(AssessmentTask, task_id, with_for_update=True)
    if task is None:
        raise LookupError("assessment task not found")
    now = datetime.now(UTC)
    task.status = "failed"
    task.completed_at = now
    task.last_error = error_summary[:2000]
    attempt = await session.scalar(
        select(AssessmentTaskAttempt).where(
            AssessmentTaskAttempt.task_id == task.id,
            AssessmentTaskAttempt.attempt_number == task.attempt_count,
        )
    )
    if attempt is not None:
        attempt.status = "failed"
        attempt.error_category = error_category
        attempt.error_summary = error_summary[:2000]
        attempt.completed_at = now
    return task


async def complete_run(
    self,
    session: AsyncSession,
    run_id: UUID,
    algorithm_bundle_version: str,
) -> AssessmentRun:
    run = await session.get(AssessmentRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("assessment run not found")
    required_tasks = (
        await session.scalars(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run.id,
                AssessmentTask.task_key.in_(("intensity.model", "intensity.fusion")),
            )
        )
    ).all()
    if {
        task.task_key for task in required_tasks if task.status == "succeeded"
    } != {"intensity.model", "intensity.fusion"}:
        raise ValueError("required intensity tasks have not succeeded")
    now = datetime.now(UTC)
    run.status = "completed"
    run.completed_at = now
    started_at = run.started_at or run.created_at
    run.duration_ms = int((now - started_at).total_seconds() * 1000)
    run.algorithm_bundle_version = algorithm_bundle_version
    event = await session.get(EarthquakeEvent, run.event_id, with_for_update=True)
    if event is not None and event.latest_assessment_run_id == run.id:
        event.effective_assessment_run_id = run.id
    return run


async def fail_run(
    self,
    session: AsyncSession,
    run_id: UUID,
    error_summary: str,
) -> AssessmentRun:
    run = await session.get(AssessmentRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("assessment run not found")
    run.status = "failed"
    run.completed_at = datetime.now(UTC)
    run.last_error = error_summary[:2000]
    return run


async def mark_superseded(
    self,
    session: AsyncSession,
    run_id: UUID,
    successor_run_id: UUID,
) -> None:
    run = await session.get(AssessmentRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("assessment run not found")
    run.superseded_by_run_id = successor_run_id
    run.superseded_at = datetime.now(UTC)
```

When `ensure_run_and_tasks` creates a new run, set `started_at=outbox.created_at`.
Do not set `completed_at` or the effective pointer before successful completion.

Create `backend/app/intensity/service.py` with the service contract and
injectable dependencies. A fixed grid is allowed only in tests; production
resolves the grid from the versioned region boundary:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.events.models import EarthquakeRevision
from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    GridDefinition,
    InstrumentProduct,
    IntensityEventSnapshot,
    ModelField,
    ProductStatus,
    ProductType,
)
from app.intensity.fusion import fuse
from app.intensity.grid import sample_grid
from app.intensity.instrument import (
    InstrumentRequest,
    InstrumentIntensityProvider,
    UnavailableInstrumentProvider,
    validate_instrument_product,
)
from app.intensity.model import evaluate_model
from app.intensity.parameters import ParameterBundle
from app.intensity.region import RegionProfile, load_region_profile
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.intensity.model import FaultCandidate, resolve_direction


@dataclass(frozen=True, slots=True)
class IntensityTaskOutcome:
    run_id: str
    task_key: str
    status: str
    product_id: str | None
    output_checksum: str | None


@dataclass(frozen=True, slots=True)
class DirectionInputs:
    override_deg: float | None = None
    focal_mechanism_deg: float | None = None
    finite_fault_deg: float | None = None
    candidates: tuple[FaultCandidate, ...] = ()


class DirectionInputProvider(Protocol):
    async def load(self, session, revision: EarthquakeRevision) -> DirectionInputs: ...


class NoDirectionInputProvider:
    async def load(
        self,
        session,
        revision: EarthquakeRevision,
    ) -> DirectionInputs:
        return DirectionInputs()


class IntensityService:
    MODEL_ALGORITHM_VERSION = "model-axis-ratio-v1"
    FUSION_ALGORITHM_VERSION = "fusion-inverse-variance-v1"

    def __init__(
        self,
        *,
        session_factory,
        parameters: ParameterBundle,
        region_profile: RegionProfile | None = None,
        fixed_grid_definition: GridDefinition | None = None,
        instrument_provider: InstrumentIntensityProvider | None = None,
        direction_provider: DirectionInputProvider | None = None,
        assessment_repository: AssessmentRepository | None = None,
        intensity_repository: IntensityRepository | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.parameters = parameters
        self.region_profile = region_profile or load_region_profile(
            settings.intensity_region_profile_path
        )
        if self.region_profile.model_parameter_version != parameters.version:
            raise ValueError("region profile model parameter version mismatch")
        if self.region_profile.fusion_strategy_version != self.FUSION_ALGORITHM_VERSION:
            raise ValueError("region profile fusion strategy version mismatch")
        self.instrument_provider = instrument_provider or UnavailableInstrumentProvider()
        self.direction_provider = direction_provider or NoDirectionInputProvider()
        self.assessment_repository = assessment_repository or AssessmentRepository()
        self.intensity_repository = intensity_repository or IntensityRepository()
        self._fixed_grid_definition = fixed_grid_definition

    @property
    def grid_definition(self) -> GridDefinition:
        if self._fixed_grid_definition is None:
            raise RuntimeError("grid definition has not been resolved")
        return self._fixed_grid_definition

    async def _load_context(self, session, run_id: str):
        try:
            run_uuid = UUID(run_id)
        except ValueError as exc:
            raise ValueError("run_id must be a UUID") from exc
        run = await session.get(AssessmentRun, run_uuid)
        if run is None:
            raise LookupError("assessment run not found")
        revision = await session.get(EarthquakeRevision, run.revision_id)
        if revision is None:
            raise LookupError("earthquake revision not found")
        return run, revision

    async def _direction(
        self,
        session,
        revision: EarthquakeRevision,
    ) -> DirectionDecision:
        inputs = await self.direction_provider.load(session, revision)
        return resolve_direction(
            override_deg=inputs.override_deg,
            focal_mechanism_deg=inputs.focal_mechanism_deg,
            finite_fault_deg=inputs.finite_fault_deg,
            candidates=inputs.candidates,
        )
```

Tasks 9 and 10 add the three complete runner methods and real grid resolution;
do not leave these methods as `pass` or placeholder returns.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_task_lifecycle.py tests/test_intensity_service.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/assessment/repository.py app/intensity/service.py tests/test_assessment_task_lifecycle.py tests/test_intensity_service.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/assessment/repository.py backend/app/intensity/service.py backend/tests/test_assessment_task_lifecycle.py backend/tests/test_intensity_service.py
git commit -m "feat: add assessment task lifecycle"
```

### Task 9: Complete Intensity Service and Grid Resolution

**Files:**
- Modify: `backend/app/intensity/service.py`
- Modify: `backend/app/intensity/repository.py`
- Create: `backend/tests/test_intensity_service_execution.py`

**Interfaces:**
- Consumes: Task 8 `IntensityService`.
- Produces:
  - `IntensityRepository.resolve_grid_definition(session, profile, boundary_version) -> GridDefinition`
  - `IntensityRepository.load_product_arrays(session, run_id, product_type) -> ProductArrays | None`
  - `IntensityService.run_model(run_id: str) -> IntensityTaskOutcome`
  - `IntensityService.run_instrument(run_id: str) -> IntensityTaskOutcome`
  - `IntensityService.run_fusion(run_id: str) -> IntensityTaskOutcome`

- [ ] **Step 1: Write failing service execution tests**

Create `backend/tests/test_intensity_service_execution.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import (
    FusionMode,
    FusionQuality,
    GridDefinition,
    InstrumentProduct,
    ProductStatus,
    ProductType,
)
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
from app.regions.domain import RegionContext


class UnavailableProvider:
    async def fetch(self, request):
        return InstrumentProduct(
            status=ProductStatus.UNAVAILABLE,
            product_id=None,
            product_version=None,
            observed_at=None,
            source="unavailable",
            grid_version=request.definition.version,
            values=None,
            sigma=None,
            quality_codes=None,
            coverage_ratio=0.0,
        )


@pytest.fixture(autouse=True)
async def clean_execution_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _seed_run(session_factory) -> str:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="EXECUTION-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "EXECUTION-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=0,
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return str(run.id)


async def test_three_tasks_produce_model_only_fusion(session_factory) -> None:
    run_id = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-test",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=UnavailableProvider(),
    )

    model = await service.run_model(run_id)
    instrument = await service.run_instrument(run_id)
    fusion = await service.run_fusion(run_id)

    assert model.status == "succeeded"
    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"

    async with session_factory() as session:
        fusion_product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == model.run_id,
                IntensityFieldProduct.product_type == ProductType.FUSION.value,
            )
        )

    assert fusion_product is not None
    assert fusion_product.status == "available"
    assert fusion_product.quality_grade == FusionQuality.F3.value
    assert fusion_product.statistics["fusion_mode"] == FusionMode.MODEL_ONLY.value
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_service_execution.py -v
```

Expected: FAIL because `run_model`, `run_instrument`, and `run_fusion` are incomplete.

- [ ] **Step 3: Implement production grid resolution and three runner methods**

Add to `IntensityRepository`:

```python
import hashlib
import json

from pyproj import CRS
from sqlalchemy import func

from app.intensity.grid import grid_definition_from_bounds
from app.intensity.region import RegionProfile
from app.regions.models import RegionBoundary


async def resolve_grid_definition(
    self,
    session: AsyncSession,
    profile: RegionProfile,
    boundary_version: str,
) -> GridDefinition:
    boundary = await session.scalar(
        select(RegionBoundary).where(RegionBoundary.version == boundary_version)
    )
    if boundary is None:
        raise LookupError("region boundary version not found")
    target_srid = CRS.from_user_input(profile.grid_crs).to_epsg()
    if target_srid is None:
        raise ValueError("region profile grid CRS must map to an EPSG code")
    transformed = func.ST_Transform(RegionBoundary.geom, target_srid)
    buffered = func.ST_Buffer(transformed, profile.grid_buffer_km * 1000)
    envelope = func.ST_Envelope(buffered)
    row = (
        await session.execute(
            select(
                func.ST_XMin(envelope).label("min_x"),
                func.ST_YMin(envelope).label("min_y"),
                func.ST_XMax(envelope).label("max_x"),
                func.ST_YMax(envelope).label("max_y"),
            ).where(RegionBoundary.id == boundary.id)
        )
    ).one()
    provisional = grid_definition_from_bounds(
        min_x=float(row.min_x),
        min_y=float(row.min_y),
        max_x=float(row.max_x),
        max_y=float(row.max_y),
        resolution_m=profile.grid_resolution_m,
        crs=profile.grid_crs,
        version="pending",
    )
    identity = {
        "region_profile_version": profile.version,
        "boundary_version": boundary_version,
        "min_x": provisional.origin_x,
        "min_y": provisional.origin_y - provisional.height * provisional.resolution_m,
        "max_x": provisional.origin_x + provisional.width * provisional.resolution_m,
        "max_y": provisional.origin_y,
        "resolution_m": provisional.resolution_m,
        "crs": provisional.crs,
    }
    checksum = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return GridDefinition(
        version=f"{profile.version}:{boundary.version}:{checksum[:16]}",
        crs=provisional.crs,
        resolution_m=provisional.resolution_m,
        origin_x=provisional.origin_x,
        origin_y=provisional.origin_y,
        width=provisional.width,
        height=provisional.height,
    )
```

Add product-array loading:

```python
@dataclass(frozen=True, slots=True)
class ProductArrays:
    product_id: UUID
    product_type: str
    status: str
    bands: dict[str, np.ndarray]
    statistics: dict


async def load_product_arrays(
    self,
    session: AsyncSession,
    *,
    run_id: UUID,
    product_type: ProductType,
) -> ProductArrays | None:
    product = await session.scalar(
        select(IntensityFieldProduct).where(
            IntensityFieldProduct.run_id == run_id,
            IntensityFieldProduct.product_type == product_type.value,
        )
    )
    if product is None:
        return None
    if product.status not in {
        ProductStatus.AVAILABLE.value,
        ProductStatus.PARTIAL.value,
    }:
        return ProductArrays(
            product_id=product.id,
            product_type=product.product_type,
            status=product.status,
            bands={},
            statistics=dict(product.statistics),
        )
    raster_bands, manifest = await self.load_raster(session, product.id)
    names = [
        item["name"]
        for item in manifest.get("bands", [])
    ]
    if len(names) != len(raster_bands):
        raise ValueError("raster band manifest does not match raster")
    return ProductArrays(
        product_id=product.id,
        product_type=product.product_type,
        status=product.status,
        bands=dict(zip(names, raster_bands, strict=True)),
        statistics=dict(product.statistics),
    )
```

`load_raster` must return `manifest["bands"]` as metadata. Modify its return
construction to decode the stored `band_manifest`:

```python
manifest = await session.scalar(
    text(
        """
        SELECT band_manifest
        FROM intensity_rasters
        WHERE product_id = :product_id
        """
    ),
    {"product_id": product_id},
)
metadata["bands"] = manifest["bands"]
```

Complete `IntensityService` with exact helpers:

```python
async def run_model(self, run_id: str) -> IntensityTaskOutcome:
    try:
        async with self.session_factory() as session:
            async with session.begin():
                run, revision = await self._load_context(session, run_id)
                definition = await self._ensure_grid(session, run)
                task = await self.assessment_repository.start_task(
                    session,
                    run.id,
                    "intensity.model",
                    self._fingerprint(run, revision, definition),
                )
                samples = sample_grid(
                    definition,
                    epicenter_longitude=float(revision.longitude),
                    epicenter_latitude=float(revision.latitude),
                )
                direction = await self._direction(session, revision)
                model = evaluate_model(
                    IntensityEventSnapshot(
                        event_id=str(run.event_id),
                        revision_id=str(run.revision_id),
                        magnitude=float(revision.magnitude),
                        longitude=float(revision.longitude),
                        latitude=float(revision.latitude),
                        report_ingested_at=run.report_ingested_at,
                    ),
                    samples,
                    direction,
                    self.parameters.model,
                )
                product_id = await self.intensity_repository.save_product(
                    session,
                    IntensityProductWrite(
                        run_id=run.id,
                        task_id=task.id,
                        product_type=ProductType.MODEL,
                        status=ProductStatus.AVAILABLE,
                        algorithm_version=self.MODEL_ALGORITHM_VERSION,
                        parameter_version=self.parameters.version,
                        strategy_version=None,
                        grid_definition=definition,
                        region_profile_version=self.region_profile.version,
                        input_fingerprint=task.input_fingerprint,
                        input_checksum=task.input_fingerprint,
                        quality_grade=None,
                        coverage_ratio=1.0,
                        statistics={
                            **_statistics(model.values),
                            "model_extrapolated": model.extrapolated,
                            "direction_status": model.direction.status.value,
                            "direction_source": model.direction.source,
                            "direction_strike_deg": model.direction.strike_deg,
                        },
                        source_product_id=None,
                        observed_at=revision.ingested_at,
                        bands=[("value", model.values), ("sigma", model.sigma)],
                    ),
                )
                product = await self.intensity_repository.get_product(
                    session,
                    run_id=run.id,
                    product_type=ProductType.MODEL,
                )
                output_checksum = (
                    product["output_checksum"] if product is not None else None
                )
                await self.assessment_repository.complete_task(
                    session,
                    task.id,
                    output_checksum,
                    {
                        "product_id": str(product_id),
                        "grid_definition_version": definition.version,
                    },
                )
                await self.assessment_repository.mark_deadline_exceeded(
                    session,
                    run.id,
                    datetime.now(UTC),
                )
                return IntensityTaskOutcome(
                    run_id=str(run.id),
                    task_key=task.task_key,
                    status="succeeded",
                    product_id=str(product_id),
                    output_checksum=output_checksum,
                )
    except Exception as exc:
        try:
            await self._record_task_failure(run_id, "intensity.model", exc)
        except Exception:
            pass
        raise
```

Implement `run_instrument` with:

```python
product = await self.instrument_provider.fetch(
    InstrumentRequest(
        event_id=str(run.event_id),
        revision_id=str(run.revision_id),
        definition=definition,
        observed_at=run.report_ingested_at,
    )
)
product = validate_instrument_product(product, definition)
```

For an unavailable product, persist a product row with `status=unavailable`,
`bands=[]`, `coverage_ratio=0`, and complete the task successfully. For a valid
product, persist value, sigma, and quality bands. If provider execution raises,
persist an unavailable product with reason `provider_error:<type>`, complete the
instrument task successfully as degraded, and do not re-raise.
If `validate_instrument_product` raises `ValueError`, persist an
`invalid` instrument product with the validation reason, complete the task
successfully, and let fusion fall back to `model_only`. Instrument validation
errors are degradation events, not model-chain failures.
Include `source`, `product_version`, `observed_at`, `generated_at`,
`grid_definition_version`, `coverage_ratio`, `raw_checksum`, and
`normalized_checksum` in the product statistics so fusion can reconstruct the
instrument metadata without loading the provider again.

Implement `run_fusion` with:

```python
model_arrays = await self.intensity_repository.load_product_arrays(
    session,
    run_id=run.id,
    product_type=ProductType.MODEL,
)
instrument_arrays = await self.intensity_repository.load_product_arrays(
    session,
    run_id=run.id,
    product_type=ProductType.INSTRUMENT,
)
fusion_task = await self.assessment_repository.start_task(
    session,
    run.id,
    "intensity.fusion",
    self._fingerprint(
        run,
        revision,
        definition,
        extra={
            "instrument_product_id": (
                str(instrument_arrays.product_id)
                if instrument_arrays is not None
                else None
            ),
            "instrument_product_checksum": (
                instrument_arrays.statistics.get("normalized_checksum")
                if instrument_arrays is not None
                else None
            ),
            "instrument_status": (
                instrument_arrays.status if instrument_arrays is not None else None
            ),
        },
    ),
)
if model_arrays is None:
    raise LookupError("model intensity product is required before fusion")
model = ModelField(
    values=model_arrays.bands["value"],
    sigma=model_arrays.bands["sigma"],
    extrapolated=bool(model_arrays.statistics["model_extrapolated"]),
    direction=DirectionDecision(
        DirectionStatus(model_arrays.statistics["direction_status"]),
        model_arrays.statistics.get("direction_source"),
        model_arrays.statistics.get("direction_strike_deg"),
    ),
)
instrument = _instrument_from_arrays(instrument_arrays)
fusion = fuse(model, instrument, self.parameters.fusion)
```

Persist fusion bands in this exact order:

```python
[
    ("value", fusion.values),
    ("sigma", fusion.sigma),
    ("p10", fusion.p10),
    ("p90", fusion.p90),
    ("model_weight", fusion.model_weight),
    ("instrument_weight", fusion.instrument_weight),
    ("quality_code", fusion.quality_codes),
    ("model_value", model.values),
    ("instrument_value", instrument_values_or_nan),
]
```

Set `quality_grade=fusion.quality.value`, `coverage_ratio=fusion.coverage_ratio`,
`strategy_version=self.parameters.version`, and statistics containing
`fusion_mode`, `instrument_status`, min/max/mean and model extrapolation metadata.
As in `run_model`, call `get_product` after saving and pass the raster
`output_checksum` to `complete_task`; never use the product UUID as its
checksum.

All three runners use the same outer `try/except` shape as `run_model`: the
working transaction must exit before `_record_task_failure` opens its fresh
session. Never call `fail_task` inside a transaction that is about to re-raise,
because SQLAlchemy will roll that failure audit back.

Helpers:

```python
async def _record_task_failure(
    self,
    run_id: str,
    task_key: str,
    exc: Exception,
) -> None:
    try:
        run_uuid = UUID(run_id)
    except ValueError:
        return
    async with self.session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_uuid,
                    AssessmentTask.task_key == task_key,
                )
            )
            if task is not None:
                await self.assessment_repository.fail_task(
                    session,
                    task.id,
                    type(exc).__name__,
                    _safe_error(exc),
                )


def _instrument_from_arrays(self, arrays):
    if arrays is None:
        return None
    required = {"value", "sigma", "quality_code"}
    if arrays.status == ProductStatus.UNAVAILABLE.value or not required <= set(
        arrays.bands
    ):
        return None
    values = np.asarray(arrays.bands["value"], dtype=np.float64)
    sigma = np.asarray(arrays.bands["sigma"], dtype=np.float64)
    codes = np.asarray(arrays.bands["quality_code"], dtype=object)
    return InstrumentProduct(
        status=ProductStatus(arrays.status),
        product_id=str(arrays.product_id),
        product_version=arrays.statistics.get("product_version"),
        observed_at=_parse_optional_datetime(
            arrays.statistics.get("observed_at")
        ),
        source=arrays.statistics.get("source", "instrument"),
        grid_version=arrays.statistics.get("grid_definition_version"),
        values=values,
        sigma=sigma,
        quality_codes=codes,
        coverage_ratio=float(arrays.statistics.get("coverage_ratio", 0.0)),
        reason=arrays.statistics.get("reason"),
        generated_at=_parse_optional_datetime(
            arrays.statistics.get("generated_at")
        ),
        crs=arrays.statistics.get("crs"),
        resolution_m=arrays.statistics.get("resolution_m"),
        raw_checksum=arrays.statistics.get("raw_checksum"),
        normalized_checksum=arrays.statistics.get("normalized_checksum"),
    )


def _parse_optional_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _statistics(values):
    array = np.asarray(values, dtype=np.float64)
    return {
        "minimum": float(np.min(array)),
        "maximum": float(np.max(array)),
        "mean": float(np.mean(array)),
    }


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _fingerprint(self, run, revision, definition, *, extra=None):
    payload = {
        "run_id": str(run.id),
        "revision_id": str(revision.id),
        "magnitude": str(revision.magnitude),
        "longitude": str(revision.longitude),
        "latitude": str(revision.latitude),
        "grid_definition_version": definition.version,
        "parameter_version": self.parameters.version,
        "region_profile_version": self.region_profile.version,
        "region_profile_checksum": self.region_profile.checksum,
        "parameter_checksum": self.parameters.checksum,
        "extra": extra or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
```

`_ensure_grid` returns `self.grid_definition` when a fixed definition is
provided; otherwise it calls
`self.intensity_repository.resolve_grid_definition(session, self.region_profile, run.snapshot["region_boundary_version"])`.
Store the resolved grid version in the task result and product metadata.

```python
async def _ensure_grid(self, session, run) -> GridDefinition:
    if self._fixed_grid_definition is not None:
        return self._fixed_grid_definition
    boundary_version = (
        run.snapshot.get("region_boundary_version")
        or self.region_profile.boundary_version
    )
    if not isinstance(boundary_version, str) or not boundary_version:
        raise ValueError("run region boundary version is required")
    return await self.intensity_repository.resolve_grid_definition(
        session,
        self.region_profile,
        boundary_version,
    )
```

Model statistics must include:

```python
{
    **_statistics(model.values),
    "model_extrapolated": model.extrapolated,
    "direction_status": model.direction.status.value,
    "direction_source": model.direction.source,
    "direction_strike_deg": model.direction.strike_deg,
}
```

- [ ] **Step 4: Run execution tests and all pure intensity tests**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_service_execution.py tests/test_intensity_service.py tests/test_intensity_model.py tests/test_intensity_fusion.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity tests/test_intensity_service_execution.py tests/test_intensity_service.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/intensity/service.py backend/app/intensity/repository.py backend/tests/test_intensity_service_execution.py backend/tests/test_intensity_service.py
git commit -m "feat: execute intensity assessment tasks"
```

### Task 10: Temporal Workflow and Activity Orchestration

**Files:**
- Modify: `backend/app/assessment/temporal.py`
- Modify: `backend/app/assessment/worker.py`
- Modify: `backend/app/config.py`
- Modify: `backend/tests/test_assessment_temporal.py`
- Modify: `infra/compose.yaml`

**Interfaces:**
- Consumes: `IntensityService.run_model`, `run_instrument`, `run_fusion`.
- Produces:
  - `PreparedAssessment`
  - `IntensityActivityInput`
  - `AssessmentRunActivityInput`
  - `FinalizeAssessmentInput`
  - activities `run_intensity_model`, `run_intensity_instrument`,
    `run_intensity_fusion`, `mark_deadline_exceeded`, `finalize_assessment`
  - workflow execution timeout `ASSESSMENT_WORKFLOW_SAFETY_TIMEOUT_SECONDS`

- [ ] **Step 1: Write failing workflow tests**

Extend `backend/tests/test_assessment_temporal.py` with:

```python
async def test_intensity_workflow_executes_three_tasks_and_skips_deferred(
    session_factory,
) -> None:
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
    )

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-intensity-test",
            workflows=[AssessmentWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.mark_deadline_exceeded,
                activities.finalize_assessment,
            ],
        ):
            result = await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment-intensity:{request.event_id}",
                task_queue="assessment-intensity-test",
            )

    async with session_factory() as session:
        run = await session.get(AssessmentRun, result.run_id)
        tasks = (
            await session.scalars(
                select(AssessmentTask)
                .where(AssessmentTask.run_id == result.run_id)
                .order_by(AssessmentTask.sequence)
            )
        ).all()
        event = await session.get(EarthquakeEvent, request.event_id)

    assert run.status == "completed"
    assert run.completed_at is not None
    assert event.effective_assessment_run_id == run.id
    statuses = {task.task_key: task.status for task in tasks}
    assert statuses["intensity.model"] == "succeeded"
    assert statuses["intensity.instrument"] == "succeeded"
    assert statuses["intensity.fusion"] == "succeeded"
    assert statuses["loss.population"] == "skipped"
    assert statuses["workgroup.response_tasks"] == "skipped"
```

Update the existing `test_assessment_workflow_prepares_run_and_tasks` so its
`Worker` registers all six activities from the new workflow. Keep the existing
preparation assertions, then assert the returned run is `completed` because
the test worker now executes the complete intensity chain.

Add this test helper at module level:

```python
def _test_intensity_service(session_factory):
    from app.intensity.domain import GridDefinition
    from app.intensity.parameters import load_parameter_bundle
    from app.intensity.service import IntensityService

    return IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-temporal-test",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
    )
```

- [ ] **Step 2: Run workflow test to verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_temporal.py -v
```

Expected: FAIL because intensity activities and finalization do not exist.

- [ ] **Step 3: Implement workflow and activities**

Replace the current single-activity `AssessmentWorkflow` body with:

```python
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from temporalio.exceptions import ActivityError, ApplicationError

logger = logging.getLogger(__name__)


@workflow.defn
class AssessmentWorkflow:
    @workflow.run
    async def run(
        self,
        request: AssessmentWorkflowInput,
        ) -> AssessmentWorkflowResult:
        prepared: PreparedAssessment = await workflow.execute_activity(
            "prepare_assessment",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )
        deadline_task = asyncio.create_task(self._mark_deadline_when_due(prepared))
        try:
            model_handle = workflow.start_activity(
                "run_intensity_model",
                IntensityActivityInput(prepared.run_id, "intensity.model"),
                start_to_close_timeout=timedelta(seconds=1800),
                retry_policy=_retry_policy(),
            )
            instrument_handle = workflow.start_activity(
                "run_intensity_instrument",
                IntensityActivityInput(prepared.run_id, "intensity.instrument"),
                start_to_close_timeout=timedelta(seconds=1800),
                retry_policy=_retry_policy(),
            )
            model_result, _instrument_result = await asyncio.gather(
                model_handle,
                instrument_handle,
                return_exceptions=True,
            )

            if isinstance(model_result, BaseException):
                return await self._finalize(
                    prepared,
                    outcome="failed",
                )

            try:
                await workflow.execute_activity(
                    "run_intensity_fusion",
                    IntensityActivityInput(prepared.run_id, "intensity.fusion"),
                    start_to_close_timeout=timedelta(seconds=1800),
                    retry_policy=_retry_policy(),
                )
            except ActivityError:
                return await self._finalize(prepared, outcome="failed")
            return await self._finalize(prepared, outcome="completed")
        finally:
            if not deadline_task.done():
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)

    async def _mark_deadline_when_due(
        self,
        prepared: PreparedAssessment,
    ) -> None:
        delay = prepared.deadline_at - workflow.now()
        if delay.total_seconds() > 0:
            await workflow.sleep(delay)
        await workflow.execute_activity(
            "mark_deadline_exceeded",
            AssessmentRunActivityInput(prepared.run_id),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )

    async def _finalize(
        self,
        prepared: PreparedAssessment,
        *,
        outcome: str,
    ) -> AssessmentWorkflowResult:
        await workflow.execute_activity(
            "finalize_assessment",
            FinalizeAssessmentInput(prepared.run_id, outcome),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )
        return AssessmentWorkflowResult(
            run_id=prepared.run_id,
            task_count=prepared.task_count,
            status=outcome,
        )


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(seconds=2),
        maximum_interval=timedelta(seconds=8),
        backoff_coefficient=4.0,
    )
```

Add:

```python
@dataclass(frozen=True, slots=True)
class PreparedAssessment:
    run_id: str
    task_count: int
    deadline_at: datetime


@dataclass(frozen=True, slots=True)
class IntensityActivityInput:
    run_id: str
    task_key: str


@dataclass(frozen=True, slots=True)
class AssessmentRunActivityInput:
    run_id: str


@dataclass(frozen=True, slots=True)
class FinalizeAssessmentInput:
    run_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowResult:
    run_id: str
    task_count: int
    status: str
```

Extend `AssessmentActivities.__init__` with:

```python
def __init__(
    self,
    session_factory: object,
    *,
    intensity_service_factory: Callable[[], IntensityService] | None = None,
) -> None:
    self._session_factory = session_factory
    self._intensity_service_factory = (
        intensity_service_factory or self._default_intensity_service_factory
    )

def _default_intensity_service_factory(self) -> IntensityService:
    return IntensityService(
        session_factory=self._session_factory,
        parameters=load_parameter_bundle(settings.intensity_parameters_path),
    )
```

Add activities:

```python
@activity.defn(name="run_intensity_model")
async def run_intensity_model(self, request: IntensityActivityInput):
    service = self._intensity_service_factory()
    try:
        return await service.run_model(request.run_id)
    except (LookupError, ValueError, ArithmeticError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}"[:2000],
            type=type(exc).__name__,
            non_retryable=True,
        ) from exc


@activity.defn(name="run_intensity_instrument")
async def run_intensity_instrument(self, request: IntensityActivityInput):
    service = self._intensity_service_factory()
    return await service.run_instrument(request.run_id)


@activity.defn(name="run_intensity_fusion")
async def run_intensity_fusion(self, request: IntensityActivityInput):
    service = self._intensity_service_factory()
    try:
        return await service.run_fusion(request.run_id)
    except (LookupError, ValueError, ArithmeticError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}"[:2000],
            type=type(exc).__name__,
            non_retryable=True,
        ) from exc


@activity.defn(name="mark_deadline_exceeded")
async def mark_deadline_exceeded(self, request: AssessmentRunActivityInput):
    from app.assessment.repository import AssessmentRepository

    repository = AssessmentRepository()
    async with self._session_factory() as session:
        async with session.begin():
            marked = await repository.mark_deadline_exceeded(
                session,
                request.run_id,
                datetime.now(UTC),
            )
    if marked:
        logger.warning(
            "assessment deadline exceeded run_id=%s",
            request.run_id,
        )
    return marked


@activity.defn(name="finalize_assessment")
async def finalize_assessment(self, request: FinalizeAssessmentInput):
    from app.assessment.repository import AssessmentRepository

    repository = AssessmentRepository()
    async with self._session_factory() as session:
        async with session.begin():
            run = await repository.get_run(session, request.run_id)
            if run is None:
                raise LookupError("assessment run not found")
            await repository.mark_deadline_exceeded(
                session,
                run.id,
                datetime.now(UTC),
            )
            if request.outcome == "completed":
                await repository.skip_deferred_tasks(session, run.id)
                return await repository.complete_run(
                    session,
                    run.id,
                    algorithm_bundle_version="intensity-v1",
                )
            return await repository.fail_run(
                session,
                run.id,
                "assessment intensity chain failed",
            )
```

Add to `AssessmentRepository`:

```python
async def get_run(self, session: AsyncSession, run_id: str | UUID) -> AssessmentRun | None:
    try:
        identifier = run_id if isinstance(run_id, UUID) else UUID(run_id)
    except ValueError as exc:
        raise ValueError("run_id must be a UUID") from exc
    return await session.get(AssessmentRun, identifier)


async def mark_deadline_exceeded(
    self,
    session: AsyncSession,
    run_id: str | UUID,
    observed_at: datetime,
) -> bool:
    try:
        identifier = run_id if isinstance(run_id, UUID) else UUID(run_id)
    except ValueError as exc:
        raise ValueError("run_id must be a UUID") from exc
    run = await session.get(AssessmentRun, identifier, with_for_update=True)
    if run is None:
        raise LookupError("assessment run not found")
    normalized = observed_at.astimezone(UTC)
    if normalized > run.deadline_at and run.deadline_exceeded_at is None:
        run.deadline_exceeded_at = normalized
        return True
    return False


async def skip_deferred_tasks(self, session: AsyncSession, run_id: UUID) -> None:
    tasks = (
        await session.scalars(
            select(AssessmentTask)
            .where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.status == "pending",
            )
            .with_for_update()
        )
    ).all()
    for task in tasks:
        task.status = "skipped"
        task.completed_at = datetime.now(UTC)
        task.result = {"reason": "out_of_phase_scope"}
```

Update `AssessmentRun.started_at` when `prepare_assessment` creates the run.
Change `prepare_assessment` return type to `PreparedAssessment` and return
`run.deadline_at`.
Update `build_worker` activity list with all six activities, including
`mark_deadline_exceeded`.

```python
return PreparedAssessment(
    run_id=str(run.id),
    task_count=task_count,
    deadline_at=run.deadline_at,
)
```

Replace the old 300-second workflow execution timeout in `worker.py` with:

```python
execution_timeout=timedelta(
    seconds=configured.assessment_workflow_safety_timeout_seconds
),
```

Keep the old `ASSESSMENT_WORKFLOW_DEADLINE_SECONDS` setting only as a deprecated
alias if existing deployments still provide it; it must not override the new
1800-second safety timeout.

- [ ] **Step 4: Run workflow and worker tests**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_temporal.py tests/test_assessment_dispatcher.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/assessment/temporal.py app/assessment/worker.py tests/test_assessment_temporal.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/assessment/temporal.py backend/app/assessment/worker.py backend/app/config.py backend/tests/test_assessment_temporal.py infra/compose.yaml
git commit -m "feat: orchestrate intensity assessment workflow"
```

### Task 11: Result Summary API

**Files:**
- Modify: `backend/app/assessment/schemas.py`
- Modify: `backend/app/assessment/router.py`
- Modify: `backend/app/intensity/repository.py`
- Modify: `backend/tests/test_assessment_api.py`
- Create: `backend/tests/test_intensity_api.py`

**Interfaces:**
- Consumes: `IntensityFieldProduct`, `AssessmentRun`.
- Produces:
  - `IntensityProductSummary`
  - `IntensityResultResponse`
  - `GET /api/v1/assessments/runs/{run_id}/intensity`
  - `AssessmentRepository.get_effective_run(session, event_id) -> AssessmentRun | None`
  - `IntensityRepository.list_products(session, run_id) -> list[dict]`

- [ ] **Step 1: Write failing API tests**

Create `backend/tests/test_intensity_api.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.main import app
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_api_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory):
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _seed_product(session_factory) -> UUID:
    event = NormalizedEvent(
        EventKind.FORMAL,
        "cenc",
        "API-INTENSITY-1",
        datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        Decimal("121.5"),
        Decimal("31.2"),
        Decimal("10"),
        Decimal("5.2"),
        "test",
        datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "API-INTENSITY-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            await IntensityRepository().save_product(
                session,
                IntensityProductWrite(
                    run_id=run.id,
                    task_id=task.id,
                    product_type=ProductType.MODEL,
                    status=ProductStatus.AVAILABLE,
                    algorithm_version="model-axis-ratio-v1",
                    parameter_version="shanghai-2019.1",
                    strategy_version=None,
                    grid_definition=GridDefinition(
                        "grid-test",
                        "EPSG:32651",
                        1000,
                        0,
                        1000,
                        1,
                        1,
                    ),
                    region_profile_version="shanghai-v1",
                    input_fingerprint="f" * 64,
                    input_checksum="i" * 64,
                    quality_grade=None,
                    coverage_ratio=0.0,
                    statistics={"minimum": 5.0, "maximum": 5.0, "mean": 5.0},
                    source_product_id=None,
                    observed_at=received,
                    bands=[("value", __import__("numpy").array([[5.0]]))],
                ),
            )
            return run.id


async def test_get_intensity_result_summary(session_factory) -> None:
    run_id = await _seed_product(session_factory)
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role="group_member",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/assessments/runs/{run_id}/intensity"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["run_status"] in {"pending", "running", "completed", "failed"}
    assert body["effective_run_id"] is None
    assert body["products"][0]["product_type"] == "model"
    assert body["products"][0]["product_id"]
    assert body["products"][0]["algorithm_version"] == "model-axis-ratio-v1"
```

- [ ] **Step 2: Run the API tests to verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_api.py tests/test_assessment_api.py -v
```

Expected: FAIL with `404` for the new route and missing response fields.

- [ ] **Step 3: Implement response schemas and routes**

Add `IntensityProductSummary` and `IntensityResultResponse` to
`backend/app/assessment/schemas.py` before `AssessmentRunStatusResponse`, then extend
the existing run response:

```python
class IntensityProductSummary(BaseModel):
    product_id: str
    product_type: str
    status: str
    algorithm_version: str
    parameter_version: str
    strategy_version: str | None
    grid_definition_version: str
    region_profile_version: str
    quality_grade: str | None
    coverage_ratio: float
    output_checksum: str | None
    source_product_id: str | None
    observed_at: datetime | None
    completed_at: datetime | None
    statistics: dict


class IntensityResultResponse(BaseModel):
    run_id: str
    event_id: str
    revision_id: str
    run_status: str
    deadline_basis_at: datetime
    deadline_at: datetime
    deadline_exceeded_at: datetime | None
    completed_at: datetime | None
    superseded_by_run_id: str | None
    effective_run_id: str | None
    effective_revision_id: str | None
    is_latest_revision: bool
    is_fallback: bool
    products: list[IntensityProductSummary]
```

Add methods:

```python
async def get_effective_run(
    self,
    session: AsyncSession,
    *,
    event_id: str,
) -> AssessmentRun | None:
    event = await session.get(EarthquakeEvent, UUID(event_id))
    if event is None or event.effective_assessment_run_id is None:
        return None
    return await session.get(AssessmentRun, event.effective_assessment_run_id)
```

Add to `IntensityRepository`:

```python
async def list_products(
    self,
    session: AsyncSession,
    run_id: UUID,
) -> list[dict]:
    products = (
        await session.scalars(
            select(IntensityFieldProduct)
            .where(IntensityFieldProduct.run_id == run_id)
            .order_by(IntensityFieldProduct.product_type)
        )
    ).all()
    return [
        {
            "product_id": str(product.id),
            "product_type": product.product_type,
            "status": product.status,
            "algorithm_version": product.algorithm_version,
            "parameter_version": product.parameter_version,
            "strategy_version": product.strategy_version,
            "grid_definition_version": product.grid_definition_version,
            "region_profile_version": product.region_profile_version,
            "quality_grade": product.quality_grade,
            "coverage_ratio": float(product.coverage_ratio),
            "output_checksum": product.output_checksum,
            "source_product_id": product.source_product_id,
            "observed_at": product.observed_at,
            "completed_at": product.completed_at,
            "statistics": dict(product.statistics),
        }
        for product in products
    ]
```

Add route:

```python
@router.get(
    "/runs/{run_id}/intensity",
    response_model=IntensityResultResponse,
)
async def get_intensity_result(
    run_id: UUID,
    assessment_repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
):
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await assessment_repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                event = await session.get(EarthquakeEvent, run.event_id)
                effective = await assessment_repository.get_effective_run(
                    session,
                    event_id=str(run.event_id),
                )
                products = await IntensityRepository().list_products(
                    session,
                    run.id,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return IntensityResultResponse(
        run_id=str(run.id),
        event_id=str(run.event_id),
        revision_id=str(run.revision_id),
        run_status=run.status,
        deadline_basis_at=run.deadline_basis_at,
        deadline_at=run.deadline_at,
        deadline_exceeded_at=run.deadline_exceeded_at,
        completed_at=run.completed_at,
        superseded_by_run_id=(
            str(run.superseded_by_run_id)
            if run.superseded_by_run_id is not None
            else None
        ),
        effective_run_id=str(effective.id) if effective is not None else None,
        effective_revision_id=(
            str(effective.revision_id) if effective is not None else None
        ),
        is_latest_revision=event.latest_assessment_run_id == run.id,
        is_fallback=effective is not None and effective.id != run.id,
        products=products,
    )
```

Update `/events/{event_id}/current` to return an optional `intensity` summary:

```python
class AssessmentRunStatusResponse(BaseModel):
    run_id: str
    event_id: str
    revision_id: str
    run_no: int
    status: AssessmentRunStatus
    t1_at: datetime
    deadline_at: datetime
    completed_task_count: int
    failed_task_count: int
    total_task_count: int
    tasks: list[AssessmentTaskStatusResponse]
    intensity: IntensityResultResponse | None = None
```

In the current endpoint, after loading the latest run:

```python
event = await session.get(EarthquakeEvent, run.event_id)
effective = await repository.get_effective_run(
    session,
    event_id=str(run.event_id),
)
result_run = effective or run
products = await IntensityRepository().list_products(session, result_run.id)
intensity = IntensityResultResponse(
    run_id=str(result_run.id),
    event_id=str(result_run.event_id),
    revision_id=str(result_run.revision_id),
    run_status=result_run.status,
    deadline_basis_at=result_run.deadline_basis_at,
    deadline_at=result_run.deadline_at,
    deadline_exceeded_at=result_run.deadline_exceeded_at,
    completed_at=result_run.completed_at,
    superseded_by_run_id=(
        str(result_run.superseded_by_run_id)
        if result_run.superseded_by_run_id is not None
        else None
    ),
    effective_run_id=str(effective.id) if effective is not None else None,
    effective_revision_id=(
        str(effective.revision_id) if effective is not None else None
    ),
    is_latest_revision=event.latest_assessment_run_id == result_run.id,
    is_fallback=effective is not None and effective.id != run.id,
    products=products,
)
```

Return that object through `_run_response(..., intensity=intensity)`. If a run is
not effective, label it as fallback; do not apply latest revision metadata to
older products.

Change the helper signature to:

```python
def _run_response(
    run: AssessmentRun,
    tasks: list[AssessmentTask],
    *,
    intensity: IntensityResultResponse | None,
) -> AssessmentRunStatusResponse:
```

- [ ] **Step 4: Run API and lint checks**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_api.py tests/test_assessment_api.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/assessment/schemas.py app/assessment/router.py app/intensity/repository.py tests/test_intensity_api.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/assessment/schemas.py backend/app/assessment/router.py backend/app/intensity/repository.py backend/tests/test_intensity_api.py backend/tests/test_assessment_api.py
git commit -m "feat: expose intensity assessment results"
```

### Task 12: Correction Reports, Deadline Flags, and Effective Fallback

**Files:**
- Modify: `backend/app/assessment/repository.py`
- Modify: `backend/app/intensity/service.py`
- Modify: `backend/tests/test_intensity_service_execution.py`
- Create: `backend/tests/test_intensity_corrections.py`

**Interfaces:**
- Consumes: `EventKind.CORRECTION`, assessment run lifecycle, intensity products.
- Produces:
  - `AssessmentRepository.mark_deadline_exceeded(session, run_id, observed_at) -> bool`
  - `AssessmentRun.superseded_by_run_id` and `superseded_at` semantics.
  - fallback behavior where an older completed run remains effective until a
    latest correction completes.

- [ ] **Step 1: Write failing correction and deadline tests**

Create `backend/tests/test_intensity_corrections.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_correction_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory):
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _event(kind: EventKind, magnitude: str, report_time: datetime):
    return NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id="CORRECTION-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="test",
        report_time=report_time,
    )


async def test_correction_supersedes_old_run_but_keeps_fallback(session_factory) -> None:
    repository = AssessmentRepository()
    first = await _event(
        EventKind.FORMAL,
        "5.2",
        datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    first_outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "CORRECTION-1", "type": "reviewed", "number": 1},
        event=first,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            first.magnitude,
            first.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            first_outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == first_outcome.revision_id
                )
            )
            first_run = await repository.ensure_run_from_outbox(
                session,
                event_id=first_outcome.event_id,
                revision_id=first_outcome.revision_id,
                outbox_id=str(first_outbox.id),
            )
            first_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == first_run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            await repository.complete_task(
                session,
                first_task.id,
                "first-checksum",
                {},
            )
            fusion_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == first_run.id,
                    AssessmentTask.task_key == "intensity.fusion",
                )
            )
            await repository.complete_task(
                session,
                fusion_task.id,
                "fusion-checksum",
                {},
            )
            await repository.complete_run(session, first_run.id, "bundle-1")
            first_run.deadline_at = received + timedelta(seconds=1)
            await repository.mark_deadline_exceeded(
                session,
                first_run.id,
                received + timedelta(seconds=2),
            )

    correction = await _event(
        EventKind.CORRECTION,
        "5.4",
        datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
    )
    second_received = datetime(2026, 9, 27, 1, 5, tzinfo=UTC)
    second_outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "CORRECTION-1", "type": "reviewed", "number": 2},
        event=correction,
        provider="fan",
        lane="websocket",
        received_at=second_received,
        response_input=ResponseInput(
            correction.magnitude,
            correction.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", second_received),
    )

    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == second_outcome.revision_id
                )
            )
            second_run = await repository.ensure_run_from_outbox(
                session,
                event_id=second_outcome.event_id,
                revision_id=second_outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            event = await session.get(EarthquakeEvent, first_run.event_id)
            old = await session.get(AssessmentRun, first_run.id)

    assert second_run.id != first_run.id
    assert old.superseded_by_run_id == second_run.id
    assert old.superseded_at is not None
    assert event.latest_assessment_run_id == second_run.id
    assert event.effective_assessment_run_id == first_run.id
    assert old.deadline_exceeded_at == received + timedelta(seconds=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_corrections.py -v
```

Expected: FAIL until supersede and effective-pointer behavior is wired.

- [ ] **Step 3: Implement correction, deadline, and final completion behavior**

`mark_deadline_exceeded` was added in Task 10 and must return `True` only when
it first sets the timestamp. Keep that method unchanged except for locking the
run row with `with_for_update()`.

Call it from each task runner after completion and from `finalize_assessment`
before changing run status:

```python
await self.assessment_repository.mark_deadline_exceeded(
    session,
    run.id,
    datetime.now(UTC),
)
```

Rules to enforce in `complete_run`:

- If `deadline_exceeded_at` is already set, keep it unchanged.
- Update the effective pointer only when the run is the event's
  `latest_assessment_run_id`.
- Never update an older run's pointer after a newer correction has been
  created.
- Keep the old effective run when the latest run is canceled, failed, or
  superseded before completion.

In `IntensityService.run_model`, `run_instrument`, and `run_fusion`, ensure a
late but complete calculation is persisted and published. A deadline flag must
not become a failure state.

- [ ] **Step 4: Run correction, lifecycle, and API fallback tests**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_corrections.py tests/test_assessment_task_lifecycle.py tests/test_assessment_api.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/assessment/repository.py app/intensity/service.py tests/test_intensity_corrections.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/assessment/repository.py backend/app/intensity/service.py backend/tests/test_intensity_corrections.py backend/tests/test_intensity_service_execution.py
git commit -m "feat: preserve correction and deadline semantics"
```

### Task 13: Failure Injection, Idempotency, and Z440 Performance Evidence

**Files:**
- Create: `backend/tests/intensity_helpers.py`
- Create: `backend/tests/test_intensity_failure_modes.py`
- Create: `backend/tests/test_intensity_performance.py`
- Modify: `backend/pyproject.toml`

**Interfaces:**
- Consumes: complete intensity service and Temporal workflow.
- Produces:
  - deterministic failure-mode tests.
  - a real PostgreSQL/PostGIS performance benchmark using the Shanghai + 100 km
    grid.
  - a pytest marker `performance`.

- [ ] **Step 1: Add the failing performance marker and tests**

Add to `backend/pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
  "performance: integration benchmarks that use the full configured grid",
]
```

Create `backend/tests/test_intensity_failure_modes.py`:

```python
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.assessment.models import AssessmentTask
from app.intensity.domain import GridDefinition
from app.intensity.instrument import UnavailableInstrumentProvider
from app.intensity.model import ModelFieldConvergenceError
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
from tests.intensity_helpers import (
    cleanup_intensity_fixture,
    seed_intensity_run,
)


class FailingInstrumentProvider:
    async def fetch(self, request):
        raise RuntimeError("provider unavailable")


async def test_instrument_failure_does_not_fail_model_fusion_chain(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    run_id = await seed_intensity_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "failure-test-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=FailingInstrumentProvider(),
    )

    try:
        model = await service.run_model(run_id)
        instrument = await service.run_instrument(run_id)
        fusion = await service.run_fusion(run_id)
    finally:
        await cleanup_intensity_fixture(session_factory)

    assert model.status == "succeeded"
    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"


async def test_duplicate_model_run_keeps_one_product(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    run_id = await seed_intensity_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "idempotency-test-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=UnavailableInstrumentProvider(),
    )

    try:
        first = await service.run_model(run_id)
        second = await service.run_model(run_id)
    finally:
        await cleanup_intensity_fixture(session_factory)

    assert first.product_id == second.product_id


async def test_model_failure_is_committed_to_task_audit(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    run_id = await seed_intensity_run(session_factory)
    parameters = load_parameter_bundle("/config/intensity/shanghai-2019.yaml")
    service = IntensityService(
        session_factory=session_factory,
        parameters=replace(
            parameters,
            model=replace(
                parameters.model,
                solver_intensity_min=100.0,
                solver_intensity_max=101.0,
            ),
        ),
        fixed_grid_definition=GridDefinition(
            "failure-audit-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
    )

    try:
        with pytest.raises(ModelFieldConvergenceError):
            await service.run_model(run_id)

        async with session_factory() as session:
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
        assert task is not None
        assert task.status == "failed"
        assert task.last_error is not None
    finally:
        await cleanup_intensity_fixture(session_factory)
```

Create `backend/tests/test_intensity_performance.py`:

```python
import time

import pytest

from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
from tests.intensity_helpers import (
    cleanup_intensity_fixture,
    seed_intensity_boundary,
    seed_intensity_run,
)


@pytest.mark.performance
async def test_full_shanghai_grid_meets_intensity_budget(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    boundary_version = await seed_intensity_boundary(session_factory)
    run_id = await seed_intensity_run(
        session_factory,
        boundary_version=boundary_version,
    )
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
    )

    try:
        started = time.perf_counter()
        model = await service.run_model(run_id)
        model_seconds = time.perf_counter() - started

        instrument_started = time.perf_counter()
        await service.run_instrument(run_id)
        instrument_seconds = time.perf_counter() - instrument_started

        fusion_started = time.perf_counter()
        fusion = await service.run_fusion(run_id)
        fusion_seconds = time.perf_counter() - fusion_started
    finally:
        await cleanup_intensity_fixture(session_factory)

    assert model.status == "succeeded"
    assert fusion.status == "succeeded"
    assert model_seconds <= 60.0
    assert instrument_seconds <= 60.0
    assert fusion_seconds <= 90.0
```

Create `backend/tests/intensity_helpers.py` instead of adding hidden fixtures:

```python
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary


BOUNDARY_VERSION_PREFIX = "intensity-performance-"
EVENT_PREFIX = "INTENSITY-PERF-"


async def cleanup_intensity_fixture(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
            await session.execute(
                delete(RegionBoundary).where(
                    RegionBoundary.version.like(f"{BOUNDARY_VERSION_PREFIX}%")
                )
            )


async def seed_intensity_boundary(session_factory) -> str:
    version = f"{BOUNDARY_VERSION_PREFIX}{uuid4()}"
    geometry = WKTElement(
        "MULTIPOLYGON (((120.8 30.6, 122.2 30.6, 122.2 31.9, "
        "120.8 31.9, 120.8 30.6)))",
        srid=4326,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                RegionBoundary(
                    version=version,
                    name="intensity performance test",
                    local_buffer_km=Decimal("50"),
                    geom=geometry,
                    maritime_geom=geometry,
                    source_uri="test://intensity-performance",
                    checksum="a" * 64,
                    is_active=False,
                )
            )
    return version


async def seed_intensity_run(
    session_factory,
    *,
    boundary_version: str = "intensity-test-grid",
) -> str:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id=f"{EVENT_PREFIX}{uuid4()}",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="intensity test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, boundary_version, received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return str(run.id)
```

The performance test must use the real 1 km grid resolver. Do not replace the
full grid with a 2×2 test grid in this performance test.

- [ ] **Step 2: Run failure tests and the benchmark to verify initial failures**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_failure_modes.py tests/test_intensity_performance.py -v
```

Expected: the failure-mode tests fail until the provider-error path is
implemented; the benchmark fails until real grid resolution is wired.

- [ ] **Step 3: Implement provider-error degradation and make the benchmark pass**

In `IntensityService.run_instrument`, catch provider exceptions and persist an
unavailable instrument product:

```python
try:
    product = await self.instrument_provider.fetch(request)
except Exception as exc:
    product = InstrumentProduct(
        status=ProductStatus.UNAVAILABLE,
        product_id=None,
        product_version=None,
        observed_at=None,
        source="provider_error",
        grid_version=definition.version,
        values=None,
        sigma=None,
        quality_codes=None,
        coverage_ratio=0.0,
        reason=f"{type(exc).__name__}: {exc}"[:500],
    )
```

Persist that unavailable product and complete the instrument task successfully.
Do not let instrument provider failure raise into the Temporal workflow.

Make the full-grid path use NumPy arrays and PyProj array operations. Do not
create one Python object per cell. Persist one PostGIS raster per product.

If the benchmark exposes a real problem, record the measured time and fix only
the hot path. Do not relax the `60/60/90` second budget.

- [ ] **Step 4: Run the full intensity suite and benchmark**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_*.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_intensity_performance.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/intensity tests
```

Expected: PASS. Record the observed model, instrument, and fusion times in the
final verification report.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/tests/conftest.py backend/tests/test_intensity_failure_modes.py backend/tests/test_intensity_performance.py backend/app/intensity/service.py
git commit -m "test: verify intensity failure and performance behavior"
```

### Task 14: Runbook, Documentation, and Final Verification

**Files:**
- Create: `docs/runbooks/intensity-assessment.md`
- Modify: `README.md`
- Modify: `docs/runbooks/assessment-orchestration.md`
- Modify: `backend/tests/test_migrations.py` only if the migration head changes.

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: reproducible operating, recovery, and verification instructions.

- [ ] **Step 1: Write the runbook and update user-facing requirements**

Create `docs/runbooks/intensity-assessment.md` with these sections:

```markdown
# Intensity Assessment Runbook

## Scope

## Required Configuration

## Starting Services

## Verifying a Formal-Report Run

## Inspecting Model, Instrument, and Fusion Products

## Interpreting Quality and Coverage

## Correcting a Report

## Handling Deadline Exceeded Alerts

## Handling Model or Fusion Failure

## Handling Instrument Degradation

## Database and Raster Checks

## Backup and Restore Notes

## Verification Commands
```

Document these exact operational rules:

- `T1` is the first formal report ingestion time.
- For each correction, the five-minute deadline is based on that correction's
  ingestion time.
- Model and fusion failures make the run fail.
- Instrument absence or failure produces `model_only`/`F3`.
- Only a completed product can become effective.
- An older complete product remains a labeled fallback while the latest
  correction is incomplete.
- The technical safety timeout is 1800 seconds.
- Raster data is stored in PostgreSQL/PostGIS and must be included in the
  backup strategy.

Update `README.md` to replace the statement that intensity is still a skeleton
with the actual implemented behavior and commands.

Update `docs/runbooks/assessment-orchestration.md` so the existing query and
recovery examples include the new run fields and intensity product tables.

- [ ] **Step 2: Run the complete verification suite**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_intensity_performance.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Expected:

- Compose configuration passes.
- Migration head is `0011_intensity_assessment`.
- All backend tests pass.
- The full-grid benchmark passes all `60/60/90` second limits.
- Ruff passes.
- Frontend tests, typecheck, and production build pass.

Record the exact counts and timings in the runbook. Do not write “approximately”
or “should pass”; use the actual command output.

- [ ] **Step 3: Verify real service integration**

Start the stack:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal temporal-ui
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker
```

Submit a formal test message through the existing event ingestion path, then
verify:

```sql
SELECT run_no, status, report_ingested_at, deadline_at, deadline_exceeded_at,
       algorithm_bundle_version, superseded_by_run_id
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 5;

SELECT task_key, status, attempt_count, output_checksum, last_error
FROM assessment_tasks
WHERE run_id = '<run-id>'
ORDER BY sequence;

SELECT product_type, status, quality_grade, coverage_ratio,
       output_checksum, completed_at
FROM intensity_field_products
WHERE run_id = '<run-id>'
ORDER BY product_type;

SELECT product_id, ST_Width(rast), ST_Height(rast), ST_SRID(rast)
FROM intensity_rasters
WHERE product_id IN (
  SELECT id FROM intensity_field_products WHERE run_id = '<run-id>'
);
```

Expected: model and fusion products complete, the instrument product is
`unavailable` or `available` according to the configured provider, and the
effective run pointer points to the latest complete run.

- [ ] **Step 4: Run the final spec coverage check**

Check each spec section against the implementation and record evidence:

```text
1. Scope and decisions       -> Tasks 1-14
2. Grid and distance         -> Tasks 2, 9
3. Model intensity           -> Tasks 3, 9
4. Direction selection       -> Task 3
5. Instrument adapter        -> Tasks 4, 9
6. Fusion                    -> Tasks 5, 9
7. Lifecycle and corrections -> Tasks 8, 10, 12
8. Database and raster       -> Tasks 6, 7
9. API                       -> Task 11
10. Failure/retry/idempotency-> Tasks 8, 10, 13
11. Test and acceptance      -> Tasks 13, 14
```

Fix any gap before committing.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/runbooks/intensity-assessment.md docs/runbooks/assessment-orchestration.md
git commit -m "docs: document intensity assessment operations"
```
