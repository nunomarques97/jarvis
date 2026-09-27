r"""Sessao guiada de naturalidade: 20 trocas com a voz real do Sponsor, e o relatorio.

    .venv\Scripts\python scripts/sessao_naturalidade.py --fase linha-de-base

Guia 20 trocas curtas com o jarvis a correr noutra janela (`python -m jarvis`,
microfone verdadeiro). As trocas vem de `tests/voz/guiao-naturalidade.md`:
uma saudacao social, um pedido de ajuda para cozinhar com seguimentos que so
se percebem com o contexto, uma pergunta de tempo com pesquisa, o estado de um
projeto dito de forma natural, um envio a um projeto com "yes", um aviso que
chega a meio da conversa, um pedido sem projeto, uma interrupcao, dormir e
acordar, e respostas locais.

Para cada troca: Enter para comecar, a troca faz-se na janela do jarvis,
Enter quando ele acabar, e depois UMA tecla: 1 natural, 2 pouco natural,
3 tive de repetir, 4 disse "hey jarvis" sem precisar. `p` salta a troca e `q`
guarda e sai (`--continuar` retoma a ultima sessao por acabar).

A sessao (horas de inicio e fim de cada troca e a tecla) fica em
`logs/naturalidade/` (ignorada pelo Git). No fim, o script le o log do
jarvis (`logs/jarvis-<data>.log`) nessas janelas, classifica cada frase
(caminho rapido local, cerebro sem pesquisa, cerebro com pesquisa na web,
pergunta geral do interprete) e escreve o relatorio em docs/forja/evidence/
(ignorada) com:

  * trocas marcadas naturais (meta >= 16/20);
  * resposta do cerebro sem pesquisa, da ultima voz a primeira frase falada
    (p50 <= 1,5 s);
  * resposta do cerebro com pesquisa na web, da ultima voz a primeira frase
    falada (p50 <= 3,5 s, p95 <= 6 s; "not measured" sem nenhuma pesquisa);
  * resposta local, da ultima voz ao primeiro som (p50 <= 1,0 s, p95 <= 1,6 s);
  * so quando ha perguntas gerais pelo interprete (o recurso sem cerebro): da
    ultima voz a primeira frase da resposta (p50 <= 3,5 s, p95 <= 6 s) e ao
    primeiro som de qualquer tipo (<= 1,2 s);
  * repeticoes (<= 1), "hey jarvis" sem precisar (0) e perguntas
    desnecessarias do jarvis (<= 1);
  * interromper: do inicio da fala a voz parada (p95 < 300 ms, 0 falsas),
    "not measured" enquanto o log nao tiver as linhas da interrupcao;
  * em nota, os tokens de entrada de cada troca do cerebro.

As latencias medem-se a partir da ultima voz (o ultimo chunk que o VAD chamou
voz, linha `ultima voz:` de cada frase), nunca do fecho da escuta. Linhas do
log que este script le:

    frase #N | etapa 3/5 interprete | ... | intencao=X projeto=Y ...
    frase #N | ultima voz: <data hora> | X ms antes do fim da escuta
    frase #N | resposta falada desde a ultima voz: X ms
    frase #N | desfecho: <estado> | <motivo>
    pergunta | resposta falada: X ms desde a ultima voz da frase #N
    cerebro | resposta falada: X ms desde a ultima voz da frase #N
    cerebro | <estado> em X s (<motivo>) | intencao=cerebro | pesquisa web: sim|nao | tokens: ...
    interrupcao | voz parada: X ms desde o inicio da fala
    interrupcao | falsa: <motivo>

A linha de resumo de um turno do cerebro nao diz o numero da frase: cada
frase com `intencao=cerebro` abre um turno e o cerebro corre um turno de cada
vez, por isso cada resumo fecha o turno mais antigo ainda aberto do mesmo
processo.

So contam frases do microfone: um jarvis a correr com `--wav` (ficheiros,
voz sintetica) fica de fora. Sem sessao do Sponsor, o relatorio diz
"PENDING - Sponsor step" e o passo exato; nada e simulado. O relatorio so
leva numeros e ids do guiao: nem transcricoes, nem nomes de projetos.

Este script nunca toca som nem abre o microfone.

Uso:
    .venv\Scripts\python scripts/sessao_naturalidade.py --fase linha-de-base   # sessao nova (agora)
    .venv\Scripts\python scripts/sessao_naturalidade.py --fase final           # depois das melhorias
    .venv\Scripts\python scripts/sessao_naturalidade.py --continuar            # retoma a ultima
    .venv\Scripts\python scripts/sessao_naturalidade.py --relatorio            # so reescreve o relatorio
    .venv\Scripts\python scripts/sessao_naturalidade.py --autoteste            # logs falsos, sem hardware
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.app import PASTA_LOGS, agora_iso, caminho_do_log, formatar_etapa  # noqa: E402
from jarvis.audio_util import PASTA_EVIDENCIA, caminho_evidencia_de_saida, caminho_para_mostrar  # noqa: E402
from jarvis.config import CAMINHO_CONFIG_PADRAO, ConfigError, carregar_config  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.interprete import INTENCOES_COM_PROJETO  # noqa: E402
from jarvis.ouvido import percentil  # noqa: E402

PASTA_SCRIPTS = Path(__file__).resolve().parent


def _carregar_modulo_irmao(nome: str):
    """Importa `scripts/<nome>.py` por caminho, com a mesma chave de cache dos outros scripts."""
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


#: A leitura das teclas e o "jarvis a correr?" sao os da aceitacao: uma so fonte.
aceitacao = _carregar_modulo_irmao("aceitacao_sponsor")
ENTER = aceitacao.ENTER
tecla_do_teclado = aceitacao.tecla_do_teclado
jarvis_a_correr = aceitacao.jarvis_a_correr

# --- Constantes -------------------------------------------------------------------

GUIAO = RAIZ / "tests" / "voz" / "guiao-naturalidade.md"
PASTA_SESSOES = PASTA_LOGS / "naturalidade"
EVIDENCIA_PENDENTE = PASTA_EVIDENCIA / "naturalidade-pendente.md"

TOTAL_DE_TROCAS = 20
TIPOS = ("local", "conversa", "seguimento", "pesquisa", "estado", "ditado", "aviso", "sem-projeto", "interromper")
#: Minimo de trocas de cada tipo: as metas de latencia precisam de amostras.
MINIMOS_POR_TIPO = {"local": 3, "conversa": 2, "seguimento": 2, "pesquisa": 2}
COLUNAS = ("id", "tipo", "o que dizer", "o que deve acontecer", "pergunta esperada")
PADRAO_ID = re.compile(r"^n-\d{2}$")
PADRAO_MARCADOR = re.compile(r"<[^>]*>")
MARCADORES = ("<projeto-1>", "<projeto-2>")
SIM, NAO = "sim", "não"

FASES = ("linha-de-base", "final")
FASE_BASE, FASE_FINAL = FASES

#: As teclas do fim de cada troca.
NATURAL, POUCO_NATURAL, REPETIU, ATIVACAO_A_MAIS = "1", "2", "3", "4"
MARCAS = {
    NATURAL: "natural",
    POUCO_NATURAL: "pouco natural",
    REPETIU: "tive de repetir",
    ATIVACAO_A_MAIS: 'disse "hey jarvis" sem precisar',
}
NOMES_DAS_MARCAS_NO_RELATORIO = {
    NATURAL: "natural",
    POUCO_NATURAL: "unnatural",
    REPETIU: "had to repeat",
    ATIVACAO_A_MAIS: "wake word without need",
}

META_NATURAIS = 16
META_CEREBRO_P50_MS = 1500.0
META_PESQUISA_P50_MS = 3500.0
META_PESQUISA_P95_MS = 6000.0
META_LOCAL_P50_MS = 1000.0
META_LOCAL_P95_MS = 1600.0
META_GERAL_P50_MS = 3500.0
META_GERAL_P95_MS = 6000.0
META_PRIMEIRO_SOM_MS = 1200.0
META_REPETICOES = 1
META_ATIVACOES_A_MAIS = 0
META_PERGUNTAS_A_MAIS = 1
#: Estrita: p95 abaixo de 300 ms.
META_INTERRUPCAO_P95_MS = 300.0
META_INTERRUPCOES_FALSAS = 0
#: Abaixo disto a sessao e INCOMPLETA: os numeros mostram-se, mas nao decidem nada.
MINIMO_FEITAS = 16

ESTADO_PENDENTE = "PENDING - Sponsor step"
ESTADO_CUMPRIDA = "MET"
ESTADO_NAO_CUMPRIDA = "NOT MET"
ESTADO_INCOMPLETA = "INCOMPLETE"
NAO_MEDIDO = "not measured"

ORIGEM_SPONSOR = "sponsor"
ORIGEM_AUTOTESTE = "autoteste"

FONTE_MICROFONE = "microfone"
FONTE_FICHEIRO = "ficheiro"
FONTE_DESCONHECIDA = "desconhecida"

PASSO_DO_SPONSOR = (
    "1. Open jarvis: `.venv\\Scripts\\python -m jarvis` (or the shortcut) and wait for the `JARVIS PRONTO` line.",
    "2. In a second window, in the jarvis folder: `.venv\\Scripts\\python scripts/sessao_naturalidade.py "
    "--fase linha-de-base` now (about 15 min), and again with `--fase final` after the remaining improvements.",
    "3. Follow the 20 exchanges on screen (guide: `tests/voz/guiao-naturalidade.md`) and press 1, 2, 3 or 4 "
    "after each one; the report is written at the end.",
)

SEPARADOR = "=" * 72

ECRA_INICIAL = (
    "ANTES DE COMEÇAR — lê isto uma vez (1 minuto)",
    "",
    "1. Tem só UM jarvis aberto, com o microfone. Se houver outra janela do jarvis, fecha-a primeiro.",
    '2. Para falar com o jarvis: Shift da direita enquanto falas, ou começa com "hey jarvis",',
    '   salvo quando a troca diz para falar sem ele. Fala como falarias com uma pessoa.',
    "3. Enter para começar cada troca, Enter quando o jarvis acabar, e depois UMA tecla:",
    *(f"     {tecla} = {nome}" for tecla, nome in MARCAS.items()),
    "4. Os pedidos aos projetos só leem. A resposta do projeto à troca n-10 chega sozinha a meio",
    '   da conversa. Na troca sem projeto, diz o nome do projeto e depois "abort": nada é enviado.',
    "5. p salta uma troca; q guarda e sai (--continuar retoma onde ficou).",
)


class ErroDoGuiao(Exception):
    """O guiao nao cumpre o formato; a mensagem diz a linha e o problema."""


class ErroDaSessao(Exception):
    """A sessao nao pode comecar ou nao se consegue ler."""


# --- O guiao -----------------------------------------------------------------------


@dataclass(frozen=True)
class Troca:
    id: str
    tipo: str
    dizer: str
    acontecer: str
    #: O jarvis deve fazer uma pergunta de esclarecimento (so o pedido sem projeto).
    pergunta_esperada: bool


def _celulas(linha: str) -> list[str]:
    return [celula.strip() for celula in linha.strip().strip("|").split("|")]


def ler_guiao(caminho: Path = GUIAO) -> list[Troca]:
    """Le e valida a tabela das trocas. Levanta ErroDoGuiao com a linha em falta."""
    caminho = Path(caminho)
    trocas: list[Troca] = []
    vistos: set[str] = set()
    cabecalho_visto = False
    for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), start=1):
        if not linha.lstrip().startswith("|"):
            continue
        celulas = _celulas(linha)
        if not cabecalho_visto:
            if tuple(celulas) == COLUNAS:
                cabecalho_visto = True
            continue  # outras tabelas do guiao (teclas, metas) ficam de fora
        if all(set(c) <= set(":-") for c in celulas):
            continue
        onde = f"{caminho.name}:{numero}"
        if len(celulas) != len(COLUNAS):
            raise ErroDoGuiao(f"{onde}: {len(celulas)} colunas, esperava {len(COLUNAS)}")
        id_, tipo, dizer, acontecer, pergunta = celulas
        onde = f"{onde} ({id_})"
        if not PADRAO_ID.match(id_):
            raise ErroDoGuiao(f"{onde}: id tem de ser n-NN")
        if id_ in vistos:
            raise ErroDoGuiao(f"{onde}: id repetido")
        vistos.add(id_)
        if tipo not in TIPOS:
            raise ErroDoGuiao(f"{onde}: tipo '{tipo}' fora de {TIPOS}")
        if pergunta not in (SIM, NAO):
            raise ErroDoGuiao(f"{onde}: pergunta esperada tem de ser '{SIM}' ou '{NAO}'")
        vazias = [coluna for coluna, celula in zip(COLUNAS, celulas) if not celula]
        if vazias:
            raise ErroDoGuiao(f"{onde}: celula vazia em {vazias}")
        for texto in (dizer, acontecer):
            for marcador in PADRAO_MARCADOR.findall(texto):
                if marcador not in MARCADORES:
                    raise ErroDoGuiao(f"{onde}: so se aceitam os marcadores {MARCADORES}, nao '{marcador}'")
        trocas.append(Troca(id_, tipo, dizer, acontecer, pergunta == SIM))
    if not cabecalho_visto:
        raise ErroDoGuiao(f"{caminho.name}: tabela das trocas nao encontrada (cabecalho {COLUNAS})")
    return trocas


def verificar_cobertura_do_guiao(trocas: Sequence[Troca]) -> list[str]:
    """O guiao tem as 20 trocas e cada caso que a sessao mede. Vazia = cumpre."""
    problemas: list[str] = []
    if len(trocas) != TOTAL_DE_TROCAS:
        problemas.append(f"{len(trocas)} trocas, esperava {TOTAL_DE_TROCAS}")
    for tipo in TIPOS:
        if not any(t.tipo == tipo for t in trocas):
            problemas.append(f"nenhuma troca do tipo '{tipo}'")
    for tipo, minimo in MINIMOS_POR_TIPO.items():
        n = sum(1 for t in trocas if t.tipo == tipo)
        if n < minimo:
            problemas.append(f"{n} troca(s) '{tipo}', minimo {minimo}")
    com_pergunta = [t.id for t in trocas if t.pergunta_esperada]
    sem_projeto = [t.id for t in trocas if t.tipo == "sem-projeto"]
    if com_pergunta != sem_projeto:
        problemas.append(f"so o pedido sem projeto espera uma pergunta: {com_pergunta} != {sem_projeto}")
    return problemas


def trocar_marcadores(texto: str, projetos: dict[str, str]) -> str:
    for marcador, nome in projetos.items():
        texto = texto.replace(marcador, nome)
    return texto


def nomes_dos_marcadores(nomes: Sequence[str], escolhidos: Sequence[str | None] = ()) -> dict[str, str]:
    """<projeto-1>/<projeto-2> -> projetos que o jarvis conhece (os mais usados, ou os escolhidos)."""
    nomes = list(nomes)
    if not nomes:
        raise ErroDaSessao("o jarvis nao conhece nenhum projeto; o guiao precisa de pelo menos um")
    mapa = {"<projeto-1>": nomes[0], "<projeto-2>": nomes[1] if len(nomes) > 1 else nomes[0]}
    for marcador, nome in zip(MARCADORES, escolhidos):
        if nome is None:
            continue
        if nome not in nomes:
            raise ErroDaSessao(f"'{nome}' nao e um projeto que o jarvis conheca")
        mapa[marcador] = nome
    return mapa


# --- A sessao ----------------------------------------------------------------------

FEITA = "feita"
SALTADA = "saltada"
FORMATO_INSTANTE = "%Y-%m-%d %H:%M:%S.%f"


def ler_instante(texto: str) -> datetime.datetime:
    return datetime.datetime.strptime(texto, FORMATO_INSTANTE)


@dataclass
class RegistoDaTroca:
    id: str
    estado: str
    inicio: str | None = None
    fim: str | None = None
    #: A tecla do Sponsor (`MARCAS`).
    marca: str | None = None
    motivo: str = ""


@dataclass
class Sessao:
    fase: str
    #: Nomes reais dos projetos: so em logs/ (ignorada), nunca no relatorio.
    projetos: dict
    origem: str = ORIGEM_SPONSOR
    criada: str = ""
    concluida: bool = False
    trocas: list = field(default_factory=list)

    def registo(self, id_: str) -> RegistoDaTroca | None:
        return next((r for r in self.trocas if r.id == id_), None)

    def para_json(self) -> dict:
        return {
            "versao": 1,
            "origem": self.origem,
            "fase": self.fase,
            "criada": self.criada,
            "concluida": self.concluida,
            "projetos": self.projetos,
            "trocas": [vars(r) for r in self.trocas],
        }

    @classmethod
    def de_json(cls, dados: dict) -> "Sessao":
        try:
            return cls(
                fase=dados["fase"],
                projetos=dict(dados["projetos"]),
                origem=dados.get("origem", ""),
                criada=dados.get("criada", ""),
                concluida=bool(dados.get("concluida")),
                trocas=[RegistoDaTroca(**r) for r in dados.get("trocas", [])],
            )
        except (KeyError, TypeError) as erro:
            raise ErroDaSessao(f"sessao ilegivel: {erro!r}") from erro


def guardar_sessao(sessao: Sessao, caminho: Path) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(sessao.para_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(caminho)


def ler_sessao(caminho: Path) -> Sessao:
    try:
        return Sessao.de_json(json.loads(Path(caminho).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as erro:
        raise ErroDaSessao(f"{Path(caminho).name}: {erro}") from erro


def sessoes_gravadas(pasta: Path = PASTA_SESSOES) -> list[Path]:
    return sorted(Path(pasta).glob("sessao-*.json")) if Path(pasta).is_dir() else []


def ultima_sessao(pasta: Path = PASTA_SESSOES, fase: str | None = None) -> Path | None:
    """A sessao mais recente (da fase dada, se houver); as ilegiveis ficam de fora na procura por fase."""
    for caminho in reversed(sessoes_gravadas(pasta)):
        if fase is None:
            return caminho
        try:
            if ler_sessao(caminho).fase == fase:
                return caminho
        except ErroDaSessao:
            continue
    return None


# --- O log do jarvis ---------------------------------------------------------------

_LINHA = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (.*)$")
_ARRANQUE = re.compile(r"^jarvis a arrancar")
_TERMINADO = re.compile(r"^jarvis terminado")
_PRONTO = "JARVIS PRONTO em"
_OUVIDO = re.compile(r"^\s+ouvido: (.*)$")
_FRASE = re.compile(r"^frase #(\d+) \| (.*)$")
_ETAPA_3 = re.compile(r"^etapa 3/5 interprete\s*\|\s*-?\d+ ms \| (.*)$")
_INTENCAO = re.compile(r"intencao=(\S+) projeto=(\S+)")
_ULTIMA_VOZ = re.compile(r"^ultima voz: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \|")
_FALA_DESDE_A_VOZ = re.compile(r"^resposta falada desde a ultima voz: (-?\d+) ms")
_DESFECHO = re.compile(r"^desfecho: (\S+) \| ?(.*)$")
_RESPOSTA_GERAL = re.compile(r"^pergunta \| resposta falada: (-?\d+) ms desde a ultima voz da frase #(\d+)")
_RESPOSTA_DO_CEREBRO = re.compile(r"^cerebro \| resposta falada: (-?\d+) ms desde a ultima voz da frase #(\d+)")
#: O resumo de um turno do cerebro; o grupo 3 e o resto da linha (pesquisa, tokens e o texto dito).
_RESUMO_DO_CEREBRO = re.compile(r"^cerebro \| (\w+)(?: em [\d.]+ s)? \((.*?)\) \| intencao=cerebro(.*)$")
_PESQUISA_WEB = re.compile(r"^ \| pesquisa web: (sim|nao)\b")
_TOKENS = re.compile(r"\| tokens: ([^|]*)")
_CAMPO_DOS_TOKENS = re.compile(r"(input|cache_read|cache_creation|output)=(\d+)")
INTENCAO_DO_CEREBRO = "cerebro"
_INTERRUPCAO_PARADA = re.compile(r"^interrupcao \| voz parada: (-?\d+) ms desde o inicio da fala")
_INTERRUPCAO_FALSA = re.compile(r"^interrupcao \| falsa\b")


@dataclass
class FraseDoLog:
    segmento: int
    numero: int
    #: A primeira linha da frase (escrita depois de a fala acabar).
    instante: datetime.datetime
    fonte: str
    intencao: str | None = None
    projeto: str | None = None
    resposta_ao_recap: bool = False
    #: A frase continuou uma pergunta geral cuja resposta acabou numa pergunta.
    continuacao: bool = False
    desfecho: str | None = None
    motivo: str = ""
    #: O ultimo chunk com voz: quando o Sponsor acabou mesmo de falar.
    ultima_voz: datetime.datetime | None = None
    #: Ultima voz -> primeiro audio da primeira coisa dita nesta frase.
    fala_ms: float | None = None
    #: Ultima voz -> primeiro audio da resposta a pergunta geral.
    resposta_geral_ms: float | None = None
    #: Ultima voz -> primeiro audio da primeira frase da resposta do cerebro.
    resposta_cerebro_ms: float | None = None
    #: Desfecho do turno do cerebro (linha de resumo); None enquanto nao chegou.
    estado_do_cerebro: str | None = None
    #: O cerebro usou a web neste turno; None sem a linha de resumo.
    pesquisa_web: bool | None = None
    #: Tokens do turno do cerebro (input, cache_read, cache_creation, output).
    tokens: dict = field(default_factory=dict)

    @property
    def quando(self) -> datetime.datetime:
        """Quando foi dita: a ultima voz, ou (sem ela) a primeira linha."""
        return self.ultima_voz or self.instante

    @property
    def do_cerebro(self) -> bool:
        """A frase foi ao cerebro de conversa (fora do caminho rapido)."""
        return self.intencao == INTENCAO_DO_CEREBRO

    @property
    def geral(self) -> bool:
        """Pergunta geral pelo interprete local (o caminho sem cerebro)."""
        return not self.do_cerebro and (self.intencao == "pergunta_geral" or self.continuacao)

    @property
    def entrada_do_cerebro(self) -> int | None:
        """Tokens de entrada do turno: input, lidos da cache e escritos na cache."""
        partes = [self.tokens.get(nome) for nome in ("input", "cache_read", "cache_creation")]
        if all(parte is None for parte in partes):
            return None
        return sum(parte or 0 for parte in partes)

    @property
    def caminho(self) -> str:
        """Por onde a frase foi tratada, para o relatorio."""
        if self.do_cerebro:
            if self.pesquisa_web is None:
                return "brain (no summary line)"
            return "brain + web search" if self.pesquisa_web else "brain"
        if self.geral:
            return "general question"
        return "local"

    @property
    def perguntas(self) -> int:
        """Perguntas de esclarecimento que o jarvis fez nesta frase (0 ou 1)."""
        if self.desfecho == "nao_percebido":
            return 1
        if self.desfecho == "pendente" and self.motivo.startswith("resposta nao percebida"):
            return 1
        sem_projeto = self.intencao in INTENCOES_COM_PROJETO and self.projeto is None
        if sem_projeto and not self.resposta_ao_recap and self.desfecho == "pendente":
            return 1  # "Which project?"
        return 0


@dataclass(frozen=True)
class InterrupcaoDoLog:
    instante: datetime.datetime
    fonte: str
    #: Inicio da fala -> voz parada; None numa interrupcao falsa.
    latencia_ms: float | None
    falsa: bool = False


@dataclass
class LeituraDoLog:
    frases: list = field(default_factory=list)
    interrupcoes: list = field(default_factory=list)


class _LeitorDoLog:
    """Le os logs de um ou mais processos do jarvis, um de cada vez no ficheiro.

    Cada processo (`jarvis a arrancar`) e um segmento com as suas frases
    numeradas a partir de 1; a linha `ouvido:` do cabecalho diz se ouviu o
    microfone ou ficheiros WAV.
    """

    def __init__(self, primeiro_segmento: int = 1) -> None:
        self.frases: dict[tuple[int, int], FraseDoLog] = {}
        self.interrupcoes: list[InterrupcaoDoLog] = []
        self.fontes: dict[int, str] = {}
        self._segmento = primeiro_segmento - 1
        self._a_espera_do_cabecalho = False
        #: Frases do cerebro deste processo cujo resumo ainda nao chegou, pela ordem.
        self._turnos_abertos: list[FraseDoLog] = []

    def _fonte(self) -> str:
        return self.fontes.get(self._segmento, FONTE_DESCONHECIDA)

    def ler(self, linhas: Iterable[str]) -> None:
        for crua in linhas:
            linha = crua.rstrip("\r\n")
            if _PRONTO in linha:
                self._a_espera_do_cabecalho = True
                continue
            cabecalho = _OUVIDO.match(linha)
            if cabecalho and not _LINHA.match(linha):
                if self._a_espera_do_cabecalho:
                    ficheiro = "ficheiro" in cabecalho.group(1).lower()
                    self.fontes[self._segmento] = FONTE_FICHEIRO if ficheiro else FONTE_MICROFONE
                    self._a_espera_do_cabecalho = False
                continue
            casamento = _LINHA.match(linha)
            if not casamento:
                continue
            try:
                instante = ler_instante(casamento.group(1) + "000")
            except ValueError:
                continue
            self._ler_linha(instante, casamento.group(2))

    def _frase(self, numero: int, instante: datetime.datetime) -> FraseDoLog:
        chave = (self._segmento, numero)
        frase = self.frases.get(chave)
        if frase is None:
            frase = self.frases[chave] = FraseDoLog(self._segmento, numero, instante, self._fonte())
        return frase

    def _ler_linha(self, instante: datetime.datetime, texto: str) -> None:
        if _ARRANQUE.match(texto):
            self._segmento += 1
            self._a_espera_do_cabecalho = False
            self._turnos_abertos = []
            return
        if _TERMINADO.match(texto):
            return
        resposta = _RESPOSTA_GERAL.match(texto)
        if resposta:
            frase = self._frase(int(resposta.group(2)), instante)
            if frase.resposta_geral_ms is None:
                frase.resposta_geral_ms = float(resposta.group(1))
            return
        do_cerebro = _RESPOSTA_DO_CEREBRO.match(texto)
        if do_cerebro:
            frase = self._frase(int(do_cerebro.group(2)), instante)
            if frase.resposta_cerebro_ms is None:
                frase.resposta_cerebro_ms = float(do_cerebro.group(1))
            return
        resumo = _RESUMO_DO_CEREBRO.match(texto)
        if resumo:
            self._fechar_turno(resumo.group(1), resumo.group(3))
            return
        parada = _INTERRUPCAO_PARADA.match(texto)
        if parada:
            self.interrupcoes.append(InterrupcaoDoLog(instante, self._fonte(), float(parada.group(1))))
            return
        if _INTERRUPCAO_FALSA.match(texto):
            self.interrupcoes.append(InterrupcaoDoLog(instante, self._fonte(), None, falsa=True))
            return
        casamento = _FRASE.match(texto)
        if not casamento:
            return
        frase = self._frase(int(casamento.group(1)), instante)
        resto = casamento.group(2)
        etapa = _ETAPA_3.match(resto)
        if etapa:
            detalhe = etapa.group(1)
            frase.resposta_ao_recap = detalhe.startswith("resposta ao recap")
            frase.continuacao = detalhe.startswith("continuacao da pergunta geral (")
            intencao = _INTENCAO.search(detalhe)
            if intencao:
                frase.intencao = intencao.group(1)
                frase.projeto = None if intencao.group(2) == "-" else intencao.group(2)
                if frase.do_cerebro and all(aberta is not frase for aberta in self._turnos_abertos):
                    self._turnos_abertos.append(frase)
            return
        voz = _ULTIMA_VOZ.match(resto)
        if voz:
            try:
                frase.ultima_voz = ler_instante(voz.group(1) + "000")
            except ValueError:
                pass
            return
        fala = _FALA_DESDE_A_VOZ.match(resto)
        if fala:
            if frase.fala_ms is None:
                frase.fala_ms = float(fala.group(1))
            return
        desfecho = _DESFECHO.match(resto)
        if desfecho:
            frase.desfecho, frase.motivo = desfecho.group(1), desfecho.group(2)

    def _fechar_turno(self, estado: str, resto: str) -> None:
        """O resumo de um turno do cerebro fecha o turno mais antigo ainda aberto deste processo."""
        if not self._turnos_abertos:
            return
        frase = self._turnos_abertos.pop(0)
        frase.estado_do_cerebro = estado
        web = _PESQUISA_WEB.match(resto)
        # Sem a parte da pesquisa o turno nem chegou a correr: nao usou a web.
        frase.pesquisa_web = web is not None and web.group(1) == "sim"
        tokens = _TOKENS.search(resto)
        if tokens:
            frase.tokens = {nome: int(valor) for nome, valor in _CAMPO_DOS_TOKENS.findall(tokens.group(1))}

    def resultado(self) -> LeituraDoLog:
        frases = list(self.frases.values())
        for frase in frases:
            # O cabecalho chega depois das primeiras linhas do processo.
            frase.fonte = self.fontes.get(frase.segmento, FONTE_DESCONHECIDA)
        return LeituraDoLog(frases, list(self.interrupcoes))


def ler_log(linhas: Iterable[str], primeiro_segmento: int = 1) -> LeituraDoLog:
    leitor = _LeitorDoLog(primeiro_segmento)
    leitor.ler(linhas)
    return leitor.resultado()


def ler_logs(caminhos: Sequence[Path]) -> LeituraDoLog:
    leitura = LeituraDoLog()
    segmento = 1
    for caminho in caminhos:
        with Path(caminho).open(encoding="utf-8", errors="replace") as ficheiro:
            parte = ler_log(ficheiro.read().splitlines(), primeiro_segmento=segmento)
        leitura.frases.extend(parte.frases)
        leitura.interrupcoes.extend(parte.interrupcoes)
        segmento = max([segmento, *(f.segmento + 1 for f in parte.frases)]) + 1
    leitura.frases.sort(key=lambda f: (f.quando, f.segmento, f.numero))
    return leitura


def logs_da_sessao(sessao: Sessao, pasta: Path = PASTA_LOGS) -> list[Path]:
    """Os logs diarios que podem ter as janelas da sessao (um processo escreve no dia em que arrancou)."""
    instantes = [ler_instante(r.inicio) for r in sessao.trocas if r.inicio] + [
        ler_instante(r.fim) for r in sessao.trocas if r.fim
    ]
    if not instantes:
        return []
    dia = min(instantes).date() - datetime.timedelta(days=1)
    caminhos = []
    while dia <= max(instantes).date():
        caminho = caminho_do_log(datetime.datetime.combine(dia, datetime.time()), Path(pasta))
        if caminho.is_file():
            caminhos.append(caminho)
        dia += datetime.timedelta(days=1)
    return caminhos


# --- A avaliacao -------------------------------------------------------------------

#: O Sponsor pode comecar a falar um pouco antes do Enter de inicio.
ANTECIPACAO = datetime.timedelta(seconds=3)
#: Depois do Enter final ainda pode chegar fala da troca (a resposta a um recap),
#: ate a troca seguinte comecar, no maximo isto.
GRACA_FINAL = datetime.timedelta(seconds=60)


@dataclass
class _Janela:
    registo: RegistoDaTroca
    abre: datetime.datetime
    fecha: datetime.datetime


def janelas(registos: Sequence[RegistoDaTroca]) -> list[_Janela]:
    """As janelas das trocas feitas, por ordem e sem se sobreporem."""
    feitas = sorted(
        (r for r in registos if r.estado == FEITA and r.inicio and r.fim), key=lambda r: ler_instante(r.inicio)
    )
    resultado: list[_Janela] = []
    fim_anterior: datetime.datetime | None = None
    for registo in feitas:
        inicio, fim = ler_instante(registo.inicio), ler_instante(registo.fim)
        abre = inicio - ANTECIPACAO
        if fim_anterior is not None:
            abre = min(max(abre, fim_anterior), inicio)
        resultado.append(_Janela(registo, abre, fim + GRACA_FINAL))
        fim_anterior = fim
    for janela, seguinte in zip(resultado, resultado[1:]):
        janela.fecha = max(min(janela.fecha, seguinte.abre), ler_instante(janela.registo.fim))
    return resultado


@dataclass
class ResultadoDaTroca:
    troca: Troca
    registo: RegistoDaTroca
    frases: list = field(default_factory=list)
    interrupcoes: list = field(default_factory=list)
    excluidas: int = 0

    @property
    def perguntas_a_mais(self) -> int:
        feitas = sum(f.perguntas for f in self.frases)
        return max(0, feitas - 1) if self.troca.pergunta_esperada else feitas


@dataclass
class Medida:
    """Uma meta do relatorio: o valor obtido (texto) e se cumpre (None = nao medida)."""

    nome: str
    obtido: str
    meta: str
    cumpre: bool | None


@dataclass
class Avaliacao:
    estado: str
    sessao: Sessao | None
    resultados: list = field(default_factory=list)
    saltadas: list = field(default_factory=list)
    medidas: list = field(default_factory=list)
    local_ms: list = field(default_factory=list)
    #: Respostas do cerebro, da ultima voz a primeira frase falada: sem e com pesquisa na web.
    cerebro_ms: list = field(default_factory=list)
    pesquisa_ms: list = field(default_factory=list)
    #: Tokens de entrada de cada turno do cerebro (input + cache lida + cache escrita).
    entrada_do_cerebro: list = field(default_factory=list)
    geral_ms: list = field(default_factory=list)
    primeiro_som_ms: list = field(default_factory=list)
    interrupcao_ms: list = field(default_factory=list)
    interrupcoes_falsas: int = 0
    naturais: int = 0
    repeticoes: int = 0
    ativacoes_a_mais: int = 0
    perguntas_a_mais: int = 0
    pendencias: list = field(default_factory=list)
    notas: list = field(default_factory=list)

    @property
    def falhas(self) -> list[str]:
        return [f"{m.nome}: {m.obtido} (target {m.meta})" for m in self.medidas if m.cumpre is False]

    @property
    def nao_medidas(self) -> list[str]:
        return [m.nome for m in self.medidas if m.cumpre is None]


def atribuir(registos: Sequence[RegistoDaTroca], leitura: LeituraDoLog) -> dict[str, tuple[list, list]]:
    """As frases e as interrupcoes de cada troca feita, pela hora em que foram ditas."""
    por_troca: dict[str, tuple[list, list]] = {}
    lista = janelas(registos)
    for janela in lista:
        por_troca[janela.registo.id] = ([], [])
    for frase in leitura.frases:
        janela = next((j for j in lista if j.abre <= frase.quando < j.fecha), None)
        if janela is not None:
            por_troca[janela.registo.id][0].append(frase)
    for interrupcao in leitura.interrupcoes:
        janela = next((j for j in lista if j.abre <= interrupcao.instante < j.fecha), None)
        if janela is not None:
            por_troca[janela.registo.id][1].append(interrupcao)
    return por_troca


def _p(valores: Sequence[float], p: float) -> float:
    return percentil(list(valores), p)


def _estatistica(valores: Sequence[float]) -> str:
    if not valores:
        return "no samples"
    return f"p50 {_p(valores, 50):.0f} ms · p95 {_p(valores, 95):.0f} ms · max {max(valores):.0f} ms · n={len(valores)}"


def _medida_de_latencia(
    nome: str,
    valores: Sequence[float],
    limite_p50: float,
    limite_p95: float | None = None,
    *,
    sem_amostras: bool | None = False,
) -> Medida:
    """Uma meta de latencia; sem amostras vale `sem_amostras` (False falha, None nao medida)."""
    meta = f"p50 <= {limite_p50:.0f} ms" + (f", p95 <= {limite_p95:.0f} ms" if limite_p95 is not None else "")
    if not valores:
        return Medida(nome, "no samples" if sem_amostras is not None else NAO_MEDIDO, meta, sem_amostras)
    ok = _p(valores, 50) <= limite_p50 and (limite_p95 is None or _p(valores, 95) <= limite_p95)
    return Medida(nome, _estatistica(valores), meta, ok)


NOME_CEREBRO = "brain reply without web search, last voice -> first spoken sentence"
NOME_PESQUISA = "brain reply with web search, last voice -> first spoken sentence"
NOME_LOCAL = "local reply, last voice -> first sound"


def avaliar_sessao(sessao: Sessao | None, trocas: Sequence[Troca], leitura: LeituraDoLog) -> Avaliacao:
    """Mede a sessao contra as metas. Sem sessao real do Sponsor: PENDING."""
    if sessao is None:
        return Avaliacao(ESTADO_PENDENTE, None, pendencias=["no Sponsor session recorded yet"])
    if sessao.origem != ORIGEM_SPONSOR:
        return Avaliacao(
            ESTADO_PENDENTE, sessao, pendencias=[f"the session is not the Sponsor's (origin '{sessao.origem}'); it never counts"]
        )
    avaliacao = Avaliacao(ESTADO_NAO_CUMPRIDA, sessao)
    atribuidas = atribuir(sessao.trocas, leitura)
    for troca in trocas:
        registo = sessao.registo(troca.id)
        if registo is None or registo.id not in atribuidas:
            avaliacao.saltadas.append((troca.id, registo.motivo if registo else "not done"))
            continue
        frases, interrupcoes = atribuidas[registo.id]
        validas = [f for f in frases if f.fonte == FONTE_MICROFONE]
        resultado = ResultadoDaTroca(
            troca,
            registo,
            validas,
            [i for i in interrupcoes if i.fonte == FONTE_MICROFONE],
            excluidas=len(frases) - len(validas),
        )
        avaliacao.resultados.append(resultado)

    resultados = avaliacao.resultados
    excluidas = sum(r.excluidas for r in resultados)
    if excluidas:
        avaliacao.notas.append(
            f"{excluidas} sentence(s) in the windows came from WAV files or a process without a header: they do not count"
        )
    ouvidas = [f for r in resultados for f in r.frases]
    if not ouvidas:
        avaliacao.estado = ESTADO_PENDENTE
        avaliacao.pendencias.append(
            "the jarvis log has no microphone sentence in the session windows "
            "(was jarvis open, with the microphone, during the session?)"
        )
        return avaliacao
    if not any(f.ultima_voz is not None for f in ouvidas):
        avaliacao.notas.append(
            "no 'ultima voz' line in the log: this jarvis does not log the last voiced chunk yet, "
            "so no latency is measured"
        )

    marcas = [r.registo.marca for r in resultados]
    avaliacao.naturais = marcas.count(NATURAL)
    avaliacao.repeticoes = marcas.count(REPETIU)
    avaliacao.ativacoes_a_mais = marcas.count(ATIVACAO_A_MAIS)
    avaliacao.perguntas_a_mais = sum(r.perguntas_a_mais for r in resultados)
    sem_resumo = 0
    for frase in ouvidas:
        if frase.do_cerebro:
            if frase.entrada_do_cerebro is not None:
                avaliacao.entrada_do_cerebro.append(frase.entrada_do_cerebro)
            if frase.resposta_cerebro_ms is None:
                continue
            if frase.pesquisa_web is None:
                sem_resumo += 1
            elif frase.pesquisa_web:
                avaliacao.pesquisa_ms.append(frase.resposta_cerebro_ms)
            else:
                avaliacao.cerebro_ms.append(frase.resposta_cerebro_ms)
        elif frase.geral:
            if frase.resposta_geral_ms is not None:
                avaliacao.geral_ms.append(frase.resposta_geral_ms)
            sons = [ms for ms in (frase.fala_ms, frase.resposta_geral_ms) if ms is not None]
            if sons:
                avaliacao.primeiro_som_ms.append(min(sons))
        elif frase.fala_ms is not None:
            avaliacao.local_ms.append(frase.fala_ms)
    interrupcoes = [i for r in resultados for i in r.interrupcoes]
    avaliacao.interrupcao_ms = [i.latencia_ms for i in interrupcoes if i.latencia_ms is not None]
    avaliacao.interrupcoes_falsas = sum(1 for i in interrupcoes if i.falsa)
    if not any(f.do_cerebro for f in ouvidas):
        avaliacao.notas.append("no sentence went to the brain: the conversation ran on the local interpreter")
    elif not avaliacao.pesquisa_ms:
        avaliacao.notas.append("no spoken brain reply used a web search: the web search target is not measured")
    if sem_resumo:
        avaliacao.notas.append(
            f"{sem_resumo} brain reply(ies) without the turn summary line: web search unknown, left out of both brain targets"
        )
    entradas = avaliacao.entrada_do_cerebro
    if entradas:
        avaliacao.notas.append(
            "brain input tokens per exchange (input + cache read + cache creation): "
            f"p50 {_p(entradas, 50):.0f} · max {max(entradas)} · n={len(entradas)}"
        )

    medidas = avaliacao.medidas
    medidas.append(
        Medida(
            "exchanges marked natural",
            f"{avaliacao.naturais} of {TOTAL_DE_TROCAS}",
            f">= {META_NATURAIS} of {TOTAL_DE_TROCAS}",
            avaliacao.naturais >= META_NATURAIS,
        )
    )
    medidas.append(_medida_de_latencia(NOME_CEREBRO, avaliacao.cerebro_ms, META_CEREBRO_P50_MS))
    medidas.append(
        _medida_de_latencia(
            NOME_PESQUISA, avaliacao.pesquisa_ms, META_PESQUISA_P50_MS, META_PESQUISA_P95_MS, sem_amostras=None
        )
    )
    medidas.append(_medida_de_latencia(NOME_LOCAL, avaliacao.local_ms, META_LOCAL_P50_MS, META_LOCAL_P95_MS))
    if any(f.geral for f in ouvidas):
        # So o recurso sem cerebro (o interprete local) faz perguntas gerais.
        medidas.append(
            _medida_de_latencia(
                "general answer, last voice -> first answer sentence",
                avaliacao.geral_ms,
                META_GERAL_P50_MS,
                META_GERAL_P95_MS,
            )
        )
        som = avaliacao.primeiro_som_ms
        medidas.append(
            Medida(
                "general answer, last voice -> first sound of any kind",
                _estatistica(som),
                f"max <= {META_PRIMEIRO_SOM_MS:.0f} ms",
                bool(som) and max(som) <= META_PRIMEIRO_SOM_MS,
            )
        )
    for nome, valor, meta in (
        ("times the Sponsor had to repeat", avaliacao.repeticoes, META_REPETICOES),
        ("wake words said without needing them", avaliacao.ativacoes_a_mais, META_ATIVACOES_A_MAIS),
        ("unnecessary questions from jarvis", avaliacao.perguntas_a_mais, META_PERGUNTAS_A_MAIS),
    ):
        medidas.append(Medida(nome, str(valor), f"<= {meta}", valor <= meta))
    if interrupcoes:
        latencias = avaliacao.interrupcao_ms
        medidas.append(
            Medida(
                "interruption, speech onset -> voice stopped",
                _estatistica(latencias),
                f"p95 < {META_INTERRUPCAO_P95_MS:.0f} ms",
                bool(latencias) and _p(latencias, 95) < META_INTERRUPCAO_P95_MS,
            )
        )
        medidas.append(
            Medida(
                "false interruptions",
                str(avaliacao.interrupcoes_falsas),
                str(META_INTERRUPCOES_FALSAS),
                avaliacao.interrupcoes_falsas <= META_INTERRUPCOES_FALSAS,
            )
        )
    else:
        medidas.append(Medida("interruption, speech onset -> voice stopped", NAO_MEDIDO, f"p95 < {META_INTERRUPCAO_P95_MS:.0f} ms", None))
        medidas.append(Medida("false interruptions", NAO_MEDIDO, str(META_INTERRUPCOES_FALSAS), None))

    if len(resultados) < MINIMO_FEITAS:
        avaliacao.estado = ESTADO_INCOMPLETA
        avaliacao.pendencias.append(
            f"only {len(resultados)} of {len(trocas)} exchanges done (minimum {MINIMO_FEITAS}); "
            "continue with `.venv\\Scripts\\python scripts/sessao_naturalidade.py --continuar`"
        )
    else:
        avaliacao.estado = ESTADO_NAO_CUMPRIDA if avaliacao.falhas else ESTADO_CUMPRIDA
    return avaliacao


# --- O relatorio -------------------------------------------------------------------


def _cumpre(valor: bool | None) -> str:
    if valor is None:
        return NAO_MEDIDO
    return "yes" if valor else "**no**"


def texto_do_relatorio(
    avaliacao: Avaliacao, base: Avaliacao | None = None, quando: datetime.datetime | None = None
) -> str:
    """O relatorio em markdown: so numeros e ids do guiao (nem transcricoes nem projetos)."""
    quando = quando or datetime.datetime.now()
    sessao = avaliacao.sessao
    fase = sessao.fase if sessao is not None else "-"
    linhas = [
        f"# Naturalness session with the Sponsor's real voice — {quando:%Y-%m-%d %H:%M}",
        "",
        f"**State: {avaliacao.estado}**",
        "",
    ]
    if avaliacao.estado == ESTADO_PENDENTE:
        linhas += [f"## {ESTADO_PENDENTE}", "", *[f"- {p}" for p in avaliacao.pendencias], ""]
        linhas += ["Exact step:", "", *PASSO_DO_SPONSOR, ""]
        linhas += [
            "Nothing was simulated: without the session with the Sponsor's real voice no target is declared met. "
            "Synthetic voices and WAV files never count.",
            "",
        ]
        return "\n".join(linhas)
    assert sessao is not None
    if avaliacao.pendencias:
        linhas += ["## Pending", "", *[f"- {p}" for p in avaliacao.pendencias], ""]
    feitas = len(avaliacao.resultados)
    linhas += [
        "## Conditions",
        "",
        f"- Phase: {fase}; session created {sessao.criada or '-'}; guide `tests/voz/guiao-naturalidade.md`.",
        f"- Exchanges done: {feitas} of {TOTAL_DE_TROCAS}; skipped or not done: "
        + (", ".join(f"{i} ({m})" for i, m in avaliacao.saltadas) if avaliacao.saltadas else "none")
        + ".",
        "- Only microphone sentences count (real voice); log segments fed from WAV files are left out.",
        "- Latencies are measured from the last voiced audio chunk (true end of speech), not from the VAD close.",
        "- A brain reply counts as 'with web search' when its turn summary line in the log shows a web use.",
        *[f"- Note: {nota}" for nota in avaliacao.notas],
        "",
        "## Targets",
        "",
    ]
    if base is not None and base.sessao is not None:
        por_nome = {m.nome: m for m in base.medidas}
        linhas += ["| measure | baseline | now | target | met |", "|---|---|---|---|---|"]
        for medida in avaliacao.medidas:
            anterior = por_nome.get(medida.nome)
            linhas.append(
                f"| {medida.nome} | {anterior.obtido if anterior else '-'} | {medida.obtido} | {medida.meta} "
                f"| {_cumpre(medida.cumpre)} |"
            )
    else:
        linhas += ["| measure | obtained | target | met |", "|---|---|---|---|"]
        for medida in avaliacao.medidas:
            linhas.append(f"| {medida.nome} | {medida.obtido} | {medida.meta} | {_cumpre(medida.cumpre)} |")
    linhas += [
        "",
        "## Exchange by exchange",
        "",
        "| id | type | Sponsor's key | sentences heard | unnecessary questions | route | local reply "
        "| brain reply | general answer / first sound |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in avaliacao.resultados:
        caminhos = list(dict.fromkeys(f.caminho for f in r.frases))
        locais = [f.fala_ms for f in r.frases if not f.geral and not f.do_cerebro and f.fala_ms is not None]
        do_cerebro = [f.resposta_cerebro_ms for f in r.frases if f.do_cerebro and f.resposta_cerebro_ms is not None]
        cerebro = f"{do_cerebro[0]:.0f} ms" if do_cerebro else "-"
        gerais = [f for f in r.frases if f.geral]
        local = f"{locais[0]:.0f} ms" if locais else "-"
        if gerais:
            g = gerais[0]
            resposta = f"{g.resposta_geral_ms:.0f} ms" if g.resposta_geral_ms is not None else "-"
            # A primeira coisa que soou: o aviso curto, ou a propria resposta quando chega depressa.
            sons = [ms for ms in (g.fala_ms, g.resposta_geral_ms) if ms is not None]
            som = f"{min(sons):.0f} ms" if sons else "-"
            geral = f"{resposta} / {som}"
        else:
            geral = "-"
        marca = NOMES_DAS_MARCAS_NO_RELATORIO.get(r.registo.marca or "", "-")
        linhas.append(
            f"| {r.troca.id} | {r.troca.tipo} | {marca} | {len(r.frases)} | {r.perguntas_a_mais} "
            f"| {', '.join(caminhos) or '-'} | {local} | {cerebro} | {geral} |"
        )
    linhas += ["", f"Result: **{avaliacao.estado}**", *[f"- {falha}" for falha in avaliacao.falhas]]
    if avaliacao.nao_medidas:
        linhas.append(f"- {NAO_MEDIDO}: " + "; ".join(avaliacao.nao_medidas))
    linhas.append("")
    return "\n".join(linhas)


def escrever_relatorio(avaliacao: Avaliacao, caminho: Path, base: Avaliacao | None = None) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto_do_relatorio(avaliacao, base), encoding="utf-8")
    return caminho


def caminho_do_relatorio(valor: str | None, sessao: Sessao | None) -> Path:
    if valor:
        return caminho_evidencia_de_saida(valor)
    if sessao is None:
        return caminho_evidencia_de_saida(EVIDENCIA_PENDENTE)
    return caminho_evidencia_de_saida(
        PASTA_EVIDENCIA / f"naturalidade-{sessao.fase}-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
    )


def avaliar_ficheiro(caminho: Path | None, trocas: Sequence[Troca], pasta_logs: Path = PASTA_LOGS) -> Avaliacao:
    sessao = ler_sessao(caminho) if caminho is not None else None
    leitura = ler_logs(logs_da_sessao(sessao, pasta_logs)) if sessao is not None else LeituraDoLog()
    return avaliar_sessao(sessao, trocas, leitura)


def relatorio(
    caminho: Path | None,
    trocas: Sequence[Troca],
    evidencia: str | None = None,
    *,
    pasta_sessoes: Path = PASTA_SESSOES,
    pasta_logs: Path = PASTA_LOGS,
) -> tuple[Avaliacao, Path]:
    """Avalia a sessao e escreve o relatorio; a sessao final compara com a ultima linha de base."""
    avaliacao = avaliar_ficheiro(caminho, trocas, pasta_logs)
    base = None
    if avaliacao.sessao is not None and avaliacao.sessao.fase == FASE_FINAL:
        caminho_base = ultima_sessao(pasta_sessoes, FASE_BASE)
        if caminho_base is not None:
            base = avaliar_ficheiro(caminho_base, trocas, pasta_logs)
    destino = caminho_do_relatorio(evidencia, avaliacao.sessao)
    escrever_relatorio(avaliacao, destino, base)
    return avaliacao, destino


# --- A sessao guiada ---------------------------------------------------------------


def linhas_da_troca(troca: Troca, projetos: dict[str, str]) -> list[str]:
    return [
        f"   {SEPARADOR}",
        f"   O QUE DIZER:           \"{trocar_marcadores(troca.dizer, projetos)}\"",
        f"   O QUE DEVE ACONTECER:  {trocar_marcadores(troca.acontecer, projetos)}",
        f"   {SEPARADOR}",
    ]


def correr_sessao(
    sessao: Sessao,
    trocas: Sequence[Troca],
    caminho: Path,
    *,
    tecla: Callable[[str, Sequence[str]], str] = tecla_do_teclado,
    escrever: Callable[[str], object] = print,
    agora: Callable[[], datetime.datetime] = datetime.datetime.now,
    frases_na_janela: Callable[[str, str], int] | None = None,
) -> bool:
    """Guia as trocas por fazer. Guarda depois de cada uma. True quando nao falta nenhuma."""
    por_fazer = [t for t in trocas if sessao.registo(t.id) is None]
    feitas_antes = len(trocas) - len(por_fazer)
    teclas_da_marca = tuple(MARCAS)
    for indice, troca in enumerate(por_fazer, start=feitas_antes + 1):
        escrever("")
        escrever(f"[{indice}/{len(trocas)}] {troca.id} — {troca.tipo}")
        for linha in linhas_da_troca(troca, sessao.projetos):
            escrever(linha)
        while True:
            escolha = tecla("   Enter para começar esta troca (p salta, q guarda e sai):", (ENTER, "p", "q"))
            if escolha == "q":
                guardar_sessao(sessao, caminho)
                return False
            if escolha == "p":
                sessao.trocas.append(RegistoDaTroca(troca.id, SALTADA, motivo="skipped by the Sponsor"))
                guardar_sessao(sessao, caminho)
                break
            inicio = agora_iso(agora())
            escrever("   Agora fala com o jarvis e diz a frase de O QUE DIZER.")
            escolha = tecla("   Enter quando o jarvis acabar (q sai sem contar esta troca):", (ENTER, "q"))
            if escolha == "q":
                guardar_sessao(sessao, caminho)
                return False
            fim = agora_iso(agora())
            if frases_na_janela is not None and frases_na_janela(inicio, fim) == 0:
                escrever("   AVISO: o log do jarvis não tem nenhuma frase do microfone nesta troca.")
                escrever("   Confirma que o jarvis está aberto e a ouvir, e repete a troca.")
                if tecla("   r repete a troca, Enter segue assim:", ("r", ENTER)) == "r":
                    continue
            escrever("   " + " · ".join(f"{t} = {nome}" for t, nome in MARCAS.items()))
            marca = tecla("   Como foi esta troca? (1/2/3/4)", teclas_da_marca)
            sessao.trocas.append(RegistoDaTroca(troca.id, FEITA, inicio=inicio, fim=fim, marca=marca))
            guardar_sessao(sessao, caminho)
            break
    sessao.concluida = True
    guardar_sessao(sessao, caminho)
    return True


# --- Linha de comandos -------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/sessao_naturalidade.py",
        description=(
            "Sessao de naturalidade com a voz real do Sponsor: 20 trocas guiadas, uma tecla por troca, "
            "e o relatorio a partir do log do jarvis. Nunca toca som nem abre o microfone."
        ),
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--continuar", action="store_true", help="retoma a ultima sessao por acabar")
    modo.add_argument("--relatorio", action="store_true", help="so le a ultima sessao e reescreve o relatorio")
    modo.add_argument("--autoteste", action="store_true", help="parser e metas com logs falsos (sem hardware nem som)")
    parser.add_argument("--fase", choices=FASES, default=FASE_BASE, help="linha-de-base (agora) ou final (no fim)")
    parser.add_argument("--projeto-1", metavar="NOME", help="projeto para <projeto-1> (por omissao o mais usado)")
    parser.add_argument("--projeto-2", metavar="NOME", help="projeto para <projeto-2> (por omissao o segundo)")
    parser.add_argument("--sessao", metavar="FICHEIRO", help="com --relatorio: a sessao a ler (por omissao a ultima)")
    parser.add_argument(
        "--evidencia",
        metavar="FICHEIRO",
        help="onde escrever o relatorio (dentro de docs/forja/evidence/; por omissao naturalidade-<fase>-<data>.md)",
    )
    return parser


def _log_de_hoje(pasta: Path = PASTA_LOGS) -> list[str]:
    caminho = caminho_do_log(pasta=pasta)
    if not caminho.is_file():
        return []
    return caminho.read_text(encoding="utf-8", errors="replace").splitlines()


def frases_do_microfone_entre(leitura: LeituraDoLog, inicio: str, fim: str) -> int:
    abre, fecha = ler_instante(inicio) - ANTECIPACAO, ler_instante(fim)
    return sum(1 for f in leitura.frases if f.fonte == FONTE_MICROFONE and abre <= f.quando <= fecha)


def _projetos_conhecidos() -> list[str]:
    from jarvis.projetos import com_projetos_descobertos

    config = com_projetos_descobertos(carregar_config(CAMINHO_CONFIG_PADRAO), registar=lambda _texto: None)
    return [projeto.nome for projeto in config.projetos]


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    try:
        trocas = ler_guiao()
    except (OSError, ErroDoGuiao) as erro:
        print(f"ERRO no guiao: {erro}")
        return 2
    problemas = verificar_cobertura_do_guiao(trocas)
    if problemas:
        print("ERRO: o guiao nao cobre o que a sessao mede: " + "; ".join(problemas))
        return 2

    if args.relatorio:
        caminho = Path(args.sessao) if args.sessao else ultima_sessao()
        try:
            avaliacao, destino = relatorio(caminho, trocas, args.evidencia)
        except (ErroDaSessao, ValueError) as erro:
            print(f"ERRO: {erro}")
            return 2
        print(f"{avaliacao.estado} | relatorio: {caminho_para_mostrar(destino)}")
        return 0

    if args.continuar:
        caminho = ultima_sessao()
        if caminho is None:
            print("Nao ha nenhuma sessao para continuar. Comeca uma:" + chr(10) + "  " + PASSO_DO_SPONSOR[1])
            return 2
        try:
            sessao = ler_sessao(caminho)
        except ErroDaSessao as erro:
            print(f"ERRO: {erro}")
            return 2
        if sessao.concluida:
            print(f"A ultima sessao ({caminho.name}) ja esta acabada. Comeca uma nova sem --continuar.")
            return 2
    else:
        try:
            projetos = nomes_dos_marcadores(_projetos_conhecidos(), (args.projeto_1, args.projeto_2))
        except (ConfigError, ErroDaSessao) as erro:
            print(f"ERRO: {erro}")
            return 2
        agora = datetime.datetime.now()
        sessao = Sessao(fase=args.fase, projetos=projetos, criada=f"{agora:%Y-%m-%d %H:%M}")
        caminho = PASTA_SESSOES / f"sessao-{agora:%Y%m%d-%H%M%S}.json"

    while not jarvis_a_correr(_log_de_hoje()):
        print("O jarvis nao parece estar a correr com o microfone (o log de hoje nao tem um JARVIS PRONTO ativo).")
        print("  " + PASSO_DO_SPONSOR[0])
        escolha = tecla_do_teclado("Enter para verificar outra vez, s segue assim, q sai:", (ENTER, "s", "q"))
        if escolha == "q":
            return 2
        if escolha == "s":
            break

    def frases_na_janela(inicio: str, fim: str) -> int:
        return frases_do_microfone_entre(ler_log(_log_de_hoje()), inicio, fim)

    print(f"Sessao de naturalidade ({sessao.fase}) | {len(trocas)} trocas")
    print(f"Registo da sessao: {caminho_para_mostrar(caminho)} (pasta ignorada pelo Git)")
    for linha in ["", SEPARADOR, *ECRA_INICIAL, SEPARADOR]:
        print(linha)
    if tecla_do_teclado("Enter para seguir para a primeira troca (q sai):", (ENTER, "q")) == "q":
        guardar_sessao(sessao, caminho)
        print("\nSessao guardada. Para continuar: .venv\\Scripts\\python scripts/sessao_naturalidade.py --continuar")
        return 0
    acabou = correr_sessao(sessao, trocas, caminho, frases_na_janela=frases_na_janela)
    if not acabou:
        print("\nSessao guardada. Para continuar: .venv\\Scripts\\python scripts/sessao_naturalidade.py --continuar")
    try:
        avaliacao, destino = relatorio(caminho, trocas, args.evidencia)
    except (ErroDaSessao, ValueError) as erro:
        print(f"ERRO: {erro}")
        return 2
    print(f"\n{avaliacao.estado} | relatorio: {caminho_para_mostrar(destino)}")
    for falha in avaliacao.falhas:
        print(f"  - {falha}")
    return 0


# --- Logs falsos (autoteste e testes) ----------------------------------------------


class EscritorDeLogFalso:
    """Escreve um log com o formato do jarvis, com relogio de parede controlado."""

    def __init__(self, inicio: datetime.datetime) -> None:
        self.agora = inicio
        self.linhas: list[str] = []
        self._numero = 0

    def avancar(self, segundos: float) -> None:
        self.agora += datetime.timedelta(seconds=segundos)

    def linha(self, texto: str) -> None:
        self.linhas.append(f"{agora_iso(self.agora)} {texto}")

    def arranque(self, *, microfone: bool = True) -> None:
        self._numero = 0
        self.linha("jarvis a arrancar | log em logs/jarvis-teste.log")
        self.linha("configuracao: 2 projeto(s) | lingua=en | motor=motor-falso (cpu) | voz=ligada")
        self.linhas += [
            "",
            "=" * 78,
            "   JARVIS PRONTO em 5.0 s (meta <= 30 s)",
            "   ouvido: " + ("Microfone Ficticio (MME)" if microfone else "3 ficheiro(s) WAV (o microfone NAO e usado)"),
            "=" * 78,
        ]

    def frase(
        self,
        intencao: str | None,
        projeto: str | None = None,
        *,
        desfecho: str = "executado",
        motivo: str = "sem confirmacao",
        resposta_ao_recap: bool = False,
        continuacao: bool = False,
        fala_ms: float | None = 800.0,
        fecho_ms: float = 600.0,
        com_ultima_voz: bool = True,
    ) -> int:
        """Uma frase inteira; a ultima voz foi `fecho_ms` + 300 ms antes da primeira linha."""
        self._numero += 1
        n = self._numero
        ultima_voz = self.agora - datetime.timedelta(milliseconds=fecho_ms + 300)
        self.linha(formatar_etapa(n, 1, 1000.0, "tecla de falar | audio 1.00 s"))
        self.linha(formatar_etapa(n, 2, 200.0, "motor=motor-falso inferencia=200 ms | texto: 'frase ficticia'"))
        self.linha(f"frase #{n} | primeiro sinal de vida: 300 ms desde o fim da fala (linha A PENSAR)")
        if com_ultima_voz:
            self.linha(f"frase #{n} | ultima voz: {agora_iso(ultima_voz)} | {fecho_ms:.0f} ms antes do fim da escuta")
        if resposta_ao_recap:
            detalhe = "resposta ao recap pendente"
        else:
            detalhe = (
                f"intencao={intencao or 'desconhecido'} projeto={projeto or '-'} origem=llm modelo=ficticio "
                "llm=400 ms | prompt: 'pedido ficticio' | motivo: ficticio"
            )
            if continuacao:
                detalhe = "continuacao da pergunta geral (a resposta anterior acabou numa pergunta; " \
                    "sem intencoes de projeto) | " + detalhe
        self.linha(formatar_etapa(n, 3, 400.0, detalhe))
        if fala_ms is not None:
            # O fim da fala (fecho do VAD) e sempre depois da ultima voz: a latencia dele e mais curta.
            self.linha(f"frase #{n} | inicio da resposta falada: {fala_ms - fecho_ms:.0f} ms desde o fim da fala")
            if com_ultima_voz:
                self.linha(f"frase #{n} | resposta falada desde a ultima voz: {fala_ms:.0f} ms")
        self.linha(f"frase #{n} | desfecho: {desfecho} | {motivo}")
        self.linha(f"frase #{n} | TOTAL |    1500 ms desde o inicio da escuta | 500 ms desde o fim da fala | fim")
        return n

    def frase_do_cerebro(
        self,
        *,
        resposta_ms: float | None = 1200.0,
        web: bool = False,
        aviso_ms: float | None = None,
        sem_ativacao: bool = False,
        estado: str = "respondido",
        com_resumo: bool = True,
        com_ultima_voz: bool = True,
        fecho_ms: float = 600.0,
    ) -> int:
        """Uma frase que vai ao cerebro, com as linhas que o jarvis escreve.

        `aviso_ms` e o aviso curto dito antes da resposta (quando ela demora);
        `resposta_ms` e a primeira frase da resposta (None: nada foi dito).
        """
        self._numero += 1
        n = self._numero
        ultima_voz = self.agora - datetime.timedelta(milliseconds=fecho_ms + 300)
        self.linha(formatar_etapa(n, 1, 1000.0, "tecla de falar | audio 1.00 s"))
        self.linha(formatar_etapa(n, 2, 200.0, "motor=motor-falso inferencia=200 ms | texto: 'frase ficticia'"))
        if com_ultima_voz:
            self.linha(f"frase #{n} | ultima voz: {agora_iso(ultima_voz)} | {fecho_ms:.0f} ms antes do fim da escuta")
        self.linha(
            formatar_etapa(
                n,
                3,
                0.0,
                "intencao=cerebro projeto=- origem=cerebro modelo=modelo-ficticio llm=0 ms | prompt: 'frase ficticia' "
                "| motivo: fora do caminho rapido, a conversa segue pelo cerebro"
                + (" (ouvida sem palavra de ativacao)" if sem_ativacao else ""),
            )
        )
        self.linha("cerebro | frase ao cerebro (modelo-ficticio" + (", ouvida sem palavra de ativacao" if sem_ativacao else "") + ")")
        primeiro_som = aviso_ms if aviso_ms is not None else resposta_ms
        if aviso_ms is not None:
            self.linha("cerebro | sem frase pronta em 1 s: aviso curto")
        if primeiro_som is not None and com_ultima_voz:
            self.linha(f"frase #{n} | resposta falada desde a ultima voz: {primeiro_som:.0f} ms")
        self.linha(f"frase #{n} | desfecho: executado | conversa pelo cerebro")
        self.linha(f"frase #{n} | TOTAL |    1500 ms desde o inicio da escuta | 500 ms desde o fim da fala | fim")
        if resposta_ms is not None and com_ultima_voz:
            self.linha(f"cerebro | primeira frase pronta: {resposta_ms - 100:.0f} ms desde a ultima voz da frase #{n}")
            self.linha(f"cerebro | resposta falada: {resposta_ms:.0f} ms desde a ultima voz da frase #{n}")
        if com_resumo:
            self.resumo_do_cerebro(estado=estado, web=web)
        return n

    def resumo_do_cerebro(self, *, estado: str = "respondido", web: bool = False) -> None:
        """A linha de resumo que fecha o turno mais antigo ainda aberto."""
        pesquisa = "sim (1 uso(s), 1 pesquisa(s))" if web else "nao"
        entrada = 9000 if web else 5000
        self.linha(
            f"cerebro | {estado} em 1.2 s (ok) | intencao=cerebro | pesquisa web: {pesquisa} "
            f"| tokens: input=12 cache_read={entrada - 312} cache_creation=300 output=40 "
            f"| contexto: {entrada} tokens | primeiro texto: 400 ms: Claude says: 'resposta ficticia'"
        )

    def resposta_geral(self, numero: int, ms: float, *, com_ultima_voz: bool = True) -> None:
        self.linha(f"pergunta | respondida em {ms / 1000:.1f} s (ok): Claude says: 'resposta ficticia'")
        if com_ultima_voz:
            self.linha(f"pergunta | resposta falada: {ms:.0f} ms desde a ultima voz da frase #{numero}")

    def interrupcao(self, ms: float | None) -> None:
        self.linha("interrupcao | fala detetada por cima da voz")
        if ms is None:
            self.linha("interrupcao | falsa: so ruido, a resposta continua")
        else:
            self.linha(f"interrupcao | voz parada: {ms:.0f} ms desde o inicio da fala")


def escrever_troca_falsa(
    log: EscritorDeLogFalso,
    troca: Troca,
    *,
    local_ms: float = 800.0,
    cerebro_ms: float = 1200.0,
    pesquisa_ms: float = 3000.0,
    som_ms: float = 900.0,
    interrupcao_ms: float | None = 200.0,
    com_ultima_voz: bool = True,
) -> None:
    """As frases que o jarvis com o cerebro escreve quando a troca corre como o guiao pede."""
    voz = {"com_ultima_voz": com_ultima_voz}
    if troca.tipo == "local":
        log.frase("horas", fala_ms=local_ms, **voz)
        return
    if troca.tipo == "pesquisa":
        log.frase_do_cerebro(resposta_ms=pesquisa_ms, web=True, aviso_ms=som_ms, **voz)
        return
    # O cerebro faz a pergunta do projeto e o recap; a resposta ao recap e local.
    log.frase_do_cerebro(resposta_ms=cerebro_ms, sem_ativacao=troca.tipo == "seguimento", **voz)
    if troca.tipo == "sem-projeto":
        log.avancar(3)
        log.frase_do_cerebro(resposta_ms=cerebro_ms, **voz)
    if troca.tipo in ("ditado", "sem-projeto"):
        log.avancar(3)
        cancelado = troca.tipo == "sem-projeto"
        log.frase(
            None,
            resposta_ao_recap=True,
            desfecho="cancelado" if cancelado else "executado",
            motivo="cancelado pelo utilizador" if cancelado else "confirmado",
            fala_ms=local_ms,
            **voz,
        )
    if troca.tipo == "interromper":
        log.avancar(2)
        log.interrupcao(interrupcao_ms)


def sessao_falsa(
    trocas: Sequence[Troca],
    inicio: datetime.datetime,
    *,
    origem: str = ORIGEM_SPONSOR,
    fase: str = FASE_BASE,
    microfone: bool = True,
    marcas: Sequence[str] | None = None,
    **tempos,
) -> tuple[Sessao, list[str]]:
    """Uma sessao completa em que tudo corre como o guiao pede, e o log correspondente."""
    log = EscritorDeLogFalso(inicio)
    log.arranque(microfone=microfone)
    sessao = Sessao(fase=fase, projetos=dict(PROJETOS_FICTICIOS), origem=origem, criada=f"{inicio:%Y-%m-%d %H:%M}")
    sessao.concluida = True
    marcas = list(marcas) if marcas is not None else [NATURAL] * len(trocas)
    for troca, marca in zip(trocas, marcas):
        log.avancar(10)
        registo = RegistoDaTroca(troca.id, FEITA, inicio=agora_iso(log.agora), marca=marca)
        log.avancar(3)
        escrever_troca_falsa(log, troca, **tempos)
        log.avancar(2)
        registo.fim = agora_iso(log.agora)
        sessao.trocas.append(registo)
    return sessao, log.linhas


PROJETOS_FICTICIOS = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois"}


def _autoteste() -> int:
    import contextlib
    import io

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    trocas = ler_guiao()
    verificar("guiao: 20 trocas e todos os casos", verificar_cobertura_do_guiao(trocas), [])
    inicio = datetime.datetime(2026, 9, 27, 10, 0, 0)

    def avaliar(sessao: Sessao, linhas: list[str]) -> Avaliacao:
        return avaliar_sessao(sessao, trocas, ler_log(linhas))

    # 1. Tudo como o guiao pede, com a interrupcao no log: MET, e nada privado no relatorio.
    sessao, linhas = sessao_falsa(trocas, inicio)
    boa = avaliar(sessao, linhas)
    verificar("sessao perfeita: MET", (boa.estado, boa.falhas, boa.nao_medidas), (ESTADO_CUMPRIDA, [], []))
    verificar("20 trocas, 20 naturais", (len(boa.resultados), boa.naturais), (20, 20))
    verificar("so a pergunta do pedido sem projeto, e esperada", boa.perguntas_a_mais, 0)
    verificar("latencia local desde a ultima voz, nao do fim da fala", set(boa.local_ms), {800.0})
    verificar("cerebro sem pesquisa: primeira frase falada", set(boa.cerebro_ms), {1200.0})
    verificar("cerebro com pesquisa: a resposta, nao o aviso curto", set(boa.pesquisa_ms), {3000.0})
    verificar("sem perguntas gerais pelo interprete, sem as metas delas", boa.geral_ms, [])
    verificar("tokens de entrada do cerebro em nota", any("brain input tokens" in n for n in boa.notas), True)
    verificar("interrupcao medida", (boa.interrupcao_ms, boa.interrupcoes_falsas), ([200.0], 0))
    texto = texto_do_relatorio(boa)
    verificar("relatorio sem nomes de projetos", any(n in texto for n in PROJETOS_FICTICIOS.values()), False)
    verificar("relatorio sem texto das frases", "ficticia" in texto or "ficticio" in texto, False)

    # 2. Sem as linhas da interrupcao: "not measured", nunca cumprida nem falhada.
    sessao_sem, linhas_sem = sessao_falsa(trocas, inicio)
    sem_interrupcao = [linha for linha in linhas_sem if " interrupcao | " not in linha]
    sem = avaliar(sessao_sem, sem_interrupcao)
    verificar(
        "sem linhas da interrupcao: not measured",
        (sem.estado, len(sem.nao_medidas), NAO_MEDIDO in texto_do_relatorio(sem)),
        (ESTADO_CUMPRIDA, 2, True),
    )

    # 3. Sem sessao, ou sessao que nao e do Sponsor: PENDING com o passo exato.
    pendente = avaliar_sessao(None, trocas, LeituraDoLog())
    texto = texto_do_relatorio(pendente)
    verificar("sem sessao: PENDING - Sponsor step", (pendente.estado, ESTADO_PENDENTE in texto), (ESTADO_PENDENTE, True))
    verificar("PENDING diz o passo exato e nao declara metas", (PASSO_DO_SPONSOR[1] in texto, "## Targets" in texto), (True, False))
    falsa, linhas_falsas = sessao_falsa(trocas, inicio, origem=ORIGEM_AUTOTESTE)
    verificar("sessao do autoteste nunca conta", avaliar(falsa, linhas_falsas).estado, ESTADO_PENDENTE)

    # 4. Frases de ficheiros WAV (voz sintetica) ficam de fora.
    wav, linhas_wav = sessao_falsa(trocas, inicio, microfone=False)
    verificar("jarvis com --wav: PENDING", avaliar(wav, linhas_wav).estado, ESTADO_PENDENTE)

    # 5. Lento: as metas de latencia falham, cada uma pelo seu numero.
    lenta_s, lenta_l = sessao_falsa(
        trocas, inicio, local_ms=1900.0, cerebro_ms=1600.0, pesquisa_ms=7000.0, interrupcao_ms=450.0
    )
    lenta = avaliar(lenta_s, lenta_l)
    verificar(
        "lento: NOT MET no cerebro, na pesquisa, no local e na interrupcao",
        (lenta.estado, len(lenta.falhas)),
        (ESTADO_NAO_CUMPRIDA, 4),
    )

    # 6. As teclas do Sponsor: naturais, repeticoes e "hey jarvis" sem precisar.
    marcas = [NATURAL] * 14 + [POUCO_NATURAL, REPETIU, REPETIU, ATIVACAO_A_MAIS, NATURAL, NATURAL]
    teclas_s, teclas_l = sessao_falsa(trocas, inicio, marcas=marcas)
    teclas = avaliar(teclas_s, teclas_l)
    verificar(
        "teclas: 16 naturais, 2 repeticoes, 1 hey jarvis a mais",
        (teclas.naturais, teclas.repeticoes, teclas.ativacoes_a_mais, len(teclas.falhas)),
        (16, 2, 1, 2),
    )

    # 7. "Which project?" numa troca que nao a espera conta como pergunta desnecessaria.
    log = EscritorDeLogFalso(inicio)
    log.arranque()
    perguntas = Sessao(fase=FASE_BASE, projetos=dict(PROJETOS_FICTICIOS), criada="teste")
    for troca in trocas:
        log.avancar(10)
        registo = RegistoDaTroca(troca.id, FEITA, inicio=agora_iso(log.agora), marca=NATURAL)
        log.avancar(3)
        if troca.tipo == "estado":
            log.frase("estado", None, desfecho="pendente", motivo="a espera de confirmacao")
            log.avancar(2)
            log.frase("desconhecido", desfecho="nao_percebido", motivo="nada percebido")
        else:
            escrever_troca_falsa(log, troca)
        log.avancar(2)
        registo.fim = agora_iso(log.agora)
        perguntas.trocas.append(registo)
    estados = sum(1 for t in trocas if t.tipo == "estado")
    verificar("perguntas desnecessarias contadas", avaliar(perguntas, log.linhas).perguntas_a_mais, 2 * estados)

    # 8. Um jarvis sem a linha da ultima voz: nenhuma latencia medida (nunca do fecho do VAD).
    antigo_s, antigo_l = sessao_falsa(trocas, inicio, com_ultima_voz=False)
    antigo = avaliar(antigo_s, antigo_l)
    verificar(
        "sem ultima voz: sem latencias",
        (antigo.local_ms, antigo.cerebro_ms, antigo.pesquisa_ms),
        ([], [], []),
    )

    # 9. A linha de base aparece ao lado da sessao final.
    final_s, final_l = sessao_falsa(trocas, inicio, fase=FASE_FINAL, local_ms=700.0)
    texto = texto_do_relatorio(avaliar(final_s, final_l), base=lenta)
    verificar("final compara com a linha de base", "| baseline | now |" in texto, True)

    # 10. A sessao guiada guarda janelas e teclas, e o q guarda e sai.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "sessao-teste.json"
        nova = Sessao(fase=FASE_BASE, projetos=dict(PROJETOS_FICTICIOS))
        relogio = iter(inicio + datetime.timedelta(seconds=s) for s in range(0, 1000, 5))
        teclas_premidas = iter([ENTER, ENTER, NATURAL, "p", ENTER, ENTER, REPETIU, "q"])
        with contextlib.redirect_stdout(io.StringIO()):
            acabou = correr_sessao(
                nova, trocas, caminho, tecla=lambda _p, _v: next(teclas_premidas), escrever=lambda _t: None,
                agora=lambda: next(relogio),
            )
        guardada = ler_sessao(caminho)
        verificar(
            "teclas registadas, p salta e q guarda e sai",
            (acabou, [(r.id, r.estado, r.marca) for r in guardada.trocas]),
            (False, [(trocas[0].id, FEITA, NATURAL), (trocas[1].id, SALTADA, None), (trocas[2].id, FEITA, REPETIU)]),
        )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste da sessao de naturalidade completo (logs falsos, sem hardware, sem som).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
