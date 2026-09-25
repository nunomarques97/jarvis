r"""Testes da escolha do microfone: API, nome cortado, prova de tempo real e zeros.

Nenhum teste toca no microfone nem no altifalante: o PyAudio, os streams e o
relogio sao falsos (`PyAudioFalso` e `RelogioFalso` de
scripts/verificar_microfone.py), com nomes de dispositivos inventados que
reproduzem o defeito medido no Windows: o mesmo microfone em MME (nome cortado
a 31 caracteres, le em tempo real a 16 kHz), DirectSound (zeros sem esperar),
WASAPI (so 48 kHz e 2 canais) e WDM-KS.

Corre com:

    .venv\Scripts\python -m unittest tests.test_verificar_microfone -v
"""

from __future__ import annotations

import contextlib
import io
import struct
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts import avaliar_voz  # noqa: E402

gravar = avaliar_voz.gravar
microfone = gravar.microfone

NOME = microfone.NOME_FALSO
MME, DS, WASAPI, WDMKS = 2, 3, 4, 5  # indices dos dispositivos falsos


def _pa(**comportamentos) -> "microfone.PyAudioFalso":
    """PyAudio falso; `comportamentos` troca o de cada indice (ex.: d2='rapido')."""
    pa = microfone.PyAudioFalso()
    for chave, comportamento in comportamentos.items():
        pa.dispositivos[int(chave[1:])]["comportamento"] = comportamento
    return pa


def _abrir(pa, nome: str = NOME, **kwargs):
    return microfone.abrir_microfone(pa, nome, 8, relogio=pa.relogio, **kwargs)


class TestNomeCortado(unittest.TestCase):
    def test_nome_inteiro_e_parte_casam_sem_maiusculas(self) -> None:
        self.assertTrue(microfone.nome_casa(NOME, NOME))
        self.assertTrue(microfone.nome_casa(NOME, "  audio usb 1234 "))
        self.assertTrue(microfone.nome_casa(NOME.upper(), NOME.lower()))

    def test_nome_cortado_pelo_mme_casa_pelo_prefixo(self) -> None:
        cortado = NOME[: microfone.LIMITE_DO_NOME_MME]
        self.assertEqual(len(cortado), 31)
        self.assertTrue(microfone.nome_casa(cortado, NOME))
        # O PortAudio tira o espaco que o corte pode deixar no fim.
        self.assertTrue(microfone.nome_casa(NOME[:29].strip(), NOME))

    def test_prefixos_que_nao_sao_corte_nao_casam(self) -> None:
        self.assertFalse(microfone.nome_casa("Microfone", NOME))  # curto demais
        self.assertFalse(microfone.nome_casa(NOME + " extra", "Outro microfone"))
        # Um nome com mais de 31 caracteres nao foi cortado pelo MME.
        self.assertFalse(microfone.nome_casa(NOME[:35], NOME))
        self.assertFalse(microfone.nome_casa("Entrada de linha (placa integrada)", NOME))
        self.assertFalse(microfone.nome_casa("", NOME))
        self.assertFalse(microfone.nome_casa(NOME, "   "))

    def test_mme_com_nome_cortado_e_encontrado(self) -> None:
        candidatos, origem, _ = microfone.candidatos_do_microfone(_pa(), NOME)
        self.assertEqual(candidatos[0].indice, MME)
        self.assertEqual(origem, f"configurado: '{NOME}'")


