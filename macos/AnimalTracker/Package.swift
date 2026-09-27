// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "AnimalTrackerMac", platforms: [.macOS(.v14)], products: [
    .executable(name: "AnimalTracker", targets: ["AnimalTrackerDesktop"])
], targets: [
    .target(name: "DesktopCore"),
    .executableTarget(name: "AnimalTrackerDesktop", dependencies: ["DesktopCore"]),
    .testTarget(name: "DesktopCoreTests", dependencies: ["DesktopCore"])
])
