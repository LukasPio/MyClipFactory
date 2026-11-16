import math
import re
from bisect import bisect_left

import numpy as np


REACTIONS = re.compile(
    r"\b(nossa|caramba|eita|mano|mentira|socorro|meu deus|meu amor|quê|what|whoa|wow|oh my god|"
    r"no way|help|seriously|holy|damn|kkkk+|hahaha+)\b", re.I
)
ACTION = re.compile(
    r"\b(explod\w*|explos\w*|fug\w*|persegu\w*|polícia|tiro\w*|bateu|capot\w*|"
    r"shot|chase|chasing|crash\w*|explod\w*|explosion|police|fight\w*)\b", re.I
)
TERMINAL = re.compile(r"[.!?…][\W]*$")


def webcam_region(faces, duration):
    if not faces:
        return None
    clusters = {}
    for stamp, x, y, w, h in faces:
        key = (min(3, int(x * 4)), min(3, int(y * 4)))
        if key in {(1, 1), (1, 2), (2, 1), (2, 2)}:
            continue
        clusters.setdefault(key, []).append((stamp, x, y, w, h))
    if not clusters:
        return None
    strongest = max(clusters.values(), key=lambda group: len({f[0] for f in group}))
    observed = {f[0] for f in strongest}
    if len(observed) < max(3, math.ceil(duration / 2 * .10)):
        return None
    _, x, y, w, h = np.median(strongest, axis=0)
    width, height = min(.42, max(.18, w * 3.1)), min(.48, max(.18, h * 2.8))
    return {"x": float(np.clip(x - width / 2, 0, 1 - width)),
            "y": float(np.clip(y - height / 2, 0, 1 - height)),
            "w": float(width), "h": float(height)}


def aligned_window(t, duration, minimum, maximum, segments, starts):
    left = max(0, min(t - maximum * .45, duration - minimum))
    right = min(duration, left + maximum)
    index = bisect_left(starts, left)
    if index and segments[index - 1]["end"] > left:
        left = segments[index - 1]["start"]
        right = min(duration, left + maximum)
    index = bisect_left(starts, right)
    if index and segments[index - 1]["end"] > right:
        right = segments[index - 1]["start"]
    eligible = [s["end"] for s in segments[bisect_left(starts, max(left, t)) : index]
                if max(left + minimum, t + 1) <= s["end"] <= right and TERMINAL.search(s["text"])]
    if eligible and right - eligible[-1] <= 8:
        right = eligible[-1]
    if right - left < minimum or not left <= t <= right:
        return None
    return round(left, 3), round(right, 3)


