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
from dataclasses import replace
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
    INTENCAO_ESQUECER_FACTO,
    INTENCAO_LEMBRAR_FACTO,
    INTENCOES_DA_MEMORIA,
    INTENCOES_SO_DE_LEITURA,
    PALAVRAS_DE_CONFIRMAR,
    PALAVRAS_DE_CORTESIA,
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
        self.assertEqual(
            INTENCOES_COM_EFEITO | INTENCOES_SEM_EFEITO | {"desconhecido", "pergunta_geral"}, set(INTENCOES)
        )

    def test_cem_por_cento_das_intencoes_com_efeito_esperam_pelo_sim(self) -> None:
        for lingua in ("pt", "en"):
            for intencao in sorted(INTENCOES_COM_EFEITO - INTENCOES_SO_DE_LEITURA):
                with self.subTest(lingua=lingua, intencao=intencao):
                    confirmacao = self.montar(lingua=lingua)
                    desfecho = confirmacao.iniciar(self.pedido(intencao))
                    self.assertEqual(desfecho.estado, "pendente")
                    self.assertEqual(self.canal.recebidos, [])
                    self.assertEqual(len(self.falas), 1)
                    for talvez in ("ok", "claro", "surely", "podes", "hum", "sim talvez", "yes maybe"):
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
            "", "simples", "sim mas", "assim", "envia ao orbita", "yes and no", "okay", "claro", "surely",
            "acrescenta que sim", "muda sim para nao", "send it to orbita", "sim sim talvez", "yesterday",
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

    def test_um_no_fim_da_correcao_e_uma_palavra(self) -> None:
        # "um" so e hesitacao ao decidir enviar ou cancelar, nunca no fim de
        # uma correcao.
        for texto in ("não, muda o dois para um", "muda o atlas para um", "não, troca o um para dois"):
            with self.subTest(texto=texto):
                self.assertTrue(e_correcao(texto, NOMES))
        self.assertEqual(classificar_resposta("yes, uh."), ("confirmar", ""))

    def test_projeto_mal_ouvido_conta_como_projeto_dito(self) -> None:
        nomes = (*NOMES, "crypto-radar")
        self.assertFalse(e_correcao("change the title to welcome in CryptoRather", nomes))
        self.assertTrue(e_correcao("change atlas to Crypto Rather", nomes))

    def test_sem_pedido_diz_que_nao_ha_nada_para_corrigir(self) -> None:
        for lingua, fala in (
            ("pt", "Não há nenhum pedido à espera para corrigir."),
            ("en", "There's nothing waiting to correct."),
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

    def test_prazo_por_omissao_e_30_s_e_expira_sem_enviar(self) -> None:
        self.assertEqual(ESPERA_DA_CONFIRMACAO_S, 30.0)
        self.assertEqual(ConfigInterprete().confirmacao_s, 30.0)
        confirmacao = self.montar()
        self.assertEqual(confirmacao.limite_s, 30.0)
        confirmacao.iniciar(self.pedido("lancar_run"))
        self.relogio.avancar(29.9)
        self.assertIsNone(confirmacao.verificar_tempo())
        self.relogio.avancar(0.1)
        self.assertEqual(confirmacao.verificar_tempo().estado, "expirado")
        self.assertEqual(confirmacao.responder("sim").estado, "sem_pedido")
        self.assertEqual(self.canal.recebidos, [])

    def test_sim_depois_do_prazo_nao_envia_mesmo_sem_verificar_tempo(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.relogio.avancar(30.0)
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


# --- Respostas ao recap: horas, hesitacoes, cancelar tolerante, enviar estrito ----


class TestHorasComPedidoPendente(Base):
    lingua = "en"

    def test_horas_sao_respondidas_e_o_pedido_fica_pendente_com_prazo_novo(self) -> None:
        for frase, detalhe in (
            ("hey jarvis, what time is it?", "horas"),
            ("Uh, what time is it?", "horas"),
            ("que horas são", "horas"),
            ("what's the date today", "data"),
        ):
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                recap = confirmacao.recap
                self.relogio.avancar(15)
                desfecho = confirmacao.responder(frase)
                self.assertEqual(desfecho.estado, "executado")
                self.assertEqual(desfecho.pedido, Pedido("horas", None, "", detalhe))
                self.assertEqual(self.canal.recebidos, [Pedido("horas", None, "", detalhe)])
                self.assertTrue(confirmacao.a_espera)
                self.assertIs(confirmacao.recap, recap)
                self.assertEqual(confirmacao.prazo_restante(), confirmacao.limite_s)
                self.relogio.avancar(15)
                self.assertIsNone(confirmacao.verificar_tempo(), "o prazo recomecou depois das horas")
                self.assertEqual(confirmacao.responder("yes").estado, "executado")
                self.assertEqual(self.canal.recebidos[-1], recap.pedido)

    def test_horas_nao_contam_como_resposta_falhada(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        for _ in range(TENTATIVAS - 1):
            self.assertEqual(confirmacao.responder("maybe").estado, "pendente")
        confirmacao.responder("what time is it")
        self.assertTrue(confirmacao.a_espera)
        self.assertEqual(confirmacao.responder("maybe").estado, "cancelado")
        self.assertEqual(self.canal.recebidos, [Pedido("horas", None, "", "horas")])

    def test_so_a_frase_inteira_de_horas_conta(self) -> None:
        for frase in ("yes, what time is it", "what time is it in atlas", "no, change the time to noon"):
            with self.subTest(frase=frase):
                confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Corrige os testes do login.")])
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                desfecho = confirmacao.responder(frase)
                self.assertNotEqual(desfecho.estado, "executado")
                self.assertNotIn("horas", [pedido.intencao for pedido in self.canal.recebidos])

    def test_horas_que_falham_mantem_o_pedido(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.canal.falhar = True
        self.assertEqual(confirmacao.responder("what time is it").estado, "falhou")
        self.assertTrue(confirmacao.a_espera)
        self.canal.falhar = False
        self.assertEqual(confirmacao.responder("yes").estado, "executado")

    def test_resposta_durante_as_horas_e_ignorada(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        durante: list[str] = []
        executar = self.canal

        def executar_e_responder(pedido: Pedido) -> str:
            if pedido.intencao == "horas":
                durante.append(confirmacao.responder("yes").estado)
            return executar(pedido)

        confirmacao._executar = executar_e_responder
        confirmacao.responder("what time is it")
        self.assertEqual(durante, ["ignorado"])
        self.assertEqual([pedido.intencao for pedido in self.canal.recebidos], ["horas"])
        self.assertTrue(confirmacao.a_espera)

    def test_dialogo_com_horas_pelo_meio(self) -> None:
        confirmacao = self.montar()
        ouvido = OuvidoFalso(self.relogio, [("hey jarvis, what time is it?", 15.0), ("yes", 15.0)])
        pedido = self.pedido("ditar_prompt", prompt="Add tests to the configuration module.")
        desfecho = confirmacao.dialogar(pedido, ouvido)
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(
            self.canal.recebidos,
            [Pedido("horas", None, "", "horas"), Pedido("ditar_prompt", "atlas", "Add tests to the configuration module.")],
        )


class TestRespostasMalOuvidas(Base):
    lingua = "en"

    def test_hesitacoes_e_pontuacao_nao_contam(self) -> None:
        casos = {
            "Uh, yes.": "confirmar",
            "yes.": "confirmar",
            "um, sim, envia": "confirmar",
            "Hmm... send it!": "confirmar",
            "yes, uh": "confirmar",
            "Uh, cancel.": "cancelar",
            "Uh, no, change tests to docs": "corrigir",
            "Er, add that it is urgent": "acrescentar",
        }
        for frase, tipo in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase)[0], tipo)
        self.assertEqual(classificar_resposta("Uh, no, change tests to docs")[1], "no, change tests to docs")
        self.assertEqual(classificar_resposta("muda um teste para dois")[1], "muda um teste para dois")

    def test_enviar_so_com_sim_claro(self) -> None:
        for frase in (
            "yet", "yeah no", "guess", "yes sir change it", "jess", "yess", "yeahh", "yes uh send it",
            "says", "send in", "sin", "uh", "confirmed", "sent it", "yes cancel", "yes yes maybe",
        ):
            with self.subTest(frase=frase):
                self.assertNotEqual(classificar_resposta(frase)[0], "confirmar")

    def test_frases_parecidas_com_sim_nao_enviam_e_o_pedido_fica(self) -> None:
        for frase in ("yet", "yeah no", "guess", "jess", "yes sir change it"):
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                self.assertEqual(confirmacao.responder(frase).estado, "pendente")
                self.assertTrue(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(self.falas[-1], "Say yes to send, or abort.")

    def test_sim_claro_envia_exatamente_o_recap(self) -> None:
        for frase in ("yes", "Uh, yes.", "yes, send it."):
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the configuration module."))
                recap = confirmacao.recap
                desfecho = confirmacao.responder(frase)
                self.assertEqual(desfecho.estado, "executado")
                self.assertEqual(self.canal.recebidos, [recap.pedido])
                self.assertEqual(recap.pedido.prompt, "Add tests to the configuration module.")

    def test_cancelar_mal_ouvido_cancela_sem_enviar(self) -> None:
        for frase in ("Uh castle.", "cancer", "can sell", "Castle", "Um, cancel it.", "cancela isso", "cancelo"):
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                self.assertEqual(confirmacao.responder(frase).estado, "cancelado")
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(self.falas[-1], "Cancelled, nothing was sent.")

    def test_aproximacao_nunca_transforma_correcao_ou_acrescento_em_cancelar(self) -> None:
        casos = {
            "change castle to docs": "corrigir",
            "add cancel": "acrescentar",
            "add castle": "acrescentar",
            "muda cancer para castle": "corrigir",
        }
        for frase, tipo in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase)[0], tipo)

    def test_frases_que_nao_soam_a_cancelar_nao_cancelam(self) -> None:
        for frase in ("close", "kings", "maybe", "castle in the sky tonight", "yes", "send", "sure", "okay"):
            with self.subTest(frase=frase):
                self.assertNotEqual(classificar_resposta(frase)[0], "cancelar")

    def test_nome_de_projeto_parecido_com_cancelar_responde_a_qual_projeto(self) -> None:
        self.relogio = RelogioFalso()
        config = Config(
            microfone="Microfone Ficticio",
            projetos=(Projeto("consul", Path("D:/caminho/para/consul")), Projeto("atlas", Path("D:/caminho/para/atlas"))),
            ouvido=ConfigOuvido(lingua="en"),
            interprete=ConfigInterprete(),
        )
        interprete = Interprete(config, cliente=LlmFalso())
        canal = CanalFalso()
        confirmacao = Confirmacao(interprete, canal, falar=lambda _t: None, mostrar=lambda _t: None, relogio=self.relogio)
        confirmacao.iniciar(self.pedido("ditar_prompt", projeto=None))
        self.assertTrue(confirmacao.recap.falta_projeto)
        self.assertEqual(confirmacao.responder("consul").estado, "pendente")
        self.assertEqual(confirmacao.recap.pedido.projeto, "consul")
        self.assertEqual(confirmacao.responder("cancel").estado, "cancelado")
        self.assertEqual(canal.recebidos, [])


# --- Recap: fala curta, ecra completo, texto exato ----------------------------


class TestRespostasClarasDeConfirmar(Base):
    lingua = "en"

    def test_cada_frase_da_lista_envia_depois_de_tirar_hesitacoes_e_pontuacao(self) -> None:
        for frase in (
            "yes", "yeah", "yep", "yup", "sure", "send", "send it", "go", "go ahead", "confirm", "do it",
            "ok send it", "okay send it", "sim", "envia", "manda", "confirma",
            "Yeah.", "Uh, yep.", "Go ahead.", "OK, send it.", "Okay, send it.", "Manda.", "Confirma.",
            "Hmm, sure!", "Do it.", "Yup...", "hey jarvis, go ahead",
        ):
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase), ("confirmar", ""))

    def test_quase_confirmacoes_nunca_enviam(self) -> None:
        for frase in (
            "yeah but change X", "yes but ...", "go to atlas", "yellow", "surely not", "send it to atlas and",
            "yeah sure thing", "go go go now", "ok", "okay", "do it later", "manda ao orbita", "confirma depois", "goat",
            "yeah, no", "sure thing", "send it now to atlas",
        ):
            with self.subTest(frase=frase):
                self.assertNotEqual(classificar_resposta(frase)[0], "confirmar")

    def test_cancelar_continua_tolerante_e_corrigir_continua_a_corrigir(self) -> None:
        for frase, tipo in (
            ("Uh cancel.", "cancelar"),
            ("Uh castle.", "cancelar"),
            ("no, change X to Y", "corrigir"),
            ("yeah but change tests to docs", "corrigir"),
        ):
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase)[0], tipo)
        self.assertEqual(classificar_resposta("yeah but change tests to docs")[1], "change tests to docs")

    def test_yeah_ao_recap_envia_uma_vez_exatamente_o_recap(self) -> None:
        confirmacao = self.montar()
        recap = confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module.")).recap
        self.assertEqual(confirmacao.responder("Yeah.").estado, "executado")
        self.assertEqual(self.canal.recebidos, [recap.pedido])
        self.assertEqual(confirmacao.responder("Yeah.").estado, "sem_pedido")
        self.assertEqual(len(self.canal.recebidos), 1)


