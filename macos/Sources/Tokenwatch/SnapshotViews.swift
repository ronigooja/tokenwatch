import SwiftUI
import TokenwatchCore

/// Coin-first workspace for automatic snapshots. Opaque pricing IDs, chains, sources, and raw token JSON are never shown.
///
/// A screen changes only after its data has loaded, inside the same `ActionState` run that fetched it. Navigation
/// controls are disabled while any request runs, so a click can never open a screen that will not load.
@MainActor
struct SnapshotsView: View {
    private enum Screen: Equatable {
        case index
        case history(SnapshotCoin)
        case detail(Int, SnapshotCoin)
        case compare(SnapshotCoin)
    }

    let client: APIClient
    @State private var action = ActionState()
    @State private var screen = Screen.index
    @State private var coins: [SnapshotCoin] = []
    @State private var query = ""
    /// Row selection in the coin index. Used only to detect a row click; reset as soon as it is handled.
    @State private var selectedCoinID: String?
    @State private var history: [Snapshot] = []
    @State private var historyPage = Page()
    /// Kept across history pages. Cleared when a different coin is opened.
    @State private var selected: [Int: Snapshot] = [:]
    @State private var selectionCoinID: String?
    @State private var detail: SnapshotDetail?
    @State private var comparison: SnapshotComparison?

    var body: some View {
        VStack(alignment: .leading) {
            if screen != .index {
                HStack {
                    Button { goBack() } label: { Label("Back", systemImage: "chevron.left") }
                        .disabled(action.busy)
                    Spacer()
                }
            }
            // The only progress indicator on this page.
            ActionNotice(action: action)
            content
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .navigationTitle("Snapshots")
        .task { await action.load { try await loadIndex() } }
    }

    @ViewBuilder private var content: some View {
        switch screen {
        case .index: indexView
        case .history(let coin): historyView(coin)
        case .detail: detailContent
        case .compare: compareContent
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
                Button {
                    action.run { coins = try await client.snapshotCoins() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")
                .accessibilityLabel("Refresh")
                .disabled(action.busy)
            }
            if coins.isEmpty {
                if !action.busy { compactEmptyState("No coins are eligible for automatic snapshots yet.") }
            } else if visible.isEmpty {
                compactEmptyState("No coins match “\(query)”.")
            } else {
                Table(visible, selection: $selectedCoinID) {
                    TableColumn("Coin") { (coin: SnapshotCoin) in Text(coin.symbol) }
                    TableColumn("Last Captured") { (coin: SnapshotCoin) in
                        if let date = coin.lastCapturedAt {
                            Text(date.formatted(date: .abbreviated, time: .shortened))
                        } else {
                            Text("Not captured yet").foregroundStyle(.secondary)
                        }
                    }
                    TableColumn("Snapshots") { (coin: SnapshotCoin) in Text(coin.snapshotCount, format: .number) }
                        .width(90)
                }
                .disabled(action.busy)
                .onChange(of: selectedCoinID) { _, id in
                    // A row click opens that coin's history. The selection is cleared at once so the same row can be clicked again.
                    guard let id, let coin = visible.first(where: { $0.id == id }) else { selectedCoinID = nil; return }
                    selectedCoinID = nil
                    openHistory(coin)
                }
            }
        }
        .searchable(text: $query, prompt: "Search by symbol")
    }

    /// Top-aligned, single-line empty state. Replaces the full-height unavailable view so it does not leave blank space.
    private func compactEmptyState(_ text: String) -> some View {
        Text(text).foregroundStyle(.secondary).padding(.top, 4)
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
            if history.isEmpty {
                if !action.busy { compactEmptyState("No snapshots on this page.") }
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
                    TableColumn("Lookback") { (entry: Snapshot) in Text(entry.lookback.rawValue) }
                    TableColumn("Minimum") { (entry: Snapshot) in Text(minimumText(entry.balanceMin)) }
                    TableColumn("Coverage") { (entry: Snapshot) in Text(coverageText(entry.truncated)) }
                    TableColumn("") { (entry: Snapshot) in
                        Button("Details") { openDetail(entry.id, coin) }
                            .buttonStyle(.link).disabled(action.busy)
                    }.width(70)
                }
                .disabled(action.busy)
            }
        }
    }

    // MARK: Entity details and comparison

    @ViewBuilder private var detailContent: some View {
        if let detail { SnapshotDetailView(detail: detail) }
    }

    @ViewBuilder private var compareContent: some View {
        if let comparison { SnapshotCompareView(comparison: comparison) }
    }

