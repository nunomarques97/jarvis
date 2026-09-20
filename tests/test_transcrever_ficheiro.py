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

from jarvis.lingua import LINGUA_FIXA_DO_PRODUTO  # noqa: E402
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


# --- T8/D58b/S10: deteccao de lingua no caminho dos scripts ----------------


class SegmentoFalso:
    def __init__(self, text: str) -> None:
        self.text = text


class InfoFalso:
    """O `TranscriptionInfo` do faster-whisper, reduzido ao que e lido."""

    def __init__(self, duration: float, all_language_probs, language: str) -> None:
        self.duration = duration
        self.all_language_probs = all_language_probs
        self.language = language
        self.language_probability = 0.0 if not all_language_probs else all_language_probs[0][1]


class ModeloQueTranscreve:
    """Regista os kwargs de cada `transcribe()` e devolve um info a medida.

    `detect_language` existe de proposito e REBENTA: a S10 proibe chamar a
    deteccao a parte (pagava o encoder duas vezes). Se alguem a chamar um dia,
    o teste cai em vez de passar a custar latencia em silencio.
    """

    def __init__(self, nome: str, device: str, compute_type: str, download_root: str) -> None:
        self.nome = nome
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.chamadas: list[dict] = []
        self.probabilidades = [("pt", 0.96), ("es", 0.02), ("en", 0.01)]
        self.texto = "que horas sao"

    def transcribe(self, caminho, **kwargs):
        self.chamadas.append(dict(kwargs))
        info = InfoFalso(1.5, self.probabilidades, "pt")
        return iter([SegmentoFalso(self.texto)]), info

    def detect_language(self, *args, **kwargs):  # pragma: no cover - so falha
        raise AssertionError(
            "detect_language() chamado a parte: paga o encoder duas vezes (S10)"
        )


