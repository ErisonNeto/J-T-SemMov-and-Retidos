from __future__ import annotations

"""Executor automático do importador Sem Mov / Retidos.

Mantém a lógica de importar_excel.py. Para arquivos Sem Mov com nome genérico,
lê a coluna A quando for Data de referência e valida todas as linhas.
NUNCA infere a data pelo horário do download. Após a importação,
consulta a JT API e imprime as datas efetivamente presentes no PostgreSQL.
"""

import sys
from pathlib import Path

import requests

import importar_excel as core


# Salva a função original ANTES do monkeypatch para não criar recursão.
_data_referencia_segura = core.data_referencia


def data_referencia_auto(path: Path, indicador: str):
    """Mesma validação segura do importador principal, inclusive A versus nome."""
    return _data_referencia_segura(path, indicador)


def verificar_banco() -> None:
    """Mostra o que realmente ficou gravado no PostgreSQL após a carga."""
    print("=" * 88)
    print("VERIFICAÇÃO NO BANCO - JT API / PostgreSQL")
    print("=" * 88)

    sql = """
        SELECT
            indicador,
            data::text AS data,
            COUNT(*)::bigint AS registros,
            SUM(COALESCE(pedidos, 0))::bigint AS pedidos
        FROM public.sem_mov_retidos_detalhe
        WHERE data >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY indicador, data
        ORDER BY data, indicador
    """

    with requests.Session() as session:
        resp = session.post(
            f"{core.API_URL}/query",
            headers=core._headers(),
            json={"sql": sql, "params": []},
            timeout=min(core.API_TIMEOUT, 60),
        )

    if not resp.ok:
        print(f"Não consegui verificar o banco: HTTP {resp.status_code}: {resp.text[:500]}")
        return

    payload = resp.json()
    rows = payload.get("rows") or [] if isinstance(payload, dict) else []
    if not rows:
        print("A consulta não retornou datas para Sem Mov / Retidos.")
        return

    por_indicador: dict[str, list[tuple[str, int, int]]] = {"sem_mov": [], "retidos": []}
    for row in rows:
        if isinstance(row, dict):
            indicador = str(row.get("indicador") or "")
            data = str(row.get("data") or "")
            registros = int(row.get("registros") or 0)
            pedidos = int(row.get("pedidos") or 0)
        else:
            indicador = str(row[0]) if len(row) > 0 else ""
            data = str(row[1]) if len(row) > 1 else ""
            registros = int(row[2] or 0) if len(row) > 2 else 0
            pedidos = int(row[3] or 0) if len(row) > 3 else 0
        if indicador in por_indicador:
            por_indicador[indicador].append((data, registros, pedidos))

    for indicador in ("sem_mov", "retidos"):
        itens = por_indicador[indicador]
        print(f"\n{indicador.upper()}")
        if not itens:
            print("  nenhuma data encontrada")
            continue
        for data, registros, pedidos in itens:
            print(f"  {data} | {registros:,} registros | {pedidos:,} pedidos")
        print(f"  Última data no banco: {itens[-1][0]}")


# Monkey patch: localizar_arquivos() do importador passa a usar a regra automática.
core.data_referencia = data_referencia_auto


if __name__ == "__main__":
    core.main()

    argumentos = set(sys.argv[1:])
    if "--dry-run" not in argumentos and "--check-api" not in argumentos:
        verificar_banco()
