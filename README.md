# Sem Movimentação e Retidos — Google Drive + Render

O painel lê os resumos CSV da pasta privada do Google Drive. Nenhum banco é necessário. O código vai no GitHub; CSVs e chaves não precisam ir ao repositório. O importador de Excel permanece igual.

## Configuração no Render

1. Extraia este pacote e envie os arquivos de código para um repositório PRIVADO no GitHub: `app.py`, `arquivos.py`, `drive_csv.py`, `config.py`, `main.py`, `testar_drive.py`, `importar_excel.py`, `requirements.txt`, `render.yaml`, `.gitignore`, `templates/` e `static/`.
2. Não envie JSON de credenciais, `.env`, Excel, `detalhes_locais/` ou `.venv/`. A pasta `resumos/` é opcional no modo Drive e não precisa ser enviada.
3. No Web Service do Render: Build `pip install -r requirements.txt`; Start `uvicorn app:app --host 0.0.0.0 --port $PORT`. Root Directory vazio quando `app.py` estiver na raiz.
4. Em Environment → Secret Files, adicione um arquivo chamado `google-drive.json`. No conteúdo, cole o JSON inteiro baixado do Google Cloud. Ele fica fora do repositório.
5. Defina estas variáveis de ambiente:

| Nome | Valor |
|---|---|
| DATA_SOURCE | drive |
| GOOGLE_DRIVE_FOLDER_ID | COLE_O_ID_DA_PASTA |
| GOOGLE_APPLICATION_CREDENTIALS | /etc/secrets/google-drive.json |
| DRIVE_CACHE_SECONDS | 300 |
| DRIVE_HISTORY_DAYS | 30 |
| JT_TIMEZONE | America/Belem |

6. Salve e faça o deploy. Mantenha a pasta compartilhada como Leitor com `EMAIL_DA_SUA_CONTA_DE_SERVICO`. Não é necessário torná-la pública.

## Atualizar todos os dias

- Execute `python importar_excel.py` no computador com os Excel nas pastas de origem.
- Envie os CSVs gerados em `resumos/` para a mesma pasta `SemMov_Retidos` no Drive.
- Para atualizar um dia já enviado, substitua a versão existente (Gerenciar versões → Fazer upload de nova versão). Não deixe dois arquivos com o mesmo nome. O painel recusa duplicatas no período para evitar dobrar resultados.
- Mantenha CSV como arquivo, sem converter para Google Sheets.
- Aguarde até 5 minutos e atualize a página do painel. Não há novo deploy para atualização de dados.

## Como o cache funciona

A primeira consulta baixa os CSVs necessários. As consultas seguintes usam uma cópia em memória. Após cinco minutos, a próxima consulta verifica o Drive e baixa apenas os arquivos novos/alterados. Sem acessos ao site, não há verificação em segundo plano.

O período carregado é de 30 dias até ontem, configurável por DRIVE_HISTORY_DAYS. A evolução continua usando a janela de config.py (14 dias por padrão). Arquivos antigos continuam no Drive, mas não são carregados. Os filtros são derivados dos arquivos carregados. O painel mostra fonte, horário da última sincronização e eventual falha. Em falha, mantém o último conjunto válido somente enquanto o processo continuar vivo; depois de reiniciar precisa acessar o Drive novamente. O Drive é a fonte persistente.

Limites de proteção: 10 MiB por CSV e 40 MiB no total baixado por sincronização. A memória real usada pelos DataFrames é maior que o tamanho dos arquivos. Esses limites podem ser ajustados por DRIVE_MAX_FILE_MB e DRIVE_MAX_TOTAL_MB após avaliar a RAM disponível.

## Datas e resultados

A comparação permanece D-1 × D-8. Arquivos somente de 19 a 25 de outubro não preenchem os cartões de D-1 em 30/10: é necessário enviar o dia 29/10. O painel informa a ausência sem trocar silenciosamente a data. Compara somas de pedidos; a coluna linhas conta registros de origem, não remessas únicas ao longo dos dias.

## Teste local do Drive (opcional)

Instale dependências com `python -m pip install -r requirements.txt`. Copie `.env.example` para `.env` e ajuste GOOGLE_APPLICATION_CREDENTIALS para o caminho completo do JSON no seu computador, por exemplo `C:/Users/SeuUsuario/Downloads/chave.json`. Rode `python testar_drive.py`, depois `python main.py`. Abra http://localhost:8004.

## Recursos e limites

Mantidos layout, filtros UF/RM/SC/base, metas por indicador, aging, motivos e ranking. O botão de download exporta o resumo filtrado, sem remessas. Detalhes continuam nos GZIP locais. Planos de ação continuam salvos no navegador, com exportação/importação JSON; não são compartilhados pelo Drive.

A pasta privada protege os arquivos no Drive, mas o pacote não inclui autenticação de visitantes no dashboard. O acesso à URL do painel precisa ser tratado conforme a necessidade da operação. A chave fica somente no servidor e não é entregue ao navegador. Não remova o banco antigo antes de comparar resultados com seus relatórios.

Se o acesso falhar: confira API Google Drive ativada no projeto, JSON correto no Secret File, caminho da variável e compartilhamento da pasta com a conta de serviço.
