r"""Testes do jarvis a conversar pelo cerebro (jarvis/app.py com jarvis/cerebro.py).

Nenhum teste chama o Claude Code real nem o Ollama: o cerebro e o `Cerebro`
verdadeiro com um CLI falso que fala o protocolo stream-json
(`tests.test_cerebro.CliFalso`), e um guarda faz falhar o teste se o
`subprocess.Popen` verdadeiro for chamado. O interprete local e substituido
por um que falha se for chamado, salvo nos testes do recurso. Sem microfone e
sem som (a voz e uma funcao que so regista o texto). O que protegem:

  * com o cerebro ativo, o caminho rapido (calar, dormir/acordar, horas e
    data, resposta ao recap, "that's all", ruido na janela, recusa
    financeira) nunca chama o cerebro nem o interprete, e tudo o resto vai
    ao cerebro;
  * a primeira frase e dita logo; o aviso curto so sem frase em ~1 s; o
    segundo aviso numa pesquisa longa; tempo esgotado da uma frase curta;
    nada do turno e dito depois de cala-te, dormir, interrupcao ou frase nova;
  * a conversa continua na mesma sessao pela janela de seguimento, e a fala
    que nao era para o jarvis nao diz nada e mantem o prazo da janela;
  * cerebro indisponivel passa ao interprete com uma frase dita uma vez, e
    volta a ser tentado depois do intervalo; uma pergunta geral no recurso
    diz que nao esta disponivel;
  * as fontes de uma resposta ficam no log e fora da voz;
  * o aquecimento no arranque (o cerebro sim; com ele, o interprete e a
    persona nao fazem nenhum pedido ao Ollama, e o interprete so carrega
    quando o recurso entra) e as linhas do log que o arnes de naturalidade le.

Corre com:

    .venv\Scripts\python -m unittest tests.test_cerebro_na_conversa -v
"""

from __future__ import annotations

import datetime
import subprocess
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from jarvis import app, confirmacao
from jarvis.app import A_FALAR, A_PENSAR, LogDaSessao
from jarvis.cerebro import MARCA_NAO_DIRIGIDA, Cerebro
from jarvis.config import ConfigCerebro
from jarvis.interprete import Interpretacao, MotorIndisponivel
from jarvis.persona import Persona
from jarvis.ouvido import ESCUTA_SEGUIMENTO, GATILHO_ATIVACAO, GATILHO_INTERRUPCAO, GATILHO_JANELA, Frase
from tests.test_app import DITADO, CanalFalso, ForjaFalsa, Montagem, resposta_llm
from tests.test_cerebro import (
    CLI_FALSO,
    HOJE,
    CliFalso,
    bloqueada,
    delta,
    esperar_ate,
    init,
    inicio,
    resposta,
    resultado,
    seccao,
    silencio,
    uso_da_web,
)
from tests.test_sessao_naturalidade import sn

AVISOS = app._TEXTOS["en"]["a_verificar"]
SEGUNDOS_AVISOS = app._TEXTOS["en"]["ainda_a_ver"]
RECURSO = app._TEXTOS["en"]["cerebro_em_baixo"]
SEM_PERGUNTAS = app._TEXTOS["en"]["sem_perguntas"]
FALHOU = app._TEXTOS["en"]["pergunta_falhou"]
SOCIAL = app._TEXTOS["en"]["social_como_estas"]
RECUSA = confirmacao._FRASES["en"]["recusado"]
ESPERA = 5.0


class OuvidoFalso:
    """Regista as escutas sem palavra de ativacao pedidas pelo jarvis."""

    def __init__(self) -> None:
        self.escutas: list[tuple[str, float]] = []
        self.escuta_aberta: str | None = None
        self.ocupado = False

    def abrir_escuta(self, limite_s: float, *, para: str) -> bool:
        self.escutas.append((para, limite_s))
        self.escuta_aberta = para
        return True

    def fechar_escuta(self) -> None:
        self.escuta_aberta = None


class CliEmFalta(CliFalso):
    """O executavel do Claude Code nao existe: cada arranque falha."""

    def __init__(self) -> None:
        super().__init__()
        self.arranques_pedidos = 0

    def __call__(self, argv: list[str], **kwargs):
        self.arranques_pedidos += 1
        raise FileNotFoundError("claude nao encontrado")


class CliQueEspera(CliFalso):
    """Cada arranque espera na barreira (para provar o aquecimento em paralelo)."""

    def __init__(self, barreira: threading.Barrier) -> None:
        super().__init__()
        self.barreira = barreira

    def __call__(self, argv: list[str], **kwargs):
        self.barreira.wait()
        return super().__call__(argv, **kwargs)


