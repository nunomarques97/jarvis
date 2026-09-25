r"""Mede a palavra de ativacao na voz real do Sponsor: detecao e falsos despertares.

Tres passos, descritos para o Sponsor em `tests/voz/guiao-ativacao.md`:

  --gravar-palavra   grava as 20 repeticoes da palavra de ativacao da lingua
                     (tabela do guiao) em recordings/ativacao/<lingua>/;
                     `--conjunto treino` grava um conjunto separado, so para o
                     treino do modelo portugues, em recordings/ativacao/<lingua>-treino/.
  --gravar-ruido     grava 30 min de ruido normal da casa, em blocos de 5 min,
                     em recordings/ativacao/ruido/ (retoma onde parou).
  (sem modo)         mede e escreve a evidencia em docs/forja/evidence/.

A MEDICAO corre cada gravacao pelo MESMO detetor do ouvido
(`jarvis.ouvido.DetetorOpenWakeWord`, o modelo openWakeWord da lingua), em
passos de 80 ms, que e o passo do proprio openWakeWord:

  detecao            uma repeticao conta como detetada a um limiar quando o
                     score maximo dela (com 0,5 s de silencio antes e 1 s
                     depois) chega ao limiar. Meta: >= 95%.
  falsos despertares cada vez que o score do ruido chega ao limiar conta um
                     despertar; os passos seguintes na janela mais curta em
                     que o ouvido ignora o detetor depois de uma ativacao
                     (~0,94 s: surdez inicial, 90 ms de fala e 0,6 s de
                     silencio) nao contam outra vez. No ouvido a janela real
                     e quase sempre maior (5 s sem fala), por isso a conta
                     nunca fica abaixo do que o Sponsor ouviria. O numero e
                     normalizado a 30 min. Meta: <= 1 por 30 min.

A curva limiar -> detecao/falsos vai de 0,05 a 0,95 em passos de 0,05. O
limiar escolhido e o do MEIO da faixa de limiares que cumprem as duas metas
(margem para os dois lados); sem nenhum que cumpra, o que da mais detecao
com os falsos dentro da meta, ou, sem esse, o que da menos falsos. A
evidencia diz a linha a escrever no config.toml.

Sem as 20 repeticoes validas, sem 30 min de ruido ou sem o modelo da lingua,
a evidencia diz "PENDENTE — passo do Sponsor", lista o que falta e a meta NAO
e declarada cumprida (mesmo que a curva parcial pareca boa). Nunca se usa voz
sintetica para decidir.

Nunca toca som: so a gravacao, e so com --com-som, da um bip antes de cada
repeticao. O audio fica em recordings/ (ignorada pelo Git); a evidencia leva
so ids, duracoes e scores.

Uso:
    .venv\Scripts\python scripts/avaliar_ativacao.py --gravar-palavra --lingua en
    .venv\Scripts\python scripts/avaliar_ativacao.py --gravar-ruido
    .venv\Scripts\python scripts/avaliar_ativacao.py --lingua en [--verificar]
    .venv\Scripts\python scripts/avaliar_ativacao.py --autoteste
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import math
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    TAXA_AMOSTRAGEM_PADRAO,
    caminho_evidencia_de_saida,
    escrever_wav_pcm16,
    ler_wav_pcm16,
    pico_pcm16,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.ouvido import (  # noqa: E402
    CHUNKS_PARA_COMECAR_A_FALA,
    DURACAO_DO_CHUNK_S,
    PALAVRAS_DE_ATIVACAO,
    SILENCIO_FINAL_S,
    SURDEZ_APOS_ATIVACAO_S,
    modelo_de_ativacao,
)

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` por caminho, com a chave de cache dos outros arneses."""
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


gravar_voz = _carregar_modulo_irmao("gravar_voz")
microfone = gravar_voz.microfone

GUIAO = RAIZ / "tests" / "voz" / "guiao-ativacao.md"
NOME_PASTA_ATIVACAO = "ativacao"
NOME_PASTA_RUIDO = "ruido"
CONJUNTO_AVALIACAO = "avaliacao"
CONJUNTO_TREINO = "treino"
CONJUNTOS = (CONJUNTO_AVALIACAO, CONJUNTO_TREINO)
LINGUAS = tuple(PALAVRAS_DE_ATIVACAO)

REPETICOES = 20
META_DETECAO = 0.95
META_FALSOS_POR_30_MIN = 1.0
MEIA_HORA_S = 1800.0
RUIDO_MINIMO_S = MEIA_HORA_S
SEGMENTO_DE_RUIDO_S = 300.0
#: Um bloco de ruido interrompido mais curto do que isto nao se guarda.
SEGMENTO_MINIMO_S = 10.0
#: Capturas recusadas seguidas que param a gravacao do ruido.
RECUSAS_SEGUIDAS_MAXIMAS = 3

#: O passo do openWakeWord: um score novo por cada 1280 amostras (80 ms).
AMOSTRAS_POR_PASSO = 1280
PASSO_S = AMOSTRAS_POR_PASSO / TAXA_AMOSTRAGEM_PADRAO
#: O menor tempo em que o ouvido nao olha para o detetor depois de uma
#: ativacao: a surdez inicial, o minimo de fala e o silencio que fecha a frase.
#: Sem fala a escuta dura mais; com o minimo os falsos nunca ficam por baixo.
REFRATARIO_S = SURDEZ_APOS_ATIVACAO_S + CHUNKS_PARA_COMECAR_A_FALA * DURACAO_DO_CHUNK_S + SILENCIO_FINAL_S
SILENCIO_ANTES_S = 0.5
SILENCIO_DEPOIS_S = 1.0
LIMIARES = tuple(round(0.05 * i, 2) for i in range(1, 20))

