r"""Testes da descoberta dos projetos (jarvis/projetos.py) e da tabela [descoberta].

Tudo corre numa arvore temporaria: nunca se le a pasta pessoal real. As
juncoes do Windows sao criadas com `_winapi.CreateJunction` (sem privilegios);
noutro sistema usa-se um atalho simbolico, e o teste salta se nao der.

Corre com:

    .venv\Scripts\python -m unittest tests.test_descoberta -v
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from jarvis.adaptacao import frases_de_reforco
from jarvis.config import (
    Config,
    ConfigAdaptacao,
    ConfigDescoberta,
    ConfigError,
    ConfigOuvido,
    Projeto,
    carregar_config,
)
from jarvis.interprete import projetos_mencionados
from jarvis.projetos import (
    com_projetos_descobertos,
    descobrir_projetos,
    por_atividade,
    raiz_padrao,
    raizes_da_descoberta,
)
from jarvis.router import encaminhar

RAIZ_DO_REPO = Path(__file__).resolve().parent.parent


def _repo(pasta: Path, *, ficheiro_git: bool = False) -> Path:
    """Uma pasta com `.git` (pasta, ou ficheiro como num worktree)."""
    pasta.mkdir(parents=True, exist_ok=True)
    if ficheiro_git:
        (pasta / ".git").write_text("gitdir: D:/caminho/para/outro\n", encoding="utf-8")
    else:
        (pasta / ".git").mkdir(exist_ok=True)
    return pasta


def _ligar(ligacao: Path, alvo: Path) -> None:
    """Uma juncao (Windows) ou um atalho simbolico de `ligacao` para `alvo`."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(alvo), str(ligacao))
    else:
        os.symlink(alvo, ligacao, target_is_directory=True)


class _ComArvore(unittest.TestCase):
    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.base = Path(pasta.name).resolve()
        self.raiz = self.base / "Repositorios"
        self.raiz.mkdir()
        self.linhas: list[str] = []

    def descobrir(self, *raizes: Path, conhecidos=()) -> list[str]:
        achados = descobrir_projetos(raizes or (self.raiz,), ja_conhecidos=conhecidos, registar=self.linhas.append)
        return [projeto.nome for projeto in achados]

    def linhas_com(self, texto: str) -> list[str]:
        return [linha for linha in self.linhas if texto in linha]


