import SwiftUI
import TokenwatchCore

/// Coin-first workspace for automatic snapshots. Opaque pricing IDs, chains, sources, and raw token JSON are never shown.
@MainActor
struct SnapshotsView: View {
    private enum Origin: Equatable { case history(SnapshotCoin), earlier }
    private enum Screen: Equatable {
        case index
        case history(SnapshotCoin)
        case earlier
        case detail(Int, Origin)
        case compare(SnapshotCoin)
    }

    let client: APIClient
    @State private var action = ActionState()
    @State private var screen = Screen.index
    @State private var coins: [SnapshotCoin] = []
    @State private var query = ""
    @State private var history: [Snapshot] = []
    @State private var historyPage = Page()
    @State private var earlier: [Snapshot] = []
    @State private var earlierPage = Page()
    /// Kept across history pages. Keyed by snapshot ID; cleared when a different coin is opened.
    @State private var selected: [Int: Snapshot] = [:]
    @State private var selectionCoinID: String?
    @State private var detail: SnapshotDetail?
    @State private var comparison: SnapshotComparison?

    var body: some View {
        VStack(alignment: .leading) {
            if screen != .index {
                HStack {
                    Button { goBack() } label: { Label("Back", systemImage: "chevron.left") }
                    Spacer()
                }
            }
            ActionNotice(action: action)
            content
        }
        .navigationTitle("Snapshots")
        .task { await action.load { try await loadIndex() } }
    }

    @ViewBuilder private var content: some View {
        switch screen {
        case .index: indexView
        case .history(let coin): historyView(coin)
        case .earlier: earlierView
        case .detail: detailContent
        case .compare(let coin): compareContent(coin)
        }
    }

    // MARK: Home

    private var indexView: some View {
        let visible = SnapshotCoinIndex.matching(coins, query: query)
        return VStack(alignment: .leading) {
            HStack {
                Text("Snapshots are captured automatically. Each coin lists its captures.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Earlier Snapshots") { openEarlier() }
                Button("Refresh") { action.run { try await loadIndex() } }
            }
            if coins.isEmpty {
                ContentUnavailableView("No Snapshots Yet", systemImage: "camera",
                                       description: Text("Coins appear here after their first automatic capture."))
            } else if visible.isEmpty {
                ContentUnavailableView.search(text: query)
            } else {
                Table(visible) {
                    TableColumn("Coin") { (coin: SnapshotCoin) in
                        Button(coin.symbol) { openHistory(coin) }.buttonStyle(.link)
                    }
                    TableColumn("Last Captured") { (coin: SnapshotCoin) in
                        Text(coin.lastCapturedAt.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? "—")
                    }
                    TableColumn("Snapshots") { (coin: SnapshotCoin) in Text(coin.snapshotCount, format: .number) }
                        .width(90)
                }
            }
        }
        .searchable(text: $query, prompt: "Search by symbol")
    }

    // MARK: Per-coin history

    private func historyView(_ coin: SnapshotCoin) -> some View {
        VStack(alignment: .leading) {
            HStack {
                Text(coin.symbol).font(.title2.bold())
                Spacer()
                Text("\(selected.count) selected").foregroundStyle(.secondary).monospacedDigit()
                Button("Compare") { startCompare(coin) }
                    .disabled(selected.count < 2 || action.busy)
            }
            Pager(page: historyPage, count: history.count, busy: action.busy) { offset in
                action.run { try await loadHistory(coin, Page(offset: offset)) }
            }
            Text("Select at least two snapshots to compare. Selections are kept across pages.")
                .font(.caption).foregroundStyle(.secondary)
            if history.isEmpty && !action.busy {
                ContentUnavailableView("No Snapshots on This Page", systemImage: "camera")
            } else {
                Table(history) {
                    TableColumn("Compare") { (entry: Snapshot) in
                        Toggle("Select", isOn: Binding(
                            get: { selected[entry.id] != nil },
                            set: { on in selected[entry.id] = on ? entry : nil }
                        )).labelsHidden()
                    }.width(60)
                    TableColumn("Captured") { (entry: Snapshot) in
                        Text(entry.createdAt.formatted(date: .abbreviated, time: .shortened))
                    }
                    TableColumn("Interval") { (entry: Snapshot) in Text(entry.interval.rawValue) }
                    TableColumn("Minimum") { (entry: Snapshot) in Text(minimumText(entry.balanceMin)) }
                    TableColumn("Coverage") { (entry: Snapshot) in Text(coverageText(entry.truncated)) }
                    TableColumn("") { (entry: Snapshot) in
                        Button("Details") { openDetail(entry.id, from: .history(coin)) }.buttonStyle(.link)
                    }.width(70)
                }
            }
        }
    }

