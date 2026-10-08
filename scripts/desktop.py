# -*- coding: utf-8 -*-
"""桌面版（浏览器 + 电脑模式）的导航与看课。

## 为什么要另起一个模块，而不是改 `run_exam_watch.py`

手机版和桌面版**页面完全是两套**：

| | 手机版（微信 WebView） | 桌面版（浏览器 + 电脑模式） |
|---|---|---|
| 入口 | 微信聊天里的链接 → 底部 tab | 顶部导航 `我的学习` |
| 课程列表 | 一行一张卡，`去学习` 在卡片里 | 左侧栏 + 主区列表，`去学习` 在每行 |
| 课程页 | 平台自己的域名（`elearning`） | **另一个域名**（`course.zs-hospital.sh.cn`） |
| 播放器 | 保利威，控件是自己画的 | `<video>`（video.js），能用 JS 直接控 |
| 切换讲次 | 靠点坐标 | 左侧 `course_chapter_item` 列表，能按文本点 |

所以手机版那套 `roi` / 点击坐标**一条都不能复用**。硬塞进同一条管线只会
让两边都变得难改。这里单独一套，手机版保持原样 —— 也方便随时退回去。

## 这里的核心思路：**用页面自己的结构，不用像素**

手机版之所以要靠 OCR + 硬编码坐标，是因为微信的 WebView 里问不出 DOM。
桌面版不一样：CDP 能直接问出元素的位置和文案。所以这里：

* **找东西**用 `find_text` / `JS` 按文本或选择器找；
* **点击**用 `Input.dispatchMouseEvent` 发**真鼠标事件**（Vue 的
  handler 只认这种，`element.click()` 实测点不动 —— 见 `DEVELOPMENT.md` 7.7）；
* **判断进度**直接读 `<video>.currentTime / duration`，不用 OCR 认时间；
* OCR 只留给「确认页面确实到了」这种兜底判断。

代价是**每一步都要问浏览器**（一次 CDP 往返几十毫秒），比点坐标慢；
但页面一改版，改的是选择器、不是坐标，维护成本低得多。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import paths

# ---------------------------------------------------------------- 常量

#: 平台主站（登录、我的学习）
ELEARN = "https://elearning.zs-hospital.sh.cn"
#: 课程站（课程页、播放器）—— 注意是**另一个域名**
COURSE = "https://course.zs-hospital.sh.cn"
#: 「我的学习」页面。实测 `module=learning`；「购课清单」是 `module=order`
PERSONAL = f"{ELEARN}/learning/personalCenter?module=learning&tabIndex=1"
#: 登录页
LOGIN = f"{ELEARN}/learning/login"

#: 默认看到「视频课件已完成」的百分比就到顶了。
#: 平台自己判「完成」的粒度没那么细，97% 和 100% 一样算完成；
#: 留 3% 是因为有的视频最后一秒会卡住，死等 100% 会白等到超时。
TARGET_PERCENT = 97.0

#: 单节课最多看多久（秒）。录播课最长见过 90 分钟，留一倍余量。
LESSON_MAX_SECONDS = 4 * 3600

#: 轮询间隔（秒）。读一次 currentTime 很便宜，但没必要太密。
POLL_SECONDS = 15.0

#: 一节课「卡住」的定义：连续这么多次轮询 currentTime 几乎不动。
STALL_ROUNDS = 4

#: 看课时的临时弹题：选项文案 → 要点的答案。
#: 平台的学习状态检测题，提示语里写着「如需继续学习，请选择A」。
QUIZ_ANSWER = "A"

#: 等登录时多久查一次（秒）。
LOGIN_POLL_SECONDS = 5.0


# ------------------------------------------------------------ 停止钩子

#: 界面注入的「该不该停」回调。见 `set_stop_check()`。
#:
#: ## 为什么做成模块级的钩子，而不是给每个函数加参数
#:
#: 看课这条路是 `界面 → desktop_runner → desktop_watch → Session.watch_video`
#: 四层往下传，而「停下来」是**每一层都要问**的事：外面在等一门课，里面在等
#: 一讲，最里面在等一轮 15 秒的轮询。把 `should_stop` 当参数一层层加下去，
#: 每一层都得记住往下传，漏一层就在那一层变成「点了停止没反应」——
#: 手机版那条路当初正是这么踩的（`post_stop()` 只在节点边界生效）。
#:
#: 所以做成一个进程级的钩子：谁都能问，谁都不用往下传。
_stop_check: "Callable[[], bool] | None" = None


def set_stop_check(fn: "Callable[[], bool] | None") -> None:
    """装上/卸下停止判据。`fn()` 返回 True 表示「别再往下做了」。

    界面在开跑前装上（通常是 `lambda: core.stopped`），跑完在 `finally`
    里卸掉 —— **必须卸**，否则下一次跑会带着上一次那个已经置位的判据，
    一启动就立刻停。
    """
    global _stop_check
    _stop_check = fn


def should_stop() -> bool:
    """现在是不是被要求停下。没装钩子时恒为 False。

    回调抛异常一律当作「不停」：这个函数是在几小时的看护循环**每一轮**
    里调的，让它因为一个回调故障把整门课崩掉，比多跑一轮糟糕得多。
    """
    if _stop_check is None:
        return False
    try:
        return bool(_stop_check())
    except Exception:  # noqa: BLE001 - 见 docstring：回调坏了不该崩掉看护
        return False


def _quiet(_msg: str) -> None:
    pass


def _first(pattern: str, text: str, default: str = "") -> str:
    """正则第一个捕获组；没命中返回 `default`（地址栏里抠参数用）。"""
    m = re.search(pattern, text or "")
    return m.group(1) if m else default


# ---------------------------------------------------------------- JS 片段

#: 按文本找元素并返回它的**视口坐标**。返回 `"null"` 表示没找到。
#:
#: 为什么要先 `scrollIntoView` 再量：列表长了之后目标在屏幕外，
#: 量出来的 y 是负的，点上去就落到别的元素上了。
FIND_JS = r"""
(() => {
  const want = __T__;
  const norm = s => (s || '').replace(/\s+/g, '');
  const pool = [...document.querySelectorAll(
    'li,div,span,a,button,td,label,em,i,p,h1,h2,h3,h4')];
  // 先找「文本正好等于」的**最深的那个**元素，避免命中整个容器
  let hit = null;
  for (const e of pool) {
    if (norm(e.textContent) !== want) continue;
    if (hit === null || e.contains(hit) === false) hit = e;
    if (e.children.length === 0) { hit = e; break; }
  }
  if (!hit) return 'null';
  hit.scrollIntoView({block: 'center', inline: 'center'});
  const r = hit.getBoundingClientRect();
  return JSON.stringify({
    x: r.x + r.width / 2, y: r.y + r.height / 2,
    left: Math.round(r.x), top: Math.round(r.y),
    w: Math.round(r.width), h: Math.round(r.height),
    tag: hit.tagName, cls: String(hit.className).slice(0, 50),
  });
})()
"""

#: 在一门课自己的卡片里找某个按钮，并返回它的**视口坐标**。
#:
#: 为什么不能直接按文本找：列表里每门课都有一个「去学习」，
#: `find_text('去学习')` 拿到的是**第一门课**的（实测踩到：想点「肝胆」
#: 那门课，结果点开了第一门「老年认知症」）。所以必须先把范围收进
#: 目标课程的卡片，再在卡片里找按钮。
#:
#: 卡片怎么认：从「含课程名的、最内层的、高度不超过 400px 的容器」往上
#: 找到那个同时含有「去学习」的盒子 —— 那个就是一行。
FIND_IN_COURSE_JS = r"""
(() => {
  const want = __NAME__, button = __BTN__;
  const norm = s => (s || '').replace(/\s+/g, '');
  // 1) 找到那张课程卡：文本含课程名、且含按钮文字的最小盒子
  let card = null;
  for (const e of document.querySelectorAll('div,li,section,article')) {
    const t = norm(e.textContent);
    if (!t.includes(want) || !t.includes(button)) continue;
    const r = e.getBoundingClientRect();
    if (r.height <= 0 || r.height > 500) continue;
    if (card === null || e.contains(card)) card = e;
  }
  if (!card) return 'null';
  // 2) 在卡片里找按钮本身（取最深的那个）
  let hit = null;
  for (const e of card.querySelectorAll('span,div,a,button')) {
    if (norm(e.textContent) !== button) continue;
    if (hit === null || e.contains(hit)) hit = e;
    if (e.children.length === 0) { hit = e; break; }
  }
  if (!hit) hit = card;
  // 3) 滚到可见处再量 —— 列表长了之后不滚会量到屏幕外
  hit.scrollIntoView({block: 'center', inline: 'center'});
  const r = hit.getBoundingClientRect();
  return JSON.stringify({
    x: r.x + r.width / 2, y: r.y + r.height / 2,
    left: Math.round(r.x), top: Math.round(r.y),
    w: Math.round(r.width), h: Math.round(r.height),
    name: norm(card.textContent).slice(0, 60), tag: hit.tagName,
    cls: String(hit.className).slice(0, 50),
  });
})()
"""

#: 当前页面的视频状态。没有 `<video>` 返回 `null`。
VIDEO_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return 'null';
  return JSON.stringify({
    dur: v.duration || 0, cur: v.currentTime || 0,
    paused: !!v.paused, ended: !!v.ended, ready: v.readyState,
    rate: v.playbackRate || 1,
    src: String(v.currentSrc || v.src || '').slice(0, 120),
  });
})()
"""

#: 播放器**当前认的**那一讲，以及它内部那份学习记录。
#:
#: 为什么要专门读这个：平台的上报逻辑（每 60 秒一条
#: `sendVideoLearnRecord`、每 300 秒一条 `sendLearnTime`）是围着
#: `courseLearnCoursewareConfig.activeItemObj` 转的，**不是**围着地址栏
#: 转的。2026-10-08 实测：改 hash 之后地址栏已经是第 9 讲、`<video>` 的
#: src 也换了，可 `activeItemObj` 还停在第 1 讲 —— 于是这 45 分钟里
#: 服务端涨的是**第 1 讲**的账（`studyTime` 到 6665），第 9 讲一动不动。
#: 判断"到底在看哪一讲"只能看这里。
#:
#: `learnRecordObj` 里几个字段的实测含义：
#:   * `completeStatus` —— 播放器自己认的 0/1/2；
#:   * `recordTimeDelay` —— 上报周期，实测 `60`（秒）；
#:   * `learnTime` / `totalTime` —— 内部累计的已学秒数，"已播秒数"是
#:     **乘着倍速**累加的，所以它和服务端的 `studyTime` 对不上是正常的
#:     （实测 `totalTime=6667` 而服务端 `studyTime=6665`，两个口径）。
ACTIVE_JS = r"""
(() => {
  if (typeof angular === 'undefined') return 'null';
  const v = document.querySelector('video');
  if (!v) return 'null';
  let node = angular.element(v).scope();
  const out = {hash_item_id: '', id: '', name: '', status: '', position: 0,
               rate: v.playbackRate || 1, paused: !!v.paused, cur: 0, dur: 0,
               complete_status: '', learn_time: 0, total_time: 0};
  out.cur = v.currentTime || 0;
  out.dur = v.duration || 0;
  out.hash_item_id = (location.hash.match(/itemId=([0-9a-zA-Z]+)/) || ['', ''])[1];
  let depth = 0;
  while (node && depth < 10) {
    const cfg = node.courseLearnCoursewareConfig;
    if (cfg && cfg.activeItemObj && cfg.activeItemObj.id) {
      out.id = cfg.activeItemObj.id;
      out.name = String(cfg.activeItemObj.name || '').slice(0, 60);
      out.status = String(cfg.activeItemObj.status);
      const lr = (cfg.activeItemObj.learnRecordObj) || {};
      out.position = lr.lastExitPosition || 0;
      break;
    }
    node = node.$parent;
    depth++;
  }
  // 播放器自己的记录（`customObj.videoPlayPosition` 是当前播放位置）
  depth = 0;
  node = angular.element(v).scope();
  while (node && depth < 4) {
    if (node.learnRecordObj) {
      out.complete_status = String(node.learnRecordObj.completeStatus);
      out.learn_time = node.learnRecordObj.learnTime || 0;
      out.total_time = node.learnRecordObj.totalTime || 0;
      out.record_time_delay = node.learnRecordObj.recordTimeDelay || 0;
    }
    if (node.customObj && node.customObj.videoPlayPosition) {
      out.play_position = node.customObj.videoPlayPosition;
    }
    node = node.$parent;
    depth++;
  }
  return JSON.stringify(out);
})()
"""

#: 让当前视频从头播起来。返回设置后的状态。
#:
#: ⚠ **桌面版拿不到倍速，1× 是唯一速度**（2026-10-08 实测，三层证据）：
#:   1. 直接设 `playbackRate` 1.25 / 1.5 / 1.75 / 2 / 2.5 / 4 —— **每个值
#:      都被立刻按回 1**，6 秒真实时间视频只走 6.0 秒（`_drate5.py`）；
#:   2. 20ms 观测器看到 `ratechange` 事件序列永远是 `2 -> 1 -> 1`
#:      —— 平台有两处代码在同时复位（`_drate4.py`）；
#:   3. 即使完全不碰页面、把助推也停掉，那串 `2 -> 1` 照样每 500ms 出现，
#:      说明复位是**平台自己在做**，不是我们没顶到。
#: 客户端里就是 `whatyFunctionUitl.getCookie(j.PLAYBACK_RATE)` /
#: `showPlaybackRate` 为假时强制 `e.playbackRate(1)` 那套。
#: **别再试倍速了：一讲 55 分钟就是要播 55 分钟。**
#:
#: 那 `ratechange` 还要不要派发：要。播放器内部"累加已播秒数"是**乘着
#: 倍速加**的（`o += parseFloat(j)`，`j` 从 `ratechange` 里取），整页重载
#: 之后这个事件没再触发过，累加器就涨不到上报阈值（60 秒），**平台自己
#: 的 `sendVideoLearnRecord` 一次都不发**，服务端 `studyTime` 冻住、
#: `completeStatus` 永远停在 1（实测：本地播了 20 多分钟，服务端一动不动）。
#: 派发一次能让播放器把 `j` 重新读成当前倍速，累加器恢复。
PLAY_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return 'null';
  if (__RESET__) { try { v.currentTime = 0; } catch (e) {} }
  v.playbackRate = __R__;
  try { v.dispatchEvent(new Event('ratechange')); } catch (e) {}
  const p = v.play();
  if (p && p.catch) p.catch(() => {});
  return JSON.stringify({rate: v.playbackRate, cur: v.currentTime,
                         dur: v.duration, paused: v.paused});
})()
"""

#: 页面内部的小助推：**只负责"别停"**（以及可选把播放头钉在末尾附近）。
#:
#: ⚠ **桌面版拿不到倍速，1× 是唯一速度**（2026-10-08 实测，见 `_drate5.py`）：
#: 页面里每 500ms 把 `playbackRate` 设成 1.25 / 1.5 / 1.75 / 2 / 2.5 / 4，
#: 用 20ms 的观测器盯着 —— **每一个值都被立刻按回 1**，`ratechange`
#: 事件序列永远是 `2 -> 1 -> 1`，6 秒真实时间视频永远只走 6.0 秒。
#: 那串 `2 -> 1 -> 1` 是平台两处代码同时复位（不是我们没顶到）。
#: 所以：**别再试倍速了**，一讲 55 分钟就是要播 55 分钟。
#:
#: 那为什么还留着这个助推：播放器被整页重建之后 `paused` 会停在 true
#: （实测 `cur` 死死停在 0，`play()` 调了也不动），页内定时器比 Python
#: 15 秒一轮的轮询快得多，能立刻把它捞回来。
#:
#: `tail > 0` 时把播放头钉在 `dur - __TAIL__`：本地是否"播到末尾"对服务端
#: 没有意义（它只看累计时长），留着是为了"整段都播过"这件事有个交代。
BOOST_JS = r"""
(() => {
  const KEY = '__dshBoost';
  if (window[KEY]) { clearInterval(window[KEY]); window[KEY] = null; }
  const rate = __R__;
  const tail = __TAIL__;
  const tick = () => {
    const v = document.querySelector('video');
    if (!v) return;
    if (rate && Math.abs(v.playbackRate - rate) > 0.01) {
      try { v.playbackRate = rate; } catch (e) {}
    }
    if (v.paused && !v.ended) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
    if (tail > 0 && v.duration && v.currentTime < v.duration - tail) {
      try { v.currentTime = v.duration - tail; } catch (e) {}
    }
  };
  tick();
  window[KEY] = setInterval(tick, 500);
  return JSON.stringify({armed: true, rate: rate, tail: tail,
                         cur: (document.querySelector('video') || {}).currentTime || 0});
})()
"""

#: 撤掉倍速助推，交还给平台自己。
STOP_BOOST_JS = r"""
(() => {
  const KEY = '__dshBoost';
  if (window[KEY]) { clearInterval(window[KEY]); window[KEY] = null; }
  return 'ok';
})()
"""

#: "谁按了暂停" —— 挂一个 pause 事件监听，把调用栈记下来。
#:
#: 为什么需要：实测第 9 讲切过去之后视频每 30 秒自己停一次，但页面上
#: 找不出任何可见的浮层（没有 layer、没有 dialog）。平台的暂停一定是
#: 自己的 JS 干的，光看 DOM 看不出来，得让它在暂停的那一刻把调用栈
#: 交出来。栈里带的函数名就是答案。
SNITCH_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v || v.__dshSnitch) return 'already';
  v.__dshSnitch = true;
  window.__dshPauseLog = [];
  v.addEventListener('pause', () => {
    const e = new Error('paused');
    window.__dshPauseLog.push({t: Date.now(), cur: v.currentTime,
                               stack: String(e.stack || '').slice(0, 600)});
  });
  v.addEventListener('play', () => {
    window.__dshPauseLog.push({t: Date.now(), cur: v.currentTime, play: true});
  });
  return 'armed';
})()
"""

#: 取回"谁按了暂停"的记录。
PAUSE_LOG_JS = r"""
JSON.stringify(window.__dshPauseLog || [])
"""

