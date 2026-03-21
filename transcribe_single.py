"""
Transcribe a single .m4a file using WhisperX with speaker diarization.
Usage: python3 transcribe_single.py <audio_file> <output_txt>
"""
import sys, os, json, torch, numpy as np, warnings
warnings.filterwarnings("ignore")
import whisperx
from whisperx.diarize import DiarizationPipeline, assign_word_speakers
from datetime import datetime

EMBEDDINGS_DIR = "/home/eoin/audio-inbox/Embeddings"

HF_TOKEN = os.environ.get("HF_TOKEN", "")  # set in environment, never hardcode
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

audio_file = sys.argv[1]
out_path = sys.argv[2]

uuid = os.path.basename(audio_file).replace(".m4a", "")
ts_file = "/home/eoin/uuid_timestamps.json"
ts_map = {}
if os.path.exists(ts_file):
    with open(ts_file) as f:
        ts_map = json.load(f)

recorded_at = ts_map.get(uuid, datetime.fromtimestamp(os.path.getmtime(audio_file)).strftime("%Y-%m-%d %H:%M:%S"))

print(f"Loading model for {uuid}...")
model = whisperx.load_model("large-v2", device=DEVICE, compute_type=COMPUTE_TYPE)
diarize_model = DiarizationPipeline(token=HF_TOKEN, device=DEVICE)

audio = whisperx.load_audio(audio_file)
result = model.transcribe(audio, batch_size=16, language="en")
language = "en"
print(f"  Language: en (hardcoded)")

try:
    align_model, metadata = whisperx.load_align_model(language_code=language, device=DEVICE)
    result = whisperx.align(result["segments"], align_model, metadata, audio, device=DEVICE)
except Exception as e:
    print(f"  Alignment skipped: {e}")

diarize_segments = diarize_model(audio)
result = assign_word_speakers(diarize_segments, result)

with open(out_path, "w") as f:
    f.write(f"File: {uuid}.m4a\n")
    f.write(f"Recorded: {recorded_at}\n")
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
