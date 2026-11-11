#!/bin/zsh
# Compila Pancho en este Mac y lo deja en ~/Applications/Pancho.app.
#   ./install.sh
# No pide contraseña, no instala servicios y no toca Claude ni Codex:
# eso se hace después, desde la ventana de Pancho, con el botón «Conectar».
set -eu
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Pancho solo funciona en macOS." >&2; exit 1
fi
major=$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d. -f1)
if (( major < 13 )); then
  echo "Pancho necesita macOS 13 (Ventura) o posterior." >&2; exit 1
fi
if ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
  echo "Faltan las herramientas de desarrollo de Apple (gratuitas)." >&2
  echo "Instálalas con:  xcode-select --install" >&2
  echo "y vuelve a ejecutar ./install.sh" >&2
  exit 1
fi

echo "Compilando Pancho…"
/bin/zsh scripts/build_pancho.sh

DEST="$HOME/Applications/Pancho.app"
/bin/mkdir -p "$HOME/Applications"
# Si ya hay un Pancho abierto, se cierra para poder sustituirlo.
/usr/bin/pkill -f "$DEST/Contents/MacOS/Pancho" 2>/dev/null || true
/bin/rm -rf "$DEST"
/usr/bin/ditto build/Pancho.app "$DEST"
/usr/bin/open "$DEST"

echo
echo "Listo: Pancho está en $DEST y ya está abierto."
echo "Siguiente paso: en la ventana de Pancho, pulsa «Conectar» en Claude y/o en Codex."
