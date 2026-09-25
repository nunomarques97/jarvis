r"""Voz de resposta do jarvis: falar(texto) pelo motor de voz residente.

MOTOR RESIDENTE: o motor de sintese carrega UMA vez por processo (no primeiro
uso, ou em `aquecer()` no arranque do jarvis) e fica em memoria; cada frase e
so inferencia, sem arrancar processo nenhum. Dois motores, os dois locais e
dentro do processo (onnxruntime em CPU), escolhidos pela LINGUA DA VOZ
(`lingua_da_voz()`, "pt" por omissao porque as respostas sao escritas em
portugues), nunca pelo que estiver instalado:

  - "pt": Piper `pt_PT-tugao-medium` pela biblioteca `piper-tts`;
  - "en": Kokoro-82M (`kokoro-onnx`), voz inglesa natural, quando o pacote e
    os ficheiros do modelo em `models/kokoro/` estao presentes e a primeira
    sintese passa; senao o Piper, com o motivo na descricao do motor.

Ver docs/MODELOS.md para os ficheiros, os URLs e os sha256.

STREAMING: o texto e partido em pedacos (frase a frase, e a primeira frase
comprida na primeira virgula); a sintese corre numa thread que enche uma fila
e a reproducao toca cada bloco assim que chega, por isso a resposta comeca a
soar antes de a frase inteira estar sintetizada. `ResultadoFala.primeiro_audio`
guarda o instante do primeiro bloco: e o inicio da resposta falada.

FALLBACK EXPLICITO: `falar()` nunca levanta. Qualquer falha na
sintese, na reproducao ou na escrita do ficheiro e apanhada, o texto sai
impresso na consola como resposta de recurso, e o `ResultadoFala` devolvido
tem `falou=False` com `motivo_falha` preenchido — e assim que o utilizador
continua a "ouvir" a resposta mesmo quando a voz falha (sem motor carregado,
sem dispositivo de audio, modelo em falta, ...).

O texto a dizer e SEMPRE o que quem chamou decidiu dizer: este modulo
nunca le transcricao nem configuracao, so recebe uma string e fala-a.

SILENCIO IMEDIATO: `calar_agora()` e o unico mecanismo de silenciamento do
jarvis, e os tres gatilhos (Ctrl+C, saida do processo e o comando "cala-te")
chamam-no. Primeiro para a sintese da frase em curso, depois a reproducao, que
so escreve no dispositivo em blocos de `BLOCO_DE_REPRODUCAO_S` e verifica o
pedido entre cada um. Com `definitivo=True` (Ctrl+C, saida) o modulo fica
calado e `falar()` passa a recusar tudo: nada novo e dito, nem o resto da
frase, nem uma despedida, nem a resposta que estivesse a chegar do Claude Code.

TESTES SILENCIOSOS POR OMISSAO: `falar()` NUNCA abre
um dispositivo de audio sem opt-in explicito. Chamado sem `ficheiro=` e sem
`com_som=True`, recusa-se a tocar e devolve um erro claro (`falou=False`,
`motivo_falha` a dizer porque) ANTES de tocar em `_construir_stream()` — nao ha
"tentar e falhar em silencio", ha recusa a montante. `ficheiro=` sozinho
escreve o WAV com `muted=True` e nunca abre dispositivo: e o caminho de todos
os testes, autotestes e arneses. `com_som=True` e a UNICA maneira de haver som,
com ou sem `ficheiro=` (com os dois, toca E grava, tal como
`scripts/gerar_wav.py --com-som`), e e o que `jarvis/app.py` liga no arranque
real (o jarvis a serio tem de falar). NAO ha variavel de ambiente para isto:
so o parametro explicito e, na CLI, a flag `--com-som`.

Uso como biblioteca:

    from jarvis.voz import falar
    resultado = falar("sao quinze e trinta", com_som=True)          # toca as colunas
    resultado = falar("sao quinze e trinta", ficheiro="audio/t-voz.wav")  # so ficheiro, nunca toca
    resultado = falar("sao quinze e trinta", ficheiro="audio/t.wav", com_som=True)  # toca E grava
    resultado = falar("sao quinze e trinta")                        # RECUSADO: sem opt-in

Uso na linha de comandos (sem --ficheiro e sem --com-som, nada toca):

    .venv\Scripts\python -m jarvis.voz "sao quinze e trinta" --com-som
    .venv\Scripts\python -m jarvis.voz "sao quinze e trinta" --ficheiro audio/t-voz.wav

Autoteste das partes que nao precisam de dispositivo de audio real (sintese
para ficheiro, confinamento do caminho, e o fallback provocado de proposito):

    .venv\Scripts\python -m jarvis.voz --autoteste

Prova de tempo do silencio, tambem sem microfone e sem tocar som: escreve as
duas linhas com timestamps e o intervalo em milissegundos num ficheiro de
evidencia em docs/forja/evidence/:

    .venv\Scripts\python -m jarvis.voz --prova-silencio

A latencia (texto -> primeiro bloco de audio) mede-se com
`scripts/medir_latencia_voz.py --verificar`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import logging
import re
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.audio_util import (  # noqa: E402
    PASTA_EVIDENCIA,
    PASTA_MODELOS_PIPER,
    caminho_evidencia_de_saida,
    caminho_para_mostrar,
    caminho_wav_de_saida,
    garantir_pasta,
    ler_wav_pcm16,
)
from jarvis.consola import forcar_consola_utf8  # noqa: E402

#: A voz escolhida para o produto. Nao mudar sem voltar a medir.
NOME_DA_VOZ = "pt_PT-tugao-medium"
MODELO_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx"
CONFIG_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx.json"

# --- Silencio imediato: matar a sintese em curso ---

#: Fasquia da D60: do pedido de paragem (Ctrl+C, saida do processo ou
#: "cala-te") ao ultimo sample de audio, no maximo isto.
LIMITE_DE_SILENCIO_MS = 500.0


#: Quem esta a falar AGORA (a `FalaResidente` da chamada a `falar()` em
#: curso) e se ja foi pedido silencio definitivo. Globais de proposito: quem
#: cala e outra thread (Ctrl+C, atexit) e nao tem o stream a mao.
_TRANCA_DA_VOZ = threading.Lock()
_stream_ativo: object | None = None
_calado_ate_novo_aviso = False


def _guardar_voz_ativa(stream: object) -> None:
    global _stream_ativo
    with _TRANCA_DA_VOZ:
        _stream_ativo = stream


def _esquecer_voz_ativa(stream: object) -> None:
    global _stream_ativo
    with _TRANCA_DA_VOZ:
        if _stream_ativo is stream:
            _stream_ativo = None
    # Fim da frase: se esta tinha sido calada, e aqui que o ruido de terceiros
    # provocado por isso ja acabou de sair (ver `_FiltroDoRuidoDeCalar`).
    _tirar_filtro_de_ruido_de_calar()


def esta_calado() -> bool:
    """True depois de um `calar_agora(definitivo=True)` (Ctrl+C, saida).

    Quem fala consulta isto A MONTANTE: depois de um Ctrl+C nada novo e
    falado, nem o resto da frase, nem uma despedida, nem a resposta que
    estivesse a chegar do Claude Code.
    """
    with _TRANCA_DA_VOZ:
        return _calado_ate_novo_aviso


def retomar_a_voz() -> None:
    """Desfaz o silencio definitivo.

    O processo do jarvis nunca chama isto (depois de um Ctrl+C nada volta a
    falar): existe para os testes e para quem use este modulo como
    biblioteca depois de ter pedido silencio.
    """
    global _calado_ate_novo_aviso
    with _TRANCA_DA_VOZ:
        _calado_ate_novo_aviso = False


@dataclass(frozen=True)
class ResultadoSilencio:
    """O que `calar_agora()` fez, e em quanto tempo (D60(4)(b))."""

    #: Relogio de parede do pedido e do fim do audio: sao estes dois que vao
    #: para o log, para o intervalo se ler em milissegundos como as outras
    #: etapas.
    instante_do_pedido: datetime.datetime
    instante_do_fim_do_audio: datetime.datetime
    #: Medido com `time.perf_counter` entre os dois instantes acima.
    intervalo_ms: float
    matou_sintese: bool
    parou_reproducao: bool
    definitivo: bool
    motivo: str = ""
    erro: str = ""

    @property
    def dentro_do_limite(self) -> bool:
        return self.intervalo_ms <= LIMITE_DE_SILENCIO_MS


#: O RealtimeTTS grita no logger raiz quando lhe matam a sintese por baixo:
#: chama "failed ... unknown error" ao que foi uma ordem cumprida. Mesma regra
#: do ruido do encerramento em jarvis/app.py: descarta-se SO o ruido
#: conhecido, SO durante a janela em que se esta a calar, e tudo o resto passa.
_RUIDO_DE_QUEM_FOI_MANDADO_CALAR = (
    "failed to synthesize sentence",
    "is the only engine available",
    "No playback thread found",
)


class _FiltroDoRuidoDeCalar(logging.Filter):
    """Descarta so as tres mensagens que o RealtimeTTS emite ao ser calado."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - API do logging
        mensagem = record.getMessage()
        return not any(parte in mensagem for parte in _RUIDO_DE_QUEM_FOI_MANDADO_CALAR)


