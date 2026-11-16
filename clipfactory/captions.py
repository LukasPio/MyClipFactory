def ass_time(seconds):
    centis = max(0, round(seconds * 100))
    return f"{centis//360000}:{centis//6000%60:02d}:{centis//100%60:02d}.{centis%100:02d}"


def ass_color(color):
    return f"&H00{color[5:7]}{color[3:5]}{color[1:3]}&"


def escape(text):
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")


def timed_words(segment, correction):
    if correction is None and segment["unclear"]:
        return [segment | {"text": "[inaudível]"}]
    original = segment["words"]
    if correction is None:
        return original or [segment]
    words = correction.split()
    if not words:
        return []
    if len(words) == len(original):
        return [timing | {"text": word} for timing, word in zip(original, words)]
    total = len(words)
    length = segment["end"] - segment["start"]
    return [{"text": word, "start": segment["start"] + length * index / total,
             "end": segment["start"] + length * (index + 1) / total}
            for index, word in enumerate(words)]


def subtitles(path, transcript, edit, webcam):
    style = edit["captions"]
    start, end = edit["start"], edit["end"]
    color, highlight = ass_color(style["color"]), ass_color(style["highlight"])
    vertical = 330 if style["position"] == "bottom" and not webcam else 420
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 0",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{style['font']},{style['size']},{highlight},{color},&H0010141A,&H5510141A,"
        f"-1,0,0,0,100,100,0,0,1,5,2,2,110,110,{vertical},1",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    fixes = {correction["id"]: correction["text"] for correction in edit["corrections"]}
    for segment in transcript["segments"]:
        if segment["end"] <= start or segment["start"] >= end:
            continue
        words = [w for w in timed_words(segment, fixes.get(segment["id"]))
                 if w["end"] > start and w["start"] < end]
        for index in range(0, len(words), style["words"]):
            block = words[index:index + style["words"]]
            begin = max(start, block[0]["start"])
            finish = min(end, block[-1]["end"])
            if finish - begin < .02:
                continue
            cursor = begin
            content = []
            for word in block:
                offset = max(cursor, min(finish, word["start"]))
                until = max(offset, min(finish, word["end"]))
                if style["animate"] and offset - cursor >= .01:
                    content.append(r"{\k" + str(round((offset - cursor) * 100)) + "}")
                tag = r"{\kf" + str(max(1, round((until - offset) * 100))) + "}" if style["animate"] else r"{\1c" + color + "}"
                content.append(tag + escape(word["text"]))
                cursor = until
            prefix = r"{\fad(45,65)}" if style["animate"] else ""
            lines.append(f"Dialogue: 0,{ass_time(begin-start)},{ass_time(finish-start)},Caption,,0,0,0,,"
                         + prefix + " ".join(content))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