def candidates(info, transcript, audio, video, faces, config):
    duration = info["duration"]
    webcam = config["webcam"]
    if webcam is None and config["detect_webcam"]:
        webcam = webcam_region(faces, duration)
    minimum, maximum = min(duration, config["min_seconds"]), min(duration, config["max_seconds"])
    segments = transcript["segments"]
    starts = [s["start"] for s in segments]
    motion = np.array([v["motion"] for v in video])
    rms = np.array([a["rms"] for a in audio])
    motion_ref = max(.025, float(np.percentile(motion, 80)))
    audio_ref = max(.035, float(np.percentile(rms, 82))) if len(rms) else .035
    activity = []
    for index, frame in enumerate(video):
        sound = audio[index] if index < len(audio) else {"rms": 0, "jump": 0}
        score = min(2, frame["motion"] / motion_ref) + .5 * frame["scene"]
        score += min(1.3, sound["jump"] / audio_ref)
        activity.append((score, frame["t"]))
    stems = set()
    for index in range(0, len(activity), 12):
        strength, stamp = max(activity[index:index + 12])
        if strength >= .48:
            stems.add(stamp)
    for segment in segments:
        if not segment["unclear"] and (REACTIONS.search(segment["text"]) or ACTION.search(segment["text"])):
            stems.add((segment["start"] + segment["end"]) / 2)
    proposals = {}
    face_starts = [item[0] for item in faces]
    for stamp in sorted(stems):
        bounds = aligned_window(stamp, duration, minimum, maximum, segments, starts)
        if bounds is None or bounds in proposals:
            continue
        start, end = bounds
        begin, stop = round(start * 2), max(round(end * 2), round(start * 2) + 1)
        visuals, sounds = video[begin:stop], audio[begin:stop]
        speech = [s for s in segments[max(0, bisect_left(starts, start) - 1):bisect_left(starts, end)]
                  if s["end"] > start and not s["unclear"]]
        text = " ".join(s["text"] for s in speech)
        words = text.split()
        reaction = any(REACTIONS.search(s["text"]) for s in speech)
        action = bool(ACTION.search(text))
        picture = min(1, sum(v["motion"] for v in visuals) / max(len(visuals), 1) / motion_ref)
        scenes = sum(v["scene"] for v in visuals)
        sonic = min(1, max((s["jump"] for s in sounds), default=0) / audio_ref)
        silence = sum(s["rms"] < .012 for s in sounds) / max(1, len(sounds))
        complete = bool(speech and TERMINAL.search(speech[-1]["text"]))
        context = min(1, len(words) / 44)
        variety = len(set(re.findall(r"\w+", text.lower()))) / max(12, len(words))
        if picture < .22 and scenes == 0 and not (context >= .35 and (reaction or action)):
            continue
        score = 100 * min(1, .20 * picture + .08 * min(1, scenes / 2) + .10 * sonic
                          + .18 * context + .12 * complete + .10 * reaction + .08 * action
                          + .06 * min(1, variety) + .08 * (1 - silence))
        face_window = faces[bisect_left(face_starts, start):bisect_left(face_starts, end)]
        face_track = [{"t": round(item[0], 2), "x": round(item[1], 3), "y": round(item[2], 3)}
                      for item in face_window if webcam and webcam["x"] <= item[1] <= webcam["x"] + webcam["w"]
                      and webcam["y"] <= item[2] <= webcam["y"] + webcam["h"]]
        highlight = next((s["start"] for s in speech if REACTIONS.search(s["text"])), stamp)
        mode = "webcam" if webcam and reaction and sonic > .48 else "split" if webcam else "gameplay"
        reason = [f"movimento visual {picture:.0%}", f"{scenes} mudança(s) visual(is)",
                  f"{len(words)} palavras em trechos claros"]
        if reaction:
            reason.append("reação identificada pela fala")
        if action:
            reason.append("ação mencionada na transcrição")
        if complete:
            reason.append("fechamento no fim de uma frase")
        mood = ("reaction" if reaction and sonic > .65 else "funny" if reaction
                else "epic" if action and picture > .65 else "tension" if picture > .7 else "")
        proposals[bounds] = {"start": start, "end": end, "score": round(score, 1),
                             "reason": "; ".join(reason), "layout": mode, "highlight": highlight,
                             "webcam": webcam, "face_track": face_track, "mood": mood,
                             "evidence": {"movement": round(picture, 3), "audio_change": round(sonic, 3),
                                          "silence": round(silence, 3), "scene_changes": scenes,
                                          "speech_reaction": reaction, "action_in_speech": action}}
    chosen = []
    for candidate in sorted(proposals.values(), key=lambda item: -item["score"]):
        if any(overlap_ratio(candidate, existing) > .22 for existing in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) == config["limit"]:
            break
    return {"webcam": webcam, "candidates": chosen}


def overlap_ratio(a, b):
    overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return max(0, overlap) / min(a["end"] - a["start"], b["end"] - b["start"])


def suggest(candidate, transcript):
    lines = [s["text"] for s in transcript["segments"] if not s["review"]
             and s["start"] >= candidate["start"] and s["end"] <= candidate["end"] and s["text"]]
    quote = next((line for line in lines if REACTIONS.search(line) and 8 <= len(line) <= 90), "")
    if not quote:
        quote = next((line for line in lines if 8 <= len(line) <= 90), "")
    tags = ["Gameplay", "Clipes"]
    if candidate["evidence"]["speech_reaction"]:
        tags.append("Reação")
    return {"title": quote or f"Momento de gameplay · {int(candidate['start'] // 60):02d}:"
                             f"{int(candidate['start'] % 60):02d}",
            "description": " ".join(lines[:2])[:350] or "Recorte da gravação de gameplay.",
            "hashtags": tags, "hook": quote, "caption": quote}
