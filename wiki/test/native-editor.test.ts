import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { describe, it, expect } from 'vitest';
const source = readFileSync(new URL('../assets/editor.js', import.meta.url),'utf8');
const sandbox: Record<string,any> = { URL };
vm.runInNewContext(source.split('// ---- editor runtime ----')[0], sandbox);
const model=sandbox.createCompanionEditorModel();
const sourceUri='https://en.wikipedia.org/w/index.php?title=Test&oldid=1';
const docs='https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#AddCommGroup';
const mathlib={type:'lean',uri:'https://github.com/leanprover-community/mathlib4',location:{declaration:'AddCommGroup',module:'Mathlib.Algebra.Group.Defs'}};
const base=()=>({id:'aaaaaaaaaaaa',status:'partial',links:[
  {id:'first',base_uri:'https://example.net/shards/a.json',annotation:{subject:[{type:'pdf',uri:'../paper.pdf',location:{page:2,rect:[0,0,0.5,0.5]},extensions:{sourceApp:[42]}}],
    object:[{type:'generic',uri:'../formal.html#One'}],relation:'closeMatch',id:'external-id',extensions:{opaque:{a:['kept',null,true]}}}},
  {id:'second',annotation:{subject:[{type:'html',uri:sourceUri,location:{profile:'urn:wikilean:anchor:v3',anchor:{section:'(lead)',snippet:'sample',extra:'kept'},custom:{color:'blue'}}}],
    object:[{...mathlib,version:'a'.repeat(40)}]}}],
  unlinked:[{type:'html',uri:'https://example.net/article',location:{css:'#theorem'}}],legacy:{original:'private'},review:{decision:'retained'}});
