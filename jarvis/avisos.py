r"""Avisos por voz: uma sessao do Claude Code acabou ou esta a espera, um run FORJA mudou.

De onde vem cada aviso:

  hooks do Claude Code   A sessao que o jarvis abre (`jarvis.sessoes`) arranca com
                         `--settings <repo>/.jarvis/sessoes/<projeto>/settings.json`,
                         gerado aqui por `config_de_hooks`. Esse ficheiro liga dois
                         hooks: `Stop` (a sessao acabou de responder) e
                         `Notification` com `idle_prompt` ou `permission_prompt` (a
                         sessao esta a espera do utilizador). Nada e escrito na
                         pasta do projeto nem em ~/.claude.
  runs FORJA             `VigiaDosRuns` le `forja core status` de cada projeto com
                         um run (a cada 30 s) e avisa quando o run passa a
                         terminado, falhado ou bloqueado.

O hook e este mesmo ficheiro, corrido pelo Claude Code:

    <python> <repo>/jarvis/avisos.py hook --projeto <nome>

Le o JSON que o Claude Code passa no stdin e tira dele so tres coisas: o nome
do evento, o tipo de notificacao e o id da sessao. A mensagem da notificacao, o
transcript e o resto nunca saem do hook. Manda ao jarvis uma linha `evento`
pelo IPC local autenticado de `jarvis.canal_mcp` (porta e segredo em
`.jarvis/ipc.json`) e sai sempre com 0 e sem nada no stdout: um hook nunca
bloqueia nem muda o que a sessao faz.

Do lado do jarvis, `Avisos` poe cada aviso numa fila e fala-o quando pode:

  - a frase e fixa ("<projeto> acabou.", "<projeto> está à espera de ti."): so
    o nome do projeto, que vem da configuracao, nunca conteudo da sessao;
  - nunca fala por cima do utilizador nem de outra resposta: se o jarvis esta a
    ouvir, a tratar uma frase, a falar, a espera de confirmacao ou numa janela
    de conversa, o aviso espera a vez;
  - "cala-te" deita fora os avisos em fila e, calado, um aviso so vai para o
    ecra; a dormir, fica so no ecra e no log;
  - no maximo um aviso por sessao (ou por run) por minuto.

Uso manual (o Claude Code e quem corre o hook):

    .venv\Scripts\python -m jarvis.avisos hook --projeto <nome> < evento.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

if __package__ in (None, ""):
    # arrancado como script pelo hook: o cwd e a pasta do projeto, nao o jarvis
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.persona import VARIANTES, Variantes  # noqa: E402
from jarvis.canal_mcp import (  # noqa: E402
    FICHEIRO_IPC,
    HOST_IPC,
    TIPO_EVENTO,
    linha_de_mensagem,
    ler_endereco,
    validar_nome_de_projeto,
)

RAIZ = Path(__file__).resolve().parent.parent
#: O proprio ficheiro: e ele que o hook corre (por caminho, nao por modulo).
SCRIPT_DO_HOOK = Path(__file__).resolve()

# --- Eventos -----------------------------------------------------------------

EVENTO_ACABOU = "acabou"
EVENTO_ESPERA = "espera"
#: Os unicos eventos que um hook pode mandar ao jarvis.
EVENTOS_DA_SESSAO = frozenset({EVENTO_ACABOU, EVENTO_ESPERA})

RUN_TERMINADO = "run_terminado"
RUN_FALHOU = "run_falhou"
RUN_BLOQUEADO = "run_bloqueado"
#: Estado do `core status` -> aviso. So estes estados avisam.
AVISO_DO_ESTADO_DO_RUN = {"done": RUN_TERMINADO, "failed": RUN_FALHOU, "blocked": RUN_BLOQUEADO}

HOOK_STOP = "Stop"
HOOK_NOTIFICATION = "Notification"
#: Tipos de notificacao que querem dizer "a sessao esta a espera de ti".
NOTIFICACOES_DE_ESPERA = ("idle_prompt", "permission_prompt")

#: Ids de sessao aceites no IPC (os UUID do Claude Code).
PADRAO_SESSAO = re.compile(r"\A[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")

#: Um aviso por sessao (ou por run) neste intervalo; os outros ficam so no log.
INTERVALO_POR_SESSAO_S = 60.0
#: Um `Stop` logo depois de uma resposta lida pelo canal repetia o que ja se ouviu.
SILENCIO_DEPOIS_DA_RESPOSTA_S = 30.0
#: Um aviso que nao conseguiu vez neste tempo ja nao interessa.
VALIDADE_DO_AVISO_S = 300.0
#: Avisos em fila, no maximo; cheia, o mais antigo sai.
MAXIMO_NA_FILA = 20
#: De quanto em quanto tempo se tenta outra vez quando o jarvis esta ocupado.
PASSO_DA_ESPERA_S = 0.1
#: Sondagem do estado dos runs FORJA.
SONDAGEM_DOS_RUNS_S = 30.0

#: O que o hook le do stdin, no maximo (o JSON do Claude Code e pequeno).
MAXIMO_DA_ENTRADA = 1024 * 1024
#: Prazo da ligacao do hook ao jarvis: a sessao nunca fica a espera do jarvis.
LIMITE_DO_HOOK_S = 1.0
#: Prazo que o Claude Code da ao hook (segundos, no settings.json).
TIMEOUT_DO_HOOK_S = 10

_FRASES = {
    "pt": {
        EVENTO_ACABOU: "{p} acabou.",
        EVENTO_ESPERA: "{p} está à espera de ti.",
        RUN_TERMINADO: "O run do {p} terminou.",
        RUN_FALHOU: "O run do {p} falhou.",
        RUN_BLOQUEADO: "O run do {p} está bloqueado.",
    },
    "en": {
        EVENTO_ACABOU: ("{p} is done.", "{p} has finished.", "{p} is all done."),
        EVENTO_ESPERA: ("{p} is waiting for you.", "{p} needs you.", "{p} is waiting on you."),
        RUN_TERMINADO: ("The {p} run finished.", "The {p} run is done."),
        RUN_FALHOU: ("The {p} run failed.", "Bad news, the {p} run failed."),
        RUN_BLOQUEADO: ("The {p} run is blocked.", "The {p} run is stuck."),
    },
}


def frase_do_aviso(evento: str, projeto: str, lingua: str = "pt", variantes: Variantes | None = None) -> str:
    """A frase fixa de um aviso, numa das suas formas: so o nome do projeto muda."""
    frases = _FRASES["en" if lingua == "en" else "pt"]
    return (variantes or VARIANTES).escolher(f"avisos.{evento}", frases[evento]).format(p=projeto)


# --- Lado do hook (corre dentro do Claude Code) -------------------------------


def evento_do_hook(entrada: Any) -> tuple[str, str] | None:
    """(evento, sessao) do JSON de um hook, ou None se nao for para avisar.

    So le `hook_event_name`, `notification_type` e `session_id`. A sessao
    que nao for um UUID vai vazia.
    """
    if not isinstance(entrada, dict):
        return None
    nome = entrada.get("hook_event_name")
    if nome == HOOK_STOP:
        evento = EVENTO_ACABOU
    elif nome == HOOK_NOTIFICATION and entrada.get("notification_type") in NOTIFICACOES_DE_ESPERA:
        evento = EVENTO_ESPERA
    else:
        return None
    sessao = entrada.get("session_id")
    return evento, sessao if isinstance(sessao, str) and PADRAO_SESSAO.match(sessao) else ""


def enviar_evento(
    projeto: str,
    evento: str,
    sessao: str = "",
    caminho_endereco: Path = FICHEIRO_IPC,
    limite_s: float = LIMITE_DO_HOOK_S,
) -> bool:
    """Manda um evento ao jarvis em execucao. False se nao houver jarvis ou falhar."""
    if evento not in EVENTOS_DA_SESSAO:
        raise ValueError("evento desconhecido")
    validar_nome_de_projeto(projeto)
    if sessao and not PADRAO_SESSAO.match(sessao):
        sessao = ""
    endereco = ler_endereco(caminho_endereco)
    if endereco is None:
        return False
    campos = {"projeto": projeto, "evento": evento}
    if sessao:
        campos["sessao"] = sessao
    linha = linha_de_mensagem(TIPO_EVENTO, endereco.segredo, **campos)
    try:
        with socket.create_connection((HOST_IPC, endereco.porta), timeout=limite_s) as sock:
            sock.sendall(linha)
            sock.shutdown(socket.SHUT_WR)
    except OSError:
        return False
    return True


def correr_hook(projeto: str, entrada: bytes, caminho_endereco: Path = FICHEIRO_IPC) -> int:
    """O hook inteiro: le o evento, avisa o jarvis se houver, devolve sempre 0."""
    try:
        validar_nome_de_projeto(projeto)
        if len(entrada) > MAXIMO_DA_ENTRADA:
            return 0
        lido = evento_do_hook(json.loads(entrada.decode("utf-8")))
        if lido is not None:
            enviar_evento(projeto, lido[0], lido[1], caminho_endereco)
    except Exception:  # noqa: BLE001 - um hook nunca falha a sessao
        pass
    return 0


# --- settings.json da sessao (gerado no repositorio jarvis) -------------------

#: Caracteres que um shell (bash ou cmd) interpretaria dentro de aspas.
_PROIBIDOS_NO_COMANDO = frozenset('"$`%!\r\n\x00')


def _entre_aspas(valor: str) -> str:
    if any(caractere in _PROIBIDOS_NO_COMANDO for caractere in valor):
        raise ValueError("caminho ou nome recusado para o comando do hook: tem aspas, $, `, %, ! ou quebras")
    return f'"{valor}"'


def comando_do_hook(projeto: str, python: str | None = None, ipc: Path | None = None) -> str:
    """A linha de comando do hook, com cada argumento entre aspas.

    O Claude Code corre-a num shell; por isso so entram caminhos e nomes sem
    nada que um shell expanda dentro de aspas, e os caminhos vao com barras
    normais.
    """
    from jarvis.canal_claude import verificar_executavel_seguro

    validar_nome_de_projeto(projeto)
    executavel = verificar_executavel_seguro(python or sys.executable)
    partes = [Path(executavel).as_posix(), SCRIPT_DO_HOOK.as_posix(), "hook", "--projeto", projeto]
    if ipc is not None:
        partes += ["--ipc", Path(ipc).as_posix()]
    return " ".join(_entre_aspas(parte) for parte in partes)


def config_de_hooks(projeto: str, python: str | None = None, ipc: Path | None = None) -> dict[str, Any]:
    """O --settings da sessao: Stop e Notification (so espera) chamam o hook do jarvis."""
    gancho = {"type": "command", "command": comando_do_hook(projeto, python, ipc), "timeout": TIMEOUT_DO_HOOK_S}
    return {
        "hooks": {
            HOOK_STOP: [{"hooks": [dict(gancho)]}],
            HOOK_NOTIFICATION: [{"matcher": "|".join(NOTIFICACOES_DE_ESPERA), "hooks": [dict(gancho)]}],
        }
    }


def escrever_settings(projeto: str, pasta: Path, python: str | None = None, ipc: Path | None = None) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / "settings.json"
    caminho.write_text(json.dumps(config_de_hooks(projeto, python, ipc), indent=2), encoding="utf-8")
    return caminho


# --- Lado do jarvis: a fila de avisos -----------------------------------------

#: O que `entregar(aviso)` devolve.
FALADO = "falado"
OCUPADO = "ocupado"  # fica na fila e tenta-se outra vez
SO_ECRA = "so_ecra"  # calado ou sem voz: o aviso fica so no ecra
DESCARTADO = "descartado"  # a dormir


@dataclass(frozen=True)
class Aviso:
    projeto: str
    evento: str
    texto: str
    #: Chave do limite: (projeto, sessao) ou (projeto, "run:<id>").
    chave: tuple[str, str]
    recebido_em: float


class Avisos:
    """Fila de avisos falados, um de cada vez, quando o jarvis esta livre.

    `entregar(aviso)` e do jarvis: fala o aviso se puder e devolve FALADO,
    OCUPADO, SO_ECRA ou DESCARTADO. `processar()` e o passo que a thread
    corre, exposto para testes deterministas.
    """

    def __init__(
        self,
        entregar: Callable[[Aviso], str],
        *,
        lingua: str = "pt",
        escrever: Callable[[str], object] = print,
        relogio: Callable[[], float] = time.monotonic,
        intervalo_s: float = INTERVALO_POR_SESSAO_S,
        validade_s: float = VALIDADE_DO_AVISO_S,
        silencio_depois_da_resposta_s: float = SILENCIO_DEPOIS_DA_RESPOSTA_S,
        passo_s: float = PASSO_DA_ESPERA_S,
        maximo: int = MAXIMO_NA_FILA,
    ) -> None:
        self._entregar = entregar
        self.lingua = "en" if lingua == "en" else "pt"
        self._escrever = escrever
        self._relogio = relogio
        self.intervalo_s = intervalo_s
        self.validade_s = validade_s
        self.silencio_depois_da_resposta_s = silencio_depois_da_resposta_s
        self.passo_s = passo_s
        self._fila: collections.deque[Aviso] = collections.deque(maxlen=maximo)
        self._ultimo: dict[tuple[str, str], float] = {}
        self._resposta_em: dict[str, float] = {}
        self._condicao = threading.Condition()
        self._parar = threading.Event()
        self._fio: threading.Thread | None = None
        #: (aviso, desfecho) de cada aviso que saiu da fila, pela ordem.
        self.entregues: list[tuple[Aviso, str]] = []

    def _registar(self, texto: str) -> None:
        self._escrever(f"aviso | {texto}")

    # -- entrada

    def receber(self, projeto: str, evento: str, sessao: str = "") -> bool:
        """Um evento de um hook (ja autenticado pelo IPC). True se entrou na fila."""
        if evento not in EVENTOS_DA_SESSAO:
            self._registar("evento desconhecido recusado")
            return False
        agora = self._relogio()
        if evento == EVENTO_ACABOU:
            with self._condicao:
                resposta = self._resposta_em.get(projeto.casefold())
            if resposta is not None and agora - resposta < self.silencio_depois_da_resposta_s:
                self._registar(f"{projeto} acabou logo depois da resposta ja lida: sem aviso")
                return False
        sessao = sessao if PADRAO_SESSAO.match(sessao or "") else ""
        return self._por_na_fila(projeto, evento, (projeto.casefold(), sessao), agora)

    def receber_run(self, projeto: str, run: str, estado: str) -> bool:
        """Um run FORJA que mudou para `estado` (done, failed ou blocked)."""
        evento = AVISO_DO_ESTADO_DO_RUN.get(estado)
        if evento is None:
            return False
        return self._por_na_fila(projeto, evento, (projeto.casefold(), f"run:{run}"), self._relogio())

    def houve_resposta(self, projeto: str) -> None:
        """Uma resposta do projeto acabou de chegar pelo canal (ja foi dita)."""
        with self._condicao:
            self._resposta_em[projeto.casefold()] = self._relogio()

    def _por_na_fila(self, projeto: str, evento: str, chave: tuple[str, str], agora: float) -> bool:
        texto = frase_do_aviso(evento, projeto, self.lingua)
        with self._condicao:
            ultimo = self._ultimo.get(chave)
            if ultimo is not None and agora - ultimo < self.intervalo_s:
                limitado = True
            else:
                limitado = False
                self._ultimo[chave] = agora
                self._fila.append(Aviso(projeto, evento, texto, chave, agora))
                self._condicao.notify_all()
        if limitado:
            self._registar(f"limite de 1 por minuto: '{texto}' so no log")
            return False
        self._registar(f"recebido: {texto}")
        return True

    def descartar(self, motivo: str) -> int:
        """Esvazia a fila (cala-te, dormir). Devolve quantos sairam."""
        with self._condicao:
            fora = len(self._fila)
            self._fila.clear()
        if fora:
            self._registar(f"{fora} aviso(s) em fila deitados fora ({motivo})")
        return fora

    @property
    def em_fila(self) -> int:
        with self._condicao:
            return len(self._fila)

    # -- saida

    def processar(self) -> str | None:
        """Tenta o aviso mais antigo. Devolve o desfecho, ou None sem nada na fila."""
        with self._condicao:
            if not self._fila:
                return None
            aviso = self._fila[0]
        if self._relogio() - aviso.recebido_em > self.validade_s:
            desfecho = "expirado"
        else:
            try:
                desfecho = self._entregar(aviso)
            except Exception as erro:  # noqa: BLE001 - um aviso falhado nunca para a fila
                self._registar(f"falhou ao dizer '{aviso.texto}': {erro!r}")
                desfecho = SO_ECRA
        if desfecho == OCUPADO:
            return OCUPADO
        with self._condicao:
            if self._fila and self._fila[0] is aviso:
                self._fila.popleft()
        self.entregues.append((aviso, desfecho))
        if desfecho == SO_ECRA:
            self._registar(f"so no ecra (calado): {aviso.texto}")
        elif desfecho == DESCARTADO:
            self._registar(f"so no log (a dormir): {aviso.texto}")
        elif desfecho == "expirado":
            self._registar(f"sem vez em {self.validade_s:.0f} s, fica so no log: {aviso.texto}")
        return desfecho

    # -- thread

    def iniciar(self) -> "Avisos":
        if self._fio is None:
            self._parar.clear()
            self._fio = threading.Thread(target=self._ciclo, name="jarvis-avisos", daemon=True)
            self._fio.start()
        return self

    def parar(self) -> None:
        self._parar.set()
        with self._condicao:
            self._condicao.notify_all()
        if self._fio is not None:
            self._fio.join(timeout=5.0)
            self._fio = None

    def _ciclo(self) -> None:
        while not self._parar.is_set():
            with self._condicao:
                self._condicao.wait_for(lambda: bool(self._fila) or self._parar.is_set())
            if self._parar.is_set():
                return
            if self.processar() == OCUPADO:
                self._parar.wait(self.passo_s)


# --- Vigia dos runs FORJA -----------------------------------------------------


def tem_run_forja(caminho: Path) -> bool:
    """O projeto tem (ou teve) um run FORJA: so esses se sondam."""
    return (Path(caminho) / ".forja" / "current.json").is_file()


class VigiaDosRuns:
    """Sonda `core status` dos projetos com run e avisa quando um run acaba ou bloqueia.

    `ler_run(projeto)` e o `Estado.ler_run` de `jarvis.estado` (so leitura):
    devolve (EstadoDoRun | None, Falha | None). A primeira leitura de cada
    projeto so serve de referencia; avisa-se quando o par (run, estado) muda
    para done, failed ou blocked.
    """

    def __init__(
        self,
        projetos: Iterable[Any],
        ler_run: Callable[[Any], tuple[Any, Any]],
        ao_mudar: Callable[[str, str, str], object],
        *,
        intervalo_s: float = SONDAGEM_DOS_RUNS_S,
        escrever: Callable[[str], object] = print,
        tem_run: Callable[[Path], bool] = tem_run_forja,
    ) -> None:
        self.projetos = tuple(projetos)
        self._ler_run = ler_run
        self._ao_mudar = ao_mudar
        self.intervalo_s = intervalo_s
        self._escrever = escrever
        self._tem_run = tem_run
        self._vistos: dict[str, tuple[str, str]] = {}
        self._parar = threading.Event()
        self._fio: threading.Thread | None = None

    def sondar(self) -> list[tuple[str, str, str]]:
        """Uma volta por todos os projetos. Devolve os (projeto, run, estado) avisados."""
        avisados: list[tuple[str, str, str]] = []
        for projeto in self.projetos:
            if self._parar.is_set():
                break
            try:
                if not self._tem_run(projeto.caminho):
                    continue
                run, falha = self._ler_run(projeto)
            except Exception as erro:  # noqa: BLE001 - uma leitura falhada nunca para a vigia
                self._escrever(f"aviso | estado do run do {projeto.nome} por ler: {type(erro).__name__}")
                continue
            if run is None or falha is not None:
                continue  # sem run ou leitura falhada: fica a referencia anterior
            atual = (str(run.run), str(run.estado))
            anterior = self._vistos.get(projeto.nome)
            self._vistos[projeto.nome] = atual
            if anterior is not None and anterior != atual and atual[1] in AVISO_DO_ESTADO_DO_RUN:
                avisados.append((projeto.nome, atual[0], atual[1]))
                self._ao_mudar(projeto.nome, atual[0], atual[1])
        return avisados

    def iniciar(self) -> "VigiaDosRuns":
        if self._fio is None:
            self._parar.clear()
            self._fio = threading.Thread(target=self._ciclo, name="jarvis-vigia-runs", daemon=True)
            self._fio.start()
        return self

    def parar(self) -> None:
        self._parar.set()
        if self._fio is not None:
            self._fio.join(timeout=5.0)
            self._fio = None

    def _ciclo(self) -> None:
        while not self._parar.is_set():
            self.sondar()
            self._parar.wait(self.intervalo_s)


# --- Linha de comandos --------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="python -m jarvis.avisos")
    comandos = analisador.add_subparsers(dest="comando", required=True)
    hook = comandos.add_parser("hook", help="corrido pelo Claude Code nos hooks Stop e Notification")
    hook.add_argument("--projeto", required=True)
    hook.add_argument("--ipc", type=Path, default=FICHEIRO_IPC, help=argparse.SUPPRESS)
    try:
        argumentos = analisador.parse_args(argv)
    except SystemExit:
        return 0  # argumentos maus nunca falham a sessao
    try:
        entrada = sys.stdin.buffer.read(MAXIMO_DA_ENTRADA + 1)
    except (OSError, ValueError, AttributeError):
        return 0
    return correr_hook(argumentos.projeto, entrada, argumentos.ipc)


if __name__ == "__main__":
    sys.exit(main())
