r"""Testes da medicao do gasto das perguntas gerais (scripts/medir_gasto_perguntas.py).

Nenhum teste chama o Claude Code real: o processo e o `Popen` falso de
tests/test_pergunta_geral.py, que regista o que recebe por stdin. Sem som,
sem microfone e sem rede. O que protegem:

  * os tres cenarios (sem memoria, 5 trocas, 10 trocas e caderno cheio) vem
    das classes reais da memoria e respeitam os limites de [memoria];
  * o teto garantido do contexto nunca e passado, nem com texto que escapa;
  * o gasto (`usage`, custo, pesquisas) e lido, e a falta dele fica "-";
  * so texto ficticio vai para o Claude: nem a localizacao, nem os projetos;
  * sem --gasta-quota ou sem o CLI nada arranca e nada e escrito.

Corre com:

    .venv\Scripts\python -m unittest tests.test_medir_gasto_perguntas -v
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.config import Config, ConfigMemoria, ConfigPerguntas, Projeto
from jarvis.memoria import (
    MAXIMO_DA_PERGUNTA,
    MAXIMO_DA_RESPOSTA,
    HistoricoDePerguntas,
    recusa_do_facto,
    texto_do_facto,
)
from jarvis.pergunta_geral import contexto_da_memoria, texto_do_pedido
from tests.test_pergunta_geral import CLI_FALSO, ArranqueFalso, saida_json

RAIZ = Path(__file__).resolve().parent.parent


def _carregar_script(nome: str):
    chave = f"_jarvis_scripts_{nome}"
    if chave in sys.modules:
        return sys.modules[chave]
    spec = importlib.util.spec_from_file_location(chave, RAIZ / "scripts" / f"{nome}.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[chave] = modulo
    spec.loader.exec_module(modulo)
    return modulo


mg = _carregar_script("medir_gasto_perguntas")

USO = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 3400,
    "cache_read_input_tokens": 15000,
    "output_tokens": 85,
    "server_tool_use": {"web_search_requests": 1},
}
LOCAL_PRIVADO = "Rua Privada 42, Aldeia Escondida"
PROJETO_PRIVADO = "orbita-privada"


def saida_com_uso(texto: str = "It launched on 25 December 2021.") -> str:
    return saida_json(texto, usage=USO, total_cost_usd=0.0123, duration_ms=4321)


class _ComPasta(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def cenarios(self, memoria: ConfigMemoria | None = None):
        return mg.construir_cenarios(memoria or ConfigMemoria(), self.pasta / "caderno")


class TestCenarios(_ComPasta):
    def test_tres_cenarios_com_os_limites_por_omissao(self) -> None:
        sem, cinco, cheio = self.cenarios()
        self.assertEqual((sem.nome, len(sem.trocas), len(sem.factos)), ("sem-memoria", 0, 0))
        self.assertEqual(sem.pergunta, mg.PERGUNTA_SOZINHA)
        self.assertEqual((cinco.nome, len(cinco.trocas), len(cinco.factos)), ("seguimento-5", 5, 0))
        self.assertEqual((cheio.nome, len(cheio.trocas)), ("seguimento-10-cheio", 10))
        self.assertEqual(len(cheio.factos), 50)
        self.assertEqual(sum(map(len, cheio.factos)), 4000)
        for cenario in (cinco, cheio):
            self.assertEqual(cenario.pergunta, mg.PERGUNTA_DE_SEGUIMENTO)
            self.assertTrue(cenario.trocas[-1].pergunta.startswith("What is the James Webb Space Telescope?"))

    def test_o_pior_caso_leva_cada_troca_ao_tamanho_maximo(self) -> None:
        cheio = self.cenarios()[2]
        for troca in cheio.trocas:
            self.assertGreaterEqual(len(troca.pergunta), MAXIMO_DA_PERGUNTA - 20)
            self.assertLessEqual(len(troca.pergunta), MAXIMO_DA_PERGUNTA + 1)
            self.assertGreaterEqual(len(troca.resposta), MAXIMO_DA_RESPOSTA - 20)
            self.assertLessEqual(len(troca.resposta), MAXIMO_DA_RESPOSTA + 1)

    def test_factos_distintos_gravaveis_e_ficticios(self) -> None:
        factos = self.cenarios()[2].factos
        self.assertEqual(len(set(factos)), len(factos))
        for facto in factos:
            self.assertEqual(texto_do_facto(facto), facto)
            self.assertIsNone(recusa_do_facto(facto))
            self.assertTrue(facto.startswith("Fact "))

    def test_limites_baixados_dao_cenarios_menores(self) -> None:
        memoria = ConfigMemoria(trocas=3, factos=20, caracteres=500)
        _sem, cinco, cheio = self.cenarios(memoria)
        self.assertEqual(len(cinco.trocas), 3)
        self.assertEqual(len(cheio.trocas), 3)
        self.assertEqual(len(cheio.factos), 20)
        self.assertEqual(sum(map(len, cheio.factos)), 500)

    def test_limites_minimos_ainda_dao_factos_distintos(self) -> None:
        cheio = self.cenarios(ConfigMemoria(factos=50, caracteres=200))[2]
        self.assertEqual(len(cheio.factos), 50)
        self.assertEqual(len(set(cheio.factos)), 50)
        self.assertEqual(sum(map(len, cheio.factos)), 200)

    def test_o_caderno_ficticio_fica_na_pasta_dada(self) -> None:
        self.cenarios()
        self.assertTrue((self.pasta / "caderno" / "factos.json").is_file())


class TestTeto(_ComPasta):
    def test_os_cenarios_ficam_dentro_do_teto(self) -> None:
        teto = mg.teto_do_contexto(ConfigMemoria(), ConfigPerguntas())
        self.assertLessEqual(teto.contexto_texto_normal, teto.contexto_garantido)
        self.assertLess(teto.contexto_garantido, teto.pedido_garantido)
        for cenario in self.cenarios():
            contexto = len(contexto_da_memoria(cenario.trocas, cenario.factos))
            self.assertLessEqual(contexto, teto.contexto_texto_normal)

    def test_texto_que_escapa_nunca_passa_o_teto_garantido(self) -> None:
        memoria = ConfigMemoria()
        teto = mg.teto_do_contexto(memoria, ConfigPerguntas())
        sorteio = random.Random(7)
        letras = '"\\aé€ \t\n{}<>' + "\u2028"
        for _ in range(20):
            historico = HistoricoDePerguntas.da_config(memoria)
            for _ in range(15):
                historico.acrescentar(
                    "".join(sorteio.choice(letras) for _ in range(900)),
                    "".join(sorteio.choice(letras) for _ in range(1500)),
                )
            factos = ["".join(sorteio.choice(letras) for _ in range(80)) for _ in range(memoria.factos)]
            contexto = contexto_da_memoria(historico.trocas(), factos)
            self.assertLessEqual(len(contexto), teto.contexto_garantido)

    def test_limites_mais_baixos_baixam_o_teto(self) -> None:
        cheio = mg.teto_do_contexto(ConfigMemoria(), ConfigPerguntas())
        baixo = mg.teto_do_contexto(ConfigMemoria(trocas=2, factos=5, caracteres=300), ConfigPerguntas())
        self.assertLess(baixo.contexto_garantido, cheio.contexto_garantido)
        self.assertLess(baixo.pedido_garantido, cheio.pedido_garantido)
        self.assertEqual((baixo.trocas, baixo.factos, baixo.caracteres), (2, 5, 300))


class TestMedir(_ComPasta):
    def perguntas(self, *saidas):
        arranque = ArranqueFalso(*saidas)
        perguntas = mg.PerguntasGerais(
            mg.configuracao_ficticia(ConfigPerguntas()),
            "en",
            pastas_proibidas=(RAIZ,),
            cli=CLI_FALSO,
            arrancar=arranque,
            pasta=self.pasta / "neutra",
            hoje=lambda: datetime.date(2026, 9, 26),
        )
        return arranque, perguntas

    def test_le_o_uso_o_custo_e_as_pesquisas(self) -> None:
        arranque, perguntas = self.perguntas(saida_com_uso())
        medicao = mg.medir(perguntas, self.cenarios()[1])
        self.assertTrue(medicao.resultado.respondida)
        self.assertEqual(medicao.uso("input_tokens"), 12)
        self.assertEqual(medicao.uso("cache_creation_input_tokens"), 3400)
        self.assertEqual(medicao.uso("cache_read_input_tokens"), 15000)
        self.assertEqual(medicao.uso("output_tokens"), 85)
        self.assertEqual(medicao.uso("web_search_requests"), 1)
        self.assertEqual(medicao.entrada_total, 12 + 3400 + 15000)
        self.assertEqual(medicao.resultado.custo_usd, 0.0123)
        self.assertEqual(medicao.resultado.duracao_ms, 4321)
        self.assertEqual(medicao.caracteres_do_pedido, len(arranque.ultimo.entrada))
        self.assertGreater(medicao.resultado.caracteres_do_contexto, 0)

    def test_sem_uso_fica_sem_numeros(self) -> None:
        _arranque, perguntas = self.perguntas(saida_json("It launched in 2021."))
        medicao = mg.medir(perguntas, self.cenarios()[0])
        self.assertTrue(medicao.resultado.respondida)
        self.assertIsNone(medicao.resultado.uso)
        self.assertIsNone(medicao.entrada_total)
        self.assertIsNone(medicao.uso("output_tokens"))
        self.assertIsNone(medicao.resultado.custo_usd)
        self.assertEqual(medicao.resultado.caracteres_do_contexto, 0)
        linhas = mg.linhas_do_resumo(
            [medicao],
            mg.teto_do_contexto(ConfigMemoria(), ConfigPerguntas()),
            modelo="claude-haiku-4-5",
            versao="9.9.9",
            lingua="en",
            quando=datetime.datetime(2026, 9, 26, 12, 0),
        )
        linha = next(l for l in linhas if l.startswith("| sem-memoria"))
        celulas = [c.strip() for c in linha.strip("|").split("|")]
        self.assertEqual(celulas[5], "respondida")
        self.assertEqual(celulas[6:13], ["-"] * 7)

    def test_uso_mal_formado_nao_estraga_a_medicao(self) -> None:
        saida = saida_json("It launched in 2021.", usage={"input_tokens": "muitos", "output_tokens": -3})
        _arranque, perguntas = self.perguntas(saida)
        medicao = mg.medir(perguntas, self.cenarios()[0])
        self.assertTrue(medicao.resultado.respondida)
        self.assertIsNone(medicao.uso("input_tokens"))
        self.assertIsNone(medicao.entrada_total)


def config_privada(pasta: Path) -> Config:
    return Config(
        microfone="Microfone privado",
        projetos=(Projeto(PROJETO_PRIVADO, pasta / PROJETO_PRIVADO),),
        perguntas=ConfigPerguntas(modelo="claude-haiku-4-5", limite_s=45.0, localizacao=LOCAL_PRIVADO),
        memoria=ConfigMemoria(),
    )


class TestMain(_ComPasta):
    def correr(self, argv, arranque, *, config=None, cli=CLI_FALSO):
        saida, erros = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(mg, "carregar_config", return_value=config or config_privada(self.pasta)),
            mock.patch.object(mg, "forcar_consola_utf8"),
            mock.patch.object(mg, "PASTA_DE_PROVA", self.pasta / "evidencia"),
            contextlib.redirect_stdout(saida),
            contextlib.redirect_stderr(erros),
        ):
            codigo = mg.main(
                argv,
                arrancar=arranque,
                cli=cli,
                versao=lambda _cli: "9.9.9 (Claude Code)",
                agora=lambda: datetime.datetime(2026, 9, 26, 12, 30, 0),
            )
        return codigo, saida.getvalue(), erros.getvalue()

    def test_sem_a_flag_recusa_e_nada_arranca(self) -> None:
        arranque = ArranqueFalso(saida_com_uso())
        codigo, _saida, erros = self.correr([], arranque)
        self.assertEqual(codigo, 2)
        self.assertIn("--gasta-quota", erros)
        self.assertEqual(arranque.processos, [])
        self.assertFalse((self.pasta / "evidencia").exists())

    def test_sem_cli_diz_que_nao_esta_disponivel_e_nada_arranca(self) -> None:
        arranque = ArranqueFalso(saida_com_uso())
        with mock.patch.object(mg, "localizar_cli", side_effect=FileNotFoundError("claude.exe nao encontrado")):
            codigo, _saida, erros = self.correr(["--gasta-quota"], arranque, cli=None)
        self.assertEqual(codigo, 3)
        self.assertIn("nao esta disponivel", erros)
        self.assertEqual(arranque.processos, [])
        self.assertFalse((self.pasta / "evidencia").exists())

    def test_um_shim_cmd_e_recusado_como_cli_indisponivel(self) -> None:
        arranque = ArranqueFalso(saida_com_uso())
        codigo, _saida, _erros = self.correr(["--gasta-quota"], arranque, cli="C:/ficticio/claude.cmd")
        self.assertEqual(codigo, 3)
        self.assertEqual(arranque.processos, [])

    def test_tres_perguntas_so_com_texto_ficticio(self) -> None:
        arranque = ArranqueFalso(saida_com_uso())
        codigo, saida, _erros = self.correr(["--gasta-quota"], arranque)
        self.assertEqual(codigo, 0, saida)
        self.assertEqual(len(arranque.processos), 3)
        ficticia = mg.configuracao_ficticia(config_privada(self.pasta).perguntas)
        self.assertEqual(ficticia.limite_s, 45.0)
        cenarios = self.cenarios()
        for processo, cenario in zip(arranque.processos, cenarios):
            self.assertIn("claude-haiku-4-5", processo.argv)
            self.assertNotIn(LOCAL_PRIVADO, processo.entrada)
            self.assertNotIn(PROJETO_PRIVADO, processo.entrada)
            self.assertNotIn("Microfone privado", processo.entrada)
            esperado = texto_do_pedido(
                cenario.pergunta,
                ficticia,
                "en",
                datetime.date.today(),
                trocas=cenario.trocas,
                factos=cenario.factos,
            )
            self.assertEqual(processo.entrada, esperado)
            for argumento in processo.argv:
                self.assertNotIn(LOCAL_PRIVADO, argumento)

    def test_o_resumo_tem_os_numeros_e_o_teto(self) -> None:
        arranque = ArranqueFalso(saida_com_uso())
        self.correr(["--gasta-quota"], arranque)
        resumo = (self.pasta / "evidencia" / "gasto-perguntas-20260926-123000.md").read_text(encoding="utf-8")
        teto = mg.teto_do_contexto(ConfigMemoria(), config_privada(self.pasta).perguntas)
        for nome in ("sem-memoria", "seguimento-5", "seguimento-10-cheio"):
            self.assertIn(f"| {nome} |", resumo)
        self.assertIn("| 12 | 3400 | 15000 | 18412 | 85 | 0.0123 | 1 |", resumo)
        self.assertIn(f"{teto.contexto_garantido} caracteres", resumo)
        self.assertIn(f"{teto.pedido_garantido} caracteres", resumo)
        self.assertIn("9.9.9 (Claude Code)", resumo)
        self.assertIn("respondidas: 3 de 3", resumo)
        self.assertIn("dentro do teto garantido: sim", resumo)
        self.assertIn("It launched on 25 December 2021.", resumo)
        self.assertNotIn("não verificada", resumo)
        self.assertNotIn(LOCAL_PRIVADO, resumo)
        self.assertNotIn(PROJETO_PRIVADO, resumo)

    def test_uma_falha_fica_no_resumo_e_sai_com_erro(self) -> None:
        falha = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True})
        arranque = ArranqueFalso(saida_com_uso(), falha, saida_com_uso())
        destino = self.pasta / "resumo.md"
        codigo, _saida, _erros = self.correr(["--gasta-quota", "--saida", str(destino)], arranque)
        self.assertEqual(codigo, 1)
        resumo = destino.read_text(encoding="utf-8")
        self.assertIn("| seguimento-5 | 5 | 0 |", resumo)
        self.assertIn("falhou", resumo)
        self.assertIn("respondidas: 2 de 3", resumo)


if __name__ == "__main__":
    unittest.main()
