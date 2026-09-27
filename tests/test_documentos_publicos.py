r"""Os documentos publicos da naturalidade e do cerebro estao completos e nao expoem nada privado.

docs/NATURALNESS.md e a pesquisa e o plano para o jarvis soar natural. O teste garante
que tem as seccoes pedidas (uma por tecnica, cada uma com os seis sistemas comparados,
o que corre local e de graca, o ganho e o custo), a seccao informativa sobre voz na
cloud, as metas numericas e a tabela das mudancas, que as fontes sao https e que nem
esse documento nem o docs/ROADMAP.md tem caminhos de disco ou de utilizador. Tambem
confirma que o ROADMAP e o README apontam para ele.

docs/BRAIN.md e a pesquisa e o desenho do cerebro de conversa. O teste garante que
esta em ingles, que tem as seccoes pedidas (tabela das ferramentas com o "yes" falado,
orcamento de latencia com a meta de 1.5 s, teto de tokens, mudancas com a metrica), que
todas as fontes sao https e primarias, que nao tem caminhos privados, audio, e-mails
nem transcricoes da sessao real, e que o ROADMAP e o README apontam para ele.

Corre com:

    .venv\Scripts\python -m unittest tests.test_documentos_publicos -v
"""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NATURALIDADE = RAIZ / "docs" / "NATURALNESS.md"
ROADMAP = RAIZ / "docs" / "ROADMAP.md"
README = RAIZ / "README.md"
CEREBRO = RAIZ / "docs" / "BRAIN.md"

#: Tecnicas que a pesquisa tem de cobrir, cada uma como seccao propria.
TECNICAS = (
    "Turn-taking and barge-in",
    "Echo handling",
    "Semantic end-of-turn detection",
    "End-to-end streaming",
    "LLM-generated replies with a short persona",
    "Context assumptions",
    "Light confirmations",
    "Fillers and backchannels",
    "Prosody and speaking rate",
)
#: Sistemas comparados em cada tecnica.
SISTEMAS = (
    "ChatGPT voice",
    "Gemini Live",
    "Alexa+",
    "Pipecat",
    "LiveKit Agents",
    "Home Assistant Assist",
)
#: Seccoes de topo obrigatorias alem das tecnicas.
SECCOES = (
    "Hardware budget",
    "Techniques",
    "Numeric targets",
    "Planned changes and the metric each moves",
    "Cloud speech-to-speech (information only)",
    "Sources",
)

#: Seccoes de topo obrigatorias do desenho do cerebro.
SECCOES_DO_CEREBRO = (
    "Problems seen in the 2026-09-27 session",
    "Connecting the brain",
    "Default model",
    "Tools exposed to the brain",
    "Safety rules",
    "Latency budget",
    "Context and long sessions",
    "Notices queue",
    "Fate of the local qwen3:8b classifier",
    "Token cost per exchange and guaranteed ceiling",
    "Numeric targets",
    "Planned changes and the metric each moves",
    "Sources",
)
#: Ferramentas que so criam o recap: todas precisam do "yes" falado.
FERRAMENTAS_COM_EFEITO = (
    "enviar_ao_projeto",
    "lancar_run",
    "parar_run",
    "retomar_run",
    "lembrar_facto",
    "esquecer_facto",
)
#: Ferramentas so de leitura: nenhuma precisa do "yes".
FERRAMENTAS_DE_LEITURA = (
    "WebSearch",
    "WebFetch",
    "hora_e_data",
    "listar_projetos",
    "estado_do_projeto",
    "relatorio_do_projeto",
    "factos_guardados",
    "avisos_pendentes",
)
#: Fontes primarias aceites: documentacao da Anthropic e do Claude Code, o repositorio
#: oficial do SDK e a especificacao do MCP.
FONTES_PRIMARIAS = (
    "https://code.claude.com/",
    "https://platform.claude.com/",
    "https://docs.claude.com/",
    "https://support.claude.com/",
    "https://www.anthropic.com/",
    "https://github.com/anthropics/",
    "https://modelcontextprotocol.io/",
)
#: Resumos sha256 de frases reais da sessao de 2026-09-27 (transcricoes e respostas),
#: normalizadas em palavras minusculas; o texto nunca fica no repositorio.
_FRASES_REAIS = frozenset({
    "04fc89b35a4ae118e2fa704e1a9b3f18d0dfc810c1386637eda15bb4df0eac27",
    "ca465ff6d1191ae02df0ed8d81a63b7b98b311c814469112b84c6144bbf5e799",
    "5bb784fa752bf89102af7574f13a06e75f9025839271b627acc2dca39cb66a62",
    "b1f50d236015f22971f9408bfbf8e7b290e3f996934fa3cb54e265381ec973e5",
    "8a4ff425ed53acddb1c2e4eda256f44d23fbb872cd9c164538fe283cf7919ead",
    "615b6726a7241c6a83f88b777a2423d79862cf6b6f0b6549bd7ebd18cfc3eb08",
    "31fb4a25868a8ed28476fc7e5c4ec04ccef75b7ea07b90efa80bc3857eb5a40d",
    "8c07e881db174aef52b857d4de12cb501e03e4fe1b53b66419585bda4cb17c3c",
    "9b65a994d98aa634021b5320fdc2635faa703b368a00186264e89ee1924759b8",
    "182b476350ccd8a77405f65433c75db88f52113466148296477a38d005bfed02",
})
_TAMANHOS_DAS_FRASES_REAIS = (2, 3, 4, 5, 6, 7, 8)
#: Marcas de linhas do log do jarvis: hora com segundos, numero da frase, texto ouvido.
_LINHA_DE_LOG = re.compile(r"\b\d{2}:\d{2}:\d{2}\b|frase #\d|\|\s*texto\b|\blogs/jarvis-", re.IGNORECASE)

