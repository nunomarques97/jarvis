r"""Testes da medicao do cerebro (scripts/medir_cerebro.py).

Nenhum teste chama o Claude real: o CLI e o `CliFalso` de tests.test_cerebro,
que fala o protocolo stream-json, e um guarda faz falhar o teste se o
`subprocess.Popen` verdadeiro for chamado. Sem som, sem microfone e sem rede.
O que protegem:

  * sem `--com-claude` o script recusa, nao arranca nenhum processo e nao
    escreve relatorio, mas imprime o teto garantido por troca;
  * o teto e calculado a partir de [cerebro] do config.toml e dos tetos fixos
    de jarvis/cerebro.py;
  * com `--com-claude`, cada modelo pedido (por omissao claude-haiku-4-5 e
    sonnet) corre a conversa sintetica fixa numa sessao propria, com primeiro
    texto, primeira frase, total, tokens e pesquisas por troca;
  * so vao ao Claude as frases, a localizacao e os factos ficticios do script;
  * o relatorio so e escrito na pasta de evidencia (ignorada pelo Git);
  * modelo invalido, saida fora da pasta, CLI indisponivel, turno falhado e
    processo que nao arranca dao codigos de saida distintos.

Corre com:

    .venv\Scripts\python -m unittest tests.test_medir_cerebro -v
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import inspect
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.cerebro import CONTEXTO_DURO_TOKENS, MAX_TURNS
from jarvis.config import ConfigCerebro
from tests.test_cerebro import CLI_FALSO, CliFalso, delta, init, inicio, resultado, seccao, uso_da_web

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


mc = _carregar_script("medir_cerebro")

QUANDO = datetime.datetime(2026, 9, 27, 18, 30, 0)


class RelogioFalso:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


class CliDaConversa(CliFalso):
    """Responde a cada frase sintetica; a do tempo usa a web e demora mais no relogio falso."""

    def __init__(self, relogio: RelogioFalso, *, falhar: str | None = None) -> None:
        super().__init__()
        self.relogio = relogio
        self.falhar = falhar

    def responder(self, processo, texto: str) -> None:
        fala = json.loads(seccao(texto, "SPEECH")[0])
        if self.falhar is not None and fala == self.falhar:
            processo.emitir(init(), inicio(), {"type": "result", "subtype": "error_during_execution", "is_error": True})
            return
        web = "weather" in fala or "tomorrow" in fala
        self.relogio.agora += 2.0 if web else 0.4
        resposta = "It is sunny in Lisbon. " if web else "Happy to help. "
        eventos = [init(), inicio()]
        if web:
            eventos.append(uso_da_web("w1"))
        eventos += [delta(resposta), delta("Anything else?"), resultado(resposta + "Anything else?")]
        processo.emitir(*eventos)


class CliQueNaoArranca(CliFalso):
    def __init__(self) -> None:
        super().__init__()
        self.pedidos = 0

    def __call__(self, argv, **kwargs):
        self.pedidos += 1
        raise FileNotFoundError("claude nao encontrado")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        # Guarda: o subprocess.Popen verdadeiro nunca pode ser chamado.
        self.popen_real: list = []

        def guarda(*args, **kwargs):
            self.popen_real.append(args)
            raise AssertionError("subprocess.Popen real chamado num teste")

        remendo = mock.patch.object(subprocess, "Popen", new=guarda)
        remendo.start()
        self.addCleanup(remendo.stop)
        temporaria = tempfile.TemporaryDirectory()
        self.addCleanup(temporaria.cleanup)
        self.base = Path(temporaria.name)
        self.prova = self.base / "evidence"
        self.projeto = self.base / "projeto-atlas"
        self.projeto.mkdir()
        self.config = self.base / "config.toml"
        self.escrever_config("[cerebro]\nlimite_s = 30\ncontexto_max_tokens = 8000\n")
        self.relogio = RelogioFalso()

    def tearDown(self) -> None:
        self.assertEqual(self.popen_real, [], "o subprocess.Popen real foi chamado")

    def escrever_config(self, extra: str) -> None:
        self.config.write_text(
            f'[microfone]\nnome = "M"\n\n[[projetos]]\nnome = "atlas"\ncaminho = "{self.projeto.as_posix()}"\n\n'
            + extra,
            encoding="utf-8",
        )

    def correr(self, *argv: str, cli=None, arrancar=None, executavel: str = CLI_FALSO) -> tuple[int, str, str]:
        saida, erros = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(erros):
            codigo = mc.main(
                ["--config", str(self.config), *argv],
                arrancar=arrancar,
                cli=executavel,
                versao=lambda _cli: "9.9.9 (falso)",
                agora=lambda: QUANDO,
                relogio=self.relogio,
                pasta=self.base / "neutra",
                pasta_de_prova=self.prova,
            )
        return codigo, saida.getvalue(), erros.getvalue()

    def relatorios(self) -> list[Path]:
        return sorted(self.prova.glob("*.md")) if self.prova.is_dir() else []


class TestSemComClaude(_Base):
    def test_recusa_nao_arranca_nada_e_imprime_o_teto(self) -> None:
        cli = CliDaConversa(self.relogio)
        codigo, saida, erros = self.correr(arrancar=cli)
        self.assertEqual(codigo, mc.CODIGO_RECUSADO)
        self.assertEqual(cli.processos, [], "nenhum processo arrancado")
        self.assertIn("Recusado", erros)
        self.assertIn("--com-claude", erros)
        self.assertIn(f"{MAX_TURNS * CONTEXTO_DURO_TOKENS} tokens", saida)
        self.assertEqual(self.relatorios(), [], "nada escrito")

    def test_recusa_mesmo_sem_arrancar_injetado(self) -> None:
        # Sem `arrancar` o script usaria o subprocess.Popen real: o guarda prova que nao o chama.
        codigo, _saida, _erros = self.correr()
        self.assertEqual(codigo, mc.CODIGO_RECUSADO)


class TestTeto(_Base):
    def test_teto_por_troca_a_partir_da_configuracao(self) -> None:
        teto = mc.teto_por_troca(ConfigCerebro(limite_s=42.0, contexto_max_tokens=12000))
        self.assertEqual(teto.entrada_maxima, MAX_TURNS * CONTEXTO_DURO_TOKENS)
        self.assertEqual(teto.entrada_maxima, 128000)
        self.assertEqual((teto.usos_web, teto.caracteres), (2, 1500))
        texto = "\n".join(mc.linhas_do_teto(teto))
        self.assertIn("at most 42 s", texto)
        self.assertIn("passes 12000 tokens", texto)

    def test_o_teto_impresso_usa_o_cerebro_do_config(self) -> None:
        _codigo, saida, _erros = self.correr()
        self.assertIn("at most 30 s ([cerebro] limite_s)", saida)
        self.assertIn("passes 8000 tokens ([cerebro] contexto_max_tokens)", saida)

    def test_config_em_falta_usa_os_valores_por_omissao(self) -> None:
        self.config.unlink()
        _codigo, saida, erros = self.correr()
        self.assertIn("AVISO", erros)
        self.assertIn(f"at most {ConfigCerebro().limite_s:g} s", saida)


class TestMedir(_Base):
    def test_cada_modelo_corre_a_conversa_numa_sessao_propria(self) -> None:
        cli = CliDaConversa(self.relogio)
        codigo, saida, erros = self.correr("--com-claude", arrancar=cli)
        self.assertEqual(codigo, mc.CODIGO_OK, saida + erros)
        self.assertEqual(len(cli.processos), 2, "um processo por modelo")
        modelos = [p.argv[p.argv.index("--model") + 1] for p in cli.processos]
        self.assertEqual(modelos, list(mc.MODELOS_POR_OMISSAO))
        self.assertEqual(mc.MODELOS_POR_OMISSAO, ("claude-haiku-4-5", "sonnet"))
        for processo in cli.processos:
            self.assertEqual(
                [json.loads(seccao(m, "SPEECH")[0]) for m in processo.mensagens],
                [t.frase for t in mc.TROCAS_SINTETICAS],
            )
            self.assertNotIn("Bash", processo.argv[processo.argv.index("--tools") + 1])
        self.assertEqual(cli.fechados, 2, "cada sessao fecha no fim")

    def test_so_dados_ficticios_vao_ao_claude(self) -> None:
        cli = CliDaConversa(self.relogio)
        self.correr("--com-claude", "--modelos", "claude-haiku-4-5", arrancar=cli)
        mensagens = cli.processos[0].mensagens
        self.assertIn('{"location": "Lisbon, Portugal"}', seccao(mensagens[0], "DATA"))
        self.assertEqual(seccao(mensagens[0], "FACTS"), [json.dumps(f) for f in mc.FACTOS_FICTICIOS])
        self.assertEqual(seccao(mensagens[1], "FACTS"), [], "os factos so vao na primeira mensagem da sessao")
        for mensagem in mensagens:
            self.assertNotIn("atlas", mensagem)
            self.assertNotIn(str(self.base), mensagem)

    def test_mede_primeiro_texto_primeira_frase_total_e_tokens(self) -> None:
        cli = CliDaConversa(self.relogio)
        codigo, _saida, _erros = self.correr("--com-claude", "--modelos", "claude-haiku-4-5", arrancar=cli)
        self.assertEqual(codigo, mc.CODIGO_OK)
        [relatorio] = self.relatorios()
        self.assertEqual(relatorio.name, "medir-cerebro-20260927-183000.md")
        texto = relatorio.read_text(encoding="utf-8")
        self.assertIn("| `claude-haiku-4-5` | social | no | respondido | 0.40 s | 0.40 s | 0.40 s | 12 | 4800 | 300 | 40 | 1 |", texto)
        self.assertIn("| `claude-haiku-4-5` | weather | 1 use(s) | respondido | 2.00 s | 2.00 s | 2.00 s |", texto)
        self.assertIn("| `claude-haiku-4-5` | 8 of 8 |", texto)
        self.assertIn("p50 0.40 s · p95 0.40 s · n=6", texto, "primeira frase sem web")
        self.assertIn("p50 2.00 s · p95 2.00 s · n=2", texto, "primeira frase com web")
        self.assertIn("**128000 tokens**", texto)
        self.assertIn("typical input without web 5112 tokens (within the planned ~10000)", texto)
        self.assertIn("every exchange within the guaranteed ceiling: yes", texto)
        self.assertIn('- **cook-follow-up** "The first dish?": Happy to help. Anything else?', texto)

    def test_medicao_de_uma_troca(self) -> None:
        from jarvis.cerebro import Cerebro

        cli = CliDaConversa(self.relogio)
        cerebro = Cerebro(
            ConfigCerebro(limite_s=5.0), "en", cli=CLI_FALSO, arrancar=cli, pasta=self.base / "neutra", relogio=self.relogio
        )
        self.addCleanup(cerebro.fechar)
        troca = mc.TROCAS_SINTETICAS[5]
        medicao = mc.medir_troca(cerebro, troca, self.relogio)
        self.assertTrue(medicao.com_pesquisa)
        self.assertEqual((medicao.primeira_frase_s, medicao.total_s), (2.0, 2.0))
        self.assertEqual(medicao.resultado.primeiro_texto_s, 2.0)
        self.assertEqual(medicao.entrada_total, 12 + 4800 + 300)
        self.assertEqual(medicao.uso("web_search_requests"), 1)


class TestFalhas(_Base):
    def test_modelo_invalido_e_recusado_sem_arrancar(self) -> None:
        cli = CliDaConversa(self.relogio)
        for modelos in ("Claude Haiku", "haiku;rm", ","):
            with self.subTest(modelos=modelos):
                codigo, _saida, erros = self.correr("--com-claude", "--modelos", modelos, arrancar=cli)
                self.assertEqual(codigo, mc.CODIGO_RECUSADO)
                self.assertIn("ERRO", erros)
        self.assertEqual(cli.processos, [])

    def test_saida_fora_da_pasta_de_evidencia_e_recusada(self) -> None:
        cli = CliDaConversa(self.relogio)
        for saida in ("notas.md", str(self.base / "fora.md"), str(self.prova / "x.txt")):
            with self.subTest(saida=saida):
                codigo, _saida, erros = self.correr("--com-claude", "--saida", saida, arrancar=cli)
                self.assertEqual(codigo, mc.CODIGO_RECUSADO)
                self.assertIn("ERRO", erros)
        self.assertEqual(cli.processos, [])
        self.assertEqual(self.relatorios(), [])

    def test_saida_dentro_da_pasta_de_evidencia(self) -> None:
        cli = CliDaConversa(self.relogio)
        destino = self.prova / "comparacao.md"
        codigo, saida, _erros = self.correr("--com-claude", "--modelos", "sonnet", "--saida", str(destino), arrancar=cli)
        self.assertEqual(codigo, mc.CODIGO_OK)
        self.assertTrue(destino.is_file())
        self.assertIn("relatorio:", saida)

    def test_cli_indisponivel_nao_mede_nada(self) -> None:
        cli = CliDaConversa(self.relogio)
        codigo, _saida, erros = self.correr("--com-claude", arrancar=cli, executavel="C:/ficticio/claude.cmd")
        self.assertEqual(codigo, mc.CODIGO_SEM_CLI)
        self.assertIn("nada foi medido", erros)
        self.assertEqual(cli.processos, [])
        self.assertEqual(self.relatorios(), [])

    def test_um_turno_falhado_da_codigo_1_e_o_relatorio_diz_qual(self) -> None:
        cli = CliDaConversa(self.relogio, falhar="The first dish?")
        codigo, _saida, _erros = self.correr("--com-claude", "--modelos", "claude-haiku-4-5", arrancar=cli)
        self.assertEqual(codigo, mc.CODIGO_FALHOU)
        texto = self.relatorios()[0].read_text(encoding="utf-8")
        self.assertIn("| `claude-haiku-4-5` | 7 of 8 |", texto)
        self.assertIn("| cook-follow-up | no | falhou |", texto)

    def test_processo_que_nao_arranca(self) -> None:
        cli = CliQueNaoArranca()
        codigo, saida, _erros = self.correr("--com-claude", "--modelos", "claude-haiku-4-5", arrancar=cli)
        self.assertEqual(codigo, mc.CODIGO_FALHOU)
        self.assertEqual(cli.pedidos, 1, "nao tenta cada troca depois de nao arrancar")
        self.assertIn("nao arrancou", saida)
        self.assertIn("(did not start)", self.relatorios()[0].read_text(encoding="utf-8"))


class TestPastaIgnorada(unittest.TestCase):
    def test_o_relatorio_por_omissao_vai_para_a_pasta_ignorada(self) -> None:
        omissao = inspect.signature(mc.main).parameters["pasta_de_prova"].default
        self.assertEqual(omissao, RAIZ / "docs" / "forja" / "evidence")
        ignoradas = (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("docs/forja/", ignoradas)


if __name__ == "__main__":
    unittest.main()
