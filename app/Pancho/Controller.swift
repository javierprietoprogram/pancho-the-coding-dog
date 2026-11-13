import AppKit

enum StackMode { case down, up }

/// Coordina almacén, Pancho, barra, lista y panel. Pancho manda: todo se coloca a partir de su posición.
final class Controller: NSObject, NSApplicationDelegate, PetDelegate, QuestionPanelDelegate {
    let store = Store()
    let library: SpriteLibrary
    let animator: Animator

    let petWindow = FloatingPanel(size: NSSize(width: M.petW, height: M.petH), shadow: false)
    let petView = PetView(frame: NSRect(x: 0, y: 0, width: M.petW, height: M.petH))
    let barWindow = FloatingPanel(size: NSSize(width: M.barW, height: M.barH), shadow: true)
    let barView = BarView(frame: NSRect(x: 0, y: 0, width: M.barW, height: M.barH))
    let listWindow = FloatingPanel(size: NSSize(width: M.cardW, height: M.cardH), shadow: true)
    let listView = ListView(frame: NSRect(x: 0, y: 0, width: M.cardW, height: M.cardH))
    let panel = QuestionPanel()
    /// Panel de agentes: conversaciones de Claude Code y Codex con sus subagentes (cuarto botón de la barra).
    lazy var agentsPanel: AgentsPanel = {
        let d = AgentsPanel()
        d.anchorScreen = { [weak self] in self?.petWindow.screen }
        return d
    }()
    private var agentsPanelCreated = false
    var subagents: [SubagentInfo] = []
    /// Subagentes de Codex desde su índice local (también los lanzados antes de conectar los avisos).
    let codexIndex = CodexIndex()
    /// Codex: invoca `codex-send` una vez tras escribir response.json (o al pulsar «Reintentar»).
    lazy var codexSender: CodexSender = {
        let s = CodexSender(home: store.home)
        // Un proceso que superó el plazo sigue «en curso» hasta que termina; entonces se retira el aviso.
        s.onLateExit = { [weak self] key in self?.codexNotes[key] = nil; self?.poll() }
        return s
    }()
    /// Codex: aviso de la última invocación por clave, cuando state.json no lo explica. Se guarda el
    /// estado visto en el primer sondeo tras la invocación: si state.json cambia después, el aviso se retira.
    var codexNotes: [String: (note: String, status: String?)] = [:]

    var records: [String: RequestRecord] = [:]
    var sessions: [SessionInfo] = []
    var drafts: [String: Draft] = [:]
    /// Conversaciones quitadas con la ×: no se ven hasta que hacen una pregunta nueva.
    lazy var hiddenSessions: [String: Double] = store.loadHiddenSessions()
    /// Conversaciones que Pancho vio trabajar y terminaron: siguen en verde hasta la × (o 12 h).
    lazy var finishedSessions: [String: Double] = store.loadFinishedSessions()
    var models: [String: CardModel] = [:]
    var order: [String] = []
    var hoverList = false
    var pendingReorder = false
    var unfreezeTimer: Timer?

    var collapsed = UserDefaults.standard.bool(forKey: "pancho.collapsed")
    var muted = UserDefaults.standard.bool(forKey: "pancho.muted")
    /// Abrir la pregunta en cuanto llega (sin quitar el teclado a la app en uso) o esperar al clic.
    var autoOpen = Prefs.autoOpen
    var statusMenu: StatusMenu?
    lazy var setup = SetupWindow(controller: self)
    /// Con PANCHO_HOME (pruebas) no se toca la instalación real ni se abre la bienvenida.
    #if PANCHO_DEBUG
    /// Depuración: PANCHO_DEBUG_REAL_STATUS=1 deja leer el estado real de la conexión (sin pulsar nada).
    let testMode = (ProcessInfo.processInfo.environment["PANCHO_HOME"].map { !$0.isEmpty } ?? false)
        && ProcessInfo.processInfo.environment["PANCHO_DEBUG_REAL_STATUS"] != "1"
    #else
    let testMode = ProcessInfo.processInfo.environment["PANCHO_HOME"].map { !$0.isEmpty } ?? false
    #endif
    var reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    var simulation = false
    var mode: StackMode = .down
    var petOrigin = NSPoint.zero

    var oneShot: String?
    /// El ratón está sobre Pancho: solo entonces se anima (petición de Javier: quieto el resto del tiempo).
    var hovering = false
    var dragging = false
    var dragDir = "right"
    var dragStill = false
    var lastPointerX: CGFloat = 0
    var stillTimer: Timer?

    var firstPoll = true
    var lastStatus: [String: String] = [:]
    var lastActivity: [String: String] = [:]
    var lastCount = 0
    var closingAfterReceive = false
    var timers: [Timer] = []
    var listShown = false
    /// Evita App Nap mientras hay preguntas pendientes (el puente vigila el latido).
    private var pendingActivity: NSObjectProtocol?
    /// Lo último que se pintó: si el sondeo no cambia nada, no se repinta ni se recoloca.
    private var lastRender: RenderSignature?
    private var mouseMonitors: [Any] = []
    #if PANCHO_DEBUG
    var debug: DebugHooks?
    #endif

    override init() {
        let res = Bundle.main.resourceURL?.appendingPathComponent("pancho", isDirectory: true)
            ?? URL(fileURLWithPath: "pancho", isDirectory: true)
        library = SpriteLibrary(directory: res)
        animator = Animator(library: library)
        super.init()
    }

    // MARK: Arranque

