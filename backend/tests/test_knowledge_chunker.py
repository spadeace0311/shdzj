import hashlib

from app.knowledge.chunker import chunk_document
from app.knowledge.parser import ParsedBlock, ParsedDocument


def test_chunker_preserves_section_and_overlap() -> None:
    text = "第一段。" * 240
    document = ParsedDocument(
        title="预案",
        blocks=[
            ParsedBlock(
                kind="paragraph",
                text=text,
                page=1,
                section_path=("第二章", "响应分级"),
                row_range=None,
                metadata={},
            )
        ],
        metadata={},
    )

    chunks = chunk_document(
        document,
        min_chars=500,
        max_chars=1000,
        overlap_chars=100,
    )

    assert len(chunks) >= 2
    assert all(chunk.section_path == ("第二章", "响应分级") for chunk in chunks)
    assert chunks[0].text[-100:] == chunks[1].text[:100]
    assert all(500 <= len(chunk.text) <= 1000 for chunk in chunks)


def test_chunker_starts_new_segment_on_heading_change() -> None:
    document = ParsedDocument(
        title="预案",
        blocks=[
            ParsedBlock(
                kind="heading",
                text="第一章",
                page=None,
                section_path=("第一章",),
                row_range=None,
                metadata={},
            ),
            ParsedBlock(
                kind="paragraph",
                text="震后立即报告。" * 80,
                page=None,
                section_path=("第一章",),
                row_range=None,
                metadata={},
            ),
            ParsedBlock(
                kind="heading",
                text="第二章",
                page=None,
                section_path=("第二章",),
                row_range=None,
                metadata={},
            ),
            ParsedBlock(
                kind="paragraph",
                text="响应分级启动。" * 80,
                page=None,
                section_path=("第二章",),
                row_range=None,
                metadata={},
            ),
        ],
        metadata={},
    )

    chunks = chunk_document(document, min_chars=500, max_chars=1000, overlap_chars=100)

    assert {chunk.section_path for chunk in chunks} == {("第一章",), ("第二章",)}
    assert all(chunk.section_path in {("第一章",), ("第二章",)} for chunk in chunks)


def test_chunker_preserves_overlap_after_a_long_sentence() -> None:
    text = "前置。" * 50 + "长" * 1200 + "。"
    document = ParsedDocument(
        title="长句预案",
        blocks=[
            ParsedBlock(
                kind="paragraph",
                text=text,
                page=1,
                section_path=("第三章", "处置要求"),
                row_range=None,
                metadata={},
            )
        ],
        metadata={},
    )

    chunks = chunk_document(document, min_chars=500, max_chars=1000, overlap_chars=100)

    assert chunks
    assert all(500 <= len(chunk.text) <= 1000 for chunk in chunks)
    assert all(
        first.text[-100:] == second.text[:100]
        for first, second in zip(chunks, chunks[1:])
    )


def test_chunker_hard_split_default_1301_respects_limits() -> None:
    text = "长" * 1301
    document = ParsedDocument(
        title="默认硬切片",
        blocks=[
            ParsedBlock(
                kind="paragraph",
                text=text,
                page=1,
                section_path=(),
                row_range=None,
                metadata={},
            )
        ],
        metadata={},
    )

    chunks = chunk_document(document)

    assert [len(chunk.text) for chunk in chunks] == [701, 700]
    assert all(500 <= len(chunk.text) <= 1000 for chunk in chunks)
    assert chunks[0].text[-100:] == chunks[1].text[:100]


def test_chunker_hard_split_custom_81_respects_max() -> None:
    text = "长" * 81
    document = ParsedDocument(
        title="自定义硬切片",
        blocks=[
            ParsedBlock(
                kind="paragraph",
                text=text,
                page=1,
                section_path=(),
                row_range=None,
                metadata={},
            )
        ],
        metadata={},
    )

    chunks = chunk_document(
        document,
        min_chars=50,
        max_chars=80,
        overlap_chars=10,
    )

    assert [len(chunk.text) for chunk in chunks] == [46, 45]
    assert all(len(chunk.text) <= 80 for chunk in chunks)
    assert chunks[0].text[-10:] == chunks[1].text[:10]


def test_chunker_tracks_pages_per_cross_page_chunk() -> None:
    document = ParsedDocument(
        title="跨页正文",
        blocks=[
            ParsedBlock(
                kind="paragraph",
                text="第一页。" * 30,
                page=1,
                section_path=("总则",),
                row_range=None,
                metadata={},
            ),
            ParsedBlock(
                kind="paragraph",
                text="第二页。" * 30,
                page=2,
                section_path=("总则",),
                row_range=None,
                metadata={},
            ),
        ],
        metadata={},
    )

    chunks = chunk_document(document, min_chars=50, max_chars=80, overlap_chars=10)
    page_ranges = {(chunk.page_from, chunk.page_to) for chunk in chunks}

    assert (1, 1) in page_ranges
    assert (1, 2) in page_ranges
    assert (2, 2) in page_ranges


def test_chunker_keeps_table_rows_and_repeats_header() -> None:
    document = ParsedDocument(
        title="表格",
        blocks=[
            ParsedBlock(
                kind="table",
                text="",
                page=2,
                section_path=("附件",),
                row_range=(2, 21),
                metadata={
                    "headers": ["区域", "等级"],
                    "rows": [[f"区域{i}", "一级"] for i in range(20)],
                },
            )
        ],
        metadata={},
    )

    chunks = chunk_document(document, min_chars=20, max_chars=100, overlap_chars=5)

    assert chunks
    assert all(chunk.table_range is not None for chunk in chunks)
    assert all("区域" in chunk.text and "等级" in chunk.text for chunk in chunks)


def test_chunker_writes_checksum_into_metadata() -> None:
    text = "震后立即报告。" * 80
    document = ParsedDocument(
        title="预案",
        blocks=[
            ParsedBlock(
                kind="paragraph",
                text=text,
                page=None,
                section_path=(),
                row_range=None,
                metadata={},
            )
        ],
        metadata={},
    )

    chunks = chunk_document(document)

    assert chunks
    for chunk in chunks:
        assert chunk.metadata["checksum"] == hashlib.sha256(
            chunk.text.encode("utf-8")
        ).hexdigest()
