import Foundation
import Darwin

// Contrato almacén ⇄ Pancho.app v1 (.strict-workflow/runs/pancho-integracion/CONTRACT.md).
// La app solo escribe: v1/app/heartbeat.json y, por solicitud, draft.json, response.json,
// release.json y dismissed.json. Todo lo demás lo escribe el puente y aquí solo se lee.

// Revisión 1 del contrato: `unconfirmed` es informativo (no cuenta como pendiente) y se puede descartar.
let pendingStatuses: Set<String> = ["pending", "queued", "sending", "sent", "error"]
let terminalStatuses: Set<String> = ["accepted", "continued", "released", "expired", "resolved_elsewhere"]
/// Estados que se pueden quitar de la lista con dismissed.json (siempre de forma explícita).
let dismissibleStatuses: Set<String> = ["released", "expired", "resolved_elsewhere", "unconfirmed"]

/// ¿Sigue vivo el proceso? kill(pid, 0) == 0, o EPERM (existe pero es de otro usuario).
func processAlive(_ pid: Int32) -> Bool {
    guard pid > 0 else { return false }
    if kill(pid, 0) == 0 { return true }
    return errno == EPERM
}

struct QOption { let label: String; let description: String }

struct QItem {
    let id: String
    let header: String?
    let question: String
    let multiSelect: Bool
    let options: [QOption]
}

struct RequestState {
    let raw: [String: Any]
    let key: String
    let provider: String
    let sessionId: String
    let callId: String
    let generation: Any?           // se devuelve tal cual en response.json
    let generationText: String
    let cwd: String?
    let project: String?
    let createdAt: Double
    let expiresAt: Double?
    let questions: [QItem]
    let status: String
    let message: String?
    let answers: [String: String]?
    let canRetry: Bool
    let updatedAt: Double
    /// pid del hook que espera (Claude), o nil si no consta.
    let waiterPid: Int32?
    /// Codex: el transporte IPC llegó a iniciarse (distingue el envío incierto de la pregunta no publicada).
    let transportStarted: Bool

    var projectName: String {
        if let p = project, !p.isEmpty { return p }
        if let c = cwd, !c.isEmpty { return (c as NSString).lastPathComponent }
        return "Sin proyecto"
    }
    var shortSession: String { String(sessionId.prefix(8)) }
    /// Codex: sin waiter_pid, sin expires_at, sin release.json y sin selección múltiple.
    var isCodex: Bool { provider == "codex" }

