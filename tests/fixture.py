from pathlib import Path
from tempfile import TemporaryDirectory

from clipfactory.media import ffmpeg, run

DIALOGUE = [
    (2.0, "We have to follow the blue car."),
    (6.0, "Wow, no way! The police are chasing us."),
    (11.0, "We escaped. That was close."),
    (24.0, "The other car is turning left."),
    (28.0, "Oh my god! It crashed."),
    (34.0, "They are both out of the race."),
]


def sample(path):
    video = (
        "[0:v]format=yuv420p,drawbox=x=8:y=8:w=100:h=75:color=black:t=fill,"
        "drawbox=x=33:y=17:w=51:h=54:color=0xffcfa6:t=fill,"
        "drawbox=x=44:y=29:w=8:h=8:color=black:t=fill,"
        "drawbox=x=65:y=29:w=8:h=8:color=black:t=fill,"
        "drawbox=x=45:y=53:w=26:h=7:color=0x7a3434:t=fill,"
        "drawbox=y=115:x=0:w=iw:h=ih:color=red@0.75:t=fill:enable='between(t,6,7)+between(t,28,29)',"
        "drawbox=y=115:x=0:w=iw:h=ih:color=yellow@0.7:t=fill:enable='between(t,18,19)'[v]"
    )
    with TemporaryDirectory() as directory:
        args = []
        for index, (stamp, line) in enumerate(DIALOGUE):
            word_file = Path(directory) / f"line{index}.txt"
            word_file.write_text(line, encoding="utf-8")
            args.extend(["-f", "lavfi", "-i", f"flite=textfile={word_file}:voice=slt"])
        filters = ["[1:a]aresample=16000,volume=0.04[a0]"]
        for index, (stamp, _) in enumerate(DIALOGUE, start=1):
            gain = 2.4 if stamp in (6.0, 28.0) else 1.2
            filters.append(f"[{index+1}:a]aresample=16000,volume={gain},adelay={round(stamp*1000)}:all=1[a{index}]")
        mix = "".join(f"[a{i}]" for i in range(len(DIALOGUE) + 1))
        filters.append(mix + f"amix=inputs={len(DIALOGUE)+1}:normalize=0,alimiter=limit=.95[a]")
        run(ffmpeg("-f", "lavfi", "-i", "testsrc2=s=480x270:r=12:d=42",
                   "-f", "lavfi", "-i", "sine=frequency=127:sample_rate=16000:duration=42",
                   *args, "-filter_complex_threads", "1", "-filter_complex", ";".join([video, *filters]),
                   "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                   "-threads:v", "2", "-c:a", "aac", "-movflags", "+faststart", path))
    return path


def fixture_transcript(wav, settings, configuration, check, progress):
    result = []
    for stamp, line in DIALOGUE:
        words = [{"text": word, "start": stamp + index * .26, "end": stamp + (index + 1) * .26,
                  "confidence": .98} for index, word in enumerate(line.split())]
        result.append({"id": len(result), "text": line, "start": stamp, "end": words[-1]["end"],
                       "review": False, "unclear": False, "words": words})
    progress(1)
    return {"language": "en", "segments": result}
