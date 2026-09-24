r"""Testes das accoes locais (jarvis/acoes_locais.py), unittest da biblioteca
padrao (sem pytest: nenhuma framework de testes fora da biblioteca padrao,
mesma convencao de tests/test_router.py).

Config sempre FICTICIA e em memoria ou num ficheiro temporario (nunca o
config.toml real): os projetos usados chamam-se "exemplo-um" e
"exemplo-dois", com pastas temporarias que sao apagadas no fim de cada teste.

Nenhum destes testes arranca um subprocess a serio (VS Code, explorador) nem
toca no piper.exe: --simular nunca chega ao subprocess.Popen (verificado
diretamente), e os testes de voz correm com --sem-voz, com um stream falso OU
chamam so as funcoes puras que nao falam. Esta CLI e passo do guiao de
testes manuais, por isso sem `--com-som` nao pede reproducao nenhuma
(`TestCliSilenciosaPorOmissao`). O que estes testes protegem, alem do caminho feliz:

  * --simular nunca executa nada: nao arranca processo nenhum, devolve a
    linha de comando exata que arrancaria (lista de argumentos, sempre
    shell=False, executavel real e nunca um shim .cmd);
  * um projeto que nao esta na configuracao e SEMPRE recusado com um erro
    legivel, tanto pela CLI (exit code != 0) como pela biblioteca
    (AcaoError) — nunca "o mais parecido";
  * um ResultadoRouter cujo argumento nao corresponde a nenhum caminho da
    config e recusado por executar(), mesmo vindo "do router" (defesa em
    profundidade contra um bug no router);
  * as tres accoes adiadas para o jarvis/app.py (calar/adormecer/acordar) sao recusadas
    com uma razao clara, nunca fingidas como "feitas".

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from io import StringIO
from pathlib import Path

from jarvis.acoes_locais import (
    ACOES_ADIADAS,
    AcaoError,
    ResultadoAcao,
    _projeto_conhecido,
    _projeto_pelo_caminho,
    abrir_pasta,
    abrir_vscode,
    executar,
    horas_e_data,
    main,
)
from jarvis.config import Config, ConfigError, Projeto
from jarvis.router import ResultadoRouter


def _config_com_pastas_reais(pasta_base: Path) -> Config:
    """Uma Config com projetos cujos caminhos EXISTEM mesmo no disco (pasta
    temporaria do teste) — carregar_config() exige isto, e
    abrir_vscode/abrir_pasta usam o caminho tal e qual num subprocess.Popen
    de mentira (--simular nunca chega la, mas o caminho tem de ser real para
    o teste ser representativo)."""
    caminho_um = pasta_base / "exemplo-um"
    caminho_dois = pasta_base / "exemplo-dois"
    caminho_um.mkdir()
    caminho_dois.mkdir()
    return Config(
        microfone="Microfone Ficticio de Teste",
        projetos=(
            Projeto(nome="exemplo-um", caminho=caminho_um.resolve()),
            Projeto(nome="exemplo-dois", caminho=caminho_dois.resolve()),
        ),
    )


class TestHorasEData(unittest.TestCase):
    """D4.a: nao precisa de configuracao nenhuma, nunca arranca subprocess."""

    def test_horas_devolve_texto_com_a_hora(self) -> None:
        import datetime

        resultado = horas_e_data("horas", agora=datetime.datetime(2026, 9, 20, 15, 30))
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertTrue(resultado.executou)
        self.assertIn("15", resultado.texto)
        self.assertIn("30", resultado.texto)

    def test_data_e_distinguida_de_horas_pelo_argumento(self) -> None:
        import datetime

        resultado = horas_e_data("data", agora=datetime.datetime(2026, 9, 20, 15, 30))
        self.assertNotEqual(resultado.texto, horas_e_data("horas").texto)
        self.assertIn("2026", resultado.texto)

    def test_horas_e_data_nunca_tem_comando_de_subprocess(self) -> None:
        # Contrato do modulo: so abrir_vscode/abrir_pasta arrancam processos.
        self.assertEqual(horas_e_data("horas").comando, "")


class TestModoSimularNuncaExecutaNada(unittest.TestCase):
    """--simular imprime a linha de comando exata e
    NAO abre nada. Verificado das duas formas: sem Popen a ser chamado, e com
    o resultado a dizer explicitamente executou=False."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name)
        self.config = _config_com_pastas_reais(self.pasta)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_abrir_vscode_simulado_nao_chama_popen(self) -> None:
        projeto = self.config.encontrar_projeto("exemplo-um")
        assert projeto is not None
        chamadas: list[list[str]] = []
        popen_original = subprocess.Popen

        def popen_espiao(comando, *args, **kwargs):  # noqa: ANN001
            chamadas.append(list(comando))
            raise AssertionError("--simular nunca deve chamar subprocess.Popen")

        subprocess.Popen = popen_espiao  # type: ignore[assignment]
        try:
            resultado = abrir_vscode(projeto, simular=True)
        finally:
            subprocess.Popen = popen_original  # type: ignore[assignment]

        self.assertEqual(chamadas, [])
        self.assertFalse(resultado.executou)
        self.assertIn(str(projeto.caminho), resultado.comando)
        self.assertIn("Code.exe", resultado.comando)

    def test_abrir_pasta_simulado_nao_chama_popen(self) -> None:
        projeto = self.config.encontrar_projeto("exemplo-dois")
        assert projeto is not None
        popen_original = subprocess.Popen

        def popen_espiao(comando, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("--simular nunca deve chamar subprocess.Popen")

        subprocess.Popen = popen_espiao  # type: ignore[assignment]
        try:
            resultado = abrir_pasta(projeto, simular=True)
        finally:
            subprocess.Popen = popen_original  # type: ignore[assignment]

        self.assertFalse(resultado.executou)
        self.assertIn(str(projeto.caminho), resultado.comando)
        self.assertIn("explorer.exe", resultado.comando.lower())

    def test_comando_simulado_e_uma_lista_shell_false_nunca_uma_string_composta(self) -> None:
        # Defesa em profundidade: a linha impressa e so para o utilizador
        # ler; o que corre de verdade (fora de --simular) e sempre uma lista.
        projeto = self.config.encontrar_projeto("exemplo-um")
        assert projeto is not None
        resultado = abrir_vscode(projeto, simular=True)
        # subprocess.list2cmdline aspas caminhos com espacos; o executavel
        # (dentro de "Program Files"/"AppData...VS Code") tem de vir citado.
        self.assertTrue(resultado.comando.startswith('"'))

    def test_cli_simular_nao_abre_nada_e_imprime_o_comando(self) -> None:
        caminho_config = self.pasta / "config.toml"
        projeto = self.config.projetos[0]
        caminho_config.write_text(
            '[microfone]\nnome = "Microfone de Teste"\n\n'
            f'[[projetos]]\nnome = "exemplo-um"\ncaminho = "{projeto.caminho.as_posix()}"\n',
            encoding="utf-8",
        )
        popen_original = subprocess.Popen

        def popen_espiao(comando, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("--simular nunca deve chamar subprocess.Popen")

        subprocess.Popen = popen_espiao  # type: ignore[assignment]
        saida = StringIO()
        stdout_original = sys.stdout
        sys.stdout = saida
        try:
            codigo = main(
                [
                    "--config",
                    str(caminho_config),
                    "--sem-voz",
                    "abrir-vscode",
                    "exemplo-um",
                    "--simular",
                ]
            )
        finally:
            sys.stdout = stdout_original
            subprocess.Popen = popen_original  # type: ignore[assignment]

        self.assertEqual(codigo, 0)
        texto = saida.getvalue()
        self.assertIn("comando", texto)
        self.assertIn(str(projeto.caminho), texto)
        self.assertIn("(simulado", texto)


class TestProjetoDesconhecidoENuncaAdivinhado(unittest.TestCase):
    """D4: um projeto que nao esta na config e sempre recusado, nunca 'o
    mais parecido' — tanto pela biblioteca como pela CLI."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name)
        self.config = _config_com_pastas_reais(self.pasta)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_projeto_conhecido_por_nome_desconhecido_levanta_acaoerror(self) -> None:
        with self.assertRaises(AcaoError) as ctx:
            _projeto_conhecido("projeto-fantasma", self.config)
        self.assertIn("nao esta na configuracao", str(ctx.exception))
        self.assertIn("exemplo-um", str(ctx.exception))  # lista os conhecidos

    def test_nome_parecido_nao_e_o_projeto(self) -> None:
        # "exemplo dos" nao e "exemplo-dois" (mesma regra do router).
        with self.assertRaises(AcaoError):
            _projeto_conhecido("exemplo dos", self.config)

    def test_argumento_de_router_com_caminho_fora_da_config_e_recusado(self) -> None:
        caminho_alheio = str(self.pasta / "nunca-esteve-na-config")
        with self.assertRaises(AcaoError) as ctx:
            _projeto_pelo_caminho(caminho_alheio, self.config)
        self.assertIn("nao corresponde a nenhum projeto", str(ctx.exception))

    def test_executar_recusa_projeto_desconhecido_vindo_do_router(self) -> None:
        resultado_router = ResultadoRouter(
            "local", nome_acao="abrir_vscode", argumento=str(self.pasta / "fantasma")
        )
        with self.assertRaises(AcaoError):
            executar(resultado_router, self.config, simular=True)

    def test_cli_recusa_projeto_desconhecido_com_exit_diferente_de_zero(self) -> None:
        caminho_config = self.pasta / "config.toml"
        projeto = self.config.projetos[0]
        caminho_config.write_text(
            '[microfone]\nnome = "Microfone de Teste"\n\n'
            f'[[projetos]]\nnome = "exemplo-um"\ncaminho = "{projeto.caminho.as_posix()}"\n',
            encoding="utf-8",
        )
        saida_erro = StringIO()
        stderr_original = sys.stderr
        sys.stderr = saida_erro
        try:
            codigo = main(
                [
                    "--config",
                    str(caminho_config),
                    "--sem-voz",
                    "abrir-vscode",
                    "projeto-fantasma",
                    "--simular",
                ]
            )
        finally:
            sys.stderr = stderr_original

        self.assertNotEqual(codigo, 0)
        self.assertIn("nao esta na configuracao", saida_erro.getvalue())

    def test_cli_com_config_ficticia_do_repositorio_recusa_projeto_desconhecido(self) -> None:
        # A mesma config.exemplo.toml VERSIONADA que o modo --simular
        # usa: um projeto que nao esta la tem de ser recusado, nao adivinhado.
        raiz = Path(__file__).resolve().parent.parent
        caminho_exemplo = raiz / "config.exemplo.toml"
        saida_erro = StringIO()
        stderr_original = sys.stderr
        sys.stderr = saida_erro
        try:
            codigo = main(
                [
                    "--config",
                    str(caminho_exemplo),
                    "--sem-voz",
                    "abrir-vscode",
                    "projeto-que-nao-existe",
                    "--simular",
                ]
            )
        finally:
            sys.stderr = stderr_original

        self.assertNotEqual(codigo, 0)
        self.assertIn("nao esta na configuracao", saida_erro.getvalue())


class _StreamFalsoSemDispositivo:
    """O minimo de `TextToAudioStream` que `jarvis.voz.falar()` toca.

    Nao abre dispositivo nenhum porque nao ha dispositivo nenhum: e um duplo
    (mesma convencao de tests/test_voz.py). So regista o que lhe pediram.
    """

    def __init__(self) -> None:
        self.chamadas_play: list[dict] = []

    def feed(self, texto: str) -> None:
        pass

    def play(self, **kwargs) -> None:
        self.chamadas_play.append(kwargs)


class TestCliSilenciosaPorOmissao(unittest.TestCase):
    """D61(1)(2): esta CLI e caminho de teste manual e passo do guiao de QA
    (`python -m jarvis.acoes_locais horas`), NAO "o jarvis a serio" — a unica
    isencao da D61 e `jarvis/app.py`. Sem `--com-som` nada toca: `falar()`
    recusa-se antes de construir o motor, por isso nenhum dispositivo de saida
    de audio chega sequer a ser considerado. Com `--com-som`, e so com ele, a
    reproducao e pedida (aqui ao duplo, nunca ao Piper nem ao PyAudio).
    """

    def _correr(self, argumentos: list[str], stream) -> tuple[int, str]:
        from unittest import mock

        from jarvis import voz

        self.addCleanup(voz.retomar_a_voz)
        saida = StringIO()
        stdout_original = sys.stdout
        sys.stdout = saida
        try:
            with mock.patch.object(voz, "_construir_stream", lambda: stream):
                codigo = main(argumentos)
        finally:
            sys.stdout = stdout_original
        return codigo, saida.getvalue()

    def test_horas_sem_flag_nao_abre_dispositivo_nenhum(self) -> None:
        stream = _StreamFalsoSemDispositivo()
        codigo, texto = self._correr(["horas"], stream)

        self.assertEqual(codigo, 0)
        self.assertEqual(
            stream.chamadas_play,
            [],
            "python -m jarvis.acoes_locais horas pediu reproducao sem --com-som (D61)",
        )
        self.assertIn("resposta =", texto)
        self.assertIn("voz recusada", texto)

    def test_com_som_e_a_unica_maneira_de_pedir_reproducao(self) -> None:
        stream = _StreamFalsoSemDispositivo()
        codigo, texto = self._correr(["--com-som", "horas"], stream)

        self.assertEqual(codigo, 0)
        self.assertEqual(stream.chamadas_play, [{"muted": False}])
        self.assertIn("resposta =", texto)

    def test_sem_voz_continua_a_nem_chamar_a_voz(self) -> None:
        stream = _StreamFalsoSemDispositivo()
        codigo, texto = self._correr(["--sem-voz", "horas"], stream)

        self.assertEqual(codigo, 0)
        self.assertEqual(stream.chamadas_play, [])
        self.assertIn("resposta =", texto)
        self.assertNotIn("voz recusada", texto)

    def test_a_ajuda_diz_numa_linha_que_sem_a_flag_nada_toca(self) -> None:
        saida = StringIO()
        stdout_original = sys.stdout
        sys.stdout = saida
        try:
            with self.assertRaises(SystemExit):
                main(["--help"])
        finally:
            sys.stdout = stdout_original
        texto = saida.getvalue()
        self.assertIn("--com-som", texto)
        self.assertIn("sem esta flag", texto)


class TestAcoesAdiadas(unittest.TestCase):
    """Calar, adormecer e acordar nao se executam aqui e o dizem, nunca
    fingem sucesso."""

    def test_as_tres_ficam_de_fora_desta_task(self) -> None:
        self.assertEqual(set(ACOES_ADIADAS), {"calar", "adormecer", "acordar"})

    def test_executar_recusa_cada_uma_com_razao_clara(self) -> None:
        config = Config(microfone="Microfone de Teste", projetos=())
        for nome_acao in ACOES_ADIADAS:
            with self.subTest(nome_acao=nome_acao):
                resultado_router = ResultadoRouter("local", nome_acao=nome_acao)
                with self.assertRaises(AcaoError) as ctx:
                    executar(resultado_router, config)
                self.assertIn("jarvis/app.py", str(ctx.exception))


class TestExecutarDespachaCorretamente(unittest.TestCase):
    """executar() e a ponte entre jarvis.router e as accoes: cobre o
    contrato inteiro, nao so o caminho feliz."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name)
        self.config = _config_com_pastas_reais(self.pasta)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_executar_recusa_resultado_que_nao_e_local(self) -> None:
        with self.assertRaises(AcaoError):
            executar(ResultadoRouter("claude", texto="ola"), self.config)
        with self.assertRaises(AcaoError):
            executar(ResultadoRouter("nada", motivo="ruido"), self.config)

    def test_executar_horas_e_data_nao_precisa_de_projeto(self) -> None:
        resultado = executar(ResultadoRouter("local", nome_acao="horas_e_data", argumento="horas"), self.config)
        self.assertIsInstance(resultado, ResultadoAcao)
        self.assertTrue(resultado.executou)

    def test_executar_abrir_vscode_simulado_identifica_o_projeto(self) -> None:
        projeto = self.config.encontrar_projeto("exemplo-dois")
        assert projeto is not None
        resultado_router = ResultadoRouter("local", nome_acao="abrir_vscode", argumento=str(projeto.caminho))
        resultado = executar(resultado_router, self.config, simular=True)
        self.assertEqual(resultado.projeto, "exemplo-dois")
        self.assertFalse(resultado.executou)

    def test_executar_acao_desconhecida_e_recusada(self) -> None:
        with self.assertRaises(AcaoError):
            executar(ResultadoRouter("local", nome_acao="formatar_disco"), self.config)


if __name__ == "__main__":
    unittest.main()
