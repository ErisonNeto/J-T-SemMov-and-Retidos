# -*- coding: utf-8 -*-
"""Correção controlada das 11 bases informadas como gestão de Johan.

Usa a Gestão XLSX local para identificar CÓDIGOS; nunca transfere bases
por correspondência de nome diretamente no PostgreSQL.

Padrão: somente diagnóstico, sem modificar banco.
--aplicar: atualiza a tabela jt_gestao_bases (a usada no dashboard),
           conferindo cada uma das 11 bases após a publicação.
Também gera SQL para atualizar, com backup transacional, TODOS os
registros históricos de Sem Mov/Retidos no PostgreSQL (nenhuma data é excluída).

Não executa o SQL de histórico via JT API /query (endpoint somente leitura).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import importar_excel as core

JOHAN = "JOHAN RAFAEL QUEIROZ PACHECO"
NOMES = (
    "CPN-PA", "CRA-PA", "CST-PA", "F CST-PA", "F IGA-PA",
    "MAR-PA", "PRM-PA", "SIP-PA", "SLP-PA", "STA-PA", "VGA-PA",
)
DIM = "public.jt_gestao_bases"
TABELA_HIST = "public.sem_mov_retidos_detalhe"
BACKUP_TABELA = "public.jt_backup_sc_johan_20261105"
CAMPOS_DIM = (
    "base_codigo", "base_nome", "uf", "rm", "sc",
    "regional", "coordenador", "supervisor", "atualizado_em",
)


def canon(nome):
    s = unicodedata.normalize("NFKD", str(nome or ""))
    return re.sub(r"[^A-Z0-9]+", "", "".join(
        c for c in s if not unicodedata.combining(c)
    ).upper())


def text(v):
    if v is None or pd.isna(v):
        return ""
    return str(v).strip()


def fetch(session, sql, params=None):
    r = session.post(
        f"{core.API_URL}/query",
        headers=core._headers(),
        json={"sql": sql, "params": params or []},
        timeout=max(core.API_TIMEOUT, 100),
    )
    if not r.ok:
        raise RuntimeError(f"JT API /query HTTP {r.status_code}: {r.text[:300]}")
    payload = r.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("rows", "data", "result", "resultado", "registros", "items"):
            if isinstance(payload.get(k), list):
                return payload[k]
    raise RuntimeError("Resposta /query não contém lista de linhas")


def rows_as_dict(session, sql, fields, params=None):
    resultado = []
    for row in fetch(session, sql, params):
        if isinstance(row, dict):
            resultado.append({c: row.get(c) for c in fields})
        elif isinstance(row, (list, tuple)) and len(row) == len(fields):
            resultado.append(dict(zip(fields, row)))
        else:
            raise RuntimeError("Formato de coluna inesperado na resposta da API")
    return resultado


def carregar_alvos(path):
    if not path.is_file():
        raise RuntimeError(f"Gestão não encontrada: {path}")
    df = pd.read_excel(path, dtype=str)
    req = ("Estação No.", "Nome da base", "Responsável da Região",
           "Estado(UF)", "Coordenador", "Supervisor")
    faltantes = [k for k in req if k not in df.columns]
    if faltantes:
        raise RuntimeError(f"Gestão sem colunas: {faltantes}")
    por_nome = {}
    nomes_canonicos = {canon(n): n for n in NOMES}
    for _, r in df.iterrows():
        key = canon(r.get("Nome da base"))
        if key not in nomes_canonicos:
            continue
        codigo = core.norm_codigo(r.get("Estação No."))
        if not re.fullmatch(r"\d{3,10}", codigo):
            raise RuntimeError(
                f"{nomes_canonicos[key]}: código inválido na Gestão: {codigo!r}"
            )
        info = {
            "nome_informado": nomes_canonicos[key],
            "base_codigo": codigo,
            "base_nome": text(r.get("Nome da base")),
            "uf": text(r.get("Estado(UF)")),
            "rm": text(r.get("Responsável da Região")),
            "regional": text(r.get("Regional")),
            "coordenador_antigo": text(r.get("Coordenador")),
            "supervisor_antigo": text(r.get("Supervisor")),
        }
        por_nome.setdefault(key, []).append(info)

    faltando = [n for n in NOMES if canon(n) not in por_nome]
    if faltando:
        raise RuntimeError(
            "A Gestão não localizou estas bases informadas para Johan: "
            + ", ".join(faltando)
            + ". Nenhuma alteração permitida até identificar seus códigos."
        )
    codigos = set()
    alvos = []
    for nome in NOMES:
        encontrados = por_nome[canon(nome)]
        unicos = {x["base_codigo"] for x in encontrados}
        if len(unicos) != 1:
            raise RuntimeError(
                f"{nome}: vários códigos encontrados {sorted(unicos)}; "
                "corrija a Gestão para evitar transferência indevida."
            )
        distintos_rm = {canon(x["rm"]) for x in encontrados if x["rm"]}
        if len(distintos_rm) > 1:
            raise RuntimeError(f"{nome}: a Gestão possui R/M conflitantes.")
        escolhido = next((x for x in encontrados if x["rm"]), encontrados[0])
        if not escolhido["rm"]:
            raise RuntimeError(f"{nome}: R/M vazio. Não é seguro publicar.")
        if "ORLEAN" not in canon(escolhido["rm"]):
            raise RuntimeError(
                f"{nome} (código {escolhido['base_codigo']}) está sob "
                f"R/M {escolhido['rm']!r} na Gestão. "
                "Revisar antes de atribuir ao Johan."
            )
        if escolhido["base_codigo"] in codigos:
            raise RuntimeError(
                f"O código {escolhido['base_codigo']} está atribuído a "
                "duas bases distintas na lista; alteração cancelada."
            )
        codigos.add(escolhido["base_codigo"])
        alvos.append(escolhido)
    return alvos


def banco_dimensao(session, codigos):
    campos = CAMPOS_DIM[:8]
    registros = rows_as_dict(
        session,
        f"""SELECT base_codigo,base_nome,uf,rm,sc,regional,coordenador,supervisor
            FROM {DIM}
            WHERE base_codigo = ANY(%s::text[])
            ORDER BY base_codigo""",
        campos, [codigos],
    )
    return {text(x["base_codigo"]): x for x in registros}


def contagem_historico(session, codigos):
    campos = ("indicador", "registros", "divergentes")
    rows = rows_as_dict(
        session,
        f"""SELECT indicador,
                   COUNT(*)::bigint AS registros,
                   COUNT(*) FILTER (WHERE sc IS DISTINCT FROM %s)::bigint AS divergentes
            FROM {TABELA_HIST}
            WHERE indicador IN ('sem_mov','retidos')
              AND base_codigo = ANY(%s::text[])
            GROUP BY indicador
            ORDER BY indicador""",
        campos, [JOHAN, codigos],
    )
    return [{
        "indicador": x["indicador"],
        "registros": int(x["registros"]),
        "divergentes": int(x["divergentes"]),
    } for x in rows]


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def gerar_sql(alvos, pasta):
    codigos = [x["base_codigo"] for x in alvos]
    lista = ", ".join(lit(c) for c in codigos)
    sql = f"""-- J&T | Corrigir S/C Johan em TODO o histórico, Sem Mov + Retidos
