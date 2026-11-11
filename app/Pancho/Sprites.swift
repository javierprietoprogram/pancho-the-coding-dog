import AppKit

/// Máscara alfa de un fotograma (fila 0 = arriba) para dejar pasar clics por lo transparente.
struct AlphaMask {
    let width: Int, height: Int
    let bytes: [UInt8]
    init(_ image: CGImage) {
        let w = image.width, h = image.height
        var rgba = [UInt8](repeating: 0, count: w * h * 4)
        rgba.withUnsafeMutableBytes { raw in
            if let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8,
                                   bytesPerRow: w * 4, space: CGColorSpaceCreateDeviceRGB(),
                                   bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
                ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            }
        }
        var buf = [UInt8](repeating: 0, count: w * h)
        for i in 0..<(w * h) { buf[i] = rgba[i * 4 + 3] }
        width = w; height = h; bytes = buf
    }
    /// Alfa máximo en un entorno de 1 px alrededor de (col, fila).
    func alpha(col: Int, row: Int) -> UInt8 {
        var best: UInt8 = 0
        for r in (row - 1)...(row + 1) where r >= 0 && r < height {
            for c in (col - 1)...(col + 1) where c >= 0 && c < width { best = max(best, bytes[r * width + c]) }
        }
        return best
    }
}

struct Animation {
    let name: String
    let frames: [CGImage]
    let masks: [AlphaMask]
    let durations: [Double]   // segundos
    let once: Bool
    let pause: (Double, Double)?
}

/// Lee pancho.json y los fotogramas copiados en Contents/Resources/pancho.
final class SpriteLibrary {
    var animations: [String: Animation] = [:]

    init(directory: URL) {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent("pancho.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let states = obj["estados"] as? [String: [String: Any]] else {
            NSLog("Pancho: no se pudo leer pancho.json en \(directory.path)")
            return
        }
        for (name, spec) in states {
            let count = (spec["fotogramas"] as? Int) ?? 0
            let durs = ((spec["duraciones"] as? [NSNumber]) ?? []).map { $0.doubleValue / 1000 }
            var frames: [CGImage] = []
            for i in 0..<count {
                let url = directory.appendingPathComponent(name).appendingPathComponent(String(format: "%02d.png", i))
                guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
                      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { continue }
                frames.append(img)
            }
            guard !frames.isEmpty else { continue }
            var pause: (Double, Double)? = nil
            if let p = spec["pausa"] as? [NSNumber], p.count == 2 { pause = (p[0].doubleValue / 1000, p[1].doubleValue / 1000) }
            let durations = frames.indices.map { $0 < durs.count ? durs[$0] : 0.14 }
            animations[name] = Animation(name: name, frames: frames, masks: frames.map(AlphaMask.init), durations: durations,
                                         once: (spec["repeticion"] as? String) == "una vez", pause: pause)
        }
    }
}

/// Reproductor: bucles con pausa aleatoria sobre el fotograma 00, animaciones puntuales y modo quieto.
final class Animator {
    let library: SpriteLibrary
    var onFrame: ((CGImage, AlphaMask) -> Void)?
    var onOneShotEnd: (() -> Void)?
    private(set) var current: String?
    private(set) var index = 0
    private var timer: Timer?
    private var still = false

    init(library: SpriteLibrary) { self.library = library }

    /// Cambia de animación. `still`: fotograma 00 fijo (silencio o Reducir movimiento).
    func play(_ name: String, still: Bool, force: Bool = false) {
        if name == current && still == self.still && !force { return }
        timer?.invalidate(); timer = nil
        guard let anim = library.animations[name] ?? library.animations["idle"] else { return }
        current = name; index = 0; self.still = still
        onFrame?(anim.frames[0], anim.masks[0])
        if still { return }
        let firstHold = anim.pause.map { Double.random(in: $0.0...$0.1) * 0.35 } ?? 0
        schedule(anim.durations[0] + firstHold)
    }

    private func schedule(_ delay: Double) {
        let t = Timer(timeInterval: delay, repeats: false) { [weak self] _ in self?.tick() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func tick() {
        guard let name = current, let anim = library.animations[name] else { return }
        index += 1
        var wait: Double
        if index >= anim.frames.count {
            if anim.once { current = nil; onOneShotEnd?(); return }
            index = 0
            wait = anim.durations[0] + (anim.pause.map { Double.random(in: $0.0...$0.1) } ?? 0)
        } else {
            wait = anim.durations[index]
        }
        onFrame?(anim.frames[index], anim.masks[index])
        schedule(wait)
    }
}
