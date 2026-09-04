# Hosted 公网部署完整加固设计

## 背景

DesignToolkit 已具备匿名浏览器上传、Redis/RQ 队列、双 Worker、任务状态与
SSE、结果下载、两小时自动清理以及 Docker Compose + Caddy 部署能力。

真实容器联调已经通过，但发布前审查发现以下风险：

- multipart 请求会先由 Starlette 写入容器临时目录，绕过应用存储配额。
- 任务输出只在处理完成后检查大小，运行期间仍可能写满共享磁盘。
- 压缩媒体缺少解码后尺寸和时长限制。
- RQ 超时或 Worker 异常退出时，任务可能长期停留在 `running`。
- cleanup 与上传、处理、下载之间缺少跨进程互斥。
- 容器没有 CPU、内存和 PID 上限。

本设计用于消除这些风险，使默认配置适合匿名公网部署。

## 部署基线与硬限制

默认部署基线：

- 8 vCPU
- 16 GiB RAM
- 100 GB SSD

业务限制：

- 单文件最大 500 MiB。
- 单匿名会话最多占用 2 GiB。
- 单任务输出最多 5 GiB。
- 系统始终保留至少 5 GiB 可用磁盘。
- 最多 20 个等待任务。
- 最多 2 个并行 Worker。
- 文件和终态任务保留 2 小时。
- 图片最多 100,000,000 像素。
- 视频最多 3840 x 2160，最长 1,800 秒。

所有限制均可通过环境变量降低；生产默认值不得通过环境变量放宽到不安全值，
除非部署者显式修改代码或 Compose 配置。

## 方案比较

### 方案 A：仅依赖反向代理和容器限制

实现简单，但无法准确执行会话配额、任务输出总量限制，也无法修复任务和清理
竞态。不采用。

### 方案 B：应用层检查加定期清理

可以覆盖常规请求，但检查与写入不是原子操作，并发上传或双 Worker 仍可同时
通过磁盘检查。不采用。

### 方案 C：流式写入、Redis 原子预留、租约和容器限制

由应用逐块接收数据，在 Redis 中原子预留容量；Worker 使用输出预留和增量
预算；所有会话文件操作通过租约协调；容器层再设置资源上限。采用此方案。

## 上传协议

### API

`POST /api/uploads` 改为单文件原始请求体：

- `Content-Type` 为素材 MIME 类型。
- `Content-Length` 必须存在且为正数，否则返回 `411 Length Required`。
- `X-Upload-Filename` 使用 UTF-8 文件名经 URI 编码后的 ASCII 值。
- 一次请求只上传一个文件；前端多文件选择按顺序逐个请求。

不再使用 FastAPI `UploadFile` 或 multipart 解析，因此请求体不会先落到
Starlette 的临时目录。

### 容量预留

上传开始前通过 Redis Lua 脚本执行一次原子事务：

1. 验证全局上传并发数未超过 4。
2. 验证会话已用量加已预留量加 `Content-Length` 不超过 2 GiB。
3. 验证全局已预留量不会侵占 5 GiB 基础磁盘余量。
4. 创建带 TTL 的上传 reservation。

应用将 `Request.stream()` 数据写入 `/data` 下的 `.uploading` 文件，每个块均
更新实际字节数并再次检查 500 MiB 上限。成功后原子重命名；失败、断连或超时
时删除临时文件并释放 reservation。

客户端保持现有 XHR 上传进度体验，但请求体直接发送 `File`，不再构造
`FormData`。

## 媒体元数据限制

上传完成后、任务入队前验证媒体：

- 图片只读取头部元数据，拒绝宽乘高超过 100 MP 的文件。
- 视频使用现有 FFmpeg 元数据能力读取宽、高和时长。
- 视频宽高不得超过 3840 x 2160，时长不得超过 1,800 秒。
- 无法可靠读取元数据的文件按无效素材拒绝。

批处理请求逐个验证全部输入，任一输入不合法时整个任务不入队。错误响应使用
稳定错误码，并向用户返回可理解的限制说明。

## 输出容量保护

### 原子预留

Worker 开始任务前通过 Redis Lua 脚本预留 5 GiB 输出预算。脚本结合当前
磁盘可用空间和所有活跃 reservation，保证：

```text
free_bytes - reserved_bytes - requested_bytes >= min_free_bytes
```