#: 调一次 `play()`，并把它的 **Promise 结果**写进 `window.__dshPlay`。
#:
#: 为什么非要看这个 Promise：`play()` 被**拒绝**的时候**不会派发 `pause`
#: 事件**，所以从外面看就是"视频没在播、但也没人按过暂停、页面上一个浮层
#: 都没有" —— 2026-10-08 第 10 讲就是这么卡的，`cur` 恒为 63 秒二十多秒
#: 不动，`readyState=4`（早就缓冲完了）、`error=null`、`__dshPauseLog`
#: 是空的，很容易误判成"平台把视频按停了"。
#:
#: 抓到的原文（`debug/_dclick4.py`）：
#:
#:   `NotAllowedError: play() failed because the user didn't interact
#:    with the document first.`
#:
#: 这是 Chrome 的自动播放策略：**整页载入**会清掉这个文档的"用户激活"
#: 状态（`enter_course()` 走的正是 `Page.navigate`），所以刚进第 10 讲
#: 那一下 `play()` 必被拒；而第 9 讲是上一轮用户点过的文档，就没这问题。
PLAY_CHECK_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return 'no-video';
  window.__dshPlay = 'pending';
  let p;
  try { p = v.play(); } catch (e) { window.__dshPlay = 'THREW: ' + e; return window.__dshPlay; }
  if (p && p.then) {
    p.then(() => { window.__dshPlay = 'RESOLVED'; })
     .catch((e) => { window.__dshPlay = (e && e.name) + ': ' +
                     String((e && e.message) || '').slice(0, 90); });
  } else {
    window.__dshPlay = 'no-promise';
  }
  // 顺便把「这一刻的状态」也交出来。**Promise 是异步落定的**
  // （正在缓冲时既不 resolve 也不 reject），早几毫秒去读只会读到
  // `pending`；判断"到底播起来没有"最硬的证据还是 `paused`。
  return JSON.stringify({res: (p && p.then) ? 'pending' : window.__dshPlay,
                         paused: v.paused, ended: v.ended,
                         cur: +v.currentTime.toFixed(1),
                         dur: +(v.duration || 0).toFixed(1),
                         ready: v.readyState,
                         err: v.error ? (v.error.code + ':' + v.error.message) : null});
})()
"""

#: 取回上一次 `play()` 的 Promise 结果。
PLAY_RESULT_JS = r"""String(window.__dshPlay || '')"""

#: 视频上方那行「本次学习 00分07秒　总计时长 73分11秒」。
#:
#: 实测 DOM（`debug/_probe_time2.py` 原样 dump）：
#:
#:     <div class="video_learn_info">
#:       <span>本次学习</span>
#:       <span class="time_text ng-binding"
#:             ng-bind="learnRecordObj.learnTime | timeToText">00分07秒</span>
#:       <span>总计时长</span>
#:       <span class="time_text ng-binding"
#:             ng-bind="learnRecordObj.totalTime | timeToText">73分11秒</span>
#:     </div>
#:
#: 外两层是 `.col-xs-6 > .video_learn_info`，就在视频上方（y≈233，
#: 视频 y≈264）。**总计时长是账号级的**，不是这一讲的。
#:
#: 两条路都读：scope 上的秒数（准，`totalTime=4391` 就是 73分11秒）优先，
#: 读不到再解析那两行文字。文字那条是给"页面改版把 scope 挖断了"留的后路。
STUDY_TIME_JS = r"""
(() => {
  const out = {learn: null, total: null, learn_text: '', total_text: '', err: ''};
  const info = document.querySelector('.video_learn_info');
  if (info) {
    const ts = info.querySelectorAll('.time_text');
    if (ts.length >= 1) out.learn_text = (ts[0].innerText || '').trim();
    if (ts.length >= 2) out.total_text = (ts[1].innerText || '').trim();
  }
  try {
    let el = info, scope = null, depth = 0;
    while (el && !scope && depth < 8) {
      const s = (typeof angular !== 'undefined') ? angular.element(el).scope() : null;
      if (s && s.learnRecordObj) scope = s;
      el = el.parentElement; depth++;
    }
    if (scope) {
      out.learn = scope.learnRecordObj.learnTime;
      out.total = scope.learnRecordObj.totalTime;
      out.complete_status = String(scope.learnRecordObj.completeStatus);
      out.show_hint = !!scope.learnRecordObj.showCompleteHint;
    }
  } catch (e) { out.err = String(e && e.message || e); }
  return JSON.stringify(out);
})()
"""

#: 「73分11秒」/「1小时02分03秒」/「45秒」→ 秒。平台用 `timeToText` 过滤器
#: 拼这串字，格式随总时长跨过 1 小时会变，所以三种单位都要认。
_TIME_TEXT_RE = re.compile(r"(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?")


def parse_time_text(text: str) -> int:
    """把「73分11秒」这类文字换成秒数。认不出来返回 -1。

    单独拎成模块级函数是为了能直接测 —— 它是 scope 读不到时的唯一退路，
    算错了会让刷时长"永远差一点"或者"提前收工"。
    """
    t = (text or "").strip()
    if not t:
        return -1
    m = _TIME_TEXT_RE.fullmatch(t)
    if not m or not any(m.groups()):
        return -1
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


#: 课件树里"视频讲座"那一类叶子的标记。**按 `wareType` 认，不按
#: `wareTypeName` 认** —— 平台在同一份课件树里对同一种东西混用两种叫法，
#: 实测 17 门课合计 39 个 `wareTypeName="讲座"` + 138 个
#: `wareTypeName="视频"`，但两边的 `wareType` 都是 `"jz"`。
#:
#: 这行判据曾经写成 `wareTypeName == "讲座"`，后果是**静默失效**：
#: 凡是把讲座标成"视频"的课，`items()` 恒返回 0 → `pending()` 恒为空 →
#: 看课脚本以为所有课都看完了，一节都不播；日志里只看得到一句
#: 「没有要看的课」，看不出哪里错了。
#:
#: `"tk"`（作业，标题「本项目考核」）必须排掉：考核走顶部「考核」tab 的
#: 另一套接口，把它算成要看的讲座会让 `pending()` 永远还不清。
_VIDEO_WARE_TYPE = "jz"


def _is_video_leaf(it: dict) -> bool:
    """课件树里的这个节点是不是一节要看的视频讲座。

    先看 `wareType`（权威），`wareType` 缺失时才退回 `wareTypeName`
    的两种叫法 —— 少数字段在某些课程上会缺，只认一个字段会重演上面
    那种「一半的课静默消失」。
    """
    if str(it.get("wareType") or "") == _VIDEO_WARE_TYPE:
        return True
    if it.get("wareType"):
        return False  # 明确标了别的类型（比如 tk=作业），别猜
    return str(it.get("wareTypeName") or "") in ("讲座", "视频")


#: 平台自己弹的 `layer.js` 确认框（**和"视频弹题"是两回事**，别混）。
#:
#: 静态 DOM 里搜不到它 —— `layer.confirm()` 是**点击那一刻**才建节点的，
#: 整页 HTML、`<script type="text/ng-template">`、内联 script 里
#: 「下一节」全 0 命中（2026-10-08 找了一整轮）。原文在客户端的
#: `CourseLearnControllers.js`（115,304 字节）里：
#:
#:     "2" !== ...activeItemObj.status
#:       ? layer.confirm("您的视频课件观看时长未达到，请继续观看以完成学习",
#:           {icon:0, title:"温馨提示", btn:["确定","取消"]}, ...)
#:       : angular.isDefined(t) && layer.confirm(
#:           "该视频课件已观看完毕，是否继续学习下一课程节点？",
#:           {icon:0, title:"温馨提示", btn:["学习下一课节","取消"]}, ...)
#:
#: 刷时长要处理的正是第二个：视频一播完它就弹，点「学习下一课节」当场跳走，
#: 点「取消」才留在这一讲。★ **两个框按钮文案不一样**（第一个是「确定/取消」），
#: 所以不能靠"第几个按钮"判断，只能**按文字点**。
#:
#: `layer.js` 关掉之后节点会留在 DOM 里（可能只是 `display:none`），
#: 所以这里必须过滤可见性，否则会对着一堆看不见的框空点。
LAYER_JS = r"""
(() => {
  const vis = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 2 && r.height >= 2;
  };
  const grab = (L) => {
    const btns = [];
    L.querySelectorAll('.layui-layer-btn a').forEach(b => {
      const r = b.getBoundingClientRect();
      btns.push({text: (b.innerText || '').trim(),
                 x: Math.round(r.x + r.width / 2),
                 y: Math.round(r.y + r.height / 2),
                 w: Math.round(r.width), h: Math.round(r.height)});
    });
    const c = L.querySelector('.layui-layer-content');
    return {
      title: ((L.querySelector('.layui-layer-title') || {}).innerText || '').trim(),
      content: c ? (c.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160) : '',
      buttons: btns,
    };
  };
  const out = [];
  document.querySelectorAll('.layui-layer').forEach(L => { if (vis(L)) out.push(grab(L)); });
  if (out.length) return JSON.stringify(out);
  // 兜底：万一平台换了皮肤类名，就找"可见 + 文字正好是取消/确定"的按钮，
  // 再往上爬到一个像浮层的祖先。只认这几个词，避免误点正文里的链接。
  const WORDS = ['取消', '确定', '学习下一课节', '继续学习'];
  const seen = new Set();
  document.querySelectorAll('a, button, span, div').forEach(b => {
    const t = (b.innerText || '').trim();
    if (WORDS.indexOf(t) < 0 || b.children.length) return;
    if (!vis(b)) return;
    let up = b.parentElement, depth = 0;
    while (up && depth < 6) {
      const cls = String(up.className || '');
      if (cls.indexOf('layer') >= 0 || cls.indexOf('popup') >= 0 || cls.indexOf('dialog') >= 0) {
        if (!seen.has(up)) { seen.add(up); out.push(grab(up)); }
        return;
      }
      up = up.parentElement; depth++;
    }
  });
  return JSON.stringify(out);
})()
"""

#: 把当前这一讲倒回 0 秒并重新播起来。
#:
#: 为什么刷时长要这个：视频播到结尾时平台会弹「是否继续学习下一课程节点？」，
#: 我们选「取消」留在这一讲 —— 但选完 `currentTime` 停在末尾、`paused` 是 true，
#: **它不会自己从头再来**（平台的循环开关 `showCompleteHint` 是给"下一节"用的）。
#: 所以每轮都要主动倒带。
#:
#: `play()` 返回 Promise，整页重载后可能 `NotAllowedError`（文档丢了用户激活），
#: 这里**必须如实报告有没有播起来**，不能一律回 `'ok'`。
#:
#: 2026-10-08 踩到的坑：原来这段是同步的、`play()` 的拒绝被吞掉、恒返回
#: `'ok'`。于是上层 `if what != "ok": start_video()` 那个点按兜底**永远
#: 不会触发**（判据不可能为真），现象是刷时长每 10 秒报一次「被按停了 →
#: 倒回 0 秒重播」，`cur` 死死停在 0，连刷 30 轮一动不动 —— 从日志看
#: 像是"平台不让播"，其实是整页载入丢了用户激活、`play()` 被浏览器拒了，
#: 而唯一的补救路径被这个假的 `'ok'` 挡住了。
#:
#: 所以现在等 900 毫秒再看 `paused`，把真实结果带回去。**不 `await`
#: `play()` 本身** —— 缓冲中的视频那个 Promise 可以长时间不落定，
#: `await` 会把整个 CDP 求值挂到超时。
REWIND_JS = r"""
(async () => {
  const v = document.querySelector('video');
  if (!v) return 'no-video';
  try { v.currentTime = 0; } catch (e) { return 'seek-fail'; }
  let err = '';
  try {
    const p = v.play();
    if (p && p.catch) p.catch(e => { err = (e && e.name) || 'error'; });
  } catch (e) { err = (e && e.name) || 'error'; }
  await new Promise(r => setTimeout(r, 900));
  if (!v.paused) return 'ok';
  return 'still-paused' + (err ? ':' + err : '');
})()
"""

#: 看课途中弹出来的"视频弹题"（**不只是选择题**）。
#:
#: 实测（2026-10-08 第 9 讲 00:52:09）弹的是一道**打分题**：
#:
#:   题面「请您为老师此堂讲课总体效果打分，满分100分(内容新颖实用，
#:   讲授清晰易懂，ppt制作精良)」，
#:   `input[type=number]` + `placeholder="请输入 50-100 的数值"` + 「提交」。
#:
#: 而原先的 `answer_popup()` 只找页面里有没有「A」，所以这题一直没答 ——
#: 平台每几秒就把视频按停一次（暂停取证里 PAUSE/PLAY 成对刷屏，`cur` 死死
#: 停在 3130 秒），而我们那 15 秒一轮的轮询每次都"刚好"看到它又在播，于是
#: 一轮轮地判成"没卡住"。**这就是第 9 讲反复自己暂停的真因。**
#:
#: DOM 结构（实测，`debug/_dpopup.py`）：
#:
#:   `.popup_layer` > `.popup_dialog` > `.popup_do` > `.popup_do_question`
#:     ├ `.popup_title`      「视频弹题（00:52:09）」
#:     ├ `.popup_count_hint` 「未做（1） 已做（0）」
#:     ├ `.question-number`  第几题
#:     ├ `.question-stem`    题面
#:     ├ `input`（选择题是单选框，打分题是 number 框，可能都有）
#:     └ `button`「提交」
#:
#: 答完提交之后**弹层不一定关**（实测标题从「视频弹题」变成「视频弹题1」，
#: 多题接着出），所以判"还在不在"不能只看它有没有消失。
QUIZ_JS = r"""
(() => {
  const p = document.querySelector('.popup_layer');
  if (!p) return JSON.stringify({open: false});
  const vis = getComputedStyle(p).display !== 'none' &&
              p.getBoundingClientRect().height > 10;
  const inp = p.querySelector('.popup_do_question input, .popup_body input');
  const btn = p.querySelector('.popup_operate button, .popup_body button');
  const r = p.getBoundingClientRect();
  // 选项：实测是 `.popup_body ul li`，每个 li 里一个 `.checkbox-inline`，
  // 文字在它右边那个 `.ng-binding` span 里（`A . 是` / `B . 否`）。
  // 一开始按 `.question-option / li.option` 那一串猜，**一个都没匹配上**
  // （见 DEVELOPMENT.md 7.5.1 ④-3），于是第二道弹题"认不出怎么答"被晾着，
  // 平台每隔几秒就把视频按停一次。li 是整行宽的、靠不住的是点哪儿，
  // 所以点**文字**那段的正中（实测点得动）。
  let opts = [];
  const scopes = [];
  if (p.querySelector('.popup_body')) scopes.push(p.querySelector('.popup_body'));
  scopes.push(p);
  for (const sc of scopes) {
    for (const li of sc.querySelectorAll('li')) {
      const t = (li.innerText || '').trim().replace(/\s+/g, ' ');
      if (!t || t.length > 60) continue;
      // 只要"选项行"：题干那一层也是 `li`（`LI.topic-item.DANXUAN`），
      // 它的文字是整道题干（带题号、超过 60 字通常被上面挡掉，
      // 但也有短的），所以额外要求它里面有 `.checkbox-inline` 那个小圈。
      const mark = li.querySelector('.checkbox-inline');
      if (!mark) continue;
      const sp = mark.querySelector('span') || mark;
      const b = sp.getBoundingClientRect();
      if (b.height < 4 || b.width < 2) continue;
      // 文字只取**选项自己**那段（`A . 是`）。取整个 li 的话，题干那层
      // 也会跟着进来，日志里会出现一份被截断的题干冒充选项。
      const ot = (sp.innerText || '').trim().replace(/\s+/g, ' ') || t;
      opts.push({text: ot.slice(0, 40), x: b.x + b.width / 2, y: b.y + b.height / 2});
    }
    if (opts.length) break;
  }
  const bb = btn ? btn.getBoundingClientRect() : null;
  const ib = inp ? inp.getBoundingClientRect() : null;
  // 弹层里所有按钮都念出来：不同的题按钮字不一样（打分题是「提交」，
  // 下一题可能变「继续」/「确定」），所以除了主按钮还留一份清单兜底。
  const btns = [...p.querySelectorAll('button, a.btn, .btn')]
    .map(e => {
      const b = e.getBoundingClientRect();
      return {text: (e.innerText || '').trim().slice(0, 12),
              x: b.x + b.width / 2, y: b.y + b.height / 2, h: Math.round(b.height)};
    })
    .filter(b => b.text && b.h > 8 && b.h < 200);
  return JSON.stringify({
    open: vis,
    title: ((p.querySelector('.popup_title') || {}).innerText || '').slice(0, 60),
    stem: ((p.querySelector('.popup_body .title') ||
            p.querySelector('.question-stem') || {}).innerText || '')
            .trim().replace(/\s+/g, ' ').slice(0, 120),
    inputType: inp ? inp.type : '',
    inputBox: ib ? {x: ib.x + ib.width / 2, y: ib.y + ib.height / 2} : null,
    btnText: btn ? (btn.innerText || '').trim().slice(0, 12) : '',
    btnBox: bb ? {x: bb.x + bb.width / 2, y: bb.y + bb.height / 2} : null,
    buttons: btns.slice(0, 6),
    options: opts.slice(0, 8),
    counts: ((p.querySelector('.popup_count_hint') || {}).innerText || '')
            .replace(/\s+/g, ' ').slice(0, 40),
  });
})()
"""

#: 往弹题那个 `input` 里写答案。
#:
#: **必须走原型上的 `value` setter + 派发 `input`/`change`**，直接
#: `el.value = x` 不会更新 AngularJS 的 `ng-model`（`questionObj.wendaAnswer`），
#: 提交上去就是空的 —— 手机版那边填表单踩过同一个坑。
QUIZ_FILL_JS = r"""
(() => {
  const p = document.querySelector('.popup_layer');
  if (!p) return 'no-popup';
  const inp = p.querySelector('.popup_do_question input, .popup_body input');
  if (!inp) return 'no-input';
  const set = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set;
  set.call(inp, __V__);
  inp.dispatchEvent(new Event('input', {bubbles: true}));
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  inp.dispatchEvent(new Event('blur', {bubbles: true}));
  return 'filled:' + inp.value;
})()
"""

#: 读考核页上的题。**从配置对象里读，不认 DOM 文案** —— 文案会被
#: `ng-bind-html` 拼出来（题干里还带 `$index + 1 + '、'` 这种前缀），
#: 而配置对象的字段名是从 `homework/do.html` 模板原文抄下来的，稳。
#:
#: 模板原文（`debug/examjs/tpl_homework_do.html`）：
#:   danxuan：`ng-repeat="topicObj in …questionObj.danxuanList"`
#:            `input[type=radio][ng-model="topicObj.userAnswerModel"]`
#:   duoxuan：`input[type=checkbox][ng-model="topicObj.userAnswerModel[topicItemObj.index]"]`
#:   panduan：同 danxuan（radio）
#:   wenda：`textarea[ng-model="topicObj.userAnswerModel"]`
#: 选项文字在 `topicItemObj.content`（回退 `title`／`text`），
#: 选项的**取值**是 `topicItemObj.index`（模板里 `value="{{topicItemObj.index}}"`）。
HOMEWORK_JS = r"""
(() => {
  const root = document.querySelector(
      '#page_learn_homework_do, #page_learn_homework_show, .page_content') || document.body;
  const cfg = (window.angular && angular.element(root).scope()) || {};
  // 答题页和「查看」页是**两个不同的配置对象**：do 页是
  // `courseLearnHomeworkDoConfig`（题干 + 我填的答案），show 页是
  // `courseLearnHomeworkShowConfig`（题干 + 我填的答案 + **平台给的正确答案**）。
  // 两个都认，上层就只用一套读法。
  const c = cfg.courseLearnHomeworkDoConfig || cfg.courseLearnHomeworkShowConfig || null;
  if (!c) return '{}';
  const hw = c.homeworkObj || {};
  const q = c.questionObj || {};
  const opts = (t) => (t.topicItemList || []).map((it) => ({
    index: String(it.index),
    text: String(it.content || it.title || it.text || '').replace(/\s+/g, ' ').trim().slice(0, 80),
  }));
  const list = [];
  const push = (kind, arr) => (arr || []).forEach((t) => list.push({
    kind: kind, id: String(t.id || ''), title: String(t.title || t.questionContent || '')
      .replace(/\s+/g, ' ').trim().slice(0, 120),
    model: (t.userAnswerModel === undefined || t.userAnswerModel === null)
      ? null : t.userAnswerModel,
    // 平台公布的正确答案。**只有交过卷、且 `answerShowType` 允许显示时才非空**；
    // 2026-10-08 实测取值：单选/判断是一个字母（`"D"`），多选是 `"A|C|D|E"`
    // （拼的就是选项 `index`，和提交时 `p()` 的拼法一致）。
    sanswer: (t.sanswer === undefined || t.sanswer === null)
      ? '' : String(t.sanswer),
    options: opts(t),
  }));
  push('danxuan', q.danxuanList);
  push('duoxuan', q.duoxuanList);
  push('panduan', q.panduanList);
  push('wenda', q.wendaList);
  return JSON.stringify({
    loaded: !!c.loaded, id: String(hw.id || ''), title: String(hw.title || ''),
    type: hw.homeworkType, status: hw.homeworkStatus, category: hw.homeworkCategory,
    redo: hw.allowRedoNum, answer_show: hw.answerShowType,
    //: 这一份是从哪个页面读出来的：`do` = 答题页，`show` = 批改后的查看页。
    route: cfg.courseLearnHomeworkDoConfig ? 'do' : 'show',
    timestamps: (q.showTimestamp === undefined ? '' : q.showTimestamp),
    answer_shown: !!q.showAnswer,
    questions: list, count: list.length,
  });
})()
"""

#: 按计划填答案。`__PLAN__` 被替换成 `{题目 id: 答案}`。
#:
#: 为什么要派发 `click` 而不只是 `change`：模板上绑的是
#: `ng-change="checkAnswerModel()"`，而 AngularJS 的 `ng-change` **只在它
#: 自己处理的事件里**才跑；同时 `ng-model` 要靠 AngularJS 的
#: `$digest` 才会同步。所以对 radio/checkbox：**先把它 `.click()`**
#: （这一下会同时改 `checked`、更新 `ng-model`、跑 `ng-change` 与
#: `$digest`），再核对 `model` 有没有变成我们要的值。直接写
#: `el.checked = true` 不会触发任何 AngularJS 逻辑。
#: 主观题的 textarea 走"原型 value setter + input/change"，和 `QUIZ_FILL_JS`
#: 同一个理由（直接 `el.value=` 不更新 `ng-model`）。
HOMEWORK_FILL_JS = r"""
(() => {
  const plan = __PLAN__;
  const cfg = (window.angular && angular.element(
      document.querySelector('#page_learn_homework_do, .page_content') || document.body)
      .scope() || {});
  const c = (cfg && cfg.courseLearnHomeworkDoConfig) || null;
  if (!c) return JSON.stringify({filled: 0, missing: Object.keys(plan)});
  const q = c.questionObj || {};
  const groups = [
    ['danxuan', q.danxuanList, 'radio'],
    ['duoxuan', q.duoxuanList, 'checkbox'],
    ['panduan', q.panduanList, 'radio'],
    ['wenda', q.wendaList, 'text'],
  ];
  let filled = 0;
  const missing = [];
  for (const [kind, arr, mode] of groups) {
    for (const t of (arr || [])) {
      const want = plan[String(t.id)];
      if (want === undefined || want === null || want === '') continue;
      const items = t.topicItemList || [];
      if (mode === 'text') {
        const box = document.querySelector(
          'textarea[ng-model="topicObj.userAnswerModel"]')
          || [...document.querySelectorAll('textarea')].find(
              (x) => String(angular.element(x).attr('ng-model') || '').includes('userAnswerModel'));
        if (!box) { missing.push(String(t.id)); continue; }
        const set = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value').set;
        set.call(box, String(want));
        box.dispatchEvent(new Event('input', {bubbles: true}));
        box.dispatchEvent(new Event('change', {bubbles: true}));
        box.dispatchEvent(new Event('blur', {bubbles: true}));
        filled += 1;
        continue;
      }
      // 单选/判断：want 是选项的 index；多选：want 是 "0|2" 这种。
      const wanted = (mode === 'checkbox')
        ? String(want).split('|').map((x) => x.trim()).filter((x) => x !== '')
        : [String(want)];
      let hit = 0;
      for (const it of items) {
        if (!wanted.includes(String(it.index))) continue;
        const node = [...document.querySelectorAll(
            mode === 'checkbox' ? 'input[type=checkbox]' : 'input[type=radio]')]
          .find((x) => String(angular.element(x).attr('name') || '') === String(t.id)
                       && String(x.value) === String(it.index));
        if (!node) continue;
        if (mode === 'radio' ? !node.checked : node.checked !== true) node.click();
        hit += 1;
      }
      if (hit) filled += 1; else missing.push(String(t.id));
    }
  }
  try { cfg.$apply && cfg.$apply(); } catch (e) {}
  return JSON.stringify({filled: filled, missing: missing});
})()
"""

#: 用**页面自己的** `element.click()` 点一个元素。
#:
#: 为什么不能只有真鼠标三连（`click_at`）：2026-10-08 实测，在考核页上用
#: `Input.dispatchMouseEvent` 点「提交」按钮，装在按钮上的 `click` 监听器
#: **一次都没触发**（探针 `window.__probe` 里连 `click:` 都没记到），也就
#: 没有任何 `submitHomework` 上报 —— 而页面看起来完全正常，最容易误判成
#: 「服务端不认」。同一个按钮换成 `btn.click()` 立刻弹出确认框。
#: 原因是这类页面把内容装在一个**会滚动的容器**里（`getBoundingClientRect`
#: 的 y 能到 -2457），坐标换算靠不住。
#:
#: 反过来说，**顶部导航**那套（`$state.go` + `history.pushState`）用
#: `.click()` 是无效的，那里还得用真鼠标。所以两个都要有。
JS_CLICK_JS = r"""
(() => {
  const tag = __TAG__, cls = __CLS__, text = __TEXT__, exact = __EXACT__;
  const ok = (e) => {
    const t = String(e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t) return false;
    return exact ? t === text : t.includes(text);
  };
  const sel = (tag || '*') + (cls ? '.' + cls.split(' ').join('.') : '');
  let cands;
  try { cands = [...document.querySelectorAll(sel)]; } catch (e) { return 'bad-sel'; }
  const hit = cands.filter(ok).pop();
  if (!hit) return 'none';
  try { hit.click(); } catch (e) { return 'click-threw:' + String(e).slice(0, 40); }
  return 'ok';
})()
"""

#: 点一下视频元素的正中间 —— 平台自己的"点击播放"入口。
#: 为什么不能只靠 `v.play()`：桌面版**拿不到倍速**，而且实测整页载入之后
#: `cur` 会死死停在中途（第 10 讲是 63 秒）、`paused` 一直 true、`play()`
#: 的 Promise 报 `NotAllowedError`（Chrome 自动播放策略，见 `PLAY_CHECK_JS`）。
#: 而平台自己的播放开关是绑在 `<video>` 上的点击处理 —— 给它一次真鼠标
#: 事件，`cur` 立刻按秒涨，之后再 `play()` 就 `RESOLVED` 了。
CLICK_VIDEO_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return 'null';
  const r = v.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2,
                         w: Math.round(r.width), h: Math.round(r.height)});
})()
"""