ESTADO_PENDENTE = "PENDENTE — passo do Sponsor"
ESTADO_CUMPRIDA = "METAS CUMPRIDAS"
ESTADO_FALHADA = "METAS FALHADAS"


# --- O guiao -------------------------------------------------------------------

COLUNAS_DO_GUIAO = ("n", "como dizer")


class GuiaoError(Exception):
    """O guiao da palavra de ativacao nao cumpre o formato."""


def ler_guiao(caminho: Path = GUIAO) -> list[tuple[str, str]]:
    """A tabela `| n | como dizer |`: exatamente 20 linhas, numeradas 01 a 20."""
    linhas: list[tuple[str, str]] = []
    cabecalho = False
    for numero, linha in enumerate(Path(caminho).read_text(encoding="utf-8").splitlines(), start=1):
        if not linha.lstrip().startswith("|"):
            continue
        celulas = gravar_voz._celulas(linha)
        if not cabecalho:
            if tuple(celulas) == COLUNAS_DO_GUIAO:
                cabecalho = True
            continue
        if all(set(c) <= set(":-") for c in celulas):
            continue
        if len(celulas) != 2 or not celulas[1]:
            raise GuiaoError(f"{Path(caminho).name}:{numero}: esperava '| n | como dizer |'")
        linhas.append((celulas[0], celulas[1]))
    esperados = [f"{n:02d}" for n in range(1, REPETICOES + 1)]
    if [n for n, _ in linhas] != esperados:
        raise GuiaoError(
            f"{Path(caminho).name}: a tabela tem de ter as linhas {esperados[0]} a {esperados[-1]} por ordem"
        )
    return linhas


def id_da_repeticao(lingua: str, n: str, conjunto: str = CONJUNTO_AVALIACAO) -> str:
    prefixo = "ativ" if conjunto == CONJUNTO_AVALIACAO else "treino"
    return f"{prefixo}-{lingua}-{n}"


def frases_para_gravar(lingua: str, conjunto: str = CONJUNTO_AVALIACAO, guiao: Path = GUIAO):
    """As repeticoes do guiao no formato que `gravar_voz.gravar_guiao` grava."""
    palavra = PALAVRAS_DE_ATIVACAO[lingua]
    return [
        gravar_voz.FraseDoGuiao(
            id=id_da_repeticao(lingua, n, conjunto),
            caso="ativacao",
            frase=f"{palavra}      [{como}]",
            intencao="desconhecido",
            projeto=None,
            ativacao=True,
        )
        for n, como in ler_guiao(guiao)
    ]


# --- Pastas e gravacoes ----------------------------------------------------------


def pasta_base(raiz: Path | None = None) -> Path:
    """recordings/ativacao/, validada como qualquer pasta de gravacoes."""
    raiz = RAIZ if raiz is None else Path(raiz)
    return gravar_voz.pasta_de_gravacoes(
        Path(gravar_voz.NOME_PASTA_GRAVACOES) / NOME_PASTA_ATIVACAO, raiz
    )


def nome_da_pasta_da_palavra(lingua: str, conjunto: str = CONJUNTO_AVALIACAO) -> str:
    return lingua if conjunto == CONJUNTO_AVALIACAO else f"{lingua}-{CONJUNTO_TREINO}"


def gravacoes_validas(
    pasta: Path, ids: Iterable[str] | None = None, *, origem: str | None = gravar_voz.ORIGEM_MICROFONE
) -> tuple[list[tuple[str, Path]], dict[str, str]]:
    """(id, WAV) das gravacoes do manifesto que existem e passam a validacao do gravador.

    Devolve tambem id -> motivo das que existem mas nao servem (silencio
    digital, mais audio do que tempo real, duracao diferente do manifesto) ou
    que nao tem a `origem` pedida. Por omissao so conta o que veio do
    microfone: audio sintetico nunca passa por voz do Sponsor. `origem=None`
    aceita qualquer origem.
    """
    manifesto = gravar_voz.ler_manifesto(pasta)
    invalidas = gravar_voz.gravacoes_invalidas(pasta, manifesto)
    if origem is not None:
        for id_ in sorted(gravar_voz.ja_gravadas(pasta, manifesto) - set(invalidas)):
            registada = manifesto["gravacoes"][id_].get("origem")
            if registada != origem:
                invalidas[id_] = f"origem '{registada}' no manifesto, nao '{origem}': nao conta como voz do Sponsor"
    existentes = gravar_voz.ja_gravadas(pasta, manifesto) - set(invalidas)
    if ids is not None:
        permitidos = set(ids)
        existentes &= permitidos
        invalidas = {i: m for i, m in invalidas.items() if i in permitidos}
    registos = manifesto.get("gravacoes", {})
    return [(i, pasta / str(registos[i]["ficheiro"])) for i in sorted(existentes)], invalidas


def pcm_mono_16k(caminho: Path) -> bytes:
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if taxa != TAXA_AMOSTRAGEM_PADRAO or canais != 1:
        raise ValueError(f"{caminho.name}: esperava 16 kHz mono (o gravador escreve assim)")
    return dados


# --- Scores e curva ---------------------------------------------------------------


def _passos(pcm16: bytes) -> list[bytes]:
    tamanho = AMOSTRAS_POR_PASSO * 2
    return [
        pcm16[i : i + tamanho].ljust(tamanho, b"\x00") for i in range(0, len(pcm16), tamanho)
    ]


def _silencio(segundos: float) -> bytes:
    return b"\x00\x00" * int(TAXA_AMOSTRAGEM_PADRAO * segundos)


def serie_de_scores(detetor, pcm16: bytes) -> list[float]:
    """Um score por passo de 80 ms, com o detetor reiniciado antes."""
    detetor.reiniciar()
    return [float(detetor.processar(passo)) for passo in _passos(pcm16)]


