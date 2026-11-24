// Sephora SKU-list reader for Nick's old MacBook Chrome (placeholders __TOKEN__, __LIMIT__, __SOURCE__) (signed-out "Lead search" profile), run in a sephora.com tab with
// javascript_tool. Reads the unread product pages from /api/store-skus/todo (closest to profitable first), one page every
// 7 s, never two at once: #linkStore -> page.product.currentSku plus every *ChildSkus list (regularChildSkus AND
// onSaleChildSkus). Each SKU's finish refinement (Matte, Satin...) is added to its description. Uploads to
// /api/store-skus/upload in tens. __TOKEN__ = a scoped store_skus upload token (never the engine key).
// Nov 24, 2026 (Nick): each item also carries Sephora's other name fields and image alt text (shade names some items lack),
// and for products sold in colours/shades the reader fetches each item's swatch (and the product shot when the product has
// at most 8 items) from the same page visit and uploads them to /api/store-skus/images, so the match check can compare
// shades without loading sephora.com from the cloud. Pauses 15 min after
// 3 pages in a row without product data, stops after 8 pauses. Never clicks or submits anything. Progress in window.sk.
window.sk={stop:false,done:0,saved:0,blocked:0,missing:0,pauses:0,errors:[],finished:false,startedAt:new Date().toISOString()};
const H='https://order-ledger-3mt6.onrender.com',T='__TOKEN__';
// __SOURCE__: 'unread' = products with no item list yet (/api/store-skus/todo); 'recheck' = the daily job's list of products
// held back as out of stock (Hub doc reader/sephora_todo via /api/reader/todo?list=sephora), re-read so restocks are seen.
const SRC='__SOURCE__';
const todo=(SRC==='recheck'||SRC==='images')?await fetch(H+'/api/reader/todo?list='+(SRC==='images'?'sephora_images':'sephora'),{headers:{'X-Upload-Token':T}}).then(r=>r.json()).then(d=>({items:(d.items||[]).slice(0,__LIMIT__),left:(d.items||[]).length}))
 :await fetch(H+'/api/store-skus/todo?limit=__LIMIT__',{headers:{'X-Upload-Token':T}}).then(r=>r.json());
const L=todo.items;window.sk.total=L.length;window.sk.left=todo.left;
const urls=(o,out=[],dep=0)=>{if(!o||dep>3)return out;if(typeof o==='string'){if(o.includes('/productimages/')&&out.length<12)out.push(o.replace(/^https?:\/\/www\.sephora\.com/,''));return out;}
if(typeof o==='object')for(const v of Object.values(o))urls(v,out,dep+1);return out;};
const extra=s=>{const u=urls([s.smallImage,s.skuImages,s.swatchImage,s.image,s.images]);const alt=[s.imageAltText,(s.skuImages||{}).imageAltText,s.smallImageAltText,s.swatchAltText].find(x=>typeof x==='string'&&x.trim())||'';
return{names:[s.variationValue,s.variationName,s.displayName,s.skuName,s.colorName,s.shadeName].filter(x=>typeof x==='string'&&x.trim()),alt,
sw:u.find(x=>/\+sw|swatch/i.test(x))||'',img:u.find(x=>/main|zoom|hero/i.test(x))||u.find(x=>!/\+sw|swatch/i.test(x))||''};};
const row=s=>{const f=((s.skuRefinements||[]).find(x=>x&&x.key==='finish')||{values:[]}).values.join('/');const d=[s.variationDesc,f].filter(Boolean).join(' · ');
return[String(s.skuId),s.size||'',s.variationValue||'',d,(s.salePrice||s.listPrice||'').toString(),!!s.isOutOfStock,extra(s)];};
const b64=async(u)=>{try{const r=await fetch(u,{credentials:'include'});if(!r.ok)return null;const b=await r.blob();if(!b.type.startsWith('image/')||b.size>380000)return null;
return await new Promise(ok=>{const fr=new FileReader();fr.onload=()=>ok({ct:b.type,b64:String(fr.result).split(',')[1]});fr.onerror=()=>ok(null);fr.readAsDataURL(b);});}catch(e){return null;}};
let imgBuf=[];const flushImgs=async()=>{while(imgBuf.length){const part=imgBuf.splice(0,30);try{const r=await fetch(H+'/api/store-skus/images',{method:'POST',headers:{'Content-Type':'application/json','X-Upload-Token':T},body:JSON.stringify({images:part})});
if(r.status===200){window.sk.images=(window.sk.images||0)+part.length;}else window.sk.errors.push('images '+r.status);}catch(e){window.sk.errors.push('images '+String(e).slice(0,60));}}};
const grabImages=async(x)=>{if(!/colou?r|shade/i.test(x.vt||''))return;const many=x.skus.length>8;
for(const r of x.skus.slice(0,60)){const e=r[6]||{};const sw=e.sw||('/productimages/sku/s'+r[0]+'+sw.jpg');const got=await b64(sw);if(got)imgBuf.push({sku_id:r[0],kind:'sw',...got});
if(!many){const mu=(e.img||('/productimages/sku/s'+r[0]+'-main-zoom.jpg')).split('?')[0]+'?imwidth=315';const g2=await b64(mu);if(g2)imgBuf.push({sku_id:r[0],kind:'main',...g2});}
await new Promise(z=>setTimeout(z,250));}
if(imgBuf.length>=30)await flushImgs();};
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
try{const x=await read(path);if(x.blocked){window.sk.blocked++;streak++;}else if(x.missing){window.sk.missing++;streak=0;}else{streak=0;buf[pid]=x;await grabImages(x);}}
catch(e){window.sk.errors.push(pid+': '+String(e).slice(0,60));}
window.sk.done++;
if(streak>=3){if(window.sk.pauses>=8){window.sk.errors.push('stopped: Sephora kept blocking');break;}window.sk.pauses++;streak=0;await flush();
await new Promise(z=>setTimeout(z,15*60000));i-=3;window.sk.done-=3;continue;}
if(Object.keys(buf).length>=10)await flush();await new Promise(z=>setTimeout(z,7000));}
await flush();await flushImgs();window.sk.finished=true;window.sk.endedAt=new Date().toISOString();})();
'started '+L.length+' of '+todo.left
