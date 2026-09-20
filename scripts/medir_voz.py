r"""Arnes de medicao com audio SINTETICO das quatro combinacoes da D53
(PT/EN x com/sem prefixo "hey jarvis") (T1/T7, D7/D34/D53/D58).

AVISO OBRIGATORIO (D34), repetido no ficheiro de evidencia e em qualquer
relatorio que use estes numeros: esta medicao usa audio SINTETICO (a propria
voz Piper pt-PT do jarvis, gerada por `scripts/gerar_wav.py` e transcrita de
volta por `scripts/transcrever_ficheiro.py` — S11: nao ha voz inglesa neste
run, por isso a amostra `frases-en.md` tambem sai lida com sotaque pt-PT).
Isto testa a CADEIA — sintese -> transcricao -> encaminhador — e serve para
apanhar regressoes de codigo. NAO mede o reconhecimento da voz (nem do
portugues nem do ingles) do Sponsor e fica PROIBIDO concluir seja o que for
sobre qual lingua ele deve usar com base nestes numeros (protocolo completo
em tests/voz/frases-pt.md e tests/voz/frases-en.md, D7/D58).

O QUE ESTE SCRIPT FAZ, para cada uma das 20 frases da amostra escolhida
(`--amostra`, por omissao tests/voz/frases-pt.md):

  1. substitui os marcadores <projeto-1>/<projeto-2> pelos nomes REAIS da
     configuracao (config.toml se existir; senao degrada para o exemplo
     versionado, config.exemplo.toml, com um aviso escrito — nunca rebenta so
     por falta de config.toml, D10);
  2. calcula a intencao ESPERADA com `jarvis.router.encaminhar()` sobre a
     frase escrita (a fonte da verdade, nunca um valor fixado a mao);
  3. sintetiza essa frase em WAV com `scripts.gerar_wav.gerar_wav` (reutilizado
     tal e qual, sem alteracoes — fora de ambito desta task);
  4. transcreve o WAV com `scripts.transcrever_ficheiro.transcrever`
     (`initial_prompt=None` por omissao, D51 — a medicao e sempre "seca");
  5. calcula a intencao OBTIDA com o mesmo `encaminhar()` sobre a transcricao;
  6. compara esperado/obtido (acerto de intencao) e calcula o WER
     (distancia de edicao ao nivel da palavra, Python puro, stdlib — D3,
     T7: sem jiwer nem qualquer dependencia nova).

NUNCA executa a acao nem abre o canal do Claude Code (fora de ambito da T7):
so o texto que o router devolveria e comparado. O audio gerado e temporario
(escrito dentro de `audio/`, que o .gitignore ja cobre por inteiro) e apagado
no fim de cada frase, a menos que `--manter-audio` seja passado.

ONDE A EVIDENCIA PODE SER ESCRITA (correcao do SECURITY-REJECT da tentativa 1):
com um `config.toml` real, o ficheiro produzido leva os NOMES e os CAMINHOS
ABSOLUTOS reais dos projetos do Sponsor (o router devolve o caminho no campo
`argumento`). Por isso `--saida` nao aceita um caminho qualquer do disco: tem
de acabar em `.md` e de cair dentro de `docs/forja/evidence/`, a unica pasta
deste repositorio que o `.gitignore` apanha para ficheiros `.md`. Mesma regra,
e pela mesma razao, que `jarvis/audio_util.py::caminho_wav_de_saida` ja aplica
aos WAV (D1/D10/D48(2): a linha de comandos e entrada externa).

TESTES SILENCIOSOS POR OMISSAO (D61, TECHNOLOGY.md S13): este e um arnes de
medicao — por omissao NAO toca nenhum dispositivo de audio, so escreve e le os
WAV temporarios de `audio/medir-voz/` (apagados no fim, a menos que
`--manter-audio`). Ouvir cada frase enquanto e sintetizada e `--com-som`,
sempre opt-in explicito e nunca uma variavel de ambiente (D61.2).

Uso:
    .venv\Scripts\python scripts/medir_voz.py
    .venv\Scripts\python scripts/medir_voz.py --device cpu
    .venv\Scripts\python scripts/medir_voz.py --manter-audio
    .venv\Scripts\python scripts/medir_voz.py --amostra tests/voz/frases-en.md
    .venv\Scripts\python scripts/medir_voz.py --prefixo "hey jarvis, "
    .venv\Scripts\python scripts/medir_voz.py --modelo small
    .venv\Scripts\python scripts/medir_voz.py --com-som
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import PASTA_AUDIO, garantir_pasta  # noqa: E402
from jarvis.config import (  # noqa: E402
    CAMINHO_CONFIG_PADRAO,
    CAMINHO_EXEMPLO,
    Config,
    ConfigError,
    carregar_config,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.router import encaminhar  # noqa: E402

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` SEM mexer no sys.path do processo.

    `scripts/` nao e um pacote (sem `__init__.py`), por isso um
    `import gerar_wav` so funciona se `scripts/` estiver no sys.path. Meter la
    a pasta (o que a tentativa 1 fazia com `sys.path.insert(0, ...)`) punha-a
    A FRENTE da biblioteca padrao para TODO o processo — incluindo a suite de
    testes, que importa este modulo: um futuro `scripts/types.py`,
    `scripts/io.py` ou `scripts/re.py` passava a sombrear a stdlib em todo o
    lado. Carregar por caminho explicito tem o mesmo efeito util e nenhum
    efeito global (nit 3 do Security Reviewer, T7 a1).
    """
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


gerar_wav_mod = _carregar_modulo_irmao("gerar_wav")
transcrever_mod = _carregar_modulo_irmao("transcrever_ficheiro")

CAMINHO_AMOSTRA_PADRAO = RAIZ / "tests" / "voz" / "frases-pt.md"
PASTA_EVIDENCIA_PADRAO = RAIZ / "docs" / "forja" / "evidence"
PASTA_AUDIO_TEMPORARIO = PASTA_AUDIO / "medir-voz"

#: Marcadores da amostra (D1/D10): nunca um nome real do Sponsor em ficheiro
#: versionado. Substituidos em runtime pelos nomes da configuracao carregada.
MARCADOR_PROJETO_1 = "<projeto-1>"
MARCADOR_PROJETO_2 = "<projeto-2>"

