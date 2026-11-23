import AppKit
import SwiftUI
import TokenwatchCore
import UserNotifications

private enum TokenStore {
    private static var directory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Tokenwatch", isDirectory: true)
    }

    private static var file: URL { directory.appendingPathComponent("server-token") }

    static func load() throws -> String? {
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        let text = String(data: try Data(contentsOf: file), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let token = text, !token.isEmpty else { throw APIError.invalidToken }
        return token
    }

    static func store(_ token: String) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        try Data(token.utf8).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }

    static func remove() throws {
        if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
    }
}

private final class NotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}

@MainActor
final class AppModel: ObservableObject {
    struct EventFocus: Equatable {
        let id: Int
        private let request = UUID()
        init(id: Int) { self.id = id }
    }

    static let shared = AppModel()

    @Published private(set) var client: APIClient?
    @Published private(set) var revision = UUID()
    @Published private(set) var streamState: StreamState?
    @Published private(set) var recent: [Event] = []
    @Published private(set) var live: [Event] = []
    @Published private(set) var unreadIDs: Set<Int> = []
    @Published private(set) var changedEvents: [Int: Event] = [:]
    @Published private(set) var problem: String?
    @Published var location: SectionID? = .events
    @Published var eventFocus: EventFocus?
    @Published private(set) var serverAddress = UserDefaults.standard.string(forKey: "serverAddress") ?? ""
    @Published private(set) var notifications = UserDefaults.standard.bool(forKey: "notifications")

    private let presenter = NotificationPresenter()
    private var stream: Task<Void, Never>?
    private var refresh: Task<Void, Never>?
    private var isStarted = false
    private var receivedThrough: Int?

    var unreadLabel: String { unreadIDs.count > 100 ? "100+" : String(unreadIDs.count) }
    var status: String {
        switch streamState {
        case .connecting: return "Connecting"
        case .connected: return "Connected"
        case .waiting(let seconds, let reason): return "Retrying in \(seconds)s: \(reason)"
        case nil: return "Not connected"
        }
    }

    func start() {
        guard !isStarted else { return }
        isStarted = true
        UNUserNotificationCenter.current().delegate = presenter
        guard !serverAddress.isEmpty else { return }
        do {
            if let token = try TokenStore.load() { connect(try makeClient(address: serverAddress, token: token)) }
        } catch { problem = error.localizedDescription }
    }

    func save(address: String, token: String) throws {
        let address = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let effectiveToken: String
        if token.isEmpty, address == serverAddress { effectiveToken = try TokenStore.load() ?? "" }
        else { effectiveToken = token }
        let newClient = try makeClient(address: address, token: effectiveToken)
        try TokenStore.store(effectiveToken)
        UserDefaults.standard.set(address, forKey: "serverAddress")
        serverAddress = address
        connect(newClient)
    }

    func forget() throws {
        try TokenStore.remove()
        UserDefaults.standard.removeObject(forKey: "serverAddress")
        serverAddress = ""
        connect(nil)
    }

