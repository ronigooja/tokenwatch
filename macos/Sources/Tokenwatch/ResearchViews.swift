import Charts
import SwiftUI
import TokenwatchCore

@MainActor
struct SnapshotsView: View {
    let client: APIClient
    @State private var action = ActionState()
    @State private var list: [Snapshot] = []
    @State private var accounts: [Account] = []
    @State private var coins: [Coin] = []
    @State private var page = Page()
    @State private var snapshotID: Int?
    @State private var before: Int?
    @State private var after: Int?
    @State private var detail: SnapshotDetail?
    @State private var diff: SnapshotDiff?
    @State private var creating = false

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                snapshotPicker("Snapshot", selection: $snapshotID)
                Button("Open") {
                    guard let snapshotID else { return }
                    action.run { detail = try await client.snapshot(snapshotID); diff = nil }
                }.disabled(snapshotID == nil)
                if detail != nil || diff != nil {
                    Button("Show List") { detail = nil; diff = nil }
                }
                Spacer()
                Button("New Snapshot…") { creating = true }
                Button("Refresh") { action.run { try await bootstrap() } }
            }.disabled(action.busy)
            HStack {
                snapshotPicker("Before", selection: $before)
                snapshotPicker("After", selection: $after)
                Button("Compare") {
                    guard let before, let after else { return }
                    action.run { diff = try await client.compareSnapshots(before: before, after: after); detail = nil }
                }.disabled(before == nil || after == nil || before == after)
            }.disabled(action.busy)
            Pager(page: page, count: list.count, busy: action.busy) { offset in
                action.run { try await loadList(Page(offset: offset)) }
            }
            ActionNotice(action: action)
            if let detail {
                let minimum = usd(detail.balanceMin)
                let pricing = detail.pricingIds?.joined(separator: ", ") ?? "All pricing IDs"
                Text("#\(detail.id) · \(detail.interval.rawValue) · Minimum: \(minimum) · \(pricing)")
                if detail.truncated {
                    Label("Snapshot may be truncated", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                }
                SnapshotRows(rows: detail.rows)
            } else if let diff {
                DiffTable(diff: diff)
            } else {
                Table(list) {
                    TableColumn("ID") { (entry: Snapshot) in Text(String(entry.id)) }.width(65)
                    TableColumn("Created") { (entry: Snapshot) in Text(entry.createdAt, format: .dateTime) }
                    TableColumn("Interval") { (entry: Snapshot) in Text(entry.interval.rawValue) }
                    TableColumn("Minimum USD") { (entry: Snapshot) in Text(usd(entry.balanceMin)) }
                    TableColumn("Coverage") { (entry: Snapshot) in Text(entry.truncated ? "Possibly truncated" : "—") }
                }
            }
        }.navigationTitle("Snapshots")
            .task { await action.load { try await bootstrap() } }
            .sheet(isPresented: $creating) {
                SnapshotForm(client: client, accounts: accounts, coins: coins) { snapshot in
                    list = [snapshot] + list.filter { $0.id != snapshot.id }
                    snapshotID = snapshot.id
                    creating = false
                    action.run { try await loadList(Page()); detail = try await client.snapshot(snapshot.id); diff = nil }
                }
            }
    }

    private func bootstrap() async throws {
        async let accountList = client.accounts()
        async let coinList = client.coins()
        try await loadList(page)
        (accounts, coins) = try await (accountList, coinList)
    }

    private func loadList(_ newPage: Page) async throws {
        list = try await client.snapshots(newPage)
        page = newPage
        if !list.contains(where: { $0.id == snapshotID }) { snapshotID = list.first?.id }
    }

    private func snapshotPicker(_ label: String, selection: Binding<Int?>) -> some View {
        Picker(label, selection: selection) {
            Text("Choose…").tag(nil as Int?)
            if let value = selection.wrappedValue, !list.contains(where: { $0.id == value }) {
                Text("#\(value)").tag(Optional(value))
            }
            ForEach(list) { entry in Text("#\(entry.id) · \(entry.createdAt.formatted())").tag(Optional(entry.id)) }
        }
    }
}

@MainActor
private struct SnapshotForm: View {
    let client: APIClient
    let accounts: [Account]
    let coins: [Coin]
    let onSave: (Snapshot) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var account = ""
    @State private var interval: Interval = .week
    @State private var minimum = ""
    @State private var pricingIDs = Set<String>()
    @State private var action = ActionState()

