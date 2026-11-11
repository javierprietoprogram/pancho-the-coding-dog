import AppKit
import QuartzCore

// MARK: - Pancho

protocol PetDelegate: AnyObject {
    func petDragBegan()
    func petDragMoved(origin: NSPoint, pointer: NSPoint)
    func petDragEnded()
    func petClicked()
    func petDoubleClicked()
    func petContextMenu(_ event: NSEvent, in view: NSView)
}

final class BadgeView: NSView {
    var count = 0 { didSet { needsDisplay = true } }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func draw(_ dirtyRect: NSRect) {
        guard count > 0 else { return }
        let inner = bounds.insetBy(dx: 4, dy: 4)  // margen para la sombra
        let ring = NSBezierPath(roundedRect: inner, xRadius: inner.height / 2, yRadius: inner.height / 2)
        NSGraphicsContext.saveGraphicsState()
        let sh = NSShadow(); sh.shadowColor = NSColor(white: 0, alpha: 0.18); sh.shadowBlurRadius = 5; sh.shadowOffset = NSSize(width: 0, height: -2)
        sh.set()
        NSColor.white.setFill(); ring.fill()
        NSGraphicsContext.restoreGraphicsState()
        let fillRect = inner.insetBy(dx: 2, dy: 2)
        C.blue.setFill()
        NSBezierPath(roundedRect: fillRect, xRadius: fillRect.height / 2, yRadius: fillRect.height / 2).fill()
        if count > 1 {
            let attrs: [NSAttributedString.Key: Any] = [.font: F.sys(11, .bold), .foregroundColor: NSColor.white]
            let s = "\(count)" as NSString
            let sz = s.size(withAttributes: attrs)
            s.draw(at: NSPoint(x: fillRect.midX - sz.width / 2, y: fillRect.midY - sz.height / 2), withAttributes: attrs)
        }
    }
    /// Marco en coordenadas de Pancho (96×104, y hacia arriba), según DISEÑO.md: punto 12 o contador 18.
    func place(in size: NSSize) {
        let k = M.petScale  // los márgenes siguen al tamaño de Pancho; el indicador no cambia de tamaño
        if count == 1 {
            // dot: top 14, right 16, 12×12, anillo 2 + margen 4
            frame = NSRect(x: size.width - 16 * k - 12 - 6, y: size.height - 14 * k - 12 - 6, width: 24, height: 24)
        } else {
            let w = max(18, textWidth("\(count)", F.sys(11, .bold)) + 10)
            frame = NSRect(x: size.width - 12 * k - w - 6, y: size.height - 10 * k - 18 - 6, width: w + 12, height: 30)
        }
    }
}

