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
    var anyAccountLabel: String? = nil

    var body: some View {
        Picker("Account", selection: $selection) {
            Text(anyAccountLabel ?? "Choose…").tag("")
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
                TableColumn("Coin") { (row: TokenwatchCore.Subscription) in Text(coinLabel(row.coinId, coins)).help(row.coinId) }
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
        let values = matching.map { "\($0.accountId ?? "Any account"): \(number($0.intervalSeconds)) s" + ($0.enabled ? "" : " (off)") }
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
            TableColumn("Account") { (job: Job) in Text(job.accountId ?? "Any account") }
            TableColumn("Coin") { (job: Job) in Text(job.coinId.map { coinLabel($0, coins) } ?? "—") }
            TableColumn("Frequency (s)") { (job: Job) in Text(number(job.intervalSeconds)) }
            TableColumn("Minimum USD") { (job: Job) in Text(usd(job.options.balanceMin)) }
            TableColumn("Enabled") { (job: Job) in Text(job.enabled ? "Yes" : "No") }
            TableColumn("Next Run") { (job: Job) in
                Text(job.nextRun > 0 ? Date(timeIntervalSince1970: job.nextRun).formatted() : "—")
            }
            TableColumn("Wait") { (job: Job) in Text(job.waitReason ?? "—").lineLimit(1).help(job.waitReason ?? "") }
            TableColumn("Last Error") { (job: Job) in Text(job.lastError ?? "").lineLimit(1).help(job.lastError ?? "") }
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
                TableColumn("Coin") { (row: Exclusion) in Text(coinLabel(row.coinId, coins)).help(row.coinId) }
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

    /// The automatic snapshots job is a singleton. It can be edited (cycle and enabled) but never created or converted here.
    private var isSnapshotJob: Bool { job?.kind == .snapshots }

    var body: some View {
        Form {
            if isSnapshotJob {
                LabeledContent("Job type", value: JobKind.snapshots.rawValue)
                LabeledContent("Account", value: "Any account")
                Text("Snapshots are collected automatically. Set how long each cycle lasts.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Picker("Job type", selection: $kind) {
                    ForEach(JobKind.editable, id: \.self) { item in Text(item.rawValue).tag(item) }
                }
                AccountPicker(accounts: accounts, selection: $account, anyAccountLabel: kind == .transfers ? nil : "Any account")
                if kind == .transfers { CoinPicker(coins: coins, selection: $coin) }
                if kind == .daily { TextField("Minimum USD", text: $minimum) }
            }
            TextField(isSnapshotJob ? "Cycle (seconds)" : "Frequency (seconds)", text: $frequency)
            Toggle("Enabled", isOn: $enabled)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Save Job") { action.run { try await save() } }
                    .disabled(kind == .transfers && (account.isEmpty || coin.isEmpty))
            }
        }.formStyle(.grouped).frame(width: 420).disabled(action.busy).interactiveDismissDisabled(action.busy)
            .onAppear {
                guard let job else { account = defaultAccount(for: .transfers); return }
                kind = job.kind; account = job.accountId ?? ""; coin = job.coinId ?? ""
                frequency = String(job.intervalSeconds); minimum = job.options.balanceMin.map { String($0) } ?? ""
                enabled = job.enabled
            }
            .onChange(of: kind) { _, newKind in
                if newKind == .transfers, account.isEmpty { account = defaultAccount(for: .transfers) }
            }
            .onChange(of: accounts.map(\.id)) { _, _ in
                if kind == .transfers, account.isEmpty { account = defaultAccount(for: .transfers) }
            }
    }

    /// Transfers jobs must name an account; other kinds default to any active account.
    private func defaultAccount(for kind: JobKind) -> String {
        kind == .transfers ? accounts.first?.id ?? "" : ""
    }

    private func save() async throws {
        if isSnapshotJob {
            guard let cycle = try frequency.nonnegative("Cycle"), cycle > 0 else {
                throw InputError("Enter a cycle greater than zero.")
            }
            onSave(try await client.saveJob(.snapshotCycle(seconds: cycle, enabled: enabled), id: job?.id))
            return
        }
        guard let interval = try frequency.nonnegative("Frequency"), interval > 0 else {
            throw InputError("Enter a frequency greater than zero.")
        }
        let balanceMin = try (kind == .daily ? minimum.nonnegative("Minimum USD") : nil)
        if kind == .daily, balanceMin == nil { throw InputError("Enter the daily job's minimum USD.") }
        let chosenAccount = account.nonblank
        if kind == .transfers, chosenAccount == nil { throw InputError("Choose an account for a transfers job.") }
        if kind == .transfers, coin.isEmpty { throw InputError("Choose a coin for a transfers job.") }
        let body = JobInput(kind: kind, accountId: chosenAccount,
                            coinId: kind == .transfers ? coin : nil,
                            options: JobOptions(balanceMin: balanceMin), intervalSeconds: interval, enabled: enabled)
        onSave(try await client.saveJob(body, id: job?.id))
    }
}
