r"""Ponta a ponta: WAV -> ouvido -> interprete -> confirmacao -> canal, sem hardware.

O caminho vivo inteiro corre como em `python -m jarvis --wav ...`: a fonte de
varios ficheiros, a tecla simulada pelo ficheiro, o `Ouvido` residente, o
`Jarvis` com o `Interprete` e a `Confirmacao` verdadeiros, e o ciclo principal
de `jarvis.app.correr`. So as pontas sao falsas:

  * a transcricao: cada WAV de teste e um tom constante com um valor proprio,
    e o motor falso devolve a frase associada a esse valor (o STT verdadeiro
    mede-se em `scripts/medir_ponta_a_ponta.py`);
  * o LLM do Ollama: respostas feitas, em memoria;
  * o canal para a sessao do Claude Code: regista o que recebe;
  * a voz: guarda o texto em vez de tocar.

O que se prova:

  * ditado -> recap -> "sim": o canal recebe EXATAMENTE o prompt mostrado no
    recap, no projeto certo, e so depois do "sim";
  * ditado -> recap -> "cancela": o canal nao recebe nada;
  * ruido em maos-livres sem palavra de ativacao: nada e transcrito,
    interpretado ou enviado (com o detetor falso e, se o modelo existir em
    models/, com o openWakeWord verdadeiro).

Corre com:

    .venv\Scripts\python -m unittest tests.test_ponta_a_ponta -v
"""

from __future__ import annotations

import contextlib
import io
import random
import struct
import tempfile
import time
import unittest
from pathlib import Path

from jarvis import app
from jarvis.audio_util import escrever_wav_pcm16
from jarvis.ouvido import MODELO_ATIVACAO, BYTES_POR_CHUNK, FonteDeFicheiro, TeclaDoFicheiro
from jarvis.stt import MotorBase
from scripts import medir_ponta_a_ponta
from tests.test_app import CanalFalso, Montagem, _TeclaSolta, resposta_llm

TAXA = 16000

DITADO = "no orbita corrige o teste do login que falha à segunda"
PROMPT = "Corrige o teste do login que falha na segunda execução."
RESPOSTA_DO_DITADO = resposta_llm("ditar_prompt", "orbita", PROMPT)


class MotorPorTom(MotorBase):
    """Transcreve pelo valor do primeiro sample: cada WAV de teste tem o seu."""

    nome = "motor-por-tom"

    def __init__(self, textos: dict[int, str]) -> None:
        super().__init__("cpu")
        self.textos = textos
        self.chamadas = 0

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        if any(pcm16):  # o aquecimento transcreve silencio; so conta audio a serio
            self.chamadas += 1
        if len(pcm16) < 2:
            return "", lingua, False
        (valor,) = struct.unpack_from("<h", pcm16, 0)
        return self.textos.get(valor, ""), lingua, False


def escrever_tom(pasta: Path, nome: str, valor: int, segundos: float = 1.0) -> Path:
    caminho = pasta / f"{nome}.wav"
    escrever_wav_pcm16(caminho, struct.pack("<h", valor) * int(TAXA * segundos), TAXA, 1)
    return caminho


def ruido_domestico(segundos: float, semente: int = 7) -> bytes:
    """Ruido branco moderado, reprodutivel: nenhuma palavra de ativacao la dentro."""
    gerador = random.Random(semente)
    amostras = [int(gerador.gauss(0, 1500)) for _ in range(int(TAXA * segundos))]
    return struct.pack(f"<{len(amostras)}h", *(max(-32768, min(32767, a)) for a in amostras))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self._pasta.cleanup)
        self.pasta = Path(self._pasta.name)

    def correr_ficheiros(self, frases: list[str], respostas_llm: list[str]) -> Montagem:
        """Cada frase vira um WAV; corre o jarvis em modo ficheiro ate acabarem."""
        textos = {1000 + indice: texto for indice, texto in enumerate(frases)}
        wavs = [escrever_tom(self.pasta, f"frase-{valor}", valor) for valor in textos]
        m = Montagem(respostas_llm, relogio=time.perf_counter)
        ouvido = app.construir_ouvido(m.jarvis, motor=MotorPorTom(textos), wavs=wavs)
        self.assertIsInstance(ouvido.tecla, TeclaDoFicheiro)
        codigo = app.correr(m.jarvis, ouvido, com_voz=False, medir=lambda: None)
        self.assertEqual(codigo, 0)
        self.assertEqual(m.jarvis.frases, len(frases), m.log.texto())
        return m


