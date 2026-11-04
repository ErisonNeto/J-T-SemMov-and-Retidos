# -*- coding: utf-8 -*-
"""Prepara backup e diagnóstico de 28/10, SEM ALTERAR O BANCO.

Usar após validar o arquivo:
  python importar_excel.py "./dados/sem_mov/Sem movimentação 28.10.xlsx" --dry-run --strict
  python preparar_correcao_28.py "./dados/sem_mov/Sem movimentação 28.10.xlsx"

Backups e eventual SQL ficam FORA do repositório, em ~/JT_BACKUPS_PRIVADOS.
NÃO executa SQL de exclusão. O SQL produzido exige análise e autorização.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

import importar_excel as core
from retomar_sem_mov import consulta, estado_banco


DIA = "2026-10-28"
TOTAL_PREVISTO = 42473
PEDIDOS_PREVISTOS = 58978


def extrair_registro(row):
    """Normaliza resposta de /query: dicionário, tupla [json] ou JSON serializado."""
    if isinstance(row, dict):
        if "remessa" in row and "base_codigo" in row:
            return row
        if "registro" in row:
            return extrair_registro(row["registro"])
        if len(row) == 1:
            return extrair_registro(next(iter(row.values())))
    if isinstance(row, (list, tuple)) and len(row) == 1:
        return extrair_registro(row[0])
    if isinstance(row, str):
        try:
            return extrair_registro(json.loads(row))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Resposta da API contém JSON inválido.") from exc
    raise RuntimeError(
        "Formato de linha desconhecido na resposta da JT API: "
        f"tipo={type(row).__name__}, "
        f"campos={list(row)[:8] if isinstance(row, dict) else 'não informado'}."
    )


def normalizar_chave(row):
    return (core.texto(row.get("remessa")), core.norm_codigo(row.get("base_codigo")))


def aspas_sql(value):
    return "'" + str(value).replace("'", "''") + "'"


def gerar_sql_remocao(remessas):
    """Gera SQL transacional isolado, nunca executado por este script."""
    lista = ", ".join(aspas_sql(rem) for rem in remessas)
    condicao = (
        "indicador = 'sem_mov'\n"
        "                 AND data = '2026-10-28'::date\n"
        "                 AND base_codigo = '89101'\n"
        "                 AND COALESCE(pedidos, 0) = 0\n"
        f"                 AND remessa IN ({lista})"
    )
    return f"""-- EXECUTAR SOMENTE APÓS AUTORIZAÇÃO E COM BACKUP CONFERIDO.
-- Precisa executar NO POSTGRESQL de origem da JT API (psql/pgAdmin),
-- em transação, após todas as outras importações terminarem.
-- Exclui apenas os 18 registros identificados; nenhuma outra data ou Retidos.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $$
DECLARE n bigint;
BEGIN
  SELECT COUNT(*) INTO n
  FROM public.sem_mov_retidos_detalhe
  WHERE {condicao};
  IF n <> 18 THEN
    RAISE EXCEPTION 'Cancelado: esperados 18 registros exatos, encontrados %', n;
  END IF;

  DELETE FROM public.sem_mov_retidos_detalhe
  WHERE {condicao};
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 18 THEN
    RAISE EXCEPTION 'Cancelado: DELETE afetou % registros em vez de 18', n;
  END IF;
  RAISE NOTICE 'Removidos exatamente % registros de 28/10', n;
