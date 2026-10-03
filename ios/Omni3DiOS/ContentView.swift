import ARKit
import SwiftUI
import WebKit

struct ContentView: View {
    @ObservedObject var store: ARStore
    @AppStorage("omni3d.homeURL") private var homeURL = "http://127.0.0.1:50865/"
    @State private var address = ""
    @State private var editingAddress = false
    @State private var reloadID = 0

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button("重载") { reloadID += 1 }
                Spacer()
                Button("服务器") { address = homeURL; editingAddress = true }
                Button("AR 扫描") { _ = store.openScan() }
                    .disabled(!store.available)
            }
            .padding(12)
            WebPanel(url: URL(string: homeURL) ?? URL(string: "http://127.0.0.1:50865/")!)
                .id(reloadID)
        }
        .alert("服务器地址", isPresented: $editingAddress) {
            TextField("https://example.com/", text: $address)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
            Button("取消", role: .cancel) {}
            Button("保存") {
                if let url = URL(string: address), ["http", "https"].contains(url.scheme),
                   url.host != nil { homeURL = address; reloadID += 1 }
            }
        } message: { Text("输入 Omni3D 网页服务地址。") }
        .fullScreenCover(isPresented: $store.showScanner) {
            ScannerView(store: store)
        }
    }
}

private struct WebPanel: UIViewRepresentable {
    let url: URL
    func makeUIView(context: Context) -> WKWebView {
        let view = WKWebView(frame: .zero)
        view.load(URLRequest(url: url))
        return view
    }
    func updateUIView(_ view: WKWebView, context: Context) {
        if view.url != url { view.load(URLRequest(url: url)) }
    }
}

private struct CameraView: UIViewRepresentable {
    let view: ARSCNView
    func makeUIView(context: Context) -> ARSCNView { view }
    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}

private struct ScannerView: View {
    @ObservedObject var store: ARStore
    var body: some View {
        ZStack(alignment: .bottom) {
            CameraView(view: store.view).ignoresSafeArea()
            VStack(spacing: 18) {
                Text(store.tracking ? "ARKit 跟踪中 · \(store.frameCount) 帧" : "请缓慢移动手机以建立跟踪")
                    .font(.headline)
                HStack(spacing: 24) {
                    Button("拍照") { store.capture() }
                        .disabled(!store.tracking)
                    Button(store.recording ? "停止录制" : "连续采集") {
                        if store.recording { store.stopRecording() }
                        else { store.startRecording() }
                    }
                    .disabled(!store.tracking && !store.recording)
                    Button("完成") { store.finish() }
                    Button("关闭") { store.closeScan() }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding(16)
            .frame(maxWidth: .infinity)
            .background(.black.opacity(0.75))
            .foregroundStyle(.white)
        }
    }
}
