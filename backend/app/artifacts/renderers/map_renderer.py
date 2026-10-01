from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import numpy as np
from PIL import Image

from app.artifacts.basemap import TileKey, load_offline_basemap_candidates
from app.artifacts.renderers.base import (
    LocalAssetMissingError,
    RemoteAssetForbiddenError,
    RenderQuality,
    RenderResult,
)
from app.config import settings

MAPLIBRE_VERSION = "5.6.0"
PLAYWRIGHT_VERSION = "1.49.1"
RENDERER_VERSION = "artifact-map-renderer-v1"
OUTPUT_FORMATS = {"jpg", "jpeg", "png"}
VIRTUAL_ORIGIN = "http://renderer.local"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _sha256_bytes(canonical.encode("utf-8"))


def _isoformat(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _float(value: Any, label: str) -> float:
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class MapEvent:
    event_id: str
    revision_id: str
    place: str
    magnitude: float
    origin_time: datetime | str | None
    longitude: float
    latitude: float
    depth_km: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "revision_id": self.revision_id,
            "place": self.place,
            "magnitude": self.magnitude,
            "origin_time": _isoformat(self.origin_time),
            "longitude": self.longitude,
            "latitude": self.latitude,
            "depth_km": self.depth_km,
        }


@dataclass(frozen=True, slots=True)
class MapViewport:
    center: tuple[float, float]
    radius_km: float
    padding: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": [self.center[0], self.center[1]],
            "radius_km": self.radius_km,
            "padding": self.padding,
        }


@dataclass(frozen=True, slots=True)
class MapOutput:
    format: str
    width: int
    height: int
    dpi: int

    def __post_init__(self) -> None:
        normalized = self.format.lower()
        if normalized not in OUTPUT_FORMATS:
            raise ValueError(f"unsupported map output format: {self.format}")
        if self.width <= 0:
            raise ValueError("map output width must be positive")
        if self.height <= 0:
            raise ValueError("map output height must be positive")
        if self.dpi <= 0:
            raise ValueError("map output dpi must be positive")
        object.__setattr__(self, "format", "jpg" if normalized == "jpeg" else normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
        }


@dataclass(frozen=True, slots=True)
class MapLayer:
    url: str
    id: str = "layer"
    type: str = "geojson"
    source: Mapping[str, Any] = field(default_factory=dict)
    style: Mapping[str, Any] = field(default_factory=dict)
    source_key: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    local_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("map layer id must not be empty")
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("map layer url must not be empty")
        if not self.source_key:
            object.__setattr__(self, "source_key", self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "type": self.type,
            "source": dict(self.source),
            "style": dict(self.style),
            "source_key": self.source_key,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MapRenderSpec:
    artifact_key: str
    title: str
    event: MapEvent
    viewport: MapViewport
    base_style: str
    output: MapOutput
    layers: tuple[MapLayer, ...] = ()
    legend: tuple[Any, ...] = ()
    source_notes: tuple[str, ...] = ()
    quality: RenderQuality = field(default_factory=RenderQuality)
    marker: str | None = None
    production_mode: str = "live"
    context_fingerprint: str = ""
    input_fingerprint: str = ""
    template_version: str = "artifact-map-template-v1"
    renderer_version: str = RENDERER_VERSION
    selected_basemap: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_key": self.artifact_key,
            "title": self.title,
            "event": self.event.to_dict(),
            "viewport": self.viewport.to_dict(),
            "base_style": self.base_style,
            "layers": [layer.to_dict() for layer in self.layers],
            "legend": list(self.legend),
            "source_notes": list(self.source_notes),
            "quality": self.quality.to_dict(),
            "marker": self.marker,
            "production_mode": self.production_mode,
            "output": self.output.to_dict(),
            "context_fingerprint": self.context_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "template_version": self.template_version,
            "renderer_version": self.renderer_version,
            "selected_basemap": dict(self.selected_basemap)
            if self.selected_basemap is not None
            else None,
        }

    def fingerprint(self) -> str:
        return _sha256_json(self.to_dict())

    @property
    def render_manifest(self) -> dict[str, Any]:
        basemap = self.selected_basemap or {}
        return {
            "artifact_key": self.artifact_key,
            "base_provider": basemap.get("provider"),
            "base_package_id": basemap.get("package_id"),
            "base_package_version": basemap.get("version"),
            "base_package_checksum": basemap.get("checksum"),
            "base_style": self.base_style,
            "marker": self.marker,
            "layer_versions": {
                layer.id: str(
                    layer.metadata.get("version")
                    or layer.metadata.get("source_checksum")
                    or "unresolved"
                )
                for layer in self.layers
            },
            "layer_checksums": {
                layer.id: layer.metadata.get("source_checksum")
                for layer in self.layers
            },
            "template_version": self.template_version,
            "output_coordinates": {
                "width": self.output.width,
                "height": self.output.height,
                "dpi": self.output.dpi,
            },
            "renderer_version": self.renderer_version,
            "maplibre_version": MAPLIBRE_VERSION,
            "playwright_version": PLAYWRIGHT_VERSION,
            "context_fingerprint": self.context_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "quality": self.quality.to_dict(),
            "allocation_rule": _layer_metadata_first(
                self.layers,
                "allocation_rule",
            ),
            "allocation_inputs": _layer_metadata_first(
                self.layers,
                "allocation_inputs",
            )
            or {},
        }