    func setNotifications(_ newValue: Bool) async throws {
        if newValue {
            guard try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) else {
                throw InputError("Notifications are disabled in macOS System Settings.")
            }
        }
        notifications = newValue
        UserDefaults.standard.set(newValue, forKey: "notifications")
    }

    func mark(_ event: Event) async throws {
        guard let client else { return }
        let generation = revision
        let updated = try await client.setRead(event.id, read: event.readAt == nil)
        guard generation == revision else { return }
        changedEvents[updated.id] = updated
        trackUnread(updated)
        recent = merged(recent, [updated])
        if live.contains(where: { $0.id == updated.id }) { live = merged(live, [updated]) }
        refreshRecent(using: client, generation: generation)
    }

    private func makeClient(address: String, token: String) throws -> APIClient {
        guard let url = URL(string: address) else { throw APIError.invalidServer }
        return try APIClient(server: url, token: token)
    }

    private func connect(_ client: APIClient?) {
        stream?.cancel()
        refresh?.cancel()
        self.client = client
        revision = UUID()
        recent = []
        live = []
        unreadIDs = []
        changedEvents = [:]
        receivedThrough = nil
        eventFocus = nil
        problem = nil
        streamState = nil
        guard let client else { return }
        let generation = revision
        streamState = .connecting
        refreshRecent(using: client, generation: generation)
        stream = Task { [weak self] in
            await EventStream(client: client).run { [weak self] state in
                await self?.update(state, client: client, generation: generation)
            } onEvent: { [weak self] event in
                await self?.receive(event, generation: generation)
            }
        }
    }

    private func update(_ state: StreamState, client: APIClient, generation: UUID) {
        guard revision == generation else { return }
        streamState = state
        if state == .connected { refreshRecent(using: client, generation: generation) }
    }

    private func refreshRecent(using client: APIClient, generation: UUID) {
        refresh?.cancel()
        refresh = Task { [weak self] in
            do {
                var unreadFilter = EventFilter()
                unreadFilter.read = false
                unreadFilter.page = Page(limit: 101)
                async let recentEvents = client.events()
                async let unreadEvents = client.events(unreadFilter)
                let (events, unread) = try await (recentEvents, unreadEvents)
                guard !Task.isCancelled, let self, self.revision == generation else { return }
                let current = events.map { self.changedEvents[$0.id] ?? $0 }
                self.recent = self.merged(self.live, current)
                var unreadByID = Set(unread.compactMap { event in
                    (self.changedEvents[event.id] ?? event).readAt == nil ? event.id : nil
                })
                let newestFetchedID = unread.first?.id
                for event in self.live where newestFetchedID.map({ event.id > $0 }) ?? true {
                    if event.readAt == nil { unreadByID.insert(event.id) }
                }
                self.unreadIDs = unreadByID
                self.problem = nil
            } catch {
                guard !Task.isCancelled, let self, self.revision == generation else { return }
                self.problem = error.localizedDescription
            }
        }
    }

    private func receive(_ event: Event, generation: UUID) {
        guard revision == generation else { return }
        let isNew = receivedThrough.map { event.id > $0 } ?? true
        receivedThrough = max(receivedThrough ?? event.id, event.id)
        let current = changedEvents[event.id] ?? event
        trackUnread(current)
        live = merged(live, [current])
        recent = merged(recent, [current])
        if isNew, current.readAt == nil, notifications {
            let content = UNMutableNotificationContent()
            content.title = current.coinId.map { "\($0) · \(current.kind)" } ?? current.kind
            content.body = "\(current.level.rawValue.capitalized) event"
            content.sound = .default
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "\(generation)-\(event.id)",
                                                                        content: content, trigger: nil),
                                                  withCompletionHandler: nil)
        }
    }

    private func trackUnread(_ event: Event) {
        if event.readAt == nil { unreadIDs.insert(event.id) }
        else { unreadIDs.remove(event.id) }
    }

    private func merged(_ base: [Event], _ updates: [Event]) -> [Event] {
        var byID = Dictionary(uniqueKeysWithValues: base.map { ($0.id, $0) })
        for item in updates { byID[item.id] = item }
        return Array(byID.values.sorted { $0.id > $1.id }.prefix(100))
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) { AppModel.shared.start() }
}

enum SectionID: String, CaseIterable, Identifiable {
    case events = "Events", transfers = "Transfers", snapshots = "Snapshots", curves = "Entity Curves"
    case subscriptions = "Subscriptions", jobs = "Jobs", rules = "Rules", accounts = "Accounts", settings = "Settings"
    var id: Self { self }
    var icon: String {
        switch self {
        case .events: return "bell"
        case .transfers: return "arrow.left.arrow.right"
        case .snapshots: return "camera"
        case .curves: return "chart.xyaxis.line"
        case .subscriptions: return "eye"
        case .jobs: return "list.bullet.rectangle"
        case .rules: return "slider.horizontal.3"
        case .accounts: return "person.2"
        case .settings: return "gear"
        }
    }
}

