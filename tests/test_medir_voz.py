r"""Testes do arnes de medicao sintetica (scripts/medir_voz.py), unittest da
biblioteca padrao (sem pytest, mesma convencao de tests/test_router.py e
tests/test_acoes.py: nenhuma framework de testes fora da biblioteca padrao).

NENHUM destes testes toca em GPU, em Piper ou no disco de audio: cobrem so as
partes puras do arnes — o calculo do WER, a substituicao dos marcadores de
projeto (<projeto-1>/<projeto-2>) e a leitura da tabela de
tests/voz/frases-pt.md. A cadeia inteira (sintese + transcricao + encaminhador)
so se prova a serio com hardware, correndo
`.venv\Scripts\python scripts/medir_voz.py`.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from scripts import medir_voz  # noqa: E402


@contextlib.contextmanager
def pasta_de_evidencia_temporaria():
    """Redireciona a pasta de evidencia para um tempfile, durante um teste.

    Sem isto os testes que exercitam `escrever_evidencia` escreviam ficheiros
    dentro da pasta de evidencia REAL do repositorio: colidem com uma corrida do arnes a decorrer e deixam lixo se o
    processo for morto a meio. `caminho_evidencia_de_saida` le as duas
    constantes no momento da chamada, por isso troca-las aqui chega — e a
    validacao que protege a pasta real continua exatamente a mesma, so aponta
    para outro sitio enquanto o teste corre.
    """
    with tempfile.TemporaryDirectory() as pasta:
        raiz_falsa = Path(pasta).resolve()
        evidencia = raiz_falsa / "docs" / "forja" / "evidence"
        evidencia.mkdir(parents=True)
        pasta_original = medir_voz.PASTA_EVIDENCIA_PADRAO
        raiz_original = medir_voz.RAIZ
        medir_voz.PASTA_EVIDENCIA_PADRAO = evidencia
        medir_voz.RAIZ = raiz_falsa
        try:
            yield evidencia
        finally:
            medir_voz.PASTA_EVIDENCIA_PADRAO = pasta_original
            medir_voz.RAIZ = raiz_original


class TestCalcularWer(unittest.TestCase):
    """WER = distancia de edicao (Levenshtein) ao nivel da palavra / N da
    referencia. Valores esperados calculados a mao, nao re-derivados da
    implementacao."""

    def test_frases_identicas_tem_wer_zero(self) -> None:
        resultado = medir_voz.calcular_wer("que horas são", "que horas são")
        self.assertEqual(resultado.wer, 0.0)
        self.assertEqual(resultado.distancia_edicao, 0)
        self.assertEqual(resultado.n_palavras_referencia, 3)

    def test_uma_substituicao_em_quatro_palavras_da_25_por_cento(self) -> None:
        # "são" -> "sao": 1 substituicao sobre 4 palavras de referencia = 25%.
        resultado = medir_voz.calcular_wer("que horas são agora", "que horas sao agora")
        self.assertEqual(resultado.wer, 0.25)
        self.assertEqual(resultado.distancia_edicao, 1)
        self.assertEqual(resultado.n_palavras_referencia, 4)

    def test_uma_insercao_conta_como_um_erro(self) -> None:
        # hipotese tem uma palavra extra: 1 insercao sobre 2 palavras de ref.
        resultado = medir_voz.calcular_wer("cala-te", "cala-te agora")
        self.assertEqual(resultado.distancia_edicao, 1)
        self.assertEqual(resultado.wer, 0.5)

    def test_uma_delecao_conta_como_um_erro(self) -> None:
        # hipotese perdeu uma palavra: 1 delecao sobre 3 palavras de ref.
        resultado = medir_voz.calcular_wer("que horas são", "que são")
        self.assertEqual(resultado.distancia_edicao, 1)
        self.assertAlmostEqual(resultado.wer, 1 / 3)

    def test_hipotese_totalmente_diferente_da_a_distancia_do_maior(self) -> None:
        resultado = medir_voz.calcular_wer("acorda", "obrigado por assistir")
        self.assertEqual(resultado.n_palavras_referencia, 1)
        self.assertEqual(resultado.distancia_edicao, 3)  # substitui + 2 insercoes
        self.assertEqual(resultado.wer, 3.0)

    def test_referencia_vazia_e_hipotese_vazia_e_wer_zero(self) -> None:
        resultado = medir_voz.calcular_wer("", "")
        self.assertEqual(resultado.wer, 0.0)
        self.assertEqual(resultado.n_palavras_referencia, 0)

    def test_referencia_vazia_e_hipotese_com_texto_e_wer_maximo(self) -> None:
        resultado = medir_voz.calcular_wer("", "obrigado por assistir")
        self.assertEqual(resultado.wer, 1.0)
        self.assertEqual(resultado.n_palavras_referencia, 0)
        self.assertEqual(resultado.n_palavras_hipotese, 3)

    def test_pontuacao_e_maiusculas_sao_ignoradas(self) -> None:
        # WER compara palavras, nao pontuacao nem caixa: "SÃO?!" == "são".
        resultado = medir_voz.calcular_wer("QUE HORAS SÃO?!", "que horas são")
        self.assertEqual(resultado.wer, 0.0)

    def test_frase_do_criterio_da_t7_bate_com_o_valor_calculado_a_mao(self) -> None:
        # "abre o vs code no exemplo-um" (5 palavras apos normalizar o hifen)
        # contra uma transcricao com uma palavra a menos e outra trocada.
        resultado = medir_voz.calcular_wer(
            "abre o vs code no exemplo-um", "abre vs code no exemplo um"
        )
        # referencia: [abre, o, vs, code, no, exemplo, um] (7 palavras, hifen
        # vira espaco); hipotese: [abre, vs, code, no, exemplo, um] (6
        # palavras) — falta o "o": 1 delecao.
        self.assertEqual(resultado.n_palavras_referencia, 7)
        self.assertEqual(resultado.distancia_edicao, 1)
        self.assertAlmostEqual(resultado.wer, 1 / 7)


class TestSubstituirMarcadores(unittest.TestCase):
    """<projeto-1>/<projeto-2>: nunca um nome real do utilizador aqui."""

    def test_substitui_os_dois_marcadores(self) -> None:
        resultado = medir_voz.substituir_marcadores(
            "abre o vs code no <projeto-1> e a pasta do <projeto-2>",
            ["exemplo-um", "exemplo-dois"],
        )
        self.assertEqual(resultado, "abre o vs code no exemplo-um e a pasta do exemplo-dois")

    def test_frase_sem_marcadores_fica_igual(self) -> None:
        resultado = medir_voz.substituir_marcadores("cala-te", ["exemplo-um", "exemplo-dois"])
        self.assertEqual(resultado, "cala-te")

    def test_um_so_projeto_configurado_reutiliza_o_mesmo_nome(self) -> None:
        # Degradar, nunca rebentar: com um so projeto, os dois
        # marcadores usam o mesmo nome em vez de recusar a frase.
        resultado = medir_voz.substituir_marcadores(
            "abre o vs code no <projeto-1> e a pasta do <projeto-2>",
            ["exemplo-unico"],
        )
        self.assertEqual(resultado, "abre o vs code no exemplo-unico e a pasta do exemplo-unico")

    def test_sem_projeto_nenhum_e_sem_marcador_na_frase_passa_tal_e_qual(self) -> None:
        resultado = medir_voz.substituir_marcadores("que horas são", [])
        self.assertEqual(resultado, "que horas são")

    def test_sem_projeto_nenhum_e_com_marcador_levanta_erro_legivel(self) -> None:
        with self.assertRaises(medir_voz.AmostraError):
            medir_voz.substituir_marcadores("abre o vs code no <projeto-1>", [])


class TestLerAmostra(unittest.TestCase):
    """A tabela de tests/voz/frases-pt.md: 20 linhas, 10 locais + 10 claude."""

    def test_a_amostra_versionada_tem_vinte_frases(self) -> None:
        frases = medir_voz.ler_amostra(medir_voz.CAMINHO_AMOSTRA_PADRAO)
        self.assertEqual(len(frases), 20)
        self.assertEqual([f.numero for f in frases], list(range(1, 21)))

    def test_a_amostra_tem_dez_locais_e_dez_para_o_claude(self) -> None:
        frases = medir_voz.ler_amostra(medir_voz.CAMINHO_AMOSTRA_PADRAO)
        locais = [f for f in frases if f.tipo_documentado == "local"]
        claude = [f for f in frases if f.tipo_documentado == "claude"]
        self.assertEqual(len(locais), 10)
        self.assertEqual(len(claude), 10)

    def test_nenhuma_frase_da_amostra_versionada_leva_um_caminho_do_disco(self) -> None:
        # D1/D10: a garantia e por comportamento, nunca por uma lista de
        # nomes proibidos em codigo — aqui so se verifica a forma (nenhum
        # caminho absoluto do Windows), nunca um nome especifico de projeto.
        texto_do_ficheiro = medir_voz.CAMINHO_AMOSTRA_PADRAO.read_text(encoding="utf-8")
        self.assertNotRegex(texto_do_ficheiro, r"[A-Za-z]:[\\/]")
        self.assertNotIn("Users", texto_do_ficheiro)

    def test_ficheiro_inexistente_levanta_erro_legivel(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "nao-existe.md"
            with self.assertRaises(medir_voz.AmostraError):
                medir_voz.ler_amostra(caminho)

    def test_ficheiro_sem_tabela_levanta_erro_legivel(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "vazio.md"
            caminho.write_text("# nada aqui, so prosa\n", encoding="utf-8")
            with self.assertRaises(medir_voz.AmostraError):
                medir_voz.ler_amostra(caminho)

    def test_le_uma_tabela_minima_corretamente(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "amostra.md"
            caminho.write_text(
                "# titulo\n\n"
                "| nº | tipo | frase (com marcadores) | intenção esperada |\n"
                "|----|------|--------------------------|--------------------|\n"
                "| 1 | local | que horas são | horas_e_data (horas) |\n"
                "| 2 | claude | achas que vai chover | texto (pergunta comum) |\n",
                encoding="utf-8",
            )
            frases = medir_voz.ler_amostra(caminho)
            self.assertEqual(len(frases), 2)
            self.assertEqual(frases[0].tipo_documentado, "local")
            self.assertEqual(frases[0].frase_com_marcadores, "que horas são")
            self.assertEqual(frases[1].tipo_documentado, "claude")


class TestEsperadoUsaOEncaminhadorComoFonteDaVerdade(unittest.TestCase):
    """A intencao ESPERADA de cada frase e sempre calculada por
    jarvis.router.encaminhar() sobre a frase escrita, nunca um valor fixado a
    mao — evita que a amostra fique dessincronizada do router de verdade."""

    def setUp(self) -> None:
        from jarvis.config import Config, Projeto

        self.config = Config(
            microfone="Microfone de Teste",
            projetos=(
                Projeto(nome="exemplo-um", caminho=Path("D:/caminho/para/exemplo-um")),
                Projeto(nome="exemplo-dois", caminho=Path("D:/caminho/para/exemplo-dois")),
            ),
        )

    def test_cada_frase_da_amostra_produz_uma_intencao_esperada_consistente_com_o_tipo_documentado(
        self,
    ) -> None:
        from jarvis.router import encaminhar

        frases = medir_voz.ler_amostra(medir_voz.CAMINHO_AMOSTRA_PADRAO)
        nomes_projetos = [p.nome for p in self.config.projetos]
        for frase in frases:
            frase_esperada = medir_voz.substituir_marcadores(frase.frase_com_marcadores, nomes_projetos)
            esperado = encaminhar(frase_esperada, self.config)
            self.assertEqual(
                esperado.tipo,
                frase.tipo_documentado,
                msg=(
                    f"frase {frase.numero} ({frase.frase_com_marcadores!r}): a tabela diz "
                    f"'{frase.tipo_documentado}' mas o router devolveu '{esperado.tipo}' "
                    f"({esperado.motivo})"
                ),
            )


class TestCaminhoEvidenciaDeSaida(unittest.TestCase):
    """Regressao de seguranca: o `--saida` escrevia em qualquer caminho do
    disco. A evidencia leva os NOMES e os CAMINHOS reais dos projetos do
    utilizador e este repositorio e publico, por isso so pode cair na pasta
    de evidencia e so com sufixo .md."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name).resolve()
        self.pasta = self.raiz / "docs" / "forja" / "evidence"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def validar(self, valor: str) -> Path:
        return medir_voz.caminho_evidencia_de_saida(valor, self.pasta, self.raiz)

    def recusa(self, valor: str) -> bool:
        try:
            self.validar(valor)
        except ValueError:
            return True
        return False

    def test_caminho_relativo_dentro_da_pasta_permitida_e_aceite(self) -> None:
        self.assertEqual(
            self.validar("docs/forja/evidence/medicao.md"),
            self.pasta / "medicao.md",
        )

    def test_caminho_absoluto_dentro_da_pasta_permitida_e_aceite(self) -> None:
        alvo = self.pasta / "sub" / "medicao.md"
        self.assertEqual(self.validar(str(alvo)), alvo)

    def test_sufixo_md_aceite_sem_distinguir_maiusculas(self) -> None:
        self.assertEqual(
            self.validar("docs/forja/evidence/M.MD"),
            self.pasta / "M.MD",
        )

    def test_pasta_versionada_do_repositorio_e_recusada(self) -> None:
        # Tres exemplos da revisao de seguranca: caem dentro do repo, mas em
        # pastas que o .gitignore NAO cobre.
        self.assertTrue(self.recusa("docs/EVIDENCIA.md"))
        self.assertTrue(self.recusa("README.md"))
        self.assertTrue(self.recusa("tests/voz/resultado.md"))

    def test_docs_forja_fora_da_pasta_de_evidencia_e_recusado(self) -> None:
        self.assertTrue(self.recusa("docs/forja/notas.md"))

    def test_caminho_fora_do_repositorio_e_recusado(self) -> None:
        # A sonda da revisao de seguranca: escrever ao lado da raiz.
        self.assertTrue(self.recusa(str(self.raiz.parent / "FUGA-probe.md")))

    def test_travessia_com_dois_pontos_e_recusada(self) -> None:
        self.assertTrue(self.recusa("docs/forja/evidence/../../../fuga.md"))

    def test_sufixo_que_o_gitignore_nao_apanha_e_recusado(self) -> None:
        self.assertTrue(self.recusa("docs/forja/evidence/notas.txt"))
        self.assertTrue(self.recusa("docs/forja/evidence/sem-sufixo"))

    def test_nenhuma_recusa_criou_seja_o_que_for_no_disco(self) -> None:
        for valor in (
            "README.md",
            "docs/EVIDENCIA.md",
            str(self.raiz.parent / "FUGA-probe.md"),
            "docs/forja/evidence/notas.txt",
        ):
            with self.assertRaises(ValueError):
                self.validar(valor)
        self.assertEqual(sorted(item.name for item in self.raiz.iterdir()), [])

    def test_a_pasta_por_omissao_e_docs_forja_evidence_do_repositorio(self) -> None:
        self.assertEqual(
            medir_voz.PASTA_EVIDENCIA_PADRAO,
            medir_voz.RAIZ / "docs" / "forja" / "evidence",
        )

    def test_escrever_evidencia_recusa_um_caminho_fora_da_pasta(self) -> None:
        # A validacao nao vive so no main(): quem chamar a funcao diretamente
        # (como a sonda da revisao de seguranca fez) tambem e recusado.
        alvo = medir_voz.RAIZ / "FUGA-probe.md"
        with self.assertRaises(ValueError):
            medir_voz.escrever_evidencia([], "exemplo", "cpu", alvo, 0.0)
        self.assertFalse(alvo.exists())


