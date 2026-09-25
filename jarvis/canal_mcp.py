r"""Canal do jarvis dentro de uma sessao interativa do Claude Code (servidor MCP stdio).

O Claude Code arranca este processo a partir de um --mcp-config gerado pelo jarvis
(`python -m jarvis.sessoes abrir <projeto>`) e liga-o como "channel" com
`--dangerously-load-development-channels server:jarvis`. A partir dai:

  jarvis --(IPC local autenticado)--> este processo --notifications/claude/channel--> sessao
  sessao --(ferramenta `responder`)--> este processo --(IPC)--> jarvis --> voz

Regras deste modulo:

- Declara so a capacidade experimental `claude/channel`. NUNCA declara
  `claude/channel/permission`: a voz nao aprova ferramentas, os pedidos de
  permissao ficam no terminal, a frente do utilizador.
- O texto da ferramenta `responder` passa pelo filtro da resposta falada
  (`jarvis.resposta_falada.resumo_falado`) antes de seguir para o jarvis: o que
  chega a voz nunca e codigo nem chamadas de ferramentas. O texto inteiro segue a
  parte, so para o ecra e para o log.
- O IPC e so local (127.0.0.1). O jarvis escreve a porta e um segredo gerado em
  cada arranque num ficheiro ignorado pelo Git (`.jarvis/ipc.json`). Cada
  mensagem, nos dois sentidos, leva o segredo e o projeto; uma mensagem sem
  segredo, com segredo errado, com CR/LF fora do campo de texto ou para outro
  projeto e recusada e nunca chega a sessao (`validar_mensagem`).
- Este processo nao abre nenhuma porta: so se liga ao jarvis.
- stdout e so do protocolo MCP; os avisos vao para stderr, sem segredo nem
  conteudo das mensagens.

Uso (quem o arranca e o Claude Code, pelo --mcp-config gerado):

    python -m jarvis.canal_mcp --projeto <nome> [--ipc <ficheiro>]

Autoteste com um cliente MCP falso e um jarvis falso, sem CLI nem rede externa:

    .venv\Scripts\python -m jarvis.canal_mcp --autoteste
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

if __package__ in (None, ""):
    # arrancado como script: o Claude Code pode correr isto com outro cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.resposta_falada import resumo_falado  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent

# --- Contrato MCP -----------------------------------------------------------

#: Nome do servidor no --mcp-config e em `server:<nome>` do channel.
NOME_DO_SERVIDOR = "jarvis"
VERSAO_DO_SERVIDOR = "0.1.0"

CAPACIDADE_DO_CANAL = "claude/channel"
#: Capacidade que este servidor nunca declara (relay de permissoes por voz).
CAPACIDADE_PROIBIDA = "claude/channel/permission"

METODO_DO_CANAL = "notifications/claude/channel"
FERRAMENTA_DE_RESPOSTA = "responder"

#: Revisoes do MCP com notificacoes nao pedidas do servidor para o cliente, que
#: e o que o channel precisa. Um pedido de outra revisao recebe a ultima destas.
VERSOES_MCP_ACEITES = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
VERSAO_MCP_PADRAO = "2025-06-18"

ERRO_METODO_DESCONHECIDO = -32601
ERRO_PARAMETROS_INVALIDOS = -32602
ERRO_PEDIDO_INVALIDO = -32600

INSTRUCOES = (
    "As mensagens do canal 'jarvis' sao pedidos que o utilizador ditou por voz e "
    "confirmou antes de enviar. Trata cada uma como se ele a tivesse escrito no "
    "terminal. Quando acabares, chama a ferramenta 'responder' com um resumo curto "
    "em linguagem natural (uma ou duas frases, sem codigo, sem caminhos, na lingua "
    "da mensagem): esse texto e lido em voz alta. Se o pedido ficar a espera de "
    "uma resposta dele, termina o resumo com essa pergunta."
)

FERRAMENTA = {
    "name": FERRAMENTA_DE_RESPOSTA,
    "description": (
        "Devolve ao jarvis um resumo curto, falavel, do que fizeste ou da pergunta "
        "que tens para o utilizador. O texto e lido em voz alta depois de filtrado."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "texto": {"type": "string", "description": "Uma ou duas frases, sem codigo."},
            "pedido": {
                "type": "string",
                "description": "O atributo 'pedido' da mensagem do canal a que respondes.",
            },
        },
        "required": ["texto"],
    },
}

# --- Contrato do IPC local --------------------------------------------------

PASTA_DE_ESTADO = RAIZ / ".jarvis"
#: Porta e segredo do jarvis em execucao; reescrito em cada arranque.
FICHEIRO_IPC = PASTA_DE_ESTADO / "ipc.json"
HOST_IPC = "127.0.0.1"

MAXIMO_BYTES_POR_LINHA = 64 * 1024
MAXIMO_CARACTERES_DE_TEXTO = 20000

TIPO_OLA = "ola"
TIPO_PROMPT = "prompt"
TIPO_RESPOSTA = "resposta"
#: Aviso de um hook do Claude Code (a sessao acabou ou esta a espera). Uma
#: ligacao curta: o hook manda uma linha e fecha; nunca leva conteudo.
TIPO_EVENTO = "evento"

#: Campos de cada tipo de mensagem: (obrigatorios, opcionais). O segredo e
#: sempre obrigatorio e nunca sai de `validar_mensagem`.
CAMPOS_POR_TIPO: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    TIPO_OLA: (frozenset({"tipo", "segredo", "projeto"}), frozenset()),
    TIPO_PROMPT: (frozenset({"tipo", "segredo", "projeto", "pedido", "texto"}), frozenset()),
    TIPO_RESPOSTA: (
        frozenset({"tipo", "segredo", "projeto", "texto", "falado"}),
        frozenset({"pedido"}),
    ),
    TIPO_EVENTO: (frozenset({"tipo", "segredo", "projeto", "evento"}), frozenset({"sessao"})),
}

#: O unico campo que pode ter quebras de linha (o que o utilizador ditou ou o
#: que o Claude respondeu). Todos os outros sao de uma linha so.
CAMPO_DE_TEXTO = "texto"

#: Nomes de projeto aceites no IPC e na linha de comandos do canal.
PADRAO_NOME_DE_PROJETO = re.compile(r"\A[^\W_][\w .-]{0,63}\Z")
PADRAO_SEGREDO = re.compile(r"\A[0-9a-f]{64}\Z")
PADRAO_PEDIDO = re.compile(r"\A[0-9a-f]{32}\Z")


class MensagemRecusada(ValueError):
    """Uma mensagem do IPC que nao passou a validacao. A mensagem nunca e usada."""


def validar_nome_de_projeto(nome: object) -> str:
    """Devolve o nome se for seguro para o IPC e para argv; levanta ValueError."""
    if not isinstance(nome, str) or not PADRAO_NOME_DE_PROJETO.match(nome):
        raise ValueError(
            "nome de projeto recusado: so letras, algarismos, espaco, ponto, hifen e "
            "sublinhado, ate 64 caracteres, a comecar por letra ou algarismo"
        )
    return nome


def gerar_segredo() -> str:
    """Segredo novo para um arranque do jarvis (256 bits, hexadecimal)."""
    return secrets.token_hex(32)


def novo_pedido() -> str:
    return secrets.token_hex(16)


def _sem_quebras(valor: str, campo: str) -> None:
    for caractere, nome in (("\r", "CR"), ("\n", "LF"), ("\x00", "NUL")):
        if caractere in valor:
            raise MensagemRecusada(f"{nome} no campo '{campo}' (so '{CAMPO_DE_TEXTO}' aceita)")


def validar_mensagem(
    linha: bytes | str,
    *,
    segredo: str,
    tipos: frozenset[str] | set[str] | tuple[str, ...],
    projeto: str | None = None,
) -> dict[str, str]:
    """Valida uma linha do IPC e devolve os campos sem o segredo.

    Levanta MensagemRecusada com um motivo curto (sem ecoar o conteudo) quando:
    a linha e grande demais ou nao e UTF-8; tem CR/LF crus; nao e um objeto JSON;
    nao traz segredo ou traz o errado; o tipo nao e esperado aqui; faltam ou
    sobram campos; um campo que nao e o texto tem CR/LF/NUL; ou o projeto nao e
    o desta ligacao. O segredo verifica-se antes de tudo o resto.
    """
    if isinstance(linha, bytes):
        if len(linha) > MAXIMO_BYTES_POR_LINHA:
            raise MensagemRecusada("linha grande demais")
        try:
            linha = linha.decode("utf-8")
        except UnicodeDecodeError:
            raise MensagemRecusada("linha nao e UTF-8") from None
    elif len(linha.encode("utf-8", "surrogatepass")) > MAXIMO_BYTES_POR_LINHA:
        raise MensagemRecusada("linha grande demais")
    if linha.endswith("\n"):
        linha = linha[:-1]
    if "\r" in linha or "\n" in linha:
        raise MensagemRecusada("CR/LF fora do campo de texto")
    try:
        obj = json.loads(linha)
    except (json.JSONDecodeError, RecursionError):
        raise MensagemRecusada("nao e JSON") from None
    if not isinstance(obj, dict):
        raise MensagemRecusada("nao e um objeto JSON")

    recebido = obj.get("segredo")
    if not isinstance(recebido, str) or not recebido:
        raise MensagemRecusada("sem segredo")
    if not segredo or not hmac.compare_digest(
        recebido.encode("utf-8", "surrogatepass"), segredo.encode("utf-8")
    ):
        raise MensagemRecusada("segredo errado")

    tipo = obj.get("tipo")
    if not isinstance(tipo, str) or tipo not in tipos or tipo not in CAMPOS_POR_TIPO:
        raise MensagemRecusada("tipo nao esperado")
    obrigatorios, opcionais = CAMPOS_POR_TIPO[tipo]
    chaves = set(obj)
    if not obrigatorios <= chaves:
        raise MensagemRecusada("faltam campos")
    if chaves - obrigatorios - opcionais:
        raise MensagemRecusada("campos a mais")

    campos: dict[str, str] = {}
    for chave, valor in obj.items():
        if not isinstance(valor, str):
            raise MensagemRecusada(f"campo '{chave}' nao e texto")
        if chave == CAMPO_DE_TEXTO:
            if "\x00" in valor:
                raise MensagemRecusada("NUL no texto")
            if len(valor) > MAXIMO_CARACTERES_DE_TEXTO:
                raise MensagemRecusada("texto grande demais")
        else:
            _sem_quebras(valor, chave)
        if chave != "segredo":
            campos[chave] = valor

    try:
        validar_nome_de_projeto(campos["projeto"])
    except ValueError:
        raise MensagemRecusada("nome de projeto invalido") from None
    if projeto is not None and campos["projeto"] != projeto:
        raise MensagemRecusada("mensagem para outro projeto")
    pedido = campos.get("pedido")
    if pedido is not None and pedido != "" and not PADRAO_PEDIDO.match(pedido):
        raise MensagemRecusada("pedido invalido")
    return campos


def linha_de_mensagem(tipo: str, segredo: str, **campos: str) -> bytes:
    """Uma linha NDJSON do IPC (ASCII: o texto vai sempre escapado pelo JSON)."""
    return (json.dumps({"tipo": tipo, "segredo": segredo, **campos}, ensure_ascii=True) + "\n").encode(
        "ascii"
    )


# --- Ficheiro com a porta e o segredo --------------------------------------


@dataclass(frozen=True)
class EnderecoIpc:
    porta: int
    segredo: str
    pid: int


def escrever_endereco(endereco: EnderecoIpc, caminho: Path = FICHEIRO_IPC) -> None:
    """Escreve o endereco de forma atomica (ficheiro temporario + replace)."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conteudo = json.dumps(
        {"porta": endereco.porta, "segredo": endereco.segredo, "pid": endereco.pid}
    )
    descritor, temporario = tempfile.mkstemp(prefix=".ipc-", dir=str(caminho.parent))
    try:
        with os.fdopen(descritor, "w", encoding="ascii") as ficheiro:
            ficheiro.write(conteudo)
        os.replace(temporario, caminho)
    except BaseException:
        try:
            os.unlink(temporario)
        except OSError:
            pass
        raise


