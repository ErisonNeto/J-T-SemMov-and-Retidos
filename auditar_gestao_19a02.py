# -*- coding: utf-8 -*-
"""Auditoria SOMENTE LEITURA: Gestão de colaboradores x PostgreSQL (19/10 a 02/11).

Objetivos:
  - comparar R/M e S/C do banco com a Gestão atual, base a base, dia a dia;
  - evidenciar em quais bases o nome JOHAN aparece no banco ou na Gestão;
  - diferenciar coordenador, supervisor e responsável da região;
  - conferir se um filtro S/C do dashboard retorna linhas de outro S/C.

Este script não importa, atualiza ou exclui qualquer registro.
Resultados são CSVs locais em auditoria_gestao_19a02/.
"""
from __future__ import annotations

import argparse
import csv
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

import importar_excel as core

INICIO = date(2026, 10, 19)
FIM = date(2026, 11, 2)
CHAVE_JOHAN = "JOHAN"
CHAVE_MARCELO = "MARCELO HENRIQUE COSTA"

CAMPOS = [
    "data", "indicador", "base_codigo", "base_nome_banco",
    "base_nome_gestao", "uf_banco", "uf_gestao",
    "rm_banco", "rm_gestao", "sc_banco",
    "sc_gestao_regra_atual", "coordenador_gestao", "supervisor_gestao",
    "registros", "pedidos", "acima_3", "acima_10",
    "status_sc", "status_rm", "status_uf", "gestao_duplicada",
]
SQL_COLS = [
    "indicador", "data", "base_codigo", "base_nome", "uf", "rm", "sc",
    "registros", "pedidos", "acima_3", "acima_10",
]


def normal(s):
    if s is None:
        return ""
    x = unicodedata.normalize("NFKD", str(s))
    x = "".join(c for c in x if not unicodedata.combining(c))
    return " ".join(x.upper().split())


def strvalor(v):
    if v is None or pd.isna(v):
        return ""
    return str(v).strip()


def extrair_linhas(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for nome in ("rows", "data", "result", "resultado", "registros", "items"):
            if isinstance(obj.get(nome), list):
                return obj[nome]
    raise RuntimeError("JT API: resposta sem lista de linhas reconhecível.")


def query(session, sql, params=None):
    r = session.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=max(core.API_TIMEOUT, 120),
    )
    if not r.ok:
        raise RuntimeError(f"JT API /query HTTP {r.status_code}: {r.text[:350]}")
    return extrair_linhas(r.json())


def sql_text(s):
    return "'" + str(s).replace("'", "''") + "'"


def ler_gestao(path):
    df = pd.read_excel(path, dtype=str)
    obrigatorios = [
        "Estação No.", "Estado(UF)", "Responsável da Região",
        "Coordenador", "Supervisor",
    ]
    faltando = [c for c in obrigatorios if c not in df.columns]
    if faltando:
        raise RuntimeError(f"Gestão sem colunas obrigatórias: {faltando}")

    por_codigo = defaultdict(list)
    for _, r in df.iterrows():
        codigo = core.norm_codigo(r.get("Estação No."))
        if not codigo or normal(codigo) in {"NAN", "NONE"}:
            continue
        c = {
            "base_codigo": codigo,
            "base_nome": strvalor(r.get("Nome da base")),
            "uf": strvalor(r.get("Estado(UF)")),
            "rm": strvalor(r.get("Responsável da Região")),
            "coordenador": strvalor(r.get("Coordenador")),
            "supervisor": strvalor(r.get("Supervisor")),
        }
        # Reproduz exatamente a regra do importador: Coordenador ou Supervisor.
        c["sc_atual"] = c["coordenador"] or c["supervisor"]
        por_codigo[codigo].append(c)

    escolhidos = {codigo: lista[-1] for codigo, lista in por_codigo.items()}
    conflitantes = {}
    for codigo, lista in por_codigo.items():
        escolhas = {
            tuple(normal(l[k]) for k in ("rm", "sc_atual", "coordenador", "supervisor", "uf"))
            for l in lista
        }
        if len(escolhas) > 1:
            conflitantes[codigo] = lista
    return escolhidos, por_codigo, conflitantes