class TestPrazoDepoisDaFala(Base):
    """O prazo conta a partir do fim da voz, nao de quando o recap foi gerado."""

    lingua = "en"
    DURACAO_DA_FALA_S = 5.0

    def montar_com_fala_lenta(self, **ajustes) -> Confirmacao:
        confirmacao = self.montar(**ajustes)
        self.fins_da_fala: list[float] = []

        def falar(texto: str) -> None:
            self.falas.append(texto)
            self.relogio.avancar(self.DURACAO_DA_FALA_S)
            self.fins_da_fala.append(self.relogio())

        confirmacao._falar = falar
        return confirmacao

    def test_prazo_do_recap_comeca_no_fim_da_voz_e_usa_confirmacao_s(self) -> None:
        confirmacao = self.montar_com_fala_lenta(confirmacao_s=12.0)
        gerado_em = self.relogio()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        fim_da_fala = self.fins_da_fala[-1]
        self.assertEqual(fim_da_fala, gerado_em + self.DURACAO_DA_FALA_S)
        self.assertEqual(confirmacao._prazo, fim_da_fala + 12.0)
        self.assertEqual(confirmacao.prazo_restante(), 12.0)
        self.relogio.avancar(11.9)
        self.assertIsNone(confirmacao.verificar_tempo())
        self.relogio.avancar(0.1)
        self.assertEqual(confirmacao.verificar_tempo().estado, "expirado")
        self.assertEqual(self.canal.recebidos, [])
        self.assertFalse(confirmacao.a_espera)

    def test_prazo_da_pergunta_repetida_comeca_no_fim_da_voz(self) -> None:
        confirmacao = self.montar_com_fala_lenta()
        self.assertEqual(confirmacao.limite_s, 30.0)
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.relogio.avancar(20.0)
        desfecho = confirmacao.responder("maybe")
        self.assertEqual((desfecho.estado, desfecho.motivo), ("pendente", "resposta nao percebida"))
        self.assertEqual(self.falas[-1], "Say yes to send, or abort.")
        fim_da_fala = self.fins_da_fala[-1]
        self.assertEqual(confirmacao._prazo, fim_da_fala + 30.0)
        self.assertEqual(confirmacao.prazo_restante(), 30.0)
        self.relogio.avancar(29.9)
        self.assertIsNone(confirmacao.verificar_tempo())
        self.relogio.avancar(0.1)
        self.assertEqual(confirmacao.verificar_tempo().estado, "expirado")
        self.assertEqual(self.canal.recebidos, [])

    def test_durante_a_pergunta_repetida_nada_conta_nem_expira(self) -> None:
        confirmacao = self.montar_com_fala_lenta(confirmacao_s=4.0)
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        durante: list[tuple[str | None, float | None]] = []
        falar = confirmacao._falar

        def falar_e_espreitar(texto: str) -> None:
            falar(texto)
            # A voz demorou mais do que o prazo: o pedido nao expira a meio.
            durante.append((getattr(confirmacao.verificar_tempo(), "estado", None), confirmacao.prazo_restante()))
            durante.append((confirmacao.responder("yes").estado, None))

        confirmacao._falar = falar_e_espreitar
        confirmacao.responder("maybe")
        self.assertEqual(durante, [(None, 4.0), ("ignorado", None)])
        self.assertTrue(confirmacao.a_espera)
        self.assertEqual(self.canal.recebidos, [])

    def test_silencio_ate_ao_prazo_cancela_sem_enviar_e_diz_porque(self) -> None:
        confirmacao = self.montar_com_fala_lenta()
        confirmacao.iniciar(self.pedido("lancar_run"))
        self.relogio.avancar(30.0)
        desfecho = confirmacao.verificar_tempo()
        self.assertEqual(desfecho.estado, "expirado")
        self.assertEqual(self.falas[-1], "No answer, so I cancelled. Nothing was sent.")
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(confirmacao.responder("yes").estado, "sem_pedido")

    def test_resposta_comecada_antes_do_prazo_conta_mesmo_que_chegue_depois(self) -> None:
        confirmacao = self.montar_com_fala_lenta(confirmacao_s=10.0)
        recap = confirmacao.iniciar(self.pedido("ditar_prompt")).recap
        self.relogio.avancar(9.5)
        dito_em = self.relogio()
        self.relogio.avancar(2.0)  # acabou de falar e foi transcrita depois do prazo
        desfecho = confirmacao.responder("Yeah.", dito_em=dito_em)
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(self.canal.recebidos, [recap.pedido])

    def test_resposta_comecada_antes_do_prazo_tambem_cancela_ou_pergunta_de_novo(self) -> None:
        for frase, estado in (("Uh cancel.", "cancelado"), ("maybe", "pendente")):
            with self.subTest(frase=frase):
                confirmacao = self.montar_com_fala_lenta(confirmacao_s=10.0)
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                self.relogio.avancar(9.9)
                dito_em = self.relogio()
                self.relogio.avancar(1.0)
                self.assertEqual(confirmacao.responder(frase, dito_em=dito_em).estado, estado)
                self.assertEqual(self.canal.recebidos, [])

    def test_resposta_comecada_depois_do_prazo_expira(self) -> None:
        for dito_depois in (0.0, 0.5):
            with self.subTest(dito_depois=dito_depois):
                confirmacao = self.montar_com_fala_lenta(confirmacao_s=10.0)
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                self.relogio.avancar(10.0 + dito_depois)
                dito_em = self.relogio()
                self.relogio.avancar(1.0)
                self.assertEqual(confirmacao.responder("yes", dito_em=dito_em).estado, "expirado")
                self.assertEqual(self.canal.recebidos, [])


