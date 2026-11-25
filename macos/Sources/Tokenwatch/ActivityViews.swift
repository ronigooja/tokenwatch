import SwiftUI
import TokenwatchCore

@MainActor
struct EventsView: View {
    let client: APIClient
    @ObservedObject var model: AppModel
    @State private var draft = EventFilter()
    @State private var applied = EventFilter()
    @State private var coin = ""
    @State private var kind = ""
    @State private var minUSD = ""
    @State private var maxUSD = ""
    @State private var rows: [Event] = []
    @State private var selection: Int?
    @State private var action = ActionState()

    private var events: [Event] {
        var values: [Int: Event] = [:]
        if applied.page.offset == 0 {
            for event in model.live { values[event.id] = event }
            if let id = model.eventFocus?.id, let event = model.recent.first(where: { $0.id == id }) { values[id] = event }
        }
        for event in rows { values[event.id] = event }
        return values.values.map { model.changedEvents[$0.id] ?? $0 }.filter(applied.matches).sorted { $0.id > $1.id }
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                TextField("Coin ID", text: $coin)
                Picker("Level", selection: $draft.level) {
                    Text("All").tag(nil as Level?)
                    ForEach(Level.allCases, id: \.self) { level in Text(level.rawValue).tag(Optional(level)) }
                }
                TextField("Event type", text: $kind)
                TextField("Min USD", text: $minUSD)
                TextField("Max USD", text: $maxUSD)
            }
            HStack {
                DateBound(title: "From", date: $draft.start)
                DateBound(title: "Through", date: $draft.end)
                Spacer()
                Button("Apply Filters") { action.run { try await apply(offset: 0) } }
            }
            .disabled(action.busy)
            Text("USD filters use absolute amounts on loaded events. The live stream contains push-enabled events only.")
                .font(.caption).foregroundStyle(.secondary)
            Text(model.status).font(.caption).foregroundStyle(.secondary)
            ActionNotice(action: action)
            HSplitView {
                Table(events, selection: $selection) {
                    TableColumn("Time") { (event: Event) in Text(event.createdAt, format: .dateTime) }
                    TableColumn("Coin") { (event: Event) in Text(event.coinId.map { model.coinLabel($0) } ?? "—") }
                    TableColumn("Event") { (event: Event) in Text(event.kind) }
                    TableColumn("Level") { (event: Event) in
                        Text(event.level.rawValue).foregroundStyle(event.level == .important ? Color.red : Color.primary)
                    }
                    TableColumn("Read") { (event: Event) in Text(event.readAt == nil ? "Unread" : "Read") }.width(60)
                }.frame(minWidth: 460)
                if let event = events.first(where: { $0.id == selection }) {
                    details(event).frame(minWidth: 220, idealWidth: 260)
                }
            }
            Pager(page: applied.page, count: rows.count, busy: action.busy) { offset in
                action.run {
                    var filter = applied
                    filter.page = Page(offset: offset)
                    try await load(filter)
                    selection = nil
                }
            }
        }
        .navigationTitle("Events")
        .task { await action.load { try await load(applied) } }
        .onChange(of: model.streamState) { _, state in
            if state == .connected, applied.page.offset == 0 { action.run { try await load(applied) } }
        }
        .onChange(of: model.eventFocus, initial: true) { _, focus in
            guard let focus else { return }
            selection = focus.id
            if applied != EventFilter() {
                applied = EventFilter()
                rows = []
                draft = EventFilter()
                coin = ""; kind = ""; minUSD = ""; maxUSD = ""
                action.run { try await load(applied) }
            }
        }
    }

    private func apply(offset: Int) async throws {
        var filter = draft
        filter.coinId = coin.nonblank
        filter.kind = kind.nonblank
        filter.minUsd = try minUSD.nonnegative("Min USD")
        filter.maxUsd = try maxUSD.nonnegative("Max USD")
        try validateBounds(filter.minUsd, filter.maxUsd, "USD")
        try validateBounds(filter.start, filter.end, "Time")
        filter.page = Page(offset: offset)
        try await load(filter)
        selection = nil
    }

    private func load(_ filter: EventFilter) async throws {
        rows = try await client.events(filter)
        applied = filter
        await model.resolveCoins(rows.map(\.coinId))
    }

    private func details(_ event: Event) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(event.kind).font(.headline)
                LabeledContent("Event ID", value: String(event.id))
                LabeledContent("Coin", value: event.coinId.map { model.coinLabel($0) } ?? "—")
                LabeledContent("Rule", value: event.ruleId.map { String($0) } ?? "—")
                LabeledContent("Level", value: event.level.rawValue)
                LabeledContent("Last seen", value: event.lastAt.formatted())
                LabeledContent("Count", value: String(event.count))
                LabeledContent("USD", value: usd(event.usd))
                Button(event.readAt == nil ? "Mark Read" : "Mark Unread") {
                    action.run { try await model.mark(event) }
                }.disabled(action.busy)
                Divider()
                Text(jsonText(event.data)).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
            }.padding(10)
        }
    }
}