class TestDitadoConfirmado(_Base):
    def test_ditado_recap_sim_entrega_o_prompt_confirmado_no_projeto_certo(self) -> None:
        m = self.correr_ficheiros([DITADO, "sim"], [RESPOSTA_DO_DITADO])

        self.assertEqual(m.canal.recebidos, [("orbita", PROMPT)])
        texto = m.log.texto()
        # O recap mostrou o mesmo prompt que chegou ao canal, antes do "sim".
        self.assertIn(PROMPT, m.falados[0])
        self.assertLess(texto.index("ecra | "), texto.index("prompt confirmado entregue ao canal do orbita"))
        self.assertEqual(m.falados[-1], "Enviado para o orbita.")

    def test_cada_frase_regista_as_etapas_e_as_duas_medidas(self) -> None:
        m = self.correr_ficheiros([DITADO, "sim"], [RESPOSTA_DO_DITADO])

        ditado, sim = m.jarvis.medidas
        self.assertEqual((ditado.intencao, ditado.projeto, ditado.desfecho), ("ditar_prompt", "orbita", "pendente"))
        self.assertTrue(sim.resposta_ao_recap)
        self.assertEqual(sim.desfecho, "executado")
        for medida in (ditado, sim):
            self.assertIsNotNone(medida.sinal_de_vida_ms)
            self.assertIsNotNone(medida.primeira_fala_ms)
            self.assertGreaterEqual(medida.primeira_fala_ms, 0.0)
        texto = m.log.texto()
        for etapa in ("1/5 ouvido", "2/5 transcricao", "3/5 interprete", "4/5 confirmacao/accao", "5/5 voz"):
            self.assertIn(f"frase #1 | etapa {etapa}", texto)
        self.assertIn("frase #1 | primeiro sinal de vida:", texto)
        self.assertIn("frase #1 | inicio da resposta falada:", texto)
        self.assertIn("frase #2 | TOTAL", texto)

    def test_o_estado_passa_por_pensar_espera_e_ouvir(self) -> None:
        m = self.correr_ficheiros([DITADO, "sim"], [RESPOSTA_DO_DITADO])
        historico = m.jarvis.painel.historico
        self.assertIn(app.A_PENSAR, historico)
        self.assertIn(app.A_ESPERA, historico)
        self.assertIn(app.A_FALAR, historico)
        self.assertEqual(historico[-1], app.A_OUVIR)


class TestDitadoCancelado(_Base):
    def test_cancela_depois_do_recap_nao_envia_nada(self) -> None:
        m = self.correr_ficheiros([DITADO, "cancela"], [RESPOSTA_DO_DITADO])

        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.jarvis.medidas[-1].desfecho, "cancelado")
        self.assertFalse(m.jarvis.confirmacao.a_espera)

    def test_sem_resposta_ao_recap_nada_e_enviado(self) -> None:
        m = self.correr_ficheiros([DITADO], [RESPOSTA_DO_DITADO])

        self.assertEqual(m.canal.recebidos, [])
        self.assertTrue(m.jarvis.confirmacao.a_espera, "o recap fica a espera do sim, nunca envia sozinho")


