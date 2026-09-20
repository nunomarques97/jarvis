# Amostra fixa de 20 frases EN — medição do inglês (D53/D58)

Ficheiro versionado (D1/D10): nenhum caminho nem nome de projeto REAL do
Sponsor entra aqui. Os nomes de projeto aparecem so como os marcadores
`<projeto-1>` e `<projeto-2>`, exatamente como em `tests/voz/frases-pt.md` —
quem correr a medicao substitui-os a partir da CONFIGURACAO local
(`config.toml`, fora do Git) ou, na falta dela, a partir do exemplo
versionado (`config.exemplo.toml`, projetos ficticios "exemplo-um" e
"exemplo-dois"). Nada abaixo assume um nome real. Nenhuma frase desta amostra
usa o vocabulario ingles de ordens de compra/venda que a D64/D52 proibe em
ficheiros versionados deste run: a linha 15 continua a testar "vocabulario
financeiro generico" com outras palavras (ver a tabela e a nota no fim).

## Limite desta amostra que nao existe na amostra pt-PT (LER ANTES DE USAR)

Esta amostra tem as MESMAS 20 intencoes de `tests/voz/frases-pt.md` (mesma
numeracao 1–20, mesmo tipo `local`/`claude` linha a linha, mesmos marcadores
`<projeto-1>`/`<projeto-2>`), so traduzida para ingles. Mas quando
`scripts/medir_voz.py` a corre, a **sintese continua a ser feita pela voz
Piper pt-PT** (`pt_PT-tugao-medium`) — **nao existe voz inglesa neste run**
(S11 de `docs/forja/TECHNOLOGY.md`: candidata futura registada, nao adotada
agora). Ou seja, o audio que sai para as frases desta tabela e ingles lido
com sotaque e prosodia portugueses.

Isto e um **limite do teste da cadeia** (sintese -> transcricao ->
encaminhador), nao uma medida de nada: **nunca** se conclui, a partir deste
audio sintetico, se o ingles do Sponsor (ou o ingles em geral) e bem ou mal
reconhecido — nem sequer se a pronuncia inglesa "correta" seria melhor ou
pior. O unico numero fiavel sobre o ingles falado pelo Sponsor so existe
quando ele falar ingles ao microfone a serio, no QA de fecho ou num run
seguinte (D34, inaceitavel n.6 do PRODUCT-PROFILE, alargado pela D58).

Adicionalmente (herdado da D53/T2, ver `docs/forja/evidence/base-resumo.md`
depois da T2 correr): as frases `local` desta amostra ainda nao tem lista
branca em ingles no `jarvis/router.py` (essa e a T7 deste run, D58). Ate la,
`scripts/medir_voz.py` conta essas linhas como "divergentes" nos agregados
(tipo documentado `local` != tipo calculado por `encaminhar()`, que hoje so
reconhece portugues) — nao e um defeito desta amostra, e o esperado antes da
T7 correr.

## Protocolo completo da D7/D58 (para o QA de fecho, com a voz do Sponsor)

Mesmo protocolo de `tests/voz/frases-pt.md`, aplicado a esta amostra em vez
da amostra pt-PT: 20 frases fixas, ditas 3 vezes cada nas condicoes normais
do Sponsor (60 gravacoes), acerto de intencao como metrica principal
(`jarvis.router.encaminhar`), WER como diagnostico, os mesmos limiares (>=
90% E WER <= 15% / entre 75% e 90% / < 75%). A diferenca introduzida pela
D58: nao ha escada de "propor ingles" a partir desta amostra — o ingles ja e
uma lingua de entrada de pleno direito, a decisao de qual usar no dia-a-dia e
sempre do Sponsor, informada pelos numeros lado a lado das duas amostras,
nunca por palpite.

Nota sobre QUAL WER (igual a pt-PT): macro-media e WER de corpus sao numeros
diferentes sobre a mesma corrida; o arnes escreve os dois, com o rotulo.

