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
/// Lookback window of a snapshot capture. Not a schedule; the wire key is `interval`.
public enum Lookback: String, Codable, Sendable, CaseIterable {
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
    case transfers, daily, coinCatalog = "coin_catalog", snapshots

    /// Kinds the Jobs editor may create. `snapshots` is decoded for display only until its backend shape is confirmed.
    public static let editable: [JobKind] = [.transfers, .daily, .coinCatalog]
}
public enum RuleSource: String, Codable, Sendable, CaseIterable { case transfer, balanceDiff = "balance_diff" }

public enum CoinStatus: Hashable, Sendable, Codable {
    case supported, noMatch, ambiguous, unsupported, needsManualMapping, unknownOnchain, excluded, noFutures
    case pendingMatch, pendingSupport
    case unknown(String)

    public static let known: [CoinStatus] = [
        .supported, .noMatch, .ambiguous, .unsupported, .needsManualMapping, .unknownOnchain,
        .excluded, .noFutures, .pendingMatch, .pendingSupport,
    ]

    public var rawValue: String {
        switch self {
        case .supported: return "supported"
        case .noMatch: return "no_match"
        case .ambiguous: return "ambiguous"
        case .unsupported: return "unsupported"
        case .needsManualMapping: return "needs_manual_mapping"
        case .unknownOnchain: return "unknown_onchain"
        case .excluded: return "excluded"
        case .noFutures: return "no_futures"
        case .pendingMatch: return "pending_match"
        case .pendingSupport: return "pending_support"
        case .unknown(let value): return value
        }
    }

    public var label: String {
        switch self {
        case .supported: return "Supported"
        case .noMatch: return "No match"
        case .ambiguous: return "Ambiguous"
        case .unsupported: return "Unsupported"
        case .needsManualMapping: return "Needs mapping"
        case .unknownOnchain: return "Unknown on-chain"
        case .excluded: return "Excluded"
        case .noFutures: return "No futures"
        case .pendingMatch: return "Pending match"
        case .pendingSupport: return "Pending support"
        case .unknown(let value): return value
        }
    }

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self = Self.known.first(where: { $0.rawValue == value }) ?? .unknown(value)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public enum CoinFeed: String, Codable, Sendable, CaseIterable {
    case spot, alpha

    public var label: String { self == .spot ? "Spot" : "Alpha" }
}

public struct Deployment: Codable, Sendable, Hashable {
    public let feed: CoinFeed
    public let chain: String
    public let address: String?
    public let depositEnabled: Bool?
    public let withdrawEnabled: Bool?
}

public struct Coin: Codable, Sendable, Identifiable {
    public let id: String
    public let symbol: String
    public let pricingId: String?
    public let status: CoinStatus
    public let reason: String?
    public let sources: [CoinFeed]
    public let deployments: [Deployment]
    public let matchedChains: [String]

    public var chains: [String] {
        if !matchedChains.isEmpty { return matchedChains }
        var seen = Set<String>()
        return deployments.map(\.chain).filter { seen.insert($0).inserted }
    }
}

public struct CoinMapping: Codable, Sendable {
    public let pricingId: String
}

public enum CatalogState: String, Codable, Sendable { case validated, unknown }

public struct CatalogStatus: Codable, Sendable {
    public let status: CatalogState
    public let attemptedAt: Double?
    public let refreshedAt: Double?
    public let error: String?
}

public enum CatalogRuleKind: String, Codable, Sendable, CaseIterable { case flag, tag }

public struct CatalogExclusion: Codable, Sendable, Identifiable {
    public var id: String { "\(kind.rawValue):\(value)" }
    public let kind: CatalogRuleKind
    public let value: String
    public let reason: String
}

public struct FuturesMapping: Codable, Sendable, Identifiable {
    public var id: String { base }
    public let base: String
    public let symbol: String
}

public struct FuturesMappingInput: Encodable, Sendable {
    public let symbol: String
}

public struct ChainObservation: Codable, Sendable, Identifiable {
    public var id: String { alphaChainId }
    public let alphaChainId: String
    public let arkhamChains: [String]
    public let conflict: Bool
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

/// One coin in the automatic snapshot index. `id` is an opaque Arkham pricing ID: send it back as `coin_id`, never display it.
public struct SnapshotCoin: Codable, Sendable, Hashable, Identifiable {
    public let id: String
    public let symbol: String
    public let lastCapturedAt: Date?
    public let snapshotCount: Int
}

/// `pricingIds` and `balanceMin` are decoded for interpretation only; pricing IDs are never displayed.
public struct Snapshot: Codable, Sendable, Identifiable {
    private enum CodingKeys: String, CodingKey {
        case id, createdAt, lookback = "interval", balanceMin, pricingIds, truncated
    }

