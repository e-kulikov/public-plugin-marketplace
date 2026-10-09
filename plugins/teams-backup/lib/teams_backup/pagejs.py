"""JavaScript run inside the Teams tab (through ``agent-browser eval``) and inside the transcript iframe.

Teams' web UI is not a public API: selectors below were taken from the live DOM (``data-tid`` /
``data-testid`` attributes are the most stable hooks it offers). The Python side fails loudly when a
hook disappears instead of guessing.
"""

# Installs window.__tb (idempotent). Everything else calls into it.
INSTALL = r"""
(()=>{
if (window.__tb && window.__tb.v === 4) return 'ok';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const tb = window.__tb = {v: 4, items: new Map(), lastAuthor: null};
const NB = / /g;
function emoji(n){ return /Emoji/i.test(n.getAttribute('itemtype') || '') || /emoji|emoticon/i.test(n.className + ' ' + (n.getAttribute('data-tid') || '')); }
function md(n){
  if (n.nodeType === 3) return n.nodeValue.replace(NB, ' ');
  if (n.nodeType !== 1) return '';
  const tag = n.tagName.toLowerCase();
  if (tag === 'script' || tag === 'style' || tag === 'svg' || tag === 'button') return '';
  if (tag === 'img') { const alt = n.getAttribute('alt') || ''; return emoji(n) ? alt : '[image' + (alt ? ': ' + alt : '') + ']'; }
  if (tag === 'br') return '\n';
  const kids = () => [...n.childNodes].map(md).join('');
  switch (tag) {
    case 'p': { const k = kids(); return k.trim() ? k.replace(/\n+$/, '') + '\n\n' : '\n'; }
    case 'div': { const k = kids(); return k.trim() ? k.replace(/\n+$/, '') + '\n' : k; }
    case 'strong': case 'b': { const k = kids().trim(); return k ? '**' + k + '**' : ''; }
    case 'em': case 'i': { const k = kids().trim(); return k ? '_' + k + '_' : ''; }
    case 's': case 'strike': case 'del': { const k = kids().trim(); return k ? '~~' + k + '~~' : ''; }
    case 'code': return n.parentElement && n.parentElement.tagName === 'PRE' ? kids() : '`' + kids() + '`';
    case 'pre': return '```\n' + n.textContent.replace(NB, ' ').replace(/\n$/, '') + '\n```\n';
    case 'blockquote': return kids().trim().split('\n').map(l => '> ' + l).join('\n') + '\n';
    case 'ul': case 'ol': return kids() + '\n';
    case 'li': { const p = n.parentElement; let pre = '- ';
      if (p && p.tagName === 'OL') pre = ([...p.children].indexOf(n) + 1) + '. ';
      const depth = (() => { let d = 0, e = p; while (e) { if (e.tagName === 'UL' || e.tagName === 'OL') d++; e = e.parentElement; } return d - 1; })();
      return '  '.repeat(Math.max(0, depth)) + pre + kids().trim().replace(/\n/g, '\n' + '  '.repeat(depth + 1)) + '\n'; }
    case 'a': { const t = kids().trim(), h = n.getAttribute('href') || '';
      if (!h || h === t || !/^https?:/.test(h)) return t; return t ? '[' + t + '](' + h + ')' : h; }
    case 'tr': return [...n.children].map(c => md(c).trim().replace(/\n+/g, ' ')).join(' | ') + '\n';
    case 'table': return kids() + '\n';
    default: return kids();
  }
}
function clean(s){ return s.replace(/^[ \t]+$/gm, '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim(); }

function extract(el){
  const mid = el.getAttribute('data-mid');
  const ctl = el.getAttribute('data-tid') === 'control-message-renderer';
  const content = el.querySelector('[data-message-content]');
  const w = el.closest('[data-testid="message-wrapper"]');
  let author = null, mri = null;
  if (!ctl) {
    const a = w && w.querySelector('[data-tid="message-author-name"]');
    author = a ? a.textContent.trim() : null;
    if (!author && w && w.firstElementChild) {   // grouped messages hide the header; the screen-reader line ends with "by <name>"
      const s = (w.firstElementChild.textContent || '').replace(NB, ' ');
      const i = s.lastIndexOf(' by ');
      if (i >= 0) author = s.slice(i + 4).replace(/, has an attachment\.?$/, '').replace(/\.$/, '').trim();
    }
    const m = w && w.querySelector('[data-person-mri]');
    mri = m ? m.getAttribute('data-person-mri') : null;
    if (!author) author = tb.lastAuthor;
    if (author) tb.lastAuthor = author;
  }
  let text = '';
  if (content) {
    if (ctl) {
      const first = content.firstElementChild || content;
      text = (first.innerText || '').replace(NB, ' ').replace(/\s+/g, ' ').trim().replace(/^\d{1,2}:\d{2}(\s?[AP]M)?\s*/i, '');
      const top = content.querySelector('[data-testid="meeting-recap-chiclet-top-container"]');
      if (top) text += ' - ' + [...top.querySelectorAll('span')].map(s => s.textContent.trim()).filter(Boolean).slice(0, 2).join(', ');
    } else {
      const clone = content.cloneNode(true);
      clone.querySelectorAll('[data-tid="file-attachment-grid"]').forEach(x => x.remove());
      text = clean(md(clone));
    }
  }
  const atts = [...el.querySelectorAll('[data-tid="file-attachment-grid"] [data-testid="content-card-custom-title"]')]
    .map(t => (t.firstElementChild ? t.firstElementChild.textContent : t.textContent).trim()).filter(Boolean);
  const reactions = [...(w || el).querySelectorAll('[data-tid="diverse-reaction-pill-button"]')].map(b => {
    const l = (b.getAttribute('aria-labelledby') || '').split(' ').map(i => document.getElementById(i)).filter(Boolean).map(x => x.textContent).join(' ');
    return (l || b.getAttribute('aria-label') || b.textContent || '').replace(NB, ' ').replace(/\s+/g, ' ').replace(/\.$/, '').trim(); }).filter(Boolean);
  const edited = !!(w && /^\s*Edited\s*$/m.test(w.innerText || ''));
  return {id: mid, ts_ms: +mid, type: ctl ? 'system' : 'message', author, author_id: mri, text, attachments: atts, reactions, edited};
}
tb.harvest = () => {
  const els = document.querySelectorAll('[data-tid="chat-pane-message"][data-mid], [data-tid="control-message-renderer"][data-mid]');
  let added = 0;
  els.forEach(el => { const it = extract(el); if (!tb.items.has(it.id)) added++; tb.items.set(it.id, it); });
  return added;
};
tb.viewport = () => document.querySelector('[data-tid="message-pane-list-viewport"]');
tb.oldest = () => { let m = Infinity; tb.items.forEach(i => { if (i.ts_ms < m) m = i.ts_ms; }); return m; };
// Scroll up until history older than fromMs is loaded, the top is reached, or maxMs elapsed.
tb.run = async (fromMs, maxMs) => {
  const vp = tb.viewport(); if (!vp) return {done: true, reason: 'no-viewport'};
  const t0 = Date.now(); let stale = 0;
  tb.harvest();
  while (Date.now() - t0 < maxMs) {
    const before = tb.items.size;
    vp.scrollTop = Math.max(0, vp.scrollTop - vp.clientHeight * 0.8);
    await sleep(350); tb.harvest();
    if (fromMs && tb.oldest() < fromMs) return {done: true, reason: 'from', count: tb.items.size};
    if (vp.scrollTop === 0) {
      await sleep(1500); tb.harvest();
      if (tb.items.size === before) { if (++stale >= 4) return {done: true, reason: 'top', count: tb.items.size}; } else stale = 0;
    } else stale = 0;
  }
  return {done: false, count: tb.items.size, oldest: tb.oldest()};
};
tb.slice = (i, n) => [...tb.items.values()].slice(i, i + n);
// Bring the attachment grid of a message into the DOM (the list is virtualised) and centre it.
tb.find = async mid => {
  const vp = tb.viewport(); const id = 'attachments-' + mid;
  let el = document.getElementById(id);
  if (!el) { vp.scrollTop = 0; await sleep(500);
    for (let i = 0; i < 600 && !(el = document.getElementById(id)); i++) {
      const b = vp.scrollTop; vp.scrollTop += vp.clientHeight * 0.7; await sleep(250); if (vp.scrollTop === b) break; } }
  if (!el) return false;
  el.scrollIntoView({block: 'center'}); await sleep(400); return true;
};
tb.download = async (mid, idx) => {
  const g = document.getElementById('attachments-' + mid); if (!g) return 'no-grid';
  const btn = g.querySelectorAll('[data-testid="overflow-button"]')[idx]; if (!btn) return 'no-button';
  btn.click(); await sleep(500);
  const mi = [...document.querySelectorAll('[role="menuitem"]')].find(e => /^(Download|Скачать)$/i.test(e.textContent.trim()));
  if (!mi) { document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); btn.blur(); return 'no-download-item'; }
  mi.click(); return 'ok';
};
return 'installed';
})()
"""

