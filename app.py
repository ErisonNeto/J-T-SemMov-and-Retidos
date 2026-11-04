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
        inicio = date.fromisoformat(data_inicial) if data_inicial else max(menor, fim - timedelta(days=6))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Data inválida. Use AAAA-MM-DD.') from exc

    if inicio > fim:
        raise HTTPException(status_code=400, detail='A data inicial não pode ser maior que a data final.')
    if fim < menor or inicio > maior:
        raise HTTPException(status_code=404, detail='O período selecionado não possui dados carregados.')

    return inicio, fim, disponiveis


def datas_entre(inicio, fim):
    dias = (fim - inicio).days + 1
    return [(inicio + timedelta(days=i)).isoformat() for i in range(dias)]


def rotulo_semana(inicio, fim):
    if (fim - inicio).days == 6 and inicio.weekday() == 0 and fim.weekday() == 6:
        return f"W{inicio.isocalendar().week:02d}"
    return None


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
            'periodo': {'inicio': None, 'fim': None, 'atual_completo': False, 'anterior_completo': False},
            'metrics': {
                'acima_3': {'atual': None, 'anterior': None, 'variacao': None, 'meta': METAS[indicador]['acima_3'], 'dentro_meta': None},
                'acima_10': {'atual': None, 'anterior': None, 'variacao': None, 'meta': METAS[indicador]['acima_10'], 'dentro_meta': None},
            },
            'evolucao': [], 'aging': [], 'top_bases': [], 'top_sc': [], 'motivos': [],
            'ultima_carga': None, 'fonte': status_fonte(),
        }

    dias_periodo = (fim_data - inicio_data).days + 1
    anterior_fim = inicio_data - timedelta(days=1)
    anterior_inicio = anterior_fim - timedelta(days=dias_periodo - 1)

    inicio_iso = inicio_data.isoformat()
    fim_iso = fim_data.isoformat()
    anterior_inicio_iso = anterior_inicio.isoformat()
    anterior_fim_iso = anterior_fim.isoformat()

    disponiveis_set = set(disponiveis)
    esperadas_atual = datas_entre(inicio_data, fim_data)
    esperadas_anterior = datas_entre(anterior_inicio, anterior_fim)
    faltantes_atual = [d for d in esperadas_atual if d not in disponiveis_set]
    faltantes_anterior = [d for d in esperadas_anterior if d not in disponiveis_set]
    atual_completo = not faltantes_atual
    anterior_completo = not faltantes_anterior

    df = filtrar(raw, indicador, uf, sc, base)
    periodo_df = df[(df.data >= inicio_iso) & (df.data <= fim_iso)]
    anterior_df = df[(df.data >= anterior_inicio_iso) & (df.data <= anterior_fim_iso)]
    final_df = df[df.data == fim_iso]

    q_atual = quantidades(periodo_df)
    q_anterior = quantidades(anterior_df)

    metrics = {}
    for c in ('3', '10'):
        key = f'acima_{c}'
        a = q_atual[key]
        p = q_anterior[key]
        meta = METAS[indicador][key]
        metrics[key] = {
            'atual': a,
            'anterior': p,
            'variacao': variacao(a, p) if (atual_completo and anterior_completo) else None,
            'meta': meta,
            'dentro_meta': a <= meta if (a is not None and meta is not None) else None,
        }

    evo = [
        {'data': dt, **quantidades(g)}
        for dt, g in periodo_df.groupby('data', sort=True)
    ]

    bases = ranking(periodo_df, ['base_codigo', 'base_nome', 'sc'], 20)
    for b in bases:
        prev = quantidades(anterior_df[anterior_df.base_codigo == b['base_codigo']])['acima_3'] if anterior_completo else None
        b['variacao'] = variacao(b['acima_3'], prev) if atual_completo else None

    return {
        'indicador': indicador,
        'periodo': {
            'inicio': inicio_iso,
            'fim': fim_iso,
            'anterior_inicio': anterior_inicio_iso,
            'anterior_fim': anterior_fim_iso,
            'dias': dias_periodo,
            'semana_atual': rotulo_semana(inicio_data, fim_data),
            'semana_anterior': rotulo_semana(anterior_inicio, anterior_fim),
            'atual_completo': atual_completo,
            'anterior_completo': anterior_completo,
            'faltantes_atual': faltantes_atual,
            'faltantes_anterior': faltantes_anterior,
        },
        'metrics': metrics,
        'evolucao': evo,
        'aging': aging_distribuicao(final_df),
        'top_bases': bases,
        'top_sc': ranking(periodo_df[periodo_df.sc != ''], ['sc'], 20),
        'motivos': [],
        'ultima_carga': disponiveis[-1] if disponiveis else None,
        'fonte': status_fonte(),
    }