class TestDescoberta(_ComArvore):
    def test_so_filhos_diretos_com_git(self) -> None:
        _repo(self.raiz / "nimbus")
        _repo(self.raiz / "kanban-lite", ficheiro_git=True)
        (self.raiz / "notas").mkdir()
        (self.raiz / "leia-me.txt").write_text("x", encoding="utf-8")
        _repo(self.raiz / "notas" / "neto")
        self.assertEqual(self.descobrir(), ["kanban-lite", "nimbus"])
        self.assertEqual(len(self.linhas_com("'notas' ignorada")), 1)
        self.assertEqual(self.linhas_com("neto"), [], "so os filhos diretos sao vistos")

    def test_caminho_do_projeto_e_o_resolvido(self) -> None:
        _repo(self.raiz / "nimbus")
        achados = descobrir_projetos([self.raiz], registar=self.linhas.append)
        self.assertEqual(achados, (Projeto("nimbus", (self.raiz / "nimbus").resolve()),))

    def test_nome_invalido_fica_de_fora_com_uma_linha(self) -> None:
        for nome in ("_privado", "-opcao", "com$dinheiro", "x" * 70, "a;b"):
            _repo(self.raiz / nome)
        _repo(self.raiz / "nimbus")
        self.assertEqual(self.descobrir(), ["nimbus"])
        self.assertEqual(len(self.linhas_com("nome de projeto invalido")), 5)

    def test_raiz_em_falta_nao_e_erro(self) -> None:
        _repo(self.raiz / "nimbus")
        self.assertEqual(self.descobrir(self.base / "nao-existe", self.raiz), ["nimbus"])
        self.assertEqual(len(self.linhas_com("nao existe")), 1)

    def test_pasta_sem_acesso_fica_de_fora_e_as_outras_seguem(self) -> None:
        _repo(self.raiz / "nimbus")
        _repo(self.raiz / "trancada")
        original = Path.exists

        def exists(caminho: Path, *args, **kwargs) -> bool:
            if caminho.parent.name == "trancada":
                raise PermissionError(13, "Access is denied", str(caminho))
            return original(caminho, *args, **kwargs)

        with mock.patch.object(Path, "exists", exists):
            self.assertEqual(self.descobrir(), ["nimbus"])
        self.assertEqual(len(self.linhas_com("'trancada' ignorada: sem acesso")), 1)

    def test_raiz_sem_acesso_fica_de_fora_e_as_outras_seguem(self) -> None:
        _repo(self.raiz / "nimbus")
        trancada = self.base / "trancada"
        trancada.mkdir()
        original = Path.is_dir

        def is_dir(caminho: Path, *args, **kwargs) -> bool:
            if caminho.name == "trancada":
                raise PermissionError(13, "Access is denied", str(caminho))
            return original(caminho, *args, **kwargs)

        with mock.patch.object(Path, "is_dir", is_dir):
            self.assertEqual(self.descobrir(trancada, self.raiz), ["nimbus"])
        self.assertEqual(len(self.linhas_com("ignorada: sem acesso")), 1)

    def test_entrada_que_nao_se_consegue_ver_fica_de_fora(self) -> None:
        _repo(self.raiz / "nimbus")
        _repo(self.raiz / "trancada")
        original = os.scandir

        class _Entrada:
            def __init__(self, entrada) -> None:
                self._entrada = entrada
                self.name = entrada.name
                self.path = entrada.path

            def is_dir(self) -> bool:
                if self.name == "trancada":
                    raise PermissionError(13, "Access is denied", self.path)
                return self._entrada.is_dir()

        class _Listagem:
            def __init__(self, caminho) -> None:
                self._iterador = original(caminho)

            def __enter__(self):
                return (_Entrada(entrada) for entrada in self._iterador)

            def __exit__(self, *_erro) -> None:
                self._iterador.close()

        with mock.patch("jarvis.projetos.os.scandir", _Listagem):
            self.assertEqual(self.descobrir(), ["nimbus"])
        self.assertEqual(len(self.linhas_com("'trancada' ignorada: sem acesso")), 1)

    def test_juncao_que_sai_da_raiz_fica_de_fora(self) -> None:
        fora = _repo(self.base / "fora" / "segredo")
        try:
            _ligar(self.raiz / "atalho", fora)
        except (OSError, NotImplementedError) as erro:
            self.skipTest(f"sem juncoes neste sistema: {erro}")
        _repo(self.raiz / "nimbus")
        self.assertEqual(self.descobrir(), ["nimbus"])
        self.assertEqual(len(self.linhas_com("'atalho' ignorada: aponta para fora da raiz")), 1)

    def test_juncao_dentro_da_raiz_nao_duplica_a_pasta(self) -> None:
        nimbus = _repo(self.raiz / "nimbus")
        try:
            _ligar(self.raiz / "outro-nome", nimbus)
        except (OSError, NotImplementedError) as erro:
            self.skipTest(f"sem juncoes neste sistema: {erro}")
        self.assertEqual(self.descobrir(), ["nimbus"])
        self.assertEqual(len(self.linhas_com("'outro-nome' ignorada")), 1)

    def test_nome_ja_conhecido_ou_igual_para_o_router_fica_de_fora(self) -> None:
        _repo(self.raiz / "Atlas")
        _repo(self.raiz / "o_meu_repo")
        _repo(self.raiz / "nimbus")
        self.assertEqual(self.descobrir(conhecidos=("atlas", "o-meu-repo")), ["nimbus"])
        self.assertEqual(len(self.linhas_com("ja ha um projeto com esse nome")), 2)

    def test_mesmo_nome_em_duas_raizes_ganha_a_primeira(self) -> None:
        segunda = self.base / "Outros"
        _repo(self.raiz / "nimbus")
        _repo(segunda / "nimbus")
        achados = descobrir_projetos([self.raiz, segunda], registar=self.linhas.append)
        self.assertEqual(achados, (Projeto("nimbus", (self.raiz / "nimbus").resolve()),))

    def test_cinquenta_pastas_em_menos_de_200_ms(self) -> None:
        for numero in range(50):
            _repo(self.raiz / f"repo-{numero:02d}")
        config = Config(
            microfone="x",
            projetos=(Projeto("atlas", self.base),),
            descoberta=ConfigDescoberta(pastas=(self.raiz,)),
        )
        # A carga da maquina so acrescenta tempo: conta a melhor de tres medicoes.
        tempos = []
        for _ in range(3):
            inicio = time.perf_counter()
            junta = com_projetos_descobertos(config, registar=self.linhas.append)
            tempos.append(time.perf_counter() - inicio)
        demorou = min(tempos)
        self.assertLess(demorou, 0.2, f"a descoberta demorou {demorou * 1000:.0f} ms")
        self.assertEqual(len(junta.projetos), 51)


