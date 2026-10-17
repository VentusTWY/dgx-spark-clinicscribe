
import logging
import os
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio.functional as audio_functional

from transformers import AutoModelForRNNT, AutoProcessor

from ray import serve
from ray.serve.config import AutoscalingConfig, AutoscalingPolicy
from ray.serve.schema import CeleryAdapterConfig, TaskProcessorConfig
from ray.serve.task_consumer import task_consumer, task_handler

from common.job_store import update_task


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("asr.consumer")


REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "mysecurepassword")

BROKER_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"
BACKEND_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1"

QUEUE_NAME = "asr_queue"
MODEL_ID = "nvidia/nemotron-3.5-asr-streaming-0.6b"

DEFAULT_LANGUAGE = os.getenv("ASR_LANGUAGE", "auto")
KEEP_LANGUAGE_TAG = os.getenv(
    "ASR_KEEP_LANGUAGE_TAG", "true"
).lower() in ("1", "true", "yes")

MAX_AUDIO_SECONDS = float(os.getenv("ASR_MAX_AUDIO_SECONDS", "30"))
MAX_NEW_TOKENS = int(os.getenv("ASR_MAX_NEW_TOKENS", "512"))

processor_config = TaskProcessorConfig(
    queue_name=QUEUE_NAME,
    adapter_config=CeleryAdapterConfig(
        broker_url=BROKER_URL,
        backend_url=BACKEND_URL,
    ),
)