    private var availableCoins: [Coin] { coins.filter { $0.pricingId != nil } }

    var body: some View {
        Form {
            AccountPicker(accounts: accounts, selection: $account)
            Picker("Interval", selection: $interval) {
                ForEach(Interval.allCases, id: \.self) { item in Text(item.rawValue).tag(item) }
            }
            TextField("Minimum USD (optional)", text: $minimum)
            Text("Select up to 50 coins, or none for an unfiltered snapshot.").foregroundStyle(.secondary)
            List(availableCoins) { coin in
                if let pricingID = coin.pricingId {
                    Toggle("\(coin.symbol) · \(pricingID)", isOn: Binding(get: { pricingIDs.contains(pricingID) }, set: { enabled in
                        if enabled { pricingIDs.insert(pricingID) } else { pricingIDs.remove(pricingID) }
                    }))
                }
            }.frame(minHeight: 150, idealHeight: 200)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Run Snapshot") { action.run { try await save() } }.disabled(account.isEmpty)
            }
        }.formStyle(.grouped).frame(width: 460).disabled(action.busy).interactiveDismissDisabled(action.busy)
    }

    private func save() async throws {
        guard pricingIDs.count <= 50 else { throw InputError("Select no more than 50 coins.") }
        let input = SnapshotInput(accountId: account, interval: interval, balanceMin: try minimum.nonnegative("Minimum USD"),
                                  pricingIds: pricingIDs.isEmpty ? nil : pricingIDs.sorted())
        onSave(try await client.createSnapshot(input))
    }
}

@MainActor
private struct SnapshotRows: View {
    let rows: [SnapshotRow]
    @State private var selection: String?

    var body: some View {
        Table(rows, selection: $selection) {
            TableColumn("Entity") { (row: SnapshotRow) in Text(row.entityName ?? row.entityId).help(row.entityId) }
            TableColumn("Type") { (row: SnapshotRow) in Text(row.entityType ?? "—") }
            TableColumn("Units") { (row: SnapshotRow) in Text(number(row.balanceUnit)) }
            TableColumn("USD") { (row: SnapshotRow) in Text(usd(row.balanceUsd)) }
            TableColumn("Previous Units") { (row: SnapshotRow) in Text(number(row.prevBalanceUnit)) }
            TableColumn("Previous USD") { (row: SnapshotRow) in Text(usd(row.prevBalanceUsd)) }
        }
        if let row = rows.first(where: { $0.id == selection }) {
            Text(row.entityId).textSelection(.enabled)
            if let balances = row.tokenBalances {
                ScrollView {
                    Text(jsonText(balances)).font(.caption.monospaced()).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: 105)
            }
        }
    }
}

@MainActor
private struct DiffTable: View {
    private struct Entry: Identifiable {
        let status: String
        let color: Color
        let before: SnapshotRow?
        let after: SnapshotRow?
        var id: String { "\(status):\(after?.entityId ?? before?.entityId ?? "")" }
        var label: String { after?.entityName ?? before?.entityName ?? after?.entityId ?? before?.entityId ?? "—" }

        var tokenChanges: String { before?.tokenBalances == after?.tokenBalances ? "—" : "Changed" }
    }

    let diff: SnapshotDiff
    @State private var selection: String?

    private var rows: [Entry] {
        diff.added.map { Entry(status: "Added", color: .green, before: nil, after: $0) } +
        diff.removed.map { Entry(status: "Removed", color: .red, before: $0, after: nil) } +
        diff.changed.map { Entry(status: "Changed", color: .orange, before: $0.before, after: $0.after) }
    }

    var body: some View {
        Table(rows, selection: $selection) {
            TableColumn("Change") { (row: Entry) in Text(row.status).foregroundStyle(row.color) }
            TableColumn("Entity") { (row: Entry) in Text(row.label) }
            TableColumn("USD Before → After") { (row: Entry) in Text("\(usd(row.before?.balanceUsd)) → \(usd(row.after?.balanceUsd))") }
            TableColumn("Units Before → After") { (row: Entry) in
                Text("\(number(row.before?.balanceUnit)) → \(number(row.after?.balanceUnit))")
            }
            TableColumn("Token Balances") { (row: Entry) in Text(row.tokenChanges) }
        }
        if let row = rows.first(where: { $0.id == selection }) {
            ScrollView {
                HStack(alignment: .top) {
                    Text("Before\n" + jsonText(row.before)).frame(maxWidth: .infinity, alignment: .leading)
                    Text("After\n" + jsonText(row.after)).frame(maxWidth: .infinity, alignment: .leading)
                }.font(.caption.monospaced()).textSelection(.enabled)
            }.frame(height: 135)
        }
    }
}

