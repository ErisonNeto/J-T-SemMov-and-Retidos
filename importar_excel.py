from __future__ import annotations

import argparse
import hashlib
import os
import re
from itertools import chain, islice
import shutil
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from lxml import etree
from openpyxl.utils.datetime import from_excel

BASE_DIR = Path(__file__).resolve().parent
DADOS_DIR = BASE_DIR / "dados"
SEM_DIR = DADOS_DIR / "sem_mov"
RET_DIR = DADOS_DIR / "retidos"
GESTAO_PADRAO = DADOS_DIR / "Gestão de colaboradores.xlsx"
CACHE_ZIP_DIR = DADOS_DIR / ".extraidos_importacao"

load_dotenv(BASE_DIR / ".env")

API_URL = os.getenv("JT_API_URL", "https://jt-api.rockettechbr.com.br").rstrip("/")
API_KEY = os.getenv("JT_API_KEY", "").strip()
API_TIMEOUT = float(os.getenv("JT_API_TIMEOUT", "120"))
BATCH_SIZE = max(100, int(os.getenv("JT_IMPORT_BATCH", "3000")))

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------

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


def normalizar(v) -> str:
    s = texto(v).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def num_pedidos(v) -> int:
    try:
        return int(float(str(v or "0").replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def aging_sem_mov(label: str) -> Optional[int]:
    """Converte os formatos de Aging usados nas diferentes exportações de Sem Mov.

    Exemplos aceitos: ``Exceed 3 day``, ``3 days``, ``3 dias``, ``3天``,
    ``acima de 3 dias`` e valores numéricos simples.
    """
    s = texto(label)
    if not s:
        return None

    s_norm = normalizar(s)

    # Formato clássico da base antiga.
    m = re.search(r"exceed(?:ed)?\s*(\d+)\s*day", s_norm, flags=re.I)
    if m:
        # 'Exceed 3 day' significa mais de 3 dias, logo pelo menos 4.
        return int(m.group(1)) + 1

    # Faixas como 3-5 dias / 3~5 days: usa o início da faixa.
    m = re.search(r"(?<!\d)(\d+)\s*[-~a]\s*(\d+)", s_norm)
    if m:
        return int(m.group(1))

    # Expressões de limite estrito: 'acima de 3 dias' representa 4+.
    limite_estrito = re.search(
        r"(?:acima|mais|maior|over|above|超过)\s*(?:de|than)?\s*(\d+)\s*(?:dia|dias|day|days|天)",
        s_norm, flags=re.I,
    )
    if limite_estrito:
        return int(limite_estrito.group(1)) + 1

    m = re.search(r"(?<!\d)(\d+)\s*(?:dia|dias|day|days|天)(?!\d)", s_norm, flags=re.I)
    if m:
        return int(m.group(1))

    # Alguns arquivos trazem apenas o número de dias na coluna Aging.
    try:
        n = float(s.replace(",", "."))
        if 0 <= n <= 365:
            return int(n)
    except (ValueError, TypeError):
        pass

    return None


def aging_retidos(label: str) -> Optional[int]:
    s = label or ""
    if "超15天" in s:
        return 16
    m = re.search(r"(\d+)天滞留", s)
    return int(m.group(1)) if m else None


def data_explicita_do_nome(path: Path) -> Optional[date]:
    """Lê apenas datas que claramente fazem parte do nome lógico do arquivo.

    Não usa o timestamp compacto de exports como ...20261102160606.xlsx, porque
    esse timestamp é a data de download/exportação e não necessariamente o dia
    operacional do indicador.
    """
    nome = normalizar(path.stem.replace("_", " "))

    m = re.search(
        r"(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)"
        r"[\s.-]+(\d{1,2})[\s.-]+(20\d{2})",
        nome,
    )
    if m:
        return date(int(m.group(3)), MESES[m.group(1)], int(m.group(2)))

    # YYYY-MM-DD / YYYY_MM_DD / YYYY.MM.DD
    m = re.search(r"(?<!\d)(20\d{2})[-_. ](0?[1-9]|1[0-2])[-_. ]([0-2]?\d|3[01])(?!\d)", path.stem)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD-MM-YYYY / DD_MM_YYYY / DD.MM.YYYY
    m = re.search(r"(?<!\d)([0-2]?\d|3[01])[-_. ](0?[1-9]|1[0-2])[-_. ](20\d{2})(?!\d)", path.stem)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # Arquivos operacionais como "retido 27.10.xlsx".
    m = re.search(r"(?<!\d)([0-2]?\d|3[01])[.-](0?[1-9]|1[0-2])(?![.\d])", path.stem)
    if m:
        hoje = date.today()
        candidato = date(hoje.year, int(m.group(2)), int(m.group(1)))
        # Evita escolher dezembro do ano seguinte quando estamos no começo de janeiro.
        if candidato > hoje + timedelta(days=40):
            candidato = date(hoje.year - 1, candidato.month, candidato.day)
        return candidato

    return None


# -----------------------------------------------------------------------------
# Gestão
# -----------------------------------------------------------------------------

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
        mapa[codigo] = {
            "base_nome_gestao": texto(row.get("Nome da base")),
            "uf": texto(row.get("Estado(UF)")),
            "rm": texto(row.get("Responsável da Região")),
            "sc": coord or sup,
            "regional": texto(row.get("Regional")),
        }
    return mapa


# -----------------------------------------------------------------------------
# Leitura XLSX por XML (streaming)
# -----------------------------------------------------------------------------

def _cell_value(cell) -> str:
    tipo = cell.get("t")
    if tipo == "inlineStr":
        return "".join((t.text or "") for t in cell.findall(".//" + NS + "t"))
    v = cell.find(NS + "v")
    return "" if v is None else (v.text or "")


def _col(ref: str) -> str:
    m = re.match(r"([A-Z]+)", ref or "")
    return m.group(1) if m else ""


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    strings: list[str] = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return strings
    with z.open("xl/sharedStrings.xml") as sf:
        for _, elem in etree.iterparse(sf, events=("end",), tag=NS + "si"):
            strings.append("".join(elem.itertext()))
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]
    return strings


def _valor_cell(cell, strings: list[str]) -> str:
    value = _cell_value(cell)
    if cell.get("t") == "s" and value:
        try:
            return strings[int(value)]
        except (ValueError, IndexError):
            return value
    return value


def _sheet_paths(z: zipfile.ZipFile) -> list[str]:
    def chave(nome: str):
        m = re.search(r"sheet(\d+)\.xml$", nome)
        return int(m.group(1)) if m else 999999
    return sorted(
        [n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")],
        key=chave,
    )


def iter_xlsx(path: Path, colunas: set[str]) -> Iterator[dict]:
    """Compatibilidade com o layout SEM MOV antigo, usando a primeira worksheet."""
    with zipfile.ZipFile(path) as z:
        sheets = _sheet_paths(z)
        if not sheets:
            raise ValueError(f"Planilha sem worksheet: {path.name}")
        strings = _shared_strings(z)
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
                        out[c] = _valor_cell(cell, strings)
                if out:
                    yield out
                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]



SEM_MOV_ALIASES = {
    "remessa": {
        "numero da remessa", "número da remessa", "remessa", "waybill no.", "waybill no",
    },
    "pedidos": {"pedidos", "pedido", "quantidade de pedidos", "qtd pedidos"},
    "base_codigo": {
        "codigo da base", "código da base", "codigo da base de entrega", "código da base de entrega",
        "codigo da estacao", "código da estação", "estacao no.", "estacao no", "estação no.", "estação no",
    },
    "base_nome": {
        "nome da base", "nome da base de entrega", "base de entrega", "estacao", "estação",
    },
    "aging_label": {
        "aging", "aging label", "faixa aging", "tempo sem movimentacao", "tempo sem movimentação",
        "dias sem movimentacao", "dias sem movimentação", "dias sem mov", "tempo de permanencia",
        "tempo de permanência", "dias parado", "dias parados",
    },
    "motivo_1": {
        "pacote problematico", "pacote problemático", "motivo", "tipo de problema",
        "tipos de problemas", "ultima operacao", "última operação", "ultimo status", "último status",
    },
    "motivo_2": {
        "tipos de problemas de primeiro nivel", "tipos de problemas de primeiro nível",
        "tipos de problemas de segundo nivel", "tipos de problemas de segundo nível",
    },
}


def _campo_sem_mov_por_cabecalho(cab: str) -> Optional[str]:
    """Mapeia o cabeçalho do Sem Mov sem depender da letra da coluna."""
    n = normalizar(cab)
    if not n:
        return None

    for campo, aliases in SEM_MOV_ALIASES.items():
        aliases_norm = {normalizar(a) for a in aliases}
        if n in aliases_norm:
            return campo

    # Fallbacks semânticos para pequenas mudanças de nomenclatura.
    if "remessa" in n or "waybill" in n:
        return "remessa"
    if "pedido" in n and "data" not in n:
        return "pedidos"
    if ("codigo" in n or "estacao no" in n) and ("base" in n or "estacao" in n):
        return "base_codigo"
    if "nome" in n and ("base" in n or "estacao" in n):
        return "base_nome"
    if "aging" in n or ("dia" in n and ("mov" in n or "parad" in n or "perman" in n)):
        return "aging_label"
    if "problema" in n or "motivo" in n or "ultima operacao" in n or "ultimo status" in n:
        return "motivo_1"
    return None


def _achar_coluna_aging_sem_mov(
    z: zipfile.ZipFile,
    sheet: str,
    strings: list[str],
    header_row: int,
    colunas_ja_usadas: set[str],
) -> Optional[str]:
    """Descobre a coluna de Aging pela amostra dos valores quando o cabeçalho mudou."""
    contagem: dict[str, int] = {}
    lidas = 0
    with z.open(sheet) as f:
        context = etree.iterparse(f, events=("end",), tag=NS + "row")
        for _, row in context:
            numero_linha = int(row.get("r") or 0)
            if numero_linha <= header_row:
                row.clear()
                continue
            for cell in row.findall(NS + "c"):
                col = _col(cell.get("r", ""))
                if not col or col in colunas_ja_usadas:
                    continue
                valor = _valor_cell(cell, strings)
                if aging_sem_mov(valor) is not None:
                    contagem[col] = contagem.get(col, 0) + 1
            lidas += 1
            row.clear()
            while row.getprevious() is not None:
                del row.getparent()[0]
            if lidas >= 120:
                break

    if not contagem:
        return None
    col, qtd = max(contagem.items(), key=lambda x: x[1])
    # Evita escolher uma coluna acidentalmente numérica por uma ou duas linhas.
    return col if qtd >= max(3, int(lidas * 0.15)) else None


def _layout_sem_mov_posicional_conhecido(path: Path) -> Optional[tuple[str, int, dict[str, str]]]:
    """Detecta rapidamente o layout antigo conhecido do Sem Mov.

    No layout histórico da J&T: B=remessa, C=pedidos, I=código da base,
    J=nome da base, K=última operação, O=aging e T=problema. Preferimos
    esse mapa quando a coluna O realmente contém Aging, evitando escolher
    por engano outra coluna de código existente no cabeçalho.
    """
    with zipfile.ZipFile(path) as z:
        sheets = _sheet_paths(z)
        if not sheets:
            return None
        strings = _shared_strings(z)
        sheet = sheets[0]
        validos = 0
        com_codigo = 0
        lidas = 0
        with z.open(sheet) as f:
            context = etree.iterparse(f, events=("end",), tag=NS + "row")
            for _, row in context:
                numero_linha = int(row.get("r") or 0)
                if numero_linha <= 1:
                    row.clear()
                    continue
                vals = {}
                for cell in row.findall(NS + "c"):
                    col = _col(cell.get("r", ""))
                    if col in {"I", "O"}:
                        vals[col] = _valor_cell(cell, strings)
                if aging_sem_mov(vals.get("O", "")) is not None:
                    validos += 1
                if norm_codigo(vals.get("I", "")):
                    com_codigo += 1
                lidas += 1
                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]
                if lidas >= 120:
                    break

        if lidas >= 10 and validos >= max(3, int(lidas * 0.15)) and com_codigo >= max(3, int(lidas * 0.50)):
            return sheet, 1, {
                "remessa": "B", "pedidos": "C", "base_codigo": "I", "base_nome": "J",
                "motivo_1": "K", "aging_label": "O", "motivo_2": "T",
            }
    return None



