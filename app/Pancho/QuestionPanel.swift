import AppKit

// MARK: - Piezas del panel

final class SelectIndicator: NSView {
    var multi = false { didSet { needsDisplay = true } }
    var checked = false { didSet { needsDisplay = true } }
    override var isFlipped: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func draw(_ dirtyRect: NSRect) {
        let b = bounds.insetBy(dx: 0.75, dy: 0.75)
        let path = multi ? NSBezierPath(roundedRect: b, xRadius: bounds.width * 0.28, yRadius: bounds.width * 0.28) : NSBezierPath(ovalIn: b)
        if checked {
            C.blue.setFill(); path.fill()
            if multi { Icon.check.draw(in: bounds.insetBy(dx: bounds.width * 0.1, dy: bounds.width * 0.1), color: .white) }
            else {
                let d = bounds.width - 10
                NSColor.white.setFill()
                NSBezierPath(ovalIn: NSRect(x: (bounds.width - d) / 2, y: (bounds.height - d) / 2, width: d, height: d)).fill()
            }
        } else {
            NSColor.white.setFill(); path.fill()
            NSColor(hex: 0xc2c2c7).setStroke(); path.lineWidth = bounds.width > 13 ? 1.5 : 1.3; path.stroke()
        }
    }
}

final class OptionRow: ClickView {
    let index: Int
    let ind = SelectIndicator()
    let title: NSTextField
    let desc: NSTextField?
    let kbd = makeLabel("", font: F.sys(10.5, .semibold), color: C.text3, pass: true)
    var checked = false { didSet { guard checked != oldValue else { return }; ind.checked = checked; needsDisplay = true; setAccessibilityValue(checked) } }
    var inactive = false { didSet { guard inactive != oldValue else { return }; alphaValue = inactive ? 0.55 : 1 } }
    var showKbd = false { didSet { kbd.isHidden = !showKbd || index >= 9 } }

    init(option: QOption, index: Int, multi: Bool) {
        self.index = index
        title = makeLabel(option.label, font: F.sys(13, .semibold), wrap: true, pass: true)
        desc = option.description.isEmpty ? nil : makeLabel(option.description, font: F.sys(12), color: C.text2, wrap: true, pass: true)
        super.init(frame: .zero)
        ind.multi = multi
        kbd.stringValue = "\(index + 1)"; kbd.alignment = .center
        kbd.wantsLayer = true; kbd.layer?.borderWidth = 0.5; kbd.layer?.borderColor = NSColor(white: 0, alpha: 0.16).cgColor
        kbd.layer?.cornerRadius = 5
        kbd.isHidden = true
        addSubview(ind); addSubview(title); if let d = desc { addSubview(d) }; addSubview(kbd)
        setAccessibilityRole(multi ? .checkBox : .radioButton)
        setAccessibilityLabel(option.label + (option.description.isEmpty ? "" : ". " + option.description))
    }
    required init?(coder: NSCoder) { fatalError() }

    func layoutRow(width: CGFloat) -> CGFloat {
        let kbdW: CGFloat = index < 9 ? 18 : 0
        let tx: CGFloat = 11 + 16 + 10
        let tw = width - tx - 10 - (kbdW > 0 ? kbdW + 8 : 0)
        let th = labelHeight(title, width: tw)
        title.frame = NSRect(x: tx, y: 9, width: tw, height: th)
        var y = 9 + th
        if let d = desc {
            y += 2
            let dh = labelHeight(d, width: tw)
            d.frame = NSRect(x: tx, y: y, width: tw, height: dh)
            y += dh
        }
        y += 9
        ind.frame = NSRect(x: 11, y: 10, width: 16, height: 16)
        kbd.frame = NSRect(x: width - 10 - kbdW, y: 9, width: kbdW, height: 16)
        frame.size = NSSize(width: width, height: y)
        return y
    }

    override func draw(_ dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 0.5, dy: 0.5)
        let p = NSBezierPath(roundedRect: r, xRadius: 12, yRadius: 12)
        if checked {
            C.blue.withAlphaComponent(0.07).setFill(); p.fill()
            C.blue.withAlphaComponent(0.6).setStroke(); p.lineWidth = 1; p.stroke()
        } else {
            NSColor.white.withAlphaComponent(hovering && isEnabled ? 1 : 0.72).setFill(); p.fill()
            NSColor(white: 0, alpha: 0.11).setStroke(); p.lineWidth = 0.5; p.stroke()
        }
    }
}

final class PanchoTextView: NSTextView {
    var onFocus: ((Bool) -> Void)?
    override func becomeFirstResponder() -> Bool { let r = super.becomeFirstResponder(); if r { onFocus?(true) }; return r }
    override func resignFirstResponder() -> Bool { let r = super.resignFirstResponder(); if r { onFocus?(false) }; return r }
}

