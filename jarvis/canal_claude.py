r"""Canal do jarvis para uma sessao do Claude Code: a escada da D40/D8.

Objetivo: entregar uma frase de texto a uma sessao do Claude Code que corre neste
PC e trazer a resposta de volta para o processo do jarvis, sem nada sair da
maquina alem do que o proprio Claude Code ja envia com a subscricao do utilizador.

A escada, por ordem. O primeiro degrau que funcionar e o
escolhido; esta gravado em DEGRAU_ESCOLHIDO:

  1   Remote Control da app  -> VETADO. Passa pelo relay da API da
      Anthropic, por isso nao e tentado nem investigado. Nao existe codigo para
      este degrau de proposito.
  2a  CLI `claude` em stream-json sobre subprocess.Popen (pipes do Windows).
      Sessao viva entre frases, tudo local, zero instalacoes.
  2b  O mesmo transporte sobre pywinpty (ConPTY), para o caso de 2a bloquear
      no buffering dos pipes do Windows (bug #208 do claude-agent-sdk).
  3   pywinauto a escrever na janela do terminal onde ja esta aberta uma sessao
      do Claude Code; o titulo da janela vem da configuracao.
  4   `claude -p --resume <session-id>` por frase: divida assumida (processo
      novo por frase, mais lento), mas nao precisa de instalar nada.

Estado provado neste PC (2026-09-20): o degrau 2a entregou a frase
"responde apenas OK" a uma sessao real e capturou a resposta em ~1,9 s, o
processo ficou vivo e a segunda frase confirmou que a sessao-ponte mantem o
contexto. O degrau 4 tambem passa e fica como plano B (arrancar um processo por
frase e uma divida assumida). A prova fica em `scripts/testar_canal.py`.

REGRA DE SEGURANCA DESTE MODULO (depois de uma revisao de seguranca):
o alvo e sempre o `claude.exe` real, NUNCA o shim `claude.CMD` do npm. Um .CMD
faz o Windows arrancar o cmd.exe, que volta a parsear a linha de comandos: a
lista de argumentos do subprocess deixa de proteger (uma frase com aspas e `&`
executa comandos) e o %VARIAVEL% do ambiente e expandido para dentro do prompt
que sai do PC. Por cima disso: a frase vai por stdin e nunca em argv, o
session-id tem de ser um UUID, e CR/LF em texto externo e recusado. Os testes
negativos vivem em --autoteste e em scripts/testar_canal.py.

Cada degrau e uma funcao isolada que devolve um ResultadoDegrau e nunca levanta
excecao: um degrau que falha escreve o erro completo no resultado. O limite de
tempo por degrau e TIMEOUT_POR_DEGRAU_S (60 s).

Uso na producao (o resto do jarvis so precisa disto):

    from jarvis.canal_claude import abrir_canal

    with abrir_canal() as canal:
        resposta = canal.perguntar("que horas sao?")

Autoteste das partes puras (parsing do stream-json e o limite de tempo), sem
tocar no CLI nem na rede:

    .venv\Scripts\python -m jarvis.canal_claude --autoteste

Diagnostico da escada inteira contra o CLI real: scripts/testar_canal.py.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

RAIZ = Path(__file__).resolve().parent.parent

# --- Contratos fixos --------------------------------------------------------

#: Frase exata com que a escada e provada.
FRASE_DE_TESTE = "responde apenas OK"

#: Limite de tempo por degrau, em segundos.
TIMEOUT_POR_DEGRAU_S = 60.0

#: Degrau em vigor, gravado depois do teste real neste PC.
#: Alterar isto e uma decisao: exige correr scripts/testar_canal.py outra vez.
DEGRAU_ESCOLHIDO = "2a"

#: Titulo (ou parte do titulo) da janela do terminal onde a sessao do Claude
#: Code esta aberta, usado pelo degrau 3. Le-se desta variavel de ambiente,
#: com o default abaixo.
VARIAVEL_TITULO_JANELA = "JARVIS_TITULO_JANELA"
TITULO_JANELA_PADRAO = "claude"

#: Comandos exatos que a D46 autoriza, e so se o degrau anterior falhar.
COMANDO_INSTALAR_PYWINPTY = r".venv\Scripts\python -m pip install pywinpty"
COMANDO_INSTALAR_PYWINAUTO = r".venv\Scripts\python -m pip install pywinauto"


class FimDoStream(Exception):
    """O processo filho fechou o stdout antes de a resposta estar completa."""


class CanalIndisponivel(RuntimeError):
    """O transporte em vigor nao conseguiu abrir."""


@dataclass
class ResultadoDegrau:
    """O que um degrau fez, em forma de dados (e o que vai para a prova)."""

    degrau: str
    nome: str
    comando: str
    estado: str  # "PASS", "FAIL" ou "VETADO"
    frase: str = ""
    resposta: str = ""
    segundos_ate_primeira_resposta: float | None = None
    segundos_total: float | None = None
    erro: str = ""
    notas: list[str] = field(default_factory=list)

    @property
    def passou(self) -> bool:
        return self.estado == "PASS"


# --- Invocacao do CLI ------------------------------------------------------

#: Extensoes que o Windows nao executa diretamente: arranca o cmd.exe (ou o
#: powershell.exe) com a linha de comandos inteira, que volta a ser parseada.
#: Quando isso acontece, a lista de argumentos do subprocess DEIXA DE PROTEGER:
#: uma frase com aspas seguidas de `&` executa comandos arbitrarios, e o
#: cmd.exe expande %VARIAVEL% do ambiente para dentro do prompt que sai do PC.
#: A D48(1) proibe arrancar qualquer um destes alvos, em todos os degraus.
EXTENSOES_QUE_PASSAM_PELO_SHELL = (".cmd", ".bat", ".ps1", ".com", ".vbs", ".js")

#: Onde o executavel real vive dentro de uma instalacao npm do Claude Code
#: (o `claude.CMD` ao lado e so um shim que chama este ficheiro).
SUBCAMINHO_NPM_DO_EXE = ("node_modules", "@anthropic-ai", "claude-code", "bin")


def verificar_executavel_seguro(caminho: str | Path) -> str:
    """Recusa um alvo que o Windows executaria atraves de um shell.

    Devolve o caminho como string se for seguro; levanta ValueError se for um
    .cmd/.bat/.ps1/... Nunca ha fallback: um shim e um erro, nao uma alternativa.
    """
    texto = str(caminho)
    sufixo = Path(texto).suffix.lower()
    if sufixo in EXTENSOES_QUE_PASSAM_PELO_SHELL:
        raise ValueError(
            f"recusado arrancar '{texto}': um {sufixo} faz o Windows arrancar o cmd.exe, "
            "que volta a parsear a linha de comandos (injecao de comandos e expansao de "
            "%VARIAVEL% do ambiente). D48(1): o jarvis so arranca o executavel real."
        )
    return texto


def candidatos_do_cli(nome: str = "claude", ambiente: dict[str, str] | None = None) -> list[Path]:
    """Os sitios onde se procura o executavel real, por ordem de preferencia.

    Nunca inclui um shim: quando o PATH so tem o `claude.CMD` do npm, o que
    entra na lista e o `claude.exe` que esse shim chamaria.
    """
    env = dict(os.environ if ambiente is None else ambiente)
    exe = f"{nome}.exe" if os.name == "nt" else nome
    candidatos: list[Path] = []

    atalho = shutil.which(nome, path=env.get("PATH"))
    if atalho:
        alvo = Path(atalho)
        if alvo.suffix.lower() in EXTENSOES_QUE_PASSAM_PELO_SHELL:
            # Shim do npm: o binario real esta em node_modules, ao lado do shim.
            candidatos.append(alvo.parent.joinpath(*SUBCAMINHO_NPM_DO_EXE, exe))
            candidatos.append(alvo.with_name(exe))
        else:
            candidatos.append(alvo)

    appdata = env.get("APPDATA")
    if appdata:
        candidatos.append(Path(appdata).joinpath("npm", *SUBCAMINHO_NPM_DO_EXE, exe))
    perfil = env.get("USERPROFILE") or env.get("HOME")
    if perfil:
        candidatos.append(Path(perfil).joinpath(".local", "bin", exe))

    unicos: list[Path] = []
    for candidato in candidatos:
        if candidato not in unicos:
            unicos.append(candidato)
    return unicos


def localizar_cli(nome: str = "claude", ambiente: dict[str, str] | None = None) -> str:
    """Caminho do EXECUTAVEL REAL do Claude Code, ou levanta FileNotFoundError.

    Regra permanente do projeto (depois de uma revisao de seguranca):
    o `shutil.which('claude')` devolve em Windows o shim `claude.CMD` do npm, e
    arrancar um .CMD poe o cmd.exe a reparsear a linha de comandos inteira. Por
    isso o shim NUNCA e devolvido, em nenhum degrau da escada: resolve-se o
    `claude.exe` que ele chamaria. Se o .exe nao existir, isto falha com erro
    claro em vez de cair para o shim.
    """
    candidatos = candidatos_do_cli(nome, ambiente)
    for candidato in candidatos:
        if candidato.is_file() and candidato.suffix.lower() not in EXTENSOES_QUE_PASSAM_PELO_SHELL:
            return str(candidato)
    procurados = "\n".join(f"  - {caminho}" for caminho in candidatos) or "  (nenhum)"
    raise FileNotFoundError(
        f"executavel real do CLI '{nome}' nao encontrado. Por D48(1) o jarvis nunca arranca "
        f"o shim .CMD do npm, por isso nao ha alternativa: procurei em\n{procurados}\n"
        "Instalacao do Claude Code: https://docs.claude.com/en/docs/claude-code"
    )


#: Formato estrito de um session-id do Claude Code (UUID v4 canonico). Um
#: session-id e a unica coisa que ainda entra em argv no degrau 4, e vem de
#: fora (da saida do CLI, ou um dia da configuracao): valida-se sempre.
PADRAO_SESSION_ID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def validar_session_id(valor: str) -> str:
    """Devolve o session-id se for um UUID; levanta ValueError se nao for."""
    if not isinstance(valor, str) or not PADRAO_SESSION_ID.match(valor):
        raise ValueError(
            f"session-id recusado: {valor!r} nao e um UUID. So um UUID pode ir para a "
            "linha de comandos do CLI (D48, defesa em profundidade)."
        )
    return valor


def verificar_sem_quebras_de_linha(valor: str, campo: str = "texto") -> str:
    """Recusa CR, LF e NUL em texto externo que va para argv ou stdin."""
    for caractere, nome in (("\r", "CR"), ("\n", "LF"), ("\x00", "NUL")):
        if caractere in valor:
            raise ValueError(
                f"{campo} recusado: contem {nome}. Texto externo com quebras de linha nao "
                "entra num argumento nem num prompt (D48, defesa em profundidade)."
            )
    return valor


def ambiente_para_filho(base: dict[str, str] | None = None) -> dict[str, str]:
    """Ambiente limpo para a sessao-ponte.

    Tira as variaveis com que uma sessao do Claude Code marca os seus processos
    filhos (CLAUDECODE, CLAUDE_CODE_*, CLAUDE_PID, ...). Sem isto, uma sessao
    arrancada de dentro do Claude Code herda o contexto de subagente do pai e
    deixa de ser uma sessao independente. As variaveis ANTHROPIC_* nao se tocam:
    e ali que vive a autenticacao de quem usa chave em vez da subscricao.
    """
    ambiente = dict(os.environ if base is None else base)
    for chave in list(ambiente):
        if chave == "CLAUDECODE" or chave.startswith("CLAUDE_"):
            ambiente.pop(chave, None)
    return ambiente


#: Argumentos do transporte stream-json. A sessao filha nao pode mexer em nada:
#: `--tools ""` tira-lhe todas as ferramentas, `--restricted` tira os modos que
#: correm comandos e ignora os ficheiros de settings, `--permission-prompts
#: none` nega automaticamente tudo o que pediria autorizacao, e a frase de teste
#: nao precisa de ferramenta nenhuma.
ARGS_SESSAO_MINIMA = (
    "--tools",
    "",
    "--restricted",
    "--permission-prompts",
    "none",
    "--strict-mcp-config",
    "--disable-slash-commands",
)

ARGS_STREAM_JSON = (
    "--print",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
)


def comando_stream_json(cli: str | None = None) -> list[str]:
    """Linha de comando completa do transporte dos degraus 2a e 2b.

    Um `cli` dado a mao passa pela mesma verificacao da D48(1): nem por aqui
    entra um .CMD na escada.
    """
    alvo = verificar_executavel_seguro(cli) if cli else localizar_cli()
    return [alvo, *ARGS_STREAM_JSON, *ARGS_SESSAO_MINIMA]


def mensagem_de_utilizador(frase: str) -> str:
    """Uma linha NDJSON com a frase, no formato que o --input-format stream-json le."""
    return json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": frase}]},
        },
        ensure_ascii=False,
    )


def formatar_comando(argumentos: Iterable[str]) -> str:
    """O comando como se escreve na consola (para a prova e para os logs)."""
    partes = []
    for argumento in argumentos:
        if argumento == "" or " " in argumento:
            partes.append(f'"{argumento}"')
        else:
            partes.append(argumento)
    return " ".join(partes)


# --- Parsing do stream-json (puro, testavel sem CLI) -----------------------


def interpretar_mensagem(obj: Any) -> tuple[str, str | None]:
    """Classifica uma mensagem do stream-json.

    Devolve (tipo, texto), em que tipo e um de:
      "init"      - arranque da sessao (texto = session_id)
      "texto"     - pedaco de resposta do assistente (texto = o que ele disse)
      "fim"       - fim do turno (texto = resposta final, se o CLI a deu)
      "erro"      - o CLI reportou erro (texto = descricao)
      "ignorar"   - qualquer outra coisa (rate limits, eventos internos)
    """
    if not isinstance(obj, dict):
        return "ignorar", None
    tipo = obj.get("type")
    if tipo == "system" and obj.get("subtype") == "init":
        return "init", obj.get("session_id")
    if tipo == "assistant":
        mensagem = obj.get("message") or {}
        blocos = mensagem.get("content") or []
        pedacos = [
            bloco.get("text", "")
            for bloco in blocos
            if isinstance(bloco, dict) and bloco.get("type") == "text"
        ]
        texto = "".join(pedacos).strip()
        return ("texto", texto) if texto else ("ignorar", None)
    if tipo == "result":
        if obj.get("is_error"):
            return "erro", str(obj.get("result") or obj.get("subtype") or "erro sem descricao")
        resultado = obj.get("result")
        return "fim", resultado.strip() if isinstance(resultado, str) else None
    return "ignorar", None


@dataclass
class RespostaRecolhida:
    texto: str = ""
    session_id: str | None = None
    segundos_ate_primeira_resposta: float | None = None
    terminou: bool = False
    erro: str = ""
    linhas_lidas: int = 0
    linhas_invalidas: int = 0


def recolher_resposta(
    ler_linha: Callable[[float], str],
    limite_s: float = TIMEOUT_POR_DEGRAU_S,
    relogio: Callable[[], float] = time.monotonic,
) -> RespostaRecolhida:
    """Le linhas NDJSON ate ao fim do turno, com limite de tempo global.

    `ler_linha(restante)` devolve a proxima linha, levanta TimeoutError se nao
    houver nada dentro de `restante` segundos, e FimDoStream se o processo
    fechou a saida. Nao toca em processos: e por aqui que o parsing e o limite
    de tempo se testam sem CLI.
    """
    inicio = relogio()
    recolhida = RespostaRecolhida()
    partes: list[str] = []
    while True:
        restante = limite_s - (relogio() - inicio)
        if restante <= 0:
            recolhida.erro = (
                f"timeout: {limite_s:.0f} s sem o turno fechar "
                f"({recolhida.linhas_lidas} linhas lidas)"
            )
            break
        try:
            linha = ler_linha(restante)
        except TimeoutError:
            fase = "a espera da resposta" if recolhida.session_id else "no arranque"
            recolhida.erro = (
                f"timeout {fase}: {limite_s:.0f} s sem resposta "
                f"({recolhida.linhas_lidas} linhas lidas)"
            )
            break
        except FimDoStream:
            recolhida.erro = (
                "o processo fechou a saida antes de fechar o turno "
                f"({recolhida.linhas_lidas} linhas lidas)"
            )
            break
        linha = linha.strip()
        if not linha:
            continue
        recolhida.linhas_lidas += 1
        try:
            obj = json.loads(linha)
        except json.JSONDecodeError:
            recolhida.linhas_invalidas += 1
            continue
        tipo, texto = interpretar_mensagem(obj)
        if tipo == "init":
            recolhida.session_id = texto
        elif tipo == "texto":
            if recolhida.segundos_ate_primeira_resposta is None:
                recolhida.segundos_ate_primeira_resposta = relogio() - inicio
            partes.append(texto or "")
        elif tipo == "fim":
            if texto:
                partes = [texto]
                if recolhida.segundos_ate_primeira_resposta is None:
                    recolhida.segundos_ate_primeira_resposta = relogio() - inicio
            recolhida.terminou = True
            break
        elif tipo == "erro":
            recolhida.erro = f"o CLI reportou erro: {texto}"
            break
    recolhida.texto = "\n".join(parte for parte in partes if parte).strip()
    return recolhida


# --- Degrau 2a: subprocess.Popen -------------------------------------------


class CanalStreamJson:
    """Sessao-ponte: um processo `claude` vivo, falado em NDJSON pelos pipes.

    Mantem o contexto entre frases porque e sempre o mesmo processo e a mesma
    sessao. A leitura e feita por uma thread para um queue: em Windows nao ha
    select() em pipes, e e por isso que o limite de tempo tem de vir de fora.
    """

    transporte = "2a"

    def __init__(self, cwd: str | Path = RAIZ, cli: str | None = None):
        self.cwd = str(cwd)
        self.cli = cli
        self.argumentos = comando_stream_json(cli)
        self.processo: subprocess.Popen[str] | None = None
        self.session_id: str | None = None
        self._linhas: queue.Queue[str] = queue.Queue()
        self._erros: list[str] = []
        self._fim = threading.Event()

    # -- ciclo de vida

    def abrir(self) -> "CanalStreamJson":
        if self.processo is not None:  # idempotente: with abrir_canal() nao abre dois
            return self
        self.processo = subprocess.Popen(
            self.argumentos,
            cwd=self.cwd,
            env=ambiente_para_filho(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._bombear_saida, daemon=True).start()
        threading.Thread(target=self._bombear_erros, daemon=True).start()
        return self

    def fechar(self) -> None:
        processo = self.processo
        if processo is None:
            return
        try:
            if processo.stdin and not processo.stdin.closed:
                processo.stdin.close()
        except OSError:
            pass
        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            processo.kill()
            try:
                processo.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.processo = None

    def __enter__(self) -> "CanalStreamJson":
        return self.abrir()

    def __exit__(self, *_excecao: object) -> None:
        self.fechar()

    @property
    def vivo(self) -> bool:
        return self.processo is not None and self.processo.poll() is None

    # -- transporte

    def _bombear_saida(self) -> None:
        processo = self.processo
        assert processo is not None and processo.stdout is not None
        try:
            for linha in processo.stdout:
                self._linhas.put(linha)
        finally:
            self._fim.set()

    def _bombear_erros(self) -> None:
        processo = self.processo
        assert processo is not None and processo.stderr is not None
        for linha in processo.stderr:
            if linha.strip():
                self._erros.append(linha.rstrip())

    def _ler_linha(self, restante: float) -> str:
        try:
            return self._linhas.get(timeout=max(restante, 0.0))
        except queue.Empty:
            if self._fim.is_set() and self._linhas.empty():
                raise FimDoStream from None
            raise TimeoutError from None

    def escrever(self, frase: str) -> None:
        processo = self.processo
        if processo is None or processo.stdin is None:
            raise CanalIndisponivel("o canal nao esta aberto")
        processo.stdin.write(mensagem_de_utilizador(frase) + "\n")
        processo.stdin.flush()

    # -- uso

    def perguntar_detalhado(
        self, frase: str, limite_s: float = TIMEOUT_POR_DEGRAU_S
    ) -> RespostaRecolhida:
        self.escrever(frase)
        recolhida = recolher_resposta(self._ler_linha, limite_s)
        if recolhida.session_id:
            self.session_id = recolhida.session_id
        if recolhida.erro and self._erros:
            recolhida.erro += " | stderr: " + " / ".join(self._erros[-5:])
        return recolhida

    def perguntar(self, frase: str, limite_s: float = TIMEOUT_POR_DEGRAU_S) -> str:
        recolhida = self.perguntar_detalhado(frase, limite_s)
        if recolhida.erro:
            raise CanalIndisponivel(recolhida.erro)
        return recolhida.texto

    def stderr_acumulado(self) -> str:
        return "\n".join(self._erros)


def degrau_2a_subprocess_stream_json(
    frase: str = FRASE_DE_TESTE,
    limite_s: float = TIMEOUT_POR_DEGRAU_S,
    cwd: str | Path = RAIZ,
) -> ResultadoDegrau:
    """Degrau 2a: CLI `claude` em stream-json sobre subprocess.Popen."""
    try:
        cli = localizar_cli()
    except FileNotFoundError as erro:
        return ResultadoDegrau(
            degrau="2a",
            nome="CLI claude em stream-json sobre subprocess.Popen",
            comando=formatar_comando(["claude", *ARGS_STREAM_JSON, *ARGS_SESSAO_MINIMA]),
            estado="FAIL",
            frase=frase,
            erro=str(erro),
        )
    canal = CanalStreamJson(cwd=cwd, cli=cli)
    resultado = ResultadoDegrau(
        degrau="2a",
        nome="CLI claude em stream-json sobre subprocess.Popen",
        comando=formatar_comando(canal.argumentos),
        estado="FAIL",
        frase=frase,
    )
    resultado.notas.append(f"cwd = {canal.cwd}")
    inicio = time.monotonic()
    try:
        canal.abrir()
        recolhida = canal.perguntar_detalhado(frase, limite_s)
        resultado.segundos_total = time.monotonic() - inicio
        resultado.segundos_ate_primeira_resposta = recolhida.segundos_ate_primeira_resposta
        resultado.resposta = recolhida.texto
        if recolhida.session_id:
            resultado.notas.append(f"session_id da sessao-ponte = {recolhida.session_id}")
        resultado.notas.append(
            f"linhas NDJSON lidas = {recolhida.linhas_lidas} "
            f"(invalidas: {recolhida.linhas_invalidas})"
        )
        if recolhida.erro:
            resultado.erro = recolhida.erro
        elif not recolhida.texto:
            resultado.erro = "o turno fechou sem texto na resposta"
        else:
            resultado.estado = "PASS"
            resultado.notas.append(
                "processo ainda vivo depois da resposta: "
                + ("sim" if canal.vivo else "nao")
            )
            # Segunda frase: prova que a sessao-ponte mantem o contexto.
            if canal.vivo:
                segunda = canal.perguntar_detalhado(
                    "qual foi a frase exata que eu te disse antes desta?", limite_s
                )
                if segunda.texto and not segunda.erro:
                    resultado.notas.append(
                        "contexto entre frases confirmado; 2a frase respondida em "
                        f"{segunda.segundos_ate_primeira_resposta:.2f} s: "
                        + segunda.texto.replace("\n", " ")[:200]
                    )
                else:
                    resultado.notas.append(
                        "2a frase sem resposta utilizavel: " + (segunda.erro or "sem texto")
                    )
    except Exception:
        resultado.segundos_total = time.monotonic() - inicio
        resultado.erro = traceback.format_exc()
    finally:
        try:
            canal.fechar()
        except Exception:
            resultado.notas.append("aviso: falha ao fechar o processo:\n" + traceback.format_exc())
    stderr = canal.stderr_acumulado()
    if stderr:
        resultado.notas.append("stderr do CLI:\n" + stderr[:2000])
    return resultado


# --- Degrau 2b: o mesmo transporte sobre pywinpty (ConPTY) -----------------


class CanalStreamJsonPty(CanalStreamJson):
    """Igual ao 2a, mas o processo corre dentro de um ConPTY (pywinpty).

    Existe para o caso de os pipes do Windows bloquearem (bug #208 do
    claude-agent-sdk): um PTY faz o CLI ver um terminal real. Reutiliza todo o
    parsing do 2a; so o transporte muda. Em PTY a saida vem em blocos com CRLF
    (e possivelmente sequencias ANSI), por isso ha aqui um partidor de linhas.
    """

    transporte = "2b"

    def __init__(self, cwd: str | Path = RAIZ, cli: str | None = None, colunas: int = 400):
        super().__init__(cwd=cwd, cli=cli)
        self.colunas = colunas
        self.pty: Any = None

    def abrir(self) -> "CanalStreamJsonPty":
        if self.pty is not None:  # idempotente, como no 2a
            return self
        import winpty  # importado aqui: so o degrau 2b depende dele

        self.pty = winpty.PtyProcess.spawn(
            list(self.argumentos),
            cwd=self.cwd,
            env=ambiente_para_filho(),
            dimensions=(50, self.colunas),
        )
        threading.Thread(target=self._bombear_pty, daemon=True).start()
        return self

    def _bombear_pty(self) -> None:
        resto = ""
        try:
            while True:
                pedaco = self.pty.read(4096)
                if not pedaco:
                    break
                resto += pedaco.replace("\r\n", "\n").replace("\r", "\n")
                *linhas, resto = resto.split("\n")
                for linha in linhas:
                    if linha.strip():
                        self._linhas.put(linha)
        except EOFError:
            pass
        except Exception:
            self._erros.append(traceback.format_exc())
        finally:
            if resto.strip():
                self._linhas.put(resto)
            self._fim.set()

    def escrever(self, frase: str) -> None:
        if self.pty is None:
            raise CanalIndisponivel("o canal PTY nao esta aberto")
        self.pty.write(mensagem_de_utilizador(frase) + "\r\n")

    @property
    def vivo(self) -> bool:
        return bool(self.pty is not None and self.pty.isalive())

    def fechar(self) -> None:
        if self.pty is None:
            return
        try:
            self.pty.close(force=True)
        except Exception:
            pass
        self.pty = None


def degrau_2b_pywinpty(
    frase: str = FRASE_DE_TESTE,
    limite_s: float = TIMEOUT_POR_DEGRAU_S,
    cwd: str | Path = RAIZ,
) -> ResultadoDegrau:
    """Degrau 2b: stream-json sobre pywinpty. Nao instala nada por si."""
    try:
        cli = localizar_cli()
    except FileNotFoundError as erro:
        return ResultadoDegrau(
            degrau="2b",
            nome="CLI claude em stream-json sobre pywinpty (ConPTY)",
            comando=formatar_comando(["claude", *ARGS_STREAM_JSON, *ARGS_SESSAO_MINIMA])
            + "  (dentro de um ConPTY)",
            estado="FAIL",
            frase=frase,
            erro=str(erro),
        )
    canal = CanalStreamJsonPty(cwd=cwd, cli=cli)
    resultado = ResultadoDegrau(
        degrau="2b",
        nome="CLI claude em stream-json sobre pywinpty (ConPTY)",
        comando=formatar_comando(canal.argumentos) + "  (dentro de um ConPTY do pywinpty)",
        estado="FAIL",
        frase=frase,
    )
    import importlib.util

    if importlib.util.find_spec("winpty") is None:
        resultado.erro = (
            "pywinpty nao esta instalado. A D46 so autoriza instala-lo se o degrau "
            f"anterior falhar; comando exato: {COMANDO_INSTALAR_PYWINPTY}"
        )
        return resultado
    inicio = time.monotonic()
    try:
        canal.abrir()
        recolhida = canal.perguntar_detalhado(frase, limite_s)
        resultado.segundos_total = time.monotonic() - inicio
        resultado.segundos_ate_primeira_resposta = recolhida.segundos_ate_primeira_resposta
        resultado.resposta = recolhida.texto
        resultado.notas.append(
            f"linhas NDJSON lidas = {recolhida.linhas_lidas} "
            f"(invalidas: {recolhida.linhas_invalidas})"
        )
        if recolhida.erro:
            resultado.erro = recolhida.erro
        elif not recolhida.texto:
            resultado.erro = "o turno fechou sem texto na resposta"
        else:
            resultado.estado = "PASS"
    except Exception:
        resultado.segundos_total = time.monotonic() - inicio
        resultado.erro = traceback.format_exc()
    finally:
        canal.fechar()
    return resultado


# --- Degrau 3: automacao de janela com pywinauto ---------------------------


def titulo_da_janela_configurado(ambiente: dict[str, str] | None = None) -> str:
    """Titulo da janela do terminal com a sessao aberta.

    O valor vem da variavel de ambiente JARVIS_TITULO_JANELA, com TITULO_JANELA_PADRAO como default.
    """
    origem = os.environ if ambiente is None else ambiente
    valor = (origem.get(VARIAVEL_TITULO_JANELA) or "").strip()
    return valor or TITULO_JANELA_PADRAO


#: Caracteres que o type_keys do pywinauto trata como sintaxe e nao como texto.
_TECLAS_ESPECIAIS = "^+%~(){}[]"


def teclas_seguras(frase: str) -> str:
    """Escapa a frase para o type_keys do pywinauto escrever texto literal.

    No type_keys, `^ + % ~ ( ) { } [ ]` sao sintaxe; um literal escreve-se entre
    chaves. Sem isto, uma frase com parentesis ou com um `+` faria a automacao
    premir teclas em vez de escrever o que o utilizador disse.
    """
    saida = []
    for caractere in frase:
        if caractere == "{":
            saida.append("{{}")
        elif caractere == "}":
            saida.append("{}}")
        elif caractere in _TECLAS_ESPECIAIS:
            saida.append("{" + caractere + "}")
        else:
            saida.append(caractere)
    return "".join(saida)


def _texto_da_consola(pid: int, linhas_maximas: int = 60) -> str:
    """Le o ecra da consola de um processo pelo ReadConsoleOutputCharacter.

    Unica forma local de capturar o que a sessao respondeu quando as teclas
    foram enviadas para uma janela: nao ha pipe nenhum para ler. Usa so a API do
    Windows pelo ctypes (nenhuma dependencia nova). Requer que este processo nao
    tenha consola propria ou a largue primeiro, porque um processo so pode estar
    ligado a uma consola de cada vez.
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", ctypes.c_short),
            ("Top", ctypes.c_short),
            ("Right", ctypes.c_short),
            ("Bottom", ctypes.c_short),
        ]

    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    # Tipos explicitos: sem isto o ctypes assume c_int e trunca HANDLEs de 64 bits.
    INVALIDO = wintypes.HANDLE(-1).value
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
    ]
    k32.ReadConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        COORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    k32.FreeConsole()
    if not k32.AttachConsole(ctypes.c_uint(pid)):
        raise OSError(
            f"AttachConsole({pid}) falhou (erro {ctypes.get_last_error()}): "
            "a janela alvo pode nao ser uma consola do Windows"
        )
    saida = None
    try:
        # GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING
        saida = k32.CreateFileW(
            "CONOUT$", 0x40000000 | 0x80000000, 0x1 | 0x2, None, 3, 0, None
        )
        if not saida or saida == INVALIDO:
            raise OSError(
                f"nao foi possivel abrir CONOUT$ da consola alvo (erro {ctypes.get_last_error()})"
            )
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not k32.GetConsoleScreenBufferInfo(saida, ctypes.byref(info)):
            raise OSError("GetConsoleScreenBufferInfo falhou na consola alvo")
        largura = info.dwSize.X
        ultima = info.dwCursorPosition.Y
        primeira = max(0, ultima - linhas_maximas + 1)
        tampao = ctypes.create_unicode_buffer(largura)
        lidos = wintypes.DWORD(0)
        recolhidas: list[str] = []
        for y in range(primeira, ultima + 1):
            if not k32.ReadConsoleOutputCharacterW(
                saida, tampao, wintypes.DWORD(largura), COORD(0, y), ctypes.byref(lidos)
            ):
                break
            recolhidas.append(tampao[: lidos.value].rstrip())
        return "\n".join(recolhidas)
    finally:
        if saida and saida != INVALIDO:
            k32.CloseHandle(saida)
        k32.FreeConsole()


