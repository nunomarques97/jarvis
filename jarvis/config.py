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
    confirmacao_s = 20.0          # espera pelo "sim"; depois cancela sem enviar

    [forja]                       # opcional; sem ela nao ha runs FORJA por voz
    caminho = "D:/caminho/para/forja"         # instalacao (tem bin/forja.mjs)
    perfil = "D:/caminho/para/perfil.json"    # --config dos runs lancados
    provider = "claude"                       # claude ou codex

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

#: Quanto tempo o jarvis espera pelo "sim" depois de recapitular um pedido.
#: Sem resposta dentro deste tempo o pedido e cancelado sem ser enviado.
ESPERA_DA_CONFIRMACAO_S = 20.0
ESPERA_DA_CONFIRMACAO_MINIMA_S = 3.0
ESPERA_DA_CONFIRMACAO_MAXIMA_S = 120.0

#: Providers que um run FORJA lancado por voz pode usar (lista fechada).
PROVIDERS_DA_FORJA = ("claude", "codex")

#: O ponto de entrada da FORJA dentro da pasta de instalacao.
SUBCAMINHO_DA_FORJA = ("bin", "forja.mjs")

#: Nome de modelo do Ollama ("familia:etiqueta"), sem espacos nem caminhos.
_PADRAO_NOME_DE_MODELO = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}(?::[a-z0-9][a-z0-9._-]{0,63})?")


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
    permitidas = ("url", "modelo", "modelo_alternativo", "limite_s", "confirmacao_s")
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
