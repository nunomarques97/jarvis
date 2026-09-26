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
  * o arranque em paralelo e o canal real com pecas falsas;
  * o RealtimeSTT ja nao e importado pelo caminho vivo.

Corre com:

    .venv\Scripts\python -m unittest tests.test_app -v
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from jarvis import acoes_locais, app, voz
from jarvis.acoes_locais import AcaoError, ResultadoAcao
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
from jarvis.config import Config, ConfigInterprete, ConfigOuvido, Projeto
from jarvis.interprete import Interprete, MotorIndisponivel
from jarvis.ouvido import GATILHO_ATIVACAO, GATILHO_TECLA, Frase, TeclaDoFicheiro
from jarvis.resposta_falada import FRASE_RECURSO_SO_TECNICO, PREFIXO_DA_RESPOSTA_DO_CLAUDE
from jarvis.sessoes import Entrega
from jarvis.stt import MotorBase, MotorIndisponivel as SttIndisponivel
from jarvis.voz import ResultadoFala

RAIZ = Path(__file__).resolve().parent.parent
NOMES = ("atlas", "orbita")


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


def config_de_teste(lingua: str = "pt", *, nomes: tuple[str, ...] = NOMES, **ajustes) -> Config:
    return Config(
        microfone="Microfone Ficticio de Teste",
        projetos=tuple(Projeto(nome, Path("D:/caminho/para") / nome) for nome in nomes),
        ouvido=ConfigOuvido(lingua=lingua),
        interprete=ConfigInterprete(**ajustes),
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
            falar=falar,
            calar=calar,
            executar_local=executar_local,
            relogio=self.relogio,
        )

    def ouvir(self, texto: str, *, gatilho: str = GATILHO_TECLA, fim: float | None = None) -> None:
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
                score_ativacao=0.9 if gatilho == GATILHO_ATIVACAO else None,
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

    def test_estado_sem_forja_recapitula_e_diz_que_nao_esta_disponivel(self) -> None:
        m = Montagem([resposta_llm("estado", "atlas")])
        m.ouvir("como está o run do atlas")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        m.avancar()
        m.ouvir("sim")
        self.assertIn("não estão disponíveis", m.falados[-1])
        self.assertIn("forja | indisponivel", m.log.texto())

    def test_conversa_sem_projeto_pede_o_projeto_e_nao_envia(self) -> None:
        m = Montagem([resposta_llm("conversa", "", "Sim, pode avançar.")])
        m.ouvir("diz-lhe que sim pode avançar")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.canal.recebidos, [])
        self.assertEqual(m.falados, ["Diz em que projeto é a conversa."])

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


# --- Nomes mal ouvidos e correcao sem pedido ------------------------------------------

#: Um projeto ficticio com uma palavra financeira no nome.
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
        self.assertEqual(m.falados[-1], "There is no pending request to correct.")
        self.assertIn("desfecho: sem_pedido", m.log.texto())
        # O prazo de um recap nunca chega a correr.
        m.avancar(m.jarvis.confirmacao.limite_s + 1)
        m.jarvis.verificar_tempo()
        self.assertEqual(m.falados[-1], "There is no pending request to correct.")

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
    def test_resposta_natural_e_falada_com_a_origem(self) -> None:
        m = Montagem([DITADO], canal=CanalFalso(resposta="Está feito, os testes passam."))
        m.ouvir("no atlas corrige o teste do login")
        m.avancar()
        m.ouvir("sim")
        # O canal falso responde dentro de `enviar`; o real responde mais tarde.
        self.assertIn(f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Está feito, os testes passam.", m.falados)

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
        self.assertIn('"sim" envia, "nao, muda X para Y" corrige, "cancela" cancela', texto)
        self.assertEqual(m.falados, ["São 15 horas e 30 minutos."])

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

    def test_main_liga_o_ouvido_e_o_processo_residente(self) -> None:
        self.addCleanup(voz.retomar_a_voz)
        log = LogFalso()
        chamadas = []

        def correr_falso(jarvis, ouvido, **kwargs):
            chamadas.append((jarvis, ouvido, kwargs))
            return 0

        with mock.patch.object(app, "LogDaSessao", lambda *a, **k: log), mock.patch.object(
            app, "construir_ouvido", lambda jarvis, **k: ("ouvido", k)
        ), mock.patch.object(app, "correr", correr_falso), mock.patch.object(
            app.atexit, "register"
        ), mock.patch.object(app, "forcar_consola_utf8"):
            codigo = app.main(["--config", "nao-existe.toml", "--sem-voz", "--com-som"])
        self.assertEqual(codigo, 0)
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


if __name__ == "__main__":
    unittest.main()