class TestJuntarAConfig(_ComArvore):
    def config(self, pastas, projetos=None) -> Config:
        return Config(
            microfone="x",
            projetos=projetos if projetos is not None else (Projeto("atlas", self.base / "atlas-configurado"),),
            ouvido=ConfigOuvido(lingua="en"),
            descoberta=ConfigDescoberta(pastas=pastas),
        )

    def test_configurado_ganha_ao_descoberto_com_o_mesmo_nome(self) -> None:
        _repo(self.raiz / "atlas")
        _repo(self.raiz / "nimbus")
        junta = com_projetos_descobertos(self.config((self.raiz,)), registar=self.linhas.append)
        self.assertEqual(sorted(p.nome for p in junta.projetos), ["atlas", "nimbus"])
        self.assertEqual(junta.encontrar_projeto("atlas").caminho, self.base / "atlas-configurado")
        self.assertEqual(len(self.linhas_com("1 projeto(s) descoberto(s)")), 1)

    def test_lista_vazia_desliga_a_descoberta(self) -> None:
        _repo(self.raiz / "nimbus")
        config = self.config(())
        self.assertIs(com_projetos_descobertos(config, registar=self.linhas.append), config)
        self.assertEqual(len(self.linhas_com("desligada")), 1)

    def test_sem_a_tabela_procura_em_desktop_repositorios_da_pasta_pessoal(self) -> None:
        casa = self.base / "casa"
        _repo(casa / "Desktop" / "Repositorios" / "nimbus")
        config = self.config(None)
        self.assertEqual(raizes_da_descoberta(config, casa), (casa / "Desktop" / "Repositorios",))
        self.assertEqual(raiz_padrao(casa), casa / "Desktop" / "Repositorios")
        junta = com_projetos_descobertos(config, registar=self.linhas.append, casa=casa)
        self.assertIn("nimbus", [p.nome for p in junta.projetos])

    def test_sem_a_tabela_e_sem_a_pasta_nao_ha_erro(self) -> None:
        junta = com_projetos_descobertos(self.config(None), registar=self.linhas.append, casa=self.base / "vazia")
        self.assertEqual([p.nome for p in junta.projetos], ["atlas"])

    def test_ordem_pela_atividade_git_mais_recente(self) -> None:
        antigo = _repo(self.raiz / "antigo")
        recente = _repo(self.raiz / "recente")
        agora = time.time()
        for repo, idade in ((antigo, 3600), (recente, 60)):
            (repo / ".git" / "index").write_text("x", encoding="utf-8")
            os.utime(repo / ".git" / "index", (agora - idade, agora - idade))
        ordenados = por_atividade(
            [Projeto("antigo", antigo), Projeto("recente", recente), Projeto("sem-git", self.base)]
        )
        self.assertEqual([p.nome for p in ordenados], ["recente", "antigo", "sem-git"])

    def test_descoberto_segue_as_regras_do_router_e_do_som(self) -> None:
        _repo(self.raiz / "chamora")
        _repo(self.raiz / "kanban-lite")
        junta = com_projetos_descobertos(self.config((self.raiz,)), registar=self.linhas.append)
        caminho = str((self.raiz / "chamora").resolve())
        for frase in ("open vs code in chamora", "abre a pasta do chamorra", "open the kanban lite folder"):
            with self.subTest(frase=frase):
                resultado = encaminhar(frase, junta)
                self.assertEqual(resultado.tipo, "local")
        self.assertEqual(encaminhar("open vs code in chamora", junta).argumento, caminho)
        nomes = tuple(p.nome for p in junta.projetos)
        for ouvido in ("Shamura, list the tests.", "The project is Chamara."):
            with self.subTest(ouvido=ouvido):
                self.assertEqual(projetos_mencionados(ouvido, nomes), ("chamora",))

    def test_descoberto_entra_no_reforco_de_frases(self) -> None:
        _repo(self.raiz / "nimbus")
        config = Config(
            microfone="x",
            projetos=(Projeto("atlas", self.base),),
            adaptacao=ConfigAdaptacao(reforco=True),
            descoberta=ConfigDescoberta(pastas=(self.raiz,)),
        )
        frases = frases_de_reforco(com_projetos_descobertos(config, registar=self.linhas.append))
        self.assertIn("nimbus", frases)
        self.assertIn("atlas", frases)


