# -*- coding: utf-8 -*-
"""
Auditoria SOMENTE LEITURA de planilhas/ZIPs Sem Movimentação.
Não conecta à API, não altera PostgreSQL e não importa dados.

Uso (na pasta do projeto):
  python auditar_sem_mov.py "sem mov 19 ao 30(1).zip" "29, 31, 01 e 02.zip"

Gera auditoria_sem_mov.csv e cobertura_sem_mov.csv.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import importar_excel as core

DIAS_ESPERADOS = [date(2026, 10, 19) + timedelta(days=i) for i in range(15)]
MAX_COMPACTADO = 2_500_000_000  # limite por planilha para evitar extrações acidentais gigantes


def chave_remessa(valor: str) -> bytes:
    return hashlib.blake2b(valor.strip().upper().encode("utf-8"), digest_size=12).digest()


def valor_aging(label: str):
    """Converte uma faixa 'Exceed N day' em limite inferior estrito N+1.
    Outros valores seguem a leitura do importador; faixas ambíguas ficam sinalizadas.
    """
    bruto = str(label or "").strip()
    s = core.normalizar(bruto)
    m = re.search(r"\bexceed(?:ed)?\s*(\d+)\s*day", s)
    if m:
        return int(m.group(1)) + 1, "limite_exceed"
    m = re.search(r"\b(?:acima|mais|maior|over|above)\s*(?:de|than)?\s*(\d+)\s*(?:dia|day)", s)
    if m:
        return int(m.group(1)) + 1, "limite_estrito"
    if re.search(r"\d+\s*[-~]\s*\d+", s):
        return None, "faixa_ambigua"
    aging = core.aging_sem_mov(bruto)
    return aging, "numerico" if aging is not None else "nao_reconhecido"


def data_do_nome(nome: str):
    # Só datas explicitamente associadas ao relatório, não timestamp YYYYMMDDHHMMSS.
    try:
        d = core.data_explicita_do_nome(Path(nome))
        return d.isoformat() if d else ""
    except Exception:
        return ""


def encontrar_origem(entradas):
    for p in entradas:
        x = Path(p).expanduser()
        if not x.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {x}")
        if x.is_dir():
            for f in sorted(x.rglob("*")):
                if f.is_file() and not f.name.startswith("~$") and f.suffix.lower() in (".zip", ".xlsx"):
                    yield f
        elif not x.name.startswith("~$"):
            yield x


def _data_em_celula(valor):
    """Converte data Excel (serial) ou texto; nunca interpreta código de remessa como data."""
    v = str(valor or "").strip()
    if not v:
        return None
    try:
        numero = float(v)
        if 30000 <= numero <= 80000:
            d = core.from_excel(numero)
            return d.date() if isinstance(d, datetime) else d
    except (TypeError, ValueError, OverflowError):
        pass
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
                    "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v, formato).date()
        except ValueError:
            pass
    return None


def inspecionar_coluna_a(path, aba, linha_cabecalho, limite=250):
    """Inspeciona A na aba de dados: mostra datas, sem confundir última operação com snapshot."""
    exemplos = []
    datas = set()
    cabecalho = ""
    lidas = 0
    datas_validas = 0
    with zipfile.ZipFile(path) as z:
        strings = core._shared_strings(z)
        with z.open(aba) as f:
            for _, row in core.etree.iterparse(f, events=("end",), tag=core.NS + "row"):
                numero_linha = int(row.get("r") or 0)
                if limite and numero_linha > linha_cabecalho + limite:
                    row.clear()
                    break
                celula_a = None
                for c in row.findall(core.NS + "c"):
                    if core._col(c.get("r", "")) == "A":
                        celula_a = c
                        break
                valor = core._valor_cell(celula_a, strings) if celula_a is not None else ""
                if numero_linha == linha_cabecalho:
                    cabecalho = str(valor).strip()
                elif numero_linha > linha_cabecalho:
                    lidas += 1
                    if valor and len(exemplos) < 5 and str(valor) not in exemplos:
                        exemplos.append(str(valor)[:70])
                    d = _data_em_celula(valor)
                    if d and 2020 <= d.year <= 2035:
                        datas.add(d.isoformat())
                        datas_validas += 1
                row.clear()
                while row.getprevious() is not None:
                    del row.getparent()[0]
    titulo = core.normalizar(cabecalho)
    # O layout J&T usa exatamente o cabeçalho Data para a data do snapshot.
    # Nunca usar A quando o cabeçalho é Número da Remessa ou última operação.
    confiavel = titulo in {
        "data", "data de referencia", "data do relatorio",
        "data da consulta", "data operacional", "data do snapshot"
    }
    data_segura = (next(iter(datas)) if confiavel and len(datas) == 1
                   and lidas >= 1 and datas_validas == lidas else "")
    return {
        "cabecalho": cabecalho,
        "amostras": " | ".join(exemplos),
        "datas": sorted(datas),
        "data_segura": data_segura,
        "lidas": lidas,
    }


def processar_planilha(path, nome_origem, data_sugerida, gestao, limite, diarios):
    resumo = {
        "arquivo": nome_origem, "data_confirmada_pelo_nome": data_sugerida,
        "data_operacional_auditada": data_sugerida, "origem_data": "nome" if data_sugerida else "indefinida",
        "coluna_a_cabecalho": "", "coluna_a_exemplos": "",
        "coluna_a_datas_encontradas": "", "coluna_a_data_confiavel": "",
        "aba": "", "mapa_colunas": "", "status": "REVISAR", "erro": "",
        "linhas_lidas": 0, "remessas_preenchidas": 0, "remessas_unicas_arquivo": 0,
        "repetidas_no_arquivo": 0, "repetidas_no_dia_entre_arquivos": 0,
        "pedidos_somados": 0, "sem_pedidos": 0, "aging_validos": 0,
        "aging_desconhecido": 0, "base_na_gestao": 0, "base_orlean": 0,
        "remessas_mais_3": 0, "remessas_mais_10": 0,
        "remessas_orlean_mais_3": 0, "remessas_orlean_mais_10": 0,
        "exemplos_aging": "", "exemplos_codigos_sem_gestao": "",
    }
    unicas_arquivo = set()
    tres = set()
    dez = set()
    orlean_tres = set()
    orlean_dez = set()
    aging_exemplos = []
    cod_exemplos = []
    try:
        aba, cab_linha, mapa = core.localizar_layout_sem_mov(path)
        info_a = inspecionar_coluna_a(path, aba, cab_linha, limite=min(limite, 300) if limite else 0)
        resumo["coluna_a_cabecalho"] = info_a["cabecalho"]
        resumo["coluna_a_exemplos"] = info_a["amostras"]
        resumo["coluna_a_datas_encontradas"] = " | ".join(info_a["datas"][:12])
        resumo["coluna_a_data_confiavel"] = info_a["data_segura"]
        if data_sugerida and info_a["data_segura"] and data_sugerida != info_a["data_segura"]:
            raise ValueError(
                f"Data do nome ({data_sugerida}) diverge da coluna A "
                f"({info_a['data_segura']})."
            )
        if not data_sugerida and info_a["data_segura"]:
            data_sugerida = info_a["data_segura"]
            resumo["origem_data"] = "coluna A (referencia unica)"
        resumo["data_operacional_auditada"] = data_sugerida
        resumo["aba"] = aba
        resumo["mapa_colunas"] = " / ".join(f"{k}:{v}" for k,v in sorted(mapa.items()))
        obrigatorios = {"remessa", "base_codigo", "aging_label", "pedidos"}
        if not obrigatorios.issubset(mapa):
            raise ValueError("Campos obrigatórios ausentes: " + ",".join(sorted(obrigatorios - set(mapa))))
        for registro in core.iter_sem_mov_layout(path):
            resumo["linhas_lidas"] += 1
            if limite and resumo["linhas_lidas"] > limite:
                break
            rem = core.texto(registro.get("remessa"))
            if not rem:
                continue
            resumo["remessas_preenchidas"] += 1
            h = chave_remessa(rem)
            ja_neste_arquivo = h in unicas_arquivo
            if ja_neste_arquivo:
                resumo["repetidas_no_arquivo"] += 1
            unicas_arquivo.add(h)

            if data_sugerida:
                ja = diarios[data_sugerida]["remessas"]
                if h in ja and not ja_neste_arquivo:
                    resumo["repetidas_no_dia_entre_arquivos"] += 1
                ja.add(h)

            ped = core.num_pedidos(registro.get("pedidos"))
            resumo["pedidos_somados"] += ped
            if ped <= 0:
                resumo["sem_pedidos"] += 1

            cod = core.norm_codigo(registro.get("base_codigo"))
            cadastro = gestao.get(cod, {})
            if cadastro:
                resumo["base_na_gestao"] += 1
            elif cod and cod not in cod_exemplos and len(cod_exemplos) < 8:
                cod_exemplos.append(cod)
            orlean = "ORLEAN" in str(cadastro.get("rm", "")).upper()
            if orlean:
                resumo["base_orlean"] += 1

            label = core.texto(registro.get("aging_label"))
            if label and label not in aging_exemplos and len(aging_exemplos) < 12:
                aging_exemplos.append(label[:65])
            valor, tipo = valor_aging(label)
            if valor is None:
                resumo["aging_desconhecido"] += 1
                continue
            resumo["aging_validos"] += 1
            if valor > 3:
                tres.add(h)
                if orlean:
                    orlean_tres.add(h)
                if data_sugerida:
                    diarios[data_sugerida]["mais_3"].add(h)
                    if orlean:
                        diarios[data_sugerida]["orlean_mais_3"].add(h)
            if valor > 10:
                dez.add(h)
                if orlean:
                    orlean_dez.add(h)
                if data_sugerida:
                    diarios[data_sugerida]["mais_10"].add(h)
                    if orlean:
                        diarios[data_sugerida]["orlean_mais_10"].add(h)

        resumo["remessas_unicas_arquivo"] = len(unicas_arquivo)
        resumo["remessas_mais_3"] = len(tres)
        resumo["remessas_mais_10"] = len(dez)
        resumo["remessas_orlean_mais_3"] = len(orlean_tres)
        resumo["remessas_orlean_mais_10"] = len(orlean_dez)
        resumo["exemplos_aging"] = " | ".join(aging_exemplos)
        resumo["exemplos_codigos_sem_gestao"] = " | ".join(cod_exemplos)
        if resumo["remessas_unicas_arquivo"] == 0:
            resumo["erro"] = "Nenhuma remessa encontrada no layout"
        elif resumo["pedidos_somados"] == 0:
            resumo["erro"] = "Coluna Pedidos zerada: revisar layout antes de importar"
        elif resumo["aging_validos"] == 0:
            resumo["erro"] = "Nenhum Aging reconhecido"
        elif not data_sugerida:
            resumo["erro"] = "Data operacional não comprovada no nome: validar referência"
        elif resumo["base_orlean"] == 0:
            resumo["erro"] = "Nenhuma remessa cruzou R/M Orlean: revisar base/Gestão"
        elif resumo["aging_desconhecido"]:
            resumo["erro"] = f"{resumo['aging_desconhecido']} linhas com Aging não reconhecido"
        elif resumo["repetidas_no_arquivo"] or resumo["repetidas_no_dia_entre_arquivos"]:
            resumo["erro"] = ("Remessas duplicadas no mesmo dia/arquivo: revisar antes de importar "
                             f"(internas={resumo['repetidas_no_arquivo']}, "
                             f"entre_arquivos={resumo['repetidas_no_dia_entre_arquivos']})")
        else:
            resumo["status"] = "AMOSTRA OK" if limite else "PRE-VALIDADO"
    except Exception as exc:
        resumo["erro"] = f"{type(exc).__name__}: {str(exc)[:250]}"
    return resumo


def salvar_csv(caminho, linhas):
    if not linhas:
        return
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=linhas[0].keys(), delimiter=";")
        w.writeheader()
        w.writerows(linhas)


def main():
    p = argparse.ArgumentParser(description="Auditoria local (sem envio) dos ZIPs de Sem Mov")
    p.add_argument("fontes", nargs="+", help="ZIPs, XLSXs ou pastas contendo Sem Mov")
    p.add_argument("--gestao", default=str(core.GESTAO_PADRAO))
    p.add_argument("--amostra", type=int, default=0, help="Ler N linhas por planilha; 0 = arquivo inteiro")
    args = p.parse_args()
    gestao = core.carregar_gestao(Path(args.gestao))
    print(f"Gestão carregada: {len(gestao)} códigos | Modo: SOMENTE AUDITORIA", flush=True)
    diarios = defaultdict(lambda: {
        "remessas": set(),
        "mais_3": set(), "mais_10": set(),
        "orlean_mais_3": set(), "orlean_mais_10": set(),
    })
    resumos = []
    with tempfile.TemporaryDirectory(prefix="jt_auditoria_") as tmp:
        for fonte in encontrar_origem(args.fontes):
            if fonte.suffix.lower() == ".xlsx":
                print("ANALISANDO", fonte.name, flush=True)
                dia = data_do_nome(fonte.name)
                r = processar_planilha(fonte, str(fonte), dia, gestao, args.amostra, diarios)
                resumos.append(r)
                print("  ",r["status"],"dia:",r["data_operacional_auditada"] or "NÃO COMPROVADO",
                      "| Coluna A:",r["coluna_a_cabecalho"] or "SEM CABEÇALHO",
                      "| Datas em A:",r["coluna_a_datas_encontradas"][:90] or "NENHUMA",
                      "| Amostras A:",r["coluna_a_exemplos"][:110] or "VAZIA",
                      "remessas:",r["remessas_unicas_arquivo"],
                      "pedidos:",r["pedidos_somados"],"Orlean:",r["base_orlean"],r["erro"],flush=True)
            elif fonte.suffix.lower() == ".zip":
                print("ABRINDO ZIP", fonte.name, flush=True)
                with zipfile.ZipFile(fonte) as z:
                    for i, entrada in enumerate(z.infolist(), 1):
                        if entrada.is_dir() or not entrada.filename.lower().endswith(".xlsx"):
                            continue
                        if entrada.filename.split("/")[-1].startswith("~$"):
                            continue
                        if entrada.file_size > MAX_COMPACTADO:
                            print("  IGNORADO, entrada muito grande:",entrada.filename,flush=True)
                            continue
                        nome = Path(entrada.filename).name
                        nome_temporario = f"entrada_{len(resumos):05d}_{nome}"
                        destino = Path(tmp) / nome_temporario
                        with z.open(entrada) as src, open(destino,"wb") as dst:
                            shutil.copyfileobj(src,dst)
                        dia = data_do_nome(nome) or data_do_nome(entrada.filename)
                        print("  ANALISANDO", entrada.filename,"dia",dia or "INDEFINIDO",flush=True)
                        resumo = processar_planilha(destino, f"{fonte.name} :: {entrada.filename}",
                                                     dia, gestao,args.amostra, diarios)
                        resumos.append(resumo)
                        print("    ",resumo["status"],"dia:",resumo["data_operacional_auditada"] or "NÃO COMPROVADO",
                              "| Coluna A:",resumo["coluna_a_cabecalho"] or "SEM CABEÇALHO",
                              "| Datas em A:",resumo["coluna_a_datas_encontradas"][:90] or "NENHUMA",
                              "remessas:",resumo["remessas_unicas_arquivo"],
                              "pedidos:",resumo["pedidos_somados"],"Orlean:",resumo["base_orlean"],
                              resumo["erro"],flush=True)
                        destino.unlink(missing_ok=True)
    # Este bloco termina fora do contexto do ZIP/TemporaryDirectory.
    # Indentação normalizada em 4 espaços, sem tabulações.
    cobertura = []
    for dia in DIAS_ESPERADOS:
        chave = dia.isoformat()
        d = diarios.get(chave)
        arquivos = [item for item in resumos if item["data_operacional_auditada"] == chave]
        if not arquivos:
            status = 'SEM ARQUIVO'
        elif any(item['status'] not in ('PRE-VALIDADO', 'AMOSTRA OK') for item in arquivos):
            status = 'REVISAR'
        elif args.amostra:
            status = 'AMOSTRA'
        else:
            status = 'PRE-VALIDADO'
        cobertura.append({
            'data': chave,
            'semana': ('W43' if dia <= date(2026, 10, 25) else
                       'W44' if dia <= date(2026, 11, 1) else 'W45'),
            'arquivos': len(arquivos),
            'status': status,
            'remessas_unicas_dia': len(d['remessas']) if d else 0,
            'acima_3_dia': len(d['mais_3']) if d else 0,
            'acima_10_dia': len(d['mais_10']) if d else 0,
            'orlean_acima_3_dia': len(d['orlean_mais_3']) if d else 0,
            'orlean_acima_10_dia': len(d['orlean_mais_10']) if d else 0,
        })

    salvar_csv(Path('auditoria_sem_mov.csv'), resumos)
    salvar_csv(Path('cobertura_sem_mov.csv'), cobertura)
    print('\n=== COBERTURA (ARQUIVOS RECEBIDOS) ===')
    for item in cobertura:
        print(item['semana'], item['data'], item['status'], 'arquivos:', item['arquivos'],
              'Orlean >3:', item['orlean_acima_3_dia'],
              'Orlean >10:', item['orlean_acima_10_dia'])
    sem_data = [item['arquivo'] for item in resumos if not item["data_operacional_auditada"]]
    if sem_data:
        print('\nATENÇÃO: estes arquivos precisam ter a data operacional confirmada antes de importar:')
        for nome in sem_data:
            print(' -', nome)
    print('\nRelatórios gerados: auditoria_sem_mov.csv e cobertura_sem_mov.csv')
    print('Nenhum dado foi enviado ao banco.')

if __name__ == "__main__":
    main()
