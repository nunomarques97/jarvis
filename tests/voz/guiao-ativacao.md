# Guião de gravação da palavra de ativação

O que o Sponsor grava para `scripts/avaliar_ativacao.py` medir a palavra de
ativação com a voz dele: **20 repetições da palavra** e **30 minutos de ruído
normal da casa**. Só estas gravações decidem o limiar e dizem se as metas
estão cumpridas (deteção >= 95%, no máximo 1 falso despertar por 30 min); voz
sintética não conta. Sem elas, a evidência fica "PENDENTE — passo do Sponsor".

A palavra é a da língua do `config.toml` (`[ouvido] lingua`): **"hey jarvis"**
em inglês, **"boas jarvis"** em português. Diz-se só a palavra, sozinha, sem
comando a seguir.

As gravações ficam em `recordings/ativacao/` (ignorada pelo Git, como todo o
áudio). O gravador retoma onde parou e nunca apaga ficheiros. Sem `--com-som`
não toca nada.

## Passo 1 — 20 repetições da palavra (cerca de 5 minutos)

```
.venv\Scripts\python scripts/avaliar_ativacao.py --gravar-palavra --lingua en
```

Para cada linha da tabela: Enter, dizer a palavra da maneira indicada, Enter.
Não é preciso ser perfeito: as variações existem para a medição apanhar o
que acontece no dia a dia.

| n | como dizer |
|----|------------|
| 01 | voz normal, sentado à secretária |
| 02 | voz normal, sentado à secretária |
| 03 | voz normal, sentado à secretária |
| 04 | voz normal, sentado à secretária |
| 05 | mais baixo, como quem não quer incomodar ninguém |
| 06 | mais baixo, quase a murmurar |
| 07 | mais alto, como para alguém do outro lado da sala |
| 08 | mais depressa |
| 09 | mais devagar, bem separado |
| 10 | cansado, como no fim do dia |
| 11 | com a cabeça virada para o lado, sem olhar para o microfone |
| 12 | encostado para trás na cadeira |
| 13 | a cerca de um metro do microfone |
| 14 | a cerca de dois metros do microfone, de pé |
| 15 | com a música ou o vídeo que costuma ter a tocar |
| 16 | com a mão à frente da boca |
| 17 | em tom de pergunta |
| 18 | a meio de escrever no teclado |
| 19 | voz normal, sentado à secretária |
| 20 | voz normal, sentado à secretária |

## Passo 2 — 30 minutos de ruído normal (deixar a correr)

```
.venv\Scripts\python scripts/avaliar_ativacao.py --gravar-ruido
```

Carregar Enter para começar e deixar o microfone a gravar enquanto se faz a
vida normal à secretária: computador ligado, teclado, rato, música ou vídeos
se for hábito, pessoas a falar em casa se for hábito, telefone. **Não dizer a
palavra de ativação** durante esta gravação: tudo o que o detetor acordar
aqui conta como falso despertar. O gravador guarda blocos de 5 minutos e pára
sozinho aos 30 minutos; Enter a meio pára mais cedo e o que já foi gravado
fica (volta a correr o mesmo comando para completar).

## Passo 3 — medir

```
.venv\Scripts\python scripts/avaliar_ativacao.py --lingua en
```

Escreve em `docs/forja/evidence/` a curva limiar -> deteção/falsos
despertares, o limiar escolhido pelos números e a linha a pôr no
`config.toml` (`[ouvido] limiar_ativacao = ...`).

## Só em português — conjunto de treino

"boas jarvis" não tem modelo pronto: treina-se localmente com
`scripts/treinar_ativacao.py` (procedimento em `docs/MODELOS.md`). O treino
usa um conjunto SEPARADO de 20 repetições, gravado com a mesma tabela:

```
.venv\Scripts\python scripts/avaliar_ativacao.py --gravar-palavra --lingua pt --conjunto treino
```

A medição do passo 3 usa só o conjunto de avaliação (passo 1) e o ruído do
passo 2; nenhum dos dois entra no treino, para os números não saírem
inflacionados.
