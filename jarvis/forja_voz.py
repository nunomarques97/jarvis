r"""Runs FORJA por voz: ver o estado, ler o relatorio, lancar, retomar e parar.

So estas cinco coisas existem por voz, e as tres que mudam alguma coisa so
chegam aqui depois do "sim" ao recap:

  lancar   node <forja>/bin/forja.mjs start --provider <p> --config <perfil> --goal <objetivo>
           com o objetivo ditado e confirmado e o perfil do config.toml.
           Recusado, com o motivo dito, se ja ha um run ativo no projeto ou
           se a arvore tem alteracoes por guardar (nunca com --allow-dirty).
  retomar  node <forja>/bin/forja.mjs core resume
  parar    interrompe o controlador que o proprio jarvis lancou ou retomou,
           como um Ctrl+C na janela dele: a FORJA para o worker, guarda o
           trabalho e deixa o run pronto para "retoma o run".

abandon, retry, commit, push, deliver, apagar e ordens financeiras nao
existem aqui: os argumentos de cada comando sao fixos e so o objetivo, um
unico argumento, vem da voz.

Os subprocessos correm com argv em lista, sem shell, com cwd = pasta do
projeto no config.toml. O controlador corre sem janela e escreve em
`logs/forja/` (pasta ignorada pelo Git); fechar o jarvis nao o para.

Uso interno (a interrupcao corre num processo a parte, para o Ctrl+C nunca
chegar a consola do jarvis):

    python -m jarvis.forja_voz --ctrl-c <pid>
"""

from __future__ import annotations

import datetime
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from jarvis.persona import VARIANTES
from jarvis.canal_claude import ambiente_para_filho, verificar_executavel_seguro
from jarvis.config import Config, ConfigForja, Projeto
from jarvis.estado import (
    LIMITE_DO_CLI_S,
    Estado,
    Falha,
    Resposta,
    SaidaDoCli,
    _curto,
    correr_cli,
    localizar_git,
)

RAIZ = Path(__file__).resolve().parent.parent

#: Os logs dos controladores lancados por voz (pasta ignorada pelo Git).
PASTA_DE_LOGS = RAIZ / "logs" / "forja"

#: As intencoes que este modulo trata. Lista fechada: nada mais chega a FORJA.
INTENCOES_DE_ESTADO = ("estado", "ler_relatorio")
INTENCOES_DA_FORJA = ("lancar_run", "retomar_run", "parar_run")
INTENCOES_POR_VOZ = INTENCOES_DE_ESTADO + INTENCOES_DA_FORJA

#: Os subcomandos da FORJA que o jarvis alguma vez corre.
SUBCOMANDOS_PERMITIDOS = (("core", "status"), ("start",), ("core", "resume"))

#: Tamanho maximo do objetivo ditado passado em --goal.
MAXIMO_DO_OBJETIVO = 2000

#: Quanto tempo se espera, depois de lancar, para ver se o controlador
#: recusou logo (run ativo, arvore suja, decisao pendente, ...).
ESPERA_INICIAL_S = 2.0
#: Quanto tempo se espera que o controlador interrompido feche.
ESPERA_DA_PARAGEM_S = 10.0
#: Prazo do processo auxiliar que envia o Ctrl+C.
LIMITE_DA_INTERRUPCAO_S = 10.0
#: O que se le, no maximo, do fim do log de um controlador que nao arrancou.
MAXIMO_DO_LOG = 16 * 1024

_CRIAR_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NOVO_GRUPO = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


