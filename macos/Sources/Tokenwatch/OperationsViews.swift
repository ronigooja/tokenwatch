import SwiftUI
import TokenwatchCore

@MainActor
struct CoinPicker: View {
    let coins: [Coin]
    @Binding var selection: String

    var body: some View {
        Picker("Coin", selection: $selection) {
            Text("Choose…").tag("")
            if !selection.isEmpty, !coins.contains(where: { $0.id == selection }) { Text(selection).tag(selection) }
            ForEach(coins) { coin in Text("\(coin.symbol) · \(coin.id)").tag(coin.id) }
        }
    }
}

@MainActor
struct AccountPicker: View {
    let accounts: [Account]
    @Binding var selection: String

    var body: some View {
        Picker("Account", selection: $selection) {
            Text("Choose…").tag("")
            if !selection.isEmpty, !accounts.contains(where: { $0.id == selection }) { Text(selection).tag(selection) }
            ForEach(accounts) { account in Text("\(account.id) · \(account.status.rawValue)").tag(account.id) }
        }
    }
}

@MainActor
struct SubscriptionsView: View {
    let client: APIClient
    @State private var subscriptions: [TokenwatchCore.Subscription] = []
    @State private var coins: [Coin] = []
    @State private var jobs: [Job] = []
    @State private var coin = ""
    @State private var selection: String?
    @State private var gap: Event?
    @State private var checkedGap = false
    @State private var confirming = false
    @State private var action = ActionState()

    private var selected: TokenwatchCore.Subscription? { subscriptions.first(where: { $0.id == selection }) }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                CoinPicker(coins: coins.filter { coin in !subscriptions.contains(where: { $0.coinId == coin.id }) }, selection: $coin)
                Button("Add Subscription") {
                    let id = coin
                    action.run {
                        subscriptions.append(try await client.saveSubscription(id, enabled: true))
                        coin = ""
                        selection = id
                    }
                }.disabled(coin.isEmpty)
                Spacer()
                Button("Refresh") { action.run { try await load() } }
            }.disabled(action.busy)
            ActionNotice(action: action)
            Table(subscriptions, selection: $selection) {
                TableColumn("Coin") { (row: TokenwatchCore.Subscription) in Text(row.coinId) }
                TableColumn("Subscription") { (row: TokenwatchCore.Subscription) in Text(row.enabled ? "Enabled" : "Disabled") }
                TableColumn("Job Frequency (Configured)") { (row: TokenwatchCore.Subscription) in Text(frequency(row.coinId)) }
            }
            if let selected {
                HStack {
                    Button(selected.enabled ? "Disable" : "Enable") {
                        action.run {
                            let updated = try await client.saveSubscription(selected.id, enabled: !selected.enabled)
                            subscriptions = subscriptions.map { $0.id == updated.id ? updated : $0 }
                        }
                    }
                    Button("Remove…", role: .destructive) { confirming = true }
                    Button("Check Latest Gap Alert") { action.run { try await loadGap(selected.id) } }
                }.disabled(action.busy)
                if checkedGap {
                    if let gap {
                        Text("Latest recorded gap alert: \(gap.createdAt.formatted())").foregroundStyle(.orange)
                    } else {
                        Text("No gap alert found in retained events.").foregroundStyle(.secondary)
                    }
                }
            }
            Text("Edit scheduled frequency in Jobs. The API does not expose the adaptive interval, transfer rate, or latest poll gap state.")
                .font(.caption).foregroundStyle(.secondary)
        }.navigationTitle("Subscriptions")
            .task { await action.load { try await load() } }
            .onChange(of: selection) { _, _ in gap = nil; checkedGap = false }
            .confirmationDialog("Remove this subscription?", isPresented: $confirming) {
                Button("Remove", role: .destructive) {
                    guard let selected else { return }
                    action.run {
                        try await client.deleteSubscription(selected.id)
                        subscriptions.removeAll { $0.id == selected.id }
                        selection = nil
                    }
                }
            }
    }

    private func load() async throws {
        async let subList = client.subscriptions()
        async let coinList = client.coins()
        async let jobList = client.jobs()
        (subscriptions, coins, jobs) = try await (subList, coinList, jobList)
    }

    private func frequency(_ id: String) -> String {
        let matching = jobs.filter { $0.kind == .transfers && $0.coinId == id }
        let values = matching.map { "\($0.accountId): \(number($0.intervalSeconds)) s" + ($0.enabled ? "" : " (off)") }
        return values.isEmpty ? "No transfer job" : values.joined(separator: ", ")
    }

    private func loadGap(_ id: String) async throws {
        var filter = EventFilter()
        filter.coinId = id
        filter.kind = "transfer_gap"
        filter.page = Page(limit: 1)
        let event = try await client.events(filter).first
        guard selection == id else { return }
        gap = event
        checkedGap = true
    }
}