def _qualidade_layout_sem_mov(
    z: zipfile.ZipFile,
    sheet: str,
    strings: list[str],
    cab_linha: int,
    mapa: dict[str, str],
    limite: int = 160,
) -> tuple[int, int, int, int, int]:
    """Pontua a ABA pelos registros reais, não pelo total de cabeçalhos.

    Tabelas dinâmicas e resumos frequentemente contêm os mesmos cabeçalhos
    da lista principal, mas poucos registros e nenhuma contagem válida de pedidos.
    """
    importantes = {mapa[k] for k in ("remessa", "pedidos", "base_codigo", "aging_label")
                   if k in mapa}
    linhas = validas = pedidos_validos = remessas_validas = aging_validos = 0
    with z.open(sheet) as f:
        for _, row in etree.iterparse(f, events=("end",), tag=NS + "row"):
            nr = int(row.get("r") or 0)
            if nr <= cab_linha:
                row.clear()
                continue
            campos = {}
            for cell in row.findall(NS + "c"):
                col = _col(cell.get("r", ""))
                if col in importantes:
                    campos[col] = _valor_cell(cell, strings)
            if not campos:
                row.clear()
                continue
            linhas += 1
            remessa = texto(campos.get(mapa.get("remessa", "")))
            pedidos = num_pedidos(campos.get(mapa.get("pedidos", "")))
            base = norm_codigo(campos.get(mapa.get("base_codigo", "")))
            aging = aging_sem_mov(campos.get(mapa.get("aging_label", "")))
            rem_ok = len(remessa) >= 8 and any(c.isdigit() for c in remessa)
            if rem_ok:
                remessas_validas += 1
            if pedidos > 0:
                pedidos_validos += 1
            if aging is not None:
                aging_validos += 1
            if rem_ok and pedidos > 0 and base and aging is not None:
                validas += 1
            row.clear()
            while row.getprevious() is not None:
                del row.getparent()[0]
            if linhas >= limite:
                break
    return validas, pedidos_validos, remessas_validas, aging_validos, linhas


