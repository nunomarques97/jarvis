r"""Testes do jarvis residente (jarvis/app.py), unittest da biblioteca padrao.

NENHUM destes testes toca em hardware nem em servicos: sem microfone, sem
Ollama (o LLM e um cliente falso em memoria), sem Claude Code (o canal e
falso) e sem som (a voz e uma funcao que so regista o texto). O que protegem:

  * o formato do log de cada frase (etapas, timestamps, latencias, total);
  * o estado mostrado na consola (a ouvir, a pensar, a espera de
    confirmacao, a falar, a dormir);
  * a politica de confirmacao no processo: horas corre logo, abrir o editor
    ou enviar um ditado so depois de "sim", e nada chega ao canal sem ele;
  * "cala-te" dito por cima da voz cala logo, dormir ignora frases, o canal
    em baixo nunca envia nada;
  * a resposta do Claude passa pelo filtro da voz e fica inteira so no log;
  * as perguntas gerais vao ao Claude Code (processo falso) sem recap, sem
    sessao de projeto e sem horas por engano; cala-te, Ctrl+C, dormir e um
    pedido novo descartam a resposta pendente;
  * o arranque em paralelo e o canal real com pecas falsas;
  * o RealtimeSTT ja nao e importado pelo caminho vivo.

Corre com:

    .venv\Scripts\python -m unittest tests.test_app -v
"""

from __future__ import annotations

import datetime
import json
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from jarvis import acoes_locais, app, conversa, pergunta_geral, voz
from jarvis.acoes_locais import AcaoError, ResultadoAcao
from jarvis.persona import opcoes_da_frase
from jarvis.app import (
    A_DORMIR,
    A_ESPERA,
    A_FALAR,
    A_OUVIR,
    A_PENSAR,
    Arranque,
    CanalDasSessoes,
    FonteDeSequencia,
    Jarvis,
    LogDaSessao,
    Painel,
    RegistoDaFrase,
    agora_iso,
    aquecer_em_paralelo,
    caminho_do_log,
    carregar_config_tolerante,
    construir_ouvido,
    construir_parser,
    formatar_etapa,
)
from jarvis.config import Config, ConfigEscuta, ConfigInterprete, ConfigOuvido, ConfigPerguntas, Projeto
from jarvis.interprete import CARACTERES_DO_CONTEXTO, INTENCAO_RECUSADA, Interprete, MotorIndisponivel, Vram
from jarvis.memoria import CadernoDeFactos, HistoricoDePerguntas
from jarvis.ouvido import GATILHO_ATIVACAO, GATILHO_JANELA, GATILHO_TECLA, Frase, TeclaDoFicheiro
from jarvis.resposta_falada import FRASE_RECURSO_SO_TECNICO, frase_de_recurso, rotulo_da_origem
from jarvis.sessoes import Entrega
from jarvis.stt import MotorBase, MotorIndisponivel as SttIndisponivel
from jarvis.voz import ResultadoFala
from tests.test_pergunta_geral import (
    HOJE,
    ArranqueFalso,
    linha_de_texto,
    perguntas_de_teste,
    resultado_json,
    saida_json,
)

RAIZ = Path(__file__).resolve().parent.parent
NOMES = ("atlas", "orbita")
#: As variantes do aviso curto de uma pergunta geral que demora.
AVISOS_EN = app._TEXTOS["en"]["a_verificar"]


# --- Pecas falsas partilhadas com tests/test_ponta_a_ponta.py -------------------


class LogFalso:
    """Guarda as linhas em vez de as escrever, com a mesma interface."""

    def __init__(self) -> None:
        self.linhas: list[str] = []
        self.caminho = Path("logs") / "jarvis-teste.log"
        self.fechado = False
        self._tranca = threading.Lock()

    def linha(self, texto: str) -> str:
        with self._tranca:
            self.linhas.append(texto)
        return texto

    def bruto(self, texto: str = "") -> None:
        self.linha(texto)

    def fechar(self) -> None:
        self.fechado = True

    def texto(self) -> str:
        with self._tranca:
            return "\n".join(self.linhas)


def config_de_teste(
    lingua: str = "pt", *, nomes: tuple[str, ...] = NOMES, escuta: ConfigEscuta | None = None, **ajustes
) -> Config:
    return Config(
        microfone="Microfone Ficticio de Teste",
        projetos=tuple(Projeto(nome, Path("D:/caminho/para") / nome) for nome in nomes),
        ouvido=ConfigOuvido(lingua=lingua),
        interprete=ConfigInterprete(**ajustes),
        escuta=escuta or ConfigEscuta(),
    )


def resposta_llm(intencao: str, projeto: str = "", prompt: str = "", financeiro: bool = False) -> str:
    return json.dumps({"intencao": intencao, "projeto": projeto, "prompt": prompt, "financeiro": financeiro})


class LlmFalso:
    """Faz de Ollama em memoria: respostas feitas, pela ordem; depois indisponivel."""

    def __init__(self, respostas=None) -> None:
        self.respostas = list(respostas or [])
        self.pedidos: list[list[dict]] = []
        self.limite_s = 5.0

    def conversar(self, modelo, mensagens, esquema, *, limite_s=None):
        self.pedidos.append(mensagens)
        if not self.respostas:
            raise MotorIndisponivel("sem resposta feita")
        return self.respostas.pop(0)


class CanalFalso:
    """Faz de canal para as sessoes: regista (projeto, texto) e responde se pedido."""

    def __init__(self, resposta: str | None = None, aberta: bool = True) -> None:
        self.recebidos: list[tuple[str, str]] = []
        self.resposta = resposta
        self.aberta = aberta
        self.fechado = False

    def enviar(self, projeto: str, texto: str, ao_responder) -> bool:
        self.recebidos.append((projeto, texto))
        if self.resposta is not None:
            ao_responder(projeto, Entrega(projeto=projeto, caminho="canal", texto=self.resposta))
        return self.aberta

    def fechar(self) -> None:
        self.fechado = True


class RelogioFalso:
    def __init__(self, agora: float = 1000.0) -> None:
        self.agora = agora

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class Montagem:
    """Um Jarvis com todas as pecas externas falsas."""

    def __init__(
        self,
        respostas_llm=None,
        *,
        lingua: str = "pt",
        canal: CanalFalso | None | bool = True,
        relogio=None,
        primeiro_audio_depois_s: float = 0.1,
        nomes: tuple[str, ...] = NOMES,
        perguntas=None,
        sons=None,
        historico=None,
        caderno=None,
        espera_do_aviso_s: float = 2.0,
        **ajustes,
    ) -> None:
        self.log = LogFalso()
        self.config = config_de_teste(lingua, nomes=nomes, **ajustes)
        self.llm = LlmFalso(respostas_llm)
        self.interprete = Interprete(self.config, cliente=self.llm)
        self.canal = CanalFalso() if canal is True else (canal or None)
        self.falados: list[str] = []
        self.locais: list[tuple] = []
        self.silencios: list[tuple[str, bool]] = []
        self.relogio = relogio or RelogioFalso()

        def falar(texto: str) -> ResultadoFala:
            self.falados.append(texto)
            return ResultadoFala(falou=True, primeiro_audio=self.relogio() + primeiro_audio_depois_s)

        def executar_local(intencao, projeto, config, *, detalhe=None, lingua="pt"):
            self.locais.append((intencao, projeto, detalhe))
            return ResultadoAcao(nome_acao=intencao, executou=True, texto="São 15 horas e 30 minutos.")

        def calar(motivo, *, definitivo, registar=None):
            self.silencios.append((motivo, definitivo))
            agora = datetime.datetime.now()
            return voz.ResultadoSilencio(
                instante_do_pedido=agora,
                instante_do_fim_do_audio=agora,
                intervalo_ms=0.0,
                matou_sintese=False,
                parou_reproducao=False,
                definitivo=definitivo,
                motivo=motivo,
            )

        self.jarvis = Jarvis(
            self.config,
            self.log,
            interprete=self.interprete,
            canal=self.canal,
            perguntas=perguntas,
            falar=falar,
            calar=calar,
            executar_local=executar_local,
            relogio=self.relogio,
            sons=sons,
            historico=historico,
            caderno=caderno,
            espera_do_aviso_s=espera_do_aviso_s,
        )

    def ouvir(
        self,
        texto: str,
        *,
        gatilho: str = GATILHO_TECLA,
        fim: float | None = None,
        score: float | None = None,
        ultima_voz: float | None = None,
    ) -> None:
        """Uma frase acabada de transcrever, entregue como o ouvido a entrega."""
        fim = self.relogio() if fim is None else fim
        self.jarvis.ao_ouvir(
            Frase(
                texto=texto,
                gatilho=gatilho,
                lingua=self.config.ouvido.lingua,
                motor="motor-falso",
                duracao_audio_s=1.0,
                inicio_da_escuta=fim - 1.0,
                fim_da_escuta=fim,
                texto_pronto=fim + 0.2,
                latencia_stt_ms=200.0,
                score_ativacao=score if score is not None else (0.9 if gatilho == GATILHO_ATIVACAO else None),
                ultima_voz=ultima_voz,
            )
        )

    def avancar(self, segundos: float = 1.0) -> None:
        self.relogio.avancar(segundos)


DITADO = resposta_llm("ditar_prompt", "atlas", "Corrige o teste do login.")


# --- Log -------------------------------------------------------------------------


class TestFormatoDoLog(unittest.TestCase):
    def test_linha_de_etapa_tem_frase_etapa_latencia_e_detalhe(self) -> None:
        self.assertEqual(
            formatar_etapa(7, 3, 41.6, "intencao=horas"),
            "frase #7 | etapa 3/5 interprete            |      42 ms | intencao=horas",
        )

    def test_as_cinco_etapas_tem_nome_proprio(self) -> None:
        nomes = [app.NOMES_DAS_ETAPAS[n].strip() for n in range(1, 6)]
        self.assertEqual(
            nomes, ["1/5 ouvido", "2/5 transcricao", "3/5 interprete", "4/5 confirmacao/accao", "5/5 voz"]
        )

    def test_timestamp_tem_milissegundos(self) -> None:
        self.assertEqual(agora_iso(datetime.datetime(2026, 9, 25, 9, 5, 7, 45000)), "2026-09-25 09:05:07.045")

    def test_nome_do_ficheiro_de_log_e_por_dia(self) -> None:
        self.assertEqual(caminho_do_log(datetime.datetime(2026, 9, 25), Path("x")).name, "jarvis-2026-09-25.log")

    def test_o_log_escreve_na_consola_e_no_ficheiro(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            consola = StringIO()
            log = LogDaSessao(pasta=Path(pasta), consola=consola)
            log.linha("frase #1 | ola")
            log.fechar()
            log.linha("depois de fechar so vai para a consola")
            ficheiro = log.caminho.read_text(encoding="utf-8")
        self.assertIn("frase #1 | ola", consola.getvalue())
        self.assertIn("frase #1 | ola", ficheiro)
        self.assertNotIn("depois de fechar", ficheiro)

    def test_latencias_medem_desde_a_etapa_anterior_e_o_total_desde_o_fim_da_fala(self) -> None:
        tempos = iter([10.0, 11.0, 11.25, 12.0])
        registo = RegistoDaFrase(numero=1, relogio=lambda: next(tempos))
        registo.fim_da_fala = 10.5
        self.assertEqual(round(registo.marcar(1, "a").latencia_ms), 1000)
        self.assertEqual(round(registo.marcar(2, "b").latencia_ms), 250)
        self.assertIn("1500 ms desde o fim da fala", registo.fechar("fim"))


class TestRegistoDeUmaFrase(unittest.TestCase):
    def test_horas_tem_as_cinco_etapas_e_as_duas_medidas(self) -> None:
        m = Montagem()
        m.ouvir("que horas são")
        texto = m.log.texto()
        for etapa in ("1/5 ouvido", "2/5 transcricao", "3/5 interprete", "4/5 confirmacao/accao", "5/5 voz"):
            self.assertIn(etapa, texto)
        self.assertIn("primeiro sinal de vida: 0 ms desde o fim da fala (linha A PENSAR)", texto)
        self.assertIn("inicio da resposta falada: 100 ms desde o fim da fala", texto)
        self.assertIn("| TOTAL |", texto)
        medida = m.jarvis.medidas[0]
        self.assertEqual((medida.intencao, medida.desfecho), ("horas", "executado"))
        self.assertAlmostEqual(medida.primeira_fala_ms, 100.0)
        self.assertEqual(medida.sinal_de_vida_ms, 0.0)


# --- Estado na consola -----------------------------------------------------------


class TestEstadoNaConsola(unittest.TestCase):
    def test_o_ditado_passa_por_pensar_falar_e_esperar(self) -> None:
        m = Montagem([DITADO])
        m.jarvis.iniciar()
        try:
            m.ouvir("no atlas corrige o teste do login")
            self.assertTrue(m.jarvis.esperar_ocioso(5.0))
        finally:
            m.jarvis.fechar()
        self.assertEqual(m.jarvis.painel.historico, [A_OUVIR, A_PENSAR, A_FALAR, A_ESPERA])
        self.assertIn(f"estado | {A_ESPERA}", m.log.texto())

    def test_dormir_mostra_a_dormir(self) -> None:
        m = Montagem()
        m.ouvir("dorme")
        self.assertEqual(m.jarvis.painel.atual, A_DORMIR)

    def test_painel_nao_repete_a_mesma_linha(self) -> None:
        linhas: list[str] = []
        painel = Painel(linhas.append)
        painel.mudar(A_OUVIR)
        painel.mudar(A_OUVIR)
        painel.mudar(A_PENSAR, "frase #1")
        self.assertEqual(linhas, [f"estado | {A_OUVIR}", f"estado | {A_PENSAR} | frase #1"])


# --- Politica de confirmacao dentro do processo ---------------------------------


class TestConfirmacaoNoProcesso(unittest.TestCase):
    def test_horas_corre_sem_confirmacao_e_nunca_toca_no_canal(self) -> None:
        m = Montagem()
        m.ouvir("que horas são")
        self.assertEqual(m.locais, [("horas", None, "horas")])
        self.assertEqual(m.falados, ["São 15 horas e 30 minutos."])
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.llm.pedidos, [], "a lista branca responde sem o LLM")

    def test_abrir_o_editor_so_depois_do_sim(self) -> None:
        m = Montagem()
        m.ouvir("abre o editor no atlas")
        self.assertEqual(m.locais, [], "nada abre antes do sim")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(m.locais, [("abrir_editor", "atlas", None)])

    def test_ditado_so_chega_ao_canal_depois_do_sim_e_e_o_texto_mostrado(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("no atlas corrige o teste do login")
        self.assertEqual(m.canal.recebidos, [])
        self.assertIn("ecra | Corrige o teste do login.", m.log.texto())
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos, [("atlas", "Corrige o teste do login.")])
        self.assertEqual(m.falados[-1], "Enviado para o atlas.")

    def test_sessao_por_abrir_diz_que_vai_abrir_a_janela(self) -> None:
        m = Montagem([DITADO], canal=CanalFalso(aberta=False))
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("sim")
        self.assertIn("Aceita o aviso na janela nova", m.falados[-1])

    def test_sim_dito_antes_do_recap_nao_envia(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("no atlas corrige o teste do login")
        # A fala do "sim" comecou antes de o recap ter sido dito.
        m.ouvir("sim", fim=m.relogio() - 0.5)
        self.assertEqual(m.canal.recebidos, [])
        self.assertTrue(m.jarvis.confirmacao.a_espera)

    def test_cancela_nunca_envia(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("cancela")
        self.assertEqual(m.canal.recebidos, [])
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertIn("Cancelado, não enviei nada.", m.falados)

    def test_sem_resposta_o_prazo_cancela_pelo_ciclo_principal(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("no atlas corrige o teste do login")
        m.avancar(m.jarvis.confirmacao.limite_s + 1)
        m.jarvis.verificar_tempo()
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [])
        self.assertIn("Sem resposta, cancelei. Não enviei nada.", m.falados)
        self.assertEqual(m.jarvis.painel.atual, A_OUVIR)

    def test_pedido_financeiro_e_recusado_sem_recap(self) -> None:
        m = Montagem()
        m.ouvir("compra cem euros de bitcoin")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.llm.pedidos, [])

    def test_estado_sem_forja_corre_logo_e_diz_que_nao_esta_disponivel(self) -> None:
        m = Montagem([resposta_llm("estado", "atlas")])
        m.ouvir("como está o run do atlas")
        self.assertFalse(m.jarvis.confirmacao.a_espera, "ver o estado so le: sem recap")
        self.assertEqual(len(m.falados), 1)
        self.assertIn("não estão disponíveis", m.falados[-1])
        self.assertIn("forja | indisponivel", m.log.texto())

    def test_conversa_sem_projeto_pede_o_projeto_e_nao_envia(self) -> None:
        m = Montagem([resposta_llm("conversa", "", "Sim, pode avançar.")])
        m.ouvir("sim, pode avançar")
        self.assertTrue(m.jarvis.confirmacao.a_espera, "fica pendente como um ditado sem projeto")
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados, ["Para que projeto é: atlas, orbita ou outro?"])
        m.avancar()
        m.ouvir("atlas")
        self.assertEqual(m.canal.recebidos, [], "o projeto dito so completa o recap")
        self.assertEqual(m.falados[-1], "Responder ao Claude no atlas: Sim, pode avançar - envio?")
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos, [("atlas", "Sim, pode avançar.")])

    def test_sem_canal_o_sim_nao_envia_e_diz_porque(self) -> None:
        m = Montagem([DITADO], canal=None)
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("sim")
        self.assertIn("O canal para o Claude Code não está disponível", m.falados[-1])
        self.assertIn("o prompt confirmado NAO foi enviado", m.log.texto())

    def test_accao_local_recusada_diz_que_falhou(self) -> None:
        m = Montagem()

        def recusar(*_a, **_k):
            raise AcaoError("Code.exe em falta")

        m.jarvis._executar_local = recusar
        m.ouvir("abre o editor no atlas")
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(m.falados[-1], "Não consegui fazer isso.")

    def test_em_ingles_as_respostas_do_jarvis_sao_inglesas(self) -> None:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test.")], lingua="en")
        m.ouvir("in atlas fix the login test")
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertEqual(m.falados[-1], "Sent to atlas.")

    def test_horas_com_recap_pendente_sao_respondidas_e_o_pedido_fica(self) -> None:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Add tests to the configuration module.")], lingua="en")
        m.ouvir("in atlas add tests to the configuration module")
        recap = m.jarvis.confirmacao.recap
        m.avancar(15)
        m.ouvir("hey jarvis, what time is it?", gatilho=GATILHO_ATIVACAO)
        self.assertEqual(m.locais, [("horas", None, "horas")])
        self.assertEqual(m.falados[-1], "São 15 horas e 30 minutos.")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertIs(m.jarvis.confirmacao.recap, recap)
        self.assertEqual(len(m.llm.pedidos), 1, "as horas nao passam pelo LLM")
        self.assertEqual(m.jarvis.painel.atual, A_ESPERA)
        m.avancar(15)
        m.jarvis.verificar_tempo()
        self.assertTrue(m.jarvis.confirmacao.a_espera, "o prazo recomecou depois das horas")
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("atlas", "Add tests to the configuration module.")])

    def test_cancelar_mal_ouvido_cancela_sem_enviar(self) -> None:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test.")], lingua="en")
        m.ouvir("in atlas fix the login test")
        m.avancar()
        m.ouvir("Uh castle.")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados[-1], "Cancelled, nothing was sent.")

    def test_sim_pouco_claro_nao_envia_e_so_o_sim_claro_envia(self) -> None:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test.")], lingua="en")
        m.ouvir("in atlas fix the login test")
        m.avancar()
        m.ouvir("yet")
        self.assertEqual(m.canal.recebidos, [])
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        m.avancar()
        m.ouvir("Uh, yes.")
        self.assertEqual(m.canal.recebidos, [("atlas", "Fix the login test.")])


