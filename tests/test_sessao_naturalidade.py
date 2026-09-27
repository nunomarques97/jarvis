r"""Testes da sessao de naturalidade (scripts/sessao_naturalidade.py).

Sem microfone, sem Ollama, sem Claude Code e sem som. O que protegem:

  * o guiao tem as 20 trocas, todos os casos que a sessao mede e o passo do
    Sponsor (linha de base agora, sessao final no fim);
  * o log que o jarvis a serio escreve (montado com pecas falsas, com o
    cerebro verdadeiro sobre um CLI falso) e lido como o script espera:
    ultima voz, resposta local, resposta do cerebro com e sem pesquisa na
    web, tokens, resposta a pergunta geral, pergunta "Which project?";
  * as metas: naturais, cerebro sem pesquisa (p50 <= 1,5 s), cerebro com
    pesquisa (p50 <= 3,5 s, p95 <= 6 s), latencias locais desde a ultima voz,
    repeticoes, "hey jarvis" a mais, perguntas desnecessarias e a interrupcao
    ("not measured" sem as linhas dela);
  * sem sessao do Sponsor, ou so com ficheiros WAV, o relatorio diz
    "PENDING - Sponsor step"; nunca leva nomes de projetos nem transcricoes;
  * a sessao guiada guarda as janelas e as teclas.

Corre com:

    .venv\Scripts\python -m unittest tests.test_sessao_naturalidade -v
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from jarvis.app import LogDaSessao, agora_iso
from jarvis.cerebro import Cerebro
from jarvis.config import ConfigCerebro
from jarvis.ouvido import GATILHO_JANELA, GATILHO_TECLA, Frase
from tests.test_app import Montagem, resposta_llm
from tests.test_cerebro import CLI_FALSO, HOJE, CliFalso, delta, init, inicio, resultado, uso_da_web

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


sn = _carregar_script("sessao_naturalidade")

INICIO = datetime.datetime(2026, 9, 27, 10, 0, 0)
Troca = sn.Troca
TROCAS = sn.ler_guiao()
POR_ID = {t.id: t for t in TROCAS}


def _avaliar(sessao, linhas):
    return sn.avaliar_sessao(sessao, TROCAS, sn.ler_log(linhas))


def _sessao_com(trocas, tempos_por_id: dict):
    """Como `sn.sessao_falsa`, com tempos proprios em algumas trocas."""
    log = sn.EscritorDeLogFalso(INICIO)
    log.arranque()
    sessao = sn.Sessao(fase=sn.FASE_BASE, projetos=dict(sn.PROJETOS_FICTICIOS))
    for troca in trocas:
        log.avancar(10)
        registo = sn.RegistoDaTroca(troca.id, sn.FEITA, inicio=agora_iso(log.agora), marca=sn.NATURAL)
        log.avancar(3)
        sn.escrever_troca_falsa(log, troca, **tempos_por_id.get(troca.id, {}))
        log.avancar(2)
        registo.fim = agora_iso(log.agora)
        sessao.trocas.append(registo)
    return sessao, log.linhas


class TestGuiao(unittest.TestCase):
    def test_vinte_trocas_com_todos_os_casos(self) -> None:
        self.assertEqual(len(TROCAS), 20)
        self.assertEqual(sn.verificar_cobertura_do_guiao(TROCAS), [])
        tipos = {t.tipo for t in TROCAS}
        self.assertEqual(tipos, set(sn.TIPOS))

    def test_so_o_pedido_sem_projeto_espera_uma_pergunta(self) -> None:
        self.assertEqual([t.tipo for t in TROCAS if t.pergunta_esperada], ["sem-projeto"])

    def test_cozinhar_e_depois_um_seguimento_que_depende_do_contexto(self) -> None:
        ids = [t.id for t in TROCAS]
        cozinhar = next(t for t in TROCAS if "cook" in t.dizer)
        self.assertEqual(cozinhar.tipo, "conversa")
        seguinte = TROCAS[ids.index(cozinhar.id) + 1]
        self.assertEqual(seguinte.tipo, "seguimento")
        self.assertIn('sem "hey jarvis"', seguinte.dizer)
        self.assertIn("The first dish?", seguinte.dizer)

    def test_o_guiao_cobre_a_conversa_pelo_cerebro(self) -> None:
        def uma(tipo: str, *palavras: str) -> Troca:
            achadas = [t for t in TROCAS if t.tipo == tipo and all(p in t.dizer for p in palavras)]
            self.assertTrue(achadas, f"falta uma troca '{tipo}' com {palavras}")
            return achadas[0]

        uma("conversa", "how are you doing")
        uma("pesquisa", "weather")
        estado = uma("estado", "<projeto-1>")
        self.assertNotIn("status", estado.dizer, "o estado e pedido de forma natural")
        ditado = uma("ditado", "<projeto-1>")
        self.assertIn('"yes"', ditado.acontecer)
        avisos = [t for t in TROCAS if t.tipo == "aviso"]
        self.assertTrue(avisos)
        self.assertTrue(any("nunca corta" in t.acontecer for t in avisos))
        self.assertLess([t.id for t in TROCAS].index(ditado.id), [t.id for t in TROCAS].index(avisos[0].id))
        uma("interromper", "that's enough")
        ids = [t.id for t in TROCAS]
        dormir = uma("local", "go to sleep")
        acordar = TROCAS[ids.index(dormir.id) + 1]
        self.assertIn("hey jarvis", acordar.dizer)
        self.assertIn("(a dormir)", acordar.dizer)

    def test_as_teclas_e_as_metas_estao_no_guiao(self) -> None:
        texto = sn.GUIAO.read_text(encoding="utf-8")
        for tecla in sn.MARCAS:
            self.assertIn(f"| `{tecla}` |", texto)
        self.assertIn("| trocas marcadas naturais | pelo menos 16 de 20 |", texto)
        self.assertIn("primeira frase falada | p50 <= 1,5 s |", texto)
        self.assertIn("primeira frase falada | p50 <= 3,5 s, p95 <= 6 s |", texto)

    def test_marcadores_trocados_pelos_nomes(self) -> None:
        projetos = sn.nomes_dos_marcadores(["alfa", "beta", "gama"])
        textos = [sn.trocar_marcadores(t.dizer, projetos) for t in TROCAS]
        self.assertFalse(any("<projeto" in texto for texto in textos))
        self.assertTrue(any("alfa" in texto for texto in textos))
        self.assertTrue(any("beta" in texto for texto in textos))

    def test_um_so_projeto_serve_os_dois_marcadores_e_um_nome_escolhido_tem_de_existir(self) -> None:
        self.assertEqual(set(sn.nomes_dos_marcadores(["alfa"]).values()), {"alfa"})
        self.assertEqual(sn.nomes_dos_marcadores(["alfa", "beta"], (None, "alfa"))["<projeto-2>"], "alfa")
        with self.assertRaises(sn.ErroDaSessao):
            sn.nomes_dos_marcadores(["alfa"], ("zeta", None))
        with self.assertRaises(sn.ErroDaSessao):
            sn.nomes_dos_marcadores([])

    def test_o_passo_do_sponsor_esta_no_guiao(self) -> None:
        texto = sn.GUIAO.read_text(encoding="utf-8")
        self.assertIn("## Passo do Sponsor", texto)
        self.assertIn("--fase linha-de-base", texto)
        self.assertIn("--fase final", texto)
        self.assertIn("PENDING - Sponsor step", texto)

    def test_guiao_invalido_e_recusado(self) -> None:
        cabecalho = "| id | tipo | o que dizer | o que deve acontecer | pergunta esperada |\n|---|---|---|---|---|\n"
        casos = {
            "marcador": "| n-01 | local | tell <projeto-9> hi | nada | não |",
            "tipo": "| n-01 | cantar | what time is it | nada | não |",
            "pergunta": "| n-01 | local | what time is it | nada | talvez |",
            "id": "| x-01 | local | what time is it | nada | não |",
            "vazia": "| n-01 | local |  | nada | não |",
        }
        with tempfile.TemporaryDirectory() as pasta:
            for nome, linha in casos.items():
                with self.subTest(nome=nome):
                    caminho = Path(pasta) / f"{nome}.md"
                    caminho.write_text(cabecalho + linha + "\n", encoding="utf-8")
                    with self.assertRaises(sn.ErroDoGuiao):
                        sn.ler_guiao(caminho)


class TestLerLog(unittest.TestCase):
    def test_latencias_vem_da_ultima_voz_e_nao_do_fim_da_fala(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase("horas", fala_ms=900.0, fecho_ms=600.0)
        frase = sn.ler_log(log.linhas).frases[0]
        self.assertEqual(frase.fala_ms, 900.0)
        self.assertEqual(frase.fonte, sn.FONTE_MICROFONE)
        self.assertLess(frase.ultima_voz, frase.instante)

    def test_resposta_geral_vai_para_a_frase_da_pergunta(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase("horas")
        numero = log.frase("pergunta_geral", fala_ms=700.0)
        log.avancar(3)
        log.resposta_geral(numero, 3200.0)
        frases = {f.numero: f for f in sn.ler_log(log.linhas).frases}
        self.assertIsNone(frases[1].resposta_geral_ms)
        self.assertEqual(frases[2].resposta_geral_ms, 3200.0)
        self.assertTrue(frases[2].geral)

    def test_frases_de_ficheiros_wav_ficam_marcadas(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque(microfone=False)
        log.frase("horas")
        log.arranque(microfone=True)
        log.frase("horas")
        fontes = sorted(f.fonte for f in sn.ler_log(log.linhas).frases)
        self.assertEqual(fontes, [sn.FONTE_FICHEIRO, sn.FONTE_MICROFONE])

    def test_linhas_da_interrupcao(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.interrupcao(240.0)
        log.interrupcao(None)
        interrupcoes = sn.ler_log(log.linhas).interrupcoes
        self.assertEqual([(i.latencia_ms, i.falsa) for i in interrupcoes], [(240.0, False), (None, True)])

    def test_perguntas_de_esclarecimento(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase("ditar_prompt", None, desfecho="pendente", motivo="a espera de confirmacao")  # Which project?
        log.frase("ditar_prompt", "atlas", desfecho="pendente", motivo="a espera de confirmacao")  # recap
        log.frase("desconhecido", desfecho="nao_percebido", motivo="nada")  # I didn't get that
        log.frase(None, resposta_ao_recap=True, desfecho="pendente", motivo="resposta nao percebida")
        log.frase(None, resposta_ao_recap=True, desfecho="pendente", motivo="a espera de confirmacao")  # correcao
        log.frase("horas")
        frases = sorted(sn.ler_log(log.linhas).frases, key=lambda f: f.numero)
        self.assertEqual([f.perguntas for f in frases], [1, 0, 1, 1, 0, 0])

    def _frases(self, log) -> list:
        return sorted(sn.ler_log(log.linhas).frases, key=lambda f: (f.segmento, f.numero))

    def test_turnos_do_cerebro_com_e_sem_pesquisa_e_os_tokens(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase_do_cerebro(resposta_ms=1100.0)
        log.frase_do_cerebro(resposta_ms=2900.0, web=True, aviso_ms=950.0)
        log.frase("horas", fala_ms=600.0)
        frases = self._frases(log)
        self.assertEqual([f.caminho for f in frases], ["brain", "brain + web search", "local"])
        self.assertEqual([f.resposta_cerebro_ms for f in frases], [1100.0, 2900.0, None])
        self.assertEqual(frases[1].fala_ms, 950.0, "o primeiro som foi o aviso curto")
        self.assertEqual(frases[0].tokens, {"input": 12, "cache_read": 4688, "cache_creation": 300, "output": 40})
        self.assertEqual((frases[0].entrada_do_cerebro, frases[1].entrada_do_cerebro), (5000, 9000))
        self.assertEqual([f.geral for f in frases], [False, False, False])
        self.assertEqual([f.perguntas for f in frases], [0, 0, 0])

    def test_o_resumo_fecha_o_turno_mais_antigo_ainda_aberto(self) -> None:
        # Uma frase nova chega antes do resumo do turno que ela substituiu.
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase_do_cerebro(resposta_ms=None, com_resumo=False)
        log.frase_do_cerebro(resposta_ms=1000.0, com_resumo=False)
        log.resumo_do_cerebro(estado="cancelado", web=True)
        log.resumo_do_cerebro()
        frases = self._frases(log)
        self.assertEqual(
            [(f.estado_do_cerebro, f.pesquisa_web) for f in frases], [("cancelado", True), ("respondido", False)]
        )

    def test_resumo_sem_turno_aberto_ou_de_outro_processo_nao_fecha_nada(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase_do_cerebro(com_resumo=False)
        log.arranque()
        log.linha("cerebro | falhou (erro) | intencao=cerebro")
        log.frase_do_cerebro(com_resumo=False)
        log.linha("cerebro | falhou (o Claude Code devolveu um erro (error_during_execution)) | intencao=cerebro")
        antiga, nova = self._frases(log)
        self.assertEqual((antiga.estado_do_cerebro, antiga.pesquisa_web), (None, None))
        self.assertEqual(antiga.caminho, "brain (no summary line)")
        self.assertEqual((nova.estado_do_cerebro, nova.pesquisa_web, nova.tokens), ("falhou", False, {}))

    def test_texto_dito_com_pesquisa_web_nao_engana_o_leitor(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        log.frase_do_cerebro(com_resumo=False)
        log.linha(
            "cerebro | respondido em 1.0 s (ok) | intencao=cerebro | pesquisa web: nao | tokens: input=5 output=9 "
            "| contexto: 5 tokens | primeiro texto: 300 ms: Claude says: 'no | pesquisa web: sim | tokens: input=99999'"
        )
        frase = self._frases(log)[0]
        self.assertEqual((frase.pesquisa_web, frase.tokens), (False, {"input": 5, "output": 9}))


class TestMetas(unittest.TestCase):
    def test_sessao_perfeita_cumpre(self) -> None:
        avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO))
        self.assertEqual(avaliacao.estado, sn.ESTADO_CUMPRIDA)
        self.assertEqual(avaliacao.falhas, [])
        self.assertEqual(avaliacao.naturais, 20)

    def test_limites_de_cada_meta(self) -> None:
        casos = {
            "cerebro p50 no limite": (dict(cerebro_ms=1500.0), []),
            "cerebro acima": (dict(cerebro_ms=1501.0), [sn.NOME_CEREBRO]),
            "pesquisa no limite": (dict(pesquisa_ms=3500.0), []),
            "pesquisa acima": (dict(pesquisa_ms=3600.0), [sn.NOME_PESQUISA]),
            "aviso curto lento nao conta na pesquisa": (dict(som_ms=2500.0), []),
            "local p50 no limite": (dict(local_ms=1000.0), []),
            "local acima": (dict(local_ms=1001.0), [sn.NOME_LOCAL]),
            "interrupcao abaixo de 300": (dict(interrupcao_ms=299.0), []),
            "interrupcao de 300 falha": (dict(interrupcao_ms=300.0), ["interruption, speech onset -> voice stopped"]),
        }
        for nome, (tempos, falhas) in casos.items():
            with self.subTest(nome=nome):
                avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, **tempos))
                self.assertEqual([m.nome for m in avaliacao.medidas if m.cumpre is False], falhas)

    def test_as_metas_do_cerebro_sao_separadas_da_pesquisa(self) -> None:
        avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO))
        medidas = {m.nome: m for m in avaliacao.medidas}
        self.assertEqual(medidas[sn.NOME_CEREBRO].meta, "p50 <= 1500 ms")
        self.assertEqual(medidas[sn.NOME_PESQUISA].meta, "p50 <= 3500 ms, p95 <= 6000 ms")
        com_cerebro = sum(1 for t in TROCAS if t.tipo not in ("local", "pesquisa")) + 1  # o sem-projeto tem duas
        self.assertEqual(len(avaliacao.cerebro_ms), com_cerebro)
        self.assertEqual(len(avaliacao.pesquisa_ms), sum(1 for t in TROCAS if t.tipo == "pesquisa"))
        self.assertNotIn("general answer, last voice -> first answer sentence", medidas)

    def test_p95_da_pesquisa(self) -> None:
        pesquisas = [t.id for t in TROCAS if t.tipo == "pesquisa"]
        sessao, linhas = _sessao_com(TROCAS, {pesquisas[-1]: dict(pesquisa_ms=6500.0)})
        avaliacao = _avaliar(sessao, linhas)
        self.assertEqual(sn._p(avaliacao.pesquisa_ms, 50), 3000.0)
        self.assertEqual([m.nome for m in avaliacao.medidas if m.cumpre is False], [sn.NOME_PESQUISA])

    def test_sem_pesquisas_a_meta_fica_not_measured_mas_sem_cerebro_falha(self) -> None:
        sessao, linhas = sn.sessao_falsa(TROCAS, INICIO)
        sem_web = [linha.replace("pesquisa web: sim (1 uso(s), 1 pesquisa(s))", "pesquisa web: nao") for linha in linhas]
        avaliacao = _avaliar(sessao, sem_web)
        self.assertEqual(avaliacao.pesquisa_ms, [])
        self.assertIn(sn.NOME_PESQUISA, avaliacao.nao_medidas)
        self.assertEqual(avaliacao.estado, sn.ESTADO_CUMPRIDA)
        self.assertTrue(any("web search target is not measured" in nota for nota in avaliacao.notas))
        # Uma sessao so com o interprete local nao cumpre a meta do cerebro.
        sem_cerebro = [linha for linha in linhas if "intencao=cerebro" not in linha]
        avaliacao = _avaliar(sessao, sem_cerebro)
        medidas = {m.nome: m for m in avaliacao.medidas}
        self.assertEqual((medidas[sn.NOME_CEREBRO].obtido, medidas[sn.NOME_CEREBRO].cumpre), ("no samples", False))
        self.assertTrue(any("no sentence went to the brain" in nota for nota in avaliacao.notas))

    def test_perguntas_gerais_do_interprete_mantem_as_metas_delas(self) -> None:
        log = sn.EscritorDeLogFalso(INICIO)
        log.arranque()
        sessao = sn.Sessao(fase=sn.FASE_BASE, projetos=dict(sn.PROJETOS_FICTICIOS))
        for troca in TROCAS:
            log.avancar(10)
            registo = sn.RegistoDaTroca(troca.id, sn.FEITA, inicio=agora_iso(log.agora), marca=sn.NATURAL)
            log.avancar(3)
            if troca.tipo == "conversa":
                numero = log.frase("pergunta_geral", fala_ms=1300.0)
                log.avancar(3)
                log.resposta_geral(numero, 3200.0)
            else:
                sn.escrever_troca_falsa(log, troca)
            log.avancar(2)
            registo.fim = agora_iso(log.agora)
            sessao.trocas.append(registo)
        avaliacao = _avaliar(sessao, log.linhas)
        falhas = {m.nome for m in avaliacao.medidas if m.cumpre is False}
        self.assertEqual(falhas, {"general answer, last voice -> first sound of any kind"})
        self.assertEqual(set(avaliacao.geral_ms), {3200.0})
        self.assertNotIn(1300.0, avaliacao.local_ms)

    def test_interrupcao_falsa_falha(self) -> None:
        avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, interrupcao_ms=None))
        self.assertEqual(avaliacao.interrupcoes_falsas, 1)
        self.assertTrue(any("false interruptions" in f for f in avaliacao.falhas))

    def test_sem_linhas_da_interrupcao_fica_not_measured(self) -> None:
        sessao, linhas = sn.sessao_falsa(TROCAS, INICIO)
        avaliacao = _avaliar(sessao, [linha for linha in linhas if " interrupcao | " not in linha])
        self.assertEqual(len(avaliacao.nao_medidas), 2)
        texto = sn.texto_do_relatorio(avaliacao)
        self.assertIn("| interruption, speech onset -> voice stopped | not measured |", texto)

    def test_teclas_do_sponsor(self) -> None:
        marcas = [sn.NATURAL] * 15 + [sn.POUCO_NATURAL, sn.REPETIU, sn.ATIVACAO_A_MAIS, sn.NATURAL, sn.POUCO_NATURAL]
        avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, marcas=marcas))
        self.assertEqual((avaliacao.naturais, avaliacao.repeticoes, avaliacao.ativacoes_a_mais), (16, 1, 1))
        self.assertEqual(len(avaliacao.falhas), 1, avaliacao.falhas)
        self.assertIn("wake words", avaliacao.falhas[0])

    def test_quinze_naturais_nao_chega(self) -> None:
        marcas = [sn.NATURAL] * 15 + [sn.POUCO_NATURAL] * 5
        avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, marcas=marcas))
        self.assertEqual(avaliacao.estado, sn.ESTADO_NAO_CUMPRIDA)

    def test_poucas_trocas_feitas_e_incompleta(self) -> None:
        sessao, linhas = sn.sessao_falsa(TROCAS, INICIO)
        sessao.trocas = sessao.trocas[:15]
        avaliacao = _avaliar(sessao, linhas)
        self.assertEqual(avaliacao.estado, sn.ESTADO_INCOMPLETA)
        self.assertEqual(len(avaliacao.saltadas), 5)

    def test_sem_sessao_ou_sem_voz_real_fica_pendente(self) -> None:
        self.assertEqual(sn.avaliar_sessao(None, TROCAS, sn.LeituraDoLog()).estado, sn.ESTADO_PENDENTE)
        autoteste = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, origem=sn.ORIGEM_AUTOTESTE))
        self.assertEqual(autoteste.estado, sn.ESTADO_PENDENTE)
        wav = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, microfone=False))
        self.assertEqual(wav.estado, sn.ESTADO_PENDENTE)
        self.assertEqual(wav.medidas, [])

    def test_sem_a_linha_da_ultima_voz_nao_mede_latencias(self) -> None:
        avaliacao = _avaliar(*sn.sessao_falsa(TROCAS, INICIO, com_ultima_voz=False))
        self.assertEqual((avaliacao.local_ms, avaliacao.cerebro_ms, avaliacao.pesquisa_ms), ([], [], []))
        self.assertTrue(any("ultima voz" in nota for nota in avaliacao.notas))
        self.assertEqual(avaliacao.estado, sn.ESTADO_NAO_CUMPRIDA)

    def test_frase_dita_depois_da_troca_seguinte_comecar_nao_conta_na_anterior(self) -> None:
        sessao, linhas = sn.sessao_falsa(TROCAS, INICIO)
        janelas = sn.janelas(sessao.trocas)
        for janela, seguinte in zip(janelas, janelas[1:]):
            self.assertLessEqual(janela.fecha, seguinte.abre)


class TestRelatorio(unittest.TestCase):
    def test_pendente_diz_o_passo_e_nao_declara_metas(self) -> None:
        texto = sn.texto_do_relatorio(sn.avaliar_sessao(None, TROCAS, sn.LeituraDoLog()))
        self.assertIn("**State: PENDING - Sponsor step**", texto)
        for passo in sn.PASSO_DO_SPONSOR:
            self.assertIn(passo, texto)
        self.assertNotIn("## Targets", texto)
        self.assertIn("Synthetic voices and WAV files never count", texto)

    def test_relatorio_sem_nomes_nem_transcricoes(self) -> None:
        texto = sn.texto_do_relatorio(_avaliar(*sn.sessao_falsa(TROCAS, INICIO)))
        for nome in sn.PROJETOS_FICTICIOS.values():
            self.assertNotIn(nome, texto)
        self.assertNotIn("ficticia", texto)
        self.assertNotIn("ficticio", texto)
        for troca in TROCAS:
            self.assertIn(f"| {troca.id} |", texto)

    def test_relatorio_so_dentro_da_pasta_de_evidencia(self) -> None:
        self.assertEqual(sn.caminho_do_relatorio(None, None), sn.EVIDENCIA_PENDENTE.resolve())
        self.assertTrue(sn.EVIDENCIA_PENDENTE.resolve().is_relative_to(sn.PASTA_EVIDENCIA.resolve()))
        for fora in ("notas.md", "docs/notas.md", "docs/forja/evidence/x.txt"):
            with self.subTest(fora=fora), self.assertRaises(ValueError):
                sn.caminho_do_relatorio(fora, None)

    def test_final_compara_com_a_ultima_linha_de_base(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            pasta = Path(pasta)
            sessoes, logs = pasta / "sessoes", pasta / "logs"
            logs.mkdir()
            base, linhas_base = sn.sessao_falsa(TROCAS, INICIO, local_ms=1500.0)
            final, linhas_final = sn.sessao_falsa(
                TROCAS, INICIO + datetime.timedelta(hours=2), fase=sn.FASE_FINAL, local_ms=700.0
            )
            (logs / f"jarvis-{INICIO:%Y-%m-%d}.log").write_text("\n".join(linhas_base + linhas_final), encoding="utf-8")
            sn.guardar_sessao(base, sessoes / "sessao-20260927-100000.json")
            sn.guardar_sessao(final, sessoes / "sessao-20260927-120000.json")
            destino = pasta / "relatorio.md"
            original = sn.caminho_evidencia_de_saida
            sn.caminho_evidencia_de_saida = lambda valor: Path(valor)
            try:
                avaliacao, escrito = sn.relatorio(
                    sessoes / "sessao-20260927-120000.json", TROCAS, str(destino), pasta_sessoes=sessoes, pasta_logs=logs
                )
            finally:
                sn.caminho_evidencia_de_saida = original
            texto = escrito.read_text(encoding="utf-8")
        self.assertEqual(avaliacao.estado, sn.ESTADO_CUMPRIDA)
        self.assertIn("| measure | baseline | now | target | met |", texto)
        self.assertIn("p50 1500 ms", texto)
        self.assertIn("p50 700 ms", texto)


class _RelogioDeParede:
    def __init__(self, inicio: datetime.datetime) -> None:
        self.agora = inicio

    def __call__(self) -> datetime.datetime:
        return self.agora


class TestContratoComOLogDoJarvis(unittest.TestCase):
    """O jarvis sem cerebro (interprete local, pecas falsas) escreve o log; o script le-o e mede desde a ultima voz."""

    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.parede = _RelogioDeParede(INICIO)
        self.m = Montagem(
            [
                resposta_llm("pergunta_geral", "", "What is the weather in Lisbon today?"),
                resposta_llm("ditar_prompt", "", "Add a short section about tests to the readme."),
            ],
            lingua="en",
        )
        self.addCleanup(self.m.jarvis.fechar)
        self.log = LogDaSessao(pasta=self.pasta, consola=StringIO(), quando=INICIO, relogio_de_parede=self.parede)
        self.addCleanup(self.log.fechar)
        self.m.jarvis.log = self.log
        self.log.linha("jarvis a arrancar | log em logs/jarvis-teste.log")
        self.log.bruto("   JARVIS PRONTO em 4.2 s (meta <= 30 s)")
        self.log.bruto("   ouvido: Microfone Ficticio de Teste (MME)")
        self.sessao = sn.Sessao(fase=sn.FASE_BASE, projetos={"<projeto-1>": "atlas", "<projeto-2>": "orbita"})

    def _passar(self, segundos: float) -> None:
        self.parede.agora += datetime.timedelta(seconds=segundos)
        self.m.avancar(segundos)

    def _ouvir(self, texto: str, silencio_final_s: float = 0.6) -> None:
        fim = self.m.relogio()
        self.m.jarvis.ao_ouvir(
            Frase(
                texto=texto,
                gatilho=GATILHO_TECLA,
                lingua="en",
                motor="motor-falso",
                duracao_audio_s=1.5,
                inicio_da_escuta=fim - 1.5,
                fim_da_escuta=fim,
                texto_pronto=fim,
                latencia_stt_ms=150.0,
                ultima_voz=fim - silencio_final_s,
            )
        )

    def _troca(self, id_: str, textos: list[str], marca: str = sn.NATURAL) -> None:
        registo = sn.RegistoDaTroca(id_, sn.FEITA, inicio=agora_iso(self.parede()), marca=marca)
        for texto in textos:
            self._passar(2)
            self._ouvir(texto)
            self.assertTrue(self.m.jarvis.esperar_pergunta(5.0))
        self._passar(2)
        registo.fim = agora_iso(self.parede())
        self.sessao.trocas.append(registo)
        self._passar(5)

    def test_hora_pergunta_geral_e_which_project(self) -> None:
        self._troca("n-02", ["what time is it"])
        self._troca("n-06", ["what's the weather like in Lisbon today"])
        self._troca("n-13", ["tell it to add a short section about tests to the readme"])
        self._troca("n-08", ["tell it to add a short section about tests to the readme"], marca=sn.POUCO_NATURAL)
        self.log.fechar()
        texto = self.log.caminho.read_text(encoding="utf-8")
        # O jarvis escreve a ultima voz e as duas medidas desde ela.
        self.assertIn("frase #1 | ultima voz: 2026-09-27 10:00:01.400 | 600 ms antes do fim da escuta", texto)
        self.assertIn("frase #1 | inicio da resposta falada: 100 ms desde o fim da fala", texto)
        self.assertIn("frase #1 | resposta falada desde a ultima voz: 700 ms", texto)
        # Sem o cerebro, a pergunta geral nao sai do PC: a resposta e a frase local.
        self.assertIn("pergunta geral | sem o cerebro: nada saiu do PC", texto)
        self.assertIn("frase #2 | resposta falada desde a ultima voz: 700 ms", texto)

        leitura = sn.ler_logs([self.log.caminho])
        atribuidas = sn.atribuir(self.sessao.trocas, leitura)
        horas = atribuidas["n-02"][0]
        self.assertEqual([(f.intencao, f.fala_ms, f.geral) for f in horas], [("horas", 700.0, False)])
        pergunta = atribuidas["n-06"][0]
        self.assertEqual(
            [(f.intencao, f.fala_ms, f.resposta_geral_ms) for f in pergunta], [("pergunta_geral", 700.0, None)]
        )
        # O ditado sem projeto: o jarvis pergunta o projeto, e ali a pergunta e esperada.
        sem_projeto = sn.ResultadoDaTroca(POR_ID["n-13"], self.sessao.registo("n-13"), atribuidas["n-13"][0])
        self.assertEqual([f.perguntas for f in sem_projeto.frases], [1])
        self.assertEqual(sem_projeto.perguntas_a_mais, 0)
        # A frase seguinte cai no recap ainda pendente e o jarvis volta a perguntar: a mais.
        a_mais = sn.ResultadoDaTroca(POR_ID["n-08"], self.sessao.registo("n-08"), atribuidas["n-08"][0])
        self.assertEqual([(f.resposta_ao_recap, f.motivo) for f in a_mais.frases], [(True, "resposta nao percebida")])
        self.assertEqual(a_mais.perguntas_a_mais, 1)

        avaliacao = sn.avaliar_sessao(self.sessao, TROCAS, leitura)
        self.assertEqual(avaliacao.estado, sn.ESTADO_INCOMPLETA)
        self.assertEqual(avaliacao.geral_ms, [], "nenhuma resposta do Claude pelo interprete local")
        self.assertEqual(avaliacao.primeiro_som_ms, [700.0])
        self.assertEqual(avaliacao.perguntas_a_mais, 1)
        self.assertNotIn("atlas", sn.texto_do_relatorio(avaliacao))


class TestContratoComOCerebro(unittest.TestCase):
    """O jarvis com o cerebro verdadeiro (CLI falso) escreve o log; o script classifica e mede."""

    def setUp(self) -> None:
        # Guarda: o subprocess.Popen verdadeiro nunca pode ser chamado.
        self.popen_real: list = []

        def guarda(*args, **kwargs):
            self.popen_real.append(args)
            raise AssertionError("subprocess.Popen real chamado num teste")

        remendo = mock.patch.object(subprocess, "Popen", new=guarda)
        remendo.start()
        self.addCleanup(remendo.stop)
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.parede = _RelogioDeParede(INICIO)
        self.cli = CliFalso(
            self._demora(0.5, delta("I'm doing well, thanks."), resultado("I'm doing well, thanks.")),
            self._demora(
                2.4,
                uso_da_web("w1"),
                delta("It is sunny in Lisbon today."),
                resultado("It is sunny in Lisbon today."),
            ),
            self._demora(0.3, delta("Tomorrow looks sunny too."), resultado("Tomorrow looks sunny too.")),
        )
        self.cerebro = Cerebro(
            ConfigCerebro(limite_s=5.0),
            "en",
            localizacao="Lisbon, Portugal",
            nomes_de_projeto=["atlas", "orbita"],
            cli=CLI_FALSO,
            arrancar=self.cli,
            pasta=self.pasta / "neutra",
            hoje=lambda: HOJE,
        )
        self.m = Montagem(lingua="en", cerebro=self.cerebro)
        self.addCleanup(self.m.jarvis.fechar)
        self.log = LogDaSessao(pasta=self.pasta, consola=StringIO(), quando=INICIO, relogio_de_parede=self.parede)
        self.addCleanup(self.log.fechar)
        self.m.jarvis.log = self.log
        self.log.linha("jarvis a arrancar | log em logs/jarvis-teste.log")
        self.log.bruto("   JARVIS PRONTO em 4.2 s (meta <= 30 s)")
        self.log.bruto("   ouvido: Microfone Ficticio de Teste (MME)")
        self.sessao = sn.Sessao(fase=sn.FASE_FINAL, projetos={"<projeto-1>": "atlas", "<projeto-2>": "orbita"})

    def tearDown(self) -> None:
        self.assertEqual(self.popen_real, [], "o subprocess.Popen real foi chamado")

    def _demora(self, segundos: float, *eventos):
        """Um guiao do CLI falso cuja resposta leva `segundos` no relogio do jarvis."""

        def guiao(processo, _texto: str) -> None:
            self.m.avancar(segundos)
            processo.emitir(init(), inicio(), *eventos)

        return guiao

    def _troca(self, id_: str, texto: str, gatilho: str = GATILHO_TECLA) -> None:
        registo = sn.RegistoDaTroca(id_, sn.FEITA, inicio=agora_iso(self.parede()), marca=sn.NATURAL)
        self.parede.agora += datetime.timedelta(seconds=2)
        self.m.avancar(2)
        self.m.ouvir(texto, gatilho=gatilho, ultima_voz=self.m.relogio() - 0.6)
        self.assertTrue(self.m.jarvis.esperar_pergunta(5.0), "a thread do cerebro nao acabou")
        self.parede.agora += datetime.timedelta(seconds=2)
        registo.fim = agora_iso(self.parede())
        self.sessao.trocas.append(registo)
        self.parede.agora += datetime.timedelta(seconds=5)
        self.m.avancar(1)

    def test_caminho_rapido_cerebro_e_pesquisa_medidos_desde_a_ultima_voz(self) -> None:
        self._troca("n-01", "hey jarvis, how are you doing?")
        self._troca("n-02", "what time is it")
        self._troca("n-06", "what's the weather like in Lisbon today?")
        self._troca("n-07", "and tomorrow?", gatilho=GATILHO_JANELA)
        self.log.fechar()
        texto = self.log.caminho.read_text(encoding="utf-8")
        self.assertIn("cerebro | resposta falada: 1200 ms desde a ultima voz da frase #1", texto)
        self.assertIn("pesquisa web: sim (1 uso(s), 1 pesquisa(s))", texto)
        self.assertEqual(len(self.cli.processos), 1, "uma so sessao do cerebro")

        leitura = sn.ler_logs([self.log.caminho])
        atribuidas = sn.atribuir(self.sessao.trocas, leitura)
        resumo = {
            id_: [(f.caminho, f.fala_ms, f.resposta_cerebro_ms) for f in frases]
            for id_, (frases, _interrupcoes) in atribuidas.items()
        }
        self.assertEqual(
            resumo,
            {
                "n-01": [("brain", 1200.0, 1200.0)],
                "n-02": [("local", 700.0, None)],
                "n-06": [("brain + web search", 3100.0, 3100.0)],
                "n-07": [("brain", 1000.0, 1000.0)],
            },
        )
        entradas = [f.entrada_do_cerebro for frases, _ in atribuidas.values() for f in frases if f.do_cerebro]
        self.assertEqual(entradas, [5112] * 3, "input + cache lida + cache escrita da linha final")

        avaliacao = sn.avaliar_sessao(self.sessao, TROCAS, leitura)
        self.assertEqual(avaliacao.estado, sn.ESTADO_INCOMPLETA)
        self.assertEqual(avaliacao.cerebro_ms, [1200.0, 1000.0])
        self.assertEqual(avaliacao.pesquisa_ms, [3100.0])
        self.assertEqual(avaliacao.local_ms, [700.0])
        medidas = {m.nome: m for m in avaliacao.medidas}
        self.assertTrue(medidas[sn.NOME_CEREBRO].cumpre)
        self.assertTrue(medidas[sn.NOME_PESQUISA].cumpre)
        self.assertTrue(medidas[sn.NOME_LOCAL].cumpre)
        self.assertNotIn("general answer, last voice -> first answer sentence", medidas)
        relatorio = sn.texto_do_relatorio(avaliacao)
        self.assertIn("| n-06 | pesquisa | natural | 1 | 0 | brain + web search | - | 3100 ms | - |", relatorio)
        self.assertNotIn("atlas", relatorio)
        self.assertNotIn("Lisbon", relatorio)

    def test_uma_resposta_lenta_do_cerebro_falha_a_meta(self) -> None:
        self.cli.guioes = [self._demora(1.5, delta("Fine, thanks."), resultado("Fine, thanks."))]
        self._troca("n-01", "hey jarvis, how are you doing?")
        self.log.fechar()
        avaliacao = sn.avaliar_sessao(self.sessao, TROCAS, sn.ler_logs([self.log.caminho]))
        self.assertEqual(avaliacao.cerebro_ms, [2200.0])
        medida = next(m for m in avaliacao.medidas if m.nome == sn.NOME_CEREBRO)
        self.assertFalse(medida.cumpre)


class TestSessaoGuiada(unittest.TestCase):
    def _correr(self, teclas: list[str], **kw):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "sessao-teste.json"
            sessao = sn.Sessao(fase=sn.FASE_BASE, projetos=dict(sn.PROJETOS_FICTICIOS))
            relogio = iter(INICIO + datetime.timedelta(seconds=s) for s in range(0, 10000, 5))
            premidas = iter(teclas)
            ecra: list[str] = []
            acabou = sn.correr_sessao(
                sessao, TROCAS, caminho, tecla=lambda _p, _v: next(premidas), escrever=ecra.append,
                agora=lambda: next(relogio), **kw,
            )
            return acabou, sn.ler_sessao(caminho), ecra

    def test_todas_as_trocas_com_uma_tecla_cada(self) -> None:
        teclas = []
        for indice in range(20):
            teclas += [sn.ENTER, sn.ENTER, "1234"[indice % 4]]
        acabou, sessao, ecra = self._correr(teclas)
        self.assertTrue(acabou)
        self.assertTrue(sessao.concluida)
        self.assertEqual([r.marca for r in sessao.trocas], list("1234" * 5))
        self.assertTrue(all(r.inicio < r.fim for r in sessao.trocas))
        self.assertTrue(any("exemplo-um" in linha for linha in ecra), "o ecra mostra o nome do projeto")

    def test_sem_frases_no_log_avisa_e_deixa_repetir(self) -> None:
        contagens = iter([0, 1])
        acabou, sessao, ecra = self._correr(
            [sn.ENTER, sn.ENTER, "r", sn.ENTER, sn.ENTER, sn.NATURAL, "q"],
            frases_na_janela=lambda _i, _f: next(contagens),
        )
        self.assertFalse(acabou)
        self.assertEqual([(r.id, r.marca) for r in sessao.trocas], [(TROCAS[0].id, sn.NATURAL)])
        self.assertTrue(any("AVISO" in linha for linha in ecra))


class TestAutoteste(unittest.TestCase):
    def test_autoteste_passa_sem_hardware(self) -> None:
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = sn.main(["--autoteste"])
        self.assertEqual(codigo, 0, saida.getvalue())
        self.assertIn("OK: autoteste da sessao de naturalidade completo", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