class TestRespostaVaziaEIgnorada(Base):
    lingua = "en"
    VAZIAS = ("", "   ", ".", "...", "?!", "Uh.", "uh", "Hmm...", "Um, uh.", None)

    def test_ruido_nao_envia_nao_fala_nao_conta_e_nao_mexe_no_prazo(self) -> None:
        for texto in self.VAZIAS:
            with self.subTest(texto=texto):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt"))
                self.relogio.avancar(7.0)
                falas = list(self.falas)
                prazo = confirmacao._prazo
                desfecho = confirmacao.responder(texto)
                self.assertEqual((desfecho.estado, desfecho.motivo), ("ignorado", "resposta vazia"))
                self.assertIsNotNone(desfecho.recap)
                self.assertEqual(self.falas, falas, "o ruido nao faz o jarvis falar")
                self.assertEqual(confirmacao._prazo, prazo)
                self.assertEqual(confirmacao._falhas, 0)
                self.assertTrue(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])

    def test_ruido_repetido_nao_gasta_as_tentativas_e_um_sim_depois_envia(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        for _ in range(TENTATIVAS - 1):
            self.assertEqual(confirmacao.responder("maybe").estado, "pendente")
        for texto in self.VAZIAS:
            self.assertEqual(confirmacao.responder(texto).estado, "ignorado")
        self.assertEqual(confirmacao.responder("Yeah.").estado, "executado")
        self.assertEqual(len(self.canal.recebidos), 1)

    def test_ruido_nao_impede_o_prazo_de_expirar(self) -> None:
        confirmacao = self.montar(confirmacao_s=10.0)
        confirmacao.iniciar(self.pedido("ditar_prompt"))
        self.relogio.avancar(9.0)
        self.assertEqual(confirmacao.responder("Uh.").estado, "ignorado")
        self.relogio.avancar(1.0)
        self.assertEqual(confirmacao.verificar_tempo().estado, "expirado")
        self.assertEqual(self.canal.recebidos, [])

    def test_dialogo_com_ruido_pelo_meio_continua_a_ouvir(self) -> None:
        confirmacao = self.montar()
        ouvido = OuvidoFalso(self.relogio, [("Uh.", 2.0), (".", 3.0), ("Yeah.", 4.0)])
        desfecho = confirmacao.dialogar(self.pedido("abrir_pasta", "orbita"), ouvido)
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("abrir_pasta", "orbita", "")])
        self.assertEqual(len(self.falas), 1, "so o recap foi dito; o ruido nao teve resposta")


class TestRecap(Base):
    LONGO = (
        "Refaz o ecrã de login para usar o novo componente de formulário. Mantém a validação do email "
        "e da palavra-passe como está. Acrescenta uma mensagem de erro clara quando o servidor não "
        "responde, e escreve testes para os três casos: sucesso, credenciais erradas e servidor em baixo."
    )

    def test_recap_falado_le_as_frases_do_prompt_e_acaba_na_pergunta(self) -> None:
        prompts = ("Corrige os testes.", "Corrige os testes. Depois corre a suite! Está bem?", self.LONGO, "Versão 1.5 do x; e depois y")
        for lingua in ("pt", "en"):
            for intencao in sorted(INTENCOES_COM_EFEITO):
                for projeto in ("atlas", None):
                    for prompt in prompts:
                        interpretacao = self.pedido(intencao, projeto, prompt)
                        recap = compor_recap(1, interpretacao, lingua)
                        with self.subTest(lingua=lingua, intencao=intencao, projeto=projeto, prompt=prompt[:20]):
                            # Um prompt curto e dito com as frases dele; um longo, o pedido que falta o
                            # projeto ou um sem prompt ficam numa so frase.
                            curto = recap.pedido.prompt and prompt != self.LONGO and not recap.falta_projeto
                            maximo = contar_frases(prompt) if curto else 1
                            self.assertLessEqual(contar_frases(recap.fala), maximo, recap.fala)
                            self.assertTrue(recap.fala.endswith("?"))
                            if curto:
                                self.assertIn(recap.pedido.prompt.rstrip(".!?"), recap.fala)

    def test_recap_diz_as_frases_como_estao_no_prompt(self) -> None:
        interpretacao = self.pedido(
            "ditar_prompt", "chamora", "Read the README and summarize it in Portuguese. Don't change anything."
        )
        recap = compor_recap(1, interpretacao, "en")
        self.assertEqual(
            recap.fala, "For chamora: Read the README and summarize it in Portuguese. Don't change anything - send it?"
        )
        self.assertNotIn(", Don't", recap.fala)
        self.assertIn("Read the README and summarize it in Portuguese. Don't change anything.", recap.ecra)

    def test_resumo_do_prompt_longo_nao_junta_frases_com_virgula(self) -> None:
        longo = (
            "Fix the login. Then rewrite the form so that it uses the new component and keeps the email and "
            "password validation exactly as it is today, and add a clear error message when the server does not answer."
        )
        recap = compor_recap(1, self.pedido("ditar_prompt", "atlas", longo), "en")
        self.assertEqual(recap.fala, "Fix the login, for atlas, the full text is on screen - send it?")
        self.assertNotIn("login, Then", recap.fala)
        self.assertIn(longo, recap.ecra)

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
        self.assertEqual(self.falas[-1], "Corrige os testes do login, para o atlas - envio?")

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
        self.assertEqual(desfecho.recap.fala, "Para que projeto é: atlas, orbita, kanban-lite ou outro?")

    def test_acoes_sem_prompt_dizem_a_acao_e_o_projeto(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("parar_run", "orbita"))
        self.assertEqual(self.falas[-1], "Parar o run do orbita - confirmas?")


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
        for respostas in ([], [(None, 0)], [("sim", 35.0)]):
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

    def test_confirmacoes_aceites_sao_so_as_da_lista_fechada(self) -> None:
        palavras = {palavra for frase in PALAVRAS_DE_CONFIRMAR | PALAVRAS_DE_CORTESIA for palavra in frase.split()}
        self.assertEqual(
            palavras,
            {
                "sim", "envia", "isso", "manda", "confirma", "yes", "yeah", "yep", "yup", "sure", "send", "it",
                "go", "ahead", "confirm", "do", "ok", "okay", "please", "por", "favor",
            },
        )
        for frase in FRASES_DE_CONFIRMAR:
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase), ("confirmar", ""))
        for cortesia in PALAVRAS_DE_CORTESIA:
            with self.subTest(cortesia=cortesia):
                self.assertNotEqual(classificar_resposta(cortesia)[0], "confirmar", "cortesia sozinha nunca envia")



# --- Respostas reais ao recap: abort/cancel primeiro, "sim" exato --------------

#: Transcricoes reais (e variantes de "abort") que tem de cancelar.
CANCELAMENTOS_OUVIDOS = (
    "Can't sell it.", "No, can't sell it.", "Castle Castle.", "Uh castle.", "Uh cancel.", "abort", "abort it",
    "a board", "aboard", "a bored", "abored", "uh abort", "cancel",
)
#: Combinacoes claras de confirmar e cortesia que enviam.
CONFIRMACOES_COMBINADAS = ("Go, yes.", "yes please", "yes, send it", "yeah go ahead", "ok yes", "yes yes")
#: Respostas que confirmam e cancelam ao mesmo tempo, ou com outro conteudo.
RESPOSTAS_AMBIGUAS = (
    "yes abort", "yes cancel", "send it no cancel", "abort yes", "yes, abort it", "cancel, send it",
    "yeah, cancel", "yes please abort", "yes send it now", "ok yes to atlas", "yes buy it",
)


