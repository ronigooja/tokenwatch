import SwiftUI
import TokenwatchCore

@MainActor
struct AccountsView: View {
    let client: APIClient
    @ObservedObject var store: AccountStore
    private var accounts: [Account] { store.accounts }
    @State private var lastPause: [String: Event] = [:]
    @State private var selection: String?
    @State private var adding = false
    @State private var pendingLogin: LoginRequest?
    @State private var loggingIn: LoginRequest?
    @State private var editing: Account?
    @State private var confirmingDelete = false
    @State private var loginNote: String?
    @State private var action = ActionState()

    private var selected: Account? { accounts.first(where: { $0.id == selection }) }

    private var signedOut: [Account] {
        accounts.filter { $0.sessionStatus == .loggedOut || ($0.sessionStatus != .loggedIn && $0.pauseReason == "AuthRequired") }
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Text("Upstream Accounts").font(.headline)
                Spacer()
                Button("Add Account") { adding = true }
            }
            if !signedOut.isEmpty {
                Label("Login required: \(signedOut.map(\.label).joined(separator: ", "))", systemImage: "exclamationmark.octagon.fill")
                    .font(.headline).foregroundStyle(.red)
            }
            Table(accounts, selection: $selection) {
                TableColumn("Account") { (account: Account) in Text(account.label) }
                    .width(min: 180, ideal: 260)
                TableColumn("Status") { (account: Account) in Text(account.status.rawValue) }
                TableColumn("Session") { (account: Account) in
                    Text(sessionText(account))
                    .lineLimit(1)
                    .foregroundStyle(signedOut.contains(where: { $0.id == account.id }) ? Color.red : Color.primary)
                }
                TableColumn("Timezone") { (account: Account) in Text(account.timezone ?? "—") }
                TableColumn("Proxy URL") { (account: Account) in Text(account.proxyUrl ?? "—") }
                    .width(min: 180, ideal: 260)
                TableColumn("Pause Reason") { (account: Account) in Text(account.pauseReason ?? "—") }
                TableColumn("Failures") { (account: Account) in Text(String(account.failures)) }
                    .width(min: 60, ideal: 70, max: 90)
            }
            HStack {
                if let account = selected {
                    Button("Log In") { loggingIn = LoginRequest(account: account, page: .login) }
                    Button("Register") { loggingIn = LoginRequest(account: account, page: .register) }
                    Button("Maintenance…") { loggingIn = LoginRequest(account: account, page: .maintenance) }
                    Button("Edit…") { editing = account }.disabled(account.managed != true)
                    Button(account.status == .paused ? "Resume" : "Pause") { action.run { try await setPaused(account) } }
                    Button("Delete…", role: .destructive) { confirmingDelete = true }.disabled(account.managed != true)
                }
            }.disabled(action.busy)
            VStack(alignment: .leading) {
                ActionNotice(action: action)
                if let account = selected {
                    if let event = lastPause[account.id], case .string(let reason)? = event.data["reason"] {
                        Text("Last pause alert: \(reason) · \(event.createdAt.formatted())")
                    }
                    if account.availableAt > 0 {
                        Text("Available at: \(Date(timeIntervalSince1970: account.availableAt).formatted())")
                    }
                }
                if let loginNote { Text(loginNote).foregroundStyle(.secondary) }
            }
            .font(.caption)
            .frame(height: 60, alignment: .topLeading)
        }.navigationTitle("Accounts").task { await action.load { try await load() } }
            .sheet(isPresented: $adding, onDismiss: {
                loggingIn = pendingLogin
                pendingLogin = nil
            }) {
                AccountForm(client: client) { account, page in
                    store.replace(account)
                    selection = account.id
                    if let page { pendingLogin = LoginRequest(account: account, page: page) }
                }
            }
            .sheet(item: $editing) { account in
                AccountForm(client: client, existing: account) { updated, _ in
                    store.replace(updated)
                }
            }
            .sheet(item: $loggingIn) { request in
                LoginSheet(client: client, account: request.account, page: request.page) { result in
                    switch result {
                    case .success(let status):
                        if let status {
                            loginNote = "Session check for \(request.account.label): \(status.rawValue)"
                        }
                    case .failure(let error):
                        loginNote = "Session check for \(request.account.label) failed: \(error.localizedDescription)"
                    }
                    action.run { try await store.reload { try await client.accounts() } }
                }
            }
            .confirmationDialog("Delete the selected account?", isPresented: $confirmingDelete) {
                Button("Delete Account", role: .destructive) { action.run { try await deleteSelected() } }
            }
    }

    private func sessionText(_ account: Account) -> String {
        guard let status = account.sessionStatus else { return "—" }
        guard let timestamp = account.sessionCheckedAt else { return status.rawValue }
        let checked = Date(timeIntervalSince1970: timestamp)
        return "\(status.rawValue) · \(age(checked))"
    }

    private func age(_ date: Date) -> String {
        let seconds = max(0, Int(Date.now.timeIntervalSince(date)))
        switch seconds {
        case ..<60: return "\(seconds)s"
        case ..<3600: return "\(seconds / 60)m"
        case ..<86400: return "\(seconds / 3600)h"
        default: return "\(seconds / 86400)d"
        }
    }

    private func setPaused(_ account: Account) async throws {
        let status: Account.Status = account.status == .paused ? .active : .paused
        let updated = try await client.saveAccount(account.id, update: AccountUpdate(status: status))
        store.replace(updated)
    }

    private func deleteSelected() async throws {
        guard let id = selection else { return }
        try await client.deleteAccount(id)
        store.remove(id)
        lastPause[id] = nil
        selection = nil
    }

    private func load() async throws {
        var filter = EventFilter()
        filter.kind = "account_paused"
        filter.page = Page(limit: 500)
        let events = try await client.events(filter)
        var latest: [String: Event] = [:]
        for event in events {
            if case .string(let id)? = event.data["account_id"], latest[id] == nil { latest[id] = event }
        }
        lastPause = latest
    }
}
