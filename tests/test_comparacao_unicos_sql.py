"""Testes do contrato de contagem distinta entre semanas (sem PostgreSQL)."""
import unittest
from unittest.mock import patch

import arquivos


class RespostaFake:
    ok = True
    text = ""

    def json(self):
        return {"rows": [
            ["anterior", "4", "1", "6", "2", "0", "0"],
            ["atual", "3", "2", "8", "4", "0", "0"],
        ]}


class SessaoFake:
    def __init__(self):
        self.headers = {}
        self.chamadas = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers, json, timeout):
        self.chamadas.append(json)
        return RespostaFake()


class TestComparacaoSemanal(unittest.TestCase):
    def test_duas_semanas_distintos_por_corte_e_arbitragem(self):
        for indicador in ("sem_mov", "retidos"):
            with self.subTest(indicador=indicador):
                sessao = SessaoFake()
                with patch.object(arquivos.requests, "Session", return_value=sessao):
                    resultado = arquivos.consultar_remessas_unicas_semanas(
                        indicador, "2026-11-09", "2026-11-16",
                        "2026-11-22", ["B2", "B1", "B1"]
                    )
                self.assertEqual(resultado["anterior"]["unicos_4"], 4)
                self.assertEqual(resultado["atual"]["unicos_11"], 2)
                self.assertEqual(resultado["atual"]["acumulado_4"], 8)
                self.assertEqual(resultado["anterior"]["acumulado_11"], 2)
                chamada = sessao.chamadas[0]
                self.assertEqual(chamada["params"], [
                    "2026-11-16", "2026-11-09", "2026-11-22",
                    ["B1", "B2"], indicador
                ])
                sql = chamada["sql"].upper()
                self.assertIn("COUNT(DISTINCT REMESSA)", sql)
                self.assertIn("GROUP BY PERIODO", sql)
                self.assertIn("AGING > 3", sql)
                self.assertIn("AGING > 10", sql)
                self.assertIn("NOT EXISTS", sql)
                self.assertIn("JT_ARBITRAGEM_OCORRENCIAS", sql)
                self.assertIn("PROCESSO ENCERRADO", sql)
                self.assertIn("ARBITRAGEM FINALIZADA", sql)

    def test_sem_bases_nao_faz_query(self):
        with patch.object(arquivos.requests, "Session") as session:
            self.assertEqual(arquivos.consultar_remessas_unicas_semanas(
                "retidos", "2026-11-09", "2026-11-16", "2026-11-22", []
            ), {})
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
