import SwiftUI
import TokenwatchCore
import WebKit

struct LoginRequest: Identifiable {
    let account: Account
    let page: LoginPage
    var id: String { "\(account.id)/\(page.rawValue)" }
}

@MainActor
struct AccountForm: View {
    let client: APIClient
    let existing: Account?
    let onSave: (Account, LoginPage?) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var email: String
    @State private var password: String
    @State private var proxyURL: String
    @State private var action = ActionState()
    @FocusState private var focus: Field?

    private enum Field: Hashable { case email, password, proxy }

    init(client: APIClient, existing: Account? = nil, onSave: @escaping (Account, LoginPage?) -> Void) {
        self.client = client
        self.existing = existing
        self.onSave = onSave
        _email = State(initialValue: existing?.label ?? "")
        _password = State(initialValue: existing?.password ?? "")
        _proxyURL = State(initialValue: existing?.proxyUrl ?? "")
    }

    var body: some View {
        Form {
            if existing == nil {
                TextField("Email", text: $email).focused($focus, equals: .email)
            } else {
                LabeledContent("Email", value: email)
                Text("The email is the account ID and cannot be changed. To use another email, delete the account and add it again.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            TextField("Password", text: $password).focused($focus, equals: .password)
            TextField("Proxy", text: $proxyURL, prompt: Text("socks5h://user:pass@host:port")).focused($focus, equals: .proxy)
            ActionNotice(action: action)
            HStack {
                Button("Cancel") { dismiss() }
                if existing == nil {
                    Button("Add Account") { action.run { try await add(.login) } }
                    Button("Add and Register") { action.run { try await add(.register) } }
                } else {
                    Button("Save") { action.run { try await update() } }
                }
            }
        }.formStyle(.grouped).frame(width: 460).disabled(action.busy).interactiveDismissDisabled(action.busy)
    }

    private func add(_ page: LoginPage) async throws {
        let mail = try validEmail()
        let secret = try validPassword()
        let proxy = try validProxy()
        let account = try await client.createAccount(AccountInput(email: mail, password: secret, proxyUrl: proxy))
        onSave(account, page)
        dismiss()
    }

    private func update() async throws {
        guard let existing else { return }
        let proxy = try validProxy()
        let update = AccountUpdate(proxyUrl: proxy == existing.proxyUrl ? nil : proxy,
                                   password: password.isEmpty || password == existing.password ? nil : password)
        if update.proxyUrl != nil || update.password != nil {
            onSave(try await client.saveAccount(existing.id, update: update), nil)
        }
        dismiss()
    }

    private func validEmail() throws -> String {
        guard let text = email.nonblank, text.filter({ $0 == "@" }).count == 1 else {
            throw InputError("Enter the account email address.")
        }
        return text
    }

    private func validPassword() throws -> String {
        guard !password.isEmpty else { throw InputError("Enter the account password.") }
        return password
    }

    private func validProxy() throws -> String {
        guard let text = proxyURL.nonblank, text.hasPrefix("socks5h://") else {
            throw InputError("The proxy URL must start with socks5h://.")
        }
        return text
    }
}

@MainActor
struct LoginSheet: View {
    let client: APIClient
    let account: Account
    let page: LoginPage
    let onFinish: (Result<AccountSession.Status?, Error>) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var phase: AccountLogin.Phase?
    @State private var path: String?
    @State private var started = false
    @State private var finished = false
    @State private var note: String?
    @State private var action = ActionState()

    private var title: String { page == .register ? "Register" : "Log In" }

    private var loginURL: URL? {
        guard let path, path.hasPrefix("/"), !path.hasPrefix("//") else { return nil }
        var base = client.serverURL.absoluteString
        if base.hasSuffix("/") { base.removeLast() }
        return URL(string: base + path)
    }

    private var statusText: String {
        switch phase {
        case nil, .starting?: return "Starting…"
        case .open?:
            return page == .register
                ? "Open. Complete registration in the window, then press Done."
                : "Open. Complete the verification and submit in the window, then press Done."
        case .closed?, .none?: return "The window was closed. Press Done to check the session."
        case .failed?: return "Failed to open the window."
        }
    }

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Text("\(title): \(account.label)").font(.headline)
                Spacer()
                Button("Close") { action.run { try await cancel() } }.disabled(action.busy)
                Button("Done") { action.run { try await finish() } }.disabled(action.busy)
            }
            Group {
                if let message = action.error ?? note {
                    Text(message).foregroundStyle(.red).textSelection(.enabled)
                } else {
                    Text(statusText).foregroundStyle(.secondary)
                }
            }
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: 18)
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
        let login = try await client.startAccountLogin(account.id, page: page)
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

    private func cancel() async throws {
        guard started else {
            dismiss()
            return
        }
        _ = try await client.cancelAccountLogin(account.id)
        finished = true
        onFinish(.success(nil))
        dismiss()
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