# --- Nomes mal ouvidos e correcao sem pedido ------------------------------------------

#: Um projeto ficticio com uma palavra financeira no nome.
NOMES_DO_ENSAIO = ("chamora", "jarvis", "atlas")

#: Frases do ensaio de aceitacao, como o reconhecimento as transcreveu.
RUN_NO_CHAMORA = "Start a run on Shamara to read them README and listed sections. Don't change anything."
CONVERSA_NO_JARVIS = "Asked Jarvis to ask me if the change log should mention the new option and wait for my answer."
CONVERSA_SEM_PROJETO = "Tell it to ask me if the change log should mention the new option and wait for my answer."
PERGUNTA_DO_CHANGELOG = "Ask me if the change log should mention the new option and wait for my answer."


class ForjaFalsa:
    """Regista os pedidos confirmados a FORJA; nunca lanca nada."""

    def __init__(self) -> None:
        self.pedidos: list[tuple[str, str | None, str]] = []

    def executar(self, intencao, projeto, texto="", lingua="pt"):
        self.pedidos.append((intencao, projeto, texto))
        return mock.Mock(ecra=(), falado="Run started.")


class TestObjetivoEConversaSemProjeto(unittest.TestCase):
    def montagem(self, respostas_llm) -> Montagem:
        m = Montagem(respostas_llm, lingua="en", nomes=NOMES_DO_ENSAIO)
        m.jarvis.forja = ForjaFalsa()
        return m

    def test_o_objetivo_do_run_e_so_o_pedido_e_so_segue_depois_do_sim(self) -> None:
        limpo = "Read the README and list its sections. Don't change anything."
        for prompt_do_llm in (limpo, "Start a run on Shamara to read the README and list its sections. Don't change anything.", ""):
            with self.subTest(prompt_do_llm=prompt_do_llm):
                m = self.montagem([resposta_llm("lancar_run", "", prompt_do_llm)])
                m.ouvir(RUN_NO_CHAMORA)
                recap = m.jarvis.confirmacao.recap
                self.assertEqual((recap.pedido.intencao, recap.pedido.projeto), ("lancar_run", "chamora"))
                objetivo = recap.pedido.prompt
                self.assertTrue(objetivo.startswith("Read "), objetivo)
                self.assertTrue(objetivo.endswith("Don't change anything."), objetivo)
                self.assertNotIn("start", objetivo.lower())
                self.assertNotIn("shamara", objetivo.lower())
                self.assertEqual(m.jarvis.forja.pedidos, [], "nada lanca antes do sim")
                m.avancar()
                m.ouvir("yes")
                self.assertEqual(m.jarvis.forja.pedidos, [("lancar_run", "chamora", objetivo)])

    def test_asked_jarvis_to_e_uma_conversa_com_o_projeto_jarvis(self) -> None:
        m = self.montagem([resposta_llm("conversa", "", "")])
        m.ouvir(CONVERSA_NO_JARVIS)
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados[-1], f"Reply to Claude in jarvis: {PERGUNTA_DO_CHANGELOG.rstrip('.')} - send it?")
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("jarvis", PERGUNTA_DO_CHANGELOG)])

    def test_conversa_sem_projeto_pergunta_o_projeto_e_a_resposta_completa_o_pedido(self) -> None:
        m = self.montagem([resposta_llm("conversa", "", PERGUNTA_DO_CHANGELOG)])
        m.ouvir(CONVERSA_SEM_PROJETO)
        self.assertTrue(m.jarvis.confirmacao.a_espera, "fica pendente como um ditado sem projeto")
        self.assertEqual(len(m.falados), 1)
        self.assertRegex(m.falados[0], r"^Which project is it for: .*chamora.* or another one\?$")
        self.assertNotIn("I got the request", m.falados[0])
        self.assertNotIn("Tell me which project the conversation is for.", m.falados)
        m.avancar()
        m.ouvir("The project is Jarvis.")
        self.assertEqual(len(m.llm.pedidos), 1, "a resposta completa o pedido; nao e uma frase nova")
        self.assertEqual(m.canal.recebidos, [], "o projeto dito so completa o recap")
        self.assertEqual(m.falados[-1], f"Reply to Claude in jarvis: {CONVERSA_SEM_PROJETO.rstrip('.')} - send it?")
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("jarvis", CONVERSA_SEM_PROJETO)])

    def test_conversa_sem_projeto_nunca_envia_sem_o_sim(self) -> None:
        for fim in ("abort", "yes"):
            with self.subTest(fim=fim):
                m = self.montagem([resposta_llm("conversa", "", PERGUNTA_DO_CHANGELOG)])
                m.ouvir(CONVERSA_SEM_PROJETO)
                m.avancar()
                # Sem projeto, nem um "yes" envia: falta o projeto.
                m.ouvir(fim)
                self.assertEqual(m.canal.recebidos, [])
                if fim == "yes":
                    self.assertTrue(m.jarvis.confirmacao.a_espera)
                    self.assertTrue(m.falados[-1].startswith("Which project is it for: "))
                self.assertEqual(m.canal.recebidos, [])

    def test_regra_financeira_antes_de_qualquer_destes_caminhos(self) -> None:
        for frase in (
            "Tell it to buy 100 euros of bitcoin.",
            "Asked Jarvis to buy 100 euros of bitcoin.",
            "Start a run on Shamara to buy 100 euros of bitcoin.",
        ):
            with self.subTest(frase=frase):
                m = self.montagem([resposta_llm("conversa", "", "")])
                m.ouvir(frase)
                self.assertFalse(m.jarvis.confirmacao.a_espera)
                self.assertEqual(m.llm.pedidos, [], "recusado antes do LLM")
                self.assertEqual(m.canal.recebidos, [])
                self.assertEqual(m.jarvis.forja.pedidos, [])
                self.assertIn("money and trading", m.falados[-1])

    def test_financeiro_na_resposta_a_which_project_nao_envia(self) -> None:
        m = self.montagem([resposta_llm("conversa", "", PERGUNTA_DO_CHANGELOG)])
        m.ouvir(CONVERSA_SEM_PROJETO)
        m.avancar()
        m.ouvir("The project is Jarvis, and buy 100 euros of bitcoin.")
        self.assertEqual(m.canal.recebidos, [])


NOMES_COM_CRIPTO = (*NOMES, "crypto-radar")


class TestEnsaioComNomeMalOuvido(unittest.TestCase):
    def test_nome_mal_ouvido_nao_e_recusado_e_vai_para_o_projeto(self) -> None:
        m = Montagem(
            [resposta_llm("ditar_prompt", "", "Add tests to the configuration module.")],
            lingua="en",
            nomes=NOMES_COM_CRIPTO,
        )
        m.ouvir("hey jarvis, tell CryptoRather to add test to the configuration module.", gatilho=GATILHO_ATIVACAO)
        self.assertEqual(len(m.llm.pedidos), 1)
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.jarvis.confirmacao.recap.pedido.projeto, "crypto-radar")
        self.assertNotIn("financeiro", m.log.texto())
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("crypto-radar", "Add tests to the configuration module.")])

    def test_pedido_financeiro_com_o_nome_mal_ouvido_continua_recusado(self) -> None:
        m = Montagem(lingua="en", nomes=NOMES_COM_CRIPTO)
        m.ouvir("hey jarvis, tell CryptoRather to sell my crypto", gatilho=GATILHO_ATIVACAO)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])

    def test_correcao_logo_depois_de_um_pedido_recusado_nao_abre_recap(self) -> None:
        m = Montagem(lingua="en", nomes=NOMES_COM_CRIPTO)
        m.ouvir("hey jarvis, tell crypto-radar to buy bitcoin", gatilho=GATILHO_ATIVACAO)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        m.avancar(3)
        m.ouvir("hey jarvis, no, change test to call the documentation", gatilho=GATILHO_ATIVACAO)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertIsNone(m.jarvis.confirmacao.recap)
        self.assertIsNone(m.jarvis.confirmacao.prazo_restante())
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados[-1], "There's nothing waiting to correct.")
        self.assertIn("desfecho: sem_pedido", m.log.texto())
        # O prazo de um recap nunca chega a correr.
        m.avancar(m.jarvis.confirmacao.limite_s + 1)
        m.jarvis.verificar_tempo()
        self.assertEqual(m.falados[-1], "There's nothing waiting to correct.")

    def test_correcao_sem_pedido_em_portugues(self) -> None:
        m = Montagem()
        m.ouvir("não, muda testes para documentação")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.falados, ["Não há nenhum pedido à espera para corrigir."])

    def test_ditados_que_nao_sao_correcoes_continuam_a_abrir_recap(self) -> None:
        for texto, prompt in (
            ("in atlas change the title to welcome", "Change the title to welcome."),
            ("change the login title to welcome in atlas", "Change the login title to welcome."),
            ("in atlas add tests to the configuration module", "Add tests to the configuration module."),
        ):
            with self.subTest(texto=texto):
                m = Montagem([resposta_llm("ditar_prompt", "atlas", prompt)], lingua="en")
                m.ouvir(texto)
                self.assertEqual(len(m.llm.pedidos), 1)
                self.assertTrue(m.jarvis.confirmacao.a_espera)
                self.assertEqual(m.jarvis.confirmacao.recap.pedido.prompt, prompt)

    def test_com_pedido_pendente_a_correcao_segue_o_fluxo_de_correcao(self) -> None:
        m = Montagem(
            [DITADO, resposta_llm("ditar_prompt", "atlas", "Corrige a documentação do login.")]
        )
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("não, muda teste para documentação")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.jarvis.confirmacao.recap.pedido.prompt, "Corrige a documentação do login.")
        self.assertNotIn("Não há nenhum pedido à espera para corrigir.", m.falados)


# --- Silencio, dormir e voz desligada ----------------------------------------------