final class PetView: NSView {
    weak var delegate: PetDelegate?
    let sprite = NSView()
    let badge = BadgeView()
    private var mask: AlphaMask?
    private var downPointer: NSPoint?
    private var downOrigin = NSPoint.zero
    private(set) var dragging = false
    private var pendingClick: DispatchWorkItem?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        sprite.wantsLayer = true
        sprite.frame = bounds
        sprite.autoresizingMask = [.width, .height]
        sprite.layer?.contentsGravity = .resize
        sprite.layer?.shadowColor = NSColor.black.cgColor
        sprite.layer?.shadowOpacity = 0.10
        sprite.layer?.shadowRadius = 2.5
        sprite.layer?.shadowOffset = CGSize(width: 0, height: -3)
        addSubview(sprite)
        badge.wantsLayer = true
        badge.isHidden = true
        addSubview(badge)
        setAccessibilityElement(true)
        setAccessibilityRole(.button)
    }
    required init?(coder: NSCoder) { fatalError() }

    func show(_ image: CGImage, mask: AlphaMask) {
        CATransaction.begin(); CATransaction.setDisableActions(true)
        sprite.layer?.contents = image
        CATransaction.commit()
        self.mask = mask
    }

    func setBadge(_ n: Int, pop: Bool) {
        badge.isHidden = n == 0
        badge.count = n
        if n > 0 { badge.place(in: bounds.size) }
        if pop, n > 0, let layer = badge.layer {
            let c = CGPoint(x: badge.bounds.midX, y: badge.bounds.midY)
            var t = CATransform3DMakeTranslation(c.x, c.y, 0)
            t = CATransform3DScale(t, 0.4, 0.4, 1)
            t = CATransform3DTranslate(t, -c.x, -c.y, 0)
            let a = CABasicAnimation(keyPath: "transform")
            a.fromValue = NSValue(caTransform3D: t)
            a.toValue = NSValue(caTransform3D: CATransform3DIdentity)
            a.duration = 0.45
            a.timingFunction = CAMediaTimingFunction(controlPoints: 0.3, 1.6, 0.5, 1)
            layer.add(a, forKey: "pop")
        }
    }

    /// ¿Hay píxel visible de Pancho (o el indicador) en este punto de la vista?
    func isSolid(at p: NSPoint) -> Bool {
        guard bounds.contains(p) else { return false }
        if !badge.isHidden && badge.frame.insetBy(dx: 3, dy: 3).contains(p) { return true }
        guard let m = mask else { return true }
        let col = Int(p.x / bounds.width * CGFloat(m.width))
        let row = Int((bounds.height - p.y) / bounds.height * CGFloat(m.height))
        return m.alpha(col: col, row: row) > 24
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        let p = convert(point, from: superview)
        return isSolid(at: p) || dragging ? self : nil
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) {
        downPointer = NSEvent.mouseLocation
        downOrigin = window?.frame.origin ?? .zero
        dragging = false
    }
    override func mouseDragged(with event: NSEvent) {
        guard let start = downPointer else { return }
        let p = NSEvent.mouseLocation
        if !dragging {
            if hypot(p.x - start.x, p.y - start.y) < M.dragThreshold { return }
            dragging = true
            delegate?.petDragBegan()
        }
        delegate?.petDragMoved(origin: NSPoint(x: downOrigin.x + p.x - start.x, y: downOrigin.y + p.y - start.y), pointer: p)
    }
    override func mouseUp(with event: NSEvent) {
        guard downPointer != nil else { return }
        downPointer = nil
        if dragging { dragging = false; delegate?.petDragEnded(); return }
        // Doble clic: recoge o muestra las conversaciones. El clic simple espera el intervalo de doble clic
        // para no abrir el panel justo antes de recogerlas.
        pendingClick?.cancel(); pendingClick = nil
        if event.clickCount >= 2 { delegate?.petDoubleClicked(); return }
        let work = DispatchWorkItem { [weak self] in self?.pendingClick = nil; self?.delegate?.petClicked() }
        pendingClick = work
        DispatchQueue.main.asyncAfter(deadline: .now() + NSEvent.doubleClickInterval, execute: work)
    }
    override func rightMouseDown(with event: NSEvent) { delegate?.petContextMenu(event, in: self) }
    override func accessibilityPerformPress() -> Bool { delegate?.petClicked(); return true }
}

// MARK: - Barra de controles

final class BarView: NSView {
    let glass = GlassView(radius: 20, tint: C.surface)
    let refresh = IconButton(.refresh, label: "Actualizar conversaciones en ejecución")
    let mute = IconButton(.wave, label: "Silenciar animaciones de Pancho")
    let toggle = IconButton(.chevronDown, label: "Contraer conversaciones")
    let sep1 = NSView(), sep2 = NSView()
    let simLabel = makeLabel("SIMULACIÓN", font: F.sys(8.5, .bold), color: C.orangeText, pass: true)
    var simulation = false { didSet { simLabel.isHidden = !simulation; needsLayout = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        glass.autoresizingMask = [.width, .height]
        glass.frame = bounds
        addSubview(glass)
        for s in [sep1, sep2] { s.wantsLayer = true; s.layer?.backgroundColor = NSColor(white: 0, alpha: 0.12).cgColor }
        [refresh, sep1, mute, sep2, toggle, simLabel].forEach(glass.content.addSubview)
        refresh.iconInset = 7.5; mute.iconInset = 7.5; toggle.iconInset = 7.5
        simLabel.alignment = .center
        simLabel.isHidden = true
        simLabel.setAccessibilityLabel("Modo simulación")
        setAccessibilityElement(true)
        setAccessibilityRole(.toolbar)
        setAccessibilityLabel("Controles de Pancho")
    }
    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        refresh.frame = NSRect(x: 4, y: 4, width: 34, height: 32)
        sep1.frame = NSRect(x: 40, y: 10, width: 1, height: 20)
        mute.frame = NSRect(x: 43, y: 4, width: 34, height: 32)
        sep2.frame = NSRect(x: 79, y: 10, width: 1, height: 20)
        toggle.frame = NSRect(x: 82, y: 4, width: 34, height: 32)
        simLabel.frame = NSRect(x: 0, y: 37, width: bounds.width, height: 12)
    }
}

