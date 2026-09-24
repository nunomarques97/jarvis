r"""Testes do contrato da resposta falada (jarvis/resposta_falada.py).

Fecha o defeito 3 do teste real: a linha 650 de logs/jarvis-2026-09-20.log trouxe o texto
interno de uma chamada de ferramenta do Claude Code e a linha 652 mostra isso LIDO EM VOZ ALTA
porque o codigo de entao so cortava aos 240 caracteres. Este ficheiro prova, com o texto VERBATIM
da linha 650 (fixture tests/fixtures/resposta-linha-650.txt) mais um corpus adversarial de sete
casos, que o filtro por exclusao (nunca um modelo a julgar) fecha esse buraco.

Mesma convencao dos outros testes deste repositorio: unittest da biblioteca padrao, sem hardware
nenhum. Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from jarvis.resposta_falada import (
    FRASE_RECURSO_SEM_TEXTO,
    FRASE_RECURSO_SO_TECNICO,
    MAXIMO_ABSOLUTO_FALADO,
    MAXIMO_CARACTERES_FALADOS,
    MAXIMO_CARACTERES_POR_LINHA,
    MAXIMO_CARACTERES_POR_PALAVRA,
    PREFIXO_DA_RESPOSTA_DO_CLAUDE,
    cortar_no_limite,
    resumo_falado,
    texto_falavel,
    texto_proibido,
)

FIXTURE_LINHA_650 = Path(__file__).parent / "fixtures" / "resposta-linha-650.txt"


class TestFixtureDaLinha650(unittest.TestCase):
    """A fasquia de fecho nao cortavel do D59.6: o texto exato da linha 650 do log."""

    def test_nada_com_tags_chega_a_voz(self) -> None:
        resposta = FIXTURE_LINHA_650.read_text(encoding="utf-8")
        # a fixture tem mesmo as tags que causaram o defeito, senao o teste nao prova nada
        self.assertIn("<invoke", resposta)
        self.assertIn("</invoke>", resposta)

        falado = resumo_falado(resposta)

        self.assertNotIn("<", falado)
        self.assertNotIn(">", falado)
        self.assertNotIn("invoke", falado)
        self.assertNotIn("parameter", falado)
        self.assertNotIn("git status", falado)
        self.assertNotIn("git diff", falado)

    def test_fixture_esgota_o_filtro_e_cai_no_recurso_tecnico(self) -> None:
        # a linha 650 e so tags e saida de git: nao sobra nenhuma linguagem natural, por isso a
        # voz tem de cair na frase de recurso e nao ficar em silencio
        resposta = FIXTURE_LINHA_650.read_text(encoding="utf-8")
        self.assertEqual(resumo_falado(resposta), FRASE_RECURSO_SO_TECNICO)


class TestCorpusAdversarial(unittest.TestCase):
    """Sete casos curtos, cada um com a sua afirmacao."""

    def test_bloco_de_codigo_e_removido_mas_o_texto_a_volta_fica(self) -> None:
        resposta = (
            "Aqui está o código:\n```python\ndef f():\n    return 1\n```\nEspero que ajude."
        )
        falado = resumo_falado(resposta)
        self.assertNotIn("def f()", falado)
        self.assertNotIn("```", falado)
        self.assertIn("Aqui está o código", falado)
        self.assertIn("Espero que ajude", falado)

    def test_json_nao_chega_a_voz(self) -> None:
        resposta = '{"status": "ok", "count": 3, "ficheiro": "jarvis/app.py"}'
        falado = resumo_falado(resposta)
        self.assertNotIn("{", falado)
        self.assertNotIn('"status"', falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_diff_de_git_nao_chega_a_voz(self) -> None:
        resposta = (
            "diff --git a/jarvis/app.py b/jarvis/app.py\n"
            "index 1234567..89abcde 100644\n"
            "--- a/jarvis/app.py\n"
            "+++ b/jarvis/app.py\n"
            "@@ -10,7 +10,7 @@\n"
            "-linha antiga\n"
            "+linha nova"
        )
        falado = resumo_falado(resposta)
        self.assertNotIn("diff --git", falado)
        self.assertNotIn("@@", falado)
        self.assertNotIn("linha antiga", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_caminho_do_windows_nao_chega_a_voz(self) -> None:
        resposta = r"C:\Users\exemplo\projetos\jarvis\app.py"
        falado = resumo_falado(resposta)
        self.assertNotIn("Users", falado)
        self.assertNotIn("\\", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_traceback_nao_chega_a_voz(self) -> None:
        resposta = (
            "Traceback (most recent call last):\n"
            '  File "jarvis/app.py", line 120, in responder\n'
            "    resposta = self.canal.perguntar(texto, LIMITE_CLAUDE_S)\n"
            "ValueError: falha ao ligar ao Claude Code"
        )
        falado = resumo_falado(resposta)
        self.assertNotIn("Traceback", falado)
        self.assertNotIn("ValueError", falado)
        self.assertNotIn("self.canal", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_resposta_vazia_nao_fica_em_silencio(self) -> None:
        # ficar a espera/calado nunca e resposta
        for vazia in ("", "   ", "\n\n"):
            with self.subTest(vazia=repr(vazia)):
                falado = resumo_falado(vazia)
                self.assertEqual(falado, FRASE_RECURSO_SEM_TEXTO)
                self.assertTrue(falado)

    def test_resposta_so_com_codigo_nao_chega_a_voz(self) -> None:
        resposta = '```python\nprint("oi")\n```'
        falado = resumo_falado(resposta)
        self.assertNotIn("print", falado)
        self.assertNotIn("```", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)


class TestNuncaTruncaParaDentroDoProibido(unittest.TestCase):
    """D59.2: um pedaco proibido sai inteiro, nunca cortado a meio."""

    def test_tag_no_fim_de_uma_resposta_longa_desaparece_inteira(self) -> None:
        resposta = (
            "O trabalho ficou concluído com sucesso, todos os testes passaram sem problemas.\n"
            '<invoke name="Bash">\n<parameter name="command">rm -rf /</parameter>\n</invoke>'
        )
        falado = resumo_falado(resposta)
        self.assertNotIn("<", falado)
        self.assertNotIn("invoke", falado)
        self.assertNotIn("rm -rf", falado)
        self.assertIn("trabalho ficou concluído", falado)

    def test_bloco_de_tres_crases_por_fechar_descarta_o_resto(self) -> None:
        # uma crase tripla sem fecho: nao se sabe se o que vem a seguir e seguro, por isso
        # descarta-se tudo a partir dali em vez de arriscar ler codigo por engano
        resposta = "Segue o ficheiro:\n```python\ndef f():\n    return 1"
        falado = resumo_falado(resposta)
        self.assertNotIn("def f()", falado)
        self.assertNotIn("```", falado)


class TestLimitesDeCaracteres(unittest.TestCase):
    """D59.3: 200 de conteudo, 280 no total, corte no fim de frase ou de palavra."""

    def test_constantes_da_decisao(self) -> None:
        self.assertEqual(MAXIMO_CARACTERES_FALADOS, 200)
        self.assertEqual(MAXIMO_ABSOLUTO_FALADO, 280)

    def test_corta_no_fim_de_uma_palavra_sem_ponto_final(self) -> None:
        falado = resumo_falado("palavra " * 50, limite=20)
        corpo = falado[len(PREFIXO_DA_RESPOSTA_DO_CLAUDE) + 1 :]
        self.assertTrue(corpo.endswith("..."))
        self.assertLessEqual(len(corpo), 24)
        self.assertNotIn("palav.", corpo)

    def test_corta_no_fim_de_frase_quando_ha_um_ponto_final_na_janela(self) -> None:
        resposta = "Primeira frase completa. " + ("palavra " * 40)
        falado = resumo_falado(resposta, limite=30)
        corpo = falado[len(PREFIXO_DA_RESPOSTA_DO_CLAUDE) + 1 :]
        self.assertEqual(corpo, "Primeira frase completa.")

    def test_resposta_dentro_do_limite_nunca_falado_e_como_o_conteudo_falavel(self) -> None:
        resposta = "Está tudo feito."
        self.assertEqual(
            resumo_falado(resposta), f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Está tudo feito."
        )

    def test_conteudo_falado_nunca_passa_dos_duzentos_caracteres(self) -> None:
        resposta = "frase natural sem tags nem código. " * 20
        falado = resumo_falado(resposta)
        corpo = falado[len(PREFIXO_DA_RESPOSTA_DO_CLAUDE) + 1 :]
        self.assertLessEqual(len(corpo), MAXIMO_CARACTERES_FALADOS + len("..."))

    def test_teto_absoluto_com_prefixo_nunca_e_ultrapassado(self) -> None:
        resposta = "frase natural sem tags nem código. " * 20
        falado = resumo_falado(resposta)
        self.assertLessEqual(len(falado), MAXIMO_ABSOLUTO_FALADO)

    def test_cortar_no_limite_nunca_parte_uma_palavra_ao_meio(self) -> None:
        self.assertEqual(cortar_no_limite("palavra completa aqui", 100), "palavra completa aqui")
        original = "uma frase bastante longa sem pontuacao nenhuma"
        cortado = cortar_no_limite(original, 15)
        self.assertTrue(cortado.endswith("..."))
        corpo_sem_reticencias = cortado[: -len("...")]
        for palavra in corpo_sem_reticencias.split(" "):
            self.assertIn(palavra, original.split(" "), f"{palavra!r} nao e uma palavra inteira")


class TestPrefixoDeOrigem(unittest.TestCase):
    """D48.4/D59.5: quem fala nomeia a origem e diz que nao esta verificada."""

    def test_prefixo_diz_de_quem_e_a_frase(self) -> None:
        self.assertIn("Claude", PREFIXO_DA_RESPOSTA_DO_CLAUDE)

    def test_prefixo_diz_que_nao_esta_verificada(self) -> None:
        self.assertIn("não verificada", PREFIXO_DA_RESPOSTA_DO_CLAUDE)

    def test_toda_a_resposta_falada_comeca_pelo_prefixo(self) -> None:
        for resposta in ("Está tudo bem.", "", "   ", '{"a": 1}'):
            with self.subTest(resposta=repr(resposta)):
                self.assertTrue(resumo_falado(resposta).startswith(PREFIXO_DA_RESPOSTA_DO_CLAUDE))


class TestFraseDeRecurso(unittest.TestCase):
    """D59.4: o silencio nunca e a resposta; a frase de recurso diz onde esta o resto."""

    def test_as_duas_frases_de_recurso_dizem_onde_esta_a_resposta_completa(self) -> None:
        self.assertIn("consola", FRASE_RECURSO_SEM_TEXTO)
        self.assertIn("consola", FRASE_RECURSO_SO_TECNICO)

    def test_as_duas_frases_de_recurso_sao_diferentes(self) -> None:
        self.assertNotEqual(FRASE_RECURSO_SEM_TEXTO, FRASE_RECURSO_SO_TECNICO)


class TestTextoFalavelEFuncaoPura(unittest.TestCase):
    """texto_falavel nao tem estado nem efeitos secundarios: mesma entrada, mesma saida."""

    def test_e_deterministica(self) -> None:
        resposta = "Uma frase normal.\n<invoke name=\"Bash\"></invoke>\nOutra frase normal."
        self.assertEqual(texto_falavel(resposta), texto_falavel(resposta))

    def test_frase_totalmente_natural_fica_intacta(self) -> None:
        self.assertEqual(
            texto_falavel("Isto é uma frase perfeitamente normal, sem nada de especial."),
            "Isto é uma frase perfeitamente normal, sem nada de especial.",
        )


class TestTagsSemFechoNaMesmaLinha(unittest.TestCase):
    """Regressao: tags sem fecho na mesma linha.

    O filtro de entao exigia o `>` de fecho na MESMA linha, por isso `<invoke`, `<parameter`,
    `<function`, `<thinking` e `</invoke` sozinhos chegavam a voz. Uma resposta do Claude Code
    cortada a meio de uma chamada de ferramenta (streaming truncado, limite de tokens) acaba
    exatamente assim. Cada uma destas afirmacoes falhava no codigo anterior.
    """

    def test_inicio_de_tag_sem_fecho_nunca_chega_a_voz(self) -> None:
        inicios = ("<invoke", "<parameter", "<function", "<thinking", "</invoke", "< div")
        for inicio in inicios:
            with self.subTest(inicio=inicio):
                falado = resumo_falado(f"Feito.\n{inicio}")
                self.assertEqual(falado, f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Feito.")
                self.assertNotIn(inicio.strip("<"), falado)
                self.assertNotIn("<", falado)

    def test_tag_partida_em_linhas_sai_inteira_sem_deixar_o_atributo(self) -> None:
        # `name="Bash">` sozinho nao e uma tag, mas tambem nao e linguagem natural
        falado = resumo_falado('Ok.\n<invoke\nname="Bash">\n</invoke')
        self.assertEqual(falado, f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Ok.")

    def test_linha_so_com_o_maior_que_do_fecho_nao_chega_a_voz(self) -> None:
        falado = resumo_falado("Ok.\n</invoke\n>")
        self.assertEqual(falado, f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Ok.")
        self.assertNotIn(">", falado)


class TestJuncaoDepoisDoFiltroNaoRemontaProibido(unittest.TestCase):
    """Regressao: o mesmo defeito outra vez, pela porta do lado.

    `texto_falavel` junta as linhas sobreviventes com um espaco DEPOIS de filtrar; no codigo anterior
    nada revalidava o resultado, por isso duas linhas inocentes uma a uma voltavam a formar
    `<invoke name="Bash">` — literalmente a linha 652 do log, lida em voz alta.
    """

    def test_a_tag_da_linha_652_nunca_se_remonta_depois_do_filtro(self) -> None:
        falado = resumo_falado('Esta tudo bem <invoke\nname="Bash">')
        self.assertNotIn('<invoke name="Bash">', falado)
        self.assertNotIn("<", falado)
        self.assertNotIn("invoke", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_linha_em_branco_no_meio_nao_abre_excecao(self) -> None:
        # a linha em branco fecha o bloco proibido; a revalidacao do texto junto e que apanha
        falado = resumo_falado('Esta tudo bem <invoke\n\nname="Bash">')
        self.assertNotIn("Bash", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_texto_falavel_devolve_vazio_quando_a_juncao_fica_proibida(self) -> None:
        self.assertEqual(texto_falavel('Esta tudo bem <invoke\nname="Bash">'), "")

    def test_texto_proibido_ve_o_que_o_filtro_por_linha_nao_ve(self) -> None:
        self.assertTrue(texto_proibido('Esta tudo bem <invoke name="Bash">'))
        self.assertTrue(texto_proibido("Ve em https://exemplo.pt/guia para saber mais"))
        self.assertFalse(texto_proibido("Está tudo feito, os testes passaram todos."))

    def test_o_que_sai_de_texto_falavel_nunca_e_proibido(self) -> None:
        respostas = [
            'Esta tudo bem <invoke\nname="Bash">',
            "Correu bem.\nTraceback (most recent call last):\nValueError: x",
            "Tudo ok.\n$ git status\nlimpo",
            "Feito.\n<parameter",
        ]
        for resposta in respostas:
            with self.subTest(resposta=resposta):
                self.assertFalse(texto_proibido(texto_falavel(resposta)))


class TestCorteSoEmFimDeFraseVerdadeiro(unittest.TestCase):
    """Regressao: corte so em fim de frase verdadeiro.

    `rfind(".")` nao verificava que o ponto era fim de frase: `A versao 3.14` cortava em
    `A versao 3.` ("a versão três vírgula" falado) e `jarvis.app` em `jarvis.`. O criterio diz
    "nunca a meio de uma palavra". Cada afirmacao destas falhava no codigo anterior.
    """

    def _afirmar_palavras_inteiras(self, original: str, cortado: str) -> None:
        palavras_originais = original.split(" ")
        corpo = cortado[: -len("...")] if cortado.endswith("...") else cortado
        for palavra in corpo.split(" "):
            self.assertIn(
                palavra,
                palavras_originais,
                f"{palavra!r} nao e uma palavra inteira de {original!r}",
            )

    def test_o_ponto_de_um_numero_decimal_nao_e_fim_de_frase(self) -> None:
        original = "A versao 3.14 do Python trouxe melhorias grandes de desempenho"
        cortado = cortar_no_limite(original, 20)
        self.assertNotEqual(cortado, "A versao 3.")
        self.assertFalse(cortado.endswith("3."))
        self._afirmar_palavras_inteiras(original, cortado)

    def test_o_ponto_de_um_identificador_nao_e_fim_de_frase(self) -> None:
        original = "Ver o modulo jarvis.app que faz o encaminhamento todo do sistema"
        cortado = cortar_no_limite(original, 25)
        self.assertNotEqual(cortado, "Ver o modulo jarvis.")
        self.assertFalse(cortado.endswith("jarvis."))
        self._afirmar_palavras_inteiras(original, cortado)

    def test_ponto_seguido_de_espaco_continua_a_ser_fim_de_frase(self) -> None:
        self.assertEqual(
            cortar_no_limite("Primeira frase. Segunda frase bem mais longa.", 20),
            "Primeira frase.",
        )

    def test_ponto_final_no_fim_do_texto_conta_como_fim_de_frase(self) -> None:
        original = "Acabei agora. E ainda sobrou tempo"
        self.assertEqual(cortar_no_limite(original, 14), "Acabei agora.")

    def test_exclamacao_e_interrogacao_tambem_fecham_frase(self) -> None:
        self.assertEqual(
            cortar_no_limite("Já está! Agora é contigo, sem pressa nenhuma.", 20), "Já está!"
        )
        self.assertEqual(
            cortar_no_limite("Queres ver? Abre a consola e olha para o fim.", 20), "Queres ver?"
        )

    def test_numero_decimal_falado_por_inteiro_quando_cabe(self) -> None:
        falado = resumo_falado("A versao 3.14 do Python está instalada.")
        self.assertIn("3.14", falado)


class TestFiltroNaoComeLinguagemNatural(unittest.TestCase):
    """Um filtro que come tudo tambem e um defeito: calaria o jarvis.

    Estas frases sao o tipo de resposta que o Claude Code da em portugues normal e tem de
    chegar a voz INTEIRAS, incluindo numeros com ponto decimal, dois pontos e horas.
    """

    FRASES = (
        "Já corri os testes e está tudo verde, 228 passaram sem falhas.",
        "Acrescentei o filtro e a frase de recurso; falta só rever contigo.",
        "A versão 3.14 do Python está instalada e é a que o projeto usa.",
        "Encontrei dois problemas: o primeiro é fácil, o segundo leva mais tempo.",
        "Não consegui reproduzir o erro. Queres que tente outra vez com mais detalhe?",
        "O trabalho ficou feito às 15 horas e 30 minutos, como pediste.",
    )

    def test_frases_normais_chegam_inteiras_a_voz(self) -> None:
        for frase in self.FRASES:
            with self.subTest(frase=frase):
                falado = resumo_falado(frase)
                self.assertEqual(falado, f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} {frase}")


class TestCategoriasExtraDeExclusao(unittest.TestCase):
    """Os casos de exclusao que o codigo anterior deixava passar por acaso ou nao apanhava."""

    def test_caminho_unc_sem_extensao_nao_chega_a_voz(self) -> None:
        self.assertEqual(resumo_falado(r"Ficou em \\servidor\partilha"), FRASE_RECURSO_SO_TECNICO)

    def test_url_sem_esquema_nao_chega_a_voz(self) -> None:
        self.assertEqual(
            resumo_falado("Vê em www.exemplo.com para saber mais."), FRASE_RECURSO_SO_TECNICO
        )

    def test_nome_de_ficheiro_sem_pasta_nao_chega_a_voz(self) -> None:
        self.assertEqual(
            resumo_falado("Alterei o app.py e corri os testes."), FRASE_RECURSO_SO_TECNICO
        )

    def test_atributo_de_tag_solto_nao_chega_a_voz(self) -> None:
        self.assertEqual(resumo_falado('name="Bash"'), FRASE_RECURSO_SO_TECNICO)


class TestCaminhoRealDaRespostaDoClaude(unittest.TestCase):
    """O contrato tem de estar LIGADO ao caminho da resposta do Claude Code (criterio 1), e o
    log tem de continuar a levar a resposta INTEIRA em bruto (criterio 6).

    Usa o mesmo arnes sem hardware dos testes do orquestrador (`tests/test_app.py`): canal,
    voz e accoes falsos, nenhum GPU, nenhum microfone, nenhum Piper.
    """

    def _jarvis_que_responde(self, resposta: str):
        from tests.test_app import JarvisDeTeste  # import local: nao prende os outros testes

        return JarvisDeTeste(resposta_do_claude=resposta)

    def test_a_resposta_da_linha_650_nunca_chega_as_colunas(self) -> None:
        resposta = FIXTURE_LINHA_650.read_text(encoding="utf-8")
        teste = self._jarvis_que_responde(resposta)
        teste.frase("Pergunta ao claude uma coisa")

        self.assertEqual(teste.falados, [FRASE_RECURSO_SO_TECNICO])
        falado = teste.falados[0]
        for proibido in ("<invoke", "invoke", "parameter", "git status", "git diff", "<", ">"):
            self.assertNotIn(proibido, falado)

    def test_o_log_continua_a_levar_a_resposta_inteira_em_bruto(self) -> None:
        resposta = FIXTURE_LINHA_650.read_text(encoding="utf-8")
        teste = self._jarvis_que_responde(resposta)
        teste.frase("Pergunta ao claude uma coisa")

        texto_do_log = teste.log.texto()
        self.assertIn(repr(resposta), texto_do_log)
        self.assertIn("git status --short && git diff --stat", texto_do_log)

    def test_frase_natural_do_claude_e_falada_com_o_prefixo_de_origem(self) -> None:
        teste = self._jarvis_que_responde("Está tudo feito, os testes passaram todos.")
        teste.frase("Pergunta ao claude uma coisa")
        self.assertEqual(
            teste.falados,
            [f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Está tudo feito, os testes passaram todos."],
        )

    def test_resposta_gigante_sem_um_espaco_nao_parte_uma_palavra_ao_meio(self) -> None:
        from jarvis.resposta_falada import FRASE_RECURSO_SEM_CORTE_SEGURO

        teste = self._jarvis_que_responde("ok")
        registo = teste.frase("Pergunta ao claude uma coisa")
        teste.falados.clear()
        teste.jarvis.responder("a" * (MAXIMO_ABSOLUTO_FALADO + 50), registo)
        self.assertEqual(teste.falados, [FRASE_RECURSO_SEM_CORTE_SEGURO])


class TestVarreduraPorCategoria(unittest.TestCase):
    r"""A lista de exclusao e de TOKENS, nao de casos bem formados.

    Duas versoes anteriores falharam pela MESMA forma de defeito: os padroes estavam
    escritos para a forma bonita de cada categoria (`<invoke name="x">`, `$ git status`,
    `www.exemplo.com`) e o que o Claude Code manda vem truncado, partido em linhas e sem fecho.
    Esta classe percorre as categorias de exclusao uma a uma com variantes MALFORMADAS ou PARCIAIS
    — as que uma resposta cortada a meio produz de facto — e exige que nenhuma chegue a voz.
    Cada caso e colado a uma frase natural ("Feito."), por isso a unica saida aceite e ou so a
    frase natural, ou a frase de recurso: nunca o token tecnico.
    """

    def _afirmar_que_nada_chega_a_voz(self, categoria: str, variantes: tuple[str, ...]) -> None:
        for variante in variantes:
            with self.subTest(categoria=categoria, variante=variante):
                falado = resumo_falado(f"Feito.\n{variante}")
                self.assertIn(
                    falado,
                    (f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Feito.", FRASE_RECURSO_SO_TECNICO),
                    f"{variante!r} chegou a voz: {falado!r}",
                )

    def test_1_tags_xml_html_malformadas(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "tags",
            (
                "<invok",  # tag cortada a meio do nome
                "</",  # so o inicio de um fecho
                "/>",  # so o fim de uma tag que se auto-fecha
                "&lt;invoke&gt;",  # a mesma tag, escapada em entidades HTML
                "<INVOKE NAME=",  # em maiusculas e sem fechar o atributo
                'name=Bash>',  # atributo sem aspas, o resto de uma tag partida em linhas
                "<thinking",
                "<!-- comentario -->",
            ),
        )

    def test_2_blocos_de_tres_crases_mal_formados(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "crases",
            (
                "``` def f(",  # cerca por fechar, com codigo cortado
                "```py",  # cerca com linguagem e sem corpo
                "````",  # quatro crases
                "`` print(1) ``",  # cerca de duas crases
                '"""',  # a mesma cerca em aspas triplas de Python
                "'" * 3,
            ),
        )

    def test_3_json_e_dicionarios_por_fechar(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "json",
            (
                '{"ok": true',  # objeto por fechar
                '"description":',  # so a chave, o valor ficou na linha seguinte
                "{ok: 1",  # sem aspas nenhumas
                "[1, 2, 3",  # lista por fechar
                "}",  # so o fecho
                "key: value,",  # par chave-valor estilo YAML
                "( 'a', 1 )",  # tuplo
                "dict(a=1)",
            ),
        )

    def test_4_diffs_e_saidas_de_git_e_comandos(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "diff/git",
            (
                "@@-1,2+3,4@@",  # cabecalho de hunk sem os espacos
                "---",  # regua sozinha
                "+ linha nova",  # linha de diff com espaco a seguir ao sinal
                " M jarvis/app.py",  # git status --short
                "?? tests/fixtures/",
                "On branch main",
                "nothing to commit, working tree clean",
                "1 file changed, 2 insertions(+), 1 deletion(-)",
                "1d707e3 Adiciona amostra inglesa",  # git log --oneline
                "git status --short",
                "Fast-forward",
            ),
        )

    def test_5_caminhos_de_ficheiro_em_todas_as_formas(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "caminhos",
            (
                "c:/Users/exemplo",  # Windows com barra normal
                "./scripts",  # relativo sem extensao
                "~/bin",
                "src/jarvis/",  # so pastas, acaba em barra
                ".gitignore",  # ficheiro oculto, sem extensao
                "..\\..\\x",
                "/usr/local/bin",
                "Esta em C:",  # o que sobra quando o caminho se parte em linhas
                "requirements.txt",
                "Dockerfile",
            ),
        )

    def test_6_linhas_comecadas_por_cifrao_ou_maior_que(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "prompt",
            (
                ">dir",  # sem espaco a seguir ao simbolo (um buraco anterior)
                ">>> print(1)",
                "$env:PATH",
                "$PWD",
                "$HOME/bin",
                "Corre $env:PATH agora",  # variavel de shell no meio da frase
                "2>&1",
                "%USERPROFILE%",
                "$(pwd)",
            ),
        )

    def test_7_tabelas_markdown_incompletas(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "tabelas",
            (
                "|a|b",  # sem a barra final
                "Tabela: | a | b |",  # a linha nao comeca pela barra
                "| coluna",  # so a primeira celula
                "a | b | c",  # sem barras nas pontas
                "|",
            ),
        )

    def test_8_urls_com_e_sem_esquema(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "urls",
            (
                "Ve em publico.pt hoje.",  # .pt, o dominio do utilizador
                "exemplo.pt/guia",
                "exemplo.co.uk",
                "https:/exemplo",  # esquema com uma barra so
                "http://",
                "PUBLICO.PT",
                "sub.exemplo.io/x",
                "github.com/exemplo/jarvis",
            ),
        )

    def test_9_tracebacks_e_linhas_soltas_de_traceback(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "tracebacks",
            (
                "Traceback (most recent call last)",  # sem os dois pontos
                "KeyboardInterrupt",  # nao acaba em "Error" nem em "Exception"
                "During handling of the above exception, another exception occurred:",
                '  File "x", line 3, in <module>',
                "    raise ValueError",
                "  at processTicksAndRejections (node:internal/process)",  # traceback de JS
                "  ... 3 more",
            ),
        )

    def test_10_o_resto_saidas_de_consola_e_de_arneses(self) -> None:
        self._afirmar_que_nada_chega_a_voz(
            "resto",
            (
                "\x1b[31mERRO\x1b[0m",  # cores de terminal
                "====",
                "E   AssertionError",
                "resumo_falado(texto)",  # identificador em snake case
                "npm run check",
                "Ran 251 tests in 1.148s",
                "FAILED (failures=19)",
                "2026-09-20 10:09:12 frase 1",  # linha de log com timestamp
                "a\tb\tc",  # saida em colunas
                "## Resumo",  # marcador de titulo markdown
                "# comentario",
            ),
        )

    def test_o_paragrafo_por_baixo_de_um_titulo_markdown_continua_a_falar_se(self) -> None:
        # o filtro tira a marcacao, nao a resposta: so o titulo sai, o texto por baixo fica
        self.assertEqual(
            resumo_falado("## Resumo\n\nEstá tudo feito e os testes passaram."),
            f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Está tudo feito e os testes passaram.",
        )

    def test_11_a_juncao_das_linhas_nao_remonta_nenhuma_categoria(self) -> None:
        # a categoria que falta e a que nao esta na lista: o texto que so fica proibido DEPOIS
        # de as linhas sobreviventes serem juntas (foi assim que a linha 652 do log aconteceu)
        self._afirmar_que_nada_chega_a_voz(
            "juncao",
            (
                '<invoke\nname="Bash">',
                "https\n://exemplo.pt/guia",
                "C:\n\\Users\\exemplo",
                "{\n'chave': 1\n}",
            ),
        )


