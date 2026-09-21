"""Reviewed emission vocabulary; graph labels are not source-manifest names.

This is a structural provenance map, not reconstruction of every graph operation.
The outer mapping pins the exact producer and every concrete input generation.
"""
from __future__ import annotations

DBS = frozenset({'lmfdb_knowl','nlab','mathworld','proofwiki','eom','planetmath','oeis','metamath','dlmf','msc','stacks','kerodon','erdos','kgmid'})
XREF_DBS = DBS - {'stacks','kerodon','erdos','kgmid'}
REPOS = {'formal_conjectures':'formal-conjectures','tauceti':'tauceti','user_lean_repos':'user-repos'}
QUEUES = {'seed_queue':'bot-seed-queue','recycle_queue':'bot-recycle-queue','brain_queue':'bot-brain-queue','pool_candidates':'bot-pool-candidates'}
UNIVERSE = ('concept-graph','wikidata-universe','universe-extension')
MODULES = ('concept-graph','theoremgraph-links','theorem-matching','statement-formal','mathlib-tag-xrefs','declaration-oracle','mathlib-ilean-tree','decl-renames','brain-discovery-proposals')
FAMILIES = frozenset({'mathlib-gold','agent-grounding','container-fold','discovery-fold','annotation-citation','formal-dependency-lift','wikidata-relations','wikidata-external-ids','mathlib-external-tags','hierarchy-containment','theoremgraph-derived-links','theoremgraph-matching','literature-containment','openalex-citations','external-page-qid','external-verified-anchor','external-internal-link','external-projected-link','frontier-library','frontier-reference-xrefs','erdos-oeis-join','formal-conjectures-fold','tauceti-fold','tag-queue','article-organ'})


