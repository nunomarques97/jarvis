r"""Testes do processo de adaptacao ao sotaque (scripts/adaptar_sotaque.py).

Sem microfone, sem som e sem modelos: as gravacoes sao WAVs sinteticos numa
pasta temporaria com o manifesto do gravador, o motor e um transcritor falso
que devolve texto roteirizado por gravacao e o interprete e uma funcao pura.

Corre com:

    .venv\Scripts\python -m unittest tests.test_adaptar_sotaque -v
"""

from __future__ import annotations

import contextlib
import io
import json
import random
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.adaptacao import CAMINHO_DO_LEXICO, Lexico  # noqa: E402
from jarvis.audio_util import escrever_wav_pcm16  # noqa: E402
from jarvis.config import Config, Projeto  # noqa: E402
from scripts import adaptar_sotaque as adaptar  # noqa: E402

avaliar = adaptar.avaliar
gravar = adaptar.gravar
medir = adaptar.medir

PROJETOS = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois"}
PASTA_DE_EVIDENCIA = RAIZ / "docs" / "forja" / "evidence"


def _config() -> Config:
    return Config(
        microfone="",
        projetos=(Projeto("exemplo-um", RAIZ / "exemplo-um"), Projeto("exemplo-dois", RAIZ / "exemplo-dois")),
    )


def _pcm_unico(indice: int, segundos: float = 0.5) -> bytes:
    """Audio sintetico diferente por gravacao (onda quadrada), sem troços de zeros."""
    amostras = int(16_000 * segundos) // 2
    return struct.pack("<2h", 1000 + indice, -1000 - indice) * amostras


def _marca(id_: str) -> str:
    """A palavra "mal ouvida" que o transcritor falso poe numa gravacao."""
    return "zz" + id_.replace("-", "")


def _escrever_pasta(pasta_lingua: Path, frases, inicio: int) -> dict[bytes, str]:
    """WAVs sinteticos + manifesto como os do gravador. Devolve pcm -> id."""
    pasta_lingua.mkdir(parents=True, exist_ok=True)
    por_pcm: dict[bytes, str] = {}
    gravacoes = {}
    for indice, frase in enumerate(frases, start=inicio):
        pcm = _pcm_unico(indice)
        escrever_wav_pcm16(pasta_lingua / f"{frase.id}.wav", pcm, 16_000)
        gravacoes[frase.id] = {
            "ficheiro": f"{frase.id}.wav",
            "origem": gravar.ORIGEM_SINTETICA,
            "duracao_s": (len(pcm) // 2) / 16_000,
        }
        por_pcm[pcm] = frase.id
    gravar.escrever_manifesto(pasta_lingua, {"projetos": PROJETOS, "gravacoes": gravacoes})
    return por_pcm


class TranscritorFalso:
    """Sem reforco troca a primeira palavra pela marca da gravacao; com reforco acerta.

    Guarda os ids que transcreveu antes de o lexico ser escrito, para provar
    que o lado do teste nunca alimenta a aprendizagem.
    """

    nome = "falso"

    def __init__(self, referencias: dict[bytes, tuple[str, str]], latencia_base: float, latencia_reforco: float,
                 reforco_disponivel: bool = True) -> None:
        self.referencias = referencias
        self.latencias = {False: latencia_base, True: latencia_reforco}
        self.reforco_disponivel = reforco_disponivel
        self.latencia_carregamento_ms = 1.0
        self.pedidos: list[tuple[str, bool]] = []
        self.libertado = False

    def carregar(self) -> None:
        pass

    def transcrever(self, pcm: bytes, reforco: bool) -> tuple[str, float]:
        id_, referencia = self.referencias[pcm]
        self.pedidos.append((id_, reforco))
        if reforco:
            return referencia, self.latencias[True]
        palavras = referencia.split()
        return " ".join([_marca(id_)] + palavras[1:]), self.latencias[False]

    def libertar(self) -> None:
        self.libertado = True


def _interpretar(texto: str, _config: Config) -> str:
    return "errada" if "zz" in texto else "certa"


class _ComGravacoes(unittest.TestCase):
    """Uma pasta recordings/ temporaria com parte do guiao de avaliacao e de treino."""

    N_TREINO_EXTRA = 4

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name).resolve()
        self.pasta = gravar.pasta_de_gravacoes(None, self.raiz)
        # Um pouco de cada caso: a divisao e estratificada.
        frases = gravar.ler_guiao(gravar.GUIOES["en"], "en")
        por_caso: dict[str, list] = {}
        for frase in frases:
            por_caso.setdefault(frase.caso, []).append(frase)
        self.frases = [f for lista in por_caso.values() for f in lista[:4]]
        treino = gravar.ler_guiao(gravar.GUIOES_DE_TREINO["en"], "en", treino=True)[: self.N_TREINO_EXTRA]
        pcm_para_id = _escrever_pasta(self.pasta / "en", self.frases, 0)
        pcm_para_id |= _escrever_pasta(self.pasta / gravar.nome_da_subpasta("en", treino=True), treino, 500)
        self.avaliacao = avaliar.carregar_gravacoes(self.pasta, "en")
        self.treino_extra = avaliar.carregar_gravacoes(self.pasta, "en", treino=True)
        todas = self.avaliacao.gravacoes + self.treino_extra.gravacoes
        self.assertEqual(len(self.avaliacao.gravacoes), len(self.frases))
        self.assertEqual(len(self.treino_extra.gravacoes), self.N_TREINO_EXTRA)
        referencia_por_id = {g.frase.id: g.referencia for g in todas}
        self.referencias = {pcm: (id_, referencia_por_id[id_]) for pcm, id_ in pcm_para_id.items()}
        self.lexico = self.raiz / "models" / "adaptacao" / "lexico-en.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _relatorio(self) -> adaptar.Relatorio:
        return adaptar.Relatorio(
            semente=adaptar.SEMENTE_PADRAO, fracao_treino=0.5, bonus=1.5, origem_config="teste",
            frases_reforcadas=10, projetos_reforcados=2,
        )

    def _correr(self, transcritor, medir_teste: bool = True, treino_extra=True) -> adaptar.Relatorio:
        return adaptar.correr(
            transcritor, self.avaliacao, self.treino_extra if treino_extra else None, _config(),
            self._relatorio(), self.lexico, medir_teste, interpretar=_interpretar,
        )


