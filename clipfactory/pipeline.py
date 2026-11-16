import hashlib
import logging
import signal
import sqlite3
import time
from tempfile import TemporaryDirectory
from threading import Event, Thread
from pathlib import Path

from .config import Settings
from .editorial import candidates, suggest
from .media import MediaError, audio_signals, extract_audio, visual_signals
from .publisher import queue as publisher
from .render import render
from .schemas import Captions
from .store import Store, dump, uid, unpack
from .transcription import transcribe
from .vision import VisionFailure, rerank

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, store, asr=transcribe):
        self.settings = settings
        self.store = store
        self.asr = asr

    def run_job(self, job, stop):
        job_id, owner = job["id"], job["owner"]
        finished, cancelled, lost = Event(), Event(), Event()

        def heartbeat():
            while not finished.is_set():
                try:
                    with self.store.tx(True) as conn:
                        row = conn.execute("SELECT cancel FROM jobs WHERE id=? AND owner=?",
                                           (job_id, owner)).fetchone()
                        if not row:
                            lost.set()
                            return
                        if row["cancel"]:
                            cancelled.set()
                            return
                        conn.execute("UPDATE jobs SET lease=? WHERE id=? AND owner=?",
                                     (time.time() + 90, job_id, owner))
                except sqlite3.Error:
                    logger.exception("Heartbeat do job %s falhou", job_id)
                finished.wait(1)

        def check():
            if lost.is_set():
                raise InterruptedError("Reserva perdida.")
            if stop.is_set() or cancelled.is_set():
                raise InterruptedError("Processamento interrompido.")

        def report(progress, message):
            check()
            with self.store.tx(True) as conn:
                self.store.report(conn, job, progress, message)

        thread = Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            if job["kind"] == "VIDEO":
                self.analyze(job, check, report)
            else:
                self.render_clip(job, check, report)
        except InterruptedError:
            with self.store.tx(True) as conn:
                row = conn.execute("SELECT cancel,progress FROM jobs WHERE id=? AND owner=?",
                                   (job_id, owner)).fetchone()
                if row:
                    state = "CANCELLED" if row["cancel"] else "QUEUED"
                    self.store.report(conn, job, row["progress"], "Processamento interrompido.", state)
                    conn.execute("UPDATE jobs SET owner=NULL WHERE id=?", (job_id,))
        except Exception as error:
            logger.exception("Job %s falhou", job_id)
            message = str(error) if isinstance(error, (MediaError, ModuleNotFoundError)) else (
                "Falha no processamento. Consulte o log do worker para diagnóstico."
            )
            with self.store.tx(True) as conn:
                row = conn.execute("SELECT progress FROM jobs WHERE id=? AND owner=?",
                                   (job_id, owner)).fetchone()
                if row:
                    self.store.report(conn, job, row["progress"], message, "FAILED")
        finally:
            finished.set()
            thread.join()

    def analyze(self, job, check, report):
        video_id, config = job["video_id"], job["config"]
        with self.store.tx() as conn:
            video = unpack(conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone(), "info")
            info = video["info"]
        selected = config["audio_stream"]
        fingerprint = hashlib.sha256(dump({"version": 1, "model": self.settings.model,
                                           "vision": self.settings.vision_model, "config": config}).encode()).hexdigest()
        with self.store.tx() as conn:
            row = conn.execute("SELECT * FROM analysis_cache WHERE video_id=? AND cache_key=?",
                               (video_id, fingerprint)).fetchone()
        source = self.settings.file("sources", video_id + info.get("extension", ".mp4"))
        if row:
            report(80, "Reutilizando transcrição e análise salvas.")
            cache = unpack(row, "transcript", "analysis")
            transcript, analysis = cache["transcript"], cache["analysis"]
        else:
            with TemporaryDirectory(prefix="mcf-analysis-", dir=self.settings.file("tmp", "-").parent) as directory:
                wav = Path(directory) / "audio.wav"
                report(8, "Validando e extraindo a faixa de áudio.")
                extract_audio(source, wav, selected, check)
                sound = audio_signals(wav, check)
                report(24, "Decodificando o vídeo e detectando movimento e mudanças de cena.")
                visuals, faces = visual_signals(source, info, config["detect_webcam"], check)
                report(45, "Transcrevendo com timestamps por palavra.")
                transcript = self.asr(wav, self.settings, config, check,
                                      lambda fraction: report(45 + round(30 * fraction), "Transcrevendo o áudio."))
            report(80, "Combinando sinais de imagem, áudio e fala para selecionar candidatos.")
            analysis = candidates(info, transcript, sound, visuals, faces, config)
            if self.settings.vision_model and analysis["candidates"]:
                report(85, "Avaliando imagens e falas no modelo multimodal configurado.")
                try:
                    analysis = rerank(source, info, analysis, transcript, self.settings, check)
                except VisionFailure as error:
                    logger.warning("Modelo multimodal indisponível (%s)", type(error).__name__)
                    report(90, "Modelo multimodal indisponível. Candidatos mantidos pela análise local.")
        with self.store.tx(True) as conn:
            check()
            conn.execute("INSERT OR IGNORE INTO analysis_cache VALUES(?,?,?,?)",
                         (video_id, fingerprint, dump(transcript), dump(analysis)))
            for candidate in analysis["candidates"]:
                key = f"{fingerprint}:{candidate['start']}:{candidate['end']}"
                candidate = candidate | {"cache_key": fingerprint, "audio_stream": selected}
                clip_id = uid()
                fields = suggest(candidate, transcript) | {
                    "start": candidate["start"], "end": candidate["end"], "layout": "auto",
                    "captions": Captions().model_dump(), "corrections": [],
                }
                result = conn.execute(
                    """INSERT OR IGNORE INTO clips(id,video_id,candidate,candidate_key,state,revision,fields,created)
                       VALUES(?,?,?,?,'PENDING',1,?,?)""",
                    (clip_id, video_id, dump(candidate), key, dump(fields), time.time()),
                )
                if result.rowcount:
                    conn.execute("INSERT INTO edits VALUES(?,?,?,?)", (clip_id, 1, dump(fields), time.time()))
                    self.store.enqueue(conn, video_id, clip_id, "RENDER", {"revision": 1})
            message = (f"{len(analysis['candidates'])} candidato(s) identificado(s). Acompanhe a fila de renderização."
                       if analysis["candidates"] else "Nenhum trecho atingiu o critério de seleção.")
            self.store.report(conn, job, 100, message, "DONE")

    def render_clip(self, job, check, report):
        revision, clip_id = job["config"]["revision"], job["clip_id"]
        with self.store.tx() as conn:
            row = conn.execute(
                """SELECT c.candidate,c.revision,v.id,v.info,e.fields FROM clips c JOIN videos v ON v.id=c.video_id
                   JOIN edits e ON e.clip_id=c.id WHERE c.id=? AND e.revision=?""", (clip_id, revision),
            ).fetchone()
            if not row or row["revision"] != revision:
                raise MediaError("Essa edição foi substituída por uma revisão mais recente.")
            clip = unpack(row, "candidate", "info", "fields")
            candidate = clip["candidate"]
            cache = conn.execute("SELECT transcript FROM analysis_cache WHERE video_id=? AND cache_key=?",
                                 (clip["id"], candidate["cache_key"])).fetchone()
            transcript = unpack(cache, "transcript")["transcript"]
        report(15, "Preparando corte, trilha e legendas.")
        source = self.settings.file("sources", clip["id"] + clip["info"].get("extension", ".mp4"))
        stage = self.settings.file("tmp", f"{job['id']}-{job['owner']}.mp4")
        output = self.settings.file("clips", f"{clip_id}-{revision}.mp4")
        try:
            report(30, "Codificando o MP4 vertical em H.264/AAC.")
            render(source, stage, clip["info"], transcript, candidate, clip["fields"],
                   candidate["audio_stream"], self.settings, check)
            with self.store.tx(True) as conn:
                check()
                active = conn.execute("SELECT cancel FROM jobs WHERE id=? AND owner=?",
                                      (job["id"], job["owner"])).fetchone()
                if not active or active["cancel"]:
                    raise InterruptedError("Renderização cancelada.")
                stage.replace(output)
                conn.execute("UPDATE clips SET rendered_revision=? WHERE id=? AND revision=?",
                             (revision, clip_id, revision))
                self.store.report(conn, job, 100, "MP4 disponível para revisão.", "DONE")
        finally:
            stage.unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    store = Store(settings)
    store.init()
    worker = Worker(settings, store)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def publications():
        while not stop.is_set():
            try:
                if not publisher.work(store, settings):
                    stop.wait(1)
            except Exception:
                logger.exception("Falha na fila de publicação")
                stop.wait(3)

    thread = Thread(target=publications, daemon=True)
    thread.start()
    while not stop.is_set():
        try:
            job = store.claim_job()
            if job:
                worker.run_job(job, stop)
            else:
                stop.wait(1)
        except Exception:
            logger.exception("Falha na fila de processamento")
            stop.wait(3)
    thread.join(timeout=6)


if __name__ == "__main__":
    main()
