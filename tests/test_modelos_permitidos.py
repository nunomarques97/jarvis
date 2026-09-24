r"""Testes das listas FECHADAS de modelos de transcricao e do modelo por omissao.

Escritos quando se mediu o `large-v3-turbo` contra o `medium` nas quatro
combinacoes (pt/en, com/sem prefixo). Dois contratos diferentes vivem aqui, e e por
serem diferentes que ambos precisam de teste:

  * A LISTA e uma barreira de seguranca. `faster_whisper.WhisperModel` aceita
    qualquer `repo/id` do Hugging Face, por isso um `--modelo alguem/repo-mau`
    sem lista fechada mandava descarregar pesos arbitrarios para `models/`,
    fora do registo de docs/MODELOS.md, e entrega-los ao parser binario do CTranslate2.
    Um nome novo so entra na lista para poder ser MEDIDO.
  * O DEFAULT e uma decisao de produto, e so os numeros medidos a mudam. O
    `large-v3-turbo` foi medido e NAO adotado: com os MESMOS WAV, `language='pt'`
    fixo, o acerto de intencao em PT sem prefixo desceu de 11/20 para 10/20
    (perdeu a unica accao local dessa combinacao: «abre a pasta do jarvis» saiu
    «Hava a pasta do Javis.» em vez de «Abre a pasta dos Javis.»). O acerto vem
    primeiro, mesmo com a latencia e a VRAM do turbo a passarem com folga. Os
    numeros estao em `docs/MODELOS.md`.

Se alguem trocar o default para `large-v3-turbo` sem medir de novo, estes
testes ficam vermelhos — que e exatamente o que se quer, porque «maior e
melhor» ja falhou duas vezes neste repositorio (o `large-v3` simples e o
`large-v3-turbo`).

NENHUM teste aqui toca no GPU, descarrega nada ou carrega um modelo: sao
constantes e parsers de linha de comandos.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import app  # noqa: E402
from scripts import transcrever_ficheiro  # noqa: E402

#: O candidato medido e nao adotado.
CANDIDATO_TURBO = "large-v3-turbo"


def accao_do_parser(parser: argparse.ArgumentParser, flag: str) -> argparse.Action:
    for accao in parser._actions:  # noqa: SLF001 - a API publica nao expoe isto
        if flag in accao.option_strings:
            return accao
    raise AssertionError(f"o parser nao tem {flag}")


class TestListasFechadas(unittest.TestCase):
    """O que a lista tem de continuar a garantir."""

    def test_o_candidato_da_t9_esta_nas_duas_listas(self) -> None:
        # Sem isto o arnes nem consegue medir: `medir_voz.py --modelo` usa
        # MODELOS_PERMITIDOS como `choices`, e o caminho vivo recusaria o nome.
        self.assertIn(CANDIDATO_TURBO, transcrever_ficheiro.MODELOS_PERMITIDOS)
        self.assertIn(CANDIDATO_TURBO, app.MODELOS_STT_PERMITIDOS)

    def test_as_duas_listas_sao_iguais(self) -> None:
        # O arnes mede pela lista do transcritor e o produto corre pela lista do
        # app: se divergirem, mede-se um modelo que o caminho vivo recusa.
        self.assertEqual(
            transcrever_ficheiro.MODELOS_PERMITIDOS,
            app.MODELOS_STT_PERMITIDOS,
        )

    def test_a_lista_nao_deixa_passar_um_repo_id_do_hugging_face(self) -> None:
        # A razao de ser da lista: nenhum nome com barra, que e a forma
        # de um `owner/repo` arbitrario.
        for nome in transcrever_ficheiro.MODELOS_PERMITIDOS:
            self.assertNotIn("/", nome, f"{nome!r} parece um repo id, nao um tamanho")
            self.assertNotIn("\\", nome, f"{nome!r} parece um caminho, nao um tamanho")

    def test_os_tamanhos_ja_registados_na_d14e_continuam_la(self) -> None:
        # Nomes que docs/MODELOS.md ja documenta com URL e sha256.
        for nome in ("tiny", "base", "small", "medium", "large-v3", CANDIDATO_TURBO):
            self.assertIn(nome, app.MODELOS_STT_PERMITIDOS)


class TestModeloPorOmissao(unittest.TestCase):
    """O `large-v3-turbo`: medido, nao adotado."""

    def test_o_default_do_caminho_vivo_continua_medium(self) -> None:
        self.assertEqual(app.MODELO_STT, "medium")

    def test_o_default_do_transcritor_continua_medium(self) -> None:
        self.assertEqual(transcrever_ficheiro.MODELO_PREFERIDO, "medium")

    def test_o_fallback_continua_small(self) -> None:
        self.assertEqual(transcrever_ficheiro.MODELO_FALLBACK, "small")

    def test_entrar_na_lista_nao_e_ser_o_default(self) -> None:
        # O contrato numa linha: o candidato e medivel e nao e o preferido.
        self.assertIn(CANDIDATO_TURBO, app.MODELOS_STT_PERMITIDOS)
        self.assertNotEqual(app.MODELO_STT, CANDIDATO_TURBO)
        self.assertNotEqual(transcrever_ficheiro.MODELO_PREFERIDO, CANDIDATO_TURBO)


class TestParsers(unittest.TestCase):
    """As duas CLI que expoem `--modelo` ao utilizador."""

    def test_a_cli_do_transcritor_aceita_o_candidato(self) -> None:
        args = transcrever_ficheiro.construir_parser().parse_args(
            ["x.wav", "--modelo", CANDIDATO_TURBO]
        )
        self.assertEqual(args.modelo, CANDIDATO_TURBO)

    def test_a_cli_do_transcritor_recusa_um_repo_arbitrario(self) -> None:
        parser = transcrever_ficheiro.construir_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["x.wav", "--modelo", "alguem/repo-mau"])

    def test_a_cli_do_caminho_vivo_aceita_o_candidato(self) -> None:
        args = app.construir_parser().parse_args(["--modelo", CANDIDATO_TURBO])
        self.assertEqual(args.modelo, CANDIDATO_TURBO)

    def test_a_cli_do_caminho_vivo_recusa_um_repo_arbitrario(self) -> None:
        parser = app.construir_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--modelo", "alguem/repo-mau"])

    def test_sem_flag_as_duas_cli_correm_no_default_medido(self) -> None:
        self.assertEqual(app.construir_parser().parse_args([]).modelo, "medium")
        self.assertEqual(
            transcrever_ficheiro.construir_parser().parse_args(["x.wav"]).modelo,
            "medium",
        )

    def test_as_choices_das_duas_cli_sao_as_listas_fechadas(self) -> None:
        self.assertEqual(
            list(accao_do_parser(app.construir_parser(), "--modelo").choices),
            app.MODELOS_STT_PERMITIDOS,
        )
        parser = transcrever_ficheiro.construir_parser()
        self.assertEqual(
            list(accao_do_parser(parser, "--modelo").choices),
            transcrever_ficheiro.MODELOS_PERMITIDOS,
        )
        # O --fallback partilha a mesma lista: um fallback fora da D14e seria o
        # mesmo buraco, so que aberto pelo caminho de recurso.
        self.assertEqual(
            list(accao_do_parser(parser, "--fallback").choices),
            transcrever_ficheiro.MODELOS_PERMITIDOS,
        )


if __name__ == "__main__":
    unittest.main()
