// Table search + click-to-sort, drag/drop upload zones, status badges. No dependencies.
document.querySelectorAll("input[data-filter]").forEach(inp=>{
  const t=document.querySelector(inp.dataset.filter);
  inp.addEventListener("input",()=>{const q=inp.value.toLowerCase();
    t.querySelectorAll("tbody tr").forEach(r=>r.hidden=q&&!r.textContent.toLowerCase().includes(q))})});
document.querySelectorAll("table.sortable").forEach(t=>t.querySelectorAll("th").forEach((th,i)=>th.addEventListener("click",()=>{
  const rows=[...t.tBodies[0].rows],dir=th.dataset.d=th.dataset.d==="a"?"d":"a";
  const v=r=>{const x=r.cells[i].textContent.trim().replace(/[%,]/g,"");return isNaN(x)||x===""?x.toLowerCase():+x};
  rows.sort((a,b)=>(v(a)>v(b)?1:-1)*(dir==="a"?1:-1)).forEach(r=>t.tBodies[0].appendChild(r))})));
document.querySelectorAll(".drop").forEach(z=>{
  ["dragenter","dragover"].forEach(e=>z.addEventListener(e,ev=>{ev.preventDefault();z.classList.add("over")}));
  ["dragleave","drop"].forEach(e=>z.addEventListener(e,()=>z.classList.remove("over")));
  z.addEventListener("drop",ev=>{ev.preventDefault();const i=z.querySelector("input[type=file]");i.files=ev.dataTransfer.files;
    z.querySelector(".fn").textContent=[...i.files].map(f=>f.name).join(", ")});
  z.querySelector("input[type=file]").addEventListener("change",e=>z.querySelector(".fn").textContent=[...e.target.files].map(f=>f.name).join(", "))});
fetch("/update/status").then(r=>r.json()).then(s=>{
  if(!s.ok||!s.behind)return;const d=document.getElementById("upd");d.hidden=false;
  d.innerHTML="Update available ("+s.behind+" new): "+s.changes.slice(0,3).join(" · ")+
   (s.dirty?" — local changes block auto-update":' <button class="sm" id="ub">Update &amp; restart</button>');
  const b=document.getElementById("ub");
  if(b)b.onclick=()=>{b.disabled=true;b.textContent="Updating…";fetch("/update/apply",{method:"POST"}).then(r=>r.json())
   .then(x=>x.ok?setTimeout(()=>location.reload(),4000):d.textContent="Update failed: "+x.error)}}).catch(()=>{});
const ai=document.getElementById("ai-state");
if(ai)fetch("/ai/status").then(r=>r.json()).then(s=>{ai.textContent=s.ok?"AI ready":"AI offline";ai.title=s.message;ai.className="pill "+(s.ok?"ok":"warn")});
document.querySelectorAll(".ex").forEach(b=>b.onclick=()=>{const o=b.parentElement.querySelector(".exo");o.textContent="Thinking…";
  fetch("/explain/"+encodeURIComponent(b.dataset.p),{method:"POST"}).then(r=>r.json()).then(x=>o.textContent=(x.ok?"AI: ":"")+x.text)});
