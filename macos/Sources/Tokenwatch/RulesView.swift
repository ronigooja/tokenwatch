import SwiftUI
import TokenwatchCore

@MainActor
struct RulesView: View {
    private struct Edit: Identifiable { let id = UUID(); let rule: Rule? }

    let client: APIClient
    @State private var rules: [Rule] = []
    @State private var selection: Int?
    @State private var edit: Edit?
    @State private var confirming = false
    @State private var action = ActionState()

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Button("New Rule…") { edit = Edit(rule: nil) }
                Button("Edit…") {
                    if let rule = rules.first(where: { $0.id == selection }) { edit = Edit(rule: rule) }
                }.disabled(selection == nil)
                Button("Delete…", role: .destructive) { confirming = true }.disabled(selection == nil)
                Spacer()
                Button("Refresh") { action.run { rules = try await client.rules() } }
            }.disabled(action.busy)
            ActionNotice(action: action)
            Table(rules, selection: $selection) {
                TableColumn("Name", value: \.name)
                TableColumn("Source") { (rule: Rule) in Text(rule.source.rawValue) }
                TableColumn("Level") { (rule: Rule) in Text(rule.level.rawValue) }
                TableColumn("Enabled") { (rule: Rule) in Text(rule.enabled ? "Yes" : "No") }
                TableColumn("Push") { (rule: Rule) in Text(rule.push ? "Yes" : "No") }
                TableColumn("Cooldown (s)") { (rule: Rule) in Text(number(rule.cooldownSeconds)) }
            }
        }.navigationTitle("Rules")
            .task { await action.load { rules = try await client.rules() } }
            .sheet(item: $edit) { record in
                RuleForm(client: client, rule: record.rule) { rule in
                    rules = (rules.filter { $0.id != rule.id } + [rule]).sorted { $0.id < $1.id }
                    selection = rule.id
                    edit = nil
                }
            }
            .confirmationDialog("Delete the selected rule?", isPresented: $confirming) {
                Button("Delete Rule", role: .destructive) {
                    guard let id = selection else { return }
                    action.run { try await client.deleteRule(id); rules.removeAll { $0.id == id }; selection = nil }
                }
            }
    }
}

@MainActor
private struct RuleForm: View {
    let client: APIClient
    let rule: Rule?
    let onSave: (Rule) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var enabled = false
    @State private var source: RuleSource?
    @State private var coinsInclude = ""
    @State private var coinsExclude = ""
    @State private var entitiesInclude = ""
    @State private var entitiesExclude = ""
    @State private var direction: RuleDirection?
    @State private var minUSD = ""
    @State private var minBalanceChange = ""
    @State private var level: Level?
    @State private var push = false
    @State private var cooldown = ""
    @State private var merge = ""
    @State private var action = ActionState()
    @FocusState private var nameFocused: Bool

    var body: some View {
        Form {
            Section("Rule") {
                TextField("Name", text: $name).focused($nameFocused)
                Toggle("Enabled", isOn: $enabled)
                Picker("Source", selection: $source) {
                    Text("Choose…").tag(nil as RuleSource?)
                    ForEach(RuleSource.allCases, id: \.self) { item in Text(item.rawValue).tag(Optional(item)) }
                }
            }
            Section("Conditions") {
                Text("Separate IDs with commas. Leave a condition blank to omit it.").font(.caption).foregroundStyle(.secondary)
                TextField("Include coin IDs", text: $coinsInclude)
                TextField("Exclude coin IDs", text: $coinsExclude)
                TextField("Include entity IDs", text: $entitiesInclude)
                TextField("Exclude entity IDs", text: $entitiesExclude)
                TextField("Minimum USD", text: $minUSD)
                if let source {
                    Picker("Direction", selection: $direction) {
                        Text("Any").tag(nil as RuleDirection?)
                        ForEach(RuleDirection.allCases.filter { $0.isBalance == (source == .balanceDiff) }, id: \.self) { item in
                            Text(item.rawValue).tag(Optional(item))
                        }
                    }
                }
                if source == .balanceDiff { TextField("Minimum balance change (%)", text: $minBalanceChange) }
            }
            Section("Actions") {
                Picker("Level", selection: $level) {
                    Text("Choose…").tag(nil as Level?)
                    ForEach(Level.allCases, id: \.self) { item in Text(item.rawValue).tag(Optional(item)) }
                }
                Toggle("Push over WebSocket", isOn: $push)
                TextField("Cooldown (seconds)", text: $cooldown)
                TextField("Merge window (seconds)", text: $merge)
            }
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Save Rule") { action.run { try await save() } }
                    .disabled(name.nonblank == nil || source == nil || level == nil)
            }
        }.formStyle(.grouped).frame(width: 490, height: 635)
            .disabled(action.busy).interactiveDismissDisabled(action.busy)
            .defaultFocus($nameFocused, true)
            .onChange(of: source) { _, newSource in
                if let direction, direction.isBalance != (newSource == .balanceDiff) { self.direction = nil }
            }
            .onAppear { populate() }
    }

    private func populate() {
        guard let rule else { return }
        name = rule.name; enabled = rule.enabled; source = rule.source; level = rule.level; push = rule.push
        let fields = rule.conditions
        coinsInclude = fields.coinsInclude?.joined(separator: ", ") ?? ""
        coinsExclude = fields.coinsExclude?.joined(separator: ", ") ?? ""
        entitiesInclude = fields.entitiesInclude?.joined(separator: ", ") ?? ""
        entitiesExclude = fields.entitiesExclude?.joined(separator: ", ") ?? ""
        direction = fields.direction
        minUSD = fields.minUsd.map { String($0) } ?? ""
        minBalanceChange = fields.minBalanceChangePct.map { String($0) } ?? ""
        cooldown = rule.cooldownSeconds.map { String($0) } ?? ""
        merge = rule.mergeSeconds.map { String($0) } ?? ""
    }

    private func save() async throws {
        guard let name = name.nonblank, let source, let level else { return }
        let threshold = try (source == .balanceDiff ? minBalanceChange.nonnegative("Minimum balance change") : nil)
        let conditions = RuleConditions(coinsInclude: coinsInclude.csv, coinsExclude: coinsExclude.csv,
                                        entitiesInclude: entitiesInclude.csv, entitiesExclude: entitiesExclude.csv,
                                        direction: direction, minUsd: try minUSD.nonnegative("Minimum USD"),
                                        minBalanceChangePct: threshold)
        let input = RuleInput(name: name, enabled: enabled, source: source, conditions: conditions, level: level, push: push,
                              cooldownSeconds: try cooldown.nonnegative("Cooldown"), mergeSeconds: try merge.nonnegative("Merge window"))
        onSave(try await client.saveRule(input, id: rule?.id))
    }
}