def localizar_layout_sem_mov(path: Path) -> tuple[str, int, dict[str, str]]:
    """Localiza a aba bruta de Sem Mov e suas colunas por cabeçalho/conteúdo.

    Isso suporta arquivos em que a primeira aba virou resumo/pivot e a base bruta
    foi deslocada para outra worksheet, como ocorreu nas exportações mais novas.
    """
    antigo = _layout_sem_mov_posicional_conhecido(path)
    if antigo is not None:
        return antigo

    candidatos: list[tuple[tuple[int, int, int, int, int], int, int, str, int, dict[str, str]]] = []

    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        for sheet in _sheet_paths(z):
            with z.open(sheet) as f:
                context = etree.iterparse(f, events=("end",), tag=NS + "row")
                for _, row in context:
                    numero_linha = int(row.get("r") or 0)
                    if numero_linha > 20:
                        row.clear()
                        break

                    mapa: dict[str, str] = {}
                    for cell in row.findall(NS + "c"):
                        col = _col(cell.get("r", ""))
                        valor = _valor_cell(cell, strings)
                        campo = _campo_sem_mov_por_cabecalho(valor)
                        if campo and campo not in mapa:
                            mapa[campo] = col

                    base_minima = {"remessa", "pedidos", "base_codigo"}
                    if base_minima.issubset(mapa):
                        if "aging_label" not in mapa:
                            aging_col = _achar_coluna_aging_sem_mov(
                                z, sheet, strings, numero_linha, set(mapa.values())
                            )
                            if aging_col:
                                mapa["aging_label"] = aging_col

                        if "aging_label" in mapa:
                            tamanho_xml = z.getinfo(sheet).file_size
                            qualidade = _qualidade_layout_sem_mov(
                                z, sheet, strings, numero_linha, mapa
                            )
                            score = len(mapa)
                            candidatos.append(
                                (qualidade, tamanho_xml, score, sheet, numero_linha, mapa)
                            )
                            row.clear()
                            break

                    row.clear()
                    while row.getprevious() is not None:
                        del row.getparent()[0]

    if candidatos:
        # Prioriza dados reais (pedidos/remessas/Aging), não cabeçalho de pivot.
        qualidade, _, _, sheet, numero_linha, mapa = max(
            candidatos, key=lambda x: (x[0][0], x[0][1], x[0][2], x[1], x[2])
        )
        if qualidade[0] < 3 or qualidade[1] < 3:
            raise ValueError(
                f"{path.name}: nenhuma aba com lista de Sem Mov confiável. "
                f"Melhor candidato {sheet} teve {qualidade[0]} registros completos "
                f"e {qualidade[1]} pedidos positivos em {qualidade[4]} linhas."
            )
        return sheet, numero_linha, mapa

    # Compatibilidade garantida com o layout antigo conhecido por posição.
    with zipfile.ZipFile(path) as z:
        sheets = _sheet_paths(z)
        if sheets:
            return sheets[0], 1, {
                "remessa": "B", "pedidos": "C", "base_codigo": "I", "base_nome": "J",
                "motivo_1": "K", "aging_label": "O", "motivo_2": "T",
            }

    raise ValueError(f"{path.name}: não encontrei nenhuma aba válida de Sem Mov.")


