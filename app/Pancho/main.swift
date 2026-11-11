import AppKit

// Pancho.app: mascota flotante que muestra y responde preguntas de Claude Code y Codex.
// Sin Dock ni menú (LSUIElement), sin arranque automático.

if ProcessInfo.processInfo.environment["PANCHO_HOME"] == nil,
   let id = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: id).contains(where: { $0.processIdentifier != getpid() }) {
    // Ya hay un Pancho abierto sobre el almacén real: no se duplica.
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = Controller()
app.delegate = controller
app.run()