def erro_do_cli(processo, _texto: str) -> None:
    processo.emitir(init(), inicio(), {"type": "result", "subtype": "error_during_execution", "is_error": True})


def sem_quota(processo, _texto: str) -> None:
    processo.emitir(init(), {"type": "system", "subtype": "api_retry", "error": "rate_limit"})


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        # Guarda: o subprocess.Popen verdadeiro nunca pode ser chamado.
        self.popen_real: list = []

        def guarda(*args, **kwargs):
            self.popen_real.append(args)
            raise AssertionError("subprocess.Popen real chamado num teste")

        remendo = mock.patch.object(subprocess, "Popen", new=guarda)
        remendo.start()
        self.addCleanup(remendo.stop)
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name) / "neutra"

    def tearDown(self) -> None:
        self.assertEqual(self.popen_real, [], "o subprocess.Popen real foi chamado")

    def montagem(
        self,
        *guioes,
        cli: CliFalso | None = None,
        config: ConfigCerebro | None = None,
        interprete_proibido: bool = True,
        respostas_llm=None,
        **kw,
    ) -> Montagem:
        self.cli = cli if cli is not None else CliFalso(*guioes)
        self.cerebro = Cerebro(
            config or ConfigCerebro(limite_s=5.0),
            "en",
            localizacao="Porto, Portugal",
            nomes_de_projeto=["atlas", "orbita"],
            cli=CLI_FALSO,
            arrancar=self.cli,
            pasta=self.pasta,
            hoje=lambda: HOJE,
        )
        m = Montagem(respostas_llm, lingua="en", cerebro=self.cerebro, **kw)
        self.addCleanup(m.jarvis.fechar)
        m.jarvis.ouvido = OuvidoFalso()
        self.interpretacoes: list[str] = []
        if interprete_proibido:
            original = m.jarvis.interprete.interpretar

            def proibido(texto, contexto=None):
                self.interpretacoes.append(texto)
                raise AssertionError(f"o interprete local foi chamado com {texto!r}")

            m.jarvis.interprete.interpretar = proibido
            self.addCleanup(setattr, m.jarvis.interprete, "interpretar", original)
        return m

    def esperar(self, m: Montagem) -> None:
        self.assertTrue(m.jarvis.esperar_pergunta(ESPERA), "a thread do cerebro nao acabou")

    def esperar_falado(self, m: Montagem, quantos: int) -> None:
        self.assertTrue(esperar_ate(lambda: len(m.falados) >= quantos), m.falados)

    def mensagens(self) -> list[str]:
        return [mensagem for processo in self.cli.processos for mensagem in processo.mensagens]

    def sem_erros(self, m: Montagem) -> None:
        self.assertNotIn("ERRO", m.log.texto())
        self.assertEqual(self.interpretacoes, [], "o interprete local nunca e chamado")
        self.assertEqual(m.llm.pedidos, [], "o LLM local nunca e chamado")


# --- Caminho rapido e cerebro ---------------------------------------------------


