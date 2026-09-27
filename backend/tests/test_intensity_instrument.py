import hashlib
from datetime import UTC, datetime

import numpy as np
import pytest

from app.intensity.domain import (
    GridDefinition,
    InstrumentProduct,
    InstrumentProductFormat,
    InstrumentQuality,
    ProductStatus,
)
from app.intensity.instrument import (
    InstrumentRequest,
    UnavailableInstrumentProvider,
    validate_instrument_product,
)


DEFINITION = GridDefinition("grid-1", "EPSG:32651", 1000, 0, 2000, 2, 1)
OBSERVED_AT = datetime(2026, 9, 27, tzinfo=UTC)


def _product(
    *,
    status: ProductStatus = ProductStatus.AVAILABLE,
    product_format: InstrumentProductFormat | None = InstrumentProductFormat.GRID,
    source_verified: bool = True,
    grid_version: str | None = "grid-1",
    values: np.ndarray | None = None,
    sigma: np.ndarray | None = None,
    quality_codes: np.ndarray | None = None,
    coverage_ratio: float = 1.0,
    product_id: str | None = "instrument-1",
    product_version: str | None = "v1",
    observed_at: datetime | None = OBSERVED_AT,
    source: str = "eqim-fixture",
    reason: str | None = None,
    generated_at: datetime | None = None,
    crs: str | None = None,
    resolution_m: float | None = None,
    raw_checksum: str | None = None,
    normalized_checksum: str | None = None,
) -> InstrumentProduct:
    if values is None:
        values = np.array([[3.0, 4.0]], dtype=np.float64)
    if sigma is None:
        sigma = np.array([[0.2, 0.3]], dtype=np.float64)
    if quality_codes is None:
        quality_codes = np.array(
            [[InstrumentQuality.Q1, InstrumentQuality.Q2]],
            dtype=object,
        )
    return InstrumentProduct(
        status=status,
        product_id=product_id,
        product_version=product_version,
        observed_at=observed_at,
        source=source,
        format=product_format,
        source_verified=source_verified,
        grid_version=grid_version,
        values=values,
        sigma=sigma,
        quality_codes=quality_codes,
        coverage_ratio=coverage_ratio,
        reason=reason,
        generated_at=generated_at,
        crs=crs,
        resolution_m=resolution_m,
        raw_checksum=raw_checksum,
        normalized_checksum=normalized_checksum,
    )


async def test_default_provider_returns_unavailable_without_fake_grid() -> None:
    product = await UnavailableInstrumentProvider().fetch(
        InstrumentRequest(
            event_id="event-1",
            revision_id="revision-1",
            definition=DEFINITION,
            observed_at=OBSERVED_AT,
        )
    )

    assert product.status is ProductStatus.UNAVAILABLE
    assert product.format is None
    assert product.source_verified is False
    assert product.values is None
    assert product.sigma is None
    assert product.quality_codes is None
    assert product.coverage_ratio == 0.0


def test_valid_product_is_normalized_on_grid() -> None:
    product = _product()

    normalized = validate_instrument_product(product, DEFINITION)

    assert normalized.status is ProductStatus.AVAILABLE
    assert normalized.values.shape == (2,)
    assert normalized.quality_codes.tolist() == ["Q1", "Q2"]
    assert normalized.coverage_ratio == pytest.approx(1.0)
    assert normalized.normalized_checksum is not None
    assert len(normalized.normalized_checksum) == 64


def test_normalized_checksum_is_canonical_and_layout_independent() -> None:
    definition = GridDefinition("grid-1", "EPSG:32651", 1000, 0, 2000, 2, 2)
    values = np.array([[3.0, 4.0], [5.0, 6.0]], dtype=np.float64)
    sigma = np.array([[0.2, 0.3], [0.4, 0.5]], dtype=np.float64)
    codes = np.array(
        [
            [InstrumentQuality.Q1, InstrumentQuality.Q2],
            [InstrumentQuality.Q3, InstrumentQuality.Q0],
        ],
        dtype=object,
    )

    c_order = validate_instrument_product(
        _product(values=values, sigma=sigma, quality_codes=codes),
        definition,
    )
    fortran_order = validate_instrument_product(
        _product(
            values=np.asfortranarray(values),
            sigma=np.asfortranarray(sigma),
            quality_codes=np.asfortranarray(codes),
        ),
        definition,
    )

    expected = hashlib.sha256()
    expected.update(b"instrument-normalized-v1\0")
    expected.update(np.asarray(values, dtype="<f8").tobytes(order="C"))
    expected.update(np.asarray(sigma, dtype="<f8").tobytes(order="C"))
    code_bytes = b"Q1Q2Q3Q0"
    expected.update(len(code_bytes).to_bytes(4, byteorder="little"))
    expected.update(code_bytes)

    assert c_order.normalized_checksum == expected.hexdigest()
    assert fortran_order.normalized_checksum == expected.hexdigest()


