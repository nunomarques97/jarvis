r"""Testes da conversa maos-livres (jarvis/conversa.py e a janela no jarvis e no ouvido).

Sem microfone, sem Ollama, sem Claude Code e sem som: o canal e falso e
responde com o texto escolhido, o relogio e falso e a voz so regista o texto.
O que protegem:

  * uma resposta do Claude que acaba numa pergunta ouvida abre uma janela de
    8 s sem palavra de ativacao; uma que nao acaba em pergunta, nao;
  * a resposta dita na janela perde so as hesitacoes e o endereco ao jarvis
    (sem LLM); uma curta (ate 5 palavras) vai logo e o jarvis diz "Enviado."
    / "Sent.", uma mais longa vai ao recap e so um "sim" a envia;
  * fora da janela nada se envia sem recap;
  * "sai da conversa", o prazo, "cala-te" e dormir fecham a janela sem enviar;
  * a regra financeira vale tambem na conversa;
  * nenhum codigo nem chamada de ferramenta da resposta do Claude e falado;
  * o ouvido abre a escuta sem palavra de ativacao e fecha-a sozinho.

Corre com:

    .venv\Scripts\python -m unittest tests.test_conversa -v
"""

from __future__ import annotations

import unittest

from jarvis import conversa
from jarvis.app import A_CONVERSA, A_OUVIR
from jarvis.conversa import (
    JANELA_S,
    JanelaDeConversa,
    acaba_em_pergunta,
    e_para_sair,
    e_resposta_curta,
    limpar_resposta,
    pede_resposta,
    resposta_literal,
)
from jarvis.interprete import INTENCAO_RECUSADA, Interpretacao
from jarvis.ouvido import ESCUTA_CONVERSA, GATILHO_ATIVACAO, GATILHO_JANELA, GATILHO_TECLA, Ouvido
from jarvis.resposta_falada import resumo_falado
from jarvis.sessoes import Entrega
from tests.test_app import DITADO, CanalFalso, Montagem, RelogioFalso, resposta_llm
from tests.test_ouvido import SILENCIO, MotorFalso, TeclaFixa, VadFalso, chunk, chunks_em

PERGUNTA = "Já corrigi o teste do login. Queres que corra a suite inteira?"
TECNICA = "Feito.\n```python\nimport os\nos.remove('dados.db')\n```\nQueres que faça commit?"
#: Uma chamada de ferramenta a meio: o filtro corta tudo dali para a frente.
TECNICA_CORTADA = 'Corri os testes.\n<tool_use name="Bash">rm -rf build</tool_use>\nQueres que faça commit?'
PROIBIDOS = ("```", "import os", "os.remove", "tool_use", "rm -rf", "Bash")
VOZ = 0x33


class OuvidoFalso:
    """Regista as janelas pedidas; `ocupado` diz se alguem esta a falar.

    `abertas` so tem as janelas de conversa; as escutas da resposta ao recap
    ficam em `do_recap`.
    """

    def __init__(self) -> None:
        self.abertas: list[float] = []
        self.do_recap: list[float] = []
        self.fechadas = 0
        self.ocupado = False

    def abrir_escuta(self, limite_s: float, *, para: str = ESCUTA_CONVERSA) -> bool:
        (self.abertas if para == ESCUTA_CONVERSA else self.do_recap).append(limite_s)
        return True

    def fechar_escuta(self) -> None:
        self.fechadas += 1


def montagem_em_conversa(resposta: str = PERGUNTA, respostas_llm=None) -> Montagem:
    """Ditado confirmado ao atlas; o Claude responde com `resposta`."""
    m = Montagem([DITADO] + list(respostas_llm or []), canal=CanalFalso(resposta=resposta))
    m.jarvis.ouvido = OuvidoFalso()
    m.ouvir("diz ao atlas para corrigir o teste do login")
    m.avancar(1)
    m.ouvir("sim")
    # a frase falsa comeca 1 s antes do fim: assim a proxima comeca dentro da janela
    m.avancar(1.5)
    return m


# --- Regras puras ---------------------------------------------------------------