PAGE_INFO = r"""
({
  url: location.href, host: location.hostname,
  title: (document.querySelector('[data-tid="chat-title"]') || {}).textContent || null,
  app: !!document.querySelector('[data-tid="app-bar-wrapper"]'),
  viewport: !!document.querySelector('[data-tid="message-pane-list-viewport"]'),
  launcher: !!([...document.querySelectorAll('button')].find(b => /web app instead/i.test(b.textContent))),
  msgs: document.querySelectorAll('[data-tid="chat-pane-message"], [data-tid="control-message-renderer"]').length,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone, off: -new Date().getTimezoneOffset()
})
"""

CLICK_LAUNCHER = r"""
(()=>{const b=[...document.querySelectorAll('button')].find(b=>/web app instead/i.test(b.textContent)); if(b){b.click();return true} return false})()
"""

# --- meetings -------------------------------------------------------------------------------------

OPEN_RECAP = r"""
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const isTab=(e,n)=>{const t=(e.textContent||'').replace(/\s+/g,'').toLowerCase();n=n.toLowerCase();return t===n||t===n+n;};
  const isRecap=e=>isTab(e,'Recap');
  let t=[...document.querySelectorAll('[role="tab"]')].find(isRecap);
  if(!t){ const o=document.querySelector('[data-tid="tab-overflow-button"]'); if(!o) return 'no-recap-tab'; o.click(); await sleep(600);
    t=[...document.querySelectorAll('[role="menuitem"]')].find(isRecap); }
  if(!t) return 'no-recap-tab';
  t.click(); return 'ok';
})()
"""

