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
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let query = components?.queryItems ?? []
        let encodedPath = components?.percentEncodedPath ?? ""
        switch (request.httpMethod, url.path) {
        case ("GET", "/fixture/transfers"):
            let names = Set(query.map(\.name))
            let expected: Set<String> = ["coin_id", "start", "end", "min_usd", "max_usd", "direction", "entity",
                                         "address", "chain", "txhash", "offset", "limit"]
            let valid = authorized && names == expected && query.contains(.init(name: "entity", value: "A+B & C")) &&
                url.absoluteString.contains("A%2BB") && query.contains(.init(name: "offset", value: "100")) &&
                query.contains(.init(name: "coin_id", value: fixtureCoinID))
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
            let valid = authorized && keys(payload) == ["email", "password", "proxy_url"] &&
                payload?["email"] as? String == "user@example.com" && payload?["password"] as? String == "fixture-password" &&
                payload?["proxy_url"] as? String == "socks5h://user:pass@proxy.example:1080"
            status = valid ? 201 : 422
            body = valid ? accountFixture : #"{"detail":"Invalid account"}"#
        case ("PUT", "/fixture/accounts/acc-1"):
            let payload = bodyObject()
            let pause = keys(payload) == ["status"] && payload?["status"] as? String == "paused"
            let edit = keys(payload) == ["password", "proxy_url"] && payload?["password"] as? String == "new-fixture-password" &&
                payload?["proxy_url"] as? String == "socks5h://user:pass@proxy.example:1081"
            status = authorized && (pause || edit) ? 200 : 422
            body = pause ? accountFixture.replacingOccurrences(of: #""status":"active""#, with: #""status":"paused""#)
                : edit ? accountFixture.replacingOccurrences(of: #""proxy_url":"socks5h://user:pass@proxy.example:1080""#,
                                                             with: #""proxy_url":"socks5h://user:pass@proxy.example:1081""#)
                : "{}"
        case ("DELETE", "/fixture/accounts/acc-1") where authorized:
            status = 204
            body = ""
        case ("POST", "/fixture/accounts/acc-1/login") where authorized:
            let payload = bodyObject()
            let page = payload?["page"] as? String
            let valid = keys(payload) == ["page"] && (page == "login" || page == "register")
            status = valid ? 201 : 422
            body = valid ? #"{"state":"starting","path":"/login-view/ticket-1/vnc.html?autoconnect=1"}"# : #"{"detail":"Invalid page"}"#
        case ("GET", "/fixture/accounts/acc-1/login") where authorized:
            status = 200
            body = #"{"state":"open","path":"/login-view/ticket-1/vnc.html?autoconnect=1"}"#
        case ("POST", "/fixture/accounts/acc-1/login/finish") where authorized:
            status = 200
            body = #"{"status":"logged_in"}"#
        case ("POST", "/fixture/accounts/user+a@example.com/login") where authorized && encodedPath == emailLoginPath:
            status = 201
            body = #"{"state":"starting","path":"/login-view/ticket-2/vnc.html?autoconnect=1"}"#
        case ("POST", "/fixture/accounts/user+a@example.com/login/finish") where authorized && encodedPath == emailLoginPath + "/finish":
            status = 200
            body = #"{"status":"logged_in"}"#
        case ("PUT", "/fixture/accounts/user+a@example.com") where authorized && encodedPath == emailAccountPath:
            status = keys(bodyObject()) == ["status"] ? 200 : 422
            body = status == 200 ? accountFixture : #"{"detail":"Invalid update"}"#
        case ("DELETE", "/fixture/accounts/user+a@example.com") where authorized && encodedPath == emailAccountPath:
            status = 204
            body = ""
        case ("GET", "/fixture/coins"):
            let valid = authorized && query == [.init(name: "status", value: "no_match")]
            status = valid ? 200 : 422
            body = valid ? "[\(coinFixture)]" : #"{"detail":"Invalid coin filter"}"#
        case ("GET", "/fixture/coins/\(fixtureCoinID)") where authorized && encodedPath == coinPath("coins", fixtureCoinID):
            status = 200
            body = coinFixture
        case ("GET", "/fixture/coins/native:bitcoin/mapping") where authorized &&
            encodedPath == coinPath("coins", "native:bitcoin", "mapping"):
            status = 404
            body = #"{"detail":"Resource not found"}"#
        case ("PUT", "/fixture/coins/native:bitcoin/mapping") where authorized &&
            encodedPath == coinPath("coins", "native:bitcoin", "mapping"):
            let valid = keys(bodyObject()) == ["pricing_id"] && bodyObject()?["pricing_id"] as? String == "bitcoin-fixture"
            status = valid ? 200 : 422
            body = valid ? #"{"pricing_id":"bitcoin-fixture"}"# : #"{"detail":"Invalid mapping"}"#
        case ("DELETE", "/fixture/coins/native:bitcoin/mapping") where authorized &&
            encodedPath == coinPath("coins", "native:bitcoin", "mapping"):
            status = 204
            body = ""
        case ("PUT", "/fixture/exclusions/\(fixtureCoinID)") where authorized && encodedPath == coinPath("exclusions", fixtureCoinID):
            let valid = keys(bodyObject()) == ["reason"] && bodyObject()?["reason"] as? String == "fixture reason"
            status = valid ? 200 : 422
            body = valid ? #"{"coin_id":"\#(fixtureCoinID)","reason":"fixture reason"}"# : #"{"detail":"Invalid exclusion"}"#
        case ("DELETE", "/fixture/exclusions/\(fixtureCoinID)") where authorized && encodedPath == coinPath("exclusions", fixtureCoinID):
            status = 204
            body = ""
        case ("PUT", "/fixture/coin-catalog/futures-mappings/1000SHIB") where authorized:
            let valid = bodyObject()?["symbol"] as? String == "SHIB"
            status = valid ? 200 : 422
            body = valid ? #"{"symbol":"SHIB","base":"1000SHIB"}"# : #"{"detail":"Invalid mapping"}"#
        case ("DELETE", "/fixture/coin-catalog/futures-mappings/1000SHIB") where authorized:
            status = 204
            body = ""
        case ("PUT", "/fixture/coin-catalog/exclusion-rules/tag/bStocks") where authorized:
            let valid = keys(bodyObject()) == ["reason"] && bodyObject()?["reason"] as? String == "fixture reason"
            status = valid ? 200 : 422
            body = valid ? #"{"kind":"tag","value":"bStocks","reason":"fixture reason"}"# : #"{"detail":"Invalid rule"}"#
        case ("DELETE", "/fixture/coin-catalog/exclusion-rules/tag/bStocks") where authorized:
            status = 204
            body = ""
        case ("GET", "/fixture/coin-catalog/chains") where authorized:
            status = 200
            body = #"[{"alpha_chain_id":"56","arkham_chains":["bsc","ethereum"],"conflict":true}]"#
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

    private func keys(_ payload: [String: Any]?) -> Set<String> {
        Set((payload ?? [:]).keys)
    }
}

private let emailAccountPath = "/fixture/accounts/user+a@example.com"
private let emailLoginPath = emailAccountPath + "/login"
private let fixtureCoinID = "alpha:56:0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"

/// Expected percent-encoded request path below the fixture base; `:` and `~` are literal path characters.
private func coinPath(_ parts: String...) -> String {
    "/fixture/" + parts.joined(separator: "/")
}

private let coinFixture = #"{"id":"\#(fixtureCoinID)","symbol":"FOO","pricing_id":null,"status":"no_match","reason":null,"sources":["alpha"],"deployments":[{"feed":"alpha","chain":"BSC","address":"0x1f9840a85d5af5bf1d1762f925bdaddc4201f984","deposit_enabled":true,"withdraw_enabled":null}],"matched_chains":[]}"#

private let accountFixture = #"{"id":"acc-1","email":"user@example.com","password":"fixture-password","proxy_url":"socks5h://user:pass@proxy.example:1080","status":"active","pause_reason":null,"available_at":0,"failures":0,"managed":true,"timezone":"UTC","persona":"windows"}"#

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
        filter.coinId = fixtureCoinID
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
        let created = try await api.createAccount(AccountInput(email: "user@example.com", password: "fixture-password",
                                                               proxyUrl: "socks5h://user:pass@proxy.example:1080"))
        XCTAssertEqual(created.managed, true)
        XCTAssertEqual(created.label, "user@example.com")
        XCTAssertEqual(created.password, "fixture-password")
        XCTAssertEqual(created.proxyUrl, "socks5h://user:pass@proxy.example:1080")
        XCTAssertEqual(created.timezone, "UTC")
        XCTAssertEqual(created.persona, "windows")
        let paused = try await api.saveAccount("acc-1", update: AccountUpdate(status: .paused))
        XCTAssertEqual(paused.status, .paused)
        let edited = try await api.saveAccount("acc-1", update: AccountUpdate(
            proxyUrl: "socks5h://user:pass@proxy.example:1081", password: "new-fixture-password"))
        XCTAssertEqual(edited.proxyUrl, "socks5h://user:pass@proxy.example:1081")
        try await api.deleteAccount("acc-1")
        let login = try await api.startAccountLogin("acc-1")
        XCTAssertEqual(login.state, .starting)
        XCTAssertEqual(login.path, "/login-view/ticket-1/vnc.html?autoconnect=1")
        let register = try await api.startAccountLogin("acc-1", page: .register)
        XCTAssertEqual(register.state, .starting)
        let state = try await api.accountLoginState("acc-1")
        XCTAssertEqual(state.state, .open)
        let finished = try await api.finishAccountLogin("acc-1")
        XCTAssertEqual(finished.status, .loggedIn)
    }

    func testEmailAccountIDPaths() async throws {
        let api = try client()
        let id = "user+a@example.com"
        let login = try await api.startAccountLogin(id)
        XCTAssertEqual(login.state, .starting)
        let finished = try await api.finishAccountLogin(id)
        XCTAssertEqual(finished.status, .loggedIn)
        let saved = try await api.saveAccount(id, update: AccountUpdate(status: .paused))
        XCTAssertEqual(saved.id, "acc-1")
        try await api.deleteAccount(id)
    }

    func testInvalidAccountIDPathsAreRejected() async throws {
        let api = try client()
        for id in ["..", "a..b", "a/b", ".hidden", "a b", "a\\b"] {
            do {
                _ = try await api.accountSession(id)
                XCTFail("Expected \(id) to be rejected")
            } catch APIError.invalidPath {
            }
        }
    }

    func testAccountDecodesWithAndWithoutManagementFields() throws {
        let legacy = #"{"id":"old","proxy_ref":"p","status":"paused","pause_reason":"AuthRequired","available_at":12.5,"failures":3}"#
        let old = try APIClient.decoder().decode(Account.self, from: Data(legacy.utf8))
        XCTAssertEqual(old.id, "old")
        XCTAssertEqual(old.label, "old")
        XCTAssertEqual(old.status, .paused)
        XCTAssertNil(old.email)
        XCTAssertNil(old.password)
        XCTAssertNil(old.proxyUrl)
        XCTAssertNil(old.managed)
        XCTAssertNil(old.timezone)
        XCTAssertNil(old.persona)
        XCTAssertNil(old.sessionStatus)
        XCTAssertNil(old.sessionCheckedAt)
        let current = try APIClient.decoder().decode(Account.self, from: Data(accountFixture.utf8))
        XCTAssertEqual(current.managed, true)
        XCTAssertEqual(current.label, "user@example.com")
        XCTAssertEqual(current.persona, "windows")
        XCTAssertNil(current.sessionStatus)
        XCTAssertNil(current.sessionCheckedAt)
        let idle = try APIClient.decoder().decode(AccountLogin.self, from: Data(#"{"state":"none","path":null}"#.utf8))
        XCTAssertEqual(idle.state, .none)
        XCTAssertNil(idle.path)
    }

    func testCoinRequestsKeepCoinRefCharacters() async throws {
        let api = try client()
        let coin = try await api.coin(fixtureCoinID)
        XCTAssertEqual(coin.id, fixtureCoinID)
        XCTAssertEqual(coin.status, .noMatch)
        XCTAssertEqual(coin.deployments.first?.feed, .alpha)
        let unmapped = try await api.coinMapping("native:bitcoin")
        XCTAssertNil(unmapped)
        let mapping = try await api.saveCoinMapping("native:bitcoin", pricingId: "bitcoin-fixture")
        XCTAssertEqual(mapping.pricingId, "bitcoin-fixture")
        try await api.deleteCoinMapping("native:bitcoin")
        let exclusion = try await api.saveExclusion(fixtureCoinID, reason: "fixture reason")
        XCTAssertEqual(exclusion.coinId, fixtureCoinID)
        try await api.deleteExclusion(fixtureCoinID)
        let rows = try await api.coins(.noMatch)
        XCTAssertEqual(rows.first?.symbol, "FOO")
    }

    func testCoinIDPathsRejectUnsafeValues() async throws {
        let api = try client()
        let ids = ["a/b", "..", "a..b", "a b", "a\\b", ".hidden", "a\tb", ":leading", String(repeating: "a", count: 257)]
        for id in ids {
            do {
                _ = try await api.coin(id)
                XCTFail("Expected \(id) to be rejected")
            } catch APIError.invalidPath {
            }
        }
    }

    func testCatalogRequests() async throws {
        let api = try client()
        let mapping = try await api.saveFuturesMapping("1000SHIB", symbol: "SHIB")
        XCTAssertEqual(mapping.base, "1000SHIB")
        try await api.deleteFuturesMapping("1000SHIB")
        let rule = try await api.saveCatalogExclusion(.tag, value: "bStocks", reason: "fixture reason")
        XCTAssertEqual(rule.kind, .tag)
        XCTAssertEqual(rule.id, "tag:bStocks")
        try await api.deleteCatalogExclusion(.tag, value: "bStocks")
        let chains = try await api.chainObservations()
        XCTAssertEqual(chains.first?.conflict, true)
        XCTAssertEqual(chains.first?.arkhamChains, ["bsc", "ethereum"])
    }

    func testCoinDecodesCurrentShapeAndUnknownStatus() throws {
        let coin = try APIClient.decoder().decode(Coin.self, from: Data(coinFixture.utf8))
        XCTAssertEqual(coin.status, .noMatch)
        XCTAssertNil(coin.pricingId)
        XCTAssertNil(coin.reason)
        XCTAssertNil(coin.deployments.first?.withdrawEnabled)
        XCTAssertEqual(coin.deployments.first?.depositEnabled, true)
        XCTAssertEqual(coin.chains, ["BSC"])
        let future = #"{"id":"native:bitcoin","symbol":"BTC","pricing_id":"bitcoin","status":"supported","reason":null,"sources":["spot"],"deployments":[{"feed":"spot","chain":"bitcoin","address":null,"deposit_enabled":null,"withdraw_enabled":null}],"matched_chains":[]}"#
        let native = try APIClient.decoder().decode(Coin.self, from: Data(future.utf8))
        XCTAssertEqual(native.status, .supported)
        XCTAssertEqual(native.pricingId, "bitcoin")
        XCTAssertEqual(native.chains, ["bitcoin"])
        XCTAssertEqual(native.status.label, "Supported")
        let unknown = #"{"id":"native:x","symbol":"X","pricing_id":null,"status":"brand_new_state","reason":null,"sources":[],"deployments":[],"matched_chains":[]}"#
        let decoded = try APIClient.decoder().decode(Coin.self, from: Data(unknown.utf8))
        XCTAssertEqual(decoded.status, .unknown("brand_new_state"))
        XCTAssertEqual(decoded.status.rawValue, "brand_new_state")
        XCTAssertEqual(decoded.status.label, "brand_new_state")
    }

    func testJobDecodesLegacyAndNullableAccount() throws {
        let legacy = #"{"id":4,"kind":"transfers","account_id":"acc-1","coin_id":"native:bitcoin","options":{"balance_min":null},"interval_seconds":60,"enabled":true,"next_run":0,"wait_reason":null}"#
        let job = try APIClient.decoder().decode(Job.self, from: Data(legacy.utf8))
        XCTAssertEqual(job.accountId, "acc-1")
        XCTAssertNil(job.waitReason)
        let anyAccount = #"{"id":5,"kind":"daily","account_id":null,"coin_id":null,"options":{"balance_min":100},"interval_seconds":3600,"enabled":true,"next_run":1700000000.0,"wait_reason":"No active account"}"#
        let pooled = try APIClient.decoder().decode(Job.self, from: Data(anyAccount.utf8))
        XCTAssertNil(pooled.accountId)
        XCTAssertEqual(pooled.waitReason, "No active account")
        XCTAssertNil(pooled.lastError)
    }

    func testJobDecodesLastError() throws {
        let failed = #"{"id":6,"kind":"transfers","account_id":"acc-1","coin_id":"native:bitcoin","options":{"balance_min":null},"interval_seconds":60,"enabled":true,"next_run":0,"wait_reason":null,"last_error":"HTTP 502"}"#
        let job = try APIClient.decoder().decode(Job.self, from: Data(failed.utf8))
        XCTAssertEqual(job.lastError, "HTTP 502")
        XCTAssertNil(job.waitReason)
        let cleared = #"{"id":6,"kind":"transfers","account_id":"acc-1","coin_id":"native:bitcoin","options":{"balance_min":null},"interval_seconds":60,"enabled":true,"next_run":0,"wait_reason":null,"last_error":null}"#
        let ok = try APIClient.decoder().decode(Job.self, from: Data(cleared.utf8))
        XCTAssertNil(ok.lastError)
    }

    func testJobInputEncodesNullAccount() throws {
        let input = JobInput(kind: .coinCatalog, accountId: nil, coinId: nil, options: JobOptions(balanceMin: nil),
                             intervalSeconds: 3600, enabled: true)
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(input)) as? [String: Any])
        XCTAssertTrue(object["account_id"] is NSNull)
        XCTAssertNil(object["coin_id"])
        XCTAssertEqual(object["kind"] as? String, "coin_catalog")
    }

    func testAccountDecodesCachedSessionFields() throws {
        let cached = #"{"id":"cached","status":"active","available_at":0,"failures":0,"session_status":"logged_in","session_checked_at":1700000000.5}"#
        let logged = try APIClient.decoder().decode(Account.self, from: Data(cached.utf8))
        XCTAssertEqual(logged.sessionStatus, .loggedIn)
        XCTAssertEqual(logged.sessionCheckedAt, 1700000000.5)
        let empty = #"{"id":"cached","status":"active","available_at":0,"failures":0,"session_status":null,"session_checked_at":null}"#
        let unset = try APIClient.decoder().decode(Account.self, from: Data(empty.utf8))
        XCTAssertNil(unset.sessionStatus)
        XCTAssertNil(unset.sessionCheckedAt)
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
