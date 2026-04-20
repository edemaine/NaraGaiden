import Foundation

struct NaraConfig {
    private static let defaultServerURLString = "http://192.168.2.1:8888"
    static let appGroupIdentifier = "group.org.erikdemaine.NaraGaiden"

    static var serverURLString: String {
        (Bundle.main.object(forInfoDictionaryKey: "NaraGaidenServerURL") as? String)?.nilIfEmpty
            ?? defaultServerURLString
    }

    static var serverURL: URL {
        URL(string: serverURLString) ?? URL(string: defaultServerURLString)!
    }

    static var jsonURL: URL {
        serverURL.appendingPathComponent("json")
    }

    static var plotURL: URL {
        serverURL.appendingPathComponent("plot")
    }
}

enum NaraSharedStore {
    static var defaults: UserDefaults {
        UserDefaults(suiteName: NaraConfig.appGroupIdentifier) ?? .standard
    }
}

enum NaraPasswordStore {
    private static let key = "server_password"

    static func load() -> String? {
        NaraSharedStore.defaults.string(forKey: key)?.nilIfEmpty
    }

    static func save(_ password: String) {
        NaraSharedStore.defaults.set(password, forKey: key)
    }

    static func clear() {
        NaraSharedStore.defaults.removeObject(forKey: key)
    }
}

enum NaraAPIError: LocalizedError, Equatable {
    case passwordRequired
    case passwordRejected
    case badStatus(Int)

    var errorDescription: String? {
        switch self {
        case .passwordRequired:
            return "Password required"
        case .passwordRejected:
            return "Password rejected"
        case let .badStatus(statusCode):
            return "HTTP \(statusCode)"
        }
    }
}

private extension String {
    var nilIfEmpty: String? {
        isEmpty ? nil : self
    }
}

enum NaraAlerts {
    static let poopThresholdMs: Int64 = 2 * 24 * 60 * 60 * 1000
    static let warningEmoji = "⚠️"

    static func formatPoopAlertDays(lastPoopDiaperBeginDt: Int64?, now: Date = Date()) -> String? {
        guard let lastPoopDiaperBeginDt else {
            return nil
        }
        let nowMs = Int64(now.timeIntervalSince1970 * 1000)
        let deltaMs = max(0, nowMs - lastPoopDiaperBeginDt)
        guard deltaMs >= poopThresholdMs else {
            return nil
        }
        let roundedTenths = Int((Double(deltaMs) / 86_400_000.0 * 10.0).rounded())
        let wholeDays = roundedTenths / 10
        let tenths = roundedTenths % 10
        let value: String
        if tenths == 0 {
            value = String(wholeDays)
        } else {
            value = String(format: "%.1f", Double(roundedTenths) / 10.0)
        }
        let unit = roundedTenths == 10 ? "day" : "days"
        return "\(value) \(unit)"
    }
}

struct NaraPayload: Codable {
    let generatedAt: Int64
    let children: [NaraChild]

    var generatedDate: Date {
        Date(timeIntervalSince1970: TimeInterval(generatedAt) / 1000.0)
    }

    var poopAlerts: [String] {
        children.compactMap { $0.poopAlertText() }
    }

    static func preview() -> NaraPayload {
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        return NaraPayload(
            generatedAt: nowMs,
            children: [
                NaraChild(
                    id: "child-1",
                    name: "Ava",
                    feed: NaraEvent(
                        label: "Bottle (120 mL)",
                        beginDt: nowMs - 20 * 60 * 1000
                    ),
                    diaper: NaraEvent(
                        label: "Wet",
                        beginDt: nowMs - 55 * 60 * 1000
                    ),
                    lastPoopDiaperBeginDt: nowMs - 60 * 60 * 60 * 1000,
                    vitaminsToday: 2,
                    medicationToday: 1,
                    bathsToday: 1
                )
            ]
        )
    }
}

struct NaraChild: Codable, Identifiable {
    let id: String
    let name: String
    let feed: NaraEvent
    let diaper: NaraEvent
    let lastPoopDiaperBeginDt: Int64?
    let vitaminsToday: Int?
    let medicationToday: Int?
    let bathsToday: Int?

    var displayIndicators: String {
        let vitaminCount = max(vitaminsToday ?? 0, 0)
        let medicationCount = max(medicationToday ?? 0, 0)
        let bathCount = max(bathsToday ?? 0, 0)
        let alertIndicator = poopAlertText() == nil ? "" : NaraAlerts.warningEmoji
        return alertIndicator
            + String(repeating: "💊", count: max(vitaminCount, 0))
            + String(repeating: "💉", count: max(medicationCount, 0))
            + String(repeating: "🛁", count: max(bathCount, 0))
    }

    var displayName: String {
        if displayIndicators.isEmpty {
            return name
        }
        return "\(name) \(displayIndicators)"
    }

    func poopAlertText(now: Date = Date()) -> String? {
        guard let daysText = NaraAlerts.formatPoopAlertDays(lastPoopDiaperBeginDt: lastPoopDiaperBeginDt, now: now) else {
            return nil
        }
        return "\(NaraAlerts.warningEmoji) \(name) hasn't pooped for \(daysText)."
    }
}

struct NaraEvent: Codable {
    let label: String
    let beginDt: Int64?

    func relativeString(now: Date = Date()) -> String {
        guard let beginDt else {
            return "unknown"
        }
        let nowMs = Int64(now.timeIntervalSince1970 * 1000)
        let deltaSec = max(0, (nowMs - beginDt) / 1000)
        let mins = deltaSec / 60
        let hours = mins / 60
        let days = hours / 24

        var parts: [String] = []
        if days > 0 {
            parts.append("\(days) day" + (days == 1 ? "" : "s"))
        }
        if hours % 24 > 0 {
            parts.append("\(hours % 24) hour" + (hours % 24 == 1 ? "" : "s"))
        }
        if mins % 60 > 0 && days == 0 {
            let minsPart = mins % 60
            let suffix = minsPart == 1 ? "" : "s"
            parts.append("\(minsPart) min\(suffix)")
        }
        if parts.isEmpty {
            return "just now"
        }
        return parts.joined(separator: " ") + " ago"
    }
}

enum NaraAPI {
    static func fetch() async throws -> NaraPayload {
        let password = NaraPasswordStore.load()
        var request = URLRequest(url: NaraConfig.jsonURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        if let password {
            request.setValue(password, forHTTPHeaderField: "X-NaraGaiden-Password")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse {
            if http.statusCode == 401 {
                NaraPasswordStore.clear()
                throw password == nil ? NaraAPIError.passwordRequired : NaraAPIError.passwordRejected
            }
            if http.statusCode != 200 {
                throw NaraAPIError.badStatus(http.statusCode)
            }
        }
        return try JSONDecoder().decode(NaraPayload.self, from: data)
    }
}