// MARK: - Tarjetas

enum Indicator: Equatable { case breathing, dots, spinner, check, bang, none }
enum Tone: Equatable { case blue, gray, red, green }

struct CardModel: Equatable {
    let id: String
    let title: String
    let status: String
    let indicator: Indicator
    let tone: Tone
    let provider: String
    let draft: Bool
    let chevron: Bool
    let dismissible: Bool
    let attention: Bool
    let clickable: Bool
    let tooltip: String
}

private func toneColor(_ t: Tone) -> NSColor {
    switch t { case .blue: return C.blueText; case .gray: return C.text2; case .red: return C.redText; case .green: return C.greenText }
}

final class CardView: ClickView {
    let glass = GlassView(radius: 28, tint: C.surface)
    let title = makeLabel("", font: F.sys(13, .semibold), pass: true)
    let statusLabel = makeLabel("", font: F.sys(12), pass: true)
    let sepDot = makeLabel("·", font: F.sys(12), color: C.text3, pass: true)
    let providerMark = NSView()
    /// Proveedor junto al proyecto (línea 1): ▪ + «Claude»/«Codex», discreto (DISEÑO.md: marca neutra + nombre).
    let providerLabel = makeLabel("", font: F.sys(12, .medium), color: C.text2, pass: true)
    let indicatorHost = PassView()
    let chevron = IconView(.chevronRight, color: C.text3)
    let draftTag = makeLabel("Borrador", font: F.sys(10.5, .semibold), color: C.text2, pass: true)
    let close = IconButton(.x, label: "Quitar de la lista")
    var model: CardModel?
    var isOpen = false
    var reduceMotion = false
    var onDismiss: (() -> Void)?
    private var indicatorKind: Indicator?

    override init(frame: NSRect) {
        super.init(frame: frame)
        glass.frame = bounds
        glass.autoresizingMask = [.width, .height]
        addSubview(glass)
        providerMark.wantsLayer = true; providerMark.layer?.cornerRadius = 2
        draftTag.wantsLayer = true; draftTag.drawsBackground = false
        draftTag.layer?.backgroundColor = NSColor(white: 0, alpha: 0.06).cgColor; draftTag.layer?.cornerRadius = 6
        draftTag.alignment = .center
        close.iconInset = 5.5
        close.onClick = { [weak self] in self?.onDismiss?() }
        indicatorHost.wantsLayer = true
        [title, indicatorHost, statusLabel, sepDot, providerMark, providerLabel, chevron, draftTag, close].forEach(glass.content.addSubview)
        setAccessibilityRole(.button)
    }
    required init?(coder: NSCoder) { fatalError() }

    func update(_ m: CardModel, open: Bool, reduceMotion: Bool) {
        let changed = m != model || open != isOpen || reduceMotion != self.reduceMotion
        model = m; isOpen = open; self.reduceMotion = reduceMotion
        guard changed else { return }
        title.stringValue = m.title
        statusLabel.stringValue = m.status
        statusLabel.textColor = toneColor(m.tone)
        statusLabel.font = F.sys(12, (m.tone == .blue || m.tone == .red) ? .semibold : .regular)
        providerLabel.stringValue = providerShortName(m.provider)
        providerMark.layer?.backgroundColor = providerColor(m.provider).cgColor
        chevron.isHidden = !m.chevron
        draftTag.isHidden = !m.draft
        toolTip = m.tooltip
        setAccessibilityLabel("\(m.title). \(m.status). \(providerName(m.provider))")
        if indicatorKind != m.indicator { rebuildIndicator(m.indicator) }
        applyLook()
        needsLayout = true
    }

