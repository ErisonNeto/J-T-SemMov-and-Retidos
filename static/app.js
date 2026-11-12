const state={indicador:'sem_mov',request:0};
const $=s=>document.querySelector(s); const $$=s=>[...document.querySelectorAll(s)];
const fmt=n=>n===null||n===undefined?'—':Number(n).toLocaleString('pt-BR');
const fmtDate=s=>s?new Date(s+'T12:00:00').toLocaleDateString('pt-BR'):'—';
const fmtShort=s=>s?fmtDate(s).slice(0,5):'—';
function fmtRange(a,b){return a&&b?`${fmtShort(a)}–${fmtDate(b)}`:'—'}
function varHtml(v){if(v===null||v===undefined)return {text:'—',cls:'neutral'};const cls=v<0?'good':v>0?'bad':'neutral';const arrow=v<0?'▼':v>0?'▲':'•';return {text:`${arrow} ${Math.abs(v).toLocaleString('pt-BR',{minimumFractionDigits:1,maximumFractionDigits:1})}%`,cls};}
function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function q(){return new URLSearchParams({indicador:state.indicador,data_inicial:$('#fDataInicial').value,data_final:$('#fDataFinal').value,uf:$('#fUf').value,sc:$('#fSc').value,base:$('#fBase').value})}
async function json(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function setSelect(el,items,mapper=x=>({v:x,t:x}),keep=true){const old=keep?el.value:'';const first=el.options[0]?.textContent||'Todos';el.innerHTML=`<option value="">${first}</option>`;items.forEach(x=>{const m=mapper(x),o=document.createElement('option');o.value=m.v;o.textContent=m.t;el.appendChild(o)});if([...el.options].some(o=>o.value===old))el.value=old}
function isoShift(s,offset){const d=new Date(s+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10)}
function dayUTC(s){return new Date(s+'T12:00:00Z').getUTCDay()}
function setPeriodDates(items){
 const disponiveis=[...new Set(items||[])].filter(x=>/^\d{4}-\d{2}-\d{2}$/.test(x)).sort();
 const ini=$('#fDataInicial'),fim=$('#fDataFinal'),oldIni=ini.value,oldFim=fim.value;
 if(!disponiveis.length){ini.value='';fim.value='';return;}
 const min=disponiveis[0],max=disponiveis[disponiveis.length-1];
 const primeiraSegunda=isoShift(min,-((dayUTC(min)+6)%7));
 ini.min=primeiraSegunda;fim.min=primeiraSegunda;
 ini.max=max;fim.max=max;
 // Padrao da ultima semana encerrada (segunda a domingo).
 const domingo=isoShift(max,-dayUTC(max));
 const finalPadrao=domingo>=primeiraSegunda?domingo:max;
 const inicialPadrao=isoShift(finalPadrao,-6);
 const valida=x=>/^\d{4}-\d{2}-\d{2}$/.test(x)&&x>=primeiraSegunda&&x<=max;
 fim.value=valida(oldFim)?oldFim:finalPadrao;
 ini.value=valida(oldIni)&&oldIni<=fim.value?oldIni:inicialPadrao;
}

async function loadFilters(ind=state.indicador){const f=await json('/api/filters?indicador='+ind);if(ind!==state.indicador)return;setPeriodDates(f.datas||[]);setSelect($('#fUf'),f.uf||[]);setSelect($('#fSc'),f.sc||[]);setSelect($('#fBase'),f.bases||[],x=>({v:x.codigo,t:`${x.nome} (${x.codigo})`}));}
function meta(el,m,cur){
 el.className='meta';
 if(m===null||m===undefined){el.hidden=true;el.textContent='';return}
 el.hidden=false;el.textContent='Meta ≤ '+fmt(m);
 if(cur!==null&&cur!==undefined)el.classList.add(cur<=m?'good':'bad');
}
function renderVolumetria(d){
 const v=d.volumetria||{};
 const sem=v.sem_mov||{},ret=v.retidos||{};
 const pct=n=>n===null||n===undefined?'—':Number(n).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2})+'%';
 $('#volTotal').textContent=fmt(v.total);
 $('#volSplit').textContent=v.total===null||v.total===undefined
  ?'Sem volumetria J + M cadastrada'
  :'Coletada (J): '+fmt(v.coletada)+' • Saída (M): '+fmt(v.saida_entrega);
 $('#pctSemMov').textContent=pct(sem.percentual);
 $('#pctRetidos').textContent=pct(ret.percentual);
 $('#pctSemMovInfo').textContent=sem.pedidos===null||sem.pedidos===undefined
  ?'Sem dados suficientes para calcular'
  :fmt(sem.pedidos)+' pedidos >2 dias ÷ '+fmt(v.total)+' volumes';
 $('#pctRetidosInfo').textContent=ret.pedidos===null||ret.pedidos===undefined
  ?'Sem dados suficientes para calcular'
  :fmt(ret.pedidos)+' pedidos >3 dias ÷ '+fmt(v.total)+' volumes';
 $('#volAviso').textContent=v.aviso||'';
 $('#volAviso').classList.toggle('hidden',!v.aviso);
}
function renderKpis(d){
 const p=d.periodo||{};
 const atualLabel=p.semana_atual||'Período atual',anteriorLabel=p.semana_anterior||'Período anterior';
 const semanal=!!(p.semana_atual&&p.semana_anterior);
 $('#refDate').textContent=fmtRange(p.inicio,p.fim);
 $('#basesVarHead').textContent=semanal?'Variação semanal':'Variação do período';
 ['3','10'].forEach(c=>{
  const m=d.metrics['acima_'+c],v=varHtml(m.variacao);
  $('#prev'+c).textContent=fmt(m.anterior);
  $('#cur'+c).textContent=fmt(m.atual);
  $('#var'+c).textContent=v.text;
  $('#var'+c).className=v.cls;
  $('#lblPrev'+c).textContent=anteriorLabel+' • '+fmtRange(p.anterior_inicio,p.anterior_fim);
  $('#lblCur'+c).textContent=atualLabel+' • '+fmtRange(p.inicio,p.fim);
  $('#lblVar'+c).textContent=semanal?'Variação semanal':'Variação do período';
  meta($('#meta'+c),m.meta,m.atual);
 });
 const faltaAtual=p.faltantes_atual||[],faltaAnterior=p.faltantes_anterior||[];
 $('#missingD1').classList.toggle('hidden',!!p.atual_completo);
 $('#missingD1').textContent=faltaAtual.length
 ?'Período selecionado incompleto: '+faltaAtual.map(fmtDate).join(', ')+'. Sem variação até completar as cargas.'
 :'Período selecionado incompleto.';
 $('#missingD8').classList.toggle('hidden',!!p.anterior_completo);
 $('#missingD8').textContent=faltaAnterior.length
 ?'Período anterior incompleto: '+faltaAnterior.map(fmtDate).join(', ')+'. Sem variação até completar as cargas.'
 :'Período anterior incompleto.';
}
function lineChart(el,data){if(!data.length){el.innerHTML='<div class="empty">Sem histórico carregado no período.</div>';return}const W=900,H=300,L=68,R=28,T=28,B=42;const max=Math.max(...data.flatMap(x=>[x.acima_3,x.acima_10]),1)*1.16;const start=new Date(data[0].data+'T12:00:00').getTime(),end=new Date(data[data.length-1].data+'T12:00:00').getTime();const x=d=>L+(W-L-R)*(end===start?0.5:(new Date(d.data+'T12:00:00').getTime()-start)/(end-start));const y=v=>H-B-(H-T-B)*v/max;let svg='';for(let i=0;i<5;i++){const v=max*i/4,yy=y(v);svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#e4e7ec"/><text x="${L-9}" y="${yy+4}" text-anchor="end" font-size="11" fill="#667085">${fmt(Math.round(v))}</text>`}for(const [key,color,label,dy] of [['acima_3','#d71920',state.indicador==='sem_mov'?'3+d':'4+d',-9],['acima_10','#3978c6','11+d',15]]){svg+=`<polyline points="${data.map(d=>x(d)+','+y(d[key])).join(' ')}" fill="none" stroke="${color}" stroke-width="3"/>`;data.forEach(d=>{const xx=x(d),yy=y(d[key]);svg+=`<circle cx="${xx}" cy="${yy}" r="4" fill="${color}" stroke="#fff" stroke-width="2"><title>${fmtDate(d.data)} • ${label}: ${fmt(d[key])}</title></circle><text x="${xx}" y="${yy+dy}" text-anchor="middle" font-size="9" font-weight="700" fill="${color}">${fmt(d[key])}</text>`})}data.forEach((d,i)=>{if(i%Math.max(1,Math.ceil(data.length/10))===0||i===data.length-1)svg+=`<text x="${x(d)}" y="${H-12}" text-anchor="middle" font-size="10" fill="#667085">${fmtDate(d.data).slice(0,5)}</text>`});el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Evolução diária de pedidos por faixa de aging">${svg}</svg>`}
function bars(el,data,labelKey='label',valueKey='qtd'){if(!data.length){el.innerHTML='<div class="empty">Sem pacotes na faixa de dias selecionada.</div>';return}const max=Math.max(...data.map(x=>x[valueKey]),1);el.innerHTML=data.map(x=>`<div class="bar-row"><div class="bar-label" title="${esc(x[labelKey])}">${esc(x[labelKey])}</div><div class="bar-track"><div class="bar-fill" style="width:${x[valueKey]/max*100}%"></div></div><div class="bar-value">${fmt(x[valueKey])}</div></div>`).join('')}
function renderTables(d){
 const b=$('#basesBody');
 if(!d.top_bases.length)b.innerHTML='<tr><td colspan="5" class="empty">Sem pacotes no limiar do indicador no período selecionado.</td></tr>';
 else b.innerHTML=d.top_bases.map(x=>{
  const v=varHtml(x.variacao);
  return '<tr><td><strong>'+esc(x.base_nome||x.base_codigo||'—')+'</strong><div class="base-secondary">'+esc(x.base_codigo||'')+'</div></td><td>'+esc(x.sc||'—')+'</td><td class="num">'+fmt(x.acima_3)+'</td><td class="num">'+fmt(x.acima_10)+'</td><td class="'+v.cls+'">'+v.text+'</td></tr>';
 }).join('');
 const s=$('#scBody');
 if(!d.top_sc.length)s.innerHTML='<tr><td colspan="3" class="empty">Sem responsáveis com pacotes no limiar do indicador.</td></tr>';
 else s.innerHTML=d.top_sc.map(x=>'<tr><td>'+esc(x.sc||'—')+'</td><td class="num">'+fmt(x.acima_3)+'</td><td class="num">'+fmt(x.acima_10)+'</td></tr>').join('');
}
function limparResultadosAoFiltrar(){
 $('#basesBody').innerHTML='<tr><td colspan="5" class="empty">Atualizando bases para os filtros selecionados...</td></tr>';
 $('#scBody').innerHTML='<tr><td colspan="3" class="empty">Atualizando responsáveis...</td></tr>';
 $('#evolutionChart').innerHTML='<div class="empty">Atualizando evolução...</div>';
 ['#volTotal','#pctSemMov','#pctRetidos'].forEach(id=>$(id).textContent='—');
 $('#volSplit').textContent='Carregando volumetria...';
 $('#pctSemMovInfo').textContent='Calculando...';
 $('#pctRetidosInfo').textContent='Calculando...';
 $('#volAviso').textContent='';
 $('#agingBars').innerHTML='<div class="empty">Atualizando Aging...</div>';
 ['3','10'].forEach(c=>{
  for(const k of ['prev','cur','var'])$('#'+k+c).textContent='—';
 });
}
function renderSnapshot(d){
 $('#snapshotDate').textContent=d.data?'• '+fmtDate(d.data):'• sem dados';
 const campos=['d1','d2','d3','d4','acima4','total','acima3'];
 const tbody=$('#snapshotBody');
 const lista=d.bases||[];
 if(!lista.length){
  tbody.innerHTML='<tr><td colspan="9" class="empty">Nenhuma remessa para o filtro e a data selecionados.</td></tr>';
 }else{
  tbody.innerHTML=lista.map(x=>'<tr>'+
   '<td><strong>'+esc(x.base_nome||x.base_codigo)+'</strong><div class="base-secondary">'+esc(x.base_codigo)+'</div></td>'+
   '<td>'+esc(x.sc||'—')+'</td>'+
   campos.map(k=>'<td class="num">'+fmt(x[k])+'</td>').join('')+
   '</tr>').join('');
 }
 const total=d.total||{};
 $('#snapshotFoot').innerHTML='<tr><th>Total</th><th>—</th>'+
  campos.map(k=>'<th class="num">'+fmt(total[k]||0)+'</th>').join('')+'</tr>';
 $('#snapshotNote').textContent=d.aviso||'Comparar somente snapshots da mesma data.';
}
async function loadDashboard(){const request=++state.request;document.body.setAttribute('aria-busy','true');limparResultadosAoFiltrar();$('#snapshotBody').innerHTML='<tr><td colspan="9" class="empty">Conferindo remessas do dia final...</td></tr>';$('#snapshotFoot').replaceChildren();$('#loadError').classList.add('hidden');try{if($('#fDataInicial').value>$('#fDataFinal').value){toast('Data inicial não pode ser maior que a final');return}const d=await json('/api/dashboard?'+q());if(request!==state.request)return;const fs=d.fonte||{};$('#sourceStatus').textContent=fs.fonte?fs.fonte+(fs.sincronizado_em?' • Sincronizado: '+new Date(fs.sincronizado_em).toLocaleString('pt-BR'):'')+(fs.consulta_segundos!==undefined?' • Consulta ao banco: '+fmt(fs.consulta_segundos)+' s':''):'';$('#sourceWarning').textContent=fs.aviso||'';$('#sourceWarning').classList.toggle('hidden',!fs.aviso);renderKpis(d);renderVolumetria(d);lineChart($('#evolutionChart'),d.evolucao);bars($('#agingBars'),d.aging);renderTables(d);
const sp=new URLSearchParams({indicador:state.indicador,data:$('#fDataFinal').value,uf:$('#fUf').value,sc:$('#fSc').value,base:$('#fBase').value});
json('/api/snapshot?'+sp).then(snapshot=>{if(request===state.request)renderSnapshot(snapshot)}).catch(err=>{
 if(request===state.request){
  $('#snapshotDate').textContent='• sem conferência';
  $('#snapshotBody').innerHTML='<tr><td colspan="9" class="empty">Sem conferência para essa data ou filtro. Verifique a carga diária.</td></tr>';
  $('#snapshotFoot').replaceChildren();
 }
});}catch(e){if(request===state.request){$('#loadError').textContent='Não foi possível carregar este indicador. Verifique a conexão e o período selecionado.';$('#loadError').classList.remove('hidden')}}finally{if(request===state.request)document.body.setAttribute('aria-busy','false')}}
async function switchTab(ind){++state.request;state.indicador=ind;$$('.tab').forEach(x=>{x.classList.toggle('active',x.dataset.indicador===ind);x.setAttribute('aria-pressed',x.dataset.indicador===ind)});const name=ind==='sem_mov'?'Sem Movimentação':'Retidos';$('#indicatorTitle').textContent=name;$('#evolutionTitle').textContent='Evolução diária • '+name;const limiar=ind==='sem_mov'?'3+ dias':'4+ dias';
 $('.indicador-limiar').forEach(el=>el.textContent=el.tagName==='SPAN' && el.closest('.kpi-head')?(ind==='sem_mov'?'(3 dias ou mais)':'(4 dias ou mais)'):limiar);
 $('#indicatorDesc').textContent='Pedidos com aging de '+(ind==='sem_mov'?'3 dias ou mais':'4 dias ou mais')+' e corte adicional de 11+ dias • R/M Orlean';
 $('#downloadBtn').textContent='Baixar base '+limiar;['3','10'].forEach(c=>{['prev','cur','var'].forEach(k=>$('#'+k+c).textContent='—');$('#meta'+c).textContent='Meta —'});['evolutionChart','agingBars','basesBody','scBody'].forEach(id=>$('#'+id).replaceChildren());['#fUf','#fSc','#fBase'].forEach(s=>$(s).value='');document.body.setAttribute('aria-busy','true');try{await loadFilters(ind);if(state.indicador===ind)await loadDashboard()}catch(e){if(state.indicador===ind){document.body.setAttribute('aria-busy','false');toast('Erro ao carregar filtros. Tente novamente.')}}}
$$('.tab').forEach(t=>t.onclick=()=>switchTab(t.dataset.indicador));
['#fDataInicial','#fDataFinal','#fUf','#fSc','#fBase'].forEach(s=>$(s).onchange=()=>loadDashboard());
$('#btnClear').onclick=()=>{['#fUf','#fSc','#fBase'].forEach(s=>$(s).value='');loadDashboard()};
$('#downloadBtn').onclick=()=>{const p=new URLSearchParams({indicador:state.indicador,data_inicial:$('#fDataInicial').value,data_final:$('#fDataFinal').value,corte:'3',uf:$('#fUf').value,sc:$('#fSc').value,base:$('#fBase').value});window.location='/api/download?'+p};
switchTab(new URLSearchParams(window.location.search).get('indicador') === 'retidos' ? 'retidos' : 'sem_mov');