@MainActor
struct JobsView: View {
    private struct Edit: Identifiable { let id = UUID(); let job: Job? }

    let client: APIClient
    @State private var jobs: [Job] = []
    @State private var accounts: [Account] = []
    @State private var coins: [Coin] = []
    @State private var exclusions: [Exclusion] = []
    @State private var selectedJob: Int?
    @State private var selectedExclusion: String?
    @State private var excludeCoin = ""
    @State private var reason = ""
    @State private var edit: Edit?
    @State private var confirmingJob = false
    @State private var confirmingExclusion = false
    @State private var action = ActionState()

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Button("New Job…") { edit = Edit(job: nil) }
                Button("Edit…") {
                    if let job = jobs.first(where: { $0.id == selectedJob }) { edit = Edit(job: job) }
                }.disabled(selectedJob == nil)
                Button("Delete…", role: .destructive) { confirmingJob = true }.disabled(selectedJob == nil)
                Spacer()
                Button("Refresh") { action.run { try await load() } }
            }.disabled(action.busy)
            ActionNotice(action: action)
            VSplitView {
                jobsTable.frame(minHeight: 145)
                exclusionPanel.frame(minHeight: 155)
            }
        }.navigationTitle("Jobs")
            .task { await action.load { try await load() } }
            .sheet(item: $edit) { record in
                JobForm(client: client, job: record.job, accounts: accounts, coins: coins) { job in
                    jobs = (jobs.filter { $0.id != job.id } + [job]).sorted { $0.id < $1.id }
                    selectedJob = job.id
                    edit = nil
                }
            }
            .confirmationDialog("Delete the selected job?", isPresented: $confirmingJob) {
                Button("Delete Job", role: .destructive) {
                    guard let id = selectedJob else { return }
                    action.run { try await client.deleteJob(id); jobs.removeAll { $0.id == id }; selectedJob = nil }
                }
            }
    }

    private var jobsTable: some View {
        Table(jobs, selection: $selectedJob) {
            TableColumn("Job") { (job: Job) in Text(job.kind.rawValue) }
            TableColumn("Account") { (job: Job) in Text(job.accountId) }
            TableColumn("Coin") { (job: Job) in Text(job.coinId ?? "—") }
            TableColumn("Frequency (s)") { (job: Job) in Text(number(job.intervalSeconds)) }
            TableColumn("Minimum USD") { (job: Job) in Text(usd(job.options.balanceMin)) }
            TableColumn("Enabled") { (job: Job) in Text(job.enabled ? "Yes" : "No") }
            TableColumn("Next Run") { (job: Job) in
                Text(job.nextRun > 0 ? Date(timeIntervalSince1970: job.nextRun).formatted() : "—")
            }
        }
    }

    private var exclusionPanel: some View {
        VStack(alignment: .leading) {
            Text("Global Coin Exclusions").font(.headline)
            HStack {
                CoinPicker(coins: coins, selection: $excludeCoin)
                TextField("Reason", text: $reason)
                Button("Save Exclusion") {
                    guard let reason = reason.nonblank else { return }
                    let id = excludeCoin
                    action.run {
                        let saved = try await client.saveExclusion(id, reason: reason)
                        exclusions = (exclusions.filter { $0.id != id } + [saved]).sorted { $0.id < $1.id }
                        selectedExclusion = id
                    }
                }.disabled(excludeCoin.isEmpty || reason.nonblank == nil)
                Button("Remove…", role: .destructive) { confirmingExclusion = true }.disabled(selectedExclusion == nil)
            }.disabled(action.busy)
            Table(exclusions, selection: $selectedExclusion) {
                TableColumn("Coin", value: \.coinId)
                TableColumn("Reason", value: \.reason)
            }
        }.padding(.top, 6)
            .confirmationDialog("Remove the selected global exclusion?", isPresented: $confirmingExclusion) {
                Button("Remove Exclusion", role: .destructive) {
                    guard let id = selectedExclusion else { return }
                    action.run { try await client.deleteExclusion(id); exclusions.removeAll { $0.id == id }; selectedExclusion = nil }
                }
            }
    }

    private func load() async throws {
        async let jobList = client.jobs()
        async let accountList = client.accounts()
        async let coinList = client.coins()
        async let exclusionList = client.exclusions()
        (jobs, accounts, coins, exclusions) = try await (jobList, accountList, coinList, exclusionList)
    }
}

