# Vendored from ByteDance-Seed/m3-agent (Apache-2.0)

- Source: https://github.com/ByteDance-Seed/m3-agent (branch `master`)
- `face_extraction.py` = `mmagent/src/face_extraction.py`, copied unmodified.
- License: Apache License 2.0, see `LICENSE` in this folder.

Why copy instead of `import mmagent`: `mmagent/__init__.py` eagerly imports every submodule,
which loads InsightFace `buffalo_l`, torch-loads `models/pretrained_eres2netv2.ckpt` onto CUDA and
reads `configs/*.json` relative to the CWD. The package cannot be imported standalone.

Not reused (boundary):
- `voice_processing.py`: ASR + diarization is done by sending the whole clip to `gemini-1.5-pro-002`
  (second-level timestamps), not a local model. We use faster-whisper / M3-Bench `.srt` subtitles instead.
- `face_clustering.py`: offline HDBSCAN over a 30 s clip; we need online incremental matching, which
  mirrors `VideoGraph.search_img_nodes` (mean cosine similarity vs. stored embeddings, threshold from
  `configs/memory_config.json: img_matching_threshold=0.3`).