def _layer_metadata_first(layers: Any, key: str) -> Any:
    for layer in layers:
        metadata = getattr(layer, "metadata", None)
        if isinstance(metadata, Mapping) and metadata.get(key):
            return metadata[key]
    return None


def _selected_basemap_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    return None


def _coerce_layer(value: Any) -> MapLayer:
    if isinstance(value, MapLayer):
        return value
    if isinstance(value, Mapping):
        layer_id = value.get("id")
        if not isinstance(layer_id, str):
            raise ValueError("map layer mapping requires an id")
        return MapLayer(
            id=layer_id,
            url=str(value.get("url") or ""),
            type=str(value.get("type") or "geojson"),
            source=value.get("source") or {},
            style=value.get("style") or {},
            source_key=str(value.get("source_key") or layer_id),
            metadata=value.get("metadata") or {},
        )
    raise TypeError("map layers must be MapLayer instances or mappings")


def _coerce_quality(value: Any) -> RenderQuality:
    if isinstance(value, RenderQuality):
        return value
    if value is None:
        return RenderQuality(grade="A")
    if isinstance(value, Mapping):
        return RenderQuality(
            grade=str(value.get("grade") or "A"),
            needs_review=bool(value.get("needs_review", False)),
            missing_assets=tuple(
                str(item) for item in value.get("missing_assets", ())
            ),
            degradation_reasons=tuple(
                str(item) for item in value.get("degradation_reasons", ())
            ),
            spatialized_estimate=bool(value.get("spatialized_estimate", False)),
        )
    return RenderQuality(
        grade=str(getattr(value, "grade", "A")),
        needs_review=bool(getattr(value, "needs_review", False)),
        missing_assets=tuple(getattr(value, "missing_assets", ())),
        degradation_reasons=tuple(getattr(value, "degradation_reasons", ())),
        spatialized_estimate=bool(getattr(value, "spatialized_estimate", False)),
    )


def _local_url_is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "local":
        return bool(parsed.path or parsed.netloc)
    if parsed.scheme and parsed.scheme != "file":
        return False
    candidate = Path(url).resolve()
    storage_root = Path(settings.artifact_storage_root).resolve()
    try:
        candidate.relative_to(storage_root)
    except ValueError:
        return False
    return True


def _validate_local_url(url: str) -> None:
    if not _local_url_is_allowed(url):
        raise RemoteAssetForbiddenError(
            f"map render assets must use local:// or ARTIFACT_STORAGE_ROOT: {url}"
        )


