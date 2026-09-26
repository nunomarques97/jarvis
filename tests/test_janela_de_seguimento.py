r"""Testes da janela de seguimento (jarvis/app.py + jarvis/ouvido.py + jarvis/bolinha.py).

Depois de qualquer resposta que a voz do jarvis chegou a dizer, o jarvis fica
a ouvir sem palavra de ativacao durante `[escuta] seguimento_s` (8 s por
omissao): a frase dita nesse tempo e um pedido normal. Estes testes ligam um
Jarvis com pecas falsas (sem LLM real, sem canal real, sem microfone) a um
Ouvido verdadeiro com VAD falso e relogio falso (a `Cena` dos testes da
resposta ao recap). Os sons de abrir e fechar vao para uma lista: nenhum som
toca. O que protegem:

  * a janela abre no fim da voz depois das horas, do estado FORJA, do
    "Sent." e da resposta a uma pergunta geral, e nunca depois do "Let me
    check.", do "um momento" nem de um aviso nao pedido;
  * nunca ha duas janelas: recap pendente > pergunta do Claude > seguimento;
    a dormir, calado ou sem voz nao abre nenhuma;
  * a frase dita na janela passa pelo interprete e pelas mesmas regras
    (efeito so com o sim, dinheiro recusado, sem o atalho da conversa);
  * silencio, ruido, hesitacoes e cortesia nao dizem nada, nao gastam nem
    prolongam a janela;
  * uma fala nova fecha a janela e reabre-a no fim; a tecla e "hey jarvis"
    funcionam dentro e fora dela;
  * um som "abrir" por abertura e um "fechar" quando a escuta acaba sem
    continuar; `[escuta] sons = false` e o --wav sem --com-som desligam-nos;
  * a linha de estado mostra A OUVIR-TE e a bolinha mostra-o como a ouvir.

Corre com:

    .venv\Scripts\python -m unittest tests.test_janela_de_seguimento -v
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jarvis import app, sinais
from jarvis.app import A_CONVERSA, A_OUVIR, A_OUVIR_TE, ESCUTA_FECHADA, construir_sons
from jarvis.avisos import FALADO, OCUPADO, Aviso
from jarvis.bolinha import ESTADO_DO_PAINEL
from jarvis.config import ConfigEscuta
from jarvis.ouvido import (
    ESCUTA_CONVERSA,
    ESCUTA_RECAP,
    ESCUTA_SEGUIMENTO,
    GATILHO_JANELA,
    GATILHO_TECLA,
    Frase,
    Ouvido,
)
from jarvis.sessoes import Entrega
from tests.test_app import ForjaFalsa, LogFalso, RelogioFalso, config_de_teste, resposta_llm
from tests.test_ouvido import SILENCIO, MotorFalso, TeclaFixa, VadFalso, chunk, chunks_em
from tests.test_pergunta_geral import ArranqueFalso, perguntas_de_teste, saida_json
from tests.test_resposta_ao_recap import DITADO_EN, DITADO_PT, VOZ, Cena

HORAS = "hey jarvis, what time is it"
PERGUNTA_DO_CLAUDE = "I fixed the login test. Should I run the whole suite?"


def cena_com_sons(respostas_llm=None, **kw) -> tuple[Cena, list[str]]:
    sons: list[str] = []
    return Cena(respostas_llm, sons=sons.append, **kw), sons


def seguimentos(cena: Cena) -> list[tuple[float, float, str, bool]]:
    return [abertura for abertura in cena.aberturas if abertura[2] == ESCUTA_SEGUIMENTO]


def falar_ate(cena: Cena, texto: str, *, antes_s: float = 0.5) -> int:
    """Como `Cena.dizer`, mas com o ciclo principal a correr a cada chunk."""
    cena.motor.textos.append(texto)
    cena.passar(SILENCIO, antes_s, verificar=True)
    cena.passar(VOZ, 1.0, verificar=True)
    cena.passar(SILENCIO, 1.0, verificar=True)
    return cena.ouvido.transcrever_pendentes()


# --- Abre depois de qualquer resposta falada --------------------------------------


class TestAbreDepoisDeQualquerResposta(unittest.TestCase):
    def verificar_aberta_no_fim_da_voz(self, cena: Cena, sons: list[str], limite_s: float = 8.0) -> None:
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_SEGUIMENTO)
        instante, limite, _para, a_falar = seguimentos(cena)[-1]
        self.assertFalse(a_falar, "nunca abre com a voz a falar")
        self.assertEqual(instante, cena.fins_da_fala[-1], "abre quando a voz acaba")
        self.assertEqual(limite, limite_s)
        self.assertTrue(cena.jarvis.seguimento.aberta())
        self.assertEqual(cena.jarvis.painel.atual, A_OUVIR_TE)
        self.assertEqual(sons[-1], "abrir")

    def test_depois_das_horas(self) -> None:
        cena, sons = cena_com_sons()
        cena.pedir(HORAS)
        self.assertEqual(cena.escuta_ao_falar, [None], "nada aberto enquanto a voz fala")
        self.verificar_aberta_no_fim_da_voz(cena, sons)
        self.assertEqual(sons, ["abrir"])
        self.assertIn("seguimento | a ouvir sem palavra de ativacao (8 s)", cena.log())

    def test_depois_do_estado_da_forja(self) -> None:
        cena, sons = cena_com_sons([resposta_llm("estado", "atlas")])
        cena.jarvis.forja = ForjaFalsa()
        cena.pedir("hey jarvis, what is the status of atlas")
        self.assertEqual(cena.jarvis.forja.pedidos[0][:2], ("estado", "atlas"))
        self.assertEqual(cena.falados[-1], "Run started.")
        self.verificar_aberta_no_fim_da_voz(cena, sons)

    def test_depois_do_sent(self) -> None:
        cena, sons = cena_com_sons([DITADO_EN])
        cena.com_recap()
        cena.dizer("Yeah.")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertTrue(cena.falados[-1].startswith("Sent"), cena.falados[-1])
        self.verificar_aberta_no_fim_da_voz(cena, sons)
        # A escuta do recap fechou so porque a voz ia falar: nenhum "fechar".
        self.assertEqual(sons, ["abrir", "abrir"])

    def test_depois_da_resposta_a_uma_pergunta_geral_e_nao_do_let_me_check(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        arranque = ArranqueFalso(saida_json("It is 22 degrees and sunny in Porto today."))
        perguntas = perguntas_de_teste(arranque, Path(temporaria.name) / "perguntas", lingua="en")
        cena, sons = cena_com_sons(
            [resposta_llm("pergunta_geral", "", "What is the temperature in Porto?")], perguntas=perguntas
        )
        self.addCleanup(cena.jarvis.fechar)
        cena.pedir("hey jarvis, what is the temperature in Porto today")
        self.assertTrue(cena.jarvis.esperar_pergunta(5.0), "a thread da pergunta nao acabou")
        self.assertEqual(cena.falados[0], "Let me check.")
        self.assertEqual(len(cena.falados), 2)
        self.assertEqual(len(seguimentos(cena)), 1, "so a resposta abre a janela, nunca o 'Let me check.'")
        self.verificar_aberta_no_fim_da_voz(cena, sons)
        self.assertEqual(sons, ["abrir"])

    def test_o_um_momento_nao_abre(self) -> None:
        cena, sons = cena_com_sons()
        cena.jarvis._avisar_demora()
        cena.jarvis._assentar_escuta()
        self.assertEqual(len(cena.falados), 1)
        self.assertEqual(seguimentos(cena), [])
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertEqual(sons, [])

    def test_um_aviso_nao_pedido_nao_abre_e_espera_pela_janela(self) -> None:
        cena, sons = cena_com_sons()
        aviso = Aviso("atlas", "Stop", "Claude in atlas finished.", ("atlas", "s1"), 0.0)
        self.assertEqual(cena.jarvis._entregar_aviso(aviso), FALADO)
        self.assertEqual(seguimentos(cena), [], "um aviso nao abre a janela de seguimento")
        cena.pedir(HORAS)
        self.assertTrue(cena.jarvis.seguimento.aberta())
        falados = len(cena.falados)
        self.assertEqual(cena.jarvis._entregar_aviso(aviso), OCUPADO, "espera enquanto a janela esta aberta")
        self.assertEqual(len(cena.falados), falados)
        self.assertEqual(sons, ["abrir"])

    def test_sem_vad_nao_abre_e_fica_a_tecla(self) -> None:
        cena, sons = cena_com_sons(vad=False)
        cena.pedir(HORAS)
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertEqual(cena.jarvis.painel.atual, A_OUVIR)
        self.assertEqual(sons, [])


# --- Prazo configuravel ----------------------------------------------------------


class TestPrazo(unittest.TestCase):
    def test_o_prazo_vem_de_seguimento_s_e_fecha_com_som(self) -> None:
        cena, sons = cena_com_sons(escuta=ConfigEscuta(seguimento_s=5.0))
        cena.pedir(HORAS)
        self.assertEqual(seguimentos(cena)[-1][1], 5.0)
        self.assertIn("estado | A OUVIR-TE | sem palavra de ativacao, 5 s", cena.log())
        cena.passar(SILENCIO, 4.5, verificar=True)
        self.assertTrue(cena.jarvis.seguimento.aberta())
        cena.passar(SILENCIO, 1.0, verificar=True)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(cena.jarvis.painel.atual, A_OUVIR)
        self.assertIn(f"estado | A OUVIR | {ESCUTA_FECHADA}", cena.log())
        self.assertEqual(sons, ["abrir", "fechar"])
        self.assertEqual(len(cena.falados), 1, "fechar nao diz nada")
        # Fechada, so a palavra de ativacao ou a tecla.
        cena.motor.textos.append("what time is it")
        cena.passar(VOZ, 1.0)
        cena.passar(SILENCIO, 1.0)
        self.assertEqual(cena.ouvido.transcrever_pendentes(), 0)
        cena.motor.textos.clear()


# --- Prioridade: nunca duas janelas -------------------------------------------------


class TestPrioridade(unittest.TestCase):
    def test_recap_pendente_fica_com_a_escuta_do_recap(self) -> None:
        cena, sons = cena_com_sons([DITADO_EN])
        cena.com_recap()
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        self.assertEqual(cena.aberturas[-1][1:3], (30.0, ESCUTA_RECAP))
        self.assertEqual(seguimentos(cena), [])
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertEqual(sons, ["abrir"])

    def test_recap_dito_na_janela_de_seguimento_troca_para_a_do_recap(self) -> None:
        cena, sons = cena_com_sons([DITADO_EN])
        cena.pedir(HORAS)
        self.assertEqual(cena.dizer("tell atlas to fix the login test"), 1)
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.canal.recebidos, [], "nada enviado sem o sim")
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertEqual(sons, ["abrir", "abrir"])
        cena.dizer("Yeah.")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_pergunta_do_claude_fica_com_a_janela_de_conversa(self) -> None:
        cena, sons = cena_com_sons([DITADO_EN])
        cena.com_recap()
        cena.dizer("Yeah.")
        self.assertTrue(cena.jarvis.seguimento.aberta())
        cena.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto=PERGUNTA_DO_CLAUDE))
        self.assertIsNone(cena.escuta_ao_falar[-1], "a janela de seguimento fechou antes de a voz falar")
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_CONVERSA)
        self.assertTrue(cena.jarvis.janela.aberta())
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertEqual(cena.jarvis.painel.atual, A_CONVERSA)
        self.assertEqual(sons, ["abrir", "abrir", "abrir"])
        # A conversa acaba sem resposta: fecha com som e nao abre o seguimento.
        cena.passar(SILENCIO, 8.5, verificar=True)
        self.assertFalse(cena.jarvis.janela.aberta())
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(sons[-1], "fechar")

    def test_sair_da_conversa_fecha_com_som_sem_seguimento(self) -> None:
        cena, sons = cena_com_sons()
        cena.jarvis._abrir_conversa("atlas")
        self.assertEqual(sons, ["abrir"])
        cena.dizer("exit the conversation")
        self.assertFalse(cena.jarvis.janela.aberta())
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(sons, ["abrir", "fechar"])

    def test_a_dormir_nao_abre(self) -> None:
        cena, sons = cena_com_sons(lingua="pt")
        cena.pedir("boas jarvis, dorme")
        self.assertTrue(cena.jarvis.estado.adormecido)
        self.assertTrue(cena.falados[-1].startswith("Vou dormir"), cena.falados)
        self.assertEqual(seguimentos(cena), [], "nem logo depois de 'Vou dormir'")
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(sons, [])

    def test_dorme_dito_na_janela_fecha_com_som(self) -> None:
        cena, sons = cena_com_sons(lingua="pt")
        cena.pedir("boas jarvis, que horas são")
        self.assertTrue(cena.jarvis.seguimento.aberta())
        cena.dizer("dorme")
        self.assertTrue(cena.jarvis.estado.adormecido)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(sons, ["abrir", "fechar"])

    def test_cala_te_na_janela_fecha_com_som_e_nao_reabre(self) -> None:
        cena, sons = cena_com_sons(lingua="pt")
        cena.pedir("boas jarvis, que horas são")
        cena.dizer("cala-te")
        self.assertTrue(cena.jarvis.estado.mudo)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(sons, ["abrir", "fechar"])

    def test_clique_na_bolinha_fecha_com_som(self) -> None:
        cena, sons = cena_com_sons()
        cena.pedir(HORAS)
        cena.jarvis.calar_pela_bolinha()
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(cena.jarvis.painel.atual, A_OUVIR)
        self.assertEqual(sons, ["abrir", "fechar"])

    def test_sem_voz_nao_abre(self) -> None:
        cena, sons = cena_com_sons()
        cena.jarvis.com_voz = False
        cena.pedir(HORAS)
        self.assertEqual(cena.falados, [], "so no ecra")
        self.assertEqual(seguimentos(cena), [])
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertEqual(sons, [])


# --- A frase dita na janela e um pedido normal ----------------------------------------


class TestFraseNaJanela(unittest.TestCase):
    def test_e_interpretada_sem_palavra_de_ativacao_e_a_resposta_reabre(self) -> None:
        cena, sons = cena_com_sons()
        cena.pedir(HORAS)
        self.assertEqual(cena.dizer("what time is it"), 1)
        self.assertEqual(len(cena.m.locais), 2, "o mesmo pedido, sem 'hey jarvis'")
        self.assertIn("escuta de seguimento (sem palavra de ativacao)", cena.log())
        self.assertIsNone(cena.escuta_ao_falar[-1], "a janela fechou antes de a voz falar")
        self.assertEqual(len(seguimentos(cena)), 2, "a resposta abre uma janela nova")
        self.assertEqual(seguimentos(cena)[-1][0], cena.fins_da_fala[-1])
        self.assertEqual(seguimentos(cena)[-1][1], 8.0, "prazo inteiro de novo")
        self.assertEqual(sons, ["abrir", "abrir"])

    def test_um_pedido_com_efeito_so_segue_com_o_sim(self) -> None:
        cena, _sons = cena_com_sons([DITADO_EN])
        cena.pedir(HORAS)
        cena.dizer("tell atlas to fix the login test")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.canal.recebidos, [])
        self.assertTrue(cena.falados[-1].endswith("Send it?"))

    def test_nunca_usa_o_atalho_da_conversa(self) -> None:
        # Na janela de conversa uma resposta curta vai logo; aqui passa pelo
        # interprete e o que tem efeito espera pelo recap.
        cena, _sons = cena_com_sons([resposta_llm("conversa", "atlas", "Keep the newer file.")])
        cena.pedir(HORAS)
        cena.dizer("Keep the newer file.")
        self.assertEqual(len(cena.m.llm.pedidos), 1, "foi ao interprete")
        self.assertEqual(cena.canal.recebidos, [], "nada enviado sem recap e sim")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)

    def test_compra_ou_venda_e_recusada(self) -> None:
        cena, _sons = cena_com_sons(lingua="pt")
        cena.pedir("boas jarvis, que horas são")
        cena.dizer("compra cem euros de bitcoin")
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.canal.recebidos, [])
        self.assertEqual(cena.m.llm.pedidos, [])
        self.assertEqual(len(cena.m.locais), 1)

    def test_a_janela_e_gasta_por_um_pedido_sem_resposta_falada(self) -> None:
        # Um pedido tomado gasta a janela; sem resposta falada (voz desligada
        # entretanto) nao abre outra e a escuta fecha com som.
        cena, sons = cena_com_sons()
        cena.pedir(HORAS)
        cena.jarvis.com_voz = False
        cena.dizer("what time is it")
        self.assertEqual(len(cena.m.locais), 2)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertEqual(sons, ["abrir", "fechar"])


# --- Ruido nao gasta nem prolonga ------------------------------------------------------


class TestRuidoNaJanela(unittest.TestCase):
    def test_silencio_hesitacoes_e_cortesia_sao_ignorados_ate_ao_prazo_original(self) -> None:
        for texto in ("", "...", "Uh, um.", "Thank you.", "Okay, thanks."):
            with self.subTest(texto=texto):
                cena, sons = cena_com_sons()
                cena.pedir(HORAS)
                prazo = cena.jarvis.seguimento.atual.prazo
                falados = list(cena.falados)
                cena.passar(SILENCIO, 3.0, verificar=True)
                falar_ate(cena, texto)
                cena.passar(SILENCIO, 0.1, verificar=True)
                self.assertEqual(cena.falados, falados, "nada dito")
                self.assertEqual(cena.m.llm.pedidos, [], "nada interpretado")
                self.assertEqual(cena.m.locais, [("horas", None, "horas")])
                self.assertEqual(cena.canal.recebidos, [])
                self.assertEqual(cena.jarvis.seguimento.atual.prazo, prazo, "nem gasta nem prolonga")
                self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_SEGUIMENTO, "continua a ouvir")
                self.assertEqual(len(seguimentos(cena)), 2, "reaberta uma vez depois do ruido")
                instante, limite, _para, a_falar = seguimentos(cena)[-1]
                self.assertFalse(a_falar)
                self.assertAlmostEqual(instante + limite, prazo, delta=0.05, msg="reaberta so ate ao prazo original")
                self.assertEqual(sons, ["abrir"], "reabrir depois de ruido nao toca nada")
                cena.passar(SILENCIO, prazo - cena.relogio() - 0.2, verificar=True)
                self.assertTrue(cena.jarvis.seguimento.aberta())
                cena.passar(SILENCIO, 0.5, verificar=True)
                self.assertFalse(cena.jarvis.seguimento.aberta(), "fecha no prazo original")
                self.assertIsNone(cena.ouvido.escuta_aberta)
                self.assertEqual(sons, ["abrir", "fechar"])

    def test_um_pedido_depois_de_ruido_ainda_conta(self) -> None:
        cena, _sons = cena_com_sons()
        cena.pedir(HORAS)
        falar_ate(cena, "Uh.")
        self.assertEqual(falar_ate(cena, "what time is it"), 1)
        self.assertEqual(len(cena.m.locais), 2)


# --- Fala nova, tecla e palavra de ativacao -----------------------------------------------


class TestFalaTeclaEPalavra(unittest.TestCase):
    def test_uma_resposta_do_claude_fecha_e_reabre_a_janela(self) -> None:
        cena, sons = cena_com_sons()
        cena.pedir(HORAS)
        cena.passar(SILENCIO, 3.0, verificar=True)
        cena.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto="Done, all tests pass."))
        self.assertIsNone(cena.escuta_ao_falar[-1], "fechada enquanto a voz fala")
        self.assertEqual(len(seguimentos(cena)), 2)
        self.assertEqual(seguimentos(cena)[-1][0], cena.fins_da_fala[-1], "reabre no fim da fala nova")
        self.assertEqual(cena.jarvis.seguimento.atual.prazo, cena.fins_da_fala[-1] + 8.0)
        self.assertEqual(sons, ["abrir", "abrir"], "sem 'fechar' quando so fecha para falar")

    def test_a_tecla_funciona_dentro_da_janela(self) -> None:
        cena, _sons = cena_com_sons()
        cena.pedir(HORAS)
        cena.motor.textos.append("what time is it")
        cena.passar(VOZ, 1.0, premida=True)
        cena.passar(SILENCIO, 0.1)
        self.assertEqual(cena.ouvido.transcrever_pendentes(), 1)
        self.assertEqual(len(cena.m.locais), 2)
        self.assertIn("tecla de falar", cena.log())

    def test_hey_jarvis_funciona_dentro_e_fora_da_janela(self) -> None:
        cena, _sons = cena_com_sons()
        cena.pedir(HORAS)
        cena.pedir(HORAS)
        self.assertEqual(len(cena.m.locais), 2)
        cena.passar(SILENCIO, 9.0, verificar=True)
        self.assertFalse(cena.jarvis.seguimento.aberta())
        cena.pedir(HORAS)
        self.assertEqual(len(cena.m.locais), 3)
        self.assertTrue(cena.jarvis.seguimento.aberta())

    def test_cortesia_so_e_ruido_dentro_da_janela_de_seguimento(self) -> None:
        cena, _sons = cena_com_sons()
        agora = cena.relogio()
        frase = Frase("Thank you.", GATILHO_JANELA, "en", "falso", 1.0, agora, agora + 1, agora + 1, 1.0)
        self.assertFalse(cena.jarvis._ruido_no_seguimento(frase), "sem janela aberta")
        cena.pedir(HORAS)
        dentro = replace(frase, inicio_da_escuta=cena.relogio() + 1)
        self.assertTrue(cena.jarvis._ruido_no_seguimento(dentro))
        self.assertFalse(cena.jarvis._ruido_no_seguimento(replace(dentro, gatilho=GATILHO_TECLA)))


# --- Sons -----------------------------------------------------------------------------


class TestSons(unittest.TestCase):
    def test_sem_callback_nao_ha_som_nenhum(self) -> None:
        with mock.patch.object(sinais, "tocar") as tocar, mock.patch.object(sinais, "_tocar_no_sistema") as sistema:
            cena = Cena()
            cena.pedir(HORAS)
            cena.passar(SILENCIO, 9.0, verificar=True)
            self.assertIsNone(cena.jarvis._sons)
        tocar.assert_not_called()
        sistema.assert_not_called()

    def test_um_som_que_falha_nunca_para_o_jarvis(self) -> None:
        def falha(_tipo: str) -> None:
            raise OSError("sem dispositivo")

        cena = Cena(sons=falha)
        cena.pedir(HORAS)
        self.assertTrue(cena.jarvis.seguimento.aberta())
        self.assertIn("som | 'abrir' falhou", cena.log())

    def test_construir_sons_segue_a_config_e_o_modo(self) -> None:
        ligado = config_de_teste("en", escuta=ConfigEscuta(sons=True, volume=0.1))
        desligado = config_de_teste("en", escuta=ConfigEscuta(sons=False))
        self.assertIsNone(construir_sons(desligado, modo_ficheiro=False, com_som=False))
        self.assertIsNone(construir_sons(desligado, modo_ficheiro=True, com_som=True))
        self.assertIsNone(construir_sons(ligado, modo_ficheiro=True, com_som=False), "--wav sem --com-som")
        for modo_ficheiro, com_som in ((False, False), (False, True), (True, True)):
            with self.subTest(modo_ficheiro=modo_ficheiro, com_som=com_som):
                sons = construir_sons(ligado, modo_ficheiro=modo_ficheiro, com_som=com_som)
                self.assertIsNotNone(sons)
                with mock.patch.object(sinais, "tocar") as tocar:
                    sons("abrir")
                tocar.assert_called_once_with("abrir", volume=0.1, com_som=True)

    def test_o_arranque_liga_os_sons_ao_jarvis(self) -> None:
        class Parar(Exception):
            pass

        casos = (
            ([], True, True),  # microfone: com sons
            (["--com-som"], True, True),
            ([], False, False),  # [escuta] sons = false
            (["--wav", "a.wav"], True, False),  # --wav sem --com-som
            (["--wav", "a.wav", "--com-som"], True, True),
        )
        for extra, ligados, espera_sons in casos:
            with self.subTest(argumentos=extra, sons_na_config=ligados):
                config = config_de_teste("en", escuta=ConfigEscuta(sons=ligados, volume=0.1))
                vistos: list[dict] = []

                def jarvis_que_para(*_args, **kw):
                    vistos.append(kw)
                    raise Parar

                with (
                    mock.patch.object(app, "LogDaSessao", LogFalso),
                    mock.patch.object(app, "carregar_config_tolerante", lambda _caminho, _log: config),
                    mock.patch.object(app, "Interprete"),
                    mock.patch.object(app, "ForjaPorVoz"),
                    mock.patch.object(app, "PerguntasGerais"),
                    mock.patch.object(app.voz, "definir_lingua_da_voz"),
                    mock.patch.object(app.voz, "definir_voz_inglesa"),
                    mock.patch.object(app, "Jarvis", jarvis_que_para),
                ):
                    args = app.construir_parser().parse_args(extra)
                    with self.assertRaises(Parar):
                        app._arrancar_e_correr(args, 0.0, Path("config-de-teste.toml"))
                sons = vistos[0]["sons"]
                self.assertEqual(sons is not None, espera_sons)
                if sons is not None:
                    with mock.patch.object(sinais, "tocar") as tocar:
                        sons("fechar")
                    tocar.assert_called_once_with("fechar", volume=0.1, com_som=True)

    def test_o_bip_do_ouvido_nao_toca_nas_escutas_sem_palavra_de_ativacao(self) -> None:
        bips: list[str] = []
        relogio = RelogioFalso()
        ouvido = Ouvido(
            None, MotorFalso(texto="yes"), lambda _frase: None, tecla=TeclaFixa(), vad=VadFalso(),
            lingua="en", escrever=LogFalso().linha, relogio=relogio, com_som=True, tocar=bips.append,
        )

        def passar(valor: int, segundos: float, premida: bool = False) -> None:
            for _ in range(chunks_em(segundos)):
                relogio.avancar(0.03)
                ouvido.processar(chunk(valor), premida)

        for para in (ESCUTA_SEGUIMENTO, ESCUTA_RECAP, ESCUTA_CONVERSA):
            ouvido.abrir_escuta(8.0, para=para)
            passar(SILENCIO, 0.6)
            passar(VOZ, 1.0)
            passar(SILENCIO, 1.0)
        self.assertEqual(bips, [], "os sons destas escutas sao do jarvis")
        passar(VOZ, 1.0, premida=True)
        passar(SILENCIO, 0.2)
        self.assertTrue(bips, "a tecla continua com o bip do --com-som")


# --- Linha de estado e bolinha -----------------------------------------------------------


class TestEstadoEBolinha(unittest.TestCase):
    def test_a_bolinha_mostra_a_ouvir_te_como_a_ouvir(self) -> None:
        self.assertEqual(ESTADO_DO_PAINEL[A_OUVIR_TE], "ouvir")
        self.assertEqual(ESTADO_DO_PAINEL[A_OUVIR], "repouso")

    def test_a_bolinha_recebe_a_ouvir_te_e_depois_a_ouvir(self) -> None:
        cena = Cena()
        estados: list[str] = []
        cena.jarvis.painel.ao_mudar = estados.append
        cena.pedir(HORAS)
        self.assertEqual(estados[-1], A_OUVIR_TE)
        cena.passar(SILENCIO, 9.0, verificar=True)
        self.assertEqual(estados[-1], A_OUVIR)
        self.assertEqual([ESTADO_DO_PAINEL.get(e) for e in estados[-2:]], ["ouvir", "repouso"])

    def test_linhas_de_estado(self) -> None:
        cena = Cena()
        cena.pedir(HORAS)
        cena.passar(SILENCIO, 9.0, verificar=True)
        log = cena.log()
        self.assertIn("estado | A OUVIR-TE | sem palavra de ativacao, 8 s", log)
        self.assertIn(f"estado | A OUVIR | {ESCUTA_FECHADA}", log)
        self.assertIn("hey jarvis", ESCUTA_FECHADA)


if __name__ == "__main__":
    unittest.main()
