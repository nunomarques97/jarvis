r"""O jarvis residente: ouve, percebe, confirma, faz e responde, num so processo.

    .venv\Scripts\python -m jarvis

arranca o processo residente. No arranque aquece, em paralelo, as tres pecas
pesadas: a transcricao (`jarvis.stt`, pelo ouvido), a voz (`jarvis.voz`) e o
interprete (LLM local no Ollama). Fica pronto em poucos segundos (a meta e
<= 30 s) e escreve "PRONTO" com o tempo que levou e a VRAM livre antes e
depois.

O CAMINHO VIVO de cada frase:

  ouvido      tecla de falar (segurar para falar, soltar para acabar) ou,
              maos-livres, a palavra de ativacao seguida da frase; o VAD
              decide o fim. Transcricao residente e quente.
  interprete  intencao, projeto e, num ditado, o prompt reescrito claro.
  confirmacao tudo o que tem efeito (enviar um prompt, abrir o editor ou a
              pasta, lancar, retomar ou parar um run) e recapitulado em voz
              alta e no ecra e so corre depois de um "sim"; "nao, muda X
              para Y" e "acrescenta ..." corrigem, "aborta" (ou "cancela")
              cancela. Ver o estado e ler o relatorio de um projeto so leem
              e correm logo, tal como horas/data, calar, dormir e acordar.
  perguntas   uma pergunta geral ou de atualidade (tempo, desporto,
              noticias, factos) que nao e sobre um projeto corre logo, sem
              recap: passa-a ao Claude Code headless numa pasta neutra, so
              com pesquisa na web (`jarvis.pergunta_geral`), e a resposta
              chega em streaming: cada frase que passa o filtro da resposta
              falada e dita logo, enquanto o resto ainda se escreve. So se a
              primeira frase demorar mais do que ~1 s o jarvis diz antes um
              aviso curto e variado ("One sec."). "cala-te", Ctrl+C, "dorme"
              ou um pedido novo matam o processo e nada mais se diz.
  memoria     (`jarvis.memoria`) as ultimas perguntas gerais respondidas e
              as respostas ditas vao com a pergunta seguinte (so em RAM,
              com teto e prazo; "new conversation" esquece-as); o caderno de
              factos ("remember that ...", "forget that ...", "what do you
              remember about me?") fica num ficheiro local fora do Git e vai
              com cada pergunta geral. Guardar ou apagar um facto tem recap e
              "sim"; segredos e dados financeiros sao recusados antes.
  executor    as accoes locais (`jarvis.acoes_locais`), o estado e os runs
              FORJA do projeto (`jarvis.forja_voz`) ou o canal para a
              sessao do Claude Code do projeto (`jarvis.sessoes`): o prompt
              confirmado entra na sessao interativa do projeto pelo channel
              MCP do jarvis, ou numa sessao headless na pasta do projeto se o
              canal nao registar.
  voz         a resposta falada pela voz residente, em streaming.

Conversa (`jarvis.conversa`): quando a resposta do Claude acaba numa pergunta,
abre-se uma janela de escuta de 8 s sem palavra de ativacao; a resposta, sem
hesitacoes nem o endereco ao jarvis, vai logo se for curta (ate 5 palavras,
o jarvis diz "Sent.") e, se for mais longa, vai para a confirmacao rapida e
so um "sim" a envia. "sai da conversa" ou 8 s sem resposta fecham a janela.
Quando a resposta falada a uma pergunta geral acaba numa pergunta, a frase
dita na janela de seguimento continua a pergunta geral, com a memoria recente
e sem as intencoes de projeto (`_continuar_a_pergunta`).

Modo de conversa (a janela de seguimento): depois de cada resposta falada o
jarvis continua a ouvir sem palavra de ativacao, e a janela recomeca depois
de cada resposta; fecha com `[escuta] seguimento_s` de silencio (15 s por
omissao) ou com "that's all" / "thanks, that's it". O som "abrir" so toca
quando a escuta abre pela primeira vez (nao quando reabre depois de uma
resposta, de um recap ou enquanto uma pergunta geral esta a caminho) e o
"fechar" quando ela acaba de vez. A fala de fundo fica contida: silencio,
hesitacoes, so cortesia, uma frase sem intencao ou uma pergunta geral que nao
e dita como pergunta ao jarvis nao fazem nada nem gastam a janela; o que tem
efeito continua a precisar do recap e do "sim". A tecla e a palavra de
ativacao funcionam sempre (a palavra dita dentro da janela sai do texto).

Uma so instancia (`jarvis.instancia`): com o microfone, o jarvis so arranca
com a tranca `logs/jarvis.lock` (o PID dele); se outro jarvis vivo a tem, diz
isso no ecra e sai antes de carregar modelos. `--wav` e `--autoteste` nao a
tiram.

Dormir: a dormir nada e interpretado. Acorda com "acorda"/"wake up" (lista
branca) ou com a palavra de ativacao (score >= `[ouvido] limiar_ativacao`)
seguida so de um acordar curto ("Up.", "wake", "awake", nada), que responde
"I'm awake.". A palavra de ativacao (score >= limiar) seguida de um pedido
normal acorda-o sem frase propria e o pedido segue logo o caminho de sempre
(interprete, recap e "sim"). A tecla, a palavra abaixo do limiar ou outra
fala continuam ignoradas; "calar" ou "dormir" com a palavra de ativacao nao
acordam e ouvem uma vez como se acorda.

Avisos (`jarvis.avisos`): a sessao de um projeto acabou ou esta a espera do
utilizador (hooks do Claude Code, pelo IPC do canal), ou um run FORJA terminou
ou bloqueou (sondagem de `core status`). Cada aviso e uma frase fixa com o
nome do projeto, dita quando o jarvis esta livre, nunca por cima de ninguem.

Depois de o recap ser dito (e de cada recap corrigido ou pergunta repetida),
o jarvis abre no ouvido uma escuta sem palavra de ativacao ate ao fim do prazo
da confirmacao: a frase seguinte e a resposta ao recap. So abre depois de a
voz acabar, para o jarvis nao se ouvir a si proprio; ruido que fecha a escuta
volta a abri-la no ciclo de `verificar_tempo`.

A consola mostra sempre o estado atual numa linha propria (A OUVIR, A PENSAR,
A ESPERA DE CONFIRMACAO, A FALAR, A DORMIR) e cada frase escreve o seu
registo com timestamps e a latencia de cada etapa, na consola e em
`logs/jarvis-<data>.log` (pasta ignorada pelo Git). Duas medidas ficam em
cada frase: o primeiro sinal de vida (fim da fala -> linha A PENSAR, com o
texto ja transcrito) e o inicio da resposta falada (fim da fala -> primeiro
bloco de audio da resposta ou do recap). Quando o ouvido sabe o ultimo chunk
com voz (o fim verdadeiro da fala, antes do silencio que fecha a frase), a
frase escreve-o tambem, e a resposta falada e a resposta a uma pergunta geral
medem-se a partir dele (`scripts/sessao_naturalidade.py` le essas linhas).

Silencio: Ctrl+C, fechar a janela e "cala-te" passam todos por
`jarvis.voz.calar_agora`. "cala-te" dito por cima da voz cala-a logo que a
frase e transcrita, sem esperar pela vez dela.

Interromper (`[escuta] interromper`, ligado por omissao; desligado com
--sem-voz): enquanto o jarvis fala, o ouvido ouve por cima da voz com o VAD
Silero e a primeira fala PAUSA a voz logo (`ao_interromper`), com as duas
horas no log (inicio da fala e voz parada). A frase dita por cima decide:
ruido, tosse, so hesitacoes ou um acompanhamento ("yeah", "uh-huh") retomam
a resposta pausada; fala a serio para-a de vez (o resto fica no ecra e no
log) e a frase segue como o pedido seguinte, sem palavra de ativacao.
Durante o recap, um "sim" dito por cima nao envia nada: o recap acaba e o
"sim" diz-se depois, como sempre; outra resposta ("aborta", "nao, muda X
para Y") para o recap e responde-lhe. Um aviso interrompido nao se repete.
Uma resposta geral interrompida deixa de se dizer, mas continua a chegar e
aparece inteira no ecra.

Nada disto envia texto para fora do PC antes do "sim", com uma excecao: as
perguntas gerais saem do PC sem "sim" (so leem, nao fazem nada) para o
Claude Code com pesquisa na web, e gastam quota da subscricao Claude. Pedidos
de dinheiro ou de bolsa sao recusados antes de sair. O interprete corre no
Ollama local e o canal so recebe o prompt que o utilizador confirmou.

Uso:

    .venv\Scripts\python -m jarvis                       # microfone, tecla e palavra de ativacao
    .venv\Scripts\python -m jarvis --sem-ativacao        # so a tecla de falar
    .venv\Scripts\python -m jarvis --com-som             # bip no inicio e no fim da escuta
    .venv\Scripts\python -m jarvis --sem-voz             # respostas so na consola (sem interromper)
    .venv\Scripts\python -m jarvis --wav a.wav b.wav     # ficheiros em vez do microfone
    .venv\Scripts\python -m jarvis --autoteste

Com `--wav` cada ficheiro faz de uma frase dita com a tecla premida; o
seguinte so entra depois de a frase anterior estar tratada (como alguem que
ouve o recap e responde). Neste modo a voz so toca com `--com-som`.
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import os
import queue
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from jarvis import acoes_locais, conversa, sinais, voz
from jarvis.adaptacao import criar_adaptacao
from jarvis.audio_util import RAIZ, garantir_pasta
from jarvis.avisos import DESCARTADO, FALADO, OCUPADO, SO_ECRA, Aviso, Avisos, VigiaDosRuns
from jarvis.bolinha import LigacaoABolinha, PonteDaBolinha
from jarvis.config import CAMINHO_CONFIG_PADRAO, Config, ConfigDescoberta, ConfigError, carregar_config
from jarvis.confirmacao import (
    INTENCAO_ESQUECER_FACTO,
    INTENCAO_LEMBRAR_FACTO,
    Confirmacao,
    Desfecho,
    Pedido,
    classificar_resposta,
)
from jarvis.consola import forcar_consola_utf8
from jarvis.fim_de_turno import criar_fim_de_turno
from jarvis.forja_voz import INTENCOES_POR_VOZ, ForjaPorVoz
from jarvis.instancia import (
    NOME_DA_TRANCA,
    DonoDaTranca,
    OutraInstanciaAberta,
    TrancaDaInstancia,
    mensagem_de_recusa,
)
from jarvis.interprete import (
    INTENCAO_CORTESIA,
    INTENCAO_MEMORIA,
    INTENCAO_PERGUNTA_GERAL,
    INTENCAO_RECUSADA,
    INTENCAO_SOCIAL,
    INTENCOES_COM_PROJETO,
    Interpretacao,
    Interprete,
    comando_de_memoria,
    limpar_texto,
    medir_vram,
    projetos_mencionados,
    sem_palavra_de_ativacao,
    so_cortesia,
)
from jarvis.ouvido import (
    BYTES_POR_CHUNK,
    DURACAO_DO_CHUNK_S,
    ESCUTA_CONVERSA,
    ESCUTA_RECAP,
    ESCUTA_SEGUIMENTO,
    GATILHO_ATIVACAO,
    GATILHO_INTERRUPCAO,
    GATILHO_JANELA,
    GATILHO_TECLA,
    INTERRUPCAO_PAUSAR,
    INTERRUPCAO_RETOMAR,
    PALAVRAS_DE_ATIVACAO,
    DetetorOpenWakeWord,
    Frase,
    MicrofonePyAudio,
    Ouvido,
    TeclaDoFicheiro,
    TeclaWindows,
    VadWebRtc,
    chunks_do_pcm,
    modelo_de_ativacao,
    palavra_de_ativacao,
    pcm_do_wav,
)
from jarvis.memoria import (
    CadernoCheio,
    CadernoDeFactos,
    FactoRecusado,
    FrasesRecentes,
    HistoricoDePerguntas,
    contexto_do_interprete,
    facto_mais_parecido,
    recusa_do_facto,
    texto_do_facto,
)
from jarvis.pergunta_geral import Consulta, PerguntasGerais
from jarvis.persona import CASO_SOCIAL, ClienteOllamaEmFluxo, Persona, Variantes
from jarvis.projetos import com_projetos_descobertos
from jarvis.resposta_falada import (
    FRASES_DE_RECURSO,
    MAXIMO_ABSOLUTO_FALADO,
    ResumoEmFluxo,
    cortar_no_limite,
    frase_de_recurso,
    resumo_falado,
    rotulo_da_origem,
)
from jarvis.router import _normalizar, encaminhar
from jarvis.stt import MotorIndisponivel, criar_motor

# --- Constantes ---------------------------------------------------------------

#: Pasta dos logs. Ja esta no .gitignore (`logs/`): nenhuma transcricao entra
#: no repositorio publico.
PASTA_LOGS = RAIZ / "logs"

#: Meta do arranque: do inicio do processo a "PRONTO".
LIMITE_DO_ARRANQUE_S = 30.0

#: Codigo de saida quando outro jarvis com o microfone ja esta aberto.
CODIGO_OUTRA_INSTANCIA = 3

#: Uma pergunta geral cuja primeira frase nao esteja pronta ao fim disto leva
#: antes um aviso curto ("One sec.").
ESPERA_DO_AVISO_S = 1.0
#: Enquanto a resposta a uma pergunta geral se diz aos bocados, cada espera
#: pela vez (frases por tratar passam a frente) verifica o cancelamento a este ritmo.
PASSO_DA_ESPERA_DA_VEZ_S = 0.05

#: Frases ja transcritas a espera de vez. Cheia, a frase nova e descartada
#: com uma linha no log (nunca cresce sem limite).
FRASES_EM_ESPERA = 4

#: Modo ficheiro: quanto tempo se espera que a frase anterior fique tratada
#: antes de entregar o ficheiro seguinte.
ESPERA_ENTRE_FICHEIROS_S = 60.0
#: Silencio depois de cada ficheiro: a tecla simulada solta-se aqui.
SILENCIO_DEPOIS_DO_FICHEIRO_S = 0.3

NOMES_DAS_ETAPAS = {
    1: "1/5 ouvido               ",
    2: "2/5 transcricao          ",
    3: "3/5 interprete           ",
    4: "4/5 confirmacao/accao    ",
    5: "5/5 voz                  ",
}

LARGURA_DA_SEPARACAO = 78

# Estados mostrados na consola.
A_OUVIR = "A OUVIR"
A_PENSAR = "A PENSAR"
A_ESPERA = "À ESPERA DE CONFIRMAÇÃO"
A_ESPERA_A_OUVIR = "À ESPERA DE CONFIRMAÇÃO — A OUVIR A RESPOSTA"
A_FALAR = "A FALAR"
A_DORMIR = "A DORMIR"
A_CONVERSA = "EM CONVERSA"
#: Depois de o jarvis falar: a ouvir sem palavra de ativacao.
A_OUVIR_TE = "A OUVIR-TE"
#: Detalhe da linha de estado quando a escuta sem palavra de ativacao fecha.
ESCUTA_FECHADA = "escuta sem palavra de ativacao fechada; diz hey jarvis ou usa a tecla"

#: Intencoes sobre o estado e os runs FORJA de um projeto (`jarvis.forja_voz`).
INTENCOES_DA_FORJA = frozenset(INTENCOES_POR_VOZ)

#: Intencoes que acabam num prompt entregue a sessao do Claude Code.
INTENCOES_DO_CANAL = frozenset({"ditar_prompt", "conversa"})

#: Accoes da lista branca que passam a frente de um recap pendente: calar e
#: dormir nunca sao lidas como resposta ao recap.
ACOES_QUE_PASSAM_A_FRENTE = {"calar": "calar", "adormecer": "dormir", "acordar": "acordar"}

#: Com o jarvis a dormir, a palavra de ativacao (score >= limiar) seguida so de
#: uma destas formas curtas acorda-o: o motor de voz corta "wake up" em "Up.".
#: Sem nada depois da palavra de ativacao tambem acorda (tupla vazia).
ACORDAR_CURTO = frozenset({(), ("up",), ("wake",), ("wake", "up"), ("awake",), ("acorda",)})

#: Palavras ditas por cima da voz que so acompanham quem fala ("yeah",
#: "uh-huh", "right", "I see"): a voz pausada continua. Lista fechada; uma
#: palavra fora dela ("okay, that's enough", "thank you") para a voz de vez.
ACOMPANHAMENTOS = frozenset(
    {
        "yeah", "yes", "yep", "yup", "ya", "uh", "huh", "uhuh", "mhm", "mm", "mmm", "hmm", "hm", "mmhmm",
        "um", "uhm", "ah", "aha", "oh", "wow", "right", "okay", "ok", "sure", "alright", "cool", "nice",
        "great", "really", "i", "see", "got", "it",
        "sim", "pois", "certo", "claro", "exato", "ta", "boa", "hum",
    }
)

#: Uma voz em pausa sem nenhuma frase a caminho ao fim disto continua (a
#: frase dita por cima perdeu-se sem o ouvido dar noticia).
PAUSA_SEM_FRASE_S = 2.0

#: Palavras soltas que nao contam para saber se a frase e so um acordar curto.
_HESITACOES_AO_ACORDAR = frozenset({"uh", "um", "uhm", "er", "erm", "ah", "eh", "hmm", "hum", "oh"})

_TEXTOS = {
    "pt": {
        "calado": "Fico calado.",
        "dormir": "Vou dormir. Diz acorda quando precisares.",
        "acordado": "Estou acordado.",
        "a_dormir": "Estou a dormir. Para me acordar, diz boas jarvis, acorda.",
        "enviado": "Enviado para o {projeto}.",
        "enviado_curto": "Enviado.",
        "a_abrir": "Vou abrir a sessão do {projeto}. Aceita o aviso na janela nova e o pedido segue.",
        "sem_forja": "O estado e os runs da FORJA não estão disponíveis. Não fiz nada.",
        "sobre_o_projeto": "No {projeto}: {texto}",
        "sem_canal": "O canal para o Claude Code não está disponível. Não enviei nada.",
        "sem_resposta": "Não recebi resposta do {projeto}. Os detalhes estão no ecrã.",
        "conversa_fim": "Saí da conversa.",
        "a_verificar": ("Deixa-me ver.", "Um segundo.", "Vou ver.", "Já vejo."),
        "pergunta_falhou": "Não consegui obter resposta a isso.",
        "pergunta_falhou_a_meio": "Desculpa, perdi o resto da resposta.",
        "pergunta_recusada": "Isso não faço por voz: pedidos de dinheiro ou de bolsa ficam de fora.",
        "sem_perguntas": "As perguntas gerais não estão disponíveis.",
        "cortesia": "Está bem.",
        "um_momento": "Um momento.",
        "social_como_estas": "Estou bem, obrigado por perguntares.",
        "social_o_que_ha": "Nada de especial, estou aqui para ajudar.",
        "social_quem_es": "Sou o Jarvis, o teu assistente de voz para os projetos.",
        "social_estas_ai": "Sim, estou aqui.",
        "social_cumprimento": "Olá, estou aqui.",
        "memoria_nova_conversa": "Está bem, começamos uma conversa nova.",
        "memoria_sem_caderno": "O caderno da memória não está disponível. Não guardei nada.",
        "memoria_sem_facto": "Diz lembra-te que, e depois o que queres que eu guarde.",
        "memoria_recusado_segredo": "Não guardo palavras-passe, códigos nem outros segredos. Não guardei nada.",
        "memoria_recusado_financeiro": "Não guardo dados de dinheiro nem do banco. Não guardei nada.",
        "memoria_cheio": "O caderno está cheio. Pede-me primeiro para esquecer alguma coisa.",
        "memoria_ja_sei": "Já me lembro disso.",
        "memoria_nada_parecido": "Não me lembro de nada assim. Não apaguei nada.",
        "memoria_guardado": "Guardado, vou lembrar-me disso.",
        "memoria_apagado": "Esquecido.",
        "memoria_falhou": "Não consegui escrever no caderno. Não mudou nada.",
        "memoria_vazia": "Ainda não me lembro de nada sobre ti.",
        "memoria_um": "Lembro-me de uma coisa sobre ti: {facto}",
        "memoria_lista": "Lembro-me de {n} coisas sobre ti. A lista está no ecrã.",
    },
    "en": {
        "calado": ("Okay, going quiet.", "Sure, I'll be quiet.", "Quiet now."),
        "dormir": (
            "Okay, off to sleep. Say wake up when you need me.",
            "Going to sleep. Just say wake up.",
            "Sleeping now; say wake up when you want me.",
        ),
        "acordado": ("I'm awake.", "I'm up.", "Awake and listening."),
        "a_dormir": (
            "I'm asleep. Say hey jarvis, wake up.",
            "I'm sleeping; hey jarvis, wake up, brings me back.",
        ),
        "enviado": ("Sent to {projeto}.", "Done, {projeto} has it.", "Passed it to {projeto}."),
        "enviado_curto": ("Sent.", "Done.", "Passed on."),
        "a_abrir": (
            "Opening {projeto}. Accept the notice in the new window and the request follows.",
            "Starting the {projeto} session; accept the notice there and I'll pass it on.",
        ),
        "sem_forja": (
            "I can't reach FORJA right now, so I didn't do anything.",
            "FORJA isn't available at the moment; nothing was done.",
        ),
        "sobre_o_projeto": ("On {projeto}: {texto}", "For {projeto}: {texto}"),
        "sem_canal": (
            "I can't reach Claude Code right now, so nothing was sent.",
            "The Claude Code channel is down; I didn't send anything.",
        ),
        "sem_resposta": (
            "{projeto} didn't answer. The details are on screen.",
            "No answer from {projeto}; the details are on screen.",
        ),
        "conversa_fim": ("Left the conversation.", "Okay, conversation closed.", "Alright, I'm out of the conversation."),
        "a_verificar": ("One sec.", "Let me see.", "Let me look.", "Give me a second."),
        "pergunta_falhou": (
            "Sorry, I couldn't find an answer to that.",
            "No luck with that one, sorry.",
            "I couldn't get an answer to that, sorry.",
        ),
        "pergunta_falhou_a_meio": ("Sorry, I lost the rest of that.", "Sorry, the rest of that got lost."),
        "pergunta_recusada": (
            "Sorry, I don't do money and trading requests by voice.",
            "Money and trading requests are off limits for me.",
            "I never do money and trading requests by voice.",
        ),
        "sem_perguntas": (
            "I can't answer general questions right now.",
            "General questions aren't available at the moment.",
        ),
        "cortesia": ("Okay.", "Sure.", "Alright.", "No problem."),
        "um_momento": ("One moment.", "Just a moment.", "Hang on a second."),
        "social_como_estas": ("I'm doing well, thanks for asking.", "All good here, thanks.", "Pretty good, thank you."),
        "social_o_que_ha": ("Not much, just here if you need me.", "Nothing much, ready when you are."),
        "social_quem_es": (
            "I'm Jarvis, your voice assistant for your projects.",
            "I'm Jarvis; I help you run your projects by voice.",
        ),
        "social_estas_ai": ("Yes, I'm here.", "Right here.", "I'm here, listening."),
        "social_cumprimento": ("Hello, I'm here.", "Hi there.", "Hello, good to hear you."),
        "memoria_nova_conversa": ("Okay, fresh start.", "Sure, starting over.", "Okay, new conversation."),
        "memoria_sem_caderno": (
            "I can't reach my notebook right now, so nothing was saved.",
            "My notebook isn't available; I didn't save anything.",
        ),
        "memoria_sem_facto": (
            "Say remember that, and then what to keep.",
            "Tell me remember that, followed by what to keep.",
        ),
        "memoria_recusado_segredo": (
            "I don't keep passwords, codes or other secrets, so I didn't save that.",
            "Secrets stay out of my notebook; I didn't save that.",
        ),
        "memoria_recusado_financeiro": (
            "I don't keep money or bank details, so I didn't save that.",
            "Bank and money details stay out of my notebook; I didn't save that.",
        ),
        "memoria_cheio": (
            "My notebook's full. Ask me to forget something first.",
            "No room left in my notebook; ask me to forget something first.",
        ),
        "memoria_ja_sei": ("I already remember that.", "I know that one already.", "Already noted."),
        "memoria_nada_parecido": (
            "I don't remember anything like that, so nothing was deleted.",
            "Nothing like that in my notebook; I didn't delete anything.",
        ),
        "memoria_guardado": ("Got it, I'll remember that.", "Saved, I'll remember that.", "Noted."),
        "memoria_apagado": ("Forgotten.", "Done, it's forgotten.", "Gone from my notebook."),
        "memoria_falhou": (
            "I couldn't write to my notebook, so nothing changed.",
            "Writing to my notebook failed; nothing changed.",
        ),
        "memoria_vazia": ("I don't remember anything about you yet.", "My notebook about you is still empty."),
        "memoria_um": ("I remember one thing about you: {facto}", "Just one thing so far: {facto}"),
        "memoria_lista": (
            "I remember {n} things about you. The list is on screen.",
            "{n} things so far; they're on screen.",
        ),
    },
}


# --- Log ------------------------------------------------------------------------


def e_acordar_curto(texto: str, lingua: str) -> bool:
    """True se, sem a palavra de ativacao e hesitacoes, a frase e so um acordar curto.

    'Up.', 'wake', 'wake up', 'awake', 'acorda' ou nada (so a palavra de
    ativacao). Qualquer outra palavra deixa de ser um acordar curto.
    """
    ativacao = set(_normalizar(PALAVRAS_DE_ATIVACAO.get(lingua, "")).split())
    palavras = tuple(
        palavra
        for palavra in _normalizar(texto).split()
        if palavra not in ativacao and palavra not in _HESITACOES_AO_ACORDAR
    )
    return palavras in ACORDAR_CURTO


def e_so_acompanhamento(texto: str, lingua: str) -> bool:
    """A frase dita por cima da voz e so ruido, hesitacoes ou um acompanhamento ("yeah", "uh-huh")."""
    limpo = conversa.limpar_resposta(texto, lingua)
    palavras = _normalizar(limpo).split()
    return all(palavra in ACOMPANHAMENTOS for palavra in palavras)


def agora_iso(quando: datetime.datetime | None = None) -> str:
    """Timestamp local com milissegundos, o mesmo na consola e no ficheiro."""
    quando = quando or datetime.datetime.now()
    return quando.strftime("%Y-%m-%d %H:%M:%S.") + f"{quando.microsecond // 1000:03d}"


def texto_para_a_consola(texto: str, codificacao: str | None = None) -> str:
    """O mesmo texto, garantidamente imprimivel na consola em uso.

    Perder um acento na consola e mau, perder o registo da frase por causa
    dele e pior. O ficheiro de log fica sempre em UTF-8, com os acentos intactos.
    """
    codificacao = codificacao or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        texto.encode(codificacao)
    except (UnicodeEncodeError, LookupError):
        return texto.encode(codificacao, errors="replace").decode(codificacao, errors="replace")
    return texto


def caminho_do_log(quando: datetime.datetime | None = None, pasta: Path = PASTA_LOGS) -> Path:
    """`logs/jarvis-<data>.log` — um ficheiro por dia, sempre em append."""
    quando = quando or datetime.datetime.now()
    return pasta / f"jarvis-{quando:%Y-%m-%d}.log"


class LogDaSessao:
    """O log unico: a mesma linha na consola E no ficheiro do dia.

    Seguro entre threads: o ouvido, o tratamento das frases, o canal e a
    thread principal escrevem todos aqui.
    """

    def __init__(
        self,
        pasta: Path = PASTA_LOGS,
        consola=None,
        quando: datetime.datetime | None = None,
        relogio_de_parede: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self.caminho = caminho_do_log(quando, pasta)
        self.consola = consola if consola is not None else sys.stdout
        self._relogio_de_parede = relogio_de_parede
        # RLock, nao Lock: um Ctrl+C corre na thread principal entre bytecodes
        # e o handler volta a escrever aqui (para calar a voz) na MESMA thread
        # que pode ja estar dentro de `_escrever`. Com Lock era um deadlock.
        self._tranca = threading.RLock()
        garantir_pasta(self.caminho.parent)
        self._ficheiro = self.caminho.open("a", encoding="utf-8")

    def agora(self) -> datetime.datetime:
        """A hora local do relogio que carimba as linhas."""
        return self._relogio_de_parede()

    def linha(self, texto: str) -> str:
        """Escreve uma linha com timestamp. Devolve a linha completa."""
        completa = f"{agora_iso(self._relogio_de_parede())} {texto}"
        self._escrever(completa)
        return completa

    def bruto(self, texto: str = "") -> None:
        """Escreve sem timestamp (cabecalhos e separadores)."""
        self._escrever(texto)

    def _escrever(self, texto: str) -> None:
        with self._tranca:
            print(texto_para_a_consola(texto), file=self.consola, flush=True)
            if not self._ficheiro.closed:
                self._ficheiro.write(texto + "\n")
                self._ficheiro.flush()

    def fechar(self) -> None:
        with self._tranca:
            if not self._ficheiro.closed:
                self._ficheiro.close()


def formatar_etapa(numero_da_frase: int, etapa: int, latencia_ms: float, detalhe: str) -> str:
    """A linha de uma etapa, exatamente como sai na consola e no ficheiro."""
    return (
        f"frase #{numero_da_frase} | etapa {NOMES_DAS_ETAPAS[etapa]} "
        f"| {latencia_ms:7.0f} ms | {detalhe}"
    )


@dataclass(frozen=True)
class TemposDaResposta:
    """Onde foi o tempo da ultima voz ao primeiro audio da resposta, etapa a etapa (ms).

    `fim_de_turno_ms` e o silencio que o ouvido esperou para fechar a frase
    (None quando o ouvido nao sabe o ultimo chunk com voz; nesse caso tudo
    conta desde o fim da escuta). `interprete_ms` e None numa frase que nao
    passou pelo interprete (a resposta a um recap, por exemplo). `resto_ms`
    e a fila, a confirmacao e a decisao ate a voz ser pedida; `voz_ms` e o
    pedido de fala ate ao primeiro bloco de audio.
    """

    fim_de_turno_ms: float | None
    stt_ms: float
    interprete_ms: float | None
    origem: str | None
    resto_ms: float
    voz_ms: float
    total_ms: float

    def linha(self) -> str:
        def ms(valor: float | None) -> str:
            return "?" if valor is None else f"{valor:.0f} ms"

        interprete = "nao usado" if self.interprete_ms is None else f"{ms(self.interprete_ms)} (origem={self.origem})"
        desde = "desde a ultima voz" if self.fim_de_turno_ms is not None else "desde o fim da escuta"
        return (
            f"tempos ate a primeira voz: fim de turno {ms(self.fim_de_turno_ms)} | stt {ms(self.stt_ms)} "
            f"| interprete {interprete} | resto {ms(self.resto_ms)} | voz {ms(self.voz_ms)} "
            f"| total {ms(self.total_ms)} {desde}"
        )


def tempos_da_resposta(
    *,
    fim_da_escuta: float,
    texto_pronto: float | None,
    primeiro_audio: float,
    inicio_da_voz: float,
    ultima_voz: float | None = None,
    interprete: tuple[float, float, str] | None = None,
) -> TemposDaResposta:
    """A decomposicao da latencia de uma resposta, de instantes do mesmo relogio."""
    referencia = ultima_voz if ultima_voz is not None else fim_da_escuta
    fim_de_turno = None if ultima_voz is None else (fim_da_escuta - ultima_voz) * 1000
    stt = (max(texto_pronto, fim_da_escuta) - fim_da_escuta) * 1000 if texto_pronto is not None else 0.0
    interprete_ms = None if interprete is None else (interprete[1] - interprete[0]) * 1000
    voz_ms = (primeiro_audio - inicio_da_voz) * 1000
    total = (primeiro_audio - referencia) * 1000
    resto = total - (fim_de_turno or 0.0) - stt - (interprete_ms or 0.0) - voz_ms
    return TemposDaResposta(
        fim_de_turno_ms=fim_de_turno,
        stt_ms=stt,
        interprete_ms=interprete_ms,
        origem=None if interprete is None else interprete[2],
        resto_ms=max(0.0, resto),
        voz_ms=voz_ms,
        total_ms=total,
    )


@dataclass(frozen=True)
class Marca:
    """Uma etapa medida: quanto demorou e o que aconteceu nela."""

    etapa: int
    latencia_ms: float
    detalhe: str


@dataclass
class RegistoDaFrase:
    """O registo de uma frase: etapas com latencias, notas e o total.

    Cada etapa mede desde a anterior (ou desde `desde`). O relogio e
    injetavel, por isso o formato e a aritmetica testam-se sem audio.
    """

    numero: int
    log: LogDaSessao | None = None
    relogio: Callable[[], float] = time.perf_counter
    inicio: float = field(default=0.0)
    marcas: list[Marca] = field(default_factory=list)
    #: Tecla solta ou VAD a fechar a frase: a referencia das latencias que o
    #: utilizador sente.
    fim_da_fala: float | None = None
    inicio_da_fala: float | None = None
    #: O ultimo chunk com voz (o fim verdadeiro da fala), quando o ouvido o sabe.
    ultima_voz: float | None = None
    #: O texto transcrito ficou pronto (fim da etapa do STT).
    texto_pronto: float | None = None
    #: A chamada ao interprete desta frase: (inicio, fim, origem), se houve.
    interprete: tuple[float, float, str] | None = None
    fechada: bool = False
    _referencia: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.inicio:
            self.inicio = self.relogio()
        self._referencia = self.inicio

    def tem_etapa(self, etapa: int) -> bool:
        return any(marca.etapa == etapa for marca in self.marcas)

    def marcar(
        self, etapa: int, detalhe: str, desde: float | None = None, instante: float | None = None
    ) -> Marca:
        """Fecha uma etapa: latencia desde a etapa anterior (ou desde `desde`).

        `instante` fixa o momento em que a etapa aconteceu de facto, para o
        caso de so se poder escrever a linha um pouco depois.
        """
        instante = self.relogio() if instante is None else instante
        referencia = self._referencia if desde is None else desde
        marca = Marca(etapa=etapa, latencia_ms=(instante - referencia) * 1000, detalhe=detalhe)
        self.marcas.append(marca)
        self._referencia = instante
        self._emitir(formatar_etapa(self.numero, etapa, marca.latencia_ms, detalhe))
        return marca

    def nota(self, texto: str) -> None:
        """Uma linha do registo desta frase que nao e uma etapa."""
        self._emitir(f"frase #{self.numero} | {texto}")

    def fechar(self, resultado: str) -> str:
        """A linha final: latencia total, desde o inicio da escuta e desde o fim da fala."""
        self.fechada = True
        agora = self.relogio()
        total = (agora - self.inicio) * 1000
        if self.fim_da_fala is not None:
            desde_a_fala = f"{(agora - self.fim_da_fala) * 1000:.0f} ms desde o fim da fala"
        else:
            desde_a_fala = "sem fala detetada"
        linha = (
            f"frase #{self.numero} | TOTAL | {total:7.0f} ms desde o inicio da escuta "
            f"| {desde_a_fala} | {resultado}"
        )
        self._emitir(linha)
        self._emitir("-" * LARGURA_DA_SEPARACAO)
        return linha

    def _emitir(self, texto: str) -> None:
        if self.log is not None:
            self.log.linha(texto)


# --- Estado na consola -----------------------------------------------------------


def _titulo_da_consola(texto: str) -> None:
    """Poe o estado no titulo da janela (so Windows; falhar nao importa)."""
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(texto)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - o titulo e um extra, nunca um erro
        pass


class Painel:
    """O estado atual do jarvis: uma linha na consola a cada mudanca."""

    def __init__(self, escrever: Callable[[str], object], *, titulo: bool = False) -> None:
        self._escrever = escrever
        self._titulo = titulo
        self._tranca = threading.Lock()
        self.atual: str | None = None
        self.historico: list[str] = []
        #: Recebe cada estado novo (a bolinha). Tem de ser rapido e nunca levanta.
        self.ao_mudar: Callable[[str], object] | None = None

    def mudar(self, estado: str, detalhe: str = "") -> None:
        with self._tranca:
            if estado == self.atual and not detalhe:
                return
            self.atual = estado
            self.historico.append(estado)
        self._escrever(f"estado | {estado}" + (f" | {detalhe}" if detalhe else ""))
        if self._titulo:
            _titulo_da_consola(f"jarvis - {estado.lower()}")
        ao_mudar = self.ao_mudar
        if ao_mudar is not None:
            try:
                ao_mudar(estado)
            except Exception:  # noqa: BLE001 - a bolinha e um extra
                pass


# --- Modo ficheiro: varios WAV, um de cada vez ----------------------------------


class FonteDeSequencia:
    """Varios PCM seguidos, cada um "dito com a tecla premida", um de cada vez.

    Entre ficheiros entrega silencio (a tecla solta) e so passa ao seguinte
    quando `pronto(n)` diz que as `n` frases ja entregues estao tratadas, ou ao
    fim de `espera_maxima_s`. Depois do ultimo, espera do mesmo modo e acaba.
    `dentro_do_audio` e o que a `TeclaDoFicheiro` usa para segurar a tecla.
    """

    def __init__(
        self,
        pcms: Iterable[bytes],
        *,
        pronto: Callable[[int], bool] = lambda _n: True,
        ritmo_real: bool = False,
        silencio_depois_s: float = SILENCIO_DEPOIS_DO_FICHEIRO_S,
        espera_maxima_s: float = ESPERA_ENTRE_FICHEIROS_S,
        descricao: str = "ficheiros",
        avisar: Callable[[str], object] | None = None,
        dormir: Callable[[float], None] = time.sleep,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ficheiros = [chunks_do_pcm(pcm) for pcm in pcms]
        self._pronto = pronto
        self._ritmo_real = ritmo_real
        self._silencio = max(1, round(silencio_depois_s / DURACAO_DO_CHUNK_S))
        self._espera_maxima_s = espera_maxima_s
        self._avisar = avisar
        self._dormir = dormir
        self._relogio = relogio
        self.descricao = descricao
        self.dentro_do_audio = False
        self._indice = 0
        self._posicao = 0
        self._silencio_dado = 0
        self._espera_desde: float | None = None

    def abrir(self) -> None:
        self._indice = self._posicao = self._silencio_dado = 0
        self._espera_desde = None

    def _silencio_chunk(self) -> bytes:
        self.dentro_do_audio = False
        return b"\x00" * BYTES_POR_CHUNK

    def ler(self) -> bytes | None:
        if self._indice < len(self._ficheiros):
            chunks = self._ficheiros[self._indice]
            if self._posicao < len(chunks):
                if self._ritmo_real:
                    self._dormir(DURACAO_DO_CHUNK_S)
                chunk = chunks[self._posicao]
                self._posicao += 1
                self.dentro_do_audio = True
                return chunk
        if self._silencio_dado < self._silencio:
            self._silencio_dado += 1
            if self._ritmo_real:
                self._dormir(DURACAO_DO_CHUNK_S)
            return self._silencio_chunk()
        # A espera pela frase anterior: silencio ao ritmo real, sem girar a vazio.
        entregues = min(self._indice + 1, len(self._ficheiros))
        if self._espera_desde is None:
            self._espera_desde = self._relogio()
        esgotada = self._relogio() - self._espera_desde >= self._espera_maxima_s
        if not self._pronto(entregues) and not esgotada:
            self._dormir(DURACAO_DO_CHUNK_S)
            return self._silencio_chunk()
        if esgotada and self._avisar is not None:
            self._avisar(
                f"modo ficheiro | a frase do ficheiro {entregues} nao ficou tratada em "
                f"{self._espera_maxima_s:.0f} s; a seguir sem ela"
            )
        self._espera_desde = None
        self._indice += 1
        self._posicao = self._silencio_dado = 0
        if self._indice >= len(self._ficheiros):
            self.dentro_do_audio = False
            return None
        return self._silencio_chunk()

    def fechar(self) -> None:
        pass


# --- Canal para as sessoes do Claude Code ----------------------------------------


class CanalParaSessoes(Protocol):
    """Entrega um prompt confirmado a sessao de um projeto da configuracao."""

    def enviar(self, projeto: str, texto: str, ao_responder: Callable[[str, object], None]) -> bool:
        """Envia em segundo plano; devolve True se a sessao do projeto ja estava aberta."""

    def fechar(self) -> None: ...


class CanalDasSessoes:
    """O canal real: sessao interativa do projeto com o channel MCP do jarvis.

    A primeira entrega a um projeto abre a sessao numa janela nova (o
    utilizador aceita o aviso do Claude Code nessa janela); se o canal nao
    registar, os prompts vao para uma sessao headless na pasta do projeto.
    Cada entrega corre numa thread propria, por ordem dentro de cada projeto,
    e o resultado volta por `ao_responder(projeto, entrega)`.
    """

    def __init__(
        self,
        config: Config,
        escrever: Callable[[str], object],
        *,
        criar_central: Callable[..., object] | None = None,
        abrir: Callable[..., object] | None = None,
        criar_canal: Callable[..., object] | None = None,
        limite_s: float | None = None,
        ao_evento: Callable[[str, str, str], object] | None = None,
    ) -> None:
        from jarvis import sessoes

        self._sessoes = sessoes
        self.config = config
        self._escrever = escrever
        self._criar_central = criar_central or sessoes.CentralDoCanal
        self._abrir = abrir or sessoes.abrir_sessao
        self._criar_canal = criar_canal or sessoes.CanalDoProjeto
        self.limite_s = sessoes.ESPERA_DA_RESPOSTA_S if limite_s is None else limite_s
        self._ao_evento = ao_evento
        self.central = None
        self._canais: dict[str, object] = {}
        self._trancas: dict[str, threading.Lock] = {}
        self._tranca = threading.Lock()

    def _registar(self, texto: str) -> None:
        self._escrever(f"canal | {texto}")

    def iniciar(self) -> "CanalDasSessoes":
        nomes = [projeto.nome for projeto in self.config.projetos]
        if nomes and self.central is None:
            extra = {} if self._ao_evento is None else {"ao_evento": self._ao_evento}
            self.central = self._criar_central(nomes, log=self._registar, **extra).iniciar()
        return self

    def aberta(self, projeto: str) -> bool:
        with self._tranca:
            return projeto in self._canais

    def _tranca_do(self, projeto: str) -> threading.Lock:
        with self._tranca:
            return self._trancas.setdefault(projeto, threading.Lock())

    def enviar(self, projeto: str, texto: str, ao_responder: Callable[[str, object], None]) -> bool:
        alvo = self.config.encontrar_projeto(projeto)
        if alvo is None:
            raise ValueError(f"projeto '{projeto}' nao esta na configuracao")
        if self.central is None:
            raise RuntimeError("o canal nao foi iniciado")
        ja_aberta = self.aberta(alvo.nome)
        threading.Thread(
            target=self._entregar, args=(alvo, texto, ao_responder), name=f"canal-{alvo.nome}", daemon=True
        ).start()
        return ja_aberta

    def _entregar(self, alvo, texto: str, ao_responder: Callable[[str, object], None]) -> None:
        with self._tranca_do(alvo.nome):
            try:
                with self._tranca:
                    canal = self._canais.get(alvo.nome)
                if canal is None:
                    sessao = self._abrir(alvo, log=self._registar)
                    canal = self._criar_canal(sessao, self.central, log=self._registar)
                    with self._tranca:
                        self._canais[alvo.nome] = canal
                entrega = canal.entregar(texto, self.limite_s)
            except Exception as erro:  # noqa: BLE001 - a falha e dita, nunca derruba o jarvis
                entrega = self._sessoes.Entrega(projeto=alvo.nome, caminho="nenhum", erro=str(erro))
        ao_responder(alvo.nome, entrega)

    def fechar(self) -> None:
        with self._tranca:
            canais = list(self._canais.values())
            self._canais.clear()
        for canal in canais:
            try:
                canal.fechar()
            except Exception:  # noqa: BLE001 - fechar nunca impede os outros de fechar
                pass
        if self.central is not None:
            self.central.parar()
            self.central = None


# --- O processo ---------------------------------------------------------------------


@dataclass
class EstadoDoProcesso:
    """O que so existe enquanto o jarvis esta a correr."""

    adormecido: bool = False
    mudo: bool = False
    #: O aviso "estou a dormir" ja foi dito neste sono (so se diz uma vez).
    aviso_de_sono_dado: bool = False


@dataclass
class MedidaDaFrase:
    """Os numeros de uma frase, para o log e para `scripts/medir_ponta_a_ponta.py`."""

    numero: int
    texto: str
    gatilho: str
    fim_da_fala: float
    #: Fim da fala -> linha A PENSAR (com o texto transcrito).
    sinal_de_vida_ms: float | None = None
    #: Fim da fala -> primeiro bloco de audio da primeira coisa dita (resposta ou recap).
    primeira_fala_ms: float | None = None
    intencao: str | None = None
    projeto: str | None = None
    desfecho: str | None = None
    resposta_ao_recap: bool = False
    primeira_fala: str | None = None
    #: Da ultima voz ao primeiro audio, etapa a etapa (a linha "tempos" do log).
    tempos: TemposDaResposta | None = None


def _falar_com_som(texto: str) -> voz.ResultadoFala:
    # O jarvis a serio tem de falar: e o unico sitio que liga as colunas.
    return voz.falar(texto, com_som=True)


def construir_sons(config: Config, *, modo_ficheiro: bool, com_som: bool) -> Callable[[str], object] | None:
    """Os sons de abrir e fechar a escuta sem palavra de ativacao, ou None (sem som).

    Com o microfone tocam salvo `[escuta] sons = false`; com --wav so com
    --com-som (e tambem so se `sons` estiver ligado).
    """
    escuta = config.escuta
    if not escuta.sons or (modo_ficheiro and not com_som):
        return None

    def tocar(tipo: str) -> object:
        return sinais.tocar(tipo, volume=escuta.volume, com_som=True)

    return tocar


@dataclass
class _FalaDaResposta:
    """Como esta a correr a fala de uma resposta geral dita aos bocados."""

    #: Os silencios pedidos quando a primeira parte comecou (um novo corta o resto).
    silencios: int | None = None
    #: Ja saiu audio da resposta (a latencia da primeira frase ja foi escrita).
    soou: bool = False
    #: Partes da resposta ja entregues a voz.
    partes: int = 0
    #: Todas as partes foram mesmo faladas (nao so mostradas no ecra).
    inteira: bool = True
    #: Nada mais se diz: silencio, pedido novo ou a dormir.
    parada: bool = False

    def parar(self) -> None:
        self.parada = True


@dataclass
class _FalaAtual:
    """O que a voz esta a dizer agora, para uma interrupcao saber o que parou."""

    texto: str
    #: A pergunta geral de que esta fala e parte, ou None.
    consulta: Consulta | None = None
    #: Parada de vez por fala a serio dita por cima.
    interrompida: bool = False


class Jarvis:
    """Liga o ouvido ao interprete, a confirmacao, ao executor e a voz.

    As pecas externas entram pelo construtor (interprete, canal, falar,
    calar, executar_local) para os testes correrem a cadeia inteira sem
    microfone, sem Ollama, sem Claude Code e sem som.
    """

    def __init__(
        self,
        config: Config,
        log,
        *,
        interprete: Interprete,
        canal: CanalParaSessoes | None = None,
        forja: ForjaPorVoz | None = None,
        perguntas: PerguntasGerais | None = None,
        falar: Callable[[str], voz.ResultadoFala] = _falar_com_som,
        calar: Callable[..., voz.ResultadoSilencio] = voz.calar_agora,
        pausar: Callable[[], float | None] = voz.pausar_agora,
        retomar: Callable[[], bool] = voz.retomar_agora,
        executar_local: Callable[..., acoes_locais.ResultadoAcao] = acoes_locais.executar_pedido,
        com_voz: bool = True,
        relogio: Callable[[], float] = time.perf_counter,
        painel: Painel | None = None,
        limite_da_confirmacao_s: float | None = None,
        avisos: Avisos | None = None,
        janela_de_conversa_s: float = conversa.JANELA_S,
        sons: Callable[[str], object] | None = None,
        historico: HistoricoDePerguntas | None = None,
        caderno: CadernoDeFactos | None = None,
        frases_recentes: FrasesRecentes | None = None,
        espera_do_aviso_s: float = ESPERA_DO_AVISO_S,
        aleatorio: random.Random | None = None,
        persona: Persona | None = None,
        interromper: bool | None = None,
    ) -> None:
        self.config = config
        self.log = log
        self.interprete = interprete
        # Com o modelo do interprete a carregar, o jarvis diz "um momento".
        self.interprete.ao_demorar = self._avisar_demora
        self.canal = canal
        self.forja = forja
        self.perguntas = perguntas
        #: As ultimas perguntas gerais respondidas (so em RAM, com teto e prazo).
        self.historico = historico if historico is not None else HistoricoDePerguntas.da_config(config.memoria)
        #: O caderno de factos; None: os comandos de factos dizem que nao esta disponivel.
        self.caderno = caderno
        #: As ultimas frases interpretadas e o que o jarvis fez com elas, para
        #: o interprete local perceber "send that to jarvis too" (so em RAM).
        self.frases_recentes = (
            frases_recentes if frases_recentes is not None else FrasesRecentes.da_config(config.memoria)
        )
        #: O numero (em `frases_recentes`) da frase cujo recap esta pendente.
        self._frase_do_recap: int | None = None
        self.lingua ="en" if config.ouvido.lingua == "en" else "pt"
        self._falar = falar
        self._calar = calar
        self._pausar = pausar
        self._retomar = retomar
        self._executar_local = executar_local
        self.com_voz = com_voz
        #: Falar por cima da voz pausa-a (`[escuta] interromper`; sem voz nao ha nada a interromper).
        self.interromper = (config.escuta.interromper if interromper is None else interromper) and com_voz
        self.relogio = relogio
        self.estado = EstadoDoProcesso()
        self.painel = painel or Painel(log.linha)
        self.confirmacao = Confirmacao(
            interprete,
            self._executar,
            falar=self._dizer,
            mostrar=self._mostrar,
            lingua=self.lingua,
            limite_s=limite_da_confirmacao_s,
            relogio=relogio,
            persona=persona,
        )
        #: Avisos por voz (sessao acabou ou a espera, run FORJA mudou).
        self.avisos = avisos or Avisos(self._entregar_aviso, lingua=self.lingua, escrever=self.log.linha)
        #: Sondagem dos runs FORJA, ligada por `main` quando ha [forja].
        self.vigia: VigiaDosRuns | None = None
        #: Janela de escuta de uma conversa com o Claude (mesmo relogio das frases).
        self.janela = conversa.JanelaDeConversa(limite_s=janela_de_conversa_s, relogio=relogio)
        #: Janela de seguimento: depois de qualquer resposta falada, continuar
        #: sem palavra de ativacao durante [escuta].seguimento_s.
        self.seguimento = conversa.JanelaDeConversa(limite_s=config.escuta.seguimento_s, relogio=relogio)
        #: Toca "abrir" ou "fechar" (`jarvis.sinais`); None nos testes: sem som.
        self._sons = sons
        #: O ouvido, quando existe: abre e fecha a escuta sem palavra de ativacao.
        self.ouvido: Ouvido | None = None
        self.medidas: list[MedidaDaFrase] = []
        #: O que o arranque mediu (tempos, VRAM), preenchido por `correr`.
        self.arranque: Arranque | None = None
        self.frases = 0
        self.frases_concluidas = 0
        # Uma frase (ou um prazo, ou uma resposta do canal) de cada vez.
        self._tranca = threading.RLock()
        # Nunca duas falas ao mesmo tempo.
        self._tranca_da_voz = threading.Lock()
        self._condicao = threading.Condition()
        self._pendentes = 0
        self._contador = threading.Lock()
        self._local = threading.local()
        self._fila: queue.Queue | None = None
        self._fio: threading.Thread | None = None
        #: O ouvido tem (ou teve ha pouco) uma escuta sem palavra de ativacao
        #: aberta para a resposta ao recap pendente.
        self._a_ouvir_o_recap = False
        #: A escuta sem palavra de ativacao que o jarvis abriu (recap, conversa
        #: ou seguimento) e cujo som de fecho ainda nao tocou; None sem ela.
        #: Enquanto nao e None, reabrir a escuta (depois de uma resposta, de
        #: um recap ou de ruido) continua a mesma conversa e nao toca nada.
        self._escuta_de: str | None = None
        #: A ultima fala foi uma resposta que abre a janela de seguimento.
        self._falou_resposta = False
        #: Conta os silencios pedidos (cala-te, clique na bolinha): uma fala
        #: interrompida por um deles nao abre a janela de seguimento.
        self._silencios = 0
        #: A linha de estado ainda tem de dizer que a escuta fechou.
        self._escuta_fechou = False
        #: A janela de seguimento aberta por uma resposta geral que acabou numa
        #: pergunta: a frase dita nela continua a pergunta geral. So vale
        #: enquanto for a mesma janela (`seguimento.atual`); qualquer fala
        #: nova, silencio ou pedido fecha-a e ela deixa de contar.
        self._continuacao: conversa.EstadoDaJanela | None = None
        #: A pergunta geral em curso e a thread que espera pela resposta. So a
        #: consulta mais recente pode ser dita.
        self._tranca_da_pergunta = threading.Lock()
        self._consulta: Consulta | None = None
        self._fio_da_pergunta: threading.Thread | None = None
        #: Sem a primeira frase da resposta ao fim disto, diz-se o aviso curto.
        self.espera_do_aviso_s = espera_do_aviso_s
        #: Escolhe as variantes das frases (nunca a mesma duas vezes seguidas).
        self._aleatorio = aleatorio or random.Random()
        self._variantes = Variantes(self._aleatorio)
        #: Quem escreve as respostas sociais com o LLM local; None: so as frases fixas.
        self.persona = persona
        #: A bolinha de estado, quando ha janela (`ligar_bolinha`).
        self.bolinha: PonteDaBolinha | None = None
        #: Interromper: o que a voz diz agora, as falas por cima ainda por
        #: decidir e desde quando a voz esta em pausa (None: nao esta).
        self._tranca_da_interrupcao = threading.Lock()
        self._fala_atual: _FalaAtual | None = None
        self._interrupcoes_pendentes = 0
        self._voz_pausada_em: float | None = None
        #: Uma pergunta geral cuja resposta foi interrompida: continua a chegar
        #: so para o ecra; nunca mais e dita.
        self._consulta_largada: Consulta | None = None

    # -- textos

    def _texto(self, chave: str, **valores: str) -> str:
        """Uma das variantes da frase, nunca a mesma que da ultima vez."""
        return self._variantes.escolher(chave, _TEXTOS[self.lingua][chave]).format(**valores)

    def _texto_gerado(self, caso: str, chave: str, frase_ouvida: str | None) -> str:
        """A frase do LLM local para `caso`, ou a variante fixa de `chave` (prazo, falha ou filtro)."""
        if self.persona is not None:
            try:
                gerada = self.persona.frase(caso, frase_ouvida)
            except Exception as erro:  # noqa: BLE001 - a persona nunca impede a resposta
                self.log.linha(f"persona | falhou ({type(erro).__name__}); frase fixa")
                gerada = None
            if gerada:
                return gerada
        return self._texto(chave)

    # -- ciclo de vida

    def iniciar(self) -> None:
        """Passa a tratar as frases numa thread propria (o ouvido nunca espera pela voz)."""
        if self._fio is not None:
            return
        self._fila = queue.Queue(maxsize=FRASES_EM_ESPERA)
        self._fio = threading.Thread(target=self._tratar_em_ciclo, name="jarvis-frases", daemon=True)
        self._fio.start()
        self.avisos.iniciar()
        if self.vigia is not None:
            self.vigia.iniciar()
        self._mostrar_repouso()

    def fechar(self) -> None:
        self._cancelar_pergunta("o jarvis vai fechar")
        self._cancelar_a_largada("o jarvis vai fechar")
        if self.vigia is not None:
            self.vigia.parar()
        self.avisos.parar()
        if self._fila is not None and self._fio is not None:
            self._fila.put(None)
            self._fio.join(timeout=5.0)
        self._fio = None
        self._fila = None
        canal, self.canal = self.canal, None
        if canal is not None:
            try:
                canal.fechar()
            except Exception as erro:  # noqa: BLE001 - fechar nunca levanta
                self.log.linha(f"canal | erro ao fechar: {erro!r}")

    def ligar_bolinha(self, ponte: PonteDaBolinha) -> None:
        """A bolinha passa a seguir o painel, a escuta, o recap e a voz."""
        self.bolinha = ponte
        self.painel.ao_mudar = ponte.painel
        self.confirmacao.ao_propor = self._legenda_do_recap
        voz.definir_ouvinte_da_voz(ponte.nivel_da_voz)
        if self.painel.atual is not None:
            ponte.painel(self.painel.atual)
        ponte.atualizar()

    def desligar_bolinha(self) -> None:
        if self.bolinha is None:
            return
        voz.definir_ouvinte_da_voz(None)
        self.painel.ao_mudar = None
        self.confirmacao.ao_propor = None
        self.bolinha = None

    def ao_evento_do_ouvido(self, evento: str) -> None:
        if self.bolinha is not None:
            self.bolinha.ouvido(evento)

    def ao_audio_do_microfone(self, chunk: bytes) -> None:
        if self.bolinha is not None:
            self.bolinha.nivel_do_microfone(chunk)

    def _legenda(self, texto: str) -> None:
        if self.bolinha is not None:
            self.bolinha.legenda(texto)

    def _legenda_do_recap(self, recap) -> None:
        """Durante o recap, a legenda mostra o texto a enviar."""
        primeira = recap.ecra.splitlines()[0] if recap.ecra else ""
        self._legenda(recap.pedido.prompt or primeira)

    def _erro_na_bolinha(self) -> None:
        if self.bolinha is not None:
            self.bolinha.erro()

    def calar_pela_bolinha(self) -> None:
        """Um clique na bolinha: cala como o "cala-te" dito, sem fechar o jarvis."""
        self._calar_ja("clique na bolinha", "clique na bolinha")

    def _calar_ja(self, motivo: str, curto: str) -> None:
        self._silencios += 1
        self.calar_agora(motivo, definitivo=False)
        self.avisos.descartar(curto)
        self._fechar_conversa(curto)
        self._terminar_escuta(curto)
        self._mostrar_repouso()

    def ocioso(self) -> bool:
        with self._condicao:
            return self._pendentes == 0

    def esperar_ocioso(self, limite_s: float) -> bool:
        with self._condicao:
            return self._condicao.wait_for(lambda: self._pendentes == 0, limite_s)

    def _estado_de_repouso(self) -> str:
        if self.estado.adormecido:
            return A_DORMIR
        if self.confirmacao.a_espera:
            return A_ESPERA_A_OUVIR if self._a_ouvir_o_recap else A_ESPERA
        if self.janela.aberta():
            return A_CONVERSA
        return A_OUVIR_TE if self.seguimento.aberta() else A_OUVIR

    def _mostrar_repouso(self) -> None:
        """A linha de estado em repouso, com o detalhe da escuta sem palavra de ativacao."""
        estado = self._estado_de_repouso()
        detalhe = ""
        if estado == A_OUVIR_TE and self.painel.atual != A_OUVIR_TE:
            detalhe = f"sem palavra de ativacao, {self.seguimento.limite_s:.0f} s"
        elif estado == A_OUVIR and self._escuta_fechou and self.painel.atual != A_OUVIR:
            detalhe = ESCUTA_FECHADA
        self._escuta_fechou = False
        self.painel.mudar(estado, detalhe)

    # -- entrada: uma frase transcrita pelo ouvido

    def ao_ouvir(self, frase: Frase) -> None:
        """Callback do ouvido. Rapido: mostra o sinal de vida e poe a frase na fila."""
        if frase.gatilho == GATILHO_INTERRUPCAO and not self._aceitar_interrupcao(frase):
            return
        with self._contador:
            self.frases += 1
            numero = self.frases
        sinal = self.relogio()
        self.painel.mudar(A_PENSAR, f"frase #{numero}: {frase.texto!r}")
        self._legenda(frase.texto)
        medida = MedidaDaFrase(
            numero=numero,
            texto=frase.texto,
            gatilho=frase.gatilho,
            fim_da_fala=frase.fim_da_escuta,
            sinal_de_vida_ms=(sinal - frase.fim_da_escuta) * 1000,
        )
        if self._acao_rapida(frase.texto) == "calar":
            # Dito por cima da voz: cala ja, sem esperar que a fala acabe.
            self._calar_ja("cala-te dito ao jarvis", "cala-te")
        with self._condicao:
            self._pendentes += 1
        if self._fila is None:
            self._tratar(frase, medida)
            return
        try:
            self._fila.put_nowait((frase, medida))
        except queue.Full:
            self.log.linha(
                f"frase #{numero} | descartada: {FRASES_EM_ESPERA} frases ainda por tratar | nada executado"
            )
            self._concluir()

    # -- interromper: fala por cima da voz

    def ao_interromper(self, evento: str, instante: float) -> None:
        """Callback do ouvido: a fala por cima da voz comecou (pausa ja) ou perdeu-se (retoma)."""
        if evento == INTERRUPCAO_PAUSAR:
            with self._tranca_da_interrupcao:
                self._interrupcoes_pendentes += 1
            parada = self._pausar()
            if parada is None:
                self.log.linha(
                    "interrupcao | fala por cima, mas a voz ja nao estava a tocar; "
                    "a frase segue sem palavra de ativacao"
                )
                return
            with self._tranca_da_interrupcao:
                self._voz_pausada_em = self.relogio()
            self.log.linha(
                f"interrupcao | voz parada: {(parada - instante) * 1000:.0f} ms desde o inicio da fala "
                f"| inicio da fala: {agora_iso(self._instante_de_parede(instante))} "
                f"| voz parada: {agora_iso(self._instante_de_parede(parada))}"
            )
            self.painel.mudar(A_OUVIR, "a ouvir por cima da voz; a resposta esta em pausa")
        elif evento == INTERRUPCAO_RETOMAR:
            self._resolver_interrupcao(False, "nada transcrito (ruido, tosse ou respiracao)")

    def _aceitar_interrupcao(self, frase: Frase) -> bool:
        """Decide a frase dita por cima da voz: True se segue como pedido (e a voz parou de vez)."""
        texto = frase.texto
        rapida = self._acao_rapida(texto)
        if rapida is not None:
            return self._resolver_interrupcao(True, f"'{rapida}' dito por cima da voz", calar=rapida != "calar")
        if self.confirmacao.a_espera and classificar_resposta(texto)[0] == "confirmar":
            # O "sim" so conta depois de o recap ser ouvido inteiro.
            return self._resolver_interrupcao(
                False, f"confirmacao dita durante o recap ({texto!r}); o recap acaba e o sim diz-se depois",
                retomada=True,
            )
        if e_so_acompanhamento(texto, self.lingua):
            return self._resolver_interrupcao(False, f"so um acompanhamento ou hesitacoes ({texto!r})")
        return self._resolver_interrupcao(True, f"fala a serio por cima da voz ({texto!r})")

    def _resolver_interrupcao(
        self, a_serio: bool, motivo: str, *, retomada: bool = False, calar: bool = True
    ) -> bool:
        """Retoma a voz pausada (fala que nao conta) ou para-a de vez (fala a serio). Devolve `a_serio`."""
        with self._tranca_da_interrupcao:
            self._interrupcoes_pendentes = max(0, self._interrupcoes_pendentes - 1)
            ultima = self._interrupcoes_pendentes == 0
            pausada = self._voz_pausada_em is not None
            if pausada and (a_serio or ultima):
                self._voz_pausada_em = None
            fala = self._fala_atual
        if not a_serio:
            if not pausada:
                self.log.linha(f"interrupcao | ignorada: {motivo} | a voz ja nao estava em pausa")
            elif ultima:
                self._retomar()
                self.painel.mudar(A_FALAR)
                self.log.linha(f"interrupcao | {'retomada' if retomada else 'falsa'}: {motivo} | a voz continua")
            else:
                self.log.linha(
                    f"interrupcao | {'retomada' if retomada else 'falsa'}: {motivo} "
                    "| outra fala por cima ainda por decidir; a voz continua em pausa"
                )
            return False
        if not pausada and fala is None:
            self.log.linha(f"interrupcao | a serio: {motivo} | a voz ja nao estava a tocar; a frase segue")
            return True
        # Pausada, ou ainda a tocar (a pausa nao chegou a tempo, ou ja e a parte seguinte da resposta).
        if fala is not None:
            fala.interrompida = True
            if fala.consulta is not None:
                self._largar_pergunta(fala.consulta)
        # A fala interrompida nao abre a janela de seguimento: a frase dita por cima segue ja.
        self._silencios += 1
        if calar:
            self._calar(f"interrupcao: {motivo}", definitivo=False, registar=self.log.linha)
        self.log.linha(f"interrupcao | a serio: {motivo} | a voz parou de vez; o resto fica no ecra")
        return True

    def _vigiar_a_pausa(self) -> None:
        """Uma voz em pausa sem frase a caminho continua (a frase perdeu-se)."""
        with self._tranca_da_interrupcao:
            pausada_em = self._voz_pausada_em
        if pausada_em is None or getattr(self.ouvido, "a_ouvir_alguem", False):
            return
        if self.relogio() - pausada_em < PAUSA_SEM_FRASE_S:
            return
        with self._tranca_da_interrupcao:
            if self._voz_pausada_em is not pausada_em:
                return
            self._voz_pausada_em = None
            self._interrupcoes_pendentes = 0
        self._retomar()
        self.painel.mudar(A_FALAR)
        self.log.linha(
            f"interrupcao | falsa: nenhuma frase chegou em {PAUSA_SEM_FRASE_S:g} s depois da pausa | a voz continua"
        )

    def _voz_a_falar(self, a_falar: bool) -> None:
        """Diz ao ouvido quando a voz fala (so entao ouve por cima dela)."""
        if not self.interromper:
            return
        definir = getattr(self.ouvido, "definir_voz_a_falar", None)
        if definir is not None:
            definir(a_falar)

    def ao_ativar_sem_fala(self, frase: Frase) -> None:
        """Callback do ouvido: so a palavra de ativacao, sem fala depois.

        Acordado nao ha nada a fazer. A dormir segue como uma frase de texto
        vazio, que acorda se o score chegar ao limiar.
        """
        if not self.estado.adormecido:
            self.log.linha(
                f"palavra de ativacao sem fala (score {frase.score_ativacao or 0:.2f}) "
                "| o jarvis esta acordado: nada a fazer"
            )
            return
        self.ao_ouvir(frase)

    def _tratar_em_ciclo(self) -> None:
        fila = self._fila
        assert fila is not None
        while True:
            item = fila.get()
            if item is None:
                return
            self._tratar(*item)

    def _concluir(self) -> None:
        with self._condicao:
            self._pendentes -= 1
            self.frases_concluidas += 1
            self._condicao.notify_all()

    def _acao_rapida(self, texto: str) -> str | None:
        """calar/dormir/acordar pela lista branca (frase inteira), sem LLM."""
        try:
            resultado = encaminhar(texto, self.config)
        except Exception:  # noqa: BLE001 - na duvida, a frase segue o caminho normal
            return None
        if resultado.tipo != "local":
            return None
        return ACOES_QUE_PASSAM_A_FRENTE.get(resultado.nome_acao or "")

    def _tratar(self, frase: Frase, medida: MedidaDaFrase) -> None:
        try:
            with self._tranca:
                try:
                    self._tratar_com_tranca(frase, medida)
                finally:
                    # A voz ja acabou: abre (ou fecha) a escuta sem palavra de ativacao.
                    self._assentar_escuta()
        except Exception as erro:  # noqa: BLE001 - uma frase falhada nunca para o jarvis
            self.log.linha(f"frase #{medida.numero} | ERRO no tratamento: {erro!r} | nada enviado")
            self._erro_na_bolinha()
        finally:
            self._local.registo = None
            self._local.medida = None
            self.medidas.append(medida)
            self._mostrar_repouso()
            self._concluir()

    def _tratar_com_tranca(self, frase: Frase, medida: MedidaDaFrase) -> None:
        registo = RegistoDaFrase(
            numero=medida.numero, log=self.log, relogio=self.relogio, inicio=frase.inicio_da_escuta
        )
        registo.inicio_da_fala = frase.inicio_da_escuta
        registo.fim_da_fala = frase.fim_da_escuta
        registo.ultima_voz = getattr(frase, "ultima_voz", None)
        registo.texto_pronto = frase.texto_pronto
        self._local.registo = registo
        self._local.medida = medida
        if frase.gatilho == GATILHO_TECLA:
            gatilho = "tecla de falar"
        elif frase.gatilho == GATILHO_INTERRUPCAO:
            gatilho = "fala por cima da voz do jarvis (sem palavra de ativacao)"
        elif frase.gatilho == GATILHO_JANELA and self.confirmacao.a_espera:
            gatilho = "resposta ao recap (escuta sem palavra de ativacao)"
        elif frase.gatilho == GATILHO_JANELA and self.janela.aberta():
            gatilho = "janela de conversa (sem palavra de ativacao)"
        elif frase.gatilho == GATILHO_JANELA and self._na_continuacao(frase):
            gatilho = "escuta de seguimento depois de uma pergunta geral (sem palavra de ativacao)"
        elif frase.gatilho == GATILHO_JANELA:
            gatilho = "escuta de seguimento (sem palavra de ativacao)"
        else:
            palavra = PALAVRAS_DE_ATIVACAO.get(self.config.ouvido.lingua, "?")
            gatilho = f"palavra de ativacao '{palavra}' (score {frase.score_ativacao or 0:.2f})"
        registo.marcar(1, f"{gatilho} | audio {frase.duracao_audio_s:.2f} s", instante=frase.inicio_da_escuta)
        registo.marcar(
            2,
            f"motor={frase.motor} inferencia={frase.latencia_stt_ms:.0f} ms "
            f"| palavra de ativacao retirada: {frase.palavra_retirada or 'nenhuma'!r} | texto: {frase.texto!r}",
            desde=frase.fim_da_escuta,
            instante=frase.texto_pronto,
        )
        registo.nota(f"primeiro sinal de vida: {medida.sinal_de_vida_ms:.0f} ms desde o fim da fala (linha A PENSAR)")
        if registo.ultima_voz is not None:
            registo.nota(
                f"ultima voz: {agora_iso(self._instante_de_parede(registo.ultima_voz))} "
                f"| {(frase.fim_da_escuta - registo.ultima_voz) * 1000:.0f} ms antes do fim da escuta"
            )

        if not frase.texto.strip() and not self.estado.adormecido:
            # So a palavra de ativacao, e o jarvis ja acordou entretanto (ou
            # nada dito numa escuta sem ela): a janela de seguimento continua.
            registo.marcar(3, "so a palavra de ativacao com o jarvis acordado (nada interpretado)")
            medida.desfecho = "ignorada"
            registo.fechar("ignorada (so a palavra de ativacao)")
            return

        rapida = self._acao_rapida(frase.texto)
        acordar_pela_palavra = False
        if self.estado.adormecido and rapida != "acordar":
            # A dormir so a palavra de ativacao acima do limiar conta; calar ou
            # dormir outra vez nao acordam o jarvis.
            if not self._ativacao_acima_do_limiar(frase) or rapida in ("calar", "dormir"):
                self._ignorar_a_dormir(frase, registo, medida)
                return
            if e_acordar_curto(frase.texto, self.config.ouvido.lingua):
                acordar_pela_palavra = True
            else:
                self._acordar_com_pedido(registo)

        if acordar_pela_palavra:
            registo.marcar(3, "palavra de ativacao seguida de um acordar curto (nada interpretado)")
            desfecho = self._decidir(
                Interpretacao(frase.texto, "acordar", None, "", "regra", "acordar pela palavra de ativacao")
            )
        elif self.confirmacao.a_espera and rapida is None:
            medida.resposta_ao_recap = True
            registo.marcar(3, "resposta ao recap pendente")
            # Dita por cima do recap (que parou por causa dela): conta como resposta.
            dito_em = None if frase.gatilho == GATILHO_INTERRUPCAO else frase.inicio_da_escuta
            desfecho = self.confirmacao.responder(frase.texto, dito_em=dito_em)
            self._desfecho_do_recap(desfecho)
        elif rapida is None and self.janela.aceita(frase.inicio_da_escuta):
            desfecho = self._responder_na_conversa(frase, registo, medida)
        elif rapida is None and self._fecha_o_seguimento(frase):
            # "that's all" / "thanks, that's it": a conversa acaba, com o som de fecho.
            medida.intencao = "fechar_escuta"
            registo.marcar(3, "modo de conversa: fechar a escuta a pedido (nada interpretado, nada dito)")
            self._terminar_escuta("fechada a pedido")
            desfecho = Desfecho("cancelado", "escuta sem palavra de ativacao fechada a pedido")
        elif rapida is None and self._ruido_no_seguimento(frase):
            # Hesitacoes ou cortesia soltas na janela de seguimento: nada dito,
            # e a janela continua ate ao prazo que ja tinha.
            medida.intencao = INTENCAO_CORTESIA if so_cortesia(frase.texto) else "ruido"
            registo.marcar(3, "escuta de seguimento: so hesitacoes ou cortesia (nada interpretado, nada dito)")
            desfecho = Desfecho("ignorado", "ruido na janela de seguimento")
        elif rapida is None and self._continua_a_pergunta(frase, registo):
            desfecho = self._continuar_a_pergunta(frase, registo, medida)
        elif rapida is None and self.confirmacao.e_correcao_sem_pedido(frase.texto):
            # "nao, muda X para Y" sem nada a espera nao vira um ditado novo.
            registo.marcar(3, "correcao sem nenhum pedido pendente (nada interpretado)")
            self._consumir_seguimento()
            desfecho = self.confirmacao.correcao_sem_pedido()
        elif rapida is None and so_cortesia(frase.texto):
            # "Excellent.", "Yeah." soltos: nada a pedir, nunca vao ao LLM nem
            # ao Claude, e nao cortam uma resposta que ainda esteja a caminho.
            medida.intencao = INTENCAO_CORTESIA
            registo.marcar(3, "so cortesia fora de um recap ou de uma conversa (nada interpretado)")
            self._consumir_seguimento()
            desfecho = self._decidir(Interpretacao(frase.texto, INTENCAO_CORTESIA, None, "", "regra", "so cortesia"))
        else:
            # Ouvida sem palavra de ativacao nem tecla: pode ser conversa a volta,
            # por isso so gasta a janela (e cancela o resto) se for um pedido.
            de_fundo = rapida is None and self._so_pela_escuta_de_seguimento(frase)
            if not de_fundo:
                self._tomar_o_pedido(rapida)
            contexto = contexto_do_interprete(frase.texto, self.frases_recentes, self.caderno)
            interpretacao = self._interpretar(frase.texto, contexto)
            medida.intencao, medida.projeto = interpretacao.intencao, interpretacao.projeto
            registo.marcar(3, self._detalhe_da_interpretacao(interpretacao))
            fundo = self._fala_de_fundo(frase, interpretacao) if de_fundo else None
            if fundo is not None:
                registo.nota(f"modo de conversa: {fundo}; nada dito, a escuta continua ate ao prazo que ja tinha")
                desfecho = Desfecho("ignorado", f"fala de fundo na escuta de seguimento ({fundo})")
            else:
                if de_fundo:
                    self._tomar_o_pedido(rapida)
                desfecho = self._decidir(interpretacao)
                self._lembrar_frase(interpretacao, desfecho)
        if desfecho is not None:
            medida.desfecho = desfecho.estado
            if desfecho.pedido is not None:
                medida.intencao, medida.projeto = desfecho.pedido.intencao, desfecho.pedido.projeto
            registo.nota(f"desfecho: {desfecho.estado} | {desfecho.motivo}")
        if not registo.tem_etapa(4):
            registo.marcar(4, "nada a executar")
        registo.fechar(f"desfecho: {medida.desfecho}")

    # -- dormir

    def _ativacao_acima_do_limiar(self, frase: Frase) -> bool:
        """Frase aberta pela palavra de ativacao com score >= `[ouvido] limiar_ativacao`."""
        return (
            frase.gatilho == GATILHO_ATIVACAO
            and frase.score_ativacao is not None
            and frase.score_ativacao >= self.config.ouvido.limiar_ativacao
        )

    def _acordar_com_pedido(self, registo: RegistoDaFrase) -> None:
        """Palavra de ativacao seguida de um pedido: acorda sem frase propria e o pedido segue."""
        self.estado.adormecido = False
        self.estado.mudo = False
        registo.nota("acordado pela palavra de ativacao com um pedido: a frase segue o caminho normal")

    def _ignorar_a_dormir(self, frase: Frase, registo: RegistoDaFrase, medida: MedidaDaFrase) -> None:
        """A dormir nada e interpretado; com a palavra de ativacao, diz uma vez como se acorda."""
        medida.desfecho = "ignorada"
        if self._ativacao_acima_do_limiar(frase) and not self.estado.aviso_de_sono_dado:
            self.estado.aviso_de_sono_dado = True
            registo.marcar(3, "ignorada: o jarvis esta a dormir; diz uma vez como o acordar (nada interpretado)")
            self._dizer(self._texto("a_dormir"))
        else:
            registo.marcar(3, "ignorada: o jarvis esta a dormir (nada interpretado)")
        registo.fechar("ignorada (a dormir)")

    # -- conversa com o Claude

    def _responder_na_conversa(
        self, frase: Frase, registo: RegistoDaFrase, medida: MedidaDaFrase
    ) -> Desfecho | None:
        """A frase dita na janela: sair, uma resposta curta que vai logo, ou uma longa com recap."""
        estado = self.janela.fechar()
        self._fechar_escuta()
        if estado is None:  # fechada entretanto: a frase nao chega a ser tratada
            registo.marcar(3, "conversa: a janela ja tinha fechado (nada interpretado)")
            return None
        medida.projeto = estado.projeto
        if conversa.e_para_sair(frase.texto, self.lingua):
            medida.intencao = "sair_da_conversa"
            registo.marcar(3, f"conversa com o {estado.projeto}: sair (nada enviado)")
            self._dizer(self._texto("conversa_fim"), abre_seguimento=False)
            return Desfecho("cancelado", "saiu da conversa")
        nomes = tuple(projeto.nome for projeto in self.config.projetos)
        interpretacao = conversa.resposta_literal(frase.texto, estado.projeto, nomes, lingua=self.lingua)
        medida.intencao = interpretacao.intencao
        detalhe = self._detalhe_da_interpretacao(interpretacao)
        if interpretacao.intencao == INTENCAO_RECUSADA:
            registo.marcar(3, "conversa: resposta na janela recusada | " + detalhe)
            return self.confirmacao.iniciar(interpretacao)
        if not interpretacao.prompt:
            # So hesitacoes: nada a enviar, e a pergunta continua por responder.
            registo.marcar(3, "conversa: so hesitacoes na janela (nada interpretado)")
            self._abrir_conversa(estado.projeto)
            return Desfecho("ignorado", "resposta so com hesitacoes; a janela abre de novo")
        if conversa.e_resposta_curta(interpretacao.prompt):
            registo.marcar(3, "conversa: resposta curta na janela, enviada sem recap | " + detalhe)
            return self.confirmacao.enviar_sem_recap(interpretacao)
        registo.marcar(3, "conversa: resposta longa na janela, com recap | " + detalhe)
        return self.confirmacao.iniciar(interpretacao)

    def _abrir_conversa(self, projeto: str) -> None:
        """A resposta do Claude acabou numa pergunta: ouve a resposta sem palavra de ativacao."""
        self.seguimento.fechar()  # nunca duas janelas: a da conversa ganha
        self._falou_resposta = False
        self.janela.abrir(projeto)
        sem_ativacao = self._abrir_sem_ativacao(self.janela.limite_s, ESCUTA_CONVERSA)
        self.log.linha(
            f"conversa | o {projeto} fez uma pergunta: janela de {self.janela.limite_s:.0f} s "
            + ("a ouvir sem palavra de ativacao" if sem_ativacao else "so com a tecla de falar")
            + "; 'sai da conversa' fecha"
        )

    def _fechar_escuta(self) -> None:
        """Fecha a escuta sem palavra de ativacao no ouvido, sem som (ela pode reabrir logo)."""
        self._a_ouvir_o_recap = False
        fechar = getattr(self.ouvido, "fechar_escuta", None)
        if fechar is not None:
            fechar()

    # -- escuta sem palavra de ativacao: recap, conversa e seguimento

    def _som(self, tipo: str) -> None:
        if self._sons is None:
            return
        try:
            self._sons(tipo)
        except Exception as erro:  # noqa: BLE001 - um som falhado nunca para o jarvis
            self.log.linha(f"som | '{tipo}' falhou: {erro!r}")

    def _abrir_sem_ativacao(self, limite_s: float, para: str) -> bool:
        """Pede ao ouvido uma escuta sem palavra de ativacao; toca "abrir" so se abre pela primeira vez.

        Reabrir depois de uma resposta, de um recap ou de ruido, sem a escuta
        ter acabado (`_terminar_escuta`), e a mesma conversa: nao toca nada.
        Devolve False sem VAD: ai so ha a tecla de falar.
        """
        abrir = getattr(self.ouvido, "abrir_escuta", None)
        if abrir is None or not abrir(limite_s, para=para):
            return False
        nova = self._escuta_de is None
        self._escuta_de = para
        self._escuta_fechou = False
        if nova:
            self._som("abrir")
        return True

    def _terminar_escuta(self, motivo: str) -> None:
        """A escuta sem palavra de ativacao acaba sem continuar: fecha e toca "fechar"."""
        self.seguimento.fechar()
        self._continuacao = None
        self._fechar_escuta()
        para, self._escuta_de = self._escuta_de, None
        if para is None:
            return
        self._escuta_fechou = True
        self.log.linha(f"escuta | sem palavra de ativacao fechada ({motivo}); diz hey jarvis ou usa a tecla")
        self._som("fechar")

    def _consumir_seguimento(self) -> None:
        """Um pedido tomado gasta a janela de seguimento (a resposta dele pode abrir outra)."""
        self._continuacao = None
        if self.seguimento.fechar() is not None:
            self._fechar_escuta()

    # -- continuacao de uma pergunta geral que acabou numa pergunta

    def _na_continuacao(self, frase: Frase) -> bool:
        """A frase comecou na janela de seguimento aberta por uma resposta geral que acabou numa pergunta.

        So a mesma janela conta: uma fala nova (outra resposta, um aviso, o
        recap) fecha-a e abre outra, e uma frase que comecou antes dessa nao
        e aceite por ela.
        """
        continuacao = self._continuacao
        return (
            continuacao is not None
            and continuacao is self.seguimento.atual
            and self.seguimento.aceita(frase.inicio_da_escuta)
        )

    def _continua_a_pergunta(self, frase: Frase, registo: RegistoDaFrase) -> bool:
        """A frase dita na janela continua a pergunta geral, salvo as excecoes que passam a frente.

        Calar, dormir e acordar ja passaram antes (`_acao_rapida`). Aqui ficam
        de fora: so cortesia ("Thank you."), um comando da memoria ("new
        conversation") e um comando local da lista branca dito inteiro ("what
        time is it"). Um projeto nomeado decide-se em `_continuar_a_pergunta`.
        """
        if not self._na_continuacao(frase):
            return False
        texto = frase.texto
        if not conversa.limpar_resposta(texto, self.lingua):
            return False
        if so_cortesia(texto) and not conversa.responde_a_pergunta(texto):
            excecao = "so cortesia"
        elif comando_de_memoria(sem_palavra_de_ativacao(limpar_texto(texto))) is not None:
            excecao = "comando da memoria"
        elif self._comando_local(texto):
            excecao = "comando local da lista branca"
        else:
            return True
        registo.nota(f"continuacao da pergunta geral: nao ({excecao}); segue o caminho normal")
        return False

    def _comando_local(self, texto: str) -> bool:
        try:
            return encaminhar(texto, self.config).tipo == "local"
        except Exception:  # noqa: BLE001 - na duvida, continua a pergunta
            return False

    def _continuar_a_pergunta(
        self, frase: Frase, registo: RegistoDaFrase, medida: MedidaDaFrase
    ) -> Desfecho | None:
        """A frase continua a pergunta geral, com a memoria da conversa recente.

        Nunca vai as intencoes de projeto nem pergunta "Which project?". So uma
        frase que nomeia um projeto conhecido E que o interprete le como um
        pedido a esse projeto (ditado, estado, ...) segue o caminho normal.
        """
        self._consumir_seguimento()
        self._cancelar_pergunta("pedido novo")
        nomes = self._nomes_de_projeto()
        interpretacao: Interpretacao | None = None
        if projetos_mencionados(conversa.limpar_resposta(frase.texto, self.lingua), nomes):
            contexto = contexto_do_interprete(frase.texto, self.frases_recentes, self.caderno)
            lida = self._interpretar(frase.texto, contexto)
            if lida.intencao in INTENCOES_COM_PROJETO and lida.projeto:
                registo.marcar(
                    3,
                    f"continuacao da pergunta geral: nao, a frase nomeia o projeto {lida.projeto} | "
                    + self._detalhe_da_interpretacao(lida),
                )
                interpretacao = lida
        if interpretacao is None:
            interpretacao = conversa.pergunta_de_seguimento(frase.texto, nomes, lingua=self.lingua)
            registo.marcar(
                3,
                "continuacao da pergunta geral (a resposta anterior acabou numa pergunta; "
                "sem intencoes de projeto) | " + self._detalhe_da_interpretacao(interpretacao),
            )
        medida.intencao, medida.projeto = interpretacao.intencao, interpretacao.projeto
        desfecho = self._decidir(interpretacao)
        self._lembrar_frase(interpretacao, desfecho)
        return desfecho

    def _abrir_continuacao(self, falado: str) -> None:
        """A resposta geral dita acabou numa pergunta: a janela de seguimento que ela abriu continua-a."""
        janela = self.seguimento.atual
        if janela is None or not conversa.acaba_em_pergunta(falado):
            return
        self._continuacao = janela
        self.log.linha("seguimento | a resposta acabou numa pergunta: a frase seguinte continua a pergunta geral")

    def _tomar_o_pedido(self, rapida: str | None) -> None:
        """Uma frase que e um pedido fecha a conversa e a janela de seguimento, e cancela o que ja nao conta."""
        self._fechar_conversa(f"'{rapida}' dito" if rapida else "frase fora da janela")
        self._consumir_seguimento()
        # Um pedido novo: a resposta de uma pergunta anterior ja nao se diz.
        self._cancelar_pergunta("pedido novo")
        if self.confirmacao.a_espera and rapida == "dormir":
            self._desfecho_do_recap(self.confirmacao.cancelar("o jarvis foi dormir"))

    def _so_pela_escuta_de_seguimento(self, frase: Frase) -> bool:
        """A frase entrou pela janela de seguimento, sem tecla nem palavra de ativacao."""
        return frase.gatilho == GATILHO_JANELA and self.seguimento.aceita(frase.inicio_da_escuta)

    def _fecha_o_seguimento(self, frase: Frase) -> bool:
        """"that's all" / "thanks, that's it" dito com a janela de seguimento aberta (tecla e palavra tambem)."""
        return self.seguimento.aceita(frase.inicio_da_escuta) and conversa.e_para_fechar_a_escuta(
            frase.texto, self.lingua
        )

    def _fala_de_fundo(self, frase: Frase, interpretacao: Interpretacao) -> str | None:
        """Porque e que uma frase ouvida so pela janela de seguimento nao faz nada, ou None se e um pedido.

        Sem intencao, nada. Uma pergunta geral so segue se for dita como
        pergunta ao jarvis (`conversa.pergunta_dirigida`, ou com a palavra de
        ativacao dita dentro da janela): o interprete le muitas vezes a
        conversa a volta como pergunta geral, e essa iria ao Claude. O resto
        (recusas, recaps, leituras) segue as regras de sempre.
        """
        if interpretacao.intencao == "desconhecido":
            return "sem intencao"
        if (
            interpretacao.intencao == INTENCAO_PERGUNTA_GERAL
            and frase.palavra_retirada is None
            and not conversa.pergunta_dirigida(frase.texto, self.lingua)
        ):
            return "pergunta geral que nao e dita como pergunta ao jarvis"
        return None

    def _ruido_no_seguimento(self, frase: Frase) -> bool:
        """So hesitacoes ou so cortesia, ditas na janela de seguimento."""
        if frase.gatilho != GATILHO_JANELA or not self.seguimento.aceita(frase.inicio_da_escuta):
            return False
        if not conversa.limpar_resposta(frase.texto, self.lingua):
            return True
        if self._na_continuacao(frase) and conversa.responde_a_pergunta(frase.texto):
            return False  # "Yes." e a resposta a pergunta com que a resposta geral acabou
        return so_cortesia(frase.texto)

    def _assentar_escuta(self, *, seguimento: bool = True) -> None:
        """Depois de a voz acabar: abre a escuta que tem prioridade, ou fecha-a.

        Recap pendente > pergunta do Claude (conversa) > seguimento depois de
        uma resposta falada. A dormir nao ha escuta nenhuma. Chamar so com a
        voz calada (depois de `_dizer` voltar); `seguimento=False` quando a
        ultima fala nao pede continuacao (o recap cancelado por falta de
        resposta).
        """
        falou, self._falou_resposta = self._falou_resposta, False
        if self.estado.adormecido:
            self._terminar_escuta("a dormir")
            return
        restante = self.confirmacao.prazo_restante()
        if restante is not None:
            self.seguimento.fechar()
            if restante > 0:  # senao o prazo ja passou: `verificar_tempo` cancela
                self._ouvir_o_recap(restante)
            return
        estado = self.janela.atual
        if estado is not None:
            self.seguimento.fechar()
            restante = estado.prazo - self.relogio()
            if restante > 0 and getattr(self.ouvido, "escuta_aberta", None) != ESCUTA_CONVERSA:
                self._abrir_sem_ativacao(restante, ESCUTA_CONVERSA)
            return
        if falou and seguimento and not self.estado.mudo and self.com_voz:
            self.seguimento.abrir("")
            if self._abrir_sem_ativacao(self.seguimento.limite_s, ESCUTA_SEGUIMENTO):
                self.log.linha(
                    f"seguimento | a ouvir sem palavra de ativacao ({self.seguimento.limite_s:.0f} s)"
                )
                return
            self.seguimento.fechar()  # sem VAD: so a tecla e a palavra de ativacao
        elif self.seguimento.aberta() and self._reabrir_seguimento():
            return
        if self._escuta_de is not None and self._pergunta_a_caminho():
            # A resposta a uma pergunta geral ainda vem e reabre a escuta quando
            # for dita: a conversa continua, sem som de fecho nem de abertura.
            self.seguimento.fechar()
            self._continuacao = None
            self._fechar_escuta()
            self.log.linha("escuta | em pausa ate a resposta da pergunta geral ser dita")
            return
        self._terminar_escuta("nada a continuar")

    def _pergunta_a_caminho(self) -> bool:
        """Ha uma pergunta geral cuja resposta ainda vai ser dita por outra thread."""
        with self._tranca_da_pergunta:
            consulta, fio = self._consulta, self._fio_da_pergunta
        return (
            consulta is not None
            and not consulta.cancelada
            and fio is not None
            and fio.is_alive()
            and fio is not threading.current_thread()
        )

    def _ouvir_o_recap(self, restante: float) -> None:
        """Com um recap pendente, ouve a resposta sem palavra de ativacao durante o que falta do prazo."""
        sem_ativacao = self._abrir_sem_ativacao(restante, ESCUTA_RECAP)
        self._a_ouvir_o_recap = sem_ativacao
        if sem_ativacao:
            self.log.linha(f"confirmacao | a ouvir a resposta ao recap sem palavra de ativacao ({restante:.0f} s)")
        else:
            self.log.linha(
                f"confirmacao | resposta ao recap so com a tecla de falar ({restante:.0f} s): "
                "sem VAD nao ha escuta sem palavra de ativacao"
            )

    def _reabrir_escuta_do_recap(self) -> None:
        """A escuta do recap acabou sem frase (ruido descartado): volta a ouvir ate ao prazo."""
        if not self._a_ouvir_o_recap or not self.confirmacao.a_espera or self.estado.adormecido:
            return
        if getattr(self.ouvido, "escuta_aberta", ESCUTA_RECAP) is not None or self._alguem_a_responder():
            return
        restante = self.confirmacao.prazo_restante()
        if restante is None or restante <= 0:
            return
        if self._abrir_sem_ativacao(restante, ESCUTA_RECAP):
            self.log.linha(f"confirmacao | de novo a ouvir a resposta ao recap ({restante:.0f} s)")

    def _reabrir_seguimento(self) -> bool:
        """A janela de seguimento continua (ruido ignorado): ouve ate ao prazo ORIGINAL, sem som.

        Devolve False se o prazo ja passou ou se o ouvido nao abre.
        """
        estado = self.seguimento.atual
        if estado is None or self.estado.adormecido:
            return False
        restante = estado.prazo - self.relogio()
        if restante <= 0:
            return False
        if getattr(self.ouvido, "escuta_aberta", None) == ESCUTA_SEGUIMENTO:
            return True
        if not self._abrir_sem_ativacao(restante, ESCUTA_SEGUIMENTO):
            return False
        self.log.linha(f"seguimento | de novo a ouvir sem palavra de ativacao ({restante:.1f} s ate ao prazo)")
        return True

    def _alguem_a_responder(self) -> bool:
        """O utilizador comecou a dizer uma frase, ou ha uma a caminho do texto ou por tratar.

        Uma escuta sem palavra de ativacao ainda sem fala nao conta.
        """
        ouvido = self.ouvido
        ocupado = getattr(ouvido, "a_ouvir_alguem", getattr(ouvido, "ocupado", False))
        return bool(ocupado) or not self.ocioso()

    def _fechar_conversa(self, motivo: str) -> None:
        estado = self.janela.fechar()
        if estado is None:
            return
        self._fechar_escuta()
        self.log.linha(f"conversa | janela do {estado.projeto} fechada ({motivo}); nada enviado")

    def _alguem_a_falar(self) -> bool:
        """O utilizador esta a falar ou ha uma frase dele a caminho."""
        return bool(getattr(self.ouvido, "ocupado", False)) or not self.ocioso()

    # -- avisos

    def _entregar_aviso(self, aviso: Aviso) -> str:
        """Diz um aviso da fila se ninguem estiver a falar nem a espera; senao, espera."""
        if self.estado.adormecido:
            return DESCARTADO
        if self.estado.mudo or not self.com_voz or voz.esta_calado():
            return SO_ECRA
        if not self._tranca.acquire(blocking=False):
            return OCUPADO
        try:
            if (
                self._alguem_a_falar()
                or self.confirmacao.a_espera
                or self.janela.aberta()
                or self.seguimento.aberta()
            ):
                return OCUPADO
            self._local.registo = None
            self._local.medida = None
            self._dizer(aviso.texto, abre_seguimento=False)
            self._mostrar_repouso()
            return FALADO
        finally:
            self._tranca.release()

    def _interpretar(self, texto: str, contexto) -> Interpretacao:
        """O interprete, com o inicio e o fim da chamada guardados para os tempos da frase."""
        registo: RegistoDaFrase | None = getattr(self._local, "registo", None)
        inicio = self.relogio()
        interpretacao = self.interprete.interpretar(texto, contexto=contexto)
        if registo is not None:
            registo.interprete = (inicio, self.relogio(), interpretacao.origem)
        return interpretacao

    @staticmethod
    def _detalhe_da_interpretacao(interpretacao: Interpretacao) -> str:
        return (
            f"intencao={interpretacao.intencao} projeto={interpretacao.projeto or '-'} "
            f"origem={interpretacao.origem} modelo={interpretacao.modelo or '-'} "
            f"llm={interpretacao.latencia_s * 1000:.0f} ms | prompt: {interpretacao.prompt!r} "
            f"| motivo: {interpretacao.motivo}"
        )

    def _decidir(self, interpretacao: Interpretacao) -> Desfecho | None:
        if interpretacao.intencao == INTENCAO_CORTESIA:
            self._marcar_decisao("so cortesia: nada enviado")
            self._dizer(self._texto("cortesia"))
            return Desfecho("ignorado", "so cortesia")
        if interpretacao.intencao == INTENCAO_SOCIAL:
            # Conversa social curta: uma frase do LLM local (ou a fixa), sem Claude e sem recap.
            self._marcar_decisao(f"conversa social ({interpretacao.detalhe}): resposta local, nada enviado")
            self._dizer(
                self._texto_gerado(CASO_SOCIAL, f"social_{interpretacao.detalhe or 'como_estas'}", interpretacao.texto)
            )
            return Desfecho("executado", "conversa social: resposta local")
        if interpretacao.intencao == INTENCAO_MEMORIA:
            return self._memoria(interpretacao)
        return self.confirmacao.iniciar(interpretacao)

    # -- frases recentes para o interprete local

    def _lembrar_frase(self, interpretacao: Interpretacao, desfecho: Desfecho | None) -> None:
        """Guarda a frase interpretada e o que o jarvis fez com ela.

        Cortesia, conversa social e comandos da memoria ficam de fora: sao
        regras locais, nunca um pedido a que uma frase seguinte se refira.
        """
        estado = desfecho.estado if desfecho is not None else None
        if estado == "pendente" or not self.confirmacao.a_espera:
            # Um recap novo (ou nenhum) substitui o anterior.
            self._frase_do_recap = None
        if interpretacao.intencao in (INTENCAO_CORTESIA, INTENCAO_SOCIAL, INTENCAO_MEMORIA):
            return
        pedido = self._pedido_do_desfecho(desfecho)
        numero = self.frases_recentes.acrescentar(
            interpretacao.texto,
            interpretacao.intencao,
            pedido.projeto if pedido is not None else interpretacao.projeto,
            pedido.prompt if pedido is not None and pedido.prompt else interpretacao.prompt,
            estado,
        )
        if estado == "pendente":
            self._frase_do_recap = numero

    @staticmethod
    def _pedido_do_desfecho(desfecho: Desfecho | None) -> Pedido | None:
        if desfecho is None:
            return None
        if desfecho.pedido is not None:
            return desfecho.pedido
        return desfecho.recap.pedido if desfecho.recap is not None else None

    def _desfecho_do_recap(self, desfecho: Desfecho | None) -> None:
        """Atualiza a frase do recap pendente com o desfecho (sim, nao, correcao, prazo)."""
        numero = self._frase_do_recap
        if numero is None or desfecho is None:
            return
        pedido = self._pedido_do_desfecho(desfecho)
        self.frases_recentes.atualizar(numero, desfecho.estado, pedido.prompt if pedido is not None else None)
        if not self.confirmacao.a_espera:
            self._frase_do_recap = None

    # -- memoria: conversa recente e caderno de factos

    def _nomes_de_projeto(self) -> tuple[str, ...]:
        return tuple(projeto.nome for projeto in self.config.projetos)

    def _memoria(self, interpretacao: Interpretacao) -> Desfecho:
        """Um comando da memoria. Guardar ou apagar um facto passa pelo recap e pelo "sim"."""
        comando = interpretacao.detalhe
        if comando == "nova_conversa":
            quantas = self.historico.limpar()
            # A conversa nova tambem comeca sem as frases recentes do interprete.
            self.frases_recentes.limpar()
            self._frase_do_recap = None
            self._marcar_decisao(f"memoria: conversa recente esquecida ({quantas} troca(s))")
            self._dizer(self._texto("memoria_nova_conversa"))
            return Desfecho("executado", "memoria: nova conversa")
        if self.caderno is None:
            self._marcar_decisao("memoria: caderno indisponivel, nada feito")
            self._dizer(self._texto("memoria_sem_caderno"))
            return Desfecho("falhou", "memoria: caderno indisponivel")
        if comando == "listar":
            return self._listar_factos()
        if comando == "lembrar":
            facto = texto_do_facto(interpretacao.prompt)
            if not facto:
                self._marcar_decisao("memoria: lembrar sem facto, nada guardado")
                self._dizer(self._texto("memoria_sem_facto"))
                return Desfecho("nao_percebido", "memoria: lembrar sem facto")
            motivo = recusa_do_facto(facto, self._nomes_de_projeto())
            if motivo is not None:
                # Recusado antes do recap: o facto nunca e dito nem escrito.
                self._marcar_decisao(f"memoria: facto recusado ({motivo}), nada guardado")
                self._dizer(self._texto(f"memoria_recusado_{motivo}"))
                return Desfecho("recusado", f"memoria: facto com {motivo}")
            if facto in self.caderno.factos():
                self._marcar_decisao("memoria: facto ja guardado, nada mudou")
                self._dizer(self._texto("memoria_ja_sei"))
                return Desfecho("ignorado", "memoria: facto ja guardado")
            if not self.caderno.cabe(facto):
                self._marcar_decisao("memoria: caderno cheio, nada guardado")
                self._dizer(self._texto("memoria_cheio"))
                return Desfecho("recusado", "memoria: caderno cheio")
            return self.confirmacao.iniciar(
                Interpretacao(
                    interpretacao.texto, INTENCAO_LEMBRAR_FACTO, None, facto, "regra", "memoria: guardar um facto"
                )
            )
        if comando == "esquecer":
            alvo = facto_mais_parecido(interpretacao.prompt, self.caderno.factos())
            if alvo is None:
                self._marcar_decisao("memoria: nenhum facto parecido, nada apagado")
                self._dizer(self._texto("memoria_nada_parecido"))
                return Desfecho("ignorado", "memoria: nenhum facto parecido")
            return self.confirmacao.iniciar(
                Interpretacao(
                    interpretacao.texto, INTENCAO_ESQUECER_FACTO, None, alvo, "regra", "memoria: apagar um facto"
                )
            )
        return self.confirmacao.iniciar(interpretacao)

    def _listar_factos(self) -> Desfecho:
        """Le o caderno localmente: um resumo curto na voz e a lista inteira no ecra."""
        factos = self.caderno.factos() if self.caderno is not None else ()
        self._marcar_decisao(f"memoria: {len(factos)} facto(s) lidos localmente, nada enviado")
        for numero, facto in enumerate(factos, start=1):
            self.log.linha(f"ecra | {numero}. {facto}")
        if not factos:
            self._dizer(self._texto("memoria_vazia"))
        elif len(factos) == 1:
            self._dizer(self._texto("memoria_um", facto=factos[0]))
        else:
            self._dizer(self._texto("memoria_lista", n=str(len(factos))))
        return Desfecho("executado", "memoria: lista dos factos")

    def _gravar_facto(self, pedido: Pedido) -> object:
        """Grava ou apaga o facto confirmado com "sim". O caderno volta a verificar tudo."""
        if self.caderno is None:
            self._dizer(self._texto("memoria_sem_caderno"))
            return None
        try:
            if pedido.intencao == INTENCAO_LEMBRAR_FACTO:
                feito = self.caderno.acrescentar(pedido.prompt)
                self.log.linha("memoria | facto confirmado e guardado no caderno")
                self._dizer(self._texto("memoria_guardado"))
            else:
                feito = self.caderno.apagar(pedido.prompt)
                self.log.linha(
                    "memoria | facto confirmado e apagado do caderno" if feito else "memoria | o facto ja nao existia"
                )
                self._dizer(self._texto("memoria_apagado"))
        except FactoRecusado as erro:
            motivo = str(erro) if str(erro) in ("segredo", "financeiro") else "segredo"
            self.log.linha(f"memoria | facto recusado ao gravar ({erro}); nada guardado")
            self._dizer(self._texto(f"memoria_recusado_{motivo}"))
            return None
        except CadernoCheio as erro:
            self.log.linha(f"memoria | caderno cheio ({erro}); nada guardado")
            self._dizer(self._texto("memoria_cheio"))
            return None
        except OSError as erro:
            self.log.linha(f"memoria | ERRO a escrever o caderno ({erro.__class__.__name__}); nada mudou")
            self._dizer(self._texto("memoria_falhou"))
            return None
        return feito

    # -- saida para o ecra e para a voz

    def _instante_de_parede(self, instante: float) -> datetime.datetime:
        """Um instante do relogio da frase na hora local, como as linhas do log."""
        parede = getattr(self.log, "agora", None) or datetime.datetime.now
        return parede() - datetime.timedelta(seconds=max(0.0, self.relogio() - instante))

    def _marcar_decisao(self, detalhe: str) -> None:
        registo = getattr(self._local, "registo", None)
        if registo is not None and not registo.tem_etapa(4):
            registo.marcar(4, detalhe)

    def _mostrar(self, texto: str) -> None:
        """O ecra da confirmacao (recap, cancelado, ...): cada linha no log."""
        primeira = texto.splitlines()[0] if texto else ""
        self._marcar_decisao(f"ecra: {primeira}")
        for linha in texto.splitlines() or [""]:
            self.log.linha(f"ecra | {linha}")

    def _avisar_demora(self) -> None:
        """O modelo do interprete esta a carregar: "um momento", uma vez, na thread da frase."""
        self._dizer(self._texto("um_momento"), aviso=True, abre_seguimento=False)

    def _dizer(
        self,
        texto: str,
        *,
        aviso: bool = False,
        abre_seguimento: bool = True,
        consulta: Consulta | None = None,
    ) -> voz.ResultadoFala | None:
        """Fala (ou mostra) uma resposta do jarvis, e regista quando comecou a soar.

        Um `aviso` (o "um momento" enquanto o modelo carrega) so fica numa
        nota: nao e a decisao nem a resposta da frase, nem mexe nas etapas.
        Uma resposta que chega a tocar abre a janela de seguimento quando a
        voz acaba (`_assentar_escuta`), salvo com `abre_seguimento=False` ou
        num aviso.
        """
        falado = " ".join((texto or "").split())
        if not falado:
            return None
        if len(falado) > MAXIMO_ABSOLUTO_FALADO:  # ultima rede, nunca deve disparar
            falado = cortar_no_limite(falado, MAXIMO_ABSOLUTO_FALADO) or frase_de_recurso(
                "sem_corte_seguro", self.lingua
            )
        registo: RegistoDaFrase | None = getattr(self._local, "registo", None)
        medida: MedidaDaFrase | None = None if aviso else getattr(self._local, "medida", None)
        if not aviso:
            self._marcar_decisao("resposta sem accao")
        if medida is not None and medida.primeira_fala is None:
            medida.primeira_fala = falado

        def marcar(detalhe: str) -> None:
            if registo is not None and aviso:
                registo.nota(f"aviso de demora do interprete: {detalhe}")
            elif registo is not None:
                registo.marcar(5, detalhe)
            else:
                self.log.linha(f"voz | {detalhe}")

        if voz.esta_calado():
            marcar(f"silenciado a pedido: nada e falado | texto: {falado!r}")
            return None
        if self.estado.mudo or not self.com_voz:
            razao = "modo calado" if self.estado.mudo else "voz desligada"
            marcar(f"{razao}; resposta so no ecra: {falado!r}")
            return None
        if self._a_ouvir_o_recap or self._escuta_de is not None:
            # Nunca ouvir a propria voz: fecha sem som e reabre quando ela acaba.
            self._fechar_escuta()
        self.seguimento.fechar()
        self._continuacao = None
        silencios = self._silencios
        fala = _FalaAtual(falado, consulta)
        with self._tranca_da_voz:
            self.painel.mudar(A_FALAR)
            inicio_da_voz = self.relogio()
            with self._tranca_da_interrupcao:
                self._fala_atual = fala
            self._voz_a_falar(True)
            try:
                resultado = self._falar(falado)
            finally:
                self._voz_a_falar(False)
                with self._tranca_da_interrupcao:
                    self._fala_atual = None
                    self._voz_pausada_em = None
        if getattr(resultado, "falou", False):
            # Interrompida por um cala-te ou um clique na bolinha: nao continua.
            if abre_seguimento and not aviso and silencios == self._silencios:
                self._falou_resposta = True
        primeiro_audio = getattr(resultado, "primeiro_audio", None)
        if primeiro_audio is not None and registo is not None and registo.fim_da_fala is not None and not aviso:
            ms = (primeiro_audio - registo.fim_da_fala) * 1000
            if medida is not None and medida.primeira_fala_ms is None:
                medida.primeira_fala_ms = ms
                medida.tempos = tempos_da_resposta(
                    fim_da_escuta=registo.fim_da_fala,
                    texto_pronto=registo.texto_pronto,
                    primeiro_audio=primeiro_audio,
                    inicio_da_voz=inicio_da_voz,
                    ultima_voz=registo.ultima_voz,
                    interprete=registo.interprete,
                )
                registo.nota(medida.tempos.linha())
            registo.nota(f"inicio da resposta falada: {ms:.0f} ms desde o fim da fala")
            if registo.ultima_voz is not None:
                registo.nota(f"resposta falada desde a ultima voz: {(primeiro_audio - registo.ultima_voz) * 1000:.0f} ms")
        if getattr(resultado, "falou", False):
            marcar(f"falado: {falado!r}")
        elif fala.interrompida:
            marcar(f"interrompida por quem falou por cima; o texto fica no ecra: {falado!r}")
            self.log.linha(f"ecra | {falado}")
        else:
            motivo = getattr(resultado, "motivo_falha", "") or "sem motivo"
            marcar(f"voz falhou, resposta so no ecra ({motivo}) | texto: {falado!r}")
            self._erro_na_bolinha()
        return resultado

    # -- executor: so recebe pedidos sem efeito ou confirmados com "sim"

    def _executar(self, pedido: Pedido) -> object:
        intencao = pedido.intencao
        self._marcar_decisao(
            f"a executar '{intencao}'" + (f" no projeto {pedido.projeto}" if pedido.projeto else "")
        )
        if intencao == "calar":
            self.calar_agora("cala-te dito ao jarvis", definitivo=False)
            self.avisos.descartar("cala-te")
            self._fechar_conversa("cala-te")
            self.estado.mudo = True
            self._dizer(self._texto("calado"))
            return "calado"
        if intencao == "dormir":
            self._cancelar_pergunta("a dormir")
            self.avisos.descartar("a dormir")
            self._fechar_conversa("a dormir")
            self.estado.adormecido = True
            self.estado.aviso_de_sono_dado = False
            self._dizer(self._texto("dormir"))
            return "a dormir"
        if intencao == "acordar":
            self.estado.adormecido = False
            self.estado.mudo = False
            self._dizer(self._texto("acordado"))
            return "acordado"
        if intencao in acoes_locais.INTENCOES_LOCAIS:
            feito = self._executar_local(
                intencao, pedido.projeto, self.config, detalhe=pedido.detalhe, lingua=self.lingua
            )
            if feito.comando:
                self.log.linha(f"accao local | {feito.nome_acao} | comando: {feito.comando}")
            self._dizer(feito.texto)
            return feito
        if intencao in INTENCOES_DA_FORJA:
            if self.forja is None:
                self.log.linha(f"forja | indisponivel: '{intencao}' NAO foi feito")
                self._dizer(self._com_o_projeto_assumido(pedido, self._texto("sem_forja")))
                return None
            resposta = self.forja.executar(intencao, pedido.projeto, pedido.prompt, self.lingua)
            # O detalhe (e o relatorio inteiro) so no ecra; a voz diz a frase curta.
            for linha in resposta.ecra:
                self.log.linha(f"ecra | {linha}")
            self._dizer(self._com_o_projeto_assumido(pedido, resposta.falado))
            return resposta
        if intencao == INTENCAO_PERGUNTA_GERAL:
            return self._perguntar(pedido.prompt)
        if intencao in (INTENCAO_LEMBRAR_FACTO, INTENCAO_ESQUECER_FACTO):
            return self._gravar_facto(pedido)
        if intencao in INTENCOES_DO_CANAL and pedido.projeto:
            if self.canal is None:
                self.log.linha("canal | indisponivel: o prompt confirmado NAO foi enviado")
                self._dizer(self._texto("sem_canal"))
                return None
            ja_aberta = self.canal.enviar(pedido.projeto, pedido.prompt, self._ao_responder)
            if pedido.sem_recap:
                self.log.linha(
                    f"canal | resposta curta ao Claude entregue ao canal do {pedido.projeto} sem recap: {pedido.prompt!r}"
                )
            else:
                self.log.linha(f"canal | prompt confirmado entregue ao canal do {pedido.projeto}: {pedido.prompt!r}")
            enviado = "enviado_curto" if pedido.sem_recap else "enviado"
            self._dizer(self._texto(enviado if ja_aberta else "a_abrir", projeto=pedido.projeto))
            return pedido
        raise acoes_locais.AcaoError(f"'{intencao}' ainda nao se faz por voz")

    def _com_o_projeto_assumido(self, pedido: Pedido, falado: str) -> str:
        """Com o projeto assumido (nao dito), a resposta diz sempre de que projeto fala."""
        projeto = pedido.projeto
        if not pedido.projeto_assumido or not projeto or not falado:
            return falado
        if re.search(r"(?<!\w)" + re.escape(projeto) + r"(?!\w)", falado, re.IGNORECASE):
            return falado
        return self._texto("sobre_o_projeto", projeto=projeto, texto=falado)

    def _ao_responder(self, projeto: str, entrega) -> None:
        """Resposta (ou falha) de uma entrega, vinda da thread do canal."""
        texto = getattr(entrega, "texto", "") or ""
        erro = getattr(entrega, "erro", "") or ""
        caminho = getattr(entrega, "caminho", "?")
        if not erro:
            # O Stop que chega logo a seguir repetia o que ja se vai ouvir.
            self.avisos.houve_resposta(projeto)
        # A resposta inteira, com o rotulo de origem, fica so no ecra e no log
        # (pasta ignorada); a voz so diz o que passa o filtro da resposta falada.
        self.log.linha(
            f"canal | resposta do {projeto} (caminho={caminho}): {rotulo_da_origem(self.lingua)} {texto!r}"
            + (f" | erro: {erro}" if erro else "")
        )
        retomar = getattr(entrega, "comando_para_retomar", "")
        if retomar:
            self.log.linha(f"canal | para retomar essa sessao, na pasta do projeto: {retomar}")
        if erro:
            falar = (
                entrega.frase_para_o_utilizador()
                if self.lingua == "pt" and hasattr(entrega, "frase_para_o_utilizador")
                else self._texto("sem_resposta", projeto=projeto)
            )
        else:
            # O filtro corre aqui outra vez, na fronteira da voz, seja qual for o caminho.
            falar = resumo_falado(texto, lingua=self.lingua)
        if self.estado.adormecido or not falar:
            return
        with self._tranca:
            self._local.registo = None
            self._local.medida = None
            self._dizer(falar)
            if (
                not erro
                and conversa.pede_resposta(texto, falar)
                and not self.confirmacao.a_espera
                and not self.estado.adormecido
            ):
                self._abrir_conversa(projeto)
            else:
                self._assentar_escuta()
            self._mostrar_repouso()

    # -- perguntas gerais (Claude Code com pesquisa na web)

    def _perguntar(self, pergunta: str) -> Consulta | None:
        """Pergunta em segundo plano; a resposta e dita aos bocados por `_consultar`.

        So quando a primeira frase da resposta nao fica pronta em
        `espera_do_aviso_s` o jarvis diz antes um aviso curto e variado.
        """
        if self.perguntas is None:
            self.log.linha("pergunta | indisponivel: a pergunta NAO foi feita")
            self._dizer(self._texto("sem_perguntas"))
            return None
        termo = self.perguntas.recusar(pergunta)
        if termo is not None:
            self.log.linha(f"pergunta | recusada (pedido financeiro, '{termo}'): nada saiu do PC")
            self._dizer(self._texto("pergunta_recusada"))
            return None
        trocas = self.historico.trocas()
        factos = self.caderno.factos_para_contexto() if self.caderno is not None else ()
        consulta = self.perguntas.nova(pergunta, trocas=trocas, factos=factos)
        registo: RegistoDaFrase | None = getattr(self._local, "registo", None)
        referencia = None
        if registo is not None and registo.ultima_voz is not None:
            referencia = (registo.numero, registo.ultima_voz)
        pronta = threading.Event()
        fio = threading.Thread(
            target=self._consultar, args=(consulta, referencia, pronta), name="jarvis-pergunta", daemon=True
        )
        with self._tranca_da_pergunta:
            anterior, self._consulta = self._consulta, consulta
            self._fio_da_pergunta = fio
        if anterior is not None and anterior.cancelar():
            self.log.linha("pergunta | a anterior foi substituida por uma nova; a resposta dela nao se diz")
        self.log.linha(
            f"pergunta | ao Claude Code ({self.perguntas.config.modelo}, so pesquisa na web, "
            f"limite {self.perguntas.config.limite_s:g} s, memoria: {len(trocas)} troca(s) e "
            f"{len(factos)} facto(s)): {consulta.pergunta!r}"
        )
        fio.start()
        if not pronta.wait(self.espera_do_aviso_s) and not consulta.cancelada:
            # O aviso nao abre a janela: a resposta, quando acabar, abre.
            self.log.linha(f"pergunta | sem frase pronta em {self.espera_do_aviso_s:g} s: aviso curto")
            self._dizer(self._texto("a_verificar"), abre_seguimento=False)
        return consulta

    def _consultar(
        self,
        consulta: Consulta,
        referencia: tuple[int, float] | None = None,
        pronta: threading.Event | None = None,
    ) -> None:
        """Thread da pergunta: diz cada frase da resposta assim que passa o filtro.

        O processo e lido noutra thread (`ler`); esta so fala, e so enquanto a
        consulta for a mais recente. Entre duas partes da resposta, as frases
        por tratar passam a frente: um pedido novo cancela-a e nada mais se
        diz. A memoria so recebe a resposta dita, uma vez, depois de o stream
        acabar bem. `referencia` e (numero da frase, ultima voz dela): com ela
        o log diz quanto tempo passou da fala do Sponsor a primeira frase.
        """
        fluxo = ResumoEmFluxo(lingua=self.lingua)
        fila: queue.Queue = queue.Queue()
        houve_texto = threading.Event()

        def ao_texto(pedaco: str) -> None:
            houve_texto.set()
            novas = fluxo.acrescentar(pedaco)
            if not novas:
                return
            if pronta is not None and not pronta.is_set():
                if referencia is not None:
                    numero, ultima_voz = referencia
                    self.log.linha(
                        f"pergunta | primeira frase pronta: {(self.relogio() - ultima_voz) * 1000:.0f} ms "
                        f"desde a ultima voz da frase #{numero}"
                    )
                pronta.set()
            fila.put(("frases", novas))

        def ler() -> None:
            try:
                resultado = consulta.correr(ao_texto=ao_texto)
            except Exception as erro:  # noqa: BLE001 - uma pergunta falhada nunca para o jarvis
                self.log.linha(f"pergunta | ERRO: {erro!r}")
                resultado = None
            # A resposta inteira, com o rotulo de origem, fica so no ecra e no log
            # (pasta ignorada); a voz so diz o que passa o filtro da resposta falada.
            self.log.linha(
                f"pergunta | {resultado.estado if resultado is not None else 'falhou'} em "
                f"{resultado.duracao_s if resultado is not None else 0.0:.1f} s "
                f"({resultado.motivo if resultado is not None else 'erro'})"
                + (
                    f": {rotulo_da_origem(self.lingua)} {resultado.texto!r}"
                    if resultado is not None and resultado.respondida
                    else ""
                )
            )
            fila.put(("fim", resultado))
            if pronta is not None:
                pronta.set()

        threading.Thread(target=ler, name="jarvis-pergunta-leitura", daemon=True).start()
        fala = _FalaDaResposta()
        resultado = None
        acabou = False
        frases: list[str] = []

        def juntar(itens: list) -> bool:
            """Junta as frases prontas; True quando o stream ja acabou."""
            nonlocal resultado
            fim = False
            while True:
                for tipo, valor in itens:
                    if tipo == "frases":
                        frases.extend(valor)
                    else:
                        fim, resultado = True, valor
                try:
                    itens = [fila.get_nowait()]
                except queue.Empty:
                    return fim

        while not acabou:
            frases = []
            acabou = juntar([fila.get()])
            if acabou or not frases or fala.parada:
                continue
            # A vez pode demorar (o aviso, uma frase por tratar): o que chegou entretanto vai junto.
            self._esperar_a_vez(consulta)
            acabou = juntar([])
            if acabou:
                continue
            if not self._dizer_parte_da_resposta(consulta, " ".join(frases), fala, referencia):
                fluxo.parar()
        estado = resultado.estado if resultado is not None else "falhou"
        if estado == "cancelada":
            return
        if estado == "respondida" and not fala.parada:
            if not houve_texto.is_set():
                # Sem pedacos de texto no stream: a resposta inteira da linha final.
                frases.extend(fluxo.acrescentar(resultado.texto))
            frases.extend(fluxo.acabar())
        self._acabar_a_resposta(consulta, estado, " ".join(frases), fluxo, fala, referencia)

    def _esperar_a_vez(self, consulta: Consulta) -> None:
        """Frases por tratar (a da propria pergunta, ou uma nova) passam a frente da resposta."""
        with self._condicao:
            while self._pendentes > 0 and not consulta.cancelada:
                self._condicao.wait(PASSO_DA_ESPERA_DA_VEZ_S)

    def _vigente(self, consulta: Consulta, *, acabar: bool = False) -> bool:
        """A consulta ainda e a mais recente e nao foi cancelada; com `acabar` deixa de ser a em curso."""
        with self._tranca_da_pergunta:
            vigente = self._consulta is consulta and not consulta.cancelada
            if vigente and acabar:
                self._consulta = None
        return vigente

    def _dizer_parte_da_resposta(
        self, consulta: Consulta, texto: str, fala: "_FalaDaResposta", referencia: tuple[int, float] | None
    ) -> bool:
        """Diz uma parte da resposta ainda a chegar; False quando nada mais e para dizer."""
        self._esperar_a_vez(consulta)
        with self._tranca:
            if not self._vigente(consulta):
                fala.parar()
                self.log.linha("pergunta | resto da resposta descartado: ja houve silencio ou um pedido novo")
                return False
            if self.estado.adormecido:
                fala.parar()
                return False
            return self._dizer_da_resposta(texto, fala, referencia, consulta)

    def _dizer_da_resposta(
        self,
        texto: str,
        fala: "_FalaDaResposta",
        referencia: tuple[int, float] | None,
        consulta: Consulta | None = None,
    ) -> bool:
        """Diz `texto` como parte da resposta (com a tranca); False se foi mandado calar a meio."""
        self._local.registo = None
        self._local.medida = None
        self._continuacao = None
        if fala.silencios is None:
            fala.silencios = self._silencios
        dito = self._dizer(texto, consulta=consulta)
        primeiro_audio = getattr(dito, "primeiro_audio", None)
        if referencia is not None and primeiro_audio is not None and not fala.soou:
            numero, ultima_voz = referencia
            self.log.linha(
                f"pergunta | resposta falada: {(primeiro_audio - ultima_voz) * 1000:.0f} ms "
                f"desde a ultima voz da frase #{numero}"
            )
        if primeiro_audio is not None:
            fala.soou = True
        fala.partes += 1
        fala.inteira = fala.inteira and bool(getattr(dito, "falou", False))
        if voz.esta_calado() or fala.silencios != self._silencios:
            fala.parar()
            return False
        return True

    def _acabar_a_resposta(
        self,
        consulta: Consulta,
        estado: str,
        resto: str,
        fluxo: ResumoEmFluxo,
        fala: "_FalaDaResposta",
        referencia: tuple[int, float] | None,
    ) -> None:
        """O stream acabou: diz o que falta (ou uma frase curta de falha) e abre a escuta."""
        self._esperar_a_vez(consulta)
        with self._tranca:
            if not self._vigente(consulta, acabar=True):
                if not fala.parada:
                    self.log.linha("pergunta | resposta descartada: ja houve silencio ou um pedido novo")
                return
            if self.estado.adormecido or fala.parada:
                return
            if estado == "respondida":
                falar = resto
            elif estado == "recusada":
                falar = self._texto("pergunta_recusada")
            elif fala.partes:
                falar = self._texto("pergunta_falhou_a_meio")
            else:
                falar = self._texto("pergunta_falhou")
            completa = True
            if falar:
                completa = self._dizer_da_resposta(falar, fala, referencia, consulta)
            if estado == "respondida" and completa:
                self._lembrar_troca(consulta.pergunta, fluxo.falado)
            self._assentar_escuta()
            # So uma resposta dita inteira, sem um silencio pelo meio, abre a continuacao.
            if estado == "respondida" and completa and fala.inteira and fala.partes:
                self._abrir_continuacao(fluxo.falado)
            self._mostrar_repouso()

    def _lembrar_troca(self, pergunta: str, falado: str) -> None:
        """Guarda no historico a pergunta respondida e a resposta exatamente como foi dita.

        Uma frase de recurso (resposta vazia ou so tecnica) nao e uma resposta: nao entra.
        """
        recursos = {frase for frases in FRASES_DE_RECURSO.values() for frase in frases.values()}
        if not falado or falado in recursos:
            return
        self.historico.acrescentar(pergunta, falado)

    def _cancelar_pergunta(self, motivo: str) -> None:
        """A pergunta em curso deixa de ser dita e o processo dela e morto."""
        with self._tranca_da_pergunta:
            consulta, self._consulta = self._consulta, None
        if consulta is not None and consulta.cancelar():
            self.log.linha(f"pergunta | cancelada ({motivo}); a resposta nao se diz")

    def _largar_pergunta(self, consulta: Consulta) -> None:
        """A resposta foi interrompida: deixa de se dizer, mas continua a chegar para o ecra."""
        with self._tranca_da_pergunta:
            if self._consulta is not consulta:
                return
            self._consulta = None
            anterior, self._consulta_largada = self._consulta_largada, consulta
        if anterior is not None and anterior.cancelar():
            self.log.linha("pergunta | uma resposta interrompida antes desta foi cancelada")
        self.log.linha("pergunta | resposta interrompida: nao se diz mais; aparece inteira no ecra quando acabar")

    def _cancelar_a_largada(self, motivo: str) -> None:
        with self._tranca_da_pergunta:
            consulta, self._consulta_largada = self._consulta_largada, None
        if consulta is not None and consulta.cancelar():
            self.log.linha(f"pergunta | a resposta interrompida foi cancelada ({motivo})")

    def esperar_pergunta(self, limite_s: float) -> bool:
        """Espera que a thread da ultima pergunta acabe (para os testes e o modo ficheiro)."""
        with self._tranca_da_pergunta:
            fio = self._fio_da_pergunta
        if fio is None:
            return True
        fio.join(limite_s)
        return not fio.is_alive()

    # -- prazo da confirmacao e silencio

    def verificar_tempo(self) -> None:
        """Cancela um recap sem resposta dentro do prazo. Chamado pelo ciclo principal."""
        # Antes da tranca: quem esta em pausa e a propria fala que a tem.
        self._vigiar_a_pausa()
        if not self._tranca.acquire(blocking=False):
            return
        try:
            self._local.registo = None
            self._local.medida = None
            # Uma resposta a meio de ser dita ou transcrita ainda conta: o prazo espera por ela.
            desfecho = None if self._alguem_a_responder() else self.confirmacao.verificar_tempo()
            self._desfecho_do_recap(desfecho)
            if desfecho is not None:
                self.log.linha(f"confirmacao | {desfecho.estado}: {desfecho.motivo}")
                # O aviso de que cancelou nao abre a janela de seguimento.
                self._assentar_escuta(seguimento=False)
                self._mostrar_repouso()
            else:
                self._reabrir_escuta_do_recap()
            fechada = self.janela.fechar_se_expirou(alguem_a_falar=self._alguem_a_falar())
            if fechada is not None:
                self.log.linha(
                    f"conversa | janela do {fechada.projeto} fechada: {self.janela.limite_s:.0f} s sem resposta; "
                    "nada enviado"
                )
                self._terminar_escuta("conversa sem resposta")
                self._mostrar_repouso()
            if self.seguimento.fechar_se_expirou(alguem_a_falar=self._alguem_a_responder()) is not None:
                self._terminar_escuta(f"{self.seguimento.limite_s:.0f} s sem pedido")
                self._mostrar_repouso()
            elif self.seguimento.aberta() and not self._alguem_a_responder():
                self._reabrir_seguimento()
        finally:
            self._tranca.release()

    def calar_agora(
        self, motivo: str, *, definitivo: bool, ja_calado: bool = False
    ) -> voz.ResultadoSilencio:
        """Cala a voz JA pelo mecanismo unico de `jarvis.voz` e regista as duas linhas."""
        self._cancelar_pergunta(motivo)
        return self._calar(motivo, definitivo=definitivo, registar=None if ja_calado else self.log.linha)


# --- Arranque ------------------------------------------------------------------------


def carregar_config_tolerante(caminho: Path, log) -> Config:
    """A config privada, ou uma config vazia com o aviso escrito no log.

    Sem `config.toml` o jarvis continua a servir o que nao precisa de projeto
    (horas, data, calar, dormir, acordar); nenhum projeto e adivinhado. A
    descoberta tambem fica desligada: a config que nao se leu podia ter
    `[descoberta] pastas = []`.
    """
    try:
        return carregar_config(caminho)
    except ConfigError as erro:
        log.linha(
            f"AVISO: configuracao privada por carregar ({erro}). "
            "O jarvis segue SEM projetos (nem descobertos)."
        )
        return Config(microfone="", projetos=(), descoberta=ConfigDescoberta(pastas=()))


def construir_ouvido(
    jarvis: Jarvis,
    *,
    com_som: bool = False,
    com_ativacao: bool = True,
    wavs: list[Path] | None = None,
    motor=None,
    fonte=None,
    tecla=None,
    detetor=None,
    vad=None,
    ritmo_real: bool = False,
    vad_da_interrupcao=None,
    fim_de_turno=None,
) -> Ouvido:
    """O ouvido residente ligado ao `Jarvis`. As pecas entram so nos testes.

    O fim de turno do config ([ouvido].fim_de_turno) so e criado com o VAD
    real criado aqui; com um VAD dado (os testes) fica o silencio fixo, a nao
    ser que venha tambem um `fim_de_turno`.

    Com `wavs`, os ficheiros fazem de microfone e de tecla (sem maos-livres),
    um de cada vez: o seguinte entra quando a frase anterior esta tratada.
    """
    log = jarvis.log
    config_ouvido = jarvis.config.ouvido
    nome_da_tecla = config_ouvido.tecla
    if wavs:
        pcms = [pcm_do_wav(caminho) for caminho in wavs]
        fonte = FonteDeSequencia(
            pcms,
            pronto=lambda n: jarvis.frases >= n and jarvis.ocioso(),
            ritmo_real=ritmo_real,
            descricao=f"{len(pcms)} ficheiro(s) WAV (o microfone NAO e usado)",
            avisar=log.linha,
        )
        tecla = TeclaDoFicheiro(fonte)
        nome_da_tecla = "tecla simulada pelo ficheiro"
        com_ativacao = False
    if motor is None:
        adaptacao = criar_adaptacao(jarvis.config, avisar=log.linha)
        motor = criar_motor(config_ouvido.motor, config_ouvido.device, adaptacao=adaptacao)
    if fonte is None:
        fonte = MicrofonePyAudio(jarvis.config.microfone, avisar=log.linha)
    if tecla is None:
        tecla = TeclaWindows(config_ouvido.codigo_da_tecla)
    vad_dado = vad is not None
    if com_ativacao and detetor is None:
        try:
            detetor, vad = DetetorOpenWakeWord(modelo_de_ativacao(config_ouvido.lingua)), vad or VadWebRtc()
        except (FileNotFoundError, ImportError) as erro:
            log.linha(f"AVISO: maos-livres desligadas ({erro}); fica so a tecla de falar")
            detetor = vad = None
    if not com_ativacao:
        detetor = vad = None
    vad_da_interrupcao = _vad_da_interrupcao(jarvis, vad, vad_da_interrupcao, modo_ficheiro=bool(wavs))
    if fim_de_turno is None and vad is not None and not vad_dado:
        fim_de_turno = criar_fim_de_turno(
            config_ouvido.fim_de_turno, config_ouvido.fim_de_turno_maximo_s, escrever=log.linha
        )
    ouvido = Ouvido(
        fonte,
        motor,
        jarvis.ao_ouvir,
        tecla=tecla,
        detetor=detetor,
        vad=vad,
        lingua=config_ouvido.lingua,
        limiar_de_ativacao=config_ouvido.limiar_ativacao,
        escrever=log.linha,
        com_som=com_som,
        nome_da_tecla=nome_da_tecla,
        ao_ativar_sem_fala=jarvis.ao_ativar_sem_fala,
        ao_evento=jarvis.ao_evento_do_ouvido,
        ao_chunk=jarvis.ao_audio_do_microfone,
        vad_da_interrupcao=vad_da_interrupcao,
        ao_interromper=jarvis.ao_interromper if vad_da_interrupcao is not None else None,
        fim_de_turno=fim_de_turno,
    )
    jarvis.ouvido = ouvido
    return ouvido


def _vad_da_interrupcao(jarvis: Jarvis, vad, vad_da_interrupcao, *, modo_ficheiro: bool):
    """O VAD Silero que ouve por cima da voz, ou None (com o motivo no log)."""
    motivo = None
    if not jarvis.interromper:
        motivo = "sem voz" if not jarvis.com_voz else "[escuta] interromper = false"
    elif modo_ficheiro:
        motivo = "modo ficheiro, sem microfone"
    elif vad is None:
        motivo = "sem maos-livres nao ha VAD para o fim da frase"
    elif vad_da_interrupcao is None:
        try:
            from jarvis.vad_silero import VadSilero

            vad_da_interrupcao = VadSilero()
        except Exception as erro:  # noqa: BLE001 - sem Silero o jarvis so nao se deixa interromper
            motivo = f"Silero VAD por carregar ({erro})"
    if motivo is not None:
        jarvis.interromper = False
        jarvis.log.linha(f"interromper | desligado: {motivo}")
        return None
    jarvis.log.linha("interromper | ligado: falar por cima do jarvis pausa a voz (VAD Silero)")
    return vad_da_interrupcao


@dataclass
class Arranque:
    """O que o arranque mediu: tempos de cada peca, VRAM e o total."""

    total_s: float
    pecas_ms: dict[str, float]
    erros: dict[str, str]
    resultados: dict[str, object] = field(default_factory=dict)
    vram_antes: object = None
    vram_depois: object = None

    @property
    def dentro_da_meta(self) -> bool:
        return self.total_s <= LIMITE_DO_ARRANQUE_S


def aquecer_em_paralelo(
    tarefas: dict[str, Callable[[], object]],
    log,
    *,
    medir: Callable[[], object] = medir_vram,
    relogio: Callable[[], float] = time.perf_counter,
    inicio: float | None = None,
) -> Arranque:
    """Corre as tarefas de aquecimento ao mesmo tempo e escreve o que cada uma levou."""
    inicio = relogio() if inicio is None else inicio
    vram_antes = medir()
    log.linha(f"arranque | VRAM antes: {vram_antes if vram_antes is not None else 'sem nvidia-smi'}")
    pecas_ms: dict[str, float] = {}
    erros: dict[str, str] = {}
    resultados: dict[str, object] = {}

    def correr(nome: str, tarefa: Callable[[], object]) -> None:
        antes = relogio()
        try:
            resultados[nome] = tarefa()
        except Exception as erro:  # noqa: BLE001 - cada peca diz o seu erro
            erros[nome] = f"{type(erro).__name__}: {erro}"
        pecas_ms[nome] = (relogio() - antes) * 1000

    fios = [threading.Thread(target=correr, args=item, name=f"aquecer-{item[0]}", daemon=True) for item in tarefas.items()]
    for fio in fios:
        fio.start()
    for fio in fios:
        fio.join(timeout=LIMITE_DO_ARRANQUE_S * 4)
    for nome in tarefas:
        if nome not in pecas_ms:
            erros[nome] = "nao acabou de aquecer"
            continue
        resultado = resultados.get(nome)
        detalhe = erros.get(nome) or str(getattr(resultado, "motivo", None) or resultado or "pronto")
        log.linha(f"arranque | {nome}: {pecas_ms[nome]:.0f} ms | {detalhe}")
    vram_depois = medir()
    log.linha(f"arranque | VRAM depois: {vram_depois if vram_depois is not None else 'sem nvidia-smi'}")
    return Arranque(relogio() - inicio, pecas_ms, erros, dict(resultados), vram_antes, vram_depois)


def arrancar(
    jarvis: Jarvis,
    ouvido: Ouvido,
    *,
    com_voz: bool,
    inicio: float | None = None,
    medir: Callable[[], object] = medir_vram,
) -> Arranque:
    """Aquece transcricao, voz e interprete em paralelo e diz quanto levou."""
    tarefas: dict[str, Callable[[], object]] = {
        "transcricao": lambda: f"{ouvido.motor.descrever()} pronto em {ouvido.preparar():.0f} ms",
        "interprete": lambda: jarvis.interprete.aquecer(medir=medir),
    }
    if com_voz:
        tarefas["voz"] = voz.aquecer
    arranque = aquecer_em_paralelo(tarefas, jarvis.log, medir=medir, inicio=inicio)
    if "voz" in arranque.erros:
        jarvis.log.linha("AVISO: voz por carregar; as respostas saem so no ecra")
        jarvis.com_voz = False
    escolha = arranque.resultados.get("interprete")
    if "interprete" in arranque.erros or getattr(escolha, "modelo", None) is None:
        jarvis.log.linha("AVISO: interprete sem LLM; so os comandos da lista branca sao percebidos")
    return arranque


#: As respostas ao recap no cabecalho, com as palavras a dizer na lingua do ouvido.
_RESPOSTAS_AO_RECAP = {
    "pt": '"sim" envia, "aborta" cancela ("cancela" tambem), "nao, muda X para Y" ou "acrescenta ..." corrigem',
    "en": '"yes" envia, "abort" cancela ("cancel" tambem), "no, change X to Y" ou "add ..." corrigem',
}


def _cabecalho(jarvis: Jarvis, ouvido: Ouvido, arranque: Arranque) -> None:
    log = jarvis.log
    config_ouvido = jarvis.config.ouvido
    log.bruto("")
    log.bruto("=" * LARGURA_DA_SEPARACAO)
    log.bruto(
        f"   JARVIS PRONTO em {arranque.total_s:.1f} s (meta <= {LIMITE_DO_ARRANQUE_S:.0f} s)"
        + ("" if arranque.dentro_da_meta else "  -- ACIMA DA META")
    )
    log.bruto(f"   ouvido: {ouvido.fonte.descricao}")
    log.bruto(f"   segura '{ouvido.nome_da_tecla}' para falar, solta para acabar")
    if ouvido.detetor is not None:
        log.bruto(f'   maos-livres: diz "{palavra_de_ativacao(config_ouvido.lingua)}" e a frase')
    else:
        log.bruto("   maos-livres desligadas: so a tecla de falar")
    log.bruto(f"   depois do recap: {_RESPOSTAS_AO_RECAP.get(config_ouvido.lingua, _RESPOSTAS_AO_RECAP['pt'])}")
    prazo = f"{jarvis.confirmacao.limite_s:g} s"
    if getattr(ouvido, "vad", None) is not None:
        log.bruto(f"   a resposta ao recap diz-se logo, sem palavra de ativacao, dentro de {prazo}")
    else:
        log.bruto(f"   a resposta ao recap diz-se com a tecla de falar, dentro de {prazo}")
    log.bruto('   pergunta do Claude: 8 s para responder sem palavra de ativacao; "sai da conversa" fecha')
    seguimento = f"{jarvis.config.escuta.seguimento_s:g} s"
    log.bruto(
        f"   depois de o jarvis falar: {seguimento} para continuar sem palavra de ativacao;"
        f" o som e a bolinha ({A_OUVIR_TE}) dizem quando esta a ouvir"
    )
    fecho = '"e tudo"' if config_ouvido.lingua == "pt" else "\"that's all\""
    log.bruto(f"   essa escuta recomeca depois de cada resposta; {seguimento} de silencio ou {fecho} fecham-na")
    if getattr(ouvido, "interromper_ligado", False):
        log.bruto("   enquanto o jarvis fala: fala por cima e ele pausa; yeah/uh-huh deixam-no continuar")
    log.bruto("   Ctrl+C ou fechar esta janela cala a voz e desliga o microfone")
    log.bruto("=" * LARGURA_DA_SEPARACAO)


def correr(
    jarvis: Jarvis,
    ouvido: Ouvido,
    *,
    com_voz: bool = True,
    inicio: float | None = None,
    medir: Callable[[], object] = medir_vram,
) -> int:
    """Arranca, fica a ouvir ate Ctrl+C (ou ate os ficheiros acabarem) e fecha."""
    log = jarvis.log
    try:
        arranque = arrancar(jarvis, ouvido, com_voz=com_voz, inicio=inicio, medir=medir)
        jarvis.arranque = arranque
        if "transcricao" in arranque.erros:
            log.linha(f"ERRO: transcricao por carregar ({arranque.erros['transcricao']})")
            return 2
        _cabecalho(jarvis, ouvido, arranque)
        jarvis.iniciar()
        ouvido.iniciar()
    except (MotorIndisponivel, OSError) as erro:
        log.linha(f"ERRO: {erro}")
        jarvis.fechar()
        return 2
    for tentativa in getattr(ouvido.fonte, "tentativas", []):
        log.linha(f"ouvido: posto de parte {tentativa}")
    try:
        while ouvido.a_correr():
            ouvido.esperar(0.2)
            jarvis.verificar_tempo()
        # Fonte acabada (modo ficheiro): as frases ja entregues ainda acabam.
        jarvis.esperar_ocioso(ESPERA_ENTRE_FICHEIROS_S)
        jarvis.esperar_pergunta(ESPERA_ENTRE_FICHEIROS_S)
    except KeyboardInterrupt:
        log.bruto("")
        # Calar primeiro; so depois desligar o microfone.
        jarvis.calar_agora("Ctrl+C", definitivo=True, ja_calado=voz.esta_calado())
        log.linha("Ctrl+C: voz calada, a desligar o microfone e a fechar o jarvis")
        return 0
    finally:
        ouvido.parar()
        ouvido.esperar(3.0)
        jarvis.fechar()
    return 0


def construir_canal(
    config: Config, log, ao_evento: Callable[[str, str, str], object] | None = None
) -> CanalParaSessoes | None:
    """O canal real para as sessoes, ou None (com o motivo no log) se nao arrancar.

    `ao_evento` recebe os avisos dos hooks das sessoes abertas pelo jarvis.
    """
    if not config.projetos:
        log.linha("canal | sem projetos na configuracao: os ditados nao tem para onde ir")
        return None
    try:
        return CanalDasSessoes(config, log.linha, ao_evento=ao_evento).iniciar()
    except Exception as erro:  # noqa: BLE001 - sem canal, o resto do jarvis funciona
        log.linha(f"AVISO: canal para o Claude Code por arrancar ({erro}); os ditados nao sao enviados")
        return None


#: Com esta variavel de ambiente preenchida, a bolinha nunca abre (os testes usam-na).
VARIAVEL_SEM_BOLINHA = "JARVIS_SEM_BOLINHA"


def abrir_bolinha(jarvis: Jarvis, *, ligacao: LigacaoABolinha | None = None) -> LigacaoABolinha | None:
    """Lanca a janela da bolinha e liga-a ao jarvis; sem ela, o jarvis segue igual."""
    ligacao = ligacao or LigacaoABolinha(jarvis.calar_pela_bolinha, jarvis.log.linha)
    if not ligacao.iniciar():
        return None
    jarvis.ligar_bolinha(PonteDaBolinha(ligacao.enviar, ligacao.enviar_nivel))
    return ligacao


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis",
        description="jarvis residente: tecla de falar ou palavra de ativacao, recap, confirmacao e resposta falada.",
    )
    parser.add_argument(
        "--config", default=None, metavar="FICHEIRO", help="configuracao privada (por omissao: config.toml na raiz)"
    )
    parser.add_argument(
        "--wav",
        nargs="+",
        metavar="FICHEIRO",
        help="um ou mais WAV em vez do microfone, um de cada vez, como frases ditas com a tecla",
    )
    parser.add_argument("--sem-voz", action="store_true", help="respostas so na consola e no log")
    parser.add_argument("--sem-ativacao", action="store_true", help="desliga as maos-livres: so a tecla de falar")
    parser.add_argument(
        "--sem-bolinha", action="store_true", help="sem a janela da bolinha de estado (com --wav nunca abre)"
    )
    parser.add_argument(
        "--com-som",
        action="store_true",
        help="bip curto no inicio e no fim da escuta; com --wav, e a unica forma de a voz tocar",
    )
    # A tecla de falar passou a ser o caminho normal; a flag antiga continua aceite.
    parser.add_argument("--ptt", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--autoteste", action="store_true", help="regressao das partes puras (sem hardware)")
    return parser


def main(argv: list[str] | None = None) -> int:
    inicio = time.perf_counter()
    forcar_consola_utf8()
    args = construir_parser().parse_args(argv)
    if args.autoteste:
        return _autoteste()

    caminho_da_config = Path(args.config) if args.config else CAMINHO_CONFIG_PADRAO
    modo_ficheiro = bool(args.wav)
    if modo_ficheiro:
        return _arrancar_e_correr(args, inicio, caminho_da_config)
    # So o microfone tira a tranca: antes de carregar modelos ou abrir o microfone.
    tranca = TrancaDaInstancia(PASTA_LOGS / NOME_DA_TRANCA)
    try:
        substituida = tranca.adquirir()
    except OutraInstanciaAberta as erro:
        print(texto_para_a_consola(mensagem_de_recusa(erro.pid, _lingua_da_config(caminho_da_config))), flush=True)
        return CODIGO_OUTRA_INSTANCIA
    except OSError as erro:
        print(texto_para_a_consola(f"ERRO: tranca da instancia por criar em {tranca.caminho} ({erro})"), flush=True)
        return 2
    try:
        return _arrancar_e_correr(args, inicio, caminho_da_config, tranca_substituida=substituida)
    finally:
        tranca.libertar()


def _lingua_da_config(caminho: Path) -> str:
    """A lingua configurada, so para a mensagem de recusa (sem log nem modelos)."""
    try:
        lingua = carregar_config(caminho).ouvido.lingua
    except Exception:  # noqa: BLE001 - na duvida, a lingua por omissao
        lingua = Config(microfone="", projetos=()).ouvido.lingua
    return "en" if lingua == "en" else "pt"


def _arrancar_e_correr(
    args: argparse.Namespace,
    inicio: float,
    caminho_da_config: Path,
    *,
    tranca_substituida: DonoDaTranca | None = None,
) -> int:
    log = LogDaSessao()
    log.linha(f"jarvis a arrancar | pid {os.getpid()} | log em {log.caminho}")
    if tranca_substituida is not None:
        log.linha(
            f"tranca da instancia: a anterior (pid {tranca_substituida.pid or '?'}) era de um jarvis "
            "que ja nao esta a correr; substituida por esta"
        )
    config = com_projetos_descobertos(carregar_config_tolerante(caminho_da_config, log), registar=log.linha)
    lingua = "en" if config.ouvido.lingua == "en" else "pt"
    voz.definir_lingua_da_voz(lingua)
    voz.definir_voz_inglesa(config.voz.nome)
    voz.definir_velocidade(config.voz.velocidade)
    modo_ficheiro = bool(args.wav)
    com_voz = not args.sem_voz and (not modo_ficheiro or args.com_som)
    log.linha(
        f"configuracao: {len(config.projetos)} projeto(s) | lingua={lingua} | voz inglesa={config.voz.nome} "
        f"(velocidade {voz.velocidade_da_voz(config.voz.nome):g}) "
        f"| frases geradas={'ligadas' if config.voz.frases_geradas else 'desligadas'} "
        f"| motor={config.ouvido.motor} "
        f"({config.ouvido.device}) | voz={'ligada' if com_voz else 'desligada'} "
        f"| forja={'configurada' if config.forja else 'sem [forja] no config.toml: so as sessoes'}"
    )
    interprete = Interprete(config)
    persona = (
        Persona(
            ClienteOllamaEmFluxo(config.interprete.url),
            lambda: interprete.modelo,
            lingua,
            projetos=lambda: [projeto.nome for projeto in config.projetos],
            registar=log.linha,
        )
        if config.voz.frases_geradas
        else None
    )
    forja = ForjaPorVoz(config)
    perguntas = PerguntasGerais(
        config.perguntas,
        lingua,
        pastas_proibidas=[RAIZ, *(projeto.caminho for projeto in config.projetos)],
        nomes_de_projeto=[projeto.nome for projeto in config.projetos],
    )
    caderno = CadernoDeFactos.da_config(
        config.memoria, nomes_de_projeto=[projeto.nome for projeto in config.projetos], registar=log.linha
    )
    jarvis = Jarvis(
        config,
        log,
        interprete=interprete,
        forja=forja,
        perguntas=perguntas,
        com_voz=com_voz,
        painel=Painel(log.linha, titulo=not modo_ficheiro),
        sons=construir_sons(config, modo_ficheiro=modo_ficheiro, com_som=args.com_som),
        caderno=caderno,
        persona=persona,
    )
    jarvis.canal = construir_canal(config, log, ao_evento=jarvis.avisos.receber)
    sem_bolinha = modo_ficheiro or args.sem_bolinha or bool(os.environ.get(VARIAVEL_SEM_BOLINHA))
    ligacao = None if sem_bolinha else abrir_bolinha(jarvis)
    if config.forja is not None and config.projetos and not modo_ficheiro:
        jarvis.vigia = VigiaDosRuns(
            config.projetos, forja.estado.ler_run, jarvis.avisos.receber_run, escrever=log.linha
        )
    # Ultima rede da saida do processo: a voz nunca fica a falar sozinha.
    atexit.register(voz.calar_agora, "saida do processo (atexit)", definitivo=True)

    def ao_ctrl_c(_numero, _quadro):
        """Cala ANTES de a excecao desenrolar a pilha (o instante mais cedo possivel)."""
        jarvis.calar_agora("Ctrl+C", definitivo=True)
        raise KeyboardInterrupt

    try:
        handler_anterior = signal.signal(signal.SIGINT, ao_ctrl_c)
    except ValueError:
        handler_anterior = None  # fora da thread principal nao ha handlers de sinal
    try:
        try:
            ouvido = construir_ouvido(
                jarvis,
                com_som=args.com_som and not modo_ficheiro,
                com_ativacao=not args.sem_ativacao,
                wavs=[Path(caminho) for caminho in args.wav] if modo_ficheiro else None,
            )
        except (ValueError, OSError) as erro:
            log.linha(f"ERRO: {erro}")
            jarvis.fechar()
            return 1
        return correr(jarvis, ouvido, com_voz=com_voz, inicio=inicio)
    finally:
        if handler_anterior is not None:
            signal.signal(signal.SIGINT, handler_anterior)
        jarvis.calar_agora("saida do processo", definitivo=True, ja_calado=voz.esta_calado())
        if ligacao is not None:
            jarvis.desligar_bolinha()
            ligacao.fechar()
        log.linha("jarvis terminado")
        log.fechar()


# --- Autoteste das partes puras (sem microfone, sem Ollama, sem som) ----------


def _autoteste() -> int:
    """Formato do log, aritmetica das latencias e a fonte de varios ficheiros."""
    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    verificar(
        "formato da etapa",
        formatar_etapa(3, 2, 812.4, "texto: 'que horas sao'"),
        "frase #3 | etapa 2/5 transcricao           |     812 ms | texto: 'que horas sao'",
    )
    verificar(
        "timestamp com milissegundos",
        agora_iso(datetime.datetime(2026, 9, 20, 6, 12, 1, 123456)),
        "2026-09-20 06:12:01.123",
    )
    verificar(
        "nome do ficheiro de log",
        caminho_do_log(datetime.datetime(2026, 9, 20), Path("logs")).name,
        "jarvis-2026-09-20.log",
    )
    tempos = iter([0.0, 1.0, 1.5])
    registo = RegistoDaFrase(numero=1, relogio=lambda: next(tempos))
    verificar("latencia da primeira etapa", round(registo.marcar(1, "x").latencia_ms), 1000)
    verificar("latencia da segunda etapa", round(registo.marcar(2, "y").latencia_ms), 500)

    fonte = FonteDeSequencia([b"\x01\x00" * 480, b"\x02\x00" * 480], silencio_depois_s=0.03, dormir=lambda _s: None)
    fonte.abrir()
    lidos = []
    while (chunk := fonte.ler()) is not None:
        lidos.append(fonte.dentro_do_audio)
    verificar("dois ficheiros com silencio depois de cada um", lidos, [True, False, False, True, False])

    if falhas:
        print("\nFALHAS:")
        for falha in falhas:
            print(f"  - {falha}")
        return 1
    print("\nOK: autoteste das partes puras do jarvis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
