r"""Decisao da lingua de uma frase, restrita a {pt, en}.

Este modulo NAO transcreve nada e NAO abre modelo nenhum: recebe as
probabilidades por lingua que o `faster_whisper` ja devolveu no MESMO
`transcribe()` (`info.all_language_probs`) e aplica-lhes a regra do produto.

ESTADO DE HOJE, a ler antes do resto: **a deteccao esta
DESLIGADA no produto**. Foi ligada nos dois caminhos e medida com um A/B
controlado (os mesmos 20 WAV transcritos com `language="pt"` e com
`language=None`); o acerto de intencao em PORTUGUES desceu — 21/40 linhas
certas com lingua fixa contra 20/40 com deteccao — e a regra era reverter se
o portugues descesse. O produto voltou a
`language="pt"` (`LINGUA_FIXA_DO_PRODUTO`, abaixo) e o que corre em producao e
`lingua_fixada()`. Tudo o que esta a seguir continua vivo, testado e a um
argumento de distancia (`lingua_fixa=None`, `--lingua auto`), para se poder
voltar a medir com voz inglesa real ou com modelo maior.

A regra, em tres linhas:

  1. **Argmax restrito a {pt, en}.** So as entradas `"pt"` e `"en"` da lista
     contam. Uma terceira lingua com a probabilidade mais alta (o Whisper
     conhece ~100) nunca entra na escolha do PRODUTO — a que vai para o log,
     para a coluna da evidencia e para qualquer decisao do encaminhador.

     LIMITE, escrito aqui porque uma versao anterior dizia o contrario:
     quando o argmax livre cai fora de {pt, en}, foi ESSA
     terceira lingua que DESCODIFICOU o audio. O parametro `language=` da API
     publica do faster-whisper aceita um codigo unico, nao uma lista de
     candidatas, por isso `language=None` deixa a
     biblioteca decidir livremente com que lingua decodifica
     (`faster_whisper/transcribe.py:880-904`) e a restricao so e possivel em
     pos-processamento — e isso e o que este modulo faz. A frase segue o
     caminho normal, nunca e descartada nem re-transcrita, e fica MARCADA
     `lingua-terceira` (pontos 2 e 4).
  2. **Limiar 0,5**, o default da propria biblioteca
     (`WhisperModel.transcribe(language_detection_threshold=0.5)`): se a lingua
     escolhida nao passar o limiar, a deteccao `hesitou`. Nao ha terceiro
     estado e nao ha "lingua desconhecida": `lingua` e SEMPRE `"pt"` ou
     `"en"`, porque o produto tem sempre de responder alguma coisa.
  3. **Hesitar nao trava nada.** A lingua detetada nunca e a razao de uma frase
     valida nao ser reconhecida: o encaminhamento casa a frase contra AS
     DUAS listas brancas (`jarvis/router.py`), com ou sem hesitacao, hoje e
     enquanto a regra das duas listas existir. O que este modulo entrega serve
     para o LOG e para a lingua da resposta — nunca para escolher lista.

Porque ha DUAS entradas:

  * `decidir_lingua(all_language_probs)` — o caminho dos scripts
    (`scripts/transcrever_ficheiro.py`), onde temos o objeto `info` completo do
    faster-whisper e portanto a lista inteira de probabilidades;
  * `decidir_lingua_do_top1(lingua, probabilidade)` — o caminho vivo
    (`jarvis/app.py` -> RealtimeSTT), onde **so existe o top-1**: o
    RealtimeSTT 0.3.104 le `info.language`/`info.language_probability` em
    `audio_recorder.py:1533-1534` e deita fora o resto do `info`, incluindo o
    `all_language_probs`, antes de o devolver a quem chamou `text()`. O caminho
    vivo fica com o que o RealtimeSTT expoe, sem monkeypatch a biblioteca.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

#: As unicas duas linguas que podem sair daqui (as mesmas cinco accoes
#: nas duas linguas, zero accoes novas).
LINGUAS_RESTRITAS = ("pt", "en")

#: A lingua por omissao do PRODUTO: quando a
#: deteccao nao da nada de util, responde-se em portugues e o log diz porque.
LINGUA_POR_OMISSAO = "pt"

#: A lingua com que o produto TRANSCREVE, hoje.
#:
#: A deteccao automatica (`language=None`) foi ligada nos dois caminhos e
#: medida. O A/B CONTROLADO — os mesmos 20 WAV transcritos duas vezes, uma com
#: `language="pt"` e outra com `language=None`, unica variavel a lingua —
#: mostrou o acerto de intencao em PORTUGUES a DESCER: 21/40 linhas certas com
#: a lingua fixa contra 20/40 com deteccao (pt sem prefixo 11/20 -> 10/20, pt
#: com prefixo 10/20 = 10/20). A regra definida antes da medicao era reverter
#: se o portugues descesse: o produto volta a `language="pt"`.
#:
#: O ingles continua a funcionar porque nunca dependeu disto: o encaminhamento
#: casa a frase contra AS DUAS listas brancas e o mesmo A/B mostrou
#: ZERO linhas inglesas a mudar de acerto com a deteccao ligada.
#:
#: O mecanismo NAO foi apagado — `decidir_lingua` continua testado e ligavel
#: com `lingua_fixa=None` (scripts) ou `--lingua auto` (arnes). E o que permite
#: voltar a medir isto quando houver voz inglesa real ou modelo maior,
#: sem reescrever nada.
LINGUA_FIXA_DO_PRODUTO = "pt"

#: O limiar e o default da biblioteca instalada
#: (`faster_whisper/transcribe.py:747`, `language_detection_threshold=0.5`).
LIMIAR_CONFIANCA = 0.5

#: A marca que o log e a evidencia levam quando o argmax LIVRE caiu fora de
#: {pt, en}. Nao e um aviso decorativo: e a unica forma de
#: alguem contar, nos ficheiros, quantas frases foram descodificadas por uma
#: lingua que o produto nao escolheu.
MARCA_LINGUA_TERCEIRA = "lingua-terceira"


@dataclass(frozen=True)
class LinguaDetetada:
    """O que se sabe sobre a lingua de UMA frase, ja decidido.

    `lingua` e sempre `"pt"` ou `"en"`. `hesitou` diz se o numero por tras
    dessa escolha passou o limiar — e e isso, e nao a lingua, que o log tem de
    mostrar para a decisao ser explicavel ao utilizador.
    """

    lingua: str
    probabilidade: float
    hesitou: bool
    prob_pt: float
    prob_en: float
    #: A lingua mais provavel entre TODAS as ~100 do Whisper. Se nao for `pt`
    #: nem `en`, nao entra na escolha do produto — mas foi ela que descodificou
    #: o audio, e a frase fica marcada `lingua-terceira`.
    top1: str | None
    top1_probabilidade: float
    motivo: str
    #: False quando NAO houve deteccao nenhuma: a transcricao correu com a
    #: lingua fixa (o produto, desde a reversao da deteccao). Nesse
    #: caso `probabilidade` e 0.0 porque nao ha numero nenhum medido — e o log
    #: tem de dizer "FIXA", nao um `p=0.00 hesitou` que parece uma deteccao
    #: falhada.
    detetada: bool = True
    #: False quando a fonte so deu o top-1 (o caminho vivo, via RealtimeSTT):
    #: nesse caso `prob_pt`/`prob_en` tem 0.0 na lingua que NAO ganhou porque
    #: e desconhecida, nao porque valha zero — e o log tem de o dizer, senao
    #: inventa um numero que ninguem mediu.
    probabilidades_completas: bool = True

    @property
    def margem(self) -> float:
        """Distancia entre as duas candidatas, `|p(pt) - p(en)|`.

        O numero exato medido fica para se poder revisitar o limiar. Nao
        entra na decisao de `hesitou` (o criterio e o limiar, e so ele);
        anda no log como diagnostico.
        """
        return abs(self.prob_pt - self.prob_en)

    @property
    def lingua_terceira(self) -> bool:
        """True quando o argmax LIVRE apontou para fora de {pt, en}.

        Nesse caso a lingua do produto continua a ser `pt` ou `en` (argmax
        restrito), mas quem descodificou o audio foi a terceira lingua: e por
        isso que isto nao se chama "ignorada" (seria uma afirmacao falsa).
        """
        return self.top1 is not None and self.top1 not in LINGUAS_RESTRITAS

    @property
    def marca_lingua_terceira(self) -> str:
        """`lingua-terceira(<codigo> descodificou)` ou vazio.

        A marca que se escreve no log de cada frase e
        que a evidencia conta por ficheiro.
        """
        if not self.lingua_terceira:
            return ""
        return f"{MARCA_LINGUA_TERCEIRA}({self.top1} descodificou)"

    def resumo(self) -> str:
        """Lingua, probabilidade e hesitacao, sem rotulo.

        Escreve sempre os tres numeros — lingua, probabilidade
        e se hesitou — mais a margem entre as duas candidatas, quando ela e
        conhecida, e a marca `lingua-terceira` quando o argmax livre caiu fora
        de {pt, en}.
        """
        if not self.detetada:
            # Sem deteccao nao ha probabilidade, e inventar uma (ou escrever
            # `p=0.00 hesitou`, que parece uma deteccao falhada) seria afirmar
            # o que ninguem mediu.
            #
            # Forma CURTA de proposito: esta linha sai uma vez por frase, no log
            # que o utilizador le em direto, e a justificacao inteira da reversao
            # (~190 caracteres) nao muda de frase para frase. Fica onde se
            # consulta uma vez: em `self.motivo` (que o caminho dos scripts
            # devolve no dict como `lingua_motivo` e o autoteste imprime) e no
            # comentario de `LINGUA_FIXA_DO_PRODUTO`, com os numeros.
            return f"{self.lingua} FIXA (sem deteccao)"
        estado = "hesitou" if self.hesitou else "decidida"
        marca = f" {self.marca_lingua_terceira}" if self.lingua_terceira else ""
        if not self.probabilidades_completas:
            return (
                f"{self.lingua} p={self.probabilidade:.2f} {estado}{marca} "
                "(so top-1: a probabilidade da outra lingua nao e exposta pelo "
                "RealtimeSTT)"
            )
        return (
            f"{self.lingua} p={self.probabilidade:.2f} {estado}{marca} "
            f"(pt={self.prob_pt:.2f} en={self.prob_en:.2f} margem={self.margem:.2f})"
        )

    def para_log(self) -> str:
        """O pedaco de linha de log da lingua, ja com o rotulo `lingua=`."""
        return f"lingua={self.resumo()}"


def _probabilidade(mapa: Mapping[str, float], codigo: str) -> float:
    valor = mapa.get(codigo, 0.0)
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def _normalizar(
    all_language_probs: Iterable[Sequence] | Mapping[str, float] | None,
) -> tuple[dict[str, float], str | None, float]:
    """Lista `[(codigo, prob), ...]` -> (mapa, top1, prob do top1).

    O top-1 e calculado aqui (maximo sobre TUDO) e nao assumido pela ordem da
    lista: o faster-whisper devolve-a ordenada, mas depender disso seria
    depender de um detalhe que nao esta no contrato da biblioteca.
    """
    if all_language_probs is None:
        return {}, None, 0.0
    if isinstance(all_language_probs, Mapping):
        pares = list(all_language_probs.items())
    else:
        pares = []
        for entrada in all_language_probs:
            # Uma entrada que nao seja um par (codigo, prob) e ignorada em vez
            # de rebentar: isto corre no meio de uma transcricao ja feita, e
            # perder a frase por causa do formato de um diagnostico seria pior
            # do que perder o diagnostico.
            if isinstance(entrada, (str, bytes)):
                continue
            try:
                codigo, prob = entrada
            except (TypeError, ValueError):
                continue
            pares.append((codigo, prob))

    mapa: dict[str, float] = {}
    for codigo, prob in pares:
        try:
            mapa[str(codigo)] = float(prob)
        except (TypeError, ValueError):
            continue
    if not mapa:
        return {}, None, 0.0
    top1 = max(mapa, key=lambda codigo: mapa[codigo])
    return mapa, top1, mapa[top1]


def decidir_lingua(
    all_language_probs: Iterable[Sequence] | Mapping[str, float] | None,
    *,
    limiar: float = LIMIAR_CONFIANCA,
) -> LinguaDetetada:
    """Argmax restrito a {pt, en} sobre `info.all_language_probs`.

    `hesitou` segue a comparacao ESTRITA da propria biblioteca
    (`transcribe.py:1787`: aceita a deteccao quando `prob > limiar`), por isso
    uma probabilidade exatamente igual ao limiar conta como hesitacao. E o lado
    seguro: hesitar nao perde comando nenhum (as duas listas brancas continuam
    a mandar), enquanto dar por decidida uma lingua a 0,50 seria afirmar mais
    do que o numero diz.
    """
    mapa, top1, top1_prob = _normalizar(all_language_probs)
    if not mapa:
        return LinguaDetetada(
            lingua=LINGUA_POR_OMISSAO,
            probabilidade=0.0,
            hesitou=True,
            prob_pt=0.0,
            prob_en=0.0,
            top1=None,
            top1_probabilidade=0.0,
            motivo=(
                "sem probabilidades por lingua (transcricao com language fixo, "
                f"ou info sem all_language_probs): fica '{LINGUA_POR_OMISSAO}' por omissao"
            ),
        )

    prob_pt = _probabilidade(mapa, "pt")
    prob_en = _probabilidade(mapa, "en")
    # Empate (incluindo 0.0 vs 0.0) fica em portugues: e a lingua por omissao
    # do produto, nao uma escolha do acaso da ordem do dicionario.
    lingua = "en" if prob_en > prob_pt else "pt"
    probabilidade = prob_en if lingua == "en" else prob_pt
    hesitou = not (probabilidade > limiar)

    if top1 is not None and top1 not in LINGUAS_RESTRITAS:
        motivo = (
            f"{MARCA_LINGUA_TERCEIRA}: o argmax livre era '{top1}' "
            f"(p={top1_prob:.2f}), fora de {{pt, en}} — nao entra na escolha do "
            f"PRODUTO (log, evidencia, encaminhador), mas foi ELE que "
            f"descodificou o audio, porque o parametro language= da API publica "
            f"do faster-whisper aceita um codigo unico e nao uma lista; "
            f"a frase segue o caminho normal"
        )
    elif hesitou:
        motivo = f"probabilidade {probabilidade:.2f} nao passa o limiar {limiar:.2f}"
    else:
        motivo = "argmax restrito a {pt, en} acima do limiar"

    return LinguaDetetada(
        lingua=lingua,
        probabilidade=probabilidade,
        hesitou=hesitou,
        prob_pt=prob_pt,
        prob_en=prob_en,
        top1=top1,
        top1_probabilidade=top1_prob,
        motivo=motivo,
    )


def lingua_fixada(codigo: str = LINGUA_FIXA_DO_PRODUTO) -> LinguaDetetada:
    """O estado da lingua quando NAO houve deteccao nenhuma.

    Desde a reversao (o A/B controlado mostrou o acerto em portugues a descer),
    o produto transcreve com `language="pt"` fixo. Nesse caso nao ha
    `all_language_probs`, nao ha probabilidade e nao ha hesitacao: ha uma
    escolha do produto, e e isso que o log escreve. Devolver aqui um
    `p=0.00 hesitou` — que e o que `decidir_lingua(None)` devolve — daria a
    entender que uma deteccao correu e falhou, o que seria falso.
    """
    limpo = (codigo or "").strip().lower() or LINGUA_POR_OMISSAO
    return LinguaDetetada(
        lingua=limpo,
        probabilidade=0.0,
        hesitou=False,
        prob_pt=0.0,
        prob_en=0.0,
        top1=None,
        top1_probabilidade=0.0,
        motivo=(
            f"sem deteccao: a transcricao correu com language='{limpo}' fixo "
            "(o A/B controlado mostrou o acerto de "
            "intencao em portugues a descer com a deteccao ligada)"
        ),
        detetada=False,
        probabilidades_completas=False,
    )


def decidir_lingua_do_top1(
    lingua: str | None,
    probabilidade: float | None,
    *,
    limiar: float = LIMIAR_CONFIANCA,
) -> LinguaDetetada:
    """A mesma decisao com a unica informacao que o RealtimeSTT expoe.

    O caminho vivo so tem `recorder.detected_language` /
    `recorder.detected_language_probability` (o `all_language_probs` e deitado
    fora dentro do RealtimeSTT). Regra, identica em espirito a `decidir_lingua`:

      * top-1 `pt` ou `en` -> e essa a lingua, com a sua probabilidade;
      * top-1 numa terceira lingua, ou nenhum -> nao entra na escolha do
        produto: fica o portugues por omissao, com `hesitou=True`, a marca
        `lingua-terceira` e o motivo escrito no log.

    MESMO LIMITE do caminho dos scripts, e aqui ainda mais direto: o
    RealtimeSTT tambem chama o faster-whisper com `language=None`, portanto o
    top-1 que ele devolve E a lingua com que o audio foi descodificado. Quando
    cai fora de {pt, en}, o texto que chega ao encaminhador ja veio dessa
    terceira lingua — a marca serve para isso se ver no log.

    Nos dois casos a probabilidade da OUTRA candidata e desconhecida (fica
    0.0), e o log di-lo pelo motivo — nunca se inventa um numero.
    """
    try:
        prob = float(probabilidade) if probabilidade is not None else 0.0
    except (TypeError, ValueError):
        prob = 0.0

    codigo = (lingua or "").strip().lower() or None

    if codigo in LINGUAS_RESTRITAS:
        hesitou = not (prob > limiar)
        motivo = (
            "top-1 do RealtimeSTT (all_language_probs nao exposto pela "
            "biblioteca)"
        )
        if hesitou:
            motivo += f"; probabilidade {prob:.2f} nao passa o limiar {limiar:.2f}"
        return LinguaDetetada(
            lingua=codigo,
            probabilidade=prob,
            hesitou=hesitou,
            prob_pt=prob if codigo == "pt" else 0.0,
            prob_en=prob if codigo == "en" else 0.0,
            top1=codigo,
            top1_probabilidade=prob,
            motivo=motivo,
            probabilidades_completas=False,
        )

    if codigo is None:
        motivo = (
            "o RealtimeSTT nao devolveu lingua nenhuma: fica "
            f"'{LINGUA_POR_OMISSAO}' por omissao"
        )
    else:
        motivo = (
            f"{MARCA_LINGUA_TERCEIRA}: top-1 '{codigo}' (p={prob:.2f}) fora de "
            f"{{pt, en}} — nao entra na escolha do PRODUTO, mas foi ELE que "
            f"descodificou o audio (o RealtimeSTT tambem transcreve com "
            f"language=None); fica '{LINGUA_POR_OMISSAO}' por omissao "
            f"e a frase segue o caminho normal"
        )
    return LinguaDetetada(
        lingua=LINGUA_POR_OMISSAO,
        probabilidade=0.0,
        hesitou=True,
        prob_pt=0.0,
        prob_en=0.0,
        top1=codigo,
        top1_probabilidade=prob if codigo is not None else 0.0,
        motivo=motivo,
        probabilidades_completas=False,
    )
