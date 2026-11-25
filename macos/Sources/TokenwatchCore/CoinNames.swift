import Combine
import Foundation

public protocol CoinListing {
    func coins(_ status: CoinStatus?) async throws -> [Coin]
}

extension APIClient: CoinListing {}

@MainActor
public final class CoinNames: ObservableObject {
    @Published public private(set) var symbols: [String: String] = [:]
    public private(set) var missing: Set<String> = []
    private let source: CoinListing
    private var inFlight: Task<Bool, Never>?

    public init(source: CoinListing) { self.source = source }

    public func label(_ id: String) -> String { symbols[id] ?? id }

    /// Fetches the full coin list and clears `missing`. Returns false and keeps the current state when the request fails.
    @discardableResult
    public func load() async -> Bool {
        let loaded = await fetch()
        if loaded { missing = [] }
        return loaded
    }

    /// Fetches only when an id is neither known nor already recorded as missing. Ids still unknown after a successful fetch are recorded as missing.
    public func resolve(_ ids: [String?]) async {
        let unresolved = Set(ids.compactMap { $0 }).filter { symbols[$0] == nil && !missing.contains($0) }
        guard !unresolved.isEmpty else { return }
        guard await fetch() else { return }
        missing.formUnion(unresolved.filter { symbols[$0] == nil })
    }

    private func fetch() async -> Bool {
        if let inFlight { return await inFlight.value }
        let task = Task { () -> Bool in
            let loaded = await self.perform()
            self.inFlight = nil
            return loaded
        }
        inFlight = task
        return await task.value
    }

    private func perform() async -> Bool {
        guard let coins = try? await source.coins(nil) else { return false }
        symbols = Dictionary(coins.map { ($0.id, $0.symbol) }, uniquingKeysWith: { first, _ in first })
        return true
    }
}
