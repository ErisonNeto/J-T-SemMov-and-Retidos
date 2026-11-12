"""Fonte de dados do dashboard via JT API / PostgreSQL.

O dashboard não acessa o PostgreSQL diretamente. A JT API executa a consulta
agregada e devolve somente o histórico necessário para os gráficos/filtros.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

from config import JANELA_EVOLUCAO_DIAS, TIMEZONE
from exclusoes import sc_excluido

DIMENSOES = [
    "indicador", "data", "base_codigo", "base_nome", "uf", "rm", "sc",
    "regional", "aging_label", "aging_valor", "acima_3", "acima_10", "motivo",
]
COLUNAS = DIMENSOES + ["pedidos", "linhas"]

_API_URL = os.getenv("JT_API_URL", "https://jt-api.rockettechbr.com.br").rstrip("/")
_API_KEY = os.getenv("JT_API_KEY", "").strip()
_TIMEOUT = float(os.getenv("JT_API_TIMEOUT", "90"))
_CACHE_SECONDS = max(30, int(os.getenv("JT_API_CACHE_SECONDS", "600")))
_HISTORY_DAYS = max(
    JANELA_EVOLUCAO_DIAS + 9,
    int(os.getenv("JT_HISTORY_DAYS", "45")),
)

_lock = threading.Lock()
_cache_df: pd.DataFrame | None = None
_cache_until = 0.0
_status = {
    "fonte": "PostgreSQL via JT API",
    "sincronizado_em": None,
    "aviso": None,
}


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["X-API-Key"] = _API_KEY
    return headers


def _extrair_linhas(payload: Any) -> list:
    """Extrai as linhas da resposta da JT API, inclusive quando vêm posicionais."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Resposta inesperada da JT API.")

    for chave in ("rows", "data", "registros", "resultado", "result", "items"):
        valor = payload.get(chave)
        if isinstance(valor, list):
            return valor

    if isinstance(payload.get("data"), dict):
        return _extrair_linhas(payload["data"])

    raise ValueError("A JT API respondeu, mas não retornou uma lista de linhas reconhecível.")


def _dataframe_da_resposta(payload: Any) -> pd.DataFrame:
    rows = _extrair_linhas(payload)
    if not rows:
        return pd.DataFrame(columns=COLUNAS)

    primeira = rows[0]
    if isinstance(primeira, dict):
        df = pd.DataFrame(rows)
    else:
        if not isinstance(primeira, (list, tuple)):
            raise ValueError("Formato de linha inesperado na resposta da JT API.")
        if len(primeira) != len(COLUNAS):
            raise ValueError(
                f"JT API retornou {len(primeira)} colunas por linha; "
                f"o dashboard espera {len(COLUNAS)}."
            )
        df = pd.DataFrame(rows, columns=COLUNAS)

    faltantes = set(COLUNAS) - set(df.columns)
    if faltantes:
        raise ValueError(f"Consulta da JT API sem colunas esperadas: {sorted(faltantes)}")
    return df[COLUNAS].copy()


