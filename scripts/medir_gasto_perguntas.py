r"""Mede o gasto real de uma pergunta geral ao Claude, sem e com memoria.

Faz tres perguntas reais pelo mesmo caminho do jarvis
(`jarvis.pergunta_geral.PerguntasGerais`, `claude -p` headless so com
pesquisa na web), sempre com dados ficticios fixos deste ficheiro:

  1. sem-memoria        uma pergunta sozinha, sem historico nem factos;
  2. seguimento-5       uma pergunta de seguimento com 5 trocas recentes;
  3. seguimento-10-cheio a mesma pergunta com 10 trocas, cada pergunta e
                        cada resposta no tamanho maximo, e o caderno de
                        factos cheio (50 factos / 4000 caracteres com os
                        limites por omissao): o pior caso dos limites.

Para cada uma regista os tokens de entrada, de criacao e de leitura da
cache, os de saida, o `total_cost_usd` e as pesquisas na web quando a saida
JSON os traz, os caracteres de memoria enviados e o tempo de resposta. O
resumo em Markdown vai para docs/forja/evidence/ (ignorada pelo Git), com o
teto garantido do contexto de memoria calculado a partir dos limites de
`[memoria]` do config.toml.

Nada do utilizador e enviado: nem a localizacao do config.toml (vai a
localizacao por omissao), nem os projetos, nem o caderno real (o caderno
ficticio fica numa pasta temporaria apagada no fim). Do config.toml so se
usam o modelo, o limite de tempo e os limites de `[memoria]`.

Gasta quota da subscricao Claude, por isso recusa correr sem `--gasta-quota`:

    .venv\Scripts\python scripts/medir_gasto_perguntas.py --gasta-quota

Codigos de saida: 0 = as tres responderam; 1 = alguma falhou (o resumo e
escrito na mesma); 2 = falta `--gasta-quota`; 3 = o CLI do Claude Code nao
esta disponivel (nada e perguntado nem escrito).
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.canal_claude import localizar_cli, verificar_executavel_seguro  # noqa: E402
from jarvis.config import (  # noqa: E402
    CAMINHO_CONFIG_PADRAO,
    LOCALIZACAO_PADRAO,
    ConfigError,
    ConfigMemoria,
    ConfigPerguntas,
    carregar_config,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.interprete import MAXIMO_DO_TEXTO  # noqa: E402
from jarvis.memoria import (  # noqa: E402
    MAXIMO_DA_PERGUNTA,
    MAXIMO_DA_RESPOSTA,
    MAXIMO_POR_FACTO,
    CadernoDeFactos,
    HistoricoDePerguntas,
    Troca,
)
from jarvis.pergunta_geral import (  # noqa: E402
    PerguntasGerais,
    ResultadoDaPergunta,
    contexto_da_memoria,
    texto_do_pedido,
)
from jarvis.resposta_falada import resumo_falado  # noqa: E402

PASTA_DE_PROVA = RAIZ / "docs" / "forja" / "evidence"

#: A pergunta sozinha e o seguimento que so se percebe com o historico.
PERGUNTA_SOZINHA = "When did the James Webb Space Telescope launch?"
PERGUNTA_DE_SEGUIMENTO = "And when did it launch?"

#: Conversa ficticia, da mais antiga para a mais recente. A ultima troca e a
#: que da sentido ao seguimento.
TROCAS_FICTICIAS = (
    ("How tall is the Eiffel Tower?", "The Eiffel Tower is about 330 metres tall, including its antennas."),
    ("What is the longest river in Europe?", "The Volga is the longest river in Europe."),
    ("Who painted the Mona Lisa?", "Leonardo da Vinci painted the Mona Lisa in the early sixteenth century."),
    ("How many planets are in the solar system?", "There are eight planets in the solar system."),
    ("What is the boiling point of water at sea level?", "Water boils at 100 degrees Celsius at sea level."),
    ("What language is spoken in Brazil?", "Portuguese is the official language of Brazil."),
    ("How far is the Moon from the Earth?", "The Moon is on average about 384 thousand kilometres away."),
    ("What is the largest ocean?", "The Pacific is the largest ocean on Earth."),
    ("Who wrote Don Quixote?", "Miguel de Cervantes wrote Don Quixote."),
    (
        "What is the James Webb Space Telescope?",
        "It is a large infrared space telescope run by NASA with the European and Canadian space agencies, "
        "and it studies the early universe.",
    ),
)

#: Enchimento ficticio para levar cada pergunta e resposta ao tamanho maximo.
ENCHIMENTO_DA_PERGUNTA = " Please keep it short, in plain words, for a spoken answer."
ENCHIMENTO_DA_RESPOSTA = " This sentence is fictitious filler that only brings the memory to its limit."

#: Enchimento dos factos ficticios: palavras que nenhuma regra de segredos ou
#: de dinheiro apanha, mesmo cortadas a meio.
_ENCHIMENTO_DO_FACTO = (
    "the fictitious test user enjoys long walks by the river green tea old maps jazz records "
    "board games quiet libraries mountain trails and sunny mornings "
)

#: A data com o nome de dia e de mes mais compridos, para o teto do pedido.
_DATA_MAIS_COMPRIDA = datetime.date(2026, 9, 30)


@dataclass(frozen=True)
class Cenario:
    nome: str
    descricao: str
    pergunta: str
    trocas: tuple[Troca, ...] = ()
    factos: tuple[str, ...] = ()


@dataclass(frozen=True)
class Medicao:
    cenario: Cenario
    resultado: ResultadoDaPergunta
    #: Tempo de parede de `responder`, medido aqui.
    tempo_s: float
    #: Caracteres de tudo o que foi por stdin (instrucoes, memoria e pergunta).
    caracteres_do_pedido: int

    def uso(self, campo: str) -> int | None:
        return None if self.resultado.uso is None else self.resultado.uso.get(campo)

    @property
    def entrada_total(self) -> int | None:
        """Tokens de entrada contando os da cache (criados e lidos), quando vieram."""
        partes = [self.uso(c) for c in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if all(p is None for p in partes):
            return None
        return sum(p or 0 for p in partes)


@dataclass(frozen=True)
class Teto:
    """O tamanho maximo do contexto de memoria que os limites deixam enviar."""

    trocas: int
    factos: int
    caracteres: int
    #: Com texto normal (nenhum caractere escapado no JSON de cada item).
    contexto_texto_normal: int
    #: Garantido: cada caractere escapado (aspas e barras contam a dobrar).
    contexto_garantido: int
    #: O pedido inteiro no pior caso: instrucoes, memoria e a pergunta maxima.
    pedido_garantido: int


# --- Dados ficticios -------------------------------------------------------------


def _ate(texto: str, enchimento: str, maximo: int) -> str:
    """O texto com enchimento ate passar o maximo (o historico corta-o depois)."""
    while len(texto) <= maximo:
        texto += enchimento
    return texto


def facto_ficticio(numero: int, comprimento: int) -> str:
    """Um facto ficticio distinto com exatamente `comprimento` caracteres gravados."""
    comprimento = max(2, min(comprimento, MAXIMO_POR_FACTO))
    prefixo = f"Fact {numero:02d} "
    if len(prefixo) + 1 > comprimento - 1:
        prefixo = f"F{numero:02d}"
    corpo = (prefixo + _ENCHIMENTO_DO_FACTO * (comprimento // len(_ENCHIMENTO_DO_FACTO) + 2))[: comprimento - 1]
    if corpo.endswith(" "):
        corpo = corpo[:-1] + "s"
    return corpo + "."


def encher_caderno(caderno: CadernoDeFactos) -> tuple[str, ...]:
    """Grava factos ficticios ate aos limites do caderno e devolve os do contexto."""
    quantos, caracteres = caderno.maximo_factos, caderno.maximo_caracteres
    base, sobra = divmod(caracteres, quantos)
    for indice in range(quantos):
        caderno.acrescentar(facto_ficticio(indice + 1, base + (1 if indice < sobra else 0)))
    return caderno.factos_para_contexto()


def construir_cenarios(memoria: ConfigMemoria, pasta: Path) -> list[Cenario]:
    """Os tres cenarios, pelas classes reais da memoria e com os limites dados."""
    curto = HistoricoDePerguntas.da_config(memoria)
    for pergunta, resposta in TROCAS_FICTICIAS[-5:]:
        curto.acrescentar(pergunta, resposta)
    cheio = HistoricoDePerguntas.da_config(memoria)
    for pergunta, resposta in TROCAS_FICTICIAS:
        cheio.acrescentar(
            _ate(pergunta, ENCHIMENTO_DA_PERGUNTA, MAXIMO_DA_PERGUNTA),
            _ate(resposta, ENCHIMENTO_DA_RESPOSTA, MAXIMO_DA_RESPOSTA),
        )
    caderno = CadernoDeFactos.da_config(memoria, Path(pasta) / "factos.json")
    factos = encher_caderno(caderno)
    return [
        Cenario("sem-memoria", "pergunta sozinha, sem historico nem factos", PERGUNTA_SOZINHA),
        Cenario(
            "seguimento-5",
            "seguimento com 5 trocas recentes",
            PERGUNTA_DE_SEGUIMENTO,
            curto.trocas(),
        ),
        Cenario(
            "seguimento-10-cheio",
            "seguimento com 10 trocas no tamanho maximo e o caderno cheio",
            PERGUNTA_DE_SEGUIMENTO,
            cheio.trocas(),
            factos,
        ),
    ]


def configuracao_ficticia(perguntas: ConfigPerguntas) -> ConfigPerguntas:
    """So o modelo e o limite de tempo reais; a localizacao e a por omissao."""
    return ConfigPerguntas(modelo=perguntas.modelo, limite_s=perguntas.limite_s, localizacao=LOCALIZACAO_PADRAO)


# --- Teto garantido ------------------------------------------------------------


def _partes(total: int, quantas: int) -> list[int]:
    base, sobra = divmod(total, quantas)
    return [base + (1 if i < sobra else 0) for i in range(quantas)]


def _contexto_maximo(memoria: ConfigMemoria, letra: str) -> tuple[tuple[Troca, ...], tuple[str, ...]]:
    # O historico corta cada texto no maximo e acrescenta "…" (mais um caractere).
    trocas = tuple(
        Troca(letra * (MAXIMO_DA_PERGUNTA + 1), letra * (MAXIMO_DA_RESPOSTA + 1)) for _ in range(memoria.trocas)
    )
    factos = tuple(letra * n for n in _partes(memoria.caracteres, memoria.factos))
    return trocas, factos


def teto_do_contexto(memoria: ConfigMemoria, perguntas: ConfigPerguntas, lingua: str = "pt") -> Teto:
    """O teto do contexto e do pedido que os limites garantem, qualquer que seja o texto.

    O historico guarda no maximo `trocas` trocas com cada texto cortado, e o
    caderno envia no maximo `factos` factos e `caracteres` caracteres. No
    pior caso cada caractere e escapado no JSON do item (conta a dobrar):
    e isso que as aspas dao, por isso o teto garantido e medido com aspas.
    """
    normal = contexto_da_memoria(*_contexto_maximo(memoria, "a"))
    trocas, factos = _contexto_maximo(memoria, '"')
    garantido = contexto_da_memoria(trocas, factos)
    pedido = texto_do_pedido(
        "x" * MAXIMO_DO_TEXTO, perguntas, lingua, _DATA_MAIS_COMPRIDA, trocas=trocas, factos=factos
    )
    return Teto(memoria.trocas, memoria.factos, memoria.caracteres, len(normal), len(garantido), len(pedido))


# --- Medicao --------------------------------------------------------------------


def medir(perguntas: PerguntasGerais, cenario: Cenario, relogio: Callable[[], float] = time.monotonic) -> Medicao:
    pedido = texto_do_pedido(
        cenario.pergunta,
        perguntas.config,
        perguntas.lingua,
        perguntas.hoje(),
        trocas=cenario.trocas,
        factos=cenario.factos,
    )
    inicio = relogio()
    resultado = perguntas.responder(cenario.pergunta, trocas=cenario.trocas, factos=cenario.factos)
    return Medicao(cenario, resultado, relogio() - inicio, len(pedido))


def versao_do_cli(cli: str) -> str:
    """A versao do Claude Code, ou "?" se nao se conseguir ler."""
    try:
        feito = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    return (feito.stdout or "").strip().splitlines()[0][:80] if (feito.stdout or "").strip() else "?"


def _n(valor) -> str:
    return "-" if valor is None else str(valor)


def _custo(valor: float | None) -> str:
    return "-" if valor is None else f"{valor:.4f}"


def linhas_do_resumo(
    medicoes: list[Medicao],
    teto: Teto,
    *,
    modelo: str,
    versao: str,
    lingua: str,
    quando: datetime.datetime,
) -> list[str]:
    linhas = [
        f"# Gasto das perguntas gerais sem e com memoria ({quando:%Y-%m-%d %H:%M})",
        "",
        f"Claude Code real ({versao}), modelo `{modelo}`, lingua `{lingua}`, pelo caminho do jarvis "
        "(`PerguntasGerais`, `claude -p` so com WebSearch/WebFetch).",
        "Dados ficticios fixos do script; nem a localizacao, nem os projetos, nem o caderno do utilizador "
        f"foram enviados (localizacao por omissao: {LOCALIZACAO_PADRAO}).",
        "",
        "| cenario | trocas | factos | caracteres de memoria | caracteres do pedido | estado | entrada | "
        "criacao de cache | leitura de cache | entrada total | saida | custo USD | pesquisas | tempo | "
        "duration_ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in medicoes:
        r = m.resultado
        linhas.append(
            f"| {m.cenario.nome} | {len(m.cenario.trocas)} | {len(m.cenario.factos)} "
            f"| {r.caracteres_do_contexto} | {m.caracteres_do_pedido} | {r.estado} "
            f"| {_n(m.uso('input_tokens'))} | {_n(m.uso('cache_creation_input_tokens'))} "
            f"| {_n(m.uso('cache_read_input_tokens'))} | {_n(m.entrada_total)} | {_n(m.uso('output_tokens'))} "
            f"| {_custo(r.custo_usd)} | {_n(m.uso('web_search_requests'))} | {m.tempo_s:.1f} s "
            f"| {_n(r.duracao_ms)} |"
        )
    linhas += [
        "",
        "`entrada total` = entrada + criacao de cache + leitura de cache: tudo o que o modelo leu, incluindo "
        "as instrucoes fixas do proprio Claude Code e o texto das pesquisas na web. A cache dura poucos "
        "minutos: a primeira pergunta paga a criacao da cache dessas instrucoes fixas e as seguintes leem-na.",
        "",
        "## Teto garantido pelos limites",
        "",
        f"Limites de `[memoria]` usados: {teto.trocas} trocas (pergunta ate {MAXIMO_DA_PERGUNTA} e resposta ate "
        f"{MAXIMO_DA_RESPOSTA} caracteres cada), {teto.factos} factos, {teto.caracteres} caracteres de factos.",
        "",
        f"- contexto de memoria no pior caso com texto normal: {teto.contexto_texto_normal} caracteres;",
        f"- contexto de memoria garantido (cada caractere escapado no JSON): {teto.contexto_garantido} caracteres;",
        f"- pedido inteiro garantido (instrucoes, memoria e a pergunta maxima de {MAXIMO_DO_TEXTO} caracteres, "
        f"com a localizacao configurada): {teto.pedido_garantido} caracteres.",
    ]
    base = medicoes[0] if medicoes else None
    pior = medicoes[-1] if medicoes else None
    if (
        base is not None
        and pior is not None
        and base.entrada_total is not None
        and pior.entrada_total is not None
        and pior.resultado.caracteres_do_contexto > 0
    ):
        extra = pior.entrada_total - base.entrada_total
        por_caractere = extra / pior.resultado.caracteres_do_contexto
        linhas += [
            "",
            f"Entrada a mais do pior caso medido sobre a pergunta sem memoria: {extra} tokens para "
            f"{pior.resultado.caracteres_do_contexto} caracteres de memoria ({por_caractere:.2f} tokens por "
            "caractere; inclui a variacao do texto das pesquisas). Estimativa do teto da memoria em tokens com "
            f"esta razao: {round(teto.contexto_texto_normal * por_caractere)} (texto normal).",
        ]
    linhas += [
        "",
        "O gasto das pesquisas na web nao depende da memoria: fica limitado pelo limite de tempo de "
        "`[perguntas]`.",
        "",
        "## Veredito",
        "",
    ]
    respondidas = sum(1 for m in medicoes if m.resultado.respondida)
    linhas.append(f"- respondidas: {respondidas} de {len(medicoes)}")
    dentro = all(m.resultado.caracteres_do_contexto <= teto.contexto_garantido for m in medicoes)
    linhas.append(f"- contexto de memoria sempre dentro do teto garantido: {'sim' if dentro else 'NAO'}")
    linhas += ["", "## Respostas (como seriam faladas)", ""]
    for m in medicoes:
        r = m.resultado
        falado = resumo_falado(r.texto, lingua=lingua) if r.respondida else f"({r.estado}: {r.motivo})"
        linhas.append(f"- **{m.cenario.nome}** ({m.cenario.descricao}), pergunta \"{m.cenario.pergunta}\": {falado}")
    return linhas


# --- Linha de comandos ------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mede o gasto real de tokens de uma pergunta geral sem e com memoria (dados ficticios).",
        epilog="Gasta quota da subscricao Claude. A evidencia fica em docs/forja/evidence/ (ignorada pelo Git).",
    )
    parser.add_argument(
        "--gasta-quota",
        action="store_true",
        help="confirma que se podem fazer 3 perguntas reais ao Claude (gasta quota da subscricao)",
    )
    parser.add_argument("--config", default=None, metavar="FICHEIRO")
    parser.add_argument("--lingua", choices=("en", "pt"), default="en")
    parser.add_argument("--saida", default=None, metavar="FICHEIRO", help="ficheiro Markdown do resumo")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    arrancar: Callable[..., subprocess.Popen] = subprocess.Popen,
    cli: str | None = None,
    versao: Callable[[str], str] = versao_do_cli,
    agora: Callable[[], datetime.datetime] = datetime.datetime.now,
) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if not args.gasta_quota:
        print(
            "Recusado: isto faz 3 perguntas reais ao Claude e gasta quota da subscricao.\n"
            "Para correr mesmo, repete com --gasta-quota.",
            file=sys.stderr,
        )
        return 2

    try:
        config = carregar_config(Path(args.config) if args.config else CAMINHO_CONFIG_PADRAO)
        perguntas_cfg, memoria_cfg = config.perguntas, config.memoria
        proibidas = [RAIZ, *(projeto.caminho for projeto in config.projetos)]
    except ConfigError as erro:
        print(f"AVISO: {erro}\nA usar os valores por omissao de [perguntas] e [memoria].", file=sys.stderr)
        perguntas_cfg, memoria_cfg, proibidas = ConfigPerguntas(), ConfigMemoria(), [RAIZ]

    try:
        executavel = verificar_executavel_seguro(cli if cli is not None else localizar_cli())
    except (FileNotFoundError, ValueError) as erro:
        print(
            f"O CLI do Claude Code nao esta disponivel: nada foi perguntado nem medido.\n{erro}",
            file=sys.stderr,
        )
        return 3

    ficticia = configuracao_ficticia(perguntas_cfg)
    perguntas = PerguntasGerais(ficticia, args.lingua, pastas_proibidas=proibidas, cli=executavel, arrancar=arrancar)
    teto = teto_do_contexto(memoria_cfg, perguntas_cfg)
    with tempfile.TemporaryDirectory(prefix="jarvis-medir-gasto-") as pasta:
        cenarios = construir_cenarios(memoria_cfg, Path(pasta))
    medicoes = []
    for cenario in cenarios:
        print(f"a perguntar: {cenario.nome} ({len(cenario.trocas)} trocas, {len(cenario.factos)} factos) ...")
        medicao = medir(perguntas, cenario)
        r = medicao.resultado
        print(
            f"  {r.estado} em {medicao.tempo_s:.1f} s | uso={dict(r.uso) if r.uso is not None else '-'} "
            f"| custo_usd={_custo(r.custo_usd)} | memoria={r.caracteres_do_contexto} caracteres"
        )
        medicoes.append(medicao)

    quando = agora()
    linhas = linhas_do_resumo(
        medicoes, teto, modelo=ficticia.modelo, versao=versao(executavel), lingua=args.lingua, quando=quando
    )
    saida = Path(args.saida) if args.saida else PASTA_DE_PROVA / f"gasto-perguntas-{quando:%Y%m%d-%H%M%S}.md"
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"resumo: {saida}")
    return 0 if all(m.resultado.respondida for m in medicoes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