class TestRuidoSemAtivacao(_Base):
    """Maos-livres ligadas, tecla solta, so ruido: nada acontece."""

    def _correr_ruido(self, detetor, vad) -> tuple[Montagem, MotorPorTom]:
        m = Montagem([RESPOSTA_DO_DITADO], relogio=time.perf_counter)
        motor = MotorPorTom({})
        ouvido = app.construir_ouvido(
            m.jarvis,
            motor=motor,
            fonte=FonteDeFicheiro(ruido_domestico(6.0), silencio_depois_s=1.0),
            tecla=_TeclaSolta(),
            detetor=detetor,
            vad=vad,
        )
        self.assertIsNotNone(ouvido.detetor, "o teste tem de correr com as maos-livres ligadas")
        codigo = app.correr(m.jarvis, ouvido, com_voz=False, medir=lambda: None)
        self.assertEqual(codigo, 0)
        return m, motor

    def _nada_aconteceu(self, m: Montagem, motor: MotorPorTom) -> None:
        self.assertEqual(m.jarvis.frases, 0, m.log.texto())
        self.assertEqual(motor.chamadas, 0, "sem ativacao nao se transcreve nada")
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados, [])

    def test_ruido_com_detetor_que_nunca_dispara(self) -> None:
        from tests.test_palavra_ativacao import DetetorComScore

        m, motor = self._correr_ruido(DetetorComScore(0.9), _VadDeEnergia())
        self._nada_aconteceu(m, motor)

    def test_controlo_a_mesma_montagem_com_ativacao_transcreve(self) -> None:
        """Prova que o teste do ruido nao passa por o ouvido estar surdo."""

        class DetetorQueDispara:
            def __init__(self) -> None:
                self.vezes = 0

            def processar(self, _pedaco: bytes) -> float:
                self.vezes += 1
                return 0.95 if self.vezes == 5 else 0.0

            def reiniciar(self) -> None:
                pass

        m, motor = self._correr_ruido(DetetorQueDispara(), _VadDeEnergia())
        self.assertGreater(motor.chamadas, 0)

    @unittest.skipUnless(MODELO_ATIVACAO.is_file(), "modelo openWakeWord em falta em models/ (docs/MODELOS.md)")
    def test_ruido_com_o_openwakeword_verdadeiro(self) -> None:
        from jarvis.ouvido import DetetorOpenWakeWord, VadWebRtc

        m, motor = self._correr_ruido(DetetorOpenWakeWord(MODELO_ATIVACAO), VadWebRtc())
        self._nada_aconteceu(m, motor)


class _VadDeEnergia:
    """Fala = energia acima de um limiar (o ruido de teste passa: pior caso)."""

    def e_fala(self, pedaco: bytes) -> bool:
        amostras = struct.unpack(f"<{len(pedaco) // 2}h", pedaco[: BYTES_POR_CHUNK])
        return max(abs(a) for a in amostras) > 500


class TestCanalFalsoEOProjetoCerto(_Base):
    def test_projeto_nomeado_e_o_que_recebe_nunca_o_outro(self) -> None:
        dito = "no atlas acrescenta um teste ao login"
        resposta = resposta_llm("ditar_prompt", "atlas", "Acrescenta um teste ao login.")
        m = self.correr_ficheiros([dito, "sim, envia"], [resposta])
        self.assertEqual(m.canal.recebidos, [("atlas", "Acrescenta um teste ao login.")])

    def test_sem_canal_o_sim_nao_envia(self) -> None:
        textos = {1000: DITADO, 1001: "sim"}
        wavs = [escrever_tom(self.pasta, f"f-{valor}", valor) for valor in textos]
        m = Montagem([RESPOSTA_DO_DITADO], canal=None, relogio=time.perf_counter)
        ouvido = app.construir_ouvido(m.jarvis, motor=MotorPorTom(textos), wavs=wavs)
        app.correr(m.jarvis, ouvido, com_voz=False, medir=lambda: None)
        self.assertIn("o prompt confirmado NAO foi enviado", m.log.texto())


# --- scripts/medir_ponta_a_ponta.py: as metas e a classificacao, sem motores --------