#: O filtro so existe entre o pedido de silencio e o fim da frase que estava a
#: ser dita: o RealtimeTTS ainda grita depois de `stop()` devolver (a thread da
#: sintese e a da reproducao acordam a seguir), por isso quem o tira e o
#: `finally` de `falar()`, atraves de `_esquecer_voz_ativa`.
_TRANCA_DO_FILTRO = threading.Lock()
_filtro_de_ruido: _FiltroDoRuidoDeCalar | None = None


def _por_filtro_de_ruido_de_calar() -> None:
    """Instala o filtro no logger raiz (idempotente). Nunca levanta."""
    global _filtro_de_ruido
    with _TRANCA_DO_FILTRO:
        if _filtro_de_ruido is not None:
            return
        filtro = _FiltroDoRuidoDeCalar()
        try:
            logging.getLogger().addFilter(filtro)
        except Exception:  # noqa: BLE001 - calar nunca pode levantar
            return
        _filtro_de_ruido = filtro


def _tirar_filtro_de_ruido_de_calar() -> None:
    """Tira o filtro do logger raiz, se la estiver. Nunca levanta."""
    global _filtro_de_ruido
    with _TRANCA_DO_FILTRO:
        filtro = _filtro_de_ruido
        _filtro_de_ruido = None
    if filtro is None:
        return
    try:
        logging.getLogger().removeFilter(filtro)
    except Exception:  # noqa: BLE001 - calar nunca pode levantar
        pass


@contextlib.contextmanager
def _sem_ruido_de_quem_foi_calado():
    """O mesmo, com principio e fim no mesmo sitio (usado pelos testes)."""
    _por_filtro_de_ruido_de_calar()
    try:
        yield
    finally:
        _tirar_filtro_de_ruido_de_calar()


def _linha_do_pedido(motivo: str, definitivo: bool) -> str:
    fim = "; nada novo sera falado (D60)" if definitivo else " (D60)"
    return (
        f"SILENCIO pedido ({motivo or 'sem motivo'}): parar a sintese "
        f"e so depois parar a reproducao{fim}"
    )


def _linha_do_fim(resultado: ResultadoSilencio) -> str:
    sim_nao = {True: "sim", False: "nao"}
    linha = (
        f"SILENCIO fim do audio ({resultado.motivo or 'sem motivo'}): "
        f"{resultado.intervalo_ms:.0f} ms desde o pedido "
        f"(fasquia D60: <= {LIMITE_DE_SILENCIO_MS:.0f} ms) | sintese morta="
        f"{sim_nao[resultado.matou_sintese]} | reproducao parada="
        f"{sim_nao[resultado.parou_reproducao]}"
    )
    if resultado.erro:
        linha += f" | falhas: {resultado.erro}"
    return linha


def calar_agora(
    motivo: str = "",
    *,
    definitivo: bool = False,
    registar: Callable[[str], object] | None = None,
) -> ResultadoSilencio:
    """Cala a voz JA: mata a sintese em curso e so depois para a reproducao.

    E o UNICO mecanismo de silenciamento do jarvis: usam-no o handler
    de Ctrl+C, a saida do processo e a accao "calar" da lista branca da D4.d (e
    o equivalente ingles quando existir). A ordem — `matar_agora()` primeiro,
    `stream.stop()` depois — nao e negociavel: parar a reproducao
    antes de a sintese parar deixava a thread da sintese a produzir mais audio.

    `definitivo=True` (Ctrl+C, saida do processo) marca tambem o modulo como
    calado, e a partir dai `falar()` recusa tudo.

    `registar` e uma funcao de log (tipicamente `LogDaSessao.linha`): recebe a
    linha do pedido ANTES do trabalho e a linha do fim do audio DEPOIS, que
    sao as duas linhas com timestamps que a D60(4)(b) exige.

    Nunca levanta: qualquer falha fica em `ResultadoSilencio.erro`.
    """
    global _calado_ate_novo_aviso
    marca_inicial = time.perf_counter()
    instante_do_pedido = datetime.datetime.now()
    if registar is not None:
        registar(_linha_do_pedido(motivo, definitivo))

    with _TRANCA_DA_VOZ:
        if definitivo:
            _calado_ate_novo_aviso = True
        stream = _stream_ativo

    falhas: list[str] = []
    motor = getattr(stream, "engine", None)
    matou = False
    parou = False

    _por_filtro_de_ruido_de_calar()
    try:
        # (1) a sintese em curso: e ela que `stream.stop()` sozinho nao apanha.
        matar = getattr(motor, "matar_agora", None)
        if callable(matar):
            try:
                matou = bool(matar())
            except Exception as erro:  # noqa: BLE001 - calar nunca pode levantar
                falhas.append(f"matar_agora: {erro}")

        # (2) e so agora a reproducao ja sintetizada (corta em
        # dezenas de ms, desde que nao haja sintese viva a bloquear o join).
        parar = getattr(stream, "stop", None)
        if callable(parar):
            try:
                parar()
                parou = True
            except Exception as erro:  # noqa: BLE001 - calar nunca pode levantar
                falhas.append(f"stop: {erro}")
    finally:
        if stream is None:
            # Nao havia ninguem a falar: nao ha ruido nenhum para esperar.
            _tirar_filtro_de_ruido_de_calar()

    intervalo_ms = (time.perf_counter() - marca_inicial) * 1000.0
    resultado = ResultadoSilencio(
        instante_do_pedido=instante_do_pedido,
        instante_do_fim_do_audio=datetime.datetime.now(),
        intervalo_ms=intervalo_ms,
        matou_sintese=matou,
        parou_reproducao=parou,
        definitivo=definitivo,
        motivo=motivo,
        erro="; ".join(falhas),
    )
    if registar is not None:
        registar(_linha_do_fim(resultado))
    return resultado


