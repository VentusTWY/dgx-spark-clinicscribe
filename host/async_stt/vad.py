
import logging
import os
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio.functional as audio_functional

from transformers import (
    AutoModelForAudioFrameClassification,
    AutoProcessor,
)

from ray import serve
from ray.serve.config import AutoscalingConfig, AutoscalingPolicy
from ray.serve.schema import CeleryAdapterConfig, TaskProcessorConfig
from ray.serve.task_consumer import task_consumer, task_handler

from common.job_store import update_task


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("diarization.consumer")


# ---------------------------------------------------------
# Redis / Celery configuration
# ---------------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "mysecurepassword")

BROKER_URL = (
    f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"
)
BACKEND_URL = (
    f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1"
)

# Keep the existing queue for producer compatibility.
QUEUE_NAME = "vad_queue"

MODEL_ID = "nvidia/Nemotron-3-Diarization"


processor_config = TaskProcessorConfig(
    queue_name=QUEUE_NAME,
    adapter_config=CeleryAdapterConfig(
        broker_url=BROKER_URL,
        backend_url=BACKEND_URL,
    ),
)


# ---------------------------------------------------------
# Ray Serve deployment
# ---------------------------------------------------------

@serve.deployment(
    max_ongoing_requests=50,
    autoscaling_config=AutoscalingConfig(
        min_replicas=1,
        max_replicas=5,
        target_ongoing_requests=20,
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
class DiarizationConsumer:

    def __init__(self):
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)

        self.model = (
            AutoModelForAudioFrameClassification.from_pretrained(
                MODEL_ID,
                device_map="auto",
            )
        )
        self.model.eval()

        self.device = next(self.model.parameters()).device

        self.sample_rate = (
            self.processor.feature_extractor.sampling_rate
        )

        logger.info(
            "Nemotron diarization loaded: device=%s sample_rate=%s",
            self.device,
            self.sample_rate,
        )

    # Default callable entry point for Ray Serve.
    # Delegates to the same processing handler.
    def __call__(self, data):
        return self.detect(data)

    # -----------------------------------------------------
    # Audio loading and preprocessing
    # -----------------------------------------------------

    def _load_audio(self, audio_path: Path):
        audio, source_rate = sf.read(
            str(audio_path),
            dtype="float32",
            always_2d=False,
        )

        # Convert stereo or multichannel audio to mono.
        if audio.ndim == 2:
            audio = audio.mean(axis=1)

        if audio.ndim != 1:
            raise ValueError(
                f"Expected mono audio after conversion; "
                f"shape={audio.shape}"
            )

        if not np.isfinite(audio).all():
            raise ValueError("Audio contains NaN or infinity")

        if audio.size == 0:
            return audio, self.sample_rate

        waveform = torch.from_numpy(
            np.ascontiguousarray(audio)
        )

        # Resample to the model's expected sampling rate.
        if source_rate != self.sample_rate:
            waveform = audio_functional.resample(
                waveform,
                orig_freq=source_rate,
                new_freq=self.sample_rate,
            )

        return waveform.numpy(), self.sample_rate

    # -----------------------------------------------------
    # Diarization task handler
    # -----------------------------------------------------

    @task_handler(name="detect")
    def detect(self, data):
        task_id = str(data["task_id"])
        job_id = str(data.get("job_id", task_id))
        audio_path = Path(data["audio_path"]).resolve()

        try:
            update_task(
                job_id,
                task_id,
                task_type="diarization",
                status="processing",
                audio_path=str(audio_path),
                error=None,
            )

            if not audio_path.is_file():
                raise FileNotFoundError(
                    f"Audio file not found: {audio_path}"
                )

            audio, sample_rate = self._load_audio(audio_path)

            duration = round(len(audio) / sample_rate, 3)

            if len(audio) == 0:
                segments = []

            else:
                inputs = self.processor(
                    audio,
                    sampling_rate=sample_rate,
                    return_tensors="pt",
                )

                inputs = inputs.to(self.device)

                with torch.inference_mode():
                    logits = self.model(**inputs).logits

                raw_segments = self.processor.extract_speaker_dict(
                    logits,
                    inputs.attention_mask,
                )[0]

                segments = [
                    {
                        "speaker": str(segment["Speaker"]),
                        "start": round(
                            float(segment["Start"]), 3
                        ),
                        "end": round(
                            float(segment["End"]), 3
                        ),
                    }
                    for segment in raw_segments
                ]

            speakers = sorted(
                {segment["speaker"] for segment in segments}
            )

            output = {
                "audio_path": str(audio_path),
                "sample_rate": int(sample_rate),
                "duration": duration,
                "segment_count": len(segments),
                "speaker_count": len(speakers),
                "speakers": speakers,
                "segments": segments,
            }

            update_task(
                job_id,
                task_id,
                task_type="diarization",
                status="completed",
                audio_path=str(audio_path),
                sample_rate=int(sample_rate),
                duration=duration,
                segment_count=len(segments),
                speaker_count=len(speakers),
                result=output,
                error=None,
            )

            logger.info(
                "Diarization completed: job=%s task=%s "
                "segments=%d speakers=%d",
                job_id,
                task_id,
                len(segments),
                len(speakers),
            )

            return output

        except Exception as exc:
            logger.exception(
                "Diarization failed: job=%s task=%s",
                job_id,
                task_id,
            )

            try:
                update_task(
                    job_id,
                    task_id,
                    task_type="diarization",
                    status="failed",
                    audio_path=str(audio_path),
                    error=str(exc),
                )
            except Exception:
                logger.exception(
                    "Could not persist diarization failure: "
                    "task=%s",
                    task_id,
                )

            raise


app = DiarizationConsumer.bind()