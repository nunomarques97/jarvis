r"""Testes do silencio imediato (T4, D60): o Ctrl+C, a saida do processo e o
"cala-te" calam a voz pelo MESMO mecanismo, e nada novo e falado a seguir.

unittest da biblioteca padrao, mesma convencao de tests/test_app.py e
tests/test_voz.py (nao ha decisao do Technology Scout para uma framework de
testes fora da biblioteca padrao).

NENHUM destes testes abre um dispositivo de audio, arranca o Piper ou toca em
GPU (D61/S13): o motor de voz e FALSO e o `subprocess.Popen` do wrapper e
substituido por um duplo. O que eles protegem:

  * o wrapper da S12 (`jarvis.voz.MorteDoPiper`): o processo fica guardado
    ANTES de `communicate()` bloquear — que e a diferenca toda entre poder e
    nao poder interromper uma sintese em curso — e `matar_agora()` mata-o
    mesmo, de outra thread;
  * a ORDEM obrigatoria de `calar_agora()`: `matar_agora()` primeiro,
    `stream.stop()` so depois (S12: parar a reproducao com o `piper.exe` vivo
    deixaria o `stop()` a espera da sintese);
  * as duas linhas de log com timestamps que a D60(4)(b) exige;
  * os TRES gatilhos a passarem pelo mesmo mecanismo: handler de Ctrl+C (antes
    de desligar o microfone, que demora segundos), saida do processo e a accao
    "calar" da lista branca da D4.d;
  * que depois de um Ctrl+C nada novo e falado — nem o resto da frase, nem uma
    despedida, nem a resposta do Claude Code que estivesse a chegar.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import logging
import queue
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import app, voz
from jarvis.app import EstadoDoProcesso, Jarvis, LogDaSessao, RegistoDaFrase
from jarvis.audio_util import PASTA_EVIDENCIA_FORJA, caminho_evidencia_de_saida
from jarvis.config import Config
from jarvis.voz import ResultadoFala

ESPERA_MAXIMA_S = 10.0


# --- duplos do Piper (nada disto arranca um processo a serio) ---------------


class VozFalsa:
    """O minimo de `PiperVoice` que o wrapper le."""

    model_file = "modelo-de-mentira.onnx"
    config_file = "modelo-de-mentira.onnx.json"


class BaseFalsaDoPiper:
    """O minimo do `PiperEngine` que o wrapper da S12 toca.

    Existe para o mixin `MorteDoPiper` ser testavel sem RealtimeTTS e sem
    Piper: os atributos sao os mesmos que a classe real da
    (`piper_engine.py` + `base_engine.py`).
    """

    def __init__(self, voice=None, piper_path="piper-de-mentira.exe", debug=False):
        self.voice = voice if voice is not None else VozFalsa()
        self.piper_path = piper_path
        self.debug = debug
        self.queue = queue.Queue()
        self.stop_synthesis_event = threading.Event()
        self._trim_silence_start_pending = False
        self.parou_na_base = False

    def stop(self):
        self.parou_na_base = True


class MotorDeTeste(voz.MorteDoPiper, BaseFalsaDoPiper):
    """A mesma composicao que `classe_do_motor_com_morte` faz com o real."""


class ProcessoFalso:
    """Um `subprocess.Popen` de mentira que se deixa matar.

    `communicate()` bloqueia como o verdadeiro bloqueia enquanto o `piper.exe`
    sintetiza, e so devolve quando alguem chamar `kill()` ou quando a "sintese"
    acabar sozinha.
    """

    criados: list["ProcessoFalso"] = []

    def __init__(self, cmd, stdin=None, stdout=None, stderr=None, shell=False):
        self.cmd = list(cmd)
        self.returncode = None
        self.matado = False
        self.entrou_no_communicate = threading.Event()
        self._acabar = threading.Event()
        self.handle_visivel_no_communicate = None
        self.ao_comunicar = None
        ProcessoFalso.criados.append(self)

    def acabar_sozinho(self) -> None:
        """Simula a sintese a terminar normalmente."""
        self._acabar.set()

    def communicate(self, input=None, timeout=None):
        self.entrou_no_communicate.set()
        if self.ao_comunicar is not None:
            self.handle_visivel_no_communicate = self.ao_comunicar()
        self._acabar.wait(timeout=ESPERA_MAXIMA_S)
        if self.matado:
            self.returncode = 1
            return b"", b"morto"
        self.returncode = 0
        return b"AUDIO-DE-MENTIRA", b""

    def kill(self):
        self.matado = True
        self.returncode = 1
        self._acabar.set()

    def poll(self):
        return self.returncode


class TestWrapperDoPiper(unittest.TestCase):
    """Criterio (1) da T4: o wrapper da S12, sem Piper nenhum."""

    def setUp(self) -> None:
        ProcessoFalso.criados.clear()
        self.motor = MotorDeTeste()
        self.remendo = mock.patch("jarvis.voz.subprocess.Popen", ProcessoFalso)
        self.remendo.start()
        self.addCleanup(self.remendo.stop)

    def _sintetizar_noutra_thread(self) -> tuple[threading.Thread, list[bool]]:
        devolvido: list[bool] = []

        def correr() -> None:
            devolvido.append(self.motor.synthesize("uma frase comprida qualquer"))

        thread = threading.Thread(target=correr, name="sintese-de-teste", daemon=True)
        thread.start()
        return thread, devolvido

    def test_o_comando_do_piper_e_o_mesmo_da_biblioteca(self) -> None:
        """O cmd_list tem de continuar a ser o do `piper_engine.py` (S12)."""
        thread, devolvido = self._sintetizar_noutra_thread()
        processo = self._esperar_pelo_processo()
        processo.acabar_sozinho()
        thread.join(timeout=ESPERA_MAXIMA_S)

        self.assertEqual(
            processo.cmd,
            [
                "piper-de-mentira.exe",
                "-m",
                "modelo-de-mentira.onnx",
                "--output-raw",
                "-c",
                "modelo-de-mentira.onnx.json",
                "--output-raw",
            ],
        )
        self.assertEqual(devolvido, [True])
        self.assertEqual(self.motor.queue.get_nowait(), b"AUDIO-DE-MENTIRA")

    def _esperar_pelo_processo(self) -> ProcessoFalso:
        fim = threading.Event()
        for _ in range(int(ESPERA_MAXIMA_S * 200)):
            if ProcessoFalso.criados and ProcessoFalso.criados[-1].entrou_no_communicate.wait(0.005):
                return ProcessoFalso.criados[-1]
            fim.wait(0.005)
        self.fail("o wrapper nunca chegou a arrancar o processo de sintese")

    def test_o_handle_fica_guardado_antes_do_communicate(self) -> None:
        """O achado critico da S12: sem isto, matar a sintese e impossivel."""
        visto: list[object] = []

        def espreitar() -> object:
            # Corre DENTRO do communicate(), ou seja, enquanto a "sintese"
            # ainda esta a decorrer: e neste instante que o Ctrl+C precisa de
            # encontrar o processo guardado.
            visto.append(self.motor._processo_atual)
            return self.motor._processo_atual

        original = ProcessoFalso.__init__

        def com_espia(self_processo, *args, **kwargs):
            original(self_processo, *args, **kwargs)
            self_processo.ao_comunicar = espreitar

        with mock.patch.object(ProcessoFalso, "__init__", com_espia):
            thread, devolvido = self._sintetizar_noutra_thread()
            processo = self._esperar_pelo_processo()
            processo.acabar_sozinho()
            thread.join(timeout=ESPERA_MAXIMA_S)

        self.assertEqual(visto, [processo])
        self.assertEqual(devolvido, [True])
        self.assertIsNone(self.motor._processo_atual, "o handle tem de ser largado no fim")

    def test_matar_agora_mata_o_processo_e_nada_vai_para_a_fila(self) -> None:
        """Criterio (4): o processo filho e mesmo morto e nada e falado."""
        thread, devolvido = self._sintetizar_noutra_thread()
        processo = self._esperar_pelo_processo()

        self.assertTrue(self.motor.matar_agora(), "matar_agora() tinha um processo para matar")
        thread.join(timeout=ESPERA_MAXIMA_S)

        self.assertFalse(thread.is_alive(), "a sintese ficou pendurada depois do kill")
        self.assertTrue(processo.matado)
        self.assertEqual(devolvido, [False])
        self.assertTrue(self.motor.queue.empty(), "audio de uma sintese morta chegou a fila")

    def test_depois_de_matar_nao_comeca_frase_nenhuma_nova(self) -> None:
        """D60(1): nem o resto da frase, nem uma despedida."""
        self.motor.matar_agora()
        quantos_antes = len(ProcessoFalso.criados)

        self.assertFalse(self.motor.synthesize("a frase seguinte"))
        self.assertEqual(len(ProcessoFalso.criados), quantos_antes, "arrancou um piper novo")

    def test_matar_agora_sem_nada_a_matar_nao_levanta(self) -> None:
        self.assertFalse(self.motor.matar_agora())

    def test_o_stop_do_motor_tambem_mata_a_sintese(self) -> None:
        """`stream.stop()` chama `engine.stop()`: aqui isso mata mesmo (S12)."""
        thread, devolvido = self._sintetizar_noutra_thread()
        processo = self._esperar_pelo_processo()

        self.motor.stop()
        thread.join(timeout=ESPERA_MAXIMA_S)

        self.assertTrue(processo.matado)
        self.assertTrue(self.motor.parou_na_base, "o stop() da classe-base tem de correr na mesma")
        self.assertEqual(devolvido, [False])

    def test_o_pior_caso_da_s12_cabe_na_fasquia_dos_500_ms(self) -> None:
        """Achado 3 da S12, o buraco todo: uma sintese VIVA a meio.

        O `ProcessoFalso` desta frase nunca acaba sozinho — e o equivalente a
        um `piper.exe` que ainda ia a meio da frase. Sem o wrapper, o
        `stream.stop()` ficava a espera dele (medido no caminho real: 2102 ms,
        ver docs/forja/evidence/silencio-*.md). Aqui mede-se o caminho
        completo de `calar_agora()` — `matar_agora()` e so depois `stop()` —
        contra a fasquia da D60, sem Piper, sem audio e sem GPU (D61).
        """
        stream = StreamFalso()
        stream.engine = self.motor  # o motor REAL da S12, nao o duplo de voz
        voz._guardar_voz_ativa(stream)
        self.addCleanup(voz._guardar_voz_ativa, None)
        self.addCleanup(voz.retomar_a_voz)

        thread, devolvido = self._sintetizar_noutra_thread()
        processo = self._esperar_pelo_processo()
        self.assertFalse(processo.matado, "a sintese tem de estar viva quando o pedido chega")

        linhas: list[str] = []
        resultado = voz.calar_agora("Ctrl+C (D30/D60)", definitivo=True, registar=linhas.append)
        thread.join(timeout=ESPERA_MAXIMA_S)

        self.assertTrue(processo.matado, "o processo filho ficou vivo")
        self.assertFalse(thread.is_alive(), "a sintese ficou pendurada: o buraco da S12 continua")
        self.assertEqual(devolvido, [False], "uma sintese morta nunca devolve sucesso")
        self.assertTrue(self.motor.queue.empty(), "audio de uma sintese morta chegou a fila")
        self.assertLessEqual(
            resultado.intervalo_ms,
            voz.LIMITE_DE_SILENCIO_MS,
            f"calar demorou {resultado.intervalo_ms:.0f} ms com a sintese viva",
        )
        self.assertEqual(len(linhas), 2, linhas)


# --- duplos do stream de voz ------------------------------------------------


class MotorFalsoDeVoz:
    """Um motor de voz com a mesma superficie que `calar_agora()` usa."""

    def __init__(self, registo: list[str]) -> None:
        self.registo = registo
        self.matou = 0
        self._calado = False

    def matar_agora(self) -> bool:
        self.registo.append("matar_agora")
        self.matou += 1
        self._calado = True
        return True


class StreamFalso:
    """Um `TextToAudioStream` de mentira: nunca abre dispositivo de audio."""

    def __init__(self, registo: list[str] | None = None) -> None:
        self.registo = registo if registo is not None else []
        self.engine = MotorFalsoDeVoz(self.registo)
        self.a_tocar = threading.Event()
        self._parar = threading.Event()
        self.alimentado: list[str] = []
        self.tocou_com = None

    def feed(self, texto: str) -> None:
        self.alimentado.append(texto)

    def play(self, **kwargs) -> None:
        self.tocou_com = kwargs
        self.a_tocar.set()
        self._parar.wait(timeout=ESPERA_MAXIMA_S)

    def stop(self) -> None:
        self.registo.append("stop")
        self._parar.set()


class TestCalarAgora(unittest.TestCase):
    """Criterio (2): um so mecanismo, com a ordem da S12 e as duas linhas."""

    def setUp(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        # Nenhum teste deixa uma "voz ativa" pendurada para o seguinte.
        self.addCleanup(voz._guardar_voz_ativa, None)

    def test_a_ordem_e_matar_a_sintese_e_so_depois_parar_a_reproducao(self) -> None:
        stream = StreamFalso()
        voz._guardar_voz_ativa(stream)

        resultado = voz.calar_agora("teste", definitivo=False)

        self.assertEqual(stream.registo, ["matar_agora", "stop"])
        self.assertTrue(resultado.matou_sintese)
        self.assertTrue(resultado.parou_reproducao)
        self.assertEqual(resultado.erro, "")

    def test_escreve_as_duas_linhas_com_o_intervalo_em_ms(self) -> None:
        """D60(4)(b): pedido e fim do audio, para o intervalo se ler em ms."""
        stream = StreamFalso()
        voz._guardar_voz_ativa(stream)
        linhas: list[str] = []

        resultado = voz.calar_agora("Ctrl+C (D30/D60)", definitivo=True, registar=linhas.append)

        self.assertEqual(len(linhas), 2, linhas)
        self.assertIn("SILENCIO pedido (Ctrl+C (D30/D60))", linhas[0])
        self.assertIn("SILENCIO fim do audio (Ctrl+C (D30/D60))", linhas[1])
        self.assertIn("ms desde o pedido", linhas[1])
        self.assertIn("sintese morta=sim", linhas[1])
        self.assertLessEqual(resultado.intervalo_ms, voz.LIMITE_DE_SILENCIO_MS)
        self.assertTrue(resultado.dentro_do_limite)

    def test_definitivo_cala_o_modulo_e_retomar_desfaz(self) -> None:
        self.assertFalse(voz.esta_calado())
        voz.calar_agora("Ctrl+C", definitivo=True)
        self.assertTrue(voz.esta_calado())
        voz.retomar_a_voz()
        self.assertFalse(voz.esta_calado())

    def test_nao_definitivo_nao_cala_o_futuro(self) -> None:
        voz.calar_agora("cala-te", definitivo=False)
        self.assertFalse(voz.esta_calado())

    def test_calar_sem_ninguem_a_falar_nao_levanta(self) -> None:
        resultado = voz.calar_agora("saida do processo", definitivo=True)
        self.assertFalse(resultado.matou_sintese)
        self.assertFalse(resultado.parou_reproducao)
        self.assertEqual(resultado.erro, "")

    def test_um_motor_que_rebenta_nao_impede_o_silencio(self) -> None:
        class MotorMauFeitio(MotorFalsoDeVoz):
            def matar_agora(self):
                raise RuntimeError("o motor rebentou a morrer")

        stream = StreamFalso()
        stream.engine = MotorMauFeitio(stream.registo)
        voz._guardar_voz_ativa(stream)

        resultado = voz.calar_agora("teste", definitivo=False)

        self.assertIn("o motor rebentou a morrer", resultado.erro)
        self.assertTrue(resultado.parou_reproducao, "a reproducao tem de parar na mesma")


class TestFalarObedeceAoSilencio(unittest.TestCase):
    """Criterio (3) e (4) do lado de `falar()`, com motor de voz FALSO."""

    def setUp(self) -> None:
        self.addCleanup(voz.retomar_a_voz)

    def test_calar_a_meio_da_frase_corta_e_devolve_o_motivo(self) -> None:
        stream = StreamFalso()
        construidos: list[StreamFalso] = []

        def construir_falso():
            construidos.append(stream)
            return stream

        resultados: list[ResultadoFala] = []
        with mock.patch.object(voz, "_construir_stream", construir_falso):
            thread = threading.Thread(
                target=lambda: resultados.append(voz.falar("uma frase a meio", com_som=True)),
                name="fala-de-teste",
                daemon=True,
            )
            thread.start()
            self.assertTrue(stream.a_tocar.wait(ESPERA_MAXIMA_S), "a fala nunca comecou")

            silencio = voz.calar_agora("Ctrl+C (D30/D60)", definitivo=True)
            thread.join(timeout=ESPERA_MAXIMA_S)

            # Nada NOVO e falado a seguir: `falar()` nem chega a construir um
            # stream, por isso o motor de voz nunca e acordado outra vez.
            # com_som=True so para chegar ao guarda `esta_calado()` desta
            # frase (D60); o guarda da D61 (opt-in) e testado a parte.
            depois = voz.falar("adeus, ate a proxima", com_som=True)

        self.assertFalse(thread.is_alive(), "a fala ficou pendurada depois do silencio")
        self.assertEqual(stream.registo, ["matar_agora", "stop"])
        self.assertTrue(silencio.matou_sintese)
        self.assertEqual(len(resultados), 1)
        self.assertFalse(resultados[0].falou)
        self.assertEqual(resultados[0].motivo_falha, voz.MOTIVO_SILENCIADO)
        self.assertFalse(depois.falou)
        self.assertEqual(depois.motivo_falha, voz.MOTIVO_SILENCIADO)
        self.assertEqual(len(construidos), 1, "construiu um stream novo depois de calado")

    def test_cala_te_corta_a_frase_sem_calar_o_jarvis_para_sempre(self) -> None:
        """O gatilho "cala-te": corta a frase em curso e o resultado di-lo."""
        stream = StreamFalso()
        resultados: list[ResultadoFala] = []
        with mock.patch.object(voz, "_construir_stream", lambda: stream):
            thread = threading.Thread(
                target=lambda: resultados.append(voz.falar("uma frase a meio", com_som=True)),
                name="fala-de-teste",
                daemon=True,
            )
            thread.start()
            self.assertTrue(stream.a_tocar.wait(ESPERA_MAXIMA_S), "a fala nunca comecou")
            voz.calar_agora("cala-te (lista branca D4.d)", definitivo=False)
            thread.join(timeout=ESPERA_MAXIMA_S)

        self.assertEqual(len(resultados), 1)
        self.assertFalse(resultados[0].falou, "uma frase cortada a meio nao e uma frase dita")
        self.assertEqual(resultados[0].motivo_falha, voz.MOTIVO_SILENCIADO)
        self.assertFalse(voz.esta_calado(), "cala-te nao cala o jarvis para sempre")

    def test_ctrl_c_a_subir_por_dentro_do_falar_mata_antes_de_desenrolar(self) -> None:
        """O `KeyboardInterrupt` passa por `falar()` antes do handler do app.

        Se `falar()` se limitasse a deixa-lo passar, o seu `finally` largava o
        registo da voz ativa e o handler la em cima ja nao tinha o `piper.exe`
        a mao para matar: ficava a acabar a frase sozinho.
        """
        stream = StreamFalso()

        def play_interrompido(**kwargs):
            raise KeyboardInterrupt

        stream.play = play_interrompido
        with mock.patch.object(voz, "_construir_stream", lambda: stream):
            with self.assertRaises(KeyboardInterrupt):
                voz.falar("uma frase interrompida", com_som=True)

        self.assertEqual(stream.engine.matou, 1, "a sintese tinha de ser morta ali mesmo")
        self.assertTrue(voz.esta_calado())
        self.assertIsNone(voz._stream_ativo)

    def test_a_voz_ativa_e_largada_no_fim_de_cada_frase(self) -> None:
        stream = StreamFalso()
        stream._parar.set()  # esta frase toca e acaba sozinha
        with mock.patch.object(voz, "_construir_stream", lambda: stream):
            voz.falar("uma frase normal", com_som=True)
        self.assertIsNone(voz._stream_ativo)


# --- os tres gatilhos, do lado do processo ---------------------------------


class LogFalso:
    """Guarda as linhas em vez de as escrever (igual ao de test_app.py)."""

    def __init__(self) -> None:
        self.linhas: list[str] = []
        self.caminho = Path("logs") / "log-de-teste.log"
        self.fechado = False

    def linha(self, texto: str) -> str:
        self.linhas.append(texto)
        return texto

    def bruto(self, texto: str = "") -> None:
        self.linhas.append(texto)

    def fechar(self) -> None:
        self.fechado = True

    def texto(self) -> str:
        return "\n".join(self.linhas)


class EspiaDoSilencio:
    """Chama o mecanismo a serio e regista quem o chamou e com que argumentos."""

    def __init__(self, ordem: list[str] | None = None) -> None:
        self.chamadas: list[tuple[str, bool, bool]] = []
        self.ordem = ordem if ordem is not None else []

    def __call__(self, motivo="", *, definitivo=False, registar=None):
        self.chamadas.append((motivo, definitivo, registar is not None))
        self.ordem.append("calar")
        return voz.calar_agora(motivo, definitivo=definitivo, registar=registar)


class GravadorFalso:
    """Um `AudioToTextRecorder` de mentira: sem microfone e sem GPU."""

    device = "cpu-de-mentira"

    def __init__(self, ordem: list[str]) -> None:
        self.ordem = ordem
        self.desligado = False

    def text(self):
        raise KeyboardInterrupt

    def shutdown(self) -> None:
        self.ordem.append("shutdown")
        self.desligado = True


def _jarvis_de_teste(espia: EspiaDoSilencio, log: LogFalso, falas: list[str]) -> Jarvis:
    def falar_falso(texto, **kwargs):
        falas.append(texto)
        return ResultadoFala(falou=True)

    return Jarvis(
        Config(microfone="Microfone Ficticio de Teste", projetos=()),
        log,
        estado=EstadoDoProcesso(),
        falar=falar_falso,
        executar=lambda *a, **k: None,
        abrir_canal=lambda: None,
        calar=espia,
    )


class TestOsTresGatilhos(unittest.TestCase):
    """Criterio (2) e (3) da T4, do lado do processo: sem microfone, sem voz."""

    def setUp(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        self.log = LogFalso()
        self.ordem: list[str] = []
        self.espia = EspiaDoSilencio(self.ordem)
        self.falas: list[str] = []
        self.jarvis = _jarvis_de_teste(self.espia, self.log, self.falas)

    def test_gatilho_cala_te_da_lista_branca(self) -> None:
        """D4.d + D60(2): "cala-te" cala JA, nao so as respostas futuras."""
        resposta = self.jarvis._accao_de_estado("calar")

        self.assertEqual(resposta, "Fico calado.")
        self.assertTrue(self.jarvis.estado.mudo, "o estado para o futuro mantem-se")
        self.assertEqual(self.espia.chamadas, [("cala-te (lista branca D4.d)", False, True)])
        self.assertIn("SILENCIO pedido (cala-te (lista branca D4.d))", self.log.texto())
        self.assertIn("SILENCIO fim do audio", self.log.texto())
        self.assertFalse(voz.esta_calado(), "cala-te nao pode calar o jarvis para sempre")

    def test_gatilho_ctrl_c_cala_antes_de_desligar_o_microfone(self) -> None:
        gravador = GravadorFalso(self.ordem)
        with mock.patch.object(app, "construir_recorder", lambda **k: gravador):
            codigo = app.correr_microfone(self.jarvis)

        self.assertEqual(codigo, 0)
        self.assertEqual(self.espia.chamadas, [("Ctrl+C (D30/D60)", True, True)])
        self.assertEqual(
            self.ordem,
            ["calar", "shutdown"],
            "calar tem de vir ANTES do shutdown do gravador, que demora segundos",
        )
        self.assertTrue(gravador.desligado)
        self.assertTrue(voz.esta_calado(), "depois do Ctrl+C a voz fica calada")

    def test_gatilho_saida_do_processo(self) -> None:
        """A saida do processo cala pelo mesmo mecanismo, com log e atexit."""
        registados: list[tuple] = []
        log = LogFalso()

        with mock.patch.object(app, "LogDaSessao", lambda *a, **k: log), mock.patch.object(
            app, "correr_microfone", lambda *a, **k: 0
        ), mock.patch.object(app.atexit, "register", lambda f, *a, **k: registados.append((f, a, k))):
            codigo = app.main(["--config", "config-que-nao-existe.toml", "--sem-voz"])

        self.assertEqual(codigo, 0)
        texto = log.texto()
        self.assertIn("SILENCIO pedido (saida do processo)", texto)
        self.assertIn("SILENCIO fim do audio (saida do processo)", texto)
        self.assertLess(
            texto.index("SILENCIO fim do audio"),
            texto.index("jarvis terminado"),
            "o silencio tem de acontecer antes de o processo se dar por terminado",
        )
        self.assertTrue(log.fechado)
        self.assertEqual(
            [(f, a, k) for f, a, k in registados],
            [(voz.calar_agora, ("saida do processo (atexit)",), {"definitivo": True})],
            "falta a rede do atexit: um piper.exe podia ficar vivo",
        )

    def test_o_processo_instala_e_repoe_o_handler_de_ctrl_c(self) -> None:
        """O handler de sinal cala no instante mais cedo possivel (D60)."""
        log = LogFalso()
        handler_antes = signal.getsignal(signal.SIGINT)
        apanhado: list[str] = []

        def correr_falso(*a, **k):
            handler = signal.getsignal(signal.SIGINT)
            self.assertNotEqual(handler, handler_antes, "o handler de Ctrl+C nao foi instalado")
            try:
                handler(signal.SIGINT, None)
            except KeyboardInterrupt:
                apanhado.append("KeyboardInterrupt")
            return 0

        with mock.patch.object(app, "LogDaSessao", lambda *a, **k: log), mock.patch.object(
            app, "correr_microfone", correr_falso
        ), mock.patch.object(app.atexit, "register", lambda *a, **k: None):
            codigo = app.main(["--config", "config-que-nao-existe.toml", "--sem-voz"])

        self.assertEqual(codigo, 0)
        self.assertEqual(apanhado, ["KeyboardInterrupt"], "o handler tem de levantar na mesma")
        self.assertIn("SILENCIO pedido (Ctrl+C (D30/D60))", log.texto())
        self.assertEqual(
            signal.getsignal(signal.SIGINT), handler_antes, "o handler anterior tem de voltar"
        )

    def test_depois_do_ctrl_c_nada_novo_e_falado(self) -> None:
        """D60(1): nem o resto da frase, nem despedida, nem a resposta que chegou."""
        registo = RegistoDaFrase(numero=1, log=self.log)
        voz.calar_agora("Ctrl+C (D30/D60)", definitivo=True)

        self.jarvis.responder("Resposta do Claude Code, nao verificada: ola.", registo)

        self.assertEqual(self.falas, [], "falou depois de lhe terem mandado calar")
        self.assertIn("silenciado a pedido (D60)", self.log.texto())

    def test_sem_ctrl_c_a_voz_continua_a_falar(self) -> None:
        """A rede de seguranca ao contrario: isto nao pode emudecer o jarvis."""
        registo = RegistoDaFrase(numero=1, log=self.log)

        self.jarvis.responder("sao quinze e trinta", registo)

        self.assertEqual(self.falas, ["sao quinze e trinta"])


class TestRuidoDeTerceirosAoCalar(unittest.TestCase):
    """O RealtimeTTS chama "failed ... unknown error" a uma ordem cumprida.

    Mesma regra da T12: descarta-se SO o ruido conhecido e SO durante a janela
    do silenciamento; tudo o resto passa intacto.
    """

    def test_o_ruido_conhecido_e_descartado_e_o_resto_passa(self) -> None:
        with self.assertLogs(level="WARNING") as apanhado:
            with voz._sem_ruido_de_quem_foi_calado():
                logging.warning(
                    'engine piper failed to synthesize sentence "ola", unknown error'
                )
                logging.warning("No playback thread found, cannot stop playback")
                logging.warning("isto e um aviso a serio e tem de aparecer")

        self.assertEqual(
            [registo.getMessage() for registo in apanhado.records],
            ["isto e um aviso a serio e tem de aparecer"],
        )

    def test_o_filtro_nao_fica_no_logger_raiz(self) -> None:
        filtros_antes = list(logging.getLogger().filters)
        with voz._sem_ruido_de_quem_foi_calado():
            pass
        self.assertEqual(list(logging.getLogger().filters), filtros_antes)


class TestNadaAbreDispositivoDeAudio(unittest.TestCase):
    """Rede de seguranca da D61 para ESTE ficheiro: nada aqui toca som."""

    def test_o_falar_real_nunca_e_chamado_sem_motor_falso(self) -> None:
        """`voz.falar` so corre nestes testes com `_construir_stream` trocado.

        Se algum teste deste ficheiro chamasse o caminho real, arrancaria o
        `piper.exe` e abriria as colunas do Sponsor. Esta verificacao afirma o
        contrario pelo lado observavel: com o modulo calado, `falar()` recusa
        antes de construir seja o que for.
        """
        self.addCleanup(voz.retomar_a_voz)
        voz.calar_agora("teste", definitivo=True)

        chamou: list[int] = []
        with mock.patch.object(voz, "_construir_stream", lambda: chamou.append(1)):
            # com_som=True: para isolar o guarda da D60 (esta_calado) do
            # guarda da D61 (opt-in), testado a parte em tests/test_voz.py.
            resultado = voz.falar("isto nunca pode chegar as colunas", com_som=True)

        self.assertEqual(chamou, [])
        self.assertFalse(resultado.falou)
        self.assertEqual(resultado.motivo_falha, voz.MOTIVO_SILENCIADO)


class TestTrancaDoLogEhReentrante(unittest.TestCase):
    """Bloqueador 1 do Security Reviewer (T4-a1-security.md, item 2):
    `LogDaSessao._escrever` tomava um `threading.Lock` (nao reentrante). Um
    SIGINT corre na thread principal entre bytecodes; se cair enquanto essa
    MESMA thread esta dentro de `_escrever` (a tranca ja detida), o handler de
    Ctrl+C chama `jarvis.calar_agora()` -> `voz.calar_agora()` ->
    `log.linha()` -> `_escrever()` de novo, na mesma thread, ANTES de matar o
    `piper.exe` — com `Lock` isso e um deadlock; com `RLock` a mesma thread
    reentra sem bloquear.
    """

    def setUp(self) -> None:
        self._pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self._pasta.cleanup)
        self.log = LogDaSessao(pasta=Path(self._pasta.name), consola=io.StringIO())
        self.addCleanup(self.log.fechar)

    def test_a_tranca_e_um_rlock(self) -> None:
        """Trava a decisao no tipo: uma regressao para `threading.Lock()`
        parte este teste mesmo que, por sorte de escalonamento, o teste de
        reentrada a seguir nao chegue a bloquear."""
        self.assertIsInstance(
            self.log._tranca, type(threading.RLock()),
            "LogDaSessao._tranca tem de ser threading.RLock (bloqueador 1, T4/T12)",
        )

    def test_reentrar_a_tranca_na_mesma_thread_nao_bloqueia(self) -> None:
        """A prova direta, no cenario exato do SIGINT: escrever com a tranca
        ja detida PELA MESMA thread tem de devolver, nunca ficar pendurado.
        Corre num watchdog para o teste nunca ficar preso mesmo que a
        correcao regrida."""
        resultado: dict[str, str] = {}
        excecoes: list[BaseException] = []

        def reentrar() -> None:
            try:
                with self.log._tranca:  # como se estivesse a meio de _escrever
                    resultado["linha"] = self.log.linha("reentrada dentro da propria tranca")
            except BaseException as erro:  # noqa: BLE001 - queremos ver qualquer falha
                excecoes.append(erro)

        watchdog = threading.Thread(target=reentrar, daemon=True)
        watchdog.start()
        watchdog.join(timeout=ESPERA_MAXIMA_S)

        self.assertFalse(
            watchdog.is_alive(),
            "deadlock: a tranca do log nao e reentrante na mesma thread (bloqueador 1)",
        )
        self.assertEqual(excecoes, [])
        self.assertIn("reentrada dentro da propria tranca", resultado.get("linha", ""))


class TestCtrlCNaoBloqueiaComATrancaDoLogDetida(unittest.TestCase):
    """O cenario de producao completo do bloqueador 1: `Jarvis.calar_agora`
    (chamado pelo handler de Ctrl+C, `app.py:1324`) escreve no MESMO log que,
    no pior caso, a propria thread principal ja tem detido porque o SIGINT
    caiu a meio de `LogDaSessao._escrever`.
    """

    def setUp(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        self._pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self._pasta.cleanup)
        self.log = LogDaSessao(pasta=Path(self._pasta.name), consola=io.StringIO())
        self.addCleanup(self.log.fechar)
        self.jarvis = Jarvis(
            Config(microfone="Microfone Ficticio de Teste", projetos=()),
            self.log,
            estado=EstadoDoProcesso(),
            falar=lambda *a, **k: ResultadoFala(falou=True),
            executar=lambda *a, **k: None,
            abrir_canal=lambda: None,
            com_voz=False,
        )

    def test_ctrl_c_fecha_sem_bloquear_com_a_tranca_do_log_detida(self) -> None:
        resultado: dict[str, object] = {}

        def como_se_o_sigint_caisse_a_meio_de_escrever() -> None:
            with self.log._tranca:
                resultado["silencio"] = self.jarvis.calar_agora(
                    "Ctrl+C (D30/D60)", definitivo=True
                )

        watchdog = threading.Thread(
            target=como_se_o_sigint_caisse_a_meio_de_escrever, daemon=True
        )
        marca = _instante_ms()
        watchdog.start()
        watchdog.join(timeout=ESPERA_MAXIMA_S)
        decorrido_ms = _instante_ms() - marca

        self.assertFalse(
            watchdog.is_alive(),
            "deadlock: Ctrl+C nao devolveu com a tranca do log ja detida (bloqueador 1)",
        )
        self.assertIn("silencio", resultado, "calar_agora nunca chegou a devolver")
        self.assertLess(
            decorrido_ms, ESPERA_MAXIMA_S * 1000,
            "Ctrl+C demorou a fasquia toda do watchdog: sinal de bloqueio, nao de sucesso",
        )
        texto_do_log = self.log.consola.getvalue()
        self.assertIn("SILENCIO pedido (Ctrl+C (D30/D60))", texto_do_log)
        self.assertIn("SILENCIO fim do audio (Ctrl+C (D30/D60))", texto_do_log)
        self.assertTrue(voz.esta_calado())


def _instante_ms() -> float:
    return time.perf_counter() * 1000.0


class TestConfinamentoDaEvidencia(unittest.TestCase):
    """Bloqueador 2 do Security Reviewer (T4-a1-security.md, item 1):
    `--evidencia` aceitava qualquer caminho vindo da linha de comandos e
    escrevia fora do repositorio (ou na raiz, nao apanhada pelo
    `.gitignore`). `jarvis.audio_util.caminho_evidencia_de_saida` confina a
    `docs/forja/evidence/`, mesma forma de `caminho_wav_de_saida`.
    """

    def test_aceita_um_relativo_a_raiz_que_cai_dentro_da_pasta_permitida(self) -> None:
        """Um relativo conta a partir da RAIZ do repositorio (mesma regra do
        --saida de scripts/medir_voz.py), por isso tem de vir com o prefixo
        docs/forja/evidence/ para ser aceite."""
        destino = caminho_evidencia_de_saida("docs/forja/evidence/silencio-teste.md")
        self.assertEqual(destino, (PASTA_EVIDENCIA_FORJA / "silencio-teste.md").resolve())
        self.assertTrue(destino.is_relative_to(PASTA_EVIDENCIA_FORJA.resolve()))

    def test_aceita_um_absoluto_dentro_da_pasta_permitida(self) -> None:
        alvo = PASTA_EVIDENCIA_FORJA / "silencio-absoluto-teste.md"
        destino = caminho_evidencia_de_saida(str(alvo))
        self.assertEqual(destino, alvo.resolve())

    def test_recusa_travessia_de_pastas(self) -> None:
        with self.assertRaises(ValueError):
            caminho_evidencia_de_saida("../../fora-do-repo.md")

    def test_recusa_caminho_absoluto_fora_da_pasta_permitida(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            alvo = Path(pasta).resolve() / "fuga.md"
            with self.assertRaises(ValueError):
                caminho_evidencia_de_saida(str(alvo))
            self.assertFalse(alvo.exists(), "a guarda nao pode criar nada fora da pasta")

    def test_recusa_relativo_que_cai_na_raiz_do_repo_nao_ignorada(self) -> None:
        """`--evidencia notas.md` cairia em `<raiz>/notas.md`: dentro do
        repositorio mas FORA de `docs/forja/evidence/`, e essa raiz nao e
        apanhada por regra nenhuma do `.gitignore` (ataque (b) do Security
        Reviewer)."""
        with self.assertRaises(ValueError):
            caminho_evidencia_de_saida("notas.md")

    def test_recusa_relativo_dentro_de_docs_mas_fora_de_evidence(self) -> None:
        with self.assertRaises(ValueError):
            caminho_evidencia_de_saida("docs/notas.md")

    def test_recusa_sufixo_diferente_de_md(self) -> None:
        with self.assertRaises(ValueError):
            caminho_evidencia_de_saida("docs/forja/evidence/fuga.txt")

    @unittest.skipUnless(hasattr(Path, "is_symlink"), "Path.is_symlink sempre existe")
    def test_recusa_symlink_que_aponta_para_fora_da_pasta_permitida(self) -> None:
        """Windows: uma ligacao simbolica (se o utilizador tiver privilegio
        para a criar) nao pode contornar a guarda — `resolve()` segue-a antes
        da verificacao de confinamento."""
        with tempfile.TemporaryDirectory() as pasta_fora:
            alvo_real = Path(pasta_fora).resolve() / "fuga-verdadeira.md"
            alvo_real.write_text("fora do repo", encoding="utf-8")
            ligacao = PASTA_EVIDENCIA_FORJA / "ligacao-de-teste.md"
            try:
                ligacao.symlink_to(alvo_real)
            except (OSError, NotImplementedError):
                self.skipTest("sem privilegio para criar symlinks nesta maquina/conta")
            try:
                with self.assertRaises(ValueError):
                    caminho_evidencia_de_saida(str(ligacao))
            finally:
                ligacao.unlink(missing_ok=True)

    def test_prova_de_silencio_recusa_e_nao_escreve_nada_fora_da_pasta(self) -> None:
        """De ponta a ponta, pelo `--evidencia` real: um caminho fora da
        pasta permitida devolve 1 e nao toca no disco, ANTES de gastar tempo
        a sintetizar seja o que for (sem Piper e sem GPU envolvidos aqui,
        porque a guarda corre antes de qualquer sintese)."""
        with tempfile.TemporaryDirectory() as pasta:
            alvo = Path(pasta).resolve() / "fuga.md"
            saida, erro = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erro):
                codigo = voz._prova_de_silencio(str(alvo))

            self.assertEqual(codigo, 1)
            self.assertFalse(alvo.exists())
            self.assertIn("confinamento do --evidencia", erro.getvalue())


if __name__ == "__main__":
    unittest.main()