# --- Divisao -------------------------------------------------------------------------


class TestDivisao(unittest.TestCase):
    ITENS = [(f"en-{i:02d}", caso) for i, caso in enumerate("aaaaaaabbbbbcccclllllllkkk", start=1)]

    def test_deterministica_seja_qual_for_a_ordem(self) -> None:
        primeira = adaptar.dividir(self.ITENS, 0.5, 1790)
        baralhados = list(self.ITENS)
        random.Random(7).shuffle(baralhados)
        self.assertEqual(adaptar.dividir(baralhados, 0.5, 1790), primeira)
        self.assertEqual(adaptar.dividir(self.ITENS, 0.5, 1790), primeira)

    def test_disjunta_e_completa(self) -> None:
        for semente in (1, 1790, 99999):
            with self.subTest(semente=semente):
                divisao = adaptar.dividir(self.ITENS, 0.5, semente)
                self.assertFalse(set(divisao.treino) & set(divisao.teste))
                self.assertEqual(set(divisao.treino) | set(divisao.teste), {i for i, _ in self.ITENS})

    def test_estratificada_pelo_caso(self) -> None:
        caso_de = dict(self.ITENS)
        divisao = adaptar.dividir(self.ITENS, 0.5, 1790)
        for caso in set(caso_de.values()):
            n = sum(1 for c in caso_de.values() if c == caso)
            no_treino = sum(1 for i in divisao.treino if caso_de[i] == caso)
            no_teste = sum(1 for i in divisao.teste if caso_de[i] == caso)
            with self.subTest(caso=caso):
                self.assertGreaterEqual(no_teste, 1)
                self.assertLessEqual(abs(no_treino - n / 2), 0.5)

    def test_a_semente_muda_a_divisao(self) -> None:
        divisoes = {adaptar.dividir(self.ITENS, 0.5, s) for s in range(5)}
        self.assertGreater(len(divisoes), 1)

    def test_caso_com_uma_so_frase_fica_no_teste(self) -> None:
        divisao = adaptar.dividir([("en-01", "x"), ("en-02", "y"), ("en-03", "y")], 0.9, 1790)
        self.assertIn("en-01", divisao.teste)

    def test_entradas_invalidas(self) -> None:
        with self.assertRaises(ValueError):
            adaptar.dividir([("en-01", "a"), ("en-01", "b")], 0.5, 1)
        for fracao in (0, 1, -0.1, 1.5):
            with self.subTest(fracao=fracao), self.assertRaises(ValueError):
                adaptar.dividir(self.ITENS, fracao, 1)


