import AppKit

// Medidas en puntos de macOS (design/pancho-flotante/DISEÑO.md §1).
enum M {
    /// Tamaño de Pancho elegido en el menú (pequeño, normal o grande). El resto de medidas no cambia.
    static var petScale: CGFloat = Prefs.size.scale
    static var petW: CGFloat { (96 * petScale).rounded() }
    static var petH: CGFloat { (104 * petScale).rounded() }
    static let barW: CGFloat = 159, barH: CGFloat = 40, barSimH: CGFloat = 52
    static let barOverlap: CGFloat = 4
    static let gapBarList: CGFloat = 12
    static let cardW: CGFloat = 330, cardH: CGFloat = 56, cardGap: CGFloat = 10
    static let maxVisibleCards = 5
    static let panelW: CGFloat = 360, panelMaxH: CGFloat = 560, panelGap: CGFloat = 12
    static let edge: CGFloat = 8
    static let dragThreshold: CGFloat = 4
}

extension NSColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
                  blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
    }
}

enum C {
    static let text = NSColor(hex: 0x1d1d1f), text2 = NSColor(hex: 0x6e6e73), text3 = NSColor(hex: 0x8e8e93)
    static let blue = NSColor(hex: 0x007AFF), blueText = NSColor(hex: 0x0068DA), blueDeep = NSColor(hex: 0x0062CC)
    static let orangeText = NSColor(hex: 0xC26A00)
    static let green = NSColor(hex: 0x34C759), greenText = NSColor(hex: 0x248A3D)
    static let red = NSColor(hex: 0xFF3B30), redText = NSColor(hex: 0xD70015), redDeep = NSColor(hex: 0xA3121B)
    static let claude = NSColor(hex: 0xD97757), codex = NSColor(hex: 0x3A3A3C)
    static let hairline = NSColor(white: 0, alpha: 0.08)
    static let disabled = NSColor(hex: 0xb0b0b5)
    // Capa blanca sobre NSVisualEffectView para acercarse a surface 80 % / hover 94 % / panel 88 %.
    static let surface: CGFloat = 0.55, surfaceHover: CGFloat = 0.9, surfacePanel: CGFloat = 0.72
}

enum F {
    static func sys(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont { .systemFont(ofSize: size, weight: weight) }
}

func providerName(_ p: String) -> String { p == "codex" ? "Codex" : "Claude Code" }
func providerColor(_ p: String) -> NSColor { p == "codex" ? C.codex : C.claude }
/// Nombre corto en las tarjetas, junto al proyecto: «Claude» o «Codex».
func providerShortName(_ p: String) -> String { p == "codex" ? "Codex" : "Claude" }

class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

/// Vista que deja pasar el ratón a sus hermanas/padre (solo decoración).
final class PassView: NSView {
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

// MARK: - Ventana flotante (lógica de NSPanel verificada en src/Popup.swift)

final class FloatingPanel: NSPanel {
    var allowKey = false
    override var canBecomeKey: Bool { allowKey }
    override var canBecomeMain: Bool { false }

    init(size: NSSize, shadow: Bool) {
        super.init(contentRect: NSRect(origin: .zero, size: size),
                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = shadow
        level = .floating
        isFloatingPanel = true
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
        isMovable = false
        worksWhenModal = true
        becomesKeyOnlyIfNeeded = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        if #available(macOS 13.0, *) { collectionBehavior.insert(.canJoinAllApplications) }
        appearance = NSAppearance(named: .aqua) // el diseño aprobado es claro
    }
}

// MARK: - Superficie translúcida

func roundedMask(_ r: CGFloat) -> NSImage {
    let side = r * 2 + 2
    let img = NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
        NSColor.black.setFill()
        NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r).fill()
        return true
    }
    img.capInsets = NSEdgeInsets(top: r, left: r, bottom: r, right: r)
    img.resizingMode = .stretch
    return img
}

/// NSVisualEffectView con esquinas redondeadas + capa blanca + filete. Los hijos van en `content` (invertida).
final class GlassView: NSView {
    let effect = NSVisualEffectView()
    let tint = NSView()
    let content = FlippedView()
    var radius: CGFloat { didSet { applyShape() } }
    var tintAlpha: CGFloat { didSet { tint.layer?.backgroundColor = NSColor.white.withAlphaComponent(tintAlpha).cgColor } }
    var rimColor: NSColor? { didSet { applyRim() } }
    var rimWidth: CGFloat = 1 { didSet { applyRim() } }

