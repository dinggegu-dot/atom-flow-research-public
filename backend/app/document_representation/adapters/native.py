"""Simple native formats and lossless import of already-frozen historical text."""
import re
from ..domain import DocumentRepresentation,DocumentElement,DocumentCoverage,EvidenceLocator,identity


def element(rep,source,order,kind,text,*,payload=None,semantic='parsed',**locator):
    return DocumentElement(id=f'{rep}:e{order+1}',representation_id=rep,order=order,type=kind,content=text,
        locator=EvidenceLocator(snapshot_id=source.snapshot_id,media_type=source.media_type,**locator),
        structured_payload=payload,semantic_parse_status=semantic)


class NativeTextAdapter:
    parser_id='native-text'
    parser_version='1.0'
    def supports(self,source):return source.source_type in {'text','web'} or source.media_type.startswith('text/')
    def parse(self,source,options):
        from markdown_it import MarkdownIt
        rep=identity();items=[];tokens=MarkdownIt('commonmark').enable('table').parse(source.content)
        lines=source.content.splitlines(keepends=True)
        starts=[0]
        for line in lines:starts.append(starts[-1]+len(line))
        i=0
        while i<len(tokens):
            token=tokens[i]
            if token.type=='table_open':
                rows=[];row=[];begin=i
                while i<len(tokens) and tokens[i].type!='table_close':
                    t=tokens[i]
                    if t.type=='tr_open':row=[]
                    elif t.type=='inline':row.append(t.content)
                    elif t.type=='tr_close':rows.append(row)
                    i+=1
                start,end=tokens[begin].map
                cells=[{'row':r,'column':c,'text':value,'row_span':1,'column_span':1} for r,row in enumerate(rows) for c,value in enumerate(row)]
                items.append(element(rep,source,len(items),'table',''.join(lines[start:end]).strip(),
                    text_start=starts[start],text_end=starts[end],table=sum(x.type=='table' for x in items)+1,
                    payload={'rows':len(rows),'columns':max(map(len,rows),default=0),'cells':cells}))
            elif token.type=='inline' and token.map:
                start,end=token.map;previous=tokens[i-1].type if i else ''
                kind='heading' if previous=='heading_open' else 'paragraph'
                if any(t.type=='list_item_open' and t.level<token.level for t in tokens[max(0,i-3):i]):kind='list'
                items.append(element(rep,source,len(items),kind,token.content,text_start=starts[start],text_end=starts[end]))
            i+=1
        if not items and source.content.strip():items=[element(rep,source,0,'paragraph',source.content,text_start=0,text_end=len(source.content))]
        tables=sum(e.type=='table' for e in items)
        return DocumentRepresentation(id=rep,source_id=source.source_id,snapshot_id=source.snapshot_id,
            parser_id=self.parser_id,parser_version=self.parser_version,status='ready' if items else 'failed',elements=items,
            coverage=DocumentCoverage(tables_detected=tables,tables_parsed=tables))

    def import_snapshot(self,source):
        """Preserve every old Unicode coordinate; never pretend to reparse the file."""
        snap=source.snapshot;rep=identity();content=source.content;boundaries={0,len(content)}
        for unit in snap.locator_units:boundaries.update((unit.start,unit.end))
        items=[];points=sorted(boundaries)
        for start,end in zip(points,points[1:]):
            unit=next((u for u in snap.locator_units if u.start<=start<u.end),None)
            kind=getattr(unit,'kind',None) or 'paragraph'
            if kind not in {'heading','paragraph','list','table'}:kind='paragraph'
            # The old table text has no trustworthy cell payload. Preserve it as text, not a parsed table.
            if kind=='table':kind='paragraph'
            page=getattr(unit,'page',None)
            items.append(element(rep,source,len(items),kind,content[start:end],page=page,text_start=start,text_end=end))
        total=snap.metadata.get('page_count');pages=sorted({u.page for u in snap.locator_units if u.page and content[u.start:u.end].strip()})
        unreadable=sorted(set(range(1,total+1))-set(pages)) if total else []
        return DocumentRepresentation(id=rep,source_id=source.source_id,snapshot_id=source.snapshot_id,
            parser_id='frozen-snapshot-text',parser_version='1.0',status='partial',elements=items,
            projection_format='verbatim-v1',coverage=DocumentCoverage(total_pages=total,readable_pages=pages,unreadable_pages=unreadable,
                warnings=['historical_text_only']),warnings=['historical_text_only'])


class TranscriptAdapter:
    parser_id='native-transcript'
    parser_version='1.0'
    time=re.compile(r'^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})(?:\s+([^:：]{1,30})[:：])?\s*(.*)$')
    def supports(self,source):
        return source.source_type=='douyin' or bool(re.search('转写|抖音',source.filename)) or bool(re.search(r'^\d{1,2}:\d{2}\s',source.content,re.M))
    def parse(self,source,options):
        if source.source_type=='docx':
            from .native_docx import NativeDocxAdapter
            base=NativeDocxAdapter().parse(source,options)
        else:base=NativeTextAdapter().parse(source,options)
        current=None;speaker=None;items=[]
        for e in base.elements:
            match=self.time.match(e.content.strip())
            if match:
                current=match[1];speaker=match[2] or speaker
            if current and e.type not in {'table','figure'}:
                e=e.model_copy(update={'type':'transcript','locator':e.locator.model_copy(update={'time_start':current,'time_end':current,'speaker':speaker})})
            items.append(e)
        return base.model_copy(update={'parser_id':self.parser_id,'parser_version':self.parser_version,'elements':items})
