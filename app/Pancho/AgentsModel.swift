import Foundation

// Lógica pura del panel de agentes (sin AppKit): se comprueba aparte con scripts/test_swift.sh.
// Agentes = conversaciones de Claude Code y Codex (v1/sessions); subagentes = los que lanzan (v1/agents).

/// Una conversación vista por los hooks (agente principal).
struct SessionInfo: Equatable {
    let provider: String
    let sessionId: String
    let cwd: String?
    let project: String?
    let firstSeen: Double
    let lastEventAt: Double
    let activity: String
    /// Solo Claude: id de la sesión de la app de escritorio (local_…), punto de entrada y app de terminal.
    var hostSession: String? = nil
    var entrypoint: String? = nil
    var termProgram: String? = nil

    var id: String { provider + "|" + sessionId }
    var projectName: String {
        if let p = project, !p.isEmpty { return p }
        if let c = cwd, !c.isEmpty { return (c as NSString).lastPathComponent }
        return "Sin proyecto"
    }
}

/// Un subagente registrado por el puente. Solo lo acordado: nunca el encargo ni la respuesta.
struct SubagentInfo: Equatable {
    let provider: String
    let sessionId: String
    let agentId: String
    let agentType: String?
    let title: String?
    let cwd: String?
    let project: String?
    let startedAt: Double?
    let endedAt: Double?
    let running: Bool

    var id: String { provider + "|" + sessionId + "|" + agentId }
}

enum AgentsLimits {
    static let doneKeep: Double = 30 * 60      // terminados: media hora
    static let runningMax: Double = 12 * 3600  // en marcha sin fin tras 12 h: caído
}

/// Lee un JSON de v1/agents. Los pendientes (listas) y los registros sin identidad se ignoran.
func subagentInfo(from object: Any?) -> SubagentInfo? {
    guard let o = object as? [String: Any], let provider = o["provider"] as? String, provider == "claude" || provider == "codex",
          let session = o["session_id"] as? String, !session.isEmpty, let agent = o["agent_id"] as? String, !agent.isEmpty else { return nil }
    func s(_ k: String) -> String? { (o[k] as? String).flatMap { $0.isEmpty ? nil : $0 } }
    func d(_ k: String) -> Double? { (o[k] as? NSNumber)?.doubleValue }
    return SubagentInfo(provider: provider, sessionId: session, agentId: agent, agentType: s("agent_type"), title: s("title"),
                        cwd: s("cwd"), project: s("project"), startedAt: d("started_at"), endedAt: d("ended_at"),
                        running: (o["status"] as? String) != "done")
}

/// Un grupo del panel: una conversación (o un grupo propio para subagentes cuya conversación no se ve).
struct AgentGroup: Equatable {
    let id: String
    let provider: String
    let session: SessionInfo?
    let working: Bool
    let subagents: [SubagentInfo]

    var runningCount: Int { subagents.filter(\.running).count }
    var project: String {
        if let s = session { return s.projectName }
        let sub = subagents.first
        if let p = sub?.project, !p.isEmpty { return p }
        if let c = sub?.cwd, !c.isEmpty { return (c as NSString).lastPathComponent }
        return "Sin proyecto"
    }
    /// Última actividad, para ordenar.
    var lastAt: Double {
        let subs = subagents.map { max($0.startedAt ?? 0, $0.endedAt ?? 0) }.max() ?? 0
        return max(session?.lastEventAt ?? 0, subs)
    }
}

struct AgentsSummary: Equatable {
    var working = 0, runningSubagents = 0, doneRecent = 0, claude = 0, codex = 0
    /// Globo del botón: conversaciones trabajando + subagentes en marcha.
    var badge: Int { working + runningSubagents }
}

func agentConversationWorking(_ s: SessionInfo, now: Double) -> Bool {
    s.activity == "working" && now - s.lastEventAt < AgentsLimits.runningMax
}

/// ¿Se muestra este subagente? En marcha hasta 12 h; terminado durante media hora.
func subagentVisible(_ a: SubagentInfo, now: Double) -> Bool {
    if a.running { return now - (a.startedAt ?? 0) < AgentsLimits.runningMax }
    return now - (a.endedAt ?? 0) <= AgentsLimits.doneKeep
}

