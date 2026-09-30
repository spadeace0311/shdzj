from __future__ import annotations

import hashlib
import warnings
from dataclasses import replace
from pathlib import Path

import pytest
from docx import Document
import numpy as np
from PIL import Image

from app.artifacts.renderers.chart_renderer import ChartRenderer, ChartSeries, ChartSpec
from app.artifacts.renderers.docx_renderer import (
    DocxRenderer,
    build_background_document_spec,
)
from app.artifacts.template_builder import build_background_templates

BACKGROUND_DOCS = (
    "doc.background",
    "doc.housing",
    "doc.economy",
    "doc.population",
    "doc.key_targets",
    "doc.spatial_distances",
    "doc.area_overview",
    "doc.historical_catalog",
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture
def docx_renderer() -> DocxRenderer:
    return DocxRenderer()


@pytest.fixture
def chart_renderer() -> ChartRenderer:
    return ChartRenderer()


class CountingChartRenderer(ChartRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.render_count = 0

    def render(self, spec: ChartSpec, output_path: Path):
        self.render_count += 1
        return super().render(spec, output_path)


@pytest.mark.parametrize("artifact_key", BACKGROUND_DOCS)
async def test_background_documents_are_valid_docx(
    artifact_key: str,
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(artifact_key)
    result = await docx_renderer.render(
        build_background_document_spec(context, artifact_key),
        tmp_path / f"{artifact_key}.docx",
    )
    document = Document(result.path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)

    assert result.page_count >= 1
    assert "{{" not in text
    assert "事件名称" in text
    assert "数据来源" in text
    assert result.control_fields["template_version"] == "v1"


@pytest.mark.parametrize(
    ("production_mode", "marker"),
    (
        ("test", "【测试】"),
        ("drill", "【演练】"),
        ("replay", "【测试回放】"),
    ),
)
async def test_docx_contains_mode_marker_in_filename_body_manifest_and_chart(
    production_mode: str,
    marker: str,
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.population",
        production_mode=production_mode,
    )
    spec = build_background_document_spec(context, "doc.population")
    assert all(chart.marker == marker for chart in spec.charts)
    result = await docx_renderer.render(
        spec,
        tmp_path / f"{production_mode}.docx",
    )
    document = Document(result.path)

    assert marker in result.file_name
    assert marker in document.paragraphs[0].text
    assert result.render_manifest["marker"] == marker
    chart_path = Path(result.render_manifest["image_paths"]["population-impact"])
    image = np.asarray(Image.open(chart_path).convert("RGB"))
    height, width = image.shape[:2]
    marker_region = image[
        int(height * 0.70):height,
        int(width * 0.60):width,
    ]
    assert np.any(
        (marker_region[:, :, 0] > 120)
        & (marker_region[:, :, 1] < 90)
        & (marker_region[:, :, 2] < 90)
    )


async def test_docx_reuses_the_same_map_checksum_for_repeated_reference(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("doc.area_overview")
    spec = build_background_document_spec(context, "doc.area_overview")
    result = await docx_renderer.render(spec, tmp_path / "overview.docx")

    assert result.render_manifest["image_checksums"]["map.epicenter"] == (
        context.artifacts["map.epicenter"].checksum
    )


async def test_docx_uses_not_applicable_t1_when_null(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.background",
        t1_at=None,
    )
    result = await docx_renderer.render(
        build_background_document_spec(context, "doc.background"),
        tmp_path / "manual.docx",
    )
    text = "\n".join(paragraph.text for paragraph in Document(result.path).paragraphs)

    assert "T1: 不适用（人工/测试/演练）" in text
    assert "不适用（人工/测试/演练）" in text


async def test_chart_renderer_generates_deterministic_local_png(
    chart_renderer: ChartRenderer,
    tmp_path: Path,
) -> None:
    spec = ChartSpec(
        chart_id="population-age",
        title="常住人口年龄结构",
        labels=("0-14岁", "15-64岁", "65岁及以上"),
        series=(
            ChartSeries(name="常住人口", values=(18.2, 71.6, 10.2)),
        ),
        y_label="比例（%）",
        marker="【测试】",
    )
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"

    first = chart_renderer.render(spec, first_path)
    second = chart_renderer.render(spec, second_path)

    assert first.checksum == second.checksum
    assert first.render_manifest["image_checksums"]["population-age"] == first.checksum
    assert first.render_manifest["font_family"].startswith("Noto Sans CJK")
    assert first.render_manifest["marker_baked"] is True
    with Image.open(first_path) as image:
        assert image.size == (spec.width, spec.height)
        assert image.format == "PNG"
        assert tuple(
            round(value) for value in image.info.get("dpi", (0, 0))
        ) == (spec.dpi, spec.dpi)


def test_chart_renderer_uses_configured_cjk_font_without_missing_glyph_warnings(
    chart_renderer: ChartRenderer,
    tmp_path: Path,
) -> None:
    spec = ChartSpec(
        chart_id="population-age",
        title="常住人口年龄结构",
        labels=("0-14岁", "15-64岁", "65岁及以上"),
        series=(ChartSeries(name="常住人口", values=(18.2, 71.6, 10.2)),),
        marker="【测试】",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        chart_renderer.render(spec, tmp_path / "font.png")

    assert not any("Glyph" in str(item.message) for item in caught)
    assert not any("missing from font" in str(item.message) for item in caught)


async def test_docx_rejects_template_checksum_mismatch(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("doc.background")
    spec = build_background_document_spec(context, "doc.background")
    bad_spec = replace(spec, template_checksum="0" * 64)

    with pytest.raises(ValueError, match="template checksum mismatch"):
        await docx_renderer.render(bad_spec, tmp_path / "bad.docx")


async def test_docx_footer_contains_real_file_name_and_page_field(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("doc.background")
    result = await docx_renderer.render(
        build_background_document_spec(context, "doc.background"),
        tmp_path / "background.docx",
    )
    document = Document(result.path)
    footer_xml = document.sections[0].footer.paragraphs[0]._element.xml

    assert result.file_name in footer_xml
    assert "PAGE" in footer_xml
    assert result.render_manifest["page_count_method"] == "declared_page_breaks_plus_one"


async def test_docx_renders_each_chart_once(
    seeded_artifact_assessment,
    tmp_path: Path,
) -> None:
    chart_renderer = CountingChartRenderer()
    renderer = DocxRenderer(chart_renderer=chart_renderer)
    context = await seeded_artifact_assessment.document_context("doc.population")
    result = await renderer.render(
        build_background_document_spec(context, "doc.population"),
        tmp_path / "population.docx",
    )

    assert chart_renderer.render_count == 1
    assert "population-impact" in result.render_manifest["image_checksums"]


def test_build_background_templates_is_deterministic_and_contains_placeholders(
    tmp_path: Path,
) -> None:
    first = build_background_templates(tmp_path / "first")
    second = build_background_templates(tmp_path / "second")
    first_path = first["background-template"]
    second_path = second["background-template"]

    assert _sha256_path(first_path) == _sha256_path(second_path)
    document = Document(first_path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "{{title}}" in text
    assert "{{section_1_title}}" in text
    assert "{{section_1_body}}" in text
    assert "{{section_1_image}}" in text
    footer_text = document.sections[0].footer.paragraphs[0].text
    assert "{{file_name}}" in footer_text
    assert "{{page_number}}" in footer_text
    section = document.sections[0]
    assert round(section.page_width.cm) == 21
    assert round(section.page_height.cm) == 30


@pytest.mark.parametrize(
    ("artifact_key", "expected_sections"),
    (
        (
            "doc.background",
            {"事件参数", "所在行政区", "历史地震", "邻近断裂", "基础地理背景"},
        ),
        (
            "doc.housing",
            {"街镇房屋总量", "结构类型", "破坏统计", "覆盖与质量"},
        ),
        (
            "doc.economy",
            {"GDP", "产业结构", "经济损失", "来源与情景"},
        ),
        (
            "doc.population",
            {"常住人口", "流动人口", "家庭户", "年龄结构", "受灾人口"},
        ),
        (
            "doc.key_targets",
            {"避难场所", "学校", "医院", "危险源", "救援队伍", "文物单位", "重点目标列表"},
        ),
        (
            "doc.spatial_distances",
            {"城市距离", "区县距离", "街镇距离", "主要城市距离", "重点目标距离", "断裂距离"},
        ),
        (
            "doc.area_overview",
            {"地理概况", "行政区", "烈度", "人口", "房屋", "经济", "关键风险"},
        ),
        (
            "doc.historical_catalog",
            {"检索半径", "震级阈值", "历史地震目录", "灾害地震目录", "统计"},
        ),
    ),
)
async def test_each_background_document_has_its_required_sections(
    artifact_key: str,
    expected_sections: set[str],
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(artifact_key)
    spec = build_background_document_spec(context, artifact_key)
    result = await docx_renderer.render(spec, tmp_path / f"{artifact_key}.docx")
    text = "\n".join(paragraph.text for paragraph in Document(result.path).paragraphs)

    for section in expected_sections:
        assert section in text


@pytest.mark.parametrize(
    ("artifact_key", "expected_values"),
    (
        ("doc.background", {"12 条，最大震级 4.9", "长江三角洲冲积平原"}),
        ("doc.housing", {"10000", "1200", "300", "80"}),
        ("doc.economy", {"560000", "12000", "220000", "328000", "8800"}),
        ("doc.population", {"120000", "18000", "52000", "4300", "18.2%"}),
        ("doc.key_targets", {"避难场所 45 处", "学校 118 所", "医院 36 所"}),
        ("doc.spatial_distances", {"8.6 km", "12.4 km", "5.2 km"}),
        ("doc.area_overview", {"长江三角洲冲积平原", "人口密集区、重点目标、危险源与断裂带"}),
        ("doc.historical_catalog", {"50 km", "M >= 3.0", "12 条 / 3 条灾害"}),
    ),
)
async def test_background_documents_use_concrete_manifest_values(
    artifact_key: str,
    expected_values: set[str],
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(artifact_key)
    result = await docx_renderer.render(
        build_background_document_spec(context, artifact_key),
        tmp_path / f"{artifact_key}.docx",
    )
    text = "\n".join(paragraph.text for paragraph in Document(result.path).paragraphs)

    for expected in expected_values:
        assert expected in text


async def test_doc_background_uses_administration_scalar_not_dict(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("doc.background")
    result = await docx_renderer.render(
        build_background_document_spec(context, "doc.background"),
        tmp_path / "background.docx",
    )
    text = "\n".join(paragraph.text for paragraph in Document(result.path).paragraphs)
    normalized_text = text.replace("\n", "")

    assert "上海市及邻近行政区" in normalized_text
    assert "{'geography'" not in normalized_text
