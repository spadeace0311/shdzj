from app.loss.domain import (
    BuildingStructure,
    DamageState,
    LossQualityGrade,
    ResourceKind,
)


def building_damage_result(severe_fraction: float = 0.10):
    from app.loss.buildings import (
        BuildingDamageResult,
        BuildingDamageStateArea,
        TownBuildingDamage,
    )

    total_area = 10000.0
    raw_fractions = {
        DamageState.BASIC: 0.70 - severe_fraction,
        DamageState.SLIGHTLY_DAMAGED: 0.15,
        DamageState.MODERATELY_DAMAGED: max(0.15 - severe_fraction, 0.0),
        DamageState.SEVERELY_DAMAGED: severe_fraction / 2.0,
        DamageState.COLLAPSED: severe_fraction / 2.0,
    }
    fraction_total = sum(raw_fractions.values())
    state_fractions = {
        state: fraction / fraction_total
        for state, fraction in raw_fractions.items()
    }
    states = tuple(
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=state,
            area_m2=total_area * fraction,
        )
        for state, fraction in state_fractions.items()
    )
    return BuildingDamageResult(
        towns={
            "t1": TownBuildingDamage(
                town_code="t1",
                total_area_m2=total_area,
                states=states,
                quality_grade=LossQualityGrade.L3,
            )
        },
        quality_grade=LossQualityGrade.L3,
        fallback_model_used=True,
    )


def population_impact_result(
    emergency_shelter_population: float = 100.0,
    affected_population: float = 1000.0,
):
    from app.loss.population import (
        IntensityPopulation,
        PopulationImpactResult,
        TownPopulationImpact,
    )

    return PopulationImpactResult(
        towns={
            "t1": TownPopulationImpact(
                town_code="t1",
                full_population=1200.0,
                affected_population=affected_population,
                emergency_shelter_population=emergency_shelter_population,
                temporary_shelter_population=500.0,
                by_intensity=(
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=6,
                        population=600.0,
                        area_ratio=0.5,
                    ),
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=7,
                        population=400.0,
                        area_ratio=0.5,
                    ),
                ),
            )
        },
        quality_grade=LossQualityGrade.L3,
    )


def casualty_result(
    deaths: float = 1.0,
    injuries: float = 2.0,
    buried: float = 3.0,
):
    from app.loss.casualties import CasualtyResult, TownCasualty

    return CasualtyResult(
        towns={
            "t1": TownCasualty(
                town_code="t1",
                deaths=deaths,
                injuries=injuries,
                buried=buried,
                quality_grade=LossQualityGrade.L3,
            )
        },
        quality_grade=LossQualityGrade.L3,
        fallback_model_used=True,
    )


def economic_loss_result():
    from app.loss.economic import EconomicLossResult, TownEconomicLoss

    town = TownEconomicLoss(
        town_code="t1",
        reconstruction_loss_yuan=1000000.0,
        contents_loss_yuan=100000.0,
        total_loss_yuan=1100000.0,
    )
    return EconomicLossResult(
        towns={"t1": town},
        partial_scope=True,
        excluded_scope=(
            "transport",
            "lifelines",
            "business_interruption",
            "indirect_loss",
            "recovery_duration",
        ),
    )


def resource_demand_result():
    from app.loss.resources import (
        ResourceDemandInputs,
        ResourceDemandResult,
        ResourceDemandValue,
    )

    return ResourceDemandResult(
        inputs=ResourceDemandInputs(
            affected_population=1000.0,
            emergency_shelter_population=100.0,
            deaths=1.0,
            injuries=2.0,
            buried=3.0,
        ),
        values={
            kind: ResourceDemandValue(
                quantity=index,
                status="available",
                reason=None,
            )
            for index, kind in enumerate(ResourceKind, start=1)
        },
    )
