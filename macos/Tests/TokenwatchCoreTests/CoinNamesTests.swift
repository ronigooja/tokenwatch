import Foundation
import XCTest
@testable import TokenwatchCore

private struct FixtureFailure: Error {}

private final class FakeCoinListing: CoinListing {
    var coins: [Coin] = []
    var failure: Error?
    private(set) var calls = 0

    func coins(_ status: CoinStatus?) async throws -> [Coin] {
        calls += 1
        if let failure { throw failure }
        return coins
    }
}

@MainActor
final class CoinNamesTests: XCTestCase {
    private let known = "alpha:56:0xaa03"
    private let unknown = "spot:BASE:0x0864"

    private func coin(_ id: String, _ symbol: String) throws -> Coin {
        let json = #"{"id":"\#(id)","symbol":"\#(symbol)","pricing_id":null,"status":"supported","reason":null,"sources":["spot"],"deployments":[],"matched_chains":[]}"#
        return try APIClient.decoder().decode(Coin.self, from: Data(json.utf8))
    }

    func testKnownIDReturnsSymbol() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin(known, "EVAA")]
        let names = CoinNames(source: source)

        let loaded = await names.load()

        XCTAssertTrue(loaded)
        XCTAssertEqual(names.label(known), "EVAA")
        XCTAssertEqual(source.calls, 1)
    }

    func testUnknownIDTriggersOneFetchThenNoneForSameID() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin(known, "EVAA")]
        let names = CoinNames(source: source)
        await names.load()

        await names.resolve([unknown])
        XCTAssertEqual(source.calls, 2)
        XCTAssertEqual(names.label(unknown), unknown)
        XCTAssertEqual(names.missing, [unknown])

        await names.resolve([unknown])
        XCTAssertEqual(source.calls, 2)
    }

    func testKnownAndNilIDsDoNotFetch() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin(known, "EVAA")]
        let names = CoinNames(source: source)
        await names.load()

        await names.resolve([known, nil])

        XCTAssertEqual(source.calls, 1)
    }

    func testFailedFetchIsRetriedOnNextResolve() async throws {
        let source = FakeCoinListing()
        source.failure = FixtureFailure()
        let names = CoinNames(source: source)

        await names.resolve([unknown])
        XCTAssertEqual(source.calls, 1)
        XCTAssertTrue(names.missing.isEmpty)
        XCTAssertEqual(names.label(unknown), unknown)

        source.failure = nil
        source.coins = [try coin(unknown, "BREV")]
        await names.resolve([unknown])
        XCTAssertEqual(source.calls, 2)
        XCTAssertEqual(names.label(unknown), "BREV")
    }

    func testFailedRefreshKeepsExistingSymbolsAndMissing() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin(known, "EVAA")]
        let names = CoinNames(source: source)
        await names.load()
        await names.resolve([unknown])

        source.failure = FixtureFailure()
        await names.load()

        XCTAssertEqual(names.label(known), "EVAA")
        XCTAssertEqual(names.missing, [unknown])
    }

    func testConnectionLoadResetsMissing() async throws {
        let source = FakeCoinListing()
        let names = CoinNames(source: source)
        await names.load()
        await names.resolve([unknown])
        XCTAssertEqual(names.missing, [unknown])

        source.coins = [try coin(unknown, "BREV")]
        await names.load()

        XCTAssertTrue(names.missing.isEmpty)
        XCTAssertEqual(names.label(unknown), "BREV")
    }

    func testFailedInitialLoadFallsBackToRawID() async {
        let source = FakeCoinListing()
        source.failure = FixtureFailure()
        let names = CoinNames(source: source)

        let loaded = await names.load()

        XCTAssertFalse(loaded)
        XCTAssertEqual(names.label(known), known)
    }
}

final class BoundedWaitTests: XCTestCase {
    func testReturnsWhenWorkFinishesBeforeTimeout() async {
        let work = Task { () -> Void in }
        let clock = ContinuousClock()
        let start = clock.now

        await BoundedWait.wait(for: work, within: .seconds(5))

        XCTAssertLessThan(start.duration(to: clock.now), .seconds(2))
    }

    func testReturnsAtTimeoutWhenWorkSuspends() async {
        let suspended = Task<Void, Never> {
            do { try await Task.sleep(for: .seconds(60)) } catch {}
        }
        defer { suspended.cancel() }
        let clock = ContinuousClock()
        let start = clock.now

        await BoundedWait.wait(for: suspended, within: .milliseconds(100))

        XCTAssertLessThan(start.duration(to: clock.now), .seconds(5))
        XCTAssertFalse(suspended.isCancelled)
    }
}
