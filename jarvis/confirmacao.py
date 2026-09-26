r"""Confirmar antes de enviar: o jarvis diz o que percebeu e so age depois do "sim".

Recebe a `Interpretacao` de uma frase e decide:

  - horas/data, calar, dormir e acordar correm logo: so leem ou silenciam;
  - um pedido financeiro e recusado em voz alta, sem nada a confirmar;
  - uma frase que nao se percebeu (ou que o LLM nao interpretou) nao faz
    nada: o jarvis pede para repetir;
  - todas as outras intencoes tem efeito (enviar um prompt, abrir o editor
    ou a pasta, lancar, retomar ou parar um run, responder numa conversa,
    ler estado ou relatorio) e ficam PENDENTES: o jarvis mostra na consola o
    que percebeu e o texto exato a enviar, e diz em voz alta um resumo com
    no maximo duas frases.

Com um pedido pendente, cada resposta do utilizador e uma de:

  - "sim" / "envia" / "manda" / "confirma" (ou "yes" / "yeah" / "send it" /
    "go ahead" / "do it", ver `FRASES_DE_CONFIRMAR`): executa o pedido do
    ultimo recap, exatamente o que foi mostrado, e so esse;
  - "nao, muda X para Y" / "acrescenta ...": o interprete reescreve o pedido
    mantendo o resto, e o jarvis volta a recapitular;
  - "cancela" (ou "nao" sozinho): cancela sem enviar; um "cancel" mal
    ouvido ("Uh castle.") tambem conta, mas um "sim" tem de ser claro;
  - "que horas sao" / "what time is it" (a frase inteira, na lista branca
    do router): o jarvis diz as horas e o pedido continua pendente;
  - outra coisa: o jarvis volta a perguntar; a terceira vez cancela.

As hesitacoes (uh, um, hum, ...) e a pontuacao nao contam nas respostas; uma
resposta que so tem isso (ruido) e ignorada e nao gasta o prazo.

Sem resposta dentro do prazo (30 s por omissao, `[interprete].confirmacao_s`
no config.toml) o pedido e cancelado sem enviar. O prazo conta a partir do
fim da fala do recap (ou da pergunta repetida), nao de quando o recap foi
gerado; uma resposta que comecou a ser dita antes do fim do prazo conta,
mesmo que chegue depois dele. Uma resposta que chega
enquanto o recap ainda esta a ser preparado, ou que foi dita antes dele, e
ignorada: um "sim" so confirma o recap que o utilizador ja ouviu.

Uso:

    confirmacao = Confirmacao(interprete, executar, falar=voz.falar)
    confirmacao.iniciar(interprete.interpretar(frase))   # recap, fica pendente
    confirmacao.responder("sim")                         # executar(pedido)

ou, num ciclo que bloqueia a espera da resposta:

    confirmacao.dialogar(interpretacao, ouvir)   # ouvir(limite_s) -> texto | None
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Literal

from jarvis.interprete import (
    INTENCAO_RECUSADA,
    INTENCOES_COM_EFEITO,
    INTENCOES_COM_PROJETO,
    INTENCOES_COM_PROMPT,
    Interpretacao,
    Interprete,
    _PADRAO_TROCA,
    _so_o_projeto,
    limpar_texto,
    projetos_em_alternativa,
    projetos_mencionados,
    sem_palavra_de_ativacao,
)
from jarvis.router import _normalizar, encaminhar

#: No maximo este numero de palavras do prompt e dito em voz alta; um prompt
#: mais longo e resumido na voz e mostrado inteiro na consola.
PALAVRAS_DITAS = 30
CARACTERES_DITOS = 220
#: Quantas palavras do inicio de um prompt longo entram no resumo falado.
PALAVRAS_DO_RESUMO = 12

#: Respostas que nao se percebem antes de o pedido ser cancelado.
TENTATIVAS = 3

Estado = Literal[
    "executado",  # correu: logo (sem efeito) ou depois do "sim"
    "pendente",  # recap dito, a espera da resposta
    "cancelado",
    "expirado",  # sem resposta dentro do prazo: cancelado
    "recusado",  # pedido financeiro
    "nao_percebido",  # nada a executar nem a confirmar
    "falhou",  # confirmado, mas o executor falhou
    "ignorado",  # resposta que nao conta (recap a ser preparado ou dita antes dele)
    "sem_pedido",  # resposta sem nenhum pedido pendente
]

TipoDeResposta = Literal["confirmar", "cancelar", "corrigir", "acrescentar", "outro"]


# --- Resposta do utilizador ------------------------------------------------------

#: A frase inteira tem de ser uma destas (depois de normalizada) para enviar.
FRASES_DE_CONFIRMAR = frozenset(
    {
        "sim",
        "envia",
        "sim envia",
        "envia sim",
        "sim envia isso",
        "envia isso",
        "manda",
        "confirma",
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "send",
        "send it",
        "yes send",
        "yes send it",
        "go",
        "go ahead",
        "confirm",
        "do it",
        "ok send it",
        "okay send it",  # como o STT escreve "ok send it"
    }
)
#: Um "sim" no inicio de "sim, mas muda X": o resto e a correcao.
_SINS = ("sim", "yes", "yeah", "yep", "yup")
_CORTESIAS_NO_FIM = ("por favor", "please")
_NEGACOES = frozenset({"nao", "no", "nope"})
_PALAVRAS_DE_CANCELAR = frozenset(
    {
        "cancela", "cancelar", "cancele", "cancel", "cancelled", "esquece", "esquecer", "aborta",
        "abortar", "abort", "envies", "mandes", "dont", "don",
    }
)
_PARES_DE_CANCELAR = frozenset({"deixa estar", "forget it", "never mind", "nevermind", "do not"})
_VERBOS_DE_TROCA = frozenset(
    {
        "muda", "mudar", "mude", "troca", "trocar", "troque", "substitui", "substituir", "substitua",
        "altera", "alterar", "altere", "change", "replace", "switch", "swap",
    }
)
_VERBOS_DE_ACRESCENTO = frozenset(
    {"acrescenta", "acrescentar", "acrescente", "adiciona", "adicionar", "adicione", "junta",
     "juntar", "junte", "add", "append"}
)
_SIM_MAS_NO_INICIO = re.compile(r"^\W*(?:" + "|".join(_SINS) + r")\W+(?:mas|but)\W+", re.IGNORECASE)

#: Hesitacoes que o STT transcreve ("Uh, yes.", "hum, cancela"), ja
#: normalizadas. So saem do inicio e do fim da frase: no meio "um" e uma
#: palavra em portugues ("muda um teste").
_HESITACOES = frozenset(
    {"uh", "uhh", "uhm", "um", "umm", "hum", "hmm", "hm", "mm", "eh", "ehm", "er", "erm", "ah", "ahm", "ha", "han"}
)
_HESITACAO_NO_INICIO = re.compile(
    r"^\W*(?:" + "|".join(sorted(_HESITACOES | {"hã", "hãn"}, key=len, reverse=True)) + r")\b\W*",
    re.IGNORECASE,
)

#: Formas de cancelar aceites por aproximacao ("Uh castle.", "cancer", "can
#: sell"). Cancelar nunca tem efeito, por isso pode ser tolerante; enviar
#: nunca e aproximado.
_FRASES_DE_CANCELAR_APROXIMADAS = ("cancel", "cancel it", "cancela", "cancelar", "cancele", "cancelo", "cancela isso")
#: Uma frase mais comprida do que isto nunca e um cancelar mal ouvido.
_PALAVRAS_DO_CANCELAR_APROXIMADO = 3
#: Diferencas maximas entre os esqueletos de consoantes (Levenshtein).
_DISTANCIA_DO_CANCELAR_APROXIMADO = 1


def _sem_hesitacoes(palavras: list[str], *, no_fim: bool) -> list[str]:
    """Tira as hesitacoes do inicio (e do fim, se pedido), nunca esvaziando a frase."""
    inicio, fim = 0, len(palavras)
    while inicio < fim and palavras[inicio] in _HESITACOES:
        inicio += 1
    while no_fim and fim > inicio and palavras[fim - 1] in _HESITACOES:
        fim -= 1
    return palavras[inicio:fim] if inicio < fim else palavras


def _texto_sem_hesitacoes(texto: str) -> str:
    """O texto original sem as hesitacoes do inicio, nunca esvaziado."""
    restante = texto
    while True:
        seguinte = _HESITACAO_NO_INICIO.sub("", restante, count=1)
        if seguinte == restante or not _normalizar(seguinte):
            return restante
        restante = seguinte


def _palavras(texto: str, *, hesitacoes_no_fim: bool = False) -> list[str]:
    """As palavras normalizadas, sem hesitacoes no inicio nem cortesia no fim.

    As hesitacoes do fim so saem a pedido, ao decidir enviar ou cancelar: numa
    correcao o "um" final e uma palavra ("muda o dois para um").
    """
    palavras = _normalizar(sem_palavra_de_ativacao(limpar_texto(texto))).split()
    palavras = _sem_hesitacoes(palavras, no_fim=hesitacoes_no_fim)
    for cortesia in _CORTESIAS_NO_FIM:
        partes = cortesia.split()
        if len(palavras) > len(partes) and palavras[-len(partes) :] == partes:
            palavras = _sem_hesitacoes(palavras[: -len(partes)], no_fim=hesitacoes_no_fim)
    return palavras


def e_resposta_vazia(texto: str | None) -> bool:
    """A frase nao diz nada: vazia, so pontuacao ou so hesitacoes ("Uh.")."""
    palavras = _normalizar(sem_palavra_de_ativacao(limpar_texto(texto or ""))).split()
    return all(palavra in _HESITACOES for palavra in palavras)


def _esqueleto(palavras: list[str] | tuple[str, ...]) -> str:
    """Esqueleto fonetico simples: as consoantes como se ouvem, sem vogais.

    "castle" -> "ksl", "cancel" / "can sell" -> "knsl", "cancer" -> "knsr".
    """
    texto = "".join(palavras)
    texto = texto.replace("ph", "f").replace("ck", "k")
    texto = texto.replace("stl", "sl")  # "castle": o t nao se ouve
    texto = re.sub(r"c(?=[eiy])", "s", texto)
    texto = texto.replace("c", "k").replace("q", "k").replace("z", "s")
    texto = re.sub(r"(.)\1+", r"\1", texto)
    return texto[:1] + re.sub(r"[aeiouyhw]", "", texto[1:])


def _distancia(a: str, b: str) -> int:
    """Distancia de Levenshtein entre dois textos curtos."""
    anterior = list(range(len(b) + 1))
    for i, letra_a in enumerate(a, 1):
        atual = [i]
        for j, letra_b in enumerate(b, 1):
            atual.append(min(anterior[j] + 1, atual[j - 1] + 1, anterior[j - 1] + (letra_a != letra_b)))
        anterior = atual
    return anterior[-1]


_ESQUELETOS_DE_CANCELAR = frozenset(_esqueleto(frase.split()) for frase in _FRASES_DE_CANCELAR_APROXIMADAS)


def _parece_cancelar(palavras: list[str]) -> bool:
    """A frase curta soa a "cancel"/"cancela". Deterministico."""
    if not palavras or len(palavras) > _PALAVRAS_DO_CANCELAR_APROXIMADO:
        return False
    esqueleto = _esqueleto(palavras)
    if len(esqueleto) < 3:
        return False
    return any(_distancia(esqueleto, alvo) <= _DISTANCIA_DO_CANCELAR_APROXIMADO for alvo in _ESQUELETOS_DE_CANCELAR)


def classificar_resposta(texto: str | None) -> tuple[TipoDeResposta, str]:
    """(tipo, texto da edicao) de uma resposta ao recap. Deterministico.

    As hesitacoes (uh, um, hum, ...) e a pontuacao nao contam. So uma frase
    que e toda ela um "sim"/"envia" confirma, sem aproximacao nenhuma: "sim,
    mas muda X" e uma correcao, "acrescenta que e urgente" nunca envia.
    Cancelar aceita um "cancel" mal ouvido ("Uh castle."), mas so depois de
    se ver que a frase nao confirma, nao corrige nem acrescenta.
    """
    tipo, edicao, _aproximado = _classificar(texto)
    return tipo, edicao


def _classificar(texto: str | None) -> tuple[TipoDeResposta, str, bool]:
    """(tipo, texto da edicao, cancelar por aproximacao)."""
    limpo = _texto_sem_hesitacoes(sem_palavra_de_ativacao(limpar_texto(texto or "")))
    palavras = _palavras(limpo, hesitacoes_no_fim=True)
    if not palavras:
        return "outro", "", False
    if " ".join(palavras) in FRASES_DE_CONFIRMAR:
        return "confirmar", "", False
    tipo, edicao = _classificar_sem_confirmar(limpo, palavras)
    if tipo == "outro" and _parece_cancelar(palavras):
        return "cancelar", "", True
    return tipo, edicao, False


def _classificar_sem_confirmar(limpo: str, palavras: list[str]) -> tuple[TipoDeResposta, str]:
    if palavras[0] in _SINS and len(palavras) > 2 and palavras[1] in {"mas", "but"}:
        palavras = palavras[2:]
        limpo = _SIM_MAS_NO_INICIO.sub("", limpo, count=1)
    negada = False
    while palavras and palavras[0] in _NEGACOES:
        negada = True
        palavras = palavras[1:]
    if not palavras:
        return "cancelar", ""
    if palavras[0] in _PALAVRAS_DE_CANCELAR or " ".join(palavras[:2]) in _PARES_DE_CANCELAR:
        return "cancelar", ""
    if palavras[0] in _VERBOS_DE_TROCA:
        return "corrigir", limpo
    inicio = palavras[1:] if palavras[0] in {"e", "and", "tambem", "also"} else palavras
    if inicio and inicio[0] in _VERBOS_DE_ACRESCENTO:
        return "acrescentar", limpo
    if negada:
        return "corrigir", limpo
    return "outro", ""


def e_correcao(texto: str | None, nomes: tuple[str, ...] | list[str]) -> bool:
    """A frase e claramente uma correcao a um pedido. Deterministico, sem LLM.

    Conta uma negacao seguida de uma troca ("nao, muda X para Y", "no, change
    X to Y"), ou uma troca sozinha ("change X to Y") quando X ou Y e so um
    nome de projeto, ou quando a frase nao diz nenhum projeto. Uma troca que
    diz o projeto noutro sitio ("change the title to welcome in atlas") e um
    ditado, e um acrescento ("add ...") nunca e uma correcao sem pedido.
    """
    palavras = _palavras(texto or "")
    negada = False
    while palavras and palavras[0] in _NEGACOES:
        negada = True
        palavras = palavras[1:]
    if not palavras or palavras[0] not in _VERBOS_DE_TROCA:
        return False
    frase = " ".join(palavras)
    troca = _PADRAO_TROCA.match(frase)
    if troca is None:
        return False
    if negada:
        return True
    if _so_o_projeto(troca.group("de"), nomes) or _so_o_projeto(troca.group("para"), nomes):
        return True
    return not projetos_mencionados(frase, nomes)


# --- Recap --------------------------------------------------------------------


@dataclass(frozen=True)
class Pedido:
    """O que o executor recebe. `prompt` e o texto exato mostrado no recap."""

    intencao: str
    projeto: str | None
    prompt: str
    detalhe: str | None = None


@dataclass(frozen=True)
class Recap:
    """Um recap apresentado: o pedido, o que foi dito e o que foi mostrado."""

    numero: int
    pedido: Pedido
    fala: str
    ecra: str
    #: A pergunta que fecha a fala ("Envio?", "Confirmas?" ou qual projeto).
    pergunta: str = ""
    #: O pedido precisa de projeto e nenhum foi dito: "sim" nao chega.
    falta_projeto: bool = False


@dataclass(frozen=True)
class Desfecho:
    estado: Estado
    motivo: str
    #: O pedido executado (so em "executado" e "falhou").
    pedido: Pedido | None = None
    #: O recap pendente (em "pendente") ou o que foi confirmado.
    recap: Recap | None = None
    resultado: object = None

    @property
    def executado(self) -> bool:
        return self.estado == "executado"


_ACOES_PT = {
    "ditar_prompt": "Para o {p}",
    "conversa": "Responder ao Claude no {p}",
    "lancar_run": "Lançar um run no {p} com o objetivo",
    "retomar_run": "Retomar o run do {p}",
    "parar_run": "Parar o run do {p}",
    "abrir_editor": "Abrir o editor no {p}",
    "abrir_pasta": "Abrir a pasta do {p}",
    "estado": "Ver o estado do {p}",
    "ler_relatorio": "Ler o relatório do {p}",
}
_ACOES_EN = {
    "ditar_prompt": "To {p}",
    "conversa": "Reply to Claude in {p}",
    "lancar_run": "Start a run in {p} with the goal",
    "retomar_run": "Resume the run in {p}",
    "parar_run": "Stop the run in {p}",
    "abrir_editor": "Open the editor in {p}",
    "abrir_pasta": "Open the folder of {p}",
    "estado": "Check the status of {p}",
    "ler_relatorio": "Read the report of {p}",
}
_SEM_PROJETO = {
    "pt": {"conversa": "Responder ao Claude", "projeto": "que projeto", "marcador": "<projeto>"},
    "en": {"conversa": "Reply to Claude", "projeto": "which project", "marcador": "<project>"},
}

_FRASES = {
    "pt": {
        "enviar": "Envio?",
        "confirmar": "Confirmas?",
        "longo": "{acao}, um pedido de {n} palavras que começa por: {inicio}, e o resto está no ecrã",
        "sem_projeto": "Percebi o pedido, mas não o projeto",
        "recusado": "Isso não faço por voz: pedidos de dinheiro ou de bolsa ficam de fora.",
        "nao_percebi": "Não percebi. Repete, por favor.",
        "cancelado": "Cancelado, não enviei nada.",
        "expirado": "Sem resposta, cancelei. Não enviei nada.",
        "de_novo": "Diz sim para enviar, muda, acrescenta ou cancela.",
        "correcao_falhou": "Não consegui aplicar essa correção; o pedido fica igual. {pergunta}",
        "falhou": "Não consegui fazer isso.",
        "nada_para_corrigir": "Não há nenhum pedido à espera para corrigir.",
        "ecra_percebi": "Percebi: {intencao}{projeto}",
        "ecra_enviar": "Texto a enviar:",
        "ecra_ajuda": 'Responde "sim" para enviar, "não, muda X para Y", "acrescenta ..." ou "cancela".',
        "ecra_falta_projeto": "Falta o projeto: diz o nome do projeto, ou \"cancela\".",
    },
    "en": {
        "enviar": "Send it?",
        "confirmar": "Confirm?",
        "longo": "{acao}, a {n}-word request that starts with: {inicio}, and the rest is on screen",
        "sem_projeto": "I got the request, but not the project",
        "recusado": "I don't do that by voice: money and trading requests are off limits.",
        "nao_percebi": "I didn't get that. Please say it again.",
        "cancelado": "Cancelled, nothing was sent.",
        "expirado": "No answer, so I cancelled. Nothing was sent.",
        "de_novo": "Say yes to send, change, add or cancel.",
        "correcao_falhou": "I couldn't apply that change; the request stays the same. {pergunta}",
        "falhou": "I couldn't do that.",
        "nada_para_corrigir": "There is no pending request to correct.",
        "ecra_percebi": "Understood: {intencao}{projeto}",
        "ecra_enviar": "Text to send:",
        "ecra_ajuda": 'Answer "yes" to send, "no, change X to Y", "add ..." or "cancel".',
        "ecra_falta_projeto": "Missing project: say the project name, or \"cancel\".",
    },
}

#: Fim de frase dentro do prompt: na voz vira virgula, para o recap falado
#: ter sempre no maximo duas frases (o pedido e a pergunta).
_FIM_DE_FRASE = re.compile(r"\s*[.!?…;]+(?:\s+|$)")


def _numa_frase(texto: str) -> str:
    return _FIM_DE_FRASE.sub(", ", texto.strip()).strip().rstrip(",").strip()


def contar_frases(fala: str) -> int:
    """Quantas frases tem um texto falado (fim de frase seguido de espaco ou fim)."""
    return len([parte for parte in re.split(r"(?<=[.!?…])\s+", fala.strip()) if parte.strip()])


def _com_projeto(modelo: str, projeto: str | None, lingua: str) -> str:
    return modelo.format(p=projeto if projeto else _SEM_PROJETO[lingua]["marcador"])


def compor_recap(numero: int, interpretacao: Interpretacao, lingua: str) -> Recap:
    """O recap de um pedido com efeito: fala (<= 2 frases) e ecra (tudo)."""
    frases = _FRASES[lingua]
    acoes = _ACOES_EN if lingua == "en" else _ACOES_PT
    intencao, projeto = interpretacao.intencao, interpretacao.projeto
    # O texto mostrado e o texto enviado sao o mesmo objeto, numa so linha.
    prompt = limpar_texto(interpretacao.prompt) if intencao in INTENCOES_COM_PROMPT else ""
    pedido = Pedido(intencao, projeto, prompt, interpretacao.detalhe)
    falta_projeto = intencao in INTENCOES_COM_PROJETO and projeto is None

    if intencao == "conversa" and projeto is None:
        acao = _SEM_PROJETO[lingua]["conversa"]
    else:
        acao = _com_projeto(acoes[intencao], projeto, lingua)

    if falta_projeto:
        pergunta = _numa_frase(interpretacao.pergunta or _SEM_PROJETO[lingua]["projeto"]) + "?"
        pergunta = pergunta[0].upper() + pergunta[1:]
        fala = f"{frases['sem_projeto']}. {pergunta}"
    else:
        pergunta = frases["enviar"] if prompt else frases["confirmar"]
        if not prompt:
            fala = f"{acao}. {pergunta}"
        else:
            dito = _numa_frase(prompt)
            palavras = dito.split()
            if len(palavras) > PALAVRAS_DITAS or len(dito) > CARACTERES_DITOS:
                inicio = " ".join(palavras[:PALAVRAS_DO_RESUMO]).rstrip(",")
                fala = frases["longo"].format(acao=acao, n=len(prompt.split()), inicio=inicio) + f". {pergunta}"
            else:
                fala = f"{acao}: {dito}. {pergunta}"

    nome_da_intencao = intencao.replace("_", " ")
    linhas = [
        frases["ecra_percebi"].format(
            intencao=nome_da_intencao, projeto=f" | projeto: {projeto}" if projeto else ""
        )
    ]
    if prompt:
        linhas += [frases["ecra_enviar"], prompt]
    else:
        linhas.append(acao)
    linhas.append(frases["ecra_falta_projeto"] if falta_projeto else frases["ecra_ajuda"])
    return Recap(numero, pedido, fala, "\n".join(linhas), pergunta, falta_projeto)


# --- Dialogo ------------------------------------------------------------------


class Confirmacao:
    """O dialogo de confirmacao de um pedido de cada vez. Seguro entre threads.

    `executar(pedido)` so e chamado para intencoes sem efeito ou depois de um
    "sim" explicito ao ultimo recap; `falar(texto)` diz, `mostrar(texto)`
    escreve na consola. O relogio tem de ser o mesmo dos instantes passados
    em `dito_em` (por omissao `time.perf_counter`, como no ouvido).
    """

    def __init__(
        self,
        interprete: Interprete,
        executar: Callable[[Pedido], object],
        *,
        falar: Callable[[str], object],
        mostrar: Callable[[str], object] = print,
        lingua: str | None = None,
        limite_s: float | None = None,
        relogio: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.interprete = interprete
        self._executar = executar
        self._falar = falar
        self._mostrar = mostrar
        self.lingua = "en" if (lingua or interprete.lingua) == "en" else "pt"
        espera = interprete.config.interprete.confirmacao_s if limite_s is None else limite_s
        if isinstance(espera, bool) or not isinstance(espera, (int, float)) or espera <= 0:
            raise ValueError(f"limite da confirmacao invalido: {espera!r}")
        self.limite_s = float(espera)
        self._relogio = relogio
        self._trinco = threading.Lock()
        self._pendente: Interpretacao | None = None
        self._recap: Recap | None = None
        self._prazo = 0.0
        self._apresentado_em = 0.0
        self._ocupado = False
        self._falhas = 0
        self._numero = 0

    # -- estado

    @property
    def a_espera(self) -> bool:
        """Ha um pedido por confirmar."""
        with self._trinco:
            return self._pendente is not None

    @property
    def recap(self) -> Recap | None:
        """O ultimo recap apresentado do pedido pendente."""
        with self._trinco:
            return self._recap if self._pendente is not None else None

    def prazo_restante(self) -> float | None:
        with self._trinco:
            if self._pendente is None:
                return None
            if self._ocupado:
                return self.limite_s
            return max(0.0, self._prazo - self._relogio())

    def _texto(self, chave: str, **valores: str) -> str:
        return _FRASES[self.lingua][chave].format(**valores)

    def _limpar(self) -> None:
        self._pendente = None
        self._recap = None
        self._ocupado = False
        self._falhas = 0

    # -- entrada de uma frase nova

    def iniciar(self, interpretacao: Interpretacao) -> Desfecho:
        """Trata uma frase interpretada: executa, recusa ou fica pendente."""
        with self._trinco:
            substituido = self._pendente is not None
            self._limpar()
        if substituido:
            self._mostrar("confirmacao | pedido anterior cancelado por um pedido novo; nada foi enviado")

        if interpretacao.intencao == INTENCAO_RECUSADA:
            self._falar(self._texto("recusado"))
            return Desfecho("recusado", interpretacao.motivo)
        if interpretacao.pode_dispensar_confirmacao:
            pedido = Pedido(interpretacao.intencao, None, "", interpretacao.detalhe)
            return self._correr(pedido, None, "sem efeito: dispensa confirmacao")
        sem_texto = interpretacao.intencao in INTENCOES_COM_PROMPT and not limpar_texto(interpretacao.prompt)
        if interpretacao.so_confirmacao or interpretacao.intencao not in INTENCOES_COM_EFEITO or sem_texto:
            if interpretacao.texto:
                self._mostrar(f"confirmacao | nao percebi: {interpretacao.texto}")
            self._falar(self._texto("nao_percebi"))
            return Desfecho("nao_percebido", interpretacao.motivo)
        return self._propor(interpretacao)

    def _propor(self, interpretacao: Interpretacao, fala: str | None = None) -> Desfecho:
        with self._trinco:
            self._numero += 1
            recap = compor_recap(self._numero, interpretacao, self.lingua)
            self._pendente = interpretacao
            self._recap = None
            self._ocupado = True
        self._mostrar(recap.ecra)
        self._falar(fala or recap.fala)
        with self._trinco:
            if self._pendente is not interpretacao:
                return Desfecho("cancelado", "o pedido foi cancelado durante o recap")
            self._recap = recap
            self._apresentado_em = self._relogio()
            self._prazo = self._apresentado_em + self.limite_s
            self._ocupado = False
        return Desfecho("pendente", "a espera de confirmacao", recap=recap)

    # -- resposta do utilizador

    def responder(self, texto: str | None, *, dito_em: float | None = None) -> Desfecho:
        """Trata a resposta ao recap. `dito_em`: quando a fala comecou.

        Uma resposta que comecou a ser dita antes do fim do prazo conta,
        mesmo que chegue depois dele. Uma resposta vazia (ruido, so pontuacao
        ou so hesitacoes) e ignorada: nao diz nada, nao conta como tentativa
        e nao mexe no prazo.
        """
        acao: str
        with self._trinco:
            if self._pendente is None:
                return Desfecho("sem_pedido", "nao ha nenhum pedido por confirmar")
            if self._ocupado:
                return Desfecho("ignorado", "o recap ainda esta a ser preparado")
            dita_a_tempo = dito_em is not None and dito_em < self._prazo
            if self._relogio() >= self._prazo and not dita_a_tempo:
                acao = "expirar"
            elif dito_em is not None and dito_em < self._apresentado_em:
                return Desfecho("ignorado", "resposta dita antes do recap", recap=self._recap)
            elif e_resposta_vazia(texto):
                return Desfecho("ignorado", "resposta vazia", recap=self._recap)
            else:
                recap = self._recap
                assert recap is not None
                pendente = self._pendente
                horas = self._horas_pedidas(texto)
                tipo, edicao, aproximado = _classificar(texto)
                projeto_dito = recap.falta_projeto and self._projeto_dito(texto) is not None
                if horas is not None:
                    # So le as horas: responde e o pedido continua a espera.
                    self._ocupado = True
                    acao = "horas"
                elif tipo == "confirmar" and not recap.falta_projeto:
                    self._limpar()
                    acao = "executar"
                elif tipo == "cancelar" and not (aproximado and projeto_dito):
                    self._limpar()
                    acao = "cancelar"
                elif projeto_dito:
                    # "no orbita" e a resposta a "qual projeto", nao um "no".
                    self._ocupado = True
                    acao = "projeto"
                elif tipo in ("corrigir", "acrescentar"):
                    self._ocupado = True
                    acao = "corrigir"
                else:
                    self._falhas += 1
                    if self._falhas >= TENTATIVAS:
                        self._limpar()
                        acao = "desistir"
                    else:
                        # O prazo novo so conta depois de a pergunta ser dita.
                        self._ocupado = True
                        acao = "de_novo"

        if acao == "expirar":
            return self._expirar()
        if acao == "horas":
            return self._horas_com_pedido_pendente(pendente, horas)
        if acao == "executar":
            return self._correr(recap.pedido, recap, "confirmado")
        if acao == "cancelar":
            self._mostrar("confirmacao | cancelado; nada foi enviado")
            self._falar(self._texto("cancelado"))
            return Desfecho("cancelado", "cancelado pelo utilizador", recap=recap)
        if acao == "desistir":
            self._mostrar("confirmacao | cancelado depois de respostas que nao percebi; nada foi enviado")
            self._falar(self._texto("cancelado"))
            return Desfecho("cancelado", f"{TENTATIVAS} respostas sem confirmar nem corrigir", recap=recap)
        if acao == "de_novo":
            try:
                self._falar(recap.pergunta if recap.falta_projeto else self._texto("de_novo"))
            finally:
                with self._trinco:
                    if self._pendente is pendente:
                        self._ocupado = False
                        self._prazo = self._relogio() + self.limite_s
            return Desfecho("pendente", "resposta nao percebida", recap=recap)
        if acao == "projeto":
            projeto = self._projeto_dito(texto)
            return self._propor(replace(pendente, projeto=projeto, pergunta=None))
        return self._aplicar_correcao(pendente, recap, edicao, tipo)

    def _horas_pedidas(self, texto: str | None) -> str | None:
        """"horas" ou "data" se a frase inteira e esse pedido na lista branca do router."""
        frase = _texto_sem_hesitacoes(sem_palavra_de_ativacao(limpar_texto(texto or "")))
        try:
            encaminhado = encaminhar(frase, self.interprete.config)
        except Exception:  # noqa: BLE001 - na duvida, e uma resposta ao recap
            return None
        if encaminhado.tipo != "local" or encaminhado.nome_acao != "horas_e_data":
            return None
        return "data" if encaminhado.argumento == "data" else "horas"

    def _horas_com_pedido_pendente(self, pendente: Interpretacao, detalhe: str) -> Desfecho:
        """Diz as horas ou a data; o pedido pendente fica e o prazo recomeca."""
        self._mostrar("confirmacao | horas pedidas com um pedido a espera; o pedido continua pendente")
        try:
            return self._correr(Pedido("horas", None, "", detalhe), None, "horas com um pedido pendente")
        finally:
            with self._trinco:
                if self._pendente is pendente:
                    self._ocupado = False
                    self._prazo = self._relogio() + self.limite_s

    def e_correcao_sem_pedido(self, texto: str | None) -> bool:
        """A frase e claramente uma correcao e nao ha nenhum pedido a espera."""
        nomes = tuple(projeto.nome for projeto in self.interprete.config.projetos)
        return not self.a_espera and e_correcao(texto, nomes)

    def correcao_sem_pedido(self) -> Desfecho:
        """Diz que nao ha nenhum pedido a corrigir. Nunca chama o interprete nem abre um recap."""
        self._mostrar("confirmacao | correcao sem nenhum pedido a espera; nada foi enviado")
        self._falar(self._texto("nada_para_corrigir"))
        return Desfecho("sem_pedido", "correcao sem nenhum pedido por confirmar")

    def _projeto_dito(self, texto: str | None) -> str | None:
        nomes = tuple(projeto.nome for projeto in self.interprete.config.projetos)
        limpo = limpar_texto(texto or "")
        ditos = projetos_mencionados(limpo, nomes)
        if len(ditos) != 1 or projetos_em_alternativa(limpo, nomes):
            return None
        return ditos[0]

    def _aplicar_correcao(
        self, pendente: Interpretacao, recap: Recap, edicao: str, tipo: str
    ) -> Desfecho:
        try:
            nova = self.interprete.corrigir(pendente, edicao, tipo)
        except Exception as erro:  # noqa: BLE001 - uma falha nunca envia nada
            nova = replace(pendente, intencao="desconhecido", motivo=f"a correcao falhou: {erro!r}")
        with self._trinco:
            if self._pendente is not pendente:
                return Desfecho("cancelado", "o pedido foi cancelado durante a correcao")
        if nova.intencao == INTENCAO_RECUSADA:
            with self._trinco:
                self._limpar()
            self._mostrar("confirmacao | correcao recusada (pedido financeiro); nada foi enviado")
            self._falar(self._texto("recusado"))
            return Desfecho("recusado", nova.motivo, recap=recap)
        if nova.intencao not in INTENCOES_COM_EFEITO or nova.so_confirmacao:
            self._mostrar(f"confirmacao | correcao nao aplicada ({nova.motivo})")
            return self._propor(pendente, fala=self._texto("correcao_falhou", pergunta=recap.pergunta))
        return self._propor(nova)

    # -- prazo, cancelamento e execucao

    def verificar_tempo(self) -> Desfecho | None:
        """Cancela o pedido se o prazo passou. Chamar periodicamente."""
        with self._trinco:
            if self._pendente is None or self._ocupado or self._relogio() < self._prazo:
                return None
        return self._expirar()

    def _expirar(self) -> Desfecho:
        with self._trinco:
            if self._pendente is None:
                return Desfecho("sem_pedido", "nao ha nenhum pedido por confirmar")
            recap = self._recap
            self._limpar()
        self._mostrar(f"confirmacao | sem resposta em {self.limite_s:g} s: cancelado; nada foi enviado")
        self._falar(self._texto("expirado"))
        return Desfecho("expirado", f"sem resposta em {self.limite_s:g} s", recap=recap)

    def cancelar(self, motivo: str) -> Desfecho | None:
        """Cancela em silencio o pedido pendente (por exemplo ao adormecer)."""
        with self._trinco:
            if self._pendente is None:
                return None
            recap = self._recap
            self._limpar()
        self._mostrar(f"confirmacao | cancelado ({motivo}); nada foi enviado")
        return Desfecho("cancelado", motivo, recap=recap)

    def _correr(self, pedido: Pedido, recap: Recap | None, motivo: str) -> Desfecho:
        try:
            resultado = self._executar(pedido)
        except Exception as erro:  # noqa: BLE001 - o erro e dito, nunca derruba o dialogo
            self._mostrar(f"confirmacao | {pedido.intencao} falhou: {erro!r}")
            self._falar(self._texto("falhou"))
            return Desfecho("falhou", f"{motivo}; o executor falhou: {erro!r}", pedido, recap)
        return Desfecho("executado", motivo, pedido, recap, resultado)

    # -- ciclo bloqueante

    def dialogar(
        self, interpretacao: Interpretacao, ouvir: Callable[[float], str | None]
    ) -> Desfecho:
        """Trata a frase e, se ficar pendente, ouve ate confirmar, cancelar ou expirar.

        `ouvir(limite_s)` devolve o texto da proxima resposta, ou None se nada
        foi dito dentro de `limite_s`: None fecha o pedido como expirado. Um
        texto vazio (ruido) e ignorado e continua a ouvir ate ao prazo.
        """
        desfecho = self.iniciar(interpretacao)
        while self.a_espera:
            restante = self.prazo_restante()
            if restante is None:
                break
            texto = ouvir(restante) if restante > 0 else None
            if texto is None:
                desfecho = self._expirar()
                continue
            resposta = self.responder(texto)
            if resposta.estado != "ignorado":
                desfecho = resposta
        return desfecho
