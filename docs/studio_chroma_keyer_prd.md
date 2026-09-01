# Studio Chroma Keyer MVP PRD

## 1. 产品定位

Studio Chroma Keyer 是 DesignToolkit 中的本地专业抠像模块。它使用公开图像处理原理生成连续 Alpha Matte，不使用任何第三方私有抠像算法。

本阶段只解决单张图片的绿幕、蓝幕或其他纯色背景抠除问题：用户在画布上吸取实际要抠除的颜色，调整基础参数，预览并导出透明 PNG。

## 2. MVP 目标

1. 从本机绝对路径导入一张 PNG、JPG 或 JPEG 图片。
2. 展示缩放预览画布，允许用户吸取一个待抠除的背景颜色；再次吸取时替换当前颜色。
3. 基于采样颜色生成连续 Alpha Matte，不要求用户预先选择“绿幕”或“蓝幕”。
4. 支持基础 Matte 调整、边缘羽化及基于采样背景色反混合的 Despill。
5. 支持 Final 和 Alpha Matte 预览。
6. 导出原图分辨率的 RGBA PNG。

## 3. 非目标

MVP 不实现以下能力：

- 浏览器文件上传、拖入上传、云端或对象存储。
- 视频、序列帧、ZIP 导入和批量导出。
- 任意帧选择、时间轴、参数关键帧或取消任务。
- GPU/WebGL 实时计算、AI 边缘恢复、Guided Filter、ProRes 或 EXR 输出。
- 自动背景颜色检测、前景/背景画笔或 Trimap。

视频与序列帧处理将在第二阶段以同一套 `KeyerParams` 和单帧算法为基础扩展。

## 4. 用户流程

1. 用户进入“Studio Chroma Keyer”模块。
2. 用户通过路径输入框或文件夹按钮选择一张本地图片。
3. 后端读取图片并返回预览图及原图尺寸。
4. 用户启用取样工具，在预览画布点击背景区域。
5. 前端将预览坐标和预览尺寸提交给后端；后端换算原图坐标并返回采样颜色。
6. 用户可重新取样以替换当前颜色，或清空当前颜色。
7. 系统以当前采样色生成预览，用户调整参数。
8. 用户在 Final / Alpha Matte 视图中检查结果。
9. 用户选择输出路径，导出 RGBA PNG。

## 5. 本地路径约束

本项目的 Web GUI 运行在本地，浏览器不能可靠地取得绝对文件路径。MVP 必须沿用现有模式：

- 用户可手动粘贴本地绝对路径。
- 文件夹图标调用 FastAPI 进程提供的本机原生文件选择器。
- 不提供 multipart 上传端点。
- 仅处理当前 FastAPI 进程可访问的本地文件。

## 6. 采样与颜色模型

### 6.1 颜色吸取

- 用户吸取的是“需要被抠除的实际背景颜色”，可为绿色、蓝色或其他单色背景。
- 系统只保留一个采样颜色；再次点击画布时，新颜色替换原颜色。
- UI 显示当前采样色块、RGB 和 HEX，并支持清空。
- 预览和导出请求必须包含且仅包含一个采样颜色；清空后不可预览或导出。
- 采样色的最强通道必须比其余通道至少高 `12.75`，否则按低饱和度背景拒绝，并提示用户选择具有明显主色的纯色幕背景。

### 6.2 坐标契约

前端请求包含 `x`、`y`、`preview_width`、`preview_height`。后端按下式映射并截断到原图边界：

```text
source_x = floor(x * source_width / preview_width)
source_y = floor(y * source_height / preview_height)
```

采样必须来自原图，而非浏览器缩放、压缩或叠加后的像素。

### 6.3 单色模型

`screen_model_from_samples` 严格接收一个 RGB 采样值。`mean_rgb` 等于该采样值，
`stddev_rgb` 固定为零，`samples` 只包含该颜色。空列表、多于一个颜色或低饱和度
颜色均拒绝，不再通过多点均值或颜色离散度扩展抠除范围。

### 6.4 基于采样背景色的 Despill

不设置固定的绿幕/蓝幕开关，也不只抑制采样色的主通道。系统将 Alpha
为 `a` 的半透明像素视为前景 `F` 与采样背景均值 `B` 的合成结果 `C`，并对
完整 RGB 向量执行颜色反混合：

```text
C = aF + (1 - a)B
F = clip((C - (1 - a)B) / max(a, 1/255), 0, 255)
result = lerp(C, F, despill_amount)
```

