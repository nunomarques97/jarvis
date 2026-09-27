r"""Testes de interromper o jarvis a falar (jarvis/ouvido.py, jarvis/voz.py, jarvis/app.py).

Enquanto o jarvis fala, o ouvido ouve por cima da voz com o VAD Silero; a
primeira fala pausa a voz e a frase dita por cima decide se ela continua
(ruido, tosse, "yeah", "uh-huh") ou para de vez (fala a serio, que segue como
o pedido seguinte sem palavra de ativacao). O que estes testes protegem:

  * o ouvido: a pausa e pedida ao terceiro chunk com fala, com o instante do
    primeiro (bem dentro dos 300 ms); a frase leva os 300 ms antes da fala;
    so ha interrupcao com a voz a falar e com as tres pecas; uma frase que se
    perde (nada transcrito, fila cheia, tecla, falha) retoma a voz;
  * a voz: pausar para a reproducao e devolve o instante, retomar continua a
    mesma frase um pouco atras, calar durante a pausa acaba-a;
  * o jarvis: falsa retoma, a serio para e segue, o resto fica no ecra; o
    recap (um "sim" por cima nunca envia; "aborta" por cima cancela), os
    avisos e uma resposta geral a meio portam-se bem; uma pausa sem frase
    retoma sozinha; `[escuta] interromper = false` e sem voz desligam tudo;
  * as linhas do log sao as que `scripts/sessao_naturalidade.py` le;
  * o VAD Silero real (quando o ficheiro existe): silencio e ruido fraco nao
    sao fala.

Nenhum teste toca som nem abre o microfone.

    .venv\Scripts\python -m unittest tests.test_interrupcao -v
"""

from __future__ import annotations

import datetime
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import app, voz
from jarvis.app import Jarvis, construir_ouvido, e_so_acompanhamento
from jarvis.avisos import FALADO, Aviso
from jarvis.config import ConfigEscuta
from jarvis.interprete import Interprete
from jarvis.ouvido import (
    BYTES_POR_CHUNK,
    CHUNKS_ANTES_DA_INTERRUPCAO,
    CHUNKS_PARA_INTERROMPER,
    DURACAO_DO_CHUNK_S,
    FRASES_EM_ESPERA,
    GATILHO_INTERRUPCAO,
    GATILHO_TECLA,
    INTERRUPCAO_PAUSAR,
    INTERRUPCAO_RETOMAR,
    SILENCIO_FINAL_S,
    FonteDeFicheiro,
    Frase,
    Ouvido,
)
from jarvis.resposta_falada import rotulo_da_origem
from jarvis.voz import MOTIVO_SILENCIADO, FalaResidente, ResultadoFala, ResultadoSilencio
from tests.test_app import CanalFalso, LlmFalso, LogFalso, config_de_teste, resposta_llm
from tests.test_ouvido import SILENCIO, DetetorFalso, MotorFalso, TeclaFixa, VadFalso, chunk
from tests.test_pergunta_geral import (
    LINHA_DE_ARRANQUE,
    LINHA_DE_MENSAGEM,
    ArranqueFalso,
    linha_de_texto,
    perguntas_de_teste,
    resultado_json,
)

ESPERA_S = 5.0
FALA = 0x55
TOSSE = 0x66


# --- Ouvido ----------------------------------------------------------------------


class VadPorValor:
    """Chama fala aos chunks com um destes valores (o Silero falso)."""

    def __init__(self, valores=(FALA,)) -> None:
        self.valores = set(valores)
        self.reinicios = 0

    def e_fala(self, pedaco: bytes) -> bool:
        return pedaco[0] in self.valores

    def reiniciar(self) -> None:
        self.reinicios += 1


class RelogioDeChunks:
    """Avanca 30 ms por chunk processado (o ritmo do microfone)."""

    def __init__(self) -> None:
        self.agora = 100.0

    def __call__(self) -> float:
        return self.agora


class _BaseOuvido(unittest.TestCase):
    def montar(self, *, texto="stop there", vad_da_interrupcao=None, com_callback=True, detetor=True):
        self.relogio = RelogioDeChunks()
        self.eventos: list[tuple[str, float, float]] = []
        self.frases: list[Frase] = []
        self.linhas: list[str] = []
        self.motor = MotorFalso(texto)
        self.vad_int = vad_da_interrupcao if vad_da_interrupcao is not None else VadPorValor()
        self.tecla = TeclaFixa()
        self.ouvido = Ouvido(
            FonteDeFicheiro(b""),
            self.motor,
            self.frases.append,
            tecla=self.tecla,
            detetor=DetetorFalso() if detetor else None,
            vad=VadFalso(),
            lingua="en",
            escrever=self.linhas.append,
            relogio=self.relogio,
            vad_da_interrupcao=self.vad_int,
            ao_interromper=(lambda evento, instante: self.eventos.append((evento, instante, self.relogio())))
            if com_callback
            else None,
        )
        return self.ouvido

    def passar(self, valor: int, n: int = 1, premida: bool = False) -> None:
        for _ in range(n):
            self.relogio.agora += DURACAO_DO_CHUNK_S
            self.ouvido.processar(chunk(valor), premida)