#: Sufixo obrigatorio do ficheiro de evidencia (ver `caminho_evidencia_de_saida`).
SUFIXO_DE_EVIDENCIA_PERMITIDO = ".md"

#: Separador de celulas de uma tabela markdown: um `|` que NAO esteja escapado.
SEPARADOR_DE_CELULA = re.compile(r"(?<!\\)\|")

#: Primeira coluna de uma linha de frase: o numero da frase.
PADRAO_NUMERO = re.compile(r"^\d+$")

#: Linha separadora do cabecalho markdown (`|----|------|`): ignorada em silencio.
PADRAO_CELULA_SEPARADORA = re.compile(r"^:?-{2,}:?$")

TIPOS_DOCUMENTADOS = ("local", "claude")


class AmostraError(Exception):
    """A amostra de frases nao pode ser lida ou esta mal formada."""


@dataclass(frozen=True)
class FraseDaAmostra:
    """Uma linha da tabela de tests/voz/frases-pt.md, ja parseada."""

    numero: int
    tipo_documentado: str
    frase_com_marcadores: str
    intencao_documentada: str


@dataclass(frozen=True)
class ResultadoWer:
    """Word Error Rate ao nivel da palavra (distancia de edicao, stdlib)."""

    wer: float
    distancia_edicao: int
    n_palavras_referencia: int
    n_palavras_hipotese: int


@dataclass(frozen=True)
class LinhaMedida:
    """O resultado de uma frase depois de passar pela cadeia inteira.

    `erro` so e preenchido quando a frase rebentou a meio (sintese, GPU,
    transcricao): nesse caso a linha continua a existir na evidencia, com a
    intencao obtida vazia, acerto False e WER de 100% — o que se perde e a
    medicao daquela frase, nunca o ficheiro inteiro (nit 3 do Reviewer).
    """

    numero: int
    tipo_documentado: str
    frase_esperada: str
    transcricao: str
    tipo_esperado: str
    nome_acao_esperado: str | None
    argumento_esperado: str | None
    tipo_obtido: str
    nome_acao_obtido: str | None
    argumento_obtido: str | None
    acertou_intencao: bool
    wer: ResultadoWer
    prompt_estado: str = "?"
    modelo_usado: str = "?"
    device_usado: str = "?"
    duracao_audio_s: float = 0.0
    #: So a transcricao (o dict de `transcrever()` traz a chave com este nome);
    #: o carregamento do modelo esta em `latencia_total_ms`, nao aqui (nit 1 do
    #: Reviewer, T7 a2: o campo guardava o tempo TOTAL com o nome da parte).
    latencia_transcricao_ms: float = 0.0
    #: Carregamento do modelo + transcricao. Com a cache ligada (o arnes liga-a)
    #: so a primeira frase paga o carregamento, por isso este numero NAO e
    #: comparavel com as latencias a frio medidas na T3/T6.
    latencia_total_ms: float = 0.0
    erro: str | None = None


@dataclass(frozen=True)
class Agregados:
    """Os numeros do fim do ficheiro de evidencia.

    Dois WER de proposito, porque nao sao a mesma coisa e o Reviewer da
    tentativa 1 apanhou a ambiguidade: a macro-media trata as 20 frases por
    igual (uma frase curta mal transcrita pesa tanto como uma longa), o WER de
    corpus pesa cada frase pelo numero de palavras.

    `n_divergentes` (T1, D53): quantas linhas tem o tipo DOCUMENTADO na
    tabela (`local`/`claude`) diferente do tipo CALCULADO por
    `encaminhar()` sobre a frase sem prefixo (`tipo_esperado`). Sem isto, uma
    amostra cujas frases `local` ainda nao tem lista branca (o caso de
    `frases-en.md` antes da T7/D58) aparece com acerto de intencao alto so
    por construcao: a intencao OBTIDA tambem bate `claude` contra `claude`
    assim que a transcricao reproduzir a frase, e isso conta como acerto sem
    a amostra estar a medir o que diz medir.
    """

    n_frases: int
    n_acertos: int
    n_erros: int
    n_divergentes: int
    acerto_intencao_pct: float
    wer_macro_pct: float
    wer_corpus_pct: float


# --- Celulas de tabela markdown: escrever e ler sem deslocar colunas --------


def celula_markdown(texto: object) -> str:
    """Torna um valor arbitrario seguro dentro de uma celula de tabela markdown.

    A transcricao e saida de um modelo e a frase vem de um ficheiro: um `|` no
    meio parte a linha em mais colunas e o `acerto` que um humano (ou um
    agente) le passa a ser outra coluna qualquer — foi provado na tentativa 1
    com a transcricao `isto | NAO | sim | 0.0% | lixo` (nit 2 do Security
    Reviewer). Escapa-se o `|` e achatam-se as mudancas de linha.

    Limite conhecido e aceite: uma `\\` mesmo antes de um `|` no texto original
    fica ambigua para `dividir_celulas` (o unico consumidor do formato inverso
    e a amostra versionada, escrita por nos, e um teste garante que ela continua
    a dar 20 linhas).
    """
    limpo = str(texto).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    limpo = limpo.replace("|", r"\|")
    return " ".join(limpo.split()) or "—"


def dividir_celulas(linha: str) -> list[str] | None:
    """Divide uma linha de tabela markdown pelos `|` NAO escapados.

    Devolve `None` se a linha nem sequer parece uma linha de tabela. As celulas
    vem ja sem espacos nas pontas e com `\\|` traduzido de volta para `|`.
    """
    bruta = linha.strip()
    if not bruta.startswith("|"):
        return None
    partes = SEPARADOR_DE_CELULA.split(bruta)
    if partes and not partes[0].strip():
        partes = partes[1:]
    if partes and not partes[-1].strip():
        partes = partes[:-1]
    return [parte.strip().replace(r"\|", "|") for parte in partes]


# --- Leitura da amostra fixa -------------------------------------------------


