using System;

namespace CelesteAI.Bridge;

/// <summary>
/// 模拟摇杆角度 → 摇杆向量（cos, sin）。不调用平台数学库（Windows 与 Linux 的 cosf/sinf 末位会不同）：
/// 用纯双精度四则运算（fdlibm 的区间约化与多项式）求值后转成单精度。预置模拟器用的是同一份算法，
/// 两边在各平台上逐位一致；结果与按双精度库函数求值再取单精度相同（只差不可能遇到的舍入边界）。
/// </summary>
internal static class StickTrig {
    private const double InvPio2 = 6.36619772367581382433e-01;
    private const double Pio2_1 = 1.57079632673412561417e+00;
    private const double Pio2_1t = 6.07710050650619224932e-11;
    private const double Pio2_2 = 6.07710050630396597660e-11;
    private const double Pio2_2t = 2.02226624879595063154e-21;

    private const double S1 = -1.66666666666666324348e-01, S2 = 8.33333333332248946124e-03,
        S3 = -1.98412698298579493134e-04, S4 = 2.75573137070700676789e-06,
        S5 = -2.50507602534068634195e-08, S6 = 1.58969099521155010221e-10;

    private const double C1 = 4.16666666666666019037e-02, C2 = -1.38888888888741095749e-03,
        C3 = 2.48015872894767294178e-05, C4 = -2.75573143513906633035e-07,
        C5 = 2.08757232129817482790e-09, C6 = -1.13596475577881948265e-11;

    /// <summary>角度（度，0 为右、90 为上）→ (cos, sin)；弧度先按 MathHelper.ToRadians 取单精度。</summary>
    public static void FromDegrees(float degrees, out float cos, out float sin) {
        float rad = (float)((double)degrees * (Math.PI / 180.0));
        CosSin(rad, out double c, out double s);
        cos = (float)c;
        sin = (float)s;
    }

    public static void CosSin(double x, out double cos, out double sin) {
        if (double.IsNaN(x) || double.IsInfinity(x)) {
            cos = sin = double.NaN;
            return;
        }
        int n;
        double y0, y1;
        if (Math.Abs(x) <= 7.85398163397448278999e-01) {
            n = 0;
            y0 = x;
            y1 = 0.0;
        } else {
            // 区间约化：x = n·π/2 + (y0 + y1)
            double fn = Math.Floor(x * InvPio2 + 0.5);
            double r = x - fn * Pio2_1;
            double w = fn * Pio2_1t;
            y0 = r - w;
            if (Math.Abs(y0) < 1e-3) {
                // 抵消较多时再取 π/2 的下一段
                double t = r;
                w = fn * Pio2_2;
                r = t - w;
                w = fn * Pio2_2t - ((t - r) - w);
                y0 = r - w;
            }
            y1 = (r - y0) - w;
            n = (int)(((long)fn) & 3);
        }
        double ks = KernelSin(y0, y1), kc = KernelCos(y0, y1);
        switch (n) {
            case 0: cos = kc; sin = ks; break;
            case 1: cos = -ks; sin = kc; break;
            case 2: cos = -kc; sin = -ks; break;
            default: cos = ks; sin = -kc; break;
        }
    }

    private static double KernelSin(double x, double y) {
        double z = x * x;
        double v = z * x;
        double r = S2 + z * (S3 + z * (S4 + z * (S5 + z * S6)));
        return x - ((z * (0.5 * y - v * r) - y) - v * S1);
    }

    private static double KernelCos(double x, double y) {
        double z = x * x;
        double w = z * z;
        double r = z * (C1 + z * (C2 + z * C3)) + w * w * (C4 + z * (C5 + z * C6));
        double hz = 0.5 * z;
        w = 1.0 - hz;
        return w + (((1.0 - w) - hz) + (z * r - x * y));
    }
}
