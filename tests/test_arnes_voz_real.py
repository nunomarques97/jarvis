r"""Testes do arnes de voz real: guioes, interface STT, gravador e avaliador.

Nenhum teste toca em GPU, em modelos, no microfone ou no altifalante: os
motores e a captura sao falsos e os WAV sao tons gerados numa pasta
temporaria. A medicao a serio precisa da voz do Sponsor (ver
scripts/gravar_voz.py e scripts/avaliar_voz.py).

Corre com:

    .venv\Scripts\python -m unittest tests.test_arnes_voz_real -v
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from jarvis import stt  # noqa: E402
from jarvis.audio_util import escrever_wav_pcm16  # noqa: E402
from jarvis.config import Config, Projeto  # noqa: E402
from scripts import avaliar_voz  # noqa: E402

gravar = avaliar_voz.gravar
PROJETOS = {"<projeto-1>": "exemplo-um", "<projeto-2>": "exemplo-dois"}


def _config() -> Config:
    return Config(
        microfone="",
        projetos=(Projeto("exemplo-um", RAIZ / "exemplo-um"), Projeto("exemplo-dois", RAIZ / "exemplo-dois")),
    )


def _silencioso():
    return contextlib.redirect_stdout(io.StringIO())


class _PastaTemporaria(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name).resolve()
        self.pasta = gravar.pasta_de_gravacoes(None, self.raiz)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# --- Guioes -------------------------------------------------------------------------


class TestGuioes(unittest.TestCase):
    def test_os_dois_guioes_cumprem_os_minimos(self) -> None:
        for lingua in gravar.LINGUAS:
            with self.subTest(lingua=lingua):
                frases = gravar.ler_guiao(gravar.GUIOES[lingua], lingua)
                self.assertGreaterEqual(len(frases), 40)
                self.assertEqual(gravar.verificar_cobertura(frases), [])
                contagem = {caso: sum(f.caso == caso for f in frases) for caso in gravar.MINIMO_POR_CASO}
                self.assertGreaterEqual(contagem["a"], 10)
                self.assertGreaterEqual(contagem["b"], 8)
                self.assertGreaterEqual(contagem["c"], 8)
                self.assertGreaterEqual(contagem["local"], 10)
                self.assertGreaterEqual(contagem["confirmacao"], 4)
                self.assertEqual(sum(f.ativacao for f in frases) * 2, len(frases))

    def test_comandos_locais_e_confirmacoes_cobrem_o_pedido(self) -> None:
        for lingua in gravar.LINGUAS:
            frases = gravar.ler_guiao(gravar.GUIOES[lingua], lingua)
            locais = {f.intencao for f in frases if f.caso == "local"}
            confirmacoes = {f.intencao for f in frases if f.caso == "confirmacao"}
            with self.subTest(lingua=lingua):
                self.assertTrue({"horas", "abrir_editor", "abrir_pasta", "calar", "dormir", "acordar"} <= locais)
                self.assertTrue({"confirmar", "corrigir", "acrescentar", "cancelar"} <= confirmacoes)

    def test_cada_frase_tem_intencao_e_projeto_so_por_marcador(self) -> None:
        for lingua in gravar.LINGUAS:
            for frase in gravar.ler_guiao(gravar.GUIOES[lingua], lingua):
                with self.subTest(id=frase.id):
                    self.assertIn(frase.intencao, gravar.INTENCOES)
                    self.assertIn(frase.projeto, (None, *gravar.MARCADORES_DE_PROJETO))
                    for marcador in gravar.PADRAO_MARCADOR.findall(frase.frase):
                        self.assertIn(marcador, gravar.MARCADORES_DE_PROJETO)

    def test_guioes_versionados_sem_caminhos_nem_nomes_reais(self) -> None:
        for caminho in gravar.GUIOES.values():
            texto = caminho.read_text(encoding="utf-8")
            with self.subTest(guiao=caminho.name):
                for proibido in (":\\", "/Users/", "\\Users\\", "Desktop", "Repositorios"):
                    self.assertNotIn(proibido, texto)

    def _guiao(self, linhas: list[str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        caminho = Path(tmp.name) / "guiao.md"
        cabecalho = ["| id | caso | frase | intenção | projeto | ativação |", "|---|---|---|---|---|---|"]
        caminho.write_text("\n".join(cabecalho + linhas), encoding="utf-8")
        return caminho

    def test_parser_recusa_entradas_erradas(self) -> None:
        casos = {
            "marcador desconhecido": "| pt-01 | a | pede ao <cliente> para x | ditar_prompt | — | não |",
            "intencao fora da lista": "| pt-01 | local | que horas são | comprar | — | não |",
            "ativacao que nao bate": "| pt-01 | local | que horas são | horas | — | sim |",
            "projeto ausente da frase": "| pt-01 | a | corrige os testes | ditar_prompt | <projeto-1> | não |",
            "id de outra lingua": "| en-01 | local | que horas são | horas | — | não |",
            "caso desconhecido": "| pt-01 | outro | que horas são | horas | — | não |",
        }
        for nome, linha in casos.items():
            with self.subTest(nome):
                with self.assertRaises(gravar.GuiaoError):
                    gravar.ler_guiao(self._guiao([linha]), "pt")
        linha = "| pt-01 | local | que horas são | horas | — | não |"
        with self.assertRaises(gravar.GuiaoError):
            gravar.ler_guiao(self._guiao([linha, linha]), "pt")

    def test_marcadores_trocados_pelos_nomes_da_configuracao(self) -> None:
        frase = next(f for f in gravar.ler_guiao(gravar.GUIOES["pt"], "pt") if f.projeto)
        self.assertNotIn("<projeto", gravar.texto_a_ler(frase, PROJETOS))
        nomes = gravar.nomes_dos_marcadores(_config())
        self.assertEqual(nomes, PROJETOS)
        with self.assertRaises(gravar.GuiaoError):
            gravar.nomes_dos_marcadores(Config(microfone="", projetos=()))


# --- Interface STT -----------------------------------------------------------------


class _MotorContador(stt.MotorBase):
    nome = "contador"

    def __init__(self) -> None:
        super().__init__("cpu")
        self.carregamentos = 0

    def _carregar_modelo(self):
        self.carregamentos += 1
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        return " ola   mundo ", lingua or "en", lingua is None


class TestInterfaceStt(unittest.TestCase):
    def test_carrega_uma_vez_e_mede_latencia(self) -> None:
        motor = _MotorContador()
        um_segundo = b"\x01\x00" * stt.TAXA_DO_MOTOR
        primeira = motor.transcrever(um_segundo, lingua="pt")
        motor.transcrever(um_segundo)
        self.assertEqual(motor.carregamentos, 1)
        self.assertEqual(primeira.texto, "ola mundo")
        self.assertEqual((primeira.lingua, primeira.lingua_detetada), ("pt", False))
        self.assertEqual(primeira.duracao_audio_s, 1.0)
        self.assertGreaterEqual(primeira.latencia_ms, 0.0)
        self.assertEqual(primeira.motor, "contador")

    def test_entrada_invalida_recusada(self) -> None:
        motor = _MotorContador()
        with self.assertRaises(ValueError):
            motor.transcrever(b"\x00")
        with self.assertRaises(TypeError):
            motor.transcrever("texto")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            motor.transcrever(b"\x00\x00", lingua="fr")
        with self.assertRaises(ValueError):
            stt.MotorFasterWhisper("small")
        with self.assertRaises(ValueError):
            stt.criar_motor("inventado")

    def test_lista_de_motores(self) -> None:
        self.assertEqual(stt.MOTORES, ("whisper-medium", "whisper-large-v3-turbo", "parakeet-tdt-0.6b-v3"))
        self.assertIsInstance(stt.criar_motor("parakeet-tdt-0.6b-v3", "cpu"), stt.MotorParakeet)
        self.assertEqual(stt.criar_motor("whisper-large-v3-turbo", "cpu").modelo, "large-v3-turbo")

    def test_parakeet_sem_modelo_e_indisponivel_com_o_comando(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            motor = stt.MotorParakeet("cpu", pasta=Path(pasta) / "nao-existe")
            with self.assertRaises(stt.MotorIndisponivel) as erro:
                motor.transcrever(b"\x00\x00" * 100)
        self.assertIn("python", str(erro.exception))
        self.assertFalse(motor.carregado)

    def test_whisper_sem_pesos_e_indisponivel_sem_descarregar(self) -> None:
        chamadas: list[dict] = []

        class ModeloQueFalta:
            def __init__(self, *args, **kwargs) -> None:
                chamadas.append(kwargs)
                raise RuntimeError("Cannot find an appropriate cached snapshot folder; local_files_only")

        falso = mock.MagicMock()
        falso.WhisperModel = ModeloQueFalta
        with mock.patch.dict(sys.modules, {"faster_whisper": falso}):
            motor = stt.MotorFasterWhisper("medium", "cpu")
            with self.assertRaises(stt.MotorIndisponivel):
                motor.carregar()
        self.assertTrue(chamadas[0]["local_files_only"])

    def test_erro_de_carregamento_vira_indisponivel(self) -> None:
        class Rebenta(_MotorContador):
            def _carregar_modelo(self):
                raise MemoryError("sem VRAM")

        with self.assertRaises(stt.MotorIndisponivel):
            Rebenta().carregar()

    def test_autoteste_do_modulo(self) -> None:
        with _silencioso():
            self.assertEqual(stt._autoteste(), 0)


# --- Gravador ------------------------------------------------------------------------


class TestGravador(_PastaTemporaria):
    def test_recusa_pastas_fora_de_recordings(self) -> None:
        for valor in ("../fora", str(self.raiz.parent / "fora"), "tests", "recordings/../tests"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    gravar.pasta_de_gravacoes(valor, self.raiz)
        self.assertEqual(gravar.pasta_de_gravacoes("recordings/sessao", self.raiz), self.raiz / "recordings" / "sessao")

    def _gravar(self, frases, respostas, **kwargs):
        with _silencioso():
            return gravar.gravar_guiao(
                "pt", frases, PROJETOS, self.pasta, kwargs.pop("captura", gravar.CapturaFalsa()),
                entrada=gravar.entrada_roteirizada(respostas), raiz=self.raiz, **kwargs,
            )

    def test_retoma_onde_parou_e_manifesto_local(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:4]
        primeira = self._gravar(frases, ["", "", "q"])
        self.assertEqual(primeira.gravadas, ["pt-01"])
        self.assertTrue(primeira.interrompido)
        segunda = self._gravar(frases, [""] * 8)
        self.assertEqual(segunda.ja_existiam, ["pt-01"])
        self.assertEqual(segunda.gravadas, ["pt-02", "pt-03", "pt-04"])
        manifesto = json.loads((self.pasta / "pt" / "manifesto.json").read_text(encoding="utf-8"))
        self.assertEqual(manifesto["projetos"], PROJETOS)
        self.assertEqual(manifesto["gravacoes"]["pt-01"]["origem"], gravar.ORIGEM_SINTETICA)
        self.assertNotIn(str(self.raiz), json.dumps(manifesto))
        # Um WAV apagado a mao volta a ser pedido.
        (self.pasta / "pt" / "pt-02.wav").unlink()
        terceira = self._gravar(frases, [""] * 4)
        self.assertEqual(terceira.gravadas, ["pt-02"])

    def test_nunca_toca_som_sem_com_som(self) -> None:
        def tocar():
            raise AssertionError("tocou som sem --com-som")

        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:2]
        resumo = self._gravar(frases, [""] * 4, tocar_sinal=tocar)
        self.assertEqual(len(resumo.gravadas), 2)
        sinais: list[int] = []
        self._gravar(frases, [""] * 4, com_som=True, tocar_sinal=lambda: sinais.append(1), refazer=["pt-01"])
        self.assertEqual(sinais, [1])

    def test_sem_sinal_nao_grava(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:1]
        resumo = self._gravar(frases, ["", "", "q"], captura=gravar.CapturaFalsa(b"\x00\x00" * 16_000))
        self.assertEqual(resumo.gravadas, [])
        self.assertFalse((self.pasta / "pt" / "pt-01.wav").exists())

    def _wavs(self) -> set[str]:
        return {p.name for p in (self.pasta / "pt").glob("*.wav")}

    def test_captura_mais_rapida_que_o_relogio_repete_a_frase(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:1]
        # 3 s de audio em 10 ms de relogio (o defeito do DirectSound), depois uma boa.
        captura = gravar.CapturaFalsa(gravar.tom_pcm16(3.0), segundos_reais=[0.01, 3.0])
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            resumo = gravar.gravar_guiao(
                "pt", frases, PROJETOS, self.pasta, captura,
                entrada=gravar.entrada_roteirizada([""] * 4), raiz=self.raiz,
            )
        self.assertEqual((resumo.recusadas, resumo.gravadas), (1, ["pt-01"]))
        self.assertIn("mais rapida que o relogio", saida.getvalue())
        self.assertIn("--verificar", saida.getvalue())
        registo = gravar.ler_manifesto(self.pasta / "pt")["gravacoes"]["pt-01"]
        self.assertEqual(registo["tempo_real_s"], 3.0)

    def test_captura_com_troco_de_zeros_nao_e_gravada(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:1]
        com_buraco = gravar.tom_pcm16(1.0) + b"\x00\x00" * 8000 + gravar.tom_pcm16(1.0)
        resumo = self._gravar(frases, ["", "", "q"], captura=gravar.CapturaFalsa(com_buraco))
        self.assertEqual((resumo.recusadas, resumo.gravadas), (1, []))
        self.assertFalse((self.pasta / "pt" / "pt-01.wav").exists())

    def _estragar(self, id_: str, pcm: bytes, duracao_no_manifesto: float | None = None) -> None:
        escrever_wav_pcm16(self.pasta / "pt" / f"{id_}.wav", pcm, 16_000)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        registo = manifesto["gravacoes"][id_]
        registo.pop("tempo_real_s", None)
        registo["duracao_s"] = round(len(pcm) / 2 / 16_000, 3) if duracao_no_manifesto is None else duracao_no_manifesto
        gravar.escrever_manifesto(self.pasta / "pt", manifesto)

    def test_gravacoes_antigas_invalidas_voltam_a_ser_pedidas_sem_apagar_nada(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:3]
        self._gravar(frases, [""] * 6)
        # pt-01 como as do DirectSound: ~2 s de fala e o resto zeros ate ao teto.
        self._estragar("pt-01", gravar.tom_pcm16(2.0) + b"\x00\x00" * 16_000 * 18)
        # pt-02 com uma duracao que nao bate com o manifesto.
        self._estragar("pt-02", gravar.tom_pcm16(1.0), duracao_no_manifesto=19.968)
        invalidas = gravar.gravacoes_invalidas(self.pasta / "pt", gravar.ler_manifesto(self.pasta / "pt"))
        self.assertEqual(sorted(invalidas), ["pt-01", "pt-02"])
        self.assertIn("silencio digital", invalidas["pt-01"])
        self.assertIn("manifesto", invalidas["pt-02"])
        antes = self._wavs()
        resumo = self._gravar(frases, [""] * 4)
        self.assertEqual(resumo.ja_existiam, ["pt-03"])
        self.assertEqual(resumo.gravadas, ["pt-01", "pt-02"])
        depois = self._wavs()
        self.assertLessEqual(antes, depois)  # nada apagado
        self.assertEqual(len([n for n in depois if ".invalida-" in n]), 2)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        self.assertTrue(manifesto["gravacoes"]["pt-01"]["substituiu"].startswith("pt-01.invalida-"))
        self.assertEqual(gravar.gravacoes_invalidas(self.pasta / "pt", manifesto), {})
        # Tudo valido: a sessao seguinte nao pede nada.
        self.assertEqual(self._gravar(frases, ["q"]).gravadas, [])

    def test_manifesto_sem_duracao_e_invalido(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:1]
        self._gravar(frases, [""] * 2)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        del manifesto["gravacoes"]["pt-01"]["duracao_s"]
        gravar.escrever_manifesto(self.pasta / "pt", manifesto)
        self.assertIn("pt-01", gravar.gravacoes_invalidas(self.pasta / "pt", manifesto))

    def test_autoteste_do_gravador(self) -> None:
        with _silencioso():
            self.assertEqual(gravar.main(["--autoteste"]), 0)

    def test_pasta_invalida_na_linha_de_comandos(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), _silencioso():
            self.assertEqual(gravar.main(["--lingua", "pt", "--pasta", "../fora"]), 2)


# --- Avaliador ----------------------------------------------------------------------


class _MotorFalso(stt.MotorBase):
    def __init__(self, nome: str, textos: dict[int, str] | None = None, falha_em: int | None = None) -> None:
        super().__init__("cpu")
        self.nome = nome
        self.textos = textos or {}
        self.falha_em = falha_em
        self.chamadas = 0

    def _carregar_modelo(self):
        return object()

    def _inferir(self, modelo, pcm16, lingua):
        self.chamadas += 1
        if self.falha_em is not None and self.chamadas >= self.falha_em:
            raise RuntimeError("falha simulada")
        return self.textos.get(len(pcm16) // 2, "obrigado"), lingua, False


class _MotorAusente(_MotorFalso):
    def _carregar_modelo(self):
        raise stt.MotorIndisponivel("ausente: por instalar")


class TestAvaliador(_PastaTemporaria):
    def setUp(self) -> None:
        super().setUp()
        self.evidencia = self.raiz / "docs" / "forja" / "evidence"
        self.saida = avaliar_voz.medir.caminho_evidencia_de_saida(self.evidencia / "t.md", self.evidencia, self.raiz)

    def _correr(self, motores, linguas=("pt",)):
        return avaliar_voz.correr(list(linguas), motores, _config(), "ficticia", self.pasta, self.saida, "cpu")

    def test_faixas_e_projeto(self) -> None:
        self.assertEqual(
            [avaliar_voz.faixa_de(d) for d in (0.3, 0.999, 1.0, 2.0, 9.0)],
            ["<1 s", "<1 s", "1-2 s", ">2 s", ">2 s"],
        )
        nomes = ["exemplo-um", "exemplo-dois"]
        self.assertEqual(avaliar_voz.projeto_mencionado("abre o Exemplo Um", nomes), "exemplo-um")
        self.assertEqual(avaliar_voz.projeto_mencionado("muda exemplo-um para exemplo-dois", nomes), "exemplo-dois")
        self.assertIsNone(avaliar_voz.projeto_mencionado("abre o exemplo umbigo", nomes))

    def test_projeto_com_o_nome_da_palavra_de_ativacao(self) -> None:
        nomes = ["exemplo-um", "jarvis"]
        self.assertIsNone(avaliar_voz.projeto_mencionado("Hey Jarvis, what time is it?", nomes, "en"))
        self.assertIsNone(avaliar_voz.projeto_mencionado("boas jarvis, acorda", nomes, "pt"))
        self.assertEqual(avaliar_voz.projeto_mencionado("hey jarvis, open the jarvis folder", nomes, "en"), "jarvis")
        self.assertEqual(avaliar_voz.projeto_mencionado("abre a pasta do jarvis", nomes, "pt"), "jarvis")
        # So sai a palavra de ativacao da lingua da frase, e so do inicio.
        self.assertEqual(avaliar_voz.projeto_mencionado("boas jarvis", nomes, "en"), "jarvis")
        # Com esse projeto como <projeto-2>, um motor perfeito acerta o projeto em todas as frases.
        projetos = {"<projeto-1>": "exemplo-um", "<projeto-2>": "jarvis"}
        config = Config(
            microfone="",
            projetos=(Projeto("exemplo-um", RAIZ / "exemplo-um"), Projeto("jarvis", RAIZ / "jarvis")),
        )
        for lingua in gravar.LINGUAS:
            with self.subTest(lingua=lingua):
                n = len(gravar.ler_guiao(gravar.GUIOES[lingua], lingua))
                textos = avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, lingua, projetos, n)
                dados = avaliar_voz.carregar_gravacoes(self.pasta, lingua)
                resultado = avaliar_voz.avaliar([_MotorFalso("perfeito", textos)], dados.gravacoes, config)
                todas = {a.faixa: a for a in avaliar_voz.agregar(resultado.linhas)}[avaliar_voz.TODAS]
                self.assertEqual((todas.n, todas.projeto_certo), (n, n))

    def test_projetos_lidos_por_gravacao_sobrevivem_a_mudancas_do_config(self) -> None:
        frases = gravar.ler_guiao(gravar.GUIOES["pt"], "pt")[:2]
        with _silencioso():
            gravar.gravar_guiao("pt", frases[:1], PROJETOS, self.pasta, gravar.CapturaFalsa(),
                                entrada=gravar.entrada_roteirizada([""] * 2), raiz=self.raiz)
            novos = {"<projeto-1>": "outro-um", "<projeto-2>": "outro-dois"}
            gravar.gravar_guiao("pt", frases, novos, self.pasta, gravar.CapturaFalsa(gravar.tom_pcm16(1.5)),
                                entrada=gravar.entrada_roteirizada([""] * 2), raiz=self.raiz)
        dados = avaliar_voz.carregar_gravacoes(self.pasta, "pt")
        por_id = {g.frase.id: g for g in dados.gravacoes}
        self.assertIn("exemplo-um", por_id["pt-01"].referencia)
        self.assertEqual(por_id["pt-01"].projeto_esperado, "exemplo-um")
        self.assertEqual(por_id["pt-02"].projeto_esperado, "outro-dois")
        # Os nomes lidos contam mesmo sem estarem no config.toml atual.
        motor = _MotorFalso("repete")
        motor.textos = {len(g.pcm) // 2: g.referencia for g in dados.gravacoes}
        resultado = avaliar_voz.avaliar([motor], dados.gravacoes, _config())
        self.assertTrue(all(l.projeto_certo for l in resultado.linhas))

    def test_aquecimento_fica_fora_das_latencias(self) -> None:
        textos = avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, "pt", PROJETOS, 3)
        motor = _MotorFalso("perfeito", textos)
        dados = avaliar_voz.carregar_gravacoes(self.pasta, "pt")
        resultado = avaliar_voz.avaliar([motor], dados.gravacoes, _config())
        self.assertEqual(motor.chamadas, 4)
        self.assertEqual(len(resultado.linhas), 3)
        self.assertIn("perfeito", resultado.aquecimento_ms)

    def test_motor_perfeito_e_motores_saltados(self) -> None:
        textos = avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, "pt", PROJETOS, 10)
        perfeito = _MotorFalso("perfeito", textos)
        resultado, texto = self._correr(
            [perfeito, _MotorAusente("ausente"), _MotorFalso("meio", falha_em=3), _MotorFalso("surdo")]
        )
        self.assertEqual(set(resultado.saltados), {"ausente", "meio"})
        self.assertFalse(any(l.motor in ("ausente", "meio") for l in resultado.linhas))
        self.assertIn("SALTADO", texto)
        agregados = {(a.motor, a.faixa): a for a in avaliar_voz.agregar(resultado.linhas)}
        todas = agregados[("perfeito", avaliar_voz.TODAS)]
        self.assertEqual((todas.n, todas.wer, todas.intencao_preservada, todas.projeto_certo), (10, 0.0, 10, 10))
        self.assertEqual(todas.ativacao_detetadas, todas.ativacao_esperadas)
        self.assertEqual(todas.ativacao_falsas, 0)
        self.assertLessEqual(todas.p50_ms, todas.p95_ms)
        self.assertTrue({"<1 s", "1-2 s", ">2 s"} <= {f for (m, f) in agregados if m == "perfeito"})
        surdo = agregados[("surdo", avaliar_voz.TODAS)]
        self.assertEqual(surdo.ativacao_detetadas, 0)
        self.assertLess(surdo.projeto_certo, surdo.n)
        self.assertGreater(surdo.wer, 0.9)

    def test_audio_sintetico_marca_a_evidencia(self) -> None:
        textos = avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, "pt", PROJETOS, 4)
        _, texto = self._correr([_MotorFalso("perfeito", textos)])
        self.assertIn("SEM VOZ DO SPONSOR — não decide nada", texto)
        self.assertEqual(self.saida.read_text(encoding="utf-8"), texto)
        self.assertNotIn(str(self.raiz), texto)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        for registo in manifesto["gravacoes"].values():
            registo["origem"] = gravar.ORIGEM_MICROFONE
        gravar.escrever_manifesto(self.pasta / "pt", manifesto)
        _, texto = self._correr([_MotorFalso("perfeito", textos)])
        self.assertNotIn("SEM VOZ DO SPONSOR", texto)

    def test_sem_gravacoes_fica_pendente_e_nenhum_motor_corre(self) -> None:
        motor = _MotorFalso("perfeito")
        resultado, texto = self._correr([motor], linguas=("pt", "en"))
        self.assertIn("PENDENTE — passo do Sponsor (pt)", texto)
        self.assertIn("gravar_voz.py --lingua en", texto)
        self.assertEqual(motor.chamadas, 0)
        self.assertEqual(resultado.linhas, [])
        self.assertIn("não corrido (sem gravações)", texto)
        self.assertNotIn("| avaliado |", texto)

    def test_manifesto_nao_le_fora_da_pasta(self) -> None:
        avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, "pt", PROJETOS, 2)
        fora = self.raiz / "fora.wav"
        escrever_wav_pcm16(fora, gravar.tom_pcm16(0.5), 16_000)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        manifesto["gravacoes"]["pt-01"]["ficheiro"] = "../../fora.wav"
        gravar.escrever_manifesto(self.pasta / "pt", manifesto)
        dados = avaliar_voz.carregar_gravacoes(self.pasta, "pt")
        self.assertEqual([g.frase.id for g in dados.gravacoes], ["pt-02"])
        self.assertTrue(any("pt-01" in p for p in dados.problemas))

    def test_gravacoes_invalidas_marcadas_e_excluidas(self) -> None:
        textos = avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, "pt", PROJETOS, 4)
        com_zeros = gravar.tom_pcm16(0.6) + b"\x00\x00" * 16_000
        escrever_wav_pcm16(self.pasta / "pt" / "pt-01.wav", com_zeros, 16_000)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        manifesto["gravacoes"]["pt-01"]["duracao_s"] = 1.6
        manifesto["gravacoes"]["pt-03"]["duracao_s"] = 19.968
        gravar.escrever_manifesto(self.pasta / "pt", manifesto)
        dados = avaliar_voz.carregar_gravacoes(self.pasta, "pt")
        self.assertEqual(sorted(dados.invalidas), ["pt-01", "pt-03"])
        self.assertEqual([g.frase.id for g in dados.gravacoes], ["pt-02", "pt-04"])
        motor = _MotorFalso("perfeito", textos)
        resultado, texto = self._correr([motor])
        self.assertEqual(motor.chamadas, 3)  # aquecimento + 2 validas
        self.assertEqual({l.id for l in resultado.linhas}, {"pt-02", "pt-04"})
        self.assertIn("GRAVAÇÕES INVÁLIDAS (pt): 2", texto)
        self.assertIn("## Gravações inválidas", texto)

    def test_so_gravacoes_invalidas_fica_pendente(self) -> None:
        avaliar_voz.preparar_gravacoes_sinteticas(self.pasta, "pt", PROJETOS, 1)
        escrever_wav_pcm16(self.pasta / "pt" / "pt-01.wav", b"\x00\x00" * 16_000, 16_000)
        manifesto = gravar.ler_manifesto(self.pasta / "pt")
        manifesto["gravacoes"]["pt-01"]["duracao_s"] = 1.0
        gravar.escrever_manifesto(self.pasta / "pt", manifesto)
        motor = _MotorFalso("perfeito")
        _, texto = self._correr([motor])
        self.assertEqual(motor.chamadas, 0)
        self.assertIn("Não há gravações válidas desta língua", texto)

    def test_evidencia_so_em_docs_forja_evidence(self) -> None:
        for valor in (self.raiz / "fora.md", self.evidencia / "x.txt", self.raiz / "README.md"):
            with self.subTest(valor=str(valor)):
                with self.assertRaises(ValueError):
                    avaliar_voz.medir.caminho_evidencia_de_saida(valor, self.evidencia, self.raiz)

    def test_router_traduzido_para_a_lista_fechada(self) -> None:
        config = _config()
        self.assertEqual(avaliar_voz.intencao_pelo_router("que horas são", config), "horas")
        self.assertEqual(avaliar_voz.intencao_pelo_router("", config), "desconhecido")
        self.assertEqual(avaliar_voz.intencao_pelo_router("corrige os testes do exemplo-um", config), "ditar_prompt")
        self.assertTrue(set(avaliar_voz.INTENCAO_DA_ACAO_LOCAL.values()) <= set(gravar.INTENCOES))

    def test_device_por_motor_explicito_e_validado(self) -> None:
        self.assertEqual(
            avaliar_voz.motores_pedidos("whisper-medium, parakeet-tdt-0.6b-v3:cpu", "cuda"),
            [("whisper-medium", "cuda"), ("parakeet-tdt-0.6b-v3", "cpu")],
        )
        for errado in ("inventado", "whisper-medium:tpu", " , "):
            with self.assertRaises(ValueError):
                avaliar_voz.motores_pedidos(errado, "cuda")
        with _silencioso(), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(avaliar_voz.main(["--motores", "whisper-medium:tpu"]), 2)
        self.assertEqual(
            avaliar_voz.motores_pedidos(avaliar_voz.MOTORES_POR_OMISSAO, "cuda")[-1],
            ("parakeet-tdt-0.6b-v3", "cpu"),
        )

    def test_autoteste_do_avaliador(self) -> None:
        with _silencioso():
            self.assertEqual(avaliar_voz.main(["--autoteste"]), 0)


# --- Privacidade: o que o arnes escreve fica fora do Git ----------------------------


class TestFicheirosIgnorados(unittest.TestCase):
    def test_gravacoes_manifesto_e_evidencia_ignorados(self) -> None:
        caminhos = (
            "recordings/pt/pt-01.wav",
            "recordings/en/manifesto.json",
            "docs/forja/evidence/avaliar-voz-20260101-000000.md",
            "tmp/autoteste-avaliar-voz/x.wav",
        )
        try:
            saida = subprocess.run(
                ["git", "check-ignore", "--no-index", *caminhos],
                cwd=RAIZ, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as erro:
            self.skipTest(f"git indisponivel: {erro}")
        self.assertEqual(sorted(saida.stdout.split()), sorted(caminhos))


if __name__ == "__main__":
    unittest.main()
