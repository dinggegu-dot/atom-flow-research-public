"""Small ordered evidence representation for DOCX and Web; no database schema."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict


class IngestionError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SourceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    block_id: str
    kind: Literal["heading", "paragraph", "table", "list"]
    text: str
    locator: dict[str, str]


def append_block(blocks: list[SourceBlock], kind: str, text: str) -> None:
    if text.strip():
        block_id = f"b{len(blocks) + 1}"
        blocks.append(SourceBlock(block_id=block_id, kind=kind, text=text.strip(), locator={"block": block_id}))


def blocks_markdown(blocks: list[SourceBlock]) -> str:
    return "\n\n".join(("## " if b.kind == "heading" else "") + b.text for b in blocks) + "\n"


def table_markdown(rows: list[list[str]]) -> str:
    if not rows or not any(cell.strip() for row in rows for cell in row):
        return ""
    width = max(map(len, rows))
    def line(row):
        cells = [s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ") for s in row]
        return "| " + " | ".join(cells + [""] * (width - len(cells))) + " |"
    return "\n".join([line(rows[0]), line(["---"] * width), *[line(row) for row in rows[1:]]])


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    blocks: list[SourceBlock]
    metadata: dict

    @property
    def markdown(self) -> str:
        return blocks_markdown(self.blocks)
