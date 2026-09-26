r"""Testes de jarvis/sinais.py e da tabela [escuta] de jarvis/config.py.

Os sons sao so bytes gerados em memoria: mede-se a duracao, a diferenca entre
"abrir" (sobe) e "fechar" (desce), as rampas sem estalido e o volume. Nenhum
teste toca som: o tocador do sistema e substituido por um que falha se for
chamado, e onde `com_som=True` e preciso para provar a thread, o tocador e
falso.

Corre com:

    .venv\Scripts\python -m unittest tests.test_sinais -v
"""

from __future__ import annotations

import builtins
import io
import tempfile
import threading
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

from jarvis import ouvido, sinais
from jarvis.config import (
    CAMINHO_EXEMPLO,
    ConfigError,
    ConfigEscuta,
    SEGUIMENTO_S,
    VOLUME_DOS_SONS_PADRAO,
    carregar_config,
)


def _amostras(pcm: bytes) -> array:
    valores = array("h")
    valores.frombytes(pcm)
    return valores


def _passagens_por_zero(valores) -> int:
    return sum(1 for a, b in zip(valores, valores[1:]) if (a < 0) != (b < 0))


def _tocador_proibido(_wav: bytes) -> None:
    raise AssertionError("o tocador do sistema foi chamado num teste")


class TestSons(unittest.TestCase):
    def test_cada_som_dura_no_maximo_200_ms_e_menos_do_que_a_guarda(self):
        for tipo in ("abrir", "fechar"):
            duracao = len(sinais.gerar_som(tipo)) / 2 / sinais.TAXA
            self.assertGreater(duracao, 0.05, tipo)
            self.assertLessEqual(duracao, 0.2, tipo)
            self.assertLess(duracao, ouvido.GUARDA_APOS_A_VOZ_S, tipo)

    def test_abrir_sobe_e_fechar_desce(self):
        # A altura do tom mede-se pelas passagens por zero em cada metade.
        def metades(tipo):
            valores = _amostras(sinais.gerar_som(tipo))
            meio = len(valores) // 2
            return _passagens_por_zero(valores[:meio]), _passagens_por_zero(valores[meio:])

        inicio, fim = metades("abrir")
        self.assertGreater(fim, inicio * 1.15)
        inicio, fim = metades("fechar")
        self.assertLess(fim, inicio / 1.15)
        self.assertNotEqual(sinais.gerar_som("abrir"), sinais.gerar_som("fechar"))

    def test_comeca_e_acaba_em_silencio_sem_estalido(self):
        for tipo in ("abrir", "fechar"):
            valores = _amostras(sinais.gerar_som(tipo, volume=1.0))
            limite = 0.01 * sinais.ESCALA_COMPLETA
            self.assertLessEqual(abs(valores[0]), limite, tipo)
            self.assertLessEqual(abs(valores[-1]), limite, tipo)
            # A rampa sobe aos poucos: os primeiros 2 ms ficam bem abaixo do pico.
            inicio = max(abs(v) for v in valores[: int(sinais.TAXA * 0.002)])
            self.assertLess(inicio, 0.05 * max(abs(v) for v in valores), tipo)

    def test_pico_acompanha_o_volume_e_e_baixo_por_omissao(self):
        for tipo in ("abrir", "fechar"):
            pico_padrao = max(abs(v) for v in _amostras(sinais.gerar_som(tipo)))
            self.assertLessEqual(pico_padrao, 0.2 * sinais.ESCALA_COMPLETA, tipo)
            self.assertGreater(pico_padrao, 0, tipo)
            pico_metade = max(abs(v) for v in _amostras(sinais.gerar_som(tipo, volume=0.05)))
            pico_cheio = max(abs(v) for v in _amostras(sinais.gerar_som(tipo, volume=0.5)))
            self.assertAlmostEqual(pico_cheio / pico_metade, 10.0, delta=0.1)
            self.assertLessEqual(pico_cheio, 0.5 * sinais.ESCALA_COMPLETA + 1)

    def test_wav_em_memoria_mono_pcm16(self):
        dados = sinais.wav_do_som("abrir")
        with wave.open(io.BytesIO(dados), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), sinais.TAXA)
            self.assertEqual(wav.readframes(wav.getnframes()), sinais.gerar_som("abrir"))

    def test_tipo_ou_volume_invalidos_recusados(self):
        with self.assertRaises(ValueError):
            sinais.gerar_som("outro")
        for volume in (0, -0.1, 1.5, True, "baixo"):
            with self.assertRaises(ValueError, msg=repr(volume)):
                sinais.gerar_som("abrir", volume=volume)


