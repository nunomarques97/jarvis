r"""Testes da resposta ao recap sem palavra de ativacao (jarvis/app.py + jarvis/ouvido.py).

Depois de o recap ser dito, o jarvis abre no ouvido uma escuta sem palavra de
ativacao ate ao fim do prazo da confirmacao: a frase seguinte e a resposta.
Estes testes ligam um Jarvis com pecas falsas (sem LLM real, sem canal real)
a um Ouvido verdadeiro sem microfone: o audio sao chunks feitos a mao, o VAD
e falso e o motor de transcricao devolve textos feitos. A voz e uma funcao
que so regista o texto e faz o relogio falso andar o tempo da leitura, sem
tocar som. O que protegem:

  * a escuta so abre depois de a voz acabar, nunca enquanto ela fala;
  * o prazo conta desde o fim da voz e usa `[interprete] confirmacao_s`;
  * "Yeah." envia uma vez, "Uh cancel." cancela, "no, change X to Y" corrige
    e a escuta volta a abrir depois do recap novo;
  * a tecla de falar e "hey jarvis" continuam a responder dentro da escuta;
  * ruido ou texto vazio nao envia nada nem gasta a escuta;
  * silencio ate ao prazo cancela sem enviar, mas nunca a meio de uma frase;
  * uma resposta que comeca no fim da escuta chega inteira ao motor (o teto
    da frase conta desde o inicio da fala) e o fim da voz do jarvis que ainda
    soa quando a escuta abre nunca entra na resposta;
  * sem recap pendente nada e ouvido sem palavra de ativacao nem tecla.

Corre com:

    .venv\Scripts\python -m unittest tests.test_resposta_ao_recap -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from jarvis import app
from jarvis.app import A_ESPERA_A_OUVIR, A_OUVIR
from jarvis.ouvido import BYTES_POR_CHUNK, DURACAO_DO_CHUNK_S, ESCUTA_RECAP, GATILHO_JANELA, GATILHO_TECLA, Ouvido
from jarvis.voz import ResultadoFala
from tests.test_app import CanalFalso, LogFalso, Montagem, RelogioFalso, resposta_llm
from tests.test_ouvido import SILENCIO, MotorFalso, TeclaFixa, VadFalso, chunk, chunks_em

VOZ = 0x33
#: Quanto demora a voz falsa a ler cada frase (o recap real demora 4-6 s).
DURACAO_DA_FALA_S = 5.0

DITADO_EN = resposta_llm("ditar_prompt", "atlas", "Fix the login test.")
CORRIGIDO_EN = resposta_llm("ditar_prompt", "atlas", "Fix the login documentation.")
DITADO_PT = resposta_llm("ditar_prompt", "atlas", "Corrige o teste do login.")
EXPIRADO_EN = "No answer, so I cancelled. Nothing was sent."


class MotorDeTextos(MotorFalso):
    """Devolve os textos dados, um por frase; `durante` corre a meio da transcricao."""

    def __init__(self) -> None:
        super().__init__()
        self.textos: list[str] = []
        self.durante = None

    def _inferir(self, modelo, pcm16, lingua):
        if not any(pcm16):
            self.aquecimentos += 1
            return "", lingua, False
        self.recebidos.append(pcm16)
        if self.durante is not None:
            self.durante()
        return (self.textos.pop(0) if self.textos else ""), lingua, False


class Cena:
    """Um Jarvis com pecas falsas ligado a um Ouvido verdadeiro, sem microfone nem som."""

    def __init__(self, respostas_llm=None, *, lingua: str = "en", vad: bool = True, **ajustes) -> None:
        self.m = Montagem(respostas_llm, lingua=lingua, canal=CanalFalso(), **ajustes)
        self.jarvis = self.m.jarvis
        self.relogio = self.m.relogio
        self.canal = self.m.canal
        self.falados = self.m.falados
        self.motor = MotorDeTextos()
        self.tecla = TeclaFixa()
        self.linhas_do_ouvido = LogFalso()
        self.ouvido = Ouvido(
            None,
            self.motor,
            self.jarvis.ao_ouvir,
            tecla=self.tecla,
            detetor=None,
            vad=VadFalso() if vad else None,
            lingua=lingua,
            escrever=self.linhas_do_ouvido.linha,
            relogio=self.relogio,
        )
        self.jarvis.ouvido = self.ouvido
        #: O que o ouvido tinha aberto quando cada frase comecou a ser dita.
        self.escuta_ao_falar: list[str | None] = []
        #: (instante, limite_s, para, a voz estava a falar) de cada abrir_escuta.
        self.aberturas: list[tuple[float, float, str, bool]] = []
        self.fins_da_fala: list[float] = []
        self.a_falar = False

        abrir = self.ouvido.abrir_escuta

        def abrir_e_registar(limite_s: float, *, para: str = "conversa") -> bool:
            self.aberturas.append((self.relogio(), limite_s, para, self.a_falar))
            return abrir(limite_s, para=para)

        self.ouvido.abrir_escuta = abrir_e_registar

        def falar(texto: str) -> ResultadoFala:
            self.a_falar = True
            try:
                self.escuta_ao_falar.append(self.ouvido.escuta_aberta)
                self.falados.append(texto)
                inicio = self.relogio()
                self.relogio.avancar(DURACAO_DA_FALA_S)
                return ResultadoFala(falou=True, primeiro_audio=inicio + 0.1)
            finally:
                self.fins_da_fala.append(self.relogio())
                self.a_falar = False

        self.jarvis._falar = falar

    # -- entrada

    def pedir(self, texto: str) -> None:
        """Uma frase dita com a palavra de ativacao, entregue ja transcrita."""
        self.m.ouvir(texto, gatilho="ativacao")

    def passar(self, valor: int, segundos: float, *, premida: bool = False, verificar: bool = False) -> None:
        for _ in range(chunks_em(segundos)):
            self.relogio.avancar(DURACAO_DO_CHUNK_S)
            self.ouvido.processar(chunk(valor), premida)
            if verificar:
                self.jarvis.verificar_tempo()

    def dizer(self, texto: str, *, antes_s: float = 0.5) -> int:
        """Fala `texto` para o microfone (sem palavra de ativacao) e transcreve."""
        self.motor.textos.append(texto)
        self.passar(SILENCIO, antes_s)
        self.passar(VOZ, 1.0)
        self.passar(SILENCIO, 1.0)
        return self.ouvido.transcrever_pendentes()

    def restante_da_escuta(self) -> float | None:
        janela_ate = self.ouvido._janela_ate
        return None if janela_ate is None else janela_ate - self.relogio()

    def com_recap(self, texto: str = "hey jarvis, tell atlas to fix the login test") -> None:
        self.pedir(texto)
        assert self.jarvis.confirmacao.a_espera, self.m.log.texto()

    def log(self) -> str:
        return self.m.log.texto() + "\n" + self.linhas_do_ouvido.texto()


def _sem_escuta_nada_e_ouvido(teste: unittest.TestCase, cena: Cena) -> None:
    teste.assertIsNone(cena.ouvido.escuta_aberta)
    teste.assertFalse(cena.jarvis._a_ouvir_o_recap)
    cena.motor.textos.append("yes")
    cena.passar(VOZ, 1.0)
    cena.passar(SILENCIO, 1.0)
    teste.assertEqual(cena.ouvido.transcrever_pendentes(), 0, "sem escuta nem ativacao, nada e ouvido")
    cena.motor.textos.clear()


# --- A escuta abre depois da voz, com o prazo que falta --------------------------


class TestEscutaDepoisDoRecap(unittest.TestCase):
    def test_abre_so_depois_de_a_voz_acabar_com_o_prazo_inteiro(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        self.assertTrue(cena.falados[-1].endswith("Send it?"))
        self.assertEqual(cena.escuta_ao_falar, [None], "nada aberto enquanto a voz le o recap")
        self.assertTrue(cena.aberturas)
        self.assertFalse(any(a_falar for *_resto, a_falar in cena.aberturas), "nunca abre com a voz a falar")
        instante, limite_s, para, _ = cena.aberturas[-1]
        self.assertEqual(para, ESCUTA_RECAP)
        self.assertEqual(instante, cena.fins_da_fala[-1], "abre no fim da voz")
        self.assertEqual(limite_s, 30.0, "prazo por omissao, contado desde o fim da voz")
        self.assertEqual(cena.jarvis.confirmacao.prazo_restante(), 30.0)
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        self.assertEqual(cena.jarvis.painel.atual, A_ESPERA_A_OUVIR)
        self.assertIn("confirmacao | a ouvir a resposta ao recap sem palavra de ativacao (30 s)", cena.log())

    def test_o_prazo_vem_de_confirmacao_s(self) -> None:
        cena = Cena([DITADO_EN], confirmacao_s=12)
        cena.com_recap()
        self.assertEqual(cena.aberturas[-1][1], 12.0)
        cena.passar(SILENCIO, 11.5, verificar=True)
        self.assertTrue(cena.jarvis.confirmacao.a_espera, "ainda dentro dos 12 s desde o fim da voz")
        cena.passar(SILENCIO, 1.0, verificar=True)
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.falados[-1], EXPIRADO_EN)

    def test_o_sinal_do_ouvido_diz_que_e_a_resposta_ao_recap(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.passar(SILENCIO, 0.1)
        self.assertEqual(cena.ouvido.estado, "ativado")
        texto = cena.linhas_do_ouvido.texto()
        self.assertIn("resposta ao recap (30 s, sem palavra de ativacao)", texto)
        self.assertNotIn("janela de conversa", texto)

    def test_sem_vad_so_a_tecla_e_o_log_diz_isso(self) -> None:
        cena = Cena([DITADO_EN], vad=False)
        cena.com_recap()
        self.assertIsNone(cena.ouvido.escuta_aberta)
        self.assertIn("resposta ao recap so com a tecla de falar (30 s)", cena.log())
        cena.motor.textos.append("yes")
        cena.passar(VOZ, 1.0)
        self.assertEqual(cena.ouvido.transcrever_pendentes(), 0)
        cena.passar(VOZ, 1.0, premida=True)
        cena.passar(SILENCIO, 0.1)
        cena.ouvido.transcrever_pendentes()
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])


# --- A frase seguinte e a resposta -----------------------------------------------


class TestRespostaSemPalavraDeAtivacao(unittest.TestCase):
    def test_yeah_envia_uma_vez(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        self.assertEqual(cena.dizer("Yeah."), 1)
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertIn("resposta ao recap (escuta sem palavra de ativacao)", cena.log())
        self.assertIn("resposta ao recap pendente", cena.log())
        _sem_escuta_nada_e_ouvido(self, cena)
        self.assertEqual(len(cena.canal.recebidos), 1)
        self.assertEqual(cena.jarvis.painel.atual, A_OUVIR)

    def test_uh_cancel_cancela_sem_enviar(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.dizer("Uh cancel.")
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.canal.recebidos, [])
        self.assertEqual(cena.falados[-1], "Cancelled, nothing was sent.")
        _sem_escuta_nada_e_ouvido(self, cena)

    def test_correcao_faz_recap_novo_e_a_escuta_volta_a_abrir_depois_dele(self) -> None:
        cena = Cena([DITADO_EN, CORRIGIDO_EN])
        cena.com_recap()
        aberturas = len(cena.aberturas)
        cena.dizer("no, change test to documentation")
        self.assertEqual(len(cena.m.llm.pedidos), 2, "a correcao passou pelo interprete")
        self.assertEqual(cena.jarvis.confirmacao.recap.pedido.prompt, "Fix the login documentation.")
        self.assertIn("documentation", cena.falados[-1])
        self.assertEqual(cena.canal.recebidos, [])
        self.assertEqual(cena.escuta_ao_falar[-1], None, "fechada enquanto o recap novo e lido")
        self.assertEqual(len(cena.aberturas), aberturas + 1)
        instante, limite_s, para, a_falar = cena.aberturas[-1]
        self.assertEqual((instante, limite_s, para, a_falar), (cena.fins_da_fala[-1], 30.0, ESCUTA_RECAP, False))
        cena.dizer("Yes.")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login documentation.")])

    def test_resposta_nao_percebida_volta_a_abrir_depois_da_pergunta(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.dizer("purple elephants")
        self.assertEqual(cena.falados[-1], "Say yes to send, or abort.")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.aberturas[-1][0], cena.fins_da_fala[-1])
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        cena.dizer("yep")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_horas_durante_o_recap_mantem_a_escuta(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.dizer("what time is it")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        self.assertEqual(cena.aberturas[-1][0], cena.fins_da_fala[-1])
        cena.dizer("sure")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_recap_da_janela_de_conversa_tambem_ouve_a_resposta(self) -> None:
        cena = Cena()
        cena.jarvis._abrir_conversa("atlas")
        cena.dizer("yes, run the whole suite and then the linter")
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.falados[-1], "Reply to Claude in atlas: yes, run the whole suite and then the linter. Send it?")
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        self.assertEqual(cena.aberturas[-1][1:], (30.0, ESCUTA_RECAP, False))
        cena.dizer("go ahead")
        self.assertEqual(cena.canal.recebidos, [("atlas", "yes, run the whole suite and then the linter")])

    def test_resposta_do_claude_durante_o_recap_nao_e_ouvida_como_resposta(self) -> None:
        from jarvis.sessoes import Entrega

        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.passar(SILENCIO, 0.5)
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        cena.jarvis._ao_responder("orbita", Entrega(projeto="orbita", caminho="canal", texto="All tests pass."))
        self.assertIsNone(cena.escuta_ao_falar[-1], "a escuta fecha antes de a voz falar")
        self.assertEqual(cena.aberturas[-1][0], cena.fins_da_fala[-1], "e volta a abrir no fim da voz")
        self.assertFalse(cena.aberturas[-1][3])
        cena.dizer("yes")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_em_portugues(self) -> None:
        cena = Cena([DITADO_PT], lingua="pt")
        cena.pedir("boas jarvis, no atlas corrige o teste do login")
        cena.dizer("Envia.")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Corrige o teste do login.")])

    def test_nada_parecido_com_sim_envia(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.dizer("yes but change tests to docs")
        self.assertEqual(cena.canal.recebidos, [])


class TestTeclaEPalavraDentroDaEscuta(unittest.TestCase):
    def test_hey_jarvis_yes_na_escuta_confirma(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.dizer("Hey Jarvis, yes.")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertIn("palavra de ativacao retirada do inicio do texto: 'Hey Jarvis'", cena.log())

    def test_tecla_de_falar_na_escuta_confirma(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.passar(SILENCIO, 0.5)
        self.assertEqual(cena.ouvido.estado, "ativado")
        cena.motor.textos.append("yes")
        cena.passar(VOZ, 1.0, premida=True)
        cena.passar(SILENCIO, 0.1)
        self.assertEqual(cena.ouvido.transcrever_pendentes(), 1)
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertEqual(cena.ouvido.descartadas, 0, "premir a tecla na escuta vazia nao perde nada")
        self.assertIn("tecla de falar", cena.log())


# --- Ruido nao gasta a escuta ----------------------------------------------------


class TestRuidoNaoGastaAEscuta(unittest.TestCase):
    def test_transcricao_vazia_descartada_reabre_no_ciclo_do_prazo(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        falados = list(cena.falados)
        cena.dizer("")  # o motor nao percebeu nada: o ouvido descarta, ao_ouvir nunca corre
        self.assertIn("nada transcrito nesta frase", cena.log())
        self.assertIsNone(cena.ouvido.escuta_aberta)
        cena.jarvis.verificar_tempo()
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP, "o ciclo do prazo volta a abrir a escuta")
        self.assertIn("confirmacao | de novo a ouvir a resposta ao recap", cena.log())
        self.assertAlmostEqual(cena.restante_da_escuta(), cena.jarvis.confirmacao.prazo_restante())
        self.assertEqual(cena.falados, falados, "ruido nao diz nada")
        cena.dizer("yes")
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_so_hesitacao_ou_pontuacao_nao_conta(self) -> None:
        for ruido in ("Uh.", "...", "hmm, uh"):
            with self.subTest(ruido=ruido):
                cena = Cena([DITADO_EN])
                cena.com_recap()
                falados = list(cena.falados)
                prazo = cena.jarvis.confirmacao.prazo_restante()
                cena.dizer(ruido)
                self.assertTrue(cena.jarvis.confirmacao.a_espera)
                self.assertEqual(cena.falados, falados)
                self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP, "reaberta logo, sem esperar pelo ciclo")
                self.assertLess(cena.jarvis.confirmacao.prazo_restante(), prazo, "o prazo nao recomeca")
                cena.dizer("yup")
                self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_escuta_que_acabou_sem_fala_reabre_ate_ao_prazo(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        # A escuta do ouvido acaba sozinha se ninguem falar; aqui fecha-se para simular isso a meio.
        cena.passar(SILENCIO, 3.0, verificar=True)
        cena.ouvido.fechar_escuta()
        cena.passar(SILENCIO, 0.1)
        self.assertIsNone(cena.ouvido.escuta_aberta)
        cena.passar(SILENCIO, 0.1, verificar=True)
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)


# --- Captura da resposta: o teto da frase e o fim da voz do jarvis --------------

ECO = 0x44
FRASE_LONGA = "Yes, but change tests to docs."


class MotorQueContaAVoz(MotorDeTextos):
    """Devolve a frase inteira so se recebeu a fala toda; uma frase cortada fica "Yes."."""

    def __init__(self, fala_s: float) -> None:
        super().__init__()
        self.bytes_de_fala = chunks_em(fala_s) * BYTES_POR_CHUNK

    def _inferir(self, modelo, pcm16, lingua):
        if not any(pcm16):
            self.aquecimentos += 1
            return "", lingua, False
        self.recebidos.append(pcm16)
        inteira = pcm16.count(bytes([VOZ])) >= self.bytes_de_fala
        return (FRASE_LONGA if inteira else "Yes."), lingua, False


def _ouvido_com_escuta(limite_s: float, para: str = ESCUTA_RECAP):
    frases, linhas = [], LogFalso()
    relogio = RelogioFalso()
    ouvido = Ouvido(
        None, MotorFalso(texto="yes"), frases.append, tecla=TeclaFixa(), vad=VadFalso(),
        lingua="en", escrever=linhas.linha, relogio=relogio,
    )
    aberta_em = relogio()
    ouvido.abrir_escuta(limite_s, para=para)

    def passar(valor: int, segundos: float) -> None:
        for _ in range(chunks_em(segundos)):
            relogio.avancar(DURACAO_DO_CHUNK_S)
            ouvido.processar(chunk(valor), False)

    return ouvido, frases, linhas, passar, aberta_em


class TestCapturaDaResposta(unittest.TestCase):
    def test_resposta_no_fim_da_escuta_de_30_s_nao_e_cortada(self) -> None:
        ouvido, frases, linhas, passar, aberta_em = _ouvido_com_escuta(30.0)
        passar(SILENCIO, 29.5)
        passar(VOZ, 2.0)
        passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 1)
        self.assertNotIn("frase no maximo", linhas.texto())
        pcm16 = ouvido.motor.recebidos[0]
        self.assertEqual(pcm16.count(bytes([VOZ])), chunks_em(2.0) * BYTES_POR_CHUNK, "a fala chega inteira")
        self.assertLessEqual(frases[0].duracao_audio_s, 0.3 + 2.0 + 0.7, "sem o silencio da espera")
        self.assertAlmostEqual(frases[0].inicio_da_escuta, aberta_em + 29.5, delta=0.05)
        self.assertLess(frases[0].inicio_da_escuta, aberta_em + 30.0)

    def test_escuta_de_60_s_apanha_uma_resposta_que_comeca_depois_dos_30(self) -> None:
        ouvido, frases, linhas, passar, aberta_em = _ouvido_com_escuta(60.0)
        passar(SILENCIO, 45.0)
        passar(VOZ, 3.0)
        passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 1)
        self.assertNotIn("frase no maximo", linhas.texto())
        self.assertEqual(ouvido.motor.recebidos[0].count(bytes([VOZ])), chunks_em(3.0) * BYTES_POR_CHUNK)
        self.assertAlmostEqual(frases[0].inicio_da_escuta, aberta_em + 45.0, delta=0.05)

    def test_yes_but_no_fim_do_prazo_nao_vira_um_yes(self) -> None:
        cena = Cena([DITADO_EN, CORRIGIDO_EN])
        cena.motor = cena.ouvido.motor = MotorQueContaAVoz(2.0)
        cena.com_recap()
        cena.passar(SILENCIO, 29.4, verificar=True)
        cena.passar(VOZ, 2.0, verificar=True)
        cena.passar(SILENCIO, 1.0, verificar=True)
        cena.ouvido.transcrever_pendentes()
        self.assertEqual(cena.canal.recebidos, [], "uma frase inteira que nao e um sim nunca envia")
        self.assertEqual(len(cena.m.llm.pedidos), 2, "foi tratada como correcao")

    def test_resposta_depois_de_30_s_com_confirmacao_de_60_envia(self) -> None:
        cena = Cena([DITADO_EN], confirmacao_s=60)
        cena.com_recap()
        cena.passar(SILENCIO, 45.0, verificar=True)
        self.assertEqual(cena.dizer("Yeah."), 1)
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_o_fim_da_voz_do_jarvis_nunca_entra_na_resposta(self) -> None:
        ouvido, frases, linhas, passar, _ = _ouvido_com_escuta(30.0)
        passar(ECO, 0.45)  # "Send it?" ainda a sair da placa de som
        self.assertEqual(ouvido.estado, "ativado", "o eco nao comeca uma frase")
        self.assertEqual(ouvido._buffer, [])
        passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 0)
        self.assertEqual(ouvido.descartadas, 0, "a escuta continua aberta")
        self.assertEqual(ouvido.escuta_aberta, ESCUTA_RECAP)
        passar(VOZ, 1.0)
        passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 1)
        self.assertNotIn(bytes([ECO]), ouvido.motor.recebidos[0])

    def test_eco_colado_a_resposta_fica_de_fora(self) -> None:
        ouvido, frases, linhas, passar, aberta_em = _ouvido_com_escuta(30.0)
        passar(ECO, 0.45)
        passar(VOZ, 1.0)
        passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 1)
        self.assertNotIn(bytes([ECO]), ouvido.motor.recebidos[0])
        self.assertGreaterEqual(frases[0].inicio_da_escuta, aberta_em + 0.45)

    def test_eco_que_passa_o_filtro_nao_envia_sozinho(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.motor.textos.append("Send it?")
        cena.passar(ECO, 0.45, verificar=True)
        cena.passar(SILENCIO, 2.0, verificar=True)
        self.assertEqual(cena.ouvido.transcrever_pendentes(), 0)
        self.assertEqual(cena.canal.recebidos, [])
        self.assertTrue(cena.jarvis.confirmacao.a_espera)

    def test_a_janela_de_conversa_nao_tem_guarda(self) -> None:
        ouvido, frases, linhas, passar, _ = _ouvido_com_escuta(8.0, para="conversa")
        passar(VOZ, 1.0)
        passar(SILENCIO, 1.0)
        self.assertEqual(ouvido.transcrever_pendentes(), 1)
        # So o chunk em que a escuta abre fica de fora, como sempre.
        self.assertEqual(ouvido.motor.recebidos[0].count(bytes([VOZ])), (chunks_em(1.0) - 1) * BYTES_POR_CHUNK)


# --- Prazo -----------------------------------------------------------------------


class TestPrazo(unittest.TestCase):
    def test_silencio_ate_ao_prazo_cancela_sem_enviar_e_fecha_a_escuta(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.passar(SILENCIO, 29.5, verificar=True)
        self.assertTrue(cena.jarvis.confirmacao.a_espera, "o prazo conta desde o fim da voz, nao da geracao")
        cena.passar(SILENCIO, 1.0, verificar=True)
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        self.assertEqual(cena.falados[-1], EXPIRADO_EN)
        self.assertIsNone(cena.escuta_ao_falar[-1], "a frase do prazo nunca e ouvida como resposta")
        self.assertEqual(cena.canal.recebidos, [])
        self.assertIn("confirmacao | expirado", cena.log())
        cena.passar(SILENCIO, 1.0, verificar=True)
        _sem_escuta_nada_e_ouvido(self, cena)
        self.assertEqual(cena.jarvis.painel.atual, A_OUVIR)

    def test_nao_expira_a_meio_de_uma_frase(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.passar(SILENCIO, 29.0, verificar=True)
        cena.motor.textos.append("Yes.")
        cena.passar(VOZ, 2.0, verificar=True)  # passa o prazo a falar
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        cena.passar(SILENCIO, 1.0, verificar=True)  # a frase espera na fila pela transcricao
        self.assertTrue(cena.jarvis.confirmacao.a_espera)
        cena.ouvido.transcrever_pendentes()
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertNotIn(EXPIRADO_EN, cena.falados)

    def test_nao_expira_a_meio_da_transcricao(self) -> None:
        cena = Cena([DITADO_EN])
        cena.com_recap()
        cena.passar(SILENCIO, 28.0, verificar=True)
        cena.motor.textos.append("Yes.")
        cena.passar(VOZ, 1.0)
        cena.passar(SILENCIO, 0.7)

        def transcricao_lenta() -> None:
            cena.relogio.avancar(3.0)
            cena.jarvis.verificar_tempo()

        cena.motor.durante = transcricao_lenta
        cena.ouvido.transcrever_pendentes()
        self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])
        self.assertNotIn(EXPIRADO_EN, cena.falados)


# --- Fora de um recap pendente ---------------------------------------------------


class TestSemRecapPendente(unittest.TestCase):
    def test_sem_nada_pendente_nada_e_ouvido(self) -> None:
        cena = Cena()
        cena.jarvis.verificar_tempo()
        _sem_escuta_nada_e_ouvido(self, cena)
        self.assertEqual(cena.aberturas, [])

    def test_horas_sem_recap_nao_abre_escuta(self) -> None:
        cena = Cena()
        cena.pedir("hey jarvis, what time is it")
        _sem_escuta_nada_e_ouvido(self, cena)

    def test_dorme_durante_o_recap_fecha_a_escuta(self) -> None:
        cena = Cena([DITADO_PT], lingua="pt")
        cena.pedir("boas jarvis, no atlas corrige o teste do login")
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        cena.dizer("dorme")
        self.assertTrue(cena.jarvis.estado.adormecido)
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        cena.passar(SILENCIO, 1.0, verificar=True)
        _sem_escuta_nada_e_ouvido(self, cena)
        self.assertEqual(cena.canal.recebidos, [])

    def test_cala_te_durante_o_recap_fecha_a_escuta(self) -> None:
        cena = Cena([DITADO_PT], lingua="pt")
        cena.pedir("boas jarvis, no atlas corrige o teste do login")
        cena.dizer("cala-te")
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        cena.passar(SILENCIO, 1.0, verificar=True)
        _sem_escuta_nada_e_ouvido(self, cena)
        self.assertEqual(cena.canal.recebidos, [])

    def test_a_janela_de_conversa_continua_a_ser_de_conversa(self) -> None:
        cena = Cena()
        cena.jarvis._abrir_conversa("atlas")
        self.assertEqual(cena.ouvido.escuta_aberta, "conversa")
        self.assertEqual(cena.aberturas[-1][1:3], (8.0, "conversa"))


# --- Texto de arranque -------------------------------------------------------------


class TestCabecalho(unittest.TestCase):
    def _cabecalho(self, vad) -> str:
        cena = Cena()
        ouvido = SimpleNamespace(
            fonte=SimpleNamespace(descricao="microfone falso"), nome_da_tecla="F8", detetor=None, vad=vad
        )
        app._cabecalho(cena.jarvis, ouvido, SimpleNamespace(total_s=1.0, dentro_da_meta=True))
        return cena.m.log.texto()

    def test_diz_que_a_resposta_nao_precisa_da_palavra_de_ativacao(self) -> None:
        texto = self._cabecalho(VadFalso())
        self.assertIn("a resposta ao recap diz-se logo, sem palavra de ativacao, dentro de 30 s", texto)

    def test_sem_vad_diz_que_e_com_a_tecla(self) -> None:
        self.assertIn("a resposta ao recap diz-se com a tecla de falar, dentro de 30 s", self._cabecalho(None))



class TestRespostasReaisAoRecap(unittest.TestCase):
    """As transcricoes reais da aceitacao, ditas sem palavra de ativacao."""

    def test_cancelamentos_ouvidos_cancelam_sem_regra_financeira(self) -> None:
        for frase in (
            "Can't sell it.", "No, can't sell it.", "Castle Castle.", "Uh castle.", "Uh cancel.", "abort", "abort it",
            "a board", "aboard", "a bored", "abored", "uh abort", "cancel",
        ):
            with self.subTest(frase=frase):
                cena = Cena([DITADO_EN])
                cena.com_recap()
                pedidos_ao_llm = len(cena.m.llm.pedidos)
                cena.dizer(frase)
                self.assertFalse(cena.jarvis.confirmacao.a_espera)
                self.assertEqual(cena.canal.recebidos, [])
                self.assertEqual(len(cena.m.llm.pedidos), pedidos_ao_llm, "cancelar nunca vai ao LLM")
                self.assertEqual(cena.falados[-1], "Cancelled, nothing was sent.")
                self.assertNotIn("recusad", cena.log())

    def test_combinacoes_claras_enviam_uma_vez(self) -> None:
        for frase in ("Go, yes.", "yes please", "yes, send it", "yeah go ahead", "ok yes", "yes yes"):
            with self.subTest(frase=frase):
                cena = Cena([DITADO_EN])
                cena.com_recap()
                cena.dizer(frase)
                self.assertEqual(cena.canal.recebidos, [("atlas", "Fix the login test.")])

    def test_confirmar_e_cancelar_juntos_pergunta_de_novo(self) -> None:
        for frase in ("yes abort", "yes cancel", "send it no cancel"):
            with self.subTest(frase=frase):
                cena = Cena([DITADO_EN])
                cena.com_recap()
                cena.dizer(frase)
                self.assertTrue(cena.jarvis.confirmacao.a_espera)
                self.assertEqual(cena.canal.recebidos, [])
                self.assertEqual(cena.falados[-1], "Say yes to send, or abort.")
                self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)

    def test_acrescento_com_hesitacoes_reescrito_limpo_e_enviado_so_com_o_sim(self) -> None:
        pedido = "Read the README and summarize it. Don't change anything."
        limpo = "Read the README and summarize it in Portuguese. Don't change anything."
        cena = Cena(
            [resposta_llm("ditar_prompt", "chamora", pedido), resposta_llm("ditar_prompt", "chamora", limpo)],
            nomes=("atlas", "chamora"),
        )
        cena.com_recap("hey jarvis, tell chamora to read the README and summarize it. Don't change anything.")
        cena.dizer("Uh no, add uh one more uh request. I want to s uh the summarize to be in Portuguese.")
        self.assertEqual(cena.jarvis.confirmacao.recap.pedido.prompt, limpo)
        self.assertEqual(cena.falados[-1], f"To chamora: {limpo} Send it?")
        self.assertEqual(cena.canal.recebidos, [])
        self.assertEqual(cena.ouvido.escuta_aberta, ESCUTA_RECAP)
        cena.dizer("Yes.")
        self.assertEqual(cena.canal.recebidos, [("chamora", limpo)])

    def test_correcao_com_hesitacoes_e_ordem_solta_e_uma_correcao(self) -> None:
        cena = Cena(
            [
                resposta_llm("ditar_prompt", "atlas", "Fix the login screen."),
                resposta_llm("ditar_prompt", "atlas", "Fix the Wipstone."),
            ]
        )
        cena.com_recap("hey jarvis, tell atlas to fix the login screen")
        cena.dizer("The no change uh um the login screen to Wipstone")
        self.assertEqual(len(cena.m.llm.pedidos), 2, "a correcao passou pelo interprete")
        self.assertEqual(cena.jarvis.confirmacao.recap.pedido.prompt, "Fix the Wipstone.")
        self.assertEqual(cena.falados[-1], "To atlas: Fix the Wipstone. Send it?")
        self.assertEqual(cena.canal.recebidos, [])

    def test_estado_do_jarvis_corre_sem_recap(self) -> None:
        cena = Cena([resposta_llm("estado", "jarvis")], nomes=("atlas", "jarvis"))
        cena.pedir("What is the status of Jarvis?")
        self.assertFalse(cena.jarvis.confirmacao.a_espera)
        self.assertFalse(any("Confirm?" in fala for fala in cena.falados))
        self.assertIn("desfecho: executado", cena.log())


if __name__ == "__main__":
    unittest.main()
