const q=s=>document.querySelector(s);
const fmt=(v,d=0)=>v==null?"–":Number(v).toFixed(d);
const hours=m=>m==null?"–":(Number(m)/60).toFixed(1);
async function get(path){const r=await fetch(path);if(!r.ok)throw new Error(path+" "+r.status);return r.json()}
function drawTrend(days){
  const svg=q("#trend"); const pts=days.filter(x=>x.data?.recovery!=null).slice(-30);
  svg.innerHTML='<line class="axis" x1="0" y1="200" x2="900" y2="200"/>';
  if(!pts.length)return;
  const p=pts.map((x,i)=>{const xx=pts.length===1?450:i*(900/(pts.length-1));const yy=200-(Math.max(0,Math.min(100,x.data.recovery))*1.8);return [xx,yy]});
  svg.innerHTML+=`<polyline class="trendline" points="${p.map(x=>x.join(",")).join(" ")}"/>`+p.map(x=>`<circle class="dot" cx="${x[0]}" cy="${x[1]}" r="4"/>`).join("");
}
function row(title,sub,right){return `<div class="row"><div><strong>${title}</strong><br><span>${sub}</span></div><strong>${right}</strong></div>`}
function latestMetric(rows,key){
  const hits=(rows||[]).filter(x=>x.key?.key===key && x.data?.value!=null);
  return hits.length?hits[hits.length-1].data.value:null;
}
async function load(){
  try{
    const [s,sleep,work,apple]=await Promise.all([
      get("/api/dashboard/summary"),get("/api/dashboard/sleep"),get("/api/dashboard/workouts"),get("/api/dashboard/apple")
    ]);
    const d=s.latest?.data||{};
    q("#recovery").textContent=fmt(d.recovery);
    q("#sleep").textContent=hours(d.totalSleepMin);
    q("#hrv").textContent=fmt(d.avgHrv);
    q("#rhr").textContent=fmt(d.restingHr);
    q("#strain").textContent=fmt(d.strain,1);
    q("#sync").textContent=s.lastSync?"Last sync "+new Date(s.lastSync*1000).toLocaleString():"No data yet";
    drawTrend(s.daily);
    const ad=apple.latest?.data||{};
    q("#appleSteps").textContent=fmt(ad.steps);
    q("#appleActive").textContent=fmt(ad.activeKcal);
    q("#appleVo2").textContent=fmt(ad.vo2max,1);
    q("#appleWeight").textContent=fmt(ad.weightKg,1);
    q("#appleBodyFat").textContent=fmt(latestMetric(apple.metrics,"body_fat"),1);
    q("#appleBmi").textContent=fmt(latestMetric(apple.metrics,"bmi"),1);
    q("#counts").innerHTML=Object.entries(s.counts||{}).sort().map(([k,v])=>`<span class="pill">${k}: ${v}</span>`).join("")||'<span class="pill">Waiting for NoopZone</span>';
    q("#sleepList").innerHTML=sleep.slice(0,6).map(x=>{
      const st=x.key.startTs?new Date(x.key.startTs*1000).toLocaleDateString():"Sleep";
      const mins=x.data.endTs&&x.key.startTs?(x.data.endTs-x.key.startTs)/60:null;
      return row(st,`HRV ${fmt(x.data.avgHrv)} · RHR ${fmt(x.data.restingHr)}`,mins?hours(mins)+" h":"–");
    }).join("")||'<span class="pill">No sleep sessions yet</span>';
    q("#workoutList").innerHTML=work.slice(0,6).map(x=>{
      const st=x.key.startTs?new Date(x.key.startTs*1000).toLocaleDateString():"Workout";
      return row(x.key.sport||"Workout",st+` · avg HR ${fmt(x.data.avgHr)}`,x.data.strain!=null?"Strain "+fmt(x.data.strain,1):"");
    }).join("")||'<span class="pill">No workouts yet</span>';
  }catch(e){q("#sync").textContent="Dashboard API unavailable"}
}
load(); setInterval(load,30000);

async function importHealth(){
  const file=q("#healthFile")?.files?.[0];
  const token=q("#healthToken")?.value?.trim()||"";
  const status=q("#healthImportStatus");
  const button=q("#healthImport");
  if(!file){status.textContent="Choose export.zip or export.xml first.";status.className="importstatus bad";return}
  if(!token){status.textContent="Enter the server token.";status.className="importstatus bad";return}
  const form=new FormData(); form.append("file",file,file.name);
  button.disabled=true; status.textContent="Importing… large Apple Health exports can take a while.";status.className="importstatus";
  try{
    const r=await fetch("/api/healthkit/import",{method:"POST",headers:{Authorization:"Bearer "+token},body:form});
    const body=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(body.detail||("HTTP "+r.status));
    status.textContent=`Imported ${body.days||0} days, ${body.sleepSessions||0} sleep sessions and ${body.workoutCount||0} workouts.`;
    status.className="importstatus ok";
    q("#healthToken").value="";
    await load();
  }catch(e){
    status.textContent="Import failed: "+e.message;
    status.className="importstatus bad";
  }finally{button.disabled=false}
}
q("#healthImport")?.addEventListener("click",importHealth);
