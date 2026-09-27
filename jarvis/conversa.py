"""Conversa maos-livres com o Claude: quando a resposta acaba em pergunta, o jarvis ouve.

Uma resposta do Claude de um projeto (pelo canal ou pelo caminho headless) que
faz uma pergunta, e cuja pergunta foi dita em voz alta, abre uma JANELA DE
ESCUTA de 8 s. Basta uma pergunta em qualquer frase: o Claude pergunta muitas
vezes primeiro e explica depois ("Which two files do you mean? ... I won't
touch either file until you answer."). Um falso positivo custa so uma janela
de escuta, porque nada se envia sem o "sim" do recap.

  - o ouvido escuta sem palavra de ativacao (o VAD decide o fim da fala) e a
    tecla de falar continua a funcionar;
  - a frase ouvida e a resposta a pergunta, sem reescrita pelo LLM: so perde
    as hesitacoes ("uh", "um") e o endereco ao jarvis ("Jarvis, answer ...");
  - uma resposta curta (ate 5 palavras: "Yes.", "the first one") vai logo e o
    jarvis diz "Sent."; uma mais longa vai para a confirmacao rapida
    ("Responder ao Claude no <projeto>: <texto> - envio?") e so um "sim" a
    envia. Fora desta janela nada se envia sem recap;
  - "sai da conversa" (ou "exit the conversation") fecha a janela sem enviar
    nada, e 8 s sem ninguem comecar a falar tambem;
  - calar, dormir e acordar passam a frente e fecham a janela.

A regra financeira vem sempre antes: um pedido de compra ou venda e recusado,
curto ou longo.

Uma pergunta so abre a janela se o utilizador a ouviu: se o filtro da resposta
falada a cortou, a janela nao abre (o texto inteiro fica no ecra).

Uma resposta a uma PERGUNTA GERAL que acaba numa pergunta ouvida ("... Do you
want the forecast for tomorrow too?") faz o mesmo na janela de seguimento: a
frase seguinte continua a pergunta geral, com a memoria da conversa recente,
sem passar pelas intencoes de projeto (`pergunta_de_seguimento`). "Yes." e
uma resposta nessa janela, nao cortesia solta.

Depois de qualquer resposta falada, o jarvis continua a ouvir sem palavra de
ativacao (o modo de conversa, em `jarvis.app`). Daqui vem o que ele precisa:
"that's all" / "thanks, that's it" fecham essa escuta
(`e_para_fechar_a_escuta`), e uma pergunta geral ouvida nela so segue se for
dita como pergunta ao jarvis (`pergunta_dirigida`), para a conversa a volta
nunca ir ao Claude.

Este modulo so tem as regras (sem threads nem audio); `jarvis.app` liga-as ao
ouvido, a confirmacao e ao canal.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from jarvis.interprete import (
    INTENCAO_PERGUNTA_GERAL,
    INTENCAO_RECUSADA,
    Interpretacao,
    limpar_texto,
    pedido_financeiro,
    sem_palavra_de_ativacao,
)
from jarvis.resposta_falada import tem_pergunta
from jarvis.router import _normalizar

#: Quanto tempo a janela espera que o utilizador comece a responder.
JANELA_S = 8.0

#: Fim de uma pergunta: "?" seguido so de aspas, parenteses ou enfase.
_FIM_EM_PERGUNTA = re.compile(r"[?¿]\s*[\"'”’»)\]*_]*\s*\Z")

#: Uma resposta com ate este numero de palavras (sem hesitacoes nem o endereco
#: ao jarvis) vai logo, sem recap. So dentro da janela de conversa.
PALAVRAS_DA_RESPOSTA_CURTA = 5

#: Hesitacoes que nunca chegam ao texto enviado. Sem "ha" ("há" em portugues).
_HESITACOES = frozenset(
    {"uh", "uhh", "uhm", "um", "umm", "hum", "hmm", "hm", "mm", "eh", "ehm", "er", "erm", "ah", "ahm", "hã", "hãn"}
)
#: Em portugues "um" e o artigo ("o um", "muda um teste").
_HESITACOES_QUE_SAO_PALAVRAS = {"pt": frozenset({"um"})}

#: O que o STT pode ouvir antes de "jarvis" num endereco ("Uh ans uh A Jarvis
#: answer no"). Lista fechada: "the jarvis one" nunca perde o "the jarvis".
_ANTES_DO_JARVIS = frozenset(
    {"a", "ans", "and", "e", "hey", "hei", "ei", "boas", "ola", "oh", "o", "ok", "okay", "so"}
)
_MAXIMO_ANTES_DO_JARVIS = 2
#: Depois de "jarvis": o verbo do endereco e o que o liga a resposta.
_VERBOS_DO_ENDERECO = frozenset(
    {"answer", "reply", "respond", "say", "tell", "responde", "responder", "diz", "diga", "dizer"}
)
_DESTINO_DO_ENDERECO = frozenset({"to", "ao", "claude", "him", "it", "lhe", "that", "que"})
_TERMINAIS = ".!?…"

#: A frase inteira, normalizada, tem de ser uma destas para sair.
FRASES_DE_SAIR = frozenset(
    {
        "sai da conversa",
        "sair da conversa",
        "sai desta conversa",
        "fim da conversa",
        "acaba a conversa",
        "termina a conversa",
        "exit conversation",
        "exit the conversation",
        "leave the conversation",
        "leave conversation",
        "end the conversation",
        "end conversation",
        "stop the conversation",
        "stop conversation",
    }
)


#: Fecham a escuta sem palavra de ativacao depois de o jarvis falar (o modo
#: de conversa): a frase inteira, ja normalizada, sem a cortesia a volta.
FRASES_DE_FECHAR_A_ESCUTA = frozenset(
    {
        "that s all",
        "thats all",
        "that is all",
        "that s it",
        "thats it",
        "that is it",
        "that ll be all",
        "that will be all",
        "that ll do",
        "that will do",
        "nothing else",
        "that s everything",
        "stop listening",
        "e tudo",
        "e so",
        "e so isso",
        "mais nada",
        "nada mais",
        "para de ouvir",
        *FRASES_DE_SAIR,
    }
)
#: Palavras soltas que podem estar a volta da frase de fecho ("Thanks, that's
#: it.", "No, that's all for now, jarvis.", "Obrigado, e tudo.").
_A_VOLTA_DO_FECHO = frozenset(
    {
        "thanks", "thank", "you", "no", "nope", "ok", "okay", "alright", "right", "cheers", "great",
        "jarvis", "hey", "for", "now", "please",
        "obrigado", "obrigada", "nao", "pronto", "entao", "por", "agora", "boas",
    }
)

#: Abertura de uma pergunta ou de um pedido de informacao, no inicio da frase
#: ja normalizada (sem hesitacoes nem o endereco ao jarvis).
_ABERTURA_DIRIGIDA = re.compile(
    r"(?:what|whats|who|whos|whose|where|wheres|when|which|why|how|hows"
    r"|is|are|was|were|do|does|did|can|could|will|would|should|has|have"
    r"|tell\s+me|explain|give\s+me|remind\s+me|look\s+up|search|find\s+out"
    r"|qual|quais|quem|onde|quando|quanto|quantos|quantas|que|o\s+que|como|porque|sera|e\s+verdade"
    r"|diz\s+me|explica|sabes|procura)\b"
)
#: Palavras soltas antes da abertura ("So, what's ...", "Olha, quem ...").
_ANTES_DA_ABERTURA = frozenset({"so", "and", "well", "ok", "okay", "hey", "olha", "entao", "e", "now", "also"})


def e_para_fechar_a_escuta(texto: str | None, lingua: str = "pt") -> bool:
    """"that's all", "thanks, that's it" e equivalentes, ditos como frase inteira.

    Hesitacoes, a palavra de ativacao e a cortesia a volta nao contam; tudo o
    resto conta: "that's all wrong" ou "that's it, run the tests" nao fecham.
    """
    normalizado = _normalizar(sem_palavra_de_ativacao(limpar_texto(texto or "")))
    palavras = [palavra for palavra in normalizado.split() if not _e_hesitacao(palavra, lingua)]
    inicio, fim = 0, len(palavras)
    while inicio < fim and palavras[inicio] in _A_VOLTA_DO_FECHO:
        inicio += 1
    while fim > inicio and palavras[fim - 1] in _A_VOLTA_DO_FECHO:
        fim -= 1
    return " ".join(palavras[inicio:fim]) in FRASES_DE_FECHAR_A_ESCUTA


def dirigida_ao_jarvis(texto: str | None, lingua: str = "pt") -> bool:
    """A frase comeca por chamar o jarvis ("Jarvis, ...", "Uh, hey jarvis ...")."""
    formas = [_normalizar(palavra) for palavra in limpar_texto(texto or "").split()]
    formas = [forma for forma in formas if forma and not _e_hesitacao(forma, lingua)]
    for posicao, forma in enumerate(formas[: _MAXIMO_ANTES_DO_JARVIS + 1]):
        if forma == "jarvis":
            return posicao < len(formas) - 1
        if forma not in _ANTES_DO_JARVIS:
            return False
    return False


def pergunta_dirigida(texto: str | None, lingua: str = "pt") -> bool:
    """A frase e dita como uma pergunta (ou um pedido de informacao) ao jarvis.

    Serve para a fala ouvida sem palavra de ativacao: uma conversa a volta
    ("I think we should leave at six") pode ser lida pelo interprete como uma
    pergunta geral, mas so vai ao Claude quando chama o jarvis, acaba em "?"
    ou abre como uma pergunta ("what", "how", "tell me", "qual", "diz-me").
    """
    if dirigida_ao_jarvis(texto, lingua):
        return True
    limpa = limpar_resposta(texto, lingua)
    if not limpa:
        return False
    if limpa.rstrip(" \"'”’»)]*_").endswith(("?", "¿")):
        return True
    palavras = _normalizar(limpa).split()
    while palavras and palavras[0] in _ANTES_DA_ABERTURA:
        palavras = palavras[1:]
    return bool(palavras) and _ABERTURA_DIRIGIDA.match(" ".join(palavras)) is not None


def acaba_em_pergunta(texto: str | None) -> bool:
    return bool(texto) and bool(_FIM_EM_PERGUNTA.search(texto.strip()))


def pede_resposta(texto: str | None, falado: str | None) -> bool:
    """A resposta do Claude faz uma pergunta e o utilizador ouviu-a.

    `falado` e o que a voz disse (ja filtrado a partir de `texto`): e nele que a
    pergunta tem de estar, porque uma pergunta que o filtro cortou nao foi ouvida.
    """
    return "?" in (texto or "") and tem_pergunta(falado)


def e_para_sair(texto: str | None, lingua: str = "pt") -> bool:
    """"sai da conversa" e equivalentes, ditos como frase inteira.

    Tambem com hesitacoes ou o endereco ao jarvis a volta ("Uh, exit the
    conversation.", "A Jarvis, exit the conversation."): o texto que se
    enviaria (`limpar_resposta`) tambem e testado, para que sair nunca va
    ao Claude como resposta curta.
    """
    for candidato in (texto or "", limpar_resposta(texto, lingua)):
        normalizado = " ".join(_normalizar(sem_palavra_de_ativacao(limpar_texto(candidato))).split())
        for cortesia in (" por favor", " please"):
            if normalizado.endswith(cortesia):
                normalizado = normalizado[: -len(cortesia)]
        if normalizado in FRASES_DE_SAIR:
            return True
    return False


def _forma(palavra: str) -> str:
    return re.sub(r"^\W+|\W+$", "", palavra.lower())


def _e_hesitacao(palavra: str, lingua: str) -> bool:
    forma = _forma(palavra)
    return forma in _HESITACOES and forma not in _HESITACOES_QUE_SAO_PALAVRAS.get(lingua, frozenset())


def _sem_endereco(palavras: list[str]) -> list[str]:
    """Tira "<ate 2 palavras soltas> jarvis [answer] [to claude] [that]" do inicio."""
    formas = [_normalizar(palavra) for palavra in palavras]
    if "jarvis" not in formas[: _MAXIMO_ANTES_DO_JARVIS + 1]:
        return palavras
    posicao = formas.index("jarvis")
    if any(forma not in _ANTES_DO_JARVIS for forma in formas[:posicao]):
        return palavras
    resto = posicao + 1
    if resto < len(formas) - 1 and formas[resto] in _VERBOS_DO_ENDERECO:
        resto += 1
        while resto < len(formas) - 1 and formas[resto] in _DESTINO_DO_ENDERECO:
            resto += 1
    return palavras[resto:] if resto < len(palavras) else palavras


def limpar_resposta(texto: str | None, lingua: str = "pt") -> str:
    """A resposta a enviar ao Claude: o texto ouvido sem hesitacoes nem o endereco ao jarvis.

    Deterministico e sem LLM: as palavras que ficam sao as ditas, pela mesma
    ordem. "Uh ans uh A Jarvis answer no, uh not right now." da "No, not right
    now.". Devolve "" se so havia hesitacoes.
    """
    literal = limpar_texto(texto or "")
    ditas = literal.split()
    palavras = [palavra for palavra in ditas if not _e_hesitacao(palavra, lingua)]
    ficam = _sem_endereco(palavras)
    resposta = re.sub(r"^\W+", "", " ".join(ficam)).rstrip(" ,;:-")
    if not re.search(r"\w", resposta):
        return ""
    if literal[-1] in _TERMINAIS and resposta[-1] not in _TERMINAIS:
        resposta += literal[-1]
    if ficam[0] is not ditas[0]:
        # O inicio ouvido saiu: a resposta comeca agora por maiuscula.
        resposta = resposta[0].upper() + resposta[1:]
    return resposta


def numero_de_palavras(texto: str) -> int:
    return sum(1 for palavra in (texto or "").split() if re.search(r"\w", palavra))


def e_resposta_curta(texto: str | None) -> bool:
    """Ate `PALAVRAS_DA_RESPOSTA_CURTA` palavras (o texto ja limpo por `limpar_resposta`)."""
    return 0 < numero_de_palavras(texto or "") <= PALAVRAS_DA_RESPOSTA_CURTA


def resposta_literal(
    texto: str, projeto: str, nomes_de_projeto: tuple[str, ...] = (), *, lingua: str = "pt"
) -> Interpretacao:
    """A resposta dita na janela como pedido de conversa para o projeto.

    Nunca passa pelo LLM: so perde as hesitacoes e o endereco ao jarvis
    (`limpar_resposta`), e o recap mostra e diz exatamente o que se envia. A
    regra financeira aplica-se antes, ao texto ouvido inteiro: um pedido de
    compra ou venda e recusado.
    """
    literal = limpar_resposta(texto, lingua)
    termo = pedido_financeiro(limpar_texto(texto), nomes_de_projeto) or pedido_financeiro(literal, nomes_de_projeto)
    if termo is not None:
        return Interpretacao(
            texto=limpar_texto(texto),
            intencao=INTENCAO_RECUSADA,
            projeto=projeto,
            prompt="",
            origem="regra",
            motivo=f"pedido financeiro na conversa ('{termo}')",
            termo_financeiro=termo,
        )
    return Interpretacao(
        texto=limpar_texto(texto),
        intencao="conversa",
        projeto=projeto,
        prompt=literal,
        origem="regra",
        motivo="resposta na janela de conversa (texto ouvido sem hesitacoes, sem reescrita)",
    )


#: Palavras que respondem a uma pergunta de sim ou nao. Sao cortesia fora de
#: uma conversa ("Yeah." solto nao pede nada), mas depois de uma pergunta sao
#: a resposta.
_PALAVRAS_DE_RESPOSTA = frozenset({"yes", "yeah", "yep", "yup", "sim"})


def responde_a_pergunta(texto: str | None) -> bool:
    """A frase tem um "yes"/"sim": depois de uma pergunta nunca e so cortesia."""
    return any(palavra in _PALAVRAS_DE_RESPOSTA for palavra in _normalizar(texto or "").split())


def pergunta_de_seguimento(
    texto: str, nomes_de_projeto: tuple[str, ...] = (), *, lingua: str = "pt"
) -> Interpretacao:
    """A frase dita depois de uma resposta geral que acabou numa pergunta.

    Continua a pergunta geral: nunca passa pelo LLM nem pelas intencoes de
    projeto, e o texto enviado e o ouvido sem hesitacoes nem o endereco ao
    jarvis (`limpar_resposta`). A memoria da conversa recente vai com ela
    (quem a faz e `jarvis.app`). A regra financeira vem antes, ao texto
    ouvido inteiro e ao que se envia: um pedido de compra ou venda e recusado
    e nada sai do PC.
    """
    ouvido = limpar_texto(texto or "")
    literal = limpar_resposta(texto, lingua)
    termo = pedido_financeiro(ouvido, nomes_de_projeto) or pedido_financeiro(literal, nomes_de_projeto)
    if termo is not None:
        return Interpretacao(
            texto=ouvido,
            intencao=INTENCAO_RECUSADA,
            projeto=None,
            prompt="",
            origem="regra",
            motivo=f"pedido financeiro na continuacao da pergunta geral ('{termo}')",
            termo_financeiro=termo,
        )
    return Interpretacao(
        texto=ouvido,
        intencao=INTENCAO_PERGUNTA_GERAL,
        projeto=None,
        prompt=literal,
        origem="regra",
        motivo="continuacao da pergunta geral (texto ouvido sem hesitacoes, sem reescrita)",
    )


@dataclass(frozen=True)
class EstadoDaJanela:
    projeto: str
    aberta_em: float
    prazo: float


class JanelaDeConversa:
    """A janela de escuta de uma conversa: um projeto, um prazo. Segura entre threads.

    O relogio tem de ser o dos instantes das frases (`time.perf_counter` no
    ouvido). A janela aceita uma frase que COMECOU dentro dela; o prazo so
    fecha a janela quando ninguem esta a meio de falar (`fechar_se_expirou`).
    """

    def __init__(self, *, limite_s: float = JANELA_S, relogio: Callable[[], float] = time.perf_counter) -> None:
        if limite_s <= 0:
            raise ValueError("a janela de conversa precisa de um prazo positivo")
        self.limite_s = float(limite_s)
        self._relogio = relogio
        self._trinco = threading.Lock()
        self._atual: EstadoDaJanela | None = None

    @property
    def atual(self) -> EstadoDaJanela | None:
        with self._trinco:
            return self._atual

    @property
    def projeto(self) -> str | None:
        atual = self.atual
        return atual.projeto if atual is not None else None

    def aberta(self) -> bool:
        return self.atual is not None

    def abrir(self, projeto: str) -> EstadoDaJanela:
        agora = self._relogio()
        estado = EstadoDaJanela(projeto, agora, agora + self.limite_s)
        with self._trinco:
            self._atual = estado
        return estado

    def aceita(self, inicio_da_fala: float) -> bool:
        """A frase que comecou em `inicio_da_fala` e a resposta desta janela."""
        atual = self.atual
        return atual is not None and atual.aberta_em <= inicio_da_fala <= atual.prazo

    def fechar(self) -> EstadoDaJanela | None:
        """Fecha a janela. Devolve a que estava aberta (None se nao havia)."""
        with self._trinco:
            atual, self._atual = self._atual, None
        return atual

    def fechar_se_expirou(self, alguem_a_falar: bool = False) -> EstadoDaJanela | None:
        """Fecha a janela se o prazo passou e ninguem esta a meio de uma frase."""
        with self._trinco:
            atual = self._atual
            if atual is None or alguem_a_falar or self._relogio() < atual.prazo:
                return None
            self._atual = None
        return atual
