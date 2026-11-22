import Foundation

public enum JSONValue: Codable, Equatable, Sendable {
    case null, bool(Bool), integer(Int64), number(Double), string(String)
    case array([JSONValue]), object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let item = try? value.decode(Bool.self) { self = .bool(item) }
        else if let item = try? value.decode(Int64.self) { self = .integer(item) }
        else if let item = try? value.decode(Double.self) { self = .number(item) }
        else if let item = try? value.decode(String.self) { self = .string(item) }
        else if let item = try? value.decode([JSONValue].self) { self = .array(item) }
        else { self = .object(try value.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .null: try value.encodeNil()
        case .bool(let item): try value.encode(item)
        case .integer(let item): try value.encode(item)
        case .number(let item): try value.encode(item)
        case .string(let item): try value.encode(item)
        case .array(let item): try value.encode(item)
        case .object(let item): try value.encode(item)
        }
    }

    public var double: Double? {
        switch self {
        case .integer(let item): return Double(item)
        case .number(let item): return item
        default: return nil
        }
    }
}

public enum Level: String, Codable, Sendable, CaseIterable { case important, normal, log }
public enum Interval: String, Codable, Sendable, CaseIterable {
    case week = "7d", fortnight = "14d", month = "30d"
}
public enum Direction: String, Codable, Sendable, CaseIterable {
    case inbound, outbound, `internal`, entityToEntity = "entity_to_entity", unknown
}
public enum RuleDirection: String, Codable, Sendable, CaseIterable {
    case inbound, outbound, `internal`, entityToEntity = "entity_to_entity", unknown, increase, decrease
    public var isBalance: Bool { self == .increase || self == .decrease }
}
public enum JobKind: String, Codable, Sendable, CaseIterable {
    case transfers, daily, coinCatalog = "coin_catalog"
}
public enum RuleSource: String, Codable, Sendable, CaseIterable { case transfer, balanceDiff = "balance_diff" }

public struct Coin: Codable, Sendable, Identifiable {
    public let id: String
    public let symbol: String
    public let pricingId: String?
    public let verified: Bool
    public let exclusionReason: String?
    public let sources: [[String: JSONValue]]
}

public struct Event: Codable, Sendable, Identifiable {
    public let id: Int
    public let kind: String
    public let ruleId: Int?
    public let coinId: String?
    public let level: Level
    public let push: Bool
    public let createdAt: Date
    public let lastAt: Date
    public let count: Int
    public let readAt: Date?
    public let data: [String: JSONValue]
    public var usd: Double? { data["usd"]?.double }
}

public struct Transfer: Codable, Sendable, Identifiable {
    public let id: String
    public let coinIds: [String]
    public let txhash: String?
    public let chain: String?
    public let fromAddress: String?
    public let toAddress: String?
    public let fromEntity: String?
    public let toEntity: String?
    public let direction: Direction
    public let usd: Double?
    public let time: Date
    public let gap: Bool
}

public struct BalancePoint: Codable, Sendable {
    public let day: String
    public let entityId: String
    public let entityName: String?
    public let entityType: String?
    public let balanceUnit: Double
    public let balanceUsd: Double
}

public struct BalanceDay: Codable, Sendable {
    public let day: String
    public let truncated: Bool
}

public struct BalanceCurve: Codable, Sendable {
    public let coinId: String
    public let days: [BalanceDay]
    public let series: [String: [BalancePoint]]
}

public struct SnapshotInput: Codable, Sendable {
    public let accountId: String
    public let interval: Interval
    public let balanceMin: Double?
    public let pricingIds: [String]?

    public init(accountId: String, interval: Interval, balanceMin: Double?, pricingIds: [String]?) {
        self.accountId = accountId
        self.interval = interval
        self.balanceMin = balanceMin
        self.pricingIds = pricingIds
    }
}

public struct Snapshot: Codable, Sendable, Identifiable {
    public let id: Int
    public let createdAt: Date
    public let interval: Interval
    public let balanceMin: Double?
    public let pricingIds: [String]?
    public let truncated: Bool
}

public struct SnapshotRow: Codable, Sendable, Identifiable {
    public var id: String { entityId }
    public let entityId: String
    public let entityName: String?
    public let entityType: String?
    public let balanceUnit: Double?
    public let balanceUsd: Double?
    public let prevBalanceUnit: Double?
    public let prevBalanceUsd: Double?
    public let tokenBalances: JSONValue?
}

