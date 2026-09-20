r"""Voz de resposta do jarvis: falar(texto) via RealtimeTTS + PiperEngine.

Motor e voz decididos pelo Technology Scout (D47, TECHNOLOGY.md S4, corrige a
D37): RealtimeTTS 0.8.5 com o PiperEngine real a falar com o `piper.exe` do
proprio venv, voz `pt_PT-tugao-medium`. API exata, verbatim do TECHNOLOGY.md:

    from RealtimeTTS import TextToAudioStream, PiperEngine, PiperVoice
    voice = PiperVoice(model_file=..., config_file=...)
    engine = PiperEngine(voice=voice, piper_path=<caminho absoluto do venv>)
    stream = TextToAudioStream(engine)

`piper_path` e sempre o caminho ABSOLUTO do `piper.exe` deste venv (nunca o
PATH nem `PIPER_PATH`), pela mesma razao que scripts/gerar_wav.py (T3): o
jarvis pode correr sem o venv ativado no PATH do processo.

CODIFICACAO E SEGREDOS (repetidos aqui de scripts/gerar_wav.py, T3 — nao
importados de la porque scripts/ nao e um pacote importavel, ver o aviso no
docstring de jarvis/audio_util.py sobre a mesma fronteira):
  - PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 no ambiente ANTES de sintetizar: o
    `piper.exe` le o stdin com a ANSI do processo se isto nao estiver ligado,
    e cada acento chegava partido (bug corrigido na T3 depois de medido).
  - o `piper.exe` corre SEM o ambiente deste processo enquanto sintetiza
    (`ambiente_sem_segredos`), para o token de mensagens da sessao-mae do
    Claude Code e as chaves de terceiros do Sponsor nunca chegarem a um
    processo GPL de terceiros que le um modelo vindo da rede (D50.9).

FALLBACK EXPLICITO (D35.4): `falar()` nunca levanta. Qualquer falha na
sintese, na reproducao ou na escrita do ficheiro e apanhada, o texto sai
impresso na consola como resposta de recurso, e o `ResultadoFala` devolvido
tem `falou=False` com `motivo_falha` preenchido — e assim que o Sponsor
continua a "ouvir" a resposta mesmo quando a voz falha (sem voz PT local que
sirva, sem dispositivo de audio, piper.exe em falta, modelo em falta, ...).

O texto a dizer e SEMPRE o que quem chamou decidiu dizer (D48.2): este modulo
nunca le transcricao nem configuracao, so recebe uma string e fala-a.

SILENCIO IMEDIATO (D60, TECHNOLOGY.md S12): `calar_agora()` e o unico
mecanismo de silenciamento do jarvis, e os tres gatilhos (Ctrl+C, saida do
processo e o comando "cala-te" da lista branca da D4.d) chamam-no. Ele mata o
`piper.exe` da sintese em curso — coisa que o `stream.stop()` do RealtimeTTS
NAO consegue fazer sozinho, porque o `PiperEngine` original sintetiza com um
`subprocess.run()` bloqueante sem guardar o processo — e so depois para a
reproducao. Com `definitivo=True` (Ctrl+C, saida) o modulo fica calado e
`falar()` passa a recusar tudo: nada novo e dito, nem o resto da frase, nem
uma despedida, nem a resposta que estivesse a chegar do Claude Code.

TESTES SILENCIOSOS POR OMISSAO (D61, TECHNOLOGY.md S13): `falar()` NUNCA abre
um dispositivo de audio sem opt-in explicito. Chamado sem `ficheiro=` e sem
`com_som=True`, recusa-se a tocar e devolve um erro claro (`falou=False`,
`motivo_falha` a dizer porque) ANTES de tocar em `_construir_stream()` — nao ha
"tentar e falhar em silencio", ha recusa a montante. `ficheiro=` sozinho
escreve o WAV com `muted=True` e nunca abre dispositivo: e o caminho de todos
os testes, autotestes e arneses. `com_som=True` e a UNICA maneira de haver som,
com ou sem `ficheiro=` (com os dois, toca E grava, tal como
`scripts/gerar_wav.py --com-som`), e e o que `jarvis/app.py` liga no arranque
real (o jarvis a serio tem de falar). NAO ha variavel de ambiente para isto
(D61.2): so o parametro explicito e, na CLI, a flag `--com-som`.

Uso como biblioteca:

    from jarvis.voz import falar
    resultado = falar("sao quinze e trinta", com_som=True)          # toca as colunas
    resultado = falar("sao quinze e trinta", ficheiro="audio/t-voz.wav")  # so ficheiro, nunca toca
    resultado = falar("sao quinze e trinta", ficheiro="audio/t.wav", com_som=True)  # toca E grava
    resultado = falar("sao quinze e trinta")                        # RECUSADO (D61): sem opt-in

Uso na linha de comandos (sem --ficheiro e sem --com-som, nada toca):

    .venv\Scripts\python -m jarvis.voz "sao quinze e trinta" --com-som
    .venv\Scripts\python -m jarvis.voz "sao quinze e trinta" --ficheiro audio/t-voz.wav

Autoteste das partes que nao precisam de dispositivo de audio real (sintese
para ficheiro, confinamento do caminho, e o fallback provocado de proposito):

    .venv\Scripts\python -m jarvis.voz --autoteste

Prova de tempo do silencio (D60(4)(b)), tambem sem microfone e sem tocar som:
escreve as duas linhas com timestamps e o intervalo em milissegundos num
ficheiro de evidencia em docs/forja/evidence/:

    .venv\Scripts\python -m jarvis.voz --prova-silencio
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import logging
import os
import subprocess
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
    PASTA_EVIDENCIA_FORJA,
    PASTA_MODELOS_PIPER,
    caminho_evidencia_de_saida,
    caminho_para_mostrar,
    caminho_wav_de_saida,
    garantir_pasta,
    ler_wav_pcm16,
)
from jarvis.canal_claude import verificar_executavel_seguro  # noqa: E402
from jarvis.consola import forcar_consola_utf8  # noqa: E402

#: A voz decidida pelo Scout (D47/S4). Nao mudar sem uma nova decisao dele.
NOME_DA_VOZ = "pt_PT-tugao-medium"
MODELO_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx"
CONFIG_ONNX = PASTA_MODELOS_PIPER / f"{NOME_DA_VOZ}.onnx.json"

#: Ambiente que obriga o interprete Python do `piper.exe` a ler o stdin em
#: UTF-8 — o mesmo bug e a mesma correcao da T3 (scripts/gerar_wav.py).
AMBIENTE_UTF8 = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: Fragmentos que marcam uma variavel de ambiente como segredo (D50.9): o
#: `piper.exe` e codigo GPL de terceiros e nao tem nada que fazer com o token
#: vivo da sessao-mae do Claude Code nem com chaves de terceiros do Sponsor.
PADROES_DE_VARIAVEL_SENSIVEL = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "APIKEY",
    "_KEY",
)


def variavel_sensivel(nome: str) -> bool:
    """True para variaveis de ambiente que um filho de terceiros nao deve ver."""
    maiusculas = nome.upper()
    if maiusculas.startswith("CLAUDE"):
        return True
    return any(padrao in maiusculas for padrao in PADROES_DE_VARIAVEL_SENSIVEL)


@contextlib.contextmanager
def _ambiente_sem_segredos():
    """Tira os segredos de `os.environ` enquanto o piper.exe corre (D50.9)."""
    escondidas = {nome: valor for nome, valor in os.environ.items() if variavel_sensivel(nome)}
    for nome in escondidas:
        del os.environ[nome]
    try:
        yield sorted(escondidas)
    finally:
        os.environ.update(escondidas)


def caminho_do_piper_exe() -> Path:
    """O piper.exe DESTE venv (mesma pasta Scripts/ do interprete a correr).

    Caminho absoluto de proposito (TECHNOLOGY.md S4): nunca o PATH nem
    PIPER_PATH. `verificar_executavel_seguro` e defesa em profundidade (D48.1):
    este candidato nunca vem do PATH nem de configuracao, so aqui por
    seguranca extra.
    """
    candidato = Path(sys.executable).resolve().parent / "piper.exe"
    verificar_executavel_seguro(candidato)
    if not candidato.is_file():
        raise FileNotFoundError(
            f"piper.exe nao encontrado em '{candidato}'. Instalar com: "
            '.venv\\Scripts\\pip install "RealtimeTTS[piper]==0.8.5" piper-tts==1.8.0 (D47)'
        )
    return candidato


def _preparar_encoding_do_piper() -> None:
    """Poe o ambiente em UTF-8 para o piper.exe (bug corrigido na T3).

    Atribuicao direta, nao setdefault: um PYTHONUTF8=0 herdado do ambiente do
    Sponsor traria o bug de volta em silencio (mesma nota de gerar_wav.py).
    """
    os.environ.update(AMBIENTE_UTF8)


# --- Silencio imediato: matar a sintese em curso (D60, TECHNOLOGY.md S12) ---

#: Fasquia da D60: do pedido de paragem (Ctrl+C, saida do processo ou
#: "cala-te") ao ultimo sample de audio, no maximo isto.
LIMITE_DE_SILENCIO_MS = 500.0


class MorteDoPiper:
    """A metade "sabe morrer" do motor de voz (TECHNOLOGY.md S12, opcao A).

    O `PiperEngine` do RealtimeTTS 0.8.5 sintetiza com um `subprocess.run()`
    BLOQUEANTE e nao guarda o processo em lado nenhum
    (`RealtimeTTS/engines/piper_engine.py:134-142`, confirmado nesta task
    contra a versao instalada): por isso um `TextToAudioStream.stop()` NAO
    interrompe uma sintese em curso — o `piper.exe` corre ate ao fim e so
    depois e que tudo para. Esta classe reimplementa `synthesize()` com
    `subprocess.Popen`, guarda o processo ANTES de `communicate()` bloquear, e
    expoe `matar_agora()` para outra thread (a do Ctrl+C) o poder matar.

    E um mixin de proposito: o `PiperEngine` so existe depois do import tardio
    do RealtimeTTS (ver `_construir_stream`), e uma classe nao pode herdar de
    algo que ainda nao foi importado. A classe final compoe-se em
    `classe_do_motor_com_morte()`; assim esta logica fica testavel sozinha,
    sem RealtimeTTS, sem Piper e sem dispositivo de audio (D61).

    ACHADO DE TASK, que a S12 mandava escrever se aparecesse: ha mesmo uma
    corrida entre a thread que sintetiza e a que cala, por isso existe o
    `_tranca_do_processo`. Ele protege SO as atribuicoes de `_processo_atual`
    e a leitura que `matar_agora()` faz; nunca fica preso durante o
    `communicate()`, senao calar ficaria a espera da sintese — que e
    exatamente o defeito que esta classe existe para fechar.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._tranca_do_processo = threading.Lock()
        self._processo_atual: subprocess.Popen | None = None
        self._calado = False
        super().__init__(*args, **kwargs)

    # -- o que o RealtimeTTS chama (mesma assinatura do PiperEngine)

    def synthesize(self, text: str, sentence_count: int = 0) -> bool:
        """Sintetiza `text` com o piper.exe, com o processo a jeito de morrer."""
        # `BaseEngine.synthesize` (base_engine.py:278-290) so faz estas duas
        # preparacoes; chamam-se a mao porque `super().synthesize()` aqui seria
        # o `subprocess.run` bloqueante que este wrapper substitui.
        self.stop_synthesis_event.clear()
        self._trim_silence_start_pending = True

        if not self.voice:
            print("No voice set. Please provide a PiperVoice configuration.")
            return False

        # cmd_list verbatim do piper_engine.py:117-128 (o "--output-raw"
        # repetido e do original: garante que o -c e lido antes dele).
        cmd_list = [self.piper_path, "-m", self.voice.model_file, "--output-raw"]
        if self.voice.config_file:
            cmd_list.extend(["-c", self.voice.config_file])
        cmd_list.append("--output-raw")
        if getattr(self, "debug", False):
            print(f"Running Piper with args: {cmd_list}")

        with self._tranca_do_processo:
            if self._calado:
                # Ja houve pedido de silencio: nao se comeca nada de novo
                # (D60(1): nem o resto da frase, nem uma despedida).
                return False
            try:
                processo = subprocess.Popen(
                    cmd_list,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                )
            except FileNotFoundError:
                print(f"Error: Piper executable not found at '{self.piper_path}'.")
                return False
            # ANTES do communicate() (S12): e este handle que da ao Ctrl+C
            # alguem para matar enquanto esta thread esta bloqueada la dentro.
            self._processo_atual = processo

        try:
            saida, erro = processo.communicate(input=text.encode("utf-8"))
        finally:
            with self._tranca_do_processo:
                if self._processo_atual is processo:
                    self._processo_atual = None

        if self._calado:
            # Morto a pedido, ou acabado mesmo em cima do pedido: o audio NAO
            # entra na fila, senao seria dito depois do "cala-te".
            return False
        if processo.returncode != 0:
            detalhe = (erro or b"").decode("utf-8", errors="replace")
            print(f"Error running Piper: {detalhe}")
            return False
        self.queue.put(saida)
        return True

    def stop(self):
        """O `stream.stop()` chama isto (text_to_stream.py:949).

        A classe-base so marca um evento que o `PiperEngine` nunca le (S12,
        achado 2); aqui mata-se mesmo o processo, para que ate um
        `stream.stop()` sozinho corte a sintese em curso.
        """
        self.matar_agora()
        return super().stop()

    # -- o que o jarvis chama (via `calar_agora`)

    def matar_agora(self) -> bool:
        """Mata o `piper.exe` em curso, se houver. True = matou mesmo um.

        Nunca levanta e pode ser chamada de qualquer thread, as vezes que for
        preciso: o Ctrl+C, a saida do processo e o "cala-te" podem cair todos
        uns em cima dos outros.
        """
        with self._tranca_do_processo:
            self._calado = True
            processo = self._processo_atual
            self._processo_atual = None
        if processo is None:
            return False
        try:
            if processo.poll() is not None:
                return False
            processo.kill()  # Windows: TerminateProcess, sem espera (S12)
        except Exception:  # noqa: BLE001 - calar nunca pode levantar
            return False
        return True


