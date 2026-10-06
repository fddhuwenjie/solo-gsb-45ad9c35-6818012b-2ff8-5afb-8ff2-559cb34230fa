#!/usr/bin/env python3
"""冒烟测试：创建 → 非法提交 → 修订 → 章节换算 → 下载 → 并发 → 重启。

运行：python3 tests/smoke_test.py
"""
import importlib
import io
import json
import math
import os
import struct
import sys
import tempfile
import threading
import wave
import zipfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="keepsvc-test-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as svc  # noqa: E402

PASS = 0


def check(cond, label):
    global PASS
    assert cond, f"FAILED: {label}"
    PASS += 1
    print(f"  ok - {label}")


def make_wav(seconds=10, channels=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(n)))
    return buf.getvalue()


def main():
    client = svc.app.test_client()

    print("[1] 创建节目")
    meta = {
        "chapters": [
            {"title": "开场", "frame": 0},
            {"title": "删除段内", "frame": 50000},
            {"title": "保留段起点", "frame": 64000},
        ],
        "mandatory": [{"start": 16000, "end": 32000}],
    }
    r = client.post("/programs", data={
        "file": (io.BytesIO(make_wav(10)), "demo.wav"),
        "meta": json.dumps(meta),
    }, content_type="multipart/form-data")
    check(r.status_code == 201, "创建节目返回 201")
    prog = r.get_json()
    pid = prog["program_id"]
    check(prog["revision"] == 0 and prog["keeps"] == [{"start": 0, "end": 160000}],
          "初始保留全段（修订号 0）")
    check([c["frame"] for c in prog["chapters"]] == [0, 50000, 64000], "初始章节按原始帧登记")

    print("[2] 非法提交全部被拒绝且状态不变")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 3, "segments": [{"start": 0, "end": 160000}]})
    check(r.status_code == 409 and r.get_json()["current_revision"] == 0,
          "过期修订号返回 409 并给出当前修订号")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0, "segments": [{"start": 100, "end": 100}]})
    check(r.status_code == 400, "空段被拒绝")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "segments": [{"start": 0, "end": 50000}, {"start": 40000, "end": 60000}]})
    check(r.status_code == 400, "重叠/乱序段被拒绝")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0, "segments": [{"start": 0, "end": 200000}]})
    check(r.status_code == 400, "越界段被拒绝")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0, "segments": [{"start": 40000, "end": 160000}]})
    check(r.status_code == 400 and r.get_json()["error"] == "mandatory_not_covered",
          "必留口播未完整落入保留段被拒绝")
    r = client.get(f"/programs/{pid}")
    check(r.get_json()["revision"] == 0, "非法提交后旧修订保持原样")

    print("[3] 第一次修订：删除 [48000, 64000)")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "segments": [{"start": 0, "end": 48000}, {"start": 64000, "end": 160000}]})
    check(r.status_code == 200 and r.get_json()["revision"] == 1, "修订成功，修订号 0 -> 1")
    chapters = {c["title"]: c for c in r.get_json()["chapters"]}
    check("删除段内" not in chapters, "被删除区间的章节不再出现")
    check(chapters["开场"]["frame"] == 0, "保留段内章节坐标不变")
    check(chapters["保留段起点"]["frame"] == 48000,
          "位于保留段起点的章节保留并换算为成品帧 48000")
    check(r.get_json()["product_frames"] == 144000, "成品帧数正确")

    print("[4] 第二次修订：只保留必留口播段")
    r = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 1, "segments": [{"start": 16000, "end": 32000}]})
    check(r.status_code == 200 and r.get_json()["revision"] == 2, "修订成功，修订号 1 -> 2")
    check(r.get_json()["chapters"] == [], "章节全部落在删除区间")
    check(r.get_json()["product_frames"] == 16000, "反复编辑仍按原始帧计算，无偏移积累")

    print("[5] 下载成品 ZIP")
    r = client.get(f"/programs/{pid}/download")
    check(r.status_code == 200, "下载成功")
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    with wave.open(io.BytesIO(zf.read("audio.wav")), "rb") as w:
        check(w.getnframes() == 16000 and w.getframerate() == 16000
              and w.getnchannels() == 1 and w.getsampwidth() == 2,
              "ZIP 内 WAV 为真实裁接成品（16000 帧 / 16kHz / 单声道 / 16 位）")
    doc = json.loads(zf.read("chapters.json"))
    check(doc["revision"] == 2 and doc["product_frames"] == 16000,
          "ZIP 内章节表与 WAV 同属修订 2")

    print("[6] 并发提交同一修订号")
    results = []

    def worker():
        c = svc.app.test_client()
        resp = c.put(f"/programs/{pid}/keeps", json={
            "expected_revision": 2, "segments": [{"start": 0, "end": 160000}]})
        results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(sorted(results) == [200] + [409] * 7, "8 个并发提交中恰好一个成功，其余 409")

    print("[7] 重启后仍可编辑")
    importlib.reload(svc)  # 模拟进程重启：重新建连同一数据目录
    client2 = svc.app.test_client()
    r = client2.get(f"/programs/{pid}")
    check(r.status_code == 200 and r.get_json()["revision"] == 3, "重启后节目状态保留")
    r = client2.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 3,
        "segments": [{"start": 0, "end": 48000}, {"start": 64000, "end": 160000}]})
    check(r.status_code == 200 and r.get_json()["revision"] == 4, "重启后仍可提交修订")

    print("[8] 非法 WAV 被拒绝")
    r = client2.post("/programs", data={
        "file": (io.BytesIO(make_wav(10, channels=2)), "stereo.wav")},
        content_type="multipart/form-data")
    check(r.status_code == 400, "双声道 WAV 被拒绝")
    r = client2.post("/programs", data={
        "file": (io.BytesIO(make_wav(10, rate=8000)), "8k.wav")},
        content_type="multipart/form-data")
    check(r.status_code == 400, "8 kHz WAV 被拒绝")
    r = client2.post("/programs", data={
        "file": (io.BytesIO(make_wav(31)), "long.wav")},
        content_type="multipart/form-data")
    check(r.status_code == 400, "超过 30 秒的 WAV 被拒绝")

    print(f"\n全部 {PASS} 项检查通过")


if __name__ == "__main__":
    main()
