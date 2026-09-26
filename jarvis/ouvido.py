r"""Ouvido residente do jarvis: microfone, tecla de falar, palavra de ativacao e VAD.

Captura o microfone do config.toml sem parar e entrega FRASES ja transcritas a
um unico callback, por dois gatilhos:

  tecla de falar   segurar a tecla configurada ([ouvido].tecla) para falar,
                   soltar para acabar. A tecla le-se pela API do Windows
                   (`GetAsyncKeyState`, via ctypes): nao consome a tecla, nao
                   precisa de foco na consola e nao traz dependencia nova.
  maos-livres      a palavra de ativacao (openWakeWord) abre a escuta; o fim
                   da fala decide-o o VAD (webrtcvad) depois de um silencio.

Janela de escuta: `abrir_escuta(segundos, para=...)` abre uma escuta como a
das maos-livres, mas sem palavra de ativacao: a resposta a um recap pendente
(`ESCUTA_RECAP`) ou a conversa com o Claude, quando ele faz uma pergunta
(`ESCUTA_CONVERSA`). Se ninguem comecar a falar dentro do prazo, fecha
sozinha. So existe com VAD; sem ele, fica a tecla de falar. A espera de fala
so guarda os ultimos 300 ms de audio, e a resposta ao recap deita fora o
primeiro meio segundo, onde ainda pode soar o fim da voz do jarvis.

Palavra de ativacao da lingua do config ([ouvido].lingua): "hey jarvis" em
ingles (modelo pre-treinado do openWakeWord) e "boas jarvis" em portugues
(modelo treinado localmente por `scripts/treinar_ativacao.py`). O limiar vem
de [ouvido].limiar_ativacao, escolhido pelos numeros de
`scripts/avaliar_ativacao.py`. Antes de a frase seguir, a palavra de ativacao
e retirada do INICIO do texto transcrito (`retirar_palavra_de_ativacao`): so as
palavras exatas da frase de ativacao configurada, nunca uma grafia parecida.

O que NUNCA passa: audio fora da tecla premida ou de uma ativacao detetada e
deitado fora no proprio chunk em que chega. Em repouso cada chunk so vai ao
detetor da palavra de ativacao (que guarda so as suas caracteristicas internas
e e reiniciado a cada frase); nunca entra num buffer de frase, nunca chega ao
motor de transcricao e nunca chega ao callback.

Transcricao quente: o motor (`jarvis.stt`) carrega UMA vez em `preparar()` e
transcreve logo um segundo de silencio para aquecer, por isso a primeira frase
real ja nao paga o arranque do modelo.

Duas threads: a de captura le chunks de 30 ms, amostra a tecla depois de cada
leitura e corre a maquina de estados (barata: tecla, VAD e detetor); a de
transcricao tira frases de uma fila curta, transcreve e chama o callback. A
captura nunca espera pela transcricao nem pelo callback, por isso o estado da
tecla corresponde sempre ao audio que acabou de chegar.

Sinais de escuta: cada inicio e fim de escuta escreve uma linha na consola
(sempre) e, so com `com_som=True` (a flag --com-som), toca um bip curto numa
thread a parte. Nenhum teste nem autoteste toca som.

Uso:

    .venv\Scripts\python -m jarvis.ouvido --autoteste
    .venv\Scripts\python -m jarvis.ouvido --ouvir [--com-som] [--sem-ativacao]
    .venv\Scripts\python -m jarvis.ouvido --medir audio\pasta-de-wavs [--verificar]

`--medir` alimenta cada WAV (ate 5 s) pelo MESMO caminho da tecla de falar em
modo ficheiro: a tecla fica premida enquanto o ficheiro toca e e solta no
silencio a seguir; mede-se do soltar da tecla ate o texto existir.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from jarvis.audio_util import RAIZ, ler_wav_pcm16, reamostrar_pcm16
from jarvis.config import LIMIAR_DE_ATIVACAO_PADRAO
from jarvis.stt import TAXA_DO_MOTOR, MotorBase, MotorIndisponivel, criar_motor, duracao_pcm16

TAXA = TAXA_DO_MOTOR

#: 30 ms por chunk: um dos tres tamanhos que o webrtcvad aceita a 16 kHz.
AMOSTRAS_POR_CHUNK = 480
BYTES_POR_CHUNK = AMOSTRAS_POR_CHUNK * 2
DURACAO_DO_CHUNK_S = AMOSTRAS_POR_CHUNK / TAXA

#: Uma tecla premida menos do que isto e um toque acidental: descarta-se.
MINIMO_DA_TECLA_S = 0.25
#: Teto de uma frase (memoria e latencia limitadas). Com a tecla, passado o
#: teto a frase fecha-se e o resto ate soltar e deitado fora.
MAXIMO_DA_FRASE_S = 30.0

#: Maos-livres: tempo para a fala comecar depois da palavra de ativacao.
ESPERA_PELA_FALA_S = 5.0
#: Logo a seguir a ativacao ainda soa o fim da propria palavra; o inicio da
#: fala so conta depois disto (o audio fica na frase na mesma).
SURDEZ_APOS_ATIVACAO_S = 0.25
#: Chunks seguidos com voz para a fala contar como comecada (90 ms).
CHUNKS_PARA_COMECAR_A_FALA = 3
#: Janela sem palavra de ativacao a espera de fala: so os ultimos chunks ficam
#: guardados (300 ms antes do inicio da fala), nunca o silencio todo da espera.
#: Assim o teto da frase conta desde o inicio da fala e nao desde a abertura.
CHUNKS_ANTES_DA_FALA = 10
#: A resposta ao recap abre quando a voz do jarvis devolve o controlo, mas o fim
#: do recap ("Send it?") ainda pode estar no buffer da placa de som e do
#: microfone. O audio deste intervalo e deitado fora: nem conta para o VAD nem
#: entra na frase.
GUARDA_APOS_A_VOZ_S = 0.5
#: Silencio que fecha a frase das maos-livres.
SILENCIO_FINAL_S = 0.6
#: 0 (menos agressivo) a 3 (mais agressivo a chamar ruido ao que nao e voz).
AGRESSIVIDADE_DO_VAD = 2

PASTA_MODELOS_OWW = RAIZ / "models" / "openwakeword"
MODELO_MELSPEC = PASTA_MODELOS_OWW / "melspectrogram.onnx"
MODELO_EMBEDDING = PASTA_MODELOS_OWW / "embedding_model.onnx"
LIMIAR_DE_ATIVACAO = LIMIAR_DE_ATIVACAO_PADRAO

#: A palavra de ativacao de cada lingua e o modelo openWakeWord que a deteta.
#: "hey_jarvis" e o modelo pre-treinado; "boas_jarvis" so existe depois de
#: `scripts/treinar_ativacao.py` (procedimento em docs/MODELOS.md).
PALAVRAS_DE_ATIVACAO = {"en": "hey jarvis", "pt": "boas jarvis"}
MODELOS_DE_ATIVACAO = {
    "en": PASTA_MODELOS_OWW / "hey_jarvis_v0.1.onnx",
    "pt": PASTA_MODELOS_OWW / "boas_jarvis.onnx",
}
MODELO_ATIVACAO = MODELOS_DE_ATIVACAO["en"]


def modelo_de_ativacao(lingua: str | None) -> Path:
    """O modelo da palavra de ativacao da lingua (sem lingua: o ingles)."""
    return MODELOS_DE_ATIVACAO.get(lingua or "en", MODELO_ATIVACAO)


def palavra_de_ativacao(lingua: str | None) -> str:
    """A palavra que o modelo de `modelo_de_ativacao(lingua)` ouve."""
    return PALAVRAS_DE_ATIVACAO.get(lingua or "en", PALAVRAS_DE_ATIVACAO["en"])


def _sem_acentos(palavra: str) -> str:
    import unicodedata

    decomposta = unicodedata.normalize("NFKD", palavra)
    return "".join(c for c in decomposta if not unicodedata.combining(c)).casefold()


_PALAVRA = re.compile(r"\w+")
#: Espacos e pontuacao que ficam colados a palavra retirada.
_DEPOIS_DA_ATIVACAO = re.compile(r"[\s,.;:!?\u00a1\u00bf\u2026\u2014\u2013-]*")


def retirar_palavra_de_ativacao(
    texto: str, palavras_de_ativacao: Iterable[str], *, aceitar_cauda: bool = True
) -> tuple[str, str | None]:
    """Tira a palavra de ativacao do INICIO do texto transcrito.

    Compara palavra a palavra, sem maiusculas nem acentos, contra as frases de
    ativacao dadas (a da lingua configurada), e SO contra elas: uma grafia
    parecida ("jorvis") nao e a palavra de ativacao e fica no texto. Com
    `aceitar_cauda`, tambem sai o fim da frase de ativacao sozinho ("jarvis"
    de "hey jarvis"), que e o que sobra quando a escuta maos-livres abre a
    meio da palavra. O resto do texto fica tal e qual (maiusculas, acentos e
    pontuacao); a pontuacao logo a seguir a palavra retirada sai com ela.

    Nunca esvazia a frase: se nao sobra nenhuma palavra, o texto fica inteiro.
    Devolve (texto_sem_a_palavra, o_que_saiu_ou_None).
    """
    encontradas = list(_PALAVRA.finditer(texto))
    normalizadas = [_sem_acentos(m.group()) for m in encontradas]
    candidatas: list[list[str]] = []
    for frase in palavras_de_ativacao:
        alvo = [_sem_acentos(p) for p in _PALAVRA.findall(frase)]
        inicios = range(len(alvo)) if aceitar_cauda else range(1)
        candidatas.extend(alvo[inicio:] for inicio in inicios if alvo[inicio:])
    # A mais comprida primeiro: "hey jarvis" antes de "jarvis".
    for alvo in sorted(candidatas, key=len, reverse=True):
        n = len(alvo)
        if len(encontradas) <= n or normalizadas[:n] != alvo:
            continue
        fim = _DEPOIS_DA_ATIVACAO.match(texto, encontradas[n - 1].end()).end()
        return texto[fim:], texto[encontradas[0].start() : encontradas[n - 1].end()]
    return texto, None


#: Frases a espera de transcricao. Cheia, a frase nova e descartada com uma
#: linha no log (nunca cresce sem limite).
FRASES_EM_ESPERA = 4
AQUECIMENTO_S = 1.0

#: Metas da tecla de falar, verificadas por `--medir --verificar`.
META_P50_MS = 700.0
META_P95_MS = 1200.0
META_SINAL_MS = 150.0
MAXIMO_DO_WAV_MEDIDO_S = 5.0

GATILHO_TECLA = "tecla"
GATILHO_ATIVACAO = "ativacao"
#: Escuta aberta pelo jarvis (resposta ao recap ou conversa), sem palavra de ativacao.
GATILHO_JANELA = "janela"

#: Para que e a janela de escuta sem palavra de ativacao.
ESCUTA_CONVERSA = "conversa"
ESCUTA_RECAP = "recap"
_NOMES_DAS_ESCUTAS = {ESCUTA_CONVERSA: "janela de conversa", ESCUTA_RECAP: "resposta ao recap"}


@dataclass(frozen=True)
class Frase:
    """Uma frase ouvida e transcrita. Instantes em `time.perf_counter()`."""

    texto: str
    gatilho: str
    lingua: str | None
    motor: str
    duracao_audio_s: float
    #: Tecla premida ou palavra de ativacao detetada.
    inicio_da_escuta: float
    #: Tecla solta ou VAD a fechar a frase: o "fim da fala" que o Sponsor sente.
    fim_da_escuta: float
    texto_pronto: float
    #: So a inferencia (sem fila).
    latencia_stt_ms: float
    score_ativacao: float | None = None
    #: A palavra de ativacao retirada do inicio do texto (tal como foi
    #: transcrita), ou None quando nao havia nenhuma.
    palavra_retirada: str | None = None

    @property
    def ms_do_fim_ao_texto(self) -> float:
        return (self.texto_pronto - self.fim_da_escuta) * 1000


@dataclass(frozen=True)
class _FraseCaptada:
    pcm16: bytes
    gatilho: str
    inicio: float
    fim: float
    score: float | None
    #: A palavra de ativacao seguida de silencio: sem audio para transcrever.
    so_ativacao: bool = False


# --- Pecas trocaveis: fonte, tecla, detetor, VAD -----------------------------


class FonteDeAudio(Protocol):
    descricao: str

    def abrir(self) -> None: ...

    def ler(self) -> bytes | None:
        """Um chunk de BYTES_POR_CHUNK bytes, ou None quando a fonte acabou."""

    def fechar(self) -> None: ...


class Tecla(Protocol):
    """`premida()` e obrigatoria. Opcional: `outra_tecla_premida()`, para
    distinguir a tecla de falar de um atalho (Ctrl+C com a mesma tecla)."""

    def premida(self) -> bool: ...


class DetetorDeAtivacao(Protocol):
    def processar(self, chunk: bytes) -> float: ...

    def reiniciar(self) -> None: ...


class Vad(Protocol):
    def e_fala(self, chunk: bytes) -> bool: ...


def _modulo_do_microfone():
    """`scripts/verificar_microfone.py`, a escolha unica do microfone.

    Carrega-se por caminho, com a mesma chave de cache dos arneses de voz, para
    o gravador e o ouvido partilharem o mesmo modulo (e as mesmas excecoes).
    """
    chave = "_jarvis_scripts_verificar_microfone"
    ja_carregado = sys.modules.get(chave)
    if ja_carregado is not None:
        return ja_carregado
    caminho = RAIZ / "scripts" / "verificar_microfone.py"
    spec = importlib.util.spec_from_file_location(chave, caminho)
    if spec is None or spec.loader is None:
        raise ImportError(f"nao foi possivel carregar '{caminho}'")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    try:
        spec.loader.exec_module(modulo)
    except BaseException:
        del sys.modules[chave]
        raise
    return modulo


#: Zeros exatos seguidos, a meio da escuta, que dao um aviso na consola: o
#: microfone ficou em mudo, perdeu a permissao ou o driver parou.
AVISO_DE_ZEROS_S = 2.0


class MicrofonePyAudio:
    """O microfone do config.toml, a 16 kHz mono, em chunks de 30 ms.

    A escolha do dispositivo e a de `verificar_microfone.abrir_microfone`, a
    mesma do gravador: so MME e depois WASAPI (reamostrado da taxa nativa),
    nunca DirectSound nem WDM-KS, e cada candidato so e aceite depois de ler
    um segundo em tempo real sem silencio digital. Sem nenhum que passe,
    `abrir()` levanta `MicrofoneInutilizavel` (um OSError) com as tentativas.

    Ja aberto, cada leitura volta a vigiar o mesmo defeito: audio lido mais
    depressa do que o relogio para a captura com erro; um troco longo de zeros
    exatos da um aviso (uma vez por troco) sem parar a escuta. `criar_pa`,
    `formato` e `relogio` existem para os testes usarem dispositivos falsos.
    """

    def __init__(
        self,
        nome_configurado: str,
        *,
        criar_pa: Callable[[], object] | None = None,
        formato: int | None = None,
        relogio: Callable[[], float] = time.monotonic,
        avisar: Callable[[str], object] = print,
    ) -> None:
        self.nome_configurado = nome_configurado
        self.descricao = "microfone por abrir"
        self.tentativas: list[str] = []
        self._criar_pa = criar_pa
        self._formato = formato
        self._relogio = relogio
        self._avisar = avisar
        self._pa = None
        self._entrada = None
        self._resto = b""
        self._inicio = 0.0
        self._lidos = 0
        self._zeros_seguidos = 0
        self._avisado = False

    def abrir(self) -> None:
        microfone = _modulo_do_microfone()
        if self._criar_pa is None:
            import pyaudio

            self._criar_pa, self._formato = pyaudio.PyAudio, pyaudio.paInt16
        self._pa = self._criar_pa()
        try:
            self._entrada = microfone.abrir_microfone(
                self._pa, self.nome_configurado, self._formato,
                amostras_por_bloco=AMOSTRAS_POR_CHUNK, relogio=self._relogio,
            )
        except BaseException:
            self.fechar()
            raise
        # O audio da prova nunca e escuta: fica de fora, como tudo o que chega
        # sem tecla nem ativacao.
        self.tentativas = self._entrada.tentativas
        self.descricao = self._entrada.escolha.descrever()
        self._resto = b""
        self._inicio = self._relogio()
        self._lidos = 0
        self._zeros_seguidos = 0
        self._avisado = False

    def ler(self) -> bytes | None:
        while len(self._resto) < BYTES_POR_CHUNK:
            bloco = self._entrada.ler()
            if not bloco:
                raise OSError("o microfone deixou de entregar audio; a escuta parou")
            self._resto += bloco
        chunk, self._resto = self._resto[:BYTES_POR_CHUNK], self._resto[BYTES_POR_CHUNK:]
        self._lidos += 1
        self._vigiar(chunk)
        return chunk

    def _vigiar(self, chunk: bytes) -> None:
        microfone = _modulo_do_microfone()
        motivo = microfone.desacerto_com_o_relogio(
            self._lidos * DURACAO_DO_CHUNK_S, self._relogio() - self._inicio, so_excesso=True
        )
        if motivo:
            raise OSError(f"o microfone deixou de ler em tempo real ({motivo}); a escuta parou")
        if chunk.count(0) != len(chunk):
            if self._avisado:
                self._avisar("ouvido | o microfone voltou a dar sinal")
            self._zeros_seguidos = 0
            self._avisado = False
            return
        self._zeros_seguidos += 1
        if not self._avisado and self._zeros_seguidos * DURACAO_DO_CHUNK_S >= AVISO_DE_ZEROS_S:
            self._avisado = True
            self._avisar(
                f"ouvido | AVISO: o microfone da zeros exatos ha {AVISO_DE_ZEROS_S:.0f} s "
                "(em mudo, sem permissao ou driver parado); nada do que disser sera ouvido"
            )

    def fechar(self) -> None:
        entrada, pa = self._entrada, self._pa
        self._entrada = self._pa = None
        if entrada is not None:
            entrada.fechar()
        if pa is not None:
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001 - fechar nunca levanta
                pass


def chunks_do_pcm(pcm16: bytes) -> list[bytes]:
    """PCM16 mono a 16 kHz cortado em chunks; o ultimo completa-se com silencio."""
    chunks = []
    for inicio in range(0, len(pcm16), BYTES_POR_CHUNK):
        chunk = pcm16[inicio : inicio + BYTES_POR_CHUNK]
        chunks.append(chunk + b"\x00" * (BYTES_POR_CHUNK - len(chunk)))
    return chunks


def pcm_do_wav(caminho: Path) -> bytes:
    """Qualquer WAV PCM16 (mono ou estereo, qualquer taxa) em PCM16 mono a 16 kHz."""
    dados, taxa, canais = ler_wav_pcm16(caminho)
    if canais not in (1, 2):
        raise ValueError(f"{caminho}: esperava 1 ou 2 canais, encontrei {canais}")
    if canais == 2:
        import numpy as np

        estereo = np.frombuffer(dados, dtype="<i2").reshape(-1, 2).astype(np.int32)
        dados = (estereo.sum(axis=1) // 2).astype("<i2").tobytes()
    return reamostrar_pcm16(dados, taxa, TAXA, canais=1)


class FonteDeFicheiro:
    """Os chunks de um PCM seguidos de silencio: o microfone em modo ficheiro.

    `dentro_do_audio` e verdade enquanto o ultimo chunk lido era do ficheiro;
    e o que a `TeclaDoFicheiro` usa para "segurar a tecla" durante a fala.
    """

    def __init__(
        self, pcm16: bytes, *, silencio_depois_s: float = 1.0, ritmo_real: bool = False, descricao: str = "ficheiro"
    ) -> None:
        self._chunks = chunks_do_pcm(pcm16)
        self._silencio = max(1, round(silencio_depois_s / DURACAO_DO_CHUNK_S))
        self._ritmo_real = ritmo_real
        self._lidos = 0
        self.descricao = descricao
        self.dentro_do_audio = False

    def abrir(self) -> None:
        self._lidos = 0

    def ler(self) -> bytes | None:
        if self._ritmo_real:
            time.sleep(DURACAO_DO_CHUNK_S)
        indice = self._lidos
        self._lidos += 1
        if indice < len(self._chunks):
            self.dentro_do_audio = True
            return self._chunks[indice]
        self.dentro_do_audio = False
        if indice < len(self._chunks) + self._silencio:
            return b"\x00" * BYTES_POR_CHUNK
        return None

    def fechar(self) -> None:
        pass


class TeclaDoFicheiro:
    """Tecla premida enquanto a `FonteDeFicheiro` entrega o ficheiro."""

    def __init__(self, fonte: FonteDeFicheiro) -> None:
        self._fonte = fonte

    def premida(self) -> bool:
        return self._fonte.dentro_do_audio


#: Virtual-key codes do Alt direito (AltGr) e do Ctrl esquerdo que o Windows
#: acende com ele nos teclados com AltGr.
CODIGO_ALT_DIREITO = 0xA5
CODIGO_CTRL_ESQUERDO = 0xA2


class TeclaWindows:
    """Uma tecla global lida por `GetAsyncKeyState` (user32, via ctypes)."""

    def __init__(self, codigo: int) -> None:
        if not 0 < int(codigo) < 256:
            raise ValueError(f"virtual-key code fora do intervalo: {codigo}")
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (ImportError, AttributeError, OSError) as erro:
            raise OSError(f"a tecla de falar precisa da API do Windows (user32): {erro}") from erro
        self._ler = user32.GetAsyncKeyState
        self._ler.argtypes = [ctypes.c_int]
        self._ler.restype = ctypes.c_short
        self.codigo = int(codigo)
        # Teclas do teclado (sem os botoes do rato, 0x01-0x06) que fazem de
        # uma pressao um atalho. Os modificadores genericos (Shift, Ctrl, Alt
        # sem lado) ficam de fora: acendem-se com a propria tecla de falar.
        # Em teclados com AltGr (o portugues, por exemplo) o Alt direito faz o
        # Windows dar tambem o Ctrl esquerdo como premido; esse Ctrl sintetico
        # tambem nao conta como atalho.
        ignoradas = {self.codigo, 0x10, 0x11, 0x12}
        if self.codigo == CODIGO_ALT_DIREITO:
            ignoradas.add(CODIGO_CTRL_ESQUERDO)
        self._outras = tuple(c for c in range(0x08, 0xFF) if c not in ignoradas)

    def premida(self) -> bool:
        return bool(self._ler(self.codigo) & 0x8000)

    def outra_tecla_premida(self) -> bool:
        return any(self._ler(c) & 0x8000 for c in self._outras)


class DetetorOpenWakeWord:
    """O modelo openWakeWord de models/ (por omissao `hey_jarvis`)."""

    def __init__(self, modelo: Path = MODELO_ATIVACAO) -> None:
        self.nome = Path(modelo).stem
        for ficheiro in (modelo, MODELO_MELSPEC, MODELO_EMBEDDING):
            if not Path(ficheiro).is_file():
                raise FileNotFoundError(
                    f"modelo do openWakeWord em falta: '{ficheiro}' (ver docs/MODELOS.md)"
                )
        import numpy as np
        from openwakeword.model import Model

        self._np = np
        self.modelo = Model(
            wakeword_models=[str(modelo)],
            inference_framework="onnx",
            melspec_model_path=str(MODELO_MELSPEC),
            embedding_model_path=str(MODELO_EMBEDDING),
        )

    def processar(self, chunk: bytes) -> float:
        previsao = self.modelo.predict(self._np.frombuffer(chunk, dtype="<i2"))
        return float(max(previsao.values())) if previsao else 0.0

    def reiniciar(self) -> None:
        self.modelo.reset()


class VadWebRtc:
    """webrtcvad sobre chunks de 30 ms."""

    def __init__(self, agressividade: int = AGRESSIVIDADE_DO_VAD) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(agressividade)

    def e_fala(self, chunk: bytes) -> bool:
        return bool(self._vad.is_speech(chunk, TAXA))


def tocar_bip(tipo: str) -> None:
    """Um bip curto numa thread a parte (so chamado com --com-som)."""
    import winsound

    frequencia = 880 if tipo == "inicio" else 587
    threading.Thread(target=winsound.Beep, args=(frequencia, 60), name="ouvido-bip", daemon=True).start()


# --- O ouvido -----------------------------------------------------------------

#: O que `ao_evento` recebe: o inicio e o fim da escuta (os sinais), uma frase
#: captada a caminho do texto, uma escuta descartada e uma transcricao falhada.
EVENTO_INICIO = "inicio"
EVENTO_FIM = "fim"
EVENTO_CAPTADA = "captada"
EVENTO_DESCARTADA = "descartada"
EVENTO_ERRO = "erro"
EVENTOS_DO_OUVIDO = (EVENTO_INICIO, EVENTO_FIM, EVENTO_CAPTADA, EVENTO_DESCARTADA, EVENTO_ERRO)


class Ouvido:
    """Maquina de estados da escuta + threads de captura e de transcricao.

    `processar(chunk, premida)` e `transcrever_pendentes()` sao a mesma logica
    que as threads correm, expostas para testes deterministas.
    """

    def __init__(
        self,
        fonte: FonteDeAudio,
        motor: MotorBase,
        ao_ouvir: Callable[[Frase], None],
        *,
        tecla: Tecla | None = None,
        detetor: DetetorDeAtivacao | None = None,
        vad: Vad | None = None,
        lingua: str | None = None,
        limiar_de_ativacao: float = LIMIAR_DE_ATIVACAO,
        palavras_de_ativacao: Iterable[str] | None = None,
        escrever: Callable[[str], object] = print,
        com_som: bool = False,
        tocar: Callable[[str], None] = tocar_bip,
        relogio: Callable[[], float] = time.perf_counter,
        nome_da_tecla: str = "tecla de falar",
        ao_ativar_sem_fala: Callable[[Frase], None] | None = None,
        ao_evento: Callable[[str], object] | None = None,
        ao_chunk: Callable[[bytes], object] | None = None,
    ) -> None:
        if tecla is None and detetor is None:
            raise ValueError("o ouvido precisa de pelo menos um gatilho (tecla ou palavra de ativacao)")
        if detetor is not None and vad is None:
            raise ValueError("as maos-livres precisam de um VAD para saber quando a fala acaba")
        self.fonte = fonte
        self.motor = motor
        self.ao_ouvir = ao_ouvir
        #: Recebe a palavra de ativacao seguida de silencio como uma Frase de
        #: texto vazio (com o score). Sem ele, essa ativacao e descartada.
        self.ao_ativar_sem_fala = ao_ativar_sem_fala
        #: Recebe os momentos da escuta (`EVENTOS_DO_OUVIDO`) e cada chunk lido,
        #: para a bolinha. Tem de ser rapido; uma falha dele nunca para a escuta.
        self.ao_evento = ao_evento
        self.ao_chunk = ao_chunk
        self.tecla = tecla
        self.detetor = detetor
        self.vad = vad
        self.lingua = lingua
        self.limiar_de_ativacao = limiar_de_ativacao
        if palavras_de_ativacao is None:
            palavras_de_ativacao = (
                [PALAVRAS_DE_ATIVACAO[lingua]] if lingua in PALAVRAS_DE_ATIVACAO else PALAVRAS_DE_ATIVACAO.values()
            )
        self.palavras_de_ativacao = tuple(palavras_de_ativacao)
        self.escrever = escrever
        self.com_som = com_som
        self.tocar = tocar
        self.relogio = relogio
        self.nome_da_tecla = nome_da_tecla
        #: Do instante da transicao (tecla, ativacao, VAD) ao sinal dado.
        self.latencias_do_sinal_ms: list[float] = []
        self.descartadas = 0
        self._estado = "repouso"
        self._buffer: list[bytes] = []
        self._inicio = 0.0
        self._score: float | None = None
        self._voz_seguida = 0
        self._silencio_s = 0.0
        self._atalho = False
        self._gatilho = GATILHO_ATIVACAO
        self._espera_pela_fala_s = ESPERA_PELA_FALA_S
        self._surdez_s = SURDEZ_APOS_ATIVACAO_S
        #: Audio desde a ativacao ou a abertura da janela, com ou sem buffer.
        self._aguardado_s = 0.0
        #: Inicio de uma janela cujo audio e deitado fora (ver GUARDA_APOS_A_VOZ_S).
        self._guarda_s = 0.0
        #: Prazo (no relogio) de uma janela de escuta pedida e ainda por abrir,
        #: e para que e (`ESCUTA_RECAP` ou `ESCUTA_CONVERSA`).
        self._janela_ate: float | None = None
        self._janela_para = ESCUTA_CONVERSA
        #: Para que e a janela aberta agora (a espera de fala ou ja com ela).
        self._escuta = ESCUTA_CONVERSA
        # Pedir, fechar e abrir a janela vem de threads diferentes.
        self._trinco_da_janela = threading.Lock()
        self._a_transcrever = False
        self._fechar_janela = False
        self._fila: queue.Queue[_FraseCaptada | None] = queue.Queue(maxsize=FRASES_EM_ESPERA)
        self._parar = threading.Event()
        self._fios: list[threading.Thread] = []

    # -- arranque

    def preparar(self) -> float:
        """Carrega o motor e aquece-o com silencio. Devolve os ms que levou."""
        antes = time.perf_counter()
        self.motor.carregar()
        self.motor.transcrever(b"\x00\x00" * int(TAXA * AQUECIMENTO_S), lingua=self.lingua)
        return (time.perf_counter() - antes) * 1000

    # -- maquina de estados (thread de captura)

    @property
    def estado(self) -> str:
        return self._estado

    def _avisar(self, evento: str) -> None:
        if self.ao_evento is None:
            return
        try:
            self.ao_evento(evento)
        except Exception:  # noqa: BLE001 - a bolinha e um extra, a escuta segue
            pass

    def _sinal(self, tipo: str, detalhe: str, instante: float) -> None:
        seta = ">>> A OUVIR" if tipo == "inicio" else "<<< FIM DA ESCUTA"
        self.escrever(f"ouvido | {seta} | {detalhe}")
        self._avisar(tipo)
        if self.com_som:
            try:
                self.tocar(tipo)
            except Exception:  # noqa: BLE001 - um bip falhado nunca para a escuta
                pass
        self.latencias_do_sinal_ms.append((self.relogio() - instante) * 1000)

    @property
    def ocupado(self) -> bool:
        """Alguem esta a falar ou ha uma frase a caminho do texto."""
        return (
            self._estado != "repouso"
            or self._janela_ate is not None
            or self._a_transcrever
            or not self._fila.empty()
        )

    @property
    def a_ouvir_alguem(self) -> bool:
        """Ha uma frase a ser dita ou a caminho do texto.

        Ao contrario de `ocupado`, uma janela sem palavra de ativacao ainda a
        espera de fala nao conta: ali ninguem comecou a falar.
        """
        a_espera_de_fala = self._estado == "ativado" and self._gatilho == GATILHO_JANELA
        return (
            (self._estado != "repouso" and not a_espera_de_fala)
            or self._a_transcrever
            or not self._fila.empty()
        )

    @property
    def escuta_aberta(self) -> str | None:
        """Para que e a janela sem palavra de ativacao pedida ou a espera de fala, ou None."""
        with self._trinco_da_janela:
            if self._janela_ate is not None:
                return self._janela_para
            if self._estado == "ativado" and self._gatilho == GATILHO_JANELA and not self._fechar_janela:
                return self._escuta
            return None

    def abrir_escuta(self, limite_s: float, *, para: str = ESCUTA_CONVERSA) -> bool:
        """Pede uma escuta sem palavra de ativacao, aberta no proximo chunk em repouso.

        Devolve False sem VAD (sem VAD nao ha como saber o fim da fala).
        """
        if self.vad is None or limite_s <= 0:
            return False
        with self._trinco_da_janela:
            self._janela_para = para
            self._janela_ate = self.relogio() + limite_s
        return True

    def fechar_escuta(self) -> None:
        """Cancela uma janela pedida ou aberta que ainda nao tem fala."""
        with self._trinco_da_janela:
            self._janela_ate = None
            if self._estado == "ativado" and self._gatilho == GATILHO_JANELA:
                self._fechar_janela = True

    def _descartar(self, motivo: str) -> None:
        self.descartadas += 1
        self.escrever(f"ouvido | descartado: {motivo} | nada transcrito, nada enviado")
        self._avisar(EVENTO_DESCARTADA)

    def _voltar_ao_repouso(self) -> None:
        self._estado = "repouso"
        self._buffer = []
        self._score = None
        self._voz_seguida = 0
        self._silencio_s = 0.0
        self._atalho = False
        self._gatilho = GATILHO_ATIVACAO
        self._espera_pela_fala_s = ESPERA_PELA_FALA_S
        self._surdez_s = SURDEZ_APOS_ATIVACAO_S
        self._aguardado_s = 0.0
        self._guarda_s = 0.0
        with self._trinco_da_janela:
            self._fechar_janela = False
        self._escuta = ESCUTA_CONVERSA
        if self.detetor is not None:
            self.detetor.reiniciar()

    def _duracao_do_buffer(self) -> float:
        return len(self._buffer) * DURACAO_DO_CHUNK_S

    def _houve_atalho(self) -> bool:
        """Outra tecla premida durante a tecla de falar: e um atalho, nao fala."""
        outra = getattr(self.tecla, "outra_tecla_premida", None)
        if outra is not None and not self._atalho:
            self._atalho = bool(outra())
        return self._atalho

    def _enfileirar(self, gatilho: str, fim: float) -> None:
        captada = _FraseCaptada(b"".join(self._buffer), gatilho, self._inicio, fim, self._score)
        try:
            self._fila.put_nowait(captada)
        except queue.Full:
            self._descartar(f"{FRASES_EM_ESPERA} frases ainda a espera de transcricao")
            return
        self._avisar(EVENTO_CAPTADA)

    def _enfileirar_so_ativacao(self, fim: float) -> None:
        captada = _FraseCaptada(b"", GATILHO_ATIVACAO, self._inicio, fim, self._score, so_ativacao=True)
        try:
            self._fila.put_nowait(captada)
        except queue.Full:
            self._descartar(f"{FRASES_EM_ESPERA} frases ainda a espera de transcricao")

    def processar(self, chunk: bytes, premida: bool) -> None:
        """Um chunk de audio e o estado da tecla amostrado logo depois de o ler."""
        agora = self.relogio()
        premida = premida and self.tecla is not None

        if self._estado == "espera_soltar":
            if not premida:
                self._voltar_ao_repouso()
            return

        if premida and self._estado != "tecla":
            # Em repouso nao ha nada a limpar: reiniciar o detetor aqui (dezenas
            # de ms) so atrasava o sinal de inicio. Ao soltar ele e reiniciado.
            if self._estado != "repouso":
                # Uma janela sem palavra de ativacao ainda sem fala nao perde nada.
                if not (self._estado == "ativado" and self._gatilho == GATILHO_JANELA):
                    self._descartar("a tecla foi premida a meio de uma escuta por palavra de ativacao")
                self._voltar_ao_repouso()
            self._estado = "tecla"
            self._inicio = agora
            self._buffer = [chunk]
            self._sinal("inicio", f"{self.nome_da_tecla} premida", agora)
            self._houve_atalho()
            return

        if self._estado == "tecla":
            if premida:
                self._buffer.append(chunk)
                self._houve_atalho()
                if self._duracao_do_buffer() >= MAXIMO_DA_FRASE_S:
                    self._sinal("fim", f"frase no maximo de {MAXIMO_DA_FRASE_S:.0f} s; solta a tecla", agora)
                    if self._atalho:
                        self._descartar("atalho de teclado (outra tecla premida com a tecla de falar)")
                    else:
                        self._enfileirar(GATILHO_TECLA, agora)
                    self._buffer = []
                    self._estado = "espera_soltar"
                return
            # Soltou: ESTE chunk ja e audio fora da tecla e nao entra.
            self._sinal("fim", f"{self.nome_da_tecla} solta", agora)
            if self._atalho:
                self._descartar("atalho de teclado (outra tecla premida com a tecla de falar)")
            elif self._duracao_do_buffer() < MINIMO_DA_TECLA_S:
                self._descartar(f"toque de {self._duracao_do_buffer() * 1000:.0f} ms na tecla (acidental)")
            else:
                self._enfileirar(GATILHO_TECLA, agora)
            self._voltar_ao_repouso()
            return

        if self._estado == "repouso" and self._janela_ate is not None:
            with self._trinco_da_janela:
                janela_ate, self._janela_ate = self._janela_ate, None
                aberta = janela_ate is not None and janela_ate > agora and self.vad is not None
                if aberta:
                    self._estado = "ativado"
                    self._gatilho = GATILHO_JANELA
                    self._escuta = self._janela_para
                    self._espera_pela_fala_s = janela_ate - agora
                    self._surdez_s = 0.0
                    self._guarda_s = GUARDA_APOS_A_VOZ_S if self._escuta == ESCUTA_RECAP else 0.0
                    self._aguardado_s = 0.0
                    self._inicio = agora
                    self._buffer = []
                    self._score = None
            if aberta:
                self._sinal(
                    "inicio",
                    f"{_NOMES_DAS_ESCUTAS[self._escuta]} ({self._espera_pela_fala_s:.0f} s, sem palavra de ativacao)",
                    agora,
                )
                return

        if self.detetor is None and self._estado == "repouso":
            return  # sem maos-livres, audio fora da tecla nem e olhado

        if self._estado == "repouso":
            score = self.detetor.processar(chunk)
            if score >= self.limiar_de_ativacao:
                self._estado = "ativado"
                self._aguardado_s = 0.0
                self._inicio = agora
                self._buffer = []
                self._score = score
                self._sinal("inicio", f"palavra de ativacao (score {score:.2f})", agora)
            return

        # Escuta aberta pela palavra de ativacao ou pela janela: o VAD decide o fim.
        if self._estado == "ativado":
            janela = self._gatilho == GATILHO_JANELA
            nome = _NOMES_DAS_ESCUTAS[self._escuta]
            if janela and self._fechar_janela:
                self._sinal("fim", f"{nome} fechada", agora)
                self._voltar_ao_repouso()
                return
            self._aguardado_s += DURACAO_DO_CHUNK_S
            if self._aguardado_s <= self._guarda_s:
                fala = False  # ainda pode ser a voz do jarvis: fora do VAD e da frase
            else:
                self._buffer.append(chunk)
                if janela and len(self._buffer) > CHUNKS_ANTES_DA_FALA:
                    del self._buffer[0]
                fala = self.vad.e_fala(chunk)
            if fala and self._aguardado_s > self._surdez_s:
                self._voz_seguida += 1
            else:
                self._voz_seguida = 0
            if self._voz_seguida >= CHUNKS_PARA_COMECAR_A_FALA:
                self._estado = "a_falar"
                self._silencio_s = 0.0
                if janela:
                    # A frase comeca no primeiro chunk com voz, nao na abertura da janela.
                    self._inicio = agora - self._voz_seguida * DURACAO_DO_CHUNK_S
            elif self._aguardado_s >= self._espera_pela_fala_s:
                if janela:
                    self._sinal("fim", f"{nome} sem fala em {self._espera_pela_fala_s:.0f} s", agora)
                    self._descartar(f"{nome} sem resposta")
                else:
                    self._sinal("fim", f"sem fala {ESPERA_PELA_FALA_S:.0f} s depois da ativacao", agora)
                    if self.ao_ativar_sem_fala is None:
                        self._descartar("palavra de ativacao seguida de silencio")
                    else:
                        self._enfileirar_so_ativacao(agora)
                self._voltar_ao_repouso()
            return
        self._buffer.append(chunk)
        decorrido = self._duracao_do_buffer()
        fala = self.vad.e_fala(chunk)
        self._silencio_s = 0.0 if fala else self._silencio_s + DURACAO_DO_CHUNK_S
        if self._silencio_s >= SILENCIO_FINAL_S or decorrido >= MAXIMO_DA_FRASE_S:
            motivo = "fim da fala (VAD)" if self._silencio_s >= SILENCIO_FINAL_S else "frase no maximo"
            self._sinal("fim", motivo, agora)
            self._enfileirar(self._gatilho, agora)
            self._voltar_ao_repouso()

    # -- transcricao (thread de transcricao)

    def _transcrever(self, captada: _FraseCaptada) -> Frase | None:
        try:
            resultado = self.motor.transcrever(captada.pcm16, lingua=self.lingua)
        except Exception as erro:  # noqa: BLE001 - uma frase falhada nunca para o ouvido
            self.escrever(f"ouvido | transcricao falhou ({self.motor.nome}): {erro!r}")
            self._avisar(EVENTO_ERRO)
            return None
        pronto = self.relogio()
        if not resultado.texto:
            self._descartar("nada transcrito nesta frase")
            return None
        # Com a tecla a palavra so pode vir dita inteira; a cauda sozinha e o
        # resto de uma ativacao maos-livres que abriu a meio da palavra.
        texto, retirada = retirar_palavra_de_ativacao(
            resultado.texto, self.palavras_de_ativacao, aceitar_cauda=captada.gatilho == GATILHO_ATIVACAO
        )
        if retirada is not None:
            self.escrever(f"ouvido | palavra de ativacao retirada do inicio do texto: {retirada!r}")
        return Frase(
            texto=texto,
            gatilho=captada.gatilho,
            lingua=resultado.lingua,
            motor=resultado.motor,
            duracao_audio_s=duracao_pcm16(captada.pcm16),
            inicio_da_escuta=captada.inicio,
            fim_da_escuta=captada.fim,
            texto_pronto=pronto,
            latencia_stt_ms=resultado.latencia_ms,
            score_ativacao=captada.score,
            palavra_retirada=retirada,
        )

    def _entregar_so_ativacao(self, captada: _FraseCaptada) -> None:
        """A palavra de ativacao sem fala depois: nada a transcrever, so o score."""
        self.escrever(
            f"ouvido | palavra de ativacao seguida de silencio (score {captada.score or 0:.2f}) "
            "| entregue como ativacao sem fala, nada transcrito"
        )
        frase = Frase(
            texto="",
            gatilho=GATILHO_ATIVACAO,
            lingua=self.lingua,
            motor=self.motor.nome,
            duracao_audio_s=0.0,
            inicio_da_escuta=captada.inicio,
            fim_da_escuta=captada.fim,
            texto_pronto=self.relogio(),
            latencia_stt_ms=0.0,
            score_ativacao=captada.score,
        )
        try:
            self.ao_ativar_sem_fala(frase)
        except Exception as erro:  # noqa: BLE001 - o callback nunca derruba o ouvido
            self.escrever(f"ouvido | o tratamento da ativacao sem fala falhou: {erro!r}")

    def _entregar(self, captada: _FraseCaptada) -> None:
        if captada.so_ativacao:
            if self.ao_ativar_sem_fala is not None:
                self._entregar_so_ativacao(captada)
            return
        self._a_transcrever = True
        try:
            frase = self._transcrever(captada)
            if frase is None:
                return
            try:
                self.ao_ouvir(frase)
            except Exception as erro:  # noqa: BLE001 - o callback nunca derruba o ouvido
                self.escrever(f"ouvido | o tratamento da frase falhou: {erro!r}")
        finally:
            self._a_transcrever = False

    def transcrever_pendentes(self) -> int:
        """Transcreve e entrega o que esta na fila, sem esperar. Devolve quantas."""
        feitas = 0
        while True:
            try:
                captada = self._fila.get_nowait()
            except queue.Empty:
                return feitas
            if captada is not None:
                self._entregar(captada)
                feitas += 1

    # -- threads

    def _captar(self) -> None:
        try:
            while not self._parar.is_set():
                chunk = self.fonte.ler()
                if chunk is None:
                    break
                premida = self.tecla.premida() if self.tecla is not None else False
                self.processar(chunk, premida)
                if self.ao_chunk is not None:
                    try:
                        self.ao_chunk(chunk)
                    except Exception:  # noqa: BLE001 - o nivel do microfone e um extra
                        pass
        except Exception as erro:  # noqa: BLE001
            self.escrever(f"ouvido | ERRO na captura: {erro!r}")
        finally:
            self.fonte.fechar()
            self._fila.put(None)  # acorda a transcricao para acabar o que falta

    def _transcrever_em_ciclo(self) -> None:
        while True:
            captada = self._fila.get()
            if captada is None:
                return
            if self._parar.is_set():
                continue  # a parar: o que falta na fila nao se entrega
            self._entregar(captada)

    def iniciar(self) -> None:
        """Abre a fonte AQUI (um microfone que nao abre levanta para quem arranca) e lanca as threads."""
        if self._fios:
            raise RuntimeError("o ouvido ja esta a correr")
        self.fonte.abrir()
        self._fios = [
            threading.Thread(target=self._captar, name="ouvido-captura", daemon=True),
            threading.Thread(target=self._transcrever_em_ciclo, name="ouvido-transcricao", daemon=True),
        ]
        for fio in self._fios:
            fio.start()

    def a_correr(self) -> bool:
        return any(fio.is_alive() for fio in self._fios)

    def esperar(self, limite_s: float | None = None) -> bool:
        """Espera pelas threads. Devolve True se acabaram dentro do limite."""
        fim = None if limite_s is None else time.perf_counter() + limite_s
        for fio in self._fios:
            fio.join(None if fim is None else max(0.0, fim - time.perf_counter()))
        return not self.a_correr()

    def parar(self) -> None:
        """Para a captura; o que ainda nao foi entregue ja nao o sera."""
        self._parar.set()


# --- Medicao em modo ficheiro -------------------------------------------------


def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo metodo do posto mais proximo."""
    if not valores:
        raise ValueError("sem valores")
    ordenados = sorted(valores)
    posto = max(1, math.ceil(p / 100 * len(ordenados)))
    return ordenados[posto - 1]


