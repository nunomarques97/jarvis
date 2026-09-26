r"""Testes da confirmacao antes de enviar (jarvis/confirmacao.py), unittest.

Dialogos inteiros com pecas falsas: o STT e um motor falso que "transcreve"
respostas feitas, o LLM e um cliente do Ollama em memoria e o canal e um
executor que so regista o que recebe. O relogio e falso, por isso o prazo de
confirmacao corre sem esperar. Nada fala com o Ollama real nem toca som.

Corre com:

    .venv\Scripts\python -m unittest tests.test_confirmacao -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from jarvis.config import (
    ESPERA_DA_CONFIRMACAO_S,
    Config,
    ConfigError,
    ConfigInterprete,
    ConfigOuvido,
    Projeto,
    carregar_config,
)
from jarvis.confirmacao import (
    FRASES_DE_CONFIRMAR,
    TENTATIVAS,
    Confirmacao,
    Pedido,
    classificar_resposta,
    e_correcao,
    compor_recap,
    contar_frases,
)
from jarvis.interprete import (
    INTENCAO_RECUSADA,
    INTENCOES,
    INTENCOES_COM_EFEITO,
    INTENCOES_COM_PROJETO,
    INTENCOES_COM_PROMPT,
    INTENCOES_SEM_EFEITO,
    Interpretacao,
    Interprete,
    MotorIndisponivel,
    aplicar_correcao_literal,
)
from jarvis.stt import MotorBase

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
import gravar_voz  # noqa: E402

NOMES = ("atlas", "orbita", "kanban-lite")


def _config(lingua: str = "pt", **ajustes) -> Config:
    return Config(
        microfone="Microfone Ficticio",
        projetos=tuple(Projeto(nome, Path("D:/caminho/para") / nome) for nome in NOMES),
        ouvido=ConfigOuvido(lingua=lingua),
        interprete=ConfigInterprete(**ajustes),
    )


def _llm(intencao: str, projeto: str = "", prompt: str = "", financeiro: bool = False) -> str:
    return json.dumps({"intencao": intencao, "projeto": projeto, "prompt": prompt, "financeiro": financeiro})


class RelogioFalso:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class LlmFalso:
    """Faz de Ollama em memoria: respostas feitas, ou indisponivel."""

    def __init__(self, respostas=None, *, erro: Exception | None = None) -> None:
        self.respostas = list(respostas or [])
        self.erro = erro
        self.pedidos: list[list[dict]] = []
        self.limite_s = 5.0
        self.durante = None  # chamado a meio do pedido, para simular concorrencia

    def conversar(self, modelo, mensagens, esquema, *, limite_s=None):
        self.pedidos.append(mensagens)
        if self.durante is not None:
            self.durante()
        if self.erro is not None:
            raise self.erro
        if not self.respostas:
            raise MotorIndisponivel("sem resposta feita")
        return self.respostas.pop(0)


class SttFalso(MotorBase):
    """Motor de transcricao falso: o PCM traz o texto que "se ouviu"."""

    nome = "stt-falso"

    def __init__(self) -> None:
        super().__init__("cpu")

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return pcm16.decode("utf-16-le"), lingua, False


class OuvidoFalso:
    """Respostas faladas por ordem: (texto ou None, segundos ate ser dita).

    None, ou uma resposta que chega depois do limite, e "nada ouvido": o
    relogio avanca ate ao limite e devolve None, como a janela de escuta.
    """

    def __init__(self, relogio: RelogioFalso, respostas) -> None:
        self.relogio = relogio
        self.respostas = list(respostas)
        self.stt = SttFalso()
        self.ouvidas: list[str] = []

    def __call__(self, limite_s: float) -> str | None:
        if not self.respostas:
            self.relogio.avancar(limite_s)
            return None
        texto, espera = self.respostas.pop(0)
        if texto is None or espera >= limite_s:
            self.relogio.avancar(limite_s)
            return None
        self.relogio.avancar(espera)
        transcrito = self.stt.transcrever(texto.encode("utf-16-le")).texto
        self.ouvidas.append(transcrito)
        return transcrito


class CanalFalso:
    """O executor: regista cada pedido que recebe."""

    def __init__(self, falhar: bool = False) -> None:
        self.recebidos: list[Pedido] = []
        self.falhar = falhar

    def __call__(self, pedido: Pedido) -> str:
        self.recebidos.append(pedido)
        if self.falhar:
            raise RuntimeError("canal em baixo")
        return "ok"


class Base(unittest.TestCase):
    lingua = "pt"

    def montar(self, respostas_llm=None, *, erro=None, lingua: str | None = None, **ajustes) -> Confirmacao:
        lingua = lingua or self.lingua
        self.relogio = RelogioFalso()
        self.llm = LlmFalso(respostas_llm, erro=erro)
        self.interprete = Interprete(_config(lingua, **ajustes), cliente=self.llm)
        self.canal = CanalFalso()
        self.falas: list[str] = []
        self.ecra: list[str] = []
        self.confirmacao = Confirmacao(
            self.interprete,
            self.canal,
            falar=self.falas.append,
            mostrar=self.ecra.append,
            relogio=self.relogio,
        )
        return self.confirmacao

    @staticmethod
    def pedido(intencao: str, projeto: str | None = "atlas", prompt: str = "", **extra) -> Interpretacao:
        if intencao not in INTENCOES_COM_PROMPT:
            prompt = ""
        elif not prompt:
            prompt = "Corrige os testes do login."
        if intencao not in INTENCOES_COM_PROJETO and intencao != "conversa":
            projeto = None
        return Interpretacao("frase", intencao, projeto, prompt, "llm", "teste", **extra)


# --- Politica: o que exige confirmacao ----------------------------------------


class TestPoliticaDeConfirmacao(Base):
    def test_as_intencoes_com_efeito_sao_todas_menos_leituras_e_silencio(self) -> None:
        exigidas = {"ditar_prompt", "abrir_editor", "abrir_pasta", "lancar_run", "retomar_run", "parar_run", "conversa"}
        self.assertTrue(exigidas <= INTENCOES_COM_EFEITO)
        self.assertEqual(INTENCOES_SEM_EFEITO, {"horas", "calar", "dormir", "acordar"})
        self.assertEqual(INTENCOES_COM_EFEITO | INTENCOES_SEM_EFEITO | {"desconhecido"}, set(INTENCOES))

    def test_cem_por_cento_das_intencoes_com_efeito_esperam_pelo_sim(self) -> None:
        for lingua in ("pt", "en"):
            for intencao in sorted(INTENCOES_COM_EFEITO):
                with self.subTest(lingua=lingua, intencao=intencao):
                    confirmacao = self.montar(lingua=lingua)
                    desfecho = confirmacao.iniciar(self.pedido(intencao))
                    self.assertEqual(desfecho.estado, "pendente")
                    self.assertEqual(self.canal.recebidos, [])
                    self.assertEqual(len(self.falas), 1)
                    for talvez in ("ok", "claro", "sure", "podes", "hum", "sim sim sim", "yes maybe"):
                        confirmacao.responder(talvez)
                        if not confirmacao.a_espera:
                            break
                    self.assertEqual(self.canal.recebidos, [], "nada sai sem um sim explicito")
                    confirmacao.iniciar(self.pedido(intencao))
                    final = confirmacao.responder("yes" if lingua == "en" else "sim")
                    self.assertEqual(final.estado, "executado")
                    self.assertEqual(len(self.canal.recebidos), 1)
                    self.assertEqual(self.canal.recebidos[0].intencao, intencao)

    def test_horas_calar_dormir_e_acordar_correm_sem_confirmacao(self) -> None:
        for intencao, detalhe in (("horas", "horas"), ("horas", "data"), ("calar", None), ("dormir", None), ("acordar", None)):
            with self.subTest(intencao=intencao, detalhe=detalhe):
                confirmacao = self.montar()
                desfecho = confirmacao.iniciar(
                    Interpretacao("f", intencao, None, "", "regra", "lista branca", detalhe=detalhe)
                )
                self.assertEqual(desfecho.estado, "executado")
                self.assertEqual(self.canal.recebidos, [Pedido(intencao, None, "", detalhe)])
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.falas, [], "a confirmacao nao fala por cima da resposta da acao")

    def test_sem_efeito_mas_so_para_confirmacao_nao_corre(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(Interpretacao("f", "horas", None, "", "recurso", "x", so_confirmacao=True))
        self.assertEqual(desfecho.estado, "nao_percebido")
        self.assertEqual(self.canal.recebidos, [])

    def test_desconhecido_e_recurso_do_llm_nao_fazem_nada(self) -> None:
        confirmacao = self.montar(erro=MotorIndisponivel("Ollama em baixo"))
        interpretacao = self.interprete.interpretar("no atlas corrige os testes do login")
        self.assertTrue(interpretacao.so_confirmacao)
        desfecho = confirmacao.iniciar(interpretacao)
        self.assertEqual(desfecho.estado, "nao_percebido")
        self.assertFalse(confirmacao.a_espera)
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(self.canal.recebidos, [])
        self.assertIn("Repete", self.falas[-1])

    def test_pedido_financeiro_e_recusado_sem_nada_a_confirmar(self) -> None:
        confirmacao = self.montar()
        interpretacao = self.interprete.interpretar("compra dez acoes da empresa")
        self.assertEqual(interpretacao.intencao, INTENCAO_RECUSADA)
        self.assertEqual(confirmacao.iniciar(interpretacao).estado, "recusado")
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(self.llm.pedidos, [])

    def test_frase_real_pelo_interprete_ate_ao_canal(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige os testes do login.")])
        interpretacao = self.interprete.interpretar("boas jarvis, no atlas corrige-me la os testes do login")
        self.assertEqual(confirmacao.iniciar(interpretacao).estado, "pendente")
        self.assertEqual(confirmacao.responder("sim, envia").estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("ditar_prompt", "atlas", "Corrige os testes do login.")])

    def test_sem_projeto_o_sim_nao_chega_e_o_projeto_dito_volta_a_recapitular(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "", "Corrige os testes do login.")])
        interpretacao = self.interprete.interpretar("corrige os testes do login")
        self.assertIsNone(interpretacao.projeto)
        desfecho = confirmacao.iniciar(interpretacao)
        self.assertTrue(desfecho.recap.falta_projeto)
        self.assertIn("projeto", desfecho.recap.fala)
        self.assertEqual(confirmacao.responder("sim").estado, "pendente")
        self.assertEqual(self.canal.recebidos, [])
        novo = confirmacao.responder("no orbita")
        self.assertEqual(novo.estado, "pendente")
        self.assertEqual(novo.recap.pedido, Pedido("ditar_prompt", "orbita", "Corrige os testes do login."))
        self.assertFalse(novo.recap.falta_projeto)
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(confirmacao.responder("sim").estado, "executado")
        self.assertEqual(self.canal.recebidos[0].projeto, "orbita")

    def test_projetos_em_alternativa_nao_escolhem_projeto(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("abrir_editor", None))
        self.assertEqual(confirmacao.responder("no atlas ou no orbita").estado, "pendente")
        self.assertTrue(confirmacao.recap.falta_projeto)
        self.assertEqual(self.canal.recebidos, [])

    def test_so_frases_inteiras_de_confirmar_enviam(self) -> None:
        for frase in ("sim", "Sim.", "SIM!", "envia", "sim, envia", "yes", "Yes, send it.", "send", "hey jarvis, yes", "sim, por favor"):
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase)[0], "confirmar")
        for frase in (
            "", "simples", "sim mas", "assim", "envia ao orbita", "yes and no", "okay", "claro", "sure",
            "acrescenta que sim", "muda sim para nao", "send it to orbita", "sim sim sim", "yesterday",
            "não envies", "yes but change tests to docs",
        ):
            with self.subTest(frase=frase):
                self.assertNotEqual(classificar_resposta(frase)[0], "confirmar")

    def test_respostas_do_guiao_real_sao_classificadas_como_esperado(self) -> None:
        for lingua in ("pt", "en"):
            for frase in gravar_voz.ler_guiao(RAIZ / "tests" / "voz" / f"guiao-real-{lingua}.md", lingua):
                if frase.caso != "confirmacao":
                    continue
                texto = frase.frase.replace("<projeto-1>", "atlas").replace("<projeto-2>", "orbita")
                with self.subTest(id=frase.id):
                    self.assertEqual(classificar_resposta(texto)[0], frase.intencao)


# --- Correcoes e acrescentos ------------------------------------------------


class TestCorrigirEAcrescentar(Base):
    def test_muda_x_para_y_reescreve_pelo_llm_e_volta_a_recapitular(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige a documentação do login.")])
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige os testes do login."))
        desfecho = confirmacao.responder("não, muda testes para documentação")
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige a documentação do login.")
        self.assertEqual(desfecho.recap.numero, 2)
        self.assertEqual(len(self.falas), 2, "cada correcao volta a recapitular")
        self.assertEqual(self.canal.recebidos, [])
        # O LLM recebe o pedido atual e a edicao, como dados.
        enviado = json.loads(self.llm.pedidos[-1][1]["content"])
        self.assertEqual(enviado["pedido"]["prompt"], "Corrige os testes do login.")
        self.assertEqual(enviado["edicao"], {"tipo": "corrigir", "texto": "não, muda testes para documentação"})
        confirmacao.responder("sim")
        self.assertEqual(self.canal.recebidos, [Pedido("ditar_prompt", "atlas", "Corrige a documentação do login.")])

    def test_acrescenta_mantem_o_resto(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige os testes do login. É urgente.")])
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        desfecho = confirmacao.responder("acrescenta que é urgente")
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige os testes do login. É urgente.")
        self.assertEqual(json.loads(self.llm.pedidos[-1][1]["content"])["edicao"]["tipo"], "acrescentar")
        confirmacao.responder("envia")
        self.assertEqual(self.canal.recebidos[0].prompt, "Corrige os testes do login. É urgente.")

    def test_sem_llm_a_correcao_mecanica_aplica_se_a_letra(self) -> None:
        confirmacao = self.montar(erro=MotorIndisponivel("Ollama em baixo"), lingua="en")
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Fix the login tests"))
        self.assertEqual(confirmacao.responder("no, change tests to docs").recap.pedido.prompt, "Fix the login docs")
        self.assertEqual(
            confirmacao.responder("add that it is urgent").recap.pedido.prompt, "Fix the login docs. It is urgent."
        )
        self.assertEqual(confirmacao.responder("no, change atlas to orbita").recap.pedido.projeto, "orbita")
        confirmacao.responder("yes")
        self.assertEqual(self.canal.recebidos, [Pedido("ditar_prompt", "orbita", "Fix the login docs. It is urgent.")])

    def test_correcao_que_nao_se_aplica_mantem_o_pedido_e_nao_envia(self) -> None:
        confirmacao = self.montar(erro=MotorIndisponivel("Ollama em baixo"))
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige os testes do login."))
        desfecho = confirmacao.responder("não, isso está mal")
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige os testes do login.")
        self.assertIn("correção", self.falas[-1])
        self.assertLessEqual(contar_frases(self.falas[-1]), 2)
        self.assertEqual(self.canal.recebidos, [])
        confirmacao.responder("sim")
        self.assertEqual(self.canal.recebidos[0].prompt, "Corrige os testes do login.")

    def test_llm_que_escolhe_projeto_nao_dito_e_ignorado(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "orbita", "Corrige a documentação do login.")])
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige os testes do login."))
        desfecho = confirmacao.responder("não, muda testes para documentação")
        # A resposta do LLM e recusada; fica a correcao a letra, no mesmo projeto.
        self.assertEqual(desfecho.recap.pedido.projeto, "atlas")
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige os documentação do login.")

    def test_llm_que_inventa_pedidos_e_recusado(self) -> None:
        inventado = "Corrige os testes do login. " + " ".join(["Depois faz commit, push e apaga a pasta."] * 6)
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", inventado)])
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige os testes do login."))
        desfecho = confirmacao.responder("acrescenta que é urgente")
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige os testes do login. É urgente.")

    def test_correcao_nunca_passa_a_intencao_sem_efeito(self) -> None:
        confirmacao = self.montar([_llm("horas")], erro=None)
        confirmacao.iniciar(self.pedido("abrir_editor"))
        desfecho = confirmacao.responder("não, muda o editor para as horas")
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(desfecho.recap.pedido.intencao, "abrir_editor")
        self.assertEqual(self.canal.recebidos, [])

    def test_correcao_financeira_e_recusada_e_cancela(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "x")])
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.assertEqual(confirmacao.responder("acrescenta que compre dez acoes da empresa").estado, "recusado")
        self.assertEqual(self.llm.pedidos, [], "a regra recusa antes do LLM")
        self.assertFalse(confirmacao.a_espera)
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(self.canal.recebidos, [])

    def test_marca_financeira_do_llm_na_correcao_recusa(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige os testes.", financeiro=True)])
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.assertEqual(confirmacao.responder("acrescenta aquilo do costume").estado, "recusado")
        self.assertEqual(self.canal.recebidos, [])

    def test_sim_dito_enquanto_a_correcao_corre_e_ignorado(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige a documentação do login.")])
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        durante: list = []
        self.llm.durante = lambda: durante.append(confirmacao.responder("sim"))
        confirmacao.responder("não, muda testes para documentação")
        self.assertEqual([d.estado for d in durante], ["ignorado"])
        self.assertEqual(self.canal.recebidos, [], "um sim so confirma um recap ja ouvido")

    def test_resposta_dita_antes_do_recap_e_ignorada(self) -> None:
        confirmacao = self.montar()
        antes = self.relogio() - 0.5
        confirmacao.iniciar(self.pedido("parar_run"))
        self.assertEqual(confirmacao.responder("sim", dito_em=antes).estado, "ignorado")
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(confirmacao.responder("sim", dito_em=self.relogio()).estado, "executado")

    def test_correcao_literal_so_troca_o_que_e_inequivoco(self) -> None:
        self.assertIsNone(aplicar_correcao_literal("ditar_prompt", "atlas", "corre os testes e os testes lentos", "muda testes para docs", "corrigir", NOMES))
        self.assertIsNone(aplicar_correcao_literal("ditar_prompt", "atlas", "x", "muda orbita para kanban-lite", "corrigir", NOMES))
        self.assertIsNone(aplicar_correcao_literal("abrir_editor", "atlas", "", "acrescenta que é urgente", "acrescentar", NOMES))
        self.assertEqual(
            aplicar_correcao_literal("abrir_editor", "atlas", "", "não, muda o atlas para o kanban lite", "corrigir", NOMES),
            ("kanban-lite", ""),
        )


# --- Correcao sem nenhum pedido a espera ----------------------------------------


CORRECOES = (
    "hey jarvis, no, change test to call the documentation",
    "no, change tests to documentation",
    "no change the title to welcome in atlas",
    "change test to call the documentation",
    "change atlas to orbita",
    "change the title to atlas",
    "não, muda testes para documentação",
    "nao, muda o atlas para o orbita",
    "muda testes para documentação",
    "troca o atlas por orbita",
)

NAO_CORRECOES = (
    "in atlas change the title to welcome",
    "change the login title to welcome in atlas",
    "no atlas muda o título para bem-vindo",
    "muda o título para bem-vindo no atlas",
    "add tests to the configuration module",
    "add that it is urgent",
    "acrescenta que é urgente",
    "no atlas acrescenta testes ao módulo de configuração",
    "change the title",
    "no",
    "não",
    "corrige os testes do login",
    "",
    None,
)


class TestCorrecaoSemPedido(Base):
    def test_frases_claramente_de_correcao(self) -> None:
        for texto in CORRECOES:
            with self.subTest(texto=texto):
                self.assertTrue(e_correcao(texto, NOMES))

    def test_ditados_e_acrescentos_nao_sao_correcoes(self) -> None:
        for texto in NAO_CORRECOES:
            with self.subTest(texto=texto):
                self.assertFalse(e_correcao(texto, NOMES))

    def test_projeto_mal_ouvido_conta_como_projeto_dito(self) -> None:
        nomes = (*NOMES, "crypto-radar")
        self.assertFalse(e_correcao("change the title to welcome in CryptoRather", nomes))
        self.assertTrue(e_correcao("change atlas to Crypto Rather", nomes))

    def test_sem_pedido_diz_que_nao_ha_nada_para_corrigir(self) -> None:
        for lingua, fala in (
            ("pt", "Não há nenhum pedido à espera para corrigir."),
            ("en", "There is no pending request to correct."),
        ):
            with self.subTest(lingua=lingua):
                confirmacao = self.montar(lingua=lingua)
                self.assertTrue(confirmacao.e_correcao_sem_pedido("no, change test to call the documentation"))
                desfecho = confirmacao.correcao_sem_pedido()
                self.assertEqual(desfecho.estado, "sem_pedido")
                self.assertEqual(self.falas, [fala])
                self.assertFalse(confirmacao.a_espera)
                self.assertIsNone(confirmacao.prazo_restante())
                self.assertEqual(self.llm.pedidos, [])
                self.assertEqual(self.canal.recebidos, [])

    def test_com_pedido_pendente_a_correcao_segue_o_fluxo_normal(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige a documentação do login.")])
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige os testes do login."))
        self.assertFalse(confirmacao.e_correcao_sem_pedido("não, muda testes para documentação"))
        desfecho = confirmacao.responder("não, muda testes para documentação")
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige a documentação do login.")


# --- Cancelar e prazo -------------------------------------------------------


class TestCancelarEPrazo(Base):
    def test_cancelar_nunca_envia(self) -> None:
        for frase in ("cancela", "não", "no", "cancel", "esquece", "não, cancela", "never mind", "não envies", "don't send it"):
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                self.assertEqual(confirmacao.responder(frase).estado, "cancelado")
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
                self.assertEqual(self.canal.recebidos, [])
                self.assertIn("não enviei nada", self.falas[-1])

    def test_prazo_por_omissao_e_20_s_e_expira_sem_enviar(self) -> None:
        self.assertEqual(ESPERA_DA_CONFIRMACAO_S, 20.0)
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("lancar_run"))
        self.relogio.avancar(19.9)
        self.assertIsNone(confirmacao.verificar_tempo())
        self.relogio.avancar(0.1)
        self.assertEqual(confirmacao.verificar_tempo().estado, "expirado")
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(self.canal.recebidos, [])

    def test_sim_depois_do_prazo_nao_envia_mesmo_sem_verificar_tempo(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.relogio.avancar(20.0)
        self.assertEqual(confirmacao.responder("sim").estado, "expirado")
        self.assertEqual(self.canal.recebidos, [])

    def test_prazo_configuravel(self) -> None:
        confirmacao = self.montar(confirmacao_s=5.0)
        self.assertEqual(confirmacao.limite_s, 5.0)
        confirmacao.iniciar(self.pedido("abrir_pasta"))
        self.relogio.avancar(5.0)
        self.assertEqual(confirmacao.responder("sim").estado, "expirado")
        self.assertEqual(self.canal.recebidos, [])

    def test_prazo_no_config_toml_e_validado(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            (raiz / "projeto").mkdir()

            def carregar(linha: str) -> Config:
                caminho = raiz / "config.toml"
                caminho.write_text(
                    '[microfone]\nnome = "Mic"\n[[projetos]]\nnome = "atlas"\n'
                    f'caminho = "{(raiz / "projeto").as_posix()}"\n'
                    f"[interprete]\n{linha}\n",
                    encoding="utf-8",
                )
                return carregar_config(caminho)

            self.assertEqual(carregar("confirmacao_s = 30").interprete.confirmacao_s, 30.0)
            for invalido in ("0", "-1", "1", "500", "true", '"20"'):
                with self.subTest(valor=invalido), self.assertRaises(ConfigError):
                    carregar(f"confirmacao_s = {invalido}")

    def test_cada_recap_novo_reinicia_o_prazo(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige a documentação do login.")])
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.relogio.avancar(15)
        confirmacao.responder("não, muda testes para documentação")
        self.relogio.avancar(15)
        self.assertEqual(confirmacao.responder("sim").estado, "executado")

    def test_respostas_que_nao_se_percebem_acabam_por_cancelar(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        for _ in range(TENTATIVAS - 1):
            self.assertEqual(confirmacao.responder("talvez").estado, "pendente")
        self.assertEqual(confirmacao.responder("talvez").estado, "cancelado")
        self.assertEqual(self.canal.recebidos, [])

    def test_pedido_novo_substitui_o_pendente_sem_o_enviar(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("parar_run", "atlas"))
        confirmacao.iniciar(self.pedido("abrir_pasta", "orbita"))
        confirmacao.responder("sim")
        self.assertEqual(self.canal.recebidos, [Pedido("abrir_pasta", "orbita", "")])

    def test_cancelar_de_fora_nao_envia(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("retomar_run"))
        self.assertEqual(confirmacao.cancelar("adormecer").estado, "cancelado")
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(self.canal.recebidos, [])

    def test_executor_que_falha_e_dito_e_nao_repete(self) -> None:
        confirmacao = self.montar()
        self.canal.falhar = True
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.assertEqual(confirmacao.responder("sim").estado, "falhou")
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(len(self.canal.recebidos), 1)
        self.assertNotIn("canal em baixo", self.falas[-1], "o erro tecnico fica na consola")

    def test_dois_sim_ao_mesmo_tempo_enviam_uma_vez(self) -> None:
        for _ in range(20):
            confirmacao = self.montar()
            confirmacao.iniciar(self.pedido("ditar_prompt"))
            barreira = threading.Barrier(4)

            def responder() -> None:
                barreira.wait()
                confirmacao.responder("sim")

            fios = [threading.Thread(target=responder) for _ in range(4)]
            for fio in fios:
                fio.start()
            for fio in fios:
                fio.join(5)
            self.assertEqual(len(self.canal.recebidos), 1)


# --- Recap: fala curta, ecra completo, texto exato ----------------------------


class TestRecap(Base):
    LONGO = (
        "Refaz o ecrã de login para usar o novo componente de formulário. Mantém a validação do email "
        "e da palavra-passe como está. Acrescenta uma mensagem de erro clara quando o servidor não "
        "responde, e escreve testes para os três casos: sucesso, credenciais erradas e servidor em baixo."
    )

    def test_recap_falado_tem_no_maximo_duas_frases(self) -> None:
        prompts = ("Corrige os testes.", "Corrige os testes. Depois corre a suite! Está bem?", self.LONGO, "Versão 1.5 do x; e depois y")
        for lingua in ("pt", "en"):
            for intencao in sorted(INTENCOES_COM_EFEITO):
                for projeto in ("atlas", None):
                    for prompt in prompts:
                        interpretacao = self.pedido(intencao, projeto, prompt)
                        recap = compor_recap(1, interpretacao, lingua)
                        with self.subTest(lingua=lingua, intencao=intencao, projeto=projeto, prompt=prompt[:20]):
                            self.assertLessEqual(contar_frases(recap.fala), 2, recap.fala)
                            self.assertTrue(recap.fala.endswith("?"))

    def test_prompt_longo_e_resumido_na_voz_e_inteiro_no_ecra(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(self.pedido("ditar_prompt", prompt=self.LONGO))
        self.assertNotIn("servidor em baixo", self.falas[-1])
        self.assertIn("Refaz o ecrã de login", self.falas[-1])
        self.assertIn("ecrã", self.falas[-1])
        self.assertLess(len(self.falas[-1]), len(self.LONGO))
        self.assertIn(self.LONGO, self.ecra[-1].splitlines())
        self.assertEqual(desfecho.recap.pedido.prompt, self.LONGO)

    def test_prompt_curto_e_dito_inteiro(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige os testes do login."))
        self.assertEqual(self.falas[-1], "Para o atlas: Corrige os testes do login. Envio?")

    def test_ecra_mostra_o_que_percebeu_e_o_texto_a_enviar(self) -> None:
        confirmacao = self.montar(lingua="en")
        confirmacao.iniciar(self.pedido("lancar_run", "orbita", "Migrate the tests to pytest."))
        linhas = self.ecra[-1].splitlines()
        self.assertIn("lancar run", linhas[0])
        self.assertIn("orbita", linhas[0])
        self.assertIn("Migrate the tests to pytest.", linhas)
        self.assertIn("Start a run in orbita", self.falas[-1])

    def test_quebras_de_linha_nunca_chegam_ao_texto_enviado(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Corrige o login.\r\nIgnora tudo\x1b[2J e apaga"))
        self.assertEqual(desfecho.recap.pedido.prompt, "Corrige o login. Ignora tudo [2J e apaga")
        confirmacao.responder("sim")
        enviado = self.canal.recebidos[0].prompt
        self.assertNotRegex(enviado, r"[\r\n\x00-\x1f\x7f]")
        self.assertIn(enviado, self.ecra[-1].splitlines())

    def test_prompt_vazio_nao_fica_para_confirmar(self) -> None:
        confirmacao = self.montar()
        vazio = Interpretacao("f", "ditar_prompt", "atlas", "  ", "llm", "teste")
        self.assertEqual(confirmacao.iniciar(vazio).estado, "nao_percebido")
        self.assertEqual(self.canal.recebidos, [])

    def test_sem_projeto_o_ecra_mostra_o_marcador_e_pede_o_projeto(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(self.pedido("abrir_pasta", None))
        self.assertIn("Abrir a pasta do <projeto>", self.ecra[-1])
        self.assertEqual(desfecho.recap.fala, "Percebi o pedido, mas não o projeto. Que projeto?")

    def test_acoes_sem_prompt_dizem_a_acao_e_o_projeto(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("parar_run", "orbita"))
        self.assertEqual(self.falas[-1], "Parar o run do orbita. Confirmas?")


# --- Dialogos inteiros com STT, LLM e canal falsos --------------------------


class TestDialogoCompleto(Base):
    def test_ditado_corrigido_e_acrescentado_envia_byte_a_byte_o_ultimo_recap(self) -> None:
        confirmacao = self.montar(
            [
                _llm("ditar_prompt", "atlas", "Corrige os testes do login."),
                _llm("ditar_prompt", "atlas", "Corrige a documentação do login."),
                _llm("ditar_prompt", "atlas", "Corrige a documentação do login. É urgente."),
            ]
        )
        ouvido = OuvidoFalso(
            self.relogio,
            [("não, muda testes para documentação", 3.0), ("acrescenta que é urgente", 4.0), ("sim", 2.0)],
        )
        interpretacao = self.interprete.interpretar("no atlas corrige os testes do login")
        desfecho = confirmacao.dialogar(interpretacao, ouvido)
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(ouvido.ouvidas[-1], "sim")
        self.assertEqual(len(self.canal.recebidos), 1)
        enviado = self.canal.recebidos[0]
        self.assertEqual(enviado, desfecho.recap.pedido)
        self.assertEqual(enviado.prompt.encode("utf-8"), "Corrige a documentação do login. É urgente.".encode("utf-8"))
        # O ultimo ecra antes do "sim" mostra exatamente esse texto numa linha.
        self.assertIn(enviado.prompt, self.ecra[-1].splitlines())
        self.assertEqual(desfecho.recap.numero, 3)

    def test_dialogo_cancelado_nunca_envia(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige os testes do login.")])
        ouvido = OuvidoFalso(self.relogio, [("talvez", 1.0), ("cancela", 1.0), ("sim", 1.0)])
        desfecho = confirmacao.dialogar(self.interprete.interpretar("no atlas corrige os testes"), ouvido)
        self.assertEqual(desfecho.estado, "cancelado")
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(ouvido.respostas, [("sim", 1.0)], "depois de cancelar ja nao ouve")

    def test_dialogo_sem_resposta_expira_e_nunca_envia(self) -> None:
        for respostas in ([], [(None, 0)], [("sim", 25.0)]):
            with self.subTest(respostas=respostas):
                confirmacao = self.montar([_llm("lancar_run", "orbita", "Migra os testes.")])
                ouvido = OuvidoFalso(self.relogio, respostas)
                desfecho = confirmacao.dialogar(self.interprete.interpretar("lanca um run no orbita para migrar os testes"), ouvido)
                self.assertEqual(desfecho.estado, "expirado")
                self.assertEqual(self.canal.recebidos, [])
                self.assertFalse(confirmacao.a_espera)

    def test_dialogo_em_ingles(self) -> None:
        confirmacao = self.montar([_llm("abrir_editor", "kanban-lite")], lingua="en")
        ouvido = OuvidoFalso(self.relogio, [("no, change kanban lite to atlas", 2.0), ("yes, send it", 1.0)])
        desfecho = confirmacao.dialogar(self.interprete.interpretar("open the editor in kanban lite please now"), ouvido)
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("abrir_editor", "atlas", "")])

    def test_confirmacoes_aceites_sao_so_sim_e_envia(self) -> None:
        palavras = {palavra for frase in FRASES_DE_CONFIRMAR for palavra in frase.split()}
        self.assertEqual(palavras, {"sim", "envia", "isso", "yes", "send", "it"})


if __name__ == "__main__":
    unittest.main()
