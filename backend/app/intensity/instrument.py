from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
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
    if (
        product.status is not ProductStatus.AVAILABLE
        and product.status is not ProductStatus.PARTIAL
    ):
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
    )
    valid = np.asarray(
        [
            code
            in {
                InstrumentQuality.Q1.value,
                InstrumentQuality.Q2.value,
                InstrumentQuality.Q3.value,
            }
            for code in normalized_codes.reshape(-1)
        ],
        dtype=bool,
    )
    coverage = float(valid.sum() / definition.cell_count)
    status = ProductStatus.AVAILABLE if coverage == 1.0 else ProductStatus.PARTIAL
    normalized_checksum = hashlib.sha256()
    normalized_checksum.update(np.asarray(product.values, dtype=np.float64).tobytes())
    normalized_checksum.update(np.asarray(product.sigma, dtype=np.float64).tobytes())
    normalized_checksum.update("".join(normalized_codes.tolist()).encode("ascii"))
    return InstrumentProduct(
        status=status,
        product_id=product.product_id,
        product_version=product.product_version,
        observed_at=product.observed_at,
        source=product.source,
        grid_version=product.grid_version,
        values=np.asarray(product.values, dtype=np.float64).reshape(-1),
        sigma=np.asarray(product.sigma, dtype=np.float64).reshape(-1),
        quality_codes=normalized_codes,
        coverage_ratio=coverage,
        reason=product.reason,
        generated_at=product.generated_at,
        crs=product.crs,
        resolution_m=product.resolution_m,
        raw_checksum=product.raw_checksum,
        normalized_checksum=normalized_checksum.hexdigest(),
    )
