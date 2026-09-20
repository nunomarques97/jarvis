r"""Forca a consola do proprio processo jarvis para UTF-8 (T11).

Fecha o finding 3 (menor) do QA de fecho: `python -m jarvis.acoes_locais horas`
sem `PYTHONUTF8`/`PYTHONIOENCODING` no ambiente imprime `resposta = S?o 9
horas...` porque `sys.stdout.encoding` herda a codepage da consola (cp1252 ou
850 no Windows). A T3 ja tinha resolvido esta classe de bug para o AMBIENTE DO
PROCESSO FILHO do Piper (`AMBIENTE_UTF8` em scripts/gerar_wav.py e
jarvis/voz.py), mas nunca para o PROPRIO stdout/stderr do jarvis.

Modulo FOLHA: so biblioteca padrao, nao importa nada do resto do pacote
`jarvis`, para poder ser importado por qualquer ponto de entrada (jarvis/app.py,
jarvis/acoes_locais.py, scripts/*.py) sem arrastar audio nem voz.

`forcar_consola_utf8()` NUNCA corre ao importar este modulo — importar um
modulo nao pode ter efeitos colaterais. Cada ponto de entrada chama-a
explicitamente como primeira instrucao do seu `main()`.

NAO substitui `AMBIENTE_UTF8` (ambiente do processo filho, T3) nem
`texto_para_a_consola` (jarvis/app.py, rede de seguranca que degrada com "?"
em vez de rebentar quando o stream nao suporta `reconfigure`) — os dois
continuam a fazer falta e ficam intactos.
"""

from __future__ import annotations

import sys
from typing import TextIO


def _forcar_stream_utf8(stream: TextIO) -> None:
    """Reconfigura um unico stream para UTF-8, tolerante a qualquer falha.

    Se o stream nao tiver `reconfigure` (por exemplo `io.StringIO` nos
    testes, ou um stdout ja substituido por outra coisa) ou `reconfigure`
    levantar por qualquer razao, esta funcao nao faz nada e NAO propaga —
    nunca pode ser ela a rebentar o processo.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        return


def forcar_consola_utf8() -> None:
    """Forca stdout e stderr do processo atual para UTF-8 (errors=replace).

    Idempotente: chamar duas vezes nao muda nada nem rebenta (reconfigurar
    um `io.TextIOWrapper` para o mesmo encoding e uma operacao valida e
    repetivel). Chamar como primeira instrucao de `main()` em cada ponto de
    entrada do jarvis, nunca ao importar este modulo.
    """
    _forcar_stream_utf8(sys.stdout)
    _forcar_stream_utf8(sys.stderr)
