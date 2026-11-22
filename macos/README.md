# Tokenwatch for macOS

Requirements: macOS 14 or newer and Swift 5.9 or newer.

- From `macos/`, run `swift build -c release` and `bash scripts/bundle.sh`.
- Open `.build/Tokenwatch.app` and enter the server URL and bearer token in Settings.
- Run `swift test` for core tests or use the [macOS workflow](../.github/workflows/macos.yml) for hosted builds.