/// Árbol del panel: conversaciones trabajando o con subagentes visibles, cada una con sus subagentes.
/// Un subagente de Codex cuya conversación no se ve se cuelga de la conversación de Codex trabajando en la misma
/// carpeta (la más reciente); si no hay ninguna, forma su propio grupo. Orden: lo que está en marcha primero.
func agentsTree(sessions: [SessionInfo], agents: [SubagentInfo], now: Double) -> [AgentGroup] {
    let byId = Dictionary(sessions.map { ($0.id, $0) }, uniquingKeysWith: { a, b in a.lastEventAt >= b.lastEventAt ? a : b })
    var subs: [String: [SubagentInfo]] = [:]
    var orphanIds: [String: String] = [:]
    for a in agents where subagentVisible(a, now: now) {
        var key = a.provider + "|" + a.sessionId
        if byId[key] == nil, a.provider == "codex", let cwd = a.cwd,
           let parent = sessions.filter({ $0.provider == "codex" && $0.cwd == cwd && agentConversationWorking($0, now: now) })
            .max(by: { $0.lastEventAt < $1.lastEventAt }) {
            key = parent.id
        }
        if byId[key] == nil { orphanIds[key] = a.provider }
        subs[key, default: []].append(a)
    }
    var keys = Set(subs.keys)
    for s in sessions where agentConversationWorking(s, now: now) { keys.insert(s.id) }

    func order(_ list: [SubagentInfo]) -> [SubagentInfo] {
        list.sorted { a, b in
            if a.running != b.running { return a.running }
            let ta = a.running ? (a.startedAt ?? 0) : (a.endedAt ?? 0), tb = b.running ? (b.startedAt ?? 0) : (b.endedAt ?? 0)
            return ta != tb ? ta > tb : a.agentId < b.agentId
        }
    }
    let groups = keys.map { key -> AgentGroup in
        let s = byId[key]
        return AgentGroup(id: key, provider: s?.provider ?? orphanIds[key] ?? "claude", session: s,
                          working: s.map { agentConversationWorking($0, now: now) } ?? false, subagents: order(subs[key] ?? []))
    }
    return groups.sorted { a, b in
        let la = a.working || a.runningCount > 0, lb = b.working || b.runningCount > 0
        if la != lb { return la }
        return a.lastAt != b.lastAt ? a.lastAt > b.lastAt : a.id < b.id
    }
}

func agentsSummary(_ groups: [AgentGroup], now: Double) -> AgentsSummary {
    var s = AgentsSummary()
    for g in groups {
        let live = (g.working ? 1 : 0) + g.runningCount
        if g.working { s.working += 1 }
        s.runningSubagents += g.runningCount
        s.doneRecent += g.subagents.filter { !$0.running && now - ($0.endedAt ?? 0) <= AgentsLimits.doneKeep }.count
        if g.provider == "codex" { s.codex += live } else { s.claude += live }
    }
    return s
}

/// Lo que recibe la página del panel (JSON). Solo datos ya guardados por el puente.
func agentsPayload(_ groups: [AgentGroup], now: Double) -> [String: Any] {
    let sum = agentsSummary(groups, now: now)
    func opt(_ v: Any?) -> Any { v ?? NSNull() }
    let list: [[String: Any]] = groups.map { g in
        [
            "id": g.id, "provider": g.provider, "project": g.project, "working": g.working,
            "cwd": opt(g.session?.cwd ?? g.subagents.first?.cwd), "session": opt(g.session?.sessionId),
            "orphan": g.session == nil, "firstSeen": opt(g.session?.firstSeen), "lastEventAt": opt(g.session?.lastEventAt),
            "subagents": g.subagents.map { a -> [String: Any] in
                ["id": a.id, "type": opt(a.agentType), "title": opt(a.title), "project": opt(a.project), "cwd": opt(a.cwd),
                 "running": a.running, "startedAt": opt(a.startedAt), "endedAt": opt(a.endedAt), "provider": a.provider,
                 "session": a.sessionId]
            },
        ]
    }
    return ["now": now, "groups": list,
            "summary": ["working": sum.working, "running": sum.runningSubagents, "done": sum.doneRecent,
                        "claude": sum.claude, "codex": sum.codex, "badge": sum.badge]]
}

// MARK: Subagentes de Codex desde su índice local

/// Una fila del índice de Codex (~/.codex/state_5.sqlite: thread_spawn_edges + threads). Solo metadatos: nunca texto
/// de la conversación (ni título, ni primer mensaje, ni vista previa).
struct CodexSpawnRow: Equatable {
    var parent: String
    var child: String
    var status: String
    var nickname: String?
    var role: String?
    var path: String?
    var cwd: String?
    var createdMs: Double
    var updatedMs: Double
}

/// Sin actividad durante este tiempo, un subagente de Codex se da por terminado (Codex no marca el fin en el índice).
let codexQuietSeconds: Double = 180

