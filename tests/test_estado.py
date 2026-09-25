"""Estado e relatorio de um projeto por voz, com CLIs falsos (so leitura).

Os CLIs falsos sao scripts Python corridos como subprocessos a serio: o
"node" e o proprio Python, que corre um `bin/forja.mjs` falso escrito em
Python; o "claude" e o "git" sao scripts ao lado. Cada um regista o argv e o
cwd em `chamadas.jsonl` e responde o que o `cenario.json` do teste manda.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis.config import Config, ConfigForja, Projeto
from jarvis.confirmacao import contar_frases
from jarvis.estado import (
    Estado,
    SaidaDoCli,
    compor_estado,
    correr_cli,
    encontrar_relatorio,
    ler_estado_do_run,
    ler_sessoes,
    resumo_do_markdown,
)

# --- CLIs falsos ------------------------------------------------------------

SCRIPT_FALSO = r'''
import json, os, sys, time
from pathlib import Path

NOME = {nome!r}
BASE = Path({base!r})
argumentos = sys.argv[1:]


def registar(dados):
    # Os CLIs correm em paralelo; no Windows o modo "a" nao e atomico e uma
    # linha pode apagar a outra, por isso cada escrita toma um trinco exclusivo.
    trinco = BASE / "chamadas.trinco"
    limite = time.monotonic() + 5.0
    fd = None
    while fd is None and time.monotonic() < limite:
        try:
            fd = os.open(trinco, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            time.sleep(0.005)
    try:
        with open(BASE / "chamadas.jsonl", "a", encoding="utf-8") as ficheiro:
            ficheiro.write(json.dumps(dados) + "\n")
    finally:
        if fd is not None:
            os.close(fd)
            os.remove(trinco)


if NOME == "forja":
    chave = "start" if argumentos[:1] == ["start"] else " ".join(argumentos[:2])
else:
    chave = NOME
registar({{"programa": NOME, "argv": argumentos, "cwd": os.getcwd()}})
cenario = json.loads((BASE / "cenario.json").read_text(encoding="utf-8")).get(chave, {{}})
sys.stdout.write(cenario.get("saida", ""))
sys.stderr.write(cenario.get("erro", ""))
sys.stdout.flush()
sys.stderr.flush()
try:
    fim = time.monotonic() + cenario.get("dormir", 0)
    while time.monotonic() < fim:
        time.sleep(0.05)
except KeyboardInterrupt:
    registar({{"programa": NOME, "evento": "ctrl-c"}})
    sys.exit(130)
sys.exit(cenario.get("codigo", 0))
'''


def estado_json(
    status: str = "running",
    *,
    tasks=(("T1", "done", "Primeira"), ("T2", "develop", "Segunda task")),
    pending: str | None = "T2",
    recovery: str | None = None,
    technology=(),
    goal: str = "OBJETIVO-PRIVADO-NUNCA-DITO",
) -> str:
    return json.dumps(
        {
            "run": "F-1790000000000-abc123",
            "goal": goal,
            "provider": "claude",
            "status": status,
            "failure": "texto antigo de falha C:\\privado\\caminho",
            "recovery": {"code": recovery, "title": "x", "guidance": "y"} if recovery else None,
            "pending": {"task": pending, "phase": "develop"} if pending else None,
            "technology": list(technology),
            "tasks": [{"id": i, "title": t, "status": s} for i, s, t in tasks],
        }
    )


def sessoes_json(pasta: Path, *estados: str, fora: int = 0) -> str:
    itens = [
        {"pid": 100 + n, "cwd": str(pasta), "kind": "interactive", "sessionId": "s", "name": "NOME-PRIVADO", "status": e}
        for n, e in enumerate(estados)
    ]
    itens += [{"pid": 900 + n, "cwd": str(pasta.parent / "outro"), "kind": "background", "status": "busy"} for n in range(fora)]
    return json.dumps(itens)


SEM_RUN = {
    "codigo": 1,
    "erro": "forja: ENOENT: no such file or directory, open 'C:\\x\\.forja\\current.json'",
}


class CLIsFalsos:
    """Uma instalacao da FORJA, um claude e um git falsos, numa pasta temporaria."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.forja = base / "forja"
        (self.forja / "bin").mkdir(parents=True)
        (self.forja / "bin" / "forja.mjs").write_text(
            SCRIPT_FALSO.format(nome="forja", base=str(base)), encoding="utf-8"
        )
        self.claude = base / "claude_falso.py"
        self.claude.write_text(SCRIPT_FALSO.format(nome="claude", base=str(base)), encoding="utf-8")
        self.git = base / "git_falso.py"
        self.git.write_text(SCRIPT_FALSO.format(nome="git", base=str(base)), encoding="utf-8")
        self.perfil = base / "perfil.json"
        self.perfil.write_text("{}", encoding="utf-8")
        self.projeto = base / "projetos" / "atlas"
        self.projeto.mkdir(parents=True)
        self.cenario({})

    def cenario(self, dados: dict) -> None:
        (self.base / "cenario.json").write_text(json.dumps(dados), encoding="utf-8")

    def chamadas(self) -> list[dict]:
        caminho = self.base / "chamadas.jsonl"
        if not caminho.exists():
            return []
        return [json.loads(linha) for linha in caminho.read_text(encoding="utf-8").splitlines() if linha]

    def config(self, *, com_forja: bool = True) -> Config:
        return Config(
            microfone="Microfone de teste",
            projetos=(Projeto("atlas", self.projeto.resolve()),),
            forja=ConfigForja(self.forja.resolve(), self.perfil.resolve(), "claude") if com_forja else None,
        )

    def estado(self, *, com_forja: bool = True, limite_s: float = 20.0, **ajustes) -> Estado:
        return Estado(
            self.config(com_forja=com_forja),
            comando_node=[sys.executable],
            comando_claude=[sys.executable, str(self.claude)],
            limite_s=limite_s,
            **ajustes,
        )


