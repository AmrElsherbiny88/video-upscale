from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Deque, List, Optional, Tuple


RESOLUTIONS = {
    "1080p (max 1920x1080)": (1920, 1080),
    "2K / QHD (max 2560x1440)": (2560, 1440),
    "4K UHD (max 3840x2160)": (3840, 2160),
}

AI_MODELS = {
    "Real-ESRGAN Plus (general footage)": "realesrgan-plus",
    "Real-ESRGAN Anime Video v3 (animation)": "realesr-animevideov3",
}


def _video2x_settings_file() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base_dir = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return base_dir / "AI Media Upscaler" / "video2x_path.txt"


class _ConversionCancelled(Exception):
    pass


def _parse_ffmpeg_time(value: str) -> Optional[float]:
    parts = value.split(":")
    if len(parts) != 3:
        return None

    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None


def _probe_duration(
    ffprobe_path: Optional[str], input_path: Path
) -> Tuple[Optional[float], Optional[str]]:
    if ffprobe_path is None:
        return None, "ffprobe was not found; progress will be indeterminate."

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,duration",
        "-of",
        "json",
        str(input_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out; progress will be indeterminate."
    except OSError as exc:
        return None, f"Could not run ffprobe ({exc}); progress will be indeterminate."

    if result.returncode != 0:
        detail = result.stderr.strip() or "No duration information was returned."
        return None, f"Could not read duration; progress will be indeterminate. {detail[:240]}"

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe returned invalid metadata; progress will be indeterminate."

    candidates = []
    format_info = metadata.get("format")
    if isinstance(format_info, dict):
        candidates.append(format_info.get("duration"))

    streams = metadata.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                candidates.append(stream.get("duration"))

    for candidate in candidates:
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration, None

    return None, "Video duration was unavailable; progress will be indeterminate."


def _probe_video_dimensions(
    ffprobe_path: Optional[str], video_path: Path
) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    if ffprobe_path is None:
        return None, "ffprobe was not found."

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out."
    except OSError as exc:
        return None, f"Could not run ffprobe: {exc}"

    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe could not read the file."
        return None, detail[:240]

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe returned invalid metadata."

    streams = metadata.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            try:
                width = int(stream.get("width"))
                height = int(stream.get("height"))
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                return (width, height), None

    return None, "No valid video stream was found."


class VideoUpscalerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AI Media Upscaler")
        self.root.geometry("840x640")
        self.root.minsize(720, 570)
        icon_path = Path(__file__).resolve().parent / "assets" / "media_upscaler.ico"
        if icon_path.is_file():
            self.root.iconbitmap(str(icon_path))

        self.mode_var = tk.StringVar(value="Video")
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self._video2x_path_file = _video2x_settings_file()
        saved_video2x_path = self._load_video2x_path()
        self.video2x_var = tk.StringVar(
            value=saved_video2x_path or shutil.which("video2x") or ""
        )
        self._last_saved_video2x_path = self.video2x_var.get().strip()
        self.ai_model_var = tk.StringVar(value=next(iter(AI_MODELS)))
        self.resolution_var = tk.StringVar(value=next(iter(RESOLUTIONS)))
        self.status_var = tk.StringVar(value="Choose a mode and input file to get started.")

        self._events = queue.Queue()
        self._process_lock = threading.Lock()
        self._process = None
        self._cancel_requested = threading.Event()
        self._busy = False
        self._closed = False
        self._close_when_done = False
        self._output_is_default = True
        self._setting_output = False

        self.output_var.trace_add("write", self._on_output_changed)
        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    def _load_video2x_path(self) -> Optional[str]:
        try:
            saved_path = self._video2x_path_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            messagebox.showerror(
                "Could not load Video2X path",
                f"Could not read the saved Video2X path from "
                f"{self._video2x_path_file}:\n{exc}",
                parent=self.root,
            )
            return None
        return saved_path or None

    def _save_video2x_path(
        self, _event: Optional[tk.Event] = None, force: bool = False
    ) -> None:
        selected_path = self.video2x_var.get().strip()
        if not force and selected_path == self._last_saved_video2x_path:
            return

        try:
            self._video2x_path_file.parent.mkdir(parents=True, exist_ok=True)
            self._video2x_path_file.write_text(selected_path, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            messagebox.showerror(
                "Could not save Video2X path",
                f"Could not save the Video2X path to "
                f"{self._video2x_path_file}:\n{exc}",
                parent=self.root,
            )
            return

        self._last_saved_video2x_path = selected_path

    def _build_widgets(self) -> None:
        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, columnspan=3, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="AI Media Upscaler", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.info_button = ttk.Button(
            header, text="ⓘ Instructions", command=self._show_instructions
        )
        self.info_button.grid(row=0, column=1, sticky="e")
        self.subtitle_label = ttk.Label(
            main, text="AI-enhance videos or images with Real-ESRGAN."
        )
        self.subtitle_label.grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(3, 16)
        )

        ttk.Label(main, text="Mode").grid(row=2, column=0, sticky="w", pady=6)
        self.mode_combo = ttk.Combobox(
            main,
            textvariable=self.mode_var,
            values=("Video", "Image"),
            state="readonly",
        )
        self.mode_combo.grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6
        )
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_selected)

        self.input_label = ttk.Label(main, text="Input video")
        self.input_label.grid(row=3, column=0, sticky="w", pady=6)
        self.input_entry = ttk.Entry(main, textvariable=self.input_var)
        self.input_entry.grid(row=3, column=1, sticky="ew", padx=(12, 8), pady=6)
        self.input_button = ttk.Button(
            main, text="Browse...", command=self._browse_input
        )
        self.input_button.grid(row=3, column=2, sticky="ew", pady=6)

        ttk.Label(main, text="Save as").grid(row=4, column=0, sticky="w", pady=6)
        self.output_entry = ttk.Entry(main, textvariable=self.output_var)
        self.output_entry.grid(row=4, column=1, sticky="ew", padx=(12, 8), pady=6)
        self.output_button = ttk.Button(
            main, text="Browse...", command=self._browse_output
        )
        self.output_button.grid(row=4, column=2, sticky="ew", pady=6)

        ttk.Label(main, text="Video2X executable").grid(
            row=5, column=0, sticky="w", pady=6
        )
        self.video2x_entry = ttk.Entry(main, textvariable=self.video2x_var)
        self.video2x_entry.grid(
            row=5, column=1, sticky="ew", padx=(12, 8), pady=6
        )
        self.video2x_entry.bind("<FocusOut>", self._save_video2x_path)
        self.video2x_button = ttk.Button(
            main, text="Browse...", command=self._browse_video2x
        )
        self.video2x_button.grid(row=5, column=2, sticky="ew", pady=6)

        ttk.Label(main, text="AI model").grid(row=6, column=0, sticky="w", pady=6)
        self.ai_model_combo = ttk.Combobox(
            main,
            textvariable=self.ai_model_var,
            values=list(AI_MODELS),
            state="readonly",
        )
        self.ai_model_combo.grid(
            row=6, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6
        )

        ttk.Label(main, text="Maximum output size").grid(
            row=7, column=0, sticky="w", pady=6
        )
        self.resolution_combo = ttk.Combobox(
            main,
            textvariable=self.resolution_var,
            values=list(RESOLUTIONS),
            state="readonly",
        )
        self.resolution_combo.grid(
            row=7, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6
        )
        self.resolution_combo.bind(
            "<<ComboboxSelected>>", self._on_resolution_selected
        )

        self.description_label = ttk.Label(main, wraplength=740)
        self.description_label.grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(12, 18)
        )

        self.progress = ttk.Progressbar(
            main, mode="indeterminate", maximum=100
        )
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(main, textvariable=self.status_var, wraplength=690).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(0, 18)
        )

        buttons = ttk.Frame(main)
        buttons.grid(row=11, column=0, columnspan=3, sticky="e")
        self.new_window_button = ttk.Button(
            buttons, text="New window", command=self._open_new_window
        )
        self.new_window_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(
            buttons,
            text="Cancel",
            command=self._cancel_conversion,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(
            buttons, text="Upscale video", command=self._start_conversion
        )
        self.start_button.pack(side="left")

        footer = ttk.Frame(main)
        footer.grid(row=12, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Label(footer, text="Made by ").pack(side="left")
        author_link = ttk.Label(
            footer,
            text="Amr Elsherbiny",
            foreground="#0A66C2",
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
            takefocus=True,
        )
        author_link.pack(side="left")
        author_link.bind("<Button-1>", self._open_linkedin)
        author_link.bind("<Return>", self._open_linkedin)
        author_link.bind("<space>", self._open_linkedin)
        self._update_mode_ui()

    def _open_linkedin(self, _event: tk.Event) -> str:
        if not webbrowser.open("https://www.linkedin.com/in/amr-elsherbiny-197a63277"):
            messagebox.showerror(
                "Could not open LinkedIn",
                "Could not open the LinkedIn profile in a web browser.",
            )
        return "break"

    def _update_mode_ui(self) -> None:
        if self.mode_var.get() == "Image":
            self.input_label.configure(text="Input image")
            self.description_label.configure(
                text=(
                    "Real-ESRGAN AI enhances the image at 4x; the PNG output "
                    "fits within the selected maximum size and preserves its "
                    "original proportions. Transparent backgrounds may be flattened."
                )
            )
            self.start_button.configure(text="Upscale image")
        else:
            self.input_label.configure(text="Input video")
            self.description_label.configure(
                text=(
                    "Real-ESRGAN AI enhances video frames at 4x; the MP4 output "
                    "fits within the selected maximum size and preserves its "
                    "original proportions."
                )
            )
            self.start_button.configure(text="Upscale video")

    def _on_mode_selected(self, event: tk.Event) -> None:
        if event.widget is self.mode_combo:
            self.input_var.set("")
            self._set_output(None, is_default=True)
            self._update_mode_ui()

    def _show_instructions(self) -> None:
        instructions = (
            "How to use AI Media Upscaler\n\n"
            "1. Choose Video or Image mode.\n"
            "2. Browse to the input file and choose where to save the result.\n"
            "3. Select video2x.exe from your Video2X installation folder. "
            "The app remembers this path between launches.\n"
            "4. Choose the general-footage or animation model and a maximum size.\n"
            "5. Click Upscale. Use New window to process another file in parallel.\n\n"
            "Requirements: Video2X 6.x, FFmpeg and ffprobe on PATH, and a "
            "Vulkan-capable graphics driver. Image mode saves PNG files; "
            "transparent input may be flattened.\n\n"
            "To install this app and add its desktop icon, run install.bat from "
            "the app folder. Parallel jobs share GPU memory."
        )
        messagebox.showinfo("Instructions", instructions, parent=self.root)

    def _on_output_changed(self, *trace_args: str) -> None:
        if trace_args and not self._setting_output:
            self._output_is_default = False

    def _set_output(self, path: Optional[Path], is_default: bool) -> None:
        self._setting_output = True
        self.output_var.set(str(path) if path is not None else "")
        self._setting_output = False
        self._output_is_default = is_default

    def _set_default_output(self, input_path: Path) -> None:
        label = self.resolution_var.get()
        resolution_tag = label.split(" ", 1)[0].lower()
        extension = ".mp4" if self.mode_var.get() == "Video" else ".png"
        output_path = input_path.with_name(
            f"{input_path.stem}_{resolution_tag}{extension}"
        )
        self._set_output(output_path, is_default=True)

    def _on_resolution_selected(self, event: tk.Event) -> None:
        if event.widget is self.resolution_combo:
            input_name = self.input_var.get()
            if self._output_is_default and input_name:
                self._set_default_output(Path(input_name))

    def _browse_input(self) -> None:
        if self.mode_var.get() == "Video":
            title = "Choose a video"
            filetypes = [
                (
                    "Video files",
                    "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.wmv",
                ),
                ("All files", "*.*"),
            ]
        else:
            title = "Choose an image"
            filetypes = [
                (
                    "Image files",
                    "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.avif",
                ),
                ("All files", "*.*"),
            ]

        selected = filedialog.askopenfilename(
            parent=self.root, title=title, filetypes=filetypes
        )
        if selected:
            self.input_var.set(selected)
            self._set_default_output(Path(selected))

    def _browse_output(self) -> None:
        current_output = self.output_var.get()
        if self.mode_var.get() == "Video":
            options = {
                "parent": self.root,
                "title": "Save upscaled video",
                "defaultextension": ".mp4",
                "filetypes": [("MP4 video", "*.mp4"), ("All files", "*.*")],
            }
        else:
            options = {
                "parent": self.root,
                "title": "Save upscaled image",
                "defaultextension": ".png",
                "filetypes": [("PNG image", "*.png"), ("All files", "*.*")],
            }
        if current_output:
            current_path = Path(current_output)
            options["initialdir"] = str(current_path.parent)
            options["initialfile"] = current_path.name

        selected = filedialog.asksaveasfilename(**options)
        if selected:
            self._set_output(Path(selected), is_default=False)

    def _browse_video2x(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Choose video2x.exe",
            filetypes=[
                ("Video2X executable", "video2x.exe"),
                ("Executable files", "*.exe"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            self.video2x_var.set(selected)
            self._save_video2x_path(force=True)

    def _open_new_window(self) -> None:
        script_path = Path(__file__).resolve()
        model_name = AI_MODELS.get(self.ai_model_var.get())
        if model_name is None:
            messagebox.showerror(
                "AI model required",
                "Choose a Real-ESRGAN AI model before opening another window.",
                parent=self.root,
            )
            return

        command = [
            sys.executable,
            str(script_path),
            "--video2x",
            self.video2x_var.get().strip(),
            "--model",
            model_name,
            "--mode",
            self.mode_var.get(),
            "--resolution",
            self.resolution_var.get(),
        ]
        try:
            subprocess.Popen(
                command,
                cwd=str(script_path.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            messagebox.showerror(
                "Could not open another window", str(exc), parent=self.root
            )

    def _start_conversion(self) -> None:
        if self._busy:
            return

        mode = self.mode_var.get()
        input_path = Path(self.input_var.get()).expanduser()
        output_path = Path(self.output_var.get()).expanduser()

        if not input_path.is_file():
            messagebox.showerror(
                "Input required",
                f"Choose an existing {mode.lower()} file first.",
                parent=self.root,
            )
            return
        if not self.output_var.get().strip():
            messagebox.showerror(
                "Output required",
                "Choose where to save the output file.",
                parent=self.root,
            )
            return
        video2x_name = self.video2x_var.get().strip().strip('"')
        if not video2x_name:
            video2x_name = shutil.which("video2x") or ""
        video2x_path = (
            Path(video2x_name).expanduser() if video2x_name else None
        )
        if video2x_path is None or not video2x_path.is_file():
            messagebox.showerror(
                "Video2X required",
                "Install Video2X 6.x, then choose its video2x.exe file. "
                "See README.md for setup details.",
                parent=self.root,
            )
            return
        required_suffix = ".mp4" if mode == "Video" else ".png"
        if output_path.suffix.lower() != required_suffix:
            messagebox.showerror(
                f"{required_suffix[1:].upper()} output required",
                f"The output file name must end in {required_suffix}.",
                parent=self.root,
            )
            return
        if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(
            os.path.abspath(output_path)
        ):
            messagebox.showerror(
                "Choose a different output",
                "The output file cannot be the same as the input video.",
                parent=self.root,
            )
            return
        if not output_path.parent.is_dir():
            messagebox.showerror(
                "Output folder not found",
                "Choose an existing folder for the output video.",
                parent=self.root,
            )
            return
        if output_path.exists() and not messagebox.askyesno(
            "Replace existing file?",
            f"{output_path.name} already exists. Replace it after conversion completes?",
            parent=self.root,
        ):
            return

        resolution = RESOLUTIONS.get(self.resolution_var.get())
        if resolution is None:
            messagebox.showerror(
                "Resolution required", "Choose a target resolution.", parent=self.root
            )
            return
        model_name = AI_MODELS.get(self.ai_model_var.get())
        if model_name is None:
            messagebox.showerror(
                "AI model required", "Choose a Real-ESRGAN AI model.", parent=self.root
            )
            return

        self._cancel_requested.clear()
        self._set_busy(True)
        self.status_var.set(f"Preparing AI {mode.lower()} upscaling...")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)

        worker = threading.Thread(
            target=self._run_conversion,
            args=(mode, input_path, output_path, resolution, video2x_path, model_name),
            daemon=True,
        )
        worker.start()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.input_entry.configure(state=state)
        self.input_button.configure(state=state)
        self.output_entry.configure(state=state)
        self.output_button.configure(state=state)
        self.video2x_entry.configure(state=state)
        self.video2x_button.configure(state=state)
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        self.ai_model_combo.configure(state="disabled" if busy else "readonly")
        self.resolution_combo.configure(state="disabled" if busy else "readonly")
        self.start_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def _cancel_conversion(self) -> None:
        if not self._busy:
            return
        self._cancel_requested.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("Cancelling conversion...")
        with self._process_lock:
            process = self._process
        if process is not None:
            self._terminate_process(process)

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError as exc:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError as kill_error:
                        if process.poll() is None:
                            self._events.put(
                                (
                                    "status",
                                    f"Could not stop FFmpeg: {kill_error or exc}",
                                )
                            )

    def _run_external_command(
        self,
        command: List[str],
        duration: Optional[float] = None,
        expected_video_output: Optional[Path] = None,
        ffprobe_path: Optional[str] = None,
    ) -> Optional[str]:
        if self._cancel_requested.is_set():
            raise _ConversionCancelled()

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._process_lock:
            self._process = process
            if self._cancel_requested.is_set():
                self._terminate_process(process)

        output_tail: Deque[str] = deque(maxlen=12)
        reported_success = False
        reported_output = False
        validation_note = None
        try:
            if process.stdout is None:
                raise RuntimeError("Could not read process output.")

            with process.stdout as output_stream:
                for line in output_stream:
                    if self._cancel_requested.is_set() and process.poll() is None:
                        self._terminate_process(process)

                    line = line.strip()
                    if line.startswith("out_time="):
                        elapsed = _parse_ffmpeg_time(line.partition("=")[2])
                        if elapsed is not None and duration is not None:
                            percentage = min(99.9, max(0.0, elapsed / duration * 100))
                            self._events.put(("progress", percentage))
                    if "Video processed successfully" in line:
                            reported_success = True
                    if expected_video_output is not None and line.startswith(
                            "Output written to:"
                    ):
                            reported_path = line.partition(":")[2].strip().strip('"')
                            reported_output = os.path.normcase(
                                os.path.abspath(reported_path)
                            ) == os.path.normcase(os.path.abspath(expected_video_output))

                    if line and not line.startswith(
                            (
                                "frame=",
                            "fps=",
                            "stream_",
                            "bitrate=",
                            "total_size=",
                            "out_time",
                            "dup_frames=",
                            "drop_frames=",
                            "speed=",
                            "progress=",
                        )
                    ):
                        output_tail.append(line)

            return_code = process.wait()
            if self._cancel_requested.is_set():
                raise _ConversionCancelled()
            if return_code != 0:
                if (
                    return_code == 3221225477
                    and expected_video_output is not None
                    and reported_success
                    and reported_output
                    and expected_video_output.is_file()
                    and expected_video_output.stat().st_size > 0
                ):
                    dimensions, validation_note = _probe_video_dimensions(
                        ffprobe_path, expected_video_output
                    )
                    if dimensions is not None:
                        return (
                            "Video2X reported completion and FFprobe verified "
                            f"a {dimensions[0]}x{dimensions[1]} AI output, but "
                            "the CLI then exited with Windows access-violation "
                            "code 3221225477. The verified output will be used."
                        )
                details = "\n".join(output_tail)
                message = f"External process exited with code {return_code}."
                if return_code == 3221225477 and expected_video_output is not None:
                    message = (
                        "Video2X exited with Windows access-violation code "
                        "3221225477, and its output could not be verified."
                    )
                    if validation_note:
                        message = f"{message} {validation_note}"
                if details:
                    message = f"{message}\n\n{details}"
                raise RuntimeError(message)
            return None
        finally:
            if process.poll() is None:
                self._terminate_process(process)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            with self._process_lock:
                if self._process is process:
                    self._process = None

    def _run_conversion(
        self,
        mode: str,
        input_path: Path,
        output_path: Path,
        resolution: Tuple[int, int],
        video2x_path: Path,
        model_name: str,
    ) -> None:
        temporary_directory = None
        outcome: Tuple[str, str, Optional[str]] = (
            "error",
            "Upscaling did not complete.",
            None,
        )
        video2x_warning = None

        try:
            ffmpeg_path = shutil.which("ffmpeg")
            if ffmpeg_path is None:
                raise RuntimeError(
                    "FFmpeg was not found. Install FFmpeg, add its bin folder to "
                    "PATH, and restart this app."
                )
            if self._cancel_requested.is_set():
                raise _ConversionCancelled()

            ffprobe_path = shutil.which("ffprobe")
            if mode == "Video":
                duration, progress_note = _probe_duration(
                    ffprobe_path, input_path
                )
            else:
                duration, progress_note = None, None
            temporary_directory = tempfile.TemporaryDirectory(
                prefix=".video_upscaler_", dir=str(output_path.parent)
            )
            work_directory = Path(temporary_directory.name)
            ai_output = work_directory / "ai_upscaled.mp4"
            final_extension = ".mp4" if mode == "Video" else ".png"
            final_output = work_directory / f"final_output{final_extension}"

            self._events.put(
                (
                    "progress_mode",
                    (
                        None,
                        f"Real-ESRGAN AI is processing the {mode.lower()} "
                        f"with {model_name}...",
                    ),
                )
            )
            ai_command = [
                str(video2x_path),
                "--no-progress",
                "-i",
                str(input_path),
                "-o",
                str(ai_output),
                "-p",
                "realesrgan",
                "-s",
                "4",
                "--realesrgan-model",
                model_name,
                "-c",
                "libx264",
                "-e",
                "crf=16",
                "-e",
                "preset=medium",
            ]
            video2x_warning = self._run_external_command(
                ai_command,
                expected_video_output=ai_output,
                ffprobe_path=ffprobe_path,
            )
            if not ai_output.is_file():
                raise RuntimeError(
                    "Video2X finished without creating its AI-upscaled video. "
                    "Check that the selected model is installed."
                )
            if self._cancel_requested.is_set():
                raise _ConversionCancelled()

            width, height = resolution
            filter_graph = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2:flags=lanczos,setsar=1"
            )
            if mode == "Video":
                self._events.put(
                    (
                        "progress_mode",
                        (duration, progress_note or "Encoding final video..."),
                    )
                )
                self._events.put(
                    ("status", "Resizing the AI video and preserving audio...")
                )
                ffmpeg_command = [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(ai_output),
                    "-i",
                    str(input_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a?",
                    "-map_metadata",
                    "1",
                    "-map_chapters",
                    "1",
                    "-vf",
                    filter_graph,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(final_output),
                ]
            else:
                self._events.put(
                    ("progress_mode", (None, "Saving the AI-upscaled PNG image..."))
                )
                self._events.put(("status", "Resizing and saving PNG image..."))
                ffmpeg_command = [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(ai_output),
                    "-map",
                    "0:v:0",
                    "-vf",
                    filter_graph,
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    "-compression_level",
                    "3",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    str(final_output),
                ]
            self._run_external_command(ffmpeg_command, duration)
            if self._cancel_requested.is_set():
                raise _ConversionCancelled()

            os.replace(final_output, output_path)
            outcome = ("success", str(output_path), video2x_warning)
        except _ConversionCancelled:
            outcome = ("cancelled", "Upscaling was cancelled.", None)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            outcome = ("error", str(exc), None)
        finally:
            if temporary_directory is not None:
                try:
                    temporary_directory.cleanup()
                except OSError as exc:
                    if outcome[0] == "success":
                        outcome = (
                            "error",
                            "The output was saved, but temporary files could not "
                            f"be removed: {exc}",
                            None,
                        )
                    else:
                        outcome = (
                            outcome[0],
                            f"{outcome[1]}\n\nCould not remove temporary files: {exc}",
                            outcome[2],
                        )

            self._events.put(("finish", outcome))

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break

            if event == "progress_mode":
                duration, note = payload
                self.progress.stop()
                if duration is None:
                    self.progress.configure(mode="indeterminate", value=0)
                    self.progress.start(12)
                    self.status_var.set(note or "Upscaling video...")
                else:
                    self.progress.configure(
                        mode="determinate", maximum=100, value=0
                    )
                    self.status_var.set(note or "Upscaling video...")
            elif event == "progress" and isinstance(payload, (int, float)):
                self.progress.configure(value=payload)
                self.status_var.set(f"Upscaling video... {payload:.1f}%")
            elif event == "status":
                self.status_var.set(str(payload))
            elif event == "finish":
                result, message, warning = payload
                self._finish(result, message, warning)

        if not self._closed:
            self.root.after(100, self._poll_events)

    def _finish(
        self, event: str, payload: str, warning: Optional[str] = None
    ) -> None:
        self.progress.stop()
        if event == "success":
            self.progress.configure(mode="determinate", maximum=100, value=100)
            self.status_var.set(f"Finished: {Path(payload).name}")
        else:
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.status_var.set(payload)
        self._set_busy(False)

        if self._close_when_done:
            self._closed = True
            self.root.destroy()
            return

        if event == "success":
            success_message = f"Saved the upscaled video to:\n{payload}"
            if Path(payload).suffix.lower() == ".png":
                success_message = f"Saved the upscaled image to:\n{payload}"
            if warning:
                success_message = f"{success_message}\n\nNote: {warning}"
            messagebox.showinfo(
                "Upscaling complete",
                success_message,
                parent=self.root,
            )
        elif event == "error":
            messagebox.showerror("Upscaling failed", payload, parent=self.root)
        else:
            messagebox.showinfo("Upscaling cancelled", payload, parent=self.root)

    def _on_close(self) -> None:
        self._save_video2x_path()
        if self._busy:
            should_cancel = messagebox.askyesno(
                "Conversion in progress",
                "Cancel the conversion and close the app?",
                parent=self.root,
            )
            if should_cancel:
                self._close_when_done = True
                self._cancel_conversion()
            return

        self._closed = True
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI media upscaler")
    parser.add_argument("--mode", choices=("Video", "Image"))
    parser.add_argument("--video2x", help="Path to video2x.exe")
    parser.add_argument("--model", choices=list(AI_MODELS.values()))
    parser.add_argument("--resolution", choices=list(RESOLUTIONS))
    args = parser.parse_args()

    root = tk.Tk()
    app = VideoUpscalerApp(root)
    if args.mode is not None:
        app.mode_var.set(args.mode)
        app._update_mode_ui()
    if args.video2x is not None:
        app.video2x_var.set(args.video2x)
    if args.model is not None:
        for label, model_name in AI_MODELS.items():
            if model_name == args.model:
                app.ai_model_var.set(label)
                break
    if args.resolution is not None:
        app.resolution_var.set(args.resolution)
    root.mainloop()


if __name__ == "__main__":
    main()