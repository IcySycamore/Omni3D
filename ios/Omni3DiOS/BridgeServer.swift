import Foundation
import Network
import UIKit
import UniformTypeIdentifiers

private struct BridgeReply {
    var data: Data
    var type = "application/json"
    var status = 200
    var filename: String? = nil

    static func json(_ value: [String: Any], status: Int = 200) -> BridgeReply {
        BridgeReply(data: (try? JSONSerialization.data(withJSONObject: value)) ?? Data("{}".utf8),
                    status: status)
    }
}

/// The web panel already calls this loopback API on Android. Keep the iOS contract identical.
final class BridgeServer: NSObject, UIDocumentPickerDelegate {
    private let store: ARStore
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "com.omni3d.ios.bridge")
    private var pendingPick: ((BridgeReply) -> Void)?

    init(store: ARStore) { self.store = store; super.init() }

    func start() throws {
        let port = NWEndpoint.Port(rawValue: 50687)!
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: NWEndpoint.Host("127.0.0.1"), port: port)
        let listener = try NWListener(using: parameters, on: port)
        listener.newConnectionHandler = { [weak self] connection in self?.accept(connection) }
        listener.start(queue: queue)
        self.listener = listener
    }

    func stop() { listener?.cancel() }

    private func accept(_ connection: NWConnection) {
        connection.start(queue: queue)
        read(connection, buffer: Data())
    }

    private func read(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] bytes, _, done, error in
            guard let self, error == nil else { connection.cancel(); return }
            var data = buffer
            if let bytes { data.append(bytes) }
            guard data.count <= 32 * 1024 * 1024 else { connection.cancel(); return }
            let separator = Data("\r\n\r\n".utf8)
            if let range = data.range(of: separator),
               let header = String(data: data[..<range.lowerBound], encoding: .utf8) {
                let lines = header.components(separatedBy: "\r\n")
                let request = lines.first?.split(separator: " ").map(String.init) ?? []
                let length = lines.dropFirst().first { $0.lowercased().hasPrefix("content-length:") }
                    .flatMap { Int($0.split(separator: ":", maxSplits: 1).last?.trimmingCharacters(in: .whitespaces) ?? "") } ?? 0
                guard request.count >= 2, length >= 0, length <= 32 * 1024 * 1024 else {
                    connection.cancel(); return
                }
                let bodyStart = range.upperBound
                if data.count >= bodyStart + length {
                    let body = data.subdata(in: bodyStart..<(bodyStart + length))
                    DispatchQueue.main.async {
                        if request[1] == "/ar/file/pick" {
                            self.pickFile { reply in self.respond(connection, reply) }
                            return
                        }
                        let reply = self.route(method: request[0], path: request[1], body: body)
                        self.respond(connection, reply)
                    }
                    return
                }
            }
            if done { connection.cancel() } else { self.read(connection, buffer: data) }
        }
    }

    private func respond(_ connection: NWConnection, _ reply: BridgeReply) {
        let reason = reply.status == 200 ? "OK" : reply.status == 204 ? "No Content" : "Not Found"
        var headers = "HTTP/1.1 \(reply.status) \(reason)\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nAccess-Control-Expose-Headers: X-Filename\r\nContent-Type: \(reply.type)\r\nContent-Length: \(reply.data.count)\r\nConnection: close\r\n"
        if let name = reply.filename { headers += "X-Filename: \(name)\r\n" }
        headers += "\r\n"
        var packet = Data(headers.utf8)
        packet.append(reply.data)
        connection.send(content: packet, completion: .contentProcessed { _ in connection.cancel() })
    }

    private func pickFile(_ completion: @escaping (BridgeReply) -> Void) {
        guard pendingPick == nil,
              let root = UIApplication.shared.connectedScenes.compactMap({
                  ($0 as? UIWindowScene)?.keyWindow?.rootViewController
              }).first else {
            completion(.json(["ok": false, "error": "文件选择器不可用"]))
            return
        }
        pendingPick = completion
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.movie, .image])
        picker.delegate = self
        root.present(picker, animated: true)
    }

    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        guard let completion = pendingPick else { return }
        pendingPick = nil
        guard let url = urls.first else {
            completion(.json(["ok": false, "error": "未选择文件"]))
            return
        }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else {
            completion(.json(["ok": false, "error": "无法读取文件"]))
            return
        }
        let name = url.lastPathComponent.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? "media"
        completion(BridgeReply(data: data, type: "application/octet-stream", filename: name))
    }

    func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
        pendingPick?(.json(["ok": false, "error": "已取消"]))
        pendingPick = nil
    }

    private func route(method: String, path: String, body: Data) -> BridgeReply {
        if method == "OPTIONS" { return BridgeReply(data: Data(), status: 204) }
        if path == "/ar/health" { return .json(["ok": true]) }
        if path == "/ar/status" {
            return .json(["ok": true, "ready": true, "tracking": store.tracking, "scale": 1,
                          "provider": "apple-arkit"])
        }
        if path == "/ar/pose" {
            return .json(["ok": true, "pose": store.pose, "tracking": store.tracking])
        }
        if path == "/ar/history" && method == "GET" {
            return .json(["ok": true, "tasks": UserDefaults.standard.array(forKey: "omni3d.history") ?? []])
        }
        if path == "/ar/history" && method == "POST" {
            if let task = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                var tasks = UserDefaults.standard.array(forKey: "omni3d.history") ?? []
                tasks.insert(task, at: 0)
                UserDefaults.standard.set(Array(tasks.prefix(50)), forKey: "omni3d.history")
            }
            return .json(["ok": true])
        }
        if path.hasPrefix("/ar/file/save") && method == "POST" {
            let requested = URLComponents(string: "http://localhost" + path)?
                .queryItems?.first(where: { $0.name == "name" })?.value ?? "model.ply"
            let filename = URL(fileURLWithPath: requested).lastPathComponent
            guard !filename.isEmpty, filename != ".", filename != "..",
                  let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
                return .json(["ok": false, "error": "无效文件名"])
            }
            do {
                try body.write(to: documents.appendingPathComponent(filename), options: .atomic)
                return .json(["ok": true, "path": "文件 App/Omni3D/\(filename)"])
            } catch {
                return .json(["ok": false, "error": "保存失败"])
            }
        }
        if path == "/ar/scan/start" && method == "POST" {
            let started = store.openScan()
            return .json(["ok": started, "started": started,
                          "error": started ? "" : "ARKit is unavailable on this device"])
        }
        if path == "/ar/scan/settings" && method == "POST" { return .json(["ok": true]) }
        if path == "/ar/scan/capture" && method == "POST" {
            store.capture()
            return .json(["ok": true, "frameCount": store.frameCount])
        }
        if path == "/ar/scan/finish" && method == "POST" {
            store.finish()
            return .json(["ok": true, "frameCount": store.frameCount])
        }
        if path == "/ar/scan/stop" && method == "POST" {
            store.stopRecording()
            var result: [String: Any] = ["ok": true, "frameCount": store.frameCount,
                                         "scale": 1, "tracking": store.tracking]
            if let first = store.frames.first {
                result["pose0"] = first.pose
                result["intrinsics0"] = first.intrinsics
            }
            return .json(result)
        }
        if path == "/ar/scan/status" {
            return .json(["ok": true, "available": store.available, "scanning": store.recording,
                          "finished": store.finished, "frameCount": store.frameCount,
                          "pointCloudCount": store.points.count, "tracking": store.tracking,
                          "provider": "apple-arkit", "scale": 1])
        }
        if path == "/ar/scan/data" {
            return .json(["ok": true, "frameCount": store.frameCount, "scale": 1,
                          "poses": store.frames.map(\.pose),
                          "intrinsics": store.frames.map(\.intrinsics)])
        }
        if path == "/ar/scan/reset" && method == "POST" {
            store.reset()
            return .json(["ok": true])
        }
        if path.hasPrefix("/ar/scan/frames/"),
           let index = Int(path.dropFirst("/ar/scan/frames/".count)),
           store.frames.indices.contains(index) {
            return BridgeReply(data: store.frames[index].jpeg, type: "image/jpeg")
        }
        if path == "/ar/scan/pointcloud", let ply = store.pointCloudPLY() {
            return BridgeReply(data: ply, type: "application/octet-stream", filename: "arkit_pointcloud.ply")
        }
        return .json(["ok": false, "error": "not found"], status: 404)
    }
}
