#!/usr/bin/env python3
"""录音删段交付服务。

上传原始 WAV（<=30 秒、16 kHz、单声道、16 位 PCM）并登记章节与必留口播区间
（均为原始采样帧坐标）；此后按预期修订号提交完整保留清单，服务始终基于原始
音频实时裁接成品。状态持久化在 SQLite（含原始音频），重启后可继续编辑。
"""

import io
import json
import os
import sqlite3
import uuid
import wave
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_file

SAMPLE_RATE = 16000          # 固定采样率
MAX_SECONDS = 30             # 原始音频时长上限
MAX_FRAMES = SAMPLE_RATE * MAX_SECONDS
MAX_CHAPTERS = 500           # 章节数量上限
MAX_INTERVALS = 500          # 必留口播区间数量上限
MAX_SEGMENTS = 5000          # 单次提交的保留段数量上限

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "app.db")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 30 秒 PCM 约 1 MB，留足余量
app.json.ensure_ascii = False  # 中文错误信息直接输出


# ---------------------------------------------------------------- 错误

class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@app.errorhandler(ApiError)
def handle_api_error(err):
    body = {"error": err.code, "message": err.message}
    body.update(err.extra)
    return jsonify(body), err.status


@app.errorhandler(404)
def handle_404(_):
    return jsonify({"error": "not_found", "message": "资源不存在"}), 404


@app.errorhandler(405)
def handle_405(_):
    return jsonify({"error": "method_not_allowed", "message": "方法不允许"}), 405


@app.errorhandler(413)
def handle_413(_):
    return jsonify({"error": "payload_too_large", "message": "请求体超过 8 MB 上限"}), 413


@app.errorhandler(500)
def handle_500(_):
    return jsonify({"error": "internal_error", "message": "服务器内部错误"}), 500


# ---------------------------------------------------------------- 数据库

def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def immediate_tx(conn):
    """BEGIN IMMEDIATE 事务：针对同一节目的并发写在此串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
                id         TEXT PRIMARY KEY,
                revision   INTEGER NOT NULL,
                num_frames INTEGER NOT NULL,
                keeps      TEXT NOT NULL,  -- JSON [[start, end], ...]，原始帧坐标
                chapters   TEXT NOT NULL,  -- JSON [{"title": ..., "frame": ...}]，原始帧坐标
                mandatory  TEXT NOT NULL,  -- JSON [[start, end], ...]，必留口播
                wav        BLOB NOT NULL,  -- 原始音频，写入后永不修改
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                program_id TEXT NOT NULL REFERENCES programs (id),
                revision   INTEGER NOT NULL,
                keeps      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (program_id, revision)
            );
            """
        )
    finally:
        conn.close()


def row_to_program(row):
    return {
        "id": row["id"],
        "revision": row["revision"],
        "num_frames": row["num_frames"],
        "keeps": json.loads(row["keeps"]),
        "chapters": json.loads(row["chapters"]),
        "mandatory": json.loads(row["mandatory"]),
        "created_at": row["created_at"],
    }


