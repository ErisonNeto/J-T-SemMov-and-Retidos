from __future__ import annotations

"""Executor automático do importador Sem Mov / Retidos.

Mantém toda a lógica do importar_excel.py, mas elimina a necessidade de renomear
exports automáticos do Sem Mov. Para nomes J&T que terminam com timestamp de
exportação (ex.: ...20261103084820.xlsx), considera como referência operacional
o dia anterior ao timestamp da exportação. Ao final de uma importação real,
consulta a JT API e imprime as datas efetivamente presentes no PostgreSQL.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

import importar_excel as core


def _timestamp_exportacao(path: Path) -> datetime | None:
    """Encontra um timestamp YYYYMMDDHHMMSS válido mesmo dentro de uma sequência maior."""
    nome = path.stem
    candidatos: list[datetime] = []
    for match in re.finditer(r"(?=(20\d{12}))", nome):
        valor = match.group(1)
        try:
            candidatos.append(datetime.strptime(valor, "%Y%m%d%H%M%S"))
        except ValueError:
            continue
    return max(candidatos) if candidatos else None


def data_referencia_auto(path: Path, indicador: str):
    explicita = core.data_explicita_do_nome(path)
    if explicita is not None:
        return explicita

    if indicador == "retidos":
        return core.inferir_data_retidos(path)

    if indicador == "sem_mov":
        exportado_em = _timestamp_exportacao(path)
        if exportado_em is not None:
            operacional = exportado_em.date() - timedelta(days=1)
            print(
                f"    Sem Mov: data automática {operacional} "
                f"(exportado em {exportado_em:%d/%m/%Y %H:%M:%S}) | {path.name}"
            )
            return operacional

        # Último fallback somente para o nome padrão do relatório da J&T.
        # Evita exigir renomeação caso o fornecedor altere/remova o timestamp.
        nome = core.normalizar(path.stem)
        if "monitoramento de movimentacao" in nome:
            operacional = datetime.fromtimestamp(path.stat().st_mtime).date() - timedelta(days=1)
            print(
                f"    Sem Mov: data automática {operacional} pela data do arquivo | {path.name}"
            )
            return operacional

    raise ValueError(
        f"Não consegui inferir automaticamente a data operacional de {path.name}."
    )


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