_FRASES = {
    "pt": {
        "sem_forja": "A FORJA não está configurada no jarvis. Não fiz nada.",
        "projeto_desconhecido": "Não conheço esse projeto. Não fiz nada.",
        "objetivo_vazio": "Falta o objetivo do run. Não lancei nada.",
        "objetivo_invalido": "Esse objetivo não pode seguir para a FORJA. Não lancei nada.",
        "run_ativo": "Não lancei: já há um run ativo no {p}.",
        "run_por_acabar": "Não lancei: o run do {p} ainda não acabou; retoma-o ou fecha-o primeiro.",
        "alteracoes": "Não lancei: o {p} tem alterações por guardar.",
        "sem_git": "Não lancei: não consegui ver as alterações do {p} no Git.",
        "nao_confirmei": "Não lancei: não consegui confirmar que o {p} não tem um run ativo. {motivo}",
        "lancado": "Lancei o run no {p}. Pergunta-me pelo estado quando quiseres.",
        "retomado": "Retomei o run do {p}.",
        "nao_arrancou": "O run do {p} não arrancou: {motivo}",
        "falhou_arranque": "Não consegui arrancar a FORJA no {p}.",
        "sem_run": "Não há nenhum run no {p} para retomar.",
        "run_terminado": "O run do {p} já acabou; não há nada para retomar.",
        "a_correr": "O run do {p} já está a correr.",
        "decisao_pendente": "O run do {p} está à espera de uma decisão tua de tecnologia; decide primeiro e depois retoma.",
        "nao_confirmei_retoma": "Não retomei: não consegui ler o estado do run do {p}. {motivo}",
        "nao_e_meu": "Só paro um run que eu tenha lançado ou retomado, e no {p} não há nenhum a correr.",
        "paragem_falhou": "O pedido para parar o run do {p} falhou; o run continua.",
        "parado": "Parei o run do {p}. O trabalho fica guardado; diz retoma o run quando quiseres.",
        "a_parar": "Pedi ao run do {p} para parar; ainda está a fechar.",
        "motivo_alteracoes": "há alterações por guardar.",
        "motivo_run_ativo": "já há um run ativo.",
        "motivo_run_terminado": "o run já tinha acabado.",
        "motivo_decisao_pendente": "falta uma decisão de tecnologia.",
        "motivo_nao_e_raiz": "a pasta não é a raiz do repositório Git.",
        "motivo_sem_run": "não há run para retomar.",
        "motivo_erro_forja": "a FORJA deu um erro; os detalhes estão no ecrã.",
    },
    "en": {
        "sem_forja": (
            "FORJA isn't set up in jarvis, so I didn't do anything.",
            "FORJA isn't configured here; I didn't do anything.",
        ),
        "projeto_desconhecido": (
            "I don't know that project, so I didn't do anything.",
            "That's not a project I know; I didn't do anything.",
        ),
        "objetivo_vazio": ("The run needs a goal, so I didn't start it.", "I need a goal for the run; nothing started."),
        "objetivo_invalido": (
            "That goal can't go to FORJA, so I didn't start anything.",
            "I can't pass that goal to FORJA; nothing started.",
        ),
        "run_ativo": (
            "{p} already has an active run, so I didn't start another.",
            "There's already a run going on {p}; I didn't start another.",
        ),
        "run_por_acabar": (
            "I didn't start: the run on {p} isn't finished; resume or close it first.",
            "{p} has an unfinished run; resume or close it first.",
        ),
        "alteracoes": ("I didn't start: {p} has uncommitted changes.", "{p} has uncommitted changes, so I didn't start."),
        "sem_git": (
            "I didn't start: I couldn't check the Git changes of {p}.",
            "I couldn't check {p} in Git, so I didn't start.",
        ),
        "nao_confirmei": (
            "I didn't start: I couldn't confirm {p} has no active run. {motivo}",
            "I couldn't confirm {p} is free of runs, so I didn't start. {motivo}",
        ),
        "lancado": (
            "Run started on {p}. Ask me for the status any time.",
            "The {p} run is going. Ask me how it's doing whenever you like.",
        ),
        "retomado": ("Resumed the run on {p}.", "The {p} run is going again."),
        "nao_arrancou": ("The run on {p} didn't start: {motivo}", "{p}'s run failed to start: {motivo}"),
        "falhou_arranque": ("I couldn't start FORJA on {p}.", "FORJA wouldn't start on {p}."),
        "sem_run": ("There's no run on {p} to resume.", "{p} has no run to resume."),
        "run_terminado": (
            "The run on {p} already ended; there's nothing to resume.",
            "{p}'s run is already over, so there's nothing to resume.",
        ),
        "a_correr": ("The run on {p} is already running.", "{p}'s run is already going."),
        "decisao_pendente": (
            "The run on {p} needs your technology decision first; decide, then resume.",
            "{p}'s run is waiting for your technology decision; decide first, then resume.",
        ),
        "nao_confirmei_retoma": (
            "I didn't resume: I couldn't read the run status of {p}. {motivo}",
            "I couldn't read {p}'s run status, so I didn't resume. {motivo}",
        ),
        "nao_e_meu": (
            "I only stop runs I started or resumed, and none is running on {p}.",
            "No run of mine is going on {p}, and I only stop my own.",
        ),
        "paragem_falhou": (
            "Stopping the run on {p} failed; it's still going.",
            "I couldn't stop the {p} run; it's still going.",
        ),
        "parado": (
            "Stopped the run on {p}. The work is kept; say resume the run when you want.",
            "The {p} run is stopped and the work is kept; say resume the run to carry on.",
        ),
        "a_parar": ("I asked the run on {p} to stop; it's still closing.", "The {p} run is stopping; it's still closing down."),
        "motivo_alteracoes": "there are uncommitted changes.",
        "motivo_run_ativo": "there is already an active run.",
        "motivo_run_terminado": "the run had already ended.",
        "motivo_decisao_pendente": "a technology decision is missing.",
        "motivo_nao_e_raiz": "the folder is not the Git repository root.",
        "motivo_sem_run": "there is no run to resume.",
        "motivo_erro_forja": "FORJA reported an error; the details are on screen.",
    },
}


