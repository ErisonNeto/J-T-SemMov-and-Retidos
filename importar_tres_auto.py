# -*- coding: utf-8 -*-
"""Importacao diaria de 3 fontes J&T: Sem Mov, Retidos e Volume Outlet.

Uso:
  python importar_tres_auto.py --dry-run
  python importar_tres_auto.py
Pastas recomendadas:
  dados/sem_mov/      -> Excel de Sem Movimentacao
  dados/retidos/       -> Excel de Retidos
  dados/               -> Excel de Volume Outlet e Gestao de colaboradores.xlsx
Opcionalmente os tres tipos tambem podem ficar em dados/entrada/.
O sistema identifica os arquivos pelo nome, cruza a data operacional
dos tres tipos, importa Sem Mov e Retidos pela JT API, gera relatorio
percentual e registra hashes para nao reenviar cargas concluidas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

import importar_excel as core
import calcular_percentuais_outlet as outlet

ROOT = Path(__file__).resolve().parent
DADOS = ROOT / "dados"
ENTRADA = DADOS / "entrada"
SEM_DIR = DADOS / "sem_mov"
RET_DIR = DADOS / "retidos"
RELATORIOS = DADOS / "relatorios_outlet"
CONTROLE = DADOS / "controle_importacao_tres_fontes.json"
TIPOS = ("sem_mov", "retidos", "outlet")


def tipo_arquivo(path: Path) -> str | None:
    nome = core.normalizar(path.stem)
    if "analise do volume de negocios do outlet" in nome:
        return "outlet"
    if nome.startswith("monitoramento de movimentacao em tempo real") or "sem movimentacao" in nome or "sem_mov" in nome:
        return "sem_mov"
    if nome.startswith("estatistica de itens retidos na base") or "retido" in nome:
        return "retidos"
    return None


def localizar_operacionais(entrada: Path) -> list[Path]:
    """Busca as tres fontes na estrutura existente, sem incluir arquivos de apoio.

    Nao faz rglob em dados/ inteiro: evita reimportar caches de ZIP,
    planilhas de auditoria e relatorios gerados pelo proprio script.
    O --entrada continua funcionando para uma pasta alternativa.
    """
    candidatos: list[Path] = []

    if entrada.resolve() == DADOS.resolve():
        # Outlet do dia fica diretamente em dados/.
        candidatos.extend(
            p for p in DADOS.glob("*.xlsx")
            if p.is_file() and tipo_arquivo(p) == "outlet"
        )
        # Sem Mov e Retidos ficam nas pastas que o usuario ja utiliza.
        for pasta, esperado in ((SEM_DIR, "sem_mov"), (RET_DIR, "retidos")):
            if pasta.is_dir():
                candidatos.extend(
                    p for p in pasta.glob("*.xlsx")
                    if p.is_file() and tipo_arquivo(p) == esperado
                )
        # Mantem compatibilidade com a entrada unificada anterior.
        if ENTRADA.is_dir():
            candidatos.extend(
                p for p in ENTRADA.rglob("*.xlsx") if p.is_file()
            )
    else:
        if not entrada.is_dir():
            raise RuntimeError(f"Pasta de entrada nao existe: {entrada}")
        candidatos.extend(
            p for p in entrada.rglob("*.xlsx") if p.is_file()
        )

    return sorted(
        {p.resolve() for p in candidatos if not p.name.startswith("~$")},
        key=lambda p: (str(p.parent).lower(), p.name.lower()),
    )


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ler_controle() -> dict:
    if CONTROLE.is_file():
        data = json.loads(CONTROLE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cargas", {}), dict):
            raise RuntimeError("Controle de cargas invalido. Corrija antes de importar.")
        return data
    return {"cargas": {}}


def gravar_controle(data: dict) -> None:
    CONTROLE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONTROLE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(CONTROLE)


def db_contagem(session: requests.Session, tipo: str, data: str) -> int:
    resp = session.post(
        core.API_URL + "/query", headers=core._headers(),
        json={
            "sql": "SELECT COUNT(*)::bigint FROM public.sem_mov_retidos_detalhe "
                   "WHERE indicador = %s AND data = %s::date",
            "params": [tipo, data],
        },
        timeout=max(core.API_TIMEOUT, 90),
    )
    if not resp.ok:
        raise RuntimeError(f"Verificacao PostgreSQL HTTP {resp.status_code}: {resp.text[:250]}")
    rows = resp.json().get("rows", [])
    if not rows:
        raise RuntimeError(f"Sem resposta do banco para {tipo} de {data}")
    first = rows[0]
    return int(first[0] if isinstance(first, (list, tuple)) else next(iter(first.values())))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importa automaticamente os tres Excel diarios de Sem Mov, Retidos e Outlet."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Valida arquivos sem gravar no banco nem gerar relatorios.")
    parser.add_argument("--substituir-dia", action="store_true",
                        help="Permite reimportar um arquivo diferente para data ja concluida.")
    parser.add_argument("--lote", type=int, default=100)
    parser.add_argument(
        "--entrada", default=str(DADOS),
        help="Pasta alternativa de entrada (padrao: dados, sem_mov e retidos separados).",
    )
    args = parser.parse_args()
    if not 50 <= args.lote <= 3000:
        parser.error("--lote precisa ficar entre 50 e 3000")
    core.BATCH_SIZE = args.lote

    entrada = Path(args.entrada).expanduser().resolve()
    if entrada == DADOS.resolve():
        DADOS.mkdir(parents=True, exist_ok=True)
    candidatos = localizar_operacionais(entrada)
    tipos = [(p, tipo_arquivo(p)) for p in candidatos]
    desconhecidos = [p.name for p, t in tipos if t is None]
    if desconhecidos:
        print("ARQUIVOS NAO RECONHECIDOS (ignorados): " + ", ".join(desconhecidos))
    arquivos = [(p, t) for p, t in tipos if t is not None]
    if not arquivos:
        print(f"Nenhum Excel operacional em {entrada}")
        return

    gestao_local = core.GESTAO_PADRAO
    if not gestao_local.is_file():
        raise RuntimeError(f"Gestao local nao encontrada: {gestao_local}")

    print(f"ENTRADA: {entrada} | {len(arquivos)} arquivos operacionais", flush=True)
    print(f"GESTAO: {gestao_local.name}", flush=True)
    controle = ler_controle()
    cargas = controle.setdefault("cargas", {})

    # A gestao publicada tem precedencia para propriedade Orlean e S/C.
    with requests.Session() as session:
        core.testar_api(session)
        gestores = outlet.gestao_publicada(session)
        gestao_import = core.carregar_gestao(gestao_local)
        core.aplicar_sc_confirmado_no_postgres(session, gestao_import)
        # Para as bases de Orlean, a gestao publicada no PostgreSQL e a
        # fonte oficial de atribuicao de R/M e S/C; evita sobrescrever
        # a correcao historica do Johan em futuras importacoes.
        alinhadas = 0
        for codigo_base, fonte in gestores.items():
            g = gestao_import.get(codigo_base)
            if g is None:
                continue
            if g.get("sc") != fonte["sc"] or g.get("rm") != fonte["rm"]:
                alinhadas += 1
            g["sc"] = fonte["sc"]
            g["rm"] = fonte["rm"]
            g["uf"] = fonte["uf"]
        print(
            f"Gestao Orlean: {len(gestores)} bases publicadas; "
            f"{alinhadas} atribuicoes locais alinhadas ao banco.",
            flush=True,
        )

        agrupados: dict[str, dict[str, tuple[Path, str, dict | None]]] = defaultdict(dict)
        for path, tipo in arquivos:
            hash_arquivo = digest(path)
            if tipo in ("sem_mov", "retidos"):
                data_ref = core.data_referencia(path, tipo)
                dias = [data_ref.isoformat()]
                volume = None
            else:
                # Colunas A/I/J/M; somente codigos da Gestao publicada Orlean.
                volume = outlet.carregar_volume(path, gestores, aba=None)
                dias = sorted({data for data, _ in volume})
                if len(dias) != 1:
                    raise RuntimeError(
                        f"{path.name}: Outlet contem {len(dias)} datas operacionais: "
                        + ", ".join(dias)
                        + ". Exporte um XLSX por dia para cruzar com Sem Mov/Retidos."
                    )

            for dia in dias:
                if tipo in agrupados[dia]:
                    outro = agrupados[dia][tipo]
                    if outro[1] != hash_arquivo:
                        raise RuntimeError(
                            f"Mais de um arquivo diferente do tipo {tipo} para {dia}: "
                            f"{outro[0].name} e {path.name}. Deixe so o correto na entrada."
                        )
                    print(f"DUPLICATA IDENTICA ignorada: {path.name}")
                    continue
                agrupados[dia][tipo] = (path, hash_arquivo, volume)

        if not agrupados:
            print("Nenhum dia operacional detectado.")
            return

        for dia, trio in sorted(agrupados.items()):
            faltam = [t for t in TIPOS if t not in trio]
            print(f"\nDIA {dia}: {', '.join(t for t in TIPOS if t in trio)}")
            if faltam:
                print("INCOMPLETO: faltam " + ", ".join(faltam) + ". Nao importa este dia.")
                continue

            # Preflight antes de QUALQUER gravacao.
            for tipo in ("retidos", "sem_mov"):
                path, hash_arquivo, _ = trio[tipo]
                k = f"{dia}|{tipo}"
                antigo = cargas.get(k)
                if antigo and antigo.get("sha256") == hash_arquivo:
                    print(f"  {tipo}: ja concluido; nao sera reenviado.")
                elif antigo and not args.substituir_dia:
                    raise RuntimeError(
                        f"{tipo} {dia} ja consta como concluido com OUTRO arquivo. "
                        "Revise os arquivos e use --substituir-dia somente se for corrigir o dia."
                    )
                else:
                    print(f"  Validando {tipo}: {path.name}", flush=True)
                    linhas, pedidos, _, sem_gestao = core.processar_arquivo(
                        path, tipo, __import__("datetime").date.fromisoformat(dia),
                        gestao_import, None, True
                    )
                    print(
                        f"  OK {tipo}: {linhas:,} linhas | {pedidos:,} pedidos "
                        f"| {sem_gestao:,} sem Gestao"
                    )

            if args.dry_run:
                print("  DRY-RUN OK. Nenhum dado gravado.")
                continue

            # Importacao por tipo e checkpoint. Em falha, upsert pode ser
            # executado novamente: jamais marcar um arquivo incompleto como OK.
            for tipo in ("retidos", "sem_mov"):
                path, hash_arquivo, _ = trio[tipo]
                k = f"{dia}|{tipo}"
                if cargas.get(k, {}).get("sha256") == hash_arquivo:
                    continue
                linhas, pedidos, enviados, _ = core.processar_arquivo(
                    path, tipo, __import__("datetime").date.fromisoformat(dia),
                    gestao_import, session, False
                )
                no_banco = db_contagem(session, tipo, dia)
                if no_banco <= 0:
                    raise RuntimeError(
                        f"{tipo} {dia}: nao encontrei registros no PostgreSQL apos o envio."
                    )
                cargas[k] = {
                    "sha256": hash_arquivo, "arquivo": path.name,
                    "registros_processados": linhas, "pedidos": pedidos,
                    "upserts_enviados": enviados, "registros_banco": no_banco,
                    "concluido_em": datetime.now().isoformat(timespec="seconds"),
                }
                gravar_controle(controle)
                print(
                    f"  IMPORTADO {tipo}: {enviados:,} upserts; "
                    f"PostgreSQL {no_banco:,} linhas no dia {dia}.", flush=True
                )

            _, _, volume = trio["outlet"]
            assert volume is not None
            numeradores, cobertura = outlet.carregar_indicadores(session, volume)
            for tipo in ("sem_mov", "retidos"):
                if (dia, tipo) not in cobertura:
                    raise RuntimeError(
                        f"Faltam dados do indicador {tipo} de {dia} "
                        "para as bases Orlean encontradas no volume."
                    )
            visoes = {
                nome: outlet.agregar(volume, gestores, numeradores, cobertura, nome)
                for nome in ("Orlean", "S-C", "Bases")
            }
            RELATORIOS.mkdir(parents=True, exist_ok=True)
            saida = RELATORIOS / f"percentuais_outlet_{dia}.xlsx"
            outlet.salvar_excel(saida, visoes)
            cargas[f"{dia}|outlet"] = {
                "sha256": trio["outlet"][1], "arquivo": trio["outlet"][0].name,
                "relatorio": str(saida),
                "concluido_em": datetime.now().isoformat(timespec="seconds"),
            }
            gravar_controle(controle)
            for row in visoes["Orlean"]:
                _, _, _, _, j, m, total, sem, pct_sem, ret, pct_ret = row
                def pct(x): return "N/D" if x is None else f"{100*x:.2f}%"
                print(
                    f"  ORLEAN {dia}: J={j:,} + M={m:,} = {total:,} | "
                    f"Sem Mov >2: {sem:,} ({pct(pct_sem)}) | "
                    f"Retidos >3: {ret:,} ({pct(pct_ret)})"
                )
            print(f"  RELATORIO: {saida}")
        print("\nFINALIZADO. Estado salvo em:", CONTROLE)
        print(
            "NOTA: Retidos >3 de aging nao comprova vencimento 3 dias apos prazo SLA. "
            "Nao publicar como SLA vencido sem data de prazo."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nINTERROMPIDO: confira o controle antes de retomar.")
        sys.exit(130)
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr)
        sys.exit(1)
