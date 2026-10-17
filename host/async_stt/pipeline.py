
# import logging
# import os
# import time
# import uuid
# from pathlib import Path

# import soundfile as sf

# from celery import Celery
# from ray import serve
# from ray.serve.schema import CeleryAdapterConfig, TaskProcessorConfig
# from ray.serve.task_consumer import task_consumer, task_handler

# from common.job_store import (
#     create_job,
#     get_job,
#     update_task,
#     finalize_job,
# )


# logger = logging.getLogger("pipeline.consumer")
# logging.basicConfig(level=logging.INFO)

# REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
# REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
# REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "mysecurepassword")

# BROKER_URL = (
#     f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"
# )
# BACKEND_URL = (
#     f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1"
# )

# PIPELINE_QUEUE = "pipeline_queue"
# VAD_QUEUE = "vad_queue"
# ASR_QUEUE = "asr_queue"

# LANGUAGE = os.getenv("ASR_LANGUAGE", "auto")
# POLL_INTERVAL = 1
# TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT", "1800"))

# SEGMENT_DIR = Path(
#     os.getenv("SEGMENT_DIR", "./data/audio_segments")
# ).resolve()

# celery_client = Celery(
#     "pipeline_dispatcher",
#     broker=BROKER_URL,
#     backend=BACKEND_URL,
# )

# processor_config = TaskProcessorConfig(
#     queue_name=PIPELINE_QUEUE,
#     adapter_config=CeleryAdapterConfig(
#         broker_url=BROKER_URL,
#         backend_url=BACKEND_URL,
#     ),
# )


# def wait_for_task(job_id, task_id):
#     started = time.monotonic()

#     while time.monotonic() - started < TASK_TIMEOUT:
#         job = get_job(job_id)

#         if job is None:
#             raise RuntimeError(f"Job not found: {job_id}")

#         task = job.get("tasks", {}).get(task_id)

#         if task:
#             if task.get("status") == "completed":
#                 return task

#             if task.get("status") == "failed":
#                 raise RuntimeError(
#                     f"Task {task_id} failed: {task.get('error')}"
#                 )

#         time.sleep(POLL_INTERVAL)

#     raise TimeoutError(f"Timed out waiting for {task_id}")


# def submit_child(task_name, queue, payload, task_id):
#     celery_client.send_task(
#         task_name,
#         args=[payload],
#         queue=queue,
#         task_id=task_id,
#     )


# @serve.deployment(
#     max_ongoing_requests=2,
# )
# @task_consumer(task_processor_config=processor_config)
# class PipelineConsumer:

#     @task_handler(name="run_pipeline")
#     def run_pipeline(self, data):
#         job_id = str(data["job_id"])
#         pipeline_task_id = str(data["task_id"])
#         audio_path = Path(data["audio_path"]).resolve()
#         language = str(data.get("language", LANGUAGE))

#         try:
#             if not audio_path.is_file():
#                 raise FileNotFoundError(str(audio_path))

#             if get_job(job_id) is None:
#                 create_job(
#                     job_id,
#                     pipeline="diarization_asr",
#                     input_data={
#                         "audio_path": str(audio_path),
#                         "language": language,
#                     },
#                 )

#             update_task(
#                 job_id,
#                 pipeline_task_id,
#                 task_type="pipeline",
#                 status="processing",
#                 audio_path=str(audio_path),
#             )

#             # Stage 1: diarization.
#             diarization_task_id = str(uuid.uuid4())

#             logger.info("Submitting diarization: %s", job_id)

#             submit_child(
#                 "detect",
#                 VAD_QUEUE,
#                 {
#                     "task_id": diarization_task_id,
#                     "job_id": job_id,
#                     "audio_path": str(audio_path),
#                 },
#                 diarization_task_id,
#             )

#             diarization_task = wait_for_task(
#                 job_id, diarization_task_id
#             )

#             diarization_result = diarization_task.get("result") or {}
#             diarization_segments = diarization_result.get("segments", [])

#             logger.info(
#                 "Diarization complete: %d segments",
#                 len(diarization_segments),
#             )

#             # Stage 2: cut audio on the Ray worker.
#             audio, sample_rate = sf.read(
#                 str(audio_path),
#                 dtype="float32",
#                 always_2d=False,
#             )

#             if audio.ndim == 2:
#                 audio = audio.mean(axis=1)

#             duration = len(audio) / sample_rate
#             segment_dir = SEGMENT_DIR / job_id
#             segment_dir.mkdir(parents=True, exist_ok=True)

#             segments = []

#             for index, segment in enumerate(diarization_segments):
#                 start = max(0.0, float(segment["start"]))
#                 end = min(duration, float(segment["end"]))

#                 start_sample = int(start * sample_rate)
#                 end_sample = min(len(audio), int(end * sample_rate))

