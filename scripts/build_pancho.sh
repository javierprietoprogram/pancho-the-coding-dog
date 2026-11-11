#!/bin/zsh
# Compila Pancho.app (Swift/AppKit, sin dependencias).
#   scripts/build_pancho.sh          → build/Pancho.app (instalable, sin código de depuración)
#   scripts/build_pancho.sh --debug  → build/Pancho-debug.app (-D PANCHO_DEBUG: DebugHooks para QA en simulación)
set -eu
cd "$(dirname "$0")/.."
DEBUG=0
for arg in "$@"; do
  case "$arg" in
    --debug) DEBUG=1 ;;
    *) echo "Uso: scripts/build_pancho.sh [--debug]" >&2; exit 64 ;;
  esac
done
if [[ $DEBUG == 1 ]]; then
  APP=build/Pancho-debug.app; BUNDLE_ID=local.pancho.debug; FLAGS=(-D PANCHO_DEBUG)
else
  APP=build/Pancho.app; BUNDLE_ID=local.pancho; FLAGS=()
fi
SPRITES=design/pancho-flotante/assets/pancho
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/module-cache
/usr/bin/xcrun swiftc -O -swift-version 5 -module-cache-path "$PWD/build/module-cache" \
  -target "$(uname -m)-apple-macos13.0" "${FLAGS[@]}" \
  app/Pancho/*.swift -o "$APP/Contents/MacOS/Pancho" -framework AppKit -framework QuartzCore
/bin/cp -R "$SPRITES" "$APP/Contents/Resources/pancho"
# Instalador dentro de la app (ventana de configuración): copia del puente sin cachés.
BR="$APP/Contents/Resources/bridge-src"
mkdir -p "$BR"
/usr/bin/rsync -a --exclude '__pycache__' --exclude '*.pyc' src/pancho_bridge "$BR/"
cat > "$BR/pancho_install.py" <<'PY'
"""Instalador de Pancho empaquetado en la app (sin proyecto de origen ni PoC que migrar)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pancho_bridge.installer import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
PY
PL="$APP/Contents/Info.plist"
/usr/bin/plutil -create xml1 "$PL"
/usr/libexec/PlistBuddy \
  -c "Add :CFBundleIdentifier string $BUNDLE_ID" \
  -c 'Add :CFBundleName string Pancho' \
  -c 'Add :CFBundleDisplayName string Pancho' \
  -c 'Add :CFBundleExecutable string Pancho' \
  -c 'Add :CFBundlePackageType string APPL' \
  -c 'Add :CFBundleShortVersionString string 1.0' \
  -c 'Add :CFBundleVersion string 1' \
  -c 'Add :LSMinimumSystemVersion string 13.0' \
  -c 'Add :LSUIElement bool true' \
  -c 'Add :NSHighResolutionCapable bool true' \
  -c 'Add :NSPrincipalClass string NSApplication' \
  "$PL"
/usr/bin/plutil -lint "$PL" >/dev/null
/usr/bin/codesign --force --sign - "$APP" >/dev/null 2>&1 || true
n=$(/usr/bin/find "$APP/Contents/Resources/pancho" -name '*.png' | /usr/bin/wc -l | /usr/bin/tr -d ' ')
echo "OK: $APP (fotogramas: $n$([[ $DEBUG == 1 ]] && echo ', depuración'))"
