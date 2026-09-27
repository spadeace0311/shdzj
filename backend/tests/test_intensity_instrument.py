import hashlib
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


def test_normalized_checksum_uses_deterministic_content_bytes() -> None:
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
    expected = hashlib.sha256()
    expected.update(np.array([3.0, 4.0], dtype=np.float64).tobytes())
    expected.update(np.array([0.2, 0.3], dtype=np.float64).tobytes())
    expected.update(b"Q1Q2")

    normalized = validate_instrument_product(product, DEFINITION)

    assert normalized.normalized_checksum == expected.hexdigest()


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
