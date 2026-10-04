from app.loss.domain import LossModelType
from app.loss.service import LossAssessmentService


def test_algorithm_versions_are_stable() -> None:
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.BUILDING_DAMAGE] == (
        "building-structure-matrix-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.POPULATION_IMPACT] == (
        "population-intensity-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.CASUALTIES] == (
        "casualty-building-intensity-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.ECONOMIC_LOSS] == (
        "economic-building-loss-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.RESOURCE_DEMAND] == (
        "resource-linear-demand-v1"
    )
