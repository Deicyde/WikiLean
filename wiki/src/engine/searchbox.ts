// Shared header article-search widget, emitted into both page shells (the
// article shell in engine/page.ts and the meta shell in pages.ts). Three
// pieces so each shell keeps its markup valid: CSS for the shell's <style>
// block, the input+dropdown markup for the header, and the behavior <script>
// for the end of <body>. One searchbox per page (the ids are singletons).
//
// Behavior: on first focus the script fetches GET /api/articles (the compact
// KV-cached index, ~one fetch per page view at most) and filters client-side —
// case/diacritic-insensitive substring on the display title, the same
// normalization family as the /articles #q filter — rendering the top 8 hits
// as title + a small formalized/total coverage figure. ArrowUp/ArrowDown +
// Enter navigate, Escape closes, click navigates.
//
// XSS: hit rows are built with createElement/textContent only — article titles
// are data, never markup, so a hostile display_title cannot inject HTML.
//
// Colors are the shared site palette (warm paper + [data-theme="dark"]
// overrides, hardcoded like pages.ts SHELL_CSS — both shells' no-FOUC scripts
// always stamp data-theme, so no prefers-color-scheme twin is needed).

export const SEARCHBOX_CSS = `
.wl-searchbox{position:relative;display:inline-flex;align-items:center}
.wl-search{width:170px;max-width:44vw;padding:4px 9px;font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#1f1d1a;background:#fffdf9;border:1px solid #d8d0bd;border-radius:6px}
.wl-search::placeholder{color:#6e675a}
.wl-search:focus{outline:2px solid #1a4b8c;outline-offset:0;border-color:#1a4b8c}
.wl-search-list{position:absolute;top:calc(100% + 4px);left:0;right:auto;min-width:250px;max-width:min(330px,90vw);max-height:320px;overflow-y:auto;background:#fffdf9;border:1px solid #d8d0bd;border-radius:8px;box-shadow:0 4px 14px rgba(60,50,30,.14);z-index:300}
.wl-search-list[hidden]{display:none}
.wl-search-hit{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:7px 11px;text-decoration:none;color:#1f1d1a;font-size:13px;border-bottom:1px solid #ece6d8}
.wl-search-hit:last-child{border-bottom:none}
.wl-search-hit:hover,.wl-search-hit.active{background:rgba(26,75,140,.07)}
.wl-search-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wl-search-m{color:#5f594e;font-size:11px;font-variant-numeric:tabular-nums;flex:none}
[data-theme="dark"] .wl-search{background:#232020;color:#ebe5d8;border-color:#4d4742}
[data-theme="dark"] .wl-search::placeholder{color:#8a8278}
[data-theme="dark"] .wl-search:focus{outline-color:#6e9adf;border-color:#6e9adf}
[data-theme="dark"] .wl-search-list{background:#232020;border-color:#4d4742;box-shadow:0 4px 14px rgba(0,0,0,.45)}
[data-theme="dark"] .wl-search-hit{color:#ebe5d8;border-bottom-color:#3a3530}
[data-theme="dark"] .wl-search-hit:hover,[data-theme="dark"] .wl-search-hit.active{background:rgba(110,154,223,.12)}
[data-theme="dark"] .wl-search-m{color:#9a9081}
/* The dropdown is left-anchored at EVERY width: the meta shell's header wraps
   early (~641-790px puts the input at the left edge), where a right-anchored
   list hangs off-screen — and in both shells >=330px of nav links trail the
   input, so left-anchoring can never overflow the right edge. */
@media (max-width:640px){
/* iOS zooms the page when a focused input's font-size is < 16px (same fix as
   the flag comment box in style.css). */
.wl-search{width:130px;font-size:16px}
.wl-search-list{max-width:calc(100vw - 40px)}
}
`;

export const SEARCHBOX_HTML =
  `<span class="wl-searchbox">` +
  `<input id="wl-search" class="wl-search" type="search" placeholder="Search articles" ` +
  `aria-label="Search articles" autocomplete="off" role="combobox" ` +
  `aria-expanded="false" aria-controls="wl-search-list" aria-autocomplete="list">` +
  `<div id="wl-search-list" class="wl-search-list" role="listbox" aria-label="Article matches" hidden></div>` +
  `</span>`;

