import Foundation
import XCTest
@testable import TokenwatchCore

private final class StubProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let client else { return }
        let status: Int
        let body: String
        let authorized = request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token"
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        switch (request.httpMethod, url.path) {
        case ("GET", "/fixture/transfers"):
            let names = Set(query.map(\.name))
            let expected: Set<String> = ["coin_id", "start", "end", "min_usd", "max_usd", "direction", "entity",
                                         "address", "chain", "txhash", "offset", "limit"]
            let valid = authorized && names == expected && query.contains(.init(name: "entity", value: "A+B & C")) &&
                url.absoluteString.contains("A%2BB") && query.contains(.init(name: "offset", value: "100"))
            status = valid ? 200 : 422
            body = valid ? """
                [{"id":"tr-1","coin_ids":["ABC"],"chain":"example-chain","direction":"entity_to_entity",
                  "usd":125.5,"time":"2026-01-02T03:04:05.123456Z","gap":true,"txhash":null}]
                """ : #"{"detail":"Invalid transfer request"}"#
        case ("GET", "/fixture/events"):
            let valid = authorized && !query.contains { $0.name == "min_usd" || $0.name == "max_usd" }
            status = valid ? 200 : 401
            body = valid ? "[\(eventFixture)]" : #"{"detail":"Invalid bearer token"}"#
        case ("GET", "/fixture/balances"):
            let ids = query.filter { $0.name == "entity_id" }.compactMap(\.value)
            let valid = authorized && ids == ["owner_a", "owner+b"] && query.contains(.init(name: "start", value: "2026-01-02"))
            status = valid ? 200 : 422
            body = valid ? """
                {"coin_id":"ABC","days":[{"day":"2026-01-02","truncated":true}],"series":{
                "owner_a":[{"entity_id":"owner_a","day":"2026-01-02","balance_unit":5,"balance_usd":10}],"owner+b":[]}}
                """ : #"{"detail":"Invalid balance request"}"#
        case ("PUT", "/fixture/events/7/read"):
            let valid = authorized && request.value(forHTTPHeaderField: "Content-Type") == "application/json" &&
                bodyObject()?["read"] as? Bool == true
            status = valid ? 200 : 422
            body = valid ? eventFixture.replacingOccurrences(of: #""read_at":null"#, with: #""read_at":"2026-01-02T03:14:05Z""#) : "{}"
        case ("POST", "/fixture/rules"):
            let payload = bodyObject()
            let conditions = payload?["conditions"] as? [String: Any]
            let valid = authorized && payload?["source"] as? String == "balance_diff" && payload?["cooldown_seconds"] as? Int == 30 &&
                conditions?["coins_include"] as? [String] == ["ABC"] && conditions?["min_balance_change_pct"] as? Int == 5
            status = valid ? 201 : 422
            body = valid ? """
                {"id":12,"name":"Balance","enabled":true,"source":"balance_diff","level":"important","push":true,
                 "cooldown_seconds":30,"conditions":{"coins_include":["ABC"],"min_balance_change_pct":5,"direction":"decrease"}}
                """ : "{}"
        case ("DELETE", "/fixture/subscriptions/ABC") where authorized:
            status = 204
            body = ""
        case ("POST", "/fixture/accounts"):
            let payload = bodyObject()
            let valid = authorized && payload?["id"] as? String == "acc-1" && payload?["persona"] as? String == "windows" &&
                payload?["proxy_url"] as? String == "socks5h://user:pass@proxy.example:1080" && payload?["timezone"] as? String == "UTC"
            status = valid ? 201 : 422
            body = valid ? accountFixture : #"{"detail":"Invalid account"}"#
        case ("PUT", "/fixture/accounts/acc-1"):
            let payload = bodyObject()
            let valid = authorized && payload?["proxy_ref"] as? String == "proxy-1" && payload?["status"] as? String == "paused"
            status = valid ? 200 : 422
            body = valid ? accountFixture.replacingOccurrences(of: #""status":"active""#, with: #""status":"paused""#) : "{}"
        case ("DELETE", "/fixture/accounts/acc-1") where authorized:
            status = 204
            body = ""
        case ("POST", "/fixture/accounts/acc-1/login") where authorized:
            status = 201
            body = #"{"state":"starting","path":"/login-view/ticket-1/vnc.html?autoconnect=1"}"#
        case ("GET", "/fixture/accounts/acc-1/login") where authorized:
            status = 200
            body = #"{"state":"open","path":"/login-view/ticket-1/vnc.html?autoconnect=1"}"#
        case ("POST", "/fixture/accounts/acc-1/login/finish") where authorized:
            status = 200
            body = #"{"status":"logged_in"}"#
        default:
            status = 404
            body = #"{"detail":"No stub route"}"#
        }
        let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
        client.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !body.isEmpty { client.urlProtocol(self, didLoad: Data(body.utf8)) }
        client.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private func bodyObject() -> [String: Any]? {
        let data: Data
        if let body = request.httpBody { data = body }
        else if let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var body = Data()
            var buffer = [UInt8](repeating: 0, count: 1024)
            while true {
                let size = stream.read(&buffer, maxLength: buffer.count)
                if size < 0 { return nil }
                if size == 0 { break }
                body.append(contentsOf: buffer.prefix(size))
            }
            data = body
        } else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

private let accountFixture = #"{"id":"acc-1","proxy_ref":"proxy-1","status":"active","pause_reason":null,"available_at":0,"failures":0,"managed":true,"timezone":"UTC","persona":"windows"}"#

private let eventFixture = """
    {"id":7,"kind":"transfer","rule_id":3,"coin_id":"ABC","level":"normal","push":true,
     "created_at":"2026-01-02T03:04:05Z","last_at":"2026-01-02T03:04:05.123456Z","count":2,"read_at":null,
     "data":{"usd":250,"transfer_id":"tr-1","account_id":"fixture-account","entities":["owner_a"]}}
    """

final class APIClientTests: XCTestCase {
    private func client(token: String = "fixture-token") throws -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return try APIClient(server: XCTUnwrap(URL(string: "https://tokenwatch.invalid/fixture")), token: token,
                             session: URLSession(configuration: config))
    }

    func testTransferRequestAndDecode() async throws {
        let api = try client()
        var filter = TransferFilter()
        filter.coinId = "ABC"
        filter.start = Date(timeIntervalSince1970: 1)
        filter.end = Date(timeIntervalSince1970: 2)
        filter.minUsd = 1; filter.maxUsd = 300
        filter.direction = .entityToEntity; filter.entity = "A+B & C"
        filter.address = "addr"; filter.chain = "chain"; filter.txhash = "tx"
        filter.page = Page(offset: 100)
        let rows = try await api.transfers(filter)
        let row = try XCTUnwrap(rows.first)
        XCTAssertEqual(row.coinIds, ["ABC"])
        XCTAssertEqual(row.usd, 125.5)
        XCTAssertEqual(row.direction, .entityToEntity)
        XCTAssertTrue(row.gap)
        XCTAssertNil(row.txhash)
        XCTAssertEqual(WireDate.timestamp(try XCTUnwrap(filter.start)), "1970-01-01T00:00:01.000Z")
    }

    func testEventFiltersAndReadWrite() async throws {
        let api = try client()
        var filter = EventFilter()
        filter.coinId = "ABC"; filter.kind = "transfer"; filter.ruleId = 3; filter.level = .normal; filter.read = false
        filter.minUsd = 250; filter.maxUsd = 300
        XCTAssertEqual(Set(filter.queryItems.map(\.name)), ["coin_id", "kind", "rule_id", "level", "read", "limit", "offset"])
        let events = try await api.events(filter)
        let event = try XCTUnwrap(events.first)
        XCTAssertTrue(filter.matches(event))
        XCTAssertEqual(event.data["transfer_id"], .string("tr-1"))
        filter.minUsd = 251
        XCTAssertFalse(filter.matches(event))
        let updated = try await api.setRead(7, read: true)
        XCTAssertNotNil(updated.readAt)
    }

    func testBalanceQueryUsesRepeatedEntityIDs() async throws {
        let api = try client()
        let filter = BalanceFilter(coinId: "ABC", entityIds: ["owner_a", "owner+b"], start: "2026-01-02")
        let curve = try await api.balances(filter)
        XCTAssertEqual(curve.series["owner_a"]?.first?.entityId, "owner_a")
        XCTAssertEqual(curve.series["owner+b"]?.count, 0)
        XCTAssertEqual(curve.days.first?.truncated, true)
    }

    func testRuleJSONAndNoContentDelete() async throws {
        let api = try client()
        let conditions = RuleConditions(coinsInclude: ["ABC"], coinsExclude: nil, entitiesInclude: nil, entitiesExclude: nil,
                                        direction: .decrease, minUsd: nil, minBalanceChangePct: 5)
        let input = RuleInput(name: "Balance", enabled: true, source: .balanceDiff, conditions: conditions,
                              level: .important, push: true, cooldownSeconds: 30, mergeSeconds: nil)
        let rule = try await api.saveRule(input)
        XCTAssertEqual(rule.id, 12)
        XCTAssertEqual(rule.conditions.direction, .decrease)
        try await api.deleteSubscription("ABC")
    }

    func testAccountManagementRequests() async throws {
        let api = try client()
        let created = try await api.createAccount(AccountInput(id: "acc-1", proxyUrl: "socks5h://user:pass@proxy.example:1080",
                                                               timezone: "UTC", persona: "windows"))
        XCTAssertEqual(created.managed, true)
        XCTAssertEqual(created.timezone, "UTC")
        XCTAssertEqual(created.persona, "windows")
        let paused = try await api.saveAccount("acc-1", update: AccountUpdate(proxyRef: "proxy-1", status: .paused))
        XCTAssertEqual(paused.status, .paused)
        try await api.deleteAccount("acc-1")
        let login = try await api.startAccountLogin("acc-1")
        XCTAssertEqual(login.state, .starting)
        XCTAssertEqual(login.path, "/login-view/ticket-1/vnc.html?autoconnect=1")
        let state = try await api.accountLoginState("acc-1")
        XCTAssertEqual(state.state, .open)
        let finished = try await api.finishAccountLogin("acc-1")
        XCTAssertEqual(finished.status, .loggedIn)
    }

    func testAccountDecodesWithAndWithoutManagementFields() throws {
        let legacy = #"{"id":"old","proxy_ref":"p","status":"paused","pause_reason":"AuthRequired","available_at":12.5,"failures":3}"#
        let old = try APIClient.decoder().decode(Account.self, from: Data(legacy.utf8))
        XCTAssertEqual(old.id, "old")
        XCTAssertEqual(old.status, .paused)
        XCTAssertNil(old.managed)
        XCTAssertNil(old.timezone)
        XCTAssertNil(old.persona)
        let current = try APIClient.decoder().decode(Account.self, from: Data(accountFixture.utf8))
        XCTAssertEqual(current.managed, true)
        XCTAssertEqual(current.persona, "windows")
        let idle = try APIClient.decoder().decode(AccountLogin.self, from: Data(#"{"state":"none","path":null}"#.utf8))
        XCTAssertEqual(idle.state, .none)
        XCTAssertNil(idle.path)
    }

    func testUnauthorized() async throws {
        let api = try client(token: "invalid-fixture-token")
        do {
            _ = try await api.events()
            XCTFail("Expected a 401")
        } catch APIError.http(let status, let detail) {
            XCTAssertEqual(status, 401)
            XCTAssertEqual(detail, "Invalid bearer token")
        }
    }

    func testSocketRequestAndMessageDecoding() throws {
        let api = try client()
        let request = try api.webSocketRequest()
        XCTAssertEqual(request.url?.scheme, "wss")
        XCTAssertEqual(request.url?.path, "/fixture/ws/events")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture-token")
        let event = try EventStream.decode(.string(eventFixture))
        let binary = try EventStream.decode(.data(Data(eventFixture.utf8)))
        XCTAssertEqual(event.id, 7)
        XCTAssertEqual(event.data, binary.data)
        XCTAssertEqual(event.usd, 250)
        XCTAssertEqual(event.count, 2)
        XCTAssertGreaterThan(event.lastAt, event.createdAt)
        XCTAssertThrowsError(try EventStream.decode(.string("[]")))
    }
}
