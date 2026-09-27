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

O caminho vivo (`python -m jarvis`) ja nao tem `--modelo`: o motor vem de
`[ouvido].motor` no config.toml, validado contra a lista fechada
`jarvis.stt.MOTORES`, e o faster-whisper dentro dela so aceita os tamanhos de
`MotorFasterWhisper.MODELOS`. Os testes do caminho vivo verificam essa lista.

Tambem aqui: o VAD Silero que ouve por cima da voz do jarvis usa o ficheiro
que ja veio com o openWakeWord, registado em docs/MODELOS.md como usado, com
o mesmo URL e sha256 que o codigo conhece. O mesmo para o Smart Turn do fim
de turno, o unico ficheiro novo, que o .gitignore deixa fora do Git.

NENHUM teste aqui toca no GPU, descarrega nada ou carrega um modelo: sao
constantes, parsers de linha de comandos e, quando o ficheiro existe, o sha256
dele.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import fim_de_turno, stt, vad_silero  # noqa: E402
from jarvis.config import ConfigError, ConfigOuvido, carregar_config  # noqa: E402
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
        self.assertIn(CANDIDATO_TURBO, stt.MotorFasterWhisper.MODELOS)
        self.assertIn(f"whisper-{CANDIDATO_TURBO}", stt.MOTORES)

    def test_o_caminho_vivo_so_corre_tamanhos_que_o_arnes_mede(self) -> None:
        # O arnes mede pela lista do transcritor; o caminho vivo nunca corre um
        # tamanho que o arnes nao consiga medir.
        self.assertLessEqual(set(stt.MotorFasterWhisper.MODELOS), set(transcrever_ficheiro.MODELOS_PERMITIDOS))

    def test_a_lista_nao_deixa_passar_um_repo_id_do_hugging_face(self) -> None:
        # A razao de ser da lista: nenhum nome com barra, que e a forma
        # de um `owner/repo` arbitrario.
        for nome in (*transcrever_ficheiro.MODELOS_PERMITIDOS, *stt.MOTORES):
            self.assertNotIn("/", nome, f"{nome!r} parece um repo id, nao um tamanho")
            self.assertNotIn("\\", nome, f"{nome!r} parece um caminho, nao um tamanho")

    def test_os_tamanhos_ja_registados_na_d14e_continuam_la(self) -> None:
        # Nomes que docs/MODELOS.md ja documenta com URL e sha256.
        for nome in ("tiny", "base", "small", "medium", "large-v3", CANDIDATO_TURBO):
            self.assertIn(nome, transcrever_ficheiro.MODELOS_PERMITIDOS)


class TestModeloPorOmissao(unittest.TestCase):
    """O `large-v3-turbo`: medido, nao adotado."""

    def test_o_default_do_caminho_vivo_e_o_parakeet(self) -> None:
        self.assertEqual(ConfigOuvido().motor, "parakeet-tdt-0.6b-v3")

    def test_o_default_do_transcritor_continua_medium(self) -> None:
        self.assertEqual(transcrever_ficheiro.MODELO_PREFERIDO, "medium")

    def test_o_fallback_continua_small(self) -> None:
        self.assertEqual(transcrever_ficheiro.MODELO_FALLBACK, "small")

    def test_entrar_na_lista_nao_e_ser_o_default(self) -> None:
        # O contrato numa linha: o candidato e medivel e nao e o preferido.
        self.assertIn(f"whisper-{CANDIDATO_TURBO}", stt.MOTORES)
        self.assertNotEqual(ConfigOuvido().motor, f"whisper-{CANDIDATO_TURBO}")
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

    def _config_com_motor(self, motor: str):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "config.toml"
            linhas = (
                "[microfone]",
                'nome = "x"',
                "[[projetos]]",
                'nome = "exemplo"',
                'caminho = "D:/caminho/para/exemplo"',
                "[ouvido]",
                f'motor = "{motor}"',
            )
            caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
            return carregar_config(caminho, validar_caminhos=False)

    def test_a_config_do_caminho_vivo_aceita_o_candidato(self) -> None:
        self.assertEqual(self._config_com_motor(f"whisper-{CANDIDATO_TURBO}").ouvido.motor, f"whisper-{CANDIDATO_TURBO}")

    def test_a_config_do_caminho_vivo_recusa_um_repo_arbitrario(self) -> None:
        with self.assertRaises(ConfigError):
            self._config_com_motor("alguem/repo-mau")

    def test_o_motor_do_caminho_vivo_recusa_um_tamanho_fora_da_lista(self) -> None:
        with self.assertRaises(ValueError):
            stt.criar_motor("whisper-alguem/repo-mau")
        with self.assertRaises(ValueError):
            stt.MotorFasterWhisper("alguem/repo-mau")

    def test_sem_flag_o_transcritor_corre_no_default_medido(self) -> None:
        self.assertEqual(
            transcrever_ficheiro.construir_parser().parse_args(["x.wav"]).modelo,
            "medium",
        )

    def test_as_choices_da_cli_sao_a_lista_fechada(self) -> None:
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



