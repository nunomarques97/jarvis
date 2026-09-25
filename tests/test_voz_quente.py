r"""Testes da voz residente e em streaming de jarvis/voz.py.

unittest da biblioteca padrao, mesma convencao de tests/test_voz.py e
tests/test_silencio.py (nenhuma framework de testes fora da biblioteca padrao).

O que estes testes protegem:

  * o motor de voz carrega UMA vez por processo, e nenhuma frase arranca um
    processo de sintese (`subprocess.Popen` nunca e chamado);
  * `aquecer()` deixa o motor carregado sem tocar som nem escrever ficheiro;
  * a voz segue a lingua das respostas: um texto portugues nunca chega ao
    motor ingles, mesmo instalado, e um Kokoro que falha a primeira sintese
    cai para o Piper no carregamento, sem deixar o jarvis sem voz;
  * streaming: o primeiro bloco de audio sai antes de a sintese acabar, e
    `ResultadoFala.primeiro_audio` marca esse instante;
  * `dividir_para_sintese()` nunca muda nem perde texto;
  * sem `com_som=True` a frase vai so para o WAV: o dispositivo de som nunca
    e tocado.

Quase tudo corre com motores e dispositivos FALSOS. So `TestMotorVerdadeiro`
carrega o modelo de voz real, e escreve para um WAV numa pasta ignorada pelo
Git (nunca para as colunas); salta se o modelo nao estiver em models/.

Corre com:

    .venv\Scripts\python -m unittest tests.test_voz_quente -v
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import voz
from jarvis.audio_util import RAIZ

PASTA_DOS_WAV = RAIZ / "tmp" / "_teste_voz_quente"


class MotorRapidoFalso:
    """Motor residente de mentira: um bloco de 100 ms por pedaco de texto."""

    nome = "falso"
    lingua = "pt"
    taxa = 16000
    descricao = "motor falso"

    def __init__(self) -> None:
        self.textos: list[str] = []

    def sintetizar(self, texto: str):
        self.textos.append(texto)
        for _pedaco in voz.dividir_para_sintese(texto):
            yield b"\x01\x00" * 1600


class MotorComSegundoBlocoPreso(MotorRapidoFalso):
    """O segundo bloco so sai quando o teste deixar: prova o streaming."""

    def __init__(self) -> None:
        super().__init__()
        self.pode_continuar = threading.Event()
        self.fim_da_sintese: float | None = None

    def sintetizar(self, texto: str):
        yield b"\x01\x00" * 1600
        self.pode_continuar.wait(timeout=5.0)
        yield b"\x01\x00" * 1600
        self.fim_da_sintese = time.perf_counter()


class SaidaQueNuncaPodeSerUsada:
    def escrever(self, taxa: int, dados: bytes) -> None:
        raise AssertionError("o dispositivo de som foi usado sem com_som=True")


def _popen_proibido(*args, **kwargs):
    raise AssertionError(f"a voz arrancou um processo: {args!r}")


class _BaseComMotorFalso(unittest.TestCase):
    def setUp(self) -> None:
        voz._esquecer_motor_residente()
        self.addCleanup(voz._esquecer_motor_residente)
        self.addCleanup(voz.retomar_a_voz)
        PASTA_DOS_WAV.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, PASTA_DOS_WAV, True)

    def _wav(self, nome: str) -> Path:
        return PASTA_DOS_WAV / f"{nome}.wav"


class TestMotorCarregaUmaVez(_BaseComMotorFalso):
    def test_varias_frases_carregam_o_motor_uma_so_vez_e_sem_processos(self) -> None:
        motor = MotorRapidoFalso()
        carregar = mock.Mock(return_value=motor)
        with mock.patch.object(voz, "_carregar_motor", carregar), mock.patch.object(
            subprocess, "Popen", _popen_proibido
        ), mock.patch.object(voz, "_SAIDA_DE_SOM", SaidaQueNuncaPodeSerUsada()):
            resultados = [
                voz.falar(f"frase numero {i}.", ficheiro=self._wav(f"frase-{i}")) for i in range(3)
            ]

        self.assertEqual(carregar.call_count, 1)
        self.assertTrue(all(r.falou for r in resultados), [r.motivo_falha for r in resultados])
        self.assertEqual(motor.textos, ["frase numero 0.", "frase numero 1.", "frase numero 2."])

    def test_aquecer_carrega_e_a_primeira_frase_ja_nao_carrega(self) -> None:
        motor = MotorRapidoFalso()
        carregar = mock.Mock(return_value=motor)
        with mock.patch.object(voz, "_carregar_motor", carregar), mock.patch.object(
            voz, "_SAIDA_DE_SOM", SaidaQueNuncaPodeSerUsada()
        ):
            descricao = voz.aquecer()
            self.assertEqual(carregar.call_count, 1)
            resultado = voz.falar("sao dez horas.", ficheiro=self._wav("depois-de-aquecer"))

        self.assertEqual(descricao, "motor falso")
        self.assertEqual(carregar.call_count, 1)
        self.assertTrue(resultado.falou, resultado.motivo_falha)

    def test_aquecer_sem_motor_levanta_para_o_arranque_avisar(self) -> None:
        falha = mock.Mock(side_effect=voz.MotorIndisponivel("sem modelo"))
        with mock.patch.object(voz, "_carregar_motor", falha):
            with self.assertRaises(voz.MotorIndisponivel):
                voz.aquecer()

    def test_sem_motor_falar_cai_para_texto_sem_levantar(self) -> None:
        falha = mock.Mock(side_effect=voz.MotorIndisponivel("sem modelo"))
        saida = io.StringIO()
        with mock.patch.object(voz, "_carregar_motor", falha), contextlib.redirect_stdout(saida):
            resultado = voz.falar("sao dez horas.", ficheiro=self._wav("sem-motor"))

        self.assertFalse(resultado.falou)
        self.assertIn("sem modelo", resultado.motivo_falha)
        self.assertIn("sao dez horas.", saida.getvalue())


class TestStreaming(_BaseComMotorFalso):
    def test_primeiro_bloco_sai_antes_de_a_sintese_acabar(self) -> None:
        motor = MotorComSegundoBlocoPreso()
        resultado: dict[str, voz.ResultadoFala] = {}

        def falar() -> None:
            resultado["fala"] = voz.falar("uma frase.", ficheiro=self._wav("streaming"))

        with mock.patch.object(voz, "_carregar_motor", mock.Mock(return_value=motor)):
            fio = threading.Thread(target=falar, daemon=True)
            fio.start()
            time.sleep(0.2)
            motor.pode_continuar.set()
            fio.join(timeout=5.0)

        self.assertFalse(fio.is_alive())
        fala = resultado["fala"]
        self.assertTrue(fala.falou, fala.motivo_falha)
        self.assertIsNotNone(fala.primeiro_audio)
        self.assertIsNotNone(motor.fim_da_sintese)
        self.assertLess(fala.primeiro_audio, motor.fim_da_sintese - 0.1)
        self.assertAlmostEqual(fala.duracao_s, 0.2, places=2)

    def test_com_som_toca_os_blocos_no_dispositivo(self) -> None:
        escritas: list[int] = []

        class SaidaContadora:
            def escrever(self, taxa: int, dados: bytes) -> None:
                escritas.append(len(dados))

        motor = MotorRapidoFalso()
        with mock.patch.object(voz, "_carregar_motor", mock.Mock(return_value=motor)):
            fala = voz.FalaResidente(voz.motor_residente(), SaidaContadora())
            fala.feed("uma. duas.")
            fala.play(muted=False)

        # dois blocos de 100 ms, escritos em fatias de BLOCO_DE_REPRODUCAO_S
        self.assertEqual(sum(escritas), 2 * 3200)
        self.assertEqual(len(escritas), 4)
        self.assertIsNotNone(fala.instante_do_primeiro_audio)

    def test_primeiro_audio_so_conta_depois_de_o_dispositivo_o_receber(self) -> None:
        # Na primeira fala, entregar o bloco inclui abrir o dispositivo: o
        # instante do primeiro audio nao pode ser anterior a essa entrega.
        fim_da_primeira_escrita: list[float] = []

        class SaidaLentaAAbrir:
            def escrever(self, taxa: int, dados: bytes) -> None:
                if not fim_da_primeira_escrita:
                    time.sleep(0.15)
                    fim_da_primeira_escrita.append(time.perf_counter())

        motor = MotorRapidoFalso()
        with mock.patch.object(voz, "_carregar_motor", mock.Mock(return_value=motor)):
            fala = voz.FalaResidente(voz.motor_residente(), SaidaLentaAAbrir())
            fala.feed("uma.")
            fala.play(muted=False)

        self.assertGreaterEqual(fala.instante_do_primeiro_audio, fim_da_primeira_escrita[0])

    def test_falha_do_motor_a_meio_cai_para_texto(self) -> None:
        class MotorQueParte(MotorRapidoFalso):
            def sintetizar(self, texto: str):
                yield b"\x01\x00" * 1600
                raise RuntimeError("inferencia partida")

        saida = io.StringIO()
        with mock.patch.object(
            voz, "_carregar_motor", mock.Mock(return_value=MotorQueParte())
        ), contextlib.redirect_stdout(saida):
            resultado = voz.falar("sao dez horas.", ficheiro=self._wav("partido"))

        self.assertFalse(resultado.falou)
        self.assertIn("inferencia partida", resultado.motivo_falha)


class _MotorPtFalso(MotorRapidoFalso):
    """Faz de Piper pt-PT: conta quantas vezes e construido."""

    nome = "piper"
    lingua = "pt"
    descricao = "piper falso"
    construidos: list["_MotorPtFalso"] = []

    def __init__(self) -> None:
        super().__init__()
        type(self).construidos.append(self)


class _MotorEnFalso(MotorRapidoFalso):
    """Faz de Kokoro: carrega bem e guarda todo o texto que lhe chega."""

    nome = "kokoro"
    lingua = "en"
    descricao = "kokoro falso"
    construidos: list["_MotorEnFalso"] = []

    def __init__(self) -> None:
        super().__init__()
        type(self).construidos.append(self)


class _MotorEnQueNaoSintetiza(_MotorEnFalso):
    """Um Kokoro que carrega mas falha a primeira sintese (API diferente)."""

    def sintetizar(self, texto: str):
        raise TypeError("create() got an unexpected keyword argument 'lang'")
        yield b""  # pragma: no cover - so para ser um gerador


class TestLinguaDaVoz(_BaseComMotorFalso):
    """A voz segue a lingua do texto, nunca o motor que esteja instalado."""

    def setUp(self) -> None:
        super().setUp()
        _MotorPtFalso.construidos = []
        _MotorEnFalso.construidos = []
        lingua_antes = voz.lingua_da_voz()
        self.addCleanup(voz.definir_lingua_da_voz, lingua_antes)
        for nome, falso in (("MotorPiperResidente", _MotorPtFalso), ("MotorKokoro", _MotorEnFalso)):
            remendo = mock.patch.object(voz, nome, falso)
            remendo.start()
            self.addCleanup(remendo.stop)

    def test_por_omissao_a_voz_e_a_portuguesa_das_respostas(self) -> None:
        self.assertEqual(voz.lingua_da_voz(), "pt")

    def test_resposta_portuguesa_nunca_chega_ao_motor_ingles_mesmo_instalado(self) -> None:
        # O Kokoro falso carregaria sem problema nenhum: mesmo assim, com a
        # lingua portuguesa, nunca e construido e nunca recebe texto.
        with mock.patch.object(voz, "_SAIDA_DE_SOM", SaidaQueNuncaPodeSerUsada()):
            resultado = voz.falar("São 10 horas e 5 minutos.", ficheiro=self._wav("pt"))
            descricao = voz.aquecer()

        self.assertTrue(resultado.falou, resultado.motivo_falha)
        self.assertEqual(_MotorEnFalso.construidos, [])
        self.assertEqual(len(_MotorPtFalso.construidos), 1)
        self.assertIn("São 10 horas e 5 minutos.", _MotorPtFalso.construidos[0].textos)
        self.assertEqual(descricao, "piper falso")

    def test_lingua_inglesa_usa_o_kokoro_e_a_portuguesa_continua_no_piper(self) -> None:
        voz.definir_lingua_da_voz("en")
        with mock.patch.object(voz, "_SAIDA_DE_SOM", SaidaQueNuncaPodeSerUsada()):
            ingles = voz.falar("It is ten past three.", ficheiro=self._wav("en"))
            voz.definir_lingua_da_voz("pt")
            portugues = voz.falar("São três e dez.", ficheiro=self._wav("pt-depois"))

        self.assertTrue(ingles.falou and portugues.falou)
        self.assertEqual(len(_MotorEnFalso.construidos), 1)
        self.assertIn("It is ten past three.", _MotorEnFalso.construidos[0].textos)
        self.assertNotIn("São três e dez.", _MotorEnFalso.construidos[0].textos)
        self.assertIn("São três e dez.", _MotorPtFalso.construidos[0].textos)
        # Um motor por lingua, cada um carregado uma so vez.
        self.assertIsNot(voz.motor_residente("pt"), voz.motor_residente("en"))
        self.assertEqual((len(_MotorPtFalso.construidos), len(_MotorEnFalso.construidos)), (1, 1))

    def test_kokoro_que_falha_a_primeira_sintese_cai_para_o_piper_no_arranque(self) -> None:
        voz.definir_lingua_da_voz("en")
        with mock.patch.object(voz, "MotorKokoro", _MotorEnQueNaoSintetiza), mock.patch.object(
            voz, "_SAIDA_DE_SOM", SaidaQueNuncaPodeSerUsada()
        ):
            descricao = voz.aquecer()
            resultado = voz.falar("It is ten past three.", ficheiro=self._wav("en-recurso"))

        self.assertIn("Kokoro indisponivel", descricao)
        self.assertIn("unexpected keyword argument", descricao)
        # O motor em cache e o que fala: a voz nao se perde nas frases seguintes.
        self.assertTrue(resultado.falou, resultado.motivo_falha)
        self.assertIsInstance(voz.motor_residente("en"), _MotorPtFalso)

    def test_lingua_desconhecida_e_recusada(self) -> None:
        with self.assertRaises(ValueError):
            voz.definir_lingua_da_voz("fr")
        self.assertEqual(voz.lingua_da_voz(), "pt")


class TestDividirParaSintese(unittest.TestCase):
    def test_nunca_muda_nem_perde_texto(self) -> None:
        textos = [
            "Sao dez e um quarto.",
            "O run do projeto acabou, com duas tasks feitas e uma bloqueada a espera de ti. Queres que leia o relatorio?",
            "It is ten past three!  Anything else?",
            "sem pontuacao nenhuma no fim",
        ]
        for texto in textos:
            with self.subTest(texto=texto):
                pedacos = voz.dividir_para_sintese(texto)
                self.assertEqual(" ".join(pedacos), " ".join(texto.split()))
                self.assertTrue(all(p.strip() for p in pedacos))

    def test_primeira_frase_comprida_parte_na_virgula(self) -> None:
        texto = "O run do projeto acabou, com duas tasks feitas e uma bloqueada a espera de ti. Fim."
        pedacos = voz.dividir_para_sintese(texto)
        self.assertEqual(pedacos[0], "O run do projeto acabou,")
        self.assertEqual(len(pedacos), 3)

    def test_frase_curta_nao_e_partida(self) -> None:
        self.assertEqual(voz.dividir_para_sintese("Sao dez, quase."), ["Sao dez, quase."])

    def test_vazio(self) -> None:
        self.assertEqual(voz.dividir_para_sintese(""), [])
        self.assertEqual(voz.dividir_para_sintese("   "), [])


def _motor_real_ou_motivo() -> str:
    if not voz.MODELO_ONNX.is_file() or not voz.CONFIG_ONNX.is_file():
        return "modelo de voz nao esta em models/ (ver docs/MODELOS.md)"
    try:
        import piper.voice  # noqa: F401
    except ImportError:
        return "biblioteca piper-tts nao instalada"
    return ""


_SEM_MOTOR_REAL = _motor_real_ou_motivo()


@unittest.skipIf(bool(_SEM_MOTOR_REAL), _SEM_MOTOR_REAL)
class TestMotorVerdadeiro(_BaseComMotorFalso):
    """O motor real, so para ficheiro: carrega uma vez e fala a lingua dele."""

    def test_duas_frases_um_carregamento_nenhum_processo(self) -> None:
        carregar_real = voz._carregar_motor
        carregar = mock.Mock(side_effect=carregar_real)
        with mock.patch.object(voz, "_carregar_motor", carregar), mock.patch.object(
            subprocess, "Popen", _popen_proibido
        ), mock.patch.object(voz, "_SAIDA_DE_SOM", SaidaQueNuncaPodeSerUsada()):
            primeira = voz.falar("Sao dez e um quarto.", ficheiro=self._wav("real-1"))
            antes = time.perf_counter()
            segunda = voz.falar("Sao tres e dez.", ficheiro=self._wav("real-2"))

        self.assertEqual(carregar.call_count, 1)
        for resultado in (primeira, segunda):
            self.assertTrue(resultado.falou, resultado.motivo_falha)
            self.assertGreater(resultado.duracao_s, 0.3)
        # Motor ja quente: o primeiro bloco da segunda frase chega bem depressa.
        # Margem larga para uma maquina carregada; a meta a serio (300 ms p50)
        # e verificada por scripts/medir_latencia_voz.py --verificar.
        self.assertLess(segunda.primeiro_audio - antes, 2.0)

    def test_a_voz_por_omissao_e_o_piper_portugues(self) -> None:
        # As respostas do jarvis sao escritas em portugues: a voz tem de ser a
        # portuguesa, esteja ou nao o Kokoro instalado.
        motor = voz.motor_residente()
        self.assertEqual((motor.nome, motor.lingua), ("piper", "pt"))

    def test_o_motor_ingles_fala_ingles_ou_diz_porque_nao(self) -> None:
        motor = voz.motor_residente("en")
        if motor.lingua != "en":
            self.assertIn("Kokoro indisponivel", motor.descricao)
        else:
            self.assertEqual(motor.nome, "kokoro")


if __name__ == "__main__":
    unittest.main()
