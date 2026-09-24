r"""Testes da decisao de lingua PT/EN (jarvis/lingua.py).

Sem GPU, sem modelo, sem audio: `decidir_lingua` e `decidir_lingua_do_top1`
sao funcoes puras sobre numeros que o faster-whisper (ou o RealtimeSTT) ja
devolveu. O que estes testes protegem e a regra de produto, nao a aritmetica:

  * o argmax e RESTRITO a {pt, en} — uma terceira lingua com a probabilidade
    mais alta NUNCA decide nada;
  * o limiar e 0,5, o default da propria biblioteca, e e ele (e so ele) que
    diz se a deteccao HESITOU;
  * a lingua devolvida esta SEMPRE dentro de {pt, en}, aconteca o que
    acontecer a entrada — porque o produto tem sempre de decidir em que lingua
    escreve o log e responde;
  * hesitar nunca e um erro nem um estado terceiro: quem encaminha casa a
    frase contra as DUAS listas brancas, com ou sem hesitacao.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis.lingua import (  # noqa: E402
    LIMIAR_CONFIANCA,
    LINGUA_POR_OMISSAO,
    LINGUAS_RESTRITAS,
    decidir_lingua,
    decidir_lingua_do_top1,
    lingua_fixada,
)


class TestArgmaxRestrito(unittest.TestCase):
    """So `pt` e `en` decidem; terceira lingua nunca."""

    def test_portugues_claro(self) -> None:
        d = decidir_lingua([("pt", 0.97), ("es", 0.02), ("en", 0.01)])
        self.assertEqual(d.lingua, "pt")
        self.assertAlmostEqual(d.probabilidade, 0.97)
        self.assertFalse(d.hesitou)

    def test_ingles_claro(self) -> None:
        d = decidir_lingua([("en", 0.93), ("pt", 0.04), ("nl", 0.03)])
        self.assertEqual(d.lingua, "en")
        self.assertAlmostEqual(d.probabilidade, 0.93)
        self.assertFalse(d.hesitou)

    def test_terceira_lingua_mais_provavel_nao_decide_a_lingua_do_produto(self) -> None:
        # O caso a garantir: o argmax LIVRE aponta para espanhol,
        # a decisao do PRODUTO continua a sair de entre pt e en.
        d = decidir_lingua([("es", 0.70), ("en", 0.20), ("pt", 0.05)])
        self.assertEqual(d.lingua, "en")
        self.assertAlmostEqual(d.probabilidade, 0.20)
        self.assertEqual(d.top1, "es")
        self.assertTrue(d.lingua_terceira)
        self.assertIn("es", d.motivo)

    def test_o_motivo_diz_que_a_terceira_lingua_descodificou(self) -> None:
        # Um defeito anterior foi escrever que a
        # terceira lingua "nao decidiu nada". Decidiu o TEXTO: com
        # `language=None` e o argmax livre que descodifica. O motivo tem de o
        # dizer, e nao pode voltar a dizer que ela foi ignorada.
        d = decidir_lingua([("es", 0.70), ("en", 0.20), ("pt", 0.05)])
        self.assertIn("lingua-terceira", d.motivo)
        self.assertIn("descodificou", d.motivo)
        self.assertNotIn("ignorado", d.motivo)
        self.assertEqual(d.marca_lingua_terceira, "lingua-terceira(es descodificou)")
        self.assertIn("lingua-terceira(es descodificou)", d.resumo())

    def test_sem_terceira_lingua_nao_ha_marca_nenhuma(self) -> None:
        # A marca e contavel: so aparece quando ha mesmo uma terceira lingua a
        # descodificar, senao a contagem por ficheiro nao valia nada.
        d = decidir_lingua([("pt", 0.91), ("en", 0.05)])
        self.assertFalse(d.lingua_terceira)
        self.assertEqual(d.marca_lingua_terceira, "")
        self.assertNotIn("lingua-terceira", d.resumo())

    def test_galego_a_ganhar_com_o_portugues_logo_atras(self) -> None:
        # O engano realista com voz portuguesa: uma lingua vizinha a ganhar o
        # argmax livre. A lingua decidida continua a ser o portugues.
        d = decidir_lingua([("gl", 0.55), ("pt", 0.40), ("en", 0.02)])
        self.assertEqual(d.lingua, "pt")
        self.assertEqual(d.top1, "gl")
        self.assertTrue(d.hesitou)

    def test_a_lingua_devolvida_esta_sempre_dentro_das_duas(self) -> None:
        entradas = [
            None,
            [],
            [("es", 1.0)],
            [("zh", 0.4), ("ja", 0.4)],
            {"fr": 0.9},
            [("pt", 0.0), ("en", 0.0)],
        ]
        for entrada in entradas:
            with self.subTest(entrada=entrada):
                self.assertIn(decidir_lingua(entrada).lingua, LINGUAS_RESTRITAS)

    def test_empate_entre_as_duas_fica_no_portugues(self) -> None:
        # Empate nao e sorteio: fica a lingua por omissao do produto.
        d = decidir_lingua([("pt", 0.45), ("en", 0.45)])
        self.assertEqual(d.lingua, LINGUA_POR_OMISSAO)
        self.assertTrue(d.hesitou)


class TestLimiarDaHesitacao(unittest.TestCase):
    """`hesitou` e exatamente a comparacao com o limiar."""

    def test_o_limiar_e_o_default_da_biblioteca(self) -> None:
        # faster_whisper/transcribe.py:747 -> language_detection_threshold=0.5
        self.assertEqual(LIMIAR_CONFIANCA, 0.5)

    def test_acima_do_limiar_nao_hesita(self) -> None:
        self.assertFalse(decidir_lingua([("pt", 0.51), ("en", 0.10)]).hesitou)

    def test_abaixo_do_limiar_hesita(self) -> None:
        self.assertTrue(decidir_lingua([("pt", 0.49), ("en", 0.10)]).hesitou)

    def test_exatamente_no_limiar_hesita(self) -> None:
        # A biblioteca aceita a deteccao com `prob > limiar` (transcribe.py:1787):
        # 0,50 nao passa. Hesitar e o lado seguro — nao perde comando nenhum.
        self.assertTrue(decidir_lingua([("pt", 0.50), ("en", 0.10)]).hesitou)

    def test_o_limiar_pode_ser_apertado_por_quem_chama(self) -> None:
        self.assertTrue(decidir_lingua([("pt", 0.80)], limiar=0.9).hesitou)


class TestEntradasEstranhas(unittest.TestCase):
    """Uma frase ja transcrita nunca se perde por causa do diagnostico."""

    def test_sem_probabilidades_fica_o_portugues_e_hesita(self) -> None:
        d = decidir_lingua(None)
        self.assertEqual(d.lingua, "pt")
        self.assertTrue(d.hesitou)
        self.assertEqual(d.probabilidade, 0.0)
        self.assertIsNone(d.top1)
        self.assertIn("sem probabilidades", d.motivo)

    def test_lista_vazia_e_o_mesmo_que_nada(self) -> None:
        self.assertTrue(decidir_lingua([]).hesitou)

    def test_entradas_mal_formadas_sao_ignoradas_sem_rebentar(self) -> None:
        d = decidir_lingua(["lixo", ("pt", 0.88), ("en", "nao e numero"), (1, 2, 3)])
        self.assertEqual(d.lingua, "pt")
        self.assertAlmostEqual(d.probabilidade, 0.88)
        self.assertEqual(d.prob_en, 0.0)

    def test_aceita_tambem_um_dicionario(self) -> None:
        d = decidir_lingua({"pt": 0.10, "en": 0.85})
        self.assertEqual(d.lingua, "en")
        self.assertAlmostEqual(d.prob_pt, 0.10)

    def test_o_top1_nao_depende_da_ordem_da_lista(self) -> None:
        # O faster-whisper devolve a lista ordenada, mas isso nao esta no
        # contrato da biblioteca: o maximo e calculado, nao assumido.
        d = decidir_lingua([("pt", 0.10), ("es", 0.80), ("en", 0.05)])
        self.assertEqual(d.top1, "es")


class TestLinhaDeLog(unittest.TestCase):
    """Lingua, probabilidade e hesitacao no log da frase."""

    def test_decidida_escreve_os_tres_numeros(self) -> None:
        texto = decidir_lingua([("pt", 0.96), ("en", 0.01)]).para_log()
        self.assertIn("lingua=pt", texto)
        self.assertIn("p=0.96", texto)
        self.assertIn("decidida", texto)
        self.assertIn("pt=0.96", texto)
        self.assertIn("en=0.01", texto)
        self.assertIn("margem=0.95", texto)

    def test_hesitou_diz_que_hesitou(self) -> None:
        texto = decidir_lingua([("pt", 0.30), ("en", 0.25)]).para_log()
        self.assertIn("hesitou", texto)
        self.assertNotIn("decidida", texto)

    def test_a_margem_e_a_distancia_entre_as_duas(self) -> None:
        self.assertAlmostEqual(decidir_lingua([("pt", 0.60), ("en", 0.20)]).margem, 0.40)


class TestLinhaDeLogComLinguaFixa(unittest.TestCase):
    """Com a lingua FIXA (a reversao da deteccao) o log tem de ser CURTO.

    A linha sai uma vez por frase, no log que o utilizador le em direto. Dizer
    `FIXA` cabe numa dezena de caracteres; a justificacao
    inteira da reversao nao muda de frase para frase e vive no `motivo`, que
    e consultavel, e no comentario da constante.
    """

    def test_o_resumo_diz_fixa_sem_probabilidade_inventada(self) -> None:
        texto = lingua_fixada("pt").para_log()
        self.assertEqual(texto, "lingua=pt FIXA (sem deteccao)")
        self.assertNotIn("p=0.00", texto)
        self.assertNotIn("hesitou", texto)

    def test_a_linha_da_lingua_cabe_em_meia_linha_de_consola(self) -> None:
        # Valor real, nao derivado da implementacao: 60 caracteres e menos de
        # metade dos ~190 que a justificacao inteira ocupava por frase.
        self.assertLessEqual(len(lingua_fixada("pt").para_log()), 60)

    def test_a_justificacao_completa_continua_disponivel_no_motivo(self) -> None:
        # Encurtar o log nao pode apagar o porque: quem precisa dele le o
        # `motivo` (o dict de `transcrever()` devolve-o como `lingua_motivo`).
        motivo = lingua_fixada("pt").motivo
        self.assertIn("language='pt' fixo", motivo)
        self.assertIn("portugues a descer", motivo)


class TestCaminhoVivoSoComTop1(unittest.TestCase):
    """O que o RealtimeSTT expoe: `detected_language` e mais nada."""

    def test_top1_portugues(self) -> None:
        d = decidir_lingua_do_top1("pt", 0.99)
        self.assertEqual(d.lingua, "pt")
        self.assertFalse(d.hesitou)
        self.assertFalse(d.probabilidades_completas)

    def test_top1_ingles(self) -> None:
        d = decidir_lingua_do_top1("en", 0.88)
        self.assertEqual(d.lingua, "en")
        self.assertAlmostEqual(d.prob_en, 0.88)
        self.assertEqual(d.prob_pt, 0.0)

    def test_top1_de_terceira_lingua_nao_decide_a_lingua_do_produto(self) -> None:
        d = decidir_lingua_do_top1("es", 0.91)
        self.assertEqual(d.lingua, LINGUA_POR_OMISSAO)
        self.assertEqual(d.probabilidade, 0.0)
        self.assertTrue(d.hesitou)
        self.assertEqual(d.top1, "es")
        # D66, ponto 3: no caminho vivo o top-1 do RealtimeSTT E o argmax livre
        # do faster-whisper, portanto foi ele que descodificou este texto. A
        # marca tem de estar no que vai para o log.
        self.assertTrue(d.lingua_terceira)
        self.assertIn("lingua-terceira", d.motivo)
        self.assertIn("descodificou", d.motivo)
        self.assertNotIn("ignorado", d.motivo)
        self.assertIn("lingua-terceira(es descodificou)", d.resumo())

    def test_sem_lingua_nenhuma_fica_o_portugues(self) -> None:
        d = decidir_lingua_do_top1(None, None)
        self.assertEqual(d.lingua, "pt")
        self.assertTrue(d.hesitou)
        self.assertIn("nao devolveu lingua", d.motivo)

    def test_probabilidade_abaixo_do_limiar_hesita(self) -> None:
        d = decidir_lingua_do_top1("en", 0.31)
        self.assertEqual(d.lingua, "en")
        self.assertTrue(d.hesitou)
        self.assertIn("0.31", d.motivo)

    def test_o_log_diz_que_so_ha_top1_em_vez_de_inventar_a_outra(self) -> None:
        texto = decidir_lingua_do_top1("pt", 0.99).para_log()
        self.assertIn("lingua=pt", texto)
        self.assertIn("p=0.99", texto)
        self.assertIn("top-1", texto)
        # O numero que nao foi medido NAO aparece como se fosse zero medido.
        self.assertNotIn("en=0.00", texto)

    def test_maiusculas_e_espacos_no_codigo_da_lingua(self) -> None:
        self.assertEqual(decidir_lingua_do_top1(" PT ", 0.9).lingua, "pt")


if __name__ == "__main__":
    unittest.main()