    init(radius: CGFloat, tint alpha: CGFloat) {
        self.radius = radius
        self.tintAlpha = alpha
        super.init(frame: .zero)
        wantsLayer = true
        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.autoresizingMask = [.width, .height]
        tint.wantsLayer = true
        tint.autoresizingMask = [.width, .height]
        content.autoresizingMask = [.width, .height]
        addSubview(effect)
        addSubview(tint)
        addSubview(content)
        tint.layer?.backgroundColor = NSColor.white.withAlphaComponent(alpha).cgColor
        applyShape()
        applyRim()
    }
    required init?(coder: NSCoder) { fatalError() }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        effect.frame = bounds; tint.frame = bounds; content.frame = bounds
    }
    private func applyShape() {
        effect.maskImage = roundedMask(radius)
        tint.layer?.cornerRadius = radius
    }
    private func applyRim() {
        tint.layer?.borderColor = (rimColor ?? C.hairline).cgColor
        tint.layer?.borderWidth = rimColor == nil ? 0.5 : rimWidth
    }
}

// MARK: - Iconos (trazados de los SVG del prototipo)

enum Icon { case pencil, refresh, wave, waveOff, chevronDown, chevronRight, x, check, question, bang, dashboard }

extension Icon {
    var box: CGFloat {
        switch self { case .pencil, .refresh, .wave, .waveOff, .chevronDown, .dashboard: return 24; default: return 16 }
    }
    /// Dibuja en un contexto invertido (y hacia abajo).
    func draw(in r: NSRect, color: NSColor, rotated: Bool = false) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        let s = min(r.width, r.height) / box
        ctx.saveGState()
        ctx.translateBy(x: r.minX + (r.width - box * s) / 2, y: r.minY + (r.height - box * s) / 2)
        ctx.scaleBy(x: s, y: s)
        if rotated {
            ctx.translateBy(x: box / 2, y: box / 2); ctx.rotate(by: .pi); ctx.translateBy(x: -box / 2, y: -box / 2)
        }
        color.set()
        func P(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: x, y: y) }
        func stroke(_ pts: [NSPoint], _ w: CGFloat, close: Bool = false) {
            let p = NSBezierPath(); p.move(to: pts[0]); for q in pts.dropFirst() { p.line(to: q) }
            if close { p.close() }
            p.lineWidth = w; p.lineCapStyle = .round; p.lineJoinStyle = .round; p.stroke()
        }
        func dot(_ c: NSPoint, _ rad: CGFloat) {
            NSBezierPath(ovalIn: NSRect(x: c.x - rad, y: c.y - rad, width: rad * 2, height: rad * 2)).fill()
        }
        switch self {
        case .pencil:
            let p = NSBezierPath()
            p.move(to: P(11, 4.5)); p.line(to: P(7.5, 4.5))
            p.appendArc(from: P(4.5, 4.5), to: P(4.5, 7.5), radius: 3); p.line(to: P(4.5, 16.5))
            p.appendArc(from: P(4.5, 19.5), to: P(7.5, 19.5), radius: 3); p.line(to: P(16.5, 19.5))
            p.appendArc(from: P(19.5, 19.5), to: P(19.5, 16.5), radius: 3); p.line(to: P(19.5, 13))
            p.lineWidth = 1.8; p.lineCapStyle = .round; p.lineJoinStyle = .round; p.stroke()
            stroke([P(17.3, 3.9), P(20, 6.6), P(12.4, 14.2), P(9, 15), P(9.8, 11.6)], 1.8, close: true)
        case .refresh:
            // Flecha circular: arco abierto arriba a la derecha y punta en esa esquina.
            let p = NSBezierPath()
            p.appendArc(withCenter: P(12, 12), radius: 7, startAngle: 40, endAngle: 330, clockwise: false)
            p.lineWidth = 1.8; p.lineCapStyle = .round; p.lineJoinStyle = .round; p.stroke()
            stroke([P(18.6, 4.2), P(18.6, 8.9), P(13.9, 8.9)], 1.8)
        case .wave, .waveOff:
            let bars: [(CGFloat, CGFloat, CGFloat)] = [(4.5, 10.5, 13.5), (8.5, 8, 16), (12, 5.5, 18.5), (15.5, 8, 16), (19.5, 10.5, 13.5)]
            if self == .waveOff { color.withAlphaComponent(color.alphaComponent * 0.45).set() }
            for b in bars { stroke([P(b.0, b.1), P(b.0, b.2)], 1.9) }
            if self == .waveOff { color.set(); stroke([P(4, 4), P(20, 20)], 1.9) }
        case .chevronDown:
            stroke([P(6.5, 9.5), P(12, 15), P(17.5, 9.5)], 2)
        case .chevronRight:
            stroke([P(6, 3.5), P(10.5, 8), P(6, 12.5)], 1.8)
        case .x:
            stroke([P(4, 4), P(12, 12)], 2); stroke([P(12, 4), P(4, 12)], 2)
        case .check:
            stroke([P(3.5, 8.5), P(6.5, 11.5), P(12.5, 4.5)], 2.2)
        case .question:
            let c = NSBezierPath(ovalIn: NSRect(x: 1.7, y: 1.7, width: 12.6, height: 12.6)); c.lineWidth = 1.7; c.stroke()
            let p = NSBezierPath()
            p.appendArc(withCenter: P(8.05, 6.35), radius: 1.75, startAngle: 180, endAngle: 65, clockwise: false)
            p.curve(to: P(8, 9.1), controlPoint1: P(8.3, 8.25), controlPoint2: P(8, 8.6))
            p.line(to: P(8, 9.5))
            p.lineWidth = 1.7; p.lineCapStyle = .round; p.stroke()
            dot(P(8, 11.6), 0.95)
        case .dashboard:
            // Tablero: un panel alto y dos pequeños.
            for r in [NSRect(x: 4.5, y: 4.5, width: 6.5, height: 15), NSRect(x: 14, y: 4.5, width: 5.5, height: 5.5),
                      NSRect(x: 14, y: 13, width: 5.5, height: 6.5)] {
                let p = NSBezierPath(roundedRect: r, xRadius: 1.6, yRadius: 1.6); p.lineWidth = 1.8; p.stroke()
            }
        case .bang:
            let c = NSBezierPath(ovalIn: NSRect(x: 1.7, y: 1.7, width: 12.6, height: 12.6)); c.lineWidth = 1.8; c.stroke()
            stroke([P(8, 4.8), P(8, 8.8)], 1.8)
            dot(P(8, 11.2), 0.95)
        }
        ctx.restoreGState()
    }
}

