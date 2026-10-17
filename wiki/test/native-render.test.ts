import { describe, it, expect } from 'vitest';
import { buildClientData, renderArticlePage } from '../src/engine/page.js';
import { wrapAnnotations } from '../src/engine/wrap.js';
import { ANCHOR_PROFILE, MATHLIB_REPOSITORY, mathlibEnd, wikipediaSourceUri, type LinkEnd, type NativeItem } from '../src/native-companion.js';
import type { Annotation } from '../src/engine/types.js';
const context = { wikipediaTitle: 'Test Article', revid: 12345 };
const source = { type: 'html' as const, uri: wikipediaSourceUri(context) };
const formal = (uri: string): LinkEnd[] => [{ type: 'generic', uri }];
const body = '<p>A finite group has a finite set of elements.</p>';
const item = (): NativeItem => ({ id:'aaaaaaaaaaaa', status:'formalized', label:'Group', links:[
  {id:'a', annotation:{subject:[{...source,location:{profile:ANCHOR_PROFILE,anchor:{section:'(lead)',snippet:'finite group'}}}],object:formal('https://example.org/docs#Finite%20Group'),extensions:{custom:{data:[1,2,3]}}}},
], unlinked:[], legacy:{secret:'private'}, note:'a public note', proof_note:'private proof', custom:'private metadata' });
const annotation = (value: NativeItem) => value as Annotation;
const at = (value: NativeItem) => value.links[0].annotation.subject[0];
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
    a.unlinked.push({...source,location:{profile:ANCHOR_PROFILE,anchor:{snippet:'absent',section:'(lead)'}}});
    expect(wrapAnnotations(body,[annotation(a)],context).matched).toEqual([false]);
  });
  it('highlights built-in quotations and fragments', () => {
    const page='<h2><span class="mw-headline" id="Statement">Statement</span></h2><p>A finite group has a finite set of elements. A finite group is finite.</p>';
    const quoted=item(); at(quoted).location={quote:{exact:'A finite group',suffix:' is finite.'}};
    const wrapped=wrapAnnotations(page,[annotation(quoted)],context);
    expect(wrapped.matched).toEqual([true]);
    expect(wrapped.html).toContain('<span class="anno anno-formalized" data-status="formalized" data-provenance="ai" data-anno-indices="0" data-decl="Finite Group">A finite group</span> is finite.');
    const keyed=item(); at(keyed).location={quote:{exact:'nowhere'}}; at(keyed).extensions={'org.wikilean':{anchor:{section:'(lead)',snippet:'finite group'}}};
    expect(wrapAnnotations(body,[annotation(keyed)],context).matched).toEqual([true]);
    const heading=item(); at(heading).location={fragment:'Statement'};
    expect(wrapAnnotations(page,[annotation(heading)],context).html).toContain('data-decl="Finite Group"><span class="mw-headline" id="Statement">Statement</span></span></h2>');
    const missing=item(); at(missing).location={quote:{exact:'absent words'}};
    expect(wrapAnnotations(page,[annotation(missing)],context).matched).toEqual([false]);
    // Stale context stands in only for a unique quotation; 'A finite group' occurs twice here.
    const stale=item(); at(stale).location={quote:{exact:'A finite group',prefix:'stale '}};
    expect(wrapAnnotations(page,[annotation(stale)],context).matched).toEqual([false]);
    const entity=item(); at(entity).location={fragment:'cite_ref-1'};
    expect(wrapAnnotations('<p id="cite&#95;ref-1">x</p>'+page,[annotation(entity)],context).matched).toEqual([true]);
  });
  it('distinguishes absent location from empty location', () => {
    const a=item(); delete at(a).location;
    expect(wrapAnnotations(body,[annotation(a)],context).matched).toEqual([true]);
    at(a).location={};
    expect(wrapAnnotations(body,[annotation(a)],context).matched).toEqual([false]);
  });
  it('renders arbitrary formal targets without making unsafe URIs clickable or leaking private fields', () => {
    const a=item(); a.links.push({id:'b',annotation:{subject:[source],object:formal('javascript:alert(1)')}},
      {id:'c',annotation:{subject:[source],object:formal('../lean.html#Theorem')},base_uri:'https://example.net/shards/a.json'},
      {id:'d',annotation:{subject:[source],object:formal('https://name:secret@example.net/private')}},
      {id:'e',annotation:{subject:[source],object:[mathlibEnd('Mathlib.X','Foo.bar')]}},
      {id:'f',annotation:{subject:[source],object:[{type:'lean',uri:MATHLIB_REPOSITORY,version:'a'.repeat(40)}],relation:'noMatch'}},
      {id:'g',annotation:{subject:[source],object:[{type:'lean',uri:'https://example.net/project',location:{declaration:'Other.thm'}}]}});
    const data=buildClientData([annotation(a)])[0]!;
    expect(data.formal_links).toEqual([
      {target:'https://example.org/docs#Finite%20Group',label:'Finite Group',url:'https://example.org/docs#Finite%20Group'},
      {target:'javascript:alert(1)',label:'javascript:alert(1)',url:undefined},
      {target:'../lean.html#Theorem',label:'Theorem',url:'https://example.net/lean.html#Theorem'},
      {target:'https://name:secret@example.net/private',label:'https://name:secret@example.net/private',url:undefined},
      {target:'https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html#Foo.bar',label:'Foo.bar',url:'https://leanprover-community.github.io/mathlib4_docs/Mathlib/X.html#Foo.bar'},
      // A checked absence (f) names no formal result; a declaration elsewhere links to its project.
      {target:'https://example.net/project',label:'Other.thm',url:'https://example.net/project'},
    ]);
    expect(JSON.stringify(data)).not.toMatch(/private proof|private metadata|legacy|"secret"/);
    expect(data.note).toBe('a public note');
    expect(data).not.toHaveProperty('proof_note'); expect(data).not.toHaveProperty('legacy');
  });
  it('keeps unsupported locations visible without a false highlight or private archive', () => {
    const a=item(); a.links[0].annotation={subject:[{type:'pdf',uri:'https://example.net/paper.pdf',location:{page:1}}],object:formal('urn:formal:test')};
    const wrapped=wrapAnnotations(body,[annotation(a)],context);
    const page=renderArticlePage({slug:'Test',displayTitle:'Test',wikipediaTitle:context.wikipediaTitle,revid:context.revid,body:wrapped.html,annotations:[annotation(a)],matched:wrapped.matched,wpHtml:body});
    expect(page).toContain('location not displayed on this page');
    expect(page).toContain('urn:formal:test');
    expect(page).not.toContain('private proof'); expect(page).not.toContain('private metadata');
  });
});