def ler_amostra(
    caminho: Path = CAMINHO_AMOSTRA_PADRAO,
    avisos: list[str] | None = None,
) -> list[FraseDaAmostra]:
    """Le tests/voz/frases-pt.md e devolve as linhas da tabela, por ordem.

    So as linhas da tabela de frases contam (numero na primeira coluna, tipo
    reconhecido na segunda); o resto do ficheiro (protocolo da D7, avisos da
    D34) e prosa para humanos e e ignorado de proposito.

    Uma linha de tabela que PARECE uma frase e nao casa (numero mal escrito,
    tipo errado, colunas a mais ou a menos) nao desaparece em silencio: e
    contada, escrita em stderr e acrescentada a `avisos` se a lista for dada
    (nit 4 do Security Reviewer — uma 21.a frase com um erro na coluna `tipo`
    era medida como 20 sem ninguem saber).
    """
    if not caminho.is_file():
        raise AmostraError(
            f"amostra nao encontrada: '{caminho}'. Este ficheiro tem de estar "
            "versionado em tests/voz/frases-pt.md (D7)."
        )
    linhas: list[FraseDaAmostra] = []
    ignoradas: list[str] = []
    for n_linha, bruta in enumerate(caminho.read_text(encoding="utf-8").splitlines(), start=1):
        celulas = dividir_celulas(bruta)
        if celulas is None:
            continue  # prosa: nem sequer e uma linha de tabela
        celulas_com_texto = [celula for celula in celulas if celula]
        if celulas_com_texto and all(
            PADRAO_CELULA_SEPARADORA.match(celula) for celula in celulas_com_texto
        ):
            continue  # |----|----| do cabecalho markdown
        # Uma linha de celulas TODAS vazias (`| | | |`) nao e separador nenhum:
        # sem o `celulas_com_texto and`, o `all()` de uma lista vazia dava True
        # e a linha sumia sem entrar na contagem de avisos (nit 6 do Reviewer,
        # T7 a2). Cai a baixo e e contada como linha ignorada.
        if len(celulas) == 4 and PADRAO_NUMERO.match(celulas[0]) and celulas[1] in TIPOS_DOCUMENTADOS:
            linhas.append(
                FraseDaAmostra(
                    numero=int(celulas[0]),
                    tipo_documentado=celulas[1],
                    frase_com_marcadores=celulas[2],
                    intencao_documentada=celulas[3],
                )
            )
            continue
        if celulas and celulas[0].lower() in ("nº", "n.º", "no", "n", "numero", "número"):
            continue  # cabecalho da tabela
        ignoradas.append(
            f"linha {n_linha} ignorada ({len(celulas)} colunas, "
            f"primeira='{celulas[0] if celulas else ''}', "
            f"segunda='{celulas[1] if len(celulas) > 1 else ''}'): {bruta.strip()!r}"
        )

    if ignoradas:
        cabecalho = (
            f"AVISO: '{caminho.name}' tem {len(ignoradas)} linha(s) de tabela que NAO foram "
            f"medidas (esperado: 4 colunas, numero na 1.a, {TIPOS_DOCUMENTADOS} na 2.a):"
        )
        print(cabecalho, file=sys.stderr)
        for aviso in ignoradas:
            print(f"  - {aviso}", file=sys.stderr)
        if avisos is not None:
            avisos.append(cabecalho)
            avisos.extend(ignoradas)

    if not linhas:
        raise AmostraError(
            f"'{caminho}': nenhuma linha de frase encontrada (a tabela tem de "
            "ter colunas '| nº | tipo | frase | intenção |')."
        )
    return linhas


# --- Marcadores de projeto ---------------------------------------------------


def substituir_marcadores(texto: str, nomes_projetos: Sequence[str]) -> str:
    """Troca <projeto-1>/<projeto-2> pelos nomes reais da configuracao.

    Com um so projeto configurado, os dois marcadores usam o mesmo nome (a
    frase continua valida, so perde variedade) — degradar em vez de rebentar,
    D10. Uma frase sem marcadores nenhuns passa tal e qual.
    """
    if not nomes_projetos:
        if MARCADOR_PROJETO_1 in texto or MARCADOR_PROJETO_2 in texto:
            raise AmostraError(
                f"a frase {texto!r} usa um marcador de projeto mas a configuracao "
                "nao tem nenhum projeto (config.toml ou config.exemplo.toml vazios?)."
            )
        return texto
    nome_1 = nomes_projetos[0]
    nome_2 = nomes_projetos[1] if len(nomes_projetos) > 1 else nomes_projetos[0]
    return texto.replace(MARCADOR_PROJETO_1, nome_1).replace(MARCADOR_PROJETO_2, nome_2)


# --- Configuracao, com degradacao clara se config.toml faltar (D10) --------


def carregar_config_para_arnes() -> tuple[Config, str]:
    """config.toml real; se faltar ou for invalido, degrada para o exemplo.

    Nunca rebenta so porque o Sponsor ainda nao criou o config.toml dele
    (este run nao o tem) — imprime um aviso claro em vez disso, como o ambito
    da T7 exige, e usa config.exemplo.toml (D10: dados ficticios, sem
    validar caminhos no disco, que nao existem mesmo).

    A ORIGEM devolvida e so o NOME do ficheiro, nunca o caminho absoluto: esse
    caminho leva o nome de utilizador do Sponsor e vai parar a evidencia
    (nit 1 do Security Reviewer, mesma classe da D50(5)).
    """
    try:
        return carregar_config(), f"{CAMINHO_CONFIG_PADRAO.name} (configuracao real do Sponsor)"
    except ConfigError as erro:
        aviso = (
            f"AVISO: '{CAMINHO_CONFIG_PADRAO.name}' nao encontrado ou invalido "
            f"({erro}); a usar o exemplo versionado '{CAMINHO_EXEMPLO.name}' "
            "(dados ficticios, D10) so para este arnes correr. Isto NAO substitui "
            "a medicao real com a configuracao do Sponsor."
        )
        print(aviso, file=sys.stderr)
        config = carregar_config(CAMINHO_EXEMPLO, validar_caminhos=False)
        return config, f"{CAMINHO_EXEMPLO.name} (fallback; {CAMINHO_CONFIG_PADRAO.name} em falta)"


# --- Confinamento do ficheiro de evidencia (D1/D10/D48(2)) ------------------


