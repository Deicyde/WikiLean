import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { describe, it, expect } from 'vitest';
const source = readFileSync(new URL('../assets/editor.js', import.meta.url),'utf8');
const sandbox: Record<string,any> = { URL };
vm.runInNewContext(source.split('// ---- editor runtime ----')[0], sandbox);
const model=sandbox.createCompanionEditorModel();
const sourceUri='https://en.wikipedia.org/w/index.php?title=Test&oldid=1';
const base=()=>({id:'aaaaaaaaaaaa',status:'partial',links:[{id:'first',base_uri:'https://example.net/shards/a.json',annotation:{source:{type:'pdf',uri:'../paper.pdf',extensions:{sourceApp:[42]}},target:'../formal.html#One',location:{page:2,rect:[0,0,0.5,0.5]},id:'external-id',extensions:{opaque:{a:['kept',null,true]}}}},{id:'second',annotation:{source:{type:'generic',uri:sourceUri},target:'urn:two',location:{profile:'urn:wikilean:anchor:v3',anchor:{section:'(lead)',snippet:'sample',extra:'kept'},custom:{color:'blue'}}}}],unlinked:[{source:{type:'html',uri:'https://example.net/article'},location:{css:'#theorem'}}],legacy:{original:'private'},review:{decision:'retained'}});
let id=0; const newId=()=>String(++id);
describe('native editor preservation',()=>{
  it('round-trips unedited records and preserves extensions, bases, IDs and unrelated links on target edits',()=>{
    const before=base();
    expect(model.edit(before,'link:first',{target:before.links[0].annotation.target},newId).item).toEqual(before);
    const after=model.edit(before,'link:first',{target:'urn:changed'},newId).item;
    const expected=base(); expected.links[0].annotation.target='urn:changed';
    expect(after).toEqual(expected); expect(before).toEqual(base());
  });
  it('never treats a retained PDF location as editable Wikipedia prose',()=>{
    expect(model.anchor(model.entries(base())[0],sourceUri)).toBeNull();
    expect(()=>model.edit(base(),'link:first',{anchor:{snippet:'bad'}},newId)).toThrow(/cannot be edited/);
  });
  it('explicit re-anchoring changes only the selected source and selector',()=>{
    const after=model.edit(base(),'link:first',{anchor:{section:'New',snippet:'text'},reanchorSource:sourceUri},newId).item;
    expect(after.links[0].annotation.source).toEqual({type:'generic',uri:sourceUri});
    expect(after.links[0].annotation.extensions).toEqual(base().links[0].annotation.extensions);
    expect(after.links[1]).toEqual(base().links[1]); expect(after.unlinked).toEqual(base().unlinked);
  });
  it('preserves unknown selector details on ordinary range edits',()=>{
    const after=model.edit(base(),'link:second',{anchor:{section:'New',snippet:'text'}},newId).item;
    expect(after.links[1].annotation.location.custom).toEqual({color:'blue'});
    expect(after.links[1].annotation.location.anchor.extra).toBe('kept');
  });
  it('retains targetless work, and removing one link never removes its source or other links',()=>{
    const result=model.edit(base(),'link:first',{target:''},newId);
    expect(result.item.links).toHaveLength(1); expect(result.item.unlinked).toHaveLength(2);
    expect(result.item.unlinked[1]).toEqual({source:base().links[0].annotation.source,location:base().links[0].annotation.location,base_uri:base().links[0].base_uri});
    const linked=model.edit(result.item,result.key,{target:'urn:restored'},newId);
    expect(linked.item.links).toHaveLength(2);expect(linked.item.unlinked).toHaveLength(1);
  });
  it('adds a separate link without replacing existing records or minting another external id',()=>{
    const after=model.add(base(),'link:first','urn:additional',newId).item;
    expect(after.links.slice(0,2)).toEqual(base().links);expect(after.unlinked).toEqual(base().unlinked);
    expect(after.links[2].annotation.target).toBe('urn:additional');expect(after.links[2].annotation).not.toHaveProperty('id');
  });
  it('keeps the intended second source selected when promoting the first targetless occurrence',()=>{
    const before={links:[],unlinked:[{source:{type:'generic',uri:'urn:first'}},{source:{type:'generic',uri:'urn:second'}}]};
    const result=model.edit(before,'unlinked:0',{target:'urn:formal'},newId);
    const key=model.selectionAfterEdit(before,'unlinked:0','unlinked:1',result.item,result.key);
    expect(model.selected(result.item,key).occurrence.source.uri).toBe('urn:second');
    const after=model.edit(result.item,key,{target:'urn:second-formal'},newId).item;
    expect(after.links.map((link:any)=>link.annotation.source.uri)).toEqual(['urn:first','urn:second']);
  });
  it('new work requires a pinned source and begins with one targetless occurrence',()=>{
    expect(()=>model.makeItem(null,{snippet:'text'})).toThrow(/pinned/);
    expect(model.makeItem(sourceUri,{section:'(lead)',snippet:'text'})).toMatchObject({links:[],unlinked:[{source:{type:'generic',uri:sourceUri}}]});
  });
});
