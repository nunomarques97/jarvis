r"""Testes de jarvis/consola.py, unittest da biblioteca padrao (sem
pytest: nenhuma framework de testes fora da biblioteca padrao).

Fecha o finding 3 (menor) do QA de fecho: o proprio processo jarvis nunca
forcava a sua stdout/stderr para UTF-8, so o ambiente do processo filho do
Piper estava coberto. Estes testes protegem o contrato do modulo FOLHA:

  * um stream sem `reconfigure` (io.StringIO nos testes tem, mas um objeto
    qualquer sem o metodo nao) nao levanta;
  * um stream cujo `reconfigure` levanta tambem nao propaga - a funcao nunca
    pode ser ela a rebentar o processo;
  * um stream falso que regista a chamada recebe encoding="utf-8" e
    errors="replace", nos dois streams (stdout e stderr);
  * idempotencia: chamar `forcar_consola_utf8()` duas vezes nao muda nada
    nem rebenta.

Corre com:

    .venv\Scripts\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest

from jarvis.consola import _forcar_stream_utf8, forcar_consola_utf8


class _StreamSemReconfigure:
    """Imita io.StringIO na ausencia do metodo: nenhum `reconfigure`."""


class _StreamQueLevanta:
    """Um stream cujo `reconfigure` rebenta sempre (ex.: stream ja fechado)."""

    def reconfigure(self, **kwargs):
        raise RuntimeError("este stream nao aceita reconfigure")


class _StreamFalso:
    """Regista cada chamada a `reconfigure`, para inspecionar os argumentos."""

    def __init__(self) -> None:
        self.chamadas: list[dict[str, object]] = []

    def reconfigure(self, encoding=None, errors=None):
        self.chamadas.append({"encoding": encoding, "errors": errors})


class TestForcarStreamUtf8(unittest.TestCase):
    def test_stream_sem_reconfigure_nao_levanta(self) -> None:
        stream = _StreamSemReconfigure()
        try:
            _forcar_stream_utf8(stream)
        except Exception as exc:  # pragma: no cover - falha do teste, nao caminho feliz
            self.fail(f"_forcar_stream_utf8 propagou com stream sem reconfigure: {exc!r}")

    def test_stream_cujo_reconfigure_levanta_nao_propaga(self) -> None:
        stream = _StreamQueLevanta()
        try:
            _forcar_stream_utf8(stream)
        except Exception as exc:  # pragma: no cover - falha do teste, nao caminho feliz
            self.fail(f"_forcar_stream_utf8 propagou a excecao do stream: {exc!r}")

    def test_stream_falso_recebe_utf8_e_replace(self) -> None:
        stream = _StreamFalso()
        _forcar_stream_utf8(stream)
        self.assertEqual(stream.chamadas, [{"encoding": "utf-8", "errors": "replace"}])

    def test_chamar_duas_vezes_e_idempotente(self) -> None:
        stream = _StreamFalso()
        _forcar_stream_utf8(stream)
        _forcar_stream_utf8(stream)
        self.assertEqual(
            stream.chamadas,
            [
                {"encoding": "utf-8", "errors": "replace"},
                {"encoding": "utf-8", "errors": "replace"},
            ],
        )


class TestForcarConsolaUtf8(unittest.TestCase):
    def setUp(self) -> None:
        self._stdout_original = sys.stdout
        self._stderr_original = sys.stderr
        self.stdout_falso = _StreamFalso()
        self.stderr_falso = _StreamFalso()
        sys.stdout = self.stdout_falso  # type: ignore[assignment]
        sys.stderr = self.stderr_falso  # type: ignore[assignment]

    def tearDown(self) -> None:
        sys.stdout = self._stdout_original
        sys.stderr = self._stderr_original

    def test_forca_stdout_e_stderr(self) -> None:
        forcar_consola_utf8()
        self.assertEqual(self.stdout_falso.chamadas, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(self.stderr_falso.chamadas, [{"encoding": "utf-8", "errors": "replace"}])

    def test_idempotente_nao_muda_nem_rebenta(self) -> None:
        forcar_consola_utf8()
        forcar_consola_utf8()
        self.assertEqual(
            self.stdout_falso.chamadas,
            [
                {"encoding": "utf-8", "errors": "replace"},
                {"encoding": "utf-8", "errors": "replace"},
            ],
        )
        self.assertEqual(
            self.stderr_falso.chamadas,
            [
                {"encoding": "utf-8", "errors": "replace"},
                {"encoding": "utf-8", "errors": "replace"},
            ],
        )

    def test_stdout_sem_reconfigure_nao_impede_stderr(self) -> None:
        sys.stdout = _StreamSemReconfigure()  # type: ignore[assignment]
        try:
            forcar_consola_utf8()
        except Exception as exc:  # pragma: no cover - falha do teste, nao caminho feliz
            self.fail(f"forcar_consola_utf8 propagou com stdout sem reconfigure: {exc!r}")
        self.assertEqual(self.stderr_falso.chamadas, [{"encoding": "utf-8", "errors": "replace"}])


if __name__ == "__main__":
    unittest.main()
