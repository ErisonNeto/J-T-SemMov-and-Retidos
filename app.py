from datetime import date, timedelta
import csv
import io
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from config import JANELA_EVOLUCAO_DIAS, METAS
from arquivos import carregar, status_fonte

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title='J&T | Sem Movimentação e Retidos — PostgreSQL')
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')


def variacao(atual, anterior):
    if atual is None or anterior is None:
        return None
    if anterior == 0:
        return 0.0 if atual == 0 else None
    return round((atual - anterior) / anterior * 100, 2)


def filtrar(df, indicador, uf='', rm='', sc='', base=''):
    df = df[(df.indicador == indicador) & (df.rm != '')]
    for col, value in [('uf', uf), ('rm', rm), ('sc', sc), ('base_codigo', base)]:
        if value:
            df = df[df[col] == value]
    return df.copy()


def quantidades(df):
    return {f'acima_{c}': int(df.loc[df[f'acima_{c}'] == 1, 'pedidos'].sum()) for c in ('3', '10')}


def ranking(df, columns, limit):
    if df.empty:
        return []
    df = df.assign(q3=df.pedidos * df.acima_3, q10=df.pedidos * df.acima_10)
    g = df.groupby(columns, dropna=False)[['q3', 'q10']].sum().reset_index()
    g = g.sort_values(['q3'] + columns, ascending=[False] + [True] * len(columns)).head(limit)
    return [
        {**{c: r[c] for c in columns}, 'acima_3': int(r['q3']), 'acima_10': int(r['q10'])}
        for r in g.to_dict('records')
    ]


def motivos(df):
    df = df[df.motivo != '']
    if df.empty:
        return []
    g = df.groupby('motivo').pedidos.sum().sort_values(ascending=False).head(10)
    return [{'motivo': str(m), 'qtd': int(q)} for m, q in g.items()]


def datas_disponiveis(raw, indicador):
    datas = raw.loc[raw.indicador == indicador, 'data'].dropna().astype(str)
    return sorted({x for x in datas if x}, reverse=True)


def resolver_data(raw, indicador, data_ref=''):
    disponiveis = datas_disponiveis(raw, indicador)
    if not disponiveis:
        return None, None, disponiveis

    escolhida = data_ref.strip() if data_ref else disponiveis[0]
    if escolhida not in disponiveis:
        raise HTTPException(status_code=404, detail='A data selecionada não está carregada para este indicador.')

    try:
        atual = date.fromisoformat(escolhida)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Data inválida. Use AAAA-MM-DD.') from exc

    return atual, atual - timedelta(days=7), disponiveis


@app.get('/', response_class=HTMLResponse)
def home():
    return (BASE_DIR / 'templates/index.html').read_text(encoding='utf-8')


@app.get('/api/filters')
def filters(indicador: str = Query(pattern='^(sem_mov|retidos)$')):
    df = carregar()
    df_ind = df[df.indicador == indicador]
    out = {c: sorted(x for x in df_ind[c].unique() if x) for c in ('uf', 'rm', 'sc')}
    b = df_ind[df_ind.base_codigo != ''][['base_codigo', 'base_nome']].drop_duplicates().sort_values('base_nome')
    out['bases'] = [{'codigo': r.base_codigo, 'nome': r.base_nome or r.base_codigo} for r in b.itertuples()]
    out['datas'] = datas_disponiveis(df, indicador)
    return out


