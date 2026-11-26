"""Bounded DOCX parsing. Original bytes remain authoritative; no page invention."""

from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree

from docx import Document

from app.source_blocks import IngestionError


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024


def open_docx(payload: bytes):
    if len(payload) > MAX_DOCX_BYTES:
        raise IngestionError("FILE_TOO_LARGE", "DOCX files must be no larger than 20 MB.", 413)
    try:
        with ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                raise IngestionError("FILE_TOO_LARGE", "The expanded DOCX is too large.", 413)
            names = {e.filename for e in entries}
            if not {"[Content_Types].xml", "word/document.xml"} <= names or len(names) != len(entries):
                raise ValueError("Not a DOCX package")
            if archive.testzip() is not None:
                raise ValueError("Corrupt archive")
            # Count all embedded media, including header/footer images and legacy drawings.
            image_count = sum(n.startswith("word/media/") and not n.endswith("/") for n in names)
            unsupported_parts = []
            # Word模板常自带空的脚注/尾注分隔符；存在部件不等于漏读实际内容。
            note_type = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type"
            for name in sorted(names & {"word/footnotes.xml", "word/endnotes.xml"}):
                notes = ElementTree.fromstring(archive.read(name))
                if any(note.get(note_type) not in {"separator", "continuationSeparator", "continuationNotice"} for note in notes):
                    unsupported_parts.append(name)
        document = Document(BytesIO(payload))
        # python-docx does not expose these in the ordinary body iterator.
        unsupported_tags = {"txbxContent", "ins", "del", "sdt", "altChunk", "object", "oMath", "oMathPara"}
        unsupported = sorted({el.tag.rsplit("}", 1)[-1] for el in document.element.iter() if isinstance(el.tag, str)} & unsupported_tags)
        if document.element.xpath(".//w:tbl//w:tbl"):
            unsupported.append("nested_table")
        if any(part.tables or any(p.text.strip() for p in part.paragraphs)
               or part._element.xpath(".//w:drawing | .//w:pict")
               for section in document.sections
               for part in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                            section.even_page_header, section.even_page_footer)):
            unsupported.append("header_footer")
        shape_count = len(document.element.xpath(".//wp:inline | .//wp:anchor | .//w:pict"))
        unsupported.extend(unsupported_parts)
        return document, {"embedded_image_count":image_count,"unsupported_structures":unsupported}
    except IngestionError:
        raise
    except Exception:
        raise IngestionError("INVALID_DOCX", "The file is not a valid, readable DOCX document.") from None
