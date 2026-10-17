# Host async_stt worker sources

These four files were supplied from the original host on 17 October 2026 and are preserved unchanged. This directory records those sources; it is not a complete standalone deployment or a verified reproduction of the host directory layout.

- `pipeline.py`: Celery/Ray pipeline orchestration.
- `vad.py`: diarization worker.
- `asr.py`: speech recognition worker.
- `llm.py`: host summarizer worker using vLLM directly. This is separate from ClinicScribe's `agent/llm.py` OpenAI-compatible HTTP client.

The files require the original host environment, including `common/job_store.py` (not supplied), compatible Ray Serve task-consumer APIs, Celery/Redis, model dependencies and model artifacts. Preserve the host's working directories, shared storage and deployment configuration when using these modules. Syntax was checked; GPU inference and live worker integration were not tested when importing these files into this repository.

Adding these sources does not register or launch workers, change ClinicScribe's startup scripts, or connect this summarizer to the ClinicScribe agent. Do not launch a second model service merely because these files are now present.