    func applicationDidFinishLaunching(_ notification: Notification) {
        petWindow.contentView = petView
        petView.delegate = self
        for w in [petWindow, barWindow, listWindow, panel.window] { w.acceptsMouseMovedEvents = true }
        barWindow.contentView = barView
        listWindow.contentView = listView
        panel.delegate = self
        for w in [petWindow, barWindow, listWindow, panel.window] { w.contentView?.wantsLayer = true }

        animator.onFrame = { [weak self] img, mask in self?.petView.show(img, mask: mask) }
        animator.onOneShotEnd = { [weak self] in self?.oneShot = nil; self?.updateSprite() }

        barView.refresh.onClick = { [weak self] in self?.refreshConversations() }
        barView.mute.onClick = { [weak self] in self?.toggleMute() }
        barView.toggle.onClick = { [weak self] in self?.toggleCollapsed() }
        barView.agents.onClick = { [weak self] in self?.toggleAgentsPanel() }
        listView.onClick = { [weak self] id in self?.cardClicked(id) }
        listView.onDismiss = { [weak self] id in self?.dismissCard(id) }
        listView.onHover = { [weak self] inside in self?.listHover(inside) }

        restorePosition()
        store.writeHeartbeat()
        poll()
        listWindow.orderFrontRegardless()
        barWindow.orderFrontRegardless()
        petWindow.orderFrontRegardless()

        #if PANCHO_DEBUG
        debug = DebugHooks(controller: self)
        schedule(0.5) { [weak self] in self?.poll(); self?.debug?.tick() }
        #else
        schedule(0.5) { [weak self] in self?.poll() }
        #endif
        schedule(2.0) { [weak self] in self?.store.writeHeartbeat() }
        installClickThroughMonitors()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.triggerOneShot("waving") }
        #if !PANCHO_HARNESS
        statusMenu = StatusMenu(controller: self)
        statusMenu?.setCount(pendingRecords.count)
        if !testMode && !Prefs.welcomed {
            Prefs.welcomed = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.showSetup() }
        }
        #endif

        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
                                                          object: nil, queue: .main) { [weak self] _ in
            guard let self = self else { return }
            self.reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            self.oneShot = nil
            self.updateSprite(force: true)
            self.render(force: true)
        }
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            self?.layout()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        mouseMonitors.forEach(NSEvent.removeMonitor)
        mouseMonitors = []
        setPendingActivity(false)
    }

    private func schedule(_ interval: TimeInterval, _ block: @escaping () -> Void) {
        let t = Timer(timeInterval: interval, repeats: true) { _ in block() }
        RunLoop.main.add(t, forMode: .common)
        timers.append(t)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // MARK: Lectura periódica (~0,5 s)

    func poll() {
        simulation = store.readSimulation()
        var next: [String: RequestRecord] = [:]
        for r in store.scanRequests() {
            if let other = next[r.key], other.state.createdAt >= r.state.createdAt { continue }
            next[r.key] = r
        }
        var arrived = false, failed = false, finished = false
        var arrivedKeys: [String] = []
        for r in next.values {
            let prev = lastStatus[r.key]
            if !firstPoll && prev == nil && r.countsAsPending { arrived = true; arrivedKeys.append(r.key) }
            if let p = prev, p != r.state.status, r.state.status == "error" || r.state.status == "unconfirmed" { failed = true }
            lastStatus[r.key] = r.state.status
        }
        lastStatus = lastStatus.filter { next[$0.key] != nil }
        sessions = store.scanSessions()
        let now0 = Date().timeIntervalSince1970
        subagents = mergeSubagents(hooks: store.scanAgents(), codex: codexIndex.rows.compactMap { codexSubagent($0, now: now0) })
        codexIndex.refresh(now: now0) { [weak self] in self?.poll() }
        var restarted: [String] = []
        let previousActivity = lastActivity
        for s in sessions {
            if lastActivity[s.id] == "working" && s.activity == "idle" && !firstPoll { finished = true }
            // Una conversación quitada con la × vuelve sola cuando empieza a ejecutarse de nuevo.
            if let prev = lastActivity[s.id], prev != "working", s.activity == "working", hiddenSessions[s.id] != nil { restarted.append(s.id) }
            lastActivity[s.id] = s.activity
        }
        if !restarted.isEmpty {
            restarted.forEach { hiddenSessions[$0] = nil }
            store.saveHiddenSessions(hiddenSessions)
        }
        // Al empezar no se sabe qué se vio trabajar: se conservan las ya guardadas y solo se marcan las nuevas transiciones.
        let scanned = FileManager.default.fileExists(atPath: store.sessionsDir.path)
        let finishedNext = !scanned ? finishedSessions : updateFinished(finishedSessions, previous: firstPoll ? [:] : previousActivity, sessions: sessions, now: Date().timeIntervalSince1970)
        if finishedNext != finishedSessions { finishedSessions = finishedNext; store.saveFinishedSessions(finishedNext) }
        let sessionIds = Set(sessions.map(\.id))
        lastActivity = lastActivity.filter { sessionIds.contains($0.key) }
        codexNotes = codexNotes.filter { next[$0.key] != nil }
        let blocked = codexBlockedKeys(next.values)
        for key in next.keys {
            next[key]?.blockedByPrevious = blocked.contains(key)
            next[key]?.sendInFlight = codexSender.inFlight.contains(key)
            guard let n = codexNotes[key], let status = next[key]?.status else { continue }
            if n.status == nil { codexNotes[key] = (n.note, status) }
            if n.status == nil || n.status == status { next[key]?.sendNote = n.note } else { codexNotes[key] = nil }
        }
        records = next
        firstPoll = false
        for r in records.values where r.isPending && drafts[r.key] == nil {
            drafts[r.key] = store.loadDraft(r) ?? Draft()
        }
        drafts = drafts.filter { records[$0.key] != nil }
        setPendingActivity(records.values.contains(where: \.isPending))

        if let key = panel.rec?.key {
            if let r = records[key] { panel.update(r); handlePanelPhase() } else { hidePanel() }
        }
        if failed { triggerOneShot("failed") } else if finished { triggerOneShot("jumping") } else if arrived { triggerOneShot("waving") }
        // Ajuste «Abrir las preguntas al llegar»: la más antigua de las recién llegadas, sin tomar el teclado
        // y sin sustituir a la que ya esté abierta.
        if autoOpen, !panel.isOpen, !closingAfterReceive,
           let r = arrivedKeys.compactMap({ records[$0] }).filter(\.needsAction)
            .min(by: { ($0.state.createdAt, $0.key) < ($1.state.createdAt, $1.key) }) {
            openPanel(r.key, focus: false)
        }
        render()
        agentsUpdated()
        updateClickThrough()   // respaldo barato de los monitores de ratón
    }

    private func setPendingActivity(_ on: Bool) {
        if on, pendingActivity == nil {
            pendingActivity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                                                                    reason: "Pancho tiene preguntas pendientes")
        } else if !on, let a = pendingActivity {
            ProcessInfo.processInfo.endActivity(a)
            pendingActivity = nil
        }
    }

    private func handlePanelPhase() {
        guard let key = panel.rec?.key else { return }
        // Contestada en Claude o Codex mientras el panel estaba abierto: se cierra solo.
        if case .terminal(let st) = panel.phase, st == "resolved_elsewhere" { hidePanel(); return }
        // Los estados terminales y la falta de espera no se cierran solos: el usuario oculta o quita.
        guard case .received = panel.phase, !closingAfterReceive else { return }
        closingAfterReceive = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.65) { [weak self] in
            guard let self = self else { return }
            self.closingAfterReceive = false
            guard self.panel.rec?.key == key, case .received = self.panel.phase else { return }
            // El encadenado solo toma el teclado si el panel ya lo tenía.
            let wasKey = self.panel.window.isKeyWindow
            self.panel.close()
            self.triggerOneShot("waving")
            // Decisión aprobada: tras una aceptación confirmada se abre la siguiente pendiente.
            if let next = self.nextActionable(excluding: key) { self.openPanel(next, focus: wasKey) } else { self.render() }
        }
    }

    // MARK: Modelo de tarjetas

    /// Pendientes según el contrato (rev. 1): sin `unconfirmed` ni las que se quedaron sin espera.
    var pendingRecords: [RequestRecord] {
        records.values.filter(\.countsAsPending).sorted { ($0.state.createdAt, $0.key) < ($1.state.createdAt, $1.key) }
    }

    func nextActionable(excluding: String? = nil) -> String? {
        pendingRecords.first { $0.needsAction && $0.key != excluding }?.key
    }

    func buildModels() -> (models: [String: CardModel], sorted: [String]) {
        let now = Date().timeIntervalSince1970
        var out: [String: CardModel] = [:]
        var keys: [(rank: Int, created: Double, id: String)] = []
        var sessionsWithCard = Set<String>()
        let blockedKeys = codexBlockedKeys(records.values)
        for r in records.values {
            let st = r.state
            guard !r.dismissed else { continue }
            // Preguntas llegadas con Pancho cerrado: solo hay metadatos y ya se contestaron en Claude.
            guard !st.questions.isEmpty else { continue }
            // Informativas (terminales y «sin confirmar»): visibles 12 h desde el último cambio o hasta que se quiten.
            // Contestada en la propia app (Claude o Codex): no queda nada que avisar, la tarjeta se va sola.
            let recentInfo = dismissibleStatuses.contains(st.status) && st.status != "resolved_elsewhere"
                && now - max(st.createdAt, st.updatedAt) < 12 * 3600
            guard r.isPending || recentInfo else { continue }
            let cs = cardStatus(for: r, blockedByPrevious: blockedKeys.contains(r.key))
            let draft = (st.status == "pending" || st.status == "error") && !r.hasResponse && !r.hasRelease && (drafts[r.key]?.hasContent ?? false)
            let id = "r:" + r.key
            out[id] = CardModel(id: id, title: "\(st.projectName) · sesión \(st.shortSession)", status: cs.label, indicator: cs.indicator,
                                tone: cs.tone, provider: st.provider, draft: draft, chevron: cs.chevron, dismissible: r.userCanQuit,
                                attention: cs.attention, clickable: true,
                                tooltip: "\(st.cwd ?? st.projectName)\nSesión \(st.sessionId)" + (st.isCodex && st.status == "error" ? (st.message.map { "\n" + $0 } ?? "") : ""))
            keys.append((cs.rank, st.createdAt, id))
            sessionsWithCard.insert(st.provider + "|" + st.sessionId)
        }
        // Solo las conversaciones que se están ejecutando ahora (decisión de Javier, 2026-11-11): las paradas o
        // cerradas no tienen tarjeta. Las preguntas y avisos siguen saliendo por su solicitud, arriba.
        for s in sessions where !sessionsWithCard.contains(s.id) && !sessionHidden(s.id)
            && (sessionRunning(s, now: now) || finishedSessions[s.id] != nil) {
            let label: String, ind: Indicator, rank: Int
            let done = !sessionRunning(s, now: now)
            switch done ? "finished" : s.activity {
            // Terminada (la vio trabajar): en verde hasta la × (petición de Javier, 2026-11-12).
            case "finished": label = "Terminada"; ind = .none; rank = 3
            case "working": label = "Trabajando"; ind = .dots; rank = 2
            case "idle": label = "En espera"; ind = .none; rank = 3
            case "observed": label = "Observada"; ind = .none; rank = 3
            // Codex cierra la sesión del hook al acabar cada turno: el chat sigue abierto.
            case "ended": label = s.provider == "codex" ? "En espera" : "Cerrada"; ind = .none; rank = 3
            default: label = "Desconocida"; ind = .none; rank = 3
            }
            let id = "s:" + s.id
            out[id] = CardModel(id: id, title: "\(s.projectName) · sesión \(String(s.sessionId.prefix(8)))", status: label, indicator: ind,
                                tone: done ? .green : .gray, provider: s.provider, draft: false, chevron: false, dismissible: true,
                                attention: false, clickable: openTarget(s) != .none,
                                tooltip: "\(s.cwd ?? s.projectName)\nSesión \(s.sessionId)\n" + openHint(s))
            keys.append((rank, s.firstSeen, id))
        }
        keys.sort { ($0.rank, $0.created, $0.id) < ($1.rank, $1.created, $1.id) }
        return (out, keys.map(\.id))
    }

    var frozen: Bool { hoverList || panel.isOpen }

    private func refreshOrder(_ target: [String]) {
        if frozen {
            // No mover lo que el usuario está a punto de pulsar: solo retirar lo eliminado y añadir al final.
            order = order.filter { models[$0] != nil }
            for id in target where !order.contains(id) { order.append(id) }
            pendingReorder = order != target
        } else {
            order = target; pendingReorder = false
        }
    }

    // MARK: Pintado

    /// Pinta solo si cambia algo visible (tarjetas, orden, contador, modos o panel abierto).
    func render(force: Bool = false) {
        let built = buildModels()
        models = built.models
        refreshOrder(built.sorted)
        let n = pendingRecords.count
        let sig = RenderSignature(models: models, order: order, count: n, collapsed: collapsed, muted: muted,
                                  simulation: simulation, reduceMotion: reduceMotion, openKey: panel.rec?.key)
        if !force, sig == lastRender { updateSprite(); return }
        lastRender = sig
        petView.setBadge(n, pop: n > lastCount && !reduceMotion)
        statusMenu?.setCount(n)
        lastCount = n
        petView.setAccessibilityLabel("Pancho. " + (n == 0 ? "Nada pendiente" : n == 1 ? "1 conversación necesita respuesta" : "\(n) conversaciones necesitan respuesta")
                                      + ". Arrastra para mover; pulsa para abrir la siguiente pendiente.")
        barView.mute.iconView.icon = muted ? .waveOff : .wave
        let muteLabel = muted ? "Reactivar animaciones de Pancho" : "Silenciar animaciones de Pancho"
        barView.mute.toolTip = muteLabel; barView.mute.setAccessibilityLabel(muteLabel)
        let toggleLabel = collapsed ? "Mostrar conversaciones" : "Contraer conversaciones"
        barView.toggle.toolTip = toggleLabel; barView.toggle.setAccessibilityLabel(toggleLabel)
        barView.toggle.setMini(collapsed && n > 0 ? "\(n)" : nil)
        barView.simulation = simulation
        updateSprite()
        layout()
    }

    // MARK: Animación

    var hasWorking: Bool {
        let now = Date().timeIntervalSince1970
        return sessions.contains { $0.activity == "working" && now - $0.lastEventAt < 12 * 3600 }
    }

    func updateSprite(force: Bool = false) {
        let name: String
        if dragging && !dragStill { name = dragDir == "left" ? "running-left" : "running-right" }
        else if let o = oneShot { name = o }
        else if panel.isOpen { name = "review" }
        else if records.values.contains(where: \.countsAsPending) { name = "waiting" }
        else if hasWorking { name = "running" }
        else { name = "idle" }
        // Quieto (fotograma 00 del estado) salvo con el ratón encima, al arrastrar o en una reacción puntual.
        let animate = hovering || dragging || oneShot != nil
        animator.play(name, still: muted || reduceMotion || !animate, force: force)
    }

    func triggerOneShot(_ name: String) {
        guard !muted, !reduceMotion, !dragging, hovering else { return }
        oneShot = name
        animator.play(name, still: false, force: true)
    }

    // MARK: Colocación

    private func screen(containing p: NSPoint) -> NSScreen? {
        if let s = NSScreen.screens.first(where: { $0.frame.contains(p) }) { return s }
        return NSScreen.screens.min { distance(p, $0.frame) < distance(p, $1.frame) }
    }
    private func distance(_ p: NSPoint, _ r: NSRect) -> CGFloat {
        let dx = max(r.minX - p.x, 0, p.x - r.maxX), dy = max(r.minY - p.y, 0, p.y - r.maxY)
        return dx * dx + dy * dy
    }
    private func clampPet(_ o: NSPoint, in vf: NSRect) -> NSPoint {
        NSPoint(x: min(max(o.x, vf.minX), vf.maxX - M.petW), y: min(max(o.y, vf.minY), vf.maxY - M.petH))
    }
    private func clamp(_ v: CGFloat, _ a: CGFloat, _ b: CGFloat) -> CGFloat { max(a, min(b, v)) }

    private func restorePosition() {
        if let a = UserDefaults.standard.array(forKey: "pancho.pos") as? [Double], a.count == 2 {
            let p = NSPoint(x: a[0], y: a[1])
            if NSScreen.screens.contains(where: { $0.visibleFrame.insetBy(dx: -40, dy: -40).contains(NSPoint(x: p.x + M.petW / 2, y: p.y + M.petH / 2)) }) {
                petOrigin = p; return
            }
        }
        let vf = (NSScreen.screens.first ?? NSScreen.main)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        petOrigin = NSPoint(x: vf.maxX - M.edge - M.cardW / 2 - M.petW / 2 - 24, y: vf.maxY - 28 - M.petH)
    }

    private func savePosition() {
        UserDefaults.standard.set([Double(petOrigin.x), Double(petOrigin.y)], forKey: "pancho.pos")
    }

    /// Cambia el marco solo si es distinto (evita repintados y sombras recalculadas en cada sondeo).
    @discardableResult
    private func setFrame(_ w: NSWindow, _ r: NSRect) -> Bool {
        let r = r.integral == r ? r : NSRect(x: r.origin.x.rounded(), y: r.origin.y.rounded(), width: r.width.rounded(), height: r.height.rounded())
        guard w.frame != r else { return false }
        w.setFrame(r, display: true)
        w.invalidateShadow()
        return true
    }

    func layout() {
        guard let scr = screen(containing: NSPoint(x: petOrigin.x + M.petW / 2, y: petOrigin.y + M.petH / 2)) else { return }
        let vf = scr.visibleFrame
        petOrigin = clampPet(petOrigin, in: vf)
        let px = petOrigin.x, py = petOrigin.y, cx = px + M.petW / 2
        let petFrame = NSRect(x: px, y: py, width: M.petW, height: M.petH)
        if petWindow.frame != petFrame { petWindow.setFrame(petFrame, display: true) }

        let barH = simulation ? M.barSimH : M.barH
        let listVisible = !collapsed && !order.isEmpty
        let contentH = CGFloat(order.count) * M.cardH + CGFloat(max(order.count - 1, 0)) * M.cardGap
        let downBarY = py + M.barOverlap - barH            // borde inferior de la barra, debajo de Pancho
        let upBarY = py + M.petH + 2                         // borde inferior de la barra, encima de Pancho
        let downSpace = (downBarY - M.gapBarList) - (vf.minY + M.edge)
        let upSpace = (vf.maxY - M.edge) - (upBarY + barH + M.gapBarList)
        let want = min(contentH, 2 * M.cardH + M.cardGap)
        var newMode: StackMode
        if !listVisible {
            newMode = downBarY >= vf.minY + M.edge ? .down : .up
        } else {
            newMode = downSpace >= want ? .down : (upSpace > downSpace ? .up : .down)
            // Histéresis solo durante el arrastre; al soltar se vuelve a preferir abajo.
            if dragging && newMode != mode && (mode == .down ? downSpace : upSpace) >= want { newMode = mode }
        }
        // La barra nunca sale del área útil (ni invade el Dock).
        if newMode == .down && downBarY < vf.minY + M.edge && upBarY + barH <= vf.maxY - M.edge { newMode = .up }
        if newMode == .up && upBarY + barH > vf.maxY - M.edge && downBarY >= vf.minY + M.edge { newMode = .down }
        mode = newMode
        let rotated = collapsed != (mode == .up)
        if barView.toggle.iconView.rotated != rotated { barView.toggle.iconView.rotated = rotated }

        let barX = clamp(cx - M.barW / 2, vf.minX + M.edge, vf.maxX - M.edge - M.barW)
        let barY = clamp(mode == .down ? downBarY : upBarY, vf.minY + M.edge, vf.maxY - M.edge - barH)
        setFrame(barWindow, NSRect(x: barX, y: barY, width: M.barW, height: barH))

        var rects = [NSRect(x: px + 14, y: py, width: M.petW - 28, height: M.petH),
                     NSRect(x: barX, y: barY, width: M.barW, height: barH)]
        var listFrame: NSRect? = nil
        let listW = min(M.cardW, vf.width - 2 * M.edge)
        listView.update(models: models, order: order, openId: panel.rec.map { "r:" + $0.key }, width: listW, reduceMotion: reduceMotion)
        if listVisible {
            let lx = clamp(cx - listW / 2, vf.minX + M.edge, vf.maxX - M.edge - listW)
            // Hueco real dentro del área útil: la lista se reduce (y se desplaza dentro) antes que salirse.
            let avail = mode == .down ? (barY - M.gapBarList) - (vf.minY + M.edge) : (vf.maxY - M.edge) - (barY + barH + M.gapBarList)
            let maxH = min(avail, CGFloat(M.maxVisibleCards) * M.cardH + CGFloat(M.maxVisibleCards - 1) * M.cardGap)
            let h = min(contentH, maxH)
            if h >= 24 {
                let ly = mode == .down ? barY - M.gapBarList - h : barY + barH + M.gapBarList
                let lf = NSRect(x: lx, y: ly, width: listW, height: h)
                listFrame = lf
                rects.append(lf)
            }
        }

        var hideListForPanel = false
        if panel.isOpen {
            hideListForPanel = placePanel(vf: vf, rects: rects, barY: barY, barH: barH, cx: cx)
        }

        if let lf = listFrame, !hideListForPanel {
            if setFrame(listWindow, lf) { listView.updateFade() }
            if !listShown { listWindow.orderFrontRegardless(); listShown = true; petWindow.orderFrontRegardless() }
        } else if listShown {
            listWindow.orderOut(nil); listShown = false
        }
    }

    /// Devuelve true si el panel ocupa el hueco de la lista (pantalla estrecha).
    private func placePanel(vf: NSRect, rects: [NSRect], barY: CGFloat, barH: CGFloat, cx: CGFloat) -> Bool {
        let pw = min(M.panelW, vf.width - 2 * M.edge)
        let maxH = max(120, min(M.panelMaxH, vf.height - 2 * M.edge))
        let ph = min(panel.layoutContent(width: pw, maxHeight: maxH), vf.height - 2 * M.edge)
        let g = rects.dropFirst().reduce(rects[0]) { $0.union($1) }
        let pet = rects[0]
        let petTop = petOrigin.y + M.petH
        let lo = vf.minY + M.edge, hi = max(lo, vf.maxY - M.edge - ph)
        let alignY = mode == .down ? clamp(petTop - ph, lo, hi) : clamp(petOrigin.y, lo, hi)
        var x: CGFloat, y = alignY, overList = false
        if g.maxX + M.panelGap + pw <= vf.maxX - M.edge { x = g.maxX + M.panelGap }
        else if g.minX - M.panelGap - pw >= vf.minX + M.edge { x = g.minX - M.panelGap - pw }
        else {
            overList = true
            if pet.maxX + M.panelGap + pw <= vf.maxX - M.edge { x = pet.maxX + M.panelGap }
            else if pet.minX - M.panelGap - pw >= vf.minX + M.edge { x = pet.minX - M.panelGap - pw }
            else {
                x = clamp(cx - pw / 2, vf.minX + M.edge, vf.maxX - M.edge - pw)
                y = mode == .down ? clamp(barY - M.gapBarList - ph, lo, hi) : clamp(barY + barH + M.gapBarList, lo, hi)
            }
        }
        setFrame(panel.window, NSRect(x: x, y: y, width: pw, height: ph))
        return overList
    }

    // MARK: Pancho: arrastre, clic y menú

    func petDragBegan() {
        dragging = true; dragStill = false
        lastPointerX = NSEvent.mouseLocation.x
        oneShot = nil
        updateSprite()
    }

    func petDragMoved(origin: NSPoint, pointer: NSPoint) {
        // La pantalla activa es la que queda bajo el puntero.
        let vf = screen(containing: pointer)?.visibleFrame ?? NSScreen.main?.visibleFrame ?? .zero
        petOrigin = clampPet(origin, in: vf)
        let dx = pointer.x - lastPointerX
        if abs(dx) > 1.5 { dragDir = dx > 0 ? "right" : "left" }
        lastPointerX = pointer.x
        dragStill = false
        stillTimer?.invalidate()
        stillTimer = Timer.scheduledTimer(withTimeInterval: 0.22, repeats: false) { [weak self] _ in
            guard let self = self, self.dragging else { return }
            self.dragStill = true; self.updateSprite()
        }
        updateSprite()
        layout()
    }

    func petDragEnded() {
        dragging = false; dragStill = false
        stillTimer?.invalidate()
        savePosition()
        updateSprite()
        layout()
    }

    func petClicked() { openOldest(waveIfNone: true) }
    func petDoubleClicked() { toggleCollapsed() }

    func petContextMenu(_ event: NSEvent, in view: NSView) {
        let menu = NSMenu()
        let a = menu.addItem(withTitle: collapsed ? "Mostrar conversaciones" : "Ocultar conversaciones", action: #selector(toggleCollapsed), keyEquivalent: "")
        a.target = self
        let b = menu.addItem(withTitle: muted ? "Reactivar animaciones" : "Silenciar animaciones", action: #selector(toggleMute), keyEquivalent: "")
        b.target = self
        let s = menu.addItem(withTitle: "Configuración…", action: #selector(showSetup), keyEquivalent: "")
        s.target = self
        menu.addItem(.separator())
        let q = menu.addItem(withTitle: "Salir de Pancho", action: #selector(quit), keyEquivalent: "")
        q.target = self
        NSMenu.popUpContextMenu(menu, with: event, for: view)
    }

    @objc func quit() { NSApp.terminate(nil) }

    @objc func toggleCollapsed() {
        collapsed.toggle()
        UserDefaults.standard.set(collapsed, forKey: "pancho.collapsed")
        render()
    }

    @objc func toggleMute() {
        muted.toggle()
        UserDefaults.standard.set(muted, forKey: "pancho.muted")
        oneShot = nil
        updateSprite(force: true)
        render()
        setup.syncIfVisible()
    }

    @objc func toggleAutoOpen() {
        autoOpen.toggle()
        Prefs.autoOpen = autoOpen
        setup.syncIfVisible()
    }

    /// Cambia el tamaño de Pancho sin moverlo de sitio (mismo centro y misma base).
    func setSize(_ size: PetSize) {
        guard M.petScale != size.scale || Prefs.size != size else { return }
        let oldW = M.petW
        Prefs.size = size
        M.petScale = size.scale
        petOrigin.x += (oldW - M.petW) / 2
        layout()
        petView.setBadge(lastCount, pop: false)
        savePosition()
        updateSprite(force: true)
        render(force: true)
        setup.syncIfVisible()
    }

    @objc func showSetup() { setup.show() }

    // MARK: Panel de agentes

    @objc func toggleAgentsPanel() {
        agentsPanelCreated = true
        agentsPanel.toggle()
        agentsUpdated()
    }

    /// Ayuda del botón con lo que está en marcha (conversaciones trabajando + subagentes) y datos para el panel.
    private func agentsUpdated() {
        let now = Date().timeIntervalSince1970
        let tree = agentsTree(sessions: sessions, agents: subagents, now: now)
        let sum = agentsSummary(tree, now: now)
        // Sin número sobre el botón (decisión de Javier, 2026-11-12): el recuento solo va en la ayuda y en el panel.
        let b = barView.agents
        var parts: [String] = []
        if sum.working > 0 { parts.append(sum.working == 1 ? "1 conversación trabajando" : "\(sum.working) conversaciones trabajando") }
        if sum.runningSubagents > 0 { parts.append(sum.runningSubagents == 1 ? "1 subagente" : "\(sum.runningSubagents) subagentes") }
        let tip = "Agentes · " + (parts.isEmpty ? "todo tranquilo" : parts.joined(separator: " · "))
        if b.toolTip != tip { b.toolTip = tip; b.setAccessibilityLabel(tip) }
        if agentsPanelCreated && agentsPanel.isOpen { agentsPanel.update(agentsPayload(tree, now: now)) }
    }

    /// Clics por lo transparente guiados por eventos: el ratón al moverse (en cualquier app) actualiza
    /// si la ventana de Pancho acepta clics. Sin temporizador rápido; el sondeo de 0,5 s hace de respaldo.
    /// Los monitores globales de ratón no necesitan permiso de accesibilidad y no leen teclas.
    private func installClickThroughMonitors() {
        let mask: NSEvent.EventTypeMask = [.mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged]
        if let g = NSEvent.addGlobalMonitorForEvents(matching: mask, handler: { [weak self] _ in self?.updateClickThrough() }) {
            mouseMonitors.append(g)
        }
        if let l = NSEvent.addLocalMonitorForEvents(matching: mask, handler: { [weak self] e in self?.updateClickThrough(); return e }) {
            mouseMonitors.append(l)
        }
        updateClickThrough()
    }

    /// Deja pasar los clics por las zonas transparentes del fotograma. Barato: un rectángulo y, solo
    /// dentro de él, una consulta a la máscara alfa.
    private func updateClickThrough() {
        if petView.dragging || dragging { if petWindow.ignoresMouseEvents { petWindow.ignoresMouseEvents = false }; return }
        let p = NSEvent.mouseLocation
        let f = petWindow.frame
        let over = f.contains(p)
        let solid = over && petView.isSolid(at: NSPoint(x: p.x - f.minX, y: p.y - f.minY))
        if petWindow.ignoresMouseEvents == solid { petWindow.ignoresMouseEvents = !solid }
        if over != hovering { hovering = over; updateSprite(force: true) }
    }

    // MARK: Lista

    func cardClicked(_ id: String) {
        if id.hasPrefix("s:") { openConversation(String(id.dropFirst(2))); return }
        guard id.hasPrefix("r:") else { return }
        openPanel(String(id.dropFirst(2)), focus: true)
    }

    /// Lleva a la conversación: su hilo en Codex, su sesión en la app de Claude o la terminal donde corre.
    func openConversation(_ sid: String) {
        guard let s = sessions.first(where: { $0.id == sid }) else { return }
        switch openTarget(s) {
        case .none: return
        case .url(let u):
            if let url = URL(string: u) { NSWorkspace.shared.open(url) }
        case .app(let bundle):
            // Solo si está abierta: si no, la conversación ya no corre ahí y no se lanza ninguna app.
            NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first?.activate(options: [.activateAllWindows])
        }
    }

    func openHint(_ s: SessionInfo) -> String {
        switch openTarget(s) {
        case .none: return "Sin preguntas pendientes"
        case .url: return s.provider == "codex" ? "Pulsa para abrirla en Codex" : "Pulsa para abrirla en Claude"
        case .app: return "Pulsa para ir a su terminal"
        }
    }

    /// Quitar (× de la tarjeta o «Quitar» del panel): única vía que escribe dismissed.json.
    /// También oculta la conversación hasta que vuelva a preguntar. En Claude, una pregunta que aún espera
    /// respuesta se devuelve antes a Claude (release.json), para no dejarla bloqueada sin verse.
    func dismissCard(_ id: String) {
        if id.hasPrefix("s:") {
            let sid = String(id.dropFirst(2))
            if finishedSessions.removeValue(forKey: sid) != nil { store.saveFinishedSessions(finishedSessions) }
            hideSession(sid); return
        }
        guard id.hasPrefix("r:") else { return }
        let key = String(id.dropFirst(2))
        guard let first = records[key], first.userCanQuit else { return }
        if !first.isDismissible {
            do { try store.writeRelease(first) }
            catch StoreError.posix(let code, _) where code == EEXIST {}
            catch { NSLog("Pancho: release.json no escrito: \(error.localizedDescription)"); return }
            records[key]?.hasRelease = true
        }
        guard let r = records[key], r.isDismissible else { return }
        store.writeDismissed(r)
        if panel.rec?.key == r.key { panel.close() }
        hideSession(r.state.provider + "|" + r.state.sessionId)
        poll()
    }

    /// Botón de actualizar: vuelve a leer el almacén y recupera las conversaciones en ejecución de Claude y
    /// de Codex, también las que se habían quitado con la ×. No toca solicitudes ni dismissed.json.
    func refreshConversations() {
        barView.refresh.dimmed = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in self?.barView.refresh.dimmed = false }
        poll()
        unhideActiveSessions()
        render(force: true)
    }

    /// En ejecución: la regla de `agentConversationWorking` (Claude interrumpida = 15 min sin avisos; tope de 12 h).
    func sessionRunning(_ s: SessionInfo, now: Double) -> Bool { agentConversationWorking(s, now: now) }

    /// Las que se están ejecutando dejan de estar ocultas; las paradas o cerradas siguen quitadas.
    func unhideActiveSessions(now: Double = Date().timeIntervalSince1970) {
        let active = Set(sessions.filter { sessionRunning($0, now: now) }.map { $0.id })
        let kept = hiddenSessions.filter { !active.contains($0.key) }
        guard kept.count != hiddenSessions.count else { return }
        hiddenSessions = kept
        store.saveHiddenSessions(hiddenSessions)
    }

    func hideSession(_ sid: String) {
        hiddenSessions[sid] = Date().timeIntervalSince1970
        store.saveHiddenSessions(hiddenSessions)
        render()
    }

    /// Oculta mientras no llegue una pregunta nueva de esa conversación después de quitarla.
    func sessionHidden(_ sid: String) -> Bool {
        guard let at = hiddenSessions[sid] else { return false }
        return !records.values.contains { $0.state.provider + "|" + $0.state.sessionId == sid && $0.state.createdAt > at }
    }

    func listHover(_ inside: Bool) {
        unfreezeTimer?.invalidate()
        if inside { hoverList = true; return }
        // La reordenación pendiente se aplica 700 ms después de que el puntero salga.
        unfreezeTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            self.hoverList = false
            if self.pendingReorder { self.render() }
        }
    }

    // MARK: Panel

    func openOldest(waveIfNone: Bool) {
        if let k = nextActionable() ?? pendingRecords.first?.key { openPanel(k, focus: true) }
        else if waveIfNone { triggerOneShot("waving") }
    }

    func openPanel(_ key: String, focus: Bool) {
        guard let r = records[key] else { return }
        closingAfterReceive = false
        let d = drafts[key] ?? store.loadDraft(r) ?? Draft()
        drafts[key] = d
        panel.open(r, draft: d, focus: focus)
        handlePanelPhase()
        render()
    }

    /// Ocultar (Esc, × del panel): nunca responde, nunca descarta y nunca encadena.
    func hidePanel() {
        panel.close()
        render()
    }

    // QuestionPanelDelegate

    func panelHide() { hidePanel() }

    func panelDismiss() {
        guard let key = panel.rec?.key else { return }
        // Contestada en Claude o Codex mientras el panel estaba abierto: se cierra solo.
        if case .terminal(let st) = panel.phase, st == "resolved_elsewhere" { hidePanel(); return }
        dismissCard("r:" + key)
    }

    func panelSend(_ answers: [String: String]) -> String? {
        guard let key = panel.rec?.key, let r = records[key] else { return "La pregunta ya no está disponible." }
        if r.state.isCodex { return sendCodex(r, answers: answers) }
        // Comprobación en el momento: si Claude ya no espera, no se escribe nada.
        if let pid = r.state.waiterPid, !processAlive(pid) {
            records[key]?.waiterAlive = false
            if let fresh = records[key] { panel.update(fresh) }
            return "Claude dejó de esperar; no se entregó."
        }
        guard r.canSend else { return "Esta pregunta ya no admite respuesta desde Pancho." }
        do {
            try store.writeResponse(r, answers: answers)
            records[key]?.hasResponse = true
            records[key]?.sentAnswers = answers
            DispatchQueue.main.async { [weak self] in self?.render() }
            return nil
        } catch StoreError.posix(let code, _) where code == EEXIST {
            return "Ya había una respuesta enviada para esta pregunta; no se envía otra."
        } catch {
            return "No enviada. Tu respuesta se conserva. \(error.localizedDescription)"
        }
    }

    /// Codex: response.json (creación única) y después UNA invocación de `codex-send`.
    /// El argv se valida antes de escribir: si el envío no está instalado no se escribe nada.
    private func sendCodex(_ r: RequestRecord, answers: [String: String]) -> String? {
        let key = r.key
        guard r.canSend else { return "Esta pregunta ya no admite respuesta desde Pancho." }
        let argv: [String]
        switch codexSender.argv(for: key) {
        case .success(let a): argv = a
        case .failure(let e): return e.message
        }
        do {
            try store.writeResponse(r, answers: answers)
        } catch StoreError.posix(let code, _) where code == EEXIST {
            return "Ya había una respuesta enviada para esta pregunta; no se envía otra."
        } catch {
            return "No enviada. Tu respuesta se conserva. \(error.localizedDescription)"
        }
        records[key]?.hasResponse = true
        records[key]?.sentAnswers = answers
        launchCodexSend(key, argv: argv)
        DispatchQueue.main.async { [weak self] in self?.render() }
        return nil
    }

    private func launchCodexSend(_ key: String, argv: [String]) {
        codexNotes[key] = nil
        let started = codexSender.start(key: key, argv: argv) { [weak self] outcome in
            guard let self = self else { return }
            self.codexNotes[key] = outcome.note.map { ($0, nil) }
            self.poll()
        }
        if started {
            records[key]?.sendInFlight = true
            records[key]?.sendNote = nil
        }
    }

    /// «Reintentar» de Codex con response.json conservada: solo si state.json dice `error` y `can_retry`.
    /// Nunca se llama de forma automática.
    func panelRetry() -> String? {
        guard let key = panel.rec?.key, let r = records[key] else { return "La pregunta ya no está disponible." }
        guard r.canRetrySend else { return "Esta respuesta no se puede reintentar desde Pancho." }
        switch codexSender.argv(for: key) {
        case .success(let argv): launchCodexSend(key, argv: argv)
        case .failure(let e): return e.message
        }
        if let fresh = records[key] { panel.update(fresh) }
        DispatchQueue.main.async { [weak self] in self?.render() }
        return nil
    }

    func panelRelease() -> String? {
        guard let key = panel.rec?.key, let r = records[key] else { return "La pregunta ya no está disponible." }
        do {
            try store.writeRelease(r)
            records[key]?.hasRelease = true
            DispatchQueue.main.async { [weak self] in self?.render() }
            return nil
        } catch StoreError.posix(let code, _) where code == EEXIST {
            records[key]?.hasRelease = true
            return nil
        } catch {
            return "No se pudo devolver la pregunta a Claude. \(error.localizedDescription)"
        }
    }

    func panelDraftChanged(_ draft: Draft) {
        guard let key = panel.rec?.key, let r = records[key], r.canSend else { return }
        drafts[key] = draft
        do { try store.saveDraft(draft, for: r) } catch {
            panel.localError = "No se pudo guardar el borrador: \(error.localizedDescription)"
        }
        DispatchQueue.main.async { [weak self] in self?.render() }
    }

    func panelNeedsLayout() {
        if panel.isOpen { layout() }
    }
}

/// Segunda línea de la tarjeta de una solicitud: estado en lenguaje cotidiano, indicador y prioridad.
struct CardStatus: Equatable {
    var label = ""
    var indicator = Indicator.none
    var tone = Tone.gray
    var attention = false
    var chevron = false
    var rank = 0
}

/// `blockedByPrevious`: respuesta Codex en cola porque otra solicitud del mismo chat está en `sending`,
/// `sent` o envío incierto.
func cardStatus(for r: RequestRecord, blockedByPrevious: Bool = false) -> CardStatus {
    let st = r.state
    var c = CardStatus()
    switch st.status {
    case "pending":
        if r.hasRelease { c.label = "Devolviendo a Claude…"; c.indicator = .spinner }
        else if r.codexUnsent { c.label = "Sin enviar"; c.indicator = .bang; c.tone = .red; c.chevron = true; c.rank = 1 }
        else if r.hasResponse { c.label = "Enviando…"; c.indicator = .spinner }
        else if r.isStale { c.label = "Claude dejó de esperar"; c.indicator = .bang; c.rank = 3 }
        else if st.isCodex {
            c.label = "Esperando tu respuesta"
            c.indicator = .breathing; c.tone = .blue; c.attention = true; c.chevron = true
        } else {
            let n = st.questions.count
            c.label = n > 1 ? "Tiene \(n) preguntas" : "Tiene una pregunta"
            c.indicator = .breathing; c.tone = .blue; c.attention = true; c.chevron = true
        }
    case "queued": c.label = blockedByPrevious ? "En cola: esperando la respuesta anterior" : "En cola"; c.indicator = .spinner
    case "sending": c.label = "Enviando…"; c.indicator = .spinner
    case "sent": c.label = "Enviada, esperando confirmación"; c.indicator = .spinner
    case "error":
        if r.hasRelease { c.label = "Devolviendo a Claude…"; c.indicator = .spinner }
        else if r.isStale { c.label = "Claude dejó de esperar"; c.indicator = .bang; c.rank = 3 }
        else if st.isCodex && !st.canRetry { c.label = "Revisa en Codex"; c.indicator = .bang; c.tone = .red; c.rank = 1 }
        else if r.sendInFlight { c.label = "Enviando…"; c.indicator = .spinner }
        else {
            c.label = "No se pudo enviar"; c.indicator = .bang; c.tone = .red
            c.chevron = r.canSend || r.canRetrySend; c.rank = 1
        }
    case "unconfirmed": c.label = "Sin confirmar"; c.indicator = .bang; c.tone = .red; c.rank = 1
    case "released": c.label = st.isCodex ? "Contesta en Codex" : "Devuelta a Claude"; c.rank = 3
    case "expired": c.label = "Caducada"; c.rank = 3
    case "resolved_elsewhere": c.label = "Resuelta en \(st.isCodex ? "Codex" : "Claude")"; c.indicator = .check; c.tone = .green; c.rank = 3
    default: c.label = st.status
    }
    return c
}

/// Lo que determina el pintado de la barra, la lista y el contador.
struct RenderSignature: Equatable {
    let models: [String: CardModel]
    let order: [String]
    let count: Int
    let collapsed: Bool
    let muted: Bool
    let simulation: Bool
    let reduceMotion: Bool
    let openKey: String?
}
