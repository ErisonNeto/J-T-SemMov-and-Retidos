# -*- coding: utf-8 -*-
"""Exporta REMESSAS UNICAS de um S/C, com aging >10, usando a JT API.

Sem chaves no codigo: usa o .env local do importar_excel.py.
Sem endpoint publico: as remessas so aparecem no XLSX salvo no seu computador.

Exemplo:
  python exportar_remessas_sc.py --sc Pricila --total-painel 778
Ou:
  python exportar_remessas_sc.py --sc Pricila --indicador sem_mov --inicio 2026-11-09 --fim 2026-11-15
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import unicodedata

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

import importar_excel as core
import calcular_percentuais_outlet as outlet

ROOT = Path(__file__).resolve().parent
DESTINO = ROOT / "dados" / "relatorios_outlet"
COLS = ("data", "remessa", "base_codigo", "aging_valor", "pedidos")
TAMANHO_PAGINA = 400


def normalizar(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s.replace("PRISCILA", "PRICILA")


def gestor_alvo(gestores, texto):
    nomes = sorted({
        str(g.get("sc", "")).strip()
        for g in gestores.values()
        if str(g.get("sc", "")).strip() and normalizar(texto) in normalizar(g.get("sc"))
    })
    if len(nomes) == 0:
        todos = sorted({g.get("sc", "") for g in gestores.values() if g.get("sc")})
        raise RuntimeError(
            f"Nenhum S/C corresponde a {texto!r}. "
            "Disponiveis: " + ", ".join(str(x) for x in todos)
        )
    if len(nomes) > 1:
        raise RuntimeError(
            "Ha mais de um S/C correspondente a esse nome. Use --sc com "
            "o nome completo: " + ", ".join(nomes)
        )
    nome = nomes[0]
    codigos = sorted([
        cod for cod, g in gestores.items()
        if str(g.get("sc", "")).strip() == nome
    ])
    if not codigos:
        raise RuntimeError("S/C encontrado mas sem bases vinculadas")
    return nome, codigos


def datas_por_indicador(session, codigos, inicio, fim):
    """Agregado equivalente ao ranking atual: soma de pedidos por dia >10."""
    rows = outlet.consulta(
        session,
        """
        SELECT d.data::text AS data, d.indicador AS indicador,
               SUM(COALESCE(d.pedidos, 0))::bigint AS qtd
        FROM public.sem_mov_retidos_detalhe d
        WHERE d.data >= %s::date AND d.data <= %s::date
          AND d.base_codigo = ANY(%s::text[])
          AND d.indicador IN ('sem_mov', 'retidos')
          AND d.aging_valor > 10
        GROUP BY d.data, d.indicador
        ORDER BY d.data, d.indicador
        """,
        ["data", "indicador", "qtd"],
        [inicio, fim, codigos],
    )
    return {(r["data"][:10], r["indicador"]): int(r.get("qtd") or 0) for r in rows}


def achar_periodos(resumo, inicio, fim, total, indicador="auto"):
    inicio_d, fim_d = date.fromisoformat(inicio), date.fromisoformat(fim)
    datas = [inicio_d + timedelta(days=i) for i in range((fim_d - inicio_d).days + 1)]
    indicadores = ("sem_mov", "retidos") if indicador == "auto" else (indicador,)
    possibilidades = []
    # Busca por janelas de 1 a 14 dias, no maximo.
    for ind in indicadores:
        vals = [resumo.get((dt.isoformat(), ind), 0) for dt in datas]
        for i in range(len(vals)):
            acumulado = 0
            for j in range(i, min(len(vals), i + 14)):
                acumulado += vals[j]
                if acumulado == total:
                    possibilidades.append((ind, datas[i].isoformat(), datas[j].isoformat()))
    return possibilidades


def detalhes(session, codigos, indicador, inicio, fim):
    """Busca somente os registros de aging 11+ e do S/C, com paginacao."""
    registros = []
    deslocamento = 0
    while True:
        linhas = outlet.consulta(
            session,
            """
            SELECT d.data::text AS data, d.remessa AS remessa,
                   d.base_codigo AS base_codigo,
                   d.aging_valor AS aging_valor,
                   d.pedidos AS pedidos
            FROM public.sem_mov_retidos_detalhe d
            WHERE d.data >= %s::date AND d.data <= %s::date
              AND d.base_codigo = ANY(%s::text[])
              AND d.indicador = %s
              AND d.aging_valor > 10
            ORDER BY d.data, d.base_codigo, d.remessa
            LIMIT %s OFFSET %s
            """,
            list(COLS),
            [inicio, fim, codigos, indicador, TAMANHO_PAGINA, deslocamento],
        )
        registros.extend(linhas)
        if len(linhas) < TAMANHO_PAGINA:
            break
        deslocamento += len(linhas)
        if deslocamento >= 250000:
            raise RuntimeError("Volume inesperado >250 mil linhas; restrinja o periodo.")
    return registros


def workbook(registros, gestores, nome, indicador, inicio, fim, alvo):
    por_remessa = {}
    aparicoes = defaultdict(set)
    sem_remessa = 0
    total_pedidos = 0
    for r in registros:
        remessa = str(r.get("remessa") or "").strip()
        d = str(r.get("data") or "")[:10]
        codigo = str(r.get("base_codigo") or "").strip()
        age = int(r.get("aging_valor") or 0)
        qtd = int(r.get("pedidos") or 0)
        total_pedidos += qtd
        if not remessa:
            sem_remessa += 1
            continue
        aparicoes[remessa].add(d)
        atual = por_remessa.get(remessa)
        # Ultima situacao da remessa no periodo, inclusive se mudou de base.
        if atual is None or (d, age, codigo) > atual["ordem"]:
            g = gestores.get(codigo, {})
            por_remessa[remessa] = {
                "ordem": (d, age, codigo),
                "remessa": remessa,
                "data": d,
                "aging": age,
                "base_codigo": codigo,
                "base_nome": g.get("base_nome") or codigo,
                "sc": g.get("sc") or nome,
                "pedidos": qtd,
            }

    wb = Workbook()
    ws = wb.active
    ws.title = "Remessas unicas"
    ws.append([
        "Numero da remessa", "Data mais recente", "Aging (dias)",
        "Codigo da base", "Base", "S/C", "Dias em que apareceu",
    ])
    for r in sorted(por_remessa.values(), key=lambda x: (x["base_nome"], x["remessa"])):
        ws.append([
            r["remessa"], r["data"], r["aging"],
            r["base_codigo"], r["base_nome"], r["sc"],
            len(aparicoes[r["remessa"]]),
        ])
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 19
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 19
    ws.column_dimensions["E"].width = 35
    ws.column_dimensions["F"].width = 38
    ws.column_dimensions["G"].width = 24
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2, min_col=1, max_col=1):
        row[0].number_format = "@"

    conf = wb.create_sheet("Conferencia")
    conf.append(["Campo", "Resultado"])
    conf.append(["S/C", nome])
    conf.append(["Indicador", indicador])
    conf.append(["Aging", "Mais de 10 dias (11 ou mais)"])
    conf.append(["Data inicial", inicio])
    conf.append(["Data final", fim])
    conf.append(["Total exibido no painel (informado)", alvo])
    conf.append(["Soma dos pedidos nos snapshots", total_pedidos])
    conf.append(["Linhas de registros detalhados", len(registros)])
    conf.append(["Remessas distintas no periodo", len(por_remessa)])
    conf.append(["Registros sem numero de remessa", sem_remessa])
    conf.append(["Criterio", "Deduplicacao por numero de remessa; mantida a base/data mais recente"])
    conf.column_dimensions["A"].width = 48
    conf.column_dimensions["B"].width = 90
    for sh in (ws, conf):
        sh.row_dimensions[1].height = 30
        for cel in sh[1]:
            cel.font = Font(name="Calibri", size=11, color="FFFFFF", bold=True)
            cel.fill = PatternFill("solid", fgColor="C3161C")
            cel.alignment = Alignment(vertical="center", wrap_text=True)
        for n, row in enumerate(sh.iter_rows(min_row=2), start=2):
            for cel in row:
                if n % 2 == 0:
                    cel.fill = PatternFill("solid", fgColor="F5F6F8")
    DESTINO.mkdir(parents=True, exist_ok=True)
    rotulo = re.sub(r"[^a-z0-9]+", "_", normalizar(nome).lower())
    p = DESTINO / f"remessas_{rotulo}_{indicador}_{inicio}_a_{fim}_11mais.xlsx"
    wb.save(p)
    return p, total_pedidos, len(por_remessa), sem_remessa


def main():
    parser = argparse.ArgumentParser(
        description="Extrai remessas distintas 11+ dias por S/C e base."
    )
    parser.add_argument("--sc", default="Pricila")
    parser.add_argument("--total-painel", type=int, default=778)
    parser.add_argument("--indicador", choices=["auto", "sem_mov", "retidos"], default="auto")
    parser.add_argument("--inicio", help="AAAA-MM-DD do painel")
    parser.add_argument("--fim", help="AAAA-MM-DD do painel")
    parser.add_argument("--dias-busca", type=int, default=21)
    a = parser.parse_args()
    if bool(a.inicio) != bool(a.fim):
        parser.error("Informe --inicio e --fim juntos.")
    if a.inicio and a.indicador == "auto":
        parser.error("Com --inicio/--fim, informe tambem --indicador sem_mov ou retidos.")

    with requests.Session() as session:
        core.testar_api(session)
        gestores = outlet.gestao_publicada(session)
        nome, codigos = gestor_alvo(gestores, a.sc)
        print(f"S/C: {nome} | {len(codigos)} bases | aging >10 dias", flush=True)

        if a.inicio:
            inicio, fim, ind = a.inicio, a.fim, a.indicador
            if date.fromisoformat(inicio) > date.fromisoformat(fim):
                raise RuntimeError("Data inicial depois da final.")
        else:
            fim = (date.today() - timedelta(days=1)).isoformat()
            inicio = (date.fromisoformat(fim) - timedelta(days=max(a.dias_busca - 1, 0))).isoformat()
            print(f"Procurando os {a.total_painel} do ranking entre {inicio} e {fim}...", flush=True)
            resumo = datas_por_indicador(session, codigos, inicio, fim)
            matches = achar_periodos(resumo, inicio, fim, a.total_painel, a.indicador)
            if not matches:
                print("NAO ENCONTREI periodo com total exato de " + str(a.total_painel) + ".")
                for (d, ind), qtd in sorted(resumo.items(), reverse=True)[:18]:
                    print(f"  {d}  {ind}  {qtd:,} pedidos 11+")
                raise RuntimeError(
                    "Confira as datas e a aba selecionada no dashboard. "
                    "Rode com --inicio AAAA-MM-DD --fim AAAA-MM-DD --indicador sem_mov|retidos. "
                    "Nao vou inventar uma lista que nao confere com o painel."
                )
            if len(matches) != 1:
                print("O total informado ocorre em mais de uma combinacao:")
                for ind, ini, fim_ in matches[:20]:
                    print(f"  {ind} | {ini} a {fim_}")
                raise RuntimeError(
                    "Informe --inicio, --fim e --indicador exatamente como no painel "
                    "para selecionar as remessas corretas."
                )
            ind, inicio, fim = matches[0]

        print(f"Consultando remessas: {ind}, {inicio} a {fim}...", flush=True)
        registros = detalhes(session, codigos, ind, inicio, fim)
        if not registros:
            raise RuntimeError("Nao ha remessas para esse S/C, periodo e aging 11+.")
        arq, soma, unicas, vazias = workbook(
            registros, gestores, nome, ind, inicio, fim, a.total_painel
        )
        print(f"SOMA PEDIDOS DO PAINEL: {soma:,}", flush=True)
        print(f"REMESSAS UNICAS: {unicas:,} | sem remessa: {vazias:,}", flush=True)
        if soma != a.total_painel:
            print(
                f"AVISO: a soma {soma:,} nao coincide com {a.total_painel:,}. "
                "Confirme aba/periodo/filtros do dashboard."
            )
        print(f"EXCEL SALVO: {arq}", flush=True)


if __name__ == "__main__":
    main()