let id=0; const newId=()=>String(++id);
describe('native editor preservation',()=>{
  it('round-trips unedited records and preserves extensions, bases, IDs and unrelated links on target edits',()=>{
    const before=base();
    expect(model.edit(before,'link:first',{target:'../formal.html#One'},newId).item).toEqual(before);
    const after=model.edit(before,'link:first',{target:'urn:changed'},newId).item;
    // The new object replaces the old one; the relation described the old one.
    const expected:any=base(); expected.links[0].annotation.object=[{type:'generic',uri:'urn:changed'}]; delete expected.links[0].annotation.relation;
    expect(after).toEqual(expected); expect(before).toEqual(base());
  });
  it('shows a Mathlib declaration as its docs page and writes a docs page back as the declaration',()=>{
    const entries=model.entries(base());
    expect(model.targetOf(entries[1])).toBe(docs);
    expect(model.targetOf(entries[2])).toBe('');
    expect(model.edit(base(),'link:second',{target:docs},newId).item).toEqual(base());
    expect(model.endOf(docs)).toEqual(mathlib);
    expect(model.endOf('https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html#Foo.bar')).toEqual({type:'lean',uri:mathlib.uri,location:{declaration:'Foo.bar',module:'Mathlib.X'}});
    expect(model.endOf('https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html')).toEqual({type:'generic',uri:'https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html'});
    expect(model.endOf('https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html#%E0%A4%A')).toMatchObject({type:'generic'});
    // A changed reference drops the version that named the old declaration's commit.
    const after=model.edit(base(),'link:second',{target:'https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html#Foo.bar'},newId).item;
    expect(after.links[1].annotation.object).toEqual([{type:'lean',uri:mathlib.uri,location:{declaration:'Foo.bar',module:'Mathlib.X'}}]);
    expect(model.edit(base(),'link:second',{target:''},newId).item.unlinked).toHaveLength(2);
  });
  it('never treats a retained PDF location as editable Wikipedia prose',()=>{
    expect(model.anchor(model.entries(base())[0],sourceUri)).toBeNull();
    expect(()=>model.edit(base(),'link:first',{anchor:{snippet:'bad'}},newId)).toThrow(/cannot be edited/);
  });
  it('explicit re-anchoring changes only the selected end',()=>{
    const after=model.edit(base(),'link:first',{anchor:{section:'New',snippet:'text'},reanchorSource:sourceUri},newId).item;
    expect(after.links[0].annotation.subject).toEqual([{type:'html',uri:sourceUri,location:{profile:'urn:wikilean:anchor:v3',anchor:{section:'New',snippet:'text'}},extensions:{sourceApp:[42]}}]);
    const {subject:_,...rest}=after.links[0].annotation; const {subject:__,...restBefore}=base().links[0].annotation;
    expect(rest).toEqual(restBefore);
    expect(after.links[1]).toEqual(base().links[1]); expect(after.unlinked).toEqual(base().unlinked);
  });
  it('keeps junk values with their end: a replaced object takes its own along and a re-anchored end drops its own',()=>{
    const withJunk=()=>{const item:any=base();
      item.links[0].annotation.subject[0].junk=['pdf junk']; item.links[0].annotation.object[0].junk=['first object junk'];
      item.links[1].annotation.subject[0].junk=['article junk']; item.links[1].annotation.object[0].junk=['Nat.card of an infinite type is 0'];
      return item;};
    expect(model.edit(withJunk(),'link:second',{target:docs},newId).item).toEqual(withJunk());
    const retargeted=model.edit(withJunk(),'link:second',{target:'urn:changed'},newId).item.links[1].annotation;
    expect(retargeted.object).toEqual([{type:'generic',uri:'urn:changed'}]); expect(retargeted).not.toHaveProperty('junk');
    expect(retargeted.subject).toEqual(withJunk().links[1].annotation.subject);
    // A range edit and an explicit re-anchor each drop only the selected end's junk.
    const ranged=model.edit(withJunk(),'link:second',{anchor:{section:'New',snippet:'text'}},newId).item;
    expect(ranged.links[1].annotation.subject[0]).not.toHaveProperty('junk');
    expect(ranged.links[1].annotation.object).toEqual(withJunk().links[1].annotation.object);
    expect(ranged.links[0]).toEqual(withJunk().links[0]);
    const reanchored=model.edit(withJunk(),'link:first',{anchor:{section:'New',snippet:'text'},reanchorSource:sourceUri},newId).item;
    expect(reanchored.links[0].annotation.subject[0]).not.toHaveProperty('junk');
    expect(reanchored.links[0].annotation.object).toEqual(withJunk().links[0].annotation.object);
    // Unlinking leaves the subject end as it was, its junk included.
    const unlinked=model.edit(withJunk(),'link:second',{target:''},newId).item;
    expect(unlinked.unlinked[1]).toEqual(withJunk().links[1].annotation.subject[0]);
  });
  it('edits anchors on html ends only',()=>{
    const item={links:[],unlinked:[{type:'html',uri:sourceUri,location:{profile:'urn:wikilean:anchor:v3',anchor:{snippet:'old'}}}]};
    expect(model.anchor(model.entries(item)[0],sourceUri)).toEqual({snippet:'old'});
    expect(model.edit(item,'unlinked:0',{anchor:{snippet:'new'}},newId).item.unlinked[0].location.anchor).toEqual({snippet:'new'});
    const generic={links:[],unlinked:[{type:'generic',uri:sourceUri,location:{profile:'urn:wikilean:anchor:v3',anchor:{snippet:'old'}}}]};
    expect(model.anchor(model.entries(generic)[0],sourceUri)).toBeNull();
  });
  it('preserves unknown selector details on ordinary range edits',()=>{
    const after=model.edit(base(),'link:second',{anchor:{section:'New',snippet:'text'}},newId).item;
    expect(after.links[1].annotation.subject[0].location.custom).toEqual({color:'blue'});
    expect(after.links[1].annotation.subject[0].location.anchor.extra).toBe('kept');
  });
  it('retains targetless work, and removing one link never removes its source or other links',()=>{
    const result=model.edit(base(),'link:first',{target:''},newId);
    expect(result.item.links).toHaveLength(1); expect(result.item.unlinked).toHaveLength(2);
    expect(result.item.unlinked[1]).toEqual({...base().links[0].annotation.subject[0],base_uri:base().links[0].base_uri});
    expect(result.key).toBe('unlinked:1');
    const linked=model.edit(result.item,result.key,{target:'urn:restored'},newId);
    expect(linked.item.links).toHaveLength(2);expect(linked.item.unlinked).toHaveLength(1);
    expect(linked.item.links[1]).toMatchObject({base_uri:base().links[0].base_uri,annotation:{subject:base().links[0].annotation.subject,object:[{type:'generic',uri:'urn:restored'}]}});
  });
  it('lists every subject end of a link and returns all of them to unlinked work together',()=>{
    const joint:any=base(); joint.links[1].annotation.subject.push({type:'html',uri:sourceUri,location:{fragment:'Proof'}});
    const keys=model.entries(joint).map((entry:any)=>entry.key);
    expect(keys).toEqual(['link:first','link:second','link-end:1:second','unlinked:0']);
    expect(model.targetOf(model.selected(joint,'link-end:1:second'))).toBe(docs);
    const result=model.edit(joint,'link-end:1:second',{target:''},newId);
    expect(result.item.links).toHaveLength(1);
    expect(result.item.unlinked.slice(1)).toEqual(joint.links[1].annotation.subject);
    expect(model.selected(result.item,result.key).occurrence).toEqual({type:'html',uri:sourceUri,location:{fragment:'Proof'}});
  });
  it('adds a separate link without replacing existing records or minting another external id',()=>{
    const after=model.add(base(),'link:first','urn:additional',newId).item;
    expect(after.links.slice(0,2)).toEqual(base().links);expect(after.unlinked).toEqual(base().unlinked);
    expect(after.links[2]).toMatchObject({base_uri:base().links[0].base_uri,annotation:{subject:base().links[0].annotation.subject,object:[{type:'generic',uri:'urn:additional'}]}});
    expect(after.links[2].annotation).not.toHaveProperty('id'); expect(after.links[2].annotation).not.toHaveProperty('relation');
  });
  it('keeps the intended second source selected when promoting the first targetless occurrence',()=>{
    const before={links:[],unlinked:[{type:'generic',uri:'urn:first'},{type:'generic',uri:'urn:second'}]};
    const result=model.edit(before,'unlinked:0',{target:'urn:formal'},newId);
    const key=model.selectionAfterEdit(before,'unlinked:0','unlinked:1',result.item,result.key);
    expect(model.selected(result.item,key).occurrence.uri).toBe('urn:second');
    const after=model.edit(result.item,key,{target:'urn:second-formal'},newId).item;
    expect(after.links.map((link:any)=>link.annotation.subject[0].uri)).toEqual(['urn:first','urn:second']);
  });
  it('stores a located anchor as a quotation with the editing key in the end, and falls back to the profile form',()=>{
    const locate=(anchor:any)=>anchor.snippet==='findable text'?{quote:{exact:'findable text',suffix:' here'}}:null;
    const item=model.makeItem(sourceUri,{section:'Statement',snippet:'findable text'},locate);
    expect(item.unlinked[0]).toEqual({type:'html',uri:sourceUri,location:{quote:{exact:'findable text',suffix:' here'}},extensions:{'org.wikilean':{anchor:{section:'Statement',snippet:'findable text'}}}});
    expect(model.anchor(model.entries(item)[0],sourceUri)).toEqual({section:'Statement',snippet:'findable text'});
    // An edit re-locates; a snippet the page cannot find returns to the profile form and drops the stale key.
    const edited=model.edit(item,'unlinked:0',{anchor:{snippet:'absent text'}},newId,locate).item;
    expect(edited.unlinked[0]).toEqual({type:'html',uri:sourceUri,location:{profile:'urn:wikilean:anchor:v3',anchor:{section:'Statement',snippet:'absent text'}}});
    const back=model.edit(edited,'unlinked:0',{anchor:{snippet:'findable text'}},newId,locate).item;
    expect(back.unlinked[0].location).toEqual({quote:{exact:'findable text',suffix:' here'}});
    // Promotion to a link carries the end with its key; another link at the place shares the end.
    const linked=model.edit(back,'unlinked:0',{target:'urn:formal'},newId).item;
    expect(linked.links[0].annotation).toEqual({subject:[back.unlinked[0]],object:[{type:'generic',uri:'urn:formal'}]});
    const added=model.add(linked,'link:'+linked.links[0].id,"urn:other",newId).item;
    expect(added.links[1].annotation.subject).toEqual([back.unlinked[0]]);
    expect(model.makeItem(sourceUri,{section:'S',snippet:'nothing'},locate).unlinked[0].location.profile).toBe('urn:wikilean:anchor:v3');
  });
  it('new work requires a pinned source and begins with one targetless occurrence',()=>{
    expect(()=>model.makeItem(null,{snippet:'text'})).toThrow(/pinned/);
    expect(model.makeItem(sourceUri,{section:'(lead)',snippet:'text'})).toMatchObject({links:[],unlinked:[{type:'html',uri:sourceUri}]});
  });
});
