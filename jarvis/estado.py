r"""Estado de um projeto por voz: o run FORJA e as sessoes do Claude Code. So leitura.

"Como esta o run do X" junta duas leituras, feitas em paralelo com cwd = a
pasta do projeto no config.toml:

    node <forja>/bin/forja.mjs core status      o run FORJA atual (JSON)
    claude agents --json --cwd <pasta>          as sessoes do Claude Code (JSON)

e responde em no maximo tres frases faladas: o estado do run, a task em que
vai e o bloqueio (sem bloqueio, as sessoes abertas nessa pasta). O detalhe
fica no ecra. O caminho da FORJA vem da tabela [forja] do config.toml.

"Le o relatorio" procura o relatorio mais recente do projeto, entre:

  - o ultimo `docs/forja/REPORT-*.md` (relatorio final de um run);
  - a revisao mais recente do run FORJA atual (`.forja/current.json`);

diz o resumo depois do filtro da resposta falada (nunca codigo, caminhos,
JSON ou tags) e mostra o texto inteiro na consola.

Nada aqui escreve em lado nenhum nem muda o estado de um run: os dois CLIs
correm com argv em lista, sem shell, com prazo, e os erros viram uma frase
curta; a saida em bruto so vai para a consola.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from jarvis.persona import VARIANTES
from jarvis.canal_claude import (
    EXTENSOES_QUE_PASSAM_PELO_SHELL,
    ambiente_para_filho,
    localizar_cli,
    verificar_executavel_seguro,
)
from jarvis.config import Config, Projeto
from jarvis.resposta_falada import cortar_no_limite, texto_falavel, texto_proibido

#: Prazo de cada leitura (node a arrancar e a ler o estado; claude agents).
LIMITE_DO_CLI_S = 20.0

#: Saida maxima lida de um CLI; acima disto a resposta e tratada como invalida.
MAXIMO_DA_SAIDA = 2 * 1024 * 1024

#: O que se le, no maximo, de um relatorio ou do estado do run.
MAXIMO_DO_RELATORIO = 512 * 1024
MAXIMO_DO_ESTADO = 4 * 1024 * 1024

#: Caracteres do resumo de um relatorio dito em voz alta.
CARACTERES_DO_RESUMO = 220
#: Caracteres do titulo de uma task dito em voz alta.
CARACTERES_DO_TITULO = 70

#: Onde um run FORJA deixa o relatorio final, relativo a pasta do projeto.
PADRAO_DO_RELATORIO = ("docs", "forja", "REPORT-*.md")
ESTADO_DO_RUN = (".forja", "current.json")

#: Motivos de bloqueio que a FORJA publica em `recovery.code` (lista fechada).
MOTIVOS_DE_BLOQUEIO = frozenset(
    {
        "context", "rotations", "no_progress_between_rotations", "repeated_context_limit",
        "attempts", "sessions", "cloud_sessions", "timeout", "output", "provider",
        "provider_limit", "check_targets", "operator_stop", "interrupted", "inspect",
    }
)
ESTADOS_DO_RUN = frozenset({"running", "blocked", "done", "failed"})

_PADRAO_ID_DE_TASK = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,15}")
_PADRAO_RUN = re.compile(r"F-\d{1,16}-[0-9a-f]{1,12}")

_CRIAR_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- Frases -----------------------------------------------------------------

_FRASES = {
    "pt": {
        "sem_forja": "A FORJA não está configurada no jarvis.",
        "nao_encontrado": "Não encontrei o {programa} neste computador.",
        "esgotado": "O {programa} não respondeu em {segundos} segundos.",
        "saida_invalida": "O {programa} respondeu algo que não percebi.",
        "falhou": "O {programa} deu um erro; os detalhes estão no ecrã.",
        "projeto_desconhecido": "Não conheço esse projeto.",
        "sem_run": "O {p} não tem nenhum run FORJA.",
        "so_sessoes": "A FORJA não está configurada, por isso só vejo as sessões.",
        "run_running": "O run do {p} está a correr, {feitas} de {total} tasks feitas.",
        "run_interrompido": "O run do {p} foi interrompido, {feitas} de {total} tasks feitas.",
        "run_blocked": "O run do {p} está parado, {feitas} de {total} tasks feitas.",
        "run_done": "O run do {p} terminou, {feitas} de {total} tasks feitas.",
        "run_failed": "O run do {p} acabou sem terminar, {feitas} de {total} tasks feitas.",
        "task_atual": "Vai na {task}{titulo}.",
        "task_parada": "Parou na {task}{titulo}.",
        "bloqueio": "Bloqueio: {motivo}.",
        "decisao": "Está à espera de uma decisão tua sobre tecnologia.",
        "sessoes_nenhuma": "Não há sessões do Claude Code abertas no {p}.",
        "sessoes": "Sessões do Claude Code no {p}: {partes}.",
        "sessoes_falhou": "Não consegui ver as sessões do Claude Code.",
        "a_trabalhar": "{n} a trabalhar",
        "parada": "{n} parada",
        "paradas": "{n} paradas",
        "a_espera": "{n} à espera de ti",
        "outro": "{n} noutro estado",
        "e": " e ",
        "sem_relatorio": "Não encontrei nenhum relatório no {p}.",
        "relatorio": "Resumo do relatório do {p}: {resumo}",
        "relatorio_sem_resumo": "O relatório do {p} não tem um resumo que se possa ler; o texto inteiro está no ecrã.",
    },
    "en": {
        "sem_forja": ("FORJA isn't set up in jarvis.", "FORJA isn't configured here."),
        "nao_encontrado": ("I couldn't find {programa} on this computer.", "{programa} doesn't seem to be installed here."),
        "esgotado": (
            "{programa} didn't answer within {segundos} seconds.",
            "{programa} took longer than {segundos} seconds, so I gave up.",
        ),
        "saida_invalida": ("{programa} gave an answer I couldn't read.", "I couldn't make sense of what {programa} returned."),
        "falhou": ("{programa} hit an error; the details are on screen.", "{programa} reported an error; it's on screen."),
        "projeto_desconhecido": ("I don't know that project.", "That's not a project I know."),
        "sem_run": ("{p} has no FORJA run.", "There's no FORJA run on {p}."),
        "so_sessoes": (
            "FORJA isn't set up, so I can only see the sessions.",
            "Without FORJA I can only see the sessions.",
        ),
        "run_running": (
            "The run on {p} is going, {feitas} of {total} tasks done.",
            "{p}'s run is in progress, {feitas} of {total} tasks done.",
        ),
        "run_interrompido": (
            "The run on {p} was interrupted, {feitas} of {total} tasks done.",
            "{p}'s run got interrupted, {feitas} of {total} tasks done.",
        ),
        "run_blocked": (
            "The run on {p} is stopped, {feitas} of {total} tasks done.",
            "{p}'s run has stopped, {feitas} of {total} tasks done.",
        ),
        "run_done": (
            "The run on {p} finished, {feitas} of {total} tasks done.",
            "{p}'s run is finished, {feitas} of {total} tasks done.",
        ),
        "run_failed": (
            "The run on {p} ended unfinished, {feitas} of {total} tasks done.",
            "{p}'s run ended early, {feitas} of {total} tasks done.",
        ),
        "task_atual": ("It's on {task}{titulo}.", "Now working on {task}{titulo}."),
        "task_parada": ("It stopped on {task}{titulo}.", "It's stuck on {task}{titulo}."),
        "bloqueio": ("Blocker: {motivo}.", "The reason: {motivo}."),
        "decisao": ("It's waiting for your technology decision.", "It needs a technology decision from you."),
        "sessoes_nenhuma": ("No Claude Code sessions are open on {p}.", "There are no Claude Code sessions open on {p}."),
        "sessoes": ("Claude Code sessions on {p}: {partes}.", "On {p}, the Claude Code sessions: {partes}."),
        "sessoes_falhou": ("I couldn't see the Claude Code sessions.", "I couldn't check the Claude Code sessions."),
        "a_trabalhar": "{n} working",
        "parada": "{n} idle",
        "paradas": "{n} idle",
        "a_espera": "{n} waiting for you",
        "outro": "{n} in another state",
        "e": " and ",
        "sem_relatorio": ("I found no report in {p}.", "There's no report in {p}."),
        "relatorio": ("Summary of the {p} report: {resumo}", "Here's the {p} report in short: {resumo}"),
        "relatorio_sem_resumo": (
            "The {p} report has no summary I can read aloud; the full text is on screen.",
            "No summary I can read aloud in the {p} report; it's all on screen.",
        ),
    },
}

_MOTIVOS = {
    "pt": {
        "context": "chegou ao limite de contexto",
        "rotations": "chegou ao limite de continuações",
        "no_progress_between_rotations": "não avançou entre continuações",
        "repeated_context_limit": "chegou ao limite de contexto várias vezes",
        "attempts": "esgotou as tentativas da task",
        "sessions": "esgotou as sessões",
        "cloud_sessions": "esgotou as sessões",
        "timeout": "chegou ao limite de tempo",
        "output": "chegou ao limite de saída",
        "provider": "a chamada ao modelo falhou",
        "provider_limit": "a conta chegou ao limite de uso",
        "check_targets": "um check precisa de ser corrigido",
        "operator_stop": "foi parado a pedido",
        "interrupted": "foi interrompido a meio",
        "inspect": "precisa de ser inspecionado",
    },
    "en": {
        "context": "it hit the context limit",
        "rotations": "it hit the continuation limit",
        "no_progress_between_rotations": "it made no progress between continuations",
        "repeated_context_limit": "it hit the context limit repeatedly",
        "attempts": "the task ran out of attempts",
        "sessions": "it ran out of sessions",
        "cloud_sessions": "it ran out of sessions",
        "timeout": "it hit the time limit",
        "output": "it hit the output limit",
        "provider": "the model call failed",
        "provider_limit": "the account hit its usage limit",
        "check_targets": "a check needs fixing",
        "operator_stop": "it was stopped on request",
        "interrupted": "it was interrupted",
        "inspect": "it needs inspection",
    },
}


def _lingua(lingua: str | None) -> str:
    return "en" if lingua == "en" else "pt"


def frase(chave: str, lingua: str = "pt", **valores: object) -> str:
    """Uma frase fixa, na lingua do jarvis, com os valores preenchidos.

    Com variantes, nunca a mesma forma duas vezes seguidas (`VARIANTES`).
    """
    texto = VARIANTES.escolher(f"estado.{chave}", _FRASES[_lingua(lingua)][chave]).format(**valores)
    return texto[0].upper() + texto[1:] if texto else texto


# --- Resultado --------------------------------------------------------------


@dataclass(frozen=True)
class Resposta:
    """O que o jarvis diz e o que mostra no ecra depois de um pedido."""

    falado: str
    ecra: tuple[str, ...] = ()
    #: False quando o pedido foi recusado ou falhou.
    feito: bool = True


@dataclass(frozen=True)
class Falha:
    """Porque uma leitura falhou: o codigo da frase curta e o detalhe para o ecra."""

    codigo: str
    programa: str = ""
    detalhe: str = ""
    segundos: float = 0.0

    def falada(self, lingua: str) -> str:
        return frase(self.codigo, lingua, programa=self.programa, segundos=f"{self.segundos:.0f}")

    def para_o_ecra(self) -> str:
        detalhe = f": {self.detalhe}" if self.detalhe else ""
        return f"{self.programa or 'jarvis'} | {self.codigo}{detalhe}"


# --- Correr um CLI ----------------------------------------------------------


@dataclass(frozen=True)
class SaidaDoCli:
    codigo: int | None
    saida: str
    erro: str
    esgotado: bool = False
    #: "" quando correu; "nao_encontrado" ou "arranque" quando nem arrancou.
    falha: str = ""


def _texto(dados: bytes | str | None) -> str:
    if dados is None:
        return ""
    if isinstance(dados, str):
        return dados[:MAXIMO_DA_SAIDA]
    return dados[:MAXIMO_DA_SAIDA].decode("utf-8", errors="replace")


def correr_cli(
    argumentos: Sequence[str | Path],
    cwd: Path,
    limite_s: float,
    *,
    correr: Callable[..., Any] = subprocess.run,
) -> SaidaDoCli:
    """Corre um CLI com argv em lista, sem shell, com cwd e prazo. Nunca levanta."""
    try:
        argv = [verificar_executavel_seguro(argumentos[0]), *(str(parte) for parte in argumentos[1:])]
    except ValueError as erro:
        return SaidaDoCli(None, "", str(erro), falha="arranque")
    try:
        feito = correr(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=limite_s,
            shell=False,
            env=ambiente_para_filho(),
            creationflags=_CRIAR_SEM_JANELA,
        )
    except subprocess.TimeoutExpired:
        return SaidaDoCli(None, "", "", esgotado=True)
    except FileNotFoundError as erro:
        return SaidaDoCli(None, "", str(erro), falha="nao_encontrado")
    except OSError as erro:
        return SaidaDoCli(None, "", str(erro), falha="arranque")
    return SaidaDoCli(feito.returncode, _texto(feito.stdout), _texto(feito.stderr))


def localizar_node(ambiente: dict[str, str] | None = None) -> str:
    """O node.exe real (nunca um shim .cmd). Levanta FileNotFoundError."""
    env = dict(os.environ if ambiente is None else ambiente)
    achado = shutil.which("node", path=env.get("PATH"))
    if achado:
        caminho = Path(achado)
        if caminho.suffix.lower() in EXTENSOES_QUE_PASSAM_PELO_SHELL:
            caminho = caminho.with_name("node.exe" if os.name == "nt" else "node")
        if caminho.is_file():
            return verificar_executavel_seguro(caminho)
    raise FileNotFoundError("o executavel do node nao foi encontrado no PATH")


def localizar_git(ambiente: dict[str, str] | None = None) -> str:
    """O git real (nunca um shim .cmd). Levanta FileNotFoundError."""
    env = dict(os.environ if ambiente is None else ambiente)
    achado = shutil.which("git", path=env.get("PATH"))
    if achado and Path(achado).suffix.lower() not in EXTENSOES_QUE_PASSAM_PELO_SHELL:
        return verificar_executavel_seguro(achado)
    raise FileNotFoundError("o executavel do git nao foi encontrado no PATH")


def _curto(texto: str, limite: int = 300) -> str:
    """Uma linha para o ecra: sem quebras e com tamanho limitado."""
    linha = " ".join((texto or "").split())
    return linha if len(linha) <= limite else linha[: limite - 3] + "..."


# --- Ler o estado do run ----------------------------------------------------


@dataclass(frozen=True)
class EstadoDoRun:
    run: str
    estado: str
    motivo: str | None
    task: str | None
    titulo: str | None
    feitas: int
    total: int
    decisao_pendente: bool
    tasks: tuple[tuple[str, str], ...] = ()


def _id_de_task(valor: object) -> str | None:
    if isinstance(valor, str) and _PADRAO_ID_DE_TASK.fullmatch(valor):
        return valor
    return None


def ler_estado_do_run(texto: str) -> EstadoDoRun:
    """O que interessa de `forja core status`. Levanta ValueError se nao for o formato."""
    dados = json.loads(texto)
    if not isinstance(dados, dict):
        raise ValueError("o estado do run nao e um objeto JSON")
    estado = dados.get("status")
    estado = estado if isinstance(estado, str) and estado in ESTADOS_DO_RUN else "desconhecido"
    run = dados.get("run")
    run = run if isinstance(run, str) and _PADRAO_RUN.fullmatch(run) else "?"
    recuperacao = dados.get("recovery")
    motivo = None
    if isinstance(recuperacao, dict):
        codigo = recuperacao.get("code")
        motivo = codigo if isinstance(codigo, str) and codigo in MOTIVOS_DE_BLOQUEIO else "inspect"
    tarefas = dados.get("tasks")
    tarefas = [t for t in tarefas if isinstance(t, dict)] if isinstance(tarefas, list) else []
    resumo = tuple(
        (_id_de_task(t.get("id")) or "?", t.get("status") if isinstance(t.get("status"), str) else "?")
        for t in tarefas
    )
    feitas = sum(1 for _id, estado_da_task in resumo if estado_da_task == "done")
    atual: dict | None = None
    pendente = dados.get("pending")
    if isinstance(pendente, dict):
        alvo = _id_de_task(pendente.get("task"))
        atual = next((t for t in tarefas if t.get("id") == alvo), None) if alvo else None
    if atual is None and estado != "done":
        atual = next((t for t in tarefas if t.get("status") != "done"), None)
    tecnologia = dados.get("technology")
    decisao = isinstance(tecnologia, list) and any(
        isinstance(d, dict) and not d.get("selection") for d in tecnologia
    )
    titulo = atual.get("title") if atual is not None else None
    return EstadoDoRun(
        run=run,
        estado=estado,
        motivo=motivo,
        task=_id_de_task(atual.get("id")) if atual is not None else None,
        titulo=titulo if isinstance(titulo, str) else None,
        feitas=feitas,
        total=len(tarefas),
        decisao_pendente=decisao,
        tasks=resumo,
    )


def sem_run(saida: SaidaDoCli) -> bool:
    """`core status` num projeto que nunca teve um run FORJA."""
    texto = (saida.erro + saida.saida).lower()
    return saida.codigo not in (0, None) and "enoent" in texto and "current.json" in texto


# --- Ler as sessoes do Claude Code ------------------------------------------


@dataclass(frozen=True)
class SessaoClaude:
    pid: int | None
    tipo: str
    estado: str  # "a_trabalhar", "parada", "a_espera" ou "outro"


def _estado_da_sessao(valor: object) -> str:
    texto = valor.lower() if isinstance(valor, str) else ""
    if texto in ("busy", "running", "working"):
        return "a_trabalhar"
    if texto == "idle":
        return "parada"
    if any(parte in texto for parte in ("wait", "input", "permission", "approval", "blocked")):
        return "a_espera"
    return "outro"


def _dentro_de(caminho: str, pasta: Path) -> bool:
    try:
        alvo = os.path.normcase(os.path.abspath(caminho))
    except (TypeError, ValueError):
        return False
    base = os.path.normcase(os.path.abspath(str(pasta)))
    return alvo == base or alvo.startswith(base.rstrip("\\/") + os.sep)


def ler_sessoes(texto: str, pasta: Path) -> tuple[SessaoClaude, ...]:
    """As sessoes de `claude agents --json` que correm dentro da pasta do projeto."""
    dados = json.loads(texto)
    if not isinstance(dados, list):
        raise ValueError("a lista de sessoes nao e uma lista JSON")
    sessoes: list[SessaoClaude] = []
    for item in dados:
        if not isinstance(item, dict) or not isinstance(item.get("cwd"), str):
            continue
        if not _dentro_de(item["cwd"], pasta):
            continue
        pid = item.get("pid")
        tipo = item.get("kind")
        sessoes.append(
            SessaoClaude(
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                tipo=tipo if isinstance(tipo, str) and tipo.isalpha() else "?",
                estado=_estado_da_sessao(item.get("status")),
            )
        )
    return tuple(sessoes)


# --- Compor a resposta falada -----------------------------------------------

_FIM_DE_FRASE = re.compile(r"\s*[.!?…;:]+(?:\s+|$)")


def falavel_curto(texto: str | None, limite: int) -> str | None:
    """Um pedaco de texto externo dito numa so frase, ou None se nao passar o filtro."""
    if not texto:
        return None
    limpo = texto_falavel(texto)
    if not limpo:
        return None
    limpo = _FIM_DE_FRASE.sub(", ", limpo).strip().strip(",").strip()
    palavras: list[str] = []
    for palavra in limpo.split():
        if len(" ".join([*palavras, palavra])) > limite:
            break
        palavras.append(palavra)
    curto = " ".join(palavras).rstrip(",").strip()
    if not curto or texto_proibido(curto):
        return None
    return curto


def _frase_do_run(nome: str, run: EstadoDoRun, lingua: str) -> list[str]:
    valores = {"p": nome, "feitas": run.feitas, "total": run.total}
    if run.estado == "running":
        chave = "run_interrompido" if run.motivo == "interrupted" else "run_running"
    elif run.estado in ("blocked", "done", "failed"):
        chave = f"run_{run.estado}"
    else:
        chave = "run_blocked"
    frases = [frase(chave, lingua, **valores)]
    if run.task and run.estado != "done":
        titulo = falavel_curto(run.titulo, CARACTERES_DO_TITULO)
        parada = run.estado != "running" or run.motivo is not None
        frases.append(
            frase("task_parada" if parada else "task_atual", lingua, task=run.task, titulo=f", {titulo}" if titulo else "")
        )
    return frases


def _frase_das_sessoes(nome: str, sessoes: tuple[SessaoClaude, ...], lingua: str) -> str:
    if not sessoes:
        return frase("sessoes_nenhuma", lingua, p=nome)
    textos = _FRASES[_lingua(lingua)]
    partes: list[str] = []
    for estado in ("a_trabalhar", "a_espera", "parada", "outro"):
        n = sum(1 for sessao in sessoes if sessao.estado == estado)
        if not n:
            continue
        chave = "paradas" if estado == "parada" and n > 1 else estado
        partes.append(textos[chave].format(n=n))
    juntas = ", ".join(partes[:-1]) + textos["e"] + partes[-1] if len(partes) > 1 else partes[0]
    return frase("sessoes", lingua, p=nome, partes=juntas)


def compor_estado(
    nome: str,
    run: EstadoDoRun | None,
    falha_do_run: Falha | None,
    sessoes: tuple[SessaoClaude, ...] | None,
    falha_das_sessoes: Falha | None,
    lingua: str = "pt",
) -> str:
    """No maximo tres frases: o run, a task e o bloqueio (ou as sessoes)."""
    frases: list[str] = []
    if run is not None:
        frases += _frase_do_run(nome, run, lingua)
    elif falha_do_run is not None:
        frases.append(falha_do_run.falada(lingua))
    else:
        frases.append(frase("sem_run", lingua, p=nome))

    terceira: str | None = None
    if run is not None and run.decisao_pendente:
        terceira = frase("decisao", lingua)
    elif run is not None and run.motivo is not None and run.estado in ("blocked", "running"):
        terceira = frase("bloqueio", lingua, motivo=_MOTIVOS[_lingua(lingua)][run.motivo])
    elif sessoes is not None:
        terceira = _frase_das_sessoes(nome, sessoes, lingua)
    elif falha_das_sessoes is not None:
        terceira = frase("sessoes_falhou", lingua)
    if terceira:
        frases.append(terceira)
    return " ".join(frases[:3])


# --- Relatorio --------------------------------------------------------------


@dataclass(frozen=True)
class Relatorio:
    origem: str  # "markdown" ou "forja"
    quando: float
    nome: str
    texto: str
    resumo: str
    cortado: bool = False


def _dentro_do_projeto(caminho: Path, pasta: Path) -> bool:
    try:
        return caminho.resolve().is_relative_to(pasta.resolve())
    except OSError:
        return False


def _ler_limitado(caminho: Path, maximo: int) -> tuple[str, bool]:
    with open(caminho, "rb") as ficheiro:
        dados = ficheiro.read(maximo + 1)
    return dados[:maximo].decode("utf-8", errors="replace"), len(dados) > maximo


_TITULO_MD = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*#*\s*$")
_TITULO_DE_RESUMO = re.compile(
    r"resumo|sum[aá]rio|summary|tl;?dr|conclus|veredito|verdict|outcome|resultado", re.IGNORECASE
)
_LIGACAO_MD = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_ENFASE_MD = re.compile(r"\*\*|__|(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)")
_MARCADOR_DE_LISTA = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _limpar_markdown(linha: str) -> str:
    linha = _LIGACAO_MD.sub(r"\1", linha)
    linha = _ENFASE_MD.sub("", linha)
    return _MARCADOR_DE_LISTA.sub("", linha).strip()


def resumo_do_markdown(texto: str) -> str:
    """A seccao de resumo de um relatorio (ou o primeiro paragrafo), sem marcacao."""
    linhas = texto.splitlines()
    inicio: int | None = None
    for indice, linha in enumerate(linhas):
        titulo = _TITULO_MD.match(linha)
        if titulo and _TITULO_DE_RESUMO.search(titulo.group(1)):
            inicio = indice + 1
            break
    escolhidas: list[str] = []
    if inicio is not None:
        for linha in linhas[inicio:]:
            if _TITULO_MD.match(linha):
                if escolhidas:
                    break
                continue
            if linha.strip():
                escolhidas.append(_limpar_markdown(linha))
            elif escolhidas and len(" ".join(escolhidas)) > CARACTERES_DO_RESUMO:
                break
    if not escolhidas:
        for linha in linhas:
            if _TITULO_MD.match(linha) or linha.strip().startswith(("|", "---", ">")):
                if escolhidas:
                    break
                continue
            if not linha.strip():
                if escolhidas:
                    break
                continue
            escolhidas.append(_limpar_markdown(linha))
    return "\n".join(parte for parte in escolhidas if parte)


def _relatorio_markdown(pasta: Path) -> Relatorio | None:
    candidatos: list[tuple[float, Path]] = []
    base = pasta.joinpath(*PADRAO_DO_RELATORIO[:-1])
    try:
        encontrados = list(base.glob(PADRAO_DO_RELATORIO[-1]))
    except OSError:
        return None
    for caminho in encontrados:
        try:
            if caminho.is_file() and _dentro_do_projeto(caminho, pasta):
                candidatos.append((caminho.stat().st_mtime, caminho))
        except OSError:
            continue
    if not candidatos:
        return None
    quando, caminho = max(candidatos)
    try:
        texto, cortado = _ler_limitado(caminho, MAXIMO_DO_RELATORIO)
    except OSError:
        return None
    return Relatorio("markdown", quando, caminho.name, texto, resumo_do_markdown(texto), cortado)


def _instante(valor: object) -> float | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.datetime.fromisoformat(valor.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _relatorio_da_forja(pasta: Path) -> Relatorio | None:
    caminho = pasta.joinpath(*ESTADO_DO_RUN)
    try:
        if not caminho.is_file() or not _dentro_do_projeto(caminho, pasta):
            return None
        texto, cortado = _ler_limitado(caminho, MAXIMO_DO_ESTADO)
        if cortado:
            return None
        dados = json.loads(texto)
    except (OSError, ValueError):
        return None
    if not isinstance(dados, dict) or not isinstance(dados.get("tasks"), list):
        return None
    melhor: tuple[float, dict, dict] | None = None
    for task in dados["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("review"), dict):
            continue
        revisao = task["review"]
        if not isinstance(revisao.get("summary"), str) or not revisao["summary"].strip():
            continue
        quando = _instante(task.get("completed_at")) or _instante(dados.get("updated_at"))
        if quando is not None and (melhor is None or quando > melhor[0]):
            melhor = (quando, task, revisao)
    if melhor is None:
        return None
    quando, task, revisao = melhor
    run = dados.get("run_id") if isinstance(dados.get("run_id"), str) else "?"
    ident = _id_de_task(task.get("id")) or "?"
    titulo = task.get("title") if isinstance(task.get("title"), str) else ""
    estado = revisao.get("status") if isinstance(revisao.get("status"), str) else "?"
    linhas = [f"Run {run} | task {ident}: {titulo} | revisao: {estado}", "", revisao["summary"].strip()]
    achados = revisao.get("findings")
    if isinstance(achados, list) and achados:
        linhas += ["", "Achados:"] + [f"- {achado}" for achado in achados if isinstance(achado, str)]
    return Relatorio("forja", quando, f"revisao da {ident}", "\n".join(linhas), revisao["summary"].strip())


def encontrar_relatorio(pasta: Path) -> Relatorio | None:
    """O relatorio mais recente do projeto (relatorio final ou revisao do run atual)."""
    candidatos = [r for r in (_relatorio_markdown(pasta), _relatorio_da_forja(pasta)) if r is not None]
    return max(candidatos, key=lambda relatorio: relatorio.quando) if candidatos else None


def resumo_falado_do_relatorio(nome: str, relatorio: Relatorio | None, lingua: str = "pt") -> str:
    """O resumo dito em voz alta, depois do filtro da resposta falada."""
    if relatorio is None:
        return frase("sem_relatorio", lingua, p=nome)
    falavel = texto_falavel(relatorio.resumo)
    cortado = cortar_no_limite(falavel, CARACTERES_DO_RESUMO) if falavel else ""
    if not cortado or texto_proibido(cortado):
        return frase("relatorio_sem_resumo", lingua, p=nome)
    return frase("relatorio", lingua, p=nome, resumo=cortado)


# --- As leituras juntas -----------------------------------------------------


class Estado:
    """Le o estado e o relatorio de um projeto do config. Nunca escreve nada.

    Os executaveis entram pelo construtor nos testes (CLIs falsos); por
    omissao sao o node.exe e o claude.exe reais, nunca um shim .cmd.
    """

    def __init__(
        self,
        config: Config,
        *,
        comando_node: Sequence[str] | None = None,
        comando_claude: Sequence[str] | None = None,
        correr: Callable[..., SaidaDoCli] = correr_cli,
        limite_s: float = LIMITE_DO_CLI_S,
    ) -> None:
        self.config = config
        self._comando_node = list(comando_node) if comando_node else None
        self._comando_claude = list(comando_claude) if comando_claude else None
        self._correr = correr
        self.limite_s = limite_s

    def projeto(self, nome: str | None) -> Projeto | None:
        return self.config.encontrar_projeto(nome) if nome else None

    def comando_node(self) -> list[str]:
        return list(self._comando_node) if self._comando_node else [localizar_node()]

    def comando_claude(self) -> list[str]:
        return list(self._comando_claude) if self._comando_claude else [localizar_cli()]

    def argv_do_estado(self) -> list[str]:
        """node <forja>/bin/forja.mjs core status (so leitura)."""
        forja = self.config.forja
        assert forja is not None
        return [*self.comando_node(), str(forja.ponto_de_entrada), "core", "status"]

    def ler_run(self, projeto: Projeto) -> tuple[EstadoDoRun | None, Falha | None]:
        """(run, None), (None, None) sem run, ou (None, falha)."""
        if self.config.forja is None:
            return None, Falha("sem_forja")
        try:
            argv = self.argv_do_estado()
        except FileNotFoundError as erro:
            return None, Falha("nao_encontrado", "node", str(erro))
        saida = self._correr(argv, projeto.caminho, self.limite_s)
        falha = self._falha(saida, "node")
        if falha is not None:
            if saida.falha == "" and not saida.esgotado and sem_run(saida):
                return None, None
            return None, falha
        try:
            return ler_estado_do_run(saida.saida), None
        except ValueError as erro:
            return None, Falha("saida_invalida", "FORJA", str(erro))

    def ler_sessoes(self, projeto: Projeto) -> tuple[tuple[SessaoClaude, ...] | None, Falha | None]:
        try:
            argv = [*self.comando_claude(), "agents", "--json", "--cwd", str(projeto.caminho)]
        except FileNotFoundError as erro:
            return None, Falha("nao_encontrado", "claude", str(erro))
        saida = self._correr(argv, projeto.caminho, self.limite_s)
        falha = self._falha(saida, "claude")
        if falha is not None:
            return None, falha
        try:
            return ler_sessoes(saida.saida, projeto.caminho), None
        except ValueError as erro:
            return None, Falha("saida_invalida", "Claude Code", str(erro))

    def _falha(self, saida: SaidaDoCli, programa: str) -> Falha | None:
        if saida.falha == "nao_encontrado":
            return Falha("nao_encontrado", programa, _curto(saida.erro))
        if saida.falha:
            return Falha("falhou", programa, _curto(saida.erro))
        if saida.esgotado:
            return Falha("esgotado", programa, segundos=self.limite_s)
        if saida.codigo != 0:
            return Falha("falhou", programa, _curto(saida.erro or saida.saida))
        return None

    def estado(self, nome: str | None, lingua: str = "pt") -> Resposta:
        """"Como esta o run do X": no maximo tres frases e o detalhe no ecra."""
        projeto = self.projeto(nome)
        if projeto is None:
            return Resposta(frase("projeto_desconhecido", lingua), feito=False)
        resultados: dict[str, tuple] = {}

        def ler(chave: str, funcao: Callable[[Projeto], tuple]) -> None:
            try:
                resultados[chave] = funcao(projeto)
            except Exception as erro:  # noqa: BLE001 - uma leitura falhada vira frase
                resultados[chave] = (None, Falha("falhou", chave, _curto(repr(erro))))

        fios = [
            threading.Thread(target=ler, args=("run", self.ler_run), daemon=True),
            threading.Thread(target=ler, args=("sessoes", self.ler_sessoes), daemon=True),
        ]
        for fio in fios:
            fio.start()
        for fio in fios:
            fio.join(self.limite_s + 5.0)
        run, falha_do_run = resultados.get("run", (None, Falha("esgotado", "node", segundos=self.limite_s)))
        sessoes, falha_das_sessoes = resultados.get(
            "sessoes", (None, Falha("esgotado", "claude", segundos=self.limite_s))
        )
        if falha_do_run is not None and falha_do_run.codigo == "sem_forja" and sessoes is not None:
            falado = frase("so_sessoes", lingua) + " " + _frase_das_sessoes(projeto.nome, sessoes, lingua)
        else:
            falado = compor_estado(projeto.nome, run, falha_do_run, sessoes, falha_das_sessoes, lingua)
        return Resposta(falado, self._ecra_do_estado(projeto, run, falha_do_run, sessoes, falha_das_sessoes))

    @staticmethod
    def _ecra_do_estado(projeto, run, falha_do_run, sessoes, falha_das_sessoes) -> tuple[str, ...]:
        linhas = [f"estado do {projeto.nome}:"]
        if run is not None:
            linhas.append(
                f"  run {run.run}: {run.estado}"
                + (f" | bloqueio: {run.motivo}" if run.motivo else "")
                + (" | decisao de tecnologia pendente" if run.decisao_pendente else "")
            )
            linhas.append("  tasks: " + (", ".join(f"{ident}={estado}" for ident, estado in run.tasks) or "nenhuma"))
            if run.task:
                linhas.append(f"  task atual: {run.task} {_curto(run.titulo or '', 120)}")
        elif falha_do_run is not None:
            linhas.append(f"  run: {falha_do_run.para_o_ecra()}")
        else:
            linhas.append("  run: nenhum run FORJA neste projeto")
        if sessoes is not None:
            linhas.append(f"  sessoes do Claude Code: {len(sessoes)}")
            linhas += [f"    pid {s.pid or '?'} | {s.tipo} | {s.estado}" for s in sessoes]
        elif falha_das_sessoes is not None:
            linhas.append(f"  sessoes: {falha_das_sessoes.para_o_ecra()}")
        return tuple(linhas)

    def relatorio(self, nome: str | None, lingua: str = "pt") -> Resposta:
        """"Le o relatorio": o resumo filtrado na voz e o texto inteiro no ecra."""
        projeto = self.projeto(nome)
        if projeto is None:
            return Resposta(frase("projeto_desconhecido", lingua), feito=False)
        relatorio = encontrar_relatorio(projeto.caminho)
        falado = resumo_falado_do_relatorio(projeto.nome, relatorio, lingua)
        if relatorio is None:
            return Resposta(falado, (f"relatorio do {projeto.nome}: nenhum encontrado",), feito=False)
        quando = datetime.datetime.fromtimestamp(relatorio.quando).strftime("%Y-%m-%d %H:%M")
        ecra = [f"relatorio do {projeto.nome}: {relatorio.nome} ({quando})", *relatorio.texto.splitlines()]
        if relatorio.cortado:
            ecra.append(f"(so os primeiros {MAXIMO_DO_RELATORIO // 1024} KB do ficheiro)")
        return Resposta(falado, tuple(ecra))
