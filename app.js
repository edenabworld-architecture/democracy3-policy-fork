let bills=[];const grid=document.querySelector('#grid'),q=document.querySelector('#search'),dlg=document.querySelector('#dialog'),modal=document.querySelector('#modal');
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
fetch('data/bills.json').then(r=>r.json()).then(d=>{bills=d.bills;document.querySelector('#notice').textContent=d.notice;render()});
function render(){const x=q.value.toLowerCase();grid.innerHTML=bills.filter(b=>JSON.stringify(b).toLowerCase().includes(x)).map(b=>`<article class="card">
<span class="tag">${esc(b.status)}</span><h2>${esc(b.title)}</h2><div class="meta">${esc(b.committee)}</div><p>${esc(b.summary)}</p>
<div class="pills">${b.strengths.map(v=>`<span class="pill">장점: ${esc(v)}</span>`).join('')}</div>
<div class="pills">${b.risks.map(v=>`<span class="pill">위험: ${esc(v)}</span>`).join('')}</div>
<button onclick="openBill('${esc(b.id)}')">자세히 보기</button></article>`).join('')}
function openBill(id){const b=bills.find(v=>v.id===id);modal.innerHTML=`<button onclick="dlg.close()">닫기</button><h2>${esc(b.title)}</h2><p>${esc(b.summary)}</p>
<div class="section"><b>수혜자:</b> ${b.beneficiaries.map(esc).join(', ')}<br><b>비용 부담:</b> ${b.cost_bearers.map(esc).join(', ')}</div>
<div class="section"><h3>검토 지표</h3>${Object.entries(b.scores).map(([k,v])=>`<div class="score">${esc(k)}: ${v}/4</div>`).join('')}</div>
<div class="section"><h3>정책 포크</h3>${b.forks.map(f=>`<div class="fork"><b>${esc(f.title)}</b><p>${esc(f.body)}</p></div>`).join('')}</div>`;dlg.showModal()}
q.addEventListener('input',render);