class TestSilencioEDormir(unittest.TestCase):
    def test_cala_te_cala_logo_ao_ser_ouvido_mesmo_com_a_fila_ocupada(self) -> None:
        m = Montagem()
        m.jarvis.iniciar()
        bloqueio = threading.Event()
        try:
            with m.jarvis._tranca:  # uma frase "a ser tratada": a fila nao anda
                threading.Thread(target=lambda: (m.ouvir("cala-te"), bloqueio.set())).start()
                self.assertTrue(bloqueio.wait(2.0))
                self.assertEqual(m.silencios, [("cala-te dito ao jarvis", False)])
            self.assertTrue(m.jarvis.esperar_ocioso(5.0))
        finally:
            m.jarvis.fechar()
        self.assertTrue(m.jarvis.estado.mudo)

    def test_calado_as_respostas_ficam_so_no_ecra(self) -> None:
        m = Montagem()
        m.ouvir("cala-te")
        m.ouvir("que horas são")
        self.assertEqual(m.falados, [])
        self.assertIn("modo calado; resposta so no ecra: 'São 15 horas e 30 minutos.'", m.log.texto())

    def test_a_dormir_nada_e_interpretado_ate_acordar(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("dorme")
        m.ouvir("no atlas corrige o teste do login")
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])
        self.assertIn("ignorada: o jarvis esta a dormir", m.log.texto())
        m.ouvir("acorda")
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertEqual(m.falados[-1], "Estou acordado.")

    def test_acordar_pela_palavra_de_ativacao_com_formas_curtas(self) -> None:
        # O motor de voz transcreveu "hey jarvis, wake up" como "Up.".
        for texto in ("Up.", "wake", "Wake up.", "awake", "acorda", "Hey Jarvis.", "Jarvis", "Uh, wake up."):
            with self.subTest(texto=texto):
                m = Montagem([DITADO], lingua="en")
                m.ouvir("Go to sleep.")
                self.assertTrue(m.jarvis.estado.adormecido)
                m.ouvir(texto, gatilho=GATILHO_ATIVACAO, score=0.9)
                self.assertFalse(m.jarvis.estado.adormecido)
                self.assertEqual(m.falados[-1], "I'm awake.")
                self.assertEqual(m.llm.pedidos, [])
                self.assertEqual(m.canal.recebidos, [])

    def test_score_abaixo_do_limiar_nao_acorda(self) -> None:
        m = Montagem(lingua="en")
        m.ouvir("Go to sleep.")
        limiar = m.config.ouvido.limiar_ativacao
        m.ouvir("Up.", gatilho=GATILHO_ATIVACAO, score=limiar - 0.05)
        self.assertTrue(m.jarvis.estado.adormecido)
        self.assertEqual(m.falados, ["Okay, off to sleep. Say wake up when you need me."])
        m.ouvir("Up.", gatilho=GATILHO_ATIVACAO, score=limiar)
        self.assertFalse(m.jarvis.estado.adormecido, "score igual ao limiar ja conta")

    def test_up_com_a_tecla_nao_acorda(self) -> None:
        m = Montagem(lingua="en")
        m.ouvir("Go to sleep.")
        m.ouvir("Up.", gatilho=GATILHO_TECLA)
        self.assertTrue(m.jarvis.estado.adormecido)
        self.assertEqual(m.llm.pedidos, [])

    def test_palavra_de_ativacao_com_pedido_acorda_e_faz_o_pedido(self) -> None:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test.")], lingua="en")
        m.ouvir("Go to sleep.")
        self.assertTrue(m.jarvis.estado.adormecido)
        m.jarvis.estado.mudo = True
        falados_a_dormir = len(m.falados)
        m.ouvir("In atlas fix the login test.", gatilho=GATILHO_ATIVACAO, score=m.config.ouvido.limiar_ativacao)
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertFalse(m.jarvis.estado.mudo)
        self.assertEqual(len(m.llm.pedidos), 1, "a mesma frase vai ao interprete, sem a repetir")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        novos = m.falados[falados_a_dormir:]
        self.assertEqual(len(novos), 1, "so o recap, sem frase de acordar a parte")
        self.assertNotIn("I'm awake.", novos)
        self.assertNotIn("I'm asleep", novos[0])
        self.assertIn("atlas", novos[0])
        self.assertEqual(m.canal.recebidos, [], "nada sai antes do yes")
        self.assertIn("acordado pela palavra de ativacao com um pedido", m.log.texto())
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_palavra_de_ativacao_com_pedido_local_acorda_e_responde(self) -> None:
        m = Montagem(lingua="en")
        m.ouvir("Go to sleep.")
        m.ouvir("What time is it?", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertEqual(m.locais, [("horas", None, "horas")])
        self.assertEqual(m.falados, ["Okay, off to sleep. Say wake up when you need me.", "São 15 horas e 30 minutos."])

    def test_palavra_de_ativacao_com_mais_conteudo_ja_nao_e_acordar_curto(self) -> None:
        for texto in ("Up the volume.", "wake up and tell atlas to fix the login", "What's up?"):
            with self.subTest(texto=texto):
                m = Montagem([resposta_llm("conversa", "", "")], lingua="en")
                m.ouvir("Go to sleep.")
                m.ouvir(texto, gatilho=GATILHO_ATIVACAO, score=0.95)
                self.assertFalse(m.jarvis.estado.adormecido)
                self.assertNotIn("I'm awake.", m.falados)
                self.assertEqual(m.canal.recebidos, [])

    def test_a_dormir_abaixo_do_limiar_ou_sem_palavra_de_ativacao_nada_e_interpretado(self) -> None:
        m = Montagem([DITADO], lingua="en")
        m.ouvir("Go to sleep.")
        limiar = m.config.ouvido.limiar_ativacao
        m.ouvir("Tell atlas to fix the login test.", gatilho=GATILHO_ATIVACAO, score=limiar - 0.01)
        # Conversa de fundo: sem palavra de ativacao nem janela (a dormir nao abre nenhuma).
        m.ouvir("Tell atlas to fix the login test.", gatilho=GATILHO_JANELA)
        m.ouvir("buy 100 euros of bitcoin", gatilho=GATILHO_JANELA)
        self.assertTrue(m.jarvis.estado.adormecido)
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados, ["Okay, off to sleep. Say wake up when you need me."])
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertFalse(m.jarvis.seguimento.aberta())

    def test_tecla_a_dormir_continua_a_ignorar_pedidos(self) -> None:
        m = Montagem([DITADO], lingua="en")
        m.ouvir("Go to sleep.")
        m.ouvir("Tell atlas to fix the login test.", gatilho=GATILHO_TECLA)
        m.ouvir("What time is it?", gatilho=GATILHO_TECLA)
        self.assertTrue(m.jarvis.estado.adormecido)
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.locais, [])
        self.assertEqual(m.falados, ["Okay, off to sleep. Say wake up when you need me."])
        m.ouvir("wake up", gatilho=GATILHO_TECLA)
        self.assertEqual(m.falados[-1], "I'm awake.")

    def test_pedido_de_dinheiro_ao_acordar_e_recusado(self) -> None:
        m = Montagem([resposta_llm("conversa", "", "")], lingua="en")
        m.ouvir("Go to sleep.")
        m.ouvir("Tell atlas to buy 100 euros of bitcoin.", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertIn("money and trading", m.falados[-1])
        self.assertEqual(m.llm.pedidos, [], "recusado antes do LLM")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [])

    def test_calar_ou_dormir_com_a_palavra_de_ativacao_nao_acordam(self) -> None:
        for texto in ("Go to sleep.", "Be quiet."):
            with self.subTest(texto=texto):
                m = Montagem(lingua="en")
                m.ouvir("Go to sleep.")
                m.ouvir(texto, gatilho=GATILHO_ATIVACAO, score=0.9)
                self.assertTrue(m.jarvis.estado.adormecido)
                self.assertFalse(m.jarvis.estado.mudo)
                self.assertEqual(m.llm.pedidos, [])

    def test_a_lista_branca_continua_a_acordar(self) -> None:
        for gatilho in (GATILHO_TECLA, GATILHO_ATIVACAO):
            with self.subTest(gatilho=gatilho):
                m = Montagem(lingua="en")
                m.ouvir("Go to sleep.")
                m.ouvir("wake up", gatilho=gatilho, score=0.1 if gatilho == GATILHO_ATIVACAO else None)
                self.assertFalse(m.jarvis.estado.adormecido)
                self.assertEqual(m.falados[-1], "I'm awake.")

    def test_a_dormir_diz_uma_vez_como_acordar(self) -> None:
        # So "dormir"/"calar" com a palavra de ativacao, que nao acordam, ouvem o aviso.
        m = Montagem(lingua="en")
        m.ouvir("Go to sleep.")
        avisos = opcoes_da_frase(app._TEXTOS["en"]["a_dormir"])
        m.ouvir("Go to sleep.", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertEqual(m.falados[-1], avisos[0])
        m.ouvir("Be quiet.", gatilho=GATILHO_ATIVACAO, score=0.9)
        m.ouvir("Tell atlas to fix the login test.", gatilho=GATILHO_TECLA)
        self.assertEqual(sum(m.falados.count(aviso) for aviso in avisos), 1, "so uma vez por sono")
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertTrue(m.jarvis.estado.adormecido)
        # Acordar e voltar a dormir: o aviso pode ser dito outra vez.
        m.ouvir("Up.", gatilho=GATILHO_ATIVACAO, score=0.9)
        m.ouvir("Go to sleep.")
        m.ouvir("Go to sleep.", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertEqual(sum(m.falados.count(aviso) for aviso in avisos), 2)
        self.assertEqual(m.llm.pedidos, [])

    def _ouvido_maos_livres(self, m: Montagem, motor: MotorBase):
        """O ouvido real ligado pelo arranque, com detetor e VAD falsos."""
        from jarvis.ouvido import FonteDeFicheiro

        class Detetor:
            def processar(self, pedaco: bytes) -> float:
                return 0.9 if pedaco[0] == 0x7A else 0.1

            def reiniciar(self) -> None:
                pass

        class Vad:
            def e_fala(self, pedaco: bytes) -> bool:
                return pedaco[0] != 0x00

        return app.construir_ouvido(
            m.jarvis, motor=motor, fonte=FonteDeFicheiro(b""), tecla=_TeclaSolta(), detetor=Detetor(), vad=Vad()
        )

    def _so_a_palavra_de_ativacao(self, ouvido) -> None:
        """'hey jarvis' e depois silencio ate a escuta desistir."""
        from jarvis.ouvido import BYTES_POR_CHUNK, DURACAO_DO_CHUNK_S, ESPERA_PELA_FALA_S

        ouvido.processar(bytes([0x7A]) * BYTES_POR_CHUNK, False)
        for _ in range(round(ESPERA_PELA_FALA_S / DURACAO_DO_CHUNK_S) + 1):
            ouvido.processar(bytes([0x00]) * BYTES_POR_CHUNK, False)
        ouvido.transcrever_pendentes()

    def test_so_a_palavra_de_ativacao_pelo_ouvido_acorda(self) -> None:
        m = Montagem([DITADO], lingua="en")
        motor = mock.Mock(wraps=_MotorDeTexto("Tell atlas to fix the login test."))
        ouvido = self._ouvido_maos_livres(m, motor)
        m.ouvir("Go to sleep.")
        self.assertTrue(m.jarvis.estado.adormecido)
        self._so_a_palavra_de_ativacao(ouvido)
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertEqual(m.falados[-1], "I'm awake.")
        motor.transcrever.assert_not_called()
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.canal.recebidos, [])

    def _palavra_e_pedido(self, ouvido, *, com_palavra: bool = True) -> None:
        """'hey jarvis' (ou nada), um pedido com voz e silencio ate ao fim da fala."""
        from jarvis.ouvido import BYTES_POR_CHUNK

        if com_palavra:
            ouvido.processar(bytes([0x7A]) * BYTES_POR_CHUNK, False)
        for _ in range(40):
            ouvido.processar(bytes([0x01]) * BYTES_POR_CHUNK, False)
        for _ in range(30):
            ouvido.processar(bytes([0x00]) * BYTES_POR_CHUNK, False)
        ouvido.transcrever_pendentes()

    def test_palavra_de_ativacao_e_pedido_pelo_ouvido_acordam_e_fazem_o_pedido(self) -> None:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test.")], lingua="en")
        ouvido = self._ouvido_maos_livres(m, _MotorDeTexto("In atlas fix the login test."))
        m.ouvir("Go to sleep.")
        self._palavra_e_pedido(ouvido)
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertEqual(len(m.llm.pedidos), 1)
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertNotIn("I'm awake.", m.falados)
        self.assertEqual(m.canal.recebidos, [])

    def test_fala_de_fundo_pelo_ouvido_a_dormir_e_ignorada(self) -> None:
        m = Montagem([DITADO], lingua="en")
        motor = mock.Mock(wraps=_MotorDeTexto("Tell atlas to fix the login test."))
        ouvido = self._ouvido_maos_livres(m, motor)
        m.ouvir("Go to sleep.")
        self._palavra_e_pedido(ouvido, com_palavra=False)
        self.assertTrue(m.jarvis.estado.adormecido)
        motor.transcrever.assert_not_called()
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(m.falados, ["Okay, off to sleep. Say wake up when you need me."])

    def test_so_a_palavra_de_ativacao_acordado_nao_faz_nada(self) -> None:
        m = Montagem([DITADO], lingua="en")
        ouvido = self._ouvido_maos_livres(m, _MotorDeTexto())
        self._so_a_palavra_de_ativacao(ouvido)
        self.assertEqual(m.falados, [])
        self.assertEqual(m.jarvis.frases, 0)
        self.assertEqual(m.llm.pedidos, [])
        self.assertIn("o jarvis esta acordado: nada a fazer", m.log.texto())
        # Texto vazio que chega ja acordado (acordou entretanto) tambem nao e interpretado.
        m.ouvir("", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertEqual(m.falados, [])
        self.assertEqual(m.llm.pedidos, [])
        self.assertFalse(m.jarvis.confirmacao.a_espera)

    def test_so_a_palavra_de_ativacao_abaixo_do_limiar_nao_acorda(self) -> None:
        m = Montagem(lingua="en")
        m.ouvir("Go to sleep.")
        limiar = m.config.ouvido.limiar_ativacao
        m.jarvis.ao_ativar_sem_fala(
            Frase(
                texto="",
                gatilho=GATILHO_ATIVACAO,
                lingua="en",
                motor="motor-falso",
                duracao_audio_s=0.0,
                inicio_da_escuta=0.0,
                fim_da_escuta=0.0,
                texto_pronto=0.0,
                latencia_stt_ms=0.0,
                score_ativacao=limiar - 0.05,
            )
        )
        self.assertTrue(m.jarvis.estado.adormecido)
        self.assertEqual(m.llm.pedidos, [])

    def test_aviso_de_sono_em_portugues(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("dorme")
        m.ouvir("dorme", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertEqual(m.falados[-1], "Estou a dormir. Para me acordar, diz boas jarvis, acorda.")
        self.assertEqual(m.llm.pedidos, [])

    def test_pedido_em_portugues_com_a_palavra_de_ativacao_acorda_e_faz_o_pedido(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("dorme")
        m.ouvir("no atlas corrige o teste do login", gatilho=GATILHO_ATIVACAO, score=0.9)
        self.assertFalse(m.jarvis.estado.adormecido)
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(len(m.llm.pedidos), 1)
        self.assertNotIn("Estou acordado.", m.falados)
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos, [("atlas", "Corrige o teste do login.")])

    def test_dormir_com_recap_pendente_cancela_sem_enviar(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("dorme")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos, [])

    def test_depois_do_ctrl_c_nada_novo_e_falado(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        m = Montagem()
        voz.calar_agora("Ctrl+C", definitivo=True)
        m.ouvir("que horas são")
        self.assertEqual(m.falados, [])
        self.assertIn("silenciado a pedido", m.log.texto())

    def test_sem_voz_so_no_ecra(self) -> None:
        m = Montagem()
        m.jarvis.com_voz = False
        m.ouvir("que horas são")
        self.assertEqual(m.falados, [])
        self.assertIn("voz desligada; resposta so no ecra", m.log.texto())


# --- Resposta do Claude pelo canal --------------------------------------------------


class TestRespostaDoCanal(unittest.TestCase):
    def test_resposta_natural_e_falada_sem_a_origem_que_fica_no_log(self) -> None:
        m = Montagem([DITADO], canal=CanalFalso(resposta="Está feito, os testes passam."))
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("sim")
        # O canal falso responde dentro de `enviar`; o real responde mais tarde.
        self.assertIn("Está feito, os testes passam.", m.falados)
        for dito in m.falados:
            self.assertNotIn(rotulo_da_origem("pt"), dito)
        self.assertIn(f"{rotulo_da_origem('pt')} 'Está feito, os testes passam.'", m.log.texto())

    def test_em_ingles_nenhuma_frase_dita_tem_a_origem_e_o_log_tem_o_rotulo(self) -> None:
        tecnica = '<invoke name="Bash">\n{"command": "pytest"}'
        for texto, dito in (
            ("All tests pass.", "All tests pass."),
            (tecnica, frase_de_recurso("so_tecnico", "en")),
            ("", frase_de_recurso("sem_texto", "en")),
        ):
            with self.subTest(texto=texto[:20]):
                m = Montagem(lingua="en")
                m.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto=texto))
                self.assertEqual(m.falados, [dito])
                self.assertNotIn("Claude says", dito)
                self.assertIn(f"(caminho=canal): Claude says: {texto!r}", m.log.texto())

    def test_resposta_tecnica_nunca_chega_a_voz_e_fica_inteira_no_log(self) -> None:
        tecnica = "Corri `git status --short` e alterei jarvis/app.py:\n```python\nimport os\n```"
        m = Montagem()
        m.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto=tecnica))
        self.assertEqual(m.falados, [FRASE_RECURSO_SO_TECNICO])
        self.assertIn(repr(tecnica), m.log.texto())

    def test_falha_da_entrega_e_dita_e_mostra_como_retomar(self) -> None:
        m = Montagem()
        entrega = Entrega(projeto="atlas", caminho="headless", erro="o CLI saiu", session_id="abc-123")
        m.jarvis._ao_responder("atlas", entrega)
        self.assertIn("Não consegui entregar o pedido ao atlas", m.falados[-1])
        self.assertIn("claude --resume abc-123", m.log.texto())

    def test_a_dormir_a_resposta_so_fica_no_log(self) -> None:
        m = Montagem()
        m.jarvis.estado.adormecido = True
        m.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto="Feito."))
        self.assertEqual(m.falados, [])
        self.assertIn("'Feito.'", m.log.texto())


# --- Perguntas gerais pelo Claude Code com pesquisa na web -------------------------


#: As frases do teste ao vivo e o que o LLM respondeu (ou devia responder).
FRASES_DO_TESTE_AO_VIVO = (
    ("Uh what uh temperature is in Porto today?", resposta_llm("horas")),
    ("I asked about the temperature, not the time.", resposta_llm("conversa", "", "I asked about the temperature.")),
    ("Uh tell me the temperature in Porto.", resposta_llm("conversa", "", "Tell me the temperature in Porto.")),
    (
        "I'm asking about the the temperature in Porto, not about a project.",
        resposta_llm("conversa", "", "What is the temperature in Porto?"),
    ),
    (
        "Uh what football games uh is gonna be uh on today?",
        resposta_llm("pergunta_geral", "", "What football games are on today?"),
    ),
)