reservation 记录任务、会话、预算和过期时间。Worker 周期续租，终态处理使用
幂等释放；异常退出由 reconciler 回收。

### 写入期限制

- Worker 的 Linux 任务进程设置 `RLIMIT_FSIZE=5 GiB`，限制任一单文件。
- 图片序列和批量结果在每个文件发布后更新 `OutputBudgetGuard`。
- Guard 使用已写入文件大小的增量累计，不在每帧重复扫描完整目录。
- 达到 5 GiB 前保留安全余量；超限立即抛出受控错误。
- 失败路径删除任务临时输出，并将任务标记为 `failed`。
- 最终打包仍执行一次完整大小校验，作为纵深防御。

## 任务终态与恢复

RQ enqueue 注册 failure 和 stopped 回调。回调必须：

- 将 `queued` 或 `running` 任务幂等转换为 `failed`。
- 写入终态日志。
- 释放队列 reservation、输出 reservation 和会话租约。
- 设置两小时终态 TTL。

cleanup 进程每分钟执行 reconciler：

- 对照 RQ Job、StartedJobRegistry、FailedJobRegistry 和 worker heartbeat。
- 修复已不存在、已停止、超时或 Worker 失联的任务记录。
- 不修改仍有有效 Job 或 Worker heartbeat 的任务。

SSE 在修复后的终态事件出现后正常结束。

## 会话租约与清理

Redis 为每个会话维护状态：

- `active_count`
- `deleting`
- 带 TTL 的操作令牌

上传、预览、任务处理和下载在访问会话文件前原子获取租约。若 `deleting=1`，
拒绝新操作。操作期间定期续租，结束时幂等释放。

cleanup 使用 Lua 脚本仅在 `active_count=0` 时设置 `deleting=1`。获得删除权后：

1. 再次确认会话已超过保留期。
2. 将目录原子重命名到 `/data/.trash/<session-id>-<token>`。
3. 清除会话 Redis 状态。
4. 递归删除隔离目录。

重命名失败时撤销 `deleting`。`.trash` 中的遗留目录由后续清理周期删除。

## 容器资源限制

默认 Compose 限制：

| 服务 | CPU | 内存 | PID |
| --- | ---: | ---: | ---: |
| app | 1.0 | 1536 MiB | 256 |
| worker-1 | 3.0 | 5 GiB | 256 |
| worker-2 | 3.0 | 5 GiB | 256 |
| redis | 0.5 | 512 MiB | 128 |
| caddy | 0.25 | 256 MiB | 128 |
| cleanup | 0.25 | 256 MiB | 128 |

限制通过 `.env` 变量覆盖。Worker 内存限制必须高于允许的单帧解码峰值；应用
仍依赖媒体元数据限制避免压缩炸弹和超长视频。

## 错误模型

- `411`: 缺少或无效的 `Content-Length`。
- `413`: 上传、会话或输出大小超限。
- `422`: 媒体像素、视频尺寸或时长超限，或元数据不可读取。
- `429`: 上传并发或请求频率超限。
- `503`: 磁盘容量不足或暂时无法获得资源 reservation。
- Worker 超时、停止和进程异常统一落为可恢复查询的 `failed` 终态。

所有失败路径必须清理临时文件并幂等释放 Redis reservation/lease。

## 测试策略

按 TDD 分批实施：

1. 上传流式协议、缺失长度、块级超限、断连清理和并发 reservation。
2. 图片像素、视频尺寸、视频时长和批量输入验证。
3. 双 Worker 原子输出预留、单文件及序列总量超限。
4. RQ failure/stopped 回调与失联任务 reconciler。
5. cleanup 与上传、处理、下载并发时的租约测试。
6. Compose 资源配置与一条命令冷启动测试。
7. 浏览器上传、任务执行、SSE、下载和会话隔离端到端测试。

最终必须通过：

- `uv run pytest`
- `uv run ruff check .`
- `uv run python -m compileall -q src tests`
- `npm test -- --run`
- `npm run build`
- 本地模式 Playwright
- Hosted 模式 Playwright
- `docker compose up -d --build`
- 真实浏览器上传、处理和下载

## 文档与发布

README 增加本地 Docker 联调、生产域名配置、启动、升级、日志和停止命令。
`.env.example` 列出资源上限与安全限制。最终实现和文档通过审查后统一提交到
`main` 并推送 `origin/main`。