class TestMetasDaMedicao(unittest.TestCase):
    def _resultado(self, **ajustes):
        base = dict(
            voltas=2,
            horas_ms=[700.0, 800.0],
            recap_ms=[900.0, 1000.0],
            sinal_de_vida_ms=[150.0] * 6,
            confirmados=2,
            recebidos=[("atlas", "a"), ("atlas", "b")],
            arranque_s=12.0,
        )
        base.update(ajustes)
        return medir_ponta_a_ponta.Resultado(**base)

    def test_dentro_das_metas_nao_ha_falhas(self) -> None:
        self.assertEqual(medir_ponta_a_ponta.avaliar(self._resultado()), [])

    def test_cada_meta_falha_sozinha(self) -> None:
        casos = {
            "horas -> resposta falada: p50": dict(horas_ms=[1300.0, 1300.0]),
            "horas -> resposta falada: p95": dict(horas_ms=[700.0, 2100.0]),
            "ditado -> recap falado: p50": dict(recap_ms=[2600.0, 2600.0]),
            "ditado -> recap falado: p95": dict(recap_ms=[900.0, 4100.0]),
            "primeiro sinal de vida": dict(sinal_de_vida_ms=[150.0, 1001.0]),
            "arranque": dict(arranque_s=31.0),
            "horas: so 1 de 2": dict(horas_ms=[700.0]),
            "sim ao recap: so 1 de 2": dict(confirmados=1, recebidos=[("atlas", "a")]),
            "projeto errado": dict(recebidos=[("atlas", "a"), ("orbita", "b")]),
            "recebeu 3 prompt(s) para 2": dict(recebidos=[("atlas", "a")] * 3),
        }
        for esperado, ajustes in casos.items():
            with self.subTest(esperado=esperado):
                falhas = medir_ponta_a_ponta.avaliar(self._resultado(**ajustes))
                self.assertTrue(any(esperado in falha for falha in falhas), falhas)

    def test_sem_pronto_falha(self) -> None:
        falhas = medir_ponta_a_ponta.avaliar(self._resultado(arranque_s=None))
        self.assertIn("arranque: nao chegou a PRONTO", falhas)

    def test_classificar_pelo_que_o_jarvis_percebeu(self) -> None:
        def medida(numero, **campos):
            return app.MedidaDaFrase(numero=numero, texto="t", gatilho="tecla", fim_da_fala=0.0, **campos)

        medidas = [
            medida(1, sinal_de_vida_ms=100.0, primeira_fala_ms=700.0, intencao="horas", desfecho="executado"),
            medida(2, sinal_de_vida_ms=120.0, primeira_fala_ms=950.0, intencao="ditar_prompt", projeto="atlas", desfecho="pendente"),
            medida(3, sinal_de_vida_ms=90.0, primeira_fala_ms=60.0, resposta_ao_recap=True, desfecho="executado"),
            medida(4, sinal_de_vida_ms=110.0, primeira_fala_ms=900.0, intencao="ditar_prompt", projeto=None, desfecho="pendente"),
        ]
        resultado = medir_ponta_a_ponta.classificar(medidas, voltas=1)
        self.assertEqual(resultado.horas_ms, [700.0])
        self.assertEqual(resultado.recap_ms, [950.0], "ditado sem projeto nao conta como recap medido")
        self.assertEqual(resultado.confirmados, 1)
        self.assertEqual(len(resultado.nao_percebidas), 1)
        self.assertEqual(resultado.sinal_de_vida_ms, [100.0, 120.0, 90.0, 110.0])

    def test_percentil_sem_interpolar(self) -> None:
        self.assertEqual(medir_ponta_a_ponta.percentil([1.0, 2.0, 3.0, 4.0], 50), 2.0)
        self.assertEqual(medir_ponta_a_ponta.percentil([1.0, 2.0, 3.0, 4.0], 95), 4.0)


class TestWavsDaMedicao(unittest.TestCase):
    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        original = medir_ponta_a_ponta.PASTA_DOS_WAV
        medir_ponta_a_ponta.PASTA_DOS_WAV = self.pasta
        self.addCleanup(setattr, medir_ponta_a_ponta, "PASTA_DOS_WAV", original)

    def test_em_cache_nao_arranca_processo_nenhum(self) -> None:
        for tipo in medir_ponta_a_ponta.TIPOS:
            escrever_tom(self.pasta, f"pt-{tipo}", 1)

        def nunca(*_a, **_k):
            raise AssertionError("com os WAV em cache nao se gera nada")

        wavs = medir_ponta_a_ponta.gerar_wavs("pt", correr=nunca)
        self.assertEqual(set(wavs), set(medir_ponta_a_ponta.TIPOS))

    def test_gera_num_processo_filho_com_argv_e_sem_shell(self) -> None:
        chamadas = []

        class Saida:
            returncode = 0
            stdout = "wav horas, sintese 1: 'x' -> serve"
            stderr = ""

        def correr(comando, **opcoes):
            chamadas.append((comando, opcoes))
            for tipo in medir_ponta_a_ponta.TIPOS:
                escrever_tom(self.pasta, f"pt-{tipo}", 1)
            return Saida()

        with contextlib.redirect_stdout(io.StringIO()):
            medir_ponta_a_ponta.gerar_wavs("pt", correr=correr)
        comando, opcoes = chamadas[0]
        self.assertIsInstance(comando, list)
        self.assertIn("--preparar-wavs", comando)
        self.assertNotIn("shell", opcoes)

    def test_filho_que_falha_e_um_erro_legivel(self) -> None:
        class Saida:
            returncode = 1
            stdout = "nenhuma sintese percebida para: sim"
            stderr = ""

        with self.assertRaises(RuntimeError) as contexto, contextlib.redirect_stdout(io.StringIO()):
            medir_ponta_a_ponta.gerar_wavs("pt", correr=lambda *_a, **_k: Saida())
        self.assertIn("nenhuma sintese percebida", str(contexto.exception))


if __name__ == "__main__":
    unittest.main()
