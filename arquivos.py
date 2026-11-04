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

DIMENSOES = [
    "indicador", "data", "base_codigo", "base_nome", "uf", "rm", "sc",
    "regional", "aging_label", "aging_valor", "acima_3", "acima_10", "motivo",
]
COLUNAS = DIMENSOES + ["pedidos", "linhas"]

_API_URL = os.getenv("JT_API_URL", "https://jt-api.rockettechbr.com.br").rstrip("/")
_API_KEY = os.getenv("JT_API_KEY", "").strip()
_TIMEOUT = float(os.getenv("JT_API_TIMEOUT", "90"))
_CACHE_SECONDS = max(5, int(os.getenv("JT_API_CACHE_SECONDS", "120")))
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
    inicio = (datetime.now(TIMEZONE).date() - timedelta(days=_HISTORY_DAYS)).isoformat()
    # Usa a Gestão publicada como dimensão única, por código de base. Isso
    # corrige a atribuição de R/M, S/C, UF e nome em todos os períodos,
    # sem reimportar/editar milhões de remessas históricas.
    # Antes da primeira publicação, permanece no modo legado.
    gestao_ativa = False
    try:
        conf = requests.post(
            f"{_API_URL}/query",
            headers=_headers(),
            json={
                "sql": """
                    SELECT COUNT(*)::int AS qtd
                    FROM public.jt_gestao_bases
                """,
                "params": [],
            },
            timeout=_TIMEOUT,
        )
        if conf.ok:
            x = _extrair_linhas(conf.json())
            if x:
                qtd = int(x[0]["qtd"] if isinstance(x[0], dict) else x[0][0])
                gestao_ativa = qtd >= 500
                if 0 < qtd < 500:
                    _status["aviso"] = (
                        f"Tabela de Gestão parcialmente publicada ({qtd} códigos). "
                        "Mantendo os responsáveis gravados nas remessas até completar."
                    )
        elif conf.status_code not in (400, 404, 422):
            raise RuntimeError(
                f"Falha ao consultar dimensão Gestão: HTTP {conf.status_code}"
            )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Falha na consulta da Gestão: {exc}") from exc

    if gestao_ativa:
        sql = """
            SELECT
                d.indicador,
                d.data::text AS data,
                COALESCE(d.base_codigo,'') AS base_codigo,
                COALESCE(NULLIF(g.base_nome,''),d.base_nome,'') AS base_nome,
                COALESCE(NULLIF(g.uf,''),d.uf,'') AS uf,
                COALESCE(NULLIF(g.rm,''),d.rm,'') AS rm,
                COALESCE(NULLIF(g.sc,''),d.sc,'') AS sc,
                COALESCE(NULLIF(g.regional,''),d.regional,'') AS regional,
                COALESCE(d.aging_label,'') AS aging_label,
                COALESCE(d.aging_valor,0)::int AS aging_valor,
                COALESCE(d.acima_3,0)::int AS acima_3,
                COALESCE(d.acima_10,0)::int AS acima_10,
                COALESCE(d.motivo,'') AS motivo,
                SUM(COALESCE(d.pedidos,0))::bigint AS pedidos,
                COUNT(*)::bigint AS linhas
            FROM public.sem_mov_retidos_detalhe AS d
            LEFT JOIN public.jt_gestao_bases AS g
              ON d.base_codigo = g.base_codigo
            WHERE d.data >= %s::date
            GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13
            ORDER BY 2,1,4,10
        """
    else:
        sql = """
            SELECT
                indicador,
                data::text AS data,
                COALESCE(base_codigo, '') AS base_codigo,
                COALESCE(base_nome, '') AS base_nome,
                COALESCE(uf, '') AS uf,
                COALESCE(rm, '') AS rm,
                COALESCE(sc, '') AS sc,
                COALESCE(regional, '') AS regional,
                COALESCE(aging_label, '') AS aging_label,
                COALESCE(aging_valor, 0)::int AS aging_valor,
                COALESCE(acima_3, 0)::int AS acima_3,
                COALESCE(acima_10, 0)::int AS acima_10,
                COALESCE(motivo, '') AS motivo,
                SUM(COALESCE(pedidos, 0))::bigint AS pedidos,
                COUNT(*)::bigint AS linhas
            FROM public.sem_mov_retidos_detalhe
            WHERE data >= %s::date
            GROUP BY
                indicador, data, base_codigo, base_nome, uf, rm, sc, regional,
                aging_label, aging_valor, acima_3, acima_10, motivo
            ORDER BY data, indicador, base_nome, aging_valor
        """
    resp = requests.post(
        f"{_API_URL}/query",
        headers=_headers(),
        json={"sql": sql, "params": [inicio]},
        timeout=_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(
            f"JT API /query retornou HTTP {resp.status_code}: {resp.text[:500]}"
        )

    df = _dataframe_da_resposta(resp.json())
    _status["fonte"] = (
        "PostgreSQL via JT API + Gestão de colaboradores"
        if gestao_ativa else "PostgreSQL via JT API"
    )
    if df.empty:
        return df

    for col in (
        "indicador", "data", "base_codigo", "base_nome", "uf", "rm", "sc",
        "regional", "aging_label", "motivo",
    ):
        df[col] = df[col].fillna("").astype(str).str.strip()

    for col in ("aging_valor", "acima_3", "acima_10", "pedidos", "linhas"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    return df


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


def status_fonte() -> dict:
    return dict(_status)
