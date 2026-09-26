r"""Testes do ouvido residente (jarvis/ouvido.py), unittest da biblioteca padrao.

Nenhum teste abre o microfone, carrega um modelo ou toca som: a fonte, a
tecla, o detetor da palavra de ativacao, o VAD e o motor de transcricao sao
falsos. Cada chunk falso e um byte repetido, por isso da para provar byte a
byte que audio chegou (ou nao) ao motor.

Corre com:

    .venv\Scripts\python -m unittest tests.test_ouvido -v
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from jarvis import ouvido as ouvido_mod
from jarvis.audio_util import escrever_wav_pcm16
from jarvis.config import TECLAS_DE_FALAR, ConfigError, ConfigOuvido, carregar_config
from jarvis.ouvido import (
    BYTES_POR_CHUNK,
    DURACAO_DO_CHUNK_S,
    ESPERA_PELA_FALA_S,
    FRASES_EM_ESPERA,
    GATILHO_ATIVACAO,
    GATILHO_TECLA,
    MAXIMO_DA_FRASE_S,
    META_SINAL_MS,
    SILENCIO_FINAL_S,
    SURDEZ_APOS_ATIVACAO_S,
    FonteDeFicheiro,
    Frase,
    MicrofonePyAudio,
    Ouvido,
    TeclaDoFicheiro,
    TeclaWindows,
    chunks_do_pcm,
    medir,
    pcm_do_wav,
    percentil,
)
from jarvis.stt import MotorBase, MotorParakeet

ATIVACAO = 0x7A
SILENCIO = 0x00


def chunk(valor: int) -> bytes:
    return bytes([valor]) * BYTES_POR_CHUNK


def chunks_em(segundos: float) -> int:
    return round(segundos / DURACAO_DO_CHUNK_S)


class MotorFalso(MotorBase):
    """Guarda cada PCM que lhe chega (o silencio do aquecimento a parte)."""

    nome = "motor-falso"

    def __init__(self, texto: str | None = None, falhar: bool = False) -> None:
        super().__init__("cpu")
        self.texto = texto
        self.falhar = falhar
        self.recebidos: list[bytes] = []
        self.aquecimentos = 0
        self.carregamentos = 0
        self.linguas: list[str | None] = []

    def _carregar_modelo(self):
        self.carregamentos += 1
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        if not any(pcm16):
            self.aquecimentos += 1
            return "", lingua, False
        self.recebidos.append(pcm16)
        self.linguas.append(lingua)
        if self.falhar:
            raise RuntimeError("falha de proposito")
        texto = self.texto if self.texto is not None else f"frase de {len(pcm16) // BYTES_POR_CHUNK} chunks"
        return texto, lingua, False


class DetetorFalso:
    """Dispara com o chunk marcado; conta os reinicios e o que viu."""

    def __init__(self) -> None:
        self.reinicios = 0
        self.vistos: list[int] = []

    def processar(self, pedaco: bytes) -> float:
        self.vistos.append(pedaco[0])
        return 0.9 if pedaco[0] == ATIVACAO else 0.1

    def reiniciar(self) -> None:
        self.reinicios += 1


class VadFalso:
    def e_fala(self, pedaco: bytes) -> bool:
        return pedaco[0] != SILENCIO


class TeclaFixa:
    def __init__(self, premida: bool = False) -> None:
        self.estado = premida
        self.outra = False

    def premida(self) -> bool:
        return self.estado

    def outra_tecla_premida(self) -> bool:
        return self.outra


class FonteSemFim:
    """Uma fonte que nunca acaba (como um microfone), para testar `parar()`."""

    descricao = "fonte sem fim"

    def __init__(self, valor: int = 0x44) -> None:
        self.valor = valor
        self.aberta = False
        self.fechada = False

    def abrir(self) -> None:
        self.aberta = True

    def ler(self) -> bytes:
        threading.Event().wait(0.001)
        return chunk(self.valor)

    def fechar(self) -> None:
        self.fechada = True


class FonteQueNaoAbre(FonteSemFim):
    def abrir(self) -> None:
        raise OSError("microfone ocupado")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.linhas: list[str] = []
        self.bips: list[str] = []
        self.frases: list[Frase] = []

    def ouvido(self, motor: MotorBase, **kwargs) -> Ouvido:
        opcoes = dict(escrever=self.linhas.append, tocar=self.bips.append)
        opcoes.update(kwargs)
        if "tecla" not in opcoes and "detetor" not in opcoes:
            opcoes["tecla"] = TeclaFixa()
        return Ouvido(FonteDeFicheiro(b""), motor, self.frases.append, **opcoes)

    @staticmethod
    def alimentar(ouvido: Ouvido, valor: int, n: int, premida: bool = False) -> None:
        for _ in range(n):
            ouvido.processar(chunk(valor), premida)


class TestTeclaDeFalar(Base):
    def test_so_o_audio_com_a_tecla_premida_chega_ao_motor(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        self.alimentar(ouvido, 0x11, 10)  # antes
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x33, 10)  # o chunk do soltar e depois
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x22) * 20])
        self.assertEqual(len(self.frases), 1)
        self.assertEqual(self.frases[0].gatilho, GATILHO_TECLA)
        self.assertIsNone(self.frases[0].score_ativacao)

    def test_audio_fora_da_tecla_nunca_chega_ao_callback(self) -> None:
        """Negativo: sem tecla premida (e sem maos-livres) nada e transcrito nem entregue."""
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        self.alimentar(ouvido, 0x44, chunks_em(10.0))
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])
        self.assertEqual(self.frases, [])
        self.assertEqual(ouvido.estado, "repouso")

    def test_duas_frases_seguidas_nao_se_misturam(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        self.alimentar(ouvido, 0x21, 10, premida=True)
        self.alimentar(ouvido, 0x44, 5)
        self.alimentar(ouvido, 0x23, 12, premida=True)
        self.alimentar(ouvido, 0x44, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x21) * 10, chunk(0x23) * 12])

    def test_toque_curto_e_descartado(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        self.alimentar(ouvido, 0x22, 3, premida=True)  # 90 ms
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])
        self.assertEqual(ouvido.descartadas, 1)
        self.assertTrue(any("acidental" in linha for linha in self.linhas))

    def test_frase_no_maximo_fecha_e_o_resto_ate_soltar_e_deitado_fora(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        maximo = chunks_em(MAXIMO_DA_FRASE_S)
        self.alimentar(ouvido, 0x22, maximo, premida=True)
        self.alimentar(ouvido, 0x66, 10, premida=True)  # ainda premida, depois do teto
        self.assertEqual(ouvido.estado, "espera_soltar")
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(len(motor.recebidos), 1)
        self.assertNotIn(bytes([0x66]), motor.recebidos[0])
        self.assertEqual(ouvido.estado, "repouso")

    def test_atalho_com_a_tecla_de_falar_e_descartado(self) -> None:
        """Ctrl+C com a tecla de falar nao e uma frase: nada chega ao motor."""
        motor = MotorFalso()
        tecla = TeclaFixa()
        ouvido = self.ouvido(motor, tecla=tecla)
        self.alimentar(ouvido, 0x22, 5, premida=True)
        tecla.outra = True
        self.alimentar(ouvido, 0x22, 2, premida=True)
        tecla.outra = False
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])
        self.assertTrue(any("atalho de teclado" in linha for linha in self.linhas))
        # A pressao seguinte, sem atalho, volta a contar.
        self.alimentar(ouvido, 0x23, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x23) * 20])

    def test_tecla_ignorada_se_o_ouvido_nao_tem_tecla(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor, detetor=DetetorFalso(), vad=VadFalso())
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])

    def test_a_lingua_configurada_chega_ao_motor(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor, lingua="en")
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.linguas, ["en"])
        self.assertEqual(self.frases[0].lingua, "en")


class TestMaosLivres(Base):
    def ouvido_maos_livres(self, motor: MotorBase, **kwargs) -> tuple[Ouvido, DetetorFalso]:
        detetor = DetetorFalso()
        return self.ouvido(motor, detetor=detetor, vad=VadFalso(), **kwargs), detetor

    def test_ativacao_e_vad_entregam_uma_frase(self) -> None:
        motor = MotorFalso()
        ouvido, _ = self.ouvido_maos_livres(motor)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, 20)
        self.alimentar(ouvido, SILENCIO, chunks_em(SILENCIO_FINAL_S))
        ouvido.transcrever_pendentes()
        self.assertEqual(len(self.frases), 1)
        self.assertEqual(self.frases[0].gatilho, GATILHO_ATIVACAO)
        self.assertAlmostEqual(self.frases[0].score_ativacao, 0.9)
        # O chunk da ativacao nao entra; a frase e a fala e o silencio final.
        self.assertNotIn(bytes([ATIVACAO]), motor.recebidos[0])
        self.assertEqual(motor.recebidos[0][:BYTES_POR_CHUNK], chunk(0x55))

    def test_fala_sem_ativacao_nunca_chega_ao_motor(self) -> None:
        """Negativo: ruido e fala sem palavra de ativacao so passam pelo detetor."""
        motor = MotorFalso()
        ouvido, detetor = self.ouvido_maos_livres(motor)
        self.alimentar(ouvido, 0x44, chunks_em(20.0))
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])
        self.assertEqual(self.frases, [])
        self.assertEqual(len(detetor.vistos), chunks_em(20.0))

    def test_audio_antes_da_ativacao_nao_entra_na_frase(self) -> None:
        motor = MotorFalso()
        ouvido, _ = self.ouvido_maos_livres(motor)
        self.alimentar(ouvido, 0x44, 50)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, 20)
        self.alimentar(ouvido, SILENCIO, chunks_em(SILENCIO_FINAL_S))
        ouvido.transcrever_pendentes()
        self.assertEqual(len(motor.recebidos), 1)
        self.assertNotIn(bytes([0x44]), motor.recebidos[0])

    def test_depois_da_frase_o_audio_volta_a_ser_descartado(self) -> None:
        motor = MotorFalso()
        ouvido, detetor = self.ouvido_maos_livres(motor)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, 20)
        self.alimentar(ouvido, SILENCIO, chunks_em(SILENCIO_FINAL_S))
        self.alimentar(ouvido, 0x66, 100)
        ouvido.transcrever_pendentes()
        self.assertEqual(len(motor.recebidos), 1)
        self.assertNotIn(bytes([0x66]), motor.recebidos[0])
        self.assertGreaterEqual(detetor.reinicios, 1)

    def test_tecla_em_repouso_nao_reinicia_o_detetor_antes_do_sinal(self) -> None:
        """O reinicio do detetor e lento: nao pode atrasar o sinal de inicio."""
        motor = MotorFalso()
        ouvido, detetor = self.ouvido_maos_livres(motor, tecla=TeclaFixa())
        self.alimentar(ouvido, 0x44, 10)
        ouvido.processar(chunk(0x22), True)
        self.assertEqual(detetor.reinicios, 0)
        self.assertTrue(any("A OUVIR" in linha for linha in self.linhas))
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        self.assertEqual(detetor.reinicios, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x22) * 21])

    def test_tecla_a_meio_de_uma_ativacao_descarta_e_reinicia(self) -> None:
        motor = MotorFalso()
        ouvido, detetor = self.ouvido_maos_livres(motor, tecla=TeclaFixa())
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, 5)
        ouvido.processar(chunk(0x22), True)
        self.assertEqual(detetor.reinicios, 1)
        self.assertEqual(ouvido.estado, "tecla")
        self.assertTrue(any("a meio de uma escuta" in linha for linha in self.linhas))
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x22) * 21])

    def test_ativacao_seguida_de_silencio_e_descartada(self) -> None:
        motor = MotorFalso()
        ouvido, _ = self.ouvido_maos_livres(motor)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, SILENCIO, chunks_em(ESPERA_PELA_FALA_S) + 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])
        self.assertEqual(ouvido.estado, "repouso")
        self.assertTrue(any("seguida de silencio" in linha for linha in self.linhas))

    def test_ativacao_seguida_de_silencio_vai_ao_callback_sem_transcrever(self) -> None:
        motor = MotorFalso()
        so_ativacao: list[Frase] = []
        ouvido, _ = self.ouvido_maos_livres(motor, ao_ativar_sem_fala=so_ativacao.append)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, SILENCIO, chunks_em(ESPERA_PELA_FALA_S) + 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [], "nada a transcrever")
        self.assertEqual(self.frases, [], "nao e uma frase ouvida")
        self.assertEqual(len(so_ativacao), 1)
        self.assertEqual(so_ativacao[0].texto, "")
        self.assertEqual(so_ativacao[0].gatilho, GATILHO_ATIVACAO)
        self.assertEqual(so_ativacao[0].score_ativacao, 0.9)
        self.assertEqual(ouvido.estado, "repouso")

    def test_janela_sem_resposta_nunca_vai_ao_callback_da_ativacao(self) -> None:
        motor = MotorFalso()
        so_ativacao: list[Frase] = []
        ouvido, _ = self.ouvido_maos_livres(motor, ao_ativar_sem_fala=so_ativacao.append)
        self.assertTrue(ouvido.abrir_escuta(2.0))
        self.alimentar(ouvido, SILENCIO, chunks_em(2.0) + 2)
        ouvido.transcrever_pendentes()
        self.assertEqual(so_ativacao, [])
        self.assertEqual(self.frases, [])

    def test_o_fim_da_palavra_logo_apos_a_ativacao_nao_abre_a_fala(self) -> None:
        motor = MotorFalso()
        ouvido, _ = self.ouvido_maos_livres(motor)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, chunks_em(SURDEZ_APOS_ATIVACAO_S) - 1)
        self.assertEqual(ouvido.estado, "ativado")
        self.alimentar(ouvido, SILENCIO, chunks_em(ESPERA_PELA_FALA_S))
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [])

    def test_uma_pausa_curta_nao_fecha_a_frase(self) -> None:
        motor = MotorFalso()
        ouvido, _ = self.ouvido_maos_livres(motor)
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, 20)
        self.alimentar(ouvido, SILENCIO, chunks_em(SILENCIO_FINAL_S) - 3)
        self.alimentar(ouvido, 0x56, 10)
        self.alimentar(ouvido, SILENCIO, chunks_em(SILENCIO_FINAL_S))
        ouvido.transcrever_pendentes()
        self.assertEqual(len(motor.recebidos), 1)
        self.assertIn(bytes([0x56]), motor.recebidos[0])

    def test_tecla_a_meio_da_escuta_por_ativacao_descarta_essa_escuta(self) -> None:
        motor = MotorFalso()
        ouvido, _ = self.ouvido_maos_livres(motor, tecla=TeclaFixa())
        ouvido.processar(chunk(ATIVACAO), False)
        self.alimentar(ouvido, 0x55, 10)
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, SILENCIO, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x22) * 20])
        self.assertEqual(self.frases[0].gatilho, GATILHO_TECLA)

    def test_maos_livres_sem_vad_e_recusado(self) -> None:
        with self.assertRaises(ValueError):
            Ouvido(FonteDeFicheiro(b""), MotorFalso(), print, detetor=DetetorFalso())

    def test_sem_gatilho_nenhum_e_recusado(self) -> None:
        with self.assertRaises(ValueError):
            Ouvido(FonteDeFicheiro(b""), MotorFalso(), print)


class TestSinais(Base):
    def test_inicio_e_fim_escrevem_uma_linha_cada(self) -> None:
        ouvido = self.ouvido(MotorFalso())
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.assertTrue(self.linhas[-1].startswith("ouvido | >>> A OUVIR"))
        self.alimentar(ouvido, 0x00, 1)
        self.assertTrue(self.linhas[-1].startswith("ouvido | <<< FIM DA ESCUTA"))

    def test_sinal_dentro_de_150_ms_da_transicao(self) -> None:
        ouvido = self.ouvido(MotorFalso())
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        self.assertEqual(len(ouvido.latencias_do_sinal_ms), 2)
        self.assertLessEqual(max(ouvido.latencias_do_sinal_ms), META_SINAL_MS)

    def test_o_sinal_sai_no_chunk_da_transicao_e_nao_espera_pela_transcricao(self) -> None:
        """O fim de escuta e dado antes de o motor correr (a transcricao e noutra fila)."""
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        self.assertTrue(any("FIM DA ESCUTA" in linha for linha in self.linhas))
        self.assertEqual(motor.recebidos, [])

    def test_sem_com_som_nao_ha_bip(self) -> None:
        ouvido = self.ouvido(MotorFalso())
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        self.assertEqual(self.bips, [])

    def test_com_som_bip_no_inicio_e_no_fim(self) -> None:
        ouvido = self.ouvido(MotorFalso(), com_som=True)
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        self.assertEqual(self.bips, ["inicio", "fim"])

    def test_bip_que_falha_nao_para_a_escuta(self) -> None:
        def tocar_mal(_tipo: str) -> None:
            raise RuntimeError("sem dispositivo")

        motor = MotorFalso()
        ouvido = self.ouvido(motor, com_som=True, tocar=tocar_mal)
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(len(self.frases), 1)

    def test_o_bip_por_omissao_nunca_toca_sem_com_som(self) -> None:
        """Rede de seguranca: o `tocar` real (winsound) nao e chamado sem --com-som."""
        winsound_falso = mock.Mock()
        with mock.patch.dict(sys.modules, {"winsound": winsound_falso}):
            ouvido = Ouvido(
                FonteDeFicheiro(b""), MotorFalso(), self.frases.append, tecla=TeclaFixa(), escrever=self.linhas.append
            )
            self.alimentar(ouvido, 0x22, 10, premida=True)
            self.alimentar(ouvido, 0x00, 1)
        winsound_falso.Beep.assert_not_called()


class TestTranscricao(Base):
    def test_preparar_carrega_uma_vez_e_aquece(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        ouvido.preparar()
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.carregamentos, 1)
        self.assertEqual(motor.aquecimentos, 1)
        self.assertEqual(len(motor.recebidos), 1)

    def test_latencia_medida_do_soltar_ao_texto(self) -> None:
        tempos = iter([10.0, 10.0, 12.0, 12.0, 12.4])
        ouvido = self.ouvido(MotorFalso(), relogio=lambda: next(tempos))
        ouvido.processar(chunk(0x22), True)  # 10.0 transicao, 10.0 sinal
        for _ in range(9):
            ouvido._buffer.append(chunk(0x22))
        ouvido.processar(chunk(0x00), False)  # 12.0 transicao, 12.0 sinal
        ouvido.transcrever_pendentes()  # 12.4 texto pronto
        frase = self.frases[0]
        self.assertEqual((frase.inicio_da_escuta, frase.fim_da_escuta), (10.0, 12.0))
        self.assertAlmostEqual(frase.ms_do_fim_ao_texto, 400.0)

    def test_motor_que_falha_nao_entrega_e_continua(self) -> None:
        motor = MotorFalso(falhar=True)
        ouvido = self.ouvido(motor)
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(self.frases, [])
        self.assertTrue(any("transcricao falhou" in linha for linha in self.linhas))
        motor.falhar = False
        self.alimentar(ouvido, 0x23, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(len(self.frases), 1)

    def test_texto_vazio_nao_e_entregue(self) -> None:
        ouvido = self.ouvido(MotorFalso(texto=""))
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(self.frases, [])

    def test_callback_que_falha_nao_derruba_o_ouvido(self) -> None:
        def rebenta(_frase: Frase) -> None:
            raise RuntimeError("encaminhador partido")

        motor = MotorFalso()
        ouvido = Ouvido(FonteDeFicheiro(b""), motor, rebenta, tecla=TeclaFixa(), escrever=self.linhas.append)
        self.alimentar(ouvido, 0x22, 10, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        self.assertEqual(ouvido.transcrever_pendentes(), 1)
        self.assertTrue(any("tratamento da frase falhou" in linha for linha in self.linhas))

    def test_fila_cheia_descarta_a_frase_nova(self) -> None:
        motor = MotorFalso()
        ouvido = self.ouvido(motor)
        for indice in range(FRASES_EM_ESPERA + 1):
            self.alimentar(ouvido, 0x30 + indice, 10, premida=True)
            self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(len(motor.recebidos), FRASES_EM_ESPERA)
        self.assertNotIn(chunk(0x30 + FRASES_EM_ESPERA) * 10, motor.recebidos)


class TestParakeetSemEsperaAtiva(unittest.TestCase):
    """O Parakeet carrega com as threads do onnxruntime sem spin (latencia estavel)."""

    def carregar_com_modulos_falsos(self, device: str):
        opcoes = mock.Mock()
        onnxruntime_falso = mock.Mock()
        onnxruntime_falso.SessionOptions.return_value = opcoes
        onnxruntime_falso.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        onnx_asr_falso = mock.Mock()
        with tempfile.TemporaryDirectory() as pasta, mock.patch.dict(
            sys.modules, {"onnxruntime": onnxruntime_falso, "onnx_asr": onnx_asr_falso}
        ):
            MotorParakeet(device, pasta=Path(pasta)).carregar()
        return opcoes, onnx_asr_falso.load_model.call_args

    def test_cpu_e_cuda_carregam_sem_spin(self) -> None:
        for device in ("cpu", "cuda"):
            with self.subTest(device=device):
                opcoes, chamada = self.carregar_com_modulos_falsos(device)
                self.assertIs(chamada.kwargs["sess_options"], opcoes)
                opcoes.add_session_config_entry.assert_any_call("session.intra_op.allow_spinning", "0")
                opcoes.add_session_config_entry.assert_any_call("session.inter_op.allow_spinning", "0")


class TestThreads(Base):
    def test_modo_ficheiro_pelo_caminho_das_threads(self) -> None:
        motor = MotorFalso()
        fonte = FonteDeFicheiro(chunk(0x22) * 20)
        ouvido = Ouvido(fonte, motor, self.frases.append, tecla=TeclaDoFicheiro(fonte), escrever=self.linhas.append)
        ouvido.iniciar()
        self.assertTrue(ouvido.esperar(10.0))
        self.assertEqual(motor.recebidos, [chunk(0x22) * 20])
        self.assertEqual(len(self.frases), 1)

    def test_iniciar_duas_vezes_e_recusado(self) -> None:
        fonte = FonteDeFicheiro(b"")
        ouvido = Ouvido(fonte, MotorFalso(), self.frases.append, tecla=TeclaFixa(), escrever=self.linhas.append)
        ouvido.iniciar()
        with self.assertRaises(RuntimeError):
            ouvido.iniciar()
        ouvido.esperar(5.0)

    def test_microfone_que_nao_abre_levanta_no_iniciar(self) -> None:
        ouvido = Ouvido(FonteQueNaoAbre(), MotorFalso(), self.frases.append, tecla=TeclaFixa(), escrever=self.linhas.append)
        with self.assertRaises(OSError):
            ouvido.iniciar()
        self.assertFalse(ouvido.a_correr())

    def test_parar_fecha_a_fonte_e_acaba_as_threads(self) -> None:
        fonte = FonteSemFim()
        motor = MotorFalso()
        ouvido = Ouvido(fonte, motor, self.frases.append, tecla=TeclaFixa(), escrever=self.linhas.append)
        ouvido.iniciar()
        self.assertTrue(ouvido.a_correr())
        ouvido.parar()
        self.assertTrue(ouvido.esperar(5.0))
        self.assertTrue(fonte.fechada)
        self.assertEqual(motor.recebidos, [])  # a tecla nunca foi premida


class TestFonteDeFicheiro(unittest.TestCase):
    def test_ultimo_chunk_completado_com_silencio(self) -> None:
        pedacos = chunks_do_pcm(b"\x01\x00" * 500)
        self.assertEqual(len(pedacos), 2)
        self.assertTrue(all(len(p) == BYTES_POR_CHUNK for p in pedacos))
        self.assertEqual(pedacos[1][-2:], b"\x00\x00")

    def test_tecla_do_ficheiro_premida_so_durante_o_audio(self) -> None:
        fonte = FonteDeFicheiro(chunk(0x22) * 3, silencio_depois_s=0.09)
        tecla = TeclaDoFicheiro(fonte)
        estados = []
        while fonte.ler() is not None:
            estados.append(tecla.premida())
        self.assertEqual(estados, [True, True, True, False, False, False])

    def test_pcm_do_wav_junta_canais_e_reamostra(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "estereo.wav"
            escrever_wav_pcm16(caminho, b"\x10\x00\x30\x00" * 8000, 8000, 2)
            pcm = pcm_do_wav(caminho)
        self.assertAlmostEqual(len(pcm) / 2 / 16000, 1.0, places=2)


class TestMicrofone(unittest.TestCase):
    """O microfone vivo com o PyAudio falso do verificar_microfone.

    O mesmo microfone inventado existe em MME (nome cortado, 16 kHz), em
    DirectSound (zeros sem esperar), em WASAPI (so 48 kHz estereo) e em WDM-KS:
    o ouvido nunca pode abrir os dois do meio da lista de exclusao.
    """

    MME, DS, WASAPI, WDMKS = 2, 3, 4, 5
    PROIBIDOS = {3, 5}

    def setUp(self) -> None:
        self.m = ouvido_mod._modulo_do_microfone()
        self.avisos: list[str] = []

    def pa(self, **comportamentos):
        pa = self.m.PyAudioFalso()
        for chave, comportamento in comportamentos.items():
            pa.dispositivos[int(chave[1:])]["comportamento"] = comportamento
        return pa

    def fonte(self, pa, nome: str | None = None) -> MicrofonePyAudio:
        return MicrofonePyAudio(
            self.m.NOME_FALSO if nome is None else nome,
            criar_pa=lambda: pa, formato=8, relogio=pa.relogio, avisar=self.avisos.append,
        )

    def abertos(self, pa) -> set[int]:
        return {indice for indice, _, _ in pa.aberturas}

    def test_e_o_mesmo_modulo_do_gravador(self) -> None:
        from scripts import gravar_voz

        self.assertIs(ouvido_mod._modulo_do_microfone(), gravar_voz.microfone)

    def test_nome_inteiro_abre_o_mme_a_16_khz_em_chunks_de_30_ms(self) -> None:
        pa = self.pa()
        fonte = self.fonte(pa)
        fonte.abrir()
        self.assertEqual(pa.aberturas, [(self.MME, 16000, 1)])
        self.assertIn("via MME", fonte.descricao)
        self.assertTrue(all(len(fonte.ler()) == BYTES_POR_CHUNK for _ in range(50)))
        fonte.fechar()
        self.assertTrue(pa.terminado)

    def test_nome_parcial_que_o_mme_cortou_abre_wasapi_e_nunca_directsound(self) -> None:
        # O nome parcial esta dentro do nome inteiro (DirectSound, WASAPI,
        # WDM-KS) mas nao no nome cortado do MME: so WASAPI pode abrir.
        pa = self.pa()
        fonte = self.fonte(pa, "Exemplo Audio USB 1234 Pro")
        fonte.abrir()
        self.assertEqual(self.abertos(pa), {self.WASAPI})
        self.assertIn("via WASAPI", fonte.descricao)
        self.assertIn("reamostrado de 48000 Hz", fonte.descricao)
        self.assertTrue(any("DirectSound excluida" in t for t in fonte.tentativas))
        self.assertTrue(any("WDM-KS excluida" in t for t in fonte.tentativas))
        chunks = [fonte.ler() for _ in range(40)]
        self.assertTrue(all(len(c) == BYTES_POR_CHUNK for c in chunks))
        self.assertTrue(all(c.count(0) != len(c) for c in chunks))  # tom, nao zeros
        fonte.fechar()

    def test_mme_que_falha_a_prova_passa_ao_wasapi_nunca_ao_directsound(self) -> None:
        pa = self.pa(d2="rapido")
        fonte = self.fonte(pa)
        fonte.abrir()
        self.assertTrue(self.abertos(pa).isdisjoint(self.PROIBIDOS))
        self.assertIn("via WASAPI", fonte.descricao)
        self.assertTrue(any("mais rapida que o relogio" in t for t in fonte.tentativas))
        fonte.fechar()

    def test_stream_de_zeros_falha_alto_ao_abrir(self) -> None:
        pa = self.pa(d2="mudo", d4="mudo")
        fonte = self.fonte(pa)
        with self.assertRaises(OSError) as erro:
            fonte.abrir()
        self.assertIsInstance(erro.exception, self.m.MicrofoneInutilizavel)
        self.assertIn("silencio digital", str(erro.exception))
        self.assertTrue(self.abertos(pa).isdisjoint(self.PROIBIDOS))
        self.assertTrue(pa.terminado)

    def test_so_directsound_e_wdmks_nunca_abrem(self) -> None:
        pa = self.pa()
        for indice in (self.MME, self.WASAPI):
            pa.dispositivos[indice]["maxInputChannels"] = 0
        pa.dispositivos[1]["maxInputChannels"] = 0  # sem entrada por omissao que sirva
        fonte = self.fonte(pa)
        with self.assertRaises(OSError):
            fonte.abrir()
        self.assertEqual(pa.aberturas, [])

    def test_ouvido_que_nao_abre_o_microfone_levanta_para_quem_arranca(self) -> None:
        pa = self.pa(d2="zeros", d4="zeros")
        motor = MotorFalso()
        ouvido = Ouvido(self.fonte(pa), motor, lambda frase: None, tecla=TeclaFixa(True), escrever=lambda _: None)
        with self.assertRaises(OSError):
            ouvido.iniciar()
        self.assertFalse(ouvido.a_correr())
        self.assertEqual(motor.recebidos, [])

    def test_leitura_mais_rapida_que_o_relogio_depois_de_abrir_para_a_captura(self) -> None:
        pa = self.pa()
        fonte = self.fonte(pa)
        fonte.abrir()
        pa.dispositivos[self.MME]["comportamento"] = "zeros"  # o defeito do DirectSound
        with self.assertRaises(OSError) as erro:
            for _ in range(chunks_em(2.0)):
                fonte.ler()
        self.assertIn("tempo real", str(erro.exception))
        fonte.fechar()

    def test_zeros_a_meio_da_escuta_avisam_uma_vez_e_o_regresso_tambem(self) -> None:
        pa = self.pa()
        fonte = self.fonte(pa)
        fonte.abrir()
        dispositivo = pa.dispositivos[self.MME]
        dispositivo["comportamento"] = "mudo"
        for _ in range(chunks_em(5.0)):
            fonte.ler()
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("zeros exatos", self.avisos[0])
        dispositivo["comportamento"] = "tempo_real"
        for _ in range(chunks_em(0.5)):
            fonte.ler()
        self.assertEqual(len(self.avisos), 2)
        self.assertIn("voltou a dar sinal", self.avisos[1])
        fonte.fechar()

    def test_stream_que_deixa_de_entregar_audio_para_a_captura(self) -> None:
        pa = self.pa()
        fonte = self.fonte(pa)
        fonte.abrir()
        fonte._entrada.ler = lambda: b""
        with self.assertRaises(OSError):
            fonte.ler()
        fonte.fechar()


class TestTeclaWindows(unittest.TestCase):
    def test_codigo_fora_do_intervalo_e_recusado(self) -> None:
        for codigo in (0, 256, -1):
            with self.assertRaises(ValueError):
                TeclaWindows(codigo)

    @unittest.skipUnless(sys.platform == "win32", "API do Windows")
    def test_le_o_estado_de_uma_tecla_sem_a_mexer(self) -> None:
        tecla = TeclaWindows(TECLAS_DE_FALAR["f24"])
        self.assertIsInstance(tecla.premida(), bool)
        self.assertIsInstance(tecla.outra_tecla_premida(), bool)

    @unittest.skipUnless(sys.platform == "win32", "API do Windows")
    def test_atalho_ignora_a_propria_tecla_e_os_modificadores_genericos(self) -> None:
        tecla = TeclaWindows(TECLAS_DE_FALAR["ctrl-direito"])
        premidas = {0xA3, 0x11}  # ctrl-direito acende tambem o Ctrl generico
        tecla._ler = lambda codigo: -32768 if codigo in premidas else 0
        self.assertTrue(tecla.premida())
        self.assertFalse(tecla.outra_tecla_premida())
        premidas.add(ord("C"))
        self.assertTrue(tecla.outra_tecla_premida())
        premidas.discard(ord("C"))
        premidas.add(0x01)  # botao esquerdo do rato nao e atalho
        self.assertFalse(tecla.outra_tecla_premida())

    @unittest.skipUnless(sys.platform == "win32", "API do Windows")
    def test_alt_direito_ignora_o_ctrl_esquerdo_do_altgr(self) -> None:
        tecla = TeclaWindows(TECLAS_DE_FALAR["alt-direito"])
        # AltGr: o Windows da o Alt direito, o Alt e o Ctrl genericos e o
        # Ctrl esquerdo como premidos
        premidas = {0xA5, 0x12, 0x11, 0xA2}
        tecla._ler = lambda codigo: -32768 if codigo in premidas else 0
        self.assertTrue(tecla.premida())
        self.assertFalse(tecla.outra_tecla_premida())
        premidas.add(ord("V"))
        self.assertTrue(tecla.outra_tecla_premida())

    @unittest.skipUnless(sys.platform == "win32", "API do Windows")
    def test_ctrl_esquerdo_continua_a_ser_atalho_com_outras_teclas(self) -> None:
        tecla = TeclaWindows(TECLAS_DE_FALAR["ctrl-direito"])
        premidas = {0xA3, 0x11, 0xA2}
        tecla._ler = lambda codigo: -32768 if codigo in premidas else 0
        self.assertTrue(tecla.outra_tecla_premida())



class TestAltGrNoOuvido(Base):
    @unittest.skipUnless(sys.platform == "win32", "API do Windows")
    def test_alt_direito_num_teclado_com_altgr_da_uma_frase(self) -> None:
        """Segurar o AltGr (teclado portugues) e falar chega ao motor."""
        motor = MotorFalso()
        tecla = TeclaWindows(TECLAS_DE_FALAR["alt-direito"])
        tecla._ler = lambda codigo: -32768 if codigo in {0xA5, 0x12, 0x11, 0xA2} else 0
        ouvido = self.ouvido(motor, tecla=tecla)
        self.alimentar(ouvido, 0x22, 20, premida=True)
        self.alimentar(ouvido, 0x00, 1)
        ouvido.transcrever_pendentes()
        self.assertEqual(motor.recebidos, [chunk(0x22) * 20])
        self.assertFalse(any("atalho de teclado" in linha for linha in self.linhas))


class TestConfigDoOuvido(unittest.TestCase):
    def escrever(self, pasta: str, extra: str) -> Path:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            f'[[projetos]]\nnome = "p"\ncaminho = "{Path(pasta).as_posix()}"\n\n' + extra,
            encoding="utf-8",
        )
        return caminho

    def test_sem_tabela_valem_os_valores_por_omissao(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            config = carregar_config(self.escrever(pasta, ""))
        self.assertEqual(config.ouvido, ConfigOuvido())
        self.assertEqual(config.ouvido.codigo_da_tecla, 0xA3)

    def test_tabela_valida(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            config = carregar_config(
                self.escrever(pasta, '[ouvido]\ntecla = "F18"\nmotor = "whisper-medium"\ndevice = "cuda"\nlingua = "en"\n')
            )
        self.assertEqual(config.ouvido, ConfigOuvido("f18", "whisper-medium", "cuda", "en"))
        self.assertEqual(config.ouvido.codigo_da_tecla, 0x81)

    def test_valores_fora_das_listas_sao_recusados(self) -> None:
        for extra in (
            '[ouvido]\ntecla = "a"\n',
            '[ouvido]\ntecla = "insert"\n',
            '[ouvido]\ntecla = "f5"\n',
            '[ouvido]\nmotor = "alguem/modelo"\n',
            '[ouvido]\ndevice = "tpu"\n',
            '[ouvido]\nlingua = "fr"\n',
            '[ouvido]\ntecla = 5\n',
            '[ouvido]\nlimiar = "0.1"\n',
            '[[ouvido]]\ntecla = "f8"\n',
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as pasta:
                with self.assertRaises(ConfigError):
                    carregar_config(self.escrever(pasta, extra))

    def test_o_exemplo_versionado_tem_a_tabela_valida(self) -> None:
        config = carregar_config(Path(ouvido_mod.RAIZ) / "config.exemplo.toml", validar_caminhos=False)
        self.assertEqual(config.ouvido, ConfigOuvido())


class TestMedicao(unittest.TestCase):
    def test_percentil_pelo_posto_mais_proximo(self) -> None:
        valores = [float(v) for v in range(1, 21)]
        self.assertEqual(percentil(valores, 50), 10.0)
        self.assertEqual(percentil(valores, 95), 19.0)
        self.assertEqual(percentil([7.0], 95), 7.0)
        with self.assertRaises(ValueError):
            percentil([], 50)

    def test_medir_pelo_caminho_da_tecla_e_salta_os_longos(self) -> None:
        motor = MotorFalso()
        with tempfile.TemporaryDirectory() as pasta:
            curto = Path(pasta) / "curto.wav"
            longo = Path(pasta) / "longo.wav"
            escrever_wav_pcm16(curto, b"\x22\x22" * 16000, 16000, 1)
            escrever_wav_pcm16(longo, b"\x22\x22" * 16000 * 6, 16000, 1)
            resultado = medir(motor, [curto, longo], repeticoes=2, escrever=lambda _t: None)
        self.assertEqual((resultado["wavs"], resultado["saltados"]), (1, 1))
        self.assertEqual(len(resultado["latencias_ms"]), 2)
        self.assertEqual(len(resultado["sinais_ms"]), 4)
        self.assertEqual(len(motor.recebidos), 2)


class TestAutoteste(unittest.TestCase):
    def test_autoteste_passa_em_silencio(self) -> None:
        saida = io.StringIO()
        winsound_falso = mock.Mock()
        with contextlib.redirect_stdout(saida), mock.patch.dict(sys.modules, {"winsound": winsound_falso}):
            codigo = ouvido_mod._autoteste()
        self.assertEqual(codigo, 0, saida.getvalue())
        self.assertIn("OK: autoteste do jarvis.ouvido", saida.getvalue())
        winsound_falso.Beep.assert_not_called()

    def test_cli_exige_um_modo(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ouvido_mod.construir_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