@app.get('/api/snapshot')
def snapshot(
    indicador: str = Query(pattern='^(sem_mov|retidos)
def download(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    corte: str = Query(default='3', pattern='^(3|10)$'),
    uf: str = '', sc: str = '', base: str = '',
):
    raw = carregar()
    inicio_data, fim_data, _ = resolver_periodo(raw, indicador, data_inicial, data_final)
    if inicio_data is None or fim_data is None:
        raise HTTPException(status_code=404, detail='Não há dados carregados para este indicador.')

    inicio_iso = inicio_data.isoformat()
    fim_iso = fim_data.isoformat()
    df = filtrar(raw, indicador, uf, sc, base)
    df = df[(df.data >= inicio_iso) & (df.data <= fim_iso)]
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
        headers={'Content-Disposition': f'attachment; filename="resumo_{indicador}_{inicio_iso}_a_{fim_iso}_acima_{corte}.csv"'},
    )
),
    data: str = '',
    uf: str = '', sc: str = '', base: str = '',
):
    """Retrato do ÚNICO DIA selecionado: remessas por 1/2/3/4/5+ dias.

    Diferente do KPI de pedidos acumulados em uma semana, esta tabela soma
    COUNT(*) (linhas de remessa) na data exata do snapshot. Não soma uma mesma
    remessa em vários dias.
    """
    raw = carregar()
    datas = datas_disponiveis(raw, indicador)
    if not datas:
        return {
            'indicador': indicador, 'data': None, 'bases': [],
            'total': {'d1': 0, 'd2': 0, 'd3': 0, 'd4': 0,
                      'acima4': 0, 'total': 0, 'acima3': 0, 'pedidos_acima3': 0},
        }
    dia = data or datas[-1]
    try:
        date.fromisoformat(dia)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Data inválida. Use AAAA-MM-DD.') from exc
    # Exige data disponível na origem, não inventa valor zerado para dia ausente.
    if dia not in datas:
        raise HTTPException(
            status_code=404,
            detail=f'Não há snapshot de {dia} para {indicador} no banco.',
        )

    df = filtrar(raw, indicador, uf, sc, base)
    df = df[df.data == dia]
    campos = ('d1', 'd2', 'd3', 'd4', 'acima4', 'total', 'acima3', 'pedidos_acima3')
    bases = {}
    for r in df.itertuples(index=False):
        codigo = str(r.base_codigo)
        item = bases.setdefault(
            codigo,
            {
                'base_codigo': codigo, 'base_nome': str(r.base_nome),
                'sc': str(r.sc), **{k: 0 for k in campos},
            },
        )
        age = int(r.aging_valor or 0)
        # linhas = COUNT(*) de remessas no PostgreSQL (não pedidos).
        linhas = int(r.linhas or 0)
        pedidos = int(r.pedidos or 0)
        item['total'] += linhas
        if 1 <= age <= 4:
            item[f'd{age}'] += linhas
        elif age > 4:
            item['acima4'] += linhas
        if age > 3:
            item['acima3'] += linhas
            item['pedidos_acima3'] += pedidos

    lista = sorted(
        bases.values(),
        key=lambda item: (-item['total'], item['base_nome'], item['base_codigo']),
    )
    total = {c: sum(item[c] for item in lista) for c in campos}
    return {
        'indicador': indicador, 'data': dia, 'bases': lista, 'total': total,
        'sem_remessas': not bool(lista),
        'aviso': (
            'Retrato de um único dia. Contagem de remessas (COUNT), não pedidos '
            'e não a soma semanal. Só compare com planilha da mesma data, '
            'mesma lista de bases e mesmas faixas de dias.'
        ),
    }


@app.get('/api/download')
def download(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    corte: str = Query(default='3', pattern='^(3|10)$'),
    uf: str = '', sc: str = '', base: str = '',
):
    raw = carregar()
    inicio_data, fim_data, _ = resolver_periodo(raw, indicador, data_inicial, data_final)
    if inicio_data is None or fim_data is None:
        raise HTTPException(status_code=404, detail='Não há dados carregados para este indicador.')

    inicio_iso = inicio_data.isoformat()
    fim_iso = fim_data.isoformat()
    df = filtrar(raw, indicador, uf, sc, base)
    df = df[(df.data >= inicio_iso) & (df.data <= fim_iso)]
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
        headers={'Content-Disposition': f'attachment; filename="resumo_{indicador}_{inicio_iso}_a_{fim_iso}_acima_{corte}.csv"'},
    )
