r"""Testes da palavra de ativacao: modelo e limiar da lingua, texto limpo, medicao e treino.

Nenhum teste abre o microfone, sintetiza voz ou toca som: o detetor, o VAD, o
motor de transcricao e as gravacoes sao falsos (WAV sinteticos em pastas
temporarias). Os modelos reais do openWakeWord so sao usados, se existirem em
models/, para provar que um modelo treinado por `scripts/treinar_ativacao.py`
carrega no detetor do ouvido.

Corre com:

    .venv\Scripts\python -m unittest tests.test_palavra_ativacao -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from jarvis.config import LIMIAR_DE_ATIVACAO_PADRAO, Config, ConfigError, ConfigOuvido, carregar_config
from jarvis.ouvido import (
    BYTES_POR_CHUNK,
    GATILHO_ATIVACAO,
    GATILHO_TECLA,
    MODELOS_DE_ATIVACAO,
    PALAVRAS_DE_ATIVACAO,
    SILENCIO_FINAL_S,
    DURACAO_DO_CHUNK_S,
    FonteDeFicheiro,
    Ouvido,
    modelo_de_ativacao,
    retirar_palavra_de_ativacao,
)
from jarvis.router import encaminhar
from jarvis.stt import MotorBase

RAIZ = Path(__file__).resolve().parent.parent


def _carregar_script(nome: str):
    """O script por caminho, com a mesma chave de cache que os scripts usam entre si."""
    chave = f"_jarvis_scripts_{nome}"
    if chave in sys.modules:
        return sys.modules[chave]
    spec = importlib.util.spec_from_file_location(chave, RAIZ / "scripts" / f"{nome}.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    spec.loader.exec_module(modulo)
    return modulo


avaliar = _carregar_script("avaliar_ativacao")
treinar = _carregar_script("treinar_ativacao")


def _silencioso():
    return contextlib.redirect_stdout(io.StringIO())


# --- Texto: a palavra sai antes do interprete, sem dicionario de enganos -------------------


class TestRetirarPalavraDeAtivacao(unittest.TestCase):
    def test_palavra_inteira_sai_com_a_pontuacao_colada(self) -> None:
        casos = [
            ("Hey Jarvis, what time is it?", "hey jarvis", "what time is it?", "Hey Jarvis"),
            ("hey jarvis what time is it", "hey jarvis", "what time is it", "hey jarvis"),
            ("Boas, Jarvis! Que horas são?", "boas jarvis", "Que horas são?", "Boas, Jarvis"),
            ("BOAS JARVIS — abre a pasta", "boas jarvis", "abre a pasta", "BOAS JARVIS"),
        ]
        for texto, palavra, esperado, retirada in casos:
            with self.subTest(texto=texto):
                self.assertEqual(retirar_palavra_de_ativacao(texto, [palavra]), (esperado, retirada))

    def test_o_resto_do_texto_fica_tal_e_qual(self) -> None:
        texto, _ = retirar_palavra_de_ativacao("hey jarvis, Tell Ação: X.", ["hey jarvis"])
        self.assertEqual(texto, "Tell Ação: X.")

    def test_grafia_parecida_nao_e_a_palavra(self) -> None:
        # O caso do log real: "Jorvis, que oração!" nao e "jarvis" nem vira "que horas sao".
        for texto in ("Jorvis, que oração!", "Hei Jarvis, que horas são", "Javis, open the folder", "hey jervis stop"):
            with self.subTest(texto=texto):
                self.assertEqual(
                    retirar_palavra_de_ativacao(texto, ["hey jarvis", "boas jarvis"], aceitar_cauda=False),
                    (texto, None),
                )
        self.assertEqual(retirar_palavra_de_ativacao("Jorvis, que oração!", ["hey jarvis"]), ("Jorvis, que oração!", None))

    def test_cauda_so_nas_maos_livres(self) -> None:
        self.assertEqual(
            retirar_palavra_de_ativacao("Jarvis, what time is it?", ["hey jarvis"]), ("what time is it?", "Jarvis")
        )
        self.assertEqual(
            retirar_palavra_de_ativacao("Jarvis, what time is it?", ["hey jarvis"], aceitar_cauda=False),
            ("Jarvis, what time is it?", None),
        )

    def test_o_inicio_da_palavra_sozinho_nao_sai(self) -> None:
        # "boas" sozinho e uma palavra normal ("boas noticias"), nao a ativacao.
        self.assertEqual(retirar_palavra_de_ativacao("Boas notícias do run", ["boas jarvis"]), ("Boas notícias do run", None))
        self.assertEqual(retirar_palavra_de_ativacao("hey, open it", ["hey jarvis"]), ("hey, open it", None))

    def test_so_no_inicio_e_nunca_esvazia(self) -> None:
        self.assertEqual(retirar_palavra_de_ativacao("abre o jarvis", ["hey jarvis"]), ("abre o jarvis", None))
        self.assertEqual(
            retirar_palavra_de_ativacao("open it, hey jarvis", ["hey jarvis"]), ("open it, hey jarvis", None)
        )
        for texto in ("hey jarvis", "Hey Jarvis!", "jarvis"):
            with self.subTest(texto=texto):
                self.assertEqual(retirar_palavra_de_ativacao(texto, ["hey jarvis"]), (texto, None))

    def test_so_a_palavra_da_lingua_dada(self) -> None:
        self.assertEqual(
            retirar_palavra_de_ativacao("boas jarvis, que horas são", ["hey jarvis"], aceitar_cauda=False),
            ("boas jarvis, que horas são", None),
        )


class MotorComTexto(MotorBase):
    nome = "motor-falso"

    def __init__(self, texto: str) -> None:
        super().__init__("cpu")
        self.texto = texto

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return (self.texto if any(pcm16) else ""), lingua, False


class DetetorComScore:
    """Da `score` no chunk marcado com 0x7A e 0 nos outros."""

    def __init__(self, score: float = 0.9) -> None:
        self.score = score

    def processar(self, pedaco: bytes) -> float:
        return self.score if pedaco[0] == 0x7A else 0.0

    def reiniciar(self) -> None:
        pass


class VadFalso:
    def e_fala(self, pedaco: bytes) -> bool:
        return pedaco[0] != 0


def _chunk(valor: int) -> bytes:
    return bytes([valor]) * BYTES_POR_CHUNK


class TestOuvidoEntregaOTextoSemAPalavra(unittest.TestCase):
    def _maos_livres(self, texto: str, lingua: str | None, score: float = 0.9, limiar: float = 0.6):
        frases, linhas = [], []
        ouvido = Ouvido(
            FonteDeFicheiro(b""), MotorComTexto(texto), frases.append, detetor=DetetorComScore(score),
            vad=VadFalso(), lingua=lingua, limiar_de_ativacao=limiar, escrever=linhas.append,
        )
        ouvido.processar(_chunk(0x7A), False)
        for _ in range(20):
            ouvido.processar(_chunk(0x55), False)
        for _ in range(round(SILENCIO_FINAL_S / DURACAO_DO_CHUNK_S)):
            ouvido.processar(_chunk(0x00), False)
        ouvido.transcrever_pendentes()
        return frases, linhas

    def _tecla(self, texto: str, lingua: str | None):
        frases = []
        ouvido = Ouvido(
            FonteDeFicheiro(b""), MotorComTexto(texto), frases.append, tecla=object(), lingua=lingua,
            escrever=lambda _t: None,
        )
        for _ in range(20):
            ouvido.processar(_chunk(0x22), True)
        ouvido.processar(_chunk(0x00), False)
        ouvido.transcrever_pendentes()
        return frases

    def test_maos_livres_tira_a_palavra_e_regista_o_que_saiu(self) -> None:
        frases, linhas = self._maos_livres("Hey Jarvis, what time is it?", "en")
        self.assertEqual([f.texto for f in frases], ["what time is it?"])
        self.assertEqual(frases[0].palavra_retirada, "Hey Jarvis")
        self.assertEqual(frases[0].gatilho, GATILHO_ATIVACAO)
        self.assertTrue(any("palavra de ativacao retirada" in l for l in linhas))

    def test_maos_livres_tira_a_cauda_da_palavra(self) -> None:
        frases, _ = self._maos_livres("Jarvis, que horas são?", "pt")
        self.assertEqual(frases[0].texto, "que horas são?")

    def test_tecla_so_tira_a_palavra_inteira(self) -> None:
        self.assertEqual(self._tecla("boas jarvis, que horas são", "pt")[0].texto, "que horas são")
        frase = self._tecla("Jarvis, que horas são", "pt")[0]
        self.assertEqual((frase.texto, frase.palavra_retirada), ("Jarvis, que horas são", None))
        self.assertEqual(frase.gatilho, GATILHO_TECLA)

    def test_grafia_parecida_chega_intacta_e_nao_vira_horas(self) -> None:
        frases, _ = self._maos_livres("Jorvis, que oração!", "pt")
        self.assertEqual((frases[0].texto, frases[0].palavra_retirada), ("Jorvis, que oração!", None))
        resultado = encaminhar(frases[0].texto, Config(microfone="", projetos=()))
        self.assertIsNone(resultado.nome_acao)
        self.assertNotEqual(resultado.tipo, "local")

    def test_sem_lingua_vale_qualquer_das_duas_palavras(self) -> None:
        self.assertEqual(self._tecla("boas jarvis, olá", None)[0].texto, "olá")
        self.assertEqual(self._tecla("hey jarvis, hello", None)[0].texto, "hello")

    def test_o_limiar_configurado_decide_a_ativacao(self) -> None:
        frases, _ = self._maos_livres("olá", "pt", score=0.5, limiar=0.45)
        self.assertEqual(len(frases), 1)
        frases, _ = self._maos_livres("olá", "pt", score=0.5, limiar=0.6)
        self.assertEqual(frases, [])


# --- Modelo e limiar da lingua ----------------------------------------------------------


class TestModeloELimiarDaLingua(unittest.TestCase):
    def test_palavra_e_modelo_por_lingua(self) -> None:
        self.assertEqual(PALAVRAS_DE_ATIVACAO, {"en": "hey jarvis", "pt": "boas jarvis"})
        self.assertEqual(modelo_de_ativacao("en").name, "hey_jarvis_v0.1.onnx")
        self.assertEqual(modelo_de_ativacao("pt").name, "boas_jarvis.onnx")
        self.assertEqual(modelo_de_ativacao(None), MODELOS_DE_ATIVACAO["en"])
        for modelo in MODELOS_DE_ATIVACAO.values():
            self.assertEqual(modelo.parent, RAIZ / "models" / "openwakeword")

    def _config(self, pasta: str, extra: str):
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            f'[[projetos]]\nnome = "p"\ncaminho = "{Path(pasta).as_posix()}"\n\n[ouvido]\n' + extra,
            encoding="utf-8",
        )
        return carregar_config(caminho)

    def test_limiar_por_omissao_e_configurado(self) -> None:
        self.assertEqual(ConfigOuvido().limiar_ativacao, LIMIAR_DE_ATIVACAO_PADRAO)
        with tempfile.TemporaryDirectory() as pasta:
            self.assertEqual(self._config(pasta, "limiar_ativacao = 0.45\n").ouvido.limiar_ativacao, 0.45)
            self.assertEqual(self._config(pasta, 'lingua = "en"\n').ouvido.limiar_ativacao, LIMIAR_DE_ATIVACAO_PADRAO)

    def test_limiar_invalido_e_recusado(self) -> None:
        for valor in ("0", "1", "1.5", "-0.2", '"0.5"', "true"):
            with self.subTest(valor=valor), tempfile.TemporaryDirectory() as pasta:
                with self.assertRaises(ConfigError):
                    self._config(pasta, f"limiar_ativacao = {valor}\n")

    def test_o_aviso_do_ptt_diz_a_palavra_da_lingua(self) -> None:
        """A consola diz a palavra que o detetor carregado ouve, nunca a da outra lingua."""
        from unittest import mock

        from jarvis import app
        from tests.test_app import Montagem, _MotorDeTexto, _TeclaSolta

        for lingua, certa, errada in (("pt", "boas jarvis", "hey jarvis"), ("en", "hey jarvis", "boas jarvis")):
            with self.subTest(lingua=lingua):
                montagem = Montagem(lingua=lingua)
                modelos = []

                def detetor(modelo):
                    modelos.append(modelo)
                    return DetetorComScore(0.0)

                with mock.patch("jarvis.app.DetetorOpenWakeWord", detetor), mock.patch("jarvis.app.VadWebRtc", VadFalso):
                    ouvido = app.construir_ouvido(
                        montagem.jarvis, motor=_MotorDeTexto(), fonte=FonteDeFicheiro(bytes(3200)), tecla=_TeclaSolta()
                    )
                    codigo = app.correr(montagem.jarvis, ouvido, com_voz=False, medir=lambda: None)
                self.assertEqual(codigo, 0)
                self.assertEqual(modelos, [modelo_de_ativacao(lingua)])
                texto = montagem.log.texto()
                self.assertIn(f'maos-livres: diz "{certa}" e a frase', texto)
                self.assertNotIn(errada, texto)

    def test_o_exemplo_versionado_documenta_o_limiar(self) -> None:
        texto = (RAIZ / "config.exemplo.toml").read_text(encoding="utf-8")
        self.assertIn("limiar_ativacao = 0.6", texto)
        self.assertIn("avaliar_ativacao.py", texto)


# --- Guiao versionado --------------------------------------------------------------------


class TestGuiaoDeAtivacao(unittest.TestCase):
    def test_vinte_repeticoes_e_ruido_de_30_minutos(self) -> None:
        linhas = avaliar.ler_guiao()
        self.assertEqual([n for n, _ in linhas], [f"{i:02d}" for i in range(1, 21)])
        texto = avaliar.GUIAO.read_text(encoding="utf-8")
        for trecho in ("30 minutos", "--gravar-palavra", "--gravar-ruido", "PENDENTE — passo do Sponsor",
                       "--conjunto treino", "hey jarvis", "boas jarvis"):
            self.assertIn(trecho, texto)

    def test_guiao_sem_nomes_nem_caminhos_privados(self) -> None:
        texto = avaliar.GUIAO.read_text(encoding="utf-8")
        self.assertNotRegex(texto, r"[A-Za-z]:[\\/]")
        self.assertNotIn("Users", texto)

    def test_guiao_mal_formado_e_recusado(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "g.md"
            caminho.write_text("| n | como dizer |\n|---|---|\n| 01 | normal |\n", encoding="utf-8")
            with self.assertRaises(avaliar.GuiaoError):
                avaliar.ler_guiao(caminho)

    def test_ids_por_lingua_e_conjunto(self) -> None:
        self.assertEqual(avaliar.frases_para_gravar("pt")[0].frase.split("[")[0].strip(), "boas jarvis")
        self.assertEqual(avaliar.id_da_repeticao("en", "07"), "ativ-en-07")
        self.assertEqual(avaliar.id_da_repeticao("pt", "07", avaliar.CONJUNTO_TREINO), "treino-pt-07")
        self.assertEqual(avaliar.nome_da_pasta_da_palavra("pt", avaliar.CONJUNTO_TREINO), "pt-treino")


# --- Medicao: curva, limiar, PENDENTE -------------------------------------------------------


class TestCurvaEEscolhaDoLimiar(unittest.TestCase):
    def test_despertares_com_refratario(self) -> None:
        serie = [0.0] * 60
        serie[5] = serie[6] = serie[7] = 0.8  # um despertar so
        serie[40] = 0.8  # outro, 2,6 s depois
        self.assertEqual(avaliar.contar_despertares(serie, 0.5), 2)
        self.assertEqual(avaliar.contar_despertares(serie, 0.9), 0)

    def test_falsos_normalizados_a_30_minutos(self) -> None:
        ponto = avaliar.calcular_curva([0.9] * 20, [[0.7] + [0.0] * 50], 900.0, [0.5])[0]
        self.assertEqual((ponto.falsos, ponto.falsos_por_30_min), (1, 2.0))
        self.assertFalse(ponto.cumpre)

    def test_meta_exige_95_por_cento_e_um_falso_por_30_min(self) -> None:
        ponto = avaliar.calcular_curva([0.9] * 19 + [0.1], [[0.7] + [0.0] * 50], 1800.0, [0.5])[0]
        self.assertTrue(ponto.cumpre)
        ponto = avaliar.calcular_curva([0.9] * 18 + [0.1] * 2, [[0.0]], 1800.0, [0.5])[0]
        self.assertFalse(ponto.cumpre)

    def test_escolhe_o_meio_da_faixa(self) -> None:
        curva = avaliar.calcular_curva([0.9] * 20, [[0.32] + [0.0] * 40 + [0.32]], 1800.0)
        faixa = [p.limiar for p in curva if p.cumpre]
        self.assertEqual(faixa[0], 0.35)
        self.assertEqual(avaliar.escolher_limiar(curva).limiar, faixa[(len(faixa) - 1) // 2])

    def test_sem_faixa_nao_declara_cumprida(self) -> None:
        curva = avaliar.calcular_curva([0.3] * 20, [[0.9, 0.0] * 40], 1800.0)
        escolhido = avaliar.escolher_limiar(curva)
        self.assertFalse(escolhido.cumpre)


class TestEvidencia(unittest.TestCase):
    def _medir(self, raiz: Path, lingua: str = "en", modelo: Path | None = None):
        base = avaliar.pasta_base(raiz)
        if modelo is None:
            modelo = raiz / "models" / "m.onnx"
            modelo.parent.mkdir(parents=True, exist_ok=True)
            modelo.write_bytes(b"falso")
        return avaliar.medir(lingua, modelo, base / lingua, base / avaliar.NOME_PASTA_RUIDO, avaliar.DetetorFalso,
                             escrever=lambda _t: None)

    def test_sem_gravacoes_a_evidencia_diz_pendente(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            medicao = self._medir(Path(pasta))
        texto = avaliar.texto_da_evidencia(medicao, datetime(2026, 9, 25, 12, 0))
        self.assertEqual(medicao.estado, "PENDENTE — passo do Sponsor")
        self.assertIn("**Estado: PENDENTE — passo do Sponsor**", texto)
        self.assertNotIn("METAS CUMPRIDAS", texto)
        self.assertNotIn("limiar_ativacao =", texto)

    def test_ruido_curto_mantem_pendente_mesmo_com_curva_boa(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            base = avaliar.pasta_base(raiz)
            avaliar._escrever_gravacoes(base / "en", {
                avaliar.id_da_repeticao("en", f"{i:02d}"): avaliar.pcm_com_pico(0.5, 0.9) for i in range(1, 21)
            })
            avaliar._escrever_gravacoes(base / avaliar.NOME_PASTA_RUIDO, {"ruido-01": avaliar.pcm_com_pico(60.0, 0.01)})
            medicao = self._medir(raiz)
        self.assertEqual(medicao.estado, avaliar.ESTADO_PENDENTE)
        self.assertTrue(any("--gravar-ruido" in p for p in medicao.pendencias))
        texto = avaliar.texto_da_evidencia(medicao, datetime(2026, 9, 25))
        self.assertIn("PARCIAL", texto)
        self.assertNotIn("<- escolhido", texto)

    def test_modelo_portugues_em_falta_aponta_o_treino(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            medicao = self._medir(Path(pasta), "pt", Path(pasta) / "models" / "boas_jarvis.onnx")
        self.assertTrue(any("treinar_ativacao.py" in p for p in medicao.pendencias))

    def test_evidencia_so_dentro_de_docs_forja_evidence(self) -> None:
        with self.assertRaises(ValueError):
            avaliar.caminho_evidencia_de_saida("docs/ativacao.md")
        self.assertEqual(avaliar.mostrar(Path(tempfile.gettempdir()) / "x" / "m.onnx"), "m.onnx")

    def test_verificar_falha_quando_pendente(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            medicao = self._medir(Path(pasta))
        self.assertNotEqual(medicao.estado, avaliar.ESTADO_CUMPRIDA)


class TestSoContaAVozDoSponsor(unittest.TestCase):
    """Negativos: gravacoes que o manifesto marca como sinteticas nunca decidem nada."""

    def _completas(self, base: Path, origem: str) -> None:
        avaliar._escrever_gravacoes(base / "en", {
            avaliar.id_da_repeticao("en", f"{i:02d}"): avaliar.pcm_com_pico(0.5, 0.9) for i in range(1, 21)
        }, origem)
        avaliar._escrever_gravacoes(base / avaliar.NOME_PASTA_RUIDO, {
            "ruido-01": avaliar.pcm_com_pico(1800.0, 0.01)
        }, origem)

    def _medir(self, raiz: Path):
        base = avaliar.pasta_base(raiz)
        modelo = raiz / "models" / "m.onnx"
        modelo.parent.mkdir(parents=True, exist_ok=True)
        modelo.write_bytes(b"falso")
        return avaliar.medir("en", modelo, base / "en", base / avaliar.NOME_PASTA_RUIDO, avaliar.DetetorFalso,
                             escrever=lambda _t: None)

    def test_do_microfone_cumpre(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            self._completas(avaliar.pasta_base(Path(pasta)), avaliar.gravar_voz.ORIGEM_MICROFONE)
            medicao = self._medir(Path(pasta))
        self.assertEqual(medicao.estado, avaliar.ESTADO_CUMPRIDA)

    def test_sinteticas_ficam_pendentes_sem_limiar(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            self._completas(avaliar.pasta_base(Path(pasta)), avaliar.gravar_voz.ORIGEM_SINTETICA)
            medicao = self._medir(Path(pasta))
        texto = avaliar.texto_da_evidencia(medicao, datetime(2026, 9, 25))
        self.assertEqual(medicao.estado, avaliar.ESTADO_PENDENTE)
        self.assertEqual((medicao.positivos, medicao.ruido_s, medicao.escolhido), ([], 0.0, None))
        self.assertIn("ativ-en-01", medicao.invalidas)
        self.assertIn("ruido-01", medicao.invalidas)
        self.assertIn("sintetico", medicao.invalidas["ativ-en-01"])
        self.assertNotIn("limiar_ativacao =", texto)
        self.assertNotIn(avaliar.ESTADO_CUMPRIDA, texto)

    def test_origem_em_falta_nao_conta(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            destino = Path(pasta) / "en"
            avaliar._escrever_gravacoes(destino, {"ativ-en-01": avaliar.pcm_com_pico(0.5, 0.9)})
            manifesto = avaliar.gravar_voz.ler_manifesto(destino)
            del manifesto["gravacoes"]["ativ-en-01"]["origem"]
            avaliar.gravar_voz.escrever_manifesto(destino, manifesto)
            validos, invalidos = avaliar.gravacoes_validas(destino)
        self.assertEqual(validos, [])
        self.assertIn("ativ-en-01", invalidos)

    def test_treino_so_usa_repeticoes_do_microfone(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            gravacoes = Path(pasta) / "recordings"
            base = gravacoes / avaliar.NOME_PASTA_ATIVACAO
            pcm = avaliar.pcm_com_pico(0.5, 0.5)
            avaliar._escrever_gravacoes(base / "pt-treino", {
                avaliar.id_da_repeticao("pt", "01", avaliar.CONJUNTO_TREINO): pcm,
            }, avaliar.gravar_voz.ORIGEM_SINTETICA)
            positivos, _ = treinar.gravacoes_do_sponsor(gravacoes)
        self.assertEqual(positivos, [])


class TestGravacaoDoRuido(unittest.TestCase):
    def test_blocos_validos_retoma_e_recusa_de_silencio_digital(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            destino = avaliar.pasta_base(raiz) / avaliar.NOME_PASTA_RUIDO
            captura = avaliar.CapturaDeRuidoFalsa([b"\x00\x00" * 16000 * 20])
            resumo = avaliar.gravar_ruido(destino, captura, threading.Event(), alvo_s=40.0, segmento_s=20.0,
                                          raiz=raiz, escrever=lambda _t: None)
            self.assertEqual((resumo.recusados, resumo.gravados), (1, ["ruido-01", "ruido-02"]))
            self.assertEqual(captura.fechadas, 1)
            resumo = avaliar.gravar_ruido(destino, avaliar.CapturaDeRuidoFalsa(), threading.Event(), alvo_s=40.0,
                                          raiz=raiz, escrever=lambda _t: None)
            self.assertEqual(resumo.gravados, [])
            # A captura falsa grava com origem sintetica: conta para retomar a
            # gravacao falsa, nunca para a medicao.
            validos, _ = avaliar.gravacoes_validas(destino, origem=avaliar.gravar_voz.ORIGEM_SINTETICA)
            self.assertEqual([i for i, _ in validos], ["ruido-01", "ruido-02"])
            validos, invalidos = avaliar.gravacoes_validas(destino)
            self.assertEqual((validos, sorted(invalidos)), ([], ["ruido-01", "ruido-02"]))


class TestAutotestes(unittest.TestCase):
    def test_autoteste_do_avaliador(self) -> None:
        with _silencioso():
            self.assertEqual(avaliar.main(["--autoteste"]), 0)

    def test_autoteste_do_treino(self) -> None:
        with _silencioso():
            self.assertEqual(treinar.main(["--autoteste"]), 0)


# --- Treino do modelo portugues --------------------------------------------------------------


class TestTreino(unittest.TestCase):
    def test_onnx_minimo_igual_ao_forward(self) -> None:
        import numpy as np

        rng = np.random.default_rng(5)
        pesos = [rng.standard_normal((16 * 96, 4)), rng.standard_normal((4, 3)), rng.standard_normal((3, 1))]
        vieses = [rng.standard_normal(4), rng.standard_normal(3), rng.standard_normal(1)]
        x = rng.standard_normal((3, 16, 96))
        obtido = treinar.prever_onnx(treinar.onnx_do_mlp(pesos, vieses), x)
        self.assertLess(float(np.max(np.abs(obtido - treinar.forward_numpy(pesos, vieses, x)))), 1e-4)

    def test_normalizacao_dobrada_nos_pesos(self) -> None:
        import numpy as np
        from sklearn.neural_network import MLPClassifier

        rng = np.random.default_rng(6)
        x = rng.standard_normal((80, 12)) * 5 + 3
        y = (x[:, 0] > 3).astype(int)
        media, desvio = x.mean(axis=0), x.std(axis=0)
        mlp = MLPClassifier(hidden_layer_sizes=(5,), max_iter=50, random_state=0)
        with _silencioso(), __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("ignore")
            mlp.fit((x - media) / desvio, y)
        pesos, vieses = treinar.pesos_do_mlp(mlp, media, desvio)
        esperado = mlp.predict_proba((x - media) / desvio)[:, 1]
        self.assertLess(float(np.max(np.abs(treinar.forward_numpy(pesos, vieses, x) - esperado))), 1e-9)

    def test_sem_repeticoes_de_treino_recusa_e_diz_o_passo(self) -> None:
        erro = io.StringIO()
        original = treinar.gravacoes_do_sponsor
        treinar.gravacoes_do_sponsor = lambda _pasta: ([], [])
        try:
            with _silencioso(), contextlib.redirect_stderr(erro):
                codigo = treinar.main(["--saida", "models/openwakeword/teste-nunca-escrito.onnx"])
        finally:
            treinar.gravacoes_do_sponsor = original
        self.assertEqual(codigo, 1)
        self.assertIn("PENDENTE — passo do Sponsor", erro.getvalue())
        self.assertIn("--conjunto treino", erro.getvalue())
        self.assertFalse((RAIZ / "models" / "openwakeword" / "teste-nunca-escrito.onnx").exists())

    def test_treino_nunca_le_o_conjunto_de_avaliacao_nem_o_ruido(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            gravacoes = Path(pasta) / "recordings"
            base = gravacoes / avaliar.NOME_PASTA_ATIVACAO
            pcm = avaliar.pcm_com_pico(0.5, 0.5)
            avaliar._escrever_gravacoes(base / "pt", {avaliar.id_da_repeticao("pt", "01"): pcm})
            avaliar._escrever_gravacoes(base / "ruido", {"ruido-01": avaliar.pcm_com_pico(20.0, 0.1)})
            avaliar._escrever_gravacoes(base / "pt-treino", {
                avaliar.id_da_repeticao("pt", "01", avaliar.CONJUNTO_TREINO): pcm
            })
            positivos, negativos = treinar.gravacoes_do_sponsor(gravacoes)
        self.assertEqual([i for i, _ in positivos], ["treino-pt-01"])
        self.assertEqual(negativos, [])

    def test_saida_confinada(self) -> None:
        self.assertEqual(treinar.caminho_de_saida(None, False), treinar.MODELO_DE_SAIDA.resolve())
        for valor, ensaio in (("docs/m.onnx", False), ("README.onnx", False), ("models/m.bin", False), (None, True)):
            with self.subTest(valor=valor, ensaio=ensaio), self.assertRaises(ValueError):
                treinar.caminho_de_saida(valor, ensaio)

    def test_modelo_treinado_carrega_no_detetor_do_ouvido(self) -> None:
        if not (treinar.MODELO_MELSPEC.is_file() and treinar.MODELO_EMBEDDING.is_file()):
            self.skipTest("modelos de caracteristicas do openWakeWord em falta em models/")
        import numpy as np
        from jarvis.ouvido import DetetorOpenWakeWord

        rng = np.random.default_rng(7)
        pesos = [rng.standard_normal((16 * 96, 4)) * 0.01, rng.standard_normal((4, 1))]
        vieses = [np.zeros(4), np.zeros(1)]
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "boas_jarvis.onnx"
            caminho.write_bytes(treinar.onnx_do_mlp(pesos, vieses))
            detetor = DetetorOpenWakeWord(caminho)
            self.assertEqual(detetor.nome, "boas_jarvis")
            scores = avaliar.serie_de_scores(detetor, b"\x00\x00" * 16000 * 2)
        self.assertEqual(len(scores), 25)
        self.assertTrue(all(0.0 <= s <= 1.0 for s in scores))


if __name__ == "__main__":
    unittest.main()
