r"""Testes dos avisos por voz (jarvis/avisos.py), unittest da biblioteca padrao.

Os eventos sao falsos: o JSON que o Claude Code passaria ao hook e escrito
aqui, o IPC usa sockets reais em 127.0.0.1 com ficheiros de endereco
temporarios e a voz e uma funcao que so regista o texto (nenhum som). O que
protegem:

  * o hook so tira do evento o tipo e o id da sessao, e sai sempre com 0;
  * o settings.json das sessoes liga Stop e Notification (idle_prompt,
    permission_prompt) e fica no repositorio jarvis, nunca no projeto;
  * o IPC recusa um evento sem segredo, com segredo errado, de outro projeto
    ou fora da lista fechada;
  * a fila: os avisos em fila ditos juntos numa frase, nunca por cima do
    utilizador nem de outra resposta, so depois de um tempo sem conversa,
    calado com "cala-te", so no log a dormir, 1 por sessao por minuto, sem
    duplicados;
  * a vigia dos runs FORJA avisa quando um run termina, falha ou bloqueia;
  * do hook a voz em menos de 2 s, e nenhum conteudo tecnico falado.

Corre com:

    .venv\Scripts\python -m unittest tests.test_avisos -v
"""

from __future__ import annotations

import json
import socket
import subprocess
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import avisos, sessoes
from jarvis.avisos import (
    DESCARTADO,
    EVENTO_ACABOU,
    EVENTO_ESPERA,
    FALADO,
    OCUPADO,
    SO_ECRA,
    SONDAGEM_DOS_RUNS_S,
    Avisos,
    VigiaDosRuns,
    config_de_hooks,
    comando_do_hook,
    correr_hook,
    evento_do_hook,
    frase_agrupada,
    frase_do_aviso,
)
from jarvis.persona import Variantes
from jarvis.canal_mcp import TIPO_EVENTO, EnderecoIpc, escrever_endereco, gerar_segredo, linha_de_mensagem
from jarvis.config import Projeto
from jarvis.sessoes import CentralDoCanal
from tests.test_app import DITADO, Montagem, RelogioFalso
from tests.test_sessoes import LancadorFalso, _ComProjetoTemporario

RAIZ = Path(__file__).resolve().parent.parent
SESSAO = "123e4567-e89b-12d3-a456-426614174000"
OUTRA_SESSAO = "9b2f1c3d-0000-4000-8000-00000000abcd"

#: O que uma notificacao real traz: nada disto pode chegar ao jarvis nem a voz.
MENSAGEM_TECNICA = "Claude needs your permission to use Bash: rm -rf C:/Users/x && git push --force"
TRANSCRIPT = "C:/Users/x/.claude/projects/segredo/abc.jsonl"