class TestPergunta(unittest.TestCase):
    def test_acaba_em_pergunta(self) -> None:
        for texto in ("Queres que avance?", "Avanço? ", "Do you want me to run it?\n", "**Faço commit?**", '"Sigo?"'):
            with self.subTest(texto=texto):
                self.assertTrue(acaba_em_pergunta(texto))
        for texto in ("Feito.", "Porquê? Porque sim.", "", None, "Corri os testes!"):
            with self.subTest(texto=texto):
                self.assertFalse(acaba_em_pergunta(texto))

    def test_so_abre_se_a_pergunta_foi_ouvida(self) -> None:
        self.assertTrue(pede_resposta(PERGUNTA, resumo_falado(PERGUNTA)))
        # uma pergunta cortada pelo filtro da voz nao foi ouvida
        self.assertFalse(pede_resposta(PERGUNTA, "Já corrigi o teste do login."))


class TestSair(unittest.TestCase):
    def test_frases_de_sair(self) -> None:
        for texto in ("sai da conversa", "Sai da conversa.", "hey jarvis, exit the conversation please", "Fim da conversa"):
            with self.subTest(texto=texto):
                self.assertTrue(e_para_sair(texto))

    def test_com_hesitacoes_e_endereco_ao_jarvis(self) -> None:
        for texto in (
            "Uh, exit the conversation.",
            "A Jarvis, exit the conversation.",
            "Exit the conversation, uh.",
            "Uh ans uh A Jarvis, exit the conversation please.",
        ):
            with self.subTest(texto=texto):
                self.assertTrue(e_para_sair(texto, "en"))
        self.assertTrue(e_para_sair("Eh, sai da conversa.", "pt"))

    def test_so_a_frase_inteira(self) -> None:
        for texto in ("não saias da conversa sem correr os testes", "sai", "exit", "cancela", "uh exit"):
            with self.subTest(texto=texto):
                self.assertFalse(e_para_sair(texto))


class TestRespostaLiteral(unittest.TestCase):
    def test_texto_tal_e_qual(self) -> None:
        interpretacao = resposta_literal("  sim,  corre a suite\ninteira ", "atlas")
        self.assertEqual(
            (interpretacao.intencao, interpretacao.projeto, interpretacao.prompt),
            ("conversa", "atlas", "sim, corre a suite inteira"),
        )
        self.assertFalse(interpretacao.so_confirmacao)

    def test_pedido_financeiro_recusado(self) -> None:
        interpretacao = resposta_literal("sim, e compra 10 ações da Tesla", "atlas")
        self.assertEqual(interpretacao.intencao, INTENCAO_RECUSADA)
        self.assertEqual(interpretacao.prompt, "")

    def test_financeiro_curto_recusado(self) -> None:
        for texto, lingua in (("Yes, buy Tesla shares.", "en"), ("sim, vende as ações", "pt")):
            with self.subTest(texto=texto):
                interpretacao = resposta_literal(texto, "atlas", lingua=lingua)
                self.assertEqual(interpretacao.intencao, INTENCAO_RECUSADA)
                self.assertEqual(interpretacao.prompt, "")


