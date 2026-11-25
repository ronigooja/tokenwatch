import Foundation

public enum BoundedWait {
    /// Returns when `work` finishes or `timeout` elapses, whichever comes first. `work` is not cancelled on timeout.
    public static func wait(for work: Task<Void, Never>, within timeout: Duration) async {
        let timer = Task {
            do { try await Task.sleep(for: timeout) } catch {}
        }
        Task {
            await work.value
            timer.cancel()
        }
        await timer.value
    }
}
