import AppKit
import ServiceManagement

// Ventana de bienvenida y configuración. Sin cuentas ni claves: Pancho solo se conecta a las apps
// de Claude y Codex de este Mac instalando (o retirando) sus propios hooks con el instalador que
// viaja dentro de la app. En modo de prueba (PANCHO_HOME) no toca nada real.

struct InstallerResult {
    let ok: Bool
    let json: [String: Any]
    let error: String?
}

enum InstallerRunner {
    static var script: URL? {
        guard let url = Bundle.main.resourceURL?.appendingPathComponent("bridge-src/pancho_install.py"),
              FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }

    private static var realRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Pancho", isDirectory: true)
    }

    /// Intérprete: el que ya usa la instalación (para no cambiar los hooks) o el primero disponible.
    /// Se evita `/usr/bin/python3`: sin las herramientas de Apple solo abre un diálogo de instalación.
    static func python() -> String? {
        let fm = FileManager.default
        for name in ["manifest.json", "codex-manifest.json"] {
            let url = realRoot.appendingPathComponent("install/" + name)
            if let data = try? Data(contentsOf: url),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let py = (obj["python"] as? [String: Any])?["path"] as? String, fm.isExecutableFile(atPath: py) {
                return py
            }
        }
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                          "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
                          "/Library/Developer/CommandLineTools/usr/bin/python3",
                          "/Applications/Xcode.app/Contents/Developer/usr/bin/python3"]
        return candidates.first { fm.isExecutableFile(atPath: $0) }
    }

    static func run(_ action: String, codex: Bool, completion: @escaping (InstallerResult) -> Void) {
        guard let script = script else {
            completion(InstallerResult(ok: false, json: [:], error: "Esta copia de Pancho no incluye el instalador.")); return
        }
        guard let py = python() else {
            completion(InstallerResult(ok: false, json: [:], error: "No encuentro Python 3 en este Mac. Instálalo (por ejemplo desde python.org) y vuelve a probar.")); return
        }
        var args = ["-I", "-B", script.path, action]
        if codex { args.append("--codex") }
        if action == "install" { args += ["--python", py] }
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = args
            var env = ProcessInfo.processInfo.environment
            env.removeValue(forKey: "PANCHO_HOME")
            p.environment = env
            let out = Pipe()
            p.standardOutput = out
            p.standardError = FileHandle.nullDevice
            p.standardInput = FileHandle.nullDevice
            let result: InstallerResult
            do {
                try p.run()
                let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + 90, execute: killer)
                let data = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                killer.cancel()
                if let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                    let ok = (obj["ok"] as? Bool) ?? false
                    result = InstallerResult(ok: ok, json: obj, error: ok ? nil : (obj["error"] as? String ?? "El instalador no pudo terminar."))
                } else {
                    result = InstallerResult(ok: false, json: [:], error: "El instalador no respondió (código \(p.terminationStatus)).")
                }
            } catch {
                result = InstallerResult(ok: false, json: [:], error: "No se pudo ejecutar el instalador.")
            }
            DispatchQueue.main.async { completion(result) }
        }
    }
}

private enum LinkState { case checking, missing, off, on, broken, test }

private final class ConnectionRow: NSView {
    let dot = NSView()
    let title = NSTextField(labelWithString: "")
    let detail = NSTextField(labelWithString: "")
    let button = NSButton(title: "Conectar", target: nil, action: nil)
    var state: LinkState = .checking { didSet { apply() } }
    var busy = false { didSet { apply() } }
    private let name: String

