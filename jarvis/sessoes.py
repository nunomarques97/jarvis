r"""Sessoes do Claude Code por projeto: abrir com o canal do jarvis e entregar prompts.

Caminho principal (channel MCP, research preview do Claude Code):

    python -m jarvis.sessoes abrir <projeto>

abre uma sessao INTERATIVA do Claude Code numa janela nova, com cwd = pasta do
projeto no config.toml, e com o canal do jarvis ligado:

    claude --mcp-config <repo>/.jarvis/sessoes/<projeto>/mcp.json
           --dangerously-load-development-channels server:jarvis
           --settings <repo>/.jarvis/sessoes/<projeto>/settings.json
           --debug-file <repo>/.jarvis/sessoes/<projeto>/debug-<hora>.log

O settings.json so liga os hooks de aviso (`jarvis.avisos`): quando a sessao
acaba ou fica a espera do utilizador, o hook avisa o jarvis pelo mesmo IPC.

Tudo o que o jarvis gera fica no repositorio jarvis, numa pasta ignorada pelo
Git (`.jarvis/`): nada e escrito na pasta do projeto nem em ~/.claude. O
Claude Code mostra um aviso no arranque que o utilizador confirma na janela
("I am using this for local development"). O servidor do canal e
`jarvis.canal_mcp`; os prompts confirmados chegam-lhe pelo IPC local
autenticado de `CentralDoCanal`.

Como se sabe se o canal registou: o Claude Code escreve no --debug-file
`MCP server "jarvis": Channel notifications registered` ou `... skipped:
<motivo>` (preview desligado, allowlist, politica). O ficheiro de debug tem o
inicio dos prompts entregues; fica na mesma pasta ignorada e so os tres mais
recentes de cada projeto sao mantidos.

Recurso: se o canal nao registar (ou a janela fechar antes), os prompts vao
para uma sessao headless stream-json na pasta do projeto
(`jarvis.canal_claude.comando_sessao_do_projeto`), que o utilizador retoma com
`claude --resume <id>` nessa pasta. O caminho usado fica no log
(`logs/sessoes-<data>.log`) e e dito ao utilizador.

Prova real com o CLI numa pasta de teste criada em tmp/ (nunca num projeto do
utilizador):

    .venv\Scripts\python -m jarvis.sessoes prova            # canal, janela nova
    .venv\Scripts\python -m jarvis.sessoes prova --headless # so o recurso
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from jarvis import avisos, canal_mcp
from jarvis.canal_claude import (
    CanalIndisponivel,
    CanalStreamJson,
    ambiente_para_filho,
    comando_sessao_do_projeto,
    localizar_cli,
    validar_session_id,
    verificar_executavel_seguro,
)
from jarvis.canal_mcp import (
    HOST_IPC,
    MAXIMO_CARACTERES_DE_TEXTO,
    TIPO_EVENTO,
    TIPO_OLA,
    TIPO_PROMPT,
    TIPO_RESPOSTA,
    EnderecoIpc,
    LeitorDeLinhas,
    LinhaGrandeDemais,
    MensagemRecusada,
    apagar_endereco_se_for,
    escrever_endereco,
    gerar_segredo,
    linha_de_mensagem,
    novo_pedido,
    validar_mensagem,
    validar_nome_de_projeto,
)
from jarvis.config import ConfigError, Projeto, carregar_config
from jarvis.resposta_falada import resumo_falado

RAIZ = Path(__file__).resolve().parent.parent

PASTA_DE_SESSOES = canal_mcp.PASTA_DE_ESTADO / "sessoes"
PASTA_DE_LOGS = RAIZ / "logs"
#: Pasta das provas reais (ignorada pelo Git, como toda a tmp/).
PASTA_DE_PROVAS = RAIZ / "tmp" / "prova-canal"

CAMINHO_CANAL = "canal"
CAMINHO_HEADLESS = "headless"

#: Quanto tempo se espera pelo registo do canal: inclui o utilizador confirmar
#: o aviso de arranque na janela nova.
ESPERA_DO_REGISTO_S = 180.0
#: Depois de registar, quanto tempo o canal tem para se ligar ao jarvis.
ESPERA_DA_LIGACAO_S = 15.0
ESPERA_DA_RESPOSTA_S = 300.0
#: Quantos ficheiros de debug se mantem por projeto.
DEBUG_A_MANTER = 3
#: Respostas guardadas por projeto a espera de quem as leia.
MAXIMO_RESPOSTAS_EM_ESPERA = 50

_LINHA_REGISTADO = f'MCP server "{canal_mcp.NOME_DO_SERVIDOR}": Channel notifications registered'
_PREFIXO_RECUSADO = f'MCP server "{canal_mcp.NOME_DO_SERVIDOR}": Channel notifications skipped: '


# --- Log --------------------------------------------------------------------


def agora_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def registar(texto: str, pasta: Path = PASTA_DE_LOGS) -> None:
    """Uma linha com hora na consola e em `logs/sessoes-<data>.log` (ignorado pelo Git)."""
    linha = f"{agora_iso()} {texto}"
    print(linha, flush=True)
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        with (pasta / f"sessoes-{datetime.date.today():%Y-%m-%d}.log").open(
            "a", encoding="utf-8"
        ) as ficheiro:
            ficheiro.write(linha + "\n")
    except OSError:
        pass


# --- Ficheiros gerados no repositorio jarvis --------------------------------


def nome_da_pasta(nome: str) -> str:
    """Nome de pasta seguro para um projeto (o nome ja validado, sem espacos)."""
    validar_nome_de_projeto(nome)
    return re.sub(r"[^\w.-]+", "-", nome).casefold()


def pasta_da_sessao(nome: str, base: Path = PASTA_DE_SESSOES) -> Path:
    return Path(base) / nome_da_pasta(nome)


def config_mcp(nome: str, python: str | None = None, ipc: Path | None = None) -> dict[str, Any]:
    """O --mcp-config do canal: arranca `jarvis.canal_mcp` com o Python deste venv."""
    validar_nome_de_projeto(nome)
    executavel = verificar_executavel_seguro(python or sys.executable)
    argumentos = ["-m", "jarvis.canal_mcp", "--projeto", nome]
    if ipc is not None:
        argumentos += ["--ipc", str(ipc)]
    return {
        "mcpServers": {
            canal_mcp.NOME_DO_SERVIDOR: {
                "type": "stdio",
                "command": executavel,
                "args": argumentos,
                "env": {"PYTHONPATH": str(RAIZ)},
            }
        }
    }


def escrever_config_mcp(
    nome: str, pasta: Path, python: str | None = None, ipc: Path | None = None
) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / "mcp.json"
    caminho.write_text(json.dumps(config_mcp(nome, python, ipc), indent=2), encoding="utf-8")
    return caminho


def comando_interativo(
    cli: str, caminho_mcp: Path, caminho_debug: Path, caminho_settings: Path | None = None
) -> list[str]:
    """Linha de comando da sessao interativa com o canal (e os hooks de aviso) do jarvis."""
    argumentos = [
        verificar_executavel_seguro(cli),
        "--mcp-config",
        str(caminho_mcp),
        "--dangerously-load-development-channels",
        f"server:{canal_mcp.NOME_DO_SERVIDOR}",
    ]
    if caminho_settings is not None:
        argumentos += ["--settings", str(caminho_settings)]
    return argumentos + ["--debug-file", str(caminho_debug)]


def _limpar_debug_antigos(pasta: Path, manter: int = DEBUG_A_MANTER) -> None:
    ficheiros = sorted(pasta.glob("debug-*.log"), key=lambda caminho: caminho.name)
    for antigo in ficheiros[: max(0, len(ficheiros) - manter)]:
        try:
            antigo.unlink()
        except OSError:
            pass  # ainda aberto por uma sessao viva


# --- Registo do canal -------------------------------------------------------


@dataclass(frozen=True)
class Veredito:
    estado: str  # "registado", "recusado", "terminou" ou "sem-sinal"
    motivo: str = ""

    @property
    def registado(self) -> bool:
        return self.estado == "registado"


def procurar_registo(texto: str) -> Veredito | None:
    """O veredito do Claude Code sobre o canal do jarvis, se ja estiver no texto."""
    for linha in texto.splitlines():
        if _LINHA_REGISTADO in linha:
            return Veredito("registado")
        indice = linha.find(_PREFIXO_RECUSADO)
        if indice >= 0:
            return Veredito("recusado", linha[indice + len(_PREFIXO_RECUSADO) :].strip()[:200])
    return None


def esperar_registo(
    caminho_debug: Path,
    processo: Any,
    limite_s: float = ESPERA_DO_REGISTO_S,
    dormir: Callable[[float], None] = time.sleep,
    relogio: Callable[[], float] = time.monotonic,
) -> Veredito:
    """Le o ficheiro de debug ate aparecer o veredito, a janela fechar ou o tempo acabar."""
    inicio = relogio()
    posicao = 0
    resto = ""
    while True:
        try:
            with open(caminho_debug, "r", encoding="utf-8", errors="replace") as ficheiro:
                ficheiro.seek(posicao)
                novo = ficheiro.read()
                posicao = ficheiro.tell()
        except OSError:
            novo = ""
        if novo:
            texto = resto + novo
            # a ultima linha pode estar a meio: fica para a proxima leitura
            corte = texto.rfind("\n") + 1
            completas, resto = texto[:corte], texto[corte:]
            veredito = procurar_registo(completas)
            if veredito is not None:
                return veredito
        if processo is not None and processo.poll() is not None:
            return Veredito("terminou", "a janela do Claude Code fechou antes de o canal registar")
        if relogio() - inicio >= limite_s:
            return Veredito("sem-sinal", f"o canal nao registou em {limite_s:.0f} s")
        dormir(0.25)


# --- Abrir a sessao interativa ---------------------------------------------


def lancar_consola(argumentos: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    """Arranca o CLI numa janela de consola nova (sem shell, argv em lista)."""
    bandeiras = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(argumentos, cwd=str(cwd), env=env, creationflags=bandeiras)


@dataclass
class SessaoAberta:
    projeto: Projeto
    caminho: str
    motivo: str
    comando: list[str]
    config_mcp: Path
    debug: Path
    processo: Any = None
    aberta_em: str = ""

    @property
    def pid(self) -> int | None:
        return getattr(self.processo, "pid", None)

    def frase_para_o_utilizador(self) -> str:
        if self.caminho == CAMINHO_CANAL:
            return (
                f"Sessão do {self.projeto.nome} aberta com o canal do jarvis. "
                "Os pedidos que confirmares entram nessa janela."
            )
        return (
            f"O canal não registou no {self.projeto.nome}. Os pedidos confirmados vão para "
            "uma sessão em segundo plano na pasta do projeto, que retomas com claude "
            "resume; o comando exato fica no ecrã."
        )


def abrir_sessao(
    projeto: Projeto,
    *,
    cli: str | None = None,
    python: str | None = None,
    ipc: Path | None = None,
    base: Path = PASTA_DE_SESSOES,
    lancar: Callable[[list[str], Path, dict[str, str]], Any] = lancar_consola,
    espera_s: float = ESPERA_DO_REGISTO_S,
    log: Callable[[str], None] = registar,
    dormir: Callable[[float], None] = time.sleep,
    relogio: Callable[[], float] = time.monotonic,
) -> SessaoAberta:
    """Abre a sessao interativa do projeto com o canal e diz que caminho vai ser usado.

    Nunca escreve na pasta do projeto: ela so e o cwd do Claude Code. Os ficheiros
    gerados (mcp.json, debug) ficam em `base`, dentro do repositorio jarvis.
    """
    validar_nome_de_projeto(projeto.nome)
    pasta_projeto = Path(projeto.caminho).resolve()
    if not pasta_projeto.is_dir():
        raise ConfigError(f"a pasta do projeto '{projeto.nome}' nao existe ou nao e uma pasta")
    pasta = pasta_da_sessao(projeto.nome, base)
    caminho_mcp = escrever_config_mcp(projeto.nome, pasta, python, ipc)
    caminho_settings = avisos.escrever_settings(projeto.nome, pasta, python, ipc)
    _limpar_debug_antigos(pasta, DEBUG_A_MANTER - 1)
    # a hora ordena os ficheiros; o sufixo aleatorio evita repetir o nome no mesmo tique do relogio
    carimbo = f"{datetime.datetime.now():%Y%m%d-%H%M%S-%f}-{secrets.token_hex(4)}"
    caminho_debug = pasta / f"debug-{carimbo}.log"
    argumentos = comando_interativo(cli or localizar_cli(), caminho_mcp, caminho_debug, caminho_settings)

    aberta_em = agora_iso()
    log(f"sessao {projeto.nome}: a abrir a janela do Claude Code com o canal do jarvis")
    processo = lancar(argumentos, pasta_projeto, ambiente_para_filho())
    veredito = esperar_registo(caminho_debug, processo, espera_s, dormir, relogio)
    if veredito.registado:
        caminho, motivo = CAMINHO_CANAL, ""
        log(f"sessao {projeto.nome}: caminho={CAMINHO_CANAL} (canal registado)")
    else:
        caminho, motivo = CAMINHO_HEADLESS, veredito.motivo or veredito.estado
        log(
            f"sessao {projeto.nome}: caminho={CAMINHO_HEADLESS} ({veredito.estado}: {motivo}); "
            "os pedidos vao para uma sessao headless na pasta do projeto, retomavel com "
            "claude --resume <id>"
        )
    return SessaoAberta(
        projeto=projeto,
        caminho=caminho,
        motivo=motivo,
        comando=argumentos,
        config_mcp=caminho_mcp,
        debug=caminho_debug,
        processo=processo,
        aberta_em=aberta_em,
    )


# --- IPC do lado do jarvis --------------------------------------------------


@dataclass(frozen=True)
class RespostaDoCanal:
    projeto: str
    texto: str
    falado: str
    pedido: str
    recebida_em: float


class CentralDoCanal:
    """O lado do jarvis do IPC: ouve so em 127.0.0.1, com um segredo novo por arranque.

    Cada canal apresenta-se com `ola` (segredo + projeto conhecido); sem isso a
    ligacao e fechada. Depois so aceita `resposta` desse mesmo projeto. Um
    hook de aviso manda uma so linha `evento` (segredo + projeto conhecido +
    evento da lista fechada) e a ligacao fecha; o evento vai para
    `ao_evento(projeto, evento, sessao)`. O endereco (porta + segredo) vai
    para um ficheiro ignorado pelo Git.
    """

    def __init__(
        self,
        projetos: Iterable[str],
        caminho_endereco: Path = canal_mcp.FICHEIRO_IPC,
        log: Callable[[str], None] = registar,
        maximo_ligacoes: int = 16,
        espera_do_ola_s: float = 5.0,
        ao_evento: Callable[[str, str, str], object] | None = None,
    ):
        self.projetos = frozenset(validar_nome_de_projeto(nome) for nome in projetos)
        self.ao_evento = ao_evento
        self.caminho_endereco = Path(caminho_endereco)
        self.log = log
        self.maximo_ligacoes = maximo_ligacoes
        self.espera_do_ola_s = espera_do_ola_s
        self.recusadas = 0
        self.endereco: EnderecoIpc | None = None
        self._servidor: socket.socket | None = None
        self._ligacoes: dict[str, socket.socket] = {}
        self._abertas: set[socket.socket] = set()
        self._respostas: dict[str, list[RespostaDoCanal]] = {}
        self._condicao = threading.Condition()
        self._envio = threading.Lock()
        self._parar = threading.Event()

    # -- ciclo de vida

    def iniciar(self) -> "CentralDoCanal":
        if self._servidor is not None:
            return self
        servidor = socket.create_server((HOST_IPC, 0))
        self._servidor = servidor
        self.endereco = EnderecoIpc(servidor.getsockname()[1], gerar_segredo(), os.getpid())
        escrever_endereco(self.endereco, self.caminho_endereco)
        threading.Thread(target=self._aceitar, name="central-canal", daemon=True).start()
        return self

    def parar(self) -> None:
        self._parar.set()
        if self._servidor is not None:
            canal_mcp._fechar_socket(self._servidor)
            self._servidor = None
        with self._condicao:
            abertas = list(self._abertas)
            self._ligacoes.clear()
            self._condicao.notify_all()
        for sock in abertas:
            canal_mcp._fechar_socket(sock)
        if self.endereco is not None:
            apagar_endereco_se_for(self.endereco, self.caminho_endereco)

    def __enter__(self) -> "CentralDoCanal":
        return self.iniciar()

    def __exit__(self, *_excecao: object) -> None:
        self.parar()

    # -- estado

    def ligado(self, projeto: str) -> bool:
        with self._condicao:
            return projeto in self._ligacoes

    def esperar_ligacao(self, projeto: str, limite_s: float) -> bool:
        with self._condicao:
            return self._condicao.wait_for(
                lambda: projeto in self._ligacoes or self._parar.is_set(), limite_s
            ) and projeto in self._ligacoes

    # -- envio e rececao

    def enviar_prompt(self, projeto: str, texto: str) -> str:
        """Empurra o prompt confirmado para o canal do projeto; devolve o id do pedido."""
        if not isinstance(texto, str) or not texto.strip():
            raise ValueError("prompt vazio")
        if "\x00" in texto or len(texto) > MAXIMO_CARACTERES_DE_TEXTO:
            raise ValueError("prompt recusado: NUL ou grande demais")
        if projeto not in self.projetos:
            raise ValueError("projeto desconhecido")
        assert self.endereco is not None
        pedido = novo_pedido()
        linha = linha_de_mensagem(
            TIPO_PROMPT, self.endereco.segredo, projeto=projeto, pedido=pedido, texto=texto
        )
        with self._condicao:
            sock = self._ligacoes.get(projeto)
        if sock is None:
            raise CanalIndisponivel(f"o canal do projeto {projeto} nao esta ligado ao jarvis")
        try:
            with self._envio:
                sock.sendall(linha)
        except OSError as erro:
            raise CanalIndisponivel(f"o canal do projeto {projeto} caiu: {erro}") from erro
        return pedido

    def esperar_resposta(
        self, projeto: str, pedido: str | None = None, limite_s: float = ESPERA_DA_RESPOSTA_S
    ) -> RespostaDoCanal | None:
        """A primeira resposta do projeto a este pedido (ou sem pedido), ou None."""

        def encontrar() -> RespostaDoCanal | None:
            for resposta in self._respostas.get(projeto, []):
                if pedido is None or resposta.pedido in (pedido, ""):
                    return resposta
            return None

        with self._condicao:
            self._condicao.wait_for(lambda: encontrar() is not None or self._parar.is_set(), limite_s)
            resposta = encontrar()
            if resposta is not None:
                self._respostas[projeto].remove(resposta)
            return resposta

    def _tratar_evento(self, campos: dict[str, str]) -> None:
        """Um evento de hook ja autenticado: so passa o que esta na lista fechada."""
        evento = campos["evento"]
        if evento not in avisos.EVENTOS_DA_SESSAO:
            self._recusar("evento desconhecido")
            return
        sessao = campos.get("sessao", "")
        if sessao and not avisos.PADRAO_SESSAO.match(sessao):
            self._recusar("sessao invalida")
            return
        self.log(f"evento de {campos['projeto']}: {evento}")
        if self.ao_evento is not None:
            try:
                self.ao_evento(campos["projeto"], evento, sessao)
            except Exception as erro:  # noqa: BLE001 - um aviso falhado nunca derruba o IPC
                self.log(f"evento de {campos['projeto']}: aviso falhou ({type(erro).__name__})")

    # -- threads

    def _aceitar(self) -> None:
        servidor = self._servidor
        while servidor is not None and not self._parar.is_set():
            try:
                sock, _origem = servidor.accept()
            except OSError:
                return
            with self._condicao:
                cheio = len(self._abertas) >= self.maximo_ligacoes
                if not cheio:
                    self._abertas.add(sock)
            if cheio:
                self._recusar("ligacoes a mais")
                canal_mcp._fechar_socket(sock)
                continue
            threading.Thread(target=self._atender, args=(sock,), daemon=True).start()

    def _recusar(self, motivo: str) -> None:
        self.recusadas += 1
        self.log(f"canal: mensagem recusada ({motivo})")

    def _atender(self, sock: socket.socket) -> None:
        projeto: str | None = None
        try:
            assert self.endereco is not None
            segredo = self.endereco.segredo
            leitor = LeitorDeLinhas(sock, self._parar)
            try:
                linha = leitor.linha(self.espera_do_ola_s)
                if linha is None:
                    return
                ola = validar_mensagem(linha, segredo=segredo, tipos={TIPO_OLA, TIPO_EVENTO})
            except MensagemRecusada as recusa:
                self._recusar(f"ligacao sem apresentacao valida: {recusa}")
                return
            except TimeoutError:
                self._recusar("ligacao sem apresentacao no tempo")
                return
            if ola["projeto"] not in self.projetos:
                self._recusar("projeto desconhecido")
                return
            if ola["tipo"] == TIPO_EVENTO:
                self._tratar_evento(ola)
                return
            projeto = ola["projeto"]
            with self._condicao:
                antigo = self._ligacoes.get(projeto)
                self._ligacoes[projeto] = sock
                self._condicao.notify_all()
            if antigo is not None:
                canal_mcp._fechar_socket(antigo)
            self.log(f"canal ligado: {projeto}")
            while not self._parar.is_set():
                try:
                    linha = leitor.linha()
                except LinhaGrandeDemais:
                    self._recusar("linha grande demais; ligacao fechada")
                    return
                if linha is None:
                    return
                try:
                    campos = validar_mensagem(
                        linha, segredo=segredo, tipos={TIPO_RESPOSTA}, projeto=projeto
                    )
                except MensagemRecusada as recusa:
                    self._recusar(str(recusa))
                    continue
                resposta = RespostaDoCanal(
                    projeto=projeto,
                    texto=campos["texto"],
                    # o canal ja filtrou, mas o que vai para a voz e verificado deste lado
                    falado=resumo_falado(campos["texto"]),
                    pedido=campos.get("pedido", ""),
                    recebida_em=time.monotonic(),
                )
                with self._condicao:
                    fila = self._respostas.setdefault(projeto, [])
                    fila.append(resposta)
                    del fila[:-MAXIMO_RESPOSTAS_EM_ESPERA]
                    self._condicao.notify_all()
        except OSError:
            return
        finally:
            with self._condicao:
                self._abertas.discard(sock)
                if projeto is not None and self._ligacoes.get(projeto) is sock:
                    del self._ligacoes[projeto]
                    self._condicao.notify_all()
            canal_mcp._fechar_socket(sock)


# --- Recurso headless e entrega --------------------------------------------


@dataclass
class Entrega:
    projeto: str
    caminho: str
    texto: str = ""
    falado: str = ""
    session_id: str | None = None
    enviado_em: str = ""
    respondido_em: str = ""
    segundos: float | None = None
    erro: str = ""
    motivo: str = ""

    @property
    def comando_para_retomar(self) -> str:
        return f"claude --resume {self.session_id}" if self.session_id else ""

    def frase_para_o_utilizador(self) -> str:
        if self.erro:
            return f"Não consegui entregar o pedido ao {self.projeto}: {self.erro}"
        if self.caminho == CAMINHO_HEADLESS:
            return (
                f"Enviei o pedido ao {self.projeto} numa sessão em segundo plano, porque o "
                "canal não registou. Retomas com claude resume; o comando está no ecrã."
            )
        return f"Pedido entregue na sessão do {self.projeto}."


class SessaoHeadless:
    """Sessao stream-json na pasta do projeto, viva entre pedidos."""

    def __init__(
        self,
        projeto: Projeto,
        cli: str | None = None,
        criar_canal: Callable[..., Any] = CanalStreamJson,
    ):
        self.projeto = projeto
        self.canal = criar_canal(
            cwd=Path(projeto.caminho).resolve(), argumentos=comando_sessao_do_projeto(cli)
        )
        self._aberto = False

    def perguntar(self, texto: str, limite_s: float) -> tuple[str, str | None, str]:
        """Devolve (resposta, session_id, erro)."""
        if not self._aberto:
            self.canal.abrir()
            self._aberto = True
        recolhida = self.canal.perguntar_detalhado(texto, limite_s)
        session_id = recolhida.session_id or getattr(self.canal, "session_id", None)
        if session_id is not None:
            try:
                validar_session_id(session_id)
            except ValueError:
                session_id = None
        return recolhida.texto, session_id, recolhida.erro

    def fechar(self) -> None:
        if self._aberto:
            self.canal.fechar()
            self._aberto = False


class CanalDoProjeto:
    """Entrega os prompts confirmados de um projeto pelo caminho que ficou decidido."""

    def __init__(
        self,
        sessao: SessaoAberta,
        central: CentralDoCanal | None,
        *,
        cli: str | None = None,
        criar_headless: Callable[..., SessaoHeadless] = SessaoHeadless,
        log: Callable[[str], None] = registar,
        espera_da_ligacao_s: float = ESPERA_DA_LIGACAO_S,
    ):
        self.sessao = sessao
        self.central = central
        self.cli = cli
        self.criar_headless = criar_headless
        self.log = log
        self.espera_da_ligacao_s = espera_da_ligacao_s
        self.caminho = sessao.caminho if central is not None else CAMINHO_HEADLESS
        self.motivo = sessao.motivo if central is not None else "o jarvis nao tem o canal ligado"
        self._headless: SessaoHeadless | None = None

    @property
    def nome(self) -> str:
        return self.sessao.projeto.nome

    def entregar(self, texto: str, limite_s: float = ESPERA_DA_RESPOSTA_S) -> Entrega:
        """Entrega UM prompt ja confirmado. Nunca o envia duas vezes."""
        if self.caminho == CAMINHO_CANAL:
            assert self.central is not None
            if self.central.esperar_ligacao(self.nome, self.espera_da_ligacao_s):
                return self._pelo_canal(texto, limite_s)
            self.caminho = CAMINHO_HEADLESS
            self.motivo = "o canal registou mas nao se ligou ao jarvis"
            self.log(f"sessao {self.nome}: caminho={CAMINHO_HEADLESS} ({self.motivo})")
        return self._headless_entregar(texto, limite_s)

    def _pelo_canal(self, texto: str, limite_s: float) -> Entrega:
        assert self.central is not None
        entrega = Entrega(projeto=self.nome, caminho=CAMINHO_CANAL, enviado_em=agora_iso())
        inicio = time.monotonic()
        try:
            pedido = self.central.enviar_prompt(self.nome, texto)
        except (CanalIndisponivel, ValueError) as erro:
            entrega.erro = str(erro)
            self.log(f"entrega {self.nome}: caminho={CAMINHO_CANAL} falhou ({erro})")
            return entrega
        self.log(f"entrega {self.nome}: caminho={CAMINHO_CANAL} prompt entregue")
        resposta = self.central.esperar_resposta(self.nome, pedido, limite_s)
        if resposta is None:
            # o prompt ja esta na sessao: reenviar por outro caminho duplicava-o
            entrega.erro = f"sem resposta em {limite_s:.0f} s (o pedido esta na janela da sessao)"
            self.log(f"entrega {self.nome}: caminho={CAMINHO_CANAL} {entrega.erro}")
            return entrega
        entrega.texto, entrega.falado = resposta.texto, resposta.falado
        entrega.respondido_em = agora_iso()
        entrega.segundos = round(time.monotonic() - inicio, 2)
        self.log(f"entrega {self.nome}: caminho={CAMINHO_CANAL} resposta em {entrega.segundos} s")
        return entrega

    def _headless_entregar(self, texto: str, limite_s: float) -> Entrega:
        entrega = Entrega(
            projeto=self.nome, caminho=CAMINHO_HEADLESS, enviado_em=agora_iso(), motivo=self.motivo
        )
        inicio = time.monotonic()
        try:
            if self._headless is None:
                self._headless = self.criar_headless(self.sessao.projeto, cli=self.cli)
            resposta, session_id, erro = self._headless.perguntar(texto, limite_s)
        except (OSError, ValueError, CanalIndisponivel) as falha:
            resposta, session_id, erro = "", None, str(falha)
        entrega.session_id = session_id
        entrega.segundos = round(time.monotonic() - inicio, 2)
        if erro:
            entrega.erro = erro
        else:
            entrega.texto, entrega.falado = resposta, resumo_falado(resposta)
            entrega.respondido_em = agora_iso()
        retomar = entrega.comando_para_retomar or "claude --resume (sem id: a sessao nao arrancou)"
        self.log(
            f"entrega {self.nome}: caminho={CAMINHO_HEADLESS} ({self.motivo}); "
            f"{'erro: ' + entrega.erro if entrega.erro else f'resposta em {entrega.segundos} s'}; "
            f"retomar na pasta do projeto com: {retomar}"
        )
        return entrega

    def fechar(self) -> None:
        if self._headless is not None:
            self._headless.fechar()
            self._headless = None


# --- Prova real numa pasta de teste ----------------------------------------

PROMPT_DA_PROVA = (
    "Isto e um teste do canal do jarvis. Responde apenas OK, com a ferramenta responder se a "
    "tiveres."
)


def arvore(pasta: Path) -> dict[str, tuple[int, str]]:
    """Retrato da pasta: caminho relativo -> (tamanho, sha256)."""
    retrato: dict[str, tuple[int, str]] = {}
    for caminho in sorted(Path(pasta).rglob("*")):
        relativo = caminho.relative_to(pasta).as_posix()
        if caminho.is_dir():
            retrato[relativo + "/"] = (0, "")
        else:
            dados = caminho.read_bytes()
            retrato[relativo] = (len(dados), hashlib.sha256(dados).hexdigest())
    return retrato


def _criar_pasta_de_prova() -> Path:
    pasta = PASTA_DE_PROVAS / f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    pasta.mkdir(parents=True)
    (pasta / "LEIA.txt").write_text(
        "Pasta de teste do canal do jarvis. Pode ser apagada.\n", encoding="utf-8"
    )
    return pasta


def correr_prova(
    headless: bool = False, espera_s: float = ESPERA_DO_REGISTO_S, log: Callable[[str], None] = registar
) -> int:
    """Prova real contra o CLI: prompt entregue e resposta curta devolvida, com horas."""
    pasta = _criar_pasta_de_prova()
    projeto = Projeto(nome="prova", caminho=pasta)
    antes = arvore(pasta)
    log(f"prova: pasta de teste criada ({pasta.relative_to(RAIZ).as_posix()})")
    estado = PASTA_DE_SESSOES / "prova"
    ipc = estado / "ipc.json"
    central = CentralDoCanal([projeto.nome], ipc, log=log).iniciar()
    sessao: SessaoAberta | None = None
    canal: CanalDoProjeto | None = None
    entrega = Entrega(projeto=projeto.nome, caminho="", erro="a prova nao chegou a entregar")
    try:
        if headless:
            sessao = SessaoAberta(
                projeto=projeto,
                caminho=CAMINHO_HEADLESS,
                motivo="pedido --headless na prova",
                comando=[],
                config_mcp=Path(),
                debug=Path(),
                aberta_em=agora_iso(),
            )
            log("prova: so o recurso headless")
        else:
            log(
                "prova: na janela nova confirma 'Yes, I trust this folder' e depois "
                "'I am using this for local development'"
            )
            sessao = abrir_sessao(projeto, ipc=ipc, espera_s=espera_s, log=log)
        log(f"prova: {sessao.frase_para_o_utilizador()}")
        canal = CanalDoProjeto(sessao, central, log=log)
        entrega = canal.entregar(PROMPT_DA_PROVA, limite_s=180)
        log(
            "prova: "
            + json.dumps(
                {
                    "caminho": entrega.caminho,
                    "motivo": entrega.motivo,
                    "enviado_em": entrega.enviado_em,
                    "respondido_em": entrega.respondido_em,
                    "segundos": entrega.segundos,
                    "resposta": entrega.texto[:200],
                    "falado": entrega.falado,
                    "retomar": entrega.comando_para_retomar,
                    "erro": entrega.erro,
                },
                ensure_ascii=False,
            )
        )
        log(f"prova: {entrega.frase_para_o_utilizador()}")
    finally:
        if canal is not None:
            canal.fechar()
        if sessao is not None and sessao.processo is not None and sessao.processo.poll() is None:
            sessao.processo.terminate()
            try:
                sessao.processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sessao.processo.kill()
        central.parar()
    depois = arvore(pasta)
    igual = antes == depois
    log(f"prova: pasta do projeto igual antes/depois: {'sim' if igual else 'NAO'}")
    if not igual:
        log(f"prova: diferencas: {sorted(set(antes.items()) ^ set(depois.items()))[:10]}")
    ok = igual and not entrega.erro and "OK" in entrega.texto.upper()
    log(f"prova: {'PASSOU' if ok else 'FALHOU'}")
    return 0 if ok else 1


# --- Linha de comandos ------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="python -m jarvis.sessoes")
    comandos = analisador.add_subparsers(dest="comando", required=True)
    abrir = comandos.add_parser("abrir", help="abre a sessao do projeto com o canal do jarvis")
    abrir.add_argument("projeto", help="nome do projeto como esta no config.toml")
    abrir.add_argument("--espera", type=float, default=ESPERA_DO_REGISTO_S)
    prova = comandos.add_parser("prova", help="prova real numa pasta de teste em tmp/")
    prova.add_argument("--headless", action="store_true")
    prova.add_argument("--espera", type=float, default=ESPERA_DO_REGISTO_S)
    argumentos = analisador.parse_args(argv)

    if argumentos.comando == "prova":
        return correr_prova(argumentos.headless, argumentos.espera)

    try:
        config = carregar_config()
    except ConfigError as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    projeto = config.encontrar_projeto(argumentos.projeto)
    if projeto is None:
        conhecidos = ", ".join(p.nome for p in config.projetos) or "(nenhum)"
        print(
            f"erro: projeto '{argumentos.projeto}' nao esta no config.toml. Conhecidos: {conhecidos}",
            file=sys.stderr,
        )
        return 2
    try:
        sessao = abrir_sessao(projeto, espera_s=argumentos.espera)
    except (ConfigError, ValueError, FileNotFoundError) as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    print(sessao.frase_para_o_utilizador())
    if sessao.caminho == CAMINHO_HEADLESS:
        print(
            f"Motivo: {sessao.motivo}. Depois do primeiro pedido, retoma na pasta do projeto "
            "com: claude --resume <id> (o id fica no log logs/sessoes-<data>.log)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
