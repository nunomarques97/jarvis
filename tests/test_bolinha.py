"""Testes da bolinha de estado (`jarvis.bolinha`) e das capturas.

Sem ecra, sem janela, sem som e sem microfone: so as partes puras (protocolo,
modelo de estados, suavizacao do nivel, clique contra arrasto, posicao) e a
verificacao das capturas com imagens sinteticas numa pasta temporaria.

    .venv\\Scripts\\python -m unittest tests.test_bolinha -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import bolinha
from jarvis.bolinha import (
    ESTADOS,
    MAX_LEGENDA,
    MAX_LINHA_BYTES,
    NIVEL_VELHO_S,
    Area,
    Arrasto,
    Mensagem,
    MensagemInvalida,
    ModeloDaBolinha,
    guardar_posicao,
    interpretar_linha,
    intervalo_do_quadro,
    legenda_segura,
    ler_comandos,
    ler_linhas,
    ler_posicao,
    limitar,
    posicao_inicial,
    posicao_no_canto,
    prender_ao_monitor,
    quadro_do_estado,
    suavizar,
)

RAIZ = Path(__file__).resolve().parents[1]


def _carregar_script(nome: str):
    chave = f"_jarvis_scripts_{nome}"
    if chave in sys.modules:
        return sys.modules[chave]
    spec = importlib.util.spec_from_file_location(chave, RAIZ / "scripts" / f"{nome}.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    spec.loader.exec_module(modulo)
    return modulo


class TestProtocolo(unittest.TestCase):
    def test_estados_validos(self):
        for estado in ESTADOS:
            self.assertEqual(interpretar_linha(f"estado {estado}\n".encode()), Mensagem("estado", estado))

    def test_estado_desconhecido_ou_mal_escrito(self):
        for linha in (b"estado", b"estado ", b"estado OUVIR", b"estado ouvir ", b"estado a falar", b"estado  ouvir"):
            with self.assertRaises(MensagemInvalida, msg=linha):
                interpretar_linha(linha)

    def test_comando_desconhecido(self):
        for linha in (b"clique", b"sair", b"ESTADO ouvir", b"", b" estado ouvir", b"estado\touvir"):
            with self.assertRaises(MensagemInvalida, msg=linha):
                interpretar_linha(linha)

    def test_nivel_preso_a_zero_um(self):
        self.assertEqual(interpretar_linha(b"nivel 0.25"), Mensagem("nivel", 0.25))
        self.assertEqual(interpretar_linha(b"nivel 1.7"), Mensagem("nivel", 1.0))
        self.assertEqual(interpretar_linha(b"nivel -0.3"), Mensagem("nivel", 0.0))
        self.assertEqual(interpretar_linha(b"nivel 1\r\n"), Mensagem("nivel", 1.0))

    def test_nivel_invalido(self):
        for linha in (b"nivel", b"nivel ", b"nivel abc", b"nivel nan", b"nivel inf", b"nivel -inf",
                      b"nivel 0." + b"1" * 20, b"nivel 1e999"):  # fmt: skip
            with self.assertRaises(MensagemInvalida, msg=linha):
                interpretar_linha(linha)

    def test_legenda_e_apagar(self):
        self.assertEqual(interpretar_linha("legenda what time is it".encode()), Mensagem("legenda", "what time is it"))
        self.assertEqual(interpretar_linha(b"legenda"), Mensagem("legenda", ""))
        self.assertEqual(interpretar_linha(b"legenda   "), Mensagem("legenda", ""))

    def test_linha_longa_demais(self):
        no_limite = b"legenda " + b"a" * (MAX_LINHA_BYTES - len(b"legenda "))
        self.assertEqual(interpretar_linha(no_limite).tipo, "legenda")
        with self.assertRaises(MensagemInvalida):
            interpretar_linha(no_limite + b"a")

    def test_nao_utf8(self):
        with self.assertRaises(MensagemInvalida):
            interpretar_linha(b"legenda \xff\xfe")


class TestLeituraDoStdin(unittest.TestCase):
    def test_linha_longa_nao_estraga_a_seguinte(self):
        fluxo = io.BytesIO(b"estado ouvir\n" + b"x" * 5000 + b"\nestado falar\nnivel 0.5")
        linhas = list(ler_linhas(fluxo))
        self.assertEqual(linhas, [b"estado ouvir", None, b"estado falar", b"nivel 0.5"])

    def test_linha_longa_no_fim_sem_fim_de_linha(self):
        self.assertEqual(list(ler_linhas(io.BytesIO(b"y" * 3000))), [None])

    def test_entrega_as_validas_e_fim_no_eof(self):
        entregues, avisos = [], []
        fluxo = io.BytesIO(b"estado pensar\n\nbogus\nnivel 2\r\n" + b"z" * 900 + b"\nlegenda ok\n")
        ler_comandos(fluxo, entregues.append, avisos.append)
        self.assertEqual(
            entregues,
            [Mensagem("estado", "pensar"), Mensagem("nivel", 1.0), Mensagem("legenda", "ok"), None],
        )
        self.assertEqual(len(avisos), 2)

    def test_stdin_que_falha_ou_nao_existe_acaba_na_mesma(self):
        class Partido:
            def readline(self, _n):
                raise OSError("tubo partido")

        for fluxo in (Partido(), None):
            entregues, avisos = [], []
            ler_comandos(fluxo, entregues.append, avisos.append)
            self.assertEqual(entregues, [None])


class TestLegendaSegura(unittest.TestCase):
    def test_tira_controlos_e_direcao(self):
        texto = "open\tthe\neditor\x00 ‮evil‬​ now"
        self.assertEqual(legenda_segura(texto), "open the editor evil now")

    def test_tira_fora_do_plano_basico(self):
        self.assertEqual(legenda_segura("hi \U0001F600 there"), "hi there")

    def test_corta_numa_palavra_com_reticencias(self):
        texto = "word " * 40
        resultado = legenda_segura(texto)
        self.assertLessEqual(len(resultado), MAX_LEGENDA)
        self.assertTrue(resultado.endswith("…"))
        self.assertTrue(resultado[:-1].endswith("word"))

    def test_palavra_enorme_e_cortada_a_meio(self):
        resultado = legenda_segura("a" * 500)
        self.assertEqual(len(resultado), MAX_LEGENDA)

    def test_curta_fica_igual(self):
        self.assertEqual(legenda_segura("  yes,   send it "), "yes, send it")


class TestNivel(unittest.TestCase):
    def test_limitar(self):
        self.assertEqual(limitar(2), 1.0)
        self.assertEqual(limitar(-1), 0.0)
        self.assertEqual(limitar(float("nan")), 0.0)
        self.assertEqual(limitar("x"), 0.0)
        self.assertEqual(limitar(float("inf")), 1.0)

    def test_sobe_mais_depressa_do_que_desce(self):
        subida = suavizar(0.0, 1.0, 0.05)
        descida = 1.0 - suavizar(1.0, 0.0, 0.05)
        self.assertGreater(subida, descida)
        self.assertAlmostEqual(subida, 1 - math.exp(-1), places=6)

    def test_converge_e_fica_nos_limites(self):
        nivel = 0.0
        for _ in range(100):
            nivel = suavizar(nivel, 5.0, 0.033)
            self.assertLessEqual(nivel, 1.0)
        self.assertAlmostEqual(nivel, 1.0, places=3)

    def test_dt_invalido_nao_mexe(self):
        for dt in (0.0, -1.0, float("nan"), float("inf")):
            self.assertEqual(suavizar(0.4, 1.0, dt), 0.4)


class TestModelo(unittest.TestCase):
    def test_mudar_de_estado_zera_o_nivel_alvo(self):
        m = ModeloDaBolinha()
        m.aplicar(Mensagem("estado", "ouvir"), 0.0)
        m.aplicar(Mensagem("nivel", 0.9), 0.0)
        self.assertTrue(m.aplicar(Mensagem("estado", "pensar"), 0.1))
        self.assertEqual(m.nivel_alvo, 0.0)
        self.assertEqual(m.desde, 0.1)
        self.assertFalse(m.aplicar(Mensagem("estado", "pensar"), 0.2))
        self.assertEqual(m.desde, 0.1)

    def test_repouso_e_dormir_apagam_a_legenda(self):
        for estado in ("repouso", "dormir"):
            m = ModeloDaBolinha(estado="pensar")
            m.aplicar(Mensagem("legenda", "what time is it"), 0.0)
            m.aplicar(Mensagem("estado", "confirmar"), 0.1)
            self.assertEqual(m.legenda, "what time is it")
            m.aplicar(Mensagem("estado", estado), 0.2)
            self.assertEqual(m.legenda, "")

    def test_nivel_velho_volta_a_zero(self):
        m = ModeloDaBolinha(estado="ouvir")
        m.aplicar(Mensagem("nivel", 1.0), 0.0)
        m.passo(0.0)
        m.passo(0.2)
        self.assertGreater(m.nivel, 0.9)
        m.passo(NIVEL_VELHO_S + 0.01)
        self.assertEqual(m.nivel_alvo, 0.0)
        for i in range(1, 60):
            m.passo(NIVEL_VELHO_S + i * 0.033)
        self.assertLess(m.nivel, 0.01)

    def test_orbe_cresce_com_o_nivel(self):
        for estado in ("ouvir", "falar", "confirmar"):
            baixo = quadro_do_estado(estado, 0.0, 1.0, 1.0)
            alto = quadro_do_estado(estado, 1.0, 1.0, 1.0)
            self.assertGreater(alto.raio, baixo.raio, estado)

    def test_cada_estado_tem_uma_pista_de_forma_propria(self):
        for animacao in (True, False):
            pistas = {}
            for estado in ESTADOS:
                q = quadro_do_estado(estado, 0.5, 1.0, 1.0, animacao=animacao)
                pista = (q.anel is not None, q.sinal, len(q.pontos), len(q.barras), q.lua)
                pistas[estado] = pista
            # repouso distingue-se de dormir e dos outros por ser um ponto simples e pequeno
            self.assertEqual(len(set(pistas.values())), len(ESTADOS), pistas)

    def test_parado_nao_depende_do_nivel_nem_do_tempo(self):
        for estado in ESTADOS:
            a = quadro_do_estado(estado, 0.0, 0.1, 0.3, animacao=False)
            b = quadro_do_estado(estado, 1.0, 5.0, 17.9, animacao=False)
            self.assertEqual(a, b, estado)

    def test_animado_muda_com_o_tempo(self):
        for estado in ("repouso", "pensar", "confirmar"):
            quadros = {quadro_do_estado(estado, 0.0, t / 10, t / 10) for t in range(20)}
            self.assertGreater(len(quadros), 1, estado)

    def test_erro_so_treme_a_entrada(self):
        self.assertNotEqual(quadro_do_estado("erro", 0, 0.02, 0).dx, 0.0)
        self.assertEqual(quadro_do_estado("erro", 0, 1.0, 1.0).dx, 0.0)

    def test_tamanhos_cabem_na_zona_do_orbe(self):
        metade = bolinha.ZONA_DO_ORBE / 2
        for estado in ESTADOS:
            for t in range(40):
                q = quadro_do_estado(estado, 1.0, t / 20, t / 20)
                maior = max(q.raio, q.anel or 0) + q.anel_espessura
                self.assertLess(maior, metade, estado)

    def test_intervalo_do_quadro(self):
        self.assertAlmostEqual(intervalo_do_quadro("ouvir", True, 0.0), 1 / 30)
        self.assertEqual(intervalo_do_quadro("ouvir", False, 0.0), 0.1)
        self.assertEqual(intervalo_do_quadro("dormir", True, 0.0), 0.1)
        self.assertEqual(intervalo_do_quadro("erro", True, 1.0), 0.1)
        self.assertAlmostEqual(intervalo_do_quadro("erro", True, 0.1), 1 / 30)


class TestCliqueContraArrasto(unittest.TestCase):
    def test_clique_curto_sem_mexer(self):
        a = Arrasto()
        a.premir(100, 100, 10, 20, 0.0)
        self.assertIsNone(a.mover(102, 101))
        self.assertEqual(a.largar(102, 101, 0.2), "clique")

    def test_mexer_ate_ao_limiar_e_arrasto(self):
        a = Arrasto()
        a.premir(100, 100, 10, 20, 0.0)
        self.assertEqual(a.mover(104, 100), (14, 20))
        self.assertEqual(a.mover(150, 60), (60, -20))
        self.assertEqual(a.largar(150, 60, 0.1), "arrasto")

    def test_arrastar_e_voltar_ao_sitio_continua_arrasto(self):
        a = Arrasto()
        a.premir(100, 100, 0, 0, 0.0)
        a.mover(120, 100)
        self.assertEqual(a.mover(100, 100), (0, 0))
        self.assertEqual(a.largar(100, 100, 0.2), "arrasto")

    def test_premido_demais_nao_e_clique(self):
        a = Arrasto()
        a.premir(0, 0, 0, 0, 0.0)
        self.assertIsNone(a.largar(1, 0, 2.0))

    def test_largar_sem_premir(self):
        a = Arrasto()
        self.assertIsNone(a.largar(0, 0, 0.0))
        self.assertIsNone(a.mover(50, 50))

    def test_salto_so_no_largar_tambem_e_arrasto(self):
        a = Arrasto()
        a.premir(0, 0, 0, 0, 0.0)
        self.assertEqual(a.largar(30, 0, 0.1), "arrasto")


class TestPosicao(unittest.TestCase):
    MONITOR = Area(0, 0, 1920, 1040)
    SEGUNDO = Area(1920, 0, 3840, 1040)

    def setUp(self):
        self._pasta = tempfile.TemporaryDirectory()
        self.pasta = Path(self._pasta.name)
        self.caminho = self.pasta / ".jarvis" / "bolinha.json"

    def tearDown(self):
        self._pasta.cleanup()

    def test_guardar_e_ler(self):
        self.assertTrue(guardar_posicao(self.caminho, 1500, 700))
        self.assertEqual(ler_posicao(self.caminho), (1500, 700))
        self.assertEqual(json.loads(self.caminho.read_text(encoding="utf-8")), {"x": 1500, "y": 700})
        self.assertFalse(self.caminho.with_name("bolinha.json.tmp").exists())

    def test_ficheiros_que_nao_prestam(self):
        self.assertIsNone(ler_posicao(self.caminho))
        self.assertIsNone(ler_posicao(None))
        self.caminho.parent.mkdir(parents=True)
        for conteudo in ("{", "[]", '{"x": 1}', '{"x": true, "y": 2}', '{"x": 1.5, "y": 2}',
                         '{"x": 1, "y": 999999999}', '"texto"', "\xff"):  # fmt: skip
            self.caminho.write_text(conteudo, encoding="utf-8")
            self.assertIsNone(ler_posicao(self.caminho), conteudo)

    def test_guardar_que_falha_diz_que_falhou(self):
        self.caminho.mkdir(parents=True)  # um diretorio no lugar do ficheiro
        self.assertFalse(guardar_posicao(self.caminho, 1, 2))
        self.assertFalse(guardar_posicao(None, 1, 2))

    def test_canto_por_omissao(self):
        self.assertEqual(posicao_no_canto(224, 192, self.MONITOR), (1920 - 224 - 16, 1040 - 192 - 16))
        self.assertEqual(
            posicao_inicial(None, 224, 192, [self.SEGUNDO, self.MONITOR], self.MONITOR),
            (1920 - 224 - 16, 1040 - 192 - 16),
        )

    def test_monitor_desligado_volta_para_um_visivel(self):
        self.assertEqual(prender_ao_monitor(3000, 500, 224, 192, [self.MONITOR]), (1920 - 224, 500))
        self.assertEqual(prender_ao_monitor(-5000, -300, 224, 192, [self.MONITOR]), (0, 0))

    def test_meio_fora_fica_no_monitor_que_mais_a_mostra(self):
        self.assertEqual(prender_ao_monitor(1900, 1000, 224, 192, [self.MONITOR, self.SEGUNDO]), (1920, 1040 - 192))
        self.assertEqual(prender_ao_monitor(1800, 100, 224, 192, [self.MONITOR, self.SEGUNDO]), (1920 - 224, 100))

    def test_dentro_fica_onde_esta(self):
        self.assertEqual(prender_ao_monitor(2500, 300, 224, 192, [self.MONITOR, self.SEGUNDO]), (2500, 300))

    def test_posicao_guardada_e_presa_ao_arrancar(self):
        guardar_posicao(self.caminho, 5000, 5000)
        self.assertEqual(
            posicao_inicial(ler_posicao(self.caminho), 224, 192, [self.MONITOR], self.MONITOR),
            (1920 - 224, 1040 - 192),
        )

    def test_sem_areas_usa_o_ecra(self):
        self.assertEqual(posicao_inicial((9000, 9000), 224, 192, [], self.MONITOR), (1920 - 224, 1040 - 192))


class TestProcessoFilho(unittest.TestCase):
    def test_sem_tk_sai_com_erro_sem_ficar_preso(self):
        import tkinter

        with mock.patch.object(tkinter, "Tk", side_effect=tkinter.TclError("no display")), \
                mock.patch.object(bolinha, "ativar_dpi"), mock.patch("sys.stderr", io.StringIO()) as erro:  # fmt: skip
            self.assertEqual(bolinha.main(["--tema", "escuro"]), 1)
        self.assertIn("sem janela", erro.getvalue())

    def test_saida_so_escreve_e_aguenta_tubo_partido(self):
        fluxo = io.BytesIO()
        saida = bolinha._Saida(fluxo)
        saida(bolinha.MENSAGEM_CLIQUE)
        self.assertEqual(fluxo.getvalue(), b"clique\n")
        fluxo.close()
        saida(bolinha.MENSAGEM_CLIQUE)  # nao levanta


class TestCapturas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cb = _carregar_script("capturar_bolinha")

    def _imagem(self, cor=(32, 32, 32), ponto=None, largura=40, altura=30):
        rgb = bytearray(bytes(cor) * (largura * altura))
        if ponto is not None:
            for y in range(10, 20):
                for x in range(10, 20):
                    i = (y * largura + x) * 3
                    rgb[i : i + 3] = bytes(ponto)
        return largura, altura, bytes(rgb)

    def test_png_ida_e_volta(self):
        largura, altura, rgb = self._imagem(ponto=(200, 10, 90))
        dados = self.cb.png_rgb(largura, altura, rgb)
        self.assertTrue(dados.startswith(b"\x89PNG"))
        self.assertEqual(self.cb.ler_png(dados), (largura, altura, rgb))

    def test_em_branco(self):
        self.assertTrue(self.cb.em_branco(*self._imagem()))
        self.assertFalse(self.cb.em_branco(*self._imagem(ponto=(255, 0, 0))))

    def _escrever_todas(self, pasta: Path):
        for n, cena in enumerate(self.cb.cenas()):
            for t, tema in enumerate(bolinha.TEMAS):
                imagem = self._imagem(cor=(t * 200, 30, 30), ponto=(n * 7 % 256, n, 255 - n))
                (pasta / self.cb.nome_do_ficheiro(tema, cena)).write_bytes(self.cb.png_rgb(*imagem))

    def test_verificar(self):
        with tempfile.TemporaryDirectory() as nome:
            pasta = Path(nome)
            self._escrever_todas(pasta)
            self.assertEqual(self.cb.verificar(pasta), [])

            falta = pasta / "escuro-pensar.png"
            falta.unlink()
            self.assertIn("falta escuro-pensar.png", self.cb.verificar(pasta))

            self._escrever_todas(pasta)
            (pasta / "claro-dormir.png").write_bytes(self.cb.png_rgb(*self._imagem()))
            self.assertIn("claro-dormir.png esta em branco", self.cb.verificar(pasta))

            self._escrever_todas(pasta)
            igual = (pasta / "escuro-ouvir.png").read_bytes()
            (pasta / "escuro-falar.png").write_bytes(igual)
            self.assertTrue(any("escuro-falar.png e igual a escuro-ouvir.png" in p for p in self.cb.verificar(pasta)))

            (pasta / "claro-erro.png").write_bytes(b"not a png")
            self.assertTrue(any(p.startswith("claro-erro.png nao se le") for p in self.cb.verificar(pasta)))

    def test_cenas_cobrem_todos_os_estados_legenda_e_parados(self):
        nomes = [c.nome for c in self.cb.cenas()]
        for estado in ESTADOS:
            self.assertIn(estado, nomes)
            self.assertIn(f"estatico-{estado}", nomes)
        self.assertTrue(any(c.legenda for c in self.cb.cenas()))
        self.assertEqual(len(nomes), len(set(nomes)))


if __name__ == "__main__":
    unittest.main()
