from config import TIMEZONE
import os
os.environ['DATA_SOURCE']='drive'
from arquivos import carregar, status_fonte
if __name__ == '__main__':
    try:
        df=carregar()
        print('Conexão OK:',status_fonte()['arquivos'],'arquivos')
        print(df.groupby(['indicador','data'])[['pedidos','linhas']].sum().to_string())
    except Exception as e:
        print('Não foi possível ler o Drive:',str(e))
        raise SystemExit(1)
