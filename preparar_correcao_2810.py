# -*- coding: utf-8 -*-
"""Auditoria SOMENTE LEITURA da correção do Sem Mov de 28/10/2026.

Verifica as 18 linhas erradas no PostgreSQL, salva cópia de segurança JSON e
prepara SQL com salvaguardas para execução MANUAL no servidor PostgreSQL.

Este programa NUNCA envia SQL de escrita à JT API, e NUNCA importa ou deleta.
Execute o SQL gerado apenas depois de fazer backup do PostgreSQL.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import requests
import importar_excel as core

DIA = date(2026, 10, 28)
ESPERADO_REGISTROS = 42473
ESPERADO_PEDIDOS = 58978
TABELA = "public.sem_mov_retidos_detalhe"


def linhas(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for nome in ("rows", "data", "registros", "result", "resultado"):
            if isinstance(payload.get(nome), list):
                return payload[nome]
    raise RuntimeError("Formato inesperado da resposta da JT API.")


def consulta(session, sql, params=None):
    resp = session.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=min(core.API_TIMEOUT, 90),
    )
    if not resp.ok:
        raise RuntimeError(f"JT API /query HTTP {resp.status_code}: {resp.text[:250]}")
    return linhas(resp.json())


def quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def preparar_sql(rows):
    remessas = [str(x["remessa"]) for x in rows]
    lista_remessas = ", ".join(quote(r) for r in remessas)
    return f"""-- GERADO EM MODO SOMENTE LEITURA. NÃO EXECUTADO PELO PYTHON.
-- Requisitos: backup do PostgreSQL e conferência manual antes da execução.
-- Efeito: remove SOMENTE as 18 linhas erradas de SEM MOV em 28/10/2026.
-- Jamais toca em Retidos ou outros dias. Abortará se o estado tiver mudado.
\\set ON_ERROR_STOP on
BEGIN;
DO $JT_2810$
DECLARE
    qtd INTEGER;
    removidos INTEGER;
BEGIN
    SELECT COUNT(*) INTO qtd
    FROM {TABELA}
    WHERE indicador = 'sem_mov' AND data = DATE '2026-10-28';
    IF qtd <> 18 THEN
        RAISE EXCEPTION 'ABORTADO: 28/10 tem % registros; esperado 18.', qtd;
    END IF;

    SELECT COUNT(*) INTO qtd
    FROM {TABELA}
    WHERE indicador = 'sem_mov' AND data = DATE '2026-10-28'
      AND base_codigo::text = '89101'
      AND COALESCE(pedidos,0) = 0
      AND POSITION('ORLEAN' IN UPPER(COALESCE(rm,''))) > 0
      AND remessa::text IN ({lista_remessas});
    IF qtd <> 18 THEN
        RAISE EXCEPTION 'ABORTADO: as 18 chaves incorretas mudaram (apenas % conferidas).', qtd;
    END IF;

    DELETE FROM {TABELA}
    WHERE indicador = 'sem_mov' AND data = DATE '2026-10-28'
      AND base_codigo::text = '89101'
      AND COALESCE(pedidos,0) = 0
      AND remessa::text IN ({lista_remessas});
    GET DIAGNOSTICS removidos = ROW_COUNT;
    IF removidos <> 18 THEN
        RAISE EXCEPTION 'ABORTADO: foram removidos % registros; esperado 18.', removidos;
    END IF;
END
$JT_2810$;