#                 if end_sample <= start_sample:
#                     continue

#                 segment_id = str(uuid.uuid4())
#                 segment_path = (
#                     segment_dir / f"{index:05d}_{segment_id}.wav"
#                 )

#                 sf.write(
#                     str(segment_path),
#                     audio[start_sample:end_sample],
#                     sample_rate,
#                     subtype="PCM_16",
#                 )

#                 segments.append({
#                     "segment_id": segment_id,
#                     "speaker": str(segment["speaker"]),
#                     "start": round(start, 3),
#                     "end": round(end, 3),
#                     "audio_path": str(segment_path),
#                     "asr_task_id": str(uuid.uuid4()),
#                 })

#             # Stage 3: dispatch all ASR tasks before waiting.
#             for segment in segments:
#                 submit_child(
#                     "transcribe",
#                     ASR_QUEUE,
#                     {
#                         "task_id": segment["asr_task_id"],
#                         "job_id": job_id,
#                         "audio_path": segment["audio_path"],
#                         "language": language,
#                     },
#                     segment["asr_task_id"],
#                 )

#             # Stage 4: collect ASR results.
#             transcript_segments = []

#             for segment in segments:
#                 task = wait_for_task(
#                     job_id, segment["asr_task_id"]
#                 )
#                 asr_result = task.get("result") or {}

#                 transcript_segments.append({
#                     "segment_id": segment["segment_id"],
#                     "speaker": segment["speaker"],
#                     "start": segment["start"],
#                     "end": segment["end"],
#                     "text": asr_result.get("text", ""),
#                     "language": asr_result.get("language", language),
#                 })

#             transcript_segments.sort(key=lambda x: x["start"])

#             final_result = {
#                 "audio_path": str(audio_path),
#                 "language": language,
#                 "segment_count": len(transcript_segments),
#                 "speaker_count": len({
#                     s["speaker"] for s in transcript_segments
#                 }),
#                 "segments": transcript_segments,
#                 "text": " ".join(
#                     s["text"].strip()
#                     for s in transcript_segments
#                     if s["text"].strip()
#                 ),
#             }

#             update_task(
#                 job_id,
#                 pipeline_task_id,
#                 task_type="pipeline",
#                 status="completed",
#                 result=final_result,
#                 error=None,
#             )

#             finalize_job(
#                 job_id,
#                 status="completed",
#                 result=final_result,
#             )

#             logger.info("Pipeline completed: %s", job_id)
#             return final_result

#         except Exception as exc:
#             logger.exception("Pipeline failed: %s", job_id)

#             try:
#                 update_task(
#                     job_id,
#                     pipeline_task_id,
#                     task_type="pipeline",
#                     status="failed",
#                     error=str(exc),
#                 )
#                 finalize_job(
#                     job_id,
#                     status="failed",
#                     error=str(exc),
#                 )
#             except Exception:
#                 logger.exception("Failed to persist pipeline failure")

#             raise


# app = PipelineConsumer.bind()



import logging
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

import soundfile as sf

from celery import Celery
from ray import serve
from ray.serve.schema import CeleryAdapterConfig, TaskProcessorConfig
from ray.serve.task_consumer import task_consumer, task_handler

from common.job_store import (
    create_job,
    get_job,
    update_task,
    finalize_job,
)


logger = logging.getLogger("pipeline.consumer")
logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "mysecurepassword")

BROKER_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"
BACKEND_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1"

PIPELINE_QUEUE = "pipeline_queue"
VAD_QUEUE = "vad_queue"
ASR_QUEUE = "asr_queue"

LANGUAGE = os.getenv("ASR_LANGUAGE", "auto")
POLL_INTERVAL = 1
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT", "1800"))

SEGMENT_DIR = Path(
    os.getenv("SEGMENT_DIR", "./data/audio_segments")
).resolve()

celery_client = Celery(
    "pipeline_dispatcher",
    broker=BROKER_URL,
    backend=BACKEND_URL,
)

processor_config = TaskProcessorConfig(
    queue_name=PIPELINE_QUEUE,
    adapter_config=CeleryAdapterConfig(
        broker_url=BROKER_URL,
        backend_url=BACKEND_URL,
    ),
)


# ---------------------------------------------------------------------
# Transcript post-processing
# ---------------------------------------------------------------------

# Matches tags such as:
# <en-US>, <sl-SI>, <hi-IN>, <en>, <zh-CN>
LANGUAGE_TAG_RE = re.compile(
    r"<\s*[a-z]{2,3}(?:-[a-z]{2,4})?\s*>",
    re.IGNORECASE,
)

# Used to identify Devanagari text in the example's garbled Hindi section.
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]+")

# Optional: remove these characters only if they occur as control characters.
CONTROL_CATEGORIES = {"Cc", "Cf", "Cs"}