仅对 Alpha 字节处于 `1..254` 的像素计算恢复色。Alpha 为 `0` 或 `255` 时，
输出 RGB 必须与输入逐字节一致；`despill_amount = 0` 时整张图 RGB 不变；
`despill_amount = 1` 时半透明像素使用恢复色，中间值在原始合成色与恢复色
之间线性插值。

该模型没有 Trimap 或前景颜色先验，无法区分“接近采样背景色的真实前景”与
“背景颜色混入”。这类真实前景色可能发生偏移，用户可降低 Despill Amount
以换取更保守的颜色保留。

## 7. Matte 与预览

### 7.1 初始 Alpha

算法使用 Keylight 风格的主色通道覆盖率。设采样色的最强通道为 `D`，
`screen_balance` 为 `b`，先在其余两个通道之间计算平衡值，再比较主通道的
饱和度：

```text
screen_other = (1 - b) * max(screen_other_channels) + b * min(screen_other_channels)
screen_saturation = screen[D] - screen_other
pixel_other = (1 - b) * max(pixel_other_channels) + b * min(pixel_other_channels)
pixel_saturation = pixel[D] - pixel_other
coverage = clip(screen_gain * pixel_saturation / screen_saturation, 0, 1)
alpha = 1 - coverage
```

默认 `screen_gain = 1.00`、`screen_balance = 0.50`。提高 Screen Gain 会抠除
更多与幕布主色方向一致的像素；Screen Balance 决定两个非主通道对覆盖率的
相对贡献。结果保持连续 Alpha，以保留半透明边缘。

### 7.2 MVP 参数

| 参数 | 范围 | 默认值 | 作用 |
| --- | --- | --- | --- |
| Screen Gain | 0.00 - 2.00 | 1.00 | 调整主色覆盖率和抠除强度 |
| Screen Balance | 0.00 - 1.00 | 0.50 | 平衡两个非主色通道 |
| Clip Black | 0.00 - 1.00 | 0.00 | 将低 Alpha 压向透明 |
| Clip White | 0.00 - 1.00 | 1.00 | 将高 Alpha 压向不透明 |
| Shrink / Grow | -10 - 10 px | 0 | Alpha 收缩或扩张 |
| Feather | 0 - 10 px | 0 | Alpha 边缘羽化 |
| Despill Amount | 0.00 - 1.00 | 1.00 | 采样背景色反混合强度 |

### 7.3 预览模式

- Final：透明棋盘格上的 RGBA 结果。
- Alpha Matte：黑色代表透明、白色代表不透明、灰色代表半透明。

预览可使用降采样图以控制响应时间；导出始终使用原图分辨率。

## 8. 输出

- 格式固定为 RGBA PNG。
- 默认输出路径为 `<输入文件名>_keyed.png`，位于输入文件同级目录。
- 用户可通过现有保存路径选择器指定输出位置。
- 若输出存在，任务必须失败并提示用户更换路径，不得静默覆盖。

## 9. 验收标准

1. 用户可从本地路径导入 PNG/JPG/JPEG，错误路径或不支持格式有明确提示。
2. 用户在预览画布点击后，后端返回与原图对应坐标的 RGB/HEX 采样结果。
3. 系统始终只保留一个采样颜色；重新取样会替换当前颜色，清空后不可预览或导出，低饱和度颜色必须拒绝。
4. 对绿色和蓝色背景测试素材，背景区域基本透明，主体核心区域保持不透明。
5. Final 与 Alpha Matte 预览随参数变更更新。
6. 导出的 PNG 与输入尺寸一致，含 Alpha 通道。
7. 默认输出不覆盖已存在文件。
8. 对白色前景叠加绿色背景的 25%、50%、75% Alpha 合成像素，默认
   `despill_amount = 1.0` 的 Despill
   可在 uint8 舍入误差内恢复白色；透明和不透明端点的 RGB 保持逐字节不变。
9. Despill 仅处理 Alpha 为 `1..254` 的像素；由于 MVP 没有 Trimap 或前景颜色
   先验，接近幕布色的真实前景可能偏色，用户可降低 Despill Amount。

## 10. 技术约束

- 前端：既有 React + Vite + TypeScript + Zustand。
- 后端：既有 FastAPI、SSE 任务日志和本机路径选择 API。
- 算法：Python、NumPy、OpenCV、Pillow；视频能力继续由第二阶段复用 FFmpeg。
- 处理接口必须避免双重提交并复用现有任务状态和日志订阅机制。
