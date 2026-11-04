# -*- coding: utf-8 -*-
"""Conferir 11 bases informadas para Johan, sem alterar o PostgreSQL.

A planilha-pivô externa traz faixas 1/2/3/4 dias. Este script mostra
remessas (linhas) e pedidos por dia/base/faixa, para permitir comparar
somente snapshots da mesma DATA e da mesma ORIGEM.

Não faz DELETE, UPDATE, UPSERT ou importação.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import importar_excel as core

BASES_JOHAN = [
    "CPN-PA", "CRA-PA", "CST-PA", "F CST-PA", "F IGA-PA",
    "MAR-PA", "PRM-PA", "SIP-PA", "SLP-PA", "STA-PA", "VGA-PA",
]
INICIO = "2026-10-19"
FIM = "2026-11-04"


def chave_nome(s):
    txt = unicodedata.normalize("NFKD", str(s or ""))
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]+", "", txt.upper())


def vt(v):
    if v is None or pd.isna(v):
        return ""
    return str(v).strip()


def query(session, sql, params=None):
    r = session.post(
        f"{core.API_URL}/query", headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=max(90, core.API_TIMEOUT),
    )
    if not r.ok:
        raise RuntimeError(f"JT API HTTP {r.status_code}: {r.text[:280]}")
    obj = r.json()
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("rows", "data", "result", "resultado", "registros"):
            if isinstance(obj.get(key), list):
                return obj[key]
    raise RuntimeError("Formato de linhas não reconhecido na resposta da JT API")


def dict_rows(raw, names):
    result = []
    for row in raw:
        if isinstance(row, dict):
            result.append({k: row.get(k) for k in names})
        elif isinstance(row, (tuple, list)) and len(row) == len(names):
            result.append(dict(zip(names, row)))
        else:
            raise RuntimeError("JT API devolveu estrutura de linha diferente da esperada")
    return result


def ler_gestao(path):
    df = pd.read_excel(path, dtype=str)
    campos = ["Estação No.", "Nome da base", "Responsável da Região",
              "Coordenador", "Supervisor", "Estado(UF)"]
    faltando = [c for c in campos if c not in df.columns]
    if faltando:
        raise RuntimeError(f"Gestão sem campos: {faltando}")
    out = defaultdict(list)
    for _, r in df.iterrows():
        nome = vt(r["Nome da base"])
        chave = chave_nome(nome)
        if chave not in {chave_nome(x) for x in BASES_JOHAN}:
            continue
        out[chave].append({
            "base_codigo": core.norm_codigo(r["Estação No."]),
            "base_nome": nome,
            "rm": vt(r["Responsável da Região"]),
            "coordenador": vt(r["Coordenador"]),
            "supervisor": vt(r["Supervisor"]),
            "sc_operacional": vt(r["Supervisor"]) or vt(r["Coordenador"]),
            "uf": vt(r["Estado(UF)"]),
            "origem": "Gestão XLSX",
        })
    return out


def csv_file(path, rows, campos):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def procurar_nomes_banco(s):
    nomes_sql = ", ".join(
        "'" + chave_nome(x).replace("'", "''") + "'" for x in BASES_JOHAN
    )
    # Encontra código mesmo se faltar/mudar cadastro da Gestão.
    sql = f"""
        SELECT base_codigo, base_nome,
               MIN(data)::text AS primeira_data,
               MAX(data)::text AS ultima_data,
               COUNT(*)::bigint AS remessas
        FROM public.sem_mov_retidos_detalhe
        WHERE indicador='sem_mov'
          AND data BETWEEN %s::date AND %s::date
          AND UPPER(REGEXP_REPLACE(COALESCE(base_nome,''),'[^A-Za-z0-9]+','','g'))
                 IN ({nomes_sql})
        GROUP BY base_codigo, base_nome
        ORDER BY base_nome, base_codigo
    """
    return dict_rows(
        query(s, sql, [INICIO, FIM]),
        ["base_codigo", "base_nome", "primeira_data", "ultima_data", "remessas"],
    )


def buscar_dimensao(s):
    exists = query(s, """
        SELECT COUNT(*)::int AS qtd
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'jt_gestao_bases'
    """)
    first = exists[0]
    qtd = int(first["qtd"] if isinstance(first, dict) else first[0])
    if not qtd:
        return {}
    campos = ["base_codigo", "base_nome", "rm", "sc", "coordenador", "supervisor", "uf"]
    rows = dict_rows(
        query(s, """
            SELECT base_codigo, base_nome, rm, sc, coordenador, supervisor, uf
            FROM public.jt_gestao_bases
        """),
        campos,
    )
    return {vt(x["base_codigo"]): x for x in rows}


def indicadores_por_dia(s, codigos):
    if not codigos:
        return []
    codigos_sql = ", ".join("'" + str(c).replace("'", "''") + "'" for c in sorted(codigos))
    campos = [
        "data", "base_codigo", "base_nome",
        "aging_dias", "remessas", "pedidos", "rm_bruto", "sc_bruto",
    ]
    result = dict_rows(
        query(s, f"""
            SELECT data::text AS data, base_codigo,
                   MAX(COALESCE(base_nome,'')) AS base_nome,
                   COALESCE(aging_valor,0)::int AS aging_dias,
                   COUNT(*)::bigint AS remessas,
                   SUM(COALESCE(pedidos,0))::bigint AS pedidos,
                   STRING_AGG(DISTINCT NULLIF(rm,''), ' | ') AS rm_bruto,
                   STRING_AGG(DISTINCT NULLIF(sc,''), ' | ') AS sc_bruto
            FROM public.sem_mov_retidos_detalhe
            WHERE indicador='sem_mov'
              AND data BETWEEN %s::date AND %s::date
              AND base_codigo IN ({codigos_sql})
            GROUP BY data, base_codigo, COALESCE(aging_valor,0)
            ORDER BY data, base_codigo, aging_dias
        """, [INICIO, FIM]),
        campos,
    )
    for r in result:
        r["aging_dias"] = int(r["aging_dias"] or 0)
        r["remessas"] = int(r["remessas"] or 0)
        r["pedidos"] = int(r["pedidos"] or 0)
    return result


def main():
    ap = argparse.ArgumentParser(description="Conferir 11 bases Johan em Gestão e no banco, 19/10 a 04/11")
    ap.add_argument("--gestao", default=str(core.GESTAO_PADRAO))
    ap.add_argument("--dia-planilha", default="",
                    help="Data REAL da planilha-pivô, ex.: 2026-11-03")
    args = ap.parse_args()
    if args.dia_planilha:
        parsed = date.fromisoformat(args.dia_planilha)
        if not date.fromisoformat(INICIO) <= parsed <= date.fromisoformat(FIM):
            raise RuntimeError("Data da planilha fora do intervalo auditado")
    path = Path(args.gestao).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Gestão não encontrada: {path}")
    gestao = ler_gestao(path)
    pasta = Path(__file__).resolve().parent / "auditoria_gestao_19a02"
    pasta.mkdir(exist_ok=True)

    with requests.Session() as session:
        core.testar_api(session)
        encontrados = procurar_nomes_banco(session)
        dimensao = buscar_dimensao(session)
        banco_nome = defaultdict(list)
        for item in encontrados:
            banco_nome[chave_nome(item["base_nome"])].append(item)

        relatorio = []
        codigos = set()
        print("\n11 BASES INFORMADAS PARA JOHAN — GESTÃO x TABELA DE RESPONSÁVEIS x BANCO")
        print("=" * 108)
        for nome in BASES_JOHAN:
            k = chave_nome(nome)
            gest = gestao.get(k, [])
            db = banco_nome.get(k, [])
            candidatos = set()
            candidatos.update(v["base_codigo"] for v in gest if v["base_codigo"])
            candidatos.update(vt(v["base_codigo"]) for v in db if vt(v["base_codigo"]))
            if not candidatos:
                print(f"  {nome:<12} NÃO LOCALIZADA NA GESTÃO NEM NAS REMESSAS")
            for codigo in sorted(candidatos):
                codigos.add(codigo)
                g = next((x for x in gest if x["base_codigo"] == codigo), None)
                d = dimensao.get(codigo)
                origem = next((x for x in db if vt(x["base_codigo"]) == codigo), None)
                reg = {
                    "base_informada": nome,
                    "codigo": codigo,
                    "nome_gestao": g["base_nome"] if g else "",
                    "rm_gestao": g["rm"] if g else "",
                    "coordenador_gestao": g["coordenador"] if g else "",
                    "supervisor_gestao": g["supervisor"] if g else "",
                    "sc_gestao": g["sc_operacional"] if g else "",
                    "nome_dimensao": vt(d["base_nome"]) if d else "",
                    "rm_dimensao": vt(d["rm"]) if d else "",
                    "sc_dimensao": vt(d["sc"]) if d else "",
                    "remessas_no_historico": int(origem["remessas"]) if origem else 0,
                    "status": "OK" if d and "JOHAN" in str(d["sc"]).upper()
                              and "ORLEAN" in str(d["rm"]).upper()
                              else "CONFERIR",
                }
                relatorio.append(reg)
                print(
                    f"  {nome:<12} {codigo:<9}"
                    f" Gestão S/C={reg['sc_gestao'][:28]:<28}"
                    f" Gestão RM={reg['rm_gestao'][:24]:<24}"
                    f" Dimensão S/C={reg['sc_dimensao'][:28]:<28}"
                    f" Dimensão RM={reg['rm_dimensao'][:23]:<23}"
                    f" {reg['status']}"
                )
            if len(candidatos) > 1:
                print(f"    ATENÇÃO: {nome} encontrou {len(candidatos)} códigos diferentes. NÃO corrigir por nome.")
        campos_relatorio = [
            "base_informada", "codigo", "nome_gestao", "rm_gestao",
            "coordenador_gestao", "supervisor_gestao", "sc_gestao",
            "nome_dimensao", "rm_dimensao", "sc_dimensao",
            "remessas_no_historico", "status",
        ]
        csv_file(pasta / "bases_informadas_johan_verificacao.csv", relatorio, campos_relatorio)

        dados = indicadores_por_dia(session, codigos)
        csv_file(pasta / "bases_informadas_johan_faixas_por_dia.csv", dados,
                 ["data", "base_codigo", "base_nome", "aging_dias",
                  "remessas", "pedidos", "rm_bruto", "sc_bruto"])
        agrup = defaultdict(lambda: {"1":0,"2":0,"3":0,"4":0,">4":0,"total":0,">3":0,"pedidos>3":0})
        for row in dados:
            item = agrup[(row["data"],row["base_codigo"])]
            age = row["aging_dias"]
            if 1 <= age <= 4:
                item[str(age)] += row["remessas"]
            if age > 4:
                item[">4"] += row["remessas"]
            if age > 3:
                item[">3"] += row["remessas"]
                item["pedidos>3"] += row["pedidos"]
            item["total"] += row["remessas"]

        datas = sorted({k[0] for k in agrup})
        print("\nSOMATÓRIO DOS CÓDIGOS ENCONTRADOS, POR DIA (SEM MOV / REMESSAS)")
        print("DATA       | D1 | D2 | D3 | D4 | >4D | TOTAL | >3D | PEDIDOS >3D")
        for dia in datas:
            t = {k:0 for k in ("1","2","3","4",">4","total",">3","pedidos>3")}
            for (dt,cd), arr in agrup.items():
                if dt == dia:
                    for x in t:
                        t[x] += arr[x]
            print(
                f"{dia} | {t['1']:>6,} | {t['2']:>6,} | {t['3']:>6,}"
                f" | {t['4']:>6,} | {t['>4']:>6,} | {t['total']:>7,}"
                f" | {t['>3']:>6,} | {t['pedidos>3']:>11,}"
            )
        if args.dia_planilha:
            dia = args.dia_planilha
            t = {x:sum(v[x] for (dt,_),v in agrup.items() if dt==dia)
                 for x in ("1","2","3","4",">4","total",">3","pedidos>3")}
            print(f"\nPLANILHA INFORMADA (sem data comprovada): D1=20 D2=67 D3=12 D4=9 TOTAL=108")
            print(f"BANCO EM {dia}: D1={t['1']} D2={t['2']} D3={t['3']} D4={t['4']} "
                  f">4={t['>4']} TOTAL={t['total']}")
            print("ATENÇÃO: igualdade só é esperada se ambas as fontes têm MESMO dia, "
                  "mesmo recorte e mesma definição de tempo.")
        print("\nCSVs gerados em:", pasta)
        print("NENHUM REGISTRO FOI ALTERADO.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr)
        sys.exit(1)
