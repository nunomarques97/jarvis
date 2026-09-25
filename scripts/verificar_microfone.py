r"""Verifica o caminho do microfone: escolha do dispositivo, leitura em tempo real e nivel.

Sem isto nao ha como confiar em nenhuma transcricao ao vivo mais tarde: e o
unico dos tres utilitarios de audio que toca em hardware real, por isso nunca
levanta uma excecao para fora de main() — reporta o erro e sai com codigo 1
("sem rebentar" e o criterio de aceitacao).

Nome pedido: o de --dispositivo, senao o da variavel de ambiente
JARVIS_NOME_MICROFONE (mesma convencao que
jarvis.canal_claude.VARIAVEL_TITULO_JANELA), senao nenhum (entrada por omissao
de cada API).

ESCOLHA DO DISPOSITIVO (`abrir_microfone`, a funcao unica que o gravador usa e
que o ouvido ao vivo pode reutilizar). No Windows o mesmo microfone aparece uma
vez por API de audio e elas nao se portam da mesma maneira:

  MME          preferida. Le em tempo real a 16 kHz. Corta os nomes a 31
               caracteres, por isso um nome configurado inteiro casa pelo
               prefixo com o nome cortado.
  WASAPI       segunda escolha. So abre a taxa nativa (tipicamente 48 kHz) e as
               vezes so com os canais nativos: le-se assim e reamostra-se para
               16 kHz mono aqui.
  DirectSound  NUNCA. A 16 kHz devolve blocos de zeros sem esperar pelo
               microfone: 3 s reais deram centenas de milhares de segundos de
               "audio" a zeros, e as gravacoes ficaram com ~2 s uteis e o resto
               silencio digital.
  WDM-KS       NUNCA. Abre o dispositivo em modo de kernel, so a taxas
               nativas e roubando-o as outras aplicacoes.

Cada candidato so e aceite depois de uma PROVA ao abrir: le-se um pouco de
audio e compara-se com o relogio. Audio lido a mais (mais rapido que o tempo
real) ou a menos, ou um troco de zeros exatos de meio segundo, recusa o
candidato; se nenhum passa, falha com a lista do que se tentou em vez de
gravar lixo.

SINAL: alem do RMS, imprime-se o pico da captura. O RMS a quatro casas da
0.0000 tanto para "ninguem a falar" (amostras de +-1, o caminho esta vivo)
como para "silencio digital" (microfone em mudo, sem permissao, driver a
devolver zeros) — e so a segunda e que e um problema de caminho.

Uso:
    .venv\Scripts\python scripts/verificar_microfone.py
    .venv\Scripts\python scripts/verificar_microfone.py --dispositivo "USB" --segundos 3
    .venv\Scripts\python scripts/verificar_microfone.py --autoteste
"""

from __future__ import annotations

import argparse
import array
import math
import os
import re
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    PASTA_AUDIO,
    TAXA_AMOSTRAGEM_PADRAO,
    caminho_para_mostrar,
    caminho_wav_de_saida,
    escrever_wav_pcm16,
    nivel_rms,
    pico_pcm16,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402

VARIAVEL_NOME_MICROFONE = "JARVIS_NOME_MICROFONE"
SEGUNDOS_DE_CAPTURA_PADRAO = 3.0
TAMANHO_DO_BLOCO = 1024

#: Teto da captura. A captura acumula a gravacao inteira em memoria antes de
#: escrever o WAV (`b"".join`), por isso um `--segundos 1e9` enchia a RAM e o
#: disco. Um minuto e muito mais do que os 3 s que este utilitario precisa.
TETO_DE_SEGUNDOS_DE_CAPTURA = 60.0

# --- APIs de audio do Windows (o campo `type` do PortAudio) -------------------------

API_DIRECTSOUND = 1
API_MME = 2
API_WDMKS = 11
API_WASAPI = 13

NOMES_DAS_APIS = {
    API_MME: "MME",
    API_WASAPI: "WASAPI",
    API_DIRECTSOUND: "DirectSound",
    API_WDMKS: "WDM-KS",
}

#: As unicas APIs aceites, por ordem de preferencia.
APIS_PREFERIDAS = (API_MME, API_WASAPI)

#: Porque cada API conhecida fica de fora (aparece na lista de tentativas).
MOTIVO_DA_API_EXCLUIDA = {
    API_DIRECTSOUND: "DirectSound excluida: a 16 kHz devolve blocos de zeros sem esperar pelo microfone",
    API_WDMKS: "WDM-KS excluida: modo de kernel, so taxas nativas e rouba o dispositivo",
}

