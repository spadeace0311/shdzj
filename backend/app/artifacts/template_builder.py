from __future__ import annotations

import io
import os
from pathlib import Path
import zipfile

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

CONTROL_FIELDS = (
    ("event_name", "事件名称"),
    ("magnitude", "震级"),
    ("origin_time", "发震时刻"),
    ("longitude", "震中经度"),
    ("latitude", "震中纬度"),
    ("depth_km", "震源深度"),
    ("t1_at", "T1"),
    ("data_source", "数据来源"),
    ("report_type", "报告类型"),
    ("mode_marker", "成果标识"),
    ("generated_at", "生成时间"),
    ("artifact_version", "成果版本"),
    ("revision_no", "事件修订号"),
    ("assessment_run_no", "评估运行号"),
    ("data_asset_snapshot_fingerprint", "数据资产快照指纹"),
    ("model_parameter_version", "模型参数版本"),
    ("template_version", "模板版本"),
    ("quality_grade", "质量等级"),
    ("degradation_reasons", "降级原因"),
    ("needs_review", "待复核项"),
)


def build_background_templates(output_dir: Path) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "background-template.docx"
    temporary = output_dir / f".background-template.{os.getpid()}.docx"

    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)
    section.start_type = WD_SECTION.NEW_PAGE

    normal = document.styles["Normal"]
    normal.font.name = "Noto Sans CJK SC"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(6)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.add_run("{{title}}").bold = True
    title.runs[0].font.size = Pt(22)

    document.add_heading("事件控制表", level=1)
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for field_name, label in CONTROL_FIELDS:
        row = table.add_row()
        row.cells[0].text = label
        row.cells[1].text = f"{{{{{field_name}}}}}"

    document.add_heading("{{section_1_title}}", level=1)
    document.add_paragraph("{{section_1_body}}")
    document.add_paragraph("{{section_1_image}}")

    footer = document.sections[0].footer
    footer_paragraph = footer.paragraphs[0]
    footer_paragraph.text = (
        "{{file_name}} | 第 {{page_number}} 页 | 版本 {{version}} | "
        "{{generated_by}}"
    )

    _save_deterministic_docx(document, temporary)
    os.replace(temporary, target)
    return {"background-template": target}


def _save_deterministic_docx(document: Document, target: Path) -> None:
    buffer = io.BytesIO()
    document.save(buffer)
    buffer.seek(0)
    with zipfile.ZipFile(buffer, "r") as source:
        with zipfile.ZipFile(
            target,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as destination:
            for item in source.infolist():
                info = zipfile.ZipInfo(
                    item.filename,
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                destination.writestr(
                    info,
                    source.read(item.filename),
                )