class TestCasosExatosDaRevisao(unittest.TestCase):
    """Os casos reproduzidos numa revisao de uma versao anterior, um a um.

    Cada um destes era falado em voz alta pelo codigo anterior: `_PADRAO_PROMPT` exigia um
    espaco a seguir ao `$`/`>`, e a lista de dominios de topo nao tinha `.pt`.
    """

    CASOS_DE_CONSOLA = (">dir", ">>> print(1)", "$env:PATH", "$PWD", "$HOME/bin")
    CASOS_DE_URL = (
        "Ve em publico.pt hoje.",
        "Ve em exemplo.pt/guia para saber mais.",
        "Ve em exemplo.co.uk hoje.",
    )

    def test_sintaxe_de_consola_nunca_e_lida_em_voz_alta(self) -> None:
        for caso in self.CASOS_DE_CONSOLA:
            with self.subTest(caso=caso):
                self.assertEqual(resumo_falado(caso), FRASE_RECURSO_SO_TECNICO)
                self.assertTrue(texto_proibido(caso))

    def test_urls_sem_esquema_nunca_sao_lidos_em_voz_alta(self) -> None:
        for caso in self.CASOS_DE_URL:
            with self.subTest(caso=caso):
                self.assertEqual(resumo_falado(caso), FRASE_RECURSO_SO_TECNICO)
                self.assertTrue(texto_proibido(caso))

    def test_a_frase_natural_a_volta_do_prompt_sobrevive(self) -> None:
        # o pedaco proibido sai INTEIRO e o que era linguagem natural continua a falar-se
        self.assertEqual(resumo_falado("Feito.\n>dir"), f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Feito.")

    def test_o_url_partido_em_linhas_nao_se_remonta_na_juncao(self) -> None:
        falado = resumo_falado("Ve em https\n://exemplo.pt/guia")
        self.assertNotIn("://", falado)
        self.assertNotIn("exemplo", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_o_caminho_partido_em_linhas_nao_se_remonta_na_juncao(self) -> None:
        falado = resumo_falado("Esta em C:\n\\Users\\exemplo")
        self.assertNotIn("\\", falado)
        self.assertNotIn("Users", falado)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)

    def test_entidades_html_e_barras_verticais_soltas_tambem_saem(self) -> None:
        # observacao 5 do veredicto: nao estavam na lista enumerada, mas sao a mesma categoria
        self.assertEqual(resumo_falado("Feito. &lt;invoke&gt;"), FRASE_RECURSO_SO_TECNICO)
        self.assertEqual(resumo_falado("Tabela: | a"), FRASE_RECURSO_SO_TECNICO)


class TestDesempenhoDoFiltro(unittest.TestCase):
    """O filtro nunca pode deixar o jarvis "a espera".

    Os padroes de uma versao anterior faziam retrocesso quadratico dentro de um token muito longo: uma
    linha unica de 30 000 caracteres demorava 22,5 s a filtrar (medido) e 200 000
    caracteres demoravam 319,97 s. Uma resposta assim do Claude Code deixava o jarvis calado
    durante meio minuto ou mais, que e precisamente o falhanco que o perfil proibe. O teto por
    palavra (`MAXIMO_CARACTERES_POR_PALAVRA`) corta isso antes de qualquer padrao correr.
    """

    LIMITE_DE_TEMPO_S = 1.0

    def _medir(self, texto: str) -> tuple[str, float]:
        inicio = time.perf_counter()
        falado = resumo_falado(texto)
        return falado, time.perf_counter() - inicio

    def test_linha_unica_de_30k_caracteres_e_tratada_em_tempo_trivial(self) -> None:
        falado, decorrido = self._medir("a" * 30_000)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)
        self.assertLess(
            decorrido,
            self.LIMITE_DE_TEMPO_S,
            f"30 000 caracteres numa linha demoraram {decorrido:.3f}s (antes: 22,5s)",
        )

    def test_linha_unica_de_200k_caracteres_tambem(self) -> None:
        falado, decorrido = self._medir("a" * 200_000)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)
        self.assertLess(
            decorrido,
            self.LIMITE_DE_TEMPO_S,
            f"200 000 caracteres numa linha demoraram {decorrido:.3f}s (antes: 319,97s)",
        )

    def test_uma_palavra_gigante_nao_arrasta_a_frase_natural_da_mesma_resposta(self) -> None:
        falado, decorrido = self._medir("Está tudo feito.\n" + "x" * 30_000)
        self.assertEqual(falado, f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Está tudo feito.")
        self.assertLess(decorrido, self.LIMITE_DE_TEMPO_S)

    def test_uma_palavra_maior_do_que_a_frase_falada_nunca_e_falavel(self) -> None:
        # a regra que sustenta o teto: ela nem caberia numa resposta falada (nao ha corte seguro)
        self.assertTrue(texto_proibido("x" * (MAXIMO_CARACTERES_POR_PALAVRA + 1)))
        self.assertEqual(texto_falavel("x" * (MAXIMO_CARACTERES_POR_PALAVRA + 1)), "")
        self.assertEqual(cortar_no_limite("x" * 500, MAXIMO_CARACTERES_FALADOS), "")

    def test_uma_linha_de_muitas_palavras_curtas_acima_de_2000_caracteres_tambem_e_proibida(
        self,
    ) -> None:
        # o teto por palavra (MAXIMO_CARACTERES_POR_PALAVRA) nao apanha este caso: nenhuma
        # palavra individual excede 200 caracteres, mas a LINHA inteira excede o teto de ~2000
        # pedido explicitamente pelo veredicto, alem do teto por palavra. Cada "palavra" e
        # adversarial (perto de casar com _PADRAO_CAMINHO_POSIX) para provar que o corte por
        # linha acontece ANTES de qualquer padrao correr.
        palavra_adversarial = "a" * 15 + "." + "b" * 10  # 26 caracteres, bem abaixo do teto
        self.assertLessEqual(len(palavra_adversarial), MAXIMO_CARACTERES_POR_PALAVRA)
        linha = (palavra_adversarial + " ") * 100  # ~2700 caracteres, numa LINHA so
        self.assertGreater(len(linha), MAXIMO_CARACTERES_POR_LINHA)
        self.assertEqual(texto_falavel(linha), "")
        self.assertEqual(resumo_falado(linha), FRASE_RECURSO_SO_TECNICO)

    def test_a_linha_gigante_sai_sozinha_e_a_prosa_ao_lado_continua_falada(self) -> None:
        # D59.2 (o pedaco proibido sai INTEIRO, o resto fica): o teto e por LINHA, por isso a
        # linha gigante desaparece e a frase normal da mesma resposta continua a ser dita.
        linha_gigante = ("a" * 15 + "." + "b" * 10 + " ") * 100
        texto = "Está tudo feito.\n\n" + linha_gigante
        self.assertEqual(
            resumo_falado(texto), f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Está tudo feito."
        )

    def test_uma_linha_de_prosa_natural_quase_no_teto_continua_falavel(self) -> None:
        # A versao anterior deste teste usava uma frase de 59 caracteres,
        # tres ordens de grandeza abaixo da fronteira, e por isso nao provava nada sobre o teto.
        # Uma LINHA de prosa limpa logo ABAIXO de MAXIMO_CARACTERES_POR_LINHA tem de passar.
        frase = "Corri os testes todos e esta tudo verde outra vez. "  # 51 caracteres
        linha = (frase * 39).strip()  # ~1950 caracteres, ainda dentro do teto por linha
        self.assertLess(len(linha), MAXIMO_CARACTERES_POR_LINHA)
        self.assertGreater(len(linha), MAXIMO_CARACTERES_POR_LINHA - 100)
        self.assertEqual(texto_falavel(linha), linha)
        falado = resumo_falado(linha)
        self.assertNotEqual(falado, FRASE_RECURSO_SO_TECNICO)
        self.assertTrue(falado.startswith(f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Corri os testes"))

    def test_linha_de_1m_de_caracteres_feita_de_muitas_palavras_curtas_e_tratada_em_tempo_trivial(
        self,
    ) -> None:
        # antes do teto por linha, este caso (muitas palavras curtas, nenhuma isolada acima do
        # teto por palavra) nao era coberto pelo teto por palavra: crescia linearmente ate
        # perto de 1s no milhao de caracteres porque os 28 padroes corriam sobre a linha inteira.
        # Com o teto por linha a linha sai logo na primeira verificacao, antes de qualquer padrao.
        palavra_adversarial = "a" * 15 + "." + "b" * 10
        texto = (palavra_adversarial + " ") * 38_000  # ~1 000 000 de caracteres
        falado, decorrido = self._medir(texto)
        self.assertEqual(falado, FRASE_RECURSO_SO_TECNICO)
        self.assertLess(
            decorrido,
            0.1,
            f"1 000 000 de caracteres em muitas palavras curtas demorou {decorrido:.3f}s",
        )


def _prosa_em_paragrafos(comprimento_minimo: int) -> str:
    """Prosa portuguesa limpa, em paragrafos, com pelo menos `comprimento_minimo` caracteres.

    Zero marcas da lista de exclusao: sem tags, sem crases, sem caminhos, sem URLs, sem `$`/`>`,
    sem dois pontos, sem numeros. Cada LINHA fica bem abaixo do teto por linha; e a RESPOSTA
    inteira que passa dos 2000 caracteres.
    """
    paragrafos = [
        "Corri os testes todos e esta tudo verde outra vez.",
        "Depois olhei para a lista de tarefas e vi que faltava tratar a parte da voz.",
        "Vou tratar disso a seguir com calma e sem pressa nenhuma.",
        "Tambem reli as notas antigas para perceber o que tinha ficado por decidir.",
    ]
    linhas: list[str] = []
    while sum(len(linha) + 1 for linha in linhas) < comprimento_minimo:
        linhas.append(paragrafos[len(linhas) % len(paragrafos)])
        linhas.append("")
    return "\n".join(linhas)


class TestTetoPorLinhaNaoEOTetoDaRespostaJunta(unittest.TestCase):
    """Fronteira do teto por linha.

    O teto de ~2000 caracteres e por LINHA do texto de entrada. A juncao das linhas que sobrevivem
    ao filtro e a RESPOSTA inteira, nao uma linha: aplicar-lhe o teto mandava qualquer resposta de
    prosa natural com mais de 2000 caracteres falaveis para a frase de recurso — uma afirmacao
    falsa ("respondeu com codigo ou dados tecnicos" sobre prosa), contra os criterios 2 e 4 da
    D59. Estes casos de fronteira sao os que faltavam a suite.
    """

    def _afirmar_falada_normalmente(self, resposta: str) -> str:
        falavel = texto_falavel(resposta)
        self.assertNotEqual(falavel, "", "prosa limpa nunca fica sem nada falavel")
        self.assertFalse(texto_proibido(falavel))
        falado = resumo_falado(resposta)
        self.assertNotEqual(falado, FRASE_RECURSO_SO_TECNICO)
        self.assertNotEqual(falado, FRASE_RECURSO_SEM_TEXTO)
        self.assertTrue(falado.startswith(f"{PREFIXO_DA_RESPOSTA_DO_CLAUDE} Corri os testes"))
        self.assertLessEqual(len(falado), MAXIMO_ABSOLUTO_FALADO)
        return falado

    def test_prosa_natural_de_1900_caracteres_e_falada_normalmente(self) -> None:
        resposta = _prosa_em_paragrafos(1900)
        falavel = texto_falavel(resposta)
        self.assertGreater(len(falavel), 1900)
        self.assertLess(len(falavel), MAXIMO_CARACTERES_POR_LINHA)
        self._afirmar_falada_normalmente(resposta)

    def test_prosa_natural_acima_do_teto_por_linha_tambem_e_falada_normalmente(self) -> None:
        # ~2100 caracteres falaveis, cada linha bem abaixo do teto: e a resposta junta que passa
        # dos 2000. Este e o caso exato que uma versao anterior mandava para a frase de recurso.
        resposta = _prosa_em_paragrafos(2100)
        falavel = texto_falavel(resposta)
        self.assertGreater(len(falavel), MAXIMO_CARACTERES_POR_LINHA)
        for linha in resposta.splitlines():
            self.assertLess(len(linha), MAXIMO_CARACTERES_POR_LINHA)
        self._afirmar_falada_normalmente(resposta)

    def test_prosa_natural_muito_longa_continua_falada_por_maior_que_seja(self) -> None:
        # nao ha teto nenhum a partir do qual prosa limpa passe a ser "codigo ou dados tecnicos":
        # ela e sempre falada, cortada aos MAXIMO_CARACTERES_FALADOS no fim de uma frase.
        resposta = _prosa_em_paragrafos(20_000)
        self.assertGreater(len(texto_falavel(resposta)), 19_000)
        falado = self._afirmar_falada_normalmente(resposta)
        self.assertTrue(falado.endswith("."), falado)


if __name__ == "__main__":
    unittest.main()
