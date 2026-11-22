import Foundation

public enum StreamState: Equatable, Sendable {
    case connecting, connected, waiting(seconds: Int, reason: String)
}

public struct EventStream: Sendable {
    public let client: APIClient

    public init(client: APIClient) { self.client = client }

    public static func decode(_ message: URLSessionWebSocketTask.Message) throws -> Event {
        let data: Data
        switch message {
        case .string(let text): data = Data(text.utf8)
        case .data(let bytes): data = bytes
        @unknown default: throw APIError.invalidResponse
        }
        return try APIClient.decoder().decode(Event.self, from: data)
    }

    public func run(onState: @escaping @Sendable (StreamState) async -> Void,
                    onEvent: @escaping @Sendable (Event) async -> Void) async {
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
                                await onEvent(try Self.decode(message))
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
