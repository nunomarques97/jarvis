r"""Testes da cache opcional de modelos de scripts/transcrever_ficheiro.py.

A cache foi acrescentada pela T7 (o arnes de medicao transcreve 20 WAV no mesmo
processo e pagava ~5 s de carregamento em cada um). O Reviewer da tentativa 2
apanhou que o ramo novo so estava provado a mao (nit 4): estes testes provam-no
sozinhos, com um `faster_whisper` FALSO injetado em `sys.modules` — nao tocam no
GPU, nao carregam nenhum modelo real e nao leem nenhum WAV.

O que tem de continuar verdade:
  - por omissao (`usar_cache=False`) cada chamada carrega o modelo, como a T3/T6
    mediram: e nesse caminho que vive a latencia de carregamento do log;
  - com `usar_cache=True` o mesmo (nome, device) carrega UMA vez;
  - a cache e por (nome, device), nao por nome;
  - uma chamada por omissao NUNCA le da cache, mesmo com a cache cheia.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts import transcrever_ficheiro  # noqa: E402


class ModeloFalso:
    """O que `faster_whisper.WhisperModel(...)` devolveria, sem GPU nenhum."""

    def __init__(self, nome: str, device: str, compute_type: str, download_root: str) -> None:
        self.nome = nome
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root


def modulo_faster_whisper_falso(registo: list[tuple[str, str]]) -> types.ModuleType:
    """Um modulo com o mesmo nome e a mesma forma, que regista cada carregamento."""
    modulo = types.ModuleType("faster_whisper")

    def whisper_model(nome, device, compute_type, download_root):
        registo.append((nome, device))
        return ModeloFalso(nome, device, compute_type, download_root)

    modulo.WhisperModel = whisper_model  # type: ignore[attr-defined]
    return modulo


class TestCacheDeModelos(unittest.TestCase):
    def setUp(self) -> None:
        transcrever_ficheiro.limpar_cache_de_modelos()
        self.carregamentos: list[tuple[str, str]] = []
        remendo = mock.patch.dict(
            sys.modules,
            {"faster_whisper": modulo_faster_whisper_falso(self.carregamentos)},
        )
        remendo.start()
        self.addCleanup(remendo.stop)
        self.addCleanup(transcrever_ficheiro.limpar_cache_de_modelos)

    def test_por_omissao_cada_chamada_carrega_o_modelo_outra_vez(self) -> None:
        for _ in range(3):
            transcrever_ficheiro.carregar_modelo("medium", "cuda")
        self.assertEqual(self.carregamentos, [("medium", "cuda")] * 3)
        self.assertEqual(len(transcrever_ficheiro._MODELOS_EM_CACHE), 0)

    def test_com_cache_tres_chamadas_carregam_uma_vez_e_devolvem_o_mesmo_objeto(self) -> None:
        primeiro = transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        segundo = transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        terceiro = transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        self.assertEqual(self.carregamentos, [("medium", "cuda")])
        self.assertIs(primeiro, segundo)
        self.assertIs(primeiro, terceiro)

    def test_a_cache_distingue_o_device_e_o_nome_do_modelo(self) -> None:
        transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        transcrever_ficheiro.carregar_modelo("medium", "cpu", usar_cache=True)
        transcrever_ficheiro.carregar_modelo("small", "cuda", usar_cache=True)
        transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        self.assertEqual(
            self.carregamentos,
            [("medium", "cuda"), ("medium", "cpu"), ("small", "cuda")],
        )
        self.assertEqual(len(transcrever_ficheiro._MODELOS_EM_CACHE), 3)

    def test_uma_chamada_por_omissao_nao_le_da_cache_mesmo_com_ela_cheia(self) -> None:
        em_cache = transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        novo = transcrever_ficheiro.carregar_modelo("medium", "cuda")
        self.assertEqual(len(self.carregamentos), 2)
        self.assertIsNot(novo, em_cache)

    def test_o_compute_type_continua_a_seguir_o_device(self) -> None:
        # D39: float16 no GPU, int8 fora dele — a cache nao podia mudar isto.
        no_gpu = transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        no_cpu = transcrever_ficheiro.carregar_modelo("medium", "cpu", usar_cache=True)
        self.assertEqual(no_gpu.compute_type, "float16")
        self.assertEqual(no_cpu.compute_type, "int8")

    def test_limpar_cache_devolve_quantos_estavam_e_deixa_a_vazia(self) -> None:
        transcrever_ficheiro.carregar_modelo("medium", "cuda", usar_cache=True)
        transcrever_ficheiro.carregar_modelo("small", "cpu", usar_cache=True)
        self.assertEqual(transcrever_ficheiro.limpar_cache_de_modelos(), 2)
        self.assertEqual(len(transcrever_ficheiro._MODELOS_EM_CACHE), 0)
        self.assertEqual(transcrever_ficheiro.limpar_cache_de_modelos(), 0)


if __name__ == "__main__":
    unittest.main()
