import json
import math
import os
import subprocess
import tempfile
import time
import wave
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ

import numpy as np


SOURCE_FORMATS = {
    ".mp4": ("video/mp4", "mov"),
    ".webm": ("video/webm", "matroska"),
}


class MediaError(RuntimeError):
    pass


def stderr_tail(file):
    file.seek(max(0, file.tell() - 1800))
    return file.read().decode(errors="replace").strip()


def run(args, check_cancel=lambda: None, cwd=None, capture=False, timeout=None, *, progress=None):
    progress_file = tempfile.TemporaryFile() if progress else nullcontext()
    with tempfile.TemporaryFile() as stderr, progress_file as timeline:
        stdout = timeline if progress else subprocess.PIPE if capture else subprocess.DEVNULL
        with subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=stdout,
            stderr=stderr, cwd=cwd,
        ) as process:
            started = time.monotonic()
            offset, pending = 0, b""
            try:
                while True:
                    check_cancel()
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise MediaError("O arquivo demorou demais para ser inspecionado.")
                    try:
                        output = process.communicate(timeout=0.5)[0]
                    except subprocess.TimeoutExpired:
                        exited = False
                    else:
                        exited = True
                    if progress:
                        # Read the live report without moving the subprocess's write offset.
                        chunk = os.pread(timeline.fileno(), 65536, offset)
                        offset += len(chunk)
                        *lines, pending = (pending + chunk).split(b"\n")
                        for line in lines:
                            key, _, value = line.partition(b"=")
                            if key == b"out_time_us" and value.isdigit():
                                progress(int(value) / 1_000_000)
                    if exited:
                        break
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if process.returncode:
            raise MediaError(f"{args[0]} não conseguiu processar a mídia. {stderr_tail(stderr)}")
        return output


def ffmpeg(*args, progress=False):
    reporting = ("-progress", "pipe:1", "-nostats") if progress else ()
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file", *reporting, *map(str, args)]


def source_input(path):
    _, demuxer = SOURCE_FORMATS[Path(path).suffix.lower()]
    return "-f", demuxer, "-i", path


def read_ebml_vint(file, *, keep_marker=False):
    first = int.from_bytes(file.read(1), "big")
    if not first:
        raise ValueError("Cabeçalho EBML inválido.")
    remaining = 8 - first.bit_length()
    tail = file.read(remaining)
    if len(tail) != remaining:
        raise ValueError("Cabeçalho EBML incompleto.")
    if not keep_marker:
        first ^= 1 << (7 - remaining)
    return (first << (8 * remaining)) | int.from_bytes(tail, "big")


def is_webm(path):
    # FFmpeg shares a demuxer for Matroska and WebM. EBML DocType distinguishes them.
    with Path(path).open("rb") as file:
        if file.read(4) != b"\x1a\x45\xdf\xa3":
            return False
        header_size = read_ebml_vint(file)
        end = file.tell() + min(header_size, 65536)
        while file.tell() < end:
            element = read_ebml_vint(file, keep_marker=True)
            size = read_ebml_vint(file)
            if file.tell() + size > end:
                return False
            if element == 0x4282:
                return size == 4 and file.read(4) == b"webm"
            file.seek(size, 1)
    return False


def webm_duration(path, video_stream, audio_stream, max_seconds):
    # Live WebM recordings can omit the segment duration. Scan packets without decoding.
    progress = run(ffmpeg("-xerror", "-progress", "pipe:1", "-nostats", "-f", "matroska", "-i", path,
                          "-map", f"0:{video_stream}", "-map", f"0:{audio_stream}", "-c", "copy",
                          "-t", max_seconds + 1, "-f", "null", "-"), capture=True, timeout=20)
    timestamp = next(line.partition(b"=")[2] for line in reversed(progress.splitlines())
                     if line.startswith(b"out_time_us="))
    return int(timestamp) / 1_000_000


def probe(path, max_seconds, *, extension=None):
    extension = extension or Path(path).suffix.lower()
    label = extension[1:].upper()
    try:
        _, demuxer = SOURCE_FORMATS[extension]
        if extension == ".webm" and not is_webm(path):
            raise ValueError("Contêiner diferente de WebM.")
        raw = json.loads(run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", demuxer,
             "-show_streams", "-show_format", "-of", "json", str(path)],
            capture=True, timeout=20,
        ))
        container = raw["format"]
        brands = container.get("tags", {}).get("major_brand", "")
        video = next(s for s in raw["streams"] if s["codec_type"] == "video"
                     and not s.get("disposition", {}).get("attached_pic"))
        audio = [{"index": s["index"], "codec": s.get("codec_name"), "channels": s.get("channels"),
                  "language": s.get("tags", {}).get("language", "und")}
                 for s in raw["streams"] if s["codec_type"] == "audio"]
        if extension == ".mp4" and (not brands or brands.strip() in {"qt", "mjp2"}):
            raise ValueError("Contêiner diferente de MP4.")
        if not video.get("codec_name") or not audio or any(not a["codec"] for a in audio):
            raise ValueError(f"O {label} precisa conter vídeo e áudio com codecs reconhecidos.")
        duration = container.get("duration")
        if extension == ".webm" and duration in {None, "N/A"}:
            duration = webm_duration(path, video["index"], audio[0]["index"], max_seconds)
        duration = float(duration)
        rate = video.get("avg_frame_rate")
        if not rate or rate in {"0/0", "0/1"}:
            rate = video["r_frame_rate"]
        fps = float(Fraction(rate))
        width, height = video["width"], video["height"]
        rotation = next((d["rotation"] for d in video.get("side_data_list", []) if "rotation" in d), 0)
        if round(rotation) % 180 == 90:
            width, height = height, width
        if not (5 <= duration <= max_seconds and math.isfinite(fps) and fps > 0
                and 32 <= min(width, height) and max(width, height) <= 8192):
            raise ValueError("Duração, dimensões ou FPS fora do intervalo aceito.")
    except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as error:
        raise MediaError(f"{label} inválido. {error}") from error
    return {"duration": duration, "width": width, "height": height, "fps": fps,
            "video_stream": video["index"], "codec": video["codec_name"],
            "sar": video.get("sample_aspect_ratio", "1:1"), "rotation": rotation,
            "bitrate": int(container.get("bit_rate") or 0), "audio": audio, "extension": extension}