class TestDeteccaoDeLinguaNoTranscritor(unittest.TestCase):
    """Criterios 1 e 2 da T8, no caminho dos scripts."""

    def setUp(self) -> None:
        transcrever_ficheiro.limpar_cache_de_modelos()
        self.modelos: list[ModeloQueTranscreve] = []
        modulo = types.ModuleType("faster_whisper")

        def whisper_model(nome, device, compute_type, download_root):
            modelo = ModeloQueTranscreve(nome, device, compute_type, download_root)
            self.modelos.append(modelo)
            return modelo

        modulo.WhisperModel = whisper_model  # type: ignore[attr-defined]
        patch = mock.patch.dict(sys.modules, {"faster_whisper": modulo})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(transcrever_ficheiro.limpar_cache_de_modelos)

    def test_por_omissao_a_transcricao_usa_a_lingua_fixa_do_produto(self) -> None:
        # Criterio 6 da T8 (reversao): o A/B controlado deu o acerto de
        # intencao em portugues a descer com a deteccao ligada, por isso o
        # DEFAULT voltou a `language="pt"`. Trava-se aqui para ninguem religar
        # a deteccao no produto sem voltar a medir.
        transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu")
        self.assertEqual(len(self.modelos), 1)
        self.assertEqual(self.modelos[0].chamadas[0]["language"], LINGUA_FIXA_DO_PRODUTO)

    def test_lingua_fixa_none_liga_a_deteccao_e_pede_language_none(self) -> None:
        # O mecanismo nao foi apagado (D66/T9): sem isto o faster-whisper nem
        # chega a detetar lingua nenhuma e `all_language_probs` vem vazio.
        transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu", lingua_fixa=None)
        self.assertEqual(len(self.modelos), 1)
        self.assertIsNone(self.modelos[0].chamadas[0]["language"])

    def test_lingua_fixa_e_a_perna_de_controlo_do_ab_e_nao_o_default(self) -> None:
        # D66, ponto 7 + criterio 6: `lingua_fixa` e a perna de CONTROLO do
        # A/B e, desde a reversao, tambem o default do produto. As duas pernas
        # tem de chegar mesmo ao `transcribe()`, cada uma com o seu valor.
        transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu", lingua_fixa="pt")
        self.assertEqual(self.modelos[0].chamadas[0]["language"], "pt")
        self.assertEqual(
            transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu")["lingua_fixa"],
            LINGUA_FIXA_DO_PRODUTO,
        )
        self.assertEqual(
            transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu", lingua_fixa=None)[
                "lingua_fixa"
            ],
            None,
        )
        self.assertIsNone(self.modelos[-1].chamadas[0]["language"])

    def test_com_lingua_fixa_nao_ha_probabilidades_e_o_dict_di_lo(self) -> None:
        # Com `language="pt"` o faster-whisper nao devolve all_language_probs:
        # a corrida de controlo nao pode aparecer como se tivesse medido a
        # lingua. `lingua_terceira` tem de ser False — nao houve argmax livre.
        def sem_probabilidades(nome, device, compute_type, download_root):
            modelo = ModeloQueTranscreve(nome, device, compute_type, download_root)
            modelo.probabilidades = None
            self.modelos.append(modelo)
            return modelo

        sys.modules["faster_whisper"].WhisperModel = sem_probabilidades
        resultado = transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu", lingua_fixa="pt")
        self.assertEqual(resultado["lingua_fixa"], "pt")
        self.assertFalse(resultado["lingua_terceira"])
        # Sem deteccao nao ha hesitacao NENHUMA para reportar: `hesitou=True`
        # aqui daria a entender que uma deteccao correu e falhou, e nao correu
        # deteccao nenhuma. O dict diz `lingua_detetada=False` e o motivo
        # nomeia a lingua fixa — e isso que a coluna da evidencia escreve.
        self.assertFalse(resultado["lingua_detetada"])
        self.assertFalse(resultado["lingua_hesitou"])
        self.assertIn("language='pt' fixo", resultado["lingua_motivo"])

    def test_o_dict_traz_a_lingua_as_duas_probabilidades_e_a_hesitacao(self) -> None:
        resultado = transcrever_ficheiro.transcrever(
            Path("x.wav"), device="cpu", lingua_fixa=None
        )
        self.assertEqual(resultado["lingua"], "pt")
        self.assertAlmostEqual(resultado["prob_pt"], 0.96)
        self.assertAlmostEqual(resultado["prob_en"], 0.01)
        self.assertFalse(resultado["lingua_hesitou"])
        self.assertEqual(resultado["lingua_top1"], "pt")

    def test_uma_terceira_lingua_no_topo_nao_decide_a_lingua(self) -> None:
        self.modelos_probabilidades = [("es", 0.72), ("en", 0.18), ("pt", 0.05)]

        def com_espanhol(nome, device, compute_type, download_root):
            modelo = ModeloQueTranscreve(nome, device, compute_type, download_root)
            modelo.probabilidades = self.modelos_probabilidades
            self.modelos.append(modelo)
            return modelo

        sys.modules["faster_whisper"].WhisperModel = com_espanhol
        resultado = transcrever_ficheiro.transcrever(
            Path("x.wav"), device="cpu", lingua_fixa=None
        )
        self.assertEqual(resultado["lingua"], "en")
        self.assertEqual(resultado["lingua_top1"], "es")
        # D66, ponto 3: a lingua do PRODUTO nao veio do espanhol, mas o TEXTO
        # veio — e o dict tem de trazer a marca para quem escreve evidencia.
        self.assertTrue(resultado["lingua_terceira"])
        self.assertIn("lingua-terceira", resultado["lingua_motivo"])
        self.assertIn("descodificou", resultado["lingua_motivo"])
        self.assertNotIn("ignorado", resultado["lingua_motivo"])

    def test_o_fallback_tambem_deteta_a_lingua(self) -> None:
        # Se o modelo preferido rebentar, o fallback nao pode voltar a fixar
        # "pt": seria o unico caminho do produto sem deteccao (criterio 1).
        def preferido_parte(nome, device, compute_type, download_root):
            if nome == "medium":
                raise RuntimeError("sem VRAM")
            modelo = ModeloQueTranscreve(nome, device, compute_type, download_root)
            modelo.probabilidades = [("en", 0.91), ("pt", 0.03)]
            self.modelos.append(modelo)
            return modelo

        sys.modules["faster_whisper"].WhisperModel = preferido_parte
        resultado = transcrever_ficheiro.transcrever(
            Path("x.wav"), device="cpu", lingua_fixa=None
        )
        self.assertEqual(resultado["modelo"], "small")
        self.assertIsNone(self.modelos[0].chamadas[0]["language"])
        self.assertEqual(resultado["lingua"], "en")

    def test_a_deteccao_nao_chama_detect_language_a_parte(self) -> None:
        # O `detect_language` do duplo rebenta: se esta chamada passar, nao foi
        # chamado (S10 — uma so passagem pelo encoder).
        resultado = transcrever_ficheiro.transcrever(Path("x.wav"), device="cpu")
        self.assertEqual(resultado["texto"], "que horas sao")

    def test_um_info_sem_all_language_probs_nao_parte_a_transcricao(self) -> None:
        def sem_probabilidades(nome, device, compute_type, download_root):
            modelo = ModeloQueTranscreve(nome, device, compute_type, download_root)
            modelo.probabilidades = None
            self.modelos.append(modelo)
            return modelo

        sys.modules["faster_whisper"].WhisperModel = sem_probabilidades
        resultado = transcrever_ficheiro.transcrever(
            Path("x.wav"), device="cpu", lingua_fixa=None
        )
        self.assertEqual(resultado["texto"], "que horas sao")
        self.assertEqual(resultado["lingua"], "pt")
        self.assertTrue(resultado["lingua_hesitou"])


if __name__ == "__main__":
    unittest.main()