def degrau_3_pywinauto(
    frase: str = FRASE_DE_TESTE,
    limite_s: float = TIMEOUT_POR_DEGRAU_S,
    titulo: str | None = None,
) -> ResultadoDegrau:
    """Degrau 3: escrever na janela do terminal com a sessao aberta.

    Escreve a frase com pywinauto e captura a resposta lendo o ecra da consola
    (nao ha pipe). Codigo escrito por inteiro mas NAO exercitado neste PC: o
    degrau 2a passou e a D46 proibe instalar o pywinauto sem necessidade.
    """
    alvo = titulo or titulo_da_janela_configurado()
    resultado = ResultadoDegrau(
        degrau="3",
        nome="pywinauto a escrever na janela do terminal com a sessao aberta",
        comando=(
            "python -c \"from pywinauto import Desktop; "
            f"j = Desktop(backend='uia').window(title_re='.*{alvo}.*'); "
            "j.set_focus(); j.type_keys('responde apenas OK{ENTER}', with_spaces=True)\""
        ),
        estado="FAIL",
        frase=frase,
    )
    resultado.notas.append(
        f"titulo da janela = '{alvo}' (de {VARIAVEL_TITULO_JANELA}, "
        f"default '{TITULO_JANELA_PADRAO}')"
    )
    import importlib.util

    if importlib.util.find_spec("pywinauto") is None:
        resultado.erro = (
            "pywinauto nao esta instalado. A D46 so autoriza instala-lo se os degraus "
            f"anteriores falharem; comando exato: {COMANDO_INSTALAR_PYWINAUTO}"
        )
        return resultado
    if os.name != "nt":
        resultado.erro = "o degrau 3 so existe em Windows"
        return resultado
    inicio = time.monotonic()
    try:
        from pywinauto import Desktop  # type: ignore[import-not-found]

        janela = Desktop(backend="uia").window(title_re=f".*{alvo}.*")
        janela.wait("exists ready", timeout=min(10.0, limite_s))
        pid = janela.process_id()
        antes = _texto_da_consola(pid)
        janela.set_focus()
        janela.type_keys(teclas_seguras(frase) + "{ENTER}", with_spaces=True)
        limite = inicio + limite_s
        novo = ""
        while time.monotonic() < limite:
            time.sleep(1.0)
            depois = _texto_da_consola(pid)
            if depois != antes and depois.strip():
                novo = depois[len(os.path.commonprefix([antes, depois])):].strip()
                if novo and frase not in novo.splitlines()[-1:]:
                    break
        resultado.segundos_total = time.monotonic() - inicio
        if novo:
            resultado.segundos_ate_primeira_resposta = resultado.segundos_total
            resultado.resposta = novo
            resultado.estado = "PASS"
        else:
            resultado.erro = (
                f"as teclas foram enviadas para a janela '{alvo}' mas o ecra da consola "
                f"nao mudou em {limite_s:.0f} s"
            )
    except Exception:
        resultado.segundos_total = time.monotonic() - inicio
        resultado.erro = traceback.format_exc()
    return resultado


