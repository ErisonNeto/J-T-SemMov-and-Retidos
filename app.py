from datetime import datetime, timedelta
import csv
import io
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from config import JANELA_EVOLUCAO_DIAS, METAS, TIMEZONE
from arquivos import carregar, status_fonte

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title='J&T | Sem Movimentação e Retidos — PostgreSQL')
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')

def datas_ref():
    hoje = datetime.now(TIMEZONE).date()
    return hoje - timedelta(days=1), hoje - timedelta(days=8)

def variacao(atual, anterior):
    if atual is None or anterior is None: return None
    if anterior == 0: return 0.0 if atual == 0 else None
    return round((atual-anterior)/anterior*100, 2)

def filtrar(df, indicador, uf='', rm='', sc='', base=''):
    df = df[(df.indicador == indicador) & (df.rm != '')]
    for col, value in [('uf', uf), ('rm', rm), ('sc', sc), ('base_codigo', base)]:
        if value: df = df[df[col] == value]
    return df.copy()

def quantidades(df):
    return {f'acima_{c}': int(df.loc[df[f'acima_{c}'] == 1, 'pedidos'].sum()) for c in ('3', '10')}

def ranking(df, columns, limit):
    if df.empty: return []
    df = df.assign(q3=df.pedidos * df.acima_3, q10=df.pedidos * df.acima_10)
    g = df.groupby(columns, dropna=False)[['q3','q10']].sum().reset_index()
    g = g.sort_values(['q3']+columns, ascending=[False]+[True]*len(columns)).head(limit)
    return [{**{c: r[c] for c in columns}, 'acima_3':int(r['q3']), 'acima_10':int(r['q10'])} for r in g.to_dict('records')]

def motivos(df):
    df = df[df.motivo != '']
    if df.empty: return []
    g = df.groupby('motivo').pedidos.sum().sort_values(ascending=False).head(10)
    return [{'motivo': str(m), 'qtd':int(q)} for m,q in g.items()]

@app.get('/', response_class=HTMLResponse)
def home():
    return (BASE_DIR/'templates/index.html').read_text(encoding='utf-8')

@app.get('/api/filters')
def filters(indicador: str = Query(pattern='^(sem_mov|retidos)$')):
    df = carregar()
    df = df[df.indicador == indicador]
    out = {c:sorted(x for x in df[c].unique() if x) for c in ('uf','rm','sc')}
    b = df[df.base_codigo != ''][['base_codigo','base_nome']].drop_duplicates().sort_values('base_nome')
    out['bases'] = [{'codigo':r.base_codigo,'nome':r.base_nome or r.base_codigo} for r in b.itertuples()]
    return out

@app.get('/api/dashboard')
def dashboard(indicador: str = Query(pattern='^(sem_mov|retidos)$'), uf:str='', rm:str='', sc:str='', base:str=''):
    d1,d8 = datas_ref()
    raw = carregar()
    dates = raw.loc[raw.indicador == indicador, 'data']
    has1,has8 = d1.isoformat() in set(dates), d8.isoformat() in set(dates)
    df = filtrar(raw, indicador, uf,rm,sc,base)
    atual = df[df.data == d1.isoformat()]
    anterior = df[df.data == d8.isoformat()]
    q1,q8 = quantidades(atual),quantidades(anterior)
    metrics = {}
    for c in ('3','10'):
        key=f'acima_{c}'
        a,p = q1[key] if has1 else None, q8[key] if has8 else None
        meta=METAS[indicador][key]
        metrics[key]={'atual':a,'anterior':p,'variacao':variacao(a,p),'meta':meta,
                      'dentro_meta':a <= meta if a is not None and meta is not None else None}
    ini=(d1-timedelta(days=JANELA_EVOLUCAO_DIAS-1)).isoformat()
    evo=[{'data':dt,**quantidades(g)} for dt,g in df[(df.data>=ini)&(df.data<=d1.isoformat())].groupby('data',sort=True)]
    aging=[]
    if not atual.empty:
        ag=atual.groupby('aging_label').agg(ordem=('aging_valor','min'),qtd=('pedidos','sum')).sort_values('ordem')
        aging=[{'label':str(label) or 'Sem faixa','qtd':int(r.qtd)} for label,r in ag.iterrows()]
    bases=ranking(atual,['base_codigo','base_nome','sc'],10)
    for b in bases:
        prev=quantidades(anterior[anterior.base_codigo==b['base_codigo']])['acima_3'] if has8 else None
        mot=motivos(atual[(atual.base_codigo==b['base_codigo'])&(atual.acima_3==1)])
        b.update(variacao=variacao(b['acima_3'],prev),motivo=mot[0]['motivo'] if mot else '',plano_acao='',responsavel='',prazo='')
    return {'indicador':indicador,'datas':{'d1':d1.isoformat(),'d8':d8.isoformat(),'tem_d1':has1,'tem_d8':has8},
            'metrics':metrics,'evolucao':evo,'aging':aging,'top_bases':bases,
            'top_sc':ranking(atual[atual.sc!=''],['sc'],15),'motivos':motivos(atual),
            'ultima_carga':max(dates) if len(dates) else None, 'fonte':status_fonte()}

@app.get('/api/download')
def download(indicador:str=Query(pattern='^(sem_mov|retidos)$'), corte:str=Query(default='3',pattern='^(3|10|todos)$'), uf:str='',rm:str='',sc:str='',base:str=''):
    d1,_=datas_ref()
    df=filtrar(carregar(),indicador,uf,rm,sc,base)
    df=df[df.data==d1.isoformat()]
    if corte!='todos': df=df[df[f'acima_{corte}']==1]
    def stream():
        # Evita fórmulas ao abrir textos do arquivo baixado no Excel.
        def safe(v):
            return "'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v
        yield '\ufeff'
        buf=io.StringIO(); writer=csv.writer(buf,delimiter=';')
        writer.writerow(df.columns); yield buf.getvalue(); buf.seek(0);buf.truncate(0)
        for row in df.itertuples(index=False,name=None):
            writer.writerow([safe(v) for v in row]);yield buf.getvalue();buf.seek(0);buf.truncate(0)
    return StreamingResponse(stream(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="resumo_{indicador}_{d1}_acima_{corte}.csv"'})
