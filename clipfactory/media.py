import json
import math
import os
import subprocess
import tempfile
import time
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


def run(args, check_cancel=lambda: None, cwd=None, capture=False, timeout=None):
    with tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=stderr, cwd=cwd,
        ) as process:
            started = time.monotonic()
            try:
                while True:
                    check_cancel()
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise MediaError("O arquivo demorou demais para ser inspecionado.")
                    try:
                        output = process.communicate(timeout=0.5)[0]
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if process.returncode:
            stderr.seek(max(0, stderr.tell() - 1800))
            detail = stderr.read().decode(errors="replace").strip()
            raise MediaError(f"{args[0]} não conseguiu processar a mídia. {detail}")
        return output


def ffmpeg(*args):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file", *map(str, args)]


def probe(path, max_seconds):
    try:
        raw = json.loads(run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", "mov",
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
        duration = float(container["duration"])
        fps = float(Fraction(video["avg_frame_rate"]))
        width, height = video["width"], video["height"]
        rotation = next((d["rotation"] for d in video.get("side_data_list", []) if "rotation" in d), 0)
        if round(rotation) % 180 == 90:
            width, height = height, width
        if not brands or brands.strip() in {"qt", "mjp2"}:
            raise ValueError("Contêiner diferente de MP4.")
        if not (5 <= duration <= max_seconds and math.isfinite(fps) and fps > 0
                and 32 <= min(width, height) and max(width, height) <= 8192):
            raise ValueError("Duração, dimensões ou FPS fora do intervalo aceito.")
        if not video.get("codec_name") or not audio or any(not a["codec"] for a in audio):
            raise ValueError("O MP4 precisa conter vídeo e áudio com codecs reconhecidos.")
    except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as error:
        raise MediaError(f"MP4 inválido. {error}") from error
    return {"duration": duration, "width": width, "height": height, "fps": fps,
            "video_stream": video["index"], "codec": video["codec_name"],
            "sar": video.get("sample_aspect_ratio", "1:1"), "rotation": rotation,
            "bitrate": int(container.get("bit_rate") or 0), "audio": audio}


def extract_audio(source, wav, stream, check):
    run(ffmpeg("-xerror", "-f", "mov", "-i", source, "-map", f"0:{stream}",
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav), check)


def audio_signals(path, check):
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
    return timeline


def visual_signals(path, info, detect, check):
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
    args = ffmpeg("-xerror", "-threads", "2", "-f", "mov", "-i", path,
                  "-map", f"0:{info['video_stream']}", "-an", "-filter_threads", "1",
                  "-vf", f"fps=2,scale={width}:{height},format=gray",
                  "-f", "rawvideo", "-pix_fmt", "gray", "-threads", "1", "pipe:1")
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL
    ) as process:
        try:
            while block := process.stdout.read(width * height):
                check()
                if len(block) != width * height:
                    raise MediaError("Quadro incompleto durante a decodificação.")
                frame = np.frombuffer(block, np.uint8).reshape(height, width)
                delta = np.abs(frame.astype(np.int16) - previous) if previous is not None else np.zeros_like(frame)
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
                previous = frame
        except BaseException:
            process.kill()
            raise
        finally:
            process.stdout.close()
            process.wait()
        if process.returncode or not signals:
            raise MediaError("O vídeo contém quadros ilegíveis ou não possui quadros decodificáveis.")
    if len(signals) / 2 + max(1.5, info["duration"] * .02) < info["duration"]:
        raise MediaError("A trilha de vídeo termina antes da duração declarada pelo MP4.")
    return signals, faces


def atomic_json(path, value):
    from .store import dump

    temp = path.with_suffix(".tmp")
    temp.write_text(dump(value), encoding="utf-8")
    os.replace(temp, path)
