r"""Mede o interprete contra o golden set: intencao, projeto, fidelidade, latencia.

Corre cada caso de `tests/interprete/golden-<lingua>.jsonl` pelo
`jarvis.interprete.Interprete` com o LLM local (Ollama) ja aquecido, e mede:

  - acerto de intencao (inclui as recusas financeiras e a lista branca);
  - acerto de projeto: o nome certo, nenhum quando a frase nao tem projeto,
    ou uma pergunta quando o golden diz "?" (sem projeto ou ambiguo);
  - pedidos inventados: um termo proibido do caso aparece no prompt
    reescrito (os termos proibidos nunca estao na frase original);
  - termos obrigatorios em falta no prompt (so nos casos com prompt);
  - latencia a quente das chamadas ao LLM, p50 e p95;
  - VRAM antes e depois de carregar o modelo, e o modelo que ficou. Se o
    principal nao esta instalado ou nao cabe, desce para o alternativo.

Os projetos do golden sao ficticios e fixos (`PROJETOS_DO_GOLDEN`): o
config.toml real so da o URL e os modelos da tabela [interprete].

Com `--verificar` sai com erro (1) se, em qualquer lingua, a intencao ficar
abaixo de 95% ou o projeto abaixo de 98%, se houver algum pedido inventado,
ou se a latencia a quente passar de 1,2 s p50 ou 2,5 s p95. Sai com 2 se
nenhum modelo configurado estiver disponivel (diz o comando para o instalar).
`--minimo-intencao 99.1` sobe a meta da intencao (em percentagem) para nao
deixar o acerto descer face a uma medicao anterior.

    .venv\Scripts\python scripts/avaliar_interprete.py --verificar
    .venv\Scripts\python scripts/avaliar_interprete.py --lingua en --mostrar-erros
    .venv\Scripts\python scripts/avaliar_interprete.py --verificar --evidencia
    .venv\Scripts\python scripts/avaliar_interprete.py --lingua en --verificar --minimo-intencao 99.1

`--modelo` mede outro modelo instalado no lugar do principal (o relatorio
di-lo). `--evidencia` escreve o resumo em docs/forja/evidence/ (ignorada
pelo Git); so leva as frases ficticias do golden e numeros.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import caminho_evidencia_de_saida, caminho_para_mostrar  # noqa: E402
from jarvis.config import (  # noqa: E402
    CAMINHO_CONFIG_PADRAO,
    Config,
    ConfigError,
    ConfigInterprete,
    ConfigOuvido,
    Projeto,
    carregar_config,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.interprete import (  # noqa: E402
    INTENCAO_CORTESIA,
    INTENCAO_RECUSADA,
    INTENCOES,
    INTENCOES_COM_PROMPT,
    Interpretacao,
    Interprete,
    medir_vram,
    nome_canonico,
)
from jarvis.router import _normalizar  # noqa: E402

PASTA_GOLDEN = RAIZ / "tests" / "interprete"
LINGUAS = ("pt", "en")

#: Projetos ficticios do golden set. Nenhum e um projeto real.
PROJETOS_DO_GOLDEN: tuple[str, ...] = ("atlas", "orbita", "kanban-lite", "nimbus", "bolsa-radar")

#: Valor do campo "projeto" quando o jarvis tem de perguntar qual.
PROJETO_A_PERGUNTAR = "?"

MINIMO_DE_CASOS = 80
META_INTENCAO = 0.95
META_PROJETO = 0.98
LIMITE_P50_S = 1.2
LIMITE_P95_S = 2.5

CAMPOS = ("id", "texto", "intencao", "projeto", "obrigatorios", "proibidos", "nota")


class GoldenError(Exception):
    """Golden set mal formado: diz a linha e o que corrigir."""


@dataclass(frozen=True)
class Caso:
    id: str
    texto: str
    intencao: str
    projeto: str | None
    obrigatorios: tuple[str, ...]
    proibidos: tuple[str, ...]
    nota: str = ""


def termo_presente(termo: str, texto: str) -> bool:
    """O termo (ou uma das alternativas "a|b") aparece no inicio de uma palavra."""
    normalizado = _normalizar(texto)
    for alternativa in termo.split("|"):
        alvo = _normalizar(alternativa)
        if alvo and re.search(r"(?<![a-z0-9])" + re.escape(alvo), normalizado):
            return True
    return False


def ler_golden(caminho: Path, projetos: tuple[str, ...] = PROJETOS_DO_GOLDEN) -> list[Caso]:
    """Le e valida um golden set JSONL."""
    if not caminho.is_file():
        raise GoldenError(f"golden set em falta: {caminho_para_mostrar(caminho)}")
    casos: list[Caso] = []
    ids: set[str] = set()
    validas = set(INTENCOES) | {INTENCAO_RECUSADA, INTENCAO_CORTESIA}
    for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), 1):
        if not linha.strip():
            continue
        onde = f"{caminho.name}:{numero}"
        try:
            dados = json.loads(linha)
        except json.JSONDecodeError as erro:
            raise GoldenError(f"{onde}: JSON invalido: {erro}") from erro
        if not isinstance(dados, dict) or set(dados) != set(CAMPOS):
            raise GoldenError(f"{onde}: cada caso tem exatamente os campos {', '.join(CAMPOS)}")
        if dados["id"] in ids:
            raise GoldenError(f"{onde}: id repetido {dados['id']!r}")
        ids.add(dados["id"])
        if not isinstance(dados["texto"], str) or not dados["texto"].strip():
            raise GoldenError(f"{onde}: texto vazio")
        if dados["intencao"] not in validas:
            raise GoldenError(f"{onde}: intencao desconhecida {dados['intencao']!r}")
        projeto = dados["projeto"]
        if projeto is not None and projeto != PROJETO_A_PERGUNTAR and projeto not in projetos:
            raise GoldenError(f"{onde}: projeto {projeto!r} nao e um projeto do golden")
        for chave in ("obrigatorios", "proibidos"):
            if not isinstance(dados[chave], list) or not all(isinstance(t, str) and t for t in dados[chave]):
                raise GoldenError(f"{onde}: {chave} tem de ser uma lista de texto")
        for termo in dados["obrigatorios"]:
            if not termo_presente(termo, dados["texto"]):
                raise GoldenError(f"{onde}: termo obrigatorio {termo!r} nao esta na frase")
        for termo in dados["proibidos"]:
            if termo_presente(termo, dados["texto"]):
                raise GoldenError(f"{onde}: termo proibido {termo!r} ja esta na frase")
        casos.append(
            Caso(
                id=dados["id"],
                texto=dados["texto"],
                intencao=dados["intencao"],
                projeto=projeto,
                obrigatorios=tuple(dados["obrigatorios"]),
                proibidos=tuple(dados["proibidos"]),
                nota=dados["nota"] if isinstance(dados["nota"], str) else "",
            )
        )
    return casos


def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo metodo do vizinho mais proximo (sem interpolar)."""
    ordenados = sorted(valores)
    if not ordenados:
        return math.nan
    return ordenados[max(0, math.ceil(p / 100.0 * len(ordenados)) - 1)]