final class IconView: NSView {
    var icon: Icon { didSet { needsDisplay = true } }
    var color: NSColor { didSet { needsDisplay = true } }
    var rotated = false { didSet { needsDisplay = true } }
    init(_ icon: Icon, color: NSColor) { self.icon = icon; self.color = color; super.init(frame: .zero) }
    required init?(coder: NSCoder) { fatalError() }
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func draw(_ dirtyRect: NSRect) { icon.draw(in: bounds, color: color, rotated: rotated) }
}

// MARK: - Controles básicos sin robar el foco

/// Vista pulsable: acepta el primer clic, gestiona hover y pulsación. Base de botones, tarjetas y opciones.
class ClickView: NSView {
    var onClick: (() -> Void)?
    var onHover: ((Bool) -> Void)?
    var isEnabled = true { didSet { needsDisplay = true } }
    private(set) var hovering = false
    private(set) var pressed = false
    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self))
    }
    override func mouseEntered(with event: NSEvent) { hovering = true; needsDisplay = true; hoverChanged(); onHover?(true) }
    override func mouseExited(with event: NSEvent) { hovering = false; pressed = false; needsDisplay = true; hoverChanged(); onHover?(false) }
    override func mouseDown(with event: NSEvent) { pressed = true; needsDisplay = true; hoverChanged() }
    override func mouseUp(with event: NSEvent) {
        let inside = bounds.contains(convert(event.locationInWindow, from: nil))
        pressed = false; needsDisplay = true; hoverChanged()
        if inside && isEnabled { onClick?() }
    }
    func hoverChanged() {}
    override func accessibilityPerformPress() -> Bool { if isEnabled { onClick?() }; return true }
    override func isAccessibilityElement() -> Bool { true }
}

