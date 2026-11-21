// Amazon page reader for Nick's old MacBook Chrome (signed-out "Lead search" profile), run in an amazon.com tab with javascript_tool.
// Reads the ASINs in Hub doc reader/amazon_todo (4 s apart; pauses 15 min after 3 blocked pages, stops after 8 pauses) and uploads
// title, selected variation, price, bullets and Product details. __TOKEN__ = a scoped amazon_pages upload token (never the engine key).
// Never clicks or submits anything; progress in window.az (done, saved, blocked, errors, finished).
window.az={stop:false,done:0,saved:0,blocked:0,pauses:0,errors:[],finished:false,startedAt:new Date().toISOString()};
const L=(await fetch('https://order-ledger-3mt6.onrender.com/api/reader/todo?list=amazon',{headers:{'X-Upload-Token':'__TOKEN__'}}).then(r=>r.json())).items;window.az.total=L.length;
const read=async(a)=>{const r=await fetch('/dp/'+a+'?th=1&psc=1',{credentials:'include'});const h=await r.text();
const d=new DOMParser().parseFromString(h,'text/html');const t=s=>(d.querySelector(s)?.textContent||'').replace(/\s+/g,' ').trim();
if(!d.querySelector('#productTitle'))return{blocked:true};const rows={};
d.querySelectorAll('#productOverview_feature_div tr, #prodDetails tr, #detailBullets_feature_div li, #technicalSpecifications_section_1 tr').forEach(tr=>{
const k=(tr.querySelector('th,td.a-span3,span.a-text-bold')?.textContent||'').replace(/[\s\u200e\u200f:]+/g,' ').trim();
let v=(tr.querySelector('td:last-child, span:not(.a-text-bold)')?.textContent||'').replace(/\s+/g,' ').replace(/^.*?[:\u200e]\s*\u200e?\s*/,'').trim();
if(k&&v&&k.length<40&&!/Customer Reviews|Best Sellers Rank|UNSPSC/i.test(k))rows[k]=v.slice(0,150);});
const sel=[...d.querySelectorAll('#twister .selection, [id^=inline-twister-expanded-dimension-text]')].map(e=>e.textContent.replace(/\s+/g,' ').trim()).filter(Boolean);
return{title:t('#productTitle'),selected:[...new Set(sel)].slice(0,5),price:t('#corePrice_feature_div .a-offscreen')||t('.priceToPay .a-offscreen'),
bullets:[...d.querySelectorAll('#feature-bullets li')].map(e=>e.textContent.replace(/\s+/g,' ').trim()).slice(0,8),details:rows};};
(async()=>{let buf={},streak=0;
const flush=async()=>{if(!Object.keys(buf).length)return;try{const r=await fetch('https://order-ledger-3mt6.onrender.com/api/amazon-pages/upload',{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':'__TOKEN__'},body:JSON.stringify({pages:buf})});if(r.status===200){window.az.saved+=Object.keys(buf).length;buf={};}else window.az.errors.push('upload '+r.status);}catch(e){window.az.errors.push('upload '+String(e).slice(0,60));}};
for(let i=0;i<L.length;i++){if(window.az.stop)break;const a=L[i];try{const x=await read(a);if(x.blocked){window.az.blocked++;streak++;}else{streak=0;buf[a]=x;}}catch(e){window.az.errors.push(a+': '+String(e).slice(0,60));}
window.az.done++;
if(streak>=3){if(window.az.pauses>=8){window.az.errors.push('stopped: Amazon kept blocking');break;}window.az.pauses++;streak=0;await flush();await new Promise(z=>setTimeout(z,15*60000));i-=3;window.az.done-=3;continue;}
if(Object.keys(buf).length>=10)await flush();await new Promise(z=>setTimeout(z,4000));}
await flush();window.az.finished=true;window.az.endedAt=new Date().toISOString();})();
'started '+L.length