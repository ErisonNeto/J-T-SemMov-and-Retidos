-- J&T • Volumetria principal (J + M) por data e base.
-- Executar uma vez como usuario com permissao de CREATE TABLE.
-- Tabela isolada: nao altera sem_mov_retidos_detalhe nem jt_gestao_bases.
CREATE TABLE IF NOT EXISTS public.jt_outlet_volume_diario (
    data date NOT NULL,
    base_codigo text NOT NULL,
    coletada bigint NOT NULL DEFAULT 0 CHECK (coletada >= 0),
    saida_entrega bigint NOT NULL DEFAULT 0 CHECK (saida_entrega >= 0),
    PRIMARY KEY (data, base_codigo)
);

-- Opcional: melhora filtro de datas para o painel.
CREATE INDEX IF NOT EXISTS idx_outlet_volume_data
    ON public.jt_outlet_volume_diario (data);

-- Index de maior impacto para queries do indicador no banco volumoso.
-- IMPORTANTE: execute esta instrucao isoladamente e fora de transacao
-- durante janela de baixa atividade. Em bancos grandes leva minutos
-- e pode consumir I/O. Nao rodar enquanto importa snapshots.
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS
--     idx_sm_ret_base_data_ind_aging
--     ON public.sem_mov_retidos_detalhe
--        (base_codigo, data, indicador, aging_valor);
