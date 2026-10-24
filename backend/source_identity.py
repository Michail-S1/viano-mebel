"""Cross-source furniture identities use exact names or manufacturer module codes.

Unknown attributes are only compatible with a unique complete candidate. Prices,
source filenames and row numbers never identify products. No fuzzy name matching.
"""
import hashlib
import json
import re
from collections import defaultdict
from .colors import derive_color


def text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold().replace("ё", "е")


def words(value):
    return " ".join(re.findall(r"[a-zа-я0-9]+", text(value)))


def strip_finish(name, colour):
    raw=text(name).rstrip(' *');finish=text(colour).rstrip(' *')
    if finish:
        pattern=re.escape(finish).replace(r"\ ",r"\s*").replace('/',r"\s*/\s*")
        raw=re.sub(r"[\s—–,;(]*"+pattern+r"[\s).;]*$","",raw)
    return raw.strip(' —–,;().')


def descriptor(source):
    context=source.get('name_context') or {}
    warehouse=context.get('kind')=='warehouse-characteristic'
    colour=source.get('color') or derive_color(source.get('name',''))
    if str(colour).count(';')>=2:return None
    parent=context.get('parent_name') if warehouse else None
    name=strip_finish(parent or source.get('name',''),colour)
    module=re.match(r'^нкм\s+(.+?)\s+сек(?:ция|ции|ц)?[.\s]*№?\s*(\d+)\b(.*)',text(name)) if text(source.get('factory'))=='росток' else None
    if module:
        identity=('rostok-section',words(module[1]),module[2])
        extra=tuple(re.findall(r'\d+(?:[.,]\d+)?',module[3]))
    else:
        canonical=words(name)
        kind=re.search(r'\b(кровать|комод|пенал|тумба|стеллаж|полка|диван|кресло|стол)\b',canonical)
        if kind:
            canonical=kind[1]+' '+(canonical[:kind.start()]+' '+canonical[kind.end():]).strip()
            canonical=re.sub(r'\s+',' ',canonical)
        identity=('name',canonical);extra=()
        if len(identity[1])<5 or identity[1] in ('шкаф','тумба','комод','пенал','кровать','модуль','стол','стул','полка'):return None
    description=module[3] if module else text(name)
    furniture=re.search(r'\b(кровать|комод|пенал|тумба|стеллаж|полка|диван|кресло|стол|шкаф|антресоль|скамья|сиденье)\b',description)
    furniture=furniture[1] if furniture else ''
    if furniture in ('скамья','сиденье'):furniture='сиденье'
    lift='no' if re.search(r'без\s+(?:подъем|п/?м)',description) else 'yes' if re.search(r'(?:с\s+подъем|подъем[.\s]*мех)',description) else ''
    mirror='no' if re.search(r'без\s+зеркал',description) else 'yes' if re.search(r'с\s+зеркал',description) else ''
    variant=strip_finish(source.get('variant',''),colour)
    # Explicit upholstery/category characteristics must not disappear into an empty variant.
    variant=words(variant)
    component=next((v for v in ('фасад','корпус') if re.search(r'\b'+v+r'\b',text(name))), '')
    if variant==component:variant=''
    dimensions=text(source.get('dimensions',''))
    dimensions=re.sub(r'(?<=\d)\s+(?=\d)','',dimensions)
    dimensions=re.sub(r'\s+','',dimensions).replace('×','x').replace('х','x').replace(',','.')
    return {'bucket':(words(source.get('factory')),)+identity,'warehouse':warehouse,
            'colour':words(colour),'dimensions':dimensions,'sku':text(source.get('sku')),
            'variant':variant,'component':component,'extra':extra,'type':furniture,'lift':lift,'mirror':mirror,'source':source}


def compatible(a,b):
    if a['bucket']!=b['bucket']:return False
    for field in ('colour','dimensions','sku','extra','type','lift','mirror'):
        if a[field] and b[field] and a[field]!=b[field]:return False
    return a['variant']==b['variant'] and a['component']==b['component']


def history(db,identifier):
    row=db.execute('SELECT value FROM metadata WHERE key=?',('duplicate_sources:'+identifier,)).fetchone()
    return json.loads(row[0]) if row else []


def remember(catalog,db,identifier,sources):
    all_sources=history(db,identifier)
    from .importer import product_key
    seen={product_key(s) for s in all_sources}
    for source in sources:
        key=product_key(source)
        if key not in seen:all_sources.append(source);seen.add(key)
    catalog.set_meta(db,'duplicate_sources:'+identifier,all_sources)