#: A classe composta (mixin + PiperEngine real), criada uma unica vez.
_CLASSE_DO_MOTOR: type | None = None


def classe_do_motor_com_morte(piper_engine: type) -> type:
    """A subclasse real do `PiperEngine` que sabe morrer (S12)."""
    global _CLASSE_DO_MOTOR
    if _CLASSE_DO_MOTOR is None or not issubclass(_CLASSE_DO_MOTOR, piper_engine):
        _CLASSE_DO_MOTOR = type("PiperEngineComMorte", (MorteDoPiper, piper_engine), {})
    return _CLASSE_DO_MOTOR


#: Quem esta a falar AGORA (o `TextToAudioStream` da chamada a `falar()` em
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
    estivesse a chegar do Claude Code (D60(1)).
    """
    with _TRANCA_DA_VOZ:
        return _calado_ate_novo_aviso


def retomar_a_voz() -> None:
    """Desfaz o silencio definitivo.

    O processo do jarvis nunca chama isto (depois de um Ctrl+C nada volta a
    falar, D60(1)): existe para os testes e para quem use este modulo como
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
    #: etapas (D2/D11).
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
#: da T12 (jarvis/app.py, ruido do encerramento): descarta-se SO o ruido
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
        f"SILENCIO pedido ({motivo or 'sem motivo'}): matar a sintese do piper.exe "
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

    E o UNICO mecanismo de silenciamento do jarvis (D60(2)): usam-no o handler
    de Ctrl+C, a saida do processo e a accao "calar" da lista branca da D4.d (e
    o equivalente ingles quando existir). A ordem — `matar_agora()` primeiro,
    `stream.stop()` depois — e a da S12 e nao e negociavel: parar a reproducao
    com uma sintese viva deixaria o `stop()` a espera do `piper.exe`.

    `definitivo=True` (Ctrl+C, saida do processo) marca tambem o modulo como
    calado, e a partir dai `falar()` recusa tudo (D60(1)).

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

        # (2) e so agora a reproducao ja sintetizada (achado 4 da S12: corta em
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