# --- Aprendizagem do lexico ------------------------------------------------------------


def _exemplo(id_: str, referencia: str, *hipoteses: str) -> adaptar.ExemploDeTreino:
    return adaptar.ExemploDeTreino(id_, referencia, tuple(hipoteses))


class TestAprenderLexico(unittest.TestCase):
    def test_aprende_a_correcao_que_ajuda(self) -> None:
        aprendido = adaptar.aprender_lexico([
            _exemplo("t1", "Jarvis, cancel.", "Jarvis, castle."),
            _exemplo("t2", "Ask Claude to add tests", "Ask Cloud to attest", "Ask Claude to attest"),
        ])
        regras = {(r.de, r.para) for r in aprendido.regras}
        self.assertIn(("castle", "cancel"), regras)
        self.assertIn(("Cloud", "Claude"), regras)
        self.assertIn(("attest", "add tests"), regras)
        lexico = aprendido.lexico()
        self.assertEqual(lexico.corrigir("Ask Cloud to attest"), "Ask Claude to add tests")
        # O que se escreve e aceite pelo jarvis tal como esta.
        self.assertEqual(Lexico.de_dados(aprendido.dados()).regras, lexico.regras)

    def test_recusa_regra_que_piora_outra_frase_de_treino(self) -> None:
        aprendido = adaptar.aprender_lexico([
            _exemplo("t1", "add tests", "attest"),
            # Aqui "attest" foi uma palavra a mais: corrigi-la para "add tests" piorava.
            _exemplo("t2", "go now", "attest go now"),
        ])
        self.assertNotIn(("attest", "add tests"), {(r.de, r.para) for r in aprendido.regras})
        self.assertEqual(aprendido.recusadas.get(adaptar.RECUSA_PIORA), 1)

    def test_recusa_regra_que_muda_a_intencao_de_outra_frase(self) -> None:
        exemplos = [
            _exemplo("t1", "cancel", "castle"),
            _exemplo("t2", "the castles", "the castle"),
        ]
        intencao = lambda texto: "cancelar" if "cancel" in texto.lower() else "ditar"  # noqa: E731
        com_intencao = {(r.de, r.para) for r in adaptar.aprender_lexico(exemplos, intencao).regras}
        self.assertNotIn(("castle", "cancel"), com_intencao)
        # Sem olhar para a intencao a distancia nao piora, por isso so a intencao a trava.
        sem_intencao = adaptar.aprender_lexico(exemplos)
        self.assertNotIn(adaptar.RECUSA_PIORA, sem_intencao.recusadas)

    def test_recusa_forma_que_e_dita_noutra_frase_de_treino(self) -> None:
        aprendido = adaptar.aprender_lexico([
            _exemplo("t1", "cancel", "castle"),
            _exemplo("t2", "open the castle door", "open the castle door"),
        ])
        self.assertEqual(aprendido.regras, [])
        self.assertEqual(aprendido.recusadas, {adaptar.RECUSA_FORMA_CERTA: 1})

    def test_sem_ganho_e_sem_regras_longas(self) -> None:
        self.assertEqual(adaptar.aprender_lexico([_exemplo("t1", "what time is it", "what time is it")]).candidatas, 0)
        longa = adaptar.candidatas("one two three four five", "a b c d e")
        self.assertEqual(longa, [])

    def test_uma_forma_ouvida_fica_com_uma_so_correcao(self) -> None:
        aprendido = adaptar.aprender_lexico([
            _exemplo("t1", "cancel", "castle"),
            _exemplo("t2", "cancel it", "castle it"),
            _exemplo("t3", "Carlos", "castle"),
        ])
        formas = [r.de.lower() for r in aprendido.regras]
        self.assertEqual(len(formas), len(set(formas)))
        self.assertIn(("castle", "cancel"), {(r.de, r.para) for r in aprendido.regras})