class MapSpecBuilder:
    """Build an immutable MapRenderSpec from a render context."""

    def build(self, context: Any) -> MapRenderSpec:
        artifact_key = str(getattr(context, "artifact_key"))
        if not artifact_key:
            raise ValueError("artifact_key must not be empty")

        title = getattr(context, "display_name", None)
        if not title:
            title = _catalog_display_name(artifact_key)
        selected_basemap = _selected_basemap_payload(
            getattr(context, "selected_basemap", None)
        )
        base_style = getattr(context, "base_style", None)
        if not base_style and selected_basemap:
            base_style = selected_basemap.get("provider_style_key")
        if not base_style:
            raise ValueError("map render context requires a selected offline base style")

        event = getattr(context, "event", None)
        if event is None:
            raise ValueError("map render context requires an event")
        revision = getattr(context, "revision", event)
        revision_id = getattr(revision, "id", None) or getattr(
            event, "event_id", None
        )
        event_id = getattr(event, "id", None) or getattr(event, "event_id", None)
        if event_id is None:
            raise ValueError("map render event requires an id")
        if revision_id is None:
            revision_id = event_id

        from app.artifacts.renderers.map_layers import (
            MapLayerRegistry,
            MapQualityPolicy,
        )

        if artifact_key in MapLayerRegistry.artifact_keys():
            layers = MapLayerRegistry.build(artifact_key, context)
        else:
            layers = tuple(
                _coerce_layer(item) for item in getattr(context, "layers", ())
            )
            if not layers and artifact_key == "map.epicenter":
                layers = (_epicenter_layer(event),)
        for layer in layers:
            _validate_local_url(layer.url)
        quality_value = (
            MapQualityPolicy.evaluate(artifact_key, layers)
            if artifact_key in MapLayerRegistry.artifact_keys()
            else getattr(context, "quality", None)
        )
        quality = _coerce_quality(quality_value)
        legend = _collect_map_legend(
            layers,
            tuple(getattr(context, "legend", ())),
        )
        source_notes = _collect_map_source_notes(
            tuple(str(item) for item in getattr(context, "source_notes", ())),
            layers,
            selected_basemap,
            quality,
        )

        spec = MapRenderSpec(
            artifact_key=artifact_key,
            title=str(title),
            event=MapEvent(
                event_id=str(event_id),
                revision_id=str(revision_id),
                place=str(getattr(event, "place", "")),
                magnitude=_float(getattr(event, "magnitude", 0.0), "magnitude"),
                origin_time=getattr(event, "origin_time", None),
                longitude=_float(getattr(event, "longitude", 0.0), "longitude"),
                latitude=_float(getattr(event, "latitude", 0.0), "latitude"),
                depth_km=_float(getattr(event, "depth_km", 0.0), "depth_km"),
            ),
            viewport=MapViewport(
                center=(
                    _float(getattr(event, "longitude", 0.0), "longitude"),
                    _float(getattr(event, "latitude", 0.0), "latitude"),
                ),
                radius_km=50,
                padding=40,
            ),
            base_style=str(base_style),
            layers=layers,
            legend=legend,
            source_notes=source_notes,
            quality=quality,
            marker=getattr(context, "marker", None),
            production_mode=str(getattr(context, "production_mode", "live")),
            output=MapOutput(
                format=str(getattr(context, "output_format", "jpg")),
                width=4761,
                height=3369,
                dpi=300,
            ),
            selected_basemap=selected_basemap,
        )
        context_fingerprint = getattr(context, "context_fingerprint", None)
        if not context_fingerprint:
            context_fingerprint = spec.fingerprint()
        input_fingerprint = getattr(context, "input_fingerprint", None)
        if not input_fingerprint:
            input_fingerprint = context_fingerprint
        return replace(
            spec,
            context_fingerprint=str(context_fingerprint),
            input_fingerprint=str(input_fingerprint),
        )


def _collect_map_legend(
    layers: tuple[MapLayer, ...],
    context_legend: tuple[Any, ...],
) -> tuple[Any, ...]:
    entries: list[Any] = list(context_legend)
    for layer in layers:
        for entry in layer.metadata.get("legend", ()):
            if entry not in entries:
                entries.append(entry)
    return tuple(entries)


def _collect_map_source_notes(
    context_notes: tuple[str, ...],
    layers: tuple[MapLayer, ...],
    selected_basemap: Mapping[str, Any] | None,
    quality: Any = None,
) -> tuple[str, ...]:
    notes: list[str] = list(context_notes)
    if selected_basemap:
        provider = str(selected_basemap.get("provider") or "")
        if provider:
            notes.append(f"{provider} offline basemap")
    for layer in layers:
        source_key = str(layer.metadata.get("source_key") or "")
        statement = layer.metadata.get("verified_empty_statement")
        verified_empty = (
            bool(layer.metadata.get("verified_empty"))
            or layer.metadata.get("source_status") == "verified_empty"
        )
        if statement and verified_empty:
            notes.append(str(statement))
        elif layer.metadata.get("source_status") == "missing":
            reason = layer.metadata.get("degradation_reason")
            notes.append(
                str(reason) if reason else f"{source_key} unavailable"
            )
    if quality is not None and (
        str(getattr(quality, "grade", "")) == "C"
        or getattr(quality, "spatialized_estimate", False)
    ):
        notes.extend(
            str(reason)
            for reason in getattr(quality, "degradation_reasons", ())
            if reason
        )
    return tuple(dict.fromkeys(notes))


