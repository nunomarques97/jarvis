r"""Auxiliar de tests/test_app.py — NAO e um ficheiro de testes (o nome nao casa
com o padrao `test*.py` do `unittest discover`, de proposito).

Reproduz, com processos de verdade, o ruido do encerramento: o
RealtimeSTT emite `Error receiving data from connection: [WinError 6] ...` com
`logging.error(..., exc_info=True)` no logger raiz de um PROCESSO FILHO
(`RealtimeSTT/audio_recorder.py:134`, arrancado por `mp.Process` em
`_start_thread`, audio_recorder.py:996). Aqui o filho faz exatamente o mesmo,
mais um `ERROR` diferente e uma linha `INFO`, e o pai deixa o stderr do filho
correr para o seu proprio stderr — tal como acontece no jarvis real.

Dois modos:

    .venv\Scripts\python -m tests._filho_ruidoso              # com o silenciador
    .venv\Scripts\python -m tests._filho_ruidoso --controlo   # controlo negativo

No modo normal este modulo importa `jarvis.app`; como em `spawn` (Windows) o
filho volta a executar o modulo `__main__` do pai antes de correr o alvo
(`multiprocessing/spawn.py`, `prepare()` -> `_fixup_main_from_name`), o corpo de
`jarvis.app` — e com ele `instalar_silenciador_no_processo_filho()` — corre
tambem dentro do filho. No modo de controlo `jarvis.app` nunca e importado, por
isso nao ha silenciador nenhum e a mensagem tem de aparecer: e o que prova que o
teste nao passa por acaso. A decisao e a mesma nos dois processos porque o
`sys.argv` do pai e reposto no filho por `prepare()` antes da reexecucao.
"""

from __future__ import annotations

import logging
import multiprocessing
import sys

CONTROLO = "--controlo" in sys.argv

if not CONTROLO:
    from jarvis.app import silenciar_ruido_do_shutdown

MENSAGEM_DO_WINERROR_6 = (
    "Error receiving data from connection: [WinError 6] The handle is invalid"
)
OUTRO_ERRO = "Error receiving data from connection: o disco esta cheio"
LINHA_DE_INFO = "linha informativa do filho que tem de continuar a passar"
LINHA_FINAL_DO_PAI = "pai: filho terminado"


def _ruido_do_filho() -> None:
    """O mesmo que `TranscriptionWorker.poll_connection` faz no shutdown."""
    logging.getLogger().setLevel(logging.INFO)
    try:
        raise OSError("[WinError 6] The handle is invalid")
    except OSError as erro:  # noqa: PERF203 - copia fiel do emissor real
        logging.error(f"Error receiving data from connection: {erro}", exc_info=True)
    logging.error(OUTRO_ERRO)
    logging.info(LINHA_DE_INFO)


def correr() -> int:
    filho = multiprocessing.Process(target=_ruido_do_filho)
    filho.start()
    filho.join()
    print(LINHA_FINAL_DO_PAI, flush=True)
    return filho.exitcode or 0


if __name__ == "__main__":
    if CONTROLO:
        raise SystemExit(correr())
    # A metade do pai, exatamente como em `correr_wav()`/`correr_microfone()`.
    with silenciar_ruido_do_shutdown():
        codigo = correr()
    raise SystemExit(codigo)
