import ARKit
import Combine
import CoreImage
import SceneKit

struct CapturedFrame {
    let jpeg: Data
    let pose: [Float] // column-major camera-to-world, metres
    let intrinsics: [Float] // row-major 3x3
}

final class ARStore: NSObject, ObservableObject, ARSessionDelegate {
    @Published var showScanner = false
    @Published private(set) var tracking = false
    @Published private(set) var recording = false
    @Published private(set) var finished = false
    @Published private(set) var frameCount = 0

    let view = ARSCNView(frame: .zero)
    let available = ARWorldTrackingConfiguration.isSupported
    private let context = CIContext()
    private var timer: Timer?
    private(set) var frames: [CapturedFrame] = []
    private(set) var points: [SIMD3<Float>] = []
    private(set) var pose: [Float] = Array(repeating: 0, count: 16)

    override init() {
        super.init()
        view.session.delegate = self
        view.session.delegateQueue = .main
        view.automaticallyUpdatesLighting = false
        pose[0] = 1; pose[5] = 1; pose[10] = 1; pose[15] = 1
    }

    @discardableResult func openScan() -> Bool {
        guard available else { return false }
        reset()
        showScanner = true
        view.session.run(ARWorldTrackingConfiguration(), options: [.resetTracking, .removeExistingAnchors])
        return true
    }

    func closeScan() {
        stopRecording()
        view.session.pause()
        tracking = false
        showScanner = false
    }

    func startRecording() {
        guard showScanner, tracking else { return }
        recording = true
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in
            self?.capture()
        }
    }

    func stopRecording() {
        timer?.invalidate()
        timer = nil
        recording = false
    }

    func finish() {
        stopRecording()
        finished = true
        closeScan()
    }

    func reset() {
        stopRecording()
        frames.removeAll()
        points.removeAll()
        frameCount = 0
        finished = false
    }

    func capture() {
        guard let frame = view.session.currentFrame,
              case .normal = frame.camera.trackingState else { return }
        // Keep the pixel buffer in sensor orientation, matching ARCamera.intrinsics.
        let image = CIImage(cvPixelBuffer: frame.capturedImage)
        guard let jpeg = context.jpegRepresentation(of: image,
                                                    colorSpace: CGColorSpaceCreateDeviceRGB(),
                                                    options: [:]) else { return }
        let c = frame.camera.transform.columns
        let pose: [Float] = [c.0.x, c.0.y, c.0.z, c.0.w,
                             c.1.x, c.1.y, c.1.z, c.1.w,
                             c.2.x, c.2.y, c.2.z, c.2.w,
                             c.3.x, c.3.y, c.3.z, c.3.w]
        let k = frame.camera.intrinsics.columns
        let intrinsics: [Float] = [k.0.x, k.1.x, k.2.x,
                                   k.0.y, k.1.y, k.2.y,
                                   k.0.z, k.1.z, k.2.z]
        frames.append(CapturedFrame(jpeg: jpeg, pose: pose, intrinsics: intrinsics))
        frameCount = frames.count
        if let cloud = frame.rawFeaturePoints, points.count < 100_000 {
            points.append(contentsOf: cloud.points.prefix(100_000 - points.count))
        }
    }

    func pointCloudPLY() -> Data? {
        guard !points.isEmpty else { return nil }
        var data = Data("ply\nformat binary_little_endian 1.0\nelement vertex \(points.count)\nproperty float x\nproperty float y\nproperty float z\nend_header\n".utf8)
        for point in points {
            for coordinate in [point.x, point.y, point.z] {
                var bits = coordinate.bitPattern.littleEndian
                withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
            }
        }
        return data
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        tracking = { if case .normal = frame.camera.trackingState { return true }; return false }()
        let c = frame.camera.transform.columns
        pose = [c.0.x, c.0.y, c.0.z, c.0.w,
                c.1.x, c.1.y, c.1.z, c.1.w,
                c.2.x, c.2.y, c.2.z, c.2.w,
                c.3.x, c.3.y, c.3.z, c.3.w]
    }
}