# --- Degrau 4: claude -p --resume por frase (divida assumida) --------------


def _chamar_claude_print(
    argumentos: list[str], limite_s: float, cwd: str | Path, frase: str
) -> tuple[dict[str, Any] | None, str, str, float]:
    """Uma chamada `claude -p ... --output-format json`. Devolve (json, stdout, stderr, s).

    A frase vai SEMPRE por stdin (`--input-format text`, o default, le o prompt
    do stdin quando nao ha prompt em argv). Nenhum texto externo entra na linha
    de comandos: D48(2). Os `argumentos` sao so flags constantes e, quando ha
    `--resume`, um session-id ja validado como UUID.
    """
    inicio = time.monotonic()
    processo = subprocess.run(
        argumentos,
        cwd=str(cwd),
        env=ambiente_para_filho(),
        input=frase,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=limite_s,
    )
    decorrido = time.monotonic() - inicio
    try:
        obj = json.loads(processo.stdout)
    except json.JSONDecodeError:
        obj = None
    return obj, processo.stdout, processo.stderr, decorrido


def degrau_4_claude_p_resume(
    frase: str = FRASE_DE_TESTE,
    limite_s: float = TIMEOUT_POR_DEGRAU_S,
    cwd: str | Path = RAIZ,
    session_id: str | None = None,
) -> ResultadoDegrau:
    """Degrau 4: um processo `claude -p --resume <id>` por frase.

    Divida assumida: mantem contexto porque o --resume recarrega o
    historico do disco, mas paga o arranque de um processo em cada frase. Se nao
    receber um session-id, faz primeiro uma chamada `claude -p` normal e le o id
    real da saida JSON (nunca se inventa um id).

    Seguranca (depois de uma revisao de seguranca): o alvo e o `claude.exe`
    real (nunca o shim .CMD), a frase vai por stdin e nunca em argv, o
    session-id tem de ser um UUID, e uma frase com CR/LF e recusada. Uma frase
    com aspas e `&` chega ao modelo como texto e nao executa nada.
    """
    try:
        cli = localizar_cli()
    except FileNotFoundError as erro:
        return ResultadoDegrau(
            degrau="4",
            nome="claude -p --resume <session-id> por frase",
            comando="claude --print --output-format json --resume <session-id>  (frase por stdin)",
            estado="FAIL",
            frase=frase,
            erro=str(erro),
        )
    resultado = ResultadoDegrau(
        degrau="4",
        nome="claude -p --resume <session-id> por frase (divida assumida)",
        comando="",
        estado="FAIL",
        frase=frase,
    )
    base = [cli, "--print", "--output-format", "json", *ARGS_SESSAO_MINIMA]
    try:
        verificar_sem_quebras_de_linha(frase, "frase do degrau 4")
        if session_id is not None:
            validar_session_id(session_id)
        if session_id is None:
            arranque = list(base)
            obj, stdout, stderr, decorrido = _chamar_claude_print(
                arranque, limite_s, cwd, frase
            )
            resultado.notas.append(
                f"chamada de arranque (para obter um session-id real): "
                f"{formatar_comando(arranque)} -> {decorrido:.2f} s"
            )
            if not obj or not obj.get("session_id"):
                resultado.comando = formatar_comando(arranque)
                resultado.erro = (
                    "a chamada de arranque nao devolveu session_id.\nstdout:\n"
                    f"{stdout[:2000]}\nstderr:\n{stderr[:2000]}"
                )
                return resultado
            session_id = validar_session_id(str(obj["session_id"]))
            resposta_arranque = str(obj.get("result") or "").strip()
            resultado.notas.append(
                f"session_id obtido = {session_id}; resposta dessa chamada: "
                f"{resposta_arranque[:120]!r}"
            )
        argumentos = [*base, "--resume", session_id]
        resultado.comando = (
            formatar_comando(argumentos) + "     # a frase vai por stdin, nao em argv (D48.2)"
        )
        resultado.notas.append(
            "a frase NAO esta na linha de comandos: vai por stdin (D48.2). O alvo e o "
            f"executavel real: {cli}"
        )
        obj, stdout, stderr, decorrido = _chamar_claude_print(argumentos, limite_s, cwd, frase)
        resultado.segundos_total = decorrido
        resultado.segundos_ate_primeira_resposta = decorrido
        if obj is None:
            resultado.erro = (
                f"saida do CLI nao era JSON.\nstdout:\n{stdout[:2000]}\nstderr:\n{stderr[:2000]}"
            )
            return resultado
        if obj.get("is_error"):
            resultado.erro = f"o CLI reportou erro: {obj.get('result')}"
            return resultado
        resultado.resposta = str(obj.get("result") or "").strip()
        resultado.notas.append(f"session_id da resposta = {obj.get('session_id')}")
        if not resultado.resposta:
            resultado.erro = f"resposta vazia.\nstdout:\n{stdout[:2000]}"
            return resultado
        resultado.estado = "PASS"
    except ValueError as erro:  # frase ou session-id recusados pela D48
        resultado.erro = f"recusado antes de arrancar processo nenhum: {erro}"
    except subprocess.TimeoutExpired:
        resultado.segundos_total = limite_s
        resultado.erro = f"timeout: o processo nao respondeu em {limite_s:.0f} s"
    except Exception:
        resultado.erro = traceback.format_exc()
    return resultado