#: O MME corta os nomes dos dispositivos a 31 caracteres.
LIMITE_DO_NOME_MME = 31
#: Um nome cortado so casa pelo prefixo se tiver pelo menos isto: evita que um
#: nome curtinho ("Mic") case com qualquer nome configurado que comece igual.
MINIMO_DO_NOME_CORTADO = 10

# --- Prova ao abrir: a leitura acompanha o relogio ----------------------------------

#: Audio lido na prova de cada candidato.
SEGUNDOS_DE_PROVA = 1.0
#: Tolerancia entre o audio lido e o tempo real decorrido: aceita-se
#: |audio - real| ate 0,25 s mais 10% do tempo real. Cobre o arranque do
#: stream e o buffer do driver; um driver que devolve zeros sem esperar le
#: segundos de "audio" em milissegundos e fica muito para la disto.
FOLGA_ABSOLUTA_S = 0.25
FOLGA_RELATIVA = 0.10
#: Um troco de zeros exatos com esta duracao ou mais e silencio digital: um
#: microfone vivo tem sempre algum ruido, nunca meio segundo de zeros.
TROCO_DE_ZEROS_MAXIMO_S = 0.5


class MicrofoneInutilizavel(OSError):
    """Nenhum candidato abriu e passou a prova; a mensagem lista as tentativas."""


def segundos_de_captura(valor: str) -> float:
    """Tipo do argparse para --segundos: numero positivo e com teto."""
    try:
        numero = float(valor)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{valor}' nao e um numero de segundos")
    if not 0 < numero <= TETO_DE_SEGUNDOS_DE_CAPTURA:
        raise argparse.ArgumentTypeError(
            f"--segundos tem de estar entre 0 (exclusive) e "
            f"{TETO_DE_SEGUNDOS_DE_CAPTURA:.0f}; recebi {valor}"
        )
    return numero


def listar_dispositivos_de_entrada(pa) -> list[dict]:
    dispositivos = []
    for indice in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(indice)
        if info.get("maxInputChannels", 0) > 0:
            dispositivos.append(info)
    return dispositivos


def tipo_da_api(pa, info: dict) -> int | None:
    """O tipo PortAudio da API do dispositivo (API_MME, API_WASAPI, ...)."""
    try:
        return int(pa.get_host_api_info_by_index(int(info["hostApi"]))["type"])
    except Exception:  # noqa: BLE001 - API desconhecida fica de fora
        return None


def nome_da_api(tipo: int | None) -> str:
    return NOMES_DAS_APIS.get(tipo, f"API {tipo}") if tipo is not None else "API desconhecida"


def nome_casa(nome_do_dispositivo: str, nome_configurado: str) -> bool:
    """O dispositivo e o configurado: contem-no, ou e o seu prefixo cortado pelo MME.

    Sem distinguir maiusculas. O corte do MME pode deixar um espaco no fim,
    que o proprio PortAudio tira; por isso o prefixo compara-se sem espacos
    nas pontas.
    """
    alvo = nome_configurado.strip().casefold()
    nome = str(nome_do_dispositivo).strip().casefold()
    if not alvo or not nome:
        return False
    if alvo in nome:
        return True
    return MINIMO_DO_NOME_CORTADO <= len(nome) <= LIMITE_DO_NOME_MME and alvo.startswith(nome)


@dataclass(frozen=True)
class Candidato:
    indice: int
    nome: str
    tipo_api: int
    taxa_nativa: int
    canais_nativos: int

    @property
    def api(self) -> str:
        return nome_da_api(self.tipo_api)

    @property
    def taxas(self) -> tuple[int, ...]:
        """16 kHz primeiro (sem reamostrar); depois a taxa nativa."""
        return tuple(dict.fromkeys((TAXA_AMOSTRAGEM_PADRAO, self.taxa_nativa)))

    @property
    def canais(self) -> tuple[int, ...]:
        """Mono primeiro; depois os canais nativos (fica-se com o primeiro)."""
        return tuple(dict.fromkeys((1, max(1, self.canais_nativos))))


def _candidato(info: dict, tipo: int) -> Candidato:
    return Candidato(
        indice=int(info["index"]),
        nome=str(info.get("name", "?")),
        tipo_api=tipo,
        taxa_nativa=int(info.get("defaultSampleRate") or TAXA_AMOSTRAGEM_PADRAO),
        canais_nativos=int(info.get("maxInputChannels", 1)),
    )


