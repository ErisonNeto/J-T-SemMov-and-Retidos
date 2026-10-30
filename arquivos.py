"""Fontes CSV; nenhuma conexão com banco de dados."""
import csv
import os
from pathlib import Path
from functools import lru_cache
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
RESUMOS_DIR = BASE_DIR / 'resumos'
DETALHES_DIR = BASE_DIR / 'detalhes_locais'
DIMENSOES = ['indicador', 'data', 'base_codigo', 'base_nome', 'uf', 'rm', 'sc',
             'regional', 'aging_label', 'aging_valor', 'acima_3', 'acima_10', 'motivo']
COLUNAS = DIMENSOES + ['pedidos', 'linhas']

def atomic_csv(path, rows, fields):
    temp = path.with_suffix('.tmp')
    try:
        with temp.open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=';')
            w.writeheader()
            w.writerows(rows)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def validar_csv(source):
    df = pd.read_csv(source, sep=';', dtype=str, keep_default_na=False)
    missing = set(COLUNAS) - set(df.columns)
    if missing:
        raise ValueError(f'CSV inválido: faltam {sorted(missing)}')
    df = df[COLUNAS].copy()
    for c in ['aging_valor', 'acima_3', 'acima_10', 'pedidos', 'linhas']:
        values = pd.to_numeric(df[c], errors='raise')
        if ((values % 1 != 0) | (values < 0)).any():
            raise ValueError('CSV contém contagem negativa ou fracionária.')
        df[c] = values.astype('int64')
    if not df.acima_3.isin([0,1]).all() or not df.acima_10.isin([0,1]).all():
        raise ValueError('Flags de corte devem ser 0 ou 1.')
    return df

@lru_cache(maxsize=1)
def _carregar(assinatura):
    frames = [validar_csv(name) for name, _, _ in assinatura]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUNAS)

def status_fonte():
    if os.getenv('DATA_SOURCE', 'local').lower() == 'drive':
        from drive_csv import status
        return status()
    return {'fonte':'CSV local','aviso':None,'sincronizado_em':None}

def carregar():
    if os.getenv('DATA_SOURCE', 'local').lower() == 'drive':
        from drive_csv import refresh
        return refresh()
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                      for p in sorted(RESUMOS_DIR.glob('*.csv')))
    return _carregar(signature).copy()
