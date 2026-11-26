# AI Media Upscaler

A Windows desktop app with two local Real-ESRGAN AI modes:

- **Video**: enhance a video and export MP4 with H.264 video and AAC audio.
- **Image**: enhance one still image and save it as PNG.

## Requirements

- Python 3.7 or newer with Tkinter
- Video2X 6.x with Real-ESRGAN support
- FFmpeg and ffprobe available on `PATH`
- A Vulkan-capable graphics driver

## Install

Double-click `install.bat`. It copies the app under `%LOCALAPPDATA%\Programs`,
then creates desktop and Start Menu shortcuts using the included
`assets\media_upscaler.ico` icon. This installer does not install Python,
Video2X, or FFmpeg.

To run without installing, open PowerShell in this folder and run:

```powershell
py app.py
```

## Use

1. Launch **AI Media Upscaler** from the desktop shortcut.
2. Click **ⓘ Instructions** for the in-app setup and usage guide.
3. Select **Video** or **Image** mode and choose an input file.
4. Browse to `video2x.exe` in the Video2X installation folder if it is not on
   `PATH`. The selected path is remembered between launches; browse again to
   change it.
5. Choose **Real-ESRGAN Plus** for general footage or **Anime Video v3** for
   animation, then select a maximum output size.
6. Click **Upscale video** or **Upscale image**.

Images are saved as PNG. Transparent input images may be flattened during AI
processing. Video2X enhances media at 4x; the app then fits it within the
selected maximum size while preserving aspect ratio, without stretching,
cropping, or adding bars. The maximum sizes are 1920x1080, 2560x1440 (2K/QHD),
and 3840x2160 (4K UHD). A 4:3 image/video at the 1080p setting therefore outputs
at 1440x1080.

Click **New window** to process multiple files concurrently. Each window has an
independent job and inherits the current mode and AI settings. There is no
fixed window limit, but simultaneous jobs share GPU resources and may slow
down or run out of GPU memory.

AI enhancement estimates plausible detail; it cannot recover the exact detail
lost in the source. 4K processing can take a long time. Temporary processing
files are created beside the output and removed when the job finishes.

## Video2X 6.4.0 note

The installed Video2X 6.4.0 CLI may exit with Windows error code `3221225477`
after reporting completion. The app continues only when Video2X reports the
expected output and ffprobe verifies it, and includes a warning in the
completion message.
