"""A bolinha ligada ao jarvis: estados, niveis, clique e falhas do processo filho.

Sem janela, sem som e sem microfone: o processo filho e falso (tubos do SO e um
stdin que regista, prende ou parte) e o painel, o ouvido e a voz sao os reais
com pecas falsas por baixo.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import app, voz
from jarvis import bolinha as bolinha_mod
from jarvis.app import A_ESPERA, A_FALAR, A_OUVIR, A_PENSAR, Painel, abrir_bolinha
from jarvis.bolinha import ESTADO_DO_PAINEL, LigacaoABolinha, PonteDaBolinha, nivel_de_pcm16
from jarvis.ouvido import (
    EVENTO_CAPTADA,
    EVENTO_DESCARTADA,
    EVENTO_ERRO,
    EVENTO_FIM,
    EVENTO_INICIO,
    FonteDeFicheiro,
    Ouvido,
)
from jarvis.interprete import Interprete
from tests.test_app import DITADO, LlmFalso, LogFalso, Montagem, config_de_teste
from tests.test_ouvido import MotorFalso, TeclaFixa, chunk
from tests.test_voz_quente import MotorRapidoFalso

ESTADOS_DA_BOLINHA = {"repouso", "ouvir", "pensar", "falar", "confirmar", "dormir", "erro"}


def pcm_constante(valor: int, amostras: int = 480) -> bytes:
    return int(valor).to_bytes(2, "little", signed=True) * amostras


def esperar_ate(condicao, limite_s: float = 3.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        threading.Event().wait(0.005)
    return condicao()


class Relogio:
    def __init__(self, agora: float = 100.0) -> None:
        self.agora = agora

    def __call__(self) -> float:
        return self.agora


class Registo:
    """Faz de ligacao: guarda as linhas e os niveis mandados a bolinha."""

    def __init__(self) -> None:
        self.linhas: list[str] = []
        self.niveis: list[float] = []

    def enviar(self, linha: str) -> None:
        self.linhas.append(linha)

    def enviar_nivel(self, valor: float) -> None:
        self.niveis.append(valor)

    def estados(self) -> list[str]:
        return [linha.split(" ", 1)[1] for linha in self.linhas if linha.startswith("estado ")]

    def legendas(self) -> list[str]:
        return [linha[len("legenda ") :] if linha != "legenda" else "" for linha in self.linhas if linha.startswith("legenda")]


def ponte_de_teste(relogio: Relogio | None = None, **opcoes) -> tuple[PonteDaBolinha, Registo]:
    registo = Registo()
    ponte = PonteDaBolinha(registo.enviar, registo.enviar_nivel, relogio=relogio or Relogio(), **opcoes)
    return ponte, registo


# --- O processo filho falso --------------------------------------------------------


class StdinFalso:
    """O stdin do filho: regista o que chega; pode prender (filho lento) ou partir."""

    def __init__(self) -> None:
        self.recebido = bytearray()
        self.solto = threading.Event()
        self.solto.set()
        self.partir = False
        self.escritas = 0
        self.fechado = threading.Event()
        self._tranca = threading.Lock()

    def write(self, dados: bytes) -> int:
        self.solto.wait(10.0)
        if self.partir:
            raise BrokenPipeError(32, "Broken pipe")
        with self._tranca:
            self.recebido.extend(dados)
            self.escritas += 1
        return len(dados)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.fechado.set()

    def linhas(self) -> list[str]:
        with self._tranca:
            return bytes(self.recebido).decode("utf-8").splitlines()


class FilhoFalso:
    """`subprocess.Popen` de mentira: stdout e stderr sao tubos reais do SO."""

    def __init__(self) -> None:
        self.stdin = StdinFalso()
        leitura, self._escrita = os.pipe()
        self.stdout = os.fdopen(leitura, "rb")
        leitura_erros, self._escrita_erros = os.pipe()
        self.stderr = os.fdopen(leitura_erros, "rb")
        self.morto = threading.Event()
        self.mortes = 0
        self.argv: list[str] | None = None
        self.opcoes: dict = {}

    def dizer(self, dados: bytes) -> None:
        os.write(self._escrita, dados)

    def dizer_no_stderr(self, dados: bytes) -> None:
        os.write(self._escrita_erros, dados)

    def rebentar(self) -> None:
        """O filho morre: os tubos de saida fecham."""
        self.morto.set()
        for fd in (self._escrita, self._escrita_erros):
            try:
                os.close(fd)
            except OSError:
                pass

    def fechar_leituras(self) -> None:
        for fluxo in (self.stdout, self.stderr):
            try:
                fluxo.close()
            except (OSError, ValueError):
                pass

    def wait(self, timeout: float | None = None) -> int:
        if not self.morto.wait(timeout):
            raise TimeoutError("o filho falso ainda esta vivo")
        return 1

    def kill(self) -> None:
        self.mortes += 1
        self.rebentar()

    def popen(self, argv, **opcoes) -> "FilhoFalso":
        self.argv = list(argv)
        self.opcoes = opcoes
        return self


class _ComFilho(unittest.TestCase):
    def setUp(self) -> None:
        self.filho = FilhoFalso()
        self.addCleanup(self.filho.fechar_leituras)
        self.addCleanup(self.filho.rebentar)
        self.log = LogFalso()
        self.cliques = 0

    def contar_clique(self) -> None:
        self.cliques += 1

    def ligacao(self, ao_clique=None, **opcoes) -> LigacaoABolinha:
        ligacao = LigacaoABolinha(ao_clique or self.contar_clique, self.log.linha, popen=self.filho.popen, **opcoes)
        self.assertTrue(ligacao.iniciar())
        self.addCleanup(ligacao.fechar, 0.2)
        return ligacao


# --- Nivel do audio ----------------------------------------------------------------


class TestNivel(unittest.TestCase):
    def test_silencio_e_zero_e_fala_alta_e_um(self) -> None:
        self.assertEqual(nivel_de_pcm16(b""), 0.0)
        self.assertEqual(nivel_de_pcm16(pcm_constante(0)), 0.0)
        self.assertEqual(nivel_de_pcm16(pcm_constante(32000)), 1.0)

    def test_fala_normal_fica_no_meio_e_cresce_com_o_volume(self) -> None:
        baixo = nivel_de_pcm16(pcm_constante(300))
        medio = nivel_de_pcm16(pcm_constante(1500))
        self.assertLess(0.0, baixo)
        self.assertLess(baixo, medio)
        self.assertLess(medio, 1.0)

    def test_um_chunk_do_microfone_e_barato(self) -> None:
        bloco = pcm_constante(1200)
        inicio = time.perf_counter()
        for _ in range(300):
            nivel_de_pcm16(bloco)
        self.assertLess((time.perf_counter() - inicio) / 300, 0.001, "menos de 1 ms por chunk de 30 ms")


# --- Estados: do jarvis para a bolinha ---------------------------------------------


class TestMapaDeEstados(unittest.TestCase):
    def test_cada_estado_do_painel_tem_um_estado_valido_da_bolinha(self) -> None:
        self.assertTrue(set(ESTADO_DO_PAINEL.values()) <= ESTADOS_DA_BOLINHA)
        for texto in (app.A_OUVIR, app.A_PENSAR, app.A_FALAR, app.A_ESPERA, app.A_DORMIR):
            self.assertIn(texto, ESTADO_DO_PAINEL, texto)
        self.assertEqual(ESTADO_DO_PAINEL[app.A_OUVIR], "repouso")
        self.assertEqual(ESTADO_DO_PAINEL[app.A_PENSAR], "pensar")
        self.assertEqual(ESTADO_DO_PAINEL[app.A_FALAR], "falar")
        self.assertEqual(ESTADO_DO_PAINEL[app.A_ESPERA], "confirmar")
        self.assertEqual(ESTADO_DO_PAINEL[app.A_DORMIR], "dormir")

    def test_estados_da_conversa_e_da_resposta_ao_recap_do_app_estao_no_mapa(self) -> None:
        for nome in dir(app):
            valor = getattr(app, nome)
            if nome.startswith(("A_", "EM_")) and isinstance(valor, str) and valor.isupper():
                self.assertIn(valor, ESTADO_DO_PAINEL, f"{nome} = {valor!r} sem estado na bolinha")

    def test_painel_so_manda_mudancas_e_ignora_estados_desconhecidos(self) -> None:
        ponte, registo = ponte_de_teste()
        ponte.painel(app.A_OUVIR)
        ponte.painel(app.A_OUVIR)
        ponte.painel("OUTRA COISA")
        ponte.painel(app.A_PENSAR)
        self.assertEqual(registo.estados(), ["repouso", "pensar"])

    def test_uma_frase_da_palavra_de_ativacao_ao_recap(self) -> None:
        ponte, registo = ponte_de_teste()
        ponte.painel(app.A_OUVIR)
        ponte.ouvido(EVENTO_INICIO)
        ponte.ouvido(EVENTO_FIM)
        ponte.ouvido(EVENTO_CAPTADA)
        ponte.painel(app.A_PENSAR)
        ponte.painel(app.A_FALAR)
        ponte.painel(app.A_ESPERA)
        ponte.ouvido(EVENTO_INICIO)  # a ouvir a resposta ao recap
        ponte.ouvido(EVENTO_FIM)
        ponte.ouvido(EVENTO_CAPTADA)
        ponte.painel(app.A_PENSAR)
        ponte.painel(app.A_OUVIR)
        self.assertEqual(
            registo.estados(),
            ["repouso", "ouvir", "pensar", "falar", "confirmar", "pensar", "repouso"],
        )

    def test_a_ouvir_a_resposta_ao_recap_continua_confirmar(self) -> None:
        ponte, registo = ponte_de_teste()
        ponte.painel(app.A_ESPERA)
        ponte.ouvido(EVENTO_INICIO)
        self.assertEqual(ponte.estado, "confirmar")
        ponte.painel("À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA")
        self.assertEqual(ponte.estado, "confirmar")

    def test_a_voz_passa_a_frente_da_escuta_para_o_cala_te_por_cima(self) -> None:
        ponte, _ = ponte_de_teste()
        ponte.painel(app.A_FALAR)
        ponte.ouvido(EVENTO_INICIO)
        self.assertEqual(ponte.estado, "falar")
        ponte.painel(app.A_OUVIR)
        self.assertEqual(ponte.estado, "ouvir", "a voz calou, a escuta continua")

    def test_escuta_descartada_volta_ao_estado_do_painel(self) -> None:
        ponte, registo = ponte_de_teste()
        ponte.painel(app.A_OUVIR)
        ponte.ouvido(EVENTO_INICIO)
        ponte.ouvido(EVENTO_DESCARTADA)
        self.assertEqual(registo.estados(), ["repouso", "ouvir", "repouso"])
        ponte.ouvido(EVENTO_INICIO)
        ponte.ouvido(EVENTO_FIM)
        ponte.ouvido(EVENTO_CAPTADA)
        ponte.ouvido(EVENTO_DESCARTADA)  # nada transcrito
        self.assertEqual(registo.estados()[-2:], ["pensar", "repouso"])

    def test_fim_sem_frase_volta_ao_repouso_no_chunk_seguinte(self) -> None:
        ponte, registo = ponte_de_teste()
        ponte.painel(app.A_OUVIR)
        ponte.ouvido(EVENTO_INICIO)
        ponte.ouvido(EVENTO_FIM)  # janela de conversa fechada: nada captado
        self.assertEqual(registo.estados(), ["repouso", "ouvir"])
        ponte.nivel_do_microfone(pcm_constante(100))
        self.assertEqual(registo.estados(), ["repouso", "ouvir", "repouso"])

    def test_erro_fica_a_vista_e_depois_volta_sozinho(self) -> None:
        relogio = Relogio()
        ponte, registo = ponte_de_teste(relogio)
        ponte.painel(app.A_OUVIR)
        ponte.ouvido(EVENTO_ERRO)
        self.assertEqual(ponte.estado, "erro")
        relogio.agora += 1.0
        ponte.nivel_do_microfone(pcm_constante(100))
        self.assertEqual(ponte.estado, "erro")
        relogio.agora += bolinha_mod.ERRO_VISIVEL_S
        ponte.nivel_do_microfone(pcm_constante(100))  # o microfone chega sempre: e aqui que expira
        self.assertEqual(registo.estados(), ["repouso", "erro", "repouso"])
        ponte.erro()
        self.assertEqual(ponte.estado, "erro")

    def test_legenda_limpa_e_vazia_apaga(self) -> None:
        ponte, registo = ponte_de_teste()
        ponte.legenda("open the editor\nin atlas\x1b[31m")
        ponte.legenda("")
        self.assertEqual(len(registo.legendas()), 2)
        self.assertNotIn("\n", registo.linhas[0])
        self.assertNotIn("\x1b", registo.linhas[0])
        self.assertIn("open the editor", registo.legendas()[0])
        self.assertEqual(registo.legendas()[1], "")
        self.assertEqual(registo.linhas[1], "legenda")


class TestNiveisLimitados(unittest.TestCase):
    def test_microfone_no_maximo_30_por_segundo_e_so_a_ouvir(self) -> None:
        relogio = Relogio()
        medidos: list[bytes] = []

        def medir(pcm: bytes) -> float:
            medidos.append(pcm)
            return 0.5

        ponte, registo = ponte_de_teste(relogio, medir=medir)
        ponte.painel(app.A_OUVIR)
        for _ in range(100):  # 3 s de chunks de 30 ms em repouso: nada medido
            ponte.nivel_do_microfone(pcm_constante(1000))
            relogio.agora += 0.03
        self.assertEqual(medidos, [])
        ponte.ouvido(EVENTO_INICIO)
        for _ in range(300):  # 3 s de chunks de 10 ms a ouvir
            ponte.nivel_do_microfone(pcm_constante(1000))
            relogio.agora += 0.01
        self.assertLessEqual(len(registo.niveis), 3 * bolinha_mod.NIVEIS_POR_SEGUNDO + 1)
        self.assertGreaterEqual(len(registo.niveis), 3 * 20)
        self.assertEqual(len(medidos), len(registo.niveis), "so se calcula o que se manda")

    def test_voz_so_conta_a_falar_e_tambem_e_limitada(self) -> None:
        relogio = Relogio()
        ponte, registo = ponte_de_teste(relogio)
        ponte.painel(app.A_PENSAR)
        ponte.nivel_da_voz(pcm_constante(3000))
        self.assertEqual(registo.niveis, [])
        ponte.painel(app.A_FALAR)
        for _ in range(200):  # 2 s de blocos de 5 ms
            ponte.nivel_da_voz(pcm_constante(3000))
            relogio.agora += 0.005
        self.assertLessEqual(len(registo.niveis), 2 * bolinha_mod.NIVEIS_POR_SEGUNDO + 1)
        self.assertGreater(len(registo.niveis), 0)
        self.assertTrue(all(0.0 <= nivel <= 1.0 for nivel in registo.niveis))
        ponte.nivel_do_microfone(pcm_constante(3000))
        self.assertEqual(len([n for n in registo.niveis]), len(registo.niveis))

    def test_microfone_nao_mexe_na_bolinha_enquanto_fala(self) -> None:
        relogio = Relogio()
        ponte, registo = ponte_de_teste(relogio)
        ponte.painel(app.A_FALAR)
        for _ in range(50):
            ponte.nivel_do_microfone(pcm_constante(3000))
            relogio.agora += 0.05
        self.assertEqual(registo.niveis, [], "a falar, so o nivel da voz anima a bolinha")


# --- O jarvis a publicar para a bolinha --------------------------------------------


class TestJarvisPublica(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(voz.definir_ouvinte_da_voz, None)

    def montar(self, *args, **kwargs) -> tuple[Montagem, Registo]:
        m = Montagem(*args, **kwargs)
        ponte, registo = ponte_de_teste()
        m.jarvis.ligar_bolinha(ponte)
        return m, registo

    def test_ditado_mostra_a_frase_ouvida_e_depois_o_texto_a_enviar(self) -> None:
        m, registo = self.montar([DITADO])
        m.ouvir("no atlas corrige o teste do login")
        self.assertEqual(registo.estados(), ["repouso", "pensar", "falar", "confirmar"])
        self.assertEqual(
            registo.legendas(), ["no atlas corrige o teste do login", "Corrige o teste do login."]
        )
        self.assertLess(
            registo.linhas.index("legenda Corrige o teste do login."),
            registo.linhas.index("estado falar"),
            "o texto a enviar aparece antes de o recap ser dito",
        )

    def test_dormir_e_acordar(self) -> None:
        m, registo = self.montar()
        m.ouvir("dorme")
        self.assertEqual(registo.estados()[-1], "dormir")

    def test_falha_no_tratamento_mostra_erro_e_o_jarvis_segue(self) -> None:
        m, registo = self.montar([DITADO])
        with mock.patch.object(m.jarvis, "_tratar_com_tranca", side_effect=RuntimeError("falha a meio")):
            m.ouvir("no atlas corrige o teste do login")
        self.assertIn("erro", registo.estados())
        self.assertIn("ERRO no tratamento", m.log.texto())

    def test_voz_ligada_a_bolinha_e_desligada_no_fim(self) -> None:
        m, _ = self.montar()
        self.assertIsNotNone(voz._ouvinte_da_voz)
        m.jarvis.desligar_bolinha()
        self.assertIsNone(voz._ouvinte_da_voz)
        self.assertIsNone(m.jarvis.painel.ao_mudar)
        self.assertIsNone(m.jarvis.confirmacao.ao_propor)
        m.ouvir("dorme")  # sem bolinha, igual a antes

    def test_uma_bolinha_que_rebenta_nao_para_o_painel_nem_o_recap(self) -> None:
        m = Montagem([DITADO])

        def rebenta(*_args) -> None:
            raise RuntimeError("bolinha partida")

        m.jarvis.ligar_bolinha(PonteDaBolinha(rebenta, rebenta))
        m.ouvir("no atlas corrige o teste do login")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.jarvis.painel.atual, A_ESPERA)


class TestCliqueCala(unittest.TestCase):
    def test_clique_cala_como_o_cala_te_sem_fechar(self) -> None:
        dito = Montagem()
        dito.ouvir("cala-te")
        clicado = Montagem()
        clicado.jarvis.calar_pela_bolinha()
        self.assertEqual(len(clicado.silencios), 1)
        self.assertFalse(clicado.silencios[0][1], "nao definitivo: o jarvis continua")
        self.assertEqual(clicado.silencios[0][1], dito.silencios[0][1])
        self.assertIn("clique na bolinha", clicado.silencios[0][0])
        clicado.avancar()
        clicado.ouvir("que horas são")
        self.assertEqual(clicado.falados, ["São 15 horas e 30 minutos."], "depois do clique continua a responder")

    def test_clique_fecha_a_janela_de_conversa(self) -> None:
        m = Montagem()
        with mock.patch.object(m.jarvis, "_fechar_conversa") as fechar, mock.patch.object(
            m.jarvis.avisos, "descartar"
        ) as descartar:
            m.jarvis.calar_pela_bolinha()
        fechar.assert_called_once()
        descartar.assert_called_once()


# --- O ouvido e a voz como fontes -----------------------------------------------------


class TestOuvidoPublica(unittest.TestCase):
    def test_eventos_da_escuta_com_a_tecla(self) -> None:
        eventos: list[str] = []
        frases = []
        ouvido = Ouvido(
            FonteDeFicheiro(b""),
            MotorFalso(texto="que horas são"),
            frases.append,
            tecla=TeclaFixa(),
            escrever=lambda _linha: None,
            ao_evento=eventos.append,
        )
        for _ in range(10):
            ouvido.processar(chunk(0x22), True)
        ouvido.processar(chunk(0x00), False)
        self.assertEqual(eventos, [EVENTO_INICIO, EVENTO_FIM, EVENTO_CAPTADA])
        ouvido.transcrever_pendentes()
        self.assertEqual(len(frases), 1)

    def test_transcricao_falhada_e_escuta_curta_demais(self) -> None:
        eventos: list[str] = []
        ouvido = Ouvido(
            FonteDeFicheiro(b""),
            MotorFalso(falhar=True),
            lambda _frase: None,
            tecla=TeclaFixa(),
            escrever=lambda _linha: None,
            ao_evento=eventos.append,
        )
        for _ in range(10):
            ouvido.processar(chunk(0x22), True)
        ouvido.processar(chunk(0x00), False)
        ouvido.transcrever_pendentes()
        self.assertEqual(eventos[-1], EVENTO_ERRO)
        eventos.clear()
        ouvido.processar(chunk(0x22), True)
        ouvido.processar(chunk(0x00), False)
        self.assertIn(EVENTO_DESCARTADA, eventos)

    def test_um_ouvinte_que_rebenta_nao_para_a_escuta(self) -> None:
        frases = []

        def rebenta(*_args) -> None:
            raise RuntimeError("bolinha partida")

        ouvido = Ouvido(
            FonteDeFicheiro(b"\x22" * 3200),
            MotorFalso(texto="que horas são"),
            frases.append,
            tecla=TeclaFixa(),
            escrever=lambda _linha: None,
            ao_evento=rebenta,
            ao_chunk=rebenta,
        )
        for _ in range(10):
            ouvido.processar(chunk(0x22), True)
        ouvido.processar(chunk(0x00), False)
        ouvido.transcrever_pendentes()
        self.assertEqual(len(frases), 1)

    def test_cada_chunk_lido_chega_ao_ao_chunk(self) -> None:
        chunks: list[bytes] = []
        fonte = FonteDeFicheiro(b"\x10\x00" * 4800, silencio_depois_s=0.3)
        ouvido = Ouvido(
            fonte,
            MotorFalso(texto=""),
            lambda _frase: None,
            tecla=TeclaFixa(),
            escrever=lambda _linha: None,
            ao_chunk=chunks.append,
        )
        ouvido.iniciar()
        self.assertTrue(ouvido.esperar(5.0))
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(pedaco) == len(chunk(0)) for pedaco in chunks))


class TestVozPublica(unittest.TestCase):
    def setUp(self) -> None:
        voz._esquecer_motor_residente()
        self.addCleanup(voz._esquecer_motor_residente)
        self.addCleanup(voz.definir_ouvinte_da_voz, None)

    def falar(self, ouvinte) -> list[bytes]:
        escritos: list[bytes] = []

        class Saida:
            def escrever(self, _taxa: int, dados: bytes) -> None:
                escritos.append(dados)

        voz.definir_ouvinte_da_voz(ouvinte)
        with mock.patch.object(voz, "_carregar_motor", mock.Mock(return_value=MotorRapidoFalso())):
            fala = voz.FalaResidente(voz.motor_residente(), Saida())
            fala.feed("uma. duas.")
            fala.play(muted=False)
        return escritos

    def test_cada_bloco_tocado_chega_ao_ouvinte(self) -> None:
        ouvidos: list[bytes] = []
        escritos = self.falar(ouvidos.append)
        self.assertEqual(ouvidos, escritos)
        self.assertEqual(len(escritos), 4)

    def test_um_ouvinte_que_rebenta_nao_cala_a_voz(self) -> None:
        def rebenta(_bloco: bytes) -> None:
            raise RuntimeError("bolinha partida")

        self.assertEqual(sum(len(b) for b in self.falar(rebenta)), 2 * 3200)

    def test_sem_som_nada_chega_ao_ouvinte(self) -> None:
        ouvidos: list[bytes] = []
        voz.definir_ouvinte_da_voz(ouvidos.append)
        with mock.patch.object(voz, "_carregar_motor", mock.Mock(return_value=MotorRapidoFalso())):
            fala = voz.FalaResidente(voz.motor_residente(), None)
            fala.feed("uma.")
            fala.play(muted=True)
        self.assertEqual(ouvidos, [])


# --- A ligacao ao processo filho ----------------------------------------------------


class TestLigacao(_ComFilho):
    def test_lanca_o_modulo_da_bolinha_sem_consola(self) -> None:
        self.ligacao()
        self.assertEqual(self.filho.argv[1:], ["-m", "jarvis.bolinha"])
        self.assertIn("stdin", self.filho.opcoes)
        self.assertIn("creationflags", self.filho.opcoes)

    def test_mensagens_chegam_por_ordem_e_os_niveis_so_o_mais_recente(self) -> None:
        self.filho.stdin.solto.clear()  # o filho ainda nao le
        ligacao = self.ligacao()
        ligacao.enviar("estado ouvir")
        self.assertTrue(esperar_ate(lambda: ligacao._fila.empty()))
        ligacao.enviar("legenda what time is it")
        for valor in (0.1, 0.2, 0.3):
            ligacao.enviar_nivel(valor)
        self.filho.stdin.solto.set()
        self.assertTrue(esperar_ate(lambda: "nivel 0.300" in self.filho.stdin.linhas()))
        linhas = self.filho.stdin.linhas()
        self.assertEqual(linhas, ["estado ouvir", "legenda what time is it", "nivel 0.300"])

    def test_fila_cheia_perde_mensagens_sem_esperar(self) -> None:
        self.filho.stdin.solto.clear()  # filho parado: o escritor fica preso no write
        ligacao = self.ligacao(tamanho_da_fila=8)
        ligacao.enviar("estado ouvir")
        self.assertTrue(esperar_ate(lambda: ligacao._fila.empty()))
        inicio = time.perf_counter()
        for i in range(2000):
            ligacao.enviar(f"legenda frase {i}")
            ligacao.enviar_nivel(0.5)
        decorrido = time.perf_counter() - inicio
        self.assertLess(decorrido, 0.5, f"2000 mensagens levaram {decorrido:.3f} s")
        self.assertGreater(ligacao.perdidas, 0)
        self.filho.stdin.solto.set()

    def test_painel_nao_espera_por_um_filho_lento(self) -> None:
        self.filho.stdin.solto.clear()
        ligacao = self.ligacao(tamanho_da_fila=4)
        painel = Painel(lambda _linha: None)
        painel.ao_mudar = PonteDaBolinha(ligacao.enviar, ligacao.enviar_nivel).painel
        inicio = time.perf_counter()
        for i in range(500):
            painel.mudar(A_PENSAR if i % 2 else A_FALAR)
        self.assertLess(time.perf_counter() - inicio, 0.5)
        self.filho.stdin.solto.set()

    def test_tubo_partido_fica_uma_linha_e_o_resto_e_ignorado(self) -> None:
        self.filho.stdin.partir = True
        ligacao = self.ligacao()
        ligacao.enviar("estado ouvir")
        self.assertTrue(esperar_ate(lambda: not ligacao.viva))
        for _ in range(50):
            ligacao.enviar("estado pensar")
            ligacao.enviar_nivel(0.4)
        linhas = [linha for linha in self.log.linhas if "partiu" in linha]
        self.assertEqual(len(linhas), 1, self.log.linhas)
        self.assertIn("o jarvis segue sem ela", linhas[0])

    def test_filho_que_morre_fica_uma_linha_e_nada_mais_e_escrito(self) -> None:
        ligacao = self.ligacao()
        self.filho.rebentar()
        self.assertTrue(esperar_ate(lambda: not ligacao.viva))
        self.assertTrue(esperar_ate(lambda: any("a janela fechou" in linha for linha in self.log.linhas)))
        escritas = self.filho.stdin.escritas
        ligacao.enviar("estado ouvir")
        threading.Event().wait(0.05)
        self.assertEqual(self.filho.stdin.escritas, escritas)
        self.assertEqual(len([l for l in self.log.linhas if "segue sem ela" in l]), 1)

    def test_filho_que_nao_arranca(self) -> None:
        def popen(*_args, **_kwargs):
            raise FileNotFoundError("sem python")

        ligacao = LigacaoABolinha(self.contar_clique, self.log.linha, popen=popen)
        self.assertFalse(ligacao.iniciar())
        self.assertFalse(ligacao.viva)
        ligacao.enviar("estado ouvir")
        ligacao.enviar_nivel(0.2)
        ligacao.fechar()
        self.assertEqual(len(self.log.linhas), 1)
        self.assertIn("nao arrancou", self.log.linhas[0])

    def test_so_o_clique_exato_cala(self) -> None:
        ligacao = self.ligacao()
        for invalida in (
            b"CLIQUE\n",
            b"clique agora\n",
            b" clique\n",
            b"estado erro\n",
            b"\xff\xfe\n",
            b"c" * 700 + b"\n",
        ):
            self.filho.dizer(invalida)
        self.filho.dizer(b"pronto\n")
        self.filho.dizer(b"clique\n")
        self.assertTrue(esperar_ate(lambda: self.cliques == 1))
        threading.Event().wait(0.05)
        self.assertEqual(self.cliques, 1)
        self.assertEqual(ligacao.ignoradas, 6)
        ignoradas = [linha for linha in self.log.linhas if "resposta ignorada" in linha]
        self.assertEqual(len(ignoradas), 6)
        self.assertTrue(any("longa demais" in linha for linha in ignoradas))
        self.assertIn("bolinha | janela no ecra", self.log.linhas)

    def test_ignoradas_so_enchem_o_log_ate_ao_limite(self) -> None:
        ligacao = self.ligacao()
        self.filho.dizer(b"lixo\n" * 200)
        self.assertTrue(esperar_ate(lambda: ligacao.ignoradas == 200))
        ignoradas = [linha for linha in self.log.linhas if "resposta ignorada" in linha]
        self.assertEqual(len(ignoradas), bolinha_mod.MAXIMO_DE_IGNORADAS)

    def test_stderr_do_filho_vai_para_o_log_limitado(self) -> None:
        self.ligacao()
        self.filho.dizer_no_stderr(b"aviso da janela\n" * 50)
        self.assertTrue(esperar_ate(lambda: len([l for l in self.log.linhas if "aviso da janela" in l]) >= 20))
        threading.Event().wait(0.05)
        self.assertEqual(
            len([l for l in self.log.linhas if "aviso da janela" in l]), bolinha_mod.MAXIMO_DE_LINHAS_DO_FILHO
        )

    def test_clique_que_rebenta_nao_para_a_leitura(self) -> None:
        chamadas = []

        def rebenta() -> None:
            chamadas.append(1)
            raise RuntimeError("calar falhou")

        self.ligacao(ao_clique=rebenta)
        self.filho.dizer(b"clique\nclique\n")
        self.assertTrue(esperar_ate(lambda: len(chamadas) == 2))
        self.assertTrue(any("o clique falhou" in linha for linha in self.log.linhas))

    def test_fechar_fecha_o_stdin_e_mata_um_filho_preso(self) -> None:
        ligacao = self.ligacao()
        ligacao.fechar(espera_s=0.1)
        self.assertTrue(self.filho.stdin.fechado.wait(1.0))
        self.assertEqual(self.filho.mortes, 1, "o filho falso nunca sai sozinho: e morto")
        self.assertFalse(any("segue sem ela" in linha for linha in self.log.linhas), "fechar nao e uma falha")


class TestBolinhaNoJarvis(_ComFilho):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(voz.definir_ouvinte_da_voz, None)

    def abrir(self, m: Montagem) -> LigacaoABolinha:
        ligacao = LigacaoABolinha(m.jarvis.calar_pela_bolinha, m.log.linha, popen=self.filho.popen)
        self.assertIs(abrir_bolinha(m.jarvis, ligacao=ligacao), ligacao)
        self.addCleanup(ligacao.fechar, 0.2)
        return ligacao

    def test_estados_reais_chegam_ao_filho(self) -> None:
        m = Montagem([DITADO])
        m.jarvis.painel.mudar(A_OUVIR)
        self.abrir(m)
        m.ouvir("no atlas corrige o teste do login")
        self.assertTrue(esperar_ate(lambda: "estado confirmar" in self.filho.stdin.linhas()))
        linhas = self.filho.stdin.linhas()
        self.assertEqual(
            [l for l in linhas if l.startswith("estado ")],
            ["estado repouso", "estado pensar", "estado falar", "estado confirmar"],
        )
        self.assertIn("legenda Corrige o teste do login.", linhas)

    def test_clique_do_filho_cala_o_jarvis(self) -> None:
        m = Montagem()
        self.abrir(m)
        self.filho.dizer(b"clique\n")
        self.assertTrue(esperar_ate(lambda: len(m.silencios) == 1))
        self.assertEqual(m.silencios[0], ("clique na bolinha", False))

    def test_filho_que_rebenta_deixa_o_jarvis_a_funcionar(self) -> None:
        m = Montagem()
        ligacao = self.abrir(m)
        self.filho.rebentar()
        self.assertTrue(esperar_ate(lambda: not ligacao.viva))
        m.ouvir("que horas são")
        self.assertEqual(m.falados, ["São 15 horas e 30 minutos."])
        self.assertEqual(len([l for l in m.log.linhas if "segue sem ela" in l]), 1)

    def test_filho_que_nao_arranca_nao_liga_nada(self) -> None:
        m = Montagem()

        def popen(*_args, **_kwargs):
            raise OSError("sem janela")

        ligacao = LigacaoABolinha(m.jarvis.calar_pela_bolinha, m.log.linha, popen=popen)
        self.assertIsNone(abrir_bolinha(m.jarvis, ligacao=ligacao))
        self.assertIsNone(m.jarvis.bolinha)
        self.assertIsNone(m.jarvis.painel.ao_mudar)
        m.ouvir("que horas são")
        self.assertEqual(m.falados, ["São 15 horas e 30 minutos."])


# --- Arranque: --sem-bolinha, --wav e testes ------------------------------------------


class _ParaNoOuvido(ValueError):
    """Para o arranque em `construir_ouvido` (sem modelos nem microfone)."""


class TestArranqueDaBolinha(unittest.TestCase):
    def arrancar(self, argv: list[str], *, variavel: str | None = None) -> mock.Mock:
        log = LogFalso()
        abrir = mock.Mock(return_value=None)
        ambiente = {k: v for k, v in os.environ.items() if k != app.VARIAVEL_SEM_BOLINHA}
        if variavel is not None:
            ambiente[app.VARIAVEL_SEM_BOLINHA] = variavel
        with (
            mock.patch.dict(os.environ, ambiente, clear=True),
            mock.patch.object(app, "LogDaSessao", lambda: log),
            mock.patch.object(app, "carregar_config_tolerante", lambda *_a: config_de_teste("en")),
            mock.patch.object(app.voz, "definir_lingua_da_voz"),
            mock.patch.object(app.voz, "definir_voz_inglesa"),
            mock.patch.object(app, "Interprete", lambda config: Interprete(config, cliente=LlmFalso())),
            mock.patch.object(app, "ForjaPorVoz"),
            mock.patch.object(app, "construir_canal", return_value=None),
            mock.patch.object(app, "_titulo_da_consola"),
            mock.patch.object(app.atexit, "register"),
            mock.patch.object(app.Jarvis, "calar_agora"),
            mock.patch.object(app, "construir_ouvido", side_effect=_ParaNoOuvido("parado no teste")),
            mock.patch.object(app, "abrir_bolinha", abrir),
        ):
            args = app.construir_parser().parse_args(argv)
            self.assertEqual(app._arrancar_e_correr(args, time.perf_counter(), Path("config.toml")), 1)
        self.assertIn("jarvis terminado", log.linhas)
        return abrir

    def test_com_o_microfone_abre_a_bolinha(self) -> None:
        self.arrancar([]).assert_called_once()

    def test_sem_bolinha_nao_abre_janela(self) -> None:
        self.arrancar(["--sem-bolinha"]).assert_not_called()

    def test_modo_ficheiro_nunca_abre_janela(self) -> None:
        self.arrancar(["--wav", "a.wav"]).assert_not_called()
        self.arrancar(["--wav", "a.wav", "--com-som"]).assert_not_called()

    def test_testes_nunca_abrem_janela(self) -> None:
        self.assertTrue(os.environ.get(app.VARIAVEL_SEM_BOLINHA), "definida em tests/__init__.py")
        self.arrancar([], variavel="1").assert_not_called()

    def test_bolinha_aberta_e_fechada_no_fim(self) -> None:
        ligacao = mock.Mock()
        log = LogFalso()
        with (
            mock.patch.dict(os.environ, {app.VARIAVEL_SEM_BOLINHA: ""}),
            mock.patch.object(app, "LogDaSessao", lambda: log),
            mock.patch.object(app, "carregar_config_tolerante", lambda *_a: config_de_teste("en")),
            mock.patch.object(app.voz, "definir_lingua_da_voz"),
            mock.patch.object(app.voz, "definir_voz_inglesa"),
            mock.patch.object(app, "Interprete", lambda config: Interprete(config, cliente=LlmFalso())),
            mock.patch.object(app, "ForjaPorVoz"),
            mock.patch.object(app, "construir_canal", return_value=None),
            mock.patch.object(app, "_titulo_da_consola"),
            mock.patch.object(app.atexit, "register"),
            mock.patch.object(app.Jarvis, "calar_agora"),
            mock.patch.object(app, "construir_ouvido", side_effect=_ParaNoOuvido("parado no teste")),
            mock.patch.object(app, "abrir_bolinha", return_value=ligacao),
        ):
            args = app.construir_parser().parse_args([])
            app._arrancar_e_correr(args, time.perf_counter(), Path("config.toml"))
        ligacao.fechar.assert_called_once()

    def test_opcao_na_ajuda(self) -> None:
        ajuda = app.construir_parser().format_help()
        self.assertIn("--sem-bolinha", ajuda)
        self.assertTrue(app.construir_parser().parse_args(["--sem-bolinha"]).sem_bolinha)
        self.assertIsInstance(app.construir_parser().parse_args([]), argparse.Namespace)


if __name__ == "__main__":
    unittest.main()
