r"""Adapta a transcricao ao sotaque do Sponsor e mede o antes e o depois.

Um so comando, reprodutivel, sobre as gravacoes reais em ingles:

1. DIVISAO treino/teste das gravacoes de avaliacao (`recordings/en/`),
   deterministica (semente fixa) e estratificada pelo caso do guiao: cada caso
   tem frases dos dois lados. Todas as gravacoes de treino
   (`recordings/treino-en/`, `scripts/gravar_voz.py --lingua en --treino`)
   vao so para o lado do treino.
2. APRENDIZAGEM do lexico de correcoes so com o lado do treino: o Parakeet
   (CPU) transcreve cada frase de treino sem reforco e com reforco; cada troca
   entre a transcricao e a frase lida e uma regra candidata "ouvido -> certo".
   Uma regra so entra se a forma ouvida nunca e uma forma certa numa frase de
   treino, se nao piora nenhuma frase de treino (distancia de edicao ou
   intencao) e se melhora pelo menos uma. O lexico fica em
   `models/adaptacao/lexico-en.json` (ignorado pelo Git: vem da voz do Sponsor).
3. MEDICAO (`--medir`) so no lado do teste, na mesma execucao e com o mesmo
   modelo carregado: sem adaptacao, so reforco, so lexico e os dois. Para cada
   frase de teste a descodificacao sem e com reforco alterna a ordem, para a
   latencia ser comparavel; o lexico reaproveita essa descodificacao e soma o
   tempo da correcao. As medidas (WER, intencao preservada, latencia p50) sao
   as de `scripts/avaliar_voz.py`.

A evidencia vai para `docs/forja/evidence/` (ignorada): numeros, as listas de
ids dos dois lados e o veredito contra a meta (WER mais baixo e intencao
preservada mais alta do que sem adaptacao, latencia p50 no maximo 1,2x).
Nunca leva transcricoes, frases lidas, regras do lexico nem audio. Nunca toca
som.

O vocabulario reforcado e o do produto mais os nomes dos projetos do
config.toml (`jarvis.adaptacao.frases_de_reforco`), nunca os erros do teste.

Uso:
    .venv\Scripts\python scripts/adaptar_sotaque.py
    .venv\Scripts\python scripts/adaptar_sotaque.py --medir
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.adaptacao import (  # noqa: E402
    CAMINHO_DO_LEXICO,
    LINGUA_DA_ADAPTACAO,
    PASTA_DA_ADAPTACAO,
    REGRA_MAXIMA,
    VERSAO_DO_LEXICO,
    Adaptacao,
    Lexico,
    frases_de_reforco,
)
from jarvis.audio_util import caminho_para_mostrar  # noqa: E402
from jarvis.config import BONUS_DE_REFORCO_MAXIMO, BONUS_DE_REFORCO_PADRAO, Config  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.stt import MotorIndisponivel, MotorParakeet  # noqa: E402

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` por caminho (mesma chave de cache dos irmaos)."""
    chave = f"_jarvis_scripts_{nome}"
    ja_carregado = sys.modules.get(chave)
    if ja_carregado is not None:
        return ja_carregado
    caminho = PASTA_SCRIPTS / f"{nome}.py"
    spec = importlib.util.spec_from_file_location(chave, caminho)
    if spec is None or spec.loader is None:
        raise ImportError(f"nao foi possivel carregar o modulo irmao '{caminho}'")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    try:
        spec.loader.exec_module(modulo)
    except BaseException:
        del sys.modules[chave]
        raise
    return modulo


avaliar = _carregar_modulo_irmao("avaliar_voz")
gravar = avaliar.gravar
medir = avaliar.medir

LINGUA = LINGUA_DA_ADAPTACAO
SEMENTE_PADRAO = 1790
FRACAO_DE_TREINO_PADRAO = 0.5
#: Maximo de palavras de cada lado de uma regra do lexico.
PALAVRAS_MAXIMAS_DA_REGRA = 3
#: A latencia p50 adaptada pode ser no maximo isto vezes a de sem adaptacao.
LIMITE_DA_LATENCIA = 1.2