def fetch_program(conn, pid):
    row = conn.execute("SELECT * FROM programs WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"节目 {pid} 不存在")
    return row


# ---------------------------------------------------------------- 校验与换算

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def inspect_wav(data):
    """校验 WAV 格式（16 kHz / 单声道 / 16 位 PCM / <=30 秒），返回采样帧数。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, sampwidth = w.getnchannels(), w.getsampwidth()
            framerate, comptype = w.getframerate(), w.getcomptype()
            num_frames = w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise ApiError(400, "invalid_wav", f"无法解析 WAV 文件：{exc}")
    if comptype != "NONE":
        raise ApiError(400, "invalid_wav", "仅支持未压缩的 PCM WAV 文件")
    if channels != 1:
        raise ApiError(400, "invalid_wav", f"要求单声道，实际为 {channels} 声道")
    if sampwidth != 2:
        raise ApiError(400, "invalid_wav", f"要求 16 位采样，实际为 {sampwidth * 8} 位")
    if framerate != SAMPLE_RATE:
        raise ApiError(400, "invalid_wav", f"要求 {SAMPLE_RATE} Hz 采样率，实际为 {framerate} Hz")
    if num_frames <= 0:
        raise ApiError(400, "invalid_wav", "音频不包含任何采样帧")
    if num_frames > MAX_FRAMES:
        raise ApiError(400, "invalid_wav",
                       f"音频时长 {num_frames / SAMPLE_RATE:.2f} 秒，超过 {MAX_SECONDS} 秒上限")
    return num_frames


def parse_intervals(raw, num_frames, what):
    """解析并校验区间列表：左闭右开、非空、升序、不重叠、落在 [0, num_frames) 内。"""
    if not isinstance(raw, list):
        raise ApiError(400, "invalid_body", f"{what}必须是数组")
    intervals = []
    prev_end = 0
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not _is_int(item.get("start")) or not _is_int(item.get("end")):
            raise ApiError(400, "invalid_body", f"{what}第 {i} 项必须是含整数 start/end 的对象")
        start, end = item["start"], item["end"]
        if start < 0 or start >= end:
            raise ApiError(400, "invalid_segment",
                           f"{what}第 {i} 项 [{start}, {end}) 非法：段必须非空且起点不为负（左闭右开）")
        if end > num_frames:
            raise ApiError(400, "invalid_segment",
                           f"{what}第 {i} 项 [{start}, {end}) 超出音频范围 [0, {num_frames})")
        if start < prev_end:
            raise ApiError(400, "invalid_segment",
                           f"{what}第 {i} 项 [{start}, {end}) 与之前的段重叠或未按升序排列")
        intervals.append([start, end])
        prev_end = end
    return intervals


def parse_chapters(raw, num_frames):
    if not isinstance(raw, list):
        raise ApiError(400, "invalid_body", "chapters 必须是数组")
    if len(raw) > MAX_CHAPTERS:
        raise ApiError(400, "invalid_body", f"章节数量超过上限 {MAX_CHAPTERS}")
    chapters, seen = [], set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ApiError(400, "invalid_chapter", f"第 {i} 个章节必须包含非空字符串 title")
        if not _is_int(item.get("frame")):
            raise ApiError(400, "invalid_chapter", f"第 {i} 个章节的 frame 必须是整数")
        frame = item["frame"]
        if not 0 <= frame < num_frames:
            raise ApiError(400, "invalid_chapter",
                           f"第 {i} 个章节起点 {frame} 超出音频范围 [0, {num_frames})")
        if frame in seen:
            raise ApiError(400, "invalid_chapter", f"多个章节落在同一帧 {frame}")
        seen.add(frame)
        chapters.append({"title": item["title"].strip(), "frame": frame})
    chapters.sort(key=lambda c: c["frame"])
    return chapters


def check_mandatory_covered(segments, mandatory):
    for m_start, m_end in mandatory:
        if not any(s <= m_start and m_end <= e for s, e in segments):
            raise ApiError(400, "mandatory_not_covered",
                           f"必留口播区间 [{m_start}, {m_end}) 未完整落入任何一个保留段")


def map_chapters_to_product(chapters, segments):
    """把原始帧坐标的章节换算为成品帧坐标。

    区间左闭右开：恰好位于保留段起点的章节保留；落在被删除区间的章节不再出现。
    每次都以原始帧重新计算，反复编辑不会积累偏移。
    """
    mapped = []
    base = 0
    for start, end in segments:
        for ch in chapters:
            if start <= ch["frame"] < end:
                mapped.append({
                    "title": ch["title"],
                    "frame": base + ch["frame"] - start,
                    "original_frame": ch["frame"],
                })
        base += end - start
    return mapped


def splice_wav(wav_bytes, segments):
    """按保留段从原始音频裁接出成品 WAV。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        pcm = w.readframes(w.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        for start, end in segments:
            out.writeframesraw(pcm[start * 2:end * 2])
    return buf.getvalue()


def program_view(prog):
    segments = prog["keeps"]
    product_frames = sum(e - s for s, e in segments)
    return {
        "program_id": prog["id"],
        "revision": prog["revision"],
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "source_frames": prog["num_frames"],
        "source_duration_sec": round(prog["num_frames"] / SAMPLE_RATE, 6),
        "product_frames": product_frames,
        "product_duration_sec": round(product_frames / SAMPLE_RATE, 6),
        "keeps": [{"start": s, "end": e} for s, e in segments],
        "chapters": map_chapters_to_product(prog["chapters"], segments),
        "mandatory": [{"start": s, "end": e} for s, e in prog["mandatory"]],
        "created_at": prog["created_at"],
        "download_url": f"/programs/{prog['id']}/download",
    }


# ---------------------------------------------------------------- 接口

@app.get("/")
def index():
    return jsonify({
        "service": "录音删段交付服务",
        "endpoints": {
            "POST /programs": "创建节目（multipart: file=WAV, meta=JSON）",
            "GET /programs": "列出全部节目",
            "GET /programs/<id>": "查询当前版本（保留段 + 成品帧坐标章节）",
            "PUT /programs/<id>/keeps": "按预期修订号提交完整保留清单",
            "GET /programs/<id>/download": "下载成品 ZIP（裁接 WAV + 章节 JSON）",
        },
    })


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.post("/programs")
def create_program():
    if "file" not in request.files:
        raise ApiError(400, "missing_file", "请用 multipart/form-data 的 file 字段上传 WAV 文件")
    data = request.files["file"].read()
    if not data:
        raise ApiError(400, "missing_file", "上传的 WAV 文件为空")
    num_frames = inspect_wav(data)

    meta_raw = request.form.get("meta", "{}")
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "invalid_meta", f"meta 字段不是合法 JSON：{exc}")
    if not isinstance(meta, dict):
        raise ApiError(400, "invalid_meta", "meta 字段必须是 JSON 对象")

    chapters = parse_chapters(meta.get("chapters", []), num_frames)
    mandatory = parse_intervals(meta.get("mandatory", []), num_frames, "必留口播区间")
    if len(mandatory) > MAX_INTERVALS:
        raise ApiError(400, "invalid_body", f"必留口播区间数量超过上限 {MAX_INTERVALS}")

    pid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    keeps = [[0, num_frames]]  # 初始保留全段
    conn = connect()
    try:
        with immediate_tx(conn):
            conn.execute(
                "INSERT INTO programs (id, revision, num_frames, keeps, chapters, mandatory, wav, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, 0, num_frames, json.dumps(keeps),
                 json.dumps(chapters, ensure_ascii=False), json.dumps(mandatory), data, now),
            )
            conn.execute(
                "INSERT INTO revisions (program_id, revision, keeps, created_at) VALUES (?, ?, ?, ?)",
                (pid, 0, json.dumps(keeps), now),
            )
    finally:
        conn.close()

    prog = {"id": pid, "revision": 0, "num_frames": num_frames, "keeps": keeps,
            "chapters": chapters, "mandatory": mandatory, "created_at": now}
    return jsonify(program_view(prog)), 201