def _esperar(condicao, limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()


def entrada_do_hook(evento: str = "Stop", **extra) -> dict:
    dados = {
        "hook_event_name": evento,
        "session_id": SESSAO,
        "transcript_path": TRANSCRIPT,
        "cwd": "C:/Users/x/projeto",
    }
    dados.update(extra)
    return dados


class _OuvinteCru:
    """Um "jarvis" que so guarda os bytes que lhe chegam (para ver o que o hook manda)."""

    def __init__(self, caminho: Path) -> None:
        self.segredo = gerar_segredo()
        self.servidor = socket.create_server(("127.0.0.1", 0))
        escrever_endereco(EnderecoIpc(self.servidor.getsockname()[1], self.segredo, 1), caminho)
        self.recebido: list[bytes] = []
        self._fio = threading.Thread(target=self._aceitar, daemon=True)
        self._fio.start()

    def _aceitar(self) -> None:
        self.servidor.settimeout(5)
        try:
            conexao, _ = self.servidor.accept()
        except OSError:
            return
        with conexao:
            dados = b""
            while True:
                pedaco = conexao.recv(65536)
                if not pedaco:
                    break
                dados += pedaco
        self.recebido.append(dados)

    def fechar(self) -> None:
        self.servidor.close()


class _PastaTemporaria(unittest.TestCase):
    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory(prefix="jarvis-avisos-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.ipc = self.pasta / "ipc.json"


# --- Lado do hook ------------------------------------------------------------


class TestEventoDoHook(unittest.TestCase):
    def test_stop_e_a_sessao_acabou(self) -> None:
        self.assertEqual(evento_do_hook(entrada_do_hook("Stop")), (EVENTO_ACABOU, SESSAO))

    def test_idle_prompt_e_permission_prompt_sao_a_espera(self) -> None:
        for tipo in ("idle_prompt", "permission_prompt"):
            with self.subTest(tipo=tipo):
                entrada = entrada_do_hook("Notification", notification_type=tipo, message=MENSAGEM_TECNICA)
                self.assertEqual(evento_do_hook(entrada), (EVENTO_ESPERA, SESSAO))

    def test_outras_notificacoes_e_outros_hooks_nao_avisam(self) -> None:
        for entrada in (
            entrada_do_hook("Notification", notification_type="auth_success"),
            entrada_do_hook("Notification"),
            entrada_do_hook("PreToolUse", tool_name="Bash"),
            entrada_do_hook("SubagentStop"),
            [],
            "Stop",
            None,
        ):
            with self.subTest(entrada=entrada):
                self.assertIsNone(evento_do_hook(entrada))

    def test_sessao_que_nao_e_uuid_vai_vazia(self) -> None:
        for sessao in ("../../x", "abc", SESSAO + "\n", 42):
            with self.subTest(sessao=sessao):
                self.assertEqual(evento_do_hook(entrada_do_hook(session_id=sessao)), (EVENTO_ACABOU, ""))


class TestHookMandaSoOTipoDeEvento(_PastaTemporaria):
    def test_a_linha_do_ipc_so_tem_tipo_segredo_projeto_evento_e_sessao(self) -> None:
        ouvinte = _OuvinteCru(self.ipc)
        self.addCleanup(ouvinte.fechar)
        entrada = entrada_do_hook("Notification", notification_type="permission_prompt", message=MENSAGEM_TECNICA)
        codigo = correr_hook("atlas", json.dumps(entrada).encode("utf-8"), self.ipc)
        self.assertEqual(codigo, 0)
        self.assertTrue(_esperar(lambda: ouvinte.recebido))
        bruto = ouvinte.recebido[0]
        self.assertNotIn(b"rm -rf", bruto)
        self.assertNotIn(b"permission", bruto)
        self.assertNotIn(b"jsonl", bruto)
        mensagem = json.loads(bruto)
        self.assertEqual(set(mensagem), {"tipo", "segredo", "projeto", "evento", "sessao"})
        self.assertEqual(
            (mensagem["tipo"], mensagem["projeto"], mensagem["evento"], mensagem["sessao"]),
            (TIPO_EVENTO, "atlas", EVENTO_ESPERA, SESSAO),
        )
        self.assertEqual(mensagem["segredo"], ouvinte.segredo)

    def test_o_hook_sai_sempre_com_0_e_sem_nada_no_stdout(self) -> None:
        casos = [
            ("atlas", b"isto nao e json"),
            ("atlas", json.dumps(entrada_do_hook()).encode()),  # sem jarvis a correr
            ("nome com \"aspas\"", json.dumps(entrada_do_hook()).encode()),
            ("atlas", b"{" * (avisos.MAXIMO_DA_ENTRADA + 10)),
            ("atlas", b"\xff\xfe"),
        ]
        for projeto, entrada in casos:
            with self.subTest(projeto=projeto, entrada=entrada[:20]):
                self.assertEqual(correr_hook(projeto, entrada, self.ipc), 0)

    def test_o_script_do_hook_nao_escreve_no_stdout(self) -> None:
        # stdout de um hook Stop pode ser lido como decisao pelo Claude Code.
        resultado = subprocess.run(
            [sys.executable, str(avisos.SCRIPT_DO_HOOK), "hook", "--projeto", "atlas", "--ipc", str(self.ipc)],
            input=json.dumps(entrada_do_hook()).encode(),
            capture_output=True,
            timeout=30,
            cwd=self.pasta,
        )
        self.assertEqual((resultado.returncode, resultado.stdout), (0, b""))


# --- settings.json das sessoes -------------------------------------------------


class TestSettingsDosHooks(unittest.TestCase):
    def test_stop_e_notification_so_de_espera(self) -> None:
        hooks = config_de_hooks("atlas", python=sys.executable)["hooks"]
        self.assertEqual(set(hooks), {"Stop", "Notification"})
        self.assertEqual(hooks["Notification"][0]["matcher"], "idle_prompt|permission_prompt")
        for grupo in (hooks["Stop"][0], hooks["Notification"][0]):
            (gancho,) = grupo["hooks"]
            self.assertEqual(gancho["type"], "command")
            self.assertLessEqual(gancho["timeout"], 10)
            self.assertIn(avisos.SCRIPT_DO_HOOK.as_posix(), gancho["command"])
            self.assertTrue(gancho["command"].endswith('"hook" "--projeto" "atlas"'))

    def test_caminhos_ou_nomes_que_um_shell_expandiria_sao_recusados(self) -> None:
        for python in ("C:/x%PATH%/python.exe", "C:/x$HOME/python.exe", 'C:/x"/python.exe', "C:/x!a!/python.exe"):
            with self.subTest(python=python), self.assertRaises(ValueError):
                comando_do_hook("atlas", python=python)
        with self.assertRaises(ValueError):
            comando_do_hook("atlas", python="C:/x/python.cmd")
        for nome in ('atlas" && calc', "atlas$(calc)", "atlas`calc`", "atlas\nx"):
            with self.subTest(nome=nome), self.assertRaises(ValueError):
                comando_do_hook(nome, python=sys.executable)

    def test_o_exemplo_versionado_tem_a_mesma_forma_e_sem_caminhos_reais(self) -> None:
        exemplo = json.loads((RAIZ / "config" / "hooks-jarvis.exemplo.json").read_text(encoding="utf-8"))
        gerado = config_de_hooks("exemplo-um", python=sys.executable)
        self.assertEqual(set(exemplo["hooks"]), set(gerado["hooks"]))
        self.assertEqual(exemplo["hooks"]["Notification"][0]["matcher"], gerado["hooks"]["Notification"][0]["matcher"])
        texto = json.dumps(exemplo)
        self.assertIn("C:/caminho/para/jarvis", texto)
        self.assertNotIn(str(RAIZ.as_posix()), texto)


class TestSessaoArrancaComOsHooks(_ComProjetoTemporario):
    def test_settings_no_repositorio_jarvis_e_nada_no_projeto(self) -> None:
        antes = sessoes.arvore(self.pasta_projeto)
        lancador = LancadorFalso()
        self.abrir(lancador)
        self.assertEqual(antes, sessoes.arvore(self.pasta_projeto), "a pasta do projeto nao pode mudar")
        ((argumentos, _cwd, _env),) = lancador.chamadas
        caminho = Path(argumentos[argumentos.index("--settings") + 1])
        self.assertTrue(caminho.resolve().is_relative_to(RAIZ / ".jarvis"), caminho)
        self.assertFalse(caminho.resolve().is_relative_to(Path.home() / ".claude"))
        settings = json.loads(caminho.read_text(encoding="utf-8"))
        comando = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(f'"--projeto" "{self.nome}"', comando)


# --- IPC: o evento chega autenticado ou nao chega ------------------------------


class TestEventoNoIpc(_PastaTemporaria):
    def setUp(self) -> None:
        super().setUp()
        self.eventos: list[tuple[str, str, str]] = []
        self.logs: list[str] = []
        self.central = CentralDoCanal(
            ["atlas", "orbita"], self.ipc, log=self.logs.append, ao_evento=lambda *e: self.eventos.append(e)
        ).iniciar()
        self.addCleanup(self.central.parar)

    def mandar(self, linha: bytes) -> None:
        with socket.create_connection(("127.0.0.1", self.central.endereco.porta), timeout=2) as sock:
            sock.sendall(linha)
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(2)
            try:
                while sock.recv(1024):
                    pass
            except OSError:
                pass

    def test_evento_com_segredo_chega(self) -> None:
        self.assertTrue(avisos.enviar_evento("atlas", EVENTO_ACABOU, SESSAO, self.ipc))
        self.assertTrue(_esperar(lambda: self.eventos))
        self.assertEqual(self.eventos, [("atlas", EVENTO_ACABOU, SESSAO)])

    def test_evento_sem_segredo_e_recusado(self) -> None:
        linha = (json.dumps({"tipo": TIPO_EVENTO, "projeto": "atlas", "evento": EVENTO_ACABOU}) + "\n").encode()
        self.mandar(linha)
        self.assertTrue(_esperar(lambda: self.central.recusadas == 1))
        self.assertEqual(self.eventos, [])
        self.assertTrue(any("sem segredo" in linha for linha in self.logs))

    def test_eventos_maus_sao_recusados_e_nunca_chegam(self) -> None:
        s = self.central.endereco.segredo
        maus = [
            linha_de_mensagem(TIPO_EVENTO, gerar_segredo(), projeto="atlas", evento=EVENTO_ACABOU),
            linha_de_mensagem(TIPO_EVENTO, s, projeto="desconhecido", evento=EVENTO_ACABOU),
            linha_de_mensagem(TIPO_EVENTO, s, projeto="atlas", evento="rm -rf /"),
            linha_de_mensagem(TIPO_EVENTO, s, projeto="atlas", evento=EVENTO_ACABOU, sessao="../x"),
            linha_de_mensagem(TIPO_EVENTO, s, projeto="atlas", evento=EVENTO_ESPERA, mensagem=MENSAGEM_TECNICA),
            linha_de_mensagem(TIPO_EVENTO, s, projeto="atlas\n", evento=EVENTO_ACABOU),
        ]
        for linha in maus:
            self.mandar(linha)
        self.assertTrue(_esperar(lambda: self.central.recusadas == len(maus)))
        self.assertEqual(self.eventos, [])
        self.assertNotIn(MENSAGEM_TECNICA, "\n".join(self.logs))

    def test_um_evento_nao_abre_uma_ligacao_de_canal(self) -> None:
        avisos.enviar_evento("atlas", EVENTO_ESPERA, "", self.ipc)
        self.assertTrue(_esperar(lambda: self.eventos))
        self.assertFalse(self.central.ligado("atlas"))


# --- A fila --------------------------------------------------------------------


class _Voz:
    """Faz de jarvis: responde com o desfecho escolhido e guarda o que foi dito."""

    def __init__(self) -> None:
        self.desfecho = FALADO
        self.ditos: list[str] = []

    def __call__(self, grupo) -> str:
        if self.desfecho == FALADO:
            self.ditos.append(frase_agrupada(grupo))
        return self.desfecho


def formas_inglesas(evento: str, projeto: str) -> set[str]:
    return {forma.format(p=projeto) for forma in avisos._FRASES["en"][evento]}


class TestFilaDeAvisos(unittest.TestCase):
    def setUp(self) -> None:
        self.relogio = RelogioFalso()
        self.voz = _Voz()
        self.linhas: list[str] = []
        self.avisos = Avisos(self.voz, escrever=self.linhas.append, relogio=self.relogio)

    def test_aviso_dito_com_a_frase_fixa(self) -> None:
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))
        self.assertEqual(self.avisos.processar(), FALADO)
        self.assertIsNone(self.avisos.processar())
        self.assertEqual(self.voz.ditos, ["atlas acabou."])

    def test_varios_avisos_ditos_juntos_numa_frase(self) -> None:
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))
        self.assertTrue(self.avisos.receber("orbita", EVENTO_ESPERA, OUTRA_SESSAO))
        self.assertEqual(self.avisos.processar(), FALADO)
        self.assertIsNone(self.avisos.processar())
        self.assertEqual(self.voz.ditos, ["Entretanto, atlas acabou e orbita está à espera de ti."])

    def test_em_ingles(self) -> None:
        variantes = Variantes()
        self.assertEqual(frase_do_aviso(EVENTO_ACABOU, "atlas", "en", variantes), "atlas is done.")
        self.assertEqual(frase_do_aviso(EVENTO_ESPERA, "atlas", "en", variantes), "atlas is waiting for you.")

    def test_em_ingles_nunca_repete_a_mesma_forma_seguida(self) -> None:
        variantes = Variantes(random.Random(7))
        for evento in (EVENTO_ACABOU, EVENTO_ESPERA, avisos.RUN_TERMINADO, avisos.RUN_FALHOU, avisos.RUN_BLOQUEADO):
            with self.subTest(evento=evento):
                ditas = [frase_do_aviso(evento, "atlas", "en", variantes) for _ in range(30)]
                self.assertTrue(all(a != b for a, b in zip(ditas, ditas[1:])), ditas)
                self.assertGreaterEqual(len(set(ditas)), 2)
                self.assertTrue(all("atlas" in dita for dita in ditas))

    def test_ocupado_espera_na_fila_pela_ordem(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.avisos.receber("orbita", EVENTO_ACABOU, OUTRA_SESSAO)
        self.voz.desfecho = OCUPADO
        for _ in range(3):
            self.assertEqual(self.avisos.processar(), OCUPADO)
        self.assertEqual((self.voz.ditos, self.avisos.em_fila), ([], 2))
        self.voz.desfecho = FALADO
        self.assertEqual(self.avisos.processar(), FALADO)
        self.assertEqual(self.voz.ditos, ["Entretanto, atlas acabou e orbita acabou."])

    def test_limite_de_um_por_sessao_por_minuto(self) -> None:
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))
        self.relogio.avancar(20)
        self.assertFalse(self.avisos.receber("atlas", EVENTO_ESPERA, SESSAO))
        self.assertFalse(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ESPERA, OUTRA_SESSAO), "outra sessao tem o seu limite")
        self.relogio.avancar(40.1)
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ESPERA, SESSAO))
        self.assertEqual(self.avisos.em_fila, 3)
        self.assertTrue(any("limite de 1 por minuto" in linha for linha in self.linhas))

    def test_limite_tambem_para_o_run(self) -> None:
        self.assertTrue(self.avisos.receber_run("atlas", "F-1-a", "done"))
        self.assertFalse(self.avisos.receber_run("atlas", "F-1-a", "blocked"))
        self.assertTrue(self.avisos.receber_run("atlas", "F-2-b", "blocked"))
        self.assertFalse(self.avisos.receber_run("atlas", "F-3-c", "running"))

    def test_stop_logo_depois_da_resposta_lida_nao_repete(self) -> None:
        self.avisos.houve_resposta("atlas")
        self.relogio.avancar(3)
        self.assertFalse(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ESPERA, SESSAO), "a espera avisa sempre")
        self.relogio.avancar(avisos.SILENCIO_DEPOIS_DA_RESPOSTA_S + 1)
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ACABOU, OUTRA_SESSAO))

    def test_evento_fora_da_lista_e_recusado(self) -> None:
        self.assertFalse(self.avisos.receber("atlas", "apagar tudo", SESSAO))
        self.assertEqual(self.avisos.em_fila, 0)

    def test_aviso_sem_vez_em_5_minutos_expira(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.voz.desfecho = OCUPADO
        self.avisos.processar()
        self.relogio.avancar(avisos.VALIDADE_DO_AVISO_S + 1)
        self.assertEqual(self.avisos.processar(), "expirado")
        self.assertEqual(self.voz.ditos, [])

    def test_descartar_esvazia_a_fila(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.avisos.receber("orbita", EVENTO_ACABOU, OUTRA_SESSAO)
        self.assertEqual(self.avisos.descartar("cala-te"), 2)
        self.assertIsNone(self.avisos.processar())

    def test_sem_duplicados_na_fila(self) -> None:
        self.avisos.intervalo_s = 0.0
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))
        self.relogio.avancar(90)
        self.assertFalse(self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO), "o mesmo aviso ainda em fila")
        self.assertTrue(self.avisos.receber("atlas", EVENTO_ESPERA, SESSAO))
        self.assertTrue(self.avisos.receber_resposta("atlas"))
        self.assertFalse(self.avisos.receber_resposta("atlas"))
        self.assertEqual(self.avisos.em_fila, 3)
        self.assertTrue(any("ja esta na fila" in linha for linha in self.linhas))

    def test_fila_com_limite_sai_o_mais_antigo(self) -> None:
        fila = Avisos(self.voz, escrever=self.linhas.append, relogio=self.relogio, maximo=2)
        fila.receber("atlas", EVENTO_ACABOU, SESSAO)
        fila.receber("orbita", EVENTO_ACABOU, SESSAO)
        fila.receber_run("jarvis", "F-1-a", "done")
        self.assertEqual(fila.processar(), FALADO)
        self.assertEqual(self.voz.ditos, ["Entretanto, orbita acabou e o run do jarvis terminou."])

    def test_frase_agrupada_nomeia_tres_e_conta_o_resto(self) -> None:
        for projeto in ("a1", "a2", "a3", "a4", "a5"):
            self.avisos.receber(projeto, EVENTO_ACABOU, SESSAO)
        self.avisos.lingua = "en"
        grupo = [aviso for aviso in self.avisos._fila]
        self.assertEqual(frase_agrupada(grupo, "en"), "Meanwhile, a1 is done, a2 is done, a3 is done and 2 more.")
        self.assertEqual(frase_agrupada(grupo[:2], "pt"), "Entretanto, a1 acabou e a2 acabou.")
        self.assertEqual(frase_agrupada([], "en"), "")

    def test_resposta_a_meio_da_conversa_so_o_aviso(self) -> None:
        fila = Avisos(self.voz, lingua="en", escrever=self.linhas.append, relogio=self.relogio)
        self.assertTrue(fila.receber_resposta("atlas"))
        self.assertTrue(fila.receber_resposta("orbita", falhou=True))
        grupo = list(fila._fila)
        self.assertEqual(
            frase_agrupada(grupo, "en"), "Meanwhile, there's a reply from atlas on screen and orbita didn't answer."
        )
        self.assertIn(frase_agrupada(grupo[:1], "en"), formas_inglesas(avisos.EVENTO_RESPOSTA, "atlas"))
        self.assertEqual(fila.processar(), FALADO)
        self.assertEqual(self.voz.ditos, ["Entretanto, há uma resposta do atlas no ecrã e o orbita não respondeu."])

    def test_passados_ao_cerebro_saem_da_fila_e_nunca_sao_ditos(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.relogio.avancar(3)
        self.avisos.receber_run("orbita", "F-1-a", "failed")
        tirados = self.avisos.para_o_cerebro()
        self.assertEqual([aviso.projeto for aviso in tirados], ["atlas", "orbita"])
        self.assertEqual(
            [self.avisos.como_dados(aviso) for aviso in tirados],
            [
                {"project": "atlas", "notice": "session_finished", "age_s": 3},
                {"project": "orbita", "notice": "run_failed", "age_s": 0},
            ],
        )
        self.assertEqual(self.avisos.para_o_cerebro(), [], "uma unica vez")
        self.assertIsNone(self.avisos.processar(), "a caminho do cerebro nunca sao ditos")
        self.avisos.confirmar(tirados)
        self.assertIsNone(self.avisos.processar())
        self.assertEqual(self.voz.ditos, [])
        self.assertEqual(len(self.avisos.para_a_ferramenta()), 2, "a ferramenta ainda os devolve")

    def test_turno_sem_resposta_devolve_os_avisos_so_para_serem_ditos(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        tirados = self.avisos.para_o_cerebro()
        self.avisos.receber("orbita", EVENTO_ACABOU, OUTRA_SESSAO)
        self.avisos.devolver(tirados, vistos=True)
        self.assertEqual(self.avisos.em_fila, 2)
        self.assertEqual([aviso.projeto for aviso in self.avisos.para_o_cerebro()], ["orbita"], "atlas ja foi visto")
        self.assertEqual(self.avisos.processar(), FALADO)
        self.assertEqual(self.voz.ditos, ["atlas acabou."])
        self.assertIn("so para serem ditos", " ".join(self.linhas))

    def test_so_ate_ao_maximo_de_cada_vez(self) -> None:
        for numero in range(5):
            self.avisos.receber_run(f"p{numero}", f"F-{numero}", "done")
        self.assertEqual([aviso.projeto for aviso in self.avisos.para_o_cerebro(3)], ["p0", "p1", "p2"])
        self.assertEqual(self.avisos.em_fila, 2)

    def test_repetido_enquanto_a_caminho_do_cerebro(self) -> None:
        self.avisos.receber_run("orbita", "F-1", "blocked")
        self.avisos.para_o_cerebro()
        self.relogio.avancar(avisos.INTERVALO_POR_SESSAO_S + 1)
        self.assertFalse(self.avisos.receber_run("orbita", "F-1", "blocked"))

    def test_confirmados_depois_de_cala_te_nao_ficam(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        tirados = self.avisos.para_o_cerebro()
        self.relogio.avancar(1)
        self.avisos.descartar("cala-te")
        self.avisos.confirmar(tirados)
        self.assertEqual(self.avisos.para_a_ferramenta(), [])

    def test_o_grupo_a_ser_dito_nunca_vai_ao_cerebro(self) -> None:
        levados: list = []

        def entregar(grupo) -> str:
            # A meio da fala, um turno do cerebro comeca: nao pode levar o que se esta a dizer.
            levados.extend(fila.para_o_cerebro())
            levados.extend(fila.para_a_ferramenta())
            return FALADO

        fila = Avisos(entregar, escrever=lambda _l: None, relogio=self.relogio)
        fila.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.assertEqual(fila.processar(), FALADO)
        self.assertEqual(levados, [])
        self.assertEqual(fila.em_fila, 0)

    def test_ocupado_o_grupo_volta_a_poder_ir_ao_cerebro(self) -> None:
        self.voz.desfecho = OCUPADO
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.assertEqual(self.avisos.processar(), OCUPADO)
        self.assertEqual([aviso.projeto for aviso in self.avisos.para_o_cerebro()], ["atlas"])

    def test_devolvidos_quando_a_mensagem_nao_chegou_ao_cerebro(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        tirados = self.avisos.para_o_cerebro()
        self.avisos.receber("orbita", EVENTO_ACABOU, OUTRA_SESSAO)
        self.avisos.devolver(tirados)
        self.assertEqual(self.avisos.para_a_ferramenta(), [
            {"project": "atlas", "notice": "session_finished", "age_s": 0},
            {"project": "orbita", "notice": "session_finished", "age_s": 0},
        ])

    def test_depois_de_cala_te_os_devolvidos_nao_voltam(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        tirados = self.avisos.para_o_cerebro()
        self.relogio.avancar(1)
        self.avisos.descartar("cala-te")
        self.avisos.devolver(tirados)
        self.assertEqual(self.avisos.em_fila, 0)
        self.assertEqual(self.avisos.para_a_ferramenta(), [])

    def test_expirados_nao_vao_ao_cerebro(self) -> None:
        self.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.relogio.avancar(avisos.VALIDADE_DO_AVISO_S + 1)
        self.assertEqual(self.avisos.para_o_cerebro(), [])
        self.assertEqual(self.avisos.entregues[-1][1], avisos.EXPIRADO)

    def test_a_thread_fala_logo_que_o_jarvis_fica_livre(self) -> None:
        voz = _Voz()
        voz.desfecho = OCUPADO
        fila = Avisos(voz, escrever=lambda _l: None, passo_s=0.01).iniciar()
        self.addCleanup(fila.parar)
        fila.receber("atlas", EVENTO_ACABOU, SESSAO)
        time.sleep(0.1)
        self.assertEqual(voz.ditos, [])
        voz.desfecho = FALADO
        self.assertTrue(_esperar(lambda: voz.ditos == ["atlas acabou."], 2.0))


# --- A fila ligada ao jarvis ---------------------------------------------------


class _OuvidoOcupado:
    def __init__(self, ocupado: bool) -> None:
        self.ocupado = ocupado


class TestAvisosNoJarvis(unittest.TestCase):
    def test_jarvis_livre_diz_o_aviso(self) -> None:
        m = Montagem()
        m.jarvis.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.assertEqual(m.jarvis.avisos.processar(), FALADO)
        self.assertEqual(m.falados, ["atlas acabou."])

    def test_nao_fala_por_cima_do_utilizador(self) -> None:
        m = Montagem()
        m.jarvis.ouvido = _OuvidoOcupado(True)
        m.jarvis.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.assertEqual(m.jarvis.avisos.processar(), OCUPADO)
        m.jarvis.ouvido.ocupado = False
        self.assertEqual(m.jarvis.avisos.processar(), OCUPADO, "ainda dentro da espera sem conversa")
        m.avancar(m.jarvis.espera_dos_avisos_s)
        self.assertEqual(m.jarvis.avisos.processar(), FALADO)
        self.assertEqual(m.falados, ["atlas acabou."])

    def test_nao_fala_por_cima_de_outra_resposta_nem_de_uma_frase_a_ser_tratada(self) -> None:
        m = Montagem()
        m.jarvis.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        a_falar, pode_acabar = threading.Event(), threading.Event()

        def resposta_longa() -> None:
            with m.jarvis._tranca:  # como _ao_responder ou o tratamento de uma frase
                a_falar.set()
                pode_acabar.wait(5)

        fio = threading.Thread(target=resposta_longa)
        fio.start()
        self.assertTrue(a_falar.wait(5))
        self.assertEqual(m.jarvis.avisos.processar(), OCUPADO)
        pode_acabar.set()
        fio.join(5)
        self.assertEqual(m.jarvis.avisos.processar(), FALADO)

    def test_espera_que_o_recap_seja_respondido(self) -> None:
        m = Montagem([DITADO])
        m.ouvir("diz ao atlas para corrigir o teste do login")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        falados = len(m.falados)
        m.jarvis.avisos.receber("orbita", EVENTO_ACABOU, SESSAO)
        self.assertEqual(m.jarvis.avisos.processar(), OCUPADO)
        self.assertEqual(len(m.falados), falados, "nada dito entre o recap e a resposta")
        m.avancar(1)
        m.ouvir("cancela")
        self.assertEqual(m.jarvis.avisos.processar(), OCUPADO, "ainda dentro da espera sem conversa")
        m.avancar(m.jarvis.espera_dos_avisos_s)
        self.assertEqual(m.jarvis.avisos.processar(), FALADO)
        self.assertEqual(m.falados[-1], "orbita acabou.")

    def test_cala_te_deita_fora_os_avisos_e_calado_so_vai_ao_ecra(self) -> None:
        m = Montagem()
        m.jarvis.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        m.ouvir("cala-te")
        self.assertEqual(m.jarvis.avisos.em_fila, 0)
        self.assertTrue(m.jarvis.estado.mudo)
        falados = list(m.falados)
        m.jarvis.avisos.receber("orbita", EVENTO_ESPERA, SESSAO)
        self.assertEqual(m.jarvis.avisos.processar(), SO_ECRA)
        self.assertEqual(m.falados, falados)
        self.assertIn("orbita está à espera de ti.", m.log.texto())

    def test_a_dormir_nao_fala(self) -> None:
        m = Montagem()
        m.ouvir("dorme")
        falados = list(m.falados)
        m.jarvis.avisos.receber("atlas", EVENTO_ACABOU, SESSAO)
        self.assertEqual(m.jarvis.avisos.processar(), DESCARTADO)
        self.assertEqual(m.falados, falados)

    def test_stop_depois_de_uma_resposta_do_canal_nao_repete(self) -> None:
        m = Montagem()
        from jarvis.sessoes import Entrega

        m.jarvis._ao_responder("atlas", Entrega(projeto="atlas", caminho="canal", texto="Feito."))
        self.assertFalse(m.jarvis.avisos.receber("atlas", EVENTO_ACABOU, SESSAO))


class TestDoHookAVozEmMenosDe2s(_PastaTemporaria):
    def test_hook_real_ate_a_voz(self) -> None:
        m = Montagem()
        m.jarvis.avisos.passo_s = 0.01
        m.jarvis.avisos.iniciar()
        self.addCleanup(m.jarvis.avisos.parar)
        falado_em: list[float] = []
        falar_original = m.jarvis._falar

        def falar(texto):
            falado_em.append(time.monotonic())
            return falar_original(texto)

        m.jarvis._falar = falar
        central = CentralDoCanal(["atlas", "orbita"], self.ipc, log=lambda _l: None, ao_evento=m.jarvis.avisos.receber)
        central.iniciar()
        self.addCleanup(central.parar)

        entrada = entrada_do_hook("Notification", notification_type="idle_prompt", message=MENSAGEM_TECNICA)
        inicio = time.monotonic()
        resultado = subprocess.run(
            [sys.executable, str(avisos.SCRIPT_DO_HOOK), "hook", "--projeto", "atlas", "--ipc", str(self.ipc)],
            input=json.dumps(entrada).encode(),
            capture_output=True,
            timeout=30,
            cwd=self.pasta,
        )
        self.assertEqual(resultado.returncode, 0)
        self.assertTrue(_esperar(lambda: falado_em, 3.0))
        self.assertLessEqual(falado_em[0] - inicio, 2.0, "do hook a voz em <= 2 s")
        self.assertEqual(m.falados, ["atlas está à espera de ti."])
        # nenhum conteudo tecnico falado nem escrito
        self.assertNotIn("rm -rf", m.log.texto())
        self.assertNotIn(TRANSCRIPT, m.log.texto())


# --- Vigia dos runs FORJA -------------------------------------------------------


class _LeitorDeRuns:
    def __init__(self, sequencia) -> None:
        self.sequencia = list(sequencia)
        self.lidos: list[str] = []

    def __call__(self, projeto):
        self.lidos.append(projeto.nome)
        item = self.sequencia.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run(run: str, estado: str):
    return SimpleNamespace(run=run, estado=estado)


class TestVigiaDosRuns(unittest.TestCase):
    def setUp(self) -> None:
        self.projetos = (Projeto("atlas", Path("D:/caminho/para/atlas")), Projeto("orbita", Path("D:/caminho/para/orbita")))
        self.mudancas: list[tuple[str, str, str]] = []

    def vigia(self, leitor, tem_run=lambda _c: True) -> VigiaDosRuns:
        return VigiaDosRuns(
            self.projetos[:1], leitor, lambda *m: self.mudancas.append(m), escrever=lambda _l: None, tem_run=tem_run
        )

    def test_sondagem_de_30_s_no_maximo(self) -> None:
        self.assertLessEqual(SONDAGEM_DOS_RUNS_S, 30.0)

    def test_avisa_quando_o_run_termina_ou_bloqueia(self) -> None:
        leitor = _LeitorDeRuns(
            [
                (_run("F-1-a", "running"), None),
                (_run("F-1-a", "running"), None),
                (_run("F-1-a", "blocked"), None),
                (_run("F-1-a", "blocked"), None),
                (_run("F-1-a", "running"), None),
                (_run("F-1-a", "done"), None),
            ]
        )
        vigia = self.vigia(leitor)
        for _ in range(6):
            vigia.sondar()
        self.assertEqual(self.mudancas, [("atlas", "F-1-a", "blocked"), ("atlas", "F-1-a", "done")])

    def test_primeira_leitura_e_referencia_e_falhas_nao_mudam_nada(self) -> None:
        falha = SimpleNamespace(tipo="esgotado")
        leitor = _LeitorDeRuns(
            [
                (_run("F-1-a", "done"), None),
                (None, falha),
                (None, None),
                OSError("node caiu"),
                (_run("F-1-a", "done"), None),
                (_run("F-2-b", "failed"), None),
            ]
        )
        vigia = self.vigia(leitor)
        for _ in range(6):
            vigia.sondar()
        self.assertEqual(self.mudancas, [("atlas", "F-2-b", "failed")])

    def test_projeto_sem_run_nem_e_sondado(self) -> None:
        leitor = _LeitorDeRuns([])
        self.vigia(leitor, tem_run=lambda _c: False).sondar()
        self.assertEqual(leitor.lidos, [])

    def test_do_run_a_voz(self) -> None:
        m = Montagem()
        leitor = _LeitorDeRuns([(_run("F-1-a", "running"), None), (_run("F-1-a", "done"), None)])
        vigia = VigiaDosRuns(
            self.projetos[:1], leitor, m.jarvis.avisos.receber_run, escrever=lambda _l: None, tem_run=lambda _c: True
        )
        vigia.sondar()
        vigia.sondar()
        m.jarvis.avisos.processar()
        self.assertEqual(m.falados, ["O run do atlas terminou."])


if __name__ == "__main__":
    unittest.main()
