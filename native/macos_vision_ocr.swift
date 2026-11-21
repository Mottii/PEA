import CoreGraphics
import Foundation
import ImageIO
import Vision

struct OcrRequest: Decodable {
    let cmd: String?
    let image_path: String?
    let mode: String?
    let languages: [String]?
    let level: String?
}

struct OcrBoxPayload: Encodable {
    let text: String
    let confidence: Double
    let bbox_px: [Int]
    let bbox_norm: [Double]
}

struct OcrResponse: Encodable {
    let ok: Bool
    let elapsed_ms: Double
    let boxes: [OcrBoxPayload]
    let error: String?
}

func emitResponse(_ response: OcrResponse) {
    let encoder = JSONEncoder()
    if let data = try? encoder.encode(response),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

func loadCGImage(path: String) -> CGImage? {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        return nil
    }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

func performVision(
    cgImage: CGImage,
    level: VNRequestTextRecognitionLevel,
    languages: [String]
) throws -> [VNRecognizedTextObservation] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = level
    request.usesCPUOnly = true
    request.usesLanguageCorrection = false
    if !languages.isEmpty {
        request.recognitionLanguages = languages
    }
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    try handler.perform([request])
    return request.results ?? []
}

func clamp(_ value: Double, low: Double, high: Double) -> Double {
    return min(max(value, low), high)
}

func handleRequest(_ req: OcrRequest) -> OcrResponse {
    let start = DispatchTime.now().uptimeNanoseconds
    guard let imagePath = req.image_path, !imagePath.isEmpty else {
        return OcrResponse(ok: false, elapsed_ms: 0.0, boxes: [], error: "Missing image_path")
    }
    guard let cgImage = loadCGImage(path: imagePath) else {
        return OcrResponse(
            ok: false,
            elapsed_ms: 0.0,
            boxes: [],
            error: "Failed to load image at \(imagePath)"
        )
    }

    let languages = req.languages ?? ["ru-RU", "en-US"]
    let requestedFast = (req.level ?? "accurate").lowercased() == "fast"
    let primaryLevel: VNRequestTextRecognitionLevel = requestedFast ? .fast : .accurate

    var observations: [VNRecognizedTextObservation] = []
    do {
        observations = try performVision(cgImage: cgImage, level: primaryLevel, languages: languages)
        if observations.isEmpty && primaryLevel == .accurate {
            observations = try performVision(cgImage: cgImage, level: .fast, languages: languages)
        }
    } catch {
        if primaryLevel == .accurate {
            do {
                observations = try performVision(cgImage: cgImage, level: .fast, languages: languages)
            } catch let fallbackError {
                let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000.0
                return OcrResponse(
                    ok: false,
                    elapsed_ms: elapsedMs,
                    boxes: [],
                    error: "Vision OCR failed: \(fallbackError.localizedDescription)"
                )
            }
        } else {
            let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000.0
            return OcrResponse(
                ok: false,
                elapsed_ms: elapsedMs,
                boxes: [],
                error: "Vision OCR failed: \(error.localizedDescription)"
            )
        }
    }

    let width = Double(cgImage.width)
    let height = Double(cgImage.height)
    var boxes: [OcrBoxPayload] = []
    boxes.reserveCapacity(observations.count)

    for obs in observations {
        guard let candidate = obs.topCandidates(1).first else { continue }
        let trimmed = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { continue }

        let bb = obs.boundingBox
        let nx1 = clamp(Double(bb.minX), low: 0.0, high: 1.0)
        let nx2 = max(nx1, clamp(Double(bb.maxX), low: 0.0, high: 1.0))
        let ny1 = clamp(1.0 - Double(bb.maxY), low: 0.0, high: 1.0)
        let ny2 = max(ny1, clamp(1.0 - Double(bb.minY), low: 0.0, high: 1.0))

        let px1 = max(0, min(cgImage.width, Int((nx1 * width).rounded())))
        let px2 = max(px1, min(cgImage.width, Int((nx2 * width).rounded())))
        let py1 = max(0, min(cgImage.height, Int((ny1 * height).rounded())))
        let py2 = max(py1, min(cgImage.height, Int((ny2 * height).rounded())))

        boxes.append(
            OcrBoxPayload(
                text: trimmed,
                confidence: clamp(Double(candidate.confidence), low: 0.0, high: 1.0),
                bbox_px: [px1, py1, px2, py2],
                bbox_norm: [nx1, ny1, nx2, ny2]
            )
        )
    }

    let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000.0
    return OcrResponse(ok: true, elapsed_ms: elapsedMs, boxes: boxes, error: nil)
}

let decoder = JSONDecoder()
while let line = readLine(strippingNewline: true) {
    let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty {
        continue
    }
    guard let data = trimmed.data(using: .utf8),
          let req = try? decoder.decode(OcrRequest.self, from: data) else {
        emitResponse(OcrResponse(ok: false, elapsed_ms: 0.0, boxes: [], error: "Invalid JSON request"))
        continue
    }
    if req.cmd == "quit" {
        break
    }
    emitResponse(handleRequest(req))
}
