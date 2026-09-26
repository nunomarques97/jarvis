r"""Aceitacao diaria do jarvis com a voz real do Sponsor: guiao, medicao e evidencia.

    .venv\Scripts\python scripts/aceitacao_sponsor.py --projeto-teste <nome>

Guia uma sessao de cerca de 15 minutos com o jarvis a correr noutra janela
(`python -m jarvis`, microfone verdadeiro). As tarefas vem de
`tests/voz/guiao-aceitacao.md` e cobrem os quatro casos de uso: (a) ditar um
prompt, (b) ouvir o estado e o relatorio, (c) lancar, parar e retomar um run
FORJA, (d) a conversa maos-livres, mais os comandos locais.

Para cada tarefa: Enter para comecar, a tarefa e feita na janela do jarvis
(a resposta ao recap diz-se logo, sem "hey jarvis" nem tecla),
Enter quando ele acabar; depois o Sponsor responde por tecla (s/n) se o jarvis
fez o que ele pediu e, nos ditados e lancamentos, se o que foi enviado tinha
algum pedido que ele nao fez. `p` salta a tarefa e `q` guarda e sai
(`--continuar` retoma a ultima sessao por acabar).

A sessao (horas de inicio e fim de cada tarefa e as respostas s/n) fica em
`logs/aceitacao/` (ignorada pelo Git). No fim, o script le o log do jarvis
(`logs/jarvis-<data>.log`) nessas janelas e escreve a evidencia em
docs/forja/evidence/ com:

  * intencao e projeto a primeira (meta >= 90%);
  * ditados aceites sem correcao (meta >= 80%) e pedidos inventados (meta 0);
  * latencias de ponta a ponta p50/p95 contra as metas do jarvis integrado
    (as mesmas de `scripts/medir_ponta_a_ponta.py`);
  * a cobertura: pelo menos uma tarefa passada em cada caso (a)-(d);
  * a comparacao com a linha de base medida antes da reescrita.

So contam frases do microfone: um jarvis a correr com `--wav` (ficheiros,
voz sintetica) fica de fora. Sem sessao do Sponsor, a evidencia diz
"PENDENTE — passo do Sponsor" e o passo exato; nada e simulado. A evidencia so
leva numeros, ids do guiao e nomes de intencoes: nem transcricoes, nem
prompts, nem nomes de projetos.

Este script nunca toca som nem abre o microfone.

Uso:
    .venv\Scripts\python scripts/aceitacao_sponsor.py --projeto-teste <nome>   # sessao nova
    .venv\Scripts\python scripts/aceitacao_sponsor.py --continuar             # retoma a ultima
    .venv\Scripts\python scripts/aceitacao_sponsor.py --relatorio             # so reescreve a evidencia
    .venv\Scripts\python scripts/aceitacao_sponsor.py --autoteste             # logs falsos, sem hardware
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.app import PASTA_LOGS, Jarvis, agora_iso, caminho_do_log, formatar_etapa  # noqa: E402
from jarvis.audio_util import PASTA_EVIDENCIA, caminho_evidencia_de_saida, caminho_para_mostrar  # noqa: E402
from jarvis.config import CAMINHO_CONFIG_PADRAO, ConfigError, carregar_config  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.interprete import INTENCOES, Interpretacao  # noqa: E402

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


#: As metas de latencia vem da medicao de ponta a ponta: uma so fonte.
ponta = _carregar_modulo_irmao("medir_ponta_a_ponta")

# --- Constantes -------------------------------------------------------------------

GUIAO = RAIZ / "tests" / "voz" / "guiao-aceitacao.md"
PASTA_SESSOES = PASTA_LOGS / "aceitacao"

CASOS_DE_USO = ("a", "b", "c", "d")
CASOS = CASOS_DE_USO + ("local",)
FLUXOS = ("imediato", "confirmar", "corrigir", "cancelar", "projeto", "conversa")
MARCADORES = ("<projeto-1>", "<projeto-2>", "<projeto-teste>")
MARCADOR_TESTE = "<projeto-teste>"
SEM_PROJETO = "—"
LINGUAS = ("en", "pt")
COLUNAS = ("id", "caso", "tarefa", "exemplo (en)", "exemplo (pt)", "intenção", "projeto", "fluxo")
PADRAO_ID = re.compile(r"^(a|b|c|d|l)-\d{2}$")
PADRAO_MARCADOR = re.compile(r"<[^>]*>")

META_INTENCAO = 0.90
META_ACEITES = 0.80
META_INVENTADOS = 0
#: Abaixo desta fracao de tarefas feitas a sessao e INCOMPLETA: os numeros
#: mostram-se, mas nao decidem nada.
MINIMO_FEITAS = 0.80
#: Horas pedidas ao longo da sessao (amostras da latencia do comando local).
MINIMO_DE_HORAS_NO_GUIAO = 3
MINIMO_DE_DITADOS_NO_GUIAO = 5

#: Tarefas em que o Sponsor diz se o que foi enviado tinha pedidos inventados.
INTENCOES_COM_PEDIDO = frozenset({"ditar_prompt", "lancar_run"})
#: Fluxos cujo ditado conta para "aceite sem correcao".
FLUXOS_SEM_CORRECAO = frozenset({"confirmar", "conversa"})

#: Como o jarvis estava antes da reescrita (voz sintetica e comandos gravados).
LINHA_DE_BASE = {
    "intencao": "55% (amostra sintética, 2026-09-20)",
    "locais": "1/40 comandos locais percebidos",
    "horas": "5,1–5,6 s (comando local, fala → resposta)",
}

ESTADO_PENDENTE = "PENDENTE — passo do Sponsor"
ESTADO_CUMPRIDA = "CUMPRIDA"
ESTADO_NAO_CUMPRIDA = "NÃO CUMPRIDA"
ESTADO_INCOMPLETA = "INCOMPLETA"

ORIGEM_SPONSOR = "sponsor"
ORIGEM_AUTOTESTE = "autoteste"

FONTE_MICROFONE = "microfone"
FONTE_FICHEIRO = "ficheiro"
FONTE_DESCONHECIDA = "desconhecida"

PASSO_DO_SPONSOR = (
    "1. Abre o jarvis: `.venv\\Scripts\\python -m jarvis` (ou o atalho) e espera pela linha `JARVIS PRONTO`.",
    "2. Noutra janela, na pasta do jarvis: `.venv\\Scripts\\python scripts/aceitacao_sponsor.py "
    "--projeto-teste <nome>` (cerca de 15 min; `<nome>` é um projeto do config.toml onde se pode "
    "lançar e parar um run de teste).",
    "3. Segue as tarefas do ecrã (guião em `tests/voz/guiao-aceitacao.md`); a evidência é escrita no fim.",
)


class ErroDoGuiao(Exception):
    """O guiao nao cumpre o formato; a mensagem diz a linha e o problema."""


class ErroDaSessao(Exception):
    """A sessao nao pode comecar ou nao se consegue ler."""


# --- O guiao -----------------------------------------------------------------------


@dataclass(frozen=True)
class Tarefa:
    id: str
    caso: str
    tarefa: str
    exemplos: dict
    intencao: str
    #: O marcador do projeto esperado, ou None quando o jarvis nao deve escolher projeto.
    projeto: str | None
    fluxo: str

    @property
    def pede_projeto_teste(self) -> bool:
        return MARCADOR_TESTE in self.tarefa or self.projeto == MARCADOR_TESTE

    @property
    def pergunta_inventados(self) -> bool:
        return self.intencao in INTENCOES_COM_PEDIDO and self.fluxo != "cancelar"

    @property
    def conta_para_aceites(self) -> bool:
        return self.intencao == "ditar_prompt" and self.fluxo in FLUXOS_SEM_CORRECAO


def _celulas(linha: str) -> list[str]:
    return [celula.strip() for celula in linha.strip().strip("|").split("|")]


def ler_guiao(caminho: Path = GUIAO) -> list[Tarefa]:
    """Le e valida a tabela do guiao. Levanta ErroDoGuiao com a linha em falta."""
    texto = Path(caminho).read_text(encoding="utf-8")
    tarefas: list[Tarefa] = []
    vistos: set[str] = set()
    cabecalho_visto = False
    for numero, linha in enumerate(texto.splitlines(), start=1):
        if not linha.lstrip().startswith("|"):
            continue
        celulas = _celulas(linha)
        if not cabecalho_visto:
            if tuple(celulas) != COLUNAS:
                raise ErroDoGuiao(f"{Path(caminho).name}:{numero}: cabecalho esperado {COLUNAS}, obtido {celulas}")
            cabecalho_visto = True
            continue
        if all(set(c) <= set(":-") for c in celulas):
            continue
        if len(celulas) != len(COLUNAS):
            raise ErroDoGuiao(f"{Path(caminho).name}:{numero}: {len(celulas)} colunas, esperava {len(COLUNAS)}")
        id_, caso, tarefa, exemplo_en, exemplo_pt, intencao, projeto, fluxo = celulas
        onde = f"{Path(caminho).name}:{numero} ({id_})"
        if not PADRAO_ID.match(id_):
            raise ErroDoGuiao(f"{onde}: id tem de ser a-NN, b-NN, c-NN, d-NN ou l-NN")
        if id_ in vistos:
            raise ErroDoGuiao(f"{onde}: id repetido")
        vistos.add(id_)
        if caso not in CASOS:
            raise ErroDoGuiao(f"{onde}: caso '{caso}' fora de {CASOS}")
        if id_[0] != ("l" if caso == "local" else caso):
            raise ErroDoGuiao(f"{onde}: o id nao bate com o caso '{caso}'")
        if intencao not in INTENCOES:
            raise ErroDoGuiao(f"{onde}: intencao '{intencao}' fora da lista fechada do interprete")
        if fluxo not in FLUXOS:
            raise ErroDoGuiao(f"{onde}: fluxo '{fluxo}' fora de {FLUXOS}")
        for texto_com_marcadores in (tarefa, exemplo_en, exemplo_pt):
            for marcador in PADRAO_MARCADOR.findall(texto_com_marcadores):
                if marcador not in MARCADORES:
                    raise ErroDoGuiao(f"{onde}: so se aceitam os marcadores {MARCADORES}, nao '{marcador}'")
        if projeto == SEM_PROJETO:
            projeto_esperado = None
        elif projeto in MARCADORES:
            if projeto not in exemplo_en or projeto not in exemplo_pt:
                raise ErroDoGuiao(f"{onde}: o projeto {projeto} tem de aparecer nos dois exemplos")
            projeto_esperado = projeto
        else:
            raise ErroDoGuiao(f"{onde}: projeto tem de ser um marcador ou '{SEM_PROJETO}'")
        if fluxo == "projeto" and projeto_esperado is not None:
            raise ErroDoGuiao(f"{onde}: o fluxo 'projeto' e um ditado sem projeto ('{SEM_PROJETO}')")
        tarefas.append(
            Tarefa(id_, caso, tarefa, {"en": exemplo_en, "pt": exemplo_pt}, intencao, projeto_esperado, fluxo)
        )
    if not cabecalho_visto:
        raise ErroDoGuiao(f"{Path(caminho).name}: tabela do guiao nao encontrada")
    return tarefas


def verificar_cobertura_do_guiao(tarefas: Sequence[Tarefa]) -> list[str]:
    """O guiao cobre os casos (a)-(d) e da amostras para cada meta. Vazia = cumpre."""
    problemas: list[str] = []
    for caso in CASOS_DE_USO:
        if not any(t.caso == caso for t in tarefas):
            problemas.append(f"caso ({caso}) sem tarefas")
    horas = sum(1 for t in tarefas if t.intencao == "horas")
    if horas < MINIMO_DE_HORAS_NO_GUIAO:
        problemas.append(f"{horas} tarefa(s) de horas, minimo {MINIMO_DE_HORAS_NO_GUIAO}")
    ditados = sum(1 for t in tarefas if t.conta_para_aceites)
    if ditados < MINIMO_DE_DITADOS_NO_GUIAO:
        problemas.append(f"{ditados} ditado(s) que contam para as metas, minimo {MINIMO_DE_DITADOS_NO_GUIAO}")
    for fluxo in ("corrigir", "cancelar", "conversa"):
        if not any(t.fluxo == fluxo for t in tarefas):
            problemas.append(f"nenhuma tarefa com o fluxo '{fluxo}'")
    return problemas


def nomes_dos_marcadores(nomes: Sequence[str], projeto_teste: str | None) -> dict[str, str]:
    """<projeto-N> -> nome do projeto N da configuracao; <projeto-teste> so se for dado."""
    nomes = list(nomes)
    if not nomes:
        raise ErroDaSessao("a configuracao nao tem projetos; o guiao precisa de pelo menos um")
    mapa = {"<projeto-1>": nomes[0], "<projeto-2>": nomes[1] if len(nomes) > 1 else nomes[0]}
    if projeto_teste:
        if projeto_teste not in nomes:
            raise ErroDaSessao(f"--projeto-teste: '{projeto_teste}' nao e um projeto do config.toml")
        mapa[MARCADOR_TESTE] = projeto_teste
    return mapa


def trocar_marcadores(texto: str, projetos: dict[str, str]) -> str:
    for marcador, nome in projetos.items():
        texto = texto.replace(marcador, nome)
    return texto


# --- A sessao ----------------------------------------------------------------------

FEITA = "feita"
SALTADA = "saltada"
FORMATO_INSTANTE = "%Y-%m-%d %H:%M:%S.%f"


def ler_instante(texto: str) -> datetime.datetime:
    return datetime.datetime.strptime(texto, FORMATO_INSTANTE)


@dataclass
class RegistoDaTarefa:
    id: str
    estado: str
    inicio: str | None = None
    fim: str | None = None
    #: s/n do Sponsor: o jarvis fez o que ele pediu.
    fez_o_pedido: bool | None = None
    #: s/n do Sponsor: o que foi enviado tinha um pedido que ele nao fez.
    inventou: bool | None = None
    motivo: str = ""


@dataclass
class Sessao:
    lingua: str
    #: Nomes reais dos projetos: so em logs/ (ignorada), nunca na evidencia.
    projetos: dict
    origem: str = ORIGEM_SPONSOR
    criada: str = ""
    concluida: bool = False
    tarefas: list = field(default_factory=list)

    def registo(self, id_: str) -> RegistoDaTarefa | None:
        return next((r for r in self.tarefas if r.id == id_), None)

    def para_json(self) -> dict:
        return {
            "versao": 1,
            "origem": self.origem,
            "lingua": self.lingua,
            "criada": self.criada,
            "concluida": self.concluida,
            "projetos": self.projetos,
            "tarefas": [vars(r) for r in self.tarefas],
        }

    @classmethod
    def de_json(cls, dados: dict) -> "Sessao":
        try:
            return cls(
                lingua=dados["lingua"],
                projetos=dict(dados["projetos"]),
                origem=dados.get("origem", ""),
                criada=dados.get("criada", ""),
                concluida=bool(dados.get("concluida")),
                tarefas=[RegistoDaTarefa(**r) for r in dados.get("tarefas", [])],
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
        raise ErroDaSessao(f"{caminho.name}: {erro}") from erro


def ultima_sessao(pasta: Path = PASTA_SESSOES) -> Path | None:
    candidatas = sorted(Path(pasta).glob("sessao-*.json")) if Path(pasta).is_dir() else []
    return candidatas[-1] if candidatas else None


# --- O log do jarvis ---------------------------------------------------------------

_LINHA = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (.*)$")
_FRASE = re.compile(r"^frase #(\d+) \| (.*)$")
_ETAPA_3 = re.compile(r"^etapa 3/5 interprete\s*\|\s*-?\d+ ms \| (.*)$")
_INTENCAO = re.compile(r"intencao=(\S+) projeto=(\S+)")
_SINAL = re.compile(r"^primeiro sinal de vida: (-?\d+) ms")
_FALA = re.compile(r"^inicio da resposta falada: (-?\d+) ms")
_DESFECHO = re.compile(r"^desfecho: (\S+) \| ?(.*)$")
_ENTREGUE = re.compile(r"^canal \| prompt confirmado entregue ao canal do (\S+): ")
_PERGUNTA = re.compile(r"^conversa \| o (\S+) fez uma pergunta")
_CONFIGURACAO = re.compile(r"^configuracao: .*\blingua=(\w+)")
_PRONTO = "JARVIS PRONTO em"
_OUVIDO = re.compile(r"^\s+ouvido: (.*)$")


@dataclass
class FraseDoLog:
    segmento: int
    numero: int
    instante: datetime.datetime
    fonte: str
    lingua: str | None = None
    intencao: str | None = None
    projeto: str | None = None
    resposta_ao_recap: bool = False
    na_conversa: bool = False
    ignorada: bool = False
    sinal_de_vida_ms: float | None = None
    primeira_fala_ms: float | None = None
    desfecho: str | None = None
    motivo: str = ""

    @property
    def primeira_da_tarefa(self) -> bool:
        """Uma frase nova (nao a resposta a um recap nem dentro de uma conversa)."""
        return not self.resposta_ao_recap and not self.na_conversa

    @property
    def e_correcao(self) -> bool:
        """Resposta ao recap que produziu um recap novo (corrigir, acrescentar ou dizer o projeto)."""
        return self.resposta_ao_recap and self.desfecho == "pendente" and self.motivo.startswith("a espera")


@dataclass(frozen=True)
class EventoDoLog:
    instante: datetime.datetime
    tipo: str
    projeto: str
    fonte: str


def ler_log(linhas: Iterable[str]) -> tuple[list[FraseDoLog], list[EventoDoLog]]:
    """As frases e os eventos (prompt entregue, pergunta do Claude) de um log do jarvis.

    Cada `JARVIS PRONTO` abre um segmento novo (a numeracao das frases
    recomeca em cada processo); a linha `ouvido:` a seguir diz se o segmento
    ouviu o microfone ou ficheiros WAV.
    """
    frases: dict[tuple[int, int], FraseDoLog] = {}
    eventos: list[EventoDoLog] = []
    segmento = 0
    fonte = FONTE_DESCONHECIDA
    lingua_configurada: str | None = None
    lingua_do_segmento: str | None = None
    for crua in linhas:
        linha = crua.rstrip("\r\n")
        if _PRONTO in linha:
            segmento += 1
            fonte = FONTE_DESCONHECIDA
            lingua_do_segmento = lingua_configurada
            continue
        casamento = _OUVIDO.match(linha)
        if casamento and not _LINHA.match(linha):
            fonte = FONTE_FICHEIRO if "ficheiro" in casamento.group(1).lower() else FONTE_MICROFONE
            continue
        casamento = _LINHA.match(linha)
        if not casamento:
            continue
        try:
            instante = ler_instante(casamento.group(1) + "000")
        except ValueError:
            continue
        texto = casamento.group(2)
        configuracao = _CONFIGURACAO.match(texto)
        if configuracao:
            lingua_configurada = configuracao.group(1)
            continue
        for padrao, tipo in ((_ENTREGUE, "entregue"), (_PERGUNTA, "pergunta")):
            evento = padrao.match(texto)
            if evento:
                eventos.append(EventoDoLog(instante, tipo, evento.group(1), fonte))
        casamento = _FRASE.match(texto)
        if not casamento:
            continue
        chave = (segmento, int(casamento.group(1)))
        frase = frases.get(chave)
        if frase is None:
            frase = frases[chave] = FraseDoLog(segmento, chave[1], instante, fonte, lingua_do_segmento)
        resto = casamento.group(2)
        etapa = _ETAPA_3.match(resto)
        if etapa:
            detalhe = etapa.group(1)
            if detalhe.startswith("resposta ao recap"):
                frase.resposta_ao_recap = True
            elif detalhe.startswith("conversa"):
                frase.na_conversa = True
            elif detalhe.startswith("ignorada"):
                frase.ignorada = True
            intencao = _INTENCAO.search(detalhe)
            if intencao:
                frase.intencao = intencao.group(1)
                frase.projeto = None if intencao.group(2) == "-" else intencao.group(2)
            elif " sair " in f" {detalhe} ":
                frase.intencao = "sair_da_conversa"
            continue
        for padrao, atributo in ((_SINAL, "sinal_de_vida_ms"), (_FALA, "primeira_fala_ms")):
            medida = padrao.match(resto)
            if medida and getattr(frase, atributo) is None:
                setattr(frase, atributo, float(medida.group(1)))
        desfecho = _DESFECHO.match(resto)
        if desfecho:
            frase.desfecho, frase.motivo = desfecho.group(1), desfecho.group(2)
    return sorted(frases.values(), key=lambda f: (f.instante, f.segmento, f.numero)), eventos


def logs_da_sessao(sessao: Sessao, pasta: Path = PASTA_LOGS) -> list[Path]:
    """Os logs diarios que podem ter as janelas da sessao (o log de um processo
    fica no ficheiro do dia em que arrancou, mesmo depois da meia-noite)."""
    instantes = [ler_instante(r.inicio) for r in sessao.tarefas if r.inicio] + [
        ler_instante(r.fim) for r in sessao.tarefas if r.fim
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


def ler_logs(caminhos: Sequence[Path]) -> tuple[list[FraseDoLog], list[EventoDoLog]]:
    frases: list[FraseDoLog] = []
    eventos: list[EventoDoLog] = []
    for caminho in caminhos:
        with Path(caminho).open(encoding="utf-8", errors="replace") as ficheiro:
            novas, novos = ler_log(ficheiro)
        frases.extend(novas)
        eventos.extend(novos)
    return frases, eventos


def jarvis_a_correr(linhas: Iterable[str]) -> bool:
    """O ultimo processo do log esta pronto, a ouvir o microfone e ainda nao terminou."""
    a_correr = False
    for linha in linhas:
        if _PRONTO in linha:
            a_correr = False
        elif _OUVIDO.match(linha) and not _LINHA.match(linha):
            a_correr = "ficheiro" not in linha.lower()
        elif linha.rstrip().endswith(" jarvis terminado"):
            a_correr = False
    return a_correr


# --- A avaliacao -------------------------------------------------------------------


@dataclass
class ResultadoDaTarefa:
    tarefa: Tarefa
    registo: RegistoDaTarefa
    frases: list
    eventos: list
    excluidas: int = 0
    intencao_obtida: str | None = None
    #: certo | errado | nenhum (o esperado era nao escolher projeto) | outro
    projeto_obtido: str = ""
    acertou: bool = False
    aceite_sem_correcao: bool | None = None
    fluxo_no_log: bool = False
    falta_no_log: str = ""
    passou: bool = False


@dataclass
class Avaliacao:
    estado: str
    sessao: Sessao | None
    resultados: list = field(default_factory=list)
    saltadas: list = field(default_factory=list)
    total: int = 0
    acerto: tuple = (0, 0)
    aceites: tuple = (0, 0)
    inventados: tuple = (0, 0)
    locais: tuple = (0, 0)
    horas_ms: list = field(default_factory=list)
    recap_ms: list = field(default_factory=list)
    sinal_ms: list = field(default_factory=list)
    cobertura: dict = field(default_factory=dict)
    falhas: list = field(default_factory=list)
    pendencias: list = field(default_factory=list)
    notas: list = field(default_factory=list)
    linguas: tuple = ()


def _no_intervalo(instante: datetime.datetime, registo: RegistoDaTarefa) -> bool:
    return ler_instante(registo.inicio) <= instante <= ler_instante(registo.fim)


def _projeto_relativo(tarefa: Tarefa, obtido: str | None, projetos: dict[str, str]) -> tuple[bool, str]:
    """O projeto obtido bate com o esperado? E como se mostra sem nomes reais."""
    esperado = projetos.get(tarefa.projeto) if tarefa.projeto else None
    if tarefa.projeto is None:
        return obtido is None, "nenhum" if obtido is None else "escolheu um (devia perguntar)"
    if obtido == esperado:
        return True, "certo"
    return False, "nenhum" if obtido is None else "errado"


def _aceite_a_primeira(primeira: FraseDoLog, depois: Sequence[FraseDoLog]) -> bool:
    """O primeiro recap do ditado foi aceite com 'sim', sem correcao."""
    if primeira.intencao != "ditar_prompt" or primeira.desfecho != "pendente":
        return False
    for frase in depois:
        if not frase.resposta_ao_recap:
            continue
        if frase.desfecho in ("ignorado", "sem_pedido"):
            continue
        if frase.desfecho == "pendente" and not frase.e_correcao:
            continue  # "sim" mal ouvido: o recap repete-se, o prompt nao mudou
        return frase.desfecho == "executado"
    return False


def _fluxo_no_log(tarefa: Tarefa, frases: Sequence[FraseDoLog], eventos: Sequence[EventoDoLog]) -> str:
    """O que falta no log para o fluxo pedido ter corrido ate ao fim ('' = correu)."""
    if not frases:
        return "nenhuma frase do microfone nesta janela"
    primeira = next((f for f in frases if f.primeira_da_tarefa), None)
    executados = [f for f in frases if f.desfecho == "executado"]
    if tarefa.fluxo == "imediato":
        return "" if primeira is not None and primeira.desfecho == "executado" else "nao correu logo"
    if tarefa.fluxo == "cancelar":
        if executados or any(e.tipo == "entregue" for e in eventos):
            return "algo foi executado ou enviado"
        return "" if any(f.desfecho == "cancelado" for f in frases) else "nao ficou cancelado"
    if not executados:
        return "nada confirmado com 'sim'"
    if tarefa.fluxo == "corrigir":
        correcao = next((i for i, f in enumerate(frases) if f.e_correcao), None)
        if correcao is None:
            return "nenhuma correcao aplicada"
        if not any(f.desfecho == "executado" for f in frases[correcao + 1 :]):
            return "o recap corrigido nao foi confirmado"
    if tarefa.fluxo == "conversa":
        if not any(e.tipo == "pergunta" for e in eventos):
            return "o Claude nao fez nenhuma pergunta"
        resposta = next((i for i, f in enumerate(frases) if f.na_conversa), None)
        if resposta is None:
            return "nenhuma resposta dada na janela de conversa"
        if not any(f.desfecho == "executado" for f in frases[resposta:]):
            return "a resposta na conversa nao foi confirmada"
    if tarefa.intencao == "ditar_prompt" and not any(e.tipo == "entregue" for e in eventos):
        return "nenhum prompt entregue ao canal"
    return ""


def avaliar_tarefa(
    tarefa: Tarefa,
    registo: RegistoDaTarefa,
    frases: Sequence[FraseDoLog],
    eventos: Sequence[EventoDoLog],
    projetos: dict[str, str],
) -> ResultadoDaTarefa:
    na_janela = [f for f in frases if _no_intervalo(f.instante, registo)]
    validas = [f for f in na_janela if f.fonte == FONTE_MICROFONE]
    eventos_validos = [e for e in eventos if e.fonte == FONTE_MICROFONE and _no_intervalo(e.instante, registo)]
    resultado = ResultadoDaTarefa(tarefa, registo, validas, eventos_validos, excluidas=len(na_janela) - len(validas))
    indice = next((i for i, f in enumerate(validas) if f.primeira_da_tarefa), None)
    primeira = validas[indice] if indice is not None else None
    if primeira is not None:
        resultado.intencao_obtida = primeira.intencao or ("ignorada" if primeira.ignorada else None)
        certo_projeto, resultado.projeto_obtido = _projeto_relativo(tarefa, primeira.projeto, projetos)
        resultado.acertou = primeira.intencao == tarefa.intencao and certo_projeto
    else:
        resultado.projeto_obtido = "—"
    if tarefa.conta_para_aceites:
        resultado.aceite_sem_correcao = primeira is not None and _aceite_a_primeira(primeira, validas[indice + 1 :])
    resultado.falta_no_log = _fluxo_no_log(tarefa, validas, eventos_validos)
    resultado.fluxo_no_log = not resultado.falta_no_log
    resultado.passou = resultado.fluxo_no_log and registo.fez_o_pedido is True
    return resultado


def _fracao(par: tuple[int, int]) -> float:
    return par[0] / par[1] if par[1] else 0.0


def avaliar_sessao(
    sessao: Sessao | None,
    tarefas: Sequence[Tarefa],
    frases: Sequence[FraseDoLog],
    eventos: Sequence[EventoDoLog],
) -> Avaliacao:
    """Mede a sessao contra as metas. Sem sessao real do Sponsor: PENDENTE."""
    if sessao is None:
        return Avaliacao(ESTADO_PENDENTE, None, total=len(tarefas), pendencias=["nenhuma sessão do Sponsor gravada"])
    if sessao.origem != ORIGEM_SPONSOR:
        return Avaliacao(
            ESTADO_PENDENTE,
            sessao,
            total=len(tarefas),
            pendencias=[f"a última sessão não é do Sponsor (origem '{sessao.origem}'); não conta"],
        )
    avaliacao = Avaliacao(ESTADO_NAO_CUMPRIDA, sessao, total=len(tarefas))
    for tarefa in tarefas:
        registo = sessao.registo(tarefa.id)
        if registo is None or registo.estado != FEITA or not registo.inicio or not registo.fim:
            avaliacao.saltadas.append((tarefa.id, registo.motivo if registo else "por fazer"))
            continue
        avaliacao.resultados.append(avaliar_tarefa(tarefa, registo, frases, eventos, sessao.projetos))

    resultados = avaliacao.resultados
    excluidas = sum(r.excluidas for r in resultados)
    if excluidas:
        avaliacao.notas.append(
            f"{excluidas} frase(s) nas janelas vieram de ficheiros WAV ou de um processo sem cabeçalho: não contam"
        )
    ouvidas = [f for r in resultados for f in r.frases]
    avaliacao.linguas = tuple(sorted({f.lingua for f in ouvidas if f.lingua}))
    if not ouvidas:
        avaliacao.estado = ESTADO_PENDENTE
        avaliacao.pendencias.append(
            "o log do jarvis não tem nenhuma frase do microfone nas janelas da sessão "
            "(o jarvis estava aberto, com o microfone, durante a sessão?)"
        )
        return avaliacao

    avaliacao.acerto = (sum(r.acertou for r in resultados), len(resultados))
    aceites = [r for r in resultados if r.aceite_sem_correcao is not None]
    avaliacao.aceites = (sum(bool(r.aceite_sem_correcao) for r in aceites), len(aceites))
    perguntados = [r for r in resultados if r.tarefa.pergunta_inventados and r.registo.inventou is not None]
    avaliacao.inventados = (sum(bool(r.registo.inventou) for r in perguntados), len(perguntados))
    locais = [r for r in resultados if r.tarefa.caso == "local"]
    avaliacao.locais = (sum(r.acertou for r in locais), len(locais))
    for frase in ouvidas:
        if frase.sinal_de_vida_ms is not None:
            avaliacao.sinal_ms.append(frase.sinal_de_vida_ms)
        if frase.primeira_fala_ms is None or not frase.primeira_da_tarefa:
            continue
        if frase.intencao == "horas":
            avaliacao.horas_ms.append(frase.primeira_fala_ms)
        elif frase.intencao == "ditar_prompt" and frase.desfecho == "pendente":
            avaliacao.recap_ms.append(frase.primeira_fala_ms)
    for caso in CASOS_DE_USO:
        do_caso = [r for r in resultados if r.tarefa.caso == caso]
        avaliacao.cobertura[caso] = (sum(r.passou for r in do_caso), len(do_caso))

    falhas = avaliacao.falhas
    if _fracao(avaliacao.acerto) < META_INTENCAO:
        falhas.append(f"intenção e projeto à primeira: {_percentagem(avaliacao.acerto)} < {META_INTENCAO:.0%}")
    if not avaliacao.aceites[1]:
        falhas.append("ditados aceites sem correção: nenhum ditado feito")
    elif _fracao(avaliacao.aceites) < META_ACEITES:
        falhas.append(f"ditados aceites sem correção: {_percentagem(avaliacao.aceites)} < {META_ACEITES:.0%}")
    if avaliacao.inventados[0] > META_INVENTADOS:
        falhas.append(f"pedidos inventados: {avaliacao.inventados[0]} (meta {META_INVENTADOS})")
    for nome, valores, limite_p50, limite_p95 in (
        ("horas → resposta falada", avaliacao.horas_ms, ponta.LIMITE_HORAS_P50_MS, ponta.LIMITE_HORAS_P95_MS),
        ("ditado → recap falado", avaliacao.recap_ms, ponta.LIMITE_RECAP_P50_MS, ponta.LIMITE_RECAP_P95_MS),
    ):
        if not valores:
            falhas.append(f"{nome}: sem amostras")
            continue
        p50, p95 = ponta.percentil(valores, 50), ponta.percentil(valores, 95)
        if p50 > limite_p50:
            falhas.append(f"{nome}: p50 {p50:.0f} ms > {limite_p50:.0f} ms")
        if p95 > limite_p95:
            falhas.append(f"{nome}: p95 {p95:.0f} ms > {limite_p95:.0f} ms")
    if not avaliacao.sinal_ms:
        falhas.append("primeiro sinal de vida: sem amostras")
    elif max(avaliacao.sinal_ms) > ponta.LIMITE_SINAL_DE_VIDA_MS:
        falhas.append(
            f"primeiro sinal de vida: máximo {max(avaliacao.sinal_ms):.0f} ms > {ponta.LIMITE_SINAL_DE_VIDA_MS:.0f} ms"
        )
    for caso, (passadas, feitas) in avaliacao.cobertura.items():
        if passadas < 1:
            falhas.append(f"caso ({caso}): nenhum cenário passado ({feitas} feito(s))")

    if len(resultados) < MINIMO_FEITAS * len(tarefas):
        avaliacao.estado = ESTADO_INCOMPLETA
        avaliacao.pendencias.append(
            f"só {len(resultados)} de {len(tarefas)} tarefas feitas (mínimo {MINIMO_FEITAS:.0%}); "
            "continuar com `.venv\\Scripts\\python scripts/aceitacao_sponsor.py --continuar`"
        )
    else:
        avaliacao.estado = ESTADO_NAO_CUMPRIDA if falhas else ESTADO_CUMPRIDA
    return avaliacao


# --- A evidencia -------------------------------------------------------------------


def _percentagem(par: tuple[int, int]) -> str:
    return f"{_fracao(par):.0%} ({par[0]}/{par[1]})" if par[1] else "sem amostras"


def _segundos(ms: float) -> str:
    return f"{ms / 1000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " s"


def _estatistica(valores: Sequence[float]) -> str:
    if not valores:
        return "sem amostras"
    return (
        f"p50 {ponta.percentil(list(valores), 50):.0f} ms · p95 {ponta.percentil(list(valores), 95):.0f} ms "
        f"· média {statistics.fmean(valores):.0f} ms · n={len(valores)}"
    )


def _sim_nao(valor: bool | None) -> str:
    return "—" if valor is None else ("sim" if valor else "não")


def _cumpre(ok: bool) -> str:
    return "sim" if ok else "**não**"


def texto_da_evidencia(avaliacao: Avaliacao, quando: datetime.datetime | None = None) -> str:
    """A evidencia em markdown: so numeros, ids do guiao e nomes de intencoes."""
    quando = quando or datetime.datetime.now()
    sessao = avaliacao.sessao
    linhas = [f"# Aceitação com a voz do Sponsor — {quando:%Y-%m-%d %H:%M}", "", f"**Estado: {avaliacao.estado}**", ""]
    if avaliacao.estado == ESTADO_PENDENTE:
        linhas += ["## PENDENTE — passo do Sponsor", "", *[f"- {p}" for p in avaliacao.pendencias], ""]
        linhas += ["Passo exato:", "", *PASSO_DO_SPONSOR, ""]
        linhas += [
            "Nada foi simulado: sem a sessão com a voz real, nenhuma meta é declarada cumprida. "
            "Voz sintética e ficheiros WAV não contam.",
            "",
        ]
    if avaliacao.pendencias and avaliacao.estado != ESTADO_PENDENTE:
        linhas += ["## Pendente", "", *[f"- {p}" for p in avaliacao.pendencias], ""]
    linhas += ["## Linha de base (antes da reescrita)", ""]
    linhas += ["| medida | linha de base | agora (voz real) | meta |", "|---|---|---|---|"]
    tem_numeros = avaliacao.estado != ESTADO_PENDENTE or bool(avaliacao.resultados)
    agora = (lambda texto: texto) if tem_numeros else (lambda _texto: "pendente")
    horas_p50 = f"p50 {_segundos(ponta.percentil(avaliacao.horas_ms, 50))}" if avaliacao.horas_ms else "sem amostras"
    linhas += [
        f"| intenção (e projeto) à primeira | {LINHA_DE_BASE['intencao']} | {agora(_percentagem(avaliacao.acerto))} "
        f"| >= {META_INTENCAO:.0%} |",
        f"| comandos locais percebidos | {LINHA_DE_BASE['locais']} | {agora(_percentagem(avaliacao.locais))} | — |",
        f"| horas, fala → resposta falada | {LINHA_DE_BASE['horas']} | {agora(horas_p50)} "
        f"| p50 <= {_segundos(ponta.LIMITE_HORAS_P50_MS)} |",
        "",
    ]
    if avaliacao.estado == ESTADO_PENDENTE and not avaliacao.resultados:
        return "\n".join(linhas)

    assert sessao is not None
    linhas += ["## Condições", ""]
    feitas = len(avaliacao.resultados)
    linhas += [
        f"- Sessão criada em {sessao.criada or '—'}; guião `tests/voz/guiao-aceitacao.md`.",
        f"- Tarefas feitas: {feitas} de {avaliacao.total}; saltadas ou por fazer: "
        + (", ".join(f"{i} ({m})" for i, m in avaliacao.saltadas) if avaliacao.saltadas else "nenhuma")
        + ".",
        f"- Língua do guião: {sessao.lingua}; língua do jarvis no log: {', '.join(avaliacao.linguas) or '—'}.",
        "- Só contam frases do microfone (voz real); os segmentos do log com ficheiros WAV ficam de fora.",
        *[f"- Nota: {nota}" for nota in avaliacao.notas],
        "",
        "## Metas",
        "",
        "| medida | obtido | meta | cumpre |",
        "|---|---|---|---|",
        f"| intenção e projeto à primeira | {_percentagem(avaliacao.acerto)} | >= {META_INTENCAO:.0%} "
        f"| {_cumpre(_fracao(avaliacao.acerto) >= META_INTENCAO)} |",
        f"| ditados aceites sem correção | {_percentagem(avaliacao.aceites)} | >= {META_ACEITES:.0%} "
        f"| {_cumpre(bool(avaliacao.aceites[1]) and _fracao(avaliacao.aceites) >= META_ACEITES)} |",
        f"| pedidos inventados (marcados pelo Sponsor) | {avaliacao.inventados[0]} em {avaliacao.inventados[1]} "
        f"| {META_INVENTADOS} | {_cumpre(avaliacao.inventados[0] <= META_INVENTADOS)} |",
    ]
    for nome, valores, limite_p50, limite_p95 in (
        ("horas → início da resposta falada", avaliacao.horas_ms, ponta.LIMITE_HORAS_P50_MS, ponta.LIMITE_HORAS_P95_MS),
        ("ditado → início do recap falado", avaliacao.recap_ms, ponta.LIMITE_RECAP_P50_MS, ponta.LIMITE_RECAP_P95_MS),
    ):
        ok = bool(valores) and ponta.percentil(valores, 50) <= limite_p50 and ponta.percentil(valores, 95) <= limite_p95
        linhas.append(
            f"| {nome} | {_estatistica(valores)} | p50 <= {limite_p50:.0f} ms, p95 <= {limite_p95:.0f} ms | {_cumpre(ok)} |"
        )
    sinal_ok = bool(avaliacao.sinal_ms) and max(avaliacao.sinal_ms) <= ponta.LIMITE_SINAL_DE_VIDA_MS
    maximo = f" · máximo {max(avaliacao.sinal_ms):.0f} ms" if avaliacao.sinal_ms else ""
    linhas += [
        f"| primeiro sinal de vida | {_estatistica(avaliacao.sinal_ms)}{maximo} "
        f"| máximo <= {ponta.LIMITE_SINAL_DE_VIDA_MS:.0f} ms | {_cumpre(sinal_ok)} |",
        "",
        "## Cobertura dos casos de uso",
        "",
        "| caso | tarefas feitas | cenários passados | cumpre (>= 1) |",
        "|---|---|---|---|",
        *[
            f"| ({caso}) | {feitas_} | {passadas} | {_cumpre(passadas >= 1)} |"
            for caso, (passadas, feitas_) in avaliacao.cobertura.items()
        ],
        "",
        "## Tarefa a tarefa",
        "",
        "| id | caso | fluxo | intenção esperada → obtida | projeto | à primeira | aceite sem correção "
        "| inventou | fez o pedido | fluxo no log | passou |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in avaliacao.resultados:
        linhas.append(
            f"| {r.tarefa.id} | {r.tarefa.caso} | {r.tarefa.fluxo} | {r.tarefa.intencao} → {r.intencao_obtida or '—'} "
            f"| {r.projeto_obtido} | {_sim_nao(r.acertou)} | {_sim_nao(r.aceite_sem_correcao)} "
            f"| {_sim_nao(r.registo.inventou)} | {_sim_nao(r.registo.fez_o_pedido)} "
            f"| {'completo' if r.fluxo_no_log else r.falta_no_log} | {_sim_nao(r.passou)} |"
        )
    linhas += ["", f"Resultado: **{avaliacao.estado}**", *[f"- {falha}" for falha in avaliacao.falhas], ""]
    return "\n".join(linhas)


def escrever_evidencia(avaliacao: Avaliacao, caminho: Path) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto_da_evidencia(avaliacao), encoding="utf-8")
    return caminho


# --- A sessao guiada ---------------------------------------------------------------

ENTER = ""


def tecla_do_teclado(pergunta: str, validas: Sequence[str]) -> str:
    """Le uma tecla de `validas` ('' e o Enter). Sem msvcrt (fora do Windows), le uma linha."""
    print(pergunta, end=" ", flush=True)
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    while True:
        if msvcrt is not None:
            carater = msvcrt.getwch()
            if carater in ("\x00", "\xe0"):
                msvcrt.getwch()  # tecla especial (setas, F1...): ignora as duas partes
                continue
            if carater == "\x03":
                raise KeyboardInterrupt
            carater = ENTER if carater in ("\r", "\n") else carater.lower()
        else:
            carater = (input() or "").strip().lower()[:1]
        if carater in validas:
            print(carater or "Enter")
            return carater


def correr_sessao(
    sessao: Sessao,
    tarefas: Sequence[Tarefa],
    caminho: Path,
    *,
    tecla: Callable[[str, Sequence[str]], str] = tecla_do_teclado,
    escrever: Callable[[str], object] = print,
    agora: Callable[[], datetime.datetime] = datetime.datetime.now,
    frases_na_janela: Callable[[str, str], int] | None = None,
) -> bool:
    """Guia as tarefas por fazer. Guarda depois de cada uma. True quando nao falta nenhuma."""
    por_fazer = [t for t in tarefas if sessao.registo(t.id) is None]
    feitas_antes = len(tarefas) - len(por_fazer)
    for indice, tarefa in enumerate(por_fazer, start=feitas_antes + 1):
        if tarefa.pede_projeto_teste and MARCADOR_TESTE not in sessao.projetos:
            sessao.tarefas.append(RegistoDaTarefa(tarefa.id, SALTADA, motivo="sem --projeto-teste"))
            guardar_sessao(sessao, caminho)
            escrever(f"[{indice}/{len(tarefas)}] {tarefa.id}: saltada (sem --projeto-teste)")
            continue
        escrever("")
        escrever(f"[{indice}/{len(tarefas)}] {tarefa.id} — caso ({tarefa.caso})")
        escrever(f"   {trocar_marcadores(tarefa.tarefa, sessao.projetos)}")
        escrever(f"   exemplo: \"{trocar_marcadores(tarefa.exemplos[sessao.lingua], sessao.projetos)}\"")
        while True:
            escolha = tecla("   Enter para começar (p salta, q guarda e sai):", (ENTER, "p", "q"))
            if escolha == "q":
                guardar_sessao(sessao, caminho)
                return False
            if escolha == "p":
                sessao.tarefas.append(RegistoDaTarefa(tarefa.id, SALTADA, motivo="saltada pelo Sponsor"))
                guardar_sessao(sessao, caminho)
                break
            inicio = agora_iso(agora())
            escrever("   Depois do recap, responde logo, sem \"hey jarvis\" nem tecla.")
            escolha = tecla("   Faz a tarefa no jarvis. Enter quando ele acabar (q sai sem contar esta):", (ENTER, "q"))
            if escolha == "q":
                guardar_sessao(sessao, caminho)
                return False
            fim = agora_iso(agora())
            if frases_na_janela is not None and frases_na_janela(inicio, fim) == 0:
                # Nada ouvido: nada se perde ao repetir (o jarvis estava fechado?).
                escrever("   AVISO: o log do jarvis não tem nenhuma frase do microfone nesta tarefa.")
                escrever("   Confirma que o jarvis está aberto e a ouvir, e repete a tarefa.")
                if tecla("   r repete a tarefa, Enter segue assim:", ("r", ENTER)) == "r":
                    continue
            registo = RegistoDaTarefa(tarefa.id, FEITA, inicio=inicio, fim=fim)
            registo.fez_o_pedido = tecla("   O jarvis fez o que pediste? (s/n)", ("s", "n")) == "s"
            if tarefa.pergunta_inventados:
                registo.inventou = (
                    tecla("   O que foi enviado tinha algum pedido que NÃO fizeste? (s/n)", ("s", "n")) == "s"
                )
            sessao.tarefas.append(registo)
            guardar_sessao(sessao, caminho)
            break
    sessao.concluida = True
    guardar_sessao(sessao, caminho)
    return True


# --- Linha de comandos -------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/aceitacao_sponsor.py",
        description=(
            "Aceitacao do jarvis com a voz real do Sponsor: guia 15 min de tarefas, le o log do jarvis e "
            "escreve a evidencia. Nunca toca som nem abre o microfone."
        ),
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--continuar", action="store_true", help="retoma a ultima sessao por acabar")
    modo.add_argument("--relatorio", action="store_true", help="so le a ultima sessao e reescreve a evidencia")
    modo.add_argument("--autoteste", action="store_true", help="regressao com logs falsos (sem hardware)")
    parser.add_argument("--projeto-teste", metavar="NOME", help="projeto do config.toml para as tarefas do caso (c)")
    parser.add_argument("--lingua", choices=LINGUAS, default=None, help="exemplos do guiao (por omissao a do config.toml)")
    parser.add_argument("--sessao", metavar="FICHEIRO", help="com --relatorio: a sessao a ler (por omissao a ultima)")
    parser.add_argument(
        "--evidencia",
        metavar="FICHEIRO",
        help="onde escrever a evidencia (dentro de docs/forja/evidence/; por omissao aceitacao-sponsor-<data>.md)",
    )
    return parser


def _caminho_da_evidencia(valor: str | None) -> Path:
    if valor:
        return caminho_evidencia_de_saida(valor)
    return caminho_evidencia_de_saida(PASTA_EVIDENCIA / f"aceitacao-sponsor-{datetime.datetime.now():%Y%m%d-%H%M%S}.md")


def relatorio(sessao_path: Path | None, tarefas: Sequence[Tarefa], evidencia: Path, pasta_logs: Path = PASTA_LOGS) -> Avaliacao:
    sessao = ler_sessao(sessao_path) if sessao_path is not None else None
    frases, eventos = ler_logs(logs_da_sessao(sessao, pasta_logs)) if sessao is not None else ([], [])
    avaliacao = avaliar_sessao(sessao, tarefas, frases, eventos)
    escrever_evidencia(avaliacao, evidencia)
    return avaliacao


def _log_de_hoje(pasta: Path = PASTA_LOGS) -> list[str]:
    caminho = caminho_do_log(pasta=pasta)
    if not caminho.is_file():
        return []
    return caminho.read_text(encoding="utf-8", errors="replace").splitlines()


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    try:
        tarefas = ler_guiao()
    except (OSError, ErroDoGuiao) as erro:
        print(f"ERRO no guiao: {erro}")
        return 2
    problemas = verificar_cobertura_do_guiao(tarefas)
    if problemas:
        print("ERRO: o guiao nao cobre o que a aceitacao mede: " + "; ".join(problemas))
        return 2
    try:
        evidencia = _caminho_da_evidencia(args.evidencia)
    except ValueError as erro:
        print(f"ERRO: {erro}")
        return 2

    if args.relatorio:
        caminho = Path(args.sessao) if args.sessao else ultima_sessao()
        try:
            avaliacao = relatorio(caminho, tarefas, evidencia)
        except ErroDaSessao as erro:
            print(f"ERRO: {erro}")
            return 2
        print(f"{avaliacao.estado} | evidencia: {caminho_para_mostrar(evidencia)}")
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
            config = carregar_config(CAMINHO_CONFIG_PADRAO)
            projetos = nomes_dos_marcadores([p.nome for p in config.projetos], args.projeto_teste)
        except (ConfigError, ErroDaSessao) as erro:
            print(f"ERRO: {erro}")
            return 2
        lingua = args.lingua or ("en" if config.ouvido.lingua == "en" else "pt")
        agora = datetime.datetime.now()
        sessao = Sessao(lingua=lingua, projetos=projetos, criada=f"{agora:%Y-%m-%d %H:%M}")
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
        frases, _ = ler_log(_log_de_hoje())
        return sum(
            1
            for f in frases
            if f.fonte == FONTE_MICROFONE and ler_instante(inicio) <= f.instante <= ler_instante(fim)
        )

    print(f"Sessao de aceitacao | guiao: {len(tarefas)} tarefas | lingua dos exemplos: {sessao.lingua}")
    print(f"Registo da sessao: {caminho_para_mostrar(caminho)} (pasta ignorada pelo Git)")
    acabou = correr_sessao(sessao, tarefas, caminho, frases_na_janela=frases_na_janela)
    if not acabou:
        print("\nSessao guardada. Para continuar: .venv\\Scripts\\python scripts/aceitacao_sponsor.py --continuar")
    avaliacao = relatorio(caminho, tarefas, evidencia)
    print(f"\n{avaliacao.estado} | evidencia: {caminho_para_mostrar(evidencia)}")
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

    def arranque(self, *, lingua: str = "en", microfone: bool = True) -> None:
        self._numero = 0
        self.linha("jarvis a arrancar | log em logs/jarvis-teste.log")
        self.linha(f"configuracao: 2 projeto(s) | lingua={lingua} | motor=motor-falso (cpu) | voz=ligada | forja=configurada")
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
        na_conversa: bool = False,
        sinal_ms: float = 300.0,
        fala_ms: float | None = 900.0,
    ) -> None:
        self._numero += 1
        n = self._numero
        self.linha(formatar_etapa(n, 1, 1000.0, "tecla de falar | audio 1.00 s"))
        self.linha(formatar_etapa(n, 2, 200.0, "motor=motor-falso inferencia=200 ms | texto: 'frase ficticia'"))
        self.linha(f"frase #{n} | primeiro sinal de vida: {sinal_ms:.0f} ms desde o fim da fala (linha A PENSAR)")
        if resposta_ao_recap:
            detalhe = "resposta ao recap pendente"
        else:
            detalhe = Jarvis._detalhe_da_interpretacao(
                Interpretacao("frase ficticia", intencao or "desconhecido", projeto, "", "llm", "ficticio")
            )
            if na_conversa:
                detalhe = "conversa: resposta literal na janela | " + detalhe
        self.linha(formatar_etapa(n, 3, 400.0, detalhe))
        if fala_ms is not None:
            self.linha(f"frase #{n} | inicio da resposta falada: {fala_ms:.0f} ms desde o fim da fala")
        self.linha(f"frase #{n} | desfecho: {desfecho} | {motivo}")
        self.linha(f"frase #{n} | TOTAL |    1500 ms desde o inicio da escuta | 500 ms desde o fim da fala | desfecho: {desfecho}")

    def entregue(self, projeto: str) -> None:
        self.linha(f"canal | prompt confirmado entregue ao canal do {projeto}: 'pedido ficticio'")

    def pergunta(self, projeto: str) -> None:
        self.linha(f"conversa | o {projeto} fez uma pergunta: janela de 8 s a ouvir sem palavra de ativacao")


def escrever_tarefa_falsa(
    log: EscritorDeLogFalso, tarefa: Tarefa, projetos: dict[str, str], *, horas_ms: float = 900.0, recap_ms: float = 1800.0
) -> None:
    """As frases que o jarvis escreve quando a tarefa corre como o guiao pede."""
    projeto = projetos.get(tarefa.projeto) if tarefa.projeto else None
    fala = horas_ms if tarefa.intencao == "horas" else recap_ms
    if tarefa.fluxo == "imediato":
        log.frase(tarefa.intencao, projeto, fala_ms=fala)
        return
    log.frase(tarefa.intencao, projeto, desfecho="pendente", motivo="a espera de confirmacao", fala_ms=fala)
    log.avancar(3)
    if tarefa.fluxo == "cancelar":
        log.frase(None, resposta_ao_recap=True, desfecho="cancelado", motivo="cancelado pelo utilizador")
        return
    if tarefa.fluxo in ("corrigir", "projeto"):
        log.frase(None, resposta_ao_recap=True, desfecho="pendente", motivo="a espera de confirmacao")
        log.avancar(3)
        projeto = projeto or projetos["<projeto-1>"]
    log.frase(None, resposta_ao_recap=True, desfecho="executado", motivo="confirmado")
    if tarefa.intencao == "ditar_prompt":
        log.entregue(projeto)
    if tarefa.fluxo == "conversa":
        log.avancar(5)
        log.pergunta(projeto)
        log.avancar(2)
        log.frase("conversa", projeto, na_conversa=True, desfecho="pendente", motivo="a espera de confirmacao")
        log.avancar(2)
        log.frase(None, resposta_ao_recap=True, desfecho="executado", motivo="confirmado")
        log.entregue(projeto)


def sessao_falsa(
    tarefas: Sequence[Tarefa],
    projetos: dict[str, str],
    inicio: datetime.datetime,
    *,
    origem: str = ORIGEM_SPONSOR,
    microfone: bool = True,
    horas_ms: float = 900.0,
    recap_ms: float = 1800.0,
) -> tuple[Sessao, list[str]]:
    """Uma sessao completa em que tudo corre como o guiao pede, e o log correspondente."""
    log = EscritorDeLogFalso(inicio)
    log.arranque(microfone=microfone)
    sessao = Sessao(lingua="en", projetos=dict(projetos), origem=origem, criada=f"{inicio:%Y-%m-%d %H:%M}", concluida=True)
    for tarefa in tarefas:
        log.avancar(10)
        registo = RegistoDaTarefa(tarefa.id, FEITA, inicio=agora_iso(log.agora))
        log.avancar(2)
        escrever_tarefa_falsa(log, tarefa, projetos, horas_ms=horas_ms, recap_ms=recap_ms)
        log.avancar(2)
        registo.fim = agora_iso(log.agora)
        registo.fez_o_pedido = True
        registo.inventou = False if tarefa.pergunta_inventados else None
        sessao.tarefas.append(registo)
    return sessao, log.linhas


PROJETOS_FICTICIOS = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois", MARCADOR_TESTE: "exemplo-tres"}


def _autoteste() -> int:
    import contextlib
    import io

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    tarefas = ler_guiao()
    verificar("guiao cobre (a)-(d), horas, ditados e fluxos", verificar_cobertura_do_guiao(tarefas), [])
    inicio = datetime.datetime(2026, 9, 25, 10, 0, 0)

    # 1. Tudo como o guiao pede: CUMPRIDA, e nada privado na evidencia.
    sessao, linhas = sessao_falsa(tarefas, PROJETOS_FICTICIOS, inicio)
    frases, eventos = ler_log(linhas)
    avaliacao = avaliar_sessao(sessao, tarefas, frases, eventos)
    verificar("sessao perfeita: CUMPRIDA", (avaliacao.estado, avaliacao.falhas), (ESTADO_CUMPRIDA, []))
    verificar("acerto 100%", avaliacao.acerto, (len(tarefas), len(tarefas)))
    verificar("cobertura: um cenario passado em cada caso", all(p >= 1 for p, _ in avaliacao.cobertura.values()), True)
    texto = texto_da_evidencia(avaliacao)
    verificar("evidencia sem nomes de projetos", any(n in texto for n in PROJETOS_FICTICIOS.values()), False)
    verificar("evidencia sem texto das frases", "ficticia" in texto or "ficticio" in texto, False)
    verificar("evidencia compara com a linha de base", LINHA_DE_BASE["intencao"] in texto, True)

    # 2. Sem sessao, ou sessao que nao e do Sponsor: PENDENTE com o passo exato.
    pendente = avaliar_sessao(None, tarefas, [], [])
    texto = texto_da_evidencia(pendente)
    verificar("sem sessao: PENDENTE", pendente.estado, ESTADO_PENDENTE)
    verificar("PENDENTE lista o passo exato", PASSO_DO_SPONSOR[1] in texto and "cumpre" not in texto, True)
    falsa, linhas_falsas = sessao_falsa(tarefas, PROJETOS_FICTICIOS, inicio, origem=ORIGEM_AUTOTESTE)
    verificar(
        "sessao de autoteste nunca conta",
        avaliar_sessao(falsa, tarefas, *ler_log(linhas_falsas)).estado,
        ESTADO_PENDENTE,
    )

    # 3. Frases de ficheiros WAV (voz sintetica) ficam de fora.
    sessao_wav, linhas_wav = sessao_falsa(tarefas, PROJETOS_FICTICIOS, inicio, microfone=False)
    verificar("jarvis com --wav: PENDENTE", avaliar_sessao(sessao_wav, tarefas, *ler_log(linhas_wav)).estado, ESTADO_PENDENTE)

    # 4. Lento: as metas de latencia falham.
    sessao_lenta, linhas_lentas = sessao_falsa(tarefas, PROJETOS_FICTICIOS, inicio, horas_ms=5300.0, recap_ms=4200.0)
    lenta = avaliar_sessao(sessao_lenta, tarefas, *ler_log(linhas_lentas))
    verificar(
        "horas a 5,3 s e recap a 4,2 s: NAO CUMPRIDA",
        (lenta.estado, sum("p50" in f for f in lenta.falhas)),
        (ESTADO_NAO_CUMPRIDA, 2),
    )

    # 5. Pedido inventado marcado pelo Sponsor: falha a meta de zero.
    sessao.tarefas[1].inventou = True
    inventou = avaliar_sessao(sessao, tarefas, frases, eventos)
    verificar("um pedido inventado: NAO CUMPRIDA", (inventou.estado, inventou.inventados[0]), (ESTADO_NAO_CUMPRIDA, 1))
    sessao.tarefas[1].inventou = False

    # 6. A sessao guiada guarda janelas e respostas, e o q guarda e sai.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "sessao-teste.json"
        nova = Sessao(lingua="en", projetos=dict(PROJETOS_FICTICIOS))
        relogio = iter(inicio + datetime.timedelta(seconds=s) for s in range(0, 1000, 5))
        teclas = iter([ENTER, ENTER, "s", ENTER, ENTER, "n", "n", "q"])
        with contextlib.redirect_stdout(io.StringIO()):
            acabou = correr_sessao(
                nova, tarefas, caminho, tecla=lambda _p, _v: next(teclas), escrever=lambda _t: None,
                agora=lambda: next(relogio),
            )
        guardada = ler_sessao(caminho)
        verificar("q guarda e sai", (acabou, [r.id for r in guardada.tarefas]), (False, [tarefas[0].id, tarefas[1].id]))
        verificar(
            "respostas s/n registadas",
            [(r.fez_o_pedido, r.inventou) for r in guardada.tarefas],
            [(True, None), (False, False)],
        )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste da aceitacao completo (logs falsos, sem hardware, sem som).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