class TestAbortECancelarAntesDaRegraFinanceira(Base):
    lingua = "en"

    def montar_com_espia(self) -> Confirmacao:
        confirmacao = self.montar()
        self.correcoes: list[tuple] = []
        corrigir = self.interprete.corrigir

        def espia(*args, **kwargs):
            self.correcoes.append(args)
            return corrigir(*args, **kwargs)

        self.interprete.corrigir = espia
        return confirmacao

    def test_cada_cancelamento_ouvido_cancela_sem_enviar_nem_recusar(self) -> None:
        for frase in CANCELAMENTOS_OUVIDOS:
            with self.subTest(frase=frase):
                confirmacao = self.montar_com_espia()
                confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module."))
                desfecho = confirmacao.responder(frase)
                self.assertEqual(desfecho.estado, "cancelado")
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(self.correcoes, [], "o cancelar nunca passa pelas correcoes")
                self.assertEqual(self.llm.pedidos, [])
                self.assertEqual(self.falas[-1], "Cancelled, nothing was sent.")
                self.assertNotIn("recusad", " ".join(self.ecra))

    def test_cancelar_e_classificado_antes_das_correcoes(self) -> None:
        for frase in CANCELAMENTOS_OUVIDOS:
            with self.subTest(frase=frase):
                self.assertEqual(classificar_resposta(frase), ("cancelar", ""))

    def test_pedidos_e_correcoes_financeiras_continuam_recusados(self) -> None:
        confirmacao = self.montar_com_espia()
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module."))
        desfecho = confirmacao.responder("no, change tests to buy 10 shares")
        self.assertEqual(desfecho.estado, "recusado")
        self.assertEqual(len(self.correcoes), 1)
        self.assertEqual(self.canal.recebidos, [])
        self.assertFalse(confirmacao.a_espera)
        interprete = Interprete(_config("en"), cliente=LlmFalso())
        self.assertEqual(interprete.interpretar("tell atlas to sell my shares").intencao, INTENCAO_RECUSADA)

    def test_combinacoes_claras_enviam_exatamente_o_recap(self) -> None:
        for frase in CONFIRMACOES_COMBINADAS:
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                recap = confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module.")).recap
                self.assertEqual(confirmacao.responder(frase).estado, "executado")
                self.assertEqual(self.canal.recebidos, [recap.pedido])

    def test_confirmar_e_cancelar_juntos_ou_outro_conteudo_pergunta_de_novo(self) -> None:
        for frase in RESPOSTAS_AMBIGUAS:
            with self.subTest(frase=frase):
                confirmacao = self.montar()
                confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module."))
                desfecho = confirmacao.responder(frase)
                self.assertEqual(desfecho.estado, "pendente")
                self.assertTrue(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(self.falas[-1], "Say yes to send, or abort.")

    def test_aproximacao_nunca_confirma(self) -> None:
        for frase in ("yess", "yea please", "yes pls", "go yess", "send et", "yez yes", "ok", "please", "ok please"):
            with self.subTest(frase=frase):
                self.assertNotEqual(classificar_resposta(frase)[0], "confirmar")

    def test_correcao_e_acrescento_continuam_a_dar_recap_novo(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Add tests to the signup module.")])
        confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module."))
        desfecho = confirmacao.responder("no, change login to signup")
        self.assertEqual(desfecho.estado, "pendente")
        self.assertIn("signup", desfecho.recap.pedido.prompt)
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(classificar_resposta("add that it is urgent")[0], "acrescentar")

    def test_ecra_de_ajuda_nomeia_yes_abort_e_as_correcoes(self) -> None:
        for lingua, esperados in (
            ("en", ('"yes"', '"abort"', '"cancel"', '"no, change X to Y"', '"add ..."')),
            ("pt", ('"sim"', '"aborta"', '"cancela"', '"não, muda X para Y"', '"acrescenta ..."')),
        ):
            with self.subTest(lingua=lingua):
                confirmacao = self.montar(lingua=lingua)
                confirmacao.iniciar(self.pedido("ditar_prompt", prompt="Add tests to the login module."))
                ecra = "\n".join(self.ecra)
                for esperado in esperados:
                    self.assertIn(esperado, ecra)
                confirmacao.responder("purple elephants")
                self.assertEqual(
                    self.falas[-1], {"en": "Say yes to send, or abort.", "pt": "Diz sim para enviar, ou aborta."}[lingua]
                )


class TestEstadoERelatorioSemConfirmacao(Base):
    lingua = "en"

    def test_estado_e_relatorio_com_projeto_correm_logo(self) -> None:
        for intencao in ("estado", "ler_relatorio"):
            with self.subTest(intencao=intencao):
                confirmacao = self.montar()
                desfecho = confirmacao.iniciar(self.pedido(intencao, "atlas"))
                self.assertEqual(desfecho.estado, "executado")
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [Pedido(intencao, "atlas", "")])
                self.assertFalse(any("Confirm?" in fala or "Send it?" in fala for fala in self.falas))

    def test_what_is_the_status_of_jarvis_corre_logo(self) -> None:
        llm = LlmFalso([_llm("estado", "jarvis")])
        config = Config(
            microfone="Microfone Ficticio",
            projetos=(Projeto("jarvis", Path("D:/caminho/para/jarvis")),),
            ouvido=ConfigOuvido(lingua="en"),
            interprete=ConfigInterprete(),
        )
        interprete = Interprete(config, cliente=llm)
        canal = CanalFalso()
        falas: list[str] = []
        confirmacao = Confirmacao(interprete, canal, falar=falas.append, mostrar=lambda _t: None, relogio=RelogioFalso())
        interpretacao = interprete.interpretar("What is the status of Jarvis?")
        self.assertEqual((interpretacao.intencao, interpretacao.projeto), ("estado", "jarvis"))
        desfecho = confirmacao.iniciar(interpretacao)
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(canal.recebidos, [Pedido("estado", "jarvis", "")])
        self.assertFalse(any("Confirm?" in fala for fala in falas))

    def test_sem_projeto_pergunta_qual_e_corre_logo_que_e_dito(self) -> None:
        for intencao in ("estado", "ler_relatorio"):
            with self.subTest(intencao=intencao):
                confirmacao = self.montar()
                desfecho = confirmacao.iniciar(self.pedido(intencao, None))
                self.assertEqual(desfecho.estado, "pendente")
                self.assertTrue(desfecho.recap.falta_projeto)
                self.assertTrue(self.falas[-1].startswith("Which project is it for: "))
                self.assertNotIn("Send it?", self.falas[-1])
                self.assertEqual(self.canal.recebidos, [])
                desfecho = confirmacao.responder("atlas")
                self.assertEqual(desfecho.estado, "executado")
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [Pedido(intencao, "atlas", "")])

    def test_runs_continuam_a_pedir_recap_e_sim(self) -> None:
        for intencao in ("lancar_run", "retomar_run", "parar_run"):
            with self.subTest(intencao=intencao):
                confirmacao = self.montar()
                extra = {"prompt": "Fix a typo in the README."} if intencao == "lancar_run" else {}
                desfecho = confirmacao.iniciar(self.pedido(intencao, "atlas", **extra))
                self.assertEqual(desfecho.estado, "pendente")
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(confirmacao.responder("yes").estado, "executado")
                self.assertEqual(len(self.canal.recebidos), 1)

    def test_runs_sem_projeto_pedem_o_sim_depois_do_projeto(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("parar_run", None))
        self.assertEqual(confirmacao.responder("atlas").estado, "pendente")
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("parar_run", "atlas", "")])


class TestProjetoPeloSomNaResposta(Base):
    lingua = "en"

    def montar_com_chamora(self, respostas_llm) -> Confirmacao:
        self.relogio = RelogioFalso()
        self.llm = LlmFalso(respostas_llm)
        config = Config(
            microfone="Microfone Ficticio",
            projetos=tuple(
                Projeto(nome, Path("D:/caminho/para") / nome) for nome in ("seekai", "jarvis", "chamora", "atlas")
            ),
            ouvido=ConfigOuvido(lingua="en"),
            interprete=ConfigInterprete(),
        )
        self.interprete = Interprete(config, cliente=self.llm)
        self.canal = CanalFalso()
        self.falas: list[str] = []
        self.confirmacao = Confirmacao(
            self.interprete, self.canal, falar=self.falas.append, mostrar=lambda _t: None, relogio=self.relogio
        )
        return self.confirmacao

    def test_resposta_a_which_project_pelo_som_completa_o_pedido(self) -> None:
        for resposta in ("Shamura.", "The project is Chamara.", "Chamara. Chamura. Chamura.", "Mara Chamura."):
            with self.subTest(resposta=resposta):
                confirmacao = self.montar_com_chamora([_llm("ditar_prompt", "", "List the tests.")])
                interpretacao = self.interprete.interpretar("list the tests")
                self.assertIsNone(interpretacao.projeto)
                desfecho = confirmacao.iniciar(interpretacao)
                self.assertTrue(desfecho.recap.falta_projeto)
                self.assertTrue(self.falas[-1].startswith("Which project is it for: "))
                novo = confirmacao.responder(resposta)
                self.assertEqual(novo.estado, "pendente")
                self.assertFalse(novo.recap.falta_projeto)
                self.assertEqual(novo.recap.pedido, Pedido("ditar_prompt", "chamora", "List the tests."))
                # Nada sai sem o sim.
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(confirmacao.responder("yes").estado, "executado")
                self.assertEqual(self.canal.recebidos, [Pedido("ditar_prompt", "chamora", "List the tests.")])

    def test_resposta_com_dois_nomes_continua_a_perguntar(self) -> None:
        for resposta in ("Shamura or seekai.", "Shamara or atlas."):
            with self.subTest(resposta=resposta):
                confirmacao = self.montar_com_chamora([_llm("ditar_prompt", "", "List the tests.")])
                confirmacao.iniciar(self.interprete.interpretar("list the tests"))
                self.assertEqual(confirmacao.responder(resposta).estado, "pendente")
                self.assertTrue(confirmacao.recap.falta_projeto)
                self.assertEqual(self.canal.recebidos, [])

    def test_estado_sem_projeto_corre_com_o_nome_pelo_som(self) -> None:
        confirmacao = self.montar_com_chamora([])
        confirmacao.iniciar(self.pedido("estado", None))
        self.assertTrue(self.falas[-1].startswith("Which project is it for: "))
        self.assertEqual(confirmacao.responder("Shamra.").estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("estado", "chamora", "")])