def caminho_evidencia_de_saida(
    valor: str | Path,
    pasta_permitida: Path | None = None,
    raiz: Path | None = None,
) -> Path:
    """Valida o `--saida` vindo da linha de comandos (entrada externa, D48(2)).

    Duas regras, as duas pela mesma razao: com um `config.toml` real o ficheiro
    de evidencia leva os NOMES dos projetos do Sponsor e os CAMINHOS ABSOLUTOS
    deles (o router devolve `argumento=str(projeto.caminho)`), e este
    repositorio vai ser publico (D1/D10).

    1. acaba em `.md`;
    2. fica dentro de `docs/forja/evidence/`. Nao basta "dentro do repositorio":
       so `docs/forja/` esta no `.gitignore`, logo um `--saida docs/EVIDENCIA.md`,
       `--saida README.md` ou `--saida tests/voz/resultado.md` cairia em pasta
       VERSIONADA. E foi provado na tentativa 1 que sem regra nenhuma o ficheiro
       chegava a escrever-se FORA do repositorio.

    Mesma forma e mesma intencao de `jarvis/audio_util.py::caminho_wav_de_saida`.
    Devolve o caminho absoluto ja normalizado; levanta `ValueError` legivel se
    alguma das duas falhar. Nao cria nem toca em nada no disco.
    """
    pasta = (PASTA_EVIDENCIA_PADRAO if pasta_permitida is None else Path(pasta_permitida)).resolve()
    # Um caminho relativo resolve-se contra a RAIZ do repositorio, como em
    # `caminho_wav_de_saida`, e NAO contra a pasta permitida: senao um
    # `--saida docs/EVIDENCIA.md` (um dos exemplos do SECURITY-REJECT) era
    # aceite em silencio a escrever noutro sitio que nao o que quem escreveu o
    # comando estava a pensar. Fora da pasta permitida recusa-se, ponto.
    raiz_para_relativos = (RAIZ if raiz is None else Path(raiz)).resolve()
    caminho = Path(valor).expanduser()
    if not caminho.is_absolute():
        caminho = raiz_para_relativos / caminho
    caminho = caminho.resolve()
    if caminho.suffix.lower() != SUFIXO_DE_EVIDENCIA_PERMITIDO:
        raise ValueError(
            f"saida '{valor}': a evidencia so se escreve em ficheiros "
            f"{SUFIXO_DE_EVIDENCIA_PERMITIDO} (este ficheiro leva os nomes e os caminhos "
            "reais dos projetos do Sponsor, D1/D10)"
        )
    if not caminho.is_relative_to(pasta):
        raise ValueError(
            f"saida '{valor}' cai fora de '{pasta}' (resolvida para {caminho}); a evidencia "
            "desta medicao leva nomes e caminhos reais do Sponsor e so pode ser escrita na "
            "unica pasta deste repositorio que o .gitignore apanha para ficheiros .md "
            "(docs/forja/evidence/) — D1/D10"
        )
    return caminho


# --- WER: distancia de edicao ao nivel da palavra, Python puro (stdlib) ----


def _palavras_para_wer(texto: str) -> list[str]:
    """minusculas, sem pontuacao, espacos colapsados, dividido em palavras.

    Mantem acentos de proposito: a diferenca entre "sao" e "são" e exatamente
    o tipo de erro que o WER de uma transcricao pt-PT deve apanhar.
    """
    minusculo = texto.lower()
    sem_pontuacao = re.sub(r"[^\w\s]", " ", minusculo, flags=re.UNICODE)
    sem_pontuacao = sem_pontuacao.replace("_", " ")
    return sem_pontuacao.split()


def calcular_wer(referencia: str, hipotese: str) -> ResultadoWer:
    """WER = distancia de edicao (Levenshtein) ao nivel da palavra / N palavras
    da referencia. Programacao dinamica O(n*m), so a biblioteca padrao (sem
    jiwer nem qualquer dependencia nova, D3/T7).

    Referencia vazia: WER 0.0 se a hipotese tambem for vazia (nada a errar),
    senao 1.0 (100%: tudo o que saiu e insercao sobre uma referencia vazia).
    """
    ref = _palavras_para_wer(referencia)
    hip = _palavras_para_wer(hipotese)
    n, m = len(ref), len(hip)

    if n == 0:
        return ResultadoWer(
            wer=1.0 if m else 0.0,
            distancia_edicao=m,
            n_palavras_referencia=0,
            n_palavras_hipotese=m,
        )

    linha_anterior = list(range(m + 1))
    for i in range(1, n + 1):
        linha_atual = [i] + [0] * m
        for j in range(1, m + 1):
            custo_substituicao = 0 if ref[i - 1] == hip[j - 1] else 1
            linha_atual[j] = min(
                linha_anterior[j] + 1,  # delecao de ref[i-1]
                linha_atual[j - 1] + 1,  # insercao de hip[j-1]
                linha_anterior[j - 1] + custo_substituicao,  # substituicao/igual
            )
        linha_anterior = linha_atual

    distancia = linha_anterior[m]
    return ResultadoWer(
        wer=distancia / n,
        distancia_edicao=distancia,
        n_palavras_referencia=n,
        n_palavras_hipotese=m,
    )


def calcular_agregados(linhas: Sequence[LinhaMedida]) -> Agregados:
    """Os tres numeros do fim: acerto de intencao, WER macro e WER de corpus.

    Nota sobre uma frase de referencia VAZIA (nit 7 do Reviewer, T7 a2): no WER
    de corpus ela soma os erros ao numerador e zero ao denominador, porque e
    isso que a definicao diz (erros ÷ palavras de referencia) — palavras a mais
    onde nao devia haver nenhuma sao insercoes e contam. No WER macro a mesma
    frase entra como 100%. Nenhuma frase da amostra versionada e vazia; um
    teste fixa os dois comportamentos para ninguem os mudar por engano.
    """
    n = len(linhas)
    n_acertos = sum(1 for linha in linhas if linha.acertou_intencao)
    n_erros = sum(1 for linha in linhas if linha.erro)
    n_divergentes = sum(1 for linha in linhas if linha.tipo_documentado != linha.tipo_esperado)
    soma_distancias = sum(linha.wer.distancia_edicao for linha in linhas)
    soma_referencia = sum(linha.wer.n_palavras_referencia for linha in linhas)
    return Agregados(
        n_frases=n,
        n_acertos=n_acertos,
        n_erros=n_erros,
        n_divergentes=n_divergentes,
        acerto_intencao_pct=(n_acertos / n * 100) if n else 0.0,
        wer_macro_pct=(sum(linha.wer.wer for linha in linhas) / n * 100) if n else 0.0,
        wer_corpus_pct=(soma_distancias / soma_referencia * 100) if soma_referencia else 0.0,
    )