/// Campo de texto libre multilínea (crece hasta 150 y luego se desplaza).
final class FreeTextBox: NSView, NSTextViewDelegate {
    let scroll = NSScrollView()
    let text = PanchoTextView()
    var onChange: ((String) -> Void)?
    var onFocus: (() -> Void)?
    var filled = false { didSet { if filled != oldValue { updateBorder() } } }
    private var focused = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.cornerRadius = 10
        layer?.backgroundColor = NSColor.white.withAlphaComponent(0.85).cgColor
        text.isRichText = false
        text.importsGraphics = false
        text.allowsUndo = true
        text.font = F.sys(13)
        text.textColor = C.text
        text.isAutomaticQuoteSubstitutionEnabled = false
        text.isAutomaticDashSubstitutionEnabled = false
        text.isAutomaticTextReplacementEnabled = false
        text.isAutomaticSpellingCorrectionEnabled = false
        text.isContinuousSpellCheckingEnabled = false
        text.drawsBackground = false
        text.textContainerInset = NSSize(width: 5, height: 8)
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = false
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        text.delegate = self
        text.setValue(NSAttributedString(string: "Escribe aquí…", attributes: [.font: F.sys(13), .foregroundColor: C.text3]),
                      forKey: "placeholderAttributedString")
        text.onFocus = { [weak self] f in self?.focused = f; self?.updateBorder(); if f { self?.onFocus?() } }
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.documentView = text
        addSubview(scroll)
        updateBorder()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func updateBorder() {
        layer?.borderWidth = focused ? 1 : 0.5
        layer?.borderColor = (focused || filled ? C.blue.withAlphaComponent(0.6) : NSColor(white: 0, alpha: 0.15)).cgColor
        layer?.shadowOpacity = 0
    }

    func setEditable(_ v: Bool) {
        if text.isEditable != v { text.isEditable = v }
        text.isSelectable = true
        let a: CGFloat = v ? 1 : 0.7
        if alphaValue != a { alphaValue = a }
    }

    func layoutBox(width: CGFloat) -> CGFloat {
        scroll.frame = NSRect(x: 0, y: 0, width: width, height: max(frame.height, 36))
        text.frame = NSRect(x: 0, y: 0, width: scroll.contentSize.width, height: text.frame.height)
        var used: CGFloat = 17
        if let lm = text.layoutManager, let tc = text.textContainer {
            lm.ensureLayout(for: tc)
            used = lm.usedRect(for: tc).height
        }
        let h = min(150, max(36, ceil(used + text.textContainerInset.height * 2)))
        frame.size = NSSize(width: width, height: h)
        scroll.frame = bounds
        return h
    }

    func textDidChange(_ notification: Notification) { onChange?(text.string) }
}

final class QuestionSection: FlippedView {
    let q: QItem
    let index: Int, count: Int
    let num = makeLabel("", font: F.sys(11.5), color: C.text3)
    let header = makeLabel("", font: F.sys(11.5, .semibold), color: C.text, wrap: true)
    let check = makeLabel("✓", font: F.sys(11.5, .semibold), color: C.green)
    let modeInd = SelectIndicator()
    let modeLabel = makeLabel("", font: F.sys(11.5), color: C.text2)
    let qText: NSTextField
    var rows: [OptionRow] = []
    let otherLabel = makeLabel("", font: F.sys(10.5, .semibold), color: C.text3)
    let box = FreeTextBox()
    let hint = makeLabel("Si escribes aquí, se envía tu texto en lugar de las casillas.", font: F.sys(11), color: C.text3, wrap: true)
    let sentBox = FlippedView()
    let sentTitle = makeLabel("ENVIADO", font: F.sys(10.5, .semibold), color: C.text3)
    let sentText = makeLabel("", font: F.sys(12.5), color: C.text, wrap: true, selectable: true)
    let topRule = NSView()

    init(q: QItem, index: Int, count: Int) {
        self.q = q; self.index = index; self.count = count
        qText = makeLabel(q.question, font: F.sys(14, .medium), wrap: true, selectable: true)
        super.init(frame: .zero)
        num.stringValue = count > 1 ? "\(index + 1) de \(count)  ·" : ""
        header.stringValue = q.header ?? (count > 1 ? "" : "")
        modeInd.multi = q.multiSelect
        modeLabel.stringValue = q.options.isEmpty ? "Respuesta libre" : (q.multiSelect ? "Elige una o varias" : "Elige una")
        modeInd.isHidden = q.options.isEmpty
        otherLabel.attributedStringValue = NSAttributedString(string: (q.options.isEmpty ? "Tu respuesta" : "O escribe tu respuesta").uppercased(),
            attributes: [.font: F.sys(10.5, .semibold), .foregroundColor: C.text3, .kern: 0.5])
        sentTitle.attributedStringValue = NSAttributedString(string: "ENVIADO", attributes: [.font: F.sys(10.5, .semibold), .foregroundColor: C.text3, .kern: 0.5])
        for (i, o) in q.options.enumerated() {
            let r = OptionRow(option: o, index: i, multi: q.multiSelect)
            rows.append(r); addSubview(r)
        }
        sentBox.wantsLayer = true; sentBox.layer?.cornerRadius = 10
        sentBox.layer?.backgroundColor = NSColor(white: 0, alpha: 0.04).cgColor
        sentBox.addSubview(sentTitle); sentBox.addSubview(sentText)
        sentBox.isHidden = true
        topRule.wantsLayer = true; topRule.layer?.backgroundColor = C.hairline.cgColor
        topRule.isHidden = index == 0
        check.isHidden = true
        hint.isHidden = !q.multiSelect
        [topRule, num, header, check, modeInd, modeLabel, qText, otherLabel, box, hint, sentBox].forEach(addSubview)
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel((q.header.map { $0 + ". " } ?? "") + q.question)
    }
    required init?(coder: NSCoder) { fatalError() }