class TestSileroRegistado(unittest.TestCase):
    """O VAD do interromper: o ficheiro do openWakeWord, registado e usado."""

    CAMINHO = "models/openwakeword/silero_vad.onnx"
    URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/silero_vad.onnx"

    def setUp(self) -> None:
        self.modelos = (RAIZ / "docs" / "MODELOS.md").read_text(encoding="utf-8")

    def test_o_codigo_usa_o_ficheiro_registado(self) -> None:
        self.assertEqual(vad_silero.MODELO_SILERO.relative_to(RAIZ).as_posix(), self.CAMINHO)

    def test_o_registo_diz_que_e_usado_com_url_e_sha256(self) -> None:
        linhas = [linha for linha in self.modelos.splitlines() if f"`{self.CAMINHO}`" in linha and self.URL in linha]
        self.assertTrue(linhas, "docs/MODELOS.md nao regista o Silero com o URL")
        for linha in linhas:
            self.assertIn(vad_silero.SHA256_DO_SILERO, linha)
            self.assertNotIn("not used", linha)
        self.assertIn("## Interrupting jarvis: Silero VAD", self.modelos)

    def test_o_ficheiro_no_disco_e_o_registado(self) -> None:
        caminho = RAIZ / self.CAMINHO
        if not caminho.is_file():
            self.skipTest(f"{self.CAMINHO} em falta")
        self.assertEqual(hashlib.sha256(caminho.read_bytes()).hexdigest(), vad_silero.SHA256_DO_SILERO)


class TestSmartTurnRegistado(unittest.TestCase):
    """O modelo do fim de turno: registado com URL e sha256, nunca versionado."""

    CAMINHO = "models/smart-turn/smart-turn-v3.2-cpu.onnx"
    URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx"

    def setUp(self) -> None:
        self.modelos = (RAIZ / "docs" / "MODELOS.md").read_text(encoding="utf-8")

    def test_o_codigo_usa_o_ficheiro_e_o_url_registados(self) -> None:
        self.assertEqual(fim_de_turno.MODELO_SMART_TURN.relative_to(RAIZ).as_posix(), self.CAMINHO)
        self.assertEqual(fim_de_turno.URL_DO_SMART_TURN, self.URL)

    def test_o_registo_tem_url_e_sha256(self) -> None:
        linhas = [linha for linha in self.modelos.splitlines() if f"`{self.CAMINHO}`" in linha and self.URL in linha]
        self.assertTrue(linhas, "docs/MODELOS.md nao regista o Smart Turn com o URL")
        for linha in linhas:
            self.assertIn(fim_de_turno.SHA256_DO_SMART_TURN, linha)
        self.assertIn("## End of turn: Smart Turn v3", self.modelos)
        self.assertIn("BSD-2", self.modelos)

    def test_o_ficheiro_no_disco_e_o_registado(self) -> None:
        caminho = RAIZ / self.CAMINHO
        if not caminho.is_file():
            self.skipTest(f"{self.CAMINHO} em falta")
        self.assertEqual(hashlib.sha256(caminho.read_bytes()).hexdigest(), fim_de_turno.SHA256_DO_SMART_TURN)

    def test_modelos_e_audio_ficam_fora_do_git(self) -> None:
        ignorados = set((RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines())
        for padrao in ("models/", "*.onnx", "recordings/", "*.wav"):
            self.assertIn(padrao, ignorados)


if __name__ == "__main__":
    unittest.main()
