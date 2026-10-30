"""Leitura privada do Drive, com cache limitado e atualização transacional em memória."""
import io
import os
import re
import threading
import time
from datetime import datetime, timedelta, date
import pandas as pd
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
from config import TIMEZONE, JANELA_EVOLUCAO_DIAS

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
PATTERN = re.compile(r'^(sem_mov|retidos)_(\d{4}-\d{2}-\d{2})\.csv$')
API = 'https://www.googleapis.com/drive/v3/files'
_lock = threading.Lock()
_snapshot = None
_file_cache = {}
_next_check = 0
_status = {'fonte':'Google Drive', 'sincronizado_em':None, 'aviso':None, 'arquivos':0}

def status():
    return dict(_status)

def session():
    path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
    if not path:
        raise ValueError('Configure GOOGLE_APPLICATION_CREDENTIALS com o caminho do JSON secreto.')
    cred = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return AuthorizedSession(cred, refresh_timeout=20)

def list_files(s, folder):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', folder):
        raise ValueError('GOOGLE_DRIVE_FOLDER_ID inválido: use apenas o ID da pasta.')
    # Confirma acesso à pasta. Uma lista vazia isolada não prova que ela existe.
    r=s.get(f'{API}/{folder}',params={'fields':'id,mimeType'},timeout=30)
    r.raise_for_status()
    if r.json().get('mimeType') != 'application/vnd.google-apps.folder':
        raise ValueError('O ID configurado não corresponde a uma pasta.')
    out=[];token=None
    while True:
        params={'q':f"'{folder}' in parents and trashed = false", 'pageSize':1000,
                'fields':'nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum)'}
        if token:params['pageToken']=token
        r=s.get(API,params=params,timeout=30);r.raise_for_status();body=r.json()
        out.extend(body.get('files',[]));token=body.get('nextPageToken')
        if not token:return out

def download(s, f, limit):
    if int(f.get('size',0))>limit:raise ValueError('CSV excede limite de memória configurado.')
    with s.get(f"{API}/{f['id']}",params={'alt':'media'},stream=True,timeout=30) as r:
        r.raise_for_status();buf=io.BytesIO()
        for chunk in r.iter_content(65536):
            if buf.tell()+len(chunk)>limit:raise ValueError('CSV excede limite configurado.')
            buf.write(chunk)
        return buf.getvalue()

def refresh():
    global _snapshot,_file_cache,_next_check
    from arquivos import COLUNAS, validar_csv
    folder=os.getenv('GOOGLE_DRIVE_FOLDER_ID','').strip()
    if not folder:raise ValueError('Configure GOOGLE_DRIVE_FOLDER_ID.')
    now=time.monotonic()
    with _lock:
        if now<_next_check and _snapshot is not None:return _snapshot.copy()
        if now<_next_check:raise RuntimeError('Falha ao ler o Drive; confira credenciais, API e compartilhamento.')
        try:
            today=datetime.now(TIMEZONE).date()
            days=max(8,JANELA_EVOLUCAO_DIAS,int(os.getenv('DRIVE_HISTORY_DAYS','30')))
            minimum=today-timedelta(days=days)
            maximum=today-timedelta(days=1)
            ttl=max(30,int(os.getenv('DRIVE_CACHE_SECONDS','300')))
            per_file=int(os.getenv('DRIVE_MAX_FILE_MB','10'))*1024*1024
            total_limit=int(os.getenv('DRIVE_MAX_TOTAL_MB','40'))*1024*1024
            with session() as s:
                files=list_files(s,folder);chosen={}
                for f in files:
                    m=PATTERN.fullmatch(f.get('name',''))
                    if not m:continue
                    dt=date.fromisoformat(m.group(2))
                    if not minimum<=dt<=maximum:continue
                    if f['mimeType'].startswith('application/vnd.google-apps.'):
                        raise ValueError('Mantenha os resumos como CSV, sem converter para Google Sheets.')
                    if f['name'] in chosen:raise ValueError('Há CSVs duplicados com o mesmo nome na pasta. Remova a cópia duplicada.')
                    chosen[f['name']]=f
                if not chosen:raise ValueError('Nenhum CSV reconhecido no período. Confira a pasta, as datas e os nomes dos arquivos.')
                if sum(int(f.get('size',0)) for f in chosen.values())>total_limit:
                    raise ValueError('Resumos excedem o limite total; reduza DRIVE_HISTORY_DAYS.')
                new={};frames=[];total=0
                for name,f in sorted(chosen.items()):
                    key=(f['id'],f.get('modifiedTime'),f.get('md5Checksum'))
                    if key in _file_cache:frame,nbytes=_file_cache[key]
                    else:
                        raw=download(s,f,min(per_file,total_limit-total))
                        nbytes=len(raw);frame=validar_csv(io.BytesIO(raw))
                        indicator,dt=PATTERN.fullmatch(name).groups()
                        if not ((frame.indicador==indicator)&(frame.data==dt)).all():
                            raise ValueError('Data/indicador dentro do CSV não corresponde ao nome do arquivo.')
                    total+=nbytes
                    if total>total_limit:raise ValueError('Resumos excedem limite total.')
                    new[key]=(frame,nbytes);frames.append(frame)
                result=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=COLUNAS)
            # Só troca o conjunto após baixar e validar todos os arquivos.
            _snapshot=result;_file_cache=new;_next_check=time.monotonic()+ttl
            _status.update(sincronizado_em=datetime.now(TIMEZONE).isoformat(),aviso=None,arquivos=len(chosen))
        except Exception as exc:
            _next_check=time.monotonic()+60
            # Não retorna detalhes HTTP, tokens ou conteúdo do JSON para o navegador/log.
            hint=str(exc) if isinstance(exc,ValueError) else 'Confira API ativada, JSON secreto, conexão e acesso de Leitor à pasta.'
            _status['aviso']='Falha ao atualizar o Drive. '+hint
            if _snapshot is None:raise RuntimeError(_status['aviso']) from None
            _status['aviso']+=' Exibindo a última sincronização bem-sucedida.'
        return _snapshot.copy()
