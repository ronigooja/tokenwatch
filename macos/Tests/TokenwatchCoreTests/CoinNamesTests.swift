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
    private func coin(_ id: String, _ symbol: String) throws -> Coin {
        let json = #"{"id":"\#(id)","symbol":"\#(symbol)","pricing_id":null,"status":"supported","reason":null,"sources":["spot"],"deployments":[],"matched_chains":[]}"#
        return try APIClient.decoder().decode(Coin.self, from: Data(json.utf8))
    }

    func testKnownIDReturnsSymbol() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin("alpha:56:0xaa03", "EVAA")]
        let names = CoinNames(source: source)

        let loaded = await names.load()

        XCTAssertTrue(loaded)
        XCTAssertEqual(names.label("alpha:56:0xaa03"), "EVAA")
        XCTAssertEqual(source.calls, 1)
    }

    func testUnknownIDFallsBackThenShowsSymbolAfterRefresh() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin("alpha:56:0xaa03", "EVAA")]
        let names = CoinNames(source: source)
        await names.load()

        let id = "spot:BASE:0x0864"
        XCTAssertEqual(names.label(id), id)

        await names.resolve([id])
        XCTAssertEqual(names.label(id), id)
        XCTAssertEqual(source.calls, 2)

        source.coins.append(try coin(id, "BREV"))
        await names.resolve([id])
        XCTAssertEqual(names.label(id), "BREV")
        XCTAssertEqual(names.label("alpha:56:0xaa03"), "EVAA")
        XCTAssertEqual(source.calls, 3)
    }

    func testResolveWithKnownIDsDoesNotRefetch() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin("alpha:56:0xaa03", "EVAA")]
        let names = CoinNames(source: source)
        await names.load()

        await names.resolve(["alpha:56:0xaa03", nil])

        XCTAssertEqual(source.calls, 1)
    }

    func testRefreshFailureKeepsFallbackAndExistingSymbols() async throws {
        let source = FakeCoinListing()
        source.coins = [try coin("alpha:56:0xaa03", "EVAA")]
        let names = CoinNames(source: source)
        await names.load()

        source.failure = FixtureFailure()
        let id = "spot:BASE:0x0864"
        source.coins.append(try coin(id, "BREV"))
        await names.resolve([id])

        XCTAssertEqual(names.label(id), id)
        XCTAssertEqual(names.label("alpha:56:0xaa03"), "EVAA")
    }

    func testFailedInitialLoadFallsBackToRawID() async {
        let source = FakeCoinListing()
        source.failure = FixtureFailure()
        let names = CoinNames(source: source)

        let loaded = await names.load()

        XCTAssertFalse(loaded)
        XCTAssertEqual(names.label("alpha:56:0xaa03"), "alpha:56:0xaa03")
    }
}
