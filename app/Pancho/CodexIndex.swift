import Foundation
import SQLite3

/// Subagentes de Codex leídos de su índice local (~/.codex/state_N.sqlite), en solo lectura y en segundo plano.
/// Así se ven aunque se lanzaran antes de conectar los avisos. Solo se piden metadatos (madre, hijo, estado, apodo,
/// rol, tarea, carpeta y horas): nunca el título, el primer mensaje ni la vista previa de la conversación.
final class CodexIndex {
    private let queue = DispatchQueue(label: "pancho.codex-index", qos: .utility)
    private var busy = false
    private var lastRun: Double = 0
    private(set) var rows: [CodexSpawnRow] = []
    /// Solo hace falta lo de la última media hora (lo terminado se ve 30 min).
    private let window: Double = 30 * 60

    /// Llamado en cada sondeo; consulta como mucho cada 3 s. `done` llega en el hilo principal si hubo lectura nueva.
    func refresh(now: Double, done: @escaping () -> Void) {
        guard !busy, now - lastRun >= 3 else { return }
        busy = true
        lastRun = now
        queue.async { [weak self] in
            guard let self = self else { return }
            let found = CodexIndex.read(since: now - self.window)
            DispatchQueue.main.async {
                self.busy = false
                if let f = found, f != self.rows { self.rows = f; done() }
            }
        }
    }

    private static func databasePath() -> String? {
        let env = ProcessInfo.processInfo.environment
        if let p = env["PANCHO_CODEX_STATE"], !p.isEmpty { return p }
        let base = env["CODEX_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex").path
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: base) else { return nil }
        // state_5.sqlite hoy; si Codex sube de versión, la de número más alto.
        let best = names.compactMap { n -> (Int, String)? in
            guard n.hasPrefix("state_"), n.hasSuffix(".sqlite"), let v = Int(n.dropFirst(6).dropLast(7)) else { return nil }
            return (v, n)
        }.max { $0.0 < $1.0 }
        return best.map { (base as NSString).appendingPathComponent($0.1) }
    }

    /// nil si no hay índice o no se pudo leer (se conserva lo anterior); [] si no hay subagentes recientes.
    private static func read(since: Double) -> [CodexSpawnRow]? {
        guard let path = databasePath(), FileManager.default.fileExists(atPath: path) else { return [] }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK, let db = db else {
            if let db = db { sqlite3_close(db) }
            return nil
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 250)
        let sql = """
            SELECT e.parent_thread_id, e.child_thread_id, e.status, t.agent_nickname, t.agent_role, t.agent_path, t.cwd,
                   t.created_at_ms, t.updated_at_ms
            FROM thread_spawn_edges e JOIN threads t ON t.id = e.child_thread_id
            WHERE t.updated_at_ms >= ? AND COALESCE(t.archived, 0) = 0
            ORDER BY t.updated_at_ms DESC LIMIT 200
            """
        var stmt: OpaquePointer?
        // Si Codex cambia su esquema, la consulta falla y simplemente no se ven sus subagentes.
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt = stmt else { return nil }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int64(stmt, 1, Int64(since * 1000))
        func text(_ i: Int32) -> String? { sqlite3_column_text(stmt, i).map { String(cString: $0) } }
        var out: [CodexSpawnRow] = []
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_DONE { break }
            guard rc == SQLITE_ROW else { return nil }
            out.append(CodexSpawnRow(parent: text(0) ?? "", child: text(1) ?? "", status: text(2) ?? "", nickname: text(3), role: text(4),
                                     path: text(5), cwd: text(6), createdMs: Double(sqlite3_column_int64(stmt, 7)),
                                     updatedMs: Double(sqlite3_column_int64(stmt, 8))))
        }
        return out
    }
}
