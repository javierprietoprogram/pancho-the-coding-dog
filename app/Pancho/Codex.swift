import Foundation

// Envío de respuestas a Codex (CONTRACT.md, «Codex (T2)» → «Invocación de `codex-send`»).
// La app escribe response.json (creación única) y después invoca UNA vez
//   <send_argv del manifiesto> <key>
// donde send_argv = [<python>, "-I", <raíz>/bridge/pancho_hook.py, "codex-send", "--home", <raíz>, "--key"]
// sale de <raíz>/install/codex-manifest.json. Sin shell, con stdin cerrado y sin reintentos automáticos.
// La verdad es siempre state.json: el código de salida solo añade un aviso cuando state.json no lo explica.

/// Plazo máximo de espera de `codex-send` (contrato: peor caso real ≈ 50 s; lo normal, < 2 s).
let codexSendTimeout: TimeInterval = 60

enum CodexSendSetupError: Error, Equatable {
    case missingManifest
    case unreadableManifest
    case inactive
    case invalidArgv(String)
    case homeMismatch
    case invalidKey

    /// Texto para el panel. Nada se ha escrito ni enviado.
    var message: String {
        switch self {
        case .missingManifest:
            return "El envío a Codex no está instalado en Pancho (falta codex-manifest.json). No se envió nada; contesta en Codex."
        case .unreadableManifest:
            return "No se pudo leer la configuración de envío a Codex. No se envió nada; contesta en Codex."
        case .inactive:
            return "El envío a Codex está desinstalado. No se envió nada; contesta en Codex."
        case .invalidArgv(let why):
            return "La configuración de envío a Codex no es válida (\(why)). No se envió nada; contesta en Codex."
        case .homeMismatch:
            return "La configuración de envío a Codex apunta a otro almacén. No se envió nada; contesta en Codex."
        case .invalidKey:
            return "Identificador de pregunta no válido. No se envió nada."
        }
    }
}

func codexManifestURL(home: URL) -> URL {
    home.appendingPathComponent("install", isDirectory: true).appendingPathComponent("codex-manifest.json")
}

private func samePath(_ a: String, _ b: URL) -> Bool {
    URL(fileURLWithPath: a).standardizedFileURL.path == b.standardizedFileURL.path
}

#if PANCHO_DEBUG || PANCHO_HARNESS
/// Solo en la variante de depuración (activo) o en el arnés (inactivo salvo que lo active): acepta en
/// send_argv el socket IPC de pruebas. En la instalable este código no existe.
#if PANCHO_DEBUG
var codexAcceptsTestSocket = true
#else
var codexAcceptsTestSocket = false
#endif
#endif

/// Construye el argv exacto de `codex-send` a partir del manifiesto ya leído (función pura).
/// Exige el manifiesto activo y EXACTAMENTE
///   [<python.path del manifiesto>, "-I", "<raíz>/bridge/pancho_hook.py", "codex-send", "--home", "<raíz>", "--key"]
/// con <raíz> igual a la de la app (y a `root` del manifiesto). Añade la clave (64 hexadecimales).
/// La instalable no contiene la opción del socket de pruebas (ver `codexAcceptsTestSocket`).
func codexSendArgv(manifest: Any?, home: URL, key: String) -> Result<[String], CodexSendSetupError> {
    guard key.count == 64, key.allSatisfy({ ("0"..."9").contains($0) || ("a"..."f").contains($0) }) else {
        return .failure(.invalidKey)
    }
    guard let m = manifest as? [String: Any] else { return .failure(.unreadableManifest) }
    guard (m["active"] as? Bool) == true else { return .failure(.inactive) }
    guard let python = (m["python"] as? [String: Any])?["path"] as? String, python.hasPrefix("/") else {
        return .failure(.invalidArgv("falta el intérprete del manifiesto"))
    }
    guard let raw = m["send_argv"] as? [Any] else { return .failure(.invalidArgv("falta send_argv")) }
    let argv = raw.compactMap { $0 as? String }
    guard argv.count == raw.count else { return .failure(.invalidArgv("send_argv con valores que no son texto")) }
    var core = argv
    #if PANCHO_DEBUG || PANCHO_HARNESS
    if codexAcceptsTestSocket, core.count == 9, core[6] == "--ipc-socket", core[7].hasPrefix("/") { core.removeSubrange(6...7) }
    #endif
    guard core.count == 7 else { return .failure(.invalidArgv("send_argv no tiene la forma esperada")) }
    let root = core[5]
    let expected = [python, "-I", root + "/bridge/pancho_hook.py", "codex-send", "--home", root, "--key"]
    guard core == expected else { return .failure(.invalidArgv("send_argv no tiene la forma esperada")) }
    guard samePath(root, home), (m["root"] as? String).map({ samePath($0, home) }) ?? true else {
        return .failure(.homeMismatch)
    }
    return .success(argv + [key])
}