def iter_sem_mov_layout(path: Path) -> Iterator[dict]:
    sheet, header_row, mapa = localizar_layout_sem_mov(path)
    colunas = set(mapa.values())

    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        with z.open(sheet) as f:
            context = etree.iterparse(f, events=("end",), tag=NS + "row")
            for _, row in context:
                numero_linha = int(row.get("r") or 0)
                if numero_linha <= header_row:
                    row.clear()
                    continue

                por_col: dict[str, str] = {}
                for cell in row.findall(NS + "c"):
                    col = _col(cell.get("r", ""))
                    if col in colunas:
                        por_col[col] = _valor_cell(cell, strings)

                if por_col:
                    yield {campo: por_col.get(col, "") for campo, col in mapa.items()}

                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]

RETIDOS_ALIASES = {
    "remessa": {"numero da remessa", "remessa"},
    "pedidos": {"pedidos"},
    "base_nome": {"nome da base de entrega"},
    "base_codigo": {"codigo da base de entrega"},
    "coordenador_fonte": {"coordenador", "coordenador/supervisor", "supervisor"},
    "regional_fonte": {"nome da regional"},
    "aging_label": {"tempo de retencao"},
    "aging_dias": {"dias retido", "dias retidos"},
    "motivo_1": {"tipos de problemas de primeiro nivel"},
    "motivo_2": {"tipos de problemas de segundo nivel"},
    "data_inicio_retencao": {"data"},
    "horario_coleta": {"horario de coleta"},
    "horario_expedicao": {"horario de expedicao do sc"},
    "horario_recebimento": {"horario de recebimento na base"},
    "horario_saida": {"horario de saida para entrega"},
    "horario_entrega": {"horario da entrega"},
    "data_devolucao": {"data de devolucao"},
}

RETIDOS_OBRIGATORIOS = {"remessa", "pedidos", "base_nome", "base_codigo", "aging_label"}


def localizar_layout_retidos(path: Path) -> tuple[str, int, dict[str, str]]:
    """Localiza a worksheet bruta e as colunas pelo texto do cabeçalho.

    Há arquivos em que uma aba de detalhe/pivot também contém os mesmos
    cabeçalhos. Nesses casos escolhemos a worksheet de maior tamanho, que é a
    base bruta completa.
    """
    candidatos: list[tuple[int, str, int, dict[str, str]]] = []

    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        for sheet in _sheet_paths(z):
            with z.open(sheet) as f:
                context = etree.iterparse(f, events=("end",), tag=NS + "row")
                for _, row in context:
                    numero_linha = int(row.get("r") or 0)
                    if numero_linha > 15:
                        row.clear()
                        break

                    cab_por_col: dict[str, str] = {}
                    for cell in row.findall(NS + "c"):
                        col = _col(cell.get("r", ""))
                        val = normalizar(_valor_cell(cell, strings))
                        if col and val:
                            cab_por_col[col] = val

                    mapa: dict[str, str] = {}
                    for campo, aliases in RETIDOS_ALIASES.items():
                        for col, cab in cab_por_col.items():
                            if cab in aliases:
                                mapa[campo] = col
                                break

                    if RETIDOS_OBRIGATORIOS.issubset(mapa):
                        tamanho_xml = z.getinfo(sheet).file_size
                        candidatos.append((tamanho_xml, sheet, numero_linha, mapa))
                        row.clear()
                        break

                    row.clear()
                    while row.getprevious() is not None:
                        del row.getparent()[0]

    if candidatos:
        _, sheet, numero_linha, mapa = max(candidatos, key=lambda x: x[0])
        return sheet, numero_linha, mapa

    raise ValueError(
        f"{path.name}: não encontrei a aba bruta de Retidos. "
        "Esperava Remessa, Pedidos, Base, Código da Base e Tempo de retenção."
    )


def iter_retidos_layout(path: Path) -> Iterator[dict]:
    sheet, header_row, mapa = localizar_layout_retidos(path)
    colunas = set(mapa.values())

    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        with z.open(sheet) as f:
            context = etree.iterparse(f, events=("end",), tag=NS + "row")
            for _, row in context:
                numero_linha = int(row.get("r") or 0)
                if numero_linha <= header_row:
                    row.clear()
                    continue

                por_col: dict[str, str] = {}
                for cell in row.findall(NS + "c"):
                    col = _col(cell.get("r", ""))
                    if col in colunas:
                        por_col[col] = _valor_cell(cell, strings)

                if por_col:
                    yield {campo: por_col.get(col, "") for campo, col in mapa.items()}

                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]