class TestPerguntasGerais(unittest.TestCase):
    def setUp(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name) / "jarvis-perguntas"

    def montagem(self, respostas_llm=None, *comportamentos, lingua: str = "en", **kw) -> Montagem:
        self.arranque = ArranqueFalso(*comportamentos)
        perguntas = perguntas_de_teste(self.arranque, self.pasta, lingua=lingua)
        m = Montagem(respostas_llm, lingua=lingua, perguntas=perguntas, **kw)
        self.addCleanup(m.jarvis.fechar)
        return m

    def esperar(self, m: Montagem) -> None:
        self.assertTrue(m.jarvis.esperar_pergunta(5.0), "a thread da pergunta nao acabou")

    def test_as_frases_do_teste_ao_vivo_vao_para_o_claude_e_a_resposta_e_falada(self) -> None:
        for frase, llm in FRASES_DO_TESTE_AO_VIVO:
            with self.subTest(frase=frase):
                m = self.montagem([llm], saida_json("It is 22 degrees and sunny in Porto today."))
                m.ouvir(frase)
                self.esperar(m)
                # A primeira frase chega logo: sem aviso curto antes da resposta.
                self.assertEqual(m.falados, ["It is 22 degrees and sunny in Porto today."])
                self.assertNotIn("Tell me which project the conversation is for.", m.falados)
                self.assertEqual(m.locais, [], "nunca as horas")
                self.assertEqual(m.canal.recebidos, [], "nunca uma sessao de projeto")
                self.assertFalse(m.jarvis.confirmacao.a_espera, "sem recap")
                self.assertEqual(len(self.arranque.processos), 1)
                pergunta = self.arranque.ultimo.entrada.split("Question: ")[1]
                self.assertIn("temperature" if "temperature" in frase else "football", pergunta)
                self.assertIn("pergunta | respondida", m.log.texto())
                # A voz nunca diz a origem; o ecra e o log mostram-na com o texto inteiro.
                self.assertIn("Claude says: 'It is 22 degrees and sunny in Porto today.'", m.log.texto())
                self.assertFalse(any("Claude says" in dito for dito in m.falados))

    def test_what_time_is_it_continua_a_dar_as_horas(self) -> None:
        m = self.montagem()
        m.ouvir("what time is it")
        self.assertEqual(m.locais, [("horas", None, "horas")])
        self.assertEqual(m.falados, ["São 15 horas e 30 minutos."])
        self.assertEqual(self.arranque.processos, [])

    def test_resposta_tecnica_passa_pelo_filtro_e_fica_inteira_no_log(self) -> None:
        tecnica = "Corri `curl https://x.y/z`:\n```bash\nrm -rf /\n```"
        m = self.montagem([FRASES_DO_TESTE_AO_VIVO[4][1]], saida_json(tecnica))
        m.ouvir(FRASES_DO_TESTE_AO_VIVO[4][0])
        self.esperar(m)
        self.assertEqual(m.falados, [frase_de_recurso("so_tecnico", "en")])
        self.assertIn(repr(tecnica), m.log.texto())

    def test_falha_e_tempo_esgotado_dizem_so_uma_frase_curta(self) -> None:
        for comportamento in ("demora", "isto nao e JSON", saida_json(is_error=True), saida_json("")):
            with self.subTest(comportamento=comportamento[:30]):
                m = self.montagem([FRASES_DO_TESTE_AO_VIVO[0][1]], comportamento)
                m.jarvis.perguntas.config = ConfigPerguntas(limite_s=0.2)
                m.ouvir(FRASES_DO_TESTE_AO_VIVO[0][0])
                self.esperar(m)
                self.assertEqual(m.falados, ["Sorry, I couldn't find an answer to that."])
                self.assertEqual(len(m.jarvis.historico.trocas()), 0, "uma falha nunca fica na memoria")

    def test_em_portugues(self) -> None:
        m = self.montagem(
            [resposta_llm("pergunta_geral", "", "Que tempo faz hoje no Porto?")],
            saida_json("Hoje está sol no Porto."),
            lingua="pt",
        )
        m.ouvir("que tempo faz hoje no porto")
        self.esperar(m)
        self.assertEqual(m.falados, ["Hoje está sol no Porto."])
        self.assertIn("Answer in European Portuguese", self.arranque.ultimo.entrada)

    def test_precos_de_cripto_e_acoes_continuam_recusados_sem_processo(self) -> None:
        for frase, llm in (
            ("what is the price of bitcoin today", None),
            ("how much is Tesla stock worth right now", None),
            ("how are the markets doing", resposta_llm("pergunta_geral", "", "What is the Tesla share price?")),
        ):
            with self.subTest(frase=frase):
                m = self.montagem([llm] if llm else None)
                m.ouvir(frase)
                self.esperar(m)
                self.assertEqual(self.arranque.processos, [], "nada sai do PC")
                self.assertFalse(set(m.falados) & set(AVISOS_EN))
                self.assertEqual(m.falados, ["Sorry, I don't do money and trading requests by voice."])
                self.assertFalse(m.jarvis.confirmacao.a_espera)

    def _pergunta_pendente(self, m: Montagem) -> None:
        m.jarvis.espera_do_aviso_s = 0.05
        m.ouvir(FRASES_DO_TESTE_AO_VIVO[4][0])
        self.assertTrue(self.arranque.a_correr.wait(5.0))
        # A resposta demora: so entao o aviso curto, uma das variantes.
        self.assertEqual(len(m.falados), 1)
        self.assertIn(m.falados[0], AVISOS_EN)
        self.aviso = m.falados[0]

    def _nada_mais_dito(self, m: Montagem, antes: list[str]) -> None:
        self.esperar(m)
        self.assertTrue(self.arranque.ultimo.morto.is_set(), "o processo foi morto")
        self.assertEqual(m.falados, antes)
        self.assertIn("pergunta | cancelada", m.log.texto())

    def test_cala_te_mata_o_processo_e_a_resposta_nunca_e_dita(self) -> None:
        m = self.montagem([FRASES_DO_TESTE_AO_VIVO[4][1]], "bloqueia")
        self._pergunta_pendente(m)
        m.ouvir("cala-te")
        self._nada_mais_dito(m, [self.aviso])

    def test_ctrl_c_mata_o_processo_e_a_resposta_nunca_e_dita(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        m = self.montagem([FRASES_DO_TESTE_AO_VIVO[4][1]], "bloqueia")
        self._pergunta_pendente(m)
        m.jarvis.calar_agora("Ctrl+C", definitivo=True)
        self._nada_mais_dito(m, [self.aviso])

    def test_dormir_mata_o_processo_e_a_resposta_nunca_e_dita(self) -> None:
        m = self.montagem([FRASES_DO_TESTE_AO_VIVO[4][1]], "bloqueia")
        self._pergunta_pendente(m)
        m.ouvir("go to sleep")
        self.assertTrue(m.jarvis.estado.adormecido)
        self._nada_mais_dito(m, list(m.falados))

    def test_pergunta_nova_substitui_a_antiga(self) -> None:
        m = self.montagem(
            [FRASES_DO_TESTE_AO_VIVO[4][1], FRASES_DO_TESTE_AO_VIVO[0][1]],
            "bloqueia",
            saida_json("It is 22 degrees in Porto."),
        )
        self._pergunta_pendente(m)
        antiga = self.arranque.ultimo
        m.jarvis.espera_do_aviso_s = 2.0
        m.ouvir(FRASES_DO_TESTE_AO_VIVO[0][0])
        self.esperar(m)
        self.assertTrue(antiga.morto.is_set())
        self.assertEqual(len(self.arranque.processos), 2)
        self.assertEqual(m.falados, [self.aviso, "It is 22 degrees in Porto."])

    def test_resposta_que_chega_depois_do_cancelamento_nunca_e_dita(self) -> None:
        # O processo acabou com uma resposta valida, mas ja depois de cancelado.
        m = self.montagem()
        consulta = m.jarvis.perguntas.nova("what football games are on today")
        m.jarvis._consulta = consulta
        consulta.correr = lambda ao_texto=None: pergunta_geral.ResultadoDaPergunta("respondida", "Benfica plays tonight.")
        m.jarvis._cancelar_pergunta("pedido novo")
        m.jarvis._consultar(consulta)
        self.assertEqual(m.falados, [])
        self.assertIn("resposta descartada", m.log.texto())

    def test_horas_depois_de_uma_pergunta_descartam_a_resposta_pendente(self) -> None:
        m = self.montagem([FRASES_DO_TESTE_AO_VIVO[4][1]], "bloqueia")
        self._pergunta_pendente(m)
        m.ouvir("what time is it")
        self._nada_mais_dito(m, [self.aviso, "São 15 horas e 30 minutos."])

    def test_sem_perguntas_configuradas_diz_que_nao_esta_disponivel(self) -> None:
        m = Montagem([FRASES_DO_TESTE_AO_VIVO[4][1]], lingua="en")
        m.ouvir(FRASES_DO_TESTE_AO_VIVO[4][0])
        self.assertEqual(m.falados, ["I can't answer general questions right now."])
        self.assertEqual(m.canal.recebidos, [])

    def test_sim_sozinho_sem_nada_pendente_nao_gasta_quota(self) -> None:
        m = self.montagem([resposta_llm("conversa", "", "Yes.")])
        m.ouvir("yes")
        self.esperar(m)
        self.assertEqual(self.arranque.processos, [])


class TestRespostaGeralEmStreaming(unittest.TestCase):
    """A resposta a uma pergunta geral dita frase a frase, enquanto o Claude ainda a escreve.

    O `claude -p` falso le as linhas de uma fila que o teste vai enchendo.
    """

    PERGUNTA = "what is the weather in Porto today"
    INTERPRETADA = resposta_llm("pergunta_geral", "", "What is the weather in Porto today?")
    PRIMEIRA = "It is 22 degrees and sunny in Porto."

    def setUp(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name) / "jarvis-perguntas"

    def montagem(self, *, espera_do_aviso_s: float = 2.0, limite_s: float = 60.0) -> Montagem:
        self.linhas: queue.Queue = queue.Queue()
        self.arranque = ArranqueFalso(self.linhas)
        perguntas = perguntas_de_teste(self.arranque, self.pasta, config=ConfigPerguntas(limite_s=limite_s))
        m = Montagem([self.INTERPRETADA], lingua="en", perguntas=perguntas, espera_do_aviso_s=espera_do_aviso_s)
        self.addCleanup(m.jarvis.fechar)
        self.addCleanup(self.linhas.put, None)
        return m

    def texto(self, *pedacos: str) -> None:
        for pedaco in pedacos:
            self.linhas.put(linha_de_texto(pedaco))

    def acabar(self, texto: str) -> None:
        self.linhas.put(resultado_json(texto))
        self.linhas.put(None)

    def esperar_falado(self, m: Montagem, quantos: int) -> None:
        limite = time.monotonic() + 5.0
        while len(m.falados) < quantos and time.monotonic() < limite:
            time.sleep(0.005)
        self.assertGreaterEqual(len(m.falados), quantos, m.falados)

    def esperar(self, m: Montagem) -> None:
        self.assertTrue(m.jarvis.esperar_pergunta(5.0), "a thread da pergunta nao acabou")

    def com_a_primeira_frase_dita(self, **kw) -> Montagem:
        m = self.montagem(**kw)
        self.texto("It is 22 degrees ", "and sunny in Porto. ", "Light")
        m.ouvir(self.PERGUNTA, ultima_voz=m.relogio() - 0.4)
        self.esperar_falado(m, 1)
        self.assertEqual(m.falados, [self.PRIMEIRA])
        return m

    def test_a_primeira_frase_e_dita_antes_de_a_resposta_acabar(self) -> None:
        m = self.com_a_primeira_frase_dita()
        self.assertFalse(self.arranque.ultimo.morto.is_set())
        self.assertIsNone(self.arranque.ultimo.returncode, "o processo ainda esta a escrever")
        self.assertEqual(len(m.jarvis.historico.trocas()), 0, "a memoria so recebe a resposta no fim")
        self.texto(" wind from the north. ", "Want the weekend too?")
        self.acabar("It is 22 degrees and sunny in Porto. Light wind from the north. Want the weekend too?")
        self.esperar(m)
        dito = "It is 22 degrees and sunny in Porto. Light wind from the north. Want the weekend too?"
        self.assertEqual(" ".join(m.falados), dito)
        self.assertFalse(set(m.falados) & set(AVISOS_EN), "a resposta chegou depressa: sem aviso")
        trocas = m.jarvis.historico.trocas()
        self.assertEqual([(t.pergunta, t.resposta) for t in trocas], [("What is the weather in Porto today?", dito)])
        log = m.log.texto()
        self.assertIn("pergunta | primeira frase pronta: ", log)
        self.assertIn("pergunta | resposta falada: 500 ms desde a ultima voz da frase #1", log)
        self.assertEqual(log.count("pergunta | resposta falada:"), 1)
        self.assertIn(f"Claude says: {dito!r}", log)

    def test_o_aviso_curto_so_quando_a_primeira_frase_demora(self) -> None:
        m = self.montagem(espera_do_aviso_s=0.05)
        m.ouvir(self.PERGUNTA)
        self.assertEqual(len(m.falados), 1)
        self.assertIn(m.falados[0], AVISOS_EN)
        self.assertIn("pergunta | sem frase pronta em 0.05 s: aviso curto", m.log.texto())
        self.texto(self.PRIMEIRA)
        self.acabar(self.PRIMEIRA)
        self.esperar(m)
        self.assertEqual(m.falados[1:], [self.PRIMEIRA])

    def test_o_aviso_nunca_repete_a_variante_anterior(self) -> None:
        m = Montagem(lingua="en")
        m.jarvis._aleatorio = random.Random(7)
        ditas = [m.jarvis._texto("a_verificar") for _ in range(60)]
        self.assertTrue(all(a != b for a, b in zip(ditas, ditas[1:])), ditas)
        self.assertEqual(set(ditas), set(AVISOS_EN))
        self.assertTrue(all(len(aviso) <= 20 for aviso in AVISOS_EN), "curto")
        self.assertNotIn("Let me check.", AVISOS_EN)

    def _nada_mais_da_resposta(self, m: Montagem, antes: list[str]) -> None:
        self.texto(" wind. ", "Never spoken. ", "Also never.")
        self.acabar("It is 22 degrees and sunny in Porto. Light wind. Never spoken. Also never.")
        self.esperar(m)
        self.assertTrue(self.arranque.ultimo.morto.is_set(), "o processo foi morto a meio")
        self.assertEqual(m.falados, antes)
        self.assertEqual(len(m.jarvis.historico.trocas()), 0, "uma resposta cancelada nao fica na memoria")
        self.assertIn("pergunta | cancelada", m.log.texto())

    def test_be_quiet_a_meio_mata_o_processo_e_nada_mais_se_diz(self) -> None:
        m = self.com_a_primeira_frase_dita()
        m.ouvir("be quiet")
        self._nada_mais_da_resposta(m, [self.PRIMEIRA])

    def test_ctrl_c_a_meio_mata_o_processo_e_nada_mais_se_diz(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        m = self.com_a_primeira_frase_dita()
        m.jarvis.calar_agora("Ctrl+C", definitivo=True)
        self._nada_mais_da_resposta(m, [self.PRIMEIRA])

    def test_dormir_a_meio_mata_o_processo_e_nada_mais_se_diz(self) -> None:
        m = self.com_a_primeira_frase_dita()
        m.ouvir("go to sleep")
        self.assertTrue(m.jarvis.estado.adormecido)
        self._nada_mais_da_resposta(m, list(m.falados))

    def test_um_pedido_novo_a_meio_mata_o_processo_e_nada_mais_se_diz(self) -> None:
        m = self.com_a_primeira_frase_dita()
        m.ouvir("what time is it")
        self._nada_mais_da_resposta(m, [self.PRIMEIRA, "São 15 horas e 30 minutos."])

    def test_uma_cortesia_a_meio_passa_a_frente_e_a_resposta_continua(self) -> None:
        m = self.com_a_primeira_frase_dita()
        m.ouvir("okay")
        self.assertEqual(m.falados, [self.PRIMEIRA, "Okay."])
        self.texto(" wind.")
        self.acabar("It is 22 degrees and sunny in Porto. Light wind.")
        self.esperar(m)
        self.assertEqual(m.falados, [self.PRIMEIRA, "Okay.", "Light wind."])
        self.assertFalse(self.arranque.ultimo.morto.is_set())

    def test_falha_depois_de_falar_diz_uma_frase_curta_e_nao_fica_na_memoria(self) -> None:
        for como in ("tempo esgotado", "linha estragada", "processo acabou sem resultado"):
            with self.subTest(como=como):
                m = self.com_a_primeira_frase_dita(limite_s=1.5 if como == "tempo esgotado" else 60.0)
                if como == "linha estragada":
                    self.linhas.put('{"type": "stream_event", "event": {"delta": ')
                elif como == "processo acabou sem resultado":
                    self.linhas.put(None)
                self.esperar(m)
                self.assertEqual(m.falados, [self.PRIMEIRA, "Sorry, I lost the rest of that."])
                self.assertEqual(len(m.jarvis.historico.trocas()), 0)
                self.assertIsNone(m.jarvis._continuacao)
                self.assertIn("pergunta | tempo_esgotado" if como == "tempo esgotado" else "pergunta | falhou", m.log.texto())

    def test_nada_depois_do_titulo_das_fontes(self) -> None:
        m = self.montagem()
        self.texto("Benfica won 2-1 last night. ", "\n\nSources:\n", "The club site says so. ", "- record.pt\n")
        m.ouvir(self.PERGUNTA)
        self.acabar("Benfica won 2-1 last night.\n\nSources:\nThe club site says so. - record.pt")
        self.esperar(m)
        self.assertEqual(" ".join(m.falados), "Benfica won 2-1 last night.")
        self.assertIn("record.pt", m.log.texto(), "a resposta inteira fica no log")

    def test_sem_pedacos_de_texto_diz_a_resposta_da_linha_final(self) -> None:
        m = self.montagem()
        self.acabar("It is 22 degrees in Porto. Light wind.")
        m.ouvir(self.PERGUNTA)
        self.esperar(m)
        self.assertEqual(" ".join(m.falados), "It is 22 degrees in Porto. Light wind.")
        self.assertEqual(len(m.jarvis.historico.trocas()), 1)

    def test_dinheiro_e_recusado_antes_de_arrancar_o_processo(self) -> None:
        m = self.montagem()
        m.jarvis.perguntas.nova = mock.Mock(side_effect=AssertionError("nenhuma consulta"))
        m.jarvis._perguntar("what is the bitcoin price today")
        self.assertEqual(self.arranque.processos, [])
        self.assertEqual(m.falados, ["Sorry, I don't do money and trading requests by voice."])


# --- Canal real, com as pecas do Claude Code falsas -------------------------------


class _CentralFalsa:
    def __init__(self, nomes, log=None) -> None:
        self.nomes = list(nomes)
        self.parada = False

    def iniciar(self):
        return self

    def parar(self) -> None:
        self.parada = True


class _CanalDoProjetoFalso:
    def __init__(self, sessao, central, log=None) -> None:
        self.sessao = sessao
        self.entregues: list[str] = []
        self.fechado = False

    def entregar(self, texto, limite_s):
        self.entregues.append(texto)
        return Entrega(projeto=self.sessao.nome, caminho="canal", texto="ok")

    def fechar(self) -> None:
        self.fechado = True


class TestCortesiaEPedidosSemProjeto(unittest.TestCase):
    """Cortesia solta, ditado que guarda tudo e pergunta de projeto sem projeto."""

    CORTESIAS = ("Excellent.", "Yeah.", "great", "thanks", "thank you", "ok", "okay", "yes", "nice", "cool", "perfect")

    def setUp(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name) / "jarvis-perguntas"

    def montagem(self, respostas_llm=None, *, lingua: str = "en", **kw) -> Montagem:
        self.arranque = ArranqueFalso(saida_json("Uma resposta que nunca devia ser pedida."))
        perguntas = perguntas_de_teste(self.arranque, self.pasta, lingua=lingua)
        m = Montagem(respostas_llm, lingua=lingua, perguntas=perguntas, **kw)
        self.addCleanup(m.jarvis.fechar)
        return m

    def assert_nada_saiu(self, m: Montagem) -> None:
        self.assertEqual(m.llm.pedidos, [], "nunca vai ao LLM")
        self.assertEqual(m.canal.recebidos, [], "nunca vai ao canal")
        self.assertEqual(self.arranque.processos, [], "nunca vai ao Claude das perguntas gerais")

    def test_cortesia_solta_diz_okay_e_nao_sai_nada(self) -> None:
        for frase in (*self.CORTESIAS, "obrigado", "fixe", "Uh yeah."):
            for gatilho in (GATILHO_TECLA, GATILHO_ATIVACAO):
                with self.subTest(frase=frase, gatilho=gatilho):
                    m = self.montagem([resposta_llm("pergunta_geral", "", frase)])
                    m.ouvir(frase, gatilho=gatilho)
                    self.assertEqual(m.falados, ["Okay."])
                    self.assertFalse(m.jarvis.confirmacao.a_espera, "sem recap")
                    self.assert_nada_saiu(m)
                    self.assertIn("desfecho: ignorado | so cortesia", m.log.texto())

    def test_cortesia_solta_em_portugues(self) -> None:
        for frase in ("obrigado", "fixe", "Excellent."):
            with self.subTest(frase=frase):
                m = self.montagem(lingua="pt")
                m.ouvir(frase)
                self.assertEqual(m.falados, ["Está bem."])
                self.assert_nada_saiu(m)

    def test_cortesia_nao_corta_a_pergunta_geral_a_caminho(self) -> None:
        m = self.montagem([resposta_llm("pergunta_geral", "", "What is the weather in Porto?")])
        self.arranque.comportamentos = [saida_json("It is sunny in Porto.")]
        m.ouvir("i wonder what the weather is in porto")
        m.ouvir("thanks")
        self.assertTrue(m.jarvis.esperar_pergunta(5.0))
        self.assertIn("Okay.", m.falados)
        self.assertIn("sunny", m.falados[-1])
        self.assertEqual(len(m.llm.pedidos), 1, "so a pergunta foi ao LLM")

    def test_yes_dentro_de_um_recap_continua_a_enviar(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test.")])
        m.ouvir("tell atlas to fix the login test")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertNotIn("Okay.", m.falados)

    def test_yeah_dentro_da_janela_de_conversa_e_a_resposta_ao_claude(self) -> None:
        m = self.montagem()
        m.jarvis.janela.abrir("atlas")
        m.avancar(1.5)
        m.ouvir("Yeah.", gatilho=GATILHO_JANELA)
        # Resposta curta a uma pergunta do Claude: vai logo, sem recap.
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [("atlas", "Yeah.")])
        self.assertEqual(m.falados[-1], "Sent.")
        self.assertNotIn("Okay.", m.falados)
        self.assertEqual(m.llm.pedidos, [])

    def test_resposta_curta_fora_da_janela_nunca_vai_sem_recap(self) -> None:
        m = self.montagem([resposta_llm("conversa", "atlas", "Yes.")])
        m.ouvir("tell atlas yes")
        self.assertTrue(m.jarvis.confirmacao.a_espera, "fora da janela uma conversa curta vai a recap")
        self.assertEqual(m.canal.recebidos, [])
        self.assertNotIn("Sent.", m.falados)
        m.avancar()
        m.ouvir("abort")
        self.assertEqual(m.canal.recebidos, [])

    def test_janela_expirada_uma_resposta_curta_nao_e_enviada(self) -> None:
        m = self.montagem()
        m.jarvis.janela.abrir("atlas")
        m.avancar(conversa.JANELA_S + 0.5)
        m.jarvis.verificar_tempo()
        m.ouvir("Yeah.", gatilho=GATILHO_JANELA)
        self.assertEqual(m.canal.recebidos, [])
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertNotIn("Sent.", m.falados)

    def test_ditado_longo_guarda_o_arranque_lento(self) -> None:
        truncado = "Find out which step takes longest and tell me before changing anything."
        m = self.montagem(
            [resposta_llm("ditar_prompt", "crypto-radar", truncado)], nomes=(*NOMES, "crypto-radar")
        )
        m.ouvir(
            "For crypto rather, the startup is slow. Find out which step takes longest "
            "and tell me before changing anything."
        )
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(
            m.canal.recebidos,
            [
                (
                    "crypto-radar",
                    "The startup is slow. Find out which step takes longest and tell me before changing anything.",
                )
            ],
        )

    def test_tarefas_sem_projeto_perguntam_qual_projeto(self) -> None:
        m = self.montagem([resposta_llm("pergunta_geral", "", "List the tasks that are left.")])
        m.ouvir("Talk about to list the tasks that are left.")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertIn("Which project is it for", m.falados[-1])
        self.assertEqual(self.arranque.processos, [], "nunca vai as perguntas gerais")
        self.assertEqual(m.canal.recebidos, [])
        m.avancar()
        m.ouvir("atlas")
        self.assertEqual(m.canal.recebidos, [], "nada sai antes do sim")
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(len(m.canal.recebidos), 1)
        self.assertEqual(m.canal.recebidos[0][0], "atlas")
        self.assertIn("tasks that are left", m.canal.recebidos[0][1])
        self.assertEqual(self.arranque.processos, [])


class TestCanalDasSessoes(unittest.TestCase):
    def montar(self, abrir=None):
        self.abertas: list[str] = []
        self.canais: list[_CanalDoProjetoFalso] = []

        def abrir_falso(projeto, log=None):
            self.abertas.append(projeto.nome)
            return projeto

        def criar_canal(sessao, central, log=None):
            canal = _CanalDoProjetoFalso(sessao, central, log)
            self.canais.append(canal)
            return canal

        linhas: list[str] = []
        canal = CanalDasSessoes(
            config_de_teste(), linhas.append, criar_central=_CentralFalsa, abrir=abrir or abrir_falso, criar_canal=criar_canal
        ).iniciar()
        return canal, linhas

    def entregar(self, canal, projeto: str, texto: str) -> tuple[bool, list]:
        respostas = []
        feito = threading.Event()
        ja_aberta = canal.enviar(projeto, texto, lambda p, e: (respostas.append((p, e)), feito.set()))
        self.assertTrue(feito.wait(5.0))
        return ja_aberta, respostas

    def test_abre_a_sessao_uma_vez_por_projeto_e_entrega_por_ordem(self) -> None:
        canal, _ = self.montar()
        self.assertEqual(self.entregar(canal, "atlas", "primeiro")[0], False)
        ja_aberta, respostas = self.entregar(canal, "atlas", "segundo")
        self.assertTrue(ja_aberta)
        self.assertEqual(self.abertas, ["atlas"])
        self.assertEqual(self.canais[0].entregues, ["primeiro", "segundo"])
        self.assertEqual(respostas[0][0], "atlas")
        canal.fechar()
        self.assertTrue(self.canais[0].fechado)

    def test_projeto_fora_da_configuracao_e_recusado(self) -> None:
        canal, _ = self.montar()
        with self.assertRaises(ValueError):
            canal.enviar("../outro", "texto", lambda *_: None)
        self.assertEqual(self.abertas, [])

    def test_falha_ao_abrir_volta_como_entrega_com_erro(self) -> None:
        def abrir_que_falha(projeto, log=None):
            raise OSError("claude nao encontrado")

        canal, _ = self.montar(abrir=abrir_que_falha)
        _, respostas = self.entregar(canal, "atlas", "texto")
        self.assertIn("claude nao encontrado", respostas[0][1].erro)


# --- Arranque, ouvido e linha de comandos --------------------------------------------


class _MotorDeTexto(MotorBase):
    nome = "motor-falso"

    def __init__(self, texto: str = "que horas são", indisponivel: bool = False) -> None:
        super().__init__("cpu")
        self.texto = texto
        self.indisponivel = indisponivel

    def _carregar_modelo(self):
        if self.indisponivel:
            raise SttIndisponivel("motor-falso: pesos em falta")
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return (self.texto if any(pcm16) else ""), lingua, False


class _TeclaSolta:
    def premida(self) -> bool:
        return False


class _Parar(Exception):
    """Para `main` logo depois das primeiras linhas do log (sem config, modelos nem microfone)."""


class TestUmaSoInstancia(unittest.TestCase):
    """`main` com o microfone tira a tranca; processos e PIDs falsos, nada carregado."""

    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.lock = self.pasta / "jarvis.lock"
        self.vivos: dict[int, int | None] = {}
        for alvo in (
            mock.patch.object(app, "PASTA_LOGS", self.pasta),
            mock.patch.object(app, "forcar_consola_utf8", lambda: None),
            mock.patch("jarvis.instancia.estado_do_processo", self._estado),
            mock.patch.object(app, "carregar_config", lambda _caminho: config_de_teste("en")),
        ):
            alvo.start()
            self.addCleanup(alvo.stop)

    def _estado(self, pid: int) -> tuple[bool, int | None]:
        if pid == app.os.getpid():
            return True, 77
        return (True, self.vivos[pid]) if pid in self.vivos else (False, None)

    def test_outro_jarvis_vivo_recusa_antes_de_carregar_seja_o_que_for(self) -> None:
        self.lock.write_text("4242\n11\n", encoding="ascii")
        self.vivos[4242] = 11
        saida = StringIO()
        with (
            mock.patch.object(app, "_arrancar_e_correr") as arrancar,
            mock.patch.object(app, "LogDaSessao") as log,
            mock.patch.object(app, "construir_ouvido") as ouvido,
            mock.patch("sys.stdout", saida),
        ):
            codigo = app.main([])
        self.assertNotEqual(codigo, 0)
        self.assertEqual(codigo, app.CODIGO_OUTRA_INSTANCIA)
        arrancar.assert_not_called()
        log.assert_not_called()
        ouvido.assert_not_called()
        texto = saida.getvalue()
        self.assertIn("Another jarvis is already running", texto)
        self.assertIn("4242", texto)
        self.assertIn("Close it first", texto)
        self.assertEqual(self.lock.read_text(encoding="ascii"), "4242\n11\n", "a tranca do outro fica")

    def test_arranca_com_a_tranca_e_liberta_no_fim(self) -> None:
        vista: list[str] = []

        def arrancar(*_args, **_kwargs):
            vista.append(self.lock.read_text(encoding="ascii"))
            return 0

        with mock.patch.object(app, "_arrancar_e_correr", arrancar):
            self.assertEqual(app.main([]), 0)
        self.assertEqual(vista, [f"{app.os.getpid()}\n77\n"])
        self.assertFalse(self.lock.exists())

    def test_liberta_com_ctrl_c_e_com_erros(self) -> None:
        for excecao in (KeyboardInterrupt(), RuntimeError("falha a meio")):
            with self.subTest(excecao=repr(excecao)):
                with mock.patch.object(app, "_arrancar_e_correr", side_effect=excecao):
                    with self.assertRaises(type(excecao)):
                        app.main([])
                self.assertFalse(self.lock.exists())

    def test_tranca_de_um_jarvis_morto_e_substituida_e_fica_no_log(self) -> None:
        self.lock.write_text("4242\n11\n", encoding="ascii")
        log = LogFalso()
        with (
            mock.patch.object(app, "LogDaSessao", lambda: log),
            mock.patch.object(app, "carregar_config_tolerante", side_effect=_Parar),
        ):
            with self.assertRaises(_Parar):
                app.main([])
        self.assertIn(f"jarvis a arrancar | pid {app.os.getpid()} | log em", log.linhas[0])
        self.assertIn("tranca da instancia: a anterior (pid 4242)", log.linhas[1])
        self.assertIn("substituida", log.linhas[1])
        self.assertFalse(self.lock.exists(), "libertada tambem quando o arranque falha")

    def test_wav_e_autoteste_nao_tiram_a_tranca(self) -> None:
        self.lock.write_text("4242\n11\n", encoding="ascii")
        self.vivos[4242] = 11  # outro jarvis com o microfone esta aberto
        with mock.patch.object(app, "_arrancar_e_correr", return_value=0) as arrancar:
            self.assertEqual(app.main(["--wav", "a.wav"]), 0)
        arrancar.assert_called_once()
        with mock.patch.object(app, "_autoteste", return_value=0) as autoteste:
            self.assertEqual(app.main(["--autoteste"]), 0)
        autoteste.assert_called_once()
        self.assertEqual(self.lock.read_text(encoding="ascii"), "4242\n11\n")

    def test_wav_nao_cria_tranca(self) -> None:
        with mock.patch.object(app, "_arrancar_e_correr", return_value=0):
            app.main(["--wav", "a.wav"])
        self.assertFalse(self.lock.exists())


class TestArranque(unittest.TestCase):
    def test_aquece_as_pecas_ao_mesmo_tempo(self) -> None:
        barreira = threading.Barrier(3, timeout=5.0)
        log = LogFalso()
        arranque = aquecer_em_paralelo(
            {nome: (lambda: barreira.wait() or "pronto") for nome in ("transcricao", "voz", "interprete")},
            log,
            medir=lambda: None,
        )
        self.assertEqual(arranque.erros, {}, "as tres so passam a barreira se correrem ao mesmo tempo")
        self.assertTrue(arranque.dentro_da_meta)
        self.assertIn("arranque | voz:", log.texto())

    def test_uma_peca_que_falha_fica_no_log_e_nao_para_as_outras(self) -> None:
        def falhar():
            raise RuntimeError("Ollama desligado")

        log = LogFalso()
        arranque = aquecer_em_paralelo({"interprete": falhar, "voz": lambda: "ok"}, log, medir=lambda: None)
        self.assertEqual(set(arranque.erros), {"interprete"})
        self.assertIn("RuntimeError: Ollama desligado", log.texto())

    def test_transcricao_indisponivel_sai_com_erro_legivel(self) -> None:
        m = Montagem()
        ouvido = construir_ouvido(
            m.jarvis, motor=_MotorDeTexto(indisponivel=True), fonte=FonteDeSequencia([]), tecla=_TeclaSolta(), com_ativacao=False
        )
        self.assertEqual(app.correr(m.jarvis, ouvido, com_voz=False, medir=lambda: None), 2)
        self.assertIn("motor-falso: pesos em falta", m.log.texto())

    def test_pronto_e_cabecalho_com_a_tecla_e_as_respostas_ao_recap(self) -> None:
        m = Montagem()
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "horas.wav"
            from jarvis.audio_util import escrever_wav_pcm16

            escrever_wav_pcm16(caminho, b"\x10\x20" * 16000, 16000, 1)
            ouvido = construir_ouvido(m.jarvis, motor=_MotorDeTexto(), wavs=[caminho])
            self.assertIsInstance(ouvido.tecla, TeclaDoFicheiro)
            self.assertIsNone(ouvido.detetor, "em modo ficheiro nao ha maos-livres")
            codigo = app.correr(m.jarvis, ouvido, com_voz=False, medir=lambda: None)
        self.assertEqual(codigo, 0)
        texto = m.log.texto()
        self.assertIn("JARVIS PRONTO em", texto)
        self.assertIn('"sim" envia, "aborta" cancela ("cancela" tambem), "nao, muda X para Y"', texto)
        self.assertIn("a resposta ao recap diz-se com a tecla de falar, dentro de 30 s", texto, "ficheiros: sem VAD")
        self.assertEqual(m.falados, ["São 15 horas e 30 minutos."])

    def test_cabecalho_em_ingles_diz_abort_e_aceita_cancel(self) -> None:
        m = Montagem(lingua="en")
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "horas.wav"
            from jarvis.audio_util import escrever_wav_pcm16

            escrever_wav_pcm16(caminho, b" " * 16000, 16000, 1)
            ouvido = construir_ouvido(m.jarvis, motor=_MotorDeTexto(), wavs=[caminho])
            app.correr(m.jarvis, ouvido, com_voz=False, medir=lambda: None)
        texto = m.log.texto()
        self.assertIn('"yes" envia, "abort" cancela ("cancel" tambem), "no, change X to Y" ou "add ..." corrigem', texto)

    def test_sem_modelo_da_palavra_de_ativacao_fica_so_a_tecla(self) -> None:
        m = Montagem()

        def sem_modelo(_modelo):
            raise FileNotFoundError("modelo em falta")

        with mock.patch("jarvis.app.DetetorOpenWakeWord", sem_modelo):
            ouvido = construir_ouvido(m.jarvis, motor=_MotorDeTexto(), fonte=FonteDeSequencia([]), tecla=_TeclaSolta())
        self.assertIsNone(ouvido.detetor)
        self.assertIn("maos-livres desligadas", m.log.texto())

    def test_parser(self) -> None:
        args = construir_parser().parse_args(["--wav", "a.wav", "b.wav", "--sem-voz", "--com-som", "--sem-ativacao"])
        self.assertEqual(args.wav, ["a.wav", "b.wav"])
        self.assertTrue(args.sem_voz and args.com_som and args.sem_ativacao)
        self.assertFalse(construir_parser().parse_args(["--ptt"]).wav)

    def test_sem_config_segue_sem_projetos(self) -> None:
        log = LogFalso()
        config = carregar_config_tolerante(Path("config-que-nao-existe.toml"), log)
        self.assertEqual(config.projetos, ())
        self.assertIn("O jarvis segue SEM projetos", log.texto())

    def test_sem_config_nao_descobre_projetos(self) -> None:
        # A config que falhou podia desligar a descoberta: nao se adivinha.
        log = LogFalso()
        config = carregar_config_tolerante(Path("config-que-nao-existe.toml"), log)
        self.assertEqual(config.descoberta.pastas, ())
        with mock.patch("jarvis.projetos.descobrir_projetos") as descobrir:
            self.assertIs(app.com_projetos_descobertos(config, registar=log.linha), config)
        descobrir.assert_not_called()

    def test_main_liga_o_ouvido_e_o_processo_residente(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        log = LogFalso()
        chamadas = []

        def correr_falso(jarvis, ouvido, **kwargs):
            chamadas.append((jarvis, ouvido, kwargs))
            return 0

        # A descoberta real leria a pasta pessoal: aqui devolve a config tal e qual.
        descobrir = mock.Mock(side_effect=lambda config, **_k: config)
        with mock.patch.object(app, "LogDaSessao", lambda *a, **k: log), mock.patch.object(
            app, "construir_ouvido", lambda jarvis, **k: ("ouvido", k)
        ), mock.patch.object(app, "correr", correr_falso), mock.patch.object(
            app.atexit, "register"
        ), mock.patch.object(app, "forcar_consola_utf8"), mock.patch.object(
            app, "com_projetos_descobertos", descobrir
        ):
            codigo = app.main(["--config", "nao-existe.toml", "--sem-voz", "--com-som"])
        self.assertEqual(codigo, 0)
        descobrir.assert_called_once()
        jarvis, ouvido, kwargs = chamadas[0]
        self.assertEqual(ouvido[1], {"com_som": True, "com_ativacao": True, "wavs": None})
        self.assertFalse(kwargs["com_voz"])
        self.assertIsNone(jarvis.canal, "sem projetos nao ha canal")
        self.assertTrue(log.fechado)


class TestRealtimeSttForaDoCaminhoVivo(unittest.TestCase):
    def test_importar_o_jarvis_nao_importa_o_realtimestt(self) -> None:
        codigo = "import sys, jarvis.app; print('RealtimeSTT' in sys.modules)"
        saida = subprocess.run(
            [sys.executable, "-c", codigo], cwd=RAIZ, capture_output=True, text=True, timeout=120, check=True
        )
        self.assertEqual(saida.stdout.strip(), "False")

    def test_o_modulo_nao_refere_o_gravador_antigo(self) -> None:
        fonte = (RAIZ / "jarvis" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("AudioToTextRecorder", fonte)
        self.assertNotIn("canal_claude", fonte, "a sessao-ponte sem ferramentas saiu do caminho vivo")

    def test_python_menos_m_jarvis_e_o_mesmo_main(self) -> None:
        texto = (RAIZ / "jarvis" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("from jarvis.app import main", texto)


class TestAtalho(unittest.TestCase):
    """scripts/criar_atalho.py: atalho local, em pasta ignorada, sem interpolar caminhos."""

    def setUp(self) -> None:
        from scripts import criar_atalho

        self.atalho = criar_atalho
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.raiz = Path(pasta.name) / "repo com espaços & 'aspas'"
        (self.raiz / ".venv" / "Scripts").mkdir(parents=True)
        (self.raiz / ".venv" / "Scripts" / "python.exe").write_bytes(b"")

    def test_a_pasta_do_atalho_e_ignorada_pelo_git(self) -> None:
        saida = subprocess.run(
            ["git", "check-ignore", "-q", str(self.atalho.PASTA_DO_ATALHO / "jarvis.lnk")], cwd=RAIZ, timeout=30
        )
        self.assertEqual(saida.returncode, 0, ".jarvis/atalho/ tem de estar no .gitignore")

    def test_so_opcoes_da_lista_fechada(self) -> None:
        self.assertEqual(self.atalho.argumentos_do_jarvis(["--sem-ativacao"]), ["-m", "jarvis", "--sem-ativacao"])
        with self.assertRaises(ValueError):
            self.atalho.argumentos_do_jarvis(["--sem-ativacao & del *"])

    def test_os_caminhos_vao_pelo_ambiente_e_nunca_pelo_comando(self) -> None:
        chamadas = []

        def correr(comando, **opcoes):
            chamadas.append((comando, opcoes))
            Path(opcoes["env"]["JARVIS_ATALHO"]).write_bytes(b"lnk")
            return subprocess.CompletedProcess(comando, 0, "", "")

        codigo = self.atalho.criar(["--com-som"], raiz=self.raiz, correr=correr, escrever=lambda _t: None)
        self.assertEqual(codigo, 0)
        comando, opcoes = chamadas[0]
        self.assertNotIn("shell", opcoes)
        self.assertFalse(any(str(self.raiz) in parte for parte in comando), "caminho colado no comando")
        self.assertEqual(opcoes["env"]["JARVIS_PASTA"], str(self.raiz))
        self.assertEqual(opcoes["env"]["JARVIS_ARGUMENTOS"], "-m jarvis --com-som")
        self.assertTrue((self.raiz / ".jarvis" / "atalho" / "jarvis.lnk").is_file())

    def test_sem_lnk_fica_o_cmd_com_caminhos_relativos(self) -> None:
        def falha(comando, **_opcoes):
            return subprocess.CompletedProcess(comando, 1, "", "sem COM")

        linhas: list[str] = []
        self.assertEqual(self.atalho.criar([], raiz=self.raiz, correr=falha, escrever=linhas.append), 0)
        cmd = (self.raiz / ".jarvis" / "atalho" / "jarvis.cmd").read_text(encoding="ascii")
        self.assertIn('".venv\\Scripts\\python.exe" -m jarvis %*', cmd)
        self.assertNotIn(":\\", cmd, "o .cmd nao leva caminhos absolutos")
        self.assertTrue(any("usa o jarvis.cmd" in linha for linha in linhas))

    def test_sem_venv_nao_escreve_nada(self) -> None:
        (self.raiz / ".venv" / "Scripts" / "python.exe").unlink()
        codigo = self.atalho.criar([], raiz=self.raiz, correr=lambda *a, **k: None, escrever=lambda _t: None)
        self.assertEqual(codigo, 1)
        self.assertFalse((self.raiz / ".jarvis").exists())


class TestExecutorLocal(unittest.TestCase):
    """acoes_locais.executar_pedido: o que o jarvis corre depois do "sim" (ou logo, nas horas)."""

    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.config = Config(microfone="x", projetos=(Projeto("atlas", self.pasta),))

    def test_horas_e_data_na_lingua_do_jarvis(self) -> None:
        self.assertTrue(acoes_locais.executar_pedido("horas", None, self.config).texto.startswith("São "))
        self.assertTrue(acoes_locais.executar_pedido("horas", None, self.config, lingua="en").texto.startswith("It's "))
        data = acoes_locais.executar_pedido("horas", None, self.config, detalhe="data", lingua="en")
        self.assertTrue(data.texto.startswith("Today is "))

    def test_so_intencoes_locais(self) -> None:
        for intencao in ("ditar_prompt", "lancar_run", "comprar", ""):
            with self.subTest(intencao=intencao), self.assertRaises(acoes_locais.AcaoError):
                acoes_locais.executar_pedido(intencao, "atlas", self.config, simular=True)

    def test_abrir_sem_projeto_ou_com_projeto_desconhecido_e_recusado(self) -> None:
        with self.assertRaises(acoes_locais.AcaoError):
            acoes_locais.executar_pedido("abrir_pasta", None, self.config, simular=True)
        for nome in ("orbita", "C:/Windows", "..\\atlas"):
            with self.subTest(nome=nome), self.assertRaises(acoes_locais.AcaoError):
                acoes_locais.executar_pedido("abrir_pasta", nome, self.config, simular=True)

    def test_abrir_pasta_resolve_o_projeto_pela_configuracao(self) -> None:
        resultado = acoes_locais.executar_pedido("abrir_pasta", "ATLAS", self.config, simular=True)
        self.assertFalse(resultado.executou)
        self.assertIn(str(self.pasta), resultado.comando)


# --- LLM ocupado: "um momento" enquanto o modelo carrega -----------------------------


class OllamaQueCarrega(LlmFalso):
    """Nenhum modelo do jarvis carregado; a resposta chega `demora_s` depois, no relogio falso."""

    def __init__(self, respostas, relogio: RelogioFalso, demora_s: float, carregados=None) -> None:
        super().__init__(respostas)
        self.relogio = relogio
        self.demora_s = demora_s
        self.pronto_em = 0.0
        self.carregados = carregados if carregados is not None else {"qwen3:14b": (10 << 30, 10 << 30)}

    def conversar(self, modelo, mensagens, esquema, *, limite_s=None):
        self.pronto_em = self.relogio.agora + self.demora_s
        if limite_s is not None and self.demora_s > limite_s:
            self.pedidos.append(mensagens)
            raise MotorIndisponivel(f"o LLM nao respondeu em {limite_s:g} s")
        return super().conversar(modelo, mensagens, esquema, limite_s=limite_s)

    def modelos_carregados(self, limite_s=None):
        return dict(self.carregados)

    def modelos_instalados(self, limite_s=None):
        return {"qwen3:8b": 5 << 30, "qwen3:4b": 5 << 29}

    def esperar(self, evento: threading.Event, segundos: float) -> bool:
        evento.wait(5)
        fim = self.relogio.agora + segundos
        if self.pronto_em <= fim:
            self.relogio.agora = max(self.relogio.agora, self.pronto_em)
            return True
        self.relogio.agora = fim
        return False


class TestUmMomento(unittest.TestCase):
    """O modelo foi despejado por outro programa: o jarvis avisa e espera ate 20 s."""

    FRASE = "Jarvis, start a run on atlas to read the README and list its sections."
    RUN = resposta_llm("lancar_run", "atlas", "Read the README and list its sections.")

    def _montagem(self, demora_s: float, lingua: str = "en", carregados=None) -> tuple[Montagem, OllamaQueCarrega]:
        m = Montagem(lingua=lingua)
        ollama = OllamaQueCarrega([self.RUN], m.relogio, demora_s, carregados)
        # Nao cabe nenhum dos dois na VRAM: o Ollama tem de carregar o principal.
        m.interprete.cliente = ollama
        m.interprete._relogio = m.relogio
        m.interprete._medir = lambda: Vram(usada_mib=15800, livre_mib=500, total_mib=16303)
        m.interprete._esperar = ollama.esperar
        return m, ollama

    def test_diz_one_moment_uma_vez_antes_do_recap_e_regista_o_modelo(self) -> None:
        m, _ = self._montagem(15.0)
        m.ouvir(self.FRASE)
        self.assertEqual(m.falados[0], "One moment.")
        self.assertEqual(m.falados.count("One moment."), 1)
        self.assertGreater(len(m.falados), 1, "depois do aviso vem o recap")
        self.assertTrue(m.jarvis.confirmacao.a_espera, "o pedido fica a espera do sim")
        self.assertEqual(m.canal.recebidos, [], "nada enviado sem o sim")
        registo = "\n".join(m.log.linhas)
        self.assertIn("aviso de demora do interprete: falado: 'One moment.'", registo)
        interprete = next(linha for linha in m.log.linhas if "etapa 3/5" in linha)
        self.assertIn("modelo=qwen3:8b", interprete)
        self.assertIn("um momento dito; esperou 15.0 s", interprete)
        decisao = next(linha for linha in m.log.linhas if "etapa 4/5" in linha)
        self.assertNotIn("resposta sem accao", decisao, "o aviso nao e a decisao da frase")
        self.assertNotEqual(m.jarvis.medidas[0].primeira_fala, "One moment.")

    def test_em_portugues_diz_um_momento(self) -> None:
        m, _ = self._montagem(15.0, lingua="pt")
        m.ouvir(self.FRASE)
        self.assertEqual(m.falados[0], "Um momento.")

    def test_mais_de_20_s_nao_percebido_sem_nada_executado(self) -> None:
        m, _ = self._montagem(25.0)
        m.ouvir(self.FRASE)
        self.assertEqual(m.falados.count("One moment."), 1)
        self.assertEqual(m.falados[0], "One moment.")
        self.assertEqual((m.canal.recebidos, m.locais), ([], []))
        interprete = next(linha for linha in m.log.linhas if "etapa 3/5" in linha)
        self.assertIn("intencao=desconhecido", interprete)
        self.assertIn("nao respondeu em 20 s", interprete)

    def test_resposta_rapida_nunca_diz_one_moment(self) -> None:
        for demora_s, carregados in ((0.6, {"qwen3:8b": (5 << 30, 5 << 30)}), (1.0, None)):
            with self.subTest(demora_s=demora_s, carregado=carregados is not None):
                m, _ = self._montagem(demora_s, carregados=carregados)
                m.ouvir(self.FRASE)
                self.assertNotIn("One moment.", m.falados)
                self.assertTrue(m.jarvis.confirmacao.a_espera)

    def test_calado_so_regista_o_aviso(self) -> None:
        m, _ = self._montagem(15.0)
        m.jarvis.estado.mudo = True
        m.ouvir(self.FRASE)
        self.assertNotIn("One moment.", m.falados)
        self.assertIn("aviso de demora do interprete: modo calado", "\n".join(m.log.linhas))


if __name__ == "__main__":
    unittest.main()


# --- Correcoes do teste ao vivo: conversa social, fontes e ativacao mal ouvida ---------


class TestCorrecoesDoTesteAoVivo(unittest.TestCase):
    def setUp(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name) / "jarvis-perguntas"

    def montagem(self, respostas_llm=None, *comportamentos, lingua: str = "en", **kw) -> Montagem:
        self.arranque = ArranqueFalso(*comportamentos)
        perguntas = perguntas_de_teste(self.arranque, self.pasta, lingua=lingua)
        m = Montagem(respostas_llm, lingua=lingua, perguntas=perguntas, **kw)
        self.addCleanup(m.jarvis.fechar)
        return m

    def assert_nada_saiu(self, m: Montagem) -> None:
        self.assertEqual(m.llm.pedidos, [], "nunca vai ao LLM")
        self.assertEqual(m.canal.recebidos, [], "nunca vai ao canal")
        self.assertEqual(self.arranque.processos, [], "nunca vai ao Claude das perguntas gerais")
        self.assertFalse(m.jarvis.confirmacao.a_espera, "sem recap")

    def test_conversa_social_do_teste_ao_vivo_responde_localmente(self) -> None:
        m = self.montagem(
            [resposta_llm("pergunta_geral", "", "How are you today?")], saida_json("Nunca devia ser pedida.")
        )
        m.ouvir("Oh yes? And uh how add you today?")
        self.assertEqual(m.falados, [opcoes_da_frase(app._TEXTOS["en"]["social_como_estas"])[0]])
        self.assert_nada_saiu(m)
        self.assertIn("conversa social", m.log.texto())

    def test_cada_tipo_tem_a_sua_frase_em_ingles_e_em_portugues(self) -> None:
        casos = {
            "en": {
                "how are you doing today": "social_como_estas",
                "what's up": "social_o_que_ha",
                "who are you": "social_quem_es",
                "are you there": "social_estas_ai",
                "hello": "social_cumprimento",
            },
            "pt": {
                "tudo bem": "social_como_estas",
                "como estás": "social_como_estas",
                "quem és tu": "social_quem_es",
                "estás aí": "social_estas_ai",
                "bom dia": "social_cumprimento",
            },
        }
        for lingua, frases in casos.items():
            for frase, chave in frases.items():
                with self.subTest(lingua=lingua, frase=frase):
                    m = self.montagem(lingua=lingua)
                    m.ouvir(frase)
                    self.assertEqual(m.falados, [opcoes_da_frase(app._TEXTOS[lingua][chave])[0]])
                    self.assert_nada_saiu(m)

    def test_frases_sociais_sao_curtas(self) -> None:
        for lingua in ("pt", "en"):
            for chave, valor in app._TEXTOS[lingua].items():
                if chave.startswith("social_"):
                    for texto in opcoes_da_frase(valor):
                        with self.subTest(lingua=lingua, chave=chave, texto=texto):
                            self.assertLessEqual(len(texto), 80)
                            self.assertEqual(len(re.findall(r"[.!?]", texto)), 1, "uma frase so")

    def test_pergunta_a_serio_continua_a_ir_ao_claude(self) -> None:
        m = self.montagem(
            [resposta_llm("pergunta_geral", "", "How do you make pancakes?")], saida_json("Mix flour, eggs and milk.")
        )
        m.ouvir("how do you make pancakes")
        self.assertTrue(m.jarvis.esperar_pergunta(5.0))
        self.assertEqual(len(self.arranque.processos), 1)
        self.assertIn("Mix flour", m.falados[-1])

    def test_fontes_da_pergunta_geral_ficam_no_log_e_fora_da_voz(self) -> None:
        resposta = (
            "Benfica beat Porto 2-1 last night.\n\nSources:\n- [BBC Sport](https://bbc.co.uk/sport/x)\n- ESPN report"
        )
        m = self.montagem(
            [resposta_llm("pergunta_geral", "", "What was the result of Benfica against Porto?")], saida_json(resposta)
        )
        m.ouvir("what was the result of benfica against porto")
        self.assertTrue(m.jarvis.esperar_pergunta(5.0))
        self.assertEqual(m.falados[-1], "Benfica beat Porto 2-1 last night.")
        self.assertNotIn("Sources", " ".join(m.falados))
        self.assertIn(repr(resposta), m.log.texto())

    def test_frase_ao_vivo_do_sotaque_chega_limpa_ao_projeto(self) -> None:
        for resposta in ("Add tests for the login flow.", "AJar is tell Crypto Radar to what tests for the login flow."):
            with self.subTest(resposta=resposta):
                m = self.montagem(
                    [resposta_llm("ditar_prompt", "crypto-radar", resposta)], nomes=(*NOMES, "crypto-radar")
                )
                m.ouvir("AJar is tell Crypto Radar to what tests for the login flow.")
                self.assertTrue(m.jarvis.confirmacao.a_espera, "um ditado passa sempre pelo recap")
                m.avancar()
                m.ouvir("yes")
                self.assertEqual(m.canal.recebidos, [("crypto-radar", "Add tests for the login flow.")])


# --- Memoria: conversa recente e caderno de factos -------------------------------


def _pergunta(numero: int) -> tuple[str, str]:
    """(frase dita, resposta do LLM) de uma pergunta geral numerada."""
    return f"what is the capital of country {numero}", resposta_llm(
        "pergunta_geral", "", f"What is the capital of country {numero}?"
    )


class TestMemoriaDasPerguntas(unittest.TestCase):
    """Historico curto e caderno de factos, com um `claude -p` falso e sem LLM real."""

    def setUp(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.base = Path(temporaria.name)
        self.pasta = self.base / "jarvis-perguntas"
        self.caminho = self.base / "memoria" / "factos.json"
        self.relogio_da_memoria = RelogioFalso()

    def montagem(self, respostas_llm=None, *comportamentos, lingua: str = "en", caderno=True, **kw) -> Montagem:
        self.arranque = ArranqueFalso(*comportamentos)
        perguntas = perguntas_de_teste(self.arranque, self.pasta, lingua=lingua)
        self.log_do_caderno: list[str] = []
        self.caderno = (
            CadernoDeFactos(self.caminho, nomes_de_projeto=NOMES, registar=self.log_do_caderno.append)
            if caderno
            else None
        )
        historico = HistoricoDePerguntas(relogio=self.relogio_da_memoria)
        m = Montagem(respostas_llm, lingua=lingua, perguntas=perguntas, historico=historico, caderno=self.caderno, **kw)
        self.addCleanup(m.jarvis.fechar)
        return m

    def esperar(self, m: Montagem) -> None:
        self.assertTrue(m.jarvis.esperar_pergunta(5.0), "a thread da pergunta nao acabou")

    def perguntar(self, m: Montagem, frase: str) -> str:
        """Faz a pergunta, espera pela resposta e devolve o stdin enviado ao claude falso."""
        antes = len(self.arranque.processos)
        m.ouvir(frase)
        self.esperar(m)
        self.assertEqual(len(self.arranque.processos), antes + 1)
        return self.arranque.ultimo.entrada

    def factos_no_ficheiro(self) -> list[str]:
        if not self.caminho.exists():
            return []
        return json.loads(self.caminho.read_text(encoding="utf-8"))["factos"]

    # -- historico

    def test_a_pergunta_de_seguimento_leva_a_troca_anterior_por_stdin(self) -> None:
        m = self.montagem(
            [
                resposta_llm("pergunta_geral", "", "Who won the Benfica game yesterday?"),
                resposta_llm("pergunta_geral", "", "And who scored?"),
            ],
            saida_json("Benfica won two to one."),
            saida_json("Pavlidis scored both goals."),
        )
        primeira = self.perguntar(m, "i wonder who won the benfica game yesterday")
        self.assertNotIn("BEGIN_HISTORY", primeira)
        segunda = self.perguntar(m, "and who scored")
        self.assertIn('Q1: "Who won the Benfica game yesterday?"', segunda)
        # A resposta guardada e exatamente a que foi dita; o rotulo de origem nunca la esta.
        self.assertIn('A1: "Benfica won two to one."', segunda)
        self.assertNotIn(rotulo_da_origem("en"), segunda.split("BEGIN_HISTORY")[-1])
        self.assertEqual(self.arranque.ultimo.argv, self.arranque.processos[0].argv, "argv igual")
        self.assertIn("memoria: 1 troca(s)", m.log.texto())

    def test_no_maximo_10_trocas_vao_na_pergunta(self) -> None:
        frases = [_pergunta(numero) for numero in range(1, 14)]
        m = self.montagem(
            [llm for _frase, llm in frases], *(saida_json(f"Capital {numero}.") for numero in range(1, 14))
        )
        for frase, _llm in frases[:12]:
            self.perguntar(m, frase)
        entrada = self.perguntar(m, frases[12][0])
        self.assertIn('Q1: "What is the capital of country 3?"', entrada)
        self.assertIn('A10: "Capital 12."', entrada)
        self.assertNotIn("Q11:", entrada)
        self.assertNotIn("country 2?", entrada)
        self.assertEqual(len(m.jarvis.historico.trocas()), 10)

    def test_ao_fim_de_30_minutos_sem_perguntas_a_conversa_e_esquecida(self) -> None:
        frases = [_pergunta(1), _pergunta(2), _pergunta(3)]
        m = self.montagem([llm for _f, llm in frases], saida_json("Answer."))
        self.perguntar(m, frases[0][0])
        self.relogio_da_memoria.avancar(29 * 60)
        self.assertIn("BEGIN_HISTORY", self.perguntar(m, frases[1][0]))
        self.relogio_da_memoria.avancar(30 * 60)
        self.assertNotIn("BEGIN_HISTORY", self.perguntar(m, frases[2][0]))

    def test_new_conversation_esquece_logo_sem_llm_nem_claude(self) -> None:
        frases = [_pergunta(1), _pergunta(2)]
        m = self.montagem([frases[0][1], frases[1][1]], saida_json("Answer."))
        self.perguntar(m, frases[0][0])
        for dito in ("New conversation.", "hey jarvis, forget this conversation"):
            with self.subTest(dito=dito):
                pedidos_ao_llm = len(m.llm.pedidos)
                m.falados.clear()
                m.ouvir(dito)
                self.assertEqual(len(m.falados), 1)
                self.assertIn(m.falados[0], opcoes_da_frase(app._TEXTOS["en"]["memoria_nova_conversa"]))
                self.assertEqual(len(m.llm.pedidos), pedidos_ao_llm, "nunca vai ao LLM")
                self.assertEqual(m.jarvis.historico.trocas(), ())
        self.assertEqual(len(self.arranque.processos), 1)
        entrada = self.perguntar(m, frases[1][0])
        self.assertEqual(entrada, pergunta_geral.texto_do_pedido("What is the capital of country 2?", ConfigPerguntas(), "en", HOJE))

    def test_nova_conversa_em_portugues(self) -> None:
        m = self.montagem(lingua="pt")
        m.jarvis.historico.acrescentar("pergunta", "resposta")
        m.ouvir("Esquece esta conversa.")
        self.assertEqual(m.falados, ["Está bem, começamos uma conversa nova."])
        self.assertEqual(m.jarvis.historico.trocas(), ())

    def test_respostas_falhadas_recusadas_canceladas_ou_tecnicas_nunca_entram(self) -> None:
        frases = [_pergunta(numero) for numero in range(1, 6)]
        m = self.montagem(
            [llm for _f, llm in frases],
            "isto nao e JSON",
            saida_json(is_error=True),
            saida_json("```bash\nrm -rf /\n```"),
            "bloqueia",
            saida_json("Answer."),
        )
        for frase, _llm in frases[:3]:
            self.perguntar(m, frase)
        self.assertEqual(m.jarvis.historico.trocas(), ())
        # Cancelada a meio por um cala-te.
        self.arranque.a_correr.clear()
        m.ouvir(frases[3][0])
        self.assertTrue(self.arranque.a_correr.wait(5.0))
        m.ouvir("cala-te")
        self.esperar(m)
        self.assertEqual(self.arranque.ultimo.comportamento, "bloqueia")
        self.assertTrue(self.arranque.ultimo.morto.is_set())
        self.assertEqual(m.jarvis.historico.trocas(), ())
        # Recusada (financeira) nem chega ao claude.
        m.ouvir("what is the price of bitcoin today")
        self.assertEqual(m.jarvis.historico.trocas(), ())
        m.ouvir("wake up")
        self.assertNotIn("BEGIN_HISTORY", self.perguntar(m, frases[4][0]))

    def test_resposta_descartada_nao_entra(self) -> None:
        m = self.montagem()
        consulta = m.jarvis.perguntas.nova("what football games are on today")
        m.jarvis._consulta = consulta
        consulta.correr = lambda ao_texto=None: pergunta_geral.ResultadoDaPergunta("respondida", "Benfica plays tonight.")
        m.jarvis._cancelar_pergunta("pedido novo")
        m.jarvis._consultar(consulta)
        self.assertEqual(m.jarvis.historico.trocas(), ())

    # -- caderno: guardar

    def test_remember_faz_recap_e_so_grava_depois_do_sim(self) -> None:
        m = self.montagem()
        m.ouvir("Remember that my favourite team is Benfica.")
        self.assertEqual(m.falados, ["Remember: My favourite team is Benfica - save it?"])
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(self.factos_no_ficheiro(), [], "nada gravado antes do sim")
        self.assertEqual(m.llm.pedidos, [], "nunca vai ao LLM")
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(self.factos_no_ficheiro(), ["My favourite team is Benfica."])
        self.assertEqual(m.falados[-1], "Got it, I'll remember that.")
        self.assertEqual(self.arranque.processos, [])
        self.assertEqual(m.canal.recebidos, [])

    def test_lembra_te_em_portugues(self) -> None:
        m = self.montagem(lingua="pt")
        m.ouvir("Lembra-te que a minha filha se chama Ana.")
        self.assertEqual(m.falados, ["Lembrar: A minha filha se chama Ana - guardo?"])
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(self.factos_no_ficheiro(), ["A minha filha se chama Ana."])
        self.assertEqual(m.falados[-1], "Guardado, vou lembrar-me disso.")

    def test_abort_nao_ou_prazo_nao_gravam_nada(self) -> None:
        for resposta in ("abort", "no", None):
            with self.subTest(resposta=resposta):
                m = self.montagem()
                m.ouvir("remember that I live in Braga")
                self.assertTrue(m.jarvis.confirmacao.a_espera)
                if resposta is None:
                    m.avancar(31)
                    m.jarvis.verificar_tempo()
                    self.assertEqual(m.falados[-1], "No answer, so I cancelled. My memory is unchanged.")
                else:
                    m.avancar()
                    m.ouvir(resposta)
                    self.assertEqual(m.falados[-1], "Cancelled, my memory is unchanged.")
                self.assertFalse(m.jarvis.confirmacao.a_espera)
                self.assertEqual(self.factos_no_ficheiro(), [])

    def test_correcao_a_um_facto_nao_vai_ao_llm_e_nao_grava(self) -> None:
        m = self.montagem()
        m.ouvir("remember that I live in Braga")
        m.avancar()
        m.ouvir("no, change Braga to Porto")
        self.assertEqual(m.llm.pedidos, [])
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(self.factos_no_ficheiro(), [])

    def test_caderno_cheio_avisa_e_nao_faz_recap(self) -> None:
        m = self.montagem()
        for numero in range(50):
            self.caderno.acrescentar(f"fact number {numero} is fine")
        m.ouvir("remember that I live in Braga")
        self.assertEqual(m.falados, ["My notebook's full. Ask me to forget something first."])
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(len(self.factos_no_ficheiro()), 50)
        self.assertNotIn("I live in Braga.", self.factos_no_ficheiro())

    def test_segredos_sao_recusados_antes_do_recap_e_nunca_escritos(self) -> None:
        for frase in (
            "remember that my password is hunter2",
            "remember that the door code is 4321",
            "remember that my API key is sk-123",
            "lembra-te que a minha palavra-passe é gato",
        ):
            with self.subTest(frase=frase):
                m = self.montagem()
                m.ouvir(frase)
                self.assertEqual(m.falados, ["I don't keep passwords, codes or other secrets, so I didn't save that."])
                self.assertFalse(m.jarvis.confirmacao.a_espera)
                self.assertFalse(self.caminho.exists())

    def test_dados_financeiros_sao_recusados_antes_do_recap(self) -> None:
        for frase in ("remember that my IBAN is PT50 1234", "remember that my salary is paid on the 25th"):
            with self.subTest(frase=frase):
                m = self.montagem()
                m.ouvir(frase)
                self.assertEqual(m.falados, ["I don't keep money or bank details, so I didn't save that."])
                self.assertFalse(m.jarvis.confirmacao.a_espera)
                self.assertFalse(self.caminho.exists())

    def test_compra_e_venda_continuam_recusadas_pela_regra_financeira(self) -> None:
        m = self.montagem()
        m.ouvir("remember that I want to buy bitcoin tomorrow")
        self.assertEqual(m.falados, ["Sorry, I don't do money and trading requests by voice."])
        self.assertEqual(m.jarvis.medidas[-1].intencao, "recusado")
        self.assertEqual(m.jarvis.medidas[-1].desfecho, "recusado")
        self.assertFalse(self.caminho.exists())

    def test_sem_facto_ou_ja_guardado(self) -> None:
        m = self.montagem()
        m.ouvir("remember that")
        self.assertEqual(m.falados, ["Say remember that, and then what to keep."])
        self.caderno.acrescentar("I live in Braga")
        m.ouvir("remember that I live in Braga")
        self.assertEqual(m.falados[-1], "I already remember that.")
        self.assertFalse(m.jarvis.confirmacao.a_espera)

    def test_sem_caderno_diz_que_nao_esta_disponivel(self) -> None:
        m = self.montagem(caderno=False)
        m.ouvir("remember that I live in Braga")
        self.assertEqual(m.falados, ["I can't reach my notebook right now, so nothing was saved."])
        self.assertFalse(m.jarvis.confirmacao.a_espera)

    def test_erro_ao_escrever_diz_que_nada_mudou(self) -> None:
        m = self.montagem()
        m.ouvir("remember that I live in Braga")
        m.avancar()
        with mock.patch("jarvis.memoria.os.replace", side_effect=PermissionError("negado")):
            m.ouvir("yes")
        self.assertEqual(m.falados[-1], "I couldn't write to my notebook, so nothing changed.")
        self.assertEqual(self.factos_no_ficheiro(), [])

    # -- caderno: apagar e listar

    def test_forget_apaga_o_facto_mais_parecido_so_depois_do_sim(self) -> None:
        m = self.montagem()
        self.caderno.acrescentar("I live in Braga")
        self.caderno.acrescentar("My favourite team is Benfica")
        m.ouvir("forget that my favourite team is Benfica")
        self.assertEqual(m.falados, ["Forget: My favourite team is Benfica - delete it?"])
        self.assertEqual(len(self.factos_no_ficheiro()), 2)
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(self.factos_no_ficheiro(), ["I live in Braga."])
        self.assertEqual(m.falados[-1], "Forgotten.")

    def test_forget_sem_nada_parecido_nao_apaga(self) -> None:
        m = self.montagem()
        self.caderno.acrescentar("I live in Braga")
        m.ouvir("forget that my cat is black")
        self.assertEqual(m.falados, ["I don't remember anything like that, so nothing was deleted."])
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(self.factos_no_ficheiro(), ["I live in Braga."])

    def test_forget_cancelado_nao_apaga(self) -> None:
        m = self.montagem()
        self.caderno.acrescentar("I live in Braga")
        m.ouvir("forget that I live in Braga")
        m.avancar()
        m.ouvir("abort")
        self.assertEqual(self.factos_no_ficheiro(), ["I live in Braga."])

    def test_what_do_you_remember_le_localmente(self) -> None:
        m = self.montagem()
        m.ouvir("What do you remember about me?")
        self.assertEqual(m.falados, ["I don't remember anything about you yet."])
        self.caderno.acrescentar("I live in Braga")
        m.ouvir("What do you remember about me?")
        self.assertEqual(m.falados[-1], "I remember one thing about you: I live in Braga.")
        self.caderno.acrescentar("My favourite team is Benfica")
        m.ouvir("what do you remember about me")
        self.assertEqual(m.falados[-1], "I remember 2 things about you. The list is on screen.")
        self.assertIn("ecra | 1. I live in Braga.", m.log.texto())
        self.assertIn("ecra | 2. My favourite team is Benfica.", m.log.texto())
        self.assertEqual(m.llm.pedidos, [])
        self.assertEqual(self.arranque.processos, [], "nunca vai ao Claude")

    def test_o_que_te_lembras_de_mim_em_portugues(self) -> None:
        m = self.montagem(lingua="pt")
        self.caderno.acrescentar("Moro em Braga")
        self.caderno.acrescentar("Gosto de chá")
        m.ouvir("O que te lembras de mim?")
        self.assertEqual(m.falados, ["Lembro-me de 2 coisas sobre ti. A lista está no ecrã."])

    # -- caderno nas perguntas gerais

    def test_os_factos_vao_com_cada_pergunta_geral(self) -> None:
        m = self.montagem([_pergunta(1)[1]], saida_json("Answer."))
        self.caderno.acrescentar("I live in Braga")
        entrada = self.perguntar(m, _pergunta(1)[0])
        self.assertIn('- "I live in Braga."', entrada)
        self.assertNotIn("Braga", " ".join(self.arranque.ultimo.argv))


# --- Frases recentes como contexto do interprete local ----------------------------

NOMES_DA_REFERENCIA = ("atlas", "crypto-radar", "jarvis")
ADD_TESTS = "Add tests for the login flow."
DITADO_EN = resposta_llm("ditar_prompt", "atlas", "Fix the footer.")


def _contexto_enviado(pedido: list[dict]) -> str | None:
    """A mensagem de contexto de um pedido ao LLM (a penultima), se existe."""
    penultima = pedido[-2] if len(pedido) >= 2 else {}
    conteudo = penultima.get("content", "")
    return conteudo if penultima.get("role") == "user" and conteudo.startswith("Context, not a transcript") else None


class TestContextoDoInterpreteNaApp(unittest.TestCase):
    def montagem(self, respostas, **kw) -> Montagem:
        m = Montagem(respostas, lingua="en", nomes=NOMES_DA_REFERENCIA, **kw)
        self.addCleanup(m.jarvis.fechar)
        return m

    def test_a_primeira_frase_vai_sem_contexto_e_a_seguinte_com_ela(self) -> None:
        m = self.montagem(
            [
                resposta_llm("ditar_prompt", "crypto-radar", ADD_TESTS),
                resposta_llm("ditar_prompt", "jarvis", ADD_TESTS),
            ]
        )
        m.ouvir("tell crypto-radar to add tests for the login flow")
        self.assertIsNone(_contexto_enviado(m.llm.pedidos[0]))
        self.assertEqual(m.llm.pedidos[0][-1]["content"], "tell crypto-radar to add tests for the login flow")
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("crypto-radar", ADD_TESTS)])
        m.avancar()
        m.ouvir("send that to jarvis too")
        contexto = _contexto_enviado(m.llm.pedidos[1])
        self.assertIsNotNone(contexto)
        self.assertIn('said "tell crypto-radar to add tests for the login flow"', contexto)
        self.assertIn(f'project crypto-radar, prompt "{ADD_TESTS}", done', contexto)
        self.assertEqual(m.llm.pedidos[1][-1]["content"], "send that to jarvis too")
        # O pedido referido, no projeto dito, e ainda com recap e sim.
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [("crypto-radar", ADD_TESTS)])
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("crypto-radar", ADD_TESTS), ("jarvis", ADD_TESTS)])

    def test_o_desfecho_do_recap_atualiza_a_frase(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "atlas", "Fix the header."), DITADO_EN])
        m.ouvir("in atlas fix the header")
        self.assertEqual(m.jarvis.frases_recentes.frases()[-1].feito, "waiting for yes")
        m.avancar()
        m.ouvir("no")
        self.assertEqual(m.jarvis.frases_recentes.frases()[-1].feito, "cancelled")
        m.avancar()
        m.ouvir("same for jarvis")
        self.assertIn('prompt "Fix the header.", cancelled', _contexto_enviado(m.llm.pedidos[1]))

    def test_o_prazo_do_recap_tambem_atualiza(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "atlas", "Fix the header.")])
        m.ouvir("in atlas fix the header")
        m.avancar(m.jarvis.confirmacao.limite_s + 1)
        m.jarvis.verificar_tempo()
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.jarvis.frases_recentes.frases()[-1].feito, "no answer, not sent")

    def test_teto_de_frases_no_contexto(self) -> None:
        respostas = [resposta_llm("ditar_prompt", "atlas", f"Fix bug {n}.") for n in range(1, 10)]
        m = self.montagem(respostas)
        for n in range(1, 9):
            m.ouvir(f"in atlas fix bug {n}")
            m.avancar()
            m.ouvir("no")
            m.avancar()
        m.ouvir("same for jarvis")
        contexto = _contexto_enviado(m.llm.pedidos[-1])
        self.assertEqual(contexto.count("\n- said"), 5)
        self.assertNotIn("fix bug 3\"", contexto)
        self.assertIn("fix bug 4\"", contexto)
        self.assertIn("fix bug 8\"", contexto)
        self.assertLessEqual(len(contexto), CARACTERES_DO_CONTEXTO)

    def test_cortesia_social_e_memoria_nao_entram_e_a_recusa_entra_sem_texto(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "jarvis", "Fix the header.")])
        for frase in ("thanks", "how are you", "what do you remember about me", "buy 100 euros of bitcoin"):
            m.ouvir(frase)
            m.avancar()
        frases = m.jarvis.frases_recentes.frases()
        self.assertEqual([(f.intencao, f.frase, f.prompt) for f in frases], [(INTENCAO_RECUSADA, "", "")])
        m.ouvir("same for jarvis")
        contexto = _contexto_enviado(m.llm.pedidos[-1])
        self.assertIn("said (not kept) -> recusado", contexto)
        self.assertNotIn("bitcoin", contexto)

    def test_nova_conversa_esquece_as_frases_recentes(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "atlas", "Fix the header."), DITADO_EN])
        m.ouvir("in atlas fix the header")
        m.avancar()
        m.ouvir("no")
        m.avancar()
        m.ouvir("new conversation")
        self.assertEqual(m.jarvis.frases_recentes.frases(), ())
        m.avancar()
        m.ouvir("in atlas fix the footer")
        self.assertIsNone(_contexto_enviado(m.llm.pedidos[-1]))

    def test_factos_com_uma_palavra_em_comum_vao_no_contexto(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caderno = CadernoDeFactos(Path(pasta) / "factos.json", nomes_de_projeto=NOMES_DA_REFERENCIA)
            caderno.acrescentar("My favourite team is Benfica")
            caderno.acrescentar("I live in Braga")
            m = self.montagem(
                [
                    resposta_llm("pergunta_geral", "", "Did Benfica win?"),
                    resposta_llm("pergunta_geral", "", "What is the capital of Peru?"),
                ],
                caderno=caderno,
            )
            m.ouvir("same question for benfica")
            contexto = _contexto_enviado(m.llm.pedidos[0])
            self.assertIn('- "My favourite team is Benfica."', contexto)
            self.assertNotIn("Braga", contexto)
            m.jarvis.frases_recentes.limpar()
            m.avancar()
            m.ouvir("same question for peru")
            self.assertIsNone(_contexto_enviado(m.llm.pedidos[1]), "sem frases e sem factos parecidos")

    def test_frase_sem_referencia_vai_sem_contexto(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "atlas", "Fix the header."), DITADO_EN])
        m.ouvir("in atlas fix the header")
        m.avancar()
        m.ouvir("no")
        m.avancar()
        m.ouvir("in atlas fix the footer")
        self.assertEqual(len(m.jarvis.frases_recentes.frases()), 2)
        self.assertIsNone(_contexto_enviado(m.llm.pedidos[1]))

    def test_o_contexto_nunca_da_o_projeto(self) -> None:
        m = self.montagem(
            [
                resposta_llm("ditar_prompt", "crypto-radar", ADD_TESTS),
                resposta_llm("ditar_prompt", "crypto-radar", ADD_TESTS),
            ]
        )
        m.ouvir("tell crypto-radar to add tests for the login flow")
        m.avancar()
        m.ouvir("no")
        m.avancar()
        m.ouvir("do the same again")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        recap = m.jarvis.confirmacao.recap
        self.assertIsNone(recap.pedido.projeto)
        self.assertTrue(recap.falta_projeto)
        m.avancar()
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [])