PROMPT_DO_README = "Read the README and summarize it. Don't change anything."
ACRESCENTO_DITO = "Uh no, add uh one more uh request. I want to s uh the summarize to be in Portuguese."
PROMPT_EM_PORTUGUES = "Read the README and summarize it in Portuguese. Don't change anything."
CORRECAO_DITA = "The no change uh um the login screen to Wipstone"


class TestCorrecoesDitasNoRecap(TestProjetoPeloSomNaResposta):
    """Correcoes e acrescentos reais ao recap: reescritos limpos, nada enviado sem o sim."""

    # Os testes herdados ja correm na classe de cima.
    test_resposta_a_which_project_pelo_som_completa_o_pedido = None
    test_resposta_com_dois_nomes_continua_a_perguntar = None
    test_estado_sem_projeto_corre_com_o_nome_pelo_som = None

    def test_classifica_o_acrescento_e_a_correcao_com_hesitacoes(self) -> None:
        self.assertEqual(classificar_resposta(ACRESCENTO_DITO)[0], "acrescentar")
        self.assertEqual(classificar_resposta(CORRECAO_DITA)[0], "corrigir")
        # Sem pedido pendente, tambem e uma correcao.
        self.assertTrue(e_correcao(CORRECAO_DITA, ("seekai", "jarvis", "chamora", "atlas")))
        self.assertTrue(e_correcao("Uh no uh change tests to docs", ()))
        self.assertFalse(e_correcao("The login screen is broken", ()))
        # Um artigo sem negacao nem hesitacao comeca uma frase nova.
        self.assertFalse(e_correcao("The change log for version two is wrong", ()))
        self.assertEqual(classificar_resposta("The change log for version two is wrong")[0], "outro")
        self.assertTrue(e_correcao("The uh change tests to docs", ()))
        # Em portugues "um" logo depois do verbo e o artigo, nao uma hesitacao.
        self.assertTrue(e_correcao("muda um para dois", ()))
        self.assertTrue(e_correcao("não, muda um teste para dois", ()))

    def test_acrescento_reescrito_e_recap_com_as_frases_do_prompt(self) -> None:
        confirmacao = self.montar_com_chamora([_llm("ditar_prompt", "chamora", PROMPT_EM_PORTUGUES)])
        confirmacao.iniciar(self.pedido("ditar_prompt", "chamora", PROMPT_DO_README))
        desfecho = confirmacao.responder(ACRESCENTO_DITO)
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(len(self.llm.pedidos), 1, "o acrescento passou pelo LLM")
        self.assertEqual(confirmacao.recap.pedido, Pedido("ditar_prompt", "chamora", PROMPT_EM_PORTUGUES))
        self.assertEqual(
            self.falas[-1], "For chamora: Read the README and summarize it in Portuguese. Don't change anything - send it?"
        )
        self.assertEqual(self.canal.recebidos, [])
        self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("ditar_prompt", "chamora", PROMPT_EM_PORTUGUES)])

    def test_acrescento_colado_ou_sem_llm_nao_muda_o_pedido(self) -> None:
        colado = "Read the README and summarize it. Don't change anything, no, add uh one more uh request, I want to s u"
        for respostas in ([_llm("ditar_prompt", "chamora", colado)], []):
            with self.subTest(respostas=bool(respostas)):
                confirmacao = self.montar_com_chamora(respostas)
                if not respostas:
                    self.llm.erro = MotorIndisponivel("Ollama indisponivel")
                confirmacao.iniciar(self.pedido("ditar_prompt", "chamora", PROMPT_DO_README))
                confirmacao.responder(ACRESCENTO_DITO)
                self.assertEqual(confirmacao.recap.pedido.prompt, PROMPT_DO_README)
                self.assertTrue(self.falas[-1].startswith("I couldn't apply that change; the request stays the same."))
                self.assertNotIn(" uh", " ".join(self.falas))
                self.assertEqual(self.canal.recebidos, [])

    def test_correcao_com_hesitacoes_e_ordem_solta_corrige_sem_enviar(self) -> None:
        confirmacao = self.montar_com_chamora([_llm("ditar_prompt", "chamora", "Fix the Wipstone. Don't change the tests.")])
        confirmacao.iniciar(self.pedido("ditar_prompt", "chamora", "Fix the login screen. Don't change the tests."))
        desfecho = confirmacao.responder(CORRECAO_DITA)
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(confirmacao.recap.pedido.prompt, "Fix the Wipstone. Don't change the tests.")
        self.assertEqual(self.falas[-1], "For chamora: Fix the Wipstone. Don't change the tests - send it?")
        self.assertEqual(self.canal.recebidos, [])

    def test_regra_financeira_na_correcao_nunca_envia(self) -> None:
        for resposta, respostas_llm in (
            ("Uh no, add uh buy some bitcoin.", []),
            (ACRESCENTO_DITO, [_llm("ditar_prompt", "chamora", "Read the README and sell the shares. Don't change anything.")]),
        ):
            with self.subTest(resposta=resposta):
                confirmacao = self.montar_com_chamora(respostas_llm)
                confirmacao.iniciar(self.pedido("ditar_prompt", "chamora", PROMPT_DO_README))
                confirmacao.responder(resposta)
                confirmacao.responder("yes")
                self.assertEqual(self.canal.recebidos, [])


PERGUNTA_DO_CHANGELOG = "Ask me if the change log should mention the new option and wait for my answer."


class TestConversaSemProjeto(TestProjetoPeloSomNaResposta):
    def conversa_sem_projeto(self) -> Confirmacao:
        confirmacao = self.montar_com_chamora([_llm("conversa", "", PERGUNTA_DO_CHANGELOG)])
        interpretacao = self.interprete.interpretar(
            "Tell it to ask me if the change log should mention the new option and wait for my answer."
        )
        self.assertEqual((interpretacao.intencao, interpretacao.projeto), ("conversa", None))
        self.confirmacao_prompt = interpretacao.prompt
        desfecho = confirmacao.iniciar(interpretacao)
        self.assertEqual(desfecho.estado, "pendente")
        self.assertTrue(desfecho.recap.falta_projeto)
        return confirmacao

    def test_fica_pendente_e_pergunta_which_project(self) -> None:
        self.conversa_sem_projeto()
        self.assertEqual(self.falas, ["Which project is it for: seekai, jarvis, chamora, atlas or another one?"])
        self.assertEqual(self.canal.recebidos, [])

    def test_a_resposta_completa_o_pedido_e_segue_o_recap_normal(self) -> None:
        confirmacao = self.conversa_sem_projeto()
        novo = confirmacao.responder("The project is Jarvis.")
        self.assertEqual(novo.estado, "pendente")
        self.assertFalse(novo.recap.falta_projeto)
        prompt = self.confirmacao_prompt
        self.assertEqual(novo.recap.pedido, Pedido("conversa", "jarvis", prompt))
        self.assertEqual(self.falas[-1], f"Reply to Claude in jarvis: {prompt.rstrip('.')} - send it?")
        self.assertEqual(len(self.llm.pedidos), 1, "a resposta nao e uma frase nova")
        self.assertEqual(self.canal.recebidos, [], "nada sai sem o sim")
        self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("conversa", "jarvis", prompt)])

    def test_sem_projeto_nem_o_sim_envia_e_abortar_cancela(self) -> None:
        confirmacao = self.conversa_sem_projeto()
        self.assertEqual(confirmacao.responder("yes").estado, "pendente")
        self.assertTrue(confirmacao.recap.falta_projeto)
        self.assertEqual(self.falas[-1], "Which project is it for: seekai, jarvis, chamora, atlas or another one?")
        self.assertEqual(confirmacao.responder("abort").estado, "cancelado")
        self.assertEqual(self.canal.recebidos, [])

    def test_resposta_financeira_a_which_project_nao_envia(self) -> None:
        confirmacao = self.conversa_sem_projeto()
        desfecho = confirmacao.responder("The project is Jarvis, and buy 100 euros of bitcoin.")
        self.assertEqual(desfecho.estado, "recusado")
        self.assertFalse(confirmacao.a_espera)
        self.assertIn("money and trading", self.falas[-1])
        confirmacao.responder("yes")
        self.assertEqual(self.canal.recebidos, [])

    def test_em_portugues_pergunta_que_projeto(self) -> None:
        confirmacao = self.montar(
            [_llm("conversa", "", "Pergunta-me se o changelog deve falar da opção nova.")], lingua="pt"
        )
        interpretacao = self.interprete.interpretar("diz-lhe para me perguntar se o changelog deve falar da opção nova")
        self.assertEqual((interpretacao.intencao, interpretacao.projeto), ("conversa", None))
        desfecho = confirmacao.iniciar(interpretacao)
        self.assertTrue(desfecho.recap.falta_projeto)
        self.assertEqual(self.falas[-1], "Para que projeto é: atlas, orbita, kanban-lite ou outro?")
        novo = confirmacao.responder("atlas")
        self.assertEqual(novo.recap.pedido.projeto, "atlas")
        self.assertTrue(self.falas[-1].startswith("Responder ao Claude no atlas: "), self.falas[-1])
        self.assertEqual(self.canal.recebidos, [])