class TestTocarSoComSom(unittest.TestCase):
    def test_sem_com_som_nao_importa_nem_chama_o_tocador(self):
        importar = builtins.__import__

        def importar_vigiado(nome, *args, **kwargs):
            if nome == "winsound":
                raise AssertionError("winsound importado sem com_som=True")
            return importar(nome, *args, **kwargs)

        with mock.patch.object(sinais, "_tocar_no_sistema", side_effect=_tocador_proibido) as tocador, \
                mock.patch.object(builtins, "__import__", side_effect=importar_vigiado), \
                mock.patch.object(threading, "Thread", side_effect=AssertionError("thread criada")):
            self.assertIsNone(sinais.tocar("abrir"))
            self.assertIsNone(sinais.tocar("fechar", volume=0.5))
            self.assertIsNone(sinais.tocar("abrir", com_som=False))
        tocador.assert_not_called()

    def test_com_som_toca_numa_thread_daemon(self):
        tocados: list[bytes] = []
        with mock.patch.object(sinais, "_tocar_no_sistema", side_effect=tocados.append):
            thread = sinais.tocar("fechar", volume=0.1, com_som=True)
            self.assertIsNotNone(thread)
            self.assertTrue(thread.daemon)
            thread.join(timeout=5)
        self.assertEqual(tocados, [sinais.wav_do_som("fechar", 0.1)])

    def test_falha_do_tocador_nunca_chega_a_quem_chamou(self):
        with mock.patch.object(sinais, "_tocar_no_sistema", side_effect=RuntimeError("sem dispositivo")), \
                self.assertLogs("jarvis.sinais", level="WARNING") as registos:
            thread = sinais.tocar("abrir", com_som=True)
            thread.join(timeout=5)
        self.assertIn("sem dispositivo", registos.output[0])

    def test_falha_ao_gerar_nunca_chega_a_quem_chamou(self):
        with mock.patch.object(sinais, "_tocar_no_sistema", side_effect=_tocador_proibido), \
                self.assertLogs("jarvis.sinais", level="WARNING"):
            self.assertIsNone(sinais.tocar("outro", com_som=True))


_BASE = '[microfone]\nnome = "Microfone"\n\n[[projetos]]\nnome = "x"\ncaminho = "."\n\n'


class TestConfigEscuta(unittest.TestCase):
    def carregar(self, extra: str):
        # Uma chave solta tem de vir antes das tabelas para ficar na raiz.
        texto = extra + _BASE if not extra.startswith("[") and extra else _BASE + extra
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "config.toml"
            caminho.write_text(texto, encoding="utf-8")
            return carregar_config(caminho, validar_caminhos=False)

    def test_sem_tabela_valem_os_valores_por_omissao(self):
        escuta = self.carregar("").escuta
        self.assertEqual(escuta, ConfigEscuta())
        self.assertEqual(escuta.seguimento_s, 8.0)
        self.assertEqual(SEGUIMENTO_S, 8.0)
        self.assertTrue(escuta.sons)
        self.assertEqual(escuta.volume, VOLUME_DOS_SONS_PADRAO)
        self.assertLessEqual(VOLUME_DOS_SONS_PADRAO, 0.2)

    def test_exemplo_versionado_tem_os_valores_por_omissao(self):
        self.assertIn("[escuta]", CAMINHO_EXEMPLO.read_text(encoding="utf-8"))
        self.assertEqual(carregar_config(CAMINHO_EXEMPLO, validar_caminhos=False).escuta, ConfigEscuta())

    def test_valores_validos(self):
        escuta = self.carregar("[escuta]\nseguimento_s = 5\nsons = false\nvolume = 0.3\n").escuta
        self.assertEqual(escuta, ConfigEscuta(seguimento_s=5.0, sons=False, volume=0.3))
        self.assertIsInstance(escuta.seguimento_s, float)
        self.assertEqual(self.carregar("[escuta]\nseguimento_s = 3\n").escuta.seguimento_s, 3.0)
        self.assertEqual(self.carregar("[escuta]\nseguimento_s = 30.0\n").escuta.seguimento_s, 30.0)
        self.assertEqual(self.carregar("[escuta]\nvolume = 1\n").escuta.volume, 1.0)

    def test_valores_invalidos_recusados_com_mensagem_legivel(self):
        casos = {
            "escuta = 8\n": "tem de ser uma tabela",
            "[escuta]\natraso = 2\n": "chaves desconhecidas",
            "[escuta]\nseguimento_s = 2.9\n": "seguimento_s",
            "[escuta]\nseguimento_s = 31\n": "seguimento_s",
            '[escuta]\nseguimento_s = "8"\n': "seguimento_s",
            "[escuta]\nseguimento_s = true\n": "seguimento_s",
            '[escuta]\nsons = "sim"\n': "sons",
            "[escuta]\nsons = 1\n": "sons",
            "[escuta]\nvolume = 0\n": "volume",
            "[escuta]\nvolume = -0.1\n": "volume",
            "[escuta]\nvolume = 1.01\n": "volume",
            '[escuta]\nvolume = "baixo"\n': "volume",
            "[escuta]\nvolume = true\n": "volume",
        }
        for extra, esperado in casos.items():
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError) as erro:
                    self.carregar(extra)
                self.assertIn("[escuta]", str(erro.exception))
                self.assertIn(esperado, str(erro.exception))


if __name__ == "__main__":
    unittest.main()
