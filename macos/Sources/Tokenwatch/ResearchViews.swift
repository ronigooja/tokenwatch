import Charts
import SwiftUI
import TokenwatchCore

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
