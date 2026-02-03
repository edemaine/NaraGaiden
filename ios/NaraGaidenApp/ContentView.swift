import Combine
import SwiftUI
import WidgetKit

struct ContentView: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var status = "Idle"
    @State private var lastUpdated: Date?
    @State private var isFetching = false
    @State private var payload: NaraPayload?
    private let refreshTimer = Timer.publish(every: 60, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Nara Gaiden")
                .font(.title2)
                .fontWeight(.semibold)

            Text("Server: \(NaraConfig.serverURLString)")
                .font(.footnote)
                .foregroundColor(.secondary)

            if let lastUpdated {
                Text("Last fetch: \(lastUpdated.formatted(date: .numeric, time: .standard))")
                    .font(.footnote)
                    .foregroundColor(.secondary)
            }

            Text("Status: \(status)")
                .font(.footnote)
                .foregroundColor(.secondary)

            HStack(spacing: 12) {
                Button("Refresh") {
                    Task {
                        await fetchAndReload()
                    }
                }
            }

            if let payload {
                NaraAppPreview(payload: payload)
            } else {
                Text("No data")
                    .font(.footnote)
                    .foregroundColor(.secondary)
            }
        }
        .padding()
        .task {
            await fetchAndReload()
        }
        .onChange(of: scenePhase) {
            guard scenePhase == .active else {
                return
            }
            Task {
                await fetchAndReload()
            }
        }
        .onReceive(refreshTimer) { _ in
            guard scenePhase == .active else {
                return
            }
            Task {
                await fetchAndReload()
            }
        }
    }

    private func fetchAndReload() async {
        guard !isFetching else {
            return
        }
        isFetching = true
        status = "Loading"
        WidgetCenter.shared.reloadTimelines(ofKind: "NaraGaidenLockWidget")
        do {
            let payload = try await NaraAPI.fetch()
            status = "Loaded \(payload.children.count) children"
            lastUpdated = Date()
            self.payload = payload
            WidgetCenter.shared.reloadTimelines(ofKind: "NaraGaidenLockWidget")
        } catch {
            status = "Error: \(error.localizedDescription)"
        }
        isFetching = false
    }
}

#Preview {
    ContentView()
}
