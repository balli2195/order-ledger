// Sephora SKU-list reader for Nick's old MacBook Chrome (signed-out "Lead search" profile), run in a sephora.com tab with
// javascript_tool. Reads the unread product pages from /api/store-skus/todo (closest to profitable first), one page every
// 7 s, never two at once: #linkStore -> page.product.currentSku plus every *ChildSkus list (regularChildSkus AND
// onSaleChildSkus). Each SKU's finish refinement (Matte, Satin...) is added to its description. Uploads to
// /api/store-skus/upload in tens. __TOKEN__ = a scoped store_skus upload token (never the engine key). Pauses 15 min after
// 3 pages in a row without product data, stops after 8 pauses. Never clicks or submits anything. Progress in window.sk.
window.sk={stop:false,done:0,saved:0,blocked:0,missing:0,pauses:0,errors:[],finished:false,startedAt:new Date().toISOString()};
const H='https://order-ledger-3mt6.onrender.com',T='__TOKEN__';
const todo=await fetch(H+'/api/store-skus/todo?limit=__LIMIT__',{headers:{'X-Upload-Token':T}}).then(r=>r.json());
const L=todo.items;window.sk.total=L.length;window.sk.left=todo.left;
const row=s=>{const f=((s.skuRefinements||[]).find(x=>x&&x.key==='finish')||{values:[]}).values.join('/');const d=[s.variationDesc,f].filter(Boolean).join(' · ');
return[String(s.skuId),s.size||'',s.variationValue||'',d,(s.salePrice||s.listPrice||'').toString(),!!s.isOutOfStock];};
const read=async(u)=>{const r=await fetch(u,{credentials:'include'});const h=await r.text();
const d=new DOMParser().parseFromString(h,'text/html');const ls=d.querySelector('#linkStore');if(!ls)return{blocked:true};
let p;try{p=JSON.parse(ls.textContent).page.product;}catch(e){return{blocked:true};}if(!p||!p.currentSku)return{missing:true};
const all=[...Object.keys(p).filter(k=>/ChildSkus$/.test(k)&&Array.isArray(p[k])).flatMap(k=>p[k]),p.currentSku];
const seen=new Set(),skus=[];for(const s of all){if(s&&s.skuId&&!seen.has(String(s.skuId))){seen.add(String(s.skuId));skus.push(row(s));}}
return{vt:p.variationType||p.currentSku.variationType||'',skus:skus.slice(0,400)};};
(async()=>{let buf={},streak=0;
const flush=async()=>{if(!Object.keys(buf).length)return;try{const r=await fetch(H+'/api/store-skus/upload',{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':T},body:JSON.stringify({products:buf})});
if(r.status===200){window.sk.saved+=Object.keys(buf).length;buf={};}else window.sk.errors.push('upload '+r.status);}catch(e){window.sk.errors.push('upload '+String(e).slice(0,60));}};
for(let i=0;i<L.length;i++){if(window.sk.stop)break;const[pid,path]=L[i].split('|');
try{const x=await read(path);if(x.blocked){window.sk.blocked++;streak++;}else if(x.missing){window.sk.missing++;streak=0;}else{streak=0;buf[pid]=x;}}
catch(e){window.sk.errors.push(pid+': '+String(e).slice(0,60));}
window.sk.done++;
if(streak>=3){if(window.sk.pauses>=8){window.sk.errors.push('stopped: Sephora kept blocking');break;}window.sk.pauses++;streak=0;await flush();
await new Promise(z=>setTimeout(z,15*60000));i-=3;window.sk.done-=3;continue;}
if(Object.keys(buf).length>=10)await flush();await new Promise(z=>setTimeout(z,7000));}
await flush();window.sk.finished=true;window.sk.endedAt=new Date().toISOString();})();
'started '+L.length+' of '+todo.left
