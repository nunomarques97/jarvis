r"""Testes da escolha da voz inglesa (jarvis/config.py, jarvis/voz.py, scripts/amostras_voz.py).

unittest da biblioteca padrao. Nada aqui carrega um modelo real, abre o
microfone ou toca som: o `kokoro_onnx` e o `onnxruntime` sao modulos falsos
postos em `sys.modules`, e o dispositivo de som e um objeto que falha o teste
se alguem lhe escrever.

O que estes testes protegem:

  * sem tabela [voz] vale a voz por omissao, masculina britanica; o config
    pode trocar por outra da lista fechada e recusa nomes desconhecidos;
  * o Kokoro fala com a voz escolhida e com a lingua do fonemizador certa
    (en-gb para as vozes b..., en-us para as a...), e a descricao diz a voz;
  * uma voz que nao exista no ficheiro de vozes nunca tira a fala: fica o
    Piper, com o motivo na descricao;
  * `scripts/amostras_voz.py` grava um WAV por voz dentro de audio/ e so usa
    o dispositivo de som com --com-som.

Corre com:

    .venv\Scripts\python -m unittest tests.test_voz_escolha -v
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy

RAIZ = Path(__file__).resolve().parent.parent

from jarvis import voz  # noqa: E402
from jarvis.config import (  # noqa: E402
    VOZ_INGLESA_PADRAO,
    VOZES_INGLESAS,
    ConfigError,
    carregar_config,
)
from scripts import amostras_voz, medir_latencia_voz  # noqa: E402




class _KokoroFalso:
    """Faz o papel de `kokoro_onnx.Kokoro`: regista cada `create()`."""

    chamadas: list[dict] = []
    vozes = ("af_heart", "bm_george", "bm_lewis", "bm_daniel", "bm_fable")

    @classmethod
    def from_session(cls, sessao, vozes):
        return cls()

    def get_voices(self):
        return list(self.vozes)

    def create(self, texto, *, voice, speed, lang):
        _KokoroFalso.chamadas.append({"texto": texto, "voice": voice, "speed": speed, "lang": lang})
        return numpy.zeros(2400, dtype="float32") + 0.1, 24000


def _modulos_falsos(vozes=None) -> dict:
    kokoro = types.ModuleType("kokoro_onnx")
    classe = type("Kokoro", (_KokoroFalso,), {"vozes": tuple(vozes or _KokoroFalso.vozes)})
    kokoro.Kokoro = classe
    onnx = types.ModuleType("onnxruntime")
    onnx.SessionOptions = lambda: types.SimpleNamespace()
    onnx.InferenceSession = lambda *a, **k: object()
    return {"kokoro_onnx": kokoro, "onnxruntime": onnx}


class _SaidaProibida:
    """Dispositivo de som que nunca pode ser usado."""

    def escrever(self, taxa, dados):
        raise AssertionError("o dispositivo de som foi usado sem --com-som")


class _SaidaQueRegista:
    def __init__(self) -> None:
        self.escritas = 0

    def escrever(self, taxa, dados):
        self.escritas += 1


class _BaseComKokoroFalso(unittest.TestCase):
    """Ficheiros do modelo falsos numa pasta temporaria e o estado da voz reposto."""

    vozes_no_ficheiro = None

    def setUp(self) -> None:
        _KokoroFalso.chamadas = []
        self._temporaria = tempfile.TemporaryDirectory()
        pasta = Path(self._temporaria.name)
        self.modelo = pasta / "kokoro.onnx"
        self.ficheiro_vozes = pasta / "voices.bin"
        self.modelo.write_bytes(b"x")
        self.ficheiro_vozes.write_bytes(b"x")
        self._patches = [
            mock.patch.dict(sys.modules, _modulos_falsos(self.vozes_no_ficheiro)),
            mock.patch.object(voz, "MODELO_KOKORO", self.modelo),
            mock.patch.object(voz, "VOZES_KOKORO", self.ficheiro_vozes),
            mock.patch.object(voz, "_SAIDA_DE_SOM", _SaidaProibida()),
        ]
        for patch in self._patches:
            patch.start()
        self._voz_antes = voz.voz_inglesa()
        voz._esquecer_motor_residente()

    def tearDown(self) -> None:
        voz._esquecer_motor_residente()
        voz.definir_voz_inglesa(self._voz_antes)
        for patch in reversed(self._patches):
            patch.stop()
        self._temporaria.cleanup()


class TestConfigDaVoz(unittest.TestCase):
    def _config(self, *extra: str):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "config.toml"
            linhas = (
                "[microfone]",
                'nome = "x"',
                "[[projetos]]",
                'nome = "exemplo"',
                'caminho = "D:/caminho/para/exemplo"',
                *extra,
            )
            caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
            return carregar_config(caminho, validar_caminhos=False)

    def test_sem_tabela_vale_a_voz_masculina_britanica(self) -> None:
        self.assertEqual(self._config().voz.nome, VOZ_INGLESA_PADRAO)
        self.assertTrue(VOZ_INGLESA_PADRAO.startswith("bm_"))
        self.assertIn(VOZ_INGLESA_PADRAO, VOZES_INGLESAS)

    def test_tabela_vazia_vale_a_voz_por_omissao(self) -> None:
        self.assertEqual(self._config("[voz]").voz.nome, VOZ_INGLESA_PADRAO)

    def test_o_config_escolhe_outra_voz_da_lista(self) -> None:
        for nome in VOZES_INGLESAS:
            with self.subTest(nome=nome):
                self.assertEqual(self._config("[voz]", f'nome = "{nome}"').voz.nome, nome)

    def test_a_lista_fechada_tem_a_de_referencia_e_as_quatro_britanicas(self) -> None:
        self.assertEqual(
            set(VOZES_INGLESAS), {"af_heart", "bm_george", "bm_lewis", "bm_daniel", "bm_fable"}
        )

    def test_nome_desconhecido_e_recusado_com_mensagem_clara(self) -> None:
        with self.assertRaises(ConfigError) as contexto:
            self._config("[voz]", 'nome = "af_bella"')
        mensagem = str(contexto.exception)
        self.assertIn("af_bella", mensagem)
        self.assertIn("bm_george", mensagem)

    def test_nome_que_nao_e_texto_e_recusado(self) -> None:
        with self.assertRaises(ConfigError):
            self._config("[voz]", "nome = 3")

    def test_chave_desconhecida_e_recusada(self) -> None:
        with self.assertRaises(ConfigError):
            self._config("[voz]", 'nome = "bm_george"', "velocidade = 2")

    def test_o_exemplo_versionado_documenta_a_tabela(self) -> None:
        config = carregar_config(RAIZ / "config.exemplo.toml", validar_caminhos=False)
        self.assertEqual(config.voz.nome, VOZ_INGLESA_PADRAO)


class TestVozInglesa(_BaseComKokoroFalso):
    def test_por_omissao_o_kokoro_fala_com_a_voz_britanica_em_en_gb(self) -> None:
        voz.definir_voz_inglesa(VOZ_INGLESA_PADRAO)
        motor = voz.MotorKokoro()
        list(motor.sintetizar("Hello there."))
        self.assertEqual(motor.voz, VOZ_INGLESA_PADRAO)
        self.assertEqual(_KokoroFalso.chamadas[-1]["voice"], VOZ_INGLESA_PADRAO)
        self.assertEqual(_KokoroFalso.chamadas[-1]["lang"], "en-gb")
        self.assertIn(VOZ_INGLESA_PADRAO, motor.descricao)

    def test_af_heart_fala_em_en_us(self) -> None:
        voz.definir_voz_inglesa("af_heart")
        motor = voz.motor_residente("en")
        _KokoroFalso.chamadas.clear()
        list(motor.sintetizar("Hello there."))
        self.assertEqual(_KokoroFalso.chamadas[-1]["voice"], "af_heart")
        self.assertEqual(_KokoroFalso.chamadas[-1]["lang"], "en-us")
        self.assertIn("af_heart", motor.descricao)

    def test_lingua_do_fonemizador_pela_primeira_letra(self) -> None:
        for nome in VOZES_INGLESAS:
            with self.subTest(nome=nome):
                esperada = "en-gb" if nome.startswith("b") else "en-us"
                self.assertEqual(voz.lingua_do_fonemizador(nome), esperada)

    def test_cada_voz_usa_a_sua_velocidade(self) -> None:
        for nome in VOZES_INGLESAS:
            with self.subTest(nome=nome):
                _KokoroFalso.chamadas.clear()
                list(voz.MotorKokoro(voz=nome).sintetizar("Hi."))
                self.assertEqual(_KokoroFalso.chamadas[-1]["speed"], voz.VELOCIDADE_DAS_VOZES[nome])
        self.assertEqual(set(voz.VELOCIDADE_DAS_VOZES), set(VOZES_INGLESAS))

    def test_mudar_a_voz_esquece_o_motor_ingles_carregado(self) -> None:
        voz.definir_voz_inglesa("bm_lewis")
        primeiro = voz.motor_residente("en")
        voz.definir_voz_inglesa("bm_lewis")
        self.assertIs(voz.motor_residente("en"), primeiro)
        voz.definir_voz_inglesa("bm_fable")
        segundo = voz.motor_residente("en")
        self.assertIsNot(segundo, primeiro)
        self.assertEqual(segundo.voz, "bm_fable")

    def test_voz_desconhecida_e_recusada_no_modulo(self) -> None:
        with self.assertRaises(ValueError):
            voz.definir_voz_inglesa("af_bella")
        with self.assertRaises(ValueError):
            voz.MotorKokoro(voz="af_bella")

    def test_com_voz_partilha_o_modelo_e_troca_a_lingua(self) -> None:
        base = voz.MotorKokoro(voz="af_heart")
        outro = base.com_voz("bm_daniel")
        self.assertIs(outro._kokoro, base._kokoro)
        self.assertEqual((outro.voz, outro.lingua_do_fonemizador), ("bm_daniel", "en-gb"))
        self.assertEqual((base.voz, base.lingua_do_fonemizador), ("af_heart", "en-us"))


class _PiperFalso:
    nome = "piper"
    lingua = "pt"
    taxa = 22050
    descricao = "Piper falso"

    def sintetizar(self, texto):
        yield b"\x00\x00" * 10


class TestVozQueFaltaNoFicheiro(_BaseComKokoroFalso):
    """O ficheiro de vozes nao tem a britanica pedida: fala o Piper, nunca o silencio."""

    vozes_no_ficheiro = ("af_heart",)

    def test_cai_para_o_piper_com_o_motivo(self) -> None:
        voz.definir_voz_inglesa("bm_george")
        with mock.patch.object(voz, "MotorPiperResidente", _PiperFalso):
            motor = voz.motor_residente("en")
        self.assertEqual(motor.nome, "piper")
        self.assertIn("bm_george", motor.descricao)
        self.assertIn("Kokoro indisponivel", motor.descricao)

    def test_com_voz_recusa_voz_que_falta(self) -> None:
        base = voz.MotorKokoro(voz="af_heart")
        with self.assertRaises(voz.MotorIndisponivel):
            base.com_voz("bm_george")


class TestAmostras(_BaseComKokoroFalso):
    def setUp(self) -> None:
        super().setUp()
        self.pasta_de_audio = Path(self._temporaria.name) / "audio"

    def test_um_wav_por_voz_sem_som(self) -> None:
        base = voz.MotorKokoro(voz="af_heart")
        amostras = amostras_voz.gerar_amostras(
            base, list(VOZES_INGLESAS), pasta_de_audio=self.pasta_de_audio, saida_de_som=_SaidaProibida()
        )
        self.assertEqual([a.voz for a in amostras], list(VOZES_INGLESAS))
        pasta = (self.pasta_de_audio / "amostras-voz").resolve()
        for amostra in amostras:
            self.assertEqual(amostra.caminho.parent, pasta)
            with wave.open(str(amostra.caminho), "rb") as wf:
                self.assertGreater(wf.getnframes(), 0)
        vozes_ditas = {chamada["voice"] for chamada in _KokoroFalso.chamadas}
        self.assertTrue(set(VOZES_INGLESAS) <= vozes_ditas)

    def test_com_som_usa_o_dispositivo(self) -> None:
        saida = _SaidaQueRegista()
        amostras_voz.gerar_amostras(
            voz.MotorKokoro(), ["bm_george"], com_som=True, pasta_de_audio=self.pasta_de_audio, saida_de_som=saida
        )
        self.assertGreater(saida.escritas, 0)

    def test_main_sem_com_som_nao_abre_o_dispositivo(self) -> None:
        # O dispositivo real do modulo e o `_SaidaProibida` do setUp, e o
        # pyaudio nem pode ser importado: nenhuma saida de som e aberta.
        with mock.patch.object(amostras_voz, "PASTA_AUDIO", self.pasta_de_audio), mock.patch.dict(
            sys.modules, {"pyaudio": None}
        ), contextlib.redirect_stdout(io.StringIO()) as saida:
            codigo = amostras_voz.main(["--vozes", "bm_george", "af_heart"])
        self.assertEqual(codigo, 0, saida.getvalue())
        self.assertTrue((self.pasta_de_audio / "amostras-voz" / "bm_george.wav").is_file())
        self.assertTrue((self.pasta_de_audio / "amostras-voz" / "af_heart.wav").is_file())

    def test_pasta_fora_de_audio_e_recusada(self) -> None:
        with self.assertRaises(amostras_voz.AmostraError):
            amostras_voz.validar_pasta(self.pasta_de_audio / ".." / "fora", self.pasta_de_audio)
        self.assertEqual(
            amostras_voz.validar_pasta(self.pasta_de_audio / "amostras-voz", self.pasta_de_audio),
            (self.pasta_de_audio / "amostras-voz").resolve(),
        )

    def test_voz_desconhecida_e_recusada(self) -> None:
        with self.assertRaises(amostras_voz.AmostraError):
            amostras_voz.validar_vozes(["af_bella"])
        self.assertEqual(amostras_voz.validar_vozes(None), list(VOZES_INGLESAS))

    def test_a_pasta_por_omissao_esta_dentro_de_audio_ignorada(self) -> None:
        pasta = amostras_voz.pasta_das_amostras()
        self.assertEqual(pasta.parent, RAIZ / "audio")
        self.assertIn("audio/", (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines())


class TestComparacaoDasVozes(unittest.TestCase):
    """A comparacao intercalada de `scripts/medir_latencia_voz.py --vozes`, sem modelo."""

    def test_a_ordem_roda_e_cada_voz_passa_por_todas_as_posicoes(self) -> None:
        vozes = list(VOZES_INGLESAS)
        posicoes = {nome: set() for nome in vozes}
        for numero in range(len(vozes)):
            ordem = medir_latencia_voz.ordem_da_frase(vozes, 0, numero, 20)
            self.assertEqual(sorted(ordem), sorted(vozes))
            for posicao, nome in enumerate(ordem):
                posicoes[nome].add(posicao)
        self.assertTrue(all(len(p) == len(vozes) for p in posicoes.values()))

    def test_veredito_falha_se_a_voz_por_omissao_for_mais_lenta(self) -> None:
        referencia = {"p50": 500.0, "p95": 700.0}
        self.assertEqual(
            medir_latencia_voz.veredito_da_comparacao(
                {"af_heart": referencia, "bm_fable": {"p50": 500.0, "p95": 690.0}}, "bm_fable"
            ),
            [],
        )
        falhas = medir_latencia_voz.veredito_da_comparacao(
            {"af_heart": referencia, "bm_fable": {"p50": 501.0, "p95": 690.0}}, "bm_fable"
        )
        self.assertEqual(len(falhas), 1)
        self.assertIn("p50", falhas[0])
        self.assertTrue(medir_latencia_voz.veredito_da_comparacao({"bm_fable": referencia}, "bm_fable"))


if __name__ == "__main__":
    unittest.main()
