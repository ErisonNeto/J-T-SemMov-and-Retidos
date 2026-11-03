from datetime import date, timedelta
import csv
import io
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import METAS
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


def somente_orlean(df):
    if df.empty:
        return df.copy()
    return df[df['rm'].astype(str).str.contains('ORLEAN', case=False, na=False)].copy()


def filtrar(df, indicador, uf='', sc='', base=''):
    df = somente_orlean(df)
    df = df[df.indicador == indicador]
    for col, value in [('uf', uf), ('sc', sc), ('base_codigo', base)]:
        if value:
            df = df[df[col] == value]
    return df.copy()


def quantidades(df):
    if df.empty:
        return {'acima_3': 0, 'acima_10': 0}
    aging = df['aging_valor'].fillna(0)
    pedidos = df['pedidos'].fillna(0)
    return {
        'acima_3': int(pedidos[aging > 3].sum()),
        'acima_10': int(pedidos[aging > 10].sum()),
    }


def ranking(df, columns, limit):
    if df.empty:
        return []
    df = df.assign(
        q3=df.pedidos.where(df.aging_valor > 3, 0),
        q10=df.pedidos.where(df.aging_valor > 10, 0),
    )
    g = df.groupby(columns, dropna=False)[['q3', 'q10']].sum().reset_index()
    g = g[g.q3 > 0]
    if g.empty:
        return []
    g = g.sort_values(['q3'] + columns, ascending=[False] + [True] * len(columns)).head(limit)
    return [
        {**{c: r[c] for c in columns}, 'acima_3': int(r['q3']), 'acima_10': int(r['q10'])}
        for r in g.to_dict('records')
    ]


def motivos(df):
    df = df[(df.motivo != '') & (df.aging_valor > 3)]
    if df.empty:
        return []
    g = df.groupby('motivo').pedidos.sum().sort_values(ascending=False).head(10)
    return [{'motivo': str(m), 'qtd': int(q)} for m, q in g.items()]


def datas_disponiveis(raw, indicador):
    raw = somente_orlean(raw)
    datas = raw.loc[raw.indicador == indicador, 'data'].dropna().astype(str)
    return sorted({x for x in datas if x})


def resolver_periodo(raw, indicador, data_inicial='', data_final=''):
    disponiveis = datas_disponiveis(raw, indicador)
    if not disponiveis:
        return None, None, disponiveis

    datas_obj = [date.fromisoformat(x) for x in disponiveis]
    menor = min(datas_obj)
    maior = max(datas_obj)

    try:
        fim = date.fromisoformat(data_final) if data_final else maior
        inicio = date.fromisoformat(data_inicial) if data_inicial else max(menor, fim - timedelta(days=13))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Data inválida. Use AAAA-MM-DD.') from exc

    if inicio > fim:
        raise HTTPException(status_code=400, detail='A data inicial não pode ser maior que a data final.')
    if fim < menor or inicio > maior:
        raise HTTPException(status_code=404, detail='O período selecionado não possui dados carregados.')

    return inicio, fim, disponiveis


def aging_distribuicao(df):
    df = df[df.aging_valor > 3]
    if df.empty:
        return []
    g = df.groupby('aging_valor').pedidos.sum().sort_index()
    return [
        {
            'label': f'{int(aging)} dias',
            'qtd': int(qtd),
            'aging': int(aging),
            'acima_10': bool(int(aging) > 10),
        }
        for aging, qtd in g.items()
    ]


@app.get('/', response_class=HTMLResponse)
def home():
    return (BASE_DIR / 'templates/index.html').read_text(encoding='utf-8')


@app.get('/api/filters')
def filters(indicador: str = Query(pattern='^(sem_mov|retidos)$')):
    df = somente_orlean(carregar())
    df_ind = df[df.indicador == indicador]
    out = {c: sorted(x for x in df_ind[c].unique() if x) for c in ('uf', 'sc')}
    b = (
        df_ind[df_ind.base_codigo != ''][['base_codigo', 'base_nome']]
        .drop_duplicates()
        .sort_values(['base_nome', 'base_codigo'])
    )
    out['bases'] = [
        {'codigo': r.base_codigo, 'nome': r.base_nome or r.base_codigo}
        for r in b.itertuples()
    ]
    out['datas'] = datas_disponiveis(df, indicador)
    out['rm'] = 'Orlean'
    return out