END $$;
COMMIT;
"""


def run():
    parser = argparse.ArgumentParser(description="Backup e diagnóstico não destrutivo de Sem Mov 28/10")
    parser.add_argument("arquivo", help="XLSX de Sem Mov de 28/10/2026")
    args = parser.parse_args()
    arquivo = Path(args.arquivo).expanduser().resolve()
    if not arquivo.is_file():
        parser.error(f"Arquivo não encontrado: {arquivo}")
    if core.detectar_indicador(arquivo) != "sem_mov":
        parser.error("Esperado arquivo SEM MOV.")
    dt = core.data_referencia(arquivo, "sem_mov")
    if dt.isoformat() != DIA:
        parser.error(f"Data errada no arquivo: {dt}. Esperado {DIA}.")

    with requests.Session() as s:
        core.testar_api(s)
        n, pedidos_db = estado_banco(s, DIA)
        print(f"BANCO 28/10 | {n} registros | {pedidos_db} pedidos")
        if (n, pedidos_db) != (18, 0):
            raise RuntimeError(
                "O banco mudou desde a conferência anterior! "
                "Nenhuma limpeza deve ser preparada sem revisar os novos registros."
            )

        # A JT API pode retornar tuplas em vez de dicionários. Usamos
        # row_to_json(t) para recuperar o registro completo com os nomes
        # das colunas, independentemente da forma de serialização de /query.
        retorno = consulta(
            s,
            """SELECT row_to_json(t) AS registro
               FROM (
                 SELECT *
                 FROM public.sem_mov_retidos_detalhe
                 WHERE indicador = %s AND data = %s::date
                 ORDER BY remessa, base_codigo
               ) AS t""",
            ["sem_mov", DIA],
        )
        if len(retorno) != 18:
            raise RuntimeError(
                f"A JT API retornou {len(retorno)} linhas; eram esperadas 18. "
                "Nenhum dado foi alterado."
            )
        rows = [extrair_registro(x) for x in retorno]
        if not all(isinstance(x, dict) and "remessa" in x and "base_codigo" in x for x in rows):
            raise RuntimeError("Resposta não contém registros completos para backup.")
        keys = [normalizar_chave(x) for x in rows]
        if len(set(keys)) != 18:
            raise RuntimeError("As 18 chaves não são únicas. Parando.")
        for row in rows:
            if (
                core.norm_codigo(row.get("base_codigo")) != "89101"
                or int(row.get("pedidos") or 0) != 0
                or "ORLEAN" not in core.texto(row.get("rm")).upper()
            ):
                raise RuntimeError(
                    "Os 18 registros não correspondem ao diagnóstico anterior. Parando."
                )

        # Backup antes de qualquer operação. Local e FORA do GitHub.
        pasta = Path.home() / "JT_BACKUPS_PRIVADOS"
        pasta.mkdir(parents=True, exist_ok=True)
        instante = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = pasta / f"sem_mov_2026-10-28_18_registros_{instante}.json"
        conteudo = {
            "data": DIA,
            "tabela": "public.sem_mov_retidos_detalhe",
            "quantidade": 18,
            "registros": rows,
            "observacao": "Backup integral extraído via JT API /query antes de qualquer exclusão.",
        }
        with backup.open("x", encoding="utf-8") as f:
            json.dump(conteudo, f, ensure_ascii=False, indent=2, default=str)
        print(f"BACKUP LOCAL SALVO: {backup}")

        # Compara EXATAMENTE as chaves armazenadas com a planilha verdadeira.
        gestao = core.carregar_gestao(core.GESTAO_PADRAO)
        chaves_banco = set(keys)
        sobreposicoes = []
        qtd = 0
        soma_pedidos = 0
        for rec in core.sem_mov_records(arquivo, gestao, dt):
            qtd += 1
            soma_pedidos += int(rec["pedidos"])
            if normalizar_chave(rec) in chaves_banco:
                sobreposicoes.append(normalizar_chave(rec))

        print(f"ARQUIVO VÁLIDO | {qtd:,} registros | {soma_pedidos:,} pedidos")
        print(f"Chaves dos 18 registros também encontradas na base válida: {len(sobreposicoes)}")
        if (qtd, soma_pedidos) != (TOTAL_PREVISTO, PEDIDOS_PREVISTOS):
            raise RuntimeError(
                "A planilha mudou desde o dry-run; confira antes de prosseguir."
            )

        if sobreposicoes:
            print(
                "ATENÇÃO: há chaves em comum com o arquivo válido. "
                "É necessário examinar essas sobreposições antes de excluir."
            )
        else:
            sql_path = pasta / f"exclusao_CONTROLADA_sem_mov_2026-10-28_{instante}.sql"
            with sql_path.open("x", encoding="utf-8") as f:
                f.write(gerar_sql_remocao([x[0] for x in keys]))
            print(f"SQL PARA REVISÃO (NÃO EXECUTADO): {sql_path}")

        final_n, final_ped = estado_banco(s, DIA)
        if (final_n, final_ped) != (18, 0):
            raise RuntimeError("O banco mudou durante a auditoria. NÃO execute SQL.")
        print("AUDITORIA CONCLUÍDA — NENHUM REGISTRO ALTERADO.")
        print(
            "Não execute o SQL nem a importação de 28/10 enquanto "
            "os outros dias estiverem sendo importados."
        )


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
