r"""Testes do fim de turno (jarvis/fim_de_turno.py), unittest da biblioteca padrao.

O juiz e falso em quase todos: um numero fixo, um que bloqueia ate o teste o
soltar (para a thread do modelo) ou um que rebenta. So `TestModeloReal`
carrega o ficheiro do Smart Turn, e salta quando ele nao existe. Nenhum
teste abre o microfone ou toca som.

Corre com:

    .venv\Scripts\python -m unittest tests.test_fim_de_turno -v
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import fim_de_turno as ft  # noqa: E402
from jarvis.config import (  # noqa: E402
    FIM_DE_TURNO_MAXIMO_S,
    FIM_DE_TURNO_PADRAO,
    ConfigError,
    ConfigOuvido,
    carregar_config,
)
from jarvis.fim_de_turno import (  # noqa: E402
    METODO_SILENCIO,
    METODO_SMART_TURN,
    FimDeTurno,
    JuizSmartTurn,
    caracteristicas,
    criar_fim_de_turno,
)

CHUNK_S = ft.DURACAO_DO_CHUNK_S
#: Um chunk de 30 ms de "voz" (o fim de turno nao olha para o conteudo).
VOZ = b"\x10\x00" * 480


class JuizFixo:
    def __init__(self, probabilidade: float) -> None:
        self.valor = probabilidade
        self.audios: list[bytes] = []

    def probabilidade(self, pcm16: bytes) -> float:
        self.audios.append(pcm16)
        return self.valor


class JuizQueRebenta:
    def __init__(self) -> None:
        self.chamadas = 0

    def probabilidade(self, pcm16: bytes) -> float:
        self.chamadas += 1
        raise RuntimeError("onnx partido")


class JuizLento:
    """Espera `segundos` antes de responder (o tempo de inferencia)."""

    def __init__(self, probabilidade: float, segundos: float) -> None:
        self.valor = probabilidade
        self.segundos = segundos

    def probabilidade(self, pcm16: bytes) -> float:
        time.sleep(self.segundos)
        return self.valor


class JuizComTrinco:
    """Bloqueia cada pedido ate o teste o soltar, com o valor dado nesse momento."""

    def __init__(self) -> None:
        self.pedidos = threading.Semaphore(0)
        self._respostas: list[tuple[threading.Event, list[float]]] = []
        self._trinco = threading.Lock()
        self.feitos = 0

    def probabilidade(self, pcm16: bytes) -> float:
        solta, valor = threading.Event(), []
        with self._trinco:
            self._respostas.append((solta, valor))
            self.feitos += 1
        self.pedidos.release()
        solta.wait(5.0)
        return valor[0]

    def soltar(self, valor: float) -> None:
        with self._trinco:
            solta, caixa = self._respostas.pop(0)
        caixa.append(valor)
        solta.set()


class Frase:
    """Alimenta um FimDeTurno como o ouvido: voz (silencio 0) e silencio a crescer."""

    def __init__(self, fim: FimDeTurno) -> None:
        self.fim = fim
        self.silencio = 0.0
        self.audio = b""
        fim.reiniciar()

    def falar(self, segundos: float) -> str | None:
        for _ in range(round(segundos / CHUNK_S)):
            self.silencio = 0.0
            self.audio += VOZ
            motivo = self.fim.acabou(0.0, lambda: self.audio)
            if motivo is not None:
                return motivo
        return None

    def calar(self, segundos: float) -> tuple[float, str] | None:
        """Silencio ate `segundos`; devolve (silencio, motivo) do fim, ou None."""
        for _ in range(round(segundos / CHUNK_S)):
            self.silencio += CHUNK_S
            self.audio += b"\x00\x00" * 480
            motivo = self.fim.acabou(self.silencio, lambda: self.audio)
            if motivo is not None:
                return self.silencio, motivo
        return None


def esperar(condicao, limite_s: float = 5.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.005)
    return condicao()


class TestSilencioFixo(unittest.TestCase):
    def test_sem_modelo_fecha_aos_0_6_s(self) -> None:
        frase = Frase(FimDeTurno(None, escrever=lambda _t: None))
        frase.falar(1.0)
        silencio, motivo = frase.calar(3.0)
        self.assertAlmostEqual(silencio, 0.6, delta=CHUNK_S / 2)
        self.assertEqual(motivo, "fim da fala (VAD)")
        self.assertEqual(frase.fim.metodo, METODO_SILENCIO)

    def test_uma_pausa_mais_curta_nao_fecha(self) -> None:
        frase = Frase(FimDeTurno(None, escrever=lambda _t: None))
        frase.falar(0.5)
        self.assertIsNone(frase.calar(0.5))
        self.assertIsNone(frase.falar(0.5))


class TestSmartTurn(unittest.TestCase):
    def fim(self, juiz, **kwargs) -> FimDeTurno:
        kwargs.setdefault("em_fundo", False)
        return FimDeTurno(juiz, escrever=lambda _t: None, **kwargs)

    def test_acabada_fecha_aos_0_3_s(self) -> None:
        juiz = JuizFixo(0.9)
        frase = Frase(self.fim(juiz))
        frase.falar(1.0)
        silencio, motivo = frase.calar(3.0)
        self.assertAlmostEqual(silencio, 0.3, delta=CHUNK_S / 2)
        self.assertIn("Smart Turn 0.90", motivo)
        self.assertEqual(len(juiz.audios), 1, "um so pedido por silencio")

    def test_o_modelo_so_e_ouvido_depois_de_0_2_s_de_silencio(self) -> None:
        juiz = JuizFixo(0.9)
        frase = Frase(self.fim(juiz))
        frase.falar(1.0)
        frase.calar(0.18)
        self.assertEqual(juiz.audios, [])
        frase.calar(0.03)
        self.assertEqual(len(juiz.audios), 1)

    def test_inacabada_espera_ate_ao_maximo(self) -> None:
        frase = Frase(self.fim(JuizFixo(0.1)))
        frase.falar(1.0)
        silencio, motivo = frase.calar(5.0)
        self.assertAlmostEqual(silencio, FIM_DE_TURNO_MAXIMO_S, delta=CHUNK_S / 2)
        self.assertIn("espera maxima", motivo)
        self.assertIn("0.10", motivo)

    def test_o_maximo_e_configuravel(self) -> None:
        frase = Frase(self.fim(JuizFixo(0.1), maximo_s=1.0))
        frase.falar(1.0)
        silencio, _motivo = frase.calar(5.0)
        self.assertAlmostEqual(silencio, 1.0, delta=CHUNK_S)  # o primeiro chunk que chega a 1 s

    def test_uma_pausa_numa_frase_inacabada_nao_corta(self) -> None:
        # Procurar uma palavra a meio: 0,9 s cortava o silencio fixo, aqui nao.
        frase = Frase(self.fim(JuizFixo(0.1)))
        frase.falar(1.0)
        self.assertIsNone(frase.calar(0.9))
        self.assertIsNone(frase.falar(0.5))

    def test_a_fala_que_volta_invalida_o_veredicto(self) -> None:
        juiz = JuizFixo(0.1)
        frase = Frase(self.fim(juiz))
        frase.falar(1.0)
        frase.calar(0.25)  # pedido: inacabada
        frase.falar(0.3)
        juiz.valor = 0.95
        silencio, motivo = frase.calar(3.0)
        self.assertEqual(len(juiz.audios), 2, "um pedido novo no silencio seguinte")
        self.assertAlmostEqual(silencio, 0.3, delta=CHUNK_S / 2)
        self.assertIn("0.95", motivo)

    def test_o_audio_pedido_e_o_fim_da_frase_no_maximo_8_s(self) -> None:
        juiz = JuizFixo(0.9)
        frase = Frase(self.fim(juiz))
        frase.falar(10.0)
        frase.calar(0.21)  # o pedido sai neste chunk
        no_pedido = frase.audio
        frase.calar(0.3)
        self.assertEqual(juiz.audios, [no_pedido[-ft.AMOSTRAS_DA_JANELA * 2 :]])
        self.assertEqual(len(juiz.audios[0]), ft.AMOSTRAS_DA_JANELA * 2)

    def test_fala_curta_demais_fica_com_o_silencio_fixo(self) -> None:
        # Um estalido nao e um turno: o modelo nem e ouvido.
        juiz = JuizFixo(0.9)
        frase = Frase(self.fim(juiz))
        frase.falar(0.15)
        silencio, motivo = frase.calar(3.0)
        self.assertEqual(juiz.audios, [])
        self.assertAlmostEqual(silencio, 0.6, delta=CHUNK_S / 2)
        self.assertEqual(motivo, "fim da fala (VAD)")

    def test_a_voz_conta_por_frase(self) -> None:
        juiz = JuizFixo(0.9)
        fim = self.fim(juiz)
        Frase(fim).falar(1.0)
        frase = Frase(fim)  # reiniciar: a voz da frase anterior nao conta
        frase.falar(0.15)
        frase.calar(3.0)
        self.assertEqual(juiz.audios, [])

    def test_o_veredicto_so_vale_depois_da_inferencia(self) -> None:
        # Sem thread o veredicto conta a partir de 0,2 s + o tempo que o modelo levou.
        frase = Frase(self.fim(JuizLento(0.9, 0.2)))
        frase.falar(1.0)
        silencio, _motivo = frase.calar(3.0)
        self.assertGreaterEqual(silencio, 0.4 - CHUNK_S / 2)
        self.assertLess(silencio, 0.6)

    def test_esperas_incoerentes_sao_recusadas(self) -> None:
        with self.assertRaises(ValueError):
            FimDeTurno(None, maximo_s=0.25)


class TestModeloQueFalha(unittest.TestCase):
    def test_falha_fica_com_o_silencio_fixo_e_diz_uma_vez(self) -> None:
        linhas: list[str] = []
        juiz = JuizQueRebenta()
        fim = FimDeTurno(juiz, em_fundo=False, escrever=linhas.append)
        for _ in range(3):
            frase = Frase(fim)
            frase.falar(1.0)
            silencio, motivo = frase.calar(3.0)
            self.assertAlmostEqual(silencio, 0.6, delta=CHUNK_S / 2)
            self.assertEqual(motivo, "fim da fala (VAD)")
        self.assertEqual(juiz.chamadas, 1, "depois da falha o modelo nao volta a ser chamado")
        self.assertEqual(len(linhas), 1, linhas)
        self.assertIn("silencio fixo de 0.6 s", linhas[0])
        self.assertEqual(fim.metodo, METODO_SILENCIO)

    def test_falha_na_thread_tambem(self) -> None:
        linhas: list[str] = []
        fim = FimDeTurno(JuizQueRebenta(), escrever=linhas.append)
        self.addCleanup(fim.fechar)
        frase = Frase(fim)
        frase.falar(1.0)
        frase.calar(0.21)
        self.assertTrue(esperar(lambda: fim.metodo == METODO_SILENCIO))
        silencio, motivo = frase.calar(3.0)
        self.assertAlmostEqual(silencio, 0.6, delta=CHUNK_S / 2)
        self.assertEqual(len(linhas), 1)


class TestThreadDoModelo(unittest.TestCase):
    """Com `em_fundo`, a captura nunca espera pelo modelo; respostas velhas nao contam."""

    def setUp(self) -> None:
        self.juiz = JuizComTrinco()
        self.fim = FimDeTurno(self.juiz, escrever=lambda _t: None)
        self.addCleanup(self.fim.fechar)
        self.addCleanup(self._soltar_tudo)

    def _soltar_tudo(self) -> None:
        while self.juiz._respostas:  # noqa: SLF001 - nenhum pedido fica preso
            self.juiz.soltar(0.0)

    def pedido_feito(self) -> None:
        self.assertTrue(self.juiz.pedidos.acquire(timeout=5.0), "o pedido nao chegou ao modelo")

    def test_sem_resposta_so_fecha_pelo_maximo(self) -> None:
        frase = Frase(self.fim)
        frase.falar(1.0)
        inicio = time.perf_counter()
        silencio, motivo = frase.calar(5.0)
        self.assertLess(time.perf_counter() - inicio, 1.0, "a captura nunca espera pelo modelo")
        self.assertAlmostEqual(silencio, FIM_DE_TURNO_MAXIMO_S, delta=CHUNK_S / 2)
        self.assertIn("sem resposta do Smart Turn", motivo)

    def test_a_resposta_que_chega_fecha_o_turno(self) -> None:
        frase = Frase(self.fim)
        frase.falar(1.0)
        self.assertIsNone(frase.calar(0.21))
        self.pedido_feito()
        self.assertIsNone(frase.calar(0.09), "sem resposta ainda nao fecha")
        self.juiz.soltar(0.9)
        self.assertTrue(esperar(lambda: self.fim._veredicto is not None))  # noqa: SLF001
        silencio, motivo = frase.calar(0.03)
        self.assertAlmostEqual(silencio, 0.33, delta=CHUNK_S / 2)
        self.assertIn("0.90", motivo)

    def test_resposta_de_um_silencio_anterior_e_ignorada(self) -> None:
        frase = Frase(self.fim)
        frase.falar(1.0)
        frase.calar(0.21)
        self.pedido_feito()
        frase.falar(0.3)  # a fala voltou antes da resposta
        self.juiz.soltar(0.99)  # "acabada", mas do silencio velho
        self.assertTrue(esperar(lambda: self.juiz.feitos == 1 and not self.juiz._respostas))  # noqa: SLF001
        time.sleep(0.05)
        self.assertIsNone(frase.calar(0.27), "o veredicto velho nao fecha o silencio novo")
        self.pedido_feito()  # o silencio novo pede de novo
        self.juiz.soltar(0.1)
        silencio, motivo = frase.calar(5.0)
        self.assertAlmostEqual(silencio, FIM_DE_TURNO_MAXIMO_S, delta=CHUNK_S / 2)

    def test_resposta_da_frase_anterior_nao_fecha_a_seguinte(self) -> None:
        frase = Frase(self.fim)
        frase.falar(1.0)
        frase.calar(0.21)
        self.pedido_feito()
        seguinte = Frase(self.fim)  # o ouvido reinicia (frase acabou pela tecla, por exemplo)
        self.juiz.soltar(0.99)
        time.sleep(0.05)
        seguinte.falar(1.0)
        self.assertIsNone(seguinte.calar(0.2))
        self.assertIsNone(self.fim._veredicto)  # noqa: SLF001

    def test_fechar_para_a_thread(self) -> None:
        frase = Frase(self.fim)
        frase.falar(1.0)
        frase.calar(0.21)
        self.pedido_feito()
        self.juiz.soltar(0.5)
        self.fim.fechar()
        self.assertFalse(self.fim._fio.is_alive())  # noqa: SLF001


class JuizQueCarrega:
    def __init__(self, modelo: Path) -> None:
        self.modelo = modelo
        self.aquecido = False

    def probabilidade(self, pcm16: bytes) -> float:
        return 0.5

    def aquecer(self) -> float:
        self.aquecido = True
        return 1.0


class TestCriar(unittest.TestCase):
    def test_sem_ficheiro_fica_o_silencio_fixo_com_uma_linha(self) -> None:
        linhas: list[str] = []
        with tempfile.TemporaryDirectory() as pasta:
            fim = criar_fim_de_turno(METODO_SMART_TURN, escrever=linhas.append, modelo=Path(pasta) / "st.onnx")
        self.assertEqual(fim.metodo, METODO_SILENCIO)
        self.assertEqual(len(linhas), 1, linhas)
        self.assertIn("silencio fixo de 0.6 s", linhas[0])
        self.assertIn("em falta", linhas[0])
        self.assertNotIn(pasta, linhas[0], "o log nao leva caminhos da maquina")

    def test_modelo_que_nao_carrega_tambem(self) -> None:
        def rebenta(_modelo: Path):
            raise RuntimeError("ficheiro estragado")

        linhas: list[str] = []
        fim = criar_fim_de_turno(METODO_SMART_TURN, escrever=linhas.append, criar_juiz=rebenta)
        self.assertEqual(fim.metodo, METODO_SILENCIO)
        self.assertEqual(len(linhas), 1)
        self.assertIn("por carregar", linhas[0])

    def test_silencio_pedido_no_config(self) -> None:
        linhas: list[str] = []
        chamado: list[Path] = []
        fim = criar_fim_de_turno(METODO_SILENCIO, escrever=linhas.append, criar_juiz=chamado.append)
        self.assertEqual(fim.metodo, METODO_SILENCIO)
        self.assertEqual(chamado, [], "com silencio o modelo nem e carregado")
        self.assertEqual(len(linhas), 1)

    def test_com_modelo_aquece_e_usa_o_maximo(self) -> None:
        linhas: list[str] = []
        fim = criar_fim_de_turno(METODO_SMART_TURN, 2.0, escrever=linhas.append, criar_juiz=JuizQueCarrega)
        self.addCleanup(fim.fechar)
        self.assertEqual(fim.metodo, METODO_SMART_TURN)
        self.assertTrue(fim.juiz.aquecido)
        self.assertEqual(fim.maximo_s, 2.0)
        self.assertTrue(fim.em_fundo)
        self.assertEqual(len(linhas), 1)
        self.assertIn("Smart Turn", linhas[0])

    def test_metodo_desconhecido(self) -> None:
        with self.assertRaises(ValueError):
            criar_fim_de_turno("adivinhar")

    def test_juiz_real_sem_ficheiro(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, self.assertRaises(FileNotFoundError):
            JuizSmartTurn(Path(pasta) / "nao-existe.onnx")


class TestCaracteristicas(unittest.TestCase):
    """As entradas do modelo: log-mel do Whisper sobre os ultimos 8 s, zeros antes."""

    @staticmethod
    def pcm(segundos: float, semente: int) -> bytes:
        import numpy as np

        n = int(segundos * ft.TAXA)
        return (np.random.default_rng(semente).normal(0, 3000, n)).astype("<i2").tobytes()

    def test_forma_e_tipo(self) -> None:
        entrada = caracteristicas(self.pcm(1.0, 1))
        self.assertEqual(entrada.shape, (1, 80, 800))
        self.assertEqual(str(entrada.dtype), "float32")

    def test_audio_curto_leva_zeros_antes(self) -> None:
        import numpy as np

        audio = self.pcm(2.0, 2)
        com_zeros = b"\x00\x00" * (3 * ft.TAXA) + audio
        np.testing.assert_array_equal(caracteristicas(audio), caracteristicas(com_zeros))

    def test_so_contam_os_ultimos_8_s(self) -> None:
        import numpy as np

        oito = self.pcm(8.0, 3)
        mais = self.pcm(2.0, 4) + oito
        np.testing.assert_array_equal(caracteristicas(oito), caracteristicas(mais))

    def test_o_fim_do_audio_fica_no_fim_das_caracteristicas(self) -> None:
        import numpy as np

        entrada = caracteristicas(self.pcm(1.0, 5))[0]
        # Os primeiros 7 s sao zeros (o mesmo valor em todos os frames); o ultimo segundo nao.
        self.assertTrue(np.allclose(entrada[:, :600], entrada[:, :1]))
        self.assertGreater(float(entrada[:, 700:].std()), 0.05)


class TestConfig(unittest.TestCase):
    def escrever(self, pasta: str, extra: str) -> Path:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            f'[[projetos]]\nnome = "p"\ncaminho = "{Path(pasta).as_posix()}"\n\n' + extra,
            encoding="utf-8",
        )
        return caminho

    def test_por_omissao(self) -> None:
        self.assertEqual(ConfigOuvido().fim_de_turno, FIM_DE_TURNO_PADRAO)
        self.assertEqual(ConfigOuvido().fim_de_turno_maximo_s, 1.5)

    def test_o_padrao_segue_a_medicao(self) -> None:
        # Nas gravacoes do Sponsor o Smart Turn empatou nos cortes a meio (2 contra
        # 2): a regra so o liga com MENOS cortes, por isso fica o silencio fixo.
        self.assertEqual(FIM_DE_TURNO_PADRAO, METODO_SILENCIO)

    def test_valores_validos(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            config = carregar_config(
                self.escrever(pasta, '[ouvido]\nfim_de_turno = "smart-turn"\nfim_de_turno_maximo_s = 2\n')
            )
        self.assertEqual(config.ouvido.fim_de_turno, METODO_SMART_TURN)
        self.assertEqual(config.ouvido.fim_de_turno_maximo_s, 2.0)

    def test_valores_invalidos_sao_recusados(self) -> None:
        for extra in (
            '[ouvido]\nfim_de_turno = "adivinhar"\n',
            "[ouvido]\nfim_de_turno = true\n",
            "[ouvido]\nfim_de_turno_maximo_s = 0.3\n",
            "[ouvido]\nfim_de_turno_maximo_s = 9\n",
            '[ouvido]\nfim_de_turno_maximo_s = "1.5"\n',
            "[ouvido]\nfim_de_turno_maximo_s = true\n",
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as pasta:
                with self.assertRaises(ConfigError):
                    carregar_config(self.escrever(pasta, extra))


class TestAutoteste(unittest.TestCase):
    def test_autoteste_passa_sem_som(self) -> None:
        import contextlib
        import io

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = ft.main(["--autoteste"])
        self.assertEqual(codigo, 0, saida.getvalue())


class TestModeloReal(unittest.TestCase):
    """O ficheiro descarregado carrega e responde; salta sem ele."""

    def setUp(self) -> None:
        if not ft.MODELO_SMART_TURN.is_file():
            self.skipTest("models/smart-turn/ em falta")

    def test_carrega_e_da_uma_probabilidade(self) -> None:
        juiz = JuizSmartTurn()
        self.assertGreater(juiz.aquecer(), 0.0)
        probabilidade = juiz.probabilidade(b"\x00\x00" * ft.TAXA)
        self.assertGreaterEqual(probabilidade, 0.0)
        self.assertLessEqual(probabilidade, 1.0)


if __name__ == "__main__":
    unittest.main()
