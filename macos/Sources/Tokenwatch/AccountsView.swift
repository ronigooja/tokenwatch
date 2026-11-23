import SwiftUI
import TokenwatchCore

@MainActor
struct AccountsView: View {
    let client: APIClient
    @State private var accounts: [Account] = []
    @State private var sessions: [String: AccountSession.Status] = [:]
    @State private var checkErrors: [String: String] = [:]
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
        accounts.filter { sessions[$0.id] == .loggedOut || (sessions[$0.id] != .loggedIn && $0.pauseReason == "AuthRequired") }
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Text("Upstream Accounts").font(.headline)
                Spacer()
                Button("Add Account") { adding = true }
                Button("Refresh Status") { action.run { try await load() } }.disabled(action.busy)
            }
            if !signedOut.isEmpty {
                Label("Login required: \(signedOut.map(\.label).joined(separator: ", "))", systemImage: "exclamationmark.octagon.fill")
                    .font(.headline).foregroundStyle(.red)
            }
            ActionNotice(action: action)
            Table(accounts, selection: $selection) {
                TableColumn("Account") { (account: Account) in Text(account.label) }
                TableColumn("Status") { (account: Account) in Text(account.status.rawValue) }
                TableColumn("Session") { (account: Account) in
                    Text(sessions[account.id]?.rawValue ?? "—")
                        .foregroundStyle(signedOut.contains(where: { $0.id == account.id }) ? Color.red : Color.primary)
                }
                TableColumn("Timezone") { (account: Account) in Text(account.timezone ?? "—") }
                TableColumn("Proxy URL") { (account: Account) in Text(account.proxyUrl ?? "—") }
                TableColumn("Pause Reason") { (account: Account) in Text(account.pauseReason ?? "—") }
                TableColumn("Failures") { (account: Account) in Text(String(account.failures)) }
            }
            if let account = selected {
                HStack {
                    Button("Log In") { loggingIn = LoginRequest(account: account, page: .login) }
                    Button("Register") { loggingIn = LoginRequest(account: account, page: .register) }
                    Button("Edit…") { editing = account }.disabled(account.managed != true)
                    Button(account.status == .paused ? "Resume" : "Pause") { action.run { try await setPaused(account) } }
                    Button("Delete…", role: .destructive) { confirmingDelete = true }.disabled(account.managed != true)
                }.disabled(action.busy)
                if let error = checkErrors[account.id] { Text("Session check: \(error)").foregroundStyle(.red) }
                if let event = lastPause[account.id], case .string(let reason)? = event.data["reason"] {
                    Text("Last pause alert: \(reason) · \(event.createdAt.formatted())")
                }
                if account.availableAt > 0 {
                    Text("Available at: \(Date(timeIntervalSince1970: account.availableAt).formatted())")
                }
            }
            if let loginNote { Text(loginNote).foregroundStyle(.secondary) }
            Text("The API exposes failure counts and pause alerts, not a complete recent-error history. Only managed accounts can be edited or deleted.")
                .font(.caption).foregroundStyle(.secondary)
        }.navigationTitle("Accounts").task { await action.load { try await load() } }
            .sheet(isPresented: $adding, onDismiss: {
                loggingIn = pendingLogin
                pendingLogin = nil
            }) {
                AccountForm(client: client) { account, page in
                    accounts.append(account)
                    selection = account.id
                    if let page { pendingLogin = LoginRequest(account: account, page: page) }
                }
            }
            .sheet(item: $editing) { account in
                AccountForm(client: client, existing: account) { updated, _ in
                    accounts = accounts.map { $0.id == updated.id ? updated : $0 }
                }
            }
            .sheet(item: $loggingIn) { request in
                LoginSheet(client: client, account: request.account, page: request.page) { result in
                    switch result {
                    case .success(let status):
                        sessions[request.account.id] = status
                        loginNote = "Session check for \(request.account.label): \(status.rawValue)"
                    case .failure(let error):
                        loginNote = "Session check for \(request.account.label) failed: \(error.localizedDescription)"
                    }
                    action.run { try await load() }
                }
            }
            .confirmationDialog("Delete the selected account?", isPresented: $confirmingDelete) {
                Button("Delete Account", role: .destructive) { action.run { try await deleteSelected() } }
            }
    }

    private func setPaused(_ account: Account) async throws {
        let status: Account.Status = account.status == .paused ? .active : .paused
        let updated = try await client.saveAccount(account.id, update: AccountUpdate(status: status))
        accounts = accounts.map { $0.id == updated.id ? updated : $0 }
    }

    private func deleteSelected() async throws {
        guard let id = selection else { return }
        try await client.deleteAccount(id)
        accounts.removeAll { $0.id == id }
        sessions[id] = nil
        checkErrors[id] = nil
        lastPause[id] = nil
        selection = nil
    }

    private func load() async throws {
        let current = try await client.accounts()
        accounts = current
        sessions = [:]
        checkErrors = [:]
        for account in current {
            try Task.checkCancellation()
            do { sessions[account.id] = try await client.accountSession(account.id).status }
            catch { checkErrors[account.id] = error.localizedDescription }
        }
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