# --- Motor residente: carregado uma vez por processo -----------------------
#
# A voz ja nao arranca um processo de sintese por frase (o `piper.exe` custava
# cinco segundos de arranque a cada resposta). O motor carrega uma vez, no
# primeiro uso ou em `aquecer()`, e fica em memoria; cada frase e so
# inferencia. O audio sai em blocos: o primeiro pedaco da frase comeca a tocar
# enquanto o resto ainda esta a ser sintetizado.

#: Fios do onnxruntime para a sintese. O valor por omissao (todos os nucleos)
#: e o mais lento num processador hibrido: os nucleos de eficiencia atrasam
#: cada passo da inferencia. Medido no i5-14400F (20 frases x 3, texto ->
#: primeiro bloco): 1 fio 113 ms p50, 2 fios 71 ms, 4 fios 53 ms.
FIOS_DA_SINTESE = 4

#: Voz inglesa natural (Kokoro-82M em ONNX). So e usada para a lingua "en" e
#: quando o pacote e os dois ficheiros do modelo estao presentes; ver
#: docs/MODELOS.md.
PASTA_MODELOS_KOKORO = RAIZ / "models" / "kokoro"
MODELO_KOKORO = PASTA_MODELOS_KOKORO / "kokoro-v1.0.onnx"
VOZES_KOKORO = PASTA_MODELOS_KOKORO / "voices-v1.0.bin"
VOZ_KOKORO = "af_heart"
COMANDO_KOKORO = '.venv\\Scripts\\python -m pip install "kokoro-onnx==0.6.1"'

#: Linguas que a voz sabe falar: "pt" pelo Piper pt-PT, "en" pelo Kokoro.
LINGUAS_DA_VOZ = ("pt", "en")

#: A lingua das respostas do jarvis, e portanto a da voz. As respostas de hoje
#: (horas, data, confirmacoes das accoes locais) sao escritas em portugues, por
#: isso a voz e a portuguesa. Muda-se com `definir_lingua_da_voz()` so quando o
#: texto das respostas passar a ser escrito noutra lingua: a voz segue a lingua
#: do texto, nunca o motor que por acaso esteja instalado.
_lingua_da_voz = "pt"


def _validar_lingua(lingua: str) -> str:
    if lingua not in LINGUAS_DA_VOZ:
        raise ValueError(f"lingua da voz desconhecida: {lingua!r} (conhecidas: {', '.join(LINGUAS_DA_VOZ)})")
    return lingua


def lingua_da_voz() -> str:
    """A lingua em que a voz fala as respostas deste processo."""
    return _lingua_da_voz


def definir_lingua_da_voz(lingua: str) -> None:
    """Escolhe a lingua da voz; a frase seguinte usa o motor dessa lingua."""
    global _lingua_da_voz
    _lingua_da_voz = _validar_lingua(lingua)

#: Tamanho de cada escrita no dispositivo de som. Entre blocos verifica-se o
#: pedido de silencio, por isso isto e tambem o atraso maximo da reproducao a
#: obedecer a um "cala-te".
BLOCO_DE_REPRODUCAO_S = 0.05

#: Quanto `FalaResidente.stop()` espera, no maximo, pelo fim da reproducao.
#: Fica abaixo da fasquia do silencio para `calar_agora()` nunca a passar a espera.
ESPERA_DO_STOP_S = 0.4

#: Uma primeira frase mais comprida do que isto e partida na primeira virgula
#: (depois do minimo), para o primeiro audio nao esperar pela frase inteira.
MAXIMO_DO_PRIMEIRO_PEDACO = 60
MINIMO_DO_PRIMEIRO_PEDACO = 16

_FIM_DE_FRASE = re.compile(r"(?<=[.!?;])\s+")


class MotorIndisponivel(RuntimeError):
    """O motor pedido nao existe nesta maquina (pacote ou modelo em falta)."""


def dividir_para_sintese(texto: str) -> list[str]:
    """Parte o texto em pedacos que se sintetizam e tocam um a seguir ao outro.

    Frase a frase; a primeira, se for comprida, e partida na primeira virgula
    que deixe pelo menos `MINIMO_DO_PRIMEIRO_PEDACO` caracteres de cada lado.
    Nunca muda nem perde texto: juntar os pedacos com espacos devolve o texto
    com os espacos normalizados.
    """
    pedacos = [p for p in _FIM_DE_FRASE.split(" ".join((texto or "").split())) if p]
    if pedacos and len(pedacos[0]) > MAXIMO_DO_PRIMEIRO_PEDACO:
        primeira = pedacos[0]
        corte = primeira.find(", ", MINIMO_DO_PRIMEIRO_PEDACO)
        if corte > 0 and len(primeira) - corte - 2 >= MINIMO_DO_PRIMEIRO_PEDACO:
            pedacos[0:1] = [primeira[: corte + 1], primeira[corte + 2 :]]
    return pedacos


def _opcoes_do_onnx():
    import onnxruntime

    opcoes = onnxruntime.SessionOptions()
    opcoes.intra_op_num_threads = FIOS_DA_SINTESE
    opcoes.inter_op_num_threads = 1
    return opcoes


class MotorPiperResidente:
    """Voz pt-PT (Piper `pt_PT-tugao-medium`) pela biblioteca, dentro do processo."""

    nome = "piper"
    lingua = "pt"

    def __init__(self, modelo: Path = MODELO_ONNX, config: Path = CONFIG_ONNX) -> None:
        if not Path(modelo).is_file() or not Path(config).is_file():
            raise MotorIndisponivel(
                f"voz '{NOME_DA_VOZ}' nao encontrada em {PASTA_MODELOS_PIPER} (ver docs/MODELOS.md)"
            )
        try:
            import json

            import onnxruntime
            from piper.config import PiperConfig
            from piper.voice import PiperVoice
        except ImportError as erro:
            raise MotorIndisponivel(f"biblioteca piper-tts em falta: {erro}") from erro
        sessao = onnxruntime.InferenceSession(
            str(modelo), sess_options=_opcoes_do_onnx(), providers=["CPUExecutionProvider"]
        )
        configuracao = PiperConfig.from_dict(json.loads(Path(config).read_text(encoding="utf-8")))
        self._voz = PiperVoice(config=configuracao, session=sessao)
        self.taxa = int(configuracao.sample_rate)
        self.descricao = f"Piper {NOME_DA_VOZ} (pt-PT, residente, CPU)"

    def sintetizar(self, texto: str):
        """Gera blocos PCM16 mono a `self.taxa`, pedaco a pedaco."""
        for pedaco in dividir_para_sintese(texto):
            for bloco in self._voz.synthesize(pedaco):
                yield bloco.audio_int16_bytes


