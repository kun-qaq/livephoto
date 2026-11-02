# LivePhoto 制作工具

Turn any video into an iPhone **Live Photo** — crop, trim 0.5~5 s, speed/rotate/reverse/mute, and output a `JPG + MOV` pair that iOS Photos recognizes.

把任意视频转成 iPhone「实况图」(Live Photo) 的小工具：裁剪画面、截取 0.5~5 秒、变速/旋转/倒放/静音，一键输出 iOS 相册能识别的 `JPG + MOV` 配对文件。

- **[⬇ 点这里下载 exe](../../releases/latest)** —— Windows 免安装绿色单文件，双击即用（ffmpeg 已内置）
- 纯 Python，单文件 `livephoto.py`，图形界面 + 命令行双入口
- 视频处理用 ffmpeg（`imageio-ffmpeg` 自带二进制，**无需单独安装 ffmpeg**）
- 元数据写入参考开源项目 [video2livephoto](https://github.com/969246694/video2livephoto)，实测 iOS 可识别
- 支持**无损模式**（不重编码，视频数据逐字节搬运）和 **`.livp` 单文件打包**

---

## 目录结构

```
livephoto/
├── livephoto.py              # 主程序 (GUI + CLI)
├── requirements.txt          # 依赖
├── icon.ico                  # 程序图标
├── build_exe.bat             # 本地打包 exe 脚本 (PyInstaller)
├── .github/workflows/        # CI: 打 tag 自动构建并发布 Release
└── output/                   # 转换结果输出到这里 (exe 模式输出在 exe 旁)
```

## 安装

**方式 A：直接用 exe（推荐，无需 Python）**

去 [Releases](../../releases/latest) 下载 `LivePhotoMaker.exe`，放到任意文件夹双击打开图形界面；也可以在终端里当命令行工具用：

```bat
LivePhotoMaker.exe 视频.mp4 --crop portrait --livp
```

**方式 B：跑源码**（需要 Python 3.8+，Windows / macOS / Linux 通用，GUI 需要 tkinter）：

```bash
pip install -r requirements.txt
# 就两个包: pillow + imageio-ffmpeg (ffmpeg 二进制首次使用时自动下载)
```

---

## 快速开始

### 图形界面（推荐）

```bash
python livephoto.py          # 不带参数 = 打开图形界面
```

或直接**双击** `livephoto.py`。

界面用法（从上到下）：

1. **视频列表**：`添加视频`（可多选）或 `添加文件夹`（批量）；选中某一行会自动加载预览
2. **预览 / 拖拽框选裁剪**：
   - 在画面上**按住鼠标左键拖拽** → 框选裁剪区域（自动填入"自定义"）
   - 拖动**预览时间**滑块换帧，点 `设当前帧为封面` 指定封面帧（不设则取截取段中间）
3. **时间截取**：起点 + 时长滑块（时长强制限制在 **0.5~5 秒**）
4. **空间裁剪**：不裁剪 / 居中正方形 / 9:16 竖屏 / 自定义（x, y, 宽, 高）
5. **画面调整**：速度（0.5x 慢放 ~ 2x 快放）、旋转（90°/180°/270°）、画质、静音、倒放
6. **输出**：打包 `.livp`、无损模式、转换完自动打开输出文件夹
7. 点 **开始转换(批量)**，日志区看进度

> 批量时，裁剪/时间/封面等设置对所有视频**统一生效**（预览以选中项为准）。
> 分辨率不同的视频会自动夹取到画面内，不会越界。

### 命令行

```bash
# 基础: 截取前 3 秒, 不裁剪
python livephoto.py 视频.mp4

# 常用参数
python livephoto.py 视频.mp4 -o output -n IMG_0001     # 输出目录 + 文件名
python livephoto.py 视频.mp4 --start 2 --duration 3    # 截取 2~5 秒
python livephoto.py 视频.mp4 --crop portrait           # 9:16 竖屏裁剪
python livephoto.py 视频.mp4 --crop custom --cx 100 --cy 50 --cw 720 --ch 1280

# 效果
python livephoto.py 视频.mp4 --speed 0.5     # 0.5x 慢放(输出仍是设定时长, 取源片2倍素材)
python livephoto.py 视频.mp4 --rotate 90     # 顺时针旋转
python livephoto.py 视频.mp4 --reverse       # 倒放
python livephoto.py 视频.mp4 --mute          # 去掉音频
python livephoto.py 视频.mp4 --crf 23        # 画质(14~28, 越小越清晰越大)

# 封面帧
python livephoto.py 视频.mp4 --cover-t 1.5   # 封面取 1.5s 处的帧
python livephoto.py 视频.mp4 --cover-rel 0.3 # 封面取截取段 30% 处

# 无损模式: 不重编码(需不裁剪/不变速/不旋转/全长 0.5~5s, 不满足自动回退并提示)
python livephoto.py 短视频.mp4 --lossless

# 打包 & 打开文件夹
python livephoto.py 视频.mp4 --livp --open

# 批量
python livephoto.py --batch 视频文件夹                    # 处理目录下所有视频
python livephoto.py --batch 视频文件夹 -p "IMG_{n:04d}"  # 批量命名: IMG_0001, IMG_0002...
python livephoto.py a.mp4 b.mp4 c.mp4                    # 也可以直接列多个文件
```

### 子命令

```bash
# 查看信息: 视频时长/分辨率/编码, 或实况图的配对 ID
python livephoto.py info 视频.mp4 output/XXX.JPG output/XXX.MOV 某图.livp

# 校验: 检查 JPG+MOV (或 .livp) 的配对标识是否一致, 可传目录批量检查
python livephoto.py verify output/

# 解包: 把 .livp 还原成 JPG + MOV
python livephoto.py unlivp 某实况图.livp [-o 输出目录]

# 配对: 已有封面图 + 视频, 直接合成实况图(全程零重编码, 视频一个字节不动)
python livephoto.py pair 封面.jpg 视频.mp4 [-n 名字] [--livp]
```

---

## 转出来的是什么

一对**同名**文件（这就是 iOS 实况图的构成）：

| 文件 | 内容 | 关键元数据 |
|---|---|---|
| `NAME.JPG` | 封面照片 | Apple MakerNote `0x0011` = UUID |
| `NAME.MOV` | 3 秒左右的视频 | `com.apple.quicktime.content.identifier` = 同一 UUID |
| `NAME.livp` | (可选) 上两者打成的 zip 单文件 | — |

iOS「照片」App 靠这**同一个 UUID** 把两文件配对成实况图：相册里显示封面、长按播放、可设为实况壁纸。

## 怎么传到 iPhone（重要）

✅ 可行：
- **隔空投送**：两个文件同时选中一起发（Mac→iPhone）
- **iCloud 云盘 / 文件 App**：两文件放同一文件夹，一起导入
- 数据线 + Finder / 照片导入

❌ 不行：
- **微信 / QQ**：会重新压缩、剥掉元数据，传过去就变成"一张照片 + 一段视频"
- 分开发送（必须同名 + 一起传）

导入后相册里应显示为**一条**带「实况」角标的照片，而不是两个项目。

## 常见问题

**导入后 iPhone 不认（只是照片+视频）？**
先跑 `python livephoto.py verify output/` 确认配对没问题；再确认传输方式没剥元数据（见上）；两个文件必须**同名**。

**为什么有时长限制？**
实况图本质是 3 秒左右的动态照片，iOS 对 0.5~5s 兼容最好；超过 5 秒导入可以，但设锁屏壁纸可能被拒。工具已强制输出 0.5~5s。

**无损模式和普通模式差在哪？**
- 普通模式：裁剪/截取/变速需要重编码（H.264 CRF18，画质基本无损但编码一次）
- 无损模式：条件满足时（不裁剪/不变速/不旋转/全长 0.5~5s）视频数据**逐字节搬运**，零质量损失、速度极快。每次转换都会自校验（`--no-verify` 可关）。

**封面帧是哪一帧？**
默认取截取段中间那帧；GUI 拖时间轴选帧后点「设当前帧为封面」；CLI 用 `--cover-t`（绝对时间）或 `--cover-rel`（段内比例）。

**真机拍的实况图还有什么我没有的？**
`still-image-time` 定时轨和 CoreMotion 运动数据轨。前者只影响锁屏壁纸的起止位置，后者是相机传感器数据无法伪造。**不影响**照片 App 识别、长按播放、设为实况壁纸（动态效果正常）。

**视频是 HEVC / 其他编码行吗？**
能导入，但 iOS 对 H.264 兼容性最好。工具普通模式会统一重编码为 H.264(avc1)；无损模式直通原编码（会提示）。`pair` 子命令不重编码，非 H.264 源会提示。

---

## 技术原理（简）

1. **ffmpeg 预处理**：`-ss/-t` 输入侧截取 + `crop/transpose/reverse/setpts/atempo` 滤镜链，重编码为 H.264 / yuv420p / avc1（Live Photo 最稳的编码组合）
2. **封面**：ffmpeg 抽帧 → JPEG（`-strict unofficial` 兼容 ffmpeg 7.x 的 mjpeg 限制）
3. **元数据注入**（纯 Python，无第三方依赖）：
   - JPEG：构造 `Apple iOS` MakerNote，tag `0x0011` 存 UUID，写进 Exif APP1 段
   - MOV：解析 ISOBMFF 盒子树，在 `moov/meta/keys/ilst` 写入 `com.apple.quicktime.content.identifier`，重算 chunk 偏移，`ftyp` 改为 `qt` 品牌
4. **校验**：逐样本比对输出与源的视频数据字节是否一致（无损保证）

## 依赖

| 包 | 用途 |
|---|---|
| `pillow` | GUI 预览图加载显示；`pair` 转换非 JPEG 封面 |
| `imageio-ffmpeg` | 提供 ffmpeg 二进制（也支持用 PATH 里的 ffmpeg） |
| `tkinter` | 图形界面（Python 标准库自带；CLI 不需要） |

## 打包 exe（PyInstaller）

仓库里已带好脚本和 CI：

- **本地打包**：`pip install pyinstaller` 后双击 `build_exe.bat`（或手动执行里面的 pyinstaller 命令），产物在 `dist/LivePhotoMaker.exe`（约 49 MB，ffmpeg 二进制已内置，无需安装任何东西）
- **自动发布**：推一个 `v*` tag（如 `git tag v2.1.0 && git push origin v2.1.0`），GitHub Actions 会自动构建、冒烟测试、把 exe 挂到 Release

exe 行为说明：无参数双击 = 图形界面；在终端里带参数运行 = 命令行模式（会自动附加到当前控制台显示输出）；转换结果默认输出到 **exe 所在目录的 `output/`** 下。

## License

MIT，见 [LICENSE](LICENSE)。ffmpeg 二进制（GPL/LGPL 组件）由 [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) 分发，遵循其许可。