def wavs_a_medir(entradas: Iterable[str]) -> list[Path]:
    caminhos: list[Path] = []
    for entrada in entradas:
        caminho = Path(entrada)
        if caminho.is_dir():
            caminhos.extend(sorted(caminho.rglob("*.wav")))
        elif caminho.is_file():
            caminhos.append(caminho)
        else:
            raise FileNotFoundError(f"nao existe: '{entrada}'")
    return caminhos


def medir_ficheiro(
    motor: MotorBase, pcm16: bytes, *, lingua: str | None = None, escrever: Callable[[str], object] = lambda _t: None
) -> tuple[Frase | None, list[float]]:
    """Um WAV pelo caminho da tecla de falar. Devolve a frase e as latencias do sinal."""
    fonte = FonteDeFicheiro(pcm16)
    frases: list[Frase] = []
    ouvido = Ouvido(fonte, motor, frases.append, tecla=TeclaDoFicheiro(fonte), lingua=lingua, escrever=escrever)
    ouvido.iniciar()
    ouvido.esperar(60.0)
    return (frases[0] if frases else None), ouvido.latencias_do_sinal_ms


def medir(
    motor: MotorBase,
    caminhos: list[Path],
    *,
    repeticoes: int = 3,
    lingua: str | None = None,
    escrever: Callable[[str], object] = print,
) -> dict:
    latencias: list[float] = []
    sinais: list[float] = []
    medidos = saltados = 0
    for caminho in caminhos:
        pcm = pcm_do_wav(caminho)
        duracao = duracao_pcm16(pcm)
        if duracao > MAXIMO_DO_WAV_MEDIDO_S or duracao < MINIMO_DA_TECLA_S:
            saltados += 1
            continue
        medidos += 1
        for _ in range(repeticoes):
            frase, sinal = medir_ficheiro(motor, pcm, lingua=lingua)
            sinais.extend(sinal)
            if frase is None:
                escrever(f"  {caminho.name:<40} {duracao:5.2f} s  (nada transcrito)")
                continue
            latencias.append(frase.ms_do_fim_ao_texto)
            escrever(f"  {caminho.name:<40} {duracao:5.2f} s  soltar->texto {frase.ms_do_fim_ao_texto:6.0f} ms")
    return {
        "wavs": medidos,
        "saltados": saltados,
        "latencias_ms": latencias,
        "sinais_ms": sinais,
    }


