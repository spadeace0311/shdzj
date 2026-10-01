async def test_retention_removes_expired_test_and_drill_but_not_live(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    test_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    drill_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="drill",
        age_days=731,
    )
    live_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="live",
        age_days=10_000,
    )

    removed = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
    )

    assert removed.test_deleted == 1
    assert removed.drill_deleted == 1
    assert removed.live_deleted == 0
    assert await session.get(type(test_artifact), test_artifact.id) is None
    assert await session.get(type(drill_artifact), drill_artifact.id) is None
    assert await session.get(type(live_artifact), live_artifact.id) is not None
