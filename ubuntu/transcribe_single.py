"""
Transcribe a single .m4a file using WhisperX with speaker diarization.
Usage: python3 transcribe_single.py <audio_file> <output_txt>
"""
import sys, os, json, subprocess, torch, numpy as np, warnings
warnings.filterwarnings("ignore")
import whisperx
from whisperx.diarize import DiarizationPipeline, assign_word_speakers
from datetime import datetime, timezone

EMBEDDINGS_DIR = "/home/eoin/audio-inbox/Embeddings"

HF_TOKEN = os.environ.get("HF_TOKEN", "")  # set in environment, never hardcode
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

audio_file = sys.argv[1]
out_path = sys.argv[2]

_basename = os.path.basename(audio_file)
uuid = _basename.rsplit(".", 1)[0] if "." in _basename else _basename
ts_file = "/home/eoin/uuid_timestamps.json"
ts_map = {}
if os.path.exists(ts_file):
    with open(ts_file) as f:
        ts_map = json.load(f)


def _mp4_creation_time(path: str):
    """For Apple m4a, the QuickTime 'creation_time' atom is the recording start
    (UTC, ISO 8601). Returns 'YYYY-MM-DD HH:MM:SS' in UTC, or None.

    Naive-UTC output matches the existing pipeline convention: the transcript
    'Recorded:' header is read as UTC by mac/build_knowledge_base.py, so we
    deliberately do NOT convert to Dublin local here. File mtime is unreliable
    for Apple Notes audio because the file usually arrives on Ubuntu hours
    after it was recorded; ffprobe pulls the original recording time straight
    from the MP4 atom."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-of", "json", path],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        ct = json.loads(out).get("format", {}).get("tags", {}).get("creation_time", "")
        if not ct:
            return None
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# Resolve recording start time. Priority: explicit ts_map entry → MP4
# creation_time atom (Apple Notes / QuickTime) → file mtime fallback.
recorded_at = ts_map.get(uuid) or _mp4_creation_time(audio_file) \
    or datetime.fromtimestamp(os.path.getmtime(audio_file)).strftime("%Y-%m-%d %H:%M:%S")

print(f"Loading model for {uuid}...")
model = whisperx.load_model("large-v3", device=DEVICE, compute_type=COMPUTE_TYPE)
diarize_model = DiarizationPipeline(token=HF_TOKEN, device=DEVICE)

audio = whisperx.load_audio(audio_file)
result = model.transcribe(audio, batch_size=16, language="en")
language = "en"
print(f"  Language: en (hardcoded)")

# Strip hallucinated repeated segments (same text 3+ times consecutively)
def dedupe_segments(segments, max_repeats=3):
    if not segments:
        return segments
    out = [segments[0]]
    run, run_text = 1, segments[0]["text"].strip()
    for seg in segments[1:]:
        t = seg["text"].strip()
        if t == run_text:
            run += 1
            if run <= max_repeats:
                out.append(seg)
        else:
            run, run_text = 1, t
            out.append(seg)
    return out

result["segments"] = dedupe_segments(result.get("segments", []))

try:
    align_model, metadata = whisperx.load_align_model(language_code=language, device=DEVICE)
    result = whisperx.align(result["segments"], align_model, metadata, audio, device=DEVICE)
except Exception as e:
    print(f"  Alignment skipped: {e}")

diarize_segments = diarize_model(audio)
result = assign_word_speakers(diarize_segments, result)

# Audio duration (real, from ffprobe) and transcript end (last segment) — surfaced
# in the transcript header so the Mac build can flag partial recordings. iCloud
# sometimes exports only the first N seconds of an Apple Notes audio file; without
# this, the pipeline silently produces a transcript that misses most of the meeting.
def _audio_duration_seconds(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


audio_duration = _audio_duration_seconds(audio_file)
transcript_end = max((seg.get("end", seg.get("start", 0.0))
                      for seg in result["segments"]), default=0.0)

with open(out_path, "w") as f:
    f.write(f"File: {uuid}\n")
    f.write(f"Recorded: {recorded_at}\n")
    f.write(f"Duration: {audio_duration:.2f}\n")
    f.write(f"Transcript-end: {transcript_end:.2f}\n")
    f.write("-" * 60 + "\n\n")
    for seg in result["segments"]:
        speaker = seg.get("speaker", "UNKNOWN")
        start = seg["start"]
        text = seg["text"].strip()
        mins = int(start // 60)
        secs = int(start % 60)
        f.write(f"[{speaker}] {mins:02d}:{secs:02d} - {text}\n")

# ── Extract per-speaker voice embeddings ─────────────────────────────────────
# Audio is already loaded as float32 numpy array at 16kHz.
# We use speechbrain ECAPA-TDNN (192-dim) — same model pyannote uses internally.
try:
    from speechbrain.inference.speaker import EncoderClassifier
    os.makedirs(EMBEDDINGS_DIR, exist_ok=True)

    enc = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": DEVICE},
        savedir="/home/eoin/.cache/speechbrain/spkrec-ecapa-voxceleb"
    )

    # Group segment start/end times by speaker label
    speaker_segs = {}  # label → list of (start_sample, end_sample)
    sr = 16000
    total_samples = len(audio)
    for seg in result["segments"]:
        label = seg.get("speaker", "UNKNOWN")
        if label == "UNKNOWN":
            continue
        start_s = int(seg["start"] * sr)
        end_s = min(int(seg["end"] * sr), total_samples)
        if end_s - start_s >= sr:  # skip < 1 sec
            speaker_segs.setdefault(label, []).append((start_s, end_s))

    emb_output = {}
    for label, segs in speaker_segs.items():
        embeddings = []
        for start_s, end_s in segs[:20]:  # cap at 20 segments per speaker
            chunk = torch.tensor(audio[start_s:end_s], dtype=torch.float32).unsqueeze(0)
            with torch.no_grad():
                emb = enc.encode_batch(chunk.cuda())  # (1, 1, 192)
            embeddings.append(emb.squeeze().cpu().numpy())
        if embeddings:
            mean_emb = np.mean(embeddings, axis=0).tolist()
            emb_output[label] = {"embedding": mean_emb, "n_segments": len(embeddings)}

    emb_path = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
    with open(emb_path, "w") as ef:
        json.dump(emb_output, ef)
    print(f"  Embeddings saved: {list(emb_output.keys())}")
    del enc
except Exception as e:
    print(f"  Embedding extraction skipped: {e}")

torch.cuda.empty_cache()
print(f"  Saved: {out_path}")
