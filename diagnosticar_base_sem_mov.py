# -*- coding: utf-8 -*-
"""Diagnóstico SOMENTE LEITURA das colunas de base em arquivos Sem Mov.

Uso:
  python diagnosticar_base_sem_mov.py ".\\dados\\sem_mov\\sem movimentação 28.10.xlsx"
Não acessa a API nem grava no PostgreSQL.
"""
from __future__ import annotations

import argparse
import collections
import zipfile
from pathlib import Path

import importar_excel as core


def linha_valores(row, strings):
    saida = {}
    for cell in row.findall(core.NS + "c"):
        col = core._col(cell.get("r", ""))
        if col:
            valor = core._valor_cell(cell, strings)
            if valor:
                saida[col] = str(valor).strip()
    return saida


def analisar_aba(z, sheet, strings, gestao, limite):
    header_rows = []
    headers = {}
    cab_linha = 1
    maior_pontos = -1
    estat = collections.defaultdict(lambda: {
        "preenchidos": 0, "match": 0, "orlean": 0,
        "exemplos": [], "nao_match": [], "codigos": set()
    })
    linhas_dados = 0
    with z.open(sheet) as f:
        for _, row in core.etree.iterparse(
            f, events=("end",), tag=core.NS + "row"
        ):
            nr = int(row.get("r") or 0)
            vals = linha_valores(row, strings)
            if nr <= 20:
                encontrados = [
                    (col, val) for col, val in vals.items()
                    if "base" in core.normalizar(val)
                    or "estac" in core.normalizar(val)
                    or "codigo" in core.normalizar(val)
                    or "remessa" in core.normalizar(val)
                    or "aging" in core.normalizar(val)
                    or "pedido" in core.normalizar(val)
                ]
                pontos = len(encontrados)
                if pontos > maior_pontos:
                    maior_pontos = pontos
                    cab_linha = nr
                    headers = vals.copy()
                header_rows.append((nr, encontrados))
                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]
                continue

            if not vals:
                row.clear()
                continue

            linhas_dados += 1
            for col, val in vals.items():
                cod = core.norm_codigo(val)
                if not cod or len(cod) > 15:
                    continue
                t = estat[col]
                t["preenchidos"] += 1
                if len(t["exemplos"]) < 3 and cod not in t["exemplos"]:
                    t["exemplos"].append(cod)
                if cod in gestao:
                    t["match"] += 1
                    t["codigos"].add(cod)
                    if "ORLEAN" in str(gestao[cod].get("rm", "")).upper():
                        t["orlean"] += 1
                elif len(t["nao_match"]) < 3 and cod not in t["nao_match"]:
                    t["nao_match"].append(cod)

            row.clear()
            while row.getprevious() is not None:
                del row.getparent()[0]
            if linhas_dados >= limite:
                break

    print(f"  ABA: {sheet} | XML {z.getinfo(sheet).file_size/1024**2:.1f} MB "
          f"| linhas avaliadas: {linhas_dados:,}")
    print("  Cabeçalhos relacionados:")
    vistos = set()
    for nr, itens in header_rows:
        if not itens:
            continue
        for col, nome in itens:
            par = (col, nome)
            if par in vistos:
                continue
            vistos.add(par)
            print(f"    Linha {nr}, coluna {col}: {nome[:90]}")
            if len(vistos) >= 25:
                break
        if len(vistos) >= 25:
            break

    ordenadas = sorted(
        estat.items(),
        key=lambda item: (item[1]["match"], item[1]["orlean"]),
        reverse=True,
    )
    print("  Colunas com possível código de base (primeiras posições):")
    for col, t in ordenadas[:16]:
        if t["match"] == 0:
            continue
        taxa = 100 * t["match"] / max(1, t["preenchidos"])
        print(
            f"    {col:>3} | na Gestão {t['match']:,}/{t['preenchidos']:,}"
            f" ({taxa:.1f}%) | Orlean {t['orlean']:,}"
            f" | códigos distintos na Gestão {len(t['codigos'])}"
        )
        print(f"        exemplos: {', '.join(t['exemplos'])}")
        print(f"        fora da Gestão: {', '.join(t['nao_match'])}")
    for col, t in sorted(estat.items()):
        nome = headers.get(col, "")
        if "base" in core.normalizar(nome) and t["match"] == 0:
            print(f"    ALERTA: coluna {col}, cabeçalho {nome!r}, "
                  "sem correspondência na amostra")
    print()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("arquivo", help="Caminho XLSX Sem Mov")
    p.add_argument("--amostra", type=int, default=3000)
    args = p.parse_args()
    path = Path(args.arquivo)
    if not path.is_file():
        raise FileNotFoundError(path)

    gestao = core.carregar_gestao(core.GESTAO_PADRAO)
    print(f"DIAGNÓSTICO SEM MOV | arquivo: {path.name}")
    print(f"Gestão: {len(gestao)} códigos | SOMENTE LEITURA")
    with zipfile.ZipFile(path) as z:
        strings = core._shared_strings(z)
        abas = core._sheet_paths(z)
        print(f"Abas detectadas: {len(abas)}")
        for aba in abas:
            analisar_aba(z, aba, strings, gestao, max(100, args.amostra))
    try:
        aba, linha, mapa = core.localizar_layout_sem_mov(path)
        print("Layout atualmente escolhido pelo importador:")
        print(f"  Aba: {aba} | cabeçalho linha {linha}")
        for campo, coluna in sorted(mapa.items()):
            print(f"  {campo:16}: {coluna}")
    except Exception as exc:
        print(f"Erro do seletor de layout: {type(exc).__name__}: {exc}")
    print("\nNenhum dado foi enviado ao banco.")


if __name__ == "__main__":
    main()
