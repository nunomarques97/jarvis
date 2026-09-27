r"""VAD Silero para ouvir o utilizador enquanto o jarvis fala.

Usa o ficheiro `models/openwakeword/silero_vad.onnx` que veio com o
openWakeWord (ver docs/MODELOS.md), com o onnxruntime ja instalado: nada novo
e descarregado nem instalado. Corre em CPU com um so fio, sobre os mesmos
chunks de 30 ms do ouvido (480 amostras a 16 kHz), e guarda o estado da rede
entre chunks, por isso a probabilidade de cada chunk ja tem em conta o que
veio antes.

Porque o Silero e nao o webrtcvad: o webrtcvad chama voz a quase tudo o que
tem energia (uma tosse, o teclado, a respiracao no microfone do headset); o
Silero foi treinado para separar fala de ruido, que e o que decide se a voz
do jarvis deve pausar.

    .venv\Scripts\python -m jarvis.vad_silero --autoteste
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

MODELO_SILERO = RAIZ / "models" / "openwakeword" / "silero_vad.onnx"
#: sha256 do ficheiro registado em docs/MODELOS.md.
SHA256_DO_SILERO = "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28"

TAXA = 16000
#: Probabilidade a partir da qual um chunk conta como fala.
LIMIAR_DE_FALA = 0.5


class VadSilero:
    """O Silero VAD (v4, entradas input/sr/h/c) chunk a chunk, com estado."""

    def __init__(self, modelo: Path = MODELO_SILERO, *, limiar: float = LIMIAR_DE_FALA) -> None:
        if not Path(modelo).is_file():
            raise FileNotFoundError(f"modelo do Silero VAD em falta: '{modelo}' (ver docs/MODELOS.md)")
        if not 0.0 < limiar < 1.0:
            raise ValueError(f"limiar do Silero fora de (0, 1): {limiar!r}")
        import numpy as np
        import onnxruntime

        opcoes = onnxruntime.SessionOptions()
        opcoes.intra_op_num_threads = 1
        opcoes.inter_op_num_threads = 1
        self._np = np
        self._sessao = onnxruntime.InferenceSession(
            str(modelo), sess_options=opcoes, providers=["CPUExecutionProvider"]
        )
        self._taxa = np.array(TAXA, dtype=np.int64)
        self.limiar = limiar
        self.nome = "silero"
        self.reiniciar()

    def reiniciar(self) -> None:
        """Esquece o estado da rede (o proximo chunk comeca do zero)."""
        self._h = self._np.zeros((2, 1, 64), dtype=self._np.float32)
        self._c = self._np.zeros((2, 1, 64), dtype=self._np.float32)

    def probabilidade(self, chunk: bytes) -> float:
        """Probabilidade de fala (0 a 1) de um chunk PCM16 mono a 16 kHz."""
        np = self._np
        amostras = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
        if amostras.size == 0:
            return 0.0
        saida, self._h, self._c = self._sessao.run(
            None, {"input": amostras[None, :], "sr": self._taxa, "h": self._h, "c": self._c}
        )
        return float(saida[0][0])

    def e_fala(self, chunk: bytes) -> bool:
        return self.probabilidade(chunk) >= self.limiar


def _autoteste() -> int:
    """Carrega o modelo e verifica que silencio e ruido branco fraco nao sao fala; sem som."""
    import numpy as np

    falhas: list[str] = []

    def verificar(nome: str, condicao: bool, detalhe: str = "") -> None:
        if condicao:
            print(f"ok   {nome}")
        else:
            falhas.append(f"{nome} {detalhe}".strip())

    if not MODELO_SILERO.is_file():
        print(f"ERRO: modelo em falta: {MODELO_SILERO} (ver docs/MODELOS.md)")
        return 1
    vad = VadSilero()
    silencio = b"\x00\x00" * 480
    probs = [vad.probabilidade(silencio) for _ in range(30)]
    verificar("silencio nao e fala", max(probs) < vad.limiar, f"(max {max(probs):.2f})")
    vad.reiniciar()
    gerador = np.random.default_rng(7)
    ruido = [(gerador.normal(0, 300, 480)).astype("<i2").tobytes() for _ in range(30)]
    probs = [vad.probabilidade(chunk) for chunk in ruido]
    verificar("ruido branco fraco nao e fala", max(probs) < vad.limiar, f"(max {max(probs):.2f})")
    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do Silero VAD completo (sem som).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.vad_silero", description=__doc__.splitlines()[0])
    parser.add_argument("--autoteste", action="store_true", help="carrega o modelo e testa silencio e ruido; sem som")
    args = parser.parse_args(argv)
    if args.autoteste:
        return _autoteste()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
