r"""Frases faladas mais naturais: variantes das frases fixas e respostas curtas geradas.

Duas pecas:

  Variantes  cada frase fixa tem duas a quatro formas; `Variantes.escolher`
             diz a primeira da primeira vez e depois uma das outras ao
             acaso, nunca a mesma duas vezes seguidas para a mesma chave.
             Seguro entre threads. `VARIANTES` e o escolhedor partilhado dos
             modulos que falam por funcoes (estado, avisos, forja_voz).

  Persona    so para a conversa social curta ("how are you?") e para o "nao
             percebi": o LLM local (o mesmo modelo do interprete, no mesmo
             Ollama) escreve UMA frase curta a partir de uma persona de uma
             linha. O pedido vai em streaming e a primeira frase acabada e
             devolvida logo, sem esperar pelo resto. Se nao houver uma frase
             pronta dentro do prazo (cerca de 1.2 s), ou se o Ollama falhar,
             devolve None e quem chama diz a variante fixa.

O que o texto gerado NUNCA faz:

  * decidir uma accao: quem chama ja decidiu tudo antes; o texto so e dito;
  * dizer o projeto, o texto de um pedido ou o "diz sim / aborta" de um
    recap: o recap nunca passa por aqui, o pedido ao LLM nao leva nada disso
    e o filtro recusa nomes de projeto, palavras de enviar/confirmar/abortar
    e pedacos da frase ouvida;
  * falar de comprar, vender, investir ou dinheiro (filtro proprio mais a
    regra financeira do interprete);
  * passar ao lado do filtro da resposta falada (`texto_falavel`).

Um texto recusado e deitado fora e fica a variante fixa. So o Ollama local
(`127.0.0.1`) e contactado; nada sai do PC.
"""

from __future__ import annotations

import json
import queue
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Protocol, Sequence
from urllib.parse import urlsplit

# O interprete e o filtro da resposta falada so sao importados quando uma
# frase e gerada: os modulos que so usam as variantes (os avisos, que tambem
# correm como hook dentro do Claude Code) ficam leves.

# --- Variantes ----------------------------------------------------------------


def opcoes_da_frase(valor: str | Sequence[str]) -> tuple[str, ...]:
    """As formas de uma frase de uma tabela: um texto so ou um tuplo de variantes."""
    if isinstance(valor, str):
        return (valor,)
    opcoes = tuple(valor)
    if not opcoes or not all(isinstance(opcao, str) and opcao for opcao in opcoes):
        raise ValueError(f"frase sem variantes validas: {valor!r}")
    return opcoes


class Variantes:
    """Escolhe uma forma de cada frase, nunca a mesma que da ultima vez (por chave)."""

    def __init__(self, aleatorio: random.Random | None = None) -> None:
        self._aleatorio = aleatorio or random.Random()
        self._ultima: dict[str, str] = {}
        self._tranca = threading.Lock()

    def escolher(self, chave: str, valor: str | Sequence[str]) -> str:
        """A primeira forma da primeira vez; depois uma das outras, ao acaso."""
        opcoes = opcoes_da_frase(valor)
        with self._tranca:
            anterior = self._ultima.get(chave)
            if anterior is None or anterior not in opcoes:
                escolhida = opcoes[0]
            else:
                escolhida = self._aleatorio.choice([o for o in opcoes if o != anterior] or list(opcoes))
            self._ultima[chave] = escolhida
        return escolhida

    def esquecer(self) -> None:
        """Volta ao inicio: a proxima frase de cada chave e a primeira forma."""
        with self._tranca:
            self._ultima.clear()


#: O escolhedor dos modulos que falam por funcoes (estado, avisos, forja_voz).
VARIANTES = Variantes()


# --- Persona ------------------------------------------------------------------

#: Sem uma frase pronta ao fim disto, diz-se a variante fixa.
PRAZO_DA_PERSONA_S = 1.2
#: Tokens que o modelo pode gerar: chega para uma frase curta.
TOKENS_DA_PERSONA = 48
#: Uma frase gerada mais comprida do que isto e recusada.
MAXIMO_DE_CARACTERES = 140
#: A frase ouvida vai no pedido cortada a isto (so o necessario para responder).
MAXIMO_DA_FRASE_OUVIDA = 200
#: Bytes lidos do stream, no maximo (uma frase curta cabe muitas vezes).
MAXIMO_DO_STREAM_BYTES = 64 * 1024

#: Casos em que o texto pode ser gerado. Tudo o resto fica nas frases fixas.
CASO_SOCIAL = "social"
CASO_NAO_PERCEBI = "nao_percebi"
CASOS = frozenset({CASO_SOCIAL, CASO_NAO_PERCEBI})

