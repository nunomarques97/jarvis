r"""Mede o cerebro de conversa com o Claude real: latencia e tokens por troca, por modelo.

    .venv\Scripts\python scripts/medir_cerebro.py --com-claude
    .venv\Scripts\python scripts/medir_cerebro.py --com-claude --modelos claude-haiku-4-5,sonnet

Para cada modelo pedido (por omissao `claude-haiku-4-5` e `sonnet`) arranca
um cerebro pelo mesmo caminho do jarvis (`jarvis.cerebro.Cerebro`: um processo
`claude` persistente em stream-json, na pasta neutra, so com WebSearch e
WebFetch e com o login da subscricao) e corre a mesma conversa sintetica fixa,
em ingles: uma saudacao, um pedido de ajuda para cozinhar com dois seguimentos
que so se percebem com o contexto, uma pergunta de conhecimento geral, uma
pergunta de tempo que pede pesquisa na web e o seguimento dela, e um fecho.

Para cada troca mede, desde o envio da frase ao cerebro:

  * primeiro texto: ate ao primeiro pedaco de texto do modelo;
  * primeira frase: ate a primeira frase inteira entregue (a que a voz diria);
  * total: ate ao fim do turno;
  * tokens da linha final: input, cache read, cache creation, output e as
    pesquisas na web; e o maior contexto de uma chamada.

Isto e so a parte do cerebro: a meta da conversa (p50 <= 1,5 s da ultima voz
a primeira frase falada) inclui ainda o fim de turno, a transcricao e a voz, e
mede-se com a voz real em `scripts/sessao_naturalidade.py`.

Nada do utilizador vai ao Claude: a localizacao e os factos sao os ficticios
deste ficheiro. Do config.toml so se usam a tabela [cerebro] (limite de tempo
e contexto de recomeco) e as pastas dos projetos, para a pasta neutra ficar
fora delas. O relatorio vai para docs/forja/evidence/ (ignorada pelo Git).

Gasta quota da subscricao Claude, por isso so corre com `--com-claude`. Sem
ele recusa, nao arranca nenhum processo e so imprime o teto garantido de
tokens por troca calculado a partir da configuracao.

Codigos de saida: 0 = todas as trocas responderam; 1 = alguma falhou (o
relatorio e escrito na mesma); 2 = falta `--com-claude` ou um argumento e
invalido; 3 = o CLI do Claude Code nao esta disponivel (nada e medido).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import PASTA_EVIDENCIA, caminho_evidencia_de_saida, caminho_para_mostrar  # noqa: E402
from jarvis.canal_claude import localizar_cli, verificar_executavel_seguro  # noqa: E402
from jarvis.cerebro import (  # noqa: E402
    CONTEXTO_DURO_TOKENS,
    MAX_TURNS,
    MAX_USOS_WEB,
    MAXIMO_DO_TEXTO_DA_RESPOSTA,
    PALAVRAS_DO_RESUMO,
    Cerebro,
    ResultadoDoTurno,
)
from jarvis.config import (  # noqa: E402
    _PADRAO_MODELO_DO_CLAUDE,
    CAMINHO_CONFIG_PADRAO,
    ConfigCerebro,
    ConfigError,
    carregar_config,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.ouvido import percentil  # noqa: E402

MODELOS_POR_OMISSAO = ("claude-haiku-4-5", "sonnet")

#: Dados ficticios: nada disto vem do utilizador.
LOCALIZACAO_FICTICIA = "Lisbon, Portugal"
FACTOS_FICTICIOS = (
    "The user is learning to cook.",
    "The user prefers short spoken answers.",
    "The user likes walking by the river on weekends.",
)

#: Referencia do plano para uma troca tipica sem pesquisa (a medir, nao imposta).
ENTRADA_TIPICA_TOKENS = 10000

CODIGO_OK, CODIGO_FALHOU, CODIGO_RECUSADO, CODIGO_SEM_CLI = 0, 1, 2, 3


@dataclass(frozen=True)
class TrocaSintetica:
    id: str
    frase: str
    #: A resposta deve precisar de pesquisa na web (so para o relatorio).
    pesquisa_esperada: bool = False


#: A conversa fixa, pela ordem: os seguimentos so se percebem com as trocas anteriores.
TROCAS_SINTETICAS = (
    TrocaSintetica("social", "Hey jarvis, how are you doing?"),
    TrocaSintetica("cook", "I want to start learning to cook. Can you help me?"),
    TrocaSintetica("cook-follow-up", "The first dish?"),
    TrocaSintetica("cook-time", "Something quick, I have twenty minutes."),
    TrocaSintetica("knowledge", "How long does it take to boil an egg?"),
    TrocaSintetica("weather", "What's the weather like in Lisbon today?", pesquisa_esperada=True),
    TrocaSintetica("weather-follow-up", "And tomorrow?", pesquisa_esperada=True),
    TrocaSintetica("closing", "Thanks, that was helpful."),
)


# --- Teto garantido ------------------------------------------------------------


@dataclass(frozen=True)
class Teto:
    """O que o jarvis garante por troca, qualquer que seja a resposta do modelo."""

    max_turns: int
    contexto_duro: int
    usos_web: int
    caracteres: int
    limite_s: float
    contexto_de_recomeco: int
    palavras_do_resumo: int

    @property
    def entrada_maxima(self) -> int:
        """Tokens de entrada por troca: cada chamada ao modelo ate ao contexto duro."""
        return self.max_turns * self.contexto_duro


def teto_por_troca(config: ConfigCerebro) -> Teto:
    """O teto a partir de [cerebro] e dos tetos fixos de `jarvis.cerebro`."""
    return Teto(
        max_turns=MAX_TURNS,
        contexto_duro=CONTEXTO_DURO_TOKENS,
        usos_web=MAX_USOS_WEB,
        caracteres=MAXIMO_DO_TEXTO_DA_RESPOSTA,
        limite_s=config.limite_s,
        contexto_de_recomeco=config.contexto_max_tokens,
        palavras_do_resumo=PALAVRAS_DO_RESUMO,
    )


def linhas_do_teto(teto: Teto) -> list[str]:
    return [
        f"- input tokens per exchange: at most {teto.max_turns} model calls (--max-turns) x "
        f"{teto.contexto_duro} tokens of hard context per call = **{teto.entrada_maxima} tokens**; "
        "a call that reports more context interrupts the turn",
        f"- web uses (searches and pages read) per exchange: at most {teto.usos_web}; one more interrupts the turn",
        f"- reply text per exchange: at most {teto.caracteres} characters; more interrupts the turn",
        f"- time per exchange: at most {teto.limite_s:g} s ([cerebro] limite_s)",
        f"- the conversation rolls over to a new session once the reported context passes "
        f"{teto.contexto_de_recomeco} tokens ([cerebro] contexto_max_tokens) or after [cerebro] inativo_min "
        f"without exchanges: that exchange first sends one summary request to the old session (one call that "
        f"reads the old session's context and keeps at most {teto.palavras_do_resumo} words), on top of the "
        "ceiling above",
    ]


# --- Medicao --------------------------------------------------------------------


@dataclass(frozen=True)
class MedicaoDaTroca:
    troca: TrocaSintetica
    resultado: ResultadoDoTurno
    #: Segundos do envio a primeira frase entregue; None sem texto.
    primeira_frase_s: float | None
    total_s: float

    def uso(self, campo: str) -> int | None:
        return None if self.resultado.uso is None else self.resultado.uso.get(campo)

    @property
    def entrada_total(self) -> int | None:
        """Tokens de entrada contando os da cache (lidos e escritos), quando vieram."""
        partes = [self.uso(c) for c in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
        if all(parte is None for parte in partes):
            return None
        return sum(parte or 0 for parte in partes)

    @property
    def com_pesquisa(self) -> bool:
        return self.resultado.usos_web > 0


@dataclass
class MedicaoDoModelo:
    modelo: str
    aqueceu: bool
    aquecimento_s: float
    trocas: list = field(default_factory=list)

    @property
    def respondidas(self) -> int:
        return sum(1 for m in self.trocas if m.resultado.respondido)


def medir_troca(cerebro: Cerebro, troca: TrocaSintetica, relogio: Callable[[], float]) -> MedicaoDaTroca:
    inicio = relogio()
    primeira: list[float] = []

    def ao_texto(_pedaco: str) -> None:
        # O cerebro so entrega frases inteiras: o primeiro pedaco e a primeira frase.
        if not primeira:
            primeira.append(relogio() - inicio)

    resultado = cerebro.turno(troca.frase, ao_texto)
    return MedicaoDaTroca(troca, resultado, primeira[0] if primeira else None, relogio() - inicio)


def medir_modelo(
    modelo: str,
    config: ConfigCerebro,
    *,
    trocas: Sequence[TrocaSintetica] = TROCAS_SINTETICAS,
    arrancar: Callable[..., subprocess.Popen],
    cli: str,
    pasta: Path | None,
    pastas_proibidas: Sequence[Path],
    relogio: Callable[[], float] = time.monotonic,
    escrever: Callable[[str], object] = print,
) -> MedicaoDoModelo:
    """Uma sessao nova do cerebro com `modelo`, a conversa sintetica inteira e o fecho."""
    cerebro = Cerebro(
        dataclasses.replace(config, modelo=modelo),
        "en",
        localizacao=LOCALIZACAO_FICTICIA,
        pastas_proibidas=pastas_proibidas,
        factos=lambda: FACTOS_FICTICIOS,
        cli=cli,
        arrancar=arrancar,
        pasta=pasta,
        relogio=relogio,
    )
    try:
        inicio = relogio()
        aqueceu = cerebro.aquecer()
        medicao = MedicaoDoModelo(modelo, aqueceu, relogio() - inicio)
        if not aqueceu:
            escrever(f"  {modelo}: o processo do cerebro nao arrancou; nada medido")
            return medicao
        for troca in trocas:
            m = medir_troca(cerebro, troca, relogio)
            escrever(
                f"  {modelo} | {troca.id}: {m.resultado.estado} | primeira frase {_s(m.primeira_frase_s)} "
                f"| total {m.total_s:.2f} s | entrada {_n(m.entrada_total)} | saida {_n(m.uso('output_tokens'))} "
                f"| web {m.resultado.usos_web}"
            )
            medicao.trocas.append(m)
        return medicao
    finally:
        cerebro.fechar()


# --- Relatorio ------------------------------------------------------------------


def _n(valor) -> str:
    return "-" if valor is None else str(valor)


def _s(valor: float | None) -> str:
    return "-" if valor is None else f"{valor:.2f} s"


def _estatistica_s(valores: Sequence[float]) -> str:
    if not valores:
        return "no samples"
    return f"p50 {percentil(list(valores), 50):.2f} s · p95 {percentil(list(valores), 95):.2f} s · n={len(valores)}"


def _estatistica_tokens(valores: Sequence[int]) -> str:
    if not valores:
        return "no samples"
    return f"p50 {percentil(list(valores), 50):.0f} · max {max(valores)} · n={len(valores)}"


def _texto_numa_linha(texto: str) -> str:
    return " ".join((texto or "").split()).replace("|", "/")


def linhas_do_relatorio(
    medicoes: Sequence[MedicaoDoModelo],
    teto: Teto,
    *,
    versao: str,
    quando: datetime.datetime,
) -> list[str]:
    linhas = [
        f"# Brain measurement: latency and tokens per exchange — {quando:%Y-%m-%d %H:%M}",
        "",
        f"Real Claude Code ({versao}) through the jarvis brain path (`jarvis.cerebro.Cerebro`: one persistent "
        "`claude` process in stream-json, neutral folder, only WebSearch and WebFetch, subscription login).",
        f"Fixed synthetic English conversation of {len(TROCAS_SINTETICAS)} exchanges per model, a new session per "
        f"model; fictitious location ({LOCALIZACAO_FICTICIA}) and {len(FACTOS_FICTICIOS)} fictitious facts. Nothing "
        "from the user was sent.",
        "Times are the brain's share only, from sending the sentence: first text, first whole sentence (what the "
        "voice would say first) and the whole turn. The conversation target (median at most 1.5 s from the last "
        "voice to the first spoken sentence) also includes end of turn, transcription and voice, and is measured "
        "with `scripts/sessao_naturalidade.py`.",
        "",
        "## Guaranteed ceiling per exchange",
        "",
        *linhas_do_teto(teto),
        "",
        "## Summary per model",
        "",
        "| model | answered | start | first sentence, no web | first sentence, web | first text | whole turn "
        "| input tokens, no web | input tokens, any | output tokens | web searches | largest context |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in medicoes:
        sem_web = [t for t in m.trocas if not t.com_pesquisa]
        com_web = [t for t in m.trocas if t.com_pesquisa]
        entradas_sem_web = [t.entrada_total for t in sem_web if t.entrada_total is not None]
        entradas = [t.entrada_total for t in m.trocas if t.entrada_total is not None]
        saidas = [t.uso("output_tokens") for t in m.trocas if t.uso("output_tokens") is not None]
        pesquisas = sum(t.uso("web_search_requests") or 0 for t in m.trocas)
        contextos = [t.resultado.contexto_tokens for t in m.trocas if t.resultado.contexto_tokens is not None]
        linhas.append(
            f"| `{m.modelo}` | {m.respondidas} of {len(m.trocas) if m.aqueceu else len(TROCAS_SINTETICAS)} "
            f"| {m.aquecimento_s:.2f} s{'' if m.aqueceu else ' (did not start)'} "
            f"| {_estatistica_s([t.primeira_frase_s for t in sem_web if t.primeira_frase_s is not None])} "
            f"| {_estatistica_s([t.primeira_frase_s for t in com_web if t.primeira_frase_s is not None])} "
            f"| {_estatistica_s([t.resultado.primeiro_texto_s for t in m.trocas if t.resultado.primeiro_texto_s is not None])} "
            f"| {_estatistica_s([t.total_s for t in m.trocas])} "
            f"| {_estatistica_tokens(entradas_sem_web)} | {_estatistica_tokens(entradas)} "
            f"| {_estatistica_tokens(saidas)} | {pesquisas} | {max(contextos) if contextos else '-'} |"
        )
    linhas += [
        "",
        "`input tokens` = input + cache read + cache creation: everything the model read in the exchange, including "
        "Claude Code's own fixed instructions and the text of web searches. The first exchange of a session pays the "
        "cache creation of those fixed instructions; the next ones read it from the cache.",
        "",
        "## Exchange by exchange",
        "",
        "| model | id | web | state | first text | first sentence | whole turn | input | cache read "
        "| cache creation | output | web searches | largest context |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in medicoes:
        for t in m.trocas:
            r = t.resultado
            web = f"{r.usos_web} use(s)" if r.usos_web else "no"
            linhas.append(
                f"| `{m.modelo}` | {t.troca.id} | {web} | {r.estado} | {_s(r.primeiro_texto_s)} "
                f"| {_s(t.primeira_frase_s)} | {t.total_s:.2f} s | {_n(t.uso('input_tokens'))} "
                f"| {_n(t.uso('cache_read_input_tokens'))} | {_n(t.uso('cache_creation_input_tokens'))} "
                f"| {_n(t.uso('output_tokens'))} | {_n(t.uso('web_search_requests'))} | {_n(r.contexto_tokens)} |"
            )
    linhas += ["", "## Verdict", ""]
    for m in medicoes:
        entradas_sem_web = [t.entrada_total for t in m.trocas if not t.com_pesquisa and t.entrada_total is not None]
        entradas = [t.entrada_total for t in m.trocas if t.entrada_total is not None]
        tipica = percentil(entradas_sem_web, 50) if entradas_sem_web else None
        linhas.append(
            f"- `{m.modelo}`: {m.respondidas} of {len(TROCAS_SINTETICAS)} answered; typical input without web "
            + (
                f"{tipica:.0f} tokens ({'within' if tipica <= ENTRADA_TIPICA_TOKENS else 'ABOVE'} the planned "
                f"~{ENTRADA_TIPICA_TOKENS})"
                if tipica is not None
                else "not measured"
            )
            + "; every exchange within the guaranteed ceiling: "
            + ("yes" if entradas and max(entradas) <= teto.entrada_maxima else ("not measured" if not entradas else "NO"))
        )
    linhas += [
        "",
        "Changing `[cerebro] modelo` is a Sponsor decision after reading this report.",
        "",
        "## Replies (for comparing quality)",
        "",
    ]
    for m in medicoes:
        linhas += [f"### `{m.modelo}`", ""]
        for t in m.trocas:
            r = t.resultado
            texto = _texto_numa_linha(r.texto) if r.texto else f"({r.estado}: {_texto_numa_linha(r.motivo)})"
            linhas.append(f"- **{t.troca.id}** \"{t.troca.frase}\": {texto}")
        linhas.append("")
    return linhas


# --- Linha de comandos ------------------------------------------------------------


def versao_do_cli(cli: str) -> str:
    """A versao do Claude Code, ou "?" se nao se conseguir ler."""
    try:
        feito = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    saida = (feito.stdout or "").strip()
    return saida.splitlines()[0][:80] if saida else "?"


def modelos_pedidos(texto: str) -> tuple[str, ...]:
    """Os modelos de `--modelos`, validados como o `[cerebro] modelo`; ValueError se algum nao servir."""
    modelos = tuple(dict.fromkeys(parte.strip() for parte in texto.split(",") if parte.strip()))
    if not modelos:
        raise ValueError("--modelos vazio")
    for modelo in modelos:
        if not _PADRAO_MODELO_DO_CLAUDE.fullmatch(modelo):
            raise ValueError(f"modelo recusado: {modelo!r}")
    return modelos


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/medir_cerebro.py",
        description=(
            "Mede o cerebro de conversa com o Claude real: primeiro texto, primeira frase, total e tokens por troca, "
            "numa conversa sintetica fixa, para cada modelo."
        ),
        epilog="Gasta quota da subscricao Claude. O relatorio fica em docs/forja/evidence/ (ignorada pelo Git).",
    )
    parser.add_argument(
        "--com-claude",
        action="store_true",
        help="confirma que se pode falar com o Claude real (gasta quota da subscricao)",
    )
    parser.add_argument(
        "--modelos",
        default=",".join(MODELOS_POR_OMISSAO),
        metavar="M1,M2",
        help=f"modelos a comparar, separados por virgulas (por omissao {','.join(MODELOS_POR_OMISSAO)})",
    )
    parser.add_argument("--config", default=None, metavar="FICHEIRO")
    parser.add_argument(
        "--saida",
        default=None,
        metavar="FICHEIRO",
        help="ficheiro .md do relatorio, dentro de docs/forja/evidence/ (por omissao medir-cerebro-<data>.md)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    arrancar: Callable[..., subprocess.Popen] | None = None,
    cli: str | None = None,
    versao: Callable[[str], str] = versao_do_cli,
    agora: Callable[[], datetime.datetime] = datetime.datetime.now,
    relogio: Callable[[], float] = time.monotonic,
    pasta: Path | None = None,
    pasta_de_prova: Path = PASTA_EVIDENCIA,
) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    try:
        modelos = modelos_pedidos(args.modelos)
    except ValueError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return CODIGO_RECUSADO

    try:
        config = carregar_config(Path(args.config) if args.config else CAMINHO_CONFIG_PADRAO)
        config_cerebro = config.cerebro
        proibidas = [RAIZ, *(projeto.caminho for projeto in config.projetos)]
    except ConfigError as erro:
        print(f"AVISO: {erro}\nA usar os valores por omissao de [cerebro].", file=sys.stderr)
        config_cerebro, proibidas = ConfigCerebro(), [RAIZ]

    teto = teto_por_troca(config_cerebro)
    print("Teto garantido por troca (a partir de [cerebro] e dos tetos fixos do jarvis):")
    for linha in linhas_do_teto(teto):
        print("  " + linha.replace("**", ""))

    if not args.com_claude:
        print(
            "\nRecusado: isto fala com o Claude real "
            f"({len(TROCAS_SINTETICAS)} trocas por modelo, {len(modelos)} modelo(s)) e gasta quota da subscricao.\n"
            "Nenhum processo foi arrancado. Para correr mesmo, repete com --com-claude.",
            file=sys.stderr,
        )
        return CODIGO_RECUSADO

    quando = agora()
    try:
        destino = caminho_evidencia_de_saida(
            args.saida if args.saida else Path(pasta_de_prova) / f"medir-cerebro-{quando:%Y%m%d-%H%M%S}.md",
            pasta_permitida=pasta_de_prova,
        )
    except ValueError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return CODIGO_RECUSADO

    try:
        executavel = verificar_executavel_seguro(cli if cli is not None else localizar_cli())
    except (FileNotFoundError, ValueError) as erro:
        print(f"O CLI do Claude Code nao esta disponivel: nada foi medido.\n{erro}", file=sys.stderr)
        return CODIGO_SEM_CLI

    medicoes = []
    for modelo in modelos:
        print(f"\na medir {modelo} ({len(TROCAS_SINTETICAS)} trocas) ...")
        medicoes.append(
            medir_modelo(
                modelo,
                config_cerebro,
                arrancar=arrancar if arrancar is not None else subprocess.Popen,
                cli=executavel,
                pasta=pasta,
                pastas_proibidas=proibidas,
                relogio=relogio,
            )
        )

    linhas = linhas_do_relatorio(medicoes, teto, versao=versao(executavel), quando=quando)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"\nrelatorio: {caminho_para_mostrar(destino)}")
    todas = all(m.aqueceu and m.respondidas == len(TROCAS_SINTETICAS) for m in medicoes)
    return CODIGO_OK if todas else CODIGO_FALHOU


if __name__ == "__main__":
    raise SystemExit(main())