class SourceIndex:
    def __init__(self,db,factories=None):
        self.buckets=defaultdict(list)
        query='SELECT id,source_values FROM products';args=[]
        if factories is not None:
            if not factories:return
            query+=' WHERE factory IN ('+','.join('?' for _ in factories)+')';args=list(factories)
        for row in db.execute(query,args):
            current=json.loads(row['source_values']);known=descriptor(current)
            sources=[current]+history(db,row['id'])
            descriptions=[d for source in sources if (d:=descriptor(source))]
            complete={}
            for field in ('colour','dimensions','sku','variant','component','extra','type','lift','mirror'):
                values={d[field] for d in descriptions if d[field]}
                if known and known[field]:complete[field]=known[field]
                elif len(values)==1:complete[field]=next(iter(values))
            for d in descriptions:
                d.update(complete)
                self.buckets[d['bucket']].append((row['id'],d))
    def matches(self,source):
        d=descriptor(source)
        if not d:return set()
        result=set();best=-1
        for identifier,other in self.buckets.get(d['bucket'],[]):
            if not (d['warehouse'] or other['warehouse'] or d['bucket'][1]=='rostok-section'):continue
            if compatible(d,other):
                score=sum(bool(d[f] and other[f]) for f in ('colour','dimensions','sku','extra','type','lift','mirror'))
                if score>best:result={identifier};best=score
                elif score==best:result.add(identifier)
        return result

    def conflicted_targets(self,products):
        targets=defaultdict(list)
        for p in products:
            matches=self.matches(p)
            if len(matches)==1:targets[next(iter(matches))].append(p)
        blocked=set()
        for identifier,items in targets.items():
            descriptors=[descriptor(p) for p in items]
            if len({p.get('base_price') for p in items})>1 or any(
                    len({d[f] for d in descriptors if d and d[f]})>1
                    for f in ('colour','dimensions','sku','variant','component','extra','type','lift','mirror')):
                blocked.add(identifier)
        return blocked


def enriched(existing,incoming):
    result=dict(incoming)
    before,after=descriptor(existing),descriptor(incoming)
    if after and after['warehouse']:
        for field in ('name','collection','variant'):
            result[field]=existing.get(field,'')
    for field in ('dimensions','color','sku'):
        if not result.get(field):result[field]=existing.get(field,'')
    if not result.get('identity_context') and existing.get('identity_context'):
        result['identity_context']=dict(existing['identity_context'])
    return result


def repair_groups(db,excluded=()):
    excluded=set(excluded);buckets=defaultdict(list)
    modes={r['created_at']:r['mode'] for r in db.execute('SELECT created_at,mode FROM imports')}
    for row in db.execute('SELECT rowid AS position,* FROM products ORDER BY rowid'):
        if row['id'] in excluded:continue
        p=dict(row);p['normalized_source']=json.loads(p['source_values']);d=descriptor(p['normalized_source'])
        if d:buckets[d['bucket']].append((p,d))
    safe,review=[],[]
    for bucket,items in buckets.items():
        if len(items)<2:continue
        # A supplier price list is the reference. Pure stock groups remain separate.
        anchors=[(p,d) for p,d in items if not d['warehouse']]
        if not anchors:continue
        assignments=defaultdict(list)
        for p,d in items:
            if not d['warehouse'] and bucket[1]!='rostok-section':continue
            possible=[a for a,ad in anchors if a['id']!=p['id'] and compatible(d,ad)]
            if not d['warehouse'] and bucket[1]=='rostok-section':
                possible=[a for a in possible if a['position']<p['position']]
            if len(possible)==1:assignments[possible[0]['id']].append(p)
            elif len(possible)>1:review.append({'ids':[p['id']]+[a['id'] for a in possible],'reason':'Недостаточно признаков: подходит несколько вариантов'})
        used=set()
        for anchor_id,members in assignments.items():
            anchor=next(p for p,d in anchors if p['id']==anchor_id)
            products=sorted([anchor]+members,key=lambda p:p['position'])
            ids={p['id'] for p in products}
            if used & ids:continue
            descriptions=[descriptor(p['normalized_source']) for p in products]
            if any(not compatible(a,b) for i,a in enumerate(descriptions) for b in descriptions[i+1:]):
                review.append({'ids':list(ids),'reason':'Противоречивые признаки вариантов'});continue
            provenance=defaultdict(list)
            for p in products:provenance[(p['source_file'],p['source_sheet'])].append(p)
            repeated=[ps for ps in provenance.values() if len(ps)>1]
            if any(not all(descriptor(p['normalized_source'])['warehouse'] for p in ps) or bucket[1]!='rostok-section' or len({p['base_price'] for p in ps})>1 for ps in repeated):
                review.append({'ids':list(ids),'reason':'Несколько неоднозначных строк в одном прайсе'});continue
            overrides={};conflict=False
            for p in products:
                for field,value in json.loads(p['overrides']).items():
                    if field in overrides and overrides[field]!=value:conflict=True
                    overrides[field]=value
            if conflict:review.append({'ids':list(ids),'reason':'Противоречивые ручные изменения'});continue
            keeper=products[0]
            updates=[p for p in products if modes.get(p['imported_at'])=='update']
            price_source=max(updates,key=lambda p:(p['imported_at'],p['position'])) if updates else keeper
            source=enriched(anchor['normalized_source'],price_source['normalized_source'])
            for field in ('dimensions','color','sku'):
                if not source.get(field):source[field]=next((p['normalized_source'].get(field) for p in products if p['normalized_source'].get(field)), '')
            key='cross-source-v1:'+hashlib.sha256(json.dumps([bucket]+[descriptions[0][f] for f in ('colour','dimensions','variant','sku','component','extra','type','lift','mirror')],ensure_ascii=False).encode()).hexdigest()
            safe.append({'key':key,'keeper':keeper,'duplicates':products[1:],'source':source,
                         'imported_at':price_source['imported_at'],'overrides':overrides,
                         'original_sources':[p['normalized_source'] for p in products]})
            used.update(ids)
    return {'groups':safe,'review':review}