# --- Degrau 1: vetado -----------------------------------------------------


def degrau_1_remote_control() -> ResultadoDegrau:
    """Degrau 1: VETADO. Nao ha tentativa nenhuma, por decisao."""
    return ResultadoDegrau(
        degrau="1",
        nome="Remote Control da app do Claude Code",
        comando="(nenhum: nao foi executado nada)",
        estado="VETADO",
        erro="",
        notas=[
            "Vetado por decisao do projeto: as mensagens "
            "da app passam pela API da Anthropic, que as reencaminha para o processo local "
            "por uma ligacao HTTPS de saida ja aberta. E um relay de terceiros, e a D31 "
            "exige um canal 100% local, sem conta, sem emparelhamento e sem relay.",
            "Por isso nao foi tentado, nem investigado, nem existe codigo para ele neste "
            "modulo.",
        ],
    )


# --- A escada e o transporte em vigor -------------------------------------

#: Ordem da escada. O degrau 1 nao entra: esta vetado.
ORDEM_DA_ESCADA = ("2a", "2b", "3", "4")

DEGRAUS: dict[str, Callable[..., ResultadoDegrau]] = {
    "2a": degrau_2a_subprocess_stream_json,
    "2b": degrau_2b_pywinpty,
    "3": degrau_3_pywinauto,
    "4": degrau_4_claude_p_resume,
}


