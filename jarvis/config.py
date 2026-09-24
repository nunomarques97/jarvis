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


class ConfigError(Exception):
    """Configuracao em falta, mal formada, ou com um caminho que nao existe."""


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

    return Config(microfone=bruto["microfone"]["nome"].strip(), projetos=tuple(projetos))


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
