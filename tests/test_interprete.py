r"""Testes do interprete (jarvis/interprete.py) e do seu avaliador, unittest.

Nenhum teste fala com o Ollama real nem toca som. O LLM e um cliente falso
em memoria ou um servidor HTTP falso em 127.0.0.1 (porta aleatoria), que
responde bem, devagar, com JSON invalido ou fora do esquema. A configuracao
e sempre ficticia, com os projetos do golden set.

Corre com:

    .venv\Scripts\python -m unittest tests.test_interprete -v
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from jarvis import interprete as interprete_mod
from jarvis.config import (
    LIMITE_DO_INTERPRETE_S,
    Config,
    ConfigError,
    ConfigInterprete,
    ConfigOuvido,
    Projeto,
    carregar_config,
    validar_url_local,
)
from jarvis.interprete import (
    INTENCAO_RECUSADA,
    INTENCOES,
    ClienteOllama,
    Interpretacao,
    Interprete,
    MotorIndisponivel,
    Vram,
    decidir_modelo,
    pedido_financeiro,
    projetos_em_alternativa,
    projetos_mencionados,
    validar_resposta_do_llm,
)

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
import avaliar_interprete as avaliador  # noqa: E402

NOMES = avaliador.PROJETOS_DO_GOLDEN
MIB = 1024 * 1024


def _config(lingua: str = "pt", **ajustes) -> Config:
    return Config(
        microfone="Microfone Ficticio",
        projetos=tuple(Projeto(nome, Path("D:/caminho/para") / nome) for nome in NOMES),
        ouvido=ConfigOuvido(lingua=lingua),
        interprete=ConfigInterprete(**ajustes),
    )


class ClienteFalso:
    """Faz de Ollama em memoria: devolve respostas feitas e regista pedidos."""

    def __init__(self, respostas=None, *, instalados=None, carregados=None, erro=None) -> None:
        self.respostas = list(respostas or [])
        self.pedidos: list[tuple[str, list[dict]]] = []
        self.instalados = instalados if instalados is not None else {"qwen3:8b": 5200 * MIB}
        self.carregados = carregados if carregados is not None else {}
        self.descarregados: list[str] = []
        self.erro = erro
        self.limite_s = LIMITE_DO_INTERPRETE_S

    def conversar(self, modelo, mensagens, esquema, *, limite_s=None):
        self.pedidos.append((modelo, mensagens))
        if self.erro is not None:
            raise self.erro
        resposta = self.respostas.pop(0) if self.respostas else {}
        if callable(resposta):
            resposta = resposta(modelo)
        return resposta if isinstance(resposta, str) else json.dumps(resposta)

    def modelos_instalados(self, limite_s=None):
        return dict(self.instalados)

    def modelos_carregados(self, limite_s=None):
        return dict(self.carregados)

    def descarregar(self, modelo, limite_s=None):
        self.descarregados.append(modelo)


def _llm(intencao: str, projeto: str = "", prompt: str = "", financeiro: bool = False) -> dict:
    return {"intencao": intencao, "projeto": projeto, "prompt": prompt, "financeiro": financeiro}


def _interprete(respostas=None, lingua: str = "pt", **kwargs) -> tuple[Interprete, ClienteFalso]:
    cliente = ClienteFalso(respostas, **kwargs)
    return Interprete(_config(lingua), cliente=cliente), cliente


# --- Esquema e lista fechada ----------------------------------------------------


class TestEsquemaEListaFechada(unittest.TestCase):
    def test_lista_fechada_de_intencoes(self) -> None:
        self.assertEqual(
            INTENCOES,
            (
                "ditar_prompt", "estado", "ler_relatorio", "lancar_run", "retomar_run", "parar_run",
                "conversa", "horas", "abrir_editor", "abrir_pasta", "calar", "dormir", "acordar",
                "desconhecido",
            ),
        )
        self.assertNotIn(INTENCAO_RECUSADA, INTENCOES)

    def test_esquema_enviado_ao_llm_prende_intencao_e_projeto(self) -> None:
        esquema = interprete_mod._esquema(NOMES)
        self.assertEqual(esquema["properties"]["intencao"]["enum"], list(INTENCOES))
        self.assertEqual(esquema["properties"]["projeto"]["enum"], [*NOMES, ""])
        self.assertEqual(esquema["properties"]["financeiro"], {"type": "boolean"})
        self.assertEqual(set(esquema["required"]), {"intencao", "projeto", "prompt", "financeiro"})

    def test_resposta_valida(self) -> None:
        self.assertEqual(
            validar_resposta_do_llm(json.dumps(_llm("ditar_prompt", "atlas", "Corrige  o\nteste.")), NOMES),
            ("ditar_prompt", "atlas", "Corrige o teste.", False),
        )
        self.assertEqual(validar_resposta_do_llm(json.dumps(_llm("horas")), NOMES), ("horas", None, "", False))
        self.assertEqual(
            validar_resposta_do_llm(json.dumps(_llm("ditar_prompt", "", "x", financeiro=True)), NOMES),
            ("ditar_prompt", None, "x", True),
        )

    def test_respostas_fora_do_esquema_sao_recusadas(self) -> None:
        invalidas = [
            "nao e json",
            "[1, 2]",
            json.dumps(_llm("comprar_acoes")),
            json.dumps(_llm(INTENCAO_RECUSADA)),
            json.dumps(_llm("ditar_prompt", "projeto-inventado", "x")),
            json.dumps({"intencao": "horas", "projeto": "", "prompt": 3, "financeiro": False}),
            json.dumps({"projeto": "", "prompt": "", "financeiro": False}),
            json.dumps({"intencao": "horas", "projeto": "", "prompt": ""}),
            json.dumps({"intencao": "horas", "projeto": "", "prompt": "", "financeiro": "false"}),
            json.dumps({"intencao": "horas", "projeto": "", "prompt": "", "financeiro": 0}),
            json.dumps({"intencao": "horas", "projeto": "", "prompt": "", "financeiro": None}),
            json.dumps(_llm("ditar_prompt", "atlas", "x" * 3000)),
        ]
        for conteudo in invalidas:
            with self.subTest(conteudo=conteudo[:40]):
                with self.assertRaises(MotorIndisponivel):
                    validar_resposta_do_llm(conteudo, NOMES)

    def test_texto_do_utilizador_vai_como_dados_sem_a_palavra_de_ativacao(self) -> None:
        interprete, cliente = _interprete([_llm("ditar_prompt", "atlas", "Corrige o login.")])
        resultado = interprete.interpretar("Hey Jarvis, no atlas corrige o login")
        modelo, mensagens = cliente.pedidos[0]
        self.assertEqual(modelo, "qwen3:8b")
        self.assertEqual(mensagens[0]["role"], "system")
        self.assertEqual(mensagens[1], {"role": "user", "content": "no atlas corrige o login"})
        self.assertEqual(resultado.texto, "Hey Jarvis, no atlas corrige o login")
        self.assertEqual((resultado.intencao, resultado.projeto, resultado.prompt), ("ditar_prompt", "atlas", "Corrige o login."))
        self.assertEqual(resultado.origem, "llm")
        self.assertFalse(resultado.so_confirmacao)

    def test_so_a_palavra_de_ativacao_e_desconhecido_sem_llm(self) -> None:
        interprete, cliente = _interprete()
        for texto in ("hey jarvis", "Boas Jarvis!", "   ", ""):
            with self.subTest(texto=texto):
                resultado = interprete.interpretar(texto)
                self.assertEqual(resultado.intencao, "desconhecido")
                self.assertTrue(resultado.so_confirmacao)
        self.assertEqual(cliente.pedidos, [])

    def test_caracteres_de_controlo_nunca_chegam_ao_prompt(self) -> None:
        interprete, cliente = _interprete([_llm("ditar_prompt", "atlas", "linha um\r\nlinha dois\x00")])
        resultado = interprete.interpretar("no atlas\r\n faz\x1b isto")
        self.assertEqual(cliente.pedidos[0][1][1]["content"], "no atlas faz isto")
        self.assertEqual(resultado.prompt, "linha um linha dois")


# --- Projeto nunca adivinhado -------------------------------------------------------


class TestProjetoNuncaAdivinhado(unittest.TestCase):
    def test_projeto_dito_com_erros_tipicos_do_stt(self) -> None:
        casos = {
            "no kanban lite corrige o menu": ("kanban-lite",),
            "no kanbanlite corrige o menu": ("kanban-lite",),
            "no orbit a corrige o menu": ("orbita",),
            "in bolsa radar fix the chart": ("bolsa-radar",),
            "no exemplo corrige o menu": (),
            "no orbitas corrige": ("orbita",),  # um erro de escrita numa palavra longa
            "no orbe corrige": (),
            "no galaxia corrige": (),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(projetos_mencionados(texto, NOMES), esperado)

    def test_projeto_do_llm_que_nao_foi_dito_e_ignorado_e_pergunta(self) -> None:
        interprete, _ = _interprete([_llm("ditar_prompt", "atlas", "Corrige o menu.")])
        resultado = interprete.interpretar("corrige o menu")
        self.assertIsNone(resultado.projeto)
        self.assertEqual(resultado.pergunta, "Para que projeto?")
        self.assertIn("nao foi dito", resultado.motivo)

    def test_sem_projeto_pergunta_na_lingua_do_utilizador(self) -> None:
        interprete, _ = _interprete([_llm("estado")], lingua="en")
        resultado = interprete.interpretar("how is the run going")
        self.assertIsNone(resultado.projeto)
        self.assertEqual(resultado.pergunta, "Which project?")

    def test_projetos_em_alternativa_perguntam_qual(self) -> None:
        self.assertEqual(projetos_em_alternativa("no atlas ou no orbita atualiza o node", NOMES), ("atlas", "orbita"))
        self.assertEqual(projetos_em_alternativa("in nimbus and kanban-lite check lint", NOMES), ("kanban-lite", "nimbus"))
        self.assertEqual(projetos_em_alternativa("no atlas copia o que fizemos no orbita", NOMES), ())
        interprete, _ = _interprete([_llm("ditar_prompt", "atlas", "Atualiza o node.")])
        resultado = interprete.interpretar("no atlas ou no orbita atualiza o node")
        self.assertIsNone(resultado.projeto)
        self.assertEqual(resultado.pergunta, "Qual projeto: atlas ou orbita?")

    def test_alvo_claro_com_outro_projeto_no_conteudo(self) -> None:
        interprete, _ = _interprete([_llm("ditar_prompt", "atlas", "Copia a validação do orbita.")])
        resultado = interprete.interpretar("no atlas copia a validação de email que fizemos no orbita")
        self.assertEqual(resultado.projeto, "atlas")
        self.assertIsNone(resultado.pergunta)

    def test_llm_sem_projeto_mas_um_so_dito_na_frase(self) -> None:
        interprete, _ = _interprete([_llm("retomar_run")])
        self.assertEqual(interprete.interpretar("retoma o run do nimbus").projeto, "nimbus")

    def test_intencao_sem_projeto_nunca_leva_projeto(self) -> None:
        interprete, _ = _interprete([_llm("horas", "atlas")])
        resultado = interprete.interpretar("olha diz la as horas no atlas")
        self.assertEqual(resultado.intencao, "horas")
        self.assertIsNone(resultado.projeto)
        self.assertIsNone(resultado.pergunta)


# --- Regra financeira ----------------------------------------------------------------

FINANCEIRAS = (
    "compra-me duas ações da empresa agora",
    "vende as minhas ações todas",
    "quanto está o bitcoin hoje",
    "põe uma ordem de venda a cem euros",
    "investe cinquenta euros num fundo",
    "transfere vinte euros para a minha conta poupança",
    "abre uma conta na corretora",
    "como está a bolsa hoje",
    "no bolsa-radar compra mais ações quando o preço descer",
    "buy me two shares of that company now",
    "sell all my stocks",
    "how much is bitcoin today",
    "put a limit order at one hundred dollars",
    "invest fifty euros in a fund",
    "send twenty euros to my savings account",
    "move my crypto to a new wallet",
    "in bolsa-radar buy more shares when the price drops",
    "manda vinte euros para a minha conta poupança",
    "envia cem euros ao joão",
    "mandei 50 euros ao senhorio, confirma",
    "adquire duas ações da empresa",
    "põe dez ações da empresa na carteira",
    "acquire five shares of that company",
    "place an order for ten shares",
    "acquire the company's stock for me",
    # dinheiro aplicado sem verbo de compra
    "coloca mil euros em ações da tesla",
    "no atlas coloca mil euros em ações da tesla",
    "no bolsa-radar coloca mil euros em ações da tesla",
    "mete quinhentos euros num fundo",
    "põe cem euros em bitcoin",
    "aplica dois mil euros em etfs",
    "aplica mil euros na tesla",
    "mete tudo em ações da tesla",
    "mete 1 000 euros em obrigações do tesouro",
    "coloca cento e vinte euros no fundo de investimento",
    "put a thousand dollars into tesla shares",
    "in atlas put a thousand dollars into tesla shares",
    "put everything into tesla shares",
    "put 500 bucks on tesla",
    "place a thousand euros in gold",
    "invest a grand in apple shares",
    "go long on tesla",
    "long tesla stock",
    "short the market",
)

NAO_FINANCEIRAS = (
    "abre o vs code no bolsa-radar",
    "open the editor in bolsa radar",
    "no bolsa-radar corrige o gráfico de preços que aparece vazio",
    "no kanban-lite corrige as ações do menu de contexto",
    "adiciona criptografia às passwords",
    "in nimbus add cryptography to the tokens",
    "in nimbus fix the typo about trade-offs in the design doc",
    "no atlas revê o módulo de faturas",
    "que horas são",
    "comprime as imagens do atlas",
    "investiga porque é que o build falha",
    "investigate why the build fails",
    "adquire o lock antes de escrever no ficheiro",
    "acquire the mutex before writing the file",
    "no kanban-lite adiciona duas ações ao menu de contexto",
    "no atlas as três ações do formulário não respondem",
    "envia o relatório ao joão",
    "send the report to the team",
    "acquire the lock before writing",
    "no kanban-lite corrige as ações do menu de contexto",
    "no atlas muda a cor de fundo da página",
    "põe o botão no fundo do ecrã",
    "coloca o preço a 20 euros no botão",
    "no atlas o preço aparece em euros",
    "no atlas aplica 20 euros de desconto no carrinho",
    "put a euro sign next to the price",
    "copy the backup into the network shares",
    "save it in the shares folder",
    "in nimbus add a short delay",
)


class TestRegraFinanceira(unittest.TestCase):
    def test_pedidos_financeiros_sao_recusados_antes_do_llm(self) -> None:
        for texto in FINANCEIRAS:
            with self.subTest(texto=texto):
                interprete, cliente = _interprete([_llm("ditar_prompt", "atlas", texto)])
                resultado = interprete.interpretar(texto)
                self.assertEqual(resultado.intencao, INTENCAO_RECUSADA)
                self.assertEqual(resultado.prompt, "")
                self.assertIsNone(resultado.projeto)
                self.assertFalse(resultado.pode_dispensar_confirmacao)
                self.assertEqual(cliente.pedidos, [], "o LLM nunca e chamado para um pedido financeiro")

    def test_prompt_financeiro_vindo_do_llm_e_recusado_depois(self) -> None:
        for prompt in ("Compra ações da empresa.", "Buy ten shares.", "Invest in stocks for me."):
            with self.subTest(prompt=prompt):
                interprete, cliente = _interprete([_llm("ditar_prompt", "atlas", prompt)])
                resultado = interprete.interpretar("no atlas trata daquilo que falamos")
                self.assertEqual(len(cliente.pedidos), 1)
                self.assertEqual(resultado.intencao, INTENCAO_RECUSADA)
                self.assertEqual(resultado.prompt, "")
                self.assertIn("depois do LLM", resultado.motivo)

    def test_marca_financeira_do_llm_recusa_o_que_a_regra_deixou_passar(self) -> None:
        # Uma frase sem vocabulario da lista: so a marca do LLM a apanha.
        texto = "no atlas trata daquilo da tesla com o dinheiro das ferias"
        self.assertIsNone(pedido_financeiro(texto, NOMES))
        for intencao in ("ditar_prompt", "conversa", "lancar_run", "desconhecido", "abrir_pasta"):
            with self.subTest(intencao=intencao):
                interprete, cliente = _interprete(
                    [_llm(intencao, "atlas", "Trata daquilo da Tesla.", financeiro=True)]
                )
                resultado = interprete.interpretar(texto)
                self.assertEqual(len(cliente.pedidos), 1)
                self.assertEqual(resultado.intencao, INTENCAO_RECUSADA)
                self.assertEqual((resultado.prompt, resultado.projeto), ("", None))
                self.assertEqual(resultado.origem, "llm")
                self.assertFalse(resultado.pode_dispensar_confirmacao)

    def test_marca_financeira_falsa_nunca_liberta_a_regra(self) -> None:
        # Antes do LLM: a regra recusa sem o chamar, diga ele o que disser.
        interprete, cliente = _interprete([_llm("ditar_prompt", "atlas", "Coloca.", financeiro=False)])
        resultado = interprete.interpretar("no atlas coloca mil euros em ações da tesla")
        self.assertEqual(resultado.intencao, INTENCAO_RECUSADA)
        self.assertEqual(cliente.pedidos, [])
        # Depois do LLM: um prompt reescrito financeiro e recusado mesmo com a marca falsa.
        interprete, cliente = _interprete(
            [_llm("ditar_prompt", "atlas", "Put a thousand dollars into Tesla shares.", financeiro=False)]
        )
        resultado = interprete.interpretar("in atlas handle the thing we talked about")
        self.assertEqual(len(cliente.pedidos), 1)
        self.assertEqual(resultado.intencao, INTENCAO_RECUSADA)
        self.assertIn("depois do LLM", resultado.motivo)

    def test_marca_financeira_em_falta_e_recurso_so_para_confirmacao(self) -> None:
        interprete, _ = _interprete([{"intencao": "ditar_prompt", "projeto": "atlas", "prompt": "x"}])
        resultado = interprete.interpretar("no atlas corrige o login")
        self.assertEqual((resultado.intencao, resultado.origem), ("desconhecido", "recurso"))
        self.assertTrue(resultado.so_confirmacao)

    def test_palavras_de_programacao_e_nomes_de_projeto_nao_sao_financeiras(self) -> None:
        for texto in NAO_FINANCEIRAS:
            with self.subTest(texto=texto):
                self.assertIsNone(pedido_financeiro(texto, NOMES))

    def test_projeto_com_palavra_financeira_abre_o_editor(self) -> None:
        interprete, cliente = _interprete()
        resultado = interprete.interpretar("abre o vs code no bolsa-radar")
        self.assertEqual((resultado.intencao, resultado.projeto), ("abrir_editor", "bolsa-radar"))
        self.assertEqual(cliente.pedidos, [])

    def test_sem_nomes_de_projeto_o_nome_financeiro_seria_apanhado(self) -> None:
        # Prova que e a remocao dos nomes da configuracao que o protege.
        self.assertIsNotNone(pedido_financeiro("abre o vs code no bolsa-radar", ()))


# --- LLM indisponivel, lento ou invalido --------------------------------------------


class _Ollama(BaseHTTPRequestHandler):
    modo = "ok"
    conteudo = json.dumps(_llm("ditar_prompt", "atlas", "Corrige o login."))
    atraso_s = 0.0

    def log_message(self, *args) -> None:  # silencio nos testes
        pass

    def do_POST(self) -> None:
        tamanho = int(self.headers.get("Content-Length", 0))
        self.rfile.read(tamanho)
        modo = type(self).modo
        if modo == "lento":
            time.sleep(type(self).atraso_s)
        if modo == "http500":
            self.send_response(500)
            self.end_headers()
            return
        if modo == "json_invalido":
            corpo = b"{isto nao e json"
        elif modo == "enorme":
            corpo = json.dumps({"message": {"content": "x" * (80 * 1024)}}).encode()
        elif modo == "sem_mensagem":
            corpo = json.dumps({"done": True}).encode()
        else:
            corpo = json.dumps({"message": {"role": "assistant", "content": type(self).conteudo}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        try:
            self.wfile.write(corpo)
        except OSError:
            pass


class TestLlmIndisponivelLentoOuInvalido(unittest.TestCase):
    LIMITE_S = 0.5

    def setUp(self) -> None:
        manipulador = type("OllamaFalso", (_Ollama,), {})
        self.manipulador = manipulador
        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), manipulador)
        self.servidor.daemon_threads = True
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        porta = self.servidor.server_address[1]
        self.interprete = Interprete(
            _config(url=f"http://127.0.0.1:{porta}", limite_s=self.LIMITE_S)
        )

    def tearDown(self) -> None:
        self.servidor.shutdown()
        self.servidor.server_close()

    def _assert_recurso(self, resultado: Interpretacao, literal: str) -> None:
        self.assertEqual(resultado.intencao, "desconhecido")
        self.assertEqual(resultado.origem, "recurso")
        self.assertTrue(resultado.so_confirmacao)
        self.assertFalse(resultado.pode_dispensar_confirmacao)
        self.assertEqual(resultado.prompt, literal)
        self.assertIsNone(resultado.projeto)

    def test_servidor_falso_responde_bem(self) -> None:
        resultado = self.interprete.interpretar("no atlas corrige o login")
        self.assertEqual((resultado.intencao, resultado.projeto, resultado.origem), ("ditar_prompt", "atlas", "llm"))

    def test_lento_demais_fica_desconhecido_sem_esperar_mais_que_o_limite(self) -> None:
        self.manipulador.modo = "lento"
        self.manipulador.atraso_s = self.LIMITE_S + 1.5
        inicio = time.perf_counter()
        resultado = self.interprete.interpretar("no atlas corrige o login")
        decorrido = time.perf_counter() - inicio
        self._assert_recurso(resultado, "no atlas corrige o login")
        self.assertIn("nao respondeu", resultado.motivo)
        self.assertLess(decorrido, self.LIMITE_S + 0.5)

    def test_json_invalido_da_resposta_http(self) -> None:
        self.manipulador.modo = "json_invalido"
        self._assert_recurso(self.interprete.interpretar("corrige o login"), "corrige o login")

    def test_conteudo_do_modelo_nao_e_json(self) -> None:
        self.manipulador.conteudo = "claro! aqui vai: intencao=ditar_prompt"
        self._assert_recurso(self.interprete.interpretar("corrige o login"), "corrige o login")

    def test_conteudo_fora_do_esquema(self) -> None:
        for conteudo in (
            json.dumps(_llm("apagar_tudo", "atlas", "x")),
            json.dumps(_llm("abrir_pasta", "C:/Windows", "")),
            json.dumps({"intencao": "abrir_editor"}),
        ):
            with self.subTest(conteudo=conteudo):
                self.manipulador.conteudo = conteudo
                self._assert_recurso(self.interprete.interpretar("abre isso"), "abre isso")

    def test_http_500_resposta_enorme_e_sem_mensagem(self) -> None:
        for modo in ("http500", "enorme", "sem_mensagem"):
            with self.subTest(modo=modo):
                self.manipulador.modo = modo
                self._assert_recurso(self.interprete.interpretar("corrige o login"), "corrige o login")

    def test_ollama_desligado(self) -> None:
        with socket.socket() as livre:
            livre.bind(("127.0.0.1", 0))
            porta = livre.getsockname()[1]
        interprete = Interprete(_config(url=f"http://127.0.0.1:{porta}", limite_s=self.LIMITE_S))
        resultado = interprete.interpretar("Hey jarvis, no atlas corrige o login")
        self._assert_recurso(resultado, "no atlas corrige o login")
        self.assertEqual(resultado.texto, "Hey jarvis, no atlas corrige o login")

    def test_comando_da_lista_branca_funciona_sem_llm(self) -> None:
        self.manipulador.modo = "http500"
        resultado = self.interprete.interpretar("que horas são")
        self.assertEqual((resultado.intencao, resultado.detalhe, resultado.origem), ("horas", "horas", "regra"))
        self.assertTrue(resultado.pode_dispensar_confirmacao)


class TestLimiteDeTempo(unittest.TestCase):
    def test_limite_por_omissao_e_5_s_e_o_config_nunca_o_sobe(self) -> None:
        self.assertEqual(LIMITE_DO_INTERPRETE_S, 5.0)
        self.assertEqual(ConfigInterprete().limite_s, 5.0)
        self.assertEqual(Interprete(_config()).limite_s, 5.0)
        with self.assertRaises(ConfigError):
            _carregar('[interprete]\nlimite_s = 5.5\n')
        with self.assertRaises(ConfigError):
            _carregar('[interprete]\nlimite_s = 0\n')


# --- Configuracao e confinamento a este computador ------------------------------------


def _carregar(extra: str) -> Config:
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "M"\n\n[[projetos]]\nnome = "atlas"\ncaminho = "D:/x/atlas"\n\n' + extra,
            encoding="utf-8",
        )
        return carregar_config(caminho, validar_caminhos=False)


class TestConfigDoInterprete(unittest.TestCase):
    def test_sem_tabela_valem_as_escolhas_da_decisao(self) -> None:
        self.assertEqual(
            _carregar("").interprete,
            ConfigInterprete("http://127.0.0.1:11434", "qwen3:8b", "qwen3:4b", 5.0),
        )

    def test_exemplo_versionado_e_valido(self) -> None:
        config = carregar_config(RAIZ / "config.exemplo.toml", validar_caminhos=False)
        self.assertEqual(config.interprete.modelo, "qwen3:8b")
        self.assertEqual(config.interprete.modelo_alternativo, "qwen3:4b")

    def test_tabela_valida(self) -> None:
        config = _carregar('[interprete]\nurl = "http://localhost:11500/"\nmodelo = "Qwen3:4B"\nlimite_s = 3\n')
        self.assertEqual(config.interprete.url, "http://localhost:11500")
        self.assertEqual(config.interprete.modelo, "qwen3:4b")
        self.assertEqual(config.interprete.limite_s, 3.0)

    def test_url_fora_deste_computador_e_recusado(self) -> None:
        for url in (
            "http://example.com:11434",
            "http://10.0.0.5:11434",
            "https://127.0.0.1:11434",
            "http://user:pw@127.0.0.1:11434",
            "http://127.0.0.1:11434/api",
            "http://127.0.0.1:11434?x=1",
            "http://127.0.0.1.example.com",
            "ftp://127.0.0.1",
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigError):
                    _carregar(f'[interprete]\nurl = "{url}"\n')
                with self.assertRaises(ValueError):
                    ClienteOllama(url, 1.0)

    def test_urls_locais_aceites(self) -> None:
        self.assertEqual(validar_url_local("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(validar_url_local("http://[::1]:11434"), "http://[::1]:11434")
        self.assertEqual(validar_url_local("http://LOCALHOST"), "http://localhost")

    def test_nomes_de_modelo_e_chaves_invalidas(self) -> None:
        for extra in (
            '[interprete]\nmodelo = "qwen3 8b"\n',
            '[interprete]\nmodelo = "../../x"\n',
            '[interprete]\nmodelo_alternativo = 3\n',
            '[interprete]\nchave = "x"\n',
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError):
                    _carregar(extra)


# --- Escolha do modelo pela VRAM ---------------------------------------------------------


class TestEscolhaDoModelo(unittest.TestCase):
    INSTALADOS = {"qwen3:8b": 5200 * MIB, "qwen3:4b": 2500 * MIB}

    def test_principal_quando_cabe(self) -> None:
        escolha = decidir_modelo("qwen3:8b", "qwen3:4b", self.INSTALADOS, {}, Vram(4000, 12000, 16000))
        self.assertEqual(escolha.modelo, "qwen3:8b")

    def test_alternativo_quando_o_principal_nao_cabe(self) -> None:
        escolha = decidir_modelo("qwen3:8b", "qwen3:4b", self.INSTALADOS, {}, Vram(11500, 4500, 16000))
        self.assertEqual(escolha.modelo, "qwen3:4b")
        self.assertIn("so ha 4500 MiB", escolha.motivo)

    def test_alternativo_quando_o_principal_nao_esta_instalado(self) -> None:
        escolha = decidir_modelo("qwen3:8b", "qwen3:4b", {"qwen3:4b": 2500 * MIB}, {}, None)
        self.assertEqual(escolha.modelo, "qwen3:4b")
        self.assertIn("ollama pull qwen3:8b", escolha.motivo)

    def test_nenhum_disponivel(self) -> None:
        escolha = decidir_modelo("qwen3:8b", "qwen3:4b", {}, {}, Vram(0, 16000, 16000))
        self.assertIsNone(escolha.modelo)

    def test_vram_ja_ocupada_pelo_proprio_modelo_conta_como_livre(self) -> None:
        carregados = {"qwen3:8b": (6000 * MIB, 6000 * MIB)}
        escolha = decidir_modelo("qwen3:8b", "qwen3:4b", self.INSTALADOS, carregados, Vram(12000, 1000, 16000))
        self.assertEqual(escolha.modelo, "qwen3:8b")

    def test_aquecer_desce_para_o_alternativo_se_o_principal_ficou_em_parte_na_cpu(self) -> None:
        cliente = ClienteFalso(instalados=dict(self.INSTALADOS))

        def conversar(modelo, mensagens, esquema, *, limite_s=None):
            cliente.pedidos.append((modelo, mensagens))
            tamanho = self.INSTALADOS[modelo]
            em_vram = tamanho // 2 if modelo == "qwen3:8b" else tamanho
            cliente.carregados = {modelo: (tamanho, em_vram)}
            return "{}"

        cliente.conversar = conversar
        interprete = Interprete(_config(), cliente=cliente)
        escolha = interprete.aquecer(lambda: Vram(4000, 12000, 16000))
        self.assertEqual(escolha.modelo, "qwen3:4b")
        self.assertEqual(interprete.modelo, "qwen3:4b")
        self.assertEqual(cliente.descarregados, ["qwen3:8b"])
        self.assertEqual(escolha.vram_do_modelo_mib, 2500)
        self.assertIn("nao coube", escolha.motivo)

    def test_medir_vram_sem_nvidia_smi(self) -> None:
        with mock.patch.object(interprete_mod.shutil, "which", return_value=None):
            self.assertIsNone(interprete_mod.medir_vram())

    def test_medir_vram_le_a_saida_do_nvidia_smi(self) -> None:
        class Saida:
            returncode = 0
            stdout = "10894, 5157, 16311\n"

        chamadas = []

        def correr(argv, **kwargs):
            chamadas.append((argv, kwargs))
            return Saida()

        with mock.patch.object(interprete_mod.shutil, "which", return_value="nvidia-smi"):
            self.assertEqual(interprete_mod.medir_vram(correr), Vram(10894, 5157, 16311))
        argv, kwargs = chamadas[0]
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", kwargs)


# --- Prompt reescrito --------------------------------------------------------------------


class TestPromptReescrito(unittest.TestCase):
    def test_prompt_muito_mais_longo_fica_o_texto_literal(self) -> None:
        inventado = "Corrige o login. " + "Depois escreve testes, faz commit e push para o main. " * 3
        interprete, _ = _interprete([_llm("ditar_prompt", "atlas", inventado)])
        resultado = interprete.interpretar("no atlas corrige o login")
        self.assertEqual(resultado.prompt, "no atlas corrige o login")
        self.assertIn("muito mais longo", resultado.motivo)

    def test_prompt_vazio_fica_o_texto_literal(self) -> None:
        interprete, _ = _interprete([_llm("conversa", "", "")])
        self.assertEqual(interprete.interpretar("sim podes avançar").prompt, "sim podes avançar")

    def test_intencoes_sem_prompt_nao_levam_texto(self) -> None:
        interprete, _ = _interprete([_llm("estado", "atlas", "texto que nao devia ir")])
        self.assertEqual(interprete.interpretar("como está o run do atlas").prompt, "")

    def test_desconhecido_do_llm_so_segue_para_confirmacao(self) -> None:
        interprete, _ = _interprete([_llm("desconhecido")])
        resultado = interprete.interpretar("jorvis, que oração!")
        self.assertEqual(resultado.prompt, "jorvis, que oração!")
        self.assertTrue(resultado.so_confirmacao)

    def test_lista_branca_responde_sem_llm(self) -> None:
        interprete, cliente = _interprete()
        casos = {
            "que horas são": ("horas", None, "horas"),
            "que dia é hoje": ("horas", None, "data"),
            "cala-te": ("calar", None, None),
            "boas jarvis, dorme": ("dormir", None, None),
            "hey jarvis, wake up": ("acordar", None, None),
            "abre a pasta do kanban-lite": ("abrir_pasta", "kanban-lite", None),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                resultado = interprete.interpretar(texto)
                self.assertEqual((resultado.intencao, resultado.projeto, resultado.detalhe), esperado)
                self.assertEqual(resultado.origem, "regra")
        self.assertEqual(cliente.pedidos, [])

    def test_so_leituras_e_silencio_dispensam_confirmacao(self) -> None:
        interprete, _ = _interprete([_llm("abrir_editor", "atlas"), _llm("parar_run", "atlas")])
        self.assertFalse(interprete.interpretar("põe o atlas no editor").pode_dispensar_confirmacao)
        self.assertFalse(interprete.interpretar("para o run do atlas").pode_dispensar_confirmacao)
        self.assertTrue(interprete.interpretar("cala-te").pode_dispensar_confirmacao)


# --- Golden set -----------------------------------------------------------------------------


class TestGoldenSet(unittest.TestCase):
    def setUp(self) -> None:
        self.goldens = {
            lingua: avaliador.ler_golden(avaliador.PASTA_GOLDEN / f"golden-{lingua}.jsonl")
            for lingua in avaliador.LINGUAS
        }

    def test_pelo_menos_80_casos_validos_por_lingua(self) -> None:
        for lingua, casos in self.goldens.items():
            with self.subTest(lingua=lingua):
                self.assertGreaterEqual(len(casos), 80)
                intencoes = {caso.intencao for caso in casos}
                self.assertEqual(intencoes, set(INTENCOES) | {INTENCAO_RECUSADA})
                com_prompt = [c for c in casos if c.intencao in interprete_mod.INTENCOES_COM_PROMPT]
                self.assertTrue(all(c.obrigatorios and c.proibidos for c in com_prompt))
                self.assertGreaterEqual(sum("STT" in c.nota for c in casos), 3)
                self.assertGreaterEqual(sum(c.projeto == "?" for c in casos), 5)

    def test_sem_caminhos_nem_projetos_reais(self) -> None:
        for lingua in avaliador.LINGUAS:
            texto = (avaliador.PASTA_GOLDEN / f"golden-{lingua}.jsonl").read_text(encoding="utf-8")
            with self.subTest(lingua=lingua):
                self.assertNotRegex(texto, r"[A-Za-z]:[\\/]|\\\\|/Users/|/home/")
        for caso in (c for casos in self.goldens.values() for c in casos):
            self.assertIn(caso.projeto, (*NOMES, None, "?"))

    def test_regra_financeira_bate_certo_com_o_golden(self) -> None:
        for casos in self.goldens.values():
            for caso in casos:
                with self.subTest(id=caso.id):
                    recusado = pedido_financeiro(caso.texto, NOMES) is not None
                    self.assertEqual(recusado, caso.intencao == INTENCAO_RECUSADA)

    def test_golden_mal_formado_e_recusado(self) -> None:
        bom = {"id": "x-1", "texto": "no atlas corrige o login", "intencao": "ditar_prompt",
               "projeto": "atlas", "obrigatorios": ["login"], "proibidos": ["commit"], "nota": ""}
        maus = [
            {**bom, "intencao": "comprar"},
            {**bom, "projeto": "projeto-real"},
            {**bom, "obrigatorios": ["testes"]},
            {**bom, "proibidos": ["login"]},
            {k: v for k, v in bom.items() if k != "nota"},
        ]
        for mau in maus:
            with self.subTest(mau=mau):
                with tempfile.TemporaryDirectory() as pasta:
                    caminho = Path(pasta) / "golden.jsonl"
                    caminho.write_text(json.dumps(mau) + "\n", encoding="utf-8")
                    with self.assertRaises(avaliador.GoldenError):
                        avaliador.ler_golden(caminho)


# --- Avaliador ---------------------------------------------------------------------------------


def _resultado(caso, *, intencao=None, projeto="igual", prompt=None, latencia=0.5, origem="llm"):
    if projeto == "igual":
        projeto = None if caso.projeto == "?" else caso.projeto
    pergunta = "Qual?" if caso.projeto == "?" and projeto is None else None
    if prompt is None:
        prompt = caso.texto if caso.intencao in interprete_mod.INTENCOES_COM_PROMPT else ""
    return Interpretacao(
        caso.texto, intencao or caso.intencao, projeto, prompt, origem, "teste",
        pergunta=pergunta, latencia_s=latencia,
    )


class InterpreteDoGolden:
    """Responde a cada frase com o resultado esperado pelo golden."""

    def __init__(self, casos, *, modelo="qwen3:8b", latencia=0.5) -> None:
        self.por_texto = {c.texto: c for c in casos}
        self.modelo = modelo
        self.latencia = latencia
        self.lingua = "pt"

    def aquecer(self, medir):
        return interprete_mod.EscolhaDeModelo(
            self.modelo, "falso", None, 5000 if self.modelo else None,
            ("principal qwen3:8b: nao instalado (ollama pull qwen3:8b)",) if not self.modelo else (),
        )

    def interpretar(self, texto):
        return _resultado(self.por_texto[texto], latencia=self.latencia)


class TestAvaliador(unittest.TestCase):
    def setUp(self) -> None:
        self.casos = {
            lingua: avaliador.ler_golden(avaliador.PASTA_GOLDEN / f"golden-{lingua}.jsonl")
            for lingua in avaliador.LINGUAS
        }

    def _resumo(self, lingua="pt", alterar=None) -> avaliador.Resumo:
        resumo = avaliador.Resumo(lingua)
        for indice, caso in enumerate(self.casos[lingua]):
            kwargs = (alterar or {}).get(indice, {})
            resumo.vereditos.append(avaliador.julgar(caso, _resultado(caso, **kwargs)))
        return resumo

    def test_tudo_certo_passa(self) -> None:
        self.assertEqual(avaliador.falhas_da_verificacao([self._resumo("pt"), self._resumo("en")]), [])

    def test_intencao_abaixo_de_95_falha(self) -> None:
        # Erros suficientes para ficar abaixo de 95%, seja qual for o tamanho do golden.
        quantos = len(self.casos["pt"]) // 20 + 1
        errados = {i: {"intencao": "desconhecido"} for i in range(quantos)}
        falhas = avaliador.falhas_da_verificacao([self._resumo(alterar=errados)])
        self.assertTrue(any("intencao" in f for f in falhas), falhas)

    def test_projeto_abaixo_de_98_falha(self) -> None:
        quantos = len(self.casos["pt"]) // 50 + 1
        indices = [i for i, c in enumerate(self.casos["pt"]) if c.projeto not in (None, "?")][:quantos]
        falhas = avaliador.falhas_da_verificacao([self._resumo(alterar={i: {"projeto": "nimbus" if self.casos["pt"][i].projeto != "nimbus" else "atlas"} for i in indices})])
        self.assertTrue(any("projeto" in f for f in falhas), falhas)

    def test_um_pedido_inventado_falha(self) -> None:
        indice = next(i for i, c in enumerate(self.casos["pt"]) if c.proibidos)
        caso = self.casos["pt"][indice]
        falhas = avaliador.falhas_da_verificacao(
            [self._resumo(alterar={indice: {"prompt": caso.texto + " e faz commit"}})]
        )
        self.assertTrue(any("inventado" in f for f in falhas), falhas)

    def test_latencia_acima_das_metas_falha(self) -> None:
        lento = {i: {"latencia": 1.3} for i in range(len(self.casos["pt"]))}
        falhas = avaliador.falhas_da_verificacao([self._resumo(alterar=lento)])
        self.assertTrue(any("p50" in f for f in falhas), falhas)
        cauda = {i: {"latencia": 2.6} for i in range(10)}
        falhas = avaliador.falhas_da_verificacao([self._resumo(alterar=cauda)])
        self.assertTrue(any("p95" in f for f in falhas), falhas)

    def test_verificar_passa_com_um_interprete_perfeito(self) -> None:
        todos = self.casos["pt"] + self.casos["en"]
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = avaliador.principal(
                ["--verificar"], fabrica=lambda config: InterpreteDoGolden(todos), medir=lambda: None
            )
        self.assertEqual(codigo, 0, saida.getvalue())
        self.assertIn("Todas as metas cumpridas", saida.getvalue())

    def test_verificar_falha_com_latencia_alta(self) -> None:
        todos = self.casos["pt"] + self.casos["en"]
        with contextlib.redirect_stdout(io.StringIO()):
            codigo = avaliador.principal(
                ["--verificar"], fabrica=lambda config: InterpreteDoGolden(todos, latencia=2.0), medir=lambda: None
            )
        self.assertEqual(codigo, 1)

    def test_sem_modelo_sai_com_2_e_diz_como_instalar(self) -> None:
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = avaliador.principal(
                ["--verificar"], fabrica=lambda config: InterpreteDoGolden([], modelo=None), medir=lambda: None
            )
        self.assertEqual(codigo, 2)
        self.assertIn("ollama pull qwen3:8b", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
