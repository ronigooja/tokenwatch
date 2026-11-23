import SwiftUI
import TokenwatchCore

@MainActor
struct CoinsView: View {
    let client: APIClient
    @State private var coins: [Coin] = []
    @State private var manualExclusions: Set<String> = []
    @State private var mapping: CoinMapping?
    @State private var status: CoinStatus?
    @State private var search = ""
    @State private var selection: String?
    @State private var settingMapping = false
    @State private var excluding = false
    @State private var action = ActionState()
    @State private var mappingLookup = ActionState()

    private var selected: Coin? { coins.first(where: { $0.id == selection }) }

    private var statusOptions: [CoinStatus] {
        let unknown = Set(coins.map(\.status)).filter { !CoinStatus.known.contains($0) }
        return CoinStatus.known + unknown.sorted { $0.rawValue < $1.rawValue }
    }

    private var filtered: [Coin] {
        let query = search.nonblank ?? ""
        return coins.filter { coin in
            (status == nil || coin.status == status) &&
                (query.isEmpty || coin.symbol.localizedCaseInsensitiveContains(query) ||
                    coin.id.localizedCaseInsensitiveContains(query))
        }
    }

    private func count(_ value: CoinStatus) -> Int { coins.filter { $0.status == value }.count }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Picker("Status", selection: $status) {
                    Text("All (\(coins.count))").tag(nil as CoinStatus?)
                    ForEach(statusOptions, id: \.self) { item in
                        Text("\(item.label) (\(count(item)))").tag(Optional(item))
                    }
                }.fixedSize()
                TextField("Search symbol or ID", text: $search).frame(maxWidth: 260)
                Spacer()
                Button("Refresh") { action.run { try await load() } }
            }.disabled(action.busy)
            ActionNotice(action: action)
            HSplitView {
                Table(filtered, selection: $selection) {
                    TableColumn("Symbol") { (coin: Coin) in Text(coin.symbol) }
                    TableColumn("Status") { (coin: Coin) in Text(coin.status.label) }
                    TableColumn("Arkham coin number") { (coin: Coin) in Text(coin.pricingId ?? "—") }
                    TableColumn("Chains") { (coin: Coin) in Text(coin.chains.joined(separator: ", ")).lineLimit(1) }
                    TableColumn("Source") { (coin: Coin) in Text(coin.sources.map(\.label).joined(separator: ", ")) }
                }.frame(minWidth: 460)
                if let coin = selected {
                    detail(coin).frame(minWidth: 240, idealWidth: 280)
                }
            }
        }.navigationTitle("Coins")
            .task { await action.load { try await load() } }
            .task(id: selection) {
                mapping = nil
                guard let id = selection else { return }
                await mappingLookup.load { mapping = try await client.coinMapping(id) }
            }
            .sheet(isPresented: $settingMapping) {
                if let coin = selected {
                    MappingForm(client: client, coin: coin, current: mapping?.pricingId) { saved in
                        settingMapping = false
                        mapping = saved
                        action.run { try await load() }
                    }
                }
            }
            .sheet(isPresented: $excluding) {
                if let coin = selected {
                    ExclusionForm(client: client, coin: coin) {
                        excluding = false
                        action.run { try await load() }
                    }
                }
            }
    }

    private func detail(_ coin: Coin) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(coin.symbol).font(.headline)
                Text(coin.id).font(.caption.monospaced()).textSelection(.enabled)
                LabeledContent("Status", value: coin.status.label)
                LabeledContent("Arkham coin number", value: coin.pricingId ?? "—")
                if let reason = coin.reason { Text(reason).foregroundStyle(.secondary).textSelection(.enabled) }
                ForEach(coin.deployments, id: \.self) { deployment in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(deployment.chain) · \(deployment.feed.label)")
                        Text(deployment.address ?? "No address").font(.caption.monospaced()).textSelection(.enabled)
                        Text("Deposit \(flag(deployment.depositEnabled)) · Withdraw \(flag(deployment.withdrawEnabled))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Divider()
                ActionNotice(action: mappingLookup)
                HStack {
                    Button("Set Arkham coin number…") { settingMapping = true }
                    if mapping != nil {
                        Button("Clear") { action.run { try await clearMapping(coin.id) } }
                    }
                }.disabled(action.busy)
                if manualExclusions.contains(coin.id) {
                    Button("Include") { action.run { try await include(coin.id) } }.disabled(action.busy)
                } else if coin.status != .excluded {
                    Button("Exclude…") { excluding = true }.disabled(action.busy)
                }
            }.padding(10)
        }
    }

    private func flag(_ value: Bool?) -> String { value.map { $0 ? "Yes" : "No" } ?? "—" }

    private func load() async throws {
        async let coinList = client.coins()
        async let exclusionList = client.exclusions()
        (coins, manualExclusions) = try await (coinList, Set(exclusionList.map(\.coinId)))
        if let selection, !coins.contains(where: { $0.id == selection }) { self.selection = nil }
    }

    private func clearMapping(_ id: String) async throws {
        try await client.deleteCoinMapping(id)
        mapping = nil
        try await load()
    }

    private func include(_ id: String) async throws {
        try await client.deleteExclusion(id)
        try await load()
    }
}

@MainActor
private struct MappingForm: View {
    let client: APIClient
    let coin: Coin
    let current: String?
    let onSave: (CoinMapping) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var pricingID = ""
    @State private var action = ActionState()

    var body: some View {
        Form {
            Text(coin.symbol)
            TextField("Arkham coin number", text: $pricingID)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Save") { action.run { try await save() } }.disabled(pricingID.nonblank == nil)
            }
        }.formStyle(.grouped).frame(width: 420).disabled(action.busy).interactiveDismissDisabled(action.busy)
            .onAppear { pricingID = current ?? "" }
    }

    private func save() async throws {
        guard let value = pricingID.nonblank else { throw InputError("Enter an Arkham coin number.") }
        onSave(try await client.saveCoinMapping(coin.id, pricingId: value))
    }
}

@MainActor
private struct ExclusionForm: View {
    let client: APIClient
    let coin: Coin
    let onSave: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var reason = ""
    @State private var action = ActionState()

    var body: some View {
        Form {
            Text(coin.symbol)
            TextField("Reason", text: $reason)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Exclude") { action.run { try await save() } }.disabled(reason.nonblank == nil)
            }
        }.formStyle(.grouped).frame(width: 420).disabled(action.busy).interactiveDismissDisabled(action.busy)
    }

    private func save() async throws {
        guard let text = reason.nonblank else { throw InputError("Enter a reason.") }
        _ = try await client.saveExclusion(coin.id, reason: text)
        onSave()
    }
}