class TestCaminhoRapidoEOCerebro(_Base):
    def test_uma_frase_fora_do_caminho_rapido_vai_ao_cerebro(self) -> None:
        m = self.montagem(resposta("Happy to help. ", "What would you like to cook first?"))
        m.ouvir("I want to start learning to cook. Can you help me?")
        self.esperar(m)
        self.assertEqual(" ".join(m.falados), "Happy to help. What would you like to cook first?")
        self.assertEqual(
            seccao(self.mensagens()[0], "SPEECH"), ['"I want to start learning to cook. Can you help me?"']
        )
        self.assertEqual(m.canal.recebidos, [])
        self.assertFalse(m.jarvis.confirmacao.a_espera, "a conversa nunca abre um recap")
        self.assertIn("intencao=cerebro projeto=- origem=cerebro", m.log.texto())
        self.sem_erros(m)

    def test_o_caminho_rapido_nunca_chama_o_cerebro_nem_o_interprete(self) -> None:
        m = self.montagem()
        m.ouvir("what time is it")
        m.ouvir("what's the date today")
        m.ouvir("what is the bitcoin price today")
        m.ouvir("go to sleep")
        self.assertTrue(m.jarvis.estado.adormecido)
        m.ouvir("hey jarvis, what time is it", gatilho=GATILHO_ATIVACAO)
        self.assertFalse(m.jarvis.estado.adormecido)
        m.ouvir("go to sleep")
        m.ouvir("hey jarvis, wake up", gatilho=GATILHO_ATIVACAO)
        self.assertFalse(m.jarvis.estado.adormecido)
        m.ouvir("be quiet")
        self.assertEqual([local[0] for local in m.locais], ["horas", "horas", "horas"])
        self.assertIn(m.falados[2], RECUSA)
        self.assertEqual(self.cli.processos, [], "nenhum processo do cerebro arrancou")
        self.sem_erros(m)

    def test_respostas_ao_recap_nunca_chegam_ao_cerebro(self) -> None:
        m = self.montagem()
        for resposta_dita, enviado in (("yes", True), ("abort", False), ("no, change login to logout", False)):
            with self.subTest(resposta=resposta_dita):
                m.canal.recebidos.clear()
                m.jarvis.confirmacao.iniciar(
                    Interpretacao(
                        "tell atlas to fix the login test", "ditar_prompt", "atlas", "Fix the login test.", "regra", "teste"
                    )
                )
                self.assertTrue(m.jarvis.confirmacao.a_espera)
                m.avancar(1)
                m.ouvir(resposta_dita, gatilho=GATILHO_JANELA)
                self.assertEqual(bool(m.canal.recebidos), enviado)
                m.jarvis.confirmacao.cancelar("fim do caso")
        self.assertEqual(self.mensagens(), [], "o cerebro nunca recebeu nada")
        self.assertEqual(self.interpretacoes, [])

    def test_thats_all_e_ruido_na_janela_nao_vao_ao_cerebro(self) -> None:
        m = self.montagem(resposta("Sure, what would you like to know?"))
        m.ouvir("can you tell me something about Lisbon")
        self.esperar(m)
        self.assertTrue(m.jarvis.seguimento.aberta())
        m.avancar(1.5)
        m.ouvir("um", gatilho=GATILHO_JANELA)
        self.assertTrue(m.jarvis.seguimento.aberta(), "ruido nao gasta a janela")
        m.ouvir("that's all", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.seguimento.aberta())
        self.assertEqual(len(self.mensagens()), 1)
        self.assertEqual(m.falados, ["Sure, what would you like to know?"])
        self.sem_erros(m)

    def test_how_are_you_doing_e_add_you_doing_vao_ao_cerebro_e_nunca_ao_estado(self) -> None:
        m = self.montagem(resposta("I'm doing well, thanks."), resposta("All good here, and you?"))
        m.jarvis.forja = ForjaFalsa()
        m.ouvir("how are you doing")
        self.esperar(m)
        m.avancar(20)
        m.ouvir("add you doing?")
        self.esperar(m)
        self.assertEqual(m.falados, ["I'm doing well, thanks.", "All good here, and you?"])
        self.assertEqual(m.jarvis.forja.pedidos, [], "nunca o estado de um projeto")
        self.assertEqual([seccao(mensagem, "SPEECH") for mensagem in self.mensagens()], [['"how are you doing"'], ['"add you doing?"']])
        self.sem_erros(m)

    def test_cortesia_com_uma_resposta_a_caminho_nao_a_corta(self) -> None:
        porta = threading.Event()
        m = self.montagem(bloqueada("It is sunny in Porto. ", porta=porta, depois=("Light wind.",)))
        m.ouvir("what's the weather like")
        self.esperar_falado(m, 1)
        m.ouvir("okay")
        porta.set()
        self.esperar(m)
        self.assertEqual(m.falados[0], "It is sunny in Porto.")
        self.assertEqual(m.falados[-1], "Light wind.")
        self.assertEqual(len(self.mensagens()), 1, "a cortesia nao foi ao cerebro")


# --- Fala em fluxo e cancelamento --------------------------------------------------