def _frase(chave: str, lingua: str, **valores: object) -> str:
    """Uma frase fixa numa das suas formas, nunca a mesma duas vezes seguidas."""
    return VARIANTES.escolher(f"forja_voz.{chave}", _FRASES["en" if lingua == "en" else "pt"][chave]).format(**valores)


# --- Argumentos fixos -------------------------------------------------------


def validar_objetivo(objetivo: str | None) -> str:
    """O objetivo ditado, pronto para ser UM argumento de --goal. Levanta ValueError.

    O parser da FORJA le um valor que comece por "--" como outra opcao
    (por exemplo --allow-dirty): um objetivo assim, com quebras de linha, NUL
    ou grande demais e recusado, nunca alterado.
    """
    bruto = (objetivo or "").strip()
    if not bruto:
        raise ValueError("objetivo_vazio")
    if any(caractere in bruto for caractere in "\r\n\x00") or bruto.startswith("-") or len(bruto) > MAXIMO_DO_OBJETIVO:
        raise ValueError("objetivo_invalido")
    return " ".join(bruto.split())


def argv_da_forja(comando_node: Sequence[str], forja: ConfigForja, *subcomando: str) -> list[str]:
    """node <forja>/bin/forja.mjs <subcomando>, so para os subcomandos permitidos."""
    # O subcomando sao as palavras antes da primeira opcao; o resto sao opcoes
    # e os valores delas (o objetivo pode ter qualquer palavra).
    base: tuple[str, ...] = ()
    for parte in subcomando:
        if parte.startswith("-"):
            break
        base += (parte,)
    if base not in SUBCOMANDOS_PERMITIDOS:
        raise ValueError(f"subcomando da FORJA fora da lista: {' '.join(subcomando)!r}")
    return [*comando_node, str(forja.ponto_de_entrada), *subcomando]


def argv_de_lancar(comando_node: Sequence[str], forja: ConfigForja, objetivo: str) -> list[str]:
    return argv_da_forja(
        comando_node,
        forja,
        "start",
        "--provider",
        forja.provider,
        "--config",
        str(forja.perfil),
        "--goal",
        validar_objetivo(objetivo),
    )