def transporte_em_vigor() -> str:
    """O degrau escolhido, provado neste PC: '2a'."""
    return DEGRAU_ESCOLHIDO


def abrir_canal(cwd: str | Path = RAIZ) -> CanalStreamJson:
    """Abre o canal do transporte em vigor, pronto a receber frases.

    Devolve um objeto com perguntar(frase) -> str e que funciona como context
    manager. Os degraus 3 e 4 nao sao canais persistentes; se algum deles vier a
    ser o escolhido, esta funcao levanta CanalIndisponivel em vez de finge-lo.
    """
    degrau = transporte_em_vigor()
    if degrau == "2a":
        return CanalStreamJson(cwd=cwd).abrir()
    if degrau == "2b":
        return CanalStreamJsonPty(cwd=cwd).abrir()
    raise CanalIndisponivel(
        f"o degrau em vigor ({degrau}) nao e uma sessao-ponte persistente; "
        f"usa DEGRAUS['{degrau}'] frase a frase"
    )


# --- Autoteste das partes puras (sem CLI, sem rede) -----------------------


def _autoteste() -> int:
    """Verifica o parsing do stream-json, o limite de tempo e a configuracao."""
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    # 1. interpretar_mensagem, uma linha de cada tipo real do CLI.
    verificar(
        "init da sessao",
        interpretar_mensagem({"type": "system", "subtype": "init", "session_id": "abc"}),
        ("init", "abc"),
    )
    verificar(
        "texto do assistente",
        interpretar_mensagem(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": " OK "}]},
            }
        ),
        ("texto", "OK"),
    )
    verificar(
        "bloco sem texto ignora-se",
        interpretar_mensagem(
            {"type": "assistant", "message": {"content": [{"type": "thinking"}]}}
        ),
        ("ignorar", None),
    )
    verificar(
        "fim do turno",
        interpretar_mensagem({"type": "result", "subtype": "success", "result": "OK"}),
        ("fim", "OK"),
    )
    verificar(
        "erro do CLI",
        interpretar_mensagem({"type": "result", "is_error": True, "result": "rebentou"}),
        ("erro", "rebentou"),
    )
    verificar(
        "evento sem interesse",
        interpretar_mensagem({"type": "rate_limit_event"}),
        ("ignorar", None),
    )

    # 2. recolher_resposta com um stream falso: texto, lixo e fim.
    linhas = [
        '{"type":"system","subtype":"init","session_id":"s-1"}',
        "isto nao e json",
        '{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}]}}',
        '{"type":"result","subtype":"success","result":"OK"}',
    ]
    restantes = list(linhas)

    def ler_falso(_restante: float) -> str:
        if not restantes:
            raise FimDoStream
        return restantes.pop(0)

    recolhida = recolher_resposta(ler_falso, limite_s=5.0)
    verificar("stream falso: texto", recolhida.texto, "OK")
    verificar("stream falso: session_id", recolhida.session_id, "s-1")
    verificar("stream falso: turno fechado", recolhida.terminou, True)
    verificar("stream falso: sem erro", recolhida.erro, "")
    verificar("stream falso: linha invalida contada", recolhida.linhas_invalidas, 1)
    verificar(
        "stream falso: tempo medido",
        recolhida.segundos_ate_primeira_resposta is not None,
        True,
    )

    # 3. limite de tempo: relogio falso, nenhuma espera real.
    agora = [0.0]

    def relogio_falso() -> float:
        return agora[0]

    def ler_que_nunca_responde(restante: float) -> str:
        agora[0] += restante
        raise TimeoutError

    parado = recolher_resposta(ler_que_nunca_responde, limite_s=60.0, relogio=relogio_falso)
    verificar("timeout: sem texto", parado.texto, "")
    verificar("timeout: turno nao fechou", parado.terminou, False)
    verificar("timeout: diz que bloqueou no arranque", "no arranque" in parado.erro, True)
    verificar("timeout: diz o limite", "60 s" in parado.erro, True)

    # 3b. bloqueio depois do init distingue-se do bloqueio no arranque.
    agora[0] = 0.0
    passos = ['{"type":"system","subtype":"init","session_id":"s-2"}']

    def ler_que_para_depois_do_init(restante: float) -> str:
        if passos:
            agora[0] += 0.5
            return passos.pop(0)
        agora[0] += restante
        raise TimeoutError

    meio = recolher_resposta(ler_que_para_depois_do_init, limite_s=60.0, relogio=relogio_falso)
    verificar(
        "timeout: distingue espera da resposta", "a espera da resposta" in meio.erro, True
    )

    # 4. stream que morre a meio.
    mortas = ['{"type":"system","subtype":"init","session_id":"s-3"}']

    def ler_que_morre(_restante: float) -> str:
        if mortas:
            return mortas.pop(0)
        raise FimDoStream

    morto = recolher_resposta(ler_que_morre, limite_s=5.0)
    verificar("stream morto: erro escrito", "fechou a saida" in morto.erro, True)

    # 5. mensagem de entrada e comando.
    verificar(
        "mensagem NDJSON de entrada",
        json.loads(mensagem_de_utilizador(FRASE_DE_TESTE)),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "responde apenas OK"}],
            },
        },
    )
    verificar(
        "comando do stream-json inclui os argumentos da D40",
        all(
            argumento in comando_stream_json("claude")
            for argumento in ("--input-format", "stream-json", "--print", "--verbose")
        ),
        True,
    )
    verificar(
        "sessao minima nao leva ferramentas",
        formatar_comando(ARGS_SESSAO_MINIMA).startswith('--tools "" --restricted'),
        True,
    )

    # 6. titulo da janela: configuracao e default.
    verificar(
        "titulo vem da variavel de ambiente",
        titulo_da_janela_configurado({VARIAVEL_TITULO_JANELA: "jarvis - sessao"}),
        "jarvis - sessao",
    )
    verificar(
        "titulo cai no default quando nao esta configurado",
        titulo_da_janela_configurado({}),
        TITULO_JANELA_PADRAO,
    )
    verificar(
        "titulo ignora um valor so com espacos",
        titulo_da_janela_configurado({VARIAVEL_TITULO_JANELA: "   "}),
        TITULO_JANELA_PADRAO,
    )

    # 6b. escape das teclas do degrau 3: uma frase normal passa intacta e a
    # sintaxe do type_keys nunca e interpretada como teclas.
    verificar("teclas: frase de teste intacta", teclas_seguras(FRASE_DE_TESTE), FRASE_DE_TESTE)
    verificar(
        "teclas: sintaxe do pywinauto escapada",
        teclas_seguras("abre (1+2) ^ 50% ~x [a] {b}"),
        "abre {(}1{+}2{)} {^} 50{%} {~}x {[}a{]} {{}b{}}",
    )

    # 6c. D48(1): o shim .CMD nunca e o alvo, em degrau nenhum.
    def apanhar(funcao: Callable[[], object]) -> str:
        try:
            funcao()
        except (ValueError, FileNotFoundError) as erro:
            return str(erro)
        return ""

    verificar(
        "shim .CMD recusado por verificar_executavel_seguro",
        "recusado arrancar" in apanhar(lambda: verificar_executavel_seguro(r"C:\x\claude.CMD")),
        True,
    )
    verificar(
        "shim .bat/.ps1 tambem recusados",
        [
            bool(apanhar(lambda: verificar_executavel_seguro(f"claude{sufixo}")))
            for sufixo in (".bat", ".ps1", ".vbs", ".exe")
        ],
        [True, True, True, False],
    )
    verificar(
        "comando_stream_json recusa um cli .CMD dado a mao",
        "recusado arrancar" in apanhar(lambda: comando_stream_json(r"C:\x\claude.CMD")),
        True,
    )

    with tempfile.TemporaryDirectory() as pasta:
        base = Path(pasta)
        falso_path = base / "npm"
        falso_path.mkdir()
        (falso_path / "claude.CMD").write_text('@ECHO off\n"claude.exe" %*\n', encoding="utf-8")
        ambiente_so_com_shim = {"PATH": str(falso_path)}  # sem APPDATA nem USERPROFILE
        verificar(
            "so com o shim no PATH, localizar_cli falha em vez de o devolver",
            "nunca arranca o shim"
            in apanhar(lambda: localizar_cli("claude", ambiente_so_com_shim)),
            True,
        )
        verificar(
            "os candidatos nunca incluem o shim",
            [
                caminho.suffix.lower()
                for caminho in candidatos_do_cli("claude", ambiente_so_com_shim)
            ],
            [".exe", ".exe"],
        )
        bin_npm = falso_path.joinpath(*SUBCAMINHO_NPM_DO_EXE)
        bin_npm.mkdir(parents=True)
        exe_real = bin_npm / "claude.exe"
        exe_real.write_bytes(b"MZ")
        verificar(
            "com o .exe presente, localizar_cli devolve o executavel real",
            localizar_cli("claude", ambiente_so_com_shim),
            str(exe_real),
        )

    # 6d. D48: session-id e frase que entram em argv/stdin sao validados.
    verificar(
        "session-id UUID aceite",
        validar_session_id("7d94d31a-f197-4c93-9e07-4e14e7c83d9e"),
        "7d94d31a-f197-4c93-9e07-4e14e7c83d9e",
    )
    verificar(
        "session-id com injecao recusado",
        [
            bool(apanhar(lambda v=valor: validar_session_id(v)))
            for valor in (
                'abc" & echo x & "',
                "7d94d31a-f197-4c93-9e07-4e14e7c83d9",
                "%APPDATA%",
                "",
                "../../outro",
            )
        ],
        [True, True, True, True, True],
    )
    verificar(
        "frase com CR/LF/NUL recusada, frase normal passa",
        [
            bool(apanhar(lambda v=valor: verificar_sem_quebras_de_linha(v, "frase")))
            for valor in ("ola\r\nmais", "ola\nmais", "ola\x00", FRASE_DE_TESTE, 'ola" & dir & "')
        ],
        [True, True, True, False, False],
    )

    # 7. degrau 1 nunca e tentado.
    vetado = degrau_1_remote_control()
    verificar("degrau 1 vetado", vetado.estado, "VETADO")
    verificar("degrau 1 sem comando", vetado.comando.startswith("(nenhum"), True)
    verificar("transporte em vigor", transporte_em_vigor(), "2a")

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do canal completo (parsing, timeout, configuracao, veto).")
    return 0


if __name__ == "__main__":
    if "--autoteste" in sys.argv[1:]:
        sys.exit(_autoteste())
    print(__doc__)
    print(f"transporte em vigor = {transporte_em_vigor()}")
    print("Autoteste: python -m jarvis.canal_claude --autoteste")
    print("Escada completa contra o CLI real: python scripts/testar_canal.py")