    func layoutSection(width: CGFloat) -> CGFloat {
        var y: CGFloat = 0
        if index > 0 { topRule.frame = NSRect(x: 0, y: 0, width: width, height: 0.5); y = 14.5 }
        // Cabecera de la pregunta
        let modeW = fitWidth(modeLabel)
        modeLabel.frame = NSRect(x: width - modeW, y: y, width: modeW, height: 16)
        modeInd.frame = modeInd.isHidden ? .zero : NSRect(x: width - modeW - 17, y: y + 2, width: 12, height: 12)
        var x: CGFloat = 0
        if !num.stringValue.isEmpty {
            let w = fitWidth(num)
            num.frame = NSRect(x: 0, y: y, width: w, height: 16); x = w + 4
        } else { num.frame = .zero }
        // Encabezado corto: se ajusta en varias líneas en vez de truncarse.
        let room = max(20, width - modeW - 17 - 8 - x - 16)
        let hw = header.stringValue.isEmpty ? 0 : min(fitWidth(header), room)
        let hh = header.stringValue.isEmpty ? 16 : max(16, labelHeight(header, width: hw))
        header.frame = NSRect(x: x, y: y, width: hw, height: hh)
        check.frame = NSRect(x: x + hw + 2, y: y, width: 14, height: 16)
        y += hh + 4
        let th = labelHeight(qText, width: width)
        qText.frame = NSRect(x: 0, y: y, width: width, height: th)
        y += th + 10
        for r in rows {
            let h = r.layoutRow(width: width)
            r.frame.origin = NSPoint(x: 0, y: y)
            y += h + 6
        }
        y += 2
        otherLabel.frame = NSRect(x: 0, y: y, width: width, height: 13); y += 16
        let bh = box.layoutBox(width: width)
        box.frame.origin = NSPoint(x: 0, y: y); y += bh
        if !hint.isHidden {
            y += 4
            let hh = labelHeight(hint, width: width)
            hint.frame = NSRect(x: 0, y: y, width: width, height: hh); y += hh
        }
        if !sentBox.isHidden {
            y += 8
            sentTitle.frame = NSRect(x: 11, y: 8, width: width - 22, height: 13)
            let sh = labelHeight(sentText, width: width - 22)
            sentText.frame = NSRect(x: 11, y: 24, width: width - 22, height: sh)
            sentBox.frame = NSRect(x: 0, y: y, width: width, height: 24 + sh + 9)
            y += sentBox.frame.height
        }
        frame.size = NSSize(width: width, height: y)
        return y
    }
}

// MARK: - Panel de pregunta

enum PanelPhase: Equatable {
    case editing(retry: Bool)
    case confirmRelease
    case releasing
    case sending
    case sent
    case received
    case problem
    /// Claude dejó de esperar (waiter muerto o plazo vencido) sin que se entregara nada: solo lectura.
    case stale
    /// Informativo y descartable: released, expired, resolved_elsewhere o unconfirmed. Sin autocierre.
    case terminal(String)
    /// Codex: `error` con `can_retry` y response.json conservada. «Reintentar» vuelve a invocar
    /// `codex-send` con la misma respuesta (solo lectura).
    case retrySend
}

protocol QuestionPanelDelegate: AnyObject {
    func panelHide()
    func panelDismiss()
    func panelSend(_ answers: [String: String]) -> String?
    func panelRelease() -> String?
    func panelRetry() -> String?
    func panelDraftChanged(_ draft: Draft)
    func panelNeedsLayout()
}

final class QuestionPanel: NSObject {
    weak var delegate: QuestionPanelDelegate?
    let window = FloatingPanel(size: NSSize(width: M.panelW, height: 300), shadow: true)
    let root = GlassView(radius: 22, tint: C.surfacePanel)

    // cabecera
    let kindPill = FlippedView()
    let kindIcon = IconView(.question, color: C.blueDeep)
    let kindLabel = makeLabel("", font: F.sys(11.5, .semibold), color: C.blueDeep)
    let provMark = NSView()
    let metaLabel = makeLabel("", font: F.sys(12), color: C.text2)
    let closeButton = IconButton(.x, label: "Ocultar sin responder (Esc)")
    let titleLabel = makeLabel("", font: F.sys(15, .semibold), wrap: true, selectable: true)
    let subLabel = makeLabel("", font: F.sys(11.5), color: C.text2, wrap: true)
    let headerRule = NSView()
    // cuerpo
    let scroll = NSScrollView()
    let doc = FlippedView()
    var sections: [QuestionSection] = []
    // aviso
    let banner = FlippedView()
    let bannerLabel = makeLabel("", font: F.sys(12), color: C.text, wrap: true)
    private(set) var bannerButtons: [PillButton] = []
    let copyButton = PillButton("Copiar", style: .secondary, small: true)
    let dismissButton = PillButton("Quitar", style: .secondary, small: true)
    let cancelReleaseButton = PillButton("Cancelar", style: .secondary, small: true)
    let confirmReleaseButton = PillButton("Responder en Claude", style: .primary, small: true)
    // pie
    let footerRule = NSView()
    let footerMsg = makeLabel("", font: F.sys(11.5), color: C.text3, wrap: true)
    let releaseButton = PillButton("Responder en Claude", style: .secondary, small: true)
    let sendButton = PillButton("Enviar", style: .primary)

    private(set) var rec: RequestRecord?
    var draft = Draft()
    var activeQ = 0
    private(set) var phase: PanelPhase = .editing(retry: false)
    var localSubmitted = false
    var confirmingRelease = false
    var localError: String?
    var localNote: String?
    private var keyMonitor: Any?
    /// App que estaba activa antes de que el usuario abriera el panel; se le devuelve el foco al ocultarlo.
    private var previousApp: NSRunningApplication?
    /// Última firma pintada: si el sondeo trae lo mismo no se toca nada.
    private var shownSignature: RecordSignature?
    var isOpen: Bool { rec != nil }

