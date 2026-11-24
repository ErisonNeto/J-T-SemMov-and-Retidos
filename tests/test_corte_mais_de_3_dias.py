"""Regressao: Sem Mov e Retidos por aging exigem 4 dias ou mais.

Execute na raiz do projeto: python -m unittest discover -s tests
"""
import unittest
import pandas as pd

from app import aging_distribuicao, limite_indicador, quantidades, ranking


class CorteMaisDeTresDiasTest(unittest.TestCase):
    def setUp(self):
        self.dados = pd.DataFrame([
            {"aging_valor": 2, "pedidos": 2, "sc": "S/C A", "base_codigo": "123"},
            {"aging_valor": 3, "pedidos": 3, "sc": "S/C A", "base_codigo": "123"},
            {"aging_valor": 4, "pedidos": 4, "sc": "S/C A", "base_codigo": "123"},
            {"aging_valor": 10, "pedidos": 10, "sc": "S/C B", "base_codigo": "456"},
            {"aging_valor": 11, "pedidos": 11, "sc": "S/C B", "base_codigo": "456"},
        ])

    def test_sem_mov_4_ou_mais(self):
        self.assertEqual(limite_indicador("sem_mov"), 3)
        self.assertEqual(quantidades(self.dados, "sem_mov"), {"acima_3": 25, "acima_10": 11})

    def test_retidos_aging_permanece_4_ou_mais(self):
        self.assertEqual(limite_indicador("retidos"), 3)
        self.assertEqual(quantidades(self.dados, "retidos"), {"acima_3": 25, "acima_10": 11})

    def test_ranking_ignora_aging_exatamente_3(self):
        resultados = ranking(self.dados, ["sc"], 20, "sem_mov")
        self.assertEqual([(x["sc"], x["acima_3"]) for x in resultados],
                         [("S/C B", 21), ("S/C A", 4)])

    def test_distribuicao_ignora_aging_exatamente_3(self):
        labels = [x["aging"] for x in aging_distribuicao(self.dados, "sem_mov")]
        self.assertEqual(labels, [4, 10, 11])


if __name__ == "__main__":
    unittest.main()
