r"""Testes de jarvis/voz.py, unittest da biblioteca padrao (sem pytest, mesma
convencao de tests/test_router.py e tests/test_app.py: nao ha decisao do
Technology Scout para uma framework de testes fora da biblioteca padrao).

Este ficheiro cobre so o ponto 1 da T12 (QA-close-1.md, finding 4a): o import
tardio do RealtimeTTS em `_construir_stream()` fica dentro de um
`warnings.catch_warnings()` que ignora SO o `RuntimeWarning` do `pydub.utils`
(o aviso de ffmpeg em falta), e `warnings.filters` volta EXATAMENTE ao estado
anterior depois do import — lista inteira, nao so as entradas do pydub. A
comparacao tem de ser da lista inteira porque e isso que apanha o contrario do
que a task pede: um `warnings.filterwarnings("ignore")` global passaria numa
comparacao so das entradas do pydub.

Nao toca em GPU, em Piper nem em audio real: so o comportamento do import e do
estado dos filtros de warnings, que corre em qualquer maquina com o venv
instalado (RealtimeTTS ja e dependencia decidida, TECHNOLOGY.md S1/S4).

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
import warnings

from jarvis import voz


class TestFiltroDeAvisoDoPydub(unittest.TestCase):
    """Ponto 1 da T12: so o import do RealtimeTTS fica silenciado, e so
    localmente — nada de `warnings.filterwarnings("ignore")` global."""

    def _accionar_o_import(self) -> None:
        """Aciona o mesmo caminho que `falar()` usa: o import tardio do
        RealtimeTTS dentro do `with warnings.catch_warnings()`. Pode falhar
        depois disso (FileNotFoundError se o modelo Piper nao estiver
        descarregado nesta maquina) — irrelevante para estes testes, que so
        olham para o estado dos filtros de warnings."""
        try:
            voz._construir_stream()
        except FileNotFoundError:
            pass

    def test_iii_warnings_filters_volta_ao_estado_anterior(self) -> None:
        """(iii) da definicao de pronto, em sentido estrito: a lista INTEIRA de
        `warnings.filters` e a mesma antes e depois do import."""
        filtros_antes = list(warnings.filters)

        self._accionar_o_import()

        self.assertEqual(
            list(warnings.filters),
            filtros_antes,
            "o import do RealtimeTTS mexeu no estado global de warnings.filters",
        )

    def test_o_filtro_do_pydub_nao_fica_registado_globalmente(self) -> None:
        """O mesmo visto pelo lado do filtro concreto: nenhuma entrada de
        `ignore` para `pydub.utils` sobrevive ao `with`."""
        self._accionar_o_import()

        do_pydub = [
            entrada
            for entrada in warnings.filters
            if entrada[0] == "ignore"
            and entrada[2] is RuntimeWarning
            and entrada[3] is not None
            and entrada[3].pattern == r"pydub\.utils"
        ]
        self.assertEqual(do_pydub, [])

    def test_o_import_e_mesmo_accionado(self) -> None:
        """Sem isto os testes acima passariam por nao fazerem nada: confirma que
        `_construir_stream()` carregou mesmo o RealtimeTTS (e com ele o pydub,
        o que emite o aviso que se quer silenciado)."""
        self._accionar_o_import()

        self.assertIn("RealtimeTTS", sys.modules)
        self.assertIn("pydub", sys.modules)


if __name__ == "__main__":
    unittest.main()
