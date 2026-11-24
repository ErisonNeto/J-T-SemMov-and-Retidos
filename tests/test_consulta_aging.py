"""Testes da ferramenta operacional de Aging (sem API externa)."""
import unittest
from unittest.mock import patch

import pandas as pd

from app import (
    _aplicar_faixa_aging,
    _rotulo_faixa_aging,
    consulta_aging,
    limite_indicador,
    quantidades,
)
from fastapi import HTTPException


class TestConsultaAging(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame([
            {"indicador": "sem_mov", "data": "2026-11-13", "aging_valor": 1,
             "base_codigo": "11", "base_nome": "Base A", "sc": "S/C 1",
             "uf": "PA", "rm": "ORLEAN", "pedidos": 2, "linhas": 2},
            {"indicador": "sem_mov", "data": "2026-11-13", "aging_valor": 3,
             "base_codigo": "11", "base_nome": "Base A", "sc": "S/C 1",
             "uf": "PA", "rm": "ORLEAN", "pedidos": 3, "linhas": 2},
            {"indicador": "sem_mov", "data": "2026-11-14", "aging_valor": 4,
             "base_codigo": "11", "base_nome": "Base A", "sc": "S/C 1",
             "uf": "PA", "rm": "ORLEAN", "pedidos": 4, "linhas": 4},
            {"indicador": "sem_mov", "data": "2026-11-14", "aging_valor": 11,
             "base_codigo": "22", "base_nome": "Base B", "sc": "S/C 2",
             "uf": "PA", "rm": "ORLEAN", "pedidos": 11, "linhas": 10},
            {"indicador": "sem_mov", "data": "2026-11-14", "aging_valor": 1,
             "base_codigo": "33", "base_nome": "Outra RM", "sc": "Outra RM",
             "uf": "PA", "rm": "OUTRO", "pedidos": 999, "linhas": 999},
        ])

    def test_dias_exatos(self):
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "1").pedidos.sum()), 1001)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "3").pedidos.sum()), 3)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "4").pedidos.sum()), 4)

    def test_faixas(self):
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "1a3").pedidos.sum()), 1004)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "4mais").pedidos.sum()), 15)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "11mais").pedidos.sum()), 11)

    def test_valida_faixa(self):
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("0")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("999")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("1; DROP TABLE")
        self.assertEqual(_rotulo_faixa_aging("1"), "1 dia")

    def test_consulta_orlean_e_pedidos_do_ultimo_dia(self):
        with patch("app.carregar", return_value=self.df):
            resultado = consulta_aging(
                indicador="sem_mov", data_inicial="2026-11-13",
                data_final="2026-11-14", uf="", sc="", base="", aging="1"
            )
            self.assertEqual(resultado["total_pedidos"], 2)
            self.assertEqual(resultado["pedidos_ultimo_dia"], 0)
            self.assertEqual(resultado["total_linhas"], 2)
            self.assertEqual(len(resultado["bases"]), 1)
            self.assertEqual(resultado["bases"][0]["base_codigo"], "11")
            result4 = consulta_aging(
                indicador="sem_mov", data_inicial="2026-11-13",
                data_final="2026-11-14", uf="", sc="S/C 1", base="", aging="4mais"
            )
            self.assertEqual(result4["total_pedidos"], 4)

    def test_nao_altera_kpi_oficial(self):
        self.assertEqual(limite_indicador("sem_mov"), 3)
        subset = self.df[self.df.rm == "ORLEAN"]
        self.assertEqual(quantidades(subset, "sem_mov"),
                         {"acima_3": 15, "acima_10": 11})


if __name__ == "__main__":
    unittest.main()
