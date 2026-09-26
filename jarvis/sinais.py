"""Sons curtos que marcam a escuta sem palavra de ativacao.

Quando o jarvis acaba de falar e fica a ouvir sem "hey jarvis", toca um som
curto e suave ("abrir", um tom a subir); quando essa escuta fecha toca outro,
diferente ("fechar", um tom a descer). Os dois sao gerados aqui, em memoria,
como PCM16 mono: nenhum ficheiro, nenhum download, nenhuma dependencia nova.

Cada som dura no maximo `DURACAO_S` (menos do que a guarda do ouvido depois
da voz, `ouvido.GUARDA_APOS_A_VOZ_S`, para nunca entrar na frase seguinte),
comeca e acaba em silencio (sem estalido) e o pico e `volume` vezes a escala
completa: baixo por omissao.

Tocar segue a mesma regra de `voz.falar`: `tocar()` so abre o dispositivo de
som com `com_som=True` explicito. Sem isso volta logo, sem importar nem chamar
o tocador, que e o caso de todos os testes. Com som, toca numa thread daemon
e uma falha nunca chega a quem chamou.

Uso:
    wav = wav_do_som("abrir", volume=0.15)       # bytes WAV, nada toca
    tocar("fechar", volume=0.15, com_som=True)   # toca nas colunas
"""

from __future__ import annotations

import io
import logging
import math
import sys
import threading
import wave
from array import array

from jarvis.config import VOLUME_DOS_SONS_PADRAO

registo = logging.getLogger(__name__)

#: Taxa de amostragem dos sons gerados.
TAXA = 24000

#: Duracao de cada som; tem de ficar abaixo da guarda do ouvido depois da voz.
DURACAO_S = 0.15

#: Rampa de entrada e de saida (meio coseno), para o som nao estalar.
RAMPA_S = 0.03

#: Frequencia inicial e final de cada som: "abrir" sobe, "fechar" desce.
FREQUENCIAS = {
    "abrir": (660.0, 990.0),
    "fechar": (880.0, 523.0),
}

ESCALA_COMPLETA = 32767


def _validar(tipo: str, volume: float) -> None:
    if tipo not in FREQUENCIAS:
        raise ValueError(f"som desconhecido: {tipo!r} (so {', '.join(FREQUENCIAS)})")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 < volume <= 1:
        raise ValueError(f"volume {volume!r} invalido: tem de ser maior do que 0 e no maximo 1")


def gerar_som(tipo: str, volume: float = VOLUME_DOS_SONS_PADRAO) -> bytes:
    """As amostras PCM16 mono (little-endian) do som `tipo`, a `TAXA`."""
    _validar(tipo, volume)
    inicio, fim = FREQUENCIAS[tipo]
    total = int(TAXA * DURACAO_S)
    rampa = int(TAXA * RAMPA_S)
    pico = volume * ESCALA_COMPLETA
    amostras = array("h")
    fase = 0.0
    for indice in range(total):
        # A frequencia desliza de `inicio` a `fim`; a fase acumula-se para o
        # tom mudar de altura sem saltos.
        frequencia = inicio + (fim - inicio) * indice / (total - 1)
        fase += 2 * math.pi * frequencia / TAXA
        if indice < rampa:
            envelope = 0.5 - 0.5 * math.cos(math.pi * indice / rampa)
        elif indice >= total - rampa:
            envelope = 0.5 - 0.5 * math.cos(math.pi * (total - 1 - indice) / rampa)
        else:
            envelope = 1.0
        amostras.append(int(round(pico * envelope * math.sin(fase))))
    if sys.byteorder != "little":
        amostras.byteswap()
    return amostras.tobytes()


def wav_do_som(tipo: str, volume: float = VOLUME_DOS_SONS_PADRAO) -> bytes:
    """O som `tipo` num WAV completo em memoria (cabecalho + PCM16 mono)."""
    saida = io.BytesIO()
    with wave.open(saida, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TAXA)
        wav.writeframes(gerar_som(tipo, volume))
    return saida.getvalue()


def _tocar_no_sistema(wav: bytes) -> None:
    """Toca um WAV em memoria e so volta no fim (bloqueia; corre numa thread)."""
    import winsound

    winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_NODEFAULT)


def _tocar_sem_falhar(tipo: str, wav: bytes) -> None:
    try:
        _tocar_no_sistema(wav)
    except Exception as erro:  # noqa: BLE001 - um som falhado nunca derruba o jarvis
        registo.warning("som '%s' nao tocou: %s", tipo, erro)


def tocar(
    tipo: str, *, volume: float = VOLUME_DOS_SONS_PADRAO, com_som: bool = False
) -> threading.Thread | None:
    """Toca o som `tipo` numa thread daemon, so com `com_som=True`.

    Sem `com_som=True` devolve None sem abrir dispositivo nenhum. Com som
    devolve a thread (ja arrancada); qualquer falha ao gerar ou tocar fica no
    log e nunca e levantada para quem chamou.
    """
    if not com_som:
        return None
    try:
        wav = wav_do_som(tipo, volume)
    except Exception as erro:  # noqa: BLE001 - idem: nunca levanta
        registo.warning("som '%s' nao gerado: %s", tipo, erro)
        return None
    thread = threading.Thread(
        target=_tocar_sem_falhar, args=(tipo, wav), name=f"sinal-{tipo}", daemon=True
    )
    thread.start()
    return thread
