"""Puente local de Pancho (contrato de almacén v1).

Solo biblioteca estándar, compatible con Python 3.9+. Autocontenido: el
instalador copia este paquete a `<raíz>/bridge/` y los hooks lo ejecutan desde
allí, sin depender de la carpeta del proyecto.

Uso:
    python3 -I <raíz>/bridge/pancho_hook.py hook --provider claude|codex --owner pancho-v1 --home <raíz>
    python3 -I <raíz>/bridge/pancho_hook.py codex-send --home <raíz> --key <clave>
    python3 -m pancho_bridge status [--home RAÍZ]
    python3 -m pancho_bridge simulate ask [--home RAÍZ_SIMULACIÓN] [--wait S] [--auto-accept]
    python3 scripts/pancho_install.py install|status|uninstall [--home] [--claude-settings]
    python3 scripts/pancho_install.py install|status|uninstall --codex [--migrate-poc] [--codex-hooks]
"""

VERSION = '1.1.0'
OWNER = 'pancho-v1'
