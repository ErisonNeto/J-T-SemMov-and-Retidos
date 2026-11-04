# -*- coding: utf-8 -*-
"""Retoma carga de Sem Mov pelo prefixo comprovadamente gravado.

Lê o arquivo sempre na mesma ordem usada pelo importar_excel.py. Só pula os
registros já existentes quando (1) contagem, (2) soma dos pedidos e (3)
amostras de chaves comprovam que o banco contém o prefixo exato do arquivo.

Em timeout, NÃO repete lotes sem verificar novamente o PostgreSQL.
Nunca apaga dados. Não toca em Retidos.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests
import importar_excel as core


def linhas_resposta(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("rows", "data", "registros", "resultado", "result", "items"):
            value = payload.get(k)
            if isinstance(value, list):
                return value
        if isinstance(payload.get("data"), dict):
            return linhas_resposta(payload["data"])
    raise RuntimeError("JT API: formato de resposta não reconhecido.")


def consulta(session, sql, params=None):
    r = session.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=core.API_TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"JT API /query HTTP {r.status_code}: {r.text[:250]}")
    return linhas_resposta(r.json())


def primeiro_valor(row, key):
    return row[key] if isinstance(row, dict) else row[0]


def estado_banco(session, dia):
    rows = consulta(
        session,
        """SELECT COUNT(*)::bigint AS registros,
                  COALESCE(SUM(pedidos),0)::bigint AS pedidos
           FROM public.sem_mov_retidos_detalhe
           WHERE indicador = %s AND data = %s::date""",
        ["sem_mov", dia],
    )
    if len(rows) != 1:
        raise RuntimeError("Consulta do total retornou múltiplas linhas.")
    r = rows[0]
    n = int(r["registros"] if isinstance(r, dict) else r[0])
    soma = int(r["pedidos"] if isinstance(r, dict) else r[1])
    return n, soma


def verificar_chave(session, dia, rec):
    rows = consulta(
        session,
        """SELECT COUNT(*)::bigint AS qtd
           FROM public.sem_mov_retidos_detalhe
           WHERE indicador = %s AND data = %s::date
             AND remessa = %s AND base_codigo = %s""",
        ["sem_mov", dia, rec["remessa"], rec["base_codigo"]],
    )
    qtd = int(primeiro_valor(rows[0], "qtd"))
    if qtd != 1:
        raise RuntimeError(
            f"Prefixo do arquivo NÃO corresponde ao PostgreSQL: "
            f"remessa {rec['remessa']}, base {rec['base_codigo']}, "
            f"registros no banco: {qtd}. Importação cancelada."
        )


def run():
    ap = argparse.ArgumentParser(
        description="Retoma importação de Sem Mov com verificação do prefixo existente."
    )
    ap.add_argument("arquivo", help="Um XLSX de Sem Mov")
    ap.add_argument("--lote", type=int, default=250,
                    help="Registros por requisição (padrão: 250).")
    ap.add_argument("--verificar", action="store_true",
                    help="Confere o banco e o arquivo sem enviar registros.")
    args = ap.parse_args()
    if not (50 <= args.lote <= 1000):
        ap.error("--lote deve ficar entre 50 e 1000.")

    path = Path(args.arquivo).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".xlsx":
        ap.error(f"Arquivo XLSX não encontrado: {path}")

    indicador = core.detectar_indicador(path)
    if indicador != "sem_mov":
        ap.error("Este script aceita somente arquivos SEM MOV.")
    dia = core.data_referencia(path, "sem_mov").isoformat()
    gestao = core.carregar_gestao(core.GESTAO_PADRAO)
    print(f"Arquivo: {path.name} | dia: {dia} | Gestão: {len(gestao)} códigos", flush=True)

    with requests.Session() as session:
        core.testar_api(session)
        print("JT API: autenticação OK", flush=True)
        n_db, soma_db = estado_banco(session, dia)
        print(f"No banco: {n_db:,} registros, {soma_db:,} pedidos", flush=True)

        # Confirma que não há outra importação operando ao mesmo tempo.
        time.sleep(3)
        if estado_banco(session, dia) != (n_db, soma_db):
            raise RuntimeError(
                "A quantidade no banco está mudando. Outra carga pode estar "
                "em andamento; importação cancelada."
            )

        gerador = iter(core.sem_mov_records(path, gestao, core.date.fromisoformat(dia)))
        verificacoes = {
            x for x in (1, 2, 3, 3000, 3001, n_db // 2,
                        max(1, n_db - 2), max(1, n_db - 1), n_db)
            if 1 <= x <= n_db
        }
        amostras = []
        vistos = set()
        prefixo_pedidos = 0
        for pos in range(1, n_db + 1):
            rec = next(gerador, None)
            if rec is None:
                raise RuntimeError(
                    f"Banco tem {n_db:,} linhas; arquivo acabou antes do prefixo. "
                    "Provável arquivo errado. Importação cancelada."
                )
            chave = core.registro_chave(rec)
            if chave in vistos:
                raise RuntimeError(
                    f"Remessa/base duplicada no prefixo do arquivo, posição {pos}. "
                    "Não é seguro pular linhas automaticamente."
                )
            vistos.add(chave)
            prefixo_pedidos += int(rec["pedidos"])
            if pos in verificacoes:
                amostras.append(rec)

        if prefixo_pedidos != soma_db:
            raise RuntimeError(
                f"Prefixo NÃO CONFERE: as primeiras {n_db:,} linhas do arquivo "
                f"somam {prefixo_pedidos:,} pedidos; no banco são {soma_db:,}. "
                "Não é seguro retomar por posição."
            )
        for rec in amostras:
            verificar_chave(session, dia, rec)
        print(
            f"Prefixo confirmado: {n_db:,} registros, {prefixo_pedidos:,} pedidos; "
            f"{len(amostras)} chaves verificadas. Nenhuma delas será reenviada.",
            flush=True,
        )

        if args.verificar:
            print("VERIFICAÇÃO CONCLUÍDA — nenhum dado enviado.", flush=True)
            return

        enviados = n_db
        soma_enviada = soma_db
        lote = []

        def enviar():
            nonlocal enviados, soma_enviada
            if not lote:
                return
            try:
                core.enviar_lote(session, lote)
            except requests.exceptions.RequestException as exc:
                print(
                    f"FALHA NA API após {enviados:,} registros confirmados. "
                    "Não repita o mesmo lote manualmente. Execute este script "
                    "novamente: ele conferirá o prefixo gravado.",
                    flush=True,
                )
                raise RuntimeError(f"Erro ao enviar lote: {exc}") from exc
            enviados += len(lote)
            soma_enviada += sum(int(rec["pedidos"]) for rec in lote)
            print(
                f"  OK PostgreSQL | {dia} | {enviados:,} registros "
                f"| {soma_enviada:,} pedidos",
                flush=True,
            )
            lote.clear()

        for rec in gerador:
            chave = core.registro_chave(rec)
            if chave in vistos:
                raise RuntimeError(
                    "Remessa/base repetida em lotes diferentes. "
                    "Parando para evitar alteração indevida."
                )
            vistos.add(chave)
            lote.append(rec)
            if len(lote) >= args.lote:
                enviar()
        enviar()

        fim_db, fim_pedidos = estado_banco(session, dia)
        print(
            f"FINAL | fonte enviada {enviados:,}/{soma_enviada:,} "
            f"| banco {fim_db:,}/{fim_pedidos:,}",
            flush=True,
        )
        if (fim_db, fim_pedidos) != (enviados, soma_enviada):
            raise RuntimeError(
                "Divergência de contagem no final. Não importe novamente sem "
                "conferir o banco."
            )
        print("IMPORTAÇÃO COMPLETA E CONFERIDA.", flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr, flush=True)
        sys.exit(1)
