r"""Mede o fim de turno nas gravacoes reais do Sponsor: cortes a meio e espera no fim.

Compara os dois metodos de `jarvis.fim_de_turno` sobre as MESMAS gravacoes do
microfone (por omissao recordings/en e recordings/treino-en, ignoradas pelo
Git; so as que o manifesto diz que vieram do microfone e que passam a
validacao do gravador):

  silencio     o silencio fixo de sempre (0,6 s);
  smart-turn   o Smart Turn depois de 0,2 s de silencio: acabada fecha aos
               0,3 s, inacabada espera ate ao maximo (1,5 s por omissao).

Cada gravacao passa pelo mesmo caminho do ouvido: chunks de 30 ms, o VAD
webrtc do ouvido, a fala comeca com 3 chunks seguidos de voz e cada chunk da
frase vai a `FimDeTurno.acabou`. Quando o detetor da palavra de ativacao do
ouvido (openWakeWord, limiar do config.toml) a encontra, a gravacao segue o
caminho das maos-livres: a frase so comeca depois da ativacao e dos 0,25 s
de surdez, e a pausa depois de "hey jarvis," nao e uma pausa da frase. Sem
ativacao segue o caminho da janela sem palavra de ativacao. Como as gravacoes acabam logo a seguir a
fala, junta-se silencio digital no fim (mais do que a espera maxima). O
modelo corre no proprio chunk e o veredicto so vale depois do tempo que a
inferencia levou, como no ouvido, onde corre numa thread a parte.

Cada gravacao e UMA frase do guiao, por isso:

  corte a meio   cada frase a mais que o ouvido faria (o turno fechou numa
                 pausa e a fala continuou depois);
  espera         da ultima voz (VAD) ao fim do turno da ultima frase, a
                 mesma medida de `Frase.ms_da_ultima_voz_ao_fim`.

Decisao: o Smart Turn so fica ligado por omissao se tiver MENOS cortes do
que o silencio fixo e uma espera p50 que NAO seja maior. A evidencia vai
para docs/forja/evidence/ (ignorada) e leva so ids e numeros; nunca audio
nem texto. Sem gravacoes validas diz "PENDENTE — passo do Sponsor"; sem o
ficheiro do modelo so mede o silencio fixo e tambem nao decide.

Nunca toca som, nunca abre o microfone.

Uso:
    .venv\Scripts\python scripts/avaliar_fim_de_turno.py [--pasta recordings/en ...] [--maximo 1.5]
    .venv\Scripts\python scripts/avaliar_fim_de_turno.py --autoteste
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import caminho_evidencia_de_saida, caminho_para_mostrar, ler_wav_pcm16  # noqa: E402
from jarvis.config import FIM_DE_TURNO_MAXIMO_S, LIMIAR_DE_ATIVACAO_PADRAO  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.fim_de_turno import (  # noqa: E402
    MODELO_SMART_TURN,
    METODO_SILENCIO,
    METODO_SMART_TURN,
    FimDeTurno,
    JuizSmartTurn,
)
from jarvis.ouvido import (  # noqa: E402
    BYTES_POR_CHUNK,
    ESPERA_PELA_FALA_S,
    SURDEZ_APOS_ATIVACAO_S,
    CHUNKS_ANTES_DA_FALA,
    CHUNKS_DO_FIM_DE_TURNO,
    CHUNKS_PARA_COMECAR_A_FALA,
    DURACAO_DO_CHUNK_S,
    TAXA,
    chunks_do_pcm,
    percentil,
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

PASTAS_POR_OMISSAO = ("recordings/en", "recordings/treino-en")
#: Silencio junto ao fim de cada gravacao: sempre mais do que a espera maxima.
FOLGA_DEPOIS_DO_MAXIMO_S = 0.5

ESTADO_PENDENTE = "PENDENTE — passo do Sponsor"
ESTADO_LIGAR = "SMART TURN LIGADO POR OMISSAO"
ESTADO_NAO_LIGAR = "SMART TURN DESLIGADO POR OMISSAO"


# --- Gravacoes ---------------------------------------------------------------------


def gravacoes_validas(pasta: Path) -> tuple[list[tuple[str, Path]], dict[str, str]]:
    """(id, WAV) das gravacoes do microfone que passam a validacao, e id -> motivo das outras."""
    manifesto = gravar_voz.ler_manifesto(pasta)
    invalidas = gravar_voz.gravacoes_invalidas(pasta, manifesto)
    registos = manifesto.get("gravacoes", {})
    for id_ in sorted(gravar_voz.ja_gravadas(pasta, manifesto) - set(invalidas)):
        origem = registos[id_].get("origem")
        if origem != gravar_voz.ORIGEM_MICROFONE:
            invalidas[id_] = f"origem '{origem}' no manifesto: nao conta como voz do Sponsor"
    validas = sorted(gravar_voz.ja_gravadas(pasta, manifesto) - set(invalidas))
    return [(id_, pasta / str(registos[id_]["ficheiro"])) for id_ in validas], invalidas


def pcm_mono_16k(caminho: Path) -> bytes:
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if taxa != TAXA or canais != 1:
        raise ValueError(f"{caminho.name}: esperava 16 kHz mono (o gravador escreve assim)")
    return dados


# --- Simulacao do ouvido -----------------------------------------------------------


@dataclass(frozen=True)
class FimSimulado:
    """Um fim de turno: o chunk que fechou a frase e o ultimo chunk com voz dela."""

    chunk: int
    ultima_voz: int
    motivo: str

    @property
    def espera_s(self) -> float:
        return (self.chunk - self.ultima_voz) * DURACAO_DO_CHUNK_S


def indice_da_ativacao(chunks: Sequence[bytes], detetor, limiar: float) -> int | None:
    """O chunk em que o detetor do ouvido chega ao limiar, ou None."""
    detetor.reiniciar()
    for i, chunk in enumerate(chunks):
        if detetor.processar(chunk) >= limiar:
            return i
    return None


def simular(
    chunks: Sequence[bytes], voz: Sequence[bool], fim: FimDeTurno, ativacao: int | None = None
) -> list[FimSimulado]:
    """Os fins de turno que o ouvido daria a estes chunks (uma frase por fim).

    Sem `ativacao` (a janela sem palavra de ativacao), 3 chunks seguidos com
    voz comecam a frase, com os 300 ms antes. Com `ativacao` (o chunk em que
    a palavra foi detetada), como nas maos-livres: o audio conta a partir do
    chunk seguinte e a voz so comeca a frase depois da surdez; sem fala em
    5 s nao ha frase. Na frase, o silencio conta chunk a chunk e o fim de
    turno decide. Depois de um fim, fala que volte conta como frase nova.
    """
    fins: list[FimSimulado] = []
    a_falar = False
    seguidos = 0
    inicio = 0
    silencio = 0.0
    ultima_voz = 0
    for i, fala in enumerate(voz):
        if ativacao is not None and not fins and not a_falar:
            aguardado = (i - ativacao) * DURACAO_DO_CHUNK_S
            if aguardado <= 0:
                continue
            if aguardado > ESPERA_PELA_FALA_S:
                return fins
            seguidos = seguidos + 1 if fala and aguardado > SURDEZ_APOS_ATIVACAO_S else 0
            if seguidos >= CHUNKS_PARA_COMECAR_A_FALA:
                a_falar = True
                inicio = ativacao + 1
                silencio = 0.0
                ultima_voz = i
                fim.reiniciar()
            continue
        if not a_falar:
            seguidos = seguidos + 1 if fala else 0
            if seguidos >= CHUNKS_PARA_COMECAR_A_FALA:
                a_falar = True
                inicio = max(0, i + 1 - seguidos - CHUNKS_ANTES_DA_FALA)
                silencio = 0.0
                ultima_voz = i
                fim.reiniciar()
            continue
        if fala:
            ultima_voz = i
        silencio = 0.0 if fala else silencio + DURACAO_DO_CHUNK_S

        def audio(ate: int = i) -> bytes:
            return b"".join(chunks[max(inicio, ate + 1 - CHUNKS_DO_FIM_DE_TURNO) : ate + 1])

        motivo = fim.acabou(silencio, audio)
        if motivo is not None:
            fins.append(FimSimulado(i, ultima_voz, motivo))
            fim.reiniciar()
            a_falar = False
            seguidos = 0
    return fins


@dataclass
class ResultadoDoMetodo:
    """Os numeros de um metodo sobre todas as gravacoes."""

    metodo: str
    gravacoes: int = 0
    cortes: int = 0
    gravacoes_cortadas: int = 0
    esperas_s: list[float] = field(default_factory=list)
    #: id -> (cortes, espera em s da ultima frase)
    por_gravacao: dict[str, tuple[int, float]] = field(default_factory=dict)

    def juntar(self, id_: str, fins: list[FimSimulado]) -> None:
        if not fins:
            return
        cortes = len(fins) - 1
        espera = fins[-1].espera_s
        self.gravacoes += 1
        self.cortes += cortes
        self.gravacoes_cortadas += 1 if cortes else 0
        self.esperas_s.append(espera)
        self.por_gravacao[id_] = (cortes, espera)

    def p(self, percentagem: float) -> float | None:
        return percentil(self.esperas_s, percentagem) if self.esperas_s else None

    @property
    def media_s(self) -> float | None:
        return sum(self.esperas_s) / len(self.esperas_s) if self.esperas_s else None


@dataclass
class Medicao:
    pastas: list[Path]
    maximo_s: float
    fixo: ResultadoDoMetodo
    smart: ResultadoDoMetodo | None
    sem_fala: list[str]
    invalidas: dict[str, str]
    modelo_em_falta: str | None = None
    #: Ids que seguiram o caminho das maos-livres (palavra de ativacao detetada).
    com_ativacao: list[str] = field(default_factory=list)
    detetor_em_falta: str | None = None
    inferencias_ms: list[float] = field(default_factory=list)
    #: id -> probabilidade do Smart Turn no fim da ultima frase.
    probabilidades_no_fim: dict[str, float] = field(default_factory=dict)

    @property
    def estado(self) -> str:
        if not self.fixo.gravacoes or self.smart is None:
            return ESTADO_PENDENTE
        return ESTADO_LIGAR if self.smart_ganha else ESTADO_NAO_LIGAR

    @property
    def smart_ganha(self) -> bool:
        """Menos cortes a meio e a espera p50 nao maior do que a do silencio fixo."""
        if self.smart is None or not self.smart.esperas_s or not self.fixo.esperas_s:
            return False
        return self.smart.cortes < self.fixo.cortes and self.smart.p(50) <= self.fixo.p(50) + 1e-9

    @property
    def linha_do_config(self) -> str | None:
        if self.estado == ESTADO_PENDENTE:
            return None
        return f'fim_de_turno = "{METODO_SMART_TURN if self.smart_ganha else METODO_SILENCIO}"'


def _voz_da_gravacao(chunks: Sequence[bytes], criar_vad: Callable[[], object]) -> list[bool]:
    vad = criar_vad()
    return [bool(vad.e_fala(chunk)) for chunk in chunks]


def medir(
    pastas: Sequence[Path],
    *,
    maximo_s: float = FIM_DE_TURNO_MAXIMO_S,
    criar_juiz: Callable[[], object] | None,
    criar_vad: Callable[[], object],
    detetor=None,
    limiar_de_ativacao: float = LIMIAR_DE_ATIVACAO_PADRAO,
    modelo_em_falta: str | None = None,
    detetor_em_falta: str | None = None,
    escrever: Callable[[str], object] = print,
) -> Medicao:
    """Corre as gravacoes validas das pastas pelos dois metodos."""
    fixo = ResultadoDoMetodo(METODO_SILENCIO)
    smart = ResultadoDoMetodo(METODO_SMART_TURN) if criar_juiz is not None else None
    juiz = criar_juiz() if criar_juiz is not None else None
    aquecer = getattr(juiz, "aquecer", None)
    if aquecer is not None:
        aquecer()  # como no jarvis: a primeira frase nao paga o arranque do modelo
    medicao = Medicao(
        list(pastas), maximo_s, fixo, smart, [], {}, modelo_em_falta, detetor_em_falta=detetor_em_falta
    )
    silencio_final = b"\x00" * BYTES_POR_CHUNK * math.ceil((maximo_s + FOLGA_DEPOIS_DO_MAXIMO_S) / DURACAO_DO_CHUNK_S)
    for pasta in pastas:
        validas, invalidas = gravacoes_validas(pasta)
        medicao.invalidas.update({f"{pasta.name}/{id_}": motivo for id_, motivo in invalidas.items()})
        for id_, caminho in validas:
            chunks = chunks_do_pcm(pcm_mono_16k(caminho) + silencio_final)
            voz = _voz_da_gravacao(chunks, criar_vad)
            ativacao = None if detetor is None else indice_da_ativacao(chunks, detetor, limiar_de_ativacao)
            if ativacao is not None:
                medicao.com_ativacao.append(id_)
            fins_fixo = simular(chunks, voz, FimDeTurno(None, escrever=escrever), ativacao)
            if not fins_fixo:
                medicao.sem_fala.append(id_)
                continue
            fixo.juntar(id_, fins_fixo)
            percurso = "ativacao" if ativacao is not None else "janela"
            linha = f"  {id_:<10} {percurso:<8} silencio: {len(fins_fixo) - 1} corte(s), espera {fins_fixo[-1].espera_s:.2f} s"
            if smart is not None:
                fim = FimDeTurno(juiz, maximo_s=maximo_s, em_fundo=False, escrever=escrever)
                fins_smart = simular(chunks, voz, fim, ativacao)
                smart.juntar(id_, fins_smart)
                medicao.inferencias_ms.extend(fim.inferencias_ms)
                if fim.probabilidades:
                    medicao.probabilidades_no_fim[id_] = fim.probabilidades[-1]
                if fins_smart:
                    linha += (
                        f" | smart-turn: {len(fins_smart) - 1} corte(s), espera {fins_smart[-1].espera_s:.2f} s"
                        f" ({fins_smart[-1].motivo})"
                    )
            escrever(linha)
    return medicao


# --- Evidencia ---------------------------------------------------------------------


def _s(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.2f} s"


def _linha_do_metodo(nome: str, r: ResultadoDoMetodo) -> str:
    return (
        f"| {nome} | {r.gravacoes} | {r.cortes} | {r.gravacoes_cortadas} | {_s(r.media_s)} | "
        f"{_s(r.p(50))} | {_s(r.p(95))} |"
    )


def texto_da_evidencia(medicao: Medicao, agora: datetime) -> str:
    fixo, smart = medicao.fixo, medicao.smart
    linhas = [
        "# Fim de turno nas gravacoes reais do Sponsor",
        "",
        f"- Data: {agora:%Y-%m-%d %H:%M}",
        f"- Estado: **{medicao.estado}**",
        f"- Pastas: {', '.join(p.name for p in medicao.pastas)} (so gravacoes do microfone, validadas)",
        f"- Smart Turn: 0.2 s de silencio -> modelo; acabada fecha aos 0.3 s; inacabada espera ate {medicao.maximo_s:g} s",
        "- Silencio fixo: 0.6 s",
        "- Corte a meio: cada frase a mais que o ouvido faria numa gravacao de uma so frase.",
        "- Espera: da ultima voz (VAD webrtc) ao fim do turno da ultima frase.",
        f"- Caminho das maos-livres (palavra de ativacao detetada): {len(medicao.com_ativacao)} gravacoes; "
        "as outras seguem o caminho da janela sem palavra de ativacao.",
        "",
    ]
    if medicao.detetor_em_falta:
        linhas += [f"Detetor da palavra de ativacao por carregar: {medicao.detetor_em_falta}. Todas seguem a janela.", ""]
    if medicao.modelo_em_falta:
        linhas += [f"Modelo Smart Turn em falta: {medicao.modelo_em_falta}. So o silencio fixo foi medido.", ""]
    if not fixo.gravacoes:
        linhas += ["Nenhuma gravacao valida do microfone: o Sponsor grava primeiro (scripts/gravar_voz.py --lingua en).", ""]
    linhas += [
        "| Metodo | Gravacoes | Cortes a meio | Gravacoes cortadas | Espera media | Espera p50 | Espera p95 |",
        "|---|---|---|---|---|---|---|",
        _linha_do_metodo(METODO_SILENCIO, fixo),
    ]
    if smart is not None:
        linhas.append(_linha_do_metodo(METODO_SMART_TURN, smart))
    linhas.append("")
    if medicao.inferencias_ms:
        linhas += [
            f"Inferencia do Smart Turn (CPU, 1 fio, com as caracteristicas): p50 "
            f"{percentil(medicao.inferencias_ms, 50):.0f} ms, p95 {percentil(medicao.inferencias_ms, 95):.0f} ms "
            f"em {len(medicao.inferencias_ms)} chamadas.",
            "",
        ]
    if medicao.estado != ESTADO_PENDENTE:
        regra = "menos cortes a meio e espera p50 nao maior"
        linhas += [
            f"Regra: o Smart Turn so fica ligado por omissao com {regra}. "
            f"Resultado: {'cumpre' if medicao.smart_ganha else 'nao cumpre'}.",
            "",
            f"Linha para [ouvido] no config.toml: `{medicao.linha_do_config}`",
            "",
        ]
    if fixo.por_gravacao:
        linhas += ["## Por gravacao", "", "| Id | Silencio: cortes | Silencio: espera | Smart Turn: cortes | Smart Turn: espera | p(fim) |", "|---|---|---|---|---|---|"]
        for id_, (cortes, espera) in fixo.por_gravacao.items():
            if smart is not None and id_ in smart.por_gravacao:
                s_cortes, s_espera = smart.por_gravacao[id_]
                probabilidade = medicao.probabilidades_no_fim.get(id_)
                extra = f"{s_cortes} | {espera_txt(s_espera)} | {'—' if probabilidade is None else f'{probabilidade:.2f}'}"
            else:
                extra = "— | — | —"
            linhas.append(f"| {id_} | {cortes} | {espera_txt(espera)} | {extra} |")
        linhas.append("")
    if medicao.sem_fala:
        linhas += [f"Sem fala detetada (fora das contas): {', '.join(medicao.sem_fala)}", ""]
    if medicao.invalidas:
        linhas += ["Postas de parte:", ""]
        linhas += [f"- {id_}: {motivo}" for id_, motivo in sorted(medicao.invalidas.items())]
        linhas.append("")
    return "\n".join(linhas)


def espera_txt(valor: float) -> str:
    return f"{valor:.2f} s"


def escrever_evidencia(medicao: Medicao, saida: Path, agora: datetime | None = None) -> Path:
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(texto_da_evidencia(medicao, agora or datetime.now()), encoding="utf-8")
    return saida


# --- CLI ---------------------------------------------------------------------------


def _limiar_do_config() -> float:
    """O limiar da palavra de ativacao do config.toml local, ou o de omissao."""
    from jarvis.config import CAMINHO_CONFIG_PADRAO, ConfigError, carregar_config

    try:
        return carregar_config(CAMINHO_CONFIG_PADRAO, validar_caminhos=False).ouvido.limiar_ativacao
    except (ConfigError, OSError):
        return LIMIAR_DE_ATIVACAO_PADRAO


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/avaliar_fim_de_turno.py",
        description=__doc__.splitlines()[0],
        epilog="Nunca toca som nem abre o microfone. Evidencia em docs/forja/evidence/.",
    )
    parser.add_argument("--autoteste", action="store_true", help="gravacoes, VAD e modelo falsos; sem som")
    parser.add_argument(
        "--pasta", action="append", default=None, metavar="PASTA",
        help=f"pasta de gravacoes dentro de recordings/ (repetivel; por omissao {', '.join(PASTAS_POR_OMISSAO)})",
    )
    parser.add_argument("--maximo", type=float, default=FIM_DE_TURNO_MAXIMO_S, help="espera maxima do Smart Turn (s)")
    parser.add_argument("--saida", default=None, help="ficheiro .md dentro de docs/forja/evidence/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    try:
        if args.pasta:
            pastas = [gravar_voz.pasta_de_gravacoes(p) for p in args.pasta]
        else:
            pastas = [gravar_voz.pasta_de_gravacoes(p) for p in PASTAS_POR_OMISSAO if (RAIZ / p).is_dir()]
        carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
        saida = caminho_evidencia_de_saida(args.saida or f"docs/forja/evidence/fim-de-turno-{carimbo}.md")
    except ValueError as erro:
        print(f"ERRO: {erro}")
        return 2
    from jarvis.ouvido import DetetorOpenWakeWord, VadWebRtc, modelo_de_ativacao

    limiar = _limiar_do_config()
    detetor = detetor_em_falta = None
    try:
        detetor = DetetorOpenWakeWord(modelo_de_ativacao("en"))
    except (FileNotFoundError, ImportError) as erro:
        detetor_em_falta = str(erro)
    criar_juiz = None
    em_falta = None
    if MODELO_SMART_TURN.is_file():
        criar_juiz = JuizSmartTurn
    else:
        em_falta = f"models/{MODELO_SMART_TURN.parent.name}/{MODELO_SMART_TURN.name} (ver docs/MODELOS.md)"
    medicao = medir(
        pastas,
        maximo_s=args.maximo,
        criar_juiz=criar_juiz,
        criar_vad=VadWebRtc,
        detetor=detetor,
        limiar_de_ativacao=limiar,
        modelo_em_falta=em_falta,
        detetor_em_falta=detetor_em_falta,
    )
    escrever_evidencia(medicao, saida)
    print()
    for r in (medicao.fixo, medicao.smart):
        if r is not None:
            print(
                f"{r.metodo:<11} {r.gravacoes} gravacoes | cortes a meio {r.cortes} ({r.gravacoes_cortadas} gravacoes) | "
                f"espera media {_s(r.media_s)}, p50 {_s(r.p(50))}, p95 {_s(r.p(95))}"
            )
    print(medicao.estado)
    if medicao.linha_do_config:
        print(f"[ouvido] {medicao.linha_do_config}")
    print(f"evidencia: {caminho_para_mostrar(saida)}")
    return 0


# --- Autoteste (gravacoes sinteticas, VAD e juiz falsos; sem som nem modelo) -------

#: Amplitudes das "palavras" sinteticas: a ultima de uma frase acabada e mais forte.
_PALAVRA = 2000
_FIM_DE_FRASE = 3000


class _VadDeEnergia:
    def e_fala(self, chunk: bytes) -> bool:
        import numpy as np

        return float(np.abs(np.frombuffer(chunk, dtype="<i2")).mean()) > 500


#: Amplitude da "palavra de ativacao" sintetica.
_ATIVACAO = 2500


class _DetetorFalso:
    """Deteta a palavra de ativacao no primeiro chunk depois de um troco com a amplitude dela."""

    def __init__(self) -> None:
        self._dentro = False

    def reiniciar(self) -> None:
        self._dentro = False

    def processar(self, chunk: bytes) -> float:
        import numpy as np

        dentro = int(np.abs(np.frombuffer(chunk, dtype="<i2")).max()) == _ATIVACAO
        detetada = self._dentro and not dentro
        self._dentro = dentro
        return 0.9 if detetada else 0.0


class _JuizDaAmplitude:
    """Acabada quando a ultima fala do audio e de fim de frase (ou a palavra de ativacao)."""

    def probabilidade(self, pcm16: bytes) -> float:
        import numpy as np

        amostras = np.abs(np.frombuffer(pcm16, dtype="<i2"))
        falas = amostras[amostras > 500]
        return 0.9 if falas.size and falas[-1] >= _ATIVACAO - 50 else 0.1


def _som(segundos: float, amplitude: int) -> bytes:
    import numpy as np

    n = int(round(segundos * TAXA))
    ruido = np.random.default_rng(n + amplitude).integers(-3, 4, n)
    sinal = ruido if amplitude == 0 else np.full(n, amplitude) * np.where(np.arange(n) % 2, 1, -1)
    return sinal.astype("<i2").tobytes()


def _gravacao(*partes: tuple[float, int]) -> bytes:
    return b"".join(_som(s, a) for s, a in partes)


def _escrever_pasta(pasta: Path, gravacoes: dict[str, bytes], origem: str = "microfone") -> None:
    from jarvis.audio_util import escrever_wav_pcm16

    pasta.mkdir(parents=True, exist_ok=True)
    manifesto = {"versao": 1, "lingua": "en", "gravacoes": {}}
    for id_, pcm in gravacoes.items():
        escrever_wav_pcm16(pasta / f"{id_}.wav", pcm, TAXA)
        duracao = len(pcm) / 2 / TAXA
        manifesto["gravacoes"][id_] = {
            "ficheiro": f"{id_}.wav", "origem": origem, "duracao_s": duracao, "tempo_real_s": duracao + 0.02,
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

    calado = lambda _t: None  # noqa: E731

    # 1. simulacao: uma pausa de 0,45 s a meio de uma frase acabada ali engana o
    # Smart Turn (fecha aos 0,3 s) mas nao o silencio fixo; uma de 0,9 s numa
    # frase inacabada corta o silencio fixo mas nao o Smart Turn.
    engana = _gravacao((0.3, 0), (1.0, _FIM_DE_FRASE), (0.45, 0), (1.0, _FIM_DE_FRASE), (2.0, 0))
    pausa = _gravacao((0.3, 0), (1.0, _PALAVRA), (0.9, 0), (1.0, _FIM_DE_FRASE), (2.0, 0))
    for nome, pcm, esperados in (("engana", engana, (0, 1)), ("pausa", pausa, (1, 0))):
        chunks = chunks_do_pcm(pcm)
        voz = [_VadDeEnergia().e_fala(c) for c in chunks]
        fins_fixo = simular(chunks, voz, FimDeTurno(None, escrever=calado))
        fins_smart = simular(chunks, voz, FimDeTurno(_JuizDaAmplitude(), em_fundo=False, escrever=calado))
        verificar(
            f"simulacao '{nome}': cortes (silencio, smart-turn)",
            (len(fins_fixo) - 1, len(fins_smart) - 1) == esperados,
            (len(fins_fixo) - 1, len(fins_smart) - 1),
        )
        verificar(f"simulacao '{nome}': espera do silencio fixo 0.6 s", abs(fins_fixo[-1].espera_s - 0.6) < 0.031, fins_fixo[-1])
        verificar(f"simulacao '{nome}': espera do smart-turn 0.3 s", abs(fins_smart[-1].espera_s - 0.3) < 0.031, fins_smart[-1])

    # 2. frase inacabada no fim: o Smart Turn espera o maximo.
    inacabada = _gravacao((0.3, 0), (1.0, _PALAVRA), (2.5, 0))
    chunks = chunks_do_pcm(inacabada)
    fins = simular(chunks, [_VadDeEnergia().e_fala(c) for c in chunks], FimDeTurno(_JuizDaAmplitude(), em_fundo=False, escrever=calado))
    verificar("inacabada: espera o maximo de 1.5 s", len(fins) == 1 and abs(fins[0].espera_s - 1.5) < 0.031, fins)

    # 3. "hey jarvis," e uma pausa: nas maos-livres a pausa e antes da frase, nao dentro dela.
    com_palavra = _gravacao((0.3, 0), (0.6, _ATIVACAO), (0.45, 0), (1.0, _FIM_DE_FRASE), (2.0, 0))
    chunks = chunks_do_pcm(com_palavra)
    voz = [_VadDeEnergia().e_fala(c) for c in chunks]
    ativacao = indice_da_ativacao(chunks, _DetetorFalso(), 0.5)
    verificar("ativacao detetada no fim da palavra", ativacao == 30, ativacao)
    na_janela = simular(chunks, voz, FimDeTurno(_JuizDaAmplitude(), em_fundo=False, escrever=calado))
    nas_maos_livres = simular(chunks, voz, FimDeTurno(_JuizDaAmplitude(), em_fundo=False, escrever=calado), ativacao)
    verificar("janela: a pausa depois da palavra corta", len(na_janela) == 2, na_janela)
    verificar("maos-livres: a pausa depois da palavra nao corta", len(nas_maos_livres) == 1, nas_maos_livres)
    so_palavra = chunks_do_pcm(_gravacao((0.3, 0), (0.6, _ATIVACAO), (6.0, 0)))
    verificar(
        "maos-livres: sem fala em 5 s nao ha frase",
        simular(so_palavra, [_VadDeEnergia().e_fala(c) for c in so_palavra], FimDeTurno(None, escrever=calado), 30) == [],
    )

    with tempfile.TemporaryDirectory(prefix="avaliar-fim-de-turno-") as temporaria:
        raiz = Path(temporaria)
        evidencias = raiz / "docs" / "forja" / "evidence"

        # 3. sem gravacoes: PENDENTE, nada decidido.
        vazia = raiz / "recordings" / "en"
        vazia.mkdir(parents=True)
        medicao = medir([vazia], criar_juiz=_JuizDaAmplitude, criar_vad=_VadDeEnergia, escrever=calado)
        verificar("sem gravacoes: PENDENTE", medicao.estado == ESTADO_PENDENTE and medicao.linha_do_config is None)

        # 4. o Smart Turn corta menos e espera menos: ligado por omissao.
        pasta = raiz / "recordings" / "treino-en"
        _escrever_pasta(pasta, {"en-t01": pausa, "en-t02": pausa, "en-t03": engana, "en-t04": com_palavra})
        _escrever_pasta(raiz / "recordings" / "sint", {"s-01": pausa}, origem="sintetico")
        medicao = medir(
            [pasta, raiz / "recordings" / "sint"],
            criar_juiz=_JuizDaAmplitude,
            criar_vad=_VadDeEnergia,
            detetor=_DetetorFalso(),
            limiar_de_ativacao=0.5,
            escrever=calado,
        )
        verificar("so gravacoes do microfone contam", medicao.fixo.gravacoes == 4 and "sint/s-01" in medicao.invalidas, medicao.invalidas)
        verificar("a gravacao com a palavra segue as maos-livres", medicao.com_ativacao == ["en-t04"], medicao.com_ativacao)
        verificar("contas: 2 cortes contra 1", (medicao.fixo.cortes, medicao.smart.cortes) == (2, 1), (medicao.fixo.cortes, medicao.smart.cortes))
        verificar("ganha: ligado por omissao", medicao.estado == ESTADO_LIGAR and medicao.linha_do_config == 'fim_de_turno = "smart-turn"')

        # 5. mais cortes com o Smart Turn: fica desligado.
        mais = raiz / "recordings" / "mais"
        _escrever_pasta(mais, {"m-01": engana, "m-02": engana, "m-03": pausa})
        medicao_pior = medir([mais], criar_juiz=_JuizDaAmplitude, criar_vad=_VadDeEnergia, escrever=calado)
        verificar("perde: desligado por omissao", medicao_pior.estado == ESTADO_NAO_LIGAR and medicao_pior.linha_do_config == 'fim_de_turno = "silencio"')

        # 6. sem modelo: so o silencio fixo, PENDENTE.
        sem_modelo = medir([pasta], criar_juiz=None, criar_vad=_VadDeEnergia, modelo_em_falta="x.onnx", escrever=calado)
        verificar("sem modelo: PENDENTE e so o silencio fixo", sem_modelo.estado == ESTADO_PENDENTE and sem_modelo.smart is None)

        # 7. a evidencia: so ids e numeros, dentro de docs/forja/evidence/.
        saida = caminho_evidencia_de_saida("docs/forja/evidence/fim-de-turno-teste.md", evidencias, raiz)
        texto = escrever_evidencia(medicao, saida, datetime(2026, 1, 1)).read_text(encoding="utf-8")
        verificar("evidencia escrita com a decisao", ESTADO_LIGAR in texto and "en-t03" in texto and 'fim_de_turno = "smart-turn"' in texto)
        verificar("evidencia sem caminhos da maquina", temporaria not in texto and str(RAIZ) not in texto)
        try:
            caminho_evidencia_de_saida("docs/notas.md", evidencias, raiz)
            verificar("evidencia fora de docs/forja/evidence/ recusada", False)
        except ValueError:
            verificar("evidencia fora de docs/forja/evidence/ recusada", True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste de avaliar_fim_de_turno completo (gravacoes, VAD e modelo falsos, sem som).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