@dataclass
class Veredito:
    caso: Caso
    resultado: Interpretacao
    intencao_certa: bool
    projeto_certo: bool
    inventados: tuple[str, ...]
    em_falta: tuple[str, ...]


def julgar(caso: Caso, resultado: Interpretacao) -> Veredito:
    intencao_certa = resultado.intencao == caso.intencao
    if caso.projeto == PROJETO_A_PERGUNTAR:
        projeto_certo = resultado.projeto is None and resultado.pergunta is not None
    else:
        projeto_certo = resultado.projeto == caso.projeto
    inventados = tuple(t for t in caso.proibidos if termo_presente(t, resultado.prompt))
    em_falta: tuple[str, ...] = ()
    if caso.intencao in INTENCOES_COM_PROMPT and intencao_certa:
        em_falta = tuple(t for t in caso.obrigatorios if not termo_presente(t, resultado.prompt))
    return Veredito(caso, resultado, intencao_certa, projeto_certo, inventados, em_falta)


@dataclass
class Resumo:
    lingua: str
    vereditos: list[Veredito] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.vereditos)

    @property
    def intencao(self) -> float:
        return sum(v.intencao_certa for v in self.vereditos) / self.total if self.total else 0.0

    @property
    def projeto(self) -> float:
        return sum(v.projeto_certo for v in self.vereditos) / self.total if self.total else 0.0

    @property
    def inventados(self) -> list[Veredito]:
        return [v for v in self.vereditos if v.inventados]

    @property
    def com_termos_em_falta(self) -> list[Veredito]:
        return [v for v in self.vereditos if v.em_falta]

    @property
    def recursos(self) -> list[Veredito]:
        return [v for v in self.vereditos if v.resultado.origem == "recurso"]

    @property
    def latencias_llm(self) -> list[float]:
        return [v.resultado.latencia_s for v in self.vereditos if v.resultado.origem in ("llm", "recurso")]