def _excel_ou_texto_para_datetime(v) -> Optional[datetime]:
    s = texto(v)
    if not s:
        return None
    try:
        numero = float(s)
        if 20000 <= numero <= 80000:
            dt = from_excel(numero)
            return dt if isinstance(dt, datetime) else datetime.combine(dt, datetime.min.time())
    except (ValueError, TypeError, OverflowError):
        pass

    dt = pd.to_datetime(s, errors="coerce", dayfirst=False)
    if pd.isna(dt):
        return None
    return dt.to_pydatetime()


def inferir_data_retidos(path: Path) -> date:
    """Infere o dia operacional em exports genéricos sem data lógica no nome.

    O export da J&T pode ter nome ...20261102160606.xlsx mesmo contendo o
    snapshot de 30/10, 31/10 ou 01/11. Para esses casos usamos o maior horário
    operacional REAL existente no arquivo (não a data prevista de entrega).
    """
    campos_horario = {
        "horario_coleta", "horario_expedicao", "horario_recebimento",
        "horario_saida", "horario_entrega", "data_devolucao",
    }
    maior: Optional[datetime] = None
    for row in iter_retidos_layout(path):
        for campo in campos_horario:
            dt = _excel_ou_texto_para_datetime(row.get(campo))
            if dt is not None and (maior is None or dt > maior):
                maior = dt

    if maior is None:
        raise ValueError(
            f"{path.name}: não consegui inferir a data operacional pelo conteúdo. "
            "Renomeie para algo como retido_2026-11-01.xlsx."
        )
    return maior.date()



def inferir_data_sem_mov_coluna_a(path: Path) -> Optional[date]:
    """Lê a data de referência da coluna A quando seu cabeçalho for Data.

    Não usa a data de exportação nem a data da última movimentação.
    Confere TODAS as linhas de dados para evitar misturar dias num mesmo arquivo.
    """
    sheet, linha_cabecalho, _ = localizar_layout_sem_mov(path)
    nomes_confiaveis = {"data", "data de referencia", "data do relatorio",
                        "data da consulta", "data operacional", "data do snapshot"}
    data_encontrada: Optional[date] = None
    total_linhas = 0
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        with z.open(sheet) as f:
            context = etree.iterparse(f, events=("end",), tag=NS + "row")
            for _, row in context:
                numero_linha = int(row.get("r") or 0)
                if numero_linha < linha_cabecalho:
                    row.clear()
                    continue
                cells = row.findall(NS + "c")
                celula_a = next((c for c in cells if _col(c.get("r", "")) == "A"), None)
                valor = _valor_cell(celula_a, strings) if celula_a is not None else ""

                if numero_linha == linha_cabecalho:
                    if normalizar(valor) not in nomes_confiaveis:
                        return None
                elif cells:
                    if not texto(valor):
                        raise ValueError(
                            f"{path.name}: coluna A (Data) vazia na linha {numero_linha}."
                        )
                    data_dt = _excel_ou_texto_para_datetime(valor)
                    if data_dt is None:
                        raise ValueError(
                            f"{path.name}: data da coluna A inválida na linha {numero_linha}: {valor!r}"
                        )
                    dia = data_dt.date()
                    if data_encontrada is not None and dia != data_encontrada:
                        raise ValueError(
                            f"{path.name}: coluna A contém mais de um dia "
                            f"({data_encontrada} e {dia}, linha {numero_linha}). "
                            "Não é possível atribuir uma única data ao arquivo."
                        )
                    data_encontrada = dia
                    total_linhas += 1
                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]

    if total_linhas == 0:
        raise ValueError(f"{path.name}: cabeçalho Data encontrado, mas nenhuma linha válida.")
    if data_encontrada is None:
        return None
    print(f"    Sem Mov: data conferida na coluna A: {data_encontrada} "
          f"({total_linhas:,} linhas) | {path.name}")
    return data_encontrada


def data_referencia(path: Path, indicador: str) -> date:
    explicita = data_explicita_do_nome(path)
    if explicita is not None:
        return explicita
    if indicador == "sem_mov":
        data_da_coluna = inferir_data_sem_mov_coluna_a(path)
        if data_da_coluna is not None:
            return data_da_coluna
        raise ValueError(
            f"{path.name}: a coluna A não contém uma data de referência confiável. "
            "Confira o cabeçalho ou use uma data operacional comprovada no nome."
        )
    if indicador == "retidos":
        return inferir_data_retidos(path)
    raise ValueError(f"Indicador não reconhecido: {indicador}")


# -----------------------------------------------------------------------------
# Transformação
# -----------------------------------------------------------------------------

