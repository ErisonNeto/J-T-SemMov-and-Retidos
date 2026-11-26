from datetime import date, timedelta
import csv
import io
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import METAS, METAS_PERCENTUAIS, MINIMOS_PERCENTUAIS
from arquivos import carregar, status_fonte, carregar_volumetria, consultar_contagem_remessas, consultar_remessas_unicas_semanas
from exclusoes import sc_excluido

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
    return df[
        df['rm'].astype(str).str.contains('ORLEAN', case=False, na=False)
        & ~df['sc'].map(sc_excluido)
    ].copy()


def filtrar(df, indicador, uf='', sc='', base=''):
    df = somente_orlean(df)
    df = df[df.indicador == indicador]
    for col, value in [('uf', uf), ('sc', sc), ('base_codigo', base)]:
        if value:
            df = df[df[col] == value]
    return df.copy()


def limite_indicador(indicador):
    """Sem Mov e Retidos por aging: estritamente >3 dias (4+)."""
    return 3


def quantidades(df, indicador):
    if df.empty:
        return {'acima_3': 0, 'acima_10': 0}
    aging = df['aging_valor'].fillna(0)
    pedidos = df['pedidos'].fillna(0)
    return {
        # Campo historico acima_3 mantido por compatibilidade com frontend:
        # o limiar depende do indicador e e exibido com o rotulo correto.
        'acima_3': int(pedidos[aging > limite_indicador(indicador)].sum()),
        'acima_10': int(pedidos[aging > 10].sum()),
    }



def percentuais_da_volumetria(raw, volume, inicio, fim, uf='', sc='', base='', erro_volume=None):
    """Taxas por volume J+M, na mesma data/base; nunca media de percentuais.

    Para cada dia, exige que o snapshot do indicador exista para Orlean
    e que haja denominador no Outlet para o recorte selecionado.
    """
    sem_v = {
        'percentual': None, 'pedidos': None, 'limiar_dias': 3,
        'criterio': 'sem movimentacao > 3 dias (4 dias ou mais; DC/CD pendente de validacao)',
        'meta_percentual': METAS_PERCENTUAIS['sem_mov'],
        'minimo_percentual': MINIMOS_PERCENTUAIS['sem_mov'],
        'dentro_meta': None,
    }
    ret_v = {
        'percentual': None, 'pedidos': None, 'limiar_dias': 3,
        'criterio': 'retidos com aging > 3 dias (4 dias ou mais); vencimento do SLA nao verificado',
        'meta_percentual': METAS_PERCENTUAIS['retidos_aging'],
        'minimo_percentual': MINIMOS_PERCENTUAIS['retidos_aging'],
        'dentro_meta': None,
    }
    # A fonte atual nao tem prazo de vencimento por remessa. Nao e possivel
    # afirmar que 4 dias retido = 4 dias de SLA vencido.
    # KPI oficial fica indisponivel ate integrarmos a data do prazo.
    retidos_sla = {
        'percentual': None, 'pedidos': None, 'limiar_dias_apos_vencimento': 3,
        'criterio': 'pedidos com mais de 3 dias apos o prazo de vencimento',
        'meta_percentual': METAS_PERCENTUAIS['retidos_sla_vencido'],
        'minimo_percentual': MINIMOS_PERCENTUAIS['retidos_sla_vencido'],
        'dentro_meta': None,
        'status': 'aguardando_data_de_vencimento',
    }
    saida = {
        'coletada': None, 'saida_entrega': None, 'total': None,
        'sem_mov': sem_v, 'retidos': ret_v,
        'retidos_sla': retidos_sla,
        'aviso': erro_volume,
    }
    if volume.empty:
        saida['aviso'] = (
            erro_volume or
            'Sem volumetria Outlet no PostgreSQL. Execute a importacao atualizada.'
        )
        return saida

    vol = volume[(volume.data >= inicio) & (volume.data <= fim)]
    for campo, valor in [('uf', uf), ('sc', sc), ('base_codigo', base)]:
        if valor:
            vol = vol[vol[campo] == valor]
    if vol.empty:
        saida['aviso'] = (
            'Nao existe volumetria Outlet para as datas e bases selecionadas.'
        )
        return saida

    coletada = int(vol['coletada'].sum())
    entregue = int(vol['saida_entrega'].sum())
    denominador = coletada + entregue
    saida.update({
        'coletada': coletada,
        'saida_entrega': entregue,
        'total': denominador,
    })
    if denominador <= 0:
        saida['aviso'] = 'A volumetria e zero neste recorte.'
        return saida

    dias_esperados = set(
        datas_entre(date.fromisoformat(inicio), date.fromisoformat(fim))
    )
    dias_com_volume = set(vol['data'].dropna().astype(str))
    dias_faltando_volume = dias_esperados - dias_com_volume

    regional = somente_orlean(raw)
    # Verificar snapshot por dia no conjunto inteiro de Orlean, pois
    # uma base sem pedidos em um dia pode ser um zero verdadeiro.
    base_regional = regional
    subset = regional[(regional.data >= inicio) & (regional.data <= fim)]
    for campo, valor in [('uf', uf), ('sc', sc), ('base_codigo', base)]:
        if valor:
            subset = subset[subset[campo] == valor]

    pendencias = []
    for indicador, chave, limiar in [
        ('sem_mov', 'sem_mov', 3),
        ('retidos', 'retidos', 3),
    ]:
        dias_snapshot = set(
            base_regional.loc[
                base_regional.indicador == indicador, 'data'
            ].dropna().astype(str)
        )
        faltando = sorted(
            dias_faltando_volume | (dias_esperados - dias_snapshot)
        )
        if faltando:
            pendencias.append(
                f"{chave}: sem cobertura completa em {', '.join(faltando[:4])}"
                + ('...' if len(faltando) > 4 else '')
            )
            continue
        recorte = subset[
            (subset.indicador == indicador)
            & (subset.aging_valor > limiar)
        ]
        pedidos = int(recorte['pedidos'].sum())
        saida[chave]['pedidos'] = pedidos
        saida[chave]['percentual'] = round(
            100 * pedidos / denominador, 2
        )
        if saida[chave]['meta_percentual'] is not None:
            saida[chave]['dentro_meta'] = (
                saida[chave]['percentual'] <= saida[chave]['meta_percentual']
            )
    if pendencias:
        saida['aviso'] = (
            'Percentuais incompletos: ' + '; '.join(pendencias)
        )
    else:
        saida['aviso'] = (
            'Sem Mov e Retidos consideram aging >3 dias (4+), '
            'com acumulado de snapshots no periodo. Referencias '
            'de 5% e 3% aplicadas aos percentuais operacionais, '
            'nao aos totais de quantidade. Nao confundir aging de '
            'Retidos com prazo de SLA vencido; a fonte atual nao '
            'traz data de vencimento. Recorte DC/CD nao confirmado.'
        )
    return saida