export const SEARCHBOX_SCRIPT = `<script>
/* Header article search — see engine/searchbox.ts. Fetches /api/articles once
   on first focus; rows are [slug, title, n_formalized, n_partial, n_not]. */
(function(){
var inp=document.getElementById("wl-search"),list=document.getElementById("wl-search-list");
if(!inp||!list)return;
var data=null,loading=false,items=[],sel=-1;
function norm(s){s=String(s).toLowerCase();try{s=s.normalize("NFD").replace(/[\\u0300-\\u036f]/g,"")}catch(e){}return s}
function load(){if(data||loading)return;loading=true;
fetch("/api/articles").then(function(r){return r.json()}).then(function(j){
data=((j&&j.articles)||[]).map(function(a){return{slug:String(a[0]),title:String(a[1]),f:a[2],p:a[3],n:a[4],key:norm(a[1])}});
/* only render if the input still has focus — a blur while the fetch was in
   flight must not pop a dropdown nothing will ever close */
if(document.activeElement===inp)render()}).catch(function(){loading=false})}
function hide(){list.hidden=true;list.textContent="";items=[];sel=-1;
inp.setAttribute("aria-expanded","false");inp.removeAttribute("aria-activedescendant")}
function mark(){for(var i=0;i<items.length;i++)items[i].className=i===sel?"wl-search-hit active":"wl-search-hit";
if(sel>=0)inp.setAttribute("aria-activedescendant",items[sel].id);
else inp.removeAttribute("aria-activedescendant")}
function render(){
var q=norm(inp.value.trim());
list.textContent="";items=[];sel=-1;
inp.removeAttribute("aria-activedescendant");
if(!data||!q){list.hidden=true;inp.setAttribute("aria-expanded","false");return}
/* prefix matches rank above substring matches, both in title order */
var hits=[],rest=[];
for(var i=0;i<data.length&&hits.length<8;i++){var d=data[i],ix=d.key.indexOf(q);
if(ix===0)hits.push(d);else if(ix>0&&rest.length<8)rest.push(d)}
for(var i=0;i<rest.length&&hits.length<8;i++)hits.push(rest[i]);
if(!hits.length){list.hidden=true;inp.setAttribute("aria-expanded","false");return}
for(var i=0;i<hits.length;i++){var h=hits[i];
var a=document.createElement("a");
a.className="wl-search-hit";a.setAttribute("role","option");a.id="wl-search-hit-"+i;
a.href="/"+encodeURIComponent(h.slug);
var t=document.createElement("span");t.className="wl-search-t";t.textContent=h.title;a.appendChild(t);
if(typeof h.f==="number"&&typeof h.p==="number"&&typeof h.n==="number"&&h.f+h.p+h.n>0){
var m=document.createElement("span");m.className="wl-search-m";m.textContent=h.f+"/"+(h.f+h.p+h.n);a.appendChild(m)}
list.appendChild(a);items.push(a)}
list.hidden=false;inp.setAttribute("aria-expanded","true")}
inp.addEventListener("focus",function(){load();render()});
inp.addEventListener("input",render);
inp.addEventListener("blur",hide);
/* mousedown would blur the input and hide the list before the click lands on
   a hit — suppress the blur; the anchor's native click then navigates. */
list.addEventListener("mousedown",function(e){e.preventDefault()});
/* "Down"/"Up"/"Esc" are the legacy key names (old Edge/IE + some synthetic
   event sources) — accept both spellings. */
inp.addEventListener("keydown",function(e){
var k=e.key;
if(k==="Escape"||k==="Esc"){hide();return}
if(!items.length)return;
if(k==="ArrowDown"||k==="Down"){e.preventDefault();sel=(sel+1)%items.length;mark()}
else if(k==="ArrowUp"||k==="Up"){e.preventDefault();sel=sel<=0?items.length-1:sel-1;mark()}
else if(k==="Enter"){e.preventDefault();var t=items[sel>=0?sel:0];if(t)location.href=t.href}});
})();
</script>`;
