// swift-tools-version: 5.9
// WinstonWatch — shared code for the watchOS app and its complication.
//
// SwiftPM builds only `WinstonCore` (models, API client, payload decoding,
// formatting) so it can be unit-tested on a Mac without Xcode. The `App/` and
// `Complication/` directories hold the watchOS app and WidgetKit extension
// sources; they are added to targets when the Xcode project is generated
// (see README.md) and are intentionally not SwiftPM targets.
import PackageDescription

let package = Package(
    name: "WinstonWatch",
    platforms: [
        .watchOS(.v10),
        .iOS(.v17),
        .macOS(.v14),   // host-side unit tests only
    ],
    products: [
        .library(name: "WinstonCore", targets: ["WinstonCore"]),
    ],
    targets: [
        .target(
            name: "WinstonCore",
            path: "Sources/WinstonCore"
        ),
        .testTarget(
            name: "WinstonCoreTests",
            dependencies: ["WinstonCore"],
            path: "Tests/WinstonCoreTests"
        ),
    ]
)
