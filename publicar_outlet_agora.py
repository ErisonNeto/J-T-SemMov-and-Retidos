# -*- coding: utf-8 -*-
"""Publica somente o denominador de Outlet (J+M) sem reimportar remessas.

Uso no Windows:
  .\.venv\Scripts\python.exe .\publicar_outlet_agora.py --dry-run
  .\.venv\Scripts\python.exe .\publicar_outlet_agora.py --publicar

O importador importar_tres_auto.py ja publica o volume nas cargas futuras.
Este atalho e para o primeiro envio do historico sem reler 200 mil remessas.
"""
import argparse
from pathlib import Path
import requests

import importar_excel as core
import calcular_percentuais_outlet as outlet


def main():
    ap = argparse.ArgumentParser(description="Publica volumetria J+M por data/base.")
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--dry-run", action="store_true")
    modo.add_argument("--publicar", action="store_true")
    ap.add_argument("--arquivo", help="XLSX do Outlet (opcional).")
    args = ap.parse_args()
    if args.arquivo:
        arquivo = Path(args.arquivo).expanduser().resolve()
        if not arquivo.is_file():
            raise RuntimeError(f"Arquivo nao encontrado: {arquivo}")
    else:
        arquivos = sorted(
            (
                p for p in (Path(__file__).resolve().parent / "dados").glob("*.xlsx")
                if "analise do volume de negocios do outlet" in core.normalizar(p.stem)
            ),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        if not arquivos:
            raise RuntimeError("Nenhum XLSX do Outlet encontrado diretamente em dados/.")
        arquivo = arquivos[0]

    print(f"OUTLET SELECIONADO: {arquivo.name}", flush=True)
    with requests.Session() as session:
        core.testar_api(session)
        gestores = outlet.gestao_publicada(session)
        print(f"BASES ORLEAN: {len(gestores)}", flush=True)
        volume = outlet.carregar_volume(arquivo, gestores, aba=None)
        dias = sorted({d for d, _ in volume})
        print(
            f"VOLUMETRIA: {len(volume):,} base(s)-dia | "
            f"{dias[0]} a {dias[-1]}",
            flush=True,
        )
        if args.dry_run:
            print("DRY-RUN OK: dados nao gravados.")
            return
        enviados = outlet.publicar_volume_postgres(session, volume, lote=100)
        verif = outlet.consulta(session, """
            SELECT COUNT(*)::bigint AS linhas
            FROM public.jt_outlet_volume_diario
            WHERE data::date >= %s::date AND data::date <= %s::date
        """, ["linhas"], [dias[0], dias[-1]])
        encontrados = int(verif[0]["linhas"]) if verif else 0
        if encontrados < len(volume):
            print(
                f"ATENCAO: banco contem {encontrados:,} base(s)-dia entre "
                f"{dias[0]} e {dias[-1]}, mas o XLSX tem {len(volume):,}. "
                "Conferir permissons e conflito de dados.",
                flush=True,
            )
        print(
            f"PUBLICADO: {enviados:,} base(s)-dia | "
            f"BANCO: {encontrados:,} registro(s) no intervalo.",
            flush=True,
        )


if __name__ == "__main__":
    main()
