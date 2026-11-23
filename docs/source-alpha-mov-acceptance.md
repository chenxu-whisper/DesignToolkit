# MOV 原生 Alpha 使用与验收

## 使用步骤

1. 在色键抠图页面选择带原生 Alpha 的 MOV。
2. 将透明来源设为「视频原始透明通道」，无需采样背景色。
3. 按素材选择「直通 Alpha」或「预乘 Alpha」，检查 Final 和 Alpha 预览。
4. 导出 MP4，并保留同名 JSON 和 `<输出名>_player` 目录。
5. 用浏览器打开播放器的 `index.html`，选择 MP4 和 JSON，检查透明效果。

导出画面左侧为黑白灰度 Alpha，右侧为 RGB；JSON 的 `layout` 为
`alpha_left_color_right`。普通播放器显示左右拼接画面，配套播放器恢复透明显示，
并兼容旧的 `color_left_alpha_right` 布局。

## 验收范围

| 场景 | 预期结果 |
| --- | --- |
| QTRLE、ProRes 4444 MOV | 读取原生 Alpha，Final 与 Alpha 预览一致 |
| 直通与预乘素材 | 按显式选择解释 RGB；预乘模式还原颜色 |
| 无 Alpha 的 H.264 MOV | 明确拒绝原生 Alpha 模式 |
| 原生 Alpha 导出 | 不应用色键、羽化或去杂色参数 |
| 拼接布局 | 左侧三个通道均为 Alpha，右侧为 RGB |
| 奇数宽高 | Alpha 补零，RGB 延续边缘，播放器裁去补齐区域 |
| 带音轨素材 | 第一条音轨转为 AAC，JSON 保留音频信息 |
| 离线播放器 | 本地选择 MP4 与 JSON，无需 CDN |
| 桌面与移动端 | 1440px 与 390px 视图可预览、切换模式及导出 |

2026-11-23 的已有验收记录显示：Python 回归 226 项通过，3 项因 Windows
符号链接权限跳过；前端 504 项通过；透明播放器 13 项通过。
Ruff、compileall、TypeScript 检查与生产构建通过。
Python 记录包含一项现有 Starlette/httpx 弃用警告。

主目录 GUI 的实际导出解码结果：左侧 Alpha 样本分别为
`[0, 0, 0]`、`[128, 128, 128]`、`[255, 255, 255]`；
右侧 RGB 为 `[19, 218, 40]`（源颜色 `[20, 220, 40]`），并包含 AAC 音轨。
这些数值允许 H.264 有损压缩带来的微小偏差。

## 重现检查

在项目根目录执行：

```powershell
uv run ruff check .
uv run pytest
$env:PYTHONPYCACHEPREFIX = "$PWD/.temp/pycache"
uv run python -m compileall -q src tests
cd web/frontend
npm run lint
npm test
npm run build
npm run test:e2e
```

自动回归素材由测试生成；本地大体积素材、导出文件、截图和完整运行日志留在
`.temp/`，不纳入版本控制。

## 格式限制

支持 FFmpeg 可解码原生 Alpha 的 MOV，不根据扩展名推断透明通道。
颜色解释由用户指定；高位深 Alpha 转为 8 位，H.264 对颜色和遮罩均有损。
当前验收覆盖恒定帧率对齐；原生 Alpha 模式固定使用拼接 MP4 导出。

API 字段与示例见 [README](../README.md#保留-mov-自带透明信息)，
播放器接入见 [播放器说明](../src/designtoolkit/chroma_keyer/player/README.md)。
