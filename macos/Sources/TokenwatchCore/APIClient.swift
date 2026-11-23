import Foundation

public enum APIError: LocalizedError {
    case invalidServer, invalidToken, invalidPath, invalidResponse, http(Int, String?), socketClosed

    public var errorDescription: String? {
        switch self {
        case .invalidServer: return "Enter a valid HTTP or HTTPS server URL without credentials, a query, or a fragment."
        case .invalidToken: return "Enter a bearer token without spaces or control characters."
        case .invalidPath: return "The resource ID is invalid."
        case .invalidResponse: return "The server returned an invalid response."
        case .http(401, _): return "The server rejected the bearer token."
        case .http(let status, let detail): return detail.map { "HTTP \(status): \($0)" } ?? "HTTP \(status)"
        case .socketClosed: return "The server closed the event stream."
        }
    }
}

private final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public struct APIClient: Sendable {
    private static let privateSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 120
        return URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }()

    private let server: URL
    private let token: String
    let session: URLSession

    public init(server: URL, token: String, session: URLSession? = nil) throws {
        guard let components = URLComponents(url: server, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = components.host, !host.isEmpty, components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil else { throw APIError.invalidServer }
        guard !token.isEmpty, token.utf8.allSatisfy({ (33...126).contains($0) }) else { throw APIError.invalidToken }
        self.server = server
        self.token = token
        self.session = session ?? Self.privateSession
    }

    public var serverURL: URL { server }

    static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let value = try decoder.singleValueContainer()
            let text = try value.decode(String.self)
            guard let date = WireDate.parse(text) else {
                throw DecodingError.dataCorruptedError(in: value, debugDescription: "Expected an ISO 8601 timestamp")
            }
            return date
        }
        return decoder
    }

    private func request(_ path: [String], method: String = "GET", query: [URLQueryItem] = [],
                         websocket: Bool = false) throws -> URLRequest {
        var url = server
        for part in path {
            guard part.range(of: #"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"#, options: .regularExpression) != nil,
                  !part.contains(where: \.isWhitespace) else { throw APIError.invalidPath }
            url.appendPathComponent(part)
        }
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw APIError.invalidServer }
        if websocket { components.scheme = components.scheme?.lowercased() == "https" ? "wss" : "ws" }
        if !query.isEmpty {
            components.queryItems = query
            components.percentEncodedQuery = components.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        }
        guard let endpoint = components.url else { throw APIError.invalidServer }
        var request = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if !websocket { request.setValue("application/json", forHTTPHeaderField: "Accept") }
        return request
    }

    private func request<Body: Encodable>(_ path: [String], method: String, body: Body) throws -> URLRequest {
        var result = try request(path, method: method)
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        result.httpBody = try encoder.encode(body)
        result.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return result
    }

    private struct ErrorBody: Decodable {
        struct Issue: Decodable { let msg: String }
        let detail: String?

        init(from decoder: Decoder) throws {
            let body = try decoder.container(keyedBy: CodingKeys.self)
            detail = (try? body.decode(String.self, forKey: .detail)) ??
                (try? body.decode([Issue].self, forKey: .detail))?.map(\.msg).joined(separator: "; ")
        }

        enum CodingKeys: String, CodingKey { case detail }
    }

    private func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.http(http.statusCode, (try? JSONDecoder().decode(ErrorBody.self, from: data))?.detail)
        }
        return data
    }

    private func decode<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data = try await send(request)
        return try Self.decoder().decode(T.self, from: data)
    }

    public func coins() async throws -> [Coin] { try await decode(request(["coins"])) }
    public func accounts() async throws -> [Account] { try await decode(request(["accounts"])) }
    public func accountSession(_ id: String) async throws -> AccountSession {
        try await decode(request(["accounts", id, "session"]))
    }

    public func createAccount(_ input: AccountInput) async throws -> Account {
        try await decode(request(["accounts"], method: "POST", body: input))
    }

    public func saveAccount(_ id: String, update: AccountUpdate) async throws -> Account {
        try await decode(request(["accounts", id], method: "PUT", body: update))
    }

    public func deleteAccount(_ id: String) async throws {
        _ = try await send(request(["accounts", id], method: "DELETE"))
    }

    public func startAccountLogin(_ id: String) async throws -> AccountLogin {
        try await decode(request(["accounts", id, "login"], method: "POST"))
    }

    public func accountLoginState(_ id: String) async throws -> AccountLogin {
        try await decode(request(["accounts", id, "login"]))
    }

    public func finishAccountLogin(_ id: String) async throws -> AccountLoginFinish {
        try await decode(request(["accounts", id, "login", "finish"], method: "POST"))
    }

    public func events(_ filter: EventFilter = .init()) async throws -> [Event] {
        try await decode(request(["events"], query: filter.queryItems))
    }

    private struct ReadInput: Encodable { let read: Bool }

    public func setRead(_ id: Int, read: Bool) async throws -> Event {
        try await decode(request(["events", String(id), "read"], method: "PUT", body: ReadInput(read: read)))
    }

    public func transfers(_ filter: TransferFilter = .init()) async throws -> [Transfer] {
        try await decode(request(["transfers"], query: filter.queryItems))
    }

    public func balances(_ filter: BalanceFilter) async throws -> BalanceCurve {
        try await decode(request(["balances"], query: filter.queryItems))
    }

    public func snapshots(_ page: Page = .init()) async throws -> [Snapshot] {
        try await decode(request(["snapshots"], query: page.queryItems))
    }

    public func snapshot(_ id: Int) async throws -> SnapshotDetail {
        try await decode(request(["snapshots", String(id)]))
    }

    public func createSnapshot(_ input: SnapshotInput) async throws -> Snapshot {
        try await decode(request(["snapshots"], method: "POST", body: input))
    }

    public func compareSnapshots(before: Int, after: Int) async throws -> SnapshotDiff {
        try await decode(request(["snapshots", "compare"], query: [
            .init(name: "before", value: String(before)), .init(name: "after", value: String(after)),
        ]))
    }

    public func subscriptions() async throws -> [Subscription] { try await decode(request(["subscriptions"])) }

    public func saveSubscription(_ id: String, enabled: Bool) async throws -> Subscription {
        try await decode(request(["subscriptions", id], method: "PUT", body: Subscription.Input(enabled: enabled)))
    }

    public func deleteSubscription(_ id: String) async throws {
        _ = try await send(request(["subscriptions", id], method: "DELETE"))
    }

    public func jobs() async throws -> [Job] { try await decode(request(["jobs"])) }

    public func saveJob(_ input: JobInput, id: Int? = nil) async throws -> Job {
        if let id { return try await decode(request(["jobs", String(id)], method: "PUT", body: input)) }
        return try await decode(request(["jobs"], method: "POST", body: input))
    }

    public func deleteJob(_ id: Int) async throws { _ = try await send(request(["jobs", String(id)], method: "DELETE")) }

    public func exclusions() async throws -> [Exclusion] { try await decode(request(["exclusions"])) }

    public func saveExclusion(_ id: String, reason: String) async throws -> Exclusion {
        try await decode(request(["exclusions", id], method: "PUT", body: Exclusion.Input(reason: reason)))
    }

    public func deleteExclusion(_ id: String) async throws {
        _ = try await send(request(["exclusions", id], method: "DELETE"))
    }

    public func rules() async throws -> [Rule] { try await decode(request(["rules"])) }

    public func saveRule(_ input: RuleInput, id: Int? = nil) async throws -> Rule {
        if let id { return try await decode(request(["rules", String(id)], method: "PUT", body: input)) }
        return try await decode(request(["rules"], method: "POST", body: input))
    }

    public func deleteRule(_ id: Int) async throws { _ = try await send(request(["rules", String(id)], method: "DELETE")) }

    public func webSocketRequest() throws -> URLRequest { try request(["ws", "events"], websocket: true) }
}
