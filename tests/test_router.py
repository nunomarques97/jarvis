r"""Testes do encaminhador deterministico (jarvis/router.py), unittest da
biblioteca padrao (sem pytest: nao ha decisao do Scout para uma framework de
testes fora da biblioteca padrao).

Config sempre FICTICIA e em memoria (nunca o config.toml real, D10): os
projetos usados chamam-se "exemplo-um", "exemplo-dois" e "exemplo-bolsa", com
caminhos inventados que nem sequer existem no disco desta maquina — o router
nao toca no sistema de ficheiros, so compara texto, por isso isto e seguro e
representativo.

O que estes testes protegem, alem do caminho feliz:

  * a lista branca da D4 esta FECHADA — uma frase que apenas CONTEM um
    gatilho ("diz ao claude ... sobre o vs code no exemplo-um") vai para o
    Claude Code, nunca vira acao local. Cada uma das frases que o Reviewer
    reproduziu na tentativa 1 esta aqui como caso proprio;
  * o nome do projeto nao e adivinhado — "exemplo dos", "exemplo doido" e
    "exemplo-dois-privado" nao sao "exemplo-dois" (D4: nunca "o mais
    parecido");
  * as guardas da D5 e a leitura da D12 fixada pela D52.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

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
        # A T5 tem de saber qual das duas dizer sem reinterpretar a frase.
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
        # Contrato do router (D48.2): uma acao local descreve-se por nome e
        # argumento, nunca carrega o campo `texto` (que e so para o Claude Code).
        resultado = encaminhar("que horas são", self.config)
        self.assertIsNone(resultado.texto)


class TestListaBrancaEstaFechada(BaseRouter):
    """D4: conter o gatilho nao chega — a frase INTEIRA tem de ser o comando.

    Cada caso e uma frase que a tentativa 1 transformou em acao local (medido
    pelo Reviewer, docs/forja/reports/T4-a1-review.md, bloqueador 1). Todas
    tem de ir como texto para o Claude Code.
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
    """D4: nunca "o mais parecido". Casos medidos pelo Reviewer (bloqueador 2)."""

    def test_nome_parecido_com_palavra_diferente_nao_abre_o_vs_code(self) -> None:
        # "exemplo doido" batia 0.88 com exemplo-dois na tentativa 1.
        self.assertVaiParaClaude("abre o vs code no exemplo doido")

    def test_nome_parecido_numa_palavra_curta_nao_abre_a_pasta(self) -> None:
        # "exemplo dos" batia 0.957 com exemplo-dois na tentativa 1.
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
        # Alucinacao tipica do Whisper sobre um trecho de silencio/ruido (D51).
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
        # Nit do Reviewer: na cadeia viva (T6) e preciso ver no log quando a
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
        # Sem portao, mas o motivo D12 tem de ficar no log (nit do Reviewer).
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
    """D62: residuo da wake word tirado do INICIO, antes do encaminhamento.

    Casos e citacoes de linha vindos de logs/jarvis-2026-09-20.log (a janela
    do teste real, 10:09-10:20 do dia 20 set 2026) e da propria D62
    (docs/forja/DECISIONS.md) onde nao ha uma linha de log para citar.
    """

    def test_jarvis_colado_ao_inicio_vira_acao_local_das_horas(self) -> None:
        # O proprio exemplo da D62 (docs/forja/DECISIONS.md): "jarvis, que
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
        # horas sao?'" — o guiao de arranque exibido ao Sponsor nesta janela
        # do teste real, a mesma frase que a wake word cola a transcricao).
        resultado = encaminhar("hey jarvis, que horas sao", self.config)
        self.assertEqual(resultado.tipo, "local")
        self.assertEqual(resultado.nome_acao, "horas_e_data")
        self.assertEqual(resultado.argumento, "horas")
        self.assertEqual(resultado.residuo_removido, "hey jarvis")

    def test_jorvis_que_oracao_vai_para_claude_e_nao_executa_nada(self) -> None:
        # logs/jarvis-2026-09-20.log linha 647: a transcricao real do teste do
        # Sponsor saiu 'Jorvis, que oração!' em vez de 'Jarvis, que horas
        # são!'. PROIBIDO o dicionario de enganos (D62/D4/D5/D9): 'jorvis' e
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
        # Variantes da lista fechada da D62 que nao vieram do log mas estao
        # nomeadas no criterio de aceitacao da task.
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


if __name__ == "__main__":
    unittest.main()