    init?(json: [String: Any]) {
        guard let key = json["key"] as? String, let provider = json["provider"] as? String,
              let sid = json["session_id"] as? String, let qs = json["questions"] as? [[String: Any]],
              let status = json["status"] as? String else { return nil }
        raw = json
        self.key = key; self.provider = provider; sessionId = sid
        callId = (json["call_id"] as? String) ?? ""
        generation = json["generation"].flatMap { $0 is NSNull ? nil : $0 }
        generationText = generation.map { "\($0)" } ?? ""
        cwd = json["cwd"] as? String
        project = json["project"] as? String
        createdAt = (json["created_at"] as? NSNumber)?.doubleValue ?? 0
        expiresAt = (json["expires_at"] as? NSNumber)?.doubleValue
        self.status = status
        message = (json["message"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        answers = json["answers"] as? [String: String]
        canRetry = (json["can_retry"] as? Bool) ?? false
        updatedAt = (json["updated_at"] as? NSNumber)?.doubleValue ?? createdAt
        waiterPid = (json["waiter_pid"] as? NSNumber).map { Int32(truncatingIfNeeded: $0.intValue) }
        transportStarted = (json["transport_started"] as? Bool) ?? false
        var items: [QItem] = []
        // Codex async no tiene selección múltiple: una sola opción por pregunta, pase lo que pase.
        let single = provider == "codex"
        for q in qs {
            guard let text = q["question"] as? String else { return nil }
            let opts = ((q["options"] as? [[String: Any]]) ?? []).map {
                QOption(label: ($0["label"] as? String) ?? "", description: ($0["description"] as? String) ?? "")
            }
            items.append(QItem(id: (q["id"] as? String) ?? text, header: (q["header"] as? String).flatMap { $0.isEmpty ? nil : $0 },
                               question: text, multiSelect: !single && ((q["multiSelect"] as? Bool) ?? false), options: opts))
        }
        questions = items
    }
}

struct SessionInfo: Equatable {
    let provider: String
    let sessionId: String
    let cwd: String?
    let project: String?
    let firstSeen: Double
    let lastEventAt: Double
    let activity: String

    var id: String { provider + "|" + sessionId }
    var projectName: String {
        if let p = project, !p.isEmpty { return p }
        if let c = cwd, !c.isEmpty { return (c as NSString).lastPathComponent }
        return "Sin proyecto"
    }
}

struct Draft: Equatable {
    var selections: [String: [Int]] = [:]
    var other: [String: String] = [:]

    var hasContent: Bool {
        selections.values.contains { !$0.isEmpty } || other.values.contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
}

/// Lo que la app sabe de una solicitud en disco.
struct RequestRecord {
    var state: RequestState
    var dir: URL
    var hasResponse: Bool
    var hasRelease: Bool
    /// Existe dismissed.json (escrito solo por una acción explícita del usuario).
    var dismissedFile: Bool
    var sentAnswers: [String: String]?
    /// Resultado de kill(waiter_pid, 0) en la última lectura; true si no hay waiter_pid (siempre en Codex).
    var waiterAlive: Bool = true
    /// Codex: hay una invocación de `codex-send` en curso (la pone el Controller, no el disco). Sigue en curso
    /// hasta que el proceso termina de verdad, aunque haya vencido el plazo de espera.
    var sendInFlight: Bool = false
    /// Codex: respuesta en cola porque otra solicitud del mismo chat está en `sending`, `sent` o envío
    /// incierto (lo pone el Controller).
    var blockedByPrevious: Bool = false
    /// Codex: aviso de la última invocación cuando state.json no lo explica (lo pone el Controller).
    var sendNote: String?

    var key: String { state.key }
    var status: String { state.status }
    var clockExpired: Bool {
        if !state.isCodex, let e = state.expiresAt { return Date().timeIntervalSince1970 > e }
        return false
    }
    /// Contrato (rev. 1): «Enviar» solo si `pending` (o `error` con can_retry y sin response.json)
    /// y, si hay waiter_pid, el proceso sigue vivo.
    /// Codex: igual, sin waiter ni plazo; con `error` solo si no queda response.json (respuesta rechazada).
    var canSend: Bool {
        guard !dismissed, !hasRelease, !hasResponse, waiterAlive, !sendInFlight else { return false }
        switch state.status {
        case "pending": return !clockExpired
        case "error": return state.canRetry
        default: return false
        }
    }
    /// Codex: «Reintentar» vuelve a invocar `codex-send` con la response.json existente. Dos casos:
    /// - `error` con `can_retry` (fallo antes de enviar);
    /// - `pending` con response.json y sin invocación en curso (la app se cerró antes de invocar o el
    ///   intérprete no arrancó). Es seguro: en `pending` el transporte no se ha iniciado y el puente decide
    ///   bajo bloqueo. Nunca se invoca solo, tampoco al volver a abrir la app.
    var canRetrySend: Bool {
        guard state.isCodex, !dismissed, hasResponse, !sendInFlight else { return false }
        return (state.status == "error" && state.canRetry) || state.status == "pending"
    }
    /// Codex: respuesta guardada que no consta como enviada (`pending` + response.json, sin invocación en curso).
    var codexUnsent: Bool { state.isCodex && !dismissed && hasResponse && !sendInFlight && state.status == "pending" }
    /// Codex: `error` sin `can_retry` que NO bloquea el chat (la pregunta no se publicó; el transporte no
    /// llegó a iniciarse). Decisión del coordinador (r1): «Quitar» y fuera del contador.
    var isCodexInfoError: Bool { state.isCodex && state.status == "error" && !state.canRetry && !state.transportStarted }
    /// Codex: envío incierto (`error` sin `can_retry` tras iniciar el transporte). Bloquea el chat hasta que
    /// pase a `unconfirmed` o llegue la señal nativa: cuenta en el contador y no se puede quitar.
    var isCodexBlockingError: Bool { state.isCodex && state.status == "error" && !state.canRetry && state.transportStarted }
    /// Codex: esta solicitud impide enviar otras respuestas del mismo chat (`sending`, `sent` o envío incierto).
    var blocksCodexChat: Bool { state.isCodex && (state.status == "sending" || state.status == "sent" || isCodexBlockingError) }
    /// La pregunta seguía abierta pero quien esperaba ya no está: no se puede entregar nada.
    /// Solo Claude: en Codex no hay espera («Claude dejó de esperar» no se aplica).
    var isStale: Bool {
        guard !state.isCodex, !hasRelease, !hasResponse else { return false }
        switch state.status {
        case "pending": return !waiterAlive || clockExpired
        case "error": return state.canRetry && !waiterAlive
        default: return false
        }
    }
    /// Estado pendiente según el contrato (sin descartar). Incluye las que se quedaron sin espera.
    var isPending: Bool { !dismissed && pendingStatuses.contains(state.status) }
    /// El estado actual admite «Quitar». No depende de dismissed.json.
    var stateAllowsDismiss: Bool {
        dismissibleStatuses.contains(state.status) || (pendingStatuses.contains(state.status) && isStale) || isCodexInfoError
            || codexPendingQuittable || claudeReturning
    }
    /// Codex: pregunta sin enviar desde Pancho. Nada espera en Pancho (la pregunta sigue en Codex), así que
    /// la × la quita de la lista sin tocar el chat. Con un envío en curso o en cola no se puede quitar.
    var codexPendingQuittable: Bool { state.isCodex && state.status == "pending" && !sendInFlight }
    /// Claude: el usuario la devolvió a Claude (release.json); mientras el puente la pasa a `released`.
    var claudeReturning: Bool { !state.isCodex && hasRelease && !hasResponse && pendingStatuses.contains(state.status) }
    /// La × de la tarjeta se ofrece: se puede quitar ya o, en Claude, devolverla primero a Claude.
    var userCanQuit: Bool { isDismissible || (!state.isCodex && canSend) }
    /// dismissed.json solo se respeta mientras el estado siga siendo descartable: si la solicitud vuelve a
    /// `sent` o pasa a bloquear el chat, vuelve a verse.
    var dismissed: Bool { dismissedFile && stateAllowsDismiss }
    /// Cuenta para el contador, el lápiz y Pancho «esperando».
    var countsAsPending: Bool { isPending && !isStale && !isCodexInfoError }
    var isTerminal: Bool { terminalStatuses.contains(state.status) }
    var isDismissible: Bool { stateAllowsDismiss && !dismissedFile }
    /// Necesita una decisión del usuario: se puede responder ahora mismo.
    var needsAction: Bool { canSend }

    /// Datos que cambian lo que muestra el panel o la tarjeta. Si no cambian, no se repinta.
    var signature: RecordSignature {
        RecordSignature(key: key, generation: state.generationText, status: state.status, message: state.message,
                        canRetry: state.canRetry, hasResponse: hasResponse, hasRelease: hasRelease, dismissed: dismissed,
                        waiterAlive: waiterAlive, clockExpired: clockExpired, expiresAt: state.expiresAt,
                        answers: state.answers, sentAnswers: sentAnswers, sendInFlight: sendInFlight, sendNote: sendNote,
                        blockedByPrevious: blockedByPrevious)
    }
}

struct RecordSignature: Equatable {
    let key: String
    let generation: String
    let status: String
    let message: String?
    let canRetry: Bool
    let hasResponse: Bool
    let hasRelease: Bool
    let dismissed: Bool
    let waiterAlive: Bool
    let clockExpired: Bool
    let expiresAt: Double?
    let answers: [String: String]?
    let sentAnswers: [String: String]?
    let sendInFlight: Bool
    let sendNote: String?
    let blockedByPrevious: Bool
}

enum StoreError: LocalizedError {
    case posix(Int32, String)
    var errorDescription: String? {
        switch self { case let .posix(code, what): return "\(what): \(String(cString: strerror(code)))" }
    }
}

final class Store {
    let home: URL
    let v1: URL
    private var stateCache: [String: (mtime: Date, state: RequestState)] = [:]
    private var sentCache: [String: [String: String]] = [:]
    /// Solicitudes ya descartadas: mientras state.json no cambie no se vuelven a mirar.
    private var dismissedCache: [String: (mtime: Date, record: RequestRecord)] = [:]
    private var sessionCache: [String: (mtime: Date, info: SessionInfo?)] = [:]

    init() {
        let env = ProcessInfo.processInfo.environment["PANCHO_HOME"].flatMap { $0.isEmpty ? nil : $0 }
        if let env = env {
            home = URL(fileURLWithPath: (env as NSString).expandingTildeInPath, isDirectory: true)
        } else {
            home = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Pancho", isDirectory: true)
        }
        v1 = home.appendingPathComponent("v1", isDirectory: true)
    }

    var appDir: URL { v1.appendingPathComponent("app", isDirectory: true) }
    var requestsDir: URL { v1.appendingPathComponent("requests", isDirectory: true) }
    var sessionsDir: URL { v1.appendingPathComponent("sessions", isDirectory: true) }

    // MARK: Escritura atómica

    private func ensureDir(_ url: URL) throws {
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue { return }
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }

    private func writeTemp(_ data: Data, near url: URL) throws -> URL {
        let tmp = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        let fd = Darwin.open(tmp.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        guard fd >= 0 else { throw StoreError.posix(errno, "No se pudo crear \(tmp.lastPathComponent)") }
        var ok = true
        data.withUnsafeBytes { buf in
            var off = 0
            while off < buf.count {
                guard let base = buf.baseAddress else { ok = false; break }
                let n = Darwin.write(fd, base + off, buf.count - off)
                if n <= 0 { ok = false; break }
                off += n
            }
        }
        let werr = errno
        fsync(fd)
        Darwin.close(fd)
        if !ok { unlink(tmp.path); throw StoreError.posix(werr, "No se pudo escribir \(url.lastPathComponent)") }
        return tmp
    }

    /// Temporal + rename (reemplaza).
    func atomicWrite(_ object: Any, to url: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
        let tmp = try writeTemp(data, near: url)
        if Darwin.rename(tmp.path, url.path) != 0 {
            let e = errno; unlink(tmp.path); throw StoreError.posix(e, "No se pudo guardar \(url.lastPathComponent)")
        }
    }

    /// Temporal + link(): creación única, falla si ya existe.
    func createOnce(_ object: Any, at url: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
        let tmp = try writeTemp(data, near: url)
        let rc = Darwin.link(tmp.path, url.path)
        let e = errno
        unlink(tmp.path)
        if rc != 0 { throw StoreError.posix(e, "No se pudo crear \(url.lastPathComponent)") }
    }

    // MARK: App

    func writeHeartbeat() {
        do {
            try ensureDir(v1); try ensureDir(appDir)
            try atomicWrite(["pid": Int(getpid()), "ts": Date().timeIntervalSince1970, "version": "1"],
                            to: appDir.appendingPathComponent("heartbeat.json"))
        } catch {
            NSLog("Pancho: heartbeat no escrito: \(error.localizedDescription)")
        }
    }

    /// Minutos que Claude espera la respuesta en Pancho (5, 15 o 30). El puente solo admite acortar.
    func readWaitMinutes() -> Int {
        guard let data = try? Data(contentsOf: appDir.appendingPathComponent("prefs.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let s = (obj["wait_seconds"] as? NSNumber)?.intValue, s >= 60, s < 1800 else { return 30 }
        return s / 60
    }

    func writeWaitMinutes(_ minutes: Int) throws {
        try ensureDir(v1); try ensureDir(appDir)
        try atomicWrite(["wait_seconds": min(max(minutes, 1), 30) * 60], to: appDir.appendingPathComponent("prefs.json"))
    }

    func readSimulation() -> Bool {
        guard let data = try? Data(contentsOf: appDir.appendingPathComponent("mode.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return (obj["simulation"] as? Bool) ?? false
    }

    // MARK: Lectura

    private func mtime(_ url: URL) -> Date? {
        var st = stat()
        guard stat(url.path, &st) == 0 else { return nil }
        return Date(timeIntervalSince1970: Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) / 1e9)
    }

    func scanRequests() -> [RequestRecord] {
        let fm = FileManager.default
        guard let dirs = try? fm.contentsOfDirectory(at: requestsDir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]) else {
            stateCache = [:]; sentCache = [:]; dismissedCache = [:]
            return []
        }
        var out: [RequestRecord] = []
        var seen = Set<String>()
        for dir in dirs {
            let name = dir.lastPathComponent
            let stateURL = dir.appendingPathComponent("state.json")
            guard let mt = mtime(stateURL) else { continue }
            seen.insert(name)
            if let d = dismissedCache[name], d.mtime == mt { out.append(d.record); continue }
            var state: RequestState
            if let cached = stateCache[name], cached.mtime == mt {
                state = cached.state
            } else if let data = try? Data(contentsOf: stateURL),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let parsed = RequestState(json: obj) {
                state = parsed
                stateCache[name] = (mt, parsed)
            } else if let cached = stateCache[name] {
                state = cached.state      // lectura a medias durante un rename: se usa la anterior
            } else { continue }
            let rec = record(state, dir: dir)
            if rec.dismissed { dismissedCache[name] = (mt, rec) } else { dismissedCache[name] = nil }
            out.append(rec)
        }
        // Podar lo que ya no existe en disco.
        stateCache = stateCache.filter { seen.contains($0.key) }
        sentCache = sentCache.filter { seen.contains($0.key) }
        dismissedCache = dismissedCache.filter { seen.contains($0.key) }
        return out
    }

    private func record(_ state: RequestState, dir: URL) -> RequestRecord {
        let fm = FileManager.default
        let respURL = dir.appendingPathComponent("response.json")
        let hasResponse = fm.fileExists(atPath: respURL.path)
        var sent: [String: String]? = nil
        if hasResponse {
            if let c = sentCache[dir.lastPathComponent] { sent = c }
            else if let data = try? Data(contentsOf: respURL),
                    let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                    let a = obj["answers"] as? [String: String] {
                sent = a; sentCache[dir.lastPathComponent] = a
            }
        } else {
            sentCache[dir.lastPathComponent] = nil
        }
        // Solo importa si el hook sigue vivo mientras la pregunta está abierta.
        let open = state.status == "pending" || state.status == "error" || state.status == "queued"
            || state.status == "sending" || state.status == "sent"
        let alive = open && !state.isCodex ? (state.waiterPid.map(processAlive) ?? true) : true
        // Respuestas conservadas: response.json; si no, las que guarda el puente (p. ej. «sin confirmar»).
        let kept = sent ?? ((hasResponse || state.status == "unconfirmed") ? state.answers : nil)
        return RequestRecord(state: state, dir: dir, hasResponse: hasResponse,
                             hasRelease: fm.fileExists(atPath: dir.appendingPathComponent("release.json").path),
                             dismissedFile: fm.fileExists(atPath: dir.appendingPathComponent("dismissed.json").path),
                             sentAnswers: kept, waiterAlive: alive)
    }

    /// Sesiones: solo se vuelve a leer un archivo si cambia su mtime.
    func scanSessions() -> [SessionInfo] {
        guard let files = try? FileManager.default.contentsOfDirectory(at: sessionsDir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]) else {
            sessionCache = [:]
            return []
        }
        var out: [SessionInfo] = []
        var seen = Set<String>()
        for f in files where f.pathExtension == "json" {
            let name = f.lastPathComponent
            guard let mt = mtime(f) else { continue }
            seen.insert(name)
            if let c = sessionCache[name], c.mtime == mt {
                if let info = c.info { out.append(info) }
                continue
            }
            var info: SessionInfo? = nil
            if let data = try? Data(contentsOf: f),
               let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let provider = o["provider"] as? String, let sid = o["session_id"] as? String {
                info = SessionInfo(provider: provider, sessionId: sid, cwd: o["cwd"] as? String, project: o["project"] as? String,
                                   firstSeen: (o["first_seen"] as? NSNumber)?.doubleValue ?? 0,
                                   lastEventAt: (o["last_event_at"] as? NSNumber)?.doubleValue ?? 0,
                                   activity: (o["activity"] as? String) ?? "observed")
            } else if let c = sessionCache[name] {
                info = c.info             // lectura a medias: se conserva la anterior sin fijar el mtime
                if let i = info { out.append(i) }
                continue
            }
            sessionCache[name] = (mt, info)
            if let i = info { out.append(i) }
        }
        sessionCache = sessionCache.filter { seen.contains($0.key) }
        return out
    }

    // MARK: Por solicitud

    func loadDraft(_ rec: RequestRecord) -> Draft? {
        guard let data = try? Data(contentsOf: rec.dir.appendingPathComponent("draft.json")),
              let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        var d = Draft()
        if let s = o["selections"] as? [String: [NSNumber]] { d.selections = s.mapValues { $0.map(\.intValue) } }
        if let t = o["other"] as? [String: String] { d.other = t }
        return d
    }

    func saveDraft(_ draft: Draft, for rec: RequestRecord) throws {
        try atomicWrite(["selections": draft.selections, "other": draft.other, "updated_at": Date().timeIntervalSince1970],
                        to: rec.dir.appendingPathComponent("draft.json"))
    }

    func writeResponse(_ rec: RequestRecord, answers: [String: String]) throws {
        let s = rec.state
        let obj: [String: Any] = ["key": s.key, "provider": s.provider, "session_id": s.sessionId, "call_id": s.callId,
                                  "generation": s.generation ?? NSNull(), "answers": answers,
                                  "created_at": Date().timeIntervalSince1970]
        try createOnce(obj, at: rec.dir.appendingPathComponent("response.json"))
        sentCache[rec.dir.lastPathComponent] = answers
    }

    func writeRelease(_ rec: RequestRecord) throws {
        try createOnce(["key": rec.key, "at": Date().timeIntervalSince1970], at: rec.dir.appendingPathComponent("release.json"))
    }

    /// Conversaciones quitadas con la × (clave «proveedor|sesión» → momento). Solo las escribe la app.
    /// Se vuelven a ver cuando esa conversación hace una pregunta nueva.
    var hiddenSessionsURL: URL { appDir.appendingPathComponent("hidden-sessions.json") }

    func loadHiddenSessions() -> [String: Double] {
        guard let data = try? Data(contentsOf: hiddenSessionsURL),
              let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return o.compactMapValues { ($0 as? NSNumber)?.doubleValue }
    }

    func saveHiddenSessions(_ hidden: [String: Double]) {
        do { try ensureDir(v1); try ensureDir(appDir); try atomicWrite(hidden, to: hiddenSessionsURL) }
        catch { NSLog("Pancho: hidden-sessions.json no escrito: \(error.localizedDescription)") }
    }

    /// Solo por una acción explícita del usuario (× de la tarjeta o «Quitar»). Esc u ocultar nunca lo llaman.
    func writeDismissed(_ rec: RequestRecord) {
        let url = rec.dir.appendingPathComponent("dismissed.json")
        if FileManager.default.fileExists(atPath: url.path) { return }
        do { try createOnce(["key": rec.key, "at": Date().timeIntervalSince1970], at: url) }
        catch { NSLog("Pancho: dismissed.json no escrito: \(error.localizedDescription)") }
    }
}

/// Composición de respuestas idéntica a src/Popup.swift (verificado):
/// texto libre exacto si no está vacío tras quitar espacios; si no, etiquetas elegidas unidas por ", ".
func composeAnswer(_ q: QItem, draft: Draft) -> String? {
    let free = draft.other[q.id] ?? ""
    let selected = (draft.selections[q.id] ?? []).sorted().filter { $0 >= 0 && $0 < q.options.count }.map { q.options[$0].label }
    let answer = free.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? selected.joined(separator: ", ") : free
    return answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : answer
}

/// Texto para «Copiar»: la respuesta de cada pregunta; con varias, precedida de su encabezado o enunciado.
func copyText(_ questions: [QItem], answers: [String: String]) -> String {
    let parts: [String] = questions.compactMap { q in
        guard let a = answers[q.id] else { return nil }
        return questions.count > 1 ? "\(q.header ?? q.question): \(a)" : a
    }
    return parts.joined(separator: "\n")
}
