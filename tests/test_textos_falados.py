r"""Com lingua=en, nenhum texto fixo que o jarvis diz esta em portugues.

Na aceitacao o jarvis dizia sempre uma frase em portugues que a voz inglesa lia e
ninguem percebia ("Resposta do Claude Code, nao verificada: ..."). Este teste
percorre as tabelas inglesas de todos os modulos que falam (app, confirmacao,
estado, avisos, forja_voz, acoes_locais, pergunta_geral e os textos da conversa,
que vivem na app e na confirmacao), mais as frases de recurso da
resposta falada, e falha se algum tiver letras acentuadas ou palavras
portuguesas comuns. Uma tabela nova ou uma chave nova entram sozinhas: o teste
le as tabelas inteiras, nao uma lista escrita a mao.

Tambem garante que cada frase falada em ingles tem duas a quatro formas curtas,
todas com os mesmos marcadores, e que as funcoes que as dizem nunca repetem a
mesma forma duas vezes seguidas. So ficam com uma forma o texto do ecra, os
pedacos de frase e as perguntas do recap (deterministas de proposito).

Corre com:

    .venv\Scripts\python -m unittest tests.test_textos_falados -v
"""

from __future__ import annotations

import datetime
import re
import string
import unittest

from jarvis import acoes_locais, app, avisos, confirmacao, estado, forja_voz, instancia, pergunta_geral
from jarvis import resposta_falada
from jarvis.persona import VARIANTES

#: Letras que o ingles escrito pelo jarvis nunca tem.
_ACENTOS = re.compile(r"[àáâãäçèéêëìíîïñòóôõöùúûüÀÁÂÃÇÉÊÍÓÔÕÚ]")
#: Palavras portuguesas comuns que nao sao palavras inglesas. Ficam de fora de proposito as que
#: tambem sao ingles ("a", "as", "do", "no", "me", "o" de "o'clock", "run").
_PALAVRAS_PORTUGUESAS = frozenset(
    """
    nao sim esta estou estas fiz nada projeto pedido resposta ecra consola acorda dorme cala
    envio enviei enviado verificada que da das dos uma um com sem ao aos de em para por pelo
    pela favor ola obrigado fixe ti tu deixa ver isso isto aqui ja mas ou tambem cancelei
    cancela aborta muda acrescenta corre falta percebi percebo diz fico vou abri pasta ficam
    ficheiro dinheiro bolsa sessao janela aviso acabou terminou falhou bloqueado espera hoje
    horas minutos dia sao respondeu codigo dados tecnicos completa
    """.split()
)
#: Um marcador de formatacao (`{p}`, `{programa}`) nao e texto falado.
_MARCADOR = re.compile(r"\{[^{}]*\}")


def portugues_em(texto: str) -> list[str]:
    """Os sinais de portugues num texto: letras acentuadas e palavras portuguesas."""
    sem_marcadores = _MARCADOR.sub(" ", texto)
    sinais = _ACENTOS.findall(sem_marcadores)
    palavras = re.findall(r"[^\W\d_]+", sem_marcadores.lower())
    return sinais + [palavra for palavra in palavras if palavra in _PALAVRAS_PORTUGUESAS]


def _textos(tabela) -> list[str]:
    """Todos os textos de uma tabela (dicionario, tuplo ou texto), por ordem."""
    if isinstance(tabela, str):
        return [tabela]
    if isinstance(tabela, dict):
        return [texto for valor in tabela.values() for texto in _textos(valor)]
    if isinstance(tabela, (tuple, list)):
        return [texto for valor in tabela for texto in _textos(valor)]
    return []


