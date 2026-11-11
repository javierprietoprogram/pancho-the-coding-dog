#!/usr/bin/env python3
"""Hook de Pancho desde el proyecto (tests y desarrollo). La instalación usa la copia
estable `<raíz>/bridge/pancho_hook.py`. Ejemplo:
    python3 -I scripts/pancho_hook.py hook --provider claude --owner pancho-v1 < evento.json
"""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, 'src',
                            'pancho_bridge', 'launcher.py'), run_name='__main__')
