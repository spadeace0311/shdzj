from datetime import datetime

import pytest

from app.artifacts.domain import ArtifactNameContext, ProductionMode
from app.artifacts.naming import build_artifact_file_name


def test_file_name_contains_required_mode_marker_and_version() -> None:
    context = ArtifactNameContext(
        place="浦东新区",
        magnitude=5.1,
        display_name="震中位置分布图",
        version=1,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.TEST,
    )

    assert build_artifact_file_name(context, extension="jpg") == (
        "【测试】浦东新区_5.1级地震_震中位置分布图_V001_20260930-153000.jpg"
    )


def test_file_name_sanitizes_windows_illegal_characters() -> None:
    context = ArtifactNameContext(
        place='浦东<新区>:"/\\|?*',
        magnitude=4.0,
        display_name="震中位置分布图",
        version=2,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.LIVE,
    )

    assert build_artifact_file_name(context, extension="png") == (
        "浦东_新区_4.0级地震_震中位置分布图_V002_20260930-153000.png"
    )


@pytest.mark.parametrize(
    ("mode", "marker"),
    (
        (ProductionMode.LIVE, ""),
        (ProductionMode.MANUAL, ""),
        (ProductionMode.TEST, "【测试】"),
        (ProductionMode.DRILL, "【演练】"),
        (ProductionMode.REPLAY, "【测试回放】"),
    ),
)
def test_file_name_uses_exact_mode_markers(
    mode: ProductionMode,
    marker: str,
) -> None:
    context = ArtifactNameContext(
        place="浦东新区",
        magnitude=5.0,
        display_name="辅助决策报告",
        version=12,
        generated_at=datetime.fromisoformat("2026-09-30T07:30:00+00:00"),
        production_mode=mode,
    )

    assert build_artifact_file_name(context, extension="docx") == (
        f"{marker}浦东新区_5.0级地震_辅助决策报告_V012_20260930-153000.docx"
    )


def test_file_name_trims_and_normalizes_place_without_losing_required_fields() -> None:
    context = ArtifactNameContext(
        place=(" 浦东__新区... " * 20) + "<invalid>",
        magnitude=5.04,
        display_name="震中位置分布图",
        version=999,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.LIVE,
    )

    file_name = build_artifact_file_name(context, extension="jpg")
    place = file_name.removeprefix("").split("_5.0级地震_", maxsplit=1)[0]

    assert len(place) <= 80
    assert "__" not in file_name
    assert all(character not in file_name for character in '<>:"/\\|?*')
    assert file_name.endswith(
        "_震中位置分布图_V999_20260930-153000.jpg"
    )


@pytest.mark.parametrize("extension", ("jpeg", "pdf", "", "jpg.exe"))
def test_file_name_rejects_unsupported_extensions(extension: str) -> None:
    context = ArtifactNameContext(
        place="浦东新区",
        magnitude=5.1,
        display_name="震中位置分布图",
        version=1,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.LIVE,
    )

    with pytest.raises(ValueError):
        build_artifact_file_name(context, extension=extension)


def test_file_name_rejects_version_that_cannot_fit_three_digits() -> None:
    context = ArtifactNameContext(
        place="浦东新区",
        magnitude=5.1,
        display_name="震中位置分布图",
        version=1000,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.LIVE,
    )

    with pytest.raises(ValueError):
        build_artifact_file_name(context, extension="jpg")