def _consultar_resumo() -> pd.DataFrame:
    """Consulta SO as bases de Orlean, e agrega no PostgreSQL.

    Antes, eram agrupadas e transferidas todas as bases do Brasil durante
    45 dias, com 13 dimensoes por linha, e o filtro Orlean era aplicado
    somente no dashboard. Agora a base e filtrada ANTES do GROUP BY.
    A Gestao publicada e a fonte oficial para o S/C (incluindo Johan).
    """
    inicio_perf = time.perf_counter()
    inicio = (datetime.now(TIMEZONE).date() - timedelta(days=_HISTORY_DAYS)).isoformat()

    with requests.Session() as session:
        session.headers.update({"Accept-Encoding": "identity"})

        # Dimensao pequena (cerca de 500 codigos): consulta simples,
        # evitando filtros complexos que podem gerar 500 na JT API.
        gres = session.post(
            f"{_API_URL}/query", headers=_headers(),
            json={
                "sql": (
                    "SELECT base_codigo, base_nome, rm, sc, uf, regional "
                    "FROM public.jt_gestao_bases"
                ),
                "params": [],
            },
            timeout=_TIMEOUT,
        )
        if not gres.ok:
            raise RuntimeError(
                f"JT API Gestao HTTP {gres.status_code}: {gres.text[:400]}"
            )
        gestao = {}
        bases_excluidas = 0
        for row in _extrair_linhas(gres.json()):
            if isinstance(row, dict):
                cod = str(row.get("base_codigo") or "").strip()
                origem = row
            else:
                cols = ("base_codigo", "base_nome", "rm", "sc", "uf", "regional")
                origem = dict(zip(cols, row))
                cod = str(origem.get("base_codigo") or "").strip()
            if sc_excluido(origem.get("sc")):
                bases_excluidas += 1
                continue
            if cod and "ORLEAN" in str(origem.get("rm") or "").upper():
                gestao[cod] = {
                    field: str(origem.get(field) or "").strip()
                    for field in ("base_nome", "rm", "sc", "uf", "regional")
                }

        if not gestao:
            raise RuntimeError(
                "A Gestao publicada nao retornou bases de Orlean; "
                "nao farei a consulta nacional completa."
            )
        codigos = sorted(gestao)
        _status["bases_excluidas_sc"] = bases_excluidas
        if bases_excluidas:
            print(f"[SEM MOV/RETIDOS] S/C Darley excluido: {bases_excluidas} base(s) fora dos indicadores.", flush=True)
        else:
            print("[SEM MOV/RETIDOS] AVISO: Darley nao encontrado na coluna S/C da Gestao publicada.", flush=True)

        # GROUP BY enxuto. Exclui motivos e nomes repetidos por remessa;
        # recupera os atributos oficiais pela Gestao de 146 bases.
        sql = """
            SELECT d.indicador,
                   d.data::text AS data,
                   COALESCE(d.base_codigo, '') AS base_codigo,
                   COALESCE(d.aging_valor, 0)::int AS aging_valor,
                   SUM(COALESCE(d.pedidos, 0))::bigint AS pedidos,
                   COUNT(*)::bigint AS linhas
            FROM public.sem_mov_retidos_detalhe AS d
            WHERE d.data >= %s::date
              AND d.base_codigo = ANY(%s::text[])
              AND d.indicador IN ('sem_mov', 'retidos')
            GROUP BY 1,2,3,4
            ORDER BY 2,1,3,4
        """
        resp = session.post(
            f"{_API_URL}/query", headers=_headers(),
            json={"sql": sql, "params": [inicio, codigos]},
            timeout=max(_TIMEOUT, 120),
        )
        if not resp.ok:
            raise RuntimeError(
                f"JT API /query resumo Orlean HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        colunas_sql = (
            "indicador", "data", "base_codigo", "aging_valor", "pedidos", "linhas"
        )
        rows = _extrair_linhas(resp.json())

    if not rows:
        _status["fonte"] = "PostgreSQL via JT API - somente Orlean"
        _status["consulta_segundos"] = round(time.perf_counter() - inicio_perf, 2)
        _status["linhas_resumo"] = 0
        return pd.DataFrame(columns=COLUNAS)

    df = pd.DataFrame(
        rows if isinstance(rows[0], dict)
        else [dict(zip(colunas_sql, row)) for row in rows]
    )
    faltantes = set(colunas_sql) - set(df.columns)
    if faltantes:
        raise ValueError(f"JT API omitiu colunas de resumo: {sorted(faltantes)}")

    df["base_codigo"] = df["base_codigo"].fillna("").astype(str).str.strip()
    for field in ("base_nome", "uf", "rm", "sc", "regional"):
        df[field] = df["base_codigo"].map(
            {cod: dados[field] for cod, dados in gestao.items()}
        ).fillna("")
    df["indicador"] = df["indicador"].fillna("").astype(str).str.strip()
    df["data"] = df["data"].fillna("").astype(str).str.strip()
    for field in ("aging_valor", "pedidos", "linhas"):
        df[field] = pd.to_numeric(df[field], errors="coerce").fillna(0).astype("int64")
    df["aging_label"] = df["aging_valor"].astype(str) + " dias"
    df["acima_3"] = (df["aging_valor"] > 3).astype("int64")
    df["acima_10"] = (df["aging_valor"] > 10).astype("int64")
    df["motivo"] = ""

    _status["fonte"] = (
        "PostgreSQL via JT API • consulta otimizada: somente bases de Orlean"
    )
    _status["consulta_segundos"] = round(time.perf_counter() - inicio_perf, 2)
    _status["linhas_resumo"] = len(df)
    _status["bases_orlean"] = len(gestao)
    print(
        f"[SEM MOV/RETIDOS] Resumo Orlean: {len(df):,} linhas "
        f"de {len(gestao)} bases em {_status['consulta_segundos']}s; "
        f"cache {_CACHE_SECONDS}s.", flush=True
    )
    return df[COLUNAS].copy()

def carregar() -> pd.DataFrame:
    """Carrega um snapshot agregado com cache curto para não repetir a query por endpoint."""
    global _cache_df, _cache_until
    agora = time.monotonic()
    if _cache_df is not None and agora < _cache_until:
        return _cache_df.copy()

    with _lock:
        agora = time.monotonic()
        if _cache_df is not None and agora < _cache_until:
            return _cache_df.copy()
        try:
            novo = _consultar_resumo()
            _cache_df = novo
            _cache_until = time.monotonic() + _CACHE_SECONDS
            _status["sincronizado_em"] = datetime.now(TIMEZONE).isoformat()
            _status["aviso"] = None
        except Exception as exc:
            if _cache_df is None:
                raise
            _cache_until = time.monotonic() + min(30, _CACHE_SECONDS)
            _status["aviso"] = (
                "Falha ao atualizar a JT API; mantendo o último snapshot válido. "
                f"{exc}"
            )
        return _cache_df.copy()