# --- A cadeia inteira, frase a frase ----------------------------------------


def medir_uma_frase(
    frase_da_amostra: FraseDaAmostra,
    config: Config,
    nomes_projetos: Sequence[str],
    *,
    device: str,
    pasta_audio: Path,
    manter_audio: bool,
    modelo: str = transcrever_mod.MODELO_PREFERIDO,
    prefixo: str = "",
    com_som: bool = False,
) -> LinhaMedida:
    """Sintese -> transcricao -> encaminhador, para UMA frase da amostra.

    `prefixo` (D53, T1): colado SO ao texto que vai para o Piper ("hey
    jarvis, " + frase, por exemplo). A intencao ESPERADA continua a ser
    calculada por `encaminhar()` sobre a frase SEM prefixo — a fonte da
    verdade nunca muda por causa de um prefixo de sintese; um prefixo com
    "nao" no meio, por exemplo, nao pode fazer uma frase local passar a
    `claude` so porque foi colado ao audio. A referencia do WER, essa sim,
    passa a ser o texto REALMENTE sintetizado (com prefixo, se houver):
    e o que a transcricao tem de bater para dar WER zero.

    `modelo` (D53, T1): passado a `transcrever(modelo_preferido=...)`; por
    omissao continua a ser `transcrever_ficheiro.MODELO_PREFERIDO` (D39), o
    mesmo modelo que corria antes desta task existir.

    `com_som` (D61, T5): False por omissao — nenhum dispositivo de audio e
    aberto, so o WAV temporario e escrito e lido de volta. True toca cada
    frase nas colunas enquanto mede, so quando pedido de forma explicita.
    """
    frase_esperada = substituir_marcadores(frase_da_amostra.frase_com_marcadores, nomes_projetos)

    esperado = encaminhar(frase_esperada, config)

    texto_sintetizado = f"{prefixo}{frase_esperada}" if prefixo else frase_esperada

    garantir_pasta(pasta_audio)
    caminho_wav = pasta_audio / f"{frase_da_amostra.numero:02d}.wav"
    try:
        _, duracao_audio_s, _ = gerar_wav_mod.gerar_wav(
            texto_sintetizado, caminho_wav, com_som=com_som
        )
        resultado_transcricao = transcrever_mod.transcrever(
            caminho_wav, device=device, modelo_preferido=modelo, usar_cache_do_modelo=True
        )
    finally:
        if not manter_audio:
            caminho_wav.unlink(missing_ok=True)

    transcricao = resultado_transcricao["texto"]
    obtido = encaminhar(transcricao, config)

    acertou = (
        obtido.tipo == esperado.tipo
        and obtido.nome_acao == esperado.nome_acao
        and obtido.argumento == esperado.argumento
    )

    return LinhaMedida(
        numero=frase_da_amostra.numero,
        tipo_documentado=frase_da_amostra.tipo_documentado,
        frase_esperada=texto_sintetizado,
        transcricao=transcricao,
        tipo_esperado=esperado.tipo,
        nome_acao_esperado=esperado.nome_acao,
        argumento_esperado=esperado.argumento,
        tipo_obtido=obtido.tipo,
        nome_acao_obtido=obtido.nome_acao,
        argumento_obtido=obtido.argumento,
        acertou_intencao=acertou,
        wer=calcular_wer(texto_sintetizado, transcricao),
        prompt_estado=resultado_transcricao["prompt_estado"],
        modelo_usado=resultado_transcricao["modelo"],
        device_usado=resultado_transcricao["device"],
        duracao_audio_s=duracao_audio_s,
        latencia_transcricao_ms=resultado_transcricao["latencia_transcricao_ms"],
        latencia_total_ms=resultado_transcricao["latencia_ms"],
    )


def linha_falhada(
    frase_da_amostra: FraseDaAmostra,
    config: Config,
    nomes_projetos: Sequence[str],
    erro: BaseException,
    prefixo: str = "",
) -> LinhaMedida:
    """A linha que fica na evidencia quando UMA frase rebenta.

    Sem isto, uma excecao na frase 19 deitava fora as 18 ja medidas e o proprio
    entregavel (nit 3 do Reviewer). A frase conta como falhada: sem acerto de
    intencao e com WER de 100% (perderam-se todas as palavras da referencia).

    `prefixo` (T1): so afeta o texto mostrado/usado como referencia de WER
    (`frase_esperada`), pela mesma razao de `medir_uma_frase` — nunca entra
    no calculo da intencao esperada.
    """
    try:
        frase_esperada = substituir_marcadores(frase_da_amostra.frase_com_marcadores, nomes_projetos)
    except Exception:
        frase_esperada = frase_da_amostra.frase_com_marcadores
    texto_sintetizado = f"{prefixo}{frase_esperada}" if prefixo else frase_esperada
    try:
        esperado = encaminhar(frase_esperada, config)
        tipo_esperado = esperado.tipo
        nome_acao_esperado = esperado.nome_acao
        argumento_esperado = esperado.argumento
    except Exception:
        tipo_esperado = frase_da_amostra.tipo_documentado
        nome_acao_esperado = None
        argumento_esperado = None
    return LinhaMedida(
        numero=frase_da_amostra.numero,
        tipo_documentado=frase_da_amostra.tipo_documentado,
        frase_esperada=texto_sintetizado,
        transcricao="",
        tipo_esperado=tipo_esperado,
        nome_acao_esperado=nome_acao_esperado,
        argumento_esperado=argumento_esperado,
        tipo_obtido="—",
        nome_acao_obtido=None,
        argumento_obtido=None,
        acertou_intencao=False,
        wer=calcular_wer(texto_sintetizado, ""),
        erro=f"{type(erro).__name__}: {erro}",
    )


# --- Relatorio / evidencia ---------------------------------------------------