_PERSONA = {
    "en": (
        "You are Jarvis, a calm, warm voice assistant with a light British manner. "
        "Answer in English with exactly one short spoken sentence of at most 14 words, using contractions. "
        "No lists, no markdown, no emojis, no quotes. "
        "Never claim to have done, sent, started, stopped, saved or cancelled anything, "
        "never name a project, never ask the user to confirm, say yes or abort, "
        "and never talk about money, buying, selling, trading or investing."
    ),
    "pt": (
        "Es o Jarvis, um assistente de voz calmo e simpatico. "
        "Responde em portugues de Portugal com uma so frase curta falada, no maximo 14 palavras. "
        "Sem listas, sem markdown, sem emojis, sem aspas. "
        "Nunca digas que fizeste, enviaste, lancaste, paraste, guardaste ou cancelaste alguma coisa, "
        "nunca digas o nome de um projeto, nunca peças para confirmar, dizer sim ou abortar, "
        "e nunca fales de dinheiro, comprar, vender, bolsa ou investir."
    ),
}

_PEDIDOS = {
    "en": {
        CASO_SOCIAL: 'The user just said: "{frase}". Reply to them briefly and warmly.',
        CASO_NAO_PERCEBI: (
            'You could not make sense of what the user said: "{frase}". '
            "Tell them briefly and kindly, and ask them to say it again."
        ),
    },
    "pt": {
        CASO_SOCIAL: 'O utilizador acabou de dizer: "{frase}". Responde-lhe de forma curta e simpatica.',
        CASO_NAO_PERCEBI: (
            'Nao percebeste o que o utilizador disse: "{frase}". '
            "Diz-lho de forma curta e simpatica e pede-lhe para repetir."
        ),
    },
}

#: Palavras que uma frase gerada nunca diz: accoes, confirmacao e dinheiro.
_PALAVRAS_PROIBIDAS = re.compile(
    r"\b(?:"
    # enviar, confirmar, abortar e o que so uma accao faz
    r"send|sends|sent|sending|abort|aborts|aborted|cancel|cancels|cancell?ed|cancell?ing|"
    r"confirm|confirms|confirmed|confirming|launch\w*|resum\w*|deleted|saved|"
    r"say\s+yes|yes\s+or\s+no|"
    # dinheiro e bolsa
    r"buy\w*|bought|sell\w*|sold|trad(?:e|es|ed|ing)|invest\w*|stocks?|shares?|crypto\w*|bitcoin|"
    r"money|dollars?|euros?|pounds?|price|prices|portfolio|"
    # o mesmo em portugues
    r"envi\w*|abort\w*|cancel\w*|confirm\w*|compr\w*|vend\w*|bolsa|dinheiro|acc?(?:ao|ão|oes|ões)|invest\w*"
    r")\b",
    re.IGNORECASE,
)
_ACENTOS = re.compile(r"[à-öø-ÿÀ-ÖØ-ß]")
_FIM_DE_FRASE = re.compile(r"[.!?]+[\"'”’)]*(?=\s|$)")
_PALAVRA = re.compile(r"[^\W_]+(?:'[^\W_]+)?")


def _lingua(lingua: str | None) -> str:
    return "en" if lingua == "en" else "pt"


def _numa_linha(texto: str, maximo: int) -> str:
    """Texto externo numa so linha, sem aspas nem controlo, cortado a `maximo`."""
    limpo = "".join(c if c.isprintable() else " " for c in (texto or ""))
    limpo = limpo.replace('"', "'")
    return " ".join(limpo.split())[:maximo]


def mensagens_da_persona(caso: str, frase: str | None, lingua: str) -> list[dict[str, str]]:
    """A persona e o pedido do caso; a frase ouvida vai so como dados, numa linha."""
    if caso not in CASOS:
        raise ValueError(f"caso da persona desconhecido: {caso!r}")
    lingua = _lingua(lingua)
    return [
        {"role": "system", "content": _PERSONA[lingua]},
        {"role": "user", "content": _PEDIDOS[lingua][caso].format(frase=_numa_linha(frase or "", MAXIMO_DA_FRASE_OUVIDA))},
    ]


def primeira_frase(texto: str, *, acabado: bool = False) -> str | None:
    """A primeira frase acabada do texto; com `acabado`, o texto todo conta como uma."""
    encontrado = _FIM_DE_FRASE.search(texto)
    if encontrado is not None:
        return texto[: encontrado.end()].strip() or None
    if acabado:
        return texto.strip() or None
    return None


def _ngramas(palavras: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(palavras[i : i + n]) for i in range(len(palavras) - n + 1)}


