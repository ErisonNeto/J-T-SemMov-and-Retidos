# -*- coding: utf-8 -*-
"""Carga retomável do Sem Mov 28/10/2026, preservando 18 linhas órfãs preexistentes.

Somente envia a base correta depois de:
- validar 42.473 registros e 58.978 pedidos do Excel;
- conferir as 18 chaves órfãs contra o backup local de auditoria;
- garantir que nenhuma chave órfã existe no Excel;
- confirmar que linhas já importadas formam prefixo do Excel e batem em
  quantidade/pedidos/chaves amostradas.

Este programa NÃO apaga nem altera as 18 linhas antigas. É preciso removê-las
mais tarde via SQL controlado no PostgreSQL, depois de backup.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests
import importar_excel as core

DIA = "2026-10-28"
REGISTROS_ESPERADOS = 42473
PEDIDOS_ESPERADOS = 58978
ORFAOS_ESPERADOS = 18
TABELA = "public.sem_mov_retidos_detalhe"


def query(session, sql, params=None):
    resp = session.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=min(core.API_TIMEOUT, 120),
    )
    if not resp.ok:
        raise RuntimeError(f"JT API /query HTTP {resp.status_code}: {resp.text[:250]}")
    payload = resp.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for nome in ("rows", "data", "result", "resultado", "registros"):
            if isinstance(payload.get(nome), list):
                return payload[nome]
    raise RuntimeError("Formato de resposta inesperado da JT API")


def estado(session):
    rows = query(
        session,
        f"""SELECT COUNT(*)::bigint AS registros,
                   COALESCE(SUM(pedidos),0)::bigint AS pedidos
            FROM {TABELA} WHERE indicador = %s AND data = %s::date""",
        ["sem_mov", DIA],
    )
    if len(rows) != 1:
        raise RuntimeError("Estado do banco retornou número inesperado de linhas")
    r = rows[0]
    if isinstance(r, dict):
        return int(r["registros"]), int(r["pedidos"])
    return int(r[0]), int(r[1])


def obter_backup():
    path = Path(__file__).resolve().parent / ".correcoes_locais" / "backup_sem_mov_2026-10-28_18_linhas.json"
    if not path.is_file():
        raise RuntimeError(
            "Backup das 18 linhas não encontrado: "
            f"{path}. Execute primeiro preparar_correcao_2810.py."
        )
    with path.open(encoding="utf-8") as f:
        obj = json.load(f)
    if obj.get("data") != DIA or obj.get("tabela") != TABELA:
        raise RuntimeError("Backup não corresponde ao Sem Mov de 28/10/2026")
    rows = obj.get("registros")
    if not isinstance(rows, list) or len(rows) != ORFAOS_ESPERADOS:
        raise RuntimeError("Backup não possui as 18 linhas esperadas")
    chaves = set()
    remessas = []
    for r in rows:
        if not isinstance(r, dict):
            raise RuntimeError("Backup possui registro inválido")
        remessa = str(r.get("remessa") or "").strip()
        base = str(r.get("base_codigo") or "").strip()
        if (not remessa or base != "89101"
                or int(r.get("pedidos") or 0) != 0
                or "ORLEAN" not in str(r.get("rm") or "").upper()):
            raise RuntimeError("Backup das linhas órfãs não passou na conferência")
        if (remessa, base) in chaves:
            raise RuntimeError("Backup contém chaves órfãs duplicadas")
        chaves.add((remessa, base))
        remessas.append(remessa)
    return chaves, remessas


def verificar_orfaos(session, chaves, remessas):
    parametros = ["sem_mov", DIA, *remessas]
    lista = ", ".join(["%s"] * len(remessas))
    rows = query(
        session,
        f"""SELECT remessa, base_codigo, pedidos, rm FROM {TABELA}
            WHERE indicador = %s AND data = %s::date
              AND remessa IN ({lista})""",
        parametros,
    )
    if len(rows) != ORFAOS_ESPERADOS:
        raise RuntimeError(
            f"As 18 linhas antigas mudaram: encontrei {len(rows)} no banco. "
            "Nenhum envio foi realizado."
        )
    achadas = set()
    for row in rows:
        if isinstance(row, dict):
            remessa, base, pedidos, rm = (row.get(k) for k in
                                          ("remessa", "base_codigo", "pedidos", "rm"))
        elif isinstance(row, (list, tuple)) and len(row) == 4:
            remessa, base, pedidos, rm = row
        else:
            raise RuntimeError("Formato de linha inesperado ao verificar órfãos")
        chave = (str(remessa).strip(), str(base).strip())
        if (chave not in chaves or int(pedidos or 0) != 0
                or "ORLEAN" not in str(rm or "").upper()):
            raise RuntimeError("Dados das 18 linhas antigas foram alterados")
        achadas.add(chave)
    if achadas != chaves:
        raise RuntimeError("Chaves órfãs do backup não coincidem com o banco")


def verificar_amostra(session, registro):
    rows = query(
        session,
        f"""SELECT COUNT(*)::bigint AS qtd FROM {TABELA}
            WHERE indicador = %s AND data = %s::date
              AND remessa = %s AND base_codigo = %s""",
        ["sem_mov", DIA, registro["remessa"], registro["base_codigo"]],
    )
    if len(rows) != 1:
        raise RuntimeError("Erro na conferência das chaves importadas")
    r = rows[0]
    qtd = int(r["qtd"] if isinstance(r, dict) else r[0])
    if qtd != 1:
        raise RuntimeError(
            "A amostra de remessa/base do prefixo não coincide com o PostgreSQL; "
            "nenhum lote novo foi enviado."
        )


def main():
    p = argparse.ArgumentParser(
        description="Retoma 28/10 mesmo com as 18 linhas antigas, sem apagá-las"
    )
    p.add_argument("arquivo", help="Planilha correta de Sem Mov 28/10")
    p.add_argument("--lote", type=int, default=100)
    p.add_argument("--verificar", action="store_true",
                   help="Somente validar, sem enviar registros")
    args = p.parse_args()
    if not (50 <= args.lote <= 500):
        p.error("O tamanho de lote precisa estar entre 50 e 500")
    path = Path(args.arquivo).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".xlsx":
        p.error("Arquivo .xlsx não encontrado")
    if core.detectar_indicador(path) != "sem_mov":
        raise RuntimeError("Arquivo não é de Sem Mov")
    if core.data_referencia(path, "sem_mov") != date.fromisoformat(DIA):
        raise RuntimeError("Data incorreta: precisa ser 28/10/2026")

    gestao = core.carregar_gestao(core.GESTAO_PADRAO)
    registros = list(core.sem_mov_records(path, gestao, date.fromisoformat(DIA)))
    total = sum(int(r.get("pedidos") or 0) for r in registros)
    if (len(registros), total) != (REGISTROS_ESPERADOS, PEDIDOS_ESPERADOS):
        raise RuntimeError(
            f"Fonte divergente: {len(registros)} registros e {total} pedidos. "
            "Nenhum dado será enviado."
        )
    chaves_fonte = [
        (str(r["remessa"]).strip(), str(r["base_codigo"]).strip())
        for r in registros
    ]
    if len(set(chaves_fonte)) != REGISTROS_ESPERADOS:
        raise RuntimeError("Existem remessa/base repetidas no Excel")
    chaves_orfas, remessas_orfas = obter_backup()
    if any(chave in chaves_orfas for chave in chaves_fonte):
        raise RuntimeError("Excel contém chaves das 18 linhas órfãs; não é seguro importar")

    print(
        f"Fonte validada: {len(registros):,} registros, {total:,} pedidos; "
        f"Gestão: {len(gestao)} códigos", flush=True
    )
    with requests.Session() as session:
        core.testar_api(session)
        verificar_orfaos(session, chaves_orfas, remessas_orfas)
        banco_total, banco_pedidos = estado(session)
        if banco_total < ORFAOS_ESPERADOS or banco_total > REGISTROS_ESPERADOS + ORFAOS_ESPERADOS:
            raise RuntimeError("O total de 28/10 no banco está fora do intervalo seguro")

        prefixo = banco_total - ORFAOS_ESPERADOS
        soma_prefixo = sum(int(r["pedidos"]) for r in registros[:prefixo])
        if soma_prefixo != banco_pedidos:
            raise RuntimeError(
                f"O prefixo de {prefixo:,} linhas soma {soma_prefixo:,} pedidos; "
                f"o banco contém {banco_pedidos:,}. Importação bloqueada."
            )
        time.sleep(3)
        if estado(session) != (banco_total, banco_pedidos):
            raise RuntimeError("Banco mudou enquanto conferíamos. Não iniciar carga.")
        posicoes = sorted(
            {i for i in (0, 1, 2, 2999, prefixo // 2, prefixo - 3,
                         prefixo - 2, prefixo - 1) if 0 <= i < prefixo}
        )
        for i in posicoes:
            verificar_amostra(session, registros[i])
        print(
            f"Banco: {banco_total:,} linhas; {ORFAOS_ESPERADOS} antigas isoladas; "
            f"{prefixo:,} registros válidos conferidos; {len(posicoes)} chaves verificadas",
            flush=True
        )
        if args.verificar:
            print("VERIFICAÇÃO CONCLUÍDA — NENHUM REGISTRO ENVIADO", flush=True)
            return

        enviados = prefixo
        pedidos_enviados = soma_prefixo
        for inicio in range(prefixo, len(registros), args.lote):
            lote = registros[inicio:inicio + args.lote]
            try:
                core.enviar_lote(session, lote)
            except requests.exceptions.RequestException as exc:
                print(
                    f"Falha de rede após {enviados:,} linhas válidas confirmadas. "
                    "Não repita lote manualmente: reexecute este programa para "
                    "reconferir o prefixo no banco.", flush=True
                )
                raise RuntimeError(f"JT API falhou: {exc}") from exc
            enviados += len(lote)
            pedidos_enviados += sum(int(r["pedidos"]) for r in lote)
            if enviados % 1000 == 0 or enviados == len(registros):
                print(
                    f"  OK PostgreSQL | 28/10 | válidos {enviados:,}/{REGISTROS_ESPERADOS:,}"
                    f" | pedidos {pedidos_enviados:,}",
                    flush=True,
                )
        verificar_orfaos(session, chaves_orfas, remessas_orfas)
        banco_fim, pedidos_fim = estado(session)
        if (banco_fim, pedidos_fim) != (
            REGISTROS_ESPERADOS + ORFAOS_ESPERADOS, PEDIDOS_ESPERADOS
        ):
            raise RuntimeError(
                f"Banco ficou com {banco_fim:,} linhas e {pedidos_fim:,} pedidos. "
                "Conferir antes de nova importação."
            )
        print(
            f"IMPORTAÇÃO DOS {REGISTROS_ESPERADOS:,} REGISTROS CORRETOS CONCLUÍDA.\n"
            f"Banco: {banco_fim:,} linhas e {pedidos_fim:,} pedidos.\n"
            "ATENÇÃO: 18 linhas órfãs de zero pedidos permanecem no banco "
            "até a limpeza SQL controlada.", flush=True
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