def ranking(df, columns, limit, indicador):
    if df.empty:
        return []
    df = df.assign(
        q3=df.pedidos.where(df.aging_valor > limite_indicador(indicador), 0),
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


def aging_distribuicao(df, indicador):
    df = df[df.aging_valor > limite_indicador(indicador)]
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
            'volumetria': {
                'coletada': None, 'saida_entrega': None, 'total': None,
                'sem_mov': {'percentual': None, 'pedidos': None},
                'retidos': {'percentual': None, 'pedidos': None,
                            'meta_percentual': METAS_PERCENTUAIS['retidos_aging'],
        'minimo_percentual': MINIMOS_PERCENTUAIS['retidos_aging'],
                            'dentro_meta': None},
                'retidos_sla': {
                    'percentual': None, 'pedidos': None,
                    'meta_percentual': METAS_PERCENTUAIS['retidos_sla_vencido'],
        'minimo_percentual': MINIMOS_PERCENTUAIS['retidos_sla_vencido'],
                    'dentro_meta': None, 'status': 'aguardando_data_de_vencimento',
                },
                'aviso': 'Sem periodo disponivel',
            },
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

    q_atual = quantidades(periodo_df, indicador)
    q_anterior = quantidades(anterior_df, indicador)

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
        {'data': dt, **quantidades(g, indicador)}
        for dt, g in periodo_df.groupby('data', sort=True)
    ]

    bases = ranking(periodo_df, ['base_codigo', 'base_nome', 'sc'], 20, indicador)
    for b in bases:
        prev = quantidades(anterior_df[anterior_df.base_codigo == b['base_codigo']], indicador)['acima_3'] if anterior_completo else None
        b['variacao'] = variacao(b['acima_3'], prev) if atual_completo else None

    # Valores de Outlet permanecem independentes do indicador selecionado:
    # ambos os percentuais aparecem lado a lado no mesmo painel.
    volume, erro_volume = carregar_volumetria()
    kpi_volume = percentuais_da_volumetria(
        raw, volume, inicio_iso, fim_iso,
        uf=uf, sc=sc, base=base, erro_volume=erro_volume,
    )

    return {
        'indicador': indicador,
        'volumetria': kpi_volume,
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
        'aging': aging_distribuicao(final_df, indicador),
        'top_bases': bases,
        'top_sc': ranking(periodo_df[periodo_df.sc != ''], ['sc'], 20, indicador),
        'motivos': [],
        'ultima_carga': disponiveis[-1] if disponiveis else None,
        'fonte': status_fonte(),
    }


