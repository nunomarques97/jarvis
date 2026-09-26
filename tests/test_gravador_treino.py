r"""Testes do gravador de treino: guiao, piloto, verificacao de fala e pasta propria.

Sem microfone, sem som e sem modelos: a captura e a CapturaFalsa do gravador,
as respostas do Sponsor sao roteirizadas e o audio e gerado (silencio, quase
silencio, tom puro e um sinal com a forma da fala) numa pasta temporaria.

Corre com:

    .venv\Scripts\python -m unittest tests.test_gravador_treino -v
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts import avaliar_voz  # noqa: E402

gravar = avaliar_voz.gravar
PROJETOS = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois"}
GUIAO_DE_TREINO = gravar.GUIOES_DE_TREINO["en"]


def _fala(segundos: float = 1.0) -> bytes:
    return gravar.fala_sintetica_pcm16(segundos)


def _quase_silencio(segundos: float = 1.0) -> bytes:
    # A mesma forma da fala mas tao baixa como ruido de fundo: o pico nao e zero.
    return gravar.fala_sintetica_pcm16(segundos, amplitude=150)


def _zeros(segundos: float = 1.0) -> bytes:
    return b"\x00\x00" * int(16_000 * segundos)


class CapturaEmSequencia(gravar.CapturaFalsa):
    """CapturaFalsa que devolve um audio diferente por gravacao (o ultimo repete-se)."""

    def __init__(self, audios: list[bytes]) -> None:
        super().__init__(audios[0])
        self.audios = list(audios)

    def gravar(self, parar, maximo_s):
        self.pcm = self.audios[min(self.gravacoes, len(self.audios) - 1)]
        self.segundos_reais = [(len(self.pcm) // 2) / 16_000]
        return super().gravar(parar, maximo_s)


def _treino(n: int = 5) -> list:
    return gravar.ler_guiao(GUIAO_DE_TREINO, "en", treino=True)[:n]


# --- Guiao de treino ------------------------------------------------------------------


class TestGuiaoDeTreino(unittest.TestCase):
    def setUp(self) -> None:
        self.treino = gravar.ler_guiao(GUIAO_DE_TREINO, "en", treino=True)
        self.avaliacao = gravar.ler_guiao(gravar.GUIOES["en"], "en")

    def test_le_o_guiao_com_ids_proprios(self) -> None:
        self.assertGreaterEqual(len(self.treino), 40)
        for frase in self.treino:
            with self.subTest(id=frase.id):
                self.assertRegex(frase.id, r"^en-t\d{2}$")
                self.assertIn(frase.intencao, gravar.INTENCOES)
        self.assertEqual(gravar.verificar_guiao_de_treino(self.treino, self.avaliacao), [])

    def test_ids_de_treino_e_de_avaliacao_nao_se_misturam(self) -> None:
        self.assertFalse({f.id for f in self.treino} & {f.id for f in self.avaliacao})
        with self.assertRaises(gravar.GuiaoError):
            gravar.ler_guiao(GUIAO_DE_TREINO, "en")  # ids en-tNN recusados no guiao de avaliacao
        with self.assertRaises(gravar.GuiaoError):
            gravar.ler_guiao(gravar.GUIOES["en"], "en", treino=True)

    def test_vocabulario_dos_comandos(self) -> None:
        textos = " | ".join(f.frase.lower() for f in self.treino)
        for termo in ("add tests", "cancel", "claude", "<projeto-1>", "<projeto-2>", "hey jarvis"):
            with self.subTest(termo=termo):
                self.assertIn(termo, textos)
        confirmacoes = {f.intencao for f in self.treino if f.caso == "confirmacao"}
        self.assertTrue({"confirmar", "cancelar", "corrigir"} <= confirmacoes)
        self.assertGreaterEqual(sum(f.intencao == "ditar_prompt" for f in self.treino), 10)

    def test_nenhuma_frase_repete_a_avaliacao(self) -> None:
        de_avaliacao = {gravar.normalizar(f.frase) for f in self.avaliacao}
        normalizadas = [gravar.normalizar(f.frase) for f in self.treino]
        self.assertFalse(set(normalizadas) & de_avaliacao)
        self.assertEqual(len(normalizadas), len(set(normalizadas)))

    def test_verificacao_apanha_repeticoes_e_vocabulario_em_falta(self) -> None:
        repetida = gravar.FraseDoGuiao("en-t99", "confirmacao", "Cancel!", "cancelar", None, False)
        problemas = gravar.verificar_guiao_de_treino([repetida], self.avaliacao)
        self.assertTrue(any("en-42" in p for p in problemas), problemas)
        self.assertTrue(any("add tests" in p for p in problemas), problemas)
        self.assertTrue(any("ditar_prompt" in p for p in problemas), problemas)

    def test_sem_nomes_nem_caminhos_reais(self) -> None:
        texto = GUIAO_DE_TREINO.read_text(encoding="utf-8")
        for proibido in (":\\", "/Users/", "\\Users\\", "Desktop", "Repositorios", "C:/"):
            self.assertNotIn(proibido, texto)
        for frase in self.treino:
            for marcador in gravar.PADRAO_MARCADOR.findall(frase.frase):
                self.assertIn(marcador, gravar.MARCADORES_DE_PROJETO)
        # Os nomes reais dos projetos (config.toml local, ignorado) nunca entram no guiao.
        config_local = RAIZ / "config.toml"
        if config_local.is_file():
            from jarvis.config import ConfigError, carregar_config

            try:
                nomes = [p.nome for p in carregar_config(config_local, validar_caminhos=False).projetos]
            except ConfigError:
                nomes = []
            palavras_do_guiao = set(gravar.normalizar(texto).split())
            ativacao = set(gravar.PALAVRA_DE_ATIVACAO["en"].split())
            for nome in nomes:
                chave = gravar.normalizar(nome)
                if not chave or set(chave.split()) <= ativacao:
                    continue
                self.assertNotIn(chave, gravar.normalizar(texto), "nome real de projeto no guiao versionado")
                if " " not in chave:
                    self.assertNotIn(chave, palavras_do_guiao)


# --- Verificacao de fala real ---------------------------------------------------------


class TestAnaliseDaFala(unittest.TestCase):
    def test_fala_passa(self) -> None:
        analise = gravar.analisar_fala(_fala())
        self.assertIsNone(analise.motivo())
        self.assertGreaterEqual(analise.fala_s, gravar.FALA_MINIMA_S)

    def test_silencio_digital_recusado(self) -> None:
        self.assertIsNotNone(gravar.analisar_fala(_zeros()).motivo())
        self.assertIsNotNone(gravar.analisar_fala(b"").motivo())

    def test_quase_silencio_com_pico_recusado(self) -> None:
        pcm = _quase_silencio()
        self.assertGreater(gravar.pico_pcm16(pcm), 0)  # nao basta um pico diferente de zero
        motivo = gravar.analisar_fala(pcm).motivo()
        self.assertIsNotNone(motivo)
        self.assertIn("quase silencio", motivo)

    def test_tom_forte_sem_fala_recusado(self) -> None:
        pcm = gravar.tom_pcm16(2.0)
        analise = gravar.analisar_fala(pcm)
        self.assertGreater(analise.nivel, gravar.NIVEL_MINIMO_DA_FALA)
        self.assertIn("sem fala", analise.motivo())

    def test_nivel_exigido_mesmo_quando_o_vad_diz_que_e_fala(self) -> None:
        analise = gravar.analisar_fala(_quase_silencio(), e_fala=lambda _trama: True)
        self.assertIn("quase silencio", analise.motivo())
        self.assertIsNone(gravar.analisar_fala(_fala(), e_fala=lambda _trama: True).motivo())


# --- Piloto e gravacao do treino --------------------------------------------------------


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name).resolve()
        self.pasta = gravar.pasta_de_gravacoes(None, self.raiz)
        self.pasta_treino = self.pasta / "treino-en"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _gravar(self, respostas, frases=None, captura=None, **kwargs):
        captura = gravar.CapturaFalsa(_fala()) if captura is None else captura
        self.captura = captura
        saida = io.StringIO()
        kwargs.setdefault("tocar_sinal", self._sem_som)
        with contextlib.redirect_stdout(saida):
            resumo = gravar.gravar_guiao(
                "en", _treino() if frases is None else frases, PROJETOS, self.pasta, captura,
                entrada=gravar.entrada_roteirizada(respostas), raiz=self.raiz, treino=True, **kwargs,
            )
        self.saida = saida.getvalue()
        return resumo

    @staticmethod
    def _sem_som() -> None:
        raise AssertionError("tocou som sem --com-som")

    def _wavs(self) -> set[str]:
        return {p.name for p in self.pasta_treino.glob("*.wav")} if self.pasta_treino.is_dir() else set()


class TestPiloto(_Base):
    def test_piloto_passa_para_e_depois_de_confirmado_grava_o_resto(self) -> None:
        resumo = self._gravar(["", ""] * 3 + ["sim"] + ["", ""] * 2)
        self.assertEqual(resumo.piloto, "aprovado")
        self.assertEqual(resumo.gravadas, ["en-t01", "en-t02", "en-t03", "en-t04", "en-t05"])
        self.assertIn("PILOTO (3 frases)", self.saida)
        manifesto = gravar.ler_manifesto(self.pasta_treino)
        self.assertTrue(manifesto["piloto"]["aprovado"])
        self.assertEqual(manifesto["piloto"]["ids"], ["en-t01", "en-t02", "en-t03"])
        self.assertEqual(manifesto["guiao"], GUIAO_DE_TREINO.name)
        self.assertGreaterEqual(manifesto["gravacoes"]["en-t01"]["fala_s"], gravar.FALA_MINIMA_S)

    def test_piloto_sem_confirmacao_nao_grava_o_resto_e_retoma_na_confirmacao(self) -> None:
        resumo = self._gravar(["", ""] * 3 + ["nao"] + ["", ""] * 2)
        self.assertEqual(resumo.piloto, "por_confirmar")
        self.assertEqual(resumo.gravadas, ["en-t01", "en-t02", "en-t03"])
        self.assertEqual(self._wavs(), {"en-t01.wav", "en-t02.wav", "en-t03.wav"})
        self.assertNotIn("piloto", gravar.ler_manifesto(self.pasta_treino))
        # A sessao seguinte nao repete o piloto: pede logo a confirmacao.
        resumo = self._gravar(["sim"] + ["", ""] * 2)
        self.assertEqual(resumo.ja_existiam, ["en-t01", "en-t02", "en-t03"])
        self.assertEqual((resumo.piloto, resumo.gravadas), ("aprovado", ["en-t04", "en-t05"]))
        self.assertEqual(self.captura.gravacoes, 2)
        # E a seguinte ja nao pergunta nada sobre o piloto.
        resumo = self._gravar(["q"])
        self.assertEqual((resumo.piloto, resumo.gravadas), ("ja_aprovado", []))
        self.assertNotIn("PILOTO", self.saida)

    def test_piloto_em_silencio_bloqueia_o_resto(self) -> None:
        resumo = self._gravar([""] * 20, captura=gravar.CapturaFalsa(_zeros()))
        self.assertEqual((resumo.piloto, resumo.gravadas), ("sem_fala", []))
        self.assertEqual(self.captura.gravacoes, 1)  # parou na primeira frase
        self.assertIn("--verificar", self.saida)
        self.assertIn("PILOTO PARADO", self.saida)
        self.assertNotIn("en-t02", self.saida)
        self.assertEqual(self._wavs(), set())

    def test_piloto_quase_em_silencio_recusado(self) -> None:
        resumo = self._gravar([""] * 20, captura=gravar.CapturaFalsa(_quase_silencio()))
        self.assertEqual((resumo.piloto, resumo.gravadas, resumo.recusadas), ("sem_fala", [], 1))
        self.assertIn("quase silencio", self.saida)
        self.assertIn("--verificar", self.saida)
        self.assertEqual(self._wavs(), set())

    def test_piloto_com_tom_sem_fala_recusado(self) -> None:
        resumo = self._gravar([""] * 20, captura=gravar.CapturaFalsa(gravar.tom_pcm16(1.0)))
        self.assertEqual((resumo.piloto, resumo.gravadas), ("sem_fala", []))
        self.assertIn("sem fala reconhecivel", self.saida)

    def test_silencio_a_meio_do_piloto_para_sem_pedir_o_resto(self) -> None:
        captura = CapturaEmSequencia([_fala(), _quase_silencio(), _fala()])
        resumo = self._gravar([""] * 20, captura=captura)
        self.assertEqual((resumo.piloto, resumo.gravadas), ("sem_fala", ["en-t01"]))
        self.assertEqual(captura.gravacoes, 2)
        self.assertEqual(self._wavs(), {"en-t01.wav"})
        # Corrigido o microfone, retoma no en-t02 e continua o piloto.
        resumo = self._gravar(["", ""] * 2 + ["sim"] + ["", ""] * 2)
        self.assertEqual(resumo.gravadas, ["en-t02", "en-t03", "en-t04", "en-t05"])

    def test_frases_do_piloto_nao_se_saltam(self) -> None:
        resumo = self._gravar(["s", "", "", "q"])
        self.assertEqual(resumo.saltadas, [])
        self.assertEqual(resumo.gravadas, ["en-t01"])
        self.assertTrue(resumo.interrompido)
        self.assertIn("nao se saltam", self.saida)

    def test_piloto_retoma_onde_parou(self) -> None:
        self.assertEqual(self._gravar(["", "", "q"]).gravadas, ["en-t01"])
        resumo = self._gravar(["", "", "q"])
        self.assertEqual((resumo.ja_existiam, resumo.gravadas), (["en-t01"], ["en-t02"]))
        self.assertIsNone(resumo.piloto)

    def test_wav_do_piloto_estragado_e_medido_de_novo_antes_da_confirmacao(self) -> None:
        self._gravar(["", ""] * 3 + ["nao"])
        # Um WAV do piloto trocado por quase silencio com a mesma duracao.
        gravar.escrever_wav_pcm16(self.pasta_treino / "en-t02.wav", _quase_silencio(), 16_000)
        resumo = self._gravar(["sim"] + [""] * 10)
        self.assertEqual((resumo.piloto, resumo.gravadas), ("sem_fala", []))
        self.assertIn("--refazer en-t02", self.saida)

    def test_depois_do_piloto_uma_captura_sem_fala_repete_a_frase(self) -> None:
        self._gravar(["", ""] * 3 + ["sim", "q"])
        captura = CapturaEmSequencia([_quase_silencio(), _fala()])
        resumo = self._gravar([""] * 4 + ["q"], captura=captura)
        self.assertEqual((resumo.piloto, resumo.recusadas, resumo.gravadas), ("ja_aprovado", 1, ["en-t04"]))
        self.assertIn("a frase repete-se", self.saida)


class TestPastaESom(_Base):
    def test_pasta_propria_separada_da_avaliacao(self) -> None:
        self.assertEqual(gravar.nome_da_subpasta("en", treino=True), "treino-en")
        self.assertEqual(gravar.nome_da_subpasta("en"), "en")
        self._gravar(["", ""] * 3 + ["sim"] + ["", ""] * 2)
        self.assertEqual(sorted(p.name for p in self.pasta.iterdir()), ["treino-en"])
        self.assertFalse((self.pasta / "en").exists())
        # A avaliacao na mesma pasta nao ve nenhuma gravacao de treino.
        avaliacao = gravar.ler_guiao(gravar.GUIOES["en"], "en")[:2]
        with contextlib.redirect_stdout(io.StringIO()):
            resumo = gravar.gravar_guiao(
                "en", avaliacao, PROJETOS, self.pasta, gravar.CapturaFalsa(),
                entrada=gravar.entrada_roteirizada([""] * 4), raiz=self.raiz,
            )
        self.assertEqual((resumo.ja_existiam, resumo.gravadas), ([], ["en-01", "en-02"]))
        self.assertEqual({p.name for p in (self.pasta / "en").glob("*.wav")}, {"en-01.wav", "en-02.wav"})
        manifesto_treino = json.loads((self.pasta_treino / "manifesto.json").read_text(encoding="utf-8"))
        self.assertFalse(any(not i.startswith("en-t") for i in manifesto_treino["gravacoes"]))
        self.assertNotIn(str(self.raiz), json.dumps(manifesto_treino))

    def test_sem_com_som_nunca_toca_e_com_com_som_toca_uma_vez_por_gravacao(self) -> None:
        self._gravar(["", ""] * 3 + ["nao"])  # _sem_som rebenta se tocar
        sinais: list[int] = []
        self._gravar(["sim"] + ["", ""] * 2, com_som=True, tocar_sinal=lambda: sinais.append(1))
        self.assertEqual(sinais, [1, 1])

    def test_nunca_apaga_ficheiros(self) -> None:
        self._gravar(["", ""] * 3 + ["sim"] + ["", ""] * 2)
        antes = self._wavs()
        resumo = self._gravar(["", ""] * 2, refazer=["en-t01", "en-t04"])
        depois = self._wavs()
        self.assertEqual(resumo.gravadas, ["en-t01", "en-t04"])
        self.assertLessEqual(antes, depois)
        self.assertEqual(len([n for n in depois if ".anterior-" in n]), 2)


class TestLinhaDeComandos(unittest.TestCase):
    def test_treino_so_em_linguas_com_guiao_de_treino(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as erro, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gravar.main(["--lingua", "pt", "--treino"]), 2)
        self.assertIn("treino", erro.getvalue())

    def test_pasta_fora_de_recordings_recusada_no_treino(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gravar.main(["--lingua", "en", "--treino", "--pasta", "../fora"]), 2)

    def _main_com_resumo(self, resumo, argv):
        chamadas: list[dict] = []

        def gravar_guiao_falso(lingua, frases, projetos, pasta, captura, **kwargs):
            chamadas.append({"lingua": lingua, "ids": [f.id for f in frases], "pasta": pasta, **kwargs})
            return resumo

        medir_voz = gravar._carregar_modulo_irmao("medir_voz")
        config = mock.Mock(microfone="", projetos=(mock.Mock(nome="exemplo-um"), mock.Mock(nome="exemplo-dois")))
        with mock.patch.object(gravar, "gravar_guiao", gravar_guiao_falso), \
                mock.patch.object(gravar, "CapturaPyAudio", lambda _nome: gravar.CapturaFalsa()), \
                mock.patch.object(medir_voz, "carregar_config_para_arnes", lambda: (config, "teste")), \
                contextlib.redirect_stdout(io.StringIO()):
            codigo = gravar.main(argv)
        return codigo, chamadas

    def test_treino_passa_o_guiao_de_treino_e_a_flag(self) -> None:
        codigo, chamadas = self._main_com_resumo(
            gravar.ResumoDaGravacao([], [], [], False, piloto="aprovado"), ["--lingua", "en", "--treino"]
        )
        self.assertEqual(codigo, 0)
        self.assertTrue(chamadas[0]["treino"])
        self.assertFalse(chamadas[0]["com_som"])
        self.assertTrue(all(i.startswith("en-t") for i in chamadas[0]["ids"]))
        self.assertEqual(chamadas[0]["pasta"], RAIZ / "recordings")

    def test_piloto_sem_fala_sai_com_erro(self) -> None:
        codigo, _ = self._main_com_resumo(
            gravar.ResumoDaGravacao([], [], [], False, piloto="sem_fala"), ["--lingua", "en", "--treino"]
        )
        self.assertEqual(codigo, 1)

    def test_refazer_so_aceita_ids_do_guiao_de_treino(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as erro:
            codigo, chamadas = self._main_com_resumo(None, ["--lingua", "en", "--treino", "--refazer", "en-01"])
        self.assertEqual((codigo, chamadas), (2, []))
        self.assertIn("en-01", erro.getvalue())

    def test_audio_sintetico_de_fala_nao_e_voz_gravada(self) -> None:
        # O sinal dos testes e gerado; confirma que e PCM16 mono de 16 kHz com a duracao pedida.
        pcm = _fala(0.5)
        self.assertEqual(len(pcm), 16_000)
        self.assertEqual(len(struct.unpack(f"<{len(pcm) // 2}h", pcm)), 8_000)


class TestFicheirosDoTreinoIgnorados(unittest.TestCase):
    def test_gravacoes_de_treino_ignoradas(self) -> None:
        caminhos = ("recordings/treino-en/en-t01.wav", "recordings/treino-en/manifesto.json")
        try:
            saida = subprocess.run(
                ["git", "check-ignore", "--no-index", *caminhos],
                cwd=RAIZ, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as erro:
            self.skipTest(f"git indisponivel: {erro}")
        self.assertEqual(sorted(saida.stdout.split()), sorted(caminhos))


if __name__ == "__main__":
    unittest.main()
