import Foundation

/// Filters and orders the automatic snapshot index. The opaque coin ID is only a sort tie-breaker and is never shown.
public enum SnapshotCoinIndex {
    public static func matching(_ coins: [SnapshotCoin], query: String) -> [SnapshotCoin] {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return coins
            .filter { text.isEmpty || $0.symbol.lowercased().contains(text) }
            .sorted { ($0.symbol.lowercased(), $0.id) < ($1.symbol.lowercased(), $1.id) }
    }
}

/// Aligns several historical snapshots for side-by-side comparison. Pure data interpretation; performs no requests.
///
/// Columns are chronological (ties broken by snapshot ID). Rows are the union of entities returned by any selected
/// snapshot, and unchanged rows are kept. An entity missing from a snapshot is `.notReturned`, never a zero amount.
public struct SnapshotComparison: Sendable, Equatable {
    public struct Column: Sendable, Equatable, Identifiable {
        /// Internal snapshot ID. Used for identity only; never displayed.
        public let id: Int
        public let createdAt: Date
        public let interval: Interval
        public let balanceMin: Double?
        public let truncated: Bool
    }

    public enum Cell: Sendable, Equatable {
        /// The entity was not returned by this snapshot. This does not mean a zero balance.
        case notReturned
        /// The entity was returned. A nil amount means the server did not report that amount.
        case returned(units: Double?, usd: Double?)
    }

    public struct Row: Sendable, Equatable, Identifiable {
        /// Internal entity ID. Used for identity only; never displayed.
        public let entityId: String
        public let name: String?
        public let type: String?
        /// Aligned with `SnapshotComparison.columns`.
        public let cells: [Cell]
        public var id: String { entityId }
    }

    public let columns: [Column]
    public let rows: [Row]

    public var intervalsDiffer: Bool { Set(columns.map(\.interval)).count > 1 }
    public var minimumsDiffer: Bool { Set(columns.map(\.balanceMin)).count > 1 }
    public var hasTruncatedColumn: Bool { columns.contains(where: \.truncated) }

    public init(_ details: [SnapshotDetail]) {
        let sorted = details.sorted { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) }
        columns = sorted.map {
            Column(id: $0.id, createdAt: $0.createdAt, interval: $0.interval, balanceMin: $0.balanceMin,
                   truncated: $0.truncated)
        }

        // Within one snapshot the first occurrence of an entity wins; later duplicates are ignored.
        var perColumn: [[String: SnapshotRow]] = []
        var labels: [String: (name: String?, type: String?)] = [:]
        for detail in sorted {
            var byEntity: [String: SnapshotRow] = [:]
            for row in detail.rows where byEntity[row.entityId] == nil {
                byEntity[row.entityId] = row
            }
            perColumn.append(byEntity)
            // Labels come from the latest chronological snapshot that reports them.
            for row in detail.rows {
                let current = labels[row.entityId]
                labels[row.entityId] = (row.entityName ?? current?.name, row.entityType ?? current?.type)
            }
        }

        let entityIDs = Set(perColumn.flatMap(\.keys))
        rows = entityIDs.map { entityId in
            let label = labels[entityId]
            let cells: [Cell] = perColumn.map { byEntity in
                guard let row = byEntity[entityId] else { return .notReturned }
                return .returned(units: row.balanceUnit, usd: row.balanceUsd)
            }
            return Row(entityId: entityId, name: label?.name, type: label?.type, cells: cells)
        }
        .sorted { Self.sortKey($0) < Self.sortKey($1) }
    }

    /// Named entities first, alphabetical by name (case-insensitive), then by internal ID as a stable tie-breaker.
    private static func sortKey(_ row: Row) -> (Int, String, String) {
        (row.name == nil ? 1 : 0, row.name?.lowercased() ?? "", row.entityId)
    }
}

/// Runs `work` for each input with at most `limit` requests in flight. Results keep the input order.
public enum BoundedConcurrency {
    public static func map<Input: Sendable, Output: Sendable>(
        _ inputs: [Input], limit: Int, _ work: @escaping @Sendable (Input) async throws -> Output
    ) async throws -> [Output] {
        precondition(limit > 0, "limit must be positive")
        var results = [Output?](repeating: nil, count: inputs.count)
        try await withThrowingTaskGroup(of: (Int, Output).self) { group in
            var next = 0
            while next < min(limit, inputs.count) {
                let index = next
                let input = inputs[index]
                group.addTask { (index, try await work(input)) }
                next += 1
            }
            for try await (index, output) in group {
                results[index] = output
                if next < inputs.count {
                    let index = next
                    let input = inputs[index]
                    group.addTask { (index, try await work(input)) }
                    next += 1
                }
            }
        }
        return results.compactMap { $0 }
    }
}