class _ComCLIsFalsos(unittest.TestCase):
    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.clis = CLIsFalsos(Path(pasta.name))


def _retrato(pasta: Path) -> dict[str, str]:
    return {
        str(c.relative_to(pasta)): hashlib.sha256(c.read_bytes()).hexdigest() if c.is_file() else "pasta"
        for c in sorted(pasta.rglob("*"))
    }


# --- Estado -----------------------------------------------------------------


class TestEstadoDoRun(_ComCLIsFalsos):
    def test_run_a_correr_em_ate_tres_frases_com_task_e_sessoes(self) -> None:
        self.clis.cenario(
            {"core status": {"saida": estado_json()}, "claude": {"saida": sessoes_json(self.clis.projeto, "busy", "idle")}}
        )
        resposta = self.clis.estado().estado("atlas")
        self.assertLessEqual(contar_frases(resposta.falado), 3)
        self.assertIn("O run do atlas está a correr, 1 de 2 tasks feitas.", resposta.falado)
        self.assertIn("Vai na T2, Segunda task.", resposta.falado)
        self.assertIn("1 a trabalhar e 1 parada", resposta.falado)
        self.assertTrue(resposta.feito)

    def test_argv_e_cwd_dos_dois_clis(self) -> None:
        self.clis.cenario({"core status": {"saida": estado_json()}, "claude": {"saida": "[]"}})
        self.clis.estado().estado("atlas")
        chamadas = {c["programa"]: c for c in self.clis.chamadas()}
        self.assertEqual(chamadas["forja"]["argv"], ["core", "status"])
        self.assertEqual(chamadas["claude"]["argv"], ["agents", "--json", "--cwd", str(self.clis.projeto.resolve())])
        for chamada in chamadas.values():
            self.assertEqual(Path(chamada["cwd"]).resolve(), self.clis.projeto.resolve())

    def test_bloqueado_diz_onde_parou_e_o_bloqueio(self) -> None:
        self.clis.cenario(
            {
                "core status": {"saida": estado_json("blocked", recovery="attempts")},
                "claude": {"saida": sessoes_json(self.clis.projeto, "idle")},
            }
        )
        falado = self.clis.estado().estado("atlas").falado
        self.assertEqual(
            falado,
            "O run do atlas está parado, 1 de 2 tasks feitas. Parou na T2, Segunda task. "
            "Bloqueio: esgotou as tentativas da task.",
        )
        self.assertEqual(contar_frases(falado), 3)

    def test_interrompido_e_decisao_pendente(self) -> None:
        self.clis.cenario({"core status": {"saida": estado_json(recovery="interrupted")}, "claude": {"saida": "[]"}})
        self.assertIn("foi interrompido", self.clis.estado().estado("atlas").falado)
        self.clis.cenario(
            {
                "core status": {"saida": estado_json("blocked", technology=[{"id": "D1"}])},
                "claude": {"saida": "[]"},
            }
        )
        self.assertIn("decisão tua sobre tecnologia", self.clis.estado().estado("atlas").falado)

    def test_run_terminado_nao_fala_de_task(self) -> None:
        self.clis.cenario(
            {
                "core status": {"saida": estado_json("done", tasks=(("T1", "done", "a"),), pending=None)},
                "claude": {"saida": "[]"},
            }
        )
        falado = self.clis.estado().estado("atlas").falado
        self.assertEqual(
            falado, "O run do atlas terminou, 1 de 1 tasks feitas. Não há sessões do Claude Code abertas no atlas."
        )

    def test_sem_run_diz_isso_e_as_sessoes(self) -> None:
        self.clis.cenario({"core status": SEM_RUN, "claude": {"saida": sessoes_json(self.clis.projeto, "busy")}})
        falado = self.clis.estado().estado("atlas").falado
        self.assertEqual(falado, "O atlas não tem nenhum run FORJA. Sessões do Claude Code no atlas: 1 a trabalhar.")

    def test_sem_forja_configurada_so_ve_as_sessoes(self) -> None:
        self.clis.cenario({"claude": {"saida": sessoes_json(self.clis.projeto, "idle", "idle")}})
        falado = self.clis.estado(com_forja=False).estado("atlas").falado
        self.assertEqual(
            falado,
            "A FORJA não está configurada, por isso só vejo as sessões. Sessões do Claude Code no atlas: 2 paradas.",
        )
        self.assertEqual([c["programa"] for c in self.clis.chamadas()], ["claude"])

    def test_sessoes_de_outras_pastas_nao_contam(self) -> None:
        self.clis.cenario(
            {"core status": SEM_RUN, "claude": {"saida": sessoes_json(self.clis.projeto, "busy", fora=3)}}
        )
        self.assertIn("1 a trabalhar.", self.clis.estado().estado("atlas").falado)

    def test_em_ingles(self) -> None:
        self.clis.cenario(
            {
                "core status": {"saida": estado_json("blocked", recovery="provider_limit")},
                "claude": {"saida": "[]"},
            }
        )
        falado = self.clis.estado().estado("atlas", "en").falado
        self.assertEqual(
            falado,
            "The run on atlas is stopped, 1 of 2 tasks done. It stopped on T2, Segunda task. "
            "Blocker: the account hit its usage limit.",
        )

    def test_projeto_desconhecido_nao_corre_nada(self) -> None:
        resposta = self.clis.estado().estado("orbita")
        self.assertFalse(resposta.feito)
        self.assertEqual(resposta.falado, "Não conheço esse projeto.")
        self.assertEqual(self.clis.chamadas(), [])