# Consulta exploratoria por aging, separada dos indicadores oficiais.
_FAIXAS_AGING = {
    "todos": ("Todos os dias de aging", 1, None),
    "1a3": ("De 1 a 3 dias", 1, 3),
    "4mais": ("4 dias ou mais", 4, None),
    "4a10": ("De 4 a 10 dias", 4, 10),
    "11mais": ("11 dias ou mais", 11, None),
    "11a15": ("De 11 a 15 dias", 11, 15),
    "16a30": ("De 16 a 30 dias", 16, 30),
    "31mais": ("31 dias ou mais", 31, None),
}


def _faixa_limites(faixa: str):
    if faixa in _FAIXAS_AGING:
        return _FAIXAS_AGING[faixa]
    # Corte aberto da ferramenta operacional: acima de N = N+1 dias em diante.
    # Nao modifica os cortes fixos de 4+ e 11+ dias dos KPIs.
    if faixa.startswith("acima_"):
        dias = faixa[len("acima_"):]
        if dias.isascii() and dias.isdecimal() and 1 <= len(dias) <= 3 and dias[0] != "0":
            n = int(dias)
            if 1 <= n <= 365:
                return (f"Acima de {n} dia" if n == 1 else f"Acima de {n} dias",
                        n + 1, None)
    if faixa.isdecimal() and str(int(faixa)) == faixa and 1 <= int(faixa) <= 30:
        n = int(faixa)
        return (f"{n} dia" if n == 1 else f"{n} dias", n, n)
    raise HTTPException(
        status_code=400,
        detail="Faixa de aging invalida. Use acima_N (1 a 365), dia exato (1 a 30) ou uma faixa.",
    )


def _rotulo_faixa_aging(faixa: str) -> str:
    return _faixa_limites(faixa)[0]


def _aplicar_faixa_aging(df, faixa: str):
    _, minimo, maximo = _faixa_limites(faixa)
    if df.empty:
        return df.copy()
    ages = df["aging_valor"].fillna(0)
    mask = ages >= minimo
    if maximo is not None:
        mask &= ages <= maximo
    return df[mask].copy()


def _consulta_aging_recorte(indicador, data_inicial, data_final, uf, sc, base, aging):
    raw = carregar()
    inicio, fim, _ = resolver_periodo(raw, indicador, data_inicial, data_final)
    if inicio is None or fim is None:
        raise HTTPException(status_code=404, detail="Nao ha dados para este indicador.")
    if (fim - inicio).days > 90:
        raise HTTPException(status_code=400, detail="Consulta limitada a 91 dias.")
    filtrado = filtrar(raw, indicador, uf, sc, base)
    filtrado = filtrado[
        (filtrado.data >= inicio.isoformat())
        & (filtrado.data <= fim.isoformat())
    ]
    validado = _aplicar_faixa_aging(filtrado, aging)
    cobertura = set(datas_disponiveis(raw, indicador))
    sem_carga = [dt for dt in datas_entre(inicio, fim) if dt not in cobertura]
    return validado, inicio.isoformat(), fim.isoformat(), sem_carga