def _entradas_por_omissao(pa) -> list[Candidato]:
    """A entrada por omissao de cada API aceite, pela ordem de preferencia."""
    por_tipo: dict[int, Candidato] = {}
    for indice_api in range(pa.get_host_api_count()):
        info_api = pa.get_host_api_info_by_index(indice_api)
        tipo = int(info_api.get("type", -1))
        indice = int(info_api.get("defaultInputDevice", -1))
        if tipo not in APIS_PREFERIDAS or indice < 0 or tipo in por_tipo:
            continue
        info = pa.get_device_info_by_index(indice)
        if info.get("maxInputChannels", 0) > 0:
            por_tipo[tipo] = _candidato(info, tipo)
    return [por_tipo[tipo] for tipo in APIS_PREFERIDAS if tipo in por_tipo]


def candidatos_do_microfone(pa, nome_configurado: str) -> tuple[list[Candidato], str, list[str]]:
    """(candidatos por ordem de preferencia, origem, notas dos que ficaram de fora).

    Com nome configurado: os dispositivos de entrada cujo nome casa, MME antes
    de WASAPI (e, dentro da mesma API, pela ordem do sistema); DirectSound,
    WDM-KS e APIs desconhecidas nunca entram, e as notas dizem porque. Sem
    nome, ou sem nenhum que case, as entradas por omissao de MME e WASAPI, e a
    `origem` diz que foi isso que aconteceu (nunca se escreve no log que se
    esta a ouvir um microfone que nao foi o escolhido).
    """
    notas: list[str] = []
    if nome_configurado.strip():
        por_api: dict[int, list[Candidato]] = {tipo: [] for tipo in APIS_PREFERIDAS}
        for info in listar_dispositivos_de_entrada(pa):
            if not nome_casa(info.get("name", ""), nome_configurado):
                continue
            tipo = tipo_da_api(pa, info)
            if tipo in por_api:
                por_api[tipo].append(_candidato(info, tipo))
            else:
                motivo = MOTIVO_DA_API_EXCLUIDA.get(tipo, f"{nome_da_api(tipo)} excluida: so MME e WASAPI")
                notas.append(f"[{info['index']}] {info.get('name')}: {motivo}")
        candidatos = [c for tipo in APIS_PREFERIDAS for c in por_api[tipo]]
        if candidatos:
            return candidatos, f"configurado: '{nome_configurado}'", notas
        notas.append(f"nenhum dispositivo MME ou WASAPI casa com '{nome_configurado}'")
        return (
            _entradas_por_omissao(pa),
            f"omissao do sistema (o configurado '{nome_configurado}' nao existe em MME nem WASAPI)",
            notas,
        )
    return _entradas_por_omissao(pa), "omissao do sistema", notas


# --- Sinal e relogio ----------------------------------------------------------------


def descrever_sinal(dados: bytes) -> tuple[float, int, str]:
    """(rms, pico, veredicto) da captura. Ver a nota SINAL no docstring do modulo."""
    rms = nivel_rms(dados)
    pico = pico_pcm16(dados)
    if not dados:
        veredicto = "SEM SINAL (nao chegou nenhuma amostra do dispositivo)"
    elif pico == 0:
        veredicto = "SEM SINAL (todas as amostras a zero: microfone em mudo, sem permissao ou driver parado)"
    elif rms < 0.005:
        veredicto = "ha sinal, mas so ruido de fundo (ninguem a falar)"
    else:
        veredicto = "ha sinal audivel"
    return rms, pico, veredicto