class TestFalaEmFluxo(_Base):
    def test_a_primeira_frase_e_dita_antes_de_o_turno_acabar(self) -> None:
        porta = threading.Event()
        m = self.montagem(bloqueada("It is sunny in Porto. ", porta=porta, depois=("Light wind today.",)))
        m.ouvir("what's the weather like in Porto", ultima_voz=m.relogio() - 0.4)
        self.esperar_falado(m, 1)
        self.assertEqual(m.falados, ["It is sunny in Porto."], "sem aviso: a frase chegou depressa")
        self.assertTrue(m.jarvis._pergunta_a_caminho(), "o turno ainda nao acabou")
        porta.set()
        self.esperar(m)
        self.assertEqual(m.falados, ["It is sunny in Porto.", "Light wind today."])
        historico = m.jarvis.painel.historico
        self.assertLess(historico.index(A_PENSAR), historico.index(A_FALAR), "a bolinha pensa e depois fala")

    def test_o_aviso_curto_so_quando_nao_ha_frase_em_um_segundo(self) -> None:
        porta = threading.Event()
        m = self.montagem(bloqueada(porta=porta, depois=("Here it is.",)), espera_do_aviso_s=0.05)
        m.ouvir("tell me something interesting")
        self.assertEqual(len(m.falados), 1)
        self.assertIn(m.falados[0], AVISOS)
        self.assertIn("cerebro | sem frase pronta em 0.05 s: aviso curto", m.log.texto())
        porta.set()
        self.esperar(m)
        self.assertEqual(m.falados[1:], ["Here it is."])

    def test_segundo_aviso_numa_pesquisa_que_demora(self) -> None:
        porta = threading.Event()

        def pesquisa_longa(processo, _texto: str) -> None:
            processo.emitir(init(), inicio(), uso_da_web("w1"))

            def resto() -> None:
                if porta.wait(ESPERA):
                    processo.emitir(delta("It will rain tomorrow."), resultado("It will rain tomorrow."))

            threading.Thread(target=resto, daemon=True).start()

        m = self.montagem(pesquisa_longa, espera_do_aviso_s=0.05)
        m.jarvis.espera_do_segundo_aviso_s = 0.2
        m.ouvir("will it rain tomorrow in Porto")
        self.esperar_falado(m, 2)
        self.assertIn(m.falados[0], AVISOS)
        self.assertIn(m.falados[1], SEGUNDOS_AVISOS)
        porta.set()
        self.esperar(m)
        self.assertEqual(m.falados[2:], ["It will rain tomorrow."])
        self.assertEqual(len(m.falados), 3, "o segundo aviso so uma vez")
        self.assertIn("pesquisa web: sim (1 uso(s)", m.log.texto())

    def test_sem_pesquisa_nao_ha_segundo_aviso(self) -> None:
        porta = threading.Event()
        m = self.montagem(bloqueada(porta=porta, depois=("Done.",)), espera_do_aviso_s=0.05)
        m.jarvis.espera_do_segundo_aviso_s = 0.1
        m.ouvir("think about it for a moment")
        time.sleep(0.4)
        porta.set()
        self.esperar(m)
        self.assertEqual(len(m.falados), 2)
        self.assertNotIn(m.falados[0], SEGUNDOS_AVISOS)

    def test_tempo_esgotado_da_uma_frase_curta(self) -> None:
        m = self.montagem(silencio, config=ConfigCerebro(limite_s=0.3))
        m.ouvir("tell me a long story")
        self.esperar(m)
        self.assertEqual(len(m.falados), 1)
        self.assertIn(m.falados[0], FALHOU)
        self.assertIn("cerebro | tempo_esgotado", m.log.texto())
        self.assertTrue(m.jarvis._cerebro_ativo(), "uma falha so nao poe o cerebro de parte")

    def _a_meio(self) -> tuple[Montagem, threading.Event]:
        porta = threading.Event()
        m = self.montagem(
            bloqueada("It is sunny in Porto. ", porta=porta, depois=("Never spoken. ", "Also never.")),
            resposta("Why did the chicken cross the road?"),
        )
        m.ouvir("what's the weather like in Porto")
        self.esperar_falado(m, 1)
        return m, porta

    def _nada_mais(self, m: Montagem, porta: threading.Event, esperado: list[str]) -> None:
        porta.set()
        self.esperar(m)
        time.sleep(0.2)
        self.assertEqual(m.falados, esperado)
        self.assertFalse(any("Never" in dito for dito in m.falados))
        self.assertEqual(self.cli.processos[0].interrupcoes, 1, "o turno foi interrompido no cerebro")

    def test_cala_te_a_meio_nada_mais_e_dito(self) -> None:
        m, porta = self._a_meio()
        m.ouvir("be quiet")
        self._nada_mais(m, porta, ["It is sunny in Porto."])

    def test_dormir_a_meio_nada_mais_e_dito(self) -> None:
        m, porta = self._a_meio()
        m.ouvir("go to sleep")
        self.assertTrue(m.jarvis.estado.adormecido)
        self._nada_mais(m, porta, list(m.falados))

    def test_frase_nova_durante_o_fluxo_cancela_e_so_a_nova_e_dita(self) -> None:
        m, porta = self._a_meio()
        m.ouvir("forget that, tell me a joke")
        self.esperar(m)
        self._nada_mais(m, porta, ["It is sunny in Porto.", "Why did the chicken cross the road?"])
        fala = seccao(self.mensagens()[-1], "SPEECH")
        self.assertEqual(fala, ['"forget that, tell me a joke"'])
        self.assertTrue(self.cerebro.transcricao()[0].interrompida)

    def test_interrupcao_por_voz_para_o_resto_e_a_frase_dita_por_cima_segue(self) -> None:
        porta = threading.Event()
        m = self.montagem(
            bloqueada("It is sunny in Porto. ", porta=porta, depois=("Never spoken.",)),
            resposta("Pasta is quick: try carbonara."),
        )
        m.jarvis.iniciar()
        falar = m.jarvis._falar

        def falar_e_ser_interrompido(texto: str):
            dito = falar(texto)
            if texto == "It is sunny in Porto.":
                agora = m.relogio()
                m.jarvis.ao_ouvir(
                    Frase(
                        texto="actually, what should I cook tonight",
                        gatilho=GATILHO_INTERRUPCAO,
                        lingua="en",
                        motor="motor-falso",
                        duracao_audio_s=1.0,
                        inicio_da_escuta=agora - 1.0,
                        fim_da_escuta=agora,
                        texto_pronto=agora,
                        latencia_stt_ms=100.0,
                    )
                )
            return dito

        m.jarvis._falar = falar_e_ser_interrompido
        m.ouvir("what's the weather like in Porto")
        self.assertTrue(esperar_ate(lambda: "Pasta is quick: try carbonara." in m.falados), m.falados)
        porta.set()
        self.assertTrue(m.jarvis.esperar_ocioso(ESPERA))
        self.esperar(m)
        time.sleep(0.2)
        self.assertEqual(m.falados, ["It is sunny in Porto.", "Pasta is quick: try carbonara."])
        self.assertIn("interrupcao | a serio", m.log.texto())


