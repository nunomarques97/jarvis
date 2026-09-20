r"""Verifica o ambiente do jarvis: torch, CUDA e uma transcricao real no GPU.

Portao da Fase 2 (T1): so passa se o faster-whisper carregar o modelo no GPU e
transcrever audio sintetico. Sai com codigo 0 apenas nesse caso; qualquer queda
para CPU sai com codigo 1 e imprime o erro completo.

Correr a partir da raiz do repositorio:
    .venv\Scripts\python scripts/verificar_ambiente.py
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.consola import forcar_consola_utf8  # noqa: E402

PASTA_MODELOS = RAIZ / "models" / "faster-whisper"
MODELO = "small"
TAXA_AMOSTRAGEM = 16_000
SEGUNDOS_DE_SILENCIO = 1.0


def registar_dlls_do_torch() -> list[str]:
    """Torna as DLLs de CUDA que vieram no wheel do torch visiveis ao CTranslate2.

    Tudo dentro do .venv: nada e instalado ou registado no Windows (D14f).
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


def main() -> int:
    forcar_consola_utf8()
    import numpy as np
    import torch

    print("=== jarvis - verificacao do ambiente ===")
    print(f"python          = {sys.version.split()[0]}")
    print(f"torch           = {torch.__version__}")

    cuda_disponivel = bool(torch.cuda.is_available()) and torch.cuda.device_count() > 0
    print(f"CUDA={cuda_disponivel}")
    print(f"GPUs visiveis   = {torch.cuda.device_count()} "
          f"(torch.cuda.is_available()={bool(torch.cuda.is_available())})")
    if cuda_disponivel:
        print(f"GPU             = {torch.cuda.get_device_name(0)}")
        capacidade = torch.cuda.get_device_capability(0)
        print(f"compute cap.    = sm_{capacidade[0]}{capacidade[1]}")
        print(f"torch CUDA      = {torch.version.cuda}")
        print(f"cuDNN           = {torch.backends.cudnn.version()}")
    else:
        print("GPU             = (nenhuma visivel para o torch)")

    # Antes de importar o CTranslate2: e no import que ele carrega a DLL nativa.
    pastas = registar_dlls_do_torch()
    print(f"DLLs do venv    = {', '.join(pastas) if pastas else '(nenhuma)'}")

    import ctranslate2
    import faster_whisper

    print(f"faster-whisper  = {faster_whisper.__version__}")
    print(f"ctranslate2     = {ctranslate2.__version__}")
    print(f"tipos GPU CT2   = {ctranslate2.get_supported_compute_types('cuda') if cuda_disponivel else '(n/a)'}")

    if not cuda_disponivel:
        print("FALHOU: o torch nao ve o GPU; o portao do ambiente exige CUDA=True.")
        return 1

    PASTA_MODELOS.mkdir(parents=True, exist_ok=True)
    print(f"modelo          = {MODELO} (float16, cuda) em models/faster-whisper")

    t0 = time.perf_counter()
    try:
        modelo = faster_whisper.WhisperModel(
            MODELO,
            device="cuda",
            compute_type="float16",
            download_root=str(PASTA_MODELOS),
        )
    except Exception:
        print("FALHOU: o faster-whisper nao carregou o modelo no GPU. Erro completo:")
        traceback.print_exc(file=sys.stdout)
        return 1
    t_carregamento = time.perf_counter() - t0

    silencio = np.zeros(int(TAXA_AMOSTRAGEM * SEGUNDOS_DE_SILENCIO), dtype=np.float32)
    t0 = time.perf_counter()
    try:
        segmentos, info = modelo.transcribe(silencio, language="pt", beam_size=1, vad_filter=False)
        segmentos = list(segmentos)
    except Exception:
        print("FALHOU: o faster-whisper nao transcreveu no GPU. Erro completo:")
        traceback.print_exc(file=sys.stdout)
        return 1
    t_transcricao = time.perf_counter() - t0

    print(f"tempo de carregamento = {t_carregamento:.2f} s")
    print(f"tempo de transcricao  = {t_transcricao:.2f} s (1,0 s de silencio sintetico)")
    print(f"segmentos             = {len(segmentos)} | duracao vista = {info.duration:.2f} s")
    print("Nota: na primeira execucao estes tempos incluem o download do modelo e a "
          "compilacao JIT dos kernels CUDA para sm_120.")
    print("OK: ambiente pronto, transcricao feita no GPU.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