Este protocolo so se cumpre de verdade com a voz do Sponsor. Nao se conclui
nada sobre o ingles dele a partir de audio sintetico (inaceitavel n.6 do
PRODUCT-PROFILE).

## Aviso obrigatorio desta task (D34) — LER ANTES DE USAR QUALQUER NUMERO DAQUI

O arnes `scripts/medir_voz.py` corre esta amostra com **audio sintetico** (a
voz Piper pt-PT do jarvis, gerada por `scripts/gerar_wav.py` e transcrita de
volta por `scripts/transcrever_ficheiro.py`, ver o aviso acima sobre a voz).
Isto e **um teste da cadeia** (sintese -> transcricao -> encaminhador) e serve
para apanhar regressoes de codigo. **NAO E, e nunca pode ser tratado como,
uma medida do reconhecimento do ingles do Sponsor.** Nenhum numero produzido
por audio sintetico autoriza nenhuma conclusao sobre qual lingua ele deve
usar no dia-a-dia: essa escolha e sempre dele, informada pelos numeros reais
depois de ele falar (D34, D58). Qualquer relatorio que cite os numeros da
passagem sintetica tem de repetir este aviso por escrito.

## A amostra (20 frases fixas)

`intencao esperada` e texto para um humano ler; a fonte da verdade que o
arnes usa de facto e sempre `jarvis.router.encaminhar()` aplicado a frase
ORIGINAL (depois de substituir os marcadores), nunca um valor escrito a mao
que possa ficar desatualizado se o router mudar.

| nº | tipo | frase (com marcadores) | intenção esperada |
|----|------|--------------------------|--------------------|
| 1 | local | what time is it | horas_e_data (horas) |
| 2 | local | what day is it today | horas_e_data (data) |
| 3 | local | open vs code in the <projeto-1> | abrir_vscode (<projeto-1>) |
| 4 | local | open the <projeto-2> folder | abrir_pasta (<projeto-2>) |
| 5 | local | be quiet | calar |
| 6 | local | go to sleep | adormecer |
| 7 | local | wake up | acordar |
| 8 | local | tell me the time | horas_e_data (horas) |
| 9 | local | open vs code in project <projeto-2> | abrir_vscode (<projeto-2>) |
| 10 | local | what's today's date | horas_e_data (data) |
| 11 | claude | tell claude to open a ticket about vs code in the <projeto-1> | texto (gatilho no meio da frase, D4 fechada) |
| 12 | claude | don't open vs code in the <projeto-2> | texto (negação, D4) |
| 13 | claude | open vs code | texto (ambíguo, sem projeto identificável) |
| 14 | claude | open the folder for the ghost project | texto (projeto desconhecido, D4) |
| 15 | claude | invest some money in stocks for me right now | texto (vocabulário financeiro, D12) |
| 16 | claude | do you think it will rain tomorrow | texto (pergunta comum) |
| 17 | claude | run the tests for the <projeto-1> and tell me what failed | texto (pedido de trabalho) |
| 18 | claude | write a summary of what changed in the <projeto-2> today | texto (pedido geral) |
| 19 | claude | ask claude what day the report is due | texto (destinatário explícito, nunca vira horas_e_data) |
| 20 | claude | when you finish opening the folder for the <projeto-1> send me a summary | texto (menção a abrir a pasta no meio de uma frase, D4 fechada) |

Nota: a linha 14 usa "ghost project", tradução direta do "projeto fantasma"
da amostra pt-PT — um nome GENERICO e inventado para testar "projeto
desconhecido", nunca um projeto real do Sponsor (D10).

Nota: a linha 15 testa o mesmo papel que a linha 15 pt-PT ("compra-me duas
ações agora"), mas com vocabulario financeiro generico em ingles que NAO usa
o vocabulario ingles de ordens de compra/venda proibido pela D64/D52 nesta
task: "invest" e "stocks" bastam para manter o tom financeiro sem entrar
nesse vocabulario.