class TestNadaPrivadoNemTecnicoNaVoz(_ComCLIsFalsos):
    def test_objetivo_nomes_de_sessao_e_falhas_antigas_nunca_sao_ditos(self) -> None:
        self.clis.cenario(
            {"core status": {"saida": estado_json()}, "claude": {"saida": sessoes_json(self.clis.projeto, "busy")}}
        )
        resposta = self.clis.estado().estado("atlas")
        tudo = resposta.falado + "\n".join(resposta.ecra)
        for privado in ("OBJETIVO-PRIVADO", "NOME-PRIVADO", "privado\\caminho"):
            self.assertNotIn(privado, tudo)

    def test_titulo_com_codigo_e_caminhos_e_filtrado_e_nao_parte_frases(self) -> None:
        titulo = "Corrige `foo_bar()` em C:\\repo\\x.py. Depois <invoke name=Bash> e mais. Fim!"
        self.clis.cenario(
            {
                "core status": {"saida": estado_json(tasks=(("T1", "develop", titulo),), pending="T1")},
                "claude": {"saida": "[]"},
            }
        )
        falado = self.clis.estado().estado("atlas").falado
        self.assertLessEqual(contar_frases(falado), 3)
        for proibido in ("C:\\", "<", "_", "`", "invoke"):
            self.assertNotIn(proibido, falado)

    def test_id_de_task_estranho_nao_e_dito(self) -> None:
        self.clis.cenario(
            {
                "core status": {"saida": estado_json(tasks=(("rm -rf /; T1", "develop", "a"),), pending=None)},
                "claude": {"saida": "[]"},
            }
        )
        falado = self.clis.estado().estado("atlas").falado
        self.assertNotIn("rm -rf", falado)
        self.assertNotIn("Vai na", falado)


