import SwiftUI
import TokenwatchCore
import WebKit

private let timezoneSuggestions = [
    "UTC", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin",
    "Asia/Singapore", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney",
]

@MainActor
struct AccountForm: View {
    let client: APIClient
    let onSave: (Account) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var id = ""
    @State private var proxyURL = ""
    @State private var timezone = ""
    @State private var persona = "windows"
    @State private var action = ActionState()

    var body: some View {
        Form {
            TextField("Account ID", text: $id)
            SecureField("Proxy URL (socks5h://user:pass@host:port)", text: $proxyURL)
            HStack {
                TextField("Timezone (IANA)", text: $timezone)
                Menu("Suggestions") {
                    ForEach(timezoneSuggestions, id: \.self) { zone in Button(zone) { timezone = zone } }
                }.fixedSize()
            }
            Picker("Persona", selection: $persona) {
                Text("Windows").tag("windows")
                Text("None").tag("none")
            }
            Text("The proxy URL is sent once and never shown again.").font(.caption).foregroundStyle(.secondary)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                Button("Add Account") { action.run { try await save() } }
                    .disabled(id.isEmpty || proxyURL.isEmpty || timezone.nonblank == nil)
            }
        }.formStyle(.grouped).frame(width: 460).disabled(action.busy).interactiveDismissDisabled(action.busy)
            .onDisappear { proxyURL = "" }
    }

    private func save() async throws {
        guard id.range(of: #"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"#, options: .regularExpression) != nil else {
            throw InputError("Enter an ID that starts with a letter or digit and contains only letters, digits, hyphens, and underscores.")
        }
        guard let proxy = proxyURL.nonblank, proxy.hasPrefix("socks5h://") else {
            throw InputError("The proxy URL must start with socks5h://.")
        }
        guard let zone = timezone.nonblank else { throw InputError("Enter an IANA timezone.") }
        defer { proxyURL = "" }
        onSave(try await client.createAccount(AccountInput(id: id, proxyUrl: proxy, timezone: zone, persona: persona)))
    }
}

@MainActor
struct LoginSheet: View {
    let client: APIClient
    let account: Account
    let onFinish: (Result<AccountSession.Status, Error>) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var phase: AccountLogin.Phase?
    @State private var path: String?
    @State private var started = false
    @State private var finished = false
    @State private var note: String?
    @State private var action = ActionState()

    private var loginURL: URL? {
        guard let path, path.hasPrefix("/"), !path.hasPrefix("//") else { return nil }
        var base = client.serverURL.absoluteString
        if base.hasSuffix("/") { base.removeLast() }
        return URL(string: base + path)
    }

    private var statusText: String {
        switch phase {
        case nil: return action.busy ? "Requesting the login window…" : "The login window is unavailable."
        case .starting?: return "Starting the remote browser…"
        case .open?: return "Log in in the window below, then press Done."
        case .closed?: return "The login window closed. Press Done to check the session."
        case .failed?: return "Remote login failed. Press Done to check the session."
        case .none?: return "No login window is open. Press Done to check the session."
        }
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Text("Log In: \(account.id)").font(.headline)
                Spacer()
                Button("Done") { action.run { try await finish() } }.disabled(action.busy)
            }
            Text(statusText).foregroundStyle(.secondary)
            ActionNotice(action: action)
            if let note { Text(note).foregroundStyle(.red) }
            if let loginURL {
                LoginWebView(server: client.serverURL, url: loginURL).frame(minHeight: 520)
            } else {
                Spacer()
            }
        }.padding().frame(width: 960, height: 680)
            .interactiveDismissDisabled(action.busy)
            .task {
                await action.load { try await start() }
                while started, !Task.isCancelled, phase == .starting {
                    try? await Task.sleep(for: .seconds(2))
                    guard !Task.isCancelled else { return }
                    await poll()
                }
            }
            .onDisappear {
                guard started, !finished else { return }
                Task {
                    do { onFinish(.success(try await client.finishAccountLogin(account.id).status)) }
                    catch { onFinish(.failure(error)) }
                }
            }
    }

    private func start() async throws {
        let login = try await client.startAccountLogin(account.id)
        started = true
        phase = login.state
        path = login.path
    }

    private func poll() async {
        do {
            let login = try await client.accountLoginState(account.id)
            phase = login.state
            path = path ?? login.path
            note = nil
        } catch {
            note = error.localizedDescription
        }
    }

    private func finish() async throws {
        guard started else {
            dismiss()
            return
        }
        let status = try await client.finishAccountLogin(account.id).status
        finished = true
        onFinish(.success(status))
        dismiss()
    }
}

@MainActor
private struct LoginWebView: NSViewRepresentable {
    let server: URL
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator(server: server) }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        guard context.coordinator.loaded != url else { return }
        context.coordinator.loaded = url
        view.load(URLRequest(url: url))
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate {
        let server: URL
        var loaded: URL?

        init(server: URL) { self.server = server }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let target = action.request.url
            let sameServer = target?.scheme == server.scheme && target?.host?.lowercased() == server.host?.lowercased()
                && target?.port == server.port
            decisionHandler(sameServer ? .allow : .cancel)
        }
    }
}