    // MARK: Navigation and loading
    //
    // Each navigation fetches first and changes `screen` last, so a failed load leaves the current screen in place.

    private func openHistory(_ coin: SnapshotCoin) {
        action.run {
            let rows = try await client.snapshots(coinID: coin.id, page: Page())
            if selectionCoinID != coin.id { selected = [:]; selectionCoinID = coin.id }
            history = rows
            historyPage = Page()
            screen = .history(coin)
        }
    }

    private func openDetail(_ id: Int, _ coin: SnapshotCoin) {
        action.run {
            let loaded = try await client.snapshot(id)
            detail = loaded
            screen = .detail(id, coin)
        }
    }

    private func startCompare(_ coin: SnapshotCoin) {
        let ids = selected.keys.sorted()
        let client = client
        action.run {
            // Details are the only comparison source. At most four requests run at once.
            let details = try await BoundedConcurrency.map(ids, limit: 4) { id in try await client.snapshot(id) }
            comparison = SnapshotComparison(details)
            screen = .compare(coin)
        }
    }

    private func goBack() {
        guard !action.busy else { return }
        switch screen {
        case .index: break
        case .history: screen = .index
        case .detail(_, let coin):
            detail = nil
            screen = .history(coin)
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
}

@MainActor
private struct SnapshotDetailView: View {
    let detail: SnapshotDetail
    @State private var selection: String?

    var body: some View {
        VStack(alignment: .leading) {
            Text("Captured \(detail.createdAt.formatted(date: .abbreviated, time: .shortened))").font(.headline)
            Text("Lookback \(detail.lookback.rawValue) · \(minimumText(detail.balanceMin))")
                .foregroundStyle(.secondary)
            if detail.truncated {
                Label("Snapshot may be truncated; some entities may be missing.", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
            Text("Lists the entities the server returned for this snapshot. Dashes mean the server did not report a value.")
                .font(.caption).foregroundStyle(.secondary)
            Table(detail.rows, selection: $selection) {
                TableColumn("Entity") { (row: SnapshotRow) in
                    EntityLabel(name: row.entityName, type: nil, entityId: row.entityId, showID: true)
                }
                TableColumn("Type") { (row: SnapshotRow) in Text(row.entityType ?? "—") }
                TableColumn("Units") { (row: SnapshotRow) in Text(number(row.balanceUnit)) }
                TableColumn("USD") { (row: SnapshotRow) in Text(usd(row.balanceUsd)) }
                TableColumn("Previous Units") { (row: SnapshotRow) in Text(number(row.prevBalanceUnit)) }
                TableColumn("Previous USD") { (row: SnapshotRow) in Text(usd(row.prevBalanceUsd)) }
            }
            // Selecting a row exposes its entity ID as selectable text for copying.
            if let row = detail.rows.first(where: { $0.id == selection }) {
                HStack {
                    Text("Entity ID").foregroundStyle(.secondary)
                    Text(row.entityId).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
        }
    }
}

@MainActor
private struct SnapshotCompareView: View {
    let comparison: SnapshotComparison

    /// Names used by more than one entity. Those rows also show their entity ID so they stay distinguishable.
    private var ambiguousNames: Set<String> {
        let names = comparison.rows.compactMap(\.name)
        return Set(names.filter { name in names.lazy.filter { $0 == name }.count > 1 })
    }

    var body: some View {
        VStack(alignment: .leading) {
            Text("Comparing \(comparison.columns.count) snapshots, oldest first").font(.headline)
            if comparison.lookbacksDiffer {
                Label("Lookbacks differ between the selected snapshots.", systemImage: "info.circle").foregroundStyle(.orange)
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
                            Text("Lookback").foregroundStyle(.secondary)
                            ForEach(comparison.columns) { column in
                                Text(column.lookback.rawValue).frame(maxWidth: .infinity, alignment: .trailing)
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
                                EntityLabel(name: row.name, type: row.type, entityId: row.entityId,
                                            showID: row.name == nil || ambiguousNames.contains(row.name ?? ""))
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

/// Entity label. Unnamed entities say so and show their ID, so they remain distinguishable from one another.
@MainActor
private struct EntityLabel: View {
    let name: String?
    let type: String?
    let entityId: String
    let showID: Bool

    var body: some View {
        VStack(alignment: .leading) {
            Text(name ?? "Unnamed entity").foregroundStyle(name == nil ? .secondary : .primary)
            if let type { Text(type).font(.caption).foregroundStyle(.secondary) }
            if showID {
                Text(entityId).font(.caption.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
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