def motivo_da_recusa(
    texto: str | None,
    *,
    lingua: str,
    projetos: Iterable[str] = (),
    frase_ouvida: str | None = None,
) -> str | None:
    """Porque e que uma frase gerada nao pode ser dita, ou None se pode.

    Recusa o que o filtro da resposta falada tira, o que e comprido demais,
    nomes de projeto, palavras de enviar/confirmar/abortar, dinheiro, acentos
    com lingua=en e quatro palavras seguidas copiadas da frase ouvida (um
    pedido ditado nunca volta a ser dito por aqui).
    """
    from jarvis.interprete import pedido_financeiro, projetos_mencionados
    from jarvis.resposta_falada import texto_falavel, texto_proibido

    bruto = " ".join((texto or "").split())
    if not bruto:
        return "vazia"
    if len(bruto) > MAXIMO_DE_CARACTERES:
        return "comprida demais"
    if texto_proibido(bruto) or texto_falavel(bruto) != bruto:
        return "nao passa o filtro da resposta falada"
    if _lingua(lingua) == "en" and _ACENTOS.search(bruto):
        return "nao esta em ingles"
    nomes = tuple(nome for nome in projetos if nome)
    if nomes and projetos_mencionados(bruto, nomes):
        return "diz um projeto"
    if _PALAVRAS_PROIBIDAS.search(bruto):
        return "fala de uma accao, da confirmacao ou de dinheiro"
    if pedido_financeiro(bruto, nomes) is not None:
        return "fala de dinheiro"
    ouvidas = [palavra.lower() for palavra in _PALAVRA.findall(frase_ouvida or "")]
    ditas = [palavra.lower() for palavra in _PALAVRA.findall(bruto)]
    if len(ouvidas) >= 4 and _ngramas(ouvidas, 4) & _ngramas(ditas, 4):
        return "repete a frase ouvida"
    return None


class ClienteDeFluxo(Protocol):
    """Quem fala com o Ollama: pedaços do conteudo da resposta, a medida que chegam."""

    def conversar_em_fluxo(
        self, modelo: str, mensagens: list[dict[str, str]], *, parar: threading.Event, limite_s: float
    ) -> Iterator[str]: ...


class ClienteOllamaEmFluxo:
    """`/api/chat` com `stream: true` no Ollama local, so com a biblioteca padrao.

    As opcoes de contexto e `keep_alive` sao as do interprete: o Ollama nao
    recarrega o modelo por causa de um contexto diferente. `parar` fecha a
    ligacao no pedaco seguinte (quem chama ja desistiu).
    """

    def __init__(self, url: str) -> None:
        from jarvis.config import validar_url_local

        self.url = validar_url_local(url)
        partes = urlsplit(self.url)
        self._host = partes.hostname or "127.0.0.1"
        self._porta = partes.port or 11434

    def conversar_em_fluxo(
        self, modelo: str, mensagens: list[dict[str, str]], *, parar: threading.Event, limite_s: float
    ) -> Iterator[str]:
        import http.client

        from jarvis.interprete import CONTEXTO_DO_LLM, MANTER_CARREGADO

        corpo = {
            "model": modelo,
            "messages": mensagens,
            "stream": True,
            "think": False,
            "keep_alive": MANTER_CARREGADO,
            "options": {
                "temperature": 0.8,
                "num_ctx": CONTEXTO_DO_LLM,
                "num_predict": TOKENS_DA_PERSONA,
            },
        }
        conexao = http.client.HTTPConnection(self._host, self._porta, timeout=limite_s)
        try:
            conexao.request(
                "POST", "/api/chat", body=json.dumps(corpo).encode("utf-8"), headers={"Content-Type": "application/json"}
            )
            resposta = conexao.getresponse()
            if resposta.status != 200:
                raise ConnectionError(f"o Ollama respondeu HTTP {resposta.status}")
            lidos = 0
            while not parar.is_set():
                linha = resposta.readline(MAXIMO_DO_STREAM_BYTES)
                if not linha:
                    return
                lidos += len(linha)
                if lidos > MAXIMO_DO_STREAM_BYTES:
                    raise ValueError("o stream do Ollama e grande demais")
                dados = json.loads(linha.decode("utf-8"))
                if not isinstance(dados, dict):
                    raise ValueError("pedaco do stream que nao e um objeto JSON")
                mensagem = dados.get("message")
                if isinstance(mensagem, dict) and isinstance(mensagem.get("content"), str) and mensagem["content"]:
                    yield mensagem["content"]
                if dados.get("done"):
                    return
        finally:
            conexao.close()


@dataclass(frozen=True)
class FraseGerada:
    """O resultado de um pedido a persona: o texto (ou None) e porque."""

    texto: str | None
    motivo: str
    demorou_s: float


