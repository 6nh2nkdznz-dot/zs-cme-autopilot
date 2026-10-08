"""进度读取自检工具。

存在的理由：`main.py --ocr` 每跑一次都要新建 Resource + 加载 ppOCR 模型
（约 2 秒），而本平台播放器控件只显示约 3 秒就会自动隐藏。等 OCR 就绪时
画面已经变了，读数必然为空。

这个脚本在**同一个常驻进程内**完成「点一下唤起控件 → 立刻截图 → OCR」，
并把每次的截图落盘，方便肉眼核对「OCR 读数」和「当时画面」是否一致。

用法:
    python scripts/check_progress.py              # 连读 5 次，间隔 15s
    python scripts/check_progress.py -n 3 -i 30
    python scripts/check_progress.py --click 26,470 --verify
        # 点一下播放键，然后立刻前后各读一次，确认视频真的开始走了
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
from course import PLAYER_DUR_ROI, PLAYER_TIME_ROI, PLAY_TAP  # noqa: E402
from progress import _to_seconds, parse_progress  # noqa: E402
from snap import save_png  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "debug" / "progress-check"

# 实测值（MaaFramework 720x1280 画布）
CONTROL_TAP = (360, 200)      # 点视频区中央唤起控件
# ⚠️ 这两个 ROI **必须**从 course.py 引，别再在这里抄一份。
#
# 实测踩过：这里原来写的是 `TIME_ROI = (25, 480, 60, 25)`、
# `DUR_ROI = (570, 480, 50, 25)` —— 与 course.py 的实测值差了 20+ 像素，
# 于是**读数全是空**（实测 0/3 次解析成功），我一度以为是识别坏了，
# 其实是这个脚本自己的坐标是错的。
TIME_ROI = PLAYER_TIME_ROI
DUR_ROI = PLAYER_DUR_ROI


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--count", type=int, default=5, help="读取次数")
    parser.add_argument("-i", "--interval", type=float, default=15.0, help="间隔秒数")
    parser.add_argument("--save-every", action="store_true", default=True,
                        help="每次都落盘截图（默认开）")
    parser.add_argument("--click", default="",
                        help="验证模式：先读一次，再点 X,Y，再读一次，"
                             "看进度有没有往前走。例如 --click 26,470")
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
        """裁一块出来跑 OCR，返回识别到的文字。

        ## 取明细的正确路径（踩过坑）

        `post_recognition()` 返回的是一个**任务**，它的 `job_id` 是 **task_id**，
        不能拿去 `get_recognition_detail()` —— 那样**永远返回 None**，
        函数就静默返回空串。表现出来的现象是「读数一直为空」，
        很容易误判成「OCR 坏了」或「坐标不对」，于是去乱改坐标。

        正确路径是这条链条：

            job.get().node_id_list  →  逐 node 取 get_node_detail(nid)
                                    →  nd.recognition（这就是 RecognitionDetail，
                                       注意不是 `recognition_id`，也没有列表）

        这段逻辑和 `core.py` 的 `_screen_rows` 是同一套，改的时候一起改。
        """
        x, y, w, h = roi
        crop = img[y:y + h, x:x + w]
        job = tasker.post_recognition(JRecognitionType.OCR, JOCR(), crop).wait()
        if not job.succeeded:
            return ""
        td = job.get()
        for nid in (getattr(td, "node_id_list", None) or []):
            nd = tasker.get_node_detail(nid)
            detail = getattr(nd, "recognition", None) if nd is not None else None
            if detail is not None:
                return " ".join(
                    str(getattr(r, "text", "")) for r in (detail.all_results or [])
                )
        return ""

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    def read_once() -> tuple[str, str, object]:
        """唤起控件 → 截图 → OCR 当前时间与总长。"""
        controller.post_click(*CONTROL_TAP).wait()
        time.sleep(0.4)  # 控件淡入
        cap = controller.post_screencap().wait()
        if not cap.succeeded:
            return "", "", None
        img = cap.get()
        t_text = ocr_crop(img, TIME_ROI)
        d_text = ocr_crop(img, DUR_ROI)
        return t_text, d_text, img

    # --- 验证模式：点一下播放键，前后各读一次，看秒数有没有往前走 ---
    if args.click:
        cx, cy = (int(v) for v in args.click.split(","))
        print(f"[check] 验证模式：先确认点击前的读数，再点 ({cx},{cy})\n")
        before_t, before_d, img0 = read_once()
        if img0 is not None:
            save_png(img0, OUT_DIR / f"{stamp}_verify_before.png")
        before = _to_seconds(_digits(before_t))
        print(f"[check] 点之前: 当前 {before_t!r} 总长 {before_d!r}"
              f" → {before:.0f}s")

        print(f"[check] 点 ({cx},{cy})")
        controller.post_click(cx, cy).wait()
        time.sleep(args.interval)

        after_t, after_d, img1 = read_once()
        if img1 is not None:
            save_png(img1, OUT_DIR / f"{stamp}_verify_after.png")
        after = _to_seconds(_digits(after_t))
        print(f"[check] 点之后: 当前 {after_t!r} 总长 {after_d!r}"
              f" → {after:.0f}s")

        if before <= 0 or after <= 0:
            print("\n[check] ✗ 读数没解析出来，这次验证不算数")
            return 1
        if after > before:
            print(f"\n[check] ✓ 进度走了 {after - before:.0f}s —— "
                  f"这个坐标确实让视频开始播了")
            return 0
        print(f"\n[check] ✗ 进度没动（{before:.0f}s → {after:.0f}s）—— "
              f"这个坐标没让它播起来")
        return 1

    print(f"[check] 将读取 {args.count} 次，间隔 {args.interval:.0f}s\n")
    print(f"{'#':>3}  {'时间读数':<10} {'总长':<10} {'解析进度':<10} 截图")
    print("-" * 60)

    results = []
    for i in range(args.count):
        t_text, d_text, img = read_once()
        if img is None:
            print(f"{i+1:>3}  截图失败")
            continue

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


def _digits(text: str) -> str:
    """从 OCR 结果里挑出形如 12:34 / 1:02:33 的那一段。"""
    import re

    flat = (text or "").replace(" ", "")
    m = re.search(r"\d{1,2}:\d{2}(?::\d{2})?", flat)
    return m.group(0) if m else ""


if __name__ == "__main__":
    raise SystemExit(main())
