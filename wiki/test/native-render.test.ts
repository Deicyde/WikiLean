import { describe, it, expect } from 'vitest';
import { buildClientData, renderArticlePage } from '../src/engine/page.js';
import { wrapAnnotations } from '../src/engine/wrap.js';
import { ANCHOR_PROFILE, wikipediaSourceUri, type NativeItem } from '../src/native-companion.js';
import type { Annotation } from '../src/engine/types.js';
const context = { wikipediaTitle: 'Test Article', revid: 12345 };
const source = { type: 'generic' as const, uri: wikipediaSourceUri(context) };
const body = '<p>A finite group has a finite set of elements.</p>';
const item = (): NativeItem => ({ id:'aaaaaaaaaaaa', status:'formalized', label:'Group', links:[
  {id:'a', annotation:{source,target:'https://example.org/docs#Finite%20Group',location:{profile:ANCHOR_PROFILE,anchor:{section:'(lead)',snippet:'finite group'}},extensions:{custom:{data:[1,2,3]}}}},
], unlinked:[], legacy:{secret:'private'}, note:'private note', proof_note:'private proof', custom:'private metadata' });
const annotation = (value: NativeItem) => value as Annotation;
describe('native article rendering', () => {
  it('requires source context and exact source identity before highlighting', () => {
    expect(wrapAnnotations(body,[annotation(item())]).matched).toEqual([false]);
    expect(wrapAnnotations(body,[annotation(item())],context).matched).toEqual([true]);
    expect(wrapAnnotations(body,[annotation(item())],{...context,revid:54321}).matched).toEqual([false]);
  });
  it('requires every occurrence to resolve and deduplicates repeated span indices', () => {
    const a=item(); a.links.push({...structuredClone(a.links[0]),id:'b'});
    const wrapped=wrapAnnotations(body,[annotation(a)],context);
    expect(wrapped.matched).toEqual([true]); expect(wrapped.html).toContain('data-anno-indices="0"');
    expect(wrapped.html).not.toContain('0,0');
    a.unlinked.push({source,location:{profile:ANCHOR_PROFILE,anchor:{snippet:'absent',section:'(lead)'}}});
    expect(wrapAnnotations(body,[annotation(a)],context).matched).toEqual([false]);
  });
  it('distinguishes absent location from empty generic location', () => {
    const a=item(); delete a.links[0].annotation.location;
    expect(wrapAnnotations(body,[annotation(a)],context).matched).toEqual([true]);
    a.links[0].annotation.location={};
    expect(wrapAnnotations(body,[annotation(a)],context).matched).toEqual([false]);
  });
  it('renders arbitrary formal targets without making unsafe URIs clickable or leaking private fields', () => {
    const a=item(); a.links.push({id:'b',annotation:{source,target:'javascript:alert(1)'}},
      {id:'c',annotation:{source,target:'../lean.html#Theorem'},base_uri:'https://example.net/shards/a.json'},
      {id:'d',annotation:{source,target:'https://name:secret@example.net/private'}});
    const data=buildClientData([annotation(a)])[0]!;
    expect(data.formal_links).toEqual([
      {target:a.links[0].annotation.target,label:'Finite Group',url:a.links[0].annotation.target},
      {target:'javascript:alert(1)',label:'javascript:alert(1)',url:undefined},
      {target:'../lean.html#Theorem',label:'Theorem',url:'https://example.net/lean.html#Theorem'},
      {target:'https://name:secret@example.net/private',label:'https://name:secret@example.net/private',url:undefined},
    ]);
    expect(JSON.stringify(data)).not.toMatch(/private note|private proof|metadata|legacy|secret.*private metadata/);
    expect(data).not.toHaveProperty('note'); expect(data).not.toHaveProperty('legacy');
  });
  it('keeps unsupported locations visible without a false highlight or private archive', () => {
    const a=item(); a.links[0].annotation={source:{type:'pdf',uri:'https://example.net/paper.pdf'},target:'urn:formal:test',location:{page:1}};
    const wrapped=wrapAnnotations(body,[annotation(a)],context);
    const page=renderArticlePage({slug:'Test',displayTitle:'Test',wikipediaTitle:context.wikipediaTitle,revid:context.revid,body:wrapped.html,annotations:[annotation(a)],matched:wrapped.matched,wpHtml:body});
    expect(page).toContain('location not displayed on this page');
    expect(page).toContain('urn:formal:test');
    expect(page).not.toContain('private note'); expect(page).not.toContain('private metadata');
  });
});