# --- Conversa continua pela janela de seguimento ------------------------------------


class TestConversaComContexto(_Base):
    def test_cozinhar_e_o_primeiro_prato_chegam_a_mesma_sessao(self) -> None:
        m = self.montagem(
            resposta("Happy to help. ", "What would you like to cook first?"),
            resposta("Start with a simple omelette. ", "Want the steps?"),
            sons=(sons := []).append,
        )
        m.ouvir("I want to start learning to cook. Can you help me?")
        self.esperar(m)
        ditas_na_primeira = len(m.falados)
        self.assertTrue(m.jarvis.seguimento.aberta(), "a resposta abre a janela de seguimento")
        self.assertEqual(m.jarvis.ouvido.escuta_aberta, ESCUTA_SEGUIMENTO)
        self.assertEqual(sons, ["abrir"])
        m.avancar(1.5)
        m.ouvir("The first dish?", gatilho=GATILHO_JANELA)
        self.esperar(m)
        self.assertEqual(" ".join(m.falados[:ditas_na_primeira]), "Happy to help. What would you like to cook first?")
        self.assertEqual(" ".join(m.falados[ditas_na_primeira:]), "Start with a simple omelette. Want the steps?")
        self.assertEqual(len(self.cli.processos), 1, "uma so sessao")
        mensagens = self.cli.processos[0].mensagens
        self.assertEqual(len(mensagens), 2, "turnos seguidos da mesma sessao")
        self.assertEqual(seccao(mensagens[1], "SPEECH"), ['"The first dish?"'])
        self.assertEqual(seccao(mensagens[1], "HISTORY"), [], "a sessao ja tem o contexto")
        self.assertEqual([troca.fala for troca in self.cerebro.transcricao()], [
            "I want to start learning to cook. Can you help me?",
            "The first dish?",
        ])
        self.assertNotIn("Which project", " ".join(m.falados))
        self.assertTrue(m.jarvis.seguimento.aberta())
        self.sem_erros(m)

    def test_fala_que_nao_era_para_o_jarvis_nao_diz_nada_e_mantem_o_prazo(self) -> None:
        m = self.montagem(resposta("Sure, what would you like to know?"), resposta(MARCA_NAO_DIRIGIDA))
        m.ouvir("can you tell me something about Lisbon")
        self.esperar(m)
        prazo = m.jarvis.seguimento.atual.prazo
        m.avancar(1.5)
        m.ouvir("did you feed the cat this morning", gatilho=GATILHO_JANELA)
        self.esperar(m)
        self.assertEqual(m.falados, ["Sure, what would you like to know?"])
        self.assertTrue(m.jarvis.seguimento.aberta())
        self.assertEqual(m.jarvis.seguimento.atual.prazo, prazo, "o prazo da janela nao muda")
        self.assertIn('"speech": "heard without the wake word', self.mensagens()[1])
        self.assertIn("cerebro | a fala nao era para o jarvis: nada dito", m.log.texto())
        self.sem_erros(m)

    def test_fontes_da_resposta_ficam_no_log_e_fora_da_voz(self) -> None:
        texto = "Benfica beat Porto 2-1 last night.\n\nSources:\n- [BBC Sport](https://bbc.co.uk/sport/x)\n- ESPN report"
        m = self.montagem(resposta(texto))
        m.ouvir("what was the result of benfica against porto")
        self.esperar(m)
        self.assertEqual(m.falados, ["Benfica beat Porto 2-1 last night."])
        self.assertNotIn("Sources", " ".join(m.falados))
        self.assertIn(repr(texto), m.log.texto())
        self.sem_erros(m)


# --- Recurso: cerebro indisponivel ---------------------------------------------------