def avaliar(interprete: Interprete, lingua: str, casos: list[Caso]) -> Resumo:
    interprete.lingua = lingua
    resumo = Resumo(lingua)
    for caso in casos:
        resumo.vereditos.append(julgar(caso, interprete.interpretar(caso.texto)))
    return resumo


def falhas_da_verificacao(
    resumos: list[Resumo],
    minimo_de_casos: int = MINIMO_DE_CASOS,
    meta_intencao: float = META_INTENCAO,
) -> list[str]:
    """Os motivos por que `--verificar` falha (vazio = passa).

    `meta_intencao` e uma fracao (0.991 = 99,1%); nunca fica abaixo de
    `META_INTENCAO`.
    """
    falhas: list[str] = []
    latencias: list[float] = []
    meta_intencao = max(meta_intencao, META_INTENCAO)
    for resumo in resumos:
        if resumo.total < minimo_de_casos:
            falhas.append(f"{resumo.lingua}: so {resumo.total} casos (minimo {minimo_de_casos})")
        if resumo.intencao < meta_intencao:
            falhas.append(f"{resumo.lingua}: intencao {resumo.intencao:.2%} < {meta_intencao:.2%}")
        if resumo.projeto < META_PROJETO:
            falhas.append(f"{resumo.lingua}: projeto {resumo.projeto:.1%} < {META_PROJETO:.0%}")
        if resumo.inventados:
            ids = ", ".join(v.caso.id for v in resumo.inventados)
            falhas.append(f"{resumo.lingua}: {len(resumo.inventados)} pedido(s) inventado(s) ({ids})")
        latencias.extend(resumo.latencias_llm)
    if not latencias:
        falhas.append("nenhuma chamada ao LLM foi medida")
    else:
        p50, p95 = percentil(latencias, 50), percentil(latencias, 95)
        if p50 > LIMITE_P50_S:
            falhas.append(f"latencia a quente p50 {p50:.2f} s > {LIMITE_P50_S} s")
        if p95 > LIMITE_P95_S:
            falhas.append(f"latencia a quente p95 {p95:.2f} s > {LIMITE_P95_S} s")
    return falhas


def config_do_golden(ajustes: ConfigInterprete, lingua: str) -> Config:
    """Config em memoria com os projetos ficticios; caminhos nunca tocados."""
    return Config(
        microfone="(avaliacao do interprete)",
        projetos=tuple(
            Projeto(nome=nome, caminho=Path("D:/caminho/para") / nome) for nome in PROJETOS_DO_GOLDEN
        ),
        ouvido=ConfigOuvido(lingua=lingua),
        interprete=ajustes,
    )


def ajustes_do_utilizador(caminho: Path = CAMINHO_CONFIG_PADRAO) -> ConfigInterprete:
    """A tabela [interprete] do config.toml, ou os valores por omissao sem ele.

    Um config.toml que existe mas esta errado levanta ConfigError: medir com
    os valores por omissao escondia o erro.
    """
    if not caminho.is_file():
        return ConfigInterprete()
    return carregar_config(caminho, validar_caminhos=False).interprete


