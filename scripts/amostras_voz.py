r"""Grava uma amostra WAV de cada voz inglesa do Kokoro, para ouvir e escolher.

Um so modelo Kokoro carregado; cada voz (af_heart e as quatro masculinas
britanicas, lista fechada em `jarvis.config.VOZES_INGLESAS`) diz a mesma frase
inglesa, a velocidade com que o jarvis a usa, e o WAV fica em
`audio/amostras-voz/<voz>.wav`. A pasta `audio/` e ignorada pelo Git e e a
unica onde este script escreve.

Sem `--com-som` nada toca e nenhum dispositivo de som e aberto: so os WAV.
Com `--com-som` cada amostra e tocada enquanto se grava.

    .venv\Scripts\python scripts/amostras_voz.py
    .venv\Scripts\python scripts/amostras_voz.py --com-som
    .venv\Scripts\python scripts/amostras_voz.py --vozes bm_george bm_fable --com-som

Para trocar de voz: `[voz] nome = "..."` no config.toml (ver
config.exemplo.toml). `--autoteste` verifica o script com um motor falso,
sem modelo, sem som, e apaga so os WAV que escreveu.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import voz  # noqa: E402
from jarvis.audio_util import PASTA_AUDIO, caminho_para_mostrar  # noqa: E402
from jarvis.config import VOZ_INGLESA_PADRAO, VOZES_INGLESAS  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402

#: Onde ficam as amostras: dentro de `audio/`, que o .gitignore apanha.
NOME_DA_PASTA = "amostras-voz"

#: A frase que todas as vozes dizem: respostas tipicas do jarvis, sem nomes
#: de projetos nem nada do utilizador.
FRASE_DE_AMOSTRA = (
    "Good evening. I sent your request to the project session. "
    "The run finished without errors. Shall I read you the report?"
)


class AmostraError(Exception):
    """Pedido de amostras que nao se pode cumprir (voz ou pasta invalida)."""


@dataclass(frozen=True)
class Amostra:
    voz: str
    caminho: Path
    duracao_s: float
    palavras_por_minuto: float


def pasta_das_amostras(pasta_de_audio: Path | None = None) -> Path:
    return Path(pasta_de_audio or PASTA_AUDIO) / NOME_DA_PASTA


def validar_pasta(pasta: Path, pasta_de_audio: Path | None = None) -> Path:
    """A pasta resolvida, so se ficar dentro da pasta de audio ignorada pelo Git."""
    resolvida = Path(pasta).resolve()
    base = Path(pasta_de_audio or PASTA_AUDIO).resolve()
    if resolvida != base and base not in resolvida.parents:
        raise AmostraError(f"a pasta das amostras tem de ficar dentro de {caminho_para_mostrar(base)}")
    return resolvida


def validar_vozes(nomes: list[str] | None) -> list[str]:
    """As vozes pedidas, sem repetidas; sem nomes, todas as da lista fechada."""
    vozes = list(dict.fromkeys(nomes or VOZES_INGLESAS))
    desconhecidas = [nome for nome in vozes if nome not in VOZES_INGLESAS]
    if desconhecidas:
        raise AmostraError(
            f"voz desconhecida: {', '.join(desconhecidas)} (conhecidas: {', '.join(VOZES_INGLESAS)})"
        )
    return vozes


def _duracao_s(caminho: Path) -> float:
    with wave.open(str(caminho), "rb") as wf:
        taxa = wf.getframerate()
        return wf.getnframes() / taxa if taxa else 0.0


def gerar_amostras(
    motor_base,
    vozes: list[str],
    *,
    com_som: bool = False,
    pasta_de_audio: Path | None = None,
    saida_de_som=None,
    texto: str = FRASE_DE_AMOSTRA,
) -> list[Amostra]:
    """Um WAV por voz em `<pasta_de_audio>/amostras-voz/`; so toca com `com_som`.

    `motor_base` e um motor com `com_voz(nome)` (o `MotorKokoro`): todas as
    vozes partilham o modelo carregado. Sem `com_som` a reproducao vai com
    `muted=True` e o dispositivo de som nunca e usado.
    """
    vozes = validar_vozes(vozes)
    pasta = validar_pasta(pasta_das_amostras(pasta_de_audio), pasta_de_audio)
    pasta.mkdir(parents=True, exist_ok=True)
    palavras = len(texto.split())
    amostras: list[Amostra] = []
    for nome in vozes:
        motor = motor_base.com_voz(nome)
        caminho = pasta / f"{nome}.wav"
        fala = voz.FalaResidente(motor, saida_de_som=saida_de_som)
        fala.feed(texto)
        fala.play(muted=not com_som, output_wavfile=str(caminho))
        if not caminho.is_file():
            raise AmostraError(f"a voz {nome} nao produziu audio")
        duracao = _duracao_s(caminho)
        ppm = palavras / duracao * 60.0 if duracao > 0 else 0.0
        amostras.append(Amostra(nome, caminho, duracao, ppm))
    return amostras


def _motor_kokoro():
    """O Kokoro real; sem ele nao ha vozes britanicas (o Piper nao as tem)."""
    return voz.MotorKokoro(voz=VOZ_INGLESA_PADRAO)


# --- autoteste (motor e dispositivo falsos) --------------------------------


class _MotorFalso:
    """Seno curto por voz, a 24 kHz; `com_voz` como o do Kokoro."""

    taxa = 24000

    def __init__(self, nome: str = VOZ_INGLESA_PADRAO) -> None:
        self.voz = nome
        self.velocidade = voz.VELOCIDADE_DAS_VOZES[nome]

    def com_voz(self, nome: str) -> "_MotorFalso":
        return _MotorFalso(nome)

    def sintetizar(self, texto: str):
        for _pedaco in voz.dividir_para_sintese(texto):
            n = int(self.taxa * 0.2 / self.velocidade)
            yield b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / self.taxa))) for i in range(n)
            )


class _SaidaQueRegista:
    """Dispositivo de som falso: conta as escritas, nunca abre nada."""

    def __init__(self) -> None:
        self.escritas = 0

    def escrever(self, taxa: int, dados: bytes) -> None:
        self.escritas += 1


def autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, condicao: bool) -> None:
        print(f"[{'OK' if condicao else 'FALHOU'}] {nome}")
        if not condicao:
            falhas.append(nome)

    with tempfile.TemporaryDirectory(prefix="amostras-") as temporaria:
        pasta_de_audio = Path(temporaria) / "audio"
        saida = _SaidaQueRegista()
        amostras = gerar_amostras(_MotorFalso(), list(VOZES_INGLESAS), pasta_de_audio=pasta_de_audio, saida_de_som=saida)
        verificar("um WAV por voz", sorted(a.voz for a in amostras) == sorted(VOZES_INGLESAS))
        verificar("todos os WAV existem e tem audio", all(a.caminho.is_file() and a.duracao_s > 0 for a in amostras))
        verificar(
            "os WAV ficam na pasta de audio/amostras-voz",
            all(a.caminho.parent == pasta_das_amostras(pasta_de_audio).resolve() for a in amostras),
        )
        verificar("sem --com-som o dispositivo de som nao e usado", saida.escritas == 0)
        gerar_amostras(_MotorFalso(), ["bm_george"], com_som=True, pasta_de_audio=pasta_de_audio, saida_de_som=saida)
        verificar("com --com-som a amostra vai para o dispositivo", saida.escritas > 0)
        try:
            validar_pasta(Path(temporaria) / "fora", pasta_de_audio)
            verificar("pasta fora de audio/ recusada", False)
        except AmostraError:
            verificar("pasta fora de audio/ recusada", True)
        try:
            validar_vozes(["af_bella"])
            verificar("voz fora da lista recusada", False)
        except AmostraError:
            verificar("voz fora da lista recusada", True)
    print("autoteste: " + ("OK" if not falhas else f"{len(falhas)} falha(s)"))
    return 1 if falhas else 0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/amostras_voz.py",
        description="Grava uma amostra WAV de cada voz inglesa em audio/amostras-voz/ (so toca com --com-som).",
    )
    parser.add_argument(
        "--vozes",
        nargs="+",
        choices=VOZES_INGLESAS,
        default=None,
        metavar="VOZ",
        help=f"so estas vozes (por omissao todas: {', '.join(VOZES_INGLESAS)})",
    )
    parser.add_argument("--com-som", action="store_true", help="toca cada amostra enquanto a grava")
    parser.add_argument("--autoteste", action="store_true", help="verifica o script com um motor falso, sem som")
    return parser


def main(argv: list[str] | None = None) -> int:
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return autoteste()
    try:
        vozes = validar_vozes(args.vozes)
        motor = _motor_kokoro()
    except (AmostraError, voz.MotorIndisponivel, ValueError) as erro:
        print(f"FALHOU: {erro}", file=sys.stderr)
        return 1
    print(f"frase: {FRASE_DE_AMOSTRA}")
    print("a tocar e a gravar..." if args.com_som else "sem som: so os WAV (--com-som para ouvir)")
    try:
        amostras = gerar_amostras(motor, vozes, com_som=args.com_som)
    except (AmostraError, voz.MotorIndisponivel) as erro:
        print(f"FALHOU: {erro}", file=sys.stderr)
        return 1
    for amostra in amostras:
        marca = " (por omissao)" if amostra.voz == VOZ_INGLESA_PADRAO else ""
        print(
            f"{amostra.voz:10s}: {caminho_para_mostrar(amostra.caminho)} | {amostra.duracao_s:.2f} s | "
            f"{amostra.palavras_por_minuto:.0f} palavras/min | velocidade {voz.VELOCIDADE_DAS_VOZES[amostra.voz]:g}"
            f"{marca}"
        )
    print('para trocar de voz: [voz] nome = "..." no config.toml')
    return 0


if __name__ == "__main__":
    sys.exit(main())