class TestRecurso(_Base):
    def _sem_cli(self) -> CliEmFalta:
        return CliEmFalta()

    def test_cli_em_falta_passa_ao_interprete_com_uma_frase_dita_uma_vez(self) -> None:
        cli = self._sem_cli()
        m = self.montagem(cli=cli, interprete_proibido=False)
        m.ouvir("how are you doing")
        self.esperar(m)
        self.assertEqual(len(m.falados), 2)
        self.assertIn(m.falados[0], RECURSO)
        self.assertIn(m.falados[1], SOCIAL)
        self.assertIn("cerebro | indisponivel (indisponivel: nao arrancou", m.log.texto())
        self.assertEqual(cli.arranques_pedidos, 1)
        # Enquanto esta de parte: direto ao interprete, sem a frase outra vez.
        m.ouvir("how are you doing")
        self.esperar(m)
        self.assertEqual(len(m.falados), 3)
        self.assertIn(m.falados[2], SOCIAL)
        self.assertEqual(cli.arranques_pedidos, 1, "o cerebro nao foi tentado")
        # Passado o intervalo volta a ser tentado.
        m.avancar(m.jarvis.cerebro.config.reintentar_s + 1)
        m.ouvir("how are you doing")
        self.esperar(m)
        self.assertEqual(cli.arranques_pedidos, 2)
        self.assertIn("cerebro | passou o intervalo", m.log.texto())
        self.assertEqual(sum(dito in RECURSO for dito in m.falados), 1, "a frase do recurso so uma vez")

    def test_executavel_do_claude_por_encontrar_passa_ao_interprete(self) -> None:
        m = self.montagem(interprete_proibido=False)
        self.cerebro._cli = None
        with mock.patch("jarvis.cerebro.localizar_cli", side_effect=FileNotFoundError("sem claude.exe")):
            m.ouvir("how are you doing")
            self.esperar(m)
        self.assertIn(m.falados[0], RECURSO)
        self.assertIn(m.falados[1], SOCIAL)
        self.assertEqual(self.cli.processos, [])
        self.assertIn("nao arrancou: sem claude.exe", m.log.texto())

    def test_sem_quota_ou_sem_sessao_iniciada_passa_ao_interprete(self) -> None:
        def sem_sessao(processo, _texto: str) -> None:
            processo.emitir(init(), {"type": "system", "subtype": "api_retry", "error": "authentication_failed"})

        for guiao, estado in ((sem_quota, "rate_limit"), (sem_sessao, "autenticacao")):
            with self.subTest(estado=estado):
                m = self.montagem(guiao, interprete_proibido=False)
                m.ouvir("how are you doing")
                self.esperar(m)
                self.assertIn(m.falados[0], RECURSO)
                self.assertIn(m.falados[1], SOCIAL)
                self.assertIn(f"cerebro | indisponivel ({estado}", m.log.texto())
                self.assertFalse(m.jarvis._cerebro_ativo())

    def test_duas_falhas_seguidas_passam_ao_interprete(self) -> None:
        m = self.montagem(erro_do_cli, erro_do_cli, interprete_proibido=False)
        m.ouvir("how are you doing")
        self.esperar(m)
        self.assertEqual(len(m.falados), 1)
        self.assertIn(m.falados[0], FALHOU)
        self.assertTrue(m.jarvis._cerebro_ativo(), "uma falha nao chega")
        m.ouvir("how are you doing")
        self.esperar(m)
        self.assertIn(m.falados[1], RECURSO)
        self.assertIn(m.falados[2], SOCIAL)
        self.assertIn("cerebro | indisponivel (2 falhas seguidas", m.log.texto())

    def test_uma_resposta_poe_as_falhas_a_zero(self) -> None:
        m = self.montagem(erro_do_cli, resposta("Fine."), erro_do_cli, interprete_proibido=False)
        for _ in range(3):
            m.ouvir("how are you doing")
            self.esperar(m)
        self.assertTrue(m.jarvis._cerebro_ativo())
        self.assertFalse(any(dito in RECURSO for dito in m.falados))

    def test_pergunta_geral_no_recurso_diz_que_nao_esta_disponivel(self) -> None:
        m = self.montagem(
            sem_quota,
            interprete_proibido=False,
            respostas_llm=[resposta_llm("pergunta_geral", "", "What is the weather in Porto today?")],
        )
        m.ouvir("what's the weather in porto today")
        self.esperar(m)
        self.assertIn(m.falados[0], RECURSO)
        self.assertIn(m.falados[1], SEM_PERGUNTAS)
        self.assertEqual(len(m.falados), 2)
        self.assertIn("pergunta geral | sem o cerebro: nada saiu do PC", m.log.texto())
        self.assertEqual(len(self.mensagens()), 1, "so a primeira tentativa chegou ao cerebro")

    def test_aquecimento_falhado_poe_o_cerebro_de_parte(self) -> None:
        cli = self._sem_cli()
        m = self.montagem(cli=cli, interprete_proibido=False)
        self.assertIn("nao arrancou", m.jarvis.aquecer_cerebro())
        m.ouvir("how are you doing")
        self.assertIn(m.falados[0], RECURSO)
        self.assertIn(m.falados[1], SOCIAL)
        self.assertEqual(cli.arranques_pedidos, 1, "so o aquecimento tentou arrancar")
        # O caminho rapido nunca diz a frase do recurso.
        m2 = self.montagem(cli=self._sem_cli(), interprete_proibido=False)
        m2.jarvis.aquecer_cerebro()
        m2.ouvir("what time is it")
        self.assertEqual(m2.falados, ["São 15 horas e 30 minutos."])