AVISO_D34 = (
    "**AVISO (D34): esta medicao usou AUDIO SINTETICO** (voz Piper do jarvis, "
    "gerada por `scripts/gerar_wav.py` e transcrita de volta por "
    "`scripts/transcrever_ficheiro.py`). Isto testa a CADEIA (sintese -> "
    "transcricao -> encaminhador) e NAO mede o reconhecimento da voz do "
    "Sponsor. **Fica proibido propor a troca para ingles com base nestes "
    "numeros** — essa proposta so pode acontecer depois de medir a voz humana "
    "dele (protocolo completo em `tests/voz/frases-pt.md`, D7), e mesmo ai fica "
    "na fila do Sponsor com o default 'manter portugues'."
)

#: T1/D53: registado no cabecalho de TODA corrida, PT ou EN — a voz Piper so
#: existe em pt-PT (S11), por isso mesmo a amostra `frases-en.md` sai lida com
#: sotaque/prosodia portugueses. Limite do teste da cadeia, nunca uma medida
#: do ingles do Sponsor (D34/D58).
AVISO_VOZ_PT_PT = (
    "**AVISO (S11): toda a sintese desta medição usa a voz Piper pt-PT** "
    "(`pt_PT-tugao-medium`) — não existe voz inglesa neste run. Mesmo quando a "
    "amostra é `frases-en.md`, o áudio sintetizado sai com sotaque e prosódia "
    "portugueses. Isto é um limite do TESTE DA CADEIA (síntese -> transcrição -> "
    "encaminhador), nunca uma medida de como o Sponsor fala inglês (D34)."
)

AVISO_PRIVACIDADE = (
    "**AVISO (D1/D10): este ficheiro pode conter dados privados do Sponsor.** Com um "
    "`config.toml` real, as colunas de frase e de intencao levam os NOMES dos projetos dele e os "
    "CAMINHOS ABSOLUTOS no disco (o encaminhador devolve o caminho do projeto no argumento). So e "
    "seguro porque `docs/forja/` esta no `.gitignore` e este ficheiro nunca e versionado — o "
    "repositorio vai ser publico. **Nao copiar excertos daqui para nenhum ficheiro versionado** "
    "(codigo, testes, README, relatorios commitados) sem trocar os nomes e os caminhos por "
    "marcadores, como `tests/voz/frases-pt.md` faz."
)


def _acao_para_texto(nome_acao: str | None, argumento: str | None) -> str:
    if nome_acao is None:
        return "—"
    if argumento is None:
        return nome_acao
    return f"{nome_acao} ({argumento})"