    private func rebuildIndicator(_ kind: Indicator) {
        indicatorKind = kind
        indicatorHost.subviews.forEach { $0.removeFromSuperview() }
        indicatorHost.layer?.sublayers?.forEach { $0.removeFromSuperlayer() }
        let color = toneColor(model?.tone ?? .gray)
        switch kind {
        case .breathing:
            let dot = CALayer(); dot.frame = CGRect(x: 0, y: 4.5, width: 7, height: 7); dot.cornerRadius = 3.5
            dot.backgroundColor = C.blue.cgColor
            if !reduceMotion {
                let a = CABasicAnimation(keyPath: "opacity"); a.fromValue = 1; a.toValue = 0.35
                a.duration = 1.2; a.autoreverses = true; a.repeatCount = .infinity
                a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                dot.add(a, forKey: "breathe")
            }
            indicatorHost.layer?.addSublayer(dot)
        case .dots:
            for i in 0..<3 {
                let d = CALayer(); d.frame = CGRect(x: CGFloat(i) * 5, y: 6.5, width: 3, height: 3); d.cornerRadius = 1.5
                d.backgroundColor = C.text2.cgColor
                if !reduceMotion {
                    let a = CAKeyframeAnimation(keyPath: "opacity")
                    a.values = [0.25, 1, 0.25, 0.25]; a.keyTimes = [0, 0.4, 0.8, 1]
                    a.duration = 1.2; a.repeatCount = .infinity
                    a.beginTime = CACurrentMediaTime() + Double(i) * 0.2
                    d.add(a, forKey: "wd")
                } else { d.opacity = 0.6 }
                indicatorHost.layer?.addSublayer(d)
            }
        case .spinner:
            let s = NSProgressIndicator(); s.style = .spinning; s.controlSize = .mini
            s.frame = NSRect(x: 0, y: 2, width: 12, height: 12)
            indicatorHost.addSubview(s); s.startAnimation(nil)
        case .check:
            let v = IconView(.check, color: color); v.frame = NSRect(x: 0, y: 2.5, width: 11, height: 11); indicatorHost.addSubview(v)
        case .bang:
            let v = IconView(.bang, color: color); v.frame = NSRect(x: 0, y: 2.5, width: 11, height: 11); indicatorHost.addSubview(v)
        case .none:
            break
        }
    }

    private var indicatorWidth: CGFloat {
        switch indicatorKind ?? .none { case .breathing: return 7; case .dots: return 13; case .spinner: return 12; case .check, .bang: return 11; case .none: return 0 }
    }

    private func applyLook() {
        guard let m = model else { return }
        glass.tintAlpha = (hovering || isOpen) ? C.surfaceHover : C.surface
        if isOpen { glass.rimColor = C.blue.withAlphaComponent(0.65); glass.rimWidth = 1.5 }
        else if m.attention { glass.rimColor = C.blue.withAlphaComponent(0.30); glass.rimWidth = 1 }
        else { glass.rimColor = nil }
        close.isHidden = !(m.dismissible && hovering)
    }

    override func hoverChanged() { applyLook(); needsLayout = true }

    override func layout() {
        super.layout()
        let w = bounds.width
        var right = w - 12
        if !close.isHidden { right -= 22; close.frame = NSRect(x: right, y: 17, width: 22, height: 22); right -= 6 }
        if !chevron.isHidden { right -= 14; chevron.frame = NSRect(x: right, y: 21, width: 14, height: 14); right -= 6 }
        if !draftTag.isHidden {
            let tw = fitWidth(draftTag) + 10
            right -= tw; draftTag.frame = NSRect(x: right, y: 19, width: tw, height: 17); right -= 6
        }
        // Línea 1: ▪ proveedor · proyecto · sesión. El proveedor nunca se abrevia; el título sí.
        var x: CGFloat = 18
        providerMark.frame = NSRect(x: x, y: 15, width: 7, height: 7); x += 11
        let pw = fitWidth(providerLabel)
        providerLabel.frame = NSRect(x: x, y: 10, width: pw, height: 17); x += pw + 1
        sepDot.frame = NSRect(x: x, y: 10, width: 8, height: 17); x += 10
        title.frame = NSRect(x: x, y: 10, width: max(40, right - x - 2), height: 17)
        // Línea 2: indicador + estado.
        x = 18
        let iw = indicatorWidth
        indicatorHost.frame = NSRect(x: x, y: 28, width: max(iw, 1), height: 16)
        if iw > 0 { x += iw + 5 }
        statusLabel.frame = NSRect(x: x, y: 28, width: max(30, right - x - 2), height: 16)
    }
}

