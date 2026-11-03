const state={indicador:'sem_mov',data:null,request:0};
const $=s=>document.querySelector(s); const $$=s=>[...document.querySelectorAll(s)];
const fmt=n=>n===null||n===undefined?'—':Number(n).toLocaleString('pt-BR');
const fmtDate=s=>s?new Date(s+'T12:00:00').toLocaleDateString('pt-BR'):'—';
function varHtml(v){if(v===null||v===undefined)return {text:'—',cls:'neutral'}; const cls=v<0?'good':v>0?'bad':'neutral'; const arrow=v<0?'▼':v>0?'▲':'•'; return {text:`${arrow} ${Math.abs(v).toLocaleString('pt-BR',{minimumFractionDigits:1,maximumFractionDigits:1})}%`,cls};}
function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800)}
function q(){return new URLSearchParams({indicador:state.indicador,data:$('#fData').value,uf:$('#fUf').value,rm:$('#fRm').value,sc:$('#fSc').value,base:$('#fBase').value})}
async function json(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function setSelect(el,items,mapper=x=>({v:x,t:x}),keep=true){const old=keep?el.value:''; const first=el.options[0]?.textContent||'Todos';el.innerHTML=`<option value="">${first}</option>`;items.forEach(x=>{const m=mapper(x),o=document.createElement('option');o.value=m.v;o.textContent=m.t;el.appendChild(o)});if([...el.options].some(o=>o.value===old))el.value=old}
function setDateSelect(items){const el=$('#fData'),old=el.value;el.innerHTML='';items.forEach(x=>{const o=document.createElement('option');o.value=x;o.textContent=fmtDate(x);el.appendChild(o)});if(old&&items.includes(old))el.value=old;else if(items.length)el.value=items[0];state.data=el.value||null}
async function loadFilters(ind=state.indicador){const f=await json('/api/filters?indicador='+ind);if(ind!==state.indicador)return;setDateSelect(f.datas||[]);setSelect($('#fUf'),f.uf);setSelect($('#fRm'),f.rm);setSelect($('#fSc'),f.sc);setSelect($('#fBase'),f.bases,x=>({v:x.codigo,t:`${x.nome} (${x.codigo})`}));}
function meta(el,m,cur){el.className='meta';if(m===null||m===undefined){el.textContent='Meta a configurar';return}el.textContent=`Meta ≤ ${fmt(m)}`;if(cur!==null){el.classList.add(cur<=m?'good':'bad')}}
function renderKpis(d){const atual=fmtDate(d.datas.atual),anterior=fmtDate(d.datas.anterior);$('#refDate').textContent=atual;state.data=d.datas.atual||null;if(d.datas.atual&&$('#fData').value!==d.datas.atual)$('#fData').value=d.datas.atual;['3','10'].forEach(c=>{const m=d.metrics['acima_'+c],v=varHtml(m.variacao);$('#prev'+c).textContent=fmt(m.anterior);$('#cur'+c).textContent=fmt(m.atual);$('#var'+c).textContent=v.text;$('#var'+c).className=v.cls;$('#lblPrev'+c).textContent=`7 dias antes • ${anterior}`;$('#lblCur'+c).textContent=`Data selecionada • ${atual}`;meta($('#meta'+c),m.meta,m.atual)});$('#missingD1').classList.add('hidden')}
function lineChart(el,data){
 if(!data.length){el.innerHTML='<div class="empty">Sem histórico carregado para este indicador.</div>';return}
 const W=900,H=280,L=64,R=25,T=20,B=38;
 const max=Math.max(...data.flatMap(x=>[x.acima_3,x.acima_10]),1)*1.1;
 const start=new Date(data[0].data+'T12:00:00').getTime(),end=new Date(data[data.length-1].data+'T12:00:00').getTime();
 const x=d=>L+(W-L-R)*(end===start?.5:(new Date(d.data+'T12:00:00').getTime()-start)/(end-start));
 const y=v=>H-B-(H-T-B)*v/max;
 let svg='';
 for(let i=0;i<5;i++){const v=max*i/4,yy=y(v);svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#e4e7ec"/><text x="${L-9}" y="${yy+4}" text-anchor="end" font-size="11" fill="#667085">${fmt(Math.round(v))}</text>`}
 for(const [key,color] of [['acima_3','#d71920'],['acima_10','#3978c6']]){
 svg+=`<polyline points="${data.map(d=>x(d)+','+y(d[key])).join(' ')}" fill="none" stroke="${color}" stroke-width="3"/>`;
 data.forEach(d=>svg+=`<circle cx="${x(d)}" cy="${y(d[key])}" r="4" fill="${color}" stroke="#fff" stroke-width="2"><title>${fmtDate(d.data)} • ${key==='acima_3'?'>3 dias':'>10 dias'}: ${fmt(d[key])}</title></circle>`);
 }
 data.forEach((d,i)=>{if(i%Math.max(1,Math.ceil(data.length/7))===0||i===data.length-1)svg+=`<text x="${x(d)}" y="${H-12}" text-anchor="middle" font-size="11" fill="#667085">${fmtDate(d.data).slice(0,5)}</text>`});
 el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Evolução diária em quantidade de pedidos">${svg}</svg>`;
}
function bars(el,data,labelKey='label',valueKey='qtd'){if(!data.length){el.innerHTML='<div class="empty">Sem dados na data selecionada.</div>';return}const max=Math.max(...data.map(x=>x[valueKey]),1);el.innerHTML=data.map(x=>`<div class="bar-row"><div class="bar-label" title="${esc(x[labelKey])}">${esc(x[labelKey])}</div><div class="bar-track"><div class="bar-fill" style="width:${x[valueKey]/max*100}%"></div></div><div class="bar-value">${fmt(x[valueKey])}</div></div>`).join('')}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderTables(d){const b=$('#basesBody');if(!d.top_bases.length)b.innerHTML='<tr><td colspan="6" class="empty">Sem dados na data selecionada.</td></tr>';else b.innerHTML=d.top_bases.map((x,i)=>{const v=varHtml(x.variacao);return `<tr><td class="rank">${i+1}</td><td>${esc(x.base_nome||x.base_codigo)}</td><td>${esc(x.sc||'—')}</td><td class="num">${fmt(x.acima_3)}</td><td>${fmt(x.acima_10)}</td><td class="${v.cls}">${v.text}</td></tr>`}).join('');const s=$('#scBody');if(!d.top_sc.length)s.innerHTML='<tr><td colspan="4" class="empty">Sem dados na data selecionada.</td></tr>';else s.innerHTML=d.top_sc.map((x,i)=>`<tr><td class="rank">${i+1}</td><td>${esc(x.sc)}</td><td class="num">${fmt(x.acima_3)}</td><td>${fmt(x.acima_10)}</td></tr>`).join('');const o=$('#offendersBody');if(!d.top_bases.length)o.innerHTML='<tr><td colspan="7" class="empty">Sem dados na data selecionada para tratativa.</td></tr>';else o.innerHTML=d.top_bases.map(x=>`<tr data-code="${esc(x.base_codigo)}"><td><strong>${esc(x.base_nome||x.base_codigo)}</strong><br><small>${esc(x.sc||'')}</small></td><td class="num">${fmt(x.acima_3)}</td><td><input class="motivo" value="${esc(x.motivo)}"></td><td><input class="plano" value="${esc(x.plano_acao)}"></td><td><input class="resp" value="${esc(x.responsavel)}"></td><td><input class="prazo" placeholder="dd/mm" value="${esc(x.prazo)}"></td><td><button class="save-btn">Salvar</button></td></tr>`).join('');$$('.save-btn').forEach(btn=>btn.onclick=savePlan)}
function planKey(ind,code){return 'jt-plano-csv:'+ind+':'+code}
function restorePlans(d){d.top_bases.forEach(x=>{try{const p=JSON.parse(localStorage.getItem(planKey(d.indicador,x.base_codigo))||'null');if(p){x.motivo=p.motivo_manual;x.plano_acao=p.plano_acao;x.responsavel=p.responsavel;x.prazo=p.prazo}}catch(e){}})}
function savePlan(e){const tr=e.target.closest('tr');const body={indicador:state.indicador,base_codigo:tr.dataset.code,motivo_manual:tr.querySelector('.motivo').value,plano_acao:tr.querySelector('.plano').value,responsavel:tr.querySelector('.resp').value,prazo:tr.querySelector('.prazo').value};try{localStorage.setItem(planKey(body.indicador,body.base_codigo),JSON.stringify(body));toast('Salvo neste navegador')}catch(e){toast('Não foi possível salvar neste navegador')}}
async function loadDashboard(){
 const request=++state.request;
 document.body.setAttribute('aria-busy','true');$('#loadError').classList.add('hidden');
 try{
 const d=await json('/api/dashboard?'+q());
 if(request!==state.request)return;
 const fs=d.fonte||{};$('#sourceStatus').textContent=fs.fonte ? fs.fonte+(fs.sincronizado_em?' • Sincronizado: '+new Date(fs.sincronizado_em).toLocaleString('pt-BR'):'') : '';$('#sourceWarning').textContent=fs.aviso||'';$('#sourceWarning').classList.toggle('hidden',!fs.aviso);restorePlans(d);state.data=d;renderKpis(d);lineChart($('#evolutionChart'),d.evolucao);bars($('#agingBars'),d.aging);bars($('#motivosBars'),d.motivos,'motivo','qtd');renderTables(d);
 $('#missingD8').classList.toggle('hidden',d.datas.tem_anterior);
 $('#missingD8').textContent='Sem base de comparação em '+fmtDate(d.datas.anterior)+' (7 dias antes). A variação fica indisponível; ausência de dados não significa zero.';
 }catch(e){if(request===state.request){$('#loadError').textContent='Não foi possível carregar este indicador. Verifique a conexão e tente novamente.';$('#loadError').classList.remove('hidden');}}
 finally{if(request===state.request)document.body.setAttribute('aria-busy','false')}
}
async function switchTab(ind){
 ++state.request;state.indicador=ind;state.data=null;
 $$('.tab').forEach(x=>{x.classList.toggle('active',x.dataset.indicador===ind);x.setAttribute('aria-pressed',x.dataset.indicador===ind)});
 const name=ind==='sem_mov'?'Sem Movimentação':'Retidos';
 $('#indicatorTitle').textContent=name;$('#evolutionTitle').textContent='Evolução diária • '+name;
 $('#indicatorDesc').textContent=ind==='sem_mov'?'Pedidos sem movimentação • Metas próprias • Quanto menor, melhor':'Pedidos retidos • Metas próprias • Quanto menor, melhor';
 ['3','10'].forEach(c=>{['prev','cur','var'].forEach(k=>$('#'+k+c).textContent='—');$('#meta'+c).textContent='Meta —'});
 ['evolutionChart','agingBars','motivosBars','basesBody','scBody','offendersBody'].forEach(id=>$('#'+id).replaceChildren());
 ['#fUf','#fRm','#fSc','#fBase'].forEach(s=>$(s).value='');
 document.body.setAttribute('aria-busy','true');
 try{await loadFilters(ind);if(state.indicador===ind)await loadDashboard()}
 catch(e){if(state.indicador===ind){document.body.setAttribute('aria-busy','false');toast('Erro ao carregar filtros. Tente novamente.');}}
}
$$('.tab').forEach(t=>t.onclick=()=>switchTab(t.dataset.indicador));
['#fData','#fUf','#fRm','#fSc','#fBase'].forEach(s=>$(s).onchange=()=>{state.data=$('#fData').value||null;loadDashboard()});
$('#btnClear').onclick=()=>{['#fUf','#fRm','#fSc','#fBase'].forEach(s=>$(s).value='');loadDashboard()};
$('#downloadBtn').onclick=()=>{window.location='/api/download?'+q()+'&corte=3'};
switchTab('sem_mov');