def escrever_evidencia(
    linhas: list[LinhaMedida],
    origem_config: str,
    device: str,
    caminho_saida: Path,
    duracao_total_s: float,
    avisos_da_amostra: Sequence[str] = (),
    caminho_amostra: Path | None = None,
    modelo: str | None = None,
    prefixo: str = "",
) -> Agregados:
    """Escreve o ficheiro de evidencia e devolve os agregados.

    `caminho_saida` TEM de vir de `caminho_evidencia_de_saida` (o `main` so
    escreve caminhos ja validados); aqui repete-se a validacao para que nenhum
    outro chamador consiga escrever fora de `docs/forja/evidence/`.

    `caminho_amostra`/`modelo`/`prefixo` (T1/D53): so para o CABECALHO da
    evidencia dizer qual das quatro combinacoes (PT/EN x com/sem prefixo)
    esta corrida mediu; `None`/`""` cai para a amostra pt-PT por omissao e
    para "nenhum" prefixo, sem quebrar chamadas antigas.
    """
    caminho_saida = caminho_evidencia_de_saida(caminho_saida)
    agregados = calcular_agregados(linhas)
    n = agregados.n_frases

    caminho_amostra_efetivo = caminho_amostra if caminho_amostra is not None else CAMINHO_AMOSTRA_PADRAO
    try:
        amostra_para_mostrar = str(caminho_amostra_efetivo.relative_to(RAIZ))
    except ValueError:
        amostra_para_mostrar = str(caminho_amostra_efetivo)

    partes: list[str] = []
    partes.append("# Medição sintética (D53/T1: PT/EN × com/sem prefixo)")
    partes.append("")
    partes.append(AVISO_D34)
    partes.append("")
    partes.append(AVISO_VOZ_PT_PT)
    partes.append("")
    partes.append(AVISO_PRIVACIDADE)
    partes.append("")
    partes.append(
        f"Gerado por `scripts/medir_voz.py` em "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"(duração total: {duracao_total_s:.1f} s)."
    )
    partes.append(f"- Amostra: `{amostra_para_mostrar}` ({n} frases)")
    partes.append(f"- Configuração usada: {origem_config}")
    partes.append(f"- Device de transcrição pedido: {device}")
    partes.append(f"- Modelo de transcrição pedido (--modelo): {modelo or '?'}")
    partes.append(
        f"- Prefixo usado (--prefixo): {prefixo!r}" if prefixo else "- Prefixo usado (--prefixo): (nenhum)"
    )
    if linhas:
        partes.append(
            f"- Modelo de transcrição observado: {linhas[0].modelo_usado} "
            f"(device real: {linhas[0].device_usado})"
        )
        partes.append(f"- Estado do initial_prompt (D51): {linhas[0].prompt_estado}")
    if agregados.n_erros:
        partes.append(
            f"- **Frases que rebentaram a meio: {agregados.n_erros}** (ficam na tabela com o erro "
            "escrito, contam como falha de intenção e WER de 100%)"
        )
    for aviso in avisos_da_amostra:
        partes.append(f"- {celula_markdown(aviso)}")
    partes.append("")
    partes.append("## Resultado, frase a frase")
    partes.append("")
    partes.append(
        "| nº | tipo | frase esperada | transcrição | intenção esperada | "
        "intenção obtida | acerto | WER | áudio (s) | transcrição (ms) |"
    )
    partes.append("|---|---|---|---|---|---|---|---|---|---|")
    for linha in linhas:
        intencao_esperada = (
            f"{linha.tipo_esperado}: "
            f"{_acao_para_texto(linha.nome_acao_esperado, linha.argumento_esperado)}"
        )
        if linha.erro:
            intencao_obtida = f"ERRO — {linha.erro}"
        else:
            intencao_obtida = (
                f"{linha.tipo_obtido}: "
                f"{_acao_para_texto(linha.nome_acao_obtido, linha.argumento_obtido)}"
            )
        acerto = "sim" if linha.acertou_intencao else "NAO"
        duracao = f"{linha.duracao_audio_s:.2f}" if linha.duracao_audio_s else "—"
        latencia = f"{linha.latencia_transcricao_ms:.0f}" if linha.latencia_transcricao_ms else "—"
        partes.append(
            f"| {linha.numero} | {celula_markdown(linha.tipo_documentado)} "
            f"| {celula_markdown(linha.frase_esperada)} "
            f"| {celula_markdown(linha.transcricao)} "
            f"| {celula_markdown(intencao_esperada)} "
            f"| {celula_markdown(intencao_obtida)} "
            f"| {acerto} | {linha.wer.wer * 100:.1f}% "
            f"| {duracao} | {latencia} |"
        )
    partes.append("")
    partes.append("## Agregados")
    partes.append("")
    partes.append(
        f"- **Acerto de intenção: {agregados.n_acertos}/{n} = "
        f"{agregados.acerto_intencao_pct:.1f}%**"
    )
    partes.append(
        f"- **WER (macro-média — média dos WER das {n} frases, cada frase pesa o mesmo): "
        f"{agregados.wer_macro_pct:.1f}%**"
    )
    partes.append(
        f"- **Linhas divergentes (tipo documentado na tabela != tipo calculado por "
        f"`encaminhar()` sobre a frase sem prefixo): {agregados.n_divergentes}/{n}** — sem "
        "este número, uma amostra cujas frases `local` ainda não têm lista branca (ex.: "
        "`frases-en.md` antes da T7/D58) aparecia com acerto de intenção alto só por "
        "construção: se a tabela documenta `local` mas o router (com razão) manda a frase "
        "como `claude`, a intenção OBTIDA também bate `claude` contra `claude` assim que a "
        "transcrição reproduzir a frase, o que conta como acerto sem medir o que a amostra "
        "diz medir."
    )
    partes.append(
        f"- **WER de corpus (Σ erros de edição ÷ Σ palavras de referência, frases longas pesam "
        f"mais): {agregados.wer_corpus_pct:.1f}%**"
    )
    latencias = [linha.latencia_transcricao_ms for linha in linhas if linha.latencia_transcricao_ms]
    if latencias:
        duracao_audio_total = sum(linha.duracao_audio_s for linha in linhas)
        partes.append(
            f"- Latência só da transcrição, por frase ({len(latencias)} frases medidas): "
            f"mediana {statistics.median(latencias):.0f} ms, mínimo {min(latencias):.0f} ms, "
            f"máximo {max(latencias):.0f} ms, para {duracao_audio_total:.1f} s de áudio "
            f"sintetizado no total."
        )
        totais = [linha.latencia_total_ms for linha in linhas if linha.latencia_total_ms]
        if totais:
            partes.append(
                f"- Latência total (carregamento do modelo + transcrição): "
                f"{max(totais):.0f} ms na pior frase, {statistics.median(totais):.0f} ms de "
                f"mediana. O arnês carrega o modelo UMA vez para as {n} frases, por isso só a "
                f"primeira paga o carregamento: estes números não são comparáveis com as "
                f"latências a frio medidas na T3, e não substituem a medição de latência do "
                f"caminho vivo que a D2/D11/D33 pedem (ali a frase vem do microfone, não de um "
                f"WAV já pronto)."
            )
    n_sem_acao_esperada = sum(
        1 for linha in linhas if linha.tipo_esperado == "claude" and linha.nome_acao_esperado is None
    )
    if n_sem_acao_esperada:
        partes.append("")
        partes.append(
            f"**Piso por construção do acerto de intenção, a ler antes de comparar com os "
            f"limiares da D7:** {n_sem_acao_esperada} das {n} frases têm como intenção esperada "
            f"`claude: —` (vão como texto para o Claude Code, sem ação nem argumento). Nessas, "
            f"o acerto compara `claude: —` com `claude: —`: uma transcrição completamente "
            f"errada continua a contar como ACERTO desde que o encaminhador não a confunda com "
            f"um comando da lista branca. É fiel à definição da D7 (a métrica que manda é o "
            f"encaminhamento, não as palavras), mas significa que esta parte da amostra quase só "
            f"falha se o router classificar lixo como comando local. Quem quiser saber o que a "
            f"transcrição acertou nessas frases lê o WER, não o acerto de intenção."
        )
    partes.append("")
    partes.append(
        "O agregado de WER que a D7 nomeia é um só «WER»; ficam aqui os dois cálculos por serem "
        "números diferentes sobre a mesma corrida, para ninguém escolher o mais conveniente depois "
        "de ver o resultado."
    )
    partes.append("")
    partes.append(
        f"Limiares de referência da D7 (aplicam-se à medição REAL com a voz do "
        f"Sponsor, não a esta passagem sintética): >= 90% de acerto de intenção "
        f"e WER <= 15% -> português fica; entre 75% e 90% -> português fica com "
        f"melhorias baratas antes de medir outra vez; < 75% -> repetir a MESMA "
        f"amostra em inglês e levar a comparação ao Sponsor pela fila, default "
        f"'manter português'."
    )
    partes.append("")
    partes.append(AVISO_D34)
    partes.append("")

    garantir_pasta(caminho_saida.parent)
    caminho_saida.write_text("\n".join(partes) + "\n", encoding="utf-8")
    return agregados