# --- Autoteste (microfone, tecla e motor falsos; sem som) -------------------


class _MotorFalso(MotorBase):
    nome = "motor-falso"

    def __init__(self, falhar: bool = False) -> None:
        super().__init__("cpu")
        self.recebidos: list[bytes] = []
        self.falhar = falhar

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        if not any(pcm16):
            return "", lingua, False  # silencio de aquecimento
        self.recebidos.append(pcm16)
        if self.falhar:
            raise RuntimeError("falha de proposito")
        return f"frase de {len(pcm16) // BYTES_POR_CHUNK} chunks", lingua, False


class _DetetorFalso:
    def __init__(self, marca: int) -> None:
        self.marca = marca
        self.reinicios = 0

    def processar(self, chunk: bytes) -> float:
        return 0.9 if chunk[0] == self.marca else 0.0

    def reiniciar(self) -> None:
        self.reinicios += 1


class _VadFalso:
    def e_fala(self, chunk: bytes) -> bool:
        return chunk[0] != 0


def _chunk(valor: int) -> bytes:
    return bytes([valor]) * BYTES_POR_CHUNK


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, condicao: bool, detalhe: str = "") -> None:
        if condicao:
            print(f"ok   {nome}")
        else:
            falhas.append(f"{nome} {detalhe}".strip())

    linhas: list[str] = []
    bips: list[str] = []
    frases: list[Frase] = []

    # 1. tecla: so o audio com a tecla premida chega ao motor.
    motor = _MotorFalso()
    ouvido = Ouvido(
        FonteDeFicheiro(b""), motor, frases.append, tecla=TeclaDoFicheiro(FonteDeFicheiro(b"")),
        escrever=linhas.append, tocar=bips.append,
    )
    ouvido.preparar()
    for _ in range(5):
        ouvido.processar(_chunk(0x11), False)  # antes da tecla
    for _ in range(20):
        ouvido.processar(_chunk(0x22), True)  # 600 ms com a tecla
    ouvido.processar(_chunk(0x33), False)  # o chunk do soltar
    ouvido.transcrever_pendentes()
    verificar("tecla: uma frase entregue", len(frases) == 1, f"({len(frases)})")
    verificar("tecla: so audio premido chega ao motor", motor.recebidos == [_chunk(0x22) * 20])
    verificar("tecla: gatilho", bool(frases) and frases[0].gatilho == GATILHO_TECLA)
    verificar("sinais de inicio e fim na consola", sum("A OUVIR" in l or "FIM DA ESCUTA" in l for l in linhas) == 2)
    verificar("sem --com-som nao ha bip", bips == [])
    verificar(
        f"sinal em <= {META_SINAL_MS:.0f} ms",
        max(ouvido.latencias_do_sinal_ms) <= META_SINAL_MS,
        str(ouvido.latencias_do_sinal_ms),
    )

    # 2. toque curto descartado.
    motor.recebidos.clear()
    for _ in range(3):
        ouvido.processar(_chunk(0x22), True)
    ouvido.processar(_chunk(0x00), False)
    ouvido.transcrever_pendentes()
    verificar("toque curto descartado", motor.recebidos == [] and len(frases) == 1)

    # 3. maos-livres: ativacao + VAD; ruido sem ativacao nunca passa.
    frases.clear()
    motor = _MotorFalso()
    detetor = _DetetorFalso(marca=0x7A)
    ouvido = Ouvido(
        FonteDeFicheiro(b""), motor, frases.append, detetor=detetor, vad=_VadFalso(), escrever=linhas.append
    )
    for _ in range(30):
        ouvido.processar(_chunk(0x44), False)  # fala sem ativacao
    ouvido.processar(_chunk(0x7A), False)  # palavra de ativacao
    for _ in range(20):
        ouvido.processar(_chunk(0x55), False)  # a frase
    for _ in range(round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S)):
        ouvido.processar(_chunk(0x00), False)
    ouvido.transcrever_pendentes()
    verificar("maos-livres: uma frase pelo VAD", len(frases) == 1 and frases[0].gatilho == GATILHO_ATIVACAO)
    verificar(
        "maos-livres: fala antes da ativacao nunca chega ao motor",
        len(motor.recebidos) == 1 and bytes([0x44]) not in motor.recebidos[0],
    )
    verificar("maos-livres: o detetor reinicia depois da frase", detetor.reinicios >= 1)

    # 4. ativacao seguida de silencio: descartada.
    motor.recebidos.clear()
    ouvido.processar(_chunk(0x7A), False)
    for _ in range(round(ESPERA_PELA_FALA_S / DURACAO_DO_CHUNK_S) + 1):
        ouvido.processar(_chunk(0x00), False)
    ouvido.transcrever_pendentes()
    verificar("ativacao sem fala descartada", motor.recebidos == [] and ouvido.estado == "repouso")

    # 5. caminho com threads em modo ficheiro, motor que falha a meio.
    motor = _MotorFalso(falhar=True)
    fonte = FonteDeFicheiro(_chunk(0x22) * 10)
    frases.clear()
    ouvido = Ouvido(fonte, motor, frases.append, tecla=TeclaDoFicheiro(fonte), escrever=linhas.append)
    ouvido.iniciar()
    verificar("threads acabam com a fonte", ouvido.esperar(10.0))
    verificar("motor que falha nao entrega nem derruba", frases == [] and len(motor.recebidos) == 1)

    # 6. medicao em modo ficheiro com o motor falso.
    motor = _MotorFalso()
    frase, sinais = medir_ficheiro(motor, _chunk(0x22) * 40)
    verificar("medicao: frase e latencia", frase is not None and frase.ms_do_fim_ao_texto >= 0)
    verificar("medicao: dois sinais", len(sinais) == 2)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do jarvis.ouvido completo (microfone, tecla e motor falsos, sem som).")
    return 0