-- Gerado por corrigir_johan_todo_banco.py.
-- Requer PostgreSQL direto (VM/psql/pgAdmin); NÃO executar via /query da JT API.
-- EXECUTAR SOMENTE APÓS BACKUP COMPLETO DO POSTGRESQL.
-- Uma tabela adicional salva o estado anterior das linhas afetadas.
-- A operação é transacional. Reexecutar o mesmo SQL é bloqueado.
\\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
    IF to_regclass({lit(BACKUP_TABELA)}) IS NOT NULL THEN
        RAISE EXCEPTION 'Backup de Johan ja existe. Revisar antes de executar novamente.';
    END IF;
END
$$;

CREATE TABLE {BACKUP_TABELA} AS
SELECT id, indicador, data, base_codigo, sc AS sc_antes,
       rm AS rm_antes, uf AS uf_antes, regional AS regional_antes,
       clock_timestamp() AS salvo_em
FROM {TABELA_HIST}
WHERE indicador IN ('sem_mov','retidos')
  AND base_codigo IN ({lista})
  AND sc IS DISTINCT FROM {lit(JOHAN)};

SELECT indicador, COUNT(*) AS registros_que_serao_corrigidos
FROM {BACKUP_TABELA}
GROUP BY indicador
ORDER BY indicador;

UPDATE {TABELA_HIST}
SET sc = {lit(JOHAN)}
WHERE indicador IN ('sem_mov','retidos')
  AND base_codigo IN ({lista})
  AND sc IS DISTINCT FROM {lit(JOHAN)};

SELECT indicador, COUNT(*) AS registros_ainda_divergentes
FROM {TABELA_HIST}
WHERE indicador IN ('sem_mov','retidos')
  AND base_codigo IN ({lista})
  AND sc IS DISTINCT FROM {lit(JOHAN)}