    init(name: String) {
        self.name = name
        super.init(frame: .zero)
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 5
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.stringValue = name
        detail.font = .systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        button.bezelStyle = .rounded
        let texts = NSStackView(views: [title, detail])
        texts.orientation = .vertical; texts.alignment = .leading; texts.spacing = 2
        let row = NSStackView(views: [dot, texts, NSView(), button])
        row.orientation = .horizontal; row.alignment = .centerY; row.spacing = 10
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        NSLayoutConstraint.activate([
            dot.widthAnchor.constraint(equalToConstant: 10), dot.heightAnchor.constraint(equalToConstant: 10),
            row.leadingAnchor.constraint(equalTo: leadingAnchor), row.trailingAnchor.constraint(equalTo: trailingAnchor),
            row.topAnchor.constraint(equalTo: topAnchor), row.bottomAnchor.constraint(equalTo: bottomAnchor),
            button.widthAnchor.constraint(greaterThanOrEqualToConstant: 104),
        ])
        apply()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func apply() {
        var color = NSColor.tertiaryLabelColor
        var text = "Comprobando…", label = "Conectar", enabled = false
        switch state {
        case .checking: break
        case .test: text = "Modo de prueba: no se toca la instalación real"
        case .missing: text = "No encuentro \(name) en este Mac"
        case .off: text = "Sin conectar"; enabled = true
        case .on: color = .systemGreen; text = "Conectado"; label = "Desconectar"; enabled = true
        case .broken: color = .systemOrange; text = "Conexión incompleta"; label = "Reparar"; enabled = true
        }
        if busy { text = "Un momento…"; enabled = false }
        dot.layer?.backgroundColor = color.cgColor
        detail.stringValue = text
        button.title = label
        button.isEnabled = enabled
    }
}

/// Guía de Codex: Codex solo ejecuta los avisos de Pancho cuando su dueño los aprueba dentro de
/// Codex. Pancho no puede hacerlo por él; aquí se le explica paso a paso y se comprueba en vivo
/// (llega el primer aviso de Codex) que ha funcionado. Solo abre Codex; no pulsa nada en él.
private final class CodexGuide: NSObject {
    private let sessionsDir: URL
    private let status = NSTextField(wrappingLabelWithString: "")
    private let spinner = NSProgressIndicator()
    private var panel: NSPanel?
    private var timer: Timer?
    private var since = Date()

    init(sessionsDir: URL) { self.sessionsDir = sessionsDir; super.init() }

    private func step(_ n: Int, _ title: String, _ text: String) -> NSView {
        let num = NSTextField(labelWithString: "\(n)")
        num.font = .systemFont(ofSize: 13, weight: .bold)
        num.alignment = .center
        num.textColor = .white
        num.wantsLayer = true
        num.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        num.layer?.cornerRadius = 11
        num.widthAnchor.constraint(equalToConstant: 22).isActive = true
        num.heightAnchor.constraint(equalToConstant: 22).isActive = true
        let t = NSTextField(labelWithString: title)
        t.font = .systemFont(ofSize: 13, weight: .semibold)
        let d = NSTextField(wrappingLabelWithString: text)
        d.font = .systemFont(ofSize: 12)
        d.textColor = .secondaryLabelColor
        d.preferredMaxLayoutWidth = 330
        let texts = NSStackView(views: [t, d])
        texts.orientation = .vertical; texts.alignment = .leading; texts.spacing = 2
        let row = NSStackView(views: [num, texts])
        row.orientation = .horizontal; row.alignment = .top; row.spacing = 10
        return row
    }