# --- Arranque e log --------------------------------------------------------------------


class _MotorFalso:
    def __init__(self, barreira: threading.Barrier) -> None:
        self.barreira = barreira

    def descrever(self) -> str:
        return "motor-falso"


class _OuvidoDoArranque:
    def __init__(self, barreira: threading.Barrier) -> None:
        self.motor = _MotorFalso(barreira)
        self.barreira = barreira

    def preparar(self) -> float:
        self.barreira.wait()
        return 1.0


class OllamaQueConta:
    """Faz de Ollama para o interprete e para a persona: conta todos os pedidos."""

    def __init__(self, respostas=()) -> None:
        self.respostas = list(respostas)
        self.pedidos: list[str] = []
        self.limite_s = 5.0

    def conversar(self, modelo, mensagens, esquema, *, limite_s=None):
        self.pedidos.append(f"conversar {modelo}")
        if not self.respostas:
            raise MotorIndisponivel("sem resposta feita")
        return self.respostas.pop(0)

    def modelos_instalados(self, limite_s=None):
        self.pedidos.append("modelos_instalados")
        return {}

    def modelos_carregados(self, limite_s=None):
        # O modelo ja esta carregado: a frase nunca mede a VRAM (nvidia-smi).
        self.pedidos.append("modelos_carregados")
        return {"qwen3:8b": (1, 1)}

    def descarregar(self, modelo, limite_s=None):
        self.pedidos.append(f"descarregar {modelo}")

    def conversar_em_fluxo(self, modelo, mensagens, *, parar, limite_s):
        self.pedidos.append(f"conversar_em_fluxo {modelo}")
        yield "Hello."


class _OuvidoPronto:
    def __init__(self) -> None:
        self.motor = _MotorFalso(None)

    def preparar(self) -> float:
        return 1.0