@app.get('/api/dashboard')
def dashboard(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    uf: str = '',
    sc: str = '',
    base: str = '',
):
    raw = carregar()
    inicio_data, fim_data, disponiveis = resolver_periodo(raw, indicador, data_inicial, data_final)

    if inicio_data is None or fim_data is None:
        return {
            'indicador': indicador,
            'periodo': {'inicio': None, 'fim': None},
            'datas': {'atual': None, 'anterior': None, 'tem_atual': False, 'tem_anterior': False},
            'metrics': {
                'acima_3': {'atual': None, 'anterior': None, 'variacao': None, 'meta': METAS[indicador]['acima_3'], 'dentro_meta': None},
                'acima_10': {'atual': None, 'anterior': None, 'variacao': None, 'meta': METAS[indicador]['acima_10'], 'dentro_meta': None},
            },
            'evolucao': [], 'aging': [], 'top_bases': [], 'top_sc': [], 'motivos': [],
            'ultima_carga': None, 'fonte': status_fonte(),
        }

    fim_iso = fim_data.isoformat()
    anterior_data = fim_data - timedelta(days=7)
    anterior_iso = anterior_data.isoformat()
    tem_atual = fim_iso in set(disponiveis)
    tem_anterior = anterior_iso in set(disponiveis)

    df = filtrar(raw, indicador, uf, sc, base)
    atual = df[df.data == fim_iso]
    anterior = df[df.data == anterior_iso]

    q_atual = quantidades(atual)
    q_anterior = quantidades(anterior)

    metrics = {}
    for c in ('3', '10'):
        key = f'acima_{c}'
        a = q_atual[key] if tem_atual else None
        p = q_anterior[key] if tem_anterior else None
        meta = METAS[indicador][key]
        metrics[key] = {
            'atual': a,
            'anterior': p,
            'variacao': variacao(a, p),
            'meta': meta,
            'dentro_meta': a <= meta if (a is not None and meta is not None) else None,
        }

    inicio_iso = inicio_data.isoformat()
    periodo_df = df[(df.data >= inicio_iso) & (df.data <= fim_iso)]
    evo = [
        {'data': dt, **quantidades(g)}
        for dt, g in periodo_df.groupby('data', sort=True)
    ]

    bases = ranking(atual, ['base_codigo', 'base_nome', 'sc'], 20)
    for b in bases:
        prev = quantidades(anterior[anterior.base_codigo == b['base_codigo']])['acima_3'] if tem_anterior else None
        b['variacao'] = variacao(b['acima_3'], prev)
        mot = motivos(atual[atual.base_codigo == b['base_codigo']])
        b['motivo'] = mot[0]['motivo'] if mot else ''

    return {
        'indicador': indicador,
        'periodo': {'inicio': inicio_iso, 'fim': fim_iso},
        'datas': {
            'atual': fim_iso,
            'anterior': anterior_iso,
            'tem_atual': tem_atual,
            'tem_anterior': tem_anterior,
        },
        'metrics': metrics,
        'evolucao': evo,
        'aging': aging_distribuicao(atual),
        'top_bases': bases,
        'top_sc': ranking(atual[atual.sc != ''], ['sc'], 20),
        'motivos': motivos(atual),
        'ultima_carga': disponiveis[-1] if disponiveis else None,
        'fonte': status_fonte(),
    }


@app.get('/api/download')
def download(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_final: str = '',
    corte: str = Query(default='3', pattern='^(3|10)$'),
    uf: str = '', sc: str = '', base: str = '',
):
    raw = carregar()
    _, fim_data, _ = resolver_periodo(raw, indicador, '', data_final)
    if fim_data is None:
        raise HTTPException(status_code=404, detail='Não há dados carregados para este indicador.')

    fim_iso = fim_data.isoformat()
    df = filtrar(raw, indicador, uf, sc, base)
    df = df[df.data == fim_iso]
    limite = 3 if corte == '3' else 10
    df = df[df.aging_valor > limite]

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
        headers={'Content-Disposition': f'attachment; filename="resumo_{indicador}_{fim_iso}_acima_{corte}.csv"'},
    )
