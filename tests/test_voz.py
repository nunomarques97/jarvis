r"""Testes de jarvis/voz.py, unittest da biblioteca padrao (sem pytest, mesma
convencao de tests/test_router.py e tests/test_app.py: nenhuma framework de
testes fora da biblioteca padrao).

Este ficheiro cobre os avisos do carregamento do motor e o teste de guarda
do opt-in de som.

O carregamento do motor de voz (`_carregar_motor()`) fica dentro de um
`warnings.catch_warnings()`: o que as bibliotecas de sintese registam em
`warnings.filters` ao carregar nao sobrevive, e a lista volta EXATAMENTE ao
estado anterior — lista inteira, porque e isso que apanha um
`warnings.filterwarnings("ignore")` global.

Opt-in de som (`TestGuardaDoOptInDeSom`): `falar()` sem `ficheiro=` e sem `com_som=True` e um
erro (nunca chega a construir o motor, nunca chega perto de um dispositivo de
audio); com `ficheiro=` e sem `com_som`, escreve com `muted=True`; `com_som=True`
e a UNICA maneira de pedir `muted=False`, com ou sem `ficheiro=` — tudo provado
com um stream falso, sem Piper nem PyAudio. E o guarda do silencio
corre ANTES do do opt-in: calado, nem o recurso em texto e impresso.

Nao toca em GPU, em modelos de voz nem em audio real: so o estado dos filtros
de warnings e o contrato de `falar()`, com motores e streams falsos.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from jarvis import voz
from jarvis.audio_util import escrever_wav_pcm16


class _MotorQueSujaOsAvisos:
    """Motor falso que, ao carregar, faz o que uma biblioteca de sintese faz:
    regista um filtro global em `warnings.filters`."""

    nome = "falso"
    lingua = "pt"
    taxa = 16000
    descricao = "motor falso"

    def __init__(self) -> None:
        warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"pydub\.utils")
        warnings.filterwarnings("ignore")

    def sintetizar(self, texto: str):
        yield b"\x00\x00"


class TestCarregarMotorNaoMexeNosAvisos(unittest.TestCase):
    """O que as bibliotecas de sintese registam em `warnings.filters` ao
    carregar fica dentro de `_carregar_motor()` — nada de filtro global."""

    def _carregar_com_motor_sujo(self) -> object:
        sem_kokoro = mock.Mock(side_effect=voz.MotorIndisponivel("sem kokoro"))
        with mock.patch.object(voz, "MotorKokoro", sem_kokoro), mock.patch.object(
            voz, "MotorPiperResidente", _MotorQueSujaOsAvisos
        ):
            return voz._carregar_motor("en")

    def test_warnings_filters_volta_ao_estado_anterior(self) -> None:
        """A lista INTEIRA, nao so as entradas do pydub: um
        `warnings.filterwarnings("ignore")` global tambem tem de desaparecer."""
        filtros_antes = list(warnings.filters)

        motor = self._carregar_com_motor_sujo()

        self.assertIsInstance(motor, _MotorQueSujaOsAvisos, "o motor falso nao chegou a carregar")
        self.assertEqual(list(warnings.filters), filtros_antes)

    def test_sem_kokoro_o_motivo_fica_na_descricao(self) -> None:
        motor = self._carregar_com_motor_sujo()

        self.assertIn("Kokoro indisponivel: sem kokoro", motor.descricao)

    def test_kokoro_partido_ao_carregar_cai_para_a_voz_pt(self) -> None:
        """Um Kokoro instalado mas partido (DLL em falta, API diferente) nao
        pode deixar o jarvis sem voz nenhuma."""
        partido = mock.Mock(side_effect=OSError("DLL em falta"))
        with mock.patch.object(voz, "MotorKokoro", partido), mock.patch.object(
            voz, "MotorPiperResidente", _MotorQueSujaOsAvisos
        ):
            motor = voz._carregar_motor("en")

        self.assertIsInstance(motor, _MotorQueSujaOsAvisos)
        self.assertIn("Kokoro indisponivel: DLL em falta", motor.descricao)


class _StreamFalsoParaGuarda:
    """O minimo de `TextToAudioStream` que `falar()` toca, sem Piper nem PyAudio.

    Escreve um WAV minimo valido quando `output_wavfile` e pedido (para
    `_duracao_do_wav` nao rebentar) e nunca abre nenhum dispositivo de audio —
    e um duplo, nao ha dispositivo nenhum para abrir. Mesma convencao dos
    duplos de tests/test_silencio.py.
    """

    def __init__(self) -> None:
        self.alimentado: list[str] = []
        self.chamadas_play: list[dict] = []

    def feed(self, texto: str) -> None:
        self.alimentado.append(texto)

    def play(self, **kwargs) -> None:
        self.chamadas_play.append(kwargs)
        caminho = kwargs.get("output_wavfile")
        if caminho:
            escrever_wav_pcm16(Path(caminho), b"\x01\x00" * 8000, 16000, 1)


class TestGuardaDoOptInDeSom(unittest.TestCase):
    """Teste de guarda: `falar()` nunca abre um dispositivo de
    audio sem opt-in explicito, e o caminho de ficheiro nunca muda isso.

    As DUAS metades que a D61(3) exige, literalmente: sem `ficheiro=` e sem
    `com_som=True`, `falar()` e um ERRO (nunca chega a `_construir_stream`,
    logo nunca chega perto de um dispositivo de audio); com `ficheiro=` e sem
    opt-in, escreve o WAV com `muted=True` e nunca com `muted=False`. So
    `com_som=True` pede `muted=False`, com ou sem ficheiro. Nenhum destes
    testes toca em Piper, GPU nem PyAudio — o "dispositivo" e so o duplo
    acima, por isso a suite completa continua a correr em silencio.
    """

    def setUp(self) -> None:
        self.addCleanup(voz.retomar_a_voz)

    def test_sem_ficheiro_e_sem_com_som_e_recusado_antes_de_construir(self) -> None:
        chamou_construir: list[int] = []
        # stdout capturado: a recusa imprime o texto como recurso e
        # isso nao pode sujar a saida da suite.
        saida = io.StringIO()
        with mock.patch.object(voz, "_construir_stream", lambda: chamou_construir.append(1)):
            with contextlib.redirect_stdout(saida):
                resultado = voz.falar("frase qualquer sem opt-in")

        self.assertEqual(
            chamou_construir, [], "falar() construiu o motor sem ficheiro= nem com_som=True"
        )
        self.assertFalse(resultado.falou)
        self.assertEqual(resultado.motivo_falha, voz.MOTIVO_SEM_OPT_IN)
        self.assertIn("frase qualquer sem opt-in", saida.getvalue())

    def test_com_ficheiro_escreve_com_muted_true_e_nunca_abre_dispositivo(self) -> None:
        stream = _StreamFalsoParaGuarda()
        caminho_relativo = Path("audio") / "_teste_guarda_d61_t5.wav"
        caminho_absoluto = voz.RAIZ / caminho_relativo
        self.addCleanup(lambda: caminho_absoluto.unlink(missing_ok=True))

        with mock.patch.object(voz, "_construir_stream", lambda: stream):
            resultado = voz.falar("frase de teste do ficheiro", ficheiro=str(caminho_relativo))

        self.assertTrue(resultado.falou)
        self.assertEqual(len(stream.chamadas_play), 1)
        self.assertEqual(stream.chamadas_play[0].get("muted"), True)
        self.assertIn("output_wavfile", stream.chamadas_play[0])
        self.assertTrue(caminho_absoluto.is_file())

    def test_com_ficheiro_e_com_som_toca_e_grava(self) -> None:
        """A flag quer dizer a mesma coisa em todo o repositorio: com
        `com_som=True` sai som, tambem quando ha `ficheiro=` — o WAV continua a
        ser escrito, exatamente como `scripts/gerar_wav.py --com-som`."""
        stream = _StreamFalsoParaGuarda()
        caminho_relativo = Path("audio") / "_teste_guarda_d61_t5_com_som.wav"
        caminho_absoluto = voz.RAIZ / caminho_relativo
        self.addCleanup(lambda: caminho_absoluto.unlink(missing_ok=True))

        with mock.patch.object(voz, "_construir_stream", lambda: stream):
            resultado = voz.falar(
                "frase gravada e ouvida", ficheiro=str(caminho_relativo), com_som=True
            )

        self.assertTrue(resultado.falou)
        self.assertEqual(len(stream.chamadas_play), 1)
        self.assertEqual(stream.chamadas_play[0].get("muted"), False)
        self.assertIn("output_wavfile", stream.chamadas_play[0])
        self.assertTrue(caminho_absoluto.is_file())

    def test_calado_ganha_ao_guarda_da_d61_e_nao_imprime_nada(self) -> None:
        """D60(1) a montante da D61: depois de um Ctrl+C, uma chamada sem
        opt-in nem sequer escreve o recurso em texto na consola que o silencio
        mandou calar — e continua a nao construir motor nenhum."""
        chamou_construir: list[int] = []
        voz.calar_agora("teste: Ctrl+C", definitivo=True)
        saida = io.StringIO()
        with mock.patch.object(voz, "_construir_stream", lambda: chamou_construir.append(1)):
            with contextlib.redirect_stdout(saida):
                resultado = voz.falar("isto nao pode aparecer na consola")

        self.assertEqual(saida.getvalue(), "")
        self.assertEqual(chamou_construir, [])
        self.assertFalse(resultado.falou)
        self.assertEqual(resultado.motivo_falha, voz.MOTIVO_SILENCIADO)

    def test_com_som_true_sem_ficheiro_e_o_unico_caminho_que_pede_muted_false(self) -> None:
        """O opt-in inverso: com `com_som=True` e sem `ficheiro`, `falar()`
        chega mesmo a construir o stream e pede-lhe `muted=False`, e nao
        escreve ficheiro nenhum (aqui o "dispositivo" e so o duplo)."""
        stream = _StreamFalsoParaGuarda()
        with mock.patch.object(voz, "_construir_stream", lambda: stream):
            resultado = voz.falar("frase com opt-in", com_som=True)

        self.assertTrue(resultado.falou)
        self.assertEqual(stream.chamadas_play, [{"muted": False}])


if __name__ == "__main__":
    unittest.main()