    func show(on parent: NSWindow?, entries: Int) {
        guard let parent = parent, panel == nil else { return }
        let title = NSTextField(labelWithString: "Último paso: da tu visto bueno en Codex")
        title.font = .systemFont(ofSize: 16, weight: .bold)
        let intro = NSTextField(wrappingLabelWithString: "Pancho ya está instalado, pero Codex no le avisa de nada hasta que tú lo apruebes dentro de Codex. Es un minuto y solo se hace una vez.")
        intro.font = .systemFont(ofSize: 12)
        intro.textColor = .secondaryLabelColor
        intro.preferredMaxLayoutWidth = 362
        let count = entries > 0 ? "las \(entries) entradas" : "las entradas"
        let open = NSButton(title: "Abrir Codex", target: self, action: #selector(openCodex))
        open.bezelStyle = .rounded
        let done = NSButton(title: "Cerrar", target: self, action: #selector(close))
        done.bezelStyle = .rounded
        done.keyEquivalent = "\r"
        status.font = .systemFont(ofSize: 12, weight: .medium)
        status.preferredMaxLayoutWidth = 330
        spinner.style = .spinning
        spinner.controlSize = .small
        let statusRow = NSStackView(views: [spinner, status])
        statusRow.orientation = .horizontal; statusRow.alignment = .centerY; statusRow.spacing = 8
        let buttons = NSStackView(views: [open, NSView(), done])
        buttons.orientation = .horizontal
        let sep = NSBox(); sep.boxType = .separator
        let rows: [NSView] = [
            title, intro,
            step(1, "Abre Codex", "Pulsa «Abrir Codex» aquí abajo, o ábrelo como siempre."),
            step(2, "Pulsa «Review hooks»", "Aparece junto al cuadro donde escribes. Si no lo ves, entra en Settings > Hooks."),
            step(3, "Aprueba \(count) de Pancho", "Son las que terminan en «pancho_hook.py … --provider codex». Apruébalas todas; las demás no hace falta tocarlas."),
            step(4, "Escribe algo en un chat nuevo", "Cualquier mensaje vale. En cuanto Codex avise a Pancho, aquí abajo saldrá la confirmación."),
            sep, statusRow, buttons]
        let stack = NSStackView(views: rows)
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 22, bottom: 16, right: 22)
        stack.setCustomSpacing(4, after: title)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(stack)
        var cons = [stack.leadingAnchor.constraint(equalTo: content.leadingAnchor), stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                    stack.topAnchor.constraint(equalTo: content.topAnchor), stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
                    stack.widthAnchor.constraint(equalToConstant: 406)]
        for v in rows { cons.append(v.widthAnchor.constraint(equalToConstant: 362)) }
        NSLayoutConstraint.activate(cons)
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 406, height: 400), styleMask: [.titled], backing: .buffered, defer: false)
        p.contentView = content
        p.setContentSize(content.fittingSize)
        panel = p
        since = Date()
        waiting()
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in self?.check() }
        parent.beginSheet(p) { [weak self] _ in
            self?.timer?.invalidate(); self?.timer = nil; self?.panel = nil
        }
    }

    private func waiting() {
        spinner.isHidden = false
        spinner.startAnimation(nil)
        status.textColor = .secondaryLabelColor
        status.stringValue = "Esperando el primer aviso de Codex…"
    }

    /// ¿Algún aviso de Codex desde que se abrió la guía? Solo se mira proveedor y hora del registro.
    private func check() {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: sessionsDir, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles]) else { return }
        let floor = since.timeIntervalSince1970
        for f in files where f.pathExtension == "json" {
            guard let m = (try? f.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate, m >= since,
                  let data = try? Data(contentsOf: f),
                  let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  (o["provider"] as? String) == "codex",
                  ((o["last_event_at"] as? NSNumber)?.doubleValue ?? 0) >= floor else { continue }
            timer?.invalidate(); timer = nil
            spinner.stopAnimation(nil)
            spinner.isHidden = true
            status.textColor = .systemGreen
            status.stringValue = "✓ Listo: Codex ya avisa a Pancho. Puedes cerrar esta ventana."
            return
        }
    }

    @objc private func openCodex() {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") {
            NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
        } else {
            status.stringValue = "No encuentro la app de Codex; ábrela tú como siempre."
        }
    }

    @objc private func close() {
        guard let p = panel, let parent = p.sheetParent else { return }
        parent.endSheet(p)
    }
}

