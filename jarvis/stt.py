r"""Interface unica de motor de transcricao (STT) do jarvis.

Um motor carrega o modelo UMA vez (`carregar()`, idempotente) e depois
transcreve quantas frases for preciso: recebe bytes PCM de 16 bits, mono, a
16 kHz, e devolve uma `Transcricao` com o texto, a lingua e a latencia da
transcricao (so a inferencia; o carregamento mede-se a parte, em
`latencia_carregamento_ms`).

Adaptadores:

  "whisper-medium"          faster-whisper `medium` (CTranslate2);
  "whisper-large-v3-turbo"  faster-whisper `large-v3-turbo`;
  "parakeet-tdt-0.6b-v3"    NVIDIA Parakeet TDT 0.6B v3 em ONNX, via onnx-asr.

Um motor que nao pode correr nesta maquina (pacote por instalar, modelo por
descarregar, GPU sem memoria) levanta `MotorIndisponivel` com o motivo e o
comando que resolve. Quem mede reporta esse motivo e salta o motor: nunca ha
um resultado inventado nem uma troca silenciosa para outro motor ou device.
Nenhum adaptador descarrega modelos sozinho: os pesos vivem em `models/`
(ignorado pelo Git), e a origem e o sha256 de cada um estao em docs/MODELOS.md.

Uso:

    from jarvis.stt import criar_motor

    motor = criar_motor("whisper-medium", device="cuda")
    motor.carregar()                          # uma vez por processo
    resultado = motor.transcrever(pcm16, lingua="pt")
    resultado.texto, resultado.lingua, resultado.latencia_ms

Autoteste das partes puras (sem GPU, sem modelos, sem som):

    .venv\Scripts\python -m jarvis.stt --autoteste
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from jarvis.audio_util import (
    PASTA_MODELOS_FASTER_WHISPER,
    RAIZ,
    TAXA_AMOSTRAGEM_PADRAO,
    registar_dlls_do_torch,
)

if TYPE_CHECKING:
    from jarvis.adaptacao import Adaptacao

#: A unica taxa de amostragem que a interface aceita. Quem tem audio noutra
#: taxa reamostra antes (`jarvis.audio_util.reamostrar_pcm16`).
TAXA_DO_MOTOR = TAXA_AMOSTRAGEM_PADRAO

#: Linguas que o jarvis pode pedir a um motor. `None` = o motor decide.
LINGUAS_PEDIDAS = ("pt", "en")

PASTA_MODELO_PARAKEET = RAIZ / "models" / "parakeet-tdt-0.6b-v3"
NOME_ONNX_ASR_PARAKEET = "nemo-parakeet-tdt-0.6b-v3"

COMANDO_INSTALAR_ONNX_ASR = r'.venv\Scripts\python -m pip install "onnx-asr[cpu,hub]==0.12.0"'
COMANDO_DESCARREGAR_PARAKEET = (
    r'.venv\Scripts\python -c "import onnx_asr; '
    f"onnx_asr.load_model('{NOME_ONNX_ASR_PARAKEET}', 'models/parakeet-tdt-0.6b-v3')\""
)


class MotorIndisponivel(Exception):
    """O motor nao pode correr aqui. A mensagem diz porque e como resolver."""


@dataclass(frozen=True)
class Transcricao:
    """O que qualquer motor devolve por frase."""

    texto: str
    #: Codigo da lingua ("pt", "en", ...) ou None se o motor nao a sabe.
    lingua: str | None
    #: True quando a lingua veio de uma detecao do motor; False quando foi
    #: fixada por quem pediu (ou quando o motor nao deteta lingua nenhuma).
    lingua_detetada: bool
    #: So a inferencia desta frase, em milissegundos.
    latencia_ms: float
    motor: str
    duracao_audio_s: float


def duracao_pcm16(pcm16: bytes, taxa: int = TAXA_DO_MOTOR) -> float:
    """Segundos de audio em PCM de 16 bits mono."""
    return (len(pcm16) // 2) / taxa


def pcm16_para_float32(pcm16: bytes):
    """PCM de 16 bits -> array numpy float32 em [-1, 1), o formato dos dois motores."""
    import numpy as np

    return np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0


def validar_pcm16(pcm16: object) -> bytes:
    """Entrada externa (microfone, ficheiro): so bytes, com numero par de bytes."""
    if not isinstance(pcm16, (bytes, bytearray, memoryview)):
        raise TypeError(f"o motor recebe bytes PCM16, nao {type(pcm16).__name__}")
    dados = bytes(pcm16)
    if len(dados) % 2:
        raise ValueError("PCM de 16 bits tem de ter um numero par de bytes")
    return dados


class MotorBase:
    """Carrega uma vez, mede cada transcricao, liberta quando pedido.

    As subclasses implementam `_carregar_modelo()` (devolve o modelo, ou
    levanta `MotorIndisponivel`) e `_inferir(modelo, pcm16, lingua)` (devolve
    `(texto, lingua, lingua_detetada)`).
    """

    nome = "base"

    def __init__(self, device: str = "cuda") -> None:
        if device not in ("cuda", "cpu"):
            raise ValueError(f"device '{device}': so 'cuda' ou 'cpu'")
        self.device = device
        self._modelo = None
        self.latencia_carregamento_ms: float | None = None
        #: O device onde o modelo ficou mesmo a correr (pode diferir do pedido
        #: so se o proprio motor o disser; nunca por troca silenciosa daqui).
        self.device_real: str | None = None

    @property
    def carregado(self) -> bool:
        return self._modelo is not None

    def carregar(self) -> None:
        if self._modelo is not None:
            return
        inicio = time.perf_counter()
        try:
            modelo = self._carregar_modelo()
        except MotorIndisponivel:
            raise
        except Exception as erro:  # memoria, DLL, ficheiro corrompido
            raise MotorIndisponivel(f"{self.nome}: falhou a carregar em {self.device} ({erro!r})") from erro
        self.latencia_carregamento_ms = (time.perf_counter() - inicio) * 1000
        self._modelo = modelo

    def transcrever(self, pcm16: bytes, lingua: str | None = None) -> Transcricao:
        if lingua is not None and lingua not in LINGUAS_PEDIDAS:
            raise ValueError(f"lingua '{lingua}': so {LINGUAS_PEDIDAS} ou None")
        dados = validar_pcm16(pcm16)
        if self._modelo is None:
            self.carregar()
        inicio = time.perf_counter()
        if dados:
            texto, lingua_obtida, detetada = self._inferir(self._modelo, dados, lingua)
        else:
            texto, lingua_obtida, detetada = "", lingua, False
        latencia_ms = (time.perf_counter() - inicio) * 1000
        return Transcricao(
            texto=" ".join(str(texto).split()),
            lingua=lingua_obtida,
            lingua_detetada=detetada,
            latencia_ms=latencia_ms,
            motor=self.nome,
            duracao_audio_s=duracao_pcm16(dados),
        )

    def libertar(self) -> None:
        """Larga o modelo (e a VRAM que ele ocupa) para o motor seguinte caber."""
        self._modelo = None
        import gc

        gc.collect()

    def descrever(self) -> str:
        return f"{self.nome} ({self.device_real or self.device})"

    def _carregar_modelo(self):  # pragma: no cover - abstrato
        raise NotImplementedError

    def _inferir(self, modelo, pcm16: bytes, lingua: str | None):  # pragma: no cover - abstrato
        raise NotImplementedError


class MotorFasterWhisper(MotorBase):
    """faster-whisper com os pesos ja em models/faster-whisper (nunca descarrega)."""

    MODELOS = ("medium", "large-v3-turbo")

    def __init__(self, modelo: str, device: str = "cuda") -> None:
        if modelo not in self.MODELOS:
            raise ValueError(f"modelo faster-whisper '{modelo}': so {self.MODELOS}")
        super().__init__(device)
        self.modelo = modelo
        self.nome = f"whisper-{modelo}"

    def _carregar_modelo(self):
        if self.device == "cuda":
            try:
                registar_dlls_do_torch()  # antes do import do faster_whisper
            except ImportError:
                pass
        try:
            import faster_whisper
        except ImportError as erro:
            raise MotorIndisponivel(
                f"{self.nome}: faster-whisper nao esta instalado no .venv ({erro}); "
                r"instalar com .venv\Scripts\python -m pip install -r requirements.txt"
            ) from erro
        try:
            modelo = faster_whisper.WhisperModel(
                self.modelo,
                device=self.device,
                compute_type="float16" if self.device == "cuda" else "int8",
                download_root=str(PASTA_MODELOS_FASTER_WHISPER),
                local_files_only=True,
            )
        except Exception as erro:
            texto = repr(erro)
            if "local" in texto.lower() or "snapshot" in texto.lower() or "not found" in texto.lower():
                raise MotorIndisponivel(
                    f"{self.nome}: pesos em falta em models/faster-whisper ({erro}); ver o "
                    "comando de download em docs/MODELOS.md"
                ) from erro
            raise
        interno = getattr(modelo, "model", None)
        self.device_real = getattr(interno, "device", self.device)
        return modelo

    def _inferir(self, modelo, pcm16: bytes, lingua: str | None):
        segmentos, info = modelo.transcribe(
            pcm16_para_float32(pcm16),
            language=lingua,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        texto = " ".join(segmento.text.strip() for segmento in segmentos)
        return texto, (lingua or getattr(info, "language", None)), lingua is None


def opcoes_de_sessao_onnx(onnxruntime):
    """SessionOptions do onnxruntime para o Parakeet, com as threads sem espera ativa.

    Por omissao cada thread do onnxruntime fica a girar (spin) a espera de
    trabalho. Com um CPU hibrido e outros processos a correr, essas threads
    disputam os nucleos entre si e com o descodificador, que faz dezenas de
    inferencias pequenas por frase: medido nesta maquina, a mesma frase de
    1 s passava de ~0,3 s para ~2 s. Sem spin a latencia fica estavel.
    """
    opcoes = onnxruntime.SessionOptions()
    opcoes.add_session_config_entry("session.intra_op.allow_spinning", "0")
    opcoes.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return opcoes


class MotorParakeet(MotorBase):
    """Parakeet TDT 0.6B v3 (25 linguas europeias, sem pedir lingua) via onnx-asr.

    O modelo nao aceita nem devolve a lingua: `Transcricao.lingua` e a lingua
    pedida (ou None) e `lingua_detetada` e sempre False. Com uma `Adaptacao`
    (`jarvis.adaptacao`), o reforco de frases liga-se ao modelo quando ele
    carrega e o lexico corrige o texto de cada frase.
    """

    nome = "parakeet-tdt-0.6b-v3"

    def __init__(
        self, device: str = "cuda", pasta: Path | None = None, adaptacao: "Adaptacao | None" = None
    ) -> None:
        super().__init__(device)
        self.pasta = PASTA_MODELO_PARAKEET if pasta is None else Path(pasta)
        self.adaptacao = adaptacao

    def _carregar_modelo(self):
        try:
            import onnx_asr
            import onnxruntime
        except ImportError as erro:
            raise MotorIndisponivel(
                f"{self.nome}: onnx-asr nao esta instalado no .venv ({erro}); instalar com: "
                f"{COMANDO_INSTALAR_ONNX_ASR}"
            ) from erro
        if not self.pasta.is_dir():
            raise MotorIndisponivel(
                f"{self.nome}: modelo em falta em models/{self.pasta.name}; descarregar com: "
                f"{COMANDO_DESCARREGAR_PARAKEET}"
            )
        disponiveis = onnxruntime.get_available_providers()
        if self.device == "cuda":
            if "CUDAExecutionProvider" not in disponiveis:
                raise MotorIndisponivel(
                    f"{self.nome}: o onnxruntime deste .venv nao tem CUDA "
                    f"(providers: {', '.join(disponiveis)}); pedir este motor em cpu "
                    f"(no avaliador: --motores {self.nome}:cpu)"
                )
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        modelo = onnx_asr.load_model(
            NOME_ONNX_ASR_PARAKEET,
            str(self.pasta),
            providers=providers,
            sess_options=opcoes_de_sessao_onnx(onnxruntime),
        )
        self.device_real = self.device
        if self.adaptacao is not None:
            self.adaptacao.instalar(modelo)
        return modelo

    def _inferir(self, modelo, pcm16: bytes, lingua: str | None):
        audio = pcm16_para_float32(pcm16)
        if self.adaptacao is None:
            return modelo.recognize(audio, sample_rate=TAXA_DO_MOTOR), lingua, False
        with self.adaptacao.ao_transcrever(lingua):
            texto = modelo.recognize(audio, sample_rate=TAXA_DO_MOTOR)
        return self.adaptacao.corrigir(texto, lingua), lingua, False


#: Nomes que os scripts aceitam, pela ordem em que sao medidos.
FABRICAS: dict[str, Callable[[str], MotorBase]] = {
    "whisper-medium": lambda device: MotorFasterWhisper("medium", device),
    "whisper-large-v3-turbo": lambda device: MotorFasterWhisper("large-v3-turbo", device),
    "parakeet-tdt-0.6b-v3": lambda device: MotorParakeet(device),
}
MOTORES = tuple(FABRICAS)


def criar_motor(nome: str, device: str = "cuda", adaptacao: "Adaptacao | None" = None) -> MotorBase:
    """Um motor por nome da lista fechada `MOTORES`. Nao carrega nada ainda.

    A `adaptacao` so se aplica ao Parakeet; noutro motor fica uma linha no
    log a dizer que foi ignorada.
    """
    fabrica = FABRICAS.get(nome)
    if fabrica is None:
        raise ValueError(f"motor STT '{nome}' desconhecido; escolher de {', '.join(MOTORES)}")
    motor = fabrica(device)
    if adaptacao is not None:
        if isinstance(motor, MotorParakeet):
            motor.adaptacao = adaptacao
        else:
            adaptacao.avisar(f"adaptacao: ignorada no motor {nome} (so o Parakeet a suporta)")
    return motor


# --- Autoteste das partes puras (sem GPU, sem modelos, sem som) --------------


class _MotorFalso(MotorBase):
    nome = "falso"

    def __init__(self, indisponivel: bool = False) -> None:
        super().__init__("cpu")
        self.indisponivel = indisponivel
        self.carregamentos = 0

    def _carregar_modelo(self):
        self.carregamentos += 1
        if self.indisponivel:
            raise MotorIndisponivel("falso: indisponivel de proposito")
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return f"  {len(pcm16) // 2}   amostras ", lingua or "en", lingua is None


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    motor = _MotorFalso()
    um_segundo = b"\x00\x00" * TAXA_DO_MOTOR
    r = motor.transcrever(um_segundo, lingua="pt")
    verificar("transcreve e colapsa espacos", r.texto, "16000 amostras")
    verificar("lingua pedida devolvida", (r.lingua, r.lingua_detetada), ("pt", False))
    verificar("duracao de 1 s", r.duracao_audio_s, 1.0)
    motor.transcrever(um_segundo)
    verificar("carrega uma unica vez", motor.carregamentos, 1)
    verificar("audio vazio da texto vazio", motor.transcrever(b"").texto, "")
    for nome, valor, erro in (
        ("impar recusado", b"\x00", ValueError),
        ("str recusada", "abc", TypeError),
    ):
        try:
            motor.transcrever(valor)  # type: ignore[arg-type]
            falhas.append(f"{nome}: nao levantou")
        except erro:
            print(f"ok   {nome}")
    try:
        motor.transcrever(um_segundo, lingua="fr")
        falhas.append("lingua fora da lista: nao levantou")
    except ValueError:
        print("ok   lingua fora da lista recusada")
    try:
        _MotorFalso(indisponivel=True).transcrever(um_segundo)
        falhas.append("indisponivel: nao levantou")
    except MotorIndisponivel:
        print("ok   indisponivel levanta MotorIndisponivel")
    verificar("lista fechada de motores", MOTORES, ("whisper-medium", "whisper-large-v3-turbo", "parakeet-tdt-0.6b-v3"))
    try:
        criar_motor("inventado")
        falhas.append("motor desconhecido: nao levantou")
    except ValueError:
        print("ok   motor desconhecido recusado")
    parakeet = MotorParakeet("cpu", pasta=RAIZ / "tmp" / "nao-existe-parakeet")
    try:
        parakeet.carregar()
        falhas.append("parakeet sem modelo: carregou")
    except MotorIndisponivel as erro:
        verificar("parakeet sem pacote ou modelo e indisponivel, com comando", "python" in str(erro), True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do jarvis.stt completo.")
    return 0


if __name__ == "__main__":
    if "--autoteste" in sys.argv[1:]:
        sys.exit(_autoteste())
    print(__doc__)
