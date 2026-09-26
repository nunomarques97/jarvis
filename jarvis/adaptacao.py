r"""Adaptacao da transcricao ao sotaque do utilizador, sem treino e sem pacote novo.

Duas pecas independentes, ligadas e desligadas em [adaptacao] no config.toml:

1. Reforco de frases (phrase boosting por shallow fusion) dentro da
   descodificacao gulosa do Parakeet TDT no onnx-asr. As frases a reforcar sao
   o vocabulario de comandos do jarvis (`VOCABULARIO_EN`, so palavras
   genericas) mais os nomes dos projetos lidos do config.toml em cada arranque.
   Cada frase e partida em tokens com o proprio vocab.txt do modelo e entra
   numa arvore de prefixos de tokens. Em cada passo da descodificacao, os
   tokens que continuam um prefixo ja emitido (ou que comecam uma frase, numa
   fronteira de palavra) recebem um bonus no logit antes do argmax. Quando a
   frase acaba ou e abandonada, o estado volta a raiz.

2. Lexico de correcoes aprendido das gravacoes de treino
   (`models/adaptacao/lexico-en.json`, ignorado pelo Git porque vem da voz do
   utilizador). Aplica-se ao texto ja descodificado, so em palavras inteiras.
   Se o ficheiro faltar ou for invalido, a transcricao continua igual e o
   problema fica no log uma unica vez.

O reforco usa um gancho interno do onnx-asr 0.12.0 (o metodo `_decode`
chamado a cada passo por `_AsrWithTransducerDecoding._decoding`). Se esse
gancho faltar ou tiver outra assinatura, o reforco desliga-se com uma linha
clara no log e a transcricao normal continua. O trabalho extra por passo sao
consultas a dicionarios e um bonus somado a poucos logits.

Formato do lexico:

    {"versao": 1, "lingua": "en",
     "regras": [{"de": "texto ouvido", "para": "texto certo"}, ...]}
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from jarvis.audio_util import RAIZ
from jarvis.config import BONUS_DE_REFORCO_PADRAO

#: A lingua em que o vocabulario e o lexico fazem sentido. Noutra lingua a
#: transcricao passa sem adaptacao nenhuma.
LINGUA_DA_ADAPTACAO = "en"

PASTA_DA_ADAPTACAO = RAIZ / "models" / "adaptacao"
CAMINHO_DO_LEXICO = PASTA_DA_ADAPTACAO / f"lexico-{LINGUA_DA_ADAPTACAO}.json"

#: Vocabulario de comandos do jarvis que vale a pena reforcar. So palavras
#: genericas do produto; os nomes dos projetos vem do config.toml.
VOCABULARIO_EN: tuple[str, ...] = (
    "jarvis",
    "Claude",
    "Claude Code",
    "Forja",
    "abort",
    "cancel",
    "status",
    "report",
    "add tests",
    "run the tests",
    "project",
)

#: Assinatura esperada do gancho `_decode` do onnx-asr (sem o self).
PARAMETROS_DO_GANCHO = ("prev_tokens", "prev_state", "encoder_out")

FRASE_MAXIMA = 60
LEXICO_MAXIMO_BYTES = 1_000_000
REGRAS_MAXIMAS = 2000
REGRA_MAXIMA = 80
VERSAO_DO_LEXICO = 1

Avisar = Callable[[str], None]


def _avisar_no_stderr(linha: str) -> None:
    print(linha, file=sys.stderr, flush=True)


# --- Arvore de prefixos de tokens --------------------------------------------


def ler_vocab(caminho: str | Path) -> dict[int, str]:
    """Le um vocab.txt do onnx-asr ("token id" por linha), como o onnx-asr o le."""
    vocab: dict[int, str] = {}
    with Path(caminho).open("rt", encoding="utf-8") as ficheiro:
        for linha in ficheiro:
            token, ident = linha.rstrip("\n").split(" ")
            vocab[int(ident)] = token.replace("▁", " ")
    return vocab


def _token_especial(texto: str) -> bool:
    return texto.startswith("<") and texto.endswith(">")


def tokenizar(texto: str, texto_para_id: dict[str, int], comprimento_maximo: int) -> list[int] | None:
    """Parte o texto nos tokens do vocabulario, sempre o mais comprido primeiro.

    Devolve None quando um caracter nao existe em token nenhum.
    """
    ids: list[int] = []
    inicio = 0
    while inicio < len(texto):
        for fim in range(min(len(texto), inicio + comprimento_maximo), inicio, -1):
            ident = texto_para_id.get(texto[inicio:fim])
            if ident is not None:
                ids.append(ident)
                inicio = fim
                break
        else:
            return None
    return ids


def variantes_da_frase(frase: str) -> list[str]:
    """As formas escritas em que o modelo pode dar a frase (maiusculas e separadores)."""
    base = " ".join(frase.split())
    formas = [base]
    if re.search(r"[-_]", base):
        formas.append(" ".join(re.sub(r"[-_]+", " ", base).split()))
    variantes: list[str] = []
    for forma in formas:
        for variante in (forma, forma.lower(), forma[:1].upper() + forma[1:]):
            if variante and variante not in variantes:
                variantes.append(variante)
    return variantes


@dataclass(eq=False)
class NoDaArvore:
    filhos: dict[int, "NoDaArvore"] = field(default_factory=dict)
    terminal: bool = False
    #: Os ids dos filhos, prontos para indexar os logits (None sem filhos).
    ids: object = None


class ArvoreDeFrases:
    """Arvore de prefixos de tokens das frases a reforcar.

    Cada frase comeca por um espaco (o marcador de inicio de palavra do
    vocabulario), por isso so se entra numa frase numa fronteira de palavra.
    """

    def __init__(self, frases: Iterable[str], vocab: dict[int, str], blank: int | None = None) -> None:
        texto_para_id: dict[str, int] = {}
        for ident in sorted(vocab):
            texto = vocab[ident]
            if not texto or ident == blank or _token_especial(texto):
                continue
            texto_para_id.setdefault(texto, ident)
        comprimento_maximo = max((len(texto) for texto in texto_para_id), default=0)
        self.raiz = NoDaArvore()
        self.frases: list[str] = []
        self.ignoradas: list[str] = []
        for frase in frases:
            for variante in variantes_da_frase(frase):
                ids = tokenizar(" " + variante, texto_para_id, comprimento_maximo)
                if not ids:
                    self.ignoradas.append(variante)
                    continue
                self._inserir(ids)
                self.frases.append(variante)
        self._preparar(self.raiz)

    @property
    def vazia(self) -> bool:
        return not self.raiz.filhos

    def _inserir(self, ids: Sequence[int]) -> None:
        no = self.raiz
        for ident in ids:
            no = no.filhos.setdefault(ident, NoDaArvore())
        no.terminal = True

    def _preparar(self, raiz: NoDaArvore) -> None:
        import numpy as np

        pendentes = [raiz]
        while pendentes:
            no = pendentes.pop()
            if no.filhos:
                no.ids = np.fromiter(no.filhos, dtype=np.int64, count=len(no.filhos))
                pendentes.extend(no.filhos.values())

    def _entrar(self, no: NoDaArvore) -> NoDaArvore:
        # Frase completa sem continuacao: volta-se a raiz.
        return self.raiz if no.terminal and not no.filhos else no

    def avancar(self, no: NoDaArvore, token: int) -> NoDaArvore:
        """O no depois de o modelo emitir `token` a partir de `no`."""
        filho = no.filhos.get(token)
        if filho is not None:
            return self._entrar(filho)
        if no is not self.raiz:
            # Frase abandonada: o mesmo token pode comecar outra frase.
            filho = self.raiz.filhos.get(token)
            if filho is not None:
                return self._entrar(filho)
        return self.raiz


# --- Gancho na descodificacao do onnx-asr ------------------------------------


def motivo_sem_gancho(asr: object) -> str | None:
    """None se o modelo tem o gancho esperado; senao, porque nao se pode usar."""
    decode = getattr(asr, "_decode", None)
    if not callable(decode):
        return "o onnx-asr nao tem o metodo interno _decode"
    if not callable(getattr(asr, "_decoding", None)):
        return "o onnx-asr nao tem o metodo interno _decoding"
    if not hasattr(asr, "_max_tokens_per_step"):
        return "o modelo nao e um transdutor (RNN-T/TDT)"
    vocab = getattr(asr, "_vocab", None)
    if not isinstance(vocab, dict) or not vocab:
        return "o onnx-asr nao expoe o vocabulario do modelo"
    if not isinstance(getattr(asr, "_blank_idx", None), int):
        return "o onnx-asr nao expoe o token vazio do modelo"
    try:
        parametros = tuple(inspect.signature(decode).parameters)
    except (TypeError, ValueError) as erro:
        return f"a assinatura de _decode nao se consegue ler ({erro})"
    if parametros != PARAMETROS_DO_GANCHO:
        return f"_decode tem outra assinatura ({', '.join(parametros)})"
    return None


class ReforcoDeFrases:
    """Substitui `_decode` do modelo: chama o original e soma o bonus aos logits.

    So atua dentro de `ativo()`, e o estado de cada descodificacao e da thread
    que a faz. A lista de tokens que o onnx-asr passa em cada passo e a mesma
    ao longo de uma frase, por isso so se processam os tokens novos.
    """

    def __init__(self, arvore: ArvoreDeFrases, bonus: float, original: Callable, avisar: Avisar) -> None:
        self.arvore = arvore
        self.bonus = float(bonus)
        self.original = original
        self._avisar = avisar
        self._local = threading.local()
        self.avariado = False

    @contextmanager
    def ativo(self) -> Iterator[None]:
        local = self._local
        local.ativo, local.lista, local.processados, local.no = True, None, 0, self.arvore.raiz
        try:
            yield
        finally:
            local.ativo, local.lista, local.no = False, None, self.arvore.raiz

    def _no_atual(self, local: threading.local, tokens: list[int]) -> NoDaArvore:
        if local.lista is not tokens or len(tokens) < local.processados:
            local.lista, local.processados, local.no = tokens, 0, self.arvore.raiz
        no = local.no
        while local.processados < len(tokens):
            no = self.arvore.avancar(no, tokens[local.processados])
            local.processados += 1
        local.no = no
        return no

    def __call__(self, prev_tokens, prev_state, encoder_out):
        resultado = self.original(prev_tokens, prev_state, encoder_out)
        local = self._local
        if self.avariado or not getattr(local, "ativo", False):
            return resultado
        try:
            ids = self._no_atual(local, prev_tokens).ids
            if ids is None:
                return resultado
            logits, passo, estado = resultado
            logits = logits.copy()
            logits[ids] += self.bonus
            return logits, passo, estado
        except Exception as erro:  # um formato inesperado nunca parte a transcricao
            self.avariado = True
            self._avisar(f"adaptacao: reforco de frases desligado ({erro!r}); a transcricao continua sem ele")
            return resultado


# --- Lexico de correcoes -----------------------------------------------------


def _chave(texto: str) -> str:
    return " ".join(texto.split()).lower()


class Lexico:
    """Correcoes "ouvido -> certo", aplicadas so a palavras inteiras."""

    def __init__(self, regras: Sequence[tuple[str, str]]) -> None:
        self.regras: dict[str, str] = {}
        for de, para in regras:
            self.regras[_chave(de)] = para
        self._padrao: re.Pattern[str] | None = None
        if self.regras:
            alternativas = sorted(self.regras, key=len, reverse=True)
            corpo = "|".join(r"\s+".join(re.escape(parte) for parte in chave.split()) for chave in alternativas)
            self._padrao = re.compile(rf"(?<!\w)(?:{corpo})(?!\w)", re.IGNORECASE)

    @classmethod
    def de_dados(cls, dados: object, lingua: str = LINGUA_DA_ADAPTACAO) -> "Lexico":
        """Valida o JSON do lexico. Levanta ValueError com o motivo."""
        if not isinstance(dados, dict):
            raise ValueError("o lexico tem de ser um objeto JSON")
        if dados.get("versao") != VERSAO_DO_LEXICO:
            raise ValueError(f"versao {dados.get('versao')!r} desconhecida (so {VERSAO_DO_LEXICO})")
        if dados.get("lingua") != lingua:
            raise ValueError(f"lingua {dados.get('lingua')!r}, esperada {lingua!r}")
        regras = dados.get("regras")
        if not isinstance(regras, list) or len(regras) > REGRAS_MAXIMAS:
            raise ValueError(f"'regras' tem de ser uma lista com ate {REGRAS_MAXIMAS} entradas")
        pares: list[tuple[str, str]] = []
        vistas: set[str] = set()
        for indice, regra in enumerate(regras):
            if not isinstance(regra, dict) or set(regra) != {"de", "para"}:
                raise ValueError(f"regras[{indice}] tem de ter so 'de' e 'para'")
            de, para = regra["de"], regra["para"]
            for nome, valor in (("de", de), ("para", para)):
                if not isinstance(valor, str) or not valor.strip() or len(valor) > REGRA_MAXIMA:
                    raise ValueError(f"regras[{indice}].{nome} tem de ser texto com 1 a {REGRA_MAXIMA} caracteres")
                if "\n" in valor or "\r" in valor:
                    raise ValueError(f"regras[{indice}].{nome} nao pode ter quebras de linha")
            chave = _chave(de)
            if chave in vistas:
                raise ValueError(f"regras[{indice}].de repetido")
            vistas.add(chave)
            pares.append((de, para.strip()))
        return cls(pares)

    def corrigir(self, texto: str) -> str:
        if self._padrao is None or not texto:
            return texto
        return self._padrao.sub(lambda achado: self.regras.get(_chave(achado.group(0)), achado.group(0)), texto)


def carregar_lexico(caminho: str | Path = CAMINHO_DO_LEXICO, avisar: Avisar | None = None) -> Lexico | None:
    """O lexico do ficheiro, ou None (com uma unica linha no log) se faltar ou for invalido."""
    avisar = avisar or _avisar_no_stderr
    caminho = Path(caminho)
    try:
        if caminho.stat().st_size > LEXICO_MAXIMO_BYTES:
            raise ValueError(f"maior do que {LEXICO_MAXIMO_BYTES} bytes")
        lexico = Lexico.de_dados(json.loads(caminho.read_text(encoding="utf-8")))
    except FileNotFoundError:
        avisar(f"adaptacao: sem lexico de correcoes em models/adaptacao/{caminho.name}; transcricao sem correcoes")
        return None
    except (OSError, ValueError) as erro:  # JSONDecodeError e UnicodeDecodeError sao ValueError
        avisar(f"adaptacao: lexico de correcoes invalido em models/adaptacao/{caminho.name} ({erro}); ignorado")
        return None
    avisar(f"adaptacao: lexico de correcoes com {len(lexico.regras)} regra(s)")
    return lexico


# --- O que o motor de transcricao usa ----------------------------------------


class Adaptacao:
    """Reforco de frases e lexico para um motor Parakeet; cada peca e opcional."""

    def __init__(
        self,
        frases: Iterable[str] = (),
        *,
        bonus: float = BONUS_DE_REFORCO_PADRAO,
        reforco: bool = True,
        lexico: Lexico | None = None,
        lingua: str = LINGUA_DA_ADAPTACAO,
        avisar: Avisar | None = None,
    ) -> None:
        self.frases = tuple(frase for frase in (" ".join(str(f).split()) for f in frases) if 0 < len(frase) <= FRASE_MAXIMA)
        self.bonus = float(bonus)
        self.reforco = reforco
        self.lexico = lexico
        self.lingua = lingua
        self.avisar = avisar or _avisar_no_stderr
        self.gancho: ReforcoDeFrases | None = None

    @property
    def reforco_instalado(self) -> bool:
        return self.gancho is not None and not self.gancho.avariado

    def instalar(self, modelo: object) -> bool:
        """Liga o reforco ao modelo carregado. False (com uma linha no log) se nao der."""
        if not self.reforco:
            return False
        asr = getattr(modelo, "asr", modelo)
        motivo = motivo_sem_gancho(asr)
        if motivo is not None:
            self.avisar(f"adaptacao: reforco de frases desligado ({motivo}); a transcricao continua sem ele")
            return False
        original = asr._decode
        if isinstance(original, ReforcoDeFrases):
            original = original.original
        arvore = ArvoreDeFrases(self.frases, asr._vocab, asr._blank_idx)
        if arvore.vazia:
            self.avisar("adaptacao: reforco de frases desligado (nenhuma frase cabe no vocabulario do modelo)")
            return False
        self.gancho = ReforcoDeFrases(arvore, self.bonus, original, self.avisar)
        asr._decode = self.gancho
        self.avisar(f"adaptacao: reforco de frases ligado ({len(self.frases)} frase(s), bonus {self.bonus:g})")
        return True

    @contextmanager
    def ao_transcrever(self, lingua: str | None) -> Iterator[None]:
        """Liga o reforco durante uma transcricao, so na lingua da adaptacao."""
        if self.gancho is None or lingua != self.lingua:
            yield
            return
        with self.gancho.ativo():
            yield

    def corrigir(self, texto: str, lingua: str | None) -> str:
        if self.lexico is None or lingua != self.lingua:
            return texto
        return self.lexico.corrigir(texto)


def frases_de_reforco(config: object) -> tuple[str, ...]:
    """O vocabulario de comandos mais os nomes dos projetos do config.toml."""
    projetos = tuple(projeto.nome for projeto in getattr(config, "projetos", ()))
    return VOCABULARIO_EN + tuple(nome for nome in projetos if nome not in VOCABULARIO_EN)


def criar_adaptacao(
    config: object,
    *,
    avisar: Avisar | None = None,
    caminho_do_lexico: str | Path = CAMINHO_DO_LEXICO,
) -> Adaptacao | None:
    """A adaptacao pedida em [adaptacao], ou None quando esta toda desligada."""
    ajuste = getattr(config, "adaptacao", None)
    if ajuste is None or not (ajuste.reforco or ajuste.lexico):
        return None
    lexico = carregar_lexico(caminho_do_lexico, avisar) if ajuste.lexico else None
    return Adaptacao(
        frases_de_reforco(config),
        bonus=ajuste.bonus,
        reforco=ajuste.reforco,
        lexico=lexico,
        avisar=avisar,
    )
