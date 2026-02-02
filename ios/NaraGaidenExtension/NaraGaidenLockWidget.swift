import SwiftUI
import WidgetKit

struct NaraEntry: TimelineEntry {
    let date: Date
    let payload: NaraPayload?
    let status: String
    let isStale: Bool
}

struct NaraProvider: TimelineProvider {
    func placeholder(in context: Context) -> NaraEntry {
        NaraEntry(date: Date(), payload: NaraPayload.preview(), status: "Nara Gaiden", isStale: false)
    }

    func getSnapshot(in context: Context, completion: @escaping (NaraEntry) -> Void) {
        Task {
            if context.isPreview {
                completion(NaraEntry(date: Date(), payload: NaraPayload.preview(), status: "Nara Gaiden", isStale: false))
                return
            }
            completion(await loadEntry())
        }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<NaraEntry>) -> Void) {
        Task {
            let entry = await loadEntry()
            let nextRefresh = Calendar.current.date(byAdding: .minute, value: 15, to: Date()) ?? Date().addingTimeInterval(900)
            let timeline = Timeline(entries: [entry], policy: .after(nextRefresh))
            completion(timeline)
        }
    }

    private func loadEntry() async -> NaraEntry {
        do {
            let payload = try await NaraAPI.fetch()
            NaraCache.save(payload)
            return NaraEntry(date: Date(), payload: payload, status: "Nara Gaiden", isStale: false)
        } catch {
            let message = shortStatus("Error: \(error.localizedDescription)")
            if let cached = NaraCache.load() {
                return NaraEntry(date: Date(), payload: cached, status: message, isStale: true)
            }
            return NaraEntry(date: Date(), payload: nil, status: message, isStale: true)
        }
    }

    private func shortStatus(_ text: String) -> String {
        let limit = 80
        if text.count <= limit {
            return text
        }
        let idx = text.index(text.startIndex, offsetBy: limit - 1)
        return String(text[..<idx]) + "…"
    }
}

enum NaraCache {
    private static let key = "nara_cached_payload"

    static func save(_ payload: NaraPayload) {
        let encoder = JSONEncoder()
        if let data = try? encoder.encode(payload) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }

    static func load() -> NaraPayload? {
        guard let data = UserDefaults.standard.data(forKey: key) else {
            return nil
        }
        let decoder = JSONDecoder()
        return try? decoder.decode(NaraPayload.self, from: data)
    }
}

struct NaraGaidenLockWidgetEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: NaraProvider.Entry

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let payload = entry.payload, !payload.children.isEmpty {
                tableView(payload: payload)
                Spacer(minLength: 0)
                footerView(payload: payload)
            } else {
                Text("No data")
                    .font(primaryFont)
                footerEmptyView()
            }
        }
        .padding(.horizontal, horizontalPadding)
        .padding(.vertical, verticalPadding)
    }

    private func tableView(payload: NaraPayload) -> some View {
        GeometryReader { geo in
            let total = geo.size.width
            let babyWidth = total * babyColumnRatio
            let feedWidth = total * feedColumnRatio
            let diaperWidth = total * diaperColumnRatio

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 4) {
                    Text("Baby")
                        .frame(width: babyWidth, alignment: .leading)
                    Text("Latest Feed")
                        .frame(width: feedWidth, alignment: .leading)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                    Text("Latest Diaper")
                        .frame(width: diaperWidth, alignment: .leading)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }
                .font(headerFont)
                .fontWeight(.semibold)

                ForEach(payload.children, id: \.id) { child in
                    HStack(alignment: .top, spacing: 4) {
                        Text(child.displayName)
                            .font(nameFont)
                            .fontWeight(.semibold)
                            .frame(width: babyWidth, alignment: .leading)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)

                        VStack(alignment: .leading, spacing: 2) {
                            Text(child.feed.label)
                                .lineLimit(1)
                                .minimumScaleFactor(0.7)
                            timeBadge(text: child.feed.relativeString(), beginDt: child.feed.beginDt)
                        }
                        .frame(width: feedWidth, alignment: .leading)

                        VStack(alignment: .leading, spacing: 2) {
                            Text(child.diaper.label)
                                .lineLimit(1)
                                .minimumScaleFactor(0.7)
                            timeBadge(text: child.diaper.relativeString(), beginDt: child.diaper.beginDt)
                        }
                        .frame(width: diaperWidth, alignment: .leading)
                    }
                    .font(rowFont)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func footerView(payload: NaraPayload) -> some View {
        let asOf = formattedAsOf(payload: payload, isStale: entry.isStale)
        return HStack(spacing: 4) {
            Text(asOf)
                .font(footerFont)
                .foregroundColor(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer()
            Text(entry.status)
                .font(footerFont)
                .foregroundColor(entry.status.hasPrefix("Error") ? .red : .secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
    }

    private func footerEmptyView() -> some View {
        HStack(spacing: 4) {
            Text("as of --")
                .font(footerFont)
                .foregroundColor(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer()
            Text(entry.status)
                .font(footerFont)
                .foregroundColor(entry.status.hasPrefix("Error") ? .red : .secondary)
                .lineLimit(1)
        }
    }

    private func timeBadge(text: String, beginDt: Int64?) -> some View {
        let colors = timeColors(beginDt: beginDt)
        return Text(text)
            .font(badgeFont)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(colors.bg)
            .foregroundColor(colors.fg)
            .cornerRadius(4)
            .lineLimit(1)
            .minimumScaleFactor(0.7)
    }

    private var horizontalPadding: CGFloat {
        family == .systemMedium ? 2 : 8
    }

    private var verticalPadding: CGFloat {
        family == .systemMedium ? -6 : 4
    }

    private var babyColumnRatio: CGFloat {
        family == .systemMedium ? 0.16 : 0.2
    }

    private var feedColumnRatio: CGFloat {
        family == .systemMedium ? 0.44 : 0.4
    }

    private var diaperColumnRatio: CGFloat {
        family == .systemMedium ? 0.4 : 0.4
    }

    private var headerFont: Font {
        family == .systemMedium ? .footnote : .caption2
    }

    private var rowFont: Font {
        family == .systemMedium ? .callout : .caption2
    }

    private var nameFont: Font {
        family == .systemMedium ? .caption : .caption2
    }

    private var badgeFont: Font {
        family == .systemMedium ? .body : .caption
    }

    private var footerFont: Font {
        family == .systemMedium ? .caption : .caption2
    }

    private var primaryFont: Font {
        family == .systemMedium ? .headline : .caption
    }

    private func timeColors(beginDt: Int64?) -> (bg: Color, fg: Color) {
        guard let beginDt else {
            return (Color(white: 0.2), Color(white: 0.95))
        }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        let deltaHours = max(0.0, Double(nowMs - beginDt) / 3600000.0)

        let stops: [(Double, (Double, Double, Double))] = [
            (1.0, (27, 94, 32)),
            (2.0, (133, 100, 18)),
            (3.0, (121, 69, 0)),
            (4.0, (122, 28, 28)),
        ]

        let rgb: (Double, Double, Double)
        if deltaHours <= 1.0 {
            rgb = stops[0].1
        } else if deltaHours >= 4.0 {
            rgb = stops.last!.1
        } else {
            var color = stops.last!.1
            for i in 0..<(stops.count - 1) {
                let (h0, c0) = stops[i]
                let (h1, c1) = stops[i + 1]
                if deltaHours <= h1 {
                    let t = (deltaHours - h0) / (h1 - h0)
                    color = (
                        c0.0 + (c1.0 - c0.0) * t,
                        c0.1 + (c1.1 - c0.1) * t,
                        c0.2 + (c1.2 - c0.2) * t
                    )
                    break
                }
            }
            rgb = color
        }

        return (
            Color(red: rgb.0 / 255.0, green: rgb.1 / 255.0, blue: rgb.2 / 255.0),
            Color.white
        )
    }

    private func formattedAsOf(payload: NaraPayload, isStale: Bool) -> String {
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        let base = "as of \(formatter.string(from: payload.generatedDate))"
        if !isStale {
            return base
        }
        let minutes = Int(max(0, Date().timeIntervalSince(payload.generatedDate) / 60))
        if minutes == 0 {
            return base
        }
        let suffix = minutes == 1 ? "1 min old" : "\(minutes) mins old"
        return "\(base) (\(suffix))"
    }
}

@main
struct NaraGaidenLockWidget: Widget {
    let kind = "NaraGaidenLockWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: NaraProvider()) { entry in
            NaraGaidenLockWidgetEntryView(entry: entry)
        }
        .configurationDisplayName("Nara Gaiden")
        .description("Latest feed and diaper times.")
        .supportedFamilies([.accessoryRectangular, .systemMedium])
    }
}

#Preview(as: .accessoryRectangular) {
    NaraGaidenLockWidget()
} timeline: {
    NaraEntry(date: Date(), payload: NaraPayload.preview(), status: "Nara Gaiden", isStale: false)
}
