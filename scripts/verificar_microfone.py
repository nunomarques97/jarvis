r"""Verifica o caminho do microfone: dispositivos, captura de 3 s, nivel RMS (D33).

Sem isto nao ha como confiar em nenhuma transcricao ao vivo mais tarde: e o
unico dos tres utilitarios da T3 que toca em hardware real, por isso nunca
levanta uma excecao para fora de main() — reporta o erro e sai com codigo 1
("sem rebentar" e o criterio de aceitacao).

Dispositivo escolhido: o nome vindo de --dispositivo, senao da variavel de
ambiente JARVIS_NOME_MICROFONE (o sistema de configuracao fica para a T4; ate
la, mesma convencao que jarvis.canal_claude.VARIAVEL_TITULO_JANELA), senao o
dispositivo de entrada por omissao do sistema.

Uso do PyAudio simples (nao PyAudioWPatch, que o TECHNOLOGY.md S5 preferia mas
nao esta instalado neste venv — ver docs/MODELOS.md/relatorio): o PyAudio ja e
dependencia transitiva do RealtimeSTT (`pip show pyaudio` -> `Required-by:
realtimestt`, D36) e chega para listar dispositivos e capturar de um deles;
nenhuma dependencia nova.

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
import os
import struct
import sys
import time
from pathlib import Path

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

VARIAVEL_NOME_MICROFONE = "JARVIS_NOME_MICROFONE"
SEGUNDOS_DE_CAPTURA_PADRAO = 3.0
TAMANHO_DO_BLOCO = 1024

#: Teto da captura. `capturar()` acumula a gravacao inteira em memoria antes de
#: escrever o WAV (`b"".join`), por isso um `--segundos 1e9` enchia a RAM e o
#: disco. Um minuto e muito mais do que os 3 s que este utilitario precisa, e a
#: T4 vai reutilizar a mesma funcao.
TETO_DE_SEGUNDOS_DE_CAPTURA = 60.0


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


def escolher_dispositivo(pa, nome_configurado: str) -> tuple[dict, str]:
    """O dispositivo cujo nome contem `nome_configurado`, ou o por omissao do sistema.

    Devolve (info, origem). A `origem` descreve o que aconteceu de facto e nao
    o que foi pedido: com um nome configurado que nao casa com dispositivo
    nenhum, a linha do log tem de dizer que se caiu no dispositivo do sistema,
    senao a T4 escreve no log que esta a ouvir um microfone que nao escolheu.
    """
    if nome_configurado:
        alvo = nome_configurado.strip().lower()
        for info in listar_dispositivos_de_entrada(pa):
            if alvo in info["name"].lower():
                return info, f"configurado: '{nome_configurado}'"
        print(
            f"aviso: nenhum dispositivo de entrada contem '{nome_configurado}'; "
            "a usar o dispositivo do sistema por omissao"
        )
        return (
            pa.get_default_input_device_info(),
            f"omissao do sistema (o configurado '{nome_configurado}' nao existe)",
        )
    return pa.get_default_input_device_info(), "omissao do sistema"


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


def capturar(pa, formato, info_dispositivo: dict, segundos: float) -> bytes:
    stream = pa.open(
        format=formato,
        channels=1,
        rate=TAXA_AMOSTRAGEM_PADRAO,
        input=True,
        input_device_index=info_dispositivo["index"],
        frames_per_buffer=TAMANHO_DO_BLOCO,
    )
    blocos = []
    try:
        n_blocos = max(1, int(TAXA_AMOSTRAGEM_PADRAO / TAMANHO_DO_BLOCO * segundos))
        for _ in range(n_blocos):
            blocos.append(stream.read(TAMANHO_DO_BLOCO, exception_on_overflow=False))
    finally:
        stream.stop_stream()
        stream.close()
    return b"".join(blocos)


# --- Autoteste: escolha de dispositivo e veredicto de sinal ---------------


class _PyAudioFalso:
    """Um PyAudio de mentira com tres dispositivos, para testar a escolha sem
    hardware. So implementa o que `escolher_dispositivo` usa.

    Os nomes sao INVENTADOS de proposito e nunca podem ser trocados pelos que a
    listagem desta maquina imprime: este ficheiro e versionado e o repositorio
    vai ser publico, por isso o modelo do headset ou da placa de som do Sponsor
    ficaria no historico para sempre (D10 — o nome do microfone vive so em
    configuracao local ignorada pelo Git). Ao mexer nesta lista, confirmar que
    nenhum destes nomes casa com um dispositivo real da maquina onde se esta.
    """

    DISPOSITIVOS = [
        {"index": 0, "name": "Altifalantes (sem entrada)", "maxInputChannels": 0},
        {"index": 1, "name": "Microfone USB (generico)", "maxInputChannels": 1},
        {"index": 2, "name": "Entrada de linha (placa integrada)", "maxInputChannels": 2},
    ]

    def get_device_count(self) -> int:
        return len(self.DISPOSITIVOS)

    def get_device_info_by_index(self, indice: int) -> dict:
        return self.DISPOSITIVOS[indice]

    def get_default_input_device_info(self) -> dict:
        return self.DISPOSITIVOS[1]


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    pa = _PyAudioFalso()

    # 1. So dispositivos com canais de entrada sao candidatos.
    verificar(
        "listagem: ignora dispositivos sem entrada",
        [info["index"] for info in listar_dispositivos_de_entrada(pa)],
        [1, 2],
    )

    # 2. Escolha e rotulo de origem, nos tres casos possiveis.
    info, origem = escolher_dispositivo(pa, "")
    verificar("escolha: sem nome configurado usa o do sistema", (info["index"], origem), (1, "omissao do sistema"))

    # Maiusculas trocadas de proposito (o nome inventado tem "placa integrada"
    # em minusculas): e isto que prova o casamento por substring sem distinguir
    # maiusculas, e escolhe o dispositivo 2 em vez do 1, que e o do sistema.
    info, origem = escolher_dispositivo(pa, "Placa Integrada")
    verificar(
        "escolha: nome configurado casa por substring sem distinguir maiusculas",
        (info["index"], origem),
        (2, "configurado: 'Placa Integrada'"),
    )

    info, origem = escolher_dispositivo(pa, "NAO_EXISTE_XPTO")
    verificar(
        "escolha: nome que nao existe cai no sistema E DI-LO na origem",
        (info["index"], origem),
        (1, "omissao do sistema (o configurado 'NAO_EXISTE_XPTO' nao existe)"),
    )

    # 3. Veredicto de sinal: o caso que o RMS sozinho nao distinguia.
    silencio_digital = struct.pack("<8h", *([0] * 8))
    ruido_de_fundo = struct.pack("<8h", 1, -1, 2, -1, 0, 1, -2, 1)
    fala = struct.pack("<8h", *([9000, -9000] * 4))

    rms, pico, veredicto = descrever_sinal(silencio_digital)
    verificar("sinal: silencio digital tem pico 0", pico, 0)
    verificar("sinal: silencio digital e marcado SEM SINAL", veredicto.startswith("SEM SINAL"), True)

    rms, pico, veredicto = descrever_sinal(ruido_de_fundo)
    verificar("sinal: ruido de fundo tem RMS que arredonda a 0.0000", round(rms, 4), 0.0)
    verificar("sinal: ...mas NAO e marcado SEM SINAL (pico 2)", (pico, veredicto.startswith("SEM SINAL")), (2, False))

    rms, pico, veredicto = descrever_sinal(fala)
    verificar("sinal: sinal forte e marcado audivel", veredicto, "ha sinal audivel")
    verificar("sinal: captura vazia nao rebenta", descrever_sinal(b"")[1], 0)

    # 4. Limites do --segundos (a gravacao inteira vive em memoria ate ser escrita).
    def segundos_recusados(valor: str) -> bool:
        try:
            segundos_de_captura(valor)
        except argparse.ArgumentTypeError:
            return True
        return False

    verificar("segundos: valor normal aceite", segundos_de_captura("3"), 3.0)
    verificar("segundos: o proprio teto e aceite", segundos_de_captura("60"), 60.0)
    verificar("segundos: acima do teto recusado", segundos_recusados("1e9"), True)
    verificar(
        "segundos: zero e negativos recusados",
        (segundos_recusados("0"), segundos_recusados("-1")),
        (True, True),
    )
    verificar("segundos: texto que nao e numero recusado", segundos_recusados("tres"), True)
    verificar("segundos: nan recusado", segundos_recusados("nan"), True)

    # 5. O guarda do --saida visto deste script (a mecanica esta testada em
    # jarvis.audio_util): a voz do Sponsor nao sai do repositorio nem perde o
    # sufixo que o .gitignore apanha.
    def saida_recusada(valor: str) -> bool:
        try:
            caminho_wav_de_saida(valor)
        except ValueError:
            return True
        return False

    verificar(
        "saida: o default em audio/ e aceite",
        caminho_wav_de_saida(PASTA_AUDIO / "m.wav").parent,
        PASTA_AUDIO,
    )
    verificar("saida: travessia para fora do repo recusada", saida_recusada("audio/../../fora.wav"), True)
    verificar("saida: a voz do Sponsor num .txt e recusada", saida_recusada("notas.txt"), True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print(
        "OK: autoteste do verificar_microfone completo "
        "(escolha, origem, sinal, limites de captura e de saida)."
    )
    return 0


def main() -> int:
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
        help="corre a regressao da escolha de dispositivo e do veredicto de sinal (sem tocar no microfone)",
    )
    args = parser.parse_args()

    if args.autoteste:
        return _autoteste()

    nome_configurado = args.dispositivo or os.environ.get(VARIAVEL_NOME_MICROFONE, "").strip()

    # O que se grava aqui e a VOZ do Sponsor: o caminho de saida passa pelo
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
        try:
            dispositivos = listar_dispositivos_de_entrada(pa)
        except Exception as erro:
            print(f"ERRO: nao foi possivel listar dispositivos de entrada: {erro!r}", file=sys.stderr)
            return 1

        print(f"dispositivos de entrada = {len(dispositivos)}")
        for info in dispositivos:
            print(
                f"  [{info['index']}] {info['name']} "
                f"(taxa por omissao {int(info['defaultSampleRate'])} Hz, "
                f"canais {info['maxInputChannels']})"
            )
        if not dispositivos:
            print("ERRO: nenhum dispositivo de entrada encontrado no sistema.", file=sys.stderr)
            return 1

        try:
            escolhido, origem = escolher_dispositivo(pa, nome_configurado)
        except Exception as erro:
            print(f"ERRO: nao foi possivel escolher um dispositivo de entrada: {erro!r}", file=sys.stderr)
            return 1

        print(f"dispositivo escolhido = [{escolhido['index']}] {escolhido['name']} ({origem})")

        print(f"a capturar {args.segundos:.1f} s...")
        try:
            dados = capturar(pa, pyaudio.paInt16, escolhido, args.segundos)
        except Exception as erro:
            print(f"ERRO: falha ao capturar audio do dispositivo escolhido: {erro!r}", file=sys.stderr)
            return 1
    finally:
        pa.terminate()

    try:
        escrever_wav_pcm16(saida, dados, TAXA_AMOSTRAGEM_PADRAO, canais=1)
    except Exception as erro:
        print(f"ERRO: falha ao escrever o WAV de captura: {erro!r}", file=sys.stderr)
        return 1

    rms, pico, veredicto = descrever_sinal(dados)
    print(f"gravado em      = {caminho_para_mostrar(saida)}")
    print(f"nivel RMS       = {rms:.4f}")
    print(f"pico            = {pico} de 32768")
    print(f"sinal           = {veredicto}")
    print("OK: caminho do microfone verificado.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as erro:  # ultima rede: "sem rebentar" e o criterio da T3
        print(f"ERRO inesperado: {erro!r}", file=sys.stderr)
        sys.exit(1)