# --- CLI ----------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jarvis.ouvido", description=__doc__.splitlines()[0])
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--autoteste", action="store_true", help="microfone, tecla e motor falsos; sem som")
    modo.add_argument("--ouvir", action="store_true", help="ouve o microfone e imprime cada frase")
    modo.add_argument("--medir", nargs="+", metavar="WAV_OU_PASTA", help="mede soltar->texto em modo ficheiro")
    parser.add_argument("--config", default=None, metavar="FICHEIRO", help="config.toml (por omissao: o da raiz)")
    parser.add_argument("--motor", default=None, help="troca o motor do config ([ouvido].motor)")
    parser.add_argument("--device", default=None, choices=["cpu", "cuda"], help="troca o device do config")
    parser.add_argument("--repeticoes", type=int, default=3, help="vezes que cada WAV e medido (por omissao 3)")
    parser.add_argument("--verificar", action="store_true", help="sai com erro se as metas falharem")
    parser.add_argument("--sem-ativacao", action="store_true", help="--ouvir so com a tecla de falar")
    parser.add_argument("--com-som", action="store_true", help="bip curto no inicio e no fim da escuta; sem esta flag nada toca")
    return parser


def _config_do_ouvido(args):
    from jarvis.config import CAMINHO_CONFIG_PADRAO, Config, ConfigError, carregar_config

    try:
        return carregar_config(Path(args.config) if args.config else CAMINHO_CONFIG_PADRAO)
    except ConfigError as erro:
        print(f"aviso: {erro} | a usar os valores por omissao do ouvido")
        return Config(microfone="", projetos=())


