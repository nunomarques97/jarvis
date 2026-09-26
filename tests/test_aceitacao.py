r"""Testes da aceitacao com a voz do Sponsor (scripts/aceitacao_sponsor.py).

Sem microfone, sem Ollama, sem Claude Code e sem som. O que protegem:

  * o guiao cobre os casos (a)-(d) e so usa marcadores, intencoes e fluxos validos;
  * o log que o jarvis a serio escreve (montado com pecas falsas) e lido como
    o script espera: intencao, projeto, recap, correcao, cancelamento,
    conversa, latencias;
  * as metas: acerto a primeira, ditados aceites sem correcao, pedidos
    inventados, latencias p50/p95 e a cobertura dos casos (a)-(d);
  * sem sessao do Sponsor, ou so com voz sintetica (ficheiros WAV), a
    evidencia diz PENDENTE e o passo exato; nada e simulado;
  * a evidencia nunca leva nomes de projetos nem o texto das frases;
  * a sessao guiada guarda as janelas e as respostas s/n e retoma onde ficou.

Corre com:

    .venv\Scripts\python -m unittest tests.test_aceitacao -v
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path

from jarvis.app import LogDaSessao, agora_iso
from tests.test_app import CanalFalso, Montagem, resposta_llm

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


ac = _carregar_script("aceitacao_sponsor")

INICIO = datetime.datetime(2026, 9, 25, 10, 0, 0)
PROJETOS = dict(ac.PROJETOS_FICTICIOS)
TAREFAS = ac.ler_guiao()
POR_ID = {t.id: t for t in TAREFAS}


def _avaliar(sessao, linhas):
    return ac.avaliar_sessao(sessao, TAREFAS, *ac.ler_log(linhas))


# --- O guiao -----------------------------------------------------------------------


class TestGuiao(unittest.TestCase):
    def test_o_guiao_versionado_cobre_os_casos_e_as_metas(self) -> None:
        self.assertEqual(ac.verificar_cobertura_do_guiao(TAREFAS), [])
        self.assertEqual({t.caso for t in TAREFAS}, set(ac.CASOS))

    def test_ids_unicos_e_cada_caso_com_o_seu_prefixo(self) -> None:
        self.assertEqual(len(POR_ID), len(TAREFAS))
        for tarefa in TAREFAS:
            self.assertEqual(tarefa.id[0], "l" if tarefa.caso == "local" else tarefa.caso)

    def test_so_marcadores_nunca_nomes_de_projetos(self) -> None:
        texto = ac.GUIAO.read_text(encoding="utf-8")
        for marcador in re.findall(r"<[\w-]+>", texto):
            self.assertIn(marcador, ac.MARCADORES + ("<nome>",))

    def test_sem_vocabulario_de_compra_e_venda(self) -> None:
        texto = ac.GUIAO.read_text(encoding="utf-8").lower()
        for termo in (" buy", " sell", " trade", "broker", "wallet", " compra", " vende", "corretora"):
            self.assertNotIn(termo, texto)

    def test_uma_tarefa_de_ditado_pergunta_pelos_pedidos_inventados(self) -> None:
        self.assertTrue(POR_ID["a-01"].pergunta_inventados)
        self.assertTrue(POR_ID["c-01"].pergunta_inventados)
        self.assertFalse(POR_ID["a-06"].pergunta_inventados, "cancelado: nada foi enviado")
        self.assertFalse(POR_ID["l-01"].pergunta_inventados)

    def _guiao(self, linha: str) -> Path:
        cabecalho = "| " + " | ".join(ac.COLUNAS) + " |\n|" + "---|" * len(ac.COLUNAS) + "\n"
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        caminho = Path(pasta.name) / "guiao.md"
        caminho.write_text(cabecalho + linha + "\n", encoding="utf-8")
        return caminho

    def test_formato_invalido_e_recusado_com_a_linha(self) -> None:
        # (razao esperada na mensagem, linha): cada caso falha pela razao do nome, nao pela contagem de colunas.
        casos = {
            "marcador desconhecido": (
                "so se aceitam os marcadores",
                "| a-01 | a | x <meu-projeto> | tell <projeto-1> | diz ao <projeto-1> | y | ditar_prompt | <projeto-1> | confirmar |",
            ),
            "marcador desconhecido no que deve acontecer": (
                "so se aceitam os marcadores",
                "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | Sent to <projeto-N> | ditar_prompt | <projeto-1> | confirmar |",
            ),
            "fluxo desconhecido": (
                "fluxo 'talvez'",
                "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | y | ditar_prompt | <projeto-1> | talvez |",
            ),
            "intencao fora da lista": (
                "intencao 'comprar'",
                "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | y | comprar | <projeto-1> | confirmar |",
            ),
            "projeto sem exemplo": (
                "tem de aparecer nos dois exemplos",
                "| a-01 | a | x | tell claude | diz ao <projeto-1> | y | ditar_prompt | <projeto-1> | confirmar |",
            ),
            "id de outro caso": (
                "o id nao bate com o caso",
                "| b-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | y | ditar_prompt | <projeto-1> | confirmar |",
            ),
            "fluxo projeto com projeto": (
                "o fluxo 'projeto' e um ditado sem projeto",
                "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | y | ditar_prompt | <projeto-1> | projeto |",
            ),
            "o que deve acontecer vazio": (
                "celula vazia em ['o que deve acontecer']",
                "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> |  | ditar_prompt | <projeto-1> | confirmar |",
            ),
            "colunas a menos (formato antigo)": (
                "8 colunas, esperava 9",
                "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | ditar_prompt | <projeto-1> | confirmar |",
            ),
        }
        for nome, (razao, linha) in casos.items():
            with self.subTest(nome), self.assertRaises(ac.ErroDoGuiao) as contexto:
                ac.ler_guiao(self._guiao(linha))
            self.assertIn("guiao.md:3", str(contexto.exception))
            self.assertIn(razao, str(contexto.exception))

    def test_uma_linha_valida_com_as_nove_colunas_e_lida(self) -> None:
        linha = "| a-01 | a | x | tell <projeto-1> | diz ao <projeto-1> | y | ditar_prompt | <projeto-1> | confirmar |"
        (tarefa,) = ac.ler_guiao(self._guiao(linha))
        self.assertEqual((tarefa.acontecer, tarefa.fluxo, tarefa.projeto), ("y", "confirmar", "<projeto-1>"))

    def test_projeto_teste_tem_de_estar_na_configuracao(self) -> None:
        self.assertEqual(
            ac.nomes_dos_marcadores(["um", "dois", "tres"], "tres"),
            {"<projeto-1>": "um", "<projeto-2>": "dois", "<projeto-teste>": "tres"},
        )
        self.assertNotIn("<projeto-teste>", ac.nomes_dos_marcadores(["um"], None))
        with self.assertRaises(ac.ErroDaSessao):
            ac.nomes_dos_marcadores(["um", "dois"], "outro")
        with self.assertRaises(ac.ErroDaSessao):
            ac.nomes_dos_marcadores([], None)


# --- O log do jarvis a serio -------------------------------------------------------


class _RelogioDeParede:
    def __init__(self, inicio: datetime.datetime) -> None:
        self.agora = inicio

    def __call__(self) -> datetime.datetime:
        return self.agora


class TestContratoComOLogDoJarvis(unittest.TestCase):
    """O jarvis residente (pecas falsas) escreve o log; o script le-o e mede."""

    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.parede = _RelogioDeParede(INICIO)
        self.pergunta = "Which of the two test files do you want to keep?"
        self.m = Montagem(
            [
                resposta_llm("ditar_prompt", "atlas", "Add a test for the config loader."),
                resposta_llm("ditar_prompt", "orbita", "Add tests to the login screen."),
                resposta_llm("ditar_prompt", "orbita", "Add tests to the signup screen."),
                resposta_llm("ditar_prompt", "orbita", "Delete the old logs."),
                resposta_llm("ditar_prompt", "atlas", "Ask me which test file to keep."),
            ],
            lingua="en",
            canal=CanalFalso(),
        )
        self.log = LogDaSessao(pasta=self.pasta, consola=StringIO(), quando=INICIO, relogio_de_parede=self.parede)
        self.addCleanup(self.log.fechar)
        self.m.jarvis.log = self.log
        self.log.linha("jarvis a arrancar | log em logs/jarvis-teste.log")
        self.log.linha("configuracao: 2 projeto(s) | lingua=en | motor=motor-falso (cpu) | voz=ligada | forja=configurada")
        self.log.bruto("   JARVIS PRONTO em 4.2 s (meta <= 30 s)")
        self.log.bruto("   ouvido: Microfone Ficticio de Teste (MME)")
        self.projetos = {"<projeto-1>": "atlas", "<projeto-2>": "orbita"}
        self.sessao = ac.Sessao(lingua="en", projetos=self.projetos)

    def _passar(self, segundos: float) -> None:
        self.parede.agora += datetime.timedelta(seconds=segundos)
        self.m.avancar(segundos)

    def _tarefa(self, id_: str, frases: list[str]) -> None:
        registo = ac.RegistoDaTarefa(id_, ac.FEITA, inicio=agora_iso(self.parede()), fez_o_pedido=True)
        for texto in frases:
            self._passar(2)
            self.m.ouvir(texto)
        self._passar(2)
        registo.fim = agora_iso(self.parede())
        self.sessao.tarefas.append(registo)
        self._passar(5)

    def _resultados(self):
        self.log.fechar()
        frases, eventos = ac.ler_logs([self.log.caminho])
        atribuidas = ac.atribuir(self.sessao.tarefas, frases, eventos)
        return {
            registo.id: ac.avaliar_tarefa(POR_ID[registo.id], registo, *atribuidas[registo.id], self.projetos)
            for registo in self.sessao.tarefas
        }, frases

    def test_horas_ditado_correcao_cancelamento_e_conversa(self) -> None:
        self._tarefa("l-01", ["what time is it"])
        self._tarefa("a-01", ["tell atlas to add a test for the config loader", "yes"])
        self._tarefa("a-04", ["tell orbita to add tests to the login screen", "no, change login to signup", "yes"])
        self._tarefa("a-06", ["tell orbita to delete the old logs", "cancel"])
        # So aqui o Claude responde com uma pergunta: abre a janela de conversa.
        self.m.canal.resposta = self.pergunta
        # "the second one" e uma resposta curta: vai logo, sem recap nem segundo "yes".
        self._tarefa("d-01", ["ask atlas to ask me which test file to keep", "yes", "the second one"])
        resultados, frases = self._resultados()

        self.assertTrue(all(r.acertou for r in resultados.values()), {k: r.intencao_obtida for k, r in resultados.items()})
        self.assertTrue(
            all(r.fluxo_no_log for r in resultados.values()), {k: r.falta_no_log for k, r in resultados.items()}
        )
        self.assertTrue(resultados["a-01"].aceite_sem_correcao)
        self.assertFalse(resultados["a-04"].aceite_sem_correcao, "houve correcao")
        self.assertIsNone(resultados["a-06"].aceite_sem_correcao, "cancelar nao conta para os aceites")
        self.assertTrue(resultados["d-01"].aceite_sem_correcao)
        self.assertTrue(any(f.na_conversa for f in resultados["d-01"].frases))
        # Latencias: horas e recap medidos (a voz falsa comeca a soar 100 ms depois).
        horas = [f for f in frases if f.intencao == "horas"]
        self.assertEqual([f.primeira_fala_ms for f in horas], [100.0])
        self.assertTrue(all(f.sinal_de_vida_ms is not None for f in frases))
        self.assertTrue(all(f.fonte == ac.FONTE_MICROFONE and f.lingua == "en" for f in frases))
        # Nada foi enviado no cancelamento; o ditado e a resposta da conversa foram.
        self.assertEqual([p for p, _ in self.m.canal.recebidos], ["atlas", "orbita", "atlas", "atlas"])
        self.assertEqual(self.m.canal.recebidos[-1], ("atlas", "the second one"))
        entregas = [e for e in ac.atribuir(self.sessao.tarefas, *ac.ler_logs([self.log.caminho]))["d-01"][1]
                    if e.tipo == "entregue"]
        self.assertEqual(len(entregas), 2, "o ditado e a resposta curta contam como entregues")

    def test_projeto_errado_nao_conta_a_primeira(self) -> None:
        # O Sponsor disse atlas, a transcricao saiu orbita: o recap mostra-o e ele confirma na mesma.
        self.m.llm.respostas[0] = resposta_llm("ditar_prompt", "orbita", "Add a test for the config loader.")
        self._tarefa("l-01", ["what time is it"])
        self._tarefa("a-01", ["tell orbita to add a test for the config loader", "yes"])
        resultados, _ = self._resultados()
        self.assertFalse(resultados["a-01"].acertou)
        self.assertEqual(resultados["a-01"].projeto_obtido, "errado")
        self.assertTrue(resultados["a-01"].fluxo_no_log, "foi confirmado, so que no projeto errado")

    def test_frase_fora_da_janela_nao_conta(self) -> None:
        self._passar(2)
        self.m.ouvir("what time is it")  # bem antes de a tarefa comecar
        self._passar(30)
        self._tarefa("a-01", ["tell atlas to add a test for the config loader", "yes"])
        resultados, _ = self._resultados()
        self.assertEqual(resultados["a-01"].intencao_obtida, "ditar_prompt")
        self.assertEqual(len(resultados["a-01"].frases), 2)


# --- Leitura do log ----------------------------------------------------------------


class TestLerLog(unittest.TestCase):
    def test_cada_processo_recomeca_a_numeracao_e_os_wav_nao_contam(self) -> None:
        log = ac.EscritorDeLogFalso(INICIO)
        log.arranque(microfone=False)
        log.frase("horas")
        log.arranque(lingua="pt")
        log.frase("horas")
        frases, _ = ac.ler_log(log.linhas)
        self.assertEqual([(f.segmento, f.numero, f.fonte) for f in frases], [(1, 1, "ficheiro"), (2, 1, "microfone")])
        self.assertEqual(frases[1].lingua, "pt")

    def test_log_sem_cabecalho_e_fonte_desconhecida(self) -> None:
        log = ac.EscritorDeLogFalso(INICIO)
        log.frase("horas")
        frases, _ = ac.ler_log(log.linhas)
        self.assertEqual(frases[0].fonte, ac.FONTE_DESCONHECIDA)

    def test_jarvis_a_correr(self) -> None:
        log = ac.EscritorDeLogFalso(INICIO)
        self.assertFalse(ac.jarvis_a_correr(log.linhas))
        log.arranque()
        self.assertTrue(ac.jarvis_a_correr(log.linhas))
        log.linha("jarvis terminado")
        self.assertFalse(ac.jarvis_a_correr(log.linhas))
        log.arranque(microfone=False)
        self.assertFalse(ac.jarvis_a_correr(log.linhas), "a medir com WAV nao e o jarvis a ouvir o Sponsor")

    def test_o_log_do_dia_anterior_entra_quando_a_sessao_passa_a_meia_noite(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            for dia in (24, 25, 26):
                (Path(pasta) / f"jarvis-2026-09-{dia}.log").write_text("", encoding="utf-8")
            sessao = ac.Sessao(lingua="en", projetos=PROJETOS)
            sessao.tarefas.append(ac.RegistoDaTarefa("l-01", ac.FEITA, "2026-09-25 23:59:00.000", "2026-09-26 00:01:00.000"))
            nomes = [c.name for c in ac.logs_da_sessao(sessao, Path(pasta))]
        self.assertEqual(nomes, ["jarvis-2026-09-24.log", "jarvis-2026-09-25.log", "jarvis-2026-09-26.log"])


# --- As metas ----------------------------------------------------------------------


class TestMetas(unittest.TestCase):
    def setUp(self) -> None:
        self.sessao, self.linhas = ac.sessao_falsa(TAREFAS, PROJETOS, INICIO)

    def test_tudo_como_o_guiao_pede_cumpre(self) -> None:
        avaliacao = _avaliar(self.sessao, self.linhas)
        self.assertEqual((avaliacao.estado, avaliacao.falhas), (ac.ESTADO_CUMPRIDA, []))
        ditados = [t for t in TAREFAS if t.conta_para_aceites]
        self.assertEqual(avaliacao.aceites, (len(ditados), len(ditados)))
        self.assertEqual(avaliacao.locais[0], avaliacao.locais[1])
        self.assertEqual(len(avaliacao.horas_ms), sum(t.intencao == "horas" for t in TAREFAS))

    def _com_linhas_trocadas(self, velho: str, novo: str, vezes: int = 1) -> list[str]:
        texto = "\n".join(self.linhas)
        self.assertIn(velho, texto)
        return texto.replace(velho, novo, vezes).splitlines()

    def test_intencao_errada_abaixo_de_90_por_cento_falha(self) -> None:
        linhas = self._com_linhas_trocadas("intencao=horas", "intencao=desconhecido", 3)
        avaliacao = _avaliar(self.sessao, linhas)
        self.assertEqual(avaliacao.estado, ac.ESTADO_NAO_CUMPRIDA)
        self.assertTrue(any(f.startswith("intenção e projeto à primeira") for f in avaliacao.falhas))

    def test_sim_mal_ouvido_nao_e_correcao_mas_correcao_e(self) -> None:
        log = ac.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase("ditar_prompt", "exemplo-um", desfecho="pendente", motivo="a espera de confirmacao")
        log.frase(None, resposta_ao_recap=True, desfecho="pendente", motivo="resposta nao percebida")
        log.frase(None, resposta_ao_recap=True, desfecho="executado", motivo="confirmado")
        frases, _ = ac.ler_log(log.linhas)
        self.assertTrue(ac._aceite_a_primeira(frases[0], frases[1:]))
        corrigido = replace(frases[1], motivo="a espera de confirmacao")
        self.assertFalse(ac._aceite_a_primeira(frases[0], [corrigido, frases[2]]))
        cancelado = replace(frases[2], desfecho="cancelado")
        self.assertFalse(ac._aceite_a_primeira(frases[0], [cancelado]))

    def test_ditados_corrigidos_abaixo_de_80_por_cento_falham(self) -> None:
        # Cada ditado que conta para a meta passa a ter uma correcao antes do "sim".
        log = ac.EscritorDeLogFalso(INICIO)
        log.arranque()
        sessao = ac.Sessao(lingua="en", projetos=PROJETOS)
        for tarefa in TAREFAS:
            log.avancar(10)
            registo = ac.RegistoDaTarefa(tarefa.id, ac.FEITA, inicio=agora_iso(log.agora), fez_o_pedido=True, inventou=False)
            log.avancar(1)
            if tarefa.conta_para_aceites:
                corrigida = replace(tarefa, fluxo="corrigir")
                ac.escrever_tarefa_falsa(log, corrigida, PROJETOS)
            else:
                ac.escrever_tarefa_falsa(log, tarefa, PROJETOS)
            log.avancar(1)
            registo.fim = agora_iso(log.agora)
            sessao.tarefas.append(registo)
        avaliacao = _avaliar(sessao, log.linhas)
        self.assertEqual(avaliacao.aceites[0], 0)
        self.assertTrue(any(f.startswith("ditados aceites sem correção") for f in avaliacao.falhas))

    def test_um_pedido_inventado_falha(self) -> None:
        self.sessao.registo("a-02").inventou = True
        avaliacao = _avaliar(self.sessao, self.linhas)
        self.assertEqual(avaliacao.inventados[0], 1)
        self.assertIn("pedidos inventados: 1 (meta 0)", avaliacao.falhas)

    def test_latencias_contra_as_metas_do_jarvis_integrado(self) -> None:
        sessao, linhas = ac.sessao_falsa(TAREFAS, PROJETOS, INICIO, horas_ms=1250.0, recap_ms=2400.0)
        avaliacao = _avaliar(sessao, linhas)
        self.assertEqual(avaliacao.falhas, ["horas → resposta falada: p50 1250 ms > 1200 ms"])
        linhas = [l.replace("primeiro sinal de vida: 300 ms", "primeiro sinal de vida: 1100 ms", 1) for l in self.linhas]
        avaliacao = _avaliar(self.sessao, linhas)
        self.assertEqual(avaliacao.falhas, ["primeiro sinal de vida: máximo 1100 ms > 1000 ms"])

    def test_um_caso_sem_cenario_passado_falha_a_cobertura(self) -> None:
        for registo in self.sessao.tarefas:
            if registo.id.startswith("d-"):
                registo.fez_o_pedido = False
        avaliacao = _avaliar(self.sessao, self.linhas)
        self.assertEqual(avaliacao.cobertura["d"], (0, 2))
        self.assertIn("caso (d): nenhum cenário passado (2 feito(s))", avaliacao.falhas)

    def test_o_fluxo_tem_de_estar_no_log_alem_do_sim_do_sponsor(self) -> None:
        linhas = [l for l in self.linhas if "fez uma pergunta" not in l]
        avaliacao = _avaliar(self.sessao, linhas)
        d01 = next(r for r in avaliacao.resultados if r.tarefa.id == "d-01")
        self.assertFalse(d01.passou)
        self.assertEqual(d01.falta_no_log, "o Claude nao fez nenhuma pergunta")

    def test_cancelar_nao_passa_se_algo_foi_enviado(self) -> None:
        avaliacao = _avaliar(self.sessao, self.linhas)
        a06 = next(r for r in avaliacao.resultados if r.tarefa.id == "a-06")
        self.assertTrue(a06.passou)
        fim = ac.ler_instante(a06.registo.fim) - datetime.timedelta(seconds=1)
        extra = f"{agora_iso(fim)} canal | prompt confirmado entregue ao canal do exemplo-dois: 'x'"
        avaliacao = _avaliar(self.sessao, self.linhas + [extra])
        a06 = next(r for r in avaliacao.resultados if r.tarefa.id == "a-06")
        self.assertFalse(a06.passou)

    def test_poucas_tarefas_feitas_e_incompleta(self) -> None:
        self.sessao.tarefas = self.sessao.tarefas[:10]
        avaliacao = _avaliar(self.sessao, self.linhas)
        self.assertEqual(avaliacao.estado, ac.ESTADO_INCOMPLETA)
        self.assertIn("--continuar", avaliacao.pendencias[0])


# --- A sessao de hoje: frases fora das janelas estritas e dois jarvis --------------


ACORDAR = "palavra de ativacao seguida de um acordar curto (nada interpretado)"


class _SessaoDeHoje:
    """Um log como o da sessao de aceitacao que contou mal, com frases ficticias.

    O jarvis do microfone (PID 4242) arranca com um jarvis antigo esquecido
    (PID 3131) ainda aberto, ou caido, e um jarvis com --wav a medir a meio.
    O Sponsor comeca a falar antes do Enter de inicio, carrega no Enter final
    antes de responder ao recap, e a resposta do Claude, a pergunta e a
    resposta na conversa so chegam depois do Enter final.
    """

    PID_NOVO, PID_ANTIGO, PID_WAV = 4242, 3131, 5151

    def __init__(self, antigo: str | None = None, *, com_pids: bool = True) -> None:
        self.linhas: list[str] = []
        self.novo = ac.EscritorDeLogFalso(INICIO, self.linhas)
        self.antigo = ac.EscritorDeLogFalso(INICIO, self.linhas)
        self.wav = ac.EscritorDeLogFalso(INICIO, self.linhas)
        self.modo_antigo = antigo
        self.com_pids = com_pids
        self.sessao = ac.Sessao(lingua="en", projetos=dict(PROJETOS))
        #: id da tarefa -> numeros das frases do jarvis novo que ela causou.
        self.esperado: dict[str, list[int]] = {}
        self.eventos_esperados: dict[str, list[str]] = {}
        self._numero = 0

    def _pid(self, pid: int) -> int | None:
        return pid if self.com_pids else None

    def ir(self, segundos: float) -> None:
        for escritor in (self.novo, self.antigo, self.wav):
            escritor.agora = INICIO + datetime.timedelta(seconds=segundos)

    def comeca(self, id_: str, segundos: float) -> None:
        """O Enter de inicio; nao mexe no relogio do log (a frase pode ja ter sido escrita)."""
        inicio = INICIO + datetime.timedelta(seconds=segundos)
        registo = ac.RegistoDaTarefa(id_, ac.FEITA, inicio=agora_iso(inicio), fez_o_pedido=True)
        registo.inventou = False if POR_ID[id_].pergunta_inventados else None
        self.sessao.tarefas.append(registo)
        self.esperado[id_], self.eventos_esperados[id_] = [], []

    def acaba(self, segundos: float) -> None:
        self.ir(segundos)
        self.sessao.tarefas[-1].fim = agora_iso(self.novo.agora)

    def frase(self, tarefa: str, segundos: float, *args, antigo_ouve: bool = False, **kwargs) -> None:
        self.ir(segundos)
        self.novo.frase(*args, **kwargs)
        self._numero += 1
        self.esperado[tarefa].append(self._numero)
        if antigo_ouve and self.modo_antigo == "vivo":
            self.antigo.frase(*args, **kwargs)  # o antigo tambem ouviu e respondeu

    def evento(self, tarefa: str, segundos: float, tipo: str, projeto: str) -> None:
        self.ir(segundos)
        getattr(self.novo, tipo)(projeto)
        self.eventos_esperados[tarefa].append(tipo)

    def montar(self) -> "_SessaoDeHoje":
        um, dois = PROJETOS["<projeto-1>"], PROJETOS["<projeto-2>"]
        pendente = {"desfecho": "pendente", "motivo": "a espera de confirmacao"}
        sim = {"resposta_ao_recap": True, "desfecho": "executado", "motivo": "confirmado"}
        if self.modo_antigo:
            # O jarvis antigo, aberto de manha e esquecido: ja tinha ouvido tres frases.
            self.ir(5)
            self.antigo.arranque(pid=self._pid(self.PID_ANTIGO))
            for segundos in (8, 11, 14):
                self.ir(segundos)
                self.antigo.frase("horas")
        self.ir(20)
        self.novo.arranque(pid=self._pid(self.PID_NOVO))

        # l-01: o Sponsor falou e o jarvis respondeu antes de ele carregar no Enter de inicio.
        self.comeca("l-01", 60)
        self.frase("l-01", 59, "horas", escuta_ms=2500.0, antigo_ouve=True)
        self.acaba(65)
        # a-01: Enter final antes do "yes"; entregue e resposta do Claude depois.
        self.comeca("a-01", 80)
        self.frase("a-01", 85, "ditar_prompt", um, escuta_ms=2500.0, antigo_ouve=True, **pendente)
        self.acaba(88)
        self.frase("a-01", 91, None, escuta_ms=1000.0, **sim)
        self.evento("a-01", 91.5, "entregue", um)
        self.evento("a-01", 110, "resposta", um)
        # a-06: o "abort" chega depois do Enter final.
        self.comeca("a-06", 130)
        self.frase("a-06", 135, "ditar_prompt", dois, **pendente)
        self.acaba(137)
        self.frase("a-06", 140, None, resposta_ao_recap=True, desfecho="cancelado", motivo="cancelado pelo utilizador")
        # d-01: a resposta do Claude (uma pergunta) e a conversa chegam depois do Enter final.
        self.comeca("d-01", 160)
        self.frase("d-01", 165, "ditar_prompt", um, antigo_ouve=True, **pendente)
        self.frase("d-01", 169, None, **sim)
        self.evento("d-01", 169.5, "entregue", um)
        self.acaba(172)
        self.evento("d-01", 200, "resposta", um)
        self.evento("d-01", 200.5, "pergunta", um)
        self.frase("d-01", 205, "conversa", um, na_conversa=True, **pendente)
        self.frase("d-01", 209, None, **sim)
        self.evento("d-01", 209.5, "entregue", um)
        # l-04: um jarvis com --wav mede a meio da tarefa; as frases dele nao contam.
        self.comeca("l-04", 240)
        self.ir(241)
        self.wav.arranque(microfone=False, pid=self._pid(self.PID_WAV))
        self.ir(242)
        self.wav.frase("horas")
        self.wav.terminado()
        self.frase("l-04", 243, "dormir")
        self.acaba(245)
        # l-05: acordar pela palavra de ativacao seguida de "Up."
        self.comeca("l-05", 260)
        self.frase("l-05", 263, None, detalhe=ACORDAR)
        self.acaba(266)
        # a-04: correcao e "yes" depois do Enter final; a tarefa seguinte comeca logo.
        self.comeca("a-04", 290)
        self.frase("a-04", 294, "ditar_prompt", dois, **pendente)
        self.frase("a-04", 298, None, resposta_ao_recap=True, **pendente)
        self.acaba(300)
        self.frase("a-04", 303, None, escuta_ms=1000.0, **sim)
        self.evento("a-04", 303.5, "entregue", dois)
        # l-03: comecou a falar 1,5 s antes do Enter, ja depois do Enter final de a-04.
        self.comeca("l-03", 306)
        self.frase("l-03", 307, "horas", escuta_ms=2500.0)
        self.acaba(310)
        return self


class TestSessaoDeHoje(unittest.TestCase):
    def _atribuidas(self, montagem: _SessaoDeHoje):
        frases, eventos = ac.ler_log(montagem.linhas)
        return ac.atribuir(montagem.sessao.tarefas, frases, eventos), frases, eventos

    def test_cada_frase_conta_para_a_tarefa_que_a_causou(self) -> None:
        montagem = _SessaoDeHoje().montar()
        atribuidas, frases, _ = self._atribuidas(montagem)
        microfone = {f.numero for f in frases if f.fonte == ac.FONTE_MICROFONE}
        self.assertEqual(microfone, set(range(1, 16)))
        obtido = {
            id_: [f.numero for f in da_tarefa if f.fonte == ac.FONTE_MICROFONE]
            for id_, (da_tarefa, _) in atribuidas.items()
        }
        self.assertEqual(obtido, montagem.esperado)
        eventos = {id_: [e.tipo for e in eventos_] for id_, (_, eventos_) in atribuidas.items()}
        self.assertEqual(eventos, montagem.eventos_esperados)
        # A frase do --wav cai na janela de l-04 mas fica de fora.
        self.assertEqual([f.fonte for f in atribuidas["l-04"][0]], [ac.FONTE_FICHEIRO, ac.FONTE_MICROFONE])

    def test_nenhuma_tarefa_feita_fica_sem_frases_e_os_fluxos_correm(self) -> None:
        montagem = _SessaoDeHoje().montar()
        avaliacao = ac.avaliar_sessao(montagem.sessao, TAREFAS, *ac.ler_log(montagem.linhas))
        self.assertNotEqual(avaliacao.estado, ac.ESTADO_INVALIDA)
        resultados = {r.tarefa.id: r for r in avaliacao.resultados}
        self.assertEqual(set(resultados), set(montagem.esperado))
        for id_, resultado in resultados.items():
            self.assertNotEqual(resultado.falta_no_log, "nenhuma frase do microfone nesta janela", id_)
            self.assertEqual(resultado.falta_no_log, "", id_)
            self.assertTrue(resultado.acertou, (id_, resultado.intencao_obtida, resultado.projeto_obtido))
            self.assertTrue(resultado.passou, id_)
        self.assertTrue(resultados["a-01"].aceite_sem_correcao)
        self.assertFalse(resultados["a-04"].aceite_sem_correcao)
        self.assertEqual(resultados["l-04"].excluidas, 1)

    def test_frases_da_tarefa_seguinte_nunca_contam_para_a_anterior(self) -> None:
        montagem = _SessaoDeHoje().montar()
        atribuidas, _, _ = self._atribuidas(montagem)
        # A frase de l-03 comecou antes do Enter de l-03 e depois do Enter final de a-04.
        self.assertEqual([f.numero for f in atribuidas["l-03"][0]], [15])
        self.assertNotIn(15, [f.numero for f in atribuidas["a-04"][0]])

    def test_resposta_ao_recap_depois_de_a_tarefa_seguinte_comecar_nao_conta(self) -> None:
        log = ac.EscritorDeLogFalso(INICIO)
        log.arranque()
        sessao = ac.Sessao(lingua="en", projetos=dict(PROJETOS))
        log.avancar(10)
        a01 = ac.RegistoDaTarefa("a-01", ac.FEITA, inicio=agora_iso(log.agora))
        log.avancar(3)
        log.frase("ditar_prompt", PROJETOS["<projeto-1>"], desfecho="pendente", motivo="a espera de confirmacao")
        log.avancar(2)
        a01.fim = agora_iso(log.agora)
        log.avancar(5)
        l01 = ac.RegistoDaTarefa("l-01", ac.FEITA, inicio=agora_iso(log.agora))
        log.avancar(4)
        log.frase(None, resposta_ao_recap=True, desfecho="executado", motivo="confirmado", escuta_ms=1000.0)
        log.avancar(2)
        l01.fim = agora_iso(log.agora)
        sessao.tarefas += [a01, l01]
        atribuidas = ac.atribuir(sessao.tarefas, *ac.ler_log(log.linhas))
        self.assertEqual([f.numero for f in atribuidas["a-01"][0]], [1])
        self.assertEqual(atribuidas["l-01"][0], [])

    def test_o_acordar_pela_palavra_de_ativacao_e_lido(self) -> None:
        montagem = _SessaoDeHoje().montar()
        atribuidas, _, _ = self._atribuidas(montagem)
        self.assertEqual([f.intencao for f in atribuidas["l-05"][0]], ["acordar"])

    def test_o_aviso_da_sessao_guiada_ve_a_frase_comecada_antes_do_enter(self) -> None:
        montagem = _SessaoDeHoje().montar()
        frases, _ = ac.ler_log(montagem.linhas)
        l01 = montagem.sessao.registo("l-01")
        self.assertEqual(ac.frases_do_microfone_na_janela(frases, l01.inicio, l01.fim), 1)


class TestSegundaInstancia(unittest.TestCase):
    def test_dois_jarvis_com_o_microfone_tornam_a_sessao_invalida(self) -> None:
        montagem = _SessaoDeHoje("vivo").montar()
        frases, eventos = ac.ler_log(montagem.linhas)
        duplicados = [e for e in eventos if e.tipo == ac.TIPO_SEGUNDA_INSTANCIA]
        self.assertEqual(len(duplicados), 3)
        self.assertEqual({e.pids for e in duplicados}, {(3131, 4242)})
        # As frases do jarvis novo continuam com a numeracao dele, sem mistura.
        novas = sorted(f.numero for f in frases if f.segmento == 2)
        self.assertEqual(novas, list(range(1, 16)))
        avaliacao = ac.avaliar_sessao(montagem.sessao, TAREFAS, frases, eventos)
        self.assertEqual(avaliacao.estado, ac.ESTADO_INVALIDA)
        texto = ac.texto_da_evidencia(avaliacao)
        self.assertIn("**Estado: INVÁLIDA**", texto)
        self.assertIn("dois jarvis com o microfone ao mesmo tempo", texto)
        self.assertIn("PID 3131", texto)
        self.assertIn("PID 4242", texto)
        self.assertNotIn("## Metas", texto, "numa sessao invalida nenhuma meta e declarada cumprida")
        self.assertNotIn("| sim |", texto)
        self.assertNotIn(ac.ESTADO_CUMPRIDA + "**", texto)
        for nome in PROJETOS.values():
            self.assertNotIn(nome, texto)
        self.assertNotIn("ficticia", texto)

    def test_sem_pid_no_log_tambem_e_invalida(self) -> None:
        montagem = _SessaoDeHoje("vivo", com_pids=False).montar()
        avaliacao = ac.avaliar_sessao(montagem.sessao, TAREFAS, *ac.ler_log(montagem.linhas))
        self.assertEqual(avaliacao.estado, ac.ESTADO_INVALIDA)
        self.assertIn("dois jarvis com o microfone ao mesmo tempo:", avaliacao.pendencias[0])
        self.assertNotIn("PID", avaliacao.pendencias[0])

    def test_um_jarvis_antigo_que_deixou_de_escrever_nao_invalida(self) -> None:
        for com_pids in (True, False):
            with self.subTest(com_pids=com_pids):
                montagem = _SessaoDeHoje("caido", com_pids=com_pids).montar()
                frases, eventos = ac.ler_log(montagem.linhas)
                self.assertFalse([e for e in eventos if e.tipo == ac.TIPO_SEGUNDA_INSTANCIA])
                avaliacao = ac.avaliar_sessao(montagem.sessao, TAREFAS, frases, eventos)
                self.assertNotEqual(avaliacao.estado, ac.ESTADO_INVALIDA)
                atribuidas = ac.atribuir(montagem.sessao.tarefas, frases, eventos)
                obtido = {
                    id_: [f.numero for f in da_tarefa if f.fonte == ac.FONTE_MICROFONE and f.segmento == 2]
                    for id_, (da_tarefa, _) in atribuidas.items()
                }
                self.assertEqual(obtido, montagem.esperado)

    def test_um_jarvis_com_wav_ao_mesmo_tempo_nao_invalida(self) -> None:
        montagem = _SessaoDeHoje().montar()
        _, eventos = ac.ler_log(montagem.linhas)
        self.assertFalse([e for e in eventos if e.tipo == ac.TIPO_SEGUNDA_INSTANCIA])

    def test_jarvis_terminado_antes_do_novo_arrancar_nao_invalida(self) -> None:
        log = ac.EscritorDeLogFalso(INICIO)
        log.arranque(pid=3131)
        log.frase("horas")
        log.terminado()
        log.avancar(10)
        log.arranque(pid=4242)
        log.frase("horas")
        _, eventos = ac.ler_log(log.linhas)
        self.assertFalse([e for e in eventos if e.tipo == ac.TIPO_SEGUNDA_INSTANCIA])


# --- PENDENTE: nada e simulado ----------------------------------------------------


class TestPendente(unittest.TestCase):
    def test_sem_sessao_pendente_com_o_passo_exato(self) -> None:
        avaliacao = ac.avaliar_sessao(None, TAREFAS, [], [])
        texto = ac.texto_da_evidencia(avaliacao)
        self.assertEqual(avaliacao.estado, ac.ESTADO_PENDENTE)
        self.assertIn("**Estado: PENDENTE — passo do Sponsor**", texto)
        for passo in ac.PASSO_DO_SPONSOR:
            self.assertIn(passo, texto)
        self.assertNotIn("## Metas", texto, "nenhuma meta e declarada sem a voz real")
        self.assertIn(ac.LINHA_DE_BASE["intencao"], texto)
        self.assertIn(ac.LINHA_DE_BASE["locais"], texto)
        self.assertIn(ac.LINHA_DE_BASE["horas"], texto)

    def test_so_voz_sintetica_de_ficheiros_e_pendente(self) -> None:
        sessao, linhas = ac.sessao_falsa(TAREFAS, PROJETOS, INICIO, microfone=False)
        avaliacao = _avaliar(sessao, linhas)
        self.assertEqual(avaliacao.estado, ac.ESTADO_PENDENTE)
        self.assertIn("nenhuma frase do microfone", avaliacao.pendencias[0])

    def test_sessao_que_nao_e_do_sponsor_nunca_conta(self) -> None:
        sessao, linhas = ac.sessao_falsa(TAREFAS, PROJETOS, INICIO, origem=ac.ORIGEM_AUTOTESTE)
        self.assertEqual(_avaliar(sessao, linhas).estado, ac.ESTADO_PENDENTE)

    def test_relatorio_sem_sessao_escreve_pendente(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            evidencia = Path(pasta) / "aceitacao.md"
            avaliacao = ac.relatorio(None, TAREFAS, evidencia, pasta_logs=Path(pasta))
            self.assertEqual(avaliacao.estado, ac.ESTADO_PENDENTE)
            self.assertIn("PENDENTE", evidencia.read_text(encoding="utf-8"))


# --- Evidencia ---------------------------------------------------------------------


class TestEvidencia(unittest.TestCase):
    def test_relatorio_de_uma_sessao_real_sem_nada_privado(self) -> None:
        sessao, linhas = ac.sessao_falsa(TAREFAS, PROJETOS, INICIO)
        with tempfile.TemporaryDirectory() as pasta:
            (Path(pasta) / "jarvis-2026-09-25.log").write_text("\n".join(linhas) + "\n", encoding="utf-8")
            caminho = Path(pasta) / "sessao-20260925-100000.json"
            ac.guardar_sessao(sessao, caminho)
            evidencia = Path(pasta) / "aceitacao.md"
            avaliacao = ac.relatorio(caminho, TAREFAS, evidencia, pasta_logs=Path(pasta))
            texto = evidencia.read_text(encoding="utf-8")
        self.assertEqual(avaliacao.estado, ac.ESTADO_CUMPRIDA)
        for nome in PROJETOS.values():
            self.assertNotIn(nome, texto)
        for privado in ("frase ficticia", "pedido ficticio", "prompt:", "texto:"):
            self.assertNotIn(privado, texto)
        self.assertIn("| (a) |", texto)
        self.assertIn("| (d) |", texto)
        self.assertIn("p50", texto)
        self.assertIn(ac.LINHA_DE_BASE["horas"], texto)

    def test_projeto_escolhido_quando_devia_perguntar(self) -> None:
        tarefa = POR_ID["a-07"]
        self.assertEqual(ac._projeto_relativo(tarefa, None, PROJETOS), (True, "nenhum"))
        self.assertFalse(ac._projeto_relativo(tarefa, "exemplo-um", PROJETOS)[0])


# --- A sessao guiada ---------------------------------------------------------------


def _teclas(sequencia):
    fila = list(sequencia)
    perguntas: list[str] = []

    def tecla(pergunta, validas):
        perguntas.append(pergunta)
        escolha = fila.pop(0)
        assert escolha in validas, (escolha, validas, pergunta)
        return escolha

    tecla.perguntas = perguntas
    return tecla


def _relogio():
    instantes = iter(INICIO + datetime.timedelta(seconds=s) for s in range(0, 100_000, 7))
    return lambda: next(instantes)


class TestSessaoGuiada(unittest.TestCase):
    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.caminho = Path(pasta.name) / "sessao.json"

    def _correr(self, sessao, teclas, **extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return ac.correr_sessao(
                sessao, TAREFAS, self.caminho, tecla=teclas, escrever=lambda _t: None, agora=_relogio(), **extra
            )

    def test_janelas_respostas_e_retoma(self) -> None:
        sessao = ac.Sessao(lingua="en", projetos=dict(PROJETOS))
        self.assertFalse(self._correr(sessao, _teclas(["", "", "s", "p", "q"])))
        guardada = ac.ler_sessao(self.caminho)
        self.assertEqual([(r.id, r.estado) for r in guardada.tarefas], [("l-01", "feita"), ("a-01", "saltada")])
        self.assertLess(guardada.tarefas[0].inicio, guardada.tarefas[0].fim)
        self.assertFalse(guardada.concluida)
        # Retoma na tarefa seguinte e acaba: s/n por tecla em cada uma.
        respostas = []
        for tarefa in TAREFAS[2:]:
            respostas += ["", "", "s"] + (["n"] if tarefa.pergunta_inventados else [])
        self.assertTrue(self._correr(guardada, _teclas(respostas)))
        final = ac.ler_sessao(self.caminho)
        self.assertTrue(final.concluida)
        self.assertEqual([r.id for r in final.tarefas], [t.id for t in TAREFAS])

    def test_sem_projeto_teste_as_tarefas_do_caso_c_sao_saltadas(self) -> None:
        sessao = ac.Sessao(lingua="en", projetos={k: v for k, v in PROJETOS.items() if k != "<projeto-teste>"})
        respostas = []
        for tarefa in TAREFAS:
            if not tarefa.pede_projeto_teste:
                respostas += ["", "", "s"] + (["n"] if tarefa.pergunta_inventados else [])
        self.assertTrue(self._correr(sessao, _teclas(respostas)))
        saltadas = [r.id for r in ac.ler_sessao(self.caminho).tarefas if r.estado == ac.SALTADA]
        self.assertEqual(saltadas, [t.id for t in TAREFAS if t.caso == "c"])

    def test_sem_frases_no_log_pode_repetir_a_tarefa(self) -> None:
        sessao = ac.Sessao(lingua="en", projetos=dict(PROJETOS))
        contagens = iter([0, 1])
        teclas = _teclas(["", "", "r", "", "", "s", "q"])
        self._correr(sessao, teclas, frases_na_janela=lambda _i, _f: next(contagens))
        guardada = ac.ler_sessao(self.caminho)
        self.assertEqual([r.id for r in guardada.tarefas], ["l-01"])
        self.assertIn("r repete", " ".join(teclas.perguntas))

    def test_os_exemplos_mostram_os_nomes_da_configuracao(self) -> None:
        linhas: list[str] = []
        sessao = ac.Sessao(lingua="pt", projetos=dict(PROJETOS))
        with contextlib.redirect_stdout(io.StringIO()):
            ac.correr_sessao(
                sessao, TAREFAS[:2], self.caminho, tecla=_teclas(["", "", "s", "q"]), escrever=linhas.append,
                agora=_relogio(),
            )
        texto = "\n".join(linhas)
        self.assertIn("diz ao exemplo-um", texto)
        self.assertNotIn("<projeto-1>", texto)


# --- O que o Sponsor ve no ecra ------------------------------------------------------

ROTULOS = (ac.ROTULO_DIZER, ac.ROTULO_RESPONDER, ac.ROTULO_ACONTECER)
FIM_DO_DITADO = {"en": "Don't change anything.", "pt": "Não mudes nada."}


def _ecra_de_todas_as_tarefas(lingua: str) -> dict[str, list[str]]:
    """O que correr_sessao escreve para cada tarefa do guiao (todas saltadas com p), por id."""
    linhas: list[str] = []
    sessao = ac.Sessao(lingua=lingua, projetos=dict(PROJETOS))
    with tempfile.TemporaryDirectory() as pasta, contextlib.redirect_stdout(io.StringIO()):
        ac.correr_sessao(
            sessao, TAREFAS, Path(pasta) / "sessao.json", tecla=_teclas(["p"] * len(TAREFAS)),
            escrever=linhas.append, agora=_relogio(),
        )
    blocos: dict[str, list[str]] = {}
    atual: list[str] | None = None
    for linha in linhas:
        cabecalho = re.match(r"^\[\d+/\d+\] ([a-z]-\d{2}) ", linha)
        if cabecalho:
            atual = blocos.setdefault(cabecalho.group(1), [])
        elif atual is not None:
            atual.append(linha)
    return blocos


def _valor(bloco: list[str], rotulo: str) -> str:
    (linha,) = [l for l in bloco if l.strip().startswith(rotulo)]
    return linha.strip()[len(rotulo):].strip()


class TestEcraDasTarefas(unittest.TestCase):
    def test_cada_tarefa_tem_as_tres_linhas_preenchidas_e_sem_marcadores(self) -> None:
        for lingua in ac.LINGUAS:
            blocos = _ecra_de_todas_as_tarefas(lingua)
            self.assertEqual(sorted(blocos), sorted(t.id for t in TAREFAS))
            for tarefa in TAREFAS:
                with self.subTest(lingua=lingua, tarefa=tarefa.id):
                    bloco = blocos[tarefa.id]
                    for rotulo in ROTULOS:
                        self.assertTrue(_valor(bloco, rotulo), f"{rotulo} vazia")
                    self.assertEqual(
                        _valor(bloco, ac.ROTULO_DIZER),
                        '"' + ac.trocar_marcadores(tarefa.exemplos[lingua], PROJETOS) + '"',
                    )
                    for linha in bloco:
                        self.assertNotRegex(linha, r"<[^>]*>", "marcador por trocar")

    def test_cancelar_diz_abort_e_os_fluxos_que_enviam_dizem_yes(self) -> None:
        for lingua in ac.LINGUAS:
            blocos = _ecra_de_todas_as_tarefas(lingua)
            for tarefa in TAREFAS:
                resposta = _valor(blocos[tarefa.id], ac.ROTULO_RESPONDER)
                with self.subTest(lingua=lingua, tarefa=tarefa.id, fluxo=tarefa.fluxo):
                    if tarefa.fluxo == "cancelar":
                        self.assertIn("ABORT", resposta)
                        self.assertNotIn("YES", resposta)
                    elif tarefa.fluxo in ("confirmar", "corrigir", "projeto", "conversa"):
                        self.assertIn("YES", resposta)
                        self.assertNotIn("ABORT", resposta)
                    else:
                        self.assertIn("nada", resposta)
                        self.assertNotIn("YES", resposta)
        self.assertTrue(any(t.fluxo == "cancelar" for t in TAREFAS))

    def test_as_respostas_sao_as_palavras_que_o_jarvis_aceita(self) -> None:
        self.assertEqual(set(ac.RESPOSTAS_DO_FLUXO), set(ac.FLUXOS))
        for lingua in ac.LINGUAS:
            self.assertIn("cancel", ac.RESPOSTAS_DO_FLUXO["cancelar"][lingua])
        self.assertIn('"no, change tests to docs"', ac.RESPOSTAS_DO_FLUXO["corrigir"]["en"])
        self.assertIn('"add that it is urgent"', ac.RESPOSTAS_DO_FLUXO["corrigir"]["en"])
        self.assertIn('"não, muda testes para documentação"', ac.RESPOSTAS_DO_FLUXO["corrigir"]["pt"])
        self.assertIn('"acrescenta que é urgente"', ac.RESPOSTAS_DO_FLUXO["corrigir"]["pt"])
        self.assertIn('"sim"', ac.RESPOSTAS_DO_FLUXO["confirmar"]["pt"])
        self.assertIn('"aborta"', ac.RESPOSTAS_DO_FLUXO["cancelar"]["pt"])

    def test_nada_mostra_o_marcador_literal_da_correcao(self) -> None:
        # O Sponsor leu "change X to Y" a letra: os exemplos sao frases concretas.
        literais = re.compile(r"\bX (to|para) Y\b|change X\b|muda X\b|\"add \.\.\.\"|\"acrescenta \.\.\.\"")
        guiao = ac.GUIAO.read_text(encoding="utf-8")
        self.assertNotRegex(guiao, literais)
        for lingua in ac.LINGUAS:
            with self.subTest(lingua=lingua):
                ecra = ac.texto_do_ecra_inicial(lingua, PROJETOS)
                blocos = _ecra_de_todas_as_tarefas(lingua)
                texto = "\n".join([*ecra, *(l for b in blocos.values() for l in b)])
                self.assertNotRegex(texto, literais)
                self.assertIn('"no, change tests to docs"', "\n".join(ecra))
                self.assertIn('"não, muda testes para documentação"', "\n".join(ecra))

    def test_cada_tarefa_de_correcao_diz_a_frase_do_seu_exemplo(self) -> None:
        corrigir = [t for t in TAREFAS if t.fluxo == "corrigir"]
        self.assertEqual([t.id for t in corrigir], ["a-04", "a-05"])
        self.assertEqual(set(ac.RESPOSTAS_DE_CORRECAO), {t.id for t in corrigir})
        # A palavra trocada existe no exemplo de cada lingua.
        troca = {"a-04": {"en": "login", "pt": "login"}}
        for lingua in ac.LINGUAS:
            blocos = _ecra_de_todas_as_tarefas(lingua)
            for tarefa in corrigir:
                with self.subTest(lingua=lingua, tarefa=tarefa.id):
                    resposta = _valor(blocos[tarefa.id], ac.ROTULO_RESPONDER)
                    self.assertEqual(resposta, ac.RESPOSTAS_DE_CORRECAO[tarefa.id][lingua])
                    frase = re.match(r'^"([^"]+)"', resposta).group(1)
                    self.assertIn(f'"{frase}"', tarefa.acontecer)
                    self.assertNotEqual(frase, re.match(r'^"([^"]+)"', ac.RESPOSTAS_DO_FLUXO["corrigir"][lingua]).group(1))
                    if tarefa.id in troca:
                        self.assertIn(troca[tarefa.id][lingua], tarefa.exemplos[lingua])
        self.assertIn("no, change login to settings", ac.RESPOSTAS_DE_CORRECAO["a-04"]["en"])
        self.assertIn("não, muda login para definições", ac.RESPOSTAS_DE_CORRECAO["a-04"]["pt"])
        self.assertIn("add that the summary has three lines", ac.RESPOSTAS_DE_CORRECAO["a-05"]["en"])
        self.assertIn("acrescenta que o resumo tem três linhas", ac.RESPOSTAS_DE_CORRECAO["a-05"]["pt"])

    def test_as_tarefas_do_run_dizem_que_mexem_num_run_forja_real(self) -> None:
        blocos = _ecra_de_todas_as_tarefas("pt")
        ids = [t.id for t in TAREFAS if t.caso == "c"]
        self.assertEqual(ids, ["c-01", "c-02", "c-03", "c-04"])
        for id_ in ids:
            with self.subTest(id_):
                self.assertTrue(
                    any("a sério um run FORJA curto" in l or "a sério o run FORJA curto" in l for l in blocos[id_])
                )
                self.assertIn("exemplo-tres", "\n".join(blocos[id_]))

    def test_os_ditados_e_o_lancamento_so_leem(self) -> None:
        from jarvis.forja_voz import validar_objetivo

        for tarefa in TAREFAS:
            if tarefa.intencao not in ("ditar_prompt", "lancar_run"):
                continue
            for lingua, fim in FIM_DO_DITADO.items():
                with self.subTest(tarefa=tarefa.id, lingua=lingua):
                    self.assertTrue(tarefa.exemplos[lingua].endswith(fim), tarefa.exemplos[lingua])
        lancamentos = [t for t in TAREFAS if t.intencao == "lancar_run"]
        self.assertTrue(lancamentos)
        for tarefa in lancamentos:
            for lingua, ligacao in (("en", " to "), ("pt", " para ")):
                objetivo = tarefa.exemplos[lingua].split(tarefa.projeto + ligacao, 1)[1]
                self.assertEqual(validar_objetivo(objetivo), objetivo)
                self.assertTrue(objetivo.endswith(FIM_DO_DITADO[lingua]))

    def test_o_que_deve_acontecer_nos_ditados_que_enviam(self) -> None:
        for tarefa in TAREFAS:
            if tarefa.intencao != "ditar_prompt" or tarefa.fluxo not in ("confirmar", "projeto", "conversa"):
                continue
            with self.subTest(tarefa.id):
                for trecho in ('"Send it?"', '"Envio?"', '"Sent to <projeto-', "janela do Claude Code"):
                    self.assertIn(trecho, tarefa.acontecer)
        (cancelar,) = [t for t in TAREFAS if t.fluxo == "cancelar"]
        self.assertIn("não envia nada", cancelar.acontecer)

    def test_as_perguntas_do_fim_tem_ajuda(self) -> None:
        linhas: list[str] = []
        teclas = _teclas(["", "", "s", "n", "q"])
        sessao = ac.Sessao(lingua="pt", projetos=dict(PROJETOS))
        with tempfile.TemporaryDirectory() as pasta, contextlib.redirect_stdout(io.StringIO()):
            ac.correr_sessao(
                sessao, [POR_ID["a-01"], POR_ID["l-01"]], Path(pasta) / "sessao.json", tecla=teclas,
                escrever=linhas.append, agora=_relogio(),
            )
            guardada = ac.ler_sessao(Path(pasta) / "sessao.json")
        texto = "\n".join(linhas)
        self.assertIn("Inventou = o texto enviado tinha algum pedido que NÃO disseste.", texto)
        self.assertIn(ac.AJUDA_FEZ, texto)
        self.assertIn(ac.PERGUNTA_INVENTOU, " ".join(teclas.perguntas))
        # As teclas e o registo nao mudam: s = fez, n = nao inventou.
        self.assertEqual([(r.id, r.fez_o_pedido, r.inventou) for r in guardada.tarefas], [("a-01", True, False)])


class TestEcraInicial(unittest.TestCase):
    def test_diz_o_que_o_sponsor_precisa_antes_da_primeira_tarefa(self) -> None:
        for lingua in ac.LINGUAS:
            texto = "\n".join(ac.texto_do_ecra_inicial(lingua, PROJETOS))
            with self.subTest(lingua):
                for trecho in (
                    "só UM jarvis",
                    "Shift da direita",
                    '"hey jarvis"',
                    "diz-te exatamente o que responder",
                    "YES envia a sério para o Claude Code do projeto",
                    "abre-se uma janela do Claude Code com um aviso",
                    "aceita o aviso e deixa a janela aberta",
                    "Nunca feches essa janela",
                    "pedidos que só leem",
                    "exemplo-um",
                    FIM_DO_DITADO[lingua],
                ):
                    self.assertIn(trecho, texto)
                self.assertNotRegex(texto, r"<[^>]*>")

    def test_enter_segue_e_q_sai(self) -> None:
        linhas: list[str] = []
        teclas = _teclas([""])
        self.assertTrue(ac.mostrar_ecra_inicial("en", PROJETOS, tecla=teclas, escrever=linhas.append))
        self.assertIn("Enter", teclas.perguntas[0])
        self.assertIn("janela do Claude Code", "\n".join(linhas))
        self.assertFalse(ac.mostrar_ecra_inicial("en", PROJETOS, tecla=_teclas(["q"]), escrever=lambda _t: None))

    def _main(self, argv, tecla_do_ecra):
        """Corre main() sem jarvis, sem config.toml real e sem escrever evidencia; devolve as chamadas."""
        from types import SimpleNamespace
        from unittest import mock

        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        chamadas: list[str] = []
        guardada = Path(pasta.name) / "sessao-20260925-100000.json"
        ac.guardar_sessao(ac.Sessao(lingua="pt", projetos=dict(PROJETOS)), guardada)

        def ecra(lingua, projetos, **_):
            chamadas.append(f"ecra:{lingua}:{sorted(projetos.values())}")
            return tecla_do_ecra == ""

        def sessao(*_a, **_k):
            chamadas.append("sessao")
            return True

        config = SimpleNamespace(
            projetos=[SimpleNamespace(nome=n) for n in ("exemplo-um", "exemplo-dois", "exemplo-tres")],
            ouvido=SimpleNamespace(lingua="pt"),
        )
        avaliacao = SimpleNamespace(estado=ac.ESTADO_PENDENTE, falhas=[])
        with contextlib.ExitStack() as pilha:
            for nome, valor in (
                ("mostrar_ecra_inicial", ecra),
                ("correr_sessao", sessao),
                ("jarvis_a_correr", lambda _l: True),
                ("_log_de_hoje", lambda: []),
                ("ultima_sessao", lambda: guardada),
                ("carregar_config", lambda _c: config),
                ("relatorio", lambda *_a, **_k: avaliacao),
                ("PASTA_SESSOES", Path(pasta.name)),
            ):
                pilha.enter_context(mock.patch.object(ac, nome, valor))
            pilha.enter_context(contextlib.redirect_stdout(io.StringIO()))
            codigo = ac.main(argv)
        return codigo, chamadas

    def test_main_mostra_o_ecra_antes_da_primeira_tarefa(self) -> None:
        for argv in (["--continuar"], ["--projeto-teste", "exemplo-tres"]):
            with self.subTest(argv):
                codigo, chamadas = self._main(argv, "")
                self.assertEqual(codigo, 0)
                self.assertEqual(len(chamadas), 2)
                self.assertTrue(chamadas[0].startswith("ecra:pt:"))
                self.assertEqual(chamadas[1], "sessao")

    def test_q_no_ecra_inicial_nao_comeca_as_tarefas(self) -> None:
        codigo, chamadas = self._main(["--continuar"], "q")
        self.assertEqual(codigo, 0)
        self.assertEqual([c.split(":")[0] for c in chamadas], ["ecra"])


class TestGuiaoEmTexto(unittest.TestCase):
    def test_antes_de_comecar_diz_o_mesmo_que_o_ecra(self) -> None:
        texto = ac.GUIAO.read_text(encoding="utf-8")
        antes = texto[texto.index("## Antes de começar"):texto.index("## Como se mede")]
        for trecho in (
            "só UM jarvis",
            "Shift da direita",
            '"hey jarvis"',
            "YES",
            "ABORT",
            "janela do Claude Code com um aviso",
            "deixa a janela aberta",
            "pedidos que só leem",
            "O QUE DIZER",
            "NO FIM RESPONDE",
            "O QUE DEVE ACONTECER",
        ):
            self.assertIn(trecho, antes)
        def normal(texto: str) -> str:
            return " ".join(texto.replace("`", "").split()).lower()

        ecra = normal(" ".join(ac.ECRA_INICIAL))
        for trecho in (
            '8 s para continuar sem "hey jarvis"',
            "[escuta] seguimento_s",
            "Um som suave e a bolinha a ouvir",
            "A OUVIR-TE",
            "um som diferente indica quando deixou de ouvir",
            "espera pelo som de fecho",
        ):
            with self.subTest(trecho=trecho):
                self.assertIn(trecho.lower(), normal(antes))
                self.assertIn(trecho.lower(), ecra)
        fluxos = texto[texto.index("Fluxos"):texto.index("| id |")]
        for trecho in ("ABORT", "YES", "Don't change anything.", "Não mudes nada."):
            self.assertIn(trecho, fluxos)


# --- Linha de comandos --------------------------------------------------------------


class TestLinhaDeComandos(unittest.TestCase):
    def test_evidencia_fora_da_pasta_das_evidencias_e_recusada(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as saida:
            codigo = ac.main(["--relatorio", "--evidencia", "docs/notas.md"])
        self.assertEqual(codigo, 2)
        self.assertIn("ERRO", saida.getvalue())

    def test_autoteste_passa(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as saida:
            codigo = ac.main(["--autoteste"])
        self.assertEqual(codigo, 0, saida.getvalue())

    def test_ajuda_diz_que_nunca_toca_som(self) -> None:
        self.assertIn("Nunca toca som", ac.construir_parser().format_help().replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
