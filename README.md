# 录音删段交付服务

上传一段原始 WAV（≤30 秒、16 kHz、单声道、16 位 PCM），登记章节起点与必留口播
区间，随后按「预期修订号 + 完整保留清单」的方式反复删段，随时下载真正裁接好的
成品（ZIP：WAV + JSON 章节表）。原始音频永不修改，所有坐标均以原始采样帧为准。

技术栈：Python · Flask · SQLite · wave。

## 快速开始（单条命令）

```bash
docker compose up --build
```

服务监听 **8080** 端口：<http://localhost:8080>

数据（SQLite 库 + 原始音频）保存在 Compose 命名卷 `app-data` 中，容器重启后
可继续编辑既有节目。

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
python app.py                 # 同样监听 8080
python tests/smoke_test.py    # 冒烟测试（可选）
```

## 坐标与换算规则

- 所有接口中的帧坐标都是**原始音频的采样帧**（0 起），区间一律**左闭右开**
  `[start, end)`。
- 保留段必须：非空、升序、互不重叠、落在 `[0, 音频总帧数)` 内；每个必留口播
  区间必须**完整**落入某个保留段。
- 查询与下载时，章节被换算为**成品帧坐标**：落在被删除区间的章节不再出现；
  恰好位于保留段起点的章节保留（左闭）。每次换算都从原始帧重新计算，反复
  编辑不会积累偏移。
- 提交保留清单必须携带 `expected_revision`；与当前修订号不符时返回 409 并
  给出当前修订号，旧修订与成品不受影响。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/programs` | 创建节目：multipart 上传 `file`（WAV）+ `meta`（JSON：`chapters`、`mandatory`） |
| GET | `/programs` | 列出全部节目 |
| GET | `/programs/<id>` | 查询当前版本：保留段 + 成品帧坐标章节 |
| PUT | `/programs/<id>/keeps` | 提交完整保留清单：`{"expected_revision": N, "segments": [...]}` |
| GET | `/programs/<id>/download` | 下载成品 ZIP（`audio.wav` + `chapters.json`） |
| GET | `/healthz` | 健康检查 |

## 调用示例

### 0. 生成一段 10 秒测试音频（160000 帧）

```bash
python3 - <<'PY'
import math, struct, wave
with wave.open("demo.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"".join(
        struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(16000 * 10)))
PY
```

### 1. 创建节目

章节：0 帧「开场」、50000 帧「广告」、64000 帧「正文」；必留口播 `[16000, 32000)`：

```bash
curl -s -X POST http://localhost:8080/programs \
  -F "file=@demo.wav" \
  -F 'meta={"chapters":[{"title":"开场","frame":0},{"title":"广告","frame":50000},{"title":"正文","frame":64000}],"mandatory":[{"start":16000,"end":32000}]}'
```

响应（201，节选）：

```json
{
  "program_id": "9f2c1a0b3d4e",
  "revision": 0,
  "source_frames": 160000,
  "product_frames": 160000,
  "keeps": [{"start": 0, "end": 160000}],
  "chapters": [
    {"title": "开场", "frame": 0, "original_frame": 0},
    {"title": "广告", "frame": 50000, "original_frame": 50000},
    {"title": "正文", "frame": 64000, "original_frame": 64000}
  ]
}
```

### 2. 提交保留清单（删除 `[48000, 64000)`）

```bash
curl -s -X PUT http://localhost:8080/programs/<PID>/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"segments":[{"start":0,"end":48000},{"start":64000,"end":160000}]}'
```

### 3. 查询当前版本

```bash
curl -s http://localhost:8080/programs/<PID>
```

此时「广告」（原始 50000 帧）落在删除区间，不再出现；「正文」（原始 64000 帧）
换算为成品第 48000 帧；`revision` 变为 1。

### 4. 下载成品 ZIP

```bash
curl -s -OJ http://localhost:8080/programs/<PID>/download
unzip -l program_<PID>_r1.zip        # audio.wav + chapters.json
```

`audio.wav` 是按保留段真实裁接后的成品（本例 144000 帧 / 9 秒）；
`chapters.json` 记录同一修订号的章节表（成品帧坐标）。

### 5. 错误示例

修订号过期（409）：

```bash
curl -s -X PUT http://localhost:8080/programs/<PID>/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"segments":[{"start":0,"end":160000}]}'
# {"error":"revision_conflict","message":"期望修订号 0 已过期，当前修订号为 1；请查询最新版本后重新提交","current_revision":1}
```

必留口播未被完整保留（400）：

```bash
curl -s -X PUT http://localhost:8080/programs/<PID>/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"segments":[{"start":40000,"end":160000}]}'
# {"error":"mandatory_not_covered","message":"必留口播区间 [16000, 32000) 未完整落入任何一个保留段"}
```

其余 400 情形：空段 / 乱序 / 重叠 / 越界的保留段，非 16 kHz·单声道·16 位
或超过 30 秒的 WAV 等，响应中均带有具体原因。

## 一致性与持久化设计

- 原始 WAV 以 BLOB 存入 SQLite，写入后永不修改；成品每次按「原始音频 + 当前
  保留段」实时裁接，因此反复编辑不会产生偏移积累。
- 提交保留清单时在 `BEGIN IMMEDIATE` 事务内完成「校验修订号 → 校验区间 →
  写入新修订」，并发提交同一修订号时只有一个成功，其余收到 409。
- 下载时对节目行做单次原子读取，ZIP 内的 WAV 与章节表必然来自同一修订；
  下载期间发生的并发编辑不会造成版本混用。
- 每次成功修订都会写入 `revisions` 历史表；无效提交不改变任何状态。
- 数据落盘于 `/data`（Compose 命名卷），服务重启后节目可继续编辑。
