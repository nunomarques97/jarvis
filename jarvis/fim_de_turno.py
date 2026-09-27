r"""Fim de turno: decidir quando o utilizador acabou de falar.

Ate aqui a frase das maos-livres acabava sempre com 0,6 s de silencio (VAD
webrtc). E pouco quando o utilizador para a meio para procurar uma palavra, e
e demais quando a frase ja esta claramente acabada. Com o Smart Turn v3 (um
modelo de audio de 8 MB, BSD-2, da Pipecat) a decisao passa a ser:

  1. depois de `SILENCIO_ANTES_DO_MODELO_S` (0,2 s) de silencio, o audio da
     frase (os ultimos 8 s) vai ao modelo, que devolve a probabilidade de a
     frase estar acabada;
  2. acabada (probabilidade >= `LIMIAR_DE_FIM`): o turno fecha quando o
     silencio chega a `SILENCIO_DEPOIS_DE_ACABADA_S` (0,3 s);
  3. inacabada: continua a ouvir ate `[ouvido].fim_de_turno_maximo_s` de
     silencio (1,5 s por omissao); se a fala voltar, recomeca tudo.

Uma frase com menos de `FALA_MINIMA_PARA_O_MODELO_S` de voz (um estalido,
um toque no microfone) nao e um turno que o modelo possa julgar: ai vale o
silencio fixo.

As caracteristicas sao as do extrator Whisper que ja vem no faster-whisper
(log-mel de 80 bandas, 8 s, com o audio encostado ao fim e normalizado como
na inferencia de referencia do Smart Turn); nao e preciso o `transformers`.
O modelo corre com o onnxruntime ja instalado, em CPU com um fio.

A inferencia corre numa thread propria (`em_fundo=True`): a captura do
microfone nunca espera por ela. Cada pedido leva a geracao do silencio em
que foi feito; se a fala voltar ou a frase acabar antes da resposta, a
resposta que chega depois e ignorada. Enquanto o veredicto nao chega, o
turno so fecha pelo maximo. Com `em_fundo=False` o modelo corre no proprio
pedido e o veredicto so vale depois do tempo que a inferencia levou (e o que
`scripts/avaliar_fim_de_turno.py` usa para simular o tempo real).

Sem o ficheiro do modelo, ou com `[ouvido].fim_de_turno = "silencio"`, fica
o silencio fixo de sempre (`SILENCIO_FIXO_S`), com UMA linha no log. Um erro
do modelo a meio da escuta faz o mesmo: uma linha, e dai em diante o
silencio fixo.

    .venv\Scripts\python -m jarvis.fim_de_turno --autoteste
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.config import FIM_DE_TURNO_MAXIMO_S, FINS_DE_TURNO  # noqa: E402

MODELO_SMART_TURN = RAIZ / "models" / "smart-turn" / "smart-turn-v3.2-cpu.onnx"
#: De onde veio o ficheiro e o sha256 dele, registados em docs/MODELOS.md.
URL_DO_SMART_TURN = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx"
SHA256_DO_SMART_TURN = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"

METODO_SMART_TURN, METODO_SILENCIO = FINS_DE_TURNO

TAXA = 16000
#: O modelo ve os ultimos 8 s da frase (menos do que isso: zeros antes).
JANELA_DO_MODELO_S = 8
AMOSTRAS_DA_JANELA = JANELA_DO_MODELO_S * TAXA

#: Probabilidade a partir da qual a frase conta como acabada.
LIMIAR_DE_FIM = 0.5
#: Silencio (VAD) antes de perguntar ao modelo.
SILENCIO_ANTES_DO_MODELO_S = 0.2
#: Silencio que fecha o turno quando o modelo diz que a frase acabou.
SILENCIO_DEPOIS_DE_ACABADA_S = 0.3
#: O silencio fixo de sempre: sem modelo, e o unico criterio.
SILENCIO_FIXO_S = 0.6
#: Voz na frase (depois dos chunks que a comecaram) abaixo da qual o modelo
#: nao e ouvido e vale o silencio fixo.
FALA_MINIMA_PARA_O_MODELO_S = 0.3
#: Os chunks de 30 ms do ouvido.
DURACAO_DO_CHUNK_S = 0.03

#: Somas de 30 ms em virgula flutuante ficam um nada abaixo do valor exato.
_FOLGA_S = 1e-6


def caracteristicas(pcm16: bytes):
    """PCM16 mono a 16 kHz -> as entradas do modelo, float32 (1, 80, 800).

    Como a inferencia de referencia do Smart Turn: fica o fim do audio (ate
    8 s), com zeros ANTES quando e mais curto, normalizado a media 0 e
    variancia 1, e depois o log-mel do Whisper (aqui o extrator do
    faster-whisper, sem padding extra).
    """
    import numpy as np
    from faster_whisper.feature_extractor import FeatureExtractor

    amostras = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype="<i2").astype(np.float32) / 32768.0
    amostras = amostras[-AMOSTRAS_DA_JANELA:]
    if amostras.size < AMOSTRAS_DA_JANELA:
        amostras = np.pad(amostras, (AMOSTRAS_DA_JANELA - amostras.size, 0))
    amostras = (amostras - amostras.mean()) / np.sqrt(amostras.var() + 1e-7)
    extrator = _extrator(FeatureExtractor)
    return extrator(amostras.astype(np.float32), padding=0)[None, :, :].astype(np.float32)


_EXTRATORES: dict[type, object] = {}


def _extrator(classe):
    # Os filtros mel calculam-se uma vez.
    extrator = _EXTRATORES.get(classe)
    if extrator is None:
        extrator = _EXTRATORES[classe] = classe(chunk_length=JANELA_DO_MODELO_S)
    return extrator


class JuizSmartTurn:
    """O Smart Turn v3 em ONNX: probabilidade de a frase estar acabada."""

    nome = METODO_SMART_TURN

    def __init__(self, modelo: Path = MODELO_SMART_TURN) -> None:
        if not Path(modelo).is_file():
            raise FileNotFoundError(f"modelo Smart Turn em falta: '{modelo}' (ver docs/MODELOS.md)")
        import onnxruntime

        opcoes = onnxruntime.SessionOptions()
        opcoes.intra_op_num_threads = 1
        opcoes.inter_op_num_threads = 1
        self._sessao = onnxruntime.InferenceSession(
            str(modelo), sess_options=opcoes, providers=["CPUExecutionProvider"]
        )
        self._entrada = self._sessao.get_inputs()[0].name

    def probabilidade(self, pcm16: bytes) -> float:
        """0 (a frase continua) a 1 (acabou)."""
        saida = self._sessao.run(None, {self._entrada: caracteristicas(pcm16)})
        return float(saida[0].reshape(-1)[0])

    def aquecer(self) -> float:
        """Uma inferencia em silencio, para a primeira frase nao pagar o arranque. Devolve os ms."""
        antes = time.perf_counter()
        self.probabilidade(b"\x00\x00" * TAXA)
        return (time.perf_counter() - antes) * 1000


class FimDeTurno:
    """Decide, chunk a chunk, se o silencio atual fecha o turno.

    `acabou(silencio_s, audio)` e chamado pelo ouvido a cada chunk da frase
    em curso, com o silencio seguido acumulado (0 num chunk com voz) e uma
    funcao que devolve o audio da frase ate agora. Devolve o motivo do fim
    (texto para o log) ou None. `reiniciar()` marca uma frase nova: qualquer
    pedido ao modelo ainda em curso deixa de contar.
    """

    def __init__(
        self,
        juiz: JuizSmartTurn | None = None,
        *,
        maximo_s: float = FIM_DE_TURNO_MAXIMO_S,
        silencio_fixo_s: float = SILENCIO_FIXO_S,
        antes_do_modelo_s: float = SILENCIO_ANTES_DO_MODELO_S,
        depois_de_acabada_s: float = SILENCIO_DEPOIS_DE_ACABADA_S,
        limiar: float = LIMIAR_DE_FIM,
        fala_minima_s: float = FALA_MINIMA_PARA_O_MODELO_S,
        duracao_do_chunk_s: float = DURACAO_DO_CHUNK_S,
        em_fundo: bool = True,
        escrever: Callable[[str], object] = print,
    ) -> None:
        if not antes_do_modelo_s <= depois_de_acabada_s <= maximo_s:
            raise ValueError(
                f"esperas incoerentes: {antes_do_modelo_s} <= {depois_de_acabada_s} <= {maximo_s} nao se verifica"
            )
        self.juiz = juiz
        self.maximo_s = maximo_s
        self.silencio_fixo_s = silencio_fixo_s
        self.antes_do_modelo_s = antes_do_modelo_s
        self.depois_de_acabada_s = depois_de_acabada_s
        self.limiar = limiar
        self.fala_minima_s = fala_minima_s
        self.duracao_do_chunk_s = duracao_do_chunk_s
        self.em_fundo = em_fundo
        self.escrever = escrever
        #: O modelo falhou uma vez: dai em diante, silencio fixo.
        self._avariado = False
        #: Probabilidade e ms de cada inferencia usada (para medir).
        self.probabilidades: list[float] = []
        self.inferencias_ms: list[float] = []
        self._trinco = threading.Lock()
        self._acordar = threading.Condition(self._trinco)
        self._geracao = 0
        #: Voz ouvida na frase em curso (chunks com silencio 0).
        self._fala_s = 0.0
        #: Ja se perguntou ao modelo neste silencio.
        self._pedido = False
        #: Veredicto do pedido desta geracao: (probabilidade ou None se falhou, silencio a partir do qual vale).
        self._veredicto: tuple[float | None, float] | None = None
        #: Pedido a espera da thread: (geracao, audio).
        self._pendente: tuple[int, bytes] | None = None
        self._fio: threading.Thread | None = None
        self._fechado = False

    @property
    def metodo(self) -> str:
        return METODO_SMART_TURN if self.juiz is not None and not self._avariado else METODO_SILENCIO

    def descrever(self) -> str:
        if self.metodo == METODO_SILENCIO:
            return f"silencio fixo de {self.silencio_fixo_s:g} s"
        return (
            f"Smart Turn depois de {self.antes_do_modelo_s:g} s de silencio: acabada fecha aos "
            f"{self.depois_de_acabada_s:g} s, inacabada espera ate {self.maximo_s:g} s"
        )

    # -- estado por frase e por silencio

    def reiniciar(self) -> None:
        """Frase nova (ou fim da frase): o que o modelo ainda responder ja nao conta."""
        with self._trinco:
            self._novo_silencio()
            self._fala_s = 0.0

    def _novo_silencio(self) -> None:
        self._geracao += 1
        self._pedido = False
        self._veredicto = None
        self._pendente = None

    def acabou(self, silencio_s: float, audio: Callable[[], bytes]) -> str | None:
        if silencio_s <= 0:
            self._fala_s += self.duracao_do_chunk_s
            if self._pedido:
                with self._trinco:
                    self._novo_silencio()  # a fala voltou: o veredicto deste silencio ja nao serve
            return None
        if self.metodo == METODO_SILENCIO or self._fala_s + _FOLGA_S < self.fala_minima_s:
            if silencio_s + _FOLGA_S >= self.silencio_fixo_s:
                return "fim da fala (VAD)"
            return None
        if not self._pedido and silencio_s + _FOLGA_S >= self.antes_do_modelo_s:
            self._pedir(audio(), silencio_s)
        with self._trinco:
            veredicto = self._veredicto
        if veredicto is not None and veredicto[0] is None:
            # O modelo falhou neste silencio: vale o silencio fixo.
            return "fim da fala (VAD)" if silencio_s + _FOLGA_S >= self.silencio_fixo_s else None
        if veredicto is not None and silencio_s + _FOLGA_S >= veredicto[1]:
            probabilidade = veredicto[0]
            if probabilidade >= self.limiar and silencio_s + _FOLGA_S >= self.depois_de_acabada_s:
                return f"fim do turno (Smart Turn {probabilidade:.2f})"
        if silencio_s + _FOLGA_S >= self.maximo_s:
            if veredicto is None or silencio_s + _FOLGA_S < veredicto[1]:
                return "fim da fala (espera maxima, sem resposta do Smart Turn)"
            return f"fim da fala (espera maxima, Smart Turn {veredicto[0]:.2f})"
        return None

    # -- o pedido ao modelo

    def _pedir(self, audio: bytes, silencio_s: float) -> None:
        self._pedido = True
        audio = audio[-AMOSTRAS_DA_JANELA * 2 :]
        if not self.em_fundo:
            probabilidade, ms = self._inferir(audio)
            # O veredicto so vale depois do tempo que a inferencia levou.
            with self._trinco:
                self._veredicto = (probabilidade, silencio_s + ms / 1000)
            return
        with self._acordar:
            if self._fechado:
                return
            self._pendente = (self._geracao, audio)
            if self._fio is None:
                self._fio = threading.Thread(target=self._em_ciclo, name="fim-de-turno", daemon=True)
                self._fio.start()
            self._acordar.notify()

    def _inferir(self, audio: bytes) -> tuple[float | None, float]:
        antes = time.perf_counter()
        try:
            probabilidade = float(self.juiz.probabilidade(audio))
        except Exception as erro:  # noqa: BLE001 - sem modelo fica o silencio fixo
            if not self._avariado:
                self._avariado = True
                self.escrever(
                    f"fim de turno | o Smart Turn falhou ({erro!r}); daqui em diante, "
                    f"silencio fixo de {self.silencio_fixo_s:g} s"
                )
            return None, 0.0
        ms = (time.perf_counter() - antes) * 1000
        self.probabilidades.append(probabilidade)
        self.inferencias_ms.append(ms)
        return probabilidade, ms

    def _em_ciclo(self) -> None:
        while True:
            with self._acordar:
                while self._pendente is None and not self._fechado:
                    self._acordar.wait()
                if self._fechado:
                    return
                geracao, audio = self._pendente
                self._pendente = None
            probabilidade, _ms = self._inferir(audio)
            with self._trinco:
                # So o pedido do silencio atual conta; um mais antigo chega tarde demais.
                if geracao == self._geracao:
                    self._veredicto = (probabilidade, 0.0)

    def fechar(self) -> None:
        """Para a thread do modelo (os testes; no jarvis ela morre com o processo)."""
        with self._acordar:
            self._fechado = True
            self._pendente = None
            self._acordar.notify_all()
        fio = self._fio
        if fio is not None:
            fio.join(5.0)


def criar_fim_de_turno(
    metodo: str = METODO_SMART_TURN,
    maximo_s: float = FIM_DE_TURNO_MAXIMO_S,
    *,
    escrever: Callable[[str], object] = print,
    modelo: Path = MODELO_SMART_TURN,
    criar_juiz: Callable[[Path], JuizSmartTurn] = JuizSmartTurn,
) -> FimDeTurno:
    """O fim de turno do config, com UMA linha no log a dizer qual ficou.

    Sem o ficheiro do modelo (ou se ele nao carregar) fica o silencio fixo.
    """
    if metodo not in FINS_DE_TURNO:
        raise ValueError(f"fim de turno desconhecido: {metodo!r} (so {', '.join(FINS_DE_TURNO)})")
    juiz = None
    motivo = "[ouvido] fim_de_turno = silencio"
    if metodo == METODO_SMART_TURN:
        try:
            juiz = criar_juiz(modelo)
            aquecer = getattr(juiz, "aquecer", None)
            if aquecer is not None:
                aquecer()
        except FileNotFoundError:
            motivo = f"modelo Smart Turn em falta em models/{Path(modelo).parent.name}/{Path(modelo).name}"
            juiz = None
        except Exception as erro:  # noqa: BLE001 - sem modelo fica o silencio fixo
            motivo = f"Smart Turn por carregar ({erro!r})"
            juiz = None
    fim = FimDeTurno(juiz, maximo_s=maximo_s, escrever=escrever)
    if juiz is None:
        escrever(f"fim de turno | {fim.descrever()} ({motivo})")
    else:
        escrever(f"fim de turno | {fim.descrever()}")
    return fim


# --- Autoteste (juiz falso; sem modelo, microfone nem som) ----------------------


class _JuizFixo:
    def __init__(self, probabilidade: float) -> None:
        self.valor = probabilidade
        self.pedidos = 0

    def probabilidade(self, pcm16: bytes) -> float:
        self.pedidos += 1
        return self.valor


def _fim_ao_fim_de(fim: FimDeTurno, chunk_s: float = 0.03, limite_s: float = 3.0) -> float | None:
    for _ in range(round(1.0 / chunk_s)):  # 1 s de fala antes do silencio
        fim.acabou(0.0, lambda: b"")
    silencio = 0.0
    while silencio < limite_s:
        silencio += chunk_s
        if fim.acabou(silencio, lambda: b"\x00\x00" * 160):
            return silencio
    return None


def _autoteste() -> int:
    falhas: list[str] = []

    def verificar(nome: str, condicao: bool, detalhe: str = "") -> None:
        if condicao:
            print(f"ok   {nome}")
        else:
            falhas.append(f"{nome} {detalhe}".strip())

    fixo = _fim_ao_fim_de(FimDeTurno(None, escrever=lambda _t: None))
    verificar("sem modelo: 0.6 s de silencio", fixo is not None and abs(fixo - 0.6) < 0.031, f"({fixo})")
    acabada = _fim_ao_fim_de(FimDeTurno(_JuizFixo(0.9), em_fundo=False, escrever=lambda _t: None))
    verificar("frase acabada: cerca de 0.3 s", acabada is not None and abs(acabada - 0.3) < 0.031, f"({acabada})")
    inacabada = _fim_ao_fim_de(FimDeTurno(_JuizFixo(0.1), em_fundo=False, escrever=lambda _t: None))
    verificar("frase inacabada: ate 1.5 s", inacabada is not None and abs(inacabada - 1.5) < 0.031, f"({inacabada})")
    linhas: list[str] = []
    criado = criar_fim_de_turno(escrever=linhas.append, modelo=RAIZ / "models" / "nao-existe" / "x.onnx")
    verificar("sem ficheiro: silencio fixo e uma linha", criado.metodo == METODO_SILENCIO and len(linhas) == 1)
    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do fim de turno completo (juiz falso, sem modelo nem som).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.fim_de_turno", description=__doc__.splitlines()[0])
    parser.add_argument("--autoteste", action="store_true", help="juiz falso; sem modelo, microfone nem som")
    args = parser.parse_args(argv)
    if args.autoteste:
        return _autoteste()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