@MainActor
private struct JobForm: View {
    let client: APIClient
    let job: Job?
    let accounts: [Account]
    let coins: [Coin]
    let onSave: (Job) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var kind: JobKind = .transfers
    @State private var account = ""
    @State private var coin = ""
    @State private var frequency = ""
    @State private var minimum = ""
    @State private var enabled = false
    @State private var action = ActionState()

    var body: some View {
        Form {
            Picker("Job type", selection: $kind) {
                ForEach(JobKind.allCases, id: \.self) { item in Text(item.rawValue).tag(item) }
            }
            AccountPicker(accounts: accounts, selection: $account)
            if kind == .transfers { CoinPicker(coins: coins, selection: $coin) }
            if kind == .daily { TextField("Minimum USD", text: $minimum) }
            TextField("Frequency (seconds)", text: $frequency)
            Toggle("Enabled", isOn: $enabled)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Save Job") { action.run { try await save() } }.disabled(account.isEmpty || (kind == .transfers && coin.isEmpty))
            }
        }.formStyle(.grouped).frame(width: 420).disabled(action.busy).interactiveDismissDisabled(action.busy)
            .onAppear {
                guard let job else { return }
                kind = job.kind; account = job.accountId; coin = job.coinId ?? ""
                frequency = String(job.intervalSeconds); minimum = job.options.balanceMin.map { String($0) } ?? ""
                enabled = job.enabled
            }
    }

    private func save() async throws {
        guard let interval = try frequency.nonnegative("Frequency"), interval > 0 else {
            throw InputError("Enter a frequency greater than zero.")
        }
        let balanceMin = try (kind == .daily ? minimum.nonnegative("Minimum USD") : nil)
        if kind == .daily, balanceMin == nil { throw InputError("Enter the daily job's minimum USD.") }
        let body = JobInput(kind: kind, accountId: account, coinId: kind == .transfers ? coin : nil,
                            options: JobOptions(balanceMin: balanceMin), intervalSeconds: interval, enabled: enabled)
        onSave(try await client.saveJob(body, id: job?.id))
    }
}

@MainActor
struct AccountsView: View {
    let client: APIClient
    @State private var accounts: [Account] = []
    @State private var sessions: [String: AccountSession.Status] = [:]
    @State private var checkErrors: [String: String] = [:]
    @State private var lastPause: [String: Event] = [:]
    @State private var selection: String?
    @State private var action = ActionState()

    private var signedOut: [String] {
        accounts.filter { sessions[$0.id] == .loggedOut || (sessions[$0.id] != .loggedIn && $0.pauseReason == "AuthRequired") }.map(\.id)
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Text("Upstream Accounts").font(.headline)
                Spacer()
                Button("Refresh Status") { action.run { try await load() } }.disabled(action.busy)
            }
            if !signedOut.isEmpty {
                Label("Login required: \(signedOut.joined(separator: ", "))", systemImage: "exclamationmark.octagon.fill")
                    .font(.headline).foregroundStyle(.red)
            }
            ActionNotice(action: action)
            Table(accounts, selection: $selection) {
                TableColumn("Account") { (account: Account) in Text(account.id) }
                TableColumn("Status") { (account: Account) in Text(account.status.rawValue) }
                TableColumn("Session") { (account: Account) in
                    Text(sessions[account.id]?.rawValue ?? "—").foregroundStyle(signedOut.contains(account.id) ? Color.red : Color.primary)
                }
                TableColumn("Proxy Reference") { (account: Account) in Text(account.proxyRef) }
                TableColumn("Pause Reason") { (account: Account) in Text(account.pauseReason ?? "—") }
                TableColumn("Failures") { (account: Account) in Text(String(account.failures)) }
            }
            if let account = accounts.first(where: { $0.id == selection }) {
                if let error = checkErrors[account.id] { Text("Session check: \(error)").foregroundStyle(.red) }
                if let event = lastPause[account.id], case .string(let reason)? = event.data["reason"] {
                    Text("Last pause alert: \(reason) · \(event.createdAt.formatted())")
                }
                if account.availableAt > 0 {
                    Text("Available at: \(Date(timeIntervalSince1970: account.availableAt).formatted())")
                }
            }
            Text("The API exposes failure counts and pause alerts, not a complete recent-error history.")
                .font(.caption).foregroundStyle(.secondary)
        }.navigationTitle("Accounts").task { await action.load { try await load() } }
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