BASE, REFORCO, LEXICO, AMBOS = "sem adaptacao", "so reforco", "so lexico", "reforco + lexico"
VARIANTES = (BASE, REFORCO, LEXICO, AMBOS)
#: O que por no [adaptacao] do config.toml para cada variante.
CONFIG_DA_VARIANTE = {
    BASE: (False, False),
    REFORCO: (True, False),
    LEXICO: (False, True),
    AMBOS: (True, True),
}

COMANDO_PILOTO = r".venv\Scripts\python scripts/gravar_voz.py --lingua en --treino"
COMANDO_MEDIR = r".venv\Scripts\python scripts/adaptar_sotaque.py --medir"


# --- Divisao treino/teste --------------------------------------------------------


@dataclass(frozen=True)
class Divisao:
    treino: tuple[str, ...]
    teste: tuple[str, ...]


def _ordem(id_: str, semente: int) -> str:
    return hashlib.sha256(f"{semente}:{id_}".encode("utf-8")).hexdigest()


def dividir(itens: Iterable[tuple[str, str]], fracao_treino: float, semente: int) -> Divisao:
    """(id, caso) -> ids de treino e de teste, estratificados pelo caso.

    Deterministica: a mesma semente da sempre a mesma divisao, seja qual for a
    ordem de entrada. Cada caso fica com pelo menos uma frase no teste.
    """
    if not 0 < fracao_treino < 1:
        raise ValueError(f"fracao de treino {fracao_treino!r}: tem de estar entre 0 e 1")
    por_caso: dict[str, list[str]] = {}
    vistos: set[str] = set()
    for id_, caso in itens:
        if id_ in vistos:
            raise ValueError(f"id repetido na divisao: {id_}")
        vistos.add(id_)
        por_caso.setdefault(caso, []).append(id_)
    treino: list[str] = []
    teste: list[str] = []
    for caso in sorted(por_caso):
        ids = sorted(por_caso[caso], key=lambda i: (_ordem(i, semente), i))
        n_treino = min(len(ids) - 1, math.floor(len(ids) * fracao_treino + 0.5))
        treino += ids[:n_treino]
        teste += ids[n_treino:]
    return Divisao(tuple(sorted(treino)), tuple(sorted(teste)))


# --- Aprendizagem do lexico --------------------------------------------------------


@dataclass(frozen=True)
class ExemploDeTreino:
    id: str
    referencia: str
    #: As transcricoes da mesma frase (sem reforco e com reforco).
    hipoteses: tuple[str, ...]


@dataclass(frozen=True)
class Regra:
    de: str
    para: str


@dataclass
class LexicoAprendido:
    regras: list[Regra] = field(default_factory=list)
    candidatas: int = 0
    #: motivo -> quantas candidatas recusadas por ele.
    recusadas: dict[str, int] = field(default_factory=dict)

    def lexico(self) -> Lexico:
        return Lexico([(r.de, r.para) for r in self.regras])

    def dados(self) -> dict:
        return {
            "versao": VERSAO_DO_LEXICO,
            "lingua": LINGUA,
            "regras": [{"de": r.de, "para": r.para} for r in self.regras],
        }


RECUSA_FORMA_CERTA = "a forma ouvida e dita noutra frase de treino"
RECUSA_PIORA = "piora uma frase de treino"
RECUSA_SEM_GANHO = "nao melhora nenhuma frase de treino"
RECUSA_CONFLITO = "piora em conjunto com outra regra"


def _chave_do_token(token: str) -> str:
    return re.sub(r"[\W_]", "", token.lower())


def _sem_pontas(texto: str) -> str:
    return re.sub(r"^[\W_]+|[\W_]+$", "", texto)


def candidatas(referencia: str, hipotese: str) -> list[Regra]:
    """As trocas "ouvido -> lido" entre uma transcricao e a frase lida."""
    ref = [t for t in referencia.split() if _chave_do_token(t)]
    hip = [t for t in hipotese.split() if _chave_do_token(t)]
    comparador = difflib.SequenceMatcher(
        None, [_chave_do_token(t) for t in hip], [_chave_do_token(t) for t in ref], autojunk=False
    )
    regras: list[Regra] = []
    for operacao, i1, i2, j1, j2 in comparador.get_opcodes():
        if operacao != "replace" or i2 - i1 > PALAVRAS_MAXIMAS_DA_REGRA or j2 - j1 > PALAVRAS_MAXIMAS_DA_REGRA:
            continue
        de, para = _sem_pontas(" ".join(hip[i1:i2])), _sem_pontas(" ".join(ref[j1:j2]))
        if not de or not para or len(de) > REGRA_MAXIMA or len(para) > REGRA_MAXIMA:
            continue
        regras.append(Regra(de, para))
    return regras