class TestPreferenciaDeApi(unittest.TestCase):
    def test_mme_depois_wasapi_nunca_directsound_nem_wdmks(self) -> None:
        candidatos, _, notas = microfone.candidatos_do_microfone(_pa(), NOME)
        self.assertEqual([(c.indice, c.api) for c in candidatos], [(MME, "MME"), (WASAPI, "WASAPI")])
        self.assertTrue(any("DirectSound excluida" in n for n in notas))
        self.assertTrue(any("WDM-KS excluida" in n for n in notas))

    def test_ordem_do_sistema_nao_muda_a_preferencia(self) -> None:
        pa = _pa()
        # WASAPI listado antes do MME: o MME continua primeiro.
        pa.dispositivos[MME], pa.dispositivos[WASAPI] = pa.dispositivos[WASAPI], pa.dispositivos[MME]
        pa.dispositivos[MME]["index"], pa.dispositivos[WASAPI]["index"] = MME, WASAPI
        candidatos, _, _ = microfone.candidatos_do_microfone(pa, NOME)
        self.assertEqual([c.api for c in candidatos], ["MME", "WASAPI"])

    def test_abre_o_mme_a_16_khz_e_nunca_toca_no_directsound(self) -> None:
        pa = _pa()
        entrada = _abrir(pa)
        self.assertEqual((entrada.escolha.candidato.indice, entrada.escolha.taxa, entrada.escolha.canais), (MME, 16000, 1))
        self.assertIsNone(entrada.prova.defeito)
        entrada.fechar()
        self.assertFalse(any(indice in (DS, WDMKS) for indice, _, _ in pa.aberturas))

    def test_so_directsound_e_wdmks_com_o_nome_cai_na_omissao_e_diz_porque(self) -> None:
        pa = _pa()
        for indice in (MME, WASAPI):
            pa.dispositivos[indice]["name"] = "Outro dispositivo qualquer"
        entrada = _abrir(pa)
        self.assertEqual(entrada.escolha.candidato.api, "MME")
        self.assertIn("nao existe em MME nem WASAPI", entrada.escolha.origem)
        self.assertTrue(any("DirectSound excluida" in t for t in entrada.tentativas))
        entrada.fechar()
        self.assertFalse(any(indice in (DS, WDMKS) for indice, _, _ in pa.aberturas))

    def test_sem_nome_usa_as_entradas_por_omissao_de_mme_e_wasapi(self) -> None:
        candidatos, origem, _ = microfone.candidatos_do_microfone(_pa(), "")
        self.assertEqual([c.api for c in candidatos], ["MME", "WASAPI"])
        self.assertEqual(origem, "omissao do sistema")
        _, origem, notas = microfone.candidatos_do_microfone(_pa(), "NAO_EXISTE_XPTO")
        self.assertIn("nao existe", origem)
        self.assertTrue(any("NAO_EXISTE_XPTO" in n for n in notas))

    def test_wasapi_a_48_khz_estereo_e_reamostrado_para_16_khz_mono(self) -> None:
        pa = _pa()
        pa.dispositivos[MME]["taxas"] = set()  # o MME nao abre
        entrada = _abrir(pa)
        self.assertEqual((entrada.escolha.candidato.api, entrada.escolha.taxa, entrada.escolha.canais), ("WASAPI", 48000, 2))
        self.assertAlmostEqual(entrada.prova.audio_s, 1.0, delta=0.1)
        self.assertGreater(gravar.pico_pcm16(entrada.prova.pcm), 1000)
        # Leitura continua: o estado da reamostragem mantem o ritmo de 16 kHz.
        antes = pa.relogio()
        lidos = sum(len(entrada.ler()) for _ in range(20)) // 2
        self.assertAlmostEqual(lidos / 16000, pa.relogio() - antes, delta=0.05)
        entrada.fechar()
        self.assertIn("reamostrado de 48000 Hz", entrada.escolha.descrever())


class TestProvaDeTempoReal(unittest.TestCase):
    def test_leitura_mais_rapida_que_o_relogio_e_recusada(self) -> None:
        pa = _pa(d2="rapido")
        entrada = _abrir(pa)
        self.assertEqual(entrada.escolha.candidato.api, "WASAPI")
        self.assertTrue(any("mais rapida que o relogio" in t for t in entrada.tentativas))
        entrada.fechar()

    def test_o_defeito_do_directsound_seria_recusado(self) -> None:
        # O DirectSound nunca e candidato; se fosse, a prova apanhava-o.
        pa = _pa(d2="zeros", d4="zeros")
        with self.assertRaises(microfone.MicrofoneInutilizavel) as erro:
            _abrir(pa)
        mensagem = str(erro.exception)
        self.assertIn("mais rapida que o relogio", mensagem)
        self.assertIn("DirectSound excluida", mensagem)
        self.assertIn("nada foi gravado", mensagem)

    def test_leitura_mais_lenta_que_o_relogio_e_recusada(self) -> None:
        pa = _pa(d2="lento", d4="lento")
        with self.assertRaises(microfone.MicrofoneInutilizavel) as erro:
            _abrir(pa)
        self.assertIn("mais lenta que o relogio", str(erro.exception))

    def test_microfone_mudo_em_tempo_real_e_silencio_digital(self) -> None:
        pa = _pa(d2="mudo", d4="mudo")
        with self.assertRaises(microfone.MicrofoneInutilizavel) as erro:
            _abrir(pa)
        self.assertIn("silencio digital", str(erro.exception))

    def test_tolerancia_escrita(self) -> None:
        folga = microfone.FOLGA_ABSOLUTA_S + microfone.FOLGA_RELATIVA * 2.0
        self.assertEqual((microfone.FOLGA_ABSOLUTA_S, microfone.FOLGA_RELATIVA), (0.25, 0.10))
        self.assertIsNone(microfone.desacerto_com_o_relogio(2.0 + folga - 1e-6, 2.0))
        self.assertIsNotNone(microfone.desacerto_com_o_relogio(2.0 + folga + 1e-3, 2.0))
        self.assertIsNone(microfone.desacerto_com_o_relogio(2.0 - folga + 1e-6, 2.0))
        self.assertIsNotNone(microfone.desacerto_com_o_relogio(2.0 - folga - 1e-3, 2.0))
        self.assertIsNone(microfone.desacerto_com_o_relogio(0.1, 2.0, so_excesso=True))
        # O caso medido: 3 s reais deram centenas de milhares de segundos.
        self.assertIsNotNone(microfone.desacerto_com_o_relogio(200_000.0, 3.0))


