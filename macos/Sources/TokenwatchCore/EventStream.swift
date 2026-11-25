import Foundation

public enum StreamState: Equatable, Sendable {
    case connecting, connected, waiting(seconds: Int, reason: String)
}

public enum StreamMessage: Sendable {
    case event(Event), account(Account), accountDeleted(String), resync, ignored
}

private struct StreamEnvelope: Decodable {
    let message: StreamMessage
    private enum Keys: String, CodingKey { case type, data }
    private struct Deleted: Decodable { let id: String }
    private struct Empty: Decodable {}
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        switch try container.decode(String.self, forKey: .type) {
        case "event": message = .event(try container.decode(Event.self, forKey: .data))
        case "account": message = .account(try container.decode(Account.self, forKey: .data))
        case "account_deleted": message = .accountDeleted(try container.decode(Deleted.self, forKey: .data).id)
        case "resync": _ = try container.decode(Empty.self, forKey: .data); message = .resync
        default: message = .ignored
        }
    }
}

public struct EventStream: Sendable {
    public let client: APIClient

    public init(client: APIClient) { self.client = client }

    public static func decode(_ message: URLSessionWebSocketTask.Message) throws -> StreamMessage {
        let data: Data
        switch message {
        case .string(let text): data = Data(text.utf8)
        case .data(let bytes): data = bytes
        @unknown default: throw APIError.invalidResponse
        }
        return try APIClient.decoder().decode(StreamEnvelope.self, from: data).message
    }

    public func run(onState: @escaping @Sendable (StreamState) async -> Void,
                    onMessage: @escaping @Sendable (StreamMessage) async -> Void) async {
        var delay = 1
        while !Task.isCancelled {
            await onState(.connecting)
            let clock = ContinuousClock()
            let openedAt = clock.now
            do {
                let socket = client.session.webSocketTask(with: try client.webSocketRequest())
                defer { socket.cancel(with: .goingAway, reason: nil) }
                socket.resume()
                try await withTaskCancellationHandler {
                    try await Self.ping(socket)
                    await onState(.connected)
                    try await withThrowingTaskGroup(of: Void.self) { tasks in
                        tasks.addTask {
                            while !Task.isCancelled {
                                let message = try await socket.receive()
                                await onMessage(try Self.decode(message))
                            }
                        }
                        tasks.addTask {
                            while !Task.isCancelled {
                                try await Task.sleep(for: .seconds(25))
                                try await Self.ping(socket)
                            }
                        }
                        defer {
                            socket.cancel(with: .goingAway, reason: nil)
                            tasks.cancelAll()
                        }
                        _ = try await tasks.next()
                    }
                } onCancel: {
                    socket.cancel(with: .goingAway, reason: nil)
                }
                throw APIError.socketClosed
            } catch {
                guard !Task.isCancelled else { return }
                if openedAt.duration(to: clock.now) > .seconds(30) { delay = 1 }
                await onState(.waiting(seconds: delay, reason: error.localizedDescription))
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                delay = min(delay * 2, 60)
            }
        }
    }

    private static func ping(_ socket: URLSessionWebSocketTask) async throws {
        try await withThrowingTaskGroup(of: Void.self) { tasks in
            tasks.addTask {
                try await withCheckedThrowingContinuation { (reply: CheckedContinuation<Void, Error>) in
                    socket.sendPing { error in
                        if let error { reply.resume(throwing: error) }
                        else { reply.resume() }
                    }
                }
            }
            tasks.addTask {
                try await Task.sleep(for: .seconds(15))
                socket.cancel(with: .goingAway, reason: nil)
                throw URLError(.timedOut)
            }
            defer { tasks.cancelAll() }
            _ = try await tasks.next()
        }
    }
}
