# jarvis — controlo por voz das sessões do Claude Code

Assistente de voz local que ouve a palavra de ativação "hey jarvis", transcreve o pedido localmente e o entrega a uma sessão do Claude Code, respondendo em voz alta. Substitui escrever prompts nos projetos definidos na configuração local (`config.toml`).

## Exemplos de comandos

- "Hey jarvis, abre o VS Code no `<projeto-1>`."
- "Hey jarvis, pede o status da última sessão do `<projeto-1>`."
- "Hey jarvis, lê-me o relatório do último run."
- "Hey jarvis, lança o run X no projeto Y."

Mais tarde, quando o programa de trading existir: "prepara a trade" produz uma proposta com números; a confirmação é sempre escrita pelo utilizador, nunca por voz.

## Fases

**Fase 1 — escolher a base (time box: 1 hora de pesquisa).** Comparar projetos open source de assistente de voz com critérios explícitos: funciona offline no Windows com GPU NVIDIA, palavra de ativação, transcrição local, voz de resposta local, licença permissiva, projeto vivo, $0, e facilidade de ligar a um processo externo em vez de a uma API paga. Decisão escrita com as razões e as alternativas rejeitadas. Se nenhum servir, dizê-lo e propor o mínimo escrito de raiz.

**Fase 2 — clonar e adaptar.** Instalar a base escolhida, ligá-la ao Claude Code (entregar o texto a uma sessão existente é preferível a abrir uma nova em cada frase), tratar os comandos locais sem gastar tokens (abrir pastas, abrir o VS Code, dizer as horas), e pôr o utilizador a falar com o Claude pela primeira vez.

## Regras

- Tudo local e a $0: sem cloud paga, sem chaves de API, sem enviar áudio para fora do PC.
- Português europeu nos comandos; se o reconhecimento falhar, medir e propor inglês em vez de adivinhar.
- Nenhuma ordem de compra ou venda por voz, nem agora nem depois: uma IA nunca executa ordens, e a voz não é autorização para gastar dinheiro.
- Nada é instalado noutros projetos a partir daqui.

## Repositório público

Este repositório vai para o GitHub em público. Por isso:

- Nada de conversas, transcrições, áudio ou estado das ferramentas de IA em commits: a configuração local das ferramentas, gravações e transcrições estão no `.gitignore`.
- Nada de segredos, tokens, `.env` ou caminhos pessoais dentro do código ou da documentação. Configuração pessoal fica em ficheiros ignorados, com um `.example` versionado.
- Antes de cada commit, verificar que nenhum ficheiro de credenciais ou de áudio entrou no staging.

## Hardware

Windows 11, RTX 5060 Ti com 16 GB de VRAM, i5-14400F, 32 GB de RAM, Ollama local já instalado.

## Licença

AGPL-3.0. Copyright (c) 2026 Nuno Marques. Ver [LICENSE](LICENSE).
