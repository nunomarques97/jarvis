r"""Mede o interromper com a voz real do Sponsor: auto-interrupcoes e latencia da pausa.

Duas partes, com o headset posto e o microfone do config.toml:

  1. ECO (o jarvis nunca se interrompe a si proprio): o jarvis le
     `len(RESPOSTAS_DO_ECO)` respostas curtas pelo headset enquanto o ouvido
     ouve por cima da voz exatamente como no `python -m jarvis` (VAD Silero,
     `CHUNKS_PARA_INTERROMPER` chunks seguidos). O Sponsor fica calado. Cada
     inicio de fala detetado e uma auto-interrupcao (meta: 0). A voz nao e
     pausada nesta parte: conta-se tudo o que a teria pausado.
  2. INTERROMPER: `RONDAS` rondas guiadas. Em cada uma o jarvis le um texto
     comprido e o Sponsor interrompe quando quiser ("okay, that's enough"). A
     pausa e a verdadeira (`jarvis.voz.pausar_agora`); mede-se do primeiro
     chunk com fala ate a voz parar (meta: p95 < 300 ms) e a voz e calada a
     seguir.

Nada e gravado: o audio do microfone so passa pelo VAD, nunca vai para
ficheiro nem para o motor de transcricao. O relatorio so tem numeros.

PASSO DO SPONSOR (voz real, uns 5 minutos, headset posto):

    .venv\Scripts\python scripts\medir_interrupcao.py --com-som --evidencia

  - Parte 1: fica calado enquanto o jarvis le as respostas curtas (cerca de
    um minuto). Nao mexas no headset.
  - Parte 2: em cada uma das 10 rondas carrega Enter, ouve o jarvis e
    interrompe-o quando quiseres, por exemplo com "okay, that's enough".
  - No fim o script diz as auto-interrupcoes (meta 0) e a latencia p50/p95
    (meta p95 < 300 ms); com --evidencia escreve o resumo em
    docs/forja/evidence/ (pasta ignorada pelo Git).

Sem --com-som nada toca: o script recusa e so mostra este passo.

    .venv\Scripts\python scripts\medir_interrupcao.py --autoteste

O autoteste corre as duas partes com microfone, voz e VAD falsos (e, quando
o ficheiro existe, com o VAD Silero verdadeiro sobre silencio e ruido fraco),
sem som nem microfone.
"""

from __future__ import annotations

import argparse
import datetime
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import PASTA_EVIDENCIA, caminho_evidencia_de_saida, caminho_para_mostrar  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402
from jarvis.ouvido import (  # noqa: E402
    BYTES_POR_CHUNK,
    DURACAO_DO_CHUNK_S,
    INTERRUPCAO_PAUSAR,
    Ouvido,
)
from jarvis.stt import MotorBase  # noqa: E402

#: Metas (docs/NATURALNESS.md).
META_AUTO_INTERRUPCOES = 0
META_P95_MS = 300.0

RONDAS = 10

RESPOSTAS_DO_ECO = (
    "It's two thirty-two in the afternoon.",
    "Today is Thursday, September twenty-fifth.",
    "Sent to the project. I'll tell you when it answers.",
    "The run finished without errors; the report is on screen.",
    "It's twenty-two degrees in Porto today, with a light breeze from the north.",
    "Okay, fresh start.",
    "The session is waiting for you. It asked whether it should run the whole test suite.",
    "I don't know that project yet. The ones I know are on screen.",
)

TEXTO_COMPRIDO = (
    "Lisbon is one of the oldest cities in western Europe. It grew on the hills above the Tagus river, "
    "where traders came and went for centuries. In seventeen fifty-five a great earthquake, followed by "
    "fire and a tsunami, destroyed most of the lower town. The city was rebuilt on a regular grid, with "
    "wide streets and buildings designed to resist the next earthquake. Today the old tram lines still "
    "climb the hills, and the river front is full of people walking in the evening."
)

#: Quanto se espera, no fim de cada ronda, que o ouvido acabe a frase dita por cima.
ESPERA_PELO_REPOUSO_S = 10.0


