r"""Os documentos publicos da naturalidade estao completos e nao expoem nada privado.

docs/NATURALNESS.md e a pesquisa e o plano para o jarvis soar natural. O teste garante
que tem as seccoes pedidas (uma por tecnica, cada uma com os seis sistemas comparados,
o que corre local e de graca, o ganho e o custo), a seccao informativa sobre voz na
cloud, as metas numericas e a tabela das mudancas, que as fontes sao https e que nem
esse documento nem o docs/ROADMAP.md tem caminhos de disco ou de utilizador. Tambem
confirma que o ROADMAP e o README apontam para ele.

Corre com:

    .venv\Scripts\python -m unittest tests.test_documentos_publicos -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NATURALIDADE = RAIZ / "docs" / "NATURALNESS.md"
ROADMAP = RAIZ / "docs" / "ROADMAP.md"
README = RAIZ / "README.md"

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
        for caminho in (NATURALIDADE, ROADMAP):
            with self.subTest(documento=caminho.name):
                self.assertEqual(caminhos_privados(_ler(caminho)), [])

    def test_sem_audio_transcricoes_nem_emails(self):
        for caminho in (NATURALIDADE, ROADMAP):
            texto = _ler(caminho)
            with self.subTest(documento=caminho.name):
                self.assertIsNone(_FICHEIRO_PRIVADO.search(texto))
                self.assertIsNone(_EMAIL.search(texto))


class TestLigacoes(unittest.TestCase):
    """O ROADMAP e o README apontam para a pesquisa."""

    def test_roadmap_aponta_para_a_naturalidade(self):
        self.assertIn("NATURALNESS.md", _LIGACAO.findall(_ler(ROADMAP)))

    def test_readme_aponta_para_a_naturalidade(self):
        self.assertIn("docs/NATURALNESS.md", _LIGACAO.findall(_ler(README)))


if __name__ == "__main__":
    unittest.main()