@app.get("/programs")
def list_programs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, revision, num_frames, created_at FROM programs ORDER BY created_at, id"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"programs": [dict(row) for row in rows]})


@app.get("/programs/<pid>")
def get_program(pid):
    conn = connect()
    try:
        prog = row_to_program(fetch_program(conn, pid))
    finally:
        conn.close()
    return jsonify(program_view(prog))


@app.put("/programs/<pid>/keeps")
def submit_keeps(pid):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
    if not _is_int(body.get("expected_revision")):
        raise ApiError(400, "invalid_body", "expected_revision 必须是整数")
    if "segments" not in body:
        raise ApiError(400, "invalid_body", "缺少 segments 字段（完整保留清单）")
    expected = body["expected_revision"]

    conn = connect()
    try:
        with immediate_tx(conn):
            prog = row_to_program(fetch_program(conn, pid))
            if expected != prog["revision"]:
                raise ApiError(409, "revision_conflict",
                               f"期望修订号 {expected} 已过期，当前修订号为 {prog['revision']}；"
                               "请查询最新版本后重新提交",
                               current_revision=prog["revision"])
            segments = parse_intervals(body["segments"], prog["num_frames"], "保留段")
            if len(segments) > MAX_SEGMENTS:
                raise ApiError(400, "invalid_body", f"保留段数量超过上限 {MAX_SEGMENTS}")
            check_mandatory_covered(segments, prog["mandatory"])

            new_revision = prog["revision"] + 1
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("UPDATE programs SET revision = ?, keeps = ? WHERE id = ?",
                         (new_revision, json.dumps(segments), pid))
            conn.execute(
                "INSERT INTO revisions (program_id, revision, keeps, created_at) VALUES (?, ?, ?, ?)",
                (pid, new_revision, json.dumps(segments), now))
        prog["revision"] = new_revision
        prog["keeps"] = segments
    finally:
        conn.close()
    return jsonify(program_view(prog))


@app.get("/programs/<pid>/download")
def download_program(pid):
    conn = connect()
    try:
        # 单行 SELECT 是一个原子快照：revision / keeps / chapters / wav 必然来自同一修订，
        # 即使下载期间发生并发编辑，ZIP 内的 WAV 与章节表也不会混用版本。
        row = fetch_program(conn, pid)
        prog = row_to_program(row)
        wav_bytes = row["wav"]
    finally:
        conn.close()

    audio = splice_wav(wav_bytes, prog["keeps"])
    chapters_doc = {
        "program_id": prog["id"],
        "revision": prog["revision"],
        "sample_rate": SAMPLE_RATE,
        "product_frames": sum(e - s for s, e in prog["keeps"]),
        "chapters": [
            {"title": ch["title"], "frame": ch["frame"],
             "time_sec": round(ch["frame"] / SAMPLE_RATE, 6)}
            for ch in map_chapters_to_product(prog["chapters"], prog["keeps"])
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("audio.wav", audio)
        zf.writestr("chapters.json", json.dumps(chapters_doc, ensure_ascii=False, indent=2))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"program_{pid}_r{prog['revision']}.zip")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