def score_maximo(detetor, pcm16: bytes) -> float:
    """O score mais alto de uma repeticao, com silencio antes e depois."""
    serie = serie_de_scores(detetor, _silencio(SILENCIO_ANTES_S) + pcm16 + _silencio(SILENCIO_DEPOIS_S))
    return max(serie, default=0.0)


def contar_despertares(
    serie: Sequence[float], limiar: float, passo_s: float = PASSO_S, refratario_s: float = REFRATARIO_S
) -> int:
    """Despertares numa serie de scores: cada passo >= limiar fora do refratario do anterior."""
    despertares = 0
    ultimo: float | None = None
    for indice, score in enumerate(serie):
        instante = indice * passo_s
        if score >= limiar and (ultimo is None or instante - ultimo >= refratario_s):
            despertares += 1
            ultimo = instante
    return despertares


@dataclass(frozen=True)
class PontoDaCurva:
    limiar: float
    detetadas: int
    repeticoes: int
    falsos: int
    ruido_s: float

    @property
    def detecao(self) -> float:
        return self.detetadas / self.repeticoes if self.repeticoes else 0.0

    @property
    def falsos_por_30_min(self) -> float:
        return self.falsos * MEIA_HORA_S / self.ruido_s if self.ruido_s > 0 else math.inf

    @property
    def cumpre(self) -> bool:
        return (
            self.repeticoes > 0
            and self.ruido_s > 0
            and self.detecao >= META_DETECAO
            and self.falsos_por_30_min <= META_FALSOS_POR_30_MIN
        )


def calcular_curva(
    maximos: Sequence[float],
    series_de_ruido: Sequence[Sequence[float]],
    ruido_s: float,
    limiares: Sequence[float] = LIMIARES,
) -> list[PontoDaCurva]:
    return [
        PontoDaCurva(
            limiar=limiar,
            detetadas=sum(1 for m in maximos if m >= limiar),
            repeticoes=len(maximos),
            falsos=sum(contar_despertares(serie, limiar) for serie in series_de_ruido),
            ruido_s=ruido_s,
        )
        for limiar in limiares
    ]


