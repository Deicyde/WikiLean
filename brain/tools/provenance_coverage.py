"""Standalone exact-byte provenance coverage, separate from semantic reconstruction.

A mapping is an unsigned local review, selected by an independently supplied ID.
This tool neither licenses content nor approves graph changes or production use.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import source_policy_reviews as policy
import provenance_coverage_families as families

contracts = policy.contracts
MAPPING_SCHEMA = 'wikilean.provenance-coverage-mapping/v1'
REPORT_SCHEMA = 'wikilean.provenance-coverage-report/v1'
SCOPE = {'semantic_reconstruction':False,'legal_determination':False,'accepted_authority':False,'production_activation':False,'trust':'expected-id-pinned-local-mapping'}
MAX_LINE = 8 * 1024 * 1024
MAX_JSON = 128 * 1024 * 1024
MAX_OCCURRENCES = 20_000_000
MAX_FAILURES = 200
LIMITS = {'semantic_reconstruction':'not performed; input witnesses cover only the named simple producer checks','content_field_policy':'independently supplied public review only; provenance coverage does not infer field licensing'}
IMPLEMENTATION_PATHS = ('brain/tools/provenance_coverage.py', 'brain/tools/provenance_coverage_families.py',
    'brain/tools/source_policy_reviews.py', 'brain/tools/authority_contracts.py', 'brain/tools/execution_environment.py')
_IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[2]
_MODULES = (sys.modules[__name__], families, policy, contracts, contracts.execution_environment_contract)

class CoverageError(ValueError): pass

def require(value,message):
    if not value: raise CoverageError(message)

def canonical(value): return policy.canonical(value)
def artifact_bytes(value): return contracts.canonical_artifact_json_bytes(value)
def sha(raw): return hashlib.sha256(raw).hexdigest()
def same(a,b): return canonical(a)==canonical(b)
def exact(value,keys,label):
    require(isinstance(value,dict) and set(value)==set(keys),label+' has missing or extra fields');return value

def _capture_implementation():
    result=[]
    for relative,module in zip(IMPLEMENTATION_PATHS,_MODULES,strict=True):
        path=_IMPLEMENTATION_ROOT/relative
        require(Path(module.__file__).resolve()==path and Path(module.__spec__.origin).resolve()==path,
                'coverage implementation module origin differs: '+relative)
        raw=policy.secure_read(path)
        result.append((relative,sha(raw),len(raw)))
    return tuple(result)

_IMPORTED_IMPLEMENTATION = _capture_implementation()

def implementation():
    """Return the immutable import-captured whole local closure; reject drift."""
    require(_capture_implementation()==_IMPORTED_IMPLEMENTATION,'coverage implementation changed after import')
    return _IMPORTED_IMPLEMENTATION

def implementation_root():
    return contracts.domain_hash('provenance-coverage-implementation/v1',
        [{'path':path,'sha256':digest,'bytes':size} for path,digest,size in sorted(implementation())])

def identity(document):
    schema=document.get('schema');require(schema in {MAPPING_SCHEMA,REPORT_SCHEMA},'unknown coverage schema')
    field='mapping_id' if schema==MAPPING_SCHEMA else 'report_id'
    return contracts.domain_hash(schema,{k:v for k,v in document.items() if k!=field})

def pointer(document,value):
    require(isinstance(value,str) and value.startswith('/') and len(value)<=1024,'policy pointer must be a bounded absolute JSON pointer')
    node=document
    for token in value[1:].split('/'):
        token=token.replace('~1','/').replace('~0','~')
        require(isinstance(node,dict) and token in node,'policy pointer is absent from the exact registry')
        node=node[token]
    require(isinstance(node,dict),'policy pointer must identify an entry object')
    return node

def binding_record(pack,release):
    return {'offline_pack_id':pack['offline_pack_id'],'source_set_root':pack['source_set_root'],
        'inventory_id':pack['inventory']['inventory_id'],'reducer_sha256':sha(canonical(pack['reducer'])),
        'release_id':release['release_id'],'release_manifest_sha256':sha(canonical(release)),
        'artifact_root':contracts.domain_hash('provenance-artifacts/v1',release['artifacts'])}

class Inputs:
    def __init__(self,pack,sources,objects,root):
        self.pack,self.sources,self.objects,self.root=pack,sources,objects,Path(root)
        self.groups={b['input_id']:b for b in pack['input_bindings']}
        self.cache={};self.json_cache={};self.row_cache={};self.parents={};self.index_cache={};self.selection_cache={};self.meta_cache={}
        lineage_refs={r['normalization_lineage_id']:r for r in pack['evidence']['normalization_lineages']}
        for source_id,source in sources.items():
            lineage_id=source.get('evidence',{}).get('normalization_lineage_id')
            require(lineage_id is None or lineage_id in lineage_refs,'source lineage is missing from packed evidence')
            lineage=contracts.parse_json_bytes(contracts.verify_file_ref(self.root,lineage_refs[lineage_id],'coverage lineage'),location='coverage lineage') if lineage_id is not None else {}
            self.parents[source_id]=lineage.get('parent_source_manifest_ids',[])
    def ancestors(self,ids):
        found=set();todo=list(ids)
        while todo:
            value=todo.pop();require(value in self.sources,'coverage ancestor absent from pack')
            if value not in found: found.add(value);todo.extend(self.parents[value])
        return found
    def members(self,group,prov,context):
        require(group in self.groups,'mapping requires undeclared input group '+group)
        result=self.groups[group]['members']
        if group in {'external-pages','external-links'}:
            suffix='_pages.jsonl' if group=='external-pages' else '_links.jsonl'
            result=[m for m in result if Path(m['path']).name==prov['source']+suffix]
        spec=families.classify(prov)
        if group=='user-repos' and spec is not None and group in spec['primary'] and 'pin' in prov:
            result=[m for m in result if self.sources[m['source_manifest_id']]['pin']['value']==prov['pin']]
            method=prov['method']
            if method.startswith('file-tree (user_repos/'):
                filename=method[len('file-tree (user_repos/'):-1]
                require(Path(filename).name==filename,'user-repository provenance filename is malformed')
                result=[m for m in result if Path(m['path']).name==filename]
            if method.startswith('user-repo reference URL ('):
                repo=method[len('user-repo reference URL ('):-1]
                result=[m for m in result if self.metadata(m).get('repo')==repo]
            candidates=[context.get(k) for k in ('src','dst','id')]
            decl=next((v for v in candidates if isinstance(v,str) and v.startswith('decl:')),None)
            path=next((v for v in candidates if isinstance(v,str) and v.startswith('path:')),None)
            lib=decl.split(':',2)[1] if decl else path.removeprefix('path:').split('/',1)[0] if path else None
            if lib is not None:result=[m for m in result if self.metadata(m).get('lib')==lib]
        if group=='annotations' and context.get('kind')=='article':
            slug=context.get('id')
            result=[m for m in result if self.read_json(m).get('slug')==slug]
        return result
    def read(self,member):
        key=(member['source_manifest_id'],member['object'])
        obj=self.objects[key]
        require(obj['bytes']<=MAX_JSON,'witness input exceeds bounded parser size')
        if key not in self.cache:self.cache[key]=contracts.verify_file_ref(self.root,obj,'coverage witness input')
        return self.cache[key]
    def read_json(self,member):
        key=(member['source_manifest_id'],member['object'])
        if key not in self.json_cache:self.json_cache[key]=contracts.parse_artifact_json_bytes(self.read(member),location='coverage witness')
        return self.json_cache[key]
    def rows(self,member):
        key=(member['source_manifest_id'],member['object'])
        if key not in self.row_cache:
            rows=[]
            for line in self.read(member).splitlines():
                if line.strip():
                    require(len(line)<=MAX_LINE,'oversize witness line')
                    row=contracts.parse_artifact_json_bytes(line,location='coverage witness row')
                    require(isinstance(row,dict),'witness row is not an object')
                    if '_meta' not in row:rows.append(row)
            self.row_cache[key]=tuple(rows)
        return iter(self.row_cache[key])
    def metadata(self,member):
        key=(member['source_manifest_id'],member['object'])
        if key not in self.meta_cache:
            raw=self.read(member).split(b'\n',1)[0];require(len(raw)<=MAX_LINE,'oversize source metadata')
            row=contracts.parse_artifact_json_bytes(raw,location='coverage source metadata')
            require(isinstance(row,dict) and isinstance(row.get('_meta'),dict),'source has no first-row metadata')
            self.meta_cache[key]=row['_meta']
        return self.meta_cache[key]
    def snapshots(self):
        return [{**copy.deepcopy(group),'objects':[{'source_manifest_id':m['source_manifest_id'],'object':m['object'],
            'path':m['path'],'pin':copy.deepcopy(self.sources[m['source_manifest_id']]['pin']),
            **{k:self.objects[(m['source_manifest_id'],m['object'])][k] for k in ('sha256','bytes','media_type','roles')}} for m in group['members']]}
            for group in self.pack['input_bindings']]
    def indexed(self,member,fields,values):
        key=(member['source_manifest_id'],member['object'],tuple(fields))
        if key not in self.index_cache:
            index={}
            for row in self.rows(member):index.setdefault(artifact_bytes([row.get(f) for f in fields]),[]).append(row)
            self.index_cache[key]=index
        return self.index_cache[key].get(artifact_bytes(list(values)),())
    def selection(self,group,members):
        # The complete member preimage is already mapping-bound. Cache the
        # selection root once, so a source-tree join is not serialized per edge.
        key=(group,None if members is self.groups[group]['members'] else tuple((m['path'],m['source_manifest_id'],m['object']) for m in members))
        if key not in self.selection_cache:
            self.selection_cache[key]={'state':self.groups[group]['state'],'count':len(members),
                'member_root':contracts.domain_hash('provenance-member-selection/v1',members)}
        return self.selection_cache[key]


def artifact_documents(root,artifact):
    """Stream JSONL; other JSON is size bounded. Opaque bytes are verified elsewhere."""
    path=artifact['path']
    if artifact['logical_format']=='opaque':return
    with contracts.open_verified_file(root,artifact,'coverage artifact') as handle:
        if artifact['logical_format']=='jsonl-rowset':
            number=0
            while raw:=handle.readline(MAX_LINE+1):
                number+=1;require(len(raw)<=MAX_LINE,'oversize JSONL artifact row')
                if raw.strip():yield str(number),contracts.parse_artifact_json_bytes(raw,location=path+':'+str(number))
        else:
            require(artifact['bytes']<=MAX_JSON,'oversize JSON artifact')
            yield '$',contracts.parse_artifact_json_bytes(handle.read(MAX_JSON+1),location=path)


def occurrences(release,root):
    """Enumerate direct provenance, every declared pool, and every pool reference."""
    manifest_ref=next(a for a in release['artifacts'] if a['path']=='site/assets/brain/cells/manifest.json')
    manifest=next(artifact_documents(root,manifest_ref))[1]
    default_pool=manifest.get('prov',[])
    trace_pool=manifest.get('traces',{}).get('prov',default_pool)
    def pool(value,where):
        require(isinstance(value,list) and all(isinstance(p,dict) for p in value),'malformed provenance pool at '+where)
        return value
    pool(default_pool,'cell manifest');pool(trace_pool,'trace manifest')
    for artifact in release['artifacts']:
        path=artifact['path'];active=[]
        if path.startswith('site/assets/brain/cells/traces/'):active=trace_pool
        elif path.startswith('site/assets/brain/cells/'):active=default_pool
        def walk(value,where,ctx,current_pool):
            if isinstance(value,list):
                for i,item in enumerate(value):yield from walk(item,where+'/'+str(i),ctx,current_pool)
                return
            if not isinstance(value,dict):return
            context=dict(ctx)
            if isinstance(value.get('organs'),list):
                context['concepts']=[o['id'] for o in value['organs'] if isinstance(o,dict) and o.get('kind')=='concept' and isinstance(o.get('id'),str)]
            if isinstance(value.get('cell'),dict):context.update({k:value['cell'][k] for k in ('anchor',) if k in value['cell']})
            for k in ('src','dst','kind','id','anchor','evidence'):
                if k in value:context[k]=value[k]
            for key,item in value.items():
                here=where+'/'+key
                if key=='provenance':
                    require(isinstance(item,dict),'malformed direct provenance at '+here)
                    yield {'artifact':path,'location':here,'type':'direct','provenance':item,'context':context}
                elif key=='prov':
                    if isinstance(item,list):
                        allowed=(path in {'brain/data/cells.jsonl','brain/data/synapses.jsonl'} and here=='1/_meta/prov') or (path=='site/assets/brain/cells/manifest.json' and here in {'$/prov','$/traces/prov'})
                        require(allowed,'provenance pool declared outside a supported table location at '+here)
                        table=pool(item,here)
                        for index,p in enumerate(table):yield {'artifact':path,'location':here+'/'+str(index),'type':'pool','provenance':p,'context':{}}
                    else:
                        require(type(item) is int and 0<=item<len(current_pool),'invalid provenance reference at '+here)
                        yield {'artifact':path,'location':here,'type':'reference','provenance':current_pool[item],'context':context}
                else:yield from walk(item,here,context,current_pool)
        for row_number,document in artifact_documents(root,artifact):
            require(isinstance(document,(dict,list)),'artifact JSON root is not a collection')
            if isinstance(document,dict) and '_meta' in document:
                metadata=document['_meta'];require(isinstance(metadata,dict),'malformed artifact metadata')
                if 'prov' in metadata:active=pool(metadata['prov'],path+':metadata')
            if path in {'brain/data/edges.jsonl','brain/data/edges_links.jsonl'} and isinstance(document,dict) and '_meta' not in document:
                require(isinstance(document.get('provenance'),dict),'base edge lacks provenance at '+path+':'+row_number)
            yield from walk(document,row_number,{},active)


def match_key(prov):
    require(isinstance(prov,dict),'provenance must be an object')
    fields={'source','method'} | ({'queue'} if prov.get('source')=='tag-queue' else set())
    require(set(prov) in (fields,fields|{'pin'}),'unknown or missing provenance fields')
    require(all(isinstance(prov[k],str) and prov[k] and len(prov[k])<=2048 for k in fields),'invalid provenance vocabulary')
    if 'pin' in prov:require(isinstance(prov['pin'],str) and 0<len(prov['pin'])<=2048,'invalid provenance pin')
    return {k:prov[k] for k in sorted(fields)}


def expected_groups(spec,inputs):
    # Optional dependencies are explicit pack state, not silently dropped from a
    # reviewed mapping. Missing declarations fail; absence remains a sealed fact.
    primary=list(spec['primary']);support=sorted(set(spec['support'])-set(primary))
    for group in primary+support:require(group in inputs.groups,'producer support group is not declared: '+group)
    return primary,support


def rule_sources(rule,inputs):
    ids=set()
    for group in rule['primary_inputs']+rule['supporting_inputs']:
        ids.update(inputs.groups[group]['source_manifest_ids'])
    return sorted(inputs.ancestors(ids))


def validate_mapping(mapping,pack,release,inputs,expected_id):
    exact(mapping,{'schema','mapping_id','scope','implementation_root','binding','state','reviewer','input_bindings','registry','rules'},'coverage mapping')
    require(mapping['schema']==MAPPING_SCHEMA,'wrong mapping schema');contracts._hash(expected_id,'expected coverage mapping ID')
    require(mapping['mapping_id']==expected_id==identity(mapping),'coverage mapping differs from independently expected ID')
    require(same(mapping['scope'],SCOPE),'mapping exceeds standalone scope')
    require(mapping['implementation_root']==implementation_root(),'mapping coverage implementation differs')
    require(same(mapping['binding'],binding_record(pack,release)),'mapping pack/release/producer binding differs')
    require(same(mapping['input_bindings'],inputs.snapshots()),'mapping input member closure differs')
    require(mapping['state'] in {'pending','reviewed'},'unknown mapping state')
    if mapping['state']=='reviewed':
        exact(mapping['reviewer'],{'name','reviewed_at'},'mapping reviewer')
        policy.text(mapping['reviewer']['name'],'mapping reviewer',maximum=256)
        contracts._expect_pattern(mapping['reviewer']['reviewed_at'],'mapping time',contracts.UTC_TIMESTAMP_RE,'UTC time')
    else:require(mapping['reviewer'] is None,'pending mapping must have no reviewer claim')
    registry_ref=mapping['registry'];exact(registry_ref,{'source_manifest_id','object','sha256','bytes'},'mapping registry')
    require('source-registry' in inputs.groups,'coverage requires a source-registry group')
    candidates=inputs.groups['source-registry']['members'];require(len(candidates)==1,'coverage registry must be exactly one bound member')
    member=candidates[0];obj=inputs.objects[(member['source_manifest_id'],member['object'])]
    require(same(registry_ref,{'source_manifest_id':member['source_manifest_id'],'object':member['object'],'sha256':obj['sha256'],'bytes':obj['bytes']}),'mapping registry identity differs')
    registry=inputs.read_json(member)
    release_registry=next((a for a in release['artifacts'] if a['path']=='catalog/data/source_registry.json'),None)
    require(release_registry is not None and release_registry['sha256']==obj['sha256'] and release_registry['bytes']==obj['bytes'],
            'release registry bytes differ from the exact bound registry')
    require(isinstance(mapping['rules'],list) and len(mapping['rules'])<=4096,'invalid rule count')
    ids=[];matches=[]
    for rule in mapping['rules']:
        exact(rule,{'id','family','match','primary_inputs','supporting_inputs','policy'},'coverage rule')
        policy.text(rule['id'],'rule ID',maximum=128);ids.append(rule['id'])
        match=match_key(rule['match']);require(same(match,rule['match']),'rule match cannot contain pin')
        spec=families.classify(match);require(spec is not None and spec['family']==rule['family'],'unknown or mismatched producer family')
        primary,support=expected_groups(spec,inputs)
        require(same(rule['primary_inputs'],primary) and same(rule['supporting_inputs'],support),'rule omits or adds producer input/support groups')
        matches.append(canonical(match))
        decision=rule['policy'];exact(decision,{'basis','registry_entries','supplement','source_manifest_ids','evidence_ids'},'coverage policy')
        policy.text(decision['basis'],'mapping policy basis',empty=True)
        policy.strings(decision['registry_entries'],'registry entry pointers')
        for ptr in decision['registry_entries']:pointer(registry,ptr)
        require(same(decision['source_manifest_ids'],rule_sources(rule,inputs)),'policy omits exact source/member ancestry')
        policy.strings(decision['evidence_ids'],'mapping policy evidence IDs')
        supplement=decision['supplement']
        if supplement is not None:
            exact(supplement,{'source','description'},'supplemental policy entry')
            require(supplement['source']==match['source'],'supplemental policy label differs')
            policy.text(supplement['description'],'supplement description')
        if match['source'] in {'tag-queue','wikilean'}:
            require(supplement is not None,'synthetic provenance label requires an explicit supplemental policy entry')
    require(ids==sorted(set(ids)),'mapping rule IDs must be unique and sorted')
    require(len(matches)==len(set(matches)),'ambiguous coverage rules for one provenance method/context')
    return registry


def draft_mapping(pack_path,release_path):
    pack,sources,objects=policy.verified_pack(pack_path);release=policy.verified_release(release_path,pack)
    inputs=Inputs(pack,sources,objects,Path(pack_path).parent)
    require('source-registry' in inputs.groups and len(inputs.groups['source-registry']['members'])==1,'coverage draft requires one source-registry input')
    member=inputs.groups['source-registry']['members'][0];obj=objects[(member['source_manifest_id'],member['object'])]
    matches={}
    for occurrence in occurrences(release,Path(release_path).parent):
        match=match_key(occurrence['provenance']);matches[canonical(match)]=match
    rules=[]
    for encoded,match in sorted(matches.items()):
        spec=families.classify(match);require(spec is not None,'unrecognized emitted provenance cannot be drafted: '+str(match))
        primary,support=expected_groups(spec,inputs)
        rule={'id':spec['family']+'-'+sha(encoded)[:16],'family':spec['family'],'match':match,
            'primary_inputs':primary,'supporting_inputs':support,
            'policy':{'basis':'','registry_entries':[],'supplement':None,'source_manifest_ids':[],'evidence_ids':[]}}
        rule['policy']['source_manifest_ids']=rule_sources(rule,inputs)
        if match['source'] in {'tag-queue','wikilean'}:
            rule['policy']['supplement']={'source':match['source'],'description':'PENDING: review the exact synthetic producer and distinguish its source claims.'}
        rules.append(rule)
    result={'schema':MAPPING_SCHEMA,'mapping_id':'','scope':copy.deepcopy(SCOPE),'implementation_root':implementation_root(),'binding':binding_record(pack,release),
        'state':'pending','reviewer':None,'input_bindings':inputs.snapshots(),
        'registry':{'source_manifest_id':member['source_manifest_id'],'object':member['object'],'sha256':obj['sha256'],'bytes':obj['bytes']},
        'rules':sorted(rules,key=lambda rule:rule['id'])}
    result['mapping_id']=identity(result);return result


def witness(spec,prov,context,selected,inputs,occurrence_type):
    """Check simple retained input witnesses; do not claim general reconstruction."""
    kind=spec['witness']
    if kind is None:return 'not-reconstructed'
    if occurrence_type=='pool':return 'declaration-only'
    primary=selected[spec['primary'][0]]
    src,dst=context.get('src'),context.get('dst')
    evidence=context.get('evidence') or {}
    require(isinstance(evidence,dict),'malformed claim evidence')
    if kind=='article':
        if context.get('kind')!='article':return 'declaration-only'
        require(len(primary)==1 and inputs.read_json(primary[0]).get('annotations'),'article claim has no exact annotated D1 member')
        return 'checked'
    if context.get('kind') in {'co-page','co-statement'}:
        # The cell projector deliberately discards the original claim endpoint.
        # Context was checked separately; do not pretend to reconstruct that join.
        return 'partial-witness'
    if kind=='queue':
        pairs=[];queue=prov['queue']
        for member in primary:
            doc=inputs.read_json(member);rows=doc.get('items',[]) if isinstance(doc,dict) else doc
            require(isinstance(rows,list) and all(isinstance(r,dict) for r in rows),'invalid queue witness shape')
            for row in rows:
                triage=row.get('triage') or {}
                if queue=='recycle_queue':
                    qid=triage.get('suggested_qid') or row.get('qid');decl=triage.get('suggested_decl') or row.get('decl') or row.get('current_decl');status='recycled'
                else:
                    qid,decl=row.get('qid'),row.get('decl');status='brain' if queue=='brain_queue' else row.get('status','unreviewed')
                if qid and decl:pairs.append((qid,decl,status))
        status=prov['method'][len('AI-queued @[wikidata] candidate ('):-1]
        candidates=set(context.get('concepts',[]))
        if isinstance(context.get('anchor'),str):candidates.add(context['anchor'])
        if isinstance(src,str) and src.startswith('Q'):candidates.add(src)
        target=dst or (context.get('id') if context.get('kind')=='decl' else None)
        bare=target.split(':',2)[-1] if isinstance(target,str) and target.startswith('decl:') else None
        require(any(s==status and (bare is None or d==bare) and (not candidates or q in candidates) for q,d,s in pairs),'queued claim/status has no retained producer witness')
        return 'checked' if bare is not None and candidates else 'partial-witness'
    if not isinstance(src,str) or not isinstance(dst,str):return 'partial-witness'
    def indexed(fields,values):return (r for m in primary for r in inputs.indexed(m,fields,values))
    rows=()
    if kind=='tag':
        if prov['source']=='mathlib':
            found=any(indexed(('db','tag','decl'),('wikidata',src,dst.split(':',2)[-1])))
        else:
            rows=indexed(('db','decl'),(prov['source'],src.split(':',2)[-1]))
            found=any(r.get('db')==prov['source'] and r.get('decl')==src.split(':',2)[-1] and str(r.get('tag'))==dst.split(':',2)[-1] for r in rows)
    elif kind=='wikidata':
        properties=evidence.get('properties');require(isinstance(properties,list) and properties,'relation lacks property witnesses')
        require(all(isinstance(p,dict) and isinstance(p.get('p'),str) and isinstance(p.get('label'),str) for p in properties),'malformed property witness')
        rows=indexed(('s','o'),(src,dst))
        expected={(p['p'],p['label']) for p in properties}
        found=bool(expected) and expected <= {(r.get('p'),r.get('p_label')) for r in rows if r.get('s')==src and r.get('o')==dst}
    elif kind=='citation':found=any(indexed(('src','dst'),(src.removeprefix('lit:'),dst.removeprefix('lit:'))))
    elif kind in {'link','projected-link'}:
        a,b=(evidence.get('src_page'),evidence.get('dst_page')) if kind=='projected-link' else (src.split(':',2)[-1],dst.split(':',2)[-1])
        require(isinstance(a,str) and isinstance(b,str),'missing external link page witnesses')
        found=any(indexed(('src','dst'),(a,b)))
    elif kind=='page':found=any(indexed(('qid','id'),(src,dst.split(':',2)[-1])))
    elif kind=='container':
        rows=indexed(('qid',),(src,))
        found=any(r.get('qid')==src and 'path:'+str(r.get('path','')).removeprefix('path:').replace('.','/')==dst and r.get('skeptic')=='accept' for r in rows)
    elif kind=='discovery':
        rows=indexed(('src',),(src,))
        # A discovered declaration can be remapped into its existing library.
        found=any(r.get('src')==src and (r.get('dst')==dst or isinstance(r.get('dst'),str) and r['dst'].startswith('decl:') and dst.startswith('decl:') and r['dst'].split(':',2)[-1]==dst.split(':',2)[-1]) and r.get('verified') is True and not r.get('rejected_reason') and r.get('kind')==context.get('kind') for r in rows)
    elif kind=='fc':found=any(indexed(('qid','decl','kind'),(src,dst.split(':',2)[-1],context.get('kind'))))
    else:raise CoverageError('unsupported witness mode')
    require(found,'claim has no matching retained '+kind+' witness');return 'checked'


def validate_context(spec,prov,context,occurrence_type):
    if occurrence_type=='pool':return
    family=spec['family'];kind=context.get('kind')
    require('evidence' not in context or isinstance(context['evidence'],dict),'malformed claim evidence')
    if kind in {'contains','formalizes','mentions','depends','relates','xref','cites','matches','invocation','links'}:
        require(not spec['kinds'] or kind in spec['kinds'],'method contradicts emitted edge kind')
    if family=='article-organ':require(kind=='article','D1 article provenance attached to a non-article organ')
    if family=='tag-queue':require(kind in {'concept','decl','formalizes'},'queue provenance attached to an unrelated organ/trace')
    src,dst=context.get('src'),context.get('dst')
    if src is None and dst is None:
        organ_families={
            'article-organ':{'article'},'tag-queue':{'concept','decl'},
            'mathlib-gold':{'concept','decl'},'agent-grounding':{'concept','decl'},
            'container-fold':{'concept'},'discovery-fold':{'concept','decl'},
            'formal-conjectures-fold':{'concept','decl'},
            'frontier-library':{'concept','decl'},
            'wikidata-external-ids':{'page'},'mathlib-external-tags':{'page'},
            'external-page-qid':{'page'},'external-verified-anchor':{'page'},
            'frontier-reference-xrefs':{'page'},'erdos-oeis-join':{'page'},
            'theoremgraph-derived-links':{'statement'},'theoremgraph-matching':{'statement'},
        }
        require(occurrence_type=='reference' and kind in organ_families.get(family,set()),'method has no supported collapsed-organ context')
        return
    require(isinstance(src,str) and isinstance(dst,str),'provenance edge endpoints are incomplete')
    q=lambda s:bool(re.fullmatch(r'Q[1-9][0-9]*',s))
    d=lambda s:s.startswith('decl:') and len(s.split(':',2))==3 and bool(s.split(':',2)[-1])
    p=lambda s:s.startswith('path:') and len(s)>5
    l=lambda s:s.startswith('lit:') and len(s)>4
    x=lambda s:s.startswith('xref:'+prov['source']+':') and len(s)>len(prov['source'])+6
    if kind=='co-page':
        require(occurrence_type=='reference' and family in {'wikidata-external-ids','mathlib-external-tags','external-page-qid','external-verified-anchor','frontier-reference-xrefs','erdos-oeis-join'}
                and src==dst and x(src) and context.get('evidence',{}).get('page')==src and context.get('evidence',{}).get('db')==prov['source'],
                'invalid shared-page provenance context')
        return
    if kind=='co-statement':
        require(occurrence_type=='reference' and family in {'theoremgraph-derived-links','theoremgraph-matching'} and src==dst and l(src)
                and context.get('evidence',{}).get('statement')==src,'invalid shared-statement provenance context')
        return
    require(kind in {'contains','formalizes','mentions','depends','relates','xref','cites','matches','invocation','links'},
            'unknown emitted edge kind')
    valid=True
    if family in {'mathlib-gold','agent-grounding','annotation-citation','formal-conjectures-fold','tauceti-fold'}:valid=q(src) and d(dst)
    elif family=='container-fold':valid=q(src) and p(dst)
    elif family=='discovery-fold':valid=q(src) and (d(dst) or p(dst))
    elif family in {'formal-dependency-lift','wikidata-relations','external-projected-link'}:valid=q(src) and q(dst)
    elif family in {'wikidata-external-ids','external-page-qid','external-verified-anchor'}:valid=q(src) and x(dst)
    elif family=='mathlib-external-tags':valid=d(src) and x(dst)
    elif family=='hierarchy-containment':valid=p(src) and (p(dst) or d(dst))
    elif family in {'theoremgraph-derived-links','theoremgraph-matching'}:valid=(q(src) if kind=='cites' else d(src)) and l(dst)
    elif family in {'literature-containment','openalex-citations'}:valid=l(src) and l(dst)
    elif family=='external-internal-link':valid=x(src) and x(dst)
    elif family in {'frontier-reference-xrefs','erdos-oeis-join'}:valid=d(src) and x(dst)
    elif family=='frontier-library':
        valid=(p(src) and (p(dst) or d(dst))) if kind=='contains' else d(src) and d(dst) if kind=='invocation' else q(src) and d(dst)
    require(valid,'source/method contradicts emitted endpoint context')
    library={'formal_conjectures':'FormalConjectures','tauceti':'TauCeti'}.get(prov['source'])
    if family in {'frontier-reference-xrefs','erdos-oeis-join'}:
        library={'formal-conjectures':'FormalConjectures','tauceti':'TauCeti'}.get(spec['primary'][0])
    if library:
        target=src if kind in {'contains','invocation','xref'} else dst
        require(target.startswith('decl:'+library+':') or target=='path:'+library or target.startswith('path:'+library+'/'),
                'frontier provenance differs from its declared library')
    if family=='external-projected-link':require(context.get('evidence',{}).get('via')==prov['source'] and context.get('evidence',{}).get('projected') is True,'projected link lacks its explicit database join witness')


def check(mapping,pack_path,release_path,private_review,*,expected_mapping_id,expected_private_id,
          private_attachment_root=None,public_review=None,expected_public_id=None,public_attachment_root=None):
    """Reverify inputs and return a bounded exact report. No caller receipt is trusted."""
    implementation()
    pack,sources,objects=policy.verified_pack(pack_path);release=policy.verified_release(release_path,pack)
    inputs=Inputs(pack,sources,objects,Path(pack_path).parent)
    validate_mapping(mapping,pack,release,inputs,expected_mapping_id)
    private_result=policy.validate_private(private_review,pack_path,expected_id=expected_private_id,attachment_root=private_attachment_root)
    if public_review is None:
        require(expected_public_id is None,'expected public ID requires its review document');public_result=None
    else:
        require(expected_public_id is not None,'public review requires independently expected ID')
        public_result=policy.validate_public(public_review,pack_path,release_path,private_review,
            expected_id=expected_public_id,expected_private_id=expected_private_id,attachment_root=public_attachment_root,private_attachment_root=private_attachment_root)
    evidence_ids={e['evidence_id'] for e in private_review['evidence']}
    approved={r['source_manifest_id'] for r in private_review['sources'] if r['decision']=='approved'}
    rules={canonical(r['match']):r for r in mapping['rules']}
    failure_count=0;failures=[];counts=Counter();artifacts=Counter();types=Counter();witness_counts=Counter();root=hashlib.sha256();resolved_root=hashlib.sha256();used=set();number=0
    def fail(code,location,detail):
        nonlocal failure_count
        failure_count+=1
        if len(failures)<MAX_FAILURES:failures.append({'code':code,'location':location,'detail':detail[:2048]})
    if mapping['state']!='reviewed':fail('mapping-pending','$','The independently pinned mapping has not been reviewed.')
    if not private_result['private_replay_policy_ready']:fail('private-policy-not-ready','$','The exact private policy review is pending or rejected.')
    if public_result is not None and not public_result['public_release_policy_ready']:fail('public-policy-not-ready','$','The supplied public policy review is pending or rejected.')
    for rule in mapping['rules']:
        decision=rule['policy']
        if not decision['basis'].strip() or not decision['evidence_ids'] or not (decision['registry_entries'] or decision['supplement']):fail('incomplete-policy',rule['id'],'Explicit entry, basis and retained review evidence are required.')
        if not set(decision['evidence_ids'])<=evidence_ids:fail('unknown-policy-evidence',rule['id'],'Mapping policy evidence is absent from the independently pinned private review.')
        if not set(decision['source_manifest_ids'])<=approved:fail('unreviewed-policy-source',rule['id'],'A primary or supporting source ancestor lacks an approved scoped decision.')
    try:
        for occurrence in occurrences(release,Path(release_path).parent):
            require(number<MAX_OCCURRENCES,'occurrence count exceeds coverage bound');number+=1
            root.update(artifact_bytes(occurrence)+b'\n');artifacts[occurrence['artifact']]+=1;types[occurrence['type']]+=1
            location=occurrence['artifact']+':'+occurrence['location']
            try:
                prov=occurrence['provenance'];match=match_key(prov);spec=families.classify(match)
                require(spec is not None,'unknown emitted source/method family')
                key=canonical(match);require(key in rules,'emitted provenance has no exact reviewed rule')
                rule=rules[key];used.add(rule['id']);context=occurrence['context']
                validate_context(spec,prov,context,occurrence['type'])
                primary,support=expected_groups(spec,inputs);selected={g:inputs.members(g,prov,context) for g in primary+support}
                if 'user-repos' in primary:require(len(selected['user-repos'])==1,'user-repository provenance is absent or ambiguous')
                if occurrence['type']!='pool':
                    for group in primary:require(selected[group],'claim names an absent or unmatched primary input: '+group)
                # A rule over every declared group binds explicit optional absence.
                # The first primary is the historical printed pin, even for the
                # two-input FC/Erdos join; secondary evidence is retained below.
                if 'pin' in prov:
                    require(prov['pin'] in {sources[m['source_manifest_id']]['pin']['value'] for m in selected[primary[0]]},'printed provenance pin differs from its designated primary member')
                elif spec['family'] not in {'tag-queue','article-organ'}:raise CoverageError('base producer provenance has no immutable pin')
                resolved={'rule':rule['id'],'location':location,'groups':{g:inputs.selection(g,selected[g]) for g in sorted(selected)}}
                resolved_root.update(canonical(resolved)+b'\n')
                status=witness(spec,prov,context,selected,inputs,occurrence['type']);witness_counts[status]+=1;counts[rule['family']]+=1
            except (CoverageError,policy.PolicyReviewError,contracts.VerificationError,KeyError,TypeError,ValueError) as exc:
                fail('uncovered-provenance',location,str(exc))
    except (CoverageError,policy.PolicyReviewError,contracts.VerificationError,KeyError,TypeError,ValueError,OSError) as exc:
        fail('enumeration-failed','$',str(exc))
    # Recheck actual artifact and pack bytes after all witness reads. The report
    # does not describe a mixture of generations if a file changed mid-scan.
    again=policy.verified_release(release_path,pack)
    require(same(release,again),'release changed during coverage')
    again_pack,_,_=policy.verified_pack(pack_path);require(same(pack,again_pack),'pack changed during coverage')
    report={'schema':REPORT_SCHEMA,'report_id':'','scope':copy.deepcopy(SCOPE),'implementation_root':implementation_root(),
        'mapping_id':expected_mapping_id,'private_review_id':expected_private_id,'public_review_id':expected_public_id,
        'binding':binding_record(pack,release),'provenance_coverage_ready':failure_count==0,
        'occurrences':{'total':number,'by_type':dict(sorted(types.items())),'by_artifact':dict(sorted(artifacts.items())),'covered_by_family':dict(sorted(counts.items())),'root':'sha256:'+root.hexdigest()},
        'resolved_input_root':'sha256:'+resolved_root.hexdigest(),'witnesses':dict(sorted(witness_counts.items())),
        'used_rule_ids':sorted(used),'unused_rule_ids':sorted(set(r['id'] for r in mapping['rules'])-used),
        'failures':{'total':failure_count,'truncated':failure_count>len(failures),'items':failures},
        'limits':copy.deepcopy(LIMITS)}
    report['report_id']=identity(report);validate_report(report);implementation();return report


def validate_report(report):
    exact(report,{'schema','report_id','scope','implementation_root','mapping_id','private_review_id','public_review_id','binding','provenance_coverage_ready','occurrences','resolved_input_root','witnesses','used_rule_ids','unused_rule_ids','failures','limits'},'coverage report')
    require(report['schema']==REPORT_SCHEMA and report['report_id']==identity(report),'coverage report identity differs')
    require(same(report['scope'],SCOPE),'coverage report scope differs')
    for key in ('implementation_root','mapping_id','private_review_id','resolved_input_root'):contracts._hash(report[key],key)
    if report['public_review_id'] is not None:contracts._hash(report['public_review_id'],'public review ID')
    exact(report['binding'],{'offline_pack_id','source_set_root','inventory_id','reducer_sha256','release_id','release_manifest_sha256','artifact_root'},'coverage report binding')
    for key,value in report['binding'].items():
        (contracts._digest if key.endswith('sha256') else contracts._hash)(value,key)
    require(type(report['provenance_coverage_ready']) is bool,'coverage readiness must be boolean')
    data=report['occurrences'];exact(data,{'total','by_type','by_artifact','covered_by_family','root'},'occurrence summary');contracts._hash(data['root'],'occurrence root')
    contracts._expect_int(data['total'],'occurrence total')
    for field in ('by_type','by_artifact','covered_by_family'):
        require(isinstance(data[field],dict),'invalid occurrence counters')
        for value in data[field].values():contracts._expect_int(value,'occurrence count')
    require(set(data['by_type'])<={'direct','pool','reference'},'unknown occurrence type')
    require(set(data['covered_by_family'])<=families.FAMILIES,'unknown covered producer family')
    require(data['total']<=MAX_OCCURRENCES,'report exceeds occurrence limit')
    require(sum(data['by_type'].values())==sum(data['by_artifact'].values())==data['total'],'occurrence count sums differ')
    require(isinstance(report['witnesses'],dict),'invalid witness counters')
    for key,value in report['witnesses'].items():
        require(key in {'not-reconstructed','declaration-only','checked','partial-witness'},'unknown witness status');contracts._expect_int(value,'witness count')
    require(sum(report['witnesses'].values())==sum(data['covered_by_family'].values())<=data['total'],'covered/witness counts differ')
    if report['provenance_coverage_ready']:require(sum(report['witnesses'].values())==data['total'],'ready report omits occurrences')
    for key in ('used_rule_ids','unused_rule_ids'):policy.strings(report[key],key)
    require(not set(report['used_rule_ids'])&set(report['unused_rule_ids']),'used/unused rules overlap')
    failures=report['failures'];exact(failures,{'total','truncated','items'},'coverage failures');contracts._expect_int(failures['total'],'failure count')
    require(isinstance(failures['items'],list) and len(failures['items'])<=MAX_FAILURES,'invalid failures list')
    require(type(failures['truncated']) is bool and failures['truncated']==(failures['total']>len(failures['items'])) and failures['total']>=len(failures['items']),'failure truncation differs')
    for failure in failures['items']:
        exact(failure,{'code','location','detail'},'failure')
        for value in failure.values():policy.text(value,'failure text',maximum=4096)
    require(report['provenance_coverage_ready']==(failures['total']==0),'coverage readiness contradicts failures')
    require(same(report['limits'],LIMITS),'coverage report limits differ')
    return report
