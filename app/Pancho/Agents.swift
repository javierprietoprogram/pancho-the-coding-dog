import AppKit
import WebKit

// Panel de agentes: ventana con la página empaquetada (dashboard/index.html). La página no accede a la red ni
// al disco: Pancho le entrega el árbol de conversaciones y subagentes ya leído del almacén.

private final class AgentsWindow: NSWindow {
    var onEscape: (() -> Void)?
    override var canBecomeKey: Bool { true }
    /// Esc que no llegó a la página: decide ella (cierra el detalle o la ventana).
    override func cancelOperation(_ sender: Any?) { onEscape?() }
}

final class AgentsPanel: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    let window: NSWindow
    let web: WKWebView
    private var ready = false
    private var placed = false
    private var payload = "{}"
    private var pushed = ""
    /// Pantalla donde está Pancho, para abrir la ventana a su lado la primera vez.
    var anchorScreen: (() -> NSScreen?)?

    override init() {
        let p = AgentsWindow(contentRect: NSRect(x: 0, y: 0, width: 880, height: 620),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        // Barra de título estándar: la ventana se arrastra, se redimensiona y se amplía como cualquier otra.
        p.title = "Agentes"
        p.titlebarAppearsTransparent = true
        p.isMovable = true
        p.level = .floating
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.minSize = NSSize(width: 560, height: 400)
        p.appearance = NSAppearance(named: .aqua)
        p.collectionBehavior = [.fullScreenAuxiliary, .moveToActiveSpace]
        p.backgroundColor = NSColor(hex: 0xEEF0F4)
        window = p

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: cfg)
        super.init()
        cfg.userContentController.add(self, name: "pancho")
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.allowsMagnification = false

        let root = NSView(frame: NSRect(x: 0, y: 0, width: 880, height: 620))
        web.frame = root.bounds
        web.autoresizingMask = [.width, .height]
        root.addSubview(web)
        p.contentView = root
        p.onEscape = { [weak self] in
            guard let self = self else { return }
            if self.ready { self.call("window.pancho.escape()", [:]) } else { self.window.close() }
        }
        loadPage()
    }

    private func loadPage() {
        guard let dir = Bundle.main.resourceURL else { return }
        web.loadFileURL(dir.appendingPathComponent("dashboard/index.html"), allowingReadAccessTo: dir)
    }

    var isOpen: Bool { window.isVisible }

    func toggle() { isOpen ? window.close() : show() }

    func show() {
        if !placed {
            placed = true
            // Recuerda posición y tamaño; la primera vez, centrada en la pantalla de Pancho.
            if !window.setFrameUsingName("PanchoAgentsPanel"), let vf = (anchorScreen?() ?? NSScreen.main)?.visibleFrame {
                let w = min(880, vf.width - 40), h = min(620, vf.height - 40)
                window.setFrame(NSRect(x: vf.midX - w / 2, y: vf.midY - h / 2, width: w, height: h), display: false)
            }
            window.setFrameAutosaveName("PanchoAgentsPanel")
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(web)
        pushed = ""
        push()
    }

    /// Nuevo estado (cada sondeo). Con la ventana cerrada no se prepara nada; abierta, solo se entrega si algo cambió
    /// (la página lleva sus propios relojes, así que el «now» de cada sondeo no cuenta como cambio).
    func update(_ object: [String: Any]) {
        guard isOpen, let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
        payload = String(decoding: data, as: UTF8.self)
        push()
    }

    private func push() {
        guard ready, isOpen else { return }
        let key = payload.replacingOccurrences(of: #""now":[0-9.]+"#, with: "", options: .regularExpression)
        if key == pushed { return }
        pushed = key
        call("window.pancho.update(p)", ["p": payload])
    }

    private func call(_ js: String, _ args: [String: Any]) {
        web.callAsyncJavaScript(js, arguments: args, in: nil, in: .page, completionHandler: nil)
    }

    // MARK: Mensajes de la página (acciones seguras)

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let m = message.body as? [String: Any], let type = m["type"] as? String else { return }
        switch type {
        case "ready":
            ready = true; pushed = ""; push()
        case "close":
            window.close()
        case "openFolder":
            // Solo carpetas: se muestran en el Finder, nunca se abre ni se ejecuta nada (tampoco paquetes como .app).
            guard let raw = m["path"] as? String, raw.hasPrefix("/") else { return }
            let path = (raw as NSString).standardizingPath
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue, !NSWorkspace.shared.isFilePackage(atPath: path) {
                NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
            } else {
                call("window.pancho.toast(t)", ["t": "La carpeta ya no existe en este Mac"])
            }
        default: break
        }
    }

    /// Si el proceso de la página muere, se recarga en vez de dejar la ventana en blanco.
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        ready = false
        loadPage()
    }

    // La ventana solo muestra la página empaquetada: cualquier otra navegación se bloquea.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(action.request.url?.isFileURL == true && action.navigationType == .other ? .allow : .cancel)
    }
}
