"""一次抓屏，把「通道顺序」和「判定结果」一起打出来，避免来回猜。

之前的排查是分两步做的（先看像素值、再看判定），两次抓屏之间页面可能
已经变了，导致结论自相矛盾。这里**用同一份数组**同时给出两边的证据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
from PIL import Image

import channels
import circles
import exam
from controller import build_controller, load_config
from maa.pipeline import JOCR, JRecognitionType
from maa.resource import Resource
from maa.tasker import Tasker

res = Resource()
res.post_bundle(str(Path("assets/resource").resolve())).wait()
res.post_ocr_model(str(Path("assets/resource/model/ocr").resolve())).wait()
ctrl = build_controller(load_config())
t = Tasker()
t.bind(res, ctrl)

raw = ctrl.post_screencap().wait().get()
print("数组:", raw.shape, raw.dtype)

Image.fromarray(np.ascontiguousarray(raw)).save("debug/snap/chan_raw.png")
print("已存 chan_raw.png —— 注意这是「BGR 数组直接当 RGB 存」的产物，")
print("颜色是反的（圆圈显示成橙色）。想看真实颜色要再转一次。")

j = t.post_recognition(JRecognitionType.OCR, JOCR(), raw)
rows = []
if j.wait().succeeded and j.get() is not None:
    td = j.get()
    for nid in td.node_id_list:
        nd = t.get_node_detail(nid)
        if nd is not None and nd.recognition is not None:
            for r in (nd.recognition.all_results or []):
                b = getattr(r, "box", None)
                if b is not None and getattr(r, "text", ""):
                    rows.append((str(r.text), int(b[0]), int(b[1]),
                                 int(b[2]), int(b[3])))
            break

print("页面:", exam.page_name(exam.detect_page(" ".join(r[0] for r in rows))),
      "| 文本块:", len(rows))
print()

# 抓屏是 BGR，先转成 RGB 再喂给 circles —— 这正是 `course.py` /
# `debug_view.py` 现在走的路径（`channels.to_rgb`）。
rgb = channels.to_rgb(raw)

for label, arr in (("原始 raw（BGR，错误用法）", raw),
                   ("经 channels.to_rgb（正确用法）", rgb)):
    print(f"--- {label} ---")
    n = 0
    for tx, _x, y, _w, _h in rows:
        if ".mp4" not in tx.lower():
            continue
        sh = circles.circle_shape(np.ascontiguousarray(arr), y)
        # 顺便看看这一行 x 25..52 的通道最大值，判断到底哪边是蓝
        blk = arr[max(0, y - 40):y + 26, 25:52].reshape(-1, 3).astype(int)
        top = blk[np.argsort(-(blk[:, 2] - blk[:, 0]))[0]]
        n += 1
        print(f"    {sh.state:8} 宽{sh.width:2d} 比例{sh.ratio:.2f} | "
              f"最蓝像素[{top[0]:3d},{top[1]:3d},{top[2]:3d}] "
              f"ch2-ch0={top[2] - top[0]:+4d} | {tx[:26]}")
    print(f"    共 {n} 节")
    print()
