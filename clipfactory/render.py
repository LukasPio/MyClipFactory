import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .captions import subtitles
from .media import ffmpeg, run, source_input


def layouts(candidate, edit):
    start, end = edit["start"], edit["end"]
    region = candidate["webcam"]
    mode = edit["layout"]
    if not region:
        return [(start, end, "gameplay")]
    if mode != "auto":
        return [(start, end, mode)]
    if candidate["layout"] != "webcam":
        if candidate["layout"] == "split":
            return [(start, end, "split")]
        return [(start, end, "gameplay_pip")]
    left = max(start, candidate["highlight"] - 1)
    right = min(end, candidate["highlight"] + 5)
    if right <= left:
        return [(start, end, "split")]
    if left - start < 2:
        left = start
    if end - right < 2:
        right = end
    return [(a, b, style) for a, b, style in
            [(start, left, "split"), (left, right, "webcam"), (right, end, "split")] if b > a]


def music_track(settings, mood):
    library = settings.data / "music" / "library.json"
    if not library.exists() or not mood or mood == "reaction":
        return None
    for item in json.loads(library.read_text(encoding="utf-8")):
        name = item.get("file", "")
        if item.get("mood") != mood or not item.get("license") or name != Path(name).name:
            continue
        track = library.parent / name
        if track.is_file() and track.suffix.lower() in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
            return track
    return None


def filter_for_layout(index, start, end, origin, mode, region, fps, video):
    base = f"[{video}]trim=start={start-origin:.3f}:end={end-origin:.3f},setpts=PTS-STARTPTS,fps={fps},setsar=1"
    if mode == "gameplay":
        return base + (f",scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                       f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x141A21,setsar=1[v{index}]")
    crop = (f"crop=w=trunc(iw*{region['w']:.6f}/2)*2:h=trunc(ih*{region['h']:.6f}/2)*2:"
            f"x=iw*{region['x']:.6f}:y=ih*{region['y']:.6f}")
    if mode == "gameplay_pip":
        return (
            base + f",split[g{index}][f{index}];"
            f"[g{index}]scale=1080:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad=1080:1920:(ow-iw)/2:320:color=0x141A21,setsar=1[gp{index}];"
            f"[f{index}]{crop},scale=380:280:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"setsar=1[fp{index}];[gp{index}][fp{index}]overlay=48:(320-oh)/2[v{index}]"
        )
    game_h, face_h = (960, 440) if mode == "split" else (640, 760)
    return (
        base + f",split[g{index}][f{index}];"
        f"[g{index}]scale=1080:{game_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad=1080:{game_h}:(ow-iw)/2:(oh-ih)/2:color=0x141A21,setsar=1[gp{index}];"
        f"[f{index}]{crop},scale=1080:{face_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad=1080:{face_h}:(ow-iw)/2:(oh-ih)/2:color=0x141A21,setsar=1[fp{index}];"
        f"[fp{index}][gp{index}]vstack,pad=1080:1920:0:0:color=0x141A21[v{index}]"
    )


def render(source, output, info, transcript, candidate, edit, audio_stream, settings, check):
    start, end = edit["start"], edit["end"]
    duration = end - start
    fps = round(min(60, info["fps"]), 4)
    chapters = layouts(candidate, edit)
    track = music_track(settings, candidate["mood"])
    with TemporaryDirectory(prefix="mcf-render-", dir=settings.file("tmp", "placeholder").parent) as directory:
        temp = Path(directory)
        subtitles(temp / "caption.ass", transcript, edit, candidate["webcam"])
        args = ["-xerror", "-ss", f"{start:.3f}", "-threads", "2", *source_input(source)]
        if track:
            args.extend(["-stream_loop", "-1", "-i", track])
        inputs = [f"src{i}" for i in range(len(chapters))]
        video = f"0:{info['video_stream']}"
        video_filters = []
        if len(chapters) > 1:
            video_filters.append(f"[{video}]split={len(chapters)}" + "".join(f"[{label}]" for label in inputs))
        else:
            inputs = [video]
        for index, (left, right, mode) in enumerate(chapters):
            region = candidate["webcam"]
            track_points = [p for p in candidate["face_track"] if left <= p["t"] < right]
            if region and track_points:
                x = sorted(p["x"] for p in track_points)[len(track_points) // 2] - region["w"] / 2
                y = sorted(p["y"] for p in track_points)[len(track_points) // 2] - region["h"] / 2
                region = region | {"x": max(0, min(1 - region["w"], x)),
                                   "y": max(0, min(1 - region["h"], y))}
            video_filters.append(filter_for_layout(index, left, right, start, mode, region, fps, inputs[index]))
        stack = "".join(f"[v{i}]" for i in range(len(chapters)))
        if len(chapters) > 1:
            stack += f"concat=n={len(chapters)}:v=1:a=0,"
        video_filters.append(stack + "subtitles=caption.ass,format=yuv420p[video]")
        audio = f"[0:{audio_stream}]aresample=48000:async=1000:first_pts=0,aformat=channel_layouts=stereo"
        if track:
            audio += (
                f",apad,atrim=duration={duration:.3f},asplit=2[voice][key];"
                "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.14[music];"
                "[music][key]sidechaincompress=threshold=0.025:ratio=8:attack=25:release=400[ducked];"
                "[voice][ducked]amix=inputs=2:duration=first:normalize=0"
            )
        else:
            audio += ",apad"
        audio += ",loudnorm=I=-16:TP=-1.5:LRA=9,aresample=48000,alimiter=limit=0.84:level=0[audio]"
        run(ffmpeg(*args, "-filter_complex_threads", "1", "-filter_complex", ";".join([*video_filters, audio]),
                   "-map", "[video]", "-map", "[audio]", "-t", f"{duration:.3f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads:v", "3",
                   "-c:a", "aac", "-ar", "48000", "-b:a", "160k", "-movflags", "+faststart",
                   "-map_metadata", "-1", temp / "render.mp4"),
            check, cwd=temp)
        check()
        (temp / "render.mp4").replace(output)