def extract_audio(source, wav, stream, check, *, progress=None):
    run(ffmpeg("-xerror", "-copyts", "-start_at_zero", *source_input(source), "-map", f"0:{stream}",
               "-vn", "-ac", "1", "-af", "aresample=16000:async=1000:first_pts=0",
               "-c:a", "pcm_s16le", wav, progress=progress is not None), check, progress=progress)


def audio_signals(path, check, *, progress=None):
    timeline = []
    with wave.open(str(path), "rb") as data:
        hz = data.getframerate()
        while frame := data.readframes(hz // 2):
            if len(timeline) % 120 == 0:
                check()
            pcm = np.frombuffer(frame, "<i2").astype(np.float32) / 32768
            rms = float(np.sqrt(np.mean(pcm * pcm)))
            previous = timeline[-1]["rms"] if timeline else rms
            zcr = float(np.mean(np.diff(np.signbit(pcm)))) if len(pcm) > 1 else 0
            timeline.append({"t": len(timeline) / 2, "rms": round(rms, 5),
                             "jump": round(max(0, rms - previous), 5), "zcr": round(zcr, 4)})
            if progress:
                progress(data.tell() / hz)
    return timeline


VISUAL_IDLE_TIMEOUT = 120


def decoded_frames(stdout, size, check):
    frame = memoryview(bytearray(size))
    received = 0
    with DefaultSelector() as selector:
        selector.register(stdout, EVENT_READ)
        last_read = time.monotonic()
        while True:
            check()
            if not selector.select(timeout=.5):
                if time.monotonic() - last_read >= VISUAL_IDLE_TIMEOUT:
                    raise MediaError(f"FFmpeg ficou {VISUAL_IDLE_TIMEOUT} s sem enviar dados de vídeo.")
                continue
            count = stdout.readinto(frame[received:])
            if not count:
                if received:
                    raise MediaError("Quadro incompleto durante a decodificação.")
                return
            last_read = time.monotonic()
            received += count
            if received == size:
                yield frame
                received = 0
                last_read = time.monotonic()


def visual_signals(path, info, detect, check, *, progress=None):
    width = max(48, round(240 * min(1, info["width"] / info["height"])))
    height = max(48, round(240 * min(1, info["height"] / info["width"])))
    classifier = None
    if detect:
        import cv2

        classifier = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if classifier.empty():
            raise MediaError("O classificador facial do OpenCV está indisponível.")
    signals, faces = [], []
    previous = None
    args = ffmpeg("-xerror", "-copyts", "-start_at_zero", "-threads", "2", *source_input(path),
                  "-map", f"0:{info['video_stream']}", "-an", "-filter_threads", "1",
                  "-vf", f"fps=2:start_time=0,scale={width}:{height},format=gray",
                  "-f", "rawvideo", "-pix_fmt", "gray", "-threads", "1", "pipe:1")
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL, bufsize=0,
    ) as process:
        try:
            for block in decoded_frames(process.stdout, width * height, check):
                frame = np.frombuffer(block, np.uint8).reshape(height, width)
                current = frame.astype(np.int16)
                delta = np.abs(current - previous) if previous is not None else np.zeros_like(frame)
                tile_scores = [float(tile.mean()) for row in np.array_split(delta, 3)
                               for tile in np.array_split(row, 3, axis=1)]
                stamp = len(signals) / 2
                signals.append({"t": stamp, "motion": round(float(delta.mean()) / 255, 4),
                                "scene": float(np.mean(delta > 45)) > 0.58,
                                "focus": tile_scores.index(max(tile_scores))})
                if classifier is not None and len(signals) % 4 == 1:
                    for x, y, w, h in classifier.detectMultiScale(frame, 1.1, 4, minSize=(15, 15)):
                        if w < width * 0.35:
                            faces.append((stamp, (x + w / 2) / width, (y + h / 2) / height,
                                          w / width, h / height))
                previous = current
                if progress:
                    progress(len(signals) / 2)
            try:
                process.wait(timeout=VISUAL_IDLE_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise MediaError("FFmpeg parou de enviar dados e não encerrou a decodificação.") from error
        except MediaError as error:
            process.kill()
            process.wait()
            detail = stderr_tail(errors)
            if detail:
                raise MediaError(f"{error} Detalhes do FFmpeg · {detail}") from error
            raise
        except BaseException:
            process.kill()
            raise
        finally:
            process.stdout.close()
            process.wait()
        if process.returncode or not signals:
            detail = stderr_tail(errors)
            message = "O vídeo contém quadros ilegíveis ou não possui quadros decodificáveis."
            raise MediaError(f"{message} Detalhes do FFmpeg · {detail}" if detail else message)
    if len(signals) / 2 + max(1.5, info["duration"] * .02) < info["duration"]:
        raise MediaError("A trilha de vídeo termina antes da duração informada pelo contêiner.")
    return signals, faces