@app.get('/api/consulta-aging')
def consulta_aging(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    uf: str = '', sc: str = '', base: str = '',
    aging: str = '4mais',
):
    """Mostra 1, 2, 3, ... dias e faixas, sem alterar os cortes do KPI."""
    df, inicio, fim, sem_carga = _consulta_aging_recorte(
        indicador, data_inicial, data_final, uf, sc, base, aging
    )
    resposta = {
        "indicador": indicador,
        "faixa": aging,
        "rotulo": _rotulo_faixa_aging(aging),
        "periodo": {"inicio": inicio, "fim": fim},
        "total_pedidos": int(df["pedidos"].sum()),
        "total_linhas": int(df["linhas"].sum()),
        "ultimo_dia": fim,
        "pedidos_ultimo_dia": int(df.loc[df.data == fim, "pedidos"].sum()),
        "dias_sem_carga": sem_carga,
        "evolucao": [], "bases": [], "scs": [], "aging_dias": [],
        "aviso": (
            "Consulta exploratoria: soma de pedidos dos snapshots por dia. "
            "Uma remessa pode aparecer em varios dias. "
            "Linhas nao significam remessas distintas. "
            "Os KPIs oficiais 4+ e 11+ nao foram alterados."
            + (
                " Atencao: dias sem snapshot: " + ", ".join(sem_carga[:6])
                + ("..." if len(sem_carga) > 6 else "")
                if sem_carga else ""
            )
        ),
    }
    if df.empty:
        return resposta

    por_dia = df.groupby("data", sort=True)[["pedidos", "linhas"]].sum()
    resposta["evolucao"] = [
        {"data": str(dt), "pedidos": int(r.pedidos), "linhas": int(r.linhas)}
        for dt, r in por_dia.iterrows()
    ]
    por_base = (
        df.groupby(["base_codigo", "base_nome", "sc"], dropna=False)[
            ["pedidos", "linhas"]
        ].sum().reset_index()
        .sort_values(["pedidos", "base_nome"], ascending=[False, True])
        .head(25)
    )
    resposta["bases"] = [
        {"base_codigo": str(r.base_codigo), "base_nome": str(r.base_nome),
         "sc": str(r.sc), "pedidos": int(r.pedidos), "linhas": int(r.linhas)}
        for r in por_base.itertuples(index=False)
    ]
    por_sc = (
        df.groupby("sc", dropna=False)[["pedidos", "linhas"]]
        .sum().sort_values("pedidos", ascending=False).head(25)
    )
    resposta["scs"] = [
        {"sc": str(nome), "pedidos": int(r.pedidos), "linhas": int(r.linhas)}
        for nome, r in por_sc.iterrows()
    ]
    por_idade = df.groupby("aging_valor", sort=True)[["pedidos", "linhas"]].sum()
    resposta["aging_dias"] = [
        {"dias": int(d), "pedidos": int(r.pedidos), "linhas": int(r.linhas)}
        for d, r in por_idade.iterrows()
    ]
    return resposta


@app.get('/api/pacotes-unicos')
def pacotes_unicos(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    uf: str = '', sc: str = '', base: str = '',
    aging: str = 'acima_3',
):
    """Total sem duplicidade em consulta independente da evolucao e ranking."""
    df, inicio, fim, sem_carga = _consulta_aging_recorte(
        indicador, data_inicial, data_final, uf, sc, base, aging
    )
    resposta = {
        "pacotes_unicos": 0 if df.empty else None,
        "acumulado": int(df["pedidos"].sum()),
        "periodo": {"inicio": inicio, "fim": fim},
        "faixa": aging,
        "aviso": "",
        "evolucao": [],
    }
    if df.empty:
        return resposta

    # Os codigos filtrados ja respeitam R/M Orlean, S/C, UF e base.
    _, minimo, maximo = _faixa_limites(aging)
    codigos = sorted({
        str(x) for x in df["base_codigo"].dropna().unique() if str(x)
    })
    try:
        contagem = consultar_contagem_remessas(
            indicador, inicio, fim, codigos, minimo, maximo
        )
        if contagem["acumulado"] != resposta["acumulado"]:
            resposta["aviso"] = (
                "Nao foi possivel confirmar o total unico: a carga de detalhes "
                "difere do resumo atual. O acumulado permanece disponivel."
            )
        elif contagem["sem_remessa"] > 0:
            resposta["aviso"] = (
                f"Existem {contagem['sem_remessa']} linha(s) sem numero "
                "de remessa: total unico exato indisponivel."
            )
        else:
            resposta["pacotes_unicos"] = contagem["pacotes_unicos"]
            # Nao confundir soma dos unicos diarios com o total distinto
            # do periodo. Cada dia e conferido contra o snapshot agregado.
            esperado_por_dia = {
                str(dia): int(qtd)
                for dia, qtd in df.groupby("data")["pedidos"].sum().items()
            }
            resposta["evolucao"] = [
                {"data": item["data"],
                 "pacotes_unicos": (
                     item["pacotes_unicos"]
                     if item["sem_remessa"] == 0 and
                     item["acumulado"] == esperado_por_dia.get(item["data"])
                     else None
                 )}
                for item in contagem["evolucao"]
            ]
    except Exception:
        # A consulta de detalhes nao deve interromper o acumulado/ranking.
        resposta["aviso"] = (
            "Pacotes unicos indisponiveis: nao foi possivel consultar "
            "os detalhes no PostgreSQL pela JT API."
        )
    return resposta


