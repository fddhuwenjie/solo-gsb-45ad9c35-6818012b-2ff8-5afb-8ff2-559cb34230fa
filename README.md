# 录音删段交付 API

对节目录音做「删段」交付的 HTTP 服务：上传原始 WAV 后，按**原始采样帧坐标**反复提交保留清单，随时查询换算为**成品帧坐标**的章节，并下载真正裁接好的成品（WAV + 章节 JSON 的 ZIP）。

技术栈：Python · Flask · SQLite · 标准库 `wave` / `zipfile`。

## 快速开始

单条命令启动（Docker 与 Docker Compose）：

```bash
docker compose up --build
```

服务监听 **8000** 端口。数据保存在名为 `app-data` 的卷中（SQLite，WAL 模式），容器重启后节目仍可继续编辑。

本地运行（可选）：

```bash
pip install -r requirements.txt
python app.py                 # 监听 0.0.0.0:8000，可用 PORT 环境变量修改
```

运行测试：

```bash
pip install pytest
python -m pytest tests/ -v
```

## 概念与规则

- **音频规格**：≤ 30 秒、16 kHz、单声道、16 位 PCM WAV（上限 480000 帧）。原始音频入库后永不修改。
- **帧坐标**：所有接口中的坐标均为**原始音频的采样帧坐标**；区间一律**左闭右开** `[start_frame, end_frame)`。
- **修订号**：创建节目时为 `0`，初始保留全段 `[0, total_frames)`。每次提交保留清单必须带上 `expected_revision`，等于当前修订号才生效，成功后修订号 +1。过期或非法提交会返回原因，旧修订与成品保持原样。
- **保留清单**：完整替换式语义。各段须非空、升序、不重叠、在音频范围内；每个**必留口播区间**必须完整落入某一个保留段。
- **章节投影**：查询与下载时，章节被换算为成品帧坐标——被删除区间内的章节不再出现；位于保留段**起点**的章节保留（左闭）。换算始终从原始帧出发，反复编辑不会积累偏移。
- **一致性**：每次成功提交产生一个不可变修订；下载在单个读事务内取定修订快照，同一 ZIP 中的 WAV 与 JSON 必然来自同一修订，并发编辑不会混用版本。并发提交同一 `expected_revision` 时只有一个成功，其余收到 `409 stale_revision`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/programs` | 创建节目（multipart：`audio` 文件 + `chapters`/`must_keep` JSON 字段） |
| GET | `/programs` | 列出全部节目 |
| GET | `/programs/{id}` | 查询当前版本（保留段 + 成品帧坐标章节） |
| PUT | `/programs/{id}/keeps` | 按预期修订号提交完整保留清单 |
| GET | `/programs/{id}/download` | 下载 ZIP（`audio.wav` + `chapters.json`） |

错误统一返回 JSON：`{"error": <代码>, "message": <中文原因>, ...}`，状态码为 400（参数非法）/ 404（不存在）/ 409（修订号过期）。

## 可执行的调用示例

先准备一段合规测试音频（10 秒 16 kHz 单声道 16 位）：

```bash
python3 - <<'PY'
import wave, math, struct
with wave.open("tone.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"".join(
        struct.pack("<h", int(10000*math.sin(2*math.pi*440*i/16000)))
        for i in range(16000*10)))
PY
```

### 1. 创建节目（登记章节与必留口播）

```bash
curl -s -X POST http://localhost:8000/programs \
  -F "audio=@tone.wav" \
  -F 'chapters=[{"title":"开场","start_frame":0},{"title":"口播","start_frame":16000},{"title":"正文","start_frame":80000},{"title":"结尾","start_frame":150000}]' \
  -F 'must_keep=[{"start_frame":16000,"end_frame":32000}]'
```

响应 `201`，含 `program_id` 与 `revision: 0`。后续用 `PID=<program_id>` 指代。

### 2. 查询当前版本

```bash
curl -s http://localhost:8000/programs/$PID
```

`chapters[*]` 中 `start_frame` 为原始帧坐标，`product_start_frame` 为换算后的成品帧坐标（修订 0 时两者相同）。

### 3. 提交保留清单（删段）

删去 `[32000,48000)` 与 `[96000,112000)` 两段：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,
       "keep_segments":[{"start_frame":0,"end_frame":32000},
                        {"start_frame":48000,"end_frame":96000},
                        {"start_frame":112000,"end_frame":160000}]}'
```

响应中 `revision` 变为 `1`，`product_frames` 为 `128000`，章节被重新投影（如「正文」从原始 80000 变为成品 64000）。

过期修订号示例（返回 409 与原因，状态不变）：

```bash
curl -s -X PUT http://localhost:8000/programs/$PID/keeps \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"keep_segments":[{"start_frame":0,"end_frame":160000}]}'
# => {"current_revision":1,"error":"stale_revision","message":"预期修订号 0 与当前修订号 1 不一致；..."}
```

### 4. 下载成品 ZIP

```bash
curl -s -OJ http://localhost:8000/programs/$PID/download
unzip -o program-$PID-rev1.zip   # 得到 audio.wav 与 chapters.json
```

- `audio.wav`：按保留段真正裁接后的成品（16 kHz / 单声道 / 16 位，帧数等于保留段总长）。
- `chapters.json`：同一修订的章节表，`start_frame` 为成品帧坐标。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8000` | 本地直接运行时的监听端口 |
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |

## 项目结构

```
app.py               # 全部 API 与核心逻辑（校验、章节投影、裁接、打包）
tests/test_app.py    # pytest 测试（规格校验、投影、并发修订、下载一致性、持久化）
requirements.txt     # flask + gunicorn
Dockerfile           # python:3.12-slim + gunicorn
docker-compose.yml   # 单条命令启动，卷持久化
```