/// Lee <raíz>/install/codex-manifest.json y construye el argv.
func codexSendArgv(home: URL, key: String) -> Result<[String], CodexSendSetupError> {
    let url = codexManifestURL(home: home)
    guard FileManager.default.fileExists(atPath: url.path) else { return .failure(.missingManifest) }
    guard let data = try? Data(contentsOf: url), let obj = try? JSONSerialization.jsonObject(with: data) else {
        return .failure(.unreadableManifest)
    }
    return codexSendArgv(manifest: obj, home: home, key: key)
}

/// Resultado de una invocación de `codex-send`.
struct CodexSendOutcome: Equatable {
    var exitCode: Int32?
    /// Campo `result` de la línea JSON de stdout, si se pudo leer.
    var result: String?
    var timedOut = false
    var launchError: String?

    /// Códigos de salida del contrato.
    static let resultForExit: [Int32: String] = [0: "sent", 10: "queued", 11: "already", 20: "rejected", 21: "blocked",
                                                 22: "uncertain", 23: "not_applicable", 1: "internal_error"]

    /// Aviso para el panel solo cuando state.json no lo explica por sí mismo. Los códigos se derivan del
    /// estado FINAL (0 = este proceso envió; el estado puede ser sent, accepted, continued o unconfirmed):
    /// con 0, 10, 11, 20, 21 y 22 manda state.json y no hay aviso local. Los textos no dicen «no se reenvía»
    /// ni «contesta en Codex»: pueden aparecer junto a «Reintentar».
    var note: String? {
        if let e = launchError { return "No se pudo iniciar el envío a Codex (\(e)). No se envió nada." }
        if timedOut { return "El envío a Codex tarda más de lo previsto. Pancho espera a que termine; mientras tanto no se puede reintentar." }
        guard let code = exitCode else { return nil }
        switch code {
        case 0, 10, 11, 20, 21, 22: return nil
        case 23: return "Codex ya no admite esta respuesta desde Pancho."
        case 1: return "Error interno del puente al enviar a Codex. Tu respuesta se conserva."
        case 2: return "No se pudo invocar el envío a Codex (argumentos no válidos). No se envió nada."
        default: return "El envío a Codex terminó de forma inesperada (código \(code))."
        }
    }
}

/// Ejecuta argv y devuelve el resultado. Llama a `onExit` una vez, cuando el proceso termina de verdad
/// (o en seguida si no llegó a lanzarse), aunque ya haya devuelto `timedOut`.
typealias CodexRunner = (_ argv: [String], _ timeout: TimeInterval, _ onExit: @escaping () -> Void) -> CodexSendOutcome

/// Procesos que siguen vivos tras el plazo: se conservan hasta que terminen (nunca se matan).
private let lingering = NSLock()
private var lingeringProcesses: [ObjectIdentifier: Process] = [:]

/// stdout leído en otro hilo; se publica bajo cierre.
private final class OutputBox {
    private let lock = NSLock()
    private var data: Data?
    func set(_ d: Data) { lock.lock(); data = d; lock.unlock() }
    func get() -> Data? { lock.lock(); defer { lock.unlock() }; return data }
}

/// Ejecuta `argv` sin shell, con stdin cerrado (/dev/null) y cwd "/". Bloquea hasta que termina o
/// vence `timeout`; en ese caso deja de esperar (no lo mata) y devuelve `timedOut`.
func runCodexSendProcess(_ argv: [String], timeout: TimeInterval, onExit: @escaping () -> Void) -> CodexSendOutcome {
    guard let exe = argv.first else { onExit(); return CodexSendOutcome(launchError: "orden vacía") }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = Array(argv.dropFirst())
    p.currentDirectoryURL = URL(fileURLWithPath: "/", isDirectory: true)
    p.standardInput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    let out = Pipe()
    p.standardOutput = out
    let done = DispatchSemaphore(value: 0)
    p.terminationHandler = { proc in
        lingering.lock(); lingeringProcesses[ObjectIdentifier(proc)] = nil; lingering.unlock()
        onExit()
        done.signal()
    }
    lingering.lock(); lingeringProcesses[ObjectIdentifier(p)] = p; lingering.unlock()
    do {
        try p.run()
    } catch {
        lingering.lock(); lingeringProcesses[ObjectIdentifier(p)] = nil; lingering.unlock()
        try? out.fileHandleForReading.close()
        onExit()
        return CodexSendOutcome(launchError: (error as NSError).localizedDescription)
    }
    // stdout se lee en paralelo para que una salida grande nunca bloquee al hijo.
    let box = OutputBox()
    let readDone = DispatchSemaphore(value: 0)
    let reader = out.fileHandleForReading
    DispatchQueue.global(qos: .utility).async {
        box.set(reader.readDataToEndOfFile())
        readDone.signal()
    }
    if done.wait(timeout: .now() + timeout) == .timedOut {
        return CodexSendOutcome(timedOut: true)
    }
    // Si stdout no se cierra en 2 s (p. ej. un nieto lo heredó), se ignora: manda el código de salida.
    let data = readDone.wait(timeout: .now() + 2) == .success ? box.get() : nil
    let code = p.terminationStatus
    var result: String? = nil
    if let data = data, let text = String(data: data, encoding: .utf8),
       let line = text.split(whereSeparator: \.isNewline).last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }),
       let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] {
        result = obj["result"] as? String
    }
    if p.terminationReason == .uncaughtSignal {
        return CodexSendOutcome(exitCode: 128 + code, result: result)
    }
    return CodexSendOutcome(exitCode: code, result: result ?? CodexSendOutcome.resultForExit[code])
}