def _chave_da_regra(texto: str) -> str:
    return " ".join(texto.split()).lower()


def _forma_dita(de: str, referencias_normalizadas: Sequence[str]) -> bool:
    alvo = gravar.normalizar(de)
    return bool(alvo) and any(f" {alvo} " in ref for ref in referencias_normalizadas)


def aprender_lexico(
    exemplos: Sequence[ExemploDeTreino],
    intencao: Callable[[str], str] | None = None,
) -> LexicoAprendido:
    """O lexico de correcoes aprendido so destes exemplos.

    Com `intencao`, uma regra que muda a intencao de uma transcricao para uma
    diferente da da frase lida tambem conta como piorar essa frase.
    """
    distancia = lambda ref, hip: medir.calcular_wer(ref, hip).distancia_edicao  # noqa: E731
    pares = [(e.referencia, h) for e in exemplos for h in dict.fromkeys(e.hipoteses)]
    distancias_base = [distancia(ref, hip) for ref, hip in pares]
    cache_da_intencao: dict[str, str] = {}

    def intencao_de(texto: str) -> str:
        if texto not in cache_da_intencao:
            cache_da_intencao[texto] = intencao(texto)  # type: ignore[misc]
        return cache_da_intencao[texto]

    def efeito(lexico: Lexico) -> tuple[int, int]:
        """(ganho total em distancia, quantas frases piora)."""
        ganho = piores = 0
        for (ref, hip), antes in zip(pares, distancias_base):
            corrigida = lexico.corrigir(hip)
            if corrigida == hip:
                continue
            depois = distancia(ref, corrigida)
            ganho += antes - depois
            pior = depois > antes
            if not pior and intencao is not None:
                certa = intencao_de(ref)
                pior = intencao_de(hip) == certa and intencao_de(corrigida) != certa
            piores += pior
        return ganho, piores

    resultado = LexicoAprendido()
    referencias = [f" {gravar.normalizar(e.referencia)} " for e in exemplos]
    vistas: dict[tuple[str, str], Regra] = {}
    for ref, hip in pares:
        for regra in candidatas(ref, hip):
            vistas.setdefault((_chave_da_regra(regra.de), _chave_da_regra(regra.para)), regra)
    resultado.candidatas = len(vistas)

    def recusar(motivo: str) -> None:
        resultado.recusadas[motivo] = resultado.recusadas.get(motivo, 0) + 1

    aceites: list[tuple[int, Regra]] = []
    for regra in vistas.values():
        if _forma_dita(regra.de, referencias):
            recusar(RECUSA_FORMA_CERTA)
            continue
        ganho, piores = efeito(Lexico([(regra.de, regra.para)]))
        if piores:
            recusar(RECUSA_PIORA)
        elif ganho <= 0:
            recusar(RECUSA_SEM_GANHO)
        else:
            aceites.append((ganho, regra))

    # Juntas, as regras podem interagir: cada uma so fica se o conjunto
    # continuar sem piorar nada e ganhar mais do que sem ela. Para a mesma
    # forma ouvida fica a correcao com mais ganho.
    aceites.sort(key=lambda par: (-par[0], _chave_da_regra(par[1].de), _chave_da_regra(par[1].para)))
    escolhidas: list[Regra] = []
    ganho_atual = 0
    for _ganho, regra in aceites:
        if any(_chave_da_regra(r.de) == _chave_da_regra(regra.de) for r in escolhidas):
            recusar(RECUSA_CONFLITO)
            continue
        tentativa = escolhidas + [regra]
        ganho, piores = efeito(Lexico([(r.de, r.para) for r in tentativa]))
        if piores or ganho <= ganho_atual:
            recusar(RECUSA_CONFLITO)
            continue
        escolhidas, ganho_atual = tentativa, ganho
    resultado.regras = escolhidas
    return resultado


# --- Onde se escreve -------------------------------------------------------------


