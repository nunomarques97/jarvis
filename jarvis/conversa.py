"""Conversa maos-livres com o Claude: quando a resposta acaba em pergunta, o jarvis ouve.

Uma resposta do Claude de um projeto (pelo canal ou pelo caminho headless) que
faz uma pergunta, e cuja pergunta foi dita em voz alta, abre uma JANELA DE
ESCUTA de 8 s. Basta uma pergunta em qualquer frase: o Claude pergunta muitas
vezes primeiro e explica depois ("Which two files do you mean? ... I won't
touch either file until you answer."). Um falso positivo custa so uma janela
de escuta, porque nada se envia sem o "sim" do recap.

  - o ouvido escuta sem palavra de ativacao (o VAD decide o fim da fala) e a
    tecla de falar continua a funcionar;
  - a frase ouvida e a resposta a pergunta: vai tal e qual, sem reescrita,
    para a confirmacao rapida ("Responder ao Claude no <projeto>: <texto>.
    Envio?"), e so um "sim" a envia;
  - "sai da conversa" (ou "exit the conversation") fecha a janela sem enviar
    nada, e 8 s sem ninguem comecar a falar tambem;
  - calar, dormir e acordar passam a frente e fecham a janela.

Uma pergunta so abre a janela se o utilizador a ouviu: se o filtro da resposta
falada a cortou, a janela nao abre (o texto inteiro fica no ecra).

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


def acaba_em_pergunta(texto: str | None) -> bool:
    return bool(texto) and bool(_FIM_EM_PERGUNTA.search(texto.strip()))


def pede_resposta(texto: str | None, falado: str | None) -> bool:
    """A resposta do Claude faz uma pergunta e o utilizador ouviu-a.

    `falado` e o que a voz disse (ja filtrado a partir de `texto`): e nele que a
    pergunta tem de estar, porque uma pergunta que o filtro cortou nao foi ouvida.
    """
    return "?" in (texto or "") and tem_pergunta(falado)


def e_para_sair(texto: str | None) -> bool:
    """"sai da conversa" e equivalentes, ditos como frase inteira."""
    normalizado = " ".join(_normalizar(sem_palavra_de_ativacao(limpar_texto(texto or ""))).split())
    for cortesia in (" por favor", " please"):
        if normalizado.endswith(cortesia):
            normalizado = normalizado[: -len(cortesia)]
    return normalizado in FRASES_DE_SAIR


def resposta_literal(texto: str, projeto: str, nomes_de_projeto: tuple[str, ...] = ()) -> Interpretacao:
    """A resposta dita na janela, tal e qual, como pedido de conversa para o projeto.

    Nunca passa pelo LLM: o recap mostra e diz exatamente o que foi ouvido. A
    regra financeira aplica-se na mesma: um pedido de compra ou venda e recusado.
    """
    literal = limpar_texto(texto)
    termo = pedido_financeiro(literal, nomes_de_projeto)
    if termo is not None:
        return Interpretacao(
            texto=literal,
            intencao=INTENCAO_RECUSADA,
            projeto=projeto,
            prompt="",
            origem="regra",
            motivo=f"pedido financeiro na conversa ('{termo}')",
            termo_financeiro=termo,
        )
    return Interpretacao(
        texto=literal,
        intencao="conversa",
        projeto=projeto,
        prompt=literal,
        origem="regra",
        motivo="resposta na janela de conversa (texto literal, sem reescrita)",
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