def _epicenter_layer(event: Any) -> MapLayer:
    longitude = float(getattr(event, "longitude", 0.0))
    latitude = float(getattr(event, "latitude", 0.0))
    feature = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [longitude, latitude],
                },
                "properties": {"kind": "epicenter"},
            }
        ],
    }
    return MapLayer(
        id="epicenter",
        url="local://event-point",
        source={"type": "geojson", "data": feature},
        style={
            "type": "circle",
            "paint": {
                "circle-radius": 10,
                "circle-color": "#b3261e",
                "circle-stroke-color": "#ffffff",
                "circle-stroke-width": 2,
            },
        },
    )


def _catalog_display_name(artifact_key: str) -> str:
    from app.artifacts.catalog import load_catalog

    catalog = load_catalog(settings.artifact_catalog_path)
    return catalog.get(artifact_key, "a3v-professional").display_name


class LocalAssetRegistry:
    """Resolves local renderer assets without network access."""

    def __init__(
        self,
        *,
        base_style_path: str | Path | None = None,
        maplibre_root: str | Path | None = None,
        artifact_root: str | Path | None = None,
        basemap_root: str | Path | None = None,
    ) -> None:
        self._base_style_path = Path(
            base_style_path or "/config/artifacts/map/base-style.json"
        )
        self._maplibre_root = Path(
            maplibre_root or os.getenv("MAPLIBRE_STATIC_ROOT", "/opt/maplibre/assets")
        )
        self._artifact_root = Path(artifact_root or settings.artifact_storage_root).resolve()
        self._configured_basemap_root = (
            Path(basemap_root).resolve() if basemap_root is not None else None
        )
        self._basemap_root = self._resolve_basemap_root()
        self._base_style_bytes = b""
        self._packages: dict[str, Any] = {}

    def _resolve_basemap_root(self) -> Path:
        return (
            self._configured_basemap_root
            or Path(settings.artifact_basemap_root).resolve()
        )

    def preload(self) -> None:
        if not (self._maplibre_root / "maplibre-gl.js").is_file():
            raise LocalAssetMissingError(
                f"MapLibre JavaScript asset is missing: "
                f"{self._maplibre_root / 'maplibre-gl.js'}"
            )
        if not (self._maplibre_root / "maplibre-gl.css").is_file():
            raise LocalAssetMissingError(
                f"MapLibre CSS asset is missing: "
                f"{self._maplibre_root / 'maplibre-gl.css'}"
            )
        if not self._base_style_path.is_file():
            raise LocalAssetMissingError(
                f"artifact base style is missing: {self._base_style_path}"
            )
        self._base_style_bytes = self._base_style_path.read_bytes()
        json.loads(self._base_style_bytes.decode("utf-8"))
        self._basemap_root = self._resolve_basemap_root()
        packages, _ = load_offline_basemap_candidates(self._basemap_root)
        self._packages = packages

    def selected_basemap_package(
        self,
        selected: Mapping[str, Any] | None,
    ) -> Any:
        if selected is None:
            raise LocalAssetMissingError(
                "selected offline basemap metadata is required"
            )
        provider = selected.get("provider")
        if not isinstance(provider, str):
            raise LocalAssetMissingError("selected basemap provider is missing")
        package = self._packages.get(provider)
        if package is None:
            raise LocalAssetMissingError(
                f"selected offline basemap package is missing: {provider}"
            )
        package_id = selected.get("package_id")
        version = selected.get("version")
        checksum = selected.get("checksum")
        if package_id and package.package_id != package_id:
            raise LocalAssetMissingError(
                f"selected basemap package identity mismatch: "
                f"expected {package_id}, loaded {package.package_id}"
            )
        if version and package.version != version:
            raise LocalAssetMissingError(
                f"selected basemap package version mismatch: "
                f"expected {version}, loaded {package.version}"
            )
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or checksum != checksum.lower()
            or any(character not in "0123456789abcdef" for character in checksum)
        ):
            raise LocalAssetMissingError(
                "selected basemap checksum must be a 64 character SHA-256 hex digest"
            )
        if package.checksum.lower() != checksum:
            raise LocalAssetMissingError(
                "selected basemap checksum does not match the loaded package"
            )
        return package

    def basemap_source(self, package: Any) -> dict[str, Any]:
        return {
            "type": "raster",
            "tiles": [
                f"local://basemap/{package.provider}/"
                "{z}/{x}/{y}."
                f"{package.format}"
            ],
            "tileSize": 256,
            "minzoom": min(package.zoom_levels),
            "maxzoom": max(package.zoom_levels),
        }

    def basemap_package_path(self, package: Any) -> str:
        provider_dir = self._basemap_root / package.provider
        if package.package_format == "directory":
            return str(provider_dir / "tiles")
        index_file = package.index_file or f"{package.provider}.{package.package_format}"
        return str(provider_dir / index_file)

    def resolve(self, request_path: str) -> tuple[bytes, str] | None:
        path = request_path.lstrip("/")
        if path in {"maplibre-gl.js", "maplibre-gl.css", "base-style.json"}:
            if path == "base-style.json":
                return self._base_style_bytes, "application/json"
            suffix = ".js" if path.endswith(".js") else ".css"
            content_type = (
                "text/javascript" if suffix == ".js" else "text/css"
            )
            return (self._maplibre_root / path).read_bytes(), content_type

        if path.startswith("artifact_maps/"):
            content_type = (
                "image/png"
                if path.lower().endswith(".png")
                else "image/jpeg"
                if path.lower().endswith((".jpg", ".jpeg"))
                else "application/geo+json"
            )
            return _read_within_root(
                self._artifact_root,
                path,
                content_type,
            )

        if path.startswith("basemap/"):
            parts = path.split("/")
            if len(parts) != 5:
                return None
            provider, z_value, x_value, y_name = parts[1:]
            try:
                key = TileKey(
                    z=int(z_value),
                    x=int(x_value),
                    y=int(y_name.split(".", 1)[0]),
                )
            except (TypeError, ValueError):
                return None
            package = self._packages.get(provider)
            if package is None:
                return None
            try:
                data = package.decode_tile(key)
            except Exception:
                return None
            content_type = "image/png" if package.format == "png" else "image/jpeg"
            return data, content_type
        return None