def caminho_do_lexico_de_saida(valor: str | Path, pasta_permitida: Path | None = None) -> Path:
    """O lexico so se escreve como .json dentro de models/adaptacao/ (ignorada pelo Git)."""
    pasta = (PASTA_DA_ADAPTACAO if pasta_permitida is None else Path(pasta_permitida)).resolve()
    caminho = Path(valor)
    if not caminho.is_absolute():
        caminho = RAIZ / caminho
    caminho = caminho.resolve()
    if caminho.suffix.lower() != ".json":
        raise ValueError(f"o lexico tem de ser um .json: {caminho.name}")
    if not caminho.is_relative_to(pasta):
        raise ValueError("o lexico tem de ficar dentro de models/adaptacao/ (ignorada pelo Git)")
    return caminho


def escrever_lexico(caminho: Path, aprendido: LexicoAprendido) -> None:
    dados = aprendido.dados()
    Lexico.de_dados(dados)  # o que se escreve tem de ser o que o jarvis aceita
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporario.replace(caminho)


# --- O motor ---------------------------------------------------------------------


class Transcritor(Protocol):
    nome: str
    reforco_disponivel: bool

    def carregar(self) -> None: ...

    def transcrever(self, pcm: bytes, reforco: bool) -> tuple[str, float]: ...

    def libertar(self) -> None: ...


class TranscritorParakeet:
    """O Parakeet em CPU com o reforco que se liga e desliga a cada transcricao.

    Um so modelo carregado: sem reforco usa o `_decode` original do onnx-asr,
    com reforco o gancho de `jarvis.adaptacao`. O lexico nunca e aplicado aqui.
    """

    nome = MotorParakeet.nome

    def __init__(self, frases: Sequence[str], bonus: float, avisar: Callable[[str], None]) -> None:
        self.adaptacao = Adaptacao(frases, bonus=bonus, reforco=True, lexico=None, avisar=avisar)
        self.motor = MotorParakeet(device="cpu", adaptacao=self.adaptacao)
        self._asr = None
        self.latencia_carregamento_ms: float | None = None

    @property
    def reforco_disponivel(self) -> bool:
        return self.adaptacao.reforco_instalado

    def carregar(self) -> None:
        self.motor.carregar()
        self.latencia_carregamento_ms = self.motor.latencia_carregamento_ms
        if self.adaptacao.gancho is not None:
            modelo = self.motor._modelo
            self._asr = getattr(modelo, "asr", modelo)

    def transcrever(self, pcm: bytes, reforco: bool) -> tuple[str, float]:
        gancho = self.adaptacao.gancho
        if reforco and not self.reforco_disponivel:
            raise RuntimeError("reforco pedido sem o gancho instalado")
        if self._asr is not None and gancho is not None:
            self._asr._decode = gancho if reforco else gancho.original
        transcricao = self.motor.transcrever(pcm, lingua=LINGUA)
        return transcricao.texto, transcricao.latencia_ms

    def libertar(self) -> None:
        self.motor.libertar()


# --- A execucao ------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoDaVariante:
    variante: str
    n: int
    wer: float
    intencao_preservada: int
    intencao_certa: int
    projeto_certo: int
    p50_ms: float
    p95_ms: float


@dataclass
class Relatorio:
    semente: int
    fracao_treino: float
    bonus: float
    origem_config: str
    frases_reforcadas: int
    projetos_reforcados: int
    divisao: Divisao | None = None
    #: Ids das gravacoes da pasta de treino (todas do lado do treino).
    ids_do_treino_extra: tuple[str, ...] = ()
    em_falta: int = 0
    em_falta_no_treino: int = 0
    invalidas: int = 0
    origens: frozenset[str] = frozenset()
    pendente: str | None = None
    saltado: str | None = None
    reforco_disponivel: bool = False
    carregamento_ms: float | None = None
    lexico: LexicoAprendido | None = None
    medido: bool = False
    resultados: dict[str, ResultadoDaVariante] = field(default_factory=dict)

    def cumpre(self, variante: str) -> bool:
        """A variante bate a meta contra a medicao sem adaptacao desta execucao."""
        base, outra = self.resultados.get(BASE), self.resultados.get(variante)
        if variante == BASE or base is None or outra is None:
            return False
        return (
            outra.wer < base.wer
            and outra.intencao_preservada > base.intencao_preservada
            and outra.p50_ms <= LIMITE_DA_LATENCIA * base.p50_ms
        )

    @property
    def melhor(self) -> str | None:
        """A variante que cumpre com menor WER (depois mais intencao, depois menos latencia)."""
        boas = [v for v in VARIANTES if self.cumpre(v)]
        if not boas:
            return None
        return min(
            boas,
            key=lambda v: (
                self.resultados[v].wer,
                -self.resultados[v].intencao_preservada,
                self.resultados[v].p50_ms,
            ),
        )