def _construir_stream():
    """Constroi o TextToAudioStream ligado ao PiperEngine real (S4)."""
    # O RealtimeTTS importa o pydub (dependencia transitiva, so para outros
    # motores que este jarvis nunca usa) e o pydub avisa em stderr que nao
    # encontra o ffmpeg (QA-close-1.md, finding 4a): o caminho de voz real e
    # o piper.exe chamado como executavel e nunca usa ffmpeg, por isso o aviso
    # e ruido puro para o Sponsor. So este import fica silenciado, so este
    # aviso: warnings.filters volta ao estado de antes ao sair do `with`
    # (contrato do proprio contextlib.catch_warnings), nada fica global.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=RuntimeWarning, module=r"pydub\.utils"
        )
        from RealtimeTTS import PiperEngine, PiperVoice, TextToAudioStream

    if not MODELO_ONNX.is_file() or not CONFIG_ONNX.is_file():
        raise FileNotFoundError(
            f"voz '{NOME_DA_VOZ}' nao encontrada em {PASTA_MODELOS_PIPER}. Descarregar com: "
            f".venv\\Scripts\\python -m piper.download_voices {NOME_DA_VOZ} "
            f"--download-dir {PASTA_MODELOS_PIPER} (a voz e 'tugão' com til; ver docs/MODELOS.md)"
        )
    piper_exe = caminho_do_piper_exe()
    _preparar_encoding_do_piper()
    voz = PiperVoice(model_file=str(MODELO_ONNX), config_file=str(CONFIG_ONNX))
    # O motor e o PiperEngine decidido na S4, com a morte da S12 por cima: sem
    # isto, nem o Ctrl+C nem o "cala-te" conseguem interromper uma sintese em
    # curso, porque o PiperEngine original nao guarda o processo (D60).
    motor = classe_do_motor_com_morte(PiperEngine)(voice=voz, piper_path=str(piper_exe))
    # tokenizer="rule-based": o default "nltk+rule-based" faz o stream2sentence
    # descarregar punkt_tab da rede na primeira corrida (achado da T3, fora do
    # registo de modelos da D14e); uma frase de cada vez nao precisa dele.
    return TextToAudioStream(motor, language="pt", tokenizer="rule-based")


