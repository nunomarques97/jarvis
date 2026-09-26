"""Adaptacao ao sotaque: arvore de frases, reforco na descodificacao e lexico.

Sem modelo real, sem microfone e sem som: um vocabulario falso e um
descodificador falso que repete o ciclo guloso do onnx-asr (um `_decode` por
passo e argmax dos logits).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from jarvis import adaptacao
from jarvis.adaptacao import (
    Adaptacao,
    ArvoreDeFrases,
    Lexico,
    ReforcoDeFrases,
    carregar_lexico,
    criar_adaptacao,
    frases_de_reforco,
    ler_vocab,
    motivo_sem_gancho,
    tokenizar,
    variantes_da_frase,
)
from jarvis.config import (
    CAMINHO_EXEMPLO,
    Config,
    ConfigAdaptacao,
    ConfigError,
    Projeto,
    carregar_config,
)
from jarvis.stt import MotorParakeet, criar_motor

#: Vocabulario falso ao estilo do vocab.txt (o espaco inicial marca o inicio
#: de palavra). O 0 e o token vazio.
VOCAB = {
    0: "<blk>",
    1: " C",
    2: "la",
    3: "aude",
    4: "oud",
    5: " can",
    6: "cel",
    7: " add",
    8: " test",
    9: "s",
    10: " at",
    11: "test",
    12: " it",
    13: " ex",
    14: "em",
    15: "plo",
    16: "-",
    17: "um",
    18: " um",
    19: "<|nospeech|>",
    20: " Can",
    21: " Cl",
}
BLANK = 0
TAMANHO = len(VOCAB)


def ids_de(arvore: ArvoreDeFrases, no) -> set[int]:
    return set() if no.ids is None else {int(i) for i in no.ids}


class AsrFalso:
    """Imita o `_AsrWithTransducerDecoding` do onnx-asr com logits roteirizados.

    Cada passo do roteiro e um dicionario token -> logit (os outros ficam a
    -10). O ciclo e o do onnx-asr: argmax, e o passo avanca quando sai o
    token vazio.
    """

    _max_tokens_per_step = 10

    def __init__(self, roteiro: list[dict[int, float]]) -> None:
        self._vocab = dict(VOCAB)
        self._blank_idx = BLANK
        self.roteiro = roteiro
        self.passos = 0

    def _decode(self, prev_tokens, prev_state, encoder_out):
        self.passos += 1
        logits = np.full(TAMANHO, -10.0, dtype=np.float32)
        for token, valor in self.roteiro[int(encoder_out)].items():
            logits[token] = valor
        return logits, -1, prev_state

    def _decoding(self, n_passos: int) -> list[int]:
        tokens: list[int] = []
        t = 0
        emitidos = 0
        while t < n_passos:
            logits, _, _ = self._decode(tokens, None, t)
            token = int(logits.argmax())
            if token != self._blank_idx:
                tokens.append(token)
                emitidos += 1
            if token == self._blank_idx or emitidos == self._max_tokens_per_step:
                t += 1
                emitidos = 0
        return tokens

    def recognize(self, n_passos, sample_rate=16000):
        tokens = self._decoding(n_passos)
        return "".join(self._vocab[i] for i in tokens).strip()


class TestArvore(unittest.TestCase):
    def test_tokenizar_com_o_vocabulario_do_modelo(self) -> None:
        texto_para_id = {texto: ident for ident, texto in VOCAB.items() if not texto.startswith("<")}
        self.assertEqual(tokenizar(" Claude", texto_para_id, 5), [21, 3])
        self.assertEqual(tokenizar(" add tests", texto_para_id, 5), [7, 8, 9])
        self.assertIsNone(tokenizar(" zzz", texto_para_id, 5))

    def test_variantes_de_maiusculas_e_separadores(self) -> None:
        self.assertEqual(variantes_da_frase("cancel"), ["cancel", "Cancel"])
        self.assertIn("exemplo um", variantes_da_frase("exemplo-um"))
        self.assertIn("exemplo-um", variantes_da_frase("exemplo-um"))

    def test_construcao(self) -> None:
        arvore = ArvoreDeFrases(["Claude", "cancel", "add tests", "zzz"], VOCAB, BLANK)
        raiz = arvore.raiz
        # Inicios de frase: so tokens de inicio de palavra.
        self.assertEqual(ids_de(arvore, raiz), {21, 5, 20, 7})
        self.assertEqual(ids_de(arvore, raiz.filhos[21]), {3})
        self.assertTrue(raiz.filhos[21].filhos[3].terminal)
        self.assertEqual(ids_de(arvore, raiz.filhos[7].filhos[8]), {9})
        self.assertIn("zzz", arvore.ignoradas)

    def test_tokens_especiais_e_vazio_nunca_entram(self) -> None:
        vocab = {0: "<blk>", 1: "<|nospeech|>", 2: " a"}
        arvore = ArvoreDeFrases(["<|nospeech|>", "a"], vocab, 0)
        self.assertEqual(ids_de(arvore, arvore.raiz), {2})

    def test_reinicio_depois_de_frase_completa(self) -> None:
        arvore = ArvoreDeFrases(["Claude"], VOCAB, BLANK)
        no = arvore.avancar(arvore.raiz, 21)
        self.assertIsNot(no, arvore.raiz)
        self.assertIs(arvore.avancar(no, 3), arvore.raiz)

    def test_frase_completa_com_continuacao_fica_no_no(self) -> None:
        arvore = ArvoreDeFrases(["add test", "add tests"], VOCAB, BLANK)
        no = arvore.avancar(arvore.avancar(arvore.raiz, 7), 8)
        self.assertTrue(no.terminal)
        self.assertEqual(ids_de(arvore, no), {9})
        self.assertIs(arvore.avancar(no, 9), arvore.raiz)

    def test_reinicio_depois_de_frase_abandonada(self) -> None:
        arvore = ArvoreDeFrases(["Claude", "cancel"], VOCAB, BLANK)
        no = arvore.avancar(arvore.raiz, 21)
        self.assertIs(arvore.avancar(no, 12), arvore.raiz)
        # O token que abandona pode comecar outra frase.
        self.assertIs(arvore.avancar(no, 5), arvore.raiz.filhos[5])

    def test_ler_vocab_como_o_onnx_asr(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "vocab.txt"
            caminho.write_text("<blk> 0\n\u2581C 1\nla 2\n", encoding="utf-8")
            self.assertEqual(ler_vocab(caminho), {0: "<blk>", 1: " C", 2: "la"})


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.avisos: list[str] = []

    def adaptacao(self, frases=("Claude", "add tests"), bonus=3.0, **extra) -> Adaptacao:
        return Adaptacao(frases, bonus=bonus, avisar=self.avisos.append, **extra)


class TestReforco(_Base):
    def descodificar(self, asr: AsrFalso, n: int, reforco: Adaptacao | None, lingua="en") -> str:
        if reforco is None:
            return asr.recognize(n)
        with reforco.ao_transcrever(lingua):
            return asr.recognize(n)

    def roteiro_por_prefixo(self) -> AsrFalso:
        """O logit depende dos tokens ja emitidos, como num transdutor.

        "Cloud" ganha por pouco a "Claude" no segundo token.
        """
        asr = AsrFalso([])

        def decode(prev_tokens, prev_state, encoder_out):
            asr.passos += 1
            logits = np.full(TAMANHO, -10.0, dtype=np.float32)
            logits[BLANK] = 0.0
            if int(encoder_out) == 0 and not prev_tokens:
                logits[21] = 2.0
            if int(encoder_out) == 1 and prev_tokens == [21]:
                logits[4], logits[3] = 1.0, 0.5
            return logits, -1, prev_state

        asr._decode = decode
        return asr

    def test_sem_reforco_ouve_cloud(self) -> None:
        self.assertEqual(self.roteiro_por_prefixo().recognize(3), "Cloud")

    def test_reforco_na_continuacao_da_frase(self) -> None:
        asr = self.roteiro_por_prefixo()
        reforco = self.adaptacao(bonus=1.0)
        self.assertTrue(reforco.instalar(asr))
        self.assertEqual(self.descodificar(asr, 3, reforco), "Claude")
        self.assertTrue(any("ligado" in aviso for aviso in self.avisos))

    def test_bonus_so_nos_tokens_que_continuam_o_prefixo(self) -> None:
        vistos: list[np.ndarray] = []

        class Registo(AsrFalso):
            def _decode(inner, prev_tokens, prev_state, encoder_out):
                return np.zeros(TAMANHO, dtype=np.float32), -1, prev_state

        asr = Registo([])
        reforco = self.adaptacao(bonus=2.0)
        reforco.instalar(asr)
        with reforco.ao_transcrever("en"):
            for tokens in ([], [21], [21, 3], [21, 3, 12], [21, 12]):
                vistos.append(asr._decode(tokens if tokens else [], None, 0)[0])
        # Raiz: inicios de frase (" Cl", " add") com bonus; o resto a zero.
        self.assertEqual(set(np.flatnonzero(vistos[0])), {21, 7})
        # Depois de " Cl": so "aude".
        self.assertEqual(set(np.flatnonzero(vistos[1])), {3})
        self.assertEqual(float(vistos[1][3]), 2.0)
        # Frase completa: volta a raiz.
        self.assertEqual(set(np.flatnonzero(vistos[2])), {21, 7})
        self.assertEqual(set(np.flatnonzero(vistos[3])), {21, 7})
        # Frase abandonada (" it" depois de " Cl"): raiz.
        self.assertEqual(set(np.flatnonzero(vistos[4])), {21, 7})

    def test_estado_por_frase_nova(self) -> None:
        asr = self.roteiro_por_prefixo()
        reforco = self.adaptacao(bonus=1.0)
        reforco.instalar(asr)
        for _ in range(3):
            self.assertEqual(self.descodificar(asr, 3, reforco), "Claude")

    def test_fora_do_contexto_nao_mexe_nos_logits(self) -> None:
        asr = self.roteiro_por_prefixo()
        reforco = self.adaptacao(bonus=1.0)
        reforco.instalar(asr)
        self.assertEqual(asr.recognize(3), "Cloud")
        self.assertEqual(self.descodificar(asr, 3, reforco, lingua="pt"), "Cloud")
        self.assertEqual(self.descodificar(asr, 3, reforco, lingua=None), "Cloud")

    def test_logits_originais_nao_sao_alterados(self) -> None:
        guardados: list[np.ndarray] = []

        class Guarda(AsrFalso):
            def _decode(inner, prev_tokens, prev_state, encoder_out):
                logits = np.zeros(TAMANHO, dtype=np.float32)
                guardados.append(logits)
                return logits, -1, prev_state

        asr = Guarda([])
        reforco = self.adaptacao()
        reforco.instalar(asr)
        with reforco.ao_transcrever("en"):
            asr._decode([], None, 0)
        self.assertFalse(guardados[0].any())

    def test_instalar_duas_vezes_nao_embrulha_duas_vezes(self) -> None:
        asr = self.roteiro_por_prefixo()
        original = asr._decode
        self.adaptacao().instalar(asr)
        segunda = self.adaptacao()
        segunda.instalar(asr)
        self.assertIsInstance(asr._decode, ReforcoDeFrases)
        self.assertIs(asr._decode.original, original)

    def test_instala_no_asr_dentro_do_adaptador(self) -> None:
        class Adaptador:
            def __init__(self, asr):
                self.asr = asr

        asr = self.roteiro_por_prefixo()
        self.assertTrue(self.adaptacao().instalar(Adaptador(asr)))
        self.assertIsInstance(asr._decode, ReforcoDeFrases)

    def test_threads_tem_estado_proprio(self) -> None:
        asr = self.roteiro_por_prefixo()
        reforco = self.adaptacao(bonus=1.0)
        reforco.instalar(asr)
        resultados: list[str] = []

        def correr() -> None:
            resultados.append(self.descodificar(asr, 3, reforco))

        threads = [threading.Thread(target=correr) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(resultados, ["Claude"] * 4)

    def test_erro_no_reforco_desliga_e_mantem_a_transcricao(self) -> None:
        class LogitsEstranhos(AsrFalso):
            def _decode(inner, prev_tokens, prev_state, encoder_out):
                return ("nao e um array", -1, prev_state)

        asr = LogitsEstranhos([])
        reforco = self.adaptacao()
        reforco.instalar(asr)
        with reforco.ao_transcrever("en"):
            self.assertEqual(asr._decode([], None, 0)[0], "nao e um array")
            asr._decode([], None, 0)
        self.assertFalse(reforco.reforco_instalado)
        self.assertEqual(sum("desligado" in aviso for aviso in self.avisos), 1)


class TestSemGancho(_Base):
    def test_sem_decode(self) -> None:
        class SemDecode:
            _vocab = dict(VOCAB)
            _blank_idx = BLANK
            _max_tokens_per_step = 10

            def _decoding(self):
                return None

        self.assertIn("_decode", motivo_sem_gancho(SemDecode()))
        reforco = self.adaptacao()
        self.assertFalse(reforco.instalar(SemDecode()))
        self.assertFalse(reforco.reforco_instalado)
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("reforco de frases desligado", self.avisos[0])

    def test_assinatura_diferente(self) -> None:
        class OutraAssinatura(AsrFalso):
            def _decode(self, tokens, estado, codificacao, extra=None):
                return super()._decode(tokens, estado, codificacao)

        asr = OutraAssinatura([{BLANK: 1.0}])
        original = asr._decode
        reforco = self.adaptacao()
        self.assertFalse(reforco.instalar(asr))
        self.assertEqual(asr._decode, original)
        self.assertIn("outra assinatura", self.avisos[0])
        # A transcricao simples continua a funcionar, dentro e fora do contexto.
        with reforco.ao_transcrever("en"):
            self.assertEqual(asr.recognize(1), "")

    def test_sem_vocabulario_ou_nao_transdutor(self) -> None:
        asr = AsrFalso([])
        asr._vocab = {}
        self.assertIn("vocabulario", motivo_sem_gancho(asr))

        class Ctc:
            _vocab = dict(VOCAB)
            _blank_idx = BLANK

            def _decode(self, prev_tokens, prev_state, encoder_out):
                return None

            def _decoding(self):
                return None

        self.assertIn("transdutor", motivo_sem_gancho(Ctc()))

    def test_nenhuma_frase_no_vocabulario(self) -> None:
        reforco = self.adaptacao(frases=("zzz",))
        self.assertFalse(reforco.instalar(AsrFalso([])))
        self.assertIn("nenhuma frase", self.avisos[0])

    def test_reforco_desligado_nao_instala(self) -> None:
        asr = AsrFalso([])
        original = asr._decode
        self.assertFalse(self.adaptacao(reforco=False).instalar(asr))
        self.assertEqual(asr._decode, original)
        self.assertEqual(self.avisos, [])


class TestLexico(_Base):
    def lexico(self) -> Lexico:
        return Lexico([("attest", "add tests"), ("castle", "cancel"), ("can't sell it", "cancel"), ("cloud", "Claude")])

    def test_so_em_palavras_inteiras(self) -> None:
        lexico = self.lexico()
        self.assertEqual(lexico.corrigir("Attest the project."), "add tests the project.")
        self.assertEqual(lexico.corrigir("attestation"), "attestation")
        self.assertEqual(lexico.corrigir("newcastle castles"), "newcastle castles")
        self.assertEqual(lexico.corrigir("Uh, castle."), "Uh, cancel.")
        self.assertEqual(lexico.corrigir("ask Cloud now"), "ask Claude now")

    def test_frase_de_varias_palavras_com_espacos_quaisquer(self) -> None:
        self.assertEqual(self.lexico().corrigir("Can't  sell it."), "cancel.")

    def test_substituicao_numa_so_passagem(self) -> None:
        lexico = Lexico([("a", "b"), ("b", "c")])
        self.assertEqual(lexico.corrigir("a b"), "b c")

    def test_de_dados_valida(self) -> None:
        bom = {"versao": 1, "lingua": "en", "regras": [{"de": "castle", "para": "cancel"}]}
        self.assertEqual(Lexico.de_dados(bom).corrigir("castle"), "cancel")
        maus = [
            [],
            {"versao": 2, "lingua": "en", "regras": []},
            {"versao": 1, "lingua": "pt", "regras": []},
            {"versao": 1, "lingua": "en", "regras": "x"},
            {"versao": 1, "lingua": "en", "regras": [{"de": "", "para": "x"}]},
            {"versao": 1, "lingua": "en", "regras": [{"de": "a", "para": "b", "mais": 1}]},
            {"versao": 1, "lingua": "en", "regras": [{"de": "a\nb", "para": "b"}]},
            {"versao": 1, "lingua": "en", "regras": [{"de": "x" * 81, "para": "b"}]},
            {"versao": 1, "lingua": "en", "regras": [{"de": "a", "para": "b"}, {"de": "A", "para": "c"}]},
        ]
        for dados in maus:
            with self.subTest(dados=dados), self.assertRaises(ValueError):
                Lexico.de_dados(dados)

    def test_ficheiro_em_falta_ou_invalido(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            self.assertIsNone(carregar_lexico(Path(pasta) / "lexico-en.json", self.avisos.append))
            self.assertIn("sem lexico", self.avisos[-1])
            invalido = Path(pasta) / "invalido.json"
            invalido.write_text("{nao e json", encoding="utf-8")
            self.assertIsNone(carregar_lexico(invalido, self.avisos.append))
            self.assertIn("invalido", self.avisos[-1])
            valido = Path(pasta) / "valido.json"
            valido.write_text(
                json.dumps({"versao": 1, "lingua": "en", "regras": [{"de": "castle", "para": "cancel"}]}),
                encoding="utf-8",
            )
            self.assertIsNotNone(carregar_lexico(valido, self.avisos.append))

    def test_lexico_em_falta_regista_uma_vez_e_texto_fica_igual(self) -> None:
        config = Config(microfone="m", projetos=(), adaptacao=ConfigAdaptacao(lexico=True))
        with tempfile.TemporaryDirectory() as pasta:
            reforco = criar_adaptacao(config, avisar=self.avisos.append, caminho_do_lexico=Path(pasta) / "x.json")
        for _ in range(3):
            self.assertEqual(reforco.corrigir("uh castle", "en"), "uh castle")
        self.assertEqual(len(self.avisos), 1)

    def test_so_na_lingua_da_adaptacao(self) -> None:
        reforco = self.adaptacao(lexico=self.lexico())
        self.assertEqual(reforco.corrigir("castle", "en"), "cancel")
        self.assertEqual(reforco.corrigir("castle", "pt"), "castle")
        self.assertEqual(reforco.corrigir("castle", None), "castle")

    def test_caminho_do_lexico_fica_em_pasta_ignorada(self) -> None:
        self.assertEqual(adaptacao.CAMINHO_DO_LEXICO.parent.name, "adaptacao")
        self.assertEqual(adaptacao.CAMINHO_DO_LEXICO.parent.parent.name, "models")
        ignorado = (adaptacao.RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("models/", ignorado)


class TestFrasesDaConfig(_Base):
    def config(self, *nomes: str, **ajuste) -> Config:
        projetos = tuple(Projeto(nome=nome, caminho=Path(".")) for nome in nomes)
        return Config(microfone="m", projetos=projetos, adaptacao=ConfigAdaptacao(**ajuste))

    def test_nomes_de_projeto_so_da_config(self) -> None:
        self.assertEqual(frases_de_reforco(self.config()), adaptacao.VOCABULARIO_EN)
        self.assertEqual(
            frases_de_reforco(self.config("projeto-alfa", "beta")),
            adaptacao.VOCABULARIO_EN + ("projeto-alfa", "beta"),
        )

    def test_vocabulario_versionado_so_generico(self) -> None:
        exemplo = carregar_config(CAMINHO_EXEMPLO, validar_caminhos=False)
        nomes = {projeto.nome.casefold() for projeto in exemplo.projetos}
        self.assertTrue(nomes)
        vocabulario = {frase.casefold() for frase in adaptacao.VOCABULARIO_EN}
        self.assertFalse(nomes & vocabulario)
        self.assertTrue(all(len(frase) <= adaptacao.FRASE_MAXIMA for frase in adaptacao.VOCABULARIO_EN))

    def test_desligada_por_omissao(self) -> None:
        self.assertIsNone(criar_adaptacao(self.config("a")))
        self.assertIsNone(criar_adaptacao(object()))

    def test_criar_com_reforco(self) -> None:
        reforco = criar_adaptacao(self.config("projeto-alfa", reforco=True, bonus=2.5), avisar=self.avisos.append)
        self.assertEqual(reforco.bonus, 2.5)
        self.assertIn("projeto-alfa", reforco.frases)
        self.assertIsNone(reforco.lexico)
        self.assertEqual(self.avisos, [])


class TestConfigDaAdaptacao(unittest.TestCase):
    def carregar(self, tabela: str) -> Config:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "config.toml"
            base = '[microfone]\nnome = "m"\n[[projetos]]\nnome = "a"\ncaminho = "D:/x"\n'
            # Uma chave solta tem de vir antes das tabelas para ficar no topo.
            texto = base + tabela if tabela.startswith("[") else tabela + base
            caminho.write_text(texto, encoding="utf-8")
            return carregar_config(caminho, validar_caminhos=False)

    def test_omissao_mantem_o_comportamento(self) -> None:
        self.assertEqual(self.carregar("").adaptacao, ConfigAdaptacao(reforco=False, lexico=False))
        self.assertEqual(carregar_config(CAMINHO_EXEMPLO, validar_caminhos=False).adaptacao, ConfigAdaptacao())

    def test_valores_validos(self) -> None:
        ajuste = self.carregar("[adaptacao]\nreforco = true\nbonus = 3\nlexico = true\n").adaptacao
        self.assertEqual(ajuste, ConfigAdaptacao(reforco=True, bonus=3.0, lexico=True))

    def test_valores_invalidos(self) -> None:
        for tabela in (
            "adaptacao = 1\n",
            "[adaptacao]\nreforco = 1\n",
            '[adaptacao]\nlexico = "sim"\n',
            "[adaptacao]\nbonus = 0\n",
            "[adaptacao]\nbonus = -1\n",
            "[adaptacao]\nbonus = 10.5\n",
            "[adaptacao]\nbonus = true\n",
            "[adaptacao]\nbonus = nan\n",
            "[adaptacao]\nforca = 1\n",
        ):
            with self.subTest(tabela=tabela), self.assertRaises(ConfigError):
                self.carregar(tabela)


class TestMotorParakeetComAdaptacao(_Base):
    class ModeloFalso:
        def __init__(self, texto: str) -> None:
            self.texto = texto
            self.ativo_durante: list[bool] = []
            self.gancho = None

        def recognize(self, audio, sample_rate=16000):
            self.ativo_durante.append(bool(self.gancho and getattr(self.gancho._local, "ativo", False)))
            return self.texto

    def motor(self, reforco: Adaptacao | None, texto: str) -> tuple[MotorParakeet, "ModeloFalso"]:
        motor = MotorParakeet("cpu", pasta=Path("nao-existe"), adaptacao=reforco)
        modelo = self.ModeloFalso(texto)
        motor._modelo = modelo
        return motor, modelo

    def test_lexico_aplicado_ao_texto_em_ingles(self) -> None:
        reforco = self.adaptacao(reforco=False, lexico=Lexico([("castle", "cancel")]))
        motor, _ = self.motor(reforco, "Uh castle.")
        self.assertEqual(motor.transcrever(b"\x01\x00" * 160, lingua="en").texto, "Uh cancel.")
        self.assertEqual(motor.transcrever(b"\x01\x00" * 160, lingua="pt").texto, "Uh castle.")

    def test_sem_adaptacao_texto_igual(self) -> None:
        motor, _ = self.motor(None, "Uh castle.")
        self.assertEqual(motor.transcrever(b"\x01\x00" * 160, lingua="en").texto, "Uh castle.")

    def test_reforco_ativo_so_durante_a_transcricao(self) -> None:
        reforco = self.adaptacao()
        reforco.instalar(AsrFalso([]))
        motor, modelo = self.motor(reforco, "ok")
        modelo.gancho = reforco.gancho
        motor.transcrever(b"\x01\x00" * 160, lingua="en")
        motor.transcrever(b"\x01\x00" * 160, lingua="pt")
        self.assertEqual(modelo.ativo_durante, [True, False])
        self.assertFalse(getattr(reforco.gancho._local, "ativo", False))

    def test_criar_motor_passa_a_adaptacao_so_ao_parakeet(self) -> None:
        reforco = self.adaptacao()
        self.assertIs(criar_motor("parakeet-tdt-0.6b-v3", "cpu", adaptacao=reforco).adaptacao, reforco)
        criar_motor("whisper-medium", "cpu", adaptacao=reforco)
        self.assertIn("ignorada", self.avisos[-1])
        self.assertIsNone(criar_motor("parakeet-tdt-0.6b-v3", "cpu").adaptacao)


if __name__ == "__main__":
    unittest.main()