def _resultado_da_variante(variante: str, linhas: Sequence) -> ResultadoDaVariante:
    agregado = next(a for a in avaliar.agregar(linhas) if a.faixa == avaliar.TODAS)
    return ResultadoDaVariante(
        variante=variante,
        n=agregado.n,
        wer=agregado.wer,
        intencao_preservada=agregado.intencao_preservada,
        intencao_certa=agregado.intencao_certa,
        projeto_certo=agregado.projeto_certo,
        p50_ms=agregado.p50_ms,
        p95_ms=agregado.p95_ms,
    )


def correr(
    transcritor: Transcritor,
    avaliacao: object,  # avaliar_voz.GravacoesDaLingua
    treino_extra: object | None,  # avaliar_voz.GravacoesDaLingua da pasta de treino
    config: Config,
    relatorio: Relatorio,
    caminho_do_lexico: Path,
    medir_teste: bool,
    interpretar: Callable[[str, Config], str] = avaliar.intencao_pelo_router,
    relogio: Callable[[], float] = time.perf_counter,
) -> Relatorio:
    """Divide, aprende o lexico com o treino e, com `medir_teste`, mede no teste."""
    gravacoes = list(avaliacao.gravacoes)
    extra = list(treino_extra.gravacoes) if treino_extra is not None else []
    relatorio.em_falta = len(avaliacao.em_falta)
    relatorio.invalidas = len(avaliacao.invalidas) + (len(treino_extra.invalidas) if treino_extra is not None else 0)
    relatorio.em_falta_no_treino = len(treino_extra.em_falta) if treino_extra is not None else 0
    relatorio.origens = frozenset(g.origem for g in gravacoes + extra)
    divisao = dividir(
        [(g.frase.id, g.frase.caso) for g in gravacoes], relatorio.fracao_treino, relatorio.semente
    ) if gravacoes else Divisao((), ())
    relatorio.divisao = divisao
    relatorio.ids_do_treino_extra = tuple(g.frase.id for g in extra)
    lado_treino = [g for g in gravacoes if g.frase.id in divisao.treino] + extra
    lado_teste = [g for g in gravacoes if g.frase.id in divisao.teste]
    if {g.frase.id for g in lado_treino} & {g.frase.id for g in lado_teste}:
        raise RuntimeError("a divisao treino/teste nao e disjunta")
    if not lado_treino or (medir_teste and not lado_teste):
        relatorio.pendente = "faltam gravacoes reais em recordings/en/ para dividir em treino e teste"
        return relatorio
    try:
        transcritor.carregar()
    except MotorIndisponivel as erro:
        relatorio.saltado = str(erro)
        return relatorio
    relatorio.carregamento_ms = getattr(transcritor, "latencia_carregamento_ms", None)
    try:
        reforco = transcritor.reforco_disponivel
        relatorio.reforco_disponivel = reforco
        modos = (False, True) if reforco else (False,)
        # A primeira inferencia paga o aquecimento: fica fora de tudo.
        for modo in modos:
            transcritor.transcrever(lado_treino[0].pcm, modo)

        exemplos = []
        for gravacao in lado_treino:
            hipoteses = tuple(transcritor.transcrever(gravacao.pcm, modo)[0] for modo in modos)
            exemplos.append(ExemploDeTreino(gravacao.frase.id, gravacao.referencia, hipoteses))
        aprendido = aprender_lexico(exemplos, lambda texto: interpretar(texto, config))
        relatorio.lexico = aprendido
        escrever_lexico(caminho_do_lexico, aprendido)
        if not medir_teste:
            return relatorio

        lexico = aprendido.lexico()
        nomes = avaliar.nomes_de_projeto(config, gravacoes)
        linhas: dict[str, list] = {v: [] for v in VARIANTES}
        for indice, gravacao in enumerate(lado_teste):
            # A ordem alterna de frase para frase: nenhuma variante fica sempre
            # com a cache mais quente.
            ordem = modos if indice % 2 == 0 else tuple(reversed(modos))
            obtido = {modo: transcritor.transcrever(gravacao.pcm, modo) for modo in ordem}
            certa = interpretar(gravacao.referencia, config)
            for variante, modo, com_lexico in (
                (BASE, False, False),
                (REFORCO, True, False),
                (LEXICO, False, True),
                (AMBOS, True, True),
            ):
                if modo not in obtido:
                    continue
                texto, latencia_ms = obtido[modo]
                if com_lexico:
                    inicio = relogio()
                    texto = lexico.corrigir(texto)
                    latencia_ms += (relogio() - inicio) * 1000
                linhas[variante].append(
                    avaliar.linha_avaliada(
                        gravacao, variante, texto, latencia_ms, config, nomes, certa, interpretar
                    )
                )
        gancho = getattr(getattr(transcritor, "adaptacao", None), "gancho", None)
        if gancho is not None and gancho.avariado:
            # O reforco desligou-se a meio: essas linhas sao iguais as de base.
            relatorio.reforco_disponivel = False
            linhas[REFORCO], linhas[AMBOS] = [], []
        relatorio.resultados = {v: _resultado_da_variante(v, l) for v, l in linhas.items() if l}
        relatorio.medido = True
        return relatorio
    finally:
        transcritor.libertar()


