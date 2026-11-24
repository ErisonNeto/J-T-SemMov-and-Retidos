"""Contrato da consulta de remessas unicas via JT API; nenhuma chamada externa."""
import unittest
from unittest.mock import patch

import arquivos


class RespostaFake:
    ok = True
    text = ""

    def json(self):
        return {"rows": [["2", "4", "0"]]}


class SessaoFake:
    def __init__(self):
        self.headers = {}
        self.chamadas = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers, json, timeout):
        self.chamadas.append({"url": url, "headers": headers, "json": json})
        return RespostaFake()


class ContagemRemessasUnicasTest(unittest.TestCase):
    def test_sql_distinct_aging_arbitragem_filtro_e_acumulado(self):
        for indicador in ("sem_mov", "retidos"):
            with self.subTest(indicador=indicador):
                sessao = SessaoFake()
                with patch.object(arquivos.requests, "Session", return_value=sessao):
                    retorno = arquivos.consultar_contagem_remessas(
                        indicador, "2026-11-19", "2026-11-22",
                        ["12", "11", "12"], 4, 10,
                    )
                self.assertEqual(
                    retorno, {"pacotes_unicos": 2, "acumulado": 4, "sem_remessa": 0}
                )
                chamada = sessao.chamadas[0]["json"]
                self.assertEqual(
                    chamada["params"],
                    ["2026-11-19", "2026-11-22", ["11", "12"], indicador, 4, 10],
                )
                sql = chamada["sql"].upper()
                self.assertIn("COUNT(DISTINCT", sql)
                self.assertIn("SUM(D.PEDIDOS)", sql)
                self.assertIn("D.AGING_VALOR >= %S", sql)
                self.assertIn("D.AGING_VALOR <= %S", sql)
                self.assertIn("NOT EXISTS", sql)
                self.assertIn("JT_ARBITRAGEM_OCORRENCIAS", sql)
                self.assertIn("PROCESSO ENCERRADO", sql)
                self.assertIn("ARBITRAGEM FINALIZADA", sql)
                self.assertIn("D.INDICADOR = %S", sql)

    def test_faixa_aberta_nao_impõe_limite_superior(self):
        sessao = SessaoFake()
        with patch.object(arquivos.requests, "Session", return_value=sessao):
            arquivos.consultar_contagem_remessas(
                "sem_mov", "2026-11-20", "2026-11-22", ["11"], 2, None
            )
        self.assertNotIn("d.aging_valor <= %s", sessao.chamadas[0]["json"]["sql"])
        self.assertEqual(len(sessao.chamadas[0]["json"]["params"]), 5)

    def test_sem_bases_nao_consulta_postgresql(self):
        with patch.object(arquivos.requests, "Session") as sessao:
            retorno = arquivos.consultar_contagem_remessas(
                "sem_mov", "2026-11-20", "2026-11-22", [], 4, None
            )
        self.assertEqual(retorno["pacotes_unicos"], 0)
        self.assertEqual(retorno["acumulado"], 0)
        sessao.assert_not_called()


if __name__ == "__main__":
    unittest.main()