@MainActor
struct CurvesView: View {
    private struct ChartPoint: Identifiable {
        let date: Date
        let point: BalancePoint
        var id: String { "\(point.day):\(point.entityId)" }
        var label: String { point.entityName.map { "\($0) (\(point.entityId))" } ?? point.entityId }
    }

    let client: APIClient
    @State private var coins: [Coin] = []
    @State private var coin = ""
    @State private var entities = ""
    @State private var start: Date?
    @State private var end: Date?
    @State private var inUSD = false
    @State private var curve: BalanceCurve?
    @State private var action = ActionState()
    private let utc = TimeZone(secondsFromGMT: 0) ?? .current

    private var points: [ChartPoint] {
        guard let curve else { return [] }
        return curve.series.keys.sorted().flatMap { entity in
            (curve.series[entity] ?? []).compactMap { point in
                WireDate.parse(point.day + "T00:00:00Z").map { ChartPoint(date: $0, point: point) }
            }
        }
    }

    private var labels: [String: String] {
        var result: [String: String] = [:]
        for entry in points where entry.point.entityName != nil { result[entry.point.entityId] = entry.label }
        return result
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                CoinPicker(coins: coins, selection: $coin)
                TextField("Entity IDs, comma separated", text: $entities)
                Picker("Measure", selection: $inUSD) {
                    Text("Balance").tag(false)
                    Text("USD").tag(true)
                }.pickerStyle(.segmented).fixedSize()
            }
            HStack {
                DateBound(title: "From (UTC)", date: $start, dayOnly: true)
                DateBound(title: "Through (UTC)", date: $end, dayOnly: true)
                Spacer()
                Button("Load Curve") { action.run { try await load() } }.disabled(action.busy || coin.isEmpty)
            }.environment(\.timeZone, utc)
            Text("Entity IDs can be copied from snapshot details. Dates are UTC collection days.")
                .font(.caption).foregroundStyle(.secondary)
            ActionNotice(action: action)
            if let curve, !points.isEmpty {
                Text("Coin: \(coinLabel(curve.coinId, coins))").font(.headline)
                let seriesLabels = labels
                Chart(points) { entry in
                    let value = inUSD ? entry.point.balanceUsd : entry.point.balanceUnit
                    let label = seriesLabels[entry.point.entityId] ?? entry.point.entityId
                    LineMark(x: .value("Day", entry.date), y: .value(inUSD ? "USD" : "Balance", value),
                             series: .value("Entity ID", entry.point.entityId))
                        .foregroundStyle(by: .value("Entity", label))
                    PointMark(x: .value("Day", entry.date), y: .value(inUSD ? "USD" : "Balance", value))
                        .foregroundStyle(by: .value("Entity", label))
                }.environment(\.timeZone, utc)
                let incompleteDays = curve.days.filter(\.truncated).map(\.day)
                if !incompleteDays.isEmpty {
                    Text("Possibly truncated days (\(incompleteDays.count)): \(incompleteDays.suffix(6).joined(separator: ", "))")
                        .foregroundStyle(.orange)
                }
            } else {
                ContentUnavailableView(curve == nil ? "Choose a Coin and Entity IDs" : "No Balance Data", systemImage: "chart.xyaxis.line")
            }
        }.navigationTitle("Entity Curves")
            .task { await action.load { coins = try await client.coins() } }
    }

    private func load() async throws {
        let ids = entities.csv ?? []
        let startDay = start.map(WireDate.utcDay)
        let endDay = end.map(WireDate.utcDay)
        guard (1...100).contains(ids.count) else { throw InputError("Enter between 1 and 100 entity IDs.") }
        try validateBounds(startDay, endDay, "Day")
        curve = try await client.balances(BalanceFilter(coinId: coin, entityIds: ids, start: startDay, end: endDay))
    }
}