# --- Evidencia -------------------------------------------------------------------


def _pct(parte: int, total: int) -> str:
    return f"{parte}/{total} ({100 * parte / total:.0f}%)" if total else "—"


def _lista(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "—"


def config_recomendada(relatorio: Relatorio) -> str:
    variante = relatorio.melhor or BASE
    reforco, lexico = CONFIG_DA_VARIANTE[variante]
    linhas = ["[adaptacao]", f"reforco = {str(reforco).lower()}"]
    if reforco:
        linhas.append(f"bonus = {relatorio.bonus:g}")
    linhas.append(f"lexico = {str(lexico).lower()}")
    return "\n".join(linhas)


def passo_do_sponsor() -> str:
    return (
        f"**Passo do Sponsor:** gravar o guião de treino, a começar pelo piloto: `{COMANDO_PILOTO}` "
        "(grava 3 frases, verifica que há fala real e pede confirmação antes do resto). "
        f"Depois voltar a correr `{COMANDO_MEDIR}`."
    )


def evidencia_markdown(relatorio: Relatorio) -> str:
    linhas = [f"# Adaptação ao sotaque — antes/depois — {datetime.now().isoformat(timespec='seconds')}", ""]
    sem_voz = bool(relatorio.origens - {gravar.ORIGEM_MICROFONE})
    if sem_voz:
        linhas += [
            f"**{avaliar.AVISO_SEM_VOZ}.** Há gravações que não vieram do microfone "
            f"(origens: {', '.join(sorted(relatorio.origens))}).",
            "",
        ]
    divisao = relatorio.divisao or Divisao((), ())
    linhas += [
        "## Condições",
        "",
        f"- Motor: {MotorParakeet.nome}, CPU; um só modelo carregado para todas as variantes.",
        f"- Configuração: {relatorio.origem_config}",
        f"- Reforço: {relatorio.frases_reforcadas} frases do vocabulário do produto + "
        f"{relatorio.projetos_reforcados} nomes de projeto do config.toml; bónus {relatorio.bonus:g}.",
        f"- Divisão: semente {relatorio.semente}, fração de treino {relatorio.fracao_treino:g}, "
        "estratificada pelo caso do guião; as gravações de treino vão todas para o treino.",
        "- O léxico aprende-se só com o lado do treino; tudo o que está abaixo em 'Resultados' é só o lado do teste.",
        "- Latência: só a inferência, modelo carregado, aquecimento descartado. Sem e com reforço alternam a "
        "ordem em cada frase; 'só léxico' e 'reforço + léxico' reaproveitam essa descodificação e somam o tempo "
        "da correção.",
        "- Esta evidência não leva transcrições, frases lidas, regras do léxico nem áudio.",
        "",
        "## Divisão",
        "",
        f"- Treino ({len(divisao.treino)} de recordings/en): {_lista(divisao.treino)}",
        f"- Treino da pasta de treino ({len(relatorio.ids_do_treino_extra)}): {_lista(relatorio.ids_do_treino_extra)}",
        f"- Teste ({len(divisao.teste)}): {_lista(divisao.teste)}",
        f"- Em falta em recordings/en: {relatorio.em_falta}; em falta no guião de treino: "
        f"{relatorio.em_falta_no_treino}; inválidas (excluídas): {relatorio.invalidas}",
        "",
    ]
    if relatorio.pendente:
        linhas += [f"**PENDENTE — {relatorio.pendente}.**", "", passo_do_sponsor(), ""]
        return "\n".join(linhas)
    if relatorio.saltado:
        linhas += [f"**SALTADO:** {medir.celula_markdown(relatorio.saltado)}", ""]
        return "\n".join(linhas)
    if not relatorio.reforco_disponivel:
        linhas += ["**Reforço indisponível** (gancho do onnx-asr em falta ou avariado): só o léxico foi medido.", ""]
    aprendido = relatorio.lexico
    if aprendido is not None:
        linhas += [
            "## Léxico",
            "",
            f"- Regras candidatas: {aprendido.candidatas}; aceites: {len(aprendido.regras)} "
            "(escritas em models/adaptacao/lexico-en.json, ignorado pelo Git).",
        ]
        linhas += [f"- Recusadas — {motivo}: {n}" for motivo, n in sorted(aprendido.recusadas.items())]
        linhas.append("")
    if not relatorio.medido:
        linhas += ["Sem medição (correr com `--medir`).", ""]
        return "\n".join(linhas)
    base = relatorio.resultados[BASE]
    linhas += [
        "## Resultados no teste",
        "",
        "| variante | n | WER | intenção preservada | intenção certa | projeto certo | p50 | p95 | p50 / sem adaptação | meta |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for variante in VARIANTES:
        r = relatorio.resultados.get(variante)
        if r is None:
            linhas.append(f"| {variante} | — | não medido | — | — | — | — | — | — | — |")
            continue
        razao = r.p50_ms / base.p50_ms if base.p50_ms else float("nan")
        meta = "—" if variante == BASE else ("cumpre" if relatorio.cumpre(variante) else "não cumpre")
        linhas.append(
            f"| {variante} | {r.n} | {100 * r.wer:.1f}% | {_pct(r.intencao_preservada, r.n)} | "
            f"{_pct(r.intencao_certa, r.n)} | {_pct(r.projeto_certo, r.n)} | {r.p50_ms:.0f} ms | "
            f"{r.p95_ms:.0f} ms | {razao:.2f}x | {meta} |"
        )
    linhas += [
        "",
        "Meta: WER mais baixo e intenção preservada mais alta do que 'sem adaptação' nesta mesma execução, "
        f"e p50 no máximo {LIMITE_DA_LATENCIA:g}x o de 'sem adaptação'.",
        "",
        "## Veredito",
        "",
    ]
    melhor = relatorio.melhor
    if melhor is not None:
        r = relatorio.resultados[melhor]
        linhas += [
            f"**META CUMPRIDA no teste por '{melhor}':** WER {100 * base.wer:.1f}% → {100 * r.wer:.1f}%, "
            f"intenção preservada {_pct(base.intencao_preservada, base.n)} → {_pct(r.intencao_preservada, r.n)}, "
            f"p50 {base.p50_ms:.0f} → {r.p50_ms:.0f} ms.",
            "",
        ]
    else:
        teto = " A intenção preservada sem adaptação já está no máximo, por isso não pode subir." if (
            base.intencao_preservada == base.n
        ) else ""
        linhas += [
            f"**META NÃO CUMPRIDA:** nenhuma variante tem, ao mesmo tempo, WER mais baixo, intenção preservada "
            f"mais alta e p50 até {LIMITE_DA_LATENCIA:g}x no conjunto de teste.{teto}",
            "",
            passo_do_sponsor(),
            "",
        ]
    linhas += ["Configuração que corresponde (em config.toml):", "", "```toml", config_recomendada(relatorio), "```", ""]
    return "\n".join(linhas)


# --- Linha de comandos -------------------------------------------------------------


def _bonus(texto: str) -> float:
    try:
        valor = float(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bonus '{texto}' nao e um numero") from None
    if not 0 < valor <= BONUS_DE_REFORCO_MAXIMO:
        raise argparse.ArgumentTypeError(f"bonus tem de ser maior do que 0 e no maximo {BONUS_DE_REFORCO_MAXIMO:g}")
    return valor


def _fracao(texto: str) -> float:
    try:
        valor = float(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"fracao '{texto}' nao e um numero") from None
    if not 0 < valor < 1:
        raise argparse.ArgumentTypeError("a fracao de treino tem de estar entre 0 e 1")
    return valor


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aprende o lexico de correcoes com o lado do treino e, com --medir, mede no lado do teste.",
        epilog="Nunca toca som. Lexico em models/adaptacao/, evidencia em docs/forja/evidence/ (ambas ignoradas).",
    )
    parser.add_argument("--medir", action="store_true", help="medir antes/depois no lado do teste e escrever a evidencia")
    parser.add_argument("--semente", type=int, default=SEMENTE_PADRAO)
    parser.add_argument("--fracao-treino", type=_fracao, default=FRACAO_DE_TREINO_PADRAO)
    parser.add_argument("--bonus", type=_bonus, default=None, help="forca do reforco (por omissao a do config.toml)")
    parser.add_argument("--pasta", default=None, help="pasta das gravacoes, dentro de recordings/")
    parser.add_argument("--lexico", default=str(CAMINHO_DO_LEXICO), help="ficheiro .json dentro de models/adaptacao/")
    parser.add_argument("--saida", default=None, help="ficheiro .md dentro de docs/forja/evidence/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    try:
        pasta = gravar.pasta_de_gravacoes(args.pasta)
        caminho_do_lexico = caminho_do_lexico_de_saida(args.lexico)
        carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
        saida = medir.caminho_evidencia_de_saida(args.saida or f"docs/forja/evidence/adaptar-sotaque-{carimbo}.md")
    except ValueError as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    config, origem = medir.carregar_config_para_arnes()
    bonus = args.bonus if args.bonus is not None else config.adaptacao.bonus
    frases = frases_de_reforco(config)
    relatorio = Relatorio(
        semente=args.semente,
        fracao_treino=args.fracao_treino,
        bonus=bonus,
        origem_config=origem,
        frases_reforcadas=len(frases) - len(config.projetos),
        projetos_reforcados=len(config.projetos),
    )
    avaliacao = avaliar.carregar_gravacoes(pasta, LINGUA)
    treino_extra = avaliar.carregar_gravacoes(pasta, LINGUA, treino=True)
    transcritor = TranscritorParakeet(frases, bonus, avisar=lambda linha: print(linha, file=sys.stderr))
    correr(transcritor, avaliacao, treino_extra, config, relatorio, caminho_do_lexico, args.medir)

    if relatorio.saltado:
        print(f"SALTADO: {relatorio.saltado}")
        return 1
    if relatorio.pendente:
        print(f"PENDENTE: {relatorio.pendente}. Gravar: {COMANDO_PILOTO}")
    if relatorio.lexico is not None:
        print(
            f"Lexico: {len(relatorio.lexico.regras)} regra(s) de {relatorio.lexico.candidatas} candidata(s) -> "
            f"{caminho_para_mostrar(caminho_do_lexico)}"
        )
    if not args.medir:
        print(f"Para medir antes/depois no lado do teste: {COMANDO_MEDIR}")
        return 0
    for variante in VARIANTES:
        r = relatorio.resultados.get(variante)
        if r is not None:
            print(
                f"{variante}: WER {100 * r.wer:.1f}%, preservada {_pct(r.intencao_preservada, r.n)}, "
                f"p50 {r.p50_ms:.0f} ms" + ("  <- cumpre a meta" if relatorio.cumpre(variante) else "")
            )
    if relatorio.medido:
        print(f"Melhor: {relatorio.melhor or 'nenhuma cumpre a meta'}")
        print(config_recomendada(relatorio))
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(evidencia_markdown(relatorio), encoding="utf-8")
    print(f"Evidencia: {caminho_para_mostrar(saida)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