@pytest.mark.parametrize("status", (ProductStatus.INVALID, ProductStatus.STALE))
def test_non_fusion_statuses_pass_through(status: ProductStatus) -> None:
    product = _product(
        status=status,
        product_format=InstrumentProductFormat.STATION,
        source_verified=False,
        values=None,
        sigma=None,
        quality_codes=None,
        coverage_ratio=0.0,
    )

    assert validate_instrument_product(product, DEFINITION) is product


@pytest.mark.parametrize(
    "status",
    (ProductStatus.AVAILABLE, ProductStatus.PARTIAL),
)
@pytest.mark.parametrize(
    ("product_format", "source_verified"),
    (
        (InstrumentProductFormat.STATION, True),
        (InstrumentProductFormat.GRID, False),
        (None, False),
    ),
)
def test_available_and_partial_products_require_verified_grid_source(
    status: ProductStatus,
    product_format: InstrumentProductFormat | None,
    source_verified: bool,
) -> None:
    product = _product(
        status=status,
        product_format=product_format,
        source_verified=source_verified,
    )

    with pytest.raises(ValueError, match="verified GRID"):
        validate_instrument_product(product, DEFINITION)


def test_grid_version_mismatch_is_rejected() -> None:
    product = _product(grid_version="other-grid")

    with pytest.raises(ValueError, match="grid version"):
        validate_instrument_product(product, DEFINITION)


def test_negative_sigma_is_rejected() -> None:
    product = _product(sigma=np.array([[-0.1, 0.2]], dtype=np.float64))

    with pytest.raises(ValueError, match="non-negative"):
        validate_instrument_product(product, DEFINITION)


@pytest.mark.parametrize("bad_sigma", (np.nan, np.inf, -np.inf))
def test_non_finite_sigma_is_rejected(bad_sigma: float) -> None:
    product = _product(
        sigma=np.array([[bad_sigma, 0.2]], dtype=np.float64),
    )

    with pytest.raises(ValueError, match="finite"):
        validate_instrument_product(product, DEFINITION)


def test_q0_does_not_contribute_to_coverage() -> None:
    product = _product(
        quality_codes=np.array(
            [[InstrumentQuality.Q1, InstrumentQuality.Q0]],
            dtype=object,
        ),
    )

    normalized = validate_instrument_product(product, DEFINITION)

    assert normalized.status is ProductStatus.PARTIAL
    assert normalized.coverage_ratio == pytest.approx(0.5)


def test_validation_preserves_product_metadata() -> None:
    generated_at = datetime(2026, 9, 27, 1, tzinfo=UTC)
    product = _product(
        product_id="product-42",
        product_version="v42",
        source="verified-grid-provider",
        reason="provider-normalized",
        generated_at=generated_at,
        crs="EPSG:32651",
        resolution_m=1000.0,
        raw_checksum="a" * 64,
    )

    normalized = validate_instrument_product(product, DEFINITION)

    assert normalized.product_id == "product-42"
    assert normalized.product_version == "v42"
    assert normalized.observed_at == OBSERVED_AT
    assert normalized.source == "verified-grid-provider"
    assert normalized.format is InstrumentProductFormat.GRID
    assert normalized.source_verified is True
    assert normalized.reason == "provider-normalized"
    assert normalized.generated_at == generated_at
    assert normalized.crs == "EPSG:32651"
    assert normalized.resolution_m == pytest.approx(1000.0)
    assert normalized.raw_checksum == "a" * 64


def test_invalid_shape_is_rejected() -> None:
    product = _product(
        values=np.zeros((2, 2)),
        sigma=np.zeros((2, 2)),
        quality_codes=np.full((2, 2), InstrumentQuality.Q1),
    )

    with pytest.raises(ValueError, match="shape"):
        validate_instrument_product(product, DEFINITION)