# --- A execucao com motor falso -----------------------------------------------------------


class TestCorrer(_ComGravacoes):
    def test_o_teste_nunca_alimenta_o_lexico(self) -> None:
        transcritor = TranscritorFalso(self.referencias, 100.0, 105.0)
        relatorio = self._correr(transcritor)
        divisao = relatorio.divisao
        self.assertFalse(set(divisao.treino) & set(divisao.teste))
        self.assertEqual(set(divisao.treino) | set(divisao.teste), {f.id for f in self.frases})
        self.assertEqual(len(relatorio.ids_do_treino_extra), self.N_TREINO_EXTRA)
        # Cada regra vem de uma marca de uma gravacao do lado do treino.
        lado_treino = set(divisao.treino) | set(relatorio.ids_do_treino_extra)
        formas = {r.de for r in relatorio.lexico.regras}
        self.assertTrue(formas)
        self.assertEqual(formas, {_marca(i) for i in lado_treino} & formas)
        self.assertFalse(formas & {_marca(i) for i in divisao.teste})
        # O que foi escrito e o mesmo lexico, lido pelo jarvis.
        escrito = Lexico.de_dados(json.loads(self.lexico.read_text(encoding="utf-8")))
        self.assertEqual(escrito.regras, relatorio.lexico.lexico().regras)
        self.assertTrue(transcritor.libertado)

    def test_mede_so_o_teste_com_as_quatro_variantes(self) -> None:
        transcritor = TranscritorFalso(self.referencias, 100.0, 105.0)
        relatorio = self._correr(transcritor)
        n_teste = len(relatorio.divisao.teste)
        self.assertTrue(relatorio.medido)
        for variante in adaptar.VARIANTES:
            self.assertEqual(relatorio.resultados[variante].n, n_teste)
        base, reforco, lexico = (relatorio.resultados[v] for v in (adaptar.BASE, adaptar.REFORCO, adaptar.LEXICO))
        self.assertEqual(base.intencao_preservada, 0)
        self.assertEqual(reforco.intencao_preservada, n_teste)
        self.assertEqual(reforco.wer, 0.0)
        # O lexico nao conhece as marcas do teste: nao muda nada no teste.
        self.assertEqual(lexico.wer, base.wer)
        self.assertEqual(relatorio.melhor, adaptar.REFORCO)
        # Sem e com reforco alternam a ordem de frase para frase.
        depois_do_treino = transcritor.pedidos[-2 * n_teste:]
        self.assertEqual({i for i, _ in depois_do_treino}, set(relatorio.divisao.teste))
        self.assertEqual([m for _, m in depois_do_treino[:4]], [False, True, True, False])

    def test_latencia_acima_de_1_2x_nao_cumpre(self) -> None:
        relatorio = self._correr(TranscritorFalso(self.referencias, 100.0, 125.0))
        self.assertFalse(relatorio.cumpre(adaptar.REFORCO))
        self.assertIsNone(relatorio.melhor)
        texto = adaptar.evidencia_markdown(relatorio)
        self.assertIn("META NÃO CUMPRIDA", texto)
        self.assertIn(adaptar.COMANDO_PILOTO, texto)
        self.assertIn("reforco = false", adaptar.config_recomendada(relatorio))
        self.assertTrue(self._correr(TranscritorFalso(self.referencias, 100.0, 120.0)).cumpre(adaptar.REFORCO))

    def test_intencao_igual_nao_cumpre(self) -> None:
        relatorio = self._correr(TranscritorFalso(self.referencias, 100.0, 100.0))
        linhas = adaptar.ResultadoDaVariante
        base = relatorio.resultados[adaptar.BASE]
        relatorio.resultados[adaptar.REFORCO] = linhas(
            adaptar.REFORCO, base.n, base.wer / 2, base.intencao_preservada, 0, 0, base.p50_ms, base.p95_ms
        )
        self.assertFalse(relatorio.cumpre(adaptar.REFORCO))

    def test_evidencia_sem_transcricoes_nem_frases(self) -> None:
        relatorio = self._correr(TranscritorFalso(self.referencias, 100.0, 105.0))
        texto = adaptar.evidencia_markdown(relatorio)
        self.assertIn("META CUMPRIDA", texto)
        for id_ in relatorio.divisao.teste + relatorio.divisao.treino + relatorio.ids_do_treino_extra:
            self.assertIn(id_, texto)
            self.assertNotIn(_marca(id_), texto)
        for _id, referencia in self.referencias.values():
            self.assertNotIn(referencia, texto)
        for nome in PROJETOS.values():
            self.assertNotIn(nome, texto)
        self.assertNotIn(str(self.raiz), texto)

    def test_sem_treino_extra_e_sem_reforco(self) -> None:
        transcritor = TranscritorFalso(self.referencias, 100.0, 100.0, reforco_disponivel=False)
        relatorio = self._correr(transcritor, treino_extra=False)
        self.assertEqual(relatorio.ids_do_treino_extra, ())
        self.assertEqual(set(relatorio.resultados), {adaptar.BASE, adaptar.LEXICO})
        self.assertFalse(any(modo for _, modo in transcritor.pedidos))
        self.assertIn("Reforço indisponível", adaptar.evidencia_markdown(relatorio))

    def test_sem_medir_so_aprende(self) -> None:
        transcritor = TranscritorFalso(self.referencias, 100.0, 100.0)
        relatorio = self._correr(transcritor, medir_teste=False)
        self.assertFalse(relatorio.medido)
        self.assertTrue(self.lexico.is_file())
        self.assertFalse({i for i, _ in transcritor.pedidos} & set(relatorio.divisao.teste))

    def test_sem_gravacoes_fica_pendente_com_o_passo_do_sponsor(self) -> None:
        vazia = avaliar.GravacoesDaLingua("en")
        transcritor = TranscritorFalso({}, 100.0, 100.0)
        relatorio = adaptar.correr(
            transcritor, vazia, None, _config(), self._relatorio(), self.lexico, True, interpretar=_interpretar
        )
        self.assertIsNotNone(relatorio.pendente)
        self.assertEqual(transcritor.pedidos, [])
        self.assertFalse(self.lexico.exists())
        texto = adaptar.evidencia_markdown(relatorio)
        self.assertIn("PENDENTE", texto)
        self.assertIn(adaptar.COMANDO_PILOTO, texto)
        self.assertIn(adaptar.COMANDO_MEDIR, texto)