# --- CLI ---------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--device",
        default="cuda",
        choices=["cuda", "cpu"],
        help="device da transcricao (default: cuda)",
    )
    parser.add_argument(
        "--amostra",
        default=str(CAMINHO_AMOSTRA_PADRAO),
        help=(
            "caminho da amostra de 20 frases (default: tests/voz/frases-pt.md; a amostra "
            "inglesa da D58 e tests/voz/frases-en.md)"
        ),
    )
    parser.add_argument(
        "--saida",
        default=None,
        help=(
            "ficheiro .md a escrever DENTRO de docs/forja/evidence/ (default: "
            "docs/forja/evidence/medicao-sintetica-<timestamp>.md). Um caminho relativo conta a "
            "partir da raiz do repositorio; qualquer caminho fora dessa pasta, ou sem sufixo .md, "
            "e recusado com codigo 1 e sem escrever nada: a evidencia leva nomes e caminhos reais "
            "do Sponsor e so docs/forja/ esta fora do Git (D1/D10)"
        ),
    )
    parser.add_argument(
        "--manter-audio",
        action="store_true",
        help="nao apaga os WAV temporarios depois de cada frase (ficam em audio/medir-voz/, fora do Git)",
    )
    parser.add_argument(
        "--com-som",
        action="store_true",
        help=(
            "opt-in explicito para ouvir cada frase enquanto e medida (D61); sem esta flag "
            "nada toca nas colunas, so os WAV temporarios sao escritos e lidos de volta"
        ),
    )
    parser.add_argument(
        "--modelo",
        default=transcrever_mod.MODELO_PREFERIDO,
        choices=transcrever_mod.MODELOS_PERMITIDOS,
        help=(
            "modelo de transcricao, passado a transcrever(modelo_preferido=...) (default: o "
            f"mesmo modelo por omissao do transcritor, '{transcrever_mod.MODELO_PREFERIDO}', "
            "D39 — nao mudado por esta flag). Lista fechada = "
            "scripts/transcrever_ficheiro.py::MODELOS_PERMITIDOS (D53)"
        ),
    )
    parser.add_argument(
        "--prefixo",
        default="",
        metavar="TEXTO",
        help=(
            "texto colado ao INICIO do que e sintetizado (ex.: 'hey jarvis, '); por omissao "
            "vazio (sem prefixo). A intencao esperada continua a ser calculada por "
            "jarvis.router.encaminhar() sobre a frase SEM prefixo; a referencia do WER passa a "
            "ser o texto realmente sintetizado, com prefixo incluido (D53)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)

    print("=== jarvis - medir_voz (amostra sintetica, PT/EN x com/sem prefixo, D7/D34/D53) ===")
    print(AVISO_D34)
    print(AVISO_VOZ_PT_PT)
    print()

    # O caminho de saida e validado ANTES de sintetizar o que quer que seja:
    # um destino invalido tem de falhar em milissegundos e sem escrever nada,
    # nao depois de 20 frases (bloqueador 1 do Security Reviewer, T7 a1).
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        caminho_saida = caminho_evidencia_de_saida(
            args.saida
            if args.saida
            else PASTA_EVIDENCIA_PADRAO / f"medicao-sintetica-{timestamp}.md"
        )
    except ValueError as erro:
        print(f"FALHOU: {erro}", file=sys.stderr)
        return 1

    avisos_da_amostra: list[str] = []
    try:
        frases = ler_amostra(Path(args.amostra), avisos=avisos_da_amostra)
        config, origem_config = carregar_config_para_arnes()
        nomes_projetos = [projeto.nome for projeto in config.projetos]
    except (AmostraError, ConfigError) as erro:
        print(f"FALHOU: {erro}", file=sys.stderr)
        return 1

    print(f"configuracao    = {origem_config}")
    print(f"projetos        = {len(nomes_projetos)} projeto(s) da configuracao")
    print(f"frases          = {len(frases)}")
    print(f"modelo          = {args.modelo}")
    print(f"prefixo         = {args.prefixo!r}" if args.prefixo else "prefixo         = (nenhum)")
    print(f"com som         = {'sim (D61 opt-in)' if args.com_som else 'nao (so ficheiro, D61)'}")
    print(f"evidencia       = {caminho_saida.relative_to(RAIZ)}")
    print()

    t0 = time.perf_counter()
    linhas: list[LinhaMedida] = []
    interrompido = False
    for frase in frases:
        print(
            f"[{frase.numero:02d}/{len(frases)}] {frase.frase_com_marcadores!r} ...",
            end=" ",
            flush=True,
        )
        try:
            linha = medir_uma_frase(
                frase,
                config,
                nomes_projetos,
                device=args.device,
                pasta_audio=PASTA_AUDIO_TEMPORARIO,
                manter_audio=args.manter_audio,
                modelo=args.modelo,
                prefixo=args.prefixo,
                com_som=args.com_som,
            )
        except KeyboardInterrupt:
            print("INTERROMPIDO pelo utilizador")
            interrompido = True
            break
        except Exception as erro:  # noqa: BLE001 — uma frase nunca mata a corrida
            linha = linha_falhada(frase, config, nomes_projetos, erro, prefixo=args.prefixo)
            linhas.append(linha)
            print(f"ERRO ({linha.erro}) - continua para a frase seguinte")
            continue
        linhas.append(linha)
        marca = "OK" if linha.acertou_intencao else "FALHOU"
        print(f"{marca} (transcrito: {linha.transcricao!r}, WER {linha.wer.wer * 100:.0f}%)")
    duracao_total_s = time.perf_counter() - t0

    if not linhas:
        print("FALHOU: nenhuma frase chegou a ser medida; nada a escrever.", file=sys.stderr)
        return 1

    agregados = escrever_evidencia(
        linhas,
        origem_config,
        args.device,
        caminho_saida,
        duracao_total_s,
        avisos_da_amostra,
        caminho_amostra=Path(args.amostra),
        modelo=args.modelo,
        prefixo=args.prefixo,
    )

    print()
    print(f"frases medidas     = {agregados.n_frases}/{len(frases)}")
    print(f"acerto de intencao = {agregados.acerto_intencao_pct:.1f}%")
    print(f"WER macro-media    = {agregados.wer_macro_pct:.1f}%")
    print(f"WER de corpus      = {agregados.wer_corpus_pct:.1f}%")
    print(f"linhas divergentes = {agregados.n_divergentes}/{agregados.n_frases}")
    print(f"evidencia escrita em: {caminho_saida.relative_to(RAIZ)}")
    print()
    print(AVISO_D34)

    if interrompido:
        print("INTERROMPIDO: a evidencia acima so cobre as frases medidas ate a interrupcao.")
        return 1
    if agregados.n_erros:
        print(
            f"INCOMPLETO: {agregados.n_erros} frase(s) rebentaram; a evidencia foi escrita na "
            "mesma, com o erro por linha."
        )
        return 1
    print("OK: medicao sintetica completa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