def _read_within_root(root: Path, relative: str, content_type: str) -> tuple[bytes, str] | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate.read_bytes(), content_type


@dataclass(frozen=True, slots=True)
class BrowserSlot:
    token: str
    page: Any


class BrowserPool:
    """Priority-aware Playwright browser page pool with explicit cancellation."""

    def __init__(
        self,
        *,
        max_slots: int | None = None,
        assets: LocalAssetRegistry | None = None,
    ) -> None:
        self.max_slots = max_slots or settings.artifact_browser_pool_size
        if not 1 <= self.max_slots <= 8:
            raise ValueError("browser pool size must be between 1 and 8")
        self._assets = assets or LocalAssetRegistry()
        self._condition = asyncio.Condition()
        self._reservations = 0
        self._active_pages: dict[str, Any] = {}
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def available_slots(self) -> int:
        return self.max_slots - self._reservations

    @property
    def active_pages(self) -> int:
        return len(self._active_pages)

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._assets.preload()
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1587, "height": 1123},
                    device_scale_factor=3,
                    locale="zh-CN",
                    color_scheme="light",
                )
                self._started = True
            except Exception:
                if self._context is not None:
                    await self._context.close()
                if self._browser is not None:
                    await self._browser.close()
                if self._playwright is not None:
                    await self._playwright.stop()
                self._context = None
                self._browser = None
                self._playwright = None
                raise

    def reload_assets(self) -> None:
        self._assets.preload()

    def require_selected_basemap(
        self,
        selected: Mapping[str, Any] | None,
    ) -> Any:
        return self._assets.selected_basemap_package(selected)

    def basemap_source(self, selected: Mapping[str, Any] | None) -> dict[str, Any]:
        package = self.require_selected_basemap(selected)
        return self._assets.basemap_source(package)

    def selected_basemap_binding(
        self,
        selected: Mapping[str, Any] | None,
    ) -> tuple[Any, dict[str, Any]]:
        package = self.require_selected_basemap(selected)
        return package, self._assets.basemap_source(package)

    def basemap_package_path(self, package: Any) -> str:
        return self._assets.basemap_package_path(package)

    async def reserve_slot(self, priority: int = 100) -> str:
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ValueError("browser slot priority must be a non-negative integer")
        token = os.urandom(16).hex()
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._reservations < self.max_slots
            )
            self._reservations += 1
        return token

    async def acquire_page(self, priority: int = 100) -> BrowserSlot:
        token = await self.reserve_slot(priority)
        try:
            await self.start()
            page = await self._context.new_page()
            await page.route(
                f"{VIRTUAL_ORIGIN}/**",
                self._handle_local_request,
            )
            self._active_pages[token] = page
            return BrowserSlot(token=token, page=page)
        except Exception:
            await self.release_slot(token)
            raise

    async def cancel(self, token: str) -> None:
        await self.release_slot(token)

    async def release_slot(self, token: str) -> None:
        page = self._active_pages.pop(token, None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        async with self._condition:
            self._reservations = max(0, self._reservations - 1)
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._lock:
            if not self._started:
                return
            for page in list(self._active_pages.values()):
                try:
                    await page.close()
                except Exception:
                    pass
            self._active_pages.clear()
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._context = None
            self._browser = None
            self._playwright = None
            self._reservations = 0
            self._started = False

    async def _handle_local_request(self, route: Any) -> None:
        try:
            request_path = urlparse(route.request.url).path
            resolved = self._assets.resolve(request_path)
        except Exception:
            resolved = None
        if resolved is None:
            await route.abort()
            return
        body, content_type = resolved
        await route.fulfill(
            status=200,
            body=body,
            content_type=content_type,
        )


class MapRenderer:
    def __init__(
        self,
        browser_pool: BrowserPool,
        *,
        min_non_empty_ratio: float = 0.2,
    ) -> None:
        self._browser_pool = browser_pool
        if not 0 < min_non_empty_ratio < 1:
            raise ValueError("min_non_empty_ratio must be between zero and one")
        self._min_non_empty_ratio = min_non_empty_ratio

    async def render(self, spec: MapRenderSpec, output_path: Path) -> RenderResult:
        _validate_spec(spec)
        self._browser_pool.reload_assets()
        package, basemap_source = self._browser_pool.selected_basemap_binding(
            spec.selected_basemap
        )
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        slot = await self._browser_pool.acquire_page(priority=100)
        try:
            page = slot.page
            await page.set_content(
                self._renderer_html(spec, basemap_source),
                wait_until="load",
            )
            await page.wait_for_function(
                "() => window.__artifactMapReady === true",
                timeout=30_000,
            )
            await page.evaluate(
                "() => new Promise(resolve => "
                "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            screenshot_type = "jpeg" if spec.output.format == "jpg" else "png"
            screenshot_kwargs: dict[str, Any] = {
                "path": str(target),
                "type": screenshot_type,
                "animations": "disabled",
            }
            if screenshot_type == "jpeg":
                screenshot_kwargs["quality"] = 92
            await page.screenshot(**screenshot_kwargs)
            non_empty_ratio = _normalize_output(
                target,
                format=spec.output.format,
                dpi=spec.output.dpi,
                marker=spec.marker,
            )
            width, height = _image_dimensions(target)
            if (width, height) != (spec.output.width, spec.output.height):
                raise RuntimeError(
                    f"rendered map geometry mismatch: expected "
                    f"{(spec.output.width, spec.output.height)}, got {(width, height)}"
                )
            if non_empty_ratio < self._min_non_empty_ratio:
                target.unlink(missing_ok=True)
                raise RuntimeError(
                    "rendered map contains too few non-empty pixels: "
                    f"{non_empty_ratio:.3f}"
                )
            checksum = _sha256_path(target)
            manifest = dict(spec.render_manifest)
            manifest.update(
                {
                    "checksum": checksum,
                    "size_bytes": target.stat().st_size,
                    "non_empty_ratio": non_empty_ratio,
                    "base_package_checksum": package.checksum,
                    "base_package_format": package.package_format,
                    "base_package_path": self._browser_pool.basemap_package_path(
                        package
                    ),
                }
            )
            return RenderResult(
                path=target,
                format=spec.output.format,
                width=width,
                height=height,
                dpi=spec.output.dpi,
                checksum=checksum,
                quality=spec.quality,
                task_status=(
                    "degraded"
                    if spec.quality.needs_review
                    or spec.quality.degradation_reasons
                    else "succeeded"
                ),
                file_name=_official_file_name(spec),
                render_manifest=manifest,
                non_empty_ratio=non_empty_ratio,
                size_bytes=target.stat().st_size,
            )
        finally:
            await self._browser_pool.release_slot(slot.token)

    def _renderer_html(
        self,
        spec: MapRenderSpec,
        basemap_source: Mapping[str, Any],
    ) -> str:
        payload = spec.to_dict()
        payload["basemap_source"] = dict(basemap_source)
        payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="{VIRTUAL_ORIGIN}/maplibre-gl.css">
<script src="{VIRTUAL_ORIGIN}/maplibre-gl.js"></script>
<style>
html,body{{margin:0;padding:0;width:1587px;height:1123px;overflow:hidden;
background:#f7f5f0;font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;}}
#map{{position:absolute;inset:0;}}
.chrome{{position:absolute;z-index:5;box-sizing:border-box;color:#17202a;}}
.title{{left:64px;top:48px;right:64px;border-bottom:3px solid #a33327;padding-bottom:14px;}}
.title h1{{margin:0;font-size:42px;line-height:1.15;letter-spacing:0;}}
.summary{{margin:12px 0 0;font-size:20px;line-height:1.5;color:#33404d;}}
.footer{{left:64px;right:64px;bottom:42px;display:flex;justify-content:space-between;
gap:24px;font-size:18px;line-height:1.5;color:#33404d;}}
.footer strong{{color:#a33327;}}
.side{{right:64px;top:210px;width:330px;padding:18px 20px;background:rgba(247,245,240,.94);
border:1px solid #c9c0b2;font-size:17px;line-height:1.45;color:#33404d;}}
.side h2{{margin:0 0 10px;font-size:20px;color:#17202a;}}
.legend-item{{display:flex;gap:10px;align-items:flex-start;padding:3px 0;}}
.legend-swatch{{flex:0 0 18px;height:18px;margin-top:4px;border:1px solid #8d857a;}}
.north{{left:64px;bottom:118px;width:44px;height:44px;}}
.north-arrow{{width:0;height:0;border-left:15px solid transparent;border-right:15px solid transparent;
border-bottom:34px solid #a33327;margin:0 auto;}}
.north-label{{text-align:center;font-size:18px;font-weight:700;color:#a33327;margin-top:2px;}}
.scale{{left:112px;bottom:132px;width:180px;color:#17202a;}}
.scale-bar{{height:8px;border-left:2px solid #17202a;border-right:2px solid #17202a;
background:linear-gradient(90deg,#17202a 0 50%,#ffffff 50% 100%);}}
.scale-label{{font-size:15px;margin-top:5px;letter-spacing:0;}}
.meta{{font-size:16px;color:#33404d;}}
.meta .quality{{display:inline-block;margin-left:10px;padding:2px 8px;background:#a33327;
color:#ffffff;font-weight:700;}}
.mode-marker{{position:absolute;left:64px;top:154px;z-index:6;padding:6px 12px;
background:#b3261e;color:#ffffff;font-size:22px;font-weight:700;}}
</style>
</head>
<body>
<div id="map"></div>
{_mode_marker_html(payload)}
<div class="chrome title">
<h1>{_html(payload["title"])}</h1>
<div class="summary">{_event_summary(payload)}</div>
</div>
<div class="chrome north">
<div class="north-arrow"></div>
<div class="north-label">N</div>
</div>
<div class="chrome scale">
<div class="scale-bar"></div>
<div class="scale-label">0 25 50 km</div>
</div>
{_legend_html(payload)}
<div class="chrome footer">
<span>{_html(" / ".join(payload["source_notes"]) or "offline data source")}</span>
<span class="meta">
<strong>300 DPI</strong> A3V
<span>生成时间 <span id="generated-at"></span></span>
<span>版本 {_html(payload["template_version"])}</span>
<span class="quality">{_quality_marker_html(payload)}</span>
</span>
</div>
<script>
(() => {{
  const payload = {payload_json};
  const generatedAt = new Date().toLocaleString("zh-CN", {{hour12: false}});
  document.getElementById("generated-at").textContent = generatedAt;
  const style = {{
    version: 8,
    name: "artifact-map",
    sources: {{}},
    layers: [{{id: "background", type: "background",
      paint: {{"background-color": "#f7f5f0"}}}}]
  }};
  if (payload.basemap_source) {{
    style.sources["offline-basemap"] = payload.basemap_source;
    style.layers.push({{
      id: "offline-basemap",
      type: "raster",
      source: "offline-basemap"
    }});
  }}
  for (const layer of payload.layers || []) {{
    const sourceKey = layer.source_key || layer.id;
    if (layer.source && Object.keys(layer.source).length) {{
      style.sources[sourceKey] = layer.source;
    }}
    style.layers.push(Object.assign({{id: layer.id, source: sourceKey}}, layer.style));
  }}
  const map = new maplibregl.Map({{
    container: "map",
    style,
    center: payload.viewport.center,
    zoom: 10,
    pitch: 0,
    bearing: 0,
    attributionControl: false,
    transformRequest: (url) => {{
      if (url.startsWith("local://")) {{
        return {{url: "{VIRTUAL_ORIGIN}/" + url.slice("local://".length)}};
      }}
      return {{url}};
    }}
  }});
  const center = payload.viewport.center;
  const centerLatRadians = center[1] * Math.PI / 180;
  const longitudeRadius = payload.viewport.radius_km /
    (111.32 * Math.max(0.2, Math.cos(centerLatRadians)));
  const latitudeRadius = payload.viewport.radius_km / 110.574;
  map.fitBounds(
    [
      [center[0] - longitudeRadius, center[1] - latitudeRadius],
      [center[0] + longitudeRadius, center[1] + latitudeRadius]
    ],
    {{padding: payload.viewport.padding, duration: 0}}
  );
  window.__artifactMapReady = false;
  map.on("load", async () => {{
    if (!map.loaded()) {{
      return;
    }}
    if (document.fonts && document.fonts.ready) {{
      await document.fonts.ready;
    }}
    window.__artifactMapReady = true;
  }});
  map.on("error", () => {{}});
}})();
</script>
</body>
</html>"""


def _validate_spec(spec: MapRenderSpec) -> None:
    if spec.output.width != 1587 * 3 or spec.output.height != 1123 * 3:
        raise ValueError("map output must be 4761x3369")
    if spec.output.dpi != 300:
        raise ValueError("map output must use 300 DPI")
    for layer in spec.layers:
        _validate_local_url(layer.url)
        if not (
            isinstance(layer.source, Mapping)
            and layer.source.get("type") == "geojson"
        ):
            _validate_resource_mapping(layer.source)
        _validate_resource_mapping(layer.style)


_RESOURCE_KEYS = {"url", "tiles", "data", "sprite", "glyphs", "source", "style"}


def _validate_resource_mapping(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _RESOURCE_KEYS:
                _validate_resource_value(item)
            else:
                _validate_resource_mapping(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_resource_mapping(item)


def _validate_resource_value(value: Any) -> None:
    if isinstance(value, str):
        _validate_local_url(value)
        return
    if isinstance(value, Mapping):
        _validate_resource_mapping(value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_resource_value(item)


def _normalize_output(
    path: Path,
    *,
    format: str,
    dpi: int,
    marker: str | None = None,
) -> float:
    with Image.open(path) as image:
        image.load()
        non_empty = _non_empty_pixel_ratio(image)
        image_format = "JPEG" if format == "jpg" else "PNG"
        save_kwargs: dict[str, Any] = {
            "format": image_format,
            "dpi": (dpi, dpi),
        }
        if image_format == "JPEG":
            save_kwargs["quality"] = 92
        if marker:
            image.info["comment"] = marker.encode("utf-8")
        temporary = path.with_name(f".{path.name}.dpi")
        image.save(temporary, **save_kwargs)
        os.replace(temporary, path)
    return non_empty


def _image_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _non_empty_pixel_ratio(image: Image.Image) -> float:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return 0.0
    background = np.array([247, 245, 240], dtype=np.int16)
    channel_delta = np.abs(rgb.astype(np.int16) - background)
    non_background = np.max(channel_delta, axis=2) > 20
    return float(np.mean(non_background))


def _html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _legend_html(payload: dict[str, Any]) -> str:
    entries = payload.get("legend") or ()
    if not entries:
        return ""
    rows: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        label = str(entry.get("label") or "")
        value = str(entry.get("value") or "")
        display = label if not value else f"{label} {value}"
        rows.append(
            '<div class="legend-item">'
            '<span class="legend-swatch"></span>'
            f"<span>{_html(display)}</span>"
            "</div>"
        )
    if not rows:
        return ""
    return (
        '<div class="chrome side"><h2>图例</h2>'
        + "".join(rows)
        + "</div>"
    )


def _mode_marker_html(payload: dict[str, Any]) -> str:
    marker = payload.get("marker")
    if not marker:
        return ""
    return f'<div class="mode-marker">{_html(str(marker))}</div>'


def _quality_marker_html(payload: dict[str, Any]) -> str:
    quality = payload.get("quality") or {}
    grade = str(quality.get("grade") or "A")
    needs_review = bool(
        quality.get("needs_review")
        or quality.get("degradation_reasons")
    )
    suffix = " 待复核" if needs_review else ""
    return _html(f"{grade} 级{suffix}")


def _event_summary(payload: dict[str, Any]) -> str:
    event = payload.get("event") or {}
    return (
        f"longitude {event.get('longitude')}, latitude {event.get('latitude')}, "
        f"magnitude {event.get('magnitude')}, depth {event.get('depth_km')} km"
    )


def _official_file_name(spec: MapRenderSpec) -> str:
    from app.artifacts.domain import ArtifactNameContext, ProductionMode
    from app.artifacts.naming import build_artifact_file_name

    return build_artifact_file_name(
        ArtifactNameContext(
            place=spec.event.place or "未命名震中",
            magnitude=spec.event.magnitude,
            display_name=spec.title,
            version=1,
            generated_at=datetime.now(UTC),
            production_mode=ProductionMode(spec.production_mode),
        ),
        extension=spec.output.format,
    )
