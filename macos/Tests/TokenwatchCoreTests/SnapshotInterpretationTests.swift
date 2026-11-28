import Foundation
import XCTest
@testable import TokenwatchCore

final class SnapshotInterpretationTests: XCTestCase {
    private func detail(_ id: Int, at seconds: TimeInterval, interval: Interval = .week, min: Double? = 100,
                        truncated: Bool = false, rows: [SnapshotRow]) -> SnapshotDetail {
        SnapshotDetail(id: id, createdAt: Date(timeIntervalSince1970: seconds), interval: interval, balanceMin: min,
                       pricingIds: nil, truncated: truncated, rows: rows)
    }

    private func row(_ id: String, name: String? = nil, units: Double?, usd: Double? = nil) -> SnapshotRow {
        SnapshotRow(entityId: id, entityName: name, entityType: nil, balanceUnit: units, balanceUsd: usd,
                    prevBalanceUnit: nil, prevBalanceUsd: nil, tokenBalances: nil)
    }

    func testIndexFiltersBySymbolOnlyAndSortsStably() {
        let coins = [
            SnapshotCoin(id: "z-id", symbol: "ETH", lastCapturedAt: nil, snapshotCount: 0),
            SnapshotCoin(id: "b-id", symbol: "BTC", lastCapturedAt: nil, snapshotCount: 1),
            SnapshotCoin(id: "a-id", symbol: "btc", lastCapturedAt: nil, snapshotCount: 2),
        ]
        XCTAssertEqual(SnapshotCoinIndex.matching(coins, query: "").map(\.id), ["a-id", "b-id", "z-id"])
        XCTAssertEqual(SnapshotCoinIndex.matching(coins, query: " bt ").map(\.id), ["a-id", "b-id"])
        // The opaque pricing ID is not searchable.
        XCTAssertEqual(SnapshotCoinIndex.matching(coins, query: "z-id"), [])
    }

    func testComparisonColumnsAreChronologicalWithStableTieBreak() {
        let late = detail(9, at: 2000, rows: [row("e1", units: 1)])
        let tieHigh = detail(8, at: 1000, rows: [row("e1", units: 2)])
        let tieLow = detail(3, at: 1000, rows: [row("e1", units: 3)])
        let comparison = SnapshotComparison([late, tieHigh, tieLow])
        XCTAssertEqual(comparison.columns.map(\.id), [3, 8, 9])
    }

    func testMissingEntityIsNotZeroAndMissingAmountIsDistinct() {
        let first = detail(1, at: 1000, rows: [row("a", name: "Alpha", units: 5, usd: 10), row("b", units: nil)])
        let second = detail(2, at: 2000, rows: [row("b", units: 0, usd: 0)])
        let comparison = SnapshotComparison([second, first])
        let alpha = comparison.rows.first { $0.entityId == "a" }
        XCTAssertEqual(alpha?.cells, [.returned(units: 5, usd: 10), .notReturned])
        let beta = comparison.rows.first { $0.entityId == "b" }
        XCTAssertEqual(beta?.cells, [.returned(units: nil, usd: nil), .returned(units: 0, usd: 0)])
    }

    func testRowsAreUnionOfEntitiesAndUnchangedRowsStay() {
        let first = detail(1, at: 1000, rows: [row("a", units: 1), row("c", units: 7)])
        let second = detail(2, at: 2000, rows: [row("a", units: 1), row("b", units: 2)])
        let comparison = SnapshotComparison([first, second])
        XCTAssertEqual(Set(comparison.rows.map(\.entityId)), ["a", "b", "c"])
        let unchanged = comparison.rows.first { $0.entityId == "a" }
        XCTAssertEqual(unchanged?.cells, [.returned(units: 1, usd: nil), .returned(units: 1, usd: nil)])
    }

    func testRowOrderIsDeterministicWithNamedFirst() {
        let snapshot = detail(1, at: 1000, rows: [
            row("z", units: 1), row("m", name: "beta", units: 1), row("y", name: "Alpha", units: 1), row("x", units: 1),
        ])
        let comparison = SnapshotComparison([snapshot])
        XCTAssertEqual(comparison.rows.map(\.entityId), ["y", "m", "x", "z"])
    }

    func testDuplicateEntityInOneSnapshotKeepsFirstRow() {
        let snapshot = detail(1, at: 1000, rows: [row("a", units: 1), row("a", units: 99)])
        let comparison = SnapshotComparison([snapshot])
        XCTAssertEqual(comparison.rows.first?.cells, [.returned(units: 1, usd: nil)])
    }

    func testIntervalMinimumAndTruncationContext() {
        let mixed = SnapshotComparison([
            detail(1, at: 1000, interval: .week, min: 100, rows: []),
            detail(2, at: 2000, interval: .month, min: nil, truncated: true, rows: []),
        ])
        XCTAssertTrue(mixed.intervalsDiffer)
        XCTAssertTrue(mixed.minimumsDiffer)
        XCTAssertTrue(mixed.hasTruncatedColumn)
        let same = SnapshotComparison([detail(1, at: 1000, rows: []), detail(2, at: 2000, rows: [])])
        XCTAssertFalse(same.intervalsDiffer)
        XCTAssertFalse(same.minimumsDiffer)
        XCTAssertFalse(same.hasTruncatedColumn)
    }

    func testBoundedConcurrencyNeverExceedsLimitAndKeepsOrder() async throws {
        let counter = InFlightCounter()
        let inputs = Array(0..<20)
        let output = try await BoundedConcurrency.map(inputs, limit: 4) { value -> Int in
            await counter.enter()
            try await Task.sleep(nanoseconds: 1_000_000)
            await counter.leave()
            return value * 2
        }
        XCTAssertEqual(output, inputs.map { $0 * 2 })
        let peak = await counter.peak
        XCTAssertLessThanOrEqual(peak, 4)
    }
}

private actor InFlightCounter {
    private(set) var peak = 0
    private var current = 0

    func enter() {
        current += 1
        peak = max(peak, current)
    }

    func leave() { current -= 1 }
}