def _linha_de_caso(veredito: Veredito) -> str:
    r = veredito.resultado
    marcas = []
    if not veredito.intencao_certa:
        marcas.append(f"intencao {r.intencao} (esperada {veredito.caso.intencao})")
    if not veredito.projeto_certo:
        obtido = r.projeto or ("pergunta" if r.pergunta else "nenhum")
        marcas.append(f"projeto {obtido} (esperado {veredito.caso.projeto or 'nenhum'})")
    if veredito.inventados:
        marcas.append(f"INVENTADO {', '.join(veredito.inventados)}")
    if veredito.em_falta:
        marcas.append(f"em falta {', '.join(veredito.em_falta)}")
    return (
        f"- {veredito.caso.id} [{r.origem} {r.latencia_s:.2f} s] \"{veredito.caso.texto}\" -> "
        f"{'; '.join(marcas)}; prompt: \"{r.prompt}\""
    )


def relatorio(
    resumos: list[Resumo],
    *,
    modelo: str,
    configurado: str,
    escolha_motivo: str,
    vram_antes: str,
    vram_depois: str,
    vram_do_modelo_mib: int | None,
    mostrar_erros: bool,
) -> list[str]:
    linhas = [
        f"Modelo: {modelo}"
        + ("" if nome_canonico(modelo) == nome_canonico(configurado) else f" (configurado: {configurado})"),
        f"Escolha: {escolha_motivo}",
        f"VRAM antes: {vram_antes}",
        f"VRAM depois: {vram_depois}"
        + (f"; o modelo ocupa {vram_do_modelo_mib} MiB" if vram_do_modelo_mib is not None else ""),
        "",
        "| lingua | casos | intencao | projeto | inventados | termos em falta | recursos | LLM p50 | LLM p95 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    todas: list[float] = []
    for r in resumos:
        lat = r.latencias_llm
        todas.extend(lat)
        linhas.append(
            f"| {r.lingua} | {r.total} | {r.intencao:.1%} | {r.projeto:.1%} | {len(r.inventados)} | "
            f"{len(r.com_termos_em_falta)} | {len(r.recursos)} | {percentil(lat, 50):.2f} s | "
            f"{percentil(lat, 95):.2f} s |"
        )
    linhas.append("")
    linhas.append(
        f"Latencia a quente do LLM ({len(todas)} chamadas): p50 {percentil(todas, 50):.2f} s, "
        f"p95 {percentil(todas, 95):.2f} s, maxima {max(todas) if todas else math.nan:.2f} s "
        f"(metas {LIMITE_P50_S} s / {LIMITE_P95_S} s)."
    )
    if mostrar_erros:
        for r in resumos:
            erros = [
                v
                for v in r.vereditos
                if not v.intencao_certa or not v.projeto_certo or v.inventados or v.em_falta
            ]
            linhas.append("")
            linhas.append(f"Casos com falhas ({r.lingua}): {len(erros)}")
            linhas.extend(_linha_de_caso(v) for v in erros)
    return linhas


def _percentagem(texto: str) -> float:
    """Uma percentagem entre 0 e 100, devolvida como fracao."""
    try:
        valor = float(texto.replace(",", "."))
    except ValueError as erro:
        raise argparse.ArgumentTypeError(f"percentagem invalida: {texto!r}") from erro
    if not 0.0 <= valor <= 100.0 or math.isnan(valor):
        raise argparse.ArgumentTypeError(f"a percentagem tem de estar entre 0 e 100: {texto!r}")
    return valor / 100.0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mede o interprete (LLM local) contra o golden set.",
        epilog="Nunca toca som. A evidencia fica em docs/forja/evidence/ (ignorada pelo Git).",
    )
    parser.add_argument("--lingua", choices=(*LINGUAS, "todas"), default="todas")
    parser.add_argument("--modelo", default=None, help="mede este modelo instalado em vez do principal")
    parser.add_argument("--verificar", action="store_true", help="sai com erro se falhar as metas")
    parser.add_argument("--mostrar-erros", action="store_true", help="lista os casos que falharam")
    parser.add_argument(
        "--minimo-intencao",
        type=_percentagem,
        default=None,
        metavar="PERCENTAGEM",
        help=f"acerto minimo da intencao em %% (por omissao {META_INTENCAO:.0%}), por exemplo 99.1",
    )
    parser.add_argument("--evidencia", nargs="?", const="", default=None, help="escreve o resumo em .md")
    return parser


