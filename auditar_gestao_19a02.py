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
        c["sc_atual"] = c["supervisor"] or c["coordenador"]
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


def comparar_gestoes(antiga, atual, destino):
    linhas = []
    for codigo in sorted(set(antiga) | set(atual)):
        x = antiga.get(codigo) or {}
        y = atual.get(codigo) or {}
        campos = ("rm", "coordenador", "supervisor", "sc_atual", "uf")
        if all(normal(x.get(c)) == normal(y.get(c)) for c in campos):
            continue
        linhas.append({
            "base_codigo": codigo,
            "base_nome_antiga": x.get("base_nome", ""),
            "base_nome_atual": y.get("base_nome", ""),
            "rm_antiga": x.get("rm", ""), "rm_atual": y.get("rm", ""),
            "coordenador_antigo": x.get("coordenador", ""),
            "coordenador_atual": y.get("coordenador", ""),
            "supervisor_antigo": x.get("supervisor", ""),
            "supervisor_atual": y.get("supervisor", ""),
            "sc_antigo": x.get("sc_atual", ""),
            "sc_atual": y.get("sc_atual", ""),
            "uf_antiga": x.get("uf", ""),
            "uf_atual": y.get("uf", ""),
        })
    campos = [
        "base_codigo", "base_nome_antiga", "base_nome_atual",
        "rm_antiga", "rm_atual", "coordenador_antigo", "coordenador_atual",
        "supervisor_antigo", "supervisor_atual", "sc_antigo", "sc_atual",
        "uf_antiga", "uf_atual",
    ]
    with (destino / "mudancas_entre_gestoes.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        wr = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        wr.writeheader()
        wr.writerows(linhas)
    print(f"\nCOMPARAÇÃO ENTRE GESTÕES: {len(linhas)} bases com alteração de atribuição")
    johan = [
        x for x in linhas
        if any(CHAVE_JOHAN in normal(v) for k, v in x.items()
               if "coordenador" in k or "supervisor" in k or k.startswith("sc_"))
    ]
    print(f"  Bases com Johan envolvido em mudança: {len(johan)}")
    for x in johan:
        print(
            f"  {x['base_codigo']} | S/C antigo={x['sc_antigo'][:35]} "
            f"| S/C atual={x['sc_atual'][:35]}"
        )
    print(f"  CSV das mudanças: {destino / 'mudancas_entre_gestoes.csv'}")


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

    # Confirma que a JT API não truncou linhas agrupadas na resposta.
    confer = query(
        session,
        f"""SELECT COUNT(*)::bigint AS registros
            FROM public.sem_mov_retidos_detalhe
            WHERE data = %s::date
              AND indicador IN ('sem_mov','retidos')
              AND (
                 POSITION('ORLEAN' IN UPPER(COALESCE(rm,''))) > 0
                 OR base_codigo IN ({clausula})
              )""",
        [dia.isoformat()],
    )
    if len(confer) != 1:
        raise RuntimeError(f"Dia {dia}: total de conferência não retornou uma linha")
    valor = confer[0]
    total_banco = int(valor["registros"] if isinstance(valor, dict) else valor[0])
    total_agrupado = sum(x["registros"] for x in output)
    if total_banco != total_agrupado:
        raise RuntimeError(
            f"Dia {dia}: consulta agregada retornou {total_agrupado:,} registros, "
            f"mas a contagem independente identificou {total_banco:,}. "
            "A API pode ter truncado o resultado; não confiar na auditoria deste dia."
        )
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


    # Relatório de TODAS as bases: não se limita a nenhum S/C.
    responsaveis = defaultdict(lambda: {"codigos": set(), "registros": 0,
                                        "pedidos": 0, "acima_3": 0, "acima_10": 0})
    desvios = defaultdict(lambda: {"dias": set(), "indicadores": set(), "registros": 0,
                                  "pedidos": 0, "sc_banco": set(), "sc_correto": set(),
                                  "status": set()})
    for codigo, g in gestao.items():
        responsaveis[g["sc_atual"] or "(SEM RESPONSÁVEL)"]["codigos"].add(codigo)
    for r in rows:
        sc = r["sc_gestao_regra_atual"] or "(SEM RESPONSÁVEL)"
        o = responsaveis[sc]
        for k in ("registros", "pedidos", "acima_3", "acima_10"):
            o[k] += r[k]
        if r["status_sc"] != "OK" or r["status_rm"] != "OK" or r["status_uf"] != "OK":
            cod = r["base_codigo"]
            x = desvios[cod]
            x["dias"].add(r["data"])
            x["indicadores"].add(r["indicador"])
            x["registros"] += r["registros"]
            x["pedidos"] += r["pedidos"]
            x["sc_banco"].add(r["sc_banco"])
            x["sc_correto"].add(r["sc_gestao_regra_atual"])
            x["status"].add(r["status_sc"])
            x["status"].add(r["status_rm"])
            x["status"].add(r["status_uf"])

    print("\nTODOS OS RESPONSÁVEIS S/C — por base da Gestão e registros entre 19/10 e 02/11:")
    for sc, x in sorted(responsaveis.items(), key=lambda kv: -kv[1]["registros"]):
        print(
            f"  {sc[:43]:<43} | bases={len(x['codigos']):>4}"
            f" | registros={x['registros']:>10,} | pedidos={x['pedidos']:>10,}"
            f" | >3d={x['acima_3']:>8,} | >10d={x['acima_10']:>8,}"
        )

    print(f"\nTODAS AS BASES COM ATRIBUIÇÃO DIVERGENTE: {len(desvios)}")
    for codigo, x in sorted(desvios.items(), key=lambda kv: -kv[1]["registros"]):
        print(
            f"  {codigo:<11} reg={x['registros']:>9,} "
            f" | banco={','.join(sorted(x['sc_banco']))[:45]}"
            f" | Gestão={','.join(sorted(x['sc_correto']))[:45]}"
            f" | dias={len(x['dias']):>2} | tipos={','.join(sorted(x['status']))}"
        )

    print("\nCADASTROS COM COORDENADOR E SUPERVISOR DIFERENTES:")
    for codigo, g in sorted(gestao.items()):
        if (
            g["coordenador"] and g["supervisor"]
            and normal(g["coordenador"]) != normal(g["supervisor"])
        ):
            print(
                f"  {codigo:<10} {g['base_nome'][:27]:<27}"
                f" Coordenador={g['coordenador'][:38]} | Supervisor={g['supervisor'][:38]}"
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
    p.add_argument("--gestao-antiga", default="",
                   help="Opcional: outra versão da Gestão para comparar mudanças de S/C")
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
    print("Regra padronizada: S/C = Supervisor, ou Coordenador quando Supervisor vazio")

    destino = Path(__file__).resolve().parent / "auditoria_gestao_19a02"
    destino.mkdir(exist_ok=True)
    if args.gestao_antiga:
        antiga_path = Path(args.gestao_antiga).expanduser().resolve()
        if not antiga_path.is_file():
            raise RuntimeError(f"Gestão antiga não encontrada: {antiga_path}")
        antiga, _, _ = ler_gestao(antiga_path)
        comparar_gestoes(antiga, gestao, destino)

    if args.somente_gestao:
        relatorio([], gestao, conflitos, [])
        return

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
            divergentes = [
                r for r in dados
                if (r["status_sc"] != "OK" or r["status_rm"] != "OK"
                    or r["status_uf"] != "OK")
            ]
            csv_out(destino / "todas_as_divergencias.csv", divergentes)
            count = sum(x["registros"] for x in resumo)
            diverg = sum(x["registros"] for x in resumo if x["status_sc"] != "OK")
            print(
                f"  {dia} | {len(resumo):,} grupos | {count:,} registros "
                f"| {diverg:,} sob S/C diferente da Gestão", flush=True,
            )
            dia += timedelta(days=1)

    relatorio(dados, gestao, conflitos, analisados)
    print(f"\nCSV completo: {destino / 'todos_os_dias_e_bases.csv'}")
    print(f"CSV com todas as divergências: {destino / 'todas_as_divergencias.csv'}")
    print("Nenhum dado foi modificado no PostgreSQL.")
    if args.dashboard_url:
        teste_dashboard(args.dashboard_url, gestao)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