def argv_de_retomar(comando_node: Sequence[str], forja: ConfigForja) -> list[str]:
    return argv_da_forja(comando_node, forja, "core", "resume")


def alteracoes_por_guardar(saida_do_git: str) -> list[str]:
    """As linhas de `git status --porcelain` que contam (a FORJA ignora `.forja/`)."""
    return [linha for linha in saida_do_git.splitlines() if linha.strip() and not linha.rstrip().endswith(".forja/")]


def motivo_do_erro(texto: str) -> str:
    """O motivo, de uma lista fechada, de a FORJA ter recusado ou falhado."""
    minusculas = (texto or "").lower()
    if "uncommitted work" in minusculas:
        return "alteracoes"
    if "run is terminal" in minusculas:
        return "run_terminado"
    if "technology choice is pending" in minusculas:
        return "decisao_pendente"
    if any(
        parte in minusculas
        for parte in ("core run is unfinished", "legacy run is active", "still alive", "lock recovery", "core lock")
    ):
        return "run_ativo"
    if "git project root" in minusculas:
        return "nao_e_raiz"
    if "enoent" in minusculas and "current.json" in minusculas:
        return "sem_run"
    return "erro_forja"


# --- Processos --------------------------------------------------------------


#: O processo intermedio entre o jarvis e o controlador. Um processo arrancado
#: de uma consola que ignora o Ctrl+C herda essa marca, e o controlador nunca
#: receberia a interrupcao: o intermedio volta a aceita-lo antes de arrancar o
#: node (que herda a marca limpa), espera por ele sem se deixar interromper e
#: sai com o mesmo codigo. Corre com `python -I` (sem PYTHONPATH nem site do
#: utilizador); o argv do controlador segue como argumentos, sem shell.
CODIGO_DO_INTERMEDIO = (
    "import subprocess, sys\n"
    "if sys.platform == 'win32':\n"
    "    import ctypes\n"
    "    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)\n"
    "processo = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)\n"
    "while True:\n"
    "    try:\n"
    "        sys.exit(processo.wait())\n"
    "    except KeyboardInterrupt:\n"
    "        pass\n"
)


def argv_com_intermedio(argv: Sequence[str]) -> list[str]:
    """O argv do controlador embrulhado no processo intermedio."""
    return [verificar_executavel_seguro(sys.executable), "-I", "-c", CODIGO_DO_INTERMEDIO, *argv]


def lancar_controlador(argv: list[str], cwd: Path, caminho_do_log: Path) -> Any:
    """Arranca o controlador sem janela, com a saida no log. Sem shell."""
    argv = argv_com_intermedio([verificar_executavel_seguro(argv[0]), *argv[1:]])
    caminho_do_log.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho_do_log, "ab") as log:
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            shell=False,
            env=ambiente_para_filho(),
            creationflags=_CRIAR_SEM_JANELA,
        )


