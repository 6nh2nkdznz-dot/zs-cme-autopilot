"""进度读取自检工具。

存在的理由：`main.py --ocr` 每跑一次都要新建 Resource + 加载 ppOCR 模型
（约 2 秒），而本平台播放器控件只显示约 3 秒就会自动隐藏。等 OCR 就绪时
画面已经变了，读数必然为空。

这个脚本在**同一个常驻进程内**完成「点一下唤起控件 → 立刻截图 → OCR」，
并把每次的截图落盘，方便肉眼核对「OCR 读数」和「当时画面」是否一致。

用法:
    python scripts/check_progress.py              # 连读 5 次，间隔 15s
    python scripts/check_progress.py -n 3 -i 30
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from controller import ConfigError, build_controller, load_config  # noqa: E402
from progress import parse_progress  # noqa: E402
from snap import save_png  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "debug" / "progress-check"

# 实测值（MaaFramework 720x1280 画布）
CONTROL_TAP = (360, 200)      # 点视频区中央唤起控件
TIME_ROI = (25, 480, 60, 25)  # 左下角「4:51」
DUR_ROI = (570, 480, 50, 25)  # 右下角「43:02」


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--count", type=int, default=5, help="读取次数")
    parser.add_argument("-i", "--interval", type=float, default=15.0, help="间隔秒数")
    parser.add_argument("--save-every", action="store_true", default=True,
                        help="每次都落盘截图（默认开）")
    args = parser.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    # 复用同一个 Tasker，只建一次 OCR —— 这是这个脚本存在的意义
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    if not resource.post_bundle(str(ROOT / "assets" / "resource")).wait().succeeded:
        print("[FATAL] 资源加载失败", file=sys.stderr)
        return 1
    if not resource.post_ocr_model(
        str(ROOT / "assets" / "resource" / "model" / "ocr")
    ).wait().succeeded:
        print("[FATAL] OCR 模型加载失败", file=sys.stderr)
        return 1

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    from maa.pipeline import JOCR, JRecognitionType

    def ocr_crop(img, roi) -> str:
        x, y, w, h = roi
        crop = img[y:y + h, x:x + w]
        job = tasker.post_recognition(JRecognitionType.OCR, JOCR(), crop).wait()
        if not job.succeeded:
            return ""
        detail = tasker.get_recognition_detail(job.job_id)
        if detail is None:
            # 退回走临时节点（post_recognition 的 id 语义与 reco_id 不同）
            return ""
        return " ".join(
            str(getattr(r, "text", "")) for r in (detail.all_results or [])
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"[check] 将读取 {args.count} 次，间隔 {args.interval:.0f}s\n")
    print(f"{'#':>3}  {'时间读数':<10} {'总长':<10} {'解析进度':<10} 截图")
    print("-" * 60)

    results = []
    for i in range(args.count):
        controller.post_click(*CONTROL_TAP).wait()
        time.sleep(0.4)  # 控件淡入
        cap = controller.post_screencap().wait()
        if not cap.succeeded:
            print(f"{i+1:>3}  截图失败")
            continue

        img = cap.get()
        t_text = ocr_crop(img, TIME_ROI)
        d_text = ocr_crop(img, DUR_ROI)

        png = OUT_DIR / f"{stamp}_{i+1:02d}.png"
        save_png(img, png)

        # 把当前时间和总长拼起来，复用已验证的解析器
        combined = f"{t_text} / {d_text}"
        reading = parse_progress(combined)
        pct = f"{reading.percent:.1f}%" if reading else "(解析失败)"

        print(f"{i+1:>3}  {t_text:<10} {d_text:<10} {pct:<10} {png.name}")
        results.append((t_text, d_text, pct))

        if i + 1 < args.count:
            time.sleep(args.interval)

    print()
    ok = sum(1 for _, _, p in results if p != "(解析失败)")
    print(f"[check] {ok}/{len(results)} 次解析成功")
    if ok < len(results):
        print("[提示] 若读数为空，看落盘截图确认控件当时是否可见；")
        print("       控件只显示约 3 秒，这是本平台最容易踩的时序坑。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
