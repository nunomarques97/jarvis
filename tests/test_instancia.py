"""Testes da tranca de uma so instancia (`jarvis.instancia`).

Sem microfone, sem som e sem processos reais: os PIDs e o estado dos
processos sao falsos, e a tranca vive numa pasta temporaria.

    .venv\\Scripts\\python -m unittest tests.test_instancia -v
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import instancia
from jarvis.instancia import (
    NOME_DA_TRANCA,
    DonoDaTranca,
    OutraInstanciaAberta,
    TrancaDaInstancia,
    ler_dono,
    mensagem_de_recusa,
)


class ProcessosFalsos:
    """Faz de sistema operativo: {pid: instante de criacao} dos processos vivos."""

    def __init__(self, vivos: dict[int, int | None] | None = None) -> None:
        self.vivos = dict(vivos or {})
        self.perguntados: list[int] = []

    def __call__(self, pid: int) -> tuple[bool, int | None]:
        self.perguntados.append(pid)
        if pid in self.vivos:
            return True, self.vivos[pid]
        return False, None


class _ComPasta(unittest.TestCase):
    def setUp(self) -> None:
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        self.caminho = Path(pasta.name) / "logs" / NOME_DA_TRANCA

    def tranca(self, pid: int, processos: ProcessosFalsos) -> TrancaDaInstancia:
        return TrancaDaInstancia(self.caminho, pid=pid, estado=processos)

    def escrever(self, texto: str) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.caminho.write_text(texto, encoding="ascii")


class TestAdquirir(_ComPasta):
    def test_sem_tranca_cria_o_ficheiro_com_o_pid(self) -> None:
        processos = ProcessosFalsos({100: 5555})
        tranca = self.tranca(100, processos)
        self.assertIsNone(tranca.adquirir())
        self.assertTrue(tranca.adquirida)
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")), DonoDaTranca(100, 5555))
        self.assertEqual(self.caminho.read_text(encoding="ascii").splitlines()[0], "100")

    def test_outro_jarvis_vivo_recusa_com_o_pid_dele(self) -> None:
        processos = ProcessosFalsos({100: 5555, 200: 7777})
        primeiro = self.tranca(100, processos)
        primeiro.adquirir()
        segundo = self.tranca(200, processos)
        with self.assertRaises(OutraInstanciaAberta) as contexto:
            segundo.adquirir()
        self.assertEqual(contexto.exception.pid, 100)
        self.assertFalse(segundo.adquirida)
        # A tranca do primeiro fica intacta.
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 100)
        segundo.libertar()
        self.assertTrue(self.caminho.exists(), "quem foi recusado nunca apaga a tranca do outro")

    def test_tranca_de_um_processo_morto_e_substituida(self) -> None:
        self.escrever("4242\n9999\n")
        processos = ProcessosFalsos({100: 5555})
        tranca = self.tranca(100, processos)
        substituida = tranca.adquirir()
        self.assertEqual(substituida, DonoDaTranca(4242, 9999))
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 100)
        self.assertEqual(list(self.caminho.parent.iterdir()), [self.caminho], "nada fica para tras")

    def test_pid_reaproveitado_por_outro_programa_conta_como_morto(self) -> None:
        # O PID 4242 esta vivo, mas nasceu noutro instante: nao e o jarvis.
        self.escrever("4242\n9999\n")
        tranca = self.tranca(100, ProcessosFalsos({100: 5555, 4242: 1234}))
        self.assertEqual(tranca.adquirir().pid, 4242)
        self.assertTrue(tranca.adquirida)

    def test_mesmo_pid_e_mesmo_instante_e_o_jarvis_vivo(self) -> None:
        self.escrever("4242\n9999\n")
        with self.assertRaises(OutraInstanciaAberta):
            self.tranca(100, ProcessosFalsos({4242: 9999})).adquirir()

    def test_tranca_sem_instante_e_processo_vivo_recusa(self) -> None:
        self.escrever("4242\n")
        with self.assertRaises(OutraInstanciaAberta) as contexto:
            self.tranca(100, ProcessosFalsos({4242: None})).adquirir()
        self.assertEqual(contexto.exception.pid, 4242)

    def test_ficheiro_estragado_e_substituido(self) -> None:
        self.escrever("isto nao e um pid")
        tranca = self.tranca(100, ProcessosFalsos())
        self.assertEqual(tranca.adquirir(), DonoDaTranca(0))
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 100)

    def test_tranca_vazia_acabada_de_criar_espera_pelo_pid(self) -> None:
        # Outro jarvis criou o ficheiro e ainda nao escreveu o PID: nao e velha.
        self.escrever("")
        escritor = threading.Timer(0.1, lambda: self.caminho.write_text("300\n3\n", encoding="ascii"))
        escritor.start()
        self.addCleanup(escritor.cancel)
        with self.assertRaises(OutraInstanciaAberta) as contexto:
            self.tranca(100, ProcessosFalsos({300: 3})).adquirir()
        self.assertEqual(contexto.exception.pid, 300)
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 300)

    def test_tranca_vazia_antiga_e_substituida(self) -> None:
        self.escrever("")
        antes = time.time() - 60
        os.utime(self.caminho, (antes, antes))
        tranca = self.tranca(100, ProcessosFalsos())
        self.assertEqual(tranca.adquirir(), DonoDaTranca(0))
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 100)

    def test_tranca_vazia_que_nunca_recebe_o_pid_acaba_substituida(self) -> None:
        self.escrever("")
        tranca = self.tranca(100, ProcessosFalsos())
        tranca.espera_da_tranca_vazia_s = 0.1
        self.assertEqual(tranca.adquirir(), DonoDaTranca(0))
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 100)

    def test_tranca_com_o_proprio_pid_e_velha(self) -> None:
        self.escrever("100\n1\n")
        tranca = self.tranca(100, ProcessosFalsos({100: 5555}))
        self.assertEqual(tranca.adquirir().pid, 100)

    def test_criacao_e_atomica_com_o_excl(self) -> None:
        chamadas: list[int] = []
        original = os.open

        def abrir(caminho, bandeiras, *resto):
            chamadas.append(bandeiras)
            return original(caminho, bandeiras, *resto)

        with mock.patch.object(instancia.os, "open", abrir):
            self.tranca(100, ProcessosFalsos()).adquirir()
        self.assertTrue(chamadas and all(b & os.O_CREAT and b & os.O_EXCL for b in chamadas))

    def test_outro_jarvis_que_ganha_a_corrida_a_meio_da_troca(self) -> None:
        # Tranca velha do 4242 (morto). Entre ler e por de parte, o 300 (vivo)
        # troca-a pela sua: a tranca do 300 tem de voltar ao lugar.
        self.escrever("4242\n")
        processos = ProcessosFalsos({300: 3})
        tranca = self.tranca(100, processos)
        original = os.replace

        def replace_com_corrida(origem, destino):
            Path(origem).write_text("300\n3\n", encoding="ascii")
            return original(origem, destino)

        with mock.patch.object(instancia.os, "replace", replace_com_corrida):
            with self.assertRaises(OutraInstanciaAberta) as contexto:
                tranca.adquirir()
        self.assertEqual(contexto.exception.pid, 300)
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 300)


class TestLibertar(_ComPasta):
    def test_libertar_apaga_e_pode_repetir(self) -> None:
        tranca = self.tranca(100, ProcessosFalsos())
        tranca.adquirir()
        tranca.libertar()
        self.assertFalse(self.caminho.exists())
        tranca.libertar()
        self.assertFalse(tranca.adquirida)

    def test_nunca_apaga_a_tranca_de_outro(self) -> None:
        tranca = self.tranca(100, ProcessosFalsos())
        tranca.adquirir()
        self.escrever("200\n")  # outro processo ficou com ela
        tranca.libertar()
        self.assertEqual(ler_dono(self.caminho.read_text(encoding="ascii")).pid, 200)

    def test_com_liberta_mesmo_com_erro(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.tranca(100, ProcessosFalsos()):
                self.assertTrue(self.caminho.exists())
                raise RuntimeError("falha a meio")
        self.assertFalse(self.caminho.exists())

    def test_depois_de_libertar_outro_jarvis_arranca(self) -> None:
        processos = ProcessosFalsos({100: 1, 200: 2})
        primeiro = self.tranca(100, processos)
        primeiro.adquirir()
        primeiro.libertar()
        self.assertIsNone(self.tranca(200, processos).adquirir())


class TestLerDono(unittest.TestCase):
    def test_formatos(self) -> None:
        self.assertEqual(ler_dono("123\n456\n"), DonoDaTranca(123, 456))
        self.assertEqual(ler_dono("123"), DonoDaTranca(123, None))
        self.assertIsNone(ler_dono(""))
        self.assertIsNone(ler_dono("0\n"))
        self.assertIsNone(ler_dono("-5\n"))
        self.assertIsNone(ler_dono("abc\n"))


class TestMensagem(unittest.TestCase):
    def test_ingles_diz_que_ha_outro_o_pid_e_para_fechar(self) -> None:
        texto = mensagem_de_recusa(4242, "en")
        self.assertIn("Another jarvis is already running", texto)
        self.assertIn("4242", texto)
        self.assertIn("Close it first", texto)

    def test_portugues(self) -> None:
        texto = mensagem_de_recusa(4242, "pt")
        self.assertIn("outro jarvis aberto", texto)
        self.assertIn("4242", texto)


class TestProcessoReal(unittest.TestCase):
    """So a biblioteca padrao: o proprio processo esta vivo, um PID impossivel nao."""

    def test_o_proprio_processo_esta_vivo(self) -> None:
        vivo, _ = instancia.estado_do_processo(os.getpid())
        self.assertTrue(vivo)

    def test_pid_invalido_nao_esta_vivo(self) -> None:
        self.assertEqual(instancia.estado_do_processo(0), (False, None))
        self.assertEqual(instancia.estado_do_processo(-1), (False, None))


if __name__ == "__main__":
    unittest.main()
