r"""Descoberta dos projetos: os repositorios git nas pastas de [descoberta].

Um projeto descoberto e uma pasta filha DIRETA de uma raiz que tem `.git`
(pasta ou ficheiro, como num worktree). Nada mais fundo e procurado. A pasta
fica de fora, com uma linha no log, quando:

  - nao tem `.git`;
  - e uma juncao ou atalho simbolico cujo caminho resolvido sai da raiz;
  - o nome nao passa a validacao dos nomes de projeto do canal
    (`jarvis.canal_mcp.validar_nome_de_projeto`);
  - o nome ja existe (sem distinguir maiusculas, ou escrito de outra forma
    que o router le igual, como "o-meu-repo" e "o_meu_repo"): os projetos de
    [[projetos]] ganham sempre, e entre raizes ganha a primeira.

Uma raiz que nao existe nao e erro: fica uma linha no log e a descoberta
segue. Sem a tabela [descoberta] a raiz e `Desktop/Repositorios` dentro da
pasta pessoal do utilizador; `pastas = []` desliga a descoberta.

Os projetos descobertos juntam-se a `config.projetos` no arranque
(`com_projetos_descobertos`), por isso o router, o interprete, a confirmacao
e o reforco de frases da adaptacao tratam-nos como os configurados. A ordem
final poe primeiro os projetos com atividade git mais recente: e a ordem
inicial dos "projetos usados ha pouco" que a pergunta "Which project?" diz.

Este modulo so le o disco (listar pastas e ver datas); nunca executa nada.

Autoteste com uma arvore temporaria (nunca a pasta real):

    .venv\Scripts\python -m jarvis.projetos --autoteste
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from jarvis.canal_mcp import validar_nome_de_projeto
from jarvis.config import Config, Projeto
from jarvis.router import _normalizar

Registar = Callable[[str], object]

#: A raiz por omissao, relativa a pasta pessoal do utilizador.
SUBPASTA_PADRAO = ("Desktop", "Repositorios")

#: Ficheiros dentro de `.git` cuja data diz quando o repositorio foi usado:
#: o historico do HEAD muda a cada commit ou checkout, o indice a cada add
#: ou status.
_SINAIS_DE_ATIVIDADE = (("logs", "HEAD"), ("index",), ("HEAD",))


def raiz_padrao(casa: Path | None = None) -> Path:
    """`Desktop/Repositorios` dentro da pasta pessoal (ou de `casa`)."""
    return (casa if casa is not None else Path.home()).joinpath(*SUBPASTA_PADRAO)


def raizes_da_descoberta(config: Config, casa: Path | None = None) -> tuple[Path, ...]:
    """As raizes a procurar: as de [descoberta], ou a raiz por omissao sem a tabela."""
    pastas = config.descoberta.pastas
    if pastas is None:
        return (raiz_padrao(casa),)
    return tuple(pastas)


def _dentro_de(caminho: Path, raiz: Path) -> bool:
    try:
        caminho.relative_to(raiz)
    except ValueError:
        return False
    return caminho != raiz


def _chave(nome: str) -> str:
    """Como o router compara nomes: sem maiusculas, acentos nem pontuacao."""
    return _normalizar(nome) or nome.casefold()


def descobrir_projetos(
    raizes: Iterable[Path],
    *,
    ja_conhecidos: Iterable[str] = (),
    registar: Registar = print,
) -> tuple[Projeto, ...]:
    """Os repositorios git filhos diretos das raizes, por ordem de nome.

    `ja_conhecidos` sao os nomes que ganham (os de [[projetos]]). Nunca
    levanta: uma raiz ou uma pasta que nao se consegue ler fica no log.
    """
    vistos = {_chave(nome) for nome in ja_conhecidos}
    caminhos_vistos: set[Path] = set()
    encontrados: list[Projeto] = []
    for raiz in raizes:
        try:
            raiz_resolvida = Path(raiz).expanduser().resolve(strict=False)
        except OSError as erro:
            registar(f"descoberta | raiz '{raiz}' ignorada: nao se resolve ({erro})")
            continue
        try:
            e_pasta = raiz_resolvida.is_dir()
        except OSError as erro:
            registar(f"descoberta | raiz '{raiz_resolvida}' ignorada: sem acesso ({erro})")
            continue
        if not e_pasta:
            registar(f"descoberta | raiz '{raiz_resolvida}' nao existe; nada a descobrir nela")
            continue
        try:
            with os.scandir(raiz_resolvida) as entradas:
                filhos = sorted(entradas, key=lambda entrada: entrada.name.casefold())
        except OSError as erro:
            registar(f"descoberta | raiz '{raiz_resolvida}' por ler ({erro})")
            continue
        for entrada in filhos:
            nome = entrada.name
            try:
                if not entrada.is_dir():
                    continue
            except OSError as erro:
                registar(f"descoberta | pasta '{nome}' ignorada: sem acesso ({erro})")
                continue
            try:
                validar_nome_de_projeto(nome)
            except ValueError:
                registar(f"descoberta | pasta {nome!r} ignorada: nome de projeto invalido")
                continue
            try:
                resolvido = Path(entrada.path).resolve(strict=True)
            except OSError as erro:
                registar(f"descoberta | pasta '{nome}' ignorada: nao se resolve ({erro})")
                continue
            if not _dentro_de(resolvido, raiz_resolvida):
                registar(f"descoberta | pasta '{nome}' ignorada: aponta para fora da raiz")
                continue
            try:
                tem_git = (resolvido / ".git").exists()
            except OSError as erro:
                registar(f"descoberta | pasta '{nome}' ignorada: sem acesso ({erro})")
                continue
            if not tem_git:
                registar(f"descoberta | pasta '{nome}' ignorada: nao e um repositorio git (sem .git)")
                continue
            chave = _chave(nome)
            if chave in vistos:
                registar(f"descoberta | pasta '{nome}' ignorada: ja ha um projeto com esse nome")
                continue
            if resolvido in caminhos_vistos:
                registar(f"descoberta | pasta '{nome}' ignorada: mesma pasta de outro projeto descoberto")
                continue
            vistos.add(chave)
            caminhos_vistos.add(resolvido)
            encontrados.append(Projeto(nome=nome, caminho=resolvido))
    return tuple(encontrados)


def atividade(projeto: Projeto) -> float:
    """Quando o repositorio foi usado pela ultima vez (0 se nao se sabe)."""
    git = projeto.caminho / ".git"
    mais_recente = 0.0
    for partes in _SINAIS_DE_ATIVIDADE:
        try:
            mais_recente = max(mais_recente, git.joinpath(*partes).stat().st_mtime)
        except OSError:
            continue
    if not mais_recente:
        try:
            mais_recente = git.stat().st_mtime
        except OSError:
            pass
    return mais_recente


def por_atividade(projetos: Iterable[Projeto]) -> tuple[Projeto, ...]:
    """Os projetos com uso git mais recente primeiro; empates ficam na ordem dada."""
    lista = list(projetos)
    datas = [atividade(projeto) for projeto in lista]
    ordem = sorted(range(len(lista)), key=lambda indice: -datas[indice])
    return tuple(lista[indice] for indice in ordem)


def com_projetos_descobertos(
    config: Config,
    *,
    registar: Registar = print,
    casa: Path | None = None,
    relogio: Callable[[], float] = time.perf_counter,
) -> Config:
    """A configuracao com os projetos descobertos juntos aos de [[projetos]].

    Os configurados ganham sempre a um descoberto com o mesmo nome. A lista
    final vem ordenada pela atividade git mais recente.
    """
    raizes = raizes_da_descoberta(config, casa)
    if not raizes:
        registar("descoberta | desligada (nenhuma pasta a procurar)")
        return config
    inicio = relogio()
    descobertos = descobrir_projetos(
        raizes, ja_conhecidos=(projeto.nome for projeto in config.projetos), registar=registar
    )
    projetos = por_atividade((*config.projetos, *descobertos))
    ms = (relogio() - inicio) * 1000
    nomes = ", ".join(projeto.nome for projeto in descobertos) or "nenhum"
    registar(
        f"descoberta | {len(descobertos)} projeto(s) descoberto(s) em {len(raizes)} raiz(es), "
        f"{ms:.0f} ms: {nomes}"
    )
    return replace(config, projetos=projetos)


# --- Autoteste (arvore temporaria, nunca a pasta real) -------------------------


def _autoteste() -> int:
    import tempfile

    falhas: list[str] = []

    def verificar(nome: str, obtido: object, esperado: object) -> None:
        if obtido != esperado:
            falhas.append(f"{nome}: esperado {esperado!r}, obtido {obtido!r}")
        else:
            print(f"ok   {nome}")

    with tempfile.TemporaryDirectory() as pasta:
        raiz = Path(pasta) / "Repositorios"
        (raiz / "alfa" / ".git").mkdir(parents=True)
        (raiz / "sem-git").mkdir()
        (raiz / "alfa" / "fundo" / ".git").mkdir(parents=True)
        linhas: list[str] = []
        achados = descobrir_projetos([raiz, Path(pasta) / "nao-existe"], registar=linhas.append)
        verificar("so os filhos diretos com .git", [p.nome for p in achados], ["alfa"])
        verificar("pasta sem .git no log", any("sem-git" in linha for linha in linhas), True)
        verificar("raiz em falta no log", any("nao existe" in linha for linha in linhas), True)

    print()
    if falhas:
        print(f"FALHOU: {len(falhas)} verificacao(oes)")
        for falha in falhas:
            print(" - " + falha)
        return 1
    print("OK: autoteste da descoberta de projetos.")
    return 0


if __name__ == "__main__":
    if "--autoteste" in sys.argv[1:]:
        sys.exit(_autoteste())
    print(__doc__)