class ForjaQueNomeiaOProjeto(ForjaFalsa):
    """Uma leitura cuja resposta ja diz o projeto."""

    def executar(self, intencao, projeto, texto="", lingua="pt"):
        self.pedidos.append((intencao, projeto, texto))
        return mock.Mock(ecra=(), falado=f"The run on {projeto} is going.")


class TestProjetoAssumidoNoProcesso(unittest.TestCase):
    """Sem projeto dito, o ultimo usado: o ditado so vai com o "sim", a leitura diz de que projeto e."""

    def montagem(self, respostas_llm, *, lingua: str = "en", forja=None) -> Montagem:
        m = Montagem([resposta_llm("ditar_prompt", "atlas", "Fix the login test."), *respostas_llm], lingua=lingua)
        m.jarvis.forja = forja if forja is not None else ForjaFalsa()
        m.ouvir("tell atlas to fix the login test")
        m.avancar(2)
        m.ouvir("yes" if lingua == "en" else "sim")
        m.avancar(2)
        self.assertEqual(m.canal.recebidos, [("atlas", "Fix the login test.")])
        return m

    def test_estado_sem_projeto_diz_o_projeto_assumido(self) -> None:
        m = self.montagem([resposta_llm("estado", "")])
        m.ouvir("what's the status?")
        self.assertEqual(m.jarvis.forja.pedidos, [("estado", "atlas", "")])
        self.assertIn(m.falados[-1], ("On atlas: Run started.", "For atlas: Run started."))
        self.assertIn("projeto nao dito: assumido o ultimo usado, atlas", m.log.texto())

    def test_em_portugues_tambem_diz_o_projeto(self) -> None:
        m = self.montagem([resposta_llm("ler_relatorio", "")], lingua="pt")
        m.ouvir("lê o relatório")
        self.assertEqual(m.jarvis.forja.pedidos, [("ler_relatorio", "atlas", "")])
        self.assertEqual(m.falados[-1], "No atlas: Run started.")

    def test_resposta_que_ja_diz_o_projeto_fica_igual(self) -> None:
        m = self.montagem([resposta_llm("estado", "")], forja=ForjaQueNomeiaOProjeto())
        m.ouvir("what's the status?")
        self.assertEqual(m.falados[-1], "The run on atlas is going.")

    def test_projeto_dito_nao_ganha_prefixo(self) -> None:
        m = self.montagem([resposta_llm("estado", "orbita")])
        m.ouvir("what's the status of orbita?")
        self.assertEqual(m.jarvis.forja.pedidos, [("estado", "orbita", "")])
        self.assertEqual(m.falados[-1], "Run started.")

    def test_sem_forja_a_recusa_tambem_diz_o_projeto(self) -> None:
        m = self.montagem([resposta_llm("estado", "")])
        m.jarvis.forja = None
        m.ouvir("what's the status?")
        self.assertTrue(m.falados[-1].startswith(("On atlas: ", "For atlas: ")), m.falados[-1])

    def test_ditado_sem_projeto_recap_assumido_troca_e_so_envia_com_o_sim(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "", "Add tests to the login page.")])
        m.ouvir("add tests to the login page")
        self.assertEqual(m.falados[-1], "Add tests to the login page, still for atlas - send it?")
        self.assertEqual(len(m.canal.recebidos), 1, "nada enviado antes do sim")
        m.avancar(2)
        m.ouvir("no, for orbita")
        self.assertEqual(m.falados[-1], "Add tests to the login page, for orbita - send it?")
        self.assertEqual(len(m.canal.recebidos), 1)
        m.avancar(2)
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos[-1], ("orbita", "Add tests to the login page."))

    def test_pedido_de_dinheiro_sem_projeto_nunca_tem_recap(self) -> None:
        m = self.montagem([resposta_llm("ditar_prompt", "", "Buy ten shares of Tesla.")])
        m.ouvir("buy ten shares of tesla")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertFalse(any(fala.endswith("send it?") for fala in m.falados[2:]), m.falados)
        m.avancar(2)
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos, [("atlas", "Fix the login test.")])
