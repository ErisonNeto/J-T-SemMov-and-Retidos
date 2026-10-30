from __future__ import annotations

import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

import pandas as pd
from lxml import etree
import csv
import gzip
import os
from collections import defaultdict

from arquivos import RESUMOS_DIR, DETALHES_DIR, DIMENSOES, atomic_csv

BASE_DIR = Path(__file__).resolve().parent
DADOS_DIR = BASE_DIR / "dados"
SEM_DIR = DADOS_DIR / "sem_mov"
RET_DIR = DADOS_DIR / "retidos"
GESTAO_PADRAO = DADOS_DIR / "Gestão de colaboradores.xlsx"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}


def norm_codigo(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def data_do_nome(path: Path) -> date:
    nome = path.stem.lower()
    m = re.search(r"(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+(\d{1,2})\s+(\d{4})", nome)
    if not m:
        raise ValueError(f"Não consegui identificar a data pelo nome: {path.name}")
    return date(int(m.group(3)), MESES[m.group(1)], int(m.group(2)))


def carregar_gestao(path: Path) -> Dict[str, dict]:
    if not path.exists():
        raise FileNotFoundError(f"Gestão de colaboradores não encontrada: {path}")
    df = pd.read_excel(path, dtype=str)
    obrig = ["Estação No.", "Estado(UF)", "Responsável da Região", "Coordenador", "Supervisor"]
    faltantes = [c for c in obrig if c not in df.columns]
    if faltantes:
        raise ValueError(f"Colunas ausentes na Gestão: {faltantes}")

    mapa: Dict[str, dict] = {}
    for _, row in df.iterrows():
        codigo = norm_codigo(row.get("Estação No."))
        if not codigo:
            continue
        coord = texto(row.get("Coordenador"))
        sup = texto(row.get("Supervisor"))
        # S/C unificado: usa Coordenador quando preenchido; caso contrário, Supervisor.
        sc = coord or sup
        mapa[codigo] = {
            "base_nome_gestao": texto(row.get("Nome da base")),
            "uf": texto(row.get("Estado(UF)")),
            "rm": texto(row.get("Responsável da Região")),
            "sc": sc,
            "regional": texto(row.get("Regional")),
        }
    return mapa


def _cell_value(cell) -> str:
    tipo = cell.get("t")
    if tipo == "inlineStr":
        return "".join((t.text or "") for t in cell.findall(".//" + NS + "t"))
    v = cell.find(NS + "v")
    return "" if v is None else (v.text or "")


def _col(ref: str) -> str:
    m = re.match(r"([A-Z]+)", ref or "")
    return m.group(1) if m else ""


def iter_xlsx(path: Path, colunas: set[str]) -> Iterator[dict]:
    """Leitura streaming: só materializa as colunas necessárias do XLSX gigante."""
    with zipfile.ZipFile(path) as z:
        sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        if not sheets:
            raise ValueError(f"Planilha sem worksheet: {path.name}")
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            with z.open('xl/sharedStrings.xml') as sf:
                for _, elem in etree.iterparse(sf, events=('end',), tag=NS+'si'):
                    strings.append(''.join(elem.itertext()))
                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]
        with z.open(sheets[0]) as f:
            context = etree.iterparse(f, events=("end",), tag=NS + "row")
            for _, row in context:
                if row.get("r") == "1":
                    row.clear()
                    continue
                out = {}
                for cell in row.findall(NS + "c"):
                    c = _col(cell.get("r", ""))
                    if c in colunas:
                        value = _cell_value(cell)
                        out[c] = strings[int(value)] if cell.get('t') == 's' and value else value
                if out:
                    yield out
                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]


def num_pedidos(v) -> int:
    try:
        return int(float(str(v or "0").replace(",", ".")))
    except ValueError:
        return 0


def aging_sem_mov(label: str) -> Optional[int]:
    m = re.search(r"Exceed\s+(\d+)\s+day", label or "", flags=re.I)
    return int(m.group(1)) if m else None


def aging_retidos(label: str) -> Optional[int]:
    s = label or ""
    if "超15天" in s:
        return 16  # representa >15 para fins de corte >10
    m = re.search(r"(\d+)天滞留", s)
    return int(m.group(1)) if m else None


def sem_mov_records(path: Path, gestao: Dict[str, dict]) -> Iterator[dict]:
    data_ref = data_do_nome(path)
    # B remessa, C pedidos, I código base atual, J nome base atual, K última operação,
    # O Aging, T pacote problemático.
    for r in iter_xlsx(path, {"B", "C", "I", "J", "K", "O", "T"}):
        aging_label = r.get("O", "")
        aging = aging_sem_mov(aging_label)
        if aging is None:
            continue
        codigo = norm_codigo(r.get("I"))
        g = gestao.get(codigo, {})
        pedidos = num_pedidos(r.get("C"))
        motivo = (r.get("T") or "").strip() or (r.get("K") or "").strip()
        yield {
            "indicador": "sem_mov", "data": data_ref,
            "remessa": (r.get("B") or "").strip(), "pedidos": pedidos,
            "base_codigo": codigo,
            "base_nome": (r.get("J") or g.get("base_nome_gestao", "")).strip(),
            "aging_label": aging_label, "aging_valor": aging,
            # No SEM MOV, "Exceed 3 days" já significa >3; idem para 10.
            "acima_3": 1 if aging >= 3 else 0,
            "acima_10": 1 if aging >= 10 else 0,
            "uf": g.get("uf", ""), "rm": g.get("rm", ""), "sc": g.get("sc", ""),
            "regional": g.get("regional", ""), "motivo": motivo,
            "arquivo": path.name,
        }


