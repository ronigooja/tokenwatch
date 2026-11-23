import SwiftUI
import TokenwatchCore

private enum CatalogTab: String, CaseIterable, Identifiable {
    case exclusions = "Exclusions", futures = "Futures", chains = "Chains"
    var id: Self { self }
}

@MainActor
struct CatalogRulesView: View {
    let client: APIClient
    @State private var tab = CatalogTab.exclusions
    @State private var exclusions: [CatalogExclusion] = []
    @State private var futures: [FuturesMapping] = []
    @State private var chains: [ChainObservation] = []
    @State private var selection: String?
    @State private var adding = false
    @State private var confirming = false
    @State private var action = ActionState()

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Picker("Rules", selection: $tab) {
                    ForEach(CatalogTab.allCases) { item in Text(item.rawValue).tag(item) }
                }.pickerStyle(.segmented).labelsHidden().fixedSize()
                Spacer()
                Button("Add…") { adding = true }.disabled(tab == .chains)
                Button("Delete…", role: .destructive) { confirming = true }.disabled(selection == nil || tab == .chains)
                Button("Refresh") { action.run { try await load() } }
            }.disabled(action.busy)
            ActionNotice(action: action)
            switch tab {
            case .exclusions:
                Table(exclusions, selection: $selection) {
                    TableColumn("Kind") { (rule: CatalogExclusion) in Text(rule.kind.rawValue) }.width(60)
                    TableColumn("Value", value: \.value)
                    TableColumn("Reason", value: \.reason)
                }
            case .futures:
                Table(futures, selection: $selection) {
                    TableColumn("Base", value: \.base)
                    TableColumn("Symbol", value: \.symbol)
                }
            case .chains:
                Table(chains) {
                    TableColumn("Alpha Chain ID", value: \.alphaChainId)
                    TableColumn("Arkham Chains") { (row: ChainObservation) in
                        Text(row.arkhamChains.joined(separator: ", ")).lineLimit(1)
                    }
                    TableColumn("Conflict") { (row: ChainObservation) in
                        if row.conflict { Label("Conflict", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
                    }.width(90)
                }
            }
        }.navigationTitle("Catalog Rules")
            .task { await action.load { try await load() } }
            .onChange(of: tab) { _, _ in selection = nil }
            .sheet(isPresented: $adding) {
                CatalogRuleForm(client: client, tab: tab) {
                    adding = false
                    action.run { try await load() }
                }
            }
            .confirmationDialog("Delete the selected rule?", isPresented: $confirming) {
                Button("Delete Rule", role: .destructive) { action.run { try await deleteSelected() } }
            }
    }

    private func load() async throws {
        async let exclusionList = client.catalogExclusions()
        async let futuresList = client.futuresMappings()
        async let chainList = client.chainObservations()
        (exclusions, futures, chains) = try await (exclusionList, futuresList, chainList)
    }

    private func deleteSelected() async throws {
        guard let id = selection else { return }
        switch tab {
        case .exclusions:
            guard let rule = exclusions.first(where: { $0.id == id }) else { return }
            try await client.deleteCatalogExclusion(rule.kind, value: rule.value)
        case .futures:
            guard let mapping = futures.first(where: { $0.id == id }) else { return }
            try await client.deleteFuturesMapping(mapping.base)
        case .chains:
            return
        }
        selection = nil
        try await load()
    }
}

@MainActor
private struct CatalogRuleForm: View {
    let client: APIClient
    let tab: CatalogTab
    let onSave: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var kind: CatalogRuleKind = .flag
    @State private var key = ""
    @State private var detail = ""
    @State private var action = ActionState()

    var body: some View {
        Form {
            if tab == .exclusions {
                Picker("Kind", selection: $kind) {
                    ForEach(CatalogRuleKind.allCases, id: \.self) { item in Text(item.rawValue).tag(item) }
                }
                TextField("Value", text: $key)
                TextField("Reason", text: $detail)
            } else {
                TextField("Base", text: $key)
                TextField("Symbol", text: $detail)
            }
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Save") { action.run { try await save() } }
                    .disabled(key.nonblank == nil || detail.nonblank == nil)
            }
        }.formStyle(.grouped).frame(width: 420).disabled(action.busy).interactiveDismissDisabled(action.busy)
    }

    private func save() async throws {
        guard let value = key.nonblank, let text = detail.nonblank else { throw InputError("Fill in every field.") }
        switch tab {
        case .exclusions: _ = try await client.saveCatalogExclusion(kind, value: value, reason: text)
        case .futures: _ = try await client.saveFuturesMapping(value, symbol: text)
        case .chains: return
        }
        onSave()
    }
}
