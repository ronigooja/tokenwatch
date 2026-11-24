import Foundation

public enum TokenMask {
    public static func display(_ token: String) -> String {
        guard token.count >= 8 else { return String(repeating: "•", count: token.count) }
        return String(repeating: "•", count: 8) + token.suffix(4)
    }
}

public enum TokenInput {
    public static func resolve(entered: String, displayedMask: String, savedToken: String?,
                               enteredAddress: String, savedAddress: String?) throws -> String {
        let trimmed = entered.trimmingCharacters(in: .whitespacesAndNewlines)
        if entered == displayedMask {
            guard enteredAddress == savedAddress, let savedToken else { throw APIError.addressChangedWithoutToken }
            return savedToken
        }
        if trimmed.isEmpty {
            guard enteredAddress == savedAddress, let savedToken else {
                throw APIError.addressChangedWithoutToken
            }
            return savedToken
        }
        return trimmed
    }
}