def detectar_indicador(path: Path) -> str:
    nome = normalizar(path.stem)

    # O nome do próprio arquivo é a evidência mais forte.
    if any(x in nome for x in ("retido", "retencao")):
        return "retidos"
    if any(x in nome for x in ("sem mov", "sem_mov", "movimentacao")):
        return "sem_mov"

    # Depois usa apenas os pais próximos/estruturados. Não olha todos os ancestrais,
    # pois o nome do projeto contém "sem_mov_retido" e causaria falso positivo.
    pais_proximos = [normalizar(path.parent.name), normalizar(path.parent.parent.name)]
    if _eh_subpath(path, RET_DIR) or any("retido" in p for p in pais_proximos):
        return "retidos"
    if _eh_subpath(path, SEM_DIR) or any("sem_mov" in p or "sem mov" in p for p in pais_proximos):
        return "sem_mov"

    # Fallback pela assinatura do Aging nas primeiras linhas do layout antigo.
    for i, r in enumerate(iter_xlsx(path, {"O", "V"})):
        if aging_sem_mov(r.get("O", "")) is not None:
            return "sem_mov"
        if aging_retidos(r.get("V", "")) is not None:
            return "retidos"
        if i >= 100:
            break
    raise ValueError(f"Não consegui identificar se o arquivo é SEM MOV ou RETIDOS: {path.name}")


def sem_mov_records(path: Path, gestao: Dict[str, dict], data_ref: date) -> Iterator[dict]:
    for r in iter_sem_mov_layout(path):
        aging_label = texto(r.get("aging_label"))
        aging = aging_sem_mov(aging_label)
        if aging is None:
            continue
        codigo = norm_codigo(r.get("base_codigo"))
        g = gestao.get(codigo, {})
        pedidos = num_pedidos(r.get("pedidos"))
        motivo = texto(r.get("motivo_2")) or texto(r.get("motivo_1"))
        yield {
            "indicador": "sem_mov",
            "data": data_ref.isoformat(),
            "remessa": texto(r.get("remessa")),
            "pedidos": pedidos,
            "base_codigo": codigo,
            "base_nome": texto(r.get("base_nome")) or g.get("base_nome_gestao", ""),
            "aging_label": aging_label or f"{aging} dias",
            "aging_valor": aging,
            "acima_3": 1 if aging > 3 else 0,
            "acima_10": 1 if aging > 10 else 0,
            "uf": g.get("uf", ""),
            "rm": g.get("rm", ""),
            "sc": g.get("sc", ""),
            "regional": g.get("regional", ""),
            "motivo": motivo,
            "arquivo": path.name,
        }


def retidos_records(path: Path, gestao: Dict[str, dict], data_ref: date) -> Iterator[dict]:
    for r in iter_retidos_layout(path):
        aging_label = texto(r.get("aging_label"))
        aging = aging_retidos(aging_label)
        if aging is None:
            # Alguns layouts também trazem o número de dias separado.
            try:
                aging = int(float(texto(r.get("aging_dias"))))
            except (ValueError, TypeError):
                continue

        codigo = norm_codigo(r.get("base_codigo"))
        g = gestao.get(codigo, {})
        pedidos = num_pedidos(r.get("pedidos"))
        motivo = texto(r.get("motivo_2")) or texto(r.get("motivo_1"))
        sc_fonte = texto(r.get("coordenador_fonte"))

        yield {
            "indicador": "retidos",
            "data": data_ref.isoformat(),
            "remessa": texto(r.get("remessa")),
            "pedidos": pedidos,
            "base_codigo": codigo,
            "base_nome": texto(r.get("base_nome")) or g.get("base_nome_gestao", ""),
            "aging_label": aging_label or f"{aging} dias",
            "aging_valor": aging,
            # Retido exatamente 3 dias não é "acima de 3"; começa em 4 dias.
            "acima_3": 1 if aging > 3 else 0,
            "acima_10": 1 if aging > 10 else 0,
            "uf": g.get("uf", ""),
            "rm": g.get("rm", ""),
            "sc": g.get("sc", "") or sc_fonte,
            "regional": g.get("regional", "") or texto(r.get("regional_fonte")),
            "motivo": motivo,
            "arquivo": path.name,
        }


def registro_chave(rec: dict) -> str:
    remessa = texto(rec.get("remessa"))
    if remessa:
        base = "|".join((rec["indicador"], rec["data"], remessa, texto(rec.get("base_codigo"))))
    else:
        campos = (
            "indicador", "data", "pedidos", "base_codigo", "base_nome", "aging_label",
            "aging_valor", "acima_3", "acima_10", "uf", "rm", "sc", "regional", "motivo",
        )
        base = "|".join(texto(rec.get(c)) for c in campos)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def testar_api(session: requests.Session) -> None:
    """Valida URL e X-API-Key antes de começar a ler arquivos grandes."""
    if not API_KEY:
        raise RuntimeError(
            "JT_API_KEY não foi carregada. Crie/ajuste o arquivo .env na mesma pasta "
            "do importar_excel.py e defina JT_API_KEY com a mesma chave configurada na JT API."
        )

    resp = session.post(
        f"{API_URL}/query",
        headers=_headers(),
        json={"sql": "SELECT 1 AS ok", "params": []},
        timeout=min(API_TIMEOUT, 30),
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "JT API recusou a autenticação (HTTP 401). O valor de JT_API_KEY no .env "
            "deste indicador não é o mesmo valor configurado no servidor da JT API."
        )
    if not resp.ok:
        raise RuntimeError(f"Falha ao testar JT API: HTTP {resp.status_code}: {resp.text[:1000]}")


def enviar_lote(session: requests.Session, registros: list[dict]) -> None:
    if not registros:
        return
    payload = {
        "tabela": "sem_mov_retidos_detalhe",
        "chave": ["indicador", "data", "remessa", "base_codigo"],
        "registros": registros,
        "criar_tabela": False,
        "adicionar_colunas": False,
    }
    resp = session.post(
        f"{API_URL}/import",
        headers=_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"JT API retornou HTTP {resp.status_code}: {resp.text[:1500]}")


