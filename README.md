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
| GOOGLE_DRIVE_FOLDER_ID | 1pAGIzGgx_gxOPwb6w7J6FBDbH837JAC6 |
| GOOGLE_APPLICATION_CREDENTIALS | /etc/secrets/google-drive.json |
| DRIVE_CACHE_SECONDS | 300 |
| DRIVE_HISTORY_DAYS | 30 |
| JT_TIMEZONE | America/Belem |

6. Salve e faça o deploy. Mantenha a pasta compartilhada como Leitor com `render-indicadores@clever-reserve-513218-u8.iam.gserviceaccount.com`. Não é necessário torná-la pública.

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

## Importação de múltiplos arquivos (Retidos)

O `importar_excel.py` aceita os layouts recebidos em outubro/novembro de 2026:

- layout antigo de Retidos (19–25/10), com `Remessa` e 22 colunas;
- layout novo (27/10 em diante), com `Número da Remessa`, 24/26 colunas e possível aba de pivot/detalhe antes da base bruta;
- exports com nome genérico `Estatística de itens retidos...YYYYMMDDHHMMSS.xlsx`: a data operacional é inferida pelos horários reais do arquivo, sem confundir com a data de exportação;
- arquivos `.zip` com vários XLSX podem ser colocados diretamente em `dados/retidos`.

Validação sem enviar ao banco:

```powershell
python importar_excel.py --dry-run
```

Importação:

```powershell
python importar_excel.py
```

Também é possível passar arquivos/pastas explicitamente:

```powershell
python importar_excel.py "retidos.zip" "RETIDO DO 27 AO 29.zip" "retido_30.xlsx"
```

O importador mostra a cobertura de datas e avisa quando existe algum dia sem arquivo.

## Ajuste de compatibilidade - Sem Mov 26/10+

O importador agora localiza a aba bruta de Sem Mov pelo cabeçalho, sem depender da primeira aba ou de letras fixas de coluna. Também reconhece Aging em formatos antigos e novos e continua processando os demais arquivos caso um único arquivo apresente erro (use `--strict` para interromper no primeiro erro).

Validação recomendada:

```powershell
python importar_excel.py --dry-run
```


## Exportações J&T com nomes originais (04/11/2026)

Sem renomear os Excel:

- `Monitoramento de movimentação em tempo real (novo)(Detalhes)33620020261104091316.xlsx` = `sem_mov`.
- `Estatística de itens retidos na base (dias corridos)(Detalhes)33620020261104091340.xlsx` = `retidos`.

Coloque-os em `dados/sem_mov/` e `dados/retidos/`, respectivamente. A Gestão é lida de `dados/Gestão de colaboradores.xlsx`, ou de outro arquivo passado por `--gestao`.

Valide antes de enviar ao PostgreSQL:

```powershell
python .\importar_excel.py ".\dados\sem_mov\Monitoramento de movimentação em tempo real (novo)(Detalhes)33620020261104091316.xlsx" ".\dados\retidos\Estatística de itens retidos na base (dias corridos)(Detalhes)33620020261104091340.xlsx" --dry-run --strict
```

Confirme a data operacional mostrada no terminal. Os 14 dígitos do nome são a data/hora da **exportação**, que pode diferir da data do snapshot. Sem Mov busca a coluna A `Data`; Retidos infere pelos últimos horários operacionais disponíveis. `--data-ref AAAA-MM-DD` permite indicar uma data operacional previamente confirmada; o código bloqueia divergências conhecidas.

Depois de validar a origem e conferir a data, importe com lotes pequenos:

```powershell
python .\importar_excel.py ".\dados\sem_mov\Monitoramento de movimentação em tempo real (novo)(Detalhes)33620020261104091316.xlsx" ".\dados\retidos\Estatística de itens retidos na base (dias corridos)(Detalhes)33620020261104091340.xlsx" --strict --lote 100
```

Se houver timeout, consulte o PostgreSQL antes de repetir. O `retomar_sem_mov.py` suporta `--data-ref` e retoma apenas se o prefixo existente no banco corresponder ao arquivo. Não utilize esse fluxo genérico para o dia 28/10/2026, que possui 18 linhas antigas auditadas.
