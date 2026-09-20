r"""Testes do orquestrador (jarvis/app.py), unittest da biblioteca padrao.

Mesma convencao de tests/test_router.py e tests/test_acoes.py: sem pytest (nao
ha decisao do Technology Scout para uma framework de testes fora da biblioteca
padrao).

NENHUM destes testes toca em hardware: sem GPU, sem microfone, sem Piper, sem
Claude Code. O que eles protegem:

  * o FORMATO do log de cada frase — as cinco etapas, os timestamps, as
    latencias em ms e a linha do total — porque o log e o entregavel da D2/D11
    e uma mudanca silenciosa no formato invalida a prova;
  * a ARITMETICA das latencias, com um relogio falso (nunca `time.sleep`);
  * a linha `FALSO DESPERTAR DESCARTADO` e, sobretudo, que uma frase descartada
    NAO executa accao nenhuma e NAO abre o canal do Claude Code — que e a
    prioridade nao funcional 2 do PRODUCT-PROFILE e o criterio (2) da T6;
  * um comando local nunca abrir o canal do Claude Code (zero tokens, D4/D5);
  * o estado do processo (calar, adormecer, acordar), que so existe aqui
    porque `jarvis.acoes_locais.executar()` as recusa de proposito;
  * a injeccao de ficheiro: o corte em chunks do tamanho que o RealtimeSTT
    consome, o preenchimento do ultimo chunk, a reamostragem e a juncao de
    canais (D33/S5);
  * o resumo falado de uma resposta do Claude Code (D48.4): nomeia a origem e
    nunca a le inteira como facto.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime
import logging
import os
import subprocess
import sys
import tempfile
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from jarvis.acoes_locais import AcaoError, ResultadoAcao
from jarvis.app import (
    BYTES_POR_CHUNK,
    PREFIXO_DA_RESPOSTA_DO_CLAUDE,
    EstadoDoProcesso,
    Jarvis,
    LogDaSessao,
    RegistoDaFrase,
    agora_iso,
    caminho_do_log,
    carregar_config_tolerante,
    chunks_de_silencio,
    construir_recorder,
    detalhe_da_transcricao,
    formatar_etapa,
    frames_do_wav,
    instalar_silenciador_no_processo_filho,
    resumo_falado,
    silenciar_ruido_do_shutdown,
)
from jarvis.audio_util import escrever_wav_pcm16
from jarvis.lingua import LINGUA_FIXA_DO_PRODUTO
from jarvis.config import Config, Projeto
from jarvis.voz import ResultadoFala


class RelogioFalso:
    """Relogio monotonico controlado pelo teste: nada de time.sleep."""

    def __init__(self, tempos: list[float]) -> None:
        self.tempos = list(tempos)
        self.ultimo = self.tempos[0] if self.tempos else 0.0

    def __call__(self) -> float:
        if self.tempos:
            self.ultimo = self.tempos.pop(0)
        return self.ultimo


class LogFalso:
    """Guarda as linhas em vez de as escrever, com a mesma interface."""

    def __init__(self) -> None:
        self.linhas: list[str] = []

    def linha(self, texto: str) -> str:
        self.linhas.append(texto)
        return texto

    def bruto(self, texto: str = "") -> None:
        self.linhas.append(texto)

    def texto(self) -> str:
        return "\n".join(self.linhas)


def _config_sem_projetos() -> Config:
    return Config(microfone="Microfone Ficticio de Teste", projetos=())


def _config_com_projeto(pasta: Path) -> Config:
    return Config(
        microfone="Microfone Ficticio de Teste",
        projetos=(Projeto(nome="exemplo-um", caminho=pasta.resolve()),),
    )


class CanalFalso:
    """Uma sessao do Claude Code de mentira: conta as chamadas."""

    def __init__(self, resposta: str = "resposta de teste") -> None:
        self.resposta = resposta
        self.perguntas: list[str] = []
        self.fechado = False

    def perguntar(self, frase: str, limite_s: float = 0.0) -> str:
        self.perguntas.append(frase)
        return self.resposta

    def fechar(self) -> None:
        self.fechado = True


class JarvisDeTeste:
    """Monta um Jarvis com todas as pecas externas substituidas por falsos."""

    def __init__(self, config: Config | None = None, resposta_do_claude: str = "ok") -> None:
        self.log = LogFalso()
        self.canal = CanalFalso(resposta_do_claude)
        self.canais_abertos = 0
        self.falados: list[str] = []
        self.accoes: list[str] = []

        def abrir_canal():
            self.canais_abertos += 1
            return self.canal

        def falar(texto: str, **_kwargs) -> ResultadoFala:
            self.falados.append(texto)
            return ResultadoFala(falou=True)

        def executar(resultado, config, **_kwargs) -> ResultadoAcao:
            self.accoes.append(resultado.nome_acao or "")
            return ResultadoAcao(
                nome_acao=resultado.nome_acao or "",
                executou=True,
                texto="São 15 horas e 30 minutos.",
            )

        self.jarvis = Jarvis(
            config or _config_sem_projetos(),
            self.log,  # type: ignore[arg-type]
            falar=falar,
            executar=executar,
            abrir_canal=abrir_canal,
        )

    def frase(self, texto: str) -> RegistoDaFrase:
        self.jarvis.frases += 1
        registo = RegistoDaFrase(numero=self.jarvis.frases, log=self.log)  # type: ignore[arg-type]
        registo.marcar(1, "palavra de ativacao de teste")
        registo.marcar(2, f"texto: {texto!r}")
        self.jarvis.tratar_transcricao(texto, registo)
        return registo


class TestFormatoDoLog(unittest.TestCase):
    """O formato e o entregavel (D2/D11): se muda, a prova deixa de casar."""

    def test_linha_de_etapa_tem_frase_etapa_latencia_e_detalhe(self) -> None:
        self.assertEqual(
            formatar_etapa(3, 2, 812.4, "texto: 'que horas sao'"),
            "frase #3 | etapa 2/5 transcricao           |     812 ms "
            "| texto: 'que horas sao'",
        )

    def test_as_cinco_etapas_tem_nome_proprio(self) -> None:
        nomes = [formatar_etapa(1, n, 0, "x").split("|")[1].strip() for n in range(1, 6)]
        self.assertEqual(
            nomes,
            [
                "etapa 1/5 palavra de ativacao",
                "etapa 2/5 transcricao",
                "etapa 3/5 encaminhamento",
                "etapa 4/5 accao/entrega",
                "etapa 5/5 resposta falada",
            ],
        )

    def test_timestamp_tem_milissegundos(self) -> None:
        self.assertEqual(
            agora_iso(datetime.datetime(2026, 9, 20, 6, 12, 1, 123456)),
            "2026-09-20 06:12:01.123",
        )

    def test_nome_do_ficheiro_de_log_e_por_dia(self) -> None:
        caminho = caminho_do_log(datetime.datetime(2026, 9, 20, 23, 59), Path("logs"))
        self.assertEqual(caminho.name, "jarvis-2026-09-20.log")

    def test_o_log_escreve_na_consola_e_no_ficheiro(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            consola = StringIO()
            log = LogDaSessao(
                pasta=Path(pasta),
                consola=consola,
                quando=datetime.datetime(2026, 9, 20),
                relogio_de_parede=lambda: datetime.datetime(2026, 9, 20, 6, 0, 0, 500000),
            )
            log.linha("frase #1 | etapa 3/5")
            log.fechar()
            esperado = "2026-09-20 06:00:00.500 frase #1 | etapa 3/5"
            self.assertEqual(consola.getvalue().strip(), esperado)
            self.assertEqual(log.caminho.read_text(encoding="utf-8").strip(), esperado)


class TestLatencias(unittest.TestCase):
    """A aritmetica das latencias, com relogio falso (sem sleep nenhum)."""

    def test_cada_etapa_mede_desde_a_anterior(self) -> None:
        relogio = RelogioFalso([0.0, 1.0, 1.5, 1.5])
        registo = RegistoDaFrase(numero=1, relogio=relogio)
        self.assertEqual(round(registo.marcar(1, "wake").latencia_ms), 1000)
        self.assertEqual(round(registo.marcar(2, "stt").latencia_ms), 500)
        self.assertEqual(round(registo.marcar(3, "router").latencia_ms), 0)

    def test_a_etapa_2_pode_medir_desde_o_fim_da_fala(self) -> None:
        relogio = RelogioFalso([0.0, 2.0, 2.9])
        registo = RegistoDaFrase(numero=1, relogio=relogio)
        registo.marcar_fim_da_fala()  # t=2.0
        marca = registo.marcar(2, "stt", desde=registo.fim_da_fala)  # t=2.9
        self.assertEqual(round(marca.latencia_ms), 900)

    def test_o_total_conta_das_duas_referencias(self) -> None:
        log = LogFalso()
        relogio = RelogioFalso([0.0, 1.0, 4.0])
        registo = RegistoDaFrase(numero=7, log=log, relogio=relogio)  # type: ignore[arg-type]
        registo.marcar_fim_da_fala()  # t=1.0
        linha = registo.fechar("accao local 'horas_e_data' (zero tokens)")  # t=4.0
        self.assertIn("frase #7 | TOTAL |", linha)
        self.assertIn("4000 ms desde o inicio da escuta", linha)
        self.assertIn("3000 ms desde o fim da fala", linha)

    def test_sem_fala_o_total_diz_que_nao_houve_fala(self) -> None:
        registo = RegistoDaFrase(numero=1, relogio=RelogioFalso([0.0, 3.0]))
        self.assertIn("sem fala detetada", registo.fechar("descartada"))


class TestFalsoDespertar(unittest.TestCase):
    """Criterio (2) da T6 e prioridade nao funcional 2 do PRODUCT-PROFILE."""

    def test_transcricao_vazia_regista_a_linha_e_nao_faz_nada(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("")
        registo = teste.log.texto()
        self.assertIn("FALSO DESPERTAR DESCARTADO", registo)
        self.assertIn("transcricao vazia (D5)", registo)
        self.assertIn("nenhuma accao executada, nada enviado ao Claude Code", registo)
        self.assertEqual(teste.accoes, [])
        self.assertEqual(teste.canais_abertos, 0)
        self.assertEqual(teste.canal.perguntas, [])
        self.assertEqual(teste.falados, [])

    def test_alucinacao_conhecida_sobre_ruido_nao_chega_ao_claude(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Obrigado por assistir!")
        self.assertIn("FALSO DESPERTAR DESCARTADO", teste.log.texto())
        self.assertEqual(teste.canais_abertos, 0)
        self.assertEqual(teste.accoes, [])

    def test_transcricao_curta_de_mais_nao_chega_ao_claude(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("ah")
        self.assertIn("FALSO DESPERTAR DESCARTADO", teste.log.texto())
        self.assertEqual(teste.canais_abertos, 0)

    def test_a_frase_descartada_fecha_com_zero_accoes_e_zero_tokens(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("")
        self.assertIn("descartada: zero accoes, zero tokens", teste.log.texto())


class TestComandoLocal(unittest.TestCase):
    def test_horas_executa_accao_local_e_nunca_abre_o_canal(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Que horas são?")
        registo = teste.log.texto()
        self.assertIn("decisao=local -> accao local 'horas_e_data'", registo)
        self.assertIn("zero tokens", registo)
        self.assertEqual(teste.accoes, ["horas_e_data"])
        self.assertEqual(teste.canais_abertos, 0, "um comando local nunca gasta tokens (D4)")
        self.assertEqual(teste.falados, ["São 15 horas e 30 minutos."])
        self.assertNotIn("FALSO DESPERTAR", registo)

    def test_as_cinco_etapas_ficam_no_log_de_um_comando_local(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Que horas são?")
        registo = teste.log.texto()
        for etapa in ("etapa 1/5", "etapa 2/5", "etapa 3/5", "etapa 4/5", "etapa 5/5"):
            self.assertIn(etapa, registo)
        self.assertIn("| TOTAL |", registo)

    def test_uma_accao_recusada_nao_inventa_resposta(self) -> None:
        teste = JarvisDeTeste()

        def recusar(_resultado, _config, **_kwargs):
            raise AcaoError("projeto 'x' nao esta na configuracao")

        teste.jarvis._executar = recusar  # type: ignore[assignment]
        teste.frase("Que horas são?")
        self.assertIn("accao local RECUSADA", teste.log.texto())
        self.assertEqual(teste.falados, ["Não consegui executar esse comando."])


class TestEntregaAoClaude(unittest.TestCase):
    def test_frase_fora_da_lista_branca_vai_ao_claude_uma_so_vez(self) -> None:
        teste = JarvisDeTeste(resposta_do_claude="Está tudo bem.")
        teste.frase("Pergunta ao claude o estado da ultima sessão")
        teste.frase("Pergunta ao claude outra coisa qualquer")
        self.assertEqual(len(teste.canal.perguntas), 2)
        self.assertEqual(teste.canais_abertos, 1, "a sessao-ponte mantem-se entre frases (D49)")
        self.assertIn("entregue ao Claude Code pelo degrau", teste.log.texto())

    def test_a_voz_nomeia_a_origem_da_resposta_do_claude(self) -> None:
        teste = JarvisDeTeste(resposta_do_claude="Li o ficheiro X.")
        teste.frase("Pergunta ao claude o que fizeste")
        self.assertEqual(len(teste.falados), 1)
        self.assertTrue(teste.falados[0].startswith(PREFIXO_DA_RESPOSTA_DO_CLAUDE))

    def test_canal_em_baixo_nao_derruba_o_jarvis(self) -> None:
        teste = JarvisDeTeste()

        def rebentar():
            raise RuntimeError("o CLI claude nao arrancou")

        teste.jarvis._abrir_canal = rebentar  # type: ignore[assignment]
        teste.frase("Pergunta ao claude uma coisa")
        self.assertIn("entrega ao Claude Code FALHOU", teste.log.texto())
        self.assertEqual(teste.falados, ["Não consegui falar com o Claude Code."])


class TestResumoFalado(unittest.TestCase):
    """D48(4): a voz nomeia a origem e nunca le a resposta inteira."""

    def test_junta_as_linhas_e_tira_as_crases(self) -> None:
        self.assertEqual(
            resumo_falado("linha um\n\nlinha `dois`"),
            f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} linha um linha dois",
        )

    def test_corta_no_fim_de_uma_palavra(self) -> None:
        falado = resumo_falado("palavra " * 50, limite=20)
        corpo = falado[len(PREFIXO_DA_RESPOSTA_DO_CLAUDE) + 1 :]
        self.assertTrue(corpo.endswith("..."))
        self.assertLessEqual(len(corpo), 24)
        self.assertNotIn("palav.", corpo)

    def test_resposta_vazia_diz_que_veio_vazia(self) -> None:
        self.assertIn("sem texto", resumo_falado("   "))


class TestEstadoDoProcesso(unittest.TestCase):
    """calar / adormecer / acordar: so existem num processo vivo (D4.d/D4.e)."""

    def test_adormecer_ignora_as_frases_seguintes_ate_acordar(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Adormece")
        self.assertTrue(teste.jarvis.estado.adormecido)
        teste.frase("Que horas são?")
        self.assertEqual(teste.accoes, [], "adormecido nao executa accoes")
        self.assertIn("o jarvis esta adormecido", teste.log.texto())
        teste.frase("Acorda")
        self.assertFalse(teste.jarvis.estado.adormecido)
        teste.frase("Que horas são?")
        self.assertEqual(teste.accoes, ["horas_e_data"])

    def test_adormecido_nunca_entrega_ao_claude(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Adormece")
        teste.frase("Pergunta ao claude uma coisa qualquer")
        self.assertEqual(teste.canais_abertos, 0)
        self.assertEqual(teste.canal.perguntas, [])

    def test_calar_mantem_a_resposta_so_na_consola(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Cala-te")
        self.assertTrue(teste.jarvis.estado.mudo)
        self.assertEqual(teste.falados, [], "depois de 'cala-te' a voz nao fala")
        teste.frase("Que horas são?")
        self.assertEqual(teste.falados, [])
        self.assertIn("voz desligada (modo calado (D4.d))", teste.log.texto())
        teste.frase("Acorda")
        self.assertFalse(teste.jarvis.estado.mudo)

    def test_sem_voz_regista_a_resposta_na_consola(self) -> None:
        teste = JarvisDeTeste()
        teste.jarvis.com_voz = False
        teste.frase("Que horas são?")
        self.assertEqual(teste.falados, [])
        self.assertIn("voz desligada (--sem-voz)", teste.log.texto())

    def test_estado_novo_comeca_acordado_e_com_voz(self) -> None:
        estado = EstadoDoProcesso()
        self.assertFalse(estado.adormecido)
        self.assertFalse(estado.mudo)


class TestInjeccaoDeFicheiro(unittest.TestCase):
    """S5/D33: os frames do WAV no mesmo pipeline, sem microfone."""

    def test_corta_em_chunks_do_tamanho_do_realtimestt(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "t.wav"
            escrever_wav_pcm16(caminho, b"\x01\x00" * 1536, 16000, 1)  # 3 chunks certos
            pedacos = list(frames_do_wav(caminho))
            self.assertEqual(len(pedacos), 3)
            self.assertTrue(all(len(c) == BYTES_POR_CHUNK for c in pedacos))

    def test_completa_o_ultimo_chunk_com_silencio(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "t.wav"
            escrever_wav_pcm16(caminho, b"\x01\x00" * 600, 16000, 1)  # 1200 bytes
            pedacos = list(frames_do_wav(caminho))
            self.assertEqual(len(pedacos), 2)
            self.assertEqual(len(pedacos[1]), BYTES_POR_CHUNK)
            self.assertEqual(pedacos[1][-2:], b"\x00\x00")

    def test_reamostra_para_16_khz(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "t.wav"
            escrever_wav_pcm16(caminho, b"\x01\x00" * 22050, 22050, 1)  # 1 s a 22050
            amostras = sum(len(c) for c in frames_do_wav(caminho)) / 2
            self.assertAlmostEqual(amostras, 16000, delta=BYTES_POR_CHUNK)

    def test_junta_os_dois_canais_de_um_wav_estereo(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "t.wav"
            escrever_wav_pcm16(caminho, b"\x01\x00\x01\x00" * 1024, 16000, 2)
            amostras = sum(len(c) for c in frames_do_wav(caminho)) / 2
            self.assertAlmostEqual(amostras, 1024, delta=512)

    def test_wav_vazio_nao_produz_frames(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "t.wav"
            escrever_wav_pcm16(caminho, b"", 16000, 1)
            self.assertEqual(list(frames_do_wav(caminho)), [])

    def test_silencio_da_cauda_tem_a_duracao_pedida(self) -> None:
        chunks = list(chunks_de_silencio(1.0))
        self.assertEqual(len(chunks), 31)  # 32000 bytes // 1024
        self.assertTrue(all(c == b"\x00" * BYTES_POR_CHUNK for c in chunks))


MENSAGEM_DO_WINERROR_6 = (
    "Error receiving data from connection: [WinError 6] The handle is invalid"
)


class _HandlerDeCaptura(logging.Handler):
    """Guarda as mensagens que chegam mesmo a um handler do logger raiz."""

    def __init__(self) -> None:
        super().__init__()
        self.mensagens: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.mensagens.append(record.getMessage())


class TestSilenciadorDoRuidoDoShutdown(unittest.TestCase):
    """Metade do processo pai (QA-close-1.md, finding 4b): so o WinError 6
    conhecido e descartado; tudo o resto do logger raiz continua a passar, e o
    logger fica exatamente como estava antes de o context manager correr.

    Esta metade NAO e a que resolve o caso Windows — la o emissor esta noutro
    processo e quem trata dele e `instalar_silenciador_no_processo_filho()`,
    provado em TestSilenciadorNoProcessoFilho com processos a serio. Aqui
    testa-se o que esta metade faz: defesa em profundidade e o caso Linux, em
    que o worker do RealtimeSTT e uma thread deste mesmo processo.
    """

    def _capturar(self, corpo) -> list[str]:
        """Corre `corpo()` com um handler no logger raiz e devolve o que passou.

        Comportamental de proposito: chama `logging.error`/`logging.info` como
        o RealtimeSTT chama, em vez de ir buscar o filtro a mao. Repoe o nivel e
        o handler no fim.
        """
        logger_raiz = logging.getLogger()
        handler = _HandlerDeCaptura()
        nivel_antes = logger_raiz.level
        logger_raiz.addHandler(handler)
        logger_raiz.setLevel(logging.DEBUG)
        try:
            corpo()
        finally:
            logger_raiz.setLevel(nivel_antes)
            logger_raiz.removeHandler(handler)
        return handler.mensagens

    def test_i_descarta_a_mensagem_exata_e_deixa_passar_tudo_o_resto(self) -> None:
        def corpo() -> None:
            with silenciar_ruido_do_shutdown():
                logging.error(MENSAGEM_DO_WINERROR_6)
                logging.error("Error receiving data from connection: o disco esta cheio")
                logging.error("[WinError 6] noutro sitio qualquer")
                logging.info("etapa 5/5 resposta falada")
            logging.error(MENSAGEM_DO_WINERROR_6)

        passaram = self._capturar(corpo)

        self.assertEqual(
            passaram,
            [
                # a mensagem do finding 4b desapareceu, e so ela
                "Error receiving data from connection: o disco esta cheio",
                "[WinError 6] noutro sitio qualquer",
                "etapa 5/5 resposta falada",
                # fora do `with`, ate ela volta a passar: nada fica permanente
                MENSAGEM_DO_WINERROR_6,
            ],
        )

    def test_ii_o_logger_raiz_fica_igual_depois_de_sair(self) -> None:
        logger_raiz = logging.getLogger()
        filtros_antes = list(logger_raiz.filters)
        handlers_antes = list(logger_raiz.handlers)
        nivel_antes = logger_raiz.level

        with silenciar_ruido_do_shutdown():
            self.assertNotEqual(list(logger_raiz.filters), filtros_antes)

        self.assertEqual(list(logger_raiz.filters), filtros_antes)
        self.assertEqual(list(logger_raiz.handlers), handlers_antes)
        self.assertEqual(logger_raiz.level, nivel_antes)

    def test_o_filtro_e_removido_mesmo_que_o_corpo_do_with_levante(self) -> None:
        logger_raiz = logging.getLogger()
        filtros_antes = list(logger_raiz.filters)

        with self.assertRaises(RuntimeError):
            with silenciar_ruido_do_shutdown():
                raise RuntimeError("recorder.shutdown() falhou a serio")

        self.assertEqual(list(logger_raiz.filters), filtros_antes)

    def test_no_processo_principal_nao_instala_nada(self) -> None:
        """Importar `jarvis.app` no processo principal nao mexe no logger raiz.

        O silenciador do filho corre no corpo do modulo; aqui garante-se que no
        pai isso e um no-op declarado — devolve False e nao deixa filtro nenhum.
        """
        logger_raiz = logging.getLogger()
        filtros_antes = list(logger_raiz.filters)

        self.assertFalse(instalar_silenciador_no_processo_filho())

        self.assertEqual(list(logger_raiz.filters), filtros_antes)


class TestSilenciadorNoProcessoFilho(unittest.TestCase):
    """A prova do finding 4b com processos a serio.

    O emissor real (`RealtimeSTT/audio_recorder.py:134`) corre num filho de
    `mp.Process` (audio_recorder.py:996, porque em Windows o sistema nao e
    Linux), por isso nenhum filtro do processo pai o apanha. `tests/_filho_ruidoso.py`
    monta a mesma situacao: um filho de verdade que faz `logging.error` com a
    mensagem exata e `exc_info=True`, mais um ERROR diferente e uma linha INFO.
    """

    RAIZ = Path(__file__).resolve().parent.parent
    OUTRO_ERRO = "Error receiving data from connection: o disco esta cheio"
    LINHA_DE_INFO = "linha informativa do filho que tem de continuar a passar"

    def _correr_auxiliar(self, *argumentos: str) -> subprocess.CompletedProcess:
        ambiente = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "tests._filho_ruidoso", *argumentos],
            cwd=str(self.RAIZ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ambiente,
            timeout=300,
        )

    def test_controlo_sem_o_silenciador_a_mensagem_aparece(self) -> None:
        """Controlo negativo: sem `jarvis.app` importado no filho, e o de hoje."""
        corrida = self._correr_auxiliar("--controlo")

        self.assertEqual(corrida.returncode, 0, corrida.stderr)
        self.assertIn(MENSAGEM_DO_WINERROR_6, corrida.stderr)
        self.assertIn("Traceback (most recent call last)", corrida.stderr)
        self.assertIn(self.OUTRO_ERRO, corrida.stderr)

    def test_com_o_silenciador_a_mensagem_do_filho_nao_chega_ao_stderr(self) -> None:
        corrida = self._correr_auxiliar()

        self.assertEqual(corrida.returncode, 0, corrida.stderr)
        # (b): a mensagem do filho e o seu traceback desapareceram do stderr do pai
        self.assertNotIn("WinError 6", corrida.stderr)
        self.assertNotIn("Traceback (most recent call last)", corrida.stderr)
        # e so ela: um ERROR diferente do mesmo filho continua a aparecer,
        # e uma linha de nivel INFO tambem
        self.assertIn(self.OUTRO_ERRO, corrida.stderr)
        self.assertIn(self.LINHA_DE_INFO, corrida.stderr)
        # o stdout do pai nao e tocado
        self.assertIn("pai: filho terminado", corrida.stdout)


class TestConfiguracaoEmFalta(unittest.TestCase):
    """Sem config.toml o jarvis continua, mas NUNCA adivinha um projeto (D4)."""

    def test_sem_ficheiro_avisa_e_segue_sem_projetos(self) -> None:
        log = LogFalso()
        config = carregar_config_tolerante(
            Path("nao-existe-config-de-teste.toml"), log  # type: ignore[arg-type]
        )
        self.assertEqual(config.projetos, ())
        self.assertIn("AVISO: configuracao privada por carregar", log.texto())

    def test_sem_projetos_um_comando_com_projeto_vai_como_texto(self) -> None:
        teste = JarvisDeTeste()
        teste.frase("Abre o VS Code no exemplo-um")
        self.assertEqual(teste.accoes, [])
        self.assertIn("decisao=claude", teste.log.texto())

    def test_com_projeto_conhecido_a_accao_e_local(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            alvo = Path(pasta) / "exemplo-um"
            alvo.mkdir()
            teste = JarvisDeTeste(_config_com_projeto(alvo))
            teste.frase("Abre o VS Code no exemplo-um")
            self.assertEqual(teste.accoes, ["abrir_vscode"])
            self.assertEqual(teste.canais_abertos, 0)


# --- T8/D58b/S10: lingua no caminho VIVO -----------------------------------


class RecorderFalso:
    """So os atributos que `detalhe_da_transcricao` le do RealtimeSTT."""

    def __init__(self, lingua, probabilidade) -> None:
        self.device = "cuda"
        self.main_model_type = "medium"
        self.detected_language = lingua
        self.detected_language_probability = probabilidade


class TestLinguaNoLogDaFrase(unittest.TestCase):
    """Criterio 2 da T8: cada frase do log diz lingua, probabilidade e hesitacao.

    ACHADO da T8 provado aqui: o RealtimeSTT 0.3.104 so expoe o TOP-1
    (`detected_language`/`detected_language_probability`), por isso o caminho
    vivo aplica o argmax restrito ao unico numero que tem — e uma terceira
    lingua no topo continua a nao decidir nada.
    """

    def test_portugues_decidido(self) -> None:
        detalhe = detalhe_da_transcricao(RecorderFalso("pt", 0.98), "que horas sao")
        self.assertIn("lingua=pt", detalhe)
        self.assertIn("p=0.98", detalhe)
        self.assertIn("decidida", detalhe)
        self.assertIn("texto: 'que horas sao'", detalhe)

    def test_ingles_decidido(self) -> None:
        detalhe = detalhe_da_transcricao(RecorderFalso("en", 0.87), "what time is it")
        self.assertIn("lingua=en", detalhe)
        self.assertIn("decidida", detalhe)

    def test_probabilidade_baixa_escreve_que_hesitou_e_porque(self) -> None:
        detalhe = detalhe_da_transcricao(RecorderFalso("en", 0.32), "what time is it")
        self.assertIn("lingua=en", detalhe)
        self.assertIn("hesitou", detalhe)
        self.assertIn("limiar", detalhe)

    def test_terceira_lingua_no_topo_nao_decide_e_fica_marcada_no_log(self) -> None:
        # D66, ponto 3 (B1 da tentativa 2): a lingua do PRODUTO continua a ser
        # `pt`, mas a linha de log tem de dizer que foi o espanhol a
        # descodificar — e o caminho vivo tambem corre com language=None.
        detalhe = detalhe_da_transcricao(RecorderFalso("es", 0.80), "que horas sao")
        self.assertIn("lingua=pt", detalhe)
        self.assertIn("hesitou", detalhe)
        self.assertIn("lingua-terceira(es descodificou)", detalhe)
        self.assertIn("descodificou o audio", detalhe)
        self.assertNotIn("ignorado", detalhe)

    def test_uma_frase_normal_nao_leva_marca_de_lingua_terceira(self) -> None:
        # Sem isto a marca nao valia nada: tem de aparecer so quando acontece.
        detalhe = detalhe_da_transcricao(RecorderFalso("pt", 0.97), "que horas sao")
        self.assertNotIn("lingua-terceira", detalhe)

    def test_sem_lingua_nenhuma_continua_a_escrever_a_linha(self) -> None:
        detalhe = detalhe_da_transcricao(RecorderFalso(None, None), "lixo")
        self.assertIn("lingua=pt", detalhe)
        self.assertIn("hesitou", detalhe)

    def test_o_resto_da_linha_nao_mudou(self) -> None:
        # O formato do log e o entregavel (D2/D11): device, modelo e prompt
        # continuam onde estavam.
        detalhe = detalhe_da_transcricao(RecorderFalso("pt", 0.9), "x")
        self.assertIn("device=cuda", detalhe)
        self.assertIn("modelo=medium", detalhe)
        self.assertIn("prompt=desligado (D51)", detalhe)


class TestRecorderPedeDeteccaoDeLingua(unittest.TestCase):
    """Criterio 6 da T8 no caminho vivo: a lingua do recorder e a do produto.

    A T8 ligou aqui a deteccao (`language=None`) e mediu-a; o A/B controlado
    deu o acerto de intencao em portugues a descer (21/40 -> 20/40) e o gatilho
    automatico do criterio 6 / D53 item 4 mandou reverter. O que este teste
    trava e a REVERSAO: o recorder pede a lingua do produto e nao um `None`
    solto — e pede-a pela constante, para nao haver dois sitios a dizer qual e.
    """

    def _opcoes_do_recorder(self, **kwargs) -> dict:
        capturadas: dict = {}

        class AudioToTextRecorderFalso:
            def __init__(self, **opcoes):
                capturadas.update(opcoes)

        modulo = types.ModuleType("RealtimeSTT")
        modulo.AudioToTextRecorder = AudioToTextRecorderFalso  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"RealtimeSTT": modulo}):
            construir_recorder(
                use_microphone=False, com_wake_word=False, device="cpu", **kwargs
            )
        return capturadas

    def test_language_e_a_lingua_fixa_do_produto(self) -> None:
        opcoes = self._opcoes_do_recorder()
        self.assertEqual(opcoes["language"], LINGUA_FIXA_DO_PRODUTO)
        self.assertEqual(opcoes["language"], "pt")

    def test_com_a_lingua_fixa_a_linha_do_log_e_curta_e_nao_inventa_numeros(self) -> None:
        # Como o recorder do produto: `language` preenchido (nao houve deteccao
        # nenhuma) e o `detected_language` a 1.0 e so o eco do que lhe demos.
        recorder = RecorderFalso(LINGUA_FIXA_DO_PRODUTO, 1.0)
        recorder.language = LINGUA_FIXA_DO_PRODUTO
        detalhe = detalhe_da_transcricao(recorder, "que horas sao")
        self.assertIn("lingua=pt FIXA (sem deteccao, T8 criterio 6)", detalhe)
        self.assertNotIn("p=1.00", detalhe)
        self.assertNotIn("hesitou", detalhe)
        # A justificacao inteira da reversao (~190 caracteres) nao se repete
        # em cada frase: fica em `lingua_fixada().motivo` e no comentario da
        # constante. Aqui basta a etiqueta.
        self.assertNotIn("A/B controlado", detalhe)
        self.assertIn("texto: 'que horas sao'", detalhe)

    def test_o_mecanismo_de_deteccao_continua_ligavel_e_testado(self) -> None:
        # A reversao desligou a deteccao no produto, nao a apagou (D66/T9):
        # com um recorder sem lingua, o caminho vivo volta a ler o top-1.
        detalhe = detalhe_da_transcricao(RecorderFalso("en", 0.93), "x")
        self.assertIn("lingua=en p=0.93 decidida", detalhe)

    def test_o_resto_da_configuracao_nao_mudou(self) -> None:
        opcoes = self._opcoes_do_recorder()
        self.assertIs(opcoes["use_microphone"], False)
        self.assertEqual(opcoes["beam_size"], 5)
        self.assertIsNone(opcoes["initial_prompt"])  # D51


if __name__ == "__main__":
    unittest.main()
