# -*- coding: utf-8 -*-
"""Corrige sc=JOHAN nos registros BRUTOS de TODAS as datas via JT API /import.

Pré-requisito: corrigir_johan_todo_banco.py --aplicar deve ter validado e
publicado os mesmos 11 códigos na jt_gestao_bases.

Etapas --aplicar:
  1. Confirma códigos da nova Gestão e propriedade na dimensão publicada.
  2. Consulta os registros históricos ainda com sc diferente de Johan.
  3. Salva BACKUP JSONL integral ANTES de fazer qualquer escrita.
  4. Reenvia registros inteiros via upsert na chave já usada no sistema,
     alterando somente o campo sc. NÃO muda data, remessa, pedidos, aging,
     rm, uf, regional, motivo ou arquivo.
  5. Verifica cada lote diretamente no PostgreSQL via endpoint /query.
  6. Confirma que não restam divergentes e que totais de linhas/pedidos
     não mudaram durante a operação.

Em falha de rede: NÃO faça retries manuais de um lote. Rode o script de
novo, que consulta apenas linhas ainda divergentes e gera novo backup.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
import importar_excel as core
import corrigir_johan_todo_banco as ref

COLS = (
    "id", "indicador", "data", "remessa", "pedidos", "base_codigo",
    "base_nome", "aging_label", "aging_valor", "acima_3", "acima_10",
    "uf", "rm", "sc", "regional", "motivo", "arquivo",
)
COLS_IMPORT = tuple(c for c in COLS if c != "id")
DB = "public.sem_mov_retidos_detalhe"


def query(s, sql, params=None):
    return ref.rows_as_dict(s, sql, list(COLS), params)


def estado(s, codigos):
    params = [ref.JOHAN, *codigos]
    marks = ", ".join(["%s"] * len(codigos))
    rows = ref.rows_as_dict(s, f"""
        SELECT COUNT(*)::bigint AS total,
               COALESCE(SUM(pedidos),0)::bigint AS pedidos,
               COUNT(*) FILTER (WHERE sc IS DISTINCT FROM %s)::bigint AS divergentes
        FROM {DB}
        WHERE indicador IN ('sem_mov','retidos')
          AND base_codigo IN ({marks})
    """, ["total", "pedidos", "divergentes"], params)
    if len(rows) != 1:
        raise RuntimeError("Estado do PostgreSQL retornou número inesperado de linhas")
    row = rows[0]
    return (int(row["total"]), int(row["pedidos"]), int(row["divergentes"]))


def ler_pagina(s, codigos, ultimo_id, tamanho):
    marks = ", ".join(["%s"] * len(codigos))
    sql = f"""
        SELECT id, indicador, data::text AS data, remessa, pedidos,
               base_codigo, base_nome, aging_label, aging_valor,
               acima_3, acima_10, uf, rm, sc, regional, motivo, arquivo
        FROM {DB}
        WHERE indicador IN ('sem_mov','retidos')
          AND base_codigo IN ({marks})
          AND sc IS DISTINCT FROM %s
          AND id > %s
        ORDER BY id
        LIMIT {int(tamanho)}
    """
    return query(s, sql, [*codigos, ref.JOHAN, ultimo_id])


def confirmar_lote(s, lote):
    ids = [int(x["id"]) for x in lote]
    marks = ", ".join(["%s"] * len(ids))
    rows = ref.rows_as_dict(s, f"""
        SELECT COUNT(*)::bigint AS total,
               COUNT(*) FILTER (WHERE sc = %s)::bigint AS corrigidos,
               COALESCE(SUM(pedidos),0)::bigint AS pedidos
        FROM {DB}
        WHERE id IN ({marks})
    """, ["total", "corrigidos", "pedidos"], [ref.JOHAN, *ids])
    if len(rows) != 1:
        raise RuntimeError("Conferência do lote sem resposta única")
    total, ok, pedidos = [int(rows[0][x]) for x in ("total", "corrigidos", "pedidos")]
    esperados = sum(int(x["pedidos"] or 0) for x in lote)
    if total != len(lote) or ok != len(lote) or pedidos != esperados:
        raise RuntimeError(
            f"LOTE DIVERGENTE: esperado {len(lote)} registros e {esperados} pedidos; "
            f"banco retornou {total} registros, {ok} corrigidos e {pedidos} pedidos."
        )


def validar_dimensao(s, alvos):
    dim = ref.banco_dimensao(s, [x["base_codigo"] for x in alvos])
    for x in alvos:
        db = dim.get(x["base_codigo"])
        if db is None or ref.canon(db["sc"]) != ref.canon(ref.JOHAN):
            raise RuntimeError(
                f"Base {x['base_codigo']}: Gestão publicada não atribui S/C ao Johan. "
                "Execute primeiro corrigir_johan_todo_banco.py --aplicar."
            )
        if ref.canon(db.get("rm")) != ref.canon(x["rm"]):
            raise RuntimeError(
                f"Base {x['base_codigo']}: R/M na dimensão difere do XLSX novo. "
                "Interrompido."
            )


def fazer_backup(s, codigos, pasta, esperado, pagina):
    # Nome único para não sobrescrever backups anteriores.
    local = pasta / (
        "backup_sc_johan_historico_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jsonl"
    )
    tmp = local.with_suffix(".jsonl.part")
    ultimo_id = 0
    salvos = 0
    vistos = set()
    with tmp.open("w", encoding="utf-8") as f:
        while True:
            bloco = ler_pagina(s, codigos, ultimo_id, pagina)
            if not bloco:
                break
            for row in bloco:
                id_num = int(row["id"])
                if id_num <= ultimo_id or id_num in vistos:
                    raise RuntimeError("Página da API repetiu ou desordenou IDs; backup cancelado")
                ultimo_id = id_num
                vistos.add(id_num)
                key = (
                    str(row["indicador"]), str(row["data"]),
                    str(row["remessa"] or ""), str(row["base_codigo"]),
                )
                if (
                    row["indicador"] not in ("sem_mov", "retidos")
                    or not key[1] or not key[2] or key[3] not in codigos
                ):
                    raise RuntimeError(
                        f"Registro histórico ID {id_num} com chave incompleta ou inesperada; "
                        "não é seguro atualizá-lo por upsert."
                    )
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                salvos += 1
            print(f"  BACKUP: {salvos:,}/{esperado:,} registros", flush=True)
    if salvos != esperado:
        raise RuntimeError(
            f"Backup incompleto: {salvos:,} registros salvos, "
            f"{esperado:,} esperados. Não foi realizada alteração no PostgreSQL."
        )
    os.replace(tmp, local)
    return local


def atualizar(s, arquivo, lote_tamanho):
    lote = []
    enviados = 0

    def enviar():
        nonlocal enviados
        if not lote:
            return
        # Copia a linha completa para NÃO apagar dados não relacionados a S/C.
        dados = []
        for original in lote:
            copia = {c: original.get(c) for c in COLS_IMPORT}
            copia["sc"] = ref.JOHAN
            dados.append(copia)
        try:
            resp = s.post(
                f"{core.API_URL}/import",
                headers=core._headers(),
                json={
                    "tabela": "sem_mov_retidos_detalhe",
                    "chave": ["indicador","data","remessa","base_codigo"],
                    "registros": dados,
                    "criar_tabela": False,
                    "adicionar_colunas": False,
                },
                timeout=max(core.API_TIMEOUT, 120),
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                "Falha de rede. O lote pode ter sido gravado parcialmente. "
                "Rode novamente o script com --aplicar, que fará novo backup "
                "apenas das linhas ainda incorretas."
            ) from exc
        if not resp.ok:
            raise RuntimeError(
                f"JT API /import HTTP {resp.status_code}: {resp.text[:500]}. "
                "Não repita manualmente o lote."
            )
        confirmar_lote(s, lote)
        enviados += len(lote)
        print(f"  POSTGRESQL: {enviados:,} linhas corrigidas e conferidas", flush=True)
        lote.clear()

    with arquivo.open(encoding="utf-8") as f:
        for linha in f:
            if not linha.strip():
                continue
            lote.append(json.loads(linha))
            if len(lote) >= lote_tamanho:
                enviar()
    enviar()
    return enviados


def main():
    ap = argparse.ArgumentParser(description="Atualizar S/C Johan em TODAS as remessas via JT API")
    ap.add_argument("--gestao", required=True)
    ap.add_argument("--aplicar", action="store_true",
                    help="Autoriza upserts nas remessas históricas com backup prévio")
    ap.add_argument("--lote", type=int, default=100)
    args = ap.parse_args()
    if not (20 <= args.lote <= 250):
        ap.error("--lote deve estar entre 20 e 250")

    path = Path(args.gestao).expanduser().resolve()
    alvos = ref.carregar_alvos(path)
    codigos = [x["base_codigo"] for x in alvos]
    destino = Path(__file__).resolve().parent / ".correcoes_locais"
    destino.mkdir(exist_ok=True)
    with requests.Session() as s:
        core.testar_api(s)
        validar_dimensao(s, alvos)
        antes = estado(s, codigos)
        print(
            f"GESTÃO: {path.name} | {len(alvos)} bases de Johan "
            f"| {antes[0]:,} registros históricos | {antes[1]:,} pedidos "
            f"| S/C a corrigir: {antes[2]:,}", flush=True
        )
        if antes[2] == 0:
            print("NENHUMA DIVERGÊNCIA: todo o histórico já está atribuído ao Johan.")
            return
        if not args.aplicar:
            print("SOMENTE LEITURA: nenhuma linha alterada.")
            print("Para corrigir o PostgreSQL inteiro, execute com --aplicar.")
            return

        backup = fazer_backup(s, codigos, destino, antes[2], pagina=500)
        print(f"BACKUP INTEGRAL SALVO LOCALMENTE: {backup}", flush=True)
        # A carga não começa se o banco mudou durante a preparação do backup.
        if estado(s, codigos) != antes:
            raise RuntimeError(
                "O estado do banco mudou após o backup. Nenhum lote enviado. "
                "Rode novamente para criar um backup atualizado."
            )
        atualizados = atualizar(s, backup, args.lote)
        depois = estado(s, codigos)
        if depois[0] != antes[0] or depois[1] != antes[1] or depois[2] != 0:
            raise RuntimeError(
                "CONFERÊNCIA FINAL DIVERGENTE: banco retornou "
                f"{depois[0]:,} linhas, {depois[1]:,} pedidos e "
                f"{depois[2]:,} S/C diferentes. Preserve o backup e investigue."
            )
        print(
            f"SUCESSO: {atualizados:,} registros históricos corrigidos, "
            f"Sem Mov e Retidos, em TODAS as datas do banco. "
            "Total de registros e pedidos preservado."
        )
        print(f"Backup das linhas antigas: {backup}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr, flush=True)
        sys.exit(1)