def processar_arquivo(
    path: Path,
    indicador: str,
    data_ref: date,
    gestao: Dict[str, dict],
    session: Optional[requests.Session],
    dry_run: bool,
) -> tuple[int, int, int, int]:
    gerador = (
        sem_mov_records(path, gestao, data_ref)
        if indicador == "sem_mov"
        else retidos_records(path, gestao, data_ref)
    )

    lote: dict[str, dict] = {}
    linhas = pedidos = enviados = sem_gestao = 0

    # Segurança: antes de enviar qualquer lote, valida uma amostra do cruzamento
    # com Gestão. Isso detecta layout que apontou a coluna errada como código da base.
    amostra = list(islice(gerador, 3000))
    if not amostra:
        raise ValueError(f"{path.name}: nenhuma linha reconhecida. Confira colunas e Aging.")

    if indicador == "sem_mov":
        if sum(int(rec.get("pedidos") or 0) for rec in amostra) <= 0:
            raise ValueError(
                f"{path.name}: amostra de Sem Mov não contém pedidos positivos. "
                "Provável aba de resumo/pivot selecionada; importação bloqueada."
            )
        sem_match_amostra = sum(1 for rec in amostra if norm_codigo(rec.get("base_codigo")) not in gestao)
        taxa_sem_match = sem_match_amostra / len(amostra)
        if taxa_sem_match >= 0.70:
            exemplos = []
            for rec in amostra:
                cod = norm_codigo(rec.get("base_codigo"))
                if cod and cod not in gestao and cod not in exemplos:
                    exemplos.append(cod)
                if len(exemplos) >= 8:
                    break
            raise ValueError(
                f"cruzamento com Gestão reprovado: {taxa_sem_match:.1%} da amostra sem correspondência. "
                f"Provável coluna errada de código da base. Exemplos: {', '.join(exemplos) or '-'}"
            )

    for rec in chain(amostra, gerador):
        chave = registro_chave(rec)
        if chave not in lote:
            linhas += 1
            pedidos += int(rec["pedidos"])
            if not rec.get("rm"):
                sem_gestao += 1
        else:
            pedidos += int(rec["pedidos"]) - int(lote[chave]["pedidos"])
        lote[chave] = rec

        if len(lote) >= BATCH_SIZE:
            valores = list(lote.values())
            if not dry_run:
                assert session is not None
                enviar_lote(session, valores)
            enviados += len(valores)
            lote.clear()

    if lote:
        valores = list(lote.values())
        if not dry_run:
            assert session is not None
            enviar_lote(session, valores)
        enviados += len(valores)

    if linhas == 0:
        raise ValueError(f"{path.name}: nenhuma linha reconhecida. Confira colunas e Aging.")

    return linhas, pedidos, enviados, sem_gestao


# -----------------------------------------------------------------------------
# Localização de XLSX / ZIP
# -----------------------------------------------------------------------------

