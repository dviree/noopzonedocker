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
async function load(){
  try{
    const [s,sleep,work]=await Promise.all([get("/api/dashboard/summary"),get("/api/dashboard/sleep"),get("/api/dashboard/workouts")]);
    const d=s.latest?.data||{};
    q("#recovery").textContent=fmt(d.recovery);
    q("#sleep").textContent=hours(d.totalSleepMin);
    q("#hrv").textContent=fmt(d.avgHrv);
    q("#rhr").textContent=fmt(d.restingHr);
    q("#strain").textContent=fmt(d.strain,1);
    q("#sync").textContent=s.lastSync?"Last sync "+new Date(s.lastSync*1000).toLocaleString():"No data yet";
    drawTrend(s.daily);
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
