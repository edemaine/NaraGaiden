import Combine
import SwiftUI
import UIKit
import WidgetKit

struct ContentView: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var status = "Idle"
    @State private var lastUpdated: Date?
    @State private var isFetching = false
    @State private var payload: NaraPayload?
    @State private var showingPasswordPrompt = false
    @State private var passwordDraft = ""
    @State private var passwordPromptMessage = "Enter the server password for this device."
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

                Button("Plots") {
                    openPlots()
                }

                Button("Nara Baby") {
                    openNaraBaby()
                }
            }

            Button("Password") {
                presentPasswordPrompt(rejected: false)
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
        .sheet(isPresented: $showingPasswordPrompt) {
            NavigationStack {
                Form {
                    Section {
                        SecureField("Server password", text: $passwordDraft)
                            .textContentType(.password)
                    } footer: {
                        Text(passwordPromptMessage)
                    }
                }
                .navigationTitle("Server Password")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") {
                            showingPasswordPrompt = false
                        }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") {
                            let password = passwordDraft
                            NaraPasswordStore.save(password)
                            showingPasswordPrompt = false
                            Task {
                                await fetchAndReload()
                            }
                        }
                        .disabled(passwordDraft.isEmpty)
                    }
                }
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
        } catch let error as NaraAPIError {
            status = "Error: \(error.localizedDescription)"
            presentPasswordPrompt(rejected: error == .passwordRejected)
        } catch {
            status = "Error: \(error.localizedDescription)"
        }
        isFetching = false
    }

    private func presentPasswordPrompt(rejected: Bool) {
        passwordDraft = rejected ? "" : (NaraPasswordStore.load() ?? "")
        passwordPromptMessage = rejected
            ? "The saved password was not accepted. Enter the current server password."
            : "Enter the server password for this device."
        showingPasswordPrompt = true
    }

    private func openNaraBaby() {
        guard let appUrl = URL(string: "com.naraorganics.nara://") else {
            return
        }
        UIApplication.shared.open(appUrl) { success in
            if success {
                return
            }
            openNaraBabyStore()
        }
    }

    private func openPlots() {
        UIApplication.shared.open(NaraConfig.plotURL)
    }

    private func openNaraBabyStore() {
        guard let storeUrl = URL(string: "itms-apps://apps.apple.com/app/id1444639029") else {
            return
        }
        UIApplication.shared.open(storeUrl)
    }
}

#Preview {
    ContentView()
}
