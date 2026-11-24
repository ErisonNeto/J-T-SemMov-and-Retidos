"""Retidos Resumo nao pode passar como relatorio de remessas detalhadas."""
import unittest
from pathlib import Path

from importar_tres_auto import eh_retidos_resumo, tipo_arquivo


class TestRetidosResumo(unittest.TestCase):
    def test_resumo_e_identificado(self):
        resumo = Path(
            "Estatística de itens retidos na base (dias corridos)"
            "(Resumo)33620020261124091530.xlsx"
        )
        self.assertEqual(tipo_arquivo(resumo), "retidos")
        self.assertTrue(eh_retidos_resumo(resumo))

    def test_detalhes_nao_e_resumo(self):
        detalhes = Path(
            "Estatística de itens retidos na base (dias corridos)"
            "(Detalhes)33620020261124091530.xlsx"
        )
        self.assertEqual(tipo_arquivo(detalhes), "retidos")
        self.assertFalse(eh_retidos_resumo(detalhes))

    def test_sem_mov_e_outlet_nao_sao_retidos_resumo(self):
        for nome in (
            "Monitoramento de movimentação em tempo real (novo)(Detalhes).xlsx",
            "Análise do volume de negócios do outlet (novo)(Resumo).xlsx",
        ):
            self.assertFalse(eh_retidos_resumo(Path(nome)))


if __name__ == "__main__":
    unittest.main()
