// 결과 카드 상단 이미지(2:1)를 라벨별로 만든다. macOS에서 실행한다.
//
// 카카오 응답 형식은 글자색·배경색을 지정할 수 없다. 발표 자료 목업의
// 신호등 색 머리 부분을 이미지로 대신한다.
//
//   swift scripts/make_verdict_images.swift src/healthcheck/api/static/verdict
import AppKit

struct Style { let file: String; let label: String; let dot: NSColor; let background: NSColor }

func rgb(_ hex: Int) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
}

let styles = [
    Style(file: "trustworthy", label: "믿을 만해요", dot: rgb(0x3DA35D), background: rgb(0xE6F4EA)),
    Style(file: "caution", label: "조심하세요", dot: rgb(0xF2A900), background: rgb(0xFFF4D6)),
    Style(file: "warning", label: "주의하세요", dot: rgb(0xD93A30), background: rgb(0xFBE7E5)),
    Style(file: "unknown", label: "알 수 없어요", dot: rgb(0x8A9099), background: rgb(0xEEF0F3)),
]

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "."
let width = 800, height = 400

for style in styles {
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    style.background.setFill()
    NSRect(x: 0, y: 0, width: width, height: height).fill()

    // 신호등 원. 좌표 원점은 왼쪽 아래다.
    let diameter: CGFloat = 120
    let dotRect = NSRect(x: (CGFloat(width) - diameter) / 2, y: 205, width: diameter, height: diameter)
    style.dot.setFill()
    NSBezierPath(ovalIn: dotRect).fill()

    let font = NSFont(name: "AppleSDGothicNeo-Bold", size: 84) ?? NSFont.boldSystemFont(ofSize: 84)
    let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: rgb(0x1F2328)]
    let text = NSAttributedString(string: style.label, attributes: attributes)
    let size = text.size()
    text.draw(at: NSPoint(x: (CGFloat(width) - size.width) / 2, y: 70))

    NSGraphicsContext.restoreGraphicsState()
    let path = "\(outDir)/\(style.file).png"
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
    print(path)
}