public struct SnapshotDetail: Codable, Sendable, Identifiable {
    public let id: Int
    public let createdAt: Date
    public let interval: Interval
    public let balanceMin: Double?
    public let pricingIds: [String]?
    public let truncated: Bool
    public let rows: [SnapshotRow]
}

public struct SnapshotDiff: Codable, Sendable {
    public struct Change: Codable, Sendable {
        public let before: SnapshotRow
        public let after: SnapshotRow
    }

    public let added: [SnapshotRow]
    public let removed: [SnapshotRow]
    public let changed: [Change]
}

public struct Subscription: Codable, Sendable, Identifiable {
    public var id: String { coinId }
    public let coinId: String
    public let enabled: Bool
    struct Input: Encodable { let enabled: Bool }
}

public struct Exclusion: Codable, Sendable, Identifiable {
    public var id: String { coinId }
    public let coinId: String
    public let reason: String
    struct Input: Encodable { let reason: String }
}

public struct Account: Codable, Sendable, Identifiable {
    public enum Status: String, Codable, Sendable { case active, paused }
    public let id: String
    public let proxyRef: String
    public let status: Status
    public let pauseReason: String?
    public let availableAt: Double
    public let failures: Int
}

public struct AccountSession: Codable, Sendable {
    public enum Status: String, Codable, Sendable {
        case loggedIn = "logged_in", loggedOut = "logged_out", unknown
    }
    public let status: Status
}

public struct JobOptions: Codable, Sendable {
    public let balanceMin: Double?
    public init(balanceMin: Double?) { self.balanceMin = balanceMin }
}

public struct JobInput: Codable, Sendable {
    public let kind: JobKind
    public let accountId: String
    public let coinId: String?
    public let options: JobOptions
    public let intervalSeconds: Double
    public let enabled: Bool

    public init(kind: JobKind, accountId: String, coinId: String?, options: JobOptions,
                intervalSeconds: Double, enabled: Bool) {
        self.kind = kind
        self.accountId = accountId
        self.coinId = coinId
        self.options = options
        self.intervalSeconds = intervalSeconds
        self.enabled = enabled
    }
}

public struct Job: Codable, Sendable, Identifiable {
    public let id: Int
    public let kind: JobKind
    public let accountId: String
    public let coinId: String?
    public let options: JobOptions
    public let intervalSeconds: Double
    public let enabled: Bool
    public let nextRun: Double
}

public struct RuleConditions: Codable, Sendable {
    public let coinsInclude: [String]?
    public let coinsExclude: [String]?
    public let entitiesInclude: [String]?
    public let entitiesExclude: [String]?
    public let direction: RuleDirection?
    public let minUsd: Double?
    public let minBalanceChangePct: Double?

    public init(coinsInclude: [String]?, coinsExclude: [String]?, entitiesInclude: [String]?,
                entitiesExclude: [String]?, direction: RuleDirection?, minUsd: Double?, minBalanceChangePct: Double?) {
        self.coinsInclude = coinsInclude
        self.coinsExclude = coinsExclude
        self.entitiesInclude = entitiesInclude
        self.entitiesExclude = entitiesExclude
        self.direction = direction
        self.minUsd = minUsd
        self.minBalanceChangePct = minBalanceChangePct
    }
}

public struct RuleInput: Codable, Sendable {
    public let name: String
    public let enabled: Bool
    public let source: RuleSource
    public let conditions: RuleConditions
    public let level: Level
    public let push: Bool
    public let cooldownSeconds: Double?
    public let mergeSeconds: Double?

    public init(name: String, enabled: Bool, source: RuleSource, conditions: RuleConditions, level: Level,
                push: Bool, cooldownSeconds: Double?, mergeSeconds: Double?) {
        self.name = name
        self.enabled = enabled
        self.source = source
        self.conditions = conditions
        self.level = level
        self.push = push
        self.cooldownSeconds = cooldownSeconds
        self.mergeSeconds = mergeSeconds
    }
}

public struct Rule: Codable, Sendable, Identifiable {
    public let id: Int
    public let name: String
    public let enabled: Bool
    public let source: RuleSource
    public let conditions: RuleConditions
    public let level: Level
    public let push: Bool
    public let cooldownSeconds: Double?
    public let mergeSeconds: Double?
}