    public let id: Int
    public let createdAt: Date
    public let lookback: Lookback
    public let balanceMin: Double?
    public let pricingIds: [String]?
    public let truncated: Bool
}

/// `tokenBalances` may contain internal identifiers and is decoded but never displayed.
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
    private enum CodingKeys: String, CodingKey {
        case id, createdAt, lookback = "interval", balanceMin, pricingIds, truncated, rows
    }

    public let id: Int
    public let createdAt: Date
    public let lookback: Lookback
    public let balanceMin: Double?
    public let pricingIds: [String]?
    public let truncated: Bool
    public let rows: [SnapshotRow]
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
}

struct ReasonInput: Encodable, Sendable { let reason: String }

public struct Account: Codable, Sendable, Identifiable {
    public enum Status: String, Codable, Sendable { case active, paused }
    public let id: String
    public let email: String?
    public let password: String?
    public let proxyUrl: String?
    public let status: Status
    public let pauseReason: String?
    public let availableAt: Double
    public let failures: Int
    public let managed: Bool?
    public let timezone: String?
    public let persona: String?
    public let sessionStatus: AccountSession.Status?
    public let sessionCheckedAt: Double?

    public var label: String { email ?? id }
}

public struct AccountInput: Encodable, Sendable {
    public let email: String
    public let password: String
    public let proxyUrl: String

    public init(email: String, password: String, proxyUrl: String) {
        self.email = email
        self.password = password
        self.proxyUrl = proxyUrl
    }
}

public struct AccountUpdate: Encodable, Sendable {
    public let status: Account.Status?
    public let proxyUrl: String?
    public let password: String?

    public init(status: Account.Status? = nil, proxyUrl: String? = nil, password: String? = nil) {
        self.status = status
        self.proxyUrl = proxyUrl
        self.password = password
    }
}

public enum LoginPage: String, Codable, Sendable, CaseIterable { case login, register, maintenance }

public struct AccountLogin: Codable, Sendable {
    public enum Phase: String, Codable, Sendable { case none, starting, open, closed, failed }
    public let state: Phase
    public let path: String?
}

public struct AccountLoginFinish: Codable, Sendable {
    public let status: AccountSession.Status
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
    public let accountId: String?
    public let coinId: String?
    public let options: JobOptions
    public let intervalSeconds: Double
    public let enabled: Bool

    public init(kind: JobKind, accountId: String?, coinId: String?, options: JobOptions,
                intervalSeconds: Double, enabled: Bool) {
        self.kind = kind
        self.accountId = accountId
        self.coinId = coinId
        self.options = options
        self.intervalSeconds = intervalSeconds
        self.enabled = enabled
    }

    private enum CodingKeys: String, CodingKey { case kind, accountId, coinId, options, intervalSeconds, enabled }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(kind, forKey: .kind)
        try container.encode(accountId, forKey: .accountId)
        // The snapshots job requires an explicit null coin_id; other kinds omit it when unset.
        if kind == .snapshots { try container.encodeNil(forKey: .coinId) }
        else { try container.encodeIfPresent(coinId, forKey: .coinId) }
        // The snapshots job requires an empty options object, not `{"balance_min": null}`.
        if kind == .snapshots { try container.encode(EmptyOptions(), forKey: .options) }
        else { try container.encode(options, forKey: .options) }
        try container.encode(intervalSeconds, forKey: .intervalSeconds)
        try container.encode(enabled, forKey: .enabled)
    }
}

private struct EmptyOptions: Encodable {}

extension JobInput {
    /// The singleton snapshots job. Backend requires null account and coin, empty options, and the whole cycle in seconds.
    public static func snapshotCycle(seconds: Double, enabled: Bool) -> JobInput {
        JobInput(kind: .snapshots, accountId: nil, coinId: nil, options: JobOptions(balanceMin: nil),
                 intervalSeconds: seconds, enabled: enabled)
    }
}

public struct Job: Codable, Sendable, Identifiable {
    public let id: Int
    public let kind: JobKind
    public let accountId: String?
    public let coinId: String?
    public let options: JobOptions
    public let intervalSeconds: Double
    public let enabled: Bool
    public let nextRun: Double
    public let waitReason: String?
    public let lastError: String?

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(Int.self, forKey: .id)
        kind = try container.decode(JobKind.self, forKey: .kind)
        accountId = try container.decodeIfPresent(String.self, forKey: .accountId)
        coinId = try container.decodeIfPresent(String.self, forKey: .coinId)
        options = try container.decode(JobOptions.self, forKey: .options)
        intervalSeconds = try container.decode(Double.self, forKey: .intervalSeconds)
        enabled = try container.decode(Bool.self, forKey: .enabled)
        nextRun = try container.decode(Double.self, forKey: .nextRun)
        waitReason = try container.decodeIfPresent(String.self, forKey: .waitReason)
        lastError = try container.decodeIfPresent(String.self, forKey: .lastError)
    }

    private enum CodingKeys: String, CodingKey {
        case id, kind, accountId, coinId, options, intervalSeconds, enabled, nextRun, waitReason, lastError
    }
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