class TestOuvidoInterrompe(_BaseOuvido):
    def test_a_pausa_e_pedida_ao_terceiro_chunk_com_o_instante_do_primeiro(self) -> None:
        ouvido = self.montar()
        ouvido.definir_voz_a_falar(True)
        self.passar(SILENCIO, 20)
        self.passar(FALA, 1)
        primeiro = self.relogio.agora
        self.passar(FALA, CHUNKS_PARA_INTERROMPER - 1)
        self.assertEqual(len(self.eventos), 1)
        evento, instante, pedido_em = self.eventos[0]
        self.assertEqual(evento, INTERRUPCAO_PAUSAR)
        self.assertEqual(instante, primeiro)
        # Do primeiro chunk com fala ao pedido de pausa: dois chunks (60 ms), longe dos 300 ms.
        self.assertLessEqual((pedido_em - instante) * 1000, 100)
        self.assertEqual(ouvido.estado, "a_falar")

    def test_a_frase_por_cima_leva_o_audio_de_antes_e_segue_sem_palavra_de_ativacao(self) -> None:
        ouvido = self.montar(texto="Hey Jarvis, stop there.")
        ouvido.definir_voz_a_falar(True)
        self.passar(0x11, 20)  # o que se ouve antes (ruido que o Silero nao chama fala)
        self.passar(FALA, 10)
        self.passar(SILENCIO, round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S))
        ouvido.transcrever_pendentes()
        self.assertEqual(len(self.frases), 1)
        frase = self.frases[0]
        self.assertEqual(frase.gatilho, GATILHO_INTERRUPCAO)
        self.assertEqual(frase.texto, "stop there.")
        audio = self.motor.recebidos[0]
        # Os 300 ms antes da fala entram (menos os chunks de fala ja contados neles).
        antes = CHUNKS_ANTES_DA_INTERRUPCAO - CHUNKS_PARA_INTERROMPER
        self.assertTrue(audio.startswith(chunk(0x11) * antes + chunk(FALA)))
        self.assertEqual(audio.count(chunk(0x11)), antes)

    def test_sem_a_voz_a_falar_nao_ha_interrupcao(self) -> None:
        ouvido = self.montar()
        self.passar(FALA, 20)
        self.assertEqual(self.eventos, [])
        self.assertEqual(ouvido.estado, "repouso")
        ouvido.definir_voz_a_falar(True)
        self.passar(FALA, 2)
        ouvido.definir_voz_a_falar(False)  # a voz acabou a meio: a contagem recomeca
        self.passar(FALA, 1)
        ouvido.definir_voz_a_falar(True)
        self.passar(FALA, 2)
        self.assertEqual(self.eventos, [])
        self.passar(FALA, 1)
        self.assertEqual([e[0] for e in self.eventos], [INTERRUPCAO_PAUSAR])

    def test_chunks_soltos_com_fala_nao_interrompem(self) -> None:
        ouvido = self.montar()
        ouvido.definir_voz_a_falar(True)
        for _ in range(10):
            self.passar(FALA, CHUNKS_PARA_INTERROMPER - 1)
            self.passar(TOSSE, 1)
        self.assertEqual(self.eventos, [])

    def test_sem_as_tres_pecas_nao_ha_interrupcao(self) -> None:
        for kw in ({"com_callback": False},):
            ouvido = self.montar(**kw)
            ouvido.definir_voz_a_falar(True)
            self.passar(FALA, 10)
            self.assertFalse(ouvido.interromper_ligado)
            self.assertNotEqual(ouvido.estado, "a_falar")
        ouvido = Ouvido(FonteDeFicheiro(b""), MotorFalso(), lambda f: None, tecla=TeclaFixa())
        ouvido.definir_voz_a_falar(True)
        for _ in range(10):
            ouvido.processar(chunk(FALA), False)
        self.assertEqual(ouvido.estado, "repouso")

    def test_nada_transcrito_retoma_a_voz(self) -> None:
        ouvido = self.montar(texto="")
        ouvido.definir_voz_a_falar(True)
        self.passar(TOSSE if False else FALA, 4)
        self.passar(SILENCIO, round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S))
        ouvido.transcrever_pendentes()
        self.assertEqual([e[0] for e in self.eventos], [INTERRUPCAO_PAUSAR, INTERRUPCAO_RETOMAR])
        self.assertEqual(self.frases, [])

    def test_transcricao_falhada_retoma_a_voz(self) -> None:
        ouvido = self.montar()
        self.motor.falhar = True
        ouvido.definir_voz_a_falar(True)
        self.passar(FALA, 4)
        self.passar(SILENCIO, round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S))
        ouvido.transcrever_pendentes()
        self.assertEqual([e[0] for e in self.eventos], [INTERRUPCAO_PAUSAR, INTERRUPCAO_RETOMAR])

    def test_fila_cheia_retoma_a_voz(self) -> None:
        ouvido = self.montar()
        for _ in range(FRASES_EM_ESPERA):
            ouvido._fila.put_nowait(None)  # noqa: SLF001 - enche a fila de proposito
        ouvido.definir_voz_a_falar(True)
        self.passar(FALA, 4)
        self.passar(SILENCIO, round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S))
        self.assertEqual([e[0] for e in self.eventos], [INTERRUPCAO_PAUSAR, INTERRUPCAO_RETOMAR])

    def test_a_tecla_a_meio_retoma_a_voz_e_segue_a_tecla(self) -> None:
        ouvido = self.montar()
        ouvido.definir_voz_a_falar(True)
        self.passar(FALA, 4)
        self.passar(FALA, 10, premida=True)
        self.assertEqual([e[0] for e in self.eventos], [INTERRUPCAO_PAUSAR, INTERRUPCAO_RETOMAR])
        self.assertEqual(ouvido.estado, "tecla")

    def test_um_callback_que_rebenta_nunca_para_a_escuta(self) -> None:
        ouvido = self.montar()

        def rebenta(evento, instante):
            raise RuntimeError("de proposito")

        ouvido.ao_interromper = rebenta
        ouvido.definir_voz_a_falar(True)
        self.passar(FALA, 4)
        self.passar(SILENCIO, round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S))
        ouvido.transcrever_pendentes()
        self.assertEqual(len(self.frases), 1)
        self.assertTrue(any("tratamento da interrupcao" in linha for linha in self.linhas))


