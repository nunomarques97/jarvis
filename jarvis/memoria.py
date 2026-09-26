"""Memoria das perguntas gerais: a conversa recente e o caderno de factos.

Duas memorias, as duas com tetos fixos, para que cada pergunta geral enviada
ao Claude tenha um tamanho maximo garantido (e um gasto de quota maximo):

  - HISTORICO (curto prazo), so em RAM e nunca escrito em disco: as ultimas
    perguntas gerais respondidas e as respostas tal como foram ditas ou
    mostradas (sem o prefixo "Claude says:"), para "and who scored?" ter
    contexto. No maximo `[memoria] trocas` trocas (teto 10), cada texto
    cortado em caracteres; esquece tudo ao fim de `[memoria] expira_min`
    minutos sem perguntas gerais (teto 30), e "new conversation" / "nova
    conversa" esquece logo.
  - CADERNO DE FACTOS (longo prazo), num ficheiro JSON local ignorado pelo
    Git (`memoria/factos.json` por omissao): o que o utilizador pediu para
    lembrar ("remember that ..."). No maximo `[memoria] factos` factos (teto
    50) e `[memoria] caracteres` caracteres no total (teto 4000), cada facto
    cortado em `MAXIMO_POR_FACTO`. Cheio, nada se grava ate se apagar um.
    A escrita e atomica (ficheiro temporario na mesma pasta e troca); um
    ficheiro estragado ou grande demais nunca derruba o jarvis: fica
    registado no log, o caderno comeca vazio e, na primeira escrita, o
    ficheiro estragado e posto de lado em `factos.json.estragado`.

Nunca se guardam segredos (palavras-passe, PINs, tokens, chaves, codigos) nem
dados financeiros (contas, IBAN, cartoes, saldos, salarios, carteiras,
valores em dinheiro, e tudo o que a regra financeira do interprete apanha):
`recusa_do_facto` diz porque, antes de qualquer recap, e o caderno volta a
verificar antes de escrever.

O historico e o caderno vao para o Claude como dados, em seccoes delimitadas
(`jarvis.pergunta_geral`); este modulo nunca envia nada.

Para o interprete local ha uma terceira memoria, tambem so em RAM: as
ultimas `[memoria] frases_interprete` frases (3 a 5) e o que o jarvis fez com
cada uma (`FrasesRecentes`), mais ate 3 factos do caderno que partilham uma
palavra de conteudo com a frase (`factos_relacionados`). O interprete corta
tudo num bloco de tamanho fixo e nunca tira dali um projeto.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from jarvis.config import (
    CARACTERES_DOS_FACTOS_MAXIMOS,
    RAIZ,
    EXPIRA_MAXIMO_MIN,
    FACTOS_MAXIMOS,
    FRASES_DO_INTERPRETE_MAXIMAS,
    FRASES_DO_INTERPRETE_MINIMAS,
    TROCAS_MAXIMAS,
    ConfigMemoria,
)
from jarvis.interprete import (
    FACTOS_NO_CONTEXTO,
    INTENCAO_RECUSADA,
    ContextoDoInterprete,
    FraseRecente,
    limpar_texto,
    pedido_financeiro,
    sem_palavra_de_ativacao,
)
from jarvis.router import _normalizar

#: Onde fica o caderno por omissao (pasta ignorada pelo Git).
CAMINHO_DO_CADERNO = RAIZ / "memoria" / "factos.json"

#: Cada pergunta e cada resposta do historico sao cortadas nisto.
MAXIMO_DA_PERGUNTA = 300
MAXIMO_DA_RESPOSTA = 600

#: Cada facto e cortado nisto (antes do recap: o que se ouve e o que se grava).
MAXIMO_POR_FACTO = 300

#: Um ficheiro do caderno maior do que isto nao e lido (esta estragado ou
#: nao foi o jarvis que o escreveu).
MAXIMO_DO_FICHEIRO_BYTES = 64 * 1024

#: Formato do ficheiro do caderno.
VERSAO_DO_CADERNO = 1


# --- Texto --------------------------------------------------------------------


def _uma_linha(texto: str, maximo: int) -> str:
    """Uma linha sem caracteres de controlo, cortada numa palavra inteira."""
    linha = " ".join(limpar_texto(texto or "").split())
    if len(linha) <= maximo:
        return linha
    cortada = linha[:maximo]
    espaco = cortada.rfind(" ")
    if espaco > maximo // 2:
        cortada = cortada[:espaco]
    return cortada.rstrip(" ,;:") + "…"


def texto_do_facto(texto: str) -> str:
    """O facto como fica gravado e e dito no recap: uma linha, com maiuscula e ponto."""
    facto = _uma_linha(texto, MAXIMO_POR_FACTO).strip(" ,;:-")
    facto = facto.rstrip(".!?… ").strip()
    if not facto:
        return ""
    return facto[0].upper() + facto[1:] + "."


# --- Segredos e dados financeiros ---------------------------------------------

#: Segredos, depois de `_normalizar`: nunca entram no caderno.
_SEGREDOS = re.compile(
    r"\b(?:"
    r"passwords?|passwd|passcodes?|passphrases?|pass\s+word|palavras?\s+passe|senhas?|"
    r"pins?|pin\s+codes?|puk|cvv|cvc|otp|2fa|two\s+factor|"
    r"tokens?|api\s+keys?|apikeys?|chaves?\s+(?:da\s+)?api|access\s+keys?|secret\s+keys?|"
    r"private\s+keys?|chaves?\s+privadas?|ssh\s+keys?|secrets?|segredos?|"
    r"credentials?|credenciais|login\s+details|dados\s+de\s+acesso|"
    r"seed\s+phrase|recovery\s+phrase|frase\s+de\s+recuperacao|mnemonic|codigos|codes"
    r")\b"
)

#: "code"/"codigo" no singular, num facto de programacao ("I write code in the
#: morning"), nao e um segredo; so conta com numeros ou com uma destas palavras.
_CODIGO = re.compile(r"\b(?:codigos?|codes?)\b")
_CODIGO_SECRETO = re.compile(
    r"\b(?:door|access|security|verification|alarm|lock|login|unlock|safe|backup|recovery|entry|gate|"
    r"porta|acesso|seguranca|verificacao|alarme|cofre|entrada|portao|desbloqueio|cartao|card|bank|banco|"
    r"multibanco|atm|wifi|wi\s+fi|router)\b"
)

#: Dados financeiros, depois de `_normalizar`.
_FINANCEIROS = re.compile(
    r"\b(?:"
    r"bank\s+accounts?|bank\s+details|account\s+numbers?|conta\s+bancaria|contas\s+bancarias|"
    r"conta\s+(?:do|no|de)\s+banco|numero\s+de\s+conta|numero\s+da\s+conta|iban|nib|swift|bic|"
    r"routing\s+number|sort\s+code|"
    r"card\s+numbers?|credit\s+cards?|debit\s+cards?|cartao\s+de\s+(?:credito|debito)|"
    r"numero\s+do\s+cartao|cartoes\s+de\s+(?:credito|debito)|"
    r"(?:account|bank|card|my)\s+balance|balances|saldos?|"
    r"salary|salaries|wage|wages|paycheck|income|earnings|salarios?|ordenados?|rendimentos?|"
    r"holdings?|portfolio|portfolios|carteira\s+de\s+(?:investimentos?|acoes)|net\s+worth|"
    r"savings|poupancas?|mortgage|hipoteca|loans?|emprestimos?|debts?|dividas?|"
    r"tax\s+(?:id|number)|nif|contribuinte|social\s+security"
    r")\b"
)

#: Um valor em dinheiro ("3000 euros", "500 dollars").
_DINHEIRO = re.compile(
    r"\b\d[\d\s]*\s*(?:k\s+)?(?:euros?|eur|dollars?|usd|pounds?|gbp|libras?|dolares?|cents?|centimos?)\b"
    r"|\b(?:euros?|eur|dollars?|usd|gbp)\s*\d"
)
_SIMBOLO_DE_DINHEIRO = re.compile(r"[€$£]\s*\d|\d\s*[€$£]")

#: Oito ou mais digitos seguidos (com espacos ou hifenes pelo meio): numero de
#: cartao, de conta, de telefone ou um codigo. Anos e quantidades pequenas passam.
_NUMERO_LONGO = re.compile(r"\d(?:[\s-]?\d){7,}")


def recusa_do_facto(texto: str, nomes_de_projeto: tuple[str, ...] | list[str] = ()) -> str | None:
    """'financeiro' ou 'segredo' se o facto nunca pode ser guardado, senao None.

    A regra financeira do interprete corre primeiro (uma ordem de compra ou
    venda e sempre financeira), depois os dados financeiros e os segredos.
    """
    limpo = limpar_texto(texto or "")
    if pedido_financeiro(limpo, tuple(nomes_de_projeto)) is not None:
        return "financeiro"
    normalizado = _normalizar(limpo)
    if _FINANCEIROS.search(normalizado) or _DINHEIRO.search(normalizado) or _SIMBOLO_DE_DINHEIRO.search(limpo):
        return "financeiro"
    if _SEGREDOS.search(normalizado):
        return "segredo"
    tem_numero = bool(re.search(r"\d", normalizado))
    if _CODIGO.search(normalizado) and (tem_numero or _CODIGO_SECRETO.search(normalizado)):
        return "segredo"
    if _NUMERO_LONGO.search(limpo):
        return "segredo"
    return None


# --- Historico de perguntas (curto prazo, so em RAM) ---------------------------


@dataclass(frozen=True)
class Troca:
    """Uma pergunta geral respondida e a resposta como foi dita ou mostrada."""

    pergunta: str
    resposta: str


class HistoricoDePerguntas:
    """As ultimas trocas das perguntas gerais. So em RAM; seguro entre threads.

    `relogio` e injetado nos testes; a expiracao conta desde a ultima troca
    guardada (a ultima pergunta geral respondida).
    """

    def __init__(
        self,
        maximo: int = TROCAS_MAXIMAS,
        expira_s: float = EXPIRA_MAXIMO_MIN * 60,
        *,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(maximo, bool) or not isinstance(maximo, int) or not 1 <= maximo <= TROCAS_MAXIMAS:
            raise ValueError(f"maximo de trocas invalido: {maximo!r} (1 a {TROCAS_MAXIMAS})")
        if isinstance(expira_s, bool) or not isinstance(expira_s, (int, float)) or not (
            0 < expira_s <= EXPIRA_MAXIMO_MIN * 60
        ):
            raise ValueError(f"expiracao invalida: {expira_s!r} s")
        self.maximo = maximo
        self.expira_s = float(expira_s)
        self._relogio = relogio
        self._trinco = threading.Lock()
        self._trocas: list[Troca] = []
        self._ultima = 0.0

    @classmethod
    def da_config(cls, config: ConfigMemoria, *, relogio: Callable[[], float] = time.monotonic) -> "HistoricoDePerguntas":
        return cls(config.trocas, config.expira_min * 60, relogio=relogio)

    def _expirar(self) -> None:
        if self._trocas and self._relogio() - self._ultima >= self.expira_s:
            self._trocas.clear()

    def acrescentar(self, pergunta: str, resposta: str) -> bool:
        """Guarda uma troca respondida. False (nada guardado) se algum texto vem vazio."""
        pergunta = _uma_linha(pergunta, MAXIMO_DA_PERGUNTA)
        resposta = _uma_linha(resposta, MAXIMO_DA_RESPOSTA)
        if not pergunta or not resposta:
            return False
        with self._trinco:
            self._expirar()
            self._trocas.append(Troca(pergunta, resposta))
            del self._trocas[: -self.maximo]
            self._ultima = self._relogio()
        return True

    def trocas(self) -> tuple[Troca, ...]:
        """As trocas ainda vigentes, da mais antiga para a mais recente."""
        with self._trinco:
            self._expirar()
            return tuple(self._trocas)

    def limpar(self) -> int:
        """Esquece a conversa ja. Devolve quantas trocas havia."""
        with self._trinco:
            quantas = len(self._trocas)
            self._trocas.clear()
            return quantas


# --- Frases recentes para o interprete local (so em RAM) ------------------------

#: O desfecho de cada frase, como o interprete o le (curto, em ingles).
_DESFECHOS = {
    "executado": "done",
    "pendente": "waiting for yes",
    "cancelado": "cancelled",
    "recusado": "refused",
    "expirado": "no answer, not sent",
    "falhou": "failed",
    "nao_percebido": "not understood",
    "ignorado": "ignored",
    "sem_pedido": "nothing to do",
}


def desfecho_para_o_interprete(estado: str | None) -> str:
    return _DESFECHOS.get(estado or "", "")


class FrasesRecentes:
    """As ultimas frases interpretadas e o que o jarvis fez com elas. So em RAM.

    Cada frase fica com um numero; o desfecho de um recap (sim, nao, prazo)
    so atualiza a frase com esse numero, se ela ainda la estiver, para uma
    resposta tardia nunca mudar outra frase. Uma frase recusada fica sem
    texto nem prompt. Seguro entre threads; esquece tudo ao fim de
    `expira_s` sem frases novas.
    """

    def __init__(
        self,
        maximo: int = FRASES_DO_INTERPRETE_MAXIMAS,
        expira_s: float = EXPIRA_MAXIMO_MIN * 60,
        *,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(maximo, bool) or not isinstance(maximo, int) or not (
            FRASES_DO_INTERPRETE_MINIMAS <= maximo <= FRASES_DO_INTERPRETE_MAXIMAS
        ):
            raise ValueError(
                f"maximo de frases invalido: {maximo!r} ({FRASES_DO_INTERPRETE_MINIMAS} a "
                f"{FRASES_DO_INTERPRETE_MAXIMAS})"
            )
        if isinstance(expira_s, bool) or not isinstance(expira_s, (int, float)) or not (
            0 < expira_s <= EXPIRA_MAXIMO_MIN * 60
        ):
            raise ValueError(f"expiracao invalida: {expira_s!r} s")
        self.maximo = maximo
        self.expira_s = float(expira_s)
        self._relogio = relogio
        self._trinco = threading.Lock()
        self._frases: list[tuple[int, FraseRecente]] = []
        self._numero = 0
        self._ultima = 0.0

    @classmethod
    def da_config(cls, config: ConfigMemoria, *, relogio: Callable[[], float] = time.monotonic) -> "FrasesRecentes":
        return cls(config.frases_interprete, config.expira_min * 60, relogio=relogio)

    def _expirar(self) -> None:
        if self._frases and self._relogio() - self._ultima >= self.expira_s:
            self._frases.clear()

    def acrescentar(
        self, frase: str, intencao: str, projeto: str | None, prompt: str, estado: str | None
    ) -> int:
        """Guarda uma frase e o desfecho dela; devolve o numero da frase."""
        if intencao == INTENCAO_RECUSADA:
            frase, prompt = "", ""
        recente = FraseRecente(
            sem_palavra_de_ativacao(limpar_texto(frase or "")),
            intencao,
            projeto,
            limpar_texto(prompt or ""),
            desfecho_para_o_interprete(estado),
        )
        with self._trinco:
            self._expirar()
            self._numero += 1
            self._frases.append((self._numero, recente))
            del self._frases[: -self.maximo]
            self._ultima = self._relogio()
            return self._numero

    def atualizar(self, numero: int, estado: str | None, prompt: str | None = None) -> bool:
        """O desfecho (e o prompt confirmado, se mudou) da frase `numero`. False se ja saiu."""
        with self._trinco:
            for indice, (proprio, recente) in enumerate(self._frases):
                if proprio != numero:
                    continue
                if recente.intencao == INTENCAO_RECUSADA:
                    prompt = None
                self._frases[indice] = (
                    proprio,
                    FraseRecente(
                        recente.frase,
                        recente.intencao,
                        recente.projeto,
                        limpar_texto(prompt) if prompt else recente.prompt,
                        desfecho_para_o_interprete(estado) or recente.feito,
                    ),
                )
                return True
            return False

    def frases(self) -> tuple[FraseRecente, ...]:
        """As frases ainda vigentes, da mais antiga para a mais recente."""
        with self._trinco:
            self._expirar()
            return tuple(recente for _numero, recente in self._frases)

    def limpar(self) -> int:
        with self._trinco:
            quantas = len(self._frases)
            self._frases.clear()
            return quantas


#: O nome do assistente nao liga uma frase a um facto.
_NOMES_DO_ASSISTENTE = frozenset({"jarvis", "hey"})


def factos_relacionados(
    frase: str, factos: tuple[str, ...] | list[str], maximo: int = FACTOS_NO_CONTEXTO
) -> tuple[str, ...]:
    """Ate `maximo` factos que partilham uma palavra de conteudo com a frase.

    Os que partilham mais palavras primeiro; no empate, a ordem do caderno.
    """
    ditas = _palavras_de_conteudo(sem_palavra_de_ativacao(limpar_texto(frase or ""))) - _NOMES_DO_ASSISTENTE
    if not ditas or maximo <= 0:
        return ()
    pontuados = []
    for ordem, facto in enumerate(factos):
        comuns = len(ditas & _palavras_de_conteudo(facto))
        if comuns:
            pontuados.append((-comuns, ordem, facto))
    return tuple(facto for _comuns, _ordem, facto in sorted(pontuados)[:maximo])


def contexto_do_interprete(
    frase: str, recentes: FrasesRecentes | None, caderno: "CadernoDeFactos | None"
) -> ContextoDoInterprete | None:
    """O contexto de uma frase para o interprete local, ou None se nao ha nada."""
    frases = recentes.frases() if recentes is not None else ()
    factos = factos_relacionados(frase, caderno.factos_para_contexto()) if caderno is not None else ()
    if not frases and not factos:
        return None
    return ContextoDoInterprete(frases, factos)


# --- Caderno de factos (longo prazo, ficheiro local) ---------------------------

#: Palavras que nao contam para achar o facto mais parecido.
_PALAVRAS_VAZIAS = frozenset(
    {
        "i", "me", "my", "mine", "a", "an", "the", "is", "am", "are", "was", "were", "be", "to", "of", "in",
        "on", "at", "for", "and", "or", "that", "this", "it", "its", "with", "about", "do", "does", "have",
        "has", "you", "your", "please",
        "eu", "o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "em", "no", "na", "nos",
        "nas", "e", "que", "se", "me", "meu", "minha", "meus", "minhas", "por", "para", "com", "sou", "e",
        "esta", "estou", "tenho", "isso", "isto", "favor",
    }
)

#: O facto mais parecido tem de partilhar pelo menos esta fracao das palavras
#: de conteudo do que foi dito.
SEMELHANCA_MINIMA = 0.5


def _palavras_de_conteudo(texto: str) -> set[str]:
    return {
        palavra for palavra in _normalizar(texto).split() if len(palavra) > 1 and palavra not in _PALAVRAS_VAZIAS
    }


def facto_mais_parecido(dito: str, factos: tuple[str, ...] | list[str]) -> str | None:
    """O facto que mais se parece com o que foi dito, ou None se nenhum se parece.

    Conta a fracao das palavras de conteudo ditas que o facto tem; o
    desempate e a semelhanca do texto inteiro. Sem palavras em comum, None.
    """
    ditas = _palavras_de_conteudo(dito)
    if not ditas:
        return None
    melhor: tuple[float, float] | None = None
    escolhido: str | None = None
    for facto in factos:
        comuns = ditas & _palavras_de_conteudo(facto)
        if not comuns:
            continue
        cobertura = len(comuns) / len(ditas)
        if cobertura < SEMELHANCA_MINIMA:
            continue
        pontos = (cobertura, SequenceMatcher(None, _normalizar(dito), _normalizar(facto)).ratio())
        if melhor is None or pontos > melhor:
            melhor, escolhido = pontos, facto
    return escolhido


class CadernoCheio(Exception):
    """O caderno chegou ao limite de factos ou de caracteres."""


class FactoRecusado(Exception):
    """O facto tem um segredo ou dados financeiros, ou esta vazio."""


class CadernoDeFactos:
    """Os factos sobre o utilizador, num ficheiro JSON local. Seguro entre threads.

    `registar(texto)` escreve no log do jarvis (problemas com o ficheiro).
    O ficheiro so e lido uma vez, na primeira utilizacao.
    """

    def __init__(
        self,
        caminho: str | Path = CAMINHO_DO_CADERNO,
        *,
        maximo_factos: int = FACTOS_MAXIMOS,
        maximo_caracteres: int = CARACTERES_DOS_FACTOS_MAXIMOS,
        nomes_de_projeto: tuple[str, ...] | list[str] = (),
        registar: Callable[[str], object] | None = None,
    ) -> None:
        if isinstance(maximo_factos, bool) or not isinstance(maximo_factos, int) or not (
            1 <= maximo_factos <= FACTOS_MAXIMOS
        ):
            raise ValueError(f"maximo de factos invalido: {maximo_factos!r} (1 a {FACTOS_MAXIMOS})")
        if isinstance(maximo_caracteres, bool) or not isinstance(maximo_caracteres, int) or not (
            1 <= maximo_caracteres <= CARACTERES_DOS_FACTOS_MAXIMOS
        ):
            raise ValueError(
                f"maximo de caracteres invalido: {maximo_caracteres!r} (ate {CARACTERES_DOS_FACTOS_MAXIMOS})"
            )
        self.caminho = Path(caminho)
        self.maximo_factos = maximo_factos
        self.maximo_caracteres = maximo_caracteres
        self.nomes_de_projeto = tuple(nomes_de_projeto)
        self._registar = registar
        self._trinco = threading.Lock()
        self._factos: list[str] | None = None
        self._estragado = False

    @classmethod
    def da_config(
        cls,
        config: ConfigMemoria,
        caminho: str | Path = CAMINHO_DO_CADERNO,
        **extras,
    ) -> "CadernoDeFactos":
        return cls(caminho, maximo_factos=config.factos, maximo_caracteres=config.caracteres, **extras)

    def _log(self, texto: str) -> None:
        if self._registar is not None:
            try:
                self._registar(f"memoria | {texto}")
            except Exception:  # noqa: BLE001 - o log nunca para o caderno
                pass

    # -- leitura

    def _carregar(self) -> list[str]:
        """Os factos do ficheiro (so na primeira vez). Nunca levanta."""
        if self._factos is not None:
            return self._factos
        self._factos = []
        try:
            if not self.caminho.exists():
                return self._factos
            if self.caminho.stat().st_size > MAXIMO_DO_FICHEIRO_BYTES:
                raise ValueError(f"ficheiro com mais de {MAXIMO_DO_FICHEIRO_BYTES} bytes")
            obj = json.loads(self.caminho.read_text(encoding="utf-8"))
            if not isinstance(obj, dict) or not isinstance(obj.get("factos"), list):
                raise ValueError("sem a lista 'factos'")
        except (OSError, ValueError, UnicodeDecodeError) as erro:
            self._estragado = True
            self._log(
                f"o caderno de factos nao se pode ler ({erro.__class__.__name__}: {str(erro)[:120]}); "
                "comeca vazio e o ficheiro e posto de lado na proxima escrita"
            )
            return self._factos
        ignorados = 0
        for item in obj["factos"]:
            facto = texto_do_facto(item) if isinstance(item, str) else ""
            if not facto or recusa_do_facto(facto, self.nomes_de_projeto) is not None or facto in self._factos:
                ignorados += 1
                continue
            self._factos.append(facto)
        if ignorados:
            self._log(f"{ignorados} entrada(s) do caderno ignorada(s) (vazias, repetidas ou proibidas)")
        return self._factos

    def factos(self) -> tuple[str, ...]:
        """Todos os factos guardados, pela ordem em que foram guardados."""
        with self._trinco:
            return tuple(self._carregar())

    def factos_para_contexto(self) -> tuple[str, ...]:
        """Os factos que cabem nos limites, para o contexto de uma pergunta.

        Mesmo com um ficheiro editado a mao ou limites baixados depois, o
        contexto nunca passa de `maximo_factos` nem de `maximo_caracteres`.
        """
        escolhidos: list[str] = []
        total = 0
        for facto in self.factos()[: self.maximo_factos]:
            if total + len(facto) > self.maximo_caracteres:
                break
            escolhidos.append(facto)
            total += len(facto)
        return tuple(escolhidos)

    def cheio(self) -> bool:
        with self._trinco:
            factos = self._carregar()
            return len(factos) >= self.maximo_factos or sum(map(len, factos)) >= self.maximo_caracteres

    def cabe(self, facto: str) -> bool:
        """O facto ainda cabe nos limites do caderno."""
        with self._trinco:
            factos = self._carregar()
            return (
                len(factos) + 1 <= self.maximo_factos
                and sum(map(len, factos)) + len(facto) <= self.maximo_caracteres
            )

    # -- escrita

    def acrescentar(self, texto: str) -> str:
        """Grava um facto e devolve-o como ficou. FactoRecusado, CadernoCheio ou OSError."""
        facto = texto_do_facto(texto)
        if not facto:
            raise FactoRecusado("facto vazio")
        motivo = recusa_do_facto(facto, self.nomes_de_projeto)
        if motivo is not None:
            raise FactoRecusado(motivo)
        with self._trinco:
            factos = self._carregar()
            if facto in factos:
                return facto
            if len(factos) + 1 > self.maximo_factos or sum(map(len, factos)) + len(facto) > self.maximo_caracteres:
                raise CadernoCheio(f"{len(factos)} factos, {sum(map(len, factos))} caracteres")
            self._gravar([*factos, facto])
            return facto

    def apagar(self, facto: str) -> bool:
        """Apaga o facto exato. False se ele ja nao existe. OSError se a escrita falha."""
        with self._trinco:
            factos = self._carregar()
            if facto not in factos:
                return False
            self._gravar([f for f in factos if f != facto])
            return True

    def _gravar(self, factos: list[str]) -> None:
        """Escrita atomica: ficheiro temporario na mesma pasta e troca. So com o trinco."""
        pasta = self.caminho.parent
        pasta.mkdir(parents=True, exist_ok=True)
        if self._estragado and self.caminho.exists():
            posto_de_lado = self.caminho.with_name(self.caminho.name + ".estragado")
            os.replace(self.caminho, posto_de_lado)
            self._log(f"o ficheiro estragado do caderno foi posto de lado em '{posto_de_lado.name}'")
        conteudo = json.dumps({"versao": VERSAO_DO_CADERNO, "factos": factos}, ensure_ascii=False, indent=2)
        descritor, temporario = tempfile.mkstemp(prefix=".factos-", suffix=".tmp", dir=pasta)
        try:
            with os.fdopen(descritor, "w", encoding="utf-8", newline="\n") as ficheiro:
                ficheiro.write(conteudo + "\n")
                ficheiro.flush()
                os.fsync(ficheiro.fileno())
            os.replace(temporario, self.caminho)
        except BaseException:
            try:
                os.unlink(temporario)
            except OSError:
                pass
            raise
        self._factos = factos
        self._estragado = False
