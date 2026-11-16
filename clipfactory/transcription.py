import wave
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=2)
def load_model(model, device, compute):
    from faster_whisper import WhisperModel

    return WhisperModel(model, device=device, compute_type=compute)


def transcribe(wav, settings, config, check, progress):
    model = load_model(settings.model, settings.device, settings.compute)
    language = None if config["language"] == "auto" else config["language"]
    prompt = config["vocabulary"] or None
    transcript = []
    with wave.open(str(wav), "rb") as audio:
        rate, total = audio.getframerate(), audio.getnframes()
        window = 480 * rate
        overlap = 5 * rate
        for cursor in range(0, total, window):
            check()
            start = max(0, cursor - overlap)
            end = min(total, cursor + window + overlap)
            audio.setpos(start)
            pcm = np.frombuffer(audio.readframes(end - start), "<i2").astype("float32") / 32768
            source, info = model.transcribe(
                pcm, language=language, initial_prompt=prompt, word_timestamps=True,
                vad_filter=True, condition_on_previous_text=False,
            )
            if language is None and info.language_probability >= 0.75:
                language = info.language
            offset = start / rate
            for segment in source:
                check()
                midpoint = offset + (segment.start + segment.end) / 2
                if not cursor / rate <= midpoint < min(total, cursor + window) / rate:
                    continue
                words = [{"start": round(offset + word.start, 3), "end": round(offset + word.end, 3),
                          "text": word.word.strip(), "confidence": round(word.probability, 3)}
                         for word in segment.words or [] if word.word.strip()]
                low = segment.avg_logprob < -0.9 or segment.no_speech_prob > 0.65
                transcript.append({"id": len(transcript), "start": round(offset + segment.start, 3),
                                   "end": round(offset + segment.end, 3), "text": segment.text.strip(),
                                   "words": words, "review": low or any(w["confidence"] < .45 for w in words),
                                   "unclear": low})
            progress(min(1, (cursor + window) / total))
    return {"language": language, "segments": transcript}
