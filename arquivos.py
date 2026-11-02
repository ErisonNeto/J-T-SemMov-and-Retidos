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


def _extrair_linhas(payload: Any) -> list[dict]:
    """Aceita os formatos de resposta já usados pela API e mantém compatibilidade."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Resposta inesperada da JT API.")

    for chave in ("rows", "data", "registros", "resultado", "result", "items"):
        valor = payload.get(chave)
        if isinstance(valor, list):
            return valor

    # Algumas versões retornam um objeto único em "data".
    if isinstance(payload.get("data"), dict):
        data = payload["data"]
        for chave in ("rows", "registros", "resultado", "items"):
            valor = data.get(chave)
            if isinstance(valor, list):
                return valor

    raise ValueError("A JT API respondeu, mas não retornou uma lista de linhas reconhecível.")


def _consultar_resumo() -> pd.DataFrame:
    inicio = (datetime.now(TIMEZONE).date() - timedelta(days=_HISTORY_DAYS)).isoformat()
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
    resp.raise_for_status()
    rows = _extrair_linhas(resp.json())
    if not rows:
        return pd.DataFrame(columns=COLUNAS)

    df = pd.DataFrame(rows)
    faltantes = set(COLUNAS) - set(df.columns)
    if faltantes:
        raise ValueError(f"Consulta da JT API sem colunas esperadas: {sorted(faltantes)}")

    df = df[COLUNAS].copy()
    for col in ("indicador", "data", "base_codigo", "base_nome", "uf", "rm", "sc", "regional", "aging_label", "motivo"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    for col in ("aging_valor", "acima_3", "acima_10", "pedidos", "linhas"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    return df


def carregar() -> pd.DataFrame:
    """Carrega um snapshot agregado com cache curto para não repetir a mesma query por endpoint."""
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
            _status["aviso"] = f"Falha ao atualizar a JT API; mantendo o último snapshot válido. {exc}"
        return _cache_df.copy()


def status_fonte() -> dict:
    return dict(_status)