def maior_troco_de_zeros_s(pcm16: bytes, taxa: int = TAXA_AMOSTRAGEM_PADRAO) -> float:
    """Duracao do maior troco de amostras exatamente a zero (PCM16 mono).

    Procura os trocos de bytes a zero e alinha-os as amostras (2 bytes): uma
    amostra a zero sao dois bytes a zero num offset par, por isso a parte
    alinhada de cada troco de bytes a zero e toda feita de amostras a zero.
    """
    maior = 0
    for troco in re.finditer(rb"\x00{2,}", pcm16):
        inicio = troco.start() + (troco.start() % 2)
        fim = troco.end() - (troco.end() % 2)
        maior = max(maior, (fim - inicio) // 2)
    return maior / taxa


def desacerto_com_o_relogio(audio_s: float, real_s: float, so_excesso: bool = False) -> str | None:
    """Motivo pelo qual `audio_s` de audio nao bate com `real_s` de relogio, ou None.

    Tolerancia: FOLGA_ABSOLUTA_S + FOLGA_RELATIVA do tempo real, para cada
    lado. `so_excesso` so recusa audio a mais (uma gravacao parada pela tecla
    perde o que ficou no buffer, por isso ter audio a menos e normal).
    """
    folga = FOLGA_ABSOLUTA_S + FOLGA_RELATIVA * real_s
    if audio_s > real_s + folga:
        return (
            f"leitura mais rapida que o relogio: {audio_s:.2f} s de audio em {real_s:.2f} s reais "
            f"(tolerancia {folga:.2f} s)"
        )
    if not so_excesso and audio_s < real_s - folga:
        return (
            f"leitura mais lenta que o relogio: {audio_s:.2f} s de audio em {real_s:.2f} s reais "
            f"(tolerancia {folga:.2f} s)"
        )
    return None


def defeito_da_captura(pcm16: bytes, real_s: float, so_excesso: bool = False) -> str | None:
    """Motivo pelo qual uma captura (16 kHz mono) nao presta, ou None."""
    audio_s = (len(pcm16) // 2) / TAXA_AMOSTRAGEM_PADRAO
    motivo = desacerto_com_o_relogio(audio_s, real_s, so_excesso)
    if motivo:
        return motivo
    zeros = maior_troco_de_zeros_s(pcm16)
    if zeros >= TROCO_DE_ZEROS_MAXIMO_S:
        return f"silencio digital: {zeros:.2f} s seguidos de zeros exatos (maximo {TROCO_DE_ZEROS_MAXIMO_S} s)"
    return None


# --- Abrir e ler o microfone --------------------------------------------------------


@dataclass(frozen=True)
class Escolha:
    """Um candidato com a taxa e os canais com que abriu."""

    candidato: Candidato
    taxa: int
    canais: int
    origem: str

    def descrever(self) -> str:
        c = self.candidato
        reamostra = "" if self.taxa == TAXA_AMOSTRAGEM_PADRAO else f", reamostrado de {self.taxa} Hz"
        canais = "" if self.canais == 1 else f", {self.canais} canais (fica o 1.o)"
        return f"[{c.indice}] {c.nome} via {c.api} ({self.origem}{reamostra}{canais})"


@dataclass(frozen=True)
class Prova:
    """O que a prova ao abrir leu: audio (16 kHz mono) e tempo real."""

    pcm: bytes
    real_s: float

    @property
    def audio_s(self) -> float:
        return (len(self.pcm) // 2) / TAXA_AMOSTRAGEM_PADRAO

    @property
    def zeros_s(self) -> float:
        return maior_troco_de_zeros_s(self.pcm)

    @property
    def defeito(self) -> str | None:
        return defeito_da_captura(self.pcm, self.real_s)


class EntradaDeMicrofone:
    """Um stream aberto que entrega PCM16 mono a 16 kHz, bloco a bloco.

    Le a taxa e canais com que o dispositivo abriu; fica com o primeiro canal
    e reamostra para 16 kHz com o estado do audioop.ratecv preservado entre
    blocos (sem estalidos nas juntas).
    """

    def __init__(self, stream, escolha: Escolha, amostras_por_bloco: int = TAMANHO_DO_BLOCO) -> None:
        self.escolha = escolha
        self.prova: Prova | None = None
        self.tentativas: list[str] = []
        self._stream = stream
        self._frames = max(1, round(amostras_por_bloco * escolha.taxa / TAXA_AMOSTRAGEM_PADRAO))
        self._estado_da_reamostragem = None

    def ler(self) -> bytes:
        dados = self._stream.read(self._frames, exception_on_overflow=False)
        if self.escolha.canais > 1:
            amostras = array.array("h")
            amostras.frombytes(dados[: len(dados) - len(dados) % (2 * self.escolha.canais)])
            dados = amostras[:: self.escolha.canais].tobytes()
        if self.escolha.taxa != TAXA_AMOSTRAGEM_PADRAO and dados:
            import audioop  # stdlib; aviso de depreciacao esperado em 3.12+

            dados, self._estado_da_reamostragem = audioop.ratecv(
                dados, 2, 1, self.escolha.taxa, TAXA_AMOSTRAGEM_PADRAO, self._estado_da_reamostragem
            )
        return dados

    def fechar(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop_stream()
            stream.close()
        except Exception:  # noqa: BLE001 - fechar nunca levanta
            pass


def abrir_entrada(pa, escolha: Escolha, formato, amostras_por_bloco: int = TAMANHO_DO_BLOCO) -> EntradaDeMicrofone:
    """Abre o stream de uma escolha ja provada (sem nova prova)."""
    frames = max(1, round(amostras_por_bloco * escolha.taxa / TAXA_AMOSTRAGEM_PADRAO))
    stream = pa.open(
        format=formato,
        channels=escolha.canais,
        rate=escolha.taxa,
        input=True,
        input_device_index=escolha.candidato.indice,
        frames_per_buffer=frames,
    )
    return EntradaDeMicrofone(stream, escolha, amostras_por_bloco)


def provar(entrada: EntradaDeMicrofone, segundos: float, relogio: Callable[[], float] = time.monotonic) -> Prova:
    """Le `segundos` de audio e mede quanto tempo real isso levou."""
    alvo = int(segundos * TAXA_AMOSTRAGEM_PADRAO) * 2
    blocos: list[bytes] = []
    lidos = 0
    inicio = relogio()
    while lidos < alvo:
        bloco = entrada.ler()
        if not bloco:
            break
        blocos.append(bloco)
        lidos += len(bloco)
    return Prova(b"".join(blocos), relogio() - inicio)


def abrir_microfone(
    pa,
    nome_configurado: str,
    formato,
    segundos_de_prova: float = SEGUNDOS_DE_PROVA,
    amostras_por_bloco: int = TAMANHO_DO_BLOCO,
    relogio: Callable[[], float] = time.monotonic,
) -> EntradaDeMicrofone:
    """A funcao unica de escolha: abre o primeiro candidato que passa a prova.

    Ordem: candidatos de `candidatos_do_microfone` (MME, depois WASAPI); em
    cada um, 16 kHz e depois a taxa nativa, mono e depois os canais nativos.
    Cada combinacao que abre e provada (`Prova.defeito`); a primeira sem
    defeito fica aberta e e devolvida, com a prova e as tentativas. Sem
    nenhuma, levanta MicrofoneInutilizavel com tudo o que se tentou.
    """
    candidatos, origem, tentativas = candidatos_do_microfone(pa, nome_configurado)
    for candidato in candidatos:
        for taxa in candidato.taxas:
            for canais in candidato.canais:
                escolha = Escolha(candidato, taxa, canais, origem)
                rotulo = f"[{candidato.indice}] {candidato.nome} via {candidato.api} a {taxa} Hz, {canais} canal(is)"
                try:
                    entrada = abrir_entrada(pa, escolha, formato, amostras_por_bloco)
                except Exception as erro:  # noqa: BLE001 - tenta a combinacao seguinte
                    tentativas.append(f"{rotulo}: nao abriu ({erro})")
                    continue
                try:
                    prova = provar(entrada, segundos_de_prova, relogio)
                except Exception as erro:  # noqa: BLE001
                    entrada.fechar()
                    tentativas.append(f"{rotulo}: falhou a ler ({erro})")
                    continue
                defeito = prova.defeito
                if defeito:
                    entrada.fechar()
                    tentativas.append(f"{rotulo}: recusado, {defeito}")
                    continue
                entrada.prova = prova
                entrada.tentativas = tentativas
                return entrada
    if not candidatos:
        tentativas.append("nenhum dispositivo de entrada MME ou WASAPI no sistema")
    raise MicrofoneInutilizavel(
        "nenhum microfone le em tempo real sem silencio digital; nada foi gravado. Tentativas:\n  - "
        + "\n  - ".join(tentativas)
    )


# --- Dispositivos falsos (autoteste e testes) ---------------------------------------


class RelogioFalso:
    def __init__(self) -> None:
        self.agora = 100.0

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class _StreamFalso:
    def __init__(self, dispositivo: dict, taxa: int, canais: int, relogio: RelogioFalso) -> None:
        self.dispositivo = dispositivo
        self.taxa = taxa
        self.canais = canais
        self.relogio = relogio
        self.amostra = 0
        self.fechado = False

    #: comportamento -> quantos segundos de relogio passam por segundo de audio.
    RITMO = {"tempo_real": 1.0, "mudo": 1.0, "lento": 2.0, "rapido": 0.0, "zeros": 0.0}

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        """tempo_real: tom ao ritmo do relogio; rapido: tom sem esperar;
        lento: tom a metade do ritmo; zeros: zeros sem esperar (o defeito do
        DirectSound); mudo: zeros ao ritmo do relogio."""
        comportamento = self.dispositivo.get("comportamento", "tempo_real")
        self.relogio.avancar(self.RITMO[comportamento] * frames / self.taxa)
        if comportamento in ("zeros", "mudo"):
            return b"\x00\x00" * frames * self.canais
        valores = []
        for i in range(self.amostra, self.amostra + frames):
            valor = int(6000 * math.sin(2 * math.pi * 220 * i / self.taxa)) or 1
            valores.extend([valor] * self.canais)
        self.amostra += frames
        return struct.pack(f"<{len(valores)}h", *valores)

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.fechado = True


#: Nome inventado de um microfone com mais de 31 caracteres. Nunca trocar por
#: nomes reais: este ficheiro e versionado e o repositorio e publico, por isso
#: o modelo do microfone do utilizador ficaria no historico para sempre.
NOME_FALSO = "Microfone (Exemplo Audio USB 1234 Pro)"


class PyAudioFalso:
    """Um PyAudio de mentira que reproduz o defeito medido, sem hardware.

    O mesmo microfone inventado aparece em MME (nome cortado a 31, le em
    tempo real a 16 kHz), DirectSound (nome inteiro, zeros sem esperar),
    WASAPI (nome inteiro, so 48 kHz e 2 canais) e WDM-KS. `dispositivos`
    permite trocar o comportamento de cada um nos testes.
    """

    APIS = [
        {"index": 0, "type": API_MME, "name": "MME", "defaultInputDevice": 2},
        {"index": 1, "type": API_DIRECTSOUND, "name": "Windows DirectSound", "defaultInputDevice": 3},
        {"index": 2, "type": API_WASAPI, "name": "Windows WASAPI", "defaultInputDevice": 4},
        {"index": 3, "type": API_WDMKS, "name": "Windows WDM-KS", "defaultInputDevice": 5},
    ]

    def __init__(self, dispositivos: list[dict] | None = None, relogio: RelogioFalso | None = None) -> None:
        self.relogio = relogio or RelogioFalso()
        self.dispositivos = dispositivos if dispositivos is not None else self.dispositivos_por_omissao()
        self.aberturas: list[tuple[int, int, int]] = []
        self.terminado = False

    @staticmethod
    def dispositivos_por_omissao() -> list[dict]:
        def dispositivo(indice, api, nome, taxa, canais, taxas, canais_aceites, comportamento="tempo_real"):
            return {
                "index": indice, "hostApi": api, "name": nome, "defaultSampleRate": float(taxa),
                "maxInputChannels": canais, "taxas": taxas, "canais_aceites": canais_aceites,
                "comportamento": comportamento,
            }

        return [
            dispositivo(0, 0, "Altifalantes (sem entrada)", 44100, 0, set(), set()),
            dispositivo(1, 0, "Entrada de linha (placa integrada)", 44100, 2, {16000, 44100}, {1, 2}),
            dispositivo(2, 0, NOME_FALSO[:LIMITE_DO_NOME_MME].strip(), 44100, 1, {16000, 44100}, {1}),
            dispositivo(3, 1, NOME_FALSO, 44100, 1, {16000, 44100}, {1}, "zeros"),
            dispositivo(4, 2, NOME_FALSO, 48000, 2, {48000}, {2}),
            dispositivo(5, 3, NOME_FALSO, 48000, 1, {48000}, {1}),
        ]

    def get_host_api_count(self) -> int:
        return len(self.APIS)

    def get_host_api_info_by_index(self, indice: int) -> dict:
        return self.APIS[indice]

    def get_device_count(self) -> int:
        return len(self.dispositivos)

    def get_device_info_by_index(self, indice: int) -> dict:
        return self.dispositivos[indice]

    def get_default_input_device_info(self) -> dict:
        return self.dispositivos[self.APIS[0]["defaultInputDevice"]]

    def open(self, format, channels, rate, input, input_device_index, frames_per_buffer):  # noqa: A002
        dispositivo = self.dispositivos[input_device_index]
        if rate not in dispositivo["taxas"]:
            raise OSError(f"[Errno -9997] Invalid sample rate ({rate})")
        if channels not in dispositivo["canais_aceites"]:
            raise OSError(f"[Errno -9998] Invalid number of channels ({channels})")
        self.aberturas.append((input_device_index, rate, channels))
        return _StreamFalso(dispositivo, rate, channels, self.relogio)

    def terminate(self) -> None:
        self.terminado = True


# --- Autoteste ----------------------------------------------------------------------


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    def abrir(pa: PyAudioFalso, nome: str, **kwargs) -> EntradaDeMicrofone:
        return abrir_microfone(pa, nome, formato=8, relogio=pa.relogio, **kwargs)

    # 1. So dispositivos com canais de entrada sao listados.
    pa = PyAudioFalso()
    verificar(
        "listagem: ignora dispositivos sem entrada",
        [info["index"] for info in listar_dispositivos_de_entrada(pa)],
        [1, 2, 3, 4, 5],
    )

    # 2. Nomes: inteiro, parte, cortado pelo MME, sem maiusculas.
    verificar("nome: contido casa", nome_casa(NOME_FALSO, "audio usb 1234"), True)
    verificar("nome: cortado a 31 casa pelo prefixo", nome_casa(NOME_FALSO[:31], NOME_FALSO), True)
    verificar("nome: prefixo curto demais nao casa", nome_casa("Microfone", NOME_FALSO), False)
    verificar("nome: outro dispositivo nao casa", nome_casa("Entrada de linha (placa integrada)", NOME_FALSO), False)

    # 3. Preferencia de API: MME, depois WASAPI; nunca DirectSound nem WDM-KS.
    candidatos, origem, notas = candidatos_do_microfone(pa, NOME_FALSO)
    verificar("candidatos: MME antes de WASAPI, sem DS nem WDM-KS", [c.api for c in candidatos], ["MME", "WASAPI"])
    verificar("candidatos: DS e WDM-KS ficam nas notas", sum("excluida" in n for n in notas), 2)
    verificar("candidatos: origem configurada", origem, f"configurado: '{NOME_FALSO}'")

    # 4. O caso medido: MME le em tempo real a 16 kHz e e escolhido.
    entrada = abrir(pa, NOME_FALSO)
    verificar("abrir: MME a 16 kHz mono", (entrada.escolha.candidato.api, entrada.escolha.taxa), ("MME", 16000))
    verificar("abrir: prova sem defeito", entrada.prova.defeito, None)
    entrada.fechar()

    # 5. Sem MME: WASAPI a 48 kHz e 2 canais, reamostrado para 16 kHz mono.
    pa = PyAudioFalso()
    pa.dispositivos[2]["taxas"] = set()
    entrada = abrir(pa, NOME_FALSO)
    verificar("abrir: WASAPI a 48 kHz quando o MME nao abre", (entrada.escolha.candidato.api, entrada.escolha.taxa, entrada.escolha.canais), ("WASAPI", 48000, 2))
    verificar("abrir: reamostrado para ~1 s a 16 kHz", abs(entrada.prova.audio_s - 1.0) < 0.1, True)
    entrada.fechar()

    # 6. Leitura mais rapida que o relogio e recusada; sem candidato bom, falha.
    pa = PyAudioFalso()
    pa.dispositivos[2]["comportamento"] = "rapido"
    pa.dispositivos[4]["comportamento"] = "zeros"
    try:
        abrir(pa, NOME_FALSO)
        falhas.append("abrir: aceitou um microfone que le mais rapido que o relogio")
    except MicrofoneInutilizavel as erro:
        texto = str(erro)
        verificar("abrir: falha com a tentativa 'mais rapida'", "mais rapida que o relogio" in texto, True)
        verificar("abrir: falha com a tentativa de zeros", "leitura mais rapida" in texto or "silencio digital" in texto, True)
        verificar("abrir: nunca tentou DirectSound", any(i == 3 for i, _, _ in pa.aberturas), False)

    # 7. Relogio e zeros.
    verificar("relogio: 1 s em 1 s aceite", desacerto_com_o_relogio(1.0, 1.0), None)
    verificar("relogio: 20 s em 0,1 s recusado", desacerto_com_o_relogio(20.0, 0.1) is not None, True)
    verificar("relogio: 0,3 s em 2 s recusado", desacerto_com_o_relogio(0.3, 2.0) is not None, True)
    verificar("relogio: audio a menos aceite com so_excesso", desacerto_com_o_relogio(0.3, 2.0, so_excesso=True), None)
    fala = struct.pack("<4h", 900, -900, 3, -1)
    verificar("zeros: nenhum", maior_troco_de_zeros_s(fala * 4000), 0.0)
    verificar("zeros: 0,5 s exatos", maior_troco_de_zeros_s(fala + b"\x00\x00" * 8000 + fala), 0.5)
    verificar("zeros: bytes a zero desalinhados nao contam", (maior_troco_de_zeros_s(struct.pack("<2h", 1, 256)), maior_troco_de_zeros_s(struct.pack("<3h", 256, 0, 1))), (0.0, 1 / 16000))

    # 8. Veredicto de sinal: o caso que o RMS sozinho nao distinguia.
    rms, pico, veredicto = descrever_sinal(struct.pack("<8h", *([0] * 8)))
    verificar("sinal: silencio digital e marcado SEM SINAL", (pico, veredicto.startswith("SEM SINAL")), (0, True))
    rms, pico, veredicto = descrever_sinal(struct.pack("<8h", 1, -1, 2, -1, 0, 1, -2, 1))
    verificar("sinal: ruido de fundo NAO e SEM SINAL", (round(rms, 4), pico, veredicto.startswith("SEM SINAL")), (0.0, 2, False))
    verificar("sinal: captura vazia nao rebenta", descrever_sinal(b"")[1], 0)

    # 9. Limites do --segundos e do --saida.
    def segundos_recusados(valor: str) -> bool:
        try:
            segundos_de_captura(valor)
        except argparse.ArgumentTypeError:
            return True
        return False

    verificar("segundos: teto aceite, acima recusado", (segundos_de_captura("60"), segundos_recusados("1e9")), (60.0, True))
    verificar("segundos: zero, negativos e nan recusados", [segundos_recusados(v) for v in ("0", "-1", "nan", "tres")], [True] * 4)

    def saida_recusada(valor: str) -> bool:
        try:
            caminho_wav_de_saida(valor)
        except ValueError:
            return True
        return False

    verificar("saida: default em audio/ aceite", caminho_wav_de_saida(PASTA_AUDIO / "m.wav").parent, PASTA_AUDIO)
    verificar("saida: fora do repo ou sem .wav recusada", (saida_recusada("audio/../../fora.wav"), saida_recusada("notas.txt")), (True, True))

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print(
        "OK: autoteste do verificar_microfone completo "
        "(nomes, preferencia de API, prova de tempo real, zeros, sinal e limites)."
    )
    return 0


def main() -> int:
    forcar_consola_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dispositivo",
        default=None,
        help=f"nome (ou parte) do dispositivo de entrada; por omissao le {VARIAVEL_NOME_MICROFONE}",
    )
    parser.add_argument(
        "--segundos",
        type=segundos_de_captura,
        default=SEGUNDOS_DE_CAPTURA_PADRAO,
        help=f"duracao da captura, de 0 a {TETO_DE_SEGUNDOS_DE_CAPTURA:.0f} s (default: 3)",
    )
    parser.add_argument(
        "--saida",
        default=None,
        help=(
            "WAV a escrever, dentro do repositorio e com sufixo .wav "
            "(default: audio/microfone-<ts>.wav)"
        ),
    )
    parser.add_argument(
        "--autoteste",
        action="store_true",
        help="corre a regressao da escolha de dispositivo e da prova (dispositivos falsos, sem microfone)",
    )
    args = parser.parse_args()

    if args.autoteste:
        return _autoteste()

    nome_configurado = args.dispositivo or os.environ.get(VARIAVEL_NOME_MICROFONE, "").strip()

    # O que se grava aqui e a VOZ do utilizador: o caminho de saida passa pelo
    # guarda (dentro do repositorio, sufixo .wav) ANTES de se abrir o
    # microfone — um destino invalido nunca chega a gravar nada.
    try:
        saida = caminho_wav_de_saida(
            args.saida or PASTA_AUDIO / f"microfone-{int(time.time())}.wav"
        )
    except ValueError as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        return 1

    print("=== jarvis - verificar_microfone ===")
    try:
        import pyaudio
    except Exception as erro:
        print(f"ERRO: nao foi possivel importar o pyaudio: {erro!r}", file=sys.stderr)
        return 1

    try:
        pa = pyaudio.PyAudio()
    except Exception as erro:
        print(f"ERRO: nao foi possivel iniciar o PyAudio: {erro!r}", file=sys.stderr)
        return 1

    try:
        dispositivos = listar_dispositivos_de_entrada(pa)
        print(f"dispositivos de entrada = {len(dispositivos)}")
        for info in dispositivos:
            print(
                f"  [{info['index']}] {info['name']} ({nome_da_api(tipo_da_api(pa, info))}, "
                f"taxa por omissao {int(info['defaultSampleRate'])} Hz, canais {info['maxInputChannels']})"
            )
        print(f"a abrir e capturar {args.segundos:.1f} s...")
        try:
            entrada = abrir_microfone(pa, nome_configurado, pyaudio.paInt16, segundos_de_prova=args.segundos)
        except MicrofoneInutilizavel as erro:
            print(f"ERRO: {erro}", file=sys.stderr)
            return 1
        entrada.fechar()
    except Exception as erro:
        print(f"ERRO: falha no caminho do microfone: {erro!r}", file=sys.stderr)
        return 1
    finally:
        pa.terminate()

    for tentativa in entrada.tentativas:
        print(f"  saltado: {tentativa}")
    prova = entrada.prova
    try:
        escrever_wav_pcm16(saida, prova.pcm, TAXA_AMOSTRAGEM_PADRAO, canais=1)
    except Exception as erro:
        print(f"ERRO: falha ao escrever o WAV de captura: {erro!r}", file=sys.stderr)
        return 1

    rms, pico, veredicto = descrever_sinal(prova.pcm)
    print(f"dispositivo     = {entrada.escolha.descrever()}")
    print(f"tempo real      = {prova.real_s:.2f} s; audio lido = {prova.audio_s:.2f} s")
    print(f"gravado em      = {caminho_para_mostrar(saida)}")
    print(f"nivel RMS       = {rms:.4f}")
    print(f"pico            = {pico} de 32768")
    print(f"sinal           = {veredicto}")
    print("OK: caminho do microfone verificado.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as erro:  # ultima rede: "sem rebentar" e o criterio de aceitacao
        print(f"ERRO inesperado: {erro!r}", file=sys.stderr)
        sys.exit(1)