final class SetupWindow: NSObject, NSWindowDelegate {
    private unowned let c: Controller
    private var window: NSWindow?
    private let claude = ConnectionRow(name: "Claude")
    private let codex = ConnectionRow(name: "Codex")
    private let loginBox = NSButton(checkboxWithTitle: "Abrir Pancho al iniciar sesión", target: nil, action: nil)
    private let autoOpenBox = NSButton(checkboxWithTitle: "Abrir las preguntas en cuanto lleguen", target: nil, action: nil)
    private let animBox = NSButton(checkboxWithTitle: "Animaciones de Pancho", target: nil, action: nil)
    private let sizePopup = NSPopUpButton()
    private let waitPopup = NSPopUpButton()
    private let message = NSTextField(wrappingLabelWithString: "")
    private let waitChoices = [5, 15, 30]
    private let guideButton = NSButton(title: "Ver los pasos de Codex…", target: nil, action: nil)
    private lazy var guide = CodexGuide(sessionsDir: c.store.sessionsDir)
    private var codexEntries = 0

    init(controller: Controller) { c = controller; super.init() }

    func show() {
        if window == nil { build() }
        sync()
        refreshConnections()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func syncIfVisible() { if window?.isVisible == true { sync() } }

    // MARK: construcción

    private func heading(_ text: String) -> NSTextField {
        let l = NSTextField(labelWithString: text.uppercased())
        l.font = .systemFont(ofSize: 11, weight: .semibold)
        l.textColor = .secondaryLabelColor
        return l
    }

    private func note(_ text: String) -> NSTextField {
        let l = NSTextField(wrappingLabelWithString: text)
        l.font = .systemFont(ofSize: 11)
        l.textColor = .secondaryLabelColor
        return l
    }

    private func labeled(_ text: String, _ control: NSView) -> NSStackView {
        let l = NSTextField(labelWithString: text)
        l.font = .systemFont(ofSize: 13)
        let row = NSStackView(views: [l, NSView(), control])
        row.orientation = .horizontal; row.alignment = .centerY
        return row
    }

    private func build() {
        let width: CGFloat = 440
        let hello = NSTextField(labelWithString: "Hola, soy Pancho")
        hello.font = .systemFont(ofSize: 20, weight: .bold)
        let sub = note("Te aviso de las preguntas de Claude y Codex y puedes contestarlas desde aquí. No necesito cuentas ni claves: solo me conecto a las apps que ya tienes en este Mac.")
        let headTexts = NSStackView(views: [hello, sub])
        headTexts.orientation = .vertical; headTexts.alignment = .leading; headTexts.spacing = 4
        let face = NSImageView()
        face.image = panchoFace(c.library, height: 64)
        face.imageScaling = .scaleProportionallyUpOrDown
        face.widthAnchor.constraint(equalToConstant: 64).isActive = true
        face.heightAnchor.constraint(equalToConstant: 64).isActive = true
        let header = NSStackView(views: [face, headTexts])
        header.orientation = .horizontal; header.alignment = .centerY; header.spacing = 14

        claude.button.target = self; claude.button.action = #selector(claudeTapped)
        codex.button.target = self; codex.button.action = #selector(codexTapped)
        let codexNote = note("Codex pide además tu visto bueno dentro de Codex. Solo puedes darlo tú, pero te guío paso a paso:")
        guideButton.bezelStyle = .inline
        guideButton.target = self; guideButton.action = #selector(showGuide)

        for (box, sel) in [(loginBox, #selector(loginToggled)), (autoOpenBox, #selector(autoOpenToggled)), (animBox, #selector(animToggled))] {
            box.target = self; box.action = sel
        }
        sizePopup.addItems(withTitles: PetSize.allCases.map(\.title))
        sizePopup.target = self; sizePopup.action = #selector(sizeChanged)
        waitPopup.addItems(withTitles: waitChoices.map { "\($0) minutos" })
        waitPopup.target = self; waitPopup.action = #selector(waitChanged)
        let autoNote = note("Si está desactivado, Pancho solo avisa y la pregunta se abre al pulsar su tarjeta.")
        let waitNote = note("Pasado ese tiempo sin contestar en Pancho, la pregunta vuelve a su app.")

        message.font = .systemFont(ofSize: 11)
        message.textColor = .secondaryLabelColor
        let done = NSButton(title: "Listo", target: self, action: #selector(close))
        done.bezelStyle = .rounded
        done.keyEquivalent = "\r"
        message.preferredMaxLayoutWidth = 320
        message.setContentHuggingPriority(.defaultLow, for: .horizontal)
        message.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        done.setContentHuggingPriority(.required, for: .horizontal)
        let footer = NSStackView(views: [message, done])
        footer.orientation = .horizontal; footer.alignment = .centerY

        let sep1 = NSBox(); sep1.boxType = .separator
        let sep2 = NSBox(); sep2.boxType = .separator
        let rows: [NSView] = [header, sep1, heading("Conexiones"), claude, codex, codexNote, guideButton, sep2, heading("Ajustes"),
                              loginBox, autoOpenBox, autoNote, animBox, labeled("Tamaño de Pancho", sizePopup),
                              labeled("Esperar la respuesta", waitPopup), waitNote, footer]
        let stack = NSStackView(views: rows)
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 22, bottom: 18, right: 22)
        stack.setCustomSpacing(16, after: header)
        stack.setCustomSpacing(4, after: autoOpenBox)
        stack.setCustomSpacing(4, after: codexNote)
        stack.setCustomSpacing(4, after: rows[14])
        stack.setCustomSpacing(16, after: waitNote)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(stack)
        var cons = [stack.leadingAnchor.constraint(equalTo: content.leadingAnchor), stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                    stack.topAnchor.constraint(equalTo: content.topAnchor), stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
                    stack.widthAnchor.constraint(equalToConstant: width)]
        for v in rows where !(v is NSButton) { cons.append(v.widthAnchor.constraint(equalToConstant: width - 44)) }
        NSLayoutConstraint.activate(cons)

        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: 520), styleMask: [.titled, .closable],
                         backing: .buffered, defer: false)
        w.title = "Pancho"
        w.isReleasedWhenClosed = false
        w.contentView = content
        w.setContentSize(content.fittingSize)
        w.center()
        w.delegate = self
        window = w
    }

