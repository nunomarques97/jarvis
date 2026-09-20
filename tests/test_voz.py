r"""Testes de jarvis/voz.py, unittest da biblioteca padrao (sem pytest, mesma
convencao de tests/test_router.py e tests/test_app.py: nao ha decisao do
Technology Scout para uma framework de testes fora da biblioteca padrao).

Este ficheiro cobre o ponto 1 da T12 (QA-close-1.md, finding 4a) e o teste de
guarda da D61/S13 (T5).

T12: o import tardio do RealtimeTTS em `_construir_stream()` fica dentro de um
`warnings.catch_warnings()` que ignora SO o `RuntimeWarning` do `pydub.utils`
(o aviso de ffmpeg em falta), e `warnings.filters` volta EXATAMENTE ao estado
anterior depois do import — lista inteira, nao so as entradas do pydub. A
comparacao tem de ser da lista inteira porque e isso que apanha o contrario do
que a task pede: um `warnings.filterwarnings("ignore")` global passaria numa
comparacao so das entradas do pydub.

T5/D61 (`TestGuardaDaD61`): `falar()` sem `ficheiro=` e sem `com_som=True` e um
erro (nunca chega a construir o motor, nunca chega perto de um dispositivo de
audio); com `ficheiro=` e sem `com_som`, escreve com `muted=True`; `com_som=True`
e a UNICA maneira de pedir `muted=False`, com ou sem `ficheiro=` — tudo provado
com um stream falso, sem Piper nem PyAudio. E o guarda do silencio (D60(1))
corre ANTES do da D61: calado, nem o recurso em texto e impresso.

Nao toca em GPU, em Piper nem em audio real: so o comportamento do import, do
estado dos filtros de warnings e do contrato de `falar()`, que corre em
qualquer maquina com o venv instalado (RealtimeTTS ja e dependencia decidida,
TECHNOLOGY.md S1/S4).

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from jarvis import voz
from jarvis.audio_util import escrever_wav_pcm16


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


class TestGuardaDaD61(unittest.TestCase):
    """Teste de guarda da D61(3)/S13: `falar()` nunca abre um dispositivo de
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
        # stdout capturado: a recusa imprime o texto como recurso (D35.4) e
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
        """A flag quer dizer a mesma coisa em todo o repositorio (D61(2)): com
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