    // MARK: Earlier snapshots (read-only, not part of any coin history)

    private var earlierView: some View {
        VStack(alignment: .leading) {
            Text("Earlier Snapshots").font(.title2.bold())
            Text("Records captured before per-coin tracking. Read-only; they are not included in coin histories or comparisons.")
                .font(.caption).foregroundStyle(.secondary)
            Pager(page: earlierPage, count: earlier.count, busy: action.busy) { offset in
                action.run { try await loadEarlier(Page(offset: offset)) }
            }
            if earlier.isEmpty && !action.busy {
                ContentUnavailableView("No Earlier Snapshots", systemImage: "tray")
            } else {
                Table(earlier) {
                    TableColumn("Captured") { (entry: Snapshot) in
                        Text(entry.createdAt.formatted(date: .abbreviated, time: .shortened))
                    }
                    TableColumn("Interval") { (entry: Snapshot) in Text(entry.interval.rawValue) }
                    TableColumn("Minimum") { (entry: Snapshot) in Text(minimumText(entry.balanceMin)) }
                    TableColumn("Coverage") { (entry: Snapshot) in Text(coverageText(entry.truncated)) }
                    TableColumn("") { (entry: Snapshot) in
                        Button("Details") { openDetail(entry.id, from: .earlier) }.buttonStyle(.link)
                    }.width(70)
                }
            }
        }
    }

    // MARK: Entity details

    @ViewBuilder private var detailContent: some View {
        if let detail {
            SnapshotDetailView(detail: detail)
        } else if action.busy {
            ProgressView()
        } else {
            ContentUnavailableView("Details Unavailable", systemImage: "exclamationmark.triangle")
        }
    }

    @ViewBuilder private func compareContent(_ coin: SnapshotCoin) -> some View {
        if let comparison {
            SnapshotCompareView(comparison: comparison)
        } else {
            ContentUnavailableView("Comparison Unavailable", systemImage: "exclamationmark.triangle")
        }
    }

    // MARK: Navigation and loading

    private func openHistory(_ coin: SnapshotCoin) {
        if selectionCoinID != coin.id { selected = [:]; selectionCoinID = coin.id }
        historyPage = Page()
        history = []
        screen = .history(coin)
        action.run { try await loadHistory(coin, Page()) }
    }

    private func openEarlier() {
        screen = .earlier
        action.run { try await loadEarlier(Page()) }
    }

    private func openDetail(_ id: Int, from origin: Origin) {
        detail = nil
        screen = .detail(id, origin)
        action.run { detail = try await client.snapshot(id) }
    }

    private func startCompare(_ coin: SnapshotCoin) {
        let ids = selected.keys.sorted()
        let client = client
        action.run {
            // Details are the only source for comparison; at most four requests run at once.
            let details = try await BoundedConcurrency.map(ids, limit: 4) { id in try await client.snapshot(id) }
            comparison = SnapshotComparison(details)
            screen = .compare(coin)
        }
    }

    private func goBack() {
        switch screen {
        case .index: break
        case .history, .earlier: screen = .index
        case .detail(_, let origin):
            detail = nil
            switch origin {
            case .history(let coin): screen = .history(coin)
            case .earlier: screen = .earlier
            }
        case .compare(let coin):
            comparison = nil
            screen = .history(coin)
        }
    }

    private func loadIndex() async throws {
        coins = try await client.snapshotCoins()
    }

    private func loadHistory(_ coin: SnapshotCoin, _ newPage: Page) async throws {
        history = try await client.snapshots(coinID: coin.id, page: newPage)
        historyPage = newPage
    }

    private func loadEarlier(_ newPage: Page) async throws {
        earlier = try await client.legacySnapshots(page: newPage)
        earlierPage = newPage
    }
}

@MainActor
private struct SnapshotDetailView: View {
    let detail: SnapshotDetail

