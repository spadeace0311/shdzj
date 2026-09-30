from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager
import matplotlib.pyplot as plt
from PIL import Image

from app.artifacts.renderers.base import RenderQuality, RenderResult
from app.config import settings

CHART_RENDERER_VERSION = "artifact-chart-renderer-v1"
FONT_FAMILY = "Noto Sans CJK SC"


def _configure_font() -> None:
    font_path = Path(settings.artifact_font_path)
    if not font_path.is_file():
        raise FileNotFoundError(f"artifact font file does not exist: {font_path}")
    font_manager.fontManager.addfont(str(font_path))
    properties = font_manager.FontProperties(fname=str(font_path))
    family = properties.get_name() or FONT_FAMILY
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = [family, FONT_FAMILY, "DejaVu Sans"]


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ChartSeries:
    name: str
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ChartSpec:
    chart_id: str
    title: str
    labels: tuple[str, ...]
    series: tuple[ChartSeries, ...] = ()
    x_label: str = ""
    y_label: str = ""
    width: int = 1200
    height: int = 800
    dpi: int = 150
    source_note: str = "离线冻结数据资产"
    quality: RenderQuality = field(default_factory=RenderQuality)
    marker: str | None = None

    def __post_init__(self) -> None:
        if not self.chart_id.strip():
            raise ValueError("chart_id must not be empty")
        if not self.title.strip():
            raise ValueError("chart title must not be empty")
        if not self.labels:
            raise ValueError("chart labels must not be empty")
        if not self.series:
            raise ValueError("chart must contain at least one series")
        if len({series.name for series in self.series}) != len(self.series):
            raise ValueError("chart series names must be unique")
        for series in self.series:
            if len(series.values) != len(self.labels):
                raise ValueError(
                    f"series {series.name!r} must match the label count"
                )
        if self.width <= 0 or self.height <= 0 or self.dpi <= 0:
            raise ValueError("chart dimensions and dpi must be positive")


class ChartRenderer:
    def render(self, spec: ChartSpec, output_path: Path) -> RenderResult:
        _configure_font()
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}")
        figure, axis = plt.subplots(figsize=(spec.width / spec.dpi, spec.height / spec.dpi))
        try:
            self._draw(axis, spec)
            figure.savefig(
                temporary,
                format="png",
                dpi=spec.dpi,
                bbox_inches=None,
                metadata={"Description": spec.source_note},
            )
        finally:
            plt.close(figure)
        os.replace(temporary, target)
        _normalize_png_dpi(target, spec.dpi)

        checksum = _sha256_path(target)
        width, height = _image_size(target)
        render_manifest: dict[str, Any] = {
            "chart_id": spec.chart_id,
            "title": spec.title,
            "image_checksums": {spec.chart_id: checksum},
            "source_note": spec.source_note,
            "renderer_version": CHART_RENDERER_VERSION,
            "matplotlib_backend": "Agg",
            "font_family": FONT_FAMILY,
            "marker_baked": spec.marker is not None,
            "marker": spec.marker,
            "quality": spec.quality.to_dict(),
        }
        return RenderResult(
            path=target,
            format="png",
            width=width,
            height=height,
            dpi=spec.dpi,
            checksum=checksum,
            quality=spec.quality,
            task_status=(
                "degraded"
                if spec.quality.needs_review
                or spec.quality.degradation_reasons
                else "succeeded"
            ),
            file_name=f"{spec.chart_id}.png",
            render_manifest=render_manifest,
            non_empty_ratio=1.0,
            size_bytes=target.stat().st_size,
            generated_at=datetime.now(UTC),
        )

    def _draw(self, axis: Any, spec: ChartSpec) -> None:
        axis.set_title(spec.title)
        axis.set_xlabel(spec.x_label)
        axis.set_ylabel(spec.y_label)
        x_positions = list(range(len(spec.labels)))
        axis.set_xticks(x_positions)
        axis.set_xticklabels(spec.labels, rotation=25, ha="right")
        for series in spec.series:
            axis.plot(
                x_positions,
                list(series.values),
                marker="o",
                linewidth=2,
                label=series.name,
            )
        if len(spec.series) > 1:
            axis.legend()
        axis.grid(True, alpha=0.25)
        if spec.marker:
            axis.text(
                0.98,
                0.02,
                spec.marker,
                transform=axis.transAxes,
                ha="right",
                va="bottom",
                fontsize=14,
                color="#b3261e",
                fontweight="bold",
            )


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _normalize_png_dpi(path: Path, dpi: int) -> None:
    temporary = path.with_name(f".{path.name}.dpi")
    with Image.open(path) as image:
        image.load()
        image.save(
            temporary,
            format="PNG",
            dpi=(dpi, dpi),
        )
    os.replace(temporary, path)
