r"""Confirmar antes de enviar: o jarvis diz o que percebeu e so age depois do "sim".

Recebe a `Interpretacao` de uma frase e decide:

  - horas/data, calar, dormir e acordar correm logo: so leem ou silenciam;
  - um pedido financeiro e recusado em voz alta, sem nada a confirmar;
  - uma pergunta geral ou de atualidade segue logo para quem a responde:
    so le, nao mexe em nada nem vai a uma sessao de projeto;
  - uma frase que nao se percebeu (ou que o LLM nao interpretou) nao faz
    nada: o jarvis pede para repetir;
  - ver o estado ou ler o relatorio de um projeto so le: corre logo, sem
    recap; sem projeto, usa o ultimo projeto usado (ver abaixo) e a resposta
    diz qual; sem nenhum recente, o jarvis pergunta qual e corre logo que ele
    e dito;
  - todas as outras intencoes tem efeito (enviar um prompt, abrir o editor
    ou a pasta, lancar, retomar ou parar um run, responder numa conversa) e
    ficam PENDENTES: o jarvis mostra na consola o que percebeu e o texto
    exato a enviar, e diz em voz alta um recap curto que acaba numa pergunta
    leve ("Add tests to the login page, for atlas - send it?"); um prompt
    longo e dito pelo tema, em poucas palavras, e o texto inteiro fica no
    ecra;
  - um ditado sem projeto dito, feito ate `[interprete] ultimo_projeto_min`
    minutos (10 por omissao) depois do ultimo pedido executado num projeto,
    assume esse projeto e o recap diz qual ("..., still for atlas - send
    it?"); continua a so ir depois do "sim", e "no, for forja" troca de
    projeto e recapitula outra vez;
  - guardar ou apagar um facto do caderno da memoria (`lembrar_facto`,
    `esquecer_facto`, vindos do jarvis depois de `jarvis.memoria` aceitar o
    facto) tambem fica pendente: o recap diz o facto e so um "sim" o grava
    ou apaga; uma correcao nao se aplica a um facto (diz-se outra vez);
  - a unica excecao e `enviar_sem_recap`: uma resposta curta a uma pergunta
    do Claude, dita na janela de conversa (`jarvis.conversa`), vai logo.

O cerebro de conversa (`jarvis.cerebro_mcp`) nunca executa nada: uma
ferramenta com efeito chega aqui por `propor_do_cerebro` e fica pendente como
qualquer outro pedido, com o mesmo recap, o mesmo prazo, as mesmas respostas
e correcoes. So um pedido pendente de cada vez: com outro a espera, devolve
"ocupado" e nada muda. `ao_fechar(dono, desfecho)` diz a quem o propos como
acabou (executado, cancelado, expirado, falhou, recusado).

Com um pedido pendente, cada resposta do utilizador e uma de:

  - "sim" / "envia" / "manda" / "confirma" (ou "yes" / "yeah" / "send it" /
    "go ahead" / "do it"), sozinhos ou juntos com cortesias ("Go, yes.",
    "yes please", "ok yes", ver `PALAVRAS_DE_CONFIRMAR`): executa o pedido
    do ultimo recap, exatamente o que foi mostrado, e so esse;
  - "aborta" / "abort" (a palavra principal), "cancela" / "cancel" ou "nao"
    sozinho: cancela sem enviar. Um cancelar mal ouvido ("Uh castle.",
    "Can't sell it.", "a board") tambem conta, e e visto antes das correcoes,
    por isso nunca chega a regra financeira; um "sim" tem de ser claro, e
    uma resposta que confirma e cancela ao mesmo tempo pergunta de novo;
  - "nao, muda X para Y" / "acrescenta ...": o interprete reescreve o pedido
    mantendo o resto, e o jarvis volta a recapitular (com `llm_na_correcao`
    a dizer que nao, so a edicao mecanica, sem LLM);
  - com o projeto assumido, "no, for forja" / "nao, para o forja" (so o nome
    de outro projeto, com negacao ou preposicao) troca o projeto, sem LLM, e
    o jarvis volta a recapitular;
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
    INTENCAO_PERGUNTA_GERAL,
    INTENCAO_RECUSADA,
    INTENCOES_COM_EFEITO,
    INTENCOES_COM_PROJETO,
    INTENCOES_COM_PROMPT,
    Interpretacao,
    Interprete,
    _PADRAO_TROCA,
    _so_o_projeto,
    limpar_texto,
    pedido_financeiro,
    pergunta_de_projeto,
    pergunta_dos_projetos_conhecidos,
    projetos_em_alternativa,
    projetos_mencionados,
    sem_palavra_de_ativacao,
)
from jarvis.conversa import e_resposta_curta
from jarvis.persona import CASO_NAO_PERCEBI, Persona, Variantes
from jarvis.router import _normalizar, encaminhar

#: Intencoes de um projeto que so leem: correm logo, sem recap nem "sim",
#: quando o projeto e conhecido. Sem projeto, o jarvis pergunta qual e corre
#: logo que ele e dito. Lancar, retomar e parar um run continuam a confirmar.
INTENCOES_SO_DE_LEITURA = frozenset({"estado", "ler_relatorio"})

#: Guardar e apagar um facto do caderno da memoria: fora do esquema do LLM,
#: so o jarvis as cria (`jarvis.memoria` ja verificou o facto), e so correm
#: depois de um "sim" ao recap que diz o facto.
INTENCAO_LEMBRAR_FACTO = "lembrar_facto"
INTENCAO_ESQUECER_FACTO = "esquecer_facto"
INTENCOES_DA_MEMORIA = frozenset({INTENCAO_LEMBRAR_FACTO, INTENCAO_ESQUECER_FACTO})

#: Sem projeto dito, estas assumem o ultimo projeto usado: o ditado (que
#: continua a ter recap e "sim") e as leituras. As outras perguntam qual.
_INTENCOES_QUE_ASSUMEM = frozenset({"ditar_prompt"}) | INTENCOES_SO_DE_LEITURA

#: Tudo o que fica pendente de um recap e de um "sim".
_INTENCOES_COM_RECAP = INTENCOES_COM_EFEITO | INTENCOES_DA_MEMORIA
#: Intencoes cujo texto aparece no recap e vai no pedido.
_INTENCOES_COM_TEXTO = INTENCOES_COM_PROMPT | INTENCOES_DA_MEMORIA

#: Um prompt ate este tamanho e dito inteiro no recap, com as frases como
#: estao; um prompt maior e dito pelo tema e mostrado inteiro na consola.
PALAVRAS_DITAS = 20
CARACTERES_DITOS = 140
#: O tema de um prompt longo: a primeira frase, se for curta, ou a primeira
#: oracao; senao as primeiras palavras.
PALAVRAS_DO_TEMA = 10
PALAVRAS_DO_TEMA_CORTADO = 8
#: Palavras que nunca acabam um tema cortado ("Fix the login and").
_PALAVRAS_DE_LIGACAO = frozenset(
    {
        "and", "or", "but", "the", "a", "an", "to", "of", "in", "on", "for", "with", "that", "so", "it", "is",
        "e", "ou", "mas", "o", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "para", "com", "que",
        "em", "no", "na", "ao",
    }
)

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
    "ocupado",  # pedido do cerebro com outro ja pendente: nada mudou
]

TipoDeResposta = Literal["confirmar", "cancelar", "corrigir", "acrescentar", "outro"]


# --- Resposta do utilizador ------------------------------------------------------

#: Palavras (ou pares) que confirmam. Para enviar, a resposta inteira tem de
#: ser feita so destas e de cortesias, com pelo menos uma destas: "yes",
#: "Go, yes.", "yes, send it", "yeah go ahead", "yes yes". Sem aproximacao.
PALAVRAS_DE_CONFIRMAR = frozenset(
    {
        "sim", "envia", "envia isso", "manda", "confirma",
        "yes", "yeah", "yep", "yup", "sure", "send", "send it", "go", "go ahead", "confirm", "do it",
    }
)
#: Cortesias que podem acompanhar um "sim", mas sozinhas nunca enviam.
PALAVRAS_DE_CORTESIA = frozenset({"ok", "okay", "please", "por favor"})
#: Exemplos de respostas que enviam (a regra e `PALAVRAS_DE_CONFIRMAR`).
FRASES_DE_CONFIRMAR = frozenset(
    {
        "sim", "envia", "sim envia", "envia sim", "sim envia isso", "envia isso", "manda", "confirma",
        "yes", "yeah", "yep", "yup", "sure", "send", "send it", "yes send", "yes send it", "go", "go ahead",
        "confirm", "do it", "ok send it", "okay send it", "go yes", "yes please", "yeah go ahead", "ok yes",
        "yes yes",
    }
)
#: Um "sim" no inicio de "sim, mas muda X": o resto e a correcao.
_SINS = ("sim", "yes", "yeah", "yep", "yup")
_CORTESIAS_NO_FIM = ("por favor", "please")
_NEGACOES = frozenset({"nao", "no", "nope"})
#: Palavras que, no inicio da resposta, cancelam. "abort" e a principal.
_PALAVRAS_DE_CANCELAR = frozenset(
    {
        "abort", "aborta", "abortar", "aborte", "cancela", "cancelar", "cancele", "cancel", "cancelled",
        "esquece", "esquecer", "envies", "mandes", "dont", "don",
    }
)
_PARES_DE_CANCELAR = frozenset({"deixa estar", "forget it", "never mind", "nevermind", "do not"})
#: Palavras que, numa resposta que confirma e cancela ao mesmo tempo ("yes
#: abort"), fazem a resposta ambigua: nao envia nem cancela, pergunta de novo.
_PALAVRAS_DE_CONFIRMAR_NA_MISTURA = frozenset(
    {"sim", "yes", "yeah", "yep", "yup", "sure", "confirm", "confirma", "ok", "okay", "send", "envia", "manda", "go"}
)
#: Um verbo de enviar a seguir a isto nao confirma: "don't send it" cancela.
_NEGAM_O_VERBO = frozenset({"dont", "not", "never", "nao"})
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

#: Formas de cancelar aceites por aproximacao ("Uh castle.", "Can't sell it.",
#: "a board", "abored"). Cancelar nunca tem efeito, por isso pode ser
#: tolerante; enviar nunca e aproximado.
_FRASES_DE_CANCELAR_APROXIMADAS = (
    "abort", "abort it", "aborta", "abortar", "aborta isso",
    "cancel", "cancel it", "cancela", "cancelar", "cancele", "cancelo", "cancela isso",
)
#: Uma frase mais comprida do que isto nunca e um cancelar mal ouvido.
_PALAVRAS_DO_CANCELAR_APROXIMADO = 3
#: Diferencas maximas entre os esqueletos de consoantes (Levenshtein).
_DISTANCIA_DO_CANCELAR_APROXIMADO = 1


#: Um artigo solto antes do verbo de uma correcao ("The no change ...").
_ARTIGOS_SOLTOS = frozenset({"the", "o"})
#: Hesitacoes que em portugues sao o artigo ("muda um teste").
_HESITACOES_QUE_SAO_ARTIGOS = frozenset({"um"})


def _abertura(palavras: list[str]) -> tuple[list[str], bool]:
    """(palavras a partir do verbo, houve negacao antes dele).

    Antes do verbo de uma correcao podem vir, em qualquer ordem, hesitacoes,
    negacoes e um artigo solto ("The no change uh um the login screen to
    ..."); o verbo seguinte tambem perde as hesitacoes que o seguem. Sem
    negacao nem verbo a seguir, as palavras ficam como estavam, e um artigo
    sem negacao nem hesitacao ao lado e o comeco de uma frase nova ("The
    change log ...").
    """
    inicio, negada, artigo, hesitou = 0, False, False, False
    while inicio < len(palavras) and (
        palavras[inicio] in _HESITACOES or palavras[inicio] in _NEGACOES or palavras[inicio] in _ARTIGOS_SOLTOS
    ):
        negada = negada or palavras[inicio] in _NEGACOES
        artigo = artigo or palavras[inicio] in _ARTIGOS_SOLTOS
        hesitou = hesitou or palavras[inicio] in _HESITACOES
        inicio += 1
    resto = palavras[inicio:]
    verbo = bool(resto) and (resto[0] in _VERBOS_DE_TROCA or resto[0] in _VERBOS_DE_ACRESCENTO)
    if (not negada and not verbo) or (artigo and not negada and not hesitou):
        return palavras, False
    if verbo:
        seguinte = 1
        while seguinte < len(resto) and resto[seguinte] in _HESITACOES:
            seguinte += 1
        # "muda um para dois": um "um" sozinho depois do verbo e o artigo.
        if set(resto[1:seguinte]) - _HESITACOES_QUE_SAO_ARTIGOS:
            resto = resto[:1] + resto[seguinte:]
    return resto, negada


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
    """A frase curta soa a "abort"/"cancel"/"cancela". Deterministico."""
    if not palavras or len(palavras) > _PALAVRAS_DO_CANCELAR_APROXIMADO:
        return False
    esqueleto = _esqueleto(palavras)
    if len(esqueleto) < 3:
        return False
    return any(_distancia(esqueleto, alvo) <= _DISTANCIA_DO_CANCELAR_APROXIMADO for alvo in _ESQUELETOS_DE_CANCELAR)


def _juntar_contracoes(palavras: list[str]) -> list[str]:
    """"can t" -> "cant", "don t" -> "dont": o apostrofo sai na normalizacao."""
    juntas: list[str] = []
    for palavra in palavras:
        if palavra == "t" and juntas:
            juntas[-1] += "t"
        else:
            juntas.append(palavra)
    return juntas


def _sem_repeticoes(palavras: list[str]) -> list[str]:
    """"castle castle" -> "castle": a mesma palavra seguida conta uma vez."""
    return [palavra for i, palavra in enumerate(palavras) if i == 0 or palavra != palavras[i - 1]]


def _e_confirmacao(palavras: list[str]) -> bool:
    """A frase inteira e feita so de confirmacoes e cortesias, com pelo menos um "sim".

    Cada palavra (ou par, como "send it" ou "por favor") tem de estar numa
    das listas fechadas; uma palavra a mais ("yes sir", "send it to atlas")
    ou uma hesitacao no meio ("yes uh send it") nunca envia.
    """
    confirma = False
    i = 0
    while i < len(palavras):
        par = " ".join(palavras[i : i + 2])
        if len(palavras) > i + 1 and (par in PALAVRAS_DE_CONFIRMAR or par in PALAVRAS_DE_CORTESIA):
            confirma = confirma or par in PALAVRAS_DE_CONFIRMAR
            i += 2
        elif palavras[i] in PALAVRAS_DE_CONFIRMAR:
            confirma = True
            i += 1
        elif palavras[i] in PALAVRAS_DE_CORTESIA:
            i += 1
        else:
            return False
    return confirma


def _tem_confirmacao(palavras: list[str]) -> bool:
    """A frase tem alguma palavra de confirmar que nao esta negada ("don't send")."""
    return any(
        palavra in _PALAVRAS_DE_CONFIRMAR_NA_MISTURA and (i == 0 or palavras[i - 1] not in _NEGAM_O_VERBO)
        for i, palavra in enumerate(palavras)
    )


def _cancelamento(palavras: list[str]) -> tuple[bool, bool]:
    """(cancela, por aproximacao). Vem antes das correcoes, sem LLM.

    Uma frase que comeca por um verbo de troca ou de acrescento ("change
    castle to docs", "add cancel") nunca e um cancelar: e uma correcao.
    """
    restantes = list(palavras)
    while restantes and restantes[0] in _NEGACOES:
        restantes = restantes[1:]
    if not restantes:
        return True, False  # "no" / "nao" sozinho
    inicio = restantes[1:] if restantes[0] in {"e", "and", "tambem", "also"} else restantes
    if restantes[0] in _VERBOS_DE_TROCA or (inicio and inicio[0] in _VERBOS_DE_ACRESCENTO):
        return False, False
    if restantes[0] in _PALAVRAS_DE_CANCELAR or " ".join(restantes[:2]) in _PARES_DE_CANCELAR:
        return True, False
    return _parece_cancelar(_sem_repeticoes(restantes)), True


def classificar_resposta(texto: str | None) -> tuple[TipoDeResposta, str]:
    """(tipo, texto da edicao) de uma resposta ao recap. Deterministico.

    As hesitacoes (uh, um, hum, ...) e a pontuacao nao contam. So uma frase
    feita toda ela de "sim"/"envia" e cortesias confirma, sem aproximacao
    nenhuma: "Go, yes." e "yes please" enviam, "sim, mas muda X" e uma
    correcao, "acrescenta que e urgente" nunca envia. Cancelar ("abort",
    "cancel") vem antes das correcoes e aceita formas mal ouvidas ("Uh
    castle.", "Can't sell it.", "a board"): cancelar nunca envia nada, por
    isso nunca chega a regra financeira das correcoes. Uma frase que confirma
    e cancela ao mesmo tempo ("yes abort") nao faz nenhuma das duas.
    """
    tipo, edicao, _aproximado = _classificar(texto)
    return tipo, edicao


def _classificar(texto: str | None) -> tuple[TipoDeResposta, str, bool]:
    """(tipo, texto da edicao, cancelar por aproximacao)."""
    limpo = _texto_sem_hesitacoes(sem_palavra_de_ativacao(limpar_texto(texto or "")))
    palavras = _juntar_contracoes(_palavras(limpo, hesitacoes_no_fim=True))
    if not palavras:
        return "outro", "", False
    if _e_confirmacao(palavras):
        return "confirmar", "", False
    cancela, aproximado = _cancelamento(palavras)
    if cancela:
        if _tem_confirmacao(palavras):
            return "outro", "", False  # confirma e cancela ao mesmo tempo: pergunta de novo
        return "cancelar", "", aproximado
    tipo, edicao = _classificar_sem_confirmar(limpo, palavras)
    return tipo, edicao, False


def _classificar_sem_confirmar(limpo: str, palavras: list[str]) -> tuple[TipoDeResposta, str]:
    """Correcao, acrescento ou outro; o cancelar ja foi visto antes."""
    if palavras[0] in _SINS and len(palavras) > 2 and palavras[1] in {"mas", "but"}:
        palavras = palavras[2:]
        limpo = _SIM_MAS_NO_INICIO.sub("", limpo, count=1)
    palavras, negada = _abertura(palavras)
    if not palavras:
        return "outro", ""
    if palavras[0] in _PALAVRAS_DE_CANCELAR or " ".join(palavras[:2]) in _PARES_DE_CANCELAR:
        return "outro", ""  # "yes but cancel": confirma e cancela, pergunta de novo
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
    X to Y", tambem com hesitacoes e ordem solta: "The no change uh um X to
    Y"), ou uma troca sozinha ("change X to Y") quando X ou Y e so um
    nome de projeto, ou quando a frase nao diz nenhum projeto. Uma troca que
    diz o projeto noutro sitio ("change the title to welcome in atlas") e um
    ditado, e um acrescento ("add ...") nunca e uma correcao sem pedido.
    """
    palavras, negada = _abertura(_palavras(texto or ""))
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
    #: Resposta curta na janela de conversa, enviada sem recap.
    sem_recap: bool = False
    #: O projeto nao foi dito: e o ultimo usado. A resposta diz qual.
    projeto_assumido: bool = False


def _pedido_de_leitura(interpretacao: Interpretacao, projeto_assumido: bool = False) -> Pedido:
    """O pedido de uma leitura (estado, relatorio): so a intencao e o projeto."""
    return Pedido(
        interpretacao.intencao, interpretacao.projeto, "", interpretacao.detalhe, projeto_assumido=projeto_assumido
    )


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
    #: O projeto nao foi dito: e o ultimo usado, e o recap diz qual.
    projeto_assumido: bool = False


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
    "lembrar_facto": "Lembrar",
    "esquecer_facto": "Esquecer",
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
    "lembrar_facto": "Remember",
    "esquecer_facto": "Forget",
}
_SEM_PROJETO = {
    "pt": {"conversa": "Responder ao Claude", "marcador": "<projeto>"},
    "en": {"conversa": "Reply to Claude", "marcador": "<project>"},
}

_FRASES = {
    "pt": {
        "enviar": "envio?",
        "confirmar": "confirmas?",
        "longo": "o texto inteiro está no ecrã",
        "para": "para o {p}",
        "ainda_para": "outra vez para o {p}",
        "recusado": "Isso não faço por voz: pedidos de dinheiro ou de bolsa ficam de fora.",
        "nao_percebi": "Não percebi. Repete, por favor.",
        "cancelado": "Cancelado, não enviei nada.",
        "expirado": "Sem resposta, cancelei. Não enviei nada.",
        "de_novo": "Diz sim para enviar, ou aborta.",
        "correcao_falhou": "Não consegui aplicar essa correção; o pedido fica igual. {pergunta}",
        "falhou": "Não consegui fazer isso.",
        "nada_para_corrigir": "Não há nenhum pedido à espera para corrigir.",
        "ecra_percebi": "Percebi: {intencao}{projeto}",
        "ecra_enviar": "Texto a enviar:",
        "ecra_ajuda": 'Diz "sim" para enviar, ou "aborta" ("cancela" também serve). Para corrigir: "não, muda X para Y" ou "acrescenta ...".',
        "ecra_falta_projeto": "Falta o projeto: diz o nome do projeto, ou \"aborta\".",
        "ecra_conhecidos": "Projetos conhecidos: {nomes}",
        "guardar": "guardo?",
        "apagar": "apago?",
        "ecra_projeto_assumido": "(projeto não dito: é o último usado; \"não, para o X\" troca)",
        "ecra_guardar": "Facto a guardar:",
        "ecra_apagar": "Facto a apagar:",
        "ecra_ajuda_memoria": 'Diz "sim" para confirmar, ou "aborta" ("cancela" também serve).',
        "cancelado_memoria": "Cancelado, não mudei nada na memória.",
        "expirado_memoria": "Sem resposta, cancelei. Não mudei nada na memória.",
        "de_novo_memoria": "Diz sim para confirmar, ou aborta.",
    },
    "en": {
        "enviar": "send it?",
        "confirmar": "go ahead?",
        "longo": "the full text is on screen",
        "para": "for {p}",
        "ainda_para": "still for {p}",
        "recusado": (
            "Sorry, I don't do money and trading requests by voice.",
            "Money and trading requests are off limits for me.",
            "I never do money and trading requests by voice.",
        ),
        "nao_percebi": (
            "Sorry, I didn't catch that. Could you say it again?",
            "I missed that, sorry. Once more?",
            "Sorry, could you repeat that?",
        ),
        "cancelado": ("Cancelled, nothing was sent.", "Okay, dropped it. Nothing was sent.", "Fine, I won't send it."),
        "expirado": (
            "No answer, so I cancelled. Nothing was sent.",
            "I didn't hear back, so I dropped it. Nothing was sent.",
        ),
        "de_novo": ("Say yes to send, or abort.", "Just yes to send it, or abort.", "Yes to send, or abort to drop it."),
        "correcao_falhou": (
            "I couldn't apply that change; the request stays the same. {pergunta}",
            "That change didn't work, so the request is unchanged. {pergunta}",
        ),
        "falhou": ("Sorry, I couldn't do that.", "That didn't work, sorry."),
        "nada_para_corrigir": ("There's nothing waiting to correct.", "Nothing's pending, so there's nothing to correct."),
        "ecra_percebi": "Understood: {intencao}{projeto}",
        "ecra_enviar": "Text to send:",
        "ecra_ajuda": 'Say "yes" to send, or "abort" ("cancel" works too). To correct: "no, change X to Y" or "add ...".',
        "ecra_falta_projeto": "Missing project: say the project name, or \"abort\".",
        "ecra_conhecidos": "Known projects: {nomes}",
        "guardar": "save it?",
        "apagar": "delete it?",
        "ecra_projeto_assumido": "(project not said: it is the last one used; \"no, for X\" switches)",
        "ecra_guardar": "Fact to save:",
        "ecra_apagar": "Fact to delete:",
        "ecra_ajuda_memoria": 'Say "yes" to confirm, or "abort" ("cancel" works too).',
        "cancelado_memoria": ("Cancelled, my memory is unchanged.", "Okay, I left my notebook as it was."),
        "expirado_memoria": (
            "No answer, so I cancelled. My memory is unchanged.",
            "I didn't hear back, so my notebook stays as it was.",
        ),
        "de_novo_memoria": ("Say yes to confirm, or abort.", "Just yes to confirm, or abort."),
    },
}

#: A pergunta do interprete quando a frase nao disse nenhum projeto (sem
#: candidatos): so aqui o projeto pode ser assumido.
_PERGUNTAS_GENERICAS = frozenset({None, pergunta_de_projeto((), "en"), pergunta_de_projeto((), "pt")})

#: O que pode vir antes do nome na troca do projeto assumido: "no, for forja",
#: "nao, para o forja", "no, forja", "in forja".
_ANTES_DA_TROCA = _NEGACOES | frozenset(
    {"for", "to", "in", "on", "the", "para", "pro", "no", "na", "ao", "em", "o", "a"}
)

#: Fim de frase dentro da pergunta de projeto: na voz vira virgula, para a
#: pergunta ser uma so frase.
_FIM_DE_FRASE = re.compile(r"\s*[.!?…;]+(?:\s+|$)")


def _numa_frase(texto: str) -> str:
    return _FIM_DE_FRASE.sub(", ", texto.strip()).strip().rstrip(",").strip()


def contar_frases(fala: str) -> int:
    """Quantas frases tem um texto falado (fim de frase seguido de espaco ou fim)."""
    return len([parte for parte in re.split(r"(?<=[.!?…])\s+", fala.strip()) if parte.strip()])


def _com_projeto(modelo: str, projeto: str | None, lingua: str) -> str:
    return modelo.format(p=projeto if projeto else _SEM_PROJETO[lingua]["marcador"])


def _pergunta_pelo_projeto(interpretacao: Interpretacao, conhecidos: tuple[str, ...], lingua: str) -> str:
    """A pergunta a dizer quando falta o projeto, numa so frase.

    Projetos ditos em alternativa ("in X or Y") ficam na pergunta do
    interprete; sem candidatos, a pergunta diz os projetos conhecidos.
    """
    if interpretacao.pergunta not in _PERGUNTAS_GENERICAS:
        pergunta = _numa_frase(interpretacao.pergunta) + "?"
    else:
        pergunta = pergunta_dos_projetos_conhecidos(conhecidos, lingua)
    return pergunta[0].upper() + pergunta[1:]


def _sem_pontuacao_no_fim(texto: str) -> str:
    return texto.strip().rstrip(" ,;:.!?…").strip()


def _tema(prompt: str) -> str:
    """O tema de um prompt longo em poucas palavras. Deterministico.

    A primeira frase, se for curta; senao a primeira oracao (ate a primeira
    virgula, ponto e virgula ou dois pontos), se for curta; senao as primeiras
    palavras, sem acabar numa palavra de ligacao ("Fix the login and").
    """
    primeira = _sem_pontuacao_no_fim(re.split(r"(?<=[.!?…])\s+", prompt.strip(), maxsplit=1)[0])
    if len(primeira.split()) <= PALAVRAS_DO_TEMA:
        return primeira
    oracao = _sem_pontuacao_no_fim(re.split(r"[,;:]\s|\s[-–—]\s", primeira, maxsplit=1)[0])
    if 3 <= len(oracao.split()) <= PALAVRAS_DO_TEMA:
        return oracao
    palavras = primeira.split()[:PALAVRAS_DO_TEMA_CORTADO]
    while len(palavras) > 1 and _normalizar(palavras[-1]) in _PALAVRAS_DE_LIGACAO:
        palavras.pop()
    return _sem_pontuacao_no_fim(" ".join(palavras))


def prompt_curto(prompt: str) -> bool:
    """O prompt e curto o bastante para ser dito inteiro no recap."""
    return len(prompt.split()) <= PALAVRAS_DITAS and len(prompt) <= CARACTERES_DITOS


def compor_recap(
    numero: int,
    interpretacao: Interpretacao,
    lingua: str,
    conhecidos: tuple[str, ...] = (),
    *,
    projeto_assumido: bool = False,
) -> Recap:
    """O recap de um pedido com efeito: fala (uma so frase) e ecra (tudo).

    A fala acaba numa pergunta leve: "Add tests to the login page, for atlas
    - send it?"; um prompt curto de varias frases diz o projeto antes delas
    ("For atlas: Fix the login. Don't change anything - send it?"). Um prompt
    longo e dito pelo tema; o texto exato a enviar esta
    sempre no ecra. `conhecidos` sao os nomes dos projetos, os usados ha menos
    tempo primeiro: quando falta o projeto, a pergunta diz os primeiros e o
    ecra mostra-os todos. Com `projeto_assumido`, o projeto nao foi dito e e o
    ultimo usado: a fala diz-o ("still for atlas") e o ecra tambem.
    """
    frases = _FRASES[lingua]
    acoes = _ACOES_EN if lingua == "en" else _ACOES_PT
    intencao, projeto = interpretacao.intencao, interpretacao.projeto
    # O texto mostrado e o texto enviado sao o mesmo objeto, numa so linha.
    prompt = limpar_texto(interpretacao.prompt) if intencao in _INTENCOES_COM_TEXTO else ""
    memoria = intencao in INTENCOES_DA_MEMORIA
    falta_projeto = intencao in INTENCOES_COM_PROJETO and projeto is None
    projeto_assumido = projeto_assumido and not falta_projeto
    pedido = Pedido(intencao, projeto, prompt, interpretacao.detalhe, projeto_assumido=projeto_assumido)

    if intencao == "conversa" and projeto is None:
        acao = _SEM_PROJETO[lingua]["conversa"]
    else:
        acao = _com_projeto(acoes[intencao], projeto, lingua)

    if falta_projeto:
        pergunta = _pergunta_pelo_projeto(interpretacao, conhecidos, lingua)
        fala = pergunta
    else:
        if memoria:
            leve = frases["guardar" if intencao == INTENCAO_LEMBRAR_FACTO else "apagar"]
        else:
            leve = frases["enviar"] if prompt else frases["confirmar"]
        pergunta = leve[0].upper() + leve[1:]
        if not prompt:
            corpo = acao
        else:
            curto = prompt_curto(prompt)
            dito = _sem_pontuacao_no_fim(prompt) if curto else _tema(prompt)
            if intencao == "ditar_prompt":
                para = frases["ainda_para" if projeto_assumido else "para"].format(p=projeto)
                if curto and contar_frases(prompt) > 1:
                    # As frases do prompt ficam como estao: o projeto vem antes.
                    corpo = f"{para}: {dito}"
                else:
                    corpo = f"{dito}, {para}"
            else:
                corpo = f"{acao}: {dito}"
            if not curto:
                corpo += f", {frases['longo']}"
        corpo = corpo[0].upper() + corpo[1:]
        fala = f"{corpo} - {leve}"

    nome_da_intencao = intencao.replace("_", " ")
    linhas = [
        frases["ecra_percebi"].format(
            intencao=nome_da_intencao, projeto=f" | projeto: {projeto}" if projeto else ""
        )
    ]
    if projeto_assumido:
        linhas.append(frases["ecra_projeto_assumido"])
    if prompt and memoria:
        linhas += [frases["ecra_guardar" if intencao == INTENCAO_LEMBRAR_FACTO else "ecra_apagar"], prompt]
    elif prompt:
        linhas += [frases["ecra_enviar"], prompt]
    else:
        linhas.append(acao)
    if falta_projeto:
        linhas.append(frases["ecra_falta_projeto"])
        if conhecidos:
            linhas.append(frases["ecra_conhecidos"].format(nomes=", ".join(conhecidos)))
    else:
        linhas.append(frases["ecra_ajuda_memoria" if memoria else "ecra_ajuda"])
    return Recap(numero, pedido, fala, "\n".join(linhas), pergunta, falta_projeto, projeto_assumido)


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
        persona: Persona | None = None,
        variantes: Variantes | None = None,
        ultimo_projeto_s: float | None = None,
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
        recente = interprete.config.interprete.ultimo_projeto_min * 60 if ultimo_projeto_s is None else ultimo_projeto_s
        if isinstance(recente, bool) or not isinstance(recente, (int, float)) or recente < 0:
            raise ValueError(f"tempo do ultimo projeto invalido: {recente!r}")
        #: Ate quanto tempo depois do ultimo pedido num projeto um ditado sem
        #: projeto o assume; 0 desliga.
        self.ultimo_projeto_s = float(recente)
        self._relogio = relogio
        self._trinco = threading.Lock()
        self._pendente: Interpretacao | None = None
        #: Quem propos o pedido pendente (`propor_do_cerebro`); None nos outros.
        self._dono: object | None = None
        self._recap: Recap | None = None
        self._prazo = 0.0
        self._apresentado_em = 0.0
        self._ocupado = False
        self._falhas = 0
        self._numero = 0
        #: Projetos a que um pedido foi feito nesta sessao, o mais recente primeiro.
        self._usados: list[str] = []
        #: O ultimo projeto a que um pedido foi executado e quando (no relogio).
        self._ultimo_projeto: tuple[str, float] | None = None
        #: Chamado com cada recap apresentado, antes de ser dito (a bolinha
        #: mostra o texto a enviar). Uma falha aqui nunca para o recap.
        self.ao_propor: Callable[[Recap], object] | None = None
        #: Escreve o "nao percebi" com o LLM local; None: so as frases fixas.
        #: Nunca escreve um recap, a resposta a um recap nem uma recusa.
        self.persona = persona
        self._variantes = variantes or Variantes()
        #: Chamado com (dono, desfecho) quando um pedido com dono deixa de estar
        #: pendente, depois do que foi dito. Uma falha aqui nunca para o dialogo.
        self.ao_fechar: Callable[[object, Desfecho], object] | None = None
        #: Chamado antes de cada correcao: False faz so a correcao a letra, sem
        #: nenhum pedido ao LLM local. None: a correcao usa sempre o LLM.
        self.llm_na_correcao: Callable[[], bool] | None = None

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

    def pendente_de(self, dono: object) -> bool:
        """O pedido pendente foi proposto por `dono`."""
        with self._trinco:
            return self._pendente is not None and self._dono is dono

    def prazo_restante(self) -> float | None:
        with self._trinco:
            if self._pendente is None:
                return None
            if self._ocupado:
                return self.limite_s
            return max(0.0, self._prazo - self._relogio())

    def _projetos_por_uso(self) -> tuple[str, ...]:
        """Os nomes dos projetos: os usados nesta sessao primeiro, depois os outros pela ordem da config."""
        nomes = [projeto.nome for projeto in self.interprete.config.projetos]
        usados = [nome for nome in self._usados if nome in nomes]
        return tuple(usados + [nome for nome in nomes if nome not in usados])

    def projetos_por_uso(self) -> tuple[str, ...]:
        with self._trinco:
            return self._projetos_por_uso()

    def ultimo_projeto(self) -> str | None:
        """O ultimo projeto usado, se foi ha menos de `ultimo_projeto_s` e ainda existe."""
        with self._trinco:
            ultimo = self._ultimo_projeto
        if ultimo is None or self.ultimo_projeto_s <= 0:
            return None
        nome, quando = ultimo
        if self._relogio() - quando > self.ultimo_projeto_s:
            return None
        if nome not in {projeto.nome for projeto in self.interprete.config.projetos}:
            return None
        return nome

    def _projeto_a_assumir(self, interpretacao: Interpretacao) -> str | None:
        """O projeto a assumir num ditado ou numa leitura sem projeto, ou None.

        So quando a frase nao diz nenhum projeto (nem mal ouvido, nem em
        alternativa), o interprete nao ficou com uma pergunta propria, o
        texto nao e um pedido de dinheiro e houve um pedido num projeto ha
        pouco tempo. O ditado continua a precisar do "sim" ao recap.
        """
        if interpretacao.intencao not in _INTENCOES_QUE_ASSUMEM or interpretacao.projeto is not None:
            return None
        if interpretacao.so_confirmacao or interpretacao.pergunta not in _PERGUNTAS_GENERICAS:
            return None
        nomes = tuple(projeto.nome for projeto in self.interprete.config.projetos)
        # A palavra de ativacao ("hey jarvis") nao e o projeto jarvis.
        texto = sem_palavra_de_ativacao(limpar_texto(interpretacao.texto))
        if projetos_mencionados(texto, nomes) or projetos_em_alternativa(texto, nomes):
            return None
        if self._financeiro(interpretacao.texto) or self._financeiro(interpretacao.prompt):
            return None
        return self.ultimo_projeto()

    def _texto(self, chave: str, **valores: str) -> str:
        """Uma das variantes da frase, nunca a mesma que da ultima vez."""
        return self._variantes.escolher(chave, _FRASES[self.lingua][chave]).format(**valores)

    def _nao_percebi(self, frase_ouvida: str | None) -> str:
        """O "nao percebi" escrito pelo LLM local, ou a variante fixa (prazo, falha ou filtro)."""
        if self.persona is not None:
            try:
                gerada = self.persona.frase(CASO_NAO_PERCEBI, frase_ouvida)
            except Exception:  # noqa: BLE001 - a persona nunca impede a resposta
                gerada = None
            if gerada:
                return gerada
        return self._texto("nao_percebi")

    def _texto_do(self, recap: Recap | None, chave: str) -> str:
        """A frase `chave`, na forma da memoria quando o recap e de um facto."""
        if recap is not None and recap.pedido.intencao in INTENCOES_DA_MEMORIA:
            return self._texto(f"{chave}_memoria")
        return self._texto(chave)

    def _limpar(self) -> object | None:
        """Tira o pedido pendente (com o trinco); devolve o dono dele, ou None."""
        dono, self._dono = self._dono, None
        self._pendente = None
        self._recap = None
        self._ocupado = False
        self._falhas = 0
        return dono

    def _fechado(self, dono: object | None, desfecho: Desfecho) -> Desfecho:
        """Diz a quem propos o pedido (`ao_fechar`) como ele acabou; devolve o desfecho."""
        if dono is not None and self.ao_fechar is not None:
            try:
                self.ao_fechar(dono, desfecho)
            except Exception:  # noqa: BLE001 - um aviso falhado nunca para o dialogo
                pass
        return desfecho

    # -- entrada de uma frase nova

    def iniciar(self, interpretacao: Interpretacao) -> Desfecho:
        """Trata uma frase interpretada: executa, recusa ou fica pendente."""
        with self._trinco:
            substituido = self._pendente is not None
            dono = self._limpar()
        if substituido:
            self._mostrar("confirmacao | pedido anterior cancelado por um pedido novo; nada foi enviado")
            self._fechado(dono, Desfecho("cancelado", "substituido por um pedido novo"))

        if interpretacao.intencao == INTENCAO_RECUSADA:
            self._falar(self._texto("recusado"))
            return Desfecho("recusado", interpretacao.motivo)
        if interpretacao.pode_dispensar_confirmacao:
            pedido = Pedido(interpretacao.intencao, None, "", interpretacao.detalhe)
            return self._correr(pedido, None, "sem efeito: dispensa confirmacao")
        pergunta = limpar_texto(interpretacao.prompt)
        if interpretacao.intencao == INTENCAO_PERGUNTA_GERAL and pergunta and not interpretacao.so_confirmacao:
            pedido = Pedido(INTENCAO_PERGUNTA_GERAL, None, pergunta)
            return self._correr(pedido, None, "pergunta geral: so le, dispensa confirmacao")
        assumido = self._projeto_a_assumir(interpretacao)
        if assumido is not None:
            interpretacao = replace(interpretacao, projeto=assumido, pergunta=None)
            self._mostrar(f"confirmacao | projeto nao dito: assumido o ultimo usado, {assumido}")
        if interpretacao.intencao in INTENCOES_SO_DE_LEITURA and interpretacao.projeto and not interpretacao.so_confirmacao:
            pedido = _pedido_de_leitura(interpretacao, projeto_assumido=assumido is not None)
            motivo = "so leitura: dispensa confirmacao" + ("; projeto assumido" if assumido else "")
            return self._correr(pedido, None, motivo)
        sem_texto = interpretacao.intencao in _INTENCOES_COM_TEXTO and not limpar_texto(interpretacao.prompt)
        if interpretacao.so_confirmacao or interpretacao.intencao not in _INTENCOES_COM_RECAP or sem_texto:
            if interpretacao.texto:
                self._mostrar(f"confirmacao | nao percebi: {interpretacao.texto}")
            self._falar(self._nao_percebi(interpretacao.texto))
            return Desfecho("nao_percebido", interpretacao.motivo)
        return self._propor(interpretacao, projeto_assumido=assumido is not None)

    def enviar_sem_recap(self, interpretacao: Interpretacao) -> Desfecho:
        """Envia logo uma resposta curta a uma pergunta do Claude, sem recap.

        So para quem trata a janela de conversa. Uma resposta que nao e de
        conversa, sem projeto ou comprida vai para `iniciar` (recap e "sim");
        a regra financeira vem antes de tudo, ao texto ouvido e ao que se envia.
        """
        texto = limpar_texto(interpretacao.prompt)
        financeiro = self._financeiro(interpretacao.texto) or self._financeiro(texto)
        if financeiro or interpretacao.intencao == INTENCAO_RECUSADA:
            with self._trinco:
                dono = self._limpar()
            self._mostrar("confirmacao | resposta recusada (pedido financeiro); nada foi enviado")
            self._falar(self._texto("recusado"))
            self._fechado(dono, Desfecho("cancelado", "substituido por um pedido novo"))
            return Desfecho("recusado", interpretacao.motivo if not financeiro else "pedido financeiro na conversa")
        if interpretacao.intencao != "conversa" or not interpretacao.projeto or not e_resposta_curta(texto):
            return self.iniciar(interpretacao)
        with self._trinco:
            substituido = self._pendente is not None
            dono = self._limpar()
        if substituido:
            self._mostrar("confirmacao | pedido anterior cancelado por um pedido novo; nada foi enviado")
            self._fechado(dono, Desfecho("cancelado", "substituido por um pedido novo"))
        pedido = Pedido(interpretacao.intencao, interpretacao.projeto, texto, sem_recap=True)
        return self._correr(pedido, None, "resposta curta na janela de conversa: enviada sem recap")

    def propor_do_cerebro(
        self, interpretacao: Interpretacao, *, dono: object, projeto_assumido: bool = False
    ) -> Desfecho:
        """O recap de um pedido com efeito pedido pelo cerebro. Nunca executa nada.

        Fica pendente como qualquer outro pedido: so um "sim" falado ao jarvis
        o executa, e o mesmo recap, prazo, respostas e correcoes valem. Com
        outro pedido pendente devolve "ocupado" e nada muda; um pedido de
        dinheiro e recusado sem recap. `projeto_assumido`: o projeto nao foi
        dito pelo utilizador, e o recap diz qual e ("still for atlas").
        `dono` volta em `ao_fechar` quando o pedido deixa de estar pendente.
        """
        intencao = interpretacao.intencao
        sem_projeto = intencao in INTENCOES_COM_PROJETO and not interpretacao.projeto
        sem_texto = intencao in _INTENCOES_COM_TEXTO and not limpar_texto(interpretacao.prompt)
        if intencao not in _INTENCOES_COM_RECAP or sem_projeto or sem_texto or interpretacao.so_confirmacao:
            self._mostrar(f"confirmacao | pedido do cerebro invalido ({intencao}); nada foi enviado")
            return Desfecho("nao_percebido", "pedido do cerebro sem intencao, projeto ou texto validos")
        with self._trinco:
            if self._pendente is not None:
                return Desfecho("ocupado", "ja ha um pedido por confirmar", recap=self._recap)
        return self._propor(
            interpretacao, projeto_assumido=projeto_assumido and bool(interpretacao.projeto), dono=dono, so_se_livre=True
        )

    def _propor(
        self,
        interpretacao: Interpretacao,
        fala: str | None = None,
        *,
        projeto_assumido: bool = False,
        dono: object | None = None,
        so_se_livre: bool = False,
    ) -> Desfecho:
        """Diz o recap e deixa o pedido pendente.

        `dono` passa a ser o do pedido (senao fica o que ja era, numa
        correcao); com `so_se_livre`, outro pedido pendente devolve "ocupado"
        e nada muda.
        """
        if self._financeiro(interpretacao.texto) or self._financeiro(interpretacao.prompt):
            # Um pedido de dinheiro nunca chega a um recap, venha de onde vier.
            with self._trinco:
                if so_se_livre and self._pendente is not None:
                    return Desfecho("ocupado", "ja ha um pedido por confirmar", recap=self._recap)
                anterior = self._limpar()
            self._mostrar("confirmacao | pedido financeiro: recusado antes do recap; nada foi enviado")
            self._falar(self._texto("recusado"))
            desfecho = Desfecho("recusado", "pedido financeiro: nunca e recapitulado")
            return self._fechado(anterior, self._fechado(dono, desfecho))
        with self._trinco:
            if so_se_livre and self._pendente is not None:
                return Desfecho("ocupado", "ja ha um pedido por confirmar", recap=self._recap)
            if dono is not None:
                self._dono = dono
            self._numero += 1
            recap = compor_recap(
                self._numero,
                interpretacao,
                self.lingua,
                self._projetos_por_uso(),
                projeto_assumido=projeto_assumido,
            )
            self._pendente = interpretacao
            self._recap = None
            self._ocupado = True
        self._mostrar(recap.ecra)
        if self.ao_propor is not None:
            try:
                self.ao_propor(recap)
            except Exception:  # noqa: BLE001 - um extra de ecra nunca para o recap
                pass
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
                dono = self._dono
                horas = self._horas_pedidas(texto)
                tipo, edicao, aproximado = _classificar(texto)
                projeto_dito = recap.falta_projeto and self._projeto_dito(texto) is not None
                troca = self._troca_de_projeto(texto) if recap.projeto_assumido else None
                if horas is not None:
                    # So le as horas: responde e o pedido continua a espera.
                    self._ocupado = True
                    acao = "horas"
                elif tipo == "confirmar" and not recap.falta_projeto:
                    self._limpar()
                    acao = "executar"
                elif tipo == "cancelar" and not (aproximado and (projeto_dito or troca)):
                    self._limpar()
                    acao = "cancelar"
                elif (projeto_dito or troca) and self._financeiro(texto):
                    # O projeto dito com um pedido de dinheiro: a regra financeira vem antes.
                    self._limpar()
                    acao = "recusar"
                elif projeto_dito:
                    # "no orbita" e a resposta a "qual projeto", nao um "no".
                    self._ocupado = True
                    acao = "projeto"
                elif troca:
                    # "no, for forja": o projeto assumido estava errado; recap novo, nada enviado.
                    self._ocupado = True
                    acao = "trocar"
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
            return self._fechado(dono, self._correr(recap.pedido, recap, "confirmado"))
        if acao == "cancelar":
            self._mostrar("confirmacao | cancelado; nada foi enviado")
            self._falar(self._texto_do(recap, "cancelado"))
            return self._fechado(dono, Desfecho("cancelado", "cancelado pelo utilizador", recap=recap))
        if acao == "recusar":
            self._mostrar("confirmacao | resposta recusada (pedido financeiro); nada foi enviado")
            self._falar(self._texto("recusado"))
            return self._fechado(dono, Desfecho("recusado", "pedido financeiro na resposta ao projeto", recap=recap))
        if acao == "desistir":
            self._mostrar("confirmacao | cancelado depois de respostas que nao percebi; nada foi enviado")
            self._falar(self._texto_do(recap, "cancelado"))
            desfecho = Desfecho("cancelado", f"{TENTATIVAS} respostas sem confirmar nem corrigir", recap=recap)
            return self._fechado(dono, desfecho)
        if acao == "de_novo":
            try:
                self._falar(recap.pergunta if recap.falta_projeto else self._texto_do(recap, "de_novo"))
            finally:
                with self._trinco:
                    if self._pendente is pendente:
                        self._ocupado = False
                        self._prazo = self._relogio() + self.limite_s
            return Desfecho("pendente", "resposta nao percebida", recap=recap)
        if acao == "projeto":
            projeto = self._projeto_dito(texto)
            completa = replace(pendente, projeto=projeto, pergunta=None)
            if completa.intencao in INTENCOES_SO_DE_LEITURA:
                # Ver o estado ou ler o relatorio: corre logo que o projeto e dito.
                with self._trinco:
                    if self._pendente is not pendente:
                        return Desfecho("cancelado", "o pedido foi cancelado antes de correr")
                    dono = self._limpar()
                return self._fechado(dono, self._correr(_pedido_de_leitura(completa), None, "so leitura: projeto dito"))
            return self._propor(completa)
        if acao == "trocar":
            self._mostrar(f"confirmacao | projeto trocado: {recap.pedido.projeto} -> {troca}; nada foi enviado")
            return self._propor(replace(pendente, projeto=troca, pergunta=None))
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

    def _financeiro(self, texto: str | None) -> bool:
        nomes = tuple(projeto.nome for projeto in self.interprete.config.projetos)
        return pedido_financeiro(limpar_texto(texto or ""), nomes) is not None

    def _projeto_dito(self, texto: str | None) -> str | None:
        nomes = tuple(projeto.nome for projeto in self.interprete.config.projetos)
        limpo = limpar_texto(texto or "")
        ditos = projetos_mencionados(limpo, nomes)
        if len(ditos) != 1 or projetos_em_alternativa(limpo, nomes):
            return None
        return ditos[0]

    def _troca_de_projeto(self, texto: str | None) -> str | None:
        """O projeto de "no, for forja" / "nao, para o forja": so o nome, sem mais nada. Deterministico.

        Antes do nome so podem vir negacoes, preposicoes e artigos; uma
        palavra a mais ("yes, for forja", "for forja and delete it") nunca e
        uma troca. O nome tem de ser um so projeto conhecido.
        """
        palavras = _juntar_contracoes(_palavras(texto or "", hesitacoes_no_fim=True))
        inicio = 0
        while inicio < len(palavras) and palavras[inicio] in _ANTES_DA_TROCA:
            inicio += 1
        resto = " ".join(palavras[inicio:])
        if not resto:
            return None
        nomes = tuple(projeto.nome for projeto in self.interprete.config.projetos)
        return _so_o_projeto(resto, nomes)

    def _aplicar_correcao(
        self, pendente: Interpretacao, recap: Recap, edicao: str, tipo: str
    ) -> Desfecho:
        if pendente.intencao in INTENCOES_DA_MEMORIA:
            # Um facto nao se reescreve pelo LLM: diz-se outra vez, inteiro.
            self._mostrar("confirmacao | correcao nao aplicada: um facto diz-se outra vez, inteiro")
            return self._propor(pendente, fala=self._texto("correcao_falhou", pergunta=recap.pergunta))
        try:
            com_llm = self.llm_na_correcao() if self.llm_na_correcao is not None else True
            nova = self.interprete.corrigir(pendente, edicao, tipo, com_llm=com_llm)
        except Exception as erro:  # noqa: BLE001 - uma falha nunca envia nada
            nova = replace(pendente, intencao="desconhecido", motivo=f"a correcao falhou: {erro!r}")
        with self._trinco:
            if self._pendente is not pendente:
                return Desfecho("cancelado", "o pedido foi cancelado durante a correcao")
        if nova.intencao == INTENCAO_RECUSADA:
            with self._trinco:
                dono = self._limpar()
            self._mostrar("confirmacao | correcao recusada (pedido financeiro); nada foi enviado")
            self._falar(self._texto("recusado"))
            return self._fechado(dono, Desfecho("recusado", nova.motivo, recap=recap))
        if nova.intencao not in INTENCOES_COM_EFEITO or nova.so_confirmacao:
            self._mostrar(f"confirmacao | correcao nao aplicada ({nova.motivo})")
            return self._propor(
                pendente,
                fala=self._texto("correcao_falhou", pergunta=recap.pergunta),
                projeto_assumido=recap.projeto_assumido,
            )
        # O projeto continua assumido enquanto a correcao nao o muda.
        return self._propor(nova, projeto_assumido=recap.projeto_assumido and nova.projeto == recap.pedido.projeto)

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
            dono = self._limpar()
        self._mostrar(f"confirmacao | sem resposta em {self.limite_s:g} s: cancelado; nada foi enviado")
        self._falar(self._texto_do(recap, "expirado"))
        return self._fechado(dono, Desfecho("expirado", f"sem resposta em {self.limite_s:g} s", recap=recap))

    def cancelar(self, motivo: str) -> Desfecho | None:
        """Cancela em silencio o pedido pendente (por exemplo ao adormecer)."""
        with self._trinco:
            if self._pendente is None:
                return None
            recap = self._recap
            dono = self._limpar()
        self._mostrar(f"confirmacao | cancelado ({motivo}); nada foi enviado")
        return self._fechado(dono, Desfecho("cancelado", motivo, recap=recap))

    def _correr(self, pedido: Pedido, recap: Recap | None, motivo: str) -> Desfecho:
        try:
            resultado = self._executar(pedido)
        except Exception as erro:  # noqa: BLE001 - o erro e dito, nunca derruba o dialogo
            self._mostrar(f"confirmacao | {pedido.intencao} falhou: {erro!r}")
            self._falar(self._texto("falhou"))
            return Desfecho("falhou", f"{motivo}; o executor falhou: {erro!r}", pedido, recap)
        if pedido.projeto:
            with self._trinco:
                self._usados = [pedido.projeto, *(nome for nome in self._usados if nome != pedido.projeto)]
                self._ultimo_projeto = (pedido.projeto, self._relogio())
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
