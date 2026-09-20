r"""Utilitarios de audio partilhados pelos tres scripts da T3.

Junta o que scripts/gerar_wav.py, scripts/transcrever_ficheiro.py e
scripts/verificar_microfone.py tinham em comum: os caminhos-padrao (audio/,
models/piper, models/faster-whisper), leitura/escrita de WAV PCM de 16 bits, e
um reamostrador feito so com a biblioteca padrao (audioop.ratecv), para nao
juntar nenhuma dependencia nova (nem sequer resampy, que a instalacao da
RealtimeTTS 0.8.5 trouxe como transitiva mas que este modulo nao usa).

`registar_dlls_do_torch` e uma copia deliberada da funcao homonima em
scripts/verificar_ambiente.py (D42, T1): esse ficheiro nao e um pacote
importavel (vive em scripts/, sem __init__.py) e a T1 ja esta fechada, por
isso o codigo repete-se aqui em vez de o scripts/verificar_ambiente.py passar
a depender de outra coisa fora do seu ambito. A logica tem de ficar identica
nos dois sitios: torna visiveis ao CTranslate2 as DLLs de CUDA que vem no
wheel do torch, e tem de correr ANTES de importar faster_whisper/ctranslate2
quando o device e "cuda" (regra da D42).
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA_AUDIO = RAIZ / "audio"
PASTA_MODELOS_PIPER = RAIZ / "models" / "piper"
PASTA_MODELOS_FASTER_WHISPER = RAIZ / "models" / "faster-whisper"

#: Taxa de amostragem que os tres scripts desta task usam (D33/criterio da T3).
TAXA_AMOSTRAGEM_PADRAO = 16_000


def garantir_pasta(pasta: Path) -> Path:
    """mkdir -p e devolve a propria pasta, para encadear numa expressao."""
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


#: Sufixo obrigatorio de qualquer WAV que estes scripts escrevam. A regra
#: `*.wav` do .gitignore apanha-o em qualquer pasta; um `--saida notas.txt`
#: nao era apanhado por regra nenhuma, e o conteudo destes ficheiros e a voz
#: do Sponsor (D10/D31, repositorio destinado a ser publico pela D1).
SUFIXO_DE_SAIDA_PERMITIDO = ".wav"


def caminho_wav_de_saida(valor: str | Path, raiz: Path | None = None) -> Path:
    """Valida um caminho de saida vindo de fora (a linha de comandos hoje; a
    configuracao na T4, e ai e entrada externa pela D48(2)).

    Duas regras, as duas pela mesma razao: o que se escreve nestes ficheiros e
    a voz do Sponsor, e este repositorio vai ser publico.

    1. fica dentro da raiz do repositorio. Sem isto, `--saida audio/../../x.wav`
       escreve fora dele (e `escrever_wav_pcm16` cria as pastas todas pelo
       caminho, com os privilegios do utilizador);
    2. acaba em `.wav`, a unica extensao que o .gitignore cobre em toda a arvore.

    Devolve o caminho absoluto ja normalizado; levanta ValueError se alguma das
    duas falhar. Nao cria nem toca no ficheiro.
    """
    raiz = (RAIZ if raiz is None else Path(raiz)).resolve()
    caminho = Path(valor).expanduser()
    if not caminho.is_absolute():
        caminho = raiz / caminho
    caminho = caminho.resolve()
    if caminho.suffix.lower() != SUFIXO_DE_SAIDA_PERMITIDO:
        raise ValueError(
            f"saida '{valor}': so se escrevem ficheiros {SUFIXO_DE_SAIDA_PERMITIDO} "
            "(e a unica extensao que o .gitignore deste repositorio apanha, e estes "
            "ficheiros levam a voz do Sponsor)"
        )
    if not caminho.is_relative_to(raiz):
        raise ValueError(
            f"saida '{valor}' aponta para fora do repositorio ({caminho}); "
            f"escolher um caminho dentro de {raiz}"
        )
    return caminho


def caminho_para_mostrar(caminho: Path) -> Path:
    """O caminho como se escreve num log: relativo a raiz do repositorio.

    `caminho_wav_de_saida` devolve caminhos absolutos (e tem de devolver, para
    poder confinar a escrita), mas imprimi-los mete o caminho do disco do
    Sponsor em todos os logs e relatorios — exatamente o que a D10 nao quer ver
    a circular. Fora do repositorio devolve o caminho tal e qual.
    """
    try:
        return Path(caminho).resolve().relative_to(RAIZ)
    except ValueError:
        return Path(caminho)


def registar_dlls_do_torch() -> list[str]:
    """Torna as DLLs de CUDA que vieram no wheel do torch visiveis ao CTranslate2.

    Tudo dentro do .venv: nada e instalado ou registado no Windows (D14f).
    Copia de scripts/verificar_ambiente.py::registar_dlls_do_torch (D42) — ver
    o aviso no docstring do modulo sobre porque nao e importada de la.
    """
    registadas: list[str] = []
    if os.name != "nt":
        return registadas
    import torch  # noqa: F401  (o import do torch ja adiciona a sua pasta lib)

    candidatas = [Path(torch.__file__).parent / "lib"]
    pacotes = Path(torch.__file__).parent.parent
    nvidia = pacotes / "nvidia"
    if nvidia.is_dir():
        candidatas += sorted(nvidia.glob("*/bin"))
    for pasta in candidatas:
        if pasta.is_dir():
            try:
                os.add_dll_directory(str(pasta))
                registadas.append(pasta.name)
            except OSError:
                pass
    return registadas


def ler_wav_pcm16(caminho: Path) -> tuple[bytes, int, int]:
    """Le um WAV PCM de 16 bits. Devolve (dados, taxa_de_amostragem, canais)."""
    with wave.open(str(caminho), "rb") as wf:
        largura = wf.getsampwidth()
        if largura != 2:
            raise ValueError(
                f"{caminho}: esperava PCM de 16 bits (sampwidth=2), encontrei {largura * 8} bits"
            )
        canais = wf.getnchannels()
        taxa = wf.getframerate()
        dados = wf.readframes(wf.getnframes())
    return dados, taxa, canais


def escrever_wav_pcm16(caminho: Path, dados: bytes, taxa: int, canais: int = 1) -> None:
    """Escreve PCM de 16 bits num WAV, criando a pasta de destino se preciso."""
    garantir_pasta(caminho.parent)
    with wave.open(str(caminho), "wb") as wf:
        wf.setnchannels(canais)
        wf.setsampwidth(2)
        wf.setframerate(taxa)
        wf.writeframes(dados)


def reamostrar_pcm16(dados: bytes, taxa_de: int, taxa_para: int, canais: int = 1) -> bytes:
    """Reamostra PCM de 16 bits com audioop (stdlib) — sem dependencia nova.

    Usado pelo gerar_wav.py para descer os 22050 Hz nativos da voz Piper
    (models/piper/pt_PT-tugao-medium.onnx.json) para os 16 kHz mono exigidos
    pelo criterio da T3. audioop esta descontinuado desde o Python 3.13, mas
    corre neste .venv (3.12) sem alternativa nova a instalar.
    """
    if taxa_de == taxa_para:
        return dados
    import audioop  # stdlib; aviso de depreciacao esperado em 3.12+

    convertidos, _estado_final = audioop.ratecv(dados, 2, canais, taxa_de, taxa_para, None)
    return convertidos


def nivel_rms(dados: bytes) -> float:
    """RMS normalizado em [0, 1] de PCM de 16 bits assinado (audioop, stdlib)."""
    if not dados:
        return 0.0
    import audioop  # stdlib; aviso de depreciacao esperado em 3.12+

    return audioop.rms(dados, 2) / 32768.0


def pico_pcm16(dados: bytes) -> int:
    """Maior amplitude absoluta (0..32768) de PCM de 16 bits assinado.

    Existe porque o RMS arredondado nao distingue "ninguem a falar" (amostras
    de +-1 a +-30, RMS 0.0000) de "silencio digital" — microfone em mudo, sem
    permissao ou driver a devolver zeros —, e essa diferenca e a unica coisa
    que o scripts/verificar_microfone.py tem para dizer se o caminho do
    microfone esta mesmo vivo.
    """
    if not dados:
        return 0
    import audioop  # stdlib; aviso de depreciacao esperado em 3.12+

    return audioop.max(dados, 2)


# --- Autoteste das partes puras (sem GPU, sem Piper, sem microfone) --------


def _autoteste() -> int:
    """Verifica o resample, o RMS e o WAV round-trip, mesma convencao de
    jarvis/canal_claude.py --autoteste: um `verificar(nome, obtido, esperado)`
    por afirmacao, sem tocar em GPU, em Piper nem em microfone.
    """
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    # 1. WAV round-trip: o que se escreve e exatamente o que se le de volta.
    amostras_originais = struct.pack("<4h", 100, -200, 32000, -32000)
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "sub" / "roundtrip.wav"
        escrever_wav_pcm16(caminho, amostras_originais, 16_000, canais=1)
        verificar("wav round-trip: pasta criada", caminho.is_file(), True)
        dados, taxa, canais = ler_wav_pcm16(caminho)
        verificar("wav round-trip: bytes identicos", dados, amostras_originais)
        verificar("wav round-trip: taxa preservada", taxa, 16_000)
        verificar("wav round-trip: canais preservados", canais, 1)

    # 2. reamostrar_pcm16: taxa igual devolve os bytes tal e qual (sem passar por audioop).
    verificar(
        "resample: taxa igual e identidade",
        reamostrar_pcm16(amostras_originais, 16_000, 16_000),
        amostras_originais,
    )

    # 3. reamostrar_pcm16: descer de 22050 para 16000 Hz encolhe a contagem de
    # amostras na proporcao esperada (16000/22050), dentro de uma tolerancia
    # pequena — isto e o que gerar_wav.py faz a seguir ao Piper (nativo 22050 Hz).
    meio_segundo_22050 = struct.pack("<%dh" % 11_025, *([1000, -1000] * 5_512 + [1000]))
    reamostrado = reamostrar_pcm16(meio_segundo_22050, 22_050, 16_000)
    n_amostras_originais = len(meio_segundo_22050) // 2
    n_amostras_esperado = round(n_amostras_originais * 16_000 / 22_050)
    n_amostras_obtido = len(reamostrado) // 2
    verificar(
        "resample: 22050->16000 encolhe na proporcao certa (tolerancia 2 amostras)",
        abs(n_amostras_obtido - n_amostras_esperado) <= 2,
        True,
    )
    verificar("resample: muda mesmo os bytes (nao e um no-op)", reamostrado != meio_segundo_22050, True)

    # 4. nivel_rms: um sinal constante tem RMS igual ao proprio valor (formula
    # do RMS, nao uma reafirmacao da implementacao): constante 16384 -> 0.5.
    constante_meia_escala = struct.pack("<100h", *([16384] * 100))
    verificar("rms: sinal constante = metade da escala", round(nivel_rms(constante_meia_escala), 4), 0.5)
    verificar("rms: silencio total = 0.0", nivel_rms(struct.pack("<10h", *([0] * 10))), 0.0)
    verificar("rms: bytes vazios nao rebenta", nivel_rms(b""), 0.0)

    # 4b. pico_pcm16: distingue silencio digital (tudo zeros) de um sinal
    # minimo de +-1, que e exatamente o caso que o RMS a 4 casas confunde.
    quase_silencio = struct.pack("<8h", 0, 1, 0, -1, 0, 1, 0, -1)
    verificar("pico: silencio digital = 0", pico_pcm16(struct.pack("<8h", *([0] * 8))), 0)
    verificar("pico: sinal minimo de +-1 = 1", pico_pcm16(quase_silencio), 1)
    verificar(
        "pico: o RMS a 4 casas nao distingue os dois casos acima",
        round(nivel_rms(quase_silencio), 4),
        0.0,
    )
    verificar("pico: maximo negativo conta pelo valor absoluto", pico_pcm16(struct.pack("<2h", 5, -300)), 300)
    verificar("pico: bytes vazios nao rebenta", pico_pcm16(b""), 0)

    # 4c. caminho_wav_de_saida: confina a escrita ao repositorio e exige .wav.
    with tempfile.TemporaryDirectory() as pasta:
        raiz_falsa = Path(pasta).resolve()
        verificar(
            "saida: caminho relativo resolve dentro da raiz",
            caminho_wav_de_saida("audio/x.wav", raiz_falsa),
            raiz_falsa / "audio" / "x.wav",
        )
        verificar(
            "saida: sufixo aceite sem distinguir maiusculas",
            caminho_wav_de_saida("audio/X.WAV", raiz_falsa),
            raiz_falsa / "audio" / "X.WAV",
        )

        def recusa(valor: str) -> bool:
            try:
                caminho_wav_de_saida(valor, raiz_falsa)
            except ValueError:
                return True
            return False

        verificar("saida: travessia com .. e recusada", recusa("audio/../../fora/x.wav"), True)
        verificar(
            "saida: caminho absoluto fora da raiz e recusado",
            recusa(str(raiz_falsa.parent / "fora-da-raiz.wav")),
            True,
        )
        verificar("saida: extensao que o .gitignore nao apanha e recusada", recusa("notas.txt"), True)
        verificar("saida: sem extensao nenhuma e recusado", recusa("audio/captura"), True)
        verificar(
            "saida: nenhuma das recusas criou nada no disco",
            sorted(item.name for item in raiz_falsa.iterdir()),
            [],
        )

    # 4d. caminho_para_mostrar: o que vai para os logs nao leva o caminho do
    # disco do Sponsor (D10).
    verificar(
        "mostrar: dentro do repositorio fica relativo",
        caminho_para_mostrar(RAIZ / "audio" / "x.wav"),
        Path("audio") / "x.wav",
    )
    verificar(
        "mostrar: fora do repositorio fica como esta",
        caminho_para_mostrar(Path(tempfile.gettempdir()) / "x.wav"),
        Path(tempfile.gettempdir()) / "x.wav",
    )

    # 5. garantir_pasta: cria pastas encadeadas e e idempotente.
    with tempfile.TemporaryDirectory() as pasta:
        alvo = Path(pasta) / "a" / "b" / "c"
        verificar("garantir_pasta: devolve a propria pasta", garantir_pasta(alvo), alvo)
        verificar("garantir_pasta: criou no disco", alvo.is_dir(), True)
        garantir_pasta(alvo)  # nao pode levantar excecao na segunda chamada
        verificar("garantir_pasta: idempotente", alvo.is_dir(), True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do audio_util completo (resample, RMS, WAV round-trip).")
    return 0


if __name__ == "__main__":
    if "--autoteste" in sys.argv[1:]:
        sys.exit(_autoteste())
    print(__doc__)
    print("Autoteste: python -m jarvis.audio_util --autoteste")