class TestLimparResposta(unittest.TestCase):
    def test_frase_do_sponsor_sem_hesitacoes_nem_endereco(self) -> None:
        self.assertEqual(
            limpar_resposta("Uh ans uh A Jarvis answer no, uh not right now.", "en"), "No, not right now."
        )

    def test_hesitacoes_nunca_chegam_ao_texto(self) -> None:
        casos = {
            "Uh, yes.": "Yes.",
            "Yes, uh.": "Yes.",
            "the uh first one": "the first one",
            "Um, eh, no, not right now.": "No, not right now.",
            "Hey Jarvis, tell Claude that the first one.": "The first one.",
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(limpar_resposta(texto, "en"), esperado)

    def test_em_portugues_um_e_uma_palavra_e_ha_tambem(self) -> None:
        self.assertEqual(limpar_resposta("o um", "pt"), "o um")
        self.assertEqual(limpar_resposta("hum, sim, há dois", "pt"), "Sim, há dois")

    def test_jarvis_como_nome_nao_e_endereco(self) -> None:
        self.assertEqual(limpar_resposta("the jarvis one", "en"), "the jarvis one")
        self.assertEqual(limpar_resposta("Jarvis.", "en"), "Jarvis.")

    def test_so_hesitacoes_fica_vazio(self) -> None:
        self.assertEqual(limpar_resposta("Uh. Um.", "en"), "")

    def test_curta_ate_5_palavras(self) -> None:
        for texto in ("Yes.", "the first one", "no, not right now", "No, not right now.", "sim, corre a suite inteira"):
            with self.subTest(texto=texto):
                self.assertTrue(e_resposta_curta(texto))
        for texto in ("", "yes, run the whole suite now", "sim, corre a suite inteira agora"):
            with self.subTest(texto=texto):
                self.assertFalse(e_resposta_curta(texto))


class TestJanela(unittest.TestCase):
    def setUp(self) -> None:
        self.relogio = RelogioFalso()
        self.janela = JanelaDeConversa(relogio=self.relogio)

    def test_janela_de_8_s(self) -> None:
        self.assertEqual(JANELA_S, 8.0)
        estado = self.janela.abrir("atlas")
        self.assertEqual(estado.prazo - estado.aberta_em, 8.0)

    def test_aceita_so_frases_comecadas_dentro_da_janela(self) -> None:
        estado = self.janela.abrir("atlas")
        self.assertTrue(self.janela.aceita(estado.aberta_em + 7.9))
        self.assertFalse(self.janela.aceita(estado.aberta_em - 0.1), "dita antes da pergunta acabar")
        self.assertFalse(self.janela.aceita(estado.prazo + 0.1))

    def test_prazo_fecha_mas_nao_a_meio_de_uma_frase(self) -> None:
        self.janela.abrir("atlas")
        self.relogio.avancar(7.9)
        self.assertIsNone(self.janela.fechar_se_expirou())
        self.relogio.avancar(0.2)
        self.assertIsNone(self.janela.fechar_se_expirou(alguem_a_falar=True))
        self.assertTrue(self.janela.aberta())
        self.assertEqual(self.janela.fechar_se_expirou().projeto, "atlas")
        self.assertFalse(self.janela.aberta())


# --- A conversa no jarvis -----------------------------------------------------


class TestJanelaAbreComPergunta(unittest.TestCase):
    def test_pergunta_do_claude_abre_a_janela_sem_palavra_de_ativacao(self) -> None:
        m = montagem_em_conversa()
        self.assertEqual(m.canal.recebidos, [("atlas", "Corrige o teste do login.")])
        self.assertTrue(m.jarvis.janela.aberta())
        self.assertEqual(m.jarvis.janela.projeto, "atlas")
        # O canal falso responde antes de o jarvis dizer "Enviado.": a escuta
        # aberta para a pergunta fecha enquanto essa fala soa (o jarvis nunca
        # se ouve) e reabre quando ela acaba, com o que falta da janela.
        self.assertEqual(m.jarvis.ouvido.abertas, [8.0, 8.0])
        self.assertEqual(m.jarvis.painel.atual, A_CONVERSA)
        self.assertIn("janela de 8 s a ouvir sem palavra de ativacao", m.log.texto())

    def test_resposta_sem_pergunta_nao_abre(self) -> None:
        m = montagem_em_conversa("Corri os testes e passaram todos.")
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertEqual(m.jarvis.ouvido.abertas, [])

    def test_resposta_com_erro_nao_abre(self) -> None:
        m = Montagem()
        m.jarvis.ouvido = OuvidoFalso()
        from jarvis.sessoes import Entrega

        m.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto="Queres?", erro="caiu"))
        self.assertFalse(m.jarvis.janela.aberta())

    def test_nenhum_conteudo_tecnico_falado(self) -> None:
        m = montagem_em_conversa(TECNICA)
        for proibido in PROIBIDOS:
            self.assertFalse(any(proibido in dito for dito in m.falados), proibido)
        self.assertIn("Resposta do Claude Code, não verificada: Feito. Queres que faça commit?", m.falados)
        self.assertTrue(m.jarvis.janela.aberta())

    def test_pergunta_cortada_pelo_filtro_nao_abre_e_nada_tecnico_e_dito(self) -> None:
        m = montagem_em_conversa(TECNICA_CORTADA)
        for proibido in PROIBIDOS:
            self.assertFalse(any(proibido in dito for dito in m.falados), proibido)
        self.assertFalse(any("commit" in dito for dito in m.falados))
        self.assertFalse(m.jarvis.janela.aberta(), "a pergunta nao foi ouvida")


#: Mais de 5 palavras: vai ao recap.
LONGA = "sim, corre a suite inteira e depois faz commit"