# --- Voz: pausar e retomar a frase ------------------------------------------------


class MotorDeBlocos:
    """Motor de sintese falso: um bloco de 1 s por pedaco, com valores diferentes."""

    taxa = 1000

    def __init__(self, blocos: int = 2) -> None:
        self.blocos = blocos

    def sintetizar(self, texto):
        for i in range(self.blocos):
            yield bytes([i + 1]) * (self.taxa * 2)


class SaidaLenta:
    """Saida de som falsa: cada escrita leva o tempo do audio (a 1 kHz), e guarda tudo."""

    def __init__(self) -> None:
        self.escritos: list[bytes] = []
        self.descartes = 0

    def escrever(self, taxa: int, dados: bytes) -> None:
        self.escritos.append(dados)
        time.sleep(len(dados) / 2 / taxa)

    def descartar(self) -> None:
        self.descartes += 1


class TestVozPausa(unittest.TestCase):
    def falar_em_fundo(self, fala: FalaResidente) -> threading.Thread:
        fio = threading.Thread(target=fala.play, daemon=True)
        fio.start()
        fim = time.perf_counter() + ESPERA_S
        while fala.instante_do_primeiro_audio is None and time.perf_counter() < fim:
            time.sleep(0.002)
        return fio

    def test_pausar_para_a_reproducao_e_retomar_continua_um_pouco_atras(self) -> None:
        saida = SaidaLenta()
        fala = FalaResidente(MotorDeBlocos(), saida)
        fala.feed("x")
        fio = self.falar_em_fundo(fala)
        time.sleep(0.2)
        pedido = time.perf_counter()
        parada = fala.pausar()
        self.assertIsNotNone(parada)
        self.assertLess((parada - pedido) * 1000, 300)
        self.assertTrue(fala.em_pausa)
        self.assertEqual(saida.descartes, 1)
        escritos = len(saida.escritos)
        time.sleep(0.15)
        self.assertEqual(len(saida.escritos), escritos, "nada se escreve em pausa")
        self.assertTrue(fala.retomar())
        fio.join(ESPERA_S)
        self.assertFalse(fio.is_alive())
        tocado = b"".join(saida.escritos)
        # Os dois blocos inteiros tocaram, e parte do primeiro repetiu-se depois da pausa.
        self.assertGreater(tocado.count(b"\x01"), MotorDeBlocos.taxa * 2)
        self.assertEqual(tocado.count(b"\x02"), MotorDeBlocos.taxa * 2)

    def test_calar_durante_a_pausa_acaba_a_frase(self) -> None:
        saida = SaidaLenta()
        fala = FalaResidente(MotorDeBlocos(), saida)
        fala.feed("x")
        fio = self.falar_em_fundo(fala)
        self.assertIsNotNone(fala.pausar())
        fala.matar_agora()
        fala.stop()
        fio.join(1.0)
        self.assertFalse(fio.is_alive())
        self.assertNotIn(b"\x02", b"".join(saida.escritos))

    def test_uma_pausa_que_nao_chega_a_tempo_e_desfeita_e_a_frase_nunca_fica_parada(self) -> None:
        saida = SaidaLenta()
        fala = FalaResidente(MotorDeBlocos(), saida)
        fala.feed("x")
        fio = self.falar_em_fundo(fala)
        self.assertIsNone(fala.pausar(0.0))
        self.assertFalse(fala.em_pausa)
        fio.join(ESPERA_S)
        self.assertFalse(fio.is_alive(), "a frase ficou parada depois de uma pausa desfeita")
        self.assertEqual(b"".join(saida.escritos).count(b""), MotorDeBlocos.taxa * 2)

    def test_pausar_sem_nada_a_tocar_devolve_none(self) -> None:
        fala = FalaResidente(MotorDeBlocos(), SaidaLenta())
        self.assertIsNone(fala.pausar())
        self.assertFalse(fala.retomar())
        with mock.patch.object(voz, "_stream_ativo", None):
            self.assertIsNone(voz.pausar_agora())
            self.assertFalse(voz.retomar_agora())

    def test_pausar_agora_e_retomar_agora_usam_a_frase_ativa(self) -> None:
        saida = SaidaLenta()
        fala = FalaResidente(MotorDeBlocos(blocos=1), saida)
        fala.feed("x")
        fio = self.falar_em_fundo(fala)
        with mock.patch.object(voz, "_stream_ativo", fala):
            self.assertIsNotNone(voz.pausar_agora())
            self.assertTrue(voz.retomar_agora())
        fio.join(ESPERA_S)
        self.assertFalse(fio.is_alive())


