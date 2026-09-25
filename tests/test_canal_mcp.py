r"""Testes do servidor MCP do canal do jarvis (jarvis/canal_mcp.py), unittest.

O cliente MCP e falso (linhas JSON-RPC em memoria) e o jarvis e um socket em
127.0.0.1 aberto pelo proprio teste. Nada arranca o CLI do Claude Code.

Corre com:

    .venv\Scripts\python -m unittest tests.test_canal_mcp -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from jarvis import canal_mcp
from jarvis.canal_mcp import (
    CAPACIDADE_DO_CANAL,
    CAPACIDADE_PROIBIDA,
    ERRO_METODO_DESCONHECIDO,
    FERRAMENTA_DE_RESPOSTA,
    METODO_DO_CANAL,
    TIPO_OLA,
    TIPO_PROMPT,
    TIPO_RESPOSTA,
    EnderecoIpc,
    LigacaoAoJarvis,
    MensagemRecusada,
    ServidorDoCanal,
    apagar_endereco_se_for,
    escrever_endereco,
    gerar_segredo,
    ler_endereco,
    linha_de_mensagem,
    novo_pedido,
    validar_mensagem,
    validar_nome_de_projeto,
)

PROJETO = "exemplo-um"


def _esperar(condicao, limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


class _Servidor:
    """ServidorDoCanal com saida em memoria e um registo do que foi para o jarvis."""

    def __init__(self, ligado: bool = True):
        self.saida = io.BytesIO()
        self.enviadas: list[tuple[str, str, str]] = []
        self.ligado = ligado
        self.servidor = ServidorDoCanal(
            PROJETO, io.BytesIO(), self.saida, self._enviar, avisar=lambda _t: None
        )

    def _enviar(self, texto: str, falado: str, pedido: str) -> bool:
        if not self.ligado:
            return False
        self.enviadas.append((texto, falado, pedido))
        return True

    def pedir(self, ident: int | None, metodo: str, params: dict | None = None) -> None:
        pedido: dict[str, Any] = {"jsonrpc": "2.0", "method": metodo}
        if ident is not None:
            pedido["id"] = ident
        if params is not None:
            pedido["params"] = params
        self.servidor.tratar(pedido)

    def mensagens(self) -> list[dict[str, Any]]:
        return [json.loads(linha) for linha in self.saida.getvalue().splitlines() if linha]

    def resposta(self, ident: int) -> dict[str, Any]:
        return next(m for m in self.mensagens() if m.get("id") == ident)

    def notificacoes(self) -> list[dict[str, Any]]:
        return [m for m in self.mensagens() if m.get("method") == METODO_DO_CANAL]

    def iniciar(self) -> None:
        self.pedir(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.pedir(None, "notifications/initialized")


class TestCapacidades(unittest.TestCase):
    def test_declara_so_o_canal_e_nunca_o_relay_de_permissoes(self):
        s = _Servidor()
        s.iniciar()
        resultado = s.resposta(1)["result"]
        experimental = resultado["capabilities"]["experimental"]
        self.assertIn(CAPACIDADE_DO_CANAL, experimental)
        self.assertNotIn(CAPACIDADE_PROIBIDA, experimental)
        self.assertNotIn(CAPACIDADE_PROIBIDA, json.dumps(resultado))
        self.assertEqual(resultado["serverInfo"]["name"], "jarvis")
        self.assertTrue(resultado["instructions"])

    def test_versao_conhecida_e_ecoada_e_desconhecida_cai_para_a_padrao(self):
        s = _Servidor()
        s.pedir(1, "initialize", {"protocolVersion": "2025-03-26"})
        s.pedir(2, "initialize", {"protocolVersion": "2026-07-28"})
        self.assertEqual(s.resposta(1)["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(s.resposta(2)["result"]["protocolVersion"], canal_mcp.VERSAO_MCP_PADRAO)

    def test_server_discover_e_metodos_desconhecidos_dao_method_not_found(self):
        # sem server/discover o Claude Code fica na revisao que aceita notificacoes do canal
        s = _Servidor()
        s.pedir(1, "server/discover", {})
        s.pedir(2, "resources/list", {})
        self.assertEqual(s.resposta(1)["error"]["code"], ERRO_METODO_DESCONHECIDO)
        self.assertEqual(s.resposta(2)["error"]["code"], ERRO_METODO_DESCONHECIDO)

    def test_uma_so_ferramenta_responder(self):
        s = _Servidor()
        s.pedir(1, "tools/list", {})
        ferramentas = s.resposta(1)["result"]["tools"]
        self.assertEqual([f["name"] for f in ferramentas], [FERRAMENTA_DE_RESPOSTA])
        self.assertEqual(ferramentas[0]["inputSchema"]["required"], ["texto"])

    def test_pedido_invalido_e_json_partido_nao_derrubam_o_servidor(self):
        s = _Servidor()
        s.servidor.entrada = io.BytesIO(
            b"isto nao e json\n"
            + b'{"jsonrpc": "1.0", "id": 9, "method": "ping"}\n'
            + b'{"jsonrpc": "2.0", "id": 3, "method": "ping"}\n'
        )
        s.servidor.correr()
        self.assertEqual(s.resposta(3)["result"], {})


class TestNotificacaoDoCanal(unittest.TestCase):
    def test_prompt_confirmado_vai_tal_e_qual_com_meta_valida(self):
        s = _Servidor()
        s.iniciar()
        pedido = novo_pedido()
        texto = "Corre os testes do modulo de voz.\nDiz-me so quantos falharam."
        s.servidor.empurrar_prompt(texto, pedido)
        (notificacao,) = s.notificacoes()
        self.assertNotIn("id", notificacao)
        self.assertEqual(notificacao["params"]["content"], texto)
        meta = notificacao["params"]["meta"]
        self.assertEqual(meta, {"origem": "voz", "projeto": PROJETO, "pedido": pedido})
        for chave, valor in meta.items():
            self.assertRegex(chave, r"^[a-zA-Z_][a-zA-Z0-9_]*$")
            self.assertIsInstance(valor, str)

    def test_prompt_antes_do_initialized_espera_e_sai_uma_vez(self):
        s = _Servidor()
        s.pedir(1, "initialize", {"protocolVersion": "2025-06-18"})
        s.servidor.empurrar_prompt("primeiro", novo_pedido())
        self.assertEqual(s.notificacoes(), [])
        s.pedir(None, "notifications/initialized")
        s.pedir(None, "notifications/initialized")
        self.assertEqual([n["params"]["content"] for n in s.notificacoes()], ["primeiro"])


class TestFerramentaResponder(unittest.TestCase):
    def _chamar(self, s: _Servidor, ident: int, argumentos: dict, nome: str = FERRAMENTA_DE_RESPOSTA):
        s.pedir(ident, "tools/call", {"name": nome, "arguments": argumentos})
        return s.resposta(ident)

    def test_texto_tecnico_passa_pelo_filtro_antes_da_voz(self):
        s = _Servidor()
        texto = (
            "Corrigi o erro no gravador.\n"
            "```python\nsubprocess.run(['rm', '-rf', 'x'])\n```\n"
            '<invoke name="Bash"><parameter name="command">git push</parameter></invoke>\n'
        )
        pedido = novo_pedido()
        resposta = self._chamar(s, 1, {"texto": texto, "pedido": pedido})
        self.assertFalse(resposta["result"]["isError"])
        ((enviado, falado, pedido_enviado),) = s.enviadas
        self.assertEqual(enviado, texto)
        self.assertEqual(pedido_enviado, pedido)
        self.assertIn("Corrigi o erro no gravador.", falado)
        for proibido in ("subprocess", "rm -rf", "invoke", "git push", "```"):
            self.assertNotIn(proibido, falado)

    def test_so_codigo_cai_na_frase_de_recurso(self):
        s = _Servidor()
        self._chamar(s, 1, {"texto": "```\nimport os\n```"})
        ((_texto, falado, _pedido),) = s.enviadas
        self.assertNotIn("import", falado)
        self.assertTrue(falado)

    def test_sem_texto_ou_texto_nao_string_e_erro_da_ferramenta(self):
        s = _Servidor()
        self.assertTrue(self._chamar(s, 1, {})["result"]["isError"])
        self.assertTrue(self._chamar(s, 2, {"texto": 5})["result"]["isError"])
        self.assertTrue(self._chamar(s, 3, {"texto": "   "})["result"]["isError"])
        self.assertEqual(s.enviadas, [])

    def test_pedido_mal_formado_e_ignorado_mas_a_resposta_segue(self):
        s = _Servidor()
        self._chamar(s, 1, {"texto": "Feito.", "pedido": "x\r\ny"})
        self.assertEqual(s.enviadas[0][2], "")

    def test_sem_jarvis_ligado_devolve_erro(self):
        s = _Servidor(ligado=False)
        self.assertTrue(self._chamar(s, 1, {"texto": "Feito."})["result"]["isError"])

    def test_outra_ferramenta_e_recusada(self):
        s = _Servidor()
        self.assertIn("error", self._chamar(s, 1, {"command": "dir"}, nome="Bash"))
        self.assertEqual(s.enviadas, [])


class TestValidarMensagem(unittest.TestCase):
    def setUp(self):
        self.segredo = gerar_segredo()
        self.pedido = novo_pedido()

    def _prompt(self, **mudancas) -> dict:
        base = {
            "tipo": TIPO_PROMPT,
            "segredo": self.segredo,
            "projeto": PROJETO,
            "pedido": self.pedido,
            "texto": "Resume o estado.",
        }
        base.update(mudancas)
        return base

    def _validar(self, obj_ou_linha, projeto: str | None = PROJETO, tipos=(TIPO_PROMPT,)):
        linha = obj_ou_linha
        if isinstance(obj_ou_linha, dict):
            linha = json.dumps(obj_ou_linha) + "\n"
        return validar_mensagem(linha, segredo=self.segredo, tipos=set(tipos), projeto=projeto)

    def _recusada(self, obj_ou_linha, motivo: str, **kwargs):
        with self.assertRaises(MensagemRecusada) as contexto:
            self._validar(obj_ou_linha, **kwargs)
        self.assertIn(motivo, str(contexto.exception))
        # o motivo nunca ecoa o segredo nem o texto
        self.assertNotIn(self.segredo, str(contexto.exception))

    def test_mensagem_valida_passa_sem_o_segredo(self):
        campos = self._validar(self._prompt(texto="linha um\nlinha dois"))
        self.assertNotIn("segredo", campos)
        self.assertEqual(campos["texto"], "linha um\nlinha dois")

    def test_sem_segredo(self):
        obj = self._prompt()
        del obj["segredo"]
        self._recusada(obj, "sem segredo")
        self._recusada(self._prompt(segredo=""), "sem segredo")
        self._recusada(self._prompt(segredo=None), "sem segredo")

    def test_segredo_errado(self):
        self._recusada(self._prompt(segredo=gerar_segredo()), "segredo errado")
        self._recusada(self._prompt(segredo=self.segredo[:-1]), "segredo errado")
        self._recusada(self._prompt(segredo=self.segredo.upper()), "segredo errado")

    def test_segredo_verifica_se_antes_de_tudo(self):
        self._recusada(
            self._prompt(segredo="0" * 64, tipo="outro", projeto="x\ny"), "segredo errado"
        )

    def test_cr_lf_fora_do_campo_de_texto(self):
        for campo, valor in (
            ("projeto", PROJETO + "\n"),
            ("projeto", PROJETO + "\r"),
            ("pedido", self.pedido + "\r\n"),
            ("tipo", TIPO_PROMPT + "\n"),
        ):
            with self.subTest(campo=campo):
                self._recusada(self._prompt(**{campo: valor}), campo)
        resposta = {
            "tipo": TIPO_RESPOSTA,
            "segredo": self.segredo,
            "projeto": PROJETO,
            "texto": "ok\nok",
            "falado": "ok\nmais",
        }
        self._recusada(resposta, "falado", tipos=(TIPO_RESPOSTA,))

    def test_cr_cru_na_linha_e_recusado(self):
        linha = json.dumps(self._prompt()) + "\r\n"
        self._recusada(linha, "CR/LF")
        linha_dupla = json.dumps(self._prompt()) + "\n" + json.dumps(self._prompt()) + "\n"
        self._recusada(linha_dupla, "CR/LF")

    def test_nul_no_texto(self):
        self._recusada(self._prompt(texto="ola\x00"), "NUL")

    def test_outro_projeto(self):
        self._recusada(self._prompt(projeto="exemplo-dois"), "outro projeto")

    def test_nome_de_projeto_invalido(self):
        for nome in ("", "../fora", "a" * 65, "-x", "nome;rm"):
            with self.subTest(nome=nome):
                self._recusada(self._prompt(projeto=nome), "projeto", projeto=None)

    def test_tipo_campos_e_tipos_de_valor(self):
        self._recusada(self._prompt(tipo=TIPO_RESPOSTA), "tipo nao esperado")
        self._recusada(self._prompt(tipo="executar"), "tipo nao esperado")
        self._recusada(self._prompt(comando="dir"), "campos a mais")
        obj = self._prompt()
        del obj["texto"]
        self._recusada(obj, "faltam campos")
        self._recusada(self._prompt(texto=["a"]), "nao e texto")
        self._recusada(self._prompt(pedido="nao-hex"), "pedido invalido")

    def test_linhas_que_nao_sao_mensagens(self):
        self._recusada("nao e json\n", "nao e JSON")
        self._recusada("[1, 2]\n", "objeto")
        self._recusada(b"\xff\xfe\n", "UTF-8")
        self._recusada(b"x" * (canal_mcp.MAXIMO_BYTES_POR_LINHA + 1), "grande demais")
        self._recusada(self._prompt(texto="a" * (canal_mcp.MAXIMO_CARACTERES_DE_TEXTO + 1)), "grande")

    def test_linha_de_mensagem_e_ascii_de_uma_linha(self):
        linha = linha_de_mensagem(
            TIPO_PROMPT, self.segredo, projeto=PROJETO, pedido=self.pedido, texto="ação\nlinha "
        )
        self.assertTrue(linha.endswith(b"\n"))
        self.assertEqual(linha.count(b"\n"), 1)
        linha.decode("ascii")
        self.assertEqual(self._validar(linha)["texto"], "ação\nlinha ")


class TestNomeDeProjeto(unittest.TestCase):
    def test_nomes_aceites_e_recusados(self):
        for nome in ("jarvis", "exemplo-um", "Projeto 2", "app.web", "ação_x"):
            self.assertEqual(validar_nome_de_projeto(nome), nome)
        for nome in ("", " x", "x\n", "a/b", "a\\b", "..", "x&y", 5, None):
            with self.subTest(nome=nome), self.assertRaises(ValueError):
                validar_nome_de_projeto(nome)


class TestFicheiroDoEndereco(unittest.TestCase):
    def test_escrever_ler_e_apagar_so_o_proprio(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "sub" / "ipc.json"
            meu = EnderecoIpc(porta=40000, segredo=gerar_segredo(), pid=os.getpid())
            escrever_endereco(meu, caminho)
            self.assertEqual(ler_endereco(caminho), meu)
            novo = EnderecoIpc(porta=40001, segredo=gerar_segredo(), pid=os.getpid())
            escrever_endereco(novo, caminho)
            apagar_endereco_se_for(meu, caminho)  # o arranque antigo nao apaga o novo
            self.assertEqual(ler_endereco(caminho), novo)
            apagar_endereco_se_for(novo, caminho)
            self.assertFalse(caminho.exists())
            self.assertEqual(list(caminho.parent.iterdir()), [])

    def test_ficheiros_invalidos_dao_none(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "ipc.json"
            self.assertIsNone(ler_endereco(caminho))
            for conteudo in (
                "nao json",
                "[]",
                json.dumps({"porta": 0, "segredo": "a" * 64, "pid": 1}),
                json.dumps({"porta": True, "segredo": "a" * 64, "pid": 1}),
                json.dumps({"porta": 4000, "segredo": "curto", "pid": 1}),
                json.dumps({"porta": 4000, "segredo": "A" * 64, "pid": 1}),
                json.dumps({"porta": 4000, "segredo": "a" * 64, "pid": "1"}),
            ):
                with self.subTest(conteudo=conteudo):
                    caminho.write_text(conteudo, encoding="ascii")
                    self.assertIsNone(ler_endereco(caminho))


class _JarvisDeTeste:
    """Um jarvis minimo: aceita uma ligacao e guarda as linhas recebidas."""

    def __init__(self, caminho: Path):
        self.segredo = gerar_segredo()
        self.servidor = socket.create_server(("127.0.0.1", 0))
        escrever_endereco(EnderecoIpc(self.servidor.getsockname()[1], self.segredo, os.getpid()), caminho)
        self.conexao: socket.socket | None = None
        self.linhas: list[bytes] = []
        self._aceite = threading.Event()
        threading.Thread(target=self._aceitar, daemon=True).start()

    def _aceitar(self):
        self.conexao, _ = self.servidor.accept()
        self._aceite.set()
        with contextlib.suppress(OSError):
            for linha in self.conexao.makefile("rb"):
                self.linhas.append(linha)

    def mandar(self, linha: bytes):
        self.assert_aceite()
        self.conexao.sendall(linha)

    def assert_aceite(self):
        if not self._aceite.wait(5):
            raise AssertionError("o canal nao se ligou")

    def fechar(self):
        for sock in (self.conexao, self.servidor):
            if sock is not None:
                canal_mcp._fechar_socket(sock)


class TestLigacaoAoJarvis(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        self.caminho = Path(self.pasta.name) / "ipc.json"
        self.jarvis = _JarvisDeTeste(self.caminho)
        self.addCleanup(self.jarvis.fechar)
        self.recebidos: list[tuple[str, str]] = []
        self.ligacao = LigacaoAoJarvis(
            PROJETO,
            lambda texto, pedido: self.recebidos.append((texto, pedido)),
            self.caminho,
            intervalo_s=0.05,
            avisar=lambda _t: None,
        ).iniciar()
        self.addCleanup(self.ligacao.parar)
        self.jarvis.assert_aceite()

    def test_apresenta_se_com_segredo_e_projeto(self):
        self.assertTrue(_esperar(lambda: self.jarvis.linhas))
        ola = validar_mensagem(self.jarvis.linhas[0], segredo=self.jarvis.segredo, tipos={TIPO_OLA})
        self.assertEqual(ola["projeto"], PROJETO)

    def test_so_prompts_validos_para_este_projeto_chegam(self):
        pedido = novo_pedido()
        s = self.jarvis.segredo
        maus = [
            (json.dumps({"tipo": TIPO_PROMPT, "projeto": PROJETO, "pedido": pedido, "texto": "a"}) + "\n").encode(),
            linha_de_mensagem(TIPO_PROMPT, gerar_segredo(), projeto=PROJETO, pedido=pedido, texto="b"),
            linha_de_mensagem(TIPO_PROMPT, s, projeto="exemplo-dois", pedido=pedido, texto="c"),
            linha_de_mensagem(TIPO_PROMPT, s, projeto=PROJETO + "\n", pedido=pedido, texto="d"),
            linha_de_mensagem(TIPO_PROMPT, s, projeto=PROJETO, pedido=pedido + "\r", texto="e"),
            linha_de_mensagem(TIPO_RESPOSTA, s, projeto=PROJETO, texto="f", falado="f"),
        ]
        for linha in maus:
            self.jarvis.mandar(linha)
        self.jarvis.mandar(linha_de_mensagem(TIPO_PROMPT, s, projeto=PROJETO, pedido=pedido, texto="bom"))
        self.assertTrue(_esperar(lambda: self.recebidos))
        self.assertEqual(self.recebidos, [("bom", pedido)])
        self.assertEqual(self.ligacao.recusadas, len(maus))

    def test_resposta_segue_com_segredo_e_projeto(self):
        self.assertTrue(_esperar(lambda: self.ligacao.ligado))
        self.assertTrue(self.ligacao.enviar_resposta("Feito.\nTudo bem.", "Feito. Tudo bem.", ""))
        self.assertTrue(_esperar(lambda: len(self.jarvis.linhas) >= 2))
        campos = validar_mensagem(
            self.jarvis.linhas[1], segredo=self.jarvis.segredo, tipos={TIPO_RESPOSTA}, projeto=PROJETO
        )
        self.assertEqual(campos["texto"], "Feito.\nTudo bem.")

    def test_sem_ficheiro_de_endereco_fica_desligado(self):
        with tempfile.TemporaryDirectory() as vazia:
            ligacao = LigacaoAoJarvis(
                PROJETO, lambda *_: None, Path(vazia) / "ipc.json", intervalo_s=0.05, avisar=lambda _t: None
            ).iniciar()
            try:
                time.sleep(0.2)
                self.assertFalse(ligacao.ligado)
                self.assertFalse(ligacao.enviar_resposta("x", "x"))
            finally:
                ligacao.parar()


class TestAutoteste(unittest.TestCase):
    def test_autoteste_com_cliente_mcp_falso_passa(self):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = canal_mcp._autoteste()
        self.assertEqual(codigo, 0, saida.getvalue())
        self.assertNotIn("FALHA", saida.getvalue())


class TestSemDadosPrivadosNoCodigo(unittest.TestCase):
    def test_modulo_e_exemplo_sem_caminhos_de_utilizador(self):
        raiz = Path(__file__).resolve().parent.parent
        for relativo in ("jarvis/canal_mcp.py", "jarvis/sessoes.py", "config/canal-mcp.exemplo.json"):
            texto = (raiz / relativo).read_text(encoding="utf-8")
            with self.subTest(ficheiro=relativo):
                self.assertIsNone(re.search(r"[A-Za-z]:[\\/]+Users[\\/]", texto))


if __name__ == "__main__":
    unittest.main()
