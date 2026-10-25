// Render printer-app/icon.svg to the PNG icons the server sends (transparent, square):
//   swiftc -O quartz/svg2png.swift -o build/svg2png && build/svg2png printer-app/icon.svg lbp6030/icons 512 128 48
import AppKit

let a = CommandLine.arguments
guard a.count >= 4, let img = NSImage(contentsOfFile: a[1]) else { print("usage: svg2png in.svg outdir size..."); exit(1) }
for s in a[3...] {
    let n = Int(s)!
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: n, pixelsHigh: n, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    img.draw(in: NSRect(x: 0, y: 0, width: n, height: n))
    NSGraphicsContext.restoreGraphicsState()
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: "\(a[2])/icon-\(n).png"))
}