@main
@MainActor
struct TokenwatchApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        Window("Tokenwatch", id: "main") {
            MainView(model: model).frame(minWidth: 950, minHeight: 620)
        }
        MenuBarExtra {
            MenuView(model: model)
        } label: {
            Label(model.unreadLabel, systemImage: model.unreadIDs.isEmpty ? "bell" : "bell.badge")
                .labelStyle(.titleAndIcon)
                .accessibilityLabel("Tokenwatch: \(model.unreadLabel) unread")
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor
private struct MenuView: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Recent Events · \(model.unreadLabel) unread").font(.headline)
            Text(model.status).font(.caption).foregroundStyle(.secondary)
            if model.recent.isEmpty { Text("No recent events").foregroundStyle(.secondary) }
            ForEach(model.recent.prefix(8)) { event in
                Button {
                    model.location = .events
                    model.eventFocus = AppModel.EventFocus(id: event.id)
                    showMain()
                } label: {
                    Label("\(event.coinId ?? "—") · \(event.kind)",
                          systemImage: event.readAt == nil ? "circle.fill" : "circle")
                        .lineLimit(1)
                }.buttonStyle(.plain)
            }
            Divider()
            Button("Open Main Window") { showMain() }
            Button("Quit Tokenwatch") { NSApplication.shared.terminate(nil) }
        }.padding().frame(width: 290, alignment: .leading)
    }

    private func showMain() {
        dismiss()
        openWindow(id: "main")
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
}

@MainActor
private struct MainView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        NavigationSplitView {
            List(selection: $model.location) {
                ForEach(SectionID.allCases) { item in Label(item.rawValue, systemImage: item.icon).tag(item) }
            }.navigationTitle("Tokenwatch").navigationSplitViewColumnWidth(min: 150, ideal: 180)
        } detail: {
            if model.location == .settings {
                SettingsView(model: model)
            } else if let client = model.client {
                page(client).id(model.revision).padding()
            } else {
                ContentUnavailableView {
                    Label("No Server Configured", systemImage: "network.slash")
                } description: {
                    Text(model.problem ?? "Enter your server URL and bearer token in Settings.")
                } actions: {
                    Button("Open Settings") { model.location = .settings }
                }
            }
        }
    }

    @ViewBuilder private func page(_ client: APIClient) -> some View {
        switch model.location ?? .events {
        case .events: EventsView(client: client, model: model)
        case .transfers: TransfersView(client: client)
        case .snapshots: SnapshotsView(client: client)
        case .curves: CurvesView(client: client)
        case .subscriptions: SubscriptionsView(client: client)
        case .jobs: JobsView(client: client)
        case .rules: RulesView(client: client)
        case .accounts: AccountsView(client: client)
        case .settings: EmptyView()
        }
    }
}

@MainActor
private struct SettingsView: View {
    @ObservedObject var model: AppModel
    @State private var address = ""
    @State private var token = ""
    @State private var action = ActionState()
    @State private var confirming = false

    var body: some View {
        Form {
            TextField("Server URL", text: $address)
            SecureField("Bearer token", text: $token)
            Text("Use HTTPS for remote servers. Leave the token blank to reuse the saved token for the same URL.")
                .foregroundStyle(.secondary)
            LabeledContent("Connection", value: model.status)
            if let problem = model.problem { Text(problem).foregroundStyle(.red) }
            HStack {
                Button("Save and Connect") {
                    action.run {
                        try model.save(address: address, token: token)
                        address = model.serverAddress
                        token = ""
                    }
                }
                Button("Forget Server…", role: .destructive) { confirming = true }
                    .disabled(model.serverAddress.isEmpty)
            }
            Toggle("Local notifications", isOn: Binding(get: { model.notifications }, set: { value in
                action.run { try await model.setNotifications(value) }
            }))
            ActionNotice(action: action)
        }.formStyle(.grouped)
            .disabled(action.busy)
            .navigationTitle("Settings")
            .onAppear { address = model.serverAddress }
            .confirmationDialog("Remove the server and its saved token?", isPresented: $confirming) {
                Button("Forget Server", role: .destructive) {
                    action.run { try model.forget(); address = ""; token = "" }
                }
            }
    }
}