def interromper_controlador(pid: int, *, correr: Callable[..., Any] = subprocess.run) -> bool:
    """Envia Ctrl+C a consola do controlador a partir de um processo auxiliar."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        feito = correr(
            [verificar_executavel_seguro(sys.executable), "-m", "jarvis.forja_voz", "--ctrl-c", str(pid)],
            cwd=str(RAIZ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=LIMITE_DA_INTERRUPCAO_S,
            shell=False,
            creationflags=_CRIAR_SEM_JANELA | _NOVO_GRUPO,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return feito.returncode == 0


def enviar_ctrl_c(pid: int) -> int:
    """Corre no processo auxiliar: liga-se a consola do pid e envia-lhe Ctrl+C.

    O proprio auxiliar ignora o Ctrl+C antes de o gerar, por isso so os
    processos dessa consola (o controlador e o worker dele) o recebem.
    """
    if sys.platform != "win32":
        import os
        import signal

        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            return 1
        return 0
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(ctypes.c_uint32(pid)):
        return 1
    try:
        kernel32.SetConsoleCtrlHandler(None, True)
        return 0 if kernel32.GenerateConsoleCtrlEvent(0, 0) else 1  # 0 = CTRL_C_EVENT
    finally:
        kernel32.FreeConsole()


def _ler_fim(caminho: Path, maximo: int = MAXIMO_DO_LOG) -> str:
    try:
        with open(caminho, "rb") as ficheiro:
            ficheiro.seek(0, 2)
            tamanho = ficheiro.tell()
            ficheiro.seek(max(0, tamanho - maximo))
            return ficheiro.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


@dataclass
class Controlador:
    """Um controlador FORJA lancado (ou retomado) por este jarvis."""

    projeto: str
    operacao: str
    processo: Any
    log: Path
    lancado_em: float

    @property
    def pid(self) -> int | None:
        return getattr(self.processo, "pid", None)

    def vivo(self) -> bool:
        try:
            return self.processo.poll() is None
        except Exception:  # noqa: BLE001 - na duvida, conta como vivo
            return True


# --- O executor por voz -----------------------------------------------------


class ForjaPorVoz:
    """Executa os cinco pedidos de estado e FORJA sobre um projeto do config.

    Os processos, o relogio e a espera entram pelo construtor para os testes
    usarem CLIs falsos. Cada pedido devolve uma `Resposta` (fala e ecra).
    """

    def __init__(
        self,
        config: Config,
        *,
        estado: Estado | None = None,
        comando_node: Sequence[str] | None = None,
        comando_git: Sequence[str] | None = None,
        correr: Callable[..., SaidaDoCli] = correr_cli,
        lancar: Callable[[list[str], Path, Path], Any] = lancar_controlador,
        interromper: Callable[[int], bool] = interromper_controlador,
        pasta_de_logs: Path = PASTA_DE_LOGS,
        limite_s: float = LIMITE_DO_CLI_S,
        espera_inicial_s: float = ESPERA_INICIAL_S,
        espera_da_paragem_s: float = ESPERA_DA_PARAGEM_S,
        dormir: Callable[[float], None] = time.sleep,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.estado = estado or Estado(config, comando_node=comando_node, correr=correr, limite_s=limite_s)
        self._comando_git = list(comando_git) if comando_git else None
        self._correr = correr
        self._lancar = lancar
        self._interromper = interromper
        self.pasta_de_logs = Path(pasta_de_logs)
        self.limite_s = limite_s
        self.espera_inicial_s = espera_inicial_s
        self.espera_da_paragem_s = espera_da_paragem_s
        self._dormir = dormir
        self._relogio = relogio
        self._tranca = threading.Lock()
        self._controladores: dict[str, Controlador] = {}

    # -- entrada

    def executar(self, intencao: str, projeto: str | None, texto: str = "", lingua: str = "pt") -> Resposta:
        """Um pedido ja confirmado (ou so de leitura). Qualquer outra intencao e recusada."""
        if intencao == "estado":
            return self.estado.estado(projeto, lingua)
        if intencao == "ler_relatorio":
            return self.estado.relatorio(projeto, lingua)
        if intencao == "lancar_run":
            return self.lancar(projeto, texto, lingua)
        if intencao == "retomar_run":
            return self.retomar(projeto, lingua)
        if intencao == "parar_run":
            return self.parar(projeto, lingua)
        raise ValueError(f"'{intencao}' nao se faz por voz")

    def controlador(self, projeto: str) -> Controlador | None:
        """O controlador deste jarvis para o projeto, se ainda estiver vivo."""
        with self._tranca:
            atual = self._controladores.get(projeto.casefold())
        return atual if atual is not None and atual.vivo() else None

    # -- verificacoes comuns

    def _preparar(self, nome: str | None, lingua: str) -> tuple[Projeto | None, ConfigForja | None, Resposta | None]:
        projeto = self.config.encontrar_projeto(nome) if nome else None
        if projeto is None:
            return None, None, Resposta(_frase("projeto_desconhecido", lingua), feito=False)
        if self.config.forja is None:
            return projeto, None, Resposta(_frase("sem_forja", lingua), feito=False)
        return projeto, self.config.forja, None

    @staticmethod
    def _recusa(chave: str, lingua: str, *ecra: str, **valores: object) -> Resposta:
        return Resposta(_frase(chave, lingua, **valores), tuple(f"forja | {linha}" for linha in ecra), feito=False)

    def _motivo_falado(self, falha: Falha, lingua: str) -> str:
        return falha.falada(lingua)

    # -- lancar

    def lancar(self, nome: str | None, objetivo: str, lingua: str = "pt") -> Resposta:
        projeto, forja, recusa = self._preparar(nome, lingua)
        if recusa is not None:
            return recusa
        assert projeto is not None and forja is not None
        p = projeto.nome
        try:
            objetivo = validar_objetivo(objetivo)
        except ValueError as erro:
            return self._recusa(str(erro), lingua, f"objetivo recusado ({erro})")
        if self.controlador(p) is not None:
            return self._recusa("run_ativo", lingua, "o controlador lancado pelo jarvis ainda esta vivo", p=p)
        run, falha = self.estado.ler_run(projeto)
        if falha is not None:
            return self._recusa(
                "nao_confirmei", lingua, falha.para_o_ecra(), p=p, motivo=self._motivo_falado(falha, lingua)
            )
        if run is not None and run.estado not in ("done", "failed"):
            chave = "run_ativo" if run.estado == "running" and run.motivo is None else "run_por_acabar"
            return self._recusa(chave, lingua, f"run {run.run}: {run.estado}", p=p)
        try:
            git = [*self._comando_git] if self._comando_git else [localizar_git()]
        except FileNotFoundError as erro:
            return self._recusa("sem_git", lingua, str(erro), p=p)
        saida = self._correr([*git, "status", "--porcelain"], projeto.caminho, self.limite_s)
        if saida.falha or saida.esgotado or saida.codigo != 0:
            return self._recusa("sem_git", lingua, _curto(saida.erro or "git sem resposta"), p=p)
        sujas = alteracoes_por_guardar(saida.saida)
        if sujas:
            return self._recusa("alteracoes", lingua, f"{len(sujas)} ficheiro(s) por guardar", p=p)
        try:
            argv = argv_de_lancar(self.estado.comando_node(), forja, objetivo)
        except FileNotFoundError as erro:
            return self._recusa("falhou_arranque", lingua, str(erro), p=p)
        return self._arrancar(projeto, "lancar_run", argv, "lancado", lingua)

    # -- retomar

    def retomar(self, nome: str | None, lingua: str = "pt") -> Resposta:
        projeto, forja, recusa = self._preparar(nome, lingua)
        if recusa is not None:
            return recusa
        assert projeto is not None and forja is not None
        p = projeto.nome
        if self.controlador(p) is not None:
            return self._recusa("a_correr", lingua, "o controlador lancado pelo jarvis ainda esta vivo", p=p)
        run, falha = self.estado.ler_run(projeto)
        if falha is not None:
            return self._recusa(
                "nao_confirmei_retoma", lingua, falha.para_o_ecra(), p=p, motivo=self._motivo_falado(falha, lingua)
            )
        if run is None:
            return self._recusa("sem_run", lingua, p=p)
        if run.estado in ("done", "failed"):
            return self._recusa("run_terminado", lingua, f"run {run.run}: {run.estado}", p=p)
        if run.decisao_pendente:
            return self._recusa("decisao_pendente", lingua, f"run {run.run}: decisao de tecnologia pendente", p=p)
        if run.estado == "running" and run.motivo is None:
            return self._recusa("a_correr", lingua, f"run {run.run}: running", p=p)
        try:
            argv = argv_de_retomar(self.estado.comando_node(), forja)
        except FileNotFoundError as erro:
            return self._recusa("falhou_arranque", lingua, str(erro), p=p)
        return self._arrancar(projeto, "retomar_run", argv, "retomado", lingua)

    # -- arranque comum de lancar e retomar

    def _caminho_do_log(self, projeto: Projeto, operacao: str) -> Path:
        pasta = re.sub(r"[^\w.-]+", "-", projeto.nome).casefold()
        carimbo = f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        return self.pasta_de_logs / f"{pasta}-{operacao}-{carimbo}.log"

    def _arrancar(self, projeto: Projeto, operacao: str, argv: list[str], chave: str, lingua: str) -> Resposta:
        p = projeto.nome
        caminho = self._caminho_do_log(projeto, operacao)
        ecra = [f"forja | {operacao} no {p} | cwd: {projeto.caminho}", f"forja | log: {caminho}"]
        try:
            processo = self._lancar(argv, projeto.caminho, caminho)
        except (OSError, ValueError) as erro:
            ecra.append(f"forja | nao arrancou: {_curto(str(erro))}")
            return Resposta(_frase("falhou_arranque", lingua, p=p), tuple(ecra), feito=False)
        controlador = Controlador(p, operacao, processo, caminho, self._relogio())
        fim = self._relogio() + self.espera_inicial_s
        while controlador.vivo() and self._relogio() < fim:
            self._dormir(0.1)
        if not controlador.vivo():
            codigo = getattr(processo, "returncode", None)
            texto = _ler_fim(caminho)
            ecra.append(f"forja | o controlador acabou logo (codigo {codigo})")
            ecra += [f"forja | {linha}" for linha in texto.splitlines()[-20:] if linha.strip()]
            if codigo == 0:
                return Resposta(_frase(chave, lingua, p=p), tuple(ecra))
            motivo = _frase(f"motivo_{motivo_do_erro(texto)}", lingua)
            return Resposta(_frase("nao_arrancou", lingua, p=p, motivo=motivo), tuple(ecra), feito=False)
        with self._tranca:
            self._controladores[p.casefold()] = controlador
        ecra.append(f"forja | controlador a correr (pid {controlador.pid})")
        return Resposta(_frase(chave, lingua, p=p), tuple(ecra))

    # -- parar

    def parar(self, nome: str | None, lingua: str = "pt") -> Resposta:
        projeto, forja, recusa = self._preparar(nome, lingua)
        if recusa is not None:
            return recusa
        assert projeto is not None
        p = projeto.nome
        controlador = self.controlador(p)
        if controlador is None or controlador.pid is None:
            return self._recusa("nao_e_meu", lingua, "nenhum controlador lancado pelo jarvis a correr", p=p)
        if not self._interromper(controlador.pid):
            return self._recusa("paragem_falhou", lingua, f"Ctrl+C ao pid {controlador.pid} falhou", p=p)
        fim = self._relogio() + self.espera_da_paragem_s
        while controlador.vivo() and self._relogio() < fim:
            self._dormir(0.1)
        ecra = (f"forja | Ctrl+C enviado ao controlador do {p} (pid {controlador.pid})", f"forja | log: {controlador.log}")
        if controlador.vivo():
            return Resposta(_frase("a_parar", lingua, p=p), ecra)
        with self._tranca:
            # So esquece este controlador; um mais novo do mesmo projeto fica.
            if self._controladores.get(p.casefold()) is controlador:
                del self._controladores[p.casefold()]
        return Resposta(_frase("parado", lingua, p=p), ecra)


def main(argv: list[str] | None = None) -> int:
    argumentos = list(sys.argv[1:] if argv is None else argv)
    if len(argumentos) == 2 and argumentos[0] == "--ctrl-c" and argumentos[1].isdigit():
        return enviar_ctrl_c(int(argumentos[1]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
