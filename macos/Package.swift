// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Tokenwatch",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "TokenwatchCore", targets: ["TokenwatchCore"]),
        .executable(name: "Tokenwatch", targets: ["Tokenwatch"]),
    ],
    targets: [
        .target(name: "TokenwatchCore"),
        .executableTarget(name: "Tokenwatch", dependencies: ["TokenwatchCore"]),
        .testTarget(name: "TokenwatchCoreTests", dependencies: ["TokenwatchCore"]),
    ]
)