@MainActor
struct TransfersView: View {
    let client: APIClient
    @State private var action = ActionState()
    @State private var draft = TransferFilter()
    @State private var applied = TransferFilter()
    @State private var coin = ""
    @State private var entity = ""
    @State private var address = ""
    @State private var chain = ""
    @State private var hash = ""
    @State private var minUSD = ""
    @State private var maxUSD = ""
    @State private var rows: [Transfer] = []
    @State private var coins: [Coin] = []
    @State private var selection: String?

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                TextField("Coin ID", text: $coin)
                TextField("Min USD", text: $minUSD)
                TextField("Max USD", text: $maxUSD)
                Picker("Direction", selection: $draft.direction) {
                    Text("All").tag(nil as Direction?)
                    ForEach(Direction.allCases, id: \.self) { direction in
                        Text(direction.rawValue).tag(Optional(direction))
                    }
                }
            }
            HStack {
                TextField("Entity", text: $entity)
                TextField("Address", text: $address)
                TextField("Chain", text: $chain)
                TextField("Transaction hash", text: $hash)
            }
            HStack {
                DateBound(title: "From", date: $draft.start)
                DateBound(title: "Through", date: $draft.end)
                Spacer()
                Button("Apply Filters") { action.run { try await apply() } }.disabled(action.busy)
            }
            ActionNotice(action: action)
            transferTable
            if let row = rows.first(where: { $0.id == selection }) {
                Text(row.txhash ?? "No transaction hash").font(.caption.monospaced()).textSelection(.enabled)
            }
            Pager(page: applied.page, count: rows.count, busy: action.busy) { offset in
                action.run {
                    var filter = applied
                    filter.page = Page(offset: offset)
                    try await load(filter)
                }
            }
        }.navigationTitle("Transfer History")
            .task { await action.load { try await load(applied) } }
            .task { coins = (try? await client.coins()) ?? [] }
    }

    private var transferTable: some View {
        Table(rows, selection: $selection) {
            TableColumn("Time") { (row: Transfer) in Text(row.time, format: .dateTime) }.width(min: 110, ideal: 125)
            TableColumn("Coin") { (row: Transfer) in Text(row.coinIds.map { coinLabel($0, coins) }.joined(separator: ", ")) }
            TableColumn("Sender") { (row: Transfer) in party(row.fromEntity, row.fromAddress) }
            TableColumn("Recipient") { (row: Transfer) in party(row.toEntity, row.toAddress) }
            TableColumn("Direction") { (row: Transfer) in Text(row.direction.rawValue) }
            TableColumn("USD") { (row: Transfer) in Text(usd(row.usd)) }
            TableColumn("Chain") { (row: Transfer) in Text(row.chain ?? "—") }
            TableColumn("Tx Hash") { (row: Transfer) in Text(row.txhash ?? "—").truncationMode(.middle) }
            TableColumn("Gap") { (row: Transfer) in
                if row.gap { Label("Gap", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
            }.width(55)
        }
    }

    private func party(_ entity: String?, _ address: String?) -> some View {
        Text(entity ?? address ?? "—").truncationMode(.middle).help([entity, address].compactMap { $0 }.joined(separator: " · "))
    }

    private func apply() async throws {
        var filter = draft
        filter.coinId = coin.nonblank
        filter.entity = entity.nonblank
        filter.address = address.nonblank
        filter.chain = chain.nonblank
        filter.txhash = hash.nonblank
        filter.minUsd = try minUSD.nonnegative("Min USD")
        filter.maxUsd = try maxUSD.nonnegative("Max USD")
        try validateBounds(filter.minUsd, filter.maxUsd, "USD")
        try validateBounds(filter.start, filter.end, "Time")
        try await load(filter)
    }

    private func load(_ filter: TransferFilter) async throws {
        rows = try await client.transfers(filter)
        applied = filter
        selection = nil
    }
}
