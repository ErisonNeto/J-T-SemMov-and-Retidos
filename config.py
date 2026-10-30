import os
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

TIMEZONE = ZoneInfo(os.getenv("JT_TIMEZONE", "America/Belem"))
JANELA_EVOLUCAO_DIAS = int(os.getenv("JANELA_EVOLUCAO_DIAS", "14"))


def _meta(nome: str):
    valor = os.getenv(nome, "").strip().replace(",", ".")
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        return None


# Quanto MENOR, melhor. Deixe None/variável vazia até confirmar a meta oficial.
METAS = {
    "sem_mov": {
        "acima_3": _meta("META_SEM_MOV_3"),
        "acima_10": _meta("META_SEM_MOV_10"),
    },
    "retidos": {
        "acima_3": _meta("META_RETIDOS_3"),
        "acima_10": _meta("META_RETIDOS_10"),
    },
}
