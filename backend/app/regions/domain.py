from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RegionContext:
    inside_shanghai: bool | None
    distance_to_boundary_km: Decimal | None
    boundary_version: str | None
    computed_at: datetime