func codexSubagent(_ r: CodexSpawnRow, now: Double) -> SubagentInfo? {
    guard !r.parent.isEmpty, !r.child.isEmpty else { return nil }
    func clean(_ s: String?) -> String? {
        guard let t = s?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else { return nil }
        return String(t.prefix(80))
    }
    // La ruta del agente (/root/h6_continuity_review) es el nombre de su tarea: sirve de título corto.
    let task = clean(r.path.map { ($0 as NSString).lastPathComponent.replacingOccurrences(of: "_", with: " ") })
    let updated = r.updatedMs / 1000
    let running = r.status == "open" && now - updated < codexQuietSeconds
    let cwd = clean(r.cwd)
    return SubagentInfo(provider: "codex", sessionId: r.parent, agentId: r.child, agentType: clean(r.nickname) ?? clean(r.role),
                        title: task, cwd: cwd, project: cwd.map { ($0 as NSString).lastPathComponent },
                        startedAt: r.createdMs > 0 ? r.createdMs / 1000 : nil, endedAt: running ? nil : updated, running: running)
}

/// Un mismo hilo de Codex puede llegar por aviso y por el índice: se queda uno, con el título del índice si el aviso no lo trae.
func mergeSubagents(hooks: [SubagentInfo], codex: [SubagentInfo]) -> [SubagentInfo] {
    let indexed = Dictionary(codex.map { ($0.agentId, $0) }, uniquingKeysWith: { a, _ in a })
    var out: [SubagentInfo] = []
    var seen = Set<String>()
    for h in hooks {
        if h.provider == "codex", let c = indexed[h.agentId] {
            seen.insert(h.agentId)
            // El aviso sabe con certeza cuándo terminó; el índice, el nombre y la tarea.
            out.append(SubagentInfo(provider: "codex", sessionId: c.sessionId, agentId: h.agentId, agentType: c.agentType ?? h.agentType,
                                    title: h.title ?? c.title, cwd: h.cwd ?? c.cwd, project: h.project ?? c.project,
                                    startedAt: h.startedAt ?? c.startedAt, endedAt: h.running ? c.endedAt : h.endedAt,
                                    running: h.running && c.running))
        } else {
            out.append(h)
        }
    }
    out += codex.filter { !seen.contains($0.agentId) }
    return out
}

// MARK: Conversaciones bajo Pancho: terminadas y abrir

/// Conversaciones terminadas (id → cuándo): las que Pancho vio «trabajando» y han dejado de estarlo. Siguen en la lista
/// en verde hasta quitarlas con la ×, volver a trabajar o pasar 12 h sin actividad.
func updateFinished(_ finished: [String: Double], previous: [String: String], sessions: [SessionInfo], now: Double) -> [String: Double] {
    var out: [String: Double] = [:]
    for s in sessions {
        if s.activity == "working" || now - s.lastEventAt >= AgentsLimits.runningMax { continue }
        if let at = finished[s.id] { out[s.id] = at }
        else if previous[s.id] == "working" { out[s.id] = now }
    }
    return out
}

enum OpenTarget: Equatable { case none, url(String), app(String) }

/// Apps de terminal por TERM_PROGRAM (lista cerrada: la misma que acepta el puente).
let terminalApps: [String: String] = [
    "Apple_Terminal": "com.apple.Terminal", "iTerm.app": "com.googlecode.iterm2", "WarpTerminal": "dev.warp.Warp-Stable",
    "ghostty": "com.mitchellh.ghostty", "vscode": "com.microsoft.VSCode", "WezTerm": "com.github.wez.wezterm",
    "Hyper": "co.zeit.hyper", "Tabby": "org.tabby", "kitty": "net.kovidgoyal.kitty", "Alacritty": "org.alacritty",
    "zed": "dev.zed.Zed",
]

/// A dónde lleva pulsar una conversación: su hilo en Codex, su sesión en la app de Claude o la terminal donde corre.
func openTarget(_ s: SessionInfo) -> OpenTarget {
    func safe(_ v: String, _ extra: String) -> Bool {
        !v.isEmpty && v.count <= 72 && v.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) && $0.isASCII || extra.unicodeScalars.contains($0) }
    }
    if s.provider == "codex" {
        return safe(s.sessionId, "-") ? .url("codex://threads/" + s.sessionId) : .none
    }
    if let host = s.hostSession, host.hasPrefix("local_"), host.count <= 70, safe(String(host.dropFirst(6)), "-") {
        return .url("claude://code/continue?session=" + host)
    }
    if let term = s.termProgram, let bundle = terminalApps[term] { return .app(bundle) }
    return .none
}