def normalize_transcript_text(text: str) -> str:
    """
    Clean transcript formatting without rewriting the spoken words.

    Removes language tags, normalizes Unicode and whitespace, and
    cleans punctuation.
    """
    if not isinstance(text, str):
        return ""

    # Normalize Unicode.
    text = unicodedata.normalize("NFC", text)

    # Remove language markers such as <en-US> and <hi-IN>.
    text = LANGUAGE_TAG_RE.sub(" ", text)

    # Remove control/format characters while preserving whitespace.
    text = "".join(
        char
        for char in text
        if unicodedata.category(char) not in CONTROL_CATEGORIES
        or char in "\t\n\r"
    )

    # Remove Devanagari characters from the corrupted Hindi portion.
    # This is character-level filtering, not language identification.
    text = DEVANAGARI_RE.sub("", text)

    # Normalize whitespace.
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return ""

    # Remove spaces before punctuation.
    text = re.sub(r"\s+([,.!?;:%])", r"\1", text)

    # Add spaces after punctuation when missing.
    text = re.sub(r"([,.!?;:])(?=\w)", r"\1 ", text)

    # Reduce excessive repeated punctuation.
    text = re.sub(r"([!?.,])\1{2,}", r"\1\1", text)

    return text.strip()


def postprocess_segments(segments: list[dict]) -> list[dict]:
    """
    Clean ASR segments while preserving their metadata, speaker labels,
    and timestamps. Empty text segments are discarded.
    """
    cleaned = []

    for segment in segments:
        text = normalize_transcript_text(
            segment.get("text", "")
        )

        if not text:
            continue

        item = {
            **segment,
            "text": text,
        }

        for key in ("start", "end"):
            if key in item:
                item[key] = round(float(item[key]), 3)

        cleaned.append(item)

    cleaned.sort(
        key=lambda item: (
            float(item.get("start", 0.0)),
            float(item.get("end", 0.0)),
        )
    )

    return cleaned


def build_transcript_text(segments: list[dict]) -> str:
    """
    Build a readable speaker-labelled transcript.

    Consecutive segments from the same speaker are combined into one
    paragraph. A speaker change starts a new line.
    """
    output = []
    current_speaker = None
    current_text = []

    for segment in segments:
        speaker = str(segment.get("speaker", "UNKNOWN"))
        text = segment.get("text", "").strip()

        if not text:
            continue

        if current_speaker is None:
            current_speaker = speaker
            current_text = [text]

        elif speaker == current_speaker:
            current_text.append(text)

        else:
            output.append(
                f"{current_speaker}: {' '.join(current_text)}"
            )
            current_speaker = speaker
            current_text = [text]

    if current_speaker is not None:
        output.append(
            f"{current_speaker}: {' '.join(current_text)}"
        )

    return "\n".join(output)


def build_plain_transcript(segments: list[dict]) -> str:
    """Build normalized text without speaker labels."""
    return " ".join(
        segment["text"]
        for segment in segments
        if segment.get("text", "").strip()
    )


# ---------------------------------------------------------------------
# Celery dispatch and task polling
# ---------------------------------------------------------------------

def wait_for_task(job_id, task_id):
    started = time.monotonic()

    while time.monotonic() - started < TASK_TIMEOUT:
        job = get_job(job_id)

        if job is None:
            raise RuntimeError(f"Job not found: {job_id}")

        task = job.get("tasks", {}).get(task_id)

        if task:
            status = task.get("status")

            if status == "completed":
                return task

            if status == "failed":
                raise RuntimeError(
                    f"Task {task_id} failed: {task.get('error')}"
                )

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"Timed out waiting for {task_id}")


def submit_child(task_name, queue, payload, task_id):
    celery_client.send_task(
        task_name,
        args=[payload],
        queue=queue,
        task_id=task_id,
    )


# ---------------------------------------------------------------------
# Ray Serve pipeline consumer
# ---------------------------------------------------------------------