class TestErrosDosCLIsViramFraseCurta(_ComCLIsFalsos):
    def test_timeout_nao_pendura_e_vira_frase(self) -> None:
        self.clis.cenario({"core status": {"dormir": 30}, "claude": {"dormir": 30}})
        inicio = time.monotonic()
        resposta = self.clis.estado(limite_s=1.0).estado("atlas")
        self.assertLess(time.monotonic() - inicio, 10.0)
        self.assertEqual(resposta.falado, "O node não respondeu em 1 segundos. Não consegui ver as sessões do Claude Code.")

    def test_erro_do_cli_nao_le_o_stderr_em_voz_alta(self) -> None:
        segredo = "token=abc123 em C:\\Users\\alguem\\privado"
        self.clis.cenario(
            {"core status": {"codigo": 3, "erro": segredo}, "claude": {"codigo": 1, "erro": segredo}}
        )
        resposta = self.clis.estado().estado("atlas")
        self.assertEqual(
            resposta.falado,
            "O node deu um erro; os detalhes estão no ecrã. Não consegui ver as sessões do Claude Code.",
        )
        self.assertNotIn("abc123", resposta.falado)
        self.assertTrue(any("abc123" in linha for linha in resposta.ecra))

    def test_saida_que_nao_e_json(self) -> None:
        self.clis.cenario({"core status": {"saida": "isto nao e json"}, "claude": {"saida": "{\"x\": 1}"}})
        falado = self.clis.estado().estado("atlas").falado
        self.assertEqual(
            falado, "O FORJA respondeu algo que não percebi. Não consegui ver as sessões do Claude Code."
        )

    def test_cli_que_nao_existe(self) -> None:
        estado = Estado(
            self.clis.config(),
            comando_node=[str(self.clis.base / "nao-existe" / "node.exe")],
            comando_claude=[str(self.clis.base / "nao-existe" / "claude.exe")],
        )
        falado = estado.estado("atlas").falado
        self.assertEqual(
            falado, "Não encontrei o node neste computador. Não consegui ver as sessões do Claude Code."
        )


