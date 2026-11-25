import Combine
import Foundation

public protocol CoinListing {
    func coins(_ status: CoinStatus?) async throws -> [Coin]
}

extension APIClient: CoinListing {}

@MainActor
public final class CoinNames: ObservableObject {
    @Published public private(set) var symbols: [String: String] = [:]
    private let source: CoinListing
    private var inFlight: Task<Bool, Never>?

    public init(source: CoinListing) { self.source = source }

    public func label(_ id: String) -> String { symbols[id] ?? id }

    /// Fetches the full coin list. Concurrent calls share one request. Returns false and keeps the current symbols when the request fails.
    @discardableResult
    public func load() async -> Bool {
        if let inFlight { return await inFlight.value }
        let task = Task { await fetch() }
        inFlight = task
        let loaded = await task.value
        inFlight = nil
        return loaded
    }

    /// Refreshes the list when any ID has no symbol yet.
    public func resolve(_ ids: [String?]) async {
        guard ids.contains(where: { $0.map { symbols[$0] == nil } ?? false }) else { return }
        await load()
    }

    private func fetch() async -> Bool {
        guard let coins = try? await source.coins(nil) else { return false }
        symbols = Dictionary(coins.map { ($0.id, $0.symbol) }, uniquingKeysWith: { first, _ in first })
        return true
    }
}
