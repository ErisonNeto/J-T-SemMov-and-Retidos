"""Testes da ferramenta operacional de Aging (sem API externa)."""
import unittest
from unittest.mock import patch

import pandas as pd

from app import (
    _aplicar_faixa_aging,
    _rotulo_faixa_aging,
    consulta_aging,
    pacotes_unicos,
    comparacao_pacotes_unicos,
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

    def test_acima_de_dias_nao_inclui_o_dia_limiar(self):
        # Acima de 1 = 2+, acima de 2 = 3+, acima de 3 = 4+.
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "acima_1").pedidos.sum()), 18)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "acima_2").pedidos.sum()), 18)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "acima_3").pedidos.sum()), 15)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "acima_10").pedidos.sum()), 11)
        self.assertEqual(int(_aplicar_faixa_aging(self.df, "acima_11").pedidos.sum()), 0)
        self.assertEqual(_rotulo_faixa_aging("acima_1"), "Acima de 1 dia")
        self.assertEqual(_rotulo_faixa_aging("acima_2"), "Acima de 2 dias")

    def test_acima_de_dias_funciona_nos_dois_indicadores(self):
        for indicador in ("sem_mov", "retidos"):
            with self.subTest(indicador=indicador):
                dados = self.df.copy()
                dados["indicador"] = indicador
                with patch("app.carregar", return_value=dados):
                    for corte, total in (("acima_1", 18), ("acima_3", 15),
                                         ("acima_10", 11)):
                        with patch(
                            "app.consultar_contagem_remessas",
                            return_value={"pacotes_unicos": 2, "acumulado": total,
                                          "sem_remessa": 0, "evolucao": []},
                        ):
                            resposta = consulta_aging(
                                indicador=indicador, data_inicial="2026-11-13",
                                data_final="2026-11-14", uf="", sc="", base="",
                                aging=corte,
                            )
                            unicos = pacotes_unicos(
                                indicador=indicador, data_inicial="2026-11-13",
                                data_final="2026-11-14", uf="", sc="", base="",
                                aging=corte,
                            )
                        self.assertEqual(resposta["total_pedidos"], total)
                        self.assertEqual(unicos["pacotes_unicos"], 2)
                        self.assertEqual(resposta["pedidos_ultimo_dia"],
                                         15 if corte != "acima_10" else 11)

    def test_valida_faixa(self):
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("0")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("999")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("1; DROP TABLE")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("acima_0")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("acima_366")
        with self.assertRaises(HTTPException):
            _rotulo_faixa_aging("acima_03")
        self.assertEqual(_rotulo_faixa_aging("1"), "1 dia")

    def test_consulta_orlean_e_pedidos_do_ultimo_dia(self):
        with patch("app.carregar", return_value=self.df), patch(
            "app.consultar_contagem_remessas",
            side_effect=[
                {"pacotes_unicos": 1, "acumulado": 2, "sem_remessa": 0, "evolucao": []},
                {"pacotes_unicos": 1, "acumulado": 4, "sem_remessa": 0, "evolucao": []},
            ],
        ):
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

    def test_pacotes_unicos_respeita_filtro_orlean_aging_e_base(self):
        for indicador in ("sem_mov", "retidos"):
            dados = self.df.copy()
            dados["indicador"] = indicador
            with patch("app.carregar", return_value=dados), patch(
                "app.consultar_contagem_remessas",
                return_value={"pacotes_unicos": 1, "acumulado": 4, "sem_remessa": 0, "evolucao": []},
            ) as contagem:
                resposta = pacotes_unicos(
                    indicador=indicador, data_inicial="2026-11-13",
                    data_final="2026-11-14", uf="PA", sc="S/C 1", base="11",
                    aging="acima_3",
                )
                self.assertEqual(resposta["pacotes_unicos"], 1)
                self.assertEqual(resposta["acumulado"], 4)
                contagem.assert_called_once_with(
                    indicador, "2026-11-13", "2026-11-14", ["11"], 4, None
                )

    def test_sem_remessa_ou_fonte_divergente_nao_inventa_total_unico(self):
        for acumulado, sem_remessa in ((4, 1), (999, 0)):
            with patch("app.carregar", return_value=self.df), patch(
                "app.consultar_contagem_remessas",
                return_value={"pacotes_unicos": 1, "acumulado": acumulado,
                              "sem_remessa": sem_remessa, "evolucao": []},
            ):
                resposta = pacotes_unicos(
                    indicador="sem_mov", data_inicial="2026-11-13",
                    data_final="2026-11-14", uf="PA", sc="S/C 1", base="11",
                    aging="acima_3",
                )
                self.assertIsNone(resposta["pacotes_unicos"])
                self.assertEqual(resposta["acumulado"], 4)

    def test_comparacao_semanal_mostra_unicos_em_4_e_11_nos_dois_paineis(self):
        for indicador in ("sem_mov", "retidos"):
            dados = self.df.copy()
            dados["indicador"] = indicador
            retorno_banco = {
                "atual": {
                    "unicos_4": 2, "unicos_11": 1,
                    "acumulado_4": 15, "acumulado_11": 11,
                    "sem_remessa_4": 0, "sem_remessa_11": 0,
                },
            }
            with patch("app.carregar", return_value=dados), patch(
                "app.consultar_remessas_unicas_semanas",
                return_value=retorno_banco,
            ) as consulta:
                resultado = comparacao_pacotes_unicos(
                    indicador=indicador, data_inicial="2026-11-13",
                    data_final="2026-11-14", uf="", sc="", base=""
                )
                self.assertEqual(resultado["atual"]["acima_3"], 2)
                self.assertEqual(resultado["atual"]["acima_10"], 1)
                self.assertEqual(resultado["anterior"]["acima_3"], 0)
                self.assertEqual(resultado["anterior"]["acima_10"], 0)
                consulta.assert_called_once_with(
                    indicador, "2026-11-11", "2026-11-13",
                    "2026-11-14", ["11", "22"]
                )

    def test_comparacao_nao_exibe_unico_quando_falta_remessa(self):
        dados = self.df.copy()
        with patch("app.carregar", return_value=dados), patch(
            "app.consultar_remessas_unicas_semanas",
            return_value={
                "atual": {
                    "unicos_4": 1, "unicos_11": 1,
                    "acumulado_4": 15, "acumulado_11": 11,
                    "sem_remessa_4": 1, "sem_remessa_11": 0,
                }
            },
        ):
            resultado = comparacao_pacotes_unicos(
                indicador="sem_mov", data_inicial="2026-11-13",
                data_final="2026-11-14", uf="", sc="", base=""
            )
        self.assertIsNone(resultado["atual"]["acima_3"])
        self.assertEqual(resultado["atual"]["acima_10"], 1)
        self.assertIn("remessa em branco", resultado["aviso"])

    def test_nao_altera_kpi_oficial(self):
        self.assertEqual(limite_indicador("sem_mov"), 3)
        subset = self.df[self.df.rm == "ORLEAN"]
        self.assertEqual(quantidades(subset, "sem_mov"),
                         {"acima_3": 15, "acima_10": 11})


if __name__ == "__main__":
    unittest.main()