    override init() {
        super.init()
        window.allowKey = true
        root.frame = NSRect(x: 0, y: 0, width: M.panelW, height: 300)
        root.autoresizingMask = [.width, .height]
        window.contentView = root
        let c = root.content
        kindPill.wantsLayer = true; kindPill.layer?.cornerRadius = 10
        kindPill.layer?.backgroundColor = C.blue.withAlphaComponent(0.12).cgColor
        kindPill.addSubview(kindIcon); kindPill.addSubview(kindLabel)
        provMark.wantsLayer = true; provMark.layer?.cornerRadius = 2
        closeButton.baseBackground = NSColor(white: 0, alpha: 0.06)
        closeButton.iconInset = 6.5
        closeButton.onClick = { [weak self] in self?.delegate?.panelHide() }
        for r in [headerRule, footerRule] { r.wantsLayer = true; r.layer?.backgroundColor = C.hairline.cgColor }
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.contentView.drawsBackground = false
        scroll.documentView = doc
        banner.wantsLayer = true; banner.layer?.cornerRadius = 12
        banner.addSubview(bannerLabel)
        sendButton.onClick = { [weak self] in self?.send() }
        releaseButton.onClick = { [weak self] in self?.confirmingRelease = true; self?.refresh() }
        copyButton.onClick = { [weak self] in self?.copyAnswers() }
        dismissButton.onClick = { [weak self] in self?.delegate?.panelDismiss() }
        cancelReleaseButton.onClick = { [weak self] in self?.confirmingRelease = false; self?.refresh() }
        confirmReleaseButton.onClick = { [weak self] in self?.confirmRelease() }
        [kindPill, provMark, metaLabel, closeButton, titleLabel, subLabel, headerRule, scroll, banner, footerRule, footerMsg, releaseButton, sendButton].forEach(c.addSubview)
        root.setAccessibilityElement(true)
        root.setAccessibilityRole(.group)
        root.setAccessibilityRoleDescription("diálogo")
    }

    // MARK: Abrir / cerrar

    /// `focus`: el panel pasa a ser la ventana clave (teclado). Solo cuando lo pide el usuario
    /// (tarjeta, lápiz, Pancho) o, al encadenar, si el panel ya era la ventana clave.
    func open(_ record: RequestRecord, draft: Draft, focus: Bool) {
        load(record, draft: draft)
        window.orderFrontRegardless()
        if focus {
            // Pancho pasa a ser la app activa mientras se escribe: así el dictado (Wispr Flow, dictado
            // de macOS) y los atajos de edición llegan a este panel y no a la app de origen.
            activateForInput()
            window.makeKey()
            window.makeFirstResponder(root.content)
        }
        installKeys()
    }

    /// Carga la solicitud y pinta el contenido sin mostrar la ventana ni tocar el foco.
    func load(_ record: RequestRecord, draft: Draft) {
        let same = rec?.key == record.key && rec?.state.generationText == record.state.generationText
        rec = record
        if !same {
            self.draft = draft
            localSubmitted = false; confirmingRelease = false; localError = nil; localNote = nil
            shownSignature = nil
            build(record)
            activeQ = firstUnanswered() ?? 0
            scroll.contentView.scroll(to: .zero)
            scroll.reflectScrolledClipView(scroll.contentView)
        }
        refresh()
    }

    func close() {
        rec = nil
        shownSignature = nil
        window.orderOut(nil)
        if let m = keyMonitor { NSEvent.removeMonitor(m); keyMonitor = nil }
        // Si en este mismo ciclo se abre la siguiente pregunta (encadenado), el foco se queda en Pancho.
        DispatchQueue.main.async { [weak self] in self?.restorePreviousApp() }
    }

    private func activateForInput() {
        let me = NSRunningApplication.current
        if !NSApp.isActive, let front = NSWorkspace.shared.frontmostApplication, front != me {
            previousApp = front
        }
        if #available(macOS 14.0, *) { NSApp.activate() } else { me.activate(options: []) }
    }

    private func restorePreviousApp() {
        guard !window.isVisible else { return }
        let prev = previousApp
        previousApp = nil
        guard NSApp.isActive, let p = prev, !p.isTerminated else { return }
        p.activate(options: [])
    }

    /// Pancho no tiene barra de menús (LSUIElement), así que los atajos de edición
    /// (⌘X ⌘C ⌘V ⌘A ⌘Z ⇧⌘Z) se envían a mano al campo con el foco.
    private func handleEditShortcut(_ e: NSEvent, _ flags: NSEvent.ModifierFlags) -> Bool {
        guard flags.contains(.command), !flags.contains(.control), !flags.contains(.option),
              let ch = e.charactersIgnoringModifiers?.lowercased() else { return false }
        let shift = flags.contains(.shift)
        let action: Selector?
        switch (ch, shift) {
        case ("x", false): action = #selector(NSText.cut(_:))
        case ("c", false): action = #selector(NSText.copy(_:))
        case ("v", false): action = #selector(NSText.paste(_:))
        case ("a", false): action = #selector(NSText.selectAll(_:))
        case ("z", false): action = Selector(("undo:"))
        case ("z", true): action = Selector(("redo:"))
        default: action = nil
        }
        guard let a = action else { return false }
        return NSApp.sendAction(a, to: nil, from: self)
    }