def main(argv: list[str] | None = None) -> int:
    from jarvis.consola import forcar_consola_utf8

    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()

    config = _config_do_ouvido(args)
    motor_nome = args.motor or config.ouvido.motor
    device = args.device or config.ouvido.device
    try:
        motor = criar_motor(motor_nome, device)
    except ValueError as erro:
        print(f"ERRO: {erro}")
        return 2

    if args.medir:
        caminhos = wavs_a_medir(args.medir)
        print(f"motor {motor_nome} em {device}: a carregar e aquecer...")
        try:
            antes = time.perf_counter()
            motor.carregar()
            motor.transcrever(b"\x00\x00" * int(TAXA * AQUECIMENTO_S), lingua=config.ouvido.lingua)
        except MotorIndisponivel as erro:
            print(f"ERRO: {erro}")
            return 2
        print(f"pronto em {(time.perf_counter() - antes) * 1000:.0f} ms")
        resultado = medir(motor, caminhos, repeticoes=max(1, args.repeticoes), lingua=config.ouvido.lingua)
        latencias = resultado["latencias_ms"]
        if not latencias:
            print(f"ERRO: nenhum WAV entre {MINIMO_DA_TECLA_S} e {MAXIMO_DO_WAV_MEDIDO_S:.0f} s foi transcrito")
            return 1
        p50, p95 = percentil(latencias, 50), percentil(latencias, 95)
        sinal95 = percentil(resultado["sinais_ms"], 95)
        print()
        print(f"motor {motor.descrever()} | {resultado['wavs']} WAV (<= {MAXIMO_DO_WAV_MEDIDO_S:.0f} s), "
              f"{len(latencias)} medicoes, {resultado['saltados']} saltados por duracao")
        print(f"soltar a tecla -> texto: p50 {p50:.0f} ms (meta <= {META_P50_MS:.0f}) | "
              f"p95 {p95:.0f} ms (meta <= {META_P95_MS:.0f})")
        print(f"sinal de inicio/fim: p95 {sinal95:.1f} ms (meta <= {META_SINAL_MS:.0f})")
        cumpre = p50 <= META_P50_MS and p95 <= META_P95_MS and sinal95 <= META_SINAL_MS
        print("METAS CUMPRIDAS" if cumpre else "METAS FALHADAS")
        return 0 if (cumpre or not args.verificar) else 1

    # --ouvir: microfone real, frases impressas; nada e encaminhado.
    from jarvis.config import TECLAS_DE_FALAR

    detetor = vad = None
    if not args.sem_ativacao:
        try:
            detetor, vad = DetetorOpenWakeWord(modelo_de_ativacao(config.ouvido.lingua)), VadWebRtc()
        except (FileNotFoundError, ImportError) as erro:
            print(f"aviso: maos-livres desligadas ({erro}); fica so a tecla de falar")
    ouvido = Ouvido(
        MicrofonePyAudio(config.microfone),
        motor,
        lambda frase: print(f"frase ({frase.gatilho}, {frase.ms_do_fim_ao_texto:.0f} ms): {frase.texto}"),
        tecla=TeclaWindows(TECLAS_DE_FALAR[config.ouvido.tecla]),
        detetor=detetor,
        vad=vad,
        lingua=config.ouvido.lingua,
        limiar_de_ativacao=config.ouvido.limiar_ativacao,
        com_som=args.com_som,
        nome_da_tecla=config.ouvido.tecla,
    )
    try:
        print(f"motor pronto em {ouvido.preparar():.0f} ms")
    except MotorIndisponivel as erro:
        print(f"ERRO: {erro}")
        return 2
    try:
        ouvido.iniciar()
    except OSError as erro:
        print(f"ERRO: {erro}")
        return 2
    print(f"microfone: {ouvido.fonte.descricao}")
    for tentativa in ouvido.fonte.tentativas:
        print(f"  posto de parte: {tentativa}")
    print(f"a ouvir: segura '{config.ouvido.tecla}' para falar; Ctrl+C para sair")
    try:
        while ouvido.a_correr():
            ouvido.esperar(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        ouvido.parar()
        ouvido.esperar(2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
