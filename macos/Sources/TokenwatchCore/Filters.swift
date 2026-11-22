import Foundation

public enum WireDate {
    public static func parse(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let result = formatter.date(from: value) { return result }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    public static func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    public static func utcDay(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.formatOptions = [.withFullDate]
        return formatter.string(from: date)
    }
}

public struct Page: Sendable, Equatable {
    public let limit: Int
    public let offset: Int

    public init(limit: Int = 100, offset: Int = 0) {
        self.limit = min(500, max(1, limit))
        self.offset = max(0, offset)
    }

    public var queryItems: [URLQueryItem] {
        [.init(name: "limit", value: String(limit)), .init(name: "offset", value: String(offset))]
    }
}

private func items(_ values: [(String, String?)]) -> [URLQueryItem] {
    values.compactMap { name, value in
        guard let value, !value.isEmpty else { return nil }
        return URLQueryItem(name: name, value: value)
    }
}

public struct TransferFilter: Sendable, Equatable {
    public var coinId: String?
    public var start: Date?
    public var end: Date?
    public var minUsd: Double?
    public var maxUsd: Double?
    public var direction: Direction?
    public var entity: String?
    public var address: String?
    public var chain: String?
    public var txhash: String?
    public var page = Page()

    public init() {}

    public var queryItems: [URLQueryItem] {
        items([("coin_id", coinId), ("start", start.map(WireDate.timestamp)), ("end", end.map(WireDate.timestamp)),
               ("min_usd", minUsd.map { String($0) }), ("max_usd", maxUsd.map { String($0) }),
               ("direction", direction?.rawValue), ("entity", entity), ("address", address),
               ("chain", chain), ("txhash", txhash)]) + page.queryItems
    }
}

public struct EventFilter: Sendable, Equatable {
    public var coinId: String?
    public var level: Level?
    public var kind: String?
    public var ruleId: Int?
    public var read: Bool?
    public var start: Date?
    public var end: Date?
    public var minUsd: Double?
    public var maxUsd: Double?
    public var page = Page()

    public init() {}

    public var queryItems: [URLQueryItem] {
        items([("coin_id", coinId), ("level", level?.rawValue), ("kind", kind),
               ("rule_id", ruleId.map { String($0) }), ("read", read.map { String($0) }),
               ("start", start.map(WireDate.timestamp)), ("end", end.map(WireDate.timestamp))]) + page.queryItems
    }

    public func matches(_ event: Event) -> Bool {
        if let coinId, coinId != event.coinId { return false }
        if let level, level != event.level { return false }
        if let kind, kind != event.kind { return false }
        if let ruleId, ruleId != event.ruleId { return false }
        if let read, read != (event.readAt != nil) { return false }
        if let start, event.createdAt < start { return false }
        if let end, event.createdAt > end { return false }
        if let minUsd, !(event.usd.map { abs($0) >= minUsd } ?? false) { return false }
        if let maxUsd, !(event.usd.map { abs($0) <= maxUsd } ?? false) { return false }
        return true
    }
}

public struct BalanceFilter: Sendable, Equatable {
    public let coinId: String
    public let entityIds: [String]
    public let start: String?
    public let end: String?

    public init(coinId: String, entityIds: [String], start: String? = nil, end: String? = nil) {
        self.coinId = coinId
        self.entityIds = entityIds
        self.start = start
        self.end = end
    }

    public var queryItems: [URLQueryItem] {
        items([("coin_id", coinId), ("start", start), ("end", end)]) +
            entityIds.map { URLQueryItem(name: "entity_id", value: $0) }
    }
}
