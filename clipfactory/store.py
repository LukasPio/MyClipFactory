import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, bytes INTEGER NOT NULL, info TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_cache (
 video_id TEXT NOT NULL REFERENCES videos(id), cache_key TEXT NOT NULL,
 transcript TEXT NOT NULL, analysis TEXT NOT NULL, PRIMARY KEY(video_id,cache_key)
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
 clip_id TEXT REFERENCES clips(id), kind TEXT NOT NULL, config TEXT NOT NULL,
 state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, message TEXT,
 cancel INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, lease REAL, owner TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created);
CREATE UNIQUE INDEX IF NOT EXISTS one_video_job ON jobs(video_id)
 WHERE kind='VIDEO' AND state IN ('QUEUED','RUNNING');
CREATE UNIQUE INDEX IF NOT EXISTS one_clip_job ON jobs(clip_id)
 WHERE kind='RENDER' AND state IN ('QUEUED','RUNNING');
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 stamp REAL NOT NULL, progress INTEGER NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
 id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
 candidate TEXT NOT NULL, candidate_key TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
 rendered_revision INTEGER, fields TEXT NOT NULL, created REAL NOT NULL, UNIQUE(video_id,candidate_key)
);
CREATE TABLE IF NOT EXISTS edits (
 clip_id TEXT NOT NULL REFERENCES clips(id), revision INTEGER NOT NULL,
 fields TEXT NOT NULL, stamp REAL NOT NULL, PRIMARY KEY(clip_id, revision)
);
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
 label TEXT NOT NULL, token TEXT NOT NULL, UNIQUE(platform, external_id)
);
CREATE TABLE IF NOT EXISTS oauth_states (
 state TEXT PRIMARY KEY, platform TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
 id TEXT PRIMARY KEY, clip_id TEXT NOT NULL REFERENCES clips(id),
 account_id TEXT NOT NULL REFERENCES accounts(id), revision INTEGER NOT NULL,
 fields TEXT NOT NULL, state TEXT NOT NULL, due REAL NOT NULL, approved REAL,
 created REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
 checkpoint TEXT NOT NULL DEFAULT '{}', external_id TEXT, url TEXT,
 lease REAL, owner TEXT, UNIQUE(clip_id, account_id, revision)
);
CREATE INDEX IF NOT EXISTS publications_queue ON publications(state, due);
CREATE TABLE IF NOT EXISTS analytics (
 id INTEGER PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publications(id),
 recorded REAL NOT NULL, source TEXT NOT NULL, fields TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analytics_history ON analytics(publication_id, recorded);
"""


def uid():
    return uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def unpack(row, *fields):
    data = dict(row)
    for field in fields:
        if data[field] is not None:
            data[field] = json.loads(data[field])
    return data


class Store:
    def __init__(self, settings):
        self.path = settings.file("", "factory.sqlite3")

    def init(self):
        with self.tx() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def tx(self, write=False):
        conn = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue(self, conn, video, clip, kind, config):
        job = uid()
        conn.execute(
            "INSERT INTO jobs(id,video_id,clip_id,kind,config,state,created) VALUES(?,?,?,?,?,'QUEUED',?)",
            (job, video, clip, kind, dump(config), time.time()),
        )
        return job

    def claim_job(self):
        now = time.time()
        with self.tx(True) as conn:
            conn.execute(
                "UPDATE jobs SET state='CANCELLED',owner=NULL WHERE state='RUNNING' AND lease<? AND cancel=1",
                (now,),
            )
            row = conn.execute(
                """SELECT * FROM jobs WHERE state='QUEUED' OR
                (state='RUNNING' AND lease<? AND cancel=0) ORDER BY created LIMIT 1""", (now,),
            ).fetchone()
            if row:
                owner = uid()
                conn.execute(
                    "UPDATE jobs SET state='RUNNING',owner=?,lease=? WHERE id=?",
                    (owner, now + 90, row["id"]),
                )
                return unpack(row, "config") | {"owner": owner}

    def report(self, conn, job, percent, message, state="RUNNING"):
        cursor = conn.execute(
            "UPDATE jobs SET state=?,progress=?,message=? WHERE id=? AND owner=?",
            (state, percent, message, job["id"], job["owner"]),
        )
        if not cursor.rowcount:
            raise InterruptedError("O worker perdeu a reserva do job.")
        conn.execute("INSERT INTO events(job_id,stamp,progress,message) VALUES(?,?,?,?)",
                     (job["id"], time.time(), percent, message))