_LIGACAO = re.compile(r"\]\(([^)\s]+)\)")
_URL_SOLTO = re.compile(r"\bhttps?://[^\s)>\]`|]+")
#: Caminhos privados: letra de disco (C:\, D:/), pastas de utilizador, a pasta pessoal
#: com til e as variaveis de ambiente do Windows que apontam para ela.
_CAMINHO_PRIVADO = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:[\\/]"
    r"|[\\/]Users[\\/]"
    r"|/home/"
    r"|(?<![\w.])~[\\/]"
    r"|%(?:USERPROFILE|HOMEPATH|APPDATA|LOCALAPPDATA)%",
    re.IGNORECASE,
)
#: Ficheiros de audio ou de conversa que nunca podem aparecer num documento publico.
_FICHEIRO_PRIVADO = re.compile(r"\.(?:wav|mp3|flac|ogg|webm|m4a)\b|\btranscri(?:pt|cao)s?/", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _ler(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8")


def _titulos(texto: str) -> list[tuple[int, str]]:
    """Titulos Markdown como (nivel, texto), pela ordem do documento."""
    return [(len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{1,6})\s+(.+)$", texto, re.MULTILINE)]


def _seccao(texto: str, titulo: str) -> str:
    """Corpo de uma seccao ate ao proximo titulo do mesmo nivel ou acima."""
    linhas = texto.splitlines()
    for i, linha in enumerate(linhas):
        m = re.match(r"^(#{1,6})\s+(.+)$", linha)
        if m and m.group(2).strip() == titulo:
            nivel = len(m.group(1))
            corpo = []
            for seguinte in linhas[i + 1:]:
                n = re.match(r"^(#{1,6})\s", seguinte)
                if n and len(n.group(1)) <= nivel:
                    break
                corpo.append(seguinte)
            return "\n".join(corpo)
    raise AssertionError(f"seccao em falta: {titulo!r}")


def _ligacoes(texto: str) -> list[str]:
    return _LIGACAO.findall(texto) + _URL_SOLTO.findall(texto)


def _linhas_da_tabela(corpo: str) -> list[list[str]]:
    """Celulas das linhas de dados das tabelas Markdown (sem cabecalho nem separador)."""
    linhas = [l.strip() for l in corpo.splitlines() if l.strip().startswith("|")]
    separador = re.compile(r"^\|[\s:|-]+\|$")
    dados = []
    for i, linha in enumerate(linhas):
        seguinte = linhas[i + 1] if i + 1 < len(linhas) else ""
        if separador.match(linha) or separador.match(seguinte):
            continue
        dados.append([c.strip() for c in linha.strip("|").split("|")])
    return dados


def frases_reais(texto: str) -> list[str]:
    """Sequencias de palavras do texto cujo resumo e o de uma frase real da sessao."""
    palavras = re.findall(r"[a-z0-9]+", texto.lower())
    achadas = []
    for n in _TAMANHOS_DAS_FRASES_REAIS:
        for i in range(len(palavras) - n + 1):
            janela = " ".join(palavras[i:i + n])
            if hashlib.sha256(janela.encode("utf-8")).hexdigest() in _FRASES_REAIS:
                achadas.append(janela)
    return achadas


def caminhos_privados(texto: str) -> list[str]:
    """Trechos que parecem caminhos de disco ou de utilizador."""
    return [m.group(0) for m in _CAMINHO_PRIVADO.finditer(texto)]


class TestDetetorDeCaminhos(unittest.TestCase):
    """O detetor apanha caminhos reais e deixa passar URLs e caminhos relativos."""

    def test_apanha_caminhos_privados(self):
        for amostra in (
            r"C:\Users\alguem\Desktop",
            "D:/repos/projeto",
            "/Users/alguem/code",
            "/home/alguem/code",
            "~/Code",
            r"%USERPROFILE%\Desktop",
            r"see c:\temp for logs",
        ):
            with self.subTest(amostra=amostra):
                self.assertTrue(caminhos_privados(amostra), amostra)

    def test_deixa_passar_urls_e_caminhos_relativos(self):
        for amostra in (
            "https://docs.livekit.io/agents/build/turns/",
            "[Kokoro](https://github.com/hexgrad/kokoro)",
            r".venv\Scripts\python -m unittest",
            "models/openwakeword/silero_vad.onnx",
            "docs/NATURALNESS.md",
        ):
            with self.subTest(amostra=amostra):
                self.assertEqual(caminhos_privados(amostra), [], amostra)


class TestNaturalidade(unittest.TestCase):
    """docs/NATURALNESS.md tem as seccoes, as comparacoes e as fontes pedidas."""

    @classmethod
    def setUpClass(cls):
        if not NATURALIDADE.is_file():
            raise AssertionError(f"documento em falta: {NATURALIDADE.relative_to(RAIZ)}")
        cls.texto = _ler(NATURALIDADE)
        cls.titulos = [t for _, t in _titulos(cls.texto)]

    def test_tem_as_seccoes_obrigatorias(self):
        for titulo in SECCOES + TECNICAS:
            with self.subTest(titulo=titulo):
                self.assertIn(titulo, self.titulos)

    def test_as_tecnicas_estao_dentro_de_techniques(self):
        corpo = _seccao(self.texto, "Techniques")
        for tecnica in TECNICAS:
            with self.subTest(tecnica=tecnica):
                self.assertRegex(corpo, rf"(?m)^###\s+{re.escape(tecnica)}\s*$")

    def test_cada_tecnica_compara_os_seis_sistemas_com_custo_e_fontes(self):
        for tecnica in TECNICAS:
            corpo = _seccao(self.texto, tecnica)
            with self.subTest(tecnica=tecnica):
                for sistema in SISTEMAS:
                    self.assertIn(sistema, corpo, f"{tecnica}: falta {sistema}")
                self.assertIn("Local and free here", corpo)
                self.assertRegex(corpo, r"Expected gain")
                self.assertRegex(corpo, r"\bCost\b|\bcost\b")
                self.assertTrue(
                    any(u.startswith("https://") for u in _ligacoes(corpo)),
                    f"{tecnica}: sem fonte https",
                )

    def test_fontes_sao_todas_https(self):
        ligacoes = _ligacoes(self.texto)
        externas = [u for u in ligacoes if "://" in u]
        self.assertGreaterEqual(len(set(externas)), 30)
        inseguras = [u for u in externas if not u.startswith("https://")]
        self.assertEqual(inseguras, [])
        self.assertNotIn("http://", self.texto)

    def test_fontes_primarias_de_cada_sistema(self):
        fontes = _seccao(self.texto, "Sources")
        for dominio in (
            "openai.com",
            "ai.google.dev",
            "aboutamazon.com",
            "pipecat.ai",
            "livekit.io",
            "home-assistant.io",
        ):
            with self.subTest(dominio=dominio):
                self.assertIn(dominio, fontes)

    def test_seccao_cloud_tem_precos_pressupostos_e_porque_nao(self):
        corpo = _seccao(self.texto, "Cloud speech-to-speech (information only)")
        for modelo in ("Gemini Live", "gpt-realtime", "gpt-realtime-mini"):
            self.assertIn(modelo, corpo)
        self.assertRegex(corpo, r"\$\d+(\.\d+)? per 1M tokens")
        self.assertIn("Assumptions", corpo)
        self.assertRegex(corpo, r"(?i)monthly estimate")
        self.assertRegex(corpo, r"(?i)not planned")
        self.assertRegex(corpo, r"(?i)free")
        self.assertRegex(corpo, r"(?i)leave the PC")

    def test_metas_numericas_com_a_voz_real(self):
        corpo = _seccao(self.texto, "Numeric targets")
        self.assertRegex(corpo, r"(?i)real voice")
        self.assertRegex(corpo, r"(?i)synthetic voices .* never count")
        for meta in ("16 of 20", "1.0 s", "1.6 s", "3.5 s", "6 s", "1.2 s", "300 ms"):
            with self.subTest(meta=meta):
                self.assertIn(meta, corpo)

    def test_cada_mudanca_aponta_uma_metrica(self):
        corpo = _seccao(self.texto, "Planned changes and the metric each moves")
        linhas = [l for l in corpo.splitlines() if re.match(r"^\|\s*\d+\s*\|", l)]
        self.assertGreaterEqual(len(linhas), 13)
        for linha in linhas:
            celulas = [c.strip() for c in linha.strip().strip("|").split("|")]
            with self.subTest(linha=celulas[0]):
                self.assertEqual(len(celulas), 3)
                self.assertTrue(celulas[1])
                self.assertTrue(celulas[2])


class TestNadaPrivadoNosDocumentos(unittest.TestCase):
    """Nem caminhos de disco ou de utilizador, nem audio, transcricoes ou e-mails."""

    def test_sem_caminhos_privados(self):
        for caminho in (NATURALIDADE, ROADMAP, CEREBRO):
            with self.subTest(documento=caminho.name):
                self.assertEqual(caminhos_privados(_ler(caminho)), [])

    def test_sem_audio_transcricoes_nem_emails(self):
        for caminho in (NATURALIDADE, ROADMAP, CEREBRO):
            texto = _ler(caminho)
            with self.subTest(documento=caminho.name):
                self.assertIsNone(_FICHEIRO_PRIVADO.search(texto))
                self.assertIsNone(_EMAIL.search(texto))

    def test_cerebro_sem_frases_reais_nem_linhas_de_log(self):
        texto = _ler(CEREBRO)
        self.assertEqual(frases_reais(texto), [])
        self.assertEqual(_LINHA_DE_LOG.findall(texto), [])


class TestDetetorDeTranscricoes(unittest.TestCase):
    """O detetor apanha as frases reais da sessao e as linhas do log, e mais nada."""

    def test_apanha_uma_frase_real_mesmo_com_outra_pontuacao(self):
        # Montada por partes para o texto da frase real nao ficar escrito seguido.
        frase = " ".join(("Crypto", "-", "Radar"))
        self.assertEqual(frases_reais(f"On {frase}, sessions."), ["crypto radar"])

    def test_deixa_passar_parafrases(self):
        for amostra in (
            "A social greeting came back as the status of a project.",
            "A request for help learning to cook, then a short follow-up.",
        ):
            with self.subTest(amostra=amostra):
                self.assertEqual(frases_reais(amostra), [])

    def test_apanha_linhas_do_log(self):
        for amostra in ("12:03:44 | texto: hello", "frase #7 | resposta falada", "see logs/jarvis-2026.log"):
            with self.subTest(amostra=amostra):
                self.assertTrue(_LINHA_DE_LOG.search(amostra), amostra)
        self.assertIsNone(_LINHA_DE_LOG.search("median at most 1.5 s, 95th percentile at most 6 s"))


class TestCerebro(unittest.TestCase):
    """docs/BRAIN.md tem as seccoes, as tabelas, as metas e as fontes pedidas."""

    @classmethod
    def setUpClass(cls):
        if not CEREBRO.is_file():
            raise AssertionError(f"documento em falta: {CEREBRO.relative_to(RAIZ)}")
        cls.texto = _ler(CEREBRO)
        cls.titulos = [t for n, t in _titulos(cls.texto) if n == 2]

    def test_tem_as_seccoes_obrigatorias_como_seccoes_de_topo(self):
        for titulo in SECCOES_DO_CEREBRO:
            with self.subTest(titulo=titulo):
                self.assertIn(titulo, self.titulos)

    def test_esta_em_ingles(self):
        # Sem o codigo entre acentos graves, onde os identificadores sao em portugues,
        # nem os URLs, onde "com" e um dominio.
        prosa = re.sub(r"`[^`]*`|https://\S+", " ", self.texto).lower()
        palavras = re.findall(r"[a-zà-ú]+", prosa)
        ingles = sum(palavras.count(p) for p in ("the", "and", "is", "with", "of"))
        portugues = sum(palavras.count(p) for p in ("que", "não", "nao", "uma", "com", "para", "é"))
        self.assertGreater(ingles, 200)
        self.assertLess(portugues, ingles / 50)

    def test_problemas_da_sessao_sao_parafraseados(self):
        corpo = _seccao(self.texto, "Problems seen in the 2026-09-27 session")
        self.assertRegex(corpo, r"(?i)paraphrased")
        self.assertGreaterEqual(len(_linhas_da_tabela(corpo)), 5)

    def test_ligar_o_cerebro_compara_as_tres_opcoes_com_custo_e_fontes(self):
        corpo = _seccao(self.texto, "Connecting the brain")
        for opcao in ("stream-json", "Agent SDK", "Messages API"):
            with self.subTest(opcao=opcao):
                self.assertIn(opcao, corpo)
        self.assertRegex(corpo, r"(?i)subscription")
        self.assertRegex(corpo, r"\$\d+(\.\d+)? per 1M")
        self.assertRegex(corpo, r"(?i)\bpaid\b")
        self.assertIn("--bare", corpo)
        self.assertTrue(any(u.startswith("https://") for u in _ligacoes(corpo)))

    def test_modelo_por_omissao(self):
        corpo = _seccao(self.texto, "Default model")
        self.assertIn("claude-haiku-4-5", corpo)
        self.assertRegex(corpo, r"(?i)sonnet")
        self.assertIn("medir_cerebro", corpo)

    def test_tabela_das_ferramentas(self):
        corpo = _seccao(self.texto, "Tools exposed to the brain")
        self.assertRegex(corpo, r'(?m)^\|\s*Tool\s*\|\s*Effect\s*\|\s*Needs a spoken "yes"\s*\|\s*What it returns')
        linhas = {c[0].strip("`"): c for c in _linhas_da_tabela(corpo)}
        for nome, celulas in linhas.items():
            with self.subTest(ferramenta=nome):
                self.assertEqual(len(celulas), 4)
                self.assertTrue(all(celulas))
        for nome in FERRAMENTAS_COM_EFEITO:
            with self.subTest(com_efeito=nome):
                self.assertIn(nome, linhas)
                self.assertRegex(linhas[nome][2], r"(?i)\byes\b")
                self.assertIn("awaiting_spoken_yes", linhas[nome][3])
        for nome in FERRAMENTAS_DE_LEITURA:
            with self.subTest(leitura=nome):
                self.assertIn(nome, linhas)
                self.assertRegex(linhas[nome][2], r"(?i)^no$")
        self.assertEqual(set(linhas), set(FERRAMENTAS_COM_EFEITO) | set(FERRAMENTAS_DE_LEITURA))
        for proibido in ("buy", "sell", "trade", "bash", "shell"):
            with self.subTest(proibido=proibido):
                self.assertFalse(any(proibido in nome.lower() for nome in linhas))

    def test_regras_de_seguranca(self):
        corpo = _seccao(self.texto, "Safety rules")
        self.assertRegex(corpo, r'(?i)spoken "yes"')
        self.assertRegex(corpo, r"(?i)no buy or sell orders by voice")
        self.assertRegex(corpo, r"(?i)never reaches the model")
        for regra in ("--safe-mode", "--restricted", "--strict-mcp-config"):
            self.assertIn(regra, corpo)

    def test_orcamento_de_latencia(self):
        corpo = _seccao(self.texto, "Latency budget")
        self.assertIn("1.5 s", corpo)
        self.assertRegex(corpo, r"(?i)median")
        linhas = _linhas_da_tabela(corpo)
        self.assertGreaterEqual(len(linhas), 5)
        orcamentos = [c[1] for c in linhas if not c[0].startswith("**")]
        total_ms = sum(float(re.match(r"(\d+(?:\.\d+)?) ms", o).group(1)) for o in orcamentos)
        self.assertLessEqual(total_ms, 1500)

    def test_contexto_e_sessoes_longas(self):
        corpo = _seccao(self.texto, "Context and long sessions")
        self.assertIn("16,000", corpo)
        self.assertIn("120 words", corpo)
        self.assertIn("100,000", corpo)
        self.assertRegex(corpo, r"(?i)nothing is written to disk")

    def test_fila_de_avisos_e_classificador(self):
        self.assertRegex(_seccao(self.texto, "Notices queue"), r"(?i)never spoken while")
        corpo = _seccao(self.texto, "Fate of the local qwen3:8b classifier")
        self.assertRegex(corpo, r"(?i)fallback")
        self.assertRegex(corpo, r"(?i)not warmed at startup")
        self.assertRegex(corpo, r"(?i)claude -p.{0,80}removed")

    def test_custo_e_teto_garantido(self):
        corpo = _seccao(self.texto, "Token cost per exchange and guaranteed ceiling")
        self.assertIn("4 × 32,000 = 128,000 input tokens per exchange", corpo)
        self.assertIn("496", corpo)
        self.assertRegex(corpo, r"(?i)at most 2 web uses")
        self.assertRegex(corpo, r"(?i)usage credits")

    def test_metas_numericas(self):
        corpo = _seccao(self.texto, "Numeric targets")
        self.assertRegex(corpo, r"(?i)real voice")
        self.assertIn("sessao_naturalidade.py", corpo)
        for meta in ("16 of 20", "1.5 s", "3.5 s", "6 s", "128,000"):
            with self.subTest(meta=meta):
                self.assertIn(meta, corpo)

    def test_cada_mudanca_aponta_uma_metrica(self):
        corpo = _seccao(self.texto, "Planned changes and the metric each moves")
        linhas = [c for c in _linhas_da_tabela(corpo) if re.match(r"^\d+$", c[0])]
        self.assertEqual([int(c[0]) for c in linhas], list(range(1, 8)))
        for celulas in linhas:
            with self.subTest(linha=celulas[0]):
                self.assertEqual(len(celulas), 3)
                self.assertTrue(celulas[1])
                self.assertTrue(celulas[2])

    def test_fontes_sao_https_e_primarias(self):
        externas = [u for u in _ligacoes(self.texto) if "://" in u]
        self.assertGreaterEqual(len(set(externas)), 10)
        self.assertNotIn("http://", self.texto)
        for url in externas:
            with self.subTest(url=url):
                self.assertTrue(url.startswith(FONTES_PRIMARIAS), url)
        fontes = _seccao(self.texto, "Sources")
        for fonte in (
            "https://code.claude.com/docs/en/headless",
            "https://code.claude.com/docs/en/cli-reference",
            "https://code.claude.com/docs/en/agent-sdk/overview",
            "https://github.com/anthropics/claude-agent-sdk-python",
            "https://platform.claude.com/docs/en/about-claude/pricing",
        ):
            with self.subTest(fonte=fonte):
                self.assertIn(fonte, fontes)

    def test_fontes_da_seccao_sources_so_https(self):
        fontes = _seccao(self.texto, "Sources")
        ligacoes = _ligacoes(fontes)
        self.assertTrue(ligacoes)
        self.assertTrue(all(u.startswith("https://") for u in ligacoes))


class TestLigacoes(unittest.TestCase):
    """O ROADMAP e o README apontam para a pesquisa e para o desenho do cerebro."""

    def test_roadmap_aponta_para_a_naturalidade(self):
        self.assertIn("NATURALNESS.md", _LIGACAO.findall(_ler(ROADMAP)))

    def test_readme_aponta_para_a_naturalidade(self):
        self.assertIn("docs/NATURALNESS.md", _LIGACAO.findall(_ler(README)))

    def test_roadmap_aponta_para_o_cerebro(self):
        self.assertIn("BRAIN.md", _LIGACAO.findall(_ler(ROADMAP)))

    def test_readme_aponta_para_o_cerebro(self):
        self.assertIn("docs/BRAIN.md", _LIGACAO.findall(_ler(README)))


if __name__ == "__main__":
    unittest.main()