def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo posto mais proximo (o mesmo de `jarvis.ouvido.percentil`)."""
    ordenados = sorted(valores)
    return ordenados[max(1, math.ceil(p / 100 * len(ordenados))) - 1]


class _MotorMudo(MotorBase):
    """Nada e transcrito: a frase dita por cima so interessa ao VAD."""

    nome = "sem-transcricao"

    def __init__(self) -> None:
        super().__init__("cpu")

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return "", lingua, False


class _SemTecla:
    def premida(self) -> bool:
        return False


@dataclass
class Resultado:
    auto_interrupcoes: int = 0
    respostas_do_eco: int = 0
    latencias_ms: list[float] = field(default_factory=list)
    rondas: int = 0
    #: Rondas em que o texto acabou sem ninguem o interromper.
    sem_interrupcao: int = 0
    #: Rondas em que a fala foi detetada mas a voz ja nao estava a tocar.
    sem_pausa: int = 0

    def falhas(self) -> list[str]:
        falhas = []
        if self.auto_interrupcoes > META_AUTO_INTERRUPCOES:
            falhas.append(f"{self.auto_interrupcoes} auto-interrupcao(oes) (meta {META_AUTO_INTERRUPCOES})")
        if not self.latencias_ms:
            falhas.append("nenhuma interrupcao medida")
        elif percentil(self.latencias_ms, 95) >= META_P95_MS:
            falhas.append(f"p95 {percentil(self.latencias_ms, 95):.0f} ms (meta < {META_P95_MS:.0f} ms)")
        return falhas

    def linhas(self) -> list[str]:
        linhas = [
            f"auto-interrupcoes: {self.auto_interrupcoes} em {self.respostas_do_eco} respostas "
            f"(meta {META_AUTO_INTERRUPCOES})",
        ]
        if self.latencias_ms:
            linhas.append(
                f"interromper, do inicio da fala a voz parada: p50 {percentil(self.latencias_ms, 50):.0f} ms, "
                f"p95 {percentil(self.latencias_ms, 95):.0f} ms, max {max(self.latencias_ms):.0f} ms "
                f"em {len(self.latencias_ms)} de {self.rondas} rondas (meta p95 < {META_P95_MS:.0f} ms)"
            )
        else:
            linhas.append(f"interromper: nenhuma medida em {self.rondas} rondas")
        if self.sem_interrupcao or self.sem_pausa:
            linhas.append(
                f"rondas sem medida: {self.sem_interrupcao} sem interrupcao, {self.sem_pausa} com a voz ja parada"
            )
        falhas = self.falhas()
        linhas.append("METAS CUMPRIDAS" if not falhas else "FORA DAS METAS: " + "; ".join(falhas))
        return linhas


class Medicao:
    """As duas partes, com as pecas trocaveis (microfone, voz, VAD) para o autoteste.

    `falar(texto)` fala e so volta no fim (ou quando calada); `pausar()`
    devolve o instante em que a voz parou (ou None); `calar()` acaba a fala.
    `perguntar(texto)` espera pelo Sponsor (o `input` na medicao real).
    """

    def __init__(
        self,
        fonte,
        vad_da_interrupcao,
        vad,
        *,
        falar: Callable[[str], object],
        pausar: Callable[[], float | None],
        calar: Callable[[], object],
        perguntar: Callable[[str], object] = input,
        escrever: Callable[[str], object] = print,
    ) -> None:
        self._falar = falar
        self._pausar = pausar
        self._calar = calar
        self._perguntar = perguntar
        self._escrever = escrever
        self._tranca = threading.Lock()
        self._parte = "eco"
        self._onsets = 0
        self._ronda: dict | None = None
        self.ouvido = Ouvido(
            fonte,
            _MotorMudo(),
            lambda _frase: None,
            tecla=_SemTecla(),
            vad=vad,
            escrever=lambda _texto: None,
            vad_da_interrupcao=vad_da_interrupcao,
            ao_interromper=self._ao_interromper,
        )

    def _ao_interromper(self, evento: str, instante: float) -> None:
        if evento != INTERRUPCAO_PAUSAR:
            return
        with self._tranca:
            if self._parte == "eco":
                self._onsets += 1
                return
            ronda = self._ronda
        if ronda is None or ronda.get("detetada"):
            return
        ronda["detetada"] = True
        parada = self._pausar()
        if parada is not None:
            ronda["ms"] = (parada - instante) * 1000
        self._calar()

    def _falar_a_ouvir(self, texto: str) -> None:
        self.ouvido.definir_voz_a_falar(True)
        try:
            self._falar(texto)
        finally:
            self.ouvido.definir_voz_a_falar(False)

    def _esperar_repouso(self) -> None:
        fim = time.perf_counter() + ESPERA_PELO_REPOUSO_S
        while self.ouvido.a_ouvir_alguem and time.perf_counter() < fim:
            time.sleep(0.05)

    def correr(self, *, respostas=RESPOSTAS_DO_ECO, rondas: int = RONDAS, texto: str = TEXTO_COMPRIDO) -> Resultado:
        resultado = Resultado(respostas_do_eco=len(respostas), rondas=rondas)
        self.ouvido.iniciar()
        try:
            self._escrever("")
            self._escrever("=== Parte 1: eco. Fica calado; o jarvis vai ler respostas curtas. ===")
            self._perguntar("Carrega Enter quando estiveres pronto e calado... ")
            for numero, resposta in enumerate(respostas, 1):
                antes = self._onsets
                self._falar_a_ouvir(resposta)
                self._esperar_repouso()
                novos = self._onsets - antes
                self._escrever(f"  resposta {numero}/{len(respostas)}: {novos} auto-interrupcao(oes)")
            resultado.auto_interrupcoes = self._onsets
            with self._tranca:
                self._parte = "interromper"
            self._escrever("")
            self._escrever(f"=== Parte 2: interromper, {rondas} rondas. Interrompe o jarvis quando quiseres. ===")
            for numero in range(1, rondas + 1):
                self._perguntar(f"Ronda {numero}/{rondas}: carrega Enter e interrompe-o (ex.: okay, that's enough)... ")
                ronda: dict = {}
                with self._tranca:
                    self._ronda = ronda
                self._falar_a_ouvir(texto)
                self._esperar_repouso()
                with self._tranca:
                    self._ronda = None
                if "ms" in ronda:
                    resultado.latencias_ms.append(ronda["ms"])
                    self._escrever(f"  ronda {numero}: voz parada {ronda['ms']:.0f} ms depois do inicio da fala")
                elif ronda.get("detetada"):
                    resultado.sem_pausa += 1
                    self._escrever(f"  ronda {numero}: a fala chegou quando a voz ja tinha acabado (sem medida)")
                else:
                    resultado.sem_interrupcao += 1
                    self._escrever(f"  ronda {numero}: o texto acabou sem interrupcao (sem medida)")
        finally:
            self.ouvido.parar()
            self.ouvido.esperar(3.0)
        return resultado


def escrever_evidencia(destino: Path, resultado: Resultado, descricao: str) -> None:
    linhas = [
        f"# Interromper: medicao com a voz real ({datetime.datetime.now():%Y-%m-%d %H:%M})\n\n",
        f"Microfone e voz: {descricao}\n\n",
        *(f"- {linha}\n" for linha in resultado.linhas()),
        "\n## Rondas (ms, do inicio da fala a voz parada)\n\n",
        ", ".join(f"{ms:.0f}" for ms in resultado.latencias_ms) or "nenhuma",
        "\n",
    ]
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("".join(linhas), encoding="utf-8")


# --- Medicao real ------------------------------------------------------------------


def _medir_a_serio(destino: Path | None) -> int:
    from jarvis import voz
    from jarvis.config import CAMINHO_CONFIG_PADRAO, ConfigError, carregar_config
    from jarvis.ouvido import MicrofonePyAudio, VadWebRtc
    from jarvis.vad_silero import VadSilero

    try:
        config = carregar_config(CAMINHO_CONFIG_PADRAO)
    except ConfigError as erro:
        print(f"ERRO: {erro}")
        return 2
    voz.definir_lingua_da_voz("en" if config.ouvido.lingua == "en" else "pt")
    voz.definir_voz_inglesa(config.voz.nome)
    voz.definir_velocidade(config.voz.velocidade)
    try:
        vad_silero = VadSilero()
    except FileNotFoundError as erro:
        print(f"ERRO: {erro}")
        return 2
    print("a carregar a voz...")
    descricao_da_voz = voz.aquecer()
    fonte = MicrofonePyAudio(config.microfone)
    medicao = Medicao(
        fonte,
        vad_silero,
        VadWebRtc(),
        falar=lambda texto: voz.falar(texto, com_som=True),
        pausar=voz.pausar_agora,
        calar=lambda: voz.calar_agora("interrupcao medida", definitivo=False),
    )
    try:
        resultado = medicao.correr()
    except OSError as erro:
        print(f"ERRO: {erro}")
        return 2
    except KeyboardInterrupt:
        voz.calar_agora("Ctrl+C na medicao", definitivo=True)
        print("\ninterrompido: nada escrito")
        return 1
    print()
    for linha in resultado.linhas():
        print(linha)
    if destino is not None:
        escrever_evidencia(destino, resultado, f"{fonte.descricao} | {descricao_da_voz}")
        print(f"evidencia escrita em {caminho_para_mostrar(destino)}")
    return 0 if not resultado.falhas() else 1


# --- Autoteste (sem som, sem microfone) -------------------------------------------------------


FALA = 0x55


class _VadFalso:
    def e_fala(self, pedaco: bytes) -> bool:
        return pedaco[0] == FALA


class _Cenario:
    """Microfone e voz falsos ligados: a fala do "Sponsor" comeca `inicio_s` depois de cada texto comecar."""

    def __init__(self, *, inicio_s: float | None, chunk_de_eco: Callable[[], bytes]) -> None:
        self.inicio_s = inicio_s
        self.chunk_de_eco = chunk_de_eco
        self.descricao = "microfone falso"
        self._a_falar_desde: float | None = None
        self._pausada = threading.Event()
        self._calada = threading.Event()
        self._fala_do_sponsor = False
        self.falados: list[str] = []
        self.caladas = 0

    # -- a fonte
    def abrir(self) -> None:
        pass

    def fechar(self) -> None:
        pass

    def ler(self) -> bytes:
        time.sleep(DURACAO_DO_CHUNK_S)
        desde = self._a_falar_desde
        if self._fala_do_sponsor and desde is not None and time.perf_counter() - desde >= self.inicio_s:
            return bytes([FALA]) * BYTES_POR_CHUNK
        if desde is not None and not self._pausada.is_set():
            return self.chunk_de_eco()
        return b"\x00" * BYTES_POR_CHUNK

    # -- a voz
    def falar(self, texto: str, duracao_s: float = 0.6) -> None:
        self.falados.append(texto)
        self._pausada.clear()
        self._calada.clear()
        self._a_falar_desde = time.perf_counter()
        fim = self._a_falar_desde + duracao_s
        while time.perf_counter() < fim and not self._calada.is_set():
            time.sleep(0.005)
        self._a_falar_desde = None

    def pausar(self) -> float | None:
        if self._a_falar_desde is None:
            return None
        # A reproducao para no fim do bloco de 50 ms em curso.
        time.sleep(0.02)
        self._pausada.set()
        return time.perf_counter()

    def calar(self) -> None:
        self.caladas += 1
        self._calada.set()


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, condicao: bool, detalhe: str = "") -> None:
        if condicao:
            print(f"ok   {nome}")
        else:
            falhas.append(f"{nome} {detalhe}".strip())

    import jarvis.vad_silero as vad_silero

    def calado(_texto: str = "") -> None:
        return None

    # 1. Eco fraco (o que o headset fechado deixa passar) nunca interrompe; o Sponsor calado.
    cenario = _Cenario(inicio_s=None, chunk_de_eco=lambda: bytes([0x11]) * BYTES_POR_CHUNK)
    medicao = Medicao(
        cenario, _VadFalso(), _VadFalso(), falar=cenario.falar, pausar=cenario.pausar,
        calar=cenario.calar, perguntar=calado, escrever=calado,
    )
    resultado = medicao.correr(respostas=RESPOSTAS_DO_ECO[:3], rondas=0)
    verificar("eco: 0 auto-interrupcoes com o eco fraco", resultado.auto_interrupcoes == 0, str(resultado))
    verificar("eco: as respostas foram todas faladas", cenario.falados == list(RESPOSTAS_DO_ECO[:3]))

    # 2. Eco forte que o VAD chama fala: contado como auto-interrupcao (e a voz nao e pausada).
    cenario = _Cenario(inicio_s=None, chunk_de_eco=lambda: bytes([FALA]) * BYTES_POR_CHUNK)
    medicao = Medicao(
        cenario, _VadFalso(), _VadFalso(), falar=cenario.falar, pausar=cenario.pausar,
        calar=cenario.calar, perguntar=calado, escrever=calado,
    )
    resultado = medicao.correr(respostas=RESPOSTAS_DO_ECO[:2], rondas=0)
    verificar("eco: o eco que parece fala e contado", resultado.auto_interrupcoes >= 1, str(resultado))
    verificar("eco: com auto-interrupcoes a meta falha", any("auto-interrupcao" in f for f in resultado.falhas()))

    # 3. Rondas: o Sponsor fala 0,3 s depois de cada texto comecar; a voz para dentro da meta.
    cenario = _Cenario(inicio_s=0.3, chunk_de_eco=lambda: b"\x00" * BYTES_POR_CHUNK)
    cenario._fala_do_sponsor = True  # noqa: SLF001 - o cenario so fala nas rondas
    medicao = Medicao(
        cenario, _VadFalso(), _VadFalso(), falar=lambda t: cenario.falar(t, duracao_s=3.0),
        pausar=cenario.pausar, calar=cenario.calar, perguntar=calado, escrever=calado,
    )
    resultado = medicao.correr(respostas=(), rondas=3)
    verificar("rondas: tres medidas", len(resultado.latencias_ms) == 3, str(resultado.latencias_ms))
    verificar(
        f"rondas: p95 < {META_P95_MS:.0f} ms",
        bool(resultado.latencias_ms) and percentil(resultado.latencias_ms, 95) < META_P95_MS,
        str(resultado.latencias_ms),
    )
    verificar("rondas: cada texto foi calado a meio", cenario.caladas == 3 and len(cenario.falados) == 3)
    verificar("rondas: metas cumpridas", resultado.falhas() == [], str(resultado.falhas()))

    # 4. Ronda sem interrupcao: sem medida, e a meta nao passa sem medidas.
    cenario = _Cenario(inicio_s=None, chunk_de_eco=lambda: b"\x00" * BYTES_POR_CHUNK)
    medicao = Medicao(
        cenario, _VadFalso(), _VadFalso(), falar=lambda t: cenario.falar(t, duracao_s=0.3),
        pausar=cenario.pausar, calar=cenario.calar, perguntar=calado, escrever=calado,
    )
    resultado = medicao.correr(respostas=(), rondas=1)
    verificar("ronda sem interrupcao: contada a parte", resultado.sem_interrupcao == 1 and not resultado.latencias_ms)
    verificar("sem medidas a meta falha", "nenhuma interrupcao medida" in resultado.falhas())

    # 5. Relatorio: so numeros.
    linhas = Resultado(0, 8, [120.0, 150.0, 90.0], 3).linhas()
    verificar("relatorio: p50 e p95", any("p50 120 ms, p95 150 ms" in linha for linha in linhas), str(linhas))
    verificar("relatorio: metas cumpridas", linhas[-1] == "METAS CUMPRIDAS", str(linhas))

    # 6. Sem --com-som nada toca: recusa e mostra o passo do Sponsor.
    import contextlib
    import io

    saida = io.StringIO()
    with contextlib.redirect_stdout(saida):
        codigo = main([])
    verificar("sem --com-som: recusa", codigo == 2 and "PASSO DO SPONSOR" in saida.getvalue(), saida.getvalue())

    # 7. O VAD Silero verdadeiro, se o ficheiro existir: silencio e ruido fraco sem auto-interrupcoes.
    if vad_silero.MODELO_SILERO.is_file():
        import numpy as np

        gerador = np.random.default_rng(11)
        cenario = _Cenario(
            inicio_s=None,
            chunk_de_eco=lambda: gerador.normal(0, 150, BYTES_POR_CHUNK // 2).astype("<i2").tobytes(),
        )
        medicao = Medicao(
            cenario, vad_silero.VadSilero(), _VadFalso(), falar=cenario.falar, pausar=cenario.pausar,
            calar=cenario.calar, perguntar=calado, escrever=calado,
        )
        resultado = medicao.correr(respostas=RESPOSTAS_DO_ECO[:2], rondas=0)
        verificar("Silero verdadeiro: ruido fraco nao interrompe", resultado.auto_interrupcoes == 0, str(resultado))
    else:
        print("---  Silero verdadeiro: models/openwakeword/silero_vad.onnx em falta (saltado)")

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste da medicao do interromper completo (sem som, sem microfone).")
    return 0


def _passo_do_sponsor() -> str:
    inicio = __doc__.index("PASSO DO SPONSOR")
    fim = __doc__.index("Sem --com-som")
    return __doc__[inicio:fim].rstrip()


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts/medir_interrupcao.py", description=__doc__.splitlines()[0])
    parser.add_argument("--autoteste", action="store_true", help="pecas falsas; sem som nem microfone")
    parser.add_argument("--com-som", action="store_true", help="toca a voz no headset e ouve o microfone")
    parser.add_argument(
        "--evidencia",
        nargs="?",
        const="",
        metavar="FICHEIRO",
        help="escreve o resumo em docs/forja/evidence/ (por omissao com data no nome)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()
    if not args.com_som:
        print("Nada toca sem --com-som: esta medicao precisa do headset e da voz real do Sponsor.")
        print()
        print(_passo_do_sponsor())
        return 2
    destino = None
    if args.evidencia is not None:
        try:
            destino = (
                caminho_evidencia_de_saida(args.evidencia)
                if args.evidencia
                else PASTA_EVIDENCIA / f"interrupcao-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
            )
        except ValueError as erro:
            print(f"FALHOU (--evidencia fora de docs/forja/evidence/): {erro}", file=sys.stderr)
            return 1
    return _medir_a_serio(destino)


if __name__ == "__main__":
    sys.exit(main())