class MotorKokoro:
    """Voz inglesa Kokoro-82M (kokoro-onnx), dentro do processo."""

    nome = "kokoro"
    lingua = "en"

    def __init__(self, modelo: Path = MODELO_KOKORO, vozes: Path = VOZES_KOKORO) -> None:
        try:
            import kokoro_onnx
        except ImportError as erro:
            raise MotorIndisponivel(
                f"pacote kokoro-onnx em falta; instalar com: {COMANDO_KOKORO}"
            ) from erro
        if not Path(modelo).is_file() or not Path(vozes).is_file():
            raise MotorIndisponivel(
                f"modelo Kokoro em falta em {caminho_para_mostrar(PASTA_MODELOS_KOKORO)} "
                "(ver docs/MODELOS.md)"
            )
        import onnxruntime

        sessao = onnxruntime.InferenceSession(
            str(modelo), sess_options=_opcoes_do_onnx(), providers=["CPUExecutionProvider"]
        )
        self._kokoro = kokoro_onnx.Kokoro.from_session(sessao, str(vozes))
        self.taxa = 24000
        self.descricao = f"Kokoro-82M voz {VOZ_KOKORO} (en, residente, CPU)"

    def sintetizar(self, texto: str):
        """Gera blocos PCM16 mono a `self.taxa`, pedaco a pedaco."""
        import numpy

        for pedaco in dividir_para_sintese(texto):
            amostras, taxa = self._kokoro.create(pedaco, voice=VOZ_KOKORO, speed=1.0, lang="en-us")
            self.taxa = int(taxa)
            yield (numpy.clip(amostras, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _aquecido(motor):
    """Sintetiza uma palavra que se deita fora: prova que o motor fala e deixa-o quente."""
    for _bloco in motor.sintetizar("ok."):
        pass
    return motor


def _carregar_motor(lingua: str | None = None):
    """Carrega e aquece o motor da `lingua` dada (por omissao, `lingua_da_voz()`).

    Portugues e sempre o Piper pt-PT: um texto portugues nunca chega a uma voz
    inglesa, que o leria com as regras de pronuncia do ingles. Ingles e o
    Kokoro; se o Kokoro faltar, nao carregar ou falhar a primeira sintese,
    fica o Piper, com o motivo na descricao, para o jarvis nunca perder a voz.

    Os imports das bibliotecas de sintese ficam dentro de um
    `warnings.catch_warnings()`: o que elas registam em `warnings.filters` ao
    carregar nao sobrevive fora daqui.
    """
    lingua = _validar_lingua(lingua or lingua_da_voz())
    with warnings.catch_warnings():
        if lingua == "en":
            try:
                return _aquecido(MotorKokoro())
            except Exception as sem_kokoro:  # noqa: BLE001 - um Kokoro partido nunca tira a voz
                motivo_kokoro = str(sem_kokoro) or type(sem_kokoro).__name__
        motor = _aquecido(MotorPiperResidente())
    if lingua == "en":
        motor.descricao += f" | Kokoro indisponivel: {motivo_kokoro}"
    return motor


_TRANCA_DO_MOTOR = threading.Lock()
_motores_residentes: dict[str, object] = {}


def motor_residente(lingua: str | None = None):
    """O motor da voz desta lingua neste processo, carregado na primeira chamada e so nela."""
    lingua = _validar_lingua(lingua or lingua_da_voz())
    with _TRANCA_DO_MOTOR:
        motor = _motores_residentes.get(lingua)
        if motor is None:
            motor = _carregar_motor(lingua)
            _motores_residentes[lingua] = motor
        return motor


def aquecer(lingua: str | None = None) -> str:
    """Carrega e aquece o motor da lingua da voz, para a primeira resposta nao esperar.

    Nao toca som nem escreve ficheiro. Devolve a descricao do motor, para o
    log do arranque. Levanta se nao houver motor nenhum.
    """
    return motor_residente(lingua).descricao


def _esquecer_motor_residente() -> None:
    """So para os testes: o proximo uso volta a carregar o motor."""
    with _TRANCA_DO_MOTOR:
        _motores_residentes.clear()


class _SaidaDeSom:
    """O dispositivo de som do processo: aberto na primeira fala com som e mantido."""

    def __init__(self) -> None:
        self._tranca = threading.Lock()
        self._pyaudio = None
        self._stream = None
        self._taxa = 0

    def escrever(self, taxa: int, dados: bytes) -> None:
        with self._tranca:
            if self._stream is None or self._taxa != taxa:
                self._fechar_stream()
                import pyaudio

                if self._pyaudio is None:
                    self._pyaudio = pyaudio.PyAudio()
                self._stream = self._pyaudio.open(
                    format=pyaudio.paInt16, channels=1, rate=taxa, output=True
                )
                self._taxa = taxa
            stream = self._stream
        stream.write(dados)

    def _fechar_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:  # noqa: BLE001 - fechar nunca pode levantar
                pass
            self._stream = None


_SAIDA_DE_SOM = _SaidaDeSom()

_FIM_DOS_BLOCOS = object()


class FalaResidente:
    """Uma frase dita pelo motor residente.

    Tem a forma que `falar()` e `calar_agora()` ja conheciam: `feed()`,
    `play()`, `stop()` e um `engine` com `matar_agora()`. A sintese corre numa
    thread que enche uma fila; `play()` tira blocos da fila e toca-os ou
    escreve-os. Um pedido de silencio para os dois lados: a sintese deixa de
    produzir e a reproducao nao escreve mais nenhum bloco.
    """

    def __init__(self, motor, saida_de_som: _SaidaDeSom | None = None) -> None:
        self.engine = self
        self._motor = motor
        self._saida = saida_de_som or _SAIDA_DE_SOM
        self._textos: list[str] = []
        self._parar = threading.Event()
        self._a_falar = threading.Event()
        self._calado = False
        self._fio_que_toca: threading.Thread | None = None
        #: `perf_counter()` do instante em que o primeiro bloco de audio saiu
        #: (entregue ao dispositivo ou escrito no WAV). None se nada saiu.
        self.instante_do_primeiro_audio: float | None = None

    def feed(self, texto: str) -> None:
        self._textos.append(texto)

    def _marcar_primeiro_audio(self) -> None:
        # Marcado DEPOIS de o bloco sair (escrito no WAV ou entregue ao
        # dispositivo, o que na primeira fala inclui abrir o dispositivo), e
        # nao quando fica pronto na fila: o log nunca promete mais cedo do que
        # o que sai de facto.
        if self.instante_do_primeiro_audio is None:
            self.instante_do_primeiro_audio = time.perf_counter()

    def matar_agora(self) -> bool:
        """Para a sintese; devolve True se havia uma frase a meio."""
        estava = self._a_falar.is_set()
        self._calado = True
        self._parar.set()
        return estava

    def stop(self) -> None:
        """Para a reproducao e espera (no maximo `ESPERA_DO_STOP_S`) que pare mesmo.

        Assim a linha "fim do audio" de `calar_agora()` so e escrita depois de
        o ultimo bloco ter saido. Chamado pela propria thread que toca (um
        Ctrl+C tratado a meio de `play()`), nao espera: seria esperar por si
        mesma.
        """
        self._parar.set()
        if self._fio_que_toca is not threading.current_thread():
            fim = time.perf_counter() + ESPERA_DO_STOP_S
            while self._a_falar.is_set() and time.perf_counter() < fim:
                time.sleep(0.002)

    def _produzir(self, fila, texto: str) -> None:
        try:
            for bloco in self._motor.sintetizar(texto):
                if self._parar.is_set():
                    break
                fila.put(bloco)
        except BaseException as erro:  # noqa: BLE001 - passa a falha para quem toca
            fila.put(erro)
        finally:
            fila.put(_FIM_DOS_BLOCOS)

    def play(self, muted: bool = False, output_wavfile: str | None = None) -> None:
        import queue
        import wave

        texto = " ".join(self._textos)
        fila: queue.Queue = queue.Queue()
        self._fio_que_toca = threading.current_thread()
        self._a_falar.set()
        produtor = threading.Thread(
            target=self._produzir, args=(fila, texto), name="voz-sintese", daemon=True
        )
        produtor.start()
        wav = None
        try:
            while not self._parar.is_set():
                try:
                    bloco = fila.get(timeout=BLOCO_DE_REPRODUCAO_S)
                except queue.Empty:
                    continue
                if bloco is _FIM_DOS_BLOCOS:
                    break
                if isinstance(bloco, BaseException):
                    raise bloco
                taxa = int(self._motor.taxa)
                if output_wavfile is not None:
                    if wav is None:
                        wav = wave.open(str(output_wavfile), "wb")
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(taxa)
                    wav.writeframes(bloco)
                if not muted:
                    passo = max(2, int(taxa * BLOCO_DE_REPRODUCAO_S) * 2)
                    for inicio in range(0, len(bloco), passo):
                        if self._parar.is_set():
                            break
                        self._saida.escrever(taxa, bloco[inicio : inicio + passo])
                        self._marcar_primeiro_audio()
                else:
                    self._marcar_primeiro_audio()
        finally:
            self._a_falar.clear()
            if wav is not None:
                wav.close()


def _construir_stream():
    """A frase seguinte, dita pelo motor residente (carregado so da primeira vez)."""
    return FalaResidente(motor_residente())


def _duracao_do_wav(caminho: Path) -> float:
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if taxa <= 0 or canais <= 0:
        return 0.0
    return (len(dados) / 2 / canais) / taxa


#: `motivo_falha` de quem nao falou porque lhe mandaram calar, para
#: quem chama distinguir "a voz falhou" de "a voz obedeceu".
MOTIVO_SILENCIADO = "silenciado a pedido (D60): nada novo e falado"


@dataclass(frozen=True)
class ResultadoFala:
    """O que `falar()` conseguiu fazer. Nunca e a excecao — e sempre o resultado."""

    #: True so quando a voz tocou ou gravou com sucesso.
    falou: bool
    #: Preenchido so no modo --ficheiro, com sucesso.
    caminho: Path | None = None
    duracao_s: float | None = None
    #: Nao vazio quando falou=False: a razao do fallback para texto.
    motivo_falha: str = ""
    #: `time.perf_counter()` do primeiro bloco de audio entregue ao dispositivo
    #: ou escrito no WAV: o inicio da resposta falada. None quando nada saiu.
    primeiro_audio: float | None = None


#: D61: `falar()` sem `ficheiro=` e sem `com_som=True` e um erro de teste, nao
#: um "tentar e falhar em silencio" — recusa-se ANTES de tocar em
#: `_construir_stream()`, por isso nenhum dispositivo de audio chega a ser
#: considerado. Nao ha variavel de ambiente para isto.
MOTIVO_SEM_OPT_IN = (
    "falar() recusado (D61): sem ficheiro= e sem com_som=True nao se abre nenhum "
    "dispositivo de audio; ouvir e sempre opt-in explicito (flag --com-som na CLI, "
    "nunca uma variavel de ambiente)"
)


def falar(
    texto: str, *, ficheiro: str | Path | None = None, com_som: bool = False
) -> ResultadoFala:
    """Fala `texto` em voz alta (motor residente), ou grava-o num WAV se `ficheiro` for dado.

    Sem `ficheiro=` E sem `com_som=True`, esta funcao RECUSA-SE a
    tocar — devolve `falou=False` com `motivo_falha=MOTIVO_SEM_OPT_IN` antes de
    tentar construir seja o que for, e portanto antes de qualquer hipotese de
    abrir um dispositivo de audio. So com `ficheiro=`, sintetiza para WAV com
    `muted=True` e nunca abre dispositivo nenhum. Com `com_som=True` sai som,
    e e a unica coisa que o faz: sozinho toca a serio nas colunas (e o que
    `jarvis/app.py` liga no arranque real), com `ficheiro=` toca E grava, tal
    como `scripts/gerar_wav.py --com-som`.

    NUNCA levanta: qualquer falha na sintese, na reproducao ou na
    escrita do ficheiro e apanhada aqui dentro, o texto sai impresso na
    consola como resposta de recurso, e o resultado devolvido diz porque.
    """
    texto_limpo = (texto or "").strip()
    if not texto_limpo:
        motivo = "texto vazio: nada a dizer"
        print(f"jarvis (voz recusada: {motivo})")
        return ResultadoFala(falou=False, motivo_falha=motivo)

    saida: Path | None = None
    if ficheiro is not None:
        try:
            saida = caminho_wav_de_saida(ficheiro)
        except ValueError as erro:
            print(f"jarvis (texto, voz recusada): {texto_limpo}")
            return ResultadoFala(falou=False, motivo_falha=str(erro))

    if esta_calado():
        # Ja houve Ctrl+C ou saida do processo: nada NOVO e falado, e nem se
        # imprime o recurso da D35.4 — quem pediu silencio pediu silencio.
        # Este e o guarda a montante de TUDO o que se segue: corre
        # antes do guarda da D61 de proposito, senao um pedido sem opt-in
        # depois do Ctrl+C ainda escrevia a frase na consola que a D60(1)
        # manda calar.
        return ResultadoFala(falou=False, motivo_falha=MOTIVO_SILENCIADO)

    if saida is None and not com_som:
        # A metade "nunca toca sem pedir" da D61: recusa a montante, nunca uma
        # tentativa que falha calada. Nenhum dispositivo de audio chega a ser
        # considerado; a resposta sai em texto como na D35.4.
        print(f"jarvis (texto, voz recusada): {texto_limpo}")
        return ResultadoFala(falou=False, motivo_falha=MOTIVO_SEM_OPT_IN)

    try:
        stream = _construir_stream()
        # Registado ANTES de falar: e por aqui que `calar_agora()`, noutra
        # thread, chega ao motor e ao stream desta frase.
        _guardar_voz_ativa(stream)
        try:
            stream.feed(texto_limpo)
            if saida is not None:
                garantir_pasta(saida.parent)
                # muted=not com_som: sem opt-in (o caso de todos os testes,
                # autotestes e arneses) escreve o WAV sem abrir dispositivo
                # nenhum; com --com-som toca E grava, exatamente como
                # scripts/gerar_wav.py --com-som (`--com-som` quer
                # dizer a mesma coisa em todo o repositorio).
                stream.play(muted=not com_som, output_wavfile=str(saida))
            else:
                stream.play(muted=False)
        except BaseException as interrupcao:
            if isinstance(interrupcao, Exception):
                raise  # falha normal: vai para o recuo da D35.4 la em baixo
            # Ctrl+C (ou SystemExit) a subir por aqui: calar AGORA, antes de
            # o `finally` largar o registo da voz ativa — senao ficava um
            # motor a acabar a frase sozinho.
            calar_agora("Ctrl+C a meio da frase (D30/D60)", definitivo=True)
            raise
        finally:
            _esquecer_voz_ativa(stream)
    except Exception as erro:  # noqa: BLE001 - D35.4: qualquer falha cai para texto
        if esta_calado():  # calado a meio: a falha e consequencia do silencio
            return ResultadoFala(falou=False, motivo_falha=MOTIVO_SILENCIADO)
        print(f"jarvis (texto, voz falhou: {erro}): {texto_limpo}")
        return ResultadoFala(falou=False, motivo_falha=str(erro))

    if esta_calado() or getattr(getattr(stream, "engine", None), "_calado", False):
        # Silenciado a meio da frase (pelo Ctrl+C, que cala o modulo, ou por um
        # "cala-te", que so mata o motor desta frase): o que se ouviu foi
        # cortado e nada mais se diz. Nao e uma falha da voz, e uma ordem
        # cumprida — por isso `falou=False` com este motivo e nao um erro.
        return ResultadoFala(falou=False, motivo_falha=MOTIVO_SILENCIADO)

    primeiro_audio = getattr(stream, "instante_do_primeiro_audio", None)
    if saida is not None:
        try:
            duracao_s = _duracao_do_wav(saida)
        except Exception as erro:  # noqa: BLE001 - D35.4: `falar()` nunca levanta
            # Acontece com um WAV cortado a meio por um "cala-te": o ficheiro
            # existe mas pode nao fechar como WAV valido. Nao e uma falha da
            # voz a dizer ao utilizador, e o resultado de ele ter mandado calar.
            return ResultadoFala(falou=False, caminho=saida, motivo_falha=f"WAV por ler: {erro}")
        return ResultadoFala(
            falou=True, caminho=saida, duracao_s=duracao_s, primeiro_audio=primeiro_audio
        )
    return ResultadoFala(falou=True, primeiro_audio=primeiro_audio)


# --- Autoteste (sem depender de um dispositivo de audio real) --------------


def _autoteste() -> int:
    """Prova a sintese para ficheiro, o confinamento do caminho e o fallback.

    Nao toca em `audio/` (usa uma pasta temporaria) e nao exige um
    dispositivo de saida de som: so o ramo --ficheiro e testado a falar a
    serio; o fallback e provocado com um motor falso.
    """
    import tempfile

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    # 1. texto vazio nunca chega a sintetizar, e falha de forma explicita.
    resultado_vazio = falar("   ")
    verificar("texto vazio: falou=False", resultado_vazio.falou, False)
    verificar("texto vazio: motivo preenchido", bool(resultado_vazio.motivo_falha), True)

    # 2. sintese real para ficheiro, dentro de audio/ (unico sitio permitido
    # por caminho_wav_de_saida) mas com um nome que se apaga a seguir.
    caminho_teste = Path("audio") / "_autoteste_voz_t5.wav"
    try:
        resultado = falar("ola, isto e um autoteste da voz.", ficheiro=str(caminho_teste))
        verificar("ficheiro: falou=True", resultado.falou, True)
        verificar("ficheiro: caminho existe", resultado.caminho is not None and resultado.caminho.is_file(), True)
        verificar("ficheiro: duracao audivel (> 0.3 s)", (resultado.duracao_s or 0) > 0.3, True)
    finally:
        (RAIZ / caminho_teste).unlink(missing_ok=True)

    # 3. caminho fora do repositorio e recusado ANTES de tocar no motor
    # (mesma regra de audio_util.caminho_wav_de_saida).
    with tempfile.TemporaryDirectory() as pasta:
        fora_da_raiz = str(Path(pasta) / "fora.wav")
        resultado_fora = falar("nunca deveria escrever aqui", ficheiro=fora_da_raiz)
        verificar("caminho fora do repo: falou=False", resultado_fora.falou, False)
        verificar("caminho fora do repo: nada escrito", Path(fora_da_raiz).exists(), False)

    # 4. fallback explicito: um motor que rebenta nunca propaga a
    # excecao, e o texto sai impresso na consola em vez da voz.
    # `sys.modules[__name__]` e nao `import jarvis.voz`, de proposito: corrido
    # como `python -m jarvis.voz` este ficheiro executa como `__main__`, e um
    # `import jarvis.voz` fresco criava um SEGUNDO objeto de modulo (mundo a
    # parte do que `falar()` ve), e o monkeypatch nao apanhava nada.
    modulo_atual = sys.modules[__name__]

    original = modulo_atual._construir_stream

    def _motor_quebrado():
        raise RuntimeError("motor de voz de mentira, so para o autoteste")

    modulo_atual._construir_stream = _motor_quebrado
    saida_capturada = io.StringIO()
    try:
        with contextlib.redirect_stdout(saida_capturada):
            # com_som=True: isto testa o fallback D35.4 (o motor rebenta DEPOIS
            # de ser construido), nao o guarda da D61 (opt-in) — esse e o caso 5.
            resultado_falha = falar("frase qualquer que devia sair em texto", com_som=True)
    finally:
        modulo_atual._construir_stream = original
    verificar("fallback: falou=False quando o motor rebenta", resultado_falha.falou, False)
    verificar(
        "fallback: motivo_falha tem a excecao original",
        "motor de voz de mentira" in resultado_falha.motivo_falha,
        True,
    )
    verificar(
        "fallback: o texto sai impresso na consola (D35.4)",
        "frase qualquer que devia sair em texto" in saida_capturada.getvalue(),
        True,
    )

    # 5. D61: sem ficheiro= e sem com_som=True, falar() recusa-se ANTES de
    # construir seja o que for — nunca chega a considerar abrir um
    # dispositivo de audio. Mesmo truque do `sys.modules[__name__]` do caso 4.
    chamou_construir: list[int] = []

    def _construir_nunca_deveria_ser_chamado():
        chamou_construir.append(1)
        raise AssertionError("falar() construiu o motor sem ficheiro= nem com_som=True")

    modulo_atual._construir_stream = _construir_nunca_deveria_ser_chamado
    try:
        resultado_sem_opt_in = falar("frase qualquer sem opt-in nenhum")
    finally:
        modulo_atual._construir_stream = original
    verificar(
        "D61: sem ficheiro/com_som falar() nao constroi motor nenhum",
        chamou_construir,
        [],
    )
    verificar("D61: sem ficheiro/com_som falou=False", resultado_sem_opt_in.falou, False)
    verificar(
        "D61: motivo_falha explica a recusa",
        resultado_sem_opt_in.motivo_falha,
        MOTIVO_SEM_OPT_IN,
    )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste da voz completo (sintese para ficheiro, confinamento, fallback D35.4).")
    return 0


# --- Prova de tempo do silencio, sem microfone e sem tocar som (D60(4)(b)) --

#: Frase comprida (dentro do limite do contrato da resposta falada) so para a
#: sintese demorar o suficiente a ser apanhada a MEIO, que e o pior caso.
FRASE_DA_PROVA = (
    "Esta frase existe so para a prova do silencio imediato: e comprida de "
    "proposito para a sintese ainda estar a meio quando o pedido de paragem "
    "chegar. Tem uma segunda frase, e depois uma terceira, para haver sempre "
    "mais audio por sintetizar."
)


def _agora_com_ms(quando: datetime.datetime | None = None) -> str:
    """Timestamp local com milissegundos, o MESMO formato do log do jarvis.

    Copiado de `jarvis/app.py::agora_iso` em vez de importado: o `app` importa
    este modulo, e um import ao contrario fechava o circulo.
    """
    quando = quando or datetime.datetime.now()
    return quando.strftime("%Y-%m-%d %H:%M:%S.") + f"{quando.microsecond // 1000:03d}"


def _esperar_pela_sintese(limite_s: float = 30.0) -> bool:
    """Espera ate a frase em curso ter o primeiro bloco de audio ja fora."""
    fim = time.perf_counter() + limite_s
    while time.perf_counter() < fim:
        if getattr(_stream_ativo, "instante_do_primeiro_audio", None) is not None:
            return True
        time.sleep(0.005)
    return False


def _intervalo_entre_linhas_ms(pedido: str, fim: str) -> float:
    """Os milissegundos entre as DUAS linhas de log, lidos dos timestamps."""
    formato = "%Y-%m-%d %H:%M:%S.%f"
    t0 = datetime.datetime.strptime(pedido.split(" SILENCIO")[0], formato)
    t1 = datetime.datetime.strptime(fim.split(" SILENCIO")[0], formato)
    return (t1 - t0).total_seconds() * 1000.0


def _um_caso_da_prova(motivo: str, *, definitivo: bool, nome_do_wav: str) -> dict:
    """Sintetiza para ficheiro e cala a meio; devolve as linhas e os numeros."""
    linhas: list[str] = []
    caminho = Path("audio") / nome_do_wav
    resultado: dict[str, ResultadoFala] = {}

    def registar(texto: str) -> None:
        linha = f"{_agora_com_ms()} {texto}"
        linhas.append(linha)
        print(linha)

    def falar_em_ficheiro() -> None:
        # ficheiro= sem com_som: muted=True, NUNCA abre dispositivo de saida
        # de audio. Esta prova nao faz barulho.
        resultado["fala"] = falar(FRASE_DA_PROVA, ficheiro=str(caminho))

    thread = threading.Thread(target=falar_em_ficheiro, name="prova-silencio", daemon=True)
    thread.start()
    apanhou_a_sintese = _esperar_pela_sintese()
    marca_do_pedido = time.perf_counter()
    silencio = calar_agora(motivo, definitivo=definitivo, registar=registar)
    # Conta-se tambem ate `falar()` devolver: e o pior numero, porque inclui a
    # frase a desenrolar-se depois de o motor ja ter deixado de produzir.
    thread.join(timeout=30)
    ate_a_thread_acabar_ms = (time.perf_counter() - marca_do_pedido) * 1000.0

    duracao_do_wav = None
    absoluto = RAIZ / caminho
    if absoluto.is_file():
        try:
            duracao_do_wav = _duracao_do_wav(absoluto)
        except Exception:  # noqa: BLE001 - um WAV cortado a meio pode nao abrir
            duracao_do_wav = None
        absoluto.unlink(missing_ok=True)

    return {
        "motivo": motivo,
        "definitivo": definitivo,
        "apanhou_a_sintese": apanhou_a_sintese,
        "linhas": linhas,
        "intervalo_lido_ms": _intervalo_entre_linhas_ms(linhas[0], linhas[1]),
        "intervalo_medido_ms": silencio.intervalo_ms,
        "ate_a_thread_acabar_ms": ate_a_thread_acabar_ms,
        "matou_sintese": silencio.matou_sintese,
        "parou_reproducao": silencio.parou_reproducao,
        "thread_viva": thread.is_alive(),
        "falou": resultado.get("fala").falou if "fala" in resultado else None,
        "motivo_falha": resultado.get("fala").motivo_falha if "fala" in resultado else "",
        "duracao_do_wav_s": duracao_do_wav,
    }


def _prova_de_silencio(caminho_pedido: str | None = None) -> int:
    """Evidencia de tempo do silencio: os dois timestamps e o intervalo.

    Corre a voz REAL (o motor residente) pelo caminho de FICHEIRO, sem abrir
    nenhum dispositivo de saida de audio e sem microfone. Faz os dois casos:
    o "cala-te" (nao definitivo) e o Ctrl+C / saida do processo (definitivo,
    depois do qual nada novo e falado).
    """
    # Confinamento do --evidencia a PASTA_EVIDENCIA: validado JA, antes de
    # gastar tempo a sintetizar seja o que for, para um caminho invalido nunca
    # chegar a `garantir_pasta`/`write_text` (que criavam pastas e escreviam
    # fora do repo com os privilegios do utilizador).
    try:
        destino = (
            caminho_evidencia_de_saida(caminho_pedido)
            if caminho_pedido
            else PASTA_EVIDENCIA / f"silencio-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
        )
    except ValueError as erro:
        print(f"FALHOU (confinamento do --evidencia): {erro}", file=sys.stderr)
        return 1
    garantir_pasta(destino.parent)

    print("=== jarvis - prova do silencio imediato - sem som, sem microfone ===")
    try:
        descricao = aquecer()
    except Exception as erro:  # noqa: BLE001 - sem motor nao ha nada para calar
        print(f"FALHOU: motor de voz por carregar: {erro}")
        return 1
    print(f"motor: {descricao}")
    casos = [
        _um_caso_da_prova("cala-te", definitivo=False, nome_do_wav="_prova_silencio_calar.wav"),
    ]
    retomar_a_voz()  # o caso anterior nao e definitivo; o proximo comeca limpo
    casos.append(_um_caso_da_prova("Ctrl+C", definitivo=True, nome_do_wav="_prova_silencio_ctrlc.wav"))

    # Depois do Ctrl+C nada NOVO e falado: nem uma despedida.
    depois = falar("Adeus, ate a proxima.", ficheiro="audio/_prova_silencio_depois.wav")
    escreveu_alguma_coisa = (RAIZ / "audio" / "_prova_silencio_depois.wav").is_file()
    (RAIZ / "audio" / "_prova_silencio_depois.wav").unlink(missing_ok=True)

    agora = datetime.datetime.now()
    partes: list[str] = []
    partes.append("# Evidencia de tempo do silencio imediato\n")
    partes.append(
        f"Gerado por `.venv\\Scripts\\python -m jarvis.voz --prova-silencio` em "
        f"{_agora_com_ms(agora)}.\n"
    )
    partes.append(
        f"Voz REAL ({descricao}) pelo caminho de FICHEIRO: nenhum dispositivo de\n"
        "saida de audio foi aberto e nenhum microfone foi usado (`falar(..., ficheiro=)`\n"
        "sem `com_som` toca com `muted=True`). Os WAV de prova sao apagados no fim; a pasta `audio/` ja e\n"
        "ignorada pelo Git.\n"
    )
    partes.append(
        f"Fasquia: <= {LIMITE_DE_SILENCIO_MS:.0f} ms entre o pedido de paragem e o fim do audio.\n"
    )
    for caso in casos:
        duracao = caso["duracao_do_wav_s"]
        duracao_escrita = "sem WAV" if duracao is None else f"{duracao:.2f} s"
        pior_ms = max(caso["intervalo_lido_ms"], caso["ate_a_thread_acabar_ms"])
        partes.append(f"\n## Gatilho: {caso['motivo']}\n")
        partes.append(
            f"- frase apanhada a meio, com o primeiro audio ja fora (pior caso): "
            f"{'sim' if caso['apanhou_a_sintese'] else 'nao'}\n"
            f"- sintese parada por `matar_agora()`: {'sim' if caso['matou_sintese'] else 'nao'}\n"
            f"- reproducao parada por `stop()`: {'sim' if caso['parou_reproducao'] else 'nao'}\n"
            f"- a thread que falava terminou: {'sim' if not caso['thread_viva'] else 'NAO'}\n"
            f"- `falar()` devolveu falou={caso['falou']} ({caso['motivo_falha'] or 'sem motivo'})\n"
            f"- duracao do WAV cortado: {duracao_escrita}\n"
        )
        partes.append("\nAs duas linhas de log com timestamps:\n\n```\n")
        partes.append(caso["linhas"][0] + "\n")
        partes.append(caso["linhas"][1] + "\n")
        partes.append("```\n")
        partes.append(
            f"\n**Intervalo lido dos dois timestamps: {caso['intervalo_lido_ms']:.0f} ms** "
            f"(medido por dentro com `perf_counter`: {caso['intervalo_medido_ms']:.0f} ms); "
            f"do pedido ate `falar()` devolver: **{caso['ate_a_thread_acabar_ms']:.0f} ms** — "
            f"fasquia {LIMITE_DE_SILENCIO_MS:.0f} ms: "
            f"{'CUMPRE' if pior_ms <= LIMITE_DE_SILENCIO_MS else 'FALHA'}\n"
        )
    partes.append("\n## Depois do Ctrl+C nada novo e falado\n")
    partes.append(
        f"- `falar('Adeus, ate a proxima.')` devolveu falou={depois.falou} "
        f"({depois.motivo_falha})\n"
        f"- WAV escrito por essa tentativa: {'SIM (defeito)' if escreveu_alguma_coisa else 'nenhum'}\n"
    )

    destino.write_text("".join(partes), encoding="utf-8")
    print()
    print(f"evidencia escrita em {caminho_para_mostrar(destino)}")

    falhou = any(caso["intervalo_lido_ms"] > LIMITE_DE_SILENCIO_MS for caso in casos)
    # A fasquia vale tambem contra o pior numero (ate `falar()` devolver),
    # senao a prova estaria a escolher o numero que lhe da jeito.
    falhou = falhou or any(caso["ate_a_thread_acabar_ms"] > LIMITE_DE_SILENCIO_MS for caso in casos)
    falhou = falhou or any(caso["thread_viva"] for caso in casos)
    falhou = falhou or depois.falou or escreveu_alguma_coisa
    falhou = falhou or any(not caso["matou_sintese"] for caso in casos)
    if falhou:
        print("FALHOU: ver o ficheiro de evidencia.")
        return 1
    print("OK: os dois gatilhos calaram dentro da fasquia e nada novo foi falado.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Forca a consola para UTF-8, como os outros pontos de entrada.
    # Sem isto, `python -m jarvis.voz --help` rebenta com UnicodeEncodeError
    # numa consola em codepage 850.
    forcar_consola_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("texto", nargs="?", help="frase a dizer em voz alta, na lingua da voz")
    parser.add_argument(
        "--lingua",
        choices=LINGUAS_DA_VOZ,
        default=None,
        help="lingua do texto, que escolhe a voz (por omissao: pt, a lingua das respostas do jarvis)",
    )
    parser.add_argument(
        "--ficheiro",
        default=None,
        help=(
            "grava o audio neste WAV em vez de o tocar (dentro do repo, sufixo .wav); "
            "juntar --com-som grava E toca"
        ),
    )
    parser.add_argument(
        "--com-som",
        action="store_true",
        help=(
            "opt-in explicito para tocar nas colunas (D61): sem esta flag nada toca, "
            "falar() recusa-se em vez de abrir um dispositivo de audio (com --ficheiro "
            "toca E grava)"
        ),
    )
    parser.add_argument(
        "--autoteste",
        action="store_true",
        help="corre o autoteste (sintese para ficheiro, confinamento, fallback D35.4)",
    )
    parser.add_argument(
        "--prova-silencio",
        action="store_true",
        help=(
            "mede o silencio imediato (D60) com a voz real pelo caminho de ficheiro: "
            "nao toca som nenhum e nao usa o microfone"
        ),
    )
    parser.add_argument(
        "--evidencia",
        default=None,
        metavar="FICHEIRO",
        help=(
            "ficheiro .md a escrever DENTRO de docs/forja/evidence/ (por omissao: "
            "docs/forja/evidence/silencio-<timestamp>.md). Um caminho relativo conta a partir "
            "da raiz do repositorio; qualquer caminho fora dessa pasta, ou sem sufixo .md, e "
            "recusado com codigo 1 e sem escrever nada (D1/D10)"
        ),
    )
    args = parser.parse_args(argv)
    if args.lingua:
        definir_lingua_da_voz(args.lingua)

    if args.autoteste:
        return _autoteste()

    if args.prova_silencio:
        return _prova_de_silencio(args.evidencia)

    if not args.texto or not args.texto.strip():
        parser.error("e preciso o texto a dizer (ou --autoteste)")

    if args.ficheiro is None and not args.com_som:
        # D61: sem --ficheiro e sem --com-som, o comando recusa-se em vez de
        # tocar nas colunas por omissao. Nao chega a chamar falar().
        print("=== jarvis - voz residente ===")
        print(f"texto    = {args.texto!r}")
        print(f"RECUSADO (D61): {MOTIVO_SEM_OPT_IN}")
        print("Usa --ficheiro <caminho.wav> para gravar, ou --com-som para ouvir a serio.")
        return 1

    print("=== jarvis - voz residente ===")
    print(f"texto    = {args.texto!r}")
    t0 = time.perf_counter()
    resultado = falar(args.texto, ficheiro=args.ficheiro, com_som=args.com_som)
    decorrido = time.perf_counter() - t0

    if resultado.falou and resultado.caminho is not None:
        print(f"ficheiro = {caminho_para_mostrar(resultado.caminho)}")
        print(f"duracao  = {resultado.duracao_s:.2f} s")
        print(f"tempo    = {decorrido:.2f} s")
        print("OK: WAV escrito.")
        return 0
    if resultado.falou:
        print(f"tempo    = {decorrido:.2f} s")
        print("OK: frase dita em voz alta.")
        return 0

    print(f"AVISO: voz falhou ({resultado.motivo_falha}); resposta caiu para texto (D35.4).")
    # --ficheiro pede um WAV real: se ele nao existe, isto e uma falha do
    # comando, nao um degrau normal de recurso.
    return 1 if args.ficheiro is not None else 0


if __name__ == "__main__":
    sys.exit(main())