    // MARK: estado

    private func sync() {
        autoOpenBox.state = c.autoOpen ? .on : .off
        animBox.state = c.muted ? .off : .on
        sizePopup.selectItem(at: PetSize.allCases.firstIndex(of: Prefs.size) ?? 1)
        let minutes = c.store.readWaitMinutes()
        waitPopup.selectItem(at: waitChoices.lastIndex(where: { $0 <= minutes }) ?? 0)
        if c.testMode {
            loginBox.isEnabled = false; loginBox.state = .off
        } else {
            loginBox.state = SMAppService.mainApp.status == .enabled ? .on : .off
        }
    }

    private func detected(codex: Bool) -> Bool {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        if codex {
            return NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") != nil
                || fm.fileExists(atPath: home.appendingPathComponent(".codex").path)
        }
        return fm.fileExists(atPath: home.appendingPathComponent(".claude").path)
            || fm.fileExists(atPath: "/Applications/Claude.app")
    }

    private func linkState(_ r: InstallerResult) -> LinkState? {
        guard r.ok else { return nil }
        guard (r.json["installed"] as? Bool) == true else { return .off }
        let entries = (r.json["entries"] as? [String: Any]) ?? [:]
        let whole = !entries.isEmpty && entries.values.allSatisfy { ($0 as? String) == "presente" }
            && (r.json["bridge_present"] as? Bool) != false && (r.json["python_present"] as? Bool) != false
        if let pin = r.json["pin_check"] as? String, pin != "ok" { return .broken }
        return whole ? .on : .broken
    }