class TestTranscritorParakeet(unittest.TestCase):
    def test_troca_o_decode_conforme_o_reforco(self) -> None:
        transcritor = adaptar.TranscritorParakeet(["cancel"], 1.5, avisar=lambda _linha: None)
        asr = mock.Mock()
        gancho = mock.Mock(original=object(), avariado=False)
        transcritor.adaptacao.gancho = gancho
        vistos = []
        motor = mock.Mock(_modelo=mock.Mock(asr=asr), latencia_carregamento_ms=5.0)
        motor.transcrever.side_effect = lambda pcm, lingua: (
            vistos.append((asr._decode, lingua)) or mock.Mock(texto="ok", latencia_ms=7.0)
        )
        transcritor.motor = motor
        transcritor.carregar()
        self.assertEqual(transcritor.transcrever(b"\x01\x00", True), ("ok", 7.0))
        transcritor.transcrever(b"\x01\x00", False)
        self.assertEqual(vistos, [(gancho, "en"), (gancho.original, "en")])

    def test_reforco_pedido_sem_gancho_rebenta(self) -> None:
        transcritor = adaptar.TranscritorParakeet(["cancel"], 1.5, avisar=lambda _linha: None)
        with self.assertRaises(RuntimeError):
            transcritor.transcrever(b"\x01\x00", True)


# --- Onde se escreve ---------------------------------------------------------------------