class TestTabelaDescoberta(unittest.TestCase):
    BASE = '[microfone]\nnome = "Microfone"\n\n[[projetos]]\nnome = "x"\ncaminho = "."\n\n'

    def carregar(self, extra: str) -> Config:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "config.toml"
            caminho.write_text(self.BASE + extra, encoding="utf-8")
            return carregar_config(caminho, validar_caminhos=False)

    def test_sem_a_tabela_fica_a_raiz_por_omissao(self) -> None:
        self.assertIsNone(self.carregar("").descoberta.pastas)
        self.assertIsNone(self.carregar("[descoberta]\n").descoberta.pastas)

    def test_lista_vazia_desliga(self) -> None:
        self.assertEqual(self.carregar("[descoberta]\npastas = []\n").descoberta.pastas, ())

    def test_pastas_absolutas_e_til(self) -> None:
        config = self.carregar('[descoberta]\npastas = ["D:/caminho/para/repositorios", "~/Repos"]\n')
        self.assertEqual(
            config.descoberta.pastas,
            (Path("D:/caminho/para/repositorios").resolve(strict=False), (Path.home() / "Repos").resolve(strict=False)),
        )

    def test_valores_invalidos_sao_recusados(self) -> None:
        for texto in (
            'pastas = "D:/caminho/para/repositorios"',
            'pastas = ["relativo/pasta"]',
            'pastas = [""]',
            "pastas = [1]",
            'outra = ["D:/x"]',
        ):
            with self.subTest(texto=texto):
                with self.assertRaises(ConfigError) as erro:
                    self.carregar("[descoberta]\n" + texto + "\n")
                self.assertIn("[descoberta]", str(erro.exception))

    def test_o_exemplo_versionado_so_tem_um_caminho_ficticio(self) -> None:
        exemplo = (RAIZ_DO_REPO / "config.exemplo.toml").read_text(encoding="utf-8")
        self.assertIn("[descoberta]", exemplo)
        config = carregar_config(RAIZ_DO_REPO / "config.exemplo.toml", validar_caminhos=False)
        for pasta in config.descoberta.pastas or ():
            self.assertIn("caminho", pasta.as_posix())


if __name__ == "__main__":
    unittest.main()