def ler_endereco(caminho: Path = FICHEIRO_IPC) -> EnderecoIpc | None:
    """O endereco do jarvis em execucao, ou None se nao houver um valido."""
    try:
        bruto = json.loads(Path(caminho).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(bruto, dict):
        return None
    porta, segredo, pid = bruto.get("porta"), bruto.get("segredo"), bruto.get("pid")
    if not isinstance(porta, int) or isinstance(porta, bool) or not 1 <= porta <= 65535:
        return None
    if not isinstance(segredo, str) or not PADRAO_SEGREDO.match(segredo):
        return None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return EnderecoIpc(porta=porta, segredo=segredo, pid=pid)


def apagar_endereco_se_for(endereco: EnderecoIpc, caminho: Path = FICHEIRO_IPC) -> None:
    """Apaga o ficheiro so se ainda for o deste arranque (nunca o de um arranque mais novo)."""
    atual = ler_endereco(caminho)
    if atual is not None and atual.segredo == endereco.segredo:
        try:
            Path(caminho).unlink()
        except OSError:
            pass


class LinhaGrandeDemais(MensagemRecusada):
    """A linha passou do maximo sem acabar: a ligacao ja nao se consegue ressincronizar."""


class LeitorDeLinhas:
    """Le linhas de um socket com um timeout curto, para a thread poder parar.

    No Windows, fechar o socket noutra thread nao acorda um recv bloqueado; por
    isso nunca se bloqueia mais do que `passo_s` sem voltar a ver `parar`.
    """

    def __init__(
        self,
        sock: socket.socket,
        parar: threading.Event,
        passo_s: float = 0.25,
        maximo: int = MAXIMO_BYTES_POR_LINHA,
    ):
        self.sock = sock
        self.parar = parar
        self.maximo = maximo
        self._tampao = b""
        sock.settimeout(passo_s)

    def linha(self, limite_s: float | None = None) -> bytes | None:
        """A proxima linha (com o LF); None se a ligacao fechou ou se mandaram parar.

        Levanta TimeoutError se `limite_s` passar sem linha completa e
        LinhaGrandeDemais se a linha passar do maximo.
        """
        fim = None if limite_s is None else time.monotonic() + limite_s
        while True:
            indice = self._tampao.find(b"\n")
            if indice >= 0:
                linha, self._tampao = self._tampao[: indice + 1], self._tampao[indice + 1 :]
                if len(linha) > self.maximo:
                    raise LinhaGrandeDemais("linha grande demais")
                return linha
            if len(self._tampao) > self.maximo:
                raise LinhaGrandeDemais("linha grande demais")
            if self.parar.is_set():
                return None
            if fim is not None and time.monotonic() >= fim:
                raise TimeoutError("sem linha no tempo")
            try:
                dados = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not dados:
                return None
            self._tampao += dados


def _avisar(texto: str) -> None:
    print(f"[canal-jarvis] {texto}", file=sys.stderr, flush=True)


# --- Ligacao ao jarvis (cliente IPC) ---------------------------------------


class LigacaoAoJarvis:
    """Liga este canal ao jarvis em execucao e mantem a ligacao viva.

    Le o endereco em cada tentativa (o jarvis pode reiniciar com porta e segredo
    novos), apresenta-se com `ola` e entrega a `ao_receber_prompt` so os prompts
    validos para este projeto.
    """

    def __init__(
        self,
        projeto: str,
        ao_receber_prompt: Callable[[str, str], None],
        caminho_endereco: Path = FICHEIRO_IPC,
        intervalo_s: float = 1.0,
        avisar: Callable[[str], None] = _avisar,
    ):
        self.projeto = validar_nome_de_projeto(projeto)
        self.ao_receber_prompt = ao_receber_prompt
        self.caminho_endereco = Path(caminho_endereco)
        self.intervalo_s = intervalo_s
        self.avisar = avisar
        self.recusadas = 0
        self._socket: socket.socket | None = None
        self._segredo: str = ""
        self._trinco = threading.Lock()
        self._parar = threading.Event()
        self._ligado = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def ligado(self) -> bool:
        return self._ligado.is_set()

    def esperar_ligacao(self, limite_s: float) -> bool:
        return self._ligado.wait(limite_s)

    def iniciar(self) -> "LigacaoAoJarvis":
        if self._thread is None:
            self._thread = threading.Thread(target=self._ciclo, name="ligacao-jarvis", daemon=True)
            self._thread.start()
        return self

    def parar(self) -> None:
        self._parar.set()
        with self._trinco:
            sock, self._socket = self._socket, None
        if sock is not None:
            _fechar_socket(sock)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def enviar_resposta(self, texto: str, falado: str, pedido: str = "") -> bool:
        """Envia a resposta ao jarvis; False se nao houver ligacao."""
        campos = {"projeto": self.projeto, "texto": texto, "falado": falado}
        if pedido:
            campos["pedido"] = pedido
        with self._trinco:
            sock = self._socket
            if sock is None:
                return False
            try:
                sock.sendall(linha_de_mensagem(TIPO_RESPOSTA, self._segredo, **campos))
            except OSError:
                return False
        return True

    def _ciclo(self) -> None:
        while not self._parar.is_set():
            endereco = ler_endereco(self.caminho_endereco)
            if endereco is not None:
                try:
                    self._sessao(endereco)
                except OSError:
                    pass
                finally:
                    self._ligado.clear()
                    with self._trinco:
                        sock, self._socket = self._socket, None
                    if sock is not None:
                        _fechar_socket(sock)
            self._parar.wait(self.intervalo_s)

    def _sessao(self, endereco: EnderecoIpc) -> None:
        sock = socket.create_connection((HOST_IPC, endereco.porta), timeout=5)
        leitor = LeitorDeLinhas(sock, self._parar)
        with self._trinco:
            if self._parar.is_set():
                _fechar_socket(sock)
                return
            self._socket = sock
            self._segredo = endereco.segredo
            sock.sendall(linha_de_mensagem(TIPO_OLA, endereco.segredo, projeto=self.projeto))
        self._ligado.set()
        while not self._parar.is_set():
            try:
                linha = leitor.linha()
            except LinhaGrandeDemais:
                self.recusadas += 1
                self.avisar("mensagem recusada: linha grande demais; ligacao fechada")
                return
            if linha is None:
                return
            try:
                campos = validar_mensagem(
                    linha, segredo=endereco.segredo, tipos={TIPO_PROMPT}, projeto=self.projeto
                )
            except MensagemRecusada as recusa:
                self.recusadas += 1
                self.avisar(f"mensagem recusada: {recusa}")
                continue
            self.ao_receber_prompt(campos["texto"], campos["pedido"])


def _fechar_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# --- Servidor MCP stdio ----------------------------------------------------


def capacidades() -> dict[str, Any]:
    """O que o servidor declara no initialize: o channel e uma ferramenta, nada mais."""
    return {"experimental": {CAPACIDADE_DO_CANAL: {}}, "tools": {}}


class ServidorDoCanal:
    """Fala JSON-RPC por linhas no stdin/stdout, como o transporte stdio do MCP."""

    def __init__(
        self,
        projeto: str,
        entrada: BinaryIO,
        saida: BinaryIO,
        enviar_resposta: Callable[[str, str, str], bool],
        avisar: Callable[[str], None] = _avisar,
    ):
        self.projeto = validar_nome_de_projeto(projeto)
        self.entrada = entrada
        self.saida = saida
        self.enviar_resposta = enviar_resposta
        self.avisar = avisar
        self._trinco = threading.Lock()
        self._pronto = False
        self._pendentes: list[tuple[str, str]] = []

    # -- saida

    def _escrever(self, obj: dict[str, Any]) -> None:
        dados = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._trinco:
            self.saida.write(dados)
            self.saida.flush()

    def _resultado(self, ident: Any, resultado: dict[str, Any]) -> None:
        self._escrever({"jsonrpc": "2.0", "id": ident, "result": resultado})

    def _erro(self, ident: Any, codigo: int, mensagem: str) -> None:
        self._escrever(
            {"jsonrpc": "2.0", "id": ident, "error": {"code": codigo, "message": mensagem}}
        )

    # -- prompts vindos do jarvis

    def empurrar_prompt(self, texto: str, pedido: str) -> None:
        """Entrega o prompt confirmado a sessao; antes do initialize fica em espera."""
        with self._trinco:
            if not self._pronto:
                self._pendentes.append((texto, pedido))
                return
        self._notificar(texto, pedido)

    def _notificar(self, texto: str, pedido: str) -> None:
        self._escrever(
            {
                "jsonrpc": "2.0",
                "method": METODO_DO_CANAL,
                "params": {
                    "content": texto,
                    "meta": {"origem": "voz", "projeto": self.projeto, "pedido": pedido},
                },
            }
        )

    def _ficar_pronto(self) -> None:
        with self._trinco:
            self._pronto = True
            pendentes, self._pendentes = self._pendentes, []
        for texto, pedido in pendentes:
            self._notificar(texto, pedido)

    # -- entrada

    def correr(self) -> None:
        for linha in iter(self.entrada.readline, b""):
            linha = linha.strip()
            if not linha:
                continue
            try:
                pedido = json.loads(linha.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self._erro(None, -32700, "parse error")
                continue
            self.tratar(pedido)

    def tratar(self, pedido: Any) -> None:
        if not isinstance(pedido, dict) or pedido.get("jsonrpc") != "2.0":
            self._erro(None, ERRO_PEDIDO_INVALIDO, "invalid request")
            return
        metodo = pedido.get("method")
        tem_id = "id" in pedido
        ident = pedido.get("id")
        if not isinstance(metodo, str):
            if tem_id and ("result" in pedido or "error" in pedido):
                return  # resposta a um pedido nosso: este servidor nao faz pedidos
            self._erro(ident, ERRO_PEDIDO_INVALIDO, "invalid request")
            return
        if not tem_id:
            if metodo == "notifications/initialized":
                self._ficar_pronto()
            return
        params = pedido.get("params") or {}
        if not isinstance(params, dict):
            self._erro(ident, ERRO_PARAMETROS_INVALIDOS, "params must be an object")
            return
        if metodo == "initialize":
            versao = params.get("protocolVersion")
            self._resultado(
                ident,
                {
                    "protocolVersion": versao if versao in VERSOES_MCP_ACEITES else VERSAO_MCP_PADRAO,
                    "capabilities": capacidades(),
                    "serverInfo": {"name": NOME_DO_SERVIDOR, "version": VERSAO_DO_SERVIDOR},
                    "instructions": INSTRUCOES,
                },
            )
        elif metodo == "ping":
            self._resultado(ident, {})
        elif metodo == "tools/list":
            self._resultado(ident, {"tools": [FERRAMENTA]})
        elif metodo == "tools/call":
            self._chamar_ferramenta(ident, params)
        else:
            self._erro(ident, ERRO_METODO_DESCONHECIDO, "method not found")

    def _chamar_ferramenta(self, ident: Any, params: dict[str, Any]) -> None:
        if params.get("name") != FERRAMENTA_DE_RESPOSTA:
            self._erro(ident, ERRO_PARAMETROS_INVALIDOS, "unknown tool")
            return
        argumentos = params.get("arguments") or {}
        texto = argumentos.get("texto") if isinstance(argumentos, dict) else None
        pedido = argumentos.get("pedido", "") if isinstance(argumentos, dict) else ""
        if not isinstance(texto, str) or not texto.strip():
            self._resultado(ident, _resultado_da_ferramenta("Falta o texto da resposta.", erro=True))
            return
        if not isinstance(pedido, str) or (pedido and not PADRAO_PEDIDO.match(pedido)):
            pedido = ""
        texto = texto.replace("\x00", "")[:MAXIMO_CARACTERES_DE_TEXTO]
        falado = resumo_falado(texto)
        if self.enviar_resposta(texto, falado, pedido):
            self._resultado(ident, _resultado_da_ferramenta("Entregue ao jarvis."))
        else:
            self._resultado(
                ident,
                _resultado_da_ferramenta(
                    "O jarvis nao esta ligado; a resposta nao foi lida.", erro=True
                ),
            )


def _resultado_da_ferramenta(texto: str, erro: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": texto}], "isError": erro}


def correr_servidor(projeto: str, caminho_endereco: Path = FICHEIRO_IPC) -> int:
    """Liga stdin/stdout ao servidor MCP e o IPC ao jarvis; corre ate o stdin fechar."""
    servidor: ServidorDoCanal | None = None

    def ao_receber(texto: str, pedido: str) -> None:
        if servidor is not None:
            servidor.empurrar_prompt(texto, pedido)

    ligacao = LigacaoAoJarvis(projeto, ao_receber, caminho_endereco)
    servidor = ServidorDoCanal(
        projeto, sys.stdin.buffer, sys.stdout.buffer, ligacao.enviar_resposta
    )
    ligacao.iniciar()
    try:
        servidor.correr()
    finally:
        ligacao.parar()
    return 0


# --- Autoteste com cliente MCP falso e jarvis falso ------------------------


class _JarvisFalso:
    """Ouve em 127.0.0.1 como o jarvis e guarda o que o canal lhe manda."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.segredo = gerar_segredo()
        self.servidor = socket.create_server((HOST_IPC, 0))
        self.porta = self.servidor.getsockname()[1]
        escrever_endereco(EnderecoIpc(self.porta, self.segredo, os.getpid()), caminho)
        self.conexao: socket.socket | None = None
        self.recebidas: list[dict[str, str]] = []
        self._aceite = threading.Event()
        threading.Thread(target=self._aceitar, daemon=True).start()

    def _aceitar(self) -> None:
        conexao, _ = self.servidor.accept()
        self.conexao = conexao
        self._aceite.set()
        with conexao.makefile("rb") as leitor:
            try:
                for linha in leitor:
                    try:
                        self.recebidas.append(
                            validar_mensagem(
                                linha, segredo=self.segredo, tipos={TIPO_OLA, TIPO_RESPOSTA}
                            )
                        )
                    except MensagemRecusada:
                        pass
            except OSError:
                pass

    def mandar(self, linha: bytes) -> None:
        assert self._aceite.wait(5) and self.conexao is not None
        self.conexao.sendall(linha)

    def fechar(self) -> None:
        for sock in (self.conexao, self.servidor):
            if sock is not None:
                _fechar_socket(sock)


def _esperar(condicao: Callable[[], bool], limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.02)
    return condicao()


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        ok = obtido == esperado
        print(f"[{'ok' if ok else 'FALHA'}] {nome}")
        if not ok:
            falhas.append(f"{nome}: obtido {obtido!r}, esperado {esperado!r}")

    projeto = "projeto-teste"
    with tempfile.TemporaryDirectory(prefix="jarvis-canal-") as pasta:
        caminho = Path(pasta) / "ipc.json"
        jarvis = _JarvisFalso(caminho)

        # cliente MCP falso: pipes em vez do stdin/stdout do processo
        leitura_servidor, escrita_cliente = os.pipe()
        leitura_cliente, escrita_servidor = os.pipe()
        entrada = os.fdopen(leitura_servidor, "rb", buffering=0)
        saida = os.fdopen(escrita_servidor, "wb")
        para_servidor = os.fdopen(escrita_cliente, "wb", buffering=0)
        do_servidor = os.fdopen(leitura_cliente, "rb")
        recebido: list[dict[str, Any]] = []

        def ler_cliente() -> None:
            for linha in do_servidor:
                recebido.append(json.loads(linha))

        leitor_do_cliente = threading.Thread(target=ler_cliente, daemon=True)
        leitor_do_cliente.start()

        servidor: ServidorDoCanal | None = None

        def ao_receber(texto: str, pedido: str) -> None:
            assert servidor is not None
            servidor.empurrar_prompt(texto, pedido)

        ligacao = LigacaoAoJarvis(projeto, ao_receber, caminho, intervalo_s=0.05)
        servidor = ServidorDoCanal(
            projeto, entrada, saida, ligacao.enviar_resposta, avisar=lambda _t: None
        )
        ligacao.avisar = lambda _t: None
        corrida = threading.Thread(target=servidor.correr, daemon=True)
        corrida.start()
        ligacao.iniciar()

        def pedir(obj: dict[str, Any]) -> None:
            para_servidor.write((json.dumps(obj) + "\n").encode("utf-8"))

        def resposta_a(ident: int) -> dict[str, Any]:
            _esperar(lambda: any(m.get("id") == ident for m in recebido))
            return next((m for m in recebido if m.get("id") == ident), {})

        def notificacoes() -> list[dict[str, Any]]:
            return [m for m in recebido if m.get("method") == METODO_DO_CANAL]

        pedir({"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}})
        verificar(
            "server/discover desconhecido (o cliente fica na revisao com notificacoes)",
            resposta_a(0).get("error", {}).get("code"),
            ERRO_METODO_DESCONHECIDO,
        )
        pedir(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        inicio = resposta_a(1).get("result", {})
        experimental = inicio.get("capabilities", {}).get("experimental", {})
        verificar("declara claude/channel", CAPACIDADE_DO_CANAL in experimental, True)
        verificar("nao declara claude/channel/permission", CAPACIDADE_PROIBIDA in experimental, False)
        verificar("versao do protocolo ecoada", inicio.get("protocolVersion"), "2025-06-18")

        verificar("o canal liga-se ao jarvis", _esperar(lambda: ligacao.ligado), True)
        verificar(
            "o jarvis recebe o ola com o projeto",
            _esperar(lambda: any(m["tipo"] == TIPO_OLA for m in jarvis.recebidas))
            and jarvis.recebidas[0]["projeto"],
            projeto,
        )

        pedido_cedo = novo_pedido()
        jarvis.mandar(
            linha_de_mensagem(
                TIPO_PROMPT, jarvis.segredo, projeto=projeto, pedido=pedido_cedo,
                texto="antes do initialized",
            )
        )
        time.sleep(0.2)
        verificar("prompt antes do initialized fica em espera", len(notificacoes()), 0)
        pedir({"jsonrpc": "2.0", "method": "notifications/initialized"})
        verificar("depois do initialized o prompt em espera sai", _esperar(lambda: len(notificacoes()) == 1), True)

        pedido = novo_pedido()
        prompt = "Resume o estado do projeto em duas frases.\nSem mexer em nada."
        jarvis.mandar(
            linha_de_mensagem(TIPO_PROMPT, jarvis.segredo, projeto=projeto, pedido=pedido, texto=prompt)
        )
        _esperar(lambda: len(notificacoes()) == 2)
        ultima = notificacoes()[-1]["params"] if len(notificacoes()) == 2 else {}
        verificar("o prompt confirmado chega a sessao tal e qual", ultima.get("content"), prompt)
        verificar(
            "meta com chaves validas para o Claude Code",
            ultima.get("meta"),
            {"origem": "voz", "projeto": projeto, "pedido": pedido},
        )

        # mensagens recusadas: nenhuma chega a sessao
        recusas_antes = ligacao.recusadas
        maus = [
            (json.dumps({"tipo": TIPO_PROMPT, "projeto": projeto, "pedido": pedido, "texto": "x"}) + "\n").encode(),
            linha_de_mensagem(TIPO_PROMPT, "0" * 64, projeto=projeto, pedido=pedido, texto="x"),
            linha_de_mensagem(TIPO_PROMPT, jarvis.segredo, projeto="outro", pedido=pedido, texto="x"),
            linha_de_mensagem(TIPO_PROMPT, jarvis.segredo, projeto=projeto, pedido=pedido + "\r\n", texto="x"),
            linha_de_mensagem(TIPO_RESPOSTA, jarvis.segredo, projeto=projeto, texto="x", falado="x"),
        ]
        for linha in maus:
            jarvis.mandar(linha)
        _esperar(lambda: ligacao.recusadas - recusas_antes == len(maus))
        verificar("5 mensagens mas recusadas", ligacao.recusadas - recusas_antes, len(maus))
        verificar("nenhuma mensagem ma chegou a sessao", len(notificacoes()), 2)

        pedir({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        nomes = [f.get("name") for f in resposta_a(2).get("result", {}).get("tools", [])]
        verificar("uma so ferramenta: responder", nomes, [FERRAMENTA_DE_RESPOSTA])

        tecnico = "Feito. Corri os testes.\n```python\nimport os\nos.remove('x')\n```\n"
        pedir(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": FERRAMENTA_DE_RESPOSTA, "arguments": {"texto": tecnico, "pedido": pedido}},
            }
        )
        verificar("responder devolve sucesso", resposta_a(3).get("result", {}).get("isError"), False)
        _esperar(lambda: any(m["tipo"] == TIPO_RESPOSTA for m in jarvis.recebidas))
        resposta = next((m for m in jarvis.recebidas if m["tipo"] == TIPO_RESPOSTA), {})
        verificar("o jarvis recebe o texto inteiro para o ecra", resposta.get("texto"), tecnico)
        verificar("o falado passou pelo filtro (sem codigo)", "os.remove" in resposta.get("falado", "x"), False)
        verificar("o falado mantem a frase natural", "Feito." in resposta.get("falado", ""), True)

        pedir({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "Bash", "arguments": {}}})
        verificar("outra ferramenta recusada", "error" in resposta_a(4), True)

        ligacao.parar()
        pedir({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
               "params": {"name": FERRAMENTA_DE_RESPOSTA, "arguments": {"texto": "ola"}}})
        verificar("sem jarvis ligado, responder devolve erro", resposta_a(5).get("result", {}).get("isError"), True)

        # stdin fechado: o servidor acaba; depois fecha-se o resto dos pipes
        para_servidor.close()
        corrida.join(timeout=5)
        saida.close()
        leitor_do_cliente.join(timeout=5)
        entrada.close()
        do_servidor.close()
        jarvis.fechar()

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do canal MCP completo (cliente MCP falso, IPC, recusas, filtro).")
    return 0


def main(argv: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="python -m jarvis.canal_mcp")
    analisador.add_argument("--projeto", help="nome do projeto desta sessao (do config.toml)")
    analisador.add_argument("--ipc", type=Path, default=FICHEIRO_IPC, help=argparse.SUPPRESS)
    analisador.add_argument("--autoteste", action="store_true")
    argumentos = analisador.parse_args(argv)
    if argumentos.autoteste:
        return _autoteste()
    if not argumentos.projeto:
        analisador.error("--projeto e obrigatorio")
    try:
        validar_nome_de_projeto(argumentos.projeto)
    except ValueError as erro:
        analisador.error(str(erro))
    return correr_servidor(argumentos.projeto, argumentos.ipc)


if __name__ == "__main__":
    sys.exit(main())