RECAP_OPTIONS = r"""
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  let c=null; for(let i=0;i<40&&!(c=document.querySelector('[data-testid="intelligent-recap-instance-select-dropdown"]'));i++) await sleep(500);
  if(!c) return {error:'no-selector'};
  c.click(); await sleep(600);
  const opts=[...document.querySelectorAll('[role="option"]')].map(o=>o.textContent.replace(/ /g,' ').replace(/\s+/g,' ').trim());
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); await sleep(300);
  return {options:opts, current:(c.textContent||'').replace(/\s+/g,' ').trim()};
})()
"""

SELECT_RECAP_OPTION = r"""
(async(i)=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const c=document.querySelector('[data-testid="intelligent-recap-instance-select-dropdown"]'); if(!c) return 'no-selector';
  c.click(); await sleep(600);
  const o=[...document.querySelectorAll('[role="option"]')][i]; if(!o) return 'no-option';
  o.click(); await sleep(800); return 'ok';
})(%d)
"""

OPEN_TRANSCRIPT_TAB = r"""
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const isTab=(e,n)=>{const t=(e.textContent||'').replace(/\s+/g,'').toLowerCase();n=n.toLowerCase();return t===n||t===n+n;};
  let t=null; for(let i=0;i<20&&!(t=[...document.querySelectorAll('[role="tab"]')].find(e=>isTab(e,'Transcript')));i++) await sleep(500);
  if(!t) return 'no-transcript-tab';
  if(t.getAttribute('aria-selected')!=='true') t.click(); else { const o=[...document.querySelectorAll('[role="tab"]')].find(e=>isTab(e,'Notes')); if(o){o.click(); await sleep(500); t.click();} }
  return 'ok';
})()
"""

OPEN_CHAT_TAB = r"""
(()=>{const t=[...document.querySelectorAll('[role="tab"]')].find(e=>{const x=(e.textContent||'').replace(/\s+/g,'').toLowerCase();return x==='chat'||x==='chatchat'}); if(t){t.click();return true} return false})()
"""

# Runs INSIDE the transcript iframe (cross-origin SharePoint page), through its own CDP target.
TRANSCRIPT_PROBE = r"""
(()=>{const sc=document.getElementById('scrollToTargetTargetedFocusZone');
 const n=sc&&sc.querySelector('[aria-setsize]'); return JSON.stringify({zone:!!sc, size:n?+n.getAttribute('aria-setsize'):0,
  empty:/no transcript|not available|isn.t available/i.test(document.body.innerText||'')})})()
"""

TRANSCRIPT_COLLECT = r"""
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const sc=document.getElementById('scrollToTargetTargetedFocusZone'); if(!sc) return JSON.stringify({error:'no-zone'});
  const size=()=>+((sc.querySelector('[aria-setsize]')||{getAttribute:()=>0}).getAttribute('aria-setsize'))||0;
  const got=new Map();
  const grab=()=>{ for(const e of sc.querySelectorAll('[role="listitem"][aria-posinset]')){
      const p=+e.getAttribute('aria-posinset'); const g=e.closest('[role="group"]');
      if(!got.has(p)||!got.get(p).text) got.set(p,{p,label:g?(g.getAttribute('aria-label')||''):'',text:(e.textContent||'').trim()}); } };
  sc.scrollTop=0; await sleep(700);
  let idle=0, last=-1;
  for(let i=0;i<4000;i++){
    grab();
    const total=size(); if(total && got.size>=total) break;
    const b=sc.scrollTop; sc.scrollTop+=Math.max(100,sc.clientHeight*0.6); await sleep(300);
    // a scroll that does not move, and no new entries, means the end (or content still laying out)
    if(sc.scrollTop===b && got.size===last){ if(++idle>=8) break; await sleep(500); } else idle=0;
    last=got.size;
  }
  grab();
  return JSON.stringify({size:size(), entries:[...got.values()].sort((a,b)=>a.p-b.p)});
})()
"""