@app.get('/api/dashboard')
def dashboard(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data: str = '',
    uf: str = '',
    rm: str = '',
    sc: str = '',
    base: str = '',
):
    raw = carregar()
    atual_data, anterior_data, disponiveis = resolver_data(raw, indicador, data)

    if atual_data is None:
        return {
            'indicador': indicador,
            'datas': {'atual': None, 'anterior': None, 'tem_atual': False, 'tem_anterior': False},
            'metrics': {
                'acima_3': {'atual': None, 'anterior': None, 'variacao': None, 'meta': METAS[indicador]['acima_3'], 'dentro_meta': None},
                'acima_10': {'atual': None, 'anterior': None, 'variacao': None, 'meta': METAS[indicador]['acima_10'], 'dentro_meta': None},
            },
            'evolucao': [], 'aging': [], 'top_bases': [], 'top_sc': [], 'motivos': [],
            'ultima_carga': None, 'fonte': status_fonte(),
        }

    atual_iso = atual_data.isoformat()
    anterior_iso = anterior_data.isoformat()
    tem_anterior = anterior_iso in set(disponiveis)

    df = filtrar(raw, indicador, uf, rm, sc, base)
    atual = df[df.data == atual_iso]
    anterior = df[df.data == anterior_iso]
    q_atual = quantidades(atual)
    q_anterior = quantidades(anterior)

    metrics = {}
    for c in ('3', '10'):
        key = f'acima_{c}'
        a = q_atual[key]
        p = q_anterior[key] if tem_anterior else None
        meta = METAS[indicador][key]
        metrics[key] = {
            'atual': a,
            'anterior': p,
            'variacao': variacao(a, p),
            'meta': meta,
            'dentro_meta': a <= meta if meta is not None else None,
        }

    inicio = (atual_data - timedelta(days=JANELA_EVOLUCAO_DIAS - 1)).isoformat()
    evo = [
        {'data': dt, **quantidades(g)}
        for dt, g in df[(df.data >= inicio) & (df.data <= atual_iso)].groupby('data', sort=True)
    ]

    aging = []
    if not atual.empty:
        ag = atual.groupby('aging_label').agg(ordem=('aging_valor', 'min'), qtd=('pedidos', 'sum')).sort_values('ordem')
        aging = [{'label': str(label) or 'Sem faixa', 'qtd': int(r.qtd)} for label, r in ag.iterrows()]

    bases = ranking(atual, ['base_codigo', 'base_nome', 'sc'], 10)
    for b in bases:
        prev = quantidades(anterior[anterior.base_codigo == b['base_codigo']])['acima_3'] if tem_anterior else None
        mot = motivos(atual[(atual.base_codigo == b['base_codigo']) & (atual.acima_3 == 1)])
        b.update(
            variacao=variacao(b['acima_3'], prev),
            motivo=mot[0]['motivo'] if mot else '',
            plano_acao='', responsavel='', prazo='',
        )

    return {
        'indicador': indicador,
        'datas': {
            'atual': atual_iso,
            'anterior': anterior_iso,
            'tem_atual': True,
            'tem_anterior': tem_anterior,
        },
        'metrics': metrics,
        'evolucao': evo,
        'aging': aging,
        'top_bases': bases,
        'top_sc': ranking(atual[atual.sc != ''], ['sc'], 15),
        'motivos': motivos(atual),
        'ultima_carga': disponiveis[0] if disponiveis else None,
        'fonte': status_fonte(),
    }


@app.get('/api/download')
def download(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data: str = '',
    corte: str = Query(default='3', pattern='^(3|10|todos)$'),
    uf: str = '', rm: str = '', sc: str = '', base: str = '',
):
    raw = carregar()
    atual_data, _, _ = resolver_data(raw, indicador, data)
    if atual_data is None:
        raise HTTPException(status_code=404, detail='Não há dados carregados para este indicador.')

    atual_iso = atual_data.isoformat()
    df = filtrar(raw, indicador, uf, rm, sc, base)
    df = df[df.data == atual_iso]
    if corte != 'todos':
        df = df[df[f'acima_{corte}'] == 1]

    def stream():
        def safe(v):
            return "'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v

        yield '\ufeff'
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=';')
        writer.writerow(df.columns)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for row in df.itertuples(index=False, name=None):
            writer.writerow([safe(v) for v in row])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        stream(),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="resumo_{indicador}_{atual_iso}_acima_{corte}.csv"'},
    )
