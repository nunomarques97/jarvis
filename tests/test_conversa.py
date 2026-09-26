r"""Testes da conversa maos-livres (jarvis/conversa.py e a janela no jarvis e no ouvido).

Sem microfone, sem Ollama, sem Claude Code e sem som: o canal e falso e
responde com o texto escolhido, o relogio e falso e a voz so regista o texto.
O que protegem:

  * uma resposta do Claude que acaba numa pergunta ouvida abre uma janela de
    8 s sem palavra de ativacao; uma que nao acaba em pergunta, nao;
  * a resposta dita na janela vai TAL E QUAL para o recap (sem LLM) e so um
    "sim" a envia ao projeto da conversa;
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
from jarvis.conversa import JANELA_S, JanelaDeConversa, acaba_em_pergunta, e_para_sair, pede_resposta, resposta_literal
from jarvis.interprete import INTENCAO_RECUSADA
from jarvis.ouvido import ESCUTA_CONVERSA, GATILHO_ATIVACAO, GATILHO_JANELA, GATILHO_TECLA, Ouvido
from jarvis.resposta_falada import resumo_falado
from tests.test_app import DITADO, CanalFalso, Montagem, RelogioFalso
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

    def test_so_a_frase_inteira(self) -> None:
        for texto in ("não saias da conversa sem correr os testes", "sai", "exit", "cancela"):
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
        self.assertEqual(m.jarvis.ouvido.abertas, [8.0])
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


class TestRespostaNaJanela(unittest.TestCase):
    def test_resposta_literal_recap_e_sim_envia(self) -> None:
        m = montagem_em_conversa()
        pedidos_ao_llm = len(m.llm.pedidos)
        m.ouvir("sim, corre a suite inteira", gatilho=GATILHO_JANELA)
        self.assertEqual(len(m.llm.pedidos), pedidos_ao_llm, "a resposta nao passa pelo LLM")
        self.assertFalse(m.jarvis.janela.aberta())
        self.assertEqual(m.falados[-1], "Responder ao Claude no atlas: sim, corre a suite inteira. Envio?")
        self.assertIn("sim, corre a suite inteira", m.log.texto())
        self.assertEqual(len(m.canal.recebidos), 1, "nada enviado antes do sim")
        m.avancar(1)
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos[-1], ("atlas", "sim, corre a suite inteira"))

    def test_tecla_de_falar_tambem_responde(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("não, espera", gatilho=GATILHO_TECLA)
        self.assertEqual(m.falados[-1], "Responder ao Claude no atlas: não, espera. Envio?")

    def test_cancela_depois_do_recap_nao_envia(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("avança", gatilho=GATILHO_JANELA)
        m.avancar(1)
        m.ouvir("cancela")
        self.assertEqual(len(m.canal.recebidos), 1)

    def test_a_conversa_continua_enquanto_o_claude_pergunta(self) -> None:
        m = montagem_em_conversa()
        m.ouvir("sim", gatilho=GATILHO_JANELA)
        m.avancar(1)
        m.ouvir("sim")
        self.assertEqual(m.canal.recebidos[-1], ("atlas", "sim"))
        self.assertTrue(m.jarvis.janela.aberta(), "a resposta seguinte tambem acabou em pergunta")
        self.assertEqual(m.jarvis.ouvido.abertas, [8.0, 8.0])

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


if __name__ == "__main__":
    unittest.main()