/// Lanza `codex-send` en segundo plano, como mucho una invocación en curso por clave.
/// La clave sigue «en curso» hasta que el proceso termina de verdad, también si vence el plazo de espera.
/// Solo se usa desde `completionQueue` (el hilo principal en la app).
final class CodexSender {
    let home: URL
    var runner: CodexRunner
    var timeout: TimeInterval
    var completionQueue: DispatchQueue = .main
    /// Se llama cuando termina un proceso cuyo plazo de espera ya había vencido.
    var onLateExit: ((String) -> Void)?
    private(set) var inFlight: Set<String> = []
    /// Plazo vencido y proceso aún vivo.
    private(set) var overdue: Set<String> = []
    private var exitedBeforeDelivery: Set<String> = []
    private let queue = DispatchQueue(label: "pancho.codex-send", qos: .userInitiated, attributes: .concurrent)

    init(home: URL, runner: @escaping CodexRunner = runCodexSendProcess, timeout: TimeInterval = codexSendTimeout) {
        self.home = home; self.runner = runner; self.timeout = timeout
    }

    /// Valida el manifiesto y construye el argv sin ejecutar nada.
    func argv(for key: String) -> Result<[String], CodexSendSetupError> { codexSendArgv(home: home, key: key) }

    /// Invoca una vez. Devuelve false (y no hace nada) si ya hay una invocación en curso para esa clave.
    @discardableResult
    func start(key: String, argv: [String], completion: @escaping (CodexSendOutcome) -> Void) -> Bool {
        guard !inFlight.contains(key) else { return false }
        inFlight.insert(key)
        exitedBeforeDelivery.remove(key)
        let runner = self.runner, timeout = self.timeout, done = completionQueue
        queue.async { [weak self] in
            let outcome = runner(argv, timeout) { done.async { self?.processExited(key) } }
            done.async { self?.deliver(key, outcome, completion) }
        }
        return true
    }

    private func processExited(_ key: String) {
        if overdue.contains(key) {
            overdue.remove(key); inFlight.remove(key)
            onLateExit?(key)
        } else if inFlight.contains(key) {
            exitedBeforeDelivery.insert(key)
        }
    }

    private func deliver(_ key: String, _ outcome: CodexSendOutcome, _ completion: (CodexSendOutcome) -> Void) {
        if outcome.timedOut && !exitedBeforeDelivery.contains(key) {
            overdue.insert(key)              // sigue en curso hasta que el proceso termine
        } else {
            inFlight.remove(key)
        }
        exitedBeforeDelivery.remove(key)
        completion(outcome)
    }
}

/// Mensaje del puente junto a «Reintentar»: sin la coletilla «contesta en Codex» ni «no se reenvía».
func retryableMessage(_ message: String) -> String {
    var t = message
    for tail in [". Contesta en Codex", "; contesta en Codex", ", contesta en Codex", ". No se reenvía", "; no se reenvía"] where t.hasSuffix(tail) {
        t.removeLast(tail.count)
    }
    return t
}

/// Claves de respuestas Codex en cola porque otra solicitud del mismo chat está en `sending`, `sent` o
/// envío incierto.
func codexBlockedKeys<S: Sequence>(_ records: S) -> Set<String> where S.Element == RequestRecord {
    let all = Array(records)
    var blockers: [String: Set<String>] = [:]
    for r in all where r.blocksCodexChat { blockers[r.state.sessionId, default: []].insert(r.key) }
    var out = Set<String>()
    for r in all where r.state.isCodex && r.state.status == "queued" {
        if !(blockers[r.state.sessionId] ?? []).subtracting([r.key]).isEmpty { out.insert(r.key) }
    }
    return out
}
