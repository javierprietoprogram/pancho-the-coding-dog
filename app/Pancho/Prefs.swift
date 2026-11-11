import AppKit

enum PetSize: String, CaseIterable {
    case small, normal, large
    var scale: CGFloat {
        switch self { case .small: return 0.75; case .normal: return 1; case .large: return 1.3 }
    }
    var title: String {
        switch self { case .small: return "Pequeño"; case .normal: return "Normal"; case .large: return "Grande" }
    }
}

/// Preferencias de la app (UserDefaults). Las animaciones y el plegado siguen en `pancho.muted` y `pancho.collapsed`.
enum Prefs {
    private static var d: UserDefaults { .standard }
    static var size: PetSize {
        get { PetSize(rawValue: d.string(forKey: "pancho.size") ?? "") ?? .normal }
        set { d.set(newValue.rawValue, forKey: "pancho.size") }
    }
    static var autoOpen: Bool {
        get { d.bool(forKey: "pancho.autoOpen") }
        set { d.set(newValue, forKey: "pancho.autoOpen") }
    }
    static var welcomed: Bool {
        get { d.bool(forKey: "pancho.welcomed") }
        set { d.set(newValue, forKey: "pancho.welcomed") }
    }
}
