# Amostra fixa de 20 frases pt-PT — medicao do portugues europeu (D7/D34)

Ficheiro versionado (D1/D10): nenhum caminho nem nome de projeto REAL do
Sponsor entra aqui. Os nomes de projeto aparecem so como os marcadores
`<projeto-1>` e `<projeto-2>`, que quem correr a medicao substitui a partir da
CONFIGURACAO local (`config.toml`, fora do Git) ou, na falta dela, a partir do
exemplo versionado (`config.exemplo.toml`, projetos ficticios "exemplo-um" e
"exemplo-dois"). Nada abaixo assume um nome real.

## Protocolo completo da D7 (para o QA de fecho, com a voz do Sponsor)

Esta e a AMOSTRA fixa; o protocolo abaixo e o que a D7 exige quando alguem a
correr a serio, com o Sponsor a falar ao microfone (nao esta a acontecer neste
run — ver o aviso da D34 mais abaixo):

1. **Amostra**: as 20 frases fixas deste ficheiro (10 de comandos locais da
   lista branca da D4, 10 destinadas ao Claude Code), cada uma dita **3 vezes**
   pelo Sponsor, nas condicoes normais dele (voz normal, a secretaria, ruido
   domestico de fundo) = 60 gravacoes. As gravacoes ficam em pastas ja
   ignoradas pelo Git (`audio/`, `recordings/`); so as transcricoes e as
   metricas entram no repositorio, e mesmo essas so fora de ficheiros
   versionados (a evidencia de uma corrida real vive em `docs/forja/evidence/`,
   que tambem esta fora do Git).
2. **Metricas**, por esta ordem de importancia:
   - **Acerto de intencao** — a frase foi encaminhada (`jarvis.router.encaminhar`)
     para a mesma decisao (tipo/accao/argumento) que a frase escrita produz? E
     esta que manda: e a que decide se o produto funciona.
   - **WER** (Word Error Rate) da transcricao — diagnostico, nao o criterio
     principal: ajuda a perceber PORQUE uma intencao falhou, mas uma
     transcricao imperfeita que ainda assim encaminha certo nao e uma falha do
     produto.
3. **Limiares** (aplicados ao acerto de intencao):
   - **acerto de intencao >= 90% E WER <= 15%** (a conjuncao exata da D7: os
     dois ao mesmo tempo, nao um deles) -> portugues europeu fica, sem mais
     discussao.
   - **entre 75% e 90%** -> portugues europeu fica, mas aplicam-se primeiro as
     melhorias baratas, por esta ordem: (a) modelo de transcricao maior
     (`large-v3` em vez de `medium`); (b) `initial_prompt` com o vocabulario
     dos comandos e dos nomes de projetos (D51 so autoriza ligar isto DEPOIS de
     medir e dentro desta banda); (c) correspondencia aproximada contra a
     lista branca em vez de exigir a frase exata. Depois de aplicar, mede-se
     outra vez com a MESMA amostra.
   - **< 75%** -> repete-se A MESMA amostra de 20 frases em ingles e leva-se ao
     Sponsor a comparacao lado a lado pela fila (`forja ask`), com o default
     **"manter portugues"**. Mudar a lingua dos comandos e sempre decisao do
     Sponsor, nunca do run.

Nota sobre QUAL WER: a D7 fala de "WER" no singular, e ha dois calculos
possiveis sobre a mesma corrida — a **macro-media** (media dos WER das 20
frases, cada frase pesa o mesmo) e o **WER de corpus** (soma dos erros de
edicao a dividir pela soma das palavras de referencia, as frases longas pesam
mais). O arnes escreve os DOIS, com o rotulo, exatamente para ninguem poder
escolher o mais conveniente depois de ver o resultado; quem correr a medicao
real diz por escrito qual esta a comparar com o limiar de 15%.

Este protocolo so se cumpre de verdade com a voz do Sponsor. Nao se conclui
nada sobre o portugues europeu dele a partir de audio sintetico (inaceitavel
n.6 do PRODUCT-PROFILE).

## Aviso obrigatorio desta task (D34) — LER ANTES DE USAR QUALQUER NUMERO DAQUI

O arnes `scripts/medir_voz.py`, entregue por esta task (T7), corre esta MESMA
amostra com **audio sintetico** (a propria voz Piper do jarvis, gerada por
`scripts/gerar_wav.py`, transcrita de volta por `scripts/transcrever_ficheiro.py`).
Isto e **um teste da cadeia** (sintese -> transcricao -> encaminhador) e serve
para apanhar regressoes de codigo. **NAO E, e nunca pode ser tratado como, uma
medida do reconhecimento da voz do Sponsor.** Nenhum numero produzido por audio
sintetico autoriza propor a troca para ingles: essa proposta so pode ser feita
depois de medir a voz humana dele, e mesmo ai fica na fila do Sponsor com o
default "manter portugues" (D34, D7). Qualquer relatorio que cite os numeros da
passagem sintetica tem de repetir este aviso por escrito.

## A amostra (20 frases fixas)

`intencao esperada` e texto para um humano ler; a fonte da verdade que o arnes
usa de facto e sempre `jarvis.router.encaminhar()` aplicado a frase ORIGINAL
(depois de substituir os marcadores), nunca um valor escrito a mao que possa
ficar desatualizado se o router mudar.

| nº | tipo | frase (com marcadores) | intenção esperada |
|----|------|--------------------------|--------------------|
| 1 | local | que horas são | horas_e_data (horas) |
| 2 | local | que dia é hoje | horas_e_data (data) |
| 3 | local | abre o vs code no <projeto-1> | abrir_vscode (<projeto-1>) |
| 4 | local | abre a pasta do <projeto-2> | abrir_pasta (<projeto-2>) |
| 5 | local | cala-te | calar |
| 6 | local | adormece | adormecer |
| 7 | local | acorda | acordar |
| 8 | local | diz-me as horas | horas_e_data (horas) |
| 9 | local | abre o vs code no projeto <projeto-2> | abrir_vscode (<projeto-2>) |
| 10 | local | qual é a data de hoje | horas_e_data (data) |
| 11 | claude | diz ao claude para abrir um ticket sobre o vs code no <projeto-1> | texto (gatilho no meio da frase, D4 fechada) |
| 12 | claude | não abras o vs code no <projeto-2> | texto (negação, D4) |
| 13 | claude | abre o vs code | texto (ambíguo, sem projeto identificável) |
| 14 | claude | abre a pasta do projeto fantasma | texto (projeto desconhecido, D4) |
| 15 | claude | compra-me duas ações agora | texto (vocabulário financeiro, D12) |
| 16 | claude | achas que amanhã vai chover | texto (pergunta comum) |
| 17 | claude | corre os testes do <projeto-1> e diz-me o que falhou | texto (pedido de trabalho) |
| 18 | claude | faz um resumo do que mudou no <projeto-2> hoje | texto (pedido geral) |
| 19 | claude | pergunta ao claude que dia é o prazo do relatório | texto (destinatário explícito, nunca vira horas_e_data) |
| 20 | claude | quando acabares de abrir a pasta do <projeto-1> manda-me um resumo | texto (menção a abrir a pasta no meio de uma frase, D4 fechada) |

Nota: a linha 14 usa "projeto fantasma", um nome GENERICO e inventado para
testar "projeto desconhecido" — nunca um projeto real do Sponsor (D10).