def classify(provenance):
    """Return zero or one known family; no user-defined regex or fuzzy aliases."""
    source, method = provenance.get('source'), provenance.get('method')
    if not isinstance(source,str) or not isinstance(method,str): return None
    def result(family, primary, support=(), kinds=(), witness=None):
        return {'family':family,'primary':tuple(primary),'support':tuple(support),'kinds':tuple(kinds),'witness':witness}
    if source == 'tag-queue' and method.startswith('AI-queued @[wikidata] candidate (') and method.endswith(')'):
        queue = provenance.get('queue')
        if queue not in QUEUES: return None
        status = method[len('AI-queued @[wikidata] candidate ('):-1]
        if not status or len(status)>256 or any(c in status for c in '\r\n'): return None
        return result('tag-queue',(QUEUES[queue],),('bot-cut-log','brain-discovery-rejected','grounding-overrides'),('formalizes',),'queue')
    if source=='wikilean' and method=='annotated article (D1)':
        return result('article-organ',('annotations',),UNIVERSE,(), 'article')
    if source=='mathlib':
        if method=='@[wikidata] attribute (mathlib4 source)':
            return result('mathlib-gold',('mathlib-tag-xrefs',),('concept-graph','rebuild-grounding','wikidata-universe','universe-extension'),('formalizes',),'tag')
        if method=='agent+oracle':
            return result('agent-grounding',('concept-graph',),('rebuild-grounding','declaration-oracle','mathlib-source-tree','grounding-overrides'),('formalizes',))
        if method=='container_links':
            return result('container-fold',('brain-container-links',),('hierarchy','wikidata-universe','universe-extension'),('formalizes',),'container')
        if method=='discovery_proposals (verified)':
            return result('discovery-fold',('brain-discovery-proposals',),('hierarchy','declaration-oracle','mathlib-source-tree','wikidata-universe','universe-extension','mathlib-tag-xrefs'),(), 'discovery')
    if source=='annotations' and method=='annotation-citation (decl_qid_roles_v2)':
        return result('annotation-citation',('decl-qid-roles',),('annotations','concept-graph','wikidata-universe'),('mentions',))
    if source=='mathlib_deps' and method=='lift_formal_edges (formal_dependency.csv)':
        return result('formal-dependency-lift',('concept-graph',),('decl-to-qid','rebuild-grounding'),('depends',))
    if source=='wikidata_props' and method=='wikidata-claims':
        return result('wikidata-relations',('wikidata-edges',),('concept-graph','wikidata-universe'),('relates',),'wikidata')
    if source in XREF_DBS and method=='wikidata-property':
        return result('wikidata-external-ids',('wikidata-crossrefs',),('concept-graph','source-registry'),('xref',))
    if source in {'stacks','kerodon'} and method==f'@[{source}] attribute (mathlib4 source)':
        return result('mathlib-external-tags',('mathlib-tag-xrefs',),('concept-graph','declaration-oracle'),('xref',),'tag')
    if source=='theoremgraph':
        if method in {'hierarchy.json file-tree','module-prefix placement'}:
            return result('hierarchy-containment',('hierarchy',),MODULES,('contains',))
        if method=='theoremgraph_links':
            return result('theoremgraph-derived-links',('theoremgraph-links',),('theorem-matching','concept-graph'),('matches','cites'))
        if method in {'theorem_matching dual-judge','theorem_matching transitive-join'}:
            return result('theoremgraph-matching',('theorem-matching',),('concept-graph','decl-to-qid'),('matches','cites'))
        if method=='arxiv-id prefix (paper→statement)':
            return result('literature-containment',('theoremgraph-links',),('theorem-matching',),('contains',))
    if source=='openalex' and method=='referenced_works':
        return result('openalex-citations',('external-arxiv-citations',),('theoremgraph-links','theorem-matching'),('links',),'citation')
    if source in DBS:
        if method=='external-ingest page qid':
            return result('external-page-qid',('external-pages',),(*UNIVERSE,'source-registry'),('xref',),'page')
        if method=='sync-agents ext-anchor (fold-verified)':
            return result('external-verified-anchor',('brain-ext-anchor-links',),('external-pages',*UNIVERSE,'source-registry'),('xref',))
        if method=='internal_link':
            return result('external-internal-link',('external-links',),('external-pages','source-registry'),('links',),'link')
        if method=='internal_link (projected)':
            return result('external-projected-link',('external-links',),('external-pages','wikidata-crossrefs','mathlib-tag-xrefs','brain-ext-anchor-links','formal-conjectures','tauceti','user-repos','erdos-joins','source-registry'),('links',),'projected-link')
    if source in REPOS:
        group=REPOS[source]
        trees={'formal_conjectures':'file-tree (formal_conjectures.jsonl)','tauceti':'file-tree (tauceti.jsonl)'}
        tree = method==trees.get(source) or source=='user_lean_repos' and method.startswith('file-tree (user_repos/') and method.endswith('.jsonl)')
        if tree or method in {'module-prefix placement','fq-name-in-statement','wikipedia-reference (module docstring)','wikipedia-citation (docstring)'}:
            kinds=('contains',) if tree or method=='module-prefix placement' else ('invocation',) if method=='fq-name-in-statement' else ('formalizes',) if method.startswith('wikipedia-reference') else ('mentions',)
            if method.startswith('wikipedia-reference') and source!='formal_conjectures': return None
            return result('frontier-library',(group,),(*UNIVERSE,'mathlib-source-tree','declaration-oracle'),kinds)
        if source=='formal_conjectures' and method=='fc-agent (fold-verified)':
            return result('formal-conjectures-fold',('brain-fc-links',),('formal-conjectures','wikidata-universe','universe-extension'),('formalizes','mentions'),'fc')
        if source=='tauceti' and method=='agent-join (fold-verified)':
            return result('tauceti-fold',('tauceti-links',),('tauceti','wikidata-universe','universe-extension'),('mentions',))
    if source in {'erdos','oeis'}:
        group=None
        if method=='formal-conjectures reference URL': group='formal-conjectures'
        elif method=='tauceti reference URL': group='tauceti'
        elif method.startswith('user-repo reference URL (') and method.endswith(')'): group='user-repos'
        if group: return result('frontier-reference-xrefs',(group,),('source-registry',),('xref',))
        if source=='oeis' and method=='erdosproblems.com join (problems.yaml)':
            return result('erdos-oeis-join',('formal-conjectures','erdos-joins'),(),('xref',))
    return None
