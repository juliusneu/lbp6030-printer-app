// Render PDF pages to 8-bit gray PGM files (255 = white) with Quartz, without anti-aliasing unless "aa" is given.
// Used instead of poppler because poppler substitutes the regular face for non-embedded bold base fonts on macOS.
// usage: quartz_render in.pdf out-prefix dpi [aa]   -> out-prefix-001.pgm, ...   (built by lbp6030/render.py)
import Foundation
import CoreGraphics

func die(_ s: String) -> Never {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 4 else { die("usage: quartz_render in.pdf out-prefix dpi [aa]") }
let url = URL(fileURLWithPath: args[1]) as CFURL
let prefix = args[2]
let dpi = Double(args[3]) ?? 600.0
let aa = args.count > 4 && args[4] == "aa"
guard let doc = CGPDFDocument(url) else { die("cannot open PDF") }
if doc.isEncrypted && !doc.isUnlocked { die("PDF is encrypted") }

for pn in 1...max(1, doc.numberOfPages) {
    guard let page = doc.page(at: pn) else { continue }
    let box = page.getBoxRect(.cropBox)
    var wpt = Double(box.width), hpt = Double(box.height)
    if page.rotationAngle % 180 != 0 { swap(&wpt, &hpt) }
    let w = Int((wpt / 72.0 * dpi).rounded())
    let h = Int((hpt / 72.0 * dpi).rounded())
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceGray(),
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else { die("cannot create bitmap context") }
    ctx.setFillColor(gray: 1.0, alpha: 1.0)
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.setAllowsAntialiasing(aa)
    ctx.setShouldAntialias(aa)
    ctx.setAllowsFontSmoothing(false)
    ctx.setShouldSmoothFonts(false)
    ctx.interpolationQuality = .high
    // scale to the requested dpi, then let Quartz map the crop box (with /Rotate) onto the page in points
    ctx.scaleBy(x: CGFloat(w) / CGFloat(wpt), y: CGFloat(h) / CGFloat(hpt))
    let t = page.getDrawingTransform(.cropBox, rect: CGRect(x: 0, y: 0, width: CGFloat(wpt), height: CGFloat(hpt)),
                                     rotate: 0, preserveAspectRatio: true)
    ctx.concatenate(t)
    ctx.drawPDFPage(page)
    guard let data = ctx.data else { die("no bitmap data") }
    let bpr = ctx.bytesPerRow
    var out = "P5\n\(w) \(h)\n255\n".data(using: .ascii)!
    for y in 0..<h {
        out.append(Data(bytes: data.advanced(by: y * bpr), count: w))
    }
    let name = String(format: "%@-%03d.pgm", prefix, pn)
    do { try out.write(to: URL(fileURLWithPath: name)) } catch { die("cannot write \(name)") }
}
