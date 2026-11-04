# -*- coding: utf-8 -*-
"""Publica TODOS os códigos da Gestão em tabela de responsáveis da JT API.

Em vez de reimportar milhões de remessas ou sobrescrever o histórico,
cria/atualiza uma tabela pequena, por código de base, que o dashboard usa
para calcular R/M, S/C e UF corretamente em ambos os indicadores.

Padrão: somente auditoria; --aplicar habilita importação de 100 códigos/lote.
Não apaga, não modifica sem_mov_retidos_detalhe.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import importar_excel as core

TABELA = "jt_gestao_bases"
CAMPOS = (
    "base_codigo", "base_nome", "uf", "rm", "sc",
    "regional", "coordenador", "supervisor", "atualizado_em",
)


def norm(v):
    return core.normalizar(v)


def query(s, sql, params=None):
    resp = s.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=max(core.API_TIMEOUT, 90),
    )
    if not resp.ok:
        raise RuntimeError(f"JT API /query HTTP {resp.status_code}: {resp.text[:250]}")
    obj = resp.json()
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for field in ("rows", "data", "result", "resultado", "registros"):
            if isinstance(obj.get(field), list):
                return obj[field]
    raise RuntimeError("Formato de resposta inesperado na consulta da JT API")


def organizar_gestao(path):
    df = pd.read_excel(path, dtype=str)
    required = [
        "Estação No.", "Nome da base", "Estado(UF)",
        "Responsável da Região", "Coordenador", "Supervisor",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Gestão não tem as colunas: {missing}")
    por_codigo = defaultdict(list)
    carimbo = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for _, row in df.iterrows():
        codigo = core.norm_codigo(row.get("Estação No."))
        if not codigo or norm(codigo) in ("nan", "none"):
            continue
        coord = core.texto(row.get("Coordenador"))
        sup = core.texto(row.get("Supervisor"))
        por_codigo[codigo].append({
            "base_codigo": codigo,
            "base_nome": core.texto(row.get("Nome da base")),
            "uf": core.texto(row.get("Estado(UF)")),
            "rm": core.texto(row.get("Responsável da Região")),
            "sc": sup or coord,
            "regional": core.texto(row.get("Regional")),
            "coordenador": coord,
            "supervisor": sup,
            "atualizado_em": carimbo,
        })

    duplicados = {c: arr for c, arr in por_codigo.items() if len(arr) > 1}
    divergencias = {}
    campos_efetivos = ("uf", "rm", "sc", "regional")
    for codigo, arr in duplicados.items():
        if len({tuple(norm(x[c]) for c in campos_efetivos) for x in arr}) > 1:
            divergencias[codigo] = arr

    if divergencias:
        for codigo, arr in divergencias.items():
            print(
                f"  ERRO: base {codigo} possui atribuições conflitantes: "
                + " / ".join(f"{x['rm']} | {x['sc']} | {x['uf']}" for x in arr),
                flush=True,
            )
        raise RuntimeError(
            f"{len(divergencias)} código(s) de base com responsáveis contraditórios. "
            "Corrija o arquivo Gestão antes de publicar."
        )

    # Preferir nome completo quando cadastro de código repetido tiver campo vazio.
    dados = []
    for codigo, arr in sorted(por_codigo.items()):
        escolhido = dict(arr[-1])
        for campo in ("base_nome", "uf", "rm", "sc", "regional", "coordenador", "supervisor"):
            if not escolhido[campo]:
                escolhido[campo] = next((x[campo] for x in arr if x[campo]), "")
        dados.append(escolhido)

    return dados, duplicados


def tabela_existe(s):
    r = query(s, """SELECT COUNT(*)::int AS qtd
                       FROM information_schema.tables
                       WHERE table_schema='public' AND table_name=%s""", [TABELA])
    x = r[0]
    return int(x["qtd"] if isinstance(x, dict) else x[0]) > 0


def obter_banco(s):
    if not tabela_existe(s):
        return {}
    r = query(s, f"""
        SELECT base_codigo, base_nome, uf, rm, sc, regional, coordenador, supervisor
        FROM public.{TABELA}
        ORDER BY base_codigo
    """)
    campos = CAMPOS[:8]
    out = {}
    for linha in r:
        if isinstance(linha, dict):
            item = {c: core.texto(linha.get(c)) for c in campos}
        elif isinstance(linha, (tuple, list)) and len(linha) == len(campos):
            item = {c: core.texto(v) for c, v in zip(campos, linha)}
        else:
            raise RuntimeError("JT API devolveu esquema inesperado da Gestão publicada")
        out[item["base_codigo"]] = item
    return out


def conferir(dados, banco):
    esperado = {r["base_codigo"]: r for r in dados}
    for cod, row in esperado.items():
        bd = banco.get(cod)
        if bd is None:
            raise RuntimeError(f"Base {cod} não consta da Gestão publicada")
        for campo in CAMPOS[:8]:
            if norm(row.get(campo)) != norm(bd.get(campo)):
                raise RuntimeError(f"Base {cod}: coluna {campo} no PostgreSQL diverge da Gestão")
    print(
        f"CONFERÊNCIA FINAL OK: {len(esperado)} códigos conferidos individualmente "
        "na tabela de responsáveis.",
        flush=True,
    )


def main():
    p = argparse.ArgumentParser(description="Publicar Gestão de colaboradores por código de base")
    p.add_argument("--gestao", default=str(core.GESTAO_PADRAO))
    p.add_argument("--aplicar", action="store_true",
                   help="Autoriza criar/atualizar jt_gestao_bases via JT API")
    p.add_argument("--lote", type=int, default=100)
    args = p.parse_args()
    if args.lote < 10 or args.lote > 200:
        p.error("--lote deve ficar entre 10 e 200")
    path = Path(args.gestao).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Gestão não encontrada: {path}")

    dados, duplicados = organizar_gestao(path)
    if not dados:
        raise RuntimeError("Nenhuma base com código válido na Gestão")
    print(f"Gestão: {path.name} | {len(dados)} códigos únicos")
    print(
        f"  Duplicados por código: {len(duplicados)} "
        "(sem conflito no responsável operacional)", flush=True,
    )
    sem_sc = [d["base_codigo"] for d in dados if not d["sc"]]
    sem_rm = [d["base_codigo"] for d in dados if not d["rm"]]
    print(f"  Bases sem S/C: {len(sem_sc)} | sem R/M: {len(sem_rm)}")
    if sem_sc:
        print("  Códigos sem S/C (não é possível preencher por inferência): "
              + ", ".join(sem_sc[:20]))
    if sem_rm:
        print("  Códigos sem R/M: " + ", ".join(sem_rm[:20]))

    pasta = Path(__file__).resolve().parent / "auditoria_gestao_19a02"
    pasta.mkdir(exist_ok=True)
    destino = pasta / "gestao_normalizada_todas_bases.csv"
    with destino.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS, delimiter=";")
        writer.writeheader()
        writer.writerows(dados)
    print(f"CSV com todos os responsáveis: {destino}")

    with requests.Session() as session:
        core.testar_api(session)
        existe = tabela_existe(session)
        print(
            f"Destino PostgreSQL: public.{TABELA} "
            f"({'já existe' if existe else 'ainda não existe'})"
        )
        if not args.aplicar:
            print("VALIDAÇÃO CONCLUÍDA: nenhum dado enviado ou modificado.")
            print("Para publicar as 509 bases, execute novamente com --aplicar.")
            return

        # Upsert SOMENTE na tabela de Gestão, nunca na tabela de remessas.
        for inicio in range(0, len(dados), args.lote):
            lote = dados[inicio:inicio + args.lote]
            payload = {
                "tabela": TABELA,
                "chave": ["base_codigo"],
                "registros": lote,
                "criar_tabela": not existe,
                "adicionar_colunas": False,
            }
            resp = session.post(
                f"{core.API_URL}/import",
                headers=core._headers(),
                json=payload,
                timeout=max(core.API_TIMEOUT, 120),
            )
            if not resp.ok:
                raise RuntimeError(
                    f"JT API /import retornou HTTP {resp.status_code}: {resp.text[:350]}. "
                    "Confira a tabela antes de repetir a carga."
                )
            existe = True
            print(f"  JT API: {min(inicio + len(lote), len(dados)):,}/{len(dados):,} bases enviadas")
        banco = obter_banco(session)
        conferir(dados, banco)
        print(
            "PUBLICAÇÃO FINALIZADA. Tabela de remessas preservada. "
            "Agora o dashboard pode usar essa Gestão como dimensão de responsáveis.",
            flush=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr, flush=True)
        sys.exit(1)