class TestCorrerCli(unittest.TestCase):
    def test_argv_em_lista_sem_shell_com_cwd_e_prazo(self) -> None:
        visto: dict = {}

        def correr(argv, **opcoes):
            visto.update(opcoes, argv=argv)
            return mock.Mock(returncode=0, stdout=b"ok", stderr=b"")

        saida = correr_cli(["C:/x/node.exe", "a b", Path("c")], Path("D:/p"), 7.0, correr=correr)
        self.assertEqual(saida, SaidaDoCli(0, "ok", ""))
        self.assertEqual(visto["argv"], ["C:/x/node.exe", "a b", "c"])
        self.assertIs(visto["shell"], False)
        self.assertEqual(visto["cwd"], str(Path("D:/p")))
        self.assertEqual(visto["timeout"], 7.0)
        self.assertNotIn("CLAUDECODE", visto["env"])

    def test_shim_cmd_e_recusado_sem_correr(self) -> None:
        correr = mock.Mock()
        for shim in ("C:/npm/claude.cmd", "C:/x/node.bat", "C:/x/forja.ps1"):
            with self.subTest(shim=shim):
                saida = correr_cli([shim, "agents"], Path("."), 5.0, correr=correr)
                self.assertEqual(saida.falha, "arranque")
        correr.assert_not_called()


# --- Partes puras -----------------------------------------------------------


class TestPartesPuras(unittest.TestCase):
    def test_ler_estado_recusa_o_que_nao_e_objeto(self) -> None:
        for texto in ("[]", "1", "nao json"):
            with self.subTest(texto=texto), self.assertRaises(ValueError):
                ler_estado_do_run(texto)

    def test_motivo_desconhecido_vira_inspecionar(self) -> None:
        dados = json.loads(estado_json("blocked"))
        dados["recovery"] = {"code": "Something <weird>"}
        self.assertEqual(ler_estado_do_run(json.dumps(dados)).motivo, "inspect")

    def test_ler_sessoes_ignora_itens_estranhos(self) -> None:
        pasta = Path(tempfile.gettempdir()) / "p"
        texto = json.dumps([1, None, {"cwd": 3}, {"cwd": str(pasta / "sub"), "status": "waiting_for_input", "pid": True}])
        sessoes = ler_sessoes(texto, pasta)
        self.assertEqual(len(sessoes), 1)
        self.assertEqual(sessoes[0].estado, "a_espera")
        self.assertIsNone(sessoes[0].pid)

    def test_compor_nunca_passa_de_tres_frases(self) -> None:
        run = ler_estado_do_run(estado_json("blocked", recovery="context", technology=[{"id": "D1"}]))
        sessoes = ler_sessoes(sessoes_json(Path("C:/p"), "busy", "idle", "waiting", "outra"), Path("C:/p"))
        for lingua in ("pt", "en"):
            with self.subTest(lingua=lingua):
                self.assertLessEqual(contar_frases(compor_estado("atlas", run, None, sessoes, None, lingua)), 3)


# --- Relatorio --------------------------------------------------------------

RELATORIO_MD = """# Relatório do run

Data: 2026-09-20

## Resumo

O run acabou com **3 tasks aprovadas** e uma bloqueada.
- A voz ficou mais rápida.
- Ver o ficheiro `C:\\repo\\docs\\x.md` para detalhes.

## Detalhe

```python
print("nunca dito")
```
"""