# --- Jarvis: decidir a frase dita por cima -------------------------------------------


class VozFalsa:
    """Faz de `jarvis.voz`: com `presa` ligada cada fala so acaba quando a soltam ou calam."""

    def __init__(self) -> None:
        self.falados: list[str] = []
        self.pausas = 0
        self.retomadas = 0
        self.calados: list[str] = []
        self.presa = threading.Event()
        self.a_falar = threading.Event()
        self.em_pausa = False
        self._calada = threading.Event()
        self._tranca = threading.Lock()

    def falar(self, texto: str) -> ResultadoFala:
        with self._tranca:
            self.falados.append(texto)
            self._calada.clear()
        self.a_falar.set()
        try:
            while self.presa.is_set() or self.em_pausa:
                if self._calada.is_set():
                    break
                time.sleep(0.002)
            if self._calada.is_set():
                return ResultadoFala(falou=False, motivo_falha=MOTIVO_SILENCIADO)
            return ResultadoFala(falou=True, primeiro_audio=time.perf_counter())
        finally:
            self.a_falar.clear()

    def pausar(self) -> float | None:
        if not self.a_falar.is_set():
            return None
        self.em_pausa = True
        self.pausas += 1
        return time.perf_counter()

    def retomar(self) -> bool:
        if not self.em_pausa:
            return False
        self.em_pausa = False
        self.retomadas += 1
        return True

    def calar(self, motivo, *, definitivo, registar=None) -> ResultadoSilencio:
        self.calados.append(motivo)
        self._calada.set()
        self.em_pausa = False
        agora = datetime.datetime.now()
        return ResultadoSilencio(agora, agora, 0.0, True, True, definitivo, motivo)

    def esperar_fala(self, texto: str | None = None) -> bool:
        fim = time.perf_counter() + ESPERA_S
        while time.perf_counter() < fim:
            if self.a_falar.is_set() and (texto is None or (self.falados and texto in self.falados[-1])):
                return True
            time.sleep(0.002)
        return False


class Cena:
    """Um Jarvis com a thread das frases a correr, voz falsa que bloqueia e relogio real."""

    def __init__(self, respostas_llm=None, *, perguntas=None, escuta=None, com_voz=True) -> None:
        self.log = LogFalso()
        self.config = config_de_teste("en", escuta=escuta)
        self.interprete = Interprete(self.config, cliente=LlmFalso(respostas_llm))
        self.canal = CanalFalso()
        self.voz = VozFalsa()
        self.locais: list[str] = []

        def executar_local(intencao, projeto, config, *, detalhe=None, lingua="pt"):
            self.locais.append(intencao)
            from jarvis.acoes_locais import ResultadoAcao

            return ResultadoAcao(nome_acao=intencao, executou=True, texto=f"Local answer {len(self.locais)}.")

        self.jarvis = Jarvis(
            self.config,
            self.log,
            interprete=self.interprete,
            canal=self.canal,
            perguntas=perguntas,
            falar=self.voz.falar,
            calar=self.voz.calar,
            pausar=self.voz.pausar,
            retomar=self.voz.retomar,
            executar_local=executar_local,
            com_voz=com_voz,
            relogio=time.perf_counter,
            espera_do_aviso_s=ESPERA_S,
        )
        self.jarvis.iniciar()

    def fechar(self) -> None:
        self.voz.presa.clear()
        self.jarvis.fechar()

    def ouvir(self, texto: str, *, gatilho: str = GATILHO_TECLA) -> None:
        agora = time.perf_counter()
        self.jarvis.ao_ouvir(
            Frase(
                texto=texto,
                gatilho=gatilho,
                lingua="en",
                motor="motor-falso",
                duracao_audio_s=1.0,
                inicio_da_escuta=agora,
                fim_da_escuta=agora,
                texto_pronto=agora,
                latencia_stt_ms=100.0,
                ultima_voz=agora,
            )
        )

    def interromper(self, texto: str | None, *, antes_s: float = 0.06, soltar: bool = False) -> None:
        """A fala por cima da voz: a pausa (com o inicio `antes_s` atras) e depois a frase ou nada.

        Com `soltar`, as falas seguintes deixam de ficar presas (a pausada so
        acaba quando for retomada ou calada).
        """
        self.jarvis.ao_interromper(INTERRUPCAO_PAUSAR, time.perf_counter() - antes_s)
        if soltar:
            if not self.voz.em_pausa:
                raise AssertionError("a voz nao ficou em pausa antes de a soltar")
            self.voz.presa.clear()
        if texto is None:
            self.jarvis.ao_interromper(INTERRUPCAO_RETOMAR, time.perf_counter())
        else:
            self.ouvir(texto, gatilho=GATILHO_INTERRUPCAO)

    def texto(self) -> str:
        return self.log.texto()

    def ocioso(self) -> bool:
        return self.jarvis.esperar_ocioso(ESPERA_S)