def consulta_dia(session, dia, codigos):
    # Inclui bases vinculadas ao Orlean NO BANCO e bases presentes na Gestão.
    # Assim, divergências de RM não desaparecem do diagnóstico.
    clausula = ", ".join(sql_text(c) for c in sorted(codigos))
    sql = f"""
        SELECT indicador,
               data::text AS data,
               COALESCE(base_codigo,'') AS base_codigo,
               COALESCE(base_nome,'') AS base_nome,
               COALESCE(uf,'') AS uf,
               COALESCE(rm,'') AS rm,
               COALESCE(sc,'') AS sc,
               COUNT(*)::bigint AS registros,
               SUM(COALESCE(pedidos,0))::bigint AS pedidos,
               SUM(CASE WHEN COALESCE(aging_valor,0)>3
                        THEN COALESCE(pedidos,0) ELSE 0 END)::bigint AS acima_3,
               SUM(CASE WHEN COALESCE(aging_valor,0)>10
                        THEN COALESCE(pedidos,0) ELSE 0 END)::bigint AS acima_10
        FROM public.sem_mov_retidos_detalhe
        WHERE data = %s::date
          AND indicador IN ('sem_mov','retidos')
          AND (
             POSITION('ORLEAN' IN UPPER(COALESCE(rm,''))) > 0
             OR base_codigo IN ({clausula})
          )
        GROUP BY indicador, data, base_codigo, base_nome, uf, rm, sc
        ORDER BY indicador, base_codigo, sc
    """
    output = []
    for row in query(session, sql, [dia.isoformat()]):
        if isinstance(row, dict):
            record = {k: row.get(k) for k in SQL_COLS}
        else:
            if len(row) != len(SQL_COLS):
                raise RuntimeError(
                    f"Dia {dia}: resposta de {len(row)} campos, esperado {len(SQL_COLS)}"
                )
            record = dict(zip(SQL_COLS, row))
        for key in ("registros", "pedidos", "acima_3", "acima_10"):
            record[key] = int(record[key] or 0)
        for key in ("indicador", "data", "base_codigo", "base_nome", "uf", "rm", "sc"):
            record[key] = strvalor(record[key])
        output.append(record)
    return output


def status_sc(banco_sc, g):
    if g is None:
        return "SEM_CODIGO_NA_GESTAO"
    b, esperado = normal(banco_sc), normal(g["sc_atual"])
    if b == esperado:
        return "OK"
    if b and b == normal(g["supervisor"]) and normal(g["coordenador"]):
        return "BANCO_SUPERVISOR_GESTAO_PRIORIZA_COORDENADOR"
    if b and b == normal(g["coordenador"]) and not esperado:
        return "BANCO_COORDENADOR_GESTAO_VAZIA"
    if not b and esperado:
        return "SC_VAZIO_NO_BANCO"
    return "SC_DIVERGENTE"


def analisar(rows, gestao, conflitantes):
    out = []
    for r in rows:
        codigo = core.norm_codigo(r["base_codigo"])
        g = gestao.get(codigo)
        sc = status_sc(r["sc"], g)
        if g:
            rm = "OK" if normal(r["rm"]) == normal(g["rm"]) else "RM_DIVERGENTE"
            uf = "OK" if normal(r["uf"]) == normal(g["uf"]) else "UF_DIVERGENTE"
        else:
            rm, uf = "SEM_CODIGO_NA_GESTAO", "SEM_CODIGO_NA_GESTAO"

        out.append({
            "data": r["data"],
            "indicador": r["indicador"],
            "base_codigo": codigo,
            "base_nome_banco": r["base_nome"],
            "base_nome_gestao": g["base_nome"] if g else "",
            "uf_banco": r["uf"],
            "uf_gestao": g["uf"] if g else "",
            "rm_banco": r["rm"],
            "rm_gestao": g["rm"] if g else "",
            "sc_banco": r["sc"],
            "sc_gestao_regra_atual": g["sc_atual"] if g else "",
            "coordenador_gestao": g["coordenador"] if g else "",
            "supervisor_gestao": g["supervisor"] if g else "",
            "registros": r["registros"],
            "pedidos": r["pedidos"],
            "acima_3": r["acima_3"],
            "acima_10": r["acima_10"],
            "status_sc": sc,
            "status_rm": rm,
            "status_uf": uf,
            "gestao_duplicada": "CONFLITO" if codigo in conflitantes else "",
        })
    return out


