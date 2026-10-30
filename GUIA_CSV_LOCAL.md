# Sem Movimentação e Retidos — sem banco, com CSV

Esta versão preserva o layout do pacote Padrão SLA, os filtros UF/RM/SC/base, as metas independentes, o ranking, motivos, aging e a comparação D-1 × D-8. Não conecta a nenhum banco. Os valores são SOMAS DE PEDIDOS, não contagens de remessas. A coluna `linhas` do resumo permite conferir a quantidade de registros de origem.

## Primeiro teste no Windows

1. Extraia em uma NOVA pasta, sem sobrescrever sua instalação antiga.
2. Copie seus Excel para `dados/sem_mov/` e `dados/retidos/`. Nome esperado: `Outubro 29 2026.xlsx`. Use apenas um arquivo por indicador e data. Os dois indicadores podem ter a mesma data.
3. Confira/atualize `dados/Gestão de colaboradores.xlsx`, incluída do pacote original.
4. Abra o PowerShell nessa nova pasta e execute:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe importar_excel.py
.\.venv\Scripts\python.exe main.py
```

Abra http://localhost:8004. Caso seu Python já tenha as dependências instaladas, basta `python importar_excel.py` e `python main.py`.

## Onde ficam os dados

- `resumos/`: um CSV por indicador/data, agrupado por base, responsáveis, aging e motivo. Não contém remessas; alimenta o site. Preserve os dias anteriores para o histórico e comparação semanal.
- `detalhes_locais/`: um CSV GZIP por indicador/data, com as remessas completas. Pode ser descompactado com 7-Zip. Fica SOMENTE no PC; faça uma cópia de segurança na pasta Qualidade.
- `dados/`: Excel originais, que também devem ser preservados.

Executar de novo o mesmo dia substitui o arquivo desse dia, sem somar duas vezes. Outros dias permanecem. Nenhuma linha reconhecida gera erro e preserva a carga anterior. Arquivos com mesma data no mesmo indicador geram erro. Os cortes originais foram preservados: Sem Mov `Exceed 3/10 days` inclui o limite 3/10; Retidos usa estritamente mais de 3/10 dias.

## Publicar no Render via repositório PRIVADO

1. Crie um repositório PRIVADO separado para esta versão de teste.
2. Envie código, `templates/`, `static/`, `resumos/`, `requirements.txt`, `render.yaml`, `config.py`, `arquivos.py`, `main.py` e `.gitignore`.
3. NÃO envie `.env`, `.venv`, `dados/` nem `detalhes_locais/`. O `.gitignore` já exclui esses caminhos para arquivos novos, mas não remove arquivos que já estavam rastreados em um repositório antigo.
4. Crie um Web Service no Render conectado ao repositório privado. Build: `pip install -r requirements.txt`. Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
5. Se necessário, defina as quatro metas do `.env.example` nas variáveis do serviço. Sem configuração, o painel indica meta a configurar.
6. A cada carga, execute `python importar_excel.py`, envie os CSVs alterados de `resumos/` e aguarde o novo deploy. O histórico precisa continuar no repositório.

A importação roda no PC; o Render só lê os resumos enviados. Não execute o importador no build. Depois de publicado, o PC pode desligar. Sem nova carga, o painel avisa quando D-1 não existe e mostra a última data disponível; não apresenta um dia antigo como se fosse ontem.

Exemplo de comandos, APENAS dentro do repositório privado já configurado:

```powershell
git add resumos
git commit -m "Atualizar resumos dos indicadores"
git push
```

O repositório privado não torna a URL do dashboard privada. Preserve o controle de acesso que sua operação exigir; o pacote original não tem autenticação. Resumos ainda contêm informação operacional e nomes de responsáveis. Motivos livres também devem ser conferidos antes da publicação.

## Diferenças nesta versão

- O botão baixa o RESUMO filtrado D-1 acima de 3 dias, sem remessas. Os detalhes completos estão nos GZIP locais. A API também aceita `corte=10` ou `corte=todos`.
- Planos de ação são salvos no navegador, não no servidor. Não sincronizam entre pessoas ou computadores e podem ser perdidos ao limpar os dados do navegador. Use Exportar planos para uma cópia JSON e Importar planos para restaurar. Importar substitui os planos com a mesma combinação indicador/base.
- Não houve migração automática dos registros do banco antigo. Reimporte todos os Excel que quiser manter no histórico. Confira totais e filtros antes de desativar qualquer banco ou painel antigo.
- Arquivos muito grandes em `resumos/` podem exigir outra hospedagem de arquivos no futuro. Esta versão evita publicar os milhões de remessas, mas o tamanho ainda depende da variedade de bases, aging e motivos.

## Validação antes de trocar o painel

Compare uma mesma data/UF/base nos dois painéis: pedidos >3 dias, >10 dias, ranking e variação semanal. Confira a soma de `linhas` e `pedidos` nos resumos contra o relatório do importador. Não some dias para interpretar remessas únicas: uma mesma remessa pode aparecer em vários retratos diários.