/// Botón de icono de la barra (34×32) o de cierre (24×24 circular).
final class IconButton: ClickView {
    let iconView: IconView
    var baseBackground: NSColor = .clear
    var dimmed = false { didSet { iconView.color = dimmed ? C.disabled : iconColor } }
    var iconColor: NSColor = C.text { didSet { if !dimmed { iconView.color = iconColor } } }
    var iconInset: CGFloat = 7.5
    let mini = NSTextField(labelWithString: "")
    init(_ icon: Icon, label: String) {
        iconView = IconView(icon, color: C.text)
        super.init(frame: .zero)
        addSubview(iconView)
        wantsLayer = true
        mini.font = F.sys(10, .bold); mini.textColor = .white; mini.alignment = .center
        mini.wantsLayer = true; mini.drawsBackground = false
        mini.layer?.backgroundColor = C.blue.cgColor; mini.layer?.cornerRadius = 7.5
        mini.isHidden = true
        addSubview(mini)
        setAccessibilityRole(.button); setAccessibilityLabel(label); toolTip = label
    }
    required init?(coder: NSCoder) { fatalError() }
    override func layout() {
        super.layout()
        let s = min(bounds.width, bounds.height) - iconInset * 2
        iconView.frame = NSRect(x: (bounds.width - s) / 2, y: (bounds.height - s) / 2, width: s, height: s)
        if !mini.isHidden {
            let w = max(15, mini.attributedStringValue.size().width + 8)
            mini.frame = NSRect(x: bounds.width - w, y: -1, width: w, height: 15)
        }
    }
    func setMini(_ text: String?) {
        mini.isHidden = text == nil
        mini.stringValue = text ?? ""
        needsLayout = true
    }
    override func draw(_ dirtyRect: NSRect) {
        var bg = baseBackground
        if pressed { bg = NSColor(white: 0, alpha: 0.10) } else if hovering { bg = NSColor(white: 0, alpha: 0.07) }
        if bg != .clear {
            bg.setFill()
            let r = min(bounds.height, bounds.width) / 2
            NSBezierPath(roundedRect: bounds, xRadius: r, yRadius: r).fill()
        }
    }
}

enum PillStyle { case primary, secondary, ok }

/// Botón píldora (Enviar, Reintentar, Cerrar…).
final class PillButton: ClickView {
    var title: String { didSet { invalidateIntrinsicContentSize(); needsDisplay = true; needsLayout = true } }
    var style: PillStyle { didSet { needsDisplay = true } }
    var small = false
    var busy = false { didSet { busy ? spinner.startAnimation(nil) : spinner.stopAnimation(nil); spinner.isHidden = !busy; needsLayout = true; needsDisplay = true } }
    var showCheck = false { didSet { needsDisplay = true } }
    let spinner = NSProgressIndicator()
    override var isEnabled: Bool { didSet { needsDisplay = true } }