def escolher_limiar(curva: Sequence[PontoDaCurva]) -> PontoDaCurva | None:
    """O meio da faixa que cumpre as duas metas; sem faixa, o melhor recurso."""
    if not curva:
        return None
    cumprem = [p for p in curva if p.cumpre]
    if cumprem:
        return cumprem[(len(cumprem) - 1) // 2]
    dentro_dos_falsos = [p for p in curva if p.falsos_por_30_min <= META_FALSOS_POR_30_MIN]
    if dentro_dos_falsos:
        return max(dentro_dos_falsos, key=lambda p: (p.detecao, -p.limiar))
    return min(curva, key=lambda p: (p.falsos_por_30_min, -p.detecao, p.limiar))


# --- Medicao --------------------------------------------------------------------


@dataclass
class Medicao:
    lingua: str
    modelo: Path
    modelo_sha256: str | None
    positivos: list[tuple[str, float, float]] = field(default_factory=list)  # id, duracao_s, score maximo
    ruido: list[tuple[str, float, int]] = field(default_factory=list)  # id, duracao_s, passos >= 0,5
    ruido_s: float = 0.0
    curva: list[PontoDaCurva] = field(default_factory=list)
    escolhido: PontoDaCurva | None = None
    pendencias: list[str] = field(default_factory=list)
    invalidas: dict[str, str] = field(default_factory=dict)

    @property
    def estado(self) -> str:
        if self.pendencias:
            return ESTADO_PENDENTE
        return ESTADO_CUMPRIDA if self.escolhido is not None and self.escolhido.cumpre else ESTADO_FALHADA


def mostrar(caminho: Path) -> str:
    """Relativo a raiz do repositorio; fora dele, so o nome (nunca o disco do Sponsor)."""
    try:
        return Path(caminho).resolve().relative_to(RAIZ).as_posix()
    except ValueError:
        return Path(caminho).name


def sha256_de(caminho: Path) -> str:
    return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()


def comando(argumentos: str) -> str:
    return rf".venv\Scripts\python scripts/avaliar_ativacao.py {argumentos}"


def medir(
    lingua: str,
    modelo: Path,
    pasta_palavra: Path,
    pasta_ruido: Path,
    criar_detetor: Callable[[Path], object],
    *,
    guiao: Path = GUIAO,
    escrever: Callable[[str], object] = print,
) -> Medicao:
    """Corre as gravacoes validas pelo detetor e monta a curva (mesmo que pendente)."""
    modelo = Path(modelo)
    medicao = Medicao(lingua, modelo, sha256_de(modelo) if modelo.is_file() else None)
    ids = [id_da_repeticao(lingua, n) for n, _ in ler_guiao(guiao)]
    positivos, invalidas = gravacoes_validas(pasta_palavra, ids)
    segmentos, invalidas_ruido = gravacoes_validas(pasta_ruido)
    medicao.invalidas = {**invalidas, **invalidas_ruido}

    if len(positivos) < REPETICOES:
        medicao.pendencias.append(
            f"gravar as repeticoes da palavra: {len(positivos)} de {REPETICOES} validas "
            f"({comando(f'--gravar-palavra --lingua {lingua}')})"
        )
    duracoes = {i: len(pcm_mono_16k(c)) / 2 / TAXA_AMOSTRAGEM_PADRAO for i, c in segmentos}
    medicao.ruido_s = sum(duracoes.values())
    if medicao.ruido_s < RUIDO_MINIMO_S:
        medicao.pendencias.append(
            f"gravar o ruido: {medicao.ruido_s / 60:.1f} de {RUIDO_MINIMO_S / 60:.0f} min "
            f"({comando('--gravar-ruido')})"
        )
    if medicao.modelo_sha256 is None:
        passo = (
            r"treinar com .venv\Scripts\python scripts/treinar_ativacao.py (docs/MODELOS.md)"
            if lingua == "pt"
            else "descarregar como diz docs/MODELOS.md"
        )
        medicao.pendencias.append(f"o modelo '{mostrar(modelo)}' nao existe: {passo}")
        return medicao
    if not positivos and not segmentos:
        return medicao

    detetor = criar_detetor(modelo)
    for id_, caminho in positivos:
        pcm = pcm_mono_16k(caminho)
        maximo = score_maximo(detetor, pcm)
        medicao.positivos.append((id_, len(pcm) / 2 / TAXA_AMOSTRAGEM_PADRAO, maximo))
        escrever(f"  {id_:<16} score maximo {maximo:.3f}")
    series: list[list[float]] = []
    for id_, caminho in segmentos:
        serie = serie_de_scores(detetor, pcm_mono_16k(caminho))
        series.append(serie)
        medicao.ruido.append((id_, duracoes[id_], sum(1 for s in serie if s >= 0.5)))
        escrever(f"  {id_:<16} {duracoes[id_] / 60:5.1f} min de ruido, score maximo {max(serie, default=0.0):.3f}")
    medicao.curva = calcular_curva([m for _, _, m in medicao.positivos], series, medicao.ruido_s)
    medicao.escolhido = escolher_limiar(medicao.curva)
    return medicao


# --- Evidencia --------------------------------------------------------------------


def _pct(valor: float) -> str:
    return f"{valor * 100:.0f}%"


def _falsos(ponto: PontoDaCurva) -> str:
    return "—" if math.isinf(ponto.falsos_por_30_min) else f"{ponto.falsos_por_30_min:.2f}"


def texto_da_evidencia(medicao: Medicao, agora: datetime) -> str:
    palavra = PALAVRAS_DE_ATIVACAO[medicao.lingua]
    linhas = [
        f"# Palavra de ativacao — '{palavra}' ({medicao.lingua}) — {agora:%Y-%m-%d %H:%M}",
        "",
        f"**Estado: {medicao.estado}**",
        "",
        f"- Metas: detecao >= {_pct(META_DETECAO)} nas {REPETICOES} repeticoes do Sponsor; "
        f"<= {META_FALSOS_POR_30_MIN:.0f} falso despertar por 30 min de ruido domestico.",
        f"- Modelo: `{mostrar(medicao.modelo)}`"
        + (f" (sha256 `{medicao.modelo_sha256}`)" if medicao.modelo_sha256 else " (em falta)"),
        f"- Repeticoes validas: {len(medicao.positivos)} de {REPETICOES}; ruido valido: "
        f"{medicao.ruido_s / 60:.1f} min em {len(medicao.ruido)} bloco(s).",
        f"- Metodo: detetor do ouvido (`jarvis.ouvido.DetetorOpenWakeWord`) em passos de "
        f"{PASSO_S * 1000:.0f} ms; detecao = score maximo da repeticao (com {SILENCIO_ANTES_S} s "
        f"de silencio antes e {SILENCIO_DEPOIS_S} s depois) >= limiar; falso despertar = passo do "
        f"ruido >= limiar fora dos {REFRATARIO_S:.2f} s a seguir ao anterior, normalizado a 30 min.",
        "",
    ]
    if medicao.pendencias:
        linhas += [
            "## PENDENTE — passo do Sponsor",
            "",
            "A meta NAO e declarada cumprida sem as gravacoes do Sponsor. Falta:",
            "",
            *[f"1. {p}" for p in medicao.pendencias],
            "",
            "Instrucoes completas: `tests/voz/guiao-ativacao.md`. Depois, voltar a medir com "
            f"`{comando(f'--lingua {medicao.lingua}')}`.",
            "",
        ]
    if medicao.invalidas:
        linhas += ["## Gravacoes postas de parte", ""]
        linhas += [f"- `{i}`: {motivo}" for i, motivo in sorted(medicao.invalidas.items())]
        linhas.append("")
    if medicao.curva:
        titulo = "## Curva limiar -> detecao / falsos despertares"
        if medicao.pendencias:
            titulo += " (PARCIAL: gravacoes incompletas, nao decide nada)"
        linhas += [
            titulo,
            "",
            "| limiar | detetadas | detecao | falsos | falsos por 30 min | cumpre |",
            "|---|---|---|---|---|---|",
        ]
        for p in medicao.curva:
            marca = " **<- escolhido**" if p is medicao.escolhido and not medicao.pendencias else ""
            linhas.append(
                f"| {p.limiar:.2f} | {p.detetadas}/{p.repeticoes} | {_pct(p.detecao)} | {p.falsos} | "
                f"{_falsos(p)} | {'sim' if p.cumpre else 'nao'}{marca} |"
            )
        linhas.append("")
    if medicao.escolhido is not None and not medicao.pendencias:
        e = medicao.escolhido
        linhas += [
            "## Limiar escolhido pelos numeros",
            "",
            f"- Limiar **{e.limiar:.2f}**: detecao {e.detetadas}/{e.repeticoes} ({_pct(e.detecao)}), "
            f"{e.falsos} falso(s) em {medicao.ruido_s / 60:.1f} min ({_falsos(e)} por 30 min).",
            "- Regra: o meio da faixa de limiares que cumprem as duas metas; sem faixa, o de mais "
            "detecao com os falsos dentro da meta; sem esse, o de menos falsos.",
            f"- Escrever no `config.toml`, tabela `[ouvido]`: `limiar_ativacao = {e.limiar:.2f}`",
            "",
        ]
    if medicao.positivos:
        linhas += ["## Repeticoes", "", "| id | duracao | score maximo |", "|---|---|---|"]
        linhas += [f"| {i} | {d:.2f} s | {m:.3f} |" for i, d, m in medicao.positivos]
        linhas.append("")
    if medicao.ruido:
        linhas += ["## Ruido", "", "| bloco | duracao | passos com score >= 0,5 |", "|---|---|---|"]
        linhas += [f"| {i} | {d / 60:.1f} min | {n} |" for i, d, n in medicao.ruido]
        linhas.append("")
    return "\n".join(linhas)


def escrever_evidencia(medicao: Medicao, saida: Path, agora: datetime | None = None) -> Path:
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(texto_da_evidencia(medicao, agora or datetime.now()), encoding="utf-8")
    return saida


# --- Gravacao do ruido ------------------------------------------------------------


@dataclass
class ResumoDoRuido:
    gravados: list[str] = field(default_factory=list)
    recusados: int = 0
    total_s: float = 0.0
    interrompido: bool = False


def _proximo_id(manifesto: dict) -> str:
    numeros = [
        int(m.group(1))
        for i in manifesto.get("gravacoes", {})
        if (m := re.fullmatch(r"ruido-(\d+)", i))
    ]
    return f"ruido-{max(numeros, default=0) + 1:02d}"


def gravar_ruido(
    pasta: Path,
    captura,
    parar: threading.Event,
    *,
    alvo_s: float = RUIDO_MINIMO_S,
    segmento_s: float = SEGMENTO_DE_RUIDO_S,
    raiz: Path | None = None,
    escrever: Callable[[str], object] = print,
) -> ResumoDoRuido:
    """Grava blocos de ruido ate haver `alvo_s` validos ou `parar` ser dado.

    Cada bloco passa a validacao do gravador (sinal, silencio digital, tempo
    real) antes de ser escrito; o manifesto e escrito a cada bloco. Nunca
    reescreve um bloco: os novos levam sempre o numero seguinte.
    """
    raiz = RAIZ if raiz is None else Path(raiz)
    # Retoma so com blocos da mesma origem da captura: um bloco de outra
    # origem nao conta para os 30 min.
    validos, invalidos = gravacoes_validas(pasta, origem=captura.origem)
    resumo = ResumoDoRuido()
    resumo.total_s = sum(len(pcm_mono_16k(c)) / 2 / TAXA_AMOSTRAGEM_PADRAO for _, c in validos)
    for id_, motivo in invalidos.items():
        escrever(f"  bloco {id_} posto de parte: {motivo}")
    if resumo.total_s >= alvo_s:
        escrever(f"Nada a gravar: ja ha {resumo.total_s / 60:.1f} min de ruido valido.")
        return resumo
    manifesto = gravar_voz.ler_manifesto(pasta)
    manifesto.setdefault("versao", 1)
    manifesto.setdefault("gravacoes", {})
    captura.abrir()
    escrever(f"Microfone: {captura.descricao}")
    recusas_seguidas = 0
    try:
        while resumo.total_s < alvo_s and not parar.is_set():
            falta = alvo_s - resumo.total_s
            escrever(f"  a gravar ruido ({resumo.total_s / 60:.1f} de {alvo_s / 60:.0f} min); Enter para parar")
            feita = captura.gravar(parar, min(segmento_s, falta))
            pcm = feita.pcm
            duracao = len(pcm) / 2 / TAXA_AMOSTRAGEM_PADRAO
            if duracao < SEGMENTO_MINIMO_S:
                escrever(f"  bloco de {duracao:.0f} s e curto demais; nao fica")
                break
            motivo = None
            if pico_pcm16(pcm) == 0:
                motivo = "sem sinal (microfone em mudo ou sem permissao)"
            else:
                motivo = microfone.defeito_da_captura(pcm, feita.segundos_reais, so_excesso=True)
            if motivo:
                resumo.recusados += 1
                recusas_seguidas += 1
                escrever(f"  AVISO: bloco recusado ({motivo})")
                if recusas_seguidas >= RECUSAS_SEGUIDAS_MAXIMAS:
                    raise OSError(f"{recusas_seguidas} blocos recusados seguidos; verificar o microfone")
                continue
            recusas_seguidas = 0
            id_ = _proximo_id(manifesto)
            ficheiro = f"{id_}.wav"
            caminho = gravar_voz.caminho_wav_de_saida(pasta / ficheiro, raiz)
            escrever_wav_pcm16(caminho, pcm, TAXA_AMOSTRAGEM_PADRAO, canais=1)
            manifesto["gravacoes"][id_] = {
                "ficheiro": ficheiro,
                "origem": captura.origem,
                "duracao_s": round(duracao, 3),
                "tempo_real_s": round(feita.segundos_reais, 3),
                "gravado_em": datetime.now().isoformat(timespec="seconds"),
            }
            gravar_voz.escrever_manifesto(pasta, manifesto)
            resumo.gravados.append(id_)
            resumo.total_s += duracao
            escrever(f"  bloco {id_} gravado: {duracao / 60:.1f} min")
    finally:
        captura.fechar()
    resumo.interrompido = parar.is_set() and resumo.total_s < alvo_s
    return resumo


# --- CLI --------------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mede a palavra de ativacao na voz do Sponsor (detecao e falsos despertares).",
        epilog="Nunca toca som (so --com-som, ao gravar). Audio em recordings/; evidencia em docs/forja/evidence/.",
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--gravar-palavra", action="store_true", help="grava as 20 repeticoes do guiao")
    modo.add_argument("--gravar-ruido", action="store_true", help="grava 30 min de ruido normal, em blocos")
    modo.add_argument("--autoteste", action="store_true", help="modelo e audio falsos; sem microfone nem som")
    parser.add_argument("--lingua", choices=LINGUAS, default=None, help="por omissao a do config.toml")
    parser.add_argument("--conjunto", choices=CONJUNTOS, default=CONJUNTO_AVALIACAO,
                        help="com --gravar-palavra: 'treino' so para o modelo portugues")
    parser.add_argument("--modelo", default=None, help="modelo openWakeWord a medir (por omissao o da lingua)")
    parser.add_argument("--saida", default=None, help="ficheiro .md dentro de docs/forja/evidence/")
    parser.add_argument("--verificar", action="store_true", help="sai com erro se a evidencia nao cumprir as metas")
    parser.add_argument("--com-som", action="store_true", help="bip antes de cada repeticao gravada")
    return parser


def _config():
    medir_voz = _carregar_modulo_irmao("medir_voz")
    return medir_voz.carregar_config_para_arnes()


def _detetor_real(modelo: Path):
    from jarvis.ouvido import DetetorOpenWakeWord

    return DetetorOpenWakeWord(modelo)


def _esperar_enter(parar: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass
    parar.set()


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    config, origem = _config()
    lingua = args.lingua or config.ouvido.lingua
    try:
        base = pasta_base()
    except ValueError as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2

    if args.gravar_palavra:
        subpasta = nome_da_pasta_da_palavra(lingua, args.conjunto)
        try:
            frases = frases_para_gravar(lingua, args.conjunto)
        except GuiaoError as erro:
            print(f"erro: {erro}", file=sys.stderr)
            return 2
        print(f"Configuracao: {origem}. Palavra: '{PALAVRAS_DE_ATIVACAO[lingua]}' ({args.conjunto}).")
        print("Diz so a palavra, da maneira entre [ ]; nada a seguir.")
        captura = gravar_voz.CapturaPyAudio(config.microfone)
        try:
            resumo = gravar_voz.gravar_guiao(
                subpasta, frases, {}, base, captura, com_som=args.com_som, guiao=GUIAO
            )
        except KeyboardInterrupt:
            print("\nInterrompido. O que ja foi gravado fica; o mesmo comando continua.")
            return 130
        except OSError as erro:
            print(f"erro do microfone: {erro}", file=sys.stderr)
            return 1
        print(f"Gravadas agora: {len(resumo.gravadas)}; ja existiam: {len(resumo.ja_existiam)}.")
        return 0

    if args.gravar_ruido:
        print(f"Configuracao: {origem}. {RUIDO_MINIMO_S / 60:.0f} min de ruido normal; NAO dizer a palavra de ativacao.")
        if input("Enter para comecar (q para sair): ").strip().lower() == "q":
            return 0
        parar = threading.Event()
        threading.Thread(target=_esperar_enter, args=(parar,), daemon=True).start()
        try:
            resumo = gravar_ruido(base / NOME_PASTA_RUIDO, gravar_voz.CapturaPyAudio(config.microfone), parar)
        except KeyboardInterrupt:
            print("\nInterrompido. Os blocos ja gravados ficam; o mesmo comando continua.")
            return 130
        except OSError as erro:
            print(f"erro do microfone: {erro}", file=sys.stderr)
            return 1
        print(f"Blocos gravados agora: {len(resumo.gravados)}; ruido valido: {resumo.total_s / 60:.1f} min.")
        return 0

    modelo = Path(args.modelo) if args.modelo else modelo_de_ativacao(lingua)
    carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        saida = caminho_evidencia_de_saida(args.saida or f"docs/forja/evidence/ativacao-{lingua}-{carimbo}.md")
    except ValueError as erro:
        print(f"erro: {erro}", file=sys.stderr)
        return 2
    print(f"Palavra '{PALAVRAS_DE_ATIVACAO[lingua]}', modelo {mostrar(modelo)}")
    medicao = medir(
        lingua, modelo, base / nome_da_pasta_da_palavra(lingua), base / NOME_PASTA_RUIDO, _detetor_real
    )
    escrever_evidencia(medicao, saida)
    print()
    print(f"Estado: {medicao.estado}")
    for pendencia in medicao.pendencias:
        print(f"  falta: {pendencia}")
    if medicao.escolhido is not None and not medicao.pendencias:
        e = medicao.escolhido
        print(f"Limiar escolhido {e.limiar:.2f}: detecao {_pct(e.detecao)}, {_falsos(e)} falsos por 30 min")
        print(f"  config.toml [ouvido]: limiar_ativacao = {e.limiar:.2f}")
    print(f"Evidencia: {mostrar(saida)}")
    if args.verificar and medicao.estado != ESTADO_CUMPRIDA:
        return 1
    return 0


# --- Autoteste: modelo e audio falsos, sem microfone nem som ----------------------


class DetetorFalso:
    """Score = pico do passo / 32767: a repeticao sintetica escolhe o seu score."""

    def __init__(self, _modelo: Path | None = None) -> None:
        self.reinicios = 0

    def processar(self, passo: bytes) -> float:
        return pico_pcm16(passo) / 32767

    def reiniciar(self) -> None:
        self.reinicios += 1


def pcm_com_pico(segundos: float, pico: float) -> bytes:
    """Um tom de 440 Hz cujo pico da, no DetetorFalso, o score pedido."""
    import numpy as np

    instantes = np.arange(int(TAXA_AMOSTRAGEM_PADRAO * segundos)) / TAXA_AMOSTRAGEM_PADRAO
    amplitude = int(round(pico * 32767))
    return (amplitude * np.sin(2 * np.pi * 440 * instantes)).astype("<i2").tobytes()


@dataclass(frozen=True)
class _CapturaFeita:
    pcm: bytes
    segundos_reais: float


class CapturaDeRuidoFalsa:
    """Devolve logo `maximo_s` de ruido fraco a cada gravacao (ou o que `pcms` mandar)."""

    origem = gravar_voz.ORIGEM_SINTETICA
    descricao = "captura falsa"

    def __init__(self, pcms: Sequence[bytes] | None = None, parar_depois: threading.Event | None = None) -> None:
        self.pcms = list(pcms or [])
        self.parar_depois = parar_depois
        self.abertas = self.fechadas = self.gravacoes = 0

    def abrir(self) -> None:
        self.abertas += 1

    def gravar(self, parar: threading.Event, maximo_s: float) -> _CapturaFeita:
        self.gravacoes += 1
        pcm = self.pcms.pop(0) if self.pcms else pcm_com_pico(maximo_s, 0.01)
        if self.parar_depois is not None:
            self.parar_depois.set()
        return _CapturaFeita(pcm, len(pcm) / 2 / TAXA_AMOSTRAGEM_PADRAO)

    def fechar(self) -> None:
        self.fechadas += 1


def _escrever_gravacoes(
    pasta: Path, gravacoes: dict[str, bytes], origem: str = gravar_voz.ORIGEM_MICROFONE
) -> None:
    """WAV falsos com manifesto; por omissao marcados como do microfone, para exercitar a cadeia."""
    pasta.mkdir(parents=True, exist_ok=True)
    manifesto = {"versao": 1, "gravacoes": {}}
    for id_, pcm in gravacoes.items():
        escrever_wav_pcm16(pasta / f"{id_}.wav", pcm, TAXA_AMOSTRAGEM_PADRAO, canais=1)
        duracao = len(pcm) / 2 / TAXA_AMOSTRAGEM_PADRAO
        manifesto["gravacoes"][id_] = {
            "ficheiro": f"{id_}.wav", "origem": origem,
            "duracao_s": round(duracao, 3), "tempo_real_s": round(duracao, 3),
        }
    gravar_voz.escrever_manifesto(pasta, manifesto)


def _autoteste() -> int:
    import tempfile

    falhas: list[str] = []

    def verificar(nome: str, condicao: bool, detalhe: object = "") -> None:
        if condicao:
            print(f"ok   {nome}")
        else:
            falhas.append(f"{nome} {detalhe}".strip())

    # 1. guiao versionado: 20 linhas, ids por lingua e por conjunto.
    linhas = ler_guiao()
    verificar("guiao: 20 repeticoes", len(linhas) == REPETICOES, len(linhas))
    frases = frases_para_gravar("en")
    verificar("guiao: ids de avaliacao", frases[0].id == "ativ-en-01" and frases[-1].id == "ativ-en-20")
    verificar("guiao: a palavra da lingua", all(f.frase.startswith("hey jarvis") for f in frases))
    verificar("guiao: treino separado", frases_para_gravar("pt", CONJUNTO_TREINO)[0].id == "treino-pt-01")

    # 2. contagem de despertares com refratario (~0,94 s = 11,7 passos).
    serie = [0.0] * 100
    for i in (10, 11, 12, 21, 30, 60):
        serie[i] = 0.9
    verificar("refratario junta passos seguidos", contar_despertares(serie, 0.5) == 3, contar_despertares(serie, 0.5))
    verificar("abaixo do limiar nao conta", contar_despertares(serie, 0.95) == 0)

    # 3. escolha do limiar: meio da faixa que cumpre.
    ruido = [0.55] + [0.0] * 30 + [0.3] + [0.0] * 70
    curva = calcular_curva([0.3] + [0.9] * 19 + [0.95] * 20, [ruido], MEIA_HORA_S)
    cumprem = [p.limiar for p in curva if p.cumpre]
    verificar("faixa que cumpre", cumprem == [0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9], cumprem)
    verificar("escolhido no meio da faixa", escolher_limiar(curva).limiar == 0.6, escolher_limiar(curva))
    sem_faixa = calcular_curva([0.2] * 20, [[0.9, 0.0] * 30], MEIA_HORA_S)
    verificar("sem faixa: nada cumpre", escolher_limiar(sem_faixa) is not None and not escolher_limiar(sem_faixa).cumpre)

    with tempfile.TemporaryDirectory(prefix="avaliar-ativacao-") as temporaria:
        raiz = Path(temporaria)
        base = pasta_base(raiz)
        evidencias = raiz / "docs" / "forja" / "evidence"
        modelo = raiz / "models" / "falso.onnx"
        modelo.parent.mkdir(parents=True)
        modelo.write_bytes(b"modelo falso")

        # 4. sem gravacoes: PENDENTE, meta nao declarada.
        medicao = medir("en", modelo, base / "en", base / NOME_PASTA_RUIDO, DetetorFalso, escrever=lambda _t: None)
        saida = escrever_evidencia(medicao, caminho_evidencia_de_saida(evidencias / "a.md", evidencias, raiz))
        texto = saida.read_text(encoding="utf-8")
        verificar("sem gravacoes: PENDENTE", medicao.estado == ESTADO_PENDENTE and ESTADO_PENDENTE in texto)
        verificar("sem gravacoes: passos do Sponsor listados", "--gravar-palavra" in texto and "--gravar-ruido" in texto)
        verificar("sem gravacoes: nenhum limiar declarado", "Limiar escolhido" not in texto and "cumpre |" not in texto)

        # 5. modelo em falta: PENDENTE com o passo do treino.
        medicao = medir("pt", raiz / "models" / "nao-existe.onnx", base / "pt", base / NOME_PASTA_RUIDO,
                        DetetorFalso, escrever=lambda _t: None)
        verificar("modelo pt em falta: aponta o treino", any("treinar_ativacao" in p for p in medicao.pendencias))

        # 6. gravacoes completas: 19 de 20 detetadas a 0,5, ruido com dois picos.
        maximos = [0.8] * 18 + [0.55, 0.3]
        _escrever_gravacoes(base / "en", {id_da_repeticao("en", f"{i + 1:02d}"): pcm_com_pico(0.6, m)
                                          for i, m in enumerate(maximos)})
        ruido = pcm_com_pico(900.0, 0.02)
        pico = pcm_com_pico(0.08, 0.6)
        meio = len(ruido) // 2 // 2560 * 2560
        _escrever_gravacoes(base / NOME_PASTA_RUIDO, {
            "ruido-01": ruido[:meio] + pico + ruido[meio + len(pico):],
            "ruido-02": ruido,
        })
        medicao = medir("en", modelo, base / "en", base / NOME_PASTA_RUIDO, DetetorFalso, escrever=lambda _t: None)
        verificar("completo: sem pendencias", medicao.pendencias == [], medicao.pendencias)
        por_limiar = {p.limiar: p for p in medicao.curva}
        verificar("curva: detecao a 0,5", por_limiar[0.5].detetadas == 19, por_limiar[0.5])
        verificar("curva: falso despertar do pico", por_limiar[0.5].falsos == 1 and por_limiar[0.65].falsos == 0)
        verificar("curva: falsos por 30 min", abs(por_limiar[0.5].falsos_por_30_min - 1.0) < 1e-9)
        verificar("escolhido cumpre", medicao.escolhido is not None and medicao.escolhido.cumpre, medicao.escolhido)
        saida = escrever_evidencia(medicao, caminho_evidencia_de_saida(evidencias / "b.md", evidencias, raiz))
        texto = saida.read_text(encoding="utf-8")
        verificar("evidencia: metas cumpridas", ESTADO_CUMPRIDA in texto and ESTADO_PENDENTE not in texto)
        verificar("evidencia: linha do config", f"limiar_ativacao = {medicao.escolhido.limiar:.2f}" in texto)
        verificar("evidencia: sem caminhos absolutos", temporaria not in texto and str(RAIZ) not in texto)

        # 6b. as mesmas gravacoes marcadas como sinteticas nao contam: PENDENTE, nenhum limiar.
        sinteticas = raiz / "sinteticas"
        _escrever_gravacoes(sinteticas / "en", {id_da_repeticao("en", f"{i + 1:02d}"): pcm_com_pico(0.6, m)
                                                for i, m in enumerate(maximos)}, gravar_voz.ORIGEM_SINTETICA)
        _escrever_gravacoes(sinteticas / NOME_PASTA_RUIDO, {"ruido-01": ruido, "ruido-02": ruido},
                            gravar_voz.ORIGEM_SINTETICA)
        medicao = medir("en", modelo, sinteticas / "en", sinteticas / NOME_PASTA_RUIDO, DetetorFalso,
                        escrever=lambda _t: None)
        texto = texto_da_evidencia(medicao, datetime(2026, 1, 1))
        verificar("sinteticas: PENDENTE", medicao.estado == ESTADO_PENDENTE, medicao.pendencias)
        verificar("sinteticas: postas de parte", "ativ-en-01" in medicao.invalidas and "ruido-01" in medicao.invalidas)
        verificar("sinteticas: nenhum limiar declarado", "limiar_ativacao =" not in texto and not medicao.positivos)

        # 7. uma repeticao com silencio digital fica de parte e volta a PENDENTE.
        _escrever_gravacoes(base / "en", {**{id_da_repeticao("en", f"{i + 1:02d}"): pcm_com_pico(0.6, 0.8)
                                              for i in range(19)},
                                          id_da_repeticao("en", "20"): b"\x00\x00" * 16000})
        medicao = medir("en", modelo, base / "en", base / NOME_PASTA_RUIDO, DetetorFalso, escrever=lambda _t: None)
        verificar("silencio digital posto de parte", "ativ-en-20" in medicao.invalidas and medicao.estado == ESTADO_PENDENTE)

        # 8. gravacao do ruido em blocos, retoma e nunca reescreve.
        pasta_ruido = base / "outro-ruido"
        resumo = gravar_ruido(pasta_ruido, CapturaDeRuidoFalsa(), threading.Event(), alvo_s=50.0,
                              segmento_s=20.0, raiz=raiz, escrever=lambda _t: None)
        verificar("ruido: blocos ate ao alvo", resumo.gravados == ["ruido-01", "ruido-02", "ruido-03"], resumo.gravados)
        resumo = gravar_ruido(pasta_ruido, CapturaDeRuidoFalsa(), threading.Event(), alvo_s=50.0,
                              segmento_s=20.0, raiz=raiz, escrever=lambda _t: None)
        verificar("ruido: retoma sem regravar", resumo.gravados == [] and resumo.total_s >= 50.0)
        parar = threading.Event()
        captura = CapturaDeRuidoFalsa([b"\x00\x00" * 16000 * 20], parar_depois=None)
        resumo = gravar_ruido(base / "mudo", captura, parar, alvo_s=40.0, segmento_s=20.0, raiz=raiz,
                              escrever=lambda _t: None)
        verificar("ruido: bloco sem sinal recusado", resumo.recusados == 1 and resumo.gravados == ["ruido-01", "ruido-02"])
        parar = threading.Event()
        resumo = gravar_ruido(base / "parado", CapturaDeRuidoFalsa(parar_depois=parar), parar, alvo_s=60.0,
                              segmento_s=20.0, raiz=raiz, escrever=lambda _t: None)
        verificar("ruido: Enter para depois do bloco", resumo.gravados == ["ruido-01"] and resumo.interrompido)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste de avaliar_ativacao completo (modelo e audio falsos, sem microfone nem som).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