    private func refreshConnections() {
        for (row, isCodex) in [(claude, false), (codex, true)] {
            if c.testMode { row.state = .test; continue }
            if row.busy { continue }
            row.state = .checking
            InstallerRunner.run("status", codex: isCodex) { [weak self] r in
                guard let self = self else { return }
                if let s = self.linkState(r) {
                    if isCodex {
                        if let n = ((r.json["review"] as? [String: Any])?["entries"] as? [Any])?.count, n > 0 { self.codexEntries = n }
                        self.guideButton.isEnabled = s == .on || s == .broken
                    }
                    row.state = (s == .off && !self.detected(codex: isCodex)) ? .missing : s
                } else {
                    row.state = self.detected(codex: isCodex) ? .off : .missing
                    row.button.isEnabled = false
                    self.say(r.error ?? "No se pudo comprobar la conexión.")
                }
            }
        }
    }

    private func say(_ text: String) { message.stringValue = text }

    private func tapped(_ row: ConnectionRow, codex isCodex: Bool) {
        let name = isCodex ? "Codex" : "Claude"
        let action: String
        if row.state == .on {
            let a = NSAlert()
            a.messageText = "¿Desconectar Pancho de \(name)?"
            a.informativeText = "Se quitan solo los avisos que instaló Pancho; el resto de tu configuración de \(name) no se toca. Las preguntas volverán a salir únicamente en \(name)."
            a.addButton(withTitle: "Desconectar")
            a.addButton(withTitle: "Cancelar")
            guard a.runModal() == .alertFirstButtonReturn else { return }
            action = "uninstall"
        } else {
            action = "install"
        }
        row.busy = true
        say("")
        InstallerRunner.run(action, codex: isCodex) { [weak self] r in
            guard let self = self else { return }
            row.busy = false
            if r.ok {
                if action == "uninstall" { self.say("Pancho ya no está conectado a \(name).") }
                else if isCodex {
                    self.say("Instalado. Falta tu visto bueno dentro de Codex.")
                    if let n = ((r.json["review"] as? [String: Any])?["entries"] as? [Any])?.count, n > 0 { self.codexEntries = n }
                    self.guide.show(on: self.window, entries: self.codexEntries)
                }
                else { self.say("Conectado. Las conversaciones nuevas de Claude ya avisan a Pancho.") }
            } else {
                self.say(r.error ?? "No se pudo completar.")
            }
            self.refreshConnections()
        }
    }

    // MARK: acciones

    @objc private func claudeTapped() { tapped(claude, codex: false) }
    @objc private func codexTapped() { tapped(codex, codex: true) }
    @objc private func showGuide() { guide.show(on: window, entries: codexEntries) }

    @objc private func loginToggled() {
        guard !c.testMode else { return }
        do {
            if loginBox.state == .on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            say(loginBox.state == .on ? "Pancho se abrirá al iniciar sesión." : "Pancho ya no se abre solo.")
        } catch {
            say("macOS no dejó cambiar el inicio automático. Puedes hacerlo en Ajustes del Sistema > General > Ítems de inicio.")
        }
        sync()
    }

    @objc private func autoOpenToggled() { if (autoOpenBox.state == .on) != c.autoOpen { c.toggleAutoOpen() } }
    @objc private func animToggled() { if (animBox.state == .off) != c.muted { c.toggleMute() } }
    @objc private func sizeChanged() {
        let i = sizePopup.indexOfSelectedItem
        if PetSize.allCases.indices.contains(i) { c.setSize(PetSize.allCases[i]) }
    }
    @objc private func waitChanged() {
        let i = waitPopup.indexOfSelectedItem
        guard waitChoices.indices.contains(i) else { return }
        do { try c.store.writeWaitMinutes(waitChoices[i]); say("Vale: \(waitChoices[i]) minutos para las próximas preguntas.") }
        catch { say("No se pudo guardar el tiempo de espera.") }
    }
    @objc private func close() { window?.close() }
}
