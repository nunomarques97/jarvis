r"""Testes do encaminhador deterministico (jarvis/router.py), unittest da
biblioteca padrao (sem pytest: nenhuma framework de testes fora da biblioteca
padrao).

Config sempre FICTICIA e em memoria (nunca o config.toml real): os
projetos usados chamam-se "exemplo-um", "exemplo-dois" e "exemplo-bolsa", com
caminhos inventados que nem sequer existem no disco desta maquina — o router
nao toca no sistema de ficheiros, so compara texto, por isso isto e seguro e
representativo.

O que estes testes protegem, alem do caminho feliz:

  * a lista branca da D4 esta FECHADA — uma frase que apenas CONTEM um
    gatilho ("diz ao claude ... sobre o vs code no exemplo-um") vai para o
    Claude Code, nunca vira acao local. Cada uma das frases que ja viraram
    acao local por engano esta aqui como caso proprio;
  * o nome do projeto nao e adivinhado — "exemplo dos", "exemplo doido" e
    "exemplo-dois-privado" nao sao "exemplo-dois" (nunca "o mais
    parecido");
  * as guardas da D5 e a leitura da D12 fixada pela D52;
  * D58a/D58b: as MESMAS cinco acoes reconhecidas em ingles, e a regra
    das duas listas brancas (PT+EN) a funcionar sem nenhuma deteccao de
    lingua — `TestListaBrancaInglesa` cobre as 20 intencoes de
    `tests/voz/frases-en.md`, `TestRegraDasDuasListasD58b` cobre o mecanismo
    de desempate entre as duas listas.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from jarvis import router
from jarvis.config import Config, Projeto
from jarvis.router import encaminhar

CAMINHO_UM = Path("D:/caminho/para/exemplo-um")
CAMINHO_DOIS = Path("D:/caminho/para/exemplo-dois")
CAMINHO_BOLSA = Path("D:/caminho/para/exemplo-bolsa")


def _config_ficticia() -> Config:
    return Config(
        microfone="Microfone Ficticio de Teste",
        projetos=(
            Projeto(nome="exemplo-um", caminho=CAMINHO_UM),
            Projeto(nome="exemplo-dois", caminho=CAMINHO_DOIS),
            Projeto(nome="exemplo-bolsa", caminho=CAMINHO_BOLSA),
        ),
    )


class BaseRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config_ficticia()

    def assertVaiParaClaude(self, frase: str) -> None:
        """A frase e texto para o Claude Code: nunca acao local, nunca perdida."""
        resultado = encaminhar(frase, self.config)
        self.assertEqual(
            resultado.tipo,
            "claude",
            msg=f"{frase!r} devia ir para o Claude Code, obteve "
            f"{resultado.tipo}/{resultado.nome_acao} ({resultado.motivo})",
        )
        self.assertIsNone(resultado.nome_acao)
        self.assertIsNone(resultado.argumento)
        self.assertEqual(resultado.texto, frase)


class TestListaBrancaAcoesLocais(BaseRouter):
    """Os cinco comandos da lista branca fechada da D4 tornam-se acao local."""

    def test_horas_vira_acao_local(self) -> None:
        resultado = encaminhar("que horas são", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")

    def test_data_vira_acao_local(self) -> None:
        resultado = encaminhar("que dia é hoje?", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "data")

    def test_horas_e_data_sao_distinguidas_pelo_argumento(self) -> None:
        # Quem executa tem de saber qual das duas dizer sem reinterpretar a frase.
        self.assertEqual(encaminhar("qual é a hora", self.config).argumento, "horas")
        self.assertEqual(encaminhar("qual é a data de hoje", self.config).argumento, "data")

    def test_horas_maiusculas_e_pontuacao_sao_normalizadas(self) -> None:
        resultado = encaminhar("QUE HORAS SÃO?!", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")

    def test_cortesia_de_fronteira_e_tolerada(self) -> None:
        # "hey jarvis" e "por favor" sao cortesia, nao conteudo extra.
        resultado = encaminhar("hey jarvis, que horas são agora, por favor?", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")

    def test_diz_me_as_horas_continua_a_ser_comando_local(self) -> None:
        # "diz" so bloqueia quando ha destinatario; "diz-me" e o proprio jarvis.
        resultado = encaminhar("diz-me as horas", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")

    def test_calar_vira_acao_local(self) -> None:
        resultado = encaminhar("cala-te", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "calar")

    def test_adormecer_vira_acao_local(self) -> None:
        resultado = encaminhar("adormece jarvis", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "adormecer")

    def test_acordar_vira_acao_local(self) -> None:
        resultado = encaminhar("acorda", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "acordar")

    def test_abrir_vscode_com_projeto_conhecido_vira_acao_local(self) -> None:
        resultado = encaminhar("abre o vs code no exemplo-um", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_vscode")
        self.assertEqual(resultado.argumento, str(CAMINHO_UM))

    def test_abrir_vscode_tolera_um_erro_do_stt_dentro_de_palavra_longa(self) -> None:
        # "exemplu" por "exemplo": um caracter trocado numa palavra longa
        # ainda e o mesmo projeto; "um" tem de continuar exato.
        resultado = encaminhar("abre o vscode do exemplu um", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.argumento, str(CAMINHO_UM))

    def test_abrir_vscode_tolera_a_palavra_projeto_antes_do_nome(self) -> None:
        resultado = encaminhar("abre o vs code no projeto exemplo-dois", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.argumento, str(CAMINHO_DOIS))

    def test_abrir_pasta_com_projeto_conhecido_vira_acao_local(self) -> None:
        resultado = encaminhar("abre a pasta do exemplo-dois", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_pasta")
        self.assertEqual(resultado.argumento, str(CAMINHO_DOIS))

    def test_abrir_pasta_com_cortesia_final_continua_a_resolver_o_projeto(self) -> None:
        resultado = encaminhar("abre a pasta do exemplo-um, por favor", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.argumento, str(CAMINHO_UM))

    def test_acao_local_nunca_leva_texto_de_claude(self) -> None:
        # Contrato do router: uma acao local descreve-se por nome e
        # argumento, nunca carrega o campo `texto` (que e so para o Claude Code).
        resultado = encaminhar("que horas são", self.config)
        self.assertIsNone(resultado.texto)


class TestListaBrancaEstaFechada(BaseRouter):
    """Conter o gatilho nao chega — a frase INTEIRA tem de ser o comando.

    Cada caso e uma frase que uma versao anterior transformou em acao local.
    Todas tem de ir como texto para o Claude Code.
    """

    def test_pedido_ao_claude_que_menciona_o_vs_code_nao_abre_nada(self) -> None:
        self.assertVaiParaClaude(
            "diz ao claude para abrir um ticket sobre o vs code no exemplo-um"
        )

    def test_negacao_nao_abre_o_vs_code(self) -> None:
        self.assertVaiParaClaude("nao abras o vs code no exemplo-um")

    def test_negacao_nao_abre_a_pasta(self) -> None:
        self.assertVaiParaClaude("nao abras a pasta do exemplo-um")

    def test_pergunta_que_contem_cala_nao_cala_o_jarvis(self) -> None:
        self.assertVaiParaClaude("explica-me a funcao que cala os avisos do compilador")

    def test_frase_com_adormecer_dirigida_ao_claude_nao_adormece_o_jarvis(self) -> None:
        self.assertVaiParaClaude("manda o claude adormecer o processo do servidor de testes")

    def test_pergunta_sobre_acordar_o_pc_nao_acorda_o_jarvis(self) -> None:
        self.assertVaiParaClaude("porque e que o pc nao acorda do modo de espera")

    def test_pergunta_ao_claude_com_que_dia_e_nao_vira_horas_e_data(self) -> None:
        self.assertVaiParaClaude("pergunta ao claude que dia e o prazo do relatorio")

    def test_pedido_de_issue_com_que_dia_e_nao_vira_horas_e_data(self) -> None:
        self.assertVaiParaClaude("abre um issue e escreve que dia e amanha")

    def test_resumo_que_pede_a_data_no_fim_nao_vira_horas_e_data(self) -> None:
        self.assertVaiParaClaude(
            "faz um resumo do que mudou hoje e diz-me qual e a data de hoje no fim"
        )

    def test_conversa_que_menciona_abrir_a_pasta_nao_abre_a_pasta(self) -> None:
        self.assertVaiParaClaude(
            "quando acabares de abrir a pasta do exemplo-um manda-me um resumo"
        )


class TestNomeDeProjetoNaoEAdivinhado(BaseRouter):
    """Nunca "o mais parecido". Casos medidos numa versao anterior."""

    def test_nome_parecido_com_palavra_diferente_nao_abre_o_vs_code(self) -> None:
        # "exemplo doido" batia 0.88 com exemplo-dois numa versao anterior.
        self.assertVaiParaClaude("abre o vs code no exemplo doido")

    def test_nome_parecido_numa_palavra_curta_nao_abre_a_pasta(self) -> None:
        # "exemplo dos" batia 0.957 com exemplo-dois numa versao anterior.
        self.assertVaiParaClaude("abre a pasta do exemplo dos")

    def test_projeto_desconhecido_que_contem_um_conhecido_nao_abre_a_pasta(self) -> None:
        # Um nome mais longo que CONTEM um projeto conhecido e outro projeto.
        self.assertVaiParaClaude("abre a pasta do exemplo-dois-privado")

    def test_projeto_conhecido_que_e_prefixo_de_outro_nao_e_confundido(self) -> None:
        resultado = encaminhar("abre a pasta do exemplo-um", self.config)
        self.assertEqual(resultado.argumento, str(CAMINHO_UM))
        self.assertNotEqual(resultado.argumento, str(CAMINHO_DOIS))

    def test_palavra_a_mais_no_nome_do_projeto_vai_para_claude(self) -> None:
        self.assertVaiParaClaude("abre o vs code no exemplo um antigo")


class TestNuncaViraAcaoLocalPorEngano(BaseRouter):
    """D4: frase ambigua ou projeto desconhecido NUNCA produz acao local."""

    def test_frase_ambigua_sem_projeto_vai_para_claude(self) -> None:
        # Casa com o padrao de "abrir o VS Code" mas nao nomeia projeto nenhum:
        # e ambiguo, e ambiguo nunca vira acao local.
        self.assertVaiParaClaude("abre o vs code")

    def test_projeto_desconhecido_vai_para_claude_nunca_local(self) -> None:
        self.assertVaiParaClaude("abre a pasta do projeto-fantasma")

    def test_pergunta_comum_fora_da_lista_branca_vai_para_claude(self) -> None:
        self.assertVaiParaClaude("achas que amanhã vai chover?")

    def test_pedido_de_trabalho_ao_claude_vai_para_claude(self) -> None:
        self.assertVaiParaClaude("corre os testes do exemplo-um e diz-me o que falhou")


class TestGuardaDeTokensEFalsosDespertares(BaseRouter):
    """D5: sem frase valida, o router devolve 'nada' e isso nunca chega ao Claude."""

    def test_transcricao_vazia_devolve_nada(self) -> None:
        resultado = encaminhar("", self.config)
        self.assertEqual(resultado.tipo, "nada")
        self.assertIsNone(resultado.texto)
        self.assertIsNone(resultado.nome_acao)

    def test_transcricao_so_espacos_devolve_nada(self) -> None:
        resultado = encaminhar("    ", self.config)
        self.assertEqual(resultado.tipo, "nada")

    def test_transcricao_de_dois_caracteres_devolve_nada(self) -> None:
        resultado = encaminhar("oi", self.config)
        self.assertEqual(resultado.tipo, "nada")

    def test_transcricao_so_com_pontuacao_devolve_nada(self) -> None:
        resultado = encaminhar("...!?...", self.config)
        self.assertEqual(resultado.tipo, "nada")

    def test_ruido_conhecido_do_stt_devolve_nada(self) -> None:
        # Alucinacao tipica do Whisper sobre um trecho de silencio/ruido.
        resultado = encaminhar("Obrigado por assistir!", self.config)
        self.assertEqual(resultado.tipo, "nada")

    def test_confianca_abaixo_do_limiar_devolve_nada(self) -> None:
        resultado = encaminhar("que horas são", self.config, confianca=0.05)
        self.assertEqual(resultado.tipo, "nada")

    def test_confianca_acima_do_limiar_nao_bloqueia_comando_valido(self) -> None:
        resultado = encaminhar("que horas são", self.config, confianca=0.99)
        self.assertEqual(resultado.tipo, "local")
        self.assertTrue(resultado.confianca_verificada)

    def test_sem_confianca_o_resultado_diz_que_a_guarda_nao_correu(self) -> None:
        # Na cadeia viva e preciso ver no log quando a
        # metade "confianca" da guarda da D5 nao foi aplicada.
        resultado = encaminhar("que horas são", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertFalse(resultado.confianca_verificada)

    def test_transcricao_none_devolve_nada_sem_rebentar(self) -> None:
        resultado = encaminhar(None, self.config)  # type: ignore[arg-type]
        self.assertEqual(resultado.tipo, "nada")


class TestProibicaoPermanenteDeOrdensFinanceiras(BaseRouter):
    """D12, na leitura fixada pela D52: a garantia e a lista branca fechada.

    Nenhuma frase de compra/venda pode virar acao (nao ha acao capaz de
    negociar); e abrir o editor ou a pasta de um projeto da configuracao
    continua a ser acao local, seja qual for o nome do projeto.
    """

    def test_mencao_a_ordem_de_compra_vai_como_texto_para_claude(self) -> None:
        self.assertVaiParaClaude("compra-me duas acoes agora")

    def test_frase_de_venda_vai_como_texto_para_claude(self) -> None:
        self.assertVaiParaClaude("vende as minhas criptomoedas todas")

    def test_mencao_a_corretora_ou_carteira_vai_como_texto_para_claude(self) -> None:
        self.assertVaiParaClaude("qual é o saldo da minha carteira na corretora")

    def test_vocabulario_financeiro_comum_fica_etiquetado_no_motivo(self) -> None:
        # Sem portao, mas o motivo financeiro tem de ficar no log.
        resultado = encaminhar("compramos acoes na bolsa quando o trading acalmar", self.config)
        self.assertEqual(resultado.tipo, "claude")
        self.assertIn("D12", resultado.motivo)

    def test_sintaxe_de_comando_local_com_ordem_de_venda_nao_vira_acao(self) -> None:
        # "abre a pasta" e um gatilho da D4, mas isto nao e o comando da D4.c:
        # tem conteudo extra, logo e texto.
        self.assertVaiParaClaude("abre a pasta para vender bitcoin do exemplo-um")

    def test_abrir_a_pasta_de_projeto_com_nome_financeiro_continua_a_ser_local(self) -> None:
        # D52.5: a D12 proibe negociar, nao proibe abrir o editor ou a pasta de
        # um repositorio da configuracao privada.
        resultado = encaminhar("abre a pasta do exemplo-bolsa", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_pasta")
        self.assertEqual(resultado.argumento, str(CAMINHO_BOLSA))

    def test_abrir_o_vs_code_em_projeto_com_nome_financeiro_continua_a_ser_local(self) -> None:
        resultado = encaminhar("abre o vs code no exemplo-bolsa", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_vscode")
        self.assertEqual(resultado.argumento, str(CAMINHO_BOLSA))


class TestLimpezaDoResiduoDaPalavraDeAtivacao(BaseRouter):
    """Residuo da wake word tirado do INICIO, antes do encaminhamento.

    Casos e citacoes de linha vindos de logs/jarvis-2026-09-20.log (a janela
    do teste real, 10:09-10:20 do dia 20 set 2026) e da especificacao da
    limpeza, onde nao ha uma linha de log para citar.
    """

    def test_jarvis_colado_ao_inicio_vira_acao_local_das_horas(self) -> None:
        # O exemplo da especificacao: "jarvis, que
        # horas sao" falha onde "que horas sao" passa — sem "hey", para
        # provar que o residuo de uma so palavra tambem e removido (nao ha
        # linha de log para este caso: e o exemplo escrito na decisao).
        resultado = encaminhar("Jarvis, que horas sao?", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")
        self.assertEqual(resultado.residuo_removido, "jarvis")

    def test_hey_jarvis_colado_ao_inicio_vira_acao_local_das_horas(self) -> None:
        # logs/jarvis-2026-09-20.log linha 138 e 613 ("diz: 'hey jarvis, que
        # horas sao?'" — o guiao de arranque exibido ao utilizador nesta janela
        # do teste real, a mesma frase que a wake word cola a transcricao).
        resultado = encaminhar("hey jarvis, que horas sao", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")
        self.assertEqual(resultado.residuo_removido, "hey jarvis")

    def test_jorvis_que_oracao_vai_para_claude_e_nao_executa_nada(self) -> None:
        # logs/jarvis-2026-09-20.log linha 647: a transcricao real do teste do
        # utilizador saiu 'Jorvis, que oração!' em vez de 'Jarvis, que horas
        # são!'. PROIBIDO o dicionario de enganos: 'jorvis' e
        # um residuo conhecido da wake word e sai do inicio, mas o que sobra —
        # 'que oracao' — NUNCA e mapeado para 'que horas sao'. Tem de seguir
        # como texto para o Claude Code, sem executar accao nenhuma.
        resultado = encaminhar("Jorvis, que oração!", self.config)
        self.assertEqual(resultado.tipo, "claude")
        self.assertIsNone(resultado.nome_acao)
        self.assertIsNone(resultado.argumento)
        self.assertEqual(resultado.texto, "Jorvis, que oração!")
        self.assertEqual(resultado.residuo_removido, "jorvis")

    def test_hei_jarvis_e_ei_jarvis_tambem_sao_residuo_conhecido(self) -> None:
        # Variantes da lista fechada que nao vieram do log mas estao
        # nomeadas na especificacao.
        resultado_hei = encaminhar("hei jarvis, que horas sao", self.config)
        self.assertEqual(resultado_hei.tipo, "local")
        self.assertEqual(resultado_hei.nome_acao, "horas_e_data")
        self.assertEqual(resultado_hei.residuo_removido, "hei jarvis")

        resultado_ei = encaminhar("ei jarvis, que horas sao", self.config)
        self.assertEqual(resultado_ei.tipo, "local")
        self.assertEqual(resultado_ei.nome_acao, "horas_e_data")
        self.assertEqual(resultado_ei.residuo_removido, "ei jarvis")

    def test_limpeza_nao_come_o_meio_nem_o_fim_da_frase(self) -> None:
        # A limpeza e so do INICIO: um projeto chamado exatamente "jarvis" no
        # MEIO ou no FIM de um comando continua a ser reconhecido tal e qual,
        # e uma frase so entregue ao Claude Code por mencionar "jarvis" a meio
        # continua inteira, sem nada cortado.
        caminho_jarvis = Path("D:/caminho/para/jarvis")
        config_com_projeto_jarvis = Config(
            microfone="Microfone Ficticio de Teste",
            projetos=(Projeto(nome="jarvis", caminho=caminho_jarvis),),
        )
        resultado_pasta = encaminhar("abre a pasta do jarvis", config_com_projeto_jarvis)
        self.assertEqual(resultado_pasta.tipo, "local")
        self.assertEqual(resultado_pasta.nome_acao, "abrir_pasta")
        self.assertEqual(resultado_pasta.argumento, str(caminho_jarvis))
        self.assertIsNone(resultado_pasta.residuo_removido)

        resultado_meio = encaminhar(
            "diz ao claude que o jarvis esta bem", self.config
        )
        self.assertEqual(resultado_meio.tipo, "claude")
        self.assertEqual(resultado_meio.texto, "diz ao claude que o jarvis esta bem")
        self.assertIsNone(resultado_meio.residuo_removido)


class TestListaBrancaInglesa(BaseRouter):
    """D58a: as MESMAS cinco acoes da D4, reconhecidas em ingles.

    As 20 frases desta classe sao as 20 intencoes de `tests/voz/frases-en.md`
    (mesma numeracao, mesmos marcadores `<projeto-1>`/`<projeto-2>`
    substituidos pelos projetos ficticios "exemplo-um"/"exemplo-dois" da
    `_config_ficticia()`). Zero acoes novas: as dez `local` sao as
    mesmas cinco acoes da D4 e as dez `claude` reproduzem os mesmos motivos
    ja testados em portugues (negacao, destinatario explicito, projeto
    desconhecido, vocabulario financeiro generico, gatilho a meio da frase).
    """

    # --- as 10 que viram acao local -----------------------------------

    def test_01_what_time_is_it_vira_horas(self) -> None:
        resultado = encaminhar("what time is it", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")

    def test_02_what_day_is_it_today_vira_data(self) -> None:
        resultado = encaminhar("what day is it today", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "data")

    def test_03_open_vs_code_in_the_projeto_um_vira_abrir_vscode(self) -> None:
        resultado = encaminhar("open vs code in the exemplo-um", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_vscode")
        self.assertEqual(resultado.argumento, str(CAMINHO_UM))

    def test_04_open_the_projeto_dois_folder_vira_abrir_pasta(self) -> None:
        resultado = encaminhar("open the exemplo-dois folder", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_pasta")
        self.assertEqual(resultado.argumento, str(CAMINHO_DOIS))

    def test_05_be_quiet_vira_calar(self) -> None:
        resultado = encaminhar("be quiet", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "calar")

    def test_06_go_to_sleep_vira_adormecer(self) -> None:
        resultado = encaminhar("go to sleep", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "adormecer")

    def test_07_wake_up_vira_acordar(self) -> None:
        resultado = encaminhar("wake up", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "acordar")

    def test_08_tell_me_the_time_vira_horas(self) -> None:
        # "tell" e destinatario explicito, EXCETO quando seguido de "me": o
        # destinatario e o proprio jarvis, tal como "diz-me as horas" em pt.
        resultado = encaminhar("tell me the time", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")

    def test_09_open_vs_code_in_project_projeto_dois_vira_abrir_vscode(self) -> None:
        resultado = encaminhar("open vs code in project exemplo-dois", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "abrir_vscode")
        self.assertEqual(resultado.argumento, str(CAMINHO_DOIS))

    def test_10_whats_todays_date_vira_data(self) -> None:
        # `_normalizar()` tira a pontuacao: "what's today's date" fica
        # "what s today s date" (o apostrofo vira espaco, nunca desaparece).
        resultado = encaminhar("what's today's date", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "data")

    # --- as 10 que tem de ir para o Claude Code -------------------------

    def test_11_tell_claude_com_gatilho_no_meio_vai_para_claude(self) -> None:
        # Destinatario explicito ("tell claude", nao "tell me") E gatilho a
        # meio da frase (D4 fechada): as duas razoes mandam para o Claude.
        self.assertVaiParaClaude(
            "tell claude to open a ticket about vs code in the exemplo-um"
        )

    def test_12_negacao_dont_open_vs_code_vai_para_claude(self) -> None:
        # "don't" fica "don t" depois de `_normalizar()` tirar o apostrofo;
        # a guarda da negacao tem de reconhecer a forma contraida na mesma.
        self.assertVaiParaClaude("don't open vs code in the exemplo-dois")

    def test_13_open_vs_code_sem_projeto_e_ambiguo_vai_para_claude(self) -> None:
        self.assertVaiParaClaude("open vs code")

    def test_14_projeto_desconhecido_ghost_project_vai_para_claude(self) -> None:
        self.assertVaiParaClaude("open the folder for the ghost project")

    def test_15_vocabulario_financeiro_generico_vai_para_claude(self) -> None:
        # "invest"/"stocks", nunca "buy"/"sell"/"order"/"trade" (a
        # lista branca inglesa nao tem nenhum verbo de compra/venda).
        self.assertVaiParaClaude("invest some money in stocks for me right now")

    def test_16_pergunta_comum_vai_para_claude(self) -> None:
        self.assertVaiParaClaude("do you think it will rain tomorrow")

    def test_17_pedido_de_trabalho_vai_para_claude(self) -> None:
        self.assertVaiParaClaude(
            "run the tests for the exemplo-um and tell me what failed"
        )

    def test_18_pedido_geral_vai_para_claude(self) -> None:
        self.assertVaiParaClaude(
            "write a summary of what changed in the exemplo-dois today"
        )

    def test_19_ask_claude_destinatario_explicito_nao_vira_horas_e_data(self) -> None:
        self.assertVaiParaClaude("ask claude what day the report is due")

    def test_20_mencao_a_abrir_a_pasta_no_meio_da_frase_vai_para_claude(self) -> None:
        self.assertVaiParaClaude(
            "when you finish opening the folder for the exemplo-um send me a summary"
        )


class TestCortesiasEResiduoAceitamVariantesInglesas(BaseRouter):
    """D58a: as cortesias de fronteira e a limpeza do residuo da wake word
    tambem aceitam as variantes inglesas, nao so as portuguesas."""

    def test_hey_jarvis_com_frase_inglesa_remove_o_mesmo_residuo(self) -> None:
        # "hey jarvis" ja e a variante que o prefixo da medicao da D53 usa
        # para as duas linguas; aqui confirma-se que o residuo removido e o
        # mesmo quando o resto da frase e ingles.
        resultado = encaminhar("hey jarvis, what time is it", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.residuo_removido, "hey jarvis")

    def test_please_no_inicio_e_no_fim_sao_cortesia_tolerada(self) -> None:
        inicio = encaminhar("please, what time is it", self.config)
        self.assertEqual(inicio.tipo, "local")
        self.assertEqual(inicio.nome_acao, "horas_e_data")

        fim = encaminhar("what time is it, please", self.config)
        self.assertEqual(fim.tipo, "local")
        self.assertEqual(fim.nome_acao, "horas_e_data")

    def test_hi_e_hello_sao_cortesia_inicial_tolerada(self) -> None:
        resultado = encaminhar("hi jarvis, what time is it", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")

    def test_negacao_inglesa_nao_contraida_tambem_bloqueia(self) -> None:
        # A propria D58a nomeia "do not", "never" e "not" — testados aqui a
        # par de "don't" (ja coberta no teste 12 da amostra inglesa).
        self.assertVaiParaClaude("do not open vs code in the exemplo-um")
        self.assertVaiParaClaude("never open vs code in the exemplo-um")


class TestRegraDasDuasListasD58b(BaseRouter):
    """D58(b): a frase e casada contra AS DUAS listas brancas, sem nenhuma
    deteccao de lingua.

    As 20 frases de `TestListaBrancaInglesa` ja provam as duas primeiras
    alineas na pratica: "casa numa so lista" (cada uma das 10 frases `local`
    so bate na lista inglesa; os testes portugueses existentes so batem na
    portuguesa). A terceira alinea — "casa em duas acoes diferentes, vai
    para o Claude Code" — nao tem NENHUMA frase real disponivel para a
    provocar: o vocabulario das duas listas de producao e deliberadamente
    disjunto (nenhuma palavra da lista fechada portuguesa e tambem uma
    palavra da lista fechada inglesa para outra accao, e vice-versa) — e essa
    separacao e uma propriedade de seguranca do desenho, nao um acaso. Por
    isso as duas classes seguintes isolam o MECANISMO com uma tabela extra
    sintetica, injetada so durante o teste (`mock.patch.object`), para provar
    que `encaminhar()` nunca escolhe "a mais provavel" se uma colisao destas
    alguma vez passar a existir (p.ex. com uma sexta accao futura).
    """

    def test_bater_nas_duas_listas_para_a_mesma_accao_executa(self) -> None:
        # Alinea do meio da D58(b): "casa nas duas para a MESMA accao,
        # executa-se". Tabela sintetica: a mesma frase portuguesa "que horas
        # sao" tambem aparece (deliberadamente, so para este teste) na
        # tabela inglesa, mapeada para a MESMA accao/argumento.
        tabela_en_com_duplicado = router._ACOES_BARE_EN + (
            (re.compile(r"que\s+horas\s+sao"), "horas_e_data", "horas"),
        )
        with mock.patch.object(router, "_ACOES_BARE_EN", tabela_en_com_duplicado):
            resultado = encaminhar("que horas sao", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")

    def test_bater_em_duas_listas_para_accoes_diferentes_vai_para_claude(self) -> None:
        # Alinea final da D58(b): "casa em DUAS acoes diferentes, vai para o
        # Claude Code como texto (nunca se escolhe a mais provavel)". Tabela
        # sintetica: "cala-te" (que so devia bater 'calar', em portugues) e
        # forcado a bater tambem 'acordar' pela tabela inglesa injetada.
        tabela_en_colisao = router._ACOES_BARE_EN + (
            (re.compile(r"cala\s+te"), "acordar", None),
        )
        with mock.patch.object(router, "_ACOES_BARE_EN", tabela_en_colisao):
            resultado = encaminhar("cala-te", self.config)
        self.assertEqual(resultado.tipo, "claude")
        self.assertIsNone(resultado.nome_acao)
        self.assertIsNone(resultado.argumento)
        self.assertEqual(resultado.texto, "cala-te")
        self.assertIn("D58b", resultado.motivo)

    def test_acoes_bare_que_batem_e_a_funcao_pura_usada_pelo_mecanismo(self) -> None:
        # Teste direto e sem mock da funcao que implementa a regra: cada uma
        # das cinco acoes so bate numa lista (a inglesa, aqui), confirmando
        # que a producao real nunca tem duas listas a bater ao mesmo tempo
        # para as frases desta amostra.
        self.assertEqual(
            router._acoes_bare_que_batem("be quiet"), [("calar", None)]
        )
        self.assertEqual(
            router._acoes_bare_que_batem("cala te"), [("calar", None)]
        )
        self.assertEqual(router._acoes_bare_que_batem("uma frase qualquer"), [])


class TestFormasCorteses(BaseRouter):
    """Pedidos comuns ditos com cortesia continuam na lista branca, sem o LLM."""

    def test_horas_pedidas_com_cortesia(self) -> None:
        for frase in (
            "can you tell me what time it is",
            "hey could you tell me what time it is right now",
            "what is the time",
            "podes dizer-me que horas são",
            "olha sabes-me dizer que horas são agora",
        ):
            with self.subTest(frase=frase):
                resultado = encaminhar(frase, self.config)
                self.assertEqual((resultado.tipo, resultado.nome_acao, resultado.argumento), ("local", "horas_e_data", "horas"))

    def test_data_calar_e_acordar_com_as_formas_comuns(self) -> None:
        casos = {
            "em que dia do mês estamos": ("horas_e_data", "data"),
            "enough, i've heard enough": ("calar", None),
            "shut up": ("calar", None),
            "that's enough": ("calar", None),
            "chega já ouvi o suficiente": ("calar", None),
            "wake up i'm back": ("acordar", None),
            "acorda que já voltei": ("acordar", None),
            "go to sleep now": ("adormecer", None),
        }
        for frase, (acao, argumento) in casos.items():
            with self.subTest(frase=frase):
                resultado = encaminhar(frase, self.config)
                self.assertEqual((resultado.tipo, resultado.nome_acao, resultado.argumento), ("local", acao, argumento))

    def test_dizer_a_outro_continua_a_ir_para_claude(self) -> None:
        for frase in (
            "podes dizer ao claude que horas são",
            "sabes dizer ao claude que horas são",
            "sabes-me dizer ao claude que horas são",
            "manda-me dizer ao claude as horas",
            "can you tell claude what time it is",
        ):
            with self.subTest(frase=frase):
                self.assertVaiParaClaude(frase)

    def test_a_lista_branca_continua_com_as_mesmas_acoes(self) -> None:
        acoes = {acao for _p, acao, _a in router._ACOES_BARE_PT + router._ACOES_BARE_EN}
        acoes |= {acao for _p, acao, _e in router._ACOES_COM_PROJETO}
        self.assertEqual(acoes, {"horas_e_data", "calar", "adormecer", "acordar", "abrir_vscode", "abrir_pasta"})
        for frase in ("what's the status of exemplo-um", "read the exemplo-um report", "what's the weather in porto"):
            with self.subTest(frase=frase):
                self.assertEqual(encaminhar(frase, self.config).tipo, "claude")


class TestCaminhoRapido(BaseRouter):
    """Estado e relatorio de um projeto dito, e perguntas gerais claras."""

    def test_estado_de_um_projeto_dito(self) -> None:
        for frase in (
            "what's the status of exemplo-um",
            "hey jarvis, what's the status of exemplo um",
            "how is the exemplo-um run going",
            "how's exemplo-um doing",
            "has the exemplo-um run finished",
            "exemplo-um status",
            "qual é o estado do exemplo-um",
            "como está o run do exemplo-um",
            "em que ponto está o exemplo-um",
            "o run do exemplo-um já acabou",
        ):
            with self.subTest(frase=frase):
                pedido = router.pedido_do_projeto(frase, self.config)
                self.assertIsNotNone(pedido)
                self.assertEqual((pedido.intencao, pedido.projeto.nome), ("estado", "exemplo-um"))

    def test_relatorio_de_um_projeto_dito(self) -> None:
        for frase in (
            "read the exemplo-dois report",
            "read me the summary of the last exemplo-dois run",
            "what does the exemplo-dois report say",
            "lê o relatório do exemplo-dois",
            "lê-me o resumo do último run do exemplo-dois",
            "o que diz o relatório do exemplo-dois",
        ):
            with self.subTest(frase=frase):
                pedido = router.pedido_do_projeto(frase, self.config)
                self.assertIsNotNone(pedido)
                self.assertEqual((pedido.intencao, pedido.projeto.nome), ("ler_relatorio", "exemplo-dois"))

    def test_sem_projeto_conhecido_ou_com_duvida_nao_ha_caminho_rapido(self) -> None:
        for frase in (
            "how is the run going",
            "read the report",
            "what's the status of exemplo-tres",
            "what's the status of exemplo dos",
            "how is the weather going",
            "isn't the exemplo-um run finished",
            "what's the status of exemplo-um and add a test",
            "tell exemplo-um to show its status",
        ):
            with self.subTest(frase=frase):
                self.assertIsNone(router.pedido_do_projeto(frase, self.config))

    def test_pergunta_geral_clara(self) -> None:
        for frase in (
            "what's the weather in porto",
            "uh who won the champions league last year",
            "what football games are on today",
            "what's the capital of australia",
            "will it rain tomorrow",
            "vai chover amanhã em lisboa",
            "quem ganhou o jogo ontem",
            "como está o tempo hoje",
        ):
            with self.subTest(frase=frase):
                pedido = router.pergunta_geral_clara(frase)
                self.assertIsNotNone(pedido)
                self.assertEqual((pedido.intencao, pedido.projeto), ("pergunta_geral", None))

    def test_sem_abertura_ou_sem_tema_ou_com_duvida_vai_ao_llm(self) -> None:
        for frase in (
            "i asked about the temperature, not the time",
            "what tests are failing",
            "the weather in porto",
            "don't tell me the weather",
            "tell claude what the weather is",
            "quanto tempo demora a compilar",
            "quem ganhou mais com as ações da galp",
            "what's the weather " + "and more " * 10,
        ):
            with self.subTest(frase=frase):
                self.assertIsNone(router.pergunta_geral_clara(frase))


if __name__ == "__main__":
    unittest.main()