def _duracao_do_wav(caminho: Path) -> float:
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if taxa <= 0 or canais <= 0:
        return 0.0
    return (len(dados) / 2 / canais) / taxa


#: `motivo_falha` de quem nao falou porque lhe mandaram calar (D60), para
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
    #: Nao vazio quando falou=False (D35.4): a razao do fallback para texto.
    motivo_falha: str = ""


#: D61: `falar()` sem `ficheiro=` e sem `com_som=True` e um erro de teste, nao
#: um "tentar e falhar em silencio" — recusa-se ANTES de tocar em
#: `_construir_stream()`, por isso nenhum dispositivo de audio chega a ser
#: considerado. Nao ha variavel de ambiente para isto (D61.2).
MOTIVO_SEM_OPT_IN = (
    "falar() recusado (D61): sem ficheiro= e sem com_som=True nao se abre nenhum "
    "dispositivo de audio; ouvir e sempre opt-in explicito (flag --com-som na CLI, "
    "nunca uma variavel de ambiente)"
)


def falar(
    texto: str, *, ficheiro: str | Path | None = None, com_som: bool = False
) -> ResultadoFala:
    """Fala `texto` em voz alta (Piper), ou grava-o num WAV se `ficheiro` for dado.

    D61/S13: sem `ficheiro=` E sem `com_som=True`, esta funcao RECUSA-SE a
    tocar — devolve `falou=False` com `motivo_falha=MOTIVO_SEM_OPT_IN` antes de
    tentar construir seja o que for, e portanto antes de qualquer hipotese de
    abrir um dispositivo de audio. So com `ficheiro=`, sintetiza para WAV com
    `muted=True` e nunca abre dispositivo nenhum. Com `com_som=True` sai som,
    e e a unica coisa que o faz: sozinho toca a serio nas colunas (e o que
    `jarvis/app.py` liga no arranque real), com `ficheiro=` toca E grava, tal
    como `scripts/gerar_wav.py --com-som`.

    NUNCA levanta (D35.4): qualquer falha na sintese, na reproducao ou na
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
        # imprime o recurso da D35.4 — quem pediu silencio pediu silencio
        # (D60(1)). Este e o guarda a montante de TUDO o que se segue: corre
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
        # thread, chega ao motor e ao stream desta frase (D60/S12).
        _guardar_voz_ativa(stream)
        try:
            stream.feed(texto_limpo)
            with _ambiente_sem_segredos():
                if saida is not None:
                    garantir_pasta(saida.parent)
                    # muted=not com_som: sem opt-in (o caso de todos os testes,
                    # autotestes e arneses) escreve o WAV sem abrir dispositivo
                    # nenhum; com --com-som toca E grava, exatamente como
                    # scripts/gerar_wav.py --com-som (D61: `--com-som` quer
                    # dizer a mesma coisa em todo o repositorio).
                    stream.play(muted=not com_som, output_wavfile=str(saida))
                else:
                    stream.play(muted=False)
        except BaseException as interrupcao:
            if isinstance(interrupcao, Exception):
                raise  # falha normal: vai para o recuo da D35.4 la em baixo
            # Ctrl+C (ou SystemExit) a subir por aqui: calar AGORA, antes de
            # o `finally` largar o registo da voz ativa — senao ficava um
            # piper.exe vivo a acabar a frase sozinho (D60).
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

    if saida is not None:
        try:
            duracao_s = _duracao_do_wav(saida)
        except Exception as erro:  # noqa: BLE001 - D35.4: `falar()` nunca levanta
            # Acontece com um WAV cortado a meio por um "cala-te": o ficheiro
            # existe mas pode nao fechar como WAV valido. Nao e uma falha da
            # voz a dizer ao Sponsor, e o resultado de ele ter mandado calar.
            return ResultadoFala(falou=False, caminho=saida, motivo_falha=f"WAV por ler: {erro}")
        return ResultadoFala(falou=True, caminho=saida, duracao_s=duracao_s)
    return ResultadoFala(falou=True)


# --- Autoteste (sem depender de um dispositivo de audio real) --------------


def _autoteste() -> int:
    """Prova a sintese para ficheiro, o confinamento do caminho e o fallback.

    Nao toca em `audio/` (usa uma pasta temporaria) e nao exige um
    dispositivo de saida de som: so o ramo --ficheiro e testado a falar a
    serio; o fallback e provocado com um motor falso, sem tocar no piper.exe.
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

    # 3. caminho fora do repositorio e recusado ANTES de tocar no Piper
    # (mesma regra de audio_util.caminho_wav_de_saida, D48.2).
    with tempfile.TemporaryDirectory() as pasta:
        fora_da_raiz = str(Path(pasta) / "fora.wav")
        resultado_fora = falar("nunca deveria escrever aqui", ficheiro=fora_da_raiz)
        verificar("caminho fora do repo: falou=False", resultado_fora.falou, False)
        verificar("caminho fora do repo: nada escrito", Path(fora_da_raiz).exists(), False)

    # 4. fallback explicito (D35.4): um motor que rebenta nunca propaga a
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
#: sintese demorar o suficiente a ser apanhada a MEIO, que e o pior caso da
#: S12: com o `piper.exe` vivo, `stream.stop()` sozinho ficaria a espera dele.
FRASE_DA_PROVA = (
    "Esta frase existe so para a prova do silencio imediato: e comprida de "
    "proposito para a sintese ainda estar a meio quando o pedido de paragem "
    "chegar, e assim medir o pior caso do Piper."
)


