// Runs the printer app as a child process. Built into "LBP6030 Printer App.app" by install.sh, so macOS lists the
// background job and its Local Network permission under that name instead of zsh or Python.
// usage: launcher /path/to/lbp6030-printer-app PRINTER_IP ["Printer name"]
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: launcher /path/to/lbp6030-printer-app PRINTER_IP [name]\n".data(using: .utf8)!)
    exit(2)
}
let child = Process()
child.executableURL = URL(fileURLWithPath: args[1])
child.arguments = Array(args.dropFirst(2))

// launchd stops the job with SIGTERM: pass it on to the printer app.
var sources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { child.terminate() }
    source.resume()
    sources.append(source)
}
child.terminationHandler = { exit($0.terminationStatus) }
do {
    try child.run()
} catch {
    FileHandle.standardError.write("cannot start \(args[1]): \(error)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
