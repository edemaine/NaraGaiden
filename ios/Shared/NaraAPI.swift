import Foundation

struct NaraConfig {
    static let serverURLString = "http://192.168.2.1:8888/json"

    static var serverURL: URL {
        URL(string: serverURLString) ?? URL(string: "http://192.168.2.1:8888/json")!
    }
}

struct NaraPayload: Codable {
    let generatedAt: Int64
    let children: [NaraChild]

    var generatedDate: Date {
        Date(timeIntervalSince1970: TimeInterval(generatedAt) / 1000.0)
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
                        label: "Bottle (120 ml)",
                        beginDt: nowMs - 20 * 60 * 1000
                    ),
                    diaper: NaraEvent(
                        label: "Wet",
                        beginDt: nowMs - 55 * 60 * 1000
                    ),
                    vitaminsToday: true
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
    let vitaminsToday: Bool?

    var displayName: String {
        if vitaminsToday == true {
            return "\(name) 💊"
        }
        return name
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
        var request = URLRequest(url: NaraConfig.serverURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(NaraPayload.self, from: data)
    }
}
