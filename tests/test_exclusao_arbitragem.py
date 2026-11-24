"""Regressao: arbitragem encerrada nao entra nos resumos oficiais.

O filtro precisa ocorrer no PostgreSQL antes do GROUP BY para que
Sem Mov, Retidos, Aging, ranking e exportacao tenham a mesma base.
"""
import unittest
from unittest.mock import patch

import arquivos


class RespostaFalsa:
    ok = True
    text = ""

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"rows": self._rows}


class SessaoFalsa:
    def __init__(self):
        self.headers = {}
        self.consultas = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers, json, timeout):
        self.consultas.append(json)
        if "jt_gestao_bases" in json["sql"]:
            return RespostaFalsa([
                ["B-001", "Base 1", "ORLEAN", "Coord A", "PA", "GP"],
            ])
        return RespostaFalsa([
            ["sem_mov", "2026-11-24", "B-001", 4, 2, 2],
            ["retidos", "2026-11-24", "B-001", 11, 1, 1],
        ])


class TestExclusaoArbitragem(unittest.TestCase):
    def test_resumo_filtra_remessas_encerradas_antes_de_agregar(self):
        sessao = SessaoFalsa()
        with patch.object(arquivos.requests, "Session", return_value=sessao):
            frame = arquivos._consultar_resumo()
        self.assertEqual(set(frame.indicador), {"sem_mov", "retidos"})
        self.assertEqual(int(frame.pedidos.sum()), 3)
        self.assertEqual(len(sessao.consultas), 2)
        sql = sessao.consultas[-1]["sql"].upper()
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("A.REMESSA = D.REMESSA", sql)
        self.assertIn("JT_ARBITRAGEM_OCORRENCIAS", sql)
        self.assertIn("PROCESSO ENCERRADO", sql)
        self.assertIn("ARBITRAGEM FINALIZADA", sql)
        self.assertIn("FECHADA_EM", sql)
        self.assertIn("<= D.DATA::DATE", sql)
        self.assertIn("GROUP BY", sql)
        self.assertLess(sql.index("NOT EXISTS"), sql.index("GROUP BY"))
        self.assertNotIn("REVOGADO", sql)
        self.assertNotIn("APElação".upper(), sql)


if __name__ == "__main__":
    unittest.main()