@serve.deployment(
    max_ongoing_requests=1,
    autoscaling_config=AutoscalingConfig(
        min_replicas=1,
        max_replicas=int(os.getenv("ASR_MAX_REPLICAS", "4")),
        target_ongoing_requests=1,
        policy=AutoscalingPolicy(
            policy_function=(
                "ray.serve.async_inference_autoscaling_policy:"
                "AsyncInferenceAutoscalingPolicy"
            ),
            policy_kwargs={
                "broker_url": BROKER_URL,
                "queue_name": QUEUE_NAME,
            },
        ),
    ),
)
@task_consumer(task_processor_config=processor_config)
class ASRConsumer:
    def __init__(self):
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)

        self.model = AutoModelForRNNT.from_pretrained(
            MODEL_ID,
            device_map="auto",
        )
        self.model.eval()

        self.device = self.model.device
        self.dtype = self.model.dtype
        self.sample_rate = int(
            self.processor.feature_extractor.sampling_rate
        )

        logger.info(
            "Nemotron ASR loaded: model=%s device=%s dtype=%s "
            "sample_rate=%d max_audio_seconds=%s max_new_tokens=%d",
            type(self.model).__name__,
            self.device,
            self.dtype,
            self.sample_rate,
            MAX_AUDIO_SECONDS,
            MAX_NEW_TOKENS,
        )

    def _load_audio(self, audio_path: Path):
        audio, source_rate = sf.read(
            str(audio_path),
            dtype="float32",
            always_2d=False,
        )

        if audio.ndim == 2:
            audio = audio.mean(axis=1)

        if audio.ndim != 1:
            raise ValueError(
                f"Expected mono audio, got shape={audio.shape}"
            )

        if not np.isfinite(audio).all():
            raise ValueError("Audio contains NaN or infinity")

        if audio.size == 0:
            return audio

        original_duration = len(audio) / source_rate

        # Clip before resampling to avoid resampling unused audio.
        if MAX_AUDIO_SECONDS > 0:
            max_source_samples = int(
                MAX_AUDIO_SECONDS * source_rate
            )
            audio = audio[:max_source_samples]

        waveform = torch.from_numpy(
            np.ascontiguousarray(audio)
        )

        if source_rate != self.sample_rate:
            waveform = audio_functional.resample(
                waveform,
                orig_freq=source_rate,
                new_freq=self.sample_rate,
            )

        audio = waveform.numpy()

        logger.info(
            "Audio ready: path=%s original=%.2fs inference=%.2fs "
            "sample_rate=%d",
            audio_path,
            original_duration,
            len(audio) / self.sample_rate,
            self.sample_rate,
        )

        return audio

    @task_handler(name="transcribe")
    def transcribe(self, data):
        task_id = str(data["task_id"])
        job_id = str(data.get("job_id", task_id))
        audio_path = Path(data["audio_path"]).resolve()

        try:
            update_task(
                job_id,
                task_id,
                task_type="asr",
                status="processing",
                audio_path=str(audio_path),
                error=None,
            )

            if not audio_path.is_file():
                raise FileNotFoundError(
                    f"Audio file not found: {audio_path}"
                )

            load_start = time.perf_counter()
            audio = self._load_audio(audio_path)
            load_seconds = time.perf_counter() - load_start

            duration = round(len(audio) / self.sample_rate, 3)

            language = str(
                data.get("language", DEFAULT_LANGUAGE)
            )

            transcription = ""
            detected_output = None
            preprocess_seconds = 0.0
            inference_seconds = 0.0
            decode_seconds = 0.0

            if len(audio) > 0:
                preprocess_start = time.perf_counter()

                inputs = self.processor(
                    audio,
                    sampling_rate=self.sample_rate,
                    language=language,
                    return_tensors="pt",
                )

                inputs = inputs.to(
                    self.model.device,
                    dtype=self.model.dtype,
                )

                preprocess_seconds = (
                    time.perf_counter() - preprocess_start
                )

                logger.info(
                    "Running ASR: job=%s task=%s duration=%.2fs "
                    "language=%s device=%s",
                    job_id,
                    task_id,
                    duration,
                    language,
                    self.device,
                )

                inference_start = time.perf_counter()

                with torch.inference_mode():
                    output = self.model.generate(
                        **inputs,
                        return_dict_in_generate=True,
                        max_new_tokens=MAX_NEW_TOKENS,
                    )

                # Ensure asynchronous GPU work has finished before timing.
                if self.device.type == "cuda":
                    torch.cuda.synchronize(self.device)

                inference_seconds = (
                    time.perf_counter() - inference_start
                )

                decode_start = time.perf_counter()

                need_language_tag = (
                    language == "auto" and KEEP_LANGUAGE_TAG
                )

                transcription = self.processor.decode(
                    output.sequences,
                    skip_special_tokens=not need_language_tag,
                )

                if isinstance(transcription, (list, tuple)):
                    transcription = transcription[0]

                if need_language_tag:
                    detected_output = self.processor.decode(
                        output.sequences,
                        skip_special_tokens=False,
                    )
                    if isinstance(detected_output, (list, tuple)):
                        detected_output = detected_output[0]

                decode_seconds = (
                    time.perf_counter() - decode_start
                )

            result = {
                "audio_path": str(audio_path),
                "sample_rate": int(self.sample_rate),
                "duration": duration,
                "language": language,
                "text": transcription,
            }

            if (
                language == "auto"
                and KEEP_LANGUAGE_TAG
                and detected_output is not None
            ):
                result["text_with_language_tag"] = detected_output

            rtf = inference_seconds / max(duration, 0.001)

            logger.info(
                "ASR timings: job=%s task=%s audio=%.2fs "
                "load=%.3fs preprocess=%.3fs inference=%.3fs "
                "decode=%.3fs RTF=%.3f",
                job_id,
                task_id,
                duration,
                load_seconds,
                preprocess_seconds,
                inference_seconds,
                decode_seconds,
                rtf,
            )

            update_task(
                job_id,
                task_id,
                task_type="asr",
                status="completed",
                audio_path=str(audio_path),
                sample_rate=int(self.sample_rate),
                duration=duration,
                language=language,
                result=result,
                error=None,
            )

            logger.info(
                "ASR completed: job=%s task=%s duration=%.2fs "
                "text_length=%d",
                job_id,
                task_id,
                duration,
                len(transcription),
            )

            return None

        except Exception as exc:
            logger.exception(
                "ASR failed: job=%s task=%s audio=%s",
                job_id,
                task_id,
                audio_path,
            )

            try:
                update_task(
                    job_id,
                    task_id,
                    task_type="asr",
                    status="failed",
                    audio_path=str(audio_path),
                    error=str(exc),
                )
            except Exception:
                logger.exception(
                    "Could not persist ASR failure: task=%s",
                    task_id,
                )

            raise


app = ASRConsumer.bind()
