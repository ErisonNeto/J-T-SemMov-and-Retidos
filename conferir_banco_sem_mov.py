# -*- coding: utf-8 -*-
"""Conferência somente leitura do Sem Mov já gravado no PostgreSQL via JT API.

Uso:
    python conferir_banco_sem_mov.py
Não modifica dados, não mostra chaves e não altera indicadores Retidos.
"""
from __future__ import annotations

import requests
import importar_excel as core


def consultar(session, sql, params=None):
    r = session.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=min(core.API_TIMEOUT, 90),
    )
    if not r.ok:
        raise RuntimeError(f"JT API /query: HTTP {r.status_code}: {r.text[:300]}")
    p = r.json()
    if isinstance(p, list):
        return p
    if isinstance(p, dict):
        for key in ("rows", "data", "result", "resultado", "registros"):
            if isinstance(p.get(key), list):
                return p[key]
    raise RuntimeError("Formato inesperado retornado pela JT API.")


def tabela(titulo, linhas, campos):
    print("\n" + titulo)
    if not linhas:
        print("  Sem linhas.")
        return
    print(" | ".join(campos))
    for row in linhas:
        if isinstance(row, dict):
            print(" | ".join(str(row.get(k, "")) for k in campos))
        else:
            print(" | ".join(str(x) for x in row[:len(campos)]))


def main():
    print("CONFERÊNCIA SEM MOV / POSTGRESQL — SOMENTE LEITURA", flush=True)
    with requests.Session() as s:
        core.testar_api(s)
        dias = consultar(s, """
            SELECT data::text AS data,
                   COUNT(*)::bigint AS registros,
                   SUM(COALESCE(pedidos,0))::bigint AS pedidos,
                   SUM(CASE WHEN UPPER(COALESCE(rm,'')) LIKE '%ORLEAN%' THEN 1 ELSE 0 END)::bigint AS linhas_orlean,
                   SUM(CASE WHEN UPPER(COALESCE(rm,'')) LIKE '%ORLEAN%' AND COALESCE(aging_valor,0)>3
                       THEN COALESCE(pedidos,0) ELSE 0 END)::bigint AS orlean_acima_3,
                   SUM(CASE WHEN UPPER(COALESCE(rm,'')) LIKE '%ORLEAN%' AND COALESCE(aging_valor,0)>10
                       THEN COALESCE(pedidos,0) ELSE 0 END)::bigint AS orlean_acima_10
            FROM public.sem_mov_retidos_detalhe
            WHERE indicador = 'sem_mov'
              AND data BETWEEN '2026-10-19'::date AND '2026-11-02'::date
            GROUP BY data
            ORDER BY data
        """)
        tabela("REGISTROS NO BANCO POR DIA (SEM MOV)", dias,
               ["data", "registros", "pedidos", "linhas_orlean", "orlean_acima_3", "orlean_acima_10"])

        resumo = consultar(s, """
            SELECT COUNT(*)::bigint AS registros,
                   SUM(COALESCE(pedidos,0))::bigint AS pedidos,
                   COUNT(DISTINCT remessa)::bigint AS remessas_unicas,
                   COUNT(DISTINCT base_codigo)::bigint AS bases_distintas,
                   COUNT(*) FILTER (WHERE COALESCE(pedidos,0) <= 0)::bigint AS linhas_sem_pedidos
            FROM public.sem_mov_retidos_detalhe
            WHERE indicador='sem_mov' AND data='2026-10-28'::date
        """)
        tabela("RESUMO DO DIA 28/10 NO BANCO", resumo,
               ["registros", "pedidos", "remessas_unicas", "bases_distintas", "linhas_sem_pedidos"])

        bases = consultar(s, """
            SELECT COALESCE(base_codigo,'') AS base_codigo,
                   COALESCE(rm,'') AS rm,
                   COUNT(*)::bigint AS registros,
                   SUM(COALESCE(pedidos,0))::bigint AS pedidos
            FROM public.sem_mov_retidos_detalhe
            WHERE indicador='sem_mov' AND data='2026-10-28'::date
            GROUP BY base_codigo, rm
            ORDER BY registros DESC, base_codigo
            LIMIT 15
        """)
        tabela("ATÉ 15 BASES DO DIA 28/10 NO BANCO", bases,
               ["base_codigo", "rm", "registros", "pedidos"])
    print("\nNenhum registro foi alterado. NÃO importar o dia 28/10 até revisar este diagnóstico.")


if __name__ == "__main__":
    main()