def textos_falados_em_ingles() -> dict[str, list[str]]:
    """Os textos fixos que o jarvis diz (ou mostra como resposta) com lingua=en, por origem."""
    domingo = datetime.datetime(2026, 9, 27, 15, 0)
    segunda = datetime.datetime(2026, 9, 28, 9, 5)
    return {
        "app": _textos(app._TEXTOS["en"]),
        "confirmacao": _textos(confirmacao._FRASES["en"])
        + _textos(confirmacao._ACOES_EN)
        + _textos(confirmacao._SEM_PROJETO["en"]),
        "estado": _textos(estado._FRASES["en"]) + _textos(estado._MOTIVOS["en"]),
        "avisos": _textos(avisos._FRASES["en"]),
        "forja_voz": _textos(forja_voz._FRASES["en"]),
        "acoes_locais": _textos(acoes_locais.MESES_EN)
        + _textos(acoes_locais.DIAS_SEMANA_EN)
        + [texto for (_, lingua), texto in acoes_locais._FRASES_DE_ABRIR.items() if lingua == "en"]
        + [
            acoes_locais._texto_horas(agora, "en") for agora in (domingo, segunda)
        ]
        + [acoes_locais._texto_data(agora, "en") for agora in (domingo, segunda)],
        "pergunta_geral": [pergunta_geral._LINGUAS["en"]],
        "instancia": [instancia._MENSAGENS["en"]],
        "resposta_falada": [resposta_falada.frase_de_recurso(caso, "en") for caso in ("sem_texto", "so_tecnico", "sem_corte_seguro")]
        + [
            resposta_falada.resumo_falado("", lingua="en"),
            resposta_falada.resumo_falado('{"a": 1}', lingua="en"),
            resposta_falada.resumo_falado("All tests pass.", lingua="en"),
        ],
    }


class TestNenhumTextoInglesEmPortugues(unittest.TestCase):
    def test_todos_os_textos_fixos_em_ingles(self) -> None:
        textos = textos_falados_em_ingles()
        for origem, lista in textos.items():
            self.assertTrue(lista, f"a tabela inglesa de {origem} esta vazia")
            for texto in lista:
                with self.subTest(origem=origem, texto=texto):
                    self.assertEqual(portugues_em(texto), [], f"{origem}: texto em portugues com lingua=en")

    def test_as_tabelas_cobrem_as_mesmas_chaves_que_o_portugues(self) -> None:
        # uma chave so em portugues cairia para o texto portugues com lingua=en
        for nome, tabela in (
            ("app", app._TEXTOS),
            ("confirmacao", confirmacao._FRASES),
            ("estado", estado._FRASES),
            ("avisos", avisos._FRASES),
            ("resposta_falada", resposta_falada.FRASES_DE_RECURSO),
        ):
            with self.subTest(tabela=nome):
                self.assertLessEqual(set(tabela["pt"]), set(tabela["en"]))


#: As frases inglesas faladas, por modulo: cada chave tem duas a quatro formas.
def _tabelas_faladas() -> dict[str, dict]:
    return {
        "app": app._TEXTOS["en"],
        "confirmacao": confirmacao._FRASES["en"],
        "estado": estado._FRASES["en"],
        "avisos": avisos._FRASES["en"],
        "forja_voz": forja_voz._FRASES["en"],
    }


#: Chaves com uma forma so, de proposito: pedacos de frase (juntos a outra frase),
#: os motivos que completam outra frase e as perguntas do recap, que ficam
#: deterministas. O texto do ecra (`ecra_*`) tambem fica com uma forma.
_UMA_FORMA_SO = {
    "confirmacao": {"enviar", "confirmar", "guardar", "apagar", "longo", "para", "ainda_para"},
    "estado": {"a_trabalhar", "parada", "paradas", "a_espera", "outro", "e"},
    "forja_voz": {chave for chave in forja_voz._FRASES["en"] if chave.startswith("motivo_")},
}
#: Uma frase falada curta: acima disto ja soa a leitura.
_MAXIMO_DE_PALAVRAS = 18


def _marcadores(texto: str) -> set[str]:
    return {nome for _, nome, _, _ in string.Formatter().parse(texto) if nome is not None}


