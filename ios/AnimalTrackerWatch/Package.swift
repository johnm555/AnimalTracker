// swift-tools-version: 5.9
// AnimalTrackerWatch — shared code for the watchOS app and its complication.
//
// SwiftPM builds only `AnimalTrackerCore` (models, API client, payload decoding,
// formatting) so it can be unit-tested on a Mac without Xcode. The `App/` and
// `Complication/` directories hold the watchOS app and WidgetKit extension
// sources; they are added to targets when the Xcode project is generated
// (see README.md) and are intentionally not SwiftPM targets.
import PackageDescription

let package = Package(
    name: "AnimalTrackerWatch",
    platforms: [
        .watchOS(.v10),
        .iOS(.v17),
        .macOS(.v14),   // host-side unit tests only
    ],
    products: [
        .library(name: "AnimalTrackerCore", targets: ["AnimalTrackerCore"]),
    ],
    targets: [
        .target(
            name: "AnimalTrackerCore",
            path: "Sources/AnimalTrackerCore"
        ),
        .testTarget(
            name: "AnimalTrackerCoreTests",
            dependencies: ["AnimalTrackerCore"],
            path: "Tests/AnimalTrackerCoreTests"
        ),
    ]
)
