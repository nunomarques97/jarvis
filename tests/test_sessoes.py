r"""Testes das sessoes por projeto (jarvis/sessoes.py), unittest.

O CLI do Claude Code e sempre falso: o "lancador" regista a linha de comandos e
escreve no ficheiro de debug o que o Claude Code real escreveria. O IPC usa
sockets reais em 127.0.0.1 com ficheiros de endereco temporarios. A prova real
contra o CLI e `python -m jarvis.sessoes prova`, fora da suite.

Corre com:

    .venv\Scripts\python -m unittest tests.test_sessoes -v
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import socket
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from jarvis import canal_mcp, sessoes
from jarvis.canal_claude import CanalIndisponivel, RespostaRecolhida
from jarvis.canal_mcp import (
    TIPO_OLA,
    TIPO_RESPOSTA,
    LigacaoAoJarvis,
    gerar_segredo,
    ler_endereco,
    linha_de_mensagem,
    novo_pedido,
)
from jarvis.config import Config, ConfigError, Projeto
from jarvis.sessoes import (
    CAMINHO_CANAL,
    CAMINHO_HEADLESS,
    CanalDoProjeto,
    CentralDoCanal,
    SessaoAberta,
    SessaoHeadless,
    abrir_sessao,
    arvore,
    esperar_registo,
    procurar_registo,
)

RAIZ = Path(__file__).resolve().parent.parent
CLI_FALSO = str(Path(tempfile.gettempdir()) / "cli-falso" / "claude.exe")
REGISTADO = '2026-09-25T10:00:00.000Z [DEBUG] MCP server "jarvis": Channel notifications registered\n'
RECUSADO = (
    '2026-09-25T10:00:00.000Z [DEBUG] MCP server "jarvis": Channel notifications skipped: '
    "channels feature is not currently available\n"
)


def _esperar(condicao, limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


class ProcessoFalso:
    def __init__(self, codigo: int | None = None):
        self.codigo = codigo
        self.pid = 4242
        self.terminado = False

    def poll(self):
        return self.codigo

    def terminate(self):
        self.terminado = True
        self.codigo = 1

    def wait(self, timeout=None):
        return self.codigo


class LancadorFalso:
    """Faz de Claude Code: regista a chamada e escreve o debug que o CLI escreveria."""

    def __init__(self, debug: str | list[str] = REGISTADO, codigo: int | None = None):
        self.debug = [debug] if isinstance(debug, str) else debug
        self.codigo = codigo
        self.chamadas: list[tuple[list[str], Path, dict]] = []

    def __call__(self, argumentos, cwd, env):
        self.chamadas.append((list(argumentos), Path(cwd), dict(env)))
        caminho = Path(argumentos[argumentos.index("--debug-file") + 1])
        with caminho.open("a", encoding="utf-8") as ficheiro:
            for pedaco in self.debug:
                ficheiro.write(pedaco)
        return ProcessoFalso(self.codigo)


class _ComProjetoTemporario(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="projeto-teste-")
        self.addCleanup(pasta.cleanup)
        self.pasta_projeto = Path(pasta.name)
        (self.pasta_projeto / "src").mkdir()
        (self.pasta_projeto / "src" / "app.py").write_text("print('ola')\n", encoding="utf-8")
        (self.pasta_projeto / "LEIA.md").write_text("projeto de teste\n", encoding="utf-8")
        # nome unico: a pasta gerada no repositorio jarvis e so deste teste
        self.nome = f"teste-sessoes-{uuid.uuid4().hex[:8]}"
        self.projeto = Projeto(nome=self.nome, caminho=self.pasta_projeto)
        self.logs: list[str] = []
        self.addCleanup(shutil.rmtree, sessoes.pasta_da_sessao(self.nome), True)

    def abrir(self, lancador, **kwargs) -> SessaoAberta:
        kwargs.setdefault("espera_s", 2.0)
        kwargs.setdefault("dormir", lambda _s: None)
        return abrir_sessao(
            self.projeto, cli=CLI_FALSO, lancar=lancador, log=self.logs.append, **kwargs
        )


class TestAbrirSessao(_ComProjetoTemporario):
    def test_abre_na_pasta_do_projeto_com_ficheiros_do_repositorio_jarvis(self):
        antes = arvore(self.pasta_projeto)
        lancador = LancadorFalso()
        sessao = self.abrir(lancador)
        depois = arvore(self.pasta_projeto)

        self.assertEqual(antes, depois, "a pasta do projeto nao pode mudar")
        ((argumentos, cwd, env),) = lancador.chamadas
        self.assertEqual(cwd, self.pasta_projeto.resolve())
        self.assertEqual(argumentos[0], CLI_FALSO)
        indice = argumentos.index("--dangerously-load-development-channels")
        self.assertEqual(argumentos[indice + 1], "server:jarvis")

        caminho_mcp = Path(argumentos[argumentos.index("--mcp-config") + 1])
        caminho_debug = Path(argumentos[argumentos.index("--debug-file") + 1])
        for caminho in (caminho_mcp, caminho_debug):
            self.assertTrue(caminho.resolve().is_relative_to(RAIZ / ".jarvis"), caminho)
            self.assertFalse(caminho.resolve().is_relative_to(self.pasta_projeto.resolve()))

        config = json.loads(caminho_mcp.read_text(encoding="utf-8"))
        servidor = config["mcpServers"]["jarvis"]
        self.assertEqual(servidor["args"], ["-m", "jarvis.canal_mcp", "--projeto", self.nome])
        self.assertTrue(servidor["command"].lower().endswith(".exe") or "python" in servidor["command"])
        self.assertEqual(servidor["env"], {"PYTHONPATH": str(RAIZ)})

        self.assertNotIn("CLAUDECODE", env)
        self.assertFalse(any(chave.startswith("CLAUDE_") for chave in env))
        self.assertEqual(sessao.caminho, CAMINHO_CANAL)
        self.assertTrue(any("caminho=canal" in linha for linha in self.logs))

    def test_canal_recusado_cai_no_headless_com_o_motivo_no_log(self):
        sessao = self.abrir(LancadorFalso(RECUSADO))
        self.assertEqual(sessao.caminho, CAMINHO_HEADLESS)
        self.assertEqual(sessao.motivo, "channels feature is not currently available")
        linha = next(l for l in self.logs if "caminho=headless" in l)
        self.assertIn("claude --resume", linha)
        self.assertIn("claude resume", sessao.frase_para_o_utilizador())

    def test_so_o_veredito_do_servidor_jarvis_conta(self):
        outro = (
            '[DEBUG] MCP server "playwright": Channel notifications skipped: server did not '
            "declare claude/channel capability\n"
        )
        sessao = self.abrir(LancadorFalso([outro, REGISTADO]))
        self.assertEqual(sessao.caminho, CAMINHO_CANAL)

    def test_janela_fechada_antes_do_registo_cai_no_headless(self):
        sessao = self.abrir(LancadorFalso("nada de canal\n", codigo=1))
        self.assertEqual(sessao.caminho, CAMINHO_HEADLESS)
        self.assertIn("fechou", sessao.motivo)

    def test_sem_sinal_dentro_do_tempo_cai_no_headless(self):
        tempo = [0.0]

        def relogio():
            return tempo[0]

        def dormir(segundos):
            tempo[0] += segundos

        sessao = self.abrir(LancadorFalso("sem veredito\n"), espera_s=10, relogio=relogio, dormir=dormir)
        self.assertEqual(sessao.caminho, CAMINHO_HEADLESS)
        self.assertIn("10 s", sessao.motivo)

    def test_shim_cmd_e_recusado_sem_lancar_nada(self):
        lancador = LancadorFalso()
        with self.assertRaises(ValueError):
            abrir_sessao(self.projeto, cli="C:/npm/claude.cmd", lancar=lancador, log=self.logs.append)
        self.assertEqual(lancador.chamadas, [])

    def test_pasta_inexistente_ou_nome_invalido_nao_lanca(self):
        lancador = LancadorFalso()
        with self.assertRaises(ConfigError):
            abrir_sessao(
                Projeto(nome=self.nome, caminho=self.pasta_projeto / "nao-existe"),
                cli=CLI_FALSO,
                lancar=lancador,
                log=self.logs.append,
            )
        with self.assertRaises(ValueError):
            abrir_sessao(
                Projeto(nome="x & del", caminho=self.pasta_projeto),
                cli=CLI_FALSO,
                lancar=lancador,
                log=self.logs.append,
            )
        self.assertEqual(lancador.chamadas, [])

    def test_so_ficam_os_debug_mais_recentes(self):
        for _ in range(sessoes.DEBUG_A_MANTER + 2):
            self.abrir(LancadorFalso())
        pasta = sessoes.pasta_da_sessao(self.nome)
        self.assertEqual(len(list(pasta.glob("debug-*.log"))), sessoes.DEBUG_A_MANTER)


class TestVereditoDoDebug(unittest.TestCase):
    def test_procurar_registo(self):
        self.assertTrue(procurar_registo(REGISTADO).registado)
        self.assertEqual(procurar_registo(RECUSADO).estado, "recusado")
        self.assertIsNone(procurar_registo("MCP server \"jarvis\": Successfully connected\n"))
        self.assertIsNone(procurar_registo('MCP server "jarvis2": Channel notifications registered'))

    def test_linha_partida_entre_leituras_nao_engana(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "debug.log"
            pedacos = [REGISTADO[:40], REGISTADO[40:]]

            def dormir(_s):
                if pedacos:
                    with caminho.open("a", encoding="utf-8") as ficheiro:
                        ficheiro.write(pedacos.pop(0))

            veredito = esperar_registo(caminho, ProcessoFalso(), 5.0, dormir)
            self.assertTrue(veredito.registado)


class _Central(unittest.TestCase):
    PROJETOS = ("exemplo-um", "exemplo-dois")

    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.caminho = Path(pasta.name) / "ipc.json"
        self.logs: list[str] = []
        self.central = CentralDoCanal(
            self.PROJETOS, self.caminho, log=self.logs.append, espera_do_ola_s=0.5
        ).iniciar()
        self.addCleanup(self.central.parar)

    def canal(self, projeto: str = "exemplo-um"):
        recebidos: list[tuple[str, str]] = []
        ligacao = LigacaoAoJarvis(
            projeto,
            lambda texto, pedido: recebidos.append((texto, pedido)),
            self.caminho,
            intervalo_s=0.05,
            avisar=lambda _t: None,
        ).iniciar()
        self.addCleanup(ligacao.parar)
        self.assertTrue(self.central.esperar_ligacao(projeto, 5))
        return ligacao, recebidos

    def cliente_cru(self) -> socket.socket:
        endereco = ler_endereco(self.caminho)
        sock = socket.create_connection(("127.0.0.1", endereco.porta), timeout=5)
        self.addCleanup(canal_mcp._fechar_socket, sock)
        return sock

    def fechada(self, sock: socket.socket) -> bool:
        try:
            return sock.recv(1) == b""
        except OSError:
            return True


class TestCentralDoCanal(_Central):
    def test_endereco_so_local_com_segredo_novo_e_apagado_ao_parar(self):
        endereco = ler_endereco(self.caminho)
        self.assertIsNotNone(endereco)
        self.assertEqual(self.central._servidor.getsockname()[0], "127.0.0.1")
        outra = CentralDoCanal(["x"], Path(self.caminho.parent) / "outro.json", log=lambda _t: None)
        outra.iniciar()
        self.assertNotEqual(outra.endereco.segredo, endereco.segredo)
        outra.parar()
        self.assertFalse((self.caminho.parent / "outro.json").exists())

    def test_ida_e_volta_com_o_canal_real(self):
        ligacao, recebidos = self.canal()
        prompt = "Resume o estado do projeto.\nEm duas frases."
        pedido = self.central.enviar_prompt("exemplo-um", prompt)
        self.assertTrue(_esperar(lambda: recebidos))
        self.assertEqual(recebidos, [(prompt, pedido)])
        texto = "Tudo verde.\n```\nrm -rf /\n```"
        self.assertTrue(ligacao.enviar_resposta(texto, "o canal diz outra coisa", pedido))
        resposta = self.central.esperar_resposta("exemplo-um", pedido, 5)
        self.assertEqual(resposta.texto, texto)
        # o que vai para a voz volta a passar pelo filtro deste lado
        self.assertIn("Tudo verde.", resposta.falado)
        self.assertNotIn("rm -rf", resposta.falado)

    def test_ligacao_sem_segredo_e_fechada_e_nao_conta_como_canal(self):
        sock = self.cliente_cru()
        sock.sendall((json.dumps({"tipo": TIPO_OLA, "projeto": "exemplo-um"}) + "\n").encode())
        self.assertTrue(self.fechada(sock))
        self.assertFalse(self.central.ligado("exemplo-um"))
        self.assertTrue(_esperar(lambda: self.central.recusadas == 1))
        self.assertTrue(any("sem segredo" in linha for linha in self.logs))

    def test_ligacao_com_segredo_errado_e_fechada(self):
        sock = self.cliente_cru()
        sock.sendall(linha_de_mensagem(TIPO_OLA, gerar_segredo(), projeto="exemplo-um"))
        self.assertTrue(self.fechada(sock))
        self.assertFalse(self.central.ligado("exemplo-um"))
        self.assertTrue(any("segredo errado" in linha for linha in self.logs))
        self.assertFalse(any(self.central.endereco.segredo in linha for linha in self.logs))

    def test_projeto_desconhecido_e_cr_lf_no_ola_sao_recusados(self):
        segredo = self.central.endereco.segredo
        for linha in (
            linha_de_mensagem(TIPO_OLA, segredo, projeto="projeto-de-outro"),
            linha_de_mensagem(TIPO_OLA, segredo, projeto="exemplo-um\r\n"),
        ):
            sock = self.cliente_cru()
            sock.sendall(linha)
            self.assertTrue(self.fechada(sock))
        self.assertFalse(self.central.ligado("exemplo-um"))

    def test_ligacao_calada_fecha_no_tempo(self):
        sock = self.cliente_cru()
        self.assertTrue(self.fechada(sock))
        self.assertTrue(any("no tempo" in linha for linha in self.logs))

    def test_depois_do_ola_so_respostas_validas_do_proprio_projeto(self):
        segredo = self.central.endereco.segredo
        sock = self.cliente_cru()
        sock.sendall(linha_de_mensagem(TIPO_OLA, segredo, projeto="exemplo-um"))
        self.assertTrue(self.central.esperar_ligacao("exemplo-um", 5))
        maus = [
            linha_de_mensagem(TIPO_RESPOSTA, segredo, projeto="exemplo-dois", texto="a", falado="a"),
            linha_de_mensagem(TIPO_RESPOSTA, gerar_segredo(), projeto="exemplo-um", texto="b", falado="b"),
            linha_de_mensagem(TIPO_RESPOSTA, segredo, projeto="exemplo-um", texto="c", falado="c\nd"),
            linha_de_mensagem(TIPO_RESPOSTA, segredo, projeto="exemplo-um", texto="e", falado="e", pedido="x\r"),
            (json.dumps({"tipo": TIPO_RESPOSTA, "projeto": "exemplo-um", "texto": "f", "falado": "f"}) + "\n").encode(),
            linha_de_mensagem("prompt", segredo, projeto="exemplo-um", pedido=novo_pedido(), texto="g"),
        ]
        for linha in maus:
            sock.sendall(linha)
        sock.sendall(linha_de_mensagem(TIPO_RESPOSTA, segredo, projeto="exemplo-um", texto="Bom.", falado="Bom."))
        resposta = self.central.esperar_resposta("exemplo-um", None, 5)
        self.assertEqual(resposta.texto, "Bom.")
        self.assertEqual(self.central.recusadas, len(maus))
        self.assertIsNone(self.central.esperar_resposta("exemplo-dois", None, 0.1))
        self.assertIsNone(self.central.esperar_resposta("exemplo-um", None, 0.1))

    def test_enviar_sem_canal_ligado_ou_para_projeto_desconhecido(self):
        with self.assertRaises(CanalIndisponivel):
            self.central.enviar_prompt("exemplo-um", "ola")
        with self.assertRaises(ValueError):
            self.central.enviar_prompt("projeto-de-outro", "ola")
        with self.assertRaises(ValueError):
            self.central.enviar_prompt("exemplo-um", "   ")
        with self.assertRaises(ValueError):
            self.central.enviar_prompt("exemplo-um", "a\x00b")

    def test_prompt_de_um_projeto_nao_chega_ao_canal_de_outro(self):
        _um, recebidos_um = self.canal("exemplo-um")
        _dois, recebidos_dois = self.canal("exemplo-dois")
        self.central.enviar_prompt("exemplo-dois", "so para o dois")
        self.assertTrue(_esperar(lambda: recebidos_dois))
        time.sleep(0.1)
        self.assertEqual(recebidos_um, [])

    def test_ligacao_nova_substitui_a_antiga_e_a_antiga_nao_apaga_a_nova(self):
        segredo = self.central.endereco.segredo
        antigo = self.cliente_cru()
        antigo.sendall(linha_de_mensagem(TIPO_OLA, segredo, projeto="exemplo-um"))
        self.assertTrue(self.central.esperar_ligacao("exemplo-um", 5))
        _novo, recebidos = self.canal("exemplo-um")
        self.assertTrue(self.fechada(antigo))
        time.sleep(0.3)
        self.assertTrue(self.central.ligado("exemplo-um"))
        self.central.enviar_prompt("exemplo-um", "para a ligacao nova")
        self.assertTrue(_esperar(lambda: recebidos))


class CentralFalsa:
    def __init__(self, ligado=True, resposta=None):
        self._ligado = ligado
        self.resposta = resposta
        self.enviados: list[tuple[str, str]] = []

    def esperar_ligacao(self, projeto, limite_s):
        return self._ligado

    def enviar_prompt(self, projeto, texto):
        self.enviados.append((projeto, texto))
        return "a" * 32

    def esperar_resposta(self, projeto, pedido, limite_s):
        return self.resposta


class HeadlessFalso:
    def __init__(self, resposta=("OK", "7d94d31a-f197-4c93-9e07-4e14e7c83d9e", "")):
        self.resposta = resposta
        self.perguntas: list[str] = []
        self.criado_com = None

    def __call__(self, projeto, cli=None):
        self.criado_com = projeto
        return self

    def perguntar(self, texto, limite_s):
        self.perguntas.append(texto)
        return self.resposta

    def fechar(self):
        pass


def _sessao(caminho=CAMINHO_CANAL, motivo="") -> SessaoAberta:
    return SessaoAberta(
        projeto=Projeto(nome="exemplo-um", caminho=Path(tempfile.gettempdir())),
        caminho=caminho,
        motivo=motivo,
        comando=[],
        config_mcp=Path(),
        debug=Path(),
    )


class TestCanalDoProjeto(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []

    def test_pelo_canal(self):
        resposta = sessoes.RespostaDoCanal("exemplo-um", "OK", "Resposta: OK", "", 0.0)
        central = CentralFalsa(resposta=resposta)
        headless = HeadlessFalso()
        entrega = CanalDoProjeto(
            _sessao(), central, criar_headless=headless, log=self.logs.append
        ).entregar("prompt confirmado")
        self.assertEqual(central.enviados, [("exemplo-um", "prompt confirmado")])
        self.assertEqual((entrega.caminho, entrega.texto, entrega.erro), (CAMINHO_CANAL, "OK", ""))
        self.assertEqual(headless.perguntas, [])
        self.assertTrue(entrega.enviado_em and entrega.respondido_em)

    def test_canal_registado_mas_desligado_cai_no_headless_uma_vez(self):
        central = CentralFalsa(ligado=False)
        headless = HeadlessFalso()
        canal = CanalDoProjeto(_sessao(), central, criar_headless=headless, log=self.logs.append, espera_da_ligacao_s=0)
        entrega = canal.entregar("prompt confirmado")
        self.assertEqual(central.enviados, [])
        self.assertEqual(headless.perguntas, ["prompt confirmado"])
        self.assertEqual(entrega.caminho, CAMINHO_HEADLESS)
        self.assertIn("nao se ligou", entrega.motivo)

    def test_sem_resposta_pelo_canal_nao_reenvia_por_outro_caminho(self):
        central = CentralFalsa(resposta=None)
        headless = HeadlessFalso()
        entrega = CanalDoProjeto(
            _sessao(), central, criar_headless=headless, log=self.logs.append
        ).entregar("prompt confirmado", limite_s=1)
        self.assertEqual(len(central.enviados), 1)
        self.assertEqual(headless.perguntas, [])
        self.assertIn("sem resposta", entrega.erro)

    def test_headless_diz_o_caminho_e_como_retomar(self):
        headless = HeadlessFalso()
        canal = CanalDoProjeto(
            _sessao(CAMINHO_HEADLESS, "channels feature is not currently available"),
            CentralFalsa(),
            criar_headless=headless,
            log=self.logs.append,
        )
        entrega = canal.entregar("prompt confirmado")
        self.assertEqual(entrega.caminho, CAMINHO_HEADLESS)
        self.assertEqual(entrega.comando_para_retomar, "claude --resume 7d94d31a-f197-4c93-9e07-4e14e7c83d9e")
        (linha,) = [l for l in self.logs if "caminho=headless" in l]
        self.assertIn("claude --resume 7d94d31a", linha)
        self.assertIn("channels feature", linha)
        frase = entrega.frase_para_o_utilizador()
        self.assertIn("segundo plano", frase)
        self.assertNotIn("7d94d31a", frase)  # um UUID nao se diz em voz alta; fica no ecra

    def test_sem_central_vai_direto_ao_headless(self):
        headless = HeadlessFalso()
        entrega = CanalDoProjeto(_sessao(), None, criar_headless=headless, log=self.logs.append).entregar("x")
        self.assertEqual(entrega.caminho, CAMINHO_HEADLESS)

    def test_erro_do_headless_e_reportado(self):
        headless = HeadlessFalso(("", None, "timeout no arranque"))
        entrega = CanalDoProjeto(
            _sessao(CAMINHO_HEADLESS), None, criar_headless=headless, log=self.logs.append
        ).entregar("x")
        self.assertEqual(entrega.erro, "timeout no arranque")
        self.assertIn("Não consegui", entrega.frase_para_o_utilizador())


class CanalStreamFalso:
    def __init__(self, cwd, argumentos):
        self.cwd = cwd
        self.argumentos = argumentos
        self.aberto = False
        self.session_id = None
        self.sessao_devolvida = "7d94d31a-f197-4c93-9e07-4e14e7c83d9e"

    def abrir(self):
        self.aberto = True
        return self

    def perguntar_detalhado(self, frase, limite_s):
        return RespostaRecolhida(texto="OK", session_id=self.sessao_devolvida, terminou=True)

    def fechar(self):
        self.aberto = False


class TestSessaoHeadless(unittest.TestCase):
    def test_na_pasta_do_projeto_sem_aprovar_ferramentas(self):
        with tempfile.TemporaryDirectory() as pasta:
            projeto = Projeto(nome="exemplo-um", caminho=Path(pasta))
            sessao = SessaoHeadless(projeto, cli=CLI_FALSO, criar_canal=CanalStreamFalso)
            self.assertEqual(sessao.canal.cwd, Path(pasta).resolve())
            argumentos = sessao.canal.argumentos
            self.assertEqual(argumentos[0], CLI_FALSO)
            for par in (("--permission-mode", "manual"), ("--permission-prompts", "none")):
                indice = argumentos.index(par[0])
                self.assertEqual(argumentos[indice + 1], par[1])
            self.assertIn("stream-json", argumentos)
            self.assertNotIn("--dangerously-skip-permissions", argumentos)
            self.assertNotIn("--no-session-persistence", argumentos)
            self.assertEqual(sessao.perguntar("x", 5), ("OK", "7d94d31a-f197-4c93-9e07-4e14e7c83d9e", ""))

    def test_session_id_que_nao_e_uuid_nao_segue(self):
        with tempfile.TemporaryDirectory() as pasta:
            sessao = SessaoHeadless(Projeto("exemplo-um", Path(pasta)), cli=CLI_FALSO, criar_canal=CanalStreamFalso)
            sessao.canal.sessao_devolvida = 'x" & del *'
            self.assertIsNone(sessao.perguntar("x", 5)[1])

    def test_shim_recusado(self):
        with tempfile.TemporaryDirectory() as pasta, self.assertRaises(ValueError):
            SessaoHeadless(Projeto("exemplo-um", Path(pasta)), cli="claude.cmd", criar_canal=CanalStreamFalso)


class TestLinhaDeComandos(unittest.TestCase):
    def _config(self, pasta: Path) -> Config:
        return Config(microfone="x", projetos=(Projeto("exemplo-um", pasta),))

    def test_projeto_desconhecido_nao_abre_nada(self):
        with tempfile.TemporaryDirectory() as pasta, mock.patch.object(
            sessoes, "carregar_config", return_value=self._config(Path(pasta))
        ), mock.patch.object(sessoes, "abrir_sessao") as abrir, contextlib.redirect_stderr(io.StringIO()) as erro:
            self.assertEqual(sessoes.main(["abrir", "exemplo-tres"]), 2)
        abrir.assert_not_called()
        self.assertIn("exemplo-um", erro.getvalue())

    def test_projeto_conhecido_abre_e_diz_o_caminho(self):
        with tempfile.TemporaryDirectory() as pasta:
            config = self._config(Path(pasta))
            sessao = _sessao(CAMINHO_HEADLESS, "channels feature is not currently available")
            with mock.patch.object(sessoes, "carregar_config", return_value=config), mock.patch.object(
                sessoes, "abrir_sessao", return_value=sessao
            ) as abrir, contextlib.redirect_stdout(io.StringIO()) as saida:
                self.assertEqual(sessoes.main(["abrir", "EXEMPLO-UM"]), 0)
            self.assertEqual(abrir.call_args.args[0], config.projetos[0])
            self.assertIn("claude --resume", saida.getvalue())
            self.assertIn("channels feature", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