def retidos_records(path: Path, gestao: Dict[str, dict]) -> Iterator[dict]:
    data_ref = data_do_nome(path)
    # A remessa, B pedidos, C nome base, D código base, S/T problemas, V tempo retenção.
    for r in iter_xlsx(path, {"A", "B", "C", "D", "S", "T", "V"}):
        aging_label = r.get("V", "")
        aging = aging_retidos(aging_label)
        if aging is None:
            continue
        codigo = norm_codigo(r.get("D"))
        g = gestao.get(codigo, {})
        pedidos = num_pedidos(r.get("B"))
        motivo = (r.get("T") or "").strip() or (r.get("S") or "").strip()
        yield {
            "indicador": "retidos", "data": data_ref,
            "remessa": (r.get("A") or "").strip(), "pedidos": pedidos,
            "base_codigo": codigo,
            "base_nome": (r.get("C") or g.get("base_nome_gestao", "")).strip(),
            "aging_label": aging_label, "aging_valor": aging,
            # Retidos traz o número de dias; "acima de" é comparação estrita.
            "acima_3": 1 if aging > 3 else 0,
            "acima_10": 1 if aging > 10 else 0,
            "uf": g.get("uf", ""), "rm": g.get("rm", ""), "sc": g.get("sc", ""),
            "regional": g.get("regional", ""), "motivo": motivo,
            "arquivo": path.name,
        }


def importar_arquivo(path: Path, indicador: str, gestao: Dict[str, dict], batch_size: int = 5000):
    data_ref = data_do_nome(path)
    gerador = sem_mov_records(path, gestao) if indicador == "sem_mov" else retidos_records(path, gestao)
    RESUMOS_DIR.mkdir(parents=True, exist_ok=True)
    DETALHES_DIR.mkdir(parents=True, exist_ok=True)
    nome = f"{indicador}_{data_ref.isoformat()}"
    destino = DETALHES_DIR / (nome + '.csv.gz')
    temporario = destino.with_suffix('.tmp')
    grupos = defaultdict(lambda: [0, 0])
    total_linhas = total_pedidos = 0
    try:
        with gzip.open(temporario, 'wt', encoding='utf-8-sig', newline='') as out:
            writer = None
            for rec in gerador:
                if writer is None:
                    writer = csv.DictWriter(out, fieldnames=list(rec), delimiter=';')
                    writer.writeheader()
                writer.writerow(rec)
                key = tuple(str(rec[c]) for c in DIMENSOES)
                grupos[key][0] += rec['pedidos']
                grupos[key][1] += 1
                total_linhas += 1
                total_pedidos += rec['pedidos']
        if not total_linhas:
            raise ValueError(f'{path.name}: nenhuma linha reconhecida. Confira colunas e aging; carga anterior preservada.')
        rows = [dict(zip(DIMENSOES, key), pedidos=v[0], linhas=v[1]) for key, v in sorted(grupos.items())]
        atomic_csv(RESUMOS_DIR / (nome + '.csv'), rows, DIMENSOES + ['pedidos', 'linhas'])
        os.replace(temporario, destino)
    finally:
        temporario.unlink(missing_ok=True)
    print(f"  OK {indicador} | {data_ref} | {total_linhas:,} linhas | {total_pedidos:,} pedidos | {len(grupos):,} linhas no resumo")


def main():
    gestao = carregar_gestao(GESTAO_PADRAO)
    print(f"Gestão carregada: {len(gestao):,} bases")

    sem_files = sorted(SEM_DIR.glob("*.xlsx"), key=data_do_nome)
    ret_files = sorted(RET_DIR.glob("*.xlsx"), key=data_do_nome)
    if not sem_files and not ret_files:
        print("Nenhum XLSX encontrado em dados/sem_mov ou dados/retidos.")
        return

    # Um arquivo por indicador/data: duplicatas não podem sobrescrever silenciosamente.
    for files in (sem_files, ret_files):
        datas = [data_do_nome(f) for f in files]
        if len(datas) != len(set(datas)):
            raise ValueError('Há mais de um Excel para a mesma data/indicador. Mantenha apenas o arquivo correto.')
    print(f"SEM MOV: {len(sem_files)} arquivo(s) | RETIDOS: {len(ret_files)} arquivo(s)")
    for path in sem_files:
        importar_arquivo(path, "sem_mov", gestao)
    for path in ret_files:
        importar_arquivo(path, "retidos", gestao)
    print("Importação concluída.")


if __name__ == "__main__":
    main()
