import Observation
import SwiftUI
import TokenwatchCore

struct InputError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

@MainActor
@Observable
final class ActionState {
    private(set) var busy = false
    private(set) var error: String?

    func run(_ work: @escaping @MainActor () async throws -> Void) {
        guard !busy else { return }
        busy = true
        Task { await perform(work) }
    }

    func load(_ work: @escaping @MainActor () async throws -> Void) async {
        guard !busy else { return }
        busy = true
        await perform(work)
    }

    private func perform(_ work: @escaping @MainActor () async throws -> Void) async {
        error = nil
        defer { busy = false }
        do { try await work() }
        catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}

@MainActor
struct ActionNotice: View {
    let action: ActionState
    var body: some View {
        if action.busy { ProgressView().controlSize(.small) }
        if let error = action.error {
            Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled)
        }
    }
}

extension String {
    var nonblank: String? {
        let text = trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    var csv: [String]? {
        let values = split(separator: ",").compactMap { String($0).nonblank }
        return values.isEmpty ? nil : values
    }

    func nonnegative(_ name: String) throws -> Double? {
        guard let text = nonblank else { return nil }
        guard let value = Double(text), value.isFinite, value >= 0 else {
            throw InputError("\(name) must be a nonnegative number.")
        }
        return value
    }
}

func validateBounds<T: Comparable>(_ lower: T?, _ upper: T?, _ name: String) throws {
    if let lower, let upper, lower > upper { throw InputError("\(name): the lower bound exceeds the upper bound.") }
}

func coinLabel(_ id: String, _ coins: [Coin]) -> String {
    coins.first(where: { $0.id == id })?.symbol ?? id
}

func number(_ value: Double?) -> String {
    value.map { $0.formatted(.number.precision(.fractionLength(0...8))) } ?? "—"
}

func usd(_ value: Double?) -> String {
    value.map { $0.formatted(.currency(code: "USD")) } ?? "—"
}

func jsonText<T: Encodable>(_ object: T) -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    guard let data = try? encoder.encode(object) else { return "—" }
    return String(decoding: data, as: UTF8.self)
}

@MainActor
struct DateBound: View {
    let title: String
    @Binding var date: Date?
    var dayOnly = false

    var body: some View {
        HStack {
            Toggle(title, isOn: Binding(get: { date != nil }, set: { date = $0 ? .now : nil }))
            if date != nil {
                DatePicker(title, selection: Binding(get: { date ?? .now }, set: { date = $0 }),
                           displayedComponents: dayOnly ? [.date] : [.date, .hourAndMinute])
                    .labelsHidden()
            }
        }
    }
}

@MainActor
struct Pager: View {
    let page: Page
    let count: Int
    let busy: Bool
    let change: (Int) -> Void

    var body: some View {
        HStack {
            Button("Previous") { change(max(0, page.offset - page.limit)) }.disabled(page.offset == 0 || busy)
            Text("Page \(page.offset / page.limit + 1)").monospacedDigit()
            Button("Next") { change(page.offset + page.limit) }.disabled(count < page.limit || busy)
            Spacer()
        }
    }
}