def _agora_com_ms(quando: datetime.datetime | None = None) -> str:
    """Timestamp local com milissegundos, o MESMO formato do log do jarvis.

    Copiado de `jarvis/app.py::agora_iso` em vez de importado: o `app` importa
    este modulo, e um import ao contrario fechava o circulo.
    """
    quando = quando or datetime.datetime.now()
    return quando.strftime("%Y-%m-%d %H:%M:%S.") + f"{quando.microsecond // 1000:03d}"


def _esperar_pela_sintese(limite_s: float = 30.0) -> bool:
    """Espera ate o `piper.exe` desta frase estar mesmo vivo a sintetizar."""
    fim = time.perf_counter() + limite_s
    while time.perf_counter() < fim:
        motor = getattr(_stream_ativo, "engine", None)
        if getattr(motor, "_processo_atual", None) is not None:
            return True
        time.sleep(0.01)
    return False


def _intervalo_entre_linhas_ms(pedido: str, fim: str) -> float:
    """Os milissegundos entre as DUAS linhas de log, lidos dos timestamps."""
    formato = "%Y-%m-%d %H:%M:%S.%f"
    t0 = datetime.datetime.strptime(pedido.split(" SILENCIO")[0], formato)
    t1 = datetime.datetime.strptime(fim.split(" SILENCIO")[0], formato)
    return (t1 - t0).total_seconds() * 1000.0


