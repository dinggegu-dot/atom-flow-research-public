"""python-docx preserves real paragraphs/cells; no DOCX layout engine."""
from io import BytesIO
from importlib.metadata import version
from ..domain import DocumentRepresentation,DocumentCoverage,identity
from .native import element


class NativeDocxAdapter:
    parser_id='native-docx'
    parser_version=version('python-docx')+'+note-coverage-v1'
    def supports(self,source):return source.source_type=='docx'
    def parse(self,source,options):
        from docx import Document
        from docx.table import Table
        from ...source_blocks import table_markdown
        from ...docx_adapter import open_docx
        # Keep the existing bounded ZIP validation, not its legacy derived artifact format.
        document,checks=open_docx(source.original_bytes)
        rep=identity();items=[];paragraph=0;table=0;warnings=[]
        for body in document.iter_inner_content():
            if isinstance(body,Table):
                table+=1;cells=[];rows=[];seen={}
                for r,row in enumerate(body.rows):
                    values=[]
                    for c,cell in enumerate(row.cells):
                        values.append(cell.text)
                        if cell._tc in seen:
                            old=seen[cell._tc];old['row_span']=max(old['row_span'],r-old['row']+1);continue
                        span=int(cell._tc.tcPr.gridSpan.val) if cell._tc.tcPr is not None and cell._tc.tcPr.gridSpan is not None else 1
                        entry={'row':r,'column':c,'text':cell.text,'row_span':1,'column_span':span}
                        cells.append(entry);seen[cell._tc]=entry
                    rows.append(values)
                items.append(element(rep,source,len(items),'table',table_markdown(rows),table=table,
                    payload={'rows':len(rows),'columns':max(map(len,rows),default=0),'cells':cells}))
            else:
                paragraph+=1
                if body.text.strip():
                    style=body.style.name if body.style else ''
                    kind='title' if style=='Title' else 'heading' if style.startswith('Heading') else 'list' if style.startswith('List') else 'paragraph'
                    items.append(element(rep,source,len(items),kind,body.text,paragraph=paragraph,text_start=0,text_end=len(body.text)))
                shapes=body._p.xpath('.//w:drawing | .//w:pict')
                for shape in shapes:
                    items.append(element(rep,source,len(items),'figure','',paragraph=paragraph,
                        semantic='not_parsed',payload={'kind':'embedded_docx_visual'}))
        if checks.get('unsupported_structures'):
            warnings.append('unsupported_docx_content')
            warnings.extend('docx_unparsed:'+part for part in checks['unsupported_structures'])
        figures=sum(e.type=='figure' for e in items)
        if figures:warnings.append('figure_content_not_parsed')
        if checks.get('embedded_image_count',0)>figures:warnings.append('additional_embedded_media_unlocated')
        return DocumentRepresentation(id=rep,source_id=source.source_id,snapshot_id=source.snapshot_id,
            parser_id=self.parser_id,parser_version=self.parser_version,status='partial' if warnings else 'ready',elements=items,
            coverage=DocumentCoverage(tables_detected=table,tables_parsed=table,figures_detected=figures,warnings=warnings),warnings=warnings)