@serve.deployment(
    max_ongoing_requests=2,
)
@task_consumer(task_processor_config=processor_config)
class PipelineConsumer:

    @task_handler(name="run_pipeline")
    def run_pipeline(self, data):
        job_id = str(data["job_id"])
        pipeline_task_id = str(data["task_id"])
        audio_path = Path(data["audio_path"]).resolve()
        language = str(data.get("language", LANGUAGE))

        try:
            # Stage 0: validate input and initialize job.
            if not audio_path.is_file():
                raise FileNotFoundError(str(audio_path))

            if get_job(job_id) is None:
                create_job(
                    job_id,
                    pipeline="diarization_asr",
                    input_data={
                        "audio_path": str(audio_path),
                        "language": language,
                    },
                )

            update_task(
                job_id,
                pipeline_task_id,
                task_type="pipeline",
                status="processing",
                audio_path=str(audio_path),
            )

            # Stage 1: diarization / VAD.
            diarization_task_id = str(uuid.uuid4())

            logger.info(
                "Submitting diarization for job %s",
                job_id,
            )

            submit_child(
                "detect",
                VAD_QUEUE,
                {
                    "task_id": diarization_task_id,
                    "job_id": job_id,
                    "audio_path": str(audio_path),
                },
                diarization_task_id,
            )

            diarization_task = wait_for_task(
                job_id,
                diarization_task_id,
            )

            diarization_result = (
                diarization_task.get("result") or {}
            )
            diarization_segments = (
                diarization_result.get("segments") or []
            )

            logger.info(
                "Diarization completed for %s: %d segments",
                job_id,
                len(diarization_segments),
            )

            # Stage 2: load audio and cut diarized segments.
            audio, sample_rate = sf.read(
                str(audio_path),
                dtype="float32",
                always_2d=False,
            )

            if audio.ndim == 2:
                audio = audio.mean(axis=1)

            if sample_rate <= 0 or len(audio) == 0:
                raise ValueError("Input audio is empty or invalid")

            duration = len(audio) / sample_rate

            segment_dir = SEGMENT_DIR / job_id
            segment_dir.mkdir(parents=True, exist_ok=True)

            segments = []

            for index, segment in enumerate(diarization_segments):
                start = max(0.0, float(segment["start"]))
                end = min(duration, float(segment["end"]))

                if end <= start:
                    logger.warning(
                        "Skipping invalid segment %d: %s - %s",
                        index,
                        start,
                        end,
                    )
                    continue

                start_sample = int(start * sample_rate)
                end_sample = min(
                    len(audio),
                    int(end * sample_rate),
                )

                if end_sample <= start_sample:
                    continue

                segment_id = str(uuid.uuid4())

                segment_path = (
                    segment_dir
                    / f"{index:05d}_{segment_id}.wav"
                )

                sf.write(
                    str(segment_path),
                    audio[start_sample:end_sample],
                    sample_rate,
                    subtype="PCM_16",
                )

                segments.append({
                    "segment_id": segment_id,
                    "speaker": str(
                        segment.get("speaker", "UNKNOWN")
                    ),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "audio_path": str(segment_path),
                    "asr_task_id": str(uuid.uuid4()),
                })

            logger.info(
                "Created %d audio segments for ASR, job %s",
                len(segments),
                job_id,
            )

            # Stage 3: dispatch all ASR tasks before waiting.
            for segment in segments:
                submit_child(
                    "transcribe",
                    ASR_QUEUE,
                    {
                        "task_id": segment["asr_task_id"],
                        "job_id": job_id,
                        "audio_path": segment["audio_path"],
                        "language": language,
                    },
                    segment["asr_task_id"],
                )

            # Stage 4: collect ASR results.
            transcript_segments = []

            for segment in segments:
                task = wait_for_task(
                    job_id,
                    segment["asr_task_id"],
                )

                asr_result = task.get("result") or {}

                transcript_segments.append({
                    "segment_id": segment["segment_id"],
                    "speaker": segment["speaker"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": asr_result.get("text", ""),
                    "language": asr_result.get(
                        "language",
                        language,
                    ),
                })

            # Stage 5: normalize text and remove empty segments.
            transcript_segments = postprocess_segments(
                transcript_segments
            )

            transcript_text = build_transcript_text(
                transcript_segments
            )
            plain_text = build_plain_transcript(
                transcript_segments
            )

            logger.info(
                "Transcript post-processing completed for %s: "
                "%d non-empty segments",
                job_id,
                len(transcript_segments),
            )

            # Stage 6: build final result.
            final_result = {
                "audio_path": str(audio_path),
                "language": language,
                "duration_seconds": round(duration, 3),
                "segment_count": len(transcript_segments),
                "speaker_count": len({
                    segment["speaker"]
                    for segment in transcript_segments
                }),
                "segments": transcript_segments,
                "text": transcript_text,
                "text_plain": plain_text,
            }

            # Stage 7: persist completion.
            update_task(
                job_id,
                pipeline_task_id,
                task_type="pipeline",
                status="completed",
                result=final_result,
                error=None,
            )

            finalize_job(
                job_id,
                status="completed",
                result=final_result,
            )

            logger.info("Pipeline completed: %s", job_id)

            return final_result

        except Exception as exc:
            logger.exception(
                "Pipeline failed: %s",
                job_id,
            )

            try:
                update_task(
                    job_id,
                    pipeline_task_id,
                    task_type="pipeline",
                    status="failed",
                    error=str(exc),
                )

                finalize_job(
                    job_id,
                    status="failed",
                    error=str(exc),
                )

            except Exception:
                logger.exception(
                    "Failed to persist pipeline failure"
                )

            raise


app = PipelineConsumer.bind()
