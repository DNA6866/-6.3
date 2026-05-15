# 牛爷爷电商视频工具箱 V6.3

PyQt5 桌面端电商视频工具箱，包含批量混剪、音视频处理、AI 音频克隆、AI 商品图生成、直播间解析下载等功能入口。

## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## 资源包说明

仓库只保存源码，不包含大体积运行资源。以下内容需要作为网盘资源包放到软件根目录：

- `网盘资源包/tools/ffmpeg.exe`
- `网盘资源包/tools/ffprobe.exe`
- `网盘资源包/models/SenseVoiceSmall`
- `网盘资源包/models/VoxCPM2`
- `网盘资源包/engines/ComfyUI`
- `网盘资源包/workflows`

用户运行时会自动生成 `用户工作区`，用于保存素材、输出、配置和日志。

