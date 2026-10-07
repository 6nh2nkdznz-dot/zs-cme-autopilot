"""校验层与恢复逻辑的自测。

不需要真机 —— 全部用假的状态回调与假的点击记录来验证行为。

运行:
    python scripts\\test_verify.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from verify import recover_to_home, retry_with_recovery, verify  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n         期望 {want!r}\n         实际 {got!r}")


def silence(_msg: str = "") -> None:
    """吞掉日志——测试输出别被淹。"""
    pass


def main() -> int:
    print("=" * 66)
    print(" 校验层与恢复逻辑自测")
    print("=" * 66)

    # ---------------- verify ----------------
    print("\n[1] verify：动作后置校验")

    # 立即符合
    check("立即符合 → True",
          verify("answer", read_state=lambda: "answer", log=silence), True)

    # 延迟符合：第 2 次才变（模拟渲染延迟）
    states = iter(["answer", "result"])
    check("第 2 次才变 → True（不误判成失败）",
          verify(("result",), read_state=lambda: next(states, "result"),
                 tries=3, interval=0.01, log=silence), True)

    # 始终不符
    check("始终不符 → False",
          verify("result", read_state=lambda: "answer",
                 tries=2, interval=0.01, log=silence), False)

    # 多个期望其一即可
    check("多个期望之一 → True",
          verify(("result", "dialog"), read_state=lambda: "dialog",
                 log=silence), True)

    # 读不到状态
    check("读不到状态 → False",
          verify("answer", read_state=lambda: "", tries=2,
                 interval=0.01, log=silence), False)

    # 日志必须报出差异，而不是只说失败
    captured: list[str] = []
    verify("result", read_state=lambda: "answer", tries=1,
           interval=0.01, log=captured.append, what="点提交")
    blob = "\n".join(captured)
    check("日志含「我做了什么」", "点提交" in blob, True)
    check("日志含期望", "result" in blob, True)
    check("日志含实际", "answer" in blob, True)

    # ---------------- recover_to_home ----------------
    print("\n[2] recover_to_home：识别出错回主页")

    # 一次 tab 就到家
    state = {"p": "answer"}
    check("按 tab 一次到主页",
          recover_to_home(
              read_state=lambda: state["p"],
              tap_back=lambda: state.update(p="wechat"),
              tap_learning_tab=lambda: state.update(p="learning_list"),
              home_page="learning_list", settle=0.01, log=silence), True)

    # tab 没用，靠 **一次** back 退回来。
    #
    # 注意这里故意让 back **第一次就成功** —— 实现只在 tab 无效后
    # 谨慎地退一次（用户实测：连按多次 back 会把微信 WebView 的宿主
    # 页面推光，落到空白页，看起来像「微信自己退出了」）。
    # 所以夹具不该要求「按好几次才成功」。
    state2 = {"p": "answer", "n": 0}

    def _back():
        state2["n"] += 1
        state2["p"] = "learning_list"

    check("tab 无效时靠 back 退回",
          recover_to_home(
              read_state=lambda: state2["p"],
              tap_back=_back,
              tap_learning_tab=lambda: None,
              home_page="learning_list", settle=0.01, log=silence), True)

    # 怎么都回不去
    check("回不去 → False（需要人工）",
          recover_to_home(
              read_state=lambda: "stuck",
              tap_back=lambda: None,
              tap_learning_tab=lambda: None,
              home_page="learning_list", settle=0.01,
              extra_backs=2, log=silence), False)

    # 已经在主页就不动
    calls = {"n": 0}
    check("已在主页 → 直接 True 且不点任何东西",
          recover_to_home(
              read_state=lambda: "learning_list",
              tap_back=lambda: calls.update(n=calls["n"] + 1),
              tap_learning_tab=lambda: calls.update(n=calls["n"] + 1),
              home_page="learning_list", settle=0.01, log=silence), True)
    check("确实没点", calls["n"], 0)

    # ---------------- retry_with_recovery ----------------
    print("\n[3] retry_with_recovery：失败就恢复再试")

    # 第 2 轮成功
    st = {"round": 0}

    def act():
        st["round"] += 1
        return True

    check("第 2 轮成功 → True",
          retry_with_recovery(
              action=act,
              verify_ok=lambda: st["round"] >= 2,
              recover=lambda: True,
              max_rounds=3, log=silence), True)
    check("确实试了 2 轮", st["round"], 2)

    # 一直不成功
    st2 = {"round": 0}
    check("始终不成功 → False",
          retry_with_recovery(
              action=lambda: st2.update(round=st2["round"] + 1),
              verify_ok=lambda: False,
              recover=lambda: True,
              max_rounds=3, log=silence), False)
    check("轮数被上限截住", st2["round"], 3)

    # 恢复失败就早点停，不空转
    st3 = {"round": 0}
    check("恢复失败 → 立刻 False",
          retry_with_recovery(
              action=lambda: st3.update(round=st3["round"] + 1),
              verify_ok=lambda: False,
              recover=lambda: False,
              max_rounds=5, log=silence), False)
    check("只试了 1 轮就停", st3["round"], 1)

    # 动作抛异常也要能兜住并继续恢复
    st4 = {"round": 0}

    def boom():
        st4["round"] += 1
        raise RuntimeError("模拟点击失败")

    check("动作抛异常 → 不崩，走恢复并重试",
          retry_with_recovery(
              action=boom,
              verify_ok=lambda: st4["round"] >= 2,
              recover=lambda: True,
              max_rounds=3, log=silence), True)

    print("\n" + "=" * 66)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 66)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
