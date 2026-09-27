r"""Testes da memoria do jarvis (jarvis/memoria.py).

Sem microfone, sem som, sem LLM e sem Claude. O relogio das frases recentes e
injetado e o caderno escreve so em pastas temporarias. O que protegem:

  * o caderno grava de forma atomica, respeita os limites de factos e de
    caracteres (e o teto nao se sobe), sobrevive a um ficheiro estragado ou
    grande demais sem o apagar, e o contexto nunca passa dos limites;
  * segredos e dados financeiros sao recusados e nunca escritos;
  * o facto mais parecido e o que se apaga, e nada parecido nao apaga nada.

Corre com:

    .venv\Scripts\python -m unittest tests.test_memoria -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.config import ConfigMemoria
from jarvis.interprete import INTENCAO_RECUSADA
from jarvis.memoria import (
    CAMINHO_DO_CADERNO,
    MAXIMO_DO_FICHEIRO_BYTES,
    MAXIMO_POR_FACTO,
    CadernoCheio,
    CadernoDeFactos,
    FactoRecusado,
    FrasesRecentes,
    contexto_do_interprete,
    facto_mais_parecido,
    factos_relacionados,
    recusa_do_facto,
    texto_do_facto,
)

RAIZ = Path(__file__).resolve().parent.parent


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


# --- Recusa de segredos e dados financeiros ----------------------------------------


class TestRecusa(unittest.TestCase):
    def test_segredos_sao_recusados(self) -> None:
        for facto in (
            "my password is hunter2",
            "the wifi password is banana",
            "a minha palavra-passe é gato123",
            "a senha do email é xpto",
            "my PIN is 1234",
            "o PIN do cartão é 4321",
            "my github token is ghp_abc",
            "my API key is sk-123",
            "a chave da API é abc",
            "the door code is 4321",
            "o código do alarme é 9876",
            "os meus códigos estão na gaveta",
            "my recovery codes are in the drawer",
            "my private key is in the drawer",
            "my phone number is 912 345 678",
        ):
            with self.subTest(facto=facto):
                self.assertEqual(recusa_do_facto(facto), "segredo")

    def test_dados_financeiros_sao_recusados(self) -> None:
        for facto in (
            "my bank account is at CGD",
            "my IBAN is PT50 0000 0000",
            "o meu NIB é o que está no papel",
            "my card number is 4111 1111 1111 1111",
            "my credit card expires in May",
            "o meu cartão de crédito é azul",
            "my balance is low this month",
            "o saldo da conta é baixo",
            "my salary is paid on the 25th",
            "o meu salário é pago ao dia 25",
            "I earn 2000 euros a month",
            "I have €500 in savings",
            "my portfolio is mostly tech",
            "my holdings are in an ETF",
            "I have a mortgage with a bank",
        ):
            with self.subTest(facto=facto):
                self.assertEqual(recusa_do_facto(facto), "financeiro")

    def test_a_regra_financeira_do_interprete_corre_primeiro(self) -> None:
        for facto in ("I want to buy bitcoin", "sell my Tesla shares", "compra ações da Apple"):
            with self.subTest(facto=facto):
                self.assertEqual(recusa_do_facto(facto), "financeiro")

    def test_factos_do_dia_a_dia_passam(self) -> None:
        for facto in (
            "my favourite team is Benfica",
            "I live in Braga",
            "my daughter was born in 2015",
            "I write code in the morning",
            "a minha filha chama-se Ana",
            "prefiro respostas curtas",
            "I like my coffee without sugar",
        ):
            with self.subTest(facto=facto):
                self.assertIsNone(recusa_do_facto(facto))


# --- Caderno de factos ------------------------------------------------------------


class _ComPasta(unittest.TestCase):
    def setUp(self) -> None:
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.pasta = Path(temporaria.name)
        self.caminho = self.pasta / "memoria" / "factos.json"
        self.log: list[str] = []

    def caderno(self, **extras) -> CadernoDeFactos:
        return CadernoDeFactos(self.caminho, registar=self.log.append, **extras)

    def ler(self) -> dict:
        return json.loads(self.caminho.read_text(encoding="utf-8"))


class TestCaderno(_ComPasta):
    def test_por_omissao_fica_numa_pasta_ignorada_pelo_git(self) -> None:
        self.assertEqual(CAMINHO_DO_CADERNO, RAIZ / "memoria" / "factos.json")
        gitignore = (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/memoria/", gitignore)

    def test_sem_ficheiro_comeca_vazio_e_nao_cria_nada(self) -> None:
        caderno = self.caderno()
        self.assertEqual(caderno.factos(), ())
        self.assertFalse(caderno.cheio())
        self.assertFalse(self.caminho.exists())

    def test_acrescentar_grava_o_ficheiro_e_volta_a_ler(self) -> None:
        self.assertEqual(self.caderno().acrescentar("my favourite team is Benfica"), "My favourite team is Benfica.")
        self.caderno().acrescentar("I live in Braga.")
        self.assertEqual(self.ler(), {"versao": 1, "factos": ["My favourite team is Benfica.", "I live in Braga."]})
        self.assertEqual(self.caderno().factos(), ("My favourite team is Benfica.", "I live in Braga."))
        # Escrita atomica: nenhum ficheiro temporario fica para tras.
        self.assertEqual(sorted(p.name for p in self.caminho.parent.iterdir()), ["factos.json"])

    def test_facto_repetido_nao_duplica(self) -> None:
        caderno = self.caderno()
        caderno.acrescentar("I live in Braga")
        caderno.acrescentar("I live in Braga.")
        self.assertEqual(caderno.factos(), ("I live in Braga.",))

    def test_apagar(self) -> None:
        caderno = self.caderno()
        caderno.acrescentar("I live in Braga")
        caderno.acrescentar("My team is Benfica")
        self.assertTrue(caderno.apagar("I live in Braga."))
        self.assertFalse(caderno.apagar("I live in Braga."))
        self.assertEqual(self.ler()["factos"], ["My team is Benfica."])

    def test_segredos_e_dados_financeiros_nunca_sao_escritos(self) -> None:
        caderno = self.caderno()
        for facto in ("my password is hunter2", "my IBAN is PT50 1234", "I want to buy bitcoin", "", "   "):
            with self.subTest(facto=facto):
                with self.assertRaises(FactoRecusado):
                    caderno.acrescentar(facto)
        self.assertFalse(self.caminho.exists())

    def test_cheio_de_factos_nao_grava(self) -> None:
        caderno = self.caderno()
        for numero in range(50):
            caderno.acrescentar(f"fact number {numero} is fine")
        self.assertTrue(caderno.cheio())
        self.assertFalse(caderno.cabe("One more fact."))
        with self.assertRaises(CadernoCheio):
            caderno.acrescentar("one more fact")
        self.assertEqual(len(self.ler()["factos"]), 50)
        # Apagar um liberta lugar.
        caderno.apagar("Fact number 0 is fine.")
        self.assertFalse(caderno.cheio())
        caderno.acrescentar("one more fact")
        self.assertEqual(len(self.ler()["factos"]), 50)

    def test_cheio_de_caracteres_nao_grava(self) -> None:
        caderno = self.caderno()
        longo = "word " * 55  # ~275 caracteres por facto
        total = 0
        numero = 0
        while True:
            try:
                total += len(caderno.acrescentar(f"{numero} {longo}"))
            except CadernoCheio:
                break
            numero += 1
        self.assertLessEqual(total, 4000)
        self.assertLessEqual(sum(map(len, self.ler()["factos"])), 4000)
        self.assertLess(len(self.ler()["factos"]), 50)

    def test_cada_facto_e_cortado(self) -> None:
        facto = self.caderno().acrescentar("I like " + "very " * 200 + "long walks")
        self.assertLessEqual(len(facto), MAXIMO_POR_FACTO + 2)

    def test_limites_so_baixam(self) -> None:
        for extras in ({"maximo_factos": 51}, {"maximo_factos": 0}, {"maximo_caracteres": 4001}, {"maximo_factos": True}):
            with self.subTest(extras=extras):
                with self.assertRaises(ValueError):
                    self.caderno(**extras)
        caderno = CadernoDeFactos.da_config(ConfigMemoria(factos=2, caracteres=200), self.caminho)
        caderno.acrescentar("I live in Braga")
        caderno.acrescentar("My team is Benfica")
        with self.assertRaises(CadernoCheio):
            caderno.acrescentar("I like tea")

    def test_ficheiro_estragado_nao_derruba_e_e_posto_de_lado_na_escrita(self) -> None:
        self.caminho.parent.mkdir(parents=True)
        self.caminho.write_text("{nao e json", encoding="utf-8")
        caderno = self.caderno()
        self.assertEqual(caderno.factos(), ())
        self.assertTrue(any("nao se pode ler" in linha for linha in self.log))
        # Ler nao muda o ficheiro estragado.
        self.assertEqual(self.caminho.read_text(encoding="utf-8"), "{nao e json")
        caderno.acrescentar("I live in Braga")
        self.assertEqual(self.ler()["factos"], ["I live in Braga."])
        guardado = self.caminho.with_name("factos.json.estragado")
        self.assertEqual(guardado.read_text(encoding="utf-8"), "{nao e json")
        self.assertTrue(any("posto de lado" in linha for linha in self.log))

    def test_formatos_errados_e_ficheiro_grande_demais(self) -> None:
        self.caminho.parent.mkdir(parents=True)
        for conteudo in ('[1, 2]', '{"factos": "x"}', "null", " " * (MAXIMO_DO_FICHEIRO_BYTES + 1)):
            with self.subTest(conteudo=conteudo[:10]):
                self.log.clear()
                self.caminho.write_text(conteudo, encoding="utf-8")
                self.assertEqual(self.caderno().factos(), ())
                self.assertTrue(self.log)
        self.caminho.write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(self.caderno().factos(), ())

    def test_entradas_invalidas_ou_proibidas_editadas_a_mao_sao_ignoradas(self) -> None:
        self.caminho.parent.mkdir(parents=True)
        self.caminho.write_text(
            json.dumps({"versao": 1, "factos": ["I live in Braga.", 42, None, "my password is x", "I live in Braga."]}),
            encoding="utf-8",
        )
        self.assertEqual(self.caderno().factos(), ("I live in Braga.",))
        self.assertTrue(any("ignorada" in linha for linha in self.log))

    def test_contexto_nunca_passa_dos_limites_mesmo_com_ficheiro_maior(self) -> None:
        self.caminho.parent.mkdir(parents=True)
        factos = [f"Fact {numero} " + "x" * 150 + "." for numero in range(60)]
        self.caminho.write_text(json.dumps({"versao": 1, "factos": factos}), encoding="utf-8")
        caderno = self.caderno()
        contexto = caderno.factos_para_contexto()
        self.assertLessEqual(len(contexto), 50)
        self.assertLessEqual(sum(map(len, contexto)), 4000)
        self.assertTrue(caderno.cheio())
        pequeno = self.caderno(maximo_factos=3)
        self.assertEqual(len(pequeno.factos_para_contexto()), 3)


class TestFactoMaisParecido(unittest.TestCase):
    FACTOS = ("My favourite team is Benfica.", "My daughter is called Ana.", "I live in Braga.")

    def test_escolhe_o_mais_parecido(self) -> None:
        self.assertEqual(facto_mais_parecido("my favourite team", self.FACTOS), self.FACTOS[0])
        self.assertEqual(facto_mais_parecido("my daughter's name", self.FACTOS), self.FACTOS[1])
        self.assertEqual(facto_mais_parecido("I live in Braga", self.FACTOS), self.FACTOS[2])

    def test_nada_parecido_e_none(self) -> None:
        for dito in ("the weather", "", "I", "my cat is black"):
            with self.subTest(dito=dito):
                self.assertIsNone(facto_mais_parecido(dito, self.FACTOS))
        self.assertIsNone(facto_mais_parecido("Benfica", ()))


class TestTextoDoFacto(unittest.TestCase):
    def test_uma_linha_com_maiuscula_e_ponto(self) -> None:
        self.assertEqual(texto_do_facto("  i live in\nBraga!! "), "I live in Braga.")
        self.assertEqual(texto_do_facto("..."), "")


if __name__ == "__main__":
    unittest.main()


# --- Frases recentes e factos para o interprete local ----------------------------


class TestFrasesRecentes(unittest.TestCase):
    def setUp(self) -> None:
        self.relogio = Relogio()
        self.recentes = FrasesRecentes(relogio=self.relogio)

    def _acrescentar(self, n: int, **kw) -> int:
        valores = dict(intencao="ditar_prompt", projeto="atlas", prompt=f"Do task {n}.", estado="executado")
        valores.update(kw)
        return self.recentes.acrescentar(f"hey jarvis, in atlas do task {n}", **valores)

    def test_guarda_no_maximo_5_e_esquece_as_mais_antigas(self) -> None:
        for n in range(1, 8):
            self._acrescentar(n)
        frases = self.recentes.frases()
        self.assertEqual([f.prompt for f in frases], [f"Do task {n}." for n in range(3, 8)])
        self.assertEqual(frases[-1].frase, "in atlas do task 7", "sem a palavra de ativacao")
        self.assertEqual(frases[-1].feito, "done")

    def test_limites_de_3_a_5_e_da_config(self) -> None:
        for invalido in (2, 6, 0, True, 4.0, "5"):
            with self.subTest(invalido=invalido), self.assertRaises(ValueError):
                FrasesRecentes(invalido)
        recentes = FrasesRecentes.da_config(ConfigMemoria(frases_interprete=3))
        for n in range(5):
            recentes.acrescentar(f"phrase {n}", "ditar_prompt", None, "", None)
        self.assertEqual([f.frase for f in recentes.frases()], ["phrase 2", "phrase 3", "phrase 4"])

    def test_frase_recusada_fica_sem_texto_nem_prompt(self) -> None:
        self.recentes.acrescentar("buy 100 euros of bitcoin", INTENCAO_RECUSADA, None, "Buy bitcoin.", "recusado")
        (frase,) = self.recentes.frases()
        self.assertEqual((frase.frase, frase.prompt, frase.feito), ("", "", "refused"))

    def test_atualizar_so_a_frase_com_esse_numero(self) -> None:
        numero = self._acrescentar(1, estado="pendente")
        self.assertEqual(self.recentes.frases()[-1].feito, "waiting for yes")
        self._acrescentar(2)
        self.assertTrue(self.recentes.atualizar(numero, "executado", "Do task 1 now."))
        self.assertEqual([(f.prompt, f.feito) for f in self.recentes.frases()], [("Do task 1 now.", "done"), ("Do task 2.", "done")])
        for n in range(3, 9):
            self._acrescentar(n)
        self.assertFalse(self.recentes.atualizar(numero, "cancelado"), "ja saiu: nada muda")

    def test_expira_e_limpa(self) -> None:
        self._acrescentar(1)
        self.relogio.agora += 30 * 60 - 1
        self.assertEqual(len(self.recentes.frases()), 1)
        self.relogio.agora += 1
        self.assertEqual(self.recentes.frases(), ())
        self._acrescentar(2)
        self.assertEqual(self.recentes.limpar(), 1)
        self.assertEqual(self.recentes.frases(), ())


class TestFactosRelacionados(unittest.TestCase):
    FACTOS = (
        "My favourite team is Benfica.",
        "I live in Braga.",
        "My daughter is called Ana.",
        "Benfica games are on Sundays.",
        "I support Benfica since 1990.",
        "Benfica plays in red.",
    )

    def test_so_factos_com_uma_palavra_de_conteudo_em_comum_e_no_maximo_3(self) -> None:
        self.assertEqual(factos_relacionados("what's the weather in braga", self.FACTOS), ("I live in Braga.",))
        relacionados = factos_relacionados("did benfica win on sunday", self.FACTOS)
        self.assertEqual(len(relacionados), 3)
        self.assertTrue(all("Benfica" in facto for facto in relacionados))
        self.assertEqual(factos_relacionados("fix the login bug", self.FACTOS), ())
        self.assertEqual(factos_relacionados("hey jarvis", ("Jarvis is my assistant.",)), ())

    def test_contexto_do_interprete(self) -> None:
        self.assertIsNone(contexto_do_interprete("what time is it", FrasesRecentes(), None))
        recentes = FrasesRecentes()
        recentes.acrescentar("in atlas fix the header", "ditar_prompt", "atlas", "Fix the header.", "executado")
        contexto = contexto_do_interprete("send that to orbita too", recentes, None)
        self.assertEqual(len(contexto.frases), 1)
        self.assertEqual(contexto.factos, ())