def _ignorado_pelo_git(caminho: Path) -> bool | None:
    try:
        feito = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", str(caminho.relative_to(RAIZ))],
            cwd=RAIZ, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(feito.returncode)


class TestSaidas(unittest.TestCase):
    def test_lexico_so_em_models_adaptacao(self) -> None:
        self.assertEqual(adaptar.caminho_do_lexico_de_saida(str(CAMINHO_DO_LEXICO)), CAMINHO_DO_LEXICO.resolve())
        for valor in (
            "lexico.json",
            "models/lexico.json",
            "models/adaptacao/../lexico.json",
            "tests/voz/lexico.json",
            "models/adaptacao/lexico.txt",
            str(RAIZ.parent / "lexico.json"),
        ):
            with self.subTest(valor=valor), self.assertRaises(ValueError):
                adaptar.caminho_do_lexico_de_saida(valor)

    def test_as_pastas_de_saida_estao_ignoradas(self) -> None:
        for caminho in (CAMINHO_DO_LEXICO, PASTA_DE_EVIDENCIA / "adaptar-sotaque-x.md",
                        RAIZ / "recordings" / "treino-en" / "en-t01.wav"):
            with self.subTest(caminho=caminho.name):
                ignorado = _ignorado_pelo_git(caminho)
                if ignorado is None:
                    self.skipTest("git indisponivel")
                self.assertTrue(ignorado)

    def test_main_recusa_saidas_fora_das_pastas_ignoradas(self) -> None:
        for argumentos in (["--lexico", "tests/lexico.json"], ["--saida", "README.md"], ["--saida", "docs/x.md"]):
            with self.subTest(argumentos=argumentos):
                with mock.patch.object(adaptar, "correr") as correr, \
                        contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(adaptar.main(argumentos), 2)
                correr.assert_not_called()


class TestMain(_ComGravacoes):
    """O comando inteiro com o motor falso: escreve so em pastas ignoradas."""

    def setUp(self) -> None:
        super().setUp()
        nome = f"teste-{uuid.uuid4().hex}"
        self.lexico_no_repo = CAMINHO_DO_LEXICO.parent / f"{nome}.json"
        self.saida_no_repo = PASTA_DE_EVIDENCIA / f"{nome}.md"
        self.addCleanup(self.lexico_no_repo.unlink, missing_ok=True)
        self.addCleanup(self.saida_no_repo.unlink, missing_ok=True)

    def test_main_mede_e_escreve_so_nas_pastas_ignoradas(self) -> None:
        carregar = avaliar.carregar_gravacoes
        transcritores = []

        def criar(_frases, _bonus, avisar):
            transcritores.append(TranscritorFalso(self.referencias, 100.0, 105.0))
            return transcritores[-1]

        antes = self._ficheiros_versionados()
        with mock.patch.object(adaptar, "TranscritorParakeet", criar), \
                mock.patch.object(avaliar, "carregar_gravacoes", lambda _p, lingua, treino=False: carregar(
                    self.pasta, lingua, treino=treino)), \
                mock.patch.object(medir, "carregar_config_para_arnes", lambda: (_config(), "teste")), \
                mock.patch.object(avaliar, "intencao_pelo_router", _interpretar), \
                mock.patch.object(adaptar.correr, "__defaults__", (_interpretar, adaptar.time.perf_counter)), \
                contextlib.redirect_stdout(io.StringIO()) as saida:
            codigo = adaptar.main([
                "--medir",
                "--lexico", str(self.lexico_no_repo.relative_to(RAIZ)),
                "--saida", str(self.saida_no_repo.relative_to(RAIZ)),
            ])
        self.assertEqual(codigo, 0)
        self.assertEqual(len(transcritores), 1)
        self.assertIn("Melhor: " + adaptar.REFORCO, saida.getvalue())
        self.assertTrue(self.lexico_no_repo.is_file())
        self.assertIn("META CUMPRIDA", self.saida_no_repo.read_text(encoding="utf-8"))
        self.assertEqual(self._ficheiros_versionados(), antes)

    def _ficheiros_versionados(self) -> str | None:
        try:
            return subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=RAIZ, capture_output=True, text=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None


if __name__ == "__main__":
    unittest.main()