def csv_out(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=CAMPOS, delimiter=";", extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def relatorio(rows, gestao, conflitantes, dia_analisados):
    print("\n" + "=" * 90)
    print("RESUMO POR DIA / INDICADOR — COMPARAÇÃO À GESTÃO INFORMADA")
    resumo = defaultdict(lambda: Counter())
    for r in rows:
        c = resumo[(r["data"], r["indicador"])]
        c["registros"] += r["registros"]
        c["pedidos"] += r["pedidos"]
        c["grupos"] += 1
        if r["status_sc"] != "OK":
            c["sc_grupos_divergentes"] += 1
            c["sc_registros_divergentes"] += r["registros"]
        if r["status_rm"] != "OK":
            c["rm_grupos_divergentes"] += 1
            c["rm_registros_divergentes"] += r["registros"]
        if r["status_sc"] == "SEM_CODIGO_NA_GESTAO":
            c["sem_gestao_grupos"] += 1
    for dia in dia_analisados:
        for ind in ("sem_mov", "retidos"):
            c = resumo.get((dia, ind), Counter())
            print(
                f"  {dia} {ind:8} | registros={c['registros']:,} "
                f"| pedidos={c['pedidos']:,} | grupos S/C divergentes={c['sc_grupos_divergentes']}"
                f" ({c['sc_registros_divergentes']:,} registros)"
                f" | R/M divergente={c['rm_grupos_divergentes']}"
                f" | sem Gestão={c['sem_gestao_grupos']}"
            )

    esperado_johan = sorted(
        [
            (codigo, g) for codigo, g in gestao.items()
            if CHAVE_JOHAN in normal(g["coordenador"])
               or CHAVE_JOHAN in normal(g["supervisor"])
               or CHAVE_JOHAN in normal(g["sc_atual"])
        ],
        key=lambda p: p[0],
    )
    print("\nBASES DO JOHAN NA GESTÃO (Coordenador / Supervisor):")
    if not esperado_johan:
        print("  Johan não consta nas colunas Coordenador/Supervisor da Gestão escolhida.")
    for codigo, g in esperado_johan:
        print(
            f"  {codigo:<10} {g['base_nome'][:32]:<32} "
            f"coord={g['coordenador'][:34]:<34} | supervisor={g['supervisor'][:34]}"
        )

    filtrar_johan = [
        r for r in rows
        if CHAVE_JOHAN in normal(r["sc_banco"])
        or CHAVE_JOHAN in normal(r["coordenador_gestao"])
        or CHAVE_JOHAN in normal(r["supervisor_gestao"])
    ]
    por_base = defaultdict(lambda: {"registros": 0, "pedidos": 0, "acima_3": 0,
                                    "acima_10": 0, "sc_bd": set(), "status": set(), "datas": set()})
    for r in filtrar_johan:
        x = por_base[r["base_codigo"]]
        for key in ("registros", "pedidos", "acima_3", "acima_10"):
            x[key] += r[key]
        x["sc_bd"].add(r["sc_banco"])
        x["status"].add(r["status_sc"])
        x["datas"].add(r["data"])
    print("\nJOHAN: BASES COM REGISTROS DE 19/10 A 02/11")
    if not por_base:
        print("  Nenhuma ocorrência do Johan na Gestão selecionada ou no banco para o intervalo.")
    for cod, x in sorted(por_base.items(), key=lambda kv: -kv[1]["acima_3"]):
        print(
            f"  {cod:<10} >3d={x['acima_3']:>8,}  >10d={x['acima_10']:>8,}"
            f"  reg={x['registros']:>8,}  S/C banco={','.join(sorted(x['sc_bd']))[:75]}"
            f"  status={','.join(sorted(x['status']))}"
        )
    print(f"\nCódigos duplicados com conflito na Gestão: {len(conflitantes)}")
    for cod, lista in sorted(conflitantes.items()):
        print(f"  {cod}: " + " | ".join(f"{x['coordenador']}/{x['supervisor']}" for x in lista)[:140])
    print("\nATENÇÃO: comparar histórico com uma única Gestão atual NÃO comprova troca de responsável")
    print("retroativa. Reatribuição de períodos passados exige confirmação da Gestão vigente em cada data.")


def teste_dashboard(url, gestao):
    nomes = sorted({
        g["sc_atual"] for g in gestao.values()
        if CHAVE_MARCELO in normal(g["sc_atual"])
    })
    if not nomes:
        print("\nTESTE DASHBOARD: Marcelo não localizado na coluna S/C da Gestão.")
        return
    sc = nomes[0]
    try:
        r = requests.get(
            url.rstrip("/") + "/api/dashboard",
            params={
                "indicador": "sem_mov", "data_inicial": "2026-10-26",
                "data_final": "2026-11-01", "sc": sc,
            },
            timeout=90,
        )
        r.raise_for_status()
        d = r.json()
        divergentes = [
            x for x in d.get("top_bases", [])
            if normal(x.get("sc")) != normal(sc)
        ]
        sc_div = [
            x for x in d.get("top_sc", [])
            if normal(x.get("sc")) != normal(sc)
        ]
        print("\nTESTE DASHBOARD: filtro S/C =", sc)
        print("  q3=", d.get("metrics", {}).get("acima_3", {}).get("atual"),
              "| bases=", len(d.get("top_bases", [])))
        print("  Bases de OUTRO S/C no resultado:", len(divergentes))
        print("  Ranking de OUTRO S/C no resultado:", len(sc_div))
        if divergentes or sc_div:
            print("  FALHA DE FILTRO NO BACKEND/API. Não confiar no dashboard até corrigir.")
        else:
            print("  Backend respeitou o filtro. Se a tela exibir Johan, investigar JS/cache/UI.")
    except Exception as exc:
        print("\nTESTE DASHBOARD não pôde executar:", exc)


def main():
    p = argparse.ArgumentParser(description="Auditar bases e S/C da Gestão vs banco entre 19/10 e 02/11")
    p.add_argument("--gestao", default=str(core.GESTAO_PADRAO),
                   help="Caminho para a Gestão que deve servir de referência")
    p.add_argument("--somente-gestao", action="store_true",
                   help="Lista as bases Johan da Gestão sem consultar a JT API")
    p.add_argument("--dashboard-url", default="",
                   help="Opcional: http://127.0.0.1:8003 para verificar filtro S/C")
    args = p.parse_args()

    gestao_path = Path(args.gestao).expanduser().resolve()
    if not gestao_path.is_file():
        raise RuntimeError(f"Gestão não encontrada: {gestao_path}")
    gestao, todas, conflitos = ler_gestao(gestao_path)
    print(f"GESTÃO: {gestao_path.name} | {len(gestao)} códigos únicos "
          f"| {sum(len(v)-1 for v in todas.values())} códigos repetidos "
          f"| {len(conflitos)} com conflito de atribuição")
    print("Regra atual do importador: S/C = Coordenador, ou Supervisor quando Coordenador vazio")

    if args.somente_gestao:
        relatorio([], gestao, conflitos, [])
        return

    destino = Path(__file__).resolve().parent / "auditoria_gestao_19a02"
    destino.mkdir(exist_ok=True)
    codigos = set(gestao)
    if not codigos:
        raise RuntimeError("Gestão vazia; auditoria abortada")
    dados = []
    analisados = []
    with requests.Session() as s:
        core.testar_api(s)
        dia = INICIO
        while dia <= FIM:
            registros = consulta_dia(s, dia, codigos)
            resumo = analisar(registros, gestao, conflitos)
            dados.extend(resumo)
            analisados.append(dia.isoformat())
            csv_out(destino / "todos_os_dias_e_bases.csv", dados)
            johan = [r for r in dados if (
                CHAVE_JOHAN in normal(r["sc_banco"]) or
                CHAVE_JOHAN in normal(r["coordenador_gestao"]) or
                CHAVE_JOHAN in normal(r["supervisor_gestao"])
            )]
            csv_out(destino / "somente_johan.csv", johan)
            count = sum(x["registros"] for x in resumo)
            diverg = sum(x["registros"] for x in resumo if x["status_sc"] != "OK")
            print(
                f"  {dia} | {len(resumo):,} grupos | {count:,} registros "
                f"| {diverg:,} sob S/C diferente da Gestão", flush=True,
            )
            dia += timedelta(days=1)

    relatorio(dados, gestao, conflitos, analisados)
    print(f"\nCSV completo: {destino / 'todos_os_dias_e_bases.csv'}")
    print(f"CSV Johan: {destino / 'somente_johan.csv'}")
    print("Nenhum dado foi modificado no PostgreSQL.")
    if args.dashboard_url:
        teste_dashboard(args.dashboard_url, gestao)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
