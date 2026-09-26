r"""Configuracao privada do jarvis: projetos conhecidos, caminhos e microfone.

O ficheiro real (`config.toml`, na raiz do repo) e IGNORADO pelo Git:
tem os nomes dos projetos do utilizador e os caminhos reais no disco dele, e este
repositorio e publico. O que fica versionado e `config.exemplo.toml`,
com a mesma estrutura mas so dados ficticios (projetos "exemplo-um" e
"exemplo-dois", caminhos "D:/caminho/para/...") — nunca copiar dados reais para
esse ficheiro.

Formato esperado (ver config.exemplo.toml para o exemplo completo):

    [microfone]
    nome = "Nome do dispositivo, como aparece no Windows"

    [[projetos]]
    nome = "nome-do-projeto"
    caminho = "D:/caminho/para/o/projeto"

    [ouvido]                      # opcional; sem ela valem os valores por omissao
    tecla = "ctrl-direito"        # tecla de falar (segurar para falar)
    motor = "parakeet-tdt-0.6b-v3"
    device = "cpu"
    lingua = "pt"
    limiar_ativacao = 0.6         # limiar da palavra de ativacao (0 a 1)

    [interprete]                  # opcional; sem ela valem os valores por omissao
    url = "http://127.0.0.1:11434"  # Ollama local; so localhost e aceite
    modelo = "qwen3:8b"
    modelo_alternativo = "qwen3:4b"
    limite_s = 5.0                # acima disto a frase fica "desconhecido"
    carregamento_s = 20.0         # sem modelo carregado: espera enquanto o Ollama o carrega
    confirmacao_s = 30.0          # espera pelo "sim"; depois cancela sem enviar

    [forja]                       # opcional; sem ela nao ha runs FORJA por voz
    caminho = "D:/caminho/para/forja"         # instalacao (tem bin/forja.mjs)
    perfil = "D:/caminho/para/perfil.json"    # --config dos runs lancados
    provider = "claude"                       # claude ou codex

    [perguntas]                   # opcional; sem ela valem os valores por omissao
    modelo = "claude-haiku-4-5"   # modelo do Claude Code para as perguntas gerais
    limite_s = 60                 # espera maxima pela resposta (10 a 300)
    localizacao = "Portugal"      # onde o utilizador esta, para o tempo e as noticias

    [voz]                         # opcional; sem ela vale a voz por omissao
    nome = "bm_fable"             # voz inglesa do Kokoro (lista fechada VOZES_INGLESAS)

    [adaptacao]                   # opcional; sem ela a transcricao nao e adaptada
    reforco = false               # reforco das frases de comando e nomes de projeto
    bonus = 1.5                   # forca do reforco, no logit de cada token
    lexico = false                # correcoes aprendidas (models/adaptacao/lexico-en.json)

    [escuta]                      # opcional; sem ela valem os valores por omissao
    seguimento_s = 8.0            # escuta sem palavra de ativacao depois de o jarvis falar (3 a 30)
    sons = true                   # som curto quando essa escuta abre e outro quando fecha
    volume = 0.15                 # volume desses sons (maior do que 0, no maximo 1)

    [memoria]                     # opcional; sem ela valem os valores por omissao
    trocas = 10                   # perguntas e respostas lembradas nas perguntas gerais (1 a 10)
    expira_min = 30               # minutos sem perguntas gerais ate esquecer a conversa (1 a 30)
    factos = 50                   # factos no caderno (1 a 50)
    caracteres = 4000             # caracteres de todos os factos juntos (200 a 4000)
    frases_interprete = 5         # frases recentes que o interprete local recebe (3 a 5)

`carregar_config()` le com `tomllib` (biblioteca padrao do Python 3.11+, sem
dependencia nova), valida a estrutura E os caminhos no disco (um caminho
de configuracao e entrada externa e verifica-se na leitura, nao se aceita em
bruto) e devolve um `Config` imutavel. Erros sao sempre `ConfigError`, com uma
mensagem legivel que diz o que falhou e onde corrigir.

Este modulo NUNCA executa nada com os valores lidos: so os devolve em
`Projeto`/`Config` para quem precisar deles (o `router` para encaminhar,
`jarvis.acoes_locais` para executar). Nenhum caminho ou nome daqui entra numa linha de comandos.

Uso:

    from jarvis.config import carregar_config

    config = carregar_config()        # le config.toml na raiz do repo
    config.microfone                  # -> "Nome do dispositivo"
    config.projetos                   # -> tupla de Projeto(nome, caminho)

Autoteste das partes puras (parsing e validacao, com ficheiros temporarios,
nunca com o config.toml real):

    .venv\Scripts\python -m jarvis.config --autoteste
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

#: Onde `carregar_config()` procura por omissao: a raiz do repositorio.
CAMINHO_CONFIG_PADRAO = RAIZ / "config.toml"

#: O exemplo versionado, citado nas mensagens de erro para quem ainda nao tem
#: config.toml (nunca se le este ficheiro como configuracao real).
CAMINHO_EXEMPLO = RAIZ / "config.exemplo.toml"


#: Teclas que podem ser a tecla de falar, pelo nome que se escreve no
#: config.toml, com o virtual-key code do Windows que `jarvis.ouvido` le.
#: Lista fechada. A leitura da tecla nao bloqueia a acao normal dela na
#: aplicacao com foco, por isso so entram teclas que sozinhas nao tem acao
#: habitual: os modificadores do lado direito, Scroll Lock (acende a luz) e
#: F13-F24. Ficam de fora Insert, Menu, Pause e F1-F12, que mudam o modo de
#: escrita, abrem menus, pausam a consola ou disparam comandos.
TECLAS_DE_FALAR: dict[str, int] = {
    "ctrl-direito": 0xA3,
    "alt-direito": 0xA5,
    "shift-direito": 0xA1,
    "scroll-lock": 0x91,
    **{f"f{numero}": 0x6F + numero for numero in range(13, 25)},
}

LINGUAS_DO_OUVIDO = ("pt", "en")
DEVICES_DO_OUVIDO = ("cpu", "cuda")

#: Limiar do score da palavra de ativacao ate haver numeros medidos na voz do
#: utilizador; `scripts/avaliar_ativacao.py` escolhe o valor a escrever em
#: [ouvido].limiar_ativacao a partir da curva limiar -> detecao/falsos.
LIMIAR_DE_ATIVACAO_PADRAO = 0.6


#: O interprete fala com o Ollama por HTTP e so aceita este computador: o
#: texto ditado nunca sai do PC antes de o utilizador o confirmar.
HOSTS_LOCAIS = ("127.0.0.1", "localhost", "::1")

#: Tempo maximo que o interprete espera pelo LLM. Acima disto a frase segue
#: como "desconhecido" (so para confirmacao); o config pode baixar, nunca subir.
LIMITE_DO_INTERPRETE_S = 5.0

#: Quando nenhum modelo do interprete esta carregado (outro programa usou o
#: Ollama e despejou-o), o jarvis diz "um momento" e espera ate este tempo
#: pelo carregamento em vez de desistir no limite normal.
ESPERA_DO_CARREGAMENTO_S = 20.0
ESPERA_DO_CARREGAMENTO_MAXIMA_S = 60.0

#: Quanto tempo o jarvis espera pelo "sim" depois de acabar de dizer o recap.
#: Sem resposta dentro deste tempo o pedido e cancelado sem ser enviado.
ESPERA_DA_CONFIRMACAO_S = 30.0
ESPERA_DA_CONFIRMACAO_MINIMA_S = 3.0
ESPERA_DA_CONFIRMACAO_MAXIMA_S = 120.0

#: Providers que um run FORJA lancado por voz pode usar (lista fechada).
PROVIDERS_DA_FORJA = ("claude", "codex")

#: O ponto de entrada da FORJA dentro da pasta de instalacao.
SUBCAMINHO_DA_FORJA = ("bin", "forja.mjs")

#: Nome de modelo do Ollama ("familia:etiqueta"), sem espacos nem caminhos.
_PADRAO_NOME_DE_MODELO = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}(?::[a-z0-9][a-z0-9._-]{0,63})?")

#: Nome ou alias de modelo do Claude Code ("claude-haiku-4-5", "sonnet"). Vai
#: para a linha de comandos, por isso comeca por uma letra (nunca um "-" que
#: seria lido como opcao) e so tem letras minusculas, digitos, "." e "-".
_PADRAO_MODELO_DO_CLAUDE = re.compile(r"[a-z][a-z0-9.-]{1,63}")

#: Respostas a perguntas gerais: modelo rapido e limites da espera.
MODELO_DAS_PERGUNTAS = "claude-haiku-4-5"
LIMITE_DAS_PERGUNTAS_S = 60.0
LIMITE_DAS_PERGUNTAS_MINIMO_S = 10.0
LIMITE_DAS_PERGUNTAS_MAXIMO_S = 300.0
LOCALIZACAO_PADRAO = "Portugal"
LOCALIZACAO_MAXIMA = 80

#: Uma localizacao e so uma linha curta de texto ("Porto, Portugal"): letras,
#: digitos, espacos e pontuacao simples. Vai no pedido ao modelo.
_PADRAO_LOCALIZACAO = re.compile(r"[^\W_](?:[\w .,'()-]*[^\W_])?")

#: Vozes inglesas do Kokoro-82M que o jarvis aceita (lista fechada, todas no
#: `voices-v1.0.bin`): a americana de antes e as quatro masculinas britanicas
#: comparadas por `scripts/medir_latencia_voz.py --vozes`. A primeira letra do
#: nome diz o sotaque ("a" americano, "b" britanico) e escolhe a lingua do
#: fonemizador em `jarvis.voz`.
VOZES_INGLESAS = ("af_heart", "bm_george", "bm_lewis", "bm_daniel", "bm_fable")

#: Voz inglesa por omissao: masculina britanica, escolhida pelos numeros em
#: docs/MODELOS.md (latencia ate ao primeiro audio nao pior do que af_heart).
VOZ_INGLESA_PADRAO = "bm_fable"


#: Forca do reforco de frases na transcricao (bonus somado ao logit de cada
#: token que continua uma frase reforcada) e o intervalo aceite.
BONUS_DE_REFORCO_PADRAO = 1.5
BONUS_DE_REFORCO_MAXIMO = 10.0

#: Quanto tempo o jarvis continua a ouvir sem palavra de ativacao depois de
#: acabar de falar, e o intervalo aceite.
SEGUIMENTO_S = 8.0
SEGUIMENTO_MINIMO_S = 3.0
SEGUIMENTO_MAXIMO_S = 30.0

#: Volume dos sons de abertura e fecho da escuta (fracao da escala completa):
#: baixo por omissao, para marcar sem assustar.
VOLUME_DOS_SONS_PADRAO = 0.15

#: Memoria das perguntas gerais: tetos fixos. A configuracao pode baixar estes
#: valores, nunca subi-los, para o contexto de cada pergunta ter um teto garantido.
TROCAS_MAXIMAS = 10
EXPIRA_MAXIMO_MIN = 30.0
FACTOS_MAXIMOS = 50
CARACTERES_DOS_FACTOS_MAXIMOS = 4000
CARACTERES_DOS_FACTOS_MINIMOS = 200
#: Frases recentes que o interprete local recebe como contexto.
FRASES_DO_INTERPRETE_MINIMAS = 3
FRASES_DO_INTERPRETE_MAXIMAS = 5


class ConfigError(Exception):
    """Configuracao em falta, mal formada, ou com um caminho que nao existe."""


@dataclass(frozen=True)
class ConfigOuvido:
    """Como o jarvis ouve: tecla de falar e motor de transcricao residente.

    O motor por omissao e o Parakeet em CPU: nao gasta VRAM e, quente, fica
    abaixo dos limites de latencia da tecla de falar.
    """

    tecla: str = "ctrl-direito"
    motor: str = "parakeet-tdt-0.6b-v3"
    device: str = "cpu"
    lingua: str = "pt"
    limiar_ativacao: float = LIMIAR_DE_ATIVACAO_PADRAO

    @property
    def codigo_da_tecla(self) -> int:
        return TECLAS_DE_FALAR[self.tecla]


@dataclass(frozen=True)
class ConfigInterprete:
    """Onde esta o LLM local que interpreta as frases, e que modelos usa.

    O modelo alternativo e o recurso quando o principal nao esta instalado ou
    nao cabe na VRAM livre.
    """

    url: str = "http://127.0.0.1:11434"
    modelo: str = "qwen3:8b"
    modelo_alternativo: str = "qwen3:4b"
    limite_s: float = LIMITE_DO_INTERPRETE_S
    #: Espera maxima pelo LLM quando o modelo ainda tem de ser carregado.
    carregamento_s: float = ESPERA_DO_CARREGAMENTO_S
    #: Espera pela confirmacao de um pedido recapitulado; depois cancela.
    confirmacao_s: float = ESPERA_DA_CONFIRMACAO_S


@dataclass(frozen=True)
class ConfigForja:
    """Onde esta a FORJA e com que perfil o jarvis lanca runs por voz.

    `caminho` e a pasta da instalacao (com `bin/forja.mjs`); `perfil` e o
    ficheiro JSON passado em `--config` a cada run lancado. Os dois ficam so
    no config.toml ignorado pelo Git.
    """

    caminho: Path
    perfil: Path
    provider: str = "claude"

    @property
    def ponto_de_entrada(self) -> Path:
        return self.caminho.joinpath(*SUBCAMINHO_DA_FORJA)


@dataclass(frozen=True)
class ConfigPerguntas:
    """Como o jarvis responde a perguntas gerais pelo Claude Code com pesquisa na web.

    `modelo` vai para `--model`; `limite_s` e a espera maxima pela resposta;
    `localizacao` diz ao modelo onde o utilizador esta quando a pergunta nao
    diz (o tempo, os jogos e as noticias de hoje).
    """

    modelo: str = MODELO_DAS_PERGUNTAS
    limite_s: float = LIMITE_DAS_PERGUNTAS_S
    localizacao: str = LOCALIZACAO_PADRAO


@dataclass(frozen=True)
class ConfigVoz:
    """Que voz fala as respostas em ingles (nome de uma voz do Kokoro)."""

    nome: str = VOZ_INGLESA_PADRAO


@dataclass(frozen=True)
class ConfigAdaptacao:
    """Adaptacao da transcricao ao sotaque (ver `jarvis.adaptacao`).

    Desligada por omissao: sem [adaptacao] a transcricao fica como estava.
    """

    reforco: bool = False
    bonus: float = BONUS_DE_REFORCO_PADRAO
    lexico: bool = False


@dataclass(frozen=True)
class ConfigEscuta:
    """A escuta sem palavra de ativacao depois de o jarvis falar, e os seus sons.

    `seguimento_s` e quanto tempo se pode continuar sem "hey jarvis"; `sons`
    liga os sons de abertura e fecho dessa escuta; `volume` e a amplitude
    maxima desses sons, em fracao da escala completa.
    """

    seguimento_s: float = SEGUIMENTO_S
    sons: bool = True
    volume: float = VOLUME_DOS_SONS_PADRAO


@dataclass(frozen=True)
class ConfigMemoria:
    """Limites da memoria das perguntas gerais (ver `jarvis.memoria`).

    `trocas` e quantas perguntas e respostas recentes vao como contexto;
    `expira_min` e quanto tempo sem perguntas gerais ate as esquecer;
    `factos` e `caracteres` limitam o caderno de factos. Todos tem um teto
    fixo que a configuracao so pode baixar. `frases_interprete` e quantas
    frases recentes (e o que o jarvis fez com elas) o interprete local
    recebe, para perceber "send that to jarvis too" (3 a 5).
    """

    trocas: int = TROCAS_MAXIMAS
    expira_min: float = EXPIRA_MAXIMO_MIN
    factos: int = FACTOS_MAXIMOS
    caracteres: int = CARACTERES_DOS_FACTOS_MAXIMOS
    frases_interprete: int = FRASES_DO_INTERPRETE_MAXIMAS


@dataclass(frozen=True)
class Projeto:
    """Um projeto conhecido: o nome que o utilizador diz e o caminho no disco."""

    nome: str
    caminho: Path


@dataclass(frozen=True)
class Config:
    """Configuracao privada carregada e validada."""

    microfone: str
    projetos: tuple[Projeto, ...]
    ouvido: ConfigOuvido = ConfigOuvido()
    interprete: ConfigInterprete = ConfigInterprete()
    #: None quando o config.toml nao tem a tabela [forja].
    forja: ConfigForja | None = None
    perguntas: ConfigPerguntas = ConfigPerguntas()
    voz: ConfigVoz = ConfigVoz()
    adaptacao: ConfigAdaptacao = ConfigAdaptacao()
    escuta: ConfigEscuta = ConfigEscuta()
    memoria: ConfigMemoria = ConfigMemoria()

    def encontrar_projeto(self, nome: str) -> Projeto | None:
        """Devolve o Projeto com este nome exato (case-insensitive), ou None."""
        alvo = nome.strip().casefold()
        for projeto in self.projetos:
            if projeto.nome.casefold() == alvo:
                return projeto
        return None


def _erro_ficheiro_em_falta(caminho: Path) -> ConfigError:
    return ConfigError(
        f"configuracao nao encontrada: '{caminho}'. Copia '{CAMINHO_EXEMPLO.name}' para "
        f"'{caminho.name}' na raiz do repositorio e preenche os teus dados reais "
        "(o ficheiro real fica de fora do Git, D10)."
    )


def _validar_tabela(bruto: dict, caminho: Path) -> None:
    if "microfone" not in bruto:
        raise ConfigError(
            f"'{caminho}': falta a tabela [microfone]. Ve '{CAMINHO_EXEMPLO.name}' "
            "para o formato esperado."
        )
    if not isinstance(bruto["microfone"], dict):
        raise ConfigError(f"'{caminho}': [microfone] tem de ser uma tabela, nao {type(bruto['microfone']).__name__}.")
    if "nome" not in bruto["microfone"]:
        raise ConfigError(f"'{caminho}': falta [microfone].nome (o nome do dispositivo no Windows).")
    if not isinstance(bruto["microfone"]["nome"], str) or not bruto["microfone"]["nome"].strip():
        raise ConfigError(f"'{caminho}': [microfone].nome tem de ser texto nao vazio.")

    if "projetos" not in bruto:
        raise ConfigError(
            f"'{caminho}': falta a lista [[projetos]] (pelo menos um projeto conhecido). "
            f"Ve '{CAMINHO_EXEMPLO.name}' para o formato esperado."
        )
    if not isinstance(bruto["projetos"], list) or not bruto["projetos"]:
        raise ConfigError(f"'{caminho}': [[projetos]] tem de ser uma lista com pelo menos um projeto.")


def _validar_projeto(item: object, indice: int, caminho: Path) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise ConfigError(f"'{caminho}': projetos[{indice}] tem de ser uma tabela [[projetos]].")
    nome = item.get("nome")
    projeto_caminho = item.get("caminho")
    if not isinstance(nome, str) or not nome.strip():
        raise ConfigError(f"'{caminho}': projetos[{indice}].nome tem de ser texto nao vazio.")
    if not isinstance(projeto_caminho, str) or not projeto_caminho.strip():
        raise ConfigError(
            f"'{caminho}': projetos[{indice}].caminho ('{nome}') tem de ser texto nao vazio."
        )
    return nome.strip(), projeto_caminho.strip()


def _validar_ouvido(bruto: dict, caminho: Path) -> ConfigOuvido:
    """A tabela [ouvido], opcional: cada chave e validada contra uma lista fechada."""
    from jarvis.stt import MOTORES

    if "ouvido" not in bruto:
        return ConfigOuvido()
    tabela = bruto["ouvido"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [ouvido] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = {
        "tecla": tuple(TECLAS_DE_FALAR),
        "motor": MOTORES,
        "device": DEVICES_DO_OUVIDO,
        "lingua": LINGUAS_DO_OUVIDO,
    }
    desconhecidas = sorted(set(tabela) - set(permitidas) - {"limiar_ativacao"})
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [ouvido] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)}, limiar_ativacao)."
        )
    valores: dict[str, object] = {}
    if "limiar_ativacao" in tabela:
        limiar = tabela["limiar_ativacao"]
        if isinstance(limiar, bool) or not isinstance(limiar, (int, float)) or not 0 < limiar < 1:
            raise ConfigError(
                f"'{caminho}': [ouvido].limiar_ativacao = {limiar!r} nao e valido; tem de ser um "
                "numero entre 0 e 1 (exclusive)."
            )
        valores["limiar_ativacao"] = float(limiar)
    for chave, opcoes in permitidas.items():
        if chave not in tabela:
            continue
        valor = tabela[chave]
        if not isinstance(valor, str) or valor.strip().lower() not in opcoes:
            raise ConfigError(
                f"'{caminho}': [ouvido].{chave} = {valor!r} nao e valido; escolher de: "
                f"{', '.join(opcoes)}."
            )
        valores[chave] = valor.strip().lower()
    return ConfigOuvido(**valores)


def validar_url_local(url: object) -> str:
    """Devolve o URL normalizado se apontar para o Ollama NESTE computador.

    So http, host em `HOSTS_LOCAIS`, porta opcional e nada mais (sem
    utilizador, caminho, query ou fragmento). Levanta ValueError com o motivo.
    """
    from urllib.parse import urlsplit

    if not isinstance(url, str) or not url.strip():
        raise ValueError("o url tem de ser texto nao vazio")
    partes = urlsplit(url.strip())
    if partes.scheme != "http":
        raise ValueError(f"so http e aceite (veio '{partes.scheme}')")
    try:
        porta = partes.port
    except ValueError as erro:
        raise ValueError(f"porta invalida: {erro}") from erro
    host = (partes.hostname or "").lower()
    if host not in HOSTS_LOCAIS:
        raise ValueError(f"so este computador e aceite ({', '.join(HOSTS_LOCAIS)}), nao '{host}'")
    if partes.username or partes.password or partes.path not in ("", "/") or partes.query or partes.fragment:
        raise ValueError("o url so pode ter esquema, host e porta")
    host_no_url = f"[{host}]" if ":" in host else host
    return f"http://{host_no_url}" + (f":{porta}" if porta else "")


def _validar_interprete(bruto: dict, caminho: Path) -> ConfigInterprete:
    """A tabela [interprete], opcional: URL so local, nomes de modelo e limite."""
    if "interprete" not in bruto:
        return ConfigInterprete()
    tabela = bruto["interprete"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [interprete] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = ("url", "modelo", "modelo_alternativo", "limite_s", "carregamento_s", "confirmacao_s")
    desconhecidas = sorted(set(tabela) - set(permitidas))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [interprete] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)})."
        )
    valores: dict[str, object] = {}
    if "url" in tabela:
        try:
            valores["url"] = validar_url_local(tabela["url"])
        except ValueError as erro:
            raise ConfigError(f"'{caminho}': [interprete].url nao e valido: {erro}.") from erro
    for chave in ("modelo", "modelo_alternativo"):
        if chave not in tabela:
            continue
        valor = tabela[chave]
        if not isinstance(valor, str) or not _PADRAO_NOME_DE_MODELO.fullmatch(valor.strip().lower()):
            raise ConfigError(
                f"'{caminho}': [interprete].{chave} = {valor!r} nao e um nome de modelo do Ollama "
                "(por exemplo \"qwen3:8b\")."
            )
        valores[chave] = valor.strip().lower()
    if "limite_s" in tabela:
        limite = tabela["limite_s"]
        if (
            isinstance(limite, bool)
            or not isinstance(limite, (int, float))
            or not 0 < limite <= LIMITE_DO_INTERPRETE_S
        ):
            raise ConfigError(
                f"'{caminho}': [interprete].limite_s = {limite!r} nao e valido; tem de ser um "
                f"numero maior que 0 e no maximo {LIMITE_DO_INTERPRETE_S:g}."
            )
        valores["limite_s"] = float(limite)
    if "carregamento_s" in tabela:
        espera = tabela["carregamento_s"]
        minimo = float(valores.get("limite_s", LIMITE_DO_INTERPRETE_S))
        if (
            isinstance(espera, bool)
            or not isinstance(espera, (int, float))
            or not minimo <= espera <= ESPERA_DO_CARREGAMENTO_MAXIMA_S
        ):
            raise ConfigError(
                f"'{caminho}': [interprete].carregamento_s = {espera!r} nao e valido; tem de ser um "
                f"numero entre {minimo:g} (o limite_s) e {ESPERA_DO_CARREGAMENTO_MAXIMA_S:g}."
            )
        valores["carregamento_s"] = float(espera)
    if "confirmacao_s" in tabela:
        espera = tabela["confirmacao_s"]
        if (
            isinstance(espera, bool)
            or not isinstance(espera, (int, float))
            or not ESPERA_DA_CONFIRMACAO_MINIMA_S <= espera <= ESPERA_DA_CONFIRMACAO_MAXIMA_S
        ):
            raise ConfigError(
                f"'{caminho}': [interprete].confirmacao_s = {espera!r} nao e valido; tem de ser um "
                f"numero entre {ESPERA_DA_CONFIRMACAO_MINIMA_S:g} e {ESPERA_DA_CONFIRMACAO_MAXIMA_S:g}."
            )
        valores["confirmacao_s"] = float(espera)
    return ConfigInterprete(**valores)


def _validar_forja(bruto: dict, caminho: Path, validar_caminhos: bool) -> ConfigForja | None:
    """A tabela [forja], opcional: instalacao, perfil e provider, verificados no disco."""
    if "forja" not in bruto:
        return None
    tabela = bruto["forja"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [forja] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = ("caminho", "perfil", "provider")
    desconhecidas = sorted(set(tabela) - set(permitidas))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [forja] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)})."
        )
    valores: dict[str, Path] = {}
    for chave in ("caminho", "perfil"):
        valor = tabela.get(chave)
        if not isinstance(valor, str) or not valor.strip():
            raise ConfigError(f"'{caminho}': falta [forja].{chave} (texto nao vazio).")
        if "\x00" in valor or valor.strip().startswith("-"):
            raise ConfigError(f"'{caminho}': [forja].{chave} nao e um caminho valido.")
        resolvido = Path(valor.strip()).expanduser().resolve(strict=False)
        if not resolvido.is_absolute():
            raise ConfigError(f"'{caminho}': [forja].{chave} tem de ser um caminho absoluto.")
        valores[chave] = resolvido
    if valores["perfil"].suffix.lower() != ".json":
        raise ConfigError(f"'{caminho}': [forja].perfil tem de ser um ficheiro .json.")
    provider = tabela.get("provider", "claude")
    if not isinstance(provider, str) or provider.strip().lower() not in PROVIDERS_DA_FORJA:
        raise ConfigError(
            f"'{caminho}': [forja].provider = {provider!r} nao e valido; escolher de: "
            f"{', '.join(PROVIDERS_DA_FORJA)}."
        )
    forja = ConfigForja(valores["caminho"], valores["perfil"], provider.strip().lower())
    if validar_caminhos:
        if not forja.ponto_de_entrada.is_file():
            raise ConfigError(
                f"'{caminho}': [forja].caminho nao e uma instalacao da FORJA: falta "
                f"'{'/'.join(SUBCAMINHO_DA_FORJA)}' em '{forja.caminho}'."
            )
        if not forja.perfil.is_file():
            raise ConfigError(f"'{caminho}': [forja].perfil nao existe: '{forja.perfil}'.")
    return forja


def _validar_perguntas(bruto: dict, caminho: Path) -> ConfigPerguntas:
    """A tabela [perguntas], opcional: modelo do Claude Code, limite e localizacao."""
    if "perguntas" not in bruto:
        return ConfigPerguntas()
    tabela = bruto["perguntas"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [perguntas] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = ("modelo", "limite_s", "localizacao")
    desconhecidas = sorted(set(tabela) - set(permitidas))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [perguntas] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)})."
        )
    valores: dict[str, object] = {}
    if "modelo" in tabela:
        modelo = tabela["modelo"]
        if not isinstance(modelo, str) or not _PADRAO_MODELO_DO_CLAUDE.fullmatch(modelo.strip()):
            raise ConfigError(
                f"'{caminho}': [perguntas].modelo = {modelo!r} nao e um nome de modelo do Claude Code "
                f"(por exemplo \"{MODELO_DAS_PERGUNTAS}\" ou \"sonnet\")."
            )
        valores["modelo"] = modelo.strip()
    if "limite_s" in tabela:
        limite = tabela["limite_s"]
        if (
            isinstance(limite, bool)
            or not isinstance(limite, (int, float))
            or not LIMITE_DAS_PERGUNTAS_MINIMO_S <= limite <= LIMITE_DAS_PERGUNTAS_MAXIMO_S
        ):
            raise ConfigError(
                f"'{caminho}': [perguntas].limite_s = {limite!r} nao e valido; tem de ser um numero "
                f"entre {LIMITE_DAS_PERGUNTAS_MINIMO_S:g} e {LIMITE_DAS_PERGUNTAS_MAXIMO_S:g}."
            )
        valores["limite_s"] = float(limite)
    if "localizacao" in tabela:
        local = tabela["localizacao"]
        texto = " ".join(local.split()) if isinstance(local, str) else ""
        if (
            not isinstance(local, str)
            or not local.isprintable()
            or len(texto) > LOCALIZACAO_MAXIMA
            or not _PADRAO_LOCALIZACAO.fullmatch(texto)
        ):
            raise ConfigError(
                f"'{caminho}': [perguntas].localizacao nao e valida; tem de ser uma linha de texto "
                f"com ate {LOCALIZACAO_MAXIMA} caracteres, so letras, digitos, espacos e , . ' ( ) - "
                "(por exemplo \"Porto, Portugal\")."
            )
        valores["localizacao"] = texto
    return ConfigPerguntas(**valores)


def _validar_voz(bruto: dict, caminho: Path) -> ConfigVoz:
    """A tabela [voz], opcional: o nome da voz inglesa, so da lista fechada."""
    if "voz" not in bruto:
        return ConfigVoz()
    tabela = bruto["voz"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [voz] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = ("nome",)
    desconhecidas = sorted(set(tabela) - set(permitidas))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [voz] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)})."
        )
    if "nome" not in tabela:
        return ConfigVoz()
    nome = tabela["nome"]
    if not isinstance(nome, str) or nome.strip() not in VOZES_INGLESAS:
        raise ConfigError(
            f"'{caminho}': [voz].nome = {nome!r} nao e uma voz conhecida; tem de ser uma de "
            f"{', '.join(VOZES_INGLESAS)} (por omissao \"{VOZ_INGLESA_PADRAO}\")."
        )
    return ConfigVoz(nome=nome.strip())


def _validar_adaptacao(bruto: dict, caminho: Path) -> ConfigAdaptacao:
    """A tabela [adaptacao], opcional: dois interruptores e a forca do reforco."""
    if "adaptacao" not in bruto:
        return ConfigAdaptacao()
    tabela = bruto["adaptacao"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [adaptacao] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = ("reforco", "bonus", "lexico")
    desconhecidas = sorted(set(tabela) - set(permitidas))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [adaptacao] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)})."
        )
    valores: dict[str, object] = {}
    for chave in ("reforco", "lexico"):
        if chave in tabela:
            if not isinstance(tabela[chave], bool):
                raise ConfigError(
                    f"'{caminho}': [adaptacao].{chave} = {tabela[chave]!r} nao e valido; tem de ser true ou false."
                )
            valores[chave] = tabela[chave]
    if "bonus" in tabela:
        bonus = tabela["bonus"]
        if (
            isinstance(bonus, bool)
            or not isinstance(bonus, (int, float))
            or not 0 < bonus <= BONUS_DE_REFORCO_MAXIMO
        ):
            raise ConfigError(
                f"'{caminho}': [adaptacao].bonus = {bonus!r} nao e valido; tem de ser um numero maior "
                f"do que 0 e no maximo {BONUS_DE_REFORCO_MAXIMO:g} (por omissao {BONUS_DE_REFORCO_PADRAO:g})."
            )
        valores["bonus"] = float(bonus)
    return ConfigAdaptacao(**valores)


def _validar_escuta(bruto: dict, caminho: Path) -> ConfigEscuta:
    """A tabela [escuta], opcional: duracao da escuta de seguimento e os sons."""
    if "escuta" not in bruto:
        return ConfigEscuta()
    tabela = bruto["escuta"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [escuta] tem de ser uma tabela, nao {type(tabela).__name__}.")
    permitidas = ("seguimento_s", "sons", "volume")
    desconhecidas = sorted(set(tabela) - set(permitidas))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [escuta] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(permitidas)})."
        )
    valores: dict[str, object] = {}
    if "seguimento_s" in tabela:
        segundos = tabela["seguimento_s"]
        if (
            isinstance(segundos, bool)
            or not isinstance(segundos, (int, float))
            or not SEGUIMENTO_MINIMO_S <= segundos <= SEGUIMENTO_MAXIMO_S
        ):
            raise ConfigError(
                f"'{caminho}': [escuta].seguimento_s = {segundos!r} nao e valido; tem de ser um numero "
                f"de segundos entre {SEGUIMENTO_MINIMO_S:g} e {SEGUIMENTO_MAXIMO_S:g} "
                f"(por omissao {SEGUIMENTO_S:g})."
            )
        valores["seguimento_s"] = float(segundos)
    if "sons" in tabela:
        if not isinstance(tabela["sons"], bool):
            raise ConfigError(
                f"'{caminho}': [escuta].sons = {tabela['sons']!r} nao e valido; tem de ser true ou false."
            )
        valores["sons"] = tabela["sons"]
    if "volume" in tabela:
        volume = tabela["volume"]
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 < volume <= 1:
            raise ConfigError(
                f"'{caminho}': [escuta].volume = {volume!r} nao e valido; tem de ser um numero maior "
                f"do que 0 e no maximo 1 (por omissao {VOLUME_DOS_SONS_PADRAO:g})."
            )
        valores["volume"] = float(volume)
    return ConfigEscuta(**valores)


def _validar_memoria(bruto: dict, caminho: Path) -> ConfigMemoria:
    """A tabela [memoria], opcional: limites que so podem baixar dos tetos fixos."""
    if "memoria" not in bruto:
        return ConfigMemoria()
    tabela = bruto["memoria"]
    if not isinstance(tabela, dict):
        raise ConfigError(f"'{caminho}': [memoria] tem de ser uma tabela, nao {type(tabela).__name__}.")
    limites: dict[str, tuple[float, float, bool]] = {
        # chave: (minimo, maximo, so inteiros)
        "trocas": (1, TROCAS_MAXIMAS, True),
        "expira_min": (1, EXPIRA_MAXIMO_MIN, False),
        "factos": (1, FACTOS_MAXIMOS, True),
        "caracteres": (CARACTERES_DOS_FACTOS_MINIMOS, CARACTERES_DOS_FACTOS_MAXIMOS, True),
        "frases_interprete": (FRASES_DO_INTERPRETE_MINIMAS, FRASES_DO_INTERPRETE_MAXIMAS, True),
    }
    desconhecidas = sorted(set(tabela) - set(limites))
    if desconhecidas:
        raise ConfigError(
            f"'{caminho}': [memoria] tem chaves desconhecidas: {', '.join(desconhecidas)} "
            f"(so {', '.join(limites)})."
        )
    omissao = ConfigMemoria()
    valores: dict[str, object] = {}
    for chave, (minimo, maximo, inteiro) in limites.items():
        if chave not in tabela:
            continue
        valor = tabela[chave]
        tipos = (int,) if inteiro else (int, float)
        if isinstance(valor, bool) or not isinstance(valor, tipos) or not minimo <= valor <= maximo:
            tipo = "um numero inteiro" if inteiro else "um numero"
            raise ConfigError(
                f"'{caminho}': [memoria].{chave} = {valor!r} nao e valido; tem de ser {tipo} "
                f"entre {minimo:g} e {maximo:g} (por omissao {getattr(omissao, chave):g}; "
                "o teto nao se pode subir)."
            )
        valores[chave] = valor if inteiro else float(valor)
    return ConfigMemoria(**valores)


def _resolver_caminho_do_projeto(nome: str, valor: str, caminho_config: Path) -> Path:
    """Resolve e valida o caminho de um projeto no disco.

    Um caminho vindo do ficheiro de configuracao e entrada externa: resolve-se
    e confirma-se que existe e que e mesmo uma pasta, em vez de o aceitar em
    bruto. Levanta ConfigError com uma mensagem legivel se falhar.
    """
    bruto = Path(valor).expanduser()
    try:
        resolvido = bruto.resolve(strict=False)
    except OSError as erro:
        raise ConfigError(
            f"'{caminho_config}': o caminho do projeto '{nome}' ('{valor}') nao pode ser "
            f"resolvido: {erro}"
        ) from erro
    if not resolvido.exists():
        raise ConfigError(
            f"'{caminho_config}': o caminho do projeto '{nome}' nao existe no disco: "
            f"'{resolvido}'. Corrige o valor em [[projetos]] ou cria a pasta."
        )
    if not resolvido.is_dir():
        raise ConfigError(
            f"'{caminho_config}': o caminho do projeto '{nome}' existe mas nao e uma pasta: "
            f"'{resolvido}'."
        )
    return resolvido


def carregar_config(
    caminho: str | Path = CAMINHO_CONFIG_PADRAO,
    *,
    validar_caminhos: bool = True,
) -> Config:
    """Le, valida e devolve a configuracao privada.

    Levanta ConfigError (nunca uma excecao de baixo nivel do tomllib ou do
    sistema de ficheiros) com uma mensagem que diz exatamente o que corrigir.

    `validar_caminhos=False` salta so a verificacao de que o caminho de CADA
    projeto existe no disco e e uma pasta (`_resolver_caminho_do_projeto`,
    D50.7); tudo o resto (TOML, [microfone], [[projetos]], duplicados)
    continua a validar-se sempre. O default e True e e o que o router usa: a
    unica excecao aceite e o modo `--simular` de `jarvis/acoes_locais.py`
    contra o `config.exemplo.toml` VERSIONADO, cujos caminhos
    ("D:/caminho/para/...") sao ficticios de proposito e nunca existem em
    disco nenhum — sem esta valvula essa pre-visualizacao nem carregava.
    Uma accao que executa mesmo alguma coisa NUNCA passa False aqui.
    """
    caminho = Path(caminho)
    if not caminho.is_file():
        raise _erro_ficheiro_em_falta(caminho)

    try:
        bruto = tomllib.loads(caminho.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as erro:
        raise ConfigError(f"'{caminho}': TOML invalido: {erro}") from erro
    except UnicodeDecodeError as erro:
        raise ConfigError(f"'{caminho}': o ficheiro nao esta em UTF-8: {erro}") from erro

    _validar_tabela(bruto, caminho)

    nomes_vistos: set[str] = set()
    projetos: list[Projeto] = []
    for indice, item in enumerate(bruto["projetos"]):
        nome, valor_caminho = _validar_projeto(item, indice, caminho)
        chave = nome.casefold()
        if chave in nomes_vistos:
            raise ConfigError(f"'{caminho}': o projeto '{nome}' esta duplicado em [[projetos]].")
        nomes_vistos.add(chave)
        if validar_caminhos:
            resolvido = _resolver_caminho_do_projeto(nome, valor_caminho, caminho)
        else:
            resolvido = Path(valor_caminho).expanduser().resolve(strict=False)
        projetos.append(Projeto(nome=nome, caminho=resolvido))

    return Config(
        microfone=bruto["microfone"]["nome"].strip(),
        projetos=tuple(projetos),
        ouvido=_validar_ouvido(bruto, caminho),
        interprete=_validar_interprete(bruto, caminho),
        forja=_validar_forja(bruto, caminho, validar_caminhos),
        perguntas=_validar_perguntas(bruto, caminho),
        voz=_validar_voz(bruto, caminho),
        adaptacao=_validar_adaptacao(bruto, caminho),
        escuta=_validar_escuta(bruto, caminho),
        memoria=_validar_memoria(bruto, caminho),
    )


# --- Autoteste das partes puras (ficheiros temporarios, nunca config.toml) --


def _autoteste() -> int:
    """Verifica a validacao e a leitura, sempre com ficheiros temporarios."""
    import tempfile

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    def apanhar(funcao) -> str:
        try:
            funcao()
        except ConfigError as erro:
            return str(erro)
        return ""

    # 1. ficheiro em falta.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "nao-existe.toml"
        verificar(
            "ficheiro em falta: mensagem legivel",
            "nao encontrada" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 2. TOML invalido.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text("isto nao e = toml valido [[[", encoding="utf-8")
        verificar(
            "toml invalido: mensagem legivel",
            "TOML invalido" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 3. falta [microfone].
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text('[[projetos]]\nnome = "x"\ncaminho = "."\n', encoding="utf-8")
        verificar(
            "sem [microfone]: mensagem legivel",
            "[microfone]" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 4. falta [[projetos]].
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text('[microfone]\nnome = "Microfone"\n', encoding="utf-8")
        verificar(
            "sem [[projetos]]: mensagem legivel",
            "[[projetos]]" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 5. projeto com caminho que nao existe no disco.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            '[[projetos]]\nnome = "fantasma"\ncaminho = "'
            + (Path(pasta) / "nao-existe-mesmo").as_posix()
            + '"\n',
            encoding="utf-8",
        )
        verificar(
            "caminho inexistente: recusado com mensagem legivel",
            "nao existe no disco" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 6. projeto cujo caminho existe mas e um ficheiro, nao uma pasta.
    with tempfile.TemporaryDirectory() as pasta:
        ficheiro = Path(pasta) / "isto-e-um-ficheiro.txt"
        ficheiro.write_text("x", encoding="utf-8")
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            '[[projetos]]\nnome = "ficheiro"\ncaminho = "' + ficheiro.as_posix() + '"\n',
            encoding="utf-8",
        )
        verificar(
            "caminho que e ficheiro: recusado",
            "nao e uma pasta" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 7. projetos duplicados.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            f'[[projetos]]\nnome = "dup"\ncaminho = "{Path(pasta).as_posix()}"\n\n'
            f'[[projetos]]\nnome = "DUP"\ncaminho = "{Path(pasta).as_posix()}"\n',
            encoding="utf-8",
        )
        verificar(
            "projeto duplicado (case-insensitive): recusado",
            "duplicado" in apanhar(lambda: carregar_config(caminho)),
            True,
        )

    # 8. configuracao valida, round-trip completo.
    with tempfile.TemporaryDirectory() as pasta:
        projeto_a = Path(pasta) / "projeto-a"
        projeto_a.mkdir()
        caminho = Path(pasta) / "config.toml"
        caminho.write_text(
            '[microfone]\nnome = "Microfone de Teste"\n\n'
            f'[[projetos]]\nnome = "projeto-a"\ncaminho = "{projeto_a.as_posix()}"\n',
            encoding="utf-8",
        )
        config = carregar_config(caminho)
        verificar("config valida: microfone", config.microfone, "Microfone de Teste")
        verificar("config valida: um projeto", len(config.projetos), 1)
        verificar(
            "config valida: caminho resolvido",
            config.projetos[0].caminho,
            projeto_a.resolve(),
        )
        verificar(
            "config valida: encontrar_projeto exato",
            config.encontrar_projeto("projeto-a") is not None,
            True,
        )
        verificar(
            "config valida: encontrar_projeto case-insensitive",
            config.encontrar_projeto("PROJETO-A") is not None,
            True,
        )
        verificar(
            "config valida: encontrar_projeto desconhecido devolve None",
            config.encontrar_projeto("nao-existe"),
            None,
        )

    # 9. validar_caminhos=False: um caminho ficticio, tipo o do
    # config.exemplo.toml, continua recusado por omissao mas carrega com a
    # valvula explicita — e o resto da validacao (TOML, [microfone],
    # [[projetos]]) continua a valer.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        fantasma = (Path(pasta) / "fantasma-so-para-este-teste").as_posix()
        caminho.write_text(
            '[microfone]\nnome = "Microfone"\n\n'
            f'[[projetos]]\nnome = "ficticio"\ncaminho = "{fantasma}"\n',
            encoding="utf-8",
        )
        verificar(
            "validar_caminhos=True (default): caminho ficticio continua recusado",
            "nao existe no disco" in apanhar(lambda: carregar_config(caminho)),
            True,
        )
        config_sem_validar = carregar_config(caminho, validar_caminhos=False)
        verificar(
            "validar_caminhos=False: carrega o mesmo caminho sem tocar no disco",
            config_sem_validar.projetos[0].caminho,
            Path(fantasma).resolve(strict=False),
        )

    # 10. [escuta]: sem a tabela valem os valores por omissao; valores fora do
    # intervalo, tipos errados e chaves desconhecidas sao recusados.
    with tempfile.TemporaryDirectory() as pasta:
        caminho = Path(pasta) / "config.toml"
        base = '[microfone]\nnome = "Microfone"\n\n[[projetos]]\nnome = "x"\ncaminho = "."\n\n'
        caminho.write_text(base, encoding="utf-8")
        verificar(
            "sem [escuta]: valores por omissao",
            carregar_config(caminho, validar_caminhos=False).escuta,
            ConfigEscuta(),
        )
        caminho.write_text(base + "[escuta]\nseguimento_s = 5\nsons = false\nvolume = 0.1\n", encoding="utf-8")
        verificar(
            "[escuta] valida: lida",
            carregar_config(caminho, validar_caminhos=False).escuta,
            ConfigEscuta(seguimento_s=5.0, sons=False, volume=0.1),
        )
        for texto in ("seguimento_s = 1", "seguimento_s = 31", 'sons = "sim"', "volume = 0", "volume = 1.5", "outra = 1"):
            caminho.write_text(base + "[escuta]\n" + texto + "\n", encoding="utf-8")
            verificar(
                f"[escuta] {texto}: recusado",
                "[escuta]" in apanhar(lambda: carregar_config(caminho, validar_caminhos=False)),
                True,
            )

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste do config completo (validacao, caminhos, round-trip).")
    return 0


if __name__ == "__main__":
    if "--autoteste" in sys.argv[1:]:
        sys.exit(_autoteste())
    print(__doc__)
    print("Autoteste: python -m jarvis.config --autoteste")
