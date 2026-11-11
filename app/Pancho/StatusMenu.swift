import AppKit

/// Cara de Pancho (parte alta del fotograma en reposo) para la barra de menús y la ventana de configuración.
func panchoFace(_ library: SpriteLibrary, height: CGFloat) -> NSImage? {
    guard let idle = library.animations["idle"], let image = idle.frames.first, let m = idle.masks.first else { return nil }
    func solid(_ r: Int, _ c: Int) -> Bool { m.bytes[r * m.width + c] > 40 }
    var top = -1, bottom = -1
    for r in 0..<m.height where (0..<m.width).contains(where: { solid(r, $0) }) {
        if top < 0 { top = r }
        bottom = r
    }
    guard top >= 0 else { return nil }
    let chin = top + Int(Double(bottom - top) * 0.47)
    var left = m.width, right = -1
    for r in top...chin { for c in 0..<m.width where solid(r, c) { left = min(left, c); right = max(right, c) } }
    guard right >= left else { return nil }
    let rect = CGRect(x: left, y: top, width: right - left + 1, height: chin - top + 1)
    guard let crop = image.cropping(to: rect) else { return nil }
    return NSImage(cgImage: crop, size: NSSize(width: (height * rect.width / rect.height).rounded(), height: height))
}

/// Icono de Pancho en la barra superior del Mac, con los ajustes de uso diario.
final class StatusMenu: NSObject, NSMenuDelegate {
    private unowned let c: Controller
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()

    init(controller: Controller) {
        c = controller
        super.init()
        if let button = item.button {
            if let face = panchoFace(controller.library, height: 18) {
                button.image = face
                button.imagePosition = .imageLeading
            } else {
                button.title = "Pancho"
            }
            button.setAccessibilityLabel("Pancho")
        }
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
    }

    /// Número de preguntas pendientes junto a la cara (nada si no hay ninguna).
    func setCount(_ n: Int) {
        guard let button = item.button else { return }
        let title = n > 0 ? " \(n)" : ""
        if button.image != nil, button.title != title { button.title = title }
        button.toolTip = n == 0 ? "Pancho: nada pendiente" : n == 1 ? "Pancho: 1 pregunta pendiente" : "Pancho: \(n) preguntas pendientes"
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let n = c.pendingRecords.count
        let open = add(n == 0 ? "No hay preguntas pendientes" : n == 1 ? "Abrir la pregunta pendiente" : "Abrir la siguiente pregunta (\(n))", #selector(openNext))
        open.isEnabled = n > 0
        add("Actualizar conversaciones", #selector(refresh))
        menu.addItem(.separator())
        add("Mostrar conversaciones", #selector(toggleCollapsed)).state = c.collapsed ? .off : .on
        add("Animaciones", #selector(toggleMute)).state = c.muted ? .off : .on
        add("Abrir las preguntas al llegar", #selector(toggleAutoOpen)).state = c.autoOpen ? .on : .off
        let sizes = NSMenu()
        for (i, s) in PetSize.allCases.enumerated() {
            let it = NSMenuItem(title: s.title, action: #selector(pickSize(_:)), keyEquivalent: "")
            it.target = self; it.tag = i; it.state = Prefs.size == s ? .on : .off
            sizes.addItem(it)
        }
        let size = NSMenuItem(title: "Tamaño de Pancho", action: nil, keyEquivalent: "")
        size.submenu = sizes
        menu.addItem(size)
        menu.addItem(.separator())
        add("Configuración…", #selector(showSetup))
        menu.addItem(.separator())
        add("Salir de Pancho", #selector(quit))
    }

    @discardableResult
    private func add(_ title: String, _ action: Selector) -> NSMenuItem {
        let it = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
        it.target = self
        return it
    }

    @objc private func openNext() { c.openOldest(waveIfNone: false) }
    @objc private func refresh() { c.refreshConversations() }
    @objc private func toggleCollapsed() { c.toggleCollapsed() }
    @objc private func toggleMute() { c.toggleMute() }
    @objc private func toggleAutoOpen() { c.toggleAutoOpen() }
    @objc private func pickSize(_ sender: NSMenuItem) { c.setSize(PetSize.allCases[sender.tag]) }
    @objc private func showSetup() { c.showSetup() }
    @objc private func quit() { c.quit() }
}