def _eh_subpath(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def extrair_zip(zip_path: Path) -> list[Path]:
    """Extrai somente XLSX, ignorando caminhos perigosos e arquivos temporários."""
    st = zip_path.stat()
    assinatura = f"{zip_path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    digest = hashlib.sha1(assinatura.encode("utf-8")).hexdigest()[:10]
    destino = CACHE_ZIP_DIR / f"{zip_path.stem}_{digest}"
    destino.mkdir(parents=True, exist_ok=True)

    saida: list[Path] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            nome = Path(info.filename).name
            if not nome.lower().endswith(".xlsx") or nome.startswith("~$"):
                continue
            alvo = destino / nome
            if not alvo.exists() or alvo.stat().st_size != info.file_size:
                with z.open(info) as src, open(alvo, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            saida.append(alvo)
    return saida


def coletar_candidatos(fontes: list[str]) -> list[Path]:
    candidatos: list[Path] = []

    if fontes:
        bases = [Path(f).expanduser().resolve() for f in fontes]
    else:
        bases = [DADOS_DIR, BASE_DIR]

    for base in bases:
        if not base.exists():
            print(f"  IGNORADO: caminho não existe: {base}")
            continue
        if base.is_dir():
            candidatos.extend(base.rglob("*.xlsx"))
            candidatos.extend(base.rglob("*.zip"))
        elif base.suffix.lower() in {".xlsx", ".zip"}:
            candidatos.append(base)

    # Expande ZIPs. ZIP dentro da própria pasta de cache não será recursivamente reaberto.
    expandidos: list[Path] = []
    vistos: set[Path] = set()
    for p in candidatos:
        p = p.resolve()
        if p in vistos:
            continue
        vistos.add(p)
        if _eh_subpath(p, CACHE_ZIP_DIR):
            continue
        if p.suffix.lower() == ".zip":
            try:
                expandidos.extend(extrair_zip(p))
            except zipfile.BadZipFile:
                print(f"  IGNORADO: ZIP inválido: {p.name}")
        elif p.suffix.lower() == ".xlsx":
            expandidos.append(p)

    return expandidos


def _parece_arquivo_gestao(path: Path) -> bool:
    # Pode existir outra copia da Gestão no diretório raiz ou dentro de ZIP.
    # Alguns ZIPs antigos também podem trazer o nome com mojibake (ex.: Gest├úo).
    # Em todos os casos, "gest...colaboradores" identifica a planilha de apoio.
    nome = path.name.lower().strip()
    return nome.startswith("gest") and "colaboradores" in nome


def localizar_arquivos(fontes: list[str], gestao_path: Path) -> list[tuple[Path, str, date]]:
    encontrados: dict[tuple[str, str, str], tuple[Path, str, date]] = {}
    gestao_real = gestao_path.resolve() if gestao_path.exists() else None

    for path in coletar_candidatos(fontes):
        try:
            if gestao_real and path.resolve() == gestao_real:
                continue
            if _parece_arquivo_gestao(path):
                continue
            if path.name.startswith("~$"):
                continue
            indicador = detectar_indicador(path)
            data_ref = data_referencia(path, indicador)
            # Mesmo conteúdo pode aparecer solto e dentro de ZIP. Mantém um por nome/data/indicador.
            chave = (indicador, data_ref.isoformat(), path.name.lower())
            encontrados[chave] = (path, indicador, data_ref)
        except Exception as exc:
            print(f"  IGNORADO: {path.name} | {exc}")

    return sorted(encontrados.values(), key=lambda x: (x[2], x[1], x[0].name.lower()))


def imprimir_cobertura(arquivos: list[tuple[Path, str, date]]) -> None:
    for indicador in ("sem_mov", "retidos"):
        datas = sorted({d for _, ind, d in arquivos if ind == indicador})
        if not datas:
            continue
        esperado = {datas[0] + timedelta(days=i) for i in range((datas[-1] - datas[0]).days + 1)}
        faltando = sorted(esperado - set(datas))
        print(f"Cobertura {indicador}: {datas[0]} até {datas[-1]} | {len(datas)} dia(s)")
        if faltando:
            print("  ATENÇÃO - dia(s) sem arquivo: " + ", ".join(d.strftime("%d/%m/%Y") for d in faltando))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Importa múltiplos XLSX/ZIP de Sem Mov e Retidos para a JT API/PostgreSQL."
    )
    parser.add_argument(
        "fontes",
        nargs="*",
        help="Arquivos XLSX/ZIP ou pastas. Sem argumentos, varre dados/ e a pasta do projeto.",
    )
    parser.add_argument(
        "--gestao",
        default=str(GESTAO_PADRAO),
        help="Caminho do Excel Gestão de colaboradores.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lê, valida e contabiliza tudo sem enviar para a API.",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="Testa somente URL/chave da JT API e encerra, sem ler planilhas nem importar.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Interrompe no primeiro arquivo com erro. Sem esta opção, registra o erro e continua os demais.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gestao_path = Path(args.gestao).expanduser().resolve()

    if not API_URL and not args.dry_run:
        raise RuntimeError("Defina JT_API_URL no .env.")

    if args.check_api:
        with requests.Session() as session:
            print(f"Testando JT API: {API_URL}")
            testar_api(session)
            print("JT API: autenticação OK")
        return

    gestao = carregar_gestao(gestao_path)
    print("=" * 88)
    print("IMPORTAÇÃO SEM MOV / RETIDOS - J&T")
    print("=" * 88)
    print(f"Gestão: {gestao_path.name} | {len(gestao):,} códigos únicos")
    print(f"Modo: {'VALIDAÇÃO (sem envio)' if args.dry_run else 'IMPORTAÇÃO'}")
    if not args.dry_run:
        print(f"Destino: {API_URL}/import")

    arquivos = localizar_arquivos(args.fontes, gestao_path)
    if not arquivos:
        print("Nenhum XLSX de SEM MOV/RETIDOS encontrado.")
        return

    print(f"Arquivos reconhecidos: {len(arquivos)}")
    for path, indicador, data_ref in arquivos:
        print(f"  - {data_ref} | {indicador:8s} | {path.name}")
    print("-" * 88)
    imprimir_cobertura(arquivos)
    print("=" * 88)

    total_linhas = total_pedidos = total_enviados = total_sem_gestao = 0
    erros: list[tuple[Path, str]] = []
    session: Optional[requests.Session] = None if args.dry_run else requests.Session()
    try:
        if session is not None:
            print("Testando autenticação da JT API...")
            testar_api(session)
            print("JT API: autenticação OK")
            print("-" * 88)

        for path, indicador, data_ref in arquivos:
            try:
                linhas, pedidos, enviados, sem_gestao = processar_arquivo(
                    path, indicador, data_ref, gestao, session, args.dry_run
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if args.strict:
                    raise
                erros.append((path, str(exc)))
                print(f"ERRO {indicador:8s} | {data_ref} | {path.name} | {exc}")
                continue

            total_linhas += linhas
            total_pedidos += pedidos
            total_enviados += enviados
            total_sem_gestao += sem_gestao
            sufixo = "validados" if args.dry_run else "upserts"
            print(
                f"  OK {indicador:8s} | {data_ref} | {linhas:,} registros | "
                f"{pedidos:,} pedidos | {sem_gestao:,} sem Gestão | {enviados:,} {sufixo} | {path.name}"
            )
    finally:
        if session is not None:
            session.close()

    print("=" * 88)
    print(f"Registros processados: {total_linhas:,}")
    print(f"Pedidos processados:   {total_pedidos:,}")
    print(f"Linhas sem Gestão:      {total_sem_gestao:,}")
    print(f"{'Registros validados' if args.dry_run else 'Upserts enviados'}: {total_enviados:,}")
    if erros:
        print(f"Arquivos com erro:      {len(erros)}")
        for path, erro in erros:
            print(f"  - {path.name}: {erro}")
        print("Execução concluída com erro(s); os demais arquivos foram processados normalmente.")
    else:
        print("Validação concluída." if args.dry_run else "Importação concluída.")


if __name__ == "__main__":
    main()