class TestMainRecusaSaidaInvalida(unittest.TestCase):
    """O codigo de saida e o efeito no disco, nao so a excecao: um `--saida`
    fora da pasta de evidencia falha com 1 ANTES de sintetizar seja o que
    for (nao ha GPU nem Piper envolvidos neste teste)."""

    def test_saida_em_ficheiro_versionado_devolve_1_e_nao_toca_no_ficheiro(self) -> None:
        readme = medir_voz.RAIZ / "README.md"
        antes = readme.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            codigo = medir_voz.main(["--saida", "README.md"])
        self.assertEqual(codigo, 1)
        self.assertIn("FALHOU", stderr.getvalue())
        self.assertEqual(readme.read_bytes(), antes)

    def test_saida_fora_do_repositorio_devolve_1_e_nao_escreve_nada(self) -> None:
        # Destino fora do repositorio, mas dentro de um tempfile deste teste:
        # a versao anterior apontava para `RAIZ.parent`, uma pasta do disco do
        # utilizador que este teste nao controla.
        with tempfile.TemporaryDirectory() as pasta:
            alvo = Path(pasta).resolve() / "FUGA-probe.md"
            self.assertFalse(alvo.exists(), "sonda: o ficheiro nao pode existir antes do teste")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                codigo = medir_voz.main(["--saida", str(alvo)])
            self.assertEqual(codigo, 1)
            self.assertFalse(alvo.exists())

    def test_saida_sem_sufixo_md_devolve_1(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            codigo = medir_voz.main(["--saida", "docs/forja/evidence/fuga.txt"])
        self.assertEqual(codigo, 1)


class TestCelulasDeTabelaMarkdown(unittest.TestCase):
    """Uma transcricao com `|` deslocava as colunas
    e falsificava a coluna de acerto que um humano le."""

    def test_pipe_na_transcricao_e_escapado(self) -> None:
        self.assertEqual(
            medir_voz.celula_markdown("isto | NAO | sim | 0.0% | lixo"),
            r"isto \| NAO \| sim \| 0.0% \| lixo",
        )

    def test_mudancas_de_linha_sao_achatadas(self) -> None:
        self.assertEqual(medir_voz.celula_markdown("uma\nduas\r\ntres"), "uma duas tres")

    def test_texto_vazio_nao_produz_celula_vazia(self) -> None:
        self.assertEqual(medir_voz.celula_markdown(""), "—")

    def test_uma_linha_escrita_com_pipes_na_transcricao_continua_a_ter_onze_colunas(self) -> None:
        # O ataque ja provado, agora de ponta a ponta: a linha
        # escrita tem de continuar a ter as colunas do cabecalho, com o
        # `acerto` na 7.a e o WER na 8.a (a duracao do audio e a latencia da
        # transcricao entraram depois; a coluna da LINGUA detetada tambem, e
        # por isso sao 11 e nao 10).
        linha = medir_voz.LinhaMedida(
            numero=1,
            tipo_documentado="local",
            frase_esperada="que horas são",
            transcricao="isto | NAO | sim | 0.0% | lixo",
            tipo_esperado="local",
            nome_acao_esperado="horas_e_data",
            argumento_esperado="horas",
            tipo_obtido="claude",
            nome_acao_obtido=None,
            argumento_obtido=None,
            acertou_intencao=False,
            wer=medir_voz.calcular_wer("que horas são", "isto | NAO | sim | 0.0% | lixo"),
            duracao_audio_s=1.25,
            latencia_transcricao_ms=432.0,
            latencia_total_ms=5432.0,
            lingua="pt",
            lingua_probabilidade=0.97,
        )
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-colunas-medir-voz.md"
            medir_voz.escrever_evidencia([linha], "exemplo", "cpu", caminho, 0.0)
            escrito = caminho.read_text(encoding="utf-8")
        linha_da_frase = [
            bruta
            for bruta in escrito.splitlines()
            if bruta.startswith("| 1 ")
        ]
        self.assertEqual(len(linha_da_frase), 1)
        celulas = medir_voz.dividir_celulas(linha_da_frase[0])
        self.assertEqual(len(celulas), 11)
        self.assertEqual(celulas[0], "1")
        self.assertEqual(celulas[3], "isto | NAO | sim | 0.0% | lixo")
        self.assertEqual(celulas[6], "NAO")
        self.assertTrue(celulas[7].endswith("%"))
        # A lingua detetada nesta frase, na sua propria coluna.
        self.assertEqual(celulas[8], "pt 0.97")
        # A duracao do audio e a latencia SO da transcricao
        # (nao o total com o carregamento) chegam mesmo ao ficheiro.
        self.assertEqual(celulas[9], "1.25")
        self.assertEqual(celulas[10], "432")

    def test_dividir_celulas_ignora_prosa(self) -> None:
        self.assertIsNone(medir_voz.dividir_celulas("isto nao e uma tabela"))

    def test_dividir_celulas_desescapa_os_pipes(self) -> None:
        self.assertEqual(
            medir_voz.dividir_celulas(r"| a \| b | c |"),
            ["a | b", "c"],
        )


class TestAvisosDaAmostra(unittest.TestCase):
    """Uma linha mal formada desaparecia em silencio."""

    def escrever_amostra(self, pasta: str, corpo: str) -> Path:
        caminho = Path(pasta) / "amostra.md"
        caminho.write_text(
            "| nº | tipo | frase (com marcadores) | intenção esperada |\n"
            "|----|------|--------------------------|--------------------|\n" + corpo,
            encoding="utf-8",
        )
        return caminho

    def test_linha_com_tipo_errado_e_avisada_e_nao_medida(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = self.escrever_amostra(
                pasta,
                "| 1 | local | que horas são | horas_e_data (horas) |\n"
                "| 2 | lokal | cala-te | calar |\n",
            )
            avisos: list[str] = []
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                frases = medir_voz.ler_amostra(caminho, avisos=avisos)
            self.assertEqual(len(frases), 1)
            self.assertTrue(avisos, "uma linha ignorada tem de produzir um aviso")
            self.assertIn("linha 4", " ".join(avisos))
            self.assertIn("AVISO", stderr.getvalue())

    def test_linha_com_colunas_a_mais_e_avisada(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = self.escrever_amostra(
                pasta,
                "| 1 | local | que horas são | horas_e_data (horas) |\n"
                "| 2 | local | frase | com | colunas a mais |\n",
            )
            avisos: list[str] = []
            with contextlib.redirect_stderr(io.StringIO()):
                frases = medir_voz.ler_amostra(caminho, avisos=avisos)
            self.assertEqual(len(frases), 1)
            self.assertTrue(avisos)

    def test_linha_de_celulas_todas_vazias_e_avisada_e_nao_passa_por_separador(self) -> None:
        # `| | | |` nao tem celula nenhuma com
        # texto, logo o `all(...)` do teste de separador dava True por vacuidade
        # e a linha sumia sem entrar na contagem de avisos.
        with tempfile.TemporaryDirectory() as pasta:
            caminho = self.escrever_amostra(
                pasta,
                "| 1 | local | que horas são | horas_e_data (horas) |\n"
                "| | | | |\n",
            )
            avisos: list[str] = []
            with contextlib.redirect_stderr(io.StringIO()):
                frases = medir_voz.ler_amostra(caminho, avisos=avisos)
            self.assertEqual(len(frases), 1)
            self.assertTrue(avisos, "a linha vazia tem de ser avisada")
            self.assertIn("1 linha(s) de tabela que NAO foram medidas", avisos[0])
            self.assertIn("linha 4", " ".join(avisos))

    def test_a_linha_separadora_do_cabecalho_continua_a_passar_em_silencio(self) -> None:
        # O contrario do teste anterior: `|----|----|` tem celulas com texto e
        # continua a ser ignorada sem aviso nenhum.
        with tempfile.TemporaryDirectory() as pasta:
            caminho = self.escrever_amostra(
                pasta, "| 1 | local | que horas são | horas_e_data (horas) |\n"
            )
            avisos: list[str] = []
            with contextlib.redirect_stderr(io.StringIO()):
                frases = medir_voz.ler_amostra(caminho, avisos=avisos)
            self.assertEqual(len(frases), 1)
            self.assertEqual(avisos, [])

    def test_a_amostra_versionada_nao_produz_nenhum_aviso(self) -> None:
        avisos: list[str] = []
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            frases = medir_voz.ler_amostra(medir_voz.CAMINHO_AMOSTRA_PADRAO, avisos=avisos)
        self.assertEqual(len(frases), 20)
        self.assertEqual(avisos, [])
        self.assertEqual(stderr.getvalue(), "")

    def test_frase_com_pipe_escapado_e_lida_de_volta_inteira(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = self.escrever_amostra(
                pasta, r"| 1 | claude | diz a \| ao claude | texto |" + "\n"
            )
            with contextlib.redirect_stderr(io.StringIO()):
                frases = medir_voz.ler_amostra(caminho)
            self.assertEqual(len(frases), 1)
            self.assertEqual(frases[0].frase_com_marcadores, "diz a | ao claude")
            self.assertEqual(frases[0].intencao_documentada, "texto")


class TestAgregados(unittest.TestCase):
    """Macro-media e WER de corpus sao numeros diferentes e
    o ficheiro tem de dizer qual e qual. Valores calculados a mao."""

    def linha(self, numero: int, referencia: str, hipotese: str, acertou: bool) -> medir_voz.LinhaMedida:
        return medir_voz.LinhaMedida(
            numero=numero,
            tipo_documentado="local",
            frase_esperada=referencia,
            transcricao=hipotese,
            tipo_esperado="local",
            nome_acao_esperado="x",
            argumento_esperado=None,
            tipo_obtido="local" if acertou else "claude",
            nome_acao_obtido="x" if acertou else None,
            argumento_obtido=None,
            acertou_intencao=acertou,
            wer=medir_voz.calcular_wer(referencia, hipotese),
        )

    def test_macro_media_e_corpus_sao_calculados_como_a_definicao_diz(self) -> None:
        # Frase A: 2 palavras de referencia, 1 erro -> WER 50%.
        # Frase B: 8 palavras de referencia, 1 erro -> WER 12.5%.
        # macro = (50 + 12.5) / 2 = 31.25%; corpus = 2 erros / 10 palavras = 20%.
        a = self.linha(1, "abre pasta", "abre casa", False)
        b = self.linha(2, "um dois tres quatro cinco seis sete oito", "um dois tres quatro cinco seis sete nove", True)
        self.assertEqual(a.wer.distancia_edicao, 1)
        self.assertEqual(b.wer.distancia_edicao, 1)
        agregados = medir_voz.calcular_agregados([a, b])
        self.assertEqual(agregados.n_frases, 2)
        self.assertEqual(agregados.n_acertos, 1)
        self.assertAlmostEqual(agregados.acerto_intencao_pct, 50.0)
        self.assertAlmostEqual(agregados.wer_macro_pct, 31.25)
        self.assertAlmostEqual(agregados.wer_corpus_pct, 20.0)

    def test_uma_referencia_vazia_conta_como_insercoes_no_wer_de_corpus(self) -> None:
        # Fixado de proposito, nao por acidente.
        # Frase A: 2 palavras de referencia, 1 erro. Frase B: referencia vazia
        # e 3 palavras de hipotese = 3 insercoes, nenhuma palavra no
        # denominador. corpus = (1 + 3) / 2 = 200%; macro = (50 + 100) / 2 = 75%.
        a = self.linha(1, "abre pasta", "abre casa", False)
        b = self.linha(2, "", "lixo a mais", False)
        self.assertEqual(b.wer.n_palavras_referencia, 0)
        self.assertEqual(b.wer.distancia_edicao, 3)
        agregados = medir_voz.calcular_agregados([a, b])
        self.assertAlmostEqual(agregados.wer_macro_pct, 75.0)
        self.assertAlmostEqual(agregados.wer_corpus_pct, 200.0)

    def test_sem_linhas_nenhum_agregado_rebenta(self) -> None:
        agregados = medir_voz.calcular_agregados([])
        self.assertEqual(agregados.n_frases, 0)
        self.assertEqual(agregados.acerto_intencao_pct, 0.0)
        self.assertEqual(agregados.wer_corpus_pct, 0.0)


class TestFalhaDeUmaFraseNaoDeitaForaAMedicao(unittest.TestCase):
    """Uma excecao na frase 19 deitava fora as 18 anteriores
    e o proprio entregavel."""

    def setUp(self) -> None:
        from jarvis.config import Config, Projeto

        self.config = Config(
            microfone="Microfone de Teste",
            projetos=(Projeto(nome="exemplo-um", caminho=Path("D:/caminho/para/exemplo-um")),),
        )

    def test_linha_falhada_conta_como_falha_com_wer_de_cem_por_cento(self) -> None:
        frase = medir_voz.FraseDaAmostra(
            numero=19,
            tipo_documentado="local",
            frase_com_marcadores="que horas são",
            intencao_documentada="horas_e_data (horas)",
        )
        linha = medir_voz.linha_falhada(frase, self.config, ["exemplo-um"], RuntimeError("GPU foi-se"))
        self.assertFalse(linha.acertou_intencao)
        self.assertEqual(linha.wer.wer, 1.0)
        self.assertEqual(linha.wer.n_palavras_referencia, 3)
        self.assertIn("GPU foi-se", linha.erro or "")
        # A intencao ESPERADA continua a ser calculada pelo router, mesmo na falha.
        self.assertEqual(linha.tipo_esperado, "local")
        self.assertEqual(linha.nome_acao_esperado, "horas_e_data")

    def test_evidencia_escrita_com_uma_frase_falhada_diz_o_erro_e_conta_os_agregados(self) -> None:
        frase_ok = medir_voz.FraseDaAmostra(19, "local", "que horas são", "horas_e_data (horas)")
        boa = medir_voz.LinhaMedida(
            numero=1,
            tipo_documentado="local",
            frase_esperada="que horas são",
            transcricao="que horas são",
            tipo_esperado="local",
            nome_acao_esperado="horas_e_data",
            argumento_esperado="horas",
            tipo_obtido="local",
            nome_acao_obtido="horas_e_data",
            argumento_obtido="horas",
            acertou_intencao=True,
            wer=medir_voz.calcular_wer("que horas são", "que horas são"),
        )
        ma = medir_voz.linha_falhada(frase_ok, self.config, ["exemplo-um"], RuntimeError("GPU foi-se"))
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-falha-medir-voz.md"
            agregados = medir_voz.escrever_evidencia([boa, ma], "exemplo", "cpu", caminho, 1.0)
            escrito = caminho.read_text(encoding="utf-8")
        self.assertEqual(agregados.n_erros, 1)
        self.assertEqual(agregados.n_acertos, 1)
        self.assertAlmostEqual(agregados.acerto_intencao_pct, 50.0)
        self.assertIn("GPU foi-se", escrito)
        self.assertIn("Frases que rebentaram a meio: 1", escrito)


class TestAvisosObrigatoriosNaEvidencia(unittest.TestCase):
    """Audio sintetico (nao mede a voz do utilizador, proibido propor ingles)
    e dados privados com um config.toml real."""

    def test_o_ficheiro_gerado_diz_as_tres_coisas_da_d34_e_avisa_da_privacidade(self) -> None:
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-avisos-medir-voz.md"
            medir_voz.escrever_evidencia([], "exemplo", "cpu", caminho, 0.0)
            escrito = caminho.read_text(encoding="utf-8")
        self.assertIn("AUDIO SINTETICO", escrito)
        self.assertIn("NAO mede o reconhecimento da voz do utilizador", escrito)
        self.assertIn("proibido propor a troca para ingles", escrito)
        # Quem copiar excertos daqui para um ficheiro versionado tem de
        # ser avisado de que isto pode levar nomes e caminhos reais.
        self.assertIn("dados privados do utilizador", escrito)
        self.assertIn("`.gitignore`", escrito)
        # E o limiar da D7 escrito com a conjuncao exata, nao suavizado.
        self.assertIn("WER <= 15%", escrito)


class TestLatenciaEPisoDoAcertoNaEvidencia(unittest.TestCase):
    """A latencia media-se e nunca chegava ao
    ficheiro, e o piso por construcao do acerto de intencao nao estava escrito
    onde quem le os agregados o ve."""

    def linha(self, numero: int, tipo: str, acertou: bool) -> "medir_voz.LinhaMedida":
        if tipo == "claude":
            esperado_acao, esperado_arg = None, None
        else:
            esperado_acao, esperado_arg = "horas_e_data", "horas"
        return medir_voz.LinhaMedida(
            numero=numero,
            tipo_documentado=tipo,
            frase_esperada="que horas são",
            transcricao="que horas são",
            tipo_esperado=tipo,
            nome_acao_esperado=esperado_acao,
            argumento_esperado=esperado_arg,
            tipo_obtido=tipo,
            nome_acao_obtido=esperado_acao,
            argumento_obtido=esperado_arg,
            acertou_intencao=acertou,
            wer=medir_voz.calcular_wer("que horas são", "que horas são"),
            duracao_audio_s=2.0,
            latencia_transcricao_ms=100.0 * numero,
            latencia_total_ms=1000.0 * numero,
        )

    def escrever(self, linhas: list) -> str:
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-latencia-medir-voz.md"
            medir_voz.escrever_evidencia(linhas, "exemplo", "cpu", caminho, 3.0)
            return caminho.read_text(encoding="utf-8")

    def test_a_latencia_da_transcricao_e_a_duracao_do_audio_chegam_ao_ficheiro(self) -> None:
        escrito = self.escrever([self.linha(1, "local", True), self.linha(3, "local", True)])
        # mediana de 100 e 300 ms = 200; maximo = 300; audio total = 4,0 s.
        self.assertIn("mediana 200 ms", escrito)
        self.assertIn("máximo 300 ms", escrito)
        self.assertIn("4.0 s de áudio", escrito)
        # E o total (carregamento incluido) sai rotulado como outra coisa.
        self.assertIn("carregamento do modelo + transcrição", escrito)
        self.assertIn("3000 ms na pior frase", escrito)

    def test_o_piso_por_construcao_do_acerto_e_escrito_com_o_numero_de_frases(self) -> None:
        linhas = [self.linha(1, "local", True)] + [
            self.linha(n, "claude", True) for n in range(2, 5)
        ]
        escrito = self.escrever(linhas)
        self.assertIn("Piso por construção do acerto de intenção", escrito)
        self.assertIn("3 das 4 frases", escrito)
        self.assertIn("`claude: —`", escrito)

    def test_sem_frases_do_claude_nao_se_escreve_o_aviso_do_piso(self) -> None:
        escrito = self.escrever([self.linha(1, "local", True)])
        self.assertNotIn("Piso por construção", escrito)


class TestProtocoloNoFicheiroVersionado(unittest.TestCase):
    """O ficheiro suavizava os limiares («e WER <= 15% como referencia» em vez
    da conjuncao)."""

    def test_o_limiar_de_noventa_por_cento_usa_a_conjuncao_da_d7(self) -> None:
        texto = medir_voz.CAMINHO_AMOSTRA_PADRAO.read_text(encoding="utf-8")
        self.assertIn("acerto de intencao >= 90% E WER <= 15%", texto)
        self.assertNotIn("como referencia", texto)

    def test_as_outras_duas_bandas_da_d7_continuam_escritas(self) -> None:
        texto = medir_voz.CAMINHO_AMOSTRA_PADRAO.read_text(encoding="utf-8")
        self.assertIn("entre 75% e 90%", texto)
        self.assertIn("< 75%", texto)
        self.assertIn("manter portugues", texto)


class TestPrefixoEModeloEmMedirUmaFrase(unittest.TestCase):
    """O `--prefixo` so entra no texto SINTETIZADO e na referencia do
    WER, nunca no calculo da intencao esperada; o `--modelo` chega ao
    transcritor tal e qual. `gerar_wav`/`transcrever` sao substituidos por
    duplos que so REGISTAM o que recebem — nenhum GPU, Piper ou disco de
    audio real e tocado (mesma garantia do resto deste ficheiro)."""

    def setUp(self) -> None:
        from jarvis.config import Config, Projeto

        self.config = Config(
            microfone="Microfone de Teste",
            projetos=(Projeto(nome="exemplo-um", caminho=Path("D:/caminho/para/exemplo-um")),),
        )
        self.frase = medir_voz.FraseDaAmostra(
            numero=1,
            tipo_documentado="local",
            frase_com_marcadores="que horas são",
            intencao_documentada="horas_e_data (horas)",
        )

    def _medir_com_duplos(self, *, modelo: str, prefixo: str, texto_transcrito: str):
        chamadas_gerar_wav: list[str] = []
        chamadas_transcrever: list[dict] = []

        def gerar_wav_falso(texto, saida, **kwargs):
            # O arnes de medicao NUNCA pede som. Se um dia alguem
            # passar `com_som=True` por omissao aqui, este teste cai antes de
            # a suite fazer barulho nas colunas do utilizador.
            assert kwargs.get("com_som") is False, (
                "medir_uma_frase pediu som ao sintetizador (D61): "
                f"com_som={kwargs.get('com_som')!r}"
            )
            chamadas_gerar_wav.append(texto)
            return saida, 1.23, 999

        def transcrever_falso(caminho, device="cuda", modelo_preferido="medium", **kwargs):
            chamadas_transcrever.append({"device": device, "modelo_preferido": modelo_preferido})
            return {
                "texto": texto_transcrito,
                "modelo": modelo_preferido,
                "device": device,
                "latencia_ms": 10.0,
                "latencia_transcricao_ms": 5.0,
                "prompt_estado": "desligado",
            }

        with tempfile.TemporaryDirectory() as pasta, mock.patch.object(
            medir_voz.gerar_wav_mod, "gerar_wav", gerar_wav_falso
        ), mock.patch.object(medir_voz.transcrever_mod, "transcrever", transcrever_falso):
            linha = medir_voz.medir_uma_frase(
                self.frase,
                self.config,
                ["exemplo-um"],
                device="cpu",
                pasta_audio=Path(pasta),
                manter_audio=False,
                modelo=modelo,
                prefixo=prefixo,
            )
        return linha, chamadas_gerar_wav, chamadas_transcrever

    def test_prefixo_entra_no_audio_e_na_referencia_do_wer_mas_nao_na_intencao_esperada(
        self,
    ) -> None:
        # Prefixo malicioso de proposito: se ele vazasse para o calculo da
        # intencao esperada, "que horas são" deixava de ser "local" — uma
        # negacao manda SEMPRE para "claude" (D4/router.PADRAO_NEGACAO).
        # Provar que isso nao acontece e a garantia central deste arnes.
        linha, chamadas_gerar_wav, _ = self._medir_com_duplos(
            modelo="medium", prefixo="não ", texto_transcrito="não que horas são"
        )
        # O audio recebeu o prefixo colado a frase.
        self.assertEqual(chamadas_gerar_wav, ["não que horas são"])
        # A intencao esperada continua a ser a da frase SEM prefixo.
        self.assertEqual(linha.tipo_esperado, "local")
        self.assertEqual(linha.nome_acao_esperado, "horas_e_data")
        self.assertEqual(linha.argumento_esperado, "horas")
        # A referencia do WER e o texto REALMENTE sintetizado (com prefixo):
        # a transcricao fingida bate-lhe exatamente, WER zero.
        self.assertEqual(linha.frase_esperada, "não que horas são")
        self.assertEqual(linha.wer.wer, 0.0)

    def test_sem_prefixo_o_comportamento_fica_exatamente_como_antes(self) -> None:
        linha, chamadas_gerar_wav, _ = self._medir_com_duplos(
            modelo="medium", prefixo="", texto_transcrito="que horas são"
        )
        self.assertEqual(chamadas_gerar_wav, ["que horas são"])
        self.assertEqual(linha.frase_esperada, "que horas são")
        self.assertEqual(linha.tipo_esperado, "local")

    def test_modelo_chega_ao_transcritor(self) -> None:
        _, _, chamadas_transcrever = self._medir_com_duplos(
            modelo="small", prefixo="", texto_transcrito="que horas são"
        )
        self.assertEqual(len(chamadas_transcrever), 1)
        self.assertEqual(chamadas_transcrever[0]["modelo_preferido"], "small")
        self.assertEqual(chamadas_transcrever[0]["device"], "cpu")


class TestParserModeloEPrefixo(unittest.TestCase):
    """As duas flags novas existem, com os defaults certos, e o
    `--modelo` usa a lista fechada do transcritor (nao aceita um repo
    qualquer do Hugging Face, mesma garantia do autoteste do jarvis)."""

    def test_defaults_nao_mudam_o_comportamento_de_hoje(self) -> None:
        args = medir_voz.construir_parser().parse_args([])
        self.assertEqual(args.modelo, medir_voz.transcrever_mod.MODELO_PREFERIDO)
        self.assertEqual(args.prefixo, "")

    def test_valores_explicitos_sao_aceites(self) -> None:
        args = medir_voz.construir_parser().parse_args(
            ["--modelo", "small", "--prefixo", "hey jarvis, "]
        )
        self.assertEqual(args.modelo, "small")
        self.assertEqual(args.prefixo, "hey jarvis, ")

    def test_modelo_fora_da_lista_fechada_e_recusado(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                medir_voz.construir_parser().parse_args(["--modelo", "alguem/repo-mau"])

    def test_a_lista_de_escolhas_e_a_mesma_do_transcritor(self) -> None:
        parser = medir_voz.construir_parser()
        acao_modelo = next(a for a in parser._actions if a.dest == "modelo")
        self.assertEqual(list(acao_modelo.choices), medir_voz.transcrever_mod.MODELOS_PERMITIDOS)

    def test_ajuda_mostra_as_duas_flags_novas(self) -> None:
        ajuda = medir_voz.construir_parser().format_help()
        self.assertIn("--modelo", ajuda)
        self.assertIn("--prefixo", ajuda)


class TestLinhasDivergentes(unittest.TestCase):
    """Conta as linhas em que o tipo DOCUMENTADO na tabela difere do
    tipo CALCULADO por `encaminhar()` (`tipo_esperado`) — sem isto, uma
    amostra cujas frases `local` ainda nao tem lista branca (o caso de
    `frases-en.md` antes da lista branca inglesa) aparecia com acerto de intencao alto so
    por construcao."""

    def linha(self, tipo_documentado: str, tipo_esperado: str) -> medir_voz.LinhaMedida:
        return medir_voz.LinhaMedida(
            numero=1,
            tipo_documentado=tipo_documentado,
            frase_esperada="x",
            transcricao="x",
            tipo_esperado=tipo_esperado,
            nome_acao_esperado=None,
            argumento_esperado=None,
            tipo_obtido=tipo_esperado,
            nome_acao_obtido=None,
            argumento_obtido=None,
            acertou_intencao=True,
            wer=medir_voz.calcular_wer("x", "x"),
        )

    def test_conta_so_as_linhas_onde_documentado_e_esperado_diferem(self) -> None:
        linhas = [
            self.linha("local", "local"),  # concorda: nao diverge
            self.linha("local", "claude"),  # tabela diz local, encaminhar() diz claude: diverge
            self.linha("claude", "claude"),  # concorda: nao diverge
            self.linha("claude", "local"),  # diverge tambem no sentido contrario
        ]
        agregados = medir_voz.calcular_agregados(linhas)
        self.assertEqual(agregados.n_divergentes, 2)

    def test_sem_divergencia_nenhuma_o_contador_fica_a_zero(self) -> None:
        linhas = [self.linha("local", "local"), self.linha("claude", "claude")]
        agregados = medir_voz.calcular_agregados(linhas)
        self.assertEqual(agregados.n_divergentes, 0)

    def test_sem_linhas_o_contador_fica_a_zero(self) -> None:
        self.assertEqual(medir_voz.calcular_agregados([]).n_divergentes, 0)

    def test_a_linha_divergentes_aparece_na_evidencia_com_o_numero_certo(self) -> None:
        linhas = [
            self.linha("local", "local"),
            self.linha("local", "claude"),
        ]
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-divergentes-medir-voz.md"
            medir_voz.escrever_evidencia(linhas, "exemplo", "cpu", caminho, 0.0)
            escrito = caminho.read_text(encoding="utf-8")
        self.assertIn("Linhas divergentes", escrito)
        self.assertIn("1/2", escrito)


class TestCabecalhoDaEvidenciaComAmostraModeloEPrefixo(unittest.TestCase):
    """O cabecalho da evidencia regista amostra, modelo, prefixo
    usado e o aviso da voz pt-PT, mesmo com a amostra `frases-en.md`."""

    def test_cabecalho_mostra_amostra_modelo_prefixo_e_aviso_da_voz(self) -> None:
        caminho_en = medir_voz.RAIZ / "tests" / "voz" / "frases-en.md"
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-cabecalho-medir-voz.md"
            medir_voz.escrever_evidencia(
                [],
                "exemplo",
                "cpu",
                caminho,
                0.0,
                caminho_amostra=caminho_en,
                modelo="small",
                prefixo="hey jarvis, ",
            )
            escrito = caminho.read_text(encoding="utf-8")
        self.assertIn("frases-en.md", escrito)
        self.assertIn("small", escrito)
        self.assertIn("'hey jarvis, '", escrito)
        self.assertIn("voz Piper pt-PT", escrito)
        self.assertIn("não existe voz inglesa", escrito)

    def test_sem_prefixo_o_cabecalho_diz_nenhum(self) -> None:
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-cabecalho-sem-prefixo.md"
            medir_voz.escrever_evidencia([], "exemplo", "cpu", caminho, 0.0, modelo="medium")
            escrito = caminho.read_text(encoding="utf-8")
        self.assertIn("Prefixo usado (--prefixo): (nenhum)", escrito)


class TestLerAmostraFrasesEmIngles(unittest.TestCase):
    """`frases-en.md` tem a mesma forma e o mesmo tamanho da amostra
    pt-PT, e nenhuma palavra proibida de compra/venda em ingles."""

    CAMINHO = RAIZ / "tests" / "voz" / "frases-en.md"

    def test_tem_vinte_frases_numeradas_de_um_a_vinte_sem_avisos(self) -> None:
        avisos: list[str] = []
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            frases = medir_voz.ler_amostra(self.CAMINHO, avisos=avisos)
        self.assertEqual(len(frases), 20)
        self.assertEqual([f.numero for f in frases], list(range(1, 21)))
        self.assertEqual(avisos, [])
        self.assertEqual(stderr.getvalue(), "")

    def test_dez_locais_e_dez_para_o_claude_no_mesmo_padrao_linha_a_linha_da_amostra_pt(
        self,
    ) -> None:
        frases_en = medir_voz.ler_amostra(self.CAMINHO)
        frases_pt = medir_voz.ler_amostra(medir_voz.CAMINHO_AMOSTRA_PADRAO)
        self.assertEqual(
            [f.tipo_documentado for f in frases_en],
            [f.tipo_documentado for f in frases_pt],
        )

    def test_nenhuma_palavra_proibida_de_compra_e_venda_em_ingles(self) -> None:
        # D64/D52: buy/sell/order/trade/broker/wallet nunca aparecem aqui.
        import re as re_mod

        texto = self.CAMINHO.read_text(encoding="utf-8").lower()
        for palavra in ("buy", "sell", "order", "trade", "broker", "wallet"):
            self.assertIsNone(
                re_mod.search(rf"\b{palavra}\b", texto),
                msg=f"a palavra proibida {palavra!r} aparece em frases-en.md",
            )

    def test_nenhum_caminho_do_disco_do_sponsor_na_amostra_em_ingles(self) -> None:
        texto = self.CAMINHO.read_text(encoding="utf-8")
        self.assertNotRegex(texto, r"[A-Za-z]:[\\/]")
        self.assertNotIn("Users", texto)


# --- A lingua e o custo de latencia na evidencia ----------------------------


class TestPercentil(unittest.TestCase):
    """O p95 da D6 e nearest-rank: sai sempre uma frase que existiu mesmo."""

    def test_p50_e_p95_de_uma_lista_conhecida(self) -> None:
        valores = list(range(1, 21))  # 1..20
        self.assertEqual(medir_voz.percentil(valores, 0.5), 10)
        self.assertEqual(medir_voz.percentil(valores, 0.95), 19)

    def test_um_so_valor(self) -> None:
        self.assertEqual(medir_voz.percentil([432.0], 0.95), 432.0)

    def test_lista_vazia_nao_rebenta(self) -> None:
        self.assertEqual(medir_voz.percentil([], 0.95), 0.0)

    def test_nao_interpola_entre_duas_frases(self) -> None:
        # Media de 100 e 200 seria 150; o nearest-rank devolve um dos dois.
        self.assertIn(medir_voz.percentil([100.0, 200.0], 0.5), (100.0, 200.0))


class TestLinguaNaEvidencia(unittest.TestCase):
    """A lingua de cada frase e os percentis no ficheiro."""

    def linha(self, numero: int, lingua: str, prob: float, hesitou: bool, top1: str, latencia: float):
        return medir_voz.LinhaMedida(
            numero=numero,
            tipo_documentado="local",
            frase_esperada="que horas sao",
            transcricao="que horas sao",
            tipo_esperado="local",
            nome_acao_esperado="horas_e_data",
            argumento_esperado="horas",
            tipo_obtido="local",
            nome_acao_obtido="horas_e_data",
            argumento_obtido="horas",
            acertou_intencao=True,
            wer=medir_voz.calcular_wer("que horas sao", "que horas sao"),
            duracao_audio_s=1.0,
            latencia_transcricao_ms=latencia,
            latencia_total_ms=latencia + 10,
            lingua=lingua,
            lingua_probabilidade=prob,
            lingua_hesitou=hesitou,
            prob_pt=prob if lingua == "pt" else 0.02,
            prob_en=prob if lingua == "en" else 0.02,
            lingua_top1=top1,
        )

    def escrever(self, linhas):
        with pasta_de_evidencia_temporaria() as pasta:
            caminho = pasta / "teste-lingua-medir-voz.md"
            medir_voz.escrever_evidencia(linhas, "exemplo", "cpu", caminho, 0.0)
            return caminho.read_text(encoding="utf-8")

    def test_a_coluna_da_lingua_traz_lingua_e_probabilidade(self) -> None:
        texto = self.escrever([self.linha(1, "pt", 0.96, False, "pt", 300.0)])
        self.assertIn("| pt 0.96 |", texto)

    def test_uma_frase_hesitante_diz_que_hesitou(self) -> None:
        texto = self.escrever([self.linha(1, "en", 0.31, True, "en", 300.0)])
        self.assertIn("en 0.31 hesitou", texto)

    def test_terceira_lingua_no_topo_marca_a_linha_como_lingua_terceira(self) -> None:
        texto = self.escrever([self.linha(1, "pt", 0.20, True, "es", 300.0)])
        self.assertIn("lingua-terceira(es descodificou)", texto)

    def test_o_agregado_conta_as_linhas_lingua_terceira_e_diz_quais(self) -> None:
        # O ficheiro tem de trazer a CONTAGEM
        # e os numeros das frases, para se poder reconferir linha a linha.
        linhas = [
            self.linha(1, "pt", 0.96, False, "pt", 300.0),
            self.linha(2, "pt", 0.20, True, "ru", 310.0),
            self.linha(3, "en", 0.31, True, "pl", 320.0),
        ]
        texto = self.escrever(linhas)
        self.assertIn("**`lingua-terceira`: 2/3 frases** (#2, #3)", texto)

    def test_o_agregado_nao_volta_a_dizer_que_a_terceira_lingua_foi_ignorada(self) -> None:
        # Uma afirmacao FALSA de uma versao anterior, fixada aqui
        # para nunca mais voltar: com language=None quem descodifica e o argmax
        # LIVRE, logo a terceira lingua nao pode ser descrita como ignorada.
        texto = self.escrever([self.linha(1, "pt", 0.20, True, "ru", 300.0)])
        self.assertNotIn("foi ignorada", texto)
        self.assertNotIn("não decidiu nada", texto)
        self.assertIn("DESCODIFICOU o áudio", texto)
        self.assertIn("nunca é descartada nem re-transcrita", texto)

    def test_sem_terceira_lingua_a_contagem_e_zero(self) -> None:
        texto = self.escrever([self.linha(1, "pt", 0.96, False, "pt", 300.0)])
        self.assertIn("**`lingua-terceira`: 0/1 frases** (nenhuma)", texto)

    def test_o_agregado_conta_as_duas_linguas_e_as_hesitacoes(self) -> None:
        linhas = [
            self.linha(1, "pt", 0.96, False, "pt", 300.0),
            self.linha(2, "pt", 0.91, False, "pt", 320.0),
            self.linha(3, "en", 0.40, True, "en", 340.0),
        ]
        texto = self.escrever(linhas)
        self.assertIn("pt em 2/3 frases, en em 1, hesitou", texto)
        self.assertIn("hesitou (probabilidade não acima do limiar) em 1", texto)

    def test_o_agregado_diz_que_a_lingua_nao_escolhe_lista_branca(self) -> None:
        texto = self.escrever([self.linha(1, "pt", 0.96, False, "pt", 300.0)])
        self.assertIn("casa sempre contra as DUAS", texto)

    def test_os_percentis_sao_escritos_com_o_orcamento_da_d6(self) -> None:
        linhas = [self.linha(n, "pt", 0.9, False, "pt", 100.0 * n) for n in range(1, 21)]
        texto = self.escrever(linhas)
        # Latencias 100..2000 ms. p50 = mediana = (1000+1100)/2 = 1050 ms (o
        # mesmo numero da linha "mediana" logo acima, para nao haver duas
        # medianas diferentes no mesmo ficheiro); p95 = 19.a de 20 = 1900 ms,
        # nearest-rank.
        self.assertIn("p50 1050 ms, p95 1900 ms", texto)
        self.assertIn("p50 DENTRO do orçamento", texto)
        self.assertIn("p95 DENTRO do orçamento", texto)

    def test_um_p95_fora_do_orcamento_e_escrito_como_fora(self) -> None:
        linhas = [self.linha(n, "pt", 0.9, False, "pt", 4000.0) for n in range(1, 4)]
        texto = self.escrever(linhas)
        self.assertIn("p50 FORA do orçamento", texto)
        self.assertIn("p95 FORA do orçamento", texto)

    def test_sem_lingua_medida_o_bloco_nao_aparece(self) -> None:
        # Compatibilidade com um transcritor antigo: a evidencia continua a
        # sair, so sem a coluna da lingua preenchida.
        import dataclasses

        linha = dataclasses.replace(self.linha(1, "pt", 0.9, False, "pt", 300.0), lingua="?")
        texto = self.escrever([linha])
        self.assertNotIn("Deteção automática de língua", texto)
        self.assertIn("| — |", texto)


class TestLinhaMedidaCarregaALingua(unittest.TestCase):
    """O dict de `transcrever()` chega mesmo a linha da evidencia."""

    def test_as_chaves_da_lingua_passam_do_transcritor_para_a_linha(self) -> None:
        from jarvis.config import Config

        config = Config(microfone="Microfone de Teste", projetos=())
        frase = medir_voz.FraseDaAmostra(
            numero=1,
            tipo_documentado="local",
            frase_com_marcadores="que horas sao",
            intencao_documentada="horas",
        )

        def gerar_wav_falso(texto, saida, **kwargs):
            return saida, 1.0, 999

        def transcrever_falso(caminho, device="cuda", modelo_preferido="medium", **kwargs):
            return {
                "texto": "que horas sao",
                "modelo": modelo_preferido,
                "device": device,
                "latencia_ms": 10.0,
                "latencia_transcricao_ms": 5.0,
                "prompt_estado": "desligado",
                "lingua": "en",
                "lingua_probabilidade": 0.42,
                "lingua_hesitou": True,
                "prob_pt": 0.30,
                "prob_en": 0.42,
                "lingua_top1": "es",
            }

        with tempfile.TemporaryDirectory() as pasta, mock.patch.object(
            medir_voz.gerar_wav_mod, "gerar_wav", gerar_wav_falso
        ), mock.patch.object(medir_voz.transcrever_mod, "transcrever", transcrever_falso):
            linha = medir_voz.medir_uma_frase(
                frase,
                config,
                [],
                device="cpu",
                pasta_audio=Path(pasta),
                manter_audio=False,
            )
        self.assertEqual(linha.lingua, "en")
        self.assertAlmostEqual(linha.lingua_probabilidade, 0.42)
        self.assertTrue(linha.lingua_hesitou)
        self.assertEqual(linha.lingua_top1, "es")
        self.assertEqual(
            medir_voz.coluna_da_lingua(linha), "en 0.42 hesitou lingua-terceira(es descodificou)"
        )


class TestAbControlado(unittest.TestCase):
    r"""A comparação que decide a língua do produto corre sobre os MESMOS WAV.

    O Piper é estocástico. Numa medição anterior, duas corridas do MESMO código
    deram 79 transcrições diferentes em 80 e mudaram a duração do áudio em
    18–20 de 20 linhas por combinação: comparar dois ficheiros desses muda duas
    variáveis ao mesmo tempo. Estes testes fixam a máquina que torna o A/B
    controlado — reutilizar o áudio e fixar a língua — e o que a evidência
    escreve sobre ela.
    """

    def frase(self):
        return medir_voz.FraseDaAmostra(
            numero=7,
            tipo_documentado="local",
            frase_com_marcadores="que horas sao",
            intencao_documentada="horas",
        )

    def config(self):
        from jarvis.config import Config

        return Config(microfone="Microfone de Teste", projetos=())

    def escrever_wav(self, pasta: Path, numero: int, segundos: float = 0.5) -> Path:
        import wave

        caminho = pasta / f"{numero:02d}.wav"
        with wave.open(str(caminho), "wb") as ficheiro:
            ficheiro.setnchannels(1)
            ficheiro.setsampwidth(2)
            ficheiro.setframerate(16000)
            ficheiro.writeframes(b"\x00\x00" * int(16000 * segundos))
        return caminho

    def test_duracao_do_wav_le_o_cabecalho(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = self.escrever_wav(Path(pasta), 1, 0.75)
            self.assertAlmostEqual(medir_voz.duracao_do_wav(caminho), 0.75, places=3)

    def test_reutilizar_audio_nao_sintetiza_nada_e_nao_apaga_o_wav(self) -> None:
        chamadas_ao_piper: list[str] = []
        kwargs_da_transcricao: list[dict] = []

        def gerar_wav_falso(texto, saida, **kwargs):  # pragma: no cover - nao deve correr
            chamadas_ao_piper.append(texto)
            return saida, 1.0, 999

        def transcrever_falso(caminho, **kwargs):
            kwargs_da_transcricao.append(kwargs)
            return {
                "texto": "que horas sao",
                "modelo": "medium",
                "device": "cpu",
                "latencia_ms": 10.0,
                "latencia_transcricao_ms": 5.0,
                "prompt_estado": "desligado",
                "lingua": "pt",
                "lingua_probabilidade": 0.0,
                "lingua_hesitou": True,
            }

        with tempfile.TemporaryDirectory() as pasta, mock.patch.object(
            medir_voz.gerar_wav_mod, "gerar_wav", gerar_wav_falso
        ), mock.patch.object(medir_voz.transcrever_mod, "transcrever", transcrever_falso):
            caminho = self.escrever_wav(Path(pasta), 7, 1.25)
            linha = medir_voz.medir_uma_frase(
                self.frase(),
                self.config(),
                [],
                device="cpu",
                pasta_audio=Path(pasta),
                manter_audio=False,
                reutilizar_audio=True,
                lingua_fixa="pt",
            )
            # O WAV sobrevive a corrida: a perna B do A/B tem de poder ler o
            # mesmo ficheiro que a perna A leu.
            self.assertTrue(caminho.is_file())
        self.assertEqual(chamadas_ao_piper, [])
        self.assertEqual(kwargs_da_transcricao[0]["lingua_fixa"], "pt")
        # A duracao vem do WAV reutilizado, nao de uma sintese que nao houve.
        self.assertAlmostEqual(linha.duracao_audio_s, 1.25, places=2)

    def test_sem_reutilizar_a_lingua_do_produto_chega_ao_transcritor(self) -> None:
        kwargs_da_transcricao: list[dict] = []

        def gerar_wav_falso(texto, saida, **kwargs):
            saida.write_bytes(b"")
            return saida, 1.0, 999

        def transcrever_falso(caminho, **kwargs):
            kwargs_da_transcricao.append(kwargs)
            return {
                "texto": "que horas sao",
                "modelo": "medium",
                "device": "cpu",
                "latencia_ms": 10.0,
                "latencia_transcricao_ms": 5.0,
                "prompt_estado": "desligado",
            }

        with tempfile.TemporaryDirectory() as pasta, mock.patch.object(
            medir_voz.gerar_wav_mod, "gerar_wav", gerar_wav_falso
        ), mock.patch.object(medir_voz.transcrever_mod, "transcrever", transcrever_falso):
            medir_voz.medir_uma_frase(
                self.frase(),
                self.config(),
                [],
                device="cpu",
                pasta_audio=Path(pasta),
                manter_audio=False,
            )
        # Uma corrida normal mede O PRODUTO, e desde a reversao do criterio 6
        # o produto transcreve com a lingua fixa. A deteccao pede-se com
        # `--lingua auto` (perna B do A/B), nunca por omissao.
        self.assertEqual(
            kwargs_da_transcricao[0]["lingua_fixa"], medir_voz.LINGUA_FIXA_DO_PRODUTO
        )

    def test_reutilizar_audio_sem_wav_rebenta_com_mensagem_util(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            with self.assertRaises(FileNotFoundError) as caixa:
                medir_voz.medir_uma_frase(
                    self.frase(),
                    self.config(),
                    [],
                    device="cpu",
                    pasta_audio=Path(pasta),
                    manter_audio=False,
                    reutilizar_audio=True,
                )
        self.assertIn("--manter-audio", str(caixa.exception))

    def test_a_pasta_de_audio_fica_sempre_dentro_de_audio(self) -> None:
        base = Path("C:/repo/audio/medir-voz")
        self.assertEqual(medir_voz.pasta_de_audio_de_saida(None, base), base)
        self.assertEqual(
            medir_voz.pasta_de_audio_de_saida("ab-pt-sem", base), base / "ab-pt-sem"
        )
        for mau in ("../fora", "a/b", "/absoluto", "..", "C:\\outro"):
            with self.assertRaises(ValueError, msg=f"'{mau}' devia ser recusado"):
                medir_voz.pasta_de_audio_de_saida(mau, base)

    def test_o_cabecalho_diz_qual_a_lingua_e_qual_o_conjunto_de_wav(self) -> None:
        linhas = [
            medir_voz.LinhaMedida(
                numero=1,
                tipo_documentado="local",
                frase_esperada="que horas sao",
                transcricao="que horas sao",
                tipo_esperado="local",
                nome_acao_esperado="horas_e_data",
                argumento_esperado="horas",
                tipo_obtido="local",
                nome_acao_obtido="horas_e_data",
                argumento_obtido="horas",
                acertou_intencao=True,
                wer=medir_voz.calcular_wer("que horas sao", "que horas sao"),
                latencia_transcricao_ms=300.0,
                duracao_audio_s=1.0,
                lingua="pt",
                lingua_probabilidade=0.9,
                lingua_top1="pt",
            )
        ]
        with pasta_de_evidencia_temporaria() as pasta:
            auto = pasta / "auto.md"
            medir_voz.escrever_evidencia(
                linhas,
                "exemplo",
                "cpu",
                auto,
                0.0,
                pasta_audio=Path("C:/repo/audio/medir-voz/ab-pt-sem"),
                audio_guardado=True,
            )
            texto_auto = auto.read_text(encoding="utf-8")
            controlo = pasta / "controlo.md"
            medir_voz.escrever_evidencia(
                linhas,
                "exemplo",
                "cpu",
                controlo,
                0.0,
                lingua_fixa="pt",
                pasta_audio=Path("C:/repo/audio/medir-voz/ab-pt-sem"),
                audio_reutilizado=True,
            )
            texto_controlo = controlo.read_text(encoding="utf-8")

        self.assertIn("Língua da transcrição: AUTOMÁTICA", texto_auto)
        self.assertIn("GUARDADO", texto_auto)
        self.assertIn("Língua da transcrição: FIXA `language='pt'`", texto_controlo)
        self.assertIn("Conjunto de WAV: REUTILIZADO", texto_controlo)
        # A perna de controlo nao pode fingir que mediu a lingua.
        self.assertIn("Deteção automática de língua: DESLIGADA", texto_controlo)
        self.assertNotIn("**`lingua-terceira`: ", texto_controlo)

    def test_as_tres_flags_existem_na_linha_de_comandos(self) -> None:
        args = medir_voz.construir_parser().parse_args(
            ["--reutilizar-audio", "--lingua", "auto", "--pasta-audio", "ab-pt-sem"]
        )
        self.assertTrue(args.reutilizar_audio)
        self.assertEqual(args.lingua, "auto")
        self.assertEqual(args.pasta_audio, "ab-pt-sem")
        # Por omissao uma corrida normal mede O PRODUTO: nao reutiliza audio e
        # transcreve com a lingua fixa.
        omissao = medir_voz.construir_parser().parse_args([])
        self.assertFalse(omissao.reutilizar_audio)
        self.assertEqual(omissao.lingua, medir_voz.LINGUA_FIXA_DO_PRODUTO)
        # `--lingua auto` e o que o codigo traduz para `lingua_fixa=None`.
        self.assertIsNone(None if args.lingua == "auto" else args.lingua)
        self.assertIsNone(omissao.pasta_audio)


if __name__ == "__main__":
    unittest.main()