_FIM = object()


class Persona:
    """Uma frase curta gerada pelo LLM local, ou None para quem chama dizer a fixa.

    `modelo` e uma funcao para o modelo acompanhar o do interprete (que pode
    mudar para o alternativo). `projetos` da os nomes a nunca dizer.
    `registar` recebe uma linha por pedido (para o log); nunca a frase ouvida.
    """

    def __init__(
        self,
        cliente: ClienteDeFluxo,
        modelo: Callable[[], str | None],
        lingua: str,
        *,
        projetos: Callable[[], Iterable[str]] = tuple,
        prazo_s: float = PRAZO_DA_PERSONA_S,
        relogio: Callable[[], float] = time.perf_counter,
        registar: Callable[[str], object] | None = None,
    ) -> None:
        if isinstance(prazo_s, bool) or not isinstance(prazo_s, (int, float)) or prazo_s <= 0:
            raise ValueError(f"prazo da persona invalido: {prazo_s!r}")
        self.cliente = cliente
        self._modelo = modelo
        self.lingua = _lingua(lingua)
        self._projetos = projetos
        self.prazo_s = float(prazo_s)
        self._relogio = relogio
        self._registar = registar

    def _nota(self, texto: str) -> None:
        if self._registar is not None:
            try:
                self._registar(texto)
            except Exception:  # noqa: BLE001 - o log nunca impede a frase
                pass

    def gerar(self, caso: str, frase_ouvida: str | None = None) -> FraseGerada:
        """Uma frase para `caso`, se ficar pronta a tempo e passar o filtro."""
        inicio = self._relogio()
        resultado = self._gerar(caso, frase_ouvida, inicio)
        self._nota(
            f"persona | {caso}: "
            + (f"gerada em {resultado.demorou_s * 1000:.0f} ms" if resultado.texto else f"frase fixa ({resultado.motivo})")
        )
        return resultado

    def frase(self, caso: str, frase_ouvida: str | None = None) -> str | None:
        """So o texto de `gerar` (None quando fica a frase fixa)."""
        return self.gerar(caso, frase_ouvida).texto

    def _gerar(self, caso: str, frase_ouvida: str | None, inicio: float) -> FraseGerada:
        def fim(texto: str | None, motivo: str) -> FraseGerada:
            return FraseGerada(texto, motivo, self._relogio() - inicio)

        if caso not in CASOS:
            return fim(None, f"caso {caso!r} nao e gerado")
        try:
            modelo = self._modelo()
            projetos = tuple(self._projetos())
        except Exception as erro:  # noqa: BLE001 - sem modelo fica a frase fixa
            return fim(None, f"sem modelo ({type(erro).__name__})")
        if not modelo:
            return fim(None, "sem modelo")
        mensagens = mensagens_da_persona(caso, frase_ouvida, self.lingua)
        pedacos: queue.Queue = queue.Queue()
        parar = threading.Event()

        def pedir() -> None:
            try:
                for pedaco in self.cliente.conversar_em_fluxo(
                    modelo, mensagens, parar=parar, limite_s=max(self.prazo_s, 5.0)
                ):
                    pedacos.put(pedaco)
                    if parar.is_set():
                        return
                pedacos.put(_FIM)
            except BaseException as erro:  # noqa: BLE001 - vai para a thread da frase
                pedacos.put(erro)

        threading.Thread(target=pedir, name="jarvis-persona", daemon=True).start()
        texto = ""
        try:
            while True:
                restante = self.prazo_s - (self._relogio() - inicio)
                if restante <= 0:
                    return fim(None, f"sem frase em {self.prazo_s:g} s")
                try:
                    pedaco = pedacos.get(timeout=restante)
                except queue.Empty:
                    return fim(None, f"sem frase em {self.prazo_s:g} s")
                if isinstance(pedaco, BaseException):
                    return fim(None, f"o LLM falhou ({type(pedaco).__name__})")
                acabado = pedaco is _FIM
                if not acabado:
                    texto += pedaco
                    if len(texto) > MAXIMO_DE_CARACTERES * 3:
                        return fim(None, "comprida demais")
                dita = primeira_frase(texto, acabado=acabado)
                if dita is None:
                    if acabado:
                        return fim(None, "vazia")
                    continue
                motivo = motivo_da_recusa(dita, lingua=self.lingua, projetos=projetos, frase_ouvida=frase_ouvida)
                if motivo is not None:
                    return fim(None, f"recusada: {motivo}")
                return fim(dita, "gerada")
        finally:
            # A resposta ja chegou ou ja nao conta: o resto nunca e dito.
            parar.set()