class TestArranqueELog(_Base):
    def _com_ollama_que_conta(self, m: Montagem, respostas=()) -> OllamaQueConta:
        ollama = OllamaQueConta(respostas)
        m.jarvis.interprete.cliente = ollama
        m.jarvis.persona = Persona(ollama, lambda: m.jarvis.interprete.modelo, "en", registar=m.log.linha)
        return ollama

    def test_com_o_cerebro_o_arranque_nao_faz_pedidos_ao_ollama(self) -> None:
        m = self.montagem()
        ollama = self._com_ollama_que_conta(m)
        arranque = app.arrancar(m.jarvis, _OuvidoPronto(), com_voz=False, medir=lambda: None)
        self.assertEqual(ollama.pedidos, [], "nem o interprete nem a persona tocam no Ollama no arranque")
        self.assertEqual(set(arranque.pecas_ms), {"transcricao", "cerebro"})
        self.assertIn("arranque | interprete: nao aquecido (qwen3:8b so carrega se o cerebro", m.log.texto())
        self.assertNotIn("interprete sem LLM", m.log.texto())
        self.assertTrue(self.cerebro.vivo)

    def test_o_interprete_so_carrega_quando_o_recurso_entra_e_fica_no_log(self) -> None:
        m = self.montagem(sem_quota, interprete_proibido=False, canal=CanalFalso())
        ollama = self._com_ollama_que_conta(m, [DITADO])
        app.arrancar(m.jarvis, _OuvidoPronto(), com_voz=False, medir=lambda: None)
        self.assertEqual(ollama.pedidos, [])
        m.ouvir("tell atlas to fix the login test")
        self.esperar(m)
        self.assertIn("conversar qwen3:8b", ollama.pedidos, "o recurso usou o interprete local")
        linha = "interprete | recurso: primeira frase pelo interprete local (qwen3:8b)"
        self.assertEqual(m.log.texto().count(linha), 1)
        self.assertTrue(m.jarvis.confirmacao.a_espera, "o ditado continua a passar pelo recap")
        m.avancar()
        m.ouvir("abort")
        self.assertEqual(m.log.texto().count(linha), 1, "a linha so aparece a primeira vez")

    def test_sem_o_cerebro_o_arranque_aquece_o_interprete(self) -> None:
        m = Montagem(lingua="en")
        self.addCleanup(m.jarvis.fechar)
        ollama = self._com_ollama_que_conta(m)
        arranque = app.arrancar(m.jarvis, _OuvidoPronto(), com_voz=False, medir=lambda: None)
        self.assertIn("interprete", arranque.pecas_ms)
        self.assertIn("modelos_instalados", ollama.pedidos)
        self.assertNotIn("nao aquecido", m.log.texto())

    def test_o_cerebro_aquece_em_paralelo_com_o_resto(self) -> None:
        barreira = threading.Barrier(2, timeout=ESPERA)
        m = self.montagem(cli=CliQueEspera(barreira))
        m.jarvis.interprete.aquecer = mock.Mock(side_effect=AssertionError("o interprete nao aquece com o cerebro"))
        arranque = app.arrancar(m.jarvis, _OuvidoDoArranque(barreira), com_voz=False, medir=lambda: None)
        self.assertEqual(arranque.erros, {}, "as duas so passam a barreira se aquecerem ao mesmo tempo")
        m.jarvis.interprete.aquecer.assert_not_called()
        self.assertIn("arranque | cerebro:", m.log.texto())
        self.assertIn("processo pronto (claude-haiku-4-5)", m.log.texto())
        self.assertTrue(self.cerebro.vivo)
        self.assertEqual(self.cli.processos[0].mensagens, [], "aquecer nao manda nenhuma frase")

    def test_o_log_tem_intencao_pesquisa_tokens_e_a_latencia_que_o_arnes_le(self) -> None:
        inicio_do_log = datetime.datetime(2026, 9, 27, 18, 0, 0)
        parede = [inicio_do_log]
        pasta = Path(tempfile.mkdtemp(dir=self.pasta.parent))
        m = self.montagem(
            resposta("It is sunny in Porto today.", extra=(uso_da_web("w1"),)),
        )
        log = LogDaSessao(pasta=pasta, consola=StringIO(), quando=inicio_do_log, relogio_de_parede=lambda: parede[0])
        self.addCleanup(log.fechar)
        m.jarvis.log = log
        log.linha("jarvis a arrancar | log em logs/jarvis-teste.log")
        log.bruto("   JARVIS PRONTO em 4.2 s (meta <= 30 s)")
        log.bruto("   ouvido: Microfone Ficticio de Teste (MME)")
        m.ouvir("what's the weather like in Porto", ultima_voz=m.relogio() - 0.6)
        self.esperar(m)
        texto = log.caminho.read_text(encoding="utf-8")
        self.assertIn("intencao=cerebro", texto)
        self.assertIn("pesquisa web: sim (1 uso(s), 1 pesquisa(s))", texto)
        self.assertIn("tokens: input=12 cache_read=4800 cache_creation=300 output=40", texto)
        self.assertIn("cerebro | resposta falada: 700 ms desde a ultima voz da frase #1", texto)
        self.assertIn("frase #1 | resposta falada desde a ultima voz: 700 ms", texto)
        leitura = sn.ler_log(texto.splitlines())
        frase = leitura.frases[0]
        self.assertEqual(frase.intencao, "cerebro")
        self.assertEqual(frase.fala_ms, 700.0)
        self.assertEqual(frase.fonte, sn.FONTE_MICROFONE)

    def test_resposta_com_aviso_nao_repete_a_linha_da_frase(self) -> None:
        porta = threading.Event()
        m = self.montagem(bloqueada(porta=porta, depois=("Here it is.",)), espera_do_aviso_s=0.05)
        m.ouvir("tell me something", ultima_voz=m.relogio() - 0.5)
        porta.set()
        self.esperar(m)
        self.assertEqual(m.log.texto().count("frase #1 | resposta falada desde a ultima voz:"), 1)
        self.assertIn("cerebro | resposta falada: 600 ms desde a ultima voz da frase #1", m.log.texto())


class TestCerebroDesligado(unittest.TestCase):
    def test_sem_cerebro_as_frases_seguem_pelo_interprete_como_antes(self) -> None:
        m = Montagem(lingua="en", canal=CanalFalso())
        self.addCleanup(m.jarvis.fechar)
        m.ouvir("how are you doing")
        self.assertEqual(len(m.falados), 1)
        self.assertIn(m.falados[0], SOCIAL)
        self.assertNotIn("cerebro", m.log.texto())


if __name__ == "__main__":
    unittest.main()