class _BaseCena(unittest.TestCase):
    def cena(self, *a, **kw) -> Cena:
        cena = Cena(*a, **kw)
        self.addCleanup(cena.fechar)
        return cena

    def a_falar(self, cena: Cena, pedido: str = "what time is it", texto: str | None = None) -> None:
        cena.voz.presa.set()
        cena.ouvir(pedido)
        self.assertTrue(cena.voz.esperar_fala(texto), "a voz nunca comecou a falar")


class TestJarvisInterrompido(_BaseCena):
    def test_um_acompanhamento_retoma_a_voz_e_nao_e_um_pedido(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.interromper("Yeah.")
        self.assertEqual((cena.voz.pausas, cena.voz.retomadas), (1, 1))
        self.assertFalse(cena.voz.em_pausa)
        cena.voz.presa.clear()
        self.assertTrue(cena.ocioso())
        self.assertEqual(cena.jarvis.frases, 1, "o 'yeah' nao conta como frase")
        self.assertEqual(cena.locais, ["horas"])
        self.assertEqual(cena.voz.calados, [])
        self.assertIn("interrupcao | falsa: so um acompanhamento", cena.texto())
        self.assertRegex(cena.texto(), r"interrupcao \| voz parada: \d+ ms desde o inicio da fala \| inicio da fala: ")

    def test_ruido_ou_tosse_sem_texto_retoma_a_voz(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.interromper(None)
        self.assertEqual(cena.voz.retomadas, 1)
        self.assertIn("interrupcao | falsa: nada transcrito", cena.texto())
        for so_hesitacoes in ("Uh-huh.", "Mm-hmm.", "Uh, um.", "Right, I see."):
            with self.subTest(so_hesitacoes):
                cena.interromper(so_hesitacoes)
        self.assertEqual(cena.voz.retomadas, 5)
        cena.voz.presa.clear()
        self.assertTrue(cena.ocioso())
        self.assertEqual(cena.jarvis.frases, 1)

    def test_fala_a_serio_para_a_voz_de_vez_e_segue_como_pedido_sem_palavra_de_ativacao(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.interromper("What's the date today?", soltar=True)
        self.assertTrue(cena.ocioso())
        self.assertEqual(len(cena.voz.calados), 1)
        self.assertIn("interrupcao", cena.voz.calados[0])
        self.assertEqual(cena.voz.retomadas, 0)
        self.assertEqual(cena.locais, ["horas", "horas"], "a frase por cima foi tratada")
        texto = cena.texto()
        self.assertIn("interrupcao | a serio:", texto)
        self.assertIn("fala por cima da voz do jarvis (sem palavra de ativacao)", texto)
        # O que estava a ser dito fica no ecra; a fala cortada nao marca erro.
        self.assertIn("ecra | Local answer 1.", texto)
        self.assertIn("interrompida por quem falou por cima", texto)
        self.assertNotIn("voz falhou", texto)

    def test_duas_falas_por_cima_so_retoma_quando_ambas_nao_contam(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.jarvis.ao_interromper(INTERRUPCAO_PAUSAR, time.perf_counter())
        cena.jarvis.ao_interromper(INTERRUPCAO_PAUSAR, time.perf_counter())
        cena.ouvir("Yeah.", gatilho=GATILHO_INTERRUPCAO)
        self.assertEqual(cena.voz.retomadas, 0, "ainda ha outra fala por decidir")
        cena.ouvir("Uh-huh.", gatilho=GATILHO_INTERRUPCAO)
        self.assertEqual(cena.voz.retomadas, 1)

    def test_uma_pausa_sem_frase_retoma_sozinha(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.jarvis.ao_interromper(INTERRUPCAO_PAUSAR, time.perf_counter())
        self.assertTrue(cena.voz.em_pausa)
        with mock.patch.object(app, "PAUSA_SEM_FRASE_S", 0.05):
            time.sleep(0.1)
            cena.jarvis.verificar_tempo()
        self.assertFalse(cena.voz.em_pausa)
        self.assertIn("interrupcao | falsa: nenhuma frase chegou", cena.texto())

    def test_fala_a_serio_com_a_voz_ainda_a_tocar_sem_pausa_tambem_a_para(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.jarvis._pausar = lambda: None  # a pausa nao chegou a tempo
        cena.jarvis.ao_interromper(INTERRUPCAO_PAUSAR, time.perf_counter())
        # A voz continua presa (a tocar) ate a frase por cima a calar.
        cena.ouvir("What's the date today?", gatilho=GATILHO_INTERRUPCAO)
        cena.voz.presa.clear()
        self.assertTrue(cena.ocioso())
        self.assertEqual(len(cena.voz.calados), 1)
        self.assertEqual(cena.locais, ["horas", "horas"])

    def test_a_voz_que_ja_acabou_nao_e_retomada_e_a_frase_segue(self) -> None:
        cena = self.cena()
        cena.interromper("What time is it?")
        self.assertTrue(cena.ocioso())
        self.assertEqual((cena.voz.pausas, cena.voz.retomadas, cena.voz.calados), (0, 0, []))
        self.assertIn("a voz ja nao estava a tocar", cena.texto())
        self.assertEqual(cena.locais, ["horas"])

    def test_cala_te_por_cima_segue_o_caminho_do_cala_te(self) -> None:
        cena = self.cena()
        self.a_falar(cena)
        cena.interromper("Be quiet.", soltar=True)
        self.assertTrue(cena.ocioso())
        # So o silencio do proprio cala-te; a interrupcao nao junta outro.
        self.assertTrue(cena.voz.calados)
        self.assertFalse(any("interrupcao" in motivo for motivo in cena.voz.calados), cena.voz.calados)
        self.assertEqual(cena.voz.retomadas, 0)
        self.assertTrue(cena.jarvis.estado.mudo)


class TestRecapInterrompido(_BaseCena):
    DITADO = resposta_llm("ditar_prompt", "atlas", "Fix the login test.")

    def test_um_sim_por_cima_do_recap_nunca_envia_e_o_recap_acaba(self) -> None:
        cena = self.cena([self.DITADO])
        self.a_falar(cena, "tell atlas to fix the login test")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        cena.interromper("Yes.")
        self.assertEqual(cena.voz.retomadas, 1)
        self.assertIn("interrupcao | retomada: confirmacao dita durante o recap", cena.texto())
        self.assertNotIn("interrupcao | falsa", cena.texto())
        cena.voz.presa.clear()
        self.assertTrue(cena.ocioso())
        self.assertEqual(cena.canal.recebidos, [], "o sim dito por cima do recap nao envia")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        # Depois de o recap ser ouvido, o sim envia como sempre.
        cena.ouvir("Yes.")
        self.assertTrue(cena.ocioso())
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_aborta_por_cima_do_recap_para_o_recap_e_cancela(self) -> None:
        cena = self.cena([self.DITADO])
        self.a_falar(cena, "tell atlas to fix the login test")
        cena.interromper("Abort.", soltar=True)
        self.assertTrue(cena.ocioso())
        self.assertEqual(len(cena.voz.calados), 1)
        self.assertEqual(cena.canal.recebidos, [])
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        self.assertIn("desfecho: cancelado", cena.texto())

    def test_um_yeah_por_cima_do_recap_tambem_nao_envia(self) -> None:
        cena = self.cena([self.DITADO])
        self.a_falar(cena, "tell atlas to fix the login test")
        cena.interromper("Yeah.")
        self.assertEqual(cena.voz.retomadas, 1)
        cena.voz.presa.clear()
        self.assertTrue(cena.ocioso())
        self.assertEqual(cena.canal.recebidos, [])


class TestAvisoInterrompido(_BaseCena):
    def aviso_em_fundo(self, cena: Cena) -> tuple[threading.Thread, list[str]]:
        resultado: list[str] = []
        aviso = Aviso("atlas", "Stop", "atlas finished.", ("atlas", "s1"), time.perf_counter())
        cena.voz.presa.set()
        fio = threading.Thread(target=lambda: resultado.append(cena.jarvis._entregar_aviso(aviso)), daemon=True)
        fio.start()
        self.assertTrue(cena.voz.esperar_fala("atlas finished."))
        return fio, resultado

    def test_aviso_interrompido_a_serio_para_nao_se_repete_e_a_frase_segue(self) -> None:
        cena = self.cena()
        fio, resultado = self.aviso_em_fundo(cena)
        cena.interromper("What time is it?", soltar=True)
        fio.join(ESPERA_S)
        self.assertEqual(resultado, [FALADO])
        self.assertTrue(cena.ocioso())
        self.assertEqual(cena.locais, ["horas"])
        self.assertEqual(cena.voz.falados, ["atlas finished.", "Local answer 1."])
        self.assertIn("ecra | atlas finished.", cena.texto())

    def test_aviso_com_um_acompanhamento_continua(self) -> None:
        cena = self.cena()
        fio, resultado = self.aviso_em_fundo(cena)
        cena.interromper("Uh-huh.")
        self.assertEqual(cena.voz.retomadas, 1)
        cena.voz.presa.clear()
        fio.join(ESPERA_S)
        self.assertEqual(resultado, [FALADO])
        self.assertEqual(cena.voz.falados, ["atlas finished."])
        self.assertEqual(cena.voz.calados, [])


class TestRespostaGeralInterrompida(_BaseCena):
    PRIMEIRA = "It is 22 degrees in Porto today."
    RESTO = "Tomorrow it will rain in the afternoon."

    def montar(self) -> tuple[Cena, queue.Queue, ArranqueFalso]:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        linhas: queue.Queue = queue.Queue()
        arranque = ArranqueFalso(linhas)
        perguntas = perguntas_de_teste(arranque, Path(temporaria.name) / "perguntas", lingua="en")
        cena = self.cena([resposta_llm("pergunta_geral", "", "what's the weather in Porto")], perguntas=perguntas)
        cena.voz.presa.set()
        cena.ouvir("what's the weather in Porto")
        for linha in (LINHA_DE_ARRANQUE, LINHA_DE_MENSAGEM, linha_de_texto(self.PRIMEIRA + " Tomorrow")):
            linhas.put(linha)
        self.assertTrue(cena.voz.esperar_fala(self.PRIMEIRA))
        return cena, linhas, arranque

    def acabar(self, linhas: queue.Queue) -> None:
        linhas.put(linha_de_texto(" it will rain in the afternoon."))
        linhas.put(resultado_json(f"{self.PRIMEIRA} {self.RESTO}"))
        linhas.put(None)

    def test_fala_a_serio_para_a_resposta_o_resto_nao_se_diz_e_aparece_no_ecra(self) -> None:
        cena, linhas, arranque = self.montar()
        cena.interromper("What time is it?", soltar=True)
        self.assertTrue(cena.ocioso())
        self.acabar(linhas)
        self.assertTrue(cena.jarvis.esperar_pergunta(ESPERA_S))
        self.assertFalse(arranque.ultimo.morto.is_set(), "a resposta interrompida continua a chegar")
        self.assertNotIn(self.RESTO, " ".join(cena.voz.falados))
        self.assertEqual(cena.locais, ["horas"])
        texto = cena.texto()
        self.assertIn("pergunta | resposta interrompida", texto)
        self.assertIn(f"{rotulo_da_origem('en')} '{self.PRIMEIRA} {self.RESTO}'", texto)

    def test_um_acompanhamento_deixa_a_resposta_continuar_inteira(self) -> None:
        cena, linhas, _arranque = self.montar()
        cena.interromper("Okay.")
        self.assertEqual(cena.voz.retomadas, 1)
        cena.voz.presa.clear()
        self.acabar(linhas)
        self.assertTrue(cena.jarvis.esperar_pergunta(ESPERA_S))
        self.assertTrue(cena.ocioso())
        self.assertIn(self.RESTO, " ".join(cena.voz.falados))
        self.assertEqual(cena.voz.calados, [])


class TestLigarEDesligar(unittest.TestCase):
    def jarvis(self, **kw) -> tuple[Jarvis, LogFalso]:
        log = LogFalso()
        escuta = kw.pop("escuta", None)
        config = config_de_teste("en", escuta=escuta)
        jarvis = Jarvis(config, log, interprete=Interprete(config, cliente=LlmFalso()), **kw)
        return jarvis, log

    def ouvido(self, jarvis: Jarvis) -> Ouvido:
        return construir_ouvido(
            jarvis,
            motor=MotorFalso(),
            fonte=FonteDeFicheiro(b""),
            tecla=TeclaFixa(),
            detetor=DetetorFalso(),
            vad=VadFalso(),
            vad_da_interrupcao=VadPorValor(),
        )

    def test_ligado_por_omissao(self) -> None:
        self.assertTrue(ConfigEscuta().interromper)
        jarvis, log = self.jarvis()
        ouvido = self.ouvido(jarvis)
        self.assertTrue(ouvido.interromper_ligado)
        self.assertIn("interromper | ligado", log.texto())

    def test_config_desliga(self) -> None:
        jarvis, log = self.jarvis(escuta=ConfigEscuta(interromper=False))
        ouvido = self.ouvido(jarvis)
        self.assertFalse(ouvido.interromper_ligado)
        self.assertIn("interromper | desligado: [escuta] interromper = false", log.texto())

    def test_sem_voz_desliga(self) -> None:
        # --sem-voz arranca o Jarvis com com_voz=False.
        jarvis, log = self.jarvis(com_voz=False)
        self.assertFalse(jarvis.interromper)
        ouvido = self.ouvido(jarvis)
        self.assertFalse(ouvido.interromper_ligado)
        self.assertIn("interromper | desligado: sem voz", log.texto())
        self.assertTrue(app.construir_parser().parse_args(["--sem-voz"]).sem_voz)

    def test_desligado_o_jarvis_nunca_diz_ao_ouvido_que_fala(self) -> None:
        jarvis, _log = self.jarvis(escuta=ConfigEscuta(interromper=False), falar=lambda t: ResultadoFala(True))
        chamadas: list[bool] = []
        jarvis.ouvido = mock.Mock(definir_voz_a_falar=chamadas.append)
        jarvis._dizer("Hello.")
        self.assertEqual(chamadas, [])
        ligado, _log = self.jarvis(falar=lambda t: ResultadoFala(True))
        ligado.ouvido = mock.Mock(definir_voz_a_falar=chamadas.append)
        ligado._dizer("Hello.")
        self.assertEqual(chamadas, [True, False])

    def test_modo_ficheiro_desliga(self) -> None:
        jarvis, log = self.jarvis()
        with tempfile.TemporaryDirectory() as pasta:
            wav = Path(pasta) / "a.wav"
            from jarvis.audio_util import escrever_wav_pcm16

            escrever_wav_pcm16(wav, b"\x00\x00" * 1600, 16000, 1)
            ouvido = construir_ouvido(jarvis, motor=MotorFalso(), wavs=[wav])
        self.assertFalse(ouvido.interromper_ligado)
        self.assertIn("interromper | desligado: modo ficheiro", log.texto())


class TestAcompanhamentos(unittest.TestCase):
    def test_so_acompanhamentos_e_hesitacoes(self) -> None:
        for texto in ("", "Yeah.", "Uh-huh.", "mm-hmm", "Okay.", "Right, I see.", "Got it.", "Sim.", "Pois."):
            with self.subTest(texto):
                self.assertTrue(e_so_acompanhamento(texto, "en"))

    def test_fala_a_serio(self) -> None:
        for texto in ("Okay, that's enough.", "Thank you.", "Stop.", "What time is it?", "Hey Jarvis.", "No."):
            with self.subTest(texto):
                self.assertFalse(e_so_acompanhamento(texto, "en"))


class TestContratoComASessaoDeNaturalidade(_BaseCena):
    def test_as_linhas_da_interrupcao_sao_as_que_o_relatorio_le(self) -> None:
        from tests.test_sessao_naturalidade import _carregar_script

        modulo = _carregar_script("sessao_naturalidade")

        cena = self.cena()
        self.a_falar(cena)
        cena.interromper("Yeah.", antes_s=0.12)
        cena.voz.presa.clear()
        self.assertTrue(cena.ocioso())
        hora = "2026-09-27 10:00:00.000"
        leitor = modulo.ler_log(f"{hora} {linha}" for linha in cena.log.linhas)
        latencias = [i.latencia_ms for i in leitor.interrupcoes if not i.falsa]
        self.assertEqual(len(latencias), 1)
        self.assertGreaterEqual(latencias[0], 100)
        self.assertEqual(sum(1 for i in leitor.interrupcoes if i.falsa), 1)


# --- VAD Silero real ----------------------------------------------------------------------


@unittest.skipUnless(
    (Path(__file__).resolve().parent.parent / "models" / "openwakeword" / "silero_vad.onnx").is_file(),
    "models/openwakeword/silero_vad.onnx em falta",
)
class TestSileroReal(unittest.TestCase):
    def test_silencio_e_ruido_fraco_nao_sao_fala_e_o_chunk_e_o_do_ouvido(self) -> None:
        import numpy as np

        from jarvis.vad_silero import VadSilero

        vad = VadSilero()
        self.assertFalse(any(vad.e_fala(b"\x00" * BYTES_POR_CHUNK) for _ in range(30)))
        vad.reiniciar()
        gerador = np.random.default_rng(3)
        ruido = [gerador.normal(0, 200, BYTES_POR_CHUNK // 2).astype("<i2").tobytes() for _ in range(30)]
        self.assertFalse(any(vad.e_fala(pedaco) for pedaco in ruido))

    def test_um_modelo_em_falta_e_um_erro_claro(self) -> None:
        from jarvis.vad_silero import VadSilero

        with self.assertRaises(FileNotFoundError):
            VadSilero(Path("models/nao-existe.onnx"))


if __name__ == "__main__":
    unittest.main()