#:
#: 课程页左侧的讲次列表。返回 `[{n, text, state, x, y, w, h, cx, cy}]`。
#:
#: **完成标记读的是那个小圆点的 font-awesome 类名**，不是外层的
#: `user-select` / `user-no-select`：
#:
#:   * `fa-circle`   实心圆 —— 这一讲学完了；
#:   * `fa-adjust`   半圆   —— 看了一半（实测是"上次学到这儿"的那一讲）；
#:   * `fa-circle-o` 空心圆 —— 没看。
#:
#: 为什么改：一开始按外层类名判（以为学完会换成 `user-select`），
#: 结果在真的课件页上读出来**全行都是 `user-no-select`**，于是"这门课
#: 的视频讲次都看完了" —— 明明第 1 讲已完成、第 9 讲在播。看颜色更不行
#: （要靠像素采样，改版就废）；类名是页面自己切样式用的，稳。
LESSONS_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('.course_chapter_item').forEach(e => {
    const t = (e.textContent || '').replace(/\s+/g, '');
    const m = /^第(\d+)讲/.exec(t);
    if (!m) return;
    const n = parseInt(m[1], 10);
    if (out.some(o => o.n === n)) return;
    const r = e.getBoundingClientRect();
    const icon = e.querySelector('.section_status i, .section_status');
    // 逐个类名比，**不要拿整串 className 做正则** ——
    // 实测 `"fa fa-circle-o"` 这种串里去掉空格之后是 `fa-circle-o`，
    // 而 `/fa-circle/` 照样命中它，于是"没看的讲"全被判成"看完了"。
    const names = icon ? [...icon.classList] : [];
    let state = 'new';
    if (names.indexOf('fa-circle-o') >= 0) state = 'new';
    else if (names.indexOf('fa-circle') >= 0) state = 'done';
    else if (names.indexOf('fa-adjust') >= 0) state = 'part';
    out.push({n: n, text: t.slice(0, 40), state: state, done: state === 'done',
              x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height),
              cx: Math.round(r.x + r.width / 2),
              cy: Math.round(r.y + r.height / 2)});
  });
  out.sort((a, b) => a.n - b.n);
  return JSON.stringify(out);
})()
"""

#: 我的学习页的课程列表。从页面自己的列表数据里读，**不靠 OCR**。
#:
#: ⚠ 这里读到的**只是当前这一页**（实测 `pageSize=8`），不是全部课程。
#: 要看全量得走 `Session.all_courses()` 翻页，`courses()` 会自动接上。
COURSES_JS = r"""
(() => {
  // 页面的课程数据挂在 `.my-study` 的 Vue 实例上（实测 data.courseList）。
  let v = null;
  for (const e of document.querySelectorAll('div,section,main')) {
    if (e.__vue__ && e.__vue__.$data && e.__vue__.$data.courseList) { v = e.__vue__; break; }
  }
  if (!v) return 'null';
  const d = v.$data || v._data || {};
  const list = d.courseList || [];
  return JSON.stringify(list.map(c => ({
    id: c.id, name: c.name,
    userClassId: c.userClassId,
    hour: c.hour, isFinish: c.isFinishCourse,
    finishDate: c.finishCourseDate || '',
    desc: c.userClassScoreDesc || '',
    canStudy: c.canStudyFlag,
    // 接口（`/user/getMyCourseList`）比上面这几个多给一些，这里也一并带出来，
    // 好处是**页面读到的**和**接口读到的**字段名完全一致，调用方不用分两条路。
    isOverdue: c.isOverdue, overdueDate: c.overdueDate || '',
    hasCertificate: c.hasCertificate,
    projectName: c.projectName || '',
    classAssessmentDesc: c.classAssessmentDesc || '',
    isPass: c.isPass,
  })));
})()
"""

#: 分页信息：平台一共有多少门课、每页几门、现在在第几页。
#:
#: 为什么要单独读：`courseList` 只有当前页那几条，光看它分不出
#: "账号下就 8 门课"和"一共 17 门、这只是第一页"。
COURSE_META_JS = r"""
(() => {
  let v = null;
  for (const e of document.querySelectorAll('div,section,main')) {
    if (e.__vue__ && e.__vue__.$data && e.__vue__.$data.courseList) { v = e.__vue__; break; }
  }
  if (!v) return 'null';
  const d = v.$data || v._data || {};
  return JSON.stringify({total: d.totalCount, pageSize: d.pageSize,
                         page: d.currentPage, got: (d.courseList || []).length});
})()
"""

#: 页面标题 / 地址 / 有没有被微信墙挡住。
WHERE_JS = r"""
JSON.stringify({
  url: location.href,
  title: document.title,
  text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 400),
  video: !!document.querySelector('video'),
})
"""

#: 抓页面自己发的上报请求。
#:
#: 为什么要抓：平台是**前端自己**把学习进度 POST 给服务端的
#: （`sendVideoLearnRecord`），响应里带着服务端认可的 `learnRecord`
#: （`state` / `status` / `studyTime`）。抓它比自己造请求干净得多 ——
#: 不额外发流量、不猜参数（`key` 是毫秒时间戳、`recordCount` 是 60、
#: 少一个都可能被服务端丢掉），拿到的就是"服务端真的收到了什么"。
#:
#: **记录写进 `sessionStorage`**（不是内存变量）：整页导航会换掉文档，
#: 钩子连变量一起没；`sessionStorage` 跟着标签页活，导航前后都在。
#: 实测踩过：钩子装在内存里，`location.reload()` 之后一条都抓不到。
OBSERVE_JS = r"""
(() => {
  const KEY = '__dsh_calls';
  if (window.__dshHooked) return 'already';
  window.__dshHooked = true;
  const push = (o) => {
    try {
      const a = JSON.parse(sessionStorage.getItem(KEY) || '[]');
      a.push(o);
      sessionStorage.setItem(KEY, JSON.stringify(a.slice(-60)));
    } catch (e) {}
  };
  const oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__dshUrl = String(u); return oOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const self = this;
    if (/studentDataAPI\.action/.test(this.__dshUrl)) {
      this.addEventListener('loadend', function () {
        let t = '';
        try { t = String(self.responseText || ''); } catch (e) {}
        push({url: self.__dshUrl, body: body ? String(body) : '', res: t});
      });
    }
    return oSend.apply(this, arguments);
  };
  return 'ok';
})()
"""

#: 把钩子攒下的记录取走（取完就清空，避免越攒越大）。
CALLS_JS = r"""
(() => {
  const a = JSON.parse(sessionStorage.getItem('__dsh_calls') || '[]');
  sessionStorage.setItem('__dsh_calls', '[]');
  return JSON.stringify(a);
})()
"""

#: 平台 API 的通道。全部走页面自己的同源 `fetch`（`credentials: 'include'`），
#: 所以 cookie、登录态、CSRF 之类都由浏览器自己处理。
#:
#: 为什么不用 `Session.js` 直接发：页面在 `course.` 域名下，
#: 而 `desktop.py` 想知道的是"平台自己认为这门课学到哪了"，走页面的
#: 同源请求最不容易出岔子（跨域、cookie 域、Referer 都可能被服务端挑）。
API_JS = r"""
(async () => {
  const p = new URLSearchParams(__P__).toString();
  const r = await fetch('/learning/student/studentDataAPI.action?' + p,
                        {credentials: 'include'});
  return await r.text();
})()
"""

#: 往**同源的任意路径** POST 一份表单。问卷那一族接口（`/user/…`）不在
#: `studentDataAPI.action` 里，是独立的 action，所以另开一条通道。
#:
#: 为什么仍然走页面的 `fetch` 而不是 Python 自己发：cookie 是 `HttpOnly`
#: 的会话 cookie，只有浏览器自己带得上；自己拼请求会 302 到登录页
#: （2026-10-08 实测过一次，白折腾）。
POST_JS = r"""
(async () => {
  try {
    const r = await fetch(__URL__, {method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest'},
      body: __BODY__});
    return (await r.text());
  } catch (e) { return 'ERR:' + String(e); }
})()
"""

#: 登录态怎么判 —— **不能看页面上有没有「登录」两个字**。
#:
#: 实测：没登录时访问「我的学习」会被 302 回 `/learning/login`，
#: 而登录页的正文里同时有「立即登录」和「管理员登录，请点击此处」，
#: 拿关键词判会误判成"已登录"（第一版就是这么写的，直接跑进登录页白转半天）。
#: 所以直接问服务器：带 cookie 请求一次受保护页面，看**最终落在哪个地址**
#: （`WS.evaluate` 开了 `awaitPromise`，所以 async 函数可以直接写）。
LOGIN_JS = r"""
(async () => {
  try {
    const r = await fetch('/learning/personalCenter?module=learning&tabIndex=1',
                          {redirect: 'follow', credentials: 'include'});
    return !/\/login/.test(r.url);
  } catch (e) { return false; }
})()
"""


# ---------------------------------------------------------------- 挑标签页


def tabs(*, port: int = 9222) -> list[dict]:
    """浏览器里所有标签页（含各自当前地址）。

    用 `/json/list` 而不是 `/json`：这台 Chrome 110 曾经在崩溃边缘对
    `/json` 回 `HTTP 404`，而 `/json/list` 同一时刻是 200。两者本应等价，
    没必要赌。

    ## 问不到的时候返回空表，**不要抛出去**

    实测踩到：浏览器被关掉之后 `adb forward` 的映射**还留着** —— TCP 连得上，
    但对端没人应答，于是 `urllib` 抛的是 `RemoteDisconnected`。它是
    `http.client` 的异常（`ConnectionResetError` 的子类），既不是 `URLError`
    也不是 `RuntimeError`，所以一路穿到 `Session.open()`，而那里只接
    `RuntimeError` —— 结果是**一个 traceback 摔在用户脸上**，而不是
    「自动把浏览器拉起来再来一次」。

    把「问不到」统一成「一个标签页都没有」就顺了：`connect()` 会因此抛它
    自己那句 `RuntimeError`，`open()` 正好接住 → 拉起浏览器 → 重连。
    """
    import browser

    try:
        raw = browser.http_json("/json/list", port=port)
    except Exception as exc:  # noqa: BLE001
        _quiet(f"[desk] 调试端口 {port} 问不到标签页"
               f"（{exc.__class__.__name__}: {exc}）")
        return []
    return [t for t in raw if t.get("type") == "page"]


_TAB_SCORE_BONUS = 40


def _tab_score(url: str) -> int:
    """给标签页打分 —— 分越高越适合拿来干活。

    实测这个模拟器浏览器里**常年挂着几个死标签页**（WebSocket 打得开、
    `Runtime.evaluate` 一律超时）。它们不只是碍眼：同一个站点的连接
    会被它们占住，**新标签页导航到这个站点会永远卡在 `readyState=loading`**
    （实测：6 个 `elearning` 标签页同时挂着时，导航后 16 秒
    `document.body` 还是 `null`）。

    所以挑标签页要看「它现在停在哪」：
      * **课程站**（`course.zs-hospital.sh.cn`）分最高 —— 那是真正干活
        的地方，点课、看视频都在这儿（+40）；
      * 已经在本站而且是 `about:blank` 的最优先 —— 页内跳转不重新拉资源（30）；
      * 已经在本站的次之（能复用已建好的连接）（20）；
      * 其它站点的排后面（5）。
    死标签页在 `connect()` 里会被探活筛掉。
    """
    if "course.zs-hospital.sh.cn" in url:
        return 40 + _TAB_SCORE_BONUS
    if url == "about:blank":
        return 30
    if "zs-hospital" in url:
        return 20
    if url.startswith("http"):
        return 5
    return 0


def tab_ids(*, port: int = 9222) -> set[str]:
    """当前所有标签页的 id。用来对比"点之前/点之后"多了哪个。"""
    return {t.get("id", "") for t in tabs(port=port)}


def close_tab(tab_id: str, *, port: int = 9222) -> bool:
    """关掉一个标签页。用 HTTP 那条通道，不用 `Page.close`。

    实测：卡死的标签页**连 `Page.close` 都会超时**（8 个里只关掉 1 个），
    但 `GET /json/close/<id>` 能把卡死的也关掉（7 个全关掉了）。
    用户明确要求过「不要开这么多页面，不然 cookie 都保存不下来」，
    所以每次开完新页都要顺手把旧的收掉。

    **最后一张不能关。** 2026-10-08 实测：把这台模拟器浏览器的标签页
    关到 0 之后，Chrome 的渲染进程跟着退了 —— 之后
    `Target.createTarget` 回 `{'code': -32000, 'message': 'Not supported'}`、
    `PUT /json/new` 回 `HTTP 500 Could not create new page`，
    连 DevTools 的 HTTP 口都开始间歇性 404，只能 `am force-stop` 重启
    浏览器（很可能把 httponly 的会话 cookie 一起丢掉，得重新登录）。
    所以这里宁可少关一个，也不把关到零的机会留给调用方。
    """
    if len(tabs(port=port)) <= 1:
        return False
    # 注意：这条通道**不能用 `browser.http_json()`** —— `/json/close/<id>`
    # 回的是纯文本（`Target is closing`），不是 JSON，`json.load()` 会抛。
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/close/{tab_id}", timeout=10) as fh:
            fh.read()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 会话


#: 探活用的异步 JS。**必须带 await** —— 见 `_live()` 的说明。
#:
#: 第三段那次 `fetch` 是 2026-10-08 补上的，补得有理由：上面两级探活**都过**
#: 的标签页仍然可能是一具僵尸 —— 实测 [31] 号页 `1+1` 秒回、promise 也兑现，
#: 可它就是 `fetch` 永远不回（被导航过太多次、渲染进程已经半死）。既然真正
#: 要干的活儿是调接口，探活就该直接试一次真实网络往返。
_LIVE_JS = ("(async () => { const r = await Promise.resolve('ok');"
            " let n = document.body ? document.body.childElementCount : -1;"
            " try { const c = new AbortController();"
            " setTimeout(() => c.abort(), 9000);"
            " const q = await fetch('/robots.txt?_=' + Date.now(),"
            " {cache: 'no-store', signal: c.signal}); n += q.status; }"
            " catch (e) { return 'net-fail:' + (e && e.name); }"
            " return r + n; })()")


def _live(ws_url: str, *, probe: float = 6.0, work: float = 40.0, tries: int = 4):
    """连一个标签页并确认它**真能干活**，能就返回连接，不能返回 `None`。

    为什么探活要分三级（实测，每一次都是踩出来的）：

      * 一级 `1+1` 只证明"WebSocket 通、渲染进程没整个死掉"。这台模拟器里
        常驻着**僵尸标签页**：`1+1` 毫秒级返回，可它对任何真活儿
        （`document.body.innerHTML`、`fetch`）一律**超时**。更坑的是它不只
        自己废，还会**占住本站的连接名额**，让新页面永远卡在
        `readyState=loading`（实测 75 秒 `body` 还是 `null`）。
      * 二级要发一条**异步**表达式（`awaitPromise=True`）。僵尸页能同步
        答 `1+1`，却永远兑现不了一个 promise，这一级筛掉一批。
      * 三级在页面里真发一次 `fetch` —— 二级全过的标签页**仍然可能**是
        半死的（实测 [31]：promise 兑现，可 `fetch` 永远不回）。既然要干
        的活儿就是调接口，探活就该试一次真实网络往返。

    ⚠ **`about:blank` 上第三级必"失败"，但它不是坏页**（2026-10-08 又踩）：
    `_LIVE_JS` 里 `fetch('/robots.txt')` 是**相对地址**，`about:blank` 没有
    origin，直接 `TypeError` → 返回 `net-fail:TypeError`。于是**刚开出来的
    新标签页永远被判死** —— 而 `open_tab()` / `json_new()` 开出来的正是
    `about:blank`。表现是：浏览器好好的、`1+1` 也答得出来，可
    `connect()` 报「N 个标签页一个都不能用（N 个探活就死）」，白让人去重启
    浏览器（重启**会把 httponly 的会话 cookie 弄丢**，用户得重新登录）。
    所以这里对 `about:blank` 放宽：它算"能用"，因为 `_at()` / `_loaded()`
    本来就会拦住"没导航过"的页面。

    探活用的超时（`probe`）和干活用的超时（`work`）分开：探活要短，否则
    一个僵尸页就要等 40 秒；探活了之后再放长，正常页面里 `fetch` 偶尔要
    几秒才回。

    `tries`：**刚开出来的新标签页经常头一两秒还没准备好**（页面正在加载，
    `robots.txt` 那一问可能直接失败），所以同一个标签页要重试几次再判死。
    实测新开的标签页第一次探测就判死过一次，白丢一个好好的页面。
    """
    import browser

    for attempt in range(max(1, tries)):
        ws = None
        try:
            ws = browser.WS(ws_url, timeout=probe)
            ws.call("Runtime.evaluate", expression="1+1", returnByValue=True)
            got = ws.call("Runtime.evaluate", expression=_LIVE_JS,
                          returnByValue=True, awaitPromise=True)
            val = str(got.get("result", {}).get("result", {}).get("value", ""))
            if not val.startswith("ok"):
                if _blank_page(ws):
                    ws.timeout = work
                    if ws.sock is not None:
                        ws.sock.settimeout(work)
                    return ws
                raise RuntimeError(val[:40] or "没有返回值")
        except Exception:  # noqa: BLE001
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1.5)
            continue
        ws.timeout = work
        if ws.sock is not None:
            ws.sock.settimeout(work)
        return ws
    return None


def _blank_page(ws) -> bool:
    """这个标签页现在是不是"还没导航过"（`about:blank` / `chrome://newtab`）。

    单独一个函数是因为它要**吞掉所有异常**：这是在判活失败之后叫的，
    连 `location.href` 都读不出来的页面按"不是空白页"处理（保守）。
    """
    try:
        href = str(ws.evaluate("location.href") or "")
    except Exception:  # noqa: BLE001
        return False
    return href.startswith("about:") or href.startswith("chrome://newtab")


def browser_ws(*, port: int = 9222):
    """连到**浏览器级**的 CDP（不是某个标签页）。

    只有这一层才有 `Target.createTarget` —— 那是唯一一条**可靠地开新标签页**
    的路子。`window.open()` 走不通（见 `Session.new_tab()` 的说明）。
    """
    import browser

    ver = browser.http_json("/json/version", port=port)
    url = ver.get("webSocketDebuggerUrl") or ""
    if not url:
        raise RuntimeError("拿不到浏览器级 webSocketDebuggerUrl（/json/version 没回）")
    return browser.WS(url, timeout=20.0)


def new_tab(url: str, *, port: int = 9222, settle: float = 12.0) -> str:
    """开一个新标签页并导航到 `url`，返回它的 target id。

    为什么不用 `window.open()`：平台的「去学习」是
    `var w = window.open(); w.location.href = <课程站地址>`。实测那一下
    **既开不出标签页、也点不动** —— 页面被一个卡死的
    `div.el-loading-mask`（`opacity:0` 但 `pointer-events:auto`，铺满视口）
    挡着，真鼠标、合成事件、MaaTouch 三种点法页面一律收不到。
    与其跟那个遮罩和弹窗拦截较劲，不如自己开标签页。

    两条通道，按可靠性排：
      1. **HTTP `PUT /json/new?<url>`** —— 这台 Chrome 110 上最稳的，
         连"一个标签页都没有"的崩后状态都能开出来。
      2. `Target.createTarget`（浏览器级 CDP）—— 标签页数为 0 时这台机器回
         `{'code': -32000, 'message': 'Not supported'}`，只作退路。
    """
    info = json_new(url, port=port)
    tid = str(info.get("id", ""))
    if not tid:
        bws = browser_ws(port=port)
        try:
            r = bws.call("Target.createTarget", url=url)
        finally:
            bws.close()
        tid = r.get("result", {}).get("targetId", "")
    if not tid:
        raise RuntimeError(
            f"开不出新标签页 —— HTTP /json/new 和 Target.createTarget 都没成。"
            f"这台浏览器该重启了（browser.launch_debug(restart=True)）")
    time.sleep(settle)
    return tid


def json_new(url: str, *, port: int = 9222) -> dict:
    """HTTP `PUT /json/new?<url>`：开一张新页，回它的 target 字典。

    这条通道**不需要**先有标签页、也不需要 WebSocket ——
    实测在 `Target.createTarget` 已经回 `Not supported` 的崩后状态下，
    只有它还能开出页（所以 `new_tab()` 把它排第一）。
    开不出来时回 `{}`，不抛异常。
    """
    import urllib.error
    import urllib.request

    for method in ("PUT", "POST"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{url}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as fh:
                return json.loads(fh.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
    return {}


#: 看课程序（`desktop_watch.py`）跑的时候占的锁文件。这里只**读**它，
#: 用来提醒"别在这个标签页上乱导航"。
LOCK_FILE = paths.debug_dir() / "desktop_watch.lock"


def busy_hint() -> str:
    """有看课程序在跑就返回它写的那行（`PID 时间`），否则返回空串。

    为什么要这个：看课**是在浏览器那一个标签页里**跑的，旁边随便一个
    探针 `enter_course()` 导航一下，正在播的 `<video>` 就没了，而看课的
    日志只会说「页面上没有视频元素了」—— 看起来像平台的毛病。同一个错
    犯过两次，白丢过 45 分钟和 34 分钟的正片进度。
    """
    try:
        return LOCK_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _human_wait(seconds: float) -> str:
    """把秒数写成人话：89 → `89 秒`，600 → `10 分钟`。

    为什么不用 `f"{s / 60:.0f} 分钟"`：`--login-wait` 收的是**秒**，命令行里
    写 `--login-wait 20` 是很自然的用法，而 `20 / 60` 取整是 `0` —— 日志里
    打出「等你最多 0 分钟」，看着像坏掉了。实测踩到过。
    """
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    return f"{seconds / 60:.0f} 分钟"


class NotLoggedIn(RuntimeError):
    """浏览器里没有登录态，而且等也等不到。

    为什么要单独一个类型：**「没登录」不是程序坏了，是一件正常的、
    需要人去做点事的情况** —— 用户得在那个浏览器窗口里登一次。原来它和
    别的 `RuntimeError` 混在一起往上抛，结果是：

      1. 命令行跑的时候摔一整段 traceback 在用户脸上；
      2. 从界面按钮跑的时候，日志面板里也是一段 traceback，看着像程序崩了。

    实测踩到（2026-10-08）：浏览器被 Android 后台杀掉之后整个 cookie 罐是
    空的（`Network.getAllCookies` 回 0 条），于是 `--list` 直接
    `RuntimeError: 等了这么久还是没登录 —— 先登录再跑` + 7 行调用栈。

    所以调用方可以 `except NotLoggedIn` 打印一句人话、返回一个专门的退出码。
    """


class Session:
    """一个「浏览器 + 框架控制器」的组合。    生命周期：`open()` → 若干操作 → `close()`。

    为什么要 `__enter__/__exit__`：这个对象握着 adb 连接和浏览器通道，
    中途抛异常不关会留下**连着的标签页**，下次跑就会连到旧的上面
    （实测踩过：以为是新页面，其实还是上一次停在的那页）。
    """

    def __init__(self, log: Callable[[str], None] = print) -> None:
        self.log = log
        self.ws = None
        self.controller = None
        self.tasker = None
        self.port = 9222
        #: 平台**自己报的**课程总数（`getMyCourseList` 回执里的 `totalCount`）。
        #: 读不到就是 `None`。存在的理由：调用方要打印「一共几门课」时，
        #: 只能拿这个数 —— 拿 `len(courses)` 或者本地进度文件里的条数，
        #: 都会在分页没读全 / 本地记录过期时少报（用户 2026-10-08 明确要求
        #: 「总课程数为从网站上读取而非记录的数量」，就是这个数）。
        self.course_total: int | None = None
        self._used_tabs: set[str] = set()
        #: 现在连着哪个标签页。整页跳转（跨域名那种）会把 WebSocket 打断，
        #: 这时候要**重连同一个标签页**，而不是让 `connect()` 去重新挑一个
        #: （它会挑分数最高的，而那多半是登记地址过期的旧标签页）。
        self.tab_id = ""
        #: 已经有看课程序在跑的时候提醒一句。这个错犯过两次：看课是**在
        #: 浏览器那一个标签页里**跑的，旁边随便一个探针 `enter_course()`
        #: 一下，正在播的视频就没了，看课的日志只会说「页面上没有视频
        #: 元素了」—— 白丢过一次 45 分钟、一次 34 分钟。
        hint = busy_hint()
        if hint:
            self.log(f"[desk] ⚠ 现在有看课程序在跑（{hint}）—— "
                     f"导航会把它正在播的视频弄没")

    # -- 开关 ------------------------------------------------------------

    def open_tab(self, url: str, *, settle: float = 12.0, keep_other: bool = True
                 ) -> bool:
        """**自己开一个新标签页**并连上去，把旧的收掉。

        这是桌面版最可靠的一条路（实测反复验证过）：

        * 旧标签页被来回导航几次之后会变得**半死** —— `1+1` 秒回、
          `document.body` 也读得出来，可 `fetch` 永远超时；而且它还会
          **占住本站的连接名额**，让新页面卡在 `readyState=loading`。
        * CDP 新建的标签页是全新的渲染进程，接口一次就通。实测同一个
          `queryCourseItemList`，旧页上超时、新页上 `returnCode=S0000`。

        `keep_other=True` 时**保留别的标签页**。默认保留是怕误关掉用户
        自己在看的页面；调用方（`desktop_watch`）每门课跑完会显式收一次。
        """
        old = self.tab_id
        try:
            tid = new_tab(url, port=self.port, settle=settle)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[desk] 开新标签页失败: {type(exc).__name__}: {exc}")
            return False
        found = next((t for t in tabs(port=self.port) if t["id"] == tid), None)
        if found is None:
            self.log("[desk] 新标签页开了但列表里找不到它")
            return False
        ws = _live(found["webSocketDebuggerUrl"])
        if ws is None:
            self.log("[desk] 新标签页刚开就是僵尸页（这台模拟器状态不好）")
            close_tab(tid, port=self.port)
            return False
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.ws = ws
        self.tab_id = tid
        self._used_tabs.add(tid)
        if old and old != tid and not keep_other:
            close_tab(old, port=self.port)
        self.log(f"[desk] 开了新标签页 [{tid[:8]}] {url[:60]}")
        return True

    def close_extra_tabs(self, *, keep: set[str] | None = None) -> int:
        """把不是当前在用的标签页收掉。返回关掉几个。

        用户明确要求过（m11305）「不要开这么多页面了，不然 cookie 都保存
        不下来」—— 实测开太多页确实会把浏览器拖垮（6 个标签页时
        `fetch` 全超时）。所以每门课收尾都要清一次。
        """
        keep = set(keep or ()) | {self.tab_id}
        n = 0
        for t in tabs(port=self.port):
            if t.get("id") not in keep and close_tab(t["id"], port=self.port):
                n += 1
        if n:
            self.log(f"[desk] 收掉 {n} 个多余标签页")
        return n

    def connect(self, *, skip: set[str] | None = None, url_part: str = "") -> None:
        """挑一个**活着**的标签页连上。

        `skip`：不再用的标签页 id。`goto()` 导航失败时会换一个再来，
        换的时候要把试过的排掉，否则会一直连回同一个死页面。

        `url_part`：只在 URL 含这个子串的标签页里挑。跳转把连接冲断后
        重连时要用它 —— 那时候要连的是**课程站**那个标签页，而不是
        随便哪个还活着的（两个域名的页面同时在时，随便挑会连错站）。
        """
        import browser

        skip = skip or set()
        cands = sorted(tabs(port=self.port), key=lambda t: -_tab_score(t.get("url", "")))
        cands = [t for t in cands if t.get("id") not in skip]
        if url_part:
            cands = [t for t in cands if url_part in t.get("url", "")]
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None

        dead = 0
        for t in cands:
            ws = _live(t["webSocketDebuggerUrl"])
            if ws is None:
                dead += 1
                continue
            self.ws = ws
            self._used_tabs.add(t["id"])
            self.tab_id = t["id"]
            u = t.get("url", "")[:60]
            self.log(f"[desk] 用标签页 [{t['id'][:8]}] {u}")
            self._sync_tab_url(t["id"], t.get("url", ""))
            return
        raise RuntimeError(
            f"{len(cands)} 个标签页一个都不能用（{dead} 个探活就死）—— "
            "在模拟器里把浏览器彻底关掉再打开一次")

    def _sync_tab_url(self, tab_id: str, claimed: str) -> None:
        """核对「连上的标签页」是不是真的停在那。

        为什么要核对：`/json/list` 报的 URL **会失真**。实测踩到 ——
        同一个标签页在 `/json/list` 里一直写着
        `course.zs-hospital.sh.cn/...studentIndex`（于是 `_tab_score` 给它
        打最高分、`connect()` 每次都挑它），可它其实早就被同页内的
        `Page.navigate` 带回了 `elearning/.../personalCenter`，连正文都是
        课程列表。结果就是：**连上的是这个标签页，读到的却是另一个页面**，
        后面所有"找不到课件列表"之类的怪事都是这么来的。
        判断真伪只认 `location.href`（页内自己报的）。

        **自相矛盾的那一条要留**：登记地址和真实地址不一致的标签页，恰恰
        是**好**标签页（它真的被导航过），而登记地址"对得上"的那个才可能
        是僵尸。所以这里不能像第一版那样把"对不上"的直接跳过 —— 反了。
        对不上的用 `_live()` 探活（僵尸页连 `location.href` 都问不出，
        探活就把它筛掉了），能干活就换过去。

        换不过去时**打印一行原因**，不要静默 —— 第一版就是静默返回，
        结果 `open()` 里 `logged_in()` 打在僵尸标签页上超时，日志里
        只有"没登录"，排查了半天。
        """
        import browser

        try:
            real = str(self.js("location.href"))
        except Exception:  # noqa: BLE001
            return
        if not real or real == claimed or real.startswith("about:"):
            return
        self.log(f"[desk] 标签页 [{tab_id[:8]}] 的登记地址是旧的"
                 f"（登记 {claimed[:40]} / 实际 {real[:40]}）")
        tried = 0
        for t in tabs(port=self.port):
            if t.get("id") == tab_id or not t.get("url"):
                continue
            if "zs-hospital" not in t.get("url", ""):
                continue
            tried += 1
            ws = _live(t["webSocketDebuggerUrl"])
            if ws is None:
                continue
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self.ws = ws
            self._used_tabs.add(t["id"])
            self.tab_id = t["id"]
            self.log(f"[desk] 改连 [{t['id'][:8]}]（{str(t.get('url', ''))[:50]}）")
            return
        self.log(f"[desk] 别的标签页也没得换（试了 {tried} 个）—— 先用着这个")

    def open(self, *, restart: bool = False, wait_login: float = 0.0,
             log: Callable[[str], None] | None = None, **_kw) -> bool:
        """把浏览器摆成桌面版 + 准备好框架控制器，成功返回 `True`。

        `restart=True` 会重启浏览器（丢掉当前标签页和 cookie）。默认
        **不重启** —— 登录态是 cookie，重启**有概率丢**（实测：用
        `am force-stop` 硬停浏览器之后登录态就没了），所以能不重启就不重启。

        `wait_login > 0`：发现没登录时**等**用户在那个浏览器窗口里登录完
        （每 `LOGIN_POLL_SECONDS` 秒查一次），而不是直接抛错。

        顺序很讲究，实测踩过坑：
          1. `connect()` 挑一个活标签页；
          2. **先** `Page.enable` + `set_desktop_ua`；
          3. 再 `Page.navigate`。
        反过来（页面已经按手机 UA 加载完再改 UA）会让页面卡死。
        """
        import browser

        if restart:
            browser.launch_debug(url="about:blank", log=self.log)
        try:
            self.connect()
        except RuntimeError:
            browser.launch_debug(url="about:blank", log=self.log)
            self.connect()
        try:
            self.ws.call("Page.enable")
        except Exception:  # noqa: BLE001 - 有的 target 不支持，不影响
            pass
        browser.set_desktop_ua(self.ws, log=self.log)
        # 给 UA 覆盖留一拍：两条命令挤在一起时实测出现过
        # 「导航先跑、UA 后到」——页面已经按手机 UA 打开，白搭。
        time.sleep(1.5)

        if not self.logged_in():
            # 没登录时**先把登录页开出来**，再等人。
            #
            # 原来只打一句「先登录再跑」就完事 —— 但这时候标签页多半还停在
            # `about:blank`（浏览器刚被系统杀掉、又被我们拉起来）或者某个早就
            # 过期的旧地址上，用户在模拟器里**根本看不到登录表单**，得自己手打
            # 网址。2026-10-08 实测踩到：浏览器被 Android 后台杀掉之后整个
            # cookie 罐是空的（`Network.getAllCookies` 回 0 条），提示说「先登录
            # 再跑」却没地方登。
            try:
                self.goto(LOGIN, settle=4.0)
                self.log(f"[desk] 登录页已打开：{LOGIN}")
            except Exception as exc:  # noqa: BLE001 - 打不开就照原样提示，别盖掉真正的原因
                self.log(f"[desk] 登录页没打开（{exc.__class__.__name__}: {exc}）")
            if wait_login <= 0:
                raise NotLoggedIn(self.LOGIN_HINT)
            self.log("")
            self.log("=" * 70)
            self.log(self.LOGIN_HINT)
            self.log(f"（等你最多 {_human_wait(wait_login)}）")
            self.log("=" * 70)
            deadline = time.monotonic() + wait_login
            while time.monotonic() < deadline:
                time.sleep(LOGIN_POLL_SECONDS)
                if self.logged_in():
                    self.log("[desk] ✓ 登录成功，继续")
                    break
            else:
                raise NotLoggedIn(
                    f"等了 {_human_wait(wait_login)} 还是没登录 —— 先登录再跑")

        from controller import build_controller, load_config
        from maa.resource import Resource
        from maa.tasker import Tasker

        import inference

        cfg = load_config()
        self.controller = build_controller(cfg, log=_quiet)
        res = Resource()
        # ★ 必须在 post_bundle / post_ocr_model 之前设，见 scripts/inference.py 的模块注释。
        self.log(f"[desk] 推理设备 {inference.apply(res, cfg, log=self.log)}")
        if not res.post_bundle(str(paths.resource_dir())).wait().succeeded:
            raise RuntimeError("资源读不出来（assets/resource）")
        res.post_ocr_model(str(paths.ocr_model_dir())).wait()
        self.tasker = Tasker()
        if not self.tasker.bind(res, self.controller):
            raise RuntimeError("框架初始化失败（tasker.bind）")
        self.tasker.set_log_dir(str(paths.log_dir()))
        self.tasker.set_save_draw(True)
        self.tasker.set_save_on_error(True)
        self.log("[desk] 控制器 + 识别模型就绪")
        return True

    #: 没登录时给人看的提示。
    LOGIN_HINT = "浏览器没登录（或者登录态被清掉了）"

    def logged_in(self, *, wait: float = 0.0) -> bool:
        """现在有没有登录态。

        `wait > 0` 时轮询到登录为止 —— 在页面上点完「立即登录」之后
        要等接口回来、cookie 落盘，不是点完就有的。
        """
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                ok = self.js(LOGIN_JS) is True
            except Exception:  # noqa: BLE001
                ok = False
            if ok or time.monotonic() >= deadline:
                return bool(ok)
            time.sleep(LOGIN_POLL_SECONDS)

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None
        self.tasker = None
        self.controller = None

    # -- 页面问答 --------------------------------------------------------

    def js(self, expression: str):
        """跑一段 JS 并拿回它的值。"""
        if self.ws is None:
            raise RuntimeError("会话没打开（先 open()）")
        return self.ws.evaluate(expression)

    def where(self) -> dict:
        """现在在哪一页。返回 `{url, title, text, video}`。

        连不上时返回**空字典**，不抛异常：换域名导航（我的学习 →
        课程站）会让这个 WebSocket 断掉，而调用方（比如 `open_course`
        等跳转结果）**恰恰是在跳转之后**才来问"到了没" —— 这里抛异常
        会把"其实已经跳过去了"误判成"点失败了"（实测踩过：点开课后
        真的进了课程页，日志却打「点了但没进课程页」，就是因为问路时
        连接已经断了）。
        """
        try:
            val = self.js(WHERE_JS)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[desk] 问不到当前页面（连接断了）: {type(exc).__name__}")
            return {}
        if isinstance(val, str):
            try:
                return json.loads(val)
            except ValueError:
                return {"text": val}
        return {}

    #: 判「页面渲染出来了没」—— `readyState` 加上「真的有 body」。
    #:
    #: 只看 `readyState` 不够：卡死的页面会一直停在 `loading`，
    #: 但**不卡的时候** SPA 也可能长时间停在 `interactive`；
    #: 只看 `body` 也不够：`about:blank` 有 body 但什么都没加载。
    #: 两个一起看，再要求 URL 不是空的，才是"能用"。
    LOADED_JS = ("JSON.stringify({r: document.readyState, u: location.href,"
                 " b: document.body ? document.body.innerHTML.length : -1})")

    def reconnect_same(self, *, timeout: float = 5.0) -> bool:
        """整页跳转把 WebSocket 打断后，重连**同一个标签页**。

        为什么不交给 `connect()`：它是"按打分挑一个" —— 而旧标签页的
        登记地址常常是过期的（`/json/list` 报着课程站的地址，页面其实已经
        被带回 `elearning`），于是它专挑那个错的。跳转之后我们**知道**
        自己要的还是同一个 tab，直接按 id 回去最保险。
        """
        import browser

        if not self.tab_id:
            return False
        for t in tabs(port=self.port):
            if t.get("id") != self.tab_id:
                continue
            try:
                ws = browser.WS(t["webSocketDebuggerUrl"], timeout=timeout)
                ws.call("Runtime.evaluate", expression="1+1", returnByValue=True)
                ws.timeout = 40.0
                if ws.sock is not None:
                    ws.sock.settimeout(40.0)
            except Exception:  # noqa: BLE001
                return False
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self.ws = ws
            return True
        return False

    def _loaded(self) -> bool:
        try:
            raw = self.js(self.LOADED_JS)
            d = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:  # noqa: BLE001
            # 连接被整页跳转打断了 —— 重连同一个标签页再问一次。
            if self.reconnect_same():
                try:
                    raw = self.js(self.LOADED_JS)
                    d = json.loads(raw) if isinstance(raw, str) else {}
                except Exception:  # noqa: BLE001
                    return False
            else:
                return False
        return (d.get("r") == "complete" and d.get("b", -1) > 0
                and str(d.get("u", "")).startswith("http"))

    def _url(self) -> str:
        try:
            return str(self.js("location.href"))
        except Exception:  # noqa: BLE001
            if self.reconnect_same():
                try:
                    return str(self.js("location.href"))
                except Exception:  # noqa: BLE001
                    return ""
            return ""

    def _at(self, url: str) -> bool:
        """现在是不是已经在 `url` 上，而且页面**已经渲染出来**。

        **必须连 `#` 后面的路由一起比。** 踩过的坑：原来把 hash 去掉再比
        （想着"都是同一个页面，fragment 变一点不该算换页"），于是
        `goto(...#!/index/course/learn/courseware/video?itemId=…)` 在地址是
        `...#!/index/course/home?courseId=…` 的时候被判成"已经在目标页"，
        直接 return，**根本没导航**。而课程站是 SPA：地址栏里的路由不换，
        AngularJS 就不会重新渲染，页面于是空着 —— 表现出来是
        「页面上找不到『在线学习』」「接口回 `E0002 系统处理出错啦!`」
        「顶部 tab 一个都数不出来」，害我往接口和登录态上查了半天。

        另外 `_loaded()` 也必须在前面：`about:blank` 的 `location.href`
        有时候和 `Page.navigate` 的目标"看起来一样"（实测踩过：连上
        `about:blank` 之后直接判"已经在"，于是没导航，后面全落空）。

        规则：两边都带 hash 就整个比；目标没带 hash 就只比地址部分
        （`我的学习` 那种地址本来就没有 hash）。
        """
        if not self._loaded():
            return False
        here, want = self._url().rstrip("/"), (url or "").rstrip("/")
        if not here:
            return False
        if "#" in want:
            return here == want
        return here.split("#")[0] == want.split("#")[0]

    def goto_route(self, base: str, route: str, *, settle: float = 10.0,
                   tries: int = 3, force: bool = False) -> bool:
        """导航到 `base` 这个 SPA 页面里的某条 hash 路由。

        为什么要整页重新载入（而不是只改 `location.hash`）：只改 hash
        的时候 AngularJS 要是没接住这次路由变化（实测会），页面就停在
        上一次的状态，接口跟着报 `E0002 系统处理出错啦!`。整页载入
        慢一点（十来秒），但路由一定会被初始化。

        `force=True` 强制真的导航一次，**即使地址看起来已经在目标页** ——
        这一条是 2026-10-08 补的：地址栏的 hash 和 AngularJS 实际渲染的
        路由**会脱节**（改 hash 之后地址变了、播放器却没换讲），这时候
        "已经在目标页"这个判断是错的，照着它会白白跳过导航，切讲永远
        切不过去。所以"要重建页面状态"的调用（切讲）一律走 `force=True`。
        """
        self.goto(f"{base}#!{route}", settle=settle, tries=tries, force=force)
        return self._loaded()

    def goto(self, url: str, *, settle: float = 8.0, tries: int = 3,
             force: bool = False) -> None:
        """硬导航到某个 URL，并等到页面**真的渲染出来**。

        为什么用 `Page.navigate` 而不是点链接：实测**点不动** ——
        桌面版顶部导航的 `我的学习` 是真鼠标事件也点不动（Vue 的
        handler 拦了一层），但它的目标 URL 是已知且稳定的。
        点击只在「必须由页面自己决定去哪」的时候用（比如课程列表的
        `去学习`，目标 URL 里带 `itemId`，只能让它自己算）。

        ## 为什么要换标签页重试

        实测这个浏览器里会攒下**死标签页**：WebSocket 打得开、
        `Runtime.evaluate("1+1")` 也答得上来（所以探活筛不掉），
        但它占着本站的连接名额，导致**导航永远卡在 `readyState=loading`、
        `document.body` 是 `null`**。同一个标签页怎么重试都没用
        （`Page.reload` 也一样卡着），**换一个标签页反而立刻就好**。
        所以这里的重试是「换标签页」而不是「再 navigate 一次」。
        """
        if self.ws is None:
            raise RuntimeError("会话没打开（先 open()）")

        for attempt in range(1, max(1, tries) + 1):
            if not force and self._at(url):
                # 已经在目标页（可能只是 hash 变了）—— 页内跳转不重新拉资源，
                # 反而是最稳的一种"导航"。
                self.log(f"[desk] 已经在目标页: {url[:70]}")
                return
            try:
                self.ws.call("Page.navigate", url=url)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[desk] navigate 就失败了（{type(exc).__name__}），换标签页")
                self.connect(skip=set(self._used_tabs))
                continue
            time.sleep(settle)
            if self._loaded():
                return
            self.log(f"[desk] 第 {attempt} 次没打开（停在 "
                     f"{self._url()[:60]}，页面没渲染出来）")
            if attempt < tries:
                self.connect(skip=set(self._used_tabs))
        raise RuntimeError(f"打不开 {url}（最后停在 {self._url()[:80]}）")

    # -- 找东西 / 点东西 -------------------------------------------------

    def find_text(self, text: str) -> dict | None:
        """按文本找一个元素，返回它的视口坐标。找不到返回 `None`。"""
        raw = self.js(FIND_JS.replace("__T__", json.dumps(text)))
        if not isinstance(raw, str) or raw == "null":
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def click_at(self, x: float, y: float, *, settle: float = 0.5) -> None:
        """在**视口坐标**上发一次真鼠标点击。

        为什么必须发真事件：`element.click()` 对这个平台的 Vue 组件
        **无效**（实测：点了 URL 不变、页面毫无反应），而
        `Input.dispatchMouseEvent` 三连（move/press/release）有用。
        """
        if self.ws is None:
            raise RuntimeError("会话没打开（先 open()）")
        for kind, buttons in (("mouseMoved", 0), ("mousePressed", 1),
                              ("mouseReleased", 0)):
            self.ws.call("Input.dispatchMouseEvent", type=kind, x=float(x), y=float(y),
                         button="left", clickCount=1, buttons=buttons)
            time.sleep(0.2)
        time.sleep(settle)

    def click_text(self, text: str, *, settle: float = 4.0) -> bool:
        """按文本找元素并点它。找不到返回 `False`。"""
        box = self.find_text(text)
        if not box:
            self.log(f"[desk] 页面上找不到「{text}」")
            return False
        self.click_at(box["x"], box["y"], settle=settle)
        self.log(f"[desk] 点了「{text}」（{box['tag']}.{box['cls'][:20]} "
                 f"@ {round(box['x'])},{round(box['y'])}）")
        return True

    def tap_canvas(self, x: int, y: int, *, settle: float = 1.0) -> None:
        """用框架的控制器在**画布坐标**上点一下（走 MaaTouch）。

        桌面版大多数点击用 `click_at` 就够了。留着这条是因为有些控件
        （播放器自己画的进度条、弹层）只认真实触摸。
        """
        if self.controller is None:
            raise RuntimeError("会话没打开（先 open()）")
        self.controller.post_click(int(x), int(y)).wait()
        time.sleep(settle)

    # -- 课程 -----------------------------------------------------------

    #: 课程列表接口。**页面自己翻页用的就是它**（`debug/examjs/pc.js:295 loadData`）。
    #:
    #:     POST /user/getMyCourseList
    #:     projectId=<项目>&finishType=<全部/未结课/已结课>&pageIndex=<0 基>
    #:     → res.data.record = {courseList, pageIndex, pageSize, totalCount, studyNotice}
    #:
    #: 空 `projectId` + 空 `finishType` 就是"全部"（实测页面自己的默认请求
    #: 也是这么发的：`currentProject = {name:"全部", id:""}`、`finishType = ""`）。
    COURSE_API = "/user/getMyCourseList"

    @staticmethod
    def _map_course(c: dict) -> dict:
        """把接口那条原始课程记录**映射成和 `COURSES_JS` 一样的字段名**。

        这一步不是多余的：接口回的是 `isFinishCourse` / `userClassScoreDesc`，
        而页面上那一路映射出来的是 `isFinish` / `desc`。两路都喂给同一个
        `desktop_exam.py`，字段名不统一的话调用方就得写两套判断。
        """
        return {
            "id": c.get("id"),
            "name": c.get("name") or "",
            "userClassId": c.get("userClassId") or "",
            "hour": c.get("hour"),
            "isFinish": c.get("isFinishCourse"),
            "finishDate": c.get("finishCourseDate") or "",
            "desc": c.get("userClassScoreDesc") or "",
            "canStudy": c.get("canStudyFlag"),
            "isOverdue": c.get("isOverdue"),
            "overdueDate": c.get("overdueDate") or "",
            "hasCertificate": c.get("hasCertificate"),
            "projectName": c.get("projectName") or "",
            "classAssessmentDesc": c.get("classAssessmentDesc") or "",
            "isPass": c.get("isPass"),
        }

    def all_courses(self, *, log: Callable[[str], None] | None = None,
                    max_pages: int = 50) -> list[dict]:
        """读**全部**课程（跨分页）。拿不到返回空列表。

        为什么需要它：`courses()` 读的是页面上 Vue 手里那份 `courseList`，
        而那个列表是**分页**的 —— 实测 `pageSize=8`、`totalCount=17`、
        分页控件是「上一页 1 2 3 下一页」。只读页面就只看得见第一页 8 门，
        剩下 9 门根本不知道存在。2026-10-08 用户指出
        「下面可以切换页码，确保所有的都写进程序里了」，一查果然漏了 9 门。

        翻页走页面自己的接口（见 `COURSE_API`）而不是去点分页控件：
        控件在 y≈1939（视口外），点它还得先滚过去，而且点完还要等 Vue 重渲染；
        接口一路 `pageIndex` 递增就行，`pageIndex` **是 0 基**。

        停止条件三个，取最先到的：接口回空页、`totalCount` 收满、页数上限
        （`max_pages`，防接口抽风时无限打）。
        """
        out: list[dict] = []
        seen: set = set()
        total: int | None = None
        for page in range(max(1, max_pages)):
            try:
                raw = self.post_form(self.COURSE_API,
                                     f"projectId=&finishType=&pageIndex={page}")
            except Exception as exc:  # noqa: BLE001
                if log is not None:
                    log(f"[desk] 读课程第 {page + 1} 页失败：{type(exc).__name__}: {exc}")
                break
            try:
                res = json.loads(raw or "{}")
            except ValueError:
                if log is not None:
                    log(f"[desk] 读课程第 {page + 1} 页回执不是 JSON：{str(raw)[:120]}")
                break
            if not isinstance(res, dict) or not res.get("success"):
                # 不在「我的学习」那一页时这个接口会 404 / 回 success=false，
                # 这是正常情况（课程域上就没有它），不当错误刷屏。
                break
            rec = res.get("record") or {}
            if isinstance(rec.get("totalCount"), int):
                total = rec["totalCount"]
            lst = rec.get("courseList") or []
            fresh = 0
            for c in lst:
                cid = c.get("id")
                if cid and cid in seen:
                    continue
                if cid:
                    seen.add(cid)
                out.append(self._map_course(c))
                fresh += 1
            if not lst or fresh == 0:
                break
            if total is not None and len(out) >= total:
                break
        if log is not None and out:
            log(f"[desk] 平台一共 {total if total is not None else '?'} 门课，"
                f"翻页读到 {len(out)} 门")
        if total is not None:
            # 记下来给调用方用（见 `course_total` 的说明）。
            self.course_total = total
        return out

    def _course_meta(self) -> dict:
        """当前页的分页信息（`{}` = 读不到）。"""
        raw = self.js(COURSE_META_JS)
        if not isinstance(raw, str) or raw == "null":
            return {}
        try:
            d = json.loads(raw)
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def courses(self, *, wait: float = 0.0, log: Callable[[str], None] | None = None
                ) -> list[dict]:
        """「我的学习」页里的课程列表 —— **全部**，不只当前页。拿不到返回空列表。

        `wait > 0` 时**轮询**到列表出来为止：这个页面是 Vue 懒加载的，
        导航过去之后课程列表要等接口回来才有。实测只等 9 秒偶尔还是空的
        （第一次跑就撞上了 —— 页面标题都对、列表却是 `[]`），
        所以宁可多等一会儿，也别把「还没加载完」误判成「页面改版了」。

        ★ **会自动翻页**：这个列表是分页的（`pageSize=8`、`totalCount=17`），
        只读页面就只看得见第一页。判据是页面自己报的 `totalCount` ——
        拿到的条数比它少就说明还有下一页，这时改用 `all_courses()` 把
        剩下的也读出来。这样调用方（`desktop_exam.py` / `desktop_watch.py`）
        不用改一行就自动看到全部课程。
        """
        deadline = time.monotonic() + max(0.0, wait)
        last_n = -1
        while True:
            raw = self.js(COURSES_JS)
            if isinstance(raw, str) and raw != "null":
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = []
                if data:
                    return self._extend_pages(data, log=log)
                last_n = 0
            if time.monotonic() >= deadline:
                if last_n == 0:
                    # 页面上读不到，但接口也许能回 —— 接口是更可靠的来源，
                    # 试一下再决定要不要报"读不到课程列表"。
                    more = self.all_courses(log=log)
                    if more:
                        return more
                if log is not None:
                    if last_n == 0:
                        log("[desk] 页面在「我的学习」，但课程列表还是空的"
                            "（接口没回来，或者这门账号下确实没有课）")
                    else:
                        # `last_n == -1`：连 Vue 实例都没找到 —— 十有八九
                        # 是停在个人中心的**别的 module** 上（问卷/订单/证书），
                        # 那些页面上没有 `div.my-study`。别让它悄悄返回空表。
                        log("[desk] 这个页面上没有 Vue 课程数据 —— 可能不是"
                            "「我的学习」（`module=learning`）那一页")
                return []
            time.sleep(1.5)

    def _extend_pages(self, page_one: list[dict],
                      *, log: Callable[[str], None] | None = None) -> list[dict]:
        """当前页读到了，但平台还有更多页时，把全部课程读回来。"""
        meta = self._course_meta()
        total = meta.get("total")
        if isinstance(total, int):
            # 页面自己报的总数先记下来：即使下面翻页失败、或者本来就只有
            # 一页，调用方也还能拿到平台说的「一共几门」（比 `len(page_one)` 准）。
            self.course_total = total
        if not isinstance(total, int) or total <= len(page_one):
            return page_one
        if log is not None:
            log(f"[desk] 「我的学习」这一页只显示 {len(page_one)} 门，"
                f"平台一共 {total} 门 —— 按分页把后面的也读出来")
        more = self.all_courses(log=log)
        # 接口要是没读全（网络抖动/改版），宁可把页面这一页也留着，
        # 别因为翻页失败反而比原来读到的还少。
        if len(more) >= len(page_one):
            return more
        return page_one

    def lessons(self) -> list[dict]:
        """课程页左侧的讲次列表。"""
        raw = self.js(LESSONS_JS)
        if not isinstance(raw, str):
            return []
        try:
            return json.loads(raw)
        except ValueError:
            return []

    def video(self) -> dict | None:
        """当前播放状态。没有 `<video>` 返回 `None`。"""
        raw = self.js(VIDEO_JS)
        if not isinstance(raw, str) or raw == "null":
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -- 进课程 / 进讲次 --------------------------------------------------

    #: 进课的入口。`{}` 里填平台课程列表里的 `courseList[].id`。
    ENTER_URL = ("/index/thirdParty/learnCenter/userEnterClass"
                 "?courseId={}&sourcePage=2")

    def enter_course(self, platform_id: str, *, settle: float = 22.0,
                     tries: int = 3) -> bool:
        """靠平台自己的入口页进课程站，成功后落在课程首页。

        走的是「我的学习」里那个「去学习」按钮**内部用的那条地址**：:

            location.href = /index/thirdParty/learnCenter/userEnterClass
                            ?courseId=<平台 courseList[].id>&sourcePage=2

        ## 为什么不用点「去学习」

        那个按钮点不动，两层原因叠在一起（都是实测出来的）：

          1. 页面上挂着一个**卡死的 Element UI 全屏遮罩**
             `div.el-loading-mask` —— `opacity:0`（看不见）但
             `pointer-events:auto`（照吃点击），铺满整个视口。`elementFromPoint`
             在按钮坐标上回的就是这个遮罩。真鼠标、合成 DOM 事件、MaaTouch
             三种点法页面**一条事件都收不到**；
          2. 它的处理函数里是 `var w = window.open(); w.location.href = …`，
             弹窗拦截器一挡，`w` 是个空壳，赋值静默失败。

        直接把那条地址赋给 `location.href` 两个问题都没有 ——
        **同标签页导航，不弹窗、不点击**。

        ## 为什么「课程站的 courseId」不用我们操心

        课程站要的 `courseId` 跟平台的 `courseList[].id` / `userClassId`
        **都不是一个东西**（实测：平台 id 和 userClassId 拿去都给
        「没有当前选课或选课无效！」）。它是服务端在这一跳里现给的：:

            elearning /index/thirdParty/learnCenter/userEnterClass?courseId=<平台id>
              → 302 → course /index/thirdParty/tyxxConnect/userEnter
              → 课程站首页 …studentIndex.action#!/index/course/home?courseId=8a8f…

        之后从地址栏读 `course_id()` 就行，不需要任何映射表。

        ## 踩过的两个坑

        * **这条 URL 属于 elearning 域**。一开始我拿它往 **course 域**上
          导航，得到 404「找不到 course.zs-hospital.sh.cn 的网页」，
          于是误判成"这条路是死的" —— 白绕了一大圈。
        * 没登录时 `userEnter` 会回一段 JSON
          ``{"success":false,"message":"未登录或登录状态已失效"}``
          （页面正文就是它），不会跳走。所以进入前要确认登录态。
        """
        if not platform_id:
            self.log("[desk] 没给平台课程 id，进不了课")
            return False
        entrance = f"{ELEARN}{self.ENTER_URL.format(platform_id)}"
        for attempt in range(1, max(1, tries) + 1):
            if self.ws is None:
                raise RuntimeError("会话没打开（先 open()）")
            self.log(f"[desk] 走平台入口进课（第 {attempt} 次）: "
                     f"userEnterClass courseId={platform_id[:16]}…")
            try:
                self.ws.call("Page.navigate", url=entrance)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[desk] 导航失败（{type(exc).__name__}），重连一次")
                self.reconnect_same()
                continue
            time.sleep(settle)
            url = self._url()
            if "studentIndex" in url and self.course_ok():
                self.log(f"[desk] ✓ 进了课程页: {url[:92]}")
                return True
            text = (self.where().get("text") or "")[:70]
            self.log(f"[desk] 没进课程页（现在 {url[:70]}｜{text}）")
            time.sleep(2.0)
        return False

    def _enter_hint(self) -> str:
        """进不了课时给人看的一句解释。"""
        return ("进不了课程页 —— 先确认浏览器里是登录状态"
                "（未登录时入口会回「未登录或登录状态已失效」）")

    def follow_new_tab(self, before: set[str], *, url_part: str = "",
                       timeout: float = 25.0) -> bool:
        """等页面自己开出来的新标签页，并连上去。成功返回 `True`。

        为什么需要它：「去学习」不是页内跳转，而是**新开一个标签页**
        （实测：点完当前标签页的 URL 一点没变，`/json/list` 里却多出
        一个 `course.zs-hospital.sh.cn/...studentIndex` 标签页）。
        不知道这件事就会出现很迷惑的现象 —— 点成功了、课程页也真开了，
        但程序连着的还是原来那个 tab，于是"点了但没进课程页"。

        `before`：点之前用 `tab_ids()` 取的那一份，用来找出多出来的是哪个。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fresh = [t for t in tabs(port=self.port)
                     if t.get("id") not in before
                     and (not url_part or url_part in t.get("url", ""))]
            if fresh:
                new_id = fresh[0]["id"]
                old = {t["id"] for t in tabs(port=self.port)} - {new_id}
                self.tab_id = new_id
                self.connect(url_part=url_part or "zs-hospital")
                # 用户要求过别攒标签页（cookie 会存不下来），但**「我的学习」
                # 那一页要留着** —— 它记着课程列表，关掉就得重新导航一次。
                for tid in old:
                    if tid in self._used_tabs:
                        continue
                    close_tab(tid, port=self.port)
                self.log(f"[desk] 页面上新开了标签页 [{new_id[:8]}]"
                         f"{'，顺手关掉旧的 ' + str(len(old)) + ' 个' if old else ''}")
                return True
            time.sleep(1.5)
        return False

    def find_in_course(self, name_contains: str, button: str) -> dict | None:
        """在「名字含 `name_contains` 的那门课」的卡片里找 `button`，返回视口坐标。

        存在的理由：列表里每门课都有同一个按钮文字，按文本全局找会点到
        **第一门课**（实测踩过：想开「肝胆」那门，结果开了「老年认知症」）。
        """
        raw = self.js(
            FIND_IN_COURSE_JS
            .replace("__NAME__", json.dumps(name_contains))
            .replace("__BTN__", json.dumps(button))
        )
        if not isinstance(raw, str) or raw == "null":
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _scroll_to_course(self, name_contains: str) -> bool:
        """把某门课的卡片滚到可见处。"""
        raw = self.js(r"""
        (() => {
          const want = __T__;
          for (const e of document.querySelectorAll('li,div,section')) {
            const t = (e.textContent || '').replace(/\s+/g, '');
            if (t.includes(want) && t.length < 200) {
              e.scrollIntoView({block: 'center'});
              return 'ok';
            }
          }
          return 'null';
        })()
        """.replace("__T__", json.dumps(name_contains)))
        time.sleep(1.0)
        return raw == "ok"

    def open_courseware(self, item_id: str = "", *, tries: int = 3) -> bool:
        """从课程首页进「课件」页（左侧有讲次列表、右边有播放器的那一页）。

        **不点顶部导航的「在线学习」**（2026-10-08 实测，改掉了旧实现）：
        那个导航项点得动，但落点不由我们决定 —— 它走的是
        `ng-click="handleChangeNav(navObj)"`，去哪个子路由由服务端记的
        lastRecord 和 `courseNavMenuConfig` 一起决定。实测点下去 URL 确实
        变了，变的是

            #!/index/course/learn/courseware/homework?itemId=…&courseId=…

        —— **作业路由，不是视频路由**。于是 `lessons()` 数得出 11 条、
        `video()` 却是 None，旧实现只报一句「点了没到课件页」，看不出
        到底哪里不对（点成功了、页面也真换了，就是换错了地方）。

        现在改成**指定要哪一讲，然后整页载入那一讲的路由** ——
        `goto_lesson()` 里 `itemId` 是地址栏里写死的，落点确定，
        而且它会拿 `active_lesson()` 核对播放器是不是真认了这一讲
        （那个坑见 `goto_lesson` 的长注释）。

        `item_id` 留空时自己从 `items()` 里挑第一讲。挑不出来（接口没
        数据 / 这门课一节视频都没有）才算失败。

        到了怎么认：左侧 `.course_chapter_item` 数得出来 **而且**
        页面上真有 `<video>`。只看 URL 不够 —— hash 路由变了但
        组件没渲染完的时候，读到的还是上一个页面。
        """
        if self.lessons() and self.video():
            self.log("[desk] 已经在课件页")
            return True

        target = item_id
        if not target:
            got = self.items()
            if not got:
                self.log("[desk] 读不到讲次列表，没法进课件页")
                return False
            target = got[0]["item_id"]
            self.log(f"[desk] 从接口挑第一讲进课件页："
                     f"{got[0]['title'][:40]}")

        for attempt in range(1, max(1, tries) + 1):
            if self.goto_lesson(target):
                self.log(f"[desk] ✓ 到课件页了（{self._url()[-70:]}）")
                return True
            self.log(f"[desk] 第 {attempt} 次进课件页失败"
                     f"（现在在 {self._url()[-60:]}）")
        return False

    # -- 平台 API（权威判据） ---------------------------------------------

    def api(self, function_code: str, **params) -> dict:
        """调一次平台的数据接口，返回解析后的 dict。失败返回 `{}`。

        这些 `functionCode` 不是猜的 —— 是抓页面自己发的 XHR 抄下来的，
        参数名一模一样（见 DEVELOPMENT.md 里那张接口表）。
        """
        q = {"functionCode": function_code}
        q.update({k: v for k, v in params.items() if v not in (None, "")})
        raw = self.js(API_JS.replace("__P__", json.dumps(q)))
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if data.get("returnCode") != "S0000":
            self.log(f"[desk] 接口 {function_code} 回 {data.get('returnCode')}"
                     f" {data.get('returnMessage')}")
        return data

    def course_id(self) -> str:
        """当前课程页地址里的 `courseId`。"""
        return _first(r"courseId=([0-9a-zA-Z]+)", self._url())

    def items(self) -> list[dict]:
        """这门课**所有视频讲座**，带平台自己的学习状态。

        一次 `queryCourseItemList` 拿到整棵章节树，摊平成
        `[{n, item_id, title, status, done, section, chapter}]`。

        **为什么要走接口而不是读左侧列表**：左侧那个小圆点是骗人的 ——
        实测第 1 讲服务端明明 `status=2`（学完），列表里还是灰的
        `fa-circle-o`（`LESSONS_JS` 全报 `'new'`）。而且 DOM 里
        `.course_chapter_item` 既没有 `data-id`、`id` 也是空，
        AngularJS 的 scope 上也挖不到 `chapterList`（试过，0 条）。
        接口给的 `status`（0 没看 / 1 看了一半 / 2 学完）是唯一权威判据。

        **判据是 `wareType == "jz"`，不是 `wareTypeName == "讲座"`。**
        曾经写的是后者，一度让整个看课流程静默失效：平台在**同一个账号
        下混用两种叫法**，有的课把视频讲座标成 `讲座`，有的标成 `视频`
        （实测分布 39 个 `讲座/jz` + 138 个 `视频/jz`，两种的 `wareType`
        都是 `"jz"`）。按 `wareTypeName` 卡，凡是用「视频」那套叫法的课
        `items()` 就恒返回 0 → `pending()` 恒为空 → 看课脚本以为所有课
        都看完了，一节都不播，而且日志里只看得到一句「没有要看的课」。
        按 `wareType == "jz"` 卡两种都收得到。

        `作业`/`tk`（就是「本项目考核」）显式排掉：考核不在视频这条路上
        （它走顶部「考核」tab 的另一套接口），把它算成要看的讲座会让
        `pending()` 永远还不清。
        """
        cid = self.course_id()
        if not cid:
            return []
        data = self.api("queryCourseItemList", courseId=cid)
        out: list[dict] = []

        def walk(nodes: list, section: str, chapter: str) -> None:
            for it in nodes or []:
                if not isinstance(it, dict):
                    continue
                title = str(it.get("title") or "")
                if _is_video_leaf(it):
                    st = it.get("status")
                    st = int(st) if isinstance(st, (int, str)) and str(st).isdigit() else 0
                    out.append({"n": len(out) + 1, "item_id": str(it.get("id") or ""),
                                "title": title, "status": st, "done": st >= 2,
                                "section": section, "chapter": chapter})
                    continue
                walk(it.get("childList") or [], title or section, chapter)

        for ch in data.get("chapterList") or []:
            walk(ch.get("childList") or [], "", str(ch.get("title") or ""))
        return out

    def pending(self, *, skip: int = 0) -> list[dict]:
        """还没学完（`status < 2`）的讲座，从第 `skip` 个之后开始。

        平台自己的"结课要求"是完成所有视频课件，所以这里返回的就是
        还欠的债 —— 已完成的门课会直接返回空表，不用白跑一趟。
        """
        return [i for i in self.items() if i["n"] > skip and not i["done"]]

    def hook(self) -> bool:
        """把上报钩子装上。整页导航会丢，所以每进一个新页面都该重装一次。"""
        return self.js(OBSERVE_JS) in ("ok", "already")

    def reports(self) -> list[dict]:
        """取走上报记录，解析成 `[{fn, url, res, body}]`（取完清空）。"""
        try:
            raw = json.loads(self.js(CALLS_JS) or "[]")
        except (TypeError, ValueError):
            return []
        out = []
        for r in raw if isinstance(raw, list) else []:
            url = str(r.get("url") or "")
            fn = _first(r"functionCode=([A-Za-z]+)", url)
            try:
                res = json.loads(r.get("res") or "{}")
            except ValueError:
                res = {}
            out.append({"fn": fn, "url": url, "res": res, "body": r.get("body") or ""})
        return out

    def last_record(self, item_id: str = "") -> dict | None:
        """最近一次 `sendVideoLearnRecord` 的响应里的 `learnRecord`。

        这是**服务端认可的**进度：`state` / `status`（2 = 学完）、
        `studyTime`（它自己累计的已学秒数）。页面每 60 秒上报一次，
        所以这个值最多滞后一分钟。

        `item_id` 是**必须过滤的**，这是踩出来的坑：上报记录存在
        `sessionStorage` 里，切到下一讲时上一讲最后那条记录还躺在里面。
        实测第 10 讲刚开播 1 秒，`last_record()` 就把第 9 讲那条
        `status=2` 交了出来，于是"看完"了 —— 其实是上一讲的成绩。

        记录里没有 `itemId`（它在**请求地址**的查询串里），所以从 url 抠。
        """
        for r in reversed(self.reports()):
            if r["fn"] != "sendVideoLearnRecord":
                continue
            if item_id and _first(r"itemId=([0-9a-zA-Z]+)", r["url"]) != item_id:
                continue
            lr = (r["res"] or {}).get("learnRecord")
            if isinstance(lr, dict):
                return lr
        return None

    def server_time(self, item_id: str) -> dict:
        """问接口要**服务端认定的**这一讲进度（`queryVideoItemDetail`）。

        为什么不拿 `last_record()` 顶替：那条记录是页面**自己**上报时
        服务端回给它的，实测很多时候**只回 `state`/`status`/`key`、没有
        `studyTime`** —— 于是进度行永远显示「服务端已学 0.0 分钟」，
        看着像服务端没记账（2026-10-08 差点又去白查一遍）。
        这里直接查接口，返回 `{"study_time": 秒, "complete": "0"/"1"/"2",
        "satisfied": …, "raw": {...}}`；查不到就全是 0 / 空。
        """
        cid = self.course_id()
        if not cid or not item_id:
            return {"study_time": 0.0, "complete": "", "satisfied": 0.0, "raw": {}}
        try:
            res = self.api("queryVideoItemDetail", courseId=cid, itemId=item_id)
        except Exception:  # noqa: BLE001 - 查不到不该把看课程序带崩
            res = {}
        lr = ((res or {}).get("itemObj") or {}).get("learnRecordObj") or {}
        return {
            "study_time": float(lr.get("studyTime") or 0.0),
            "complete": str(lr.get("completeStatus") or ""),
            "satisfied": float(lr.get("satisfied") or 0.0),
            "raw": lr,
        }

    # -- 看一节课 --------------------------------------------------------

    def goto_lesson(self, item_id: str, *, settle: float = 9.0) -> bool:
        """切到某一讲。成功返回 True（**并且保证播放器认的就是这一讲**）。

        **不点左侧列表，走 hash 路由** —— 列表那个 DOM 没有 id 也没有
        `data-id`，只能靠坐标点，点歪了就进错讲；而地址栏里
        `itemId` 是确定的。

        两种进入方式，按当前在哪决定：

        * 已经在课件页 → 只改 `location.hash`（快）；
        * 在课程站的别的路由上 → 整页载入课件路由。**这条路只在
          会话已经建起来之后才走**（即 `enter_course()` 进来之后）——
          没建会话的话整页载入只会得到「没有当前选课或选课无效！」。

        **判据是「播放器认的是不是这一讲」，不是「视频源变了没」** ——
        这是 2026-10-08 那次白跑 45 分钟的根因，必须写在这儿：

            改完 hash 之后地址栏确实变成了目标讲，`<video>` 的 src 也换了
            （`goto_lesson` 当时就是靠"src 变了"返回成功的），但是
            AngularJS 那边 `courseLearnCoursewareConfig.activeItemObj`
            **还停在第 1 讲**。播放器那一整套上报逻辑是围着
            `activeItemObj` 转的，于是这 45 分钟里平台一直在替**第 1 讲**
            记账（服务端第 1 讲 `studyTime` 涨到 6665），而真正要学的
            第 9 讲 `studyTime` 一动不动 —— 从外面看就是"服务端不记账"。

        所以这里每一步都要拿 `active_lesson()` 对一下 id，对不上就整页
        载入重来。
        """
        cid = self.course_id()
        if not cid or not item_id:
            return False
        route = (f"/index/course/learn/courseware/video"
                 f"?itemId={item_id}&courseId={cid}")

        if self._on_courseware():
            self.js(f"location.hash = '#!{route}'")
            if self._wait_active(item_id, settle):
                self.hook()
                return True
            self.log("[desk] 改 hash 之后播放器没认这一讲，整页载入重来")

        return self._load_lesson(item_id, route, settle=settle)

    def _load_lesson(self, item_id: str, route: str, *, settle: float = 9.0,
                     tries: int = 2) -> bool:
        """整页载入某一讲，并确认播放器认了它。

        **必须 `force=True`**：地址栏的 hash 这时候已经（被 `goto_lesson`
        的第一次尝试）改成了目标讲，但播放器没认 —— 不强制导航的话
        `goto()` 会判"已经在目标页"直接返回，切讲就永远切不过去。
        """
        for i in range(1, max(1, tries) + 1):
            ok = self.goto_route(f"{COURSE}/learning/student/studentIndex.action",
                                 route, settle=settle, force=True)
            if not ok:
                self.log(f"[desk] 第 {i} 次整页载入这一讲失败")
                continue
            if self._wait_active(item_id, settle):
                self.hook()
                return True
            act = self.active_lesson()
            self.log(f"[desk] 第 {i} 次整页载入后播放器还不是这一讲"
                     f"（它认 {str(act.get('id'))[:12]}…，"
                     f"hash 是 {str(act.get('hash_item_id'))[:12]}…，"
                     f"地址 {self._url()[:70]}）")
        self.log(f"[desk] ✗ 切不到这一讲（itemId={item_id[:12]}…）")
        return False

    def _wait_active(self, item_id: str, timeout: float) -> bool:
        """等播放器把 `activeItemObj` 换成 `item_id`。"""
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if self.active_lesson().get("id") == item_id:
                return True
            time.sleep(1.0)
        return False

    def active_lesson(self) -> dict:
        """播放器**当前认的**那一讲：`{id, name, status, position, rate, ...}`。

        读的是 AngularJS scope 上的 `courseLearnCoursewareConfig`
        `.activeItemObj` 和播放器自己的 `customObj` / `learnRecordObj`
        —— 这三个是说真话的那一层；地址栏 hash 说不了真话（改 hash
        之后它会立刻变，播放器却可能没跟着换，见 `goto_lesson` 的注释）。
        """
        raw = self.js(ACTIVE_JS)
        try:
            d = json.loads(raw) if raw and raw != "null" else {}
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    #: 「留在这张页面」的那类按钮文字。点这些不会跳走。
    KEEP_WORDS = ("取消", "关闭", "知道了", "知道了!")

    def dialogs(self) -> list[dict]:
        """现在弹着的 `layer.js` 确认框，`[{title, content, buttons}]`。

        和 `quiz()` 的区别：`quiz()` 看的是**视频弹题**（`.popup_layer`，
        要答题）；这里看的是平台自己弹的**确认框**（`.layui-layer`，
        只是问你要不要继续）。刷时长时必须处理后者 —— 视频播完就弹
        「该视频课件已观看完毕，是否继续学习下一课程节点？」，不点「取消」
        的话要么跳走、要么一直挡着。
        """
        raw = self.js(LAYER_JS)
        try:
            d = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            d = []
        return d if isinstance(d, list) else []

    def dismiss_dialog(self, *, settle: float = 0.6) -> str:
        """把弹着的确认框按「留在原地」点掉。返回一行说明；没框就返回空串。

        ★ **按文字找按钮，不按序号**：平台这两个框的按钮不一样 ——
        「该视频课件已观看完毕…」是 `["学习下一课节","取消"]`，
        「您的视频课件观看时长未达到…」是 `["确定","取消"]`。
        照序号点「第 0 个」在前者上就会跳走，正是要避免的事。

        找不到「取消」这类按钮时**什么都不点**（宁可这轮白等）：
        框里剩下的选项只有「学习下一课节 / 确定」这种会导致跳转的，
        乱点一下就把正在刷的这一讲刷没了。
        """
        for d in self.dialogs():
            content = (d.get("content") or "").strip()
            target = None
            for b in d.get("buttons") or []:
                if any(w in (b.get("text") or "") for w in self.KEEP_WORDS):
                    target = b
                    break
            if target is None:
                names = "、".join((b.get("text") or "?") for b in d.get("buttons") or [])
                self.log(f"[desk] 弹了个框「{content[:40]}」但没有「取消」"
                         f"（只有 {names or '没按钮'}）—— 这次不点，免得跳走")
                return ""
            self.click_at(target["x"], target["y"], settle=settle)
            self.log(f"[desk] 弹框「{content[:44]}」→ 点了「{target['text']}」留在本讲")
            return f"{content[:44]} -> {target['text']}"
        return ""

    def rewind_and_play(self) -> str:
        """把当前视频倒回 0 秒再播起来。

        返回 `ok`（**确认已经在播**）/ `no-video` / `seek-fail` /
        `still-paused[:原因]`（倒带成功但没播起来 —— 调用方该走
        `start_video()` 的点按兜底，多半是 `NotAllowedError`，
        见 `REWIND_JS` 上面那段）。

        **返回值是可信的**：等 900 毫秒看过 `paused` 才下结论。以前恒返回
        `ok`，等于告诉调用方"播起来了"，兜底分支就永远不执行。
        """
        return str(self.js(REWIND_JS) or "")

    def study_time(self) -> dict:
        """读视频上方那两行「本次学习 / 总计时长」，单位秒。

        * `total` —— **这门课**的总计时长（`learnRecordObj.totalTime`），
          刷时长的判据就是它；
        * `learn` —— 这一趟攒着还没上报的部分（每 300 秒才上报一次，
          上报成功就清零，见 `CourseLearnTimeService.js`）。

        ★ `total` 是**按课**记的，不是账号级：实测同一账号在两门课上读到
        4391 秒（基层医疗，73分11秒）和 2220 秒（三维超声心动图，37分00秒），
        而两处的 DOM 文字都和它自己的 scope 值对得上。所以「刷到 90 分钟」
        是**每门课各自 90 分钟**，不能跨课累加。

        读不到的值是 `-1`，**不是 0** —— 调用方必须能分清"还没开始学（0 秒）"
        和"页面没读出来（-1）"，否则页面一变样就会把 `-1` 当成"够了/不够了"
        乱判。scope 优先、DOM 文字兜底，理由见 `STUDY_TIME_JS`。
        """
        raw = self.js(STUDY_TIME_JS)
        try:
            d = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}

        def num(v, text: str) -> int:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
            return parse_time_text(text)

        return {
            "learn": num(d.get("learn"), d.get("learn_text") or ""),
            "total": num(d.get("total"), d.get("total_text") or ""),
            "learn_text": d.get("learn_text") or "",
            "total_text": d.get("total_text") or "",
            "complete_status": str(d.get("complete_status") or ""),
            "show_hint": bool(d.get("show_hint")),
            "err": d.get("err") or "",
        }

    def course_ok(self) -> bool:
        """当前这个课程页**服务端认不认**。

        为什么要有这个检查：课程站的地址光靠 `courseId` **进不去**。
        直接 `Page.navigate` 到
        `studentIndex.action#!/index/course/home?courseId=…` 会得到一个
        空壳页面，正文只有「没有当前选课或选课无效！重新加载」，
        接口一律回 `E0002 系统处理出错啦!`。

        原因不是页面没渲染，也不是 AngularJS 没接住路由（我一开始就往
        这两个方向查了很久），而是**会话里没有"当前选课"**。这个状态只能
        由服务端自己建立 —— 走 `enter_course()` 的那一跳
        （`userEnterClass`）之后，同一个标签页里怎么换路由都正常。

        另外 `courseId` 本身也是**必填且必须对**的：不带它页面只说
        「温馨提示 请求参数缺失」，乱编一个则回「没有当前选课或选课无效！」。
        """
        if not self._loaded():
            return False
        t = (self.where().get("text") or "")
        return "选课无效" not in t and "没有当前选课" not in t

    def _on_courseware(self) -> bool:
        """当前是不是停在课件页（看路由，不看有没有 `<video>`）。"""
        return "courseware" in self._url()

    def play(self, *, rate: float = 1.0, reset: bool = False) -> dict | None:
        """让当前视频播起来。

        三件事都要做，少一件就可能不动：
          1. `playbackRate` —— **桌面版拿不到倍速，默认就是 1×**（实测
             1.25/1.5/1.75/2/2.5/4 全被平台立刻按回 1，`ratechange`
             是 `2 -> 1 -> 1`，6 秒真实时间视频只走 6.0 秒）。留着这个
             参数只为"万一哪天平台放开了"，正常别传；
          2. **只有确实在末尾才归零** —— 服务端按**真实播放时长**记账
             （拖进度条没用，实测上报的还是 `862/884` 那段），所以
             半看的讲次从原地接着播最省时间。实测第 9 讲已经播到 31.8%，
             若不管三七二十一归零，那 14 分钟就白播了；
          3. `play()` —— 必须显式调：视频停在末尾时 `ended=true` 不会
             自己动，`autoplay` 属性在切讲之后也不可靠。

        `reset=True` 则不管在哪儿都从头播（只有在明确要重播时才传）。

        ⚠ 只看返回值会骗人：`play()` 在自动播放策略下会被**拒绝**，
        那时返回的 `paused` 还是 true 而 Promise 是 rejected。要判
        "到底播起来没有"请用 `start_video()`。
        """
        v = self.video() or {}
        dur = float(v.get("dur") or 0.0)
        cur = float(v.get("cur") or 0.0)
        at_end = bool(reset) or bool(v.get("ended")) or (dur and dur - cur <= 10.0)
        js = PLAY_JS.replace("__R__", repr(float(rate))) \
                    .replace("__RESET__", "true" if at_end else "false")
        out = self.js(js)
        if at_end:
            self.log("[desk] 这一讲停在末尾了，从头播")
        elif cur > 30.0:
            self.log(f"[desk] 从 {cur / 60:.1f} 分钟接着播（不归零，"
                     f"服务端按真实播放时长记账）")
        return json.loads(out) if isinstance(out, str) and out.startswith("{") else None

    def play_result(self) -> str:
        """上一次 `play()` 的 Promise 结果（`RESOLVED` / `NotAllowedError: …`）。"""
        out = self.js(PLAY_RESULT_JS)
        return out if isinstance(out, str) else ""

    def start_video(self, *, tries: int = 3) -> bool:
        """把这一讲的视频**真正**播起来。播起来了返回 True。

        为什么起播要单独一个方法、还非得点一下视频不可（2026-10-08
        第 10 讲实测，`debug/_dclick4.py`）：

          * 整页载入（`enter_course()` 走的 `Page.navigate`）会清掉文档的
            "用户激活"状态 → `play()` 的 Promise 直接
            `NotAllowedError: play() failed because the user didn't
            interact with the document first.`；
          * 被拒绝**不派发 `pause`**，所以 `__dshPauseLog` 是空的，
            光看事件会以为是"平台把播放按停了"；
          * 给 `<video>` 正中心发一次**真鼠标三连**，`cur` 立刻按秒涨
            （63 → 64.9 → …），之后再 `play()` 就是 `RESOLVED`。

        所以顺序是死板的：**先点一下视频，再 `play()`，然后看证据**。
        只调 `play()` 不看 Promise 的话，会得到"命令发出去了、视频没动"
        这种最难查的状态。

        判据用两条（取或，宁松不严）：`paused` 不为真，**或者** Promise
        已经 `RESOLVED`。一开始只认 Promise，结果整轮都在报"起播失败"
        却其实放着视频 —— `play()` 是异步落定的，紧接着去读只会读到
        `pending`。
        """
        last: dict = {}
        for k in range(1, max(1, tries) + 1):
            if k == 1:
                last = self.play_checked()
            else:
                # 第二、三轮才点视频：第一轮先给它一次机会（用户刚刚
                # 点过按钮进课程页的时候，用户激活还没过期，`play()`
                # 本来就能成，没必要多送一次点击）。
                self.click_video()
                time.sleep(0.4)
                last = self.play_checked()
            # 给它一点时间落定（缓冲中的视频既不 resolve 也不 reject）。
            time.sleep(1.5)
            res = self.play_result()
            state = last if last.get("paused") is not None else (self.video() or {})
            playing = bool(state) and not state.get("paused") and not state.get("ended")
            ok = res == "RESOLVED" or playing
            self.log(f"[desk] 起播第 {k}/{tries} 次："
                     f"{'✓ 播起来了' if ok else '还是不动'}"
                     f"（paused={state.get('paused')} "
                     f"cur={state.get('cur')}/{state.get('dur')} "
                     f"ready={state.get('ready')} err={state.get('err')} "
                     f"play(): {res or '无回执'}）")
            if ok:
                return True
            time.sleep(1.0)
        return False

    def play_checked(self) -> dict:
        """调一次 `play()` 并**当场**把 Promise 挂上钩子、把状态读回来。

        返回 `{"res", "paused", "ended", "cur", "dur", "ready", "err"}`；
        页面上没有 `<video>` 时返回 `{}`（外层会当成"起播失败"）。
        """
        out = self.js(PLAY_CHECK_JS)
        try:
            d = json.loads(out) if isinstance(out, str) and out.startswith("{") else {}
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    def arm_snitch(self) -> str:
        """在 `<video>` 上装"谁按了暂停"的监听（`SNITCH_JS`）。"""
        out = self.js(SNITCH_JS)
        return out if isinstance(out, str) else ""

    def pause_log(self, *, limit: int = 6) -> list:
        """取回暂停/播放记录（最近 `limit` 条）。

        每条形如 `{"t": 毫秒, "cur": 秒, "stack": "Error: paused\\n at …"}`；
        `play: True` 的是播放事件。栈里的函数名就是"谁停的"。
        """
        raw = self.js(PAUSE_LOG_JS)
        try:
            rows = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            rows = []
        rows = rows if isinstance(rows, list) else []
        return rows[-max(1, limit):]

    def click_video(self) -> bool:
        """用真鼠标点一下 `<video>` 正中间（平台自己的播放开关）。

        为什么不能只靠 `v.play()`：桌面版拿不到倍速，而且整页载入之后
        `play()` 的 Promise 会被 Chrome 自动播放策略拒掉
        （`NotAllowedError: play() failed because the user didn't
        interact with the document first.`，见 `PLAY_CHECK_JS`），
        实测表现是 `cur` 死死停在中途（第 10 讲是 63 秒）、`paused` 一直
        true、一个浮层都没有。一条真鼠标事件就能解锁 —— 点完 `cur`
        立刻按秒涨（63 → 64.9 → …），再 `play()` 就 `RESOLVED` 了。
        `element.click()` 对这套页面无效（见模块顶部注释），所以走
        `click_at()` 的 `Input.dispatchMouseEvent` 三连。
        """
        raw = self.js(CLICK_VIDEO_JS)
        if not isinstance(raw, str) or not raw.startswith("{"):
            return False
        try:
            box = json.loads(raw)
        except ValueError:
            return False
        self.click_at(float(box.get("x") or 0.0), float(box.get("y") or 0.0))
        return True

    def boost(self, *, rate: float = 0.0, tail: float = 0.0) -> dict:
        """页面内的"别停"助推（可选把播放头钉在末尾附近）。见 `BOOST_JS`。

        ⚠ `rate` **默认 0 = 不碰倍速**：桌面版拿不到倍速，平台把 1.25 /
        1.5 / 1.75 / 2 / 2.5 / 4 全部立刻按回 1×（`ratechange` 是
        `2 -> 1 -> 1`，6 秒真实时间视频只走 6.0 秒）。传 `rate=2` 只会
        换来一串无用的 `ratechange`，所以默认关掉；留着这个参数是为了
        "万一哪天平台放开了"。真正有用的是 `paused` 时把视频捞起来 ——
        页内 500ms 一轮，比 Python 15 秒一轮快得多。

        `tail > 0` 时顺便把播放头钉在 `dur - tail`。
        """
        js = BOOST_JS.replace("__R__", repr(float(rate))) \
                     .replace("__TAIL__", repr(float(tail)))
        out = self.js(js)
        try:
            return json.loads(out) if out else {}
        except (TypeError, ValueError):
            return {}

    def stop_boost(self) -> None:
        """撤掉倍速助推。"""
        self.js(STOP_BOOST_JS)

    def watch_video(self, item: dict, *, timeout: float = LESSON_MAX_SECONDS,
                    poll: float = POLL_SECONDS, log=None) -> str:
        """把一讲看到平台认可「学完」。返回结局字符串，**不抛异常**。

        判据是**服务端自己的回执**，不是视频进度条：

          * 首选 `last_record()` —— 页面每 60 秒上报一次，响应里的
            `learnRecord.status == 2` 就是服务端记账了；
          * 兜底 `items()` 里这一讲的 `status`（每 4 轮查一次，省流量）。

        为什么不能只看 `currentTime >= duration`：拖到末尾再播，
        上报的 `startTime/endTime` 还是真实播放的区间（实测
        `862/884`），服务端没被骗过 —— 它按真实播放时长记账，
        所以进度条到 100% 不等于学完。

        **每轮都要核对 `active_lesson()` 是不是这一讲** —— 这是
        2026-10-08 那次白跑的教训：地址栏 hash、`<video>` 的 src 都换了，
        AngularJS 的 `activeItemObj` 却可能还停在前一讲，于是平台整段
        时间都在替**前一讲**记账，从外面看就是"服务端不记账"。对不上
        就直接返回 `"wrong-lesson"`，让上层用 `goto_lesson()` 重切，
        别在这儿傻等。

        结局：`"done"` / `"stalled"` / `"timeout"` / `"no-video"` /
        `"gone"` / `"wrong-lesson"` / `"stopped"`。

        `"stopped"` 是用户在界面上点了「立即停止」（见 `should_stop()`）。
        它**只从这里和 `watch_course` 的讲次循环里退出来**，已经上交给
        服务端的进度一分不丢 —— 平台按真实播放时长记账，停在哪算到哪，
        下次接着看就行。
        """
        log = log or self.log
        name = item.get("title", "")[:34]
        label = f"第{item.get('n')}讲 "
        rnd = 0
        status = 0
        start = time.monotonic()
        last_cur = -1.0
        stall = 0
        kicks = 0
        starts = 0
        # 服务端账目（`queryVideoItemDetail`）：每 4 轮（约 1 分钟）查一次。
        # 为什么非要单独查：页面自己上报的响应里常常没有 `studyTime`，
        # 进度行就一直显示「服务端已学 0.0 分钟」，看着像没记账。
        srv: dict = {}
        log(f"[desk] {label}开看：{name}")

        while True:
            # 用户点了「立即停止」就当场走人。放在**轮询最前面**：
            # 一轮里最坏要等 15 秒（POLL_SECONDS）才回到这儿，而这一轮
            # 里没有任何不可中断的等待 —— 视频在浏览器那边自己播，
            # 我们撒手不管，服务端照记它已经播过的时长。
            if should_stop():
                log(f"[desk] {label}收到停止，退出看护（已看护 "
                    f"{(time.monotonic() - start) / 60:.1f} 分钟）"
                    "—— 服务端按真实播放时长记账，已记的账一分不丢")
                return "stopped"
            if time.monotonic() - start > timeout:
                log(f"[desk] {label}看护超过 {timeout / 3600:.1f} 小时，先放它走")
                return "timeout"
            rnd += 1

            v = self.video()
            if not v:
                log(f"[desk] {label}页面上没有视频元素了")
                return "no-video"
            dur = float(v.get("dur") or 0.0)
            cur = float(v.get("cur") or 0.0)
            pct = (cur / dur * 100.0) if dur else 0.0

            # 0) 先确认平台认的就是这一讲。对不上就别等了 —— 等下去
            #    服务端涨的是**别的讲**的账（2026-10-08 白跑 45 分钟）。
            act = self.active_lesson()
            if act.get("id") and act.get("id") != item.get("item_id"):
                log(f"[desk] {label}✗ 播放器认的是另一讲"
                    f"（activeItemObj.id={act.get('id')[:12]}…，"
                    f"我要的是 {item.get('item_id', '')[:12]}…），重切")
                return "wrong-lesson"

            # 0.5) 弹题**每轮都要看**，不能等到"卡住 4 轮（≈60 秒）"才看。
            #      平台的弹题会把视频按停，但它自己每隔几秒又试着恢复一下，
            #      于是 15 秒一轮的轮询每次都可能"刚好"看到它在播 ——
            #      第 9 讲就是这样：`cur` 在 3130 秒上下反复，日志一直在说
            #      "起播成功"，其实一直在被弹题按停。早看一眼就早答一道。
            if self.quiz():
                if self.answer_popup():
                    log(f"[desk] {label}答了一道弹题")
                    stall = 0
                    time.sleep(2.0)
                    continue

            # 1) 先看服务端认没认（页面自己的上报，零额外请求）
            lr = self.last_record(item.get("item_id", "")) or {}
            if str(lr.get("status")) == "2" or str(lr.get("state")) == "2":
                log(f"[desk] {label}✓ 服务端已记账（已学 "
                    f"{float(lr.get('studyTime') or 0) / 60:.1f} 分钟）")
                return "done"
            # 2) 每 4 轮兜一次权威状态（万一页面这次没上报）
            if rnd % 4 == 1:
                srv = self.server_time(item.get("item_id", ""))
                if srv.get("complete") == "2":
                    log(f"[desk] {label}✓ 服务端账目已是学完"
                        f"（{srv['study_time'] / 60:.1f} 分钟）")
                    return "done"
                for it in self.items():
                    if it["item_id"] == item.get("item_id"):
                        status = it["status"]
                        break
                if status >= 2:
                    log(f"[desk] {label}✓ 平台状态已是学完")
                    return "done"

            # 3) 倍速这一段别指望了：桌面版**拿不到倍速**，平台把所有值都
            #    按回 1×（2026-10-08 实测 1.25/1.5/1.75/2/2.5/4 全被按回，
            #    `ratechange 2 -> 1 -> 1`，6 秒真实时间视频只走 6.0 秒）。
            #    所以这里只把"停了"捞回来。
            #
            #    ⚠ 光调 `play()` 不算捞 —— 它可能被自动播放策略**拒绝**
            #    （被拒绝不派发 pause，`__dshPauseLog` 里什么都看不到），
            #    2026-10-08 第 10 讲就是这么白等的：`cur` 恒为 63 秒，
            #    日志每 15 秒刷一行"重新播"，其实一次都没播起来。
            #    必须走 `start_video()`，它会点一下视频并核对 `play()`
            #    的 Promise。
            rate = float(v.get("rate") or 1.0)
            if v.get("paused") and not v.get("ended"):
                log(f"[desk] {label}画面停着，重新起播")
                self.boost()
                if not self.start_video(tries=3):
                    starts += 1
                    log(f"[desk] {label}起不来了（第 {starts} 次）")
                    if starts >= 2:
                        log(f"[desk] {label}连点都起不来（{pct:.1f}%），先放它走")
                        return "stalled"
                    time.sleep(2.0)

            if abs(cur - last_cur) < 1.0:
                stall += 1
                if stall >= STALL_ROUNDS:
                    # 卡住先怀疑弹题：平台的"学习状态检测题"会把视频顶停，
                    # 不答就一直停在那儿（第 9 讲就是停在 44:45 没人答）。
                    if self.answer_popup():
                        log(f"[desk] {label}答了弹题，继续")
                        stall = 0
                    else:
                        # 都不是，就把"谁按了暂停"的栈打出来 —— 屏幕上
                        # 一个浮层都没有、`paused` 却是 true，光看 DOM 查不
                        # 出原因，栈里带的函数名才是答案。
                        for row in self.pause_log(limit=3):
                            if row.get("play"):
                                continue
                            log(f"[desk] {label}暂停栈（{row.get('cur', 0):.1f}s）: "
                                f"{str(row.get('stack', ''))[:200]}")
                        # 再试一次"点视频 + play + 看 Promise"。这一路
                        # 走到这儿说明它刚才还判过"在播"，那就不是自动播放
                        # 策略的问题，而是平台自己把播放按停了 —— 点一下
                        # 正好是它的"继续"开关。
                        log(f"[desk] {label}播放头 {cur / 60:.1f} 分钟都不动，"
                            f"再点一次视频（play(): {self.play_result() or '无回执'}）")
                        if self.click_video():
                            time.sleep(2.0)
                            self.play()
                            kicks += 1
                        if kicks >= 3:
                            log(f"[desk] {label}连点 {kicks} 次都起不来"
                                f"（{pct:.1f}%），先放它走")
                            return "stalled"
                        stall = 0
            else:
                stall = 0
                kicks = 0
                starts = 0
                log(f"[desk] {label}{pct:5.1f}%  {cur / 60:5.1f}/{dur / 60:.1f} 分钟"
                    f"  {rate:g}×  服务端已学 "
                    f"{srv.get('study_time', 0.0) / 60:.1f} 分钟")

            last_cur = cur
            time.sleep(poll)

    # -- 弹题 -----------------------------------------------------------

    def quiz(self) -> dict:
        """当前弹出来的视频弹题（没有就返回 `{}`）。见 `QUIZ_JS`。"""
        raw = self.js(QUIZ_JS)
        try:
            d = json.loads(raw) if raw and raw.startswith("{") else {}
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) and d.get("open") else {}

    def answer_popup(self) -> bool:
        """处理看课过程中弹出的学习状态检测题。答到了返回 True。

        **弹题不只有选择题** —— 2026-10-08 第 9 讲 00:52:09 弹的是一道
        **打分题**：「请您为老师此堂讲课总体效果打分，满分100分」，输入框
        的 `placeholder` 写着「请输入 50-100 的数值」。老版本只找页面里
        有没有「A」，这题就一直没答，平台于是每几秒把视频按停一次
        （暂停取证里 PAUSE/PLAY 成对刷屏，`cur` 停在 3130 秒不动），
        表现和"平台把播放按停了"一模一样，查了很久才落到这里。

        所以现在的顺序是：

          1. 先看 `.popup_layer` 在不在（`quiz()`）；
          2. 有输入框就填分 —— 打分题按 `placeholder` 里的上限给满分
             （实测填 100 之后 `ng-valid`，提交被接受）；选择题没有输入框，
             退回去点选项；
          3. 点「提交」（认 `button` 上的字，别认死坐标）。

        提交之后弹层**不一定消失**（实测标题从「视频弹题」变成
        「视频弹题1」，说明接着弹下一题），所以这里每次只答一道，
        由 `watch_video` 的下一轮再来一遍 —— 别指望一次答干净。
        """
        q = self.quiz()
        if not q:
            # 没有弹层时不要再去找「A」：页面上别处也有「A」（比如
            # 播放器的时间轴、正文里的英文），那样会误点到不相干的东西。
            return False
        self.log(f"[desk] 弹题「{q.get('title', '')}」{q.get('counts', '')}"
                 f"：{q.get('stem', '')[:40]}")

        # 2a) 打分题 / 填空题：有输入框就写值
        box = q.get("inputBox") or {}
        if box and str(q.get("inputType")) in ("number", "text", ""):
            score = "100" if "分" in (q.get("stem") or "") else QUIZ_ANSWER
            out = self.js(QUIZ_FILL_JS.replace("__V__", repr(score)))
            self.log(f"[desk] 填了「{score}」（{out}）")
            self._click_at(box["x"], box["y"])
            time.sleep(0.4)
        else:
            # 2b) 选择题：先把选项念出来（日志里留证），再挑一个"同意"的
            opts = q.get("options") or []
            if opts:
                self.log("[desk] 选项：" + " | ".join(o["text"][:18] for o in opts))
            hit = self.pick_option(opts)
            if hit:
                self._click_at(hit["x"], hit["y"])
                time.sleep(0.4)
            elif not box:
                # 两样都没识别出来，别硬点，交回上层（它会打印取证）
                self.log("[desk] 认不出这题怎么答，先不碰它")
                return False

        # 3) 提交
        #    按钮字不固定：打分题是「提交」，下一题可能是「继续」/「确定」。
        #    优先关键词命中（弹层里可能还有「查看解析」之类的次要按钮），
        #    都不中就退到最后一个 —— 实测操作按钮永远排在 `.popup_layer` 末尾。
        target, label = q.get("btnBox") or {}, q.get("btnText") or ""
        if not target:
            buttons = q.get("buttons") or []
            keys = ("提交", "确定", "继续", "下一题", "完成")
            hit = next((b for b in buttons
                        if any(k in (b.get("text") or "") for k in keys)), None)
            pick = hit or (buttons[-1] if buttons else None)
            if pick:
                target, label = pick, pick.get("text", "")
        if not target:
            self.log("[desk] 弹层里没找到按钮，只能先放着")
            return False
        self._click_at(target["x"], target["y"])
        self.log(f"[desk] 点「{label or '提交'}」")
        time.sleep(1.5)
        # 4) 点了之后弹层还在不在 —— 只在答过题的情况下判"没生效"，
        #    免得把"答完接着弹下一题"的正常情况误报成失败。
        still = self.quiz()
        if still and (q.get("counts") or "") == (still.get("counts") or "") \
                and (q.get("title") or "") == (still.get("title") or ""):
            self.log(f"[desk] 点了「{label}」但弹层没动（还是「{still.get('title', '')}」）")
            return False
        return True

    def pick_option(self, opts: list) -> dict | None:
        """从选项里挑一个"同意/继续"的，挑不出返回 None。

        为什么要这么挑（2026-10-08 实测，见 `debug/_dpopup4.py` 扒下来的
        弹层骨架）：

          * 看课时的学习状态检测题是**固定话术**：
            `1、是否继续当前视频学习(此题为学习状态检测，如需继续学习，
            请选择A)（单选题）`，选项只有 `A . 是` / `B . 否`。
            **选「否」就是把学习停掉**，所以不能瞎挑；
          * 但也不能只会认「A」：这题的选项文字是 `A . 是`，前缀匹配
            `A` 能中对；可是同一个平台还有打分题（输入框）和别的单选，
            选项文字不一定带字母。

        所以顺序是：**字母前缀 A → 肯定词（是/对/正确/继续/同意/可以）
        → 认不出就返回 None**（宁可不答，也不要去点「否」）。
        """
        for o in opts:
            if (o.get("text") or "").strip().upper().startswith(QUIZ_ANSWER):
                return o
        yes = ("是", "对", "正确", "继续", "同意", "可以", "确定")
        # 否定词要连「不」一起挡：`不对` 里既有「对」又有「不」，
        # 只看「否」会把它当成肯定项点下去。
        no = ("否", "不", "没", "错", "停止", "结束")
        for o in opts:
            text = (o.get("text") or "")
            if any(w in text for w in no):
                continue
            if any(w in text for w in yes):
                return o
        return None

    # -- 考核（结课三件事里的一件）----------------------------------------
    #
    # 平台的结课要求（`classAssessmentDesc`）= 视频课件全学完 + **考核 ≥60 分**
    # + 完成问卷调查。考核**不在 `testing`/`ks` 那两族里**，走的是 `homework`
    # 族 —— 这是 2026-10-08 踩了很久的坑：`queryTestingList` / `queryTestList`
    # 对肝胆这门课全回空，看着像"这门课没考核"，其实是问错了接口。
    #
    # 路由：课程页顶部「考核」tab → `#!/index/course/learn/homework/list?courseId=…`
    # 模板：`tpl/student/course/learn/homework/{list,do,show}.html`（已存 `debug/examjs/`）

    def homework_list(self) -> list:
        """这门课的考核清单（`queryHomeworkList`）。

        每一项的关键字段（实测原文，肝胆这门课）：
        `itemId` / `id` / `title` / `homeworkCategory`（0 平时、1 结课、2 补考）/
        `homeworkStatus`（0 未做、1 待批改、2 被驳回、3 已批改）/ `score` /
        `allowRedoNum`（允许重做次数）/ `allowRedo` / `answerShowType` /
        `homeworkType`（0 纯题库、1/2/3 带文件）。

        **找不到就返回空表**，不抛异常 —— 上层要能"这门课没考核"地过掉。
        """
        cid = self.course_id()
        if not cid:
            return []
        try:
            res = self.api("queryHomeworkList", courseId=cid)
        except Exception:  # noqa: BLE001
            return []
        data = (res or {}).get("homeworkDataList") or []
        return [d for d in data if isinstance(d, dict)]

    def homework_open(self, homework_id: str, *, route: str = "do",
                      settle: float = 8.0) -> bool:
        """把考核页**用页面自己的路由**打开，返回是否到了。

        为什么必须走页面路由而不是直接打 `doHomework` 接口：控制器在
        进路由时会做几件我们看不见的事（把 `homeworkObj` 塞进
        `courseLearnHomeworkDoConfig`、算 `doExpireConfig` 倒计时、
        从接口回执里拿 `questionObj.showTimestamp`），而且**提交时
        必须原样带回 `showTimestamp`** —— 自己拼一个大概率被服务端判无效。
        所以：让页面自己加载，我们只读它加载出来的东西、只填它的输入框。
        """
        cid = self.course_id()
        if not cid or not homework_id:
            return False
        target = (f"{COURSE}/learning/student/studentIndex.action"
                  f"#!/index/course/learn/homework/{route}"
                  f"?homeworkId={homework_id}&courseId={cid}")
        self.goto(target, settle=settle, tries=2, force=True)
        # **路由到位 ≠ 控制器加载完。** 页面到位之后控制器还要去打
        # `doHomework` / `showHomework` 拿题，实测刚到位就读
        # `courseLearnHomeworkDoConfig` 常常还是空的（`loaded=false`，
        # `questionObj` 影子都没有）—— 2026-10-08 就是这么漏掉了整页
        # `sanswer` 的：`homework_open(route="show")` 明明返回 True，
        # 紧接着读却一句「平台没给正确答案」，白丢一次交卷机会。
        # 所以等它**自己说 `loaded`**，只认这个信号。
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self.homework_form().get("loaded"):
                return True
            time.sleep(1.5)
        return self._loaded()

    def homework_form(self) -> dict:
        """读考核页上的题（从配置对象里读，不认 DOM 文案）。

        返回 `{"loaded": bool, "id": 卷号, "title": …, "type": 0/1/2/3,
        "status": …, "timestamps": …, "questions": [ {kind, id, title,
        model, options:[{index,text}], text: 主观题已填的字} ]}`。

        题目分四类（模板原文）：`danxuanList` 单选 / `duoxuanList` 多选 /
        `panduanList` 判断 / `wendaList` 主观（问答）。
        **答题模型的位置**：单选和判断是 `topicObj.userAnswerModel`（标量），
        多选是 `topicObj.userAnswerModel[topicItemObj.index]`（字典），
        主观是 `topicObj.userAnswerModel`（文字）。
        """
        raw = self.js(HOMEWORK_JS)
        try:
            d = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    def homework_answer(self, plan: dict, *, log: Callable | None = None) -> dict:
        """按 `plan` 把答案填进页面（`{题目 id: 答案}`），返回填了几道。

        填法**必须是想办法让 AngularJS 自己把 `ng-model` 更新掉**：
        直接改 DOM（`.checked = true`）或直接写 `scope.userAnswerModel`
        都可能与 AngularJS 自己的 `$watch`/`$digest` 打架。所以这里
        走"改值 + 派发事件"：`input`/`change` + 对单选/多选再多派发一次
        `click`（模板上绑的是 `ng-change="checkAnswerModel()"`，
        而 `ng-change` 只有在 **AngularJS 自己处理的 change 事件**里才跑）。

        答案的取值（从 `p()` 的定义反推，源码原文见 7.5.2）：
        单选/判断 = 选项的 `value`（模板里是 `topicItemObj.index`），
        多选 = `"索引|索引"` 升序拼起来。
        """
        log = log or self.log
        if not plan:
            return {"filled": 0, "missing": []}
        raw = self.js(HOMEWORK_FILL_JS.replace("__PLAN__", json.dumps(plan, ensure_ascii=False)))
        try:
            out = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
        except (TypeError, ValueError):
            out = {}
        log(f"[desk] 填了 {out.get('filled', 0)} 道题"
            + (f"，没填上 {len(out.get('missing') or [])} 道" if out.get("missing") else ""))
        return out if isinstance(out, dict) else {"filled": 0, "missing": []}

    def homework_submit(self) -> dict:
        """把答案交上去。返回接口回执（失败返回 `{}`）。

        两步：**先点「提交」→ 再点确认框的「确定」**。确认框是 `layui` 的
        （`DIV.layui-layer.layui-layer-dialog`：「温馨提示 / 确定提交本次考核吗？ /
        确定 取消」，确定按钮是 `.layui-layer-btn0`）。

        ⚠ **这两下必须用 `js_click()`，不能用真鼠标三连**（2026-10-08 实测）：
        真鼠标在「提交」上点过好几次，按钮上的 `click` 监听器**一次都没触发**
        （装探针验证：`__probe` 里连 `click:` 都没有），也就没有任何
        `submitHomework` 上报，而页面看起来一切正常 —— 最容易误判成
        "服务端不认"。原因是这个页面被 `.homework_body_layer` 装在一个
        **会滚动的容器**里（`getBoundingClientRect` 的 y 能到 -2457），
        坐标换算不可靠。`btn.click()`（平台自己的原语）一击即中，
        确认框立刻出现。

        **提交后 `showTimestamp` 会过期** —— 同一份卷子不能拿旧的
        `showTimestamp` 再交一次（要交就得重新进一次考核页）。
        """
        before = self.reports()
        if not self.js_click("button", "whaty-button", "提交"):
            self.log("[desk] 页面上找不到「提交」按钮")
            return {}
        time.sleep(1.5)
        if not self.js_click("", "layui-layer-btn0", "确定", exact=True):
            self.log("[desk] 没等到确认框（也许它本来就提交了）")
        time.sleep(3.0)
        for r in reversed(self.reports()):
            if r["fn"] in ("submitHomework", "submitFileHomework") and r not in before:
                return r["res"] if isinstance(r["res"], dict) else {}
        return {}

    def js_click(self, tag: str = "", cls: str = "", text: str = "",
                 *, exact: bool = False, settle: float = 0.0) -> bool:
        """用**页面自己的 `element.click()`** 点一个元素（不是真鼠标）。

        什么时候该用它、什么时候不该（实测分界）：

        * **页面内的控件**（考核的「提交」、`layui` 的「确定」、弹层按钮）
          → 用它。`ng-click` 是普通 DOM 事件，`.click()` 就够；
          而这些页面常把内容装进**会滚动的容器**里，坐标换算会错
          （实测 `getBoundingClientRect().y` 能到 -2457）。
        * **顶部导航**（首页 / 在线学习 / 我的学习）→ 还得用 `click_at()`
          的真鼠标三连。那几个是 `$state.go` + `history.pushState` 那套，
          实测 `.click()` 无效。

        找不到元素返回 `False`（**不猜坐标**）。
        """
        js = JS_CLICK_JS.replace("__TAG__", json.dumps(tag)) \
                        .replace("__CLS__", json.dumps(cls)) \
                        .replace("__TEXT__", json.dumps(text)) \
                        .replace("__EXACT__", "true" if exact else "false")
        out = self.js(js)
        hit = str(out or "").startswith("ok")
        if not hit:
            return False
        if settle:
            time.sleep(settle)
        return True

    def homework_answers_shown(self) -> dict:
        """读**批改后那一页**上平台公布的正确答案。

        2026-10-08 实测：`queryHomeworkAnswer` / `queryHomeworkDetail` /
        `queryHomeworkResult` 这三个 `functionCode` 在
        `studentDataAPI.action` 上**都不存在**（回 `E0002 方法…不存在！`）。
        正确答案只在**页面**上 —— 只有 `answerShowType == 2`（交卷后显示
        答案）时才给，位置是「查看」路由
        `#!/index/course/learn/homework/show?homeworkId=…` 的
        `courseLearnHomeworkShowConfig.questionObj`，每道题的 `sanswer`：

            {"title": "急性胆囊炎时呈阳性的是", "sanswer": "D",
             "options": [{"index": "A", "content": "Blumberg 征"}, …]}

        多选是 `"A|C|D|E"`（`|` 拼的就是选项 `index`）。
        **所以调用方必须先 `homework_open(route="show")` 再调这个。**

        这不是"破解"：把答案显示给考生看、再允许重做，是平台自己给的
        功能（原文写着「客观题考核可以重复提交」「允许重做次数：8 次」）。
        """
        return self.homework_form()

    def post_form(self, url: str, body: str) -> str:
        """同源 POST 一份表单，返回响应正文（连不上返回 `""`）。

        用它的前提是**当前页面就在目标域上** —— `fetch` 用的是相对路径，
        页面在哪台机器上，请求就发给哪台机器。问卷那一族接口只在
        `elearning` 域上有（实测：在课程域打 `/user/queryHomeworkList`
        是 JSON 404，在 `elearning` 域的「我的学习」上打
        `/user/queryQuestionnaireList` 才有数据）。
        """
        try:
            raw = self.js(POST_JS.replace("__URL__", json.dumps(url))
                                 .replace("__BODY__", json.dumps(body)))
        except Exception as exc:  # noqa: BLE001
            self.log(f"[desk] 表单请求发不出去: {type(exc).__name__}")
            return ""
        return raw if isinstance(raw, str) else ""

    def q_list(self, select_type: int = 1) -> list:
        """问卷清单（`queryQuestionnaireList`）。`select_type`：1 未参加 / 2 已参加。

        **关键映射**：每一条的 `classId` 就是平台「我的学习」里
        `courseList[].id`（2026-10-08 实测：重症那条的 `classId` 和它
        在课程列表里的 `id` 一个字符不差）。所以不用去猜"哪份问卷属于
        哪门课"，拿课程 id 直接对就行。

        必须在 `elearning` 域的页面上调（见 `post_form`）。失败返回空表。
        """
        raw = self.post_form("/user/queryQuestionnaireList", f"selectType={int(select_type)}")
        try:
            d = json.loads(raw or "{}")
        except ValueError:
            return []
        data = d.get("questionnaireList") or d.get("dataList") or []
        return [x for x in data if isinstance(x, dict)]

    def q_detail(self, tp_id: str, class_id: str) -> dict:
        """一份问卷的题目（`queryQuestionnaireDetail`）。

        回执形状（实测原文）：
        `{"questionnaire": {"id", "classId", "title"}, "topicList": [
        {"id", "title", "typeCode": "DAN_XUAN", "optionList": [
        {"id", "content", "least", "most"}]}]}`。
        """
        raw = self.post_form("/user/queryQuestionnaireDetail",
                             f"id={tp_id}&classId={class_id}")
        try:
            d = json.loads(raw or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def q_submit(self, bean: dict) -> dict:
        """交一份问卷（`saveQuestionnaireRecord`）。

        `bean` 的形状（2026-10-08 实测交成功过的原文，**别再改字段名**）::

            {"id": 问卷 id, "classId": 课程 id,
             "recordArr": [{"topicId": 题目 id, "optionIdArr": [选项 id]}]}

        用 `questionnaireRecordBean` 这个名字做表单字段，值要 URL 编码。
        成功回执：`{"errorCode":0,"message":"提交问卷成功","responseCode":"SUCCESS"}`。
        """
        body = ("questionnaireRecordBean="
                + urllib.parse.quote(json.dumps(bean, ensure_ascii=False)))
        raw = self.post_form("/user/saveQuestionnaireRecord", body)
        try:
            d = json.loads(raw or "{}")
        except ValueError:
            return {"raw": raw}
        return d if isinstance(d, dict) else {"raw": raw}

    def finish_course(self, course_id: str) -> dict:
        """申请结课（`updateCourseFinish`）。

        这是结课的**最后一步**，也是唯一一步不在"看视频/考核/问卷"三件事里的：
        平台页面上每张未结课的课程卡片上有个 `DIV.cert-apply`「申请结课」，
        点它会先弹一个 `$confirm('确定将该课置为结课吗?')`，确认后走
        `pc.js:734 updateCourseFinish()`：

            POST /user/updateCourseFinish   body: courseId=<courseList[].id>

        （源码原文存在 `debug/examjs/pc.js:722-777`，是 elearning 域上的接口，
        必须在「我的学习」页上打，理由同 `post_form`。）

        回执两种：
        - `{"success": true}` → 结课成功，页面上会 `$set(course, 'isFinishCourse', true)`；
        - `{"success": false, "message": "请先完成问卷调查"}` → 三件事还没齐
          （原来源码里对这条 message 专门弹了个「去完成」按钮跳到问卷页）。
        """
        try:
            raw = self.post_form("/user/updateCourseFinish",
                                 f"courseId={urllib.parse.quote(str(course_id))}")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}", "raw": ""}
        try:
            d = json.loads(raw or "{}")
        except ValueError:
            return {"ok": False, "msg": "回执不是 JSON", "raw": raw}
        if not isinstance(d, dict):
            return {"ok": False, "msg": "回执不是对象", "raw": raw}
        ok = bool(d.get("success"))
        return {"ok": ok, "msg": str(d.get("message") or ("结课申请成功" if ok else "")),
                "raw": raw}

    def _click_at(self, x: float, y: float) -> None:
        """按视口坐标点一下（`click_at` 的薄壳，省得每处都写 float）。"""
        try:
            self.click_at(float(x), float(y))
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _play_button_guess() -> tuple[int, int]:
        """播放器左下角那个播放键的大概位置（画布坐标）。

        只在「检测到暂停」时兜底用一次 —— 正常情况下视频自己在播，
        根本用不到它。实测桌面版播放器占屏幕上半部分，控制条在其底部。
        """
        return (60, 500)
