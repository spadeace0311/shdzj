from __future__ import annotations

import hashlib
import re
from itertools import zip_longest
from typing import Iterable

from app.knowledge.domain import ChunkDraft
from app.knowledge.parser import ParsedBlock, ParsedDocument


def chunk_document(
    document: ParsedDocument,
    *,
    min_chars: int = 500,
    max_chars: int = 1000,
    overlap_chars: int = 100,
) -> list[ChunkDraft]:
    if not 0 <= overlap_chars < min_chars <= max_chars:
        raise ValueError("invalid chunking parameters")

    chunks: list[ChunkDraft] = []
    buffer: list[str] = []
    section_path: tuple[str, ...] | None = None
    page_numbers: list[int | None] = []

    def flush_paragraphs() -> None:
        nonlocal buffer, page_numbers
        if not buffer:
            return
        text = "\n".join(buffer)
        for piece in _chunk_text(
            text,
            min_chars=min_chars,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        ):
            chunks.append(
                _draft(
                    text=piece,
                    section_path=section_path or (),
                    page_numbers=page_numbers,
                )
            )
        buffer = []
        page_numbers = []

    for block in document.blocks:
        if block.kind == "heading":
            flush_paragraphs()
            section_path = block.section_path
            continue
        if block.kind == "table":
            flush_paragraphs()
            section_path = block.section_path
            chunks.extend(
                _chunk_table(
                    block,
                    max_chars=max_chars,
                )
            )
            continue
        if section_path is None:
            section_path = block.section_path
        elif block.section_path != section_path:
            flush_paragraphs()
            section_path = block.section_path
        if block.text.strip():
            buffer.append(block.text)
            page_numbers.append(block.page)

    flush_paragraphs()
    return chunks


def _chunk_text(
    text: str,
    *,
    min_chars: int,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    if not text.strip():
        return []
    if len(text) <= max_chars and len(text) < (
        2 * min_chars - overlap_chars
    ):
        return [text]

    sentences = _split_sentences(text)
    remaining_chars = [0] * (len(sentences) + 1)
    for index in range(len(sentences) - 1, -1, -1):
        remaining_chars[index] = (
            remaining_chars[index + 1] + len(sentences[index])
        )

    chunks: list[str] = []
    buffer = ""
    for index, sentence in enumerate(sentences):
        if not sentence:
            continue
        remaining_after = remaining_chars[index + 1]

        if len(sentence) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            hard_chunks = _split_long_sentence(
                sentence,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
            chunks.extend(hard_chunks)
            buffer = (
                hard_chunks[-1][-overlap_chars:]
                if overlap_chars and hard_chunks
                else ""
            )
            continue

        if buffer and len(buffer) + len(sentence) > max_chars:
            chunks.append(buffer)
            buffer = buffer[-overlap_chars:] if overlap_chars else ""
            if buffer and len(buffer) + len(sentence) > max_chars:
                buffer = ""

        buffer += sentence
        if len(buffer) >= min_chars and remaining_after:
            projected_final = (
                len(buffer[-overlap_chars:]) if overlap_chars else 0
            ) + remaining_after
            if projected_final >= min_chars:
                chunks.append(buffer)
                buffer = buffer[-overlap_chars:] if overlap_chars else ""

    if buffer.strip():
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()]


def _split_sentences(text: str) -> list[str]:
    return [
        part
        for part in re.split(r"(?<=[。！？!?])", text)
        if part
    ]


def _split_long_sentence(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        next_start = end - overlap_chars
        start = max(next_start, start + 1)
    return chunks


def _chunk_table(
    block: ParsedBlock,
    *,
    max_chars: int,
) -> list[ChunkDraft]:
    headers = _string_list(block.metadata.get("headers"))
    rows = _rows_from_metadata(block.metadata.get("rows"))
    if not rows:
        return []

    header_line = " | ".join(headers)
    base_length = len(header_line) + 1 if header_line else 0
    first_row_number = block.row_range[0] if block.row_range else 1

    chunks: list[ChunkDraft] = []
    group_start = 0
    group_lines: list[str] = []
    group_length = base_length

    for index, row in enumerate(rows):
        row_line = _format_table_row(headers, row)
        added_length = len(row_line) + (1 if group_lines else 0)
        if group_lines and group_length + added_length > max_chars:
            chunks.append(
                _table_draft(
                    block=block,
                    header_line=header_line,
                    row_lines=group_lines,
                    row_from=first_row_number + group_start,
                    row_to=first_row_number + index - 1,
                )
            )
            group_start = index
            group_lines = []
            group_length = base_length
        group_lines.append(row_line)
        group_length += added_length

    if group_lines:
        chunks.append(
            _table_draft(
                block=block,
                header_line=header_line,
                row_lines=group_lines,
                row_from=first_row_number + group_start,
                row_to=first_row_number + len(rows) - 1,
            )
        )
    return chunks


def _table_draft(
    *,
    block: ParsedBlock,
    header_line: str,
    row_lines: Iterable[str],
    row_from: int,
    row_to: int,
) -> ChunkDraft:
    lines = [header_line] if header_line else []
    lines.extend(row_lines)
    text = "\n".join(lines)
    metadata: dict[str, object] = {
        "checksum": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "headers": block.metadata.get("headers", []),
        "kind": "table",
    }
    if "sheet_name" in block.metadata:
        metadata["sheet_name"] = block.metadata["sheet_name"]
    return ChunkDraft(
        text=text,
        section_path=block.section_path,
        page_from=block.page,
        page_to=block.page,
        table_range=(row_from, row_to),
        metadata=metadata,
    )


def _draft(
    *,
    text: str,
    section_path: tuple[str, ...],
    page_numbers: list[int | None],
) -> ChunkDraft:
    present_pages = [page for page in page_numbers if page is not None]
    metadata = {
        "checksum": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "kind": "paragraph",
    }
    return ChunkDraft(
        text=text,
        section_path=section_path,
        page_from=min(present_pages) if present_pages else None,
        page_to=max(present_pages) if present_pages else None,
        table_range=None,
        metadata=metadata,
    )


def _format_table_row(headers: list[str], row: list[str]) -> str:
    if not headers:
        return " | ".join(row)
    return " | ".join(
        f"{header}: {value}"
        for header, value in zip_longest(headers, row, fillvalue="")
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _rows_from_metadata(value: object) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    return [
        [str(cell) for cell in row]
        for row in value
        if isinstance(row, list)
    ]