class TestTrocoDeZeros(unittest.TestCase):
    def test_maior_troco(self) -> None:
        fala = struct.pack("<4h", 900, -900, 3, -1) * 100
        self.assertEqual(microfone.maior_troco_de_zeros_s(fala), 0.0)
        self.assertEqual(microfone.maior_troco_de_zeros_s(b""), 0.0)
        pcm = fala + b"\x00\x00" * 4000 + fala + b"\x00\x00" * 8000 + fala
        self.assertEqual(microfone.maior_troco_de_zeros_s(pcm), 0.5)
        self.assertEqual(microfone.maior_troco_de_zeros_s(b"\x00\x00" * 48000, 48000), 1.0)

    def test_bytes_a_zero_desalinhados_nao_sao_amostras(self) -> None:
        # 1 = 01 00, 256 = 00 01: dois bytes a zero seguidos, mas nenhuma amostra a zero.
        self.assertEqual(microfone.maior_troco_de_zeros_s(struct.pack("<2h", 1, 256) * 1000), 0.0)

    def test_defeito_da_captura(self) -> None:
        fala = gravar.tom_pcm16(1.0)
        self.assertIsNone(microfone.defeito_da_captura(fala, 1.0))
        com_buraco = fala + b"\x00\x00" * 8000 + fala
        self.assertIn("silencio digital", microfone.defeito_da_captura(com_buraco, 2.5))
        quase = fala + b"\x00\x00" * 7998 + fala  # o tom comeca numa amostra a zero
        self.assertIsNone(microfone.defeito_da_captura(quase, 2.5))
        self.assertIn("mais rapida", microfone.defeito_da_captura(fala, 0.01))


class TestCapturaDoGravador(unittest.TestCase):
    def test_captura_pyaudio_com_dispositivos_falsos(self) -> None:
        relogio = microfone.RelogioFalso()
        pas: list = []

        def criar():
            pas.append(microfone.PyAudioFalso(relogio=relogio))
            return pas[-1]

        captura = gravar.CapturaPyAudio(NOME, criar, 8, relogio)
        captura.abrir()
        self.assertIn("via MME", captura.descricao)
        feita = captura.gravar(threading.Event(), maximo_s=1.5)
        captura.fechar()
        self.assertTrue(pas[0].terminado)
        self.assertAlmostEqual(len(feita.pcm) / 2 / 16000, 1.5, delta=0.01)
        self.assertAlmostEqual(feita.segundos_reais, 1.5, delta=0.1)
        self.assertIsNone(microfone.defeito_da_captura(feita.pcm, feita.segundos_reais, so_excesso=True))

    def test_verificar_nao_grava_ficheiro_e_diz_dispositivo_api_tempo_e_nivel(self) -> None:
        relogio = microfone.RelogioFalso()
        saida = io.StringIO()
        with mock.patch.object(gravar, "escrever_wav_pcm16", side_effect=AssertionError("gravou ficheiro")), \
                mock.patch.object(microfone, "escrever_wav_pcm16", side_effect=AssertionError("gravou ficheiro")), \
                contextlib.redirect_stdout(saida):
            codigo = gravar.verificar_captura(NOME, lambda: microfone.PyAudioFalso(relogio=relogio), 8, relogio)
        texto = saida.getvalue()
        self.assertEqual(codigo, 0)
        for esperado in ("dispositivo   = [2]", "API           = MME", "tempo real    = 2.", "audio lido = 2.", "nivel         = RMS"):
            self.assertIn(esperado, texto)
        self.assertIn("DirectSound excluida", texto)

    def test_verificar_falha_com_mensagem_clara(self) -> None:
        relogio = microfone.RelogioFalso()

        def criar():
            pa = microfone.PyAudioFalso(relogio=relogio)
            pa.dispositivos[MME]["comportamento"] = "zeros"
            pa.dispositivos[WASAPI]["comportamento"] = "rapido"
            return pa

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = gravar.verificar_captura(NOME, criar, 8, relogio)
        self.assertEqual(codigo, 1)
        self.assertIn("nada foi gravado", saida.getvalue())

    def test_verificar_na_linha_de_comandos_nao_pede_lingua(self) -> None:
        with mock.patch.object(gravar, "verificar_captura", return_value=0) as verificar, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gravar.main(["--verificar"]), 0)
        verificar.assert_called_once()


class TestAutoteste(unittest.TestCase):
    def test_autoteste_do_verificar_microfone(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(microfone._autoteste(), 0)


if __name__ == "__main__":
    unittest.main()