class TestRelatorio(_ComCLIsFalsos):
    def _escrever_md(self, nome: str, texto: str, quando: float | None = None) -> Path:
        pasta = self.clis.projeto / "docs" / "forja"
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / nome
        caminho.write_text(texto, encoding="utf-8")
        if quando is not None:
            os.utime(caminho, (quando, quando))
        return caminho

    def _escrever_estado(self, concluida_em: str) -> None:
        pasta = self.clis.projeto / ".forja"
        pasta.mkdir(exist_ok=True)
        (pasta / "current.json").write_text(
            json.dumps(
                {
                    "run_id": "F-1790000000000-abc123",
                    "updated_at": concluida_em,
                    "tasks": [
                        {"id": "T1", "title": "Primeira", "completed_at": "2020-01-01T00:00:00Z",
                         "review": {"status": "approve", "summary": "Antiga."}},
                        {"id": "T2", "title": "Segunda", "completed_at": concluida_em,
                         "review": {"status": "approve", "summary": "A T2 esta aprovada. Os testes passam.",
                                    "findings": ["nota sobre C:\\x"]}},
                        {"id": "T3", "title": "Sem revisao"},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_le_o_resumo_filtrado_e_mostra_o_texto_inteiro(self) -> None:
        self._escrever_md("REPORT-2026-09-20.md", RELATORIO_MD)
        antes = _retrato(self.clis.projeto)
        resposta = self.clis.estado().relatorio("atlas")
        self.assertTrue(resposta.falado.startswith("Resumo do relatório do atlas: O run acabou com 3 tasks aprovadas"))
        self.assertIn("A voz ficou mais rápida.", resposta.falado)
        for proibido in ("C:\\", "`", "print", "**"):
            self.assertNotIn(proibido, resposta.falado)
        for linha in RELATORIO_MD.splitlines():
            self.assertIn(linha, resposta.ecra)
        self.assertEqual(_retrato(self.clis.projeto), antes)
        self.assertEqual(self.clis.chamadas(), [])

    def test_o_mais_recente_ganha_entre_relatorio_e_revisao_do_run(self) -> None:
        self._escrever_md("REPORT-2026-09-20.md", RELATORIO_MD, quando=time.time() - 3600)
        self._escrever_estado("2099-01-01T00:00:00Z")
        falado = self.clis.estado().relatorio("atlas").falado
        self.assertEqual(falado, "Resumo do relatório do atlas: A T2 esta aprovada. Os testes passam.")
        self._escrever_estado("2000-01-01T00:00:00Z")
        self.assertIn("O run acabou", self.clis.estado().relatorio("atlas").falado)

    def test_dos_relatorios_markdown_ganha_o_mais_recente(self) -> None:
        self._escrever_md("REPORT-2026-09-01.md", "## Summary\n\nOld report.\n", quando=time.time() - 7200)
        self._escrever_md("REPORT-2026-09-02.md", "## Summary\n\nNew report.\n", quando=time.time() - 60)
        self.assertEqual(
            self.clis.estado().relatorio("atlas", "en").falado, "Summary of the atlas report: New report."
        )

    def test_sem_relatorio(self) -> None:
        resposta = self.clis.estado().relatorio("atlas")
        self.assertEqual(resposta.falado, "Não encontrei nenhum relatório no atlas.")
        self.assertFalse(resposta.feito)

    def test_relatorio_so_tecnico_nao_e_lido(self) -> None:
        self._escrever_md("REPORT-2026-09-20.md", "## Resumo\n\n<invoke name=\"Bash\">\nC:\\x\\y.py\n")
        resposta = self.clis.estado().relatorio("atlas")
        self.assertEqual(
            resposta.falado,
            "O relatório do atlas não tem um resumo que se possa ler; o texto inteiro está no ecrã.",
        )
        self.assertIn("<invoke name=\"Bash\">", resposta.ecra)

    def test_estado_do_run_estragado_e_ignorado(self) -> None:
        (self.clis.projeto / ".forja").mkdir()
        (self.clis.projeto / ".forja" / "current.json").write_text("{meio escrito", encoding="utf-8")
        self.assertIsNone(encontrar_relatorio(self.clis.projeto))

    def test_resumo_sem_seccao_usa_o_primeiro_paragrafo(self) -> None:
        self.assertEqual(
            resumo_do_markdown("# Titulo\n\n| a | b |\n\nPrimeiro paragrafo\ncontinua.\n\nSegundo."),
            "Primeiro paragrafo\ncontinua.",
        )


if __name__ == "__main__":
    unittest.main()
