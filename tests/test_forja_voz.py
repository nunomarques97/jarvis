"""Lancar, retomar e parar runs FORJA por voz, com CLIs falsos.

Os CLIs falsos (`tests.test_estado.CLIsFalsos`) correm como subprocessos a
serio. O controlador lancado e um processo Python sem janela; parar envia-lhe
um Ctrl+C a serio. Nada toca som e nada corre num projeto real.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import acoes_locais
from jarvis.confirmacao import Pedido
from jarvis.forja_voz import (
    _FRASES,
    INTENCOES_POR_VOZ,
    SUBCOMANDOS_PERMITIDOS,
    Controlador,
    ForjaPorVoz,
    alteracoes_por_guardar,
    argv_com_intermedio,
    argv_da_forja,
    lancar_controlador,
    motivo_do_erro,
    validar_objetivo,
)
from jarvis.interprete import INTENCOES
from jarvis.persona import VARIANTES, opcoes_da_frase
from tests.test_app import Montagem, resposta_llm
from tests.test_estado import SEM_RUN, _ComCLIsFalsos, estado_json

OBJETIVO = 'Migra os testes para pytest e mantem "tudo" verde'

#: Palavras de pedidos que nunca podem chegar a FORJA nem ao sistema por voz.
PROIBIDAS = ("abandon", "retry", "commit", "push", "deliver", "apagar", "delete", "allow-dirty", "comprar")


class ProcessoFalso:
    def __init__(self, vivo: bool = True, codigo: int = 0, pid: int = 4242) -> None:
        self._vivo = vivo
        self.returncode = None if vivo else codigo
        self.codigo = codigo
        self.pid = pid

    def poll(self):
        return None if self._vivo else self.returncode

    def terminar(self) -> None:
        self._vivo = False
        self.returncode = self.codigo


class _Base(_ComCLIsFalsos):
    def setUp(self) -> None:
        super().setUp()
        self.lancados: list[tuple[list[str], Path, Path]] = []
        self.interrompidos: list[int] = []

    def forja(self, *, lancar=None, interromper=None, com_forja: bool = True, **ajustes) -> ForjaPorVoz:
        return ForjaPorVoz(
            self.clis.config(com_forja=com_forja),
            estado=self.clis.estado(com_forja=com_forja),
            comando_git=[sys.executable, str(self.clis.git)],
            lancar=lancar or self._lancar_falso,
            interromper=interromper or self._interromper_falso,
            pasta_de_logs=self.clis.base / "logs",
            espera_inicial_s=ajustes.pop("espera_inicial_s", 0.2),
            espera_da_paragem_s=ajustes.pop("espera_da_paragem_s", 0.3),
            **ajustes,
        )

    def _lancar_falso(self, argv, cwd, log):
        self.lancados.append((argv, cwd, log))
        return ProcessoFalso()

    def _interromper_falso(self, pid: int) -> bool:
        self.interrompidos.append(pid)
        return True

    def cenario(self, status: dict | None = None, git: str = "", **extra) -> None:
        dados = {"core status": status or SEM_RUN, "git": {"saida": git}, "claude": {"saida": "[]"}}
        dados.update(extra)
        self.clis.cenario(dados)

    def programas(self) -> list[str]:
        return [c["programa"] + " " + " ".join(c.get("argv", [])[:2]) for c in self.clis.chamadas()]


# --- Lancar -----------------------------------------------------------------


class TestLancar(_Base):
    def test_lanca_com_o_objetivo_ditado_e_o_perfil_configurado(self) -> None:
        self.cenario()
        resposta = self.forja().executar("lancar_run", "atlas", OBJETIVO)
        self.assertTrue(resposta.feito)
        self.assertEqual(resposta.falado, "Lancei o run no atlas. Pergunta-me pelo estado quando quiseres.")
        argv, cwd, log = self.lancados[0]
        self.assertEqual(
            argv,
            [
                sys.executable,
                str(self.clis.forja.resolve() / "bin" / "forja.mjs"),
                "start",
                "--provider",
                "claude",
                "--config",
                str(self.clis.perfil.resolve()),
                "--goal",
                OBJETIVO,
            ],
        )
        self.assertEqual(cwd, self.clis.projeto.resolve())
        self.assertTrue(log.is_relative_to(self.clis.base / "logs"))
        self.assertEqual(self.programas(), ["forja core status", "git status --porcelain"])
        git = self.clis.chamadas()[1]
        self.assertEqual(Path(git["cwd"]).resolve(), self.clis.projeto.resolve())

    def test_recusado_com_run_ativo(self) -> None:
        self.cenario({"saida": estado_json("running")})
        resposta = self.forja().executar("lancar_run", "atlas", OBJETIVO)
        self.assertFalse(resposta.feito)
        self.assertEqual(resposta.falado, "Não lancei: já há um run ativo no atlas.")
        self.assertEqual(self.lancados, [])

    def test_recusado_com_run_parado_por_acabar(self) -> None:
        for status in (estado_json("blocked", recovery="attempts"), estado_json("running", recovery="interrupted")):
            with self.subTest(status=status[:60]):
                self.cenario({"saida": status})
                falado = self.forja().executar("lancar_run", "atlas", OBJETIVO).falado
                self.assertIn("ainda não acabou", falado)
        self.assertEqual(self.lancados, [])

    def test_lanca_depois_de_um_run_terminado_ou_falhado(self) -> None:
        for estado in ("done", "failed"):
            with self.subTest(estado=estado):
                self.cenario({"saida": estado_json(estado, pending=None)})
                self.assertTrue(self.forja().executar("lancar_run", "atlas", OBJETIVO).feito)
        self.assertEqual(len(self.lancados), 2)

    def test_recusado_com_alteracoes_por_guardar(self) -> None:
        self.cenario(git=" M src/a.py\n?? novo.txt\n")
        resposta = self.forja().executar("lancar_run", "atlas", OBJETIVO)
        self.assertEqual(resposta.falado, "Não lancei: o atlas tem alterações por guardar.")
        self.assertIn("forja | 2 ficheiro(s) por guardar", resposta.ecra)
        self.assertEqual(self.lancados, [])

    def test_a_pasta_da_forja_nao_conta_como_alteracao(self) -> None:
        self.cenario(git="?? .forja/\n")
        self.assertTrue(self.forja().executar("lancar_run", "atlas", OBJETIVO).feito)

    def test_recusado_quando_nao_consegue_confirmar(self) -> None:
        casos = {
            "estado com erro": ({"codigo": 2, "erro": "boom"}, ""),
            "estado invalido": ({"saida": "nao json"}, ""),
        }
        for nome, (status, git) in casos.items():
            with self.subTest(nome):
                self.cenario(status, git)
                falado = self.forja().executar("lancar_run", "atlas", OBJETIVO).falado
                self.assertTrue(falado.startswith("Não lancei: não consegui confirmar"), falado)
        self.clis.cenario({"core status": SEM_RUN, "git": {"codigo": 128, "erro": "not a git repository"}})
        self.assertEqual(
            self.forja().executar("lancar_run", "atlas", OBJETIVO).falado,
            "Não lancei: não consegui ver as alterações do atlas no Git.",
        )
        self.assertEqual(self.lancados, [])

    def test_estado_que_nao_responde_a_tempo_recusa(self) -> None:
        self.cenario({"dormir": 30})
        inicio = time.monotonic()
        forja = self.forja(limite_s=1.0)
        forja.estado.limite_s = 1.0
        falado = forja.executar("lancar_run", "atlas", OBJETIVO).falado
        self.assertLess(time.monotonic() - inicio, 10.0)
        self.assertIn("não respondeu em 1 segundos", falado)
        self.assertEqual(self.lancados, [])

    def test_recusado_com_controlador_do_jarvis_ainda_vivo(self) -> None:
        self.cenario()
        forja = self.forja()
        forja.executar("lancar_run", "atlas", OBJETIVO)
        self.assertEqual(forja.executar("lancar_run", "atlas", OBJETIVO).falado, "Não lancei: já há um run ativo no atlas.")
        self.assertEqual(len(self.lancados), 1)

    def test_objetivos_recusados_sem_correr_nada(self) -> None:
        for objetivo, frase in (
            ("", "Falta o objetivo do run."),
            ("   ", "Falta o objetivo do run."),
            ("--allow-dirty", "Esse objetivo não pode seguir"),
            ("-x corrige", "Esse objetivo não pode seguir"),
            ("linha um\nlinha dois", "Esse objetivo não pode seguir"),
            ("com\x00nul", "Esse objetivo não pode seguir"),
            ("a" * 2001, "Esse objetivo não pode seguir"),
        ):
            with self.subTest(objetivo=objetivo[:20]):
                self.assertTrue(self.forja().executar("lancar_run", "atlas", objetivo).falado.startswith(frase))
        self.assertEqual(self.clis.chamadas(), [])
        self.assertEqual(self.lancados, [])

    def test_sem_forja_ou_projeto_desconhecido_nao_corre_nada(self) -> None:
        self.assertEqual(
            self.forja(com_forja=False).executar("lancar_run", "atlas", OBJETIVO).falado,
            "A FORJA não está configurada no jarvis. Não fiz nada.",
        )
        for projeto in ("orbita", None, "../atlas", "C:/Windows"):
            with self.subTest(projeto=projeto):
                self.assertEqual(
                    self.forja().executar("lancar_run", projeto, OBJETIVO).falado,
                    "Não conheço esse projeto. Não fiz nada.",
                )
        self.assertEqual(self.clis.chamadas(), [])

    def test_em_ingles(self) -> None:
        self.cenario(git=" M a.py\n")
        VARIANTES.esquecer()  # a primeira forma de cada frase
        self.assertEqual(
            self.forja().executar("lancar_run", "atlas", OBJETIVO, "en").falado,
            "I didn't start: atlas has uncommitted changes.",
        )

    def test_em_ingles_nunca_repete_a_mesma_forma_seguida(self) -> None:
        self.cenario(git=" M a.py\n")
        ditas = [self.forja().executar("lancar_run", "atlas", OBJETIVO, "en").falado for _ in range(12)]
        self.assertTrue(all(a != b for a, b in zip(ditas, ditas[1:])), ditas)
        formas = {forma.format(p="atlas") for forma in opcoes_da_frase(_FRASES["en"]["alteracoes"])}
        self.assertEqual(set(ditas), formas)


class TestControladorQueRecusaLogo(_Base):
    """O controlador arranca mas a FORJA recusa: o motivo vem do log, numa frase curta."""

    def _lancar_real(self, argv, cwd, log):
        from jarvis.forja_voz import lancar_controlador

        processo = lancar_controlador(argv, cwd, log)
        self.addCleanup(lambda: processo.poll() is None and processo.kill())
        return processo

    def test_recusa_da_forja_traduzida(self) -> None:
        casos = {
            "Project has uncommitted work. Commit/stash it": "há alterações por guardar.",
            "A core run is unfinished; use forja core resume": "já há um run ativo.",
            "Start FORJA from the Git project root, not a subdirectory.": "a pasta não é a raiz do repositório Git.",
            "TypeError: C:\\privado\\x token=abc": "a FORJA deu um erro; os detalhes estão no ecrã.",
        }
        for erro, motivo in casos.items():
            with self.subTest(erro=erro[:30]):
                self.cenario(start={"codigo": 1, "erro": f"forja: {erro}\n"})
                resposta = self.forja(lancar=self._lancar_real, espera_inicial_s=15.0).executar(
                    "lancar_run", "atlas", OBJETIVO
                )
                self.assertFalse(resposta.feito)
                self.assertEqual(resposta.falado, f"O run do atlas não arrancou: {motivo}")
                self.assertNotIn("abc", resposta.falado)
                self.assertTrue(any(erro in linha for linha in resposta.ecra))


# --- Retomar ----------------------------------------------------------------


class TestRetomar(_Base):
    def test_retoma_um_run_parado_ou_interrompido(self) -> None:
        for status in (estado_json("blocked", recovery="operator_stop"), estado_json("running", recovery="interrupted")):
            with self.subTest(status=status[:60]):
                self.cenario({"saida": status})
                resposta = self.forja().executar("retomar_run", "atlas")
                self.assertEqual(resposta.falado, "Retomei o run do atlas.")
        self.assertEqual(len(self.lancados), 2)
        for argv, cwd, _log in self.lancados:
            self.assertEqual(argv[1:], [str(self.clis.forja.resolve() / "bin" / "forja.mjs"), "core", "resume"])
            self.assertEqual(cwd, self.clis.projeto.resolve())

    def test_recusas_da_retoma(self) -> None:
        casos = [
            (SEM_RUN, "Não há nenhum run no atlas para retomar."),
            ({"saida": estado_json("done", pending=None)}, "O run do atlas já acabou; não há nada para retomar."),
            ({"saida": estado_json("failed", pending=None)}, "O run do atlas já acabou; não há nada para retomar."),
            ({"saida": estado_json("running")}, "O run do atlas já está a correr."),
            (
                {"saida": estado_json("blocked", technology=[{"id": "D1"}])},
                "O run do atlas está à espera de uma decisão tua de tecnologia; decide primeiro e depois retoma.",
            ),
            ({"codigo": 2, "erro": "x"}, "Não retomei: não consegui ler o estado do run do atlas. "),
        ]
        for status, frase in casos:
            with self.subTest(frase=frase):
                self.cenario(status)
                self.assertTrue(self.forja().executar("retomar_run", "atlas").falado.startswith(frase))
        self.assertEqual(self.lancados, [])

    def test_retomar_nao_olha_para_a_arvore(self) -> None:
        self.cenario({"saida": estado_json("blocked", recovery="attempts")}, git=" M a.py\n")
        self.assertTrue(self.forja().executar("retomar_run", "atlas").feito)
        self.assertNotIn("git status", " ".join(self.programas()))


# --- Parar ------------------------------------------------------------------


class TestParar(_Base):
    def test_so_para_o_controlador_que_o_jarvis_lancou(self) -> None:
        self.cenario({"saida": estado_json("running")})
        resposta = self.forja().executar("parar_run", "atlas")
        self.assertFalse(resposta.feito)
        self.assertEqual(
            resposta.falado, "Só paro um run que eu tenha lançado ou retomado, e no atlas não há nenhum a correr."
        )
        self.assertEqual(self.interrompidos, [])
        self.assertEqual(self.clis.chamadas(), [])

    def test_para_o_controlador_lancado_e_esquece_o(self) -> None:
        self.cenario()
        processos: list[ProcessoFalso] = []

        def lancar(argv, cwd, log):
            processos.append(ProcessoFalso(pid=777))
            return processos[-1]

        def interromper(pid: int) -> bool:
            self.interrompidos.append(pid)
            processos[-1].terminar()
            return True

        forja = self.forja(lancar=lancar, interromper=interromper)
        forja.executar("lancar_run", "atlas", OBJETIVO)
        resposta = forja.executar("parar_run", "atlas")
        self.assertEqual(
            resposta.falado, "Parei o run do atlas. O trabalho fica guardado; diz retoma o run quando quiseres."
        )
        self.assertEqual(self.interrompidos, [777])
        self.assertIsNone(forja.controlador("atlas"))
        self.assertIn("Só paro", forja.executar("parar_run", "atlas").falado)

    def test_controlador_que_demora_a_fechar_fica_registado(self) -> None:
        self.cenario()
        forja = self.forja()
        forja.executar("lancar_run", "atlas", OBJETIVO)
        self.assertEqual(forja.executar("parar_run", "atlas").falado, "Pedi ao run do atlas para parar; ainda está a fechar.")
        self.assertIsNotNone(forja.controlador("atlas"))

    def test_interrupcao_que_falha_diz_que_o_run_continua(self) -> None:
        self.cenario()
        forja = self.forja(interromper=lambda _pid: False)
        forja.executar("lancar_run", "atlas", OBJETIVO)
        self.assertEqual(
            forja.executar("parar_run", "atlas").falado, "O pedido para parar o run do atlas falhou; o run continua."
        )

    def test_um_controlador_mais_novo_nao_e_esquecido_pela_paragem_do_antigo(self) -> None:
        forja = self.forja()
        antigo = Controlador("atlas", "lancar_run", ProcessoFalso(), Path("a.log"), 0.0)
        novo = Controlador("atlas", "retomar_run", ProcessoFalso(), Path("b.log"), 1.0)
        forja._controladores["atlas"] = antigo

        def interromper(pid: int) -> bool:
            antigo.processo.terminar()
            forja._controladores["atlas"] = novo
            return True

        forja._interromper = interromper
        forja.parar("atlas")
        self.assertIs(forja.controlador("atlas"), novo)


@unittest.skipUnless(sys.platform == "win32", "Ctrl+C a uma consola sem janela e coisa do Windows")
class TestPararDeVerdade(_Base):
    """Controlador falso a serio, sem janela: o Ctrl+C chega-lhe e ele fecha."""

    def test_lancar_e_parar_com_ctrl_c(self) -> None:
        from jarvis.forja_voz import interromper_controlador, lancar_controlador

        self.cenario(start={"dormir": 60, "saida": "controlador falso a correr\n"})
        processos = []

        def lancar(argv, cwd, log):
            processos.append(lancar_controlador(argv, cwd, log))
            self.addCleanup(lambda: processos[-1].poll() is None and processos[-1].kill())
            return processos[-1]

        forja = self.forja(
            lancar=lancar, interromper=interromper_controlador, espera_inicial_s=1.0, espera_da_paragem_s=15.0
        )
        self.assertEqual(
            forja.executar("lancar_run", "atlas", OBJETIVO).falado,
            "Lancei o run no atlas. Pergunta-me pelo estado quando quiseres.",
        )
        resposta = forja.executar("parar_run", "atlas")
        self.assertEqual(
            resposta.falado, "Parei o run do atlas. O trabalho fica guardado; diz retoma o run quando quiseres."
        )
        self.assertEqual(processos[0].poll(), 130)
        self.assertIn({"programa": "forja", "evento": "ctrl-c"}, self.clis.chamadas())
        arranque = [c for c in self.clis.chamadas() if c.get("argv", [None])[0] == "start"][0]
        self.assertEqual(arranque["argv"][-1], OBJETIVO)
        self.assertEqual(Path(arranque["cwd"]).resolve(), self.clis.projeto.resolve())


# --- O que nao existe por voz -----------------------------------------------


class TestNaoExistePorVoz(_Base):
    def test_intencoes_do_interprete_nao_tem_pedidos_proibidos(self) -> None:
        for intencao in INTENCOES:
            for proibida in PROIBIDAS:
                self.assertNotIn(proibida, intencao)
        self.assertEqual(set(INTENCOES_POR_VOZ), {"estado", "ler_relatorio", "lancar_run", "retomar_run", "parar_run"})

    def test_executar_recusa_tudo_o_resto_sem_correr_nada(self) -> None:
        forja = self.forja()
        for intencao in ("abandon", "abandonar_run", "retry", "commit", "push", "deliver", "apagar", "delete",
                         "comprar", "ditar_prompt", "abrir_pasta", ""):
            with self.subTest(intencao=intencao), self.assertRaises(ValueError):
                forja.executar(intencao, "atlas", "tudo")
        self.assertEqual(self.clis.chamadas(), [])
        self.assertEqual(self.lancados, [])

    def test_so_ha_tres_subcomandos_da_forja(self) -> None:
        config = self.clis.config().forja
        self.assertEqual(SUBCOMANDOS_PERMITIDOS, (("core", "status"), ("start",), ("core", "resume")))
        for subcomando in (("core", "abandon"), ("core", "retry"), ("core", "deliver"), ("core", "stop"),
                           ("core", "decide"), ("run", "start"), ("abandon",), ("core", "status", "x"),
                           ("--config", "x", "core", "abandon"), ("start", "core", "abandon")):
            with self.subTest(subcomando=subcomando), self.assertRaises(ValueError):
                argv_da_forja(["node"], config, *subcomando)
        # Os valores das opcoes (o objetivo) podem ter qualquer palavra.
        argv = argv_da_forja(["node"], config, "start", "--goal", "core abandon e push")
        self.assertEqual(argv[-3:], ["start", "--goal", "core abandon e push"])

    def test_objetivo_com_palavras_perigosas_e_um_so_argumento(self) -> None:
        self.cenario()
        objetivo = "abandon the run, retry, commit and push; core deliver --allow-dirty"
        self.forja().executar("lancar_run", "atlas", objetivo)
        argv = self.lancados[0][0]
        self.assertEqual(argv[2:9], ["start", "--provider", "claude", "--config", str(self.clis.perfil.resolve()), "--goal", objetivo])
        self.assertEqual(len(argv), 9)

    def test_argv_do_intermedio_nao_usa_shell_nem_ambiente_python(self) -> None:
        argv = argv_com_intermedio(["C:/node/node.exe", "C:/forja/bin/forja.mjs", "core", "resume"])
        self.assertEqual(argv[1:3], ["-I", "-c"])
        self.assertEqual(argv[4:], ["C:/node/node.exe", "C:/forja/bin/forja.mjs", "core", "resume"])
        with self.assertRaises(ValueError):
            lancar_controlador(["C:/npm/node.cmd", "x"], Path("."), self.clis.base / "logs" / "nunca.log")
        self.assertFalse((self.clis.base / "logs" / "nunca.log").exists())

    def test_jarvis_nao_executa_intencoes_fora_da_lista(self) -> None:
        m = Montagem()
        forja = mock.Mock()
        m.jarvis.forja = forja
        for intencao in ("abandonar_run", "retry", "commit", "push", "apagar"):
            with self.subTest(intencao=intencao), self.assertRaises(acoes_locais.AcaoError):
                m.jarvis._executar(Pedido(intencao, "atlas", "tudo"))
        forja.executar.assert_not_called()

    def test_ordem_financeira_como_objetivo_e_recusada_antes_da_forja(self) -> None:
        m = Montagem([resposta_llm("lancar_run", "atlas", "Compra 10 ações da Tesla na corretora.")])
        forja = mock.Mock()
        m.jarvis.forja = forja
        m.ouvir("lança um run no atlas para comprar ações da tesla")
        m.avancar()
        m.ouvir("sim")
        forja.executar.assert_not_called()
        self.assertFalse(m.jarvis.confirmacao.a_espera)


# --- Ligado ao jarvis: o que muda passa pela confirmacao --------------------


class TestPeloJarvis(_Base):
    def montar(self, respostas) -> Montagem:
        m = Montagem(respostas)
        m.jarvis.forja = self.forja()
        return m

    def test_estado_corre_logo_diz_ate_tres_frases_e_mostra_o_detalhe(self) -> None:
        self.cenario({"saida": estado_json()})
        m = self.montar([resposta_llm("estado", "atlas")])
        m.ouvir("como está o run do atlas")
        self.assertFalse(m.jarvis.confirmacao.a_espera, "ver o estado so le: sem recap nem sim")
        self.assertEqual(len(m.falados), 1)
        self.assertTrue(m.falados[-1].startswith("O run do atlas está a correr"))
        self.assertIn("ecra | estado do atlas:", m.log.texto())

    def test_lancar_so_depois_do_sim_e_cancelar_nao_lanca(self) -> None:
        self.cenario()
        m = self.montar([resposta_llm("lancar_run", "atlas", OBJETIVO), resposta_llm("lancar_run", "atlas", OBJETIVO)])
        m.ouvir("lança um run no atlas para migrar os testes para pytest e manter tudo verde")
        self.assertEqual(self.lancados, [])
        m.avancar()
        m.ouvir("cancela")
        self.assertEqual(self.lancados, [])
        m.ouvir("lança um run no atlas para migrar os testes para pytest e manter tudo verde")
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(len(self.lancados), 1)
        self.assertEqual(self.lancados[0][0][-1], OBJETIVO + ".")
        self.assertEqual(m.falados[-1], "Lancei o run no atlas. Pergunta-me pelo estado quando quiseres.")

    def test_parar_e_retomar_tambem_pedem_confirmacao(self) -> None:
        self.cenario({"saida": estado_json("blocked", recovery="operator_stop")})
        m = self.montar([resposta_llm("retomar_run", "atlas"), resposta_llm("parar_run", "atlas")])
        m.ouvir("retoma o run do atlas")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(self.lancados, [])
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(len(self.lancados), 1)
        m.ouvir("para o run do atlas")
        self.assertTrue(m.jarvis.confirmacao.a_espera)
        self.assertEqual(self.interrompidos, [])
        m.avancar()
        m.ouvir("sim")
        self.assertEqual(self.interrompidos, [4242])


# --- Partes puras -----------------------------------------------------------


class TestPartesPuras(unittest.TestCase):
    def test_validar_objetivo(self) -> None:
        self.assertEqual(validar_objetivo("  corrige   o login  "), "corrige o login")
        for mau in ("", "-a", "--goal", "a\rb", "a\nb"):
            with self.subTest(mau=mau), self.assertRaises(ValueError):
                validar_objetivo(mau)

    def test_alteracoes_por_guardar(self) -> None:
        self.assertEqual(alteracoes_por_guardar("?? .forja/\n\n M a.py\n"), [" M a.py"])

    def test_motivo_do_erro(self) -> None:
        self.assertEqual(motivo_do_erro("Error: Run is terminal; start a new goal."), "run_terminado")
        self.assertEqual(motivo_do_erro("Sponsor technology choice is pending"), "decisao_pendente")
        self.assertEqual(motivo_do_erro("Another FORJA process or its worker is still alive."), "run_ativo")
        self.assertEqual(motivo_do_erro("forja: ENOENT: open 'x\\.forja\\current.json'"), "sem_run")
        self.assertEqual(motivo_do_erro(json.dumps({"x": 1})), "erro_forja")


if __name__ == "__main__":
    unittest.main()