def principal(
    argv: list[str] | None = None,
    *,
    fabrica: Callable[[Config], Interprete] | None = None,
    medir: Callable[[], object] = medir_vram,
) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    linguas = LINGUAS if args.lingua == "todas" else (args.lingua,)
    try:
        goldens = {lingua: ler_golden(PASTA_GOLDEN / f"golden-{lingua}.jsonl") for lingua in linguas}
    except GoldenError as erro:
        print(f"ERRO: {erro}")
        return 1

    try:
        ajustes = ajustes_do_utilizador()
    except ConfigError as erro:
        print(f"ERRO: {erro}")
        return 1
    configurado = ajustes.modelo
    if args.modelo:
        ajustes = ConfigInterprete(
            url=ajustes.url,
            modelo=args.modelo.strip().lower(),
            modelo_alternativo=ajustes.modelo_alternativo,
            limite_s=ajustes.limite_s,
        )
    fabrica = fabrica or (lambda config: Interprete(config))
    interprete = fabrica(config_do_golden(ajustes, linguas[0]))

    vram_antes = medir()
    print(f"VRAM antes de carregar: {vram_antes or 'nao medida (sem nvidia-smi)'}")
    print("A escolher e a carregar o modelo...")
    escolha = interprete.aquecer(medir)
    if escolha.modelo is None:
        print("ERRO: nenhum modelo configurado esta disponivel para o interprete.")
        for nota in escolha.notas or (escolha.motivo,):
            print(f"  - {nota}")
        print("Instalar no Ollama (uma vez):")
        for modelo in dict.fromkeys((ajustes.modelo, ajustes.modelo_alternativo)):
            print(f"  ollama pull {modelo}")
        return 2
    print(f"Modelo: {escolha.modelo} ({escolha.motivo})")

    resumos = []
    for lingua in linguas:
        print(f"A avaliar {len(goldens[lingua])} casos em {lingua}...")
        resumos.append(avaliar(interprete, lingua, goldens[lingua]))
    vram_depois = medir()

    linhas = relatorio(
        resumos,
        modelo=escolha.modelo,
        configurado=configurado,
        escolha_motivo=escolha.motivo,
        vram_antes=str(vram_antes or "nao medida"),
        vram_depois=str(vram_depois or "nao medida"),
        vram_do_modelo_mib=escolha.vram_do_modelo_mib,
        mostrar_erros=args.mostrar_erros or args.evidencia is not None,
    )
    meta_intencao = META_INTENCAO if args.minimo_intencao is None else args.minimo_intencao
    falhas = falhas_da_verificacao(resumos, meta_intencao=meta_intencao)
    print()
    print("\n".join(linhas))
    print()
    if falhas:
        print("Metas por cumprir:")
        for falha in falhas:
            print(f"  - {falha}")
    else:
        print("Todas as metas cumpridas.")

    if args.evidencia is not None:
        carimbo = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        destino = args.evidencia or f"docs/forja/evidence/interprete-{carimbo}.md"
        try:
            saida = caminho_evidencia_de_saida(destino)
        except ValueError as erro:
            print(f"ERRO: {erro}")
            return 1
        saida.parent.mkdir(parents=True, exist_ok=True)
        cabecalho = [
            f"# Interprete: golden set ({datetime.datetime.now():%Y-%m-%d %H:%M})",
            "",
            "Frases ficticias do golden set; nenhuma voz nem nome real.",
            "",
        ]
        estado = ["", "## Verificacao", ""] + ([f"- {f}" for f in falhas] or ["- todas as metas cumpridas"])
        saida.write_text("\n".join(cabecalho + linhas + estado) + "\n", encoding="utf-8")
        print(f"Evidencia: {caminho_para_mostrar(saida)}")

    if args.verificar and falhas:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(principal())