def _piper_vivo() -> str:
    """O que o Windows diz sobre processos `piper.exe` ainda vivos."""
    try:
        visto = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq piper.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as erro:  # noqa: BLE001 - e so evidencia extra
        return f"nao verificado ({erro})"
    saida = " ".join(visto.stdout.split())
    return saida or "(sem saida)"


def _esperar_por_piper_no_sistema(limite_s: float = 30.0) -> bool:
    """Espera ate o Windows mostrar um `piper.exe` vivo (sem handle nenhum).

    E a unica forma de saber que a sintese comecou no caso "antes": o
    `PiperEngine` original nao guarda o processo em lado nenhum — que e
    exatamente o defeito que a S12 descreve.
    """
    fim = time.perf_counter() + limite_s
    while time.perf_counter() < fim:
        if "piper.exe" in _piper_vivo():
            return True
        time.sleep(0.02)
    return False


def _caso_sem_wrapper() -> dict:
    """O MESMO pedido de paragem com o `PiperEngine` original: o "antes".

    Mede o que a S12 previu em teoria (achado 3): com a sintese em curso,
    `stream.stop()` sozinho fica a espera que o `piper.exe` acabe a frase.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"pydub\.utils")
        from RealtimeTTS import PiperEngine, PiperVoice, TextToAudioStream

    piper_exe = caminho_do_piper_exe()
    _preparar_encoding_do_piper()
    voz_do_piper = PiperVoice(model_file=str(MODELO_ONNX), config_file=str(CONFIG_ONNX))
    motor = PiperEngine(voice=voz_do_piper, piper_path=str(piper_exe))
    stream = TextToAudioStream(motor, language="pt", tokenizer="rule-based")

    caminho = caminho_wav_de_saida(Path("audio") / "_prova_silencio_sem_wrapper.wav")
    garantir_pasta(caminho.parent)
    acabou = threading.Event()

    def tocar() -> None:
        try:
            with _ambiente_sem_segredos():
                # muted=True: tambem aqui nao se abre dispositivo de audio.
                stream.play(muted=True, output_wavfile=str(caminho))
        finally:
            acabou.set()

    stream.feed(FRASE_DA_PROVA)
    thread = threading.Thread(target=tocar, name="prova-sem-wrapper", daemon=True)
    thread.start()
    apanhou = _esperar_por_piper_no_sistema()

    inicio = time.perf_counter()
    stream.stop()
    acabou.wait(timeout=120)
    intervalo_ms = (time.perf_counter() - inicio) * 1000.0
    caminho.unlink(missing_ok=True)
    return {
        "apanhou_a_sintese": apanhou,
        "intervalo_ms": intervalo_ms,
        "piper_vivo_depois": _piper_vivo(),
    }


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
        # ficheiro= sem com_som: muted=True no RealtimeTTS, NUNCA abre
        # dispositivo de saida de audio (D61/S13). Esta prova nao faz barulho.
        resultado["fala"] = falar(FRASE_DA_PROVA, ficheiro=str(caminho))

    thread = threading.Thread(target=falar_em_ficheiro, name="prova-silencio", daemon=True)
    thread.start()
    apanhou_a_sintese = _esperar_pela_sintese()
    marca_do_pedido = time.perf_counter()
    silencio = calar_agora(motivo, definitivo=definitivo, registar=registar)
    # A cauda: depois de `synthesize()` devolver False, o `synthesize_worker`
    # do RealtimeTTS faz um `time.sleep(0.2)` fixo antes de sair
    # (text_to_stream.py:811-813, ramo `len(self.engines) == 1`). Esses ~200 ms
    # NAO produzem nem tocam um unico sample — o `piper.exe` ja morreu e o
    # ficheiro ja nao cresce — mas contam-se aqui na mesma, para a fasquia da
    # D60 ser medida contra o pior numero possivel e nao contra o melhor.
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
        "piper_vivo_depois": _piper_vivo(),
    }


def _prova_de_silencio(caminho_pedido: str | None = None) -> int:
    """Evidencia de tempo da D60(4)(b): os dois timestamps e o intervalo.

    Corre a voz REAL (Piper, voz pt-PT) pelo caminho de FICHEIRO, sem abrir
    nenhum dispositivo de saida de audio e sem microfone. Faz os dois casos:
    o "cala-te" da lista branca (nao definitivo) e o Ctrl+C / saida do
    processo (definitivo, depois do qual nada novo e falado).
    """
    # Confinamento do --evidencia a docs/forja/evidence/ (bloqueador 2 do
    # Security Reviewer, T4/T12): validado JA, antes de gastar tempo a
    # sintetizar seja o que for, para um caminho invalido nunca chegar a
    # `garantir_pasta`/`write_text` (que criavam pastas e escreviam fora do
    # repo com os privilegios do Sponsor).
    try:
        destino = (
            caminho_evidencia_de_saida(caminho_pedido)
            if caminho_pedido
            else PASTA_EVIDENCIA_FORJA / f"silencio-{datetime.datetime.now():%Y%m%d-%H%M%S}.md"
        )
    except ValueError as erro:
        print(f"FALHOU (confinamento do --evidencia, D1/D10): {erro}", file=sys.stderr)
        return 1
    garantir_pasta(destino.parent)

    print("=== jarvis - prova do silencio imediato (D60) - sem som, sem microfone ===")
    # O "antes": o mesmo pedido de paragem sem o wrapper da S12, para o numero
    # da fasquia ter com o que ser comparado.
    try:
        antes = _caso_sem_wrapper()
    except Exception as erro:  # noqa: BLE001 - o "antes" e contexto, nao a prova
        antes = {"erro": str(erro)}
    casos = [
        _um_caso_da_prova("cala-te (lista branca D4.d)", definitivo=False, nome_do_wav="_prova_silencio_calar.wav"),
    ]
    retomar_a_voz()  # o caso anterior nao e definitivo; o proximo comeca limpo
    casos.append(
        _um_caso_da_prova("Ctrl+C (D30/D60)", definitivo=True, nome_do_wav="_prova_silencio_ctrlc.wav")
    )

    # Depois do Ctrl+C nada NOVO e falado (D60(1)): nem uma despedida.
    depois = falar("Adeus, ate a proxima.", ficheiro="audio/_prova_silencio_depois.wav")
    escreveu_alguma_coisa = (RAIZ / "audio" / "_prova_silencio_depois.wav").is_file()
    (RAIZ / "audio" / "_prova_silencio_depois.wav").unlink(missing_ok=True)

    agora = datetime.datetime.now()
    partes: list[str] = []
    partes.append("# Evidencia de tempo do silencio imediato (T4, D60)\n")
    partes.append(
        f"Gerado por `.venv\\Scripts\\python -m jarvis.voz --prova-silencio` em "
        f"{_agora_com_ms(agora)}.\n"
    )
    partes.append(
        "Voz REAL (Piper, `pt_PT-tugao-medium`) pelo caminho de FICHEIRO: nenhum dispositivo de\n"
        "saida de audio foi aberto e nenhum microfone foi usado (D61/S13 — `falar(..., ficheiro=)`\n"
        "sem `com_som` toca com `muted=True`). Os WAV de prova sao apagados no fim; a pasta `audio/` ja e\n"
        "ignorada pelo Git.\n"
    )
    partes.append(
        f"Fasquia da D60: <= {LIMITE_DE_SILENCIO_MS:.0f} ms entre o pedido de paragem e o fim do audio.\n"
    )
    partes.append("\n## Antes: o mesmo pedido sem o wrapper da S12\n")
    if "erro" in antes:
        partes.append(f"- nao medido nesta corrida: {antes['erro']}\n")
    else:
        partes.append(
            f"- `PiperEngine` original (subprocess.run bloqueante, sem handle guardado), "
            f"sintese apanhada viva: {'sim' if antes['apanhou_a_sintese'] else 'nao'}\n"
            f"- `stream.stop()` sozinho demorou **{antes['intervalo_ms']:.0f} ms** ate a voz "
            f"parar mesmo (fasquia D60: {LIMITE_DE_SILENCIO_MS:.0f} ms): "
            f"{'CUMPRIA' if antes['intervalo_ms'] <= LIMITE_DE_SILENCIO_MS else 'FALHAVA'}\n"
            f"- `tasklist` depois: {antes['piper_vivo_depois']}\n"
        )
    for caso in casos:
        duracao = caso["duracao_do_wav_s"]
        duracao_escrita = "sem WAV" if duracao is None else f"{duracao:.2f} s"
        partes.append(f"\n## Gatilho: {caso['motivo']}\n")
        partes.append(
            f"- sintese apanhada VIVA a meio (pior caso da S12): "
            f"{'sim' if caso['apanhou_a_sintese'] else 'nao'}\n"
            f"- `piper.exe` morto por `matar_agora()`: {'sim' if caso['matou_sintese'] else 'nao'}\n"
            f"- reproducao parada por `stream.stop()`: {'sim' if caso['parou_reproducao'] else 'nao'}\n"
            f"- a thread que falava terminou: {'sim' if not caso['thread_viva'] else 'NAO'}\n"
            f"- `falar()` devolveu falou={caso['falou']} ({caso['motivo_falha'] or 'sem motivo'})\n"
            f"- duracao do WAV cortado: {duracao_escrita}\n"
            f"- `tasklist` depois de calar: {caso['piper_vivo_depois']}\n"
        )
        partes.append("\nAs duas linhas de log com timestamps:\n\n```\n")
        partes.append(caso["linhas"][0] + "\n")
        partes.append(caso["linhas"][1] + "\n")
        partes.append("```\n")
        partes.append(
            f"\n**Intervalo lido dos dois timestamps: {caso['intervalo_lido_ms']:.0f} ms** "
            f"(medido por dentro com `perf_counter`: {caso['intervalo_medido_ms']:.0f} ms) — "
            f"fasquia {LIMITE_DE_SILENCIO_MS:.0f} ms: "
            f"{'CUMPRE' if caso['intervalo_lido_ms'] <= LIMITE_DE_SILENCIO_MS else 'FALHA'}\n"
        )
        partes.append(
            f"\nCom a cauda do RealtimeTTS contada por cima (o `time.sleep(0.2)` fixo do\n"
            f"`synthesize_worker`, `text_to_stream.py:811-813`, que corre DEPOIS de a sintese\n"
            f"ja estar morta e nao produz nem toca um unico sample), do pedido ate a thread da\n"
            f"voz ter mesmo acabado: **{caso['ate_a_thread_acabar_ms']:.0f} ms** — fasquia "
            f"{LIMITE_DE_SILENCIO_MS:.0f} ms: "
            f"{'CUMPRE' if caso['ate_a_thread_acabar_ms'] <= LIMITE_DE_SILENCIO_MS else 'FALHA'}\n"
        )
    partes.append("\n## Depois do Ctrl+C nada novo e falado (D60(1))\n")
    partes.append(
        f"- `falar('Adeus, ate a proxima.')` devolveu falou={depois.falou} "
        f"({depois.motivo_falha})\n"
        f"- WAV escrito por essa tentativa: {'SIM (defeito)' if escreveu_alguma_coisa else 'nenhum'}\n"
    )

    destino.write_text("".join(partes), encoding="utf-8")
    print()
    print(f"evidencia escrita em {caminho_para_mostrar(destino)}")

    falhou = any(caso["intervalo_lido_ms"] > LIMITE_DE_SILENCIO_MS for caso in casos)
    # A fasquia vale tambem contra o pior numero (com a cauda de 200 ms do
    # RealtimeTTS contada por cima), senao a prova estaria a escolher o numero
    # que lhe da jeito.
    falhou = falhou or any(caso["ate_a_thread_acabar_ms"] > LIMITE_DE_SILENCIO_MS for caso in casos)
    falhou = falhou or depois.falou or escreveu_alguma_coisa
    falhou = falhou or any(not caso["matou_sintese"] for caso in casos)
    if falhou:
        print("FALHOU: ver o ficheiro de evidencia.")
        return 1
    print("OK: os dois gatilhos calaram dentro da fasquia e nada novo foi falado.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # T11: unico ponto de entrada que ainda nao forcava a consola para UTF-8.
    # Sem isto, `python -m jarvis.voz --help` rebenta com UnicodeEncodeError
    # numa consola em codepage 850 (a do Sponsor, QA-close-1.md).
    forcar_consola_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("texto", nargs="?", help="frase a dizer em voz alta, em portugues europeu")
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

    if args.autoteste:
        return _autoteste()

    if args.prova_silencio:
        return _prova_de_silencio(args.evidencia)

    if not args.texto or not args.texto.strip():
        parser.error("e preciso o texto a dizer (ou --autoteste)")

    if args.ficheiro is None and not args.com_som:
        # D61: sem --ficheiro e sem --com-som, o comando recusa-se em vez de
        # tocar nas colunas por omissao. Nao chega a chamar falar().
        print("=== jarvis - voz (Piper, voz pt_PT-tugao-medium) ===")
        print(f"texto    = {args.texto!r}")
        print(f"RECUSADO (D61): {MOTIVO_SEM_OPT_IN}")
        print("Usa --ficheiro <caminho.wav> para gravar, ou --com-som para ouvir a serio.")
        return 1

    print("=== jarvis - voz (Piper, voz pt_PT-tugao-medium) ===")
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