GROUP BY indicador;
COMMIT;
-- IMPORTANTE: BACKUP {BACKUP_TABELA} deve ser preservado.
"""
    # psql accepts \\set ON_ERROR_STOP; for pgAdmin, remove that single line.
    path = pasta / "corrigir_johan_todo_historico_postgresql.sql"
    path.write_text(sql, encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(
        description="Atribui 11 bases ao Johan na dimensão e prepara histórico total"
    )
    ap.add_argument(
        "--gestao", required=True,
        help="Gestão NOVA (20261105-000130) usada para obter os códigos."
    )
    ap.add_argument(
        "--aplicar", action="store_true",
        help="Aplica 11 correções somente na jt_gestao_bases via JT API."
    )
    args = ap.parse_args()
    caminho = Path(args.gestao).expanduser().resolve()
    alvos = carregar_alvos(caminho)
    pasta = Path(__file__).resolve().parent / "auditoria_gestao_19a02"
    pasta.mkdir(exist_ok=True)

    with requests.Session() as session:
        core.testar_api(session)
        bd = banco_dimensao(session, [x["base_codigo"] for x in alvos])
        print("ATRIBUIÇÃO JOHAN — 11 BASES | Gestão:", caminho.name)
        print("-" * 110)
        for x in alvos:
            velho = bd.get(x["base_codigo"])
            if not velho:
                raise RuntimeError(
                    f"Base {x['base_codigo']} não existe em {DIM}. "
                    "Publicar Gestão completa antes da correção."
                )
            if canon(x["base_nome"]) != canon(velho["base_nome"]):
                raise RuntimeError(
                    f"Código {x['base_codigo']}: nomes distintos na Gestão "
                    f"({x['base_nome']}) e dimensão ({velho['base_nome']}). "
                    "Não atualizar até esclarecer."
                )
            print(
                f"  {x['nome_informado']:<12} {x['base_codigo']:<9} "
                f"S/C anterior={text(velho['sc'])[:38]:<38}"
                f" -> {JOHAN}"
            )

        resumo = contagem_historico(session, [x["base_codigo"] for x in alvos])
        for row in resumo:
            print(
                f"  Histórico {row['indicador']}: {row['registros']:,} registros, "
                f"{row['divergentes']:,} com S/C diferente."
            )

        csvpath = pasta / "correcao_johan_11_bases.csv"
        with csvpath.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "nome_informado", "base_codigo", "base_nome", "uf", "rm",
                "regional", "coordenador_antigo", "supervisor_antigo",
            ], delimiter=";")
            writer.writeheader()
            writer.writerows(alvos)
        sql_path = gerar_sql(alvos, pasta)
        print("Plano e códigos conferidos:", csvpath)
        print("SQL do histórico TOTAL:", sql_path)

        if not args.aplicar:
            print("VALIDAÇÃO CONCLUÍDA. NENHUM DADO FOI ALTERADO.")
            print("Para atualizar os resultados de todas as datas no dashboard, "
                  "execute novamente com --aplicar.")
            return

        atualizacao = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = []
        for x in alvos:
            original = bd[x["base_codigo"]]
            row = {
                k: text(original.get(k)) for k in CAMPOS_DIM[:8]
            }
            row.update({
                "sc": JOHAN,
                # Pedido explícito: Johan como coordenador destas 11 bases.
                "coordenador": JOHAN,
                "supervisor": "",
                "rm": x["rm"], "uf": x["uf"] or row["uf"],
                "regional": x["regional"] or row["regional"],
                "atualizado_em": atualizacao,
            })
            payload.append(row)

        resp = session.post(
            f"{core.API_URL}/import",
            headers=core._headers(),
            json={
                "tabela": "jt_gestao_bases", "chave": ["base_codigo"],
                "registros": payload,
                "criar_tabela": False, "adicionar_colunas": False,
            },
            timeout=max(core.API_TIMEOUT, 120),
        )
        if not resp.ok:
            raise RuntimeError(
                f"JT API /import HTTP {resp.status_code}: {resp.text[:400]}. "
                "Verificar dimensão antes de repetir."
            )
        verificados = banco_dimensao(session, [x["base_codigo"] for x in alvos])
        for row in payload:
            novo = verificados.get(row["base_codigo"])
            if not novo or any(
                text(novo.get(c)) != text(row.get(c))
                for c in ("sc", "coordenador", "supervisor", "rm", "uf")
            ):
                raise RuntimeError(
                    f"Conferência falhou para a base {row['base_codigo']}"
                )
        print(
            "ATUALIZAÇÃO DA DIMENSÃO CONCLUÍDA E VERIFICADA: 11 bases sob Johan. "
            "O dashboard usa esta dimensão para TODAS as datas e para Sem Mov/Retidos."
        )
        print(
            "REGISTROS HISTÓRICOS BRUTOS: ainda não alterados. "
            "Para corrigir também a coluna sc gravada em cada remessa, "
            f"execute o SQL auditável {sql_path} com acesso ao PostgreSQL."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr, flush=True)
        sys.exit(1)
