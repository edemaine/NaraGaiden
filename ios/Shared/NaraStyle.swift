import SwiftUI

struct NaraTimeColors {
    let bg: Color
    let fg: Color
}

enum NaraStyle {
    static func timeColors(beginDt: Int64?) -> NaraTimeColors {
        guard let beginDt else {
            return NaraTimeColors(bg: Color(white: 0.2), fg: Color(white: 0.95))
        }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        let deltaHours = max(0.0, Double(nowMs - beginDt) / 3600000.0)

        let stops: [(Double, (Double, Double, Double))] = [
            (1.0, (27, 94, 32)),
            (2.0, (133, 100, 18)),
            (3.0, (121, 69, 0)),
            (4.0, (122, 28, 28)),
        ]

        let rgb: (Double, Double, Double)
        if deltaHours <= 1.0 {
            rgb = stops[0].1
        } else if deltaHours >= 4.0 {
            rgb = stops.last!.1
        } else {
            var color = stops.last!.1
            for i in 0..<(stops.count - 1) {
                let (h0, c0) = stops[i]
                let (h1, c1) = stops[i + 1]
                if deltaHours <= h1 {
                    let t = (deltaHours - h0) / (h1 - h0)
                    color = (
                        c0.0 + (c1.0 - c0.0) * t,
                        c0.1 + (c1.1 - c0.1) * t,
                        c0.2 + (c1.2 - c0.2) * t
                    )
                    break
                }
            }
            rgb = color
        }

        return NaraTimeColors(
            bg: Color(red: rgb.0 / 255.0, green: rgb.1 / 255.0, blue: rgb.2 / 255.0),
            fg: Color.white
        )
    }
}
