import Combine
import Foundation

@MainActor
public final class AccountStore: ObservableObject {
    @Published public private(set) var accounts: [Account] = []
    private var generation = UUID()
    private enum Change { case replace(Account), remove(String) }
    private var changes: [Change] = []
    private var loads = 0

    public init() {}

    public func clear() {
        generation = UUID()
        accounts = []
        changes = []
        loads = 0
    }

    public func replace(_ account: Account) {
        if loads > 0 { changes.append(.replace(account)) }
        applyReplacement(account)
    }

    private func applyReplacement(_ account: Account) {
        if let index = accounts.firstIndex(where: { $0.id == account.id }) { accounts[index] = account }
        else { accounts.append(account) }
    }

    public func remove(_ id: String) {
        if loads > 0 { changes.append(.remove(id)) }
        accounts.removeAll { $0.id == id }
    }

    public func reload(using loader: () async throws -> [Account]) async throws {
        let current = generation
        let start = changes.count
        loads += 1
        defer {
            if current == generation {
                loads -= 1
                if loads == 0 { changes = [] }
            }
        }
        let loaded = try await loader()
        guard current == generation else { return }
        accounts = loaded
        for change in changes.dropFirst(start) {
            switch change {
            case .replace(let account): applyReplacement(account)
            case .remove(let id): accounts.removeAll { $0.id == id }
            }
        }
    }

    public func apply(_ message: StreamMessage, loader: () async throws -> [Account]) async throws {
        switch message {
        case .account(let account): replace(account)
        case .accountDeleted(let id): remove(id)
        case .resync: try await reload(using: loader)
        case .event, .ignored: break
        }
    }
}
