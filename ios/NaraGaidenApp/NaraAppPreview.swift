import SwiftUI

struct NaraAppPreview: View {
    let payload: NaraPayload

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Text("Baby")
                    .frame(width: nameWidth, alignment: .leading)
                Text("Latest Feed")
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text("Latest Diaper")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.headline)
            .foregroundColor(.primary)

            Divider()

            ForEach(payload.children) { child in
                HStack(alignment: .top, spacing: 12) {
                    Text(child.displayName)
                        .font(.headline)
                        .frame(width: nameWidth, alignment: .leading)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)

                    VStack(alignment: .leading, spacing: 6) {
                        Text(child.feed.label)
                            .font(.subheadline)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                        timeBadge(text: child.feed.relativeString(), beginDt: child.feed.beginDt)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)

                    VStack(alignment: .leading, spacing: 6) {
                        Text(child.diaper.label)
                            .font(.subheadline)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                        timeBadge(text: child.diaper.relativeString(), beginDt: child.diaper.beginDt)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            Divider()

            HStack {
                Text(asOfText)
                    .font(.footnote)
                    .foregroundColor(.secondary)
                Spacer()
            }
        }
        .padding(12)
        .background(Color(.secondarySystemBackground))
        .cornerRadius(12)
    }

    private var nameWidth: CGFloat {
        90
    }

    private var asOfText: String {
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        let base = "as of \(formatter.string(from: payload.generatedDate))"
        let minutes = Int(max(0, Date().timeIntervalSince(payload.generatedDate) / 60))
        if minutes == 0 {
            return base
        }
        let suffix = minutes == 1 ? "1 min old" : "\(minutes) mins old"
        return "\(base) (\(suffix))"
    }

    private func timeBadge(text: String, beginDt: Int64?) -> some View {
        let colors = NaraStyle.timeColors(beginDt: beginDt)
        return Text(text)
            .font(.callout)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(colors.bg)
            .foregroundColor(colors.fg)
            .cornerRadius(6)
            .lineLimit(3)
            .multilineTextAlignment(.leading)
    }
}

#Preview {
    NaraAppPreview(payload: NaraPayload.preview())
        .padding()
}
