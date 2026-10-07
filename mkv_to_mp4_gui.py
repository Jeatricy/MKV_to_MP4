"""MKV -> MP4 批量转换器

功能：
1. 递归扫描所选文件夹及其子文件夹中的 .mkv 文件。
2. 在原文件旁生成同名 .mp4 文件，保持原有目录结构。
3. 可选：转换成功后删除原始 .mkv 文件。
4. 默认仅转换容器（remux），不重新编码视频和音频；也可以切换为 H.264 + AAC 重编码。

运行：
    python mkv_to_mp4_gui.py

依赖：
    Python 3.8+（自带 tkinter）
    FFmpeg（把 ffmpeg.exe 放入 PATH，或在界面中选择 ffmpeg.exe）
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "MKV → MP4 批量转换器"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REMUX_MODE = "remux"
TRANSCODE_MODE = "transcode"
REMUX_LABEL = "仅转换容器（不重新编码）"
TRANSCODE_LABEL = "重新编码为 H.264 + AAC"

# 这些字幕通常可以由 FFmpeg 转成 MP4 的 mov_text。图形字幕（如 PGS、VobSub）
# 不加入 MP4，避免因为一个不兼容字幕轨导致整个视频转换失败。
TEXT_SUBTITLE_CODECS = {
    "ass",
    "jacosub",
    "microdvd",
    "mpl2",
    "mov_text",
    "realtext",
    "sami",
    "srt",
    "ssa",
    "stl",
    "subrip",
    "subviewer",
    "subviewer1",
    "text",
    "vplayer",
    "webvtt",
}


@dataclass
class ConversionSettings:
    folder: Path
    ffmpeg: str
    ffprobe: Optional[str]
    delete_source: bool
    overwrite: bool
    keep_subtitles: bool
    crf: int
    preset: str
    mode: str = REMUX_MODE


@dataclass
class RunSummary:
    total: int = 0
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    delete_failed: int = 0
    stopped: bool = False
    elapsed_seconds: float = 0.0


def clean_path_text(value: str) -> str:
    """清理从输入框或拖放操作中读到的路径。"""

    return value.strip().strip('"')


def executable_from_text(value: str, program_name: str) -> Optional[str]:
    """根据输入框内容或 PATH 查找可执行文件。"""

    text = clean_path_text(value)
    if text:
        candidate = Path(text).expanduser()
        if candidate.is_file():
            return str(candidate)
        from_path = shutil.which(text)
        if from_path:
            return from_path

    return shutil.which(program_name)


def initial_ffmpeg_path() -> str:
    """优先查找 PATH，再查找脚本同目录和当前目录。"""

    found = shutil.which("ffmpeg")
    if found:
        return found

    candidates = [
        Path(__file__).resolve().parent / "ffmpeg.exe",
        Path.cwd() / "ffmpeg.exe",
        Path(__file__).resolve().parent / "ffmpeg",
        Path.cwd() / "ffmpeg",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    # WinGet 安装后，已经打开的桌面进程可能还没有刷新 PATH；直接检查
    # WinGet 的 FFmpeg 包目录，保证程序仍能自动找到刚安装的版本。
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        try:
            package_dirs = sorted(winget_root.glob("Gyan.FFmpeg*"), key=lambda item: str(item).casefold())
            for package_dir in package_dirs:
                if not package_dir.is_dir():
                    continue
                for candidate in package_dir.rglob("ffmpeg.exe"):
                    if candidate.is_file():
                        return str(candidate)
        except OSError:
            pass
    return ""


def sibling_ffprobe(ffmpeg_path: str) -> Optional[str]:
    """查找与 ffmpeg.exe 放在同一目录的 ffprobe。"""

    try:
        ffmpeg_file = Path(ffmpeg_path)
        candidate_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        candidate = ffmpeg_file.with_name(candidate_name)
        if candidate.is_file():
            return str(candidate)
    except (OSError, ValueError):
        pass
    return shutil.which("ffprobe")


def run_quiet(command: Sequence[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """运行不需要实时显示输出的探测命令。"""

    return subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def probe_duration(ffprobe: Optional[str], source: Path) -> Optional[float]:
    """返回媒体时长（秒）；探测失败时返回 None。"""

    if not ffprobe:
        return None
    try:
        result = run_quiet(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            timeout=30,
        )
        value = result.stdout.strip()
        duration = float(value)
        return duration if duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def probe_text_subtitle_indices(
    ffprobe: Optional[str], source: Path
) -> Optional[List[int]]:
    """返回可尝试转为 mov_text 的字幕流索引。

    返回 None 表示没有 ffprobe 或探测失败；返回空列表表示探测成功但没有
    可兼容字幕。这样 UI 可以明确提示用户是否跳过了字幕。
    """

    if not ffprobe:
        return None
    try:
        result = run_quiet(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name",
                "-of",
                "json",
                str(source),
            ],
            timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        indices: List[int] = []
        for stream in data.get("streams", []):
            if stream.get("codec_type") != "subtitle":
                continue
            codec = str(stream.get("codec_name") or "").lower()
            if codec in TEXT_SUBTITLE_CODECS and isinstance(stream.get("index"), int):
                indices.append(stream["index"])
        return indices
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def scan_mkv_files(folder: Path) -> List[Path]:
    """递归扫描 MKV，忽略大小写并稳定排序。"""

    files: List[Path] = []
    for root, _directories, names in os.walk(str(folder), followlinks=False):
        for name in names:
            path = Path(root) / name
            if path.is_file() and path.suffix.lower() == ".mkv":
                files.append(path)
    files.sort(key=lambda item: str(item).casefold())
    return files


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "时长未知"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class MkvToMp4App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x650")
        self.root.minsize(780, 540)

        self.message_queue: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.current_process: Optional[subprocess.Popen] = None
        self.process_lock = threading.Lock()
        self.closing = False

        self.folder_var = tk.StringVar()
        self.ffmpeg_var = tk.StringVar(value=initial_ffmpeg_path())
        self.delete_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.subtitles_var = tk.BooleanVar(value=True)
        self.mode_var = tk.StringVar(value=REMUX_LABEL)
        self.crf_var = tk.StringVar(value="18")
        self.preset_var = tk.StringVar(value="medium")
        self.status_var = tk.StringVar(value="请选择包含 MKV 文件的文件夹")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.file_count_var = tk.StringVar(value="尚未扫描")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.drain_messages)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)

        folder_frame = ttk.LabelFrame(main, text="输入文件夹", padding=10)
        folder_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        folder_frame.columnconfigure(0, weight=1)
        ttk.Entry(folder_frame, textvariable=self.folder_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(folder_frame, text="选择文件夹…", command=self.choose_folder).grid(
            row=0, column=1
        )
        ttk.Label(
            folder_frame,
            text="MP4 将生成在每个 MKV 所在目录，文件名仅把扩展名改为 .mp4",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        ffmpeg_frame = ttk.LabelFrame(main, text="FFmpeg", padding=10)
        ffmpeg_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ffmpeg_frame.columnconfigure(0, weight=1)
        ttk.Entry(ffmpeg_frame, textvariable=self.ffmpeg_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(ffmpeg_frame, text="选择 ffmpeg.exe…", command=self.choose_ffmpeg).grid(
            row=0, column=1
        )
        ttk.Label(
            ffmpeg_frame,
            text="也可以把 FFmpeg 加入 PATH；ffprobe 会自动从同目录或 PATH 查找",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        settings_frame = ttk.LabelFrame(main, text="转换设置", padding=10)
        settings_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        settings_frame.columnconfigure(4, weight=1)

        ttk.Checkbutton(
            settings_frame,
            text="成功后删除原始 MKV（谨慎启用）",
            variable=self.delete_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=(0, 18))
        ttk.Checkbutton(
            settings_frame,
            text="覆盖已存在的 MP4",
            variable=self.overwrite_var,
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=(0, 18))
        ttk.Checkbutton(
            settings_frame,
            text="保留兼容文本字幕（可能转换字幕）",
            variable=self.subtitles_var,
        ).grid(row=0, column=4, sticky="w")

        ttk.Label(settings_frame, text="转换方式:").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        mode_box = ttk.Combobox(
            settings_frame,
            textvariable=self.mode_var,
            values=(REMUX_LABEL, TRANSCODE_LABEL),
            state="readonly",
            width=28,
        )
        mode_box.grid(row=1, column=1, columnspan=2, sticky="w", pady=(10, 0), padx=(6, 18))
        mode_box.bind("<<ComboboxSelected>>", self.update_encoding_controls)
        ttk.Label(
            settings_frame,
            text="默认不重新编码视频/音频；不兼容的流可能导致封装失败。",
            foreground="#8a4b08",
        ).grid(row=1, column=3, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(settings_frame, text="画质 CRF（越小越清晰）:").grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        self.crf_entry = ttk.Entry(settings_frame, textvariable=self.crf_var, width=7)
        self.crf_entry.grid(
            row=2, column=1, sticky="w", pady=(10, 0), padx=(6, 18)
        )
        ttk.Label(settings_frame, text="编码预设:").grid(
            row=2, column=2, sticky="w", pady=(10, 0)
        )
        self.preset_box = ttk.Combobox(
            settings_frame,
            textvariable=self.preset_var,
            values=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
            state="readonly",
            width=12,
        )
        self.preset_box.grid(row=2, column=3, sticky="w", pady=(10, 0), padx=(6, 18))
        cpu_count = os.cpu_count() or 1
        ttk.Label(
            settings_frame,
            text=f"CPU 线程：自动使用全部可用线程（-threads 0，检测到 {cpu_count} 个逻辑核心）",
            foreground="#1b5e20",
        ).grid(row=2, column=4, sticky="w", pady=(10, 0))
        self.update_encoding_controls()

        action_frame = ttk.Frame(main)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        action_frame.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(
            action_frame, text="开始转换", command=self.start_conversion
        )
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(
            action_frame, text="停止", command=self.request_stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(action_frame, text="清空日志", command=self.clear_log).grid(
            row=0, column=3
        )

        progress_frame = ttk.Frame(main)
        progress_frame.grid(row=4, column=0, sticky="nsew", pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)
        progress_frame.rowconfigure(2, weight=1)
        ttk.Label(progress_frame, textvariable=self.status_var).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        log_frame = ttk.Frame(progress_frame)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            height=16,
            bg="#fafafa",
            relief="solid",
            borderwidth=1,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        ttk.Label(main, textvariable=self.file_count_var, foreground="#555555").grid(
            row=5, column=0, sticky="w"
        )

    def update_encoding_controls(self, _event: Optional[tk.Event] = None) -> None:
        """仅重编码模式需要 CRF 和编码预设。"""

        is_transcode = self.mode_var.get() == TRANSCODE_LABEL
        self.crf_entry.configure(state="normal" if is_transcode else "disabled")
        self.preset_box.configure(state="readonly" if is_transcode else "disabled")

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择包含 MKV 文件的文件夹")
        if selected:
            self.folder_var.set(selected)

    def choose_ffmpeg(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            filetypes=[
                ("FFmpeg 可执行文件", "ffmpeg.exe"),
                ("所有文件", "*.*"),
            ],
        )
        if selected:
            self.ffmpeg_var.set(selected)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def validate_settings(self) -> Optional[ConversionSettings]:
        folder_text = clean_path_text(self.folder_var.get())
        if not folder_text:
            messagebox.showerror(APP_TITLE, "请先选择一个输入文件夹。")
            return None
        folder = Path(folder_text).expanduser()
        if not folder.is_dir():
            messagebox.showerror(APP_TITLE, "请选择一个存在的文件夹。")
            return None

        ffmpeg = executable_from_text(self.ffmpeg_var.get(), "ffmpeg")
        if not ffmpeg:
            messagebox.showerror(
                APP_TITLE,
                "找不到 FFmpeg。请先安装 FFmpeg，或点击“选择 ffmpeg.exe…”指定文件。",
            )
            return None
        self.ffmpeg_var.set(ffmpeg)

        try:
            crf = int(self.crf_var.get().strip())
        except ValueError:
            messagebox.showerror(APP_TITLE, "CRF 必须是 0 到 51 之间的整数。")
            return None
        if not 0 <= crf <= 51:
            messagebox.showerror(APP_TITLE, "CRF 必须是 0 到 51 之间的整数。")
            return None

        preset = self.preset_var.get().strip()
        allowed_presets = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"}
        if preset not in allowed_presets:
            messagebox.showerror(APP_TITLE, "请选择有效的编码预设。")
            return None

        mode_label = self.mode_var.get().strip()
        if mode_label == REMUX_LABEL:
            mode = REMUX_MODE
        elif mode_label == TRANSCODE_LABEL:
            mode = TRANSCODE_MODE
        else:
            messagebox.showerror(APP_TITLE, "请选择有效的转换方式。")
            return None

        return ConversionSettings(
            folder=folder,
            ffmpeg=ffmpeg,
            ffprobe=sibling_ffprobe(ffmpeg),
            delete_source=self.delete_var.get(),
            overwrite=self.overwrite_var.get(),
            keep_subtitles=self.subtitles_var.get(),
            crf=crf,
            preset=preset,
            mode=mode,
        )

    def set_running_state(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def start_conversion(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        settings = self.validate_settings()
        if settings is None:
            return

        self.clear_log()
        self.stop_event.clear()
        self.progress_var.set(0)
        self.file_count_var.set("正在扫描 MKV 文件…")
        self.status_var.set("准备开始")
        self.set_running_state(True)
        self.append_log(f"输入目录：{settings.folder}")
        self.append_log(f"FFmpeg：{settings.ffmpeg}")
        if settings.mode == REMUX_MODE:
            self.append_log("转换方式：仅转换容器；视频和音频均直接复制，不重新编码。")
        else:
            self.append_log("转换方式：H.264（libx264）+ AAC 重编码；CPU 线程：自动（-threads 0）")
        if settings.delete_source:
            self.append_log("注意：仅在对应 MP4 成功生成后删除 MKV。")
        if not settings.ffprobe:
            self.append_log("提示：未找到 ffprobe，无法读取精确进度；可转换文本字幕将被跳过。")

        self.worker = threading.Thread(
            target=self.run_conversion,
            args=(settings,),
            name="mkv-to-mp4-worker",
            daemon=True,
        )
        self.worker.start()

    def request_stop(self) -> None:
        if not self.worker or not self.worker.is_alive():
            return
        self.stop_event.set()
        self.status_var.set("正在停止当前任务…")
        self.append_log("已请求停止；当前 FFmpeg 任务结束后将停止。")
        with self.process_lock:
            process = self.current_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def set_current_process(self, process: Optional[subprocess.Popen]) -> None:
        with self.process_lock:
            self.current_process = process

    def make_ffmpeg_command(
        self,
        settings: ConversionSettings,
        source: Path,
        part_path: Path,
        subtitle_indices: Optional[List[int]],
    ) -> List[str]:
        command = [
            settings.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(source),
            # 仅选择视频、音频和可兼容的字幕，避免把 MKV 附件等流写入 MP4。
            "-map",
            "0:v?",
            "-map",
            "0:a?",
        ]
        if settings.keep_subtitles and subtitle_indices:
            for stream_index in subtitle_indices:
                command.extend(["-map", f"0:{stream_index}"])

        command.extend(["-map_metadata", "0", "-map_chapters", "0"])

        if settings.mode == REMUX_MODE:
            # 视频和音频流直接复制，真正做到不重新编码、不损失画质。
            command.extend(["-c:v", "copy", "-c:a", "copy"])
            if settings.keep_subtitles and subtitle_indices:
                # MP4 通常需要 mov_text；这里只转换字幕，视频/音频仍然 copy。
                command.extend(["-c:s", "mov_text"])
            thread_args = ["-threads", "0"]
        else:
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    settings.preset,
                    "-crf",
                    str(settings.crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-c:s",
                    "mov_text",
                ]
            )
            # 0 = FFmpeg 自动选择线程数，允许使用全部可用 CPU 线程。
            thread_args = [
                "-threads",
                "0",
                "-filter_threads",
                "0",
                "-filter_complex_threads",
                "0",
            ]

        command.extend(
            thread_args
            + [
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                str(part_path),
            ]
        )
        return command

    def convert_one(
        self,
        settings: ConversionSettings,
        source: Path,
        target: Path,
        duration: Optional[float],
        subtitle_indices: Optional[List[int]],
    ) -> Tuple[str, str]:
        """转换单个文件，返回 (状态, 详情)。"""

        part_path = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.part.mp4")
        command = self.make_ffmpeg_command(settings, source, part_path, subtitle_indices)
        last_output: List[str] = []
        success = False
        process: Optional[subprocess.Popen] = None

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            self.set_current_process(process)

            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue

                # -progress pipe:1 的 out_time_ms 实际单位是微秒。
                if line.startswith("out_time_ms="):
                    try:
                        time_us = int(line.split("=", 1)[1])
                        if duration and duration > 0:
                            percent = min(100.0, max(0.0, time_us / 1_000_000 / duration * 100))
                            self.message_queue.put(("progress", percent))
                    except ValueError:
                        pass
                elif line.startswith("out_time_us="):
                    try:
                        time_us = int(line.split("=", 1)[1])
                        if duration and duration > 0:
                            percent = min(100.0, max(0.0, time_us / 1_000_000 / duration * 100))
                            self.message_queue.put(("progress", percent))
                    except ValueError:
                        pass
                elif line.startswith("progress="):
                    continue
                else:
                    last_output.append(line)
                    if len(last_output) > 12:
                        last_output.pop(0)
                    self.message_queue.put(("log", f"  FFmpeg：{line}"))

            return_code = process.wait()
            self.set_current_process(None)

            if self.stop_event.is_set():
                return "stopped", "用户停止"
            if return_code != 0:
                detail = "；".join(last_output[-3:]) or f"FFmpeg 返回代码 {return_code}"
                return "failed", detail
            if not part_path.is_file() or part_path.stat().st_size == 0:
                return "failed", "FFmpeg 未生成有效的临时 MP4 文件"

            if target.exists() and not settings.overwrite:
                return "failed", "转换期间发现目标 MP4 已存在，未覆盖"
            part_path.replace(target)
            success = True
            return "success", ""
        except (OSError, subprocess.SubprocessError) as exc:
            if self.stop_event.is_set():
                return "stopped", "用户停止"
            return "failed", str(exc)
        finally:
            self.set_current_process(None)
            if not success:
                try:
                    if part_path.exists():
                        part_path.unlink()
                except OSError:
                    self.message_queue.put(("log", f"  警告：无法清理临时文件 {part_path}"))

    def run_conversion(self, settings: ConversionSettings) -> None:
        started_at = time.monotonic()
        summary = RunSummary()

        try:
            self.message_queue.put(("log", "开始递归扫描…"))
            files = scan_mkv_files(settings.folder)
            summary.total = len(files)
            self.message_queue.put(("scan_done", len(files)))

            if not files:
                summary.elapsed_seconds = time.monotonic() - started_at
                self.message_queue.put(("finished", summary))
                return

            for index, source in enumerate(files, start=1):
                if self.stop_event.is_set():
                    summary.stopped = True
                    break

                target = source.with_suffix(".mp4")
                if target.exists() and not settings.overwrite:
                    summary.skipped += 1
                    self.message_queue.put(("skipped", index, len(files), source, target))
                    continue

                duration = probe_duration(settings.ffprobe, source)
                subtitle_indices: Optional[List[int]] = []
                if settings.keep_subtitles:
                    subtitle_indices = probe_text_subtitle_indices(settings.ffprobe, source)
                    if subtitle_indices is None:
                        self.message_queue.put(
                            ("log", f"  提示：{source.name} 无法探测字幕，将跳过字幕流。")
                        )
                    elif subtitle_indices:
                        self.message_queue.put(
                            ("log", f"  保留 {len(subtitle_indices)} 条文本字幕。")
                        )

                self.message_queue.put(("file_start", index, len(files), source, duration))
                state, detail = self.convert_one(
                    settings, source, target, duration, subtitle_indices
                )

                if state == "success":
                    summary.converted += 1
                    deleted_text = ""
                    if settings.delete_source:
                        try:
                            source.unlink()
                            summary.deleted += 1
                            deleted_text = "，已删除源文件"
                        except OSError as exc:
                            summary.delete_failed += 1
                            deleted_text = f"，但删除源文件失败：{exc}"
                    self.message_queue.put(("converted", index, len(files), source, target, deleted_text))
                elif state == "stopped":
                    summary.stopped = True
                    self.message_queue.put(("stopped_file", index, len(files), source))
                    break
                else:
                    summary.failed += 1
                    self.message_queue.put(("failed", index, len(files), source, detail))

            if self.stop_event.is_set():
                summary.stopped = True
            summary.elapsed_seconds = time.monotonic() - started_at
            self.message_queue.put(("finished", summary))
        except Exception as exc:  # 防止后台线程静默退出，错误会显示在 UI 日志中。
            summary.elapsed_seconds = time.monotonic() - started_at
            self.message_queue.put(("worker_error", str(exc)))
            self.message_queue.put(("finished", summary))

    def drain_messages(self) -> None:
        try:
            while True:
                message = self.message_queue.get_nowait()
                kind = message[0]

                if kind == "log":
                    self.append_log(str(message[1]))
                elif kind == "scan_done":
                    total = int(message[1])
                    self.file_count_var.set(f"找到 {total} 个 MKV 文件")
                    if total == 0:
                        self.status_var.set("没有找到 MKV 文件")
                    else:
                        self.status_var.set(f"准备转换，共 {total} 个文件")
                elif kind == "file_start":
                    index, total, source, duration = message[1:]
                    self.current_file_index = int(index)
                    self.current_total = int(total)
                    self.status_var.set(
                        f"正在转换：{source.name}（{format_duration(duration)}）"
                    )
                    self.append_log(f"开始：{source}")
                elif kind == "progress":
                    current_percent = float(message[1])
                    # 当前文件的索引由状态文本承载；进度条在 file_done/skip 时推进，
                    # 这里使用当前文件比例近似更新，具体整体比例由 file_start 记录。
                    if hasattr(self, "current_file_index") and hasattr(self, "current_total"):
                        overall = (
                            (self.current_file_index - 1) + current_percent / 100.0
                        ) / max(1, self.current_total) * 100
                        self.progress_var.set(min(100.0, overall))
                elif kind == "converted":
                    index, total, source, target, deleted_text = message[1:]
                    self.current_file_index = int(index)
                    self.current_total = int(total)
                    self.progress_var.set(index / total * 100)
                    self.append_log(f"完成：{target}{deleted_text}")
                    self.file_count_var.set(f"进度：{index}/{total}，已完成 {index} 个" )
                elif kind == "skipped":
                    index, total, source, target = message[1:]
                    self.current_file_index = int(index)
                    self.current_total = int(total)
                    self.progress_var.set(index / total * 100)
                    self.append_log(f"跳过：{source}（目标已存在：{target}）")
                    self.file_count_var.set(f"进度：{index}/{total}，已处理 {index} 个")
                elif kind == "failed":
                    index, total, source, detail = message[1:]
                    self.current_file_index = int(index)
                    self.current_total = int(total)
                    self.progress_var.set(index / total * 100)
                    self.append_log(f"失败：{source}；原因：{detail}")
                    self.file_count_var.set(f"进度：{index}/{total}，已处理 {index} 个")
                elif kind == "stopped_file":
                    index, total, source = message[1:]
                    self.append_log(f"已停止：{source}")
                    self.file_count_var.set(f"在第 {index}/{total} 个文件处停止")
                elif kind == "worker_error":
                    self.append_log(f"后台错误：{message[1]}")
                elif kind == "finished":
                    self.handle_finished(message[1])
        except queue.Empty:
            pass
        finally:
            if not self.closing:
                self.root.after(100, self.drain_messages)

    def handle_finished(self, summary: RunSummary) -> None:
        self.set_running_state(False)
        if summary.stopped:
            self.status_var.set("已停止")
        elif summary.failed:
            self.status_var.set("转换完成，但有失败项目")
        else:
            self.status_var.set("全部任务处理完成")

        self.progress_var.set(100 if summary.total and not summary.stopped else self.progress_var.get())
        self.file_count_var.set(
            f"总计 {summary.total} 个：成功 {summary.converted}，跳过 {summary.skipped}，"
            f"失败 {summary.failed}，删除源文件 {summary.deleted}"
        )
        self.append_log(
            "汇总："
            f"成功 {summary.converted}，跳过 {summary.skipped}，失败 {summary.failed}，"
            f"删除 {summary.deleted}，删除失败 {summary.delete_failed}；"
            f"耗时 {summary.elapsed_seconds:.1f} 秒。"
        )

        if self.closing:
            return
        if summary.stopped:
            messagebox.showinfo(APP_TITLE, "转换已停止。已完成的 MP4 会保留，当前未完成文件不会删除源文件。")
        elif summary.failed:
            messagebox.showwarning(
                APP_TITLE,
                f"处理完成：成功 {summary.converted} 个，跳过 {summary.skipped} 个，失败 {summary.failed} 个。\n请查看日志。",
            )
        else:
            messagebox.showinfo(APP_TITLE, f"全部处理完成，共成功转换 {summary.converted} 个文件。")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "转换正在进行，确定要停止并退出吗？"):
                return
            self.closing = True
            self.request_stop()
            self.root.after(100, self.wait_for_close)
            return
        self.root.destroy()

    def wait_for_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self.wait_for_close)
        else:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        root.iconname(APP_TITLE)
    except tk.TclError:
        pass
    app = MkvToMp4App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