# --- Pergunta pelo projeto com os projetos conhecidos e descobertos --------------


class TestPerguntaPelosProjetosConhecidos(Base):
    lingua = "en"

    def montar_com(self, config: Config, respostas_llm=None) -> Confirmacao:
        self.relogio = RelogioFalso()
        self.llm = LlmFalso(respostas_llm)
        self.interprete = Interprete(config, cliente=self.llm)
        self.canal = CanalFalso()
        self.falas: list[str] = []
        self.ecra: list[str] = []
        self.confirmacao = Confirmacao(
            self.interprete, self.canal, falar=self.falas.append, mostrar=self.ecra.append, relogio=self.relogio
        )
        return self.confirmacao

    @staticmethod
    def config_com(*nomes: str) -> Config:
        return Config(
            microfone="Microfone Ficticio",
            projetos=tuple(Projeto(nome, Path("D:/caminho/para") / nome) for nome in nomes),
            ouvido=ConfigOuvido(lingua="en"),
            interprete=ConfigInterprete(),
        )

    def test_diz_ate_quatro_projetos_e_mostra_todos_no_ecra(self) -> None:
        nomes = ("jarvis", "forja", "chamora", "atlas", "orbita", "nimbus")
        confirmacao = self.montar_com(self.config_com(*nomes))
        desfecho = confirmacao.iniciar(self.pedido("ditar_prompt", None, "List the tests."))
        self.assertTrue(desfecho.recap.falta_projeto)
        self.assertEqual(self.falas, ["Which project is it for: jarvis, forja, chamora, atlas or another one?"])
        self.assertIn("Known projects: " + ", ".join(nomes), self.ecra[-1])
        self.assertFalse(any("I got the request" in fala for fala in self.falas))
        # A pergunta repetida depois de uma resposta que nao se percebe e a mesma.
        confirmacao.responder("hmm what")
        self.assertEqual(self.falas[-1], self.falas[0])
        self.assertEqual(self.canal.recebidos, [])

    def test_os_usados_ha_menos_tempo_vem_primeiro(self) -> None:
        confirmacao = self.montar_com(self.config_com("jarvis", "forja", "chamora", "atlas", "orbita"))
        for projeto in ("atlas", "orbita"):
            confirmacao.iniciar(self.pedido("parar_run", projeto))
            self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.assertEqual(confirmacao.projetos_por_uso(), ("orbita", "atlas", "jarvis", "forja", "chamora"))
        # Passado o tempo do ultimo projeto, o ditado sem projeto pergunta qual.
        self.relogio.avancar(ConfigInterprete().ultimo_projeto_min * 60 + 1)
        confirmacao.iniciar(self.pedido("ditar_prompt", None, "List the tests."))
        self.assertEqual(self.falas[-1], "Which project is it for: orbita, atlas, jarvis, forja or another one?")

    def test_um_pedido_cancelado_ou_que_falhou_nao_conta_como_usado(self) -> None:
        confirmacao = self.montar_com(self.config_com("jarvis", "atlas"))
        confirmacao.iniciar(self.pedido("parar_run", "atlas"))
        confirmacao.responder("abort")
        self.canal.falhar = True
        confirmacao.iniciar(self.pedido("parar_run", "atlas"))
        self.assertEqual(confirmacao.responder("yes").estado, "falhou")
        self.assertEqual(confirmacao.projetos_por_uso(), ("jarvis", "atlas"))

    def test_projetos_ditos_em_alternativa_continuam_na_pergunta_do_interprete(self) -> None:
        confirmacao = self.montar_com(
            self.config_com("jarvis", "forja", "atlas"), [_llm("ditar_prompt", "", "List the tests.")]
        )
        confirmacao.iniciar(self.interprete.interpretar("list the tests in atlas or forja"))
        self.assertEqual(self.falas[-1], "Which project: forja or atlas?")

    def test_sem_projetos_pergunta_sem_lista(self) -> None:
        confirmacao = self.montar_com(self.config_com())
        confirmacao.iniciar(self.pedido("ditar_prompt", None, "List the tests."))
        self.assertEqual(self.falas, ["Which project is it for?"])

    def test_projeto_descoberto_e_aceite_e_so_segue_com_o_sim(self) -> None:
        from jarvis.config import ConfigDescoberta
        from jarvis.projetos import com_projetos_descobertos

        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta) / "Repositorios"
            (raiz / "chamora" / ".git").mkdir(parents=True)
            configurada = replace(self.config_com("atlas"), descoberta=ConfigDescoberta(pastas=(raiz,)))
            config = com_projetos_descobertos(configurada, registar=lambda _linha: None)
        self.assertIn("chamora", [projeto.nome for projeto in config.projetos])
        confirmacao = self.montar_com(config, [_llm("ditar_prompt", "", "List the tests.")])
        interpretacao = self.interprete.interpretar("list the tests")
        self.assertIsNone(interpretacao.projeto)
        confirmacao.iniciar(interpretacao)
        self.assertIn("chamora", self.falas[-1])
        novo = confirmacao.responder("Shamura.")
        self.assertEqual(novo.estado, "pendente")
        self.assertEqual(novo.recap.pedido, Pedido("ditar_prompt", "chamora", "List the tests."))
        self.assertIn("chamora", self.falas[-1])
        self.assertEqual(self.falas[-1], "List the tests, for chamora - send it?")
        self.assertEqual(self.canal.recebidos, [], "nada sai sem o sim")
        self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.assertEqual(self.canal.recebidos, [Pedido("ditar_prompt", "chamora", "List the tests.")])


# --- Factos do caderno da memoria -------------------------------------------------


