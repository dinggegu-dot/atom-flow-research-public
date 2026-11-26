from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid', frozen=True)


class EvidenceLocator(Contract):
    snapshot_id: str
    media_type: str
    page: int | None = Field(default=None,ge=1)
    bbox: tuple[float,float,float,float] | None = None  # PDF points, top-left origin.
    regions: list[dict] = Field(default_factory=list)  # Additional page/bbox anchors, same convention.
    paragraph: int | None = Field(default=None,ge=1)
    paragraph_end: int | None = Field(default=None,ge=1)
    text_start: int | None = Field(default=None,ge=0)
    text_end: int | None = Field(default=None,ge=0)
    table: int | None = Field(default=None,ge=1)
    row: int | None = Field(default=None,ge=0)
    column: int | None = Field(default=None,ge=0)
    time_start: str | None = None
    time_end: str | None = None
    speaker: str | None = None

    @field_validator('bbox')
    @classmethod
    def rectangle(cls,value):
        if value and (not all(math.isfinite(v) for v in value) or value[2]<=value[0] or value[3]<=value[1]):
            raise ValueError('Invalid evidence rectangle')
        return value


class DocumentElement(Contract):
    id: str
    representation_id: str
    order: int = Field(ge=0)
    type: Literal['title','heading','paragraph','list','table','figure','transcript']
    content: str
    locator: EvidenceLocator
    structured_payload: dict | None = None
    semantic_parse_status: Literal['parsed','not_parsed','unavailable'] = 'parsed'

    @model_validator(mode='after')
    def structural_content(self):
        if self.type=='table' and (not self.structured_payload or 'cells' not in self.structured_payload):
            raise ValueError('Tables need structured cells, not prose only')
        if self.type=='figure' and self.semantic_parse_status=='parsed' and not self.content.strip():
            raise ValueError('Empty figures cannot claim semantic understanding')
        return self


class DocumentCoverage(Contract):
    total_pages: int | None = Field(default=None,ge=1)
    readable_pages: list[int] = Field(default_factory=list)
    ocr_pages: list[int] = Field(default_factory=list)
    unreadable_pages: list[int] = Field(default_factory=list)
    image_only_pages: list[int] = Field(default_factory=list)
    tables_detected: int = Field(default=0,ge=0)
    tables_parsed: int = Field(default=0,ge=0)
    figures_detected: int = Field(default=0,ge=0)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid_coverage(self):
        if self.tables_parsed>self.tables_detected:raise ValueError('Parsed tables exceed detected tables')
        if self.total_pages:
            for pages in (self.readable_pages,self.ocr_pages,self.unreadable_pages,self.image_only_pages):
                if len(pages)!=len(set(pages)) or any(p<1 or p>self.total_pages for p in pages):
                    raise ValueError('Invalid coverage page set')
            if set(self.readable_pages)&set(self.unreadable_pages):raise ValueError('Conflicting readability')
            if set(self.readable_pages)|set(self.unreadable_pages)!=set(range(1,self.total_pages+1)):
                raise ValueError('Coverage must account for every page')
            if not set(self.ocr_pages)<=set(self.readable_pages):raise ValueError('OCR pages must be readable')
        return self


class DocumentRepresentation(Contract):
    id: str
    source_id: str
    snapshot_id: str
    parser_id: str
    parser_version: str
    schema_version: str = 'document-representation-v1'
    status: Literal['ready','partial','requires_ocr','failed']
    elements: list[DocumentElement]
    coverage: DocumentCoverage
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda:datetime.now(timezone.utc).isoformat())
    projection_format: Literal['paragraphs-v1','verbatim-v1'] = 'paragraphs-v1'

    @model_validator(mode='after')
    def identities(self):
        if len({e.id for e in self.elements})!=len(self.elements):raise ValueError('Duplicate element ID')
        for i,e in enumerate(self.elements):
            if e.order!=i or e.representation_id!=self.id or e.locator.snapshot_id!=self.snapshot_id:
                raise ValueError('Element ordering/ownership mismatch')
        if self.status=='ready' and self.coverage.unreadable_pages:
            raise ValueError('Incomplete reading cannot claim ready')
        return self


class ParseOptions(Contract):
    enhanced: bool = False
    pages: tuple[int,...] = ()
    max_ocr_pages: int = Field(default=3,ge=1,le=10)
    minimum_readable_ratio: float = Field(default=.95,ge=0,le=1)
    minimum_text_characters: int = Field(default=20,ge=1)
    timeout_seconds: int = Field(default=180,ge=1,le=600)

    @model_validator(mode='after')
    def bounded_escalation(self):
        if self.enhanced and (not self.pages or len(self.pages)>self.max_ocr_pages):
            raise ValueError('Enhanced reading requires an explicit bounded page set')
        if len(set(self.pages))!=len(self.pages) or any(p<1 for p in self.pages):raise ValueError('Invalid page selection')
        return self


@dataclass(frozen=True)
class ParserInput:
    source_id: str
    snapshot_id: str
    source_type: str
    media_type: str
    filename: str
    content: str
    original_bytes: bytes | None = None
    snapshot: object | None = None


def identity():return str(uuid4())