class TestFrasesVariadas(unittest.TestCase):
    def test_cada_frase_falada_tem_duas_a_quatro_formas_diferentes(self) -> None:
        for origem, tabela in _tabelas_faladas().items():
            for chave, valor in tabela.items():
                with self.subTest(origem=origem, chave=chave):
                    if chave.startswith("ecra_") or chave in _UMA_FORMA_SO.get(origem, set()):
                        continue
                    self.assertIsInstance(valor, tuple, "frase falada com uma forma so")
                    self.assertTrue(2 <= len(valor) <= 4, f"{len(valor)} formas")
                    self.assertEqual(len(set(valor)), len(valor), "formas repetidas")

    def test_as_formas_de_uma_frase_tem_os_mesmos_marcadores(self) -> None:
        # uma forma sem o {p} (ou com um marcador a mais) partia o .format() ao acaso
        for origem, tabela in _tabelas_faladas().items():
            for chave, valor in tabela.items():
                if isinstance(valor, str):
                    continue
                with self.subTest(origem=origem, chave=chave):
                    self.assertEqual({frozenset(_marcadores(forma)) for forma in valor}, {frozenset(_marcadores(valor[0]))})

    def test_as_formas_sao_curtas(self) -> None:
        for origem, tabela in _tabelas_faladas().items():
            for chave, valor in tabela.items():
                if chave.startswith("ecra_"):
                    continue
                for forma in _textos(valor):
                    with self.subTest(origem=origem, chave=chave, forma=forma):
                        self.assertLessEqual(len(_MARCADOR.sub("x", forma).split()), _MAXIMO_DE_PALAVRAS)

    def test_as_funcoes_que_falam_nunca_repetem_a_mesma_forma_seguida(self) -> None:
        VARIANTES.esquecer()
        self.addCleanup(VARIANTES.esquecer)
        casos = {
            "estado": lambda: estado.frase("sem_run", "en", p="atlas"),
            "avisos": lambda: avisos.frase_do_aviso(avisos.EVENTO_ESPERA, "atlas", "en"),
            "forja_voz": lambda: forja_voz._frase("parado", "en", p="atlas"),
        }
        formas = {
            "estado": len(estado._FRASES["en"]["sem_run"]),
            "avisos": len(avisos._FRASES["en"][avisos.EVENTO_ESPERA]),
            "forja_voz": len(forja_voz._FRASES["en"]["parado"]),
        }
        for origem, dizer in casos.items():
            with self.subTest(origem=origem):
                ditas = [dizer() for _ in range(40)]
                for anterior, seguinte in zip(ditas, ditas[1:]):
                    self.assertNotEqual(anterior, seguinte)
                self.assertEqual(len(set(ditas)), formas[origem], "todas as formas acabam por ser ditas")


class TestODetetorApanhaPortugues(unittest.TestCase):
    """O detetor tem de falhar com os textos portugueses, senao o teste acima nao prova nada."""

    def test_os_textos_que_a_aceitacao_ouviu(self) -> None:
        for texto in (
            resposta_falada.ROTULO_DA_RESPOSTA_DO_CLAUDE,
            resposta_falada.FRASE_RECURSO_SO_TECNICO,
            resposta_falada.FRASE_RECURSO_SEM_TEXTO,
            resposta_falada.FRASE_RECURSO_SEM_CORTE_SEGURO,
            resposta_falada.resumo_falado(""),
        ):
            with self.subTest(texto=texto):
                self.assertNotEqual(portugues_em(texto), [])

    def test_as_tabelas_portuguesas_sao_apanhadas(self) -> None:
        for nome, texto in (
            ("app", app._TEXTOS["pt"]["a_dormir"]),
            ("app", app._TEXTOS["pt"]["cortesia"]),
            ("confirmacao", confirmacao._FRASES["pt"]["de_novo"]),
            ("confirmacao", confirmacao._FRASES["pt"]["enviar"]),
            ("avisos", avisos._FRASES["pt"][avisos.EVENTO_ESPERA]),
            ("forja_voz", forja_voz._FRASES["pt"]["sem_forja"]),
        ):
            with self.subTest(origem=nome, texto=texto):
                self.assertNotEqual(portugues_em(texto), [])

    def test_ingles_normal_nao_e_apanhado(self) -> None:
        for texto in ("Say yes to send, or abort.", "It's 3 o'clock.", "{p} is waiting for you.", "Okay."):
            with self.subTest(texto=texto):
                self.assertEqual(portugues_em(texto), [])


if __name__ == "__main__":
    unittest.main()