final class ListView: NSView {
    let scroll = NSScrollView()
    let doc = FlippedView()
    private var cards: [String: CardView] = [:]
    private var orderIds: [String] = []
    var onClick: ((String) -> Void)?
    var onDismiss: ((String) -> Void)?
    var onHover: ((Bool) -> Void)?
    private let fade = CAGradientLayer()
    private var fadeState: (top: Bool, bottom: Bool, size: NSSize)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = false
        scroll.hasHorizontalScroller = false
        scroll.borderType = .noBorder
        scroll.verticalScrollElasticity = .none
        scroll.contentView.drawsBackground = false
        scroll.documentView = doc
        scroll.autoresizingMask = [.width, .height]
        scroll.frame = bounds
        addSubview(scroll)
        scroll.wantsLayer = true
        scroll.contentView.postsBoundsChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(scrolled), name: NSView.boundsDidChangeNotification, object: scroll.contentView)
        setAccessibilityElement(true)
        setAccessibilityRole(.list)
        setAccessibilityLabel("Conversaciones")
    }
    required init?(coder: NSCoder) { fatalError() }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self))
    }
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }

    var contentHeight: CGFloat {
        let n = CGFloat(orderIds.count)
        return n == 0 ? 0 : n * M.cardH + (n - 1) * M.cardGap
    }

    func update(models: [String: CardModel], order: [String], openId: String?, width: CGFloat, reduceMotion: Bool) {
        for (id, v) in cards where !order.contains(id) { v.removeFromSuperview(); cards[id] = nil }
        orderIds = order.filter { models[$0] != nil }
        for (i, id) in orderIds.enumerated() {
            guard let m = models[id] else { continue }
            let card: CardView
            if let c = cards[id] { card = c } else {
                card = CardView(frame: NSRect(x: 0, y: 0, width: width, height: M.cardH))
                card.onClick = { [weak self] in self?.onClick?(id) }
                card.onDismiss = { [weak self] in self?.onDismiss?(id) }
                cards[id] = card
                doc.addSubview(card)
            }
            let f = NSRect(x: 0, y: CGFloat(i) * (M.cardH + M.cardGap), width: width, height: M.cardH)
            if card.frame != f { card.frame = f }
            card.update(m, open: openId == id, reduceMotion: reduceMotion)
        }
        let df = NSRect(x: 0, y: 0, width: width, height: max(contentHeight, 1))
        if doc.frame != df { doc.frame = df }
        updateFade()
    }

    @objc private func scrolled() { updateFade() }

    func updateFade() {
        let visible = scroll.contentView.bounds
        let top = visible.minY > 2
        let bottom = contentHeight - visible.maxY > 2
        // Sin cambios (bordes ni tamaño): no se toca la máscara.
        if let s = fadeState, s.top == top, s.bottom == bottom, s.size == scroll.bounds.size { return }
        fadeState = (top, bottom, scroll.bounds.size)
        guard top || bottom, bounds.height > 0 else { scroll.layer?.mask = nil; return }
        fade.frame = scroll.bounds
        let f = min(0.45, 28 / max(bounds.height, 1))
        let clear = NSColor.clear.cgColor, solid = NSColor.black.cgColor
        // La capa del scroll no está invertida: location 0 = abajo.
        fade.colors = [bottom ? clear : solid, solid, solid, top ? clear : solid]
        fade.locations = [0, NSNumber(value: Double(f)), NSNumber(value: Double(1 - f)), 1]
        scroll.layer?.mask = fade
    }
}
