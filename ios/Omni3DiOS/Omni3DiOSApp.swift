import SwiftUI

@main
struct Omni3DiOSApp: App {
    @StateObject private var store = ARStore()
    @State private var bridge: BridgeServer?

    var body: some Scene {
        WindowGroup {
            ContentView(store: store)
                .onAppear {
                    guard bridge == nil else { return }
                    let server = BridgeServer(store: store)
                    do { try server.start(); bridge = server }
                    catch { NSLog("Omni3D bridge failed: %@", error.localizedDescription) }
                }
        }
    }
}