    /// Nuevo estado leído del almacén para la solicitud abierta. Solo repinta si cambian
    /// la fase o los datos relevantes (estado, response/release, can_retry, waiter vivo, mensaje…).
    /// Devuelve true si ha repintado.
    @discardableResult
    func update(_ record: RequestRecord) -> Bool {
        guard let cur = rec, cur.key == record.key else { return false }
        if cur.state.generationText != record.state.generationText {
            open(record, draft: draft, focus: window.isKeyWindow); return true
        }
        rec = record
        let samePhase = QuestionPanel.phase(for: record, submitted: localSubmitted, confirming: confirmingRelease) == phase
        if samePhase && record.signature == shownSignature { return false }
        refresh()
        return true
    }

    private func installKeys() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let self = self, e.window === self.window, self.isOpen else { return e }
            return self.handleKey(e) ? nil : e
        }
    }

    private func handleKey(_ e: NSEvent) -> Bool {
        let flags = e.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if handleEditShortcut(e, flags) { return true }
        if e.keyCode == 53 { delegate?.panelHide(); return true }                       // Esc
        if (e.keyCode == 36 || e.keyCode == 76) && flags.contains(.command) {            // ⌘↩
            if sendButton.isEnabled && !sendButton.isHidden { send() }
            return true
        }
        if window.firstResponder is NSTextView { return false }
        guard flags.subtracting([.numericPad, .function]).isEmpty, let ch = e.charactersIgnoringModifiers,
              let n = Int(ch), n >= 1, n <= 9, isEditable, activeQ < sections.count else { return false }
        let s = sections[activeQ]
        guard n - 1 < s.q.options.count else { return false }
        toggle(section: activeQ, option: n - 1, viaKey: true)
        return true
    }

    // MARK: Construcción

    private func build(_ r: RequestRecord) {
        sections.forEach { $0.removeFromSuperview() }
        sections = []
        let st = r.state
        for (i, q) in st.questions.enumerated() {
            let s = QuestionSection(q: q, index: i, count: st.questions.count)
            for row in s.rows {
                row.onClick = { [weak self] in self?.toggle(section: i, option: row.index, viaKey: false) }
                row.onHover = { [weak self] inside in if inside, let self = self, self.activeQ != i { self.activeQ = i; self.refresh() } }
            }
            s.box.text.string = draft.other[q.id] ?? ""
            s.box.onChange = { [weak self] text in self?.textChanged(section: i, text: text) }
            s.box.onFocus = { [weak self] in if let self = self, self.activeQ != i { self.activeQ = i; self.refresh() } }
            sections.append(s)
            doc.addSubview(s)
        }
        kindLabel.stringValue = st.questions.count > 1 ? "\(st.questions.count) preguntas" : "Pregunta"
        provMark.layer?.backgroundColor = providerColor(st.provider).cgColor
        metaLabel.stringValue = "\(providerName(st.provider)) · \(clockText(st.createdAt))"
        titleLabel.stringValue = "\(st.projectName) · sesión \(st.shortSession)"
        titleLabel.toolTip = "Proyecto: \(st.cwd ?? st.projectName)\nSesión: \(st.sessionId)\nLlamada: \(st.callId)"
        releaseButton.isHidden = st.provider != "claude"
        root.setAccessibilityLabel("Pregunta de \(providerName(st.provider)) en \(st.projectName)")
    }

    private func firstUnanswered() -> Int? {
        guard let r = rec else { return nil }
        return r.state.questions.firstIndex { composeAnswer($0, draft: draft) == nil }
    }

    var isEditable: Bool { if case .editing = phase { return true }; return false }

    /// Respuestas que se muestran y se copian: las enviadas o, si no hay, las del borrador.
    private func shownAnswers() -> [String: String] {
        guard let r = rec else { return [:] }
        if let s = r.sentAnswers ?? ((r.hasResponse || localSubmitted || r.state.status == "unconfirmed") ? r.state.answers : nil) { return s }
        var out: [String: String] = [:]
        for q in r.state.questions { if let a = composeAnswer(q, draft: draft) { out[q.id] = a } }
        return out
    }

    private func copyAnswers() {
        guard let r = rec else { return }
        let text = copyText(r.state.questions, answers: shownAnswers())
        guard !text.isEmpty else { localNote = "No hay nada que copiar."; refresh(); return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        localNote = "Copiado. Pégalo en \(providerName(r.state.provider))."
        refresh()
    }

    // MARK: Interacción

    private func toggle(section i: Int, option o: Int, viaKey: Bool) {
        guard isEditable, let r = rec, i < r.state.questions.count else { return }
        let q = r.state.questions[i]
        var sel = draft.selections[q.id] ?? []
        if q.multiSelect {
            if let k = sel.firstIndex(of: o) { sel.remove(at: k) } else { sel.append(o); sel.sort() }
        } else {
            sel = [o]
            // En opción única, elegir una opción sustituye al texto propio.
            if !(draft.other[q.id] ?? "").isEmpty { draft.other[q.id] = ""; sections[i].box.text.string = "" }
        }
        draft.selections[q.id] = sel
        activeQ = i
        if !q.multiSelect, let next = r.state.questions.indices.first(where: { $0 != i && composeAnswer(r.state.questions[$0], draft: draft) == nil }) {
            activeQ = next
            if viaKey { scrollToSection(next) }
        }
        delegate?.panelDraftChanged(draft)
        refresh()
    }

    private func textChanged(section i: Int, text: String) {
        guard let r = rec, i < r.state.questions.count else { return }
        let q = r.state.questions[i]
        draft.other[q.id] = text
        if !q.multiSelect && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { draft.selections[q.id] = [] }
        activeQ = i
        delegate?.panelDraftChanged(draft)
        refresh()
    }

    private func scrollToSection(_ i: Int) {
        guard i < sections.count else { return }
        let f = sections[i].frame
        doc.scrollToVisible(NSRect(x: 0, y: f.minY - 8, width: 1, height: min(f.height, scroll.contentSize.height - 16)))
    }

    private func send() {
        if case .retrySend = phase { retrySend(); return }
        guard let r = rec, isEditable, r.canSend else { return }
        var answers: [String: String] = [:]
        for (i, q) in r.state.questions.enumerated() {
            guard let a = composeAnswer(q, draft: draft) else {
                localError = "Responde la pregunta \(i + 1) antes de enviar."; refresh(); return
            }
            answers[q.id] = a
        }
        delegate?.panelDraftChanged(draft)
        if let err = delegate?.panelSend(answers) {
            localError = err
        } else {
            localError = nil
            localSubmitted = true
            rec?.sentAnswers = answers
            rec?.hasResponse = true
            // Codex: la invocación ya está en curso; no mostrar «Reintentar» mientras dura.
            if rec?.state.isCodex == true { rec?.sendInFlight = true }
        }
        refresh()
    }

    /// Codex: vuelve a invocar `codex-send` con la response.json conservada. Solo por acción del usuario.
    private func retrySend() {
        guard let r = rec, r.canRetrySend else { return }
        if let err = delegate?.panelRetry() { localError = err } else { localError = nil; rec?.sendInFlight = true }
        refresh()
    }

    private func confirmRelease() {
        if let err = delegate?.panelRelease() { localError = err } else { rec?.hasRelease = true; localError = nil }
        confirmingRelease = false
        refresh()
    }

    // MARK: Estado

    static func phase(for r: RequestRecord, submitted: Bool, confirming: Bool) -> PanelPhase {
        let s = r.state.status
        if s == "accepted" || s == "continued" { return .received }
        if dismissibleStatuses.contains(s) { return .terminal(s) }   // released, expired, resolved_elsewhere, unconfirmed
        if r.hasRelease { return .releasing }
        if s == "error" {
            if r.canSend { return confirming ? .confirmRelease : .editing(retry: true) }
            if r.state.isCodex && r.state.canRetry && r.hasResponse { return .retrySend }
            return r.isStale ? .stale : .problem
        }
        // Codex: response.json guardada, estado aún `pending` y nada en curso → «Reintentar» (nunca automático).
        if r.codexUnsent { return .retrySend }
        if r.hasResponse || submitted || s == "queued" || s == "sending" || s == "sent" { return s == "sent" ? .sent : .sending }
        if r.canSend { return confirming ? .confirmRelease : .editing(retry: false) }
        return .stale
    }

    private func setBanner(_ buttons: [PillButton]) {
        guard buttons.map(ObjectIdentifier.init) != bannerButtons.map(ObjectIdentifier.init) else { return }
        bannerButtons.forEach { $0.removeFromSuperview() }
        bannerButtons = buttons
        buttons.forEach(banner.addSubview)
    }

    func refresh() {
        guard let r = rec else { return }
        let newPhase = QuestionPanel.phase(for: r, submitted: localSubmitted, confirming: confirmingRelease)
        if newPhase != phase { localNote = nil; localError = nil }   // avisos locales de la fase anterior
        phase = newPhase
        shownSignature = r.signature
        let st = r.state
        let editable = isEditable
        let shown = editable ? [:] : shownAnswers()
        let delivered = r.sentAnswers != nil || r.hasResponse || localSubmitted || st.status == "unconfirmed"

        // Secciones (solo lectura salvo en edición; el borrador sigue visible)
        for (i, s) in sections.enumerated() {
            let q = s.q
            let sel = draft.selections[q.id] ?? []
            let text = draft.other[q.id] ?? ""
            let hasText = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            for row in s.rows {
                row.checked = sel.contains(row.index)
                if row.isEnabled != editable { row.isEnabled = editable }
                row.inactive = q.multiSelect && hasText
                let kbd = editable && i == activeQ
                if row.showKbd != kbd { row.showKbd = kbd }
            }
            if s.box.text.string != text && window.firstResponder !== s.box.text { s.box.text.string = text }
            s.box.filled = hasText
            s.box.setEditable(editable)
            let answered = sections.count > 1 && composeAnswer(q, draft: draft) != nil
            if s.check.isHidden == answered { s.check.isHidden = !answered }
            if !editable, delivered, let a = shown[q.id] {
                if s.sentText.stringValue != a { s.sentText.stringValue = a }
                s.sentBox.isHidden = false
            } else if !s.sentBox.isHidden { s.sentBox.isHidden = true }
        }

        // Subtítulo: espera de Claude
        if let e = st.expiresAt, st.provider == "claude" {
            subLabel.stringValue = Date().timeIntervalSince1970 < e ? "Claude espera hasta \(clockText(e))" : "Claude dejó de esperar a las \(clockText(e))"
            subLabel.isHidden = false
        } else { subLabel.isHidden = true }

        // Botones y mensajes
        var bannerText: String? = nil
        var bannerError = false
        var buttons: [PillButton] = []
        sendButton.isHidden = false
        sendButton.busy = false; sendButton.showCheck = false; sendButton.style = .primary
        let isClaude = st.provider == "claude"
        releaseButton.isHidden = true
        releaseButton.isEnabled = false
        let missing = st.questions.filter { composeAnswer($0, draft: draft) == nil }
        let canCopy = !copyText(st.questions, answers: shown).isEmpty

        switch phase {
        case .editing(let retry):
            sendButton.title = retry ? "Reintentar" : "Enviar"
            sendButton.isEnabled = missing.isEmpty && r.canSend
            // «Responder en Claude» también al reintentar, mientras el hook siga esperando.
            releaseButton.isHidden = !isClaude
            releaseButton.isEnabled = isClaude && r.canSend
            if missing.isEmpty { footerMsg.stringValue = "⌘↩ para enviar" }
            else if st.questions.count > 1 {
                footerMsg.stringValue = "Falta: " + st.questions.enumerated()
                    .filter { composeAnswer($0.element, draft: draft) == nil }
                    .map { $0.element.header ?? "pregunta \($0.offset + 1)" }.joined(separator: ", ")
            } else {
                footerMsg.stringValue = st.questions.allSatisfy({ $0.options.isEmpty }) ? "Escribe tu respuesta" : "Elige una opción o escribe tu respuesta"
            }
            if retry {
                bannerText = (st.isCodex ? st.message.map(retryableMessage) : st.message)
                    ?? "No se pudo enviar. Tu respuesta se ha conservado; puedes reintentar o cambiarla."
                bannerError = true
            }
        case .confirmRelease:
            sendButton.title = "Enviar"; sendButton.isEnabled = false
            footerMsg.stringValue = ""
            bannerText = "La pregunta volverá a Claude y la responderás allí. Pancho no enviará nada."
            buttons = [cancelReleaseButton, confirmReleaseButton]
        case .releasing:
            sendButton.isHidden = true
            footerMsg.stringValue = "Devolviendo la pregunta a Claude…"
        case .sending:
            if st.status == "queued" {
                sendButton.title = "En cola"; sendButton.busy = true; sendButton.isEnabled = false
                if r.blockedByPrevious { footerMsg.stringValue = "En cola: esperando la respuesta anterior" }
                else { footerMsg.stringValue = st.message ?? (st.isCodex ? "En cola: se enviará cuando Codex termine el turno" : "En cola: se enviará cuando termine el turno.") }
            } else {
                sendButton.title = "Enviando…"; sendButton.busy = true; sendButton.isEnabled = false
                footerMsg.stringValue = "Esperando confirmación de recepción…"
            }
            if let n = r.sendNote { bannerText = n; bannerError = true; if canCopy { buttons = [copyButton] } }
        case .sent:
            sendButton.title = "Enviada"; sendButton.isEnabled = false
            footerMsg.stringValue = st.isCodex ? (st.message ?? "Enviada, esperando confirmación") : "Enviada, esperando confirmación"
        case .retrySend:
            sendButton.title = r.sendInFlight ? "Enviando…" : "Reintentar"
            sendButton.busy = r.sendInFlight
            sendButton.isEnabled = r.canRetrySend
            footerMsg.stringValue = "Tu respuesta se conserva. «Reintentar» la envía tal cual."
            bannerError = true
            if st.status == "pending" {
                bannerText = "Tu respuesta está guardada, pero no consta que se enviara a Codex (Pancho se cerró antes de enviarla o el envío no arrancó). «Reintentar» la envía tal cual."
            } else {
                bannerText = st.message.map(retryableMessage) ?? "No se pudo enviar. Tu respuesta se ha conservado."
            }
            if let n = r.sendNote { bannerText = (bannerText ?? "") + "\n" + n }
            if canCopy { buttons = [copyButton] }
        case .received:
            sendButton.title = "Recibido"; sendButton.showCheck = true; sendButton.style = .ok; sendButton.isEnabled = false
            footerMsg.stringValue = ""
        case .problem:
            sendButton.isHidden = true
            footerMsg.stringValue = st.isCodex && !canCopy ? "" : "Tu respuesta se conserva."
            bannerError = true
            bannerText = st.message ?? "No se pudo enviar. Tu respuesta se ha conservado."
            if r.isCodexBlockingError {
                bannerText = (bannerText ?? "") + "\nEsperando confirmación de Codex: si consta que la recibió, se marcará sola; si no, pasará a «Sin confirmar». Mientras tanto, las demás respuestas de este chat esperan."
            }
            if let n = r.sendNote { bannerText = (bannerText ?? "") + "\n" + n }
            // Codex sin `can_retry`: «Copiar»; «Quitar» solo si no bloquea el chat (dismissed.json, acción explícita).
            buttons = (canCopy ? [copyButton] : []) + (r.isDismissible ? [dismissButton] : [])
        case .stale:
            sendButton.isHidden = true
            footerMsg.stringValue = canCopy ? "Tu borrador se conserva en solo lectura." : ""
            bannerText = "Claude dejó de esperar; no se entregó. Contesta en \(providerName(st.provider))."
            buttons = canCopy ? [copyButton, dismissButton] : [dismissButton]
        case .terminal(let s):
            sendButton.isHidden = true
            footerMsg.stringValue = canCopy ? (delivered ? "Tu respuesta se conserva en solo lectura." : "Tu borrador se conserva en solo lectura.") : ""
            switch s {
            case "released":
                bannerText = st.isCodex ? "Contesta en Codex: la pregunta se responde allí. Pancho no ha enviado nada."
                    : "Devuelta a Claude: la pregunta se responde allí. Pancho no ha enviado nada."
            case "resolved_elsewhere": bannerText = "Ya se respondió desde \(providerName(st.provider)). No hace falta hacer nada."
            case "unconfirmed": bannerText = "Sin confirmar: no consta que la conversación la haya recibido. No se reenvía automáticamente."
            default: bannerText = st.provider == "claude" ? "Claude ya no espera esta respuesta. Contéstala en Claude." : "Esta pregunta ha caducado."
            }
            if let m = st.message { bannerText = m }
            bannerError = s == "unconfirmed"
            buttons = canCopy ? [copyButton, dismissButton] : [dismissButton]
        }
        if let note = localNote, localError == nil { footerMsg.stringValue = note }
        if let err = localError { bannerText = err; bannerError = true }
        setBanner(bannerText == nil ? [] : buttons)
        if let t = bannerText {
            banner.isHidden = false
            if bannerLabel.stringValue != t { bannerLabel.stringValue = t }
            bannerLabel.textColor = bannerError ? C.redDeep : NSColor(hex: 0x3a3a3c)
            banner.layer?.backgroundColor = (bannerError ? C.red.withAlphaComponent(0.1) : NSColor(white: 0, alpha: 0.055)).cgColor
        } else { banner.isHidden = true }
        sendButton.setAccessibilityLabel(sendButton.title)
        releaseButton.setAccessibilityLabel(releaseButton.title)
        delegate?.panelNeedsLayout()
    }

    // MARK: Medidas

    /// Coloca todo para un ancho y una altura máxima; devuelve la altura total del panel.
    func layoutContent(width w: CGFloat, maxHeight: CGFloat) -> CGFloat {
        // Cabecera
        let kw = fitWidth(kindLabel)
        kindPill.frame = NSRect(x: 16, y: 14, width: 6 + 12 + 4 + kw + 8, height: 20)
        kindIcon.frame = NSRect(x: 6, y: 4, width: 12, height: 12)
        kindLabel.frame = NSRect(x: 22, y: 2, width: kw, height: 16)
        closeButton.frame = NSRect(x: w - 12 - 24, y: 12, width: 24, height: 24)
        var x = kindPill.frame.maxX + 8
        provMark.frame = NSRect(x: x, y: 20.5, width: 7, height: 7); x += 11
        metaLabel.frame = NSRect(x: x, y: 16, width: max(20, closeButton.frame.minX - 8 - x), height: 16)
        var y: CGFloat = 12 + 24 + 7
        let th = labelHeight(titleLabel, width: w - 28)
        titleLabel.frame = NSRect(x: 16, y: y, width: w - 28, height: th); y += th
        if !subLabel.isHidden {
            y += 3
            let sh = labelHeight(subLabel, width: w - 28)
            subLabel.frame = NSRect(x: 16, y: y, width: w - 28, height: sh); y += sh
        }
        let headerH = y + 10
        headerRule.frame = NSRect(x: 0, y: headerH, width: w, height: 0.5)

        // Pie
        var bx = w - 12
        if !sendButton.isHidden {
            let sw = max(76, sendButton.width)
            bx -= sw; sendButton.frame = NSRect(x: bx, y: 0, width: sw, height: sendButton.height); bx -= 8
        }
        if !releaseButton.isHidden {
            let rw = releaseButton.width
            bx -= rw; releaseButton.frame = NSRect(x: bx, y: 0, width: rw, height: releaseButton.height); bx -= 8
        }
        let msgW = max(60, bx - 16)
        let mh = footerMsg.stringValue.isEmpty ? 0 : labelHeight(footerMsg, width: msgW)
        let footerInner = max(30, mh)
        let footerH = 10 + footerInner + 12

        // Aviso
        var bannerH: CGFloat = 0
        if !banner.isHidden {
            let bw = w - 24
            let lh = labelHeight(bannerLabel, width: bw - 19)
            bannerLabel.frame = NSRect(x: 11, y: 8, width: bw - 19, height: lh)
            var by = 8 + lh
            if !bannerButtons.isEmpty {
                by += 8
                var rx = bw - 8
                for b in bannerButtons.reversed() {
                    let bwid = b.width; rx -= bwid
                    b.frame = NSRect(x: rx, y: by, width: bwid, height: b.height); rx -= 8
                }
                by += 26
            }
            bannerH = by + 8
        }

        // Cuerpo
        let cw = w - 32
        var cy: CGFloat = 12
        for (i, s) in sections.enumerated() {
            let h = s.layoutSection(width: cw)
            s.frame.origin = NSPoint(x: 16, y: cy)
            cy += h
            if i < sections.count - 1 { cy += 16 }
        }
        let contentH = cy + 14
        let fixed = headerH + 0.5 + (bannerH > 0 ? bannerH + 10 : 0) + 0.5 + footerH
        // El panel nunca pasa de maxHeight (área útil): si hace falta, el cuerpo se reduce (se desplaza dentro).
        let bodyH = max(0, min(contentH, maxHeight - fixed))
        doc.frame = NSRect(x: 0, y: 0, width: w, height: contentH)
        scroll.frame = NSRect(x: 0, y: headerH + 0.5, width: w, height: bodyH)
        var yy = headerH + 0.5 + bodyH
        if bannerH > 0 { banner.frame = NSRect(x: 12, y: yy, width: w - 24, height: bannerH); yy += bannerH + 10 }
        footerRule.frame = NSRect(x: 0, y: yy, width: w, height: 0.5); yy += 0.5
        let fy = yy + 10
        if !sendButton.isHidden { sendButton.frame.origin.y = fy + (footerInner - sendButton.height) / 2 }
        if !releaseButton.isHidden { releaseButton.frame.origin.y = fy + (footerInner - releaseButton.height) / 2 }
        footerMsg.frame = NSRect(x: 16, y: fy + (footerInner - mh) / 2, width: msgW, height: mh)
        return min(yy + footerH, maxHeight)
    }
}