    var body: some View {
        VStack(alignment: .leading) {
            Text("Captured \(detail.createdAt.formatted(date: .abbreviated, time: .shortened))").font(.headline)
            Text("Interval \(detail.interval.rawValue) · \(minimumText(detail.balanceMin))")
                .foregroundStyle(.secondary)
            if detail.truncated {
                Label("Snapshot may be truncated; some entities may be missing.", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
            Text("Lists the entities the server returned for this snapshot. Dashes mean the server did not report a value.")
                .font(.caption).foregroundStyle(.secondary)
            Table(detail.rows) {
                TableColumn("Entity") { (row: SnapshotRow) in
                    Text(row.entityName ?? "Unnamed entity").foregroundStyle(row.entityName == nil ? .secondary : .primary)
                }
                TableColumn("Type") { (row: SnapshotRow) in Text(row.entityType ?? "—") }
                TableColumn("Units") { (row: SnapshotRow) in Text(number(row.balanceUnit)) }
                TableColumn("USD") { (row: SnapshotRow) in Text(usd(row.balanceUsd)) }
                TableColumn("Previous Units") { (row: SnapshotRow) in Text(number(row.prevBalanceUnit)) }
                TableColumn("Previous USD") { (row: SnapshotRow) in Text(usd(row.prevBalanceUsd)) }
            }
        }
    }
}

@MainActor
private struct SnapshotCompareView: View {
    let comparison: SnapshotComparison

    var body: some View {
        VStack(alignment: .leading) {
            Text("Comparing \(comparison.columns.count) snapshots, oldest first").font(.headline)
            if comparison.intervalsDiffer {
                Label("Intervals differ between the selected snapshots.", systemImage: "info.circle").foregroundStyle(.orange)
            }
            if comparison.minimumsDiffer {
                Label("Minimums differ, so the same entity may appear in one snapshot and not another.",
                      systemImage: "info.circle").foregroundStyle(.orange)
            }
            if comparison.hasTruncatedColumn {
                Label("Some selected snapshots may be truncated. Entities marked Not in snapshot may be missing from them.",
                      systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
            Text("Not in snapshot: the entity was not returned by that snapshot; this is not a zero balance. Units/USD not reported: the server sent no value.")
                .font(.caption).foregroundStyle(.secondary)
            if comparison.rows.isEmpty {
                ContentUnavailableView("No Entities", systemImage: "tray",
                                       description: Text("The selected snapshots returned no entities."))
            } else {
                ScrollView([.horizontal, .vertical]) {
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                        GridRow {
                            Text("Entity").bold()
                            ForEach(comparison.columns) { column in
                                Text(column.createdAt.formatted(date: .abbreviated, time: .shortened)).bold()
                                    .frame(maxWidth: .infinity, alignment: .trailing)
                            }
                        }
                        GridRow {
                            Text("Interval").foregroundStyle(.secondary)
                            ForEach(comparison.columns) { column in
                                Text(column.interval.rawValue).frame(maxWidth: .infinity, alignment: .trailing)
                            }
                        }
                        GridRow {
                            Text("Minimum").foregroundStyle(.secondary)
                            ForEach(comparison.columns) { column in
                                Text(minimumText(column.balanceMin)).frame(maxWidth: .infinity, alignment: .trailing)
                            }
                        }
                        GridRow {
                            Text("Coverage").foregroundStyle(.secondary)
                            ForEach(comparison.columns) { column in
                                Text(coverageText(column.truncated)).frame(maxWidth: .infinity, alignment: .trailing)
                            }
                        }
                        ForEach(comparison.rows) { row in
                            GridRow {
                                VStack(alignment: .leading) {
                                    Text(row.name ?? "Unnamed entity")
                                        .foregroundStyle(row.name == nil ? .secondary : .primary)
                                    if let type = row.type { Text(type).font(.caption).foregroundStyle(.secondary) }
                                }
                                ForEach(Array(row.cells.enumerated()), id: \.offset) { _, cell in
                                    CompareCell(cell: cell).frame(maxWidth: .infinity, alignment: .trailing)
                                }
                            }
                        }
                    }
                    .monospacedDigit()
                    .padding(.trailing)
                }
            }
        }
    }
}

@MainActor
private struct CompareCell: View {
    let cell: SnapshotComparison.Cell

    var body: some View {
        switch cell {
        case .notReturned:
            Text("Not in snapshot").foregroundStyle(.secondary)
        case .returned(let units, let dollars):
            VStack(alignment: .trailing) {
                Text(units == nil ? "Units not reported" : number(units))
                    .foregroundStyle(units == nil ? .orange : .primary)
                Text(dollars == nil ? "USD not reported" : usd(dollars))
                    .font(.caption).foregroundStyle(dollars == nil ? .orange : .secondary)
            }
        }
    }
}

private func minimumText(_ value: Double?) -> String {
    value.map { "Minimum \(usd($0))" } ?? "Minimum not recorded"
}

private func coverageText(_ truncated: Bool) -> String {
    truncated ? "Possibly truncated" : "Not reported as truncated"
}