class TestRespostaNaJanela(unittest.TestCase):
    def test_resposta_curta_vai_logo_e_diz_enviado(self) -> None:
        m = montagem_em_conversa()
        pedidos_ao_llm = len(m.llm.pedidos)
        falados_antes = len(m.falados)
        m.ouvir("sim, corre a suite inteira", gatilho=GATILHO_JANELA)
        self.assertEqual(len(m.llm.pedidos), pedidos_ao_llm, "a resposta nao passa pelo LLM")
        self.assertEqual(m.canal.recebidos[-1], ("atlas", "sim, corre a suite inteira"))
        self.assertIn("Enviado.", m.falados[falados_antes:])
        self.assertFalse(any(dito.endswith("Envio?") for dito in m.falados[falados_antes:]), "sem recap")
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertIn("resposta curta na janela, enviada sem recap", m.log.texto())

    def test_resposta_longa_recap_e_sim_envia(self) -> None:
        m = montagem_em_conversa()
        pedidos_ao_llm = len(m.llm.pedidos)
        m.ouvir(LONGA, gatilho=GATILHO_JANELA)
        self.assertEqual(len(m.llm.pedidos), pedidos_ao_llm, "a resposta nao passa pelo LLM")
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertEqual(m.falados[-1], f"Responder ao Claude no atlas: {LONGA}. Envio?")
        self.assertEqual(len(m.canal.recebidos), 1, "nada enviado antes do sim")
        m.avancar(1)
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos[-1], ("atlas", LONGA))
        self.assertNotIn("Enviado.", m.falados)

    def test_tecla_de_falar_tambem_responde(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("não, espera", gatilho=GATILHO_TECLA)
        self.assertEqual(m.canal.recebidos[-1], ("atlas", "não, espera"))
        self.assertIn("Enviado.", m.falados)

    def test_cancela_depois_do_recap_nao_envia(self) -> None:
        m = montagem_em_conversa()
        m.ouvir(LONGA, gatilho=GATILHO_JANELA)
        m.avancar(1)
        m.ouvir("cancela")
        self.assertEqual(len(m.canal.recebidos), 1)

    def test_a_conversa_continua_enquanto_o_claude_pergunta(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("sim", gatilho=GATILHO_JANELA)
        self.assertEqual(m.canal.recebidos[-1], ("atlas", "sim"))
        self.assertTrue(m.jarvis.janela.aberta(), "a resposta seguinte tambem acabou em pergunta")
        # Cada pergunta abre a escuta, e cada "Enviado." do canal falso (que
        # responde antes dele) fecha-a enquanto soa e reabre-a no fim.
        self.assertEqual(m.jarvis.ouvido.abertas, [8.0, 8.0, 8.0, 8.0])

    def test_so_hesitacoes_nao_envia_e_a_janela_continua(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("Hum.", gatilho=GATILHO_JANELA)
        self.assertEqual(len(m.canal.recebidos), 1)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertTrue(m.jarvis.janela.aberta())

    def test_pedido_financeiro_curto_e_recusado_e_nada_enviado(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("sim, vende as ações", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertIn("Isso não faço por voz", m.falados[-1])
        self.assertNotIn("Enviado.", m.falados)
        self.assertEqual(len(m.canal.recebidos), 1)

    def test_sai_da_conversa_fecha_sem_enviar(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("sai da conversa", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.falados[-1], "Saí da conversa.")
        self.assertEqual(len(m.canal.recebidos), 1)
        self.assertGreaterEqual(m.jarvis.ouvido.fechadas, 1)
        self.assertEqual(m.jarvis.painel.atual, A_OUVIR)

    def test_pedido_financeiro_na_conversa_e_recusado(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("sim, e vende as ações da Tesla", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertIn("Isso não faço por voz", m.falados[-1])
        self.assertEqual(len(m.canal.recebidos), 1)


class TestEnviarSemRecapNaConfirmacao(unittest.TestCase):
    """`enviar_sem_recap` nunca envia o que nao e uma resposta curta de conversa."""

    def test_longa_ou_sem_conversa_vai_ao_recap(self) -> None:
        casos = (
            Interpretacao(LONGA, "conversa", "atlas", LONGA, "regra", "teste"),
            Interpretacao("sim", "ditar_prompt", "atlas", "sim", "regra", "teste"),
        )
        for interpretacao in casos:
            with self.subTest(intencao=interpretacao.intencao):
                m = Montagem()
                desfecho = m.jarvis.confirmacao.enviar_sem_recap(interpretacao)
                self.assertEqual(desfecho.estado, "pendente")
                self.assertEqual(m.canal.recebidos, [])

    def test_financeiro_recusado_mesmo_que_venha_como_conversa(self) -> None:
        m = Montagem()
        interpretacao = Interpretacao("buy Tesla shares", "conversa", "atlas", "buy Tesla shares", "regra", "teste")
        self.assertEqual(m.jarvis.confirmacao.enviar_sem_recap(interpretacao).estado, "recusado")
        self.assertEqual(m.canal.recebidos, [])

    def test_iniciar_nunca_envia_uma_conversa_curta_sem_o_sim(self) -> None:
        m = Montagem()
        desfecho = m.jarvis.confirmacao.iniciar(Interpretacao("Yes.", "conversa", "atlas", "Yes.", "regra", "teste"))
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(m.canal.recebidos, [])


class TestJanelaFecha(unittest.TestCase):
    def test_timeout_fecha_a_janela_sem_enviar(self) -> None:
        m = montagem_em_conversa(respostas_llm=[DITADO])
        m.avancar(JANELA_S + 0.1)
        m.jarvis.verificar_tempo()
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertIn("8 s sem resposta; nada enviado", m.log.texto())
        self.assertGreaterEqual(m.jarvis.ouvido.fechadas, 1)
        self.assertEqual(m.jarvis.painel.atual, A_OUVIR)
        # a frase seguinte volta ao caminho normal (interprete)
        pedidos = len(m.llm.pedidos)
        m.ouvir("diz ao atlas para corrigir o teste do login", gatilho=GATILHO_ATIVACAO)
        self.assertEqual(len(m.llm.pedidos), pedidos + 1)

    def test_timeout_espera_por_quem_esta_a_meio_de_falar(self) -> None:
        m = montagem_em_conversa()
        m.jarvis.ouvido.ocupado = True
        m.avancar(JANELA_S + 0.5)
        m.jarvis.verificar_tempo()
        self.assertTrue(m.jarvis.janela.aberta())
        m.jarvis.ouvido.ocupado = False
        m.jarvis.verificar_tempo()
        self.assertFalse(m.jarvis.janela.aberta())

    def test_frase_comecada_antes_da_pergunta_nao_e_resposta(self) -> None:
        m = montagem_em_conversa(respostas_llm=[DITADO])
        aberta_em = m.jarvis.janela.atual.aberta_em
        pedidos = len(m.llm.pedidos)
        # inicio da escuta = fim - 1 s: comecou antes de a janela abrir
        m.ouvir("diz ao atlas para corrigir o teste do login", fim=aberta_em + 0.5)
        self.assertEqual(len(m.llm.pedidos), pedidos + 1, "vai ao interprete, nao e resposta literal")
        self.assertFalse(m.jarvis.janela.aberta())

    def test_cala_te_fecha_a_janela(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("cala-te", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertEqual(len(m.canal.recebidos), 1)

    def test_dormir_fecha_a_janela(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("dorme", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertTrue(m.jarvis.estado.adormecido)

    def test_a_dormir_uma_pergunta_nao_abre_janela(self) -> None:
        m = Montagem()
        m.jarvis.ouvido = OuvidoFalso()
        m.jarvis.estado.adormecido = True
        from jarvis.sessoes import Entrega

        m.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto=PERGUNTA))
        self.assertFalse(m.jarvis.janela.aberta())


# --- A escuta sem palavra de ativacao no ouvido ----------------------------------


class TestEscutaDoOuvido(unittest.TestCase):
    def montar(self, vad=True):
        self.frases = []
        self.relogio = RelogioFalso()
        self.motor = MotorFalso("sim, corre a suite")
        self.ouvido = Ouvido(
            fonte=None,
            motor=self.motor,
            ao_ouvir=self.frases.append,
            tecla=TeclaFixa(),
            detetor=None,
            vad=VadFalso() if vad else None,
            escrever=lambda _l: None,
            relogio=self.relogio,
        )
        return self.ouvido

    def passar(self, valor: int, segundos: float) -> None:
        from jarvis.ouvido import DURACAO_DO_CHUNK_S

        for _ in range(chunks_em(segundos)):
            self.relogio.avancar(DURACAO_DO_CHUNK_S)
            self.ouvido.processar(chunk(valor), False)

    def test_abre_sem_palavra_de_ativacao_e_entrega_a_frase(self) -> None:
        ouvido = self.montar()
        self.assertTrue(ouvido.abrir_escuta(8.0))
        self.assertTrue(ouvido.ocupado)
        self.passar(SILENCIO, 2.0)
        self.passar(VOZ, 1.0)
        self.passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.estado, "repouso")
        ouvido.transcrever_pendentes()
        (frase,) = self.frases
        self.assertEqual((frase.gatilho, frase.texto), (GATILHO_JANELA, "sim, corre a suite"))
        self.assertFalse(ouvido.ocupado)

    def test_fecha_sozinha_sem_fala(self) -> None:
        ouvido = self.montar()
        ouvido.abrir_escuta(1.0)
        self.passar(SILENCIO, 1.2)
        self.assertEqual(ouvido.estado, "repouso")
        self.assertEqual(ouvido.transcrever_pendentes(), 0)
        self.assertEqual(self.motor.recebidos, [])
        self.assertEqual(ouvido.descartadas, 1)

    def test_fechar_escuta_antes_da_fala(self) -> None:
        ouvido = self.montar()
        ouvido.abrir_escuta(8.0)
        self.passar(SILENCIO, 0.5)
        ouvido.fechar_escuta()
        self.passar(SILENCIO, 0.1)
        self.assertEqual(ouvido.estado, "repouso")
        self.passar(VOZ, 1.0)
        self.passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 0, "sem janela nem ativacao, nada e ouvido")

    def test_sem_vad_so_a_tecla(self) -> None:
        ouvido = self.montar(vad=False)
        self.assertFalse(ouvido.abrir_escuta(8.0))
        self.passar(VOZ, 1.0)
        self.assertEqual(ouvido.estado, "repouso")

    def test_a_janela_em_conversa_no_modulo(self) -> None:
        self.assertIs(conversa.JanelaDeConversa, JanelaDeConversa)



# --- Pergunta do Claude pelo caminho headless -------------------------------------------

#: A resposta headless do log da aceitacao: pergunta primeiro, com um caminho, e acaba numa
#: afirmacao ("I won't touch either file until you answer.").
RESPOSTA_HEADLESS = (
    "Which two test files do you mean? I only found one test file for the anomaly code, "
    "`tests/test_anomaly.py`, so I can't tell which pair you're thinking of.\n\n"
    "Please send me the two file names and tell me which one to keep. "
    "I won't touch either file until you answer."
)
DITADO_EN = resposta_llm(
    "ditar_prompt", "atlas", "Ask me which of the two test files I want to keep and wait for my answer."
)


class CanalHeadlessFalso(CanalFalso):
    """O canal falso a responder pelo caminho headless (`claude -p`), como na aceitacao."""

    def enviar(self, projeto: str, texto: str, ao_responder) -> bool:
        self.recebidos.append((projeto, texto))
        if self.resposta is not None:
            ao_responder(projeto, Entrega(projeto=projeto, caminho="headless", texto=self.resposta))
        return self.aberta


def montagem_headless_em_ingles() -> Montagem:
    m = Montagem([DITADO_EN], lingua="en", canal=CanalHeadlessFalso(resposta=RESPOSTA_HEADLESS))
    m.jarvis.ouvido = OuvidoFalso()
    m.ouvir("tell atlas to ask me which of the two test files I want to keep and wait for my answer")
    m.avancar(1)
    m.ouvir("yes")
    m.avancar(1.5)
    return m


class TestPerguntaDoClaudeNoCaminhoHeadless(unittest.TestCase):
    def test_a_pergunta_e_falada_sem_o_caminho_e_abre_a_janela(self) -> None:
        m = montagem_headless_em_ingles()
        self.assertEqual(len(m.canal.recebidos), 1)
        falada = next(dito for dito in m.falados if dito.startswith("Claude says:"))
        self.assertIn("Which two test files do you mean?", falada)
        for proibido in ("tests/", "test_anomaly", ".py", "`"):
            self.assertNotIn(proibido, falada)
        self.assertTrue(m.jarvis.janela.aberta())
        self.assertEqual(m.jarvis.janela.projeto, "atlas")
        # Aberta para a pergunta e reaberta depois do "Sent." (ver acima).
        self.assertEqual(m.jarvis.ouvido.abertas, [JANELA_S, JANELA_S])
        self.assertIn("caminho=headless", m.log.texto())

    def test_respostas_curtas_vao_logo_e_diz_sent(self) -> None:
        for dito, enviado in (
            ("Yeah.", "Yeah."),
            ("Yes.", "Yes."),
            ("the first one", "the first one"),
            ("no, not right now", "no, not right now"),
            ("Uh ans uh A Jarvis answer no, uh not right now.", "No, not right now."),
        ):
            with self.subTest(dito=dito):
                m = montagem_headless_em_ingles()
                pedidos_ao_llm = len(m.llm.pedidos)
                falados_antes = len(m.falados)
                m.ouvir(dito, gatilho=GATILHO_JANELA)
                self.assertEqual(len(m.llm.pedidos), pedidos_ao_llm, "a resposta nao passa pelo LLM")
                self.assertEqual(m.canal.recebidos, [m.canal.recebidos[0], ("atlas", enviado)])
                novos = m.falados[falados_antes:]
                self.assertIn("Sent.", novos)
                self.assertFalse(any(texto.endswith("Send it?") for texto in novos), "sem recap")
                self.assertFalse(m.jarvis.confirmacao.a_espera)

    def test_resposta_longa_tem_recap_e_so_o_yes_envia(self) -> None:
        m = montagem_headless_em_ingles()
        m.ouvir("Uh keep the newer file and delete the older one.", gatilho=GATILHO_JANELA)
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(
            m.falados[-1], "Reply to Claude in atlas: Keep the newer file and delete the older one. Send it?"
        )
        self.assertEqual(len(m.canal.recebidos), 1, "nada enviado antes do yes")
        m.avancar(1)
        m.ouvir("yes")
        self.assertEqual(m.canal.recebidos[-1], ("atlas", "Keep the newer file and delete the older one."))
        self.assertNotIn("Sent.", m.falados)

    def test_sem_o_yes_nada_e_enviado(self) -> None:
        m = montagem_headless_em_ingles()
        m.ouvir("keep the newer file and delete the older one", gatilho=GATILHO_JANELA)
        m.avancar(1)
        m.ouvir("abort")
        self.assertEqual(len(m.canal.recebidos), 1)
        self.assertFalse(m.jarvis.confirmacao.a_espera)

    def test_exit_the_conversation_fecha_sem_enviar(self) -> None:
        m = montagem_headless_em_ingles()
        m.ouvir("exit the conversation", gatilho=GATILHO_JANELA)
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertEqual(m.falados[-1], "Left the conversation.")
        self.assertEqual(len(m.canal.recebidos), 1)

    def test_exit_com_hesitacao_ou_endereco_fecha_sem_enviar(self) -> None:
        for texto in ("Uh, exit the conversation.", "A Jarvis, exit the conversation.", "Exit the conversation, uh."):
            with self.subTest(texto=texto):
                m = montagem_headless_em_ingles()
                m.ouvir(texto, gatilho=GATILHO_JANELA)
                self.assertFalse(m.jarvis.janela.aberta())
                self.assertFalse(m.jarvis.confirmacao.a_espera)
                self.assertEqual(m.falados[-1], "Left the conversation.")
                self.assertNotIn("Sent.", m.falados)
                self.assertEqual(len(m.canal.recebidos), 1, "sair nunca e enviado ao Claude")

    def test_resposta_curta_financeira_e_recusada(self) -> None:
        m = montagem_headless_em_ingles()
        m.ouvir("Yes, buy Tesla shares.", gatilho=GATILHO_JANELA)
        self.assertEqual(len(m.canal.recebidos), 1)
        self.assertFalse(m.jarvis.confirmacao.a_espera)
        self.assertNotIn("Sent.", m.falados)
        self.assertIn("I don't do that by voice", m.falados[-1])

    def test_fora_da_janela_uma_resposta_curta_nao_e_enviada(self) -> None:
        m = montagem_headless_em_ingles()
        m.avancar(JANELA_S + 0.5)
        m.jarvis.verificar_tempo()
        self.assertFalse(m.jarvis.janela.aberta())
        m.ouvir("Uh ans uh A Jarvis answer no, uh not right now.", gatilho=GATILHO_JANELA)
        self.assertEqual(len(m.canal.recebidos), 1, "fora da janela nada vai sem recap e sim")
        self.assertNotIn("Sent.", m.falados)

    def test_uma_resposta_sem_pergunta_nao_abre(self) -> None:
        m = Montagem([DITADO_EN], lingua="en", canal=CanalHeadlessFalso(resposta="I kept the newer file."))
        m.jarvis.ouvido = OuvidoFalso()
        m.ouvir("tell atlas to keep the newer test file")
        m.avancar(1)
        m.ouvir("yes")
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertEqual(m.jarvis.ouvido.abertas, [])


if __name__ == "__main__":
    unittest.main()