    init(_ title: String, style: PillStyle, small: Bool = false) {
        self.title = title; self.style = style; self.small = small
        super.init(frame: .zero)
        spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
        spinner.isHidden = true
        addSubview(spinner)
        setAccessibilityRole(.button)
    }
    required init?(coder: NSCoder) { fatalError() }
    var font: NSFont { F.sys(small ? 12 : 13, .semibold) }
    var height: CGFloat { small ? 26 : 30 }
    var width: CGFloat {
        let t = (title as NSString).size(withAttributes: [.font: font]).width
        return ceil(t + (small ? 22 : 28) + ((busy || showCheck) ? 18 : 0))
    }
    override func layout() {
        super.layout()
        let t = (title as NSString).size(withAttributes: [.font: font]).width
        let total = t + ((busy || showCheck) ? 18 : 0)
        spinner.frame = NSRect(x: (bounds.width - total) / 2, y: (bounds.height - 12) / 2, width: 12, height: 12)
    }
    override func draw(_ dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: r, xRadius: r.height / 2, yRadius: r.height / 2)
        var fg = C.text
        switch style {
        case .primary:
            (isEnabled ? (pressed ? NSColor(hex: 0x0062CC) : (hovering ? NSColor(hex: 0x0A6FE6) : C.blue)) : C.blue.withAlphaComponent(0.35)).setFill()
            path.fill(); fg = .white
        case .ok:
            C.green.setFill(); path.fill(); fg = .white
        case .secondary:
            (pressed ? NSColor(white: 0.93, alpha: 1) : (hovering ? NSColor(hex: 0xf7f7f8) : .white)).setFill(); path.fill()
            NSColor(white: 0, alpha: 0.12).setStroke(); path.lineWidth = 0.5; path.stroke()
            fg = isEnabled ? C.text : C.text3
        }
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: fg]
        let ts = (title as NSString).size(withAttributes: attrs)
        let lead: CGFloat = (busy || showCheck) ? 18 : 0
        let x = (bounds.width - ts.width - lead) / 2 + lead
        (title as NSString).draw(at: NSPoint(x: x, y: (bounds.height - ts.height) / 2), withAttributes: attrs)
        if showCheck {
            Icon.check.draw(in: NSRect(x: x - 18, y: (bounds.height - 13) / 2, width: 13, height: 13), color: fg)
        }
    }
}

// MARK: - Etiquetas

/// Etiqueta que no captura el ratón (para texto dentro de tarjetas y opciones pulsables).
final class PassLabel: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

func makeLabel(_ text: String = "", font: NSFont, color: NSColor = C.text, wrap: Bool = false,
               selectable: Bool = false, pass: Bool = false) -> NSTextField {
    let l: NSTextField
    if pass { l = wrap ? PassLabel(wrappingLabelWithString: text) : PassLabel(labelWithString: text) }
    else { l = wrap ? NSTextField(wrappingLabelWithString: text) : NSTextField(labelWithString: text) }
    l.font = font; l.textColor = color
    l.isSelectable = selectable
    l.drawsBackground = false; l.isBordered = false
    if wrap {
        l.lineBreakMode = .byWordWrapping; l.maximumNumberOfLines = 0
        l.cell?.wraps = true; l.cell?.isScrollable = false
    } else {
        l.lineBreakMode = .byTruncatingTail; l.cell?.truncatesLastVisibleLine = true
        l.maximumNumberOfLines = 1
    }
    return l
}

/// Altura de una etiqueta con ajuste de línea para un ancho dado.
func labelHeight(_ l: NSTextField, width: CGFloat) -> CGFloat {
    guard let cell = l.cell else { return 16 }
    return ceil(cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: width, height: 100_000)).height)
}

func textWidth(_ s: String, _ font: NSFont) -> CGFloat {
    ceil((s as NSString).size(withAttributes: [.font: font]).width)
}

/// Ancho que necesita una etiqueta de una línea (incluye el relleno de la celda).
func fitWidth(_ l: NSTextField) -> CGFloat {
    ceil(l.cell?.cellSize(forBounds: NSRect(x: 0, y: 0, width: 10_000, height: 100)).width ?? textWidth(l.stringValue, l.font ?? F.sys(12))) + 1
}

let hhmm: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm"; return f }()
func clockText(_ epoch: Double) -> String { hhmm.string(from: Date(timeIntervalSince1970: epoch)) }