# Cache independente da volumetria, com invalidacao automatica.
_vol_lock = threading.Lock()
_vol_cache: pd.DataFrame | None = None
_vol_until = 0.0
_vol_warning = None


def carregar_volumetria() -> tuple[pd.DataFrame, str | None]:
    """Volumetria Outlet J+M por base/data publicada via JT API.

    So consulta a tabela pequena jt_outlet_volume_diario (nao baixa
    planilhas nem faz leitura completa de remessas). Quando a tabela
    ainda nao existir, indica indisponibilidade, jamais 0%.
    """
    global _vol_cache, _vol_until, _vol_warning
    agora = time.monotonic()
    if _vol_cache is not None and agora < _vol_until:
        return _vol_cache.copy(), _vol_warning

    with _vol_lock:
        if _vol_cache is not None and time.monotonic() < _vol_until:
            return _vol_cache.copy(), _vol_warning
        cols = ["data", "base_codigo", "coletada", "saida_entrega"]
        try:
            inicio = (
                datetime.now(TIMEZONE).date()
                - timedelta(days=_HISTORY_DAYS)
            ).isoformat()
            with requests.Session() as session:
                session.headers.update({"Accept-Encoding": "identity"})
                resp = session.post(
                    f"{_API_URL}/query",
                    headers=_headers(),
                    json={
                        "sql": """
                            SELECT data::text AS data, base_codigo,
                                   SUM(COALESCE(coletada::bigint,0))::bigint AS coletada,
                                   SUM(COALESCE(saida_entrega::bigint,0))::bigint AS saida_entrega
                            FROM public.jt_outlet_volume_diario
                            WHERE data::date >= %s::date
                            GROUP BY data, base_codigo
                        """,
                        "params": [inicio],
                    },
                    timeout=_TIMEOUT,
                )
                if not resp.ok:
                    raise RuntimeError(
                        f"JT API tabela jt_outlet_volume_diario: "
                        f"HTTP {resp.status_code}: {resp.text[:250]}"
                    )
                rows = _extrair_linhas(resp.json())
                df = pd.DataFrame(
                    rows if rows and isinstance(rows[0], dict)
                    else [dict(zip(cols, row)) for row in rows],
                    columns=cols,
                )
                for campo in ("coletada", "saida_entrega"):
                    df[campo] = pd.to_numeric(
                        df[campo], errors="coerce"
                    ).fillna(0).astype("int64")
                df["data"] = df["data"].fillna("").astype(str).str.strip()
                df["base_codigo"] = df["base_codigo"].fillna("").astype(str).str.strip()

                # Gestao e a referencia para filtrar S/C e UF do Outlet.
                gresp = session.post(
                    f"{_API_URL}/query",
                    headers=_headers(),
                    json={
                        "sql": (
                            "SELECT base_codigo, rm, sc, uf "
                            "FROM public.jt_gestao_bases"
                        ),
                        "params": [],
                    },
                    timeout=_TIMEOUT,
                )
                if not gresp.ok:
                    raise RuntimeError(
                        f"JT API Gestao Outlet HTTP {gresp.status_code}: "
                        f"{gresp.text[:250]}"
                    )
                gc = ("base_codigo", "rm", "sc", "uf")
                g = pd.DataFrame(
                    (r if isinstance(r, dict) else dict(zip(gc, r)))
                    for r in _extrair_linhas(gresp.json())
                )
                if g.empty:
                    raise RuntimeError("Gestao publicada vazia")
                for campo in gc:
                    g[campo] = g[campo].fillna("").astype(str).str.strip()
                g = g[
                    g["rm"].str.contains("ORLEAN", case=False, na=False)
                    & ~g["sc"].map(sc_excluido)
                ]
                g = g.drop_duplicates(subset=["base_codigo"])
                df = df.merge(g[["base_codigo", "sc", "uf"]], on="base_codigo", how="inner")

            _vol_cache = df
            _vol_warning = None if not df.empty else (
                "Nenhuma volumetria Outlet publicada no periodo. "
                "Execute o importador dos tres arquivos atualizado."
            )
            _vol_until = time.monotonic() + (_CACHE_SECONDS if not df.empty else 60)
        except Exception as exc:
            _vol_cache = pd.DataFrame(
                columns=cols + ["sc", "uf"]
            )
            _vol_warning = (
                "Volumetria Outlet nao disponivel no PostgreSQL: "
                + str(exc)[:260]
            )
            _vol_until = time.monotonic() + 60
        return _vol_cache.copy(), _vol_warning

def status_fonte() -> dict:
    return dict(_status)