-- Confirma que 28/10 foi zerado após a limpeza.
SELECT indicador, data, COUNT(*) AS registros
FROM {TABELA}
WHERE indicador = 'sem_mov' AND data = DATE '2026-10-28'
GROUP BY indicador, data;
COMMIT;
"""


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Uso: python preparar_correcao_2810.py ".\\dados\\sem_mov\\sem movimentação 28.10.xlsx"')
    arquivo = Path(sys.argv[1]).expanduser().resolve()
    if not arquivo.is_file():
        raise RuntimeError(f"Arquivo não encontrado: {arquivo}")
    if core.data_referencia(arquivo, "sem_mov") != DIA:
        raise RuntimeError("O arquivo não foi reconhecido como 28/10/2026.")

    gestao = core.carregar_gestao(core.GESTAO_PADRAO)
    registros = list(core.sem_mov_records(arquivo, gestao, DIA))
    pedidos = sum(int(x.get("pedidos") or 0) for x in registros)
    if len(registros) != ESPERADO_REGISTROS or pedidos != ESPERADO_PEDIDOS:
        raise RuntimeError(
            f"Arquivo divergiu da validação: {len(registros):,} registros, "
            f"{pedidos:,} pedidos; esperado 42.473 / 58.978."
        )
    keys = {(str(x["remessa"]), str(x["base_codigo"])) for x in registros}
    if len(keys) != len(registros):
        raise RuntimeError("Existem remessa/base duplicadas na fonte: conferir antes de prosseguir.")
    print(f"Excel OK: {len(registros):,} registros, {pedidos:,} pedidos | Gestão {len(gestao)}")

    with requests.Session() as sess:
        core.testar_api(sess)
        # O endpoint /query pode devolver listas posicionais em vez de dicts.
        # Consultamos a ordem REAL das colunas para preservar um backup completo.
        columns_raw = consulta(
            sess,
            """SELECT column_name
               FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'sem_mov_retidos_detalhe'
               ORDER BY ordinal_position""",
        )
        nomes = [
            str(x["column_name"] if isinstance(x, dict) else x[0])
            for x in columns_raw
        ]
        if not nomes or not {"remessa", "base_codigo", "pedidos", "rm", "indicador", "data"}.issubset(set(nomes)):
            raise RuntimeError(
                "Não consegui confirmar as colunas do banco. "
                "Operação somente leitura encerrada."
            )
        linhas_banco = consulta(
            sess,
            f"""SELECT * FROM {TABELA}
                WHERE indicador = %s AND data = %s::date
                ORDER BY remessa""",
            ["sem_mov", DIA.isoformat()],
        )
        rows = []
        for rec in linhas_banco:
            if isinstance(rec, dict):
                rows.append(rec)
            elif isinstance(rec, (tuple, list)) and len(rec) == len(nomes):
                rows.append(dict(zip(nomes, rec)))
            else:
                raise RuntimeError(
                    "Resposta do PostgreSQL não bate com o esquema. "
                    "Nenhuma operação foi executada."
                )

    if len(rows) != 18:
        raise RuntimeError(
            f"Banco tem {len(rows)} linhas de Sem Mov em 28/10; "
            "esperadas 18. Nenhum SQL foi gerado."
        )
    def codigo(valor):
        return str(valor if valor is not None else "").strip()

    checks = {
        "base_89101": sum(codigo(r.get("base_codigo")) == "89101" for r in rows),
        "pedidos_zero": sum(
            str(r.get("pedidos") if r.get("pedidos") is not None else "0").strip()
            in ("0", "0.0", "0.00") for r in rows
        ),
        "rm_orlean": sum("ORLEAN" in codigo(r.get("rm")).upper() for r in rows),
        "remessa_preenchida": sum(bool(codigo(r.get("remessa"))) for r in rows),
    }
    print("Diagnóstico seguro das 18 linhas (sem expor remessas):")
    print("  "+", ".join(f"{k}={v}/18" for k, v in checks.items()))
    if any(v != 18 for v in checks.values()):
        print("  Códigos de base presentes:", dict(Counter(codigo(r.get("base_codigo")) for r in rows)))
        print("  Valores de pedidos:", dict(Counter(codigo(r.get("pedidos")) for r in rows)))
        print("  Valores de R/M:", dict(Counter(codigo(r.get("rm")) for r in rows)))
        raise RuntimeError(
            "Conferência das 18 linhas não corresponde ao padrão esperado. "
            "NÃO será gerado SQL de exclusão."
        )
    if len({str(r["remessa"]) for r in rows}) != 18:
        raise RuntimeError("As 18 linhas não possuem remessas únicas; abortado.")

    sobrepostas = sum(
        (str(r["remessa"]), str(r["base_codigo"])) in keys
        for r in rows
    )
    print(f"Banco: 18 registros incorretos confirmados, com 0 pedidos.")
    print(f"Chaves que também aparecem no Excel correto: {sobrepostas}/18.")

    # Dados de remessas só ficam locais; NÃO publicar/commitar a pasta.
    destino = Path(__file__).resolve().parent / ".correcoes_locais"
    destino.mkdir(parents=True, exist_ok=True)
    backup = destino / "backup_sem_mov_2026-10-28_18_linhas.json"
    sql_file = destino / "corrigir_sem_mov_2026-10-28.sql"
    if backup.exists() or sql_file.exists():
        raise RuntimeError(
            "Arquivos de correção já existem. Não sobrescrever backup; "
            "confira a pasta .correcoes_locais."
        )
    with backup.open("x", encoding="utf-8") as f:
        json.dump(
            {"tabela": TABELA, "data": DIA.isoformat(),
             "origem": "JT API /query somente leitura", "colunas": nomes,
             "registros": rows},
            f, ensure_ascii=False, indent=2, default=str
        )
    with sql_file.open("x", encoding="utf-8") as f:
        f.write(preparar_sql(rows))

    print(f"Backup local: {backup}")
    print(f"SQL para execução MANUAL e CONTROLADA no PostgreSQL: {sql_file}")
    print("NENHUMA MODIFICAÇÃO FOI FEITA NO BANCO.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
