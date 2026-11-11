# -*- coding: utf-8 -*-
"""Percentuais de Sem Mov e Retidos x volume de outlet (A/I/J/M).

Le somente a planilha de volume de outlet, a Gestao publicada e os
indicadores no PostgreSQL via JT API /query. Nao altera nenhum registro
no banco nem substitui os indicadores de >3/>10 do painel.

Numerador Sem Mov: pedidos com aging_valor > 2.
Numerador Retidos: pedidos com aging_valor > 3.
Denominador: quantidade coletada (J) + saida para entrega (M).

ATENCAO: aging de Retidos e tempo de retencao informado no relatorio
original, que nao necessariamente equivale a dias apos vencimento do SLA.
Para o KPI estrito de vencidos >3 dias sera necessaria a data de vencimento.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.datetime import from_excel

import importar_excel as core

ROOT = Path(__file__).resolve().parent


def str_clean(v):
    return "" if v is None else str(v).strip()


def codigo(v):
    s = core.norm_codigo(v)
    # Alguns relatorios exportam codigos numericos como texto com ".00".
    s = re.sub(r"^(\d{3,10})[.,]0+$", r"\1", s)
    return s if re.fullmatch(r"\d{3,10}", s) else ""


def numero(v):
    if v is None or str(v).strip() == "":
        return 0
    if isinstance(v, bool):
        raise ValueError("quantidade booleana")
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip().replace("\u00a0", "").replace(" ", "")
        if "%" in s:
            raise ValueError("porcentagem encontrada em coluna de quantidade")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif s.count(".") == 1 and len(s.rsplit(".", 1)[-1]) == 3:
            s = s.replace(".", "")
        elif s.count(",") == 1 and len(s.rsplit(",", 1)[-1]) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
        x = float(s)
    if x < 0 or x != x or not x.is_integer():
        raise ValueError(f"quantidade invalida: {v!r}")
    return int(x)


def dia(v):
    """Data operacional em formatos Excel, J&T e ISO, sem inferir pelo download."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if int(v) == v and 20000101 <= v <= 20991231:
            try:
                return date.fromisoformat(f"{int(v):08d}"[:4] + "-" +
                                          f"{int(v):08d}"[4:6] + "-" +
                                          f"{int(v):08d}"[6:8])
            except ValueError:
                return None
        if 20000 <= v <= 80000:
            dt = from_excel(v)
            return dt.date() if isinstance(dt, datetime) else dt
        return None
    s = str_clean(v).strip("\u200b\ufeff")
    if not s:
        return None
    # Um campo textual AAAAMMDD ou carimbo da meia-noite.
    m = re.fullmatch(r"(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?:[ T]00(?::?00){0,2})?", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # Excel pode armazenar o serial como TEXTO em vez de numero.
    if re.fullmatch(r"\d{5}(?:[.,]0+)?", s):
        try:
            return dia(float(s.replace(",", ".")))
        except (ValueError, OverflowError):
            return None
    for fmt in (
        "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d",
        "%d-%m-%Y", "%d.%m.%Y", "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
        "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M",
        "%Y/%m/%d %H:%M", "%d-%m-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # ISO com milissegundos, timezone, ou UTC Z (data do proprio campo).
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def extrair(payload, campos):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next(
            (payload[k] for k in ("rows", "data", "result", "resultado", "registros", "items")
             if isinstance(payload.get(k), list)), None
        )
        if rows is None:
            raise ValueError("JT API: nenhuma lista de linhas na resposta")
    else:
        raise ValueError("JT API: formato inesperado")
    return [
        dict(row) if isinstance(row, dict) else dict(zip(campos, row))
        for row in rows
    ]


def consulta(session, sql, campos, params=None):
    resp = session.post(
        f"{core.API_URL}/query",
        headers={**core._headers(), "Accept-Encoding": "identity"},
        json={"sql": sql, "params": params or []},
        timeout=max(core.API_TIMEOUT, 120),
    )
    if not resp.ok:
        raise RuntimeError(
            f"JT API /query falhou: HTTP {resp.status_code} | "
            f"resposta do servidor: {resp.text[:1200]} | "
            f"consulta: {sql.strip()[:300]}"
        )
    return extrair(resp.json(), campos)


def gestao_publicada(session):
    campos = ["base_codigo", "base_nome", "rm", "sc", "uf"]
    # Faz consulta simples para compatibilidade com a JT API /query.
    # A tabela tem poucas centenas de bases; o filtro Orlean e feito
    # no cliente para evitar que filtros SQL complexos causem erro 500.
    rows = consulta(session, """
        SELECT base_codigo, base_nome, rm, sc, uf
        FROM public.jt_gestao_bases
    """, campos)
    saida = {}
    for r in rows:
        cod = codigo(r.get("base_codigo"))
        if cod and "ORLEAN" in str_clean(r.get("rm")).upper():
            saida[cod] = {k: str_clean(r.get(k)) for k in campos}
    if not saida:
        raise RuntimeError("Nenhuma base de Orlean encontrada na Gestao publicada.")
    return saida


def escolher_aba(wb, nome):
    if nome:
        if nome not in wb.sheetnames:
            raise RuntimeError("Aba nao encontrada. Disponiveis: " + ", ".join(wb.sheetnames))
        return wb[nome]
    # Prioriza coincidencia entre data da coluna A e codigo de base da I.
    # Diagnostico em caso de erro: mostra somente exemplos destas colunas,
    # sem atribuir datas pelo nome do arquivo ou escolher base por aproximacao.
    melhor = None
    melhor_score = -1
    diagnosticos = []
    for ws in wb.worksheets:
        # Alguns XLSX exportados pelo sistema J&T declaram a dimensao
        # A1:M1 mesmo quando a worksheet possui milhares de linhas.
        # Em read_only=True o openpyxl confia nessa dimensao e le so
        # o cabecalho. reset_dimensions() obriga a varrer as linhas reais.
        # Continua usando EXATAMENTE A (data) e I (codigo).
        if hasattr(ws, "reset_dimensions"):
            dimensao_informada = ws.calculate_dimension()
            if ws.max_row is None or ws.max_row <= 1:
                ws.reset_dimensions()
                print(
                    f"  Outlet: ignorando dimensao Excel incorreta "
                    f"{dimensao_informada!r} na aba {ws.title!r}; "
                    "lendo as linhas reais do XML.",
                    flush=True,
                )
        score = datas_validas = codigos_validos = 0
        exemplos = []
        for n, row in enumerate(ws.iter_rows(min_col=1, max_col=13, values_only=True)):
            if n >= 3000:
                break
            d = dia(row[0])
            cod = codigo(row[8])
            datas_validas += int(d is not None)
            codigos_validos += int(bool(cod))
            if d is not None and cod:
                score += 1
            if len(exemplos) < 5 and any(str_clean(row[k]) for k in (0,8,9,12)):
                exemplos.append(
                    f"linha {n+1}: A={str(row[0])[:70]!r}, "
                    f"I={str(row[8])[:70]!r}, "
                    f"J={str(row[9])[:30]!r}, M={str(row[12])[:30]!r}"
                )
        diagnosticos.append(
            f"ABA {ws.title!r}: coincidencias_A_I={score}, "
            f"datas_A={datas_validas}, codigos_I={codigos_validos}\n  "
            + "\n  ".join(exemplos)
        )
        if score > melhor_score:
            melhor, melhor_score = ws, score
    if melhor is None or melhor_score <= 0:
        raise RuntimeError(
            "Nao encontrei datas validas na coluna A JUNTO de codigos de base "
            "na coluna I. NENHUM DADO FOI ENVIADO.\n"
            "Diagnostico das primeiras linhas da planilha:\n"
            + "\n".join(diagnosticos)
            + "\nConfira se a data operacional realmente esta em A e o "
            "codigo numerico da base em I. Nao vou trocar colunas "
            "automaticamente sem conferir o layout."
        )
    return melhor


def carregar_volume(arquivo, gestao, aba):
    wb = load_workbook(arquivo, data_only=True, read_only=True)
    try:
        ws = escolher_aba(wb, aba)
        # A selecao acima ja corrige, quando necessario, o limite errado
        # de linhas informado pelo XLSX. Reutilizamos a mesma worksheet.
        volume = defaultdict(lambda: [0, 0])
        lidas = validas = fora_orlean = invalidas = 0
        for row in ws.iter_rows(min_col=1, max_col=13, values_only=True):
            lidas += 1
            dt = dia(row[0])             # A: Data
            cod = codigo(row[8])         # I: Codigo da base
            if dt is None or not cod:
                invalidas += 1
                continue
            if cod not in gestao:
                fora_orlean += 1
                continue
            try:
                coletada = numero(row[9])       # J: Quantidade coletada
                saida = numero(row[12])         # M: Saida para entrega
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{ws.title}: linha {lidas}, base {cod}, data {dt}: {exc}"
                ) from exc
            volume[(dt.isoformat(), cod)][0] += coletada
            volume[(dt.isoformat(), cod)][1] += saida
            validas += 1
        if not volume:
            raise RuntimeError("Nenhuma linha valida das bases Orlean nas colunas A/I/J/M.")
        print(
            f"Aba: {ws.title} | linhas lidas: {lidas:,} | Orlean: {validas:,} "
            f"| outras bases: {fora_orlean:,} | cabecalhos/invalidas: {invalidas:,}"
        )
        return dict(volume)
    finally:
        wb.close()


def carregar_indicadores(session, volume):
    datas = sorted({k[0] for k in volume})
    codigos = sorted({k[1] for k in volume})
    campos = ["data", "base_codigo", "indicador", "pedidos"]
    rows = consulta(session, """
        SELECT data::text AS data, base_codigo, indicador,
               SUM(COALESCE(pedidos, 0))::bigint AS pedidos
        FROM public.sem_mov_retidos_detalhe
        WHERE data >= %s::date AND data <= %s::date
          AND base_codigo = ANY(%s::text[])
          AND (
             (indicador = 'sem_mov' AND aging_valor > 2)
             OR (indicador = 'retidos' AND aging_valor > 3)
          )
        GROUP BY data, base_codigo, indicador
    """, campos, [datas[0], datas[-1], codigos])
    numeradores = defaultdict(dict)
    for row in rows:
        key = (str_clean(row["data"]), codigo(row["base_codigo"]))
        numeradores[key][str_clean(row["indicador"])] = int(row["pedidos"] or 0)

    cobertura = consulta(session, """
        SELECT data::text AS data, indicador, COUNT(*)::bigint AS linhas
        FROM public.sem_mov_retidos_detalhe
        WHERE data >= %s::date AND data <= %s::date
          AND base_codigo = ANY(%s::text[])
          AND indicador IN ('sem_mov', 'retidos')
        GROUP BY data, indicador
    """, ["data", "indicador", "linhas"], [datas[0], datas[-1], codigos])
    dias_com_carga = {
        (str_clean(row["data"]), str_clean(row["indicador"]))
        for row in cobertura if int(row["linhas"] or 0) > 0
    }
    return numeradores, dias_com_carga


def publicar_volume_postgres(session, volume, lote=100):
    """Persiste J/M por dia e base para o dashboard Render.

    API /import com tabela exclusiva e chave (data, base_codigo).
    Reenvio de um dia atualiza valores, sem duplicar a volumetria.
    Nao altera sem_mov_retidos_detalhe nem a Gestao publicada.
    """
    if not volume:
        return 0
    if lote < 1 or lote > 500:
        raise ValueError("lote de Outlet fora do limite (1..500)")
    registros = []
    for (dt, cod), (j, m) in sorted(volume.items()):
        if not codigo(cod) or dia(dt) is None:
            raise RuntimeError(f"Outlet invalido para {dt!r}/{cod!r}")
        j, m = int(j), int(m)
        if j < 0 or m < 0:
            raise RuntimeError("Volume J/M nao pode ser negativo")
        registros.append({
            "data": dt,
            "base_codigo": cod,
            "coletada": j,
            "saida_entrega": m,
        })
    total = 0
    for pos in range(0, len(registros), lote):
        parte = registros[pos:pos + lote]
        payload = {
            "tabela": "jt_outlet_volume_diario",
            "chave": ["data", "base_codigo"],
            "registros": parte,
            "criar_tabela": True,
            "adicionar_colunas": True,
        }
        resp = session.post(
            f"{core.API_URL}/import",
            headers=core._headers(),
            json=payload,
            timeout=max(core.API_TIMEOUT, 120),
        )
        if not resp.ok:
            raise RuntimeError(
                "Nao foi possivel publicar o denominador Outlet na tabela "
                "jt_outlet_volume_diario. Verifique permissoes CREATE/INSERT "
                "da JT API ou crie a tabela pela migracao SQL. "
                f"HTTP {resp.status_code}: {resp.text[:650]}"
            )
        total += len(parte)
        print(
            f"  VOLUME OUTLET PostgreSQL: {total:,}/{len(registros):,} "
            f"base(s)-dia enviadas", flush=True
        )
    return total


def agregar(volume, gestao, numeradores, dias_com_carga, modo):
    grupos = defaultdict(lambda: [0, 0, 0, 0])
    for (dt, cod), (j, m) in volume.items():
        g = gestao[cod]
        chave = (dt, cod) if modo == "Bases" else (
            (dt, g["sc"] or "SEM S/C") if modo == "S-C" else (dt, "ORLEAN")
        )
        nums = numeradores.get((dt, cod), {})
        atual = grupos[chave]
        atual[0] += j
        atual[1] += m
        atual[2] += int(nums.get("sem_mov", 0))
        atual[3] += int(nums.get("retidos", 0))
    resultado = []
    for (dt, ident), (j, m, sem, ret) in sorted(grupos.items()):
        total = j + m
        base = gestao[ident]["base_nome"] if modo == "Bases" else ""
        sc = gestao[ident]["sc"] if modo == "Bases" else (
            ident if modo == "S-C" else "Orlean"
        )
        sem_ok = (dt, "sem_mov") in dias_com_carga
        ret_ok = (dt, "retidos") in dias_com_carga
        resultado.append([
            dt, ident if modo == "Bases" else "", base, sc,
            j, m, total,
            sem if sem_ok else None, (sem / total if total > 0 and sem_ok else None),
            ret if ret_ok else None, (ret / total if total > 0 and ret_ok else None),
        ])
    return resultado


CAB = [
    "Data", "Codigo da base", "Nome da base", "S/C",
    "Quantidade coletada (J)", "Saida para entrega (M)", "Volume (J + M)",
    "Sem Mov >2 dias (pedidos)", "Sem Mov (%)",
    "Retidos >3 dias (pedidos)", "Retidos (%)",
]


def salvar_excel(caminho, por_visao):
    wb = Workbook()
    wb.remove(wb.active)
    for nome, rows in por_visao.items():
        ws = wb.create_sheet(nome)
        ws.append(CAB)
        for linha in rows:
            ws.append(linha)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 29
        for cel in ws[1]:
            cel.font = Font(name="Calibri", size=11, color="FFFFFF", bold=True)
            cel.fill = PatternFill("solid", fgColor="C3161C")
            cel.alignment = Alignment(wrap_text=True, vertical="center")
        for row in ws.iter_rows(min_row=2):
            for col in (5, 6, 7, 8, 10):
                row[col-1].number_format = "#,##0"
            for col in (9, 11):
                row[col-1].number_format = "0.00%"
            for cel in row:
                if cel.row % 2 == 0:
                    cel.fill = PatternFill("solid", fgColor="F5F6F8")
        for letra, largura in {
            "A": 14, "B": 17, "C": 30, "D": 36, "E": 24,
            "F": 25, "G": 20, "H": 25, "I": 18, "J": 25, "K": 18,
        }.items():
            ws.column_dimensions[letra].width = largura
    notas = wb.create_sheet("Criterios")
    for linha in [
        ["Origem", "Relatorio: Analise do volume de negocios do outlet (novo) - Resumo"],
        ["Data", "Coluna A"],
        ["Base", "Coluna I, filtrada pela Gestao publicada de R/M Orlean"],
        ["Coletada", "Coluna J"],
        ["Saida para entrega", "Coluna M"],
        ["Denominador", "Soma J + M por data/base; S/C por Gestao publicada"],
        ["Sem Mov (%)", "Pedidos em sem_mov com aging_valor > 2 / volume operacional"],
        ["Retidos (%)", "Pedidos em retidos com aging_valor > 3 / volume operacional"],
        ["Atencao Retidos", "Tempo retido >3 nao comprova vencimento +3 apos prazo. Falta data de vencimento."],
        ["Periodo", "Nao fazer media simples de percentuais; somar numeradores e denominadores."],
        ["Sem carga", "Celula do indicador fica vazia se nao houver snapshot no PostgreSQL."],
        ["Metas", "Nao aplicadas: os cabecalhos das colunas de metas nao foram informados."],
        ["Banco", "Somente consulta /query, NENHUM INSERT/UPDATE/DELETE."],
    ]:
        notas.append(linha)
    notas.column_dimensions["A"].width = 23
    notas.column_dimensions["B"].width = 115
    notas.freeze_panes = "A2"
    wb.save(caminho)


def main():
    ap = argparse.ArgumentParser(
        description="Calcula porcentagem Sem Mov >2 e Retidos >3 usando A/I/J/M do outlet."
    )
    ap.add_argument("arquivo", help="Arquivo XLSX de volume de negocios do outlet.")
    ap.add_argument("--aba", help="Nome da aba se o arquivo contiver varias tabelas.")
    ap.add_argument("--saida", help="Relatorio XLSX de saida (opcional).")
    args = ap.parse_args()
    arquivo = Path(args.arquivo).expanduser().resolve()
    if not arquivo.is_file():
        raise RuntimeError(f"Planilha nao encontrada: {arquivo}")
    with requests.Session() as session:
        core.testar_api(session)
        gestao = gestao_publicada(session)
        print(f"Gestao publica Orlean: {len(gestao)} codigos | sem dividir coordenador/supervisor")
        volume = carregar_volume(arquivo, gestao, args.aba)
        numeradores, dias_com_carga = carregar_indicadores(session, volume)

    visoes = {
        nome: agregar(volume, gestao, numeradores, dias_com_carga, nome)
        for nome in ("Orlean", "S-C", "Bases")
    }
    saida = (
        Path(args.saida).expanduser().resolve()
        if args.saida else ROOT / "dados" / ("percentuais_outlet_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".xlsx")
    )
    saida.parent.mkdir(parents=True, exist_ok=True)
    salvar_excel(saida, visoes)
    datas = sorted({dt for dt, _ in volume})
    print(f"Datas: {datas[0]} ate {datas[-1]} | bases-dia: {len(volume):,}")
    for row in visoes["Orlean"]:
        dt, _, _, _, j, m, vol, sem, sem_pct, ret, ret_pct = row
        form = lambda x: "SEM CARGA" if x is None else f"{100*x:.2f}%"
        print(
            f"{dt}: coletada={j:,} + saida={m:,} = {vol:,} | "
            f"Sem Mov >2={sem if sem is not None else 'SEM CARGA'} ({form(sem_pct)}) | "
            f"Retidos >3={ret if ret is not None else 'SEM CARGA'} ({form(ret_pct)})"
        )
    print("Relatorio gerado:", saida)
    print("ATENCAO: Retidos >3 usa tempo de retencao, nao vencimento do prazo.")
    print("NENHUM DADO FOI MODIFICADO NO POSTGRESQL.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr, flush=True)
        sys.exit(1)
