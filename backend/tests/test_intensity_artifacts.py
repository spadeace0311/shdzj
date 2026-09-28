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
    assert metadata["grid_definition_version"] == "grid-1"
    assert metadata["origin_x"] == 500000.0
    assert metadata["origin_y"] == 3500000.0
    assert metadata["resolution_m"] == 1000.0
    assert metadata["srid"] == 32651
    assert metadata["bands"] == [
        {"number": 1, "name": "value"},
        {"number": 2, "name": "sigma"},
    ]
