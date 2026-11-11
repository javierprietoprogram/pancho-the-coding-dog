#!/usr/bin/env python3
"""Lanzador de Pancho. El instalador lo copia como `<raíz>/bridge/pancho_hook.py`.

Busca el paquete junto a este archivo (instalado), en su carpeta padre o en `../src`.
Para `hook`, cualquier fallo termina con exit 0 y sin stdout.
"""
import os
import sys


def _main():
    here = os.path.dirname(os.path.realpath(__file__))
    parent = os.path.dirname(here)
    for candidate in (here, parent, os.path.join(parent, 'src')):
        if os.path.isfile(os.path.join(candidate, 'pancho_bridge', '__init__.py')):
            sys.path.insert(0, candidate)
            break
    is_hook = sys.argv[1:2] == ['hook']
    try:
        from pancho_bridge.cli import main
        code = main(sys.argv[1:])
    except SystemExit as exc:
        if is_hook:
            code = 0
        else:
            raise
    except BaseException:
        if not is_hook:
            raise
        code = 0
    sys.exit(0 if is_hook else (code or 0))


if __name__ == '__main__':
    _main()
