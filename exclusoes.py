"""Exclusoes de S/C do indicador Sem Movimentacao & Retidos.

Politica visual: mantem as remessas e a dimensao bruta no PostgreSQL
para auditoria, mas nao contabiliza nem oferece em filtros as bases dos
responsaveis fora do escopo da regional Orlean.
"""
import re
import unicodedata

SC_EXCLUIDOS = frozenset({"DARLEY WALLACE CUNHA QUARESMA", "DARLEY"})


def normalizar_nome(valor):
    s = unicodedata.normalize("NFKD", str(valor or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def sc_excluido(valor):
    nome = normalizar_nome(valor)
    return nome in SC_EXCLUIDOS or "DARLEY WALLACE CUNHA QUARESMA" in nome
