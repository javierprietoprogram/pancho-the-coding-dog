#!/usr/bin/env python3
"""Instalador global de Pancho (hooks Claude y, con --codex, hooks Codex). Ejemplos:
    python3 scripts/pancho_install.py status
    python3 scripts/pancho_install.py install [--migrate-poc]
    python3 scripts/pancho_install.py uninstall
    python3 scripts/pancho_install.py install --codex [--migrate-poc] [--python RUTA]
    python3 scripts/pancho_install.py status --codex
    python3 scripts/pancho_install.py uninstall --codex
Pruebas: --home <raíz temporal> --claude-settings <settings temporal> [--poc-root <copia>]
         --codex-hooks <hooks.json temporal> --codex-pin <pin> --codex-app <app falsa>.
`--poc-root` por defecto es este proyecto (mascota), cuya PoC se migra con --migrate-poc.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from pancho_bridge.installer import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main(default_poc_root=str(ROOT)))