@app.get('/api/comparacao-pacotes-unicos')
def comparacao_pacotes_unicos(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    uf: str = '', sc: str = '', base: str = '',
):
    """Remessas unicas dentro de cada semana dos KPIs 4+ e 11+ dias.

    As duas semanas sao deduplicadas separadamente; acumulado oficial
    continua vindo da consulta de dashboard.
    """
    raw = carregar()
    inicio, fim, _ = resolver_periodo(raw, indicador, data_inicial, data_final)
    retorno = {
        "anterior": {"acima_3": None, "acima_10": None},
        "atual": {"acima_3": None, "acima_10": None},
        "aviso": "",
    }
    if inicio is None or fim is None:
        return retorno

    dias = (fim - inicio).days + 1
    fim_anterior = inicio - timedelta(days=1)
    inicio_anterior = fim_anterior - timedelta(days=dias - 1)
    df = filtrar(raw, indicador, uf, sc, base)
    codigos = sorted({
        str(x) for x in df.loc[
            (df.data >= inicio_anterior.isoformat())
            & (df.data <= fim.isoformat()), "base_codigo"
        ].dropna().unique() if str(x)
    })
    try:
        banco = consultar_remessas_unicas_semanas(
            indicador, inicio_anterior.isoformat(), inicio.isoformat(),
            fim.isoformat(), codigos
        )
        avisos = []
        for periodo, dt_ini, dt_fim in (
            ("anterior", inicio_anterior, fim_anterior),
            ("atual", inicio, fim),
        ):
            recorte = df.loc[
                (df.data >= dt_ini.isoformat())
                & (df.data <= dt_fim.isoformat())
            ]
            esperado = quantidades(recorte, indicador)
            consultado = banco.get(periodo)
            for chave, sufixo in (("acima_3", "4"), ("acima_10", "11")):
                acumulado = esperado[chave]
                if consultado is None:
                    if acumulado == 0:
                        retorno[periodo][chave] = 0
                    else:
                        avisos.append(f"{periodo} {sufixo}+ sem retorno do banco")
                    continue
                if consultado[f"acumulado_{sufixo}"] != acumulado:
                    avisos.append(f"{periodo} {sufixo}+ com cargas divergentes")
                elif consultado[f"sem_remessa_{sufixo}"] > 0:
                    avisos.append(f"{periodo} {sufixo}+ com remessa em branco")
                else:
                    retorno[periodo][chave] = consultado[f"unicos_{sufixo}"]
        retorno["aviso"] = "; ".join(avisos)
    except Exception:
        retorno["aviso"] = (
            "Nao foi possivel conferir os pacotes unicos das semanas "
            "no PostgreSQL. Valores nao estimados."
        )
    return retorno


@app.get('/api/consulta-aging.csv')
def consulta_aging_csv(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
    data_inicial: str = '',
    data_final: str = '',
    uf: str = '', sc: str = '', base: str = '',
    aging: str = '4mais',
):
    """Exporta resumo agregado. Nao expoe IDs de remessas na web publica."""
    df, inicio, fim, _ = _consulta_aging_recorte(
        indicador, data_inicial, data_final, uf, sc, base, aging
    )
    cols = ["data", "base_codigo", "base_nome", "sc",
            "aging_valor", "pedidos", "linhas"]
    recorte = df[cols].sort_values(["data", "base_nome", "aging_valor"])

    def safe(v):
        valor = str(v) if v is not None else ""
        return "'" + valor if valor.lstrip().startswith(("=", "+", "-", "@")) else valor

    def stream():
        yield "\ufeff"
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(["Data", "Codigo da base", "Base", "S/C",
                         "Aging (dias)", "Pedidos (soma)", "Linhas da fonte"])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in recorte.itertuples(index=False, name=None):
            writer.writerow([safe(v) for v in row])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="consulta_aging_{indicador}_{aging}_{inicio}_a_{fim}.csv"'},
    )


@app.get('/api/snapshot')
def snapshot(
    indicador: str = Query(pattern='^(sem_mov|retidos)$'),
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
        if age > limite_indicador(indicador):
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
            'Retrato de um único dia. Contagem de remessas (COUNT), não pedidos. '
            + ('Sem Mov: mais de 3 dias (4 dias ou mais). ' if indicador == 'sem_mov'
               else 'Retidos: 4 dias ou mais. ')
            + 'Só compare com planilha da mesma data, bases e faixas de dias.'
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
    limite = limite_indicador(indicador) if corte == '3' else 10
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
