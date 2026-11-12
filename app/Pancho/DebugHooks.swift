#if PANCHO_DEBUG
import AppKit

// Solo en la variante de depuración (scripts/build_pancho.sh --debug, -D PANCHO_DEBUG); nunca en la instalable.
// Para comprobación visual en simulación. Inactivo salvo que exista PANCHO_DEBUG_DIR.
// Lee órdenes de <dir>/*.cmd (una por línea) y escribe instantáneas PNG renderizando las capas de
// las propias ventanas de Pancho (no captura la pantalla ni toca el almacén).
//   snapshot NOMBRE | click-pet | reply | collapse | mute | card N | option Q O | text Q TEXTO
//   send | esc | release | confirm-release | copy | quitar | move X Y | key N
final class DebugHooks {
    let dir: URL
    unowned let c: Controller

    init?(controller: Controller) {
        guard let d = ProcessInfo.processInfo.environment["PANCHO_DEBUG_DIR"], !d.isEmpty else { return nil }
        dir = URL(fileURLWithPath: d, isDirectory: true)
        c = controller
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    func tick() {
        guard let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return }
        for f in files.filter({ $0.pathExtension == "cmd" }).sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let text = (try? String(contentsOf: f, encoding: .utf8)) ?? ""
            try? FileManager.default.removeItem(at: f)
            for line in text.split(separator: "\n") { run(String(line)) }
        }
    }

    private func log(_ s: String) {
        let url = dir.appendingPathComponent("log.txt")
        let line = "\(Date().timeIntervalSince1970) \(s)\n"
        if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(Data(line.utf8)); h.closeFile() }
        else { try? line.write(to: url, atomically: true, encoding: .utf8) }
    }

    private func run(_ line: String) {
        let parts = line.split(separator: " ", maxSplits: 3).map(String.init)
        guard let cmd = parts.first else { return }
        log("> " + line)
        switch cmd {
        case "snapshot": snapshot(parts.count > 1 ? parts[1] : "snap")
        case "click-pet": c.petClicked()
        case "reply", "refresh": c.barView.refresh.onClick?()
        case "collapse": c.toggleCollapsed()
        case "mute": c.toggleMute()
        case "dashboard", "agents": c.toggleAgentsPanel()
        case "setup": c.setup.show()
        case "setup-snapshot":
            // Configuración con su estado real (solo lee el estado de la conexión; no instala nada).
            if let w = c.setup.window, let img = image(of: w) {
                try? NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?
                    .write(to: dir.appendingPathComponent((parts.count > 1 ? parts[1] : "setup") + ".png"))
                log("setup-snapshot")
            }
            // Textos visibles (botones y etiquetas), en orden, para leer el estado sin captura de pantalla.
            func walk(_ v: NSView, _ out: inout [String]) {
                if let b = v as? NSButton, !v.isHidden { out.append("[\(b.title)\(b.isEnabled ? "" : " (inactivo)")]") }
                else if let t = v as? NSTextField, !v.isHidden, !t.stringValue.isEmpty { out.append(t.stringValue) }
                v.subviews.forEach { walk($0, &out) }
            }
            var texts: [String] = []
            if let v = c.setup.window?.contentView { walk(v, &texts) }
            log("setup-texts " + texts.joined(separator: " | "))
        case "dash-snapshot": dashSnapshot(parts.count > 1 ? parts[1] : "dashboard")
        case "dash-esc":
            if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0, windowNumber: c.agentsPanel.window.windowNumber,
                                        context: nil, characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}", isARepeat: false, keyCode: 53) {
                NSApp.postEvent(e, atStart: false)
            }
        case "dash-frame":
            // Mueve y cambia el tamaño por código (comprueba que la ventana lo admite y que la página se adapta).
            let n = line.split(separator: " ").dropFirst().compactMap { Double($0) }
            if n.count == 4 { c.agentsPanel.window.setFrame(NSRect(x: n[0], y: n[1], width: n[2], height: n[3]), display: true) }
        case "dash-js":
            // Evalúa JS en la página del panel de agentes (para pulsar filas y botones en las capturas).
            let code = parts.dropFirst().joined(separator: " ")
            c.agentsPanel.web.evaluateJavaScript(code) { [weak self] r, e in self?.log("dash-js → \(r.map { "\($0)" } ?? "nil") \(e.map { "\($0)" } ?? "")") }
        case "card":
            if parts.count > 1, let n = Int(parts[1]), n < c.order.count { c.cardClicked(c.order[n]) }
        case "option":
            if parts.count > 2, let q = Int(parts[1]), let o = Int(parts[2]), q < c.panel.sections.count, o < c.panel.sections[q].rows.count {
                c.panel.sections[q].rows[o].onClick?()
            }
        case "text":
            if parts.count > 2, let q = Int(parts[1]), q < c.panel.sections.count {
                let t = parts.count > 3 ? parts[2] + " " + parts[3] : parts[2]
                let tv = c.panel.sections[q].box.text
                tv.string = t.replacingOccurrences(of: "\\n", with: "\n")
                tv.delegate?.textDidChange?(Notification(name: NSText.didChangeNotification, object: tv))
            }
        case "send": c.panel.sendButton.onClick?()
        case "esc": c.panelHide()
        case "release": c.panel.releaseButton.onClick?()
        case "confirm-release": c.panel.confirmReleaseButton.onClick?()
        case "copy": c.panel.copyButton.onClick?()
        case "quitar": c.panel.dismissButton.onClick?()
        case "move":
            if parts.count > 2, let x = Double(parts[1]), let y = Double(parts[2]) { c.petOrigin = NSPoint(x: x, y: y); c.layout() }
        case "key":
            if parts.count > 1, let n = Int(parts[1]), let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                windowNumber: c.panel.window.windowNumber, context: nil, characters: "\(n)", charactersIgnoringModifiers: "\(n)",
                isARepeat: false, keyCode: UInt16([18, 19, 20, 21, 23, 22, 26, 28, 25][max(0, min(8, n - 1))])) {
                NSApp.postEvent(e, atStart: false)
            }
        default: log("orden desconocida")
        }
    }

    private func image(of w: NSWindow) -> CGImage? {
        guard let v = w.contentView, let layer = v.layer else { return nil }
        let scale: CGFloat = 2
        let size = w.frame.size
        guard let ctx = CGContext(data: nil, width: Int(size.width * scale), height: Int(size.height * scale), bitsPerComponent: 8,
                                  bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.scaleBy(x: scale, y: scale)
        layer.render(in: ctx)
        return ctx.makeImage()
    }

    /// Captura del panel de agentes (la página web no se puede leer con las capas de la ventana).
    func dashSnapshot(_ name: String) {
        let web = c.agentsPanel.web
        web.takeSnapshot(with: nil) { [weak self] img, err in
            guard let self = self, let img = img, let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
                self?.log("dash-snapshot \(name): sin imagen \(err.map { "\($0)" } ?? "")"); return
            }
            let w = cg.width, h = cg.height
            guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
            // Fondo neutro: el cristal de la ventana no forma parte de la página.
            ctx.setFillColor(NSColor(hex: 0xE9ECF2).cgColor); ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
            if let out = ctx.makeImage() {
                try? NSBitmapImageRep(cgImage: out).representation(using: .png, properties: [:])?.write(to: self.dir.appendingPathComponent(name + ".png"))
            }
            let report: [String: Any] = ["frame": NSStringFromRect(self.c.agentsPanel.window.frame), "visible": self.c.agentsPanel.isOpen,
                                         "isKey": self.c.agentsPanel.window.isKeyWindow, "isMovable": self.c.agentsPanel.window.isMovable,
                                         "isResizable": self.c.agentsPanel.window.isResizable, "isZoomable": self.c.agentsPanel.window.isZoomable,
                                         "badge": self.c.barView.agents.mini.isHidden ? "" : self.c.barView.agents.toolTip ?? ""]
            if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
                try? data.write(to: self.dir.appendingPathComponent(name + ".json"))
            }
            self.log("dash-snapshot \(name)")
        }
    }

    func snapshot(_ name: String) {
        let wins: [(String, NSWindow)] = [("lista", c.listWindow), ("barra", c.barWindow), ("panel", c.panel.window), ("pancho", c.petWindow)]
        let visible = wins.filter { $0.1.isVisible }
        guard let first = visible.first, let scr = c.petWindow.screen ?? NSScreen.main ?? NSScreen.screens.first else {
            log("snapshot \(name): sin ventanas visibles o sin pantalla"); return
        }
        var box = visible.map { $0.1.frame }.reduce(first.1.frame) { $0.union($1) }.insetBy(dx: -30, dy: -30)
        box = box.intersection(scr.frame.insetBy(dx: -1, dy: -1))
        let scale: CGFloat = 2
        guard let ctx = CGContext(data: nil, width: Int(box.width * scale), height: Int(box.height * scale), bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
        ctx.scaleBy(x: scale, y: scale)
        // Fondo de escritorio neutro (las superficies translúcidas no se componen sobre el escritorio real).
        if let grad = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                 colors: [NSColor(hex: 0xdfe6f2).cgColor, NSColor(hex: 0xf3e2d4).cgColor] as CFArray, locations: [0, 1]) {
            ctx.drawLinearGradient(grad, start: .zero, end: CGPoint(x: box.width, y: box.height), options: [])
        }
        // Borde del área útil (visibleFrame) si cae dentro.
        let vf = scr.visibleFrame
        ctx.setStrokeColor(NSColor(white: 0, alpha: 0.25).cgColor); ctx.setLineDash(phase: 0, lengths: [4, 4]); ctx.setLineWidth(1)
        ctx.stroke(CGRect(x: vf.minX - box.minX, y: vf.minY - box.minY, width: vf.width, height: vf.height))
        ctx.setLineDash(phase: 0, lengths: [])
        var report: [String: Any] = ["screen": NSStringFromRect(scr.frame), "visibleFrame": NSStringFromRect(vf), "box": NSStringFromRect(box)]
        for (n, w) in visible {
            let f = w.frame
            report[n] = NSStringFromRect(f)
            // sombra aproximada para leer las superficies
            if n != "pancho" {
                ctx.saveGState()
                ctx.setShadow(offset: CGSize(width: 0, height: -6), blur: 18, color: NSColor(white: 0, alpha: 0.18).cgColor)
                if let img = image(of: w) { ctx.draw(img, in: CGRect(x: f.minX - box.minX, y: f.minY - box.minY, width: f.width, height: f.height)) }
                ctx.restoreGState()
            } else if let img = image(of: w) {
                ctx.draw(img, in: CGRect(x: f.minX - box.minX, y: f.minY - box.minY, width: f.width, height: f.height))
            }
        }
        report["agentsTip"] = c.barView.agents.toolTip ?? ""
        report["mode"] = c.mode == .down ? "down" : "up"
        report["order"] = c.order
        report["panelPhase"] = "\(c.panel.phase)"
        report["panelOpen"] = c.panel.rec?.key ?? NSNull()
        report["petIgnoresMouse"] = c.petWindow.ignoresMouseEvents
        report["sprite"] = c.animator.current ?? "-"
        report["collectionBehavior"] = c.petWindow.collectionBehavior.rawValue
        report["panelIsKey"] = c.panel.window.isKeyWindow
        report["appActive"] = NSApp.isActive
        if let img = ctx.makeImage() {
            let rep = NSBitmapImageRep(cgImage: img)
            try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent(name + ".png"))
        }
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: dir.appendingPathComponent(name + ".json"))
        }
        log("snapshot \(name)")
    }
}
#endif