class TestFactosDaMemoria(Base):
    lingua = "en"

    def facto(self, intencao: str = INTENCAO_LEMBRAR_FACTO, texto: str = "My favourite team is Benfica.") -> Interpretacao:
        return Interpretacao("frase", intencao, None, texto, "regra", "teste")

    def test_guardar_um_facto_tem_recap_e_so_corre_depois_do_sim(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(self.facto())
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(self.falas, ["Remember: My favourite team is Benfica - save it?"])
        self.assertIn("Fact to save:", self.ecra[-1])
        self.assertEqual(self.canal.recebidos, [])
        self.relogio.avancar(1)
        desfecho = confirmacao.responder("yes")
        self.assertEqual(desfecho.estado, "executado")
        self.assertEqual(
            self.canal.recebidos, [Pedido(INTENCAO_LEMBRAR_FACTO, None, "My favourite team is Benfica.")]
        )

    def test_apagar_um_facto_tem_recap(self) -> None:
        confirmacao = self.montar(lingua="pt")
        confirmacao.iniciar(self.facto(INTENCAO_ESQUECER_FACTO, "Moro em Braga."))
        self.assertEqual(self.falas, ["Esquecer: Moro em Braga - apago?"])
        self.assertIn("Facto a apagar:", self.ecra[-1])

    def test_abort_e_prazo_nao_correm_nada(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.facto())
        self.relogio.avancar(1)
        self.assertEqual(confirmacao.responder("abort").estado, "cancelado")
        self.assertEqual(self.falas[-1], "Cancelled, my memory is unchanged.")
        confirmacao.iniciar(self.facto())
        self.relogio.avancar(ESPERA_DA_CONFIRMACAO_S + 1)
        self.assertEqual(confirmacao.verificar_tempo().estado, "expirado")
        self.assertEqual(self.falas[-1], "No answer, so I cancelled. My memory is unchanged.")
        self.assertEqual(self.canal.recebidos, [])

    def test_resposta_que_nao_se_percebe_pede_sim_ou_aborta(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.facto())
        self.relogio.avancar(1)
        confirmacao.responder("banana")
        self.assertEqual(self.falas[-1], "Say yes to confirm, or abort.")

    def test_correcao_nunca_vai_ao_llm(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.facto())
        self.relogio.avancar(1)
        desfecho = confirmacao.responder("no, change Benfica to Porto")
        self.assertEqual(desfecho.estado, "pendente")
        self.assertEqual(self.llm.pedidos, [])
        self.assertEqual(desfecho.recap.pedido.prompt, "My favourite team is Benfica.")
        self.assertEqual(self.canal.recebidos, [])

    def test_facto_vazio_nao_faz_recap(self) -> None:
        confirmacao = self.montar()
        self.assertEqual(confirmacao.iniciar(self.facto(texto="  ")).estado, "nao_percebido")
        self.assertEqual(self.canal.recebidos, [])

    def test_ficam_fora_do_esquema_do_llm(self) -> None:
        for intencao in INTENCOES_DA_MEMORIA:
            self.assertNotIn(intencao, INTENCOES)


# --- Recap leve e projeto assumido ---------------------------------------------


class TestRecapLeve(Base):
    lingua = "en"

    def test_prompt_curto_e_uma_frase_que_acaba_numa_pergunta_leve(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(self.pedido("ditar_prompt", "atlas", "Add tests to the login page."))
        self.assertEqual(self.falas, ["Add tests to the login page, for atlas - send it?"])
        self.assertEqual(contar_frases(self.falas[0]), 1)
        linhas = self.ecra[-1].splitlines()
        self.assertIn("Text to send:", linhas)
        self.assertIn("Add tests to the login page.", linhas)
        self.assertEqual(desfecho.recap.pergunta, "Send it?")
        confirmacao.responder("yes")
        self.assertEqual(self.canal.recebidos[0].prompt, "Add tests to the login page.")

    def test_prompt_longo_diz_o_tema_e_que_o_texto_esta_no_ecra(self) -> None:
        longo = (
            "Refactor the whole authentication module so that the session tokens are refreshed in the "
            "background without blocking the interface, and keep every existing test passing."
        )
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(self.pedido("ditar_prompt", "atlas", longo))
        fala = self.falas[-1]
        self.assertEqual(fala, "Refactor the whole authentication module, for atlas, the full text is on screen - send it?")
        self.assertEqual(contar_frases(fala), 1)
        self.assertNotIn("existing test", fala)
        self.assertIn(longo, self.ecra[-1].splitlines(), "o texto exato esta sempre no ecra")
        confirmacao.responder("yes")
        self.assertEqual(self.canal.recebidos, [desfecho.recap.pedido])
        self.assertEqual(self.canal.recebidos[0].prompt, longo)

    def test_o_tema_nunca_acaba_numa_palavra_de_ligacao(self) -> None:
        sem_oracao = "Please go through every single file in the source folder and the tests folder and tidy up all of the imports today"
        recap = compor_recap(1, self.pedido("ditar_prompt", "atlas", sem_oracao), "en")
        self.assertEqual(
            recap.fala, "Please go through every single file, for atlas, the full text is on screen - send it?"
        )
        primeira_curta = "Fix the login. " + " ".join(["Then check the form carefully."] * 5)
        recap = compor_recap(1, self.pedido("ditar_prompt", "atlas", primeira_curta), "en")
        self.assertTrue(recap.fala.startswith("Fix the login, for atlas, the full text is on screen"), recap.fala)

    def test_o_texto_enviado_e_sempre_o_mostrado_no_ecra(self) -> None:
        prompts = (
            "Add tests.",
            "Fix the login. Don't change anything.",
            "Why is the build slow?",
            " ".join(["word"] * 40) + ".",
        )
        for prompt in prompts:
            for intencao in ("ditar_prompt", "conversa", "lancar_run"):
                with self.subTest(prompt=prompt[:15], intencao=intencao):
                    confirmacao = self.montar()
                    desfecho = confirmacao.iniciar(self.pedido(intencao, "atlas", prompt))
                    self.assertTrue(self.falas[-1].endswith(" - send it?"), self.falas[-1])
                    confirmacao.responder("yes")
                    enviado = self.canal.recebidos[0]
                    self.assertEqual(enviado, desfecho.recap.pedido)
                    self.assertIn(enviado.prompt, self.ecra[0].splitlines())

    def test_acoes_sem_texto_perguntam_se_avanca(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("parar_run", "orbita"))
        self.assertEqual(self.falas[-1], "Stop the run in orbita - go ahead?")
        confirmacao.responder("go ahead")
        self.assertEqual(self.canal.recebidos, [Pedido("parar_run", "orbita", "")])

    def test_o_vocabulario_do_sim_nao_mudou(self) -> None:
        # "okay" sozinho continua a ser so cortesia: nunca envia.
        confirmacao = self.montar()
        for resposta in ("okay", "ok", "please", "yes abort", "yes but cancel", "yes but add docs", "sure, but later"):
            with self.subTest(resposta=resposta):
                if not confirmacao.a_espera:
                    confirmacao.iniciar(self.pedido("ditar_prompt", "atlas", "Add tests."))
                self.assertNotEqual(confirmacao.responder(resposta).estado, "executado")
                self.assertEqual(self.canal.recebidos, [])


class TestProjetoAssumido(Base):
    lingua = "en"

    def usar(self, confirmacao: Confirmacao, projeto: str = "atlas") -> None:
        """Um ditado enviado ao `projeto`: passa a ser o ultimo usado."""
        confirmacao.iniciar(self.pedido("ditar_prompt", projeto, "List the tests."))
        self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.canal.recebidos.clear()
        self.falas.clear()

    @staticmethod
    def ditado_sem_projeto(prompt: str = "Add tests to the login page.") -> Interpretacao:
        return Interpretacao(
            "add tests to the login page", "ditar_prompt", None, prompt, "llm", "teste", pergunta="Which project?"
        )

    def montar_com_nomes(self, nomes: tuple[str, ...]) -> Confirmacao:
        confirmacao = self.montar()
        config = replace(
            self.interprete.config,
            projetos=tuple(Projeto(nome, Path("D:/caminho/para") / nome) for nome in nomes),
        )
        self.interprete = Interprete(config, cliente=self.llm)
        confirmacao.interprete = self.interprete
        return confirmacao

    def test_ditado_sem_projeto_assume_o_ultimo_e_diz_o_no_recap(self) -> None:
        confirmacao = self.montar()
        self.usar(confirmacao)
        self.relogio.avancar(9 * 60)
        desfecho = confirmacao.iniciar(self.ditado_sem_projeto())
        self.assertEqual(desfecho.estado, "pendente")
        self.assertTrue(desfecho.recap.projeto_assumido)
        self.assertEqual(self.falas, ["Add tests to the login page, still for atlas - send it?"])
        self.assertIn("last one used", self.ecra[-1])
        self.assertIn("assumido o ultimo usado, atlas", "\n".join(self.ecra))
        self.assertEqual(self.canal.recebidos, [], "nada sai sem o sim")
        self.relogio.avancar(5)
        self.assertEqual(confirmacao.responder("yes").estado, "executado")
        self.assertEqual(
            self.canal.recebidos,
            [Pedido("ditar_prompt", "atlas", "Add tests to the login page.", projeto_assumido=True)],
        )

    def test_so_o_sim_envia_ao_projeto_assumido(self) -> None:
        for resposta in ("okay", "hmm", "yes abort", "yes, for orbita", "yes but for orbita", "for orbita and delete it"):
            with self.subTest(resposta=resposta):
                confirmacao = self.montar()
                self.usar(confirmacao)
                confirmacao.iniciar(self.ditado_sem_projeto())
                desfecho = confirmacao.responder(resposta)
                self.assertNotEqual(desfecho.estado, "executado")
                self.assertEqual(self.canal.recebidos, [])
                if confirmacao.a_espera:
                    self.assertEqual(confirmacao.recap.pedido.projeto, "atlas", "o projeto nao mudou")

    def test_no_for_outro_projeto_troca_e_recapitula_sem_enviar(self) -> None:
        for resposta in ("No, for orbita.", "no orbita", "for orbita", "Uh, no, to the orbita.", "no, in orbita please"):
            with self.subTest(resposta=resposta):
                confirmacao = self.montar()
                self.usar(confirmacao)
                confirmacao.iniciar(self.ditado_sem_projeto())
                desfecho = confirmacao.responder(resposta)
                self.assertEqual(desfecho.estado, "pendente")
                self.assertEqual(desfecho.recap.pedido.projeto, "orbita")
                self.assertFalse(desfecho.recap.projeto_assumido)
                self.assertEqual(self.falas[-1], "Add tests to the login page, for orbita - send it?")
                self.assertEqual(self.llm.pedidos, [], "a troca nao passa pelo LLM")
                self.assertEqual(self.canal.recebidos, [])
                self.assertEqual(confirmacao.responder("yes").estado, "executado")
                self.assertEqual(
                    self.canal.recebidos, [Pedido("ditar_prompt", "orbita", "Add tests to the login page.")]
                )

    def test_nao_para_o_outro_em_portugues(self) -> None:
        confirmacao = self.montar(lingua="pt")
        self.usar(confirmacao)
        confirmacao.iniciar(self.ditado_sem_projeto("Acrescenta testes ao login."))
        self.assertEqual(self.falas[-1], "Acrescenta testes ao login, outra vez para o atlas - envio?")
        desfecho = confirmacao.responder("Não, para o orbita.")
        self.assertEqual(desfecho.recap.pedido.projeto, "orbita")
        self.assertEqual(self.falas[-1], "Acrescenta testes ao login, para o orbita - envio?")
        self.assertEqual(self.canal.recebidos, [])

    def test_no_sozinho_ou_abort_cancela(self) -> None:
        for resposta in ("no", "abort", "Uh castle."):
            with self.subTest(resposta=resposta):
                confirmacao = self.montar()
                self.usar(confirmacao)
                confirmacao.iniciar(self.ditado_sem_projeto())
                self.assertEqual(confirmacao.responder(resposta).estado, "cancelado")
                self.assertEqual(self.canal.recebidos, [])

    def test_passado_o_tempo_pergunta_o_projeto(self) -> None:
        confirmacao = self.montar()
        self.usar(confirmacao)
        self.relogio.avancar(10 * 60)
        self.assertEqual(confirmacao.ultimo_projeto(), "atlas", "10 minutos ainda contam")
        self.relogio.avancar(1)
        desfecho = confirmacao.iniciar(self.ditado_sem_projeto())
        self.assertTrue(desfecho.recap.falta_projeto)
        self.assertFalse(desfecho.recap.projeto_assumido)
        self.assertTrue(self.falas[-1].startswith("Which project is it for"), self.falas[-1])

    def test_tempo_configuravel_e_zero_desliga(self) -> None:
        confirmacao = self.montar(ultimo_projeto_min=2.0)
        self.assertEqual(confirmacao.ultimo_projeto_s, 120.0)
        self.usar(confirmacao)
        self.relogio.avancar(121)
        self.assertTrue(confirmacao.iniciar(self.ditado_sem_projeto()).recap.falta_projeto)
        confirmacao = self.montar(ultimo_projeto_min=0)
        self.usar(confirmacao)
        self.assertTrue(confirmacao.iniciar(self.ditado_sem_projeto()).recap.falta_projeto)

    def test_sem_nenhum_pedido_executado_pergunta(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt", "atlas", "List the tests."))
        confirmacao.responder("abort")
        self.canal.falhar = True
        confirmacao.iniciar(self.pedido("ditar_prompt", "orbita", "List the tests."))
        self.assertEqual(confirmacao.responder("yes").estado, "falhou")
        self.assertIsNone(confirmacao.ultimo_projeto(), "cancelado ou falhado nao conta como usado")
        self.assertTrue(confirmacao.iniciar(self.ditado_sem_projeto()).recap.falta_projeto)

    def test_o_ultimo_usado_e_o_mais_recente(self) -> None:
        confirmacao = self.montar()
        self.usar(confirmacao, "atlas")
        self.usar(confirmacao, "orbita")
        confirmacao.iniciar(self.ditado_sem_projeto())
        self.assertEqual(self.falas[-1], "Add tests to the login page, still for orbita - send it?")

    def test_um_projeto_dito_mesmo_mal_ouvido_ou_em_alternativa_nunca_e_substituido(self) -> None:
        confirmacao = self.montar()
        self.usar(confirmacao, "atlas")
        alternativa = Interpretacao(
            "add tests in orbita or kanban-lite",
            "ditar_prompt",
            None,
            "Add tests.",
            "llm",
            "teste",
            pergunta="Which project: orbita or kanban-lite?",
        )
        confirmacao.iniciar(alternativa)
        self.assertEqual(self.falas[-1], "Which project: orbita or kanban-lite?")
        dito = Interpretacao("add tests to orbita", "ditar_prompt", None, "Add tests.", "llm", "teste")
        self.assertTrue(confirmacao.iniciar(dito).recap.falta_projeto)

    def test_a_palavra_de_ativacao_nao_e_o_projeto_jarvis(self) -> None:
        confirmacao = self.montar_com_nomes(("jarvis", "atlas"))
        self.usar(confirmacao, "atlas")
        frase = Interpretacao("Hey Jarvis, add tests.", "ditar_prompt", None, "Add tests.", "llm", "teste")
        self.assertEqual(confirmacao.iniciar(frase).recap.pedido.projeto, "atlas")

    def test_um_projeto_que_ja_nao_existe_nao_e_assumido(self) -> None:
        confirmacao = self.montar()
        self.usar(confirmacao, "atlas")
        config = replace(self.interprete.config, projetos=(Projeto("orbita", Path("D:/caminho/para/orbita")),))
        confirmacao.interprete = Interprete(config, cliente=self.llm)
        self.assertIsNone(confirmacao.ultimo_projeto())

    def test_so_o_ditado_e_as_leituras_assumem(self) -> None:
        for intencao in ("abrir_editor", "abrir_pasta", "lancar_run", "retomar_run", "parar_run"):
            with self.subTest(intencao=intencao):
                confirmacao = self.montar()
                self.usar(confirmacao)
                desfecho = confirmacao.iniciar(self.pedido(intencao, None, "Migrate the tests."))
                self.assertTrue(desfecho.recap.falta_projeto)
                self.assertEqual(self.canal.recebidos, [])

    def test_estado_sem_projeto_usa_o_ultimo_e_corre_logo(self) -> None:
        for intencao in sorted(INTENCOES_SO_DE_LEITURA):
            with self.subTest(intencao=intencao):
                confirmacao = self.montar()
                self.usar(confirmacao, "orbita")
                desfecho = confirmacao.iniciar(Interpretacao("what's the status", intencao, None, "", "llm", "teste"))
                self.assertEqual(desfecho.estado, "executado")
                self.assertEqual(self.canal.recebidos, [Pedido(intencao, "orbita", "", projeto_assumido=True)])
                self.assertEqual(self.falas, [], "a resposta e da leitura, que diz o projeto")

    def test_estado_sem_projeto_recente_continua_a_perguntar(self) -> None:
        confirmacao = self.montar()
        desfecho = confirmacao.iniciar(Interpretacao("what's the status", "estado", None, "", "llm", "teste"))
        self.assertTrue(desfecho.recap.falta_projeto)
        self.assertEqual(self.canal.recebidos, [])

    def test_a_correcao_mantem_o_projeto_assumido(self) -> None:
        confirmacao = self.montar([_llm("ditar_prompt", "atlas", "Add tests to the settings page.")])
        self.usar(confirmacao)
        confirmacao.iniciar(self.ditado_sem_projeto())
        desfecho = confirmacao.responder("no, change login to settings")
        self.assertEqual(desfecho.recap.pedido.projeto, "atlas")
        self.assertTrue(desfecho.recap.projeto_assumido)
        self.assertEqual(self.falas[-1], "Add tests to the settings page, still for atlas - send it?")


class TestNuncaRecapDeDinheiro(Base):
    lingua = "en"

    def test_um_ditado_de_dinheiro_sem_projeto_nunca_assume_nem_recapitula(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt", "atlas", "List the tests."))
        confirmacao.responder("yes")
        self.canal.recebidos.clear()
        for texto, prompt in (("buy 10 shares of tesla", "Buy 10 shares of Tesla."), ("add tests", "Sell my bitcoin.")):
            with self.subTest(texto=texto):
                desfecho = confirmacao.iniciar(Interpretacao(texto, "ditar_prompt", None, prompt, "llm", "teste"))
                self.assertEqual(desfecho.estado, "recusado")
                self.assertIsNone(desfecho.recap)
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])
                self.assertNotIn("send it?", self.falas[-1])

    def test_nenhuma_intencao_com_texto_de_dinheiro_chega_a_um_recap(self) -> None:
        for intencao in sorted(INTENCOES_COM_EFEITO - INTENCOES_SO_DE_LEITURA):
            with self.subTest(intencao=intencao):
                confirmacao = self.montar()
                interpretacao = replace(
                    self.pedido(intencao, "atlas", "Buy 10 shares of Tesla."), texto="buy 10 shares of tesla"
                )
                desfecho = confirmacao.iniciar(interpretacao)
                self.assertEqual(desfecho.estado, "recusado")
                self.assertFalse(confirmacao.a_espera)
                self.assertEqual(self.canal.recebidos, [])

    def test_a_troca_de_projeto_com_dinheiro_nao_envia(self) -> None:
        confirmacao = self.montar()
        confirmacao.iniciar(self.pedido("ditar_prompt", "atlas", "List the tests."))
        confirmacao.responder("yes")
        self.canal.recebidos.clear()
        confirmacao.iniciar(Interpretacao("add tests", "ditar_prompt", None, "Add tests.", "llm", "teste"))
        desfecho = confirmacao.responder("no, for orbita and buy shares")
        self.assertNotEqual(desfecho.estado, "executado")
        self.assertNotEqual(desfecho.estado, "pendente", desfecho)
        self.assertEqual(self.canal.recebidos, [])


class TestConfigDoUltimoProjeto(unittest.TestCase):
    def test_por_omissao_10_minutos_e_validado_no_config_toml(self) -> None:
        self.assertEqual(ConfigInterprete().ultimo_projeto_min, 10.0)
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

            self.assertEqual(carregar("ultimo_projeto_min = 5").interprete.ultimo_projeto_min, 5.0)
            self.assertEqual(carregar("ultimo_projeto_min = 0").interprete.ultimo_projeto_min, 0.0)
            for invalido in ("-1", "241", "true", '"10"', "nan", "inf"):
                with self.subTest(valor=invalido), self.assertRaises(ConfigError):
                    carregar(f"ultimo_projeto_min = {invalido}")

    def test_o_exemplo_documenta_a_chave(self) -> None:
        exemplo = (RAIZ / "config.exemplo.toml").read_text(encoding="utf-8")
        self.assertIn("ultimo_projeto_min = 10.0", exemplo)


if __name__ == "__main__":
    unittest.main()